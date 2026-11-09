"""原生铜辐条通用离线反例；仅合成夹具，不含真实工程 ID、坐标或来源。"""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from shapely.geometry import LineString, Point
from pcb_geometry import GeometryError, filled_region_bounds
from pcb_native_poured_geometry import native_stroke_region, scale_native_paths, validate_native_pour_snapshot


def synthetic_snapshot():
    """无入参，返回含八个非共线孔锚点的合成板和证据；不读取或修改任何原生工程。"""
    uuid, parent = 'synthetic-pcb', 'synthetic-pour'
    poured_id = json.dumps(['POURED', parent], separators=(',', ':'))
    points = [(11, 17), (23, 31), (37, 43), (53, 59), (67, 79), (83, 97), (101, 113), (127, 139)]
    paths = [[0, 0, 'L', 150, 0, 150, 150, 0, 150]]
    vias, records = [], [({'type': 'DOCHEAD'}, {'docType': 'PCB', 'uuid': uuid}),
                         ({'type': 'POUR', 'id': parent}, {'netName': 'REFERENCE'})]
    for index, (x, y) in enumerate(points):
        paths.append([x+1.2, y, 'ARC', 180, x-1.2, y, 'ARC', 180, x+1.2, y])
        via = dict(id=f'synthetic-via-{index}', x=x*10, y=y*10, net='OTHER', hole=6, diameter=12, via_type=0)
        vias.append(via)
        records.append(({'type': 'VIA', 'id': via['id']},
                        dict(viaType='NORMAL', netName='OTHER', centerX=via['x'], centerY=via['y'], holeDiameter=6, viaDiameter=12)))
    regions = [dict(id='synthetic-solid', fill=True, strokeWidth=0, path=paths),
               dict(id='synthetic-spoke', fill=False, strokeWidth=1, path=[[3, 5, 'L', 7, 9]])]
    payload = dict(pourFill=regions)
    records.append(({'type': 'POURED', 'id': poured_id}, payload))
    native = '\n'.join(json.dumps(h, separators=(',', ':'))+'||'+json.dumps(p, separators=(',', ':'))+'|' for h, p in records)
    source = dict(document_uuid=uuid, fingerprint='sha256:'+hashlib.sha256(native.encode()).hexdigest())
    calibration = dict(method='native_poured_via_anchors', source_native=native, pour_id=parent,
                       poured_id=poured_id, region_id='synthetic-solid', raw_paths=paths, via_api=vias, tolerance_mil=1e-7)
    profile = dict(source=source, coordinate_scale=10, mode='local_preview_only', calibration=calibration)
    poured = dict(id=poured_id, source_kind='Poured', parent_id=parent, layer=1, net='REFERENCE',
        filled_polygons=[{'native_compound_path': scale_native_paths(paths, 10)}, native_stroke_region(regions[1]['path'], 1, 10)],
        metadata=dict(native_source_record=dict(type='POURED', id=poured_id, payload=payload),
                      native_poured_regions=[dict(id=r['id'], fill=r['fill'], lineWidth=r['strokeWidth'], path=deepcopy(r['path'])) for r in regions]))
    board = dict(metadata=dict(source=source, native_pour_geometry=profile), routes=dict(copper_pours=[poured]))
    return board, dict(source=source, coordinate_profile=deepcopy(profile), poured_ids=[poured_id])


class NativeCopperStrokeTests(unittest.TestCase):
    def test_conservative_caps_and_units(self):
        for path in ([[0, 0, 'L', 4, 0]], [[1, 2, 'L', 4, 7]], [[2, 7, 'L', 2, -1]]):
            region = native_stroke_region(path, 1, 10)
            bounds = filled_region_bounds(region)
            line = LineString(region['native_stroke_path']['points'])
            self.assertEqual(region['native_stroke_path']['width'], 10)
            for cap in (1, 2, 3):
                actual = line.buffer(5, cap_style=cap)
                self.assertTrue(actual.covers(bounds.inner))
                self.assertTrue(bounds.outer.covers(actual))

    def test_unknown_or_degenerate_strokes_rejected(self):
        for paths, width in [([[0, 0, 'ARC', 90, 1, 1]], 1), ([[0, 0, 'L', 0, 0]], 1),
                             ([[0, 0, 'L', 1, 1]], 0), ([[0, 0, 'L', 1, 1]], True),
                             ([[0, 0, 'L', 1, 1]], float('nan')),
                             ([[0, 0, 'L', 1, 1], [1, 1, 'L', 2, 2]], 1)]:
            with self.assertRaises(GeometryError):
                native_stroke_region(paths, width, 10)

    def test_unproven_caps_do_not_prove_connectivity(self):
        region = native_stroke_region([[0, 0, 'L', 1, 0]], 1, 10)
        bounds = filled_region_bounds(region)
        self.assertFalse(bounds.inner.intersects(Point(12, 0)))
        self.assertTrue(bounds.outer.intersects(Point(12, 0)))
        self.assertFalse(bounds.inner.intersects(Point(5, 5.01)))
        with self.assertRaises(GeometryError):
            filled_region_bounds(dict(region, points=[[0, 0], [1, 1], [2, 0]]))

    def test_valid_source_and_single_mutations(self):
        board, evidence = synthetic_snapshot()
        proof = validate_native_pour_snapshot(board, evidence)
        self.assertEqual(proof['matched_circle_count'], 8)
        changed = deepcopy(board)
        changed['routes']['copper_pours'][0]['filled_polygons'][1]['native_stroke_path']['width'] = 1
        with self.assertRaises(GeometryError):
            validate_native_pour_snapshot(changed, evidence)
        changed = deepcopy(board)
        changed['routes']['copper_pours'][0]['filled_polygons'].pop()
        with self.assertRaises(GeometryError):
            validate_native_pour_snapshot(changed, evidence)

    def test_coordinated_width_mutation_rejected(self):
        board, evidence = synthetic_snapshot()
        changed = deepcopy(board)
        poured = changed['routes']['copper_pours'][0]
        poured['metadata']['native_source_record']['payload']['pourFill'][1]['strokeWidth'] = 2
        raw = poured['metadata']['native_poured_regions'][1]
        raw['lineWidth'] = 2
        poured['filled_polygons'][1] = native_stroke_region(raw['path'], 2, 10)
        self.assertEqual(changed['metadata']['native_pour_geometry'], board['metadata']['native_pour_geometry'])
        with self.assertRaisesRegex(GeometryError, '完整原生来源'):
            validate_native_pour_snapshot(changed, evidence)

    def test_solid_api_mutation_and_missing_object_rejected(self):
        board, evidence = synthetic_snapshot()
        changed = deepcopy(board)
        raw = changed['routes']['copper_pours'][0]['metadata']['native_poured_regions'][0]
        raw['fill'], raw['lineWidth'] = False, 3
        with self.assertRaisesRegex(GeometryError, 'API 填充状态'):
            validate_native_pour_snapshot(changed, evidence)
        changed = deepcopy(board)
        changed['routes']['copper_pours'].clear()
        with self.assertRaisesRegex(GeometryError, '对象全集'):
            validate_native_pour_snapshot(changed, evidence)


if __name__ == '__main__':
    unittest.main(verbosity=2)
