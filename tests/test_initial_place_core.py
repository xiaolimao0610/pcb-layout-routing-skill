"""分区初始化的真实精检、刚体语义、来源不变和预算反例。"""
from copy import deepcopy
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from shapely.geometry import box
from pcb_pipeline_common import normalize_board, check_board, PipelineError
from pcb_placement import place_components, _envelope, _options
from pcb_global_place import PlacementFailure, _Budget
from pcb_pipeline_common import rules_for


def fixture(count=6):
    """返回无既有铜的合成散落板；封装/网络保持完整，不包含任何实板信息。"""
    parts, nets = [], {}
    for index in range(count):
        cid, net = str(index), f'N{index//2}'
        parts.append(dict(id=cid, x=14000+index*1000, y=14000+index*900,
                          rotation=0, layer=1, width=20, height=10,
                          pads=[dict(padId='1', net=net, localX=0, localY=0,
                                     shape='RECTANGLE', localW=4, localH=4)]))
        nets.setdefault(net, []).append(dict(compId=cid, padId='1'))
    return normalize_board(dict(units='mil', board_bbox=[0,0,400,300], components=parts,
        nets=nets, routes={}, metadata={'board_usage':'test_only'}, tags={},
        rules=dict(copper_layers=[1,2], minimum_trace_width=1, default_clearance=2,
                   clearance_matrix=[], board_edge_clearance=5, component_courtyard_margin=4)))


def options(**extra):
    result = dict(method='analytic_sa', initialization='auto', rotations=[0,90],
                  max_candidates=400, max_seconds=5, grid_step=10,
                  global_iterations=0, sa_temperatures=0, cluster_intra_steps=0)
    result.update(extra)
    return result


class InitialPlacementCoreTests(unittest.TestCase):
    def test_regions_are_used_before_search_and_source_is_unchanged(self):
        board = fixture()
        source = deepcopy(board)
        cfg = options(functional_groups=[dict(id='left',components=['0','1','2'],region=[20,20,150,140]),
                                         dict(id='right',components=['3','4','5'],region=[220,160,380,280])])
        result, stats = place_components(board, cfg)
        self.assertEqual(check_board(result, ignore_connectivity=True), [])
        self.assertEqual(board, source)
        self.assertEqual(result['nets'], board['nets'])
        self.assertEqual(len(result['components']), 6)
        self.assertEqual(stats['initialization']['status'], 'passed')
        for group in cfg['functional_groups']:
            for component in result['components']:
                if component['id'] in group['components']:
                    self.assertTrue(box(*group['region']).covers(_envelope(component,.1)))

    def test_arbitrary_import_coordinates_do_not_define_initial_seed(self):
        board, shifted = fixture(), fixture()
        for index, component in enumerate(shifted['components']):
            component['x'] = -90000-index*3300
            component['y'] = 80000-index*2800
        cfg = options(initialization='region_pack')
        a,_ = place_components(board, cfg)
        b,_ = place_components(shifted, cfg)
        self.assertEqual(a['components'], b['components'])

    def test_auto_keeps_legal_baseline_when_following_optimization_hits_budget(self):
        board = fixture(2)
        for index, component in enumerate(board['components']):
            component.update(x=100+index*40,y=100)
        result, stats = place_components(board, options(max_candidates=1,sa_temperatures=1,sa_steps_per_temperature=1))
        self.assertEqual(result['components'], board['components'])
        self.assertEqual(stats['initialization']['status'], 'skipped')
        self.assertTrue(stats['budget_exhausted'])

    def test_functional_groups_intersect_existing_regions(self):
        board = fixture(1)
        cfg = options(regions={'0':[100,100,150,150]}, functional_groups=[dict(id='zone',components=['0'],region=[50,50,140,140])])
        normalized = _options(cfg, board, rules_for(board))
        self.assertEqual(normalized['regions']['0'].bounds, (100,100,140,140))
        result,_ = place_components(board,cfg)
        self.assertTrue(box(100,100,140,140).covers(_envelope(result['components'][0],.1)))
        cfg['functional_groups'][0]['region'] = [200,200,250,250]
        with self.assertRaises(PipelineError):
            place_components(board,cfg)

    def test_unknown_or_duplicate_group_fields_fail_closed(self):
        board = fixture(2)
        for groups in ([dict(id='z',components=['missing'],region=[0,0,100,100])],
                       [dict(id='z',components=['0'],region=[0,0,100,100], guess=True)],
                       [dict(id='a',components=['0'],region=[0,0,100,100]),dict(id='b',components=['0'],region=[0,0,100,100])]):
            with self.subTest(groups=groups), self.assertRaises(PipelineError):
                place_components(board, options(functional_groups=groups))

    def test_large_rigid_cluster_is_not_silently_dissolved(self):
        board = fixture(2)
        with self.assertRaises(PlacementFailure) as caught:
            place_components(board, options(clusters=[dict(id='rigid',components=['0','1'])]))
        self.assertEqual(caught.exception.diagnostics['code'],'PLACEMENT_UNIT_FIT')
        self.assertIn('functional_groups',str(caught.exception))
        result,_ = place_components(board,options(functional_groups=[dict(id='zone',components=['0','1'],region=[10,10,390,290])]))
        self.assertEqual(check_board(result,ignore_connectivity=True),[])

    def test_fitting_rigid_cluster_preserves_relative_pose(self):
        board = fixture(2)
        board['components'][1].update(x=14040,y=14000)
        result,_ = place_components(board,options(rotations=[0],clusters=[dict(id='rigid',components=['0','1'])]))
        a,b = result['components']
        self.assertAlmostEqual(b['x']-a['x'],40)
        self.assertAlmostEqual(b['y']-a['y'],0)

    def test_fixed_member_outside_region_fails_without_partial_output(self):
        board=fixture(2)
        board['components'][0]['locked']=True
        source=deepcopy(board)
        with self.assertRaises(PlacementFailure) as caught:
            place_components(board,options())
        self.assertEqual(caught.exception.diagnostics['code'],'PLACEMENT_FIXED_GEOMETRY')
        self.assertEqual(source,board)

    def test_edge_components_precede_larger_internal_components(self):
        board=fixture(2)
        board['components'][1].update(width=60,height=50)
        result,stats=place_components(board,options(edge_components=[dict(component='0',edge='right',target_distance=5)]))
        self.assertEqual(stats['initialization']['placement_order'][0],'component:0')
        envelope=_envelope(result['components'][0],.1)
        self.assertAlmostEqual(envelope.bounds[2],395)

    def test_candidate_and_time_budgets_report_not_found(self):
        board=fixture()
        with self.assertRaises(PlacementFailure) as caught:
            place_components(board,options(max_candidates=1))
        self.assertEqual(caught.exception.diagnostics['code'],'PLACEMENT_CANDIDATE_BUDGET')
        self.assertTrue(caught.exception.diagnostics['stats']['budget_exhausted'])
        stats={'candidate_checks':0}
        with patch('pcb_global_place.time.monotonic',side_effect=[0,2]):
            budget=_Budget({'max_seconds':1,'max_candidates':400},stats)
            with self.assertRaises(PlacementFailure) as timed:
                budget.check()
        self.assertEqual(timed.exception.diagnostics['code'],'PLACEMENT_TIME_BUDGET')

    def test_verified_initializer_survives_score_timeout_with_unknown_cost(self):
        import pcb_global_place
        original = pcb_global_place.evaluate_cost
        calls = 0
        def bounded_cost(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise PlacementFailure('PLACEMENT_TIME_BUDGET','评分预算耗尽',budget_scope='time')
            return original(*args, **kwargs)
        board=fixture(2)
        with patch('pcb_global_place.evaluate_cost',side_effect=bounded_cost):
            result,stats=place_components(board,options())
        self.assertEqual(check_board(result,ignore_connectivity=True),[])
        self.assertEqual(len(result['components']),2)
        self.assertIsNone(stats['final_cost'])
        self.assertIsNone(stats['rudy'])
        self.assertFalse(stats['cost_evaluation_complete'])
        self.assertTrue(stats['budget_exhausted'])

    def test_nonrectangular_outline_and_cutout_are_not_filled_by_bbox(self):
        board=fixture(3)
        board['board_bbox']=dict(outline=[[0,0],[300,0],[300,100],[100,100],[100,300],[0,300]],
                                 cutouts=[[[8,8],[65,8],[65,65],[8,65]]])
        result,_=place_components(board,options(max_candidates=1000))
        self.assertEqual(check_board(result,ignore_connectivity=True),[])
        self.assertEqual(result['board_bbox'],board['board_bbox'])
        from pcb_pipeline_common import board_polygon
        available=board_polygon(board).buffer(-5)
        for component in result['components']:
            self.assertTrue(available.covers(_envelope(component,.1)))

    def test_foreign_old_trace_remains_an_exact_obstacle(self):
        board=fixture(2)
        board['components'][1].update(x=300,y=200,locked=True)
        board['components'][1]['pads'][0]['net']='OLD'
        board['nets']={'N0':[dict(compId='0',padId='1')], 'OLD':[dict(compId='1',padId='1')]}
        board['routes']['traces']=[dict(net='OLD',layer=1,sx=10,sy=15,ex=150,ey=15,width=8)]
        source=deepcopy(board)
        result,stats=place_components(board,options(rotations=[0]))
        self.assertEqual(check_board(result,ignore_connectivity=True),[])
        self.assertEqual(result['routes'],source['routes'])
        self.assertEqual(result['components'][1],source['components'][1])
        self.assertEqual(board,source)
        self.assertGreater(stats['initialization']['exact_checks'],2)

    def test_rigid_rotation_obeys_each_members_distinct_region(self):
        board=fixture(2)
        board['components'][1].update(x=14040,y=14000)
        cfg=options(clusters=[dict(id='rigid',components=['0','1'])],
                    regions={'0':[50,50,80,80],'1':[50,90,80,120]})
        result,_=place_components(board,cfg)
        for component in result['components']:
            self.assertEqual(component['rotation'],90)
            self.assertTrue(box(*cfg['regions'][component['id']]).covers(_envelope(component,.1)))
        self.assertAlmostEqual(result['components'][1]['y']-result['components'][0]['y'],40)

    def test_opposite_side_bodies_may_share_xy(self):
        board=fixture(2)
        board['components'][1]['layer']=2
        # fixture 已规范化并固定焊盘层；换成底层夹具时需同步其真实焊盘层。
        board['components'][1]['pads'][0]['layer']=2
        cfg=options(regions={'0':[50,50,80,80],'1':[50,50,80,80]},rotations=[0])
        result,_=place_components(board,cfg)
        a,b=result['components']
        self.assertEqual((a['x'],a['y']),(b['x'],b['y']))
        self.assertEqual((a['layer'],b['layer']),(1,2))
        self.assertEqual(check_board(result,ignore_connectivity=True),[])


if __name__ == '__main__':
    unittest.main()
