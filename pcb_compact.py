"""Choose a compact outline and placement seed from explicit physical candidates.

This proposes geometry; it does not resize the live board or issue a writeback
plan. Apply the reviewed native outline/poses, then export a fresh source before
routing. Footprints, pads, holes, locked poses and net declarations are preserved.
"""
from argparse import ArgumentParser
from copy import deepcopy
import json
from itertools import chain, islice
import math
from pathlib import Path
import time

from shapely.affinity import translate
from shapely.prepared import prep
from shapely.ops import unary_union

from pcb_placement import (_board_hpwl, _envelope, _options, _subset, _grid_points,
                           _net_weight, _copper_locked, place_components)
from pcb_pipeline_common import PipelineError, board_polygon, check_board, normalize_board, rules_for
from pcb_constraints import check_constraints


POSE = ("x", "y", "rotation", "layer")


def _number(value, label):
    """输入候选尺寸数值，返回有限浮点值；类型或溢出错误统一抛 PipelineError。"""
    try:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError
        return float(value)
    except (ValueError, TypeError, OverflowError) as exc:
        raise PipelineError(f"{label} 必须为有限数值") from exc


def _pad_number(pad):
    return str(pad.get("metadata", {}).get("pad_number", pad.get("metadata", {}).get("native_pad_id", pad["padId"])))


def _net_topology(board):
    endpoints = {(c["id"], p["padId"]): (c.get("metadata", {}).get("designator", c["id"]), _pad_number(p))
                 for c in board["components"] for p in c["pads"]}
    signatures = {}
    for net, pins in board["nets"].items():
        signature = frozenset(endpoints[(p["compId"], p["padId"])] for p in pins)
        if not signature or len(signature) != len(pins) or signature in signatures:
            raise PipelineError("Reference topology needs unique named component/pin endpoints")
        signatures[signature] = net
    return signatures


def _pad_signature(component, net_mapping=None):
    """Match pin function and local copper, independently of document IDs."""
    pads = {}
    for pad in component["pads"]:
        number = _pad_number(pad)
        if number in pads:
            raise PipelineError("Reference mapping requires unique physical pad numbers")
        pads[number] = {k: v for k, v in pad.items() if k not in ("padId", "metadata")}
        if net_mapping:
            pads[number]["net"] = net_mapping.get(pad["net"], pad["net"])
    return pads


def reference_poses(board, reference):
    """Transfer poses only after exact component/pin-function evidence matches."""
    reference = normalize_board(reference)
    source_topology, ref_topology = _net_topology(board), _net_topology(reference)
    if source_topology.keys() != ref_topology.keys():
        raise PipelineError("Reference net topology differs from the complete source endpoint sets")
    net_mapping = {ref_topology[pins]: net for pins, net in source_topology.items()}
    by_id = {c["id"]: c for c in reference["components"]}
    by_name = {}
    for component in reference["components"]:
        name = component.get("metadata", {}).get("designator")
        if name:
            by_name.setdefault(name, []).append(component)
    poses, mapping = {}, {}
    used = set()
    for component in board["components"]:
        name = component.get("metadata", {}).get("designator")
        match = by_id.get(component["id"])
        if match is None:
            matches = by_name.get(name, [])
            if len(matches) != 1:
                raise PipelineError(f"Reference has no unique component match for {name or component['id']}")
            match = matches[0]
        if match["id"] in used or _pad_signature(component) != _pad_signature(match, net_mapping):
            raise PipelineError(f"Reference pin geometry/network mismatch for {name or component['id']}")
        if match["layer"] != component["layer"]:
            raise PipelineError("Reference placement cannot silently flip a component side")
        used.add(match["id"])
        poses[component["id"]] = {k: match[k] for k in POSE}
        mapping[component["id"]] = match["id"]
    return poses, mapping, board_polygon(reference).bounds


def _layout_metrics(board, options=None):
    """输入完整候选和布局配置，返回面积、包络、HPWL及RUDY；非法输入沿检查器拒绝。"""
    rules = rules_for(board)
    polygon = board_polygon(board)
    envelopes = [_envelope(c, rules.curve_tolerance) for c in board["components"]]
    occupied = unary_union(envelopes)
    x0, y0, x1, y1 = polygon.bounds
    from pcb_global_place import evaluate_cost
    cost = evaluate_cost(board, options or {})
    return {
        "width_mil": x1 - x0, "height_mil": y1 - y0, "board_area_mil2": polygon.area,
        "occupied_envelope_area_mil2": occupied.intersection(polygon).area,
        "occupied_fraction": occupied.intersection(polygon).area / polygon.area,
        "envelope_bounds": list(occupied.bounds),
        "weighted_hpwl_mil": cost["raw"]["hpwl"],
        "rudy_peak": cost["rudy"]["peak"], "rudy_overflow": cost["rudy"]["overflow"],
        "congestion_passed": cost["rudy"]["passed"], "rudy_hotspots": cost["rudy"]["hotspots"],
        "body_or_pad_overhang_ids": [c["id"] for c, shape in zip(board["components"], envelopes) if not polygon.covers(shape)],
    }


def _pack_independent(board, options, fixed):
    """在板与显式分区交集中生成有限装箱候选，不改变完整输入。

    入参：完整板、规范化布局配置和不可移动器件集合。出参为完整候选板与
    次数/耗时统计，仍须由调用方执行完整精检。所有循环采用协作式时限，
    候选/时间预算耗尽抛带诊断的 PlacementFailure，不返回部分摆放。
    """
    from pcb_global_place import PlacementFailure
    started = time.monotonic()
    tests = 0

    def checkpoint():
        elapsed = time.monotonic() - started
        if elapsed >= options["max_seconds"]:
            raise PlacementFailure("PLACEMENT_TIME_BUDGET", "紧凑排布在总时限内未找到完整候选，未证明无解",
                                   budget_scope="time", phase="compact_packing", candidate_checks=tests,
                                   elapsed_seconds=elapsed)

    checkpoint()
    rules = rules_for(board)
    components = {c['id']: c for c in board['components']}
    envelopes = {cid: _envelope(c, rules.curve_tolerance) for cid, c in components.items()}
    available = board_polygon(board).buffer(-rules.board_edge_clearance)
    if available.is_empty:
        raise PipelineError('Compact outline has no usable interior')
    inside = prep(available)
    links = {cid: set() for cid in components}
    for rule in board.get('metadata', {}).get('pipeline_constraints', []):
        checkpoint()
        if rule.get('operator') == 'RELATIVE_POSE':
            a, b = rule['source']['compId'], rule['target']['compId']
            links[a].add(b); links[b].add(a)
    groups, unseen = [], set(components) - fixed
    while unseen:
        checkpoint()
        stack, group = [min(unseen)], set()
        while stack:
            checkpoint()
            cid = stack.pop()
            if cid not in unseen:
                continue
            unseen.remove(cid); group.add(cid); stack.extend(sorted(links[cid] & unseen))
        groups.append(sorted(group, key=lambda cid: (-envelopes[cid].area, cid)))
    groups.sort(key=lambda group: (-max(envelopes[cid].area for cid in group), group))
    placed = {cid: components[cid] for cid in fixed}
    placed_shapes = {cid: envelopes[cid] for cid in fixed}
    errors = check_board(_subset(board, placed.values()), ignore_connectivity=True)
    errors.extend(f"PLACEMENT_REGION: {cid} exceeds its declared region" for cid in placed
                  if cid in options["regions"] and not options["regions"][cid].covers(placed_shapes[cid]))
    checkpoint()
    if errors:
        raise PipelineError('Compact fixed geometry is invalid: ' + '; '.join(errors))
    for cid in (cid for group in groups for cid in group):
        checkpoint()
        original = components[cid]
        origin = [original['x'], original['y']]
        coupled = sorted(links[cid] & placed.keys())
        if coupled:
            origin = [origin[i] + sum(placed[k][('x','y')[i]] - components[k][('x','y')[i]] for k in coupled)/len(coupled) for i in range(2)]
        same_side = [shape.buffer(rules.component_courtyard_margin + rules.curve_tolerance)
                     for other, shape in placed_shapes.items() if placed[other]['layer'] == original['layer']]
        blocked = prep(unary_union(same_side)) if same_side else None
        region = options['regions'].get(cid)
        domain = available if region is None else available.intersection(region)
        if domain.is_empty:
            raise PlacementFailure('PLACEMENT_NO_CANDIDATE', '器件分区与可用板域没有交集，本次未找到候选', component=cid)
        neighbors = []
        my_nets = {p['net'] for p in original['pads'] if p['net']}
        for net in my_nets:
            checkpoint()
            peers = [c for c in placed.values() if any(p['net'] == net for p in c['pads'])]
            if peers:
                neighbors.append((sum(c['x'] for c in peers)/len(peers), sum(c['y'] for c in peers)/len(peers), _net_weight(board, net, options)))
        choices = []
        for rotation in dict.fromkeys([original['rotation'], *options['rotations']]):
            checkpoint()
            local = dict(original, x=0, y=0, rotation=rotation)
            shape = _envelope(local, rules.curve_tolerance)
            bx0, by0, bx1, by1 = domain.bounds
            sx0, sy0, sx1, sy1 = shape.bounds
            bounds = (bx0-sx0, by0-sy0, bx1-sx1, by1-sy1)
            # 按需读取近网格，避免检查预算之前批量创建整池。
            limit = max(1, options['max_candidates']//max(1, len(components))*4)
            pool = chain([(0, *origin)], islice(_grid_points(bounds, origin, options['grid_step']), limit))
            for distance, x, y in pool:
                checkpoint()
                if tests >= options['max_candidates']:
                    break
                tests += 1
                moved_shape = translate(shape, xoff=x, yoff=y)
                if not inside.covers(moved_shape) or (blocked is not None and blocked.intersects(moved_shape)):
                    continue
                if region is not None and not region.covers(moved_shape):
                    continue
                candidate = dict(original, x=x, y=y, rotation=rotation)
                partial = _subset(board, [*placed.values(), candidate])
                constraint_errors = check_constraints(partial, construction=True)
                checkpoint()
                if constraint_errors:
                    continue
                displacement = math.hypot(x-origin[0], y-origin[1])
                cost = displacement + sum(w * math.hypot(x-nx,y-ny) for nx,ny,w in neighbors)*0.15
                choices.append((cost, rotation, x, y, candidate, moved_shape))
                if len(choices) >= 12:
                    break
            if len(choices) >= 12 or tests >= options['max_candidates']:
                break
        if not choices:
            if tests >= options['max_candidates']:
                raise PlacementFailure("PLACEMENT_CANDIDATE_BUDGET", "紧凑排布候选预算内未找到完整布局，未证明无解",
                                       budget_scope="candidates", phase="compact_packing", component=cid,
                                       candidate_checks=tests)
            name = original.get('metadata', {}).get('designator', cid)
            raise PipelineError(f'Independent packing could not place {name} after {tests} bounded candidates')
        _, _, _, _, placed[cid], placed_shapes[cid] = min(choices, key=lambda item: item[:4])
    result = _subset(board, [placed[c['id']] for c in board['components']])
    checkpoint()
    return result, {'method':'independent_rigid_packing', 'candidate_checks':tests,
                    'coupled_groups':len(groups), 'reference_positions_used':False,
                    'elapsed_seconds': time.monotonic()-started, 'budget_exhausted': False}


def _auto_options(options, original):
    """输入自动尺寸对象和原板，返回严格规范化参数；未知字段、非法预算抛 PipelineError。"""
    defaults = dict(target_occupancy=0.65, aspect_ratio_range=[0.7, 1.6], aspect_ratio_steps=5,
                    area_iterations=8, max_candidates=48, max_seconds=60.0,
                    max_area=board_polygon(original).area, area_tolerance=0.02)
    if not isinstance(options, dict) or set(options) - set(defaults):
        raise PipelineError("auto_size 必须为仅含已声明字段的对象")
    defaults.update(deepcopy(options))
    for key in ("target_occupancy", "max_seconds", "max_area", "area_tolerance"):
        defaults[key] = _number(defaults[key], f"auto_size.{key}")
        if defaults[key] <= 0:
            raise PipelineError(f"auto_size.{key} 必须为正数")
    if defaults["target_occupancy"] > 1 or defaults["area_tolerance"] >= 1:
        raise PipelineError("target_occupancy 须在 (0,1]，area_tolerance 须在 (0,1)")
    for key, minimum, maximum in (("aspect_ratio_steps", 1, 64), ("area_iterations", 0, 32),
                                  ("max_candidates", 1, 512)):
        if type(defaults[key]) is not int or not minimum <= defaults[key] <= maximum:
            raise PipelineError(f"auto_size.{key} 必须为 [{minimum},{maximum}] 内的整数")
    span = defaults["aspect_ratio_range"]
    if not isinstance(span, (list, tuple)) or len(span) != 2:
        raise PipelineError("auto_size.aspect_ratio_range 需要 [最小宽高比,最大宽高比]")
    span = [_number(v, "auto_size.aspect_ratio_range") for v in span]
    if span[0] <= 0 or span[1] < span[0] or (span[0] != span[1] and defaults["aspect_ratio_steps"] < 2):
        raise PipelineError("宽高比须为递增正范围；非退化范围至少扫描两个比例")
    defaults["aspect_ratio_range"] = span
    return defaults


def _selection_key(entry):
    """返回几何合法、拥塞达标、面积及HPWL的确定性排序键。"""
    return (not entry["geometry_passed"], not entry.get("congestion_passed", False),
            entry["board_area_mil2"], entry["weighted_hpwl_mil"])


def _finish_compact(original, outcomes, reports, settings, reference, allow_reference_poses, mapping):
    """汇总有限候选，输入必须含已选结果；只返回预览提案，不写回来源。"""
    chosen, metrics = min(outcomes, key=lambda item: _selection_key(item[1]))
    chosen = deepcopy(chosen)
    changed_outline = chosen["board_bbox"] != original["board_bbox"]
    chosen.setdefault("metadata", {})["compact_proposal"] = {
        "requires_native_outline_update_and_reexport": changed_outline,
        "original_board_bbox": deepcopy(original["board_bbox"]), "reference_mapping": mapping,
        "reference_positions_used": allow_reference_poses,
        "local_footprint_geometry_scaled": False,
    }
    chosen["metadata"]["preview_only"] = True
    report = {"geometry_passed": metrics["geometry_passed"], "routing_verified": False,
              "global_optimum_proven": False, "preview_only": True, "full_board_accepted": False,
              "selection": "几何合法 → RUDY拥塞达标 → 板面积 → 加权HPWL",
              "before": _layout_metrics(original, settings), "selected": metrics, "candidates": reports,
              "reference_mapping": mapping, "source_identity_preserved": original.get("metadata", {}).get("source") == chosen.get("metadata", {}).get("source"),
              "reference_positions_used": allow_reference_poses}
    if reference is not None:
        # 样板只按自己的显式标签评价；不将当前板的器件/网络配置套到样板。
        report["reference_metrics"] = _layout_metrics(normalize_board(reference))
    return chosen, report


def _auto_compact(original, cfg, settings, raw_placement, *, reference, allow_reference_poses, preview):
    """按面积二分和宽高比扫描生成候选，返回最佳已测提案及明确预算记录。

    下界为原始器件包络总面积除以目标占用率，默认上界为原板面积。
    可行性是有限启发式的观测，不假定严格单调，不宣称二分证明最小板框。
    所有实测端点和中点均保留；超时有合法候选则返回该候选并标记预算耗尽，
    未找到合法候选时抛 PipelineError，diagnostics 保留已测尺寸与失败原因。
    """
    started = time.monotonic()
    deadline = started + cfg["max_seconds"]
    rules = rules_for(original)
    envelope_area = sum(_envelope(c, rules.curve_tolerance).area for c in original["components"])
    lower, upper = envelope_area / cfg["target_occupancy"], cfg["max_area"]
    if not math.isfinite(lower) or lower <= 0:
        raise PipelineError("自动尺寸包络面积下界不是有限正数")
    if lower > upper:
        raise PipelineError("自动尺寸范围内未找到候选：包络面积/目标占用率超过面积上界；未证明无解，也未自动扩板")
    lo_ratio, hi_ratio = cfg["aspect_ratio_range"]
    ratios = ([lo_ratio] if lo_ratio == hi_ratio else
              [lo_ratio + (hi_ratio-lo_ratio)*i/(cfg["aspect_ratio_steps"]-1)
               for i in range(cfg["aspect_ratio_steps"])])
    ranges = {ratio: [lower, upper] for ratio in ratios}
    reports, outcomes, seen = [], [], {}
    exhausted = None
    used_placement = []

    def evaluate(area, ratio, phase):
        nonlocal exhausted
        try:
            width, height = math.sqrt(area) * math.sqrt(ratio), math.sqrt(area) / math.sqrt(ratio)
        except (OverflowError, ZeroDivisionError) as exc:
            raise PipelineError("自动尺寸比例超出有限坐标范围") from exc
        if not all(math.isfinite(v) and v > 0 for v in (width, height)):
            raise PipelineError("自动尺寸比例超出有限坐标范围")
        key = (width, height)
        if key in seen:
            return seen[key]
        if len(reports) >= cfg["max_candidates"]:
            exhausted = "max_candidates"
            return None
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            exhausted = "max_seconds"
            return None
        opts = deepcopy(raw_placement or {})
        opts["method"] = "analytic_sa"
        for name, value in (("global_iterations", 10), ("sa_temperatures", 2),
                            ("sa_steps_per_temperature", 5), ("cluster_intra_steps", 0),
                            ("max_candidates", 800)):
            opts.setdefault(name, value)
        share = cfg["max_seconds"] / max(1, min(cfg["max_candidates"], len(ratios)*(cfg["area_iterations"]+2)))
        opts["max_seconds"] = min(opts.get("max_seconds", remaining), remaining, share)
        used_placement.append({"width_mil": width, "height_mil": height, "options": deepcopy(opts)})
        try:
            candidate, report = compact_layout(original, [[width, height]], reference=reference,
                allow_reference_poses=allow_reference_poses, contract_centers=False,
                legalize=True, placement_options=opts, preview=True)
            entry = deepcopy(report["selected"])
            entry.update(requested_area_mil2=area, aspect_ratio=ratio, search_phase=phase)
            outcomes.append((candidate, entry))
        except PipelineError as exc:
            entry = dict(width_mil=width, height_mil=height, requested_area_mil2=area,
                         aspect_ratio=ratio, search_phase=phase, geometry_passed=False,
                         congestion_passed=False, violations=[str(exc)])
        reports.append(entry)
        passed = bool(entry["geometry_passed"] and entry.get("congestion_passed", False))
        seen[key] = passed
        return passed

    # 所有比例先测边界，再分轮二分，避免先耗尽单个比例的整个预算。
    for phase, area in (("lower_bound", lower), ("upper_bound", upper)):
        for ratio in ratios:
            evaluate(area, ratio, phase)
            if exhausted:
                break
        if exhausted:
            break
    for iteration in range(cfg["area_iterations"]):
        if exhausted:
            break
        active = False
        for ratio in ratios:
            low, high = ranges[ratio]
            if high - low <= cfg["area_tolerance"] * max(high, 1.0):
                continue
            active = True
            middle = (low + high) / 2
            result = evaluate(middle, ratio, f"bisection_{iteration+1}")
            if exhausted:
                break
            if result:
                ranges[ratio][1] = middle
            else:
                ranges[ratio][0] = middle
        if not active:
            break
    if time.monotonic() >= deadline:
        exhausted = exhausted or "max_seconds"
    auto_report = dict(options=deepcopy(cfg), envelope_sum_area_mil2=envelope_area,
                       lower_bound_area_mil2=lower, upper_bound_area_mil2=upper,
                       tested_candidates=len(reports), aspect_ratios=ratios,
                       remaining_area_intervals=[dict(aspect_ratio=r, lower=v[0], upper=v[1]) for r,v in ranges.items()],
                       elapsed_seconds=time.monotonic()-started, budget_exhausted=bool(exhausted),
                       stopped_by=exhausted or "configured_area_search_complete", placement_runs=used_placement,
                       monotonic_feasibility_proven=False, global_optimum_proven=False)
    legal = [(b, r) for b, r in outcomes if r["geometry_passed"]]
    eligible = legal or (outcomes if preview else [])
    if not eligible:
        error = PipelineError("自动尺寸的显式面积、比例及搜索预算内未找到合法布局，未证明无解")
        error.diagnostics = {"code": "COMPACT_SIZE_BUDGET", "auto_size": auto_report, "candidates": reports}
        raise error
    mapping = (reference_poses(original, reference)[1] if allow_reference_poses else {})
    chosen, report = _finish_compact(original, eligible, reports, settings, reference, allow_reference_poses, mapping)
    auto_report["elapsed_seconds"] = time.monotonic() - started
    if time.monotonic() >= deadline:
        auto_report["budget_exhausted"] = True
        auto_report["stopped_by"] = exhausted or "max_seconds"
    report["auto_size"] = auto_report
    return chosen, report


def compact_layout(board, candidates=None, *, reference=None, allow_reference_poses=False, contract_centers=None,
                   legalize=True, placement_options=None, preview=False, auto_size=None):
    """输入完整板、手工尺寸列表或自动尺寸配置，返回独立布局预览与比较报告。

    手工模式保留原有限装箱；显式 analytic_sa 和 auto_size 使用新全局/退火布局。
    器件、焊盘、孔与簇内尺寸不缩放，不删除既有铜、不解锁固定器件。
    样板默认只提供指标。参数非法或有限候选未找到合法结果抛 PipelineError；
    preview=True 才可返回有明确违规记录的未验收提案。最终仍须实际改框后重导来源。
    """
    original = normalize_board(board)
    if not original["components"]:
        raise PipelineError("Compact placement needs physical components")
    if placement_options and "initial_poses" in placement_options:
        raise PipelineError("Supply the current board's poses directly, not a second initial_poses map")
    if type(allow_reference_poses) is not bool or (allow_reference_poses and reference is None):
        raise PipelineError("allow_reference_poses requires a reference and an explicit boolean")
    if contract_centers is not None and type(contract_centers) is not bool:
        raise PipelineError("contract_centers 必须为布尔值或 None")
    if auto_size is not None:
        if candidates is not None:
            raise PipelineError("auto_size 与手工 candidates 互斥")
        if contract_centers is True:
            raise PipelineError("auto_size 由全局布局合法化，不接受 contract_centers=True")
        if not legalize:
            raise PipelineError("自动尺寸必须运行快速布局，不能关闭合法化")
        if placement_options is not None and not isinstance(placement_options, dict):
            raise PipelineError("placement_options 必须为对象")
        if (placement_options or {}).get("method", "analytic_sa") != "analytic_sa":
            raise PipelineError("自动尺寸使用 analytic_sa；需要 legacy 时请使用手工尺寸")
        placement_options = dict(placement_options or {}, method="analytic_sa")
    settings = _options(placement_options, original, rules_for(original))
    if not settings["enabled"]:
        raise PipelineError("Compact placement requires enabled placement")
    routes = original.get("routes", {})
    if any(routes.get(k) for k in ("traces", "copper_pours", "shield_vias")):
        raise PipelineError("Compact placement needs an unrouted source; remove authorized old copper and re-export first")
    if any(not v.get("metadata", {}).get("parent_component_id") for v in routes.get("vias", [])):
        raise PipelineError("Independent existing vias cannot be moved or dropped by compact placement")
    if auto_size is not None:
        return _auto_compact(original, _auto_options(auto_size, original), settings, placement_options,
                            reference=reference, allow_reference_poses=allow_reference_poses, preview=preview)
    if not isinstance(candidates, (list, tuple)) or not 1 <= len(candidates) <= 32:
        raise PipelineError("Provide 1..32 explicit compact outline candidates")
    dimensions = []
    for pair in candidates:
        if not isinstance(pair, (list, tuple)) or len(pair) != 2:
            raise PipelineError("Each compact candidate needs width,height in mil")
        width, height = [_number(v, "candidate dimension") for v in pair]
        if min(width, height) <= 0:
            raise PipelineError("Candidate dimensions must be positive")
        dimensions.append((width, height))
    old_bounds = board_polygon(original).bounds
    if allow_reference_poses:
        poses, mapping, seed_bounds = reference_poses(original, reference)
    else:
        poses = {c["id"]: {k: c[k] for k in POSE} for c in original["components"]}
        mapping, seed_bounds = {}, old_bounds
    contract_centers = not allow_reference_poses if contract_centers is None else contract_centers
    if type(contract_centers) is not bool:
        raise PipelineError("contract_centers must be boolean")
    tags = original.get("tags", {}).get("component_tags", {})
    fixed = {c["id"] for c in original["components"] if c.get("locked") or c.get("fixed") or "fixed_pose" in c
             or tags.get(c["id"], {}).get("fixed") or "fixed_pose" in tags.get(c["id"], {})}
    fixed.update(c["id"] for c in original["components"] if c["id"] not in settings["movable_ids"])
    fixed.update(_copper_locked(original, rules_for(original).contact_tolerance))
    reports, outcomes = [], []
    sx0, sy0, sx1, sy1 = seed_bounds
    for width, height in sorted(set(dimensions), key=lambda p: (p[0] * p[1], p)):
        trial = deepcopy(original)
        x0, y0 = old_bounds[:2]
        trial["board_bbox"] = {"outline": [[x0, y0], [x0 + width, y0], [x0 + width, y0 + height], [x0, y0 + height]],
                               "cutouts": deepcopy(original["board_bbox"].get("cutouts", [])) if isinstance(original["board_bbox"], dict) else []}
        center_x, center_y = x0 + width / 2, y0 + height / 2
        factor_x = width / (sx1 - sx0) if contract_centers else 1
        factor_y = height / (sy1 - sy0) if contract_centers else 1
        seeded_components = deepcopy(trial["components"])
        for component in seeded_components:
            if component["id"] in fixed:
                continue
            # 新算法自行处理出界种子；逐器件缩中心会破坏显式刚体簇。
            if settings["method"] == "analytic_sa" and not allow_reference_poses:
                continue
            pose = deepcopy(poses[component["id"]])
            pose["x"] = center_x + (pose["x"] - (sx0 + sx1) / 2) * factor_x
            pose["y"] = center_y + (pose["y"] - (sy0 + sy1) / 2) * factor_y
            component.update(pose)
        trial = _subset(trial, seeded_components)
        errors = check_board(trial, ignore_connectivity=True)
        errors.extend(f"PLACEMENT_REGION: {c['id']} exceeds its declared region" for c in trial["components"]
                      if c["id"] in settings["regions"] and not settings["regions"][c["id"]].covers(_envelope(c, rules_for(trial).curve_tolerance)))
        stats = {"method": "explicit_reference_seed" if allow_reference_poses else "independent_center_contraction_seed",
                 "reference_positions_used": allow_reference_poses}
        if legalize and settings["method"] == "analytic_sa":
            try:
                trial, stats = place_components(trial, placement_options)
                errors = check_board(trial, ignore_connectivity=True)
                errors.extend(f"PLACEMENT_REGION: {c['id']} exceeds its declared region" for c in trial["components"]
                              if c["id"] in settings["regions"] and not settings["regions"][c["id"]].covers(_envelope(c, rules_for(trial).curve_tolerance)))
            except PipelineError as error:
                stats["legalization_error"] = str(error)
                if hasattr(error, "diagnostics"):
                    stats["legalization_diagnostics"] = deepcopy(error.diagnostics)
                # 算法拒绝不能被原种子合法掩盖成新算法成功。
                errors = list(errors) + [str(error)]
        elif errors and legalize:
            try:
                trial, stats = _pack_independent(trial, settings, fixed)
                errors = check_board(trial, ignore_connectivity=True)
            except PipelineError as error:
                stats["legalization_error"] = str(error)
                if hasattr(error, "diagnostics"):
                    stats["legalization_diagnostics"] = deepcopy(error.diagnostics)
        try:
            metrics = _layout_metrics(trial, settings)
        except (PipelineError, ValueError) as error:
            reports.append({"width_mil": width, "height_mil": height, "geometry_passed": False,
                            "violations": errors + [str(error)], "placement": stats})
            continue
        entry = dict(metrics, geometry_passed=not errors, violations=errors, placement=stats)
        reports.append(entry)
        outcomes.append((trial, entry))
    if not outcomes:
        raise PipelineError("No candidate preserves a valid physical outline and its cutouts")
    legal = [(b, r) for b, r in outcomes if r["geometry_passed"]]
    if not legal and not preview:
        raise PipelineError("No legal compact candidate; inspect mechanical overhang, fixed poses, constraints and candidate sizes")
    return _finish_compact(original, legal or outcomes, reports, settings, reference, allow_reference_poses, mapping)


def main(argv=None):
    """解析手工或自动尺寸CLI并保存离线提案；配置/文件异常按原类型传播。"""
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("--board", required=True)
    parser.add_argument("--reference")
    parser.add_argument("--allow-reference-poses", action="store_true", help="Only with explicit user authorization; by default reference supplies metrics only")
    size_mode = parser.add_mutually_exclusive_group(required=True)
    size_mode.add_argument("--size", nargs=2, action="append", type=float, metavar=("WIDTH_MIL", "HEIGHT_MIL"))
    size_mode.add_argument("--auto-size", action="store_true", help="按包络面积下界、面积二分与宽高比扫描自动比较尺寸")
    parser.add_argument("--target-occupancy", type=float, help="包络总面积/板面积的目标比例，默认0.65")
    parser.add_argument("--aspect-ratio-range", nargs=2, type=float, metavar=("MIN", "MAX"))
    parser.add_argument("--aspect-ratio-steps", type=int)
    parser.add_argument("--area-iterations", type=int)
    parser.add_argument("--size-max-candidates", type=int)
    parser.add_argument("--size-max-seconds", type=float)
    parser.add_argument("--max-area", type=float, help="显式板面积上界，单位mil²；默认原板面积，不自动扩板")
    parser.add_argument("--area-tolerance", type=float)
    parser.add_argument("--contract-centers", action="store_true", default=None)
    parser.add_argument("--no-legalize", action="store_true")
    parser.add_argument("--preview", action="store_true")
    parser.add_argument("--placement-config", help="JSON placement options only")
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args(argv)
    auto_fields = {"target_occupancy": args.target_occupancy, "aspect_ratio_range": args.aspect_ratio_range,
                   "aspect_ratio_steps": args.aspect_ratio_steps, "area_iterations": args.area_iterations,
                   "max_candidates": args.size_max_candidates, "max_seconds": args.size_max_seconds,
                   "max_area": args.max_area, "area_tolerance": args.area_tolerance}
    if not args.auto_size and any(value is not None for value in auto_fields.values()):
        parser.error("自动尺寸参数只能与 --auto-size 一起使用")
    auto = {key: value for key, value in auto_fields.items() if value is not None} if args.auto_size else None
    def read(path):
        value = json.loads(Path(path).read_text(encoding="utf-8-sig"))
        return value.get("board", value)
    board, report = compact_layout(read(args.board), args.size,
        reference=read(args.reference) if args.reference else None, allow_reference_poses=args.allow_reference_poses, contract_centers=args.contract_centers,
        legalize=not args.no_legalize, placement_options=read(args.placement_config) if args.placement_config else None,
        preview=args.preview, auto_size=auto)
    output = Path(args.output_dir)
    output.mkdir(parents=True, exist_ok=False)
    for name, value in (("board.json", board), ("result.json", report),
                        ("placement-proposal.json", {"board_bbox": board["board_bbox"],
                            "initial_poses": {c["id"]: {k: c[k] for k in POSE} for c in board["components"]},
                            "reference_positions_used": report["reference_positions_used"],
                            "requires_native_outline_update_and_reexport": True})):
        (output / name).write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    print(json.dumps({"geometry_passed": report["geometry_passed"], "selected": report["selected"], "output_dir": str(output)}, ensure_ascii=False))
    return 0 if report["geometry_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
