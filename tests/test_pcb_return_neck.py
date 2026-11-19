"""对实际铜桥检查最小颈宽，线宽投影、保护带与净空分别核验。"""
from pathlib import Path
import sys
import unittest

from shapely.geometry import box
from shapely.ops import unary_union

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pcb_pipeline_common import check_board, normalize_board, polygon_record
from pcb_return_path import check_return_paths


def bridge_board(width, points=(-75, 75)):
    """返回两侧宽 200 mil、桥长 4 mil 的双层合成板，按给定横坐标逐段布信号线。"""
    copper = unary_union([box(-120, -100, -2, 100), box(-2, -width/2, 2, width/2),
                          box(2, -100, 120, 100)])
    components, nets = [], {}
    for name, net, x, y, layer in (('A', 'SIG', points[0], 0, 1), ('B', 'SIG', points[-1], 0, 1),
                                   ('R', 'REF', -75, 50, 2)):
        components.append(dict(id=name, x=x, y=y, rotation=0, layer=layer, width=8, height=8,
                               pads=[dict(padId='1', net=net, localX=0, localY=0,
                                          layer=layer, shape='RECTANGLE', localW=8, localH=8)]))
        nets.setdefault(net, []).append(dict(compId=name, padId='1'))
    return normalize_board(dict(units='mil', board_bbox=[-150, -150, 150, 150], components=components,
        nets=nets, tags={}, metadata=dict(source=dict(document_uuid='reference-neck-fixture')),
        rules=dict(copper_layers=[1, 2], board_edge_clearance=5, component_courtyard_margin=0),
        routes=dict(traces=[dict(net='SIG', layer=1, width=10, sx=a, sy=0, ex=b, ey=0)
                            for a, b in zip(points, points[1:])],
                    copper_pours=[dict(net='REF', layer=2, filled_polygons=[polygon_record(copper)])])))


class ReturnNeckTests(unittest.TestCase):
    def report(self, board, width=40):
        self.assertEqual(check_board(board), [])
        return check_return_paths(board, dict(signal_nets=['SIG'], guard_band=0, min_neck_width=width,
            reference_layers=[dict(signal_layer=1, reference_layer=2, reference_net='REF')]), before=board)

    def test_short_narrow_bridge_with_full_trace_projection_fails(self):
        for points in ((-75, 75), (-75, 0, 75)):
            with self.subTest(points=points):
                report = self.report(bridge_board(20, points))
                self.assertTrue(report['complete'])
                self.assertFalse(report['passed'])
                self.assertTrue(any(row['code'] == 'REFERENCE_NECK_BELOW_LIMIT' for row in report['guard_band_missing']))
                self.assertEqual(report['trace_projection_missing'], [])
                self.assertEqual(report['reference_disconnections'], [])
                self.assertEqual(report['via_clearance_change'], [])

    def test_adequate_bridge_and_signal_confined_to_one_bank(self):
        for board in (bridge_board(60), bridge_board(40), bridge_board(20, (-100, -50))):
            self.assertTrue(self.report(board)['passed'])
        self.assertFalse(self.report(bridge_board(39.999))['passed'])
        self.assertTrue(self.report(bridge_board(20), width=0)['passed'])
