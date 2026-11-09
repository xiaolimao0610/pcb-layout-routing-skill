"""Bounded multilayer grid routing with physical copper connectivity.

Every accepted edge goes through the shared conservative collision checker.
Existing copper is immutable; retries discard only copper generated here.
"""

import copy
import heapq
import itertools
import math
import time
from collections import Counter
from fractions import Fraction
from numbers import Real

from shapely.geometry import LineString, Point
from shapely.ops import nearest_points, unary_union
from shapely.strtree import STRtree

from pcb_geometry import trace_bounds, via_bounds

from pcb_pipeline_common import (
    CollisionModel, PipelineError, check_board,
    copper_and_drills, normalize_board, rules_for, terminal_items,
)


_OPTIONS = {"enabled", "grid_step", "width", "via_diameter", "via_drill",
            "via_cost", "via_in_pad_penalty", "max_expansions", "max_passes", "net_order",
            "allow_vias", "diagonal", "topology_orders", "max_seconds",
            "max_net_seconds", "net_order_strategy", "ground_nets", "pours_before_net",
            "ground_strategy", "trace_layers", "method", "occupancy_grid",
            "occupancy_max_cells", "occupancy_max_seconds", "occupancy_retries",
            "negotiated_max_iterations", "negotiated_history_increment",
            "negotiated_present_factor", "negotiated_present_growth",
            "steiner_max_candidates", "steiner_max_rounds",
            "preferred_direction", "nonpreferred_penalty", "diff_max_candidates",
            "length_max_candidates", "length_max_amplitude", "geometry_search",
            "fanout", "shove", "diff_layer_change", "negotiated_grid_cache",
            "negotiated_bend_penalty", "checkpoint", "local_repair", "escape"}


class RoutingFailure(PipelineError):
    """A bounded search failure is evidence about this search, not fabrication."""
    def __init__(self, code, message, **diagnostics):
        self.diagnostics = dict(code=code, message=message, **diagnostics)
        super().__init__(f"{code}: {message}")


class _Budget:
    def __init__(self, options):
        self.started = time.monotonic()
        self.total_limit = options["max_seconds"]
        self.net_limit = options["max_net_seconds"]
        self.spent, self.net, self.net_started = {}, None, None
        self.context = {}

    def enter(self, net):
        self.leave()
        self.net, self.net_started = net, time.monotonic()
        self.context = {"net": net, "phase": "net_setup"}
        self.check()

    def leave(self):
        if self.net is not None:
            self.spent[self.net] = self.net_elapsed()
        self.net, self.net_started = None, None
        self.context = {}

    def net_elapsed(self):
        return self.spent.get(self.net, 0) + (time.monotonic() - self.net_started
                                            if self.net_started is not None else 0)

    def check(self):
        elapsed = time.monotonic() - self.started
        net_elapsed = self.net_elapsed()
        scope = ("total" if elapsed >= self.total_limit else
                 "net" if self.net is not None and net_elapsed >= self.net_limit else None)
        if scope:
            raise RoutingFailure("TIME_BUDGET", f"routing {self.net or ''}: {scope} wall-clock budget exhausted",
                                 **self.context, elapsed_seconds=elapsed,
                                 net_elapsed_seconds=net_elapsed, budget_scope=scope,
                                 max_seconds=self.total_limit, max_net_seconds=self.net_limit,
                                 interpretation="Search stopped by elapsed time; geometry feasibility is undetermined.")

    def record(self, stats):
        stats["elapsed_seconds"] = time.monotonic() - self.started
        stats["net_elapsed_seconds"] = dict(self.spent)
        if self.net is not None:
            stats["net_elapsed_seconds"][self.net] = self.net_elapsed()


class _PairBudget:
    """Give other islands a turn without extending the enclosing net budget."""
    def __init__(self, parent, seconds):
        self.parent, self.deadline = parent, time.monotonic() + seconds

    @property
    def context(self):
        return self.parent.context

    @context.setter
    def context(self, value):
        self.parent.context = value

    def check(self):
        self.parent.check()
        if time.monotonic() >= self.deadline:
            raise RoutingFailure("PAIR_TIME_BUDGET", "Try another copper island before retrying this pair",
                                 **self.context, budget_scope="pair")


def _context(net, source, target, settings, step):
    return dict(net=net, source_terminals=sorted(source["terminals"]),
                target_terminals=sorted(target["terminals"]),
                source_layers=sorted(source["layers"]), target_layers=sorted(target["layers"]),
                allowed_layers=list(settings["layers"]), allow_vias=settings["vias"],
                via_allowed_layers=list(settings.get("via_layers", settings["layers"])),
                grid_step=step, width=settings["width"], via_drill=settings["hole"],
                via_diameter=settings["diameter"])


def _number(value, name, minimum=0, positive=False):
    try:
        valid = (not isinstance(value, bool) and isinstance(value, Real)
                 and math.isfinite(value) and value >= minimum
                 and not (positive and value == 0))
    except (OverflowError, TypeError, ValueError):
        valid = False
    if not valid:
        raise PipelineError(f"routing {name} must be a finite {'positive' if positive else 'nonnegative'} number")
    return float(value)


def _pieces(geometry):
    if geometry.geom_type == "Polygon":
        if not geometry.is_empty and geometry.area > 0:
            yield geometry
    elif hasattr(geometry, "geoms"):
        for part in geometry.geoms:
            yield from _pieces(part)


def _groups(board, net, budget=None, *, geometry_context=None):
    """输入完整板和网络，返回真实铜岛/孔壁连通组；非法几何抛 PipelineError。

    可选 geometry_context 仅传递经过验证的制造几何上下文；默认检查不变。
    不按网名直接合并铜岛，也不因提供上下文跳过端子身份检查。
    """
    if geometry_context is None:
        copper, drills = copper_and_drills(board, net_filter=net)
    else:
        copper, drills = copper_and_drills(board, net_filter=net, geometry_context=geometry_context)
    nodes = []
    for item in copper:
        if item["net"] == net:
            for layer in item["layers"]:
                for geometry in _pieces(item["inner"]):
                    nodes.append((item, layer, geometry))
    parent = list(range(len(nodes)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    holes = {d["name"]: d["outer"] for d in drills if d["plated"]}
    tolerance = rules_for(board).contact_tolerance
    layer_ids, barrel_ids = {}, {}
    for i, (item, layer, geometry) in enumerate(nodes):
        if budget is not None:
            budget.check()
        layer_ids.setdefault(layer, []).append(i)
        if item.get("barrel"):
            barrel_ids.setdefault(item["barrel"], []).append(i)
    trees = {}
    for layer, ids in layer_ids.items():
        if budget is not None:
            budget.check()
        trees[layer] = STRtree([nodes[i][2] for i in ids])
    for i, (item, layer, geometry) in enumerate(nodes):
        if budget is not None:
            budget.check()
        candidates = {layer_ids[layer][int(j)] for j in trees[layer].query(geometry)
                      if layer_ids[layer][int(j)] < i}
        if item.get("barrel"):
            candidates.update(j for j in barrel_ids[item["barrel"]]
                              if j < i and nodes[j][1] != layer)
        # Preserve the old pair order and exact positive-contact/barrel tests.
        for j in sorted(candidates):
            if budget is not None:
                budget.check()
            other, other_layer, other_geometry = nodes[j]
            touch = False
            if layer == other_layer:
                intersection = geometry.intersection(other_geometry)
                touch = (intersection.area > tolerance ** 2
                         or intersection.length > tolerance)
            elif item.get("barrel") and item.get("barrel") == other.get("barrel"):
                hole = holes.get(item["barrel"])
                touch = (hole is not None and geometry.distance(hole) <= tolerance
                         and other_geometry.distance(hole) <= tolerance)
            if touch:
                parent[find(i)] = find(j)
    groups = {}
    for i, (item, layer, geometry) in enumerate(nodes):
        group = groups.setdefault(find(i), {"layers": {}, "terminals": set(), "trace_lines": {}})
        group["layers"].setdefault(layer, []).append(geometry)
        if item.get("terminal") is not None:
            group["terminals"].add(tuple(item["terminal"]))
    net_traces = [t for t in board.get("routes", {}).get("traces", ())
                  if t.get("net") == net and (t["sx"], t["sy"]) != (t["ex"], t["ey"])]
    for group in groups.values():
        if budget is not None:
            budget.check()
        group["layers"] = {layer: unary_union(parts)
                           for layer, parts in group["layers"].items()}
        for t in net_traces:
            layer = t.get("layer")
            if layer in group["layers"]:
                line = LineString([(t["sx"], t["sy"]), (t["ex"], t["ey"])])
                if group["layers"][layer].intersects(line):
                    group["trace_lines"].setdefault(layer, []).append((line, float(t.get("width", 6.0))))
    return list(groups.values())


def _net_options(board, net, options):
    rules = rules_for(board)
    tag = board.get("tags", {}).get("net_tags", {}).get(net, {})
    from pcb_width_policy import selected_width
    width = selected_width(board, net, _number(options["width"], "width", positive=True)
                           if "width" in options else None)
    drill = _number(options.get("via_drill", tag.get("via_drill", rules.default_via_drill)),
                    "via_drill", positive=True)
    diameter = _number(options.get("via_diameter", tag.get("via_diameter", rules.default_via_diameter)),
                       "via_diameter", positive=True)
    if drill < rules.minimum_drill or diameter <= drill or (diameter - drill) / 2 < rules.min_annular_ring:
        raise PipelineError(f"routing {net}: via drill/diameter violate minimum drill or annular ring")
    alpha = _number(tag.get("alpha_length", 1), f"{net}.alpha_length", positive=True)
    via_cost = _number(options.get("via_cost", tag.get("via_cost", rules.default_via_cost)), "via_cost")
    allowed = tuple(layer for layer in rules.copper_layers
                    if layer in tag.get("allowed_layers", rules.copper_layers))
    trace_layers = tuple(layer for layer in allowed
                         if layer in options.get("trace_layers", rules.copper_layers))
    return {"width": width, "hole": drill, "diameter": diameter, "alpha": alpha,
            "via_cost": via_cost, "via_in_pad_penalty": _number(options.get("via_in_pad_penalty", 0), "via_in_pad_penalty"),
            "layers": trace_layers, "via_layers": allowed,
            "vias": options.get("allow_vias", True) and tag.get("allow_via", True)}


def _via_preference_cost(model, x, y, settings, layers):
    """输入过孔位置、网络设置及层栈，返回盘中孔的有限附加代价。

    零权重直接返回零并保留旧搜索行为；正权重按每个孔计一次，不按
    接触焊盘数重复累计。几何输入异常由模型抛出，不替代任何硬检查。
    """
    penalty = settings.get("via_in_pad_penalty", 0)
    return penalty if penalty and model.via_in_pad(x, y, settings["hole"], layers) else 0.0


def _candidate_cost(model, candidate, settings):
    """输入已构造候选和设置，返回线长、过孔及软偏好总代价；几何异常原样传播。"""
    traces, vias = candidate
    return (sum(math.hypot(t["ex"] - t["sx"], t["ey"] - t["sy"]) for t in traces) * settings["alpha"]
            + sum(settings["via_cost"] + _via_preference_cost(model, v["x"], v["y"], settings, v["layers"])
                  for v in vias))


def _soft_order(items, key, limit):
    """对有限候选重排，返回偏好前 limit 项并保留旧前 limit 项作为后备。

    入参 items 已按旧算法排列，key 是软代价函数；最多返回两倍 limit 项。
    不删除原有可用候选，代价计算异常原样传播。
    """
    preferred = sorted(items, key=key)[:limit]
    return preferred + [item for item in items[:limit] if item not in preferred]


def via_in_pad_report(board, vias, penalty):
    """输入完整候选板、新增孔列表及罚分，返回仅提示的盘中孔统计。

    不删除孔、不生成 DRC 错误；每个新孔计一次。板几何或数值非法时沿用
    PipelineError/几何异常，不以空报告掩盖失败。已有孔不计入新增统计。
    """
    penalty = _number(penalty, "via_in_pad_penalty")
    found = []
    if vias:
        model = CollisionModel(board)
        for via in vias:
            layers = via.get("layers", model.rules.copper_layers)
            if model.via_in_pad(via["x"], via["y"], via["hole"], layers):
                found.append({k: via[k] for k in ("net", "x", "y", "hole")})
    return dict(policy="soft_preference", penalty_per_via=penalty,
                added_vias_in_pad=len(found), penalty_cost=len(found) * penalty,
                retained_vias=found, affects_acceptance=False)


def _trace_layers(board, options):
    stack = tuple(rules_for(board).copper_layers)
    layers = options.get("trace_layers", list(stack))
    if (not isinstance(layers, list) or not layers
            or any(type(layer) is not int for layer in layers)
            or len(layers) != len(set(layers)) or not set(layers).issubset(stack)):
        raise PipelineError("routing trace_layers must be a nonempty unique list of physical copper layer IDs")
    return list(layers)


def _trace(net, layer, a, b, width):
    return {"net": net, "layer": layer, "sx": a[0], "sy": a[1],
            "ex": b[0], "ey": b[1], "width": width}


def _is_octilinear(a, b, tol=1e-5):
    """Return True if segment a->b is horizontal (0 deg), vertical (90 deg), or diagonal (45/135 deg)."""
    dx = abs(b[0] - a[0])
    dy = abs(b[1] - a[1])
    if dx <= tol or dy <= tol:
        return True
    return abs(dx - dy) <= tol * max(1.0, dx, dy)


def _octilinear_doglegs(a, b, diagonal=True, width=6.0, allow_short_direct=False):
    """Generate 0/90/45-degree octilinear dogleg candidate paths between a and b."""
    if a == b:
        return [[a, b]]
    dx = b[0] - a[0]
    dy = b[1] - a[1]
    adx, ady = abs(dx), abs(dy)
    if not diagonal:
        if adx <= 1e-6 or ady <= 1e-6:
            return [[a, b]]
        return [[a, (a[0], b[1]), b], [a, (b[0], a[1]), b]]
    if _is_octilinear(a, b):
        return [[a, b]]
    sx = 1.0 if dx >= 0 else -1.0
    sy = 1.0 if dy >= 0 else -1.0
    paths = []
    if adx > ady:
        p1 = (a[0] + sx * ady, b[1])
        p2 = (b[0] - sx * ady, a[1])
        half = (adx - ady) * 0.5
        m1 = (a[0] + sx * half, a[1])
        m2 = (b[0] - sx * half, b[1])
        paths.extend([[a, p1, b], [a, p2, b], [a, m1, m2, b]])
    else:
        p1 = (b[0], a[1] + sy * adx)
        p2 = (a[0], b[1] - sy * adx)
        half = (ady - adx) * 0.5
        m1 = (a[0], a[1] + sy * half)
        m2 = (b[0], b[1] - sy * half)
        paths.extend([[a, p1, b], [a, p2, b], [a, m1, m2, b]])
    if allow_short_direct and math.hypot(dx, dy) <= max(2.0 * width, 24.0):
        paths.append([a, b])
    return paths


def _postprocess_traces(model, net, traces, vias, diagonal):
    """Merge collinear/near-collinear traces, octilinearize escape stubs, and chamfer 90-degree corners."""
    if not traces:
        return traces
    via_sites = {(round(v["x"], 4), round(v["y"], 4)) for v in vias}

    def is_via_site(pt):
        return (round(pt[0], 4), round(pt[1], 4)) in via_sites or any(
            math.hypot(pt[0] - v["x"], pt[1] - v["y"]) <= max(v["diameter"] * 0.5, 1e-3)
            for v in vias
        )

    out = [dict(t) for t in traces if math.hypot(t["ex"] - t["sx"], t["ey"] - t["sy"]) > 1e-9]
    if len(out) <= 1:
        return out

    def merge_pass(seq):
        changed = True
        while changed:
            changed = False
            merged = []
            i = 0
            while i < len(seq):
                if i + 1 < len(seq):
                    t1, t2 = seq[i], seq[i + 1]
                    p_mid = (t1["ex"], t1["ey"])
                    if (t1["layer"] == t2["layer"]
                            and abs(t1["width"] - t2["width"]) < 1e-9
                            and math.hypot(p_mid[0] - t2["sx"], p_mid[1] - t2["sy"]) < 1e-6
                            and not is_via_site(p_mid)):
                        ax, ay = t1["ex"] - t1["sx"], t1["ey"] - t1["sy"]
                        bx, by = t2["ex"] - t2["sx"], t2["ey"] - t2["sy"]
                        la, lb = math.hypot(ax, ay), math.hypot(bx, by)
                        if la > 1e-9 and lb > 1e-9:
                            cross = abs(ax * by - ay * bx) / (la * lb)
                            dot = (ax * bx + ay * by) / (la * lb)
                            # 1. Exact collinear continuation
                            if cross < 1e-6 and dot > 0:
                                new_t = dict(t1, ex=t2["ex"], ey=t2["ey"])
                                if model.trace_ok(net, new_t["layer"], new_t["sx"], new_t["sy"],
                                                  new_t["ex"], new_t["ey"], new_t["width"]):
                                    merged.append(new_t)
                                    i += 2
                                    changed = True
                                    continue
                            p_start = (t1["sx"], t1["sy"])
                            p_end = (t2["ex"], t2["ey"])
                            # 2. Near-collinear (< ~5 deg) or micro-stub absorption that EasyEDA would auto-merge
                            if (dot > 0.996 or min(la, lb) < 1.2 * t1["width"]) and (
                                not _is_octilinear(p_start, p_mid)
                                or not _is_octilinear(p_mid, p_end)
                            ):
                                if _is_octilinear(p_start, p_end) or dot > 0.996:
                                    new_t = dict(t1, ex=t2["ex"], ey=t2["ey"])
                                    if model.trace_ok(net, new_t["layer"], new_t["sx"], new_t["sy"],
                                                      new_t["ex"], new_t["ey"], new_t["width"]):
                                        merged.append(new_t)
                                        i += 2
                                        changed = True
                                        continue
                            # 3. Replace non-octilinear escape stub + octilinear segment with exact 45-deg dogleg
                            if diagonal and (_is_octilinear(p_start, p_mid) != _is_octilinear(p_mid, p_end)):
                                replaced = None
                                for dogleg in _octilinear_doglegs(p_start, p_end, diagonal=True, width=t1["width"]):
                                    segs = [(p, q) for p, q in zip(dogleg, dogleg[1:]) if math.dist(p, q) > 1e-6]
                                    if segs and all(_is_octilinear(p, q) for p, q in segs) and all(
                                        model.trace_ok(net, t1["layer"], p[0], p[1], q[0], q[1], t1["width"])
                                        for p, q in segs
                                    ):
                                        replaced = [_trace(net, t1["layer"], p, q, t1["width"]) for p, q in segs]
                                        break
                                if replaced is not None:
                                    merged.extend(replaced)
                                    i += 2
                                    changed = True
                                    continue
                merged.append(seq[i])
                i += 1
            seq = merged
        return seq

    out = merge_pass(out)
    if diagonal and len(out) >= 2:
        chamfered = []
        i = 0
        while i < len(out):
            if i + 1 < len(out):
                t1, t2 = out[i], out[i + 1]
                p_mid = (t1["ex"], t1["ey"])
                if (t1["layer"] == t2["layer"]
                        and abs(t1["width"] - t2["width"]) < 1e-9
                        and math.hypot(p_mid[0] - t2["sx"], p_mid[1] - t2["sy"]) < 1e-6
                        and not is_via_site(p_mid)):
                    ax, ay = t1["ex"] - t1["sx"], t1["ey"] - t1["sy"]
                    bx, by = t2["ex"] - t2["sx"], t2["ey"] - t2["sy"]
                    la, lb = math.hypot(ax, ay), math.hypot(bx, by)
                    w = t1["width"]
                    if la > 1.5 * w and lb > 1.5 * w and abs(ax * bx + ay * by) / (la * lb) < 0.1:
                        bevel_installed = False
                        for c in (min(la, lb) * 0.45, min(la, lb) * 0.25, min(la * 0.45, lb * 0.45, max(2.0 * w, 20.0))):
                            if c < w:
                                continue
                            p1 = (p_mid[0] - (ax / la) * c, p_mid[1] - (ay / la) * c)
                            p2 = (p_mid[0] + (bx / lb) * c, p_mid[1] + (by / lb) * c)
                            cand_pts = [(t1["sx"], t1["sy"]), p1, p2, (t2["ex"], t2["ey"])]
                            segs = [(p, q) for p, q in zip(cand_pts, cand_pts[1:]) if math.dist(p, q) > 1e-6]
                            if all(model.trace_ok(net, t1["layer"], p[0], p[1], q[0], q[1], w) for p, q in segs):
                                chamfered.extend(_trace(net, t1["layer"], p, q, w) for p, q in segs)
                                i += 2
                                bevel_installed = True
                                break
                        if bevel_installed:
                            continue
            chamfered.append(out[i])
            i += 1
        out = merge_pass(chamfered)
    return out


def _direct(model, net, source, target, settings, diagonal, budget):
    candidates = []
    for layer in settings["layers"]:
        if layer in source["layers"] and layer in target["layers"]:
            a, b = nearest_points(source["layers"][layer], target["layers"][layer])
            candidates.append((a.distance(b), layer, (a.x, a.y), (b.x, b.y)))
    for dist, layer, a, b in sorted(candidates):
        if diagonal:
            if _is_octilinear(a, b) or dist <= max(2.0 * settings["width"], 24.0):
                paths = ([a, b],)
            else:
                continue
        else:
            paths = ([a, b],) if a[0] == b[0] or a[1] == b[1] else (
                [a, (a[0], b[1]), b], [a, (b[0], a[1]), b])
        for path in paths:
            budget.check()
            segs = [(start, end) for start, end in zip(path, path[1:]) if start != end]
            if segs and all(model.trace_ok(net, layer, *start, *end, settings["width"])
                            for start, end in segs):
                raw = [_trace(net, layer, start, end, settings["width"]) for start, end in segs]
                return _postprocess_traces(model, net, raw, [], diagonal), []
    return None


def _ports(model, net, group, settings, step, bounds, limit, budget, diagnostics, diagonal=True):
    """Real-copper launch paths, including a short outward fanout before snapping.

    A nearest-grid diagonal alone can cut across an adjacent 0.65-mm pin.
    Every segment of these additional paths receives the same solid check.
    This is a finite escape candidate set, never a proof of no physical escape.
    """
    ports, scanned, blocked, details = {}, 0, Counter(), {}
    diagnostics.update(candidates_tested=0, legal_ports=0)
    lo_x, lo_y, hi_x, hi_y = bounds

    def accept(layer, path):
        nonlocal scanned
        scanned += 1
        budget.check()
        diagnostics["candidates_tested"] = scanned
        diagnostics["legal_ports"] = len(ports)
        if scanned > limit * 4:
            raise RoutingFailure("SEARCH_BUDGET", f"routing {net}: escape-port candidate budget exhausted",
                                 **budget.context, escape_scan_limit=limit * 4,
                                 interpretation="Finite escape sampling stopped; geometry feasibility is undetermined.")
        point = path[-1]
        node = (round(point[0] / step), round(point[1] / step), layer)
        if not (lo_x <= node[0] <= hi_x and lo_y <= node[1] <= hi_y):
            return
        distance = sum(math.dist(a, b) for a, b in zip(path, path[1:]))
        if node in ports and distance >= ports[node][1]:
            return
        segments = list(zip(path, path[1:])) or [(path[0], path[0])]
        for a, b in segments:
            if not model.trace_ok(net, layer, *a, *b, settings["width"]):
                # Sample explanations only at escape sites, off the A* hot path.
                if scanned <= 64:
                    explain = getattr(model, "explain_trace", None)
                    reason = explain(net, layer, *a, *b, settings["width"]) if explain else None
                    if reason:
                        key = (str(reason.get("rule")), str(reason.get("object")))
                        blocked[key] += 1
                        details[key] = reason
                return
        ports[node] = (path, distance)
        diagnostics["legal_ports"] = len(ports)

    for layer, geometry in group["layers"].items():
        if layer not in settings["layers"]:
            continue
        trace_lines = group.get("trace_lines", {}).get(layer, ())
        # Separate islands avoid scanning the empty rectangle between distant branches.
        for part in _pieces(geometry):
            xmin, ymin, xmax, ymax = part.bounds
            for ix in range(max(lo_x, math.ceil(xmin / step) - 2),
                            min(hi_x, math.floor(xmax / step) + 2) + 1):
                for iy in range(max(lo_y, math.ceil(ymin / step) - 2),
                                min(hi_y, math.floor(ymax / step) + 1 + 1)):
                    budget.check()
                    node = (ix, iy, layer)
                    if node in ports:
                        continue
                    point = Point(ix * step, iy * step)
                    anchor = nearest_points(part, point)[0]
                    if anchor.distance(point) > 2.9 * step:
                        continue
                    if anchor.distance(point) > 1e-6 and trace_lines:
                        for line, t_width in trace_lines:
                            if line.distance(anchor) <= t_width * 0.55:
                                c_pt = nearest_points(line, point)[0]
                                if part.covers(c_pt):
                                    anchor = c_pt
                                break
                    a, b = (anchor.x, anchor.y), (point.x, point.y)
                    if a != b and diagonal and not _is_octilinear(a, b):
                        dogleg = _octilinear_doglegs(a, b, diagonal=True, width=settings["width"])[0]
                        cleaned = [p for i, p in enumerate(dogleg) if not i or p != dogleg[i - 1]]
                        accept(layer, cleaned)
                        if node in ports:
                            continue
                    accept(layer, [a, b])
            # Follow the real pad/island axes out of the pin field before the
            # coarse-grid connector. This also works with vias forbidden.
            center = part.representative_point()
            rectangle = list(part.minimum_rotated_rectangle.exterior.coords)
            axes = [(1.0, 0.0), (0.0, 1.0)]
            for a, b in zip(rectangle, rectangle[1:3]):
                length = math.dist(a, b)
                if length:
                    axes.append(((b[0] - a[0]) / length, (b[1] - a[1]) / length))
            radius = max(step, settings["width"])
            for dx, dy in axes:
                for sign in (-1, 1):
                    for multiplier in (1, 2, 4, 8):
                        tip = (center.x + sign * dx * radius * multiplier,
                               center.y + sign * dy * radius * multiplier)
                        p = nearest_points(part, Point(tip))[0]
                        anchor = (p.x, p.y)
                        snap = (round(tip[0] / step) * step, round(tip[1] / step) * step)
                        for sub in _octilinear_doglegs(tip, snap, diagonal=diagonal, width=settings["width"])[:2]:
                            path = [anchor] + sub
                            accept(layer, [p for i, p in enumerate(path) if not i or p != path[i - 1]])
    diagnostics["top_blockers"] = [dict(details[key], rejected_samples=count)
                                   for key, count in blocked.most_common(5)]
    diagnostics["blocker_sampling"] = "First blocker per rejected path, among the first 64 tested candidates."
    diagnostics["candidate_set_is_exhaustive"] = False
    return ports


def _offgrid_via_path(model, net, source, target, settings, options, forbidden_vias, stats, budget):
    """Bounded visibility through real copper anchors, separate from grid A*."""
    stack = tuple(model.rules.copper_layers)
    if not settings["vias"] or not set(stack).issubset(settings.get("via_layers", settings["layers"])):
        return None
    budget.context["phase"] = "offgrid_via_visibility_search"

    def anchors(group, other):
        boxes = [geometry.bounds for geometry in other["layers"].values() if not geometry.is_empty]
        if not boxes:
            return []
        xmin, ymin = min(b[0] for b in boxes), min(b[1] for b in boxes)
        xmax, ymax = max(b[2] for b in boxes), max(b[3] for b in boxes)

        def candidates():
            serial = itertools.count()
            for layer, geometry in group["layers"].items():
                if layer not in settings["layers"]:
                    continue
                for part in _pieces(geometry):
                    budget.check()
                    for point in (part.centroid, part.representative_point()):
                        budget.check()
                        # A centroid may be in a drilled hole or outside a
                        # concave island; it must be on evidenced real copper.
                        if not part.covers(point):
                            continue
                        x, y = point.x, point.y
                        distance = math.hypot(max(xmin - x, 0, x - xmax),
                                              max(ymin - y, 0, y - ymax))
                        yield distance, next(serial), (x, y, layer)

        selected = heapq.nsmallest(64, candidates())
        seen, legal = set(), []
        for _, _, anchor in selected:
            budget.check()
            x, y, _ = anchor
            site = (x, y)
            if site in seen or (round(x / options["grid_step"]), round(y / options["grid_step"])) in forbidden_vias:
                continue
            seen.add(site)
            stats["offgrid_via_visibility_checks"] = stats.get("offgrid_via_visibility_checks", 0) + 1
            if model.via_ok(net, x, y, settings["hole"], settings["diameter"], stack):
                legal.append(anchor)
                if len(legal) == 16 and not settings.get("via_in_pad_penalty", 0):
                    break
        if settings.get("via_in_pad_penalty", 0):
            legal = _soft_order(legal, lambda anchor: _via_preference_cost(model, *anchor[:2], settings, stack), 16)
        return legal

    starts, goals = anchors(source, target), anchors(target, source)
    def pair_cost(a, b):
        distance = math.dist(a[:2], b[:2])
        if settings.get("via_in_pad_penalty", 0):
            distance = (distance * settings["alpha"] + _via_preference_cost(model, *a[:2], settings, stack)
                        + _via_preference_cost(model, *b[:2], settings, stack))
        return distance

    pairs = sorted((math.dist(a[:2], b[:2]), a, b) for a in starts for b in goals if a[:2] != b[:2])
    pairs = (_soft_order(pairs, lambda item: pair_cost(item[1], item[2]), 64)
             if settings.get("via_in_pad_penalty", 0) else pairs[:64])
    for _, a, b in pairs:
        budget.check()
        pa, pb = a[:2], b[:2]
        for layer in settings["layers"]:
            if layer in (a[2], b[2]):
                continue
            paths = list(_octilinear_doglegs(pa, pb, diagonal=options["diagonal"], width=settings["width"]))
            if options["diagonal"] and not _is_octilinear(pa, pb):
                paths.append([pa, pb])
            for path in paths:
                budget.check()
                stats["offgrid_via_visibility_paths"] = stats.get("offgrid_via_visibility_paths", 0) + 1
                if all(model.trace_ok(net, layer, *p, *q, settings["width"])
                       for p, q in zip(path, path[1:]) if p != q):
                    traces = [_trace(net, layer, p, q, settings["width"])
                              for p, q in zip(path, path[1:]) if p != q]
                    vias = [dict(net=net, x=x, y=y, hole=settings["hole"],
                                 diameter=settings["diameter"], layers=list(stack)) for x, y in (pa, pb)]
                    return _postprocess_traces(model, net, traces, vias, options["diagonal"]), vias
    return None


def _local_via_path(model, net, source, target, settings, options, forbidden_vias, stats, budget):
    """Lift narrow off-grid pins before the global lattice search.

    A legal trace escape need not end at a legal grid-aligned through-via site.
    Sample short real-copper fanouts and try existing copper on each inner face
    first; this often joins a large routed group with only one additional via.
    These are candidates only: the caller still performs the physical commit.
    """
    stack = tuple(model.rules.copper_layers)
    if len(stack) < 2 or not settings["vias"] or not set(stack).issubset(settings.get("via_layers", settings["layers"])):
        return None
    budget.context["phase"] = "local_via_visibility_search"
    width = settings["width"]
    prefer = settings.get("via_in_pad_penalty", 0) > 0

    def ranked_fanouts(legal):
        if not prefer:
            return legal
        # 有限采样池内以短引线长度和盘中孔罚分排序，孔仍须通过原 via_ok。
        return _soft_order(legal, lambda item: item[2] * settings["alpha"]
                           + _via_preference_cost(model, *item[1][-1], settings, stack), 16)

    def fanouts(group, other):
        other_shape = unary_union(list(other["layers"].values()))
        parts = [(layer, part) for layer, geometry in group["layers"].items()
                 if layer in settings["layers"] for part in _pieces(geometry)]
        parts.sort(key=lambda entry: entry[1].distance(other_shape))
        legal, seen = [], set()
        radius = max(settings["diameter"] / 2, width)
        for layer, part in parts[:8]:
            budget.check()
            centers = [part.representative_point()]
            if part.covers(part.centroid):
                centers.append(part.centroid)
            axes = [(1.0, 0.0), (0.0, 1.0)]
            if options["diagonal"]:
                axes.extend([(2 ** -0.5, 2 ** -0.5), (2 ** -0.5, -2 ** -0.5)])
            rectangle = list(part.minimum_rotated_rectangle.exterior.coords)
            for a, b in zip(rectangle, rectangle[1:3]):
                length = math.dist(a, b)
                if length:
                    axis = ((b[0] - a[0]) / length, (b[1] - a[1]) / length)
                    if options["diagonal"] or abs(axis[0]) < 1e-12 or abs(axis[1]) < 1e-12:
                        axes.append(axis)
            for multiplier in (0, 1, 1.5, 2, 3, 4, 6, 8, 12, 16):
                for center in centers:
                    anchor = (center.x, center.y)
                    for dx, dy in axes:
                        for sign in (-1, 1):
                            budget.check()
                            point = (anchor[0] + sign * dx * radius * multiplier,
                                     anchor[1] + sign * dy * radius * multiplier)
                            key = (*point, layer)
                            if key in seen or (round(point[0] / options["grid_step"]),
                                               round(point[1] / options["grid_step"])) in forbidden_vias:
                                continue
                            seen.add(key)
                            stats["local_via_checks"] = stats.get("local_via_checks", 0) + 1
                            # A via centred on existing copper has no trace stub.
                            # Do not invent a width-sized zero-length disc there;
                            # the actual via and drilled contact are checked below
                            # and again by the physical commit.
                            if anchor != point and not model.trace_ok(net, layer, *anchor, *point, width):
                                continue
                            if not model.via_ok(net, *point, settings["hole"], settings["diameter"], stack):
                                continue
                            legal.append((layer, [anchor, point], math.dist(anchor, point)))
                            if len(legal) >= (64 if prefer else 16):
                                return ranked_fanouts(legal)
        if legal:
            return ranked_fanouts(legal)
        # Existing tracks on different faces can obstruct every straight ray.
        # A small pin-relative lattice finds bent escapes without quantizing
        # the pin onto the board-wide grid or expanding the whole board.
        step = min(options["grid_step"], width / 4, settings["diameter"] / 4)
        extent = 8 * settings["diameter"]
        directions = [(1, 0), (-1, 0), (0, 1), (0, -1)]
        if options["diagonal"]:
            directions += [(1, 1), (1, -1), (-1, 1), (-1, -1)]
        expanded = 0
        for layer, part in parts[:4]:
            center = part.representative_point()
            origin = (center.x, center.y)
            if not model.trace_ok(net, layer, *origin, *origin, width):
                continue
            queue, costs, previous = [(0.0, (0, 0))], {(0, 0): 0.0}, {}
            while queue and expanded < min(4096, options.get("max_expansions", 4096)):
                distance, node = heapq.heappop(queue)
                if distance != costs[node]:
                    continue
                budget.check()
                expanded += 1
                stats["local_escape_expansions"] = stats.get("local_escape_expansions", 0) + 1
                point = (origin[0] + node[0] * step, origin[1] + node[1] * step)
                key = (round(point[0] / options["grid_step"]), round(point[1] / options["grid_step"]))
                if key not in forbidden_vias and model.via_ok(net, *point, settings["hole"], settings["diameter"], stack):
                    nodes, cursor = [node], node
                    while cursor in previous:
                        cursor = previous[cursor]
                        nodes.append(cursor)
                    nodes.reverse()
                    # Delete only collinear intermediate lattice nodes; this
                    # preserves the exact union of the already checked edges.
                    compact = []
                    for item in nodes:
                        while len(compact) > 1:
                            a, b = compact[-2], compact[-1]
                            if ((b[0] - a[0]) * (item[1] - b[1])
                                    != (b[1] - a[1]) * (item[0] - b[0])):
                                break
                            compact.pop()
                        compact.append(item)
                    path = [(origin[0] + x * step, origin[1] + y * step) for x, y in compact]
                    legal.append((layer, path, distance))
                    if len(legal) >= (64 if prefer else 16):
                        return ranked_fanouts(legal)
                for dx, dy in directions:
                    nxt = (node[0] + dx, node[1] + dy)
                    if max(abs(nxt[0]), abs(nxt[1])) * step > extent:
                        continue
                    candidate = distance + math.hypot(dx, dy) * step
                    if candidate >= costs.get(nxt, math.inf):
                        continue
                    tip = (origin[0] + nxt[0] * step, origin[1] + nxt[1] * step)
                    if model.trace_ok(net, layer, *point, *tip, width):
                        costs[nxt], previous[nxt] = candidate, node
                        heapq.heappush(queue, (candidate, nxt))
        return ranked_fanouts(legal)

    def via(point):
        return dict(net=net, x=point[0], y=point[1], hole=settings["hole"],
                    diameter=settings["diameter"], layers=list(stack))

    def checked_middle(layer, a, b):
        for path in _octilinear_doglegs(a, b, diagonal=options["diagonal"], width=width):
            budget.check()
            stats["local_via_paths"] = stats.get("local_via_paths", 0) + 1
            if all(model.trace_ok(net, layer, *p, *q, width)
                   for p, q in zip(path, path[1:]) if p != q):
                return [_trace(net, layer, p, q, width)
                        for p, q in zip(path, path[1:]) if p != q]
        return None

    # Usually the smaller group is the unrouted pin. Sample it first and use
    # the routed group's existing inner copper instead of drilling both ends.
    small, large = sorted((source, target), key=lambda group: len(group["terminals"]))
    small_fanouts = fanouts(small, large)
    for group, escapes in ((large, small_fanouts),):
        for face, path, _ in escapes:
            point = path[-1]
            candidates = []
            for layer, geometry in group["layers"].items():
                if layer == face or layer not in settings["layers"]:
                    continue
                for part in _pieces(geometry):
                    anchor = nearest_points(part, Point(point))[0]
                    for line, trace_width in group.get("trace_lines", {}).get(layer, ()):
                        if line.distance(anchor) <= trace_width * .55:
                            center = nearest_points(line, Point(point))[0]
                            if part.covers(center):
                                anchor = center
                            break
                    candidates.append((anchor.distance(Point(point)), layer, (anchor.x, anchor.y)))
            for _, layer, anchor in sorted(candidates)[:32]:
                middle = checked_middle(layer, point, anchor)
                if middle is None:
                    continue
                traces = [_trace(net, face, a, b, width) for a, b in zip(path, path[1:]) if a != b] + middle
                vias = [via(point)]
                return _postprocess_traces(model, net, traces, vias, options["diagonal"]), vias

    large_fanouts = fanouts(large, small)
    def pair_cost(a, b):
        value = a[2] + b[2] + math.dist(a[1][-1], b[1][-1])
        if prefer:
            value = (value * settings["alpha"] + _via_preference_cost(model, *a[1][-1], settings, stack)
                     + _via_preference_cost(model, *b[1][-1], settings, stack))
        return value

    pairs = sorted((a[2] + b[2] + math.dist(a[1][-1], b[1][-1]), i, j)
                   for i, a in enumerate(small_fanouts) for j, b in enumerate(large_fanouts)
                   if a[1][-1] != b[1][-1])
    pairs = (_soft_order(pairs, lambda item: pair_cost(small_fanouts[item[1]], large_fanouts[item[2]]), 64)
             if prefer else pairs[:64])
    for _, i, j in pairs:
        a, b = small_fanouts[i], large_fanouts[j]
        for layer in settings["layers"]:
            if layer in (a[0], b[0]):
                continue
            middle = checked_middle(layer, a[1][-1], b[1][-1])
            if middle is None:
                continue
            traces = [_trace(net, face, p, q, width) for face, path, _ in (a, b)
                      for p, q in zip(path, path[1:]) if p != q] + middle
            vias = [via(a[1][-1]), via(b[1][-1])]
            return _postprocess_traces(model, net, traces, vias, options["diagonal"]), vias
    return None


def _prefer_local_candidate(model, net, source, target, settings, options, forbidden_vias, stats, budget, candidate):
    """为含盘中孔的现有捷径尝试有限局部替代，返回综合代价较低者。

    入参包括已找到的候选及父预算；最多使用剩余预算的四分之一且不超过
    一秒。局部预算耗尽时保留原候选，其他异常照常传播；不放松硬检查。
    """
    if not any(_via_preference_cost(model, v["x"], v["y"], settings, v["layers"])
               for v in candidate[1]):
        return candidate
    current, remaining = budget, math.inf
    while isinstance(current, _PairBudget):
        remaining = min(remaining, current.deadline - time.monotonic())
        current = current.parent
    remaining = min(remaining, current.total_limit - (time.monotonic() - current.started),
                    current.net_limit - current.net_elapsed())
    if remaining <= 0:
        return candidate
    stats["via_in_pad_preference_attempts"] = stats.get("via_in_pad_preference_attempts", 0) + 1
    try:
        alternative = _local_via_path(model, net, source, target, settings, options,
                                      forbidden_vias, stats, _PairBudget(budget, min(1.0, remaining / 4)))
    except RoutingFailure as error:
        if error.diagnostics["code"] != "PAIR_TIME_BUDGET":
            raise
        stats["via_in_pad_preference_budget_stops"] = stats.get("via_in_pad_preference_budget_stops", 0) + 1
        return candidate
    return min((candidate, alternative), key=lambda result: _candidate_cost(model, result, settings)) if alternative else candidate


class _GridSession:
    """同一网络的有界网格缓存；新网络或有序端子边使用独立会话。"""
    def __init__(self):
        """无入参；建立空缓存，尚不分配栅格；无业务异常。"""
        self.grid = self.model = self.pending_bounds = None
        self.refining = False

    def get(self, model, net, settings, options, stats, budget):
        """输入当前碰撞模型与预算，返回可查表栅格；预算不足抛出 RoutingFailure。

        同网每次新增铜后只更新受影响包围盒。构建和更新也计入外层预算；
        未知几何错误继续传播，不把构建失败解释为没有障碍。
        """
        from pcb_occupancy import OccupancyGrid, OccupancyBudgetError
        deadline = time.monotonic() + options.get("occupancy_max_seconds", 10.0)
        try:
            if self.grid is not None:
                # 延后重试可能换用更宽的单对预算；不能保留上一轮的过期回调。
                self.grid.budget_check = budget.check
            if self.grid is None:
                self.grid = OccupancyGrid(model, net, settings, options["grid_step"],
                    max_cells=options.get("occupancy_max_cells", 2000000),
                    deadline=deadline, stats=stats, budget_check=budget.check)
            elif self.model is not model:
                if self.pending_bounds is None:
                    raise PipelineError("栅格缓存来源已变化，但没有明确的新增铜范围")
                self.grid.deadline = deadline
                self.grid.budget_check = budget.check
                self.grid.update(model, self.pending_bounds)
            self.model, self.pending_bounds = model, None
            return self.grid
        except OccupancyBudgetError as exc:
            raise RoutingFailure("OCCUPANCY_BUDGET", "栅格预算内未找到可用候选，不能据此判定无解",
                                 **budget.context, detail=str(exc)) from exc

    def committed(self, before, after):
        """输入提交前后完整板，累计新增铜包围盒；不改板，异常按几何函数传播。"""
        self.refining = False
        if self.grid is None:
            return
        bounds = [] if self.pending_bounds is None else [self.pending_bounds]
        tolerance = rules_for(after).curve_tolerance
        for kind in ("traces", "vias"):
            old = before.get("routes", {}).get(kind, [])
            new = after.get("routes", {}).get(kind, [])
            if new[:len(old)] != old:
                raise PipelineError("增量栅格仅接受已核对的追加铜；原有铜发生变化")
            for item in new[len(old):]:
                shape = trace_bounds(item, tolerance) if kind == "traces" else via_bounds(item, tolerance)[0]
                bounds.append(shape.outer.bounds)
        if bounds:
            self.pending_bounds = (min(b[0] for b in bounds), min(b[1] for b in bounds),
                                   max(b[2] for b in bounds), max(b[3] for b in bounds))

    def reject(self, rejections, traces, vias, model, net, settings, options, stats, budget):
        """输入精检失败图元并标记冲突格，返回是否新增阻塞；预算异常继续传播。

        线段失败阻塞其经过的网格点；接触失败只阻塞一个中间候选点，
        不删除真实端子。所有标记只作用于有限搜索，不改实际板或设计规则。
        """
        self.refining = True
        grid = self.get(model, net, settings, options, stats, budget)
        step = options["grid_step"]
        nodes, via_nodes = set(), set()
        for rejection in rejections:
            budget.check()
            if rejection["kind"] == "via":
                v = rejection["item"]
                via_nodes.add((round(v["x"] / step), round(v["y"] / step)))
            elif rejection["kind"] == "trace":
                t = rejection["item"]
                count = max(1, math.ceil(max(abs(t["ex"] - t["sx"]), abs(t["ey"] - t["sy"])) / step))
                for i in range(count + 1):
                    budget.check()
                    nodes.add((round((t["sx"] + (t["ex"] - t["sx"]) * i / count) / step),
                               round((t["sy"] + (t["ey"] - t["sy"]) * i / count) / step), t["layer"]))
        if not nodes and not via_nodes:
            if vias:
                via_nodes.update((round(v["x"] / step), round(v["y"] / step)) for v in vias)
            elif traces:
                t = traces[len(traces) // 2]
                nodes.add((round((t["sx"] + t["ex"]) / (2 * step)),
                           round((t["sy"] + t["ey"]) / (2 * step)), t["layer"]))
        count = grid.block_cells(nodes) + grid.block_cells(via_nodes, via=True)
        return count > 0


def _search(model, net, source, target, settings, options, forbidden_vias, stats, budget, occupancy=None):
    """输入铜组和规则，返回候选铜；有限搜索失败抛出 RoutingFailure。

    栅格开关只替换 A* 逐边候选检查。返回值仍须经过 _commit；
    端口、可见性候选和后处理继续使用精确几何检查。
    """
    started = time.monotonic()
    try:
        try:
            return _search_impl(model, net, source, target, settings, options,
                                forbidden_vias, stats, budget, occupancy)
        except RoutingFailure as failure:
            cfg = options.get("geometry_search", {})
            if (not cfg.get("enabled") or failure.diagnostics["code"] in
                    ("TIME_BUDGET", "PAIR_TIME_BUDGET", "OCCUPANCY_BUDGET", "GEOMETRY_HARD_FAILURE")):
                raise
            last_failure = failure
            for factor in cfg.get("refine_factors", [2.0, 4.0]):
                budget.check()
                refined = dict(options, grid_step=options["grid_step"] / factor,
                               max_expansions=min(options["max_expansions"], cfg.get("max_refine_expansions", 10000)),
                               geometry_search={"enabled": False}, occupancy_grid=False)
                shapes = list(source["layers"].values()) + list(target["layers"].values())
                margin = cfg.get("margin", 100.0)
                refined["_search_bounds_mil"] = (min(s.bounds[0] for s in shapes)-margin,
                    min(s.bounds[1] for s in shapes)-margin, max(s.bounds[2] for s in shapes)+margin,
                    max(s.bounds[3] for s in shapes)+margin)
                record = dict(net=net, grid_step=refined["grid_step"], max_expansions=refined["max_expansions"], found=False)
                stats.setdefault("geometry_refinements", []).append(record)
                try:
                    # 禁孔集合原来以粗格索引表示，换步长须保持同一个物理位置。
                    blocked = {(round(x * factor), round(y * factor)) for x, y in forbidden_vias}
                    candidate = _search_impl(model, net, source, target, settings, refined,
                                             blocked, stats, budget, None)
                    record["found"] = True
                    return candidate
                except RoutingFailure as exc:
                    record["stop_reason"] = exc.diagnostics["code"]
                    if exc.diagnostics["code"] in ("TIME_BUDGET", "PAIR_TIME_BUDGET", "OCCUPANCY_BUDGET"):
                        raise
                    last_failure = exc
            raise last_failure
    finally:
        stats["search_seconds"] = stats.get("search_seconds", 0.0) + time.monotonic() - started


def _search_impl(model, net, source, target, settings, options, forbidden_vias, stats, budget, occupancy):
    """执行候选搜索；参数、返回及异常与 _search 相同。"""
    if (options.get("geometry_search", {}).get("enabled") and not options.get("_skip_geometry")
            and not getattr(occupancy, "refining", False)):
        from pcb_geometric_router import geometry_candidate
        try:
            geometric = geometry_candidate(model, net, source, target, settings, options, stats, budget)
        except RoutingFailure as exc:
            if exc.diagnostics["code"] != "GEOMETRY_BUDGET":
                raise
            stats["geometry_budget_fallbacks"] = stats.get("geometry_budget_fallbacks", 0) + 1
            geometric = None
        if geometric is not None:
            return geometric
    if options.get("occupancy_grid", False) and occupancy is None:
        occupancy = _GridSession()
    step = options["grid_step"]
    xmin, ymin, xmax, ymax = model.board.bounds
    if "_search_bounds_mil" in options:
        rx0, ry0, rx1, ry1 = options["_search_bounds_mil"]
        xmin, ymin, xmax, ymax = max(xmin, rx0), max(ymin, ry0), min(xmax, rx1), min(ymax, ry1)
    bounds = (math.ceil(xmin / step), math.ceil(ymin / step),
              math.floor(xmax / step), math.floor(ymax / step))
    limit = options["max_expansions"]
    context = _context(net, source, target, settings, step)
    context.update(source_escape={}, target_escape={}, search_bounds=list(bounds))
    budget.context = context
    stats["searches"] += 1
    context["phase"] = "source_escape"
    starts = _ports(model, net, source, settings, step, bounds, limit, budget, context["source_escape"], options["diagonal"])
    context["phase"] = "target_escape"
    goals = _ports(model, net, target, settings, step, bounds, limit, budget, context["target_escape"], options["diagonal"])
    budget.check()
    if not starts or not goals:
        offgrid = _offgrid_via_path(model, net, source, target, settings, options, forbidden_vias, stats, budget)
        if offgrid is not None:
            return _prefer_local_candidate(model, net, source, target, settings, options,
                                           forbidden_vias, stats, budget, offgrid)
        local = _local_via_path(model, net, source, target, settings, options, forbidden_vias, stats, budget)
        if local is not None:
            return local
        raise RoutingFailure("NO_LEGAL_ESCAPE", f"routing {net}: no legal real-copper escape in the tested candidates",
                             **context,
                             interpretation="No tested escape is legal; this finite candidate set does not prove fabrication infeasibility.")
    if occupancy is not None and occupancy.refining:
        # 精检拒绝后跳过可见性捷径，防止再次提出同一条被拒候选。
        return _timed_astar(model, net, source, target, settings, options, forbidden_vias,
                            stats, budget, starts, goals, bounds, context, occupancy)
    # Long sparse connections should not require visiting every intervening grid
    # point. Try a bounded set of solid-checked visibility paths between ports.
    context["phase"] = "visibility_search"
    def nearby(ports, other):
        xs, ys = [n[0] for n in other], [n[1] for n in other]
        box = (min(xs), min(ys), max(xs), max(ys))

        def key(node):
            dx = max(box[0] - node[0], 0, node[0] - box[2])
            dy = max(box[1] - node[1], 0, node[1] - box[3])
            return math.hypot(dx, dy) * step + ports[node][1]

        return heapq.nsmallest(64, ports, key=key)

    near_starts, near_goals = nearby(starts, goals), nearby(goals, starts)
    budget.check()

    def visibility_paths(a, b):
        return _octilinear_doglegs(a, b, diagonal=options["diagonal"], width=settings["width"])

    pairs = sorted((a_path[1] + b_path[1] + math.dist(a[:2], b[:2]) * step, a, b)
                   for a in near_starts for b in near_goals if a[2] == b[2]
                   for a_path, b_path in [(starts[a], goals[b])])
    for _, a, b in pairs[:64]:
        budget.check()
        pa, pb = (a[0] * step, a[1] * step), (b[0] * step, b[1] * step)
        for middle in visibility_paths(pa, pb):
            budget.check()
            stats["visibility_checks"] = stats.get("visibility_checks", 0) + 1
            if all(model.trace_ok(net, a[2], *p, *q, settings["width"])
                   for p, q in zip(middle, middle[1:]) if p != q):
                path = starts[a][0] + middle[1:] + list(reversed(goals[b][0]))[1:]
                raw = [_trace(net, a[2], p, q, settings["width"])
                       for p, q in zip(path, path[1:]) if p != q]
                return _postprocess_traces(model, net, raw, [], options["diagonal"]), []
    # A long blocked face can often use a clear inner face. Keep the native
    # through-via span and all its obstacles; never infer a blind-via process.
    stack = tuple(model.rules.copper_layers)
    if settings["vias"] and set(stack).issubset(settings.get("via_layers", settings["layers"])):
        context["phase"] = "via_visibility_search"

        def through_ports(candidates):
            legal = []
            for node in candidates:
                budget.check()
                if node[:2] in forbidden_vias:
                    continue
                stats["via_visibility_checks"] = stats.get("via_visibility_checks", 0) + 1
                if model.via_ok(net, node[0] * step, node[1] * step,
                                settings["hole"], settings["diameter"], stack):
                    legal.append(node)
                    if len(legal) == 16 and not settings.get("via_in_pad_penalty", 0):
                        break
            if settings.get("via_in_pad_penalty", 0):
                legal = _soft_order(legal, lambda node:
                    _via_preference_cost(model, node[0] * step, node[1] * step, settings, stack), 16)
            return legal

        via_starts, via_goals = through_ports(near_starts), through_ports(near_goals)
        budget.check()
        def via_pair_cost(a, b):
            value = starts[a][1] + goals[b][1] + math.dist(a[:2], b[:2]) * step
            if settings.get("via_in_pad_penalty", 0):
                value = (value * settings["alpha"]
                         + _via_preference_cost(model, a[0] * step, a[1] * step, settings, stack)
                         + _via_preference_cost(model, b[0] * step, b[1] * step, settings, stack))
            return value

        via_pairs = sorted((starts[a][1] + goals[b][1] + math.dist(a[:2], b[:2]) * step, a, b)
                           for a in via_starts for b in via_goals if a[:2] != b[:2])
        via_pairs = (_soft_order(via_pairs, lambda item: via_pair_cost(item[1], item[2]), 64)
                     if settings.get("via_in_pad_penalty", 0) else via_pairs[:64])
        for _, a, b in via_pairs:
            budget.check()
            pa, pb = (a[0] * step, a[1] * step), (b[0] * step, b[1] * step)
            for layer in settings["layers"]:
                if layer in (a[2], b[2]):
                    continue
                for middle in visibility_paths(pa, pb):
                    budget.check()
                    stats["via_visibility_paths"] = stats.get("via_visibility_paths", 0) + 1
                    if not all(model.trace_ok(net, layer, *p, *q, settings["width"])
                               for p, q in zip(middle, middle[1:]) if p != q):
                        continue
                    traces = []
                    for face, path in ((a[2], starts[a][0]), (layer, middle),
                                       (b[2], list(reversed(goals[b][0])))):
                        traces.extend(_trace(net, face, p, q, settings["width"])
                                      for p, q in zip(path, path[1:]) if p != q)
                    vias = [{"net": net, "x": p[0], "y": p[1], "hole": settings["hole"],
                             "diameter": settings["diameter"], "layers": list(stack)} for p in (pa, pb)]
                    candidate = _postprocess_traces(model, net, traces, vias, options["diagonal"]), vias
                    return _prefer_local_candidate(model, net, source, target, settings, options,
                                                   forbidden_vias, stats, budget, candidate)
    offgrid = _offgrid_via_path(model, net, source, target, settings, options, forbidden_vias, stats, budget)
    if offgrid is not None:
        return _prefer_local_candidate(model, net, source, target, settings, options,
                                       forbidden_vias, stats, budget, offgrid)
    local = _local_via_path(model, net, source, target, settings, options, forbidden_vias, stats, budget)
    if local is not None:
        return local
    return _timed_astar(model, net, source, target, settings, options, forbidden_vias,
                        stats, budget, starts, goals, bounds, context, occupancy)


def _timed_astar(*args):
    """输入 A* 参数，返回候选；累计搜索秒数，异常原样传播。"""
    stats = args[7]
    started = time.monotonic()
    grid_before = stats.get("occupancy_build_seconds", 0.0) + stats.get("occupancy_update_seconds", 0.0)
    try:
        return _astar_candidate(*args)
    finally:
        grid_after = stats.get("occupancy_build_seconds", 0.0) + stats.get("occupancy_update_seconds", 0.0)
        stats["astar_seconds"] = stats.get("astar_seconds", 0.0) + max(0.0, time.monotonic() - started - grid_after + grid_before)


def _astar_candidate(model, net, source, target, settings, options, forbidden_vias,
                     stats, budget, starts, goals, bounds, context, occupancy=None):
    """在给定端口间生成有界 A* 候选，不代表实体铜已验收。

    入参：碰撞模型、网络、端子组、规则、预算、端口及网格边界；
    occupancy 为可选网格会话。返回：(候选走线列表，候选过孔列表)。
    异常：节点、时间或栅格预算耗尽及未找到路径时抛出 RoutingFailure；
    非法参数抛出 PipelineError。调用方必须继续执行 _commit 与独立验收。
    """
    step = options["grid_step"]
    limit = options["max_expansions"]
    stack = tuple(model.rules.copper_layers)
    grid = occupancy.get(model, net, settings, options, stats, budget) if occupancy else None
    gx = [n[0] for n in goals]
    gy = [n[1] for n in goals]
    goal_box = (min(gx), min(gy), max(gx), max(gy))
    goal_layers = {n[2] for n in goals}

    def heuristic(node):
        x, y, layer = node
        dx = max(goal_box[0] - x, 0, x - goal_box[2])
        dy = max(goal_box[1] - y, 0, y - goal_box[3])
        distance = (max(dx, dy) + (math.sqrt(2) - 1) * min(dx, dy)
                    if options["diagonal"] else dx + dy)
        return distance * step * settings["alpha"] + (0 if layer in goal_layers else settings["via_cost"])

    directions = [(1, 0), (-1, 0), (0, 1), (0, -1)]
    if options["diagonal"]:
        directions += [(1, 1), (1, -1), (-1, 1), (-1, -1)]
    transitions = {}
    for layer in settings["layers"]:
        transitions[layer] = []
        # Generated ordinary vias use the same complete span as the native
        # writer. Existing evidenced partial-span copper remains in _groups.
        if settings["vias"] and set(stack).issubset(settings.get("via_layers", settings["layers"])):
            for other in settings["layers"]:
                if layer == other:
                    continue
                transitions[layer].append((other, stack))
    serial = itertools.count()
    queue, costs, previous, origins = [], {}, {}, {}
    for node, (path, length) in starts.items():
        if grid is not None and not grid.trace_free(node[2], node[0], node[1]):
            continue
        distance = length * settings["alpha"]
        costs[node] = distance
        origins[node] = path
        heapq.heappush(queue, (distance + heuristic(node), -distance, next(serial), node))
    edge_cache, via_cache = {}, {}
    expanded = 0
    found = None
    best = math.inf
    context["phase"] = "astar_search"
    while queue:
        budget.check()
        estimate, negative_cost, _, node = heapq.heappop(queue)
        cost = -negative_cost
        if cost != costs.get(node):
            continue
        if estimate >= best:
            break
        if node in goals:
            final_cost = cost + goals[node][1] * settings["alpha"]
            if final_cost < best:
                found, best = node, final_cost
            continue
        expanded += 1
        stats["expansions"] += 1
        context["expansions"] = expanded
        if expanded > limit:
            raise RoutingFailure("SEARCH_BUDGET", f"routing {net}: A* expansion budget exhausted ({limit})",
                                 **context, max_expansions=limit,
                                 interpretation="Node budget exhausted; geometry feasibility is undetermined.")
        x, y, layer = node
        pdx, pdy = 0.0, 0.0
        if node in previous and previous[node][1] is None:
            parent_node = previous[node][0]
            pdx, pdy = float(x - parent_node[0]), float(y - parent_node[1])
        elif len(origins.get(node, ())) >= 2:
            p0, p1 = origins[node][-2], origins[node][-1]
            pdx, pdy = p1[0] - p0[0], p1[1] - p0[1]
        plen = math.hypot(pdx, pdy)
        candidates = []
        for dx, dy in directions:
            target_node = (x + dx, y + dy, layer)
            if not (bounds[0] <= x + dx <= bounds[2] and bounds[1] <= y + dy <= bounds[3]):
                continue
            key = tuple(sorted((node, target_node)))
            if key not in edge_cache:
                if grid is None:
                    edge_cache[key] = model.trace_ok(net, layer, x * step, y * step,
                                                     (x + dx) * step, (y + dy) * step, settings["width"])
                else:
                    # 点查询只用于候选加速；跨边细障碍由 _commit 捕获。
                    edge_cache[key] = (grid.trace_free(layer, x, y)
                                       and grid.trace_free(layer, x + dx, y + dy))
            if edge_cache[key]:
                dlen = math.hypot(dx, dy)
                bend_penalty = 0.0
                if plen > 1e-9:
                    cos_t = (pdx * dx + pdy * dy) / (plen * dlen)
                    if cos_t > 0.99:
                        bend_penalty = 0.0
                    elif cos_t > 0.5:
                        bend_penalty = 0.02 * step * settings["alpha"]
                    elif cos_t > -0.1:
                        bend_penalty = (0.45 if options["diagonal"] else 0.02) * step * settings["alpha"]
                    else:
                        bend_penalty = 1.5 * step * settings["alpha"]
                candidates.append((target_node, dlen * step * settings["alpha"] + bend_penalty, None))
        if (x, y) not in forbidden_vias:
            for other, span in transitions[layer]:
                key = (x, y, span)
                if key not in via_cache:
                    via_cache[key] = (model.via_ok(net, x * step, y * step,
                                                   settings["hole"], settings["diameter"], span)
                                      if grid is None else grid.via_free(x, y))
                if via_cache[key]:
                    increment = settings["via_cost"] + _via_preference_cost(model, x * step, y * step, settings, span)
                    candidates.append(((x, y, other), increment, span))
        for target_node, increment, span in candidates:
            value = cost + increment
            if value < costs.get(target_node, math.inf):
                costs[target_node] = value
                previous[target_node] = (node, span)
                origins[target_node] = origins[node]
                heapq.heappush(queue, (value + heuristic(target_node), -value, next(serial), target_node))
    if found is None:
        raise RoutingFailure("GRID_NO_PATH", f"routing {net}: finite requested-grid graph has no legal path",
                             **context,
                             interpretation="Requested grid exhausted; off-grid paths and other grids are not ruled out.")
    nodes, spans = [found], []
    while nodes[-1] in previous:
        parent, span = previous[nodes[-1]]
        spans.append(span)
        nodes.append(parent)
    nodes.reverse()
    spans.reverse()
    traces, via_spans = [], {}

    def add_trace(layer, a, b):
        if a != b:
            traces.append(_trace(net, layer, a, b, settings["width"]))

    first, last = nodes[0], nodes[-1]
    for a, b in zip(origins[found], origins[found][1:]):
        add_trace(first[2], a, b)
    for a, b, span in zip(nodes, nodes[1:], spans):
        if span is None:
            add_trace(a[2], (a[0] * step, a[1] * step), (b[0] * step, b[1] * step))
        else:
            via_spans.setdefault((a[0], a[1]), set()).update(span)
    goal_path = list(reversed(goals[found][0]))
    for a, b in zip(goal_path, goal_path[1:]):
        add_trace(last[2], a, b)
    vias = []
    for (x, y), layers in via_spans.items():
        positions = [stack.index(layer) for layer in layers]
        span = list(stack[min(positions):max(positions) + 1])
        vias.append({"net": net, "x": x * step, "y": y * step,
                     "hole": settings["hole"], "diameter": settings["diameter"], "layers": span})
    return _postprocess_traces(model, net, traces, vias, options["diagonal"]), vias


def _trace_inside_new_drill(trace, vias):
    """Prove a new round-ended segment has no copper outside a new drill.

    A capsule lies in a disk iff both endpoint disks lie in it. Exact rational
    comparisons of the input numbers avoid expanding the drill near tangency.
    This only handles one covering circular drill; every other case retains the
    original segment for the ordinary complete collision and contact checks.
    """
    for via in vias:
        if trace["layer"] not in via["layers"]:
            continue
        radius = (Fraction(via["hole"]) - Fraction(trace["width"])) / 2
        if radius < 0:
            continue
        x, y = Fraction(via["x"]), Fraction(via["y"])
        if all((Fraction(px) - x) ** 2 + (Fraction(py) - y) ** 2 <= radius ** 2
               for px, py in ((trace["sx"], trace["sy"]), (trace["ex"], trace["ey"]))):
            return True
    return False


def _commit(board, net, traces, vias, before_count, budget, base_model=None, rejections=None):
    """按原精确规则检查并提交候选副本，不修改输入板。

    入参：完整板、网络、新铜、提交前铜组数、预算及可复用碰撞模型；
    rejections 可选，用于记录失败图元并指导栅格有限重搜。
    返回：实体铜组减少的完整候选板；几何或接触不合格时返回 None。
    异常：预算耗尽抛出 RoutingFailure；非法几何沿用原检查器的拒绝行为。
    """
    budget.context["phase"] = "physical_commit"
    budget.check()
    candidate = copy.deepcopy(board)
    # A via is inserted with its complete physical layer span in one operation.
    for i, via in enumerate(vias):
        budget.check()
        model = base_model if (i == 0 and base_model is not None) else CollisionModel(candidate)
        if (not model.via_ok(net, via["x"], via["y"], via["hole"], via["diameter"], tuple(via["layers"]))
                or (getattr(base_model, "forbidden", None) is not None
                    and not base_model.via_ok(net, via["x"], via["y"], via["hole"], via["diameter"], tuple(via["layers"])) )):
            if rejections is not None:
                rejections.append({"kind": "via", "item": via})
            return None
        candidate["routes"].setdefault("vias", []).append(via)
    # All proposed drills must be present when checking trace copper. Same-net
    # traces may touch one another, so one frozen model suffices for this batch.
    model = base_model if (not vias and base_model is not None) else CollisionModel(candidate)
    for trace in traces:
        budget.check()
        # A new via may consume a short fanout stub in its entirety. Omit only
        # this provably empty new segment, never input copper or a real remnant.
        if _trace_inside_new_drill(trace, vias):
            continue
        if (not model.trace_ok(net, trace["layer"], trace["sx"], trace["sy"],
                               trace["ex"], trace["ey"], trace["width"])
                or (getattr(base_model, "forbidden", None) is not None
                    and not base_model.trace_ok(net, trace["layer"], trace["sx"], trace["sy"],
                                                trace["ex"], trace["ey"], trace["width"]))):
            if rejections is not None:
                rejections.append({"kind": "trace", "item": trace})
            return None
        candidate["routes"].setdefault("traces", []).append(trace)
    if len(_groups(candidate, net, budget)) >= before_count:
        if rejections is not None:
            rejections.append({"kind": "contact"})
        return None
    budget.check()
    return candidate


def _grid_search_commit(board, net, source, target, settings, options, stats, budget, model, occupancy, before_count):
    """输入完整板和一对铜组，返回经过原 _commit 的候选副本。

    精检拒绝时标记失败格，并在 occupancy_retries 范围内重搜；搜索、
    空间、时间或精检重搜预算耗尽时抛出 RoutingFailure，不改输入板。
    结果仍须通过 check_board/check_routed_nets 以及实际写回后的原生 DRC。
    """
    forbidden_vias = set()
    limit = options.get("occupancy_retries", 3)
    for attempt in range(limit + 1):
        budget.check()
        traces, vias = _search(model, net, source, target, settings, options,
                              forbidden_vias, stats, budget, occupancy)
        rejections = []
        result = _commit(board, net, traces, vias, before_count, budget,
                         base_model=model, rejections=rejections)
        if result is not None:
            return result
        stats["occupancy_rejections"] = stats.get("occupancy_rejections", 0) + 1
        if attempt == limit:
            break
        occupancy.reject(rejections, traces, vias, model, net, settings, options, stats, budget)
        forbidden_vias.update((round(v["x"] / options["grid_step"]),
                               round(v["y"] / options["grid_step"])) for v in vias)
        stats["occupancy_retries"] = stats.get("occupancy_retries", 0) + 1
    raise RoutingFailure("GRID_REFINEMENT_BUDGET", "精检重搜预算内未找到合法候选，未证明无解",
                         **budget.context, occupancy_retries=limit,
                         interpretation="仅表示本次有限候选未通过实体铜检查；不改变设计规则或端子集合。")


def _route_net(board, net, settings, options, stats, budget):
    """输入完整板及单网规则，返回连通候选板；预算或几何失败抛出 PipelineError。

    旧顺序路由保持原行为；开启栅格后每次候选仍经精检，并按新增铜范围更新缓存。
    """
    occupancy = _GridSession() if options.get("occupancy_grid", False) else None
    budget.check()
    terminals = terminal_items(board, net)
    if not terminals:
        raise PipelineError(f"routing {net}: no declared terminal copper")
    first_terminal = tuple(terminals[0]["key"])
    deferred = set()
    while True:
        budget.context["phase"] = "copper_groups"
        budget.check()
        groups = _groups(board, net, budget)
        if len(groups) <= 1:
            return board
        sources = [group for group in groups if first_terminal in group["terminals"]]
        if not sources:
            raise PipelineError(f"routing {net}: anchor terminal has no real copper")
        source = sources[0]
        targets = [group for group in groups if group is not source]
        target_key = lambda group: tuple(sorted(group["terminals"]))
        distance = lambda a, b: min(x.distance(y) for x in a["layers"].values() for y in b["layers"].values())
        targets.sort(key=lambda target: (target_key(target) in deferred, distance(source, target)))
        model = CollisionModel(board)
        result = None
        failures = []
        # Cheap physical bridges need not involve the first terminal. Connect
        # nearby little islands before a blocked nearest target consumes the
        # entire search budget. Cap this heuristic's candidate count explicitly.
        if 2 < len(groups) <= 32:
            pairs = sorted(itertools.combinations(groups, 2), key=lambda pair: distance(*pair))[:64]
            for a, b in pairs:
                budget.context = _context(net, a, b, settings, options["grid_step"])
                direct = _direct(model, net, a, b, settings, options["diagonal"], budget)
                if direct is not None:
                    result = _commit(board, net, *direct, len(groups), budget, base_model=model)
                if result is not None:
                    break
            if result is not None:
                if occupancy is not None:
                    occupancy.committed(board, result)
                board = result
                stats["connections"] += 1
                continue
        for target in targets:
            budget.context = _context(net, source, target, settings, options["grid_step"])
            budget.check()
            direct = _direct(model, net, source, target, settings, options["diagonal"], budget)
            if direct is not None:
                result = _commit(board, net, *direct, len(groups), budget, base_model=model)
            if result is None and options.get("shove", {}).get("enabled"):
                from pcb_shove import shove_route
                result = shove_route(board, net, settings, options, stats, budget)
                if result is not None and occupancy is not None:
                    # 推挤移除了旧铜，不能沿用只处理追加铜的增量缓存。
                    occupancy = _GridSession()
                    stats["shove_occupancy_rebuilds"] = stats.get("shove_occupancy_rebuilds", 0) + 1
            if result is None:
                forbidden_vias = set()
                # A single remaining pair retains the full original budget.
                # Multiple targets share it; a timed-out target is tried after
                # untried targets on the next successful connection.
                pair_budget = (budget if len(targets) == 1 else _PairBudget(budget,
                               max(1.0, min(10.0, options.get("max_net_seconds", 30) / (len(targets) + 1)))))
                search_options = options
                # Newly proposed vias can conflict with one another. Re-search after
                # rejecting their sites; no relaxation of hole or copper clearance.
                if occupancy is not None:
                    try:
                        result = _grid_search_commit(board, net, source, target, settings, options,
                                                    stats, pair_budget, model, occupancy, len(groups))
                    except RoutingFailure as exc:
                        if exc.diagnostics["code"] in ("TIME_BUDGET", "OCCUPANCY_BUDGET"):
                            raise
                        if exc.diagnostics["code"] == "PAIR_TIME_BUDGET":
                            deferred.add(target_key(target))
                        failures.append(exc)
                for _ in range(0 if occupancy is not None else 4):
                    try:
                        traces, vias = _search(model, net, source, target, settings,
                                              search_options, forbidden_vias, stats, pair_budget)
                    except RoutingFailure as exc:
                        if exc.diagnostics["code"] == "TIME_BUDGET":
                            raise
                        if exc.diagnostics["code"] == "PAIR_TIME_BUDGET":
                            deferred.add(target_key(target))
                        failures.append(exc)
                        break
                    result = _commit(board, net, traces, vias, len(groups), budget, base_model=model)
                    if result is not None:
                        break
                    if search_options.get("geometry_search", {}).get("enabled") and not search_options.get("_skip_geometry"):
                        # 几何候选没有通过整批精检时，不反复提出同一路线，交给原网格/细化后备。
                        search_options = dict(options, _skip_geometry=True)
                        stats["geometry_exact_fallbacks"] = stats.get("geometry_exact_fallbacks", 0) + 1
                        if not vias:
                            continue
                    if not vias:
                        failures.append(RoutingFailure("PHYSICAL_CONTACT_FAILURE",
                                        f"routing {net}: proposed path failed physical copper contact",
                                        **budget.context))
                        break
                    forbidden_vias.update((round(v["x"] / options["grid_step"]),
                                           round(v["y"] / options["grid_step"])) for v in vias)
            if result is not None:
                if occupancy is not None:
                    occupancy.committed(board, result)
                board = result
                stats["connections"] += 1
                break
        if result is None and any(exc.diagnostics["code"] == "PAIR_TIME_BUDGET" for exc in failures):
            # The short fair pass is only a scheduling heuristic, never a new
            # feasibility cutoff. Let the first deferred pair use what remains
            # of the original net/whole-call budget before returning failure.
            target = next(target for target in targets if target_key(target) in deferred)
            budget.context = _context(net, source, target, settings, options["grid_step"])
            try:
                if occupancy is not None:
                    result = _grid_search_commit(board, net, source, target, settings, options,
                                                stats, budget, model, occupancy, len(groups))
                else:
                    traces, vias = _search(model, net, source, target, settings, options, set(), stats, budget)
                    result = _commit(board, net, traces, vias, len(groups), budget, base_model=model)
                if result is not None:
                    if occupancy is not None:
                        occupancy.committed(board, result)
                    board = result
                    stats["connections"] += 1
            except RoutingFailure as exc:
                if exc.diagnostics["code"] == "TIME_BUDGET":
                    raise
                failures.append(exc)
        if result is None:
            if failures:
                raise failures[-1]
            raise RoutingFailure("PHYSICAL_CONTACT_FAILURE", f"routing {net}: cannot join all copper islands",
                                 **budget.context)


class _ChainCollision:
    """For one ordered edge, keep every other same-net branch out of contact."""
    def __init__(self, board, net, source, target):
        self.base = CollisionModel(board)
        self.board, self.rules = self.base.board, self.base.rules
        copper, _ = copper_and_drills(board, net_filter=net)
        self.forbidden = {}
        allowed = {}
        for group in (source, target):
            for layer, geometry in group["layers"].items():
                allowed.setdefault(layer, []).append(geometry)
        for layer in self.rules.copper_layers:
            shapes = [item["inner"] for item in copper if item["net"] == net and layer in item["layers"]]
            geometry = unary_union(shapes)
            if layer in allowed:
                geometry = geometry.difference(unary_union(allowed[layer]))
            self.forbidden[layer] = geometry

    def via_in_pad(self, x, y, hole, layers):
        """转发孔洞软判定；入参、出参与异常同底层模型，不改变有序拓扑检查。"""
        return self.base.via_in_pad(x, y, hole, layers)

    def _clear(self, layers, shape):
        return all(not shape.intersects(self.forbidden[layer]) for layer in layers)

    def trace_ok(self, net, layer, sx, sy, ex, ey, width):
        if not self.base.trace_ok(net, layer, sx, sy, ex, ey, width):
            return False
        shape = trace_bounds(dict(sx=sx, sy=sy, ex=ex, ey=ey, width=width), self.rules.curve_tolerance)
        return self._clear((layer,), shape.outer)

    def via_ok(self, net, x, y, hole, diameter, layers):
        if not self.base.via_ok(net, x, y, hole, diameter, layers):
            return False
        shape, _ = via_bounds(dict(x=x, y=y, hole=hole, diameter=diameter), self.rules.curve_tolerance)
        return self._clear(layers, shape.outer)

    def explain_trace(self, net, layer, sx, sy, ex, ey, width):
        reason = self.base.explain_trace(net, layer, sx, sy, ex, ey, width)
        return reason or {"rule": "TOPOLOGY_ORDER", "kind": "same_net_branch",
                          "object": "other ordered terminal branch", "layers": [layer]}


def _route_ordered(board, net, ordered, settings, options, stats, budget):
    """输入完整板和有序端子，返回候选板；搜索及独立拓扑检查失败时抛出异常。

    各有序边的同网禁铜不同，因此各自建立网格，不跨边复用旧障碍。
    """
    keys = [(p["compId"], p["padId"]) for p in ordered]
    for a, b in zip(keys, keys[1:]):
        budget.check()
        groups = _groups(board, net, budget)
        if any(a in g["terminals"] and b in g["terminals"] for g in groups):
            continue
        copper, _ = copper_and_drills(board, net_filter=net)

        def terminal_group(key):
            layers = {}
            for item in copper:
                if item["net"] == net and item["terminal"] == key:
                    for layer in item["layers"]:
                        layers.setdefault(layer, []).append(item["inner"])
            return {"layers": {layer: unary_union(parts) for layer, parts in layers.items()}, "terminals": {key}}

        source, target = terminal_group(a), terminal_group(b)
        budget.context = _context(net, source, target, settings, options["grid_step"])
        model = _ChainCollision(board, net, source, target)
        direct = _direct(model, net, source, target, settings, options["diagonal"], budget)
        candidate = _commit(board, net, *direct, len(groups), budget) if direct is not None else None
        forbidden = set()
        search_options = options
        occupancy = _GridSession() if options.get("occupancy_grid", False) else None
        if candidate is None and occupancy is not None:
            candidate = _grid_search_commit(board, net, source, target, settings, options,
                                            stats, budget, model, occupancy, len(groups))
        for _ in range(0 if occupancy is not None else 4):
            if candidate is not None:
                break
            traces, vias = _search(model, net, source, target, settings, search_options, forbidden, stats, budget)
            candidate = _commit(board, net, traces, vias, len(groups), budget)
            if candidate is None:
                if search_options.get("geometry_search", {}).get("enabled") and not search_options.get("_skip_geometry"):
                    search_options = dict(options, _skip_geometry=True)
                    stats["geometry_exact_fallbacks"] = stats.get("geometry_exact_fallbacks", 0) + 1
                    if not vias:
                        continue
                if not vias:
                    break
                forbidden.update((round(v["x"] / options["grid_step"]),
                                  round(v["y"] / options["grid_step"])) for v in vias)
        if candidate is None:
            raise RoutingFailure("PHYSICAL_CONTACT_FAILURE", f"routing {net}: ordered edge {a}/{b} failed physical contact",
                                 **budget.context)
        board = candidate
        stats["connections"] += 1
    # Existing floating islands still require real anchoring; independent topology
    # acceptance below rejects any resulting bypass rather than deleting old copper.
    return _route_net(board, net, settings, options, stats, budget)


def deferred_ground_nets(board, options):
    """Native-plane workflows must explicitly name every deferred ground net."""
    if not isinstance(options, dict):
        raise PipelineError("routing options must be an object")
    strategy = options.get("ground_strategy", "route")
    if strategy not in ("route", "plane_then_drc"):
        raise PipelineError("routing ground_strategy must be route or plane_then_drc")
    if strategy == "route":
        return []
    grounds = options.get("ground_nets")
    if (not isinstance(grounds, list) or not grounds
            or not all(isinstance(n, str) for n in grounds)
            or len(grounds) != len(set(grounds)) or set(grounds) - set(board["nets"])):
        raise PipelineError("plane_then_drc requires explicit ground_nets from the complete netlist")
    for key in ("pours_before_net", "topology_orders"):
        if not isinstance(options.get(key, {}), dict):
            raise PipelineError(f"routing {key} must be an object")
    if set(grounds) & set(options.get("pours_before_net", {})):
        raise PipelineError("plane_then_drc uses native pours after signals, not pours_before_net")
    if set(grounds) & set(options.get("topology_orders", {})):
        raise PipelineError("a deferred native-plane ground cannot require routed topology_order")
    return [n for n in board["nets"] if n in grounds]


def check_routed_nets(board, nets):
    """Check all hard geometry and actual contact of the declared routed scope.

    The original netlist and copper stay intact. This is a stage check, never
    complete-board acceptance; pending ground still needs native pours and DRC.
    """
    errors = check_board(board, ignore_connectivity=True, defer_companions=True)
    from pcb_constraints import check_constraints
    errors += check_constraints(board, defer_companions=True)
    for net in nets:
        groups = _groups(board, net)
        declared = {(p["compId"], p["padId"]) for p in board["nets"][net]}
        terminals = set().union(*(g["terminals"] for g in groups)) if groups else set()
        if len(groups) != 1 or terminals != declared:
            errors.append(f"DISCONNECTED: {net} routed scope has {len(groups)} physical copper groups")
    return sorted(set(errors))


def algorithm_options(options=None):
    """校验算法开关并返回补齐默认值的选项副本，不执行搜索。

    入参：路由选项字典或 None。出参：保留原字段并补齐算法开关、
    栅格限额的新字典。未知字段、类型、限额或未实现方法抛 PipelineError。
    其余布线参数继续由原入口校验；此函数供整板与续算入口共同使用。
    """
    if options is None:
        options = {}
    if not isinstance(options, dict) or set(options) - _OPTIONS:
        raise PipelineError("routing options contain an unknown field or are not an object")
    options = dict(options)
    if "checkpoint" in options and type(options["checkpoint"]) is not bool:
        raise PipelineError("routing.checkpoint 必须为布尔值")
    if "local_repair" in options:
        from pcb_route_repair import validate_options as repair_options
        options["local_repair"] = repair_options(options["local_repair"])
    if "escape" in options:
        from pcb_escape import validate_options as escape_options
        options["escape"] = escape_options(options["escape"])
    # 新能力分别校验，关闭时保持旧候选路径及原默认方法。
    from pcb_geometric_router import validate_options as geometry_options
    from pcb_fanout import validate_options as fanout_options
    from pcb_shove import validate_options as shove_options
    from pcb_differential import validate_layer_change_options
    for key, validator in (("geometry_search", geometry_options), ("fanout", fanout_options),
                           ("shove", shove_options), ("diff_layer_change", validate_layer_change_options)):
        if key in options:
            options[key] = validator(options[key])
    method = options.get("method", "sequential")
    if method not in ("sequential", "negotiated"):
        raise PipelineError("routing.method 只能为 sequential 或 negotiated")
    options["method"] = method
    if method == "negotiated" and options.get("shove", {}).get("enabled"):
        raise PipelineError("本版级联推挤仅支持 sequential；协商候选的可移动铜身份尚未建立")
    options["via_in_pad_penalty"] = _number(options.get("via_in_pad_penalty", 0), "via_in_pad_penalty")
    options["negotiated_grid_cache"] = options.get("negotiated_grid_cache", False)
    if type(options["negotiated_grid_cache"]) is not bool:
        raise PipelineError("routing.negotiated_grid_cache 必须为布尔值")
    options["negotiated_bend_penalty"] = _number(options.get("negotiated_bend_penalty", 0), "negotiated_bend_penalty")
    if method != "negotiated" and (options["negotiated_grid_cache"] or options["negotiated_bend_penalty"]):
        raise PipelineError("协商缓存与转弯代价必须使用routing.method=negotiated")
    if "occupancy_grid" in options and type(options["occupancy_grid"]) is not bool:
        raise PipelineError("routing.occupancy_grid 必须为布尔值")
    if method == "negotiated" and options.get("occupancy_grid") is False:
        raise PipelineError("negotiated 依赖占用栅格，occupancy_grid 不可显式关闭")
    for key, default, low, high in (("occupancy_max_cells", 2000000, 1, 100000000),
                                    ("occupancy_retries", 3, 0, 20),
                                    ("negotiated_max_iterations", 20, 1, 1000),
                                    ("steiner_max_candidates", 128, 1, 10000),
                                    ("steiner_max_rounds", 2, 0, 100),
                                    ("diff_max_candidates", 128, 1, 4096),
                                    ("length_max_candidates", 128, 1, 4096)):
        value = options.get(key, default)
        if type(value) is not int or not low <= value <= high:
            raise PipelineError(f"routing.{key} 必须为 [{low},{high}] 内的整数")
        options[key] = value
    options["occupancy_max_seconds"] = _number(options.get("occupancy_max_seconds", 10.0),
                                                "occupancy_max_seconds", positive=True)
    options["length_max_amplitude"] = _number(options.get("length_max_amplitude", 200.0),
                                                "length_max_amplitude", positive=True)
    for key, default, minimum in (("negotiated_history_increment", 1.0, 0),
                                  ("negotiated_present_factor", 1.0, 0),
                                  ("negotiated_present_growth", 1.3, 1),
                                  ("nonpreferred_penalty", 1.5, 1)):
        options[key] = _number(options.get(key, default), key, minimum=minimum,
                               positive=minimum == 0)
    directions = options.get("preferred_direction", {})
    if not isinstance(directions, dict):
        raise PipelineError("routing.preferred_direction 必须为内层编号到方向的映射")
    normalized = {}
    for layer, direction in directions.items():
        if (not ((type(layer) is int and layer >= 0)
                 or (isinstance(layer, str) and len(layer) <= 10 and layer.isascii() and layer.isdecimal()
                     and str(int(layer)) == layer))
                or direction not in ("horizontal", "vertical")):
            raise PipelineError("preferred_direction 的层号须为整数或标准十进制字符串，方向须为 horizontal/vertical")
        key = str(layer)
        if key in normalized:
            raise PipelineError("preferred_direction 包含重复层号")
        normalized[key] = direction
    if method == "sequential" and normalized:
        raise PipelineError("preferred_direction 非空时须使用 routing.method=negotiated")
    options["preferred_direction"] = normalized
    options.setdefault("occupancy_grid", method == "negotiated")
    return options


def _preferred_layers(board, options):
    """校验优先方向的实际层号；入参为完整板与已规范化选项，返回 None。

    仅物理叠层中间的铜层可指定方向，未选为 trace_layers 的层不会因此启用。
    外层、未知层或不完整叠层引用抛 PipelineError，不修改输入。
    """
    inner = {str(layer) for layer in rules_for(board).copper_layers[1:-1]}
    unknown = set(options.get("preferred_direction", {})) - inner
    if unknown:
        raise PipelineError("preferred_direction 只能引用实际内层：" + ", ".join(sorted(unknown)))


def _validated_topology(board, options):
    """输入完整板和选项，返回已校验的有序端子映射；非法字段抛 PipelineError。

    保持原有覆盖规则：每个声明网络的排序必须包含且仅包含全部端子一次。
    此函数只校验配置；最终无旁路约束仍由 topology_errors 独立验收。
    """
    topology = options.get("topology_orders", {})
    if not isinstance(topology, dict) or set(topology) - set(board["nets"]):
        raise PipelineError("routing topology_orders must map declared nets to ordered pins")
    for net, ordered in topology.items():
        if not isinstance(ordered, list) or len(ordered) < 2:
            raise PipelineError("routing topology order needs at least two ordered pins")
        if any(not isinstance(p, dict) or set(p) != {"compId", "padId"}
               or not all(isinstance(v, str) for v in p.values()) for p in ordered):
            raise PipelineError("routing topology pins need compId/padId strings")
        keys = [(p["compId"], p["padId"]) for p in ordered]
        declared = [(p["compId"], p["padId"]) for p in board["nets"][net]]
        if len(keys) != len(set(keys)) or set(keys) != set(declared):
            raise PipelineError("routing topology order must cover exactly the declared net terminals")
    return topology


def _negotiated_batches(board, order, options, stats, budget, topology):
    """按原有铺铜依赖顺序运行协商批次，返回完整候选板，不修改输入。

    入参为已校验板、网络顺序、选项、统计及共享预算。普通网络一起协商；
    有 pours_before_net 的网络仍在前序网络完成后生成铜，再布该网。
    每批之前的铜均为固定输入，不跨依赖批次拆线。搜索或铺铜失败沿用
    RoutingFailure/PipelineError，所有批次共用总时限与各网络累计时限。
    """
    from pcb_negotiated import route_negotiated
    candidate = board
    pending = []
    for net in order:
        if net not in options.get("pours_before_net", {}):
            pending.append(net)
            continue
        if pending:
            candidate = route_negotiated(candidate, pending, options, stats, budget, topology)
            pending = []
        from pcb_copper import generate_pours
        budget.enter(net)
        try:
            budget.context["phase"] = "pours_before_net"
            candidate, pour_stats = generate_pours(candidate, options["pours_before_net"][net])
            stats.setdefault("pours_before_net", {})[net] = pour_stats
            budget.check()
        finally:
            budget.leave()
        candidate = route_negotiated(candidate, [net], options, stats, budget, topology)
    if pending:
        candidate = route_negotiated(candidate, pending, options, stats, budget, topology)
    return candidate


def _route_incremental(original, options, settings, stats, budget, order, deferred,
                       checkpoint_callback, resume_checkpoint):
    """输入已校验普通网任务，返回精检候选/统计；失败仍抛异常并保存独立续算工件。

    仅显式新选项走此路径；每个完整网络提交后才保存，恢复不重跑扇出/逃逸。
    pass_index 是下一次允许的外层尝试，失败后递增；总/逐网耗时、扩展统计、
    局部拆线次数均跨恢复累计。max_expansions 仍保持原有每次 A* 上限语义。
    """
    from pcb_routing_checkpoint import make_checkpoint, restore_checkpoint, check_increment
    from pcb_route_repair import repair_net
    candidate, completed, start_pass = copy.deepcopy(original), set(), 1
    stats['local_repair'] = dict(attempts=0, successes=0, events=[])
    stats['checkpoint'] = dict(enabled=options.get('checkpoint', False), resumed=False, saved=0)
    if resume_checkpoint is not None:
        candidate, completed, usage, start_pass = restore_checkpoint(original, options, resume_checkpoint, deferred)
        budget.started -= usage['elapsed_seconds']
        budget.spent = dict(usage['net_elapsed_seconds'])
        stats['expansions'] = usage['expansions']
        stats['local_repair']['attempts'] = usage['repair_attempts']
        stats['checkpoint']['resumed'] = True
        stats['checkpoint']['restored_seconds'] = usage['elapsed_seconds']
    def emit(pass_index, failure=None):
        if not options.get('checkpoint', False):
            return
        artifact = make_checkpoint(original, candidate, options, completed, deferred, budget, stats,
                                   pass_index, failure=failure)
        stats['checkpoint'].update(completed_nets=artifact['completed_nets'],
                                   pending_nets=artifact['pending_nets'], pass_index=pass_index)
        if checkpoint_callback is not None:
            try:
                checkpoint_callback(artifact)
            except Exception as exc:
                raise RoutingFailure('CHECKPOINT_WRITE_FAILURE', '计算检查点保存失败，停止而不重复派发',
                                     cause=str(exc)) from exc
            stats['checkpoint']['saved'] += 1

    if resume_checkpoint is not None:
        # 即使已耗尽，也先把刚复核的工件和累计预算交给父流程；它仍不是成功板。
        previous_failure = resume_checkpoint.get('failure') or {}
        emit(start_pass, previous_failure or None)
        if start_pass > options['max_passes']:
            budget.record(stats)
            raise RoutingFailure('CHECKPOINT_EXHAUSTED', '原检查点轮数预算已耗尽，未找到完整候选',
                                 stats=stats, net=previous_failure.get('net'), failure_context=previous_failure)
        try:
            budget.check()
        except RoutingFailure as exc:
            budget.record(stats)
            exc.diagnostics.update(stats=copy.deepcopy(stats), net=previous_failure.get('net'),
                                   failure_context=previous_failure)
            raise
    else:
        budget.check()
        if options.get('fanout', {}).get('enabled'):
            from pcb_fanout import fanout_board
            candidate = fanout_board(candidate, options, stats, budget, excluded_nets=set(deferred))
        if options.get('escape', {}).get('enabled'):
            from pcb_escape import escape_board
            candidate = escape_board(candidate, options, stats, budget, excluded_nets=set(deferred))
        emit(start_pass)
    last_error = None
    for attempt in range(start_pass - 1, options['max_passes']):
        stats['passes'] = attempt + 1
        failed_net = None
        try:
            for net in order:
                if net in completed:
                    continue
                failed_net = net
                try:
                    budget.enter(net)
                    candidate = _route_net(candidate, net, settings[net], options, stats, budget)
                    budget.check()
                except RoutingFailure as exc:
                    budget.leave()
                    if exc.diagnostics['code'] in ('TIME_BUDGET', 'GEOMETRY_HARD_FAILURE', 'OCCUPANCY_BUDGET'):
                        raise
                    repaired = repair_net(original, candidate, net, completed, options, settings,
                                          stats, budget, excluded_nets=deferred)
                    if repaired is None:
                        raise
                    candidate = repaired
                finally:
                    budget.leave()
                # 即使 callback 关闭，局部修复也不得跳过完整已完成网和硬几何复核。
                check_increment(original, candidate)
                errors = check_routed_nets(candidate, sorted(completed | {net}))
                if errors:
                    raise RoutingFailure('GEOMETRY_HARD_FAILURE', '增量网络提交未通过独立复核', errors=errors[:8])
                completed.add(net)
                emit(attempt + 1)
                budget.check()
            errors = (check_routed_nets(candidate, order) if deferred else
                      check_board(candidate, defer_companions=True))
            budget.check()
            if errors:
                raise RoutingFailure('GEOMETRY_HARD_FAILURE', '增量路由最终独立检查失败', errors=errors[:8])
            for key in ('traces', 'vias'):
                stats['added_' + key] = len(candidate['routes'].get(key, [])) - len(original['routes'].get(key, []))
            if options['via_in_pad_penalty']:
                stats['via_in_pad'] = via_in_pad_report(candidate,
                    candidate['routes'].get('vias', [])[len(original['routes'].get('vias', [])):],
                    options['via_in_pad_penalty'])
            budget.record(stats)
            return candidate, stats
        except PipelineError as exc:
            budget.leave()
            last_error = exc
            code = exc.diagnostics['code'] if isinstance(exc, RoutingFailure) else 'ROUTING_FAILURE'
            failure = dict(net=failed_net, code=code, message=str(exc))
            if failed_net:
                shapes = [item['inner'] for item in terminal_items(candidate, failed_net)]
                if shapes:
                    failure['bounds'] = list(unary_union(shapes).bounds)
            events = stats['local_repair']['events']
            if events:
                failure['local_repair'] = copy.deepcopy(events[-1])
            if code != 'CHECKPOINT_WRITE_FAILURE':
                emit(attempt + 2, failure)
            budget.record(stats)
            if isinstance(exc, RoutingFailure):
                exc.diagnostics.update(stats=copy.deepcopy(stats), failed_net=failed_net, net=failed_net,
                                       failure_context=failure, bounded_passes=options['max_passes'])
            if code in ('TIME_BUDGET', 'GEOMETRY_HARD_FAILURE', 'OCCUPANCY_BUDGET', 'CHECKPOINT_WRITE_FAILURE'):
                raise
    if isinstance(last_error, RoutingFailure):
        raise last_error
    raise PipelineError(f"增量路由在 {options['max_passes']} 轮预算内未找到完整候选：{last_error}")


def route_board(board, options=None, *, checkpoint_callback=None, resume_checkpoint=None):
    """输入完整板和路由选项，返回经独立检查的候选副本及统计，不修改输入。

    非法参数抛 PipelineError；节点、时间、栅格预算或几何失败抛 RoutingFailure。
    max_seconds 覆盖整次调用，max_net_seconds 累计同网各轮，max_expansions
    限制每次 A*。单次 GEOS 或检查器调用不能被合作式时限强行抢占。
    有限搜索失败不证明无解，所有原始端子仍须保留。
    trace_layers 只限制新增走线；通孔按完整物理叠层检查，allowed_layers
    仍约束全部网络铜。plane_then_drc 的信号候选不代表整板完成。
    checkpoint_callback 接收独立计算工件；resume_checkpoint 必须匹配同源基线。
    二者仅在 checkpoint=true 时启用，恢复后累计耗时与修复次数不重置。
    """
    options = algorithm_options(options)
    method = options["method"]
    for key in ("enabled", "allow_vias", "diagonal"):
        if key in options and type(options[key]) is not bool:
            raise PipelineError(f"routing {key} must be boolean")
    for key, default in (("max_expansions", 50000), ("max_passes", 3)):
        value = options.get(key, default)
        if type(value) is not int or value < 1:
            raise PipelineError(f"routing {key} must be a positive integer")
        options[key] = value
    for key, default in (("max_seconds", 120.0), ("max_net_seconds", 30.0)):
        options[key] = _number(options.get(key, default), key, positive=True)
    budget = _Budget(options)
    original = normalize_board(board)
    from pcb_differential import declarations, construction_view, route_pairs, match_lengths, quality_report
    signal_constraints = copy.deepcopy(original.get("metadata", {}).get("pipeline_constraints", []))
    stack = tuple(rules_for(original).copper_layers)
    trace_layers = _trace_layers(original, options)
    options["trace_layers"] = trace_layers
    _preferred_layers(original, options)
    deferred = deferred_ground_nets(original, options)
    budget.check()
    errors = check_board(original, ignore_connectivity=True)
    budget.check()
    if errors:
        raise RoutingFailure("GEOMETRY_HARD_FAILURE", "routing input violates independently checked geometry",
                             errors=errors[:8])
    special_pairs, length_groups = declarations(signal_constraints)
    signal_quality_requested = bool(special_pairs or length_groups)
    options["grid_step"] = _number(options.get("grid_step", rules_for(original).grid_step), "grid_step", positive=True)
    options.setdefault("diagonal", True)
    order = options.get("net_order", list(original["nets"]))
    if (not isinstance(order, list) or not all(isinstance(n, str) for n in order)
            or len(set(order)) != len(order) or set(order) - set(original["nets"])):
        raise PipelineError("routing net_order must be a unique list of declared nets")
    order = order + [net for net in original["nets"] if net not in order]
    order = [net for net in order if net not in deferred]
    strategy = options.get("net_order_strategy", "fewest_terminals")
    if strategy not in ("fewest_terminals", "ground_last", "input"):
        raise PipelineError("routing net_order_strategy must be fewest_terminals, ground_last, or input")
    grounds = options.get("ground_nets", [])
    if (not isinstance(grounds, list) or not all(isinstance(n, str) for n in grounds)
            or len(grounds) != len(set(grounds)) or set(grounds) - set(original["nets"])):
        raise PipelineError("routing ground_nets must be a unique list of declared nets")
    grounds = set(grounds) | {net for net in order
                            if original.get("tags", {}).get("net_tags", {}).get(net, {}).get("type") == "GND"}
    if strategy != "input" and "net_order" not in options:
        order.sort(key=lambda net: len(original["nets"][net]))
    if strategy == "ground_last":
        # Explicit strategy is applied after explicit net_order, also on retries.
        order.sort(key=lambda net: net in grounds)
    pours_before_net = options.get("pours_before_net", {})
    if (not isinstance(pours_before_net, dict)
            or set(pours_before_net) - set(original["nets"])):
        raise PipelineError("routing pours_before_net must map declared nets to pour requests")
    for net, requests in pours_before_net.items():
        if (not isinstance(requests, list) or not requests
                or any(not isinstance(request, dict) or request.get("net") != net for request in requests)):
            raise PipelineError("routing pours_before_net needs nonempty pour lists whose net matches the key")
    order.sort(key=lambda net: net in pours_before_net)
    topology = _validated_topology(original, options)
    incremental = options.get("checkpoint", False) or options.get("local_repair", {}).get("enabled", False)
    if checkpoint_callback is not None and not callable(checkpoint_callback):
        raise PipelineError("checkpoint_callback 必须为函数或 None")
    if (checkpoint_callback is not None or resume_checkpoint is not None) and not options.get("checkpoint", False):
        raise PipelineError("检查点回调/恢复需显式启用 routing.checkpoint")
    metadata_topology = any(item.get('operator') == 'TOPOLOGY_ORDER' for item in signal_constraints)
    if incremental and (method != "sequential" or signal_quality_requested or topology or metadata_topology
                        or pours_before_net or options.get("shove", {}).get("enabled")):
        raise PipelineError("增量检查点/局部拆线暂仅支持 sequential 普通网络；差分、等长、有序拓扑、网前铺铜和推挤仍走旧流程")
    if options.get("shove", {}).get("enabled"):
        from pcb_shove import validate_options as validate_shove
        options["shove"] = validate_shove(options["shove"], original)
        protected_nets = set(deferred) | set(topology)
        protected_nets.update(net for pair in special_pairs for net in (pair["p"], pair["n"]))
        protected_nets.update(net for group in length_groups for net in group["nets"])
        options["_shove_original_traces"] = copy.deepcopy(original["routes"].get("traces", []))
        options["_shove_protected_nets"] = sorted(protected_nets)
        movable = set(options["shove"]["movable_trace_ids"])
        if any(t.get("id") in movable and t["net"] in protected_nets for t in original["routes"].get("traces", [])):
            raise PipelineError("推挤白名单包含差分、等长、有序拓扑或暂缓地网，当前版本保护这些网络")
    settings = {net: _net_options(original, net, options) for net in order}
    stats = {"enabled": options.get("enabled", True), "passes": 0, "expansions": 0,
             "connections": 0, "added_traces": 0, "added_vias": 0, "net_order": order,
             "searches": 0, "net_order_strategy": strategy,
             "ground_strategy": options.get("ground_strategy", "route"),
             "deferred_ground_nets": deferred,
             "trace_layers": list(trace_layers), "through_via_layers": list(stack),
             "max_seconds": options["max_seconds"], "max_net_seconds": options["max_net_seconds"],
             "method": method, "occupancy_grid": options["occupancy_grid"],
             "search_seconds": 0.0, "astar_seconds": 0.0,
             "occupancy_build_seconds": 0.0, "occupancy_update_seconds": 0.0,
             "occupancy_cells": 0, "occupancy_updated_cells": 0,
             "occupancy_retries": 0, "occupancy_rejections": 0}
    if not stats["enabled"]:
        if resume_checkpoint is not None:
            raise PipelineError("routing.enabled=false 时不能恢复路由检查点")
        budget.record(stats)
        return original, stats
    if incremental:
        return _route_incremental(original, options, settings, stats, budget, order, deferred,
                                  checkpoint_callback, resume_checkpoint)
    last_error = None
    for attempt in range(1 if method == "negotiated" else options["max_passes"]):
        if attempt and "shove" in stats:
            stats.setdefault("shove_discarded_passes", []).append({"pass": attempt, "evidence": stats.pop("shove")})
        # 完成态差分/等长仅在局部构造视图中暂缓；全部原网表与硬几何保留。
        candidate = construction_view(original) if signal_quality_requested else copy.deepcopy(original)
        stats["passes"] = attempt + 1
        stats["connections"] = 0
        if pours_before_net:
            stats["pours_before_net"] = {}
        failed_net = None
        try:
            handled = set()
            if options.get("fanout", {}).get("enabled"):
                from pcb_fanout import fanout_board
                excluded = set(deferred) | set(topology)
                excluded.update(net for pair in special_pairs for net in (pair["p"], pair["n"]))
                excluded.update(net for group in length_groups for net in group["nets"])
                candidate = fanout_board(candidate, options, stats, budget, excluded_nets=excluded)
            if options.get("escape", {}).get("enabled"):
                from pcb_escape import escape_board
                excluded = set(deferred) | set(topology)
                excluded.update(net for pair in special_pairs for net in (pair["p"], pair["n"]))
                excluded.update(net for group in length_groups for net in group["nets"])
                candidate = escape_board(candidate, options, stats, budget, excluded_nets=excluded)
            if signal_quality_requested:
                candidate, handled = route_pairs(candidate, signal_constraints, options, stats, budget, topology)
            ordinary_order = [net for net in order if net not in handled]
            if method == "negotiated":
                candidate = _negotiated_batches(candidate, ordinary_order, options, stats, budget, topology)
            for net in (() if method == "negotiated" else ordinary_order):
                failed_net = net
                budget.enter(net)
                if net in pours_before_net:
                    from pcb_copper import generate_pours
                    budget.context["phase"] = "pours_before_net"
                    candidate, pour_stats = generate_pours(candidate, pours_before_net[net])
                    stats["pours_before_net"][net] = pour_stats
                    budget.check()
                if net in topology:
                    candidate = _route_ordered(candidate, net, topology[net], settings[net], options, stats, budget)
                else:
                    candidate = _route_net(candidate, net, settings[net], options, stats, budget)
                budget.check()
                budget.leave()
            if signal_quality_requested:
                candidate = match_lengths(candidate, original, signal_constraints, options, stats, budget)
                candidate.setdefault("metadata", {})["pipeline_constraints"] = copy.deepcopy(signal_constraints)
                stats["signal_quality"] = quality_report(candidate, signal_constraints)
                if not stats["signal_quality"]["passed"]:
                    raise RoutingFailure("SIGNAL_QUALITY_FAILURE", "差分/等长候选未通过实际几何复核",
                                         errors=stats["signal_quality"]["errors"])
            budget.check()
            if topology:
                from pcb_constraints import topology_errors
                topology_failures = [error for net, pins in topology.items()
                                     for error in topology_errors(candidate, net, pins)]
                if topology_failures:
                    raise PipelineError("; ".join(topology_failures))
            # A companion fence is generated in the following stage. Keep full
            # physical connectivity and topology acceptance here; defer only the
            # not-yet-generated companion count, which final pipeline acceptance
            # must check after its generation and ground anchoring.
            budget.context = {"phase": "final_verification"}
            errors = (check_routed_nets(candidate, order) if deferred else
                      check_board(candidate, defer_companions=True))
            budget.check()
            if errors:
                raise RoutingFailure("GEOMETRY_HARD_FAILURE", "routing final independent verification failed",
                                     errors=errors[:8])
            stats["added_traces"] = len(candidate["routes"].get("traces", [])) - len(original["routes"].get("traces", []))
            stats["added_vias"] = len(candidate["routes"].get("vias", [])) - len(original["routes"].get("vias", []))
            stats["net_order"] = list(order)
            if options["via_in_pad_penalty"]:
                stats["via_in_pad"] = via_in_pad_report(candidate,
                    candidate["routes"].get("vias", [])[len(original["routes"].get("vias", [])):],
                    options["via_in_pad_penalty"])
            budget.record(stats)
            return candidate, stats
        except PipelineError as exc:
            last_error = exc
            budget.record(stats)
            budget.leave()
            if isinstance(exc, RoutingFailure):
                exc.diagnostics["stats"] = copy.deepcopy(stats)
                if exc.diagnostics["code"] in ("TIME_BUDGET", "GEOMETRY_HARD_FAILURE", "OCCUPANCY_BUDGET"):
                    raise
            if method == "negotiated":
                # 协商内部已按显式轮数重布，不叠加顺序算法的外层重试。
                raise
            if len(order) > 1:
                # Prioritize the blocked net, alternating the remaining ordering.
                remaining = [net for net in order if net != failed_net]
                order = ([failed_net] if failed_net else []) + list(reversed(remaining))
                if strategy == "ground_last":
                    order.sort(key=lambda net: net in grounds)
                order.sort(key=lambda net: net in pours_before_net)
    if isinstance(last_error, RoutingFailure):
        last_error.diagnostics["bounded_passes"] = options["max_passes"]
        raise last_error
    raise PipelineError(f"routing failed after {options['max_passes']} bounded pass(es): {last_error}")
