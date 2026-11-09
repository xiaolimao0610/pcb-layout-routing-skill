"""有预算的 PathFinder 协商布线候选；最终合法性沿用原精确验收器。

原板铜始终固定。本模块只在内存中拆除本批计算生成的临时候选，
共享栅格消失后仍逐网调用 pcb_routing._commit，不把协商收敛当作验收。
"""
from __future__ import annotations

import copy
import heapq
import itertools
import math
import time
from collections import Counter, OrderedDict

import numpy as np
from shapely import contains_xy
from shapely.geometry import LineString, Point
from shapely.ops import unary_union

from pcb_occupancy import OccupancyBudgetError, OccupancyGrid
from pcb_pipeline_common import CollisionModel, PipelineError
import pcb_routing as routing


def _mst(points, budget=None):
    """输入二维点序列，返回曼哈顿最小生成树的边及长度；预算异常原样传播。"""
    if not points:
        return [], 0.0
    edges, total = [], 0.0
    def distance(a, b):
        value = abs(a[0] - b[0]) + abs(a[1] - b[1])
        if not math.isfinite(value):
            raise PipelineError("直角拓扑距离超出有限数值范围")
        return value

    distances = {i: (distance(points[0], p), 0) for i, p in enumerate(points) if i}
    while distances:
        if budget is not None:
            budget.check()
        target = min(distances, key=lambda i: (distances[i], i))
        length, source = distances.pop(target)
        edges.append((source, target))
        total += length
        if not math.isfinite(total):
            raise PipelineError("直角拓扑总长度超出有限数值范围")
        for other in distances:
            length = distance(points[target], points[other])
            if (length, target) < distances[other]:
                distances[other] = (length, target)
    return edges, total


def rectilinear_topology(points, *, max_candidates=128, max_rounds=2, budget=None):
    """生成直角 MST 并逐次尝试一个 Hanan 网格 Steiner 点。

    入参：有限二维坐标列表、每轮候选数上限和插点轮数上限，可选预算对象。
    出参：points、edges、mst_length、steiner_length、steiner_points 及计数。
    异常：坐标或限额非法抛 PipelineError，预算耗尽沿用预算异常。
    返回的拓扑只是布线顺序及分支候选，不修改或代替原网表与真实端子。
    """
    try:
        valid = (isinstance(points, (list, tuple)) and bool(points)
                 and all(isinstance(p, (list, tuple)) and len(p) == 2
                         and all(not isinstance(v, bool) and isinstance(v, (int, float))
                                 and math.isfinite(v) for v in p) for p in points)
                 and type(max_candidates) is int and max_candidates > 0
                 and type(max_rounds) is int and max_rounds >= 0)
    except (OverflowError, TypeError, ValueError):
        valid = False
    if not valid:
        raise PipelineError("直角拓扑需要有限二维坐标、正候选预算和非负插点轮数")
    result = [tuple(map(float, p)) for p in points]
    initial_count = len(result)
    edges, original_length = _mst(result, budget)
    length, tested, rounds, truncated = original_length, 0, 0, False
    # 确定性枚举；预算截断只限制改进搜索，原 MST 始终保留。
    xs, ys = sorted({p[0] for p in result}), sorted({p[1] for p in result})
    for _ in range(max_rounds):
        best = None
        available = (p for p in itertools.product(xs, ys) if p not in result)
        for count, point in enumerate(available):
            if budget is not None:
                budget.check()
            if count >= max_candidates:
                truncated = True
                break
            candidate_edges, candidate_length = _mst(result + [point], budget)
            tested += 1
            if candidate_length < length - 1e-9 and (best is None or (candidate_length, point) < (best[0], best[1])):
                best = candidate_length, point, candidate_edges
        if best is None:
            break
        length, point, edges = best
        result.append(point)
        rounds += 1
    # 先沿树从真实端子出发，分支点不会被当作真实焊盘写入网表。
    adjacency = {i: [] for i in range(len(result))}
    for a, b in edges:
        adjacency[a].append(b)
        adjacency[b].append(a)
    ordered, visited, queue = [], {0}, [0]
    for source in queue:
        for target in sorted(adjacency[source]):
            if target not in visited:
                ordered.append((source, target))
                visited.add(target)
                queue.append(target)
    return dict(points=result, edges=ordered, mst_length=original_length,
                steiner_length=length, steiner_points=result[initial_count:],
                candidates_tested=tested, steiner_rounds=rounds,
                candidate_budget_reached=truncated)


class _Resources:
    """维护容量为一的分层格胞及历史代价；同网重复经过只计一次。

    入参：固定板碰撞模型、所有待布网络规格、步长、栅格空间预算和总预算。
    出参：footprint 返回资源集合，cost 返回候选动作代价，replace 拆换一网。
    异常：空间或时间耗尽抛 RoutingFailure；几何异常沿原工具链传播。
    """

    def __init__(self, model, settings, step, max_cells, budget):
        self.model, self.settings, self.step, self.budget = model, settings, step, budget
        scaled = [value / step for value in model.board.bounds]
        if not all(math.isfinite(value) for value in scaled):
            raise PipelineError("协商栅格步长低于坐标尺度可表示范围")
        self.bounds = math.ceil(scaled[0]), math.ceil(scaled[1]), math.floor(scaled[2]), math.floor(scaled[3])
        self.max_cells = max_cells
        self.owners, self.counts, self.history = {}, Counter(), {}
        self.grid_cache = OrderedDict()
        self.radii = {}
        self.via_radii = {}
        for net, spec in settings.items():
            gap = max((model.rules.get_clearance(model.net_type(net), model.net_type(other))
                       for other in settings if other != net), default=0.0)
            self.radii[net] = spec["width"] / 2 + gap / 2
            self.via_radii[net] = max(spec["diameter"] / 2 + gap / 2,
                                      spec["hole"] / 2 + model.rules.hole_to_hole_clearance / 2)

    def _raster(self, shape, radius, layers):
        """保守覆盖几何接触的格胞；返回(layer,ix,iy)集合，不把空采样当安全。"""
        self.budget.check()
        # 每个格胞用中心表示，额外半对角线覆盖格胞边界与交叉线段。
        margin = self.step / math.sqrt(2) + max(1e-9, math.ulp(max(1.0, *map(abs, shape.bounds))) * 8)
        expanded = shape.buffer((radius + margin) / math.cos(math.pi / 64), quad_segs=16)
        bx = expanded.bounds
        lo_x, hi_x = max(self.bounds[0], math.ceil(bx[0] / self.step)), min(self.bounds[2], math.floor(bx[2] / self.step))
        lo_y, hi_y = max(self.bounds[1], math.ceil(bx[1] / self.step)), min(self.bounds[3], math.floor(bx[3] / self.step))
        if lo_x > hi_x or lo_y > hi_y:
            return set()
        cells = (hi_x - lo_x + 1) * (hi_y - lo_y + 1) * len(layers)
        if cells > self.max_cells:
            raise routing.RoutingFailure("OCCUPANCY_BUDGET", "协商资源栅格空间预算内未找到候选，未证明无解",
                                         resource_cells=cells, max_cells=self.max_cells)
        x = np.arange(lo_x, hi_x + 1, dtype=np.float64) * self.step
        y = np.arange(lo_y, hi_y + 1, dtype=np.float64) * self.step
        mask = contains_xy(expanded, x[None, :], y[:, None])
        rows, cols = np.nonzero(mask)
        result = {(layer, int(ix) + lo_x, int(iy) + lo_y) for layer in layers for ix, iy in zip(cols, rows)}
        self.budget.check()
        return result

    def footprint(self, net, traces, vias):
        """输入候选铜图元，返回含线宽、间距及通孔全层的资源集合；受总预算限制。"""
        resources = set()
        for trace in traces:
            shape = LineString([(trace["sx"], trace["sy"]), (trace["ex"], trace["ey"])])
            resources.update(self._raster(shape, self.radii[net], (trace["layer"],)))
        for via in vias:
            resources.update(self._raster(Point(via["x"], via["y"]), self.via_radii[net], tuple(via["layers"])))
        return resources

    def replace(self, net, resources):
        """仅拆换本次计算的资源归属；不接触固定板铜，异常预算在调用前检查。"""
        self.budget.check()
        for resource in self.owners.pop(net, ()):
            self.counts[resource] -= 1
            if not self.counts[resource]:
                del self.counts[resource]
        if resources:
            self.owners[net] = set(resources)
            self.counts.update(resources)

    def cost(self, base, resources, present):
        """节点代价为(base+h_n)*p_n；动作覆盖多资源时采用最坏资源代价。"""
        value = max(((base + self.history.get(cell, 0.0)) * (1 + present * self.counts.get(cell, 0))
                     for cell in resources), default=base)
        if not math.isfinite(value):
            raise routing.RoutingFailure("NEGOTIATED_COST_BUDGET", "协商节点代价超出有限数值范围，未找到候选，未证明无解")
        return value

    def conflicts(self):
        """返回共享资源及相应网络；同网自交不计异网共享，最终仍需物理检查。"""
        shared = {cell for cell, count in self.counts.items() if count > 1}
        nets = sorted(net for net, resources in self.owners.items() if resources & shared)
        return shared, nets


def _preferred_cost(layer, dx, dy, options):
    """返回内层方向乘数；斜线含非优先分量时同样受罚，未配置层返回一。"""
    preferred = options.get("preferred_direction", {}).get(str(layer))
    wrong = (preferred == "horizontal" and abs(dy) > 1e-12) or (preferred == "vertical" and abs(dx) > 1e-12)
    return options.get("nonpreferred_penalty", 1.5) if wrong else 1.0


def _merge_collinear(traces):
    """只合并连续共线同向候选，保持铜覆盖；不进行可能绕过拥塞代价的改道。"""
    result = []
    for trace in traces:
        if (trace["sx"], trace["sy"]) == (trace["ex"], trace["ey"]):
            continue
        if result:
            old = result[-1]
            ax, ay = old["ex"] - old["sx"], old["ey"] - old["sy"]
            bx, by = trace["ex"] - trace["sx"], trace["ey"] - trace["sy"]
            if (old["layer"] == trace["layer"] and old["width"] == trace["width"]
                    and (old["ex"], old["ey"]) == (trace["sx"], trace["sy"])
                    and abs(ax * by - ay * bx) <= 1e-9 and ax * bx + ay * by > 0):
                result[-1] = dict(old, ex=trace["ex"], ey=trace["ey"])
                continue
        result.append(dict(trace))
    return result


def _port_paths(model, net, group, settings, options, grid, budget):
    """从真实铜任意可采样位置或内部 Steiner 点生成端口；无端口时如实失败。"""
    if "steiner" in group:
        x, y, layer = group["steiner"]
        node = (round(x / options["grid_step"]), round(y / options["grid_step"]), layer)
        if grid.trace_free(layer, node[0], node[1]):
            return {node: ([(x, y)], 0.0)}
        return {}
    diagnostics = {}
    return routing._ports(model, net, group, settings, options["grid_step"], grid.bounds,
                          options["max_expansions"], budget, diagnostics, options["diagonal"])


def _obstacle_records(model, budget):
    """输入实际模型与预算，返回铜孔指纹到旧/新包围框的映射；预算异常直接传播。"""
    base = getattr(model, "base", model)
    records = {}
    for kind in ("copper", "drills"):
        for item in getattr(base, kind):
            budget.check()
            shape = item["outer"]
            key = (kind, item["net"], tuple(item["layers"]), shape.wkb,
                   item["inner"].wkb if kind == "copper" else item["plated"])
            # 完全相同对象的数量不改变占用；空几何的全局影响由 update 独立校验。
            records[key] = shape.bounds
    return records


def _grid_for_search(model, net, settings, options, stats, budget, resources, blocked):
    """返回本次搜索的独立拒绝掩码视图，可选复用同规格栅格。

    输入完整碰撞模型、规则选项、共享预算/资源和本次精检禁点；输出可查询栅格。
    缓存按LRU和occupancy_max_cells限制驻留布尔单元总数，不设固定网络槽位数；
    此预算不包含模型、几何指纹等Python对象，不能视为进程总内存上限。规则、板框、
    标签、禁区或有序禁铜变化重建，铜孔差异按完整旧新范围刷新。失败移除缓存，
    不暴露半更新对象。非法输入或预算异常沿原接口传播。
    """
    budget.check()
    enabled = options.get("negotiated_grid_cache", False)
    limit = options.get("occupancy_max_cells", 2_000_000)
    step = options["grid_step"]
    deadline = time.monotonic() + options.get("occupancy_max_seconds", 10)
    cache = resources.grid_cache
    base = getattr(model, "base", model)
    key, records, grid = None, None, None
    if enabled:
        key = (net, repr(settings), step, limit, repr(vars(base.rules)), repr(base.net_tags),
               base.board.wkb,
               tuple((shape.wkb, tuple(layers), gap, repr(nets), repr(types))
                     for shape, layers, gap, nets, types in base.keepouts),
               tuple(sorted((layer, shape.wkb) for layer, shape in getattr(model, "forbidden", {}).items())))
        records = _obstacle_records(model, budget)
        entry = cache.pop(key, None)
        if entry is not None:
            grid, previous = entry
            changed = records.keys() ^ previous.keys()
            try:
                if changed:
                    boxes = [records.get(item, previous.get(item)) for item in changed]
                    boxes = [b for b in boxes if len(b) == 4 and all(math.isfinite(v) for v in b)]
                    if not boxes:
                        grid = None
                    else:
                        bounds = (min(b[0] for b in boxes), min(b[1] for b in boxes),
                                  max(b[2] for b in boxes), max(b[3] for b in boxes))
                        grid.update(model, bounds, deadline=deadline, budget_check=budget.check)
                else:
                    grid._set_model(model)
                    grid.deadline, grid.budget_check = deadline, budget.check
                if grid is not None:
                    stats["negotiated_grid_cache_hits"] = stats.get("negotiated_grid_cache_hits", 0) + 1
            except OccupancyBudgetError as exc:
                raise routing.RoutingFailure("OCCUPANCY_BUDGET", str(exc), net=net) from exc
            except routing.RoutingFailure:
                raise
            except PipelineError:
                # 例如空铜对象改变过孔全局可用性：原 update 拒绝时完整重建。
                grid = None
                stats["negotiated_grid_cache_rebuilds"] = stats.get("negotiated_grid_cache_rebuilds", 0) + 1
    if grid is None:
        if enabled:
            x0, y0, x1, y1 = base.board.bounds
            cells = ((math.floor(x1/step)-math.ceil(x0/step)+1)
                     * (math.floor(y1/step)-math.ceil(y0/step)+1) * (len(base.rules.copper_layers)+1))
            resident = sum(v[0].cell_count for v in cache.values())
            while cache and resident + cells > limit:
                _, evicted = cache.popitem(last=False)
                resident -= evicted[0].cell_count
                stats["negotiated_grid_cache_evictions"] = stats.get("negotiated_grid_cache_evictions", 0) + 1
        try:
            grid = OccupancyGrid(model, net, settings, step, max_cells=limit,
                                 deadline=deadline, stats=stats, budget_check=budget.check)
        except OccupancyBudgetError as exc:
            raise routing.RoutingFailure("OCCUPANCY_BUDGET", str(exc), net=net) from exc
    try:
        grid._check()
    except OccupancyBudgetError as exc:
        raise routing.RoutingFailure("OCCUPANCY_BUDGET", str(exc), net=net) from exc
    if enabled:
        # 命中项已从LRU弹出；即使调用方调整总预算，也须先腾出真实驻留容量。
        resident = sum(v[0].cell_count for v in cache.values())
        while cache and resident + grid.cell_count > limit:
            _, evicted = cache.popitem(last=False)
            resident -= evicted[0].cell_count
            stats["negotiated_grid_cache_evictions"] = stats.get("negotiated_grid_cache_evictions", 0) + 1
        cache[key] = grid, records
        stats["negotiated_grid_cache_cells"] = resident + grid.cell_count
    # 基础数组仅由串行的缓存更新修改；每次精检拒绝集合独立，不能污染另一轮。
    view = copy.copy(grid)
    view.rejected_cells, view.rejected_vias = set(), set()
    view.block_cells(blocked[0])
    view.block_cells(blocked[1], via=True)
    return view


def _direction(a, b):
    """输入两个平面点，返回单位方向或零长度的 None；不修改坐标。"""
    dx, dy = b[0] - a[0], b[1] - a[1]
    length = math.hypot(dx, dy)
    return (dx / length, dy / length) if length > 1e-12 else None


def _turn_cost(before, after, penalty):
    """输入前后方向与非负代价，返回一次方向变化的罚分；换层重置方向。"""
    if before is None or after is None:
        return 0.0
    same = (abs(before[0]*after[1]-before[1]*after[0]) < 1e-10
            and before[0]*after[0]+before[1]*after[1] > 0)
    return 0.0 if same else penalty


def _search(model, net, source, target, settings, options, stats, budget, resources, present, blocked):
    """在固定障碍和协商资源代价上执行有界 A*，返回候选铜。

    入参包含完整真实端口、网络规则、共享资源及本网络精检拒绝格。
    出参为(traces,vias)；不会写原板。时间、节点、栅格耗尽或无候选时
    抛 RoutingFailure，端口与路径可行都不代替最终 _commit。
    """
    started = time.monotonic()
    try:
        return _search_impl(model, net, source, target, settings, options, stats, budget, resources, present, blocked)
    finally:
        stats["search_seconds"] = stats.get("search_seconds", 0.0) + time.monotonic() - started


def _search_impl(model, net, source, target, settings, options, stats, budget, resources, present, blocked):
    """执行一次候选搜索；外围负责记录失败时耗时，返回值与异常同 _search。"""
    # 非网格捷径仅使用没有当前/历史竞争的资源；发生竞争后仍交给原协商代价搜索。
    # 原精检拒绝过的网络不重复提出同一几何捷径。
    if (options.get("geometry_search", {}).get("enabled") and not any(blocked)
            and not options.get("negotiated_bend_penalty", 0)):
        from pcb_geometric_router import geometry_candidate
        try:
            candidate = geometry_candidate(model, net, source, target, settings, options, stats, budget)
        except routing.RoutingFailure as exc:
            if exc.diagnostics["code"] != "GEOMETRY_BUDGET":
                raise
            stats["geometry_budget_fallbacks"] = stats.get("geometry_budget_fallbacks", 0) + 1
            candidate = None
        if candidate is not None:
            cells = resources.footprint(net, *candidate)
            if not any(resources.counts.get(cell, 0) or resources.history.get(cell, 0) for cell in cells):
                stats["negotiated_geometry_candidates"] = stats.get("negotiated_geometry_candidates", 0) + 1
                return candidate
    step, alpha = options["grid_step"], settings["alpha"]
    grid = _grid_for_search(model, net, settings, options, stats, budget, resources, blocked)
    starts = _port_paths(model, net, source, settings, options, grid, budget)
    goals = _port_paths(model, net, target, settings, options, grid, budget)
    starts = {n: p for n, p in starts.items() if grid.trace_free(n[2], n[0], n[1])}
    goals = {n: p for n, p in goals.items() if grid.trace_free(n[2], n[0], n[1])}
    if not starts or not goals:
        raise routing.RoutingFailure("NEGOTIATED_PORTS", "有限端口候选内未找到可用接入点，未证明无解",
                                     net=net, source_ports=len(starts), target_ports=len(goals))
    stats["searches"] = stats.get("searches", 0) + 1
    astar_started = time.monotonic()
    goal_x, goal_y = [n[0] for n in goals], [n[1] for n in goals]
    box = min(goal_x), min(goal_y), max(goal_x), max(goal_y)
    directions = [(1, 0), (0, 1), (-1, 0), (0, -1)]
    if options["diagonal"]:
        directions += [(1, 1), (-1, 1), (-1, -1), (1, -1)]
    stack = tuple(model.rules.copper_layers)
    allow_vias = settings["vias"] and set(stack).issubset(settings["via_layers"])
    serial, queue, costs, previous, origins = itertools.count(), [], {}, {}, {}
    goal_costs, footprints, expanded, found, best = {}, {}, 0, None, math.inf
    bend = options.get("negotiated_bend_penalty", 0.0)
    def state_node(state):
        return state[0] if bend else state
    def state_direction(state):
        return state[1] if bend else None
    def state_for(node, direction):
        return (node, direction) if bend else node

    def heuristic(node):
        dx, dy = max(box[0] - node[0], 0, node[0] - box[2]), max(box[1] - node[1], 0, node[1] - box[3])
        length = (max(dx, dy) + (math.sqrt(2) - 1) * min(dx, dy)) if options["diagonal"] else dx + dy
        return length * step * alpha

    def path_cost(layer, path, incoming=None):
        value, direction = 0.0, incoming
        for a, b in zip(path, path[1:]):
            trace = routing._trace(net, layer, a, b, settings["width"])
            cells = resources.footprint(net, [trace], [])
            base = math.dist(a, b) * alpha * _preferred_cost(layer, b[0] - a[0], b[1] - a[1], options)
            after = _direction(a, b)
            value += resources.cost(base, cells, present) + _turn_cost(direction, after, bend)
            if not math.isfinite(value):
                raise routing.RoutingFailure("NEGOTIATED_COST_BUDGET", "端部转弯代价超出有限数值范围，未找到候选")
            if after is not None:
                direction = after
        return value, direction

    try:
        for node, (path, _) in starts.items():
            budget.check()
            distance, direction = path_cost(node[2], path)
            state = state_for(node, direction)
            costs[state], origins[state] = distance, path
            heapq.heappush(queue, (distance + heuristic(node), distance, next(serial), state))
        while queue:
            budget.check()
            estimate, cost, _, state = heapq.heappop(queue)
            node = state_node(state)
            if cost != costs.get(state):
                continue
            if estimate >= best:
                break
            if node in goals:
                if state not in goal_costs:
                    final_path = list(reversed(goals[node][0])) if bend else goals[node][0]
                    goal_costs[state] = path_cost(node[2], final_path, state_direction(state))[0]
                value = cost + goal_costs[state]
                if value < best:
                    found, best = state, value
            expanded += 1
            stats["expansions"] = stats.get("expansions", 0) + 1
            if expanded > options["max_expansions"]:
                raise routing.RoutingFailure("SEARCH_BUDGET", "协商 A* 节点预算内未找到候选，未证明无解",
                                             net=net, expansions=expanded, max_expansions=options["max_expansions"])
            x, y, layer = node
            neighbors = []
            for dx, dy in directions:
                target_node = x + dx, y + dy, layer
                if grid.trace_free(layer, x + dx, y + dy):
                    key = (layer, min((x, y), (x + dx, y + dy)), max((x, y), (x + dx, y + dy)))
                    if key not in footprints:
                        trace = routing._trace(net, layer, (x * step, y * step), ((x + dx) * step, (y + dy) * step), settings["width"])
                        footprints[key] = resources.footprint(net, [trace], [])
                    base = math.hypot(dx, dy) * step * alpha * _preferred_cost(layer, dx, dy, options)
                    neighbors.append((target_node, resources.cost(base, footprints[key], present), False))
            if allow_vias and grid.via_free(x, y):
                key = ("via", x, y)
                if key not in footprints:
                    via = dict(net=net, x=x * step, y=y * step, hole=settings["hole"], diameter=settings["diameter"], layers=list(stack))
                    footprints[key] = resources.footprint(net, [], [via])
                base = settings["via_cost"] + routing._via_preference_cost(model, x * step, y * step, settings, stack)
                increment = resources.cost(base, footprints[key], present)
                neighbors.extend(((x, y, other), increment, True) for other in settings["layers"]
                                 if other != layer and grid.trace_free(other, x, y))
            for other, increment, is_via in neighbors:
                direction = None if is_via else _direction(node, other)
                target_state = state_for(other, direction)
                value = cost + increment + _turn_cost(state_direction(state), direction, bend)
                if not math.isfinite(value):
                    raise routing.RoutingFailure("NEGOTIATED_COST_BUDGET", "转弯路径代价超出有限数值范围，未找到候选")
                if value < costs.get(target_state, math.inf) - 1e-12:
                    costs[target_state] = value
                    previous[target_state] = state, is_via
                    origins[target_state] = origins[state]
                    heapq.heappush(queue, (value + heuristic(other), value, next(serial), target_state))
        if found is None:
            raise routing.RoutingFailure("NEGOTIATED_NO_PATH", "本轮有限协商栅格内未找到路径，未证明无解", net=net, expansions=expanded)
        nodes, spans = [found], []
        while nodes[-1] in previous:
            parent, is_via = previous[nodes[-1]]
            nodes.append(parent)
            spans.append(is_via)
        nodes.reverse()
        spans.reverse()
        nodes = [state_node(state) for state in nodes]
        traces, vias = [], {}
        for a, b in zip(origins[found], origins[found][1:]):
            traces.append(routing._trace(net, nodes[0][2], a, b, settings["width"]))
        for a, b, is_via in zip(nodes, nodes[1:], spans):
            if is_via:
                vias[a[:2]] = dict(net=net, x=a[0] * step, y=a[1] * step, hole=settings["hole"],
                                  diameter=settings["diameter"], layers=list(stack))
            else:
                traces.append(routing._trace(net, a[2], (a[0] * step, a[1] * step), (b[0] * step, b[1] * step), settings["width"]))
        final_node = state_node(found)
        final = list(reversed(goals[final_node][0]))
        for a, b in zip(final, final[1:]):
            traces.append(routing._trace(net, final_node[2], a, b, settings["width"]))
        stats["negotiated_last_path_cost"] = best
        return _merge_collinear(traces), list(vias.values())
    finally:
        stats["astar_seconds"] = stats.get("astar_seconds", 0.0) + time.monotonic() - astar_started


def _representative(group, step):
    layer = min(group["layers"])
    point = group["layers"][layer].representative_point()
    return (round(point.x / step) * step, round(point.y / step) * step)


def _resolve_group(original, groups):
    """在当前同网实铜中找原岛所属连通组；失败保留原岛，绝不伪造连接。"""
    for group in groups:
        if original["terminals"] and original["terminals"] <= group["terminals"]:
            return group
        if not original["terminals"] and any(layer in group["layers"] and shape.intersects(group["layers"][layer])
                                             for layer, shape in original["layers"].items()):
            return group
    return original


def _route_candidate(board, net, settings, options, stats, budget, resources, present, blocked, topology, ordered=None):
    """按拓扑逐边生成一网候选，原铜可在任意采样接入点并入树，返回新增铜列表。"""
    work = copy.deepcopy(board)
    counts = {kind: len(work["routes"].get(kind, [])) for kind in ("traces", "vias")}
    base_groups = routing._groups(work, net, budget)
    if len(base_groups) <= 1:
        return [], []
    if not settings["layers"]:
        raise routing.RoutingFailure("NEGOTIATED_LAYERS", "网络与走线层约束的交集为空，未找到新增布线候选",
                                     net=net, trace_layers=list(options.get("trace_layers", [])),
                                     allowed_layers=list(settings["via_layers"]), unfinished_nets=[net])
    if ordered:
        # 明确链式拓扑不经过 Steiner 改写，也不从其他同网分支偷接捷径。
        from pcb_pipeline_common import copper_and_drills
        copper, _ = copper_and_drills(work, net_filter=net)
        vertices = []
        for pin in ordered:
            key, layers = (pin["compId"], pin["padId"]), {}
            for item in copper:
                if item["terminal"] == key:
                    for layer in item["layers"]:
                        layers.setdefault(layer, []).append(item["inner"])
            vertices.append(dict(layers={layer: unary_union(parts) for layer, parts in layers.items()}, terminals={key}))
        edges = list(zip(range(len(vertices)), range(1, len(vertices))))
    else:
        vertices = list(base_groups)
        layer = max(settings["layers"], key=lambda candidate: sum(candidate in g["layers"] for g in base_groups))
        vertices.extend(dict(steiner=(point[0], point[1], layer), layers={}, terminals=set())
                        for point in topology["steiner_points"])
        edges = topology["edges"]
    for a, b in edges:
        budget.check()
        groups = routing._groups(work, net, budget)
        source, target = vertices[a], vertices[b]
        if not ordered:
            if "steiner" not in source:
                source = _resolve_group(source, groups)
            if "steiner" not in target:
                target = _resolve_group(target, groups)
            if source is target:
                continue
        elif any(source["terminals"] | target["terminals"] <= g["terminals"] for g in groups):
            continue
        model = routing._ChainCollision(work, net, source, target) if ordered else CollisionModel(work)
        budget.context = dict(net=net, phase="negotiated_edge", topology_edge=[a, b])
        traces, vias = _search(model, net, source, target, settings, options, stats, budget, resources, present, blocked)
        work["routes"].setdefault("traces", []).extend(traces)
        work["routes"].setdefault("vias", []).extend(vias)
    # 漂浮既有铜必须接入真实端子；链式拓扑随后仍由原独立检查器拒绝旁路。
    for _ in range(len(base_groups)):
        groups = routing._groups(work, net, budget)
        if len(groups) <= 1:
            break
        source = next((g for g in groups if g["terminals"]), groups[0])
        targets = [g for g in groups if g is not source]
        target = min(targets, key=lambda g: min(a.distance(b) for a in source["layers"].values() for b in g["layers"].values()))
        model = CollisionModel(work)
        traces, vias = _search(model, net, source, target, settings, options, stats, budget, resources, present, blocked)
        work["routes"].setdefault("traces", []).extend(traces)
        work["routes"].setdefault("vias", []).extend(vias)
    return (work["routes"].get("traces", [])[counts["traces"]:], work["routes"].get("vias", [])[counts["vias"]:])


def _refine(rejections, traces, vias, step, blocked, budget, max_cells):
    """将精检拒绝铜的中心线采样为重搜禁点，返回新增数量，不改真实设计规则。"""
    before = len(blocked[0]) + len(blocked[1])
    for rejection in rejections:
        budget.check()
        if rejection["kind"] == "via":
            via = rejection["item"]
            blocked[1].add((round(via["x"] / step), round(via["y"] / step)))
        elif rejection["kind"] == "trace":
            trace = rejection["item"]
            count = max(1, math.ceil(math.dist((trace["sx"], trace["sy"]), (trace["ex"], trace["ey"])) / step))
            if count + 1 > max_cells:
                raise routing.RoutingFailure("OCCUPANCY_BUDGET", "精检冲突格采样预算内未找到候选，未证明无解",
                                             samples=count + 1, max_cells=max_cells)
            for i in range(count + 1):
                if i % 256 == 0:
                    budget.check()
                t = i / count
                blocked[0].add((round((trace["sx"] + (trace["ex"] - trace["sx"]) * t) / step),
                                round((trace["sy"] + (trace["ey"] - trace["sy"]) * t) / step), trace["layer"]))
    if len(blocked[0]) + len(blocked[1]) == before and traces:
        trace = max(traces, key=lambda t: math.hypot(t["ex"] - t["sx"], t["ey"] - t["sy"]))
        blocked[0].add((round((trace["sx"] + trace["ex"]) / (2 * step)),
                        round((trace["sy"] + trace["ey"]) / (2 * step)), trace["layer"]))
    return len(blocked[0]) + len(blocked[1]) - before


def route_negotiated(board, nets, options, stats, budget, topology_orders=None):
    """对完整固定板的一批网络进行有预算的 PathFinder 拆线重布。

    入参：完整板、待布网络顺序、已校验路由选项、共享统计与原预算对象；
    topology_orders 为显式链式端子顺序。出参：逐网精检通过的完整候选副本。
    异常：轮数、时间、节点、栅格、精检或拓扑失败抛 RoutingFailure；
    输入语义异常抛 PipelineError。失败不返回共享铜，不删原铜、不隐藏网络。
    """
    topology_orders = topology_orders or {}
    fixed = copy.deepcopy(board)
    settings = {net: routing._net_options(fixed, net, options) for net in nets}
    model = CollisionModel(fixed)
    resources = _Resources(model, settings, options["grid_step"], options.get("occupancy_max_cells", 2_000_000), budget)
    batch = dict(nets=list(nets), rounds=[], topologies={}, converged=False, exact_rejections=0)
    stats.setdefault("negotiated_batches", []).append(batch)
    stats.setdefault("negotiated_iterations", 0)
    stats["occupancy_grid"] = True
    blocked, rejections_by_net, candidates = {net: (set(), set()) for net in nets}, Counter(), {}
    for net in nets:
        budget.enter(net)
        try:
            groups = routing._groups(fixed, net, budget)
            if not groups:
                raise PipelineError(f"协商网络 {net} 缺少真实铜端子")
            if net in topology_orders:
                batch["topologies"][net] = dict(method="ordered", steiner_points=[], edges=[])
            else:
                result = rectilinear_topology([_representative(g, options["grid_step"]) for g in groups],
                                              max_candidates=options.get("steiner_max_candidates", 128),
                                              max_rounds=options.get("steiner_max_rounds", 2), budget=budget)
                batch["topologies"][net] = dict(method="rectilinear_mst_1steiner", **result)
        finally:
            budget.leave()
    last_failure = None
    for iteration in range(1, options.get("negotiated_max_iterations", 20) + 1):
        budget.check()
        stats["negotiated_iterations"] += 1
        try:
            present = options.get("negotiated_present_factor", 1.0) * options.get("negotiated_present_growth", 1.3) ** (iteration - 1)
        except OverflowError as exc:
            raise routing.RoutingFailure("NEGOTIATED_COST_BUDGET", "协商代价超出有限数值范围，未找到候选，未证明无解") from exc
        if not math.isfinite(present):
            raise routing.RoutingFailure("NEGOTIATED_COST_BUDGET", "协商代价超出有限数值范围，未找到候选，未证明无解")
        failed = {}
        for net in nets:
            budget.enter(net)
            try:
                resources.replace(net, set())
                candidate = _route_candidate(fixed, net, settings[net], options, stats, budget, resources,
                                             present, blocked[net], batch["topologies"][net], topology_orders.get(net))
                candidates[net] = candidate
                resources.replace(net, resources.footprint(net, *candidate))
            except routing.RoutingFailure as exc:
                if exc.diagnostics["code"] in ("TIME_BUDGET", "OCCUPANCY_BUDGET", "SEARCH_BUDGET"):
                    raise
                failed[net] = dict(exc.diagnostics)
                last_failure = exc
                # 保留上一轮候选的资源，不能让搜索失败掩盖待完成网络。
                if net in candidates:
                    resources.replace(net, resources.footprint(net, *candidates[net]))
            finally:
                budget.leave()
        shared, conflicts = resources.conflicts()
        increment = options.get("negotiated_history_increment", 1.0)
        # 先在副本中验证有限值，拒绝后不能留下无法序列化的统计。
        next_history = dict(resources.history)
        for cell in shared:
            next_history[cell] = resources.history.get(cell, 0.0) + increment * (resources.counts[cell] - 1)
        history_total = sum(next_history.values())
        if not math.isfinite(history_total):
            raise routing.RoutingFailure("NEGOTIATED_COST_BUDGET", "历史拥塞代价超出有限数值范围，未找到候选，未证明无解")
        resources.history = next_history
        round_stats = dict(iteration=iteration, shared_resources=len(shared), conflicting_nets=conflicts,
                           failed_nets=sorted(failed), present_factor=present,
                           history_total=history_total, history_cells=len(resources.history))
        batch["rounds"].append(round_stats)
        if shared or failed:
            continue
        accepted, exact_failed = copy.deepcopy(fixed), False
        for net in nets:
            budget.enter(net)
            try:
                before = len(routing._groups(accepted, net, budget))
                if before <= 1:
                    continue
                traces, vias = candidates[net]
                diagnostics = []
                candidate = routing._commit(accepted, net, traces, vias, before, budget, rejections=diagnostics)
                if candidate is None:
                    rejections_by_net[net] += 1
                    batch["exact_rejections"] += 1
                    stats["occupancy_rejections"] = stats.get("occupancy_rejections", 0) + 1
                    round_stats.setdefault("exact_failed_nets", []).append(net)
                    if rejections_by_net[net] > options.get("occupancy_retries", 3):
                        raise routing.RoutingFailure("GRID_REFINEMENT_BUDGET", "协商精检重搜预算内未找到合法候选，未证明无解",
                                                     net=net, occupancy_retries=options.get("occupancy_retries", 3))
                    count = _refine(diagnostics, traces, vias, options["grid_step"], blocked[net], budget,
                                    options.get("occupancy_max_cells", 2_000_000))
                    stats["occupancy_refined_cells"] = stats.get("occupancy_refined_cells", 0) + count
                    stats["occupancy_retries"] = stats.get("occupancy_retries", 0) + 1
                    exact_failed = True
                    break
                accepted = candidate
            finally:
                budget.leave()
        if exact_failed:
            continue
        from pcb_constraints import topology_errors
        errors = [error for net in nets if net in topology_orders
                  for error in topology_errors(accepted, net, topology_orders[net])]
        # 完整板验收由原 route_board 尾部执行；续算预览仍保持原有作用域。
        for net in nets:
            groups = routing._groups(accepted, net, budget)
            declared = {(pin["compId"], pin["padId"]) for pin in accepted["nets"][net]}
            found = set().union(*(g["terminals"] for g in groups)) if groups else set()
            if len(groups) != 1 or found != declared:
                errors.append(f"协商网络 {net} 尚未连通全部真实端子和既有铜岛")
        budget.check()
        if errors:
            raise routing.RoutingFailure("GEOMETRY_HARD_FAILURE", "协商候选未通过原独立验收器", errors=errors[:8])
        batch["converged"] = True
        stats["connections"] = stats.get("connections", 0) + sum(max(0, len(routing._groups(fixed, net, budget)) - 1) for net in nets)
        return accepted
    raise routing.RoutingFailure("NEGOTIATED_BUDGET", "协商轮数预算内未找到无共享且精检通过的候选，未证明无解",
                                 max_iterations=options.get("negotiated_max_iterations", 20),
                                 shared_resources=batch["rounds"][-1]["shared_resources"] if batch["rounds"] else 0,
                                 unfinished_nets=sorted(set(batch["rounds"][-1]["conflicting_nets"] + batch["rounds"][-1]["failed_nets"]
                                                            + batch["rounds"][-1].get("exact_failed_nets", []))) if batch["rounds"] else list(nets),
                                 last_failure=getattr(last_failure, "diagnostics", None))
