"""新算法入口、旧默认与白名单写回计划的独立回归反例。"""
from copy import deepcopy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pcb_pipeline_common import PipelineError, check_board
from pcb_pipeline import build_writeback_plan, run_pipeline
from pcb_routing import algorithm_options, route_board
from pcb_dense_preflight import inspect_channels
from test_pcb_shove import fixture, attempt
from test_pcb_dense_geometry import alternating_gates_fixture, alternating_gates_options


class DenseIntegrationTests(unittest.TestCase):
    def test_legacy_defaults_and_unknown_nested_fields(self):
        options = algorithm_options({})
        self.assertEqual(options["method"], "sequential")
        self.assertFalse(options["occupancy_grid"])
        for key in ("geometry_search", "fanout", "shove", "diff_layer_change"):
            self.assertFalse(options.get(key, {}).get("enabled", False))
            with self.assertRaises(PipelineError):
                algorithm_options({key: {"unexpected": True}})

    def test_pipeline_shove_keeps_input_and_explicit_remove_plan(self):
        board = fixture(True)
        original = deepcopy(board)
        report = run_pipeline({"schema_version": 1, "board": board, "options": {
            "placement": {"enabled": False}, "max_iterations": 1,
            "routing": {"width": 2, "trace_layers": [1], "allow_vias": False, "net_order": ["A", "B", "C"],
                        "max_passes": 1, "max_seconds": 10, "max_net_seconds": 10,
                        "occupancy_grid": True, "shove": {"enabled": True,
                        "movable_trace_ids": ["B-line", "C-line"]}}}})
        self.assertTrue(report["passed"], report.get("violations"))
        self.assertIsNot(report.get("full_board_accepted"), True)
        self.assertEqual(set(report["writeback_plan"]["remove"]["traces"]), {"B-line", "C-line"})
        self.assertEqual(report["writeback_plan"]["source"]["usage"], "test_only")
        self.assertEqual(check_board(report["board"]), [])
        self.assertEqual(board, original)

    def test_old_writeback_still_rejects_replacement_without_whitelist(self):
        board = fixture()
        result, _ = attempt(board)
        with self.assertRaises(PipelineError):
            build_writeback_plan(board, result)
        with self.assertRaises(PipelineError):
            build_writeback_plan(board, result, allowed_trace_replacements=[])

    def test_replacement_cannot_change_source_netlist_pad_or_rules(self):
        board = fixture()
        result, _ = attempt(board)
        mutations = [lambda b: b["nets"]["B"].pop(),
                     lambda b: b["rules"].update(default_clearance=0),
                     lambda b: b["components"][0]["pads"][0].update(localW=50),
                     lambda b: b["metadata"]["source"].update(fingerprint="different")]
        for mutate in mutations:
            changed = deepcopy(result)
            mutate(changed)
            with self.assertRaises(PipelineError):
                build_writeback_plan(board, changed, allowed_trace_replacements=["B-line"])

    def test_same_width_stubs_do_not_hide_neckdown(self):
        board = fixture()
        board["rules"]["minimum_trace_width"] = 1
        result = deepcopy(board)
        result["routes"]["traces"] = [dict(net="B", layer=1, sx=a, sy=50, ex=b, ey=50, width=w)
                                             for a,b,w in ((10,20,2),(20,100,1),(100,110,2))]
        with self.assertRaisesRegex(PipelineError, "同宽路径"):
            build_writeback_plan(board, result, allowed_trace_replacements=["B-line"])

    def test_fine_grid_fallback_after_geometry_budget(self):
        options = alternating_gates_options(True)
        options["geometry_search"].update(max_edges=1, refine_factors=[2,4], max_refine_expansions=20000)
        routed, stats = route_board(alternating_gates_fixture(), options)
        self.assertEqual(check_board(routed), [])
        self.assertGreater(stats.get("geometry_budget_fallbacks", 0), 0)
        self.assertTrue(any(row["found"] for row in stats["geometry_refinements"]))

    def test_preflight_does_not_claim_global_infeasibility(self):
        board = fixture()
        report = inspect_channels(board, "A", {"width": 2}, max_pairs=1)
        self.assertEqual(report["whole_board_feasibility"], "not_proven")
        self.assertFalse(report["full_board_accepted"])

    def test_negotiated_shove_unsupported_combination_is_explicit(self):
        with self.assertRaisesRegex(PipelineError, "仅支持 sequential"):
            algorithm_options({"method": "negotiated", "shove": {"enabled": True}})


if __name__ == "__main__":
    unittest.main()
