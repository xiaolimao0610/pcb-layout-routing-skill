"""Deterministic, bounded component legalization in the board's mil units.

Existing legal poses take precedence over optimization. The heuristic searches
only a finite grid and the requested rotations; failure is not a proof that no
continuous placement exists, and success is not a global optimum.
"""
from __future__ import annotations

from copy import deepcopy
import heapq
import math
from numbers import Real
import random
import time

from shapely.geometry import box
from shapely.ops import unary_union

from pcb_geometry import shape_from_spec, transform
from pcb_solver_hardened import _body, _pad_bounds, sensitive_halo_targets
from pcb_pipeline_common import (
    PipelineError, normalize_board, rules_for, check_board, board_polygon,
    copper_and_drills, _fast_pad_bounds, attached_vias_for_components,
)


_OPTION_KEYS = {"enabled", "method", "grid_step", "rotations", "max_candidates", "seed",
                "movable_ids", "regions", "repair_attempt", "optimize", "initial_poses"}
_POSE_KEYS = ("x", "y", "rotation", "layer")


def _finite(value, name):
    if isinstance(value, bool) or not isinstance(value, Real):
        raise PipelineError(f"placement.{name}: finite number required")
    try:
        value = float(value)
    except (OverflowError, ValueError) as exc:
        raise PipelineError(f"placement.{name}: finite number required") from exc
    if not math.isfinite(value):
        raise PipelineError(f"placement.{name}: finite number required")
    return value


def _options(options, board, rules):
    """校验布局选项并返回独立的规范化配置，不改变输入工程。

    入参：options 为选项字典或 None；board 为完整规范化工程；rules 为规则对象。
    出参：包含默认值、可动器件集合和区域几何的字典；method 默认 legacy。
    异常：未知字段、无效值或未知方法抛出 PipelineError，绝不静默回退。
    """
    if options is None:
        options = {}
    from pcb_global_place import NEW_OPTION_KEYS, validate_options
    if not isinstance(options, dict) or set(options) - (_OPTION_KEYS | NEW_OPTION_KEYS):
        raise PipelineError("placement: unknown option or options is not an object")
    result = {"enabled": True, "method": "legacy", "grid_step": rules.grid_step,
              "rotations": [0, 90, 180, 270], "max_candidates": 4000, "seed": 0,
              "movable_ids": [c["id"] for c in board["components"]], "regions": {},
              "repair_attempt": 0, "optimize": False, "initial_poses": {}}
    result.update(deepcopy(options))
    if result["method"] not in ("legacy", "analytic_sa"):
        raise PipelineError("placement.method 必须为 legacy 或 analytic_sa")
    if type(result["enabled"]) is not bool:
        raise PipelineError("placement.enabled must be boolean")
    if type(result["optimize"]) is not bool:
        raise PipelineError("placement.optimize must be boolean")
    result["grid_step"] = _finite(result["grid_step"], "grid_step")
    if result["grid_step"] <= 0:
        raise PipelineError("placement.grid_step must be positive")
    for name in ("max_candidates", "seed"):
        if type(result[name]) is not int or (name == "max_candidates" and result[name] <= 0):
            raise PipelineError(f"placement.{name}: {'positive ' if name == 'max_candidates' else ''}integer required")
    if type(result["repair_attempt"]) is not int or result["repair_attempt"] < 0:
        raise PipelineError("placement.repair_attempt must be a nonnegative integer")
    rotations = result["rotations"]
    if not isinstance(rotations, (list, tuple)) or not rotations:
        raise PipelineError("placement.rotations must be a nonempty angle list")
    rotations = [_finite(angle, "rotations") % 360 for angle in rotations]
    if len(set(rotations)) != len(rotations):
        raise PipelineError("placement.rotations contains duplicate equivalent angles")
    result["rotations"] = rotations
    ids = {c["id"] for c in board["components"]}
    movable = result["movable_ids"]
    if (not isinstance(movable, (list, tuple))
            or any(isinstance(cid, bool) or not isinstance(cid, (str, int)) for cid in movable)):
        raise PipelineError("placement.movable_ids must be a component ID list")
    movable = [str(cid) for cid in movable]
    if len(set(movable)) != len(movable) or set(movable) - ids:
        raise PipelineError("placement.movable_ids contains duplicates or unknown components")
    result["movable_ids"] = set(movable)
    initial = result["initial_poses"]
    if not isinstance(initial, dict) or set(initial) - ids:
        raise PipelineError("placement.initial_poses must map known component IDs to poses")
    for cid, pose in initial.items():
        if not isinstance(pose, dict) or set(pose) != set(_POSE_KEYS):
            raise PipelineError(f"placement.initial_poses.{cid}: x/y/rotation/layer required")
        for key in ("x", "y", "rotation"):
            pose[key] = _finite(pose[key], f"initial_poses.{cid}.{key}")
        if type(pose["layer"]) is not int or pose["layer"] not in (1, 2):
            raise PipelineError(f"placement.initial_poses.{cid}.layer: top or bottom required")
    regions = result["regions"]
    if not isinstance(regions, dict) or set(regions) - ids:
        raise PipelineError("placement.regions must map known component IDs to rectangles")
    result["regions"] = {}
    for cid, region in regions.items():
        if not isinstance(region, (list, tuple)) or len(region) != 4:
            raise PipelineError(f"placement.regions.{cid}: [xmin,ymin,xmax,ymax] required")
        rect = tuple(_finite(v, f"regions.{cid}") for v in region)
        if rect[2] <= rect[0] or rect[3] <= rect[1]:
            raise PipelineError(f"placement.regions.{cid}: positive rectangle required")
        result["regions"][cid] = box(*rect)
    result = validate_options(result, board)
    if result["method"] == "legacy" and result.get("initialization", "preserve") != "preserve":
        raise PipelineError("分区初始化需要 placement.method=analytic_sa，legacy 不会静默忽略初始化选项")
    if result["method"] == "legacy" and any(result.get(key) for key in
                                             ("clusters", "edge_components", "align_groups", "functional_cells")):
        raise PipelineError("clusters、edge_components、align_groups、functional_cells 需要 placement.method=analytic_sa")
    if result["method"] == "legacy" and any(result.get("feedback", {}).get(key)
                                             for key in ("failed_nets", "hotspots")):
        raise PipelineError("局部拥塞反馈需要 placement.method=analytic_sa")
    return result


def _envelope(component, tolerance):
    """Actual transformed body, courtyard and pad shapes, including bottom X mirror."""
    shapes = [_body(component, tolerance).outer]
    if "courtyard_polygon" in component:
        shapes.append(transform(shape_from_spec({"type": "POLYGON",
            "points": component["courtyard_polygon"]}, tolerance), component["x"],
            component["y"], component["rotation"], component["layer"] == 2).outer)
    shapes.extend(_pad_bounds(component, pad, tolerance).outer for pad in component["pads"])
    return unary_union(shapes)


def _subset(board, components):
    """输入完整板及已放置器件，返回保留原铜的局部检查副本。

    局部网表只保留已放端子；敏感区依据完整网表验证后投影为显式禁区，
    因而仍约束端子尚未放置的原铜。完整器件集合不生成临时禁区，保留
    原始标签供最终验收。输入不修改；非法筛选或几何沿原异常上抛。
    """
    result = dict(board)
    components = list(components)
    ids = {c["id"] for c in components}
    result["components"] = list(components)
    result["routes"] = dict(board.get("routes", {}))
    if "vias" in result["routes"]:
        result["routes"]["vias"] = attached_vias_for_components(board, components, subset=True)
    result["nets"] = {net: kept for net, refs in board.get("nets", {}).items()
                      if (kept := [ref for ref in refs if str(ref.get("compId")) in ids])}
    tags = dict(board.get("tags", {}))
    tags["component_tags"] = {cid: deepcopy(tag) for cid, tag in tags.get("component_tags", {}).items() if cid in ids}
    tags["net_tags"] = {net: tag for net, tag in tags.get("net_tags", {}).items() if net in result["nets"]}
    if ids != {c["id"] for c in board["components"]}:
        original_types = board.get("tags", {}).get("net_tags", {})
        # 原铜不能因其所有端子尚未摆放而失去保护，网类仍按完整板解释。
        copper_nets = {item["net"] for kind in ("traces", "vias", "shield_vias", "copper_pours")
                       for item in result["routes"].get(kind, []) if item.get("net")}
        candidate_nets = set(board["nets"]) | copper_nets
        keepouts = list(result["routes"].get("keepouts", []))
        for component in components:
            tag = tags["component_tags"].get(component["id"], {})
            selected, types = sensitive_halo_targets(tag, board["nets"])
            if tag.get("role") != "SENSITIVE_IC" or tag.get("keepout_halo", 0) <= 0:
                continue
            targets = sorted(net for net in candidate_nets
                             if (selected is None or net in selected)
                             and (types is None or ("GND" if net.upper() == "GND" else
                                  original_types.get(net, {}).get("type", "NORMAL")) in types))
            if targets:
                keepouts.append(dict(points=list(_body(component, rules_for(board).curve_tolerance).outer.exterior.coords),
                    layers=list(tag.get("halo_layers", [component["layer"]])),
                    clearance=tag["keepout_halo"], nets=targets))
            # 禁区已表达同一交集；空交集不能退回默认SEWAGE或扩大成类型全集。
            tag["keepout_halo"] = 0
            tag.pop("halo_nets", None)
            tag.pop("halo_net_types", None)
        if keepouts:
            result["routes"]["keepouts"] = keepouts
    result["tags"] = tags
    return result


def _copper_locked(board, tolerance, checkpoint=None):
    """输入完整铜几何与接触容差，返回受原铜连接保护的器件集合。

    可选 checkpoint 在扫描时检查预算；几何与预算异常原样上抛，不解除保护。
    """
    copper, _ = copper_and_drills(board)
    routes = [item for item in copper if item["terminal"] is None]
    locked = set()
    owners = {f"via {i}": v.get("metadata", {}).get("parent_component_id")
              for i, v in enumerate(board.get("routes", {}).get("vias", []))}
    for pad in copper:
        if checkpoint is not None:
            checkpoint("copper_protection")
        owner = pad["terminal"][0] if pad["terminal"] is not None else owners.get(pad["name"])
        if owner is None:
            continue
        for route in (routes if pad["terminal"] is not None else copper):
            if checkpoint is not None:
                checkpoint("copper_protection")
            other_owner = route["terminal"][0] if route["terminal"] is not None else owners.get(route["name"])
            if owner == other_owner:
                continue
            if (set(pad["layers"]) & set(route["layers"])
                    and pad["outer"].distance(route["outer"]) <= tolerance):
                # Conservative contact includes uncertain curved-boundary contact.
                locked.add(owner)
                break
    return locked


def _grid_points(bounds, origin, step):
    """Yield a rectangular lattice near-first without allocating the entire board."""
    x0, y0, x1, y1 = bounds
    scaled = [v / step for v in bounds]
    if not all(math.isfinite(v) for v in scaled):
        raise PipelineError("placement.grid_step is too small for the coordinate scale")
    ix0, iy0, ix1, iy1 = math.ceil(scaled[0]), math.ceil(scaled[1]), math.floor(scaled[2]), math.floor(scaled[3])
    if ix0 > ix1 or iy0 > iy1:
        return
    ix = min(ix1, max(ix0, round(min(x1, max(x0, origin[0])) / step)))
    iy = min(iy1, max(iy0, round(min(y1, max(y0, origin[1])) / step)))
    def distance(x, y):
        return math.hypot(x * step - origin[0], y * step - origin[1])
    queue, visited = [(distance(ix, iy), ix, iy)], {(ix, iy)}
    while queue:
        dist, ix, iy = heapq.heappop(queue)
        yield dist, ix * step, iy * step
        for dx, dy in ((-1, 0), (0, -1), (0, 1), (1, 0)):
            nx, ny = ix + dx, iy + dy
            if ix0 <= nx <= ix1 and iy0 <= ny <= iy1 and (nx, ny) not in visited:
                visited.add((nx, ny))
                heapq.heappush(queue, (distance(nx, ny), nx, ny))


def _anchors(components, tolerance, checkpoint=None):
    """输入器件与曲线容差，返回逐网焊盘锚点；预算/几何异常不吞掉。"""
    result = {}
    for component in components:
        if checkpoint is not None:
            checkpoint("net_anchors")
        for pad in component["pads"]:
            if pad["net"]:
                point = _fast_pad_bounds(component, pad, tolerance).inner.representative_point()
                result.setdefault(pad["net"], []).append((component["id"], point.x, point.y))
    return result


def _same_geometry(a, b, tolerance):
    if (a["x"], a["y"], a["rotation"] % 360, a["layer"]) == (b["x"], b["y"], b["rotation"] % 360, b["layer"]):
        return True
    return (_envelope(a, tolerance).equals(_envelope(b, tolerance))
            and all(_fast_pad_bounds(a, pa, tolerance).outer.equals(_fast_pad_bounds(b, pb, tolerance).outer)
                    for pa, pb in zip(a["pads"], b["pads"])))


def _net_weight(board, net, options=None):
    """读取显式布局网络权重；入参为板、网名和可选配置，返回非负权重。

    地网只按 ground_nets 或 net_tags.type 识别，不按网名猜测。
    无效权重由统一配置校验抛 PipelineError；不改变几何验收网类规则。
    """
    from pcb_global_place import net_weight
    return net_weight(board or {}, net, options or {})


def _constraint_terms(candidate, board, components_map, tolerance):
    """Evaluate RELATIVE_POSE attraction and REPEL_FIELD repulsion for candidate."""
    if not board or not components_map:
        return 0.0, []
    constraints = board.get("metadata", {}).get("pipeline_constraints", [])
    if not constraints:
        return 0.0, []
    cid = candidate["id"]
    pads_by_id = {p["padId"]: p for p in candidate["pads"]}
    cost = 0.0
    target_points = []
    cand_body = None
    for rule in constraints:
        op = rule.get("operator")
        if op == "RELATIVE_POSE":
            src, tgt = rule.get("source", {}), rule.get("target", {})
            if src.get("compId") == cid and tgt.get("compId") in components_map:
                my_pad = pads_by_id.get(src.get("padId"))
                other_comp = components_map[tgt["compId"]]
                other_pad = next((p for p in other_comp["pads"] if p["padId"] == tgt.get("padId")), None)
            elif tgt.get("compId") == cid and src.get("compId") in components_map:
                my_pad = pads_by_id.get(tgt.get("padId"))
                other_comp = components_map[src["compId"]]
                other_pad = next((p for p in other_comp["pads"] if p["padId"] == src.get("padId")), None)
            else:
                continue
            if my_pad is not None and other_pad is not None:
                p_my = _fast_pad_bounds(candidate, my_pad, tolerance).inner.centroid
                p_oth = _fast_pad_bounds(other_comp, other_pad, tolerance).inner.centroid
                d = p_my.distance(p_oth)
                max_d = float(rule.get("max_distance", 200.0))
                cost += 2.5 * d + (50.0 * (d - max_d) if d > max_d else 0.0)
                target_points.append((p_oth.x - (p_my.x - candidate["x"]),
                                      p_oth.y - (p_my.y - candidate["y"]), 4.0))
        elif op == "REPEL_FIELD":
            other_id = (rule.get("target") if rule.get("source") == cid else
                        rule.get("source") if rule.get("target") == cid else None)
            if other_id and other_id in components_map:
                if cand_body is None:
                    cand_body = _body(candidate, tolerance).outer
                other_body = _body(components_map[other_id], tolerance).outer
                gap = cand_body.distance(other_body)
                min_d = float(rule.get("min_distance", 200.0))
                if gap < min_d:
                    cost += 50.0 * (min_d - gap)
                elif gap < min_d * 1.25:
                    cost += 2.0 * (min_d * 1.25 - gap)
    return cost, target_points


def _board_hpwl(components_iter, board, tolerance, options=None):
    """Compute weighted Half-Perimeter Wirelength (HPWL) across all board nets."""
    by_net = {}
    for comp in components_iter:
        for pad in comp["pads"]:
            net = pad.get("net")
            if net:
                pt = _fast_pad_bounds(comp, pad, tolerance).inner.representative_point()
                by_net.setdefault(net, []).append((pt.x, pt.y))
    total = 0.0
    for net, pts in by_net.items():
        if len(pts) < 2:
            continue
        w = _net_weight(board, net, options)
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        total += w * ((max(xs) - min(xs)) + (max(ys) - min(ys)))
    return total


def _candidates(component, options, polygon, rules, anchors, rng, board=None, components_map=None,
                forbidden_union=None, checkpoint=None, counters=None):
    """生成有限排序候选；可选检查点覆盖生成和评分，不修改输入。

    入参为器件、规范化配置、板域、规则、网络锚点与随机发生器；其余参数
    提供原约束、障碍和预算回调。返回含原姿态的候选列表。预算异常沿回调
    上抛，几何/配置错误保持原异常；不把生成条数当成精确验收次数。
    """
    if checkpoint is not None:
        checkpoint("candidate_generation")
    origin = component["x"], component["y"]
    allowed = polygon
    region = options["regions"].get(component["id"])
    if region is not None:
        allowed = allowed.intersection(region)
    if allowed.is_empty:
        return [component]
    bx0, by0, bx1, by1 = allowed.bounds
    optimize = options.get("optimize", False)
    _, constraint_targets = _constraint_terms(component, board, components_map, rules.curve_tolerance)
    # Compute an ideal attraction center when optimizing or satisfying RELATIVE_POSE
    attract_centers = [origin]
    if optimize or constraint_targets:
        wx_sum = wy_sum = w_total = 0.0
        for tx, ty, w in constraint_targets:
            wx_sum += tx * w
            wy_sum += ty * w
            w_total += w
        for pad in component["pads"]:
            net = pad.get("net")
            w = _net_weight(board, net, options)
            if w <= 0:
                continue
            others = [(px, py) for cid, px, py in anchors.get(net, []) if cid != component["id"]]
            if others:
                net_w = w / max(1.0, math.sqrt(len(others)))
                tx = sum(px for px, _ in others) / len(others)
                ty = sum(py for _, py in others) / len(others)
                wx_sum += tx * net_w
                wy_sum += ty * net_w
                w_total += net_w
        if w_total > 0:
            ideal = (wx_sum / w_total, wy_sum / w_total)
            if math.hypot(ideal[0] - origin[0], ideal[1] - origin[1]) >= options["grid_step"]:
                attract_centers.append(ideal)
    generators, local_shapes = [], []
    for angle in options["rotations"]:
        if checkpoint is not None:
            checkpoint("candidate_generation")
        local = dict(component, x=0, y=0, rotation=angle)
        if any(_same_geometry(local, previous, rules.curve_tolerance) for previous in local_shapes):
            continue
        local_shapes.append(local)
        lx0, ly0, lx1, ly1 = _envelope(local, rules.curve_tolerance).bounds
        bounds = (bx0 - lx0, by0 - ly0, bx1 - lx1, by1 - ly1)
        for center_idx, center_pt in enumerate(attract_centers):
            def poses(rotation=angle, grid_bounds=bounds, seed_pt=center_pt, is_primary=(center_idx == 0)):
                if is_primary:
                    yield 0.0, origin[0], origin[1], rotation
                for dist, x, y in _grid_points(grid_bounds, seed_pt, options["grid_step"]):
                    yield dist, x, y, rotation
            generators.append(poses())
    pool, seen = [], {(component["x"], component["y"], component["rotation"] % 360)}
    # ponytail: rank a finite near-first pool; exhaustive continuous packing is outside this heuristic.
    merged = heapq.merge(*generators, key=lambda pose: pose[0])
    scan_limit = options["max_candidates"] * (4 if forbidden_union is not None else 1)
    for _, pose in zip(range(scan_limit), merged):
        if checkpoint is not None:
            checkpoint("candidate_generation")
        if counters is not None:
            counters["generated_candidates"] = counters.get("generated_candidates", 0) + 1
        if len(pool) >= options["max_candidates"]:
            break
        _, x, y, rotation = pose
        if (x, y, rotation) in seen:
            continue
        seen.add((x, y, rotation))
        candidate = dict(component, x=x, y=y, rotation=rotation)
        if forbidden_union is not None and forbidden_union.intersects(_envelope(candidate, rules.curve_tolerance)):
            continue
        displacement = math.hypot(x - origin[0], y - origin[1])
        wirelength = 0.0
        for pad in candidate["pads"]:
            if checkpoint is not None:
                checkpoint("candidate_scoring")
            net = pad["net"]
            others = [(px, py) for cid, px, py in anchors.get(net, []) if cid != component["id"]]
            if others:
                w = _net_weight(board, net, options)
                point = _fast_pad_bounds(candidate, pad, rules.curve_tolerance).inner.representative_point()
                tx = sum(px for px, _ in others) / len(others)
                ty = sum(py for _, py in others) / len(others)
                wirelength += w * math.hypot(point.x - tx, point.y - ty)
        c_cost, _ = _constraint_terms(candidate, board, components_map, rules.curve_tolerance)
        if optimize:
            score = wirelength + c_cost + 0.1 * displacement
        else:
            score = displacement + 0.2 * wirelength + c_cost
        pool.append((score, rng.random(), candidate))
    pool.sort(key=lambda item: (item[0], item[1]))
    if checkpoint is not None:
        checkpoint("candidate_generation")
    return [component] + [item[2] for item in pool]


def place_components(board, options=None):
    """在有限预算内生成合法布局副本，并返回布局统计。

    入参：board 为完整规范化工程；options 为布局选项或 None。
    regions 用器件 ID 映射完整本体、装配边界和焊盘必须容纳的矩形；
    movable_ids 只是可动白名单，不能覆盖固定姿态或既有铜连接保护。
    max_candidates 限制整次候选检查数，max_seconds 覆盖旧布局预处理、候选
    生成、回溯和优化；时限是协作式检查，不能抢占正在执行的几何调用。
    generated_candidates 单独记录候选生成量，不冒充精检次数。正数 repair_attempt 请求不同
    的合法姿态用于布线反馈，既不解锁器件，也不代表布线已经成功。
    出参：(布局副本, 统计字典)，输入工程保持不变。
    异常：配置无效、方法未实现或有限搜索未找到合法候选时抛出
    PipelineError；搜索耗尽不能解释为连续几何无解。
    """
    started = time.monotonic()
    result = normalize_board(board)
    rules = rules_for(result)
    options = _options(options, result, rules)
    from pcb_global_place import PlacementFailure
    timing = dict(method="legacy", candidate_checks=0, generated_candidates=0,
                  candidate_pool_count=0, budget_exhausted=False)

    def checkpoint(phase):
        if options["method"] != "legacy":
            return
        elapsed = time.monotonic() - started
        if elapsed >= options["max_seconds"]:
            timing.update(budget_exhausted=True, elapsed_seconds=elapsed,
                          stopped_by="max_seconds", budget_phase=phase)
            raise PlacementFailure("PLACEMENT_TIME_BUDGET", "布局总时限内未找到进一步候选，未证明无解",
                                   budget_scope="time", phase=phase, stats=dict(timing))

    checkpoint("input_validation")
    components = {c["id"]: c for c in result["components"]}
    source_components = components.copy()
    initial_errors = check_board(result, ignore_connectivity=True)
    checkpoint("initial_geometry")
    fatal = ("INVALID_INPUT:", "MISSING_TERMINAL:", "DUPLICATE_TERMINAL:",
             "NET_MISMATCH:", "NETLIST_INCOMPLETE:", "FIXED_POSE:")
    if any(error.startswith(fatal) for error in initial_errors):
        raise PipelineError("placement input invalid: " + "; ".join(initial_errors))
    tags = result.get("tags", {}).get("component_tags", {})
    fixed = set()
    for cid, component in components.items():
        checkpoint("fixed_components")
        if "locked" in component and type(component["locked"]) is not bool:
            raise PipelineError(f"placement: {cid}.locked must be boolean")
        if component.get("locked"):
            fixed.add(cid)
        for declaration in (component, tags.get(cid, {})):
            if "fixed" in declaration and type(declaration["fixed"]) is not bool:
                raise PipelineError(f"placement: {cid}.fixed must be boolean")
            if declaration.get("fixed") or "fixed_pose" in declaration:
                fixed.add(cid)
            if "fixed_pose" in declaration:
                pose = declaration["fixed_pose"]
                if not isinstance(pose, dict) or any(component[k] != pose.get(k) for k in _POSE_KEYS):
                    raise PipelineError(f"placement: fixed_pose baseline differs for {cid}")
    def in_region(component):
        region = options["regions"].get(component["id"])
        return region is None or region.covers(_envelope(component, rules.curve_tolerance))
    # 禁用布局时所有器件均不移动，无需再逐件与旧铜求交来推导可动集合。
    # 上面的完整几何/固定姿态检查和后面的区域检查仍执行，旧铜原样保留。
    copper_locked = (_copper_locked(result, rules.contact_tolerance, checkpoint)
                     if options["enabled"] else set())
    checkpoint("copper_protection")
    immovable = (set(components) - options["movable_ids"]) | fixed | copper_locked
    seeded_ids = []
    if options["initial_poses"]:
        if not options["enabled"]:
            raise PipelineError("placement.initial_poses requires enabled placement")
        seeded = deepcopy(result["components"])
        for component in seeded:
            checkpoint("initial_poses")
            cid = component["id"]
            if cid not in options["initial_poses"]:
                continue
            pose = options["initial_poses"][cid]
            if cid in immovable:
                raise PipelineError(f"placement.initial_poses cannot override an immovable component: {cid}")
            if pose["layer"] != component["layer"]:
                raise PipelineError(f"placement.initial_poses does not support component side changes: {cid}")
            component.update(pose)
            seeded_ids.append(cid)
        result = _subset(result, seeded)
        components = {c["id"]: c for c in result["components"]}
        initial_errors = check_board(result, ignore_connectivity=True)
    if options["method"] == "analytic_sa":
        from pcb_global_place import place_analytic
        solution, stats = place_analytic(result, options, immovable, source_components=source_components)
        stats["copper_locked_components"] = sorted(copper_locked)
        stats["copper_lock_scan_skipped"] = not options["enabled"]
        stats["seeded_components"] = sorted(seeded_ids)
        # 延续旧报表的源姿态/种子/最终姿态口径；分项代价仍使用其明确的形心定义。
        stats["initial_hpwl"] = round(_board_hpwl(source_components.values(), result, rules.curve_tolerance, options), 3)
        stats["seeded_hpwl"] = round(_board_hpwl(result["components"], result, rules.curve_tolerance, options), 3)
        stats["final_hpwl"] = round(_board_hpwl(solution["components"], result, rules.curve_tolerance, options), 3)
        stats["initial_cost_scope"] = "after_initial_poses"
        return solution, stats
    stats = timing
    stats.update({"enabled": options["enabled"], "method": "bounded_grid_legalization",
             "candidate_checks": 0, "moved_components": [], "moved_count": 0,
             "preserved_components": sorted(components),
             "copper_locked_components": sorted(copper_locked),
             "copper_lock_scan_skipped": not options["enabled"],
             "repair_attempt": options["repair_attempt"], "repair_changed": False,
             "seeded_components": sorted(seeded_ids)})
    if options["optimize"] or seeded_ids:
        stats["initial_hpwl"] = round(_board_hpwl(source_components.values(), result, rules.curve_tolerance, options), 3)
        stats["seeded_hpwl"] = round(_board_hpwl(components.values(), result, rules.curve_tolerance, options), 3)
    def finish(solution):
        stats["moved_components"] = [c["id"] for c in solution["components"]
                                     if any(c[k] != source_components[c["id"]][k] for k in _POSE_KEYS)]
        stats["moved_count"] = len(stats["moved_components"])
        stats["repair_changed"] = bool(options["repair_attempt"] and stats["moved_count"])
        stats["preserved_components"] = sorted(set(components) - set(stats["moved_components"]))
        if options["optimize"] or seeded_ids:
            stats["final_hpwl"] = round(_board_hpwl(solution["components"], result, rules.curve_tolerance, options), 3)
        stats["elapsed_seconds"] = time.monotonic() - started
        return solution, stats
    original_valid = not initial_errors and all(in_region(c) for c in components.values())
    best_solution = result if original_valid else None

    def solve_legacy():
        nonlocal best_solution
        checkpoint("legacy_search")
        if original_valid and not options["optimize"] and (
                not options["repair_attempt"] or not options["enabled"]
                or not (set(components) - immovable)):
            return result
        if original_valid and options["optimize"] and (
                not options["enabled"] or not (set(components) - immovable)):
            return result
        if not options["enabled"]:
            raise PipelineError("placement disabled but original geometry/regions are invalid: " + "; ".join(initial_errors))
        if any(not in_region(components[cid]) for cid in immovable):
            raise PipelineError("placement: an immovable component violates its region")
        # Reserve unrelated original legal poses before moving any conflicting part.
        reserved = set(immovable)
        if original_valid and options["optimize"] and not options["repair_attempt"]:
            # The complete current geometry already passed; no leave-one-out checks needed.
            reserved = set(components)
        for cid in sorted(set(components) - reserved) if not options["repair_attempt"] else []:
            checkpoint("reserve_original")
            if in_region(components[cid]):
                without = _subset(result, [c for key, c in components.items() if key != cid])
                if set(check_board(without, ignore_connectivity=True)) == set(initial_errors):
                    reserved.add(cid)
                checkpoint("reserve_original")
        placed = {cid: components[cid] for cid in sorted(reserved)}
        base_errors = check_board(_subset(result, placed.values()), ignore_connectivity=True)
        checkpoint("fixed_geometry")
        if base_errors:
            raise PipelineError("placement: fixed/copper-protected geometry is invalid: " + "; ".join(base_errors))
        rng = random.Random(options["seed"] + options["repair_attempt"] * 104729)
        pending = sorted(set(components) - reserved,
                         key=lambda cid: (-_envelope(components[cid], rules.curve_tolerance).area, cid))
        polygon = board_polygon(result)
        anchors = _anchors(components.values(), rules.curve_tolerance, checkpoint)
        candidates = {}
        def candidate_pool(cid):
            # 首次访问时才生成该器件候选；回溯复用同一池，维持随机数和排序顺序。
            if cid not in candidates:
                checkpoint("candidate_generation")
                stats["candidate_pool_count"] += 1
                pool = _candidates(components[cid], options, polygon, rules, anchors, rng,
                                   board=result, components_map=components if options["optimize"] else None,
                                   checkpoint=checkpoint, counters=stats)
                if options["repair_attempt"] and cid == pending[0]:
                    alternatives = [c for c in pool[1:]
                                    if not _same_geometry(c, components[cid], rules.curve_tolerance)]
                    offset = (options["repair_attempt"] - 1) % max(1, len(alternatives))
                    pool = alternatives[offset:] + alternatives[:offset] + pool[:1]
                candidates[cid] = pool
            return candidates[cid]
        def search(index):
            nonlocal best_solution
            checkpoint("backtracking")
            if index == len(pending):
                if options["repair_attempt"] and all(_same_geometry(c, components[cid], rules.curve_tolerance)
                                                     for cid, c in placed.items()):
                    return None
                trial = _subset(result, [placed[c["id"]] for c in result["components"]])
                if not check_board(trial, ignore_connectivity=True):
                    best_solution = trial
                    checkpoint("complete_geometry")
                    return trial
                checkpoint("complete_geometry")
                return None
            cid = pending[index]
            for candidate in candidate_pool(cid):
                checkpoint("backtracking")
                if stats["candidate_checks"] >= options["max_candidates"]:
                    stats.update(budget_exhausted=True, stopped_by="max_candidates")
                    raise PlacementFailure("PLACEMENT_CANDIDATE_BUDGET", "布局候选预算内未找到进一步候选，未证明无解",
                                           budget_scope="candidates", stats=dict(stats))
                stats["candidate_checks"] += 1
                if not in_region(candidate):
                    continue
                placed[cid] = candidate
                legal = not check_board(_subset(result, placed.values()), ignore_connectivity=True)
                checkpoint("partial_geometry")
                if legal:
                    solution = search(index + 1)
                    if solution is not None:
                        return solution
                del placed[cid]
            return None
        try:
            solution = search(0)
        except RecursionError as exc:
            raise PipelineError("placement: bounded search exceeded component recursion capacity") from exc
        if solution is None:
            if stats["candidate_checks"] >= options["max_candidates"]:
                stats.update(budget_exhausted=True, stopped_by="max_candidates")
                raise PlacementFailure("PLACEMENT_CANDIDATE_BUDGET", "布局候选预算内未找到完整布局，未证明无解",
                                       budget_scope="candidates", stats=dict(stats))
            raise PipelineError(f"placement: no legal layout found within {stats['candidate_checks']} candidate checks; "
                                "fixed poses, existing copper, grid/rotations or available area may prevent legalization")
        if options["optimize"]:
            stats["method"] = "bounded_grid_optimization"
            best_solution = solution
            current_map = {c["id"]: c for c in solution["components"]}
            movable_order = sorted(
                set(components) - immovable,
                key=lambda cid: (_envelope(current_map[cid], rules.curve_tolerance).area, cid),
            )
            remaining_budget = max(0, options["max_candidates"] - stats["candidate_checks"])
            per_comp_limit = max(8, min(64, remaining_budget // max(1, len(movable_order))))
            for cid in movable_order:
                checkpoint("optimization")
                if stats["candidate_checks"] >= options["max_candidates"]:
                    stats.update(budget_exhausted=True, stopped_by="max_candidates")
                    break
                comp_now = current_map[cid]
                same_side_others = [
                    _envelope(c, rules.curve_tolerance).buffer(max(0.0, rules.component_courtyard_margin - rules.curve_tolerance))
                    for oid, c in current_map.items()
                    if oid != cid and c["layer"] == comp_now["layer"]
                ]
                other_union = unary_union(same_side_others) if same_side_others else None
                cur_anchors = _anchors(current_map.values(), rules.curve_tolerance, checkpoint)
                local_opts = dict(options, max_candidates=max(64, per_comp_limit * 4))
                stats["candidate_pool_count"] += 1
                pool = _candidates(comp_now, local_opts, polygon, rules, cur_anchors, rng,
                                   board=result, components_map=current_map, forbidden_union=other_union,
                                   checkpoint=checkpoint, counters=stats)
                if len(pool) <= 1:
                    continue
                best_comp = comp_now
                best_obj = _board_hpwl(current_map.values(), result, rules.curve_tolerance, options) + _constraint_terms(
                    comp_now, result, current_map, rules.curve_tolerance)[0]
                tested_here = 0
                for cand in pool[1:]:
                    checkpoint("optimization")
                    if stats["candidate_checks"] >= options["max_candidates"] or tested_here >= per_comp_limit:
                        break
                    if _same_geometry(cand, best_comp, rules.curve_tolerance):
                        continue
                    if not in_region(cand):
                        continue
                    if other_union is not None and other_union.intersects(_envelope(cand, rules.curve_tolerance)):
                        continue
                    current_map[cid] = cand
                    cand_obj = _board_hpwl(current_map.values(), result, rules.curve_tolerance, options) + _constraint_terms(
                        cand, result, current_map, rules.curve_tolerance)[0]
                    if cand_obj + 1e-6 < best_obj:
                        stats["candidate_checks"] += 1
                        tested_here += 1
                        trial = _subset(result, [current_map[c["id"]] for c in result["components"]])
                        if not check_board(trial, ignore_connectivity=True):
                            best_comp = cand
                            best_obj = cand_obj
                            solution = trial
                            best_solution = trial
                            checkpoint("optimization_geometry")
                            break
                        checkpoint("optimization_geometry")
                    current_map[cid] = best_comp
                current_map[cid] = best_comp
            stats["final_hpwl"] = round(_board_hpwl(solution["components"], result, rules.curve_tolerance, options), 3)
        return solution

    try:
        solution = solve_legacy()
    except PlacementFailure as exc:
        if exc.diagnostics.get("budget_scope") not in ("time", "candidates") or best_solution is None:
            raise
        stats["budget_exhausted"] = True
        stats["budget_diagnostics"] = {k: v for k, v in exc.diagnostics.items() if k != "stats"}
        stats["retained_complete_candidate"] = True
        solution = best_solution
    return finish(solution)
