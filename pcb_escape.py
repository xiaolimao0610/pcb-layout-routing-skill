"""显式外围焊盘的短线引出：真实出口、有限回溯、原检查器精检。"""
from __future__ import annotations

from copy import deepcopy
import time

from shapely.geometry import Point

from pcb_fanout import _commit_escape, _world
from pcb_pipeline_common import (CollisionModel, PipelineError, check_board, finite,
                                 normalize_board, rules_for, strict_options, _fast_pad_bounds)
from pcb_solver_hardened import _body


_DEFAULTS = dict(enabled=False, max_seconds=30.0, max_candidates=5000,
                 max_backtracks=32, max_candidates_per_pin=64, groups=[])
_GROUP = {"component", "pads", "sides", "escape_layers", "escape_margin",
          "via_distances", "lateral_offsets", "provenance"}
# 上/下以封装局部坐标的负/正 Y 定义；随后应用实际旋转与底层镜像。
_SIDES = {"left": (-1, 0), "right": (1, 0), "top": (0, -1), "bottom": (0, 1)}


def validate_options(value=None):
    """输入 escape 对象，返回严格配置副本；未知字段、越界参数抛 PipelineError。

    pads 仅为本次明确选择的真实 padId，不猜测器件类别或网络语义。
    单位沿用完整板的 mil；provenance 保存关系来源文字，不能提供历史坐标。
    """
    result = deepcopy(dict(_DEFAULTS, **strict_options(value, _DEFAULTS, "escape")))
    if type(result["enabled"]) is not bool:
        raise PipelineError("escape.enabled 必须为布尔值")
    result["max_seconds"] = finite(result["max_seconds"], "escape.max_seconds", positive=True)
    for name, low, high in (("max_candidates", 1, 1000000), ("max_backtracks", 0, 100000),
                            ("max_candidates_per_pin", 1, 512)):
        if type(result[name]) is not int or not low <= result[name] <= high:
            raise PipelineError(f"escape.{name} 必须为 {low}..{high} 的整数")
    groups = result["groups"]
    if not isinstance(groups, list) or len(groups) > 128 or (result["enabled"] and not groups):
        raise PipelineError("启用 escape 需要 1..128 个显式引出组")
    seen, output = set(), []
    for raw in groups:
        group = deepcopy(strict_options(raw, _GROUP, "escape.groups"))
        if {"component", "pads", "sides", "escape_layers", "provenance"} - set(group):
            raise PipelineError("escape 需要 component/pads/sides/escape_layers/provenance")
        if not isinstance(group["component"], str) or not group["component"]:
            raise PipelineError("escape.component 必须为非空器件 ID")
        for name in ("pads", "sides", "escape_layers"):
            values = group[name]
            if (not isinstance(values, list) or not values or len(values) > 4096
                    or any(type(v) is not (int if name == "escape_layers" else str) for v in values)
                    or len(set(values)) != len(values)):
                raise PipelineError(f"escape.{name} 需要非空、不重复的显式列表")
        if any(not pad for pad in group["pads"]) or set(group["sides"]) - set(_SIDES):
            raise PipelineError("escape.padId 不能为空；sides 仅支持 left/right/top/bottom")
        keys = {(group["component"], pad) for pad in group["pads"]}
        if keys & seen:
            raise PipelineError("同一焊盘不能在 escape.groups 重复声明")
        seen.update(keys)
        if not isinstance(group["provenance"], str) or not group["provenance"].strip():
            raise PipelineError("escape.provenance 需要明确的配置/手册来源说明")
        group["escape_margin"] = finite(group.get("escape_margin", 30), "escape.escape_margin", positive=True)
        for name, default, minimum in (("via_distances", [20, 40], 0), ("lateral_offsets", [0, -10, 10], -float("inf"))):
            values = group.get(name, default)
            if not isinstance(values, list) or not 1 <= len(values) <= 16:
                raise PipelineError(f"escape.{name} 需要 1..16 个候选距离")
            values = [finite(v, "escape."+name, minimum=minimum) for v in values]
            if len(set(values)) != len(values):
                raise PipelineError(f"escape.{name} 候选不能重复")
            group[name] = values
        output.append(group)
    result["groups"] = output
    return result


def _tasks(board, config):
    """依据当前板实际封装产生任务；引用、层栈或完整网表不符则拒绝。"""
    components = {c["id"]: c for c in board["components"]}
    rules, tasks = rules_for(board), []
    for group in config["groups"]:
        if group["component"] not in components:
            raise PipelineError("escape 引用了未知器件")
        component = components[group["component"]]
        pads = {p["padId"]: p for p in component["pads"]}
        if not set(group["pads"]).issubset(pads):
            raise PipelineError("escape 引用了未知焊盘")
        if not set(group["escape_layers"]).issubset(rules.copper_layers):
            raise PipelineError("escape_layers 包含当前板不存在的铜层")
        local = dict(component, x=0., y=0., rotation=0., layer=1)
        bounds = [_body(local, rules.curve_tolerance).outer.bounds]
        bounds.extend(_fast_pad_bounds(local, p, rules.curve_tolerance).outer.bounds for p in pads.values())
        extent = (min(b[0] for b in bounds), min(b[1] for b in bounds),
                  max(b[2] for b in bounds), max(b[3] for b in bounds))
        for pid in group["pads"]:
            pad = pads[pid]
            if pad.get("hole") not in (None, 0) or not pad.get("net"):
                raise PipelineError("普通引出目前需要有明确网络的贴片焊盘")
            key = component["id"], pid
            if key not in {(p["compId"], p["padId"]) for p in board["nets"].get(pad["net"], [])}:
                raise PipelineError("escape 焊盘网络与完整网表不一致")
            tasks.append(dict(component=component, pad=pad, group=group, extent=extent))
    return tasks


def _exit_point(task, side, lateral):
    """根据本板真实包络和显式侧向生成出口中心，不读取旧阶段报告。"""
    pad, component, group = task["pad"], task["component"], task["group"]
    x, y = pad["localX"], pad["localY"]
    xmin, ymin, xmax, ymax = task["extent"]
    dx, dy = _SIDES[side]
    endpoint = (xmin-group["escape_margin"] if dx < 0 else xmax+group["escape_margin"] if dx > 0 else x-dy*lateral,
                ymin-group["escape_margin"] if dy < 0 else ymax+group["escape_margin"] if dy > 0 else y+dx*lateral)
    return _world(component, endpoint)


def _existing_outlet(board, task, options, budget, check):
    """复核已有铜是否把真实焊盘接到声明出口；返回证据或 None，不相信 metadata。

    用原 _groups 重建实体连通组，不能因旧报告声称 completed 就跳过本次引出。
    此处只复用已有合规铜，不新增通孔，也不改变原层/宽度/端子。
    """
    from pcb_routing import _groups, _net_options
    pad, component, group = task["pad"], task["component"], task["group"]
    net, key = pad["net"], (component["id"], pad["padId"])
    settings = _net_options(board, net, options)
    face = pad.get("layer", component["layer"])
    if face not in (1, 2) or face not in settings["layers"]:
        raise PipelineError("escape 焊盘表面不在该网络允许布线层中")
    layers = [layer for layer in group["escape_layers"] if layer in settings["layers"]]
    if not layers:
        raise PipelineError("escape 未声明满足 CHANNEL_SPEC/trace_layers 的出口层")
    groups = [item for item in _groups(board, net, budget) if key in item["terminals"]]
    for side in group["sides"]:
        for lateral in group["lateral_offsets"]:
            endpoint = _exit_point(task, side, lateral)
            for layer in layers:
                check()
                if any(layer in item["layers"] and item["layers"][layer].covers(Point(endpoint)) for item in groups):
                    return dict(component=component["id"], pad=pad["padId"], net=net, exit_layer=layer,
                        exit_point=list(endpoint), side=side, via_count=0, via_in_pad=False,
                        provenance=group["provenance"], reused=True, evidence="actual_terminal_copper_group")
    return None


def _choices(board, task, options, check, limit):
    """生成直出、45° 折线与短线接全栈孔的有界候选；初筛不取代提交精检。"""
    from pcb_routing import _net_options, _octilinear_doglegs, _candidate_cost
    component, pad, group = task["component"], task["pad"], task["group"]
    net, face = pad["net"], pad.get("layer", component["layer"])
    settings, stack = _net_options(board, net, options), list(rules_for(board).copper_layers)
    if face not in (1, 2) or face not in settings["layers"]:
        raise PipelineError("escape 焊盘表面不在该网络允许布线层中")
    layers = [layer for layer in group["escape_layers"] if layer in settings["layers"]]
    if not layers:
        raise PipelineError("escape 未声明满足 CHANNEL_SPEC/trace_layers 的出口层")
    if any(layer != face for layer in layers) and (not settings["vias"] or not set(stack).issubset(settings["via_layers"])):
        raise PipelineError("escape 换层需要允许完整物理层栈通孔")
    x, y = pad["localX"], pad["localY"]
    start, model, result, seen = _world(component, (x, y)), CollisionModel(board), [], set()
    for side in group["sides"]:
        dx, dy = _SIDES[side]
        for lateral in group["lateral_offsets"]:
            end = _exit_point(task, side, lateral)
            for layer in layers:
                for distance in ([0.] if layer == face else group["via_distances"]):
                    check()
                    landing = start if layer == face else _world(component, (x+dx*distance-dy*lateral, y+dy*distance+dx*lateral))
                    vias = [] if layer == face else [dict(net=net, x=landing[0], y=landing[1], hole=settings["hole"], diameter=settings["diameter"], layers=stack)]
                    if vias and not model.via_ok(net, *landing, settings["hole"], settings["diameter"], stack):
                        continue
                    for launch in _octilinear_doglegs(start, landing, options.get("diagonal", True), settings["width"]):
                        for tail in _octilinear_doglegs(landing, end, options.get("diagonal", True), settings["width"]):
                            check()
                            segments = [(face, a, b) for a, b in zip(launch, launch[1:]) if a != b]
                            segments += [(layer, a, b) for a, b in zip(tail, tail[1:]) if a != b]
                            signature = tuple(segments), layer, landing
                            if not segments or signature in seen:
                                continue
                            seen.add(signature)
                            traces = [dict(net=net, layer=l, sx=a[0], sy=a[1], ex=b[0], ey=b[1], width=settings["width"]) for l, a, b in segments]
                            if not all(model.trace_ok(net, t["layer"], t["sx"], t["sy"], t["ex"], t["ey"], t["width"]) for t in traces):
                                continue
                            evidence = dict(component=component["id"], pad=pad["padId"], net=net,
                                exit_layer=layer, exit_point=list(end), side=side, via_count=len(vias),
                                via_in_pad=bool(vias and model.via_in_pad(*landing, settings["hole"], stack)), provenance=group["provenance"])
                            result.append((_candidate_cost(model, (traces, vias), settings), traces, vias, evidence))
                            if len(result) >= limit:
                                return sorted(result, key=lambda v: v[0])
    return sorted(result, key=lambda v: v[0])


def escape_board(board, routing_options, stats, budget, excluded_nets=()):
    """输入完整板/路由选项/统计/父预算，返回整批显式焊盘引出的候选副本。

    使用少出口优先及有限回溯；每条枝经原 _commit_escape 精检与真实端子
    连通证明。GND 铺铜流程及差分/等长/拓扑网络明确暂缓。不修改输入，失败
    不返回部分候选；配置错误抛 PipelineError，未找到/超预算抛 RoutingFailure。
    completed 仅表示声明引出完成，不代表整网接通或整板验收。
    """
    from pcb_routing import RoutingFailure
    cfg = validate_options(routing_options.get("escape"))
    if not cfg["enabled"]:
        return deepcopy(board)
    original, started = normalize_board(board), time.monotonic()
    record = dict(method="explicit_peripheral_escape", candidate_checks=0, backtracks=0,
                  escaped=[], reused=[], deferred=[], ordering=[], completed=False, full_board_accepted=False, budget_exhausted=False)
    stats["escape"] = record
    def check(candidate=False):
        budget.check()
        if time.monotonic()-started >= cfg["max_seconds"] or (candidate and record["candidate_checks"] >= cfg["max_candidates"]):
            record["budget_exhausted"] = True
            raise RoutingFailure("ESCAPE_BUDGET", "外围引出预算内未找到完整候选，未证明无解", escape=record)
        if candidate:
            record["candidate_checks"] += 1
    if isinstance(excluded_nets, (str, bytes)) or not isinstance(excluded_nets, (list, tuple, set, frozenset)) or any(not isinstance(n, str) for n in excluded_nets):
        raise PipelineError("escape.excluded_nets 需要显式网名集合")
    excluded = set(excluded_nets)
    if routing_options.get("ground_strategy") == "plane_then_drc":
        grounds = routing_options.get("ground_nets")
        if not isinstance(grounds, list) or not grounds or any(not isinstance(n, str) for n in grounds) or len(grounds) != len(set(grounds)):
            raise PipelineError("plane_then_drc 需要显式且不重复的地网列表")
        excluded.update(grounds)
    if not excluded.issubset(original["nets"]):
        raise PipelineError("escape 排除网络包含未知网络")
    protected = set(routing_options.get("topology_orders", {}))
    for rule in original.get("metadata", {}).get("pipeline_constraints", []):
        if rule.get("diff_pair"):
            protected.update((rule["diff_pair"]["p"], rule["diff_pair"]["n"]))
        if rule.get("length_group"):
            protected.add(rule["net"])
    try:
        check()
        if check_board(original, ignore_connectivity=True):
            raise PipelineError("escape 输入存在硬几何错误，不能以短线引出掩盖")
        tasks = []
        for task in _tasks(original, cfg):
            check()
            net = task["pad"]["net"]
            if net in excluded | protected:
                record["deferred"].append(dict(component=task["component"]["id"], pad=task["pad"]["padId"], net=net,
                    reason="专用差分/等长/拓扑流程保护" if net in protected else "显式排除或原生铺铜后补地"))
                continue
            budget.enter(net)
            try:
                existing = _existing_outlet(original, task, routing_options, budget, lambda: check(True))
                if existing is not None:
                    record["reused"].append(existing)
                    check()
                    continue
                task["initial_choices"] = _choices(original, task, routing_options, lambda: check(True), cfg["max_candidates_per_pin"])
                check()
            finally:
                budget.leave()
            tasks.append(task)
        tasks.sort(key=lambda task: (len(task["initial_choices"]), task["component"]["id"], task["pad"]["padId"]))
        record["ordering"] = [dict(component=t["component"]["id"], pad=t["pad"]["padId"], bounded_outlets=len(t["initial_choices"])) for t in tasks]
        states, choices, indices, evidence, index = [original], [], [], [], 0
        while index < len(tasks):
            check()
            task = tasks[index]
            if len(choices) <= index:
                budget.enter(task["pad"]["net"])
                try:
                    choices.append(task["initial_choices"] if index == 0 else _choices(states[index], task, routing_options, lambda: check(True), cfg["max_candidates_per_pin"]))
                    check()
                finally:
                    budget.leave()
                indices.append(0)
            if indices[index] >= len(choices[index]):
                if index == 0:
                    raise RoutingFailure("ESCAPE_NOT_FOUND", "有限出口候选中未找到整批引出，未证明无解", escape=record)
                if record["backtracks"] >= cfg["max_backtracks"]:
                    record["budget_exhausted"] = True
                    raise RoutingFailure("ESCAPE_BUDGET", "外围引出回溯预算耗尽，未找到完整候选，未证明无解", escape=record)
                record["backtracks"] += 1
                choices.pop(); indices.pop(); states.pop(); evidence.pop()
                index -= 1
                continue
            _, traces, vias, entry = choices[index][indices[index]]
            indices[index] += 1
            check(True)
            budget.enter(entry["net"])
            try:
                candidate = _commit_escape(states[index], traces, vias, entry, budget)
                check()
            finally:
                budget.leave()
            if candidate is not None:
                states.append(candidate); evidence.append(entry); index += 1
        result = states[-1]
        record["escaped"] = record["reused"]+evidence
        record["generated_outlets"] = len(evidence)
        record["scope"] = "全部声明且未暂缓的焊盘出口；不是整封装、整网或整板验收"
        record["elapsed_seconds"] = time.monotonic()-started
        check()
        result.setdefault("metadata", {})["escape_candidate"] = deepcopy(record)
        check()
        record["completed"] = result["metadata"]["escape_candidate"]["completed"] = True
        return result
    except RoutingFailure as error:
        if "BUDGET" in error.diagnostics.get("code", ""):
            record["budget_exhausted"] = True
            record["stopped_by"] = error.diagnostics["code"]
        raise
    finally:
        record["elapsed_seconds"] = time.monotonic()-started
