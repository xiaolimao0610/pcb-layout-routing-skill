"""差分显式细线、载流下限与RUDY等价性的独立回归，不连接EDA。"""
from copy import deepcopy
from pathlib import Path
import random
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pcb_pipeline_common import PipelineError, normalize_board, check_board
from pcb_pipeline import run_pipeline
from pcb_constraints import compile_constraints
from pcb_differential import route_pairs, quality_report
from pcb_routing import _Budget, algorithm_options
from pcb_global_place import _pins, _rudy, validate_options, PlacementFailure
from pcb_ampacity import current_width_requirement
from pcb_width_policy import selected_width


def fixture(width=5):
    """返回四层双端无障碍测试板；所有几何、规则和端子显式声明。"""
    parts, nets = [], {"P": [], "N": []}
    for end, x in enumerate((0, 500)):
        for net, sign in (("P", 1), ("N", -1)):
            name, y = f"{net}{end}", sign*(width+5)/2
            parts.append(dict(id=name, x=x, y=y, rotation=0, layer=1,
                body_polygon=[[-2,-2],[2,-2],[2,2],[-2,2]],
                pads=[dict(padId="1", net=net, localX=0, localY=0, layer=1,
                           shape="RECTANGLE", localW=4, localH=4)]))
            nets[net].append(dict(compId=name, padId="1"))
    return normalize_board(dict(units="mil", board_bbox=[-100,-150,600,150],
        components=parts, nets=nets, tags={}, routes={}, metadata={"board_usage":"test_only"},
        rules=dict(copper_layers=[1,15,16,2], minimum_trace_width=1, default_clearance=2,
                   board_edge_clearance=5, component_courtyard_margin=0, clearance_matrix=[])))


def constraints(width=5):
    return [dict(operator="CHANNEL_SPEC", net="P", diff_pair=dict(p="P", n="N", width=width, gap=5))]


class WidthPolicyTests(unittest.TestCase):
    def test_explicit_5mil_diff_pipeline_and_default_10mil(self):
        for width in (5, 10):
            board = fixture(width)
            original = deepcopy(board)
            result = run_pipeline(dict(schema_version=1, board=board, constraints=constraints(width),
                options=dict(placement=dict(enabled=False), routing=dict(grid_step=5,
                    max_seconds=15, max_net_seconds=10, trace_layers=[1], diff_max_candidates=8))))
            self.assertEqual(result["status"], "offline_passed", result)
            self.assertEqual({line["width"] for line in result["board"]["routes"]["traces"]}, {width})
            self.assertEqual(check_board(result["board"]), [])
            self.assertTrue(quality_report(result["board"], constraints(width))["passed"])
            self.assertEqual(board, original)
        self.assertEqual(selected_width(fixture(), "P"), 10)
        self.assertEqual(selected_width(fixture(), "P", 5), 5)

    def test_real_board_or_network_limit_rejects_5mil_at_both_entries(self):
        for board_limit, net_limit in ((6, 0), (1, 10)):
            board = fixture()
            board["rules"]["minimum_trace_width"] = board_limit
            board["tags"]["net_tags"] = {"N": {"min_width": net_limit}}
            with self.assertRaises(PipelineError):
                compile_constraints(board, {}, constraints())
            opt = algorithm_options(dict(max_seconds=10, max_net_seconds=5))
            with self.assertRaises(PipelineError):
                route_pairs(board, constraints(), opt, {}, _Budget(opt), {})

    def test_original_wider_copper_is_not_rewritten(self):
        board = fixture(10)
        board["routes"]["traces"] = [dict(net=net, layer=1, sx=0, sy=sign*7.5,
            ex=500, ey=sign*7.5, width=10) for net, sign in (("P",1),("N",-1))]
        before = deepcopy(board)
        opt = algorithm_options(dict(max_seconds=10, max_net_seconds=5))
        with self.assertRaises(PipelineError):
            route_pairs(board, constraints(5), opt, {}, _Budget(opt), {})
        self.assertEqual(board, before)

    def test_current_explicit_width_preserves_model_and_default_floor(self):
        board = fixture()
        rule = dict(net="P", current_a=0.01, temp_rise_c=10, copper_oz=1)
        self.assertEqual(current_width_requirement(board, rule)["selected_min_width_mil"], 10)
        self.assertEqual(current_width_requirement(board, dict(rule, min_width=5))["selected_min_width_mil"], 5)
        self.assertGreater(current_width_requirement(board, dict(rule, min_width=5, current_a=3))["selected_min_width_mil"], 5)
        with self.assertRaises(PipelineError):
            compile_constraints(board, {}, constraints()+[dict(rule, operator="CHANNEL_SPEC", current_a=3)])

    def test_diff_current_rules_are_order_independent(self):
        current = dict(operator="CHANNEL_SPEC", net="N", current_a=0.01, temp_rise_c=10, copper_oz=1)
        declaration = constraints()[0]
        outputs = [compile_constraints(fixture(), {}, rules) for rules in
                   ([current, declaration], [declaration, current])]
        for board, cfg, _ in outputs:
            self.assertEqual(board["tags"]["net_tags"]["N"]["min_width"], 5)
            self.assertEqual(cfg["placement"]["rudy_widths"], {"P":5,"N":5})

    def test_deferred_ground_does_not_inherit_signal_width(self):
        board = fixture()
        board["nets"]["GND"] = board["nets"].pop("N")
        for component in board["components"]:
            for pad in component["pads"]:
                if pad["net"] == "N":
                    pad["net"] = "GND"
        board["tags"]["net_tags"] = {"GND":{"min_width":30}}
        config = dict(placement=dict(enabled=False), routing=dict(width=5,
            ground_strategy="plane_then_drc", ground_nets=["GND"]))
        compiled, options, _ = compile_constraints(board, config, [])
        self.assertEqual(options["placement"]["rudy_widths"], {"P":5,"GND":30})
        result = run_pipeline(dict(schema_version=1, board=board, constraints=[], options=config))
        self.assertEqual(result["status"], "awaiting_native_ground", result)
        self.assertFalse(result["full_board_accepted"])

    def test_compile_rudy_widths_idempotent_and_conflicts_rejected(self):
        board, cfg, _ = compile_constraints(fixture(), {}, constraints())
        self.assertEqual(cfg["placement"]["rudy_widths"], {"P":5, "N":5})
        self.assertEqual(compile_constraints(board, cfg, constraints())[1], cfg)
        for cfg in ({"routing":{"width":None}}, {"routing":{"width":10}}, {"placement":{"rudy_widths":{"P":10}}},
                    {"placement":{"rudy_widths":{"missing":5}}}):
            with self.assertRaises(PipelineError):
                compile_constraints(fixture(), cfg, constraints())


class RudyTests(unittest.TestCase):
    def test_selected_5mil_width_reaches_both_cost_methods(self):
        board, cfg, _ = compile_constraints(fixture(), {"routing":{"width":5}}, [])
        for method in ("legacy", "vectorized"):
            opt = validate_options(dict(cfg["placement"], rudy_method=method), board)
            report, boxes, hpwl = _rudy(board, opt, _pins(board))
            rect, density = boxes["P"]
            self.assertAlmostEqual(density*rect.area/hpwl["P"], 5)

    def test_vectorized_matches_geometric_legacy(self):
        rng = random.Random(713)
        for grid in ([1,1], [4,9], [32,24]):
            for case in range(5):
                board = fixture()
                for index, component in enumerate(board["components"]):
                    component["x"] = rng.uniform(-105, 605)
                    component["y"] = 0 if case == 0 else rng.uniform(-155, 155)
                options = dict(rudy_grid=grid, rudy_capacity=0.1, rudy_widths={"P":5,"N":15})
                outputs = [_rudy(board, validate_options(dict(options, rudy_method=method), board), _pins(board))
                           for method in ("legacy", "vectorized")]
                for key in ("peak", "overflow"):
                    self.assertAlmostEqual(outputs[0][0][key], outputs[1][0][key], places=10)
                self.assertEqual(outputs[0][0]["passed"], outputs[1][0]["passed"])
                self.assertEqual(outputs[0][2], outputs[1][2])

    def test_vectorized_budget_and_strict_options(self):
        class Expired:
            def check(self):
                raise PlacementFailure("PLACEMENT_TIME_BUDGET", "测试预算耗尽，未找到")
        board = fixture()
        opt = validate_options(dict(rudy_method="vectorized"), board)
        with self.assertRaises(PlacementFailure):
            _rudy(board, opt, _pins(board), Expired())
        for options in ({"rudy_method":"fast"}, {"rudy_widths":{"P":None}}, {"rudy_widths":{"P":0}},
                        {"rudy_widths":{"missing":5}}, {"rudy_unknown":True}):
            with self.assertRaises(PipelineError):
                validate_options(options, board)
        self.assertEqual(validate_options({}, board)["rudy_method"], "legacy")


if __name__ == "__main__":
    unittest.main()
