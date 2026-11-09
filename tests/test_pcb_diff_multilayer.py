"""成对换层的实际几何正反例：不连接原生客户端，不采用生成元数据作证据。"""
from copy import deepcopy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pcb_pipeline_common import PipelineError, normalize_board, check_board
from pcb_routing import _Budget, _net_options, RoutingFailure
from pcb_differential import quality_report, route_pairs, validate_layer_change_options
from pcb_diff_multilayer import route_pair, pair_metrics, measured_multilayer_path


PAIR = dict(p="P", n="N", width=10, gap=10)
CONSTRAINTS = [dict(operator="CHANNEL_SPEC", net="P", diff_pair=PAIR)]


def fixture(end_layer=2):
    """返回四层双端差分测试板，焊盘/孔/网表均为独立实体几何。"""
    components, nets = [], {"P": [], "N": []}
    for end, (x, layer) in enumerate(((0, 1), (1000, end_layer))):
        for net, y in (("P", 10), ("N", -10)):
            name = f"{net}{end}"
            components.append(dict(id=name, x=x, y=y, rotation=0, layer=layer,
                body_polygon=[[-4,-4], [4,-4], [4,4], [-4,4]],
                pads=[dict(padId="1", net=net, localX=0, localY=0, layer=layer,
                           shape="RECTANGLE", localW=8, localH=8)]))
            nets[net].append(dict(compId=name, padId="1"))
    return normalize_board(dict(units="mil", board_bbox=[-100,-200,1100,200],
        components=components, nets=nets, tags={}, routes={}, metadata={"board_usage":"test_only"},
        rules=dict(copper_layers=[1,15,16,2], board_edge_clearance=5,
                   component_courtyard_margin=0, clearance_matrix=[])))


def options(**diff):
    return dict(width=10, trace_layers=[1,15,2], max_seconds=30, max_net_seconds=20,
                diff_max_candidates=8, grid_step=10, diff_layer_change=dict(enabled=True, **diff))


def generate(board, **diff):
    opt = options(**diff)
    settings = {net: _net_options(board, net, opt) for net in ("P","N")}
    stats = {}
    result = route_pair(board, PAIR, settings, opt, stats, _Budget(opt))
    return result, stats


class PairedThroughViaTests(unittest.TestCase):
    def test_one_transition_actual_copper_and_independent_metrics(self):
        board = fixture()
        original = deepcopy(board)
        result, stats = generate(board, max_transitions=1)
        self.assertIsNotNone(result)
        routed, metrics, _ = result
        self.assertEqual(board, original)
        self.assertEqual(check_board(routed), [])
        self.assertTrue(quality_report(routed, CONSTRAINTS)["passed"])
        self.assertEqual(metrics["layers"], [1,2])
        self.assertEqual(len(metrics["paired_vias"]), 1)
        self.assertFalse(metrics["length_complete"])
        self.assertIsNone(metrics["p"]["total_length_mil"])
        self.assertFalse(metrics["return_path_verified"])
        self.assertEqual(stats["diff_layer_change"][0]["candidates_tested"], 1)
        routed["metadata"] = {"untrusted_generated_evidence": "全部通过"}
        self.assertTrue(quality_report(routed, CONSTRAINTS)["passed"])

    def test_two_transitions_bypass_layer_obstacle(self):
        board = fixture(1)
        board["routes"]["keepouts"] = [dict(points=[[400,-190],[600,-190],[600,190],[400,190]], layers=[1])]
        result, _ = generate(board)
        self.assertIsNotNone(result)
        routed, metrics, _ = result
        self.assertEqual(metrics["layers"], [1,15,1])
        self.assertEqual(len(routed["routes"]["vias"]), 4)
        self.assertEqual(check_board(routed), [])
        self.assertTrue(quality_report(routed, CONSTRAINTS)["passed"])

    def test_route_pairs_uses_opt_in_fallback_and_default_remains_same_layer(self):
        board = fixture()
        opt = options()
        candidate, handled = route_pairs(board, CONSTRAINTS, opt, {}, _Budget(opt), {})
        self.assertEqual(handled, {"P","N"})
        self.assertTrue(quality_report(candidate, CONSTRAINTS)["passed"])
        opt.pop("diff_layer_change")
        with self.assertRaisesRegex(PipelineError, "DIFF_PAIR_UNSUPPORTED"):
            route_pairs(board, CONSTRAINTS, opt, {}, _Budget(opt), {})

    def test_missing_via_and_disconnected_line_rejected(self):
        result, _ = generate(fixture())
        for kind in ("vias", "traces"):
            broken = deepcopy(result[0])
            broken["routes"][kind].pop()
            self.assertFalse(quality_report(broken, CONSTRAINTS)["passed"])

    def test_edge_touch_is_not_terminal_connection(self):
        result, _ = generate(fixture())
        broken = result[0]
        for trace in broken["routes"]["traces"]:
            if trace["net"] == "P" and trace["ex"] == 1000:
                trace["ex"] = 991
        self.assertFalse(quality_report(broken, CONSTRAINTS)["passed"])
        self.assertTrue(check_board(broken))

    def test_actual_gap_and_width_must_match_declaration(self):
        result, _ = generate(fixture())
        for pair in (dict(PAIR, gap=8), dict(PAIR, width=12)):
            constraints = [dict(operator="CHANNEL_SPEC", net="P", diff_pair=pair)]
            self.assertFalse(quality_report(result[0], constraints)["passed"])

    def test_full_pipeline_checks_new_candidate(self):
        from pcb_pipeline import run_pipeline
        task = dict(schema_version=1, board=fixture(), constraints=CONSTRAINTS,
                    options=dict(placement=dict(enabled=False), routing=options()))
        result = run_pipeline(task)
        self.assertEqual(result["status"], "offline_passed")
        self.assertTrue(result["passed"])
        self.assertEqual(check_board(result["board"]), [])
        task["options"]["routing"].pop("diff_layer_change")
        legacy = run_pipeline(task)
        self.assertFalse(legacy["passed"])

    def test_independently_routed_far_vias_are_not_a_pair(self):
        result, _ = generate(fixture())
        board = result[0]
        for via in board["routes"]["vias"]:
            if via["net"] == "N":
                via["x"] = 300
        for trace in board["routes"]["traces"]:
            if trace["net"] == "N":
                for key in ("sx","ex"):
                    if trace[key] == 120:
                        trace[key] = 300
        self.assertEqual(check_board(board), [])
        with self.assertRaisesRegex(PipelineError, "成对换层"):
            pair_metrics(board, PAIR)

    def test_branch_and_foreign_bridge_rejected(self):
        result, _ = generate(fixture())
        branched = deepcopy(result[0])
        branched["routes"]["traces"].append(dict(net="P", layer=2, width=10,
                                                sx=500, sy=10, ex=500, ey=70))
        self.assertFalse(quality_report(branched, CONSTRAINTS)["passed"])
        bridged = deepcopy(result[0])
        bridged["routes"]["traces"].append(dict(net="P", layer=2, width=10,
                                               sx=500, sy=10, ex=500, ey=-10))
        self.assertTrue(check_board(bridged))

    def test_same_layer_separate_edges_cannot_shortcut_inner_layer(self):
        board = fixture(1)
        board["components"] = [part for part in board["components"] if part["pads"][0]["net"] == "P"]
        board["components"][0].update(x=0, y=0)
        board["components"][1].update(x=80, y=20)
        board["nets"] = {"P": board["nets"]["P"]}
        board["board_bbox"] = [-100,-100,200,200]
        segments = [(1, (0,0), (100,0)), (1, (100,0), (100,100)),
                    (15, (100,100), (20,20)), (1, (20,20), (20,4)),
                    (1, (20,4), (80,4)), (1, (80,4), (80,20))]
        board["routes"] = dict(traces=[dict(net="P", layer=layer, width=10,
            sx=a[0], sy=a[1], ex=b[0], ey=b[1]) for layer,a,b in segments],
            vias=[dict(net="P", x=x, y=y, hole=12, diameter=24, layers=[1,15,16,2])
                  for x,y in ((100,100),(20,20))])
        self.assertEqual(check_board(board), [])
        with self.assertRaisesRegex(PipelineError, "同层不同换层分段"):
            measured_multilayer_path(board, "P")

    def test_wrong_span_and_single_layer_via_rejected(self):
        result, _ = generate(fixture())
        board = result[0]
        board["routes"]["vias"][0]["layers"] = [1,15]
        with self.assertRaisesRegex(PipelineError, "完整叠层"):
            measured_multilayer_path(board, "P")

    def test_unknown_barrel_length_cannot_pass_length_group(self):
        result, _ = generate(fixture())
        constraints = CONSTRAINTS + [dict(operator="CHANNEL_SPEC", net=net,
            length_group=dict(name="pair", tolerance=1)) for net in ("P","N")]
        report = quality_report(result[0], constraints)
        self.assertFalse(report["passed"])
        self.assertFalse(report["length_groups"][0]["measured"])

    def test_existing_copper_never_overwritten(self):
        result, _ = generate(fixture())
        original = deepcopy(result[0])
        with self.assertRaisesRegex(PipelineError, "EXISTING"):
            generate(result[0])
        self.assertEqual(result[0], original)

    def test_strict_options_and_explicit_budgets(self):
        self.assertFalse(validate_layer_change_options()["enabled"])
        for bad in ({"typo": True}, {"enabled": 1}, {"max_candidates": 0},
                    {"max_transitions": 3}, {"max_seconds": float("nan")}):
            with self.assertRaises(PipelineError):
                validate_layer_change_options(bad)
        board = fixture()
        original = deepcopy(board)
        with self.assertRaisesRegex(RoutingFailure, "TIME_BUDGET"):
            generate(board, max_seconds=1e-12)
        self.assertEqual(board, original)
        board["routes"]["keepouts"] = [dict(points=[[-90,-190],[1090,-190],[1090,190],[-90,190]], layers=[1,15,16,2])]
        original = deepcopy(board)
        result, stats = generate(board, max_candidates=1)
        self.assertIsNone(result)
        self.assertEqual(stats["diff_layer_change"][0]["candidates_tested"], 1)
        self.assertEqual(stats["diff_layer_change"][0]["stop_reason"], "candidate_budget")
        self.assertEqual(board, original)


if __name__ == "__main__":
    unittest.main(verbosity=2)
