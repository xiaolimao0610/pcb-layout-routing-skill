"""当前快照与历史状态的来源核对；不连接EDA，不把索引有效当作现场有效。"""
from contextlib import redirect_stdout
from copy import deepcopy
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pcb_pipeline_common import PipelineError
from pcb_project_state import init_state, save_state, load_state, main
from test_initial_workflow import task_fixture


class CurrentSourceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.board = task_fixture()['board']
        self.baseline = self.root / 'baseline.json'
        self.current = self.root / 'current.json'
        self.state_path = self.root / 'project-state.json'
        self.write(self.baseline, self.board)
        self.state = init_state(self.baseline)
        save_state(self.state_path, self.state)
        self.before = self.state_path.read_bytes()

    @staticmethod
    def write(path, value):
        path.write_text(json.dumps(value), encoding='utf-8')

    def test_matching_current_source_accepts_without_mutation(self):
        self.write(self.current, self.board)
        self.assertEqual(load_state(self.state_path, current_board_path=self.current), self.state)
        self.assertEqual(self.state_path.read_bytes(), self.before)

    def test_changed_fingerprint_uuid_or_usage_is_rejected(self):
        for key, value in [('fingerprint', 'sha256:'+'f'*64), ('document_uuid', 'other-board'), ('usage', 'physical')]:
            current = deepcopy(self.board)
            current['metadata']['source'][key] = value
            if key == 'usage':
                current['metadata']['board_usage'] = value
            self.write(self.current, current)
            with self.subTest(field=key), self.assertRaisesRegex(PipelineError, '来源不一致'):
                load_state(self.state_path, current_board_path=self.current)
            self.assertEqual(self.state_path.read_bytes(), self.before)

    def test_existing_artifact_hash_validation_is_not_bypassed(self):
        self.write(self.current, self.board)
        self.baseline.write_text(self.baseline.read_text()+' ', encoding='utf-8')
        with self.assertRaisesRegex(PipelineError, '已变化'):
            load_state(self.state_path, current_board_path=self.current)

    def test_cli_explicitly_distinguishes_historical_and_current_check(self):
        self.write(self.current, self.board)
        for extras, checked in [([], False), (['--current-board', str(self.current)], True)]:
            with redirect_stdout(io.StringIO()) as output:
                code = main(['validate', '--state', str(self.state_path), *extras])
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(output.getvalue())['current_source_checked'], checked)
        current = deepcopy(self.board)
        current['metadata']['source']['fingerprint'] = 'sha256:'+'e'*64
        self.write(self.current, current)
        with redirect_stdout(io.StringIO()) as output:
            code = main(['validate', '--state', str(self.state_path), '--current-board', str(self.current)])
        self.assertEqual(code, 1)
        self.assertFalse(json.loads(output.getvalue())['valid'])


if __name__ == '__main__':
    unittest.main()
