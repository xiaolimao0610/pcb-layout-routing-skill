"""选网续算与原理图比对的终端摘要、完整文件、退出状态及兼容输出。"""
from contextlib import redirect_stdout
from copy import deepcopy
from io import StringIO
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import pcb_continue
import schematic_netlist
from pcb_pipeline_common import normalize_board
from pcb_routing import RoutingFailure
from test_initial_workflow import task_fixture
from test_schematic_netlist import fixture as schematic_fixture


def call(fn, args):
    stream = StringIO()
    with redirect_stdout(stream):
        code = fn(args)
    return code, stream.getvalue()


class CompactEntryTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)

    def continue_args(self, output):
        board = task_fixture()["board"]
        for index, component in enumerate(board["components"]):
            component.update(x=50 + 100*index, y=100)
        board = normalize_board(board)
        board["routes"].update(traces=[], vias=[])
        path = self.root / "board.json"
        path.write_text(json.dumps(board), encoding="utf-8")
        return ["--board", str(path), "--calculation-only", "--output-dir", str(self.root / output)]

    def test_continue_success_keeps_progress_and_legacy_event_stream(self):
        code, out = call(pcb_continue.main, self.continue_args("current"))
        brief = json.loads(out)
        self.assertEqual((code, brief["connected"], brief["selected"]), (0, 1, 1))
        self.assertFalse(brief["full_board_accepted"])
        self.assertFalse(brief["writeback_plan_generated"])
        self.assertNotIn("network_diagnostics", brief)
        log = Path(brief["progress"]).read_text(encoding="utf-8").splitlines()
        self.assertIn("connection", [json.loads(line)["event"] for line in log])
        self.assertIn("stats", json.loads(Path(brief["report"]).read_text(encoding="utf-8")))
        code, old = call(pcb_continue.main, self.continue_args("verbose") + ["--full-output"])
        self.assertEqual(code, 0)
        self.assertEqual(old.splitlines(), (self.root/"verbose/progress.jsonl").read_text(encoding="utf-8").splitlines())

    def test_continue_failure_names_network_and_keeps_full_diagnostics(self):
        detail = {"code": "GRID_NO_PATH", "net": "N", "source_terminals": [f"U{i}" for i in range(200)]}
        failure = RoutingFailure("GRID_NO_PATH", "未找到该网的路径", **{k:v for k,v in detail.items() if k != "code"})
        with patch("pcb_routing._route_net", side_effect=failure):
            rc, out = call(pcb_continue.main, self.continue_args("failure"))
        brief = json.loads(out)
        self.assertEqual(rc, 1)
        self.assertEqual(brief["network_diagnostics"][0]["net"], "N")
        self.assertEqual(brief["network_diagnostics"][0]["code"], "GRID_NO_PATH")
        self.assertNotIn("source_terminals", out)
        report = json.loads(Path(brief["report"]).read_text(encoding="utf-8"))
        self.assertEqual(report["failed_nets"]["N"]["diagnostics"]["source_terminals"], detail["source_terminals"])

    def test_continue_cli_input_failure_saves_traceback_in_its_new_directory(self):
        args = self.continue_args("invalid")
        (self.root / "board.json").write_text('{"', encoding="utf-8")
        run = subprocess.run([sys.executable, "-X", "utf8", "-B", pcb_continue.__file__, *args],
                             capture_output=True, encoding="utf-8")
        self.assertEqual(run.returncode, 1)
        summary = json.loads(run.stderr)
        self.assertEqual(summary["status"], "failed")
        self.assertFalse(summary["full_board_accepted"])
        self.assertIn("JSONDecodeError", Path(summary["log"]).read_text(encoding="utf-8"))
        self.assertFalse((self.root/"invalid/writeback-plan.json").exists())

    def test_negotiated_batch_failure_only_attributes_declared_diagnostic_net(self):
        nets = ["A", "B", "C", "D", "E", "F", "G"]
        report = dict(connectivity={n: dict(passed=False, physical_groups=2) for n in nets},
                      failed_nets={n: {"error": "G 的搜索预算耗尽", "diagnostics":
                                       {"net": "G", "code": "TIME_BUDGET"}} for n in nets},
                      preview_only=True, full_board_accepted=False, selected_connectivity_passed=False,
                      writeback_plan_generated=False, requires_live_rebase=True, added={}, not_attempted=[],
                      deferred_ground_nets=[], topology_errors=[], final_geometry_errors=[], stats={"method": "negotiated"})
        summary = pcb_continue._summary(report, self.root)
        self.assertEqual(summary["additional_networks"], 2)
        self.assertEqual([r["net"] for r in summary["network_diagnostics"] if "code" in r], ["G"])
        self.assertIn("batch_error", summary)

    def test_netlist_differences_are_bounded_and_legacy_json_stays_available(self):
        before = schematic_fixture()
        after = deepcopy(before)
        after["components"]["id1"]["pinInfoMap"]["1"]["net"] = "BROKEN"
        for i in range(140):
            after["display_" + str(i)] = list(range(100))
        files = [self.root/"before.enet", self.root/"after.enet"]
        for p, data in zip(files, (before, after)):
            p.write_text(json.dumps(data), encoding="utf-8")
        report = self.root/"net-diff.json"
        args = [str(p) for p in files]
        rc, out = call(schematic_netlist.main, args + ["--report", str(report)])
        self.assertEqual(rc, 1)
        summary = json.loads(out)
        full = json.loads(report.read_text(encoding="utf-8"))
        self.assertEqual(summary["difference_count"], full["difference_count"])
        self.assertEqual(len(summary["differences"]), 5)
        self.assertEqual(summary["additional_differences"], full["difference_count"] - 5)
        self.assertEqual({e["key"] for e in summary["differences"] if e["scope"] == "net"}, {"SENSE", "BROKEN"})
        self.assertGreater(len(full["differences"]), len(summary["differences"]))
        for variant in (args, args + ["--report", str(report), "--full-output"]):
            rc, text = call(schematic_netlist.main, variant)
            self.assertEqual(rc, 1)
            self.assertEqual(json.loads(text), full)
