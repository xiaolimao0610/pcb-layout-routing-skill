"""几何级联推挤的合成反例；不读取或写入真实 PCB。"""
from copy import deepcopy
import math
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pcb_pipeline_common import PipelineError, check_board, normalize_board
from pcb_routing import _groups, _net_options, check_routed_nets, RoutingFailure, route_board
from pcb_shove import shove_route, validate_options


def fixture(cascade=False):
    """返回带固定焊盘、两端完整原线及一个缺口的合成板。"""
    board = dict(units="mil", board_bbox=[0, 0, 120, 90], components=[], nets={}, tags={},
                 routes=dict(traces=[], vias=[], shield_vias=[], copper_pours=[], holes=[], keepouts=[]),
                 rules=dict(copper_layers=[1, 2], minimum_trace_width=2, default_clearance=2,
                            clearance_matrix=[], board_edge_clearance=1, component_courtyard_margin=0),
                 metadata=dict(source=dict(document_uuid="synthetic", fingerprint="synthetic-fingerprint", usage="test_only")))
    def pad(identity, net, x, y):
        board["components"].append(dict(id=identity, x=x, y=y, rotation=0, layer=1, width=2, height=2,
            pads=[dict(padId="1", net=net, localX=0, localY=0, localW=2, localH=2, shape="RECTANGLE")]))
        board["nets"].setdefault(net, []).append(dict(compId=identity, padId="1"))
    pad("A1", "A", 40, 40)
    pad("A2", "A", 80, 54.5)
    for name, y in (("B", 50), ("C", 60)) if cascade else (("B", 50),):
        pad(name + "1", name, 10, y)
        pad(name + "2", name, 110, y)
        board["routes"]["traces"].append(dict(id=name + "-line", net=name, layer=1,
                                             sx=10, sy=y, ex=110, ey=y, width=2, locked=False))
    return normalize_board(board)


def attempt(board, **overrides):
    settings = dict(width=2, trace_layers=[1], allow_vias=False)
    config = dict(enabled=True, movable_trace_ids=[t["id"] for t in board["routes"]["traces"] if t.get("id")],
                  max_seconds=10, max_candidates=256)
    config.update(overrides)
    options = dict(settings, shove=config)
    stats = {}
    result = shove_route(board, "A", _net_options(board, "A", options), options, stats, None)
    return result, stats


class ShoveTests(unittest.TestCase):
    def generated_attempt(self, board, baseline=None, *, protected=(), **overrides):
        """以主入口传入的本轮基线区分匿名原铜和生成铜。"""
        config = dict(enabled=True, movable_trace_ids=[], max_seconds=10, max_candidates=256)
        config.update(overrides)
        options = dict(width=2, trace_layers=[1], allow_vias=False, shove=config,
                       _shove_protected_nets=list(protected))
        if baseline is not None:
            options["_shove_original_traces"] = baseline
        stats = {}
        result = shove_route(board, "A", _net_options(board, "A", options), options, stats, None)
        return result, stats

    def test_single_trace_keeps_endpoints_and_width(self):
        board = fixture()
        before = deepcopy(board)
        self.assertEqual(check_board(board, ignore_connectivity=True), [])
        result, stats = attempt(board)
        self.assertIsNotNone(result, stats)
        self.assertEqual(board, before)
        self.assertEqual(check_routed_nets(result, ["A", "B"]), [])
        replacement = stats["shove"]["replacements"][0]
        self.assertEqual(replacement["expected"], board["routes"]["traces"][0])
        self.assertGreater(len(replacement["replacements"]), 1)
        self.assertTrue(all(t["width"] == 2 and t["net"] == "B" and t["layer"] == 1
                            and "id" not in t for t in replacement["replacements"]))

    def test_cascade_moves_two_foreign_nets(self):
        board = fixture(True)
        before = deepcopy(board)
        result, stats = attempt(board)
        self.assertIsNotNone(result, stats)
        self.assertEqual(board, before)
        self.assertEqual(check_routed_nets(result, ["A", "B", "C"]), [])
        self.assertEqual({p["trace_id"] for p in stats["shove"]["replacements"]}, {"B-line", "C-line"})
        self.assertGreaterEqual(stats["shove"]["attempts"][0]["depth_peak"], 2)

    def test_disabled_is_noop(self):
        board = fixture()
        result, stats = attempt(board, enabled=False)
        self.assertIsNone(result)
        self.assertEqual(stats, {})

    def test_locked_and_explicit_protected_reject(self):
        for locked, protected in ((True, []), (False, ["B-line"])):
            board = fixture()
            board["routes"]["traces"][0]["locked"] = locked
            before = deepcopy(board)
            result, stats = attempt(board, protected_trace_ids=protected)
            self.assertIsNone(result)
            self.assertEqual(board, before)
            self.assertEqual(stats["shove"]["attempts"][0]["rejected_objects"]["B-line"], "locked_or_protected")

    def test_candidate_budget_exhaustion_does_not_modify_input(self):
        board = fixture(True)
        before = deepcopy(board)
        result, stats = attempt(board, max_candidates=1)
        self.assertIsNone(result)
        self.assertEqual(board, before)
        self.assertEqual(stats["shove"]["attempts"][0]["status"], "budget_exhausted")
        self.assertEqual(stats["shove"]["replacements"], [])

    def test_invalid_options_fail_closed(self):
        for config in ({"enable": True}, {"enabled": 1}, {"max_depth": True},
                       {"max_seconds": float("inf")}, {"max_displacement": 0},
                       {"movable_trace_ids": ["B-line", "B-line"]}):
            with self.assertRaises(PipelineError):
                validate_options(config)
        with self.assertRaises(PipelineError):
            attempt(fixture(), movable_trace_ids=["missing"])

    def test_empty_whitelist_cannot_move_existing_copper(self):
        board = fixture()
        before = deepcopy(board)
        result, stats = attempt(board, movable_trace_ids=[])
        self.assertIsNone(result)
        self.assertEqual(board, before)
        self.assertEqual(stats["shove"]["replacements"], [])

    def test_depth_and_object_limits_rollback_real_cascade(self):
        board = fixture(True)
        board["routes"]["keepouts"].append(dict(points=[[12, 1], [108, 1], [108, 46], [12, 46]],
                                                  layers=[1], nets=["B"], clearance=0))
        before = deepcopy(board)
        for option in ("max_depth", "max_objects"):
            result, stats = attempt(board, **{option: 1})
            self.assertIsNone(result, stats)
            self.assertIn(option, stats["shove"]["attempts"][0]["limits_hit"])
            self.assertEqual(stats["shove"]["replacements"], [])
            self.assertEqual(board, before)
        accepted, stats = attempt(board)
        self.assertIsNotNone(accepted, stats)
        self.assertEqual(check_routed_nets(accepted, ["A", "B", "C"]), [])

    def test_interior_branch_is_preserved_and_rejected(self):
        board = fixture()
        board["components"].append(dict(id="B3", x=60, y=75, rotation=0, layer=1, width=2, height=2,
            pads=[dict(padId="1", net="B", localX=0, localY=0, localW=2, localH=2, shape="RECTANGLE")]))
        board["nets"]["B"].append(dict(compId="B3", padId="1"))
        board["routes"]["traces"].append(dict(id="branch", net="B", layer=1, sx=60, sy=50, ex=60, ey=75, width=2))
        board = normalize_board(board)
        before = deepcopy(board)
        self.assertEqual(len(_groups(board, "B")), 1)
        result, stats = attempt(board, movable_trace_ids=["B-line"])
        self.assertIsNone(result)
        self.assertEqual(board, before)
        self.assertEqual(stats["shove"]["attempts"][0]["rejected_objects"]["B-line"], "interior_copper_contact")

    def test_existing_via_and_all_layers_are_preserved(self):
        board = fixture()
        board["routes"]["vias"].append(dict(id="original-via", net="B", x=60, y=50,
                                               hole=2, diameter=6, layers=[1, 2]))
        before = deepcopy(board)
        result, stats = attempt(board)
        self.assertIsNone(result)
        self.assertEqual(board, before)
        self.assertIn("B-line", stats["shove"]["attempts"][0]["rejected_objects"])

    def test_derived_copper_is_never_dropped(self):
        board = fixture()
        board["routes"]["copper_pours"].append(dict(id="derived", net="B", layer=1, source_kind="Teardrop",
            filled_polygons=[dict(points=[[49, 49.5], [51, 49.5], [51, 50.5], [49, 50.5]])],
            metadata=dict(refs=["B-line"])))
        before = deepcopy(board)
        result, stats = attempt(board)
        self.assertIsNone(result)
        self.assertEqual(board, before)
        self.assertEqual(stats["shove"]["attempts"][0]["rejected_objects"]["B-line"], "derived_copper_parent")

    def test_same_net_pour_attachment_is_protected(self):
        board = fixture()
        board["routes"]["copper_pours"].append(dict(id="plane", net="B", layer=1,
            filled_polygons=[dict(points=[[8, 49], [15, 49], [15, 51], [8, 51]])]))
        before = deepcopy(board)
        result, stats = attempt(board)
        self.assertIsNone(result)
        self.assertEqual(board, before)
        self.assertEqual(stats["shove"]["attempts"][0]["rejected_objects"]["B-line"], "attached_pour")

    def test_topology_and_length_constraints_protect_net(self):
        for rule in (dict(operator="TOPOLOGY_ORDER", net="B", ordered_pins=[dict(compId="B1", padId="1"), dict(compId="B2", padId="1")]),
                     dict(operator="CHANNEL_SPEC", net="B", length_group=dict(name="matched", tolerance=1))):
            board = fixture()
            board["metadata"]["pipeline_constraints"] = [rule]
            before = deepcopy(board)
            result, stats = attempt(board)
            self.assertIsNone(result)
            self.assertEqual(board, before)
            self.assertEqual(stats["shove"]["attempts"][0]["rejected_objects"]["B-line"], "protected_net")

    def test_initial_bridge_is_not_hidden(self):
        board = fixture()
        board["routes"]["traces"].append(dict(net="A", layer=1, sx=15, sy=50, ex=90, ey=50, width=2))
        before = deepcopy(board)
        with self.assertRaisesRegex(PipelineError, "原板硬检查"):
            attempt(board)
        self.assertEqual(board, before)

    def test_time_budget_and_parent_budget_preserve_input(self):
        board = fixture()
        before = deepcopy(board)
        with patch("pcb_shove.time.monotonic", side_effect=[0, 1, 1]):
            result, stats = attempt(board, max_seconds=0.5)
        self.assertIsNone(result)
        self.assertEqual(stats["shove"]["attempts"][0]["stop_reason"], "max_seconds")
        class Parent:
            def check(self):
                raise RoutingFailure("TIME_BUDGET", "父预算耗尽")
        options = dict(width=2, trace_layers=[1], shove=dict(enabled=True, movable_trace_ids=["B-line"]))
        with self.assertRaises(RoutingFailure):
            shove_route(board, "A", _net_options(board, "A", options), options, {}, Parent())
        self.assertEqual(board, before)

    def test_already_connected_is_not_counted_as_success(self):
        board, _ = attempt(fixture())
        before = deepcopy(board)
        result, stats = attempt(board, movable_trace_ids=[])
        self.assertIsNone(result)
        self.assertEqual(stats["shove"]["attempts"][0]["status"], "already_connected")
        self.assertEqual(stats["shove"]["replacements"], [])
        self.assertEqual(board, before)

    def test_displacement_limit_reports_not_found(self):
        board = fixture()
        before = deepcopy(board)
        result, stats = attempt(board, max_displacement=1)
        self.assertIsNone(result)
        self.assertEqual(stats["shove"]["attempts"][0]["status"], "not_found")
        self.assertEqual(board, before)

    def test_rotated_cascade_uses_geometry_not_fixed_grid(self):
        for degrees in (45, 90):
            board = fixture(True)
            board["board_bbox"] = [-50, -50, 170, 150]
            angle = math.radians(degrees)
            def rotated(x, y):
                x, y = x - 60, y - 45
                return (60 + x * math.cos(angle) - y * math.sin(angle),
                        45 + x * math.sin(angle) + y * math.cos(angle))
            for component in board["components"]:
                component["x"], component["y"] = rotated(component["x"], component["y"])
                component["rotation"] = degrees
            for trace in board["routes"]["traces"]:
                trace["sx"], trace["sy"] = rotated(trace["sx"], trace["sy"])
                trace["ex"], trace["ey"] = rotated(trace["ex"], trace["ey"])
            board = normalize_board(board)
            before = deepcopy(board)
            result, stats = attempt(board)
            self.assertIsNotNone(result, stats)
            self.assertEqual(check_routed_nets(result, ["A", "B", "C"]), [])
            self.assertEqual(board, before)

    def test_new_anonymous_trace_can_move_with_original_inventory(self):
        board = fixture()
        board["routes"]["traces"][0].pop("id")
        before = deepcopy(board)
        candidate, stats = self.generated_attempt(board, [])
        self.assertIsNotNone(candidate, stats)
        self.assertEqual(board, before)
        self.assertEqual(check_routed_nets(candidate, ["A", "B"]), [])
        self.assertTrue(all("id" not in trace for trace in candidate["routes"]["traces"]))
        evidence = stats["shove"]["replacements"][0]
        self.assertEqual(evidence["scope"], "candidate_generated")
        self.assertIsNone(evidence["trace_id"])
        self.assertNotIn("id", evidence["expected"])
        self.assertEqual(evidence["candidate_trace_index"], 0)
        self.assertNotIn("@shove-generated:", str(stats))

    def test_original_anonymous_trace_and_missing_inventory_are_protected(self):
        board = fixture()
        board["routes"]["traces"][0].pop("id")
        before = deepcopy(board)
        for baseline in (None, deepcopy(board["routes"]["traces"])):
            candidate, stats = self.generated_attempt(board, baseline)
            self.assertIsNone(candidate)
            self.assertEqual(board, before)
            self.assertEqual(stats["shove"]["replacements"], [])

    def test_anonymous_original_prefix_is_preserved_while_new_trace_moves(self):
        board = fixture(True)
        for trace in board["routes"]["traces"]:
            trace.pop("id")
        board["routes"]["traces"].reverse()
        original_trace = deepcopy(board["routes"]["traces"][0])
        before = deepcopy(board)
        candidate, stats = self.generated_attempt(board, [original_trace])
        self.assertIsNotNone(candidate, stats)
        self.assertEqual(candidate["routes"]["traces"][0], original_trace)
        self.assertEqual(check_routed_nets(candidate, ["A", "B", "C"]), [])
        self.assertEqual(board, before)
        self.assertEqual({item["expected"]["net"] for item in stats["shove"]["replacements"]}, {"B"})

    def test_changed_anonymous_original_cannot_be_reclassified_as_new(self):
        board = fixture()
        board["routes"]["traces"][0].pop("id")
        baseline = deepcopy(board["routes"]["traces"])
        baseline[0]["width"] = 3
        before = deepcopy(board)
        with self.assertRaisesRegex(PipelineError, "原有无 ID 铜丢失或改变"):
            self.generated_attempt(board, baseline)
        self.assertEqual(board, before)

    def test_generated_trace_switch_and_protected_net_are_enforced(self):
        board = fixture()
        board["routes"]["traces"][0].pop("id")
        before = deepcopy(board)
        candidate, _ = self.generated_attempt(board, [], move_generated_traces=False)
        self.assertIsNone(candidate)
        candidate, stats = self.generated_attempt(board, [], protected=["B"])
        self.assertIsNone(candidate)
        self.assertIn("protected_net", stats["shove"]["attempts"][0]["rejected_objects"].values())
        self.assertEqual(board, before)
        with self.assertRaises(PipelineError):
            validate_options(dict(move_generated_traces="true"))
        with self.assertRaisesRegex(PipelineError, "未知原线"):
            self.generated_attempt(board, [], movable_trace_ids=["@shove-generated:0"])

    def test_route_board_later_net_pushes_earlier_generated_trace(self):
        from pcb_pipeline import build_writeback_plan
        board = fixture()
        board["routes"]["traces"] = []
        before = deepcopy(board)
        options = dict(width=2, trace_layers=[1], allow_vias=False, net_order=["B", "A"],
                       max_seconds=20, max_net_seconds=10, max_passes=1,
                       shove=dict(enabled=True, movable_trace_ids=[]))
        candidate, stats = route_board(board, options)
        self.assertEqual(check_routed_nets(candidate, ["A", "B"]), [])
        self.assertEqual(board, before)
        self.assertEqual(stats["shove"]["replacements"][0]["scope"], "candidate_generated")
        plan = build_writeback_plan(board, candidate)
        self.assertEqual(plan["remove"]["traces"], [])
        self.assertTrue(all("id" not in trace for trace in plan["add"]["traces"]))

    def test_temporary_identity_does_not_collide_with_native_identity(self):
        board = fixture(True)
        board["routes"]["traces"][0].pop("id")
        board["routes"]["traces"][1].update(id="@shove-generated:0", locked=True)
        native = deepcopy(board["routes"]["traces"][1])
        board["routes"]["traces"].reverse()
        before = deepcopy(board)
        candidate, stats = self.generated_attempt(board, [native])
        self.assertIsNotNone(candidate, stats)
        self.assertEqual([trace for trace in candidate["routes"]["traces"] if "id" in trace], [native])
        self.assertEqual(board, before)
        self.assertTrue(all(item["trace_id"] is None for item in stats["shove"]["replacements"]))

    def test_generated_attempt_budget_never_leaks_temporary_identity(self):
        board = fixture()
        board["routes"]["traces"][0].pop("id")
        before = deepcopy(board)
        candidate, stats = self.generated_attempt(board, [], max_candidates=1)
        self.assertIsNone(candidate)
        self.assertEqual(board, before)
        self.assertEqual(stats["shove"]["replacements"], [])
        self.assertNotIn("@shove-generated:", str(stats))


if __name__ == "__main__":
    unittest.main()
