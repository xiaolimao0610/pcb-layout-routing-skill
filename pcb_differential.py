"""差分中心线候选与有限蛇形补长；所有结果仍由原几何及连通检查验收。"""
from __future__ import annotations

from copy import deepcopy
import math

from shapely.geometry import LineString, Point
from shapely.ops import linemerge, unary_union

from pcb_pipeline_common import PipelineError, board_polygon, finite, rules_for, terminal_items


def validate_layer_change_options(value=None):
    """输入可选换层配置，返回严格规范字典；未知字段、类型或预算非法时抛异常。"""
    from pcb_diff_multilayer import validate_options
    return validate_options(value)


def validate_channel_quality(board, rule, construction=False):
    """校验单条频道的差分/等长声明，不改变几何。

    入参为完整板、可规范化的规则字典及构造阶段标志；返回None。
    未知字段、重复网络、非法尺寸和不存在的网络均抛PipelineError。
    """
    if "diff_pair" in rule:
        pair = rule["diff_pair"]
        if not isinstance(pair, dict) or set(pair) != {"p", "n", "gap", "width"}:
            raise PipelineError("CHANNEL_SPEC.diff_pair 必须恰含 p/n/gap/width")
        if any(not isinstance(pair[k], str) or not pair[k] for k in ("p", "n")) or pair["p"] == pair["n"]:
            raise PipelineError("差分对必须声明两个不同的真实网络")
        if rule["net"] not in (pair["p"], pair["n"]):
            raise PipelineError("CHANNEL_SPEC.net 必须属于声明的差分对")
        if not construction and {pair["p"], pair["n"]} - set(board["nets"]):
            raise PipelineError("差分对引用不存在的网络")
        pair["gap"] = finite(pair["gap"], "差分铜边间距", positive=True)
        pair["width"] = finite(pair["width"], "差分线宽", positive=True)
    if "length_group" in rule:
        group = rule["length_group"]
        if not isinstance(group, dict) or set(group) != {"name", "tolerance"}:
            raise PipelineError("CHANNEL_SPEC.length_group 必须恰含 name/tolerance")
        if not isinstance(group["name"], str) or not group["name"]:
            raise PipelineError("等长组名必须为非空字符串")
        group["tolerance"] = finite(group["tolerance"], "等长容差", minimum=0)


def declarations(constraints):
    """由已校验规则提取唯一差分对与等长组；相互冲突的声明抛PipelineError。"""
    pairs, pair_by_net, groups, group_by_net = {}, {}, {}, {}
    for rule in constraints:
        if rule["operator"] != "CHANNEL_SPEC":
            continue
        if "diff_pair" in rule:
            pair = rule["diff_pair"]
            key = (pair["p"], pair["n"])
            if key in pairs and pairs[key] != pair:
                raise PipelineError("同一差分对有冲突的间距或线宽声明")
            for net in key:
                if net in pair_by_net and pair_by_net[net] != key:
                    raise PipelineError("同一网络不能属于两个差分对")
                pair_by_net[net] = key
            pairs[key] = deepcopy(pair)
        if "length_group" in rule:
            group, net = rule["length_group"], rule["net"]
            name = group["name"]
            if net in group_by_net and group_by_net[net] != name:
                raise PipelineError("同一网络不能属于两个等长组")
            group_by_net[net] = name
            if name in groups and groups[name]["tolerance"] != group["tolerance"]:
                raise PipelineError("同一等长组必须声明一致的容差")
            record = groups.setdefault(name, {"name": name, "tolerance": group["tolerance"], "nets": []})
            if net not in record["nets"]:
                record["nets"].append(net)
    return list(pairs.values()), list(groups.values())


def construction_view(board):
    """返回仅暂缓新增完成态差分/等长字段的局部构造副本。

    原网表、实体铜、已有硬约束及宽度/层/电流声明全部保留。不得将此副本
    作为最终产物；调用者必须恢复原pipeline_constraints并执行完整原验收。
    """
    candidate = deepcopy(board)
    rules = []
    for rule in board.get("metadata", {}).get("pipeline_constraints", []):
        item = deepcopy(rule)
        if item["operator"] == "CHANNEL_SPEC":
            item.pop("diff_pair", None)
            item.pop("length_group", None)
            if set(item) == {"operator", "net"}:
                continue
        rules.append(item)
    candidate.setdefault("metadata", {})["pipeline_constraints"] = rules
    return candidate


def _terminals(board, net):
    values = terminal_items(board, net)
    if len(values) != 2:
        raise PipelineError(f"SIGNAL_QUALITY_UNSUPPORTED: {net} 仅支持两个真实端子的单路径，当前{len(values)}端")
    return values


def _paired_ends(board, pair):
    """按唯一最短跨网端距匹配端对；对称歧义不猜测电气身份。"""
    p, n = _terminals(board, pair["p"]), _terminals(board, pair["n"])
    costs = [sum(math.dist(p[i]["point"], n[j]["point"]) for i, j in enumerate(order)) for order in ((0, 1), (1, 0))]
    tolerance = rules_for(board).contact_tolerance
    if abs(costs[0]-costs[1]) <= tolerance:
        raise PipelineError("DIFF_PAIR_AMBIGUOUS: 两端配对距离相同，不能猜测差分端对")
    order = (0, 1) if costs[0] < costs[1] else (1, 0)
    return [(p[i], n[j]) for i, j in enumerate(order)]


def measured_path(board, net):
    """从实际LINE重建一个无支路同层路径并测长，不能信任生成时metadata。

    入参完整板与真实网名；返回line/layer/length/widths/trace_indices。
    多端、分支、铜区、过孔、重叠线及不能连接真实端子的路径明确拒绝。
    长度为实际走线中心线几何长度，不推断介质延迟或焊盘内部传播长度。
    """
    terminals = _terminals(board, net)
    routes = board["routes"]
    if any(item["net"] == net for kind in ("vias", "shield_vias", "copper_pours") for item in routes.get(kind, [])):
        raise PipelineError(f"SIGNAL_QUALITY_UNSUPPORTED: {net} 含孔或铜区，未证明唯一同层长度路径")
    records = [(i, trace) for i, trace in enumerate(routes.get("traces", [])) if trace["net"] == net]
    if not records:
        raise PipelineError(f"SIGNAL_QUALITY_INCOMPLETE: {net} 没有完整走线")
    layers = {trace["layer"] for _, trace in records}
    if len(layers) != 1:
        raise PipelineError(f"SIGNAL_QUALITY_UNSUPPORTED: {net} 跨层走线没有已证明的长度路径")
    lines = [LineString([(t["sx"], t["sy"]), (t["ex"], t["ey"])]) for _, t in records]
    copper_line = unary_union(lines)
    line = copper_line if copper_line.geom_type == "LineString" else linemerge(copper_line)
    tolerance = rules_for(board).contact_tolerance
    if (line.geom_type != "LineString" or not line.is_simple or line.is_ring
            or abs(sum(piece.length for piece in lines)-line.length) > tolerance):
        raise PipelineError(f"SIGNAL_QUALITY_UNSUPPORTED: {net} 含分支、断点、环路或重叠线")
    ends = [Point(line.coords[0]), Point(line.coords[-1])]
    matches = [set(i for i, terminal in enumerate(terminals) if terminal["inner"].distance(point) <= tolerance) for point in ends]
    if len(matches[0]) != 1 or len(matches[1]) != 1 or matches[0] == matches[1]:
        raise PipelineError(f"SIGNAL_QUALITY_INCOMPLETE: {net} 中心线路径未在两个真实端子内结束")
    # 同网铜的短接不会被原异网DRC视为错误，但会让线长之和失去唯一路径含义。
    gap = rules_for(board).default_clearance
    max_width=max(t["width"] for _,t in records)
    simple_coords=_simplified_coords(line)
    simple_segments=[LineString([a,b]) for a,b in zip(simple_coords,simple_coords[1:])]
    for i,first in enumerate(simple_segments):
        for second in simple_segments[i+1:]:
            adjacent = any(math.dist(x,y)<=tolerance for x in first.coords for y in second.coords)
            if not adjacent and first.distance(second) <= max_width+gap+tolerance:
                raise PipelineError(f"SIGNAL_QUALITY_SHORTCUT: {net} 非相邻铜段可能形成捷径或缺少自间距")
    for end_index,terminal_index in enumerate((next(iter(matches[0])),next(iter(matches[1])))):
        contact=line.intersection(terminals[terminal_index]["inner"].buffer(max_width/2+tolerance))
        pieces=list(contact.geoms) if hasattr(contact,"geoms") else [contact]
        intervals=[]
        for piece in pieces:
            if piece.geom_type=="LineString" and not piece.is_empty:
                values=[line.project(Point(point)) for point in piece.coords]
                intervals.append((min(values),max(values)))
        intervals.sort()
        if not intervals:
            raise PipelineError(f"SIGNAL_QUALITY_INCOMPLETE: {net} 端子接触区不可证明")
        start,end=intervals[0]
        for low,high in intervals[1:]:
            if low>end+tolerance:
                raise PipelineError(f"SIGNAL_QUALITY_SHORTCUT: {net} 路径在中途再次进入端子铜区")
            end=max(end,high)
        if (end_index==0 and start>tolerance) or (end_index==1 and end<line.length-tolerance):
            raise PipelineError(f"SIGNAL_QUALITY_INCOMPLETE: {net} 端子接触没有连续至路径端点")
    return dict(line=line, layer=next(iter(layers)), length=float(line.length),
                widths=[float(t["width"]) for _, t in records], trace_indices=[i for i, _ in records])


def _simplified_coords(line):
    """去掉沿相同方向的共线分段点，保留真实折角，不改变折线路径。"""
    values=_clean_points(line.coords)
    out=[]
    for point in values:
        while len(out)>=2:
            a,b=out[-2],out[-1]
            u=(b[0]-a[0],b[1]-a[1]);v=(point[0]-b[0],point[1]-b[1])
            if abs(u[0]*v[1]-u[1]*v[0])>1e-8*max(1,math.hypot(*u)*math.hypot(*v)) or u[0]*v[0]+u[1]*v[1]<=0:
                break
            out.pop()
        out.append(point)
    return out


def _offset_line(points, distance):
    """按中心线相邻平行直线交点构造miter偏移，返回简单折线或None。

    输入有序中心线点及带符号偏移mil；拒绝退化、回折和自交，不截断或
    拼接GEOS产生的分离片段，最终实际铜仍须原_commit精确检查。
    """
    points=_simplified_coords(LineString(points))
    if len(points)<2:
        return None
    vectors=[]
    for a,b in zip(points,points[1:]):
        length=math.dist(a,b)
        vectors.append(((b[0]-a[0])/length,(b[1]-a[1])/length))
    def shifted(point,v):return point[0]-v[1]*distance,point[1]+v[0]*distance
    result=[shifted(points[0],vectors[0])]
    for i,point in enumerate(points[1:-1],1):
        u,v=vectors[i-1],vectors[i]
        denominator=u[0]*v[1]-u[1]*v[0]
        if abs(denominator)<1e-10:
            return None
        a,b=shifted(point,u),shifted(point,v)
        dx,dy=b[0]-a[0],b[1]-a[1]
        along=(dx*v[1]-dy*v[0])/denominator
        vertex=(a[0]+along*u[0],a[1]+along*u[1])
        if math.dist(vertex,point)>max(1e-6,4*abs(distance)):
            return None
        result.append(vertex)
    result.append(shifted(points[-1],vectors[-1]))
    line=LineString(result)
    return line if line.is_simple and not line.is_ring and line.length>0 else None


def _pair_metrics(board, pair):
    if any(item["net"] in (pair["p"], pair["n"]) for item in board["routes"].get("vias", [])):
        from pcb_diff_multilayer import pair_metrics
        return pair_metrics(board, pair)
    p, n = measured_path(board, pair["p"]), measured_path(board, pair["n"])
    return _planar_pair_metrics(board, pair, p, n, _paired_ends(board, pair))


def _planar_pair_metrics(board, pair, p, n, ends):
    """核验一段实际同层双轨及其明确端对；返回指标和错误，不信任生成元数据。"""
    tolerance = max(1e-6, rules_for(board).contact_tolerance)
    if p["layer"] != n["layer"]:
        raise PipelineError("DIFF_PAIR_LAYER: 差分两线不在同一铜层")
    if any(abs(width-pair["width"]) > tolerance for width in (*p["widths"], *n["widths"])):
        raise PipelineError("DIFF_PAIR_WIDTH: 实际差分线宽与声明不一致")
    distance = pair["width"] + pair["gap"]
    mids=[((a["point"][0]+b["point"][0])/2,(a["point"][1]+b["point"][1])/2) for a,b in ends]
    span=math.dist(*mids)
    radii=[min(span/4,max(distance,math.dist(a["point"],b["point"]))+pair["width"]) for a,b in ends]
    zones=unary_union([Point(point).buffer(radius) for point,radius in zip(mids,radii)])
    # 从实际折点独立重建中心线，再验证两侧offset。转角的miter外角本来
    # 比垂直间距更远，不能用到另一中心线的最近距离误报为不耦合。
    actual=[]
    for key,path,index in (("p",p,0),("n",n,1)):
        coords=_simplified_coords(path["line"])
        if math.dist(coords[-1],ends[0][index]["point"])<math.dist(coords[0],ends[0][index]["point"]):
            coords.reverse()
        actual.append(coords)
    if len(actual[0])!=len(actual[1]):
        raise PipelineError("DIFF_PAIR_STRUCTURE: 实际两轨的折角结构不同，不能证明同一中心线偏移")
    center=_clean_points([((a[0]+b[0])/2,(a[1]+b[1])/2) for a,b in zip(*actual)])
    if len(center)<2 or not LineString(center).is_simple:
        raise PipelineError("DIFF_PAIR_STRUCTURE: 实际两轨不能重建简单中心线")
    ideals=[_offset_line(center,sign*distance/2) for sign in (1,-1)]
    if any(line is None for line in ideals):
        raise PipelineError("DIFF_PAIR_STRUCTURE: 实际中心线偏移不可证明")
    def mismatch(first,second):
        values=[]
        for own,ideal in ((p["line"],first),(n["line"],second)):
            outside=own.difference(zones)
            target=ideal.difference(zones)
            if outside.is_empty or target.is_empty:
                return math.inf
            values.append(outside.hausdorff_distance(target))
        return max(values)
    errors=[]
    if min(mismatch(*ideals),mismatch(*ideals[::-1]))>tolerance*4:
        errors.append("DIFF_PAIR_GAP: 非端部实际铜不能证明是声明间距的同一中心线偏移")
    metrics = {}
    for key, own, other in (("p", p, n), ("n", n, p)):
        outside = own["line"].difference(zones)
        far = own["line"].difference(other["line"].buffer(distance+tolerance, quad_segs=16))
        if outside.is_empty or outside.length<=tolerance:
            errors.append(f"DIFF_PAIR_UNCOUPLED: {pair[key]} 未发现可证明的耦合路径")
        metrics[key] = dict(net=pair[key], length_mil=own["length"],
                            coupled_length_mil=float(outside.length) if not errors else 0.0,
                            terminal_zone_length_mil=float(own["length"]-outside.length),
                            fanout_unpaired_length_mil=float(far.intersection(zones).length))
    metrics.update(p=metrics["p"], n=metrics["n"], gap_mil=pair["gap"], width_mil=pair["width"],
                   layer=p["layer"], measured_length_difference_mil=abs(p["length"]-n["length"]),
                   terminal_pairing="unique_minimum_cross_net_pad_distance",
                   terminal_zone_radii_mil=radii, reconstructed_centerline=list(map(list,center)),
                   coupling_verified=not errors)
    return metrics, errors


def quality_report(board, constraints=None):
    """独立读取实际路线的差分/等长报告，不采用生成证据作为验收依据。

    返回passed/errors/differential_pairs/length_groups，非法几何能力范围记录
    为明确错误；未知约束结构由原约束入口校验。所有长度为mil。
    """
    constraints = constraints if constraints is not None else board.get("metadata", {}).get("pipeline_constraints", [])
    pairs, groups = declarations(constraints)
    result = dict(passed=True, errors=[], differential_pairs=[], length_groups=[], full_board_accepted=False)
    for pair in pairs:
        try:
            metrics, errors = _pair_metrics(board, pair)
            result["differential_pairs"].append(metrics)
            result["errors"].extend(errors)
        except PipelineError as exc:
            result["errors"].append(str(exc))
            result["differential_pairs"].append(dict(p=pair["p"], n=pair["n"], measured=False, error=str(exc)))
    for group in groups:
        try:
            lengths = {net: measured_path(board, net)["length"] for net in group["nets"]}
            maximum, minimum = max(lengths.values()), min(lengths.values())
            difference = maximum-minimum
            record = dict(name=group["name"], tolerance_mil=group["tolerance"], measured_lengths_mil=lengths,
                          measured_spread_mil=difference, matching_required=len(lengths)>1,
                          deviations_from_longest_mil={net:maximum-length for net,length in lengths.items()})
            result["length_groups"].append(record)
            if difference > group["tolerance"] + rules_for(board).contact_tolerance:
                result["errors"].append(f"LENGTH_GROUP: {group['name']} 实测差{difference:g}mil超过容差{group['tolerance']:g}mil")
        except PipelineError as exc:
            result["errors"].append(str(exc))
            result["length_groups"].append(dict(name=group["name"], measured=False, error=str(exc)))
    result["passed"] = not result["errors"]
    return result


def _clean_points(points):
    result = []
    for value in points:
        point = tuple(map(float, value))
        if not result or math.dist(result[-1], point) > 1e-9:
            result.append(point)
    return result


def _segments(net, layer, points, width):
    points = _clean_points(points)
    return [dict(net=net, layer=layer, width=width, sx=a[0], sy=a[1], ex=b[0], ey=b[1])
            for a, b in zip(points, points[1:])]


def _center_candidates(a, b, board, options):
    """枚举直连、45度折线及板内两侧绕行中心线；调用者限制总候选和时间。"""
    from pcb_routing import _octilinear_doglegs
    yield [a, b]
    yield from _octilinear_doglegs(a, b, width=options.get("width", 10))
    step = options["grid_step"]
    length = math.dist(a, b)
    ux,uy = (b[0]-a[0])/length,(b[1]-a[1])/length
    lead = min(length/4, max(40.0,4*step))
    # 保留两端相同切向；直接从端点横切会把一对实际焊盘的扇出互相挡住。
    # 这里只枚举有界通道，复杂迷宫可能未找到，不能因此报告连续几何无解。
    count = options["diff_max_candidates"]
    for index in range(1, count+1):
        for sign in (1, -1):
            height = sign*index*step
            values = [(0,0),(lead,0),(lead,height),(length-lead,height),(length-lead,0),(length,0)]
            yield [(a[0]+ux*x-uy*y,a[1]+uy*x+ux*y) for x,y in values]


def route_pairs(board, constraints, options, stats, budget, topology):
    """在局部构造视图中生成耦合中心线、偏移两轨并逐网原_commit精检。

    入参包含完整网表和共享预算；返回候选副本及已处理网集合。默认支持
    两端同层，显式diff_layer_change启用有限成对通孔换层；原铜只核验保留。
    有限候选耗尽抛RoutingFailure并报告未找到，绝不返回半对或删除旧铜。
    """
    from pcb_routing import _net_options, _groups, _commit, RoutingFailure
    from pcb_width_policy import hard_width_minimum
    pairs, groups = declarations(constraints)
    candidate, handled = deepcopy(board), set()
    stats.setdefault("differential", [])
    forbidden = set(options.get("ground_nets", [])) if options.get("ground_strategy") == "plane_then_drc" else set()
    if any(forbidden & set(group["nets"]) for group in groups):
        raise PipelineError("等长组不得包含plane_then_drc暂缓地网")
    for pair in pairs:
        nets = {pair["p"], pair["n"]}
        if nets & forbidden or nets & set(options.get("pours_before_net", {})):
            raise PipelineError("差分对不能包含暂缓地网或依赖铺铜的网络")
        if nets & set(topology):
            raise PipelineError("差分生成尚未支持同时指定TOPOLOGY_ORDER；不能忽略其语义")
        settings = {net:_net_options(candidate, net, dict(options, width=pair["width"])) for net in nets}
        minimums = {net: hard_width_minimum(candidate, net) for net in nets}
        if any(pair["width"] < value for value in minimums.values()):
            raise PipelineError("差分width低于现有/电流/规则线宽下限，不允许缩窄")
        if "width" in options and abs(options["width"]-pair["width"]) > rules_for(candidate).contact_tolerance:
            raise PipelineError("全局routing.width与差分width冲突")
        layer_change = validate_layer_change_options(options.get("diff_layer_change"))
        existing = [item for kind in ("traces", "vias", "shield_vias", "copper_pours")
                    for item in candidate["routes"].get(kind, []) if item["net"] in nets]
        if existing:
            if not layer_change["enabled"] and any(v["net"] in nets for v in candidate["routes"].get("vias", [])):
                raise PipelineError("DIFF_PAIR_UNSUPPORTED: 默认同层模式不验收跨层差分，需显式启用diff_layer_change")
            metric, errors = _pair_metrics(candidate, pair)
            if errors:
                raise PipelineError("差分原铜未通过质量检查，需明确重布局清理后重导，不能覆盖：" + "; ".join(errors))
            stats["differential"].append(dict(p=pair["p"], n=pair["n"], preserved_existing=True, metrics=metric))
            handled.update(nets)
            continue
        ends = _paired_ends(candidate, pair)
        layers = set.intersection(*(set(item["layers"]) for end in ends for item in end),
                                 *(set(value["layers"]) for value in settings.values()))
        if not layers and not layer_change["enabled"]:
            raise PipelineError("DIFF_PAIR_UNSUPPORTED: 四端没有共同允许铜层，尚未实现成对换层")
        a, b = [tuple((p["point"][axis]+n["point"][axis])/2 for axis in (0, 1)) for p,n in ends]
        if math.dist(a, b) <= rules_for(candidate).contact_tolerance:
            raise PipelineError("差分两端中心重合，未找到非退化中心线")
        separation = pair["width"]+pair["gap"]
        tested, found = 0, None
        budget.enter(pair["p"])
        pair_started = budget.net_elapsed()
        counts = {net:len(_groups(candidate, net, budget)) for net in nets}
        for points in _center_candidates(a, b, candidate, options):
            budget.check()
            if tested >= options["diff_max_candidates"]:
                break
            points = _clean_points(points)
            if len(points) < 2:
                continue
            center = LineString(points)
            if not center.is_simple:
                continue
            for layer in sorted(layers):
                if tested >= options["diff_max_candidates"]:
                    break
                budget.check()
                tested += 1
                offset = [_offset_line(points,sign*separation/2) for sign in (1,-1)]
                if any(line is None for line in offset):
                    continue
                choices = []
                for swap in (False, True):
                    path_lines = offset[::-1] if swap else offset
                    traces = {}
                    fanout = 0.0
                    for net, index, path in zip((pair["p"], pair["n"]), (0,1), path_lines):
                        coords = list(path.coords)
                        # GEOS的新版本保留方向；仍以真实源端最短距离消除版本差异。
                        source, target = ends[0][index]["point"], ends[1][index]["point"]
                        if math.dist(source, coords[-1])+math.dist(target, coords[0]) < math.dist(source, coords[0])+math.dist(target, coords[-1]):
                            coords.reverse()
                        fanout += math.dist(source, coords[0])+math.dist(target, coords[-1])
                        traces[net] = _segments(net, layer, [source, *coords, target], pair["width"])
                    choices.append((fanout, traces))
                for _, traces in sorted(choices, key=lambda row:row[0]):
                    budget.check()
                    first = _commit(candidate, pair["p"], traces[pair["p"]], [], counts[pair["p"]], budget)
                    if first is None:
                        continue
                    second = _commit(first, pair["n"], traces[pair["n"]], [], counts[pair["n"]], budget)
                    if second is None:
                        continue
                    try:
                        metrics, errors = _pair_metrics(second, pair)
                    except PipelineError:
                        # 输入原铜已在上方独立核验；这里失败的只是本次新候选。
                        # 例如有限宽度铜捷径不能让其他合法中心线失去搜索机会。
                        continue
                    if not errors:
                        found = second, metrics, center
                        break
                if found:
                    break
            if found:
                break
        # 两条网络共享本次耦合搜索的耗时，不因成对处理绕过单网预算。
        spent = budget.net_elapsed()-pair_started
        budget.leave()
        budget.spent[pair["n"]] = budget.spent.get(pair["n"], 0)+spent
        budget.enter(pair["n"])
        budget.leave()
        if not found and layer_change["enabled"]:
            from pcb_diff_multilayer import route_pair
            found = route_pair(candidate, pair, settings, options, stats, budget)
        if not found:
            raise RoutingFailure("DIFF_PAIR_SEARCH_BUDGET", "有限中心线候选内未找到合法耦合差分路径，未证明无解",
                                 nets=sorted(nets), failed_nets=sorted(nets), candidates_tested=tested)
        candidate, metrics, center = found
        handled.update(nets)
        stats["differential"].append(dict(p=pair["p"], n=pair["n"],
            method="paired_through_via" if metrics.get("multilayer") else "centerline_offset",
            candidates_tested=tested, centerline=list(map(list, center.coords)), metrics=metrics))
    return candidate, handled


def match_lengths(board, original, constraints, options, stats, budget):
    """仅在本轮新增线中插入有界45度蛇形补长，返回原精检通过的候选副本。

    original是本轮开始的完整板，原铜按前缀逐对象保持。长度从实际路线
    重建，不信任metadata。找不到合格蛇形或预算耗尽抛RoutingFailure，
    不返回临时板；无法唯一测长的分支/换层路径明确拒绝。
    """
    from pcb_routing import RoutingFailure, _commit, _groups
    from pcb_pipeline_common import check_board
    _, groups = declarations(constraints)
    candidate = deepcopy(board)
    original_count = len(original["routes"].get("traces", []))
    stats.setdefault("length_matching", [])
    for group in groups:
        lengths = {net:measured_path(candidate, net)["length"] for net in group["nets"]}
        target = max(lengths.values())
        for net in sorted(group["nets"], key=lambda n:(lengths[n], n)):
            measured = measured_path(candidate, net)
            delta = target-measured["length"]
            if delta <= group["tolerance"]+rules_for(candidate).contact_tolerance:
                continue
            budget.enter(net)
            tested, found = 0, None
            indices = [i for i in measured["trace_indices"] if i >= original_count]
            # 每个候选是多齿45度梯形蛇形；总增加长度由几何公式精确确定。
            for index in indices:
                budget.check()
                trace = candidate["routes"]["traces"][index]
                a,b = (trace["sx"],trace["sy"]),(trace["ex"],trace["ey"])
                length = math.dist(a,b)
                from pcb_routing import _is_octilinear
                if not _is_octilinear(a,b):
                    continue
                ux,uy = (b[0]-a[0])/length,(b[1]-a[1])/length
                for teeth in range(1, 17):
                    budget.check()
                    amplitude = delta/(2*teeth*(math.sqrt(2)-1))
                    if amplitude > options["length_max_amplitude"] or 2*teeth*amplitude >= length:
                        continue
                    for sign in (1,-1):
                        if tested >= options["length_max_candidates"]:
                            break
                        budget.check()
                        tested += 1
                        pitch = length/teeth
                        points = [a]
                        for tooth in range(teeth):
                            start = tooth*pitch
                            for along,height in ((start+amplitude, sign*amplitude),
                                                 (start+pitch-amplitude, sign*amplitude), (start+pitch,0)):
                                points.append((a[0]+ux*along-uy*height,a[1]+uy*along+ux*height))
                        # 删除的只有本轮候选线；源铜的前缀保持逐对象相同。
                        base = deepcopy(candidate)
                        del base["routes"]["traces"][index]
                        replacement = _segments(net,trace["layer"],points,trace["width"])
                        before = len(_groups(base,net,budget))
                        trial = _commit(base,net,replacement,[],before,budget)
                        if trial is None:
                            continue
                        errors = check_board(trial,ignore_connectivity=True)
                        if errors:
                            continue
                        # 补长不得破坏已经声明的差分耦合；其他等长网可仍在施工。
                        pair_rules = [r for r in constraints if r["operator"]=="CHANNEL_SPEC" and "diff_pair" in r]
                        pair_errors = quality_report(trial,pair_rules)["errors"]
                        if pair_errors:
                            continue
                        try:
                            actual = measured_path(trial,net)["length"]
                        except PipelineError:
                            # 精确铜检查通过不等于唯一长度路径已获证明；拒绝本候选再试。
                            continue
                        if abs(target-actual) <= group["tolerance"]+rules_for(trial).contact_tolerance:
                            found = trial, actual
                            break
                    if found or tested >= options["length_max_candidates"]:
                        break
                if found or tested >= options["length_max_candidates"]:
                    break
            budget.leave()
            if not found:
                raise RoutingFailure("LENGTH_SEARCH_BUDGET", "有限蛇形候选内未找到满足容差的路径，未证明无解",
                                     net=net,group=group["name"],target_length_mil=target,
                                     measured_length_mil=measured["length"],candidates_tested=tested)
            candidate,actual = found
            stats["length_matching"].append(dict(net=net,group=group["name"],method="45_degree_serpentine",
                before_length_mil=measured["length"],after_length_mil=actual,target_length_mil=target,
                candidates_tested=tested,deviation_mil=abs(target-actual)))
    return candidate
