"""分区初始化到工作流验收的端到端检查，不放宽来源或标签比较。"""
from copy import deepcopy
from pathlib import Path
import json
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pcb_pipeline_common import normalize_board, fingerprint, PipelineError
from pcb_global_place import PlacementFailure
from pcb_pipeline import run_pipeline
from pcb_workflow import observe_result, run_workflow
from pcb_project_state import file_sha256, init_state, load_state, record_run, save_state, verify_candidate
from pcb_compact import compact_layout


def task_fixture():
    """返回只供离线测试的板外散落两端网络，空标签不预先补键。"""
    board = dict(units='mil', board_bbox=[0, 0, 400, 300], tags={}, routes={},
        metadata={'board_usage': 'test_only'},
        components=[dict(id=str(i), x=14000+i*1000, y=14000+i*900,
            rotation=0, layer=1, width=20, height=10,
            pads=[dict(padId='1', net='N', localX=0, localY=0,
                       shape='RECTANGLE', localW=4, localH=4)]) for i in range(2)],
        nets={'N': [dict(compId=str(i), padId='1') for i in range(2)]},
        rules=dict(copper_layers=[1, 2], minimum_trace_width=1, default_clearance=2,
                   clearance_matrix=[], board_edge_clearance=5, component_courtyard_margin=4))
    board['metadata']['source'] = dict(document_uuid='synthetic-initial-workflow',
        fingerprint='sha256:'+fingerprint(board), usage='test_only')
    return dict(schema_version=1, board=board, constraints=[], options=dict(
        placement=dict(method='analytic_sa', initialization='auto', rotations=[0, 90],
                       max_candidates=400, max_seconds=5, grid_step=10,
                       global_iterations=0, sa_temperatures=0, cluster_intra_steps=0),
        routing=dict(width=2, grid_step=5, max_seconds=5, max_net_seconds=3), max_iterations=1))


class InitialWorkflowTests(unittest.TestCase):
    def test_old_duplicate_round_does_not_exhaust_untried_seed(self):
        task = task_fixture()
        task["options"]["placement"]["seed"] = 42
        policy = dict(max_rounds=1, stagnation_rounds=1)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            base, state = root/"board.json", root/"state.json"
            base.write_text(json.dumps(task["board"]), encoding="utf-8")
            save_state(state, init_state(base))
            run_workflow(task, policy, root/"first", state)
            # 登记旧版重复执行首轮时实际产生的合同格式与同一候选。
            run_workflow(task, policy, root/"duplicate")
            run = root/"duplicate/round-001"
            result_file = run/"result.json"
            result = json.loads(result_file.read_text(encoding="utf-8"))
            result["candidate_rounds"][0].update(improved=False, consecutive_no_improvement=1,
                                                  actual_stop_reason="STAGNATION_METHOD_STOP")
            result_file.write_text(json.dumps(result), encoding="utf-8")
            prior = load_state(state)
            save_state(state, record_run(prior, run, "old-repeat"), expected_hash=file_sha256(state))
            report = run_workflow(task, policy, root/"resumed", state)
            trial = json.loads((root/"resumed/round-001/input-task.json").read_text(encoding="utf-8"))
            self.assertEqual((report["rounds"][0]["round"], trial["options"]["placement"]["seed"]), (1, 43))
            self.assertEqual(report["stop_reason"], "METHOD_SEQUENCE_EXHAUSTED")
            with self.assertRaisesRegex(PipelineError, "停滞"):
                run_workflow(task, policy, root/"genuine-stop", state)

    def test_resume_continues_seed_sequence_and_stagnation_like_uninterrupted_run(self):
        task = task_fixture()
        task["options"]["placement"]["seed"] = 42
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            base, state = root / "board.json", root / "state.json"
            base.write_text(json.dumps(task["board"]), encoding="utf-8")
            save_state(state, init_state(base))
            policy = dict(max_rounds=1, stagnation_rounds=1)
            first = run_workflow(task, policy, root / "first", state)
            second = run_workflow(task, policy, root / "second", state)
            whole = run_workflow(task, dict(policy, max_rounds=2), root / "whole")
            def seeds(*paths):
                return [json.loads((p / "input-task.json").read_text(encoding="utf-8"))
                        ["options"]["placement"]["seed"] for p in paths]
            self.assertEqual(seeds(root/"first/round-001", root/"second/round-001"), [42, 43])
            self.assertEqual(seeds(root/"whole/round-001", root/"whole/round-002"), [42, 43])
            fields = ("round", "improved", "consecutive_no_improvement", "actual_stop_reason")
            split = first["rounds"] + second["rounds"]
            self.assertEqual([[r[k] for k in fields] for r in split],
                             [[r[k] for k in fields] for r in whole["rounds"]])
            self.assertEqual(second["stop_reason"], whole["stop_reason"])

    def test_empty_tags_have_one_canonical_form_and_source_is_unchanged(self):
        board = task_fixture()['board']
        source = deepcopy(board)
        canonical = normalize_board(board)
        for tags in ({}, {'component_tags': {}}, {'net_tags': {}},
                     {'component_tags': {}, 'net_tags': {}}):
            trial = deepcopy(board)
            trial['tags'] = tags
            self.assertEqual(normalize_board(trial), canonical)
        self.assertEqual(board, source)
        self.assertEqual(canonical['tags'], {'component_tags': {}, 'net_tags': {}})

    def test_tag_types_and_unknown_fields_still_fail_closed(self):
        for tags in (None, [], {'component_tags': []}, {'net_tags': None}, {'unknown': {}}):
            board = task_fixture()['board']
            board['tags'] = tags
            with self.subTest(tags=tags), self.assertRaises(PipelineError):
                normalize_board(board)

    def test_empty_tags_cold_start_passes_independent_workflow_verification(self):
        task = task_fixture()
        source = deepcopy(task)
        result = run_pipeline(task)
        self.assertTrue(result['passed'], result)
        metrics, level = observe_result(result, task)
        self.assertEqual(level, 2)
        self.assertEqual(metrics['returned_routed_nets'], 1)
        self.assertEqual(task, source)

    def test_real_tag_changes_remain_rejected(self):
        task = task_fixture()
        result = run_pipeline(task)
        self.assertTrue(result['passed'], result)
        candidate = deepcopy(result['board'])
        candidate['tags']['net_tags']['N'] = {'type': 'NORMAL'}
        with self.assertRaises(PipelineError):
            verify_candidate(task, candidate, result)

    def test_unknown_final_cost_is_supported_by_pipeline_workflow_and_compact(self):
        import pcb_global_place
        original = pcb_global_place.evaluate_cost
        for entry in ('pipeline', 'compact'):
            task = task_fixture()
            calls = 0
            def score(*args, **kwargs):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise PlacementFailure('PLACEMENT_TIME_BUDGET', '评分预算耗尽', budget_scope='time')
                return original(*args, **kwargs)
            with self.subTest(entry=entry), patch('pcb_global_place.evaluate_cost', side_effect=score):
                if entry == 'pipeline':
                    result = run_pipeline(task)
                    self.assertTrue(result['passed'], result)
                    stats = result['attempts'][0]['stages'][0]['stats']
                    metrics, level = observe_result(result, task)
                    self.assertEqual(level, 2)
                    self.assertIsNotNone(metrics['hpwl'])
                else:
                    _, report = compact_layout(task['board'], [(400, 300)],
                        placement_options=task['options']['placement'], contract_centers=False)
                    self.assertTrue(report['geometry_passed'], report)
                    stats = report['selected']['placement']
                    self.assertIsNotNone(report['selected']['rudy_peak'])
                self.assertIsNone(stats['final_cost'])
                self.assertIsNone(stats['rudy'])
                self.assertFalse(stats['cost_evaluation_complete'])


if __name__ == '__main__':
    unittest.main()
