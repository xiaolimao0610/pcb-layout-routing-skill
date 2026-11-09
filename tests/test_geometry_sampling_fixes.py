"""公平边界采样和方向状态的回归；实际铜仍经过独立精检。"""
from copy import deepcopy
import math
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pcb_geometric_router as geometry
from pcb_pipeline_common import CollisionModel, PipelineError, normalize_board, check_board
from pcb_routing import _Budget, _groups, _net_options, _commit, RoutingFailure


def fixture():
    """返回两圆形顶层禁区及一个内层禁区的独立四层测试板。"""
    parts = [dict(id=name, x=x, y=0, rotation=0, layer=1,
                  body_polygon=[[-4,-4],[4,-4],[4,4],[-4,4]],
                  pads=[dict(padId="1", net="N", localX=0, localY=0, layer=1,
                             shape="RECTANGLE", localW=8, localH=8)])
             for name,x in (("A",20),("B",980))]
    keepouts = [dict(points=[[x+20*math.cos(i*2*math.pi/64), y+20*math.sin(i*2*math.pi/64)]
                            for i in range(64)], layers=[layer])
                for x,y,layer in ((300,50,1),(700,50,1),(500,-50,15))]
    return normalize_board(dict(units="mil", board_bbox=[0,-200,1000,200],
        components=parts, nets={"N":[dict(compId=name,padId="1") for name in ("A","B")]},
        tags={}, routes={"keepouts":keepouts},
        rules=dict(copper_layers=[1,15,16,2],minimum_trace_width=1,default_clearance=2,
                   board_edge_clearance=0,component_courtyard_margin=0), metadata={"board_usage":"test_only"}))


def run(board, **configuration):
    """输入板和几何配置，返回候选/统计/共享预算；求解异常保留原诊断。"""
    options = dict(width=4,allow_vias=True,trace_layers=[1,15,16,2],diagonal=True,
                   grid_step=10,max_seconds=30,max_net_seconds=30,
                   geometry_search=dict(enabled=True, **configuration))
    budget = _Budget(options)
    budget.enter("N")
    stats = {}
    groups = _groups(board,"N")
    result = geometry.geometry_candidate(CollisionModel(board),"N",groups[0],groups[1],
                                        _net_options(board,"N",options),options,stats,budget)
    return result,stats,budget


class SamplingFixTests(unittest.TestCase):
    def test_default_legacy_and_strict_new_options(self):
        self.assertEqual(geometry.validate_options()["sampling"], "legacy")
        self.assertEqual(geometry.validate_options()["bend_penalty"], 0)
        for cfg in ({"sampling":"guess"}, {"sampling":True}, {"bend_penalty":True},
                    {"bend_penalty":-1}, {"bend_penalty":float("inf")}):
            with self.assertRaises(PipelineError):
                geometry.validate_options(cfg)

    def test_balanced_samples_inner_obstacle_and_exact_commit(self):
        board = fixture()
        original = deepcopy(board)
        result,stats,budget = run(board, sampling="balanced")
        record = stats["geometry_searches"][0]
        self.assertEqual(record["boundaries_sampled_by_layer"][1], 2)
        self.assertEqual(record["boundaries_sampled_by_layer"][15], 1)
        self.assertTrue(record["boundary_discovery_complete"])
        self.assertLess(record["nodes"], 512)
        self.assertLess(record["edge_checks"], 12000)
        self.assertIsNotNone(result)
        accepted = _commit(board,"N",*result,2,budget)
        self.assertIsNotNone(accepted)
        self.assertEqual(check_board(accepted), [])
        self.assertEqual(board, original)

    def test_legacy_failure_still_reported_as_budget(self):
        with self.assertRaises(RoutingFailure) as caught:
            run(fixture(),sampling="legacy")
        self.assertEqual(caught.exception.diagnostics["code"],"GEOMETRY_BUDGET")
        self.assertIn("未证明无解",str(caught.exception))

    def test_small_budget_reports_partial_coverage_and_is_deterministic(self):
        board = fixture()
        first,stats,_ = run(board,sampling="balanced",max_nodes=24)
        second,again,_ = run(board,sampling="balanced",max_nodes=24)
        self.assertEqual(first, second)
        for record in (stats["geometry_searches"][0],again["geometry_searches"][0]):
            self.assertEqual(record["nodes"],24)
            self.assertTrue(record["node_candidates_truncated"])
            self.assertGreater(record["boundaries_sampled_by_layer"][15],0)

    def test_balanced_edge_budget_remains_hard(self):
        with self.assertRaises(RoutingFailure) as caught:
            run(fixture(),sampling="balanced",max_edges=1)
        self.assertEqual(caught.exception.diagnostics["code"],"GEOMETRY_BUDGET")

    def test_obstacles_on_all_layers_still_refuse_connection(self):
        board = fixture()
        board["routes"]["keepouts"] = [dict(points=[[450,-200],[550,-200],[550,200],[450,200]],layers=[1,15,16,2])]
        result,_,_ = run(board,sampling="balanced",max_edges=100000)
        self.assertIsNone(result)

    def test_turn_cost_counts_internal_and_incoming_bends(self):
        board = fixture()
        board["routes"]["keepouts"] = []
        model = CollisionModel(board)
        options = dict(width=4,max_seconds=30,max_net_seconds=30,diagonal=False)
        settings = _net_options(board,"N",options)
        stats = dict(edge_checks=0)
        budget = geometry._SearchBudget(_Budget(options),geometry.validate_options(),stats)
        choices = geometry._edge_choices(model,"N",(100,0,1),(120,20,1),settings,options,budget,(1.0,0.0),7)
        self.assertEqual(sorted(c[0] for c in choices), [47,54])
        self.assertEqual(sorted(c[4] for c in choices), [1,2])

    def test_turn_cost_route_uses_real_copper_and_exact_commit(self):
        board = fixture()
        board["routes"]["keepouts"] = [dict(points=[[450,-30],[550,-30],[550,30],[450,30]],layers=[1,15,16,2])]
        result,stats,budget = run(board,sampling="balanced",bend_penalty=7,max_edges=100000)
        self.assertIsNotNone(result)
        record = stats["geometry_searches"][0]
        self.assertGreaterEqual(record["bend_count"],2)
        self.assertEqual(record["bend_cost"],7*record["bend_count"])
        self.assertEqual(result[1],[])
        length = sum(math.hypot(t["ex"]-t["sx"],t["ey"]-t["sy"]) for t in result[0])
        self.assertAlmostEqual(record["path_cost"],length+record["bend_cost"])
        accepted = _commit(board,"N",*result,2,budget)
        self.assertIsNotNone(accepted)
        self.assertEqual(check_board(accepted),[])

    def test_via_resets_heading_and_is_still_checked(self):
        board = fixture()
        model = CollisionModel(board)
        options = dict(width=4,max_seconds=30,max_net_seconds=30)
        settings = _net_options(board,"N",options)
        stats = dict(edge_checks=0)
        budget = geometry._SearchBudget(_Budget(options),geometry.validate_options(),stats)
        choices = geometry._edge_choices(model,"N",(100,0,1),(100,0,15),settings,options,budget,(1,0),7)
        self.assertEqual(len(choices),1)
        self.assertIsNone(choices[0][3])
        self.assertEqual(choices[0][4],0)
        self.assertEqual(choices[0][0],settings["via_cost"])
        board["routes"]["keepouts"].append(dict(points=[[90,-10],[110,-10],[110,10],[90,10]],layers=[16]))
        self.assertEqual(geometry._edge_choices(CollisionModel(board),"N",(100,0,1),(100,0,15),settings,options,budget,(1,0),7),[])

    def test_search_preserves_two_arrival_directions_at_same_node(self):
        # 此项只验证搜索状态：同节点稍贵但方向合适的路径必须保留，实铜另有独立测试。
        board = fixture()
        start,junction,end = (20.,0.,1),(21.,0.,1),(22.,0.,1)
        options = dict(width=4,max_seconds=30,max_net_seconds=30,
                       geometry_search=dict(enabled=True,sampling="balanced",bend_penalty=10))
        settings = _net_options(board,"N",options)
        settings["layers"] = [1]
        def populate(model,net,settings,cfg,region,nodes,add,record,budget):
            add(junction,1)
        def edges(model,net,a,b,settings,options,budget,incoming,penalty):
            budget.check(edge=True)
            if (a,b)==(start,junction):
                return [(2,[],[],(1,0),0),(3,[],[],(0,1),0)]
            if (a,b)==(junction,end):
                turns = int(incoming != (0,1))
                return [(1+turns*penalty,[],[],(0,1),turns)]
            return []
        stats = {}
        with patch.object(geometry,"_anchors",side_effect=[[start],[end]]), \
             patch.object(geometry,"_balanced_nodes",side_effect=populate), \
             patch.object(geometry,"_edge_choices",side_effect=edges):
            geometry.geometry_candidate(CollisionModel(board),"N",{}, {},settings,options,stats,_Budget(options))
        self.assertEqual(stats["geometry_searches"][0]["path_cost"],4)
        self.assertGreaterEqual(stats["geometry_searches"][0]["states"],4)


if __name__ == "__main__":
    unittest.main()
