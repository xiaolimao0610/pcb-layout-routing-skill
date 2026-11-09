"""有限成对通孔换层与独立实体路径核验，不推断电气延迟或原生回流通过。"""
from __future__ import annotations

from collections import defaultdict
import math
import time

from shapely.geometry import LineString, Point
from shapely.ops import linemerge, unary_union

from pcb_pipeline_common import CollisionModel, PipelineError, finite, rules_for, strict_options, _layers


def validate_options(value=None):
    """输入可选字典，返回补齐默认的配置；未知字段、非法类型或预算抛PipelineError。"""
    result = strict_options(value, {"enabled", "max_candidates", "max_transitions",
                                   "lead_distance", "search_step", "max_seconds"}, "diff_layer_change")
    result.setdefault("enabled", False)
    if type(result["enabled"]) is not bool:
        raise PipelineError("diff_layer_change.enabled 必须为布尔值")
    for key, default, limit in (("max_candidates", 128, 4096), ("max_transitions", 2, 2)):
        result.setdefault(key, default)
        if type(result[key]) is not int or not 1 <= result[key] <= limit:
            raise PipelineError(f"diff_layer_change.{key} 必须为1..{limit}整数")
    for key, default in (("lead_distance", 120), ("search_step", 20), ("max_seconds", 10)):
        result[key] = finite(result.get(key, default), f"diff_layer_change.{key}", positive=True)
    return result


def _line_parts(shape):
    if shape.geom_type == "LineString":
        return [shape]
    if shape.geom_type == "MultiLineString":
        return list(shape.geoms)
    raise PipelineError("DIFF_PATH_STRUCTURE: 路径不能表示为实体直线链")


def measured_multilayer_path(board, net):
    """从实际铜线和全栈通孔重建双端无分支层间链。

    入参完整规范板及网名；返回有序分层路径、过孔、平面长度和未知孔段。
    先使用原铜连通分组证明接触，再验证唯一中心线链；不读取生成元数据。
    仅支持1..2个换层孔、完整层栈、无铜区；断连、分支、捷径及超范围均抛异常。
    """
    from pcb_differential import _terminals, _simplified_coords
    from pcb_routing import _groups
    rules = rules_for(board)
    tolerance = rules.contact_tolerance
    terminals = _terminals(board, net)
    routes = board["routes"]
    if any(item["net"] == net for kind in ("shield_vias", "copper_pours") for item in routes.get(kind, [])):
        raise PipelineError(f"DIFF_PATH_UNSUPPORTED: {net} 含屏蔽孔或铜区，无法证明唯一路径")
    vias = [v for v in routes.get("vias", []) if v["net"] == net]
    records = [(i, t) for i, t in enumerate(routes.get("traces", [])) if t["net"] == net]
    if not 1 <= len(vias) <= 2 or not records or len(records) > 4096:
        raise PipelineError(f"DIFF_PATH_UNSUPPORTED: {net} 仅支持1..2个通孔及1..4096段实际线")
    stack = tuple(rules.copper_layers)
    for via in vias:
        if set(_layers(via, 12, rules, spanning=True)) != set(stack):
            raise PipelineError(f"DIFF_PATH_UNSUPPORTED: {net} 不是完整叠层通孔")
    groups = _groups(board, net)
    if len(groups) != 1 or len(groups[0]["terminals"]) != 2:
        raise PipelineError(f"DIFF_PATH_DISCONNECTED: {net} 原实体铜检查未证明完整双端连接")
    per_layer = defaultdict(list)
    for index, trace in records:
        per_layer[trace["layer"]].append((index, trace))
    edges, adjacency = [], defaultdict(list)

    def endpoint(point, layer):
        found = [("t", i) for i, terminal in enumerate(terminals)
                 if layer in terminal["layers"] and terminal["inner"].distance(Point(point)) <= tolerance]
        found += [("v", i) for i, via in enumerate(vias)
                  if math.dist(point, (via["x"], via["y"])) <= tolerance]
        if len(found) != 1:
            raise PipelineError(f"DIFF_PATH_ENDPOINT: {net} 端点不唯一对应真实焊盘或孔中心")
        return found[0]

    for layer, layer_records in per_layer.items():
        originals = [LineString([(t["sx"], t["sy"]), (t["ex"], t["ey"])]) for _, t in layer_records]
        union = unary_union(originals)
        if abs(union.length-sum(line.length for line in originals)) > tolerance:
            raise PipelineError(f"DIFF_PATH_OVERLAP: {net} 同层线段重叠，长度不唯一")
        merged = union if union.geom_type == "LineString" else linemerge(union)
        for line in _line_parts(merged):
            if not line.is_simple or line.is_ring:
                raise PipelineError(f"DIFF_PATH_STRUCTURE: {net} 含环或交叉")
            nodes = [endpoint(point, layer) for point in (line.coords[0], line.coords[-1])]
            if nodes[0] == nodes[1]:
                raise PipelineError(f"DIFF_PATH_STRUCTURE: {net} 返回同一端点")
            indices = [i for i, t in layer_records if line.covers(LineString([(t["sx"], t["sy"]), (t["ex"], t["ey"])]))]
            widths = [float(routes["traces"][i]["width"]) for i in indices]
            if not widths:
                raise PipelineError("DIFF_PATH_STRUCTURE: 无法对应实际铜线")
            max_width = max(widths)
            coords = _simplified_coords(line)
            segments = [LineString([a, b]) for a, b in zip(coords, coords[1:])]
            for i, first in enumerate(segments):
                for second in segments[i+1:]:
                    adjacent = any(math.dist(a,b) <= tolerance for a in first.coords for b in second.coords)
                    if not adjacent and first.distance(second) <= max_width+rules.default_clearance+tolerance:
                        raise PipelineError(f"DIFF_PATH_SHORTCUT: {net} 非相邻铜段形成捷径或缺少自间距")
            edge = dict(line=line, layer=layer, length=float(line.length), widths=widths,
                        trace_indices=indices, nodes=nodes)
            edge_index = len(edges)
            edges.append(edge)
            for node in nodes:
                adjacency[node].append(edge_index)
    # 同一层可能在两次换层之间出现多个不相邻分段。即使中心线不交叉，
    # 有限线宽铜也可能短接这两段，绕过中间层；原同网DRC不会拒绝该捷径。
    for index, first in enumerate(edges):
        for second in edges[index+1:]:
            if first["layer"] != second["layer"]:
                continue
            separation = (max(first["widths"])+max(second["widths"]))/2+rules.default_clearance
            if first["line"].distance(second["line"]) <= separation+tolerance:
                raise PipelineError(f"DIFF_PATH_SHORTCUT: {net} 同层不同换层分段形成铜捷径或缺少自间距")
    expected = {(kind, i): count for kind, amount, count in (("t", 2, 1), ("v", len(vias), 2)) for i in range(amount)}
    if set(adjacency) != set(expected) or any(len(adjacency[node]) != degree for node, degree in expected.items()):
        raise PipelineError(f"DIFF_PATH_BRANCH: {net} 含分支、悬空孔或缺失接入")
    for i in range(len(vias)):
        if len({edges[j]["layer"] for j in adjacency[("v", i)]}) != 2:
            raise PipelineError(f"DIFF_PATH_LAYER: {net} 过孔没有连接两个不同走线层")

    # 大焊盘及通孔铜环也可能在中途短接另一段线；原异网DRC不会替代这项检查。
    for edge in edges:
        line, layer = edge["line"], edge["layer"]
        width = max(edge["widths"])
        contacts = []
        for i, terminal in enumerate(terminals):
            if layer in terminal["layers"]:
                contacts.append((("t", i), terminal["inner"].buffer(width/2+tolerance)))
        contacts += [(("v", i), Point(v["x"], v["y"]).buffer(v["diameter"]/2+width/2+tolerance))
                     for i, v in enumerate(vias)]
        for node, region in contacts:
            contact = line.intersection(region)
            if contact.is_empty:
                continue
            if node not in edge["nodes"]:
                raise PipelineError(f"DIFF_PATH_SHORTCUT: {net} 路径中途进入其他端子或孔铜")
            intervals = []
            parts = [contact] if contact.geom_type == "LineString" else list(getattr(contact, "geoms", []))
            for part in parts:
                if part.geom_type == "LineString" and not part.is_empty:
                    distances = [line.project(Point(point)) for point in part.coords]
                    intervals.append((min(distances), max(distances)))
            intervals.sort()
            if not intervals:
                raise PipelineError(f"DIFF_PATH_CONTACT: {net} 接触仅为点，不能证明路径")
            lo, hi = intervals[0]
            for start, end in intervals[1:]:
                if start > hi+tolerance:
                    raise PipelineError(f"DIFF_PATH_SHORTCUT: {net} 多次进入同一端子或孔铜")
                hi = max(hi, end)
            which = edge["nodes"].index(node)
            if (which == 0 and lo > tolerance) or (which == 1 and hi < line.length-tolerance):
                raise PipelineError(f"DIFF_PATH_CONTACT: {net} 接触不在实际路径端点")

    ordered, transitions, used = [], [], set()
    current = ("t", 0)
    while current != ("t", 1):
        available = [index for index in adjacency[current] if index not in used]
        if len(available) != 1:
            raise PipelineError(f"DIFF_PATH_STRUCTURE: {net} 不能重建唯一端到端路径")
        index = available[0]
        used.add(index)
        edge = dict(edges[index])
        if edge["nodes"][0] != current:
            edge["line"] = LineString(list(edge["line"].coords)[::-1])
            edge["nodes"] = edge["nodes"][::-1]
        ordered.append(edge)
        current = edge["nodes"][1]
        if current[0] == "v":
            transitions.append(dict(vias[current[1]], index=current[1]))
    if len(used) != len(edges):
        raise PipelineError(f"DIFF_PATH_STRUCTURE: {net} 存在未遍历的分离铜或环")
    return dict(net=net, segments=ordered, transitions=transitions, terminals=terminals,
                planar_length_mil=sum(edge["length"] for edge in ordered),
                via_barrel_length_mil=None, total_length_mil=None, length_complete=False,
                length_scope="实际走线中心线平面长度；缺少已核实层厚，孔段未知，不代表电气延迟")


def _oriented(path, start_key):
    if path["terminals"][0]["key"] == start_key:
        return path
    result = dict(path)
    result["segments"] = [dict(edge, line=LineString(list(edge["line"].coords)[::-1])) for edge in path["segments"][::-1]]
    result["transitions"] = path["transitions"][::-1]
    return result


def pair_metrics(board, pair):
    """读取实际成对层间路径，返回分层耦合和过孔配对报告及错误列表。

    仅接受两条无支路链的相同换层顺序；孔必须成对且尺寸相同。路径或
    几何不支持时抛PipelineError，耦合失败返回错误。回流和电气延迟保持未知。
    """
    from pcb_differential import _paired_ends, _planar_pair_metrics
    ends = _paired_ends(board, pair)
    paths = [_oriented(measured_multilayer_path(board, pair[key]), ends[0][i]["key"])
             for i, key in enumerate(("p", "n"))]
    p, n = paths
    tolerance = max(1e-6, rules_for(board).contact_tolerance)
    if [s["layer"] for s in p["segments"]] != [s["layer"] for s in n["segments"]]:
        raise PipelineError("DIFF_PAIR_LAYER: 两轨的换层数量或层序不同")
    via_pairs = []
    for first, second in zip(p["transitions"], n["transitions"]):
        if any(abs(first[key]-second[key]) > tolerance for key in ("hole", "diameter")):
            raise PipelineError("DIFF_PAIR_VIA: 两轨成对孔的孔径或外径不同")
        separation = math.dist((first["x"], first["y"]), (second["x"], second["y"]))
        # ponytail: 第一版只接受局部成对孔，较远的独立换层需要显式更复杂的耦合模型。
        if separation > pair["width"]+pair["gap"]+2*first["diameter"]+tolerance:
            raise PipelineError("DIFF_PAIR_VIA: 两轨孔距离过远，不能认定为局部成对换层")
        via_pairs.append(dict(p=[first["x"], first["y"]], n=[second["x"], second["y"]],
                              center_distance_mil=separation, hole_mil=first["hole"],
                              diameter_mil=first["diameter"], layers=list(rules_for(board).copper_layers)))
    if len(via_pairs) != len(p["transitions"]) or len(via_pairs) != len(n["transitions"]):
        raise PipelineError("DIFF_PAIR_VIA: 孔未一一成对")
    segment_reports, errors = [], []
    for index, (first, second) in enumerate(zip(p["segments"], n["segments"])):
        terminal_pairs = []
        for end in (0, -1):
            terminal_pairs.append(tuple(dict(point=tuple(path["line"].coords[end])) for path in (first, second)))
        report, issues = _planar_pair_metrics(board, pair, first, second, terminal_pairs)
        report["segment_index"] = index
        segment_reports.append(report)
        errors.extend(f"换层分段{index}: {issue}" for issue in issues)
    metrics = dict(p=dict(net=pair["p"], planar_length_mil=p["planar_length_mil"], total_length_mil=None),
                   n=dict(net=pair["n"], planar_length_mil=n["planar_length_mil"], total_length_mil=None),
                   gap_mil=pair["gap"], width_mil=pair["width"], multilayer=True,
                   layers=[s["layer"] for s in p["segments"]], paired_vias=via_pairs,
                   segments=segment_reports, coupling_verified=not errors,
                   measured_planar_length_difference_mil=abs(p["planar_length_mil"]-n["planar_length_mil"]),
                   via_barrel_length_mil=None, length_complete=False,
                   return_path_verified=False, requires_native_return_path_check=True,
                   length_scope=p["length_scope"])
    return metrics, errors


def route_pair(board, pair, settings, options, stats, budget):
    """有限枚举1或2次成对全栈通孔换层，返回(候选板,独立指标,中心线)。

    输入完整构造板、差分声明、各网规则及共享预算；失败返回None，时间
    预算耗尽抛RoutingFailure。原铜不修改，每个候选逐网_commit再独立检查。
    本版中心通道为直线，两端允许短引出，不支持分支、微孔及跨层等长补偿。
    """
    from pcb_differential import _paired_ends, _segments
    from pcb_routing import _commit, _groups, _candidate_cost, RoutingFailure
    config = validate_options(options.get("diff_layer_change"))
    if not config["enabled"]:
        return None
    names = (pair["p"], pair["n"])
    if any(item["net"] in names for kind in ("traces", "vias", "shield_vias", "copper_pours")
           for item in board["routes"].get(kind, [])):
        raise PipelineError("DIFF_LAYER_CHANGE_EXISTING: 不覆盖已有差分铜")
    rules = rules_for(board)
    stack = tuple(rules.copper_layers)
    if any(not setting["vias"] or not set(stack).issubset(setting["via_layers"]) for setting in settings.values()):
        raise PipelineError("DIFF_LAYER_CHANGE_RULE: 两轨必须允许实际全叠层通孔")
    if any(abs(settings[names[0]][key]-settings[names[1]][key]) > rules.contact_tolerance for key in ("hole", "diameter")):
        raise PipelineError("DIFF_LAYER_CHANGE_RULE: 两轨成对通孔必须采用相同尺寸")
    ends = _paired_ends(board, pair)
    mids = [tuple((a["point"][i]+b["point"][i])/2 for i in (0, 1)) for a,b in ends]
    distance = math.dist(*mids)
    if distance <= 2*config["lead_distance"]:
        return None
    u = ((mids[1][0]-mids[0][0])/distance, (mids[1][1]-mids[0][1])/distance)
    v = (-u[1], u[0])
    allowed = set.intersection(*(set(value["layers"]) for value in settings.values()))
    layer_order = [layer for layer in stack if layer in allowed]
    start_layers = allowed & set(ends[0][0]["layers"]) & set(ends[0][1]["layers"])
    end_layers = allowed & set(ends[1][0]["layers"]) & set(ends[1][1]["layers"])
    sep = pair["width"]+pair["gap"]
    via_diameter = settings[names[0]]["diameter"]
    type_p = board.get("tags", {}).get("net_tags", {}).get(names[0], {}).get("type", "NORMAL")
    type_n = board.get("tags", {}).get("net_tags", {}).get(names[1], {}).get("type", "NORMAL")
    via_sep = max(sep, via_diameter+rules.get_clearance(type_p, type_n)+rules.curve_tolerance*4)
    sign = 1 if sum((ends[0][0]["point"][i]-mids[0][i])*v[i] for i in (0, 1)) >= 0 else -1
    started = time.monotonic()
    tested = 0
    report = dict(p=names[0], n=names[1], candidates_tested=0, candidates_generated=0,
                  found=False, stop_reason="searching", elapsed_seconds=0.0)
    stats.setdefault("diff_layer_change", []).append(report)
    budget.enter(names[0])
    pair_started = budget.net_elapsed()

    def check():
        budget.check()
        if time.monotonic()-started >= config["max_seconds"]:
            report["stop_reason"] = "local_time_budget"
            raise RoutingFailure("DIFF_LAYER_CHANGE_TIME_BUDGET", "成对换层时间预算内未找到，未证明无解",
                                 failed_nets=list(names), candidates_tested=tested)

    def point(along, offset=0):
        return (mids[0][0]+u[0]*along+v[0]*offset, mids[0][1]+u[1]*along+v[1]*offset)

    def trials():
        for i in range(config["max_candidates"]):
            check()
            lead = config["lead_distance"]+i*config["search_step"]
            if 2*lead >= distance:
                break
            for first in (layer for layer in layer_order if layer in start_layers):
                for last in (layer for layer in layer_order if layer in end_layers):
                    if first != last:
                        yield [first, last], [lead]
                    if config["max_transitions"] >= 2:
                        for middle in (layer for layer in layer_order if layer not in (first, last)):
                            yield [first, middle, last], [lead, distance-lead]

    try:
        counts = {net: len(_groups(board, net, budget)) for net in names}
        scheduled = []
        model = CollisionModel(board)
        for layers, changes in trials():
            check()
            if len(scheduled) >= config["max_candidates"]:
                break
            traces, vias = {}, {}
            stations = [0, *changes, distance]
            for index, net in enumerate(names):
                own_sign = sign if index == 0 else -sign
                vias[net] = [dict(net=net, x=point(at, own_sign*via_sep/2)[0],
                                  y=point(at, own_sign*via_sep/2)[1], hole=settings[net]["hole"],
                                  diameter=settings[net]["diameter"], layers=list(stack)) for at in changes]
                endpoints = [ends[0][index]["point"],
                             *[(via["x"], via["y"]) for via in vias[net]], ends[1][index]["point"]]
                traces[net] = []
                for segment, layer in enumerate(layers):
                    a, b = stations[segment], stations[segment+1]
                    coords = [endpoints[segment], point(a, own_sign*sep/2),
                              point(b, own_sign*sep/2), endpoints[segment+1]]
                    traces[net].extend(_segments(net, layer, coords, pair["width"]))
            cost = sum(_candidate_cost(model, (traces[net], vias[net]), settings[net]) for net in names)
            scheduled.append((cost, len(scheduled), traces, vias, len(changes)))
        report["candidates_generated"] = len(scheduled)
        for cost, _, traces, vias, transition_count in sorted(scheduled, key=lambda item:item[:2]):
            check()
            tested += 1
            report["candidates_tested"] = tested
            first = _commit(board, names[0], traces[names[0]], vias[names[0]], counts[names[0]], budget)
            if first is None:
                continue
            second = _commit(first, names[1], traces[names[1]], vias[names[1]], counts[names[1]], budget)
            if second is None:
                continue
            try:
                metrics, errors = pair_metrics(second, pair)
            except PipelineError:
                continue
            check()
            if not errors:
                report["found"] = True
                report["stop_reason"] = "found_candidate"
                report["candidate_cost"] = cost
                report["transitions"] = transition_count
                return second, metrics, LineString(mids)
        report["stop_reason"] = ("candidate_budget" if len(scheduled) >= config["max_candidates"]
                                 else "finite_station_candidates_exhausted")
        return None
    except RoutingFailure:
        if report["stop_reason"] == "searching":
            report["stop_reason"] = "parent_budget"
        raise
    finally:
        spent = budget.net_elapsed()-pair_started
        budget.leave()
        budget.spent[names[1]] = budget.spent.get(names[1], 0)+spent
        report["elapsed_seconds"] = time.monotonic()-started
        # 即使找到候选，也不能让第二根线绕过原单网时间上限。
        budget.enter(names[1])
        budget.leave()
