"""Generate actual solid copper fills and geometrically checked via fences."""
from copy import deepcopy
import math

from shapely.geometry import LineString
from shapely.ops import unary_union

from pcb_geometry import solid_polygon
from pcb_solver_hardened import _layers
from pcb_pipeline_common import (
    PipelineError, CollisionModel, normalize_board, rules_for, copper_and_drills,
    board_polygon, polygons, polygon_record, strict_options, finite, check_board,
)


def outer_offset(geom, distance):
    """Circumscribe buffer arcs; final validator independently checks the result."""
    if distance <= 0:
        return geom
    q = 32
    return geom.buffer(distance / math.cos(math.pi / (4 * q)) + 1e-7, quad_segs=q)


def generate_pours(board, requests=None):
    out = normalize_board(board)
    if requests is None:
        requests = []
    if not isinstance(requests, list):
        raise PipelineError("pours must be a list")
    stats = {"requested": len(requests), "generated": 0, "removed_floating_regions": 0, "area_mil2": 0.0}
    r = rules_for(out)
    for index, raw in enumerate(requests):
        req = strict_options(raw, {"net", "layer", "outline", "clearance", "min_area"}, f"pours[{index}]")
        net, layer = req.get("net"), req.get("layer")
        if not isinstance(net, str) or net not in out["nets"]:
            raise PipelineError("pour net needs declared physical terminals")
        if type(layer) is not int or layer not in r.copper_layers:
            raise PipelineError("pour layer must be a single configured copper layer")
        tag = out.get("tags", {}).get("net_tags", {}).get(net, {})
        if layer not in tag.get("allowed_layers", r.copper_layers):
            raise PipelineError(f"pour {net} uses forbidden layer {layer}")
        requested_gap = finite(req.get("clearance", 0), "pour.clearance", minimum=0)
        minimum_area = finite(req.get("min_area", 0), "pour.min_area", minimum=0)
        model = CollisionModel(out)
        boundary = board_polygon(out)
        # Extra geometric guard is a design margin, never a lowered DRC rule.
        guard = max(0.01, 4 * r.curve_tolerance)
        free = boundary.difference(outer_offset(boundary.boundary, r.board_edge_clearance + guard))
        if "outline" in req:
            try:
                free = free.intersection(solid_polygon(req["outline"]))
            except (ValueError, TypeError) as exc:
                raise PipelineError(f"invalid pour outline: {exc}") from exc
        copper, drills = copper_and_drills(out)
        blockers = []
        # Only already anchored physical groups are a ground truth. A floating
        # via may become connected through a new fill, but cannot certify it.
        from pcb_routing import _groups
        anchors = [g["layers"][layer] for g in _groups(out, net)
                   if g["terminals"] and layer in g["layers"]]
        for item in copper:
            if layer not in item["layers"]:
                continue
            if item["net"] == net:
                continue
            else:
                gap = max(requested_gap, r.get_clearance(model.net_type(net), model.net_type(item["net"])))
                blockers.append(outer_offset(item["outer"], gap + guard))
        for drill in drills:
            if layer in drill["layers"]:
                gap = r.npth_clearance if not drill["plated"] else 0
                blockers.append(outer_offset(drill["outer"], gap + guard))
        for geom, layers, gap, nets, types in model.keepouts:
            if layer not in layers or (nets is not None and net not in nets):
                continue
            if types is not None and model.net_type(net) not in types:
                continue
            blockers.append(outer_offset(geom, gap + guard))
        if blockers:
            free = free.difference(unary_union(blockers))
        anchor = unary_union(anchors)
        accepted = []
        pieces = polygons(free)
        if len(pieces) > 4096:
            raise PipelineError("pour fragmentation exceeds 4096 regions")
        for piece in pieces:
            contact = piece.intersection(anchor)
            if piece.area <= minimum_area or (contact.area <= r.contact_tolerance ** 2 and contact.length <= r.contact_tolerance):
                stats["removed_floating_regions"] += 1
                continue
            accepted.append(polygon_record(piece))
            stats["area_mil2"] += piece.area
        if not accepted:
            raise PipelineError(f"pour {net} on layer {layer}: no nonempty anchored fill")
        out.setdefault("routes", {}).setdefault("copper_pours", []).append(
            {"net": net, "layer": layer, "filled_polygons": accepted})
        errors = check_board(out, ignore_connectivity=True)
        if errors:
            raise PipelineError("generated pour failed independent DRC: " + "; ".join(errors))
        stats["generated"] += 1
    return out, stats


def generate_shielding(board, options=None):
    cfg = strict_options(options, {"enabled", "nets", "ground_net", "spacing", "offset",
        "diameter", "hole", "layers", "minimum_vias"}, "shielding")
    out = normalize_board(board)
    enabled = cfg.get("enabled", bool(cfg.get("nets")))
    if type(enabled) is not bool:
        raise PipelineError("shielding.enabled must be boolean")
    stats = {"enabled": enabled, "generated": 0, "rejected_candidates": 0,
             "scope": "geometric via fence only; electromagnetic shielding is not certified"}
    if not enabled:
        return out, stats
    r = rules_for(out)
    nets, ground = cfg.get("nets", []), cfg.get("ground_net", "GND")
    if (not isinstance(nets, list) or not nets or any(not isinstance(n, str) or not n for n in nets)
            or len(set(nets)) != len(nets) or any(n not in out["nets"] for n in nets)):
        raise PipelineError("shielding.nets must be a unique nonempty list of declared nets")
    if not isinstance(ground, str) or ground not in out["nets"] or ground in nets:
        raise PipelineError("shielding needs a separate declared ground_net")
    spacing = finite(cfg.get("spacing", 40), "shielding.spacing", positive=True)
    offset = finite(cfg.get("offset", 30), "shielding.offset", positive=True)
    hole = finite(cfg.get("hole", r.default_via_drill), "shielding.hole", positive=True)
    diameter = finite(cfg.get("diameter", r.default_via_diameter), "shielding.diameter", positive=True)
    layers = cfg.get("layers", list(r.copper_layers))
    if not isinstance(layers, list) or len(layers) < 2:
        raise PipelineError("shield via layers must span at least two copper layers")
    try:
        layers = list(_layers({"layers": layers}, None, r, spanning=True))
    except (ValueError, TypeError) as exc:
        raise PipelineError(f"invalid shield layers: {exc}") from exc
    minimum = cfg.get("minimum_vias", 1)
    if type(minimum) is not int or minimum < 1:
        raise PipelineError("shielding.minimum_vias must be a positive integer")
    existing = out["routes"].get("vias", []) + out["routes"].get("shield_vias", [])
    seen = {(v["x"], v["y"], tuple(v.get("layers", r.copper_layers))) for v in existing}
    target = out["routes"].setdefault("shield_vias", [])
    for net in nets:
        generated = 0
        traces = [t for t in out["routes"].get("traces", []) if t["net"] == net]
        if not traces:
            raise PipelineError(f"shielding {net}: no routed line to follow")
        for trace in traces:
            if _layers(trace, None, r)[0] not in layers:
                continue
            dx, dy = trace["ex"] - trace["sx"], trace["ey"] - trace["sy"]
            length = math.hypot(dx, dy)
            if length == 0:
                continue
            count = max(1, math.ceil(length / spacing))
            if count > 10000:
                raise PipelineError("shield via sampling exceeds budget")
            nx, ny = -dy / length, dx / length
            for i in range(count + 1):
                for side in (-1, 1):
                    x = trace["sx"] + dx * i / count + side * nx * offset
                    y = trace["sy"] + dy * i / count + side * ny * offset
                    key = (x, y, tuple(layers))
                    if key in seen:
                        continue
                    model = CollisionModel(out)
                    if not model.via_ok(ground, x, y, hole, diameter, layers):
                        stats["rejected_candidates"] += 1
                        continue
                    target.append(dict(net=ground, x=x, y=y, hole=hole, diameter=diameter, layers=layers))
                    seen.add(key)
                    generated += 1
        if generated < minimum:
            raise PipelineError(f"shielding {net}: only {generated} vias generated, required {minimum}")
        stats["generated"] += generated
    # Vias intentionally may await following ground pours; final pipeline must
    # prove they are connected. This function never claims that they shield EMI.
    errors = check_board(out, ignore_connectivity=True)
    if errors:
        raise PipelineError("shield via DRC: " + "; ".join(errors))
    return out, stats
