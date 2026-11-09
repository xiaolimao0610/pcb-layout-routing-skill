"""Propose a bounded local trace detour and a physically checked pin escape.

No editor writes occur here. The caller must review the explicit replaced trace,
revalidate the proposal against the current native board, then apply and rerun DRC.
"""
from argparse import ArgumentParser
from copy import deepcopy
import json
import math
from pathlib import Path
import time
from uuid import uuid4

from pcb_pipeline_common import CollisionModel, PipelineError, strict_options, finite, rules_for, polygons, _layers
import pcb_routing as routing


def detour_paths(trace, anchor):
    """Finite orthogonal/45-degree bypasses; retain the original two endpoints."""
    start = (trace['sx'], trace['sy']); end = (trace['ex'], trace['ey'])
    dx, dy = end[0] - start[0], end[1] - start[1]
    if not dx and not dy:
        raise PipelineError('Cannot detour a zero-length trace')
    diagonal = bool(dx and dy)
    if diagonal and not math.isclose(abs(dx), abs(dy), rel_tol=1e-12, abs_tol=1e-9):
        raise PipelineError('Local repair supports orthogonal or 45-degree traces')
    sx, sy = (1 if dx >= 0 else -1), (1 if dy >= 0 else -1)
    dominant = 0 if dx else 1
    length = abs(dx or dy)
    along = min(length, max(0, (anchor[dominant] - start[dominant]) * (sx if dx else sy)))
    # Try the pin neighbourhood first; moving the complete blocking trace is
    # less likely to fit and needlessly explores distant occupied corridors.
    spans = [(max(0, along - extent), min(length, along + extent))
             for extent in (50, 30, 40, 20, 60, 80, 100, 120, 160, 200)] + [(0, length)]
    seen = set()
    for low, high in spans:
        if high <= low:
            continue
        a = (start[0] + sx * low if dx else start[0], start[1] + sy * low if dy else start[1])
        z = (start[0] + sx * high if dx else start[0], start[1] + sy * high if dy else start[1])
        for distance in range(10, min(150, int(high - low)), 2 if not diagonal else 5):
            for side in (-1, 1):
                if diagonal:
                    middle = [a, (a[0] + sx * distance, a[1]), (z[0], z[1] - sy * distance), z] if side < 0 else [a, (a[0], a[1] + sy * distance), (z[0] - sx * distance, z[1]), z]
                elif dy:
                    middle = [a, (a[0] + side * distance, a[1] + sy * distance), (z[0] + side * distance, z[1] - sy * distance), z]
                else:
                    middle = [a, (a[0] + sx * distance, a[1] + side * distance), (z[0] - sx * distance, z[1] + side * distance), z]
                if not diagonal and distance * 2 >= high - low:
                    continue
                path = [start] + middle + [end]
                path = [point for i, point in enumerate(path) if not i or point != path[i - 1]]
                key = tuple(path)
                if key not in seen:
                    seen.add(key)
                    yield path
                if not diagonal:
                    # A square bypass can fit where the two slanted returns
                    # would close a narrow via channel. All segments still
                    # receive the same physical clearance checks below.
                    middle = ([a, (a[0] + side * distance, a[1]),
                               (z[0] + side * distance, z[1]), z] if dy else
                              [a, (a[0], a[1] + side * distance),
                               (z[0], z[1] + side * distance), z])
                    path = [start] + middle + [end]
                    path = [point for i, point in enumerate(path) if not i or point != path[i - 1]]
                    if tuple(path) not in seen:
                        seen.add(tuple(path))
                        yield path


def propose_local_detour(board, net, blocking_trace_index, *, options=None):
    """Return an explicit patch only after both physical net groups are checked."""
    if (type(blocking_trace_index) is not int or blocking_trace_index < 0
            or blocking_trace_index >= len(board.get('routes', {}).get('traces', []))):
        raise PipelineError('Use an existing nonnegative trace index')
    if net not in board.get('nets', {}):
        raise PipelineError('Requested net is absent from the complete board')
    options = strict_options(options, {'trace_layers', 'grid_step', 'diagonal', 'allow_vias',
                                      'via_diameter', 'via_drill', 'via_cost', 'via_in_pad_penalty', 'width',
                                      'max_seconds', 'max_net_seconds', 'max_expansions'},
                             'local repair routing options')
    options = dict(dict(trace_layers=list(board['rules']['copper_layers']), grid_step=10,
                        diagonal=True, allow_vias=True, max_seconds=120,
                        max_net_seconds=120, max_expansions=100000), **(options or {}))
    original = board['routes']['traces'][blocking_trace_index]
    if original.get('locked') or original['net'] == net:
        raise PipelineError('Name one unlocked foreign-net trace to propose replacing')
    groups = routing._groups(board, net)
    if len(groups) < 2:
        raise PipelineError('Requested net has no missing physical connection')
    before_other = routing._groups(board, original['net'])
    root = deepcopy(board); root['routes']['traces'].pop(blocking_trace_index)
    empty_model = CollisionModel(root)
    settings = routing._net_options(board, net, options)
    budget = routing._Budget(options); budget.enter(net)
    stats = dict(detours_tested=0, legal_detours=0)
    parts = [(group, shape) for group in groups for layer, geometry in group['layers'].items()
             if layer in settings['layers'] for shape in routing._pieces(geometry)]
    parts.sort(key=lambda item: (len(item[0]['terminals']), item[1].area))
    for source, shape in parts[:8]:
        point = shape.centroid if shape.covers(shape.centroid) else shape.representative_point()
        for path in detour_paths(original, (point.x, point.y)):
            budget.check(); stats['detours_tested'] += 1
            replacement = [routing._trace(original['net'], original['layer'], a, b, original['width']) for a, b in zip(path, path[1:])]
            if not all(empty_model.trace_ok(t['net'], t['layer'], t['sx'], t['sy'], t['ex'], t['ey'], t['width']) for t in replacement):
                continue
            candidate = deepcopy(root); candidate['routes']['traces'].extend(replacement)
            after_other = routing._groups(candidate, original['net'], budget)
            if any(not any(old['terminals'] <= new['terminals'] for new in after_other) for old in before_other):
                continue
            stats['legal_detours'] += 1
            model = CollisionModel(candidate)
            for target in groups:
                if target is source:
                    continue
                route = routing._local_via_path(model, net, source, target, settings, options, set(), stats, budget)
                if route is None:
                    continue
                accepted = routing._commit(candidate, net, *route, len(groups), budget, base_model=model)
                if accepted is not None:
                    return dict(board=accepted, net=net, source=deepcopy(board.get('metadata', {}).get('source')),
                                source_segments=[deepcopy(original)], replacement_segments=replacement,
                                added_traces=route[0], added_vias=route[1], stats=stats,
                                remaining_groups=len(routing._groups(accepted, net)),
                                affected_net=original['net'], affected_net_groups=len(after_other),
                                preview_only=True, full_board_accepted=False)
    return dict(board=None, net=net, stats=stats, reason='No candidate in this bounded local search',
                preview_only=True, full_board_accepted=False)


def _derived_teardrops(board, trace):
    """Only the named parent's source-proven derived copper can be deferred."""
    records = {t['id']: t for t in board['routes']['traces'] if t.get('id')}
    records.update({v['id']: v for v in board['routes']['vias'] if v.get('id')})
    records.update({p['padId']: dict(p, layers=_layers(p,c['layer'],rules_for(board),spanning=bool(p.get('hole'))))
                    for c in board['components'] for p in c['pads']})
    found = []
    for pour in board['routes']['copper_pours']:
        refs = pour.get('metadata', {}).get('refs', [])
        if trace['id'] not in refs:
            continue
        meta = pour.get('metadata', {}); raw = meta.get('native_source_record', {})
        header, payload = raw.get('header', {}), raw.get('payload', {})
        if (pour.get('source_kind') != 'Teardrop' or meta.get('source_only') is not True
                or header.get('type') != 'TEARDROP' or header.get('id') != pour.get('id')
                or payload.get('refs') != refs or payload.get('netName') != trace['net']
                or payload.get('layerId') != trace['layer'] or pour.get('net') != trace['net']
                or pour.get('layer') != trace['layer'] or not refs or len(set(refs)) != len(refs)
                or any(i not in records or records[i].get('net') != trace['net']
                       or trace['layer'] not in records[i].get('layers',[records[i].get('layer')]) for i in refs)):
            raise PipelineError('Named trace has unproven or foreign derived copper: ' + str(pour.get('id')))
        found.append(dict(id=pour['id'], refs=deepcopy(refs)))
    return found


def propose_joint_ground_detour(board, drc, copper_evidence, net, target_pad_id,
                                blocking_trace_id, *, rebuild_spec=None, options=None):
    """One explicit foreign trace detour plus a bounded local ground escape.

    Keep all terminals and fixed copper. Only the named Line and its actual
    source-derived teardrops are deferred; native rebuilding may create a new
    teardrop, so the result remains a proposal until fresh native DRC passes.
    """
    from shapely.geometry import Point
    from shapely.ops import unary_union
    from pcb_geometry import via_bounds, trace_bounds
    from pcb_ground_repair import (
        _validated_inputs, _drc_pads, _rebuild_ids, _reference_region,
        _fixed_copper_view, _bounded_grid_paths, _trace_records, _positive_contact,
    )
    options = strict_options(options, {'grid_step', 'radius', 'signal_radius', 'max_nodes',
        'max_ground_candidates', 'max_seconds', 'width', 'via_diameter', 'via_hole',
        'deferred_teardrop_ids'}, 'joint local options')
    opt = dict(grid_step=2.5, radius=220, signal_radius=160, max_nodes=40000,
               max_ground_candidates=200, max_seconds=120, width=10, via_diameter=24, via_hole=12)
    opt.update(options or {})
    for key in ('grid_step', 'radius', 'signal_radius', 'max_seconds', 'width', 'via_diameter', 'via_hole'):
        finite(opt[key], key, positive=True)
    if (opt['width'] < 10 or opt['via_hole'] >= opt['via_diameter']
            or max(opt['radius'], opt['signal_radius']) > 1000
            or opt['grid_step'] > min(opt['radius'], opt['signal_radius'])
            or type(opt['max_nodes']) is not int or not 1 <= opt['max_nodes'] <= 100000
            or type(opt['max_ground_candidates']) is not int or not 1 <= opt['max_ground_candidates'] <= 1000):
        raise PipelineError('Joint search needs >=10mil width, a valid annulus and finite local budgets')
    board, source, grounds = _validated_inputs(board, drc, copper_evidence)
    targets, _ = _drc_pads(board, drc, grounds)
    matches = [key for key in targets if key[1] == target_pad_id and key[2] == net and key[0] != '@via']
    if net not in grounds or len(matches) != 1:
        raise PipelineError('Name one actual, resolved native-DRC ground pad')
    key = matches[0][:2]
    lines = [t for t in board['routes']['traces'] if t.get('id') == blocking_trace_id]
    if (len(lines) != 1 or lines[0].get('locked') or lines[0]['net'] in grounds
            or lines[0]['width'] < 10):
        raise PipelineError('Name one unlocked non-ground copper Line, preserving its >=10mil width')
    original = lines[0]; derived = _derived_teardrops(board, original)
    rebuild_ids = _rebuild_ids(board, source, rebuild_spec)
    fixed = _fixed_copper_view(board, rebuild_ids)
    before_groups = routing._groups(fixed, original['net'])
    base = deepcopy(fixed)
    base['routes']['traces'] = [t for t in base['routes']['traces'] if t.get('id') != blocking_trace_id]
    known_derived = {p['id'] for p in derived}
    deferred = opt.get('deferred_teardrop_ids',sorted(known_derived))
    if (not isinstance(deferred,list) or any(not isinstance(i,str) for i in deferred)
            or len(set(deferred)) != len(deferred) or set(deferred)-known_derived):
        raise PipelineError('deferred_teardrop_ids must be a unique subset of this Line actual source-derived teardrops')
    # Retaining other derived copper is conservative and can preserve a useful
    # routing corridor. Deleting the parent may remove the full derived set in
    # EDA; report both scopes explicitly and require native geometry readback.
    removed_derived = set(deferred)
    base['routes']['copper_pours'] = [p for p in base['routes']['copper_pours'] if p.get('id') not in removed_derived]
    model = CollisionModel(base); rules = rules_for(board)
    pads = [item for item in model.copper if item.get('terminal') == key and original['layer'] in item['layers']]
    if len(pads) != 1:
        raise PipelineError('Target pad must expose one physical copper shape on the blocking Line layer')
    pad = pads[0]; layer = original['layer']
    native = next(item['geometry'] for item in drc['live_objects'] if item['id'] == target_pad_id)
    start = (round(native['x'],4), round(native['y'],4))
    if not pad['inner'].covers(Point(start)):
        raise PipelineError('Resolved native pad centre is outside the proved pad copper')
    reference = _reference_region(board, net)
    tag = board.get('tags', {}).get('net_tags', {}).get(net, {})
    width = max(opt['width'], rules.minimum_trace_width, tag.get('width',0), tag.get('min_width',0))
    deadline = time.monotonic() + opt['max_seconds']
    def ground_landing(point):
        via = dict(net=net,x=point[0],y=point[1],hole=opt['via_hole'],diameter=opt['via_diameter'],layers=list(rules.copper_layers))
        if not model.via_ok(net,via['x'],via['y'],via['hole'],via['diameter'],via['layers']):
            return None
        annulus, drill = via_bounds(via,rules.curve_tolerance)
        if (not _positive_contact(annulus.inner,reference['inner'],rules.contact_tolerance)
                or any(model.copper[i]['outer'].intersects(drill.outer) for i in
                       model._candidates('copper',via['layers'],drill.outer))):
            return None
        return dict(end=point,landing=via)
    candidates, grid_stats = _bounded_grid_paths(model,net,layer,start,width,
        step=opt['grid_step'],radius=opt['radius'],max_nodes=opt['max_nodes'],
        max_results=opt['max_ground_candidates'],deadline=deadline,accept=ground_landing)
    stats = dict(ground_grid=grid_stats, ground_candidates=len(candidates), signal_searches=[])
    for candidate_index, candidate in enumerate(candidates):
        if time.monotonic() >= deadline:
            break
        ground = _trace_records(candidate['points'],net,layer,width)
        if not ground:
            continue
        via = candidate['landing']; view = deepcopy(base)
        view['routes']['traces'].extend(ground); view['routes']['vias'].append(via)
        signal_model = CollisionModel(view)
        end = (original['ex'],original['ey'])
        def signal_landing(point):
            return dict(end=end) if math.dist(point,end) <= max(12,opt['grid_step']*2) else None
        paths, signal_stats = _bounded_grid_paths(signal_model,original['net'],layer,
            (original['sx'],original['sy']),original['width'],step=opt['grid_step'],
            radius=opt['signal_radius'],max_nodes=opt['max_nodes'],deadline=deadline,accept=signal_landing)
        stats['signal_searches'].append(dict(candidate=candidate_index,**signal_stats))
        if not paths:
            continue
        replacement = _trace_records(paths[0]['points'],original['net'],layer,original['width'])
        view['routes']['traces'].extend(replacement)
        # Unselected derived copper may remain a conservative obstacle, but
        # cannot prove connectivity: native parent deletion can remove it all.
        connectivity = deepcopy(view)
        connectivity['routes']['copper_pours'] = [p for p in connectivity['routes']['copper_pours']
                                                 if p.get('id') not in known_derived]
        after_groups = routing._groups(connectivity,original['net'])
        if any(not any(old['terminals'] <= new['terminals'] for new in after_groups) for old in before_groups):
            continue
        annulus, drill = via_bounds(via,rules.curve_tolerance)
        shapes = [pad['inner'],annulus.inner]
        for trace in ground:
            shape = trace_bounds(trace,rules.curve_tolerance).inner.difference(drill.outer)
            for i in model._candidates('drills',(layer,),shape):
                shape = shape.difference(model.drills[i]['outer'])
            shapes.append(shape)
        connected = unary_union(shapes)
        if not any(_positive_contact(piece,pad['inner'],rules.contact_tolerance)
                   and _positive_contact(piece,annulus.inner,rules.contact_tolerance) for piece in polygons(connected)):
            continue
        target = deepcopy(board)
        target['routes']['traces'] = [t for t in target['routes']['traces'] if t.get('id') != blocking_trace_id]+replacement+ground
        target['routes']['vias'].append(via)
        # Source geometry remains intact as evidence. Native deletes/rebuilds
        # derived copper; do not send a Fill deletion to imitate that process.
        report = dict(source=source,net=net,target_pad_id=target_pad_id,
            source_segments=[deepcopy(original)],replacement_segments=replacement,
            added_traces=ground,added_vias=[via],expected_removed_derived_teardrops=derived,
            deferred_derived_teardrop_ids=sorted(removed_derived),
            affected_connectivity_excludes_derived_ids=sorted(known_derived),
            affected_net=original['net'],affected_net_before_groups=len(before_groups),affected_net_after_groups=len(after_groups),
            reference_terminal=list(reference['terminal']),reference_poured_entity=reference['name'],
            reference_contact_mil2=annulus.inner.intersection(reference['inner']).area,
            pad_trace_via_connected=True,added_drill_intersects_fixed_copper=False,
            deferred_clearance_pour_ids=sorted(rebuild_ids),stats=stats,
            requires_native_pour_rebuild=True,requires_native_drc_recheck=True,
            requires_derived_copper_readback=True,preview_only=True,full_board_accepted=False)
        plan = dict(schema_version=1,plan_id=str(uuid4()),source=deepcopy(source),components=[],
            remove=dict(traces=[blocking_trace_id],vias=[],copper_pours=[]),
            add=dict(traces=replacement+ground,vias=[via],copper_pours=[]),target_board=target,metadata=deepcopy(report))
        return dict(board=target,plan=plan,**report)
    return dict(board=None,plan=None,source=source,net=net,stats=stats,
                reason='Bounded local search exhausted; feasibility remains undetermined',
                preview_only=True,full_board_accepted=False)


def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument('--board', required=True); parser.add_argument('--net', required=True)
    parser.add_argument('--mode', choices=['detour','joint-ground'], default='detour')
    parser.add_argument('--blocking-trace-index', type=int)
    parser.add_argument('--blocking-trace-id'); parser.add_argument('--target-pad-id')
    parser.add_argument('--drc-targets'); parser.add_argument('--copper-evidence'); parser.add_argument('--rebuild-spec')
    parser.add_argument('--options'); parser.add_argument('--output-dir', required=True)
    args = parser.parse_args()
    read = lambda p: json.loads(Path(p).read_text(encoding='utf-8-sig'))
    directory = Path(args.output_dir); directory.mkdir(parents=True, exist_ok=False)
    options = read(args.options) if args.options else None
    if args.mode == 'joint-ground':
        if not all((args.blocking_trace_id,args.target_pad_id,args.drc_targets,args.copper_evidence)):
            parser.error('joint-ground needs --blocking-trace-id, --target-pad-id, --drc-targets and --copper-evidence')
        result = propose_joint_ground_detour(read(args.board),read(args.drc_targets),read(args.copper_evidence),
            args.net,args.target_pad_id,args.blocking_trace_id,
            rebuild_spec=read(args.rebuild_spec) if args.rebuild_spec else None,options=options)
    else:
        if args.blocking_trace_index is None:
            parser.error('detour needs --blocking-trace-index')
        result = propose_local_detour(read(args.board), args.net, args.blocking_trace_index,options=options)
    board = result.pop('board')
    plan = result.pop('plan',None)
    if board is not None:
        (directory / 'board.json').write_text(json.dumps(board, ensure_ascii=False, indent=2), encoding='utf-8')
    if plan is not None:
        (directory / 'writeback-plan.json').write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding='utf-8')
    (directory / 'proposal.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'found': board is not None, 'stats': result['stats']}, ensure_ascii=False))
    return 0 if board is not None else 1


if __name__ == '__main__':
    raise SystemExit(main())
