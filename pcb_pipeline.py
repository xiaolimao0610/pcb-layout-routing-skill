"""Runnable PCB physical-design pipeline; bounded heuristics, explicit acceptance."""
from __future__ import annotations

import argparse
from copy import deepcopy
import json
import math
import re
from pathlib import Path
import sys
import time
import uuid

from shapely.geometry import LineString, Polygon
from shapely.ops import split, unary_union
from shapely import constrained_delaunay_triangles
from shapely.errors import GEOSException

from pcb_pipeline_common import (
    PipelineError, normalize_board, check_board, strict_options, fingerprint,
    board_polygon, copper_and_drills, polygons, polygon_record, attached_vias_for_components,
    rules_for, _fast_pad_bounds,
)
from pcb_placement import place_components
from pcb_routing import route_board, check_routed_nets, deferred_ground_nets
from pcb_copper import generate_pours, generate_shielding


def _no_holes(polygon):
    """Cut holes open without filling them; preserve exact shared boundaries."""
    queue, result = [polygon], []
    while queue:
        part = queue.pop()
        if not part.interiors:
            result.append(part)
            continue
        hole = Polygon(part.interiors[0])
        ring = list(part.interiors[0].coords)[:-1]
        outer = list(part.exterior.coords)[:-1]
        extreme = [min(range(len(ring)), key=lambda i: ring[i][0]),
                   max(range(len(ring)), key=lambda i: ring[i][0]),
                   min(range(len(ring)), key=lambda i: ring[i][1]),
                   max(range(len(ring)), key=lambda i: ring[i][1])]
        pairs = [(ring[extreme[0]], ring[extreme[1]]), (ring[extreme[2]], ring[extreme[3]])]
        pairs += [(ring[i], ring[(i + 1) % len(ring)]) for i in extreme]
        pieces = None
        # Bridges use existing boundary vertices. Arbitrary vertical cuts can
        # create rounding slivers; do not silently accept a changed copper set.
        for a, b in pairs:
            if a == b or not hole.covers(LineString([a, b])):
                continue
            visible_a = [v for v in sorted(outer, key=lambda v: math.dist(v, a))
                         if part.covers(LineString([v, a]))][:6]
            visible_b = [v for v in sorted(outer, key=lambda v: math.dist(v, b))
                         if part.covers(LineString([b, v]))][:6]
            for va in visible_a:
                for vb in visible_b:
                    if va == vb:
                        continue
                    cut = LineString([va, a, b, vb])
                    if not cut.is_simple:
                        continue
                    trial = polygons(split(part, cut))
                    if (len(trial) > 1 and sum(len(p.interiors) for p in trial) < len(part.interiors)
                            and unary_union(trial).equals(part)):
                        pieces = trial
                        break
                if pieces:
                    break
            if pieces:
                break
        if not pieces:
            pieces = polygons(constrained_delaunay_triangles(part))
            if not pieces or any(p.interiors for p in pieces):
                raise PipelineError("cannot decompose a holed copper region for EDA Fill")
        queue.extend(pieces)
        if len(queue) + len(result) > 4096:
            raise PipelineError("EDA fill decomposition exceeds 4096 pieces")
    rebuilt = unary_union(result)
    if not rebuilt.equals(polygon):
        # No geometric tolerance is used to fill a lost hole or bridge a gap.
        raise PipelineError("EDA fill decomposition did not preserve exact copper")
    return result


def _trace_replacement_delta(original, solved, allowed_ids):
    """核对显式旧线替换，返回保留前缀和删除ID；不修改输入。

    入参为同源原板、候选板及允许替换的原线ID列表。逐项保护锁定、派生铜、
    原线两端、网名/层/宽度及全部原端子连接；不支持的变化抛PipelineError。
    本函数只生成计划前证据，仍须经过原生来源、备份、几何读回和DRC。
    """
    from pcb_routing import _groups
    for field in ("units", "nets", "components", "rules", "board_bbox", "tags"):
        if original.get(field) != solved.get(field):
            raise PipelineError("旧线替换不得改变原板不变量：" + field)
    for key, value in original.get("metadata", {}).items():
        if key not in ("preview_only", "native_ground_workflow") and solved.get("metadata", {}).get(key) != value:
            raise PipelineError("旧线替换不得改变原来源、板框或约束证据：" + key)
    if (not isinstance(allowed_ids, list) or any(not isinstance(v, str) or not v for v in allowed_ids)
            or len(set(allowed_ids)) != len(allowed_ids)):
        raise PipelineError("旧线替换白名单必须为不重复的原生ID列表")
    before = original.get("routes", {}).get("traces", [])
    after = solved.get("routes", {}).get("traces", [])
    ids = [t.get("id") for t in before if t.get("id")]
    if len(ids) != len(set(ids)) or set(allowed_ids) - set(ids):
        raise PipelineError("旧线替换ID不存在或原板ID重复")
    by_id = {t["id"]: t for t in before if t.get("id")}
    present = {t.get("id"): t for t in after if t.get("id")}
    if len(present) != sum(bool(t.get("id")) for t in after) or set(present) - set(by_id):
        raise PipelineError("替换候选包含重复或伪造的原线ID")
    removed = [t["id"] for t in before if t.get("id") and t["id"] not in present]
    if not removed or set(removed) - set(allowed_ids):
        raise PipelineError("候选改变了白名单以外的旧线或没有可验证删除对象")
    preserved = [t for t in before if t.get("id") not in removed]
    if after[:len(preserved)] != preserved:
        raise PipelineError("旧线替换必须完整保留其他原线及其顺序")
    added = after[len(preserved):]
    keys = {"net", "layer", "sx", "sy", "ex", "ey", "width"}
    if any(set(t) != keys for t in added):
        raise PipelineError("替换与新增线只能包含无ID的规范LINE几何")
    pose = lambda board: [(c["id"], c["x"], c["y"], c["rotation"], c["layer"]) for c in board["components"]]
    if pose(original) != pose(solved):
        raise PipelineError("本版旧线推挤写回不得同时移动器件，须分阶段重新取源")
    refs = {ref for p in original.get("routes", {}).get("copper_pours", [])
            for ref in p.get("metadata", {}).get("refs", [])}
    tolerance = rules_for(original).contact_tolerance
    for tid in removed:
        old = by_id[tid]
        if old.get("locked") or old.get("fixed") or tid in refs or old.get("metadata", {}).get("parent_component_id"):
            raise PipelineError("旧线替换涉及锁定、附属或派生铜引用对象")
        paths = [LineString([(t["sx"], t["sy"]), (t["ex"], t["ey"])]) for t in added
                 if all(t[k] == old[k] for k in ("net", "layer", "width"))]
        from shapely.geometry import Point
        if not paths or any(min(p.distance(Point(old[x], old[y])) for p in paths) > tolerance
                            for x, y in (("sx", "sy"), ("ex", "ey"))):
            raise PipelineError("旧线替换未保持原线网名、层、宽度和两端接入位置")
        # 在同宽中心线图上独立证明两端相连，不能用两端短粗线掩盖中间缩颈。
        frontier = [i for i, p in enumerate(paths) if p.distance(Point(old["sx"], old["sy"])) <= tolerance]
        reached = set(frontier)
        while frontier:
            current = frontier.pop()
            for index, path in enumerate(paths):
                if index not in reached and paths[current].intersects(path):
                    reached.add(index)
                    frontier.append(index)
        if not any(paths[index].distance(Point(old["ex"], old["ey"])) <= tolerance for index in reached):
            raise PipelineError("旧线替换的同网同层同宽路径没有连接原两端")
    errors = check_board(solved, ignore_connectivity=True)
    if errors:
        raise PipelineError("旧线替换几何复核失败：" + "; ".join(errors[:8]))
    for net in {by_id[tid]["net"] for tid in removed}:
        old_groups, new_groups = _groups(original, net), _groups(solved, net)
        if len(new_groups) > len(old_groups) or any(
                group["terminals"] and not any(group["terminals"] <= other["terminals"] for other in new_groups)
                for group in old_groups):
            raise PipelineError("旧线替换破坏原有实体铜连接：" + net)
    return preserved, removed


def build_writeback_plan(original, solved, *, allowed_trace_replacements=None):
    """生成同源增量计划；默认保留原铜，显式白名单才允许经复核的旧LINE替换。

    入参为原板、候选板及可选原线ID白名单；返回现有适配器支持的计划。
    来源、几何或受保护对象不符抛PipelineError；不直接执行任何原生操作。
    """
    source = original.get("metadata", {}).get("source", {})
    target = deepcopy(solved)
    for kind in ("holes", "keepouts"):
        if original.get("routes", {}).get(kind, []) != target.get("routes", {}).get(kind, []):
            raise PipelineError(f"writeback would change existing {kind}; refused")
    add = {"traces": [], "vias": [], "copper_pours": []}
    removed_traces = []
    for kind in ("traces", "vias", "shield_vias", "copper_pours"):
        before = original.get("routes", {}).get(kind, [])
        after = target.get("routes", {}).get(kind, [])
        preserved = attached_vias_for_components(original, target["components"]) if kind == "vias" else before
        if kind == "traces" and after[:len(before)] != preserved and allowed_trace_replacements is not None:
            preserved, removed_traces = _trace_replacement_delta(original, target, allowed_trace_replacements)
        if after[:len(preserved)] != preserved:
            raise PipelineError(f"writeback would change existing {kind}; refused")
        generated = after[len(preserved):]
        writable = []
        if kind == "copper_pours":
            for pour in generated:
                if pour.get("source_kind") == "Teardrop":
                    if not pour.get("metadata", {}).get("source_only") or not all(
                            "native_path" in region for region in pour["filled_polygons"]):
                        raise PipelineError("native teardrop target needs source-only path evidence")
                    # Native EDA derives this copper. Expect its full geometry in
                    # target_board; do not create a duplicate Fill over it.
                    continue
                filled = []
                for region in pour["filled_polygons"]:
                    if "native_path" in region:
                        raise PipelineError("curved native copper cannot be created by the Fill API")
                    from pcb_geometry import solid_polygon
                    for piece in _no_holes(solid_polygon(region["points"], region.get("holes", []))):
                        filled.append({"points": polygon_record(piece)["points"]})
                pour["filled_polygons"] = filled
                writable.append(pour)
        else:
            writable = generated
        add["vias" if kind == "shield_vias" else kind].extend(deepcopy(writable))
    components = []
    old = {c["id"]: c for c in original["components"]}
    for c in target["components"]:
        pose = {key: c[key] for key in ("x", "y", "rotation", "layer")}
        if any(pose[k] != old[c["id"]][k] for k in pose):
            components.append({"id": c["id"], **pose})
    plan_source = {"document_uuid": source.get("document_uuid"), "fingerprint": source.get("fingerprint")}
    usages = []
    for item in (original, solved):
        meta = item.get("metadata", {})
        usages.extend((meta.get("board_usage"), meta.get("source", {}).get("usage")))
    if any(value not in (None, "physical", "test_only") for value in usages):
        raise PipelineError("unknown board/source usage; cannot create writeback plan")
    if "test_only" in usages:
        plan_source["usage"] = "test_only"
    elif source.get("usage") == "physical":
        plan_source["usage"] = "physical"
    return {
        "schema_version": 1, "plan_id": str(uuid.uuid4()),
        "source": plan_source,
        "components": components, "add": add,
        "remove": {"traces": removed_traces, "vias": [], "copper_pours": []},
        "target_board": target,
    }


def review_native_copper(original, plan, receipt):
    """Independently check observed derived copper before issuing a new plan.

    The previous attempt must have failed its strict target comparison and
    restored exactly the same native source. Nothing is accepted in EDA here.
    """
    original = normalize_board(original)
    if not isinstance(plan, dict) or not isinstance(receipt, dict):
        raise PipelineError("native copper review needs a plan and full failure receipt")
    source = original.get("metadata", {}).get("source")
    if (not isinstance(source, dict) or not source.get("document_uuid")
            or not source.get("fingerprint") or source != plan.get("source")):
        raise PipelineError("native copper review source differs from the original plan")
    if receipt.get("status") != "failed" or receipt.get("plan") != plan or not isinstance(receipt.get("result"), dict):
        raise PipelineError("native copper review needs the full failed audit receipt bound to this exact plan")
    result = receipt["result"]
    rollback = result.get("rollback", {})
    if (result.get("success") is not False or result.get("error", {}).get("code") != "TARGET_MISMATCH"
            or result.get("plan_id") != plan.get("plan_id") or not plan.get("plan_id")
            or rollback.get("restored") is not True or rollback.get("requires_reexport") is not False
            or rollback.get("source") != source):
        raise PipelineError("native copper review requires a matching failed plan and proven exact source restoration")
    observed = result.get("generated_native_copper")
    if (not isinstance(observed, dict) or observed.get("schema_version") != 1
            or observed.get("source_only") is not True or observed.get("base_source") != source
            or observed.get("evidence", {}).get("source_only") is not True
            or not isinstance(observed.get("geometry_signature_without_generated"), str)
            or not re.fullmatch(r"sha256:[0-9a-f]{64}", observed["geometry_signature_without_generated"])
            or not isinstance(observed.get("source"), dict)
            or observed["source"].get("document_uuid") != source["document_uuid"]):
        raise PipelineError("missing native-only copper observation evidence")
    candidate = normalize_board(plan.get("target_board"))
    if original.get("rules", {}) != candidate.get("rules", {}):
        raise PipelineError("native copper review cannot use a target with different source rules; re-solve")
    additions = observed.get("copper_pours")
    if not isinstance(additions, list) or not additions:
        raise PipelineError("native copper review needs nonempty observed copper")
    existing = candidate["routes"].setdefault("copper_pours", [])
    ids = {p.get("id") for p in existing if p.get("id") is not None}
    for item in additions:
        if (not isinstance(item, dict) or item.get("source_kind") != "Teardrop"
                or item.get("locked") is not True or not item.get("metadata", {}).get("source_only")
                or not isinstance(item.get("id"), str) or item["id"] in ids
                or item.get("net") not in candidate["nets"]
                or not isinstance(item.get("filled_polygons"), list) or not item["filled_polygons"]
                or any("native_path" not in region for region in item["filled_polygons"])):
            raise PipelineError("invalid or duplicate source-only native copper observation")
        ids.add(item["id"])
        existing.append(deepcopy(item))
    # Preserve the same parent-movement protection as a subsequent native
    # export. A derived teardrop cannot follow a separately moved component.
    via_parents = {v.get("id"): v.get("metadata", {}).get("parent_component_id")
                   for v in candidate["routes"].get("vias", []) if v.get("id")}
    for component in candidate["components"]:
        refs = {p["padId"] for p in component["pads"]}
        refs.update(pid for pid, parent in via_parents.items() if parent == component["id"])
        attached = [p["id"] for p in existing if p.get("source_kind") == "Teardrop"
                    and refs.intersection(p.get("metadata", {}).get("refs", []))]
        if attached:
            component["fixed"] = True
            component.setdefault("metadata", {}).update(native_teardrops_fixed=True,
                                                        attached_teardrop_ids=attached)
            tag = candidate.setdefault("tags", {}).setdefault("component_tags", {}).setdefault(component["id"], {})
            tag.update(fixed=True, fixed_pose={key: component[key] for key in ("x", "y", "rotation", "layer")})
    errors = check_board(candidate)
    # Sidecar constraints may become stronger without changing native source
    # bytes. Verify the same copper under the original snapshot as well.
    original_by_id = {c["id"]: c for c in original["components"]}
    if set(original_by_id) != {c["id"] for c in candidate["components"]}:
        raise PipelineError("native copper review cannot add or remove input components")
    baseline = deepcopy(original)
    poses = {c["id"]: c for c in candidate["components"]}
    for component in baseline["components"]:
        for key in ("x", "y", "rotation", "layer"):
            component[key] = poses[component["id"]][key]
    baseline["routes"] = deepcopy(candidate["routes"])
    for kind in ("holes", "keepouts"):
        baseline["routes"][kind] = deepcopy(original.get("routes", {}).get(kind, []))
    errors += check_board(baseline)
    if errors:
        raise PipelineError("observed native copper failed independent verification: " + "; ".join(errors))
    return candidate, build_writeback_plan(original, candidate)


def _task(task):
    task = strict_options(task, {"schema_version", "board", "options", "constraints"}, "task")
    if task.get("schema_version") != 1 or type(task.get("schema_version")) is not int:
        raise PipelineError("task.schema_version must be 1")
    board = normalize_board(task.get("board"))
    options = strict_options(task.get("options", {}),
        {"placement", "routing", "pours", "shielding", "cleanup", "return_path", "max_iterations", "automation"}, "pipeline options")
    iterations = options.get("max_iterations", 3)
    if type(iterations) is not int or not 1 <= iterations <= 20:
        raise PipelineError("max_iterations must be an integer in [1,20]")
    from pcb_constraints import compile_constraints
    constraints = task.get("constraints", board.get("metadata", {}).get("pipeline_constraints", []))
    board, options, compiled = compile_constraints(board, options, constraints)
    if "automation" in options:
        from pcb_automation import validate_options as validate_automation
        validate_automation(options["automation"], options)
    for quality_stage in ("cleanup", "return_path"):
        if quality_stage in options and not isinstance(options[quality_stage], dict):
            raise PipelineError(f"options.{quality_stage} 必须为显式配置对象")
    if "cleanup" in options:
        from pcb_cleanup import validate_options as validate_cleanup
        validate_cleanup(options["cleanup"], board)
    if "return_path" in options:
        from pcb_return_path import validate_options as validate_return_path
        validate_return_path(options["return_path"], board)
    deferred = deferred_ground_nets(board, options.get("routing", {}))
    shielding = options.get("shielding", {})
    if not isinstance(shielding, dict):
        raise PipelineError("shielding must be an object")
    if deferred and (options.get("pours") or shielding.get("enabled", bool(shielding.get("nets")))):
        raise PipelineError("plane_then_drc requires native copper/ground repair after signals; omit offline pours/shielding")
    for net, tag in board.get("tags", {}).get("net_tags", {}).items():
        if tag.get("beta_repel", 0):
            raise PipelineError(f"UNSUPPORTED_RULE: {net}.beta_repel soft field is not implemented; use explicit clearances/keepouts")
        if tag.get("need_shield_check") and (not shielding.get("enabled", bool(shielding.get("nets"))) or net not in shielding.get("nets", [])):
            raise PipelineError(f"UNSUPPORTED_RULE: {net}.need_shield_check needs an explicit geometric COMPANION_GEN/shielding request")
    return board, options, compiled


def _placement_feedback(board, placement, diagnostics, previous=None, *, explicit=None, audit=None):
    """从实际布线失败和布局拥塞提取下一轮局部退火罚区，不修改工程。

    入参：失败时完整板、布局配置、结构化失败诊断、此前反馈、原始显式
    反馈及可选审计字典。省略 explicit 时将 previous 视为显式区域。
    出参：符合 placement.feedback 契约的 failed_nets/hotspots 字典。
    显式区域全部保留，再优先最新区域，最后历史区域，总数上限256；
    省略数量写入 audit。失败网无热点时使用真实引脚包围框，网表不裁剪。
    配置或几何错误抛 PipelineError，不猜测错误字符串。
    """
    from pcb_global_place import evaluate_cost
    from pcb_placement import _options
    options = _options(placement, board, rules_for(board))
    known = set(board["nets"])
    explicit = previous if explicit is None else explicit
    explicit = explicit or {}
    failed = (set((previous or {}).get("failed_nets", [])) | set(explicit.get("failed_nets", []))) & known
    diagnostics = diagnostics if isinstance(diagnostics, dict) else {}
    rows = [diagnostics]
    failure_context = diagnostics.get("failure_context")
    if isinstance(failure_context, dict):
        rows.append(failure_context)
    diagnostic_stats = diagnostics.get("stats", {})
    batches = diagnostic_stats.get("negotiated_batches", []) if isinstance(diagnostic_stats, dict) else []
    if batches and batches[-1].get("rounds"):
        rows.append(batches[-1]["rounds"][-1])
    for row in rows:
        if isinstance(row.get("net"), str) and row["net"] in known:
            failed.add(row["net"])
        for key in ("unfinished_nets", "failed_nets", "conflicting_nets", "exact_failed_nets"):
            names = row.get(key, [])
            if isinstance(names, (list, tuple, dict)):
                failed.update(n for n in names if isinstance(n, str) and n in known)
    metric = evaluate_cost(board, options)
    hotspots = []
    # 真实失败包围盒先于代理拥塞热点；只接受明确的有限mil坐标。
    for row in rows:
        values = [row]
        if isinstance(row.get("local_repair"), dict):
            values.append(row["local_repair"])
        for item in values:
            bounds = item.get("bounds")
            if (isinstance(bounds, (list, tuple)) and len(bounds) == 4
                    and all(type(v) in (int, float) and math.isfinite(v) for v in bounds)
                    and bounds[0] < bounds[2] and bounds[1] < bounds[3]):
                names = [n for n in (row.get("net"), item.get("failed_net")) if n in known]
                hotspots.append({"bounds": list(bounds), "weight": 1.0, "nets": sorted(set(names))})
    for hot in metric.get("rudy", {}).get("hotspots", [])[:64]:
        bounds = hot.get("bounds")
        if not isinstance(bounds, (list, tuple)) or len(bounds) != 4:
            continue
        weight = float(hot.get("weight", max(1.0, float(hot.get("overflow", 1.0)))))
        names = [n for n in hot.get("nets", []) if n in known]
        hotspots.append({"bounds": list(bounds), "weight": weight, "nets": names})
    tolerance = rules_for(board).curve_tolerance
    pad_points = {}
    for component in board["components"]:
        for pad in component["pads"]:
            if pad.get("net") in failed:
                point = _fast_pad_bounds(component, pad, tolerance).inner.representative_point()
                pad_points.setdefault(pad["net"], []).append((point.x, point.y))
    for net in sorted(failed):
        if any(net in hot["nets"] for hot in hotspots) or not pad_points.get(net):
            continue
        points = pad_points[net]
        margin = options["grid_step"]
        hotspots.append({"bounds": [min(p[0] for p in points) - margin, min(p[1] for p in points) - margin,
                                    max(p[0] for p in points) + margin, max(p[1] for p in points) + margin],
                         "weight": 1.0, "nets": [net]})
    # 显式区保持优先级；最新失败区域不能被已满的历史列表挤掉。
    merged, seen = [], set()
    retained, omitted = {}, {}
    for label, rows in (("explicit", explicit.get("hotspots", [])), ("latest", hotspots),
                        ("history", (previous or {}).get("hotspots", []))):
        retained[label], omitted[label] = 0, 0
        for hot in rows:
            key = (tuple(hot["bounds"]), tuple(sorted(hot.get("nets", []))), hot.get("weight", 1.0))
            if key in seen:
                continue
            seen.add(key)
            if len(merged) < 256:
                merged.append(deepcopy(hot))
                retained[label] += 1
            else:
                omitted[label] += 1
    if audit is not None:
        audit.update(limit=256, retained=retained, omitted=omitted,
                     failed_nets_retained=len(failed), hotspot_count=len(merged))
    return {"failed_nets": sorted(failed), "hotspots": merged}


def run_pipeline(task, *, checkpoint_callback=None, resume_checkpoint=None):
    """执行有界候选流程并返回报告；失败不返回候选板或写回计划。

    可选回调仅接收独立计算检查点，恢复必须重新验证完整任务/源/预算。
    输入错误写入失败报告；外部回调的IO异常传播。原生写回不在此执行。
    """
    started = time.monotonic()
    report = {"schema_version": 1, "passed": False, "status": "failed", "attempts": [],
              "violations": [], "board": None, "writeback_plan": None,
              "scope": "bounded geometric placement/routing/copper pipeline; native EDA DRC is a separate live stage"}
    try:
        original, options, compiled = _task(task)
        report["input_fingerprint"] = fingerprint(original)
        report["constraints"] = compiled
        from pcb_automation import (validate_options as automation_options, make_checkpoint,
                                    restore_checkpoint, local_layout_input)
        automation = automation_options(options.get("automation", {}), options)
        if checkpoint_callback is not None and not callable(checkpoint_callback):
            raise PipelineError("checkpoint_callback 必须为函数或None")
        checkpoint_enabled = options.get("routing", {}).get("checkpoint", False)
        if (checkpoint_callback is not None or resume_checkpoint is not None) and not checkpoint_enabled:
            raise PipelineError("流水线检查点要求 routing.checkpoint=true")
        restored = (restore_checkpoint(task, original, options, resume_checkpoint)
                    if resume_checkpoint is not None else None)
        elapsed_before = restored["usage"]["elapsed_seconds"] if restored else 0.0
        first_attempt = restored["attempt"] if restored else 0
        completed_history = list(restored["completed_history"]) if restored else []
        def elapsed_total():
            return elapsed_before + time.monotonic() - started
        def remaining():
            seconds = automation["max_seconds"]-elapsed_total()
            if seconds <= 0:
                raise PipelineError("AUTOMATION_TIME_BUDGET：累计总时限耗尽，未找到已验收结果")
            return seconds
        latest_checkpoint = None
        local_seed, local_placement = None, None
        last_pose = None
        placement_feedback = deepcopy(restored["feedback"] if restored else options.get("placement", {}).get("feedback", {}))
        for attempt in range(first_attempt, options.get("max_iterations", 3)):
            stages = []
            entry = {"index": attempt, "stages": stages, "passed": False}
            report["attempts"].append(entry)
            stage = "placement"
            try:
                if automation["enabled"]:
                    remaining()
                    if attempt > automation["max_layout_repairs"]:
                        raise PipelineError("AUTOMATION_ROUND_BUDGET：局部布局轮数预算耗尽，未找到")
                placement = deepcopy(local_placement if local_placement is not None else options.get("placement", {}))
                if "ground_nets" not in placement and "ground_nets" in options.get("routing", {}):
                    placement["ground_nets"] = deepcopy(options["routing"]["ground_nets"])
                if attempt:
                    placement["repair_attempt"] = attempt
                    if placement.get("method", "legacy") == "analytic_sa":
                        placement["feedback"] = deepcopy(placement_feedback)
                        entry["placement_feedback"] = deepcopy(placement_feedback)
                if restored is not None and attempt == first_attempt:
                    board = deepcopy(restored["routing_input"])
                    stats = {"restored_calculation_input": True, "placement_replayed": False}
                else:
                    if automation["enabled"]:
                        placement["max_seconds"] = min(placement.get("max_seconds", 30.0), remaining())
                    board, stats = place_components(local_seed if local_seed is not None else original, placement)
                stages.append({"stage": stage, "passed": True, "stats": stats})
                pose = fingerprint({"components": board["components"]})
                if attempt and pose == last_pose:
                    stage = "feedback"
                    raise PipelineError("feedback cannot change any movable component; stopped")
                last_pose = pose
                stage = "routing"
                # 后处理只允许改变本轮布线新增图元；附属孔以本轮布局后的世界坐标为基准。
                routing_input = deepcopy(board) if "cleanup" in options or checkpoint_enabled else None
                routing_options = deepcopy(restored["routing_options"] if restored is not None and attempt == first_attempt
                                           else options.get("routing", {}))
                if automation["enabled"] and not (restored is not None and attempt == first_attempt):
                    routing_options["max_seconds"] = min(routing_options.get("max_seconds", 120.0), remaining())
                def save_calculation(checkpoint):
                    nonlocal latest_checkpoint
                    latest_checkpoint = make_checkpoint(task, routing_input, routing_options, checkpoint,
                                                        attempt, elapsed_total(), placement_feedback, completed_history)
                    if checkpoint_callback is not None:
                        checkpoint_callback(deepcopy(latest_checkpoint))
                route_kwargs = {}
                if checkpoint_enabled:
                    route_kwargs["checkpoint_callback"] = save_calculation
                    if restored is not None and attempt == first_attempt:
                        route_kwargs["resume_checkpoint"] = restored["routing_checkpoint"]
                board, stats = route_board(board, routing_options, **route_kwargs)
                if automation["enabled"]:
                    remaining()
                stages.append({"stage": stage, "passed": True, "stats": stats})
                deferred = stats.get("deferred_ground_nets", [])
                if "cleanup" in options:
                    from pcb_cleanup import cleanup_board
                    stage = "cleanup"
                    cleanup_options = deepcopy(options["cleanup"])
                    if "deferred_nets" in cleanup_options and set(cleanup_options["deferred_nets"]) != set(deferred):
                        raise PipelineError("cleanup.deferred_nets 必须与实际暂缓地网完全一致")
                    cleanup_options["deferred_nets"] = list(deferred)
                    board, cleanup_stats = cleanup_board(board, cleanup_options, original_board=routing_input)
                    entry["cleanup_report"] = deepcopy(cleanup_stats)
                    if cleanup_stats.get("passed") is not True:
                        raise PipelineError("后处理输入或候选未通过原验收；详见逐对象报告")
                    stages.append({"stage": stage, "passed": True, "stats": cleanup_stats})
                    if automation["enabled"]:
                        remaining()
                if deferred:
                    stage = "signal_verification"
                    signal_nets = [n for n in board["nets"] if n not in deferred]
                    errors = check_routed_nets(board, signal_nets)
                    if errors:
                        raise PipelineError("; ".join(errors))
                    stages.append({"stage": stage, "passed": True, "nets": signal_nets})
                    board.setdefault("metadata", {}).update(preview_only=True,
                        native_ground_workflow={"strategy": "plane_then_drc", "ground_nets": deferred,
                                                "stage": "awaiting_native_ground"})
                    stage = "writeback_plan"
                    plan = build_writeback_plan(original, board, allowed_trace_replacements=
                        options.get("routing", {}).get("shove", {}).get("movable_trace_ids")
                        if options.get("routing", {}).get("shove", {}).get("enabled") else None)
                    errors = check_routed_nets(plan["target_board"], signal_nets)
                    if errors:
                        raise PipelineError("signal writeback target rejected: " + "; ".join(errors))
                    stages.append({"stage": stage, "passed": True,
                                   "live_source_available": bool(plan["source"]["document_uuid"] and plan["source"]["fingerprint"])})
                    if automation["enabled"]:
                        remaining()
                    entry["signal_stage_passed"] = True
                    report.update(passed=False, full_board_accepted=False, signal_stage_passed=True,
                        status="awaiting_native_ground", deferred_ground_nets=deferred,
                        pending_stages=["native_pours", "native_drc", "ground_repair", "native_drc_recheck"],
                        board=board, writeback_plan=plan, violations=[])
                    if "return_path" in options:
                        report["pending_stages"].append("return_path_after_native_pours")
                    break
                stage = "shielding"
                board, stats = generate_shielding(board, options.get("shielding", {}))
                stages.append({"stage": stage, "passed": True, "stats": stats})
                stage = "pours"
                if automation["enabled"]:
                    remaining()
                board, stats = generate_pours(board, options.get("pours", []))
                stages.append({"stage": stage, "passed": True, "stats": stats})
                if automation["enabled"]:
                    remaining()
                if "return_path" in options:
                    from pcb_return_path import check_return_paths
                    stage = "return_path"
                    quality = check_return_paths(board, options["return_path"], before=original)
                    entry["return_path_report"] = deepcopy(quality)
                    if quality.get("passed") is not True:
                        raise PipelineError("回流复核未通过或证据不完整；详见分项报告")
                    stages.append({"stage": stage, "passed": True, "report": quality})
                stage = "verification"
                errors = check_board(board)
                if errors:
                    raise PipelineError("; ".join(errors))
                stages.append({"stage": stage, "passed": True, "violations": []})
                stage = "writeback_plan"
                plan = build_writeback_plan(original, board, allowed_trace_replacements=
                    options.get("routing", {}).get("shove", {}).get("movable_trace_ids")
                    if options.get("routing", {}).get("shove", {}).get("enabled") else None)
                # Re-check the actual EDA target, including hole decomposition.
                errors = check_board(plan["target_board"])
                if errors:
                    raise PipelineError("writeback target rejected: " + "; ".join(errors))
                stages.append({"stage": stage, "passed": True,
                               "live_source_available": bool(plan["source"]["document_uuid"] and plan["source"]["fingerprint"])})
                if automation["enabled"]:
                    remaining()
                entry["passed"] = True
                report.update(passed=True, status="offline_passed", board=board,
                              writeback_plan=plan, violations=[])
                break
            except (PipelineError, GEOSException, ValueError, TypeError, KeyError, AttributeError, OverflowError) as exc:
                entry["error"] = str(exc)
                stages.append({"stage": stage, "passed": False, "error": str(exc)})
                if hasattr(exc, "diagnostics"):
                    entry["diagnostics"] = deepcopy(exc.diagnostics)
                    stages[-1]["diagnostics"] = deepcopy(exc.diagnostics)
                report["violations"].append(f"{stage}: {exc}")
                if stage == "routing" and placement.get("method", "legacy") == "analytic_sa":
                    try:
                        feedback_audit = {}
                        placement_feedback = _placement_feedback(board, placement, getattr(exc, "diagnostics", {}),
                            previous=placement_feedback, explicit=options.get("placement", {}).get("feedback", {}),
                            audit=feedback_audit)
                        entry["next_placement_feedback"] = deepcopy(placement_feedback)
                        entry["placement_feedback_stats"] = feedback_audit
                        if automation["enabled"]:
                            remaining()
                            if latest_checkpoint is None:
                                raise PipelineError("LOCAL_LAYOUT_NOT_FOUND：没有可复核的路由检查点")
                            # 只利用刚才路由器产出的工件；外部恢复由路由器先严格重验。
                            completed = len(latest_checkpoint["routing_checkpoint"]["completed_nets"])
                            previous_counts = completed_history
                            entry["completed_checkpoint_nets"] = completed
                            count = automation["stagnation_rounds"]
                            if len(previous_counts) >= count and all(completed <= x for x in previous_counts[-count:]):
                                raise PipelineError("AUTOMATION_STAGNATION：连续局部修复无改善，停止当前方法")
                            completed_history.append(completed)
                            local_seed, local_placement, audit = local_layout_input(original,
                                latest_checkpoint["routing_checkpoint"]["board"], options, placement_feedback, automation)
                            entry["local_layout_repair"] = audit
                    except (PipelineError, GEOSException, ValueError, TypeError, KeyError, AttributeError, OverflowError) as feedback_error:
                        entry["feedback_error"] = str(feedback_error)
                        report["violations"].append(f"feedback: {feedback_error}")
                        break
                if stage in ("writeback_plan", "feedback") or not options.get("placement", {}).get("enabled", True):
                    break
    except (PipelineError, GEOSException, ValueError, TypeError, KeyError, AttributeError, OverflowError) as exc:
        report["violations"] = [str(exc)]
    report["elapsed_seconds"] = round(time.monotonic() - started, 6)
    if "elapsed_before" in locals():
        report["cumulative_elapsed_seconds"] = round(elapsed_total(), 6)
    if "latest_checkpoint" in locals() and latest_checkpoint is not None:
        report["calculation_checkpoint_available"] = True
        report["calculation_checkpoint_scope"] = "续算工件；禁止作为写回或整板验收依据"
    return report


def _svg(board):
    boundary = board_polygon(board)
    x0, y0, x1, y1 = boundary.bounds
    width, height = x1 - x0, y1 - y0
    copper, drills = copper_and_drills(board)
    palette = ["#2563eb", "#e11d48", "#16a34a", "#9333ea", "#d97706"]
    colors = {n: palette[i % len(palette)] for i, n in enumerate(sorted(board["nets"]))}
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{x0-10} {y0-10} {width+20} {height+20}">',
             '<rect x="-1000000" y="-1000000" width="2000000" height="2000000" fill="#f8fafc"/>',
             boundary.svg(fill_color="#e2e8f0", opacity=1)]
    for item in copper:
        parts.append(item["inner"].svg(fill_color=colors.get(item["net"], "#64748b"), opacity=0.65))
    for drill in drills:
        parts.append(drill["outer"].svg(fill_color="#ffffff", opacity=1))
    parts.append('</svg>')
    return "\n".join(parts)


def _json(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def _clear_accepted(output):
    for name in ("board.json", "writeback-plan.json", "board.svg"):
        path = output / name
        if path.exists() or path.is_symlink():
            path.unlink()


def _short_text(text, limit=240):
    """限制终端单个字段的长度，保留省略标记；报告中的原文保持完整。"""
    text = " ".join(str(text).splitlines())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _network_diagnostics(evidence, attempt, stage):
    """从失败阶段取明确的网络、端点及逐网理由；单列尚未完成的网络，输出规模有界。"""
    sources = [evidence, evidence.get("failure_context")]
    pending = evidence.get("unfinished_nets", [])
    # 协商最后一次失败可能来自旧轮次，只采用与最终未完成清单一致的证据。
    last = evidence.get("last_failure")
    if isinstance(last, dict) and last.get("net") in pending:
        sources.append(last)
    if evidence.get("code") == "NEGOTIATED_BUDGET":
        batches = evidence.get("stats", {}).get("negotiated_batches", [])
        if batches and batches[-1].get("rounds"):
            sources.append(batches[-1]["rounds"][-1])
    if stage == "return_path":
        quality = attempt.get("return_path_report", {})
        for key in ("trace_projection_missing", "guard_band_missing", "via_clearance_change",
                    "old_signal_reference_changes", "reference_disconnections", "unknown"):
            sources.extend(quality.get(key, []))
    names, unfinished, examples = {}, {}, []
    for row in sources:
        if not isinstance(row, dict):
            continue
        net = row.get("net") or row.get("failed_net")
        if isinstance(net, str) and net:
            names[net] = None
            example = {"net": _short_text(net, 100)}
            for key in ("code", "message", "signal_layer", "reference_layer"):
                if (key in row and row[key] != evidence.get(key)
                        and row[key] != f"{evidence.get('code')}: {evidence.get('message')}"):
                    example[key] = _short_text(row[key], 160)
            for key in ("source_terminals", "target_terminals"):
                if row.get(key):
                    example[key] = [[_short_text(part, 60) for part in endpoint[:2]]
                                    if isinstance(endpoint, (tuple, list)) else _short_text(endpoint, 100)
                                    for endpoint in row[key][:2]]
            if len(example) > 1 and len(examples) < 4 and example not in examples:
                examples.append(example)
        for key in ("failed_nets", "conflicting_nets", "exact_failed_nets", "unfinished_nets"):
            listed = row.get(key)
            if isinstance(listed, (list, tuple, dict)):
                target = unfinished if key == "unfinished_nets" else names
                target.update((name, None) for name in listed if isinstance(name, str) and name)
    info = {}
    for items, key, count_key in ((names, "nets", "net_count"), (unfinished, "unfinished_nets", "unfinished_count")):
        if items:
            info[count_key] = len(items)
            info[key] = [_short_text(name, 100) for name in list(items)[:8]]
            if len(items) > 8:
                info[key + "_omitted"] = len(items) - 8
    if examples:
        info["network_details"] = examples
    return info


def failure_summary(result):
    """输入 run_pipeline 的失败报告，返回本轮阶段、原因和有上限的已知网络诊断。

    全局输入错误只保留原因，未经诊断点名的网络不推断为失效。缺少阶段、网络等
    可选信息时省略相应字段。调用者仍须保存原报告以供逐项追查。
    """
    attempts = result.get("attempts", [])
    attempt = attempts[-1] if attempts else {}
    failed_stage = next((s for s in reversed(attempt.get("stages", [])) if s.get("passed") is False), {})
    stop = attempt.get("feedback_error")
    if failed_stage.get("stage") == "feedback" and len(attempts) >= 2:
        previous = attempts[-2]
        routing = next((s for s in reversed(previous.get("stages", []))
                        if s.get("stage") == "routing" and s.get("passed") is False), None)
        if routing:
            stop = failed_stage.get("error")
            attempt, failed_stage = previous, routing
    violations = result.get("violations", [])
    evidence = failed_stage.get("diagnostics", attempt.get("diagnostics", {}))
    reason = failed_stage.get("error") or (violations[-1] if violations else "失败原因未记录")
    diagnostic = {"message": _short_text(evidence.get("message") or reason)}
    if "stage" in failed_stage:
        diagnostic["stage"] = failed_stage["stage"]
    for key in ("code", "phase", "budget_scope"):
        if evidence.get(key):
            diagnostic[key] = _short_text(evidence[key], 80)

    diagnostic.update(_network_diagnostics(evidence, attempt, failed_stage.get("stage")))
    details = evidence.get("errors", [])
    if details:
        diagnostic["details"] = [_short_text(error) for error in details[:2]]
        diagnostic["detail_count"] = len(details)
    if stop:
        diagnostic["stop_reason"] = _short_text(stop)
    return diagnostic


def _solve_summary(result, report_path, task=None):
    """入参为原求解报告、已成功落盘的报告路径和原任务。返回可序列化的终端摘要。

    仅在离线或信号阶段已验收时统计已核对的网络数。异常发生在创建输出前时，
    report_path 为 None，摘要不引用旧报告。调用者保留原命令的退出码和输出流。
    """
    summary = {"status": result["status"], "passed": result["passed"],
               "full_board_accepted": result.get("full_board_accepted", False),
               "report": str(report_path.resolve()) if report_path is not None else None}
    signal_ok = result.get("signal_stage_passed", False)
    if "signal_stage_passed" in result:
        summary["signal_stage_passed"] = signal_ok
    board = task.get("board") if isinstance(task, dict) else None
    nets = board.get("nets") if isinstance(board, dict) else None
    if isinstance(nets, dict):
        counts = summary["nets"] = {"total": len(nets)}
        if result["passed"] or signal_ok:
            deferred = len(result.get("deferred_ground_nets", []))
            counts.update(verified_offline=len(nets) - deferred, deferred_ground=deferred)
    if result["passed"] or signal_ok:
        summary["artifacts"] = ["board.json", "writeback-plan.json", "board.svg"]
        if result.get("pending_stages"):
            summary["pending_stages"] = result["pending_stages"]
    else:
        summary["diagnostic"] = failure_summary(result)
        if "cleanup_error" in result:
            summary["cleanup_error"] = _short_text(result["cleanup_error"])
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    solve = sub.add_parser("solve", help="run layout/routing/copper/verification and emit a plan")
    solve.add_argument("--input", type=Path, required=True)
    solve.add_argument("--output-dir", type=Path, required=True)
    solve.add_argument("--overwrite", action="store_true")
    solve.add_argument("--full-output", action="store_true",
                       help="在终端输出完整 result.json 内容。默认输出单行摘要，完整报告始终保存在输出目录")
    solve.add_argument("--resume-checkpoint", type=Path, help="上一轮独立计算检查点；不作为验收板")
    solve.add_argument("--checkpoint-sha256", help="上次工件记录的SHA256，恢复时必填")
    validate = sub.add_parser("validate", help="strictly check a canonical board including compiled constraints")
    validate.add_argument("--input", type=Path, required=True)
    validate.add_argument("--output", type=Path)
    validate.add_argument("--full-output", action="store_true", help="指定 --output 时仍在终端显示完整违规清单")
    prepare = sub.add_parser("prepare", help="combine a bridge snapshot and explicit solver configuration")
    prepare.add_argument("--board", type=Path, required=True)
    prepare.add_argument("--config", type=Path)
    prepare.add_argument("--output", type=Path, required=True)
    native = sub.add_parser("review-native-copper", help="independently check observed native copper and create a new plan")
    native.add_argument("--snapshot", type=Path, required=True)
    native.add_argument("--plan", type=Path, required=True)
    native.add_argument("--receipt", type=Path, required=True)
    native.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.command == "review-native-copper":
        directory = None
        try:
            read = lambda path: json.loads(path.read_text(encoding="utf-8-sig"))
            original, plan, receipt = read(args.snapshot), read(args.plan), read(args.receipt)
            if args.output_dir.exists():
                raise PipelineError("native copper review output directory must be new")
            args.output_dir.mkdir(parents=True, exist_ok=False)
            directory = args.output_dir
            _json(directory / "result.json", {"passed": False, "status": "pending"})
            board, next_plan = review_native_copper(original, plan, receipt)
            _json(directory / "board.json", board)
            _json(directory / "writeback-plan.json", next_plan)
            (directory / "board.svg").write_text(_svg(board), encoding="utf-8")
            result = {"passed": True, "status": "offline_passed", "reviewed_native_copper": True,
                      "native_eda_passed": False, "previous_plan_id": plan["plan_id"],
                      "plan_id": next_plan["plan_id"], "violations": []}
            _json(directory / "result.json", result)
            print(json.dumps(result, ensure_ascii=False))
            return 0
        except (OSError, GEOSException, ValueError, TypeError, KeyError, AttributeError, OverflowError) as exc:
            result = {"passed": False, "status": "failed", "violations": [str(exc)]}
            if directory is not None:
                try:
                    _clear_accepted(directory)
                    _json(directory / "result.json", result)
                except OSError as cleanup_error:
                    result["cleanup_error"] = str(cleanup_error)
            print(json.dumps(result, ensure_ascii=False), file=sys.stderr)
            return 1
    if args.command == "prepare":
        try:
            board = json.loads(args.board.read_text(encoding="utf-8"))
            config = json.loads(args.config.read_text(encoding="utf-8")) if args.config else {}
            config = strict_options(config, {"options", "constraints"}, "configuration")
            task = {"schema_version": 1, "board": board, **config}
            _task(task)  # Validate references and executable rule support now.
            if args.output.exists():
                raise PipelineError("prepare output already exists; choose another filename")
            if args.output.resolve() in {args.board.resolve(), args.config.resolve() if args.config else args.board.resolve()}:
                raise PipelineError("prepare must not overwrite its input")
            _json(args.output, task)
            print(json.dumps({"passed": True, "task": str(args.output.resolve())}))
            return 0
        except (OSError, GEOSException, ValueError, TypeError, KeyError, AttributeError, OverflowError) as exc:
            print(json.dumps({"passed": False, "violations": [str(exc)]}, ensure_ascii=False), file=sys.stderr)
            return 1
    prepared_output = None
    data = None
    validation_output_safe = False
    def same_input(path):
        return (path.resolve() == args.input.resolve()
                or (path.exists() and args.input.exists() and path.samefile(args.input)))
    try:
        if args.command == "solve":
            output = args.output_dir
            names = ("result.json", "board.json", "writeback-plan.json", "board.svg", "input-task.json", "calculation-checkpoint.json")
            if any(same_input(output / name) for name in names):
                raise PipelineError("input overlaps a generated output path; choose another output directory")
            if not args.overwrite and any((output / name).exists() for name in names):
                raise PipelineError("output already contains pipeline files; choose another directory or --overwrite")
            if args.resume_checkpoint and any(args.resume_checkpoint.resolve() == (output/name).resolve() for name in names):
                raise PipelineError("续算必须使用新输出目录，不能覆盖被读取的检查点")
            if bool(args.resume_checkpoint) != bool(args.checkpoint_sha256):
                raise PipelineError("续算路径与原记录SHA256必须同时提供")
            output.mkdir(parents=True, exist_ok=True)
            prepared_output = output
            _clear_accepted(output)
            _json(output / "result.json", {"passed": False, "status": "pending", "violations": []})
        if args.command == "validate" and args.output:
            if same_input(args.output):
                raise PipelineError("validation report must not overwrite its input board")
            validation_output_safe = True
        data = json.loads(args.input.read_text(encoding="utf-8"))
        if args.command == "validate":
            board = normalize_board(data)
            errors = check_board(board)
            result = {"passed": not errors, "violations": errors, "units": "mil"}
            if args.output:
                _json(args.output, result)
                if not args.full_output:
                    summary = {"passed": result["passed"], "units": "mil", "violation_count": len(errors),
                               "report": str(args.output.resolve())}
                    if errors:
                        summary["violations"] = [_short_text(error) for error in errors[:4]]
                    print(json.dumps(summary, ensure_ascii=False, separators=(",", ":")))
                    return 0 if result["passed"] else 1
        else:
            from pcb_automation import read_checkpoint, write_checkpoint
            from pcb_project_state import file_sha256
            restored = read_checkpoint(args.resume_checkpoint, args.checkpoint_sha256) if args.resume_checkpoint else None
            checkpoint_path = output / "calculation-checkpoint.json"
            # overwrite的旧检查点不能伪装成这次实际产生的工件。
            checkpoint_written = False
            def save_checkpoint(value):
                nonlocal checkpoint_written
                write_checkpoint(checkpoint_path, value)
                checkpoint_written = True
            kwargs = {}
            if data.get("options", {}).get("routing", {}).get("checkpoint", False):
                kwargs["checkpoint_callback"] = save_checkpoint
            if restored is not None:
                kwargs["resume_checkpoint"] = restored
            result = run_pipeline(data, **kwargs)
            if checkpoint_written:
                result["calculation_checkpoint"] = {"path": str(checkpoint_path.resolve()),
                    "sha256": file_sha256(checkpoint_path), "writeback_allowed": False}
            _json(output / "input-task.json", data)
            if result["passed"] or result.get("signal_stage_passed"):
                _json(output / "board.json", result["board"])
                _json(output / "writeback-plan.json", result["writeback_plan"])
                (output / "board.svg").write_text(_svg(result["board"]), encoding="utf-8")
            result = {k: v for k, v in result.items() if k not in ("board", "writeback_plan")}
            _json(output / "result.json", result)  # Accepted status commits last.
        if args.command == "solve" and not args.full_output:
            print(json.dumps(_solve_summary(result, output / "result.json", data),
                             ensure_ascii=False, separators=(",", ":")))
        else:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["passed"] or result.get("signal_stage_passed") else 1
    except (OSError, GEOSException, ValueError, TypeError, KeyError, AttributeError, OverflowError) as exc:
        failed = {"passed": False, "status": "failed", "violations": [str(exc)]}
        saved_report = None
        if prepared_output is not None:
            try:
                _clear_accepted(prepared_output)
                _json(prepared_output / "result.json", failed)
                saved_report = prepared_output / "result.json"
            except OSError as cleanup_error:
                failed["cleanup_error"] = str(cleanup_error)
        elif validation_output_safe:
            try:
                _json(args.output, failed)
            except OSError as output_error:
                failed["report_error"] = str(output_error)
        if args.command == "solve" and not args.full_output:
            print(json.dumps(_solve_summary(failed, saved_report, data), ensure_ascii=False,
                             separators=(",", ":")), file=sys.stderr)
        else:
            print(json.dumps(failed, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
