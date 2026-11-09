"""Explicit executable constraint bus; natural-language rules are never guessed."""
from copy import deepcopy
import math

from shapely.geometry import LineString, Point, box
from shapely.ops import unary_union

from pcb_solver_hardened import _body, _pad_bounds, _layers
from pcb_pipeline_common import PipelineError, finite, normalize_board, rules_for, copper_and_drills
from pcb_width_policy import hard_width_minimum, selected_width


_FIELDS = {
    "ZONE_ANCHOR": {"operator", "component", "region"},
    "RELATIVE_POSE": {"operator", "source", "target", "max_distance", "same_layer"},
    "REPEL_FIELD": {"operator", "source", "target", "min_distance"},
    "CHANNEL_SPEC": {"operator", "net", "min_width", "allowed_layers", "allow_via", "alpha_length", "via_cost",
                     "diff_pair", "length_group", "current_a", "temp_rise_c", "copper_oz"},
    "COMPANION_GEN": {"operator", "nets", "ground_net", "spacing", "offset", "diameter", "hole", "layers", "minimum_vias"},
    "TOPOLOGY_ORDER": {"operator", "net", "ordered_pins"},
}


def _pin(value):
    if (not isinstance(value, dict) or set(value) != {"compId", "padId"}
            or not all(isinstance(v, str) and v for v in value.values())):
        raise PipelineError("constraint pin needs exactly nonempty compId/padId strings")
    return value["compId"], value["padId"]


def _references(board):
    components = {c["id"]: c for c in board["components"]}
    pads = {(c["id"], p["padId"]): (c, p) for c in board["components"] for p in c["pads"]}
    return components, pads


def _rules(board, constraints, construction=False):
    if not isinstance(constraints, list):
        raise PipelineError("constraints must be an explicit structured rule list")
    components, pads = _references(board)
    rules = rules_for(board)
    result = deepcopy(constraints)

    def comp(cid):
        if not isinstance(cid, str) or not cid:
            raise PipelineError("constraint component must be a nonempty component ID")
        if cid not in components and not construction:
            raise PipelineError(f"constraint references unknown component {cid}")

    def pin(value):
        key = _pin(value)
        if key not in pads and not construction:
            raise PipelineError(f"constraint references unknown pad {key}")
        return key

    def net(value):
        if not isinstance(value, str) or not value:
            raise PipelineError("constraint net must be a nonempty string")
        if value not in board["nets"] and not construction:
            raise PipelineError(f"constraint references unknown net {value}")

    for rule in result:
        if not isinstance(rule, dict) or rule.get("operator") not in _FIELDS:
            raise PipelineError("unknown or unsupported constraint operator")
        op = rule["operator"]
        if set(rule) - _FIELDS[op]:
            raise PipelineError(f"{op}: unknown/unsupported fields")
        required = _FIELDS[op] if op != "CHANNEL_SPEC" else {"operator", "net"}
        if required - set(rule):
            raise PipelineError(f"{op}: missing fields {sorted(required - set(rule))}")
        if op == "ZONE_ANCHOR":
            comp(rule["component"])
            region = rule["region"]
            if not isinstance(region, list) or len(region) != 4:
                raise PipelineError("ZONE_ANCHOR region needs [xmin,ymin,xmax,ymax]")
            rule["region"] = [finite(v, "ZONE_ANCHOR region") for v in region]
            if region[0] >= region[2] or region[1] >= region[3]:
                raise PipelineError("ZONE_ANCHOR region is empty")
        elif op == "RELATIVE_POSE":
            pin(rule["source"])
            pin(rule["target"])
            rule["max_distance"] = finite(rule["max_distance"], "RELATIVE_POSE max_distance", minimum=0)
            if type(rule["same_layer"]) is not bool:
                raise PipelineError("RELATIVE_POSE same_layer must be boolean")
        elif op == "REPEL_FIELD":
            comp(rule["source"])
            comp(rule["target"])
            if rule["source"] == rule["target"]:
                raise PipelineError("REPEL_FIELD needs two distinct components")
            rule["min_distance"] = finite(rule["min_distance"], "REPEL_FIELD min_distance", minimum=0)
        elif op == "CHANNEL_SPEC":
            net(rule["net"])
            if len(rule) < 3:
                raise PipelineError("CHANNEL_SPEC needs at least one executable channel parameter")
            for key in ("min_width", "alpha_length", "via_cost"):
                if key in rule:
                    rule[key] = finite(rule[key], f"CHANNEL_SPEC {key}", positive=key != "via_cost", minimum=0)
            if "allow_via" in rule and type(rule["allow_via"]) is not bool:
                raise PipelineError("CHANNEL_SPEC allow_via must be boolean")
            if "allowed_layers" in rule:
                rule["allowed_layers"] = list(_layers({"layers": rule["allowed_layers"]}, None, rules))
            from pcb_differential import validate_channel_quality
            validate_channel_quality(board, rule, construction)
            if {"current_a", "temp_rise_c", "copper_oz"} & set(rule):
                from pcb_ampacity import validate_current_rule
                validate_current_rule(rule)
        elif op == "COMPANION_GEN":
            if (not isinstance(rule["nets"], list) or not rule["nets"]
                    or len(set(rule["nets"])) != len(rule["nets"])):
                raise PipelineError("COMPANION_GEN nets must be a unique nonempty list")
            for value in rule["nets"]:
                net(value)
            net(rule["ground_net"])
            if rule["ground_net"] in rule["nets"]:
                raise PipelineError("COMPANION_GEN ground_net must be separate from target nets")
            for key in ("spacing", "offset", "diameter", "hole"):
                rule[key] = finite(rule[key], f"COMPANION_GEN {key}", positive=True)
            if (rule["hole"] < rules.minimum_drill or rule["diameter"] <= rule["hole"]
                    or (rule["diameter"] - rule["hole"]) / 2 < rules.min_annular_ring):
                raise PipelineError("COMPANION_GEN invalid drill/annular ring")
            rule["layers"] = list(_layers({"layers": rule["layers"]}, None, rules, spanning=True))
            if len(rule["layers"]) < 2:
                raise PipelineError("COMPANION_GEN vias need at least two physical copper layers")
            if type(rule["minimum_vias"]) is not int or rule["minimum_vias"] < 1:
                raise PipelineError("COMPANION_GEN minimum_vias must be a positive integer")
        elif op == "TOPOLOGY_ORDER":
            net(rule["net"])
            ordered = rule["ordered_pins"]
            if not isinstance(ordered, list) or len(ordered) < 2:
                raise PipelineError("TOPOLOGY_ORDER needs at least two ordered pins")
            keys = [pin(p) for p in ordered]
            if len(set(keys)) != len(keys):
                raise PipelineError("TOPOLOGY_ORDER repeats a pin")
            if not construction:
                declared = {_pin(p) for p in board["nets"][rule["net"]]}
                if set(keys) != declared:
                    raise PipelineError("TOPOLOGY_ORDER must cover exactly all declared pins of its net")
                if any(pads[key][1]["net"] != rule["net"] for key in keys):
                    raise PipelineError("TOPOLOGY_ORDER pin/net mismatch")
    from pcb_differential import declarations
    declarations(result)
    return result


def compile_constraints(board, options, constraints):
    """Validate references and lower executable rules without moving geometry."""
    out = normalize_board(board)
    if options is None:
        options = {}
    if not isinstance(options, dict):
        raise PipelineError("constraint options must be an object")
    cfg = deepcopy(options)
    for stage in ("placement", "routing", "shielding"):
        if stage in cfg and not isinstance(cfg[stage], dict):
            raise PipelineError(f"constraint options.{stage} must be an object")
    metadata = out.setdefault("metadata", {})
    if not isinstance(metadata, dict):
        raise PipelineError("board.metadata must be an object")
    existing = metadata.get("pipeline_constraints", [])
    if constraints is None:
        constraints = existing
    elif existing and existing != constraints:
        raise PipelineError("explicit constraints conflict with saved pipeline_constraints")
    try:
        compiled = _rules(out, constraints)
    except (ValueError, TypeError, KeyError, OverflowError) as exc:
        if isinstance(exc, PipelineError):
            raise
        raise PipelineError(f"invalid constraints: {exc}") from exc
    from pcb_differential import declarations
    pairs, _ = declarations(compiled)
    # 差分目标宽度本身是两网的显式声明；先登记，载流合并不受规则排列顺序影响。
    for pair in pairs:
        for net in (pair["p"], pair["n"]):
            tag = out.setdefault("tags", {}).setdefault("net_tags", {}).setdefault(net, {})
            tag["min_width"] = max(tag.get("min_width", 0), pair["width"])
    companion_seen = False
    current_calculations = []
    for rule in compiled:
        op = rule["operator"]
        if op == "ZONE_ANCHOR":
            regions = cfg.setdefault("placement", {}).setdefault("regions", {})
            if not isinstance(regions, dict):
                raise PipelineError("placement.regions must be an object")
            cid, region = rule["component"], rule["region"]
            if cid in regions:
                previous = regions[cid]
                if not isinstance(previous, (list, tuple)) or len(previous) != 4:
                    raise PipelineError("placement region conflicts with ZONE_ANCHOR")
                previous = [finite(v, "placement region") for v in previous]
                region = [max(previous[0], region[0]), max(previous[1], region[1]),
                          min(previous[2], region[2]), min(previous[3], region[3])]
                if region[0] >= region[2] or region[1] >= region[3]:
                    raise PipelineError("ZONE_ANCHOR regions have no common area")
            regions[cid] = region
        elif op == "CHANNEL_SPEC":
            tag = out.setdefault("tags", {}).setdefault("net_tags", {}).setdefault(rule["net"], {})
            for key in ("alpha_length", "via_cost"):
                if key in rule:
                    if key in tag and tag[key] != rule[key]:
                        raise PipelineError(f"CHANNEL_SPEC conflicts with existing {key}")
                    tag[key] = rule[key]
            if "min_width" in rule:
                tag["min_width"] = max(tag.get("min_width", 0), rule["min_width"])
            if "allow_via" in rule:
                tag["allow_via"] = tag.get("allow_via", True) and rule["allow_via"]
            if "allowed_layers" in rule:
                layers = [v for v in rule["allowed_layers"] if v in tag.get("allowed_layers", rule["allowed_layers"])]
                if not layers:
                    raise PipelineError("CHANNEL_SPEC has no allowed copper layer")
                tag["allowed_layers"] = layers
            if "current_a" in rule:
                from pcb_ampacity import apply_current_rule
                evidence = apply_current_rule(out, rule, tag)
                if evidence is not None:
                    current_calculations.append(evidence)
        elif op == "COMPANION_GEN":
            shield = {k: deepcopy(v) for k, v in rule.items() if k != "operator"}
            shield["enabled"] = True
            previous = cfg.get("shielding", {})
            if not isinstance(previous, dict):
                raise PipelineError("COMPANION_GEN conflicts with shielding options")
            for key, value in previous.items():
                if key == "nets" and companion_seen:
                    continue
                if key not in shield or shield[key] != value:
                    raise PipelineError(f"COMPANION_GEN conflicts with shielding.{key}")
            if companion_seen:
                shield["nets"] = list(dict.fromkeys(previous["nets"] + shield["nets"]))
            cfg["shielding"] = shield
            companion_seen = True
        elif op == "TOPOLOGY_ORDER":
            orders = cfg.setdefault("routing", {}).setdefault("topology_orders", {})
            if not isinstance(orders, dict):
                raise PipelineError("routing.topology_orders must be an object")
            if rule["net"] in orders and orders[rule["net"]] != rule["ordered_pins"]:
                raise PipelineError("conflicting TOPOLOGY_ORDER for one net")
            orders[rule["net"]] = deepcopy(rule["ordered_pins"])
    for pair in pairs:
        for net in (pair["p"], pair["n"]):
            tag = out.setdefault("tags", {}).setdefault("net_tags", {}).setdefault(net, {})
            minimum = hard_width_minimum(out, net)
            if pair["width"] < minimum:
                raise PipelineError(f"差分width低于{net}现有/电流/规则线宽下限")
            tag["min_width"] = max(tag.get("min_width", 0), pair["width"])
    # 布局只消费实际选宽的估算副本，不把软拥塞参数写入制造规则。
    widths = {net: pair["width"] for pair in pairs for net in (pair["p"], pair["n"])}
    route_width = cfg.get("routing", {}).get("width")
    if "width" in cfg.get("routing", {}):
        route_width = finite(route_width, "routing.width", positive=True)
    if route_width is not None and any(abs(route_width-width) > rules_for(out).contact_tolerance
                                       for width in widths.values()):
        raise PipelineError("全局routing.width与差分width冲突")
    routing_cfg = cfg.get("routing", {})
    deferred = set(routing_cfg.get("ground_nets", [])) if routing_cfg.get("ground_strategy") == "plane_then_drc" else set()
    resolved = {net: selected_width(out, net, None if net in deferred else widths.get(net, route_width))
                for net in out["nets"]}
    placement = cfg.setdefault("placement", {})
    declared = placement.get("rudy_widths", {})
    if not isinstance(declared, dict) or set(declared)-set(resolved):
        raise PipelineError("placement.rudy_widths只能引用真实网络")
    if any(abs(finite(width, "rudy_widths", positive=True)-resolved[net]) > rules_for(out).contact_tolerance
           for net, width in declared.items()):
        raise PipelineError("placement.rudy_widths与实际布线选宽冲突")
    placement["rudy_widths"] = resolved
    if current_calculations:
        metadata["current_width_calculations"] = current_calculations
    for net, tag in out.get("tags", {}).get("net_tags", {}).items():
        if finite(tag.get("beta_repel", 0), f"{net}.beta_repel", minimum=0) != 0:
            raise PipelineError(f"unsupported nonzero beta_repel for {net}; use explicit REPEL_FIELD")
        if "need_shield_check" in tag and type(tag["need_shield_check"]) is not bool:
            raise PipelineError(f"{net}.need_shield_check must be boolean")
        if tag.get("need_shield_check", False):
            companions = [r for r in compiled if r["operator"] == "COMPANION_GEN" and net in r["nets"]]
            if not companions:
                raise PipelineError(f"{net}.need_shield_check needs an explicit COMPANION_GEN")
    metadata["pipeline_constraints"] = compiled
    return out, cfg, {"compiled": len(compiled), "operators": sorted({r["operator"] for r in compiled})}


def _parts(geometry):
    if geometry.geom_type == "Polygon" and not geometry.is_empty and geometry.area > 0:
        yield geometry
    elif hasattr(geometry, "geoms"):
        for item in geometry.geoms:
            yield from _parts(item)


def _graph(copper, drills, tolerance, cut=None):
    """Independent geometric graph, optionally removing an intermediate pad."""
    cut_shapes = {}
    if cut is not None:
        for item in copper:
            if item["terminal"] == cut:
                for layer in item["layers"]:
                    cut_shapes.setdefault(layer, []).append(item["inner"])
        cut_shapes = {layer: unary_union(parts) for layer, parts in cut_shapes.items()}
    nodes = []
    for item in copper:
        if item["terminal"] == cut and cut is not None:
            continue
        for layer in item["layers"]:
            geometry = item["inner"]
            if layer in cut_shapes:
                geometry = geometry.difference(cut_shapes[layer])
            nodes.extend((item, layer, part) for part in _parts(geometry))
    roots = list(range(len(nodes)))

    def root(i):
        while roots[i] != i:
            roots[i] = roots[roots[i]]
            i = roots[i]
        return i

    holes = {d["name"]: d["outer"] for d in drills if d["plated"]}
    for i, (item, layer, geometry) in enumerate(nodes):
        for j in range(i):
            other, other_layer, other_geometry = nodes[j]
            contact = False
            if layer == other_layer:
                overlap = geometry.intersection(other_geometry)
                contact = overlap.area > tolerance ** 2 or overlap.length > tolerance
            elif item.get("barrel") and item.get("barrel") == other.get("barrel"):
                hole = holes.get(item["barrel"])
                contact = (hole is not None and geometry.distance(hole) <= tolerance
                           and other_geometry.distance(hole) <= tolerance)
            if contact:
                roots[root(i)] = root(j)
    terminal_roots = {}
    for i, (item, _, _) in enumerate(nodes):
        if item["terminal"] is not None:
            terminal_roots.setdefault(tuple(item["terminal"]), set()).add(root(i))
    return terminal_roots


def topology_errors(board, net, ordered_pins):
    keys = [_pin(p) for p in ordered_pins]
    copper, drills = copper_and_drills(board)
    copper = [item for item in copper if item["net"] == net]
    tolerance = rules_for(board).contact_tolerance
    all_roots = _graph(copper, drills, tolerance)
    if any(len(all_roots.get(key, set())) != 1 for key in keys):
        return [f"TOPOLOGY_ORDER: {net} terminal copper is missing or disconnected"]
    if len(set().union(*(all_roots[key] for key in keys))) != 1:
        return [f"TOPOLOGY_ORDER: {net} ordered pins are not physically connected"]
    for index, middle in enumerate(keys[1:-1], 1):
        roots = _graph(copper, drills, tolerance, middle)
        start, end = roots.get(keys[0], set()), roots.get(keys[-1], set())
        if len(start) != 1 or len(end) != 1 or start & end:
            return [f"TOPOLOGY_ORDER: {net} has a physical bypass around {middle}"]
        if (any(roots.get(key, set()) != start for key in keys[:index])
                or any(roots.get(key, set()) != end for key in keys[index + 1:])):
            return [f"TOPOLOGY_ORDER: {net} earlier/later pins are not on opposite sides of {middle}"]
    return []


def check_constraints(board, construction=False, defer_companions=False):
    """Evaluate rules; routing may defer only companions to their later stage.

    Construction also omits missing-subboard refs and topology. Neither mode
    weakens already present placement/channel geometry constraints.
    """
    try:
        constraints = board.get("metadata", {}).get("pipeline_constraints", [])
        constraints = _rules(board, constraints, construction)
        components, pads = _references(board)
        rules = rules_for(board)
        errors = []
        for net, tag in board.get("tags", {}).get("net_tags", {}).items():
            if finite(tag.get("beta_repel", 0), f"{net}.beta_repel", minimum=0) != 0:
                errors.append(f"UNSUPPORTED_RULE: nonzero beta_repel for {net}")
            if "need_shield_check" in tag and type(tag["need_shield_check"]) is not bool:
                errors.append(f"CONSTRAINT_INPUT: {net}.need_shield_check must be boolean")
            elif tag.get("need_shield_check", False) and not any(
                    r["operator"] == "COMPANION_GEN" and net in r["nets"] for r in constraints):
                errors.append(f"UNSUPPORTED_RULE: {net}.need_shield_check needs COMPANION_GEN")
        for rule in constraints:
            op = rule["operator"]
            if op == "ZONE_ANCHOR" and rule["component"] in components:
                c = components[rule["component"]]
                shapes = [_body(c, rules.curve_tolerance).outer]
                shapes += [_pad_bounds(c, p, rules.curve_tolerance).outer for p in c["pads"]]
                if not box(*rule["region"]).covers(unary_union(shapes)):
                    errors.append(f"ZONE_ANCHOR: {rule['component']} body/pads leave region")
            elif op == "RELATIVE_POSE":
                a, b = _pin(rule["source"]), _pin(rule["target"])
                if a not in pads or b not in pads:
                    continue
                ca, pa = pads[a]
                cb, pb = pads[b]
                x = _pad_bounds(ca, pa, rules.curve_tolerance).inner.centroid
                y = _pad_bounds(cb, pb, rules.curve_tolerance).inner.centroid
                if x.distance(y) > rule["max_distance"] + rules.contact_tolerance:
                    errors.append(f"RELATIVE_POSE: {a}/{b} exceed max_distance")
                if rule["same_layer"] and not set(_layers(pa, ca["layer"], rules)) & set(_layers(pb, cb["layer"], rules)):
                    errors.append(f"RELATIVE_POSE: {a}/{b} have no shared copper layer")
            elif op == "REPEL_FIELD":
                if rule["source"] not in components or rule["target"] not in components:
                    continue
                a, b = components[rule["source"]], components[rule["target"]]
                gap = _body(a, rules.curve_tolerance).outer.distance(_body(b, rules.curve_tolerance).outer)
                if gap + rules.contact_tolerance < rule["min_distance"]:
                    errors.append(f"REPEL_FIELD: {rule['source']}/{rule['target']} gap {gap:g} below minimum")
            elif op == "CHANNEL_SPEC":
                if "current_a" in rule and (not construction or rule["net"] in board["nets"]):
                    from pcb_ampacity import current_errors
                    errors.extend(current_errors(board, rule))
                for kind in ("traces", "vias", "shield_vias", "copper_pours"):
                    for item in board.get("routes", {}).get(kind, []):
                        if item["net"] != rule["net"]:
                            continue
                        layers = _layers(item, 12 if kind in ("vias", "shield_vias") else None, rules)
                        if "allowed_layers" in rule and not set(layers).issubset(rule["allowed_layers"]):
                            errors.append(f"CHANNEL_SPEC: {rule['net']} uses forbidden layer")
                        if kind == "traces" and item["width"] < rule.get("min_width", 0):
                            errors.append(f"CHANNEL_SPEC: {rule['net']} trace is too narrow")
                        if kind in ("vias", "shield_vias") and rule.get("allow_via") is False:
                            errors.append(f"CHANNEL_SPEC: {rule['net']} uses forbidden via")
            elif op == "TOPOLOGY_ORDER" and not construction:
                errors.extend(topology_errors(board, rule["net"], rule["ordered_pins"]))
            elif op == "COMPANION_GEN" and not construction and not defer_companions:
                for net in rule["nets"]:
                    lines = [LineString([(t["sx"], t["sy"]), (t["ex"], t["ey"])])
                             for t in board.get("routes", {}).get("traces", [])
                             if t["net"] == net and t["layer"] in rule["layers"]]
                    count = set()
                    def follows(line, point):
                        (sx, sy), (ex, ey) = line.coords
                        dx, dy = ex - sx, ey - sy
                        length = math.hypot(dx, dy)
                        if length == 0:
                            return False
                        along = ((point.x - sx) * dx + (point.y - sy) * dy) / length
                        perpendicular = abs((point.x - sx) * dy - (point.y - sy) * dx) / length
                        tolerance = 4 * rules.curve_tolerance
                        return (-tolerance <= along <= length + tolerance
                                and abs(perpendicular - rule["offset"]) <= tolerance)
                    for via in board.get("routes", {}).get("shield_vias", []):
                        if (via["net"] != rule["ground_net"] or via["hole"] != rule["hole"]
                                or via["diameter"] != rule["diameter"]
                                or tuple(_layers(via, 12, rules, spanning=True)) != tuple(rule["layers"])):
                            continue
                        point = Point(via["x"], via["y"])
                        if any(follows(line, point) for line in lines):
                            count.add((via["x"], via["y"], tuple(rule["layers"])))
                    if len(count) < rule["minimum_vias"]:
                        errors.append(f"COMPANION_GEN: {net} has {len(count)} physical companion vias; needs {rule['minimum_vias']}")
        if not construction:
            from pcb_differential import quality_report
            errors.extend(quality_report(board, constraints)["errors"])
        return sorted(set(errors))
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError) as exc:
        return [f"CONSTRAINT_INPUT: {exc}"]
