"""Resume selected networks without dropping any terminals or existing copper.

Preview is explicit: existing DRC errors are reported, never presented as full
acceptance. Ambiguous native curves become conservative, non-conductive routing
obstacles; their exact original records remain in the result and writeback plan.
"""
from argparse import ArgumentParser
from copy import deepcopy
import json
from pathlib import Path
import sys
import time
import traceback

from shapely.ops import unary_union

from pcb_constraints import compile_constraints
from pcb_geometry import (filled_region_bounds, _native_segments, _minor_arc_cap,
                          solid_polygon)
from pcb_pipeline import build_writeback_plan
from pcb_pipeline_common import PipelineError, check_board, rules_for, CollisionModel
import pcb_routing as routing


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def write(path, value):
    path = Path(path)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def conservative_views(board, preview):
    """A connectivity subset plus an obstacle superset; no invented anchors."""
    clean = deepcopy(board)
    pours, obstacles, evidence = [], [], []
    tolerance = rules_for(board).curve_tolerance
    for pour in board.get("routes", {}).get("copper_pours", []):
        valid = []
        for region in pour["filled_polygons"]:
            try:
                filled_region_bounds(region, tolerance)
                valid.append(deepcopy(region))
            except ValueError as error:
                if not preview or "native_path" not in region:
                    raise
                points, segments = _native_segments(region["native_path"])
                parts, remaining = [solid_polygon(points)], 65536
                for a, b, angle in segments:
                    if angle:
                        bounds, used = _minor_arc_cap(a, b, angle, tolerance, remaining)
                        remaining -= used
                        parts.append(bounds.outer)
                hull = unary_union(parts).convex_hull
                points = [list(p) for p in list(hull.exterior.coords)[:-1]]
                obstacles.append({"points": points, "layers": [pour["layer"]], "clearance": 0})
                evidence.append({"id": pour.get("id"), "net": pour["net"], "layer": pour["layer"],
                                 "reason": str(error), "obstacle_hull": points,
                                 "connectivity_anchor": False, "original_copper_preserved": True})
        if valid:
            pours.append(dict(deepcopy(pour), filled_polygons=valid))
    clean.setdefault("routes", {})["copper_pours"] = pours
    return clean, obstacles, evidence


def connectivity(board, nets):
    result = {}
    for net in nets:
        groups = routing._groups(board, net)
        declared = {(p["compId"], p["padId"]) for p in board["nets"][net]}
        terminals = set().union(*(g["terminals"] for g in groups)) if groups else set()
        result[net] = {"passed": len(groups) == 1 and terminals == declared,
                       "physical_groups": len(groups), "terminals": len(terminals),
                       "required_terminals": len(declared)}
    return result


def _summary(report, output):
    """输入已落盘的续算报告及其目录，返回有界的选网结果；不改变报告的验收范围。"""
    connectivity = report["connectivity"]
    failures = report["failed_nets"]
    pending = set(report["not_attempted"])
    negotiated = report["stats"]["method"] == "negotiated"
    summary = {k: report[k] for k in ("preview_only", "full_board_accepted", "selected_connectivity_passed",
                                      "writeback_plan_generated", "requires_live_rebase", "added")}
    summary.update(selected=len(connectivity), connected=sum(v["passed"] for v in connectivity.values()),
                   not_attempted=len(pending), deferred_ground=len(report["deferred_ground_nets"]),
                   geometry_errors=len(report["final_geometry_errors"]), topology_errors=len(report["topology_errors"]),
                   report=str((output / "result.json").resolve()), progress=str((output / "progress.jsonl").resolve()))
    if negotiated and failures:
        # 原始报告在每个批成员下保存同一异常。仅当诊断点名该网时才归因到具体网络。
        summary["batch_error"] = next(iter(failures.values()))["error"][:240]
    diagnosed, disconnected = [], []
    issue_count = 0
    for net, status in connectivity.items():
        failure = failures.get(net)
        detail = (failure or {}).get("diagnostics") or {}
        attributable = failure and (not negotiated or detail.get("net") == net)
        if status["passed"] and not attributable:
            continue
        issue_count += 1
        sample = diagnosed if attributable else disconnected
        if len(sample) >= 5:
            continue
        row = dict(net=net if len(net) <= 128 else net[:127] + "…",
                   physical_groups=status["physical_groups"], not_attempted=net in pending)
        if len(net) > 128:
            row["net_truncated"] = True
        if attributable:
            row["error"] = failure["error"][:240]
            if detail.get("code"):
                row["code"] = detail["code"]
        sample.append(row)
    issues = (diagnosed + disconnected)[:5]
    if issues:
        summary.update(network_diagnostics=issues, additional_networks=issue_count - len(issues))
    return summary


def main(argv=None):
    """读取续算命令行并输出来源绑定的候选与统计，不操作原生编辑器。

    入参：argv 为参数字符串列表或 None。出参：所选网络连通时返回 0，
    否则返回 1；结果仍为 preview_only，full_board_accepted 始终为 False。
    异常：非法配置和来源不匹配抛 PipelineError，文件错误按原异常传播。
    新算法开关与整板入口共享校验，原 CLI 搜索预算的优先级保持不变。
    """
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("--board", required=True)
    parser.add_argument("--source-board", help="Actual live baseline when --board is an unwritten calculated checkpoint")
    parser.add_argument("--trace-patch", help="Explicit JSON source_segments and replacement_segments for local rerouting")
    parser.add_argument("--config", help="Optional solver-config containing options and constraints")
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--full-output", action="store_true", help="在终端同时显示逐网完整进度（始终保存在 progress.jsonl）")
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("--nets", nargs="+")
    selection.add_argument("--exclude-net", action="append", default=[])
    parser.add_argument("--preview", action="store_true", help="Preserve partial work and existing errors as an explicit preview")
    parser.add_argument("--calculation-only", action="store_true",
                        help="Compute an unwritten layout proposal without producing a live writeback plan; reexport/rebase before writing")
    parser.add_argument("--grid-step", type=float, default=10)
    parser.add_argument("--max-net-seconds", type=float, default=600)
    parser.add_argument("--max-seconds", type=float, default=3600)
    parser.add_argument("--max-expansions", type=int, default=500000)
    args = parser.parse_args(argv)
    if min(args.grid_step, args.max_net_seconds, args.max_seconds, args.max_expansions) <= 0:
        parser.error("search budgets and grid step must be positive")
    output = Path(args.output_dir)
    output.mkdir(parents=True, exist_ok=False)
    try:
        return _run(args, parser, output)
    except (ValueError, OSError, TypeError, KeyError) as exc:
        # 已独占的新目录可留存原始异常，调用本函数的旧程序仍可捕获原有异常类型。
        log = output / "error.log"
        log.write_text(traceback.format_exc(), encoding="utf-8")
        if args.full_output or argv is not None:
            raise
        print(json.dumps({"status": "failed", "full_board_accepted": False, "error": str(exc)[:240],
                          "log": str(log.resolve())}, ensure_ascii=False), file=sys.stderr)
        return 1


def _run(args, parser, output):
    """按既有选网流程求解，完整记录进度与报告；输入不可用或写盘失败时抛出原异常。"""
    original = read(args.board)
    original = original.get("board", original)
    baseline = read(args.source_board) if args.source_board else deepcopy(original)
    baseline = baseline.get("board", baseline)
    if original.get("metadata", {}).get("source") != baseline.get("metadata", {}).get("source"):
        raise PipelineError("Calculated checkpoint and real baseline must have the same source identity")
    config = read(args.config) if args.config else {}
    if {"cleanup", "return_path"} & set(config.get("options", {})):
        raise PipelineError("选网续算尚不支持 cleanup/return_path；请使用完整 solve 或对应独立质量检查入口")
    routing_options = routing.algorithm_options(config.get("options", {}).get("routing", {}))
    if any(routing_options.get(key, {}).get("enabled") for key in ("fanout", "shove", "diff_layer_change")):
        raise PipelineError("选网续算尚不执行整封装扇出、旧铜推挤或差分换层；请使用完整 solve，不能静默忽略新选项")
    deferred = routing.deferred_ground_nets(original, routing_options)
    selected = args.nets or [n for n in original["nets"] if n not in args.exclude_net and n not in deferred]
    if ((not selected and not deferred) or len(selected) != len(set(selected))
            or set(selected) - set(original["nets"])):
        parser.error("selected nets must be unique existing networks")
    if set(args.exclude_net) - set(original["nets"]):
        parser.error("excluded network does not exist")
    clean, obstacles, evidence = conservative_views(original, args.preview)
    clean, compiled_options, _ = compile_constraints(clean, config.get("options", {}),
                                      config.get("constraints", clean.get("metadata", {}).get("pipeline_constraints", [])))
    # 选网续算不能把耦合对拆开，亦不能以局部连通代替整组实际等长验收。
    # 当前续算入口尚不执行联合构造；在产生检查点或写回计划之前明确拒绝。
    from pcb_differential import declarations
    pairs, length_groups = declarations(clean.get("metadata", {}).get("pipeline_constraints", []))
    if pairs or length_groups:
        raise PipelineError("选网续算尚不支持 diff_pair/length_group；请使用 pcb_pipeline.py solve 完成联合布线与独立质量验收")
    routing.deferred_ground_nets(clean, compiled_options.get("routing", {}))
    for key in ("tags", "metadata"):
        original[key] = deepcopy(clean[key])
    errors = check_board(clean, ignore_connectivity=True)
    if errors and not args.preview:
        raise PipelineError("Existing geometry errors require explicit preview mode: " + "; ".join(errors[:8]))
    model = deepcopy(clean)
    model["routes"].setdefault("keepouts", []).extend(obstacles)
    removed = []
    if args.trace_patch:
        patch = read(args.trace_patch)
        old_segments = patch["source_segments"]
        replacements = patch["replacement_segments"]
        by_id = {t.get("id"): t for t in model["routes"]["traces"] if t.get("id")}
        baseline_by_id = {t.get("id"): t for t in baseline["routes"]["traces"] if t.get("id")}
        for old in old_segments:
            if old.get("locked") or by_id.get(old.get("id")) != old or baseline_by_id.get(old.get("id")) != old:
                raise PipelineError("Trace patch does not exactly match an unlocked source segment")
        removed = [t["id"] for t in old_segments]
        if len(set(removed)) != len(removed):
            raise PipelineError("Repeated trace replacement ID")
        nets = set(t["net"] for t in old_segments)
        before_groups = {n: routing._groups(model, n) for n in nets}
        model["routes"]["traces"] = [t for t in model["routes"]["traces"] if t.get("id") not in removed]
        collision = CollisionModel(model)
        for t in replacements:
            if t["net"] not in nets or not collision.trace_ok(t["net"], t["layer"], t["sx"], t["sy"], t["ex"], t["ey"], t["width"]):
                raise PipelineError("Local replacement trace violates physical obstacles")
            model["routes"]["traces"].append(t)
            collision = CollisionModel(model)
        for net in nets:
            after_groups = routing._groups(model, net)
            if (len(after_groups) > len(before_groups[net]) or
                    any(old["terminals"] and not any(old["terminals"] <= new["terminals"]
                        for new in after_groups) for old in before_groups[net])):
                raise PipelineError("Local replacement breaks an existing copper group")
    counts = {k: len(baseline.get("routes", {}).get(k, [])) for k in ("traces", "vias")}
    initial_model_pours = len(model["routes"].get("copper_pours", []))
    options = dict(grid_step=args.grid_step, diagonal=True, allow_vias=True,
                   max_expansions=args.max_expansions,
                   max_seconds=args.max_seconds, max_net_seconds=args.max_net_seconds)
    compiled_routing = routing.algorithm_options(compiled_options.get("routing", {}))
    options.update({k: v for k, v in compiled_routing.items()
                    if k in ("via_diameter", "via_drill", "via_cost", "via_in_pad_penalty", "width", "allow_vias", "diagonal", "trace_layers",
                             "method", "occupancy_grid", "occupancy_max_cells", "occupancy_max_seconds", "occupancy_retries", "geometry_search")})
    if compiled_routing["method"] == "negotiated":
        # 协商需要同时看到所选网络；命令行搜索预算仍覆盖配置中的同名值。
        options = dict(compiled_routing, **options)
        for key in ("enabled", "allow_vias", "diagonal"):
            if key in options and type(options[key]) is not bool:
                raise PipelineError(f"routing {key} 必须为布尔值")
        configured_order = options.get("net_order", [])
        if (not isinstance(configured_order, list)
                or any(not isinstance(n, str) for n in configured_order)
                or len(configured_order) != len(set(configured_order))
                or set(configured_order) - set(model["nets"])):
            raise PipelineError("routing.net_order 必须为不重复的真实网络列表")
        if configured_order:
            selected = ([n for n in configured_order if n in selected]
                        + [n for n in selected if n not in configured_order])
        strategy = options.get("net_order_strategy", "input")
        if strategy not in ("input", "fewest_terminals", "ground_last"):
            raise PipelineError("routing.net_order_strategy 非法")
        grounds = options.get("ground_nets", [])
        if (not isinstance(grounds, list) or any(not isinstance(n, str) for n in grounds)
                or len(grounds) != len(set(grounds)) or set(grounds) - set(model["nets"])):
            raise PipelineError("routing.ground_nets 必须为不重复的真实网络列表")
        if strategy != "input" and not configured_order:
            selected.sort(key=lambda n: len(model["nets"][n]))
        if strategy == "ground_last":
            grounds = set(options.get("ground_nets", []))
            selected.sort(key=lambda n: n in grounds
                          or model.get("tags", {}).get("net_tags", {}).get(n, {}).get("type") == "GND")
        pours = options.get("pours_before_net", {})
        if (not isinstance(pours, dict) or set(pours) - set(model["nets"])
                or any(not isinstance(requests, list) or not requests
                       or any(not isinstance(r, dict) or r.get("net") != net for r in requests)
                       for net, requests in pours.items())):
            raise PipelineError("routing.pours_before_net 必须映射真实网络到相同网名的非空铺铜请求")
        selected.sort(key=lambda n: n in pours)
        topology = routing._validated_topology(model, options)
    else:
        # 顺序续算仍保持原有行为，但不静默忽略不受支持的优先方向。
        topology = {}
    options["trace_layers"] = routing._trace_layers(model, options)
    routing._preferred_layers(model, compiled_routing)
    budget = routing._Budget(options)
    stats = dict(connections=0, expansions=0, searches=0,
                 trace_layers=options["trace_layers"], through_via_layers=list(rules_for(model).copper_layers),
                 method=options["method"], occupancy_grid=options["occupancy_grid"],
                 search_seconds=0.0, astar_seconds=0.0, occupancy_build_seconds=0.0,
                 occupancy_update_seconds=0.0, occupancy_cells=0, occupancy_updated_cells=0,
                 occupancy_retries=0, occupancy_rejections=0)
    failures, attempted = {}, []
    started = time.monotonic()
    state = {"model": model}
    write(output / "source-board.json", baseline)
    with (output / "progress.jsonl").open("x", encoding="utf-8") as log:

        def emit(event, **data):
            entry = dict(event=event, elapsed_seconds=round(time.monotonic() - started, 3), **data)
            line = json.dumps(entry, ensure_ascii=False)
            log.write(line + "\n")
            log.flush()
            if args.full_output:
                print(line, flush=True)

        def target():
            result = deepcopy(original)
            for kind in counts:
                result["routes"][kind] = deepcopy(state["model"]["routes"][kind])
            if options["method"] == "negotiated":
                # 保守计算视图可能省略无法解释的原生曲线铜，输出仍保留其完整原文。
                new_pours = state["model"]["routes"].get("copper_pours", [])[initial_model_pours:]
                if "copper_pours" in original["routes"] or new_pours:
                    result["routes"]["copper_pours"] = (deepcopy(original["routes"].get("copper_pours", []))
                                                        + deepcopy(new_pours))
            result["metadata"]["preview_only"] = True
            if args.calculation_only:
                result["metadata"]["calculation_only"] = True
                result["metadata"]["requires_live_rebase"] = True
            if deferred:
                result["metadata"]["native_ground_workflow"] = {
                    "strategy": "plane_then_drc", "ground_nets": deferred,
                    "stage": "ground_repair" if set(selected) & set(deferred) else "awaiting_native_ground"}
            return result

        previous_commit = routing._commit

        def commit(*positional, **keywords):
            accepted = previous_commit(*positional, **keywords)
            if accepted is not None:
                state["model"] = accepted
                write(output / "board.json", target())
                emit("connection", net=positional[1], traces=len(accepted["routes"]["traces"]), vias=len(accepted["routes"]["vias"]))
            return accepted

        # 协商中的试验轮不会输出检查点；完整批次返回后才保存已精检候选。
        routing._commit = commit if options["method"] == "sequential" else previous_commit
        try:
            if options["method"] == "negotiated":
                attempted.extend(selected)
                emit("negotiation_start", nets=selected)
                try:
                    if options.get("enabled", True):
                        state["model"] = routing._negotiated_batches(state["model"], selected, options,
                                                                     stats, budget, topology)
                    write(output / "board.json", target())
                    emit("negotiation_done", nets=selected)
                except PipelineError as error:
                    for net in selected:
                        failures[net] = {"error": str(error), "diagnostics": getattr(error, "diagnostics", None)}
                    emit("negotiation_failed", error=str(error))
                finally:
                    budget.leave()
            for net in ([] if options["method"] == "negotiated" else selected):
                attempted.append(net)
                emit("net_start", net=net)
                try:
                    budget.enter(net)
                    state["model"] = routing._route_net(state["model"], net,
                        routing._net_options(state["model"], net, options), options, stats, budget)
                    emit("net_done", net=net)
                except PipelineError as error:
                    failures[net] = {"error": str(error), "diagnostics": getattr(error, "diagnostics", None)}
                    emit("net_failed", net=net, error=str(error))
                    if not args.preview:
                        break
                finally:
                    budget.leave()
                try:
                    budget.check()
                except PipelineError:
                    break
        finally:
            routing._commit = previous_commit
        solved = target()
        # Verification deliberately omits ambiguous native copper: proving contact
        # without that copper is sufficient; it cannot create a false connection.
        verify, _, _ = conservative_views(solved, args.preview)
        connection_result = connectivity(verify, selected)
        passed = all(v["passed"] for v in connection_result.values())
        topology_failures = []
        if options["method"] == "negotiated":
            from pcb_constraints import topology_errors
            topology_failures = [error for net, pins in topology.items() if net in selected
                                 for error in topology_errors(verify, net, pins)]
            passed = passed and not topology_failures and not failures
        full_geometry_errors = check_board(verify, ignore_connectivity=True)
        if options.get("via_in_pad_penalty", 0):
            stats["via_in_pad"] = routing.via_in_pad_report(verify,
                verify["routes"].get("vias", [])[counts["vias"]:], options["via_in_pad_penalty"])
        budget.record(stats)
        report = dict(preview_only=True, full_board_accepted=False, selected_connectivity_passed=passed,
            calculation_only=args.calculation_only, writeback_plan_generated=not args.calculation_only,
            requires_live_rebase=args.calculation_only,
            ground_strategy=routing_options.get("ground_strategy", "route"),
            deferred_ground_nets=[n for n in deferred if n not in selected],
            selected_nets=selected, excluded_nets=[n for n in original["nets"] if n not in selected],
            not_attempted=[n for n in selected if n not in attempted], failed_nets=failures,
            topology_errors=topology_failures,
            connectivity=connection_result, existing_geometry_errors=errors,
            final_geometry_errors=full_geometry_errors, conservative_native_obstacles=evidence,
            added={k: len(solved["routes"][k]) - counts[k] for k in counts}, stats=stats,
            elapsed_seconds=round(time.monotonic() - started, 3))
        write(output / "board.json", solved)
        if args.calculation_only:
            plan = None
        elif removed:
            # Construct the ordinary append-only delta first, then declare the
            # exact, separately collision/contact-checked line replacements.
            staged = deepcopy(solved)
            retained = len(baseline["routes"]["traces"]) - len(removed)
            expected_prefix = [t for t in baseline["routes"]["traces"] if t.get("id") not in removed]
            if solved["routes"]["traces"][:retained] != expected_prefix:
                raise PipelineError("Checkpoint changed existing copper outside the explicit replacement")
            staged["routes"]["traces"] = deepcopy(baseline["routes"]["traces"]) + solved["routes"]["traces"][retained:]
            plan = build_writeback_plan(baseline, staged)
            plan["remove"]["traces"] = removed
            plan["target_board"] = solved
        else:
            plan = build_writeback_plan(baseline, solved)
        if plan is not None:
            write(output / "writeback-plan.json", plan)
        write(output / "result.json", report)
        emit("finished", selected_connectivity_passed=passed, added=report["added"])
        if not args.full_output:
            print(json.dumps(_summary(report, output), ensure_ascii=False, separators=(",", ":")))
        return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
