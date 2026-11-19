"""Gerber 补证合成反例；不访问或修改真实 EDA 工程。"""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import io
from contextlib import redirect_stdout
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_pcb_native_poured_strokes import synthetic_snapshot
from pcb_gerber_evidence import (Budget, EvidenceError, VerifiedGerberContext, build_geometry_context,
                                 main, parse_gerber, source_fingerprint)
from pcb_geometry import filled_region_bounds
from pcb_pipeline_common import check_board
from shapely.geometry import Point


GERBER = '%FSLAX26Y26*%\n%MOMM*%\n%ADD10C,0.254*%\nG75*\nD10*\nX762000Y1270000D02*\nG01X1778000Y2286000D01*\nM02*'


def fixture(directory, gerber=GERBER):
    """输入临时目录和合成 Gerber，返回完整来源链及收据；文件错误按原异常上抛。"""
    board, _ = synthetic_snapshot()
    profile = board['metadata']['native_pour_geometry']
    native = profile['calibration']['source_native'].replace('{"netName":"REFERENCE"}', '{"netName":"REFERENCE","layerId":1}')
    profile['calibration']['source_native'] = native
    board['metadata']['source']['fingerprint'] = source_fingerprint(native)
    board.update(units='mil', components=[], nets={}, tags={}, board_bbox=[-20, -20, 1520, 1520], rules={'copper_layers': [1, 2]})
    archive = Path(directory) / 'fabrication.zip'
    with zipfile.ZipFile(archive, 'w') as z:
        z.writestr('top.gbr', gerber)
        z.writestr('bottom.gbr', '%FSLAX26Y26*%%MOMM*%M02*')
    data = archive.read_bytes()
    receipt = dict(success=True, status='exported', source=deepcopy(board['metadata']['source']), package_kind='gerber',
                   files=[dict(kind='fabrication', name=archive.name, size=len(data), sha256='sha256:'+hashlib.sha256(data).hexdigest())])
    return board, receipt, archive, {1: 'top.gbr', 2: 'bottom.gbr'}


class GerberEvidenceTests(unittest.TestCase):
    def test_non_mil_input_is_rejected_before_geometry_evidence(self):
        with tempfile.TemporaryDirectory() as d:
            args = fixture(d)
            for units in ('mm', None):
                args[0]['units'] = units
                with self.assertRaisesRegex(EvidenceError, 'mil'):
                    build_geometry_context(*args)

    def test_round_caps_bound_ids_and_keep_original_outer(self):
        with tempfile.TemporaryDirectory() as d:
            args = fixture(d); original = deepcopy(args[0])
            context, report = build_geometry_context(*args)
            self.assertTrue(report['complete']); self.assertEqual(report['verified'], 1)
            self.assertFalse(report['full_board_accepted'])
            pour = args[0]['routes']['copper_pours'][0]; region = pour['filled_polygons'][1]
            old = filled_region_bounds(region); new = context.region_bounds(pour, 1, region, .001)
            self.assertGreater(new.inner.area, old.inner.area)
            self.assertTrue(new.outer.equals_exact(old.outer, 0))
            self.assertTrue(new.outer.covers(new.inner))
            self.assertEqual(original, args[0])

    def test_full_scan_rejects_clear_unknown_holes_macro_and_late_commands(self):
        for command in ('%LPC*%', '%ADD11C,0.2X0.1*%', '%AMX*1,1,1,0,0*%', '%SRX2Y2I1J1*%',
                        '%AB*%', '%MIA1B0*%', 'G91*', 'G74*', '%IPNEG*%'):
            with self.subTest(command=command), self.assertRaises(EvidenceError):
                parse_gerber(GERBER.replace('M02*', command+'M02*'))
        with self.assertRaises(EvidenceError):
            parse_gerber(GERBER+'D10*')

    def test_source_receipt_archive_and_layer_mutations_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            args = fixture(d)
            for mutate in ('source', 'receipt', 'layer', 'width', 'native'):
                changed = list(deepcopy(args))
                if mutate == 'source': changed[0]['metadata']['source']['document_uuid'] = 'wrong'
                if mutate == 'receipt': changed[1]['source']['fingerprint'] = 'sha256:wrong'
                if mutate == 'layer': changed[0]['routes']['copper_pours'][0]['layer'] = 2
                if mutate == 'width': changed[0]['routes']['copper_pours'][0]['filled_polygons'][1]['native_stroke_path']['width'] += 1
                if mutate == 'native': changed[0]['metadata']['native_pour_geometry']['calibration']['source_native'] += '\n'
                with self.subTest(mutation=mutate), self.assertRaises(ValueError):
                    build_geometry_context(*changed)
            args[2].write_bytes(args[2].read_bytes()+b'changed')
            with self.assertRaisesRegex(EvidenceError, 'ZIP'):
                build_geometry_context(*args)

    def test_swapped_member_does_not_match_and_missing_layers_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            board, receipt, archive, layers = fixture(d)
            _, report = build_geometry_context(board, receipt, archive, {1: layers[2], 2: layers[1]})
            self.assertFalse(report['complete']); self.assertEqual(report['verified'], 0)
            with self.assertRaises(EvidenceError):
                build_geometry_context(board, receipt, archive, {1: layers[1]})

    def test_fake_context_stale_board_and_stale_object_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            args = fixture(d); context, _ = build_geometry_context(*args)
            with self.assertRaises(EvidenceError): VerifiedGerberContext(args[0], {})
            self.assertTrue(any('INVALID_INPUT:' in e for e in check_board(args[0], geometry_context={'cap_style': 'round'})))
            changed = deepcopy(args[0]); changed['nets']['hidden'] = []
            with self.assertRaises(EvidenceError): context.validate_board(changed)
            pour = deepcopy(args[0]['routes']['copper_pours'][0]); pour['net'] = 'WRONG'
            with self.assertRaises(EvidenceError): context.region_bounds(pour, 1, pour['filled_polygons'][1], .001)

    def test_quantization_uncertainty_erodes_certified_caps(self):
        with tempfile.TemporaryDirectory() as d:
            args = fixture(d, GERBER.replace('X1778000Y2286000', 'X1778001Y2286001'))
            context, report = build_geometry_context(*args)
            self.assertTrue(report['complete'])
            self.assertGreater(report['strokes'][0]['uncertainty_mil'], .00005)

    def test_budget_exhaustion_is_not_no_solution(self):
        for limits in ({'max_commands': 1}, {'max_primitives': .5}, {'seconds': 1e-12}):
            with self.assertRaisesRegex(EvidenceError, 'budget_exhausted'):
                parse_gerber(GERBER, budget=Budget(limits))
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaisesRegex(EvidenceError, 'budget_exhausted'):
                build_geometry_context(*fixture(d), limits={'max_match_tests': .5})

    def test_fingerprint_excludes_only_known_volatile_header_fields(self):
        a = '{"type":"DOCHEAD"}||{"docType":"PCB","client":"a","uuid":"u","updateTime":3,"version":"v","editVersion":"x"}|\r\n'
        b = a.replace('"client":"a"', '"client":"b"').replace('"updateTime":3', '"updateTime":4')
        self.assertEqual(source_fingerprint(a), source_fingerprint(b))
        self.assertNotEqual(source_fingerprint(a), source_fingerprint(a.replace('"editVersion":"x"', '"editVersion":"y"')))

    def test_ordered_clear_and_later_dark_restore(self):
        prefix = '%FSLAX26Y26*%%MOMM*%%ADD10C,2*%%ADD11C,1*%D10*X0Y0D03*'
        clear = '%LPC*%D11*X0Y0D03*'
        image = parse_gerber(prefix+clear+'M02*', profile='final_image')['final_image']
        self.assertFalse(image.outer.covers(Point(0, 0)))
        self.assertTrue(image.inner.covers(Point(.75, 0)))
        restored = parse_gerber(prefix+clear+'%LPD*%X0Y0D03*M02*', profile='final_image')['final_image']
        self.assertTrue(restored.inner.covers(Point(0, 0)))

    def test_region_hole_arc_and_aperture_transform(self):
        region = ('%FSLAX26Y26*%%MOMM*%G75*G36*X0Y0D02*G01X4000000Y0D01*'
                  'X4000000Y4000000D01*X0Y4000000D01*X0Y0D01*'
                  'X1000000Y1000000D02*X3000000Y1000000D01*X3000000Y3000000D01*'
                  'X1000000Y3000000D01*X1000000Y1000000D01*G37*M02*')
        image = parse_gerber(region, profile='final_image')['final_image']
        self.assertTrue(image.inner.covers(Point(.5, .5)))
        self.assertFalse(image.outer.covers(Point(2, 2)))
        aperture = '%FSLAX26Y26*%%MOMM*%%ADD10R,2X1*%%LMX*%%LR90*%%LS2*%D10*X5000000Y7000000D03*M02*'
        transformed = parse_gerber(aperture, profile='final_image')['final_image']
        self.assertTrue(transformed.inner.covers(Point(5, 8.9)))
        self.assertFalse(transformed.outer.covers(Point(6.1, 7)))
        # LM/LR/LS 变换光圈，不应变换绘制坐标。
        self.assertAlmostEqual(transformed.inner.centroid.x, 5)
        self.assertAlmostEqual(transformed.inner.centroid.y, 7)
        arc = '%FSLAX26Y26*%%MOMM*%%ADD10C,0.2*%G75*D10*X1000000Y0D02*G03X0Y1000000I-1000000J0D01*M02*'
        curved = parse_gerber(arc, profile='final_image')['final_image']
        self.assertTrue(curved.inner.covers(Point(2**-.5, 2**-.5)))
        self.assertFalse(curved.outer.covers(Point(.5, .5)))

    def test_final_clear_cannot_certify_erased_cap_or_core(self):
        # 在末端之外切除端帽最远处；不能把被清铜删除的点重新作为导体。
        clear = '%ADD11C,0.08*%%LPC*%D11*X1862853Y2370853D03*'
        with tempfile.TemporaryDirectory() as d:
            args = fixture(d, GERBER.replace('M02*', clear+'M02*'))
            context, report = build_geometry_context(*args, profile='final_image')
            self.assertTrue(report['complete'])
            pour = args[0]['routes']['copper_pours'][0]
            bounds = context.region_bounds(pour, 1, pour['filled_polygons'][1], .001)
            self.assertFalse(bounds.inner.covers(Point(1.862853/.0254, 2.370853/.0254)))
        clear_core = '%ADD11C,0.1*%%LPC*%D11*X1270000Y1778000D03*'
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaisesRegex(EvidenceError, 'conflict'):
                build_geometry_context(*fixture(d, GERBER.replace('M02*', clear_core+'M02*')), profile='final_image')

    def test_final_image_still_refuses_unsupported_and_vertex_budget(self):
        for command in ('%AMX*1,1,1,0,0*%', '%SRX2Y2I1J1*%', '%AB*%'):
            with self.assertRaises(EvidenceError):
                parse_gerber(GERBER.replace('M02*', command+'M02*'), profile='final_image')
        with self.assertRaisesRegex(EvidenceError, 'budget_exhausted'):
            parse_gerber(GERBER, profile='final_image', budget=Budget({'max_vertices': 1}))

    def test_final_narrow_clear_at_old_inner_edge_is_not_restored(self):
        clear = '%ADD11C,0.00002*%%LPC*%D11*X1180201Y1867799D03*'
        with tempfile.TemporaryDirectory() as d:
            args = fixture(d, GERBER.replace('M02*', clear+'M02*'))
            context, _ = build_geometry_context(*args, profile='final_image')
            pour = args[0]['routes']['copper_pours'][0]; region = pour['filled_polygons'][1]
            point = Point(1.180201/.0254, 1.867799/.0254)
            self.assertTrue(filled_region_bounds(region).inner.covers(point))
            self.assertFalse(context.region_bounds(pour, 1, region, .001).inner.covers(point))

    def test_archive_parse_uses_same_hashed_bytes(self):
        with tempfile.TemporaryDirectory() as d:
            args = fixture(d)
            original = Path.read_bytes
            def replace_after_read(path):
                data = original(path)
                if path == args[2]: path.write_bytes(b'changed after hashing read')
                return data
            with patch('pcb_gerber_evidence.Path.read_bytes', replace_after_read):
                _, report = build_geometry_context(*args)
            self.assertTrue(report['complete'])
            self.assertEqual(report['archive_sha256'], args[1]['files'][0]['sha256'])

    def test_cli_output_collision_never_overwrites_input(self):
        with tempfile.TemporaryDirectory() as d:
            board, receipt, archive, layers = fixture(d)
            inputs = {'board': board, 'receipt': receipt, 'layer-map': layers}
            paths = {key: Path(d)/(key+'.json') for key in inputs}
            for key, value in inputs.items(): paths[key].write_text(json.dumps(value), encoding='utf-8')
            paths['archive'] = archive
            for output in paths.values():
                previous = output.read_bytes()
                argv = [item for key, path in paths.items() for item in ('--'+key, str(path))] + ['--out', str(output)]
                with redirect_stdout(io.StringIO()): self.assertEqual(main(argv), 2)
                self.assertEqual(output.read_bytes(), previous)

    def test_context_identity_and_nested_proof_are_read_only(self):
        with tempfile.TemporaryDirectory() as d:
            context, _ = build_geometry_context(*fixture(d))
            with self.assertRaises(AttributeError): context._signature = 'wrong'
            with self.assertRaises(TypeError): next(iter(context._proofs.values()))['margin_mil'] = -1

    def test_conflicting_file_function_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            args = fixture(d, GERBER.replace('%MOMM*%', '%MOMM*%%TF.FileFunction,Copper,L2,Bot*%'))
            with self.assertRaisesRegex(EvidenceError, '错层'):
                build_geometry_context(*args)

    def test_comment_and_extended_file_attributes_have_identical_effect(self):
        for profile in ('round_caps', 'final_image'):
            for template in ('%{}*%', 'G04 #@! {}*'):
                for attribute, error in (('TF.FileFunction,Copper,L2,Bot', '错层'),
                                         ('TF.FileFunction,Copper,L1,Bot', '错层'),
                                         ('TF.FileFunction', '错层'),
                                         ('TF.FilePolarity', '极性'),
                                         ('TF.FilePolarity,Negative', '极性'),
                                         ('TF.FileFunction,Copper,L1,Top', None),
                                         ('TF.FilePolarity,Positive', None)):
                    with self.subTest(profile=profile, template=template, attribute=attribute), tempfile.TemporaryDirectory() as d:
                        args = fixture(d, GERBER.replace('G75*', template.format(attribute)+'G75*'))
                        if error:
                            with self.assertRaisesRegex(EvidenceError, error):
                                build_geometry_context(*args, profile=profile)
                        else:
                            _, report = build_geometry_context(*args, profile=profile)
                            self.assertTrue(report['complete'])
                            self.assertFalse(report['full_board_accepted'])

    def test_comment_attributes_share_duplicate_and_end_of_file_checks(self):
        function = 'TF.FileFunction,Copper,L1,Top'
        with self.assertRaisesRegex(EvidenceError, '重复'):
            parse_gerber(GERBER.replace('G75*', '%'+function+'*%G04 #@! '+function+'*G75*'))
        with self.assertRaisesRegex(EvidenceError, 'M02'):
            parse_gerber(GERBER+'G04 #@! TF.FilePolarity,Positive*')
        self.assertIsNone(parse_gerber(GERBER.replace('G75*', 'G04 TF.FilePolarity,Negative*G75*'))['file_function'])

    def test_before_build_candidate_is_not_claimed_native_verified(self):
        with tempfile.TemporaryDirectory() as d:
            board, receipt, archive, layers = fixture(d)
            board['routes']['traces'] = [dict(id='candidate-line', net='REFERENCE', layer=1,
                                             sx=50, sy=50, ex=100, ey=50, width=10)]
            context, report = build_geometry_context(board, receipt, archive, layers)
            self.assertEqual(report['source_scope'], 'native_poured_records')
            self.assertFalse(report['non_pour_source_verified'])
            self.assertFalse(report['full_board_accepted'])
            self.assertTrue(report['input_board_fingerprint'].startswith('sha256:'))
            context.validate_board(board)
            board['routes']['traces'][0]['ex'] += 1
            with self.assertRaises(EvidenceError): context.validate_board(board)

    def test_region_midway_polarity_is_rejected(self):
        region = '%FSLAX26Y26*%%MOMM*%G36*X0Y0D02*G01X1000000Y0D01*%LPD*%X0Y0D01*G37*M02*'
        for profile in ('round_caps', 'final_image'):
            with self.assertRaisesRegex(EvidenceError, '极性'):
                parse_gerber(region, profile=profile)


if __name__ == '__main__':
    unittest.main()
