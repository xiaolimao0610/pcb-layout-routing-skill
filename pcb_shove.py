"""有界、事务式的单层几何级联推挤；仅产生候选，不写编辑器。

原铜只移动明确授权的直线；本轮生成铜须有原始清单证明其身份。
保留两端、层、网络和线宽。过孔、派生铜及有中途接点的线保持原状。
所有成功候选仍须经过独立整板和原生验收。
"""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
import math
import time

from shapely.affinity import affine_transform
from shapely.geometry import Point
from shapely.ops import nearest_points, unary_union

from pcb_geometry import trace_bounds
from pcb_pipeline_common import (CollisionModel, PipelineError, check_board,
                                 copper_and_drills, finite, fingerprint, rules_for, strict_options)


OPTION_KEYS = {"enabled", "movable_trace_ids", "protected_trace_ids", "max_depth",
               "max_objects", "max_candidates", "max_seconds", "max_displacement",
               "move_generated_traces"}


def validate_options(options=None, board=None):
    """输入推挤配置及可选完整板，返回校验后的新配置。

    未知字段、非有限预算、重复或不存在的对象 ID 抛 PipelineError。
    enabled 默认 false；move_generated_traces 默认 true，但仅在总开关
    启用且调用方提供本轮原线清单时生效。白名单不覆盖其他保护。
    """
    out = strict_options(options, OPTION_KEYS, "routing.shove")
    out.setdefault("enabled", False)
    out.setdefault("move_generated_traces", True)
    for key in ("enabled", "move_generated_traces"):
        if type(out[key]) is not bool:
            raise PipelineError(f"routing.shove.{key} 必须为布尔值")
    for key in ("movable_trace_ids", "protected_trace_ids"):
        values = out.setdefault(key, [])
        if (not isinstance(values, list) or any(not isinstance(v, str) or not v for v in values)
                or len(set(values)) != len(values)):
            raise PipelineError(f"routing.shove.{key} 必须为唯一非空字符串列表")
        if board is not None:
            known = {t.get("id") for t in board.get("routes", {}).get("traces", [])}
            if set(values) - known:
                raise PipelineError(f"routing.shove.{key} 引用未知走线")
    for key, default, maximum in (("max_depth", 4, 32), ("max_objects", 8, 128),
                                  ("max_candidates", 256, 100000)):
        value = out.setdefault(key, default)
        if type(value) is not int or not 1 <= value <= maximum:
            raise PipelineError(f"routing.shove.{key} 必须为 1..{maximum} 的整数")
    for key, default in (("max_seconds", 10.0), ("max_displacement", 100.0)):
        out[key] = finite(out.get(key, default), f"routing.shove.{key}", positive=True)
    return out


class _Stop(Exception):
    """本模块预算耗尽；不表示几何无解。"""


class _Budget:
    def __init__(self, parent, config, record):
        self.parent, self.config, self.record = parent, config, record
        self.started = time.monotonic()
        self.context = {}

    def check(self, candidate=False):
        if self.parent is not None:
            self.parent.check()
        if time.monotonic() - self.started >= self.config["max_seconds"]:
            raise _Stop("max_seconds")
        if candidate:
            if self.record["candidates"] >= self.config["max_candidates"]:
                raise _Stop("max_candidates")
            self.record["candidates"] += 1


def _protected_nets(board, options):
    protected = (set(options.get("ground_nets", [])) | set(options.get("topology_orders", {}))
                 | set(options.get("_shove_protected_nets", [])))
    for net, tag in board.get("tags", {}).get("net_tags", {}).items():
        meta = tag.get("metadata", {})
        if (tag.get("type") == "GND" or tag.get("need_shield_check")
                or any(meta.get(key) for key in ("fixed", "protected", "diff_pair", "length_tuned"))):
            protected.add(net)
    for rule in board.get("metadata", {}).get("pipeline_constraints", []):
        if rule.get("operator") == "TOPOLOGY_ORDER":
            protected.add(rule["net"])
        elif rule.get("operator") == "COMPANION_GEN":
            protected.update(rule["nets"])
            protected.add(rule["ground_net"])
        elif rule.get("operator") == "CHANNEL_SPEC":
            if "diff_pair" in rule:
                protected.update((rule["diff_pair"]["p"], rule["diff_pair"]["n"]))
            if "length_group" in rule:
                protected.add(rule["net"])
    return protected


def _shape(trace, rules):
    return trace_bounds(trace, rules.curve_tolerance).outer


def _eligible(board, trace, index, copper, drills, protected, config, rules, budget):
    """检查一条原线是否可移动，返回拒绝原因或 None；不删除任何障碍。"""
    if trace.get("locked") or trace["id"] in config["protected_trace_ids"]:
        return "locked_or_protected"
    if trace["net"] in protected:
        return "protected_net"
    layers = trace.get("layers", [trace.get("layer")])
    if len(layers) != 1 or layers[0] not in rules.copper_layers:
        return "unsupported_layer"
    for pour in board["routes"].get("copper_pours", []):
        budget.check()
        if trace["id"] in pour.get("metadata", {}).get("refs", []):
            return "derived_copper_parent"
    start, end = Point(trace["sx"], trace["sy"]), Point(trace["ex"], trace["ey"])
    if start.equals(end):
        return "zero_length"
    original = trace_bounds(trace, rules.curve_tolerance).inner
    for item in copper:
        budget.check()
        if item["name"] == f"trace {index}" or item["net"] != trace["net"] or layers[0] not in item["layers"]:
            continue
        contact = original.intersection(item["inner"])
        if contact.is_empty:
            continue
        # 只接受在原端点接入的外部铜；中途焊盘、分支和同网搭接保护。
        if not (item["inner"].covers(start) or item["inner"].covers(end)):
            return "interior_copper_contact"
        if item["name"].startswith("pour "):
            return "attached_pour"
    for hole in drills:
        budget.check()
        if layers[0] not in hole["layers"] or not original.intersects(hole["outer"]):
            continue
        if not (hole["outer"].covers(start) or hole["outer"].covers(end)):
            return "interior_drill"
    return None


def _segments(net, layer, points, width):
    from pcb_routing import _trace
    return [_trace(net, layer, a, b, width) for a, b in zip(points, points[1:]) if a != b]


def _shift_paths(trace, obstacles, model, config, budget):
    """输入原线和必须避开的铜，产生保留端点的几何偏移候选。

    距离由障碍外界、实际线宽和间距计算，不依赖路由栅格；只枚举
    有限的两侧偏移及 45°／直角返回。超预算由 _Budget 抛出 _Stop。
    """
    start = (trace["sx"], trace["sy"])
    dx, dy = trace["ex"] - start[0], trace["ey"] - start[1]
    length = math.hypot(dx, dy)
    ux, uy, vx, vy = dx / length, dy / length, -dy / length, dx / length
    # 任意原角度保持；局部 45°/90° 返回相对原轴定义，不冒充全板 45° 规整。
    transform = [ux, uy, vx, vy, -ux * start[0] - uy * start[1], -vx * start[0] - vy * start[1]]
    epsilon = max(model.rules.curve_tolerance * 8, model.rules.contact_tolerance * 8)
    blocked = []
    for other in obstacles:
        budget.check()
        if other["net"] == trace["net"] or other["layer"] != trace["layer"]:
            continue
        gap = model.rules.get_clearance(model.net_type(trace["net"]), model.net_type(other["net"]))
        original_shape, other_shape = _shape(trace, model.rules), _shape(other, model.rules)
        if not original_shape.intersects(other_shape) and original_shape.distance(other_shape) >= gap:
            continue
        # 方形外包络只生成更保守的候选；最终仍用原精确检查。
        blocked.append(affine_transform(other_shape, transform).buffer(
            trace["width"] / 2 + gap + epsilon, join_style=2))
    if not blocked:
        return
    low, bottom, high, top = unary_union(blocked).bounds
    low, high = low - epsilon, high + epsilon
    if low <= 0 or high >= length:
        return
    offsets = sorted((bottom - epsilon, top + epsilon), key=abs)

    def world(x, y):
        return (start[0] + ux * x + vx * y, start[1] + uy * x + vy * y)

    for multiplier in (1.0, 1.5, 2.0):
        for initial in offsets:
            offset = initial * multiplier
            if abs(offset) > config["max_displacement"] or offset == 0:
                continue
            for ramp in (abs(offset), 0.0):
                budget.check()
                if low - ramp <= 0 or high + ramp >= length:
                    continue
                points = [start, world(low - ramp, 0), world(low, offset),
                          world(high, offset), world(high + ramp, 0), (trace["ex"], trace["ey"])]
                yield _segments(trace["net"], trace["layer"], points, trace["width"])


def _conflicts(first, second, model, budget):
    for a in first:
        for b in second:
            budget.check()
            if a["net"] == b["net"] or a["layer"] != b["layer"]:
                continue
            gap = model.rules.get_clearance(model.net_type(a["net"]), model.net_type(b["net"]))
            left, right = _shape(a, model.rules), _shape(b, model.rules)
            if left.intersects(right) or left.distance(right) < gap:
                return True
    return False


def _materialize(board, moved):
    result = deepcopy(board)
    result["routes"]["traces"] = [deepcopy(t) for t in board["routes"]["traces"] if t.get("id") not in moved]
    for key in sorted(moved):
        result["routes"]["traces"].extend(deepcopy(moved[key]))
    return result


def _candidate_identity_view(board, baseline, budget):
    """输入当前候选和本轮原线清单，返回临时身份副本及新线身份映射。

    原有无 ID 铜按完整记录的多重集合保留；缺失或变化抛 PipelineError。
    只有原输入之外的无 ID 新线获得临时身份；该身份不得进入返回板和原生计划。
    baseline 为 None 时没有可证明的新旧边界，返回原板和空映射。
    """
    if baseline is None:
        return board, {}
    if not isinstance(baseline, list) or any(not isinstance(trace, dict) for trace in baseline):
        raise PipelineError("推挤缺少完整的本轮原线清单")
    anonymous = Counter()
    for trace in baseline:
        budget.check()
        if trace.get("id") is None:
            anonymous[fingerprint(trace)] += 1
    used = {trace.get("id") for trace in board["routes"].get("traces", [])}
    prefix = "@shove-generated:"
    while any(isinstance(identity, str) and identity.startswith(prefix) for identity in used):
        prefix = "@" + prefix
    view, generated = deepcopy(board), {}
    for index, trace in enumerate(view["routes"]["traces"]):
        budget.check()
        if trace.get("id") is not None:
            continue
        signature = fingerprint(trace)
        if anonymous[signature]:
            anonymous[signature] -= 1
            continue
        identity = prefix + str(index)
        generated[identity] = {"candidate_trace_index": index, "expected": deepcopy(trace)}
        trace["id"] = identity
    if any(count for count in anonymous.values()):
        raise PipelineError("推挤原有无 ID 铜丢失或改变；不得当作本轮新线移动")
    return view, generated


def shove_route(board, net, settings, options, stats, budget):
    """生成减少目标网铜组的推挤候选，不修改 board/options/settings。

    入参：完整规范板、目标网络、_net_options 设置、含 shove 的路由
    配置、可更新统计字典和父级合作预算。出参：完整候选板或 None。
    新线仅接通同层铜组；原线只按显式 ID 白名单级联偏移，孔不移动。
    有 _shove_original_traces 原始清单时，可按 move_generated_traces 移动
    本轮无 ID 新线；原无 ID 铜始终保护，所有临时身份在返回前移除。
    非法配置／原板硬错误抛 PipelineError；父预算异常原样传播。
    局部预算耗尽返回 None 并记录“未找到”；异常和失败均不改输入。
    """
    from pcb_routing import _groups, _commit, _octilinear_doglegs, RoutingFailure

    config = validate_options(options.get("shove"))
    if not config["enabled"]:
        return None
    report = stats.setdefault("shove", {"attempts": [], "replacements": []})
    record = {"net": net, "status": "not_found", "candidates": 0, "depth_peak": 0,
              "rejected_objects": {}, "limits_hit": [], "full_board_accepted": False}
    report.setdefault("attempts", []).append(record)
    report.setdefault("replacements", [])
    local = _Budget(budget, config, record)
    try:
        local.check()
        if net not in board.get("nets", {}):
            raise PipelineError("推挤目标网络不存在")
        errors = check_board(board, ignore_connectivity=True, defer_companions=True)
        local.check()
        if errors:
            raise PipelineError("推挤原板硬检查未通过：" + "; ".join(errors[:5]))
        originals = {}
        for index, trace in enumerate(board["routes"].get("traces", [])):
            identity = trace.get("id")
            if identity is None:
                continue
            if identity in originals:
                raise PipelineError("推挤原线 ID 重复")
            originals[identity] = (index, trace)
        consumed = {item["trace_id"] for item in report["replacements"]
                    if item.get("scope", "native_source") == "native_source"}
        unknown = (set(config["movable_trace_ids"]) | set(config["protected_trace_ids"])) - set(originals) - consumed
        if unknown:
            raise PipelineError("推挤白名单引用未知原线：" + str(sorted(unknown)))
        groups = _groups(board, net, local)
        if len(groups) < 2:
            record["status"] = "already_connected"
            return None
        protected = _protected_nets(board, options)
        if net in protected:
            record["status"] = "protected_target"
            return None
        generated = {}
        if config["move_generated_traces"]:
            board, generated = _candidate_identity_view(board, options.get("_shove_original_traces"), local)
            config["movable_trace_ids"].extend(generated)
            for index, trace in enumerate(board["routes"]["traces"]):
                if trace.get("id") in generated:
                    originals[trace["id"]] = (index, trace)
        record["generated_trace_candidates"] = len(generated)
        copper, drills = copper_and_drills(board)
        rules = rules_for(board)
        movable = {}
        for identity in config["movable_trace_ids"]:
            local.check()
            if identity not in originals:
                continue
            index, trace = originals[identity]
            if trace["net"] == net:
                continue
            reason = _eligible(board, trace, index, copper, drills, protected, config, rules, local)
            if reason:
                label = (f"candidate_generated:{generated[identity]['candidate_trace_index']}"
                         if identity in generated else identity)
                record["rejected_objects"][label] = reason
            else:
                movable[identity] = dict(trace, layer=trace.get("layer", trace.get("layers", [None])[0]))
        if not movable:
            record["status"] = "no_movable_obstacle"
            return None
        # 固定模型中仅暂时移出已证明可动的线；最终完整板必须恢复或替换每条。
        model_board = _materialize(board, {key: [] for key in movable})
        model = CollisionModel(model_board)
        before = {name: _groups(board, name, local) for name in {t["net"] for t in movable.values()} | {net}}

        def clear(path, state, ancestors, reserved):
            """在候选副本上递归推开异网原线，失败返回 None，保留父路径和原端点。"""
            local.check()
            if any(not model.trace_ok(t["net"], t["layer"], t["sx"], t["sy"], t["ex"], t["ey"], t["width"]) for t in path):
                return None
            if any(_conflicts(path, fixed, model, local) for fixed in reserved):
                return None
            blocker = next((key for key in sorted(movable)
                            if key not in ancestors and _conflicts(path, state.get(key, [movable[key]]), model, local)), None)
            if blocker is None:
                return state
            if len(ancestors) >= config["max_depth"]:
                record["limits_hit"].append("max_depth")
                return None
            if blocker not in state and len(state) >= config["max_objects"]:
                record["limits_hit"].append("max_objects")
                return None
            record["depth_peak"] = max(record["depth_peak"], len(ancestors) + 1)
            for replacement in _shift_paths(movable[blocker], path, model, config, local):
                local.check(candidate=True)
                proposed = dict(state, **{blocker: replacement})
                cleared = clear(replacement, proposed, ancestors + (blocker,), reserved + [path])
                if cleared is None:
                    continue
                completed = clear(path, cleared, ancestors, reserved)
                if completed is not None:
                    return completed
            return None

        # 使用实际铜组的最近接入点；各对、层和局部形状都受总候选预算限制。
        for source_index, source in enumerate(groups):
            for target in groups[source_index + 1:]:
                for layer in settings["layers"]:
                    local.check()
                    if layer not in source["layers"] or layer not in target["layers"]:
                        continue
                    a, b = nearest_points(source["layers"][layer], target["layers"][layer])
                    for points in _octilinear_doglegs((a.x, a.y), (b.x, b.y), options.get("diagonal", True), settings["width"]):
                        local.check(candidate=True)
                        route = _segments(net, layer, points, settings["width"])
                        moved = clear(route, {}, (), []) if route else None
                        if not moved:
                            continue
                        changed = _materialize(board, moved)
                        candidate = _commit(changed, net, route, [], len(groups), budget or local)
                        if candidate is None:
                            continue
                        local.check()
                        errors = check_board(candidate, ignore_connectivity=True, defer_companions=True)
                        if errors:
                            record["last_rejection"] = errors[:5]
                            continue
                        affected = {movable[key]["net"] for key in moved} | {net}
                        after = {name: _groups(candidate, name, local) for name in affected}
                        if any(len(after[name]) > len(before[name]) or any(
                                not any(old["terminals"] <= new["terminals"] for new in after[name])
                                for old in before[name]) for name in affected):
                            record["last_rejection"] = ["原实体铜组或端子连通关系改变"]
                            continue
                        local.check()
                        replacements = []
                        for key in sorted(moved):
                            item = dict(scope="native_source", trace_id=key,
                                        expected=deepcopy(originals[key][1]), replacements=deepcopy(moved[key]))
                            if key in generated:
                                item.update(scope="candidate_generated", trace_id=None,
                                            candidate_trace_index=generated[key]["candidate_trace_index"],
                                            expected=deepcopy(generated[key]["expected"]))
                            replacements.append(item)
                        for trace in candidate["routes"]["traces"]:
                            if trace.get("id") in generated:
                                trace.pop("id")
                        record.update(status="candidate", affected_nets=sorted(affected),
                                      moved_objects=len(moved), replacements=deepcopy(replacements),
                                      moved_generated=sum(key in generated for key in moved),
                                      added_traces=deepcopy(route), remaining_groups=len(after[net]))
                        local.check()
                        report["replacements"].extend(replacements)
                        return candidate
        return None
    except _Stop as exc:
        record.update(status="budget_exhausted", stop_reason=str(exc), message="预算耗尽，本次未找到；不表示无解")
        return None
    except RoutingFailure as exc:
        record.update(status="parent_budget_exhausted", stop_reason=exc.diagnostics.get("code"),
                      message="父级搜索预算终止；不表示无解")
        raise
    finally:
        record["elapsed_seconds"] = time.monotonic() - local.started
        record["limits_hit"] = sorted(set(record["limits_hit"]))
