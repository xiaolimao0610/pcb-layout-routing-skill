"""Shared geometry for construction. Final acceptance remains a separate pass."""
from __future__ import annotations

from copy import deepcopy
from functools import lru_cache
import hashlib
import json
import math

from shapely.geometry import Point, box
from shapely.ops import unary_union
from shapely.prepared import prep
from shapely.strtree import STRtree

from pcb_geometry import shape_from_spec, solid_polygon, transform, trace_bounds, via_bounds, filled_region_bounds
from pcb_solver_hardened import (
    RuleConfig, normalize_raw_pcb_data, verify_full_board_drc_and_connectivity,
    _body, _pad_bounds, _pad_drill_bounds, _layers, sensitive_halo_targets,
)


class PipelineError(ValueError):
    pass


def strict_options(options, allowed, label):
    if options is None:
        return {}
    if not isinstance(options, dict):
        raise PipelineError(f"{label} must be an object")
    unknown = set(options) - set(allowed)
    if unknown:
        raise PipelineError(f"unsupported {label}: {sorted(unknown)}")
    return deepcopy(options)


def finite(value, name, minimum=None, positive=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PipelineError(f"{name} must be a finite number")
    try:
        value = float(value)
    except (ValueError, OverflowError) as exc:
        raise PipelineError(f"{name} must be finite") from exc
    if not math.isfinite(value):
        raise PipelineError(f"{name} must be a finite number")
    if (positive and value <= 0) or (minimum is not None and value < minimum):
        raise PipelineError(f"invalid {name}: {value}")
    return float(value)


def rules_for(board):
    overrides = strict_options(board.get("rules", {}), RuleConfig.__dataclass_fields__, "rules")
    if "clearance_matrix" in overrides:
        rows, result, seen = overrides["clearance_matrix"], {}, set()
        if not isinstance(rows, list):
            raise PipelineError("JSON clearance_matrix must be a list of [type,type,gap]")
        for row in rows:
            if not isinstance(row, (list, tuple)) or len(row) != 3 or not all(isinstance(v, str) for v in row[:2]):
                raise PipelineError("invalid clearance matrix row")
            pair = tuple(sorted(row[:2]))
            if pair in seen:
                raise PipelineError(f"duplicate/ambiguous clearance pair {pair}")
            seen.add(pair)
            result[tuple(row[:2])] = finite(row[2], "clearance", minimum=0)
        overrides["clearance_matrix"] = result
    try:
        rules = RuleConfig(**overrides)
        rules.validate()
        return rules
    except (TypeError, ValueError) as exc:
        raise PipelineError(str(exc)) from exc


def normalize_board(board):
    """校验并返回独立的规范化整板，不修改输入。

    入参为完整 mil 工程；出参统一缺省的空器件/网络标签映射，使后续布局、
    布线与来源复核比较同一表示。真实标签保持原值；非法类型、未知字段和
    不完整网表仍交原检查器拒绝并抛 PipelineError，不放宽候选验收。
    """
    if not isinstance(board, dict):
        raise PipelineError("board must be an object")
    known = {"units", "board_bbox", "components", "nets", "tags", "routes", "rules", "metadata"}
    if set(board) - known:
        raise PipelineError(f"unknown board fields: {sorted(set(board) - known)}")
    if board.get("units") != "mil":
        raise PipelineError("board.units must explicitly be mil; convert the source first")
    try:
        out = normalize_raw_pcb_data(board)
        out.setdefault("routes", {})
        out.setdefault("tags", {})
        if not isinstance(out.get("nets"), dict):
            raise PipelineError("board.nets must be an object")
        rules = rules_for(out)
        board_polygon(out)
        _, errors = verify_full_board_drc_and_connectivity(
            {c["id"]: c for c in out["components"]}, out["routes"], out["nets"],
            out["tags"], out["board_bbox"], rules,
        )
        invalid = [e for e in errors if e.startswith(("INVALID_INPUT:", "MISSING_TERMINAL:",
                   "DUPLICATE_TERMINAL:", "NET_MISMATCH:", "NETLIST_INCOMPLETE:"))]
        if invalid:
            raise PipelineError("; ".join(invalid))
        # 先由原检查器验证类型和字段，再统一等价空结构，不能掩盖非法标签。
        out["tags"].setdefault("component_tags", {})
        out["tags"].setdefault("net_tags", {})
        return out
    except (ValueError, TypeError, KeyError, AttributeError, IndexError) as exc:
        if isinstance(exc, PipelineError):
            raise
        raise PipelineError(f"invalid board: {exc}") from exc


def check_board(board, ignore_connectivity=False, *, defer_companions=False, geometry_context=None):
    """输入完整板和可选铺铜补证上下文，返回完整错误列表；非法输入记为 INVALID_INPUT。

    上下文仅改变已证明的铺铜内界，不认证其他对象的原生来源，也不替代原生验收。
    ignore_connectivity 与 defer_companions 保持原有构造阶段语义。
    """
    try:
        if geometry_context is not None:
            from pcb_gerber_evidence import require_context
            require_context(geometry_context, board)
        _, errors = verify_full_board_drc_and_connectivity(
            {c["id"]: c for c in board["components"]}, board.get("routes", {}), board["nets"],
            board.get("tags", {}), board["board_bbox"], rules_for(board), geometry_context=geometry_context,
        )
    except (ValueError, TypeError, KeyError) as exc:
        return [f"INVALID_INPUT: {exc}"]
    # Pipeline-only rules live alongside canonical geometry, not in the legacy
    # standalone validator. Construction permits missing partial-board refs.
    if board.get("metadata", {}).get("pipeline_constraints"):
        from pcb_constraints import check_constraints
        errors += check_constraints(board, construction=ignore_connectivity,
                                    defer_companions=defer_companions)
    if ignore_connectivity:
        construction = ("DISCONNECTED:", "FLOATING_COPPER:", "UNANCHORED_NET:", "TERMINAL_COPPER:")
        return [e for e in errors if not e.startswith(construction)]
    return errors


def board_polygon(board):
    boundary = board["board_bbox"]
    if isinstance(boundary, dict):
        return solid_polygon(boundary["outline"], boundary.get("cutouts", []))
    if not isinstance(boundary, (list, tuple)) or len(boundary) != 4:
        raise PipelineError("board_bbox must be rectangle or outline/cutouts")
    x0, y0, x1, y1 = [finite(v, "board coordinate") for v in boundary]
    if x0 >= x1 or y0 >= y1:
        raise PipelineError("empty board rectangle")
    return solid_polygon([(x0, y0), (x1, y0), (x1, y1), (x0, y1)])


@lru_cache(maxsize=32768)
def _cached_pad_bounds_key(cx, cy, crot, clayer, lx, ly, lrot, shape, lw, w, lh, h, d, r, cr, tol):
    c = {"x": cx, "y": cy, "rotation": crot, "layer": clayer}
    p = {"localX": lx, "localY": ly, "local_rotation": lrot, "shape": shape}
    if lw is not None:
        p["localW"] = lw
    if w is not None:
        p["width"] = w
    if lh is not None:
        p["localH"] = lh
    if h is not None:
        p["height"] = h
    if d is not None:
        p["diameter"] = d
    if r is not None:
        p["radius"] = r
    if cr is not None:
        p["corner_radius"] = cr
    return _pad_bounds(c, p, tol)


def _fast_pad_bounds(c, p, tolerance):
    if ("points" not in p and "holes" not in p and "pad" not in p
            and type(tolerance) is float and type(c.get("x")) is float and type(c.get("y")) is float):
        try:
            return _cached_pad_bounds_key(
                c["x"], c["y"], c["rotation"], c["layer"],
                p["localX"], p["localY"], p["local_rotation"], p.get("shape"),
                p.get("localW"), p.get("width"), p.get("localH"), p.get("height"),
                p.get("diameter"), p.get("radius"), p.get("corner_radius"), tolerance,
            )
        except Exception:
            pass
    return _pad_bounds(c, p, tolerance)


@lru_cache(maxsize=32768)
def _cached_trace_bounds_key(sx, sy, ex, ey, width, tol):
    return trace_bounds({"sx": sx, "sy": sy, "ex": ex, "ey": ey, "width": width}, tol)


def _fast_trace_bounds(t, tolerance):
    if type(tolerance) is float and all(type(t.get(k)) in (int, float) for k in ("sx", "sy", "ex", "ey", "width")):
        try:
            return _cached_trace_bounds_key(
                float(t["sx"]), float(t["sy"]), float(t["ex"]), float(t["ey"]), float(t["width"]), tolerance,
            )
        except Exception:
            pass
    return trace_bounds(t, tolerance)


def attached_vias_for_components(board, components, *, subset=False):
    """Move proven footprint vias only with their parent, on the same PCB side."""
    original = {c["id"]: c for c in board["components"]}
    target = {c["id"]: c for c in components}
    result = []
    for via in board.get("routes", {}).get("vias", []):
        meta = via.get("metadata", {})
        cid = meta.get("parent_component_id")
        if cid is None:
            result.append(deepcopy(via))
            continue
        parent = original.get(cid)
        if parent is None or via.get("id") not in parent.get("metadata", {}).get("attached_via_ids", []):
            raise PipelineError("attached via ownership differs from its parent")
        if cid not in target:
            if subset:
                continue
            raise PipelineError("attached via parent is missing from target")
        moved = any(parent[k] != target[cid][k] for k in ("x", "y", "rotation", "layer"))
        declaration = board.get("tags", {}).get("component_tags", {}).get(cid, {})
        if moved and (parent.get("metadata", {}).get("footprint_via_movement") != "rigid"
                      or parent.get("fixed") or parent.get("locked") or via.get("locked")
                      or declaration.get("fixed") or "fixed_pose" in declaration):
            raise PipelineError("attached via parent remains fixed")
        if target[cid]["layer"] != parent["layer"]:
            raise PipelineError("attached via side changes are not verified")
        values = [meta.get(k) for k in ("source_local_x", "source_local_y", "source_hole_diameter", "source_copper_diameter")]
        if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in values):
            raise PipelineError("attached via needs finite native footprint geometry")
        if via["hole"] != values[2] or via["diameter"] != values[3]:
            raise PipelineError("attached via dimensions differ from native footprint")
        def position(component):
            angle = component["rotation"] * math.pi / 180
            x, y = values[0] * (-1 if component["layer"] == 2 else 1), values[1]
            return (component["x"] + x * math.cos(angle) - y * math.sin(angle),
                    component["y"] + x * math.sin(angle) + y * math.cos(angle))
        if any(not math.isclose(via[k], value, rel_tol=4e-15, abs_tol=4e-15)
               for k, value in zip(("x", "y"), position(parent))):
            raise PipelineError("attached via source position differs from native footprint")
        expected = deepcopy(via)
        if moved:
            expected["x"], expected["y"] = position(target[cid])
        result.append(expected)
    return result


def copper_and_drills(board, *, stats=None, net_filter=None, geometry_context=None):
    """输入完整板、可选统计/网筛选及铺铜上下文，返回扣除真实孔洞后的逐层铜和孔。

    保留所有铜岛；输入几何非法或补证上下文过期时抛 ValueError 子类异常。
    net_filter 仅筛铜，所有会影响该铜的实际钻孔仍参与计算。
    """
    if geometry_context is not None:
        from pcb_gerber_evidence import require_context
        require_context(geometry_context, board)
    rules = rules_for(board)
    tolerance = rules.curve_tolerance
    copper, drills = [], []

    def add(name, net, layers, shape, terminal=None, barrel=None):
        copper.append(dict(name=name, net=net, layers=tuple(layers), inner=shape.inner,
                           outer=shape.outer, terminal=terminal, barrel=barrel))

    def drill(name, net, layers, shape, plated):
        drills.append(dict(name=name, net=net, layers=tuple(layers), inner=shape.inner,
                           outer=shape.outer, plated=plated))

    for c in board["components"]:
        for p in c["pads"]:
            has_hole = p.get("hole") is not None and p["hole"] != 0
            want_copper = net_filter is None or p["net"] == net_filter
            if not has_hole and not want_copper:
                continue
            name = f"pad {c['id']}.{p['padId']}"
            layers = _layers(p, c["layer"], rules, spanning=has_hole)
            barrel = None
            if has_hole:
                shape, _ = _pad_drill_bounds(c, p, tolerance)
                drill(name, p["net"], layers, shape, p["plated"])
                if not p["plated"] or not want_copper:
                    continue
                if len(layers) > 1:
                    barrel = name
            add(name, p["net"], layers, _fast_pad_bounds(c, p, tolerance),
                (c["id"], p["padId"]), barrel)
    routes = board.get("routes", {})
    for i, t in enumerate(routes.get("traces", [])):
        if net_filter is not None and t["net"] != net_filter:
            continue
        add(f"trace {i}", t["net"], _layers(t, None, rules), _fast_trace_bounds(t, tolerance))
    seen = set()
    for i, v in enumerate(routes.get("vias", []) + routes.get("shield_vias", [])):
        layers = _layers(v, 12, rules, spanning=True)
        key = (v["net"], v["x"], v["y"], v["hole"], v["diameter"], layers)
        if key in seen:
            continue
        seen.add(key)
        shape, void = via_bounds(v, tolerance)
        name = f"via {i}"
        if net_filter is None or v["net"] == net_filter:
            add(name, v["net"], layers, shape, barrel=name)
        drill(name, v["net"], layers, void, True)
    for i, h in enumerate(routes.get("holes", [])):
        shape = transform(shape_from_spec({"type": "CIRCLE", "diameter": h["diameter"]}, tolerance),
                          h["x"], h["y"], 0)
        drill(f"NPTH {i}", "", _layers(h, 12, rules, spanning=True), shape, False)
    for i, pour in enumerate(routes.get("copper_pours", [])):
        if net_filter is not None and pour["net"] != net_filter:
            continue
        for j, region in enumerate(pour["filled_polygons"]):
            shape = (filled_region_bounds(region, tolerance) if geometry_context is None else
                     geometry_context.region_bounds(pour, j, region, tolerance))
            add(f"pour {i}.{j}", pour["net"], _layers(pour, None, rules), shape)
    drill_ids = {}
    for i, hole in enumerate(drills):
        for layer in hole["layers"]:
            drill_ids.setdefault(layer, []).append(i)
    drill_trees = {layer: STRtree([drills[i]["outer"] for i in ids])
                   for layer, ids in drill_ids.items()}
    if stats is not None:
        stats.update(copper_layer_records=0, drill_objects=len(drills),
                     layer_drill_pairs=0, candidate_drills=0, difference_calls=0)
    finished = []
    for item in copper:
        for layer in item["layers"]:
            inner, outer = item["inner"], item["outer"]
            ids = drill_ids.get(layer, ())
            candidates = []
            bounds = [shape.bounds for shape in (inner, outer) if not shape.is_empty]
            if ids and bounds:
                # Query both certified geometries. A drill outer (including a
                # rotated slot or a coarse curve) encloses its cutting inner.
                query = box(math.nextafter(min(b[0] for b in bounds), -math.inf),
                            math.nextafter(min(b[1] for b in bounds), -math.inf),
                            math.nextafter(max(b[2] for b in bounds), math.inf),
                            math.nextafter(max(b[3] for b in bounds), math.inf))
                candidates = sorted(ids[int(i)] for i in drill_trees[layer].query(query))
            inner_index, outer_index = prep(inner), prep(outer)
            differences = 0
            for i in candidates:
                hole = drills[i]
                # Native planes already have many antipads. A non-intersecting
                # drill cannot change this exact geometry; avoid rebuilding an
                # entire multi-hole plane for each such drill.
                if inner_index.intersects(hole["outer"]):
                    inner = inner.difference(hole["outer"])
                    inner_index = prep(inner)
                    differences += 1
                if outer_index.intersects(hole["inner"]):
                    outer = outer.difference(hole["inner"])
                    outer_index = prep(outer)
                    differences += 1
            if stats is not None:
                stats["copper_layer_records"] += 1
                stats["layer_drill_pairs"] += len(ids)
                stats["candidate_drills"] += len(candidates)
                stats["difference_calls"] += differences
            finished.append(dict(item, layers=(layer,), inner=inner, outer=outer))
    return finished, drills


def terminal_items(board, net, *, layer=None):
    """输入规范板、网名及可选铜层，返回各端子的实铜和代表点。

    指定层时只使用该层扣除真实钻孔后的铜。无指定层沿用原有跨层并集。
    端子没有相应实铜时抛出 PipelineError。
    """
    copper, _ = copper_and_drills(board, net_filter=net)
    result = []
    for ref in board["nets"].get(net, []):
        key = (str(ref["compId"]), str(ref["padId"]))
        items = [p for p in copper if p["terminal"] == key and p["net"] == net
                 and (layer is None or layer in p["layers"])]
        inner = unary_union([p["inner"] for p in items])
        if inner.is_empty:
            if layer is not None:
                raise PipelineError(f"端子 {key} 在铜层 {layer} 没有可证明的导电区域")
            raise PipelineError(f"terminal {key} has no provable copper")
        point = inner.representative_point()
        result.append(dict(key=key, point=(point.x, point.y),
                           layers=tuple(sorted({l for p in items for l in p["layers"]})), inner=inner))
    return result


class CollisionModel:
    """Frozen obstacles, rebuilt after copper commits; no centerline-only checks."""
    def __init__(self, board, *, use_index=True):
        self.rules = rules_for(board)
        self.board = board_polygon(board)
        self.boundary = self.board.boundary
        self.copper, self.drills = copper_and_drills(board)
        # 软偏好独立保留钻孔前焊盘；不加入硬碰撞障碍或验收错误。
        self._pad_source = board["components"]
        self._smd_pad_index = None
        self._via_in_pad_cache = {}
        self.net_tags = board.get("tags", {}).get("net_tags", {})
        self.keepouts = []
        self._keepout_names = []
        for ko in board.get("routes", {}).get("keepouts", []):
            self._keepout_names.append(f"keepout {len(self.keepouts)}")
            self.keepouts.append((solid_polygon(ko["points"]), _layers(ko, 12, self.rules),
                                  ko.get("clearance", 0), ko.get("nets"), ko.get("net_types")))
        for c in board["components"]:
            tag = board.get("tags", {}).get("component_tags", {}).get(c["id"], {})
            halo_nets, halo_types = sensitive_halo_targets(tag, board["nets"])
            if tag.get("role") == "SENSITIVE_IC" and tag.get("keepout_halo", 0) > 0:
                self._keepout_names.append(f"sensitive halo {c['id']}")
                self.keepouts.append((_body(c, self.rules.curve_tolerance).outer,
                                      tuple(tag.get("halo_layers", [c["layer"]])),
                                      tag["keepout_halo"], halo_nets, halo_types))
        self.use_index = use_index
        self._objects = {"copper": self.copper, "drills": self.drills, "keepouts": self.keepouts}
        self._layer_ids, self._trees = {}, {}
        self._copper_types, self._copper_gaps = {}, {}
        self._keepout_gaps, self._empty_copper = {}, {}
        for kind, items in self._objects.items():
            ids_by_layer, trees = {}, {}
            for i, item in enumerate(items):
                layers = item[1] if kind == "keepouts" else item["layers"]
                for layer in layers:
                    ids_by_layer.setdefault(layer, []).append(i)
            for layer, ids in ids_by_layer.items():
                if use_index:
                    shapes = [items[i][0] if kind == "keepouts" else items[i]["outer"] for i in ids]
                    trees[layer] = STRtree(shapes)
                if kind == "copper":
                    self._copper_types[layer] = {self.net_type(items[i]["net"]) for i in ids}
                    # The old rule rejects a via on this layer even when this
                    # already-empty copper is far away. An STRtree cannot find it.
                    self._empty_copper[layer] = [i for i in ids if items[i]["inner"].is_empty]
                elif kind == "keepouts":
                    self._keepout_gaps[layer] = max(0, max(items[i][2] for i in ids))
            self._layer_ids[kind], self._trees[kind] = ids_by_layer, trees
        self.reset_stats()

    def via_in_pad(self, x, y, hole, layers):
        """判断候选孔洞是否接触贴片焊盘，仅供软代价及提示使用。

        入参为世界坐标、孔径和实际跨越层；出参为布尔值，同网同样计入。
        焊盘使用钻孔前真实形状及底层变换，器件自带孔的焊盘不计入本项。
        非有限坐标、非正孔径或非法层号抛 PipelineError；不改变合法性。
        """
        x, y = finite(x, "via x"), finite(y, "via y")
        hole = finite(hole, "via hole", positive=True)
        layers = tuple(layers)
        if not layers or any(type(layer) is not int or layer not in self.rules.copper_layers for layer in layers):
            raise PipelineError("盘中孔偏好需要实际铜层编号")
        key = x, y, hole, tuple(sorted(set(layers)))
        if key in self._via_in_pad_cache:
            return self._via_in_pad_cache[key]
        if self._smd_pad_index is None:
            pads = [(tuple(_layers(p, c["layer"], self.rules)),
                     _fast_pad_bounds(c, p, self.rules.curve_tolerance).outer)
                    for c in self._pad_source for p in c["pads"]
                    if p.get("hole") is None or p["hole"] == 0]
            self._smd_pad_index = pads, STRtree([shape for _, shape in pads])
        pads, tree = self._smd_pad_index
        drill = transform(shape_from_spec({"type": "CIRCLE", "diameter": hole},
                                          self.rules.curve_tolerance), x, y, 0).outer
        hit = any(set(layers).intersection(pads[int(i)][0]) and drill.intersects(pads[int(i)][1])
                  for i in tree.query(drill))
        # 缓存有显式上限，长时间搜索不累积全部访问坐标。
        if len(self._via_in_pad_cache) >= 8192:
            self._via_in_pad_cache.clear()
        self._via_in_pad_cache[key] = bool(hit)
        return bool(hit)

    def reset_stats(self):
        self.stats = dict(candidate_queries=0, candidate_objects=0,
                          intersection_calls=0, distance_calls=0, difference_calls=0)

    def _candidates(self, kind, layers, outer, gap=0, *, include_empty=False, extra_outer=None):
        """Broad phase only: exact conservative geometry remains authoritative."""
        self.stats["candidate_queries"] += 1
        ids = set()
        if self.use_index and not outer.is_empty:
            x0, y0, x1, y1 = outer.bounds
            if extra_outer is not None and not extra_outer.is_empty:
                ex0, ey0, ex1, ey1 = extra_outer.bounds
                x0, y0, x1, y1 = min(x0, ex0), min(y0, ey0), max(x1, ex1), max(y1, ey1)
            # Expand by the maximum applicable *actual* rule, including large
            # class gaps. Round outward so the broad phase cannot clip a tie.
            query = box(math.nextafter(x0 - gap, -math.inf), math.nextafter(y0 - gap, -math.inf),
                        math.nextafter(x1 + gap, math.inf), math.nextafter(y1 + gap, math.inf))
            for layer in layers:
                tree = self._trees[kind].get(layer)
                if tree is not None:
                    layer_ids = self._layer_ids[kind][layer]
                    ids.update(layer_ids[int(i)] for i in tree.query(query))
        elif not self.use_index:
            # Explicit linear reference, useful for differential checks and
            # measurements. Normal routing never takes this path.
            for layer in layers:
                ids.update(self._layer_ids[kind].get(layer, ()))
        if include_empty:
            for layer in layers:
                ids.update(self._empty_copper.get(layer, ()))
        self.stats["candidate_objects"] += len(ids)
        return sorted(ids)

    def _copper_gap(self, net, layers):
        kind = self.net_type(net)
        key = kind, tuple(layers)
        if key not in self._copper_gaps:
            self._copper_gaps[key] = max((self.rules.get_clearance(kind, other)
                for layer in layers for other in self._copper_types.get(layer, ())), default=0)
        return self._copper_gaps[key]

    def _conflict(self, shape, other, gap):
        self.stats["intersection_calls"] += 1
        if shape.intersects(other):
            return True
        if gap > 0:
            self.stats["distance_calls"] += 1
            return shape.distance(other) < gap
        return False

    @staticmethod
    def _reason(reasons, rule, kind, name, layers, net=None, gap=None):
        # Boolean hot paths allocate no diagnostic dictionaries.
        if reasons is not None:
            reason = dict(rule=rule, kind=kind, object=name, name=name, layers=list(layers))
            if len(layers) == 1:
                reason["layer"] = layers[0]
            if net is not None:
                reason.update(object_net=net, net=net)
            if gap is not None:
                reason["required_clearance"] = gap
            reasons.append(reason)

    def net_type(self, net):
        return "GND" if net.upper() == "GND" else self.net_tags.get(net, {}).get("type", "NORMAL")

    def _shape_ok(self, net, layers, outer, *, new_drill=None, reasons=None):
        r = self.rules
        valid = True
        if not self.board.covers(outer) or outer.distance(self.boundary) < r.board_edge_clearance:
            self._reason(reasons, "board_edge_clearance", "board", "board boundary/cutout", layers,
                         gap=r.board_edge_clearance)
            if reasons is None:
                return False
            valid = False
        ko_gap = max((self._keepout_gaps.get(layer, 0) for layer in layers), default=0)
        for i in self._candidates("keepouts", layers, outer, ko_gap):
            shape, ko_layers, gap, nets, types = self.keepouts[i]
            if nets is not None and net not in nets:
                continue
            if types is not None and self.net_type(net) not in types:
                continue
            if self._conflict(outer, shape, gap):
                self._reason(reasons, "keepout_clearance", "keepout", self._keepout_names[i], ko_layers, gap=gap)
                if reasons is None:
                    return False
                valid = False
        for i in self._candidates("copper", layers, outer, self._copper_gap(net, layers)):
            other = self.copper[i]
            if net and net == other["net"]:
                continue
            gap = r.get_clearance(self.net_type(net), self.net_type(other["net"]))
            if self._conflict(outer, other["outer"], gap):
                self._reason(reasons, "copper_clearance", "copper", other["name"], other["layers"], other["net"], gap)
                if reasons is None:
                    return False
                valid = False
        drill_gap = max(r.npth_clearance, r.hole_to_hole_clearance if new_drill is not None else 0)
        # Independently chosen curve subdivisions can make the certified hole
        # outer protrude beyond the copper outer, even with a positive annulus.
        for i in self._candidates("drills", layers, outer, drill_gap,
                                  extra_outer=new_drill.outer if new_drill is not None else None):
            drill = self.drills[i]
            if not drill["plated"] or not net or net != drill["net"]:
                gap = 0 if drill["plated"] else r.npth_clearance
                if self._conflict(outer, drill["outer"], gap):
                    self._reason(reasons, "drill_copper_clearance", "drill", drill["name"], drill["layers"], drill["net"], gap)
                    if reasons is None:
                        return False
                    valid = False
            if new_drill is not None:
                if self._conflict(new_drill.outer, drill["outer"], r.hole_to_hole_clearance):
                    self._reason(reasons, "hole_to_hole_clearance", "drill", drill["name"], drill["layers"], drill["net"], r.hole_to_hole_clearance)
                    if reasons is None:
                        return False
                    valid = False
        if new_drill is not None:
            for i in self._candidates("copper", layers, new_drill.outer, include_empty=True):
                other = self.copper[i]
                if net != other["net"]:
                    if self._conflict(new_drill.outer, other["outer"], 0):
                        self._reason(reasons, "new_drill_foreign_copper", "copper", other["name"], other["layers"], other["net"], 0)
                        if reasons is None:
                            return False
                        valid = False
                self.stats["difference_calls"] += 1
                if other["inner"].difference(new_drill.outer).is_empty:
                    self._reason(reasons, "new_drill_erases_copper", "copper", other["name"], other["layers"], other["net"])
                    if reasons is None:
                        return False
                    valid = False
        return valid

    def trace_ok(self, net, layer, sx, sy, ex, ey, width):
        return self._trace_check(net, layer, sx, sy, ex, ey, width)

    def _trace_check(self, net, layer, sx, sy, ex, ey, width, reasons=None):
        try:
            tag = self.net_tags.get(net, {})
            if layer not in self.rules.copper_layers or layer not in tag.get("allowed_layers", self.rules.copper_layers):
                self._reason(reasons, "trace_allowed_layers", "net_rule", net, (layer,), net)
                return False
            if width < max(self.rules.minimum_trace_width, tag.get("width", 0), tag.get("min_width", 0)):
                self._reason(reasons, "minimum_trace_width", "net_rule", net, (layer,), net)
                return False
            shape = _fast_trace_bounds(dict(sx=sx, sy=sy, ex=ex, ey=ey, width=width), self.rules.curve_tolerance)
            remaining = shape.inner
            removed_by = [] if reasons is not None else None
            for i in self._candidates("drills", (layer,), shape.inner):
                if removed_by is not None and remaining.intersects(self.drills[i]["outer"]):
                    removed_by.append(self.drills[i]["name"])
                self.stats["difference_calls"] += 1
                remaining = remaining.difference(self.drills[i]["outer"])
            if remaining.is_empty:
                name = "; ".join(removed_by) if removed_by else "trace inner geometry"
                self._reason(reasons, "trace_no_remaining_copper", "drill", name, (layer,))
                return False
            return self._shape_ok(net, (layer,), shape.outer, reasons=reasons)
        except (ValueError, TypeError):
            self._reason(reasons, "invalid_trace_geometry", "input", "trace", (layer,))
            return False

    def via_ok(self, net, x, y, hole, diameter, layers):
        return self._via_check(net, x, y, hole, diameter, layers)

    def _via_check(self, net, x, y, hole, diameter, layers, reasons=None):
        try:
            layers = _layers({"layers": list(layers)}, None, self.rules, spanning=True)
            tag = self.net_tags.get(net, {})
            if len(layers) < 2 or tag.get("allow_via") is False:
                self._reason(reasons, "via_not_allowed", "net_rule", net, layers, net)
                return False
            if not set(layers).issubset(tag.get("allowed_layers", self.rules.copper_layers)):
                self._reason(reasons, "via_allowed_layers", "net_rule", net, layers, net)
                return False
            if hole < self.rules.minimum_drill or (diameter - hole) / 2 < self.rules.min_annular_ring:
                self._reason(reasons, "via_drill_annular_ring", "net_rule", net, layers, net)
                return False
            shape, drill = via_bounds(dict(x=x, y=y, hole=hole, diameter=diameter), self.rules.curve_tolerance)
            if not self.board.covers(drill.outer) or drill.outer.distance(self.boundary) < self.rules.board_edge_clearance:
                self._reason(reasons, "drill_board_edge_clearance", "board", "board boundary/cutout", layers,
                             gap=self.rules.board_edge_clearance)
                return False
            return self._shape_ok(net, layers, shape.outer, new_drill=drill, reasons=reasons)
        except (ValueError, TypeError):
            self._reason(reasons, "invalid_via_geometry", "input", "via", ())
            return False

    def trace_diagnostics(self, net, layer, sx, sy, ex, ey, width):
        """Return rejection reasons; an empty list means the trace is legal."""
        reasons = []
        self._trace_check(net, layer, sx, sy, ex, ey, width, reasons)
        return reasons

    def via_diagnostics(self, net, x, y, hole, diameter, layers):
        """Return rejection reasons; an empty list means the via is legal."""
        reasons = []
        self._via_check(net, x, y, hole, diameter, layers, reasons)
        return reasons

    def explain_trace(self, *args, **kwargs):
        reasons = self.trace_diagnostics(*args, **kwargs)
        return reasons[0] if reasons else None

    def explain_via(self, *args, **kwargs):
        reasons = self.via_diagnostics(*args, **kwargs)
        return reasons[0] if reasons else None


def fingerprint(board):
    return hashlib.sha256(json.dumps(board, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def polygons(geom):
    if geom.is_empty:
        return []
    if geom.geom_type == "Polygon":
        return [geom]
    if hasattr(geom, "geoms"):
        return [p for g in geom.geoms for p in polygons(g)]
    return []


def polygon_record(p):
    return dict(points=[list(v) for v in list(p.exterior.coords)[:-1]],
                holes=[[list(v) for v in list(r.coords)[:-1]] for r in p.interiors])
