"""真实几何的失败闭环集成反例；不替换布局器、布线器或任何检查器。"""
from copy import deepcopy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pcb_pipeline import _task, run_pipeline
from pcb_routing import check_routed_nets
from test_pipeline_automation import task_fixture


def blocked_task(*, anneal_steps=8):
    """返回可连通 B 与被单层禁布墙隔开的 A；只授权局部移动 A 的两端。

    A 两端分别限定在墙两侧，禁止换层。测试只断言有限搜索失败及安全停止，
    不把一个节点的预算搜索结论当作一般不可布线证明。所有算法均真实运行。
    """
    task = task_fixture()
    board = task['board']
    board['components'][3]['x'] = 100
    for component in board['components']:
        if component['id'].startswith('B'):
            component['fixed'] = True
    board['routes']['keepouts'] = [dict(points=[[140,0],[160,0],[160,250],[140,250]], layers=[1])]
    board['routes']['traces'] = [dict(id='original-B', net='B', layer=1,
                                    sx=40, sy=200, ex=60, ey=200, width=2)]
    task['options'].update(max_iterations=3, automation=dict(enabled=True, max_seconds=8,
        max_layout_repairs=2, repair_radius=5, max_moved_components=2, stagnation_rounds=1))
    task['options']['routing'].update(allow_vias=False, trace_layers=[1], net_order=['B','A'],
        max_expansions=1, max_passes=1, max_seconds=2, max_net_seconds=1)
    task['options']['placement'].update(grid_step=10, seed=7,
        sa_temperatures=0 if anneal_steps == 0 else 1, sa_steps_per_temperature=anneal_steps,
        regions={'A0':[0,0,130,150], 'A1':[170,0,300,150]})
    return task


class ActualFailureFlowTests(unittest.TestCase):
    def assert_failed_only(self, report):
        """失败报告不得把部分完成缓存作为可写回整板交付。"""
        self.assertFalse(report['passed'], report)
        self.assertIsNone(report['board'])
        self.assertIsNone(report['writeback_plan'])
        self.assertNotEqual(report.get('full_board_accepted'), True)
        self.assertTrue(all(not entry['passed'] for entry in report['attempts']))

    def test_actual_failure_checkpoint_local_movement_retry_then_stagnation(self):
        task = blocked_task()
        unchanged = deepcopy(task)
        original, _, _ = _task(task)
        checkpoints = []
        report = run_pipeline(task, checkpoint_callback=checkpoints.append)
        self.assert_failed_only(report)
        self.assertEqual(task, unchanged)
        self.assertEqual(len(report['attempts']), 2)
        first, second = report['attempts']
        self.assertEqual(first['diagnostics']['code'], 'SEARCH_BUDGET')
        self.assertEqual(second['diagnostics']['code'], 'SEARCH_BUDGET')
        self.assertIn('AUTOMATION_STAGNATION', second['feedback_error'])
        self.assertEqual(first['local_layout_repair']['moved_candidates'], ['A0','A1'])
        self.assertEqual(first['local_layout_repair']['affected_nets'], ['A'])
        self.assertEqual(first['local_layout_repair']['retained_generated']['traces'], 1)
        self.assertTrue(first['local_layout_repair']['original_copper_preserved'])
        self.assertTrue(second['stages'][0]['stats']['repair_changed'])
        self.assertTrue(set(second['stages'][0]['stats']['moved_components']) <= {'A0','A1'})

        failures = [c for c in checkpoints if c['routing_checkpoint']['failure']]
        self.assertEqual([c['attempt'] for c in failures], [0,1])
        for checkpoint in checkpoints:
            self.assertFalse(checkpoint['full_board_accepted'])
            self.assertFalse(checkpoint['writeback_allowed'])
            route = checkpoint['routing_checkpoint']
            candidate = route['board']
            self.assertEqual(candidate['routes']['traces'][:1], original['routes']['traces'])
            self.assertEqual(candidate['nets'], original['nets'])
            self.assertEqual([c for c in candidate['components'] if c['id'].startswith('B')],
                             [c for c in original['components'] if c['id'].startswith('B')])
            if 'B' in route['completed_nets']:
                self.assertEqual(check_routed_nets(candidate, ['B']), [])
        self.assertEqual(failures[-1]['routing_checkpoint']['completed_nets'], ['B'])
        self.assertEqual(failures[-1]['routing_checkpoint']['pending_nets'], ['A'])
        self.assertGreater(failures[-1]['usage']['elapsed_seconds'], failures[0]['usage']['elapsed_seconds'])
        self.assertEqual(failures[-1]['completed_history'], [1])

    def test_exhausted_checkpoint_resume_keeps_stagnation_and_budget_history(self):
        task, checkpoints = blocked_task(), []
        first = run_pipeline(task, checkpoint_callback=checkpoints.append)
        self.assert_failed_only(first)
        saved, revalidated = checkpoints[-1], []
        resumed = run_pipeline(task, resume_checkpoint=saved, checkpoint_callback=revalidated.append)
        self.assert_failed_only(resumed)
        self.assertEqual(len(resumed['attempts']), 1)
        entry = resumed['attempts'][0]
        self.assertTrue(entry['stages'][0]['stats']['restored_calculation_input'])
        self.assertFalse(entry['stages'][0]['stats']['placement_replayed'])
        self.assertEqual(entry['diagnostics']['code'], 'CHECKPOINT_EXHAUSTED')
        self.assertIn('AUTOMATION_STAGNATION', entry['feedback_error'])
        self.assertEqual(len(revalidated), 1)
        self.assertEqual(revalidated[0]['completed_history'], [1])
        self.assertEqual(revalidated[0]['routing_checkpoint']['pass_index'], 2)
        self.assertEqual(revalidated[0]['routing_checkpoint']['usage']['expansions'],
                         saved['routing_checkpoint']['usage']['expansions'])
        self.assertGreaterEqual(resumed['cumulative_elapsed_seconds'], saved['usage']['elapsed_seconds'])

    def test_no_available_improving_pose_stops_without_accepting_partial_board(self):
        checkpoints = []
        report = run_pipeline(blocked_task(anneal_steps=0), checkpoint_callback=checkpoints.append)
        self.assert_failed_only(report)
        self.assertEqual(len(report['attempts']), 2)
        self.assertIn('local_layout_repair', report['attempts'][0])
        self.assertIn('cannot change any movable component', report['attempts'][1]['error'])
        self.assertFalse(report['attempts'][1]['stages'][0]['stats']['repair_changed'])
        self.assertEqual({c['attempt'] for c in checkpoints}, {0})
        self.assertEqual(checkpoints[-1]['routing_checkpoint']['pending_nets'], ['A'])

    def test_real_tiny_total_budget_exits_before_any_candidate_claim(self):
        task = blocked_task()
        task['options']['automation']['max_seconds'] = 1e-12
        checkpoints = []
        report = run_pipeline(task, checkpoint_callback=checkpoints.append)
        self.assert_failed_only(report)
        self.assertTrue(any('AUTOMATION_TIME_BUDGET' in item for item in report['violations']))
        self.assertEqual(checkpoints, [])


if __name__ == '__main__':
    unittest.main()
