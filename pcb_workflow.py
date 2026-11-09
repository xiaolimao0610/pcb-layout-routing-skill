"""显式的离线候选比较流程：每轮记合同，停滞后换已声明方法，不改默认求解器。"""
from __future__ import annotations

import argparse
from copy import deepcopy
import json
import math
from pathlib import Path
import time

from pcb_pipeline import _task, run_pipeline
from pcb_pipeline_common import (PipelineError, board_polygon, check_board, finite,
                                 fingerprint, normalize_board, rules_for, strict_options)
from pcb_routing import algorithm_options, check_routed_nets
from pcb_placement import _options as placement_options, _board_hpwl


_POLICY = {"methods", "max_rounds", "max_seconds", "stagnation_rounds", "objective",
           "min_relative_improvement", "target_nets", "target_regions"}
_METHOD = {"placement_method", "routing_method", "occupancy_grid"}
_METRICS = {"wire_length_mil", "via_count", "board_area_mil2", "hpwl"}


def _method_task(task, method, seed_offset=0):
    """返回只改已声明算法开关和可复现seed的任务副本，其他参数与硬约束原样保留。"""
    candidate = deepcopy(task)
    options = candidate.setdefault("options", {})
    placement = options.setdefault("placement", {})
    routing = options.setdefault("routing", {})
    placement["method"] = method["placement_method"]
    if type(placement.get("seed", 0)) is not int:
        raise PipelineError("placement.seed 必须为整数")
    placement["seed"] = placement.get("seed", 0) + seed_offset
    routing.update(method=method["routing_method"], occupancy_grid=method["occupancy_grid"])
    return candidate


def _comparison_context(task):
    """只排除算法开关与seed后比较完整配置，避免跨约束或预算继承旧最优记录。"""
    value = deepcopy(task)
    options = value.setdefault("options", {})
    placement = options.setdefault("placement", {})
    routing = options.setdefault("routing", {})
    for key in ("method", "seed"):
        placement.pop(key, None)
    for key in ("method", "occupancy_grid"):
        routing.pop(key, None)
    return fingerprint(value)


def validate_policy(task, policy=None):
    """校验任务和有限流程选项，返回完整原板与规范策略，不执行搜索。

    入参：原完整task和独立policy对象；出参(board, policy)。未知字段、方法
    与现有约束不兼容、非法目标或预算抛PipelineError，不删除参数来迁就方法。
    target只标记本轮关注范围，不过滤网表、障碍、端子或最终验收范围。
    """
    board, options, _ = _task(task)
    settings = strict_options(policy, _POLICY, "workflow")
    for key, default in (("max_rounds", 6), ("stagnation_rounds", 2)):
        value = settings.get(key, default)
        if type(value) is not int or not 1 <= value <= 20:
            raise PipelineError(f"workflow.{key} 必须为1..20整数")
        settings[key] = value
    settings["max_seconds"] = finite(settings.get("max_seconds", 600), "workflow.max_seconds", positive=True)
    settings["min_relative_improvement"] = finite(settings.get("min_relative_improvement", .01),
                                                   "workflow.min_relative_improvement", minimum=0)
    if settings["min_relative_improvement"] >= 1:
        raise PipelineError("workflow.min_relative_improvement 必须小于1")
    settings.setdefault("objective", "wire_length_mil")
    if not isinstance(settings["objective"], str) or settings["objective"] not in _METRICS:
        raise PipelineError("workflow.objective 为未知指标")
    current = algorithm_options(options.get("routing", {}))
    settings.setdefault("methods", [dict(placement_method=options.get("placement", {}).get("method", "legacy"),
                                         routing_method=current["method"], occupancy_grid=current["occupancy_grid"])])
    methods = settings["methods"]
    if not isinstance(methods, list) or not 1 <= len(methods) <= 6:
        raise PipelineError("workflow.methods 必须含1..6个明确方法")
    seen = set()
    for method in methods:
        if not isinstance(method, dict) or set(method) != _METHOD:
            raise PipelineError("每个方法必须恰含 placement_method/routing_method/occupancy_grid")
        if method["placement_method"] not in ("legacy", "analytic_sa"):
            raise PipelineError("未知布局方法")
        routing = algorithm_options(dict(method=method["routing_method"], occupancy_grid=method["occupancy_grid"]))
        key = (method["placement_method"], routing["method"], routing["occupancy_grid"])
        if key in seen:
            raise PipelineError("workflow.methods 不得重复；换seed不算换方法")
        seen.add(key)
        _, compiled_options, _ = _task(_method_task(task, method))
        placement_options(compiled_options.get("placement", {}), board, rules_for(board))
        algorithm_options(compiled_options.get("routing", {}))
    nets = settings.setdefault("target_nets", list(board["nets"]))
    if (not isinstance(nets, list) or not nets or any(not isinstance(n, str) for n in nets)
            or len(set(nets)) != len(nets) or set(nets)-set(board["nets"])):
        raise PipelineError("workflow.target_nets 必须为非空唯一真实网络列表")
    bounds = list(board_polygon(board).bounds)
    regions = settings.setdefault("target_regions", [bounds])
    if not isinstance(regions, list) or not regions or len(regions) > 64:
        raise PipelineError("workflow.target_regions 必须含1..64个世界mil矩形")
    for region in regions:
        if not isinstance(region, list) or len(region) != 4:
            raise PipelineError("目标区域必须为[xmin,ymin,xmax,ymax]")
        rect = [finite(v, "目标区域坐标") for v in region]
        if (rect[0] >= rect[2] or rect[1] >= rect[3] or rect[0] < bounds[0] or rect[1] < bounds[1]
                or rect[2] > bounds[2] or rect[3] > bounds[3]):
            raise PipelineError("目标区域须为板框包围盒内的正面积矩形")
    return board, settings


def candidate_contract(board, task, settings, index, method):
    """生成本轮目标、不变条件、观测指标和停止条件，返回纯JSON对象。

    入参是已验证的原板/任务/策略和轮号/方法；不变条件保存实际指纹，
    不将说明文字当作验收证据。调用者仍须执行原检查器和写回保护。
    """
    return dict(round=index, method=deepcopy(method), target_nets=list(settings["target_nets"]),
        target_regions=deepcopy(settings["target_regions"]), target_scope_filters_netlist=False,
        immutable_conditions=dict(source=deepcopy(board.get("metadata", {}).get("source", {})),
            netlist_fingerprint=fingerprint(board["nets"]), rules_fingerprint=fingerprint(board.get("rules", {})),
            constraints_fingerprint=fingerprint(board.get("metadata", {}).get("pipeline_constraints", [])),
            original_copper_fingerprint=fingerprint(board["routes"]), board_bbox_fingerprint=fingerprint(board["board_bbox"]),
            conditions=["完整端子和异网障碍不删减", "固定件/封装/孔/原铜保护沿用原验收器",
                        "不放宽间距或网络宽度", "来源绑定、备份、一次派发、几何双向读回保持",
                        "plane_then_drc与full_board_accepted保持原范围"]),
        observed_metrics=["accepted_scope", "returned_routed_nets", "wire_length_mil", "via_count",
                          "board_area_mil2", "hpwl", "elapsed_seconds"],
        stop_conditions=dict(max_rounds=settings["max_rounds"], max_seconds=settings["max_seconds"],
            stagnation_rounds=settings["stagnation_rounds"], objective=settings["objective"],
            min_relative_improvement=settings["min_relative_improvement"],
            solver_budgets=deepcopy(task.get("options", {})),
            unsafe_source_or_state="停止并重新取证；不重放在途任务",
            no_improvement="停止当前方法并转入声明的下一方法；列表耗尽则要求更换方法"))


def observe_result(result, task):
    """独立检查实际返回板后计算指标，失败返回未知几何指标而不是伪造零线长。

    入参为原run_pipeline报告和本轮任务；出参(指标,接受等级)。完整离线为2，
    等待原生地的信号范围为1，失败为0；都不表示原生整板通过。
    报告声称成功但实际板/范围不符时抛PipelineError，不登记为最佳候选。
    """
    metrics = dict(accepted_scope="none", returned_routed_nets=0, wire_length_mil=None,
                   via_count=None, board_area_mil2=None, hpwl=None,
                   elapsed_seconds=finite(result.get("elapsed_seconds", 0), "运行耗时", minimum=0))
    board = result.get("board")
    level = 2 if result.get("passed") is True else 1 if result.get("signal_stage_passed") is True else 0
    if not level:
        if board is not None or result.get("writeback_plan") is not None:
            raise PipelineError("失败候选不得带可接受板或写回计划")
        return metrics, 0
    if not isinstance(board, dict):
        raise PipelineError("成功候选缺少实际板")
    from pcb_project_state import verify_candidate
    verify_candidate(task, board, result)
    if level == 2:
        errors = check_board(board)
        nets = list(board["nets"])
    else:
        deferred = result.get("deferred_ground_nets", [])
        if result.get("status") != "awaiting_native_ground" or not deferred or result.get("full_board_accepted") is not False:
            raise PipelineError("信号候选缺少明确原生地等待状态")
        nets = [net for net in board["nets"] if net not in deferred]
        errors = check_routed_nets(board, nets)
    if errors:
        raise PipelineError("候选独立复核失败："+"; ".join(errors[:8]))
    metrics.update(accepted_scope="offline" if level == 2 else "signal_only", returned_routed_nets=len(nets),
        wire_length_mil=sum(math.hypot(t["ex"]-t["sx"], t["ey"]-t["sy"]) for t in board["routes"].get("traces", [])),
        via_count=sum(len(board["routes"].get(kind, [])) for kind in ("vias", "shield_vias")),
        board_area_mil2=float(board_polygon(board).area),
        hpwl=_board_hpwl(board["components"], board, rules_for(board).curve_tolerance, task.get("options", {}).get("placement", {})))
    return metrics, level


def is_improvement(metrics, level, best, settings):
    """先比较已验收范围，再比较同范围的指定几何指标；返回布尔，不按失败零值择优。"""
    if not level:
        return False
    if best is None or level > best["level"]:
        return True
    if level < best["level"]:
        return False
    key = settings["objective"]
    old, new = best["metrics"][key], metrics[key]
    return new < old and (old-new)/max(abs(old), 1e-12) >= settings["min_relative_improvement"]


def _write(path, value):
    """将本轮JSON写入新工件目录，拒绝非有限数据；文件异常原样传播。"""
    Path(path).write_text(json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2)+"\n", encoding="utf-8")


def run_workflow(task, policy, output_dir, project_state_path=None):
    """有界执行显式方法序列并保存每轮合同；返回workflow-result摘要，不写EDA。

    入参为完整task、独立policy、新输出目录及可选已初始化状态路径。每轮从
    同一原板求解，使用base_seed+全局轮号；保留原内部节点/轮数/时间预算。
    连续N轮无改善换下一方法，列表耗尽停止。总时间在轮间检查，不能抢占
    一次原求解/GEOS调用。状态源/文件冲突、未知在途任务、格式/文件错误
    抛PipelineError/OSError，保留已有轮次证据，禁止自动重放。
    """
    started = time.monotonic()
    board, settings = validate_policy(task, policy)
    folder = Path(output_dir).resolve()
    if folder.exists():
        raise PipelineError("工作流输出目录必须全新，不能重放已有轮次")
    state = state_hash = None
    previous_best = None
    resumed_method_index, resumed_stagnant = 0, 0
    exhausted = set()
    if project_state_path is not None:
        from pcb_project_state import load_state, file_sha256
        state_hash = file_sha256(project_state_path)
        state = load_state(project_state_path)
        if file_sha256(project_state_path) != state_hash:
            raise PipelineError("读取期间project-state变化，停止而非覆盖并发更新")
        baseline_path = Path(state["baseline"]["board"]["path"])
        baseline = normalize_board(json.loads(baseline_path.read_text(encoding="utf-8-sig")))
        if fingerprint(normalize_board(task["board"])) != fingerprint(baseline):
            raise PipelineError("工作流输入不等于project-state完整基线，先重新取证")
        if state.get("running_tasks"):
            raise PipelineError("project-state存在任务记录；先核对在途状态，不自动重复派发")
        # 从真实历史合同接续停滞计数，不允许换目录或换会话重启耗尽的方法。
        history = []
        for old in state["runs"]:
            prior_task = json.loads(Path(old["input_task"]["path"]).read_text(encoding="utf-8-sig"))
            prior_result = json.loads(Path(old["result"]["path"]).read_text(encoding="utf-8-sig"))
            if _comparison_context(prior_task) != _comparison_context(task):
                continue
            for contract in prior_result.get("candidate_rounds", []):
                stops = contract.get("stop_conditions", {})
                if (stops.get("objective") == settings["objective"]
                        and stops.get("min_relative_improvement") == settings["min_relative_improvement"]):
                    history.append(contract)
        exhausted = {fingerprint(c["method"]) for c in history if c.get("actual_stop_reason") == "STAGNATION_METHOD_STOP"}
        while resumed_method_index < len(settings["methods"]) and fingerprint(settings["methods"][resumed_method_index]) in exhausted:
            resumed_method_index += 1
        if resumed_method_index == len(settings["methods"]):
            raise PipelineError("历史候选合同显示这些方法已停滞；必须更换明确方法，不能换目录重启相同搜索")
        if history and history[-1].get("method") == settings["methods"][resumed_method_index]:
            resumed_stagnant = history[-1].get("consecutive_no_improvement", 0)
            if type(resumed_stagnant) is not int or resumed_stagnant < 0:
                raise PipelineError("历史合同停滞计数无效，停止接续")
        if state["best_candidate"] is not None:
            old = next(run for run in state["runs"] if run["key"] == state["best_candidate"]["run_key"])
            old_task = json.loads(Path(old["input_task"]["path"]).read_text(encoding="utf-8-sig"))
            if _comparison_context(old_task) != _comparison_context(task):
                raise PipelineError("状态最佳候选使用不同约束或预算；请明确重新建立比较状态，不能混用指标")
            old_result = json.loads(Path(old["result"]["path"]).read_text(encoding="utf-8-sig"))
            old_result["board"] = json.loads(Path(old["candidate"]["path"]).read_text(encoding="utf-8-sig"))
            old_metrics, old_level = observe_result(old_result, old_task)
            previous_best = dict(round=None, level=old_level, method=None, metrics=old_metrics,
                board_path=old["candidate"]["path"], result_path=old["result"]["path"], from_previous_run=True)
    folder.mkdir(parents=True, exist_ok=False)
    summary = dict(schema_version=1, status="running", policy=settings, rounds=[], best_candidate=None,
                   full_board_accepted=False, source=deepcopy(board.get("metadata", {}).get("source", {})),
                   method_changes=[], stop_reason=None, next_step=None)
    _write(folder/"workflow-result.json", summary)
    best, method_index, stagnant = previous_best, resumed_method_index, resumed_stagnant
    summary["resumed_method_index"] = method_index
    summary["resumed_no_improvement"] = stagnant
    key_prefix = "workflow:"+fingerprint({"output_dir":str(folder)})[:20]
    previous_started_method = None
    for index in range(settings["max_rounds"]):
        if time.monotonic()-started >= settings["max_seconds"]:
            summary["stop_reason"] = "TIME_BUDGET"
            break
        method = settings["methods"][method_index]
        if previous_started_method is not None and method != previous_started_method:
            summary["method_changes"].append(dict(start_round=index, previous=deepcopy(previous_started_method),
                                                 next=deepcopy(method), reason="连续N轮无改善", executed=True))
        previous_started_method = deepcopy(method)
        trial = _method_task(task, method, index)
        contract = candidate_contract(board, trial, settings, index, method)
        run_dir = folder/f"round-{index+1:03d}"
        run_dir.mkdir(exist_ok=False)
        _write(run_dir/"input-task.json", trial)
        _write(run_dir/"candidate-contract.json", contract)
        run_key = f"{key_prefix}:round-{index+1:03d}"
        if state is not None:
            from pcb_project_state import set_task, save_state
            state = set_task(state, run_key, "offline_workflow", "running", run_dir)
            state["next_step"] = f"查询已登记候选任务 {run_key} 的真实工件；未知完成状态禁止重放"
            state_hash = save_state(project_state_path, state, expected_hash=state_hash)
        kwargs = {}
        checkpoint_path = run_dir / "calculation-checkpoint.json"
        if trial.get("options", {}).get("routing", {}).get("checkpoint", False):
            from pcb_automation import write_checkpoint
            kwargs["checkpoint_callback"] = lambda value: write_checkpoint(checkpoint_path, value)
        result = run_pipeline(trial, **kwargs)
        if checkpoint_path.is_file():
            from pcb_project_state import file_sha256
            result["calculation_checkpoint"] = {"path": str(checkpoint_path.resolve()),
                "sha256": file_sha256(checkpoint_path), "writeback_allowed": False}
        metrics, level = observe_result(result, trial)
        improved = is_improvement(metrics, level, best, settings)
        stagnant = 0 if improved else stagnant+1
        contract.update(observations=metrics, improved=improved, consecutive_no_improvement=stagnant,
                        result_status=result.get("status"), actual_stop_reason=None)
        if level:
            _write(run_dir/"board.json", result["board"])
            _write(run_dir/"writeback-plan.json", result["writeback_plan"])
        if improved:
            best = dict(round=index, level=level, method=deepcopy(method), metrics=deepcopy(metrics),
                        board_path=str(run_dir/"board.json"), result_path=str(run_dir/"result.json"))
        if stagnant >= settings["stagnation_rounds"]:
            contract["actual_stop_reason"] = "STAGNATION_METHOD_STOP"
            exhausted.add(fingerprint(method))
            next_index = method_index+1
            while next_index < len(settings["methods"]) and fingerprint(settings["methods"][next_index]) in exhausted:
                next_index += 1
            if next_index < len(settings["methods"]):
                method_index = next_index
                stagnant = 0
                contract["next_method"] = deepcopy(settings["methods"][method_index])
            else:
                summary["stop_reason"] = "METHOD_SEQUENCE_EXHAUSTED"
        result["candidate_rounds"] = [contract]
        _write(run_dir/"candidate-contract.json", contract)
        _write(run_dir/"result.json", {k:v for k,v in result.items() if k not in ("board", "writeback_plan")})
        if state is not None:
            from pcb_project_state import record_run, save_state
            state = record_run(state, run_dir, run_key, select_best=improved)
            state["next_step"] = ("当前方法已停滞，核对下一方法及剩余预算后接续" if contract["actual_stop_reason"]
                                  else "读取已保存的候选合同与指标，核对最佳候选和剩余预算")
            state_hash = save_state(project_state_path, state, expected_hash=state_hash)
        summary["rounds"].append(contract)
        summary["best_candidate"] = deepcopy(best)
        _write(folder/"workflow-result.json", summary)
        if summary["stop_reason"]:
            break
    summary.update(status="completed_search", stop_reason=summary["stop_reason"] or "ROUND_BUDGET",
        elapsed_seconds=time.monotonic()-started,
        next_step="已停止当前搜索；未完成时按证据更换明确方法或修正约束，不把相同配置重新循环。",
        best_candidate=best, passed=best is not None,
        scope="预算内候选比较；离线/信号范围见最佳候选，未执行原生DRC，不证明全局最优")
    _write(folder/"workflow-result.json", summary)
    if state is not None:
        state["revision"] += 1
        state["next_step"] = summary["next_step"]+f" 汇总：{folder/'workflow-result.json'}"
        if best is None:
            state["unresolved_issues"].append(f"候选搜索未找到已验收结果：{folder/'workflow-result.json'}")
        save_state(project_state_path, state, expected_hash=state_hash)
    return summary


def main(argv=None):
    """读取任务和独立策略执行离线候选流程；有已验证候选退出0，无候选/错误退出1。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--policy", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--project-state", type=Path)
    args = parser.parse_args(argv)
    try:
        read = lambda path: json.loads(path.read_text(encoding="utf-8-sig"))
        result = run_workflow(read(args.input), read(args.policy) if args.policy else {}, args.output_dir, args.project_state)
        print(json.dumps({k:result[k] for k in ("status", "passed", "stop_reason", "best_candidate", "elapsed_seconds")}, ensure_ascii=False))
        return 0 if result["passed"] else 1
    except (PipelineError, OSError, ValueError, TypeError, KeyError, OverflowError) as exc:
        print(json.dumps(dict(passed=False, status="failed", error=str(exc)), ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
