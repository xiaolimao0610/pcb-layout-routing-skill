"""旧布局与紧凑排布的预算、候选惰性生成和分区回归。"""
from copy import deepcopy
from pathlib import Path
import sys
import random
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pcb_placement as placement
import pcb_compact as compact
from pcb_global_place import PlacementFailure
from pcb_pipeline_common import normalize_board, check_board, rules_for, PipelineError


def fixture(parts=None):
    """输入可选器件清单，返回不连接原生软件的合成几何板。"""
    return normalize_board(dict(units="mil", board_bbox=[0, 0, 1000, 800],
        components=parts or [dict(id="A", x=14000, y=14000, rotation=0, layer=1,
                                 width=20, height=20, pads=[])],
        nets={}, routes={}, metadata={"board_usage": "test_only"}, tags={},
        rules=dict(copper_layers=[1, 2], minimum_trace_width=1, default_clearance=2,
                   clearance_matrix=[], board_edge_clearance=1, component_courtyard_margin=0)))


def opts(**kwargs):
    return dict(method="legacy", rotations=[0], grid_step=10, max_candidates=100,
                max_seconds=1, **kwargs)


class PlacementTimeBudgetTests(unittest.TestCase):
    def test_candidate_loop_checks_budget_while_generating(self):
        board=fixture(); rules=rules_for(board)
        settings=placement._options(opts(),board,rules); counts={"generated_candidates":0}
        def checkpoint(phase):
            if counts["generated_candidates"] >= 3:
                raise PlacementFailure("PLACEMENT_TIME_BUDGET","测试时限耗尽，未找到进一步候选",budget_scope="time")
        with self.assertRaises(PlacementFailure):
            placement._candidates(board["components"][0],settings,placement.board_polygon(board),rules,{},
                                  random.Random(0),board=board,checkpoint=checkpoint,counters=counts)
        self.assertEqual(counts["generated_candidates"],3)

    def test_initial_check_obeys_wall_time(self):
        board=fixture(); original=deepcopy(board); clock=[0.0]
        native=placement.check_board
        def slow_check(*args, **kwargs):
            result=native(*args, **kwargs); clock[0]=2.0; return result
        with patch.object(placement.time, "monotonic", side_effect=lambda: clock[0]), \
             patch.object(placement, "check_board", side_effect=slow_check):
            with self.assertRaises(PlacementFailure) as caught:
                placement.place_components(board, opts())
        self.assertEqual(caught.exception.diagnostics["phase"], "initial_geometry")
        self.assertEqual(caught.exception.diagnostics["budget_scope"], "time")
        self.assertEqual(board, original)

    def test_generation_stops_without_returning_partial_board(self):
        board=fixture(); clock=[0.0]; native=placement._candidates
        def slow_pool(*args, **kwargs):
            clock[0]=2.0; return native(*args, **kwargs)
        with patch.object(placement.time, "monotonic", side_effect=lambda: clock[0]), \
             patch.object(placement, "_candidates", side_effect=slow_pool):
            with self.assertRaises(PlacementFailure) as caught:
                placement.place_components(board, opts())
        self.assertEqual(caught.exception.diagnostics["phase"], "candidate_generation")
        self.assertEqual(caught.exception.diagnostics["stats"]["candidate_pool_count"], 1)

    def test_valid_complete_board_survives_optimization_timeout(self):
        board=fixture([dict(id="A",x=200,y=200,rotation=0,layer=1,width=20,height=20,pads=[])])
        original=deepcopy(board); clock=[0.0]; native=placement._candidates
        def slow_pool(*args, **kwargs):
            clock[0]=2.0; return native(*args, **kwargs)
        with patch.object(placement.time, "monotonic", side_effect=lambda: clock[0]), \
             patch.object(placement, "_candidates", side_effect=slow_pool):
            result,stats=placement.place_components(board, opts(optimize=True))
        self.assertEqual(result["components"], original["components"])
        self.assertEqual(result["nets"], original["nets"])
        self.assertEqual(result["routes"], original["routes"])
        self.assertEqual(board, original)
        self.assertEqual(check_board(result,ignore_connectivity=True), [])
        self.assertTrue(stats["budget_exhausted"])
        self.assertTrue(stats["retained_complete_candidate"])

    def test_unreachable_later_parts_do_not_get_candidate_pools(self):
        board=fixture([dict(id=cid,x=14000,y=14000,rotation=0,layer=1,width=width,height=20,pads=[])
                       for cid,width in (("A",2000),("B",20),("C",20))])
        seen=[]; native=placement._candidates
        def observe(comp,*args,**kwargs):
            seen.append(comp["id"]); return native(comp,*args,**kwargs)
        with patch.object(placement,"_candidates",side_effect=observe):
            with self.assertRaises(PipelineError):
                placement.place_components(board,opts())
        self.assertEqual(seen,["A"])

    def test_candidate_budget_has_structured_failure(self):
        options=opts();options["max_candidates"]=1
        with self.assertRaises(PlacementFailure) as caught:
            placement.place_components(fixture(),options)
        self.assertEqual(caught.exception.diagnostics["budget_scope"],"candidates")
        self.assertIn("未证明无解",str(caught.exception))

    def test_candidate_generation_is_counted_separately_and_reproducible(self):
        options=opts();options["max_seconds"]=10
        first,stats=placement.place_components(fixture(),options)
        second,again=placement.place_components(fixture(),options)
        self.assertEqual(first,second)
        self.assertEqual(stats["generated_candidates"],again["generated_candidates"])
        self.assertGreater(stats["generated_candidates"],stats["candidate_checks"])
        self.assertEqual(stats["candidate_pool_count"],1)
        self.assertEqual(check_board(first,ignore_connectivity=True),[])

    def test_compact_uses_declared_region_before_grid_generation(self):
        board=fixture(); options=opts(regions={"A":[100,100,300,300]})
        options["max_candidates"]=12
        settings=placement._options(options,board,rules_for(board))
        result,stats=compact._pack_independent(board,settings,set())
        shape=placement._envelope(result["components"][0],rules_for(board).curve_tolerance)
        self.assertTrue(settings["regions"]["A"].covers(shape))
        self.assertLessEqual(stats["candidate_checks"],12)
        self.assertEqual(check_board(result,ignore_connectivity=True),[])

    def test_compact_timeout_rejects_partial_result(self):
        board=fixture(); settings=placement._options(opts(),board,rules_for(board))
        clock=[0.0]; native=compact.check_board
        def slow_check(*args,**kwargs):
            result=native(*args,**kwargs);clock[0]=2.0;return result
        with patch.object(compact.time,"monotonic",side_effect=lambda:clock[0]), \
             patch.object(compact,"check_board",side_effect=slow_check):
            with self.assertRaises(PlacementFailure) as caught:
                compact._pack_independent(board,settings,set())
        self.assertEqual(caught.exception.diagnostics["phase"],"compact_packing")

    def test_compact_keeps_fixed_region_requirement(self):
        board=fixture([dict(id="A",x=500,y=500,rotation=0,layer=1,width=20,height=20,pads=[],locked=True)])
        settings=placement._options(opts(regions={"A":[100,100,300,300]}),board,rules_for(board))
        with self.assertRaisesRegex(PipelineError,"PLACEMENT_REGION"):
            compact._pack_independent(board,settings,{"A"})

    def test_legacy_cannot_silently_ignore_regional_initialization(self):
        with self.assertRaisesRegex(PipelineError,"analytic_sa"):
            placement.place_components(fixture(),opts(initialization="auto"))


if __name__=="__main__":
    unittest.main()
