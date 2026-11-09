"""工程接续状态的来源绑定索引；不派发任务、不写EDA、不授予整板验收。"""
from __future__ import annotations

import argparse
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import re
import uuid

from pcb_pipeline_common import PipelineError, check_board, fingerprint, normalize_board, rules_for


_STATE_KEYS = {"schema_version", "revision", "baseline", "best_candidate", "passed_evidence",
               "running_tasks", "task_history", "unresolved_issues", "next_step", "runs"}
_MAX_JSON_BYTES = 32 * 1024 * 1024
_MAX_RUNS = 1000
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_SOLVE_TASK_KINDS = {"solver", "offline_workflow"}


def _object(value, keys, label):
    """校验精确字段集合，返回原对象；缺失、未知字段或类型不符抛PipelineError。"""
    if not isinstance(value, dict) or set(value) != set(keys):
        raise PipelineError(f"{label} 必须恰含字段 {sorted(keys)}")
    return value


def _text(value, label, maximum=4096):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise PipelineError(f"{label} 必须为非空且长度不超过{maximum}的字符串")
    return value


def _bytes(path):
    """读取有限大小实际文件；不存在、非文件、过大及读取错误明确失败。"""
    path = Path(path)
    if not path.is_file() or path.stat().st_size > _MAX_JSON_BYTES:
        raise PipelineError(f"索引文件不存在、不是文件或超过32MiB：{path}")
    data = path.read_bytes()
    if len(data) > _MAX_JSON_BYTES:
        raise PipelineError(f"读取中的索引文件超过32MiB：{path}")
    return data


def file_sha256(path):
    """输入实际文件路径，返回字节SHA256；路径/大小错误及OSError原样失败。"""
    return hashlib.sha256(_bytes(path)).hexdigest()


def _json_bytes(data):
    def invalid(value):
        raise PipelineError(f"JSON不接受非有限数值：{value}")
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise PipelineError(f"JSON重复字段：{key}")
            result[key] = value
        return result
    try:
        return json.loads(data.decode("utf-8-sig"), parse_constant=invalid, object_pairs_hook=pairs)
    except (ValueError, UnicodeError) as exc:
        raise PipelineError(f"索引JSON无效：{exc}") from exc


def _reference(path):
    resolved = Path(path).resolve()
    data = _bytes(resolved)
    return {"path": str(resolved), "sha256": hashlib.sha256(data).hexdigest()}, _json_bytes(data)


def _artifact(reference):
    """核对完整索引和实际字节摘要，返回同一次读取的JSON，不改写摘要。"""
    _object(reference, {"path", "sha256"}, "文件索引")
    path = Path(_text(reference["path"], "索引路径"))
    if not path.is_absolute() or not isinstance(reference["sha256"], str) or not _HASH.fullmatch(reference["sha256"]):
        raise PipelineError("索引必须使用绝对路径与64位小写SHA256")
    data = _bytes(path)
    if hashlib.sha256(data).hexdigest() != reference["sha256"]:
        raise PipelineError(f"索引文件已变化，停止接续并核实来源，不得手换摘要：{path}")
    return _json_bytes(data)


def _source(board):
    """提取实际source身份，保留test_only用途；缺来源不创建可接续基线。"""
    meta = board.get("metadata", {})
    source = meta.get("source", {})
    if not isinstance(source, dict):
        raise PipelineError("完整基线缺少metadata.source")
    identity = {"document_uuid": _text(source.get("document_uuid"), "来源UUID"),
                "fingerprint": _text(source.get("fingerprint"), "来源指纹"),
                "usage": source.get("usage", meta.get("board_usage", "physical"))}
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", identity["fingerprint"]):
        raise PipelineError("来源fingerprint必须为sha256:加64位小写摘要")
    if identity["usage"] not in ("physical", "test_only"):
        raise PipelineError("来源usage非法")
    if meta.get("board_usage") == "test_only":
        identity["usage"] = "test_only"
    return identity


def init_state(board_path, *, next_step="读取当前基线与约束，明确下一轮候选合同"):
    """以完整实际board文件建立空状态，不生成候选或通过证据。

    入参为基线文件路径和明确下一步；返回独立dict。基线可未布通，但格式和
    source身份必须完整。非法JSON、无来源、未知板字段抛PipelineError，IO错误传播。
    """
    ref, raw = _reference(board_path)
    board = normalize_board(raw)
    return dict(schema_version=1, revision=0, baseline=dict(board=ref, source=_source(board)),
                best_candidate=None, passed_evidence=[], running_tasks=[], task_history=[], unresolved_issues=[],
                next_step=_text(next_step, "下一步"), runs=[])


def _run(run, baseline):
    """从索引工件重建本次范围与证据；不信任状态中的passed或缓存候选评价。"""
    _object(run, {"key", "input_task", "result", "candidate", "status"}, "运行记录")
    _text(run["key"], "运行键", 256)
    task, result = _artifact(run["input_task"]), _artifact(run["result"])
    if not isinstance(task, dict) or "board" not in task or not isinstance(result, dict):
        raise PipelineError("运行工件缺完整input-task或result")
    raw = normalize_board(task["board"])
    if fingerprint(raw) != fingerprint(baseline) or _source(raw) != _source(baseline):
        raise PipelineError("运行输入与基线完整快照不一致，不能跨来源或替换规则接续")
    scope = None
    if result.get("passed") is True and result.get("status") == "offline_passed":
        status, scope = "offline_passed", "offline_geometry"
    elif (result.get("status") == "awaiting_native_ground" and result.get("signal_stage_passed") is True
          and result.get("passed") is False and result.get("full_board_accepted") is False):
        status, scope = "awaiting_native_ground", "signal_geometry"
    elif result.get("passed") is False and not result.get("signal_stage_passed") and result.get("status") not in ("pending", "running"):
        status = "failed"
    else:
        raise PipelineError("运行状态未完成、相互矛盾或不受支持，不能登记为通过证据")
    if run["status"] != status:
        raise PipelineError("状态中的运行结果与实际result工件不同")
    if scope is None:
        if run["candidate"] is not None:
            raise PipelineError("失败运行不能索引通过候选")
        return None
    scope = verify_candidate(task, _artifact(run["candidate"]), result)
    return dict(run_key=run["key"], scope=scope, report=deepcopy(run["result"]), candidate=deepcopy(run["candidate"]))


def verify_candidate(task, candidate, result):
    """独立核对候选与实际输入，返回offline_geometry或signal_geometry范围。

    入参为完整task、实际候选板和结果字典；无需创建状态文件。复用原规则、
    固定/旧铜保护及完整检查器，不接受篡改来源、板形、端子、规则或验收范围。
    失败、未完成、源身份缺失或任何检查不通过抛PipelineError，不修改输入。
    完整离线范围若声明return_path，必须重新通过实际双向回流检查；信号范围
    继续等待原生铺铜。本函数不核实原生DRC，也不证明后处理过程已实际执行。
    """
    if not isinstance(result, dict):
        raise PipelineError("候选结果必须为对象")
    if result.get("passed") is True and result.get("status") == "offline_passed":
        scope = "offline_geometry"
    elif (result.get("status") == "awaiting_native_ground" and result.get("signal_stage_passed") is True
          and result.get("passed") is False and result.get("full_board_accepted") is False):
        scope = "signal_geometry"
    else:
        raise PipelineError("候选结果没有明确的离线或信号阶段通过范围")
    from pcb_pipeline import _task, build_writeback_plan
    from pcb_routing import check_routed_nets, deferred_ground_nets
    original, options, _ = _task(task)
    candidate = normalize_board(candidate)
    if _source(candidate) != _source(original):
        raise PipelineError("候选source与输入基线不符")
    # 按真实输入重新施加规则、标签、约束和端子形状，避免修改候选规则来通过。
    immutable = deepcopy(original)
    by_id = {c["id"]: c for c in candidate["components"]}
    if set(by_id) != {c["id"] for c in original["components"]} or candidate["nets"] != original["nets"]:
        raise PipelineError("候选器件或完整网表与输入不符")
    from pcb_placement import _options, _copper_locked, _envelope
    rules = rules_for(original)
    placement = _options(options.get("placement", {}), original, rules)
    from pcb_functional_cells import check_relations
    relation_errors = check_relations(candidate, placement.get("functional_cells", []))
    if relation_errors:
        raise PipelineError("候选违反功能单元的显式关系：" + "; ".join(relation_errors))
    locked = _copper_locked(original, rules.contact_tolerance)
    tags = original.get("tags", {}).get("component_tags", {})
    for component in immutable["components"]:
        cid = component["id"]
        moved = any(component[k] != by_id[cid][k] for k in ("x", "y", "rotation", "layer"))
        protected = (not placement["enabled"] or cid not in placement["movable_ids"] or cid in locked
                     or component.get("locked") or any(item.get("fixed") or "fixed_pose" in item
                     for item in (component, tags.get(cid, {}))))
        if moved and protected:
            raise PipelineError(f"候选移动了原固定、白名单外或旧铜保护器件：{cid}")
        if by_id[cid]["layer"] != component["layer"]:
            raise PipelineError(f"候选未经授权翻面：{cid}")
        if by_id[cid]["rotation"] != component["rotation"] and not any(
                abs((by_id[cid]["rotation"]-angle+180) % 360-180) < 1e-8 for angle in placement["rotations"]):
            raise PipelineError(f"候选旋转不在显式允许列表：{cid}")
        for key in ("x", "y", "rotation", "layer"):
            component[key] = by_id[cid][key]
        if cid in placement["regions"] and not placement["regions"][cid].covers(_envelope(component, rules.curve_tolerance)):
            raise PipelineError(f"候选器件超出明确布局区域：{cid}")
    immutable["routes"] = deepcopy(candidate["routes"])
    if (candidate["board_bbox"] != original["board_bbox"] or candidate["rules"] != original["rules"]
            or candidate["components"] != immutable["components"] or candidate["tags"] != original["tags"]
            or candidate["metadata"].get("pipeline_constraints", []) != original["metadata"].get("pipeline_constraints", [])):
        raise PipelineError("候选不能改板框、规则或端子/封装形状")
    deferred = deferred_ground_nets(original, options.get("routing", {}))
    if scope == "signal_geometry":
        if not deferred or result.get("deferred_ground_nets") != deferred:
            raise PipelineError("信号证据缺少与真实输入一致的延期地网")
        workflow = candidate["metadata"].get("native_ground_workflow", {})
        if (candidate["metadata"].get("preview_only") is not True or not isinstance(workflow, dict)
                or workflow.get("strategy") != "plane_then_drc" or workflow.get("ground_nets") != deferred
                or workflow.get("stage") != "awaiting_native_ground"):
            raise PipelineError("信号候选丢失预览或原生地网待处理标记，不能提升验收范围")
        errors = check_routed_nets(immutable, [n for n in immutable["nets"] if n not in deferred])
    else:
        if deferred:
            raise PipelineError("延期地网运行不能登记为完整离线通过")
        errors = check_board(immutable)
    build_writeback_plan(original, immutable)
    if errors:
        raise PipelineError("候选独立检查未通过：" + "; ".join(errors[:8]))
    if scope == "offline_geometry" and "return_path" in options:
        from pcb_return_path import check_return_paths
        quality = check_return_paths(immutable, options["return_path"], before=original)
        if quality.get("passed") is not True or quality.get("complete") is not True:
            raise PipelineError("候选的已声明回流复核未通过或证据不完整，不能凭result标记接受")
    return scope


def validate_state(state):
    """严格验证完整状态及全部实际索引，返回独立副本；失败抛PipelineError。

    最多1000次运行、64个在运行任务、1000个问题；每份JSON最多32MiB。
    不探测原生客户端当前来源，不派发或恢复任务；现场仍需fresh export核对。
    """
    _object(state, _STATE_KEYS, "project-state")
    if type(state["schema_version"]) is not int or state["schema_version"] != 1:
        raise PipelineError("project-state.schema_version必须为整数1")
    if type(state["revision"]) is not int or state["revision"] < 0:
        raise PipelineError("状态revision必须为非负整数")
    _object(state["baseline"], {"board", "source"}, "基线")
    baseline = normalize_board(_artifact(state["baseline"]["board"]))
    if state["baseline"]["source"] != _source(baseline):
        raise PipelineError("状态基线身份与实际文件不一致")
    for name, cap in (("runs", _MAX_RUNS), ("passed_evidence", _MAX_RUNS), ("running_tasks", 64), ("task_history", 1000), ("unresolved_issues", 1000)):
        if not isinstance(state[name], list) or len(state[name]) > cap:
            raise PipelineError(f"{name}必须为最多{cap}项的列表")
    _text(state["next_step"], "下一步")
    for issue in state["unresolved_issues"]:
        _text(issue, "未决问题")
    known, proven = {}, []
    for run in state["runs"]:
        evidence = _run(run, baseline)
        if run["key"] in known:
            raise PipelineError("重复运行键不能被登记为不同任务")
        known[run["key"]] = run
        if evidence is not None:
            proven.append(evidence)
    if state["passed_evidence"] != proven:
        raise PipelineError("passed_evidence必须恰为实际运行工件及独立检查得到的范围，不能手填通过")
    best = state["best_candidate"]
    if best is not None:
        _object(best, {"run_key", "board", "scope"}, "当前最佳候选")
        if not any(best == dict(run_key=e["run_key"], board=e["candidate"], scope=e["scope"]) for e in proven):
            raise PipelineError("最佳候选没有对应的同源独立通过证据")
    task_keys = set()
    for name, statuses in (("running_tasks", ("running", "pending", "unknown")),
                           ("task_history", ("completed", "failed", "cancelled"))):
        for task in state[name]:
            _object(task, {"key", "kind", "status", "output_dir"}, "任务记录")
            _text(task["key"], "任务键", 256)
            _text(task["kind"], "任务类型", 128)
            if task["status"] not in statuses:
                raise PipelineError(f"{name}任务状态仅支持{statuses}")
            if not Path(_text(task["output_dir"], "任务输出目录")).is_absolute():
                raise PipelineError("任务输出目录必须为绝对路径")
            if task["key"] in task_keys or (name == "running_tasks" and task["key"] in known):
                raise PipelineError("任务键重复或已登记完成，不能再次派发")
            if name == "task_history" and task["key"] in known:
                run_directory = Path(known[task["key"]]["input_task"]["path"]).resolve().parent
                if task["kind"] not in _SOLVE_TASK_KINDS or Path(task["output_dir"]).resolve() != run_directory:
                    raise PipelineError("完成任务的类型/输出目录与同键solve运行工件不一致")
            task_keys.add(task["key"])
    return deepcopy(state)


def load_state(path, *, current_board_path=None):
    """读取状态并复核被索引文件；可与调用方刚采集的当前板比较来源。

    入参为状态路径和可选的当前完整快照路径，返回独立状态字典；文件、
    格式或来源不一致抛原有异常。不连接EDA、不修改索引、不迁移旧证据。
    未提供当前快照时仅证明历史索引自洽，不能判断现场是否已经变化。
    """
    state = validate_state(_json_bytes(_bytes(path)))
    if current_board_path is not None:
        current = _json_bytes(_bytes(current_board_path))
        if _source(current) != state['baseline']['source']:
            raise PipelineError('当前快照与状态基线来源不一致；保留历史状态，'
                                '核查已有任务并基于新鲜来源创建新的状态文件，不能重放或手换摘要')
        # 来源已不同就立即拒绝，不再对大型铺铜板做无意义的完整规范化。
        normalize_board(current)
    return state


def save_state(path, state, *, expected_hash=None):
    """验证后原子保存，返回新文件SHA256；更新必须传入读取时的旧摘要。

    使用同目录独占短锁避免两个写者同时通过CAS；锁已存在、摘要不符或未知
    原状态均抛PipelineError，不重试、不覆盖。IO错误传播；不会修改被索引工件。
    """
    path = Path(path).resolve()
    if not path.parent.is_dir():
        raise PipelineError("状态输出父目录必须已存在")
    valid = validate_state(state)
    protected = {Path(valid["baseline"]["board"]["path"]).resolve()}
    for run in valid["runs"]:
        protected.update(Path(run[k]["path"]).resolve() for k in ("input_task", "result", "candidate") if run[k] is not None)
    if path in protected:
        raise PipelineError("状态文件不能覆盖被索引的基线或运行工件")
    data = (json.dumps(valid, ensure_ascii=False, allow_nan=False, indent=2)+"\n").encode("utf-8")
    if len(data) > _MAX_JSON_BYTES:
        raise PipelineError("状态超过32MiB上限")
    lock = path.with_name(path.name+".lock")
    temporary = path.with_name(path.name+"."+uuid.uuid4().hex+".tmp")
    try:
        handle = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError as exc:
        raise PipelineError("状态已有写入锁，请核查对应进程，不自动重放") from exc
    try:
        os.close(handle)
        if path.exists():
            if expected_hash is None or file_sha256(path) != expected_hash:
                raise PipelineError("状态已变化或缺少旧摘要，拒绝覆盖")
            previous = load_state(path)
            if valid["revision"] != previous["revision"]+1:
                raise PipelineError("状态更新revision必须恰好加1")
            if valid["baseline"] != previous["baseline"]:
                raise PipelineError("现有状态不可替换基线；真实来源变化后创建新状态文件")
            for name in ("runs", "task_history"):
                if valid[name][:len(previous[name])] != previous[name]:
                    raise PipelineError("不能删除或改写历史运行/任务键来重发任务")
            terminal_tasks = {r["key"]:r for r in valid["task_history"]}
            current_tasks = {r["key"]:r for r in valid["running_tasks"]}
            for task in previous["running_tasks"]:
                if task["key"] not in current_tasks:
                    ended = terminal_tasks.get(task["key"])
                    if ended is None:
                        raise PipelineError("运行中任务不能静默丢失；须登记已观察到的终结状态")
                    if any(task[k] != ended[k] for k in ("kind", "output_dir")):
                        raise PipelineError("任务终结时不可改写原类型或输出目录")
                if task["key"] in current_tasks and any(task[k] != current_tasks[task["key"]][k] for k in ("kind", "output_dir")):
                    raise PipelineError("同一运行中任务键的类型和输出目录不可改写")
        elif expected_hash is not None:
            raise PipelineError("预期更新的状态文件不存在")
        with temporary.open("xb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()
        lock.unlink()
    return hashlib.sha256(data).hexdigest()


def record_run(state, run_dir, run_key, *, select_best=False):
    """登记solve输出的真实工件，返回revision加1的新状态，不写文件。

    输入已有完整状态、solve目录及唯一键；可显式选为当前最佳，但该选择只
    表示工程决策，改善比较须看候选合同。同键运行中任务仅支持solver或
    offline_workflow，且实际输出目录须相同；其他任务不能借solve工件关闭。
    重复键、工件不匹配或候选失败抛错误。
    失败运行也保留报告；没有通过候选时禁止select_best。函数不启动任何任务。
    """
    result = validate_state(state)
    _text(run_key, "运行键", 256)
    if type(select_best) is not bool:
        raise PipelineError("select_best必须为布尔值")
    if any(run["key"] == run_key for run in result["runs"]):
        raise PipelineError("运行键已登记，不重复记录或派发")
    if any(task["key"] == run_key for task in result["task_history"]):
        raise PipelineError("任务键已终结，不能用新solve工件复用该键")
    directory = Path(run_dir).resolve()
    pending = next((task for task in result["running_tasks"] if task["key"] == run_key), None)
    if pending is not None:
        if pending["kind"] not in _SOLVE_TASK_KINDS:
            raise PipelineError("非solver/offline_workflow任务须按实际查询收据用task入口终结，不能用solve记录关闭")
        if Path(pending["output_dir"]).resolve() != directory:
            raise PipelineError("同键运行中任务的输出目录与run_dir不一致，拒绝关闭该任务")
    task_ref, _ = _reference(directory/"input-task.json")
    report_ref, report = _reference(directory/"result.json")
    accepted = report.get("passed") is True or report.get("signal_stage_passed") is True
    candidate_ref = _reference(directory/"board.json")[0] if accepted else None
    if not accepted and (directory/"board.json").exists():
        raise PipelineError("失败运行残留board.json，先核实工件而非登记为成功候选")
    run = dict(key=run_key, input_task=task_ref, result=report_ref, candidate=candidate_ref,
               status=report.get("status") if accepted else "failed")
    baseline = normalize_board(_artifact(result["baseline"]["board"]))
    evidence = _run(run, baseline)
    result["runs"].append(run)
    if evidence is not None:
        result["passed_evidence"].append(evidence)
    if select_best:
        if evidence is None:
            raise PipelineError("失败运行不能成为当前最佳候选")
        result["best_candidate"] = dict(run_key=run_key, board=evidence["candidate"], scope=evidence["scope"])
    for task in result["running_tasks"]:
        if task["key"] == run_key:
            result["task_history"].append(dict(task, status="completed" if evidence is not None else "failed"))
    result["running_tasks"] = [item for item in result["running_tasks"] if item["key"] != run_key]
    result["revision"] += 1
    return validate_state(result)


def set_task(state, key, kind, status, output_dir):
    """只登记外部工具已派发/查询的任务，返回revision加1的副本，不执行该任务。

    入参是实际键、类型、观察状态及绝对输出目录；终结键保存在task_history。
    重复终结键、身份变化或非法字段抛PipelineError。调用方须保留实际查询证据，
    状态文件不会从这条记录推断保存或DRC成功。
    """
    result = validate_state(state)
    if any(task["key"] == key for name in ("runs", "task_history") for task in result[name]):
        raise PipelineError("任务键已终结，禁止重新登记或重放")
    new = dict(key=key, kind=kind, status=status, output_dir=str(Path(output_dir).resolve()))
    previous = next((task for task in result["running_tasks"] if task["key"] == key), None)
    if previous and any(previous[k] != new[k] for k in ("kind", "output_dir")):
        raise PipelineError("相同任务键不能改写类型或输出目录")
    result["running_tasks"] = [task for task in result["running_tasks"] if task["key"] != key]
    result["task_history" if status in ("completed", "failed", "cancelled") else "running_tasks"].append(new)
    result["revision"] += 1
    return validate_state(result)


def main(argv=None):
    """状态CLI，成功返回0、配置/文件/来源错误返回1；不执行EDA或后台任务。"""
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init")
    init.add_argument("--board", required=True)
    init.add_argument("--output", required=True)
    init.add_argument("--next-step", default="读取当前基线与约束，明确下一轮候选合同")
    validate = sub.add_parser("validate")
    validate.add_argument("--state", required=True)
    validate.add_argument("--current-board", help="刚从当前EDA采集的完整快照；比较UUID、来源指纹和用途")
    record = sub.add_parser("record")
    record.add_argument("--state", required=True)
    record.add_argument("--run-dir", required=True)
    record.add_argument("--run-key", required=True)
    record.add_argument("--select-best", action="store_true")
    task_parser = sub.add_parser("task")
    task_parser.add_argument("--state", required=True)
    task_parser.add_argument("--key", required=True)
    task_parser.add_argument("--kind", required=True)
    task_parser.add_argument("--status", choices=["pending", "running", "unknown", "completed", "failed", "cancelled"], required=True)
    task_parser.add_argument("--output-dir", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "init":
            digest = save_state(args.output, init_state(args.board, next_step=args.next_step))
            state = load_state(args.output)
        else:
            digest = file_sha256(args.state)
            state = load_state(args.state, current_board_path=getattr(args, 'current_board', None))
            if args.command == "record":
                state = record_run(state, args.run_dir, args.run_key, select_best=args.select_best)
                digest = save_state(args.state, state, expected_hash=digest)
            elif args.command == "task":
                state = set_task(state, args.key, args.kind, args.status, args.output_dir)
                digest = save_state(args.state, state, expected_hash=digest)
        print(json.dumps(dict(valid=True, state_sha256=digest, revision=state["revision"],
            current_source_checked=bool(getattr(args, 'current_board', None)),
            baseline=state["baseline"], best_candidate=state["best_candidate"], running_tasks=state["running_tasks"],
            unresolved_issues=state["unresolved_issues"], next_step=state["next_step"],
            acceptance_note="状态是同源证据索引，不是原生DRC或整板验收凭证"), ensure_ascii=False, indent=2))
        return 0
    except (PipelineError, OSError, ValueError, TypeError, KeyError, AttributeError, OverflowError) as exc:
        print(json.dumps(dict(valid=False, error=str(exc)), ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
