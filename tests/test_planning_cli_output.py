"""验证布局、换板框与多轮求解的终端摘要和本地完整工件。"""
from contextlib import redirect_stdout
from io import StringIO
import json
from pathlib import Path
import tempfile
import unittest

import pcb_compact
import pcb_pipeline
import pcb_relayout
import pcb_workflow
from test_automation_failure_flow import blocked_task
from test_initial_workflow import task_fixture


def invoke(main, *args):
    with redirect_stdout(StringIO()) as stream:
        code = main([str(arg) for arg in args])
    return code, json.loads(stream.getvalue())


class PlanningOutputTests(unittest.TestCase):
    def test_validate_with_report_bounds_errors_and_preserves_raw_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            board = task_fixture()["board"]
            src, out = root/"board.json", root/"validation.json"
            src.write_text(json.dumps(board), encoding="utf-8")
            code, raw = invoke(pcb_pipeline.main, "validate", "--input", src)
            self.assertEqual(code, 1)
            self.assertGreater(len(raw["violations"]), 4)
            for full in (False, True):
                code, view = invoke(pcb_pipeline.main, "validate", "--input", src, "--output", out,
                                    *(["--full-output"] if full else []))
                self.assertEqual(code, 1)
                self.assertEqual(json.loads(out.read_text(encoding="utf-8")), raw)
                if full:
                    self.assertEqual(view, raw)
                else:
                    self.assertEqual(view["violation_count"], len(raw["violations"]))
                    self.assertEqual(len(view["violations"]), 4)
                    self.assertEqual(Path(view["report"]), out)

    def test_compact_metrics_without_stage_dump(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            board = task_fixture()["board"]
            for i, part in enumerate(board["components"]):
                part.update(x=50 + i*50, y=50)
            src = root/"board.json"
            src.write_text(json.dumps(board), encoding="utf-8")
            for full in (False, True):
                out = root/str(full)
                code, msg = invoke(pcb_compact.main, "--board", src, "--size", 400, 300,
                                   "--output-dir", out, *(["--full-output"] if full else []))
                self.assertEqual(code, 0)
                report = json.loads((out/"result.json").read_text(encoding="utf-8"))
                self.assertEqual(msg["selected"]["board_area_mil2"], report["selected"]["board_area_mil2"])
                self.assertIn("placement", report["selected"])
                if full:
                    self.assertEqual(msg["selected"], report["selected"])
                else:
                    self.assertNotIn("placement", msg["selected"])
                    self.assertFalse(msg["full_board_accepted"])
                    self.assertTrue(msg["preview_only"])

    def test_prepare_writes_every_pose_and_prints_only_count(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            inputs = dict(
                inspection=dict(source=dict(document_uuid="offline", fingerprint="sha256:"+"1"*64),
                                copper_layers=[1, 2], tracks=[], pours=[]),
                proposal=dict(reference_positions_used=False, board_bbox=dict(outline=[[0,0],[100,0],[100,100],[0,100]]),
                              initial_poses={"U1":dict(x=30,y=30,rotation=0,layer=1)}),
                cleanup=dict(lines=[], vias=[], pours=[], outline_line_ids=["e1","e2","e3","e4"]))
            args = ["prepare"]
            for key, data in inputs.items():
                path = root/(key+".json")
                path.write_text(json.dumps(data), encoding="utf-8")
                args.extend(["--"+key, path])
            for full in (False, True):
                out = root/(str(full)+".json")
                code, msg = invoke(pcb_relayout.main, *args, "--output", out, *(["--full-output"] if full else []))
                plan = json.loads(out.read_text(encoding="utf-8"))
                self.assertEqual(code, 0)
                self.assertEqual(plan["proposal"]["initial_poses"], inputs["proposal"]["initial_poses"])
                self.assertFalse(msg["full_board_accepted"])
                self.assertEqual(msg["job_key"], plan["job_key"])
                if full:
                    self.assertEqual(msg, plan)
                else:
                    self.assertNotIn("proposal", msg)
                    self.assertEqual(msg["components"], 1)

    def test_workflow_failure_points_to_last_round(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src, policy, out = root/"in.json", root/"policy.json", root/"run"
            src.write_text(json.dumps(blocked_task(anneal_steps=0)), encoding="utf-8")
            policy.write_text('{"max_rounds":1}', encoding="utf-8")
            code, msg = invoke(pcb_workflow.main, "--input", src, "--policy", policy, "--output-dir", out)
            self.assertEqual(code, 1)
            self.assertFalse(msg["passed"])
            self.assertFalse(msg["full_board_accepted"])
            self.assertEqual(msg["failure"]["nets"], ["A"])
            self.assertEqual(Path(msg["round_report"]), out/"round-001/result.json")
            self.assertIn("attempts", json.loads(Path(msg["round_report"]).read_text(encoding="utf-8")))
