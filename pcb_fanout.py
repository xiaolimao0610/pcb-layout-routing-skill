"""显式球阵列的整封装有界扇出，使用完整物理层栈通孔和真实出口铜。"""
from __future__ import annotations

from copy import deepcopy
import math
import time

from shapely.geometry import Point

from pcb_pipeline_common import (CollisionModel, PipelineError, check_board, finite,
                                 normalize_board, rules_for, strict_options, _fast_pad_bounds)
from pcb_solver_hardened import _body


_DEFAULTS = dict(enabled=False, technology="through", max_seconds=30.0,
                 max_candidates=2000, max_backtracks=32, packages=[])
_PACKAGE = {"component", "balls", "escape_layers", "order", "escape_margin", "via_offsets"}


def validate_options(value=None):
    """输入嵌套 fanout 配置，返回严格副本；未知字段/工艺/阵列格式抛 PipelineError。

    工艺仅接受 through；真实规则、球阵列几何与网络覆盖由 fanout_board 复核。
    每个非空球位必须为当前工程的真实 padId，不推断封装或网名。
    """
    result = dict(_DEFAULTS, **strict_options(value, _DEFAULTS, "fanout"))
    if type(result["enabled"]) is not bool:
        raise PipelineError("fanout.enabled 必须为布尔值")
    if result["technology"] != "through":
        raise PipelineError("fanout 仅支持已验证的全叠层通孔；盲孔/埋孔/微孔工艺尚未支持")
    result["max_seconds"] = finite(result["max_seconds"], "fanout.max_seconds", positive=True)
    for name, low, high in (("max_candidates", 1, 1000000), ("max_backtracks", 0, 100000)):
        if type(result[name]) is not int or not low <= result[name] <= high:
            raise PipelineError(f"fanout.{name} 必须为 {low}..{high} 的整数")
    packages = result["packages"]
    if not isinstance(packages, list) or len(packages) > 32 or (result["enabled"] and not packages):
        raise PipelineError("启用 fanout 需要 1..32 个显式封装")
    result["packages"], used = [], set()
    for raw in packages:
        item = strict_options(raw, _PACKAGE, "fanout.packages")
        if {"component", "balls", "escape_layers"} - set(item):
            raise PipelineError("扇出封装需要 component、balls、escape_layers")
        cid = item["component"]
        if not isinstance(cid, str) or not cid or cid in used:
            raise PipelineError("扇出 component 必须为非空、不重复的器件ID")
        used.add(cid)
        rows = item["balls"]
        if (not isinstance(rows, list) or not 2 <= len(rows) <= 128
                or any(not isinstance(row, list) for row in rows)
                or not 2 <= len(rows[0]) <= 128 or any(len(row) != len(rows[0]) for row in rows)):
            raise PipelineError("balls 需要至少2行2列的矩形球阵列，空球位用null")
        pads = [pid for row in rows for pid in row if pid is not None]
        if (not 4 <= len(pads) <= 4096 or any(not isinstance(pid, str) or not pid for pid in pads)
                or len(set(pads)) != len(pads)):
            raise PipelineError("balls 需要4..4096个唯一真实padId，不能以单点代替整封装")
        layers = item["escape_layers"]
        if (not isinstance(layers, list) or not layers or any(type(layer) is not int for layer in layers)
                or len(set(layers)) != len(layers)):
            raise PipelineError("escape_layers 必须为非空唯一物理铜层列表")
        item.setdefault("order", "outer_first")
        if item["order"] not in ("outer_first", "inner_first"):
            raise PipelineError("fanout.order 必须为 outer_first 或 inner_first")
        item["escape_margin"] = finite(item.get("escape_margin", 40), "fanout.escape_margin", positive=True)
        offsets = item.get("via_offsets", [0.5, 1.0])
        if not isinstance(offsets, list) or not 1 <= len(offsets) <= 16:
            raise PipelineError("via_offsets 需要1..16个相对球间距的落孔偏移")
        item["via_offsets"] = [finite(v, "fanout.via_offsets", minimum=0) for v in offsets]
        if any(v > 4 for v in item["via_offsets"]) or len(set(item["via_offsets"])) != len(item["via_offsets"]):
            raise PipelineError("via_offsets 需要0..4内不重复的偏移倍数")
        result["packages"].append(item)
    return result


def _world(component, point):
    x, y = point
    if component["layer"] == 2:
        x = -x
    angle = math.radians(component["rotation"])
    return (component["x"] + x * math.cos(angle) - y * math.sin(angle),
            component["y"] + x * math.sin(angle) + y * math.cos(angle))


def _array(board, package):
    """验证完整阵列与实际局部坐标一致，返回器件、球位及真实行列间距。"""
    components = {c["id"]: c for c in board["components"]}
    if package["component"] not in components:
        raise PipelineError("fanout 引用未知器件")
    component = components[package["component"]]
    pads = {p["padId"]: p for p in component["pads"]}
    declared = {pid for row in package["balls"] for pid in row if pid is not None}
    if declared != set(pads):
        raise PipelineError("balls 必须完整覆盖所选器件全部焊盘；不能删除或遗漏内圈球")
    if len(package["escape_layers"]) > len(rules_for(board).copper_layers) or not set(package["escape_layers"]).issubset(rules_for(board).copper_layers):
        raise PipelineError("escape_layers 含未存在于本板的铜层")
    rows, cols, tasks = {}, {}, []
    tolerance = max(rules_for(board).curve_tolerance * 4, 1e-6)
    for r, row in enumerate(package["balls"]):
        for c, pid in enumerate(row):
            if pid is None:
                continue
            pad = pads[pid]
            if pad.get("hole") not in (None, 0) or not pad.get("net"):
                raise PipelineError("当前球阵列仅支持有明确网名的贴片焊盘")
            if (component["id"], pid) not in {(str(p["compId"]), str(p["padId"])) for p in board["nets"].get(pad["net"], [])}:
                raise PipelineError("球位网络与完整网表不一致")
            x, y = pad["localX"], pad["localY"]
            if (r in rows and abs(rows[r] - y) > tolerance) or (c in cols and abs(cols[c] - x) > tolerance):
                raise PipelineError("balls 行列不符合焊盘真实局部坐标；不可用虚构球阵列引出")
            rows[r], cols[c] = y, x
            ring = min(r, c, len(package["balls"]) - 1 - r, len(row) - 1 - c)
            tasks.append(dict(component=component, pad=pad, row=r, col=c, ring=ring, package=package))
    if len(rows) < 2 or len(cols) < 2:
        raise PipelineError("球阵列缺少两条实际行列，尚未支持退化阵列")
    def pitch(values):
        pairs = sorted(values.items())
        steps = [(b[1] - a[1]) / (b[0] - a[0]) for a, b in zip(pairs, pairs[1:])]
        if abs(steps[0]) <= tolerance or any(abs(value - steps[0]) > tolerance for value in steps):
            raise PipelineError("球阵列行列间距不均匀或重合；需要显式受支持的矩形阵列")
        return abs(steps[0])
    local = dict(component, x=0.0, y=0.0, rotation=0.0, layer=1)
    bounds = [_body(local, rules_for(board).curve_tolerance).outer.bounds]
    bounds.extend(_fast_pad_bounds(local, pad, rules_for(board).curve_tolerance).outer.bounds for pad in pads.values())
    dimensions = dict(xmin=min(b[0] for b in bounds), xmax=max(b[2] for b in bounds),
                      ymin=min(b[1] for b in bounds), ymax=max(b[3] for b in bounds),
                      pitch=min(pitch(cols), pitch(rows)))
    tasks.sort(key=lambda task: ((task["ring"] if package["order"] == "outer_first" else -task["ring"]), task["row"], task["col"]))
    for task in tasks:
        task["dimensions"] = dimensions
    return tasks


def _choices(board, task, options, check):
    """联合选择侧边、落孔和出口层；实际新铜占据通道供后续球位避让。"""
    from pcb_routing import _net_options, _octilinear_doglegs, _candidate_cost
    component, pad, package, d = task["component"], task["pad"], task["package"], task["dimensions"]
    net = pad["net"]
    settings = _net_options(board, net, options)
    stack = tuple(rules_for(board).copper_layers)
    face = pad.get("layer", component["layer"])
    if face not in settings["layers"] or face not in (1, 2):
        raise PipelineError("球位表面不在允许布线层，或不是受支持的单面贴片球")
    layers = [layer for layer in package["escape_layers"] if layer in settings["layers"]]
    if not layers:
        raise PipelineError("球位没有满足 CHANNEL_SPEC/trace_layers 的出口铜层")
    if any(layer != face for layer in layers) and (not settings["vias"] or not set(stack).issubset(settings["via_layers"])):
        raise PipelineError("球位换层需要允许完整层栈的通孔，不能隐式改成盲埋孔")
    # 同一圈优先轮换出口层；实际阻挡始终由已预留铜与精确检查决定。
    offset = task["ring"] % len(layers)
    layers = layers[offset:] + layers[:offset]
    x, y = pad["localX"], pad["localY"]
    sides = sorted(((x-d["xmin"], -1, 0), (d["xmax"]-x, 1, 0),
                    (y-d["ymin"], 0, -1), (d["ymax"]-y, 0, 1)))
    model, result = CollisionModel(board), []
    start = _world(component, (x, y))
    seen = set()
    for _, dx, dy in sides:
        for factor in package["via_offsets"]:
            for lateral in ((0, -0.5, 0.5) if factor else (0,)):
                check()
                landing_local = (x + d["pitch"] * (dx*factor-dy*lateral),
                                 y + d["pitch"] * (dy*factor+dx*lateral))
                landing = _world(component, landing_local)
                end_local = (d["xmin"]-package["escape_margin"] if dx < 0 else
                             d["xmax"]+package["escape_margin"] if dx > 0 else landing_local[0],
                             d["ymin"]-package["escape_margin"] if dy < 0 else
                             d["ymax"]+package["escape_margin"] if dy > 0 else landing_local[1])
                end = _world(component, end_local)
                for layer in layers:
                    check()
                    key = landing, end, layer
                    if key in seen:
                        continue
                    seen.add(key)
                    vias = ([] if layer == face else [dict(net=net, x=landing[0], y=landing[1],
                        hole=settings["hole"], diameter=settings["diameter"], layers=list(stack))])
                    if vias and not model.via_ok(net, *landing, settings["hole"], settings["diameter"], stack):
                        continue
                    for launch in _octilinear_doglegs(start, landing, options.get("diagonal", True), settings["width"]):
                        for tail in _octilinear_doglegs(landing, end, options.get("diagonal", True), settings["width"]):
                            check()
                            points = [(face, a, b) for a, b in zip(launch, launch[1:]) if a != b]
                            points.extend((layer, a, b) for a, b in zip(tail, tail[1:]) if a != b)
                            traces = [dict(net=net, layer=l, sx=a[0], sy=a[1], ex=b[0], ey=b[1], width=settings["width"])
                                      for l, a, b in points]
                            if not traces:
                                continue
                            # 初筛不能代替孔插入后的检查，提交时会重新检查全部线铜。
                            if not all(model.trace_ok(net, t["layer"], t["sx"], t["sy"], t["ex"], t["ey"], t["width"]) for t in traces):
                                continue
                            evidence = dict(component=component["id"], pad=pad["padId"], net=net,
                                ring=task["ring"], exit_layer=layer, exit_point=list(end), via_count=len(vias),
                                via_in_pad=bool(vias and model.via_in_pad(*landing, settings["hole"], stack)))
                            result.append((_candidate_cost(model, (traces, vias), settings), traces, vias, evidence))
    # 同代价保持生成时的按圈层轮换顺序，不能再用数值层号覆盖该优先级。
    return sorted(result, key=lambda value: value[0])


def _commit_escape(board, traces, vias, evidence, budget):
    """精检扇出枝并证明出口接回真实球位，保留原有每个端子连通组。

    输出候选板或 None，不修改输入。先精检出口铜岛，再由原 _commit
    接上起始线；已直接接触时不伪造铜组数，而用独立连通与整板几何复核。
    预算异常原样传播，任何原连接分裂或出口浮空均拒绝。
    """
    from pcb_routing import _commit, _groups, _trace_inside_new_drill
    net = evidence["net"]
    original_groups = _groups(board, net, budget)
    staged = deepcopy(board)
    for via in vias:
        budget.check()
        if not CollisionModel(staged).via_ok(net, via["x"], via["y"], via["hole"], via["diameter"], via["layers"]):
            return None
        staged["routes"].setdefault("vias", []).append(deepcopy(via))
    usable = [t for t in traces if not _trace_inside_new_drill(t, vias)]
    if not usable:
        return None
    model = CollisionModel(staged)
    for trace in usable:
        budget.check()
        if not model.trace_ok(net, trace["layer"], trace["sx"], trace["sy"], trace["ex"], trace["ey"], trace["width"]):
            return None
    staged["routes"].setdefault("traces", []).extend(deepcopy(usable[1:]))
    count = len(_groups(staged, net, budget))
    candidate = _commit(staged, net, usable[:1], [], count, budget)
    if candidate is None:
        # 盘内孔可能已接回原球位，此时添加合法枝不会减少铜组数。
        candidate = deepcopy(staged)
        candidate["routes"].setdefault("traces", []).append(deepcopy(usable[0]))
    budget.check()
    if check_board(candidate, ignore_connectivity=True):
        return None
    groups = _groups(candidate, net, budget)
    if any(not any(old["terminals"] <= group["terminals"] for group in groups) for old in original_groups):
        return None
    key = evidence["component"], evidence["pad"]
    point = Point(evidence["exit_point"])
    if not any(key in group["terminals"] and evidence["exit_layer"] in group["layers"]
               and group["layers"][evidence["exit_layer"]].covers(point) for group in groups):
        return None
    if any(not group["terminals"] for group in groups):
        return None
    return candidate


def fanout_board(board, options, stats, budget, excluded_nets=()):
    """输入完整板、路由选项、统计与预算，返回整批球阵列扇出的完整候选副本。

    balls 必须完整覆盖器件；地网按 plane_then_drc 暂缓并逐球报告，其他排除
    网络同样明确列出。采用外/内圈次序、出口层轮换、落孔和实际通道铜预留，
    有界回溯处理后续球受阻；不会以单个焊盘的成功冒充整封装完成。
    未找到整批结果或预算耗尽抛 RoutingFailure，非法配置/不支持工艺抛
    PipelineError；失败不返回半成品且不写EDA。成功仍非整网/整板验收。
    """
    from pcb_routing import RoutingFailure
    cfg = validate_options(options.get("fanout"))
    if not cfg["enabled"]:
        return deepcopy(board)
    original = normalize_board(board)
    record = dict(method="explicit_array_bounded_fanout", technology="through",
                  candidate_checks=0, backtracks=0, escaped=[], deferred=[],
                  packages=[], completed=False, full_board_accepted=False, budget_exhausted=False)
    stats["fanout"] = record
    started = time.monotonic()
    def check(candidate=False):
        budget.check()
        exhausted = (time.monotonic()-started >= cfg["max_seconds"] or
                     (candidate and record["candidate_checks"] >= cfg["max_candidates"]))
        if exhausted:
            record["budget_exhausted"] = True
            raise RoutingFailure("FANOUT_BUDGET", "整封装扇出预算内未找到完整候选，未证明无解", fanout=record)
        if candidate:
            record["candidate_checks"] += 1
    if isinstance(excluded_nets, (str, bytes)) or not isinstance(excluded_nets, (list, tuple, set, frozenset)):
        raise PipelineError("fanout.excluded_nets 必须为显式网络集合")
    if any(not isinstance(net, str) or not net for net in excluded_nets):
        raise PipelineError("fanout.excluded_nets 必须只含非空网名")
    excluded = set(excluded_nets)
    if options.get("ground_strategy") == "plane_then_drc":
        grounds = options.get("ground_nets")
        if (not isinstance(grounds, list) or not grounds
                or any(not isinstance(net, str) or not net for net in grounds)
                or len(set(grounds)) != len(grounds) or not set(grounds).issubset(original["nets"])):
            raise PipelineError("plane_then_drc 需要完整显式地网列表")
        excluded.update(grounds)
    if not excluded.issubset(original["nets"]):
        raise PipelineError("fanout.excluded_nets 包含未知网络")
    protected = set()
    for rule in original.get("metadata", {}).get("pipeline_constraints", []):
        if rule.get("diff_pair"):
            protected.update((rule["diff_pair"]["p"], rule["diff_pair"]["n"]))
        if rule.get("length_group"):
            protected.add(rule["net"])
    tasks = []
    try:
        if check_board(original, ignore_connectivity=True):
            raise PipelineError("fanout 输入存在硬几何错误，不能用扇出掩盖")
        for package in cfg["packages"]:
            check()
            balls = _array(original, package)
            record["packages"].append(dict(component=package["component"], declared_balls=len(balls),
                                           order=package["order"], escape_layers=package["escape_layers"]))
            for task in balls:
                net = task["pad"]["net"]
                if net in excluded:
                    record["deferred"].append(dict(component=package["component"], pad=task["pad"]["padId"], net=net,
                                                   reason="显式排除或原生铺铜后局部补地"))
                elif net in protected:
                    raise PipelineError("差分/等长网络需要专用成对扇出；当前通用球阵列不能单独布该网络")
                else:
                    tasks.append(task)
        # 迭代深度优先回溯，不以Python递归深度限制大封装。
        states, choices, indices, evidence, index = [original], [], [], [], 0
        while index < len(tasks):
            check()
            if len(choices) <= index:
                budget.enter(tasks[index]["pad"]["net"])
                try:
                    choices.append(_choices(states[index], tasks[index], options, lambda: check(candidate=True)))
                    check()
                finally:
                    budget.leave()
                indices.append(0)
            if indices[index] >= len(choices[index]):
                if index == 0:
                    raise RoutingFailure("FANOUT_NOT_FOUND", "有限落孔/出口层/圈次序中未找到整封装扇出，未证明无解", fanout=record)
                if record["backtracks"] >= cfg["max_backtracks"]:
                    record["budget_exhausted"] = True
                    raise RoutingFailure("FANOUT_BUDGET", "扇出回溯预算内未找到完整候选，未证明无解", fanout=record)
                record["backtracks"] += 1
                choices.pop(); indices.pop(); states.pop(); evidence.pop()
                index -= 1
                continue
            _, traces, vias, entry = choices[index][indices[index]]
            indices[index] += 1
            check(candidate=True)
            budget.enter(entry["net"])
            try:
                candidate = _commit_escape(states[index], traces, vias, entry, budget)
                # 单次几何调用无法中途抢占；返回后仍须检查局部、总量和单网时限。
                check()
            finally:
                budget.leave()
            if candidate is None:
                continue
            states.append(candidate)
            evidence.append(entry)
            index += 1
        result = states[-1]
        record["escaped"] = evidence
        record["scope"] = "全部声明且未暂缓球位的实体出口，不代表网络终点已接通"
        record["elapsed_seconds"] = time.monotonic()-started
        check()
        result.setdefault("metadata", {})["fanout_candidate"] = deepcopy(record)
        # 报告复制也计入预算。只标记扇出阶段，整板状态由原pipeline验收决定。
        check()
        record["completed"] = True
        result["metadata"]["fanout_candidate"]["completed"] = True
        return result
    except RoutingFailure as error:
        if "BUDGET" in error.diagnostics.get("code", ""):
            record["budget_exhausted"] = True
            record["stopped_by"] = error.diagnostics["code"]
        raise
    finally:
        record["elapsed_seconds"] = time.monotonic()-started
