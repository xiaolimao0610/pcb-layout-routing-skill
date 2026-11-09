"""跨器件同网焊盘反例；不把正常同网走线或封装内部叠加误判为装配冲突。"""
from copy import deepcopy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pcb_pipeline_common import PipelineError, check_board, normalize_board, rules_for
from pcb_placement import place_components
from pcb_solver_hardened import verify_full_board_drc_and_connectivity


def fixture():
    """两器件本体互不重叠，但外伸的同网焊盘在世界坐标相互重叠。"""
    components = [dict(id='A', x=0, y=0, layer=1, rotation=0, width=4, height=4,
                       pads=[dict(padId='1', net='GND', localX=10, localY=0,
                                  shape='RECTANGLE', localW=16, localH=8)]),
                  dict(id='B', x=30, y=0, layer=1, rotation=0, width=4, height=4,
                       pads=[dict(padId='1', net='GND', localX=-18, localY=0,
                                  shape='RECTANGLE', localW=16, localH=8)])]
    return dict(units='mil', board_bbox=[-100, -100, 200, 150], components=components,
                nets={'GND': [dict(compId=c['id'], padId='1') for c in components]},
                tags={}, routes={}, metadata={'board_usage':'test_only'},
                rules=dict(board_edge_clearance=0, component_courtyard_margin=0,
                           default_clearance=0, clearance_matrix=[], copper_layers=[1, 2]))


def errors(board):
    return [e for e in check_board(board, ignore_connectivity=True)
            if e.startswith('COMPONENT_PAD_CLEARANCE:')]


class ComponentPadClearanceTests(unittest.TestCase):
    def test_same_net_different_bodies_rejected_without_mutation(self):
        board = fixture()
        original = deepcopy(board)
        report = check_board(board)
        self.assertEqual(len(errors(board)), 1)
        self.assertFalse(any(e.startswith('COMPONENT_CLEARANCE:') for e in report))
        self.assertFalse(any(e.startswith('DISCONNECTED:') for e in report))
        self.assertEqual(board, original)

    def test_clearance_is_independent_from_electrical_class_rules(self):
        board = fixture()
        board['components'][1]['x'] = 49  # 两焊盘之间净距 5 mil。
        self.assertEqual(errors(board), [])
        board['rules']['component_pad_clearance'] = 6
        self.assertEqual(len(errors(board)), 1)
        board['rules']['component_pad_clearance'] = 5
        self.assertEqual(errors(board), [])
        board['components'][1]['x'] = 44  # 零净距仍是接触。
        board['rules']['component_pad_clearance'] = 0
        self.assertEqual(len(errors(board)), 1)

    def test_empty_net_pads_are_not_exempt(self):
        board = fixture()
        for c in board['components']:
            c['pads'][0]['net'] = ''
        board['nets'] = {}
        self.assertEqual(len(errors(board)), 1)

    def test_same_footprint_and_normal_trace_contacts_remain_valid(self):
        board = fixture()
        a = board['components'][0]
        second = deepcopy(a['pads'][0])
        second.update(padId='2', localX=11)
        a['pads'].append(second)
        board['components'] = [a]
        board['nets']['GND'] = [dict(compId='A', padId=str(i)) for i in (1, 2)]
        board['rules']['component_pad_clearance'] = 100
        self.assertEqual(check_board(board), [])
        board = fixture()
        board['components'][1]['x'] = 50
        board['routes']['traces'] = [dict(net='GND', layer=1, sx=10, sy=0, ex=32, ey=0, width=2)]
        self.assertEqual(check_board(board), [])

    def test_opposite_smd_sides_do_not_collide_but_through_pad_does(self):
        board = fixture()
        b = board['components'][1]
        b.update(layer=2, x=-8)  # 底层镜像后第二焊盘同样位于 x=10。
        self.assertEqual(errors(board), [])
        b['pads'][0].update(layer=12, hole=2, plated=True)
        self.assertEqual(len(errors(board)), 1)
        board['components'][0]['pads'][0].update(layer=12, hole=2, plated=True)
        self.assertEqual(len(errors(board)), 1)  # 两层共同接触只报一次。

    def test_mounting_side_does_not_override_explicit_pad_layer(self):
        board = normalize_board(fixture())
        board['components'][1].update(layer=2, x=-8)
        # 显式焊盘层仍是顶层，不能仅凭本体在底层就跳过两顶层焊盘冲突。
        self.assertEqual(board['components'][1]['pads'][0]['layer'], 1)
        self.assertEqual(len(errors(board)), 1)

    def test_rotated_polygon_and_conservative_curve_check(self):
        board = fixture()
        a, b = board['components']
        a.update(rotation=90)
        b.update(x=0, y=28, rotation=90)
        b['pads'][0]['shape'] = 'CIRCLE'
        b['pads'][0]['diameter'] = 8
        b['pads'][0]['localW'] = 8
        # A 的盘中心为 (0,10)，B 的盘中心也为 (0,10)。
        self.assertEqual(len(errors(board)), 1)

    def test_spatial_index_matches_linear_reference(self):
        for gap in (0, 5, 1000):
            board = fixture()
            board['rules']['component_pad_clearance'] = gap
            args = ({c['id']:c for c in board['components']}, board['routes'],
                    board['nets'], board['tags'], board['board_bbox'], rules_for(board))
            self.assertEqual(verify_full_board_drc_and_connectivity(*args, use_index=True),
                             verify_full_board_drc_and_connectivity(*args, use_index=False))

    def test_invalid_gap_and_unknown_field_still_rejected(self):
        for gap in (-1, float('inf'), float('nan'), True):
            board = fixture()
            board['rules']['component_pad_clearance'] = gap
            with self.assertRaises(PipelineError):
                normalize_board(board)
        board = fixture()
        board['rules']['component_pad_clearence'] = 0
        with self.assertRaises(PipelineError):
            normalize_board(board)

    def test_automatic_placement_rechecks_pad_overlap(self):
        board = fixture()
        board['rules']['component_pad_clearance'] = 4
        placed, _ = place_components(board, dict(method='analytic_sa', initialization='auto',
            rotations=[0], global_iterations=0, sa_temperatures=0, cluster_intra_steps=0,
            max_candidates=300, max_seconds=5))
        self.assertEqual(check_board(placed, ignore_connectivity=True), [])
        self.assertNotEqual(placed['components'], normalize_board(board)['components'])


if __name__ == '__main__':
    unittest.main()
