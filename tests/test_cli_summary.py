"""命令行按需展示，完整证据、失败范围、退出码及写前保护保持可复核。"""
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pcb_pipeline import failure_summary, main
from test_automation_failure_flow import blocked_task
from test_initial_workflow import task_fixture
from test_pipeline_automation import task_fixture as two_net_fixture


class SolveSummaryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def solve(self, task, *flags):
        source = self.root / "task.json"
        source.write_text(json.dumps(task), encoding="utf-8")
        output = self.root / "run"
        stdout, stderr = StringIO(), StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = main(["solve", "--input", str(source), "--output-dir", str(output), *flags])
        return code, stdout.getvalue(), stderr.getvalue()

    def test_two_component_success_is_one_line_and_full_mode_matches_local_report(self):
        task = task_fixture()
        code, out, err = self.solve(task)
        self.assertEqual((code, len(out.splitlines()), err), (0, 1, ""))
        brief = json.loads(out)
        report = Path(brief["report"])
        detail = json.loads(report.read_text(encoding="utf-8"))
        self.assertEqual((brief["status"], brief["passed"], brief["full_board_accepted"]),
                         ("offline_passed", True, False))
        self.assertEqual(brief["nets"], {"total": 1, "verified_offline": 1, "deferred_ground": 0})
        self.assertNotIn("attempts", brief)
        self.assertNotIn("diagnostic", brief)
        self.assertTrue(detail["attempts"][0]["stages"])
        for name in brief["artifacts"]:
            self.assertTrue((report.parent / name).is_file(), name)
        code, out, err = self.solve(task, "--overwrite", "--full-output")
        self.assertEqual((code, err), (0, ""))
        self.assertGreater(len(out.splitlines()), 1)
        self.assertEqual(json.loads(out), json.loads(report.read_text(encoding="utf-8")))

    def test_routing_failure_only_names_the_blocked_net_and_keeps_full_local_diagnostics(self):
        code, out, err = self.solve(blocked_task(anneal_steps=0))
        self.assertEqual((code, err), (1, ""))
        brief = json.loads(out)
        diag = brief["diagnostic"]
        self.assertEqual(diag["nets"], ["A"])
        self.assertEqual(diag["net_count"], 1)
        self.assertEqual(diag["code"], "SEARCH_BUDGET")
        self.assertIn("feedback", diag["stop_reason"])
        self.assertEqual(diag["network_details"][0]["source_terminals"], [["A0", "1"]])
        self.assertEqual(diag["network_details"][0]["target_terminals"], [["A1", "1"]])
        self.assertNotIn("verified_offline", brief["nets"])
        self.assertNotIn("stats", diag)
        self.assertNotIn("artifacts", brief)
        report = Path(brief["report"])
        detail = json.loads(report.read_text(encoding="utf-8"))
        self.assertTrue(detail["violations"])
        self.assertTrue(detail["attempts"][0]["diagnostics"]["stats"])
        self.assertFalse((report.parent / "writeback-plan.json").exists())

    def test_awaiting_ground_retains_distinct_signal_scope(self):
        task = two_net_fixture()
        task["options"]["routing"].update(ground_strategy="plane_then_drc", ground_nets=["B"])
        code, out, err = self.solve(task)
        self.assertEqual((code, err), (0, ""))
        brief = json.loads(out)
        self.assertEqual(brief["status"], "awaiting_native_ground")
        self.assertFalse(brief["full_board_accepted"] or brief["passed"])
        self.assertTrue(brief["signal_stage_passed"])
        self.assertIn("native_drc", brief["pending_stages"])
        self.assertEqual(brief["nets"], {"total": 2, "verified_offline": 1, "deferred_ground": 1})

    def test_early_refusal_does_not_refer_to_or_overwrite_an_older_report(self):
        report = self.root / "run" / "result.json"
        report.parent.mkdir()
        report.write_text("old", encoding="utf-8")
        code, out, err = self.solve(task_fixture())
        self.assertEqual((code, out), (1, ""))
        brief = json.loads(err)
        self.assertIsNone(brief["report"])
        self.assertNotIn("nets", brief["diagnostic"])
        self.assertIn("already contains", brief["diagnostic"]["message"])
        self.assertEqual(report.read_text(encoding="utf-8"), "old")

    def test_invalid_task_has_local_error_evidence_and_no_invented_net(self):
        code, out, err = self.solve({"schema_version": -1})
        self.assertEqual((code, err), (1, ""))
        brief = json.loads(out)
        self.assertNotIn("nets", brief["diagnostic"])
        self.assertIn("schema_version", brief["diagnostic"]["message"])
        report = json.loads(Path(brief["report"]).read_text(encoding="utf-8"))
        self.assertEqual(report["violations"], [brief["diagnostic"]["message"]])

    def test_return_path_failures_keep_their_own_network_and_evidence(self):
        checks = {"trace_projection_missing": [{"net": "N1", "code": "TRACE_PROJECTION_MISSING"}],
                  "guard_band_missing": [{"net": "N2", "code": "REFERENCE_NECK_BELOW_LIMIT"}],
                  "reference_evidence": [{"net": "GND", "evidence_available": True}]}
        stage = {"stage": "return_path", "passed": False, "error": "回流复核未通过"}
        diag = failure_summary({"attempts": [{"stages": [stage], "return_path_report": checks}]})
        self.assertEqual(diag["nets"], ["N1", "N2"])
        self.assertEqual(diag["network_details"], [{"net": "N1", "code": "TRACE_PROJECTION_MISSING"},
                                                   {"net": "N2", "code": "REFERENCE_NECK_BELOW_LIMIT"}])

    def test_diagnostic_size_is_bounded_and_input_evidence_is_unchanged(self):
        all_nets = ["N" + str(i) for i in range(25)]
        stage = {"stage": "routing", "passed": False, "error": "预算耗尽",
                 "diagnostics": {"code": "NEGOTIATED_BUDGET", "unfinished_nets": all_nets,
                                 "errors": ["错误" * 300] * 4, "stats": {"net_order": ["已经通过"]}}}
        diag = failure_summary({"attempts": [{"stages": [stage]}]})
        self.assertNotIn("net_count", diag)
        self.assertEqual(diag["unfinished_count"], 25)
        self.assertEqual(diag["unfinished_nets"], all_nets[:8])
        self.assertEqual(diag["unfinished_nets_omitted"], 17)
        self.assertEqual(diag["detail_count"], 4)
        self.assertEqual(len(diag["details"]), 2)
        self.assertTrue(diag["details"][0].endswith("…"))
        self.assertEqual(len(stage["diagnostics"]["unfinished_nets"]), 25)


if __name__ == "__main__":
    unittest.main()
