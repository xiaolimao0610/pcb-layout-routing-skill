"""Read-only EasyEDA source/footprint collector for the PCB pipeline.

Run beside pcb_bridge.py and eda_adapter.mjs. No board, project or footprint IDs
are built in. Output is evidence, not electrical/manufacturing acceptance.
"""
from __future__ import annotations

import argparse
import base64
from datetime import datetime, timezone
import hashlib
import io
import json
import math
from pathlib import Path
import sys
import zipfile

from shapely.geometry import MultiPoint, Polygon

from pcb_bridge import EasyEDABridge
from pcb_pipeline_common import rules_for


MAX_FILE_BYTES = 32 * 1024 * 1024
MAX_UNPACKED_BYTES = 128 * 1024 * 1024
REQUIRED_RULES = {
    'default_clearance', 'minimum_trace_width', 'minimum_drill',
    'min_annular_ring', 'board_edge_clearance', 'component_courtyard_margin',
    'hole_to_hole_clearance', 'npth_clearance', 'sensitive_noisy_isolation',
    'default_via_drill', 'default_via_diameter', 'clearance_matrix',
}
SPEC_FIELDS = {
    'schema_version', 'pad_coordinates', 'pad_coordinates_provenance',
    'copper_layers', 'copper_layers_provenance', 'rules', 'rules_provenance',
    'footprint_overrides', 'footprint_via_movement', 'body_curve_tolerance',
    'board', 'tags',
}


class ContextError(ValueError):
    pass


def digest(raw):
    return 'sha256:' + hashlib.sha256(raw).hexdigest()


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')


def records(source):
    result = []
    for line_no, line in enumerate(source.splitlines(), 1):
        if not line.strip():
            continue
        if '||' not in line:
            raise ContextError(f'Unsupported native record at line {line_no}')
        head, payload = line.rstrip().removesuffix('|').split('||', 1)
        try:
            head, payload = json.loads(head), json.loads(payload)
        except ValueError as exc:
            raise ContextError(f'Invalid native JSON at line {line_no}') from exc
        if not isinstance(head, dict) or not isinstance(payload, dict) or not isinstance(head.get('type'), str):
            raise ContextError(f'Only object native records are supported, line {line_no}')
        result.append({'type': head['type'], 'id': head.get('id'), 'payload': payload})
    return result


def footprint_source(raw, expected_uuid):
    """Read bounded archive members in memory; never extract supplied paths."""
    candidates = []
    if zipfile.is_zipfile(io.BytesIO(raw)):
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            members = archive.infolist()
            if len(members) > 128 or sum(m.file_size for m in members) > MAX_UNPACKED_BYTES:
                raise ContextError('Footprint archive exceeds bounded extraction limits')
            for member in members:
                if member.is_dir() or not member.filename.lower().endswith(('.elibu', '.efoo')):
                    continue
                if member.flag_bits & 1:
                    raise ContextError('Encrypted footprint archive is unsupported')
                candidates.append((member.filename, archive.read(member).decode('utf-8-sig')))
    else:
        candidates.append(('native-text', raw.decode('utf-8-sig')))
    matching = []
    for name, source in candidates:
        rows = records(source)
        heads = [r['payload'] for r in rows if r['type'] == 'DOCHEAD']
        if heads and all(h.get('docType') == 'FOOTPRINT' and h.get('uuid') == expected_uuid for h in heads):
            matching.append((name, source, rows))
    if len(matching) != 1:
        raise ContextError('Expected exactly one complete FOOTPRINT source matching its live UUID')
    return matching[0]


def number(value, label):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ContextError(f'{label} must be a finite number')
    return float(value)


def polygon(points):
    if not isinstance(points, list) or not 3 <= len(points) <= 65537:
        raise ContextError('Body must contain 3..65537 local coordinate pairs')
    for point in points:
        if not isinstance(point, (list, tuple)) or len(point) != 2:
            raise ContextError('Body vertices must be local [x,y] pairs')
        number(point[0], 'body x')
        number(point[1], 'body y')
    shape = Polygon(points)
    if not shape.is_valid or shape.is_empty or shape.area <= 0 or len(shape.interiors):
        raise ContextError('Body must be one non-self-intersecting solid polygon')
    return [list(p) for p in shape.exterior.coords]


def circle_vertices(cx, cy, radius, tolerance):
    if radius <= 0 or tolerance <= 0:
        raise ContextError('Circle radius and body_curve_tolerance must be positive')
    angle = math.acos(radius / (radius + tolerance))
    if angle <= 0:
        raise ContextError('Body curve tolerance is below floating-point resolution')
    count = max(8, math.ceil(math.pi / angle))
    if count > 65536:
        raise ContextError('Body circle needs more than 65536 envelope vertices')
    outer_radius = radius / math.cos(math.pi / count)
    return [[cx + outer_radius * math.cos(2 * math.pi * i / count),
             cy + outer_radius * math.sin(2 * math.pi * i / count)] for i in range(count)]


def circular_body(path, tolerance):
    """Conservative envelope, derived only from native local circle/arc geometry.

    Near-semicircle pairs can have independently rounded native angles. Bound
    each exact arc by its full circle, then take the convex hull of circumscribed
    polygons. This contains both arcs and the bounded filled body; it never
    pretends the two source circles have an exactly shared center.
    """
    circles = []
    if path and path[0] == 'CIRCLE' and len(path) in (4, 5):
        if len(path) == 5 and path[4] not in (0, 1, False, True):
            raise ContextError('Unknown CIRCLE direction flag')
        circles = [(number(path[1], 'circle x'), number(path[2], 'circle y'), number(path[3], 'circle radius'))]
    elif (len(path) == 10 and path[2] == 'ARC' and path[6] == 'ARC'
          and path[8:10] == path[0:2]):
        start = [number(v, 'arc coordinate') for v in path[0:2]]
        middle = [number(v, 'arc coordinate') for v in path[4:6]]
        angles = [number(path[3], 'arc angle'), number(path[7], 'arc angle')]
        if angles[0] * angles[1] <= 0 or any(abs(abs(a) - 180) > 0.001 for a in angles):
            raise ContextError('Automatic circular body requires two consistently directed near-180-degree arcs')
        for begin, end, degrees in ((start, middle, angles[0]), (middle, start, angles[1])):
            dx, dy = end[0] - begin[0], end[1] - begin[1]
            chord = math.hypot(dx, dy)
            if chord == 0:
                raise ContextError('Circle arc endpoints coincide')
            theta = math.radians(degrees)
            offset = chord / (2 * math.tan(theta / 2))
            circles.append(((begin[0] + end[0]) / 2 - dy / chord * offset,
                            (begin[1] + end[1]) / 2 + dx / chord * offset,
                            chord / (2 * abs(math.sin(theta / 2)))))
    else:
        raise ContextError('Body curve is not a supported native circle or pair of semicircular arcs')
    vertices = [v for cx, cy, radius in circles for v in circle_vertices(cx, cy, radius, tolerance)]
    hull = MultiPoint(vertices).convex_hull
    if not isinstance(hull, Polygon):
        raise ContextError('Circle envelope is degenerate')
    return polygon([list(p) for p in hull.exterior.coords]), {
        'method': 'native_local_circles_circumscribed_convex_hull',
        'curve_tolerance_mil': tolerance,
        'derived_circles_mil': [{'x': x, 'y': y, 'radius': radius} for x, y, radius in circles],
        'note': 'Contains each exact source arc; conservative full-circle union hull, not an AABB.',
    }


def body_from_source(rows, tolerance):
    candidates = [r for r in rows if r['payload'].get('layerId') == 48]
    if len(candidates) != 1 or candidates[0]['type'] not in ('POLY', 'FILL'):
        raise ContextError('Exactly one layer-48 POLY/FILL body is required; provide a sourced local override')
    record = candidates[0]
    path = record['payload'].get('path')
    if not isinstance(path, list):
        raise ContextError('Missing native layer-48 body path')
    if len(path) >= 9 and path[2] == 'L' and all(not isinstance(v, str) for v in path[:2] + path[3:]):
        values = path[:2] + path[3:]
        if len(values) % 2:
            raise ContextError('Malformed native body path')
        points = [values[i:i + 2] for i in range(0, len(values), 2)]
        # The documented native polygon format automatically closes a final edge.
        result, proof = polygon(points), {'method': 'native_local_straight_polygon'}
    else:
        result, proof = circular_body(path, tolerance)
    return result, {**proof, 'layer_id': 48, 'primitive_id': record['id'], 'native_path': path,
                    'outline_stroke_width_mil': record['payload'].get('width'),
                    'outline_semantics': 'layer-48 body boundary centerline; stroke is not an added body margin'}


def validate_spec(spec):
    if not isinstance(spec, dict) or set(spec) - SPEC_FIELDS or spec.get('schema_version') != 1:
        raise ContextError('Spec must be schema_version 1 with documented fields only')
    if spec.get('pad_coordinates') not in ('world', 'component_local'):
        raise ContextError('Explicit pad_coordinates is required')
    for field in ('pad_coordinates_provenance', 'copper_layers_provenance', 'rules_provenance'):
        if not isinstance(spec.get(field), str) or not spec[field].strip():
            raise ContextError(f'Explicit {field} is required')
    stack = spec.get('copper_layers')
    if not isinstance(stack, list) or len(stack) < 2 or any(type(v) is not int for v in stack):
        raise ContextError('copper_layers must give the real physical stack order')
    rules = spec.get('rules')
    if not isinstance(rules, dict) or REQUIRED_RULES - set(rules):
        raise ContextError('Explicit physical rules required: ' + ', '.join(sorted(REQUIRED_RULES - set(rules or {}))))
    if 'copper_layers' in rules and rules['copper_layers'] != stack:
        raise ContextError('rules.copper_layers contradicts physical stack')
    rules_for({'rules': {**rules, 'copper_layers': stack}})
    if spec.get('footprint_via_movement', 'fixed') not in ('fixed', 'rigid'):
        raise ContextError('footprint_via_movement must be fixed or rigid')
    tolerance = number(spec.get('body_curve_tolerance', 0.001), 'body_curve_tolerance')
    if tolerance <= 0:
        raise ContextError('body_curve_tolerance must be positive')
    overrides = spec.get('footprint_overrides', {})
    if not isinstance(overrides, dict):
        raise ContextError('footprint_overrides must be an object keyed by inventory footprint_key')
    for key, value in overrides.items():
        if (not isinstance(value, dict) or set(value) - {'units', 'coordinate_system', 'body_polygon', 'courtyard_polygon', 'provenance'}
                or value.get('units') != 'mil' or value.get('coordinate_system') != 'footprint_local'
                or not isinstance(value.get('provenance'), str) or not value['provenance'].strip()):
            raise ContextError(f'{key}: override requires mil, footprint_local, polygon and source provenance')
        polygon(value.get('body_polygon'))
        if 'courtyard_polygon' in value:
            polygon(value['courtyard_polygon'])
    return tolerance


INVENTORY = """(async()=>{
  const doc=await activeDocument(eda), source=await readSource(eda);
  const padEvidence=p=>({id:id(p),x:state(p,'X'),y:state(p,'Y'),rotation:state(p,'Rotation'),
    layer:state(p,'Layer'),pad_number:state(p,'PadNumber'),raw_pad:state(p,'Pad')});
  const components=[];
  for(const c of await eda.pcb_PrimitiveComponent.getAll())components.push({
    id:id(c),designator:state(c,'Designator'),footprint:state(c,'Footprint'),
    x:state(c,'X'),y:state(c,'Y'),rotation:state(c,'Rotation'),layer:state(c,'Layer'),
    component_pins:(await c.getAllPins()).map(padEvidence)});
  const global_pads=(await eda.pcb_PrimitivePad.getAll()).map(padEvidence);
  const layers=await eda.pcb_Layer.getAllLayers();
  await activeDocument(eda,doc.uuid);
  if(sourceFingerprint(await readSource(eda))!==sourceFingerprint(source))fail('BOARD_CHANGED','PCB changed during inventory');
  return {document:doc,source:{document_uuid:doc.uuid,fingerprint:sourceFingerprint(source)},native_source:source,components,global_pads,layers};
})()"""


def download_footprint(bridge, reference, document_uuid, window_id):
    args = json.dumps({'reference': reference, 'document_uuid': document_uuid, 'max_bytes': MAX_FILE_BYTES})
    expression = """(async()=>{
      const a=ARGS;await activeDocument(eda,a.document_uuid);
      const f=await eda.sys_FileManager.getFootprintFileByFootprintUuid(a.reference.uuid,a.reference.libraryUuid,'elibz2');
      if(!f)fail('MISSING_FOOTPRINT_FILE','Native footprint download returned undefined');
      if(!Number.isInteger(f.size)||f.size<=0||f.size>a.max_bytes)fail('FILE_TOO_LARGE','Invalid footprint byte length');
      const bytes=new Uint8Array(await f.arrayBuffer());
      if(bytes.length!==f.size)fail('FILE_SIZE_MISMATCH','Footprint File changed while read');
      let binary='';for(let i=0;i<bytes.length;i+=32768)binary+=String.fromCharCode(...bytes.subarray(i,i+32768));
      await activeDocument(eda,a.document_uuid);
      return {name:f.name,size:f.size,mime_type:f.type,sha256:hashBytes(bytes),data_base64:btoa(binary)};
    })()""".replace('ARGS', args)
    result = bridge.execute(bridge._adapter_code(expression), window_id)
    raw = base64.b64decode(result['data_base64'], validate=True)
    if len(raw) != result['size'] or not 0 < len(raw) <= MAX_FILE_BYTES or digest(raw) != result['sha256']:
        raise ContextError('Footprint download size/hash mismatch')
    return raw, {key: value for key, value in result.items() if key != 'data_base64'}


def collect_context(bridge, output_dir, spec=None, window_id=None):
    directory = Path(output_dir)
    if directory.exists() or directory.is_symlink():
        raise ContextError('Output directory must be new; retained evidence is never overwritten')
    directory.mkdir(parents=True, exist_ok=False)
    receipt = {'success': False, 'status': 'collecting', 'read_only': True,
               'created_utc': datetime.now(timezone.utc).isoformat(), 'footprints': [],
               'context_ready': False, 'live_board_validated': False}
    receipt_path = directory / 'context-receipt.json'
    try:
        tolerance = validate_spec(spec) if spec is not None else 0.001
        selected = bridge._window(window_id)
        inventory = bridge.execute(bridge._adapter_code(INVENTORY), selected)
        native = inventory.pop('native_source')
        native_rows = records(native)
        (directory / 'board-native-source.epru').write_text(native, encoding='utf-8')
        write_json(directory / 'native-inventory.json', inventory)
        write_json(directory / 'native-rules-and-stack.json', [r for r in native_rows if r['type'] in (
            'RULE', 'RULE_TEMPLATE', 'RULE_SELECTOR', 'LAYER', 'LAYER_PHYS', 'CANVAS')])
        receipt.update(source=inventory['source'], window_id=selected)
        evidence_dir = directory / 'footprints'
        evidence_dir.mkdir()
        footprints, components, issues = {}, {}, []
        overrides = (spec or {}).get('footprint_overrides', {})
        for component in inventory['components']:
            reference = component.get('footprint')
            if not isinstance(reference, dict) or not reference.get('uuid') or not reference.get('libraryUuid'):
                raise ContextError('Every placed component needs an explicit footprint/library UUID')
            key = reference['libraryUuid'] + ':' + reference['uuid']
            component['footprint_key'] = key
            if key not in footprints:
                raw, native_file = download_footprint(bridge, reference, inventory['source']['document_uuid'], selected)
                stem = hashlib.sha256(key.encode('utf-8')).hexdigest()[:24]
                archive_path, source_path = evidence_dir / (stem + '.elibz2'), evidence_dir / (stem + '.elibu')
                archive_path.write_bytes(raw)
                item = {'footprint_key': key, 'reference': reference, 'native_file': native_file,
                        'archive_file': archive_path.relative_to(directory).as_posix()}
                receipt['footprints'].append(item)
                write_json(receipt_path, receipt)
                member, source, rows = footprint_source(raw, reference['uuid'])
                source_path.write_text(source, encoding='utf-8')
                item.update(source_file=source_path.relative_to(directory).as_posix(), archive_member=member,
                            source_sha256=digest(source.encode('utf-8')))
                try:
                    if key in overrides:
                        override = overrides[key]
                        body = polygon(override['body_polygon'])
                        proof = {'method': 'explicit_local_override', 'provenance': override['provenance'],
                                 'automatic_native_body_proof': False}
                    else:
                        body, proof = body_from_source(rows, tolerance)
                    item['body_evidence'] = proof
                    evidence = {'body_polygon': body, 'footprint_source': source,
                                'provenance': json.dumps({'source_sha256': item['source_sha256'], 'body': proof}, ensure_ascii=False)}
                    if 'courtyard_polygon' in overrides.get(key, {}):
                        evidence['courtyard_polygon'] = polygon(overrides[key]['courtyard_polygon'])
                    footprints[key] = evidence
                except ContextError as exc:
                    item['body_error'] = str(exc)
                    issues.append({'footprint_key': key, 'error': str(exc)})
                    footprints[key] = None
                write_json(receipt_path, receipt)
            if footprints[key] is not None:
                components[component['id']] = footprints[key]
        write_json(directory / 'native-inventory.json', inventory)
        receipt.update(component_count=len(inventory['components']), footprint_count=len(footprints))
        unknown = set(overrides) - set(footprints)
        if unknown:
            raise ContextError('Override keys absent from current PCB: ' + ', '.join(sorted(unknown)))
        check = bridge.execute(bridge._adapter_code(
            '(async()=>{await activeDocument(eda,' + json.dumps(inventory['source']['document_uuid']) +
            ');return sourceFingerprint(await readSource(eda));})()'), selected)
        if check != inventory['source']['fingerprint']:
            raise ContextError('PCB changed during footprint collection; start a fresh capture')
        if spec is None or issues:
            receipt.update(status='inputs_required', missing_spec=spec is None, issues=issues)
            return receipt
        context = {'document_uuid': inventory['source']['document_uuid'],
                   'pad_coordinates': spec['pad_coordinates'], 'pad_dimensions': 'footprint_source',
                   'pad_positions': 'footprint_source', 'copper_layers': spec['copper_layers'],
                   'footprint_via_movement': spec.get('footprint_via_movement', 'fixed'),
                   'rules': spec['rules'], 'components': components}
        for key in ('tags', 'board'):
            if key in spec:
                context[key] = spec[key]
        write_json(directory / 'capture-spec.json', spec)
        snapshot = bridge.export_snapshot(context, selected)
        if snapshot.get('metadata', {}).get('source', {}).get('document_uuid') != inventory['source']['document_uuid'] or snapshot.get('metadata', {}).get('source', {}).get('fingerprint') != inventory['source']['fingerprint']:
            raise ContextError('Final strict export source differs from collected native source')
        write_json(directory / 'context.json', context)
        write_json(directory / 'snapshot.json', snapshot)
        receipt.update(success=True, status='exported', context_ready=True,
                       component_count=len(components), footprint_count=len(footprints),
                       context_file='context.json', snapshot_file='snapshot.json',
                       scope='Source identity, native geometry inventory and declared context only; no PCB acceptance claimed.')
        return receipt
    except Exception as exc:
        receipt.update(status='failed', error=str(exc))
        raise
    finally:
        write_json(receipt_path, receipt)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-url', help='Existing loopback EasyEDA bridge origin')
    parser.add_argument('--window-id', help='Required when more than one window is connected')
    parser.add_argument('--timeout', type=float, default=60)
    parser.add_argument('--spec', type=Path, help='Explicit context capture contract; omit to collect evidence and missing inputs')
    parser.add_argument('--output-dir', type=Path, required=True, help='Fresh user-selected run directory')
    args = parser.parse_args(argv)
    try:
        spec = json.loads(args.spec.read_text(encoding='utf-8-sig')) if args.spec else None
        result = collect_context(EasyEDABridge(args.base_url, timeout=args.timeout), args.output_dir, spec, args.window_id)
        print(json.dumps({key: result.get(key) for key in ('success', 'status', 'context_ready', 'component_count', 'footprint_count', 'issues')}, ensure_ascii=False))
        print(str((args.output_dir / 'context-receipt.json').resolve()))
        return 0 if result['success'] else 2
    except Exception as exc:
        print('Context capture failed: ' + str(exc), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
