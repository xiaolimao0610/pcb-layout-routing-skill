"""PCB snapshot validation, not a router or an EasyEDA write-back client.

All lengths are mil. Public compatibility API:
    normalize_raw_pcb_data(raw_data) -> copy with canonical local pad coordinates
    verify_full_board_drc_and_connectivity(...) -> (passed, list[str])

Curved copper uses certified inner/outer approximations: outer boundaries prove
clearance, inner boundaries prove electrical contact. Ambiguous contact fails.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from dataclasses import dataclass, field
import json
import math
from pathlib import Path
from typing import Dict, Optional, Tuple

from shapely.errors import GEOSException
from shapely.geometry import box
from shapely.strtree import STRtree

from pcb_geometry import (
    Bounds, GeometryError, shape_from_spec, filled_region_bounds, solid_polygon, trace_bounds,
    transform, via_bounds,
)


def number(value, name, minimum=None, positive=False):
    if isinstance(value, bool):
        raise GeometryError(f"{name}: boolean is not a length/angle")
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise GeometryError(f"{name}: finite number required") from exc
    if not math.isfinite(result) or (positive and result <= 0):
        raise GeometryError(f"{name}: finite {'positive ' if positive else ''}number required")
    if minimum is not None and result < minimum:
        raise GeometryError(f"{name}: must be >= {minimum}")
    return result


def identifier(value, name):
    if isinstance(value, bool) or not isinstance(value, (str, int)) or str(value) == "":
        raise GeometryError(f"{name}: nonempty string/integer required")
    return str(value)


def sensitive_halo_targets(tag, known_nets):
    """输入器件标签及完整网表，返回保护区的网络/类型筛选元组。

    显式筛选覆盖旧的SEWAGE类默认；同时提供两项时取交集。
    空列表、未知网络/类型或不生效的角色/半径配置抛GeometryError。
    本函数仅解释范围，不改变净距或任何几何验收条件。
    """
    explicit = {"halo_nets", "halo_net_types"} & set(tag)
    if not explicit:
        return None, ("SEWAGE",)  # 兼容旧输入，实际新项目应显式声明保护对象。
    if tag.get("role") != "SENSITIVE_IC" or number(tag.get("keepout_halo", 0), "keepout_halo", minimum=0) <= 0:
        raise GeometryError("halo筛选需要SENSITIVE_IC及正keepout_halo")
    values = []
    for key, allowed in (("halo_nets", known_nets),
                         ("halo_net_types", {"NORMAL", "SEWAGE", "DRINKING", "STORM", "GND"})):
        if key not in tag:
            values.append(None)
            continue
        selected = tag[key]
        if (not isinstance(selected, list) or not selected
                or any(not isinstance(v, str) or v not in allowed for v in selected)
                or len(set(selected)) != len(selected)):
            raise GeometryError(f"{key}需要非空、不重复且已知的字符串列表")
        values.append(tuple(selected))
    return tuple(values)


@dataclass
class RuleConfig:
    board_edge_clearance: float = 20.0
    component_courtyard_margin: float = 10.0
    sensitive_noisy_isolation: float = 200.0
    default_via_drill: float = 12.0
    default_via_diameter: float = 24.0
    default_via_cost: float = 60.0  # Retained input field; validation does not route.
    grid_step: float = 10.0
    default_clearance: float = 6.0
    clearance_matrix: Dict[Tuple[str, str], float] = field(default_factory=lambda: {
        ("SEWAGE", "DRINKING"): 30.0,
        ("SEWAGE", "NORMAL"): 12.0,
        ("SEWAGE", "GND"): 10.0,
    })
    copper_layers: Tuple[int, ...] = (1, 2)  # Physical stack order, not numeric order.
    minimum_trace_width: float = 1.0
    min_annular_ring: float = 1.0
    minimum_drill: float = 1.0
    hole_to_hole_clearance: float = 6.0
    npth_clearance: float = 6.0
    curve_tolerance: float = 0.001
    contact_tolerance: float = 1e-9
    component_pad_clearance: float = 0.0  # 跨器件共同铜层焊盘净距；零仍不允许接触。

    def validate(self):
        for name in ("board_edge_clearance", "component_courtyard_margin",
                     "component_pad_clearance",
                     "sensitive_noisy_isolation", "default_clearance",
                     "min_annular_ring", "hole_to_hole_clearance", "npth_clearance"):
            number(getattr(self, name), name, minimum=0)
        for name in ("minimum_trace_width", "minimum_drill", "default_via_drill",
                     "default_via_diameter", "curve_tolerance", "contact_tolerance", "grid_step"):
            number(getattr(self, name), name, positive=True)
        number(self.default_via_cost, "default_via_cost", minimum=0)
        if self.default_via_diameter <= self.default_via_drill:
            raise GeometryError("default via diameter must exceed drill")
        if not self.copper_layers or len(set(self.copper_layers)) != len(self.copper_layers):
            raise GeometryError("copper_layers must be a unique nonempty physical stack")
        if any(type(layer) is not int or layer in (11, 12) or layer <= 0 for layer in self.copper_layers):
            raise GeometryError("copper_layers must contain copper IDs, not BOARD_OUTLINE=11/MULTI=12")
        for key, value in self.clearance_matrix.items():
            if not isinstance(key, tuple) or len(key) != 2 or not all(isinstance(k, str) for k in key):
                raise GeometryError("clearance_matrix keys must be (type_a, type_b) tuples")
            number(value, f"clearance {key}", minimum=0)
            if key[::-1] in self.clearance_matrix and self.clearance_matrix[key[::-1]] != value:
                raise GeometryError(f"asymmetric clearance rule: {key}")

    def get_clearance(self, type_a, type_b):
        return self.clearance_matrix.get((type_a, type_b),
               self.clearance_matrix.get((type_b, type_a), self.default_clearance))


def _layers(obj, inherited, rules, *, spanning=False):
    if "layers" in obj:
        values = obj["layers"]
        if not isinstance(values, (list, tuple)) or not values:
            raise GeometryError("layers must be a nonempty copper-layer list")
        if len(set(values)) != len(values):
            raise GeometryError("duplicate copper layer")
        result = tuple(values)
        if "layer" in obj:
            declared = obj["layer"]
            if type(declared) is not int or (declared == 12 and len(result) < 2) or (declared != 12 and result != (declared,)):
                raise GeometryError("layer and layers contradict each other")
    else:
        layer = obj.get("layer", inherited)
        result = tuple(rules.copper_layers) if type(layer) is int and layer == 12 else (layer,)
    if any(type(v) is not int or v not in rules.copper_layers for v in result):
        raise GeometryError(f"invalid copper layer(s) {result}; MULTI is 12, outline 11 is not copper")
    order = [rules.copper_layers.index(v) for v in result]
    if spanning and set(order) != set(range(min(order), max(order) + 1)):
        raise GeometryError("drilled layer span must include every intervening copper layer")
    return tuple(v for v in rules.copper_layers if v in result)


def _pad_spec(pad):
    if "pad" in pad:
        raise GeometryError("raw EasyEDA pad tuples need a shape adapter; no guessed dimensions")
    if "shape" not in pad:
        raise GeometryError("explicit pad.shape required; dimensions do not identify a copper shape")
    kind = pad["shape"]
    if not isinstance(kind, str):
        raise GeometryError("pad.shape must be a supported shape name")
    spec = {"type": kind.upper()}
    for local, legacy in (("localW", "width"), ("localH", "height")):
        if local in pad and legacy in pad and number(pad[local], local) != number(pad[legacy], legacy):
            raise GeometryError(f"{local} conflicts with {legacy}; world AABB is not local size")
    w = pad.get("localW", pad.get("width"))
    h = pad.get("localH", pad.get("height"))
    if spec["type"] == "CIRCLE":
        d = pad.get("diameter", w)
        for size in (w, h):
            if size is not None and d is not None and number(size, "circle size") != number(d, "circle diameter"):
                raise GeometryError("CIRCLE diameter/width/height must be equal")
        spec["diameter"] = d
    elif spec["type"] == "POLYGON":
        spec["points"] = pad.get("points")
        spec["holes"] = pad.get("holes", [])
    else:
        spec.update(width=w, height=h)
        if spec["type"] == "ROUNDRECT":
            spec["radius"] = pad.get("radius", pad.get("corner_radius"))
    return spec


def normalize_raw_pcb_data(raw_data):
    """Preserve explicit local geometry; invert only declared world coordinates.

    width/height are LOCAL dimensions, never a world AABB. Legacy relX/relY
    denote world offsets unless coordinate_space='local'. Unknown shapes fail.
    This routine never moves a component and never modifies its input.
    """
    if not isinstance(raw_data, dict):
        raise GeometryError("raw_data must be an object")
    if raw_data.get("units", "mil") != "mil":
        raise GeometryError("internal units must be mil; convert explicitly before validation")
    result = deepcopy(raw_data)
    components = result.get("components")
    if not isinstance(components, list):
        raise GeometryError("components must be a list")
    seen = set()
    for c in components:
        cid = identifier(c.get("id"), "component.id")
        if cid in seen:
            raise GeometryError(f"duplicate component {cid}")
        seen.add(cid)
        c["id"] = cid
        cx, cy = number(c.get("x"), f"{cid}.x"), number(c.get("y"), f"{cid}.y")
        angle = number(c.get("rotation", 0), f"{cid}.rotation")
        if type(c.get("layer")) is not int or c["layer"] not in (1, 2):
            raise GeometryError(f"{cid}: component mounting side must be 1 or 2")
        if c.get("size_is_local", True) is not True:
            raise GeometryError(f"{cid}: world AABB dimensions cannot reconstruct local body")
        if "body_polygon" not in c:
            number(c.get("width"), f"{cid}.width", positive=True)
            number(c.get("height"), f"{cid}.height", positive=True)
        c.update(x=cx, y=cy, rotation=angle)
        pads = c.get("pads")
        if not isinstance(pads, list):
            raise GeometryError(f"{cid}.pads must be a list, including [] for mechanical objects")
        ids = set()
        for p in pads:
            allowed = {"padId", "net", "localX", "localY", "localW", "localH", "absX", "absY",
                       "relX", "relY", "width", "height", "diameter", "shape", "points", "holes",
                       "radius", "corner_radius", "rotation", "local_rotation", "layer", "layers",
                       "hole", "plated", "size_is_local", "coordinate_space", "pinName", "name", "metadata"}
            if any(key not in allowed for key in p):
                raise GeometryError(f"{cid}: unsupported pad field (raw shapes/offset or slotted drills need an adapter)")
            pid = identifier(p.get("padId"), f"{cid}.padId")
            if pid in ids:
                raise GeometryError(f"duplicate pad {cid}.{pid}")
            ids.add(pid)
            p["padId"] = pid
            if not isinstance(p.get("net", ""), str):
                raise GeometryError(f"{cid}.{pid}: net must be a string")
            p.setdefault("net", "")
            if p.get("size_is_local", True) is not True:
                raise GeometryError(f"{cid}.{pid}: width/height must describe local shape, not world AABB")
            local_flags = ("localX" in p, "localY" in p)
            if any(local_flags):
                if not all(local_flags):
                    raise GeometryError(f"{cid}.{pid}: localX and localY must be supplied together")
                lx, ly = number(p["localX"], "localX"), number(p["localY"], "localY")
            else:
                if "absX" in p or "absY" in p:
                    dx = number(p.get("absX"), "absX") - cx
                    dy = number(p.get("absY"), "absY") - cy
                elif "relX" in p and "relY" in p:
                    dx, dy = number(p["relX"], "relX"), number(p["relY"], "relY")
                    if p.get("coordinate_space", "world_offset") == "local":
                        p["localX"], p["localY"] = dx, dy
                        dx = dy = None
                    elif p.get("coordinate_space", "world_offset") != "world_offset":
                        raise GeometryError("coordinate_space must be local or world_offset")
                else:
                    raise GeometryError(f"{cid}.{pid}: explicit local/world pad position required")
                if dx is not None:
                    a = math.radians(angle)
                    lx = dx * math.cos(a) + dy * math.sin(a)
                    ly = -dx * math.sin(a) + dy * math.cos(a)
                    if c["layer"] == 2:
                        lx = -lx
                else:
                    lx, ly = p["localX"], p["localY"]
            p["localX"], p["localY"] = lx, ly
            if "rotation" in p and "local_rotation" not in p:
                raise GeometryError(f"{cid}.{pid}: use local_rotation; ambiguous raw pad rotation")
            p["local_rotation"] = number(p.get("local_rotation", 0), "pad.local_rotation")
            if "layers" not in p:
                p.setdefault("layer", c["layer"])
            spec = _pad_spec(p)
            shape_from_spec(spec)  # Validate dimensions/shapes now, without silent defaults.
            if "width" in spec:
                p["localW"], p["localH"] = spec["width"], spec["height"]
            elif spec["type"] == "CIRCLE":
                p["localW"] = p["localH"] = spec["diameter"]
    return result


def _body(c, tolerance):
    spec = ({"type": "POLYGON", "points": c["body_polygon"]} if "body_polygon" in c else
            {"type": "RECTANGLE", "width": c["width"], "height": c["height"]})
    return transform(shape_from_spec(spec, tolerance), c["x"], c["y"], c["rotation"], c["layer"] == 2)


def _pad_bounds(c, p, tolerance):
    local = transform(shape_from_spec(_pad_spec(p), tolerance),
                      p["localX"], p["localY"], p["local_rotation"])
    return transform(local, c["x"], c["y"], c["rotation"], c["layer"] == 2)


def _pad_drill_bounds(c, p, tolerance):
    """Return true round/capsule drill bounds and its smallest tool dimension.

    Object drill offsets and rotation are in the pad's own local axes. Apply
    that pose, then the pad pose, then the component mirror/rotation/position.
    A legacy numeric hole is a centered circle; it is never a slot surrogate.
    """
    hole = p["hole"]
    angle = ox = oy = 0.0
    if isinstance(hole, dict):
        kind = hole.get("type")
        dimensions = {"ROUND": {"diameter"}, "SLOT": {"width", "height"}}
        if kind not in dimensions:
            raise GeometryError("pad.hole.type must be ROUND or SLOT")
        allowed = dimensions[kind] | {"type", "rotation", "offsetX", "offsetY"}
        if set(hole) - allowed or not dimensions[kind].issubset(hole):
            raise GeometryError("unsupported/missing pad drill dimensions or fields")
        angle = number(hole.get("rotation", 0), "hole.rotation")
        ox = number(hole.get("offsetX", 0), "hole.offsetX")
        oy = number(hole.get("offsetY", 0), "hole.offsetY")
        if kind == "ROUND":
            feature = number(hole["diameter"], "hole.diameter", positive=True)
            spec = {"type": "CIRCLE", "diameter": feature}
        else:
            w = number(hole["width"], "hole.width", positive=True)
            h = number(hole["height"], "hole.height", positive=True)
            feature = min(w, h)
            spec = {"type": "OVAL", "width": w, "height": h}
    else:
        feature = number(hole, "pad.hole", positive=True)
        spec = {"type": "CIRCLE", "diameter": feature}
    shape = transform(shape_from_spec(spec, tolerance), ox, oy, angle)
    # Preserve the old centered numeric circle's polygon orientation exactly.
    # Its physical drill is isotropic; new object shapes use the explicit pose.
    pad_angle = p["local_rotation"] if isinstance(hole, dict) else 0
    shape = transform(shape, p["localX"], p["localY"], pad_angle)
    return transform(shape, c["x"], c["y"], c["rotation"], c["layer"] == 2), feature


def get_pad_polygon(comp, pad):
    """Compatibility helper: only straight-edged shapes have exact vertices.

    Unlike the original helper, local dimensions are mandatory. Curved shapes
    must use the bounds engine, never an AABB presented as real copper.
    """
    c = normalize_raw_pcb_data({"components": [comp]})["components"][0]
    p = next(p for p in c["pads"] if str(p["padId"]) == str(pad["padId"]))
    if _pad_spec(p)["type"] not in ("RECTANGLE", "POLYGON"):
        raise GeometryError("get_pad_polygon does not flatten curved copper to a polygon")
    polygon = _pad_bounds(c, p, 0.001).inner
    if polygon.geom_type != "Polygon" or len(polygon.interiors):
        raise GeometryError("single-ring get_pad_polygon cannot represent holes or multiple copper islands")
    return list(polygon.exterior.coords)[:-1]


@dataclass
class _Copper:
    name: str
    net: str
    layers: tuple
    bounds: Bounds
    terminal: Optional[tuple] = None
    barrel: Optional[str] = None


@dataclass
class _Drill:
    name: str
    net: str
    layers: tuple
    bounds: Bounds
    plated: bool


def _pieces(geometry):
    if geometry.is_empty:
        return []
    if geometry.geom_type == "Polygon":
        return [geometry]
    if geometry.geom_type in ("MultiPolygon", "GeometryCollection"):
        return [p for g in geometry.geoms for p in _pieces(g)]
    return []


def _expanded_search_box(geometry, gap):
    bounds = geometry.bounds
    if len(bounds) != 4:
        return None
    x0, y0, x1, y1 = bounds
    envelope = (math.nextafter(x0 - gap, -math.inf),
                math.nextafter(y0 - gap, -math.inf),
                math.nextafter(x1 + gap, math.inf),
                math.nextafter(y1 + gap, math.inf))
    if not all(math.isfinite(v) for v in envelope):
        return None
    return box(*envelope)


def _layer_trees(items):
    layers = {}
    for i, item in enumerate(items):
        for layer in item.layers:
            layers.setdefault(layer, []).append(i)
    return {layer: (indices, STRtree([items[i].bounds.outer for i in indices]))
            for layer, indices in layers.items()}


def _clearance_pairs(finished, rules, use_index):
    """Original i/j order; the tree only discards provably distant layer pairs.

    Keep the full scan as an executable reference. Nonstandard numeric rule
    objects or an unrepresentable search envelope also retain that scan; the
    original narrow phase remains responsible for their validation/decisions.
    """
    indexed = (use_index and type(rules) is RuleConfig
               and type(rules.clearance_matrix) is dict
               and getattr(rules.get_clearance, '__func__', None) is RuleConfig.get_clearance)
    if indexed:
        gaps = [rules.default_clearance, *rules.clearance_matrix.values()]
        indexed = all(type(gap) in (int, float) for gap in gaps)
    if indexed:
        # Round both the maximum rule and each envelope endpoint outwards. This
        # includes exact-threshold pairs and avoids shrinking a box by one ulp.
        gap = math.nextafter(float(max(gaps)), math.inf)
        indexed = math.isfinite(gap)
    if not indexed:
        for i in range(len(finished)):
            for j in range(i + 1, len(finished)):
                yield i, j
        return
    trees = _layer_trees(finished)
    for i, item in enumerate(finished):
        envelope = _expanded_search_box(item.bounds.outer, gap)
        if envelope is None:
            candidates = range(i + 1, len(finished))
        else:
            indices, tree = trees[item.layers[0]]
            candidates = sorted(indices[int(k)] for k in tree.query(envelope)
                                if indices[int(k)] > i)
        for j in candidates:
            yield i, j


def _foreign_drill_candidates(item, layer, trees, gap):
    """None means full scan; only foreign drill distance tests are filtered.

    The query uses the uncut original copper outer. Every successive difference
    can only shrink it. Drill subtraction order and every difference call remain
    untouched, including a drill that is absent from this candidate set.
    """
    if trees is None:
        return None
    envelope = _expanded_search_box(item.bounds.outer, gap)
    if envelope is None:
        return None
    if layer not in trees:
        return set()
    indices, tree = trees[layer]
    return {indices[int(k)] for k in tree.query(envelope)}


def _component_pad_errors(pads, rules, use_index=True):
    """输入钻孔前的焊盘铜记录与已校验规则，返回跨器件装配净距错误。

    仅比较不同器件、具有共同铜层的焊盘，与是否同网无关；同一封装内的
    叠加焊盘和合法线到焊盘连接不属本检查。保守外界用于证明净距，曲线
    近接不能证明安全时拒绝候选；不扩大几何容差。无效净距抛 GeometryError，
    由整板入口统一转为 INVALID_INPUT。每对焊盘最多报告一次，不逐层重复。
    """
    gap = number(rules.component_pad_clearance, "component_pad_clearance", minimum=0)
    trees = _layer_trees(pads) if use_index else None
    errors = []
    for i, pad in enumerate(pads):
        envelope = _expanded_search_box(pad.bounds.outer, gap) if trees is not None else None
        if envelope is None:
            candidates = range(i + 1, len(pads))
        else:
            candidates = set()
            for layer in pad.layers:
                indices, tree = trees[layer]
                candidates.update(indices[int(k)] for k in tree.query(envelope)
                                  if indices[int(k)] > i)
            candidates = sorted(candidates)
        for j in candidates:
            other = pads[j]
            if pad.terminal[0] == other.terminal[0] or not set(pad.layers).intersection(other.layers):
                continue
            a, b = pad.bounds.outer, other.bounds.outer
            if a.intersects(b) or a.distance(b) < gap:
                errors.append(f"COMPONENT_PAD_CLEARANCE: {pad.name} / {other.name} "
                              f"接触、重叠或未证明达到跨器件焊盘净距 {gap:g} mil")
    return errors


def verify_full_board_drc_and_connectivity(comps_dict, routes, nets, tags,
                                         board_bbox, rules=None, *, use_index=True, geometry_context=None):
    """输入完整器件、布线、网表、标签、板框及规则，输出通过标志和错误列表。

    所有命名焊盘（含单端网络）必须在网表中，全部命名铜须连接该网真实端子，
    未铺实铜不能通过。未知或非法输入返回分类错误，不修改输入对象。
    use_index=False 保留旧线性检查；可选 geometry_context 只补证铺铜内界，
    必须匹配创建时完整输入与规则，不认证非铺铜对象的原生来源。
    """
    errors = []
    rules = rules or RuleConfig()
    try:
        rules.validate()
        if geometry_context is not None:
            from pcb_gerber_evidence import require_context
            require_context(geometry_context).validate_parts(comps_dict, routes, nets, tags, board_bbox, rules)
        if not all(isinstance(x, dict) for x in (comps_dict, routes, nets, tags)):
            raise GeometryError("components/routes/nets/tags must be objects")
        for key, c in comps_dict.items():
            if str(key) != str(c.get("id")):
                raise GeometryError(f"component dictionary key/id mismatch: {key}")
        for key in ("required_rules", "special_rules", "global_constraints"):
            if tags.get(key):
                raise GeometryError(f"UNSUPPORTED_RULE: {key} requires an implemented checker")
        if any(key not in {"required_rules", "special_rules", "global_constraints", "component_tags", "net_tags", "metadata"}
               for key in tags):
            raise GeometryError("UNSUPPORTED_RULE: unknown top-level tags field")
        known_route_keys = {"traces", "vias", "shield_vias", "copper_pours", "holes", "keepouts", "units"}
        if any(key not in known_route_keys for key in routes):
            raise GeometryError("unsupported route geometry field")
        if routes.get("units", "mil") != "mil":
            raise GeometryError("route units must be mil")
        comps = sorted(normalize_raw_pcb_data({"components": list(comps_dict.values())})["components"],
                       key=lambda c: c["id"])
        component_tags, net_tags = tags.get("component_tags", {}), tags.get("net_tags", {})
        if not isinstance(component_tags, dict) or not isinstance(net_tags, dict):
            raise GeometryError("component_tags/net_tags must be objects")
        for net, tag in net_tags.items():
            if net not in nets:
                raise GeometryError(f"rule references unknown network {net}")
            if not isinstance(tag, dict):
                raise GeometryError("each net tag must be an object")
            if tag.get("type", "NORMAL") not in {"NORMAL", "SEWAGE", "DRINKING", "STORM", "GND"}:
                raise GeometryError(f"unsupported network classification for {net}")
            supported = {"type", "width", "min_width", "allowed_layers", "allow_via",
                         "alpha_length", "beta_repel", "via_cost", "via_drill", "via_diameter",
                         "need_shield_check", "metadata"}
            if any(key not in supported for key in tag):
                raise GeometryError(f"UNSUPPORTED_RULE: net {net} contains an unimplemented rule")
            for key in ("width", "min_width"):
                if key in tag:
                    number(tag[key], f"{net}.{key}", positive=True)
            if "allowed_layers" in tag:
                _layers({"layers": tag["allowed_layers"]}, None, rules)
            if "allow_via" in tag and type(tag["allow_via"]) is not bool:
                raise GeometryError("allow_via must be boolean")
        for cid, tag in component_tags.items():
            if cid not in comps_dict:
                raise GeometryError(f"rule references unknown component {cid}")
            if not isinstance(tag, dict):
                raise GeometryError("each component tag must be an object")
            if tag.get("role", "NORMAL") not in {"NORMAL", "SENSITIVE_IC", "NOISY_SOURCE", "CONNECTOR"}:
                raise GeometryError(f"unsupported component role for {cid}")
            if any(key not in {"role", "keepout_halo", "halo_layers", "halo_nets", "halo_net_types", "fixed", "fixed_pose", "metadata"}
                   for key in tag):
                raise GeometryError(f"UNSUPPORTED_RULE: component {cid} contains an unimplemented rule")
            sensitive_halo_targets(tag, nets)
        if isinstance(board_bbox, dict):
            if any(key not in {"outline", "cutouts"} for key in board_bbox):
                raise GeometryError("board polygon supports outline/cutouts only")
            board = solid_polygon(board_bbox["outline"], board_bbox.get("cutouts", []))
        else:
            if len(board_bbox) != 4:
                raise GeometryError("board_bbox must have four coordinates")
            x0, y0, x1, y1 = [number(v, "board coordinate") for v in board_bbox]
            if x1 <= x0 or y1 <= y0:
                raise GeometryError("invalid board rectangle")
            board = solid_polygon([(x0, y0), (x1, y0), (x1, y1), (x0, y1)])
        # solid_polygon returns the exact Shapely Polygon.
        if isinstance(board, Bounds):
            board = board.inner
        bodies, courtyards, keepouts = {}, {}, []
        copper, drills = [], []
        pad_records = {}

        def net_type(net):
            return "GND" if net.upper() == "GND" else net_tags.get(net, {}).get("type", "NORMAL")

        def net_string(value):
            if not isinstance(value, str) or not value:
                raise GeometryError("route copper requires a nonempty net name")
            return value

        def boundary(label, geom, clearance):
            if not board.covers(geom) or geom.distance(board.boundary) < clearance:
                errors.append(f"BOARD_EDGE: {label} violates board/cutout edge clearance {clearance:g} mil")

        for c in comps:
            cid = c["id"]
            bodies[cid] = _body(c, rules.curve_tolerance)
            courtyard = c.get("courtyard_polygon")
            courtyards[cid] = (transform(shape_from_spec({"type": "POLYGON", "points": courtyard}),
                c["x"], c["y"], c["rotation"], c["layer"] == 2) if courtyard is not None else bodies[cid])
            if not courtyards[cid].outer.covers(bodies[cid].outer):
                raise GeometryError(f"{cid}: courtyard must contain body")
            boundary(f"component {cid}", courtyards[cid].outer, rules.board_edge_clearance)
            ct = component_tags.get(cid, {})
            if ct.get("fixed") and "fixed_pose" not in ct:
                raise GeometryError(f"{cid}: fixed requires fixed_pose baseline")
            if "fixed_pose" in ct:
                baseline = ct["fixed_pose"]
                if any(c[k] != baseline.get(k) for k in ("x", "y", "rotation", "layer")):
                    errors.append(f"FIXED_POSE: component {cid} moved")
            halo = number(ct.get("keepout_halo", 0), f"{cid}.keepout_halo", minimum=0)
            if ct.get("role") == "SENSITIVE_IC" and halo > 0:
                layers = _layers({"layers": ct.get("halo_layers", [c["layer"]])}, None, rules)
                halo_nets, halo_types = sensitive_halo_targets(ct, nets)
                keepouts.append((f"sensitive {cid}", bodies[cid].outer, layers, halo, halo_nets, halo_types))
            for p in c["pads"]:
                key = (cid, p["padId"])
                shape = _pad_bounds(c, p, rules.curve_tolerance)
                layers = _layers(p, c["layer"], rules, spanning=bool(p.get("hole")))
                name = f"pad {cid}.{p['padId']}"
                barrel = None
                if "hole" in p:
                    hole, d = _pad_drill_bounds(c, p, rules.curve_tolerance)
                    if d < rules.minimum_drill or type(p.get("plated")) is not bool:
                        raise GeometryError(f"{name}: minimum drill and explicit plated required")
                    drills.append(_Drill(name, p["net"], layers, hole, p["plated"]))
                    if not p["plated"]:
                        if p["net"]:
                            raise GeometryError(f"{name}: electrical pad on NPTH is unsupported; use separate copper pads")
                        boundary(name, hole.outer, rules.board_edge_clearance)
                        continue
                    if not shape.inner.contains(hole.outer) or shape.inner.boundary.distance(hole.outer) < rules.min_annular_ring:
                        errors.append(f"ANNULAR_RING: {name} has insufficient copper around drill")
                    barrel = name if len(layers) > 1 else None
                elif len(layers) > 1:
                    raise GeometryError(f"{name}: multilayer electrical pad needs explicit plated hole")
                item = _Copper(name, p["net"], layers, shape, key, barrel)
                copper.append(item)
                pad_records[key] = p["net"]

        # 本体/庭院不一定包含外伸焊盘；同网铜的合法相接不能豁免装配净距。
        # 此时 copper 只含焊盘，尚未添加走线、过孔和铺铜。
        errors.extend(_component_pad_errors(copper, rules, use_index))

        for i, c in enumerate(comps):
            for other in comps[i + 1:]:
                a, b = c["id"], other["id"]
                if c["layer"] == other["layer"]:
                    distance = courtyards[a].outer.distance(courtyards[b].outer)
                    if courtyards[a].outer.intersects(courtyards[b].outer) or distance < rules.component_courtyard_margin:
                        errors.append(f"COMPONENT_CLEARANCE: {a} / {b} overlap or violate courtyard gap")
                roles = {component_tags.get(a, {}).get("role"), component_tags.get(b, {}).get("role")}
                if roles == {"SENSITIVE_IC", "NOISY_SOURCE"}:
                    if bodies[a].outer.intersects(bodies[b].outer) or bodies[a].outer.distance(bodies[b].outer) < rules.sensitive_noisy_isolation:
                        errors.append(f"ISOLATION: {a} / {b} violate projected body isolation")

        for i, tr in enumerate(routes.get("traces", [])):
            if any(key not in {"net", "layer", "layers", "sx", "sy", "ex", "ey", "width", "id", "locked"}
                   for key in tr):
                raise GeometryError("UNSUPPORTED_GEOMETRY: trace must be a straight round-ended segment")
            net = net_string(tr.get("net"))
            tag = net_tags.get(net, {})
            required_width = max(rules.minimum_trace_width, tag.get("width", 0), tag.get("min_width", 0))
            if number(tr.get("width"), "trace.width", positive=True) < required_width:
                errors.append(f"TRACE_WIDTH: {net} trace {i} below required {required_width:g} mil")
            layers = _layers(tr, None, rules)
            if len(layers) != 1:
                raise GeometryError("trace must be on exactly one copper layer")
            if "allowed_layers" in tag and not set(layers).issubset(tag["allowed_layers"]):
                errors.append(f"LAYER_RULE: {net} trace {i} on a forbidden copper layer")
            copper.append(_Copper(f"trace {i}", net, layers, trace_bounds(tr, rules.curve_tolerance)))

        seen = set()
        for i, raw in enumerate(list(routes.get("vias", [])) + list(routes.get("shield_vias", []))):
            v = dict(raw)
            net = net_string(v.get("net"))
            tag = net_tags.get(net, {})
            if "diameter" not in v or "hole" not in v:
                raise GeometryError("existing via needs explicit diameter and hole, not guessed defaults")
            if "plated" in v and v["plated"] is not True:
                raise GeometryError("a non-plated hole is not a conducting via; use routes.holes")
            if "metadata" in v and not isinstance(v["metadata"], dict):
                raise GeometryError("via.metadata must be an annotation object")
            if any(key not in {"net", "layer", "layers", "x", "y", "diameter", "hole", "plated", "id", "locked", "metadata"}
                   for key in v):
                raise GeometryError("UNSUPPORTED_GEOMETRY: only circular plated vias are supported")
            layers = _layers(v, 12, rules, spanning=True)
            if len(layers) < 2:
                raise GeometryError("via must span at least two copper layers")
            if tag.get("allow_via") is False:
                errors.append(f"VIA_RULE: {net} forbids vias")
            if "allowed_layers" in tag and not set(layers).issubset(tag["allowed_layers"]):
                errors.append(f"LAYER_RULE: {net} via {i} spans a forbidden copper layer")
            signature = (net, number(v.get("x"), "via.x"), number(v.get("y"), "via.y"),
                         number(v["diameter"], "via diameter", positive=True),
                         number(v["hole"], "via drill", positive=True), layers)
            if signature in seen:
                continue  # Same physical via may be listed in both vias and shield_vias.
            seen.add(signature)
            if v["hole"] < rules.minimum_drill or (v["diameter"] - v["hole"]) / 2 < rules.min_annular_ring:
                raise GeometryError("via drill/annular ring below configured manufacturing minimum")
            shape, drill = via_bounds(v, rules.curve_tolerance)
            name = f"via {i}"
            copper.append(_Copper(name, net, layers, shape, barrel=name))
            drills.append(_Drill(name, net, layers, drill, True))

        for i, hole in enumerate(routes.get("holes", [])):
            if any(key not in {"x", "y", "diameter", "layer", "layers", "id"} for key in hole):
                raise GeometryError("UNSUPPORTED_GEOMETRY: NPTH supports centered circular drill only")
            d = number(hole.get("diameter"), "NPTH diameter", positive=True)
            if d < rules.minimum_drill:
                raise GeometryError("NPTH drill below minimum")
            layers = _layers(hole, 12, rules, spanning=True)
            shape = transform(shape_from_spec({"type": "CIRCLE", "diameter": d}, rules.curve_tolerance),
                              number(hole.get("x"), "NPTH.x"), number(hole.get("y"), "NPTH.y"), 0)
            drills.append(_Drill(f"NPTH {i}", "", layers, shape, False))

        for i, pour in enumerate(routes.get("copper_pours", [])):
            net = net_string(pour.get("net"))
            layers = _layers(pour, None, rules)
            if len(layers) != 1 or not pour.get("filled_polygons"):
                raise GeometryError("UNFILLED_POUR: actual per-layer filled_polygons are required, not an outline")
            allowed = net_tags.get(net, {}).get("allowed_layers")
            if allowed is not None and not set(layers).issubset(allowed):
                errors.append(f"LAYER_RULE: {net} pour {i} on a forbidden copper layer")
            for j, region in enumerate(pour["filled_polygons"]):
                shape = (filled_region_bounds(region, rules.curve_tolerance) if geometry_context is None else
                         geometry_context.region_bounds(pour, j, region, rules.curve_tolerance))
                copper.append(_Copper(f"pour {i}.{j}", net, layers, shape))

        for i, keepout in enumerate(routes.get("keepouts", [])):
            shape = shape_from_spec({"type": "POLYGON", "points": keepout["points"]})
            layers = _layers(keepout, 12, rules)
            gap = number(keepout.get("clearance", 0), "keepout clearance", minimum=0)
            allowed_nets, types = keepout.get("nets"), keepout.get("net_types")
            if allowed_nets is not None and (not isinstance(allowed_nets, list) or not all(isinstance(x, str) for x in allowed_nets)):
                raise GeometryError("keepout.nets must be a net-name list")
            if types is not None and (not isinstance(types, list) or not all(isinstance(x, str) for x in types)):
                raise GeometryError("keepout.net_types must be a type-name list")
            keepouts.append((f"keepout {i}", shape.outer, layers, gap, set(allowed_nets) if allowed_nets is not None else None,
                             set(types) if types is not None else None))

        # Validate ALL terminal references before geometry connectivity, including 1-pin nets.
        resolved = {}
        for net, references in nets.items():
            net_string(net)
            if not isinstance(references, list) or not references:
                raise GeometryError(f"{net}: terminal list must be nonempty")
            resolved[net] = []
            for ref in references:
                key = (identifier(ref.get("compId"), "ref.compId"), identifier(ref.get("padId"), "ref.padId"))
                if key in resolved[net]:
                    errors.append(f"DUPLICATE_TERMINAL: {net} {key}")
                elif key not in pad_records:
                    errors.append(f"MISSING_TERMINAL: {net} {key}")
                elif pad_records[key] != net:
                    errors.append(f"NET_MISMATCH: {net} {key} has net {pad_records[key]!r}")
                else:
                    resolved[net].append(key)
        for key, net in pad_records.items():
            if net and key not in resolved.get(net, []):
                errors.append(f"NETLIST_INCOMPLETE: pad {key} missing from {net} terminal list")

        for drill in drills:
            boundary(drill.name + " drill", drill.bounds.outer, rules.board_edge_clearance)
        for i, drill in enumerate(drills):
            for other in drills[i + 1:]:
                if set(drill.layers).intersection(other.layers):
                    if drill.bounds.outer.intersects(other.bounds.outer) or drill.bounds.outer.distance(other.bounds.outer) < rules.hole_to_hole_clearance:
                        errors.append(f"HOLE_CLEARANCE: {drill.name} / {other.name}")

        # Drill voids are physical empty space, never solid copper contact.
        # Foreign copper may not be drilled away to disguise a short.
        drill_gap, drill_trees = None, None
        if (use_index and type(rules) is RuleConfig
                and type(rules.npth_clearance) in (int, float)):
            drill_gap = math.nextafter(float(rules.npth_clearance), math.inf)
            if math.isfinite(drill_gap):
                drill_trees = _layer_trees(drills)
        finished = []
        for item in copper:
            boundary(item.name, item.bounds.outer, rules.board_edge_clearance)
            for label, obstacle, layers, gap, net_filter, type_filter in keepouts:
                if not set(item.layers).intersection(layers):
                    continue
                if net_filter is not None and item.net not in net_filter:
                    continue
                if type_filter is not None and net_type(item.net) not in type_filter:
                    continue
                if item.bounds.outer.intersects(obstacle) or item.bounds.outer.distance(obstacle) < gap:
                    errors.append(f"KEEPOUT: {item.name} violates {label}")
            for layer in item.layers:
                inner, outer = item.bounds.inner, item.bounds.outer
                candidates = _foreign_drill_candidates(item, layer, drill_trees, drill_gap)
                for drill_index, drill in enumerate(drills):
                    if layer not in drill.layers:
                        continue
                    same_owner = drill.name == item.name
                    if (candidates is None or drill_index in candidates) and not same_owner and (not drill.plated or not item.net or item.net != drill.net):
                        gap = rules.npth_clearance if not drill.plated else 0
                        if outer.intersects(drill.bounds.outer) or outer.distance(drill.bounds.outer) < gap:
                            errors.append(f"DRILL_COPPER: {item.name} intersects/approaches {drill.name}")
                    inner = inner.difference(drill.bounds.outer)
                    outer = outer.difference(drill.bounds.inner)
                if inner.is_empty:
                    errors.append(f"EMPTY_COPPER: {item.name} on layer {layer} has no provable copper")
                finished.append(_Copper(item.name, item.net, (layer,), Bounds(inner, outer), item.terminal, item.barrel))

        for i, j in _clearance_pairs(finished, rules, use_index):
            item, other = finished[i], finished[j]
            if item.layers != other.layers or (item.net and item.net == other.net):
                continue
            required = rules.get_clearance(net_type(item.net), net_type(other.net))
            gap = item.bounds.outer.distance(other.bounds.outer)
            if gap < required or item.bounds.outer.intersects(other.bounds.outer):
                errors.append(f"COPPER_CLEARANCE: {item.name} / {other.name} layer {item.layers[0]}, lower-bound gap={gap:.9g}, required={required:g}")

        # Connected components use inner copper only; each island is a separate node.
        nodes = []
        for item in finished:
            for part in _pieces(item.bounds.inner):
                nodes.append((item, part))
        parent = list(range(len(nodes)))

        def find(i):
            while parent[i] != i:
                parent[i] = parent[parent[i]]
                i = parent[i]
            return i

        def join(a, b):
            a, b = find(a), find(b)
            if a != b:
                parent[b] = a

        barrel_holes = {d.name: d for d in drills if d.plated}
        for i, (item, geom) in enumerate(nodes):
            for j in range(i + 1, len(nodes)):
                other, other_geom = nodes[j]
                if not item.net or item.net != other.net:
                    continue
                if item.layers == other.layers:
                    contact = geom.intersection(other_geom)
                    if contact.area > rules.contact_tolerance ** 2 or contact.length > rules.contact_tolerance:
                        join(i, j)
                elif item.barrel and item.barrel == other.barrel:
                    hole = barrel_holes[item.barrel].bounds.outer
                    if geom.distance(hole) <= rules.contact_tolerance and other_geom.distance(hole) <= rules.contact_tolerance:
                        join(i, j)
        for net, references in resolved.items():
            terminal_roots = {}
            for key in references:
                roots = {find(i) for i, (item, _) in enumerate(nodes) if item.terminal == key and item.net == net}
                if len(roots) != 1:
                    errors.append(f"TERMINAL_COPPER: {net} {key} has {len(roots)} provable connected terminal regions")
                terminal_roots[key] = roots
            roots = set().union(*terminal_roots.values()) if terminal_roots else set()
            if len(roots) > 1:
                errors.append(f"DISCONNECTED: {net} required terminals are not one physical copper component")
            for i, (item, _) in enumerate(nodes):
                if item.net == net and find(i) not in roots:
                    errors.append(f"FLOATING_COPPER: {item.name} is not anchored to a declared {net} terminal")
        for item in finished:
            if item.net and not resolved.get(item.net):
                errors.append(f"UNANCHORED_NET: {item.name} has no valid declared terminals for {item.net}")
    except (GeometryError, GEOSException, ValueError, TypeError, KeyError, IndexError, AttributeError, OverflowError) as exc:
        errors.append(f"INVALID_INPUT: {exc}")
    return not errors, sorted(set(errors))


def main():
    parser = argparse.ArgumentParser(description="Offline PCB geometry/connectivity validation; lengths in mil")
    parser.add_argument("--input", type=Path, required=True, help="JSON snapshot containing components/routes/nets/tags/board_bbox")
    parser.add_argument("--output", type=Path, help="Optional JSON report")
    args = parser.parse_args()
    try:
        data = json.loads(args.input.read_text(encoding="utf-8"))
        known_fields = {"units", "board_bbox", "components", "nets", "tags", "routes", "rules", "metadata"}
        if any(key not in known_fields for key in data):
            raise GeometryError("unsupported top-level field; place supported rule tags in tags")
        raw = normalize_raw_pcb_data(data)
        overrides = dict(data.get("rules", {}))
        if "clearance_matrix" in overrides:
            overrides["clearance_matrix"] = {tuple(row[:2]): row[2] for row in overrides["clearance_matrix"]}
        ok, errors = verify_full_board_drc_and_connectivity(
            {c["id"]: c for c in raw["components"]}, data.get("routes", {}),
            data["nets"], data.get("tags", {}), data["board_bbox"], RuleConfig(**overrides))
    except (OSError, GEOSException, ValueError, TypeError, KeyError, IndexError, AttributeError) as exc:
        ok, errors = False, [f"INVALID_INPUT: {exc}"]
    report = {"passed": ok, "violations": errors, "units": "mil", "scope": "supplied geometric and connectivity rules"}
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(text + "\n", encoding="utf-8")
    print(text)
    raise SystemExit(0 if ok else 1)


if __name__ == "__main__":
    main()
