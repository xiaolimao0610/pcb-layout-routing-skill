"""有界力导向、簇式合法化与模拟退火，只产生经过原检查器复核的布局候选。

固定件、既有铜和原网表保持；任何代理代价都不能放宽原几何及硬约束。
"""
from __future__ import annotations

from copy import deepcopy
import math
import random
import time

import numpy as np
from shapely.geometry import box
from shapely.ops import unary_union

from pcb_pipeline_common import PipelineError, board_polygon, check_board, rules_for, _fast_pad_bounds
from pcb_solver_hardened import _body
from pcb_width_policy import selected_width


NEW_OPTION_KEYS = {
    "global_iterations", "global_step", "sa_temperature", "sa_cooling",
    "sa_steps_per_temperature", "sa_temperatures", "max_seconds",
    "cluster_intra_steps", "cluster_intra_radius", "rudy_grid", "rudy_capacity",
    "rudy_peak_limit", "rudy_overflow_limit", "rudy_method", "rudy_widths", "ground_nets", "ground_weight",
    "net_weights", "cost_weights", "clusters", "edge_components", "align_groups", "feedback",
    "initialization", "functional_groups", "functional_cells",
}
DEFAULT_WEIGHTS = dict(hpwl=1.0, rudy_peak=100.0, rudy_overflow=100.0,
                       decoupling=1.0, crystal=1.0, edge=1.0, orientation=10.0,
                       alignment=10.0, relative_pose=10.0, repel_field=10.0, feedback=10.0)


class PlacementFailure(PipelineError):
    """保留有限布局失败的结构化证据，不将未找到表述为无解。"""
    def __init__(self, code, message, **diagnostics):
        self.diagnostics = dict(code=code, message=message, **diagnostics)
        super().__init__(f"{code}: {message}")


def _finite(value, name, minimum=0.0, positive=False):
    """输入标量及下限，返回有限浮点数；类型、范围或溢出错误抛 PipelineError。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PipelineError(f"placement.{name} 必须为有限数值")
    try:
        result = float(value)
    except (ValueError, OverflowError) as exc:
        raise PipelineError(f"placement.{name} 必须为有限数值") from exc
    if not math.isfinite(result) or result < minimum or (positive and result == 0):
        raise PipelineError(f"placement.{name} 超出有限数值范围")
    return result


def _fields(value, allowed, required, label):
    if not isinstance(value, dict) or set(value) - set(allowed) or set(required) - set(value):
        raise PipelineError(f"placement.{label} 含未知字段、缺少必需字段或不是对象")


def validate_options(options, board):
    """规范化阶段3配置，不改变输入板或原配置。

    入参：原始或已规范化布局配置、完整板。出参：含默认值的新配置；
    基础布局字段由 pcb_placement._options 继续校验。本函数严格检查新增
    字段及其嵌套结构、器件/焊盘/网络引用；非法输入抛 PipelineError。
    """
    if not isinstance(options, dict):
        raise PipelineError("placement 选项必须为对象")
    from pcb_placement import _OPTION_KEYS
    if set(options) - (_OPTION_KEYS | NEW_OPTION_KEYS):
        raise PipelineError("placement 含未知字段")
    result = deepcopy(options)
    if "grid_step" in result:
        result["grid_step"] = _finite(result["grid_step"], "grid_step", positive=True)
    for key, default, maximum in (("global_iterations", 40, 10000),
                                   ("sa_steps_per_temperature", 30, 100000),
                                   ("sa_temperatures", 12, 10000),
                                   ("cluster_intra_steps", 20, 100000)):
        value = result.get(key, default)
        if type(value) is not int or not 0 <= value <= maximum:
            raise PipelineError(f"placement.{key} 必须为 0..{maximum} 的整数")
        result[key] = value
    for key, default, positive in (("global_step", 0.25, True), ("sa_temperature", 10.0, True),
                                    ("sa_cooling", 0.85, True), ("max_seconds", 30.0, True),
                                    ("cluster_intra_radius", 10.0, False), ("rudy_capacity", 1.0, True),
                                    ("rudy_peak_limit", 1.0, False), ("rudy_overflow_limit", 0.0, False),
                                    ("ground_weight", 0.05, False)):
        result[key] = _finite(result.get(key, default), key, positive=positive)
    if result["global_step"] > 1 or result["sa_cooling"] >= 1:
        raise PipelineError("global_step 必须不大于1，sa_cooling 必须小于1")
    grid = result.get("rudy_grid", [12, 12])
    if (not isinstance(grid, (list, tuple)) or len(grid) != 2
            or any(type(v) is not int or not 1 <= v <= 256 for v in grid)
            or grid[0] * grid[1] > 65536):
        raise PipelineError("rudy_grid 必须为两个 1..256 的整数，最多65536格")
    result["rudy_grid"] = list(grid)
    nets = set(board["nets"])
    result["rudy_method"] = result.get("rudy_method", "legacy")
    if result["rudy_method"] not in ("legacy", "vectorized"):
        raise PipelineError("rudy_method必须为legacy或vectorized")
    widths = result.get("rudy_widths", {})
    if not isinstance(widths, dict) or set(widths)-nets:
        raise PipelineError("rudy_widths只能引用真实网络")
    widths = {net: _finite(width, "rudy_widths", positive=True) for net, width in widths.items()}
    from pcb_differential import declarations
    pairs, _ = declarations(board.get("metadata", {}).get("pipeline_constraints", []))
    pair_widths = {net: pair["width"] for pair in pairs for net in (pair["p"], pair["n"])}
    if any(net in widths and abs(_finite(widths[net], "rudy_widths", positive=True)-width) > rules_for(board).contact_tolerance
           for net, width in pair_widths.items()):
        raise PipelineError("rudy_widths与差分声明宽度冲突")
    result["rudy_widths"] = {net: selected_width(board, net, widths.get(net, pair_widths.get(net))) for net in nets}
    grounds = result.get("ground_nets", [])
    if (not isinstance(grounds, (list, tuple)) or any(not isinstance(n, str) for n in grounds)
            or len(grounds) != len(set(grounds)) or set(grounds) - nets):
        raise PipelineError("ground_nets 必须为不重复的真实网络列表")
    result["ground_nets"] = list(grounds)
    weights = result.get("net_weights", {})
    if not isinstance(weights, dict) or set(weights) - nets:
        raise PipelineError("net_weights 只能引用真实网络")
    result["net_weights"] = {net: _finite(weight, f"net_weights.{net}") for net, weight in weights.items()}
    costs = result.get("cost_weights", {})
    if not isinstance(costs, dict) or set(costs) - set(DEFAULT_WEIGHTS):
        raise PipelineError("cost_weights 含未知代价名称或不是对象")
    result["cost_weights"] = {key: _finite(costs.get(key, default), f"cost_weights.{key}")
                              for key, default in DEFAULT_WEIGHTS.items()}
    components = {c["id"]: c for c in board["components"]}
    pads = {(c["id"], p["padId"]) for c in board["components"] for p in c["pads"]}

    def members(value, label, minimum=1):
        if (not isinstance(value, list) or len(value) < minimum
                or any(not isinstance(cid, str) for cid in value)
                or len(set(value)) != len(value) or set(value) - set(components)):
            raise PipelineError(f"{label} 必须引用不重复的真实器件")
        return list(value)

    def pin(value, label):
        _fields(value, {"compId", "padId"}, {"compId", "padId"}, label)
        if any(not isinstance(v, str) for v in value.values()) or (value["compId"], value["padId"]) not in pads:
            raise PipelineError(f"{label} 必须引用真实焊盘")

    initialization = result.get("initialization", "preserve")
    if initialization not in ("preserve", "auto", "region_pack"):
        raise PipelineError("placement.initialization 必须为 preserve/auto/region_pack")
    result["initialization"] = initialization
    groups, group_ids, grouped = result.get("functional_groups", []), set(), set()
    if not isinstance(groups, list):
        raise PipelineError("functional_groups 必须为列表")
    regions = dict(result.get("regions", {}))
    for group in groups:
        _fields(group, {"id", "components", "region"}, {"id", "components", "region"}, "functional_groups")
        if not isinstance(group["id"], str) or not group["id"] or group["id"] in group_ids:
            raise PipelineError("functional_group.id 必须是唯一非空字符串")
        group_ids.add(group["id"])
        ids_in_group = members(group["components"], "functional_group.components")
        if grouped & set(ids_in_group):
            raise PipelineError("一个器件不能重复属于多个功能分区")
        grouped.update(ids_in_group)
        rect = group["region"]
        if not isinstance(rect, (list, tuple)) or len(rect) != 4:
            raise PipelineError("functional_group.region 必须为四元素矩形")
        rect = [_finite(value, "functional_group.region", minimum=-math.inf) for value in rect]
        if rect[0] >= rect[2] or rect[1] >= rect[3]:
            raise PipelineError("functional_group.region 必须具有正面积")
        group["region"] = rect
        region = box(*rect)
        for cid in ids_in_group:
            previous = regions.get(cid)
            if previous is not None:
                previous = box(*previous) if isinstance(previous, (list, tuple)) else previous
                effective = previous.intersection(region)
            else:
                effective = region
            if effective.is_empty or effective.area <= 0:
                raise PipelineError(f"functional_group 与 placement.regions 无有效交集: {cid}")
            regions[cid] = effective
    result["regions"] = regions
    result["functional_groups"] = groups

    clusters, seen, ids = result.get("clusters", []), set(), set()
    if not isinstance(clusters, list):
        raise PipelineError("clusters 必须为列表")
    for cluster in clusters:
        _fields(cluster, {"id", "components", "relations"}, {"id", "components"}, "clusters")
        if not isinstance(cluster["id"], str) or not cluster["id"] or cluster["id"] in ids:
            raise PipelineError("cluster.id 必须是唯一非空字符串")
        ids.add(cluster["id"])
        group = members(cluster["components"], "cluster.components")
        if seen & set(group):
            raise PipelineError("一个器件不能重复属于多个刚体簇")
        seen.update(group)
        relations = cluster.setdefault("relations", [])
        if not isinstance(relations, list):
            raise PipelineError("cluster.relations 必须为列表")
        for relation in relations:
            _fields(relation, {"kind", "source", "target", "weight"}, {"kind", "source", "target"}, "cluster.relations")
            if relation["kind"] not in ("decoupling", "crystal"):
                raise PipelineError("cluster.relations.kind 只能为 decoupling 或 crystal")
            for key in ("source", "target"):
                pin(relation[key], "cluster.relations." + key)
                if relation[key]["compId"] not in group:
                    raise PipelineError("去耦或晶振关系的两端必须属于该显式簇")
            if relation["source"] == relation["target"]:
                raise PipelineError("关系必须有两个不同焊盘")
            relation["weight"] = _finite(relation.get("weight", 1.0), "relation.weight")
    result["clusters"] = clusters
    from pcb_functional_cells import validate_cells, cell_members
    result["functional_cells"] = validate_cells(result.get("functional_cells", []), board)
    if any(set(cell_members(cell)) & seen for cell in result["functional_cells"]):
        raise PipelineError("functional_cells 不能与 clusters 重复占用器件；请明确一个刚体来源")
    edges, edge_ids = result.get("edge_components", []), set()
    if not isinstance(edges, list):
        raise PipelineError("edge_components 必须为列表")
    for edge in edges:
        _fields(edge, {"component", "edge", "target_distance", "weight"}, {"component", "edge"}, "edge_components")
        if not isinstance(edge["component"], str) or edge["component"] not in components or edge["component"] in edge_ids:
            raise PipelineError("edge_components 必须引用不重复的真实器件")
        edge_ids.add(edge["component"])
        if edge["edge"] not in ("left", "right", "top", "bottom", "nearest"):
            raise PipelineError("edge_components.edge 必须为 left/right/top/bottom/nearest")
        edge["target_distance"] = _finite(edge.get("target_distance", 0), "edge.target_distance")
        edge["weight"] = _finite(edge.get("weight", 1), "edge.weight")
    result["edge_components"] = edges
    align, ids = result.get("align_groups", []), set()
    if not isinstance(align, list):
        raise PipelineError("align_groups 必须为列表")
    for group in align:
        _fields(group, {"id", "components", "axis", "orientation", "weight"}, {"id", "components", "axis"}, "align_groups")
        if not isinstance(group["id"], str) or not group["id"] or group["id"] in ids:
            raise PipelineError("align_groups.id 必须是唯一非空字符串")
        ids.add(group["id"])
        members(group["components"], "align_groups.components", 2)
        if group["axis"] not in ("x", "y", "both"):
            raise PipelineError("align_groups.axis 必须为 x/y/both")
        if "orientation" in group:
            group["orientation"] = _finite(group["orientation"], "align.orientation", minimum=-math.inf) % 360
        group["weight"] = _finite(group.get("weight", 1), "align.weight")
    result["align_groups"] = align
    feedback = result.get("feedback", {})
    _fields(feedback, {"failed_nets", "hotspots"}, set(), "feedback")
    failed = feedback.setdefault("failed_nets", [])
    if (not isinstance(failed, list) or any(not isinstance(n, str) for n in failed)
            or len(failed) != len(set(failed)) or set(failed) - nets):
        raise PipelineError("feedback.failed_nets 必须为不重复的真实网络列表")
    hotspots = feedback.setdefault("hotspots", [])
    if not isinstance(hotspots, list):
        raise PipelineError("feedback.hotspots 必须为列表")
    if len(hotspots) > 256:
        raise PipelineError("feedback.hotspots 最多256个区域")
    for hotspot in hotspots:
        _fields(hotspot, {"bounds", "weight", "nets"}, {"bounds"}, "feedback.hotspots")
        bounds = hotspot["bounds"]
        if not isinstance(bounds, (list, tuple)) or len(bounds) != 4:
            raise PipelineError("hotspot.bounds 必须为四元素边界")
        hotspot["bounds"] = bounds = [_finite(v, "hotspot.bounds", minimum=-math.inf) for v in bounds]
        if bounds[0] >= bounds[2] or bounds[1] >= bounds[3]:
            raise PipelineError("hotspot.bounds 必须具有正面积")
        hotspot["weight"] = _finite(hotspot.get("weight", 1), "hotspot.weight")
        names = hotspot.setdefault("nets", [])
        if (not isinstance(names, list) or any(not isinstance(n, str) for n in names)
                or len(names) != len(set(names)) or set(names) - nets):
            raise PipelineError("hotspot.nets 必须引用不重复的真实网络")
    result["feedback"] = feedback
    return result


def net_weight(board, net, options=None):
    """返回显式网络权重，不根据网名猜测地网。

    入参为完整板、网络名和配置。net_weights 显式覆盖优先；否则采用
    net_tags.alpha_length，并对 ground_nets 或 net_tags.type=GND 的声明
    乘 ground_weight。返回非负有限数；无效标签或配置抛 PipelineError。
    """
    if not net:
        return 0.0
    options = options or {}
    if net in options.get("net_weights", {}):
        return _finite(options["net_weights"][net], f"net_weights.{net}")
    tag = (board or {}).get("tags", {}).get("net_tags", {}).get(net, {})
    weight = _finite(tag.get("alpha_length", 1), f"net_tags.{net}.alpha_length")
    if net in options.get("ground_nets", []) or tag.get("type") == "GND":
        weight *= _finite(options.get("ground_weight", 0.05), "ground_weight")
    if not math.isfinite(weight):
        raise PipelineError("网络权重乘积超出有限范围")
    return weight


def _pins(board):
    tolerance = rules_for(board).curve_tolerance
    return {(c["id"], p["padId"]): _fast_pad_bounds(c, p, tolerance).inner.centroid
            for c in board["components"] for p in c["pads"]}


def _rudy(board, options, pins, budget=None):
    """输入板、规范化选项、真实引脚坐标和预算，返回RUDY报告、框与HPWL。

    legacy保留逐格几何相交，vectorized用等价矩形重叠面积的NumPy广播；
    两者采用相同实际选宽和有限格数。无效数值或预算耗尽抛PlacementFailure。
    """
    polygon, rules = board_polygon(board), rules_for(board)
    x0, y0, x1, y1 = polygon.bounds
    nx, ny = options["rudy_grid"]
    dx, dy = (x1-x0)/nx, (y1-y0)/ny
    if not all(math.isfinite(v) and v > 0 for v in (dx, dy, dx*dy)):
        raise PlacementFailure("PLACEMENT_COST_BUDGET", "RUDY 格尺寸超出有限数值范围，未找到可比较候选")
    cells = np.zeros((ny, nx), dtype=float)
    demand_boxes, hpwl = {}, {}
    for net in sorted(board["nets"]):
        if budget is not None:
            budget.check()
        pts = [pins[(p["compId"], p["padId"])] for p in board["nets"][net]]
        if len(pts) < 2:
            hpwl[net] = 0.0
            continue
        xs, ys = [p.x for p in pts], [p.y for p in pts]
        length = max(xs)-min(xs)+max(ys)-min(ys)
        weight = net_weight(board, net, options)
        hpwl[net] = weight * length
        if not length or not weight:
            continue
        width = selected_width(board, net, options.get("rudy_widths", {}).get(net))
        # 零宽网络框有明确物理线宽，不以任意epsilon制造无限需求。
        cx, cy = (min(xs)+max(xs))/2, (min(ys)+max(ys))/2
        w, h = max(width, max(xs)-min(xs)), max(width, max(ys)-min(ys))
        rect = box(cx-w/2, cy-h/2, cx+w/2, cy+h/2)
        density = weight * length * width / (w*h)
        demand_boxes[net] = (rect, density)
        ix0, iy0 = max(0, math.floor((rect.bounds[0]-x0)/dx)), max(0, math.floor((rect.bounds[1]-y0)/dy))
        ix1, iy1 = min(nx-1, math.floor((rect.bounds[2]-x0)/dx)), min(ny-1, math.floor((rect.bounds[3]-y0)/dy))
        if options.get("rudy_method", "legacy") == "vectorized":
            # 覆盖范围最多65536格，广播只创建本网络框内的有限面积矩阵。
            if ix0 <= ix1 and iy0 <= iy1:
                ix = np.arange(ix0, ix1+1, dtype=float)
                iy = np.arange(iy0, iy1+1, dtype=float)
                xx, yy = x0+ix*dx, y0+iy*dy
                left, bottom, right, top = rect.bounds
                # 与旧网格端点公式保持同一运算顺序，避免阈值附近额外舍入偏差。
                ox = np.maximum(0.0, np.minimum(right, x0+(ix+1)*dx)-np.maximum(left, xx))
                oy = np.maximum(0.0, np.minimum(top, y0+(iy+1)*dy)-np.maximum(bottom, yy))
                cells[iy0:iy1+1, ix0:ix1+1] += density * (oy[:, None]*ox[None, :]) / (dx*dy)
        else:
            for iy in range(iy0, iy1+1):
                if budget is not None:
                    budget.check()
                for ix in range(ix0, ix1+1):
                    overlap = rect.intersection(box(x0+ix*dx, y0+iy*dy, x0+(ix+1)*dx, y0+(iy+1)*dy)).area
                    cells[iy, ix] += density * overlap / (dx*dy)
        if budget is not None:
            budget.check()
    cells /= options["rudy_capacity"]
    if not np.isfinite(cells).all() or any(not math.isfinite(v) for v in hpwl.values()):
        raise PlacementFailure("PLACEMENT_COST_BUDGET", "布局代价超出有限数值范围，未找到可比较候选")
    overflow = np.maximum(cells-1, 0)
    peak, excess = float(cells.max(initial=0)), float(overflow.sum())
    indices = sorted(((float(cells[iy, ix]), ix, iy) for iy in range(ny) for ix in range(nx)
                      if cells[iy, ix] > options["rudy_peak_limit"] or overflow[iy, ix] > 0),
                     key=lambda v: (-v[0], v[2], v[1]))[:32]
    hotspots = []
    for demand, ix, iy in indices:
        bounds = [x0+ix*dx, y0+iy*dy, x0+(ix+1)*dx, y0+(iy+1)*dy]
        region = box(*bounds)
        hotspots.append(dict(bounds=bounds, demand=demand, overflow=max(0, demand-1),
                             nets=sorted(net for net, (rect, _) in demand_boxes.items() if rect.intersects(region))))
    if budget is not None:
        budget.check()
    return dict(peak=peak, overflow=excess,
                passed=peak <= options["rudy_peak_limit"] and excess <= options["rudy_overflow_limit"],
                grid=list(options["rudy_grid"]), capacity=options["rudy_capacity"], hotspots=hotspots), demand_boxes, hpwl


def evaluate_cost(board, options=None, *, budget=None):
    """逐项计算布局代理代价，不判断几何合法或制造可用。

    入参：完整规范板、原始或已规范化配置及可选预算。出参：raw、weighted、
    weights、total、rudy、net_hpwl；奖励项为负数，距离单位mil，RUDY无量纲。
    异常：显式引用/配置无效抛 PipelineError，时间或数值预算抛 PlacementFailure。
    """
    options = validate_options(options or {}, board)
    if budget is not None:
        budget.check()
    from pcb_placement import _envelope
    rules, polygon = rules_for(board), board_polygon(board)
    components = {c["id"]: c for c in board["components"]}
    pins = _pins(board)
    raw = {key: 0.0 for key in DEFAULT_WEIGHTS}
    rudy, boxes, net_hpwl = _rudy(board, options, pins, budget)
    raw.update(hpwl=sum(net_hpwl.values()), rudy_peak=rudy["peak"], rudy_overflow=rudy["overflow"])
    for cluster in options["clusters"]:
        for relation in cluster["relations"]:
            a, b = relation["source"], relation["target"]
            distance = pins[(a["compId"], a["padId"])].distance(pins[(b["compId"], b["padId"])])
            raw[relation["kind"]] += relation["weight"] * distance
    x0, y0, x1, y1 = polygon.bounds
    envelopes = {}
    for entry in options["edge_components"]:
        shape = envelopes.setdefault(entry["component"], _envelope(components[entry["component"]], rules.curve_tolerance))
        bx0, by0, bx1, by1 = shape.bounds
        distances = dict(left=abs(bx0-x0), right=abs(x1-bx1), bottom=abs(by0-y0), top=abs(y1-by1),
                         nearest=shape.distance(polygon.boundary))
        raw["edge"] += entry["weight"] * abs(distances[entry["edge"]]-entry["target_distance"])
    scale = options.get("grid_step", rules.grid_step)
    for group in options["align_groups"]:
        values = [components[cid] for cid in group["components"]]
        target_angle = group.get("orientation", values[0]["rotation"])
        axes = ("x", "y") if group["axis"] == "both" else (group["axis"],)
        for value in values:
            difference = ((value["rotation"] - target_angle + 180) % 360) - 180
            raw["orientation"] -= group["weight"] * (1+math.cos(math.radians(difference)))/2
        for axis in axes:
            center = sum(c[axis] for c in values)/len(values)
            raw["alignment"] -= group["weight"] * sum(1/(1+abs(c[axis]-center)/scale) for c in values)
    for rule in board.get("metadata", {}).get("pipeline_constraints", []):
        if rule.get("operator") == "RELATIVE_POSE":
            a, b = rule["source"], rule["target"]
            distance = pins[(a["compId"], a["padId"])].distance(pins[(b["compId"], b["padId"])])
            excess = max(0, distance-rule["max_distance"])
            raw["relative_pose"] += excess*excess
        elif rule.get("operator") == "REPEL_FIELD":
            a, b = components[rule["source"]], components[rule["target"]]
            distance = _body(a, rules.curve_tolerance).outer.distance(_body(b, rules.curve_tolerance).outer)
            excess = max(0, rule["min_distance"]-distance)
            raw["repel_field"] += excess*excess
    raw["feedback"] += sum(net_hpwl.get(net, 0) for net in options["feedback"]["failed_nets"])
    for hotspot in options["feedback"]["hotspots"]:
        region = box(*hotspot["bounds"])
        selected = set(hotspot["nets"])
        demand = sum(rect.intersection(region).area*density/region.area
                     for net, (rect, density) in boxes.items() if not selected or net in selected)
        raw["feedback"] += hotspot["weight"] * demand
    weighted = {key: raw[key]*options["cost_weights"][key] for key in raw}
    total = sum(weighted.values())
    if not math.isfinite(total) or any(not math.isfinite(v) for v in (*raw.values(), *weighted.values())):
        raise PlacementFailure("PLACEMENT_COST_BUDGET", "布局代价超出有限数值范围，未找到可比较候选")
    return dict(raw=raw, weighted=weighted, weights=dict(options["cost_weights"]), total=total,
                rudy=rudy, net_hpwl=net_hpwl)


class _Budget:
    """记录合作式总时限和候选检查次数；没有合法结果时预算异常不可伪装成功。"""
    def __init__(self, options, stats):
        self.started = time.monotonic()
        self.options, self.stats = options, stats
        self.best_candidate, self.best_cost = None, None

    def check(self):
        if time.monotonic()-self.started >= self.options["max_seconds"]:
            raise PlacementFailure("PLACEMENT_TIME_BUDGET", "布局总时限内未找到进一步候选，未证明无解", budget_scope="time")

    def candidate(self):
        self.check()
        if self.stats["candidate_checks"] >= self.options.get("max_candidates", 4000):
            raise PlacementFailure("PLACEMENT_CANDIDATE_BUDGET", "布局候选预算内未找到进一步候选，未证明无解", budget_scope="candidates")
        self.stats["candidate_checks"] += 1


def _units(board, options, immovable):
    """将显式簇视为刚体；包含任一不可动件的簇在宏观阶段整体固定。"""
    used, units = set(), []
    from pcb_functional_cells import cell_members
    for cell in options.get("functional_cells", []):
        ids = cell_members(cell)
        used.update(ids)
        units.append(dict(id="cell:"+cell["id"], members=ids, cluster=True, fixed=bool(set(ids)&immovable)))
    for cluster in options["clusters"]:
        ids = tuple(cluster["components"])
        used.update(ids)
        units.append(dict(id="cluster:"+cluster["id"], members=ids, cluster=True, fixed=bool(set(ids)&immovable)))
    for cid in sorted({c["id"] for c in board["components"]}-used):
        units.append(dict(id="component:"+cid, members=(cid,), cluster=False, fixed=cid in immovable))
    return units


def _center(components, unit):
    return tuple(sum(components[cid][axis] for cid in unit["members"])/len(unit["members"]) for axis in ("x", "y"))


def _translated(components, members, dx, dy):
    out = dict(components)
    for cid in members:
        out[cid] = dict(components[cid], x=components[cid]["x"]+dx, y=components[cid]["y"]+dy)
    return out


def _board_with(board, components):
    """用完整器件集合重建候选，同面刚体移动附属孔，保留完整网络与既有铜。"""
    from pcb_placement import _subset
    return _subset(board, [components[c["id"]] for c in board["components"]])


def _legal(board, components, options, budget, *, partial=False, count=True):
    """快速包络筛选后调用原 check_board；只返回真/假，不放宽硬规则。"""
    from pcb_placement import _subset, _envelope
    if count:
        budget.candidate()
    else:
        budget.check()
    from pcb_functional_cells import check_relations
    if check_relations(dict(board, components=list(components.values())), options.get("functional_cells", []), partial=partial):
        return False
    for cid, component in components.items():
        region = options.get("regions", {}).get(cid)
        if region is not None:
            region = box(*region) if isinstance(region, (list, tuple)) else region
            if not region.covers(_envelope(component, rules_for(board).curve_tolerance)):
                return False
    try:
        trial = _subset(board, list(components.values())) if partial else _board_with(board, components)
        errors = check_board(trial, ignore_connectivity=True)
    except PipelineError:
        return False
    budget.check()
    return not errors


def _global(board, components, units, options, stats, budget):
    """执行有限力导向更新；固定单元为锚，只平移整个簇，输出仍需合法化。"""
    current = dict(components)
    member_unit = {cid: i for i, unit in enumerate(units) for cid in unit["members"]}
    from pcb_initial_place import translation_bounds
    rules = rules_for(board)
    available = board_polygon(board).buffer(-rules.board_edge_clearance)
    for iteration in range(options["global_iterations"]):
        budget.check()
        sums = {i: [0.0, 0.0, 0.0] for i, unit in enumerate(units) if not unit["fixed"]}
        pins = _pins(_board_with(board, current))
        for net in sorted(board["nets"]):
            budget.check()
            weight = net_weight(board, net, options)
            refs = board["nets"][net]
            if not weight or len(refs) < 2:
                continue
            for ref in refs:
                i = member_unit[ref["compId"]]
                if i not in sums:
                    continue
                peers = [pins[(p["compId"], p["padId"])] for p in refs if member_unit[p["compId"]] != i]
                if peers:
                    mine = pins[(ref["compId"], ref["padId"])]
                    w = weight/len(refs)
                    sums[i][0] += w*(sum(p.x for p in peers)/len(peers)-mine.x)
                    sums[i][1] += w*(sum(p.y for p in peers)/len(peers)-mine.y)
                    sums[i][2] += w
        for i, (sx, sy, weight) in sums.items():
            budget.check()
            dx, dy = (sx/weight*options["global_step"], sy/weight*options["global_step"]) if weight else (0.0, 0.0)
            if not math.isfinite(dx) or not math.isfinite(dy):
                raise PlacementFailure("PLACEMENT_COST_BUDGET", "全局作用力超出有限数值范围，未找到进一步候选")
            bounds = translation_bounds(current, units[i], available, options.get("regions", {}), rules.curve_tolerance)
            if bounds is None:
                # 本角度无平移可行域；留给合法化尝试其他允许角度，不拆散刚体。
                continue
            dx = min(bounds[2], max(bounds[0], dx))
            dy = min(bounds[3], max(bounds[1], dy))
            current = _translated(current, units[i]["members"], dx, dy)
        stats["global_iterations"] = iteration+1
    return current


def _rotation_deltas(components, unit, options):
    """输入刚体及允许绝对角度，返回不改变簇内关系的可用角度增量。"""
    anchor = components[unit["members"][0]]["rotation"]
    allowed = options.get("rotations", [0, 90, 180, 270])
    deltas = []
    for angle in allowed:
        delta = (angle-anchor) % 360
        if min(delta, 360-delta) <= 1e-9:
            continue
        if all(any(abs(((components[cid]["rotation"]+delta-target+180) % 360)-180) < 1e-8
                   for target in allowed) for cid in unit["members"]):
            if not any(abs(((delta-old+180) % 360)-180) < 1e-8 for old in deltas):
                deltas.append(delta)
    return deltas


def _rotated(components, unit, delta):
    """输入器件映射、刚体与角度增量，返回围绕簇中心同面旋转的新映射。"""
    cx, cy = _center(components, unit)
    cosine, sine = math.cos(math.radians(delta)), math.sin(math.radians(delta))
    candidate = dict(components)
    for cid in unit["members"]:
        item = components[cid]
        x, y = item["x"]-cx, item["y"]-cy
        candidate[cid] = dict(item, x=cx+x*cosine-y*sine, y=cy+x*sine+y*cosine,
                              rotation=(item["rotation"]+delta) % 360)
    return candidate


def _legalize(board, components, units, options, stats, budget):
    """输入完整板和刚体候选，返回区域内合法姿态；预算与几何失败沿原异常上抛。

    复用分区空隙候选，偏好当前全局位置；所有成员各自的区域同时约束刚体。
    最终仍调用原 check_board，不输出缺失器件的部分布局。
    """
    from pcb_initial_place import pack_units
    placed = pack_units(board, components, units, options, stats, budget, stage="legalization", preserve=True)
    stats["legalization_method"] = "row_cluster_envelopes_then_check_board"
    return placed


def _anneal(board, components, units, immovable, options, stats, budget, rng, best, best_cost):
    """输入合法候选后做有限退火；宏观动作保持簇刚体，内部动作受半径限制。"""
    current, current_cost = dict(components), evaluate_cost(_board_with(board, components), options, budget=budget)
    movable = [unit for unit in units if not unit["fixed"]]
    singletons = [unit for unit in movable if not unit["cluster"]]
    clusters = [unit for unit in movable if unit["cluster"]]
    actions = ("translate", "swap", "rotate", "cluster_translate")
    step = options.get("grid_step", rules_for(board).grid_step)
    from pcb_placement import _envelope
    focus = {pin["compId"] for net in options["feedback"]["failed_nets"] for pin in board["nets"][net]}
    for hotspot in options["feedback"]["hotspots"]:
        region = box(*hotspot["bounds"])
        focus.update(cid for cid, component in components.items()
                     if _envelope(component, rules_for(board).curve_tolerance).intersects(region))
    stats["feedback_components"] = sorted(focus)
    stats["feedback_candidate_moves"] = 0
    budget.best_candidate, budget.best_cost = best, best_cost

    def choose(pool):
        # 失败网络和热点关联单元获得更高采样权重；其余单元仍可为让路而调整。
        return rng.choices(pool, weights=[4 if focus & set(unit["members"]) else 1 for unit in pool], k=1)[0]

    def consider(candidate, temperature, record):
        nonlocal current, current_cost, best, best_cost
        if not _legal(board, candidate, options, budget):
            return
        cost = evaluate_cost(_board_with(board, candidate), options, budget=budget)
        change = cost["total"]-current_cost["total"]
        if change <= 0 or rng.random() < math.exp(-min(700.0, change/max(temperature, 1e-300))):
            current, current_cost = candidate, cost
            record["accepted"] += 1
            if cost["total"] < best_cost["total"]-1e-9:
                best, best_cost = dict(candidate), cost
                budget.best_candidate, budget.best_cost = best, best_cost

    for level in range(options["sa_temperatures"]):
        temperature = options["sa_temperature"] * options["sa_cooling"]**level
        if not math.isfinite(temperature) or temperature <= 0:
            raise PlacementFailure("PLACEMENT_COST_BUDGET", "退火温度超出有限可用范围，未找到进一步候选")
        for index in range(options["sa_steps_per_temperature"]):
            budget.check()
            action = actions[(index+level) % len(actions)]
            record = stats["sa_actions"][action]
            record["attempted"] += 1
            candidate = None
            if action == "translate" and singletons:
                unit = choose(singletons)
                dx, dy = rng.choice(((step, 0), (-step, 0), (0, step), (0, -step)))
                candidate = _translated(current, unit["members"], dx, dy)
            elif action == "cluster_translate" and clusters:
                unit = choose(clusters)
                dx, dy = rng.choice(((step, 0), (-step, 0), (0, step), (0, -step)))
                candidate = _translated(current, unit["members"], dx, dy)
            elif action == "swap" and len(singletons) >= 2:
                a = choose(singletons)
                b = choose([unit for unit in singletons if unit is not a])
                ax, ay = _center(current, a)
                bx, by = _center(current, b)
                candidate = _translated(current, a["members"], bx-ax, by-ay)
                candidate = _translated(candidate, b["members"], ax-bx, ay-by)
            elif action == "rotate" and movable:
                unit = choose(movable)
                deltas = _rotation_deltas(current, unit, options)
                if deltas:
                    candidate = _rotated(current, unit, rng.choice(deltas))
            if candidate is not None:
                if any(candidate[cid] is not current[cid] for cid in focus if cid in current):
                    stats["feedback_candidate_moves"] += 1
                consider(candidate, temperature, record)
            stats["sa_steps"] += 1
        stats["sa_temperature_levels"] = level+1
    # 内部细调从最佳宏观候选开始，所选簇的其余成员和固定件保持。
    current, current_cost = dict(best), best_cost
    origins = {cid: (current[cid]["x"], current[cid]["y"])
               for unit in units if unit["cluster"] for cid in unit["members"]}
    stats["cluster_intra_origins"] = {cid: list(point) for cid, point in origins.items()}
    stats["macro_best_poses"] = {cid: {key: current[cid][key] for key in ("x", "y", "rotation", "layer")}
                                 for cid in origins}
    for unit in (unit for unit in units if unit["cluster"]):
        ids = [cid for cid in unit["members"] if cid not in immovable]
        for index in range(options["cluster_intra_steps"] if ids else 0):
            budget.check()
            record = stats["cluster_intra_actions"]
            record["attempted"] += 1
            cid = rng.choice(ids)
            local_step = min(step, options["cluster_intra_radius"])
            dx, dy = rng.choice(((local_step, 0), (-local_step, 0), (0, local_step), (0, -local_step)))
            candidate = _translated(current, (cid,), dx, dy)
            origin = origins[cid]
            if math.hypot(candidate[cid]["x"]-origin[0], candidate[cid]["y"]-origin[1]) <= options["cluster_intra_radius"]+1e-9:
                temperature = options["sa_temperature"] * options["sa_cooling"]**index
                consider(candidate, max(temperature, 1e-300), record)
    return best, best_cost


def place_analytic(board, options, immovable, source_components=None):
    """生成固定锚点力导向、簇式合法化和退火候选，返回完整板与逐项统计。

    入参：已规范化板、布局选项、父入口核实的不可动器件集合；可选原始
    器件映射用于统计。出参：(完整候选板,stats)，最终仍调用原 check_board。
    预算内已有合法候选时可返回它并明确预算耗尽；从未找到合法布局则抛
    PlacementFailure。非法配置抛 PipelineError；不写原板、不删除端子或旧铜。
    """
    options = validate_options(options, board)
    source_components = source_components or {c["id"]: c for c in board["components"]}
    components = {c["id"]: deepcopy(c) for c in board["components"]}
    immovable = set(immovable)
    if immovable - set(components):
        raise PipelineError("不可动器件集合含未知器件")
    units = _units(board, options, immovable)
    stats = dict(enabled=options.get("enabled", True), method="analytic_sa", seed=options.get("seed", 0),
                 candidate_checks=0, global_iterations=0, sa_steps=0, sa_temperature_levels=0,
                 sa_actions={action: dict(attempted=0, accepted=0) for action in ("translate", "swap", "rotate", "cluster_translate")},
                 cluster_intra_actions=dict(attempted=0, accepted=0),
                 fixed_components=sorted(immovable), macro_fixed_clusters=[unit["id"] for unit in units if unit["cluster"] and unit["fixed"]],
                 repair_attempt=options.get("repair_attempt", 0), budget_exhausted=False,
                 deterministic_completion=True, full_board_accepted=False, global_optimum_proven=False)
    budget = _Budget(options, stats)
    best, best_cost = None, None
    try:
        initial_cost = evaluate_cost(board, options, budget=budget)
        stats["initial_cost"] = initial_cost
        components_legal = _legal(board, components, options, budget)
        if components_legal:
            best, best_cost = dict(components), initial_cost
            budget.best_candidate, budget.best_cost = best, best_cost
        from pcb_placement import _envelope
        available = board_polygon(board).buffer(-rules_for(board).board_edge_clearance)
        outside = [cid for cid, component in components.items()
                   if not available.covers(_envelope(component, rules_for(board).curve_tolerance))]
        outside_regions = [cid for cid, component in components.items() if cid in options.get("regions", {})
                           and not options["regions"][cid].covers(_envelope(component, rules_for(board).curve_tolerance))]
        stats["initialization"] = dict(requested=options["initialization"], status="skipped",
            initial_geometry_legal=best is not None, initial_outside_board=sorted(outside),
            initial_outside_regions=sorted(outside_regions), functional_groups=len(options["functional_groups"]))
        if not options.get("enabled", True):
            if best is None:
                raise PlacementFailure("PLACEMENT_DISABLED_INVALID", "布局已关闭且原几何/区域无效")
        else:
            if options["functional_cells"]:
                from pcb_functional_cells import generate_seed
                components = generate_seed(board, components, options["functional_cells"], options,
                                           immovable, stats, budget)
                components_legal = _legal(board, components, options, budget)
            if options["initialization"] == "region_pack" or (options["initialization"] == "auto" and best is None):
                from pcb_initial_place import pack_units
                components = pack_units(board, components, units, options, stats, budget)
                components_legal = True
                # 几何已完整验收；随后评分超时不能丢掉唯一合法候选，也不能伪造分数。
                if best is None:
                    best, best_cost = dict(components), None
                    budget.best_candidate, budget.best_cost = best, best_cost
                initialized_cost = evaluate_cost(_board_with(board, components), options, budget=budget)
                stats["initialized_cost"] = initialized_cost
                if best_cost is None or initialized_cost["total"] < best_cost["total"]:
                    best, best_cost = dict(components), initialized_cost
                    budget.best_candidate, budget.best_cost = best, best_cost
            global_components = _global(board, components, units, options, stats, budget)
            try:
                if (options["global_iterations"] == 0 and components_legal) or _legal(board, global_components, options, budget):
                    legalized = global_components
                    stats["legalization_method"] = "verified_complete_geometry"
                else:
                    legalized = _legalize(board, global_components, units, options, stats, budget)
            except PlacementFailure as exc:
                if exc.diagnostics["code"] != "PLACEMENT_NO_CANDIDATE" or best is None:
                    raise
                stats["global_candidate_rejected"] = dict(exc.diagnostics)
                legalized = dict(best)
            legal_cost = evaluate_cost(_board_with(board, legalized), options, budget=budget)
            stats["legalized_cost"] = legal_cost
            if best is None or legal_cost["total"] < best_cost["total"]:
                best, best_cost = dict(legalized), legal_cost
                budget.best_candidate, budget.best_cost = best, best_cost
            rng = random.Random(options.get("seed", 0))
            best, best_cost = _anneal(board, best, units, immovable, options, stats, budget, rng, best, best_cost)
    except PlacementFailure as exc:
        for stage in ("initialization", "legalization"):
            if stats.get(stage, {}).get("status") == "running":
                stats[stage].update(status="not_found", reason=exc.diagnostics["code"])
        if exc.diagnostics["code"] in ("PLACEMENT_TIME_BUDGET", "PLACEMENT_CANDIDATE_BUDGET"):
            stats["budget_exhausted"] = True
            stats["budget_diagnostics"] = dict(exc.diagnostics)
            stats["deterministic_completion"] = exc.diagnostics.get("budget_scope") != "time"
        if budget.best_candidate is not None:
            best, best_cost = budget.best_candidate, budget.best_cost
        if best is None or exc.diagnostics["code"] not in ("PLACEMENT_TIME_BUDGET", "PLACEMENT_CANDIDATE_BUDGET"):
            exc.diagnostics["stats"] = deepcopy(stats)
            raise
        stats["budget_exhausted"] = True
        stats["budget_diagnostics"] = dict(exc.diagnostics)
        stats["deterministic_completion"] = exc.diagnostics.get("budget_scope") != "time"
    if best is None:
        raise PlacementFailure("PLACEMENT_NO_CANDIDATE", "有限搜索未找到合法布局，未证明无解", stats=stats)
    solution = _board_with(board, best)
    errors = check_board(solution, ignore_connectivity=True)
    from pcb_functional_cells import check_relations
    errors.extend(check_relations(solution, options.get("functional_cells", [])))
    from pcb_placement import _envelope
    for component in solution["components"]:
        region = options.get("regions", {}).get(component["id"])
        if region is not None:
            region = box(*region) if isinstance(region, (list, tuple)) else region
            if not region.covers(_envelope(component, rules_for(board).curve_tolerance)):
                errors.append(f"PLACEMENT_REGION: {component['id']} exceeds its declared region")
    if errors:
        raise PlacementFailure("PLACEMENT_VERIFICATION", "候选未通过原几何验收", errors=errors, stats=stats)
    if options.get("functional_cells"):
        stats["functional_cells_verification"] = dict(passed=True,
            relations_checked=sum(len(cell["relations"]) for cell in options["functional_cells"]),
            scope="实际完整板几何与显式相对关系，不代表布线或原生验收")
    stats["final_cost"] = best_cost
    stats["cost_evaluation_complete"] = best_cost is not None
    stats["initial_hpwl"] = stats["initial_cost"]["raw"]["hpwl"]
    stats["final_hpwl"] = best_cost["raw"]["hpwl"] if best_cost is not None else None
    stats["rudy"] = best_cost["rudy"] if best_cost is not None else None
    stats["moved_components"] = [cid for cid in best if any(best[cid][key] != source_components[cid][key]
                                                          for key in ("x", "y", "rotation", "layer"))]
    stats["moved_count"] = len(stats["moved_components"])
    stats["preserved_components"] = sorted(set(best)-set(stats["moved_components"]))
    stats["repair_changed"] = bool(stats["repair_attempt"] and stats["moved_count"])
    stats["elapsed_seconds"] = time.monotonic()-budget.started
    return solution, stats
