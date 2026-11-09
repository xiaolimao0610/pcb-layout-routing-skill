"""续算与局部布局闭环反例；不能以进度工件替代独立验收。"""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pcb_automation import (local_layout_input, read_checkpoint, validate_options,
                            verify_calculation_board, write_checkpoint)
from pcb_pipeline import run_pipeline, _task, main
from pcb_pipeline_common import PipelineError, fingerprint, normalize_board
from pcb_project_state import verify_candidate
from pcb_workflow import run_workflow


def task_fixture():
    """构造分隔的两个完整网络，真实算法在小预算内可重复验收。"""
    components = []
    for net, y in [('A', 50), ('B', 200)]:
        for index, x in enumerate([40, 260]):
            components.append(dict(id=net+str(index), x=x, y=y, rotation=0, layer=1,
                width=20, height=20, pads=[dict(padId='1', net=net, localX=0, localY=0,
                shape='RECTANGLE', localW=10, localH=10)]))
    board = dict(units='mil', board_bbox=[0, 0, 300, 250], tags={}, routes={},
        components=components, nets={n:[dict(compId=n+str(i), padId='1') for i in range(2)] for n in ['A','B']},
        rules=dict(copper_layers=[1,2], minimum_trace_width=1, default_clearance=2,
                   clearance_matrix=[], board_edge_clearance=3, component_courtyard_margin=2),
        metadata={'board_usage':'test_only'})
    board['metadata']['source'] = dict(document_uuid='automation-fixture',
        fingerprint='sha256:'+fingerprint(board), usage='test_only')
    return dict(schema_version=1, board=board, options=dict(max_iterations=2,
        placement=dict(enabled=True, method='analytic_sa', initialization='preserve', rotations=[0],
                       global_iterations=0, sa_temperatures=0, cluster_intra_steps=0, max_seconds=2),
        routing=dict(checkpoint=True, width=2, grid_step=5, max_seconds=10, max_net_seconds=3)))


class AutomationTests(unittest.TestCase):
    def calculate(self, task=None):
        task = task or task_fixture()
        checkpoints = []
        result = run_pipeline(task, checkpoint_callback=checkpoints.append)
        self.assertTrue(result['passed'], result)
        self.assertTrue(checkpoints)
        return task, result, checkpoints[-1]

    def test_checkpoint_resume_skips_placement_and_revalidates_result(self):
        task, result, checkpoint = self.calculate()
        self.assertFalse(checkpoint['writeback_allowed'])
        with patch('pcb_pipeline.place_components', side_effect=AssertionError('不能重放布局')):
            resumed = run_pipeline(task, resume_checkpoint=checkpoint)
        self.assertTrue(resumed['passed'], resumed)
        self.assertEqual(result['board'], resumed['board'])
        self.assertGreaterEqual(resumed['cumulative_elapsed_seconds'], checkpoint['usage']['elapsed_seconds'])
        self.assertEqual(verify_candidate(task, resumed['board'], resumed), 'offline_geometry')

    def test_tampered_task_and_unknown_fields_fail_without_board(self):
        task, _, checkpoint = self.calculate()
        for mutation in ('source','options','unknown','time'):
            t, c = deepcopy(task), deepcopy(checkpoint)
            if mutation == 'source': t['board']['metadata']['source']['document_uuid'] = 'other'
            elif mutation == 'options': t['options']['routing']['width'] = 3
            elif mutation == 'unknown': c['trusted'] = True
            else: c['usage']['elapsed_seconds'] = -1
            result = run_pipeline(t, resume_checkpoint=c)
            self.assertFalse(result['passed'], mutation)
            self.assertIsNone(result['board'])
            self.assertIsNone(result['writeback_plan'])

    def test_forged_routing_input_cannot_move_fixed_or_change_rules(self):
        task = task_fixture()
        task['board']['components'][0]['fixed'] = True
        task, _, checkpoint = self.calculate(task)
        for mutation in ('pose','rules','netlist','metadata'):
            c = deepcopy(checkpoint)
            board = c['routing_input']
            if mutation == 'pose': board['components'][0]['x'] += 10
            elif mutation == 'rules': board['rules']['default_clearance'] = 0
            elif mutation == 'netlist': board['nets']['A'].pop()
            else: board['metadata']['source']['fingerprint'] = 'sha256:'+'0'*64
            result = run_pipeline(task, resume_checkpoint=c)
            self.assertFalse(result['passed'], mutation)
            self.assertIsNone(result['writeback_plan'])

    def test_cumulative_automation_time_is_not_reset_on_resume(self):
        task = task_fixture()
        task['options']['automation'] = {'enabled':True,'max_seconds':20}
        task, _, checkpoint = self.calculate(task)
        checkpoint['usage']['elapsed_seconds'] = 21
        with patch('pcb_pipeline.place_components', side_effect=AssertionError('已耗尽预算')):
            result = run_pipeline(task, resume_checkpoint=checkpoint)
        self.assertFalse(result['passed'])
        self.assertTrue(any('TIME_BUDGET' in v for v in result['violations']))

    def test_outer_elapsed_cannot_hide_inner_elapsed(self):
        task, _, checkpoint = self.calculate()
        checkpoint['usage']['elapsed_seconds'] = 0
        result = run_pipeline(task, resume_checkpoint=checkpoint)
        self.assertFalse(result['passed'])
        self.assertIn('累计耗时', result['violations'][0])

    def test_last_plan_verification_overrun_cannot_report_success(self):
        import pcb_pipeline
        import time
        task = task_fixture()
        task['options']['automation'] = {'enabled':True, 'max_seconds':20}
        clock, build, offset = time.monotonic, pcb_pipeline.build_writeback_plan, [0]
        def slow_plan(*args, **kwargs):
            result = build(*args, **kwargs)
            offset[0] += 21
            return result
        with patch('pcb_pipeline.build_writeback_plan', side_effect=slow_plan), \
                patch('pcb_pipeline.time.monotonic', side_effect=lambda: clock()+offset[0]):
            result = run_pipeline(task)
        self.assertFalse(result['passed'])
        self.assertIsNone(result['board'])
        self.assertFalse(result['attempts'][-1]['passed'])

    def test_independent_candidate_review_rejects_changed_cell_relation(self):
        task = task_fixture()
        task['options']['placement']['functional_cells'] = [dict(id='pair', anchor={'compId':'A0'},
            provenance='合成测试的明确相对关系', relations=[dict(neighbor={'compId':'A1'},
            sides=['right'], distances=[220], rotations=[0])])]
        task, result, _ = self.calculate(task)
        changed = deepcopy(result['board'])
        changed['components'][1]['x'] -= 1
        with self.assertRaisesRegex(PipelineError, '功能单元'):
            verify_candidate(task, changed, result)

    def test_local_feedback_removes_only_affected_generated_copper(self):
        task = task_fixture()
        original, options, _ = _task(task)
        checkpoint = deepcopy(original)
        checkpoint['routes']['traces'] = [dict(net=n, layer=1, sx=40, sy=y, ex=260, ey=y, width=2)
                                           for n,y in [('A',50),('B',200)]]
        settings = validate_options({'enabled':True, 'max_moved_components':2, 'repair_radius':10}, options)
        result, placement, evidence = local_layout_input(original, checkpoint, options,
            dict(failed_nets=['A'], hotspots=[]), settings)
        self.assertEqual(placement['movable_ids'], ['A0','A1'])
        self.assertEqual(result['routes']['traces'], [checkpoint['routes']['traces'][1]])
        self.assertEqual(evidence['removed_generated']['traces'], 1)
        self.assertEqual(result['nets'], original['nets'])
        verify_calculation_board(original, options, result)

    def test_original_copper_and_fixed_components_are_never_ripped_or_moved(self):
        task = task_fixture()
        task['board']['routes']['traces'] = [dict(net='A', layer=1, sx=40, sy=50, ex=260, ey=50, width=2)]
        original, options, _ = _task(task)
        settings = validate_options({'enabled':True}, options)
        with self.assertRaisesRegex(PipelineError, 'LOCAL_LAYOUT_NOT_FOUND'):
            local_layout_input(original, original, options, {'failed_nets':['A'],'hotspots':[]}, settings)
        changed = deepcopy(original)
        changed['routes']['traces'] = []
        with self.assertRaises(PipelineError):
            verify_calculation_board(original, options, changed)

    def test_new_options_are_explicit_and_strict(self):
        task = task_fixture()
        for value in ({'enable':True},{'max_seconds':False},{'max_moved_components':0}):
            with self.assertRaises(PipelineError): validate_options(value, task['options'])
        task['options']['placement']['method'] = 'legacy'
        with self.assertRaises(PipelineError): validate_options({'enabled':True}, task['options'])
        task['options']['placement']['method'] = 'analytic_sa'
        task['options']['routing']['fanout'] = {'enabled':True}
        with self.assertRaisesRegex(PipelineError, 'BGA'):
            validate_options({'enabled':True}, task['options'])

    def test_file_digest_and_duplicate_fields_are_checked(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'checkpoint.json'
            write_checkpoint(path, {'ok':1})
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            self.assertEqual(read_checkpoint(path,digest), {'ok':1})
            path.write_text('{"ok":1,"ok":2}', encoding='utf-8')
            with self.assertRaises(PipelineError): read_checkpoint(path,digest)
            with self.assertRaises(PipelineError): read_checkpoint(path,hashlib.sha256(path.read_bytes()).hexdigest())

    def test_oversized_checkpoint_is_not_written_as_resumable(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'checkpoint.json'
            with self.assertRaisesRegex(PipelineError, '32MiB'):
                write_checkpoint(path, {'data':'x'*(32*1024*1024)})
            self.assertFalse(path.exists())
            self.assertEqual(list(Path(folder).iterdir()), [])

    def test_cli_and_workflow_store_progress_separately_from_acceptance(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            task = task_fixture()
            source = directory/'task.json'
            source.write_text(json.dumps(task),encoding='utf-8')
            from contextlib import redirect_stdout
            from io import StringIO
            with redirect_stdout(StringIO()):
                self.assertEqual(main(['solve','--input',str(source),'--output-dir',str(directory/'run')]),0)
            result = json.loads((directory/'run'/'result.json').read_text(encoding='utf-8'))
            ref = result['calculation_checkpoint']
            self.assertFalse(ref['writeback_allowed'])
            with redirect_stdout(StringIO()):
                self.assertEqual(main(['solve','--input',str(source),'--output-dir',str(directory/'resume'),
                    '--resume-checkpoint',ref['path'],'--checkpoint-sha256',ref['sha256']]),0)
            summary = run_workflow(task, {'max_rounds':1}, directory/'workflow')
            self.assertTrue(summary['passed'], summary)
            report = json.loads((directory/'workflow'/'round-001'/'result.json').read_text(encoding='utf-8'))
            self.assertIn('calculation_checkpoint', report)


if __name__ == '__main__': unittest.main()
