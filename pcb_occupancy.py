"""按真实碰撞模型生成有预算的候选点栅格，最终验收仍由精确几何完成。

栅格中的 ``True`` 表示阻塞。网格点通过不代表相邻点之间的线段通过，
也不代表新线与新过孔彼此兼容；调用方必须精确提交并复核最终候选。
"""
from __future__ import annotations

import math
import time

import numpy as np
from shapely import contains_xy
from shapely.errors import GEOSException

from pcb_geometry import trace_bounds, via_bounds
from pcb_pipeline_common import PipelineError


class OccupancyBudgetError(PipelineError):
    """栅格空间或合作式时间预算耗尽；不能据此判断连续几何无解。"""


def _number(value, name, *, positive=False):
    """校验有限数值；返回浮点数，格式或范围不合法时抛出 PipelineError。"""
    try:
        valid = (not isinstance(value, bool) and isinstance(value, (int, float))
                 and math.isfinite(value) and (not positive or value > 0))
    except (OverflowError, TypeError, ValueError):
        valid = False
    if not valid:
        raise PipelineError(f"占用栅格 {name} 必须是{'正' if positive else ''}有限数值")
    return float(value)


class OccupancyGrid:
    """一个网络、线宽和过孔规格对应的按层占用栅格。

    入参：model 为 CollisionModel，或带 base/forbidden 的有序拓扑包装；
    net 为目标网络；settings 使用路由器的 width/hole/diameter/layers/
    via_layers/vias；step 为 mil 步长；bounds 为闭区间整数索引框，省略时
    从板框求得。世界坐标恒为索引乘 step。max_cells 限制全部铜层及一张
    全叠层过孔栅格的布尔单元总数；deadline 为 monotonic 绝对截止时间；
    budget_check 是每批调用的现有预算检查函数，stats 为可选共享统计字典。

    出参：trace_free/via_free 返回候选点是否可用，update 原地更新局部。
    异常：非法参数或几何计算失败抛 PipelineError；空间或时间耗尽抛
    OccupancyBudgetError；budget_check 自身异常原样传递。失败的构造或
    更新不会产生可用栅格。单次 GEOS 调用不能由合作式时限抢占。
    """

    def __init__(self, model, net, settings, step, bounds=None, *,
                 max_cells=2_000_000, deadline=None, stats=None, budget_check=None):
        started = time.monotonic()
        self.ready = False
        self.stats = stats if stats is not None else {}
        self.budget_check = budget_check
        self.deadline = None if deadline is None else _number(deadline, "deadline")
        self.step = _number(step, "step", positive=True)
        if type(max_cells) is not int or max_cells < 1:
            raise PipelineError("占用栅格 max_cells 必须是正整数")
        if not isinstance(net, str) or not net or not isinstance(settings, dict):
            raise PipelineError("占用栅格必须给出非空网络名和明确路由规格")
        self.net, self.settings = net, dict(settings)
        self.width = _number(settings.get("width"), "width", positive=True)
        self.hole = _number(settings.get("hole"), "hole", positive=True)
        self.diameter = _number(settings.get("diameter"), "diameter", positive=True)
        if self.hole >= self.diameter:
            raise PipelineError("占用栅格过孔孔径必须小于铜外径")
        self._set_model(model)
        self.layers = tuple(self.base.rules.copper_layers)
        requested = settings.get("layers", self.layers)
        via_layers = settings.get("via_layers", requested)
        if (not isinstance(requested, (list, tuple)) or not requested
                or any(type(v) is not int for v in requested)
                or len(set(requested)) != len(requested)
                or not set(requested).issubset(self.layers)
                or not isinstance(via_layers, (list, tuple))
                or any(type(v) is not int for v in via_layers)
                or len(set(via_layers)) != len(via_layers)
                or not set(via_layers).issubset(self.layers)):
            raise PipelineError("占用栅格走线层和过孔允许层必须来自真实物理叠层")
        self.trace_layers = frozenset(requested)
        if type(settings.get("vias", True)) is not bool:
            raise PipelineError("占用栅格 vias 必须为布尔值")
        self.vias_allowed = settings.get("vias", True) and set(self.layers).issubset(via_layers)
        if bounds is None:
            scaled = [v / self.step for v in self.base.board.bounds]
            if not all(math.isfinite(v) for v in scaled):
                raise PipelineError("占用栅格步长低于坐标尺度可表示范围")
            bounds = (math.ceil(scaled[0]), math.ceil(scaled[1]),
                      math.floor(scaled[2]), math.floor(scaled[3]))
        if (not isinstance(bounds, (list, tuple)) or len(bounds) != 4
                or any(type(v) is not int for v in bounds)
                or bounds[0] > bounds[2] or bounds[1] > bounds[3]):
            raise PipelineError("占用栅格 bounds 必须为非空整数索引框")
        self.bounds = tuple(bounds)
        self.nx, self.ny = bounds[2] - bounds[0] + 1, bounds[3] - bounds[1] + 1
        self.cell_count = self.nx * self.ny * (len(self.layers) + 1)
        if self.cell_count > max_cells:
            raise OccupancyBudgetError(
                f"占用栅格需要 {self.cell_count} 个布尔单元，超过预算 {max_cells}；未找到候选，未证明无解")
        try:
            world = [v * self.step for v in self.bounds]
        except OverflowError as exc:
            raise PipelineError("占用栅格索引超出世界坐标的浮点表示范围") from exc
        if (not all(math.isfinite(v) for v in world)
                or any(math.ulp(v) >= self.step for v in world)):
            raise PipelineError("占用栅格步长低于世界坐标的浮点分辨率")
        self._check()
        tolerance = self.base.rules.curve_tolerance
        self.trace_radius = trace_bounds(dict(sx=0, sy=0, ex=0, ey=0,
                                             width=self.width), tolerance).outer.bounds[2]
        copper, drill = via_bounds(dict(x=0, y=0, hole=self.hole,
                                        diameter=self.diameter), tolerance)
        self.via_radius = max(abs(v) for v in copper.outer.bounds)
        self.drill_radius = max(abs(v) for v in drill.outer.bounds)
        self.blocked = {layer: np.ones((self.ny, self.nx), dtype=np.bool_) for layer in self.layers}
        self.via_blocked = np.ones((self.ny, self.nx), dtype=np.bool_)
        # 稀疏精检阻塞掩码独立于基础栅格，局部更新不能清掉已发现的冲突。
        self.rejected_cells, self.rejected_vias = set(), set()
        try:
            self._render(self.bounds)
            self._check()
            self.ready = True
        except GEOSException as exc:
            raise PipelineError(f"占用栅格几何构建失败：{exc}") from exc
        finally:
            self._add_stat("occupancy_build_seconds", time.monotonic() - started)
        self._add_stat("occupancy_builds", 1)
        self.stats["occupancy_cells"] = max(self.stats.get("occupancy_cells", 0), self.cell_count)

    def _set_model(self, model):
        """读取基础模型和显式同网禁铜；接口不匹配时抛出 PipelineError。"""
        base = getattr(model, "base", model)
        if not all(hasattr(base, key) for key in ("rules", "board", "copper", "drills", "keepouts", "net_tags")):
            raise PipelineError("占用栅格需要完整 CollisionModel")
        forbidden = getattr(model, "forbidden", {})
        if not isinstance(forbidden, dict):
            raise PipelineError("占用栅格有序拓扑禁铜必须按层给出")
        self.model, self.base, self.forbidden = model, base, forbidden

    def _check(self):
        """执行合作式预算检查；异常直接终止，不把未知单元视为可用。"""
        if self.budget_check is not None:
            self.budget_check()
        if self.deadline is not None and time.monotonic() >= self.deadline:
            raise OccupancyBudgetError("占用栅格时间预算耗尽；未找到候选，未证明无解")

    def _add_stat(self, key, value):
        self.stats[key] = self.stats.get(key, 0) + value

    def _inflate(self, shape, distance):
        """返回障碍的保守外扩；固定圆弧分段的外接补偿避免向内近似。"""
        scale = max(1.0, distance, *(abs(v) for v in shape.bounds))
        margin = max(8 * math.ulp(scale), 1e-12)
        radius = (distance + margin) / math.cos(math.pi / 64)
        return shape.buffer(radius, quad_segs=16)

    def _layer_obstacles(self, layer, radius, *, via=False):
        """枚举当前网络在指定层的膨胀障碍；不修改模型或原始铜。"""
        base, net = self.base, self.net
        for other in base.copper:
            if layer in other["layers"] and net != other["net"] and not other["outer"].is_empty:
                gap = base.rules.get_clearance(base.net_type(net), base.net_type(other["net"]))
                yield other["outer"], radius + gap
        for drill in base.drills:
            if layer not in drill["layers"]:
                continue
            if not drill["plated"] or net != drill["net"]:
                gap = base.rules.npth_clearance if not drill["plated"] else 0
                yield drill["outer"], radius + gap
            # 同网已有金属化孔允许走线接入，但新孔仍必须核孔到孔净距。
            if via:
                yield drill["outer"], self.drill_radius + base.rules.hole_to_hole_clearance
        for shape, layers, gap, nets, types in base.keepouts:
            if (layer in layers and (nets is None or net in nets)
                    and (types is None or base.net_type(net) in types)):
                yield shape, radius + gap
        shape = self.forbidden.get(layer)
        if shape is not None and not shape.is_empty:
            yield shape, radius

    def _mask_obstacles(self, mask, x, y, layer, radius, *, via=False):
        """按障碍局部包围框批量采样；每批检查时间，失败由调用方关闭栅格。"""
        for shape, distance in self._layer_obstacles(layer, radius, via=via):
            self._check()
            if shape.is_empty:
                continue
            sx0, sy0, sx1, sy1 = shape.bounds
            # 用外接补偿后的距离筛框，不能以未补偿的半径漏掉临界单元。
            extent = (distance + max(8 * math.ulp(max(1.0, distance, *(abs(v) for v in shape.bounds))), 1e-12)) / math.cos(math.pi / 64)
            if sx1 + extent < x[0] or sx0 - extent > x[-1] or sy1 + extent < y[0] or sy0 - extent > y[-1]:
                continue
            expanded = self._inflate(shape, distance)
            ex0, ey0, ex1, ey1 = expanded.bounds
            x0, x1 = np.searchsorted(x, [ex0, ex1], side="left")
            y0, y1 = np.searchsorted(y, [ey0, ey1], side="left")
            x1, y1 = min(len(x), x1 + 1), min(len(y), y1 + 1)
            if x0 >= x1 or y0 >= y1:
                continue
            mask[y0:y1, x0:x1] |= contains_xy(expanded, x[None, x0:x1], y[y0:y1, None])

    def _erased_copper(self, mask, x, y):
        """保守标记新孔会吞没既有铜的点；返回值通过 mask 原地合并。

        凸包全部顶点落在孔的外包圆内是吞铜的必要条件。此处允许额外
        阻塞临界点，但绝不把这种候选筛查当成物理连通或最终合法性证明。
        """
        for other in self.base.copper:
            self._check()
            if not set(other["layers"]).intersection(self.layers):
                continue
            inner = other["inner"]
            if inner.is_empty:
                mask[:] = True
                return
            bx0, by0, bx1, by1 = inner.bounds
            scale = max(1.0, *(abs(v) for v in inner.bounds))
            radius = self.drill_radius + max(8 * math.ulp(scale), 1e-12)
            if max(bx1 - bx0, by1 - by0) > 2 * radius:
                continue
            x0 = int(np.searchsorted(x, bx1 - radius, side="left"))
            x1 = int(np.searchsorted(x, bx0 + radius, side="right"))
            y0 = int(np.searchsorted(y, by1 - radius, side="left"))
            y1 = int(np.searchsorted(y, by0 + radius, side="right"))
            if x0 >= x1 or y0 >= y1:
                continue
            hull = inner.convex_hull
            coords = hull.exterior.coords if hull.geom_type == "Polygon" else hull.coords
            erased = np.ones((y1 - y0, x1 - x0), dtype=np.bool_)
            limit = math.nextafter(radius * radius, math.inf)
            for index, (px, py) in enumerate(coords):
                if index % 64 == 0:
                    self._check()
                erased &= ((x[None, x0:x1] - px) ** 2 + (y[y0:y1, None] - py) ** 2 <= limit)
                if not erased.any():
                    break
            mask[y0:y1, x0:x1] |= erased

    def _render(self, bounds):
        """重算闭区间索引切片，所有全板数组在切片之外保持逐字节不变。"""
        ix0, iy0, ix1, iy1 = bounds
        x = np.arange(ix0, ix1 + 1, dtype=np.float64) * self.step
        y = np.arange(iy0, iy1 + 1, dtype=np.float64) * self.step
        xs = slice(ix0 - self.bounds[0], ix1 - self.bounds[0] + 1)
        ys = slice(iy0 - self.bounds[1], iy1 - self.bounds[1] + 1)
        rules, tag = self.base.rules, self.base.net_tags.get(self.net, {})
        allowed = set(tag.get("allowed_layers", self.layers))
        width_ok = self.width >= max(rules.minimum_trace_width, tag.get("width", 0), tag.get("min_width", 0))
        # 板框内缩还保留切孔；contains_xy 在边界为 False，形成保守拒绝。
        coordinate_scale = max(1.0, *(abs(v) for v in self.base.board.bounds))
        margin = max(8 * math.ulp(coordinate_scale), 1e-12)
        trace_inset = (self.trace_radius + rules.board_edge_clearance + margin) / math.cos(math.pi / 64)
        via_inset = (max(self.via_radius, self.drill_radius) + rules.board_edge_clearance + margin) / math.cos(math.pi / 64)
        trace_inside = self.base.board.buffer(-trace_inset, quad_segs=16)
        via_inside = self.base.board.buffer(-via_inset, quad_segs=16)
        via = ~contains_xy(via_inside, x[None, :], y[:, None])
        via_allowed = (self.vias_allowed and len(self.layers) >= 2 and tag.get("allow_via") is not False
                       and set(self.layers).issubset(allowed) and self.hole >= rules.minimum_drill
                       and (self.diameter - self.hole) / 2 >= rules.min_annular_ring)
        for layer in self.layers:
            self._check()
            if layer not in self.trace_layers or layer not in allowed or not width_ok:
                self.blocked[layer][ys, xs] = True
            else:
                mask = ~contains_xy(trace_inside, x[None, :], y[:, None])
                self._mask_obstacles(mask, x, y, layer, self.trace_radius)
                self.blocked[layer][ys, xs] = mask
            if via_allowed:
                self._mask_obstacles(via, x, y, layer, self.via_radius, via=True)
        if via_allowed:
            self._erased_copper(via, x, y)
        else:
            via[:] = True
        self.via_blocked[ys, xs] = via

    def trace_free(self, layer, ix, iy):
        """入参为层号和整数索引；返回候选点是否可用，越界或未就绪返回 False。"""
        if (not self.ready or type(layer) is not int or layer not in self.blocked
                or type(ix) is not int or type(iy) is not int
                or not self.bounds[0] <= ix <= self.bounds[2] or not self.bounds[1] <= iy <= self.bounds[3]):
            return False
        return (not bool(self.blocked[layer][iy - self.bounds[1], ix - self.bounds[0]])
                and (ix, iy, layer) not in self.rejected_cells)

    def via_free(self, ix, iy):
        """入参为整数索引；返回全物理叠层过孔候选是否可用，越界或未就绪拒绝。"""
        if (not self.ready or type(ix) is not int or type(iy) is not int
                or not self.bounds[0] <= ix <= self.bounds[2] or not self.bounds[1] <= iy <= self.bounds[3]):
            return False
        return (not bool(self.via_blocked[iy - self.bounds[1], ix - self.bounds[0]])
                and (ix, iy) not in self.rejected_vias)

    def block_cells(self, nodes, *, via=False):
        """记录精确复核拒绝的候选点，返回本次新增阻塞数量。

        nodes 为 (ix,iy,layer) 列表；via=True 时为 (ix,iy) 列表。
        不接受小数索引或未知层；抛出 PipelineError 前不提交任何新增点。
        """
        if type(via) is not bool:
            raise PipelineError("占用栅格 block_cells.via 必须为布尔值")
        additions = set()
        for node in nodes:
            if (not isinstance(node, (tuple, list)) or len(node) != (2 if via else 3)
                    or any(type(v) is not int for v in node)
                    or (not via and node[2] not in self.layers)):
                raise PipelineError("精确复核阻塞点必须使用合法整数索引与真实层号")
            if self.bounds[0] <= node[0] <= self.bounds[2] and self.bounds[1] <= node[1] <= self.bounds[3]:
                additions.add(tuple(node))
        target = self.rejected_vias if via else self.rejected_cells
        count = len(additions - target)
        target.update(additions)
        self._add_stat("occupancy_refined_cells", count)
        return count

    def update(self, model, changed_bounds, *, deadline=None, budget_check=None):
        """提交网络后按变更包围框更新，返回实际重算的布尔单元数。

        changed_bounds 为所有变更几何旧、新外包框的并框（世界 mil）。
        调用方须完整提供变更范围；本方法内部按线宽、孔径和全部适用净距
        再扩展。板框、规则、网络标签变化需要新建栅格，不能局部更新。
        deadline/budget_check 可替换本轮预算。精确复核阻塞点继续保留。
        参数错误抛 PipelineError，预算耗尽抛 OccupancyBudgetError；任何
        更新异常均使整个栅格不可用，不能继续读取半更新状态。
        """
        self.ready = False
        if (not isinstance(changed_bounds, (list, tuple)) or len(changed_bounds) != 4):
            raise PipelineError("增量更新必须给出完整变更的世界包围框")
        rect = tuple(_number(v, "changed_bounds") for v in changed_bounds)
        if rect[0] > rect[2] or rect[1] > rect[3]:
            raise PipelineError("增量更新包围框方向不合法")
        previous = self.base
        new_base = getattr(model, "base", model)
        if (not all(hasattr(new_base, key) for key in ("rules", "board", "net_tags", "copper"))
                or new_base.rules != previous.rules
                or new_base.net_tags != previous.net_tags or not new_base.board.equals(previous.board)):
            raise PipelineError("板框、规则或网络标签已变化，必须重新构建占用栅格")
        if (any(o["inner"].is_empty for o in new_base.copper)
                != any(o["inner"].is_empty for o in previous.copper)):
            raise PipelineError("空铜对象改变了全局过孔可用性，必须重新构建占用栅格")
        if deadline is not None:
            self.deadline = _number(deadline, "deadline")
        if budget_check is not None:
            self.budget_check = budget_check
        started = time.monotonic()
        try:
            self._check()
            self._set_model(model)
            # 删除最大退让障碍后，新模型可能只剩较小净距；旧膨胀范围也须刷新。
            gap = max([self.base.rules.npth_clearance, self.base.rules.hole_to_hole_clearance,
                       *(base.rules.get_clearance(base.net_type(self.net), base.net_type(o["net"]))
                         for base in (previous, self.base) for o in base.copper),
                       *(ko[2] for base in (previous, self.base) for ko in base.keepouts)])
            extent = (max(self.trace_radius, self.via_radius, self.drill_radius) + gap + self.step) / math.cos(math.pi / 64)
            bounds = (max(self.bounds[0], math.floor((rect[0] - extent) / self.step)),
                      max(self.bounds[1], math.floor((rect[1] - extent) / self.step)),
                      min(self.bounds[2], math.ceil((rect[2] + extent) / self.step)),
                      min(self.bounds[3], math.ceil((rect[3] + extent) / self.step)))
            count = 0
            if bounds[0] <= bounds[2] and bounds[1] <= bounds[3]:
                self._render(bounds)
                count = (bounds[2] - bounds[0] + 1) * (bounds[3] - bounds[1] + 1) * (len(self.layers) + 1)
            self._check()
            self.ready = True
            self._add_stat("occupancy_updates", 1)
            self._add_stat("occupancy_updated_cells", count)
            return count
        except GEOSException as exc:
            raise PipelineError(f"占用栅格增量几何更新失败：{exc}") from exc
        finally:
            self._add_stat("occupancy_update_seconds", time.monotonic() - started)


__all__ = ["OccupancyGrid", "OccupancyBudgetError"]
