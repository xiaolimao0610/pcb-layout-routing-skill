"""Plan bounded repairs for native-DRC ground gaps using actual filled copper.

No global ground routing occurs. This is a calculation-only entry point: apply
the generated source-bound delta, rebuild native pours, and run native DRC again.
Raw native polygon coordinates must already have a source-bound mil conversion
receipt; this module never infers a unit multiplier from a bounding box.
"""
from argparse import ArgumentParser
from collections import deque
from copy import deepcopy
import heapq
import json
import math
from pathlib import Path
import time

from shapely.geometry import Point, LineString
from shapely.ops import nearest_points, unary_union
from shapely.strtree import STRtree

from pcb_geometry import via_bounds, trace_bounds, filled_region_bounds
from pcb_native_copper import read, write, source_identity
from pcb_native_poured_geometry import validate_native_pour_snapshot
from pcb_pipeline import build_writeback_plan
from pcb_pipeline_common import (
    CollisionModel, PipelineError, copper_and_drills,
    finite, normalize_board, rules_for, polygons,
)


def _positive_contact(a, b, tolerance):
    part = a.intersection(b)
    return part.area > tolerance ** 2


def _validated_inputs(board, drc, copper_evidence):
    if not all(isinstance(value, dict) for value in (board, drc, copper_evidence)):
        raise PipelineError("Board, DRC targets and copper evidence must be objects")
    source = source_identity(board.get("metadata", {}).get("source"))
    if board.get("metadata", {}).get("board_usage") == "test_only":
        raise PipelineError("A physical native snapshot is required")
    if drc.get("source") != source or not all(drc.get(k) is True for k in (
            "native_check_complete", "endpoint_source_stable")):
        raise PipelineError("Native DRC and endpoint evidence must match this exact source")
    if drc.get("unclassified") or drc.get("unresolved_connections"):
        raise PipelineError("Native connection diagnostics are incomplete; resolve their classification first")
    poured = board.get("routes", {}).get("copper_pours", [])
    poured_ids = {p.get("id") for p in poured if p.get("source_kind") == "Poured"}
    if not poured_ids or None in poured_ids:
        raise PipelineError("Actual filled Poured objects are required; pour borders are not copper")
    # A collector supplies this receipt only after proving its native coordinate
    # convention. Neither a raw source path nor board.units establishes that fact.
    if (copper_evidence.get("source") != source
            or copper_evidence.get("units") != "mil"
            or copper_evidence.get("coordinate_frame") != "native_pcb_data_origin"
            or not isinstance(copper_evidence.get("method"), str)
            or not copper_evidence["method"].strip()
            or not isinstance(copper_evidence.get("evidence"), list)
            or not copper_evidence["evidence"]
            or set(copper_evidence.get("poured_ids", [])) != poured_ids):
        raise PipelineError("Source-bound actual-pour coordinate evidence is required; unit guessing is prohibited")
    validate_native_pour_snapshot(board, copper_evidence)
    grounds = drc.get("ground_nets")
    if (not isinstance(grounds, list) or not grounds or len(set(grounds)) != len(grounds)
            or set(grounds) - set(board.get("nets", {}))):
        raise PipelineError("DRC must explicitly name the actual ground networks")
    # Normalization checks complete topology and all fixed geometry. Huge
    # derived planes are separately source-validated above and curve-validated
    # below; running an unrelated full-board DRC over them would dominate this
    # explicitly local, native-DRC-driven workflow.
    fixed = deepcopy(board)
    fixed['routes']['copper_pours'] = [p for p in poured if p.get('source_kind') != 'Poured']
    normalized = normalize_board(fixed)
    fixed_by_id = {p['id']: p for p in normalized['routes']['copper_pours']}
    rules = rules_for(normalized)
    restored = []
    for p in poured:
        if p.get('source_kind') != 'Poured':
            restored.append(fixed_by_id[p['id']])
            continue
        if p.get('layer') not in rules.copper_layers or p.get('net') not in normalized['nets']:
            raise PipelineError('Actual Poured layer/net is absent from the board')
        for region in p['filled_polygons']:
            filled_region_bounds(region, rules.curve_tolerance)
        restored.append(deepcopy(p))
    normalized['routes']['copper_pours'] = restored
    return normalized, source, grounds


def _drc_pads(board, drc, grounds):
    pads = {}
    for component in board["components"]:
        for pad in component["pads"]:
            identifier = pad["padId"]
            if identifier in pads:
                # Native IDs must be globally unique for a native DRC lookup.
                raise PipelineError("Ambiguous native pad ID: " + identifier)
            pads[identifier] = (component["id"], identifier, pad["net"])
    vias = {}
    for via in board['routes'].get('vias', []):
        identifier = via.get('id')
        if identifier:
            if identifier in vias or identifier in pads:
                raise PipelineError('Ambiguous native via identity: ' + identifier)
            vias[identifier] = ('@via', identifier, via['net'])
    resolved = {item["id"]: item for item in drc.get("live_objects", [])}
    targets, unsupported = set(), []
    for gap in drc.get("ground_targets", []):
        if gap.get("net") not in grounds or gap.get("connection") is not True:
            raise PipelineError("Ground target is not an actual classified ground connection error")
        mentioned = []
        for obj in gap.get("objects", []):
            identifier = obj.get("id")
            if identifier not in pads and identifier not in vias:
                continue
            item = resolved.get(identifier, {})
            key = pads.get(identifier, vias.get(identifier))
            if (item.get("resolved") is not True or item.get("net") != gap["net"]
                    or item.get("units") != "mil" or key[2] != gap["net"]
                    or item.get("coordinate_frame") != "native_pcb_data_origin"):
                raise PipelineError("DRC pad/via endpoint is unresolved or mismatched: " + identifier)
            targets.add(key)
            mentioned.append(identifier)
        if not mentioned:
            unsupported.append({"drc_index": gap.get("index"), "reason": "No resolved pad/via endpoint; no unlisted geometry is repaired"})
    return sorted(targets), unsupported


def _rebuild_ids(board, source, spec):
    if spec is None:
        return set()
    if not isinstance(spec, dict) or set(spec) != {"source", "rebuild_pour_ids"} or spec["source"] != source:
        raise PipelineError("Native pour rebuild specification must match the actual source")
    identifiers = spec["rebuild_pour_ids"]
    known = {p.get("parent_id") for p in board["routes"].get("copper_pours", [])
             if p.get("source_kind") == "Poured" and isinstance(p.get("parent_id"), str)}
    if (not isinstance(identifiers, list) or not identifiers
            or any(not isinstance(i, str) or not i for i in identifiers)
            or len(set(identifiers)) != len(identifiers) or set(identifiers) - known):
        raise PipelineError("rebuild_pour_ids must explicitly name existing native Pour parent IDs")
    return set(identifiers)


def _fixed_copper_view(board, rebuild_ids):
    """Only explicitly scheduled native derived copper may change on rebuild.

    The full snapshot remains authoritative for the pre-rebuild ground contact
    proof and for writeback preservation. Fixed Fill, native teardrops, traces,
    pads, drills, vias, and unlisted Poured objects remain collision obstacles.
    """
    if not rebuild_ids:
        return board
    view = deepcopy(board)
    view["routes"]["copper_pours"] = [p for p in view["routes"].get("copper_pours", [])
        if not (p.get("source_kind") == "Poured" and p.get("parent_id") in rebuild_ids)]
    return view


def _reference_region(board, net, *, max_nodes=200000, max_contacts=1000000, max_seconds=60.0):
    """在预算内选择面积最大的、已证明接到物理端子的实际铜区。

    入参为完整板快照、显式地网和节点/接触次数/秒数上限；输出保留原
    layer/inner/name/terminal 字段，并附带锚链 proof。同层仅认保守内界
    的正面积接触；跨层仅认同一真实镀孔及其孔壁接触。各独立铜岛分别
    建节点，不按网名、外界相交或点接触合并。无合格锚链或预算耗尽时
    抛出 PipelineError，含义是尚未证明，不宣称无解。
    """
    if type(max_nodes) is not int or max_nodes < 1 or type(max_contacts) is not int or max_contacts < 1:
        raise PipelineError('参考面证明的节点和接触预算必须是正整数')
    finite(max_seconds, 'reference_max_seconds', positive=True)
    started = time.monotonic()
    visits = contacts = 0
    def budget():
        if visits > max_nodes or contacts > max_contacts or time.monotonic()-started > max_seconds:
            raise PipelineError(f'参考面锚链预算耗尽，尚未找到合格参考面；节点={visits}，接触={contacts}')
    rules = rules_for(board)
    copper, drills = copper_and_drills(board, net_filter=net)
    budget()
    actual_names = {f"pour {i}.{j}" for i, pour in enumerate(board['routes'].get('copper_pours', []))
                    if pour.get('source_kind') == 'Poured' and pour['net'] == net
                    for j in range(len(pour['filled_polygons']))}
    nodes, layer_ids, barrel_ids = [], {}, {}
    for item in copper:
        if item['net'] != net:
            continue
        for layer in item['layers']:
            for piece in polygons(item['inner']):
                index = len(nodes)
                nodes.append((item, layer, piece))
                if len(nodes) > max_nodes:
                    raise PipelineError('参考面锚链节点预算耗尽，尚未找到合格参考面')
                layer_ids.setdefault(layer, []).append(index)
                if item.get('barrel'):
                    barrel_ids.setdefault(item['barrel'], []).append(index)
        budget()
    trees = {layer: STRtree([nodes[i][2] for i in ids]) for layer, ids in layer_ids.items()}
    holes = {hole['name']: hole for hole in drills if hole['plated'] and hole['net'] == net}
    candidates = sorted((i for i,(item,_,_) in enumerate(nodes) if item['name'] in actual_names),
                        key=lambda i: (-nodes[i][2].area, i))
    tolerance = rules.contact_tolerance
    for origin in candidates:
        queue = deque([origin])
        parent = {origin: None}
        edge = {}
        while queue:
            budget()
            index = queue.popleft(); visits += 1; budget()
            item, layer, shape = nodes[index]
            if item.get('terminal'):
                path = [index]
                while parent[path[-1]] is not None:
                    path.append(parent[path[-1]])
                path.reverse()
                first, reference_layer, region = nodes[origin]
                return dict(layer=reference_layer, inner=region, name=first['name'], terminal=item['terminal'],
                    proof=dict(method='保守内界正面积接触及真实镀孔锚链',
                        nodes=[dict(name=nodes[n][0]['name'], layer=nodes[n][1],
                                    terminal=nodes[n][0].get('terminal')) for n in path],
                        edges=[edge[n] for n in path[1:]],
                        visited_nodes=visits, contact_tests=contacts, total_copper_nodes=len(nodes),
                        elapsed_seconds=time.monotonic()-started,
                        budget=dict(max_nodes=max_nodes,max_contacts=max_contacts,max_seconds=max_seconds)))
            neighbors = {layer_ids[layer][int(i)] for i in trees[layer].query(shape)}
            barrel = item.get('barrel')
            if barrel:
                neighbors.update(barrel_ids[barrel])
            for other_index in sorted(neighbors):
                if other_index in parent:
                    continue
                contacts += 1; budget()
                other, other_layer, other_shape = nodes[other_index]
                proof = None
                if layer == other_layer:
                    area = shape.intersection(other_shape).area
                    if area > tolerance ** 2:
                        proof = dict(kind='同层保守内界正面积接触',layer=layer,area_mil2=area)
                elif barrel and barrel == other.get('barrel') and barrel in holes:
                    hole = holes[barrel]
                    if layer in hole['layers'] and other_layer in hole['layers']:
                        a = shape.intersection(hole['outer'].boundary).length
                        z = other_shape.intersection(hole['outer'].boundary).length
                        if a > tolerance and z > tolerance:
                            proof = dict(kind='同一真实镀孔孔壁跨层连接',barrel=barrel,
                                         layers=[layer,other_layer],wall_contact_lengths_mil=[a,z])
                if proof is not None:
                    parent[other_index] = index; edge[other_index] = proof
                    queue.append(other_index)
    raise PipelineError('有限实际铜图中尚未证明任何实际Poured区域连接到物理地端子')


def _paths(a, b):
    """Finite straight/45-degree/orthogonal proposals, without a grid search."""
    ax, ay = a
    bx, by = b
    dx, dy = bx - ax, by - ay
    if abs(dx) < 1e-12 or abs(dy) < 1e-12 or abs(abs(dx) - abs(dy)) < 1e-12:
        yield [a, b]
    diagonal = min(abs(dx), abs(dy))
    sx, sy = math.copysign(diagonal, dx), math.copysign(diagonal, dy)
    for mid in [(ax + sx, ay + sy), (bx - sx, by - sy), (ax, by), (bx, ay)]:
        points = [a]
        if mid not in (a, b):
            points.append(mid)
        points.append(b)
        yield points
    # A finite third segment can pass a nearby pad/track fence without global
    # grid routing or modifying existing signal copper.
    for mx in [ax+dx*f for f in (.25,.5,.75)]+[min(ax,bx)-d for d in (20,40,80)]+[max(ax,bx)+d for d in (20,40,80)]:
        points=[a,(mx,ay),(mx,by),b]
        yield [p for i,p in enumerate(points) if i==0 or p!=points[i-1]]
    for my in [ay+dy*f for f in (.25,.5,.75)]+[min(ay,by)-d for d in (20,40,80)]+[max(ay,by)+d for d in (20,40,80)]:
        points=[a,(ax,my),(bx,my),b]
        yield [p for i,p in enumerate(points) if i==0 or p!=points[i-1]]


def _trace_records(points, net, layer, width):
    # Native saved coordinates have four decimals; validate that exact proposed
    # geometry and omit duplicate corners introduced by quantization.
    points = [(round(x,4),round(y,4)) for x,y in points]
    return [dict(net=net, layer=layer, sx=a[0], sy=a[1], ex=b[0], ey=b[1], width=width)
            for a, b in zip(points, points[1:]) if a != b]


def _bounded_grid_paths(model, net, layer, start, width, *, step, radius,
                        max_nodes, accept, max_results=1, deadline=None):
    """Search one local corridor; the caller proves each proposed landing.

    ``accept(point)`` returns None or {end, landing}. Every grid edge and the
    final short connection is checked against the supplied fixed copper model.
    This helper does not choose a ground plane, alter obstacles or route a net.
    """
    for value, name in ((step, 'step'), (radius, 'radius'), (width, 'width')):
        finite(value, name, positive=True)
    if radius > 1000 or step > radius or type(max_nodes) is not int or not 1 <= max_nodes <= 100000:
        raise PipelineError('Local grid needs radius <=1000mil and max_nodes 1..100000')
    if type(max_results) is not int or not 1 <= max_results <= 1000:
        raise PipelineError('Local grid max_results must be 1..1000')
    start = tuple(round(v, 4) for v in start)
    queue = [(0, 0, 0)]; costs = {(0, 0): 0}; parents = {}; closed = set()
    results = []; accepted_ends = set(); timed_out = False
    def coordinate(key):
        return tuple(round(start[i] + key[i] * step, 4) for i in (0, 1))
    def legal(points):
        return (all(math.dist(start,p) <= radius+1e-8 for p in points)
                and all(model.trace_ok(net, layer, *a, *b, width)
                   for a, b in zip(points, points[1:]) if a != b)
                )
    while queue and len(closed) < max_nodes:
        if deadline is not None and time.monotonic() >= deadline:
            timed_out = True; break
        distance, ix, iy = heapq.heappop(queue); key = (ix, iy)
        if key in closed or distance > costs[key] + 1e-9:
            continue
        closed.add(key); point = coordinate(key); landing = accept(point)
        if landing is not None:
            end = tuple(round(v, 4) for v in landing['end'])
            if end not in accepted_ends and math.dist(start, end) <= radius:
                tails = [[point]] if end == point else list(_paths(point, end))[:5]
                tail = next((p for p in tails if legal(p)), None)
                if tail is not None:
                    keys = [key]
                    while keys[-1] != (0, 0):
                        keys.append(parents[keys[-1]])
                    points = [coordinate(k) for k in reversed(keys)] + tail[1:]
                    simple = [points[0]]
                    for i in range(1, len(points)-1):
                        a, p, z = points[i-1:i+2]
                        if abs((p[0]-a[0])*(z[1]-p[1])-(p[1]-a[1])*(z[0]-p[0])) > 1e-8:
                            simple.append(p)
                    if points[-1] != simple[-1]:
                        simple.append(points[-1])
                    # Remove grid stair-steps only when an equally short or
                    # shorter orthogonal/45-degree shortcut passes the same
                    # fixed-copper and full-radius checks.
                    shortened = [simple[0]]; index = 0
                    while index < len(simple)-1:
                        chosen = [simple[index],simple[index+1]]; far = index+1
                        for last in range(len(simple)-1,index+1,-1):
                            old_length = sum(math.dist(a,b) for a,b in zip(simple[index:last],simple[index+1:last+1]))
                            for path in list(_paths(simple[index],simple[last]))[:5]:
                                if (sum(math.dist(a,b) for a,b in zip(path,path[1:])) <= old_length+1e-8
                                        and legal(path)):
                                    chosen = path; far = last; break
                            if far == last:
                                break
                        shortened.extend(chosen[1:]); index = far
                    simple = shortened
                    if legal(simple):
                        results.append(dict(points=simple, landing=landing.get('landing'),
                                            length=sum(math.dist(a,b) for a,b in zip(simple,simple[1:]))))
                        accepted_ends.add(end)
                        if len(results) >= max_results:
                            break
        for dx, dy in ((1,0),(-1,0),(0,1),(0,-1),(1,1),(1,-1),(-1,1),(-1,-1)):
            nxt = (ix+dx, iy+dy)
            if nxt in closed or math.hypot(*nxt)*step > radius:
                continue
            dest = coordinate(nxt); new_cost = distance + math.dist(point,dest)
            if new_cost >= costs.get(nxt, math.inf)-1e-9 or not legal([point,dest]):
                continue
            costs[nxt] = new_cost; parents[nxt] = key
            heapq.heappush(queue,(new_cost,*nxt))
    return results, dict(expanded_nodes=len(closed), bounded_radius_mil=radius,
                         grid_step_mil=step, node_budget=max_nodes,
                         budget_exhausted=timed_out or (bool(queue) and len(closed)>=max_nodes),
                         timed_out=timed_out)


def _local_bridge_connected(model, rules, pad, traces, contact, new_drill=None, include_landing=False):
    """Prove a conductive chain after subtracting actual existing/new drills."""
    shapes = [pad['inner']]
    for trace in traces:
        inner = trace_bounds(trace, rules.curve_tolerance).inner
        for index in model._candidates('drills', (trace['layer'],), inner):
            inner = inner.difference(model.drills[index]['outer'])
        if new_drill is not None:
            inner = inner.difference(new_drill.outer)
        shapes.append(inner)
    if include_landing:
        shapes.append(contact)
    return any(_positive_contact(piece, pad['inner'], rules.contact_tolerance) and
               _positive_contact(piece, contact, rules.contact_tolerance)
               for piece in polygons(unary_union(shapes)))


def _repair_one(board, key, net, reference, radius, grid, width, diameter, hole, max_candidates, rebuild_ids, progress=None, native_center=None, local_grid_nodes=0):
    """Prove only the new local bridge; final connectivity belongs to native DRC.

    Existing fixed copper is immutable. A new drill may not intersect any of
    it, including same-net copper, so no old connection can be severed. Derived
    Poured may change only under the explicit rebuild scope.
    """
    rules = rules_for(board)
    fixed_view = _fixed_copper_view(board, rebuild_ids)
    model = CollisionModel(fixed_view)
    ground_plane_names = {f'pour {i}.{j}' for i,p in enumerate(fixed_view['routes']['copper_pours'])
                          if p.get('source_kind') == 'Poured' and p['net'] == net
                          for j in range(len(p['filled_polygons']))}
    if key[0] == '@via':
        index = next(i for i, via in enumerate(board['routes']['vias']) if via.get('id') == key[1])
        pads = [item for item in model.copper if item['name'] == f'via {index}']
        target = {'via_id': key[1], 'target_kind': 'via'}
    else:
        pads = [item for item in model.copper if item['terminal'] == key]
        target = {'terminal': list(key), 'target_kind': 'pad'}
    candidates = []
    grounded_vias = []
    for via in board['routes']['vias']:
        if via['net'] != net or reference['layer'] not in via['layers']:
            continue
        annulus, _ = via_bounds(via,rules.curve_tolerance)
        if _positive_contact(annulus.inner,reference['inner'],rules.contact_tolerance):
            grounded_vias.append((via,annulus.inner))
    for item in pads:
        layer = item['layers'][0]
        point = item['inner'].representative_point()
        start = tuple(native_center) if native_center else (round(point.x, 4), round(point.y, 4))
        if native_center is None and not item['inner'].covers(Point(start)):
            start = point.x, point.y
        if layer == reference['layer']:
            end = nearest_points(Point(start), reference['inner'])[1]
            if Point(start).distance(end) <= radius:
                if Point(start).distance(end) > rules.contact_tolerance:
                    candidates.append((start, (end.x, end.y), layer, False, item, reference['inner']))
                else:
                    # Native DRC still names this target. Do not skip it merely
                    # because the offline plane predicts existing contact.
                    for dx,dy in [(grid,0),(-grid,0),(0,grid),(0,-grid)]:
                        q=Point(start[0]+dx,start[1]+dy)
                        if reference['inner'].covers(q):
                            candidates.append((start,(q.x,q.y),layer,False,item,reference['inner']))
        # An existing ground via already touches the proven reference plane.
        # A short surface bridge can use it without drilling another hole.
        for via,annulus in grounded_vias:
            if layer not in via['layers'] or via.get('id') == key[1]:
                continue
            end=(via['x'],via['y'])
            if math.dist(start,end) <= radius:
                candidates.append((start,end,layer,False,item,annulus))
        steps = min(int(radius // grid), math.ceil(math.sqrt(max_candidates)))
        for ix in range(-steps, steps + 1):
            for iy in range(-steps, steps + 1):
                if ix == iy == 0 or math.hypot(ix * grid, iy * grid) > radius:
                    continue
                end = round(start[0] + ix * grid,4), round(start[1] + iy * grid,4)
                if Point(end).distance(item['outer']) < diameter / 2:
                    continue
                candidates.append((start,end,layer,True,item,None))
    candidates.sort(key=lambda c: (c[3],math.dist(c[0],c[1]),c[2],c[1]))
    old_lines = {layer: unary_union([LineString([(t['sx'],t['sy']),(t['ex'],t['ey'])])
                 for t in board['routes']['traces'] if t['net']==net and t['layer']==layer and t['width']>=width])
                 for layer in {item['layers'][0] for item in pads}}
    seen,tested=set(),0
    def finish(traces, vias, search=None):
        trial = deepcopy(board)
        trial['routes']['traces'].extend(traces); trial['routes']['vias'].extend(vias)
        repair = {**target, 'status':'candidate_awaiting_rebuild' if rebuild_ids else 'locally_joined',
                  'reference_terminal':list(reference['terminal']),
                  'reference_poured_entity':reference['name'],
                  'candidate_sites_tested':tested, 'added_traces':traces, 'added_vias':vias,
                  'deferred_clearance_pour_ids':sorted(rebuild_ids),
                  'proof':'Positive local target-to-bridge-to-continuous-reference-copper contact; fixed copper is not cut; native rebuild and DRC remain required'}
        if search is not None:
            repair['local_grid_search'] = search
        return trial, repair
    if progress: progress(dict(stage='local_candidates', candidate_count=len(candidates)))
    for start,end,layer,add_via,pad,landing in candidates:
        candidate_key=start,end,layer,add_via
        if candidate_key in seen:
            continue
        seen.add(candidate_key)
        if tested>=max_candidates:
            break
        tested+=1
        vias=[];new_drill=None;contact=landing
        if add_via:
            via=dict(net=net,x=end[0],y=end[1],hole=hole,diameter=diameter,layers=list(rules.copper_layers))
            if not model.via_ok(net,via['x'],via['y'],hole,diameter,via['layers']):
                continue
            annulus,new_drill=via_bounds(via,rules.curve_tolerance)
            if not _positive_contact(annulus.inner,reference['inner'],rules.contact_tolerance):
                continue
            # Additional protection for same-net pads/lines: ordinary collision
            # checks allow a hole in same-net copper; this local workflow does not.
            if any(model.copper[i]['name'] not in ground_plane_names and
                   model.copper[i]['outer'].intersects(new_drill.outer) for i in
                   model._candidates('copper',via['layers'],new_drill.outer)):
                continue
            vias=[via];contact=annulus.inner
        for points in _paths(start,end):
            traces=_trace_records(points,net,layer,width)
            if not traces or not all(model.trace_ok(net,layer,t['sx'],t['sy'],t['ex'],t['ey'],width) for t in traces):
                continue
            if all(old_lines[layer].covers(LineString([(t['sx'],t['sy']),(t['ex'],t['ey'])])) for t in traces):
                continue
            if not _local_bridge_connected(model, rules, pad, traces, contact, new_drill, add_via):
                continue
            return finish(traces, vias)
    searches = []
    if local_grid_nodes:
        if progress: progress(dict(stage='bounded_local_grid', node_budget=local_grid_nodes, radius=radius))
        for pad in pads:
            layer = pad['layers'][0]
            point = pad['inner'].representative_point()
            start = tuple(native_center) if native_center else (point.x, point.y)
            def accept(point):
                for via, annulus in grounded_vias:
                    if layer not in via['layers'] or via.get('id') == key[1]:
                        continue
                    end = (via['x'], via['y'])
                    if math.dist(point,end) > max(grid*4,width*6) or math.dist(start,end)>radius:
                        continue
                    if any(all(model.trace_ok(net,layer,t['sx'],t['sy'],t['ex'],t['ey'],width)
                               for t in _trace_records(path,net,layer,width))
                           for path in list(_paths(point,end))[:5]):
                        return dict(end=end,landing=dict(vias=[],contact=annulus,drill=None))
                if Point(point).distance(pad['outer']) < diameter/2:
                    return None
                via = dict(net=net,x=point[0],y=point[1],hole=hole,diameter=diameter,layers=list(rules.copper_layers))
                if not model.via_ok(net,*point,hole,diameter,via['layers']):
                    return None
                annulus, drill = via_bounds(via,rules.curve_tolerance)
                if not _positive_contact(annulus.inner,reference['inner'],rules.contact_tolerance):
                    return None
                if any(model.copper[i]['name'] not in ground_plane_names and
                       model.copper[i]['outer'].intersects(drill.outer) for i in
                       model._candidates('copper',via['layers'],drill.outer)):
                    return None
                return dict(end=point,landing=dict(vias=[via],contact=annulus.inner,drill=drill))
            found, search = _bounded_grid_paths(model,net,layer,start,width,step=grid,radius=radius,
                                                max_nodes=local_grid_nodes,accept=accept,max_results=8)
            searches.append(search)
            for candidate in found:
                traces = _trace_records(candidate['points'],net,layer,width)
                landing = candidate['landing']
                if (traces and not all(old_lines[layer].covers(LineString([(t['sx'],t['sy']),(t['ex'],t['ey'])])) for t in traces)
                        and _local_bridge_connected(model,rules,pad,traces,landing['contact'],landing['drill'],bool(landing['vias']))):
                    return finish(traces,landing['vias'],search)
    repair = {**target,'status':'unresolved','candidate_sites_tested':tested,
              'reason':'Bounded local candidates exhausted; no global ground routing or rule relaxation attempted'}
    if searches: repair['local_grid_searches'] = searches
    return board, repair


def prepare_ground_repairs(board, drc, copper_evidence, *, radius=120, grid=10,
                           width=10, via_diameter=24, via_hole=12, max_candidates=1024, rebuild_spec=None, progress=None, local_grid_nodes=0):
    if progress: progress(dict(stage='validating_actual_source_and_geometry'))
    board, source, grounds = _validated_inputs(board, drc, copper_evidence)
    rebuild_ids = _rebuild_ids(board, source, rebuild_spec)
    for value, name in [(radius, "radius"), (grid, "grid"), (width, "width"),
                        (via_diameter, "via_diameter"), (via_hole, "via_hole")]:
        finite(value, name, positive=True)
    if width < 10 or via_hole >= via_diameter or radius > 1000 or grid > radius:
        raise PipelineError("Use width >=10mil, a valid annulus, and a bounded local radius <=1000mil")
    if type(max_candidates) is not int or not 1 <= max_candidates <= 10000:
        raise PipelineError("max_candidates must be 1..10000")
    if type(local_grid_nodes) is not int or not 0 <= local_grid_nodes <= 100000:
        raise PipelineError('local_grid_nodes must be 0 (disabled) or 1..100000 per native endpoint')
    targets, unsupported = _drc_pads(board, drc, grounds)
    if progress: progress(dict(stage='reference_connectivity', targets_total=len(targets), unsupported=unsupported))
    anchors = {net: _reference_region(board, net) for net in grounds if any(key[2] == net for key in targets)}
    result, repairs = deepcopy(board), []
    for target_index, (cid, pid, net) in enumerate(targets):
        started = time.monotonic()
        if progress: progress(dict(stage='target_started', target_index=target_index, target=[cid,pid], completed=len(repairs)))
        tag = result.get("tags", {}).get("net_tags", {}).get(net, {})
        selected_width = max(width, rules_for(result).minimum_trace_width, tag.get("width", 0), tag.get("min_width", 0))
        endpoint = next(item for item in drc['live_objects'] if item['id']==pid)['geometry']
        result, repair = _repair_one(result, (cid, pid), net, anchors[net], radius, grid,
                                     selected_width, via_diameter, via_hole, max_candidates, rebuild_ids, progress,
                                     (endpoint['x'],endpoint['y']), local_grid_nodes)
        repairs.append(dict(repair, net=net))
        if progress: progress(dict(stage='target_completed', target_index=target_index, completed=len(repairs), seconds=time.monotonic()-started, repair=repairs[-1]))
    report = dict(source=source, scope="native_drc_pad_via_gaps_only", global_ground_routing=False,
                  reference_regions={net:dict(name=ref['name'],layer=ref['layer'],terminal=ref['terminal'],
                      area_mil2=ref['inner'].area,bounds=list(ref['inner'].bounds),proof=ref.get('proof'))
                      for net,ref in anchors.items()},
                  repairs=repairs, unsupported_targets=unsupported,
                  unresolved=[item for item in repairs if item["status"] == "unresolved"],
                  deferred_clearance_pour_ids=sorted(rebuild_ids),
                  ground_connected_after_rebuild=None,
                  requires_native_pour_rebuild=True, requires_native_drc_recheck=True,
                  full_board_accepted=False, native_drc_passed=False)
    result["metadata"]["native_ground_workflow"] = {
        "strategy": "plane_then_drc", "ground_nets": grounds, "stage": "local_repairs_await_native_rebuild",
        "rebuild_pour_ids": sorted(rebuild_ids), "filled_copper_stale_after_apply": bool(rebuild_ids)}
    return result, build_writeback_plan(board, result), report


def main(argv=None):
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("--board", required=True)
    parser.add_argument("--drc-targets", required=True)
    parser.add_argument("--copper-evidence", required=True)
    parser.add_argument("--rebuild-spec", help="Same-source explicit native Pour parent IDs to rebuild after this delta")
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--radius", type=float, default=120)
    parser.add_argument("--grid", type=float, default=10)
    parser.add_argument("--max-candidates", type=int, default=1024)
    parser.add_argument('--local-grid-nodes', type=int, default=0,
                        help='Opt-in bounded grid fallback per native DRC endpoint; 0 disables, maximum 100000')
    args = parser.parse_args(argv)
    directory = Path(args.output_dir)
    directory.mkdir(parents=True, exist_ok=False)
    started = time.monotonic()
    journal = dict(status='calculating', repairs=[], writeback_ready=False)
    def progress(event):
        journal.update({key:value for key,value in event.items() if key!='repair'})
        journal['elapsed_seconds'] = time.monotonic()-started
        if 'repair' in event:
            journal['repairs'].append(event['repair'])
        write(directory/'progress.json', journal)
        print(json.dumps({key:value for key,value in journal.items() if key not in ('repairs','unsupported')},ensure_ascii=False),flush=True)
    board, plan, report = prepare_ground_repairs(read(args.board), read(args.drc_targets), read(args.copper_evidence),
        radius=args.radius, grid=args.grid, max_candidates=args.max_candidates, local_grid_nodes=args.local_grid_nodes,
        rebuild_spec=read(args.rebuild_spec) if args.rebuild_spec else None, progress=progress)
    write(directory / "board.json", board)
    write(directory / "writeback-plan.json", plan)
    write(directory / "result.json", report)
    journal.update(status='calculated',writeback_ready=True)
    write(directory/'progress.json',journal)
    print(json.dumps({"local_repair_candidates": sum(r["status"] in ("locally_joined", "candidate_awaiting_rebuild") for r in report["repairs"]),
           "unresolved": len(report["unresolved"]), "unsupported": len(report["unsupported_targets"]),
           "full_board_accepted": False, "requires_native_drc_recheck": True}))
    return 0 if not report["unresolved"] and not report["unsupported_targets"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
