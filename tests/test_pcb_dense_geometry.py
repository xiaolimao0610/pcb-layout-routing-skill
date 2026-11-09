"""连续坐标候选的窄通道、实体障碍和预算反例。"""
from copy import deepcopy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pcb_geometric_router import geometry_candidate, validate_options
from pcb_pipeline_common import CollisionModel, PipelineError, check_board, normalize_board
from pcb_routing import _Budget, _commit, _groups, _net_options, route_board, RoutingFailure


def fixture():
    """两个不对齐10mil格点的真实端子和非网格狭窄通道。"""
    parts = [dict(id=name, x=x, y=3.25, rotation=0, layer=1,
        body_polygon=[[-4,-4],[4,-4],[4,4],[-4,4]],
        pads=[dict(padId="1", net="N", localX=0, localY=0, layer=1,
                   shape="RECTANGLE", localW=8, localH=8)]) for name,x in (("A",20),("B",180))]
    return normalize_board(dict(units="mil", board_bbox=[0,-100,200,100], components=parts,
        nets={"N":[dict(compId=name,padId="1") for name in ("A","B")]},
        tags={}, routes={"keepouts":[
            dict(points=[[60,-100],[140,-100],[140,-1.0],[60,-1.0]],layers=[1]),
            dict(points=[[60,7.5],[140,7.5],[140,100],[60,100]],layers=[1])]},
        rules=dict(copper_layers=[1,2], board_edge_clearance=0, default_clearance=1,
                   component_courtyard_margin=0, minimum_trace_width=1), metadata={"source":{"uuid":"test","sourceFingerprint":"test"}}))


def options(**updates):
    result = dict(width=4, allow_vias=False, trace_layers=[1], diagonal=True,
                  grid_step=10, max_seconds=20, max_net_seconds=20,
                  geometry_search=dict(enabled=True,max_seconds=10,max_nodes=128,max_edges=10000))
    result.update(updates)
    return result


def alternating_gates_fixture():
    """三道交替窄门：有效中心走廊没有10mil格点，且必须多次转折。"""
    board = fixture()
    board["board_bbox"] = [0, 0, 240, 180]
    for component, x, y in zip(board["components"], (20,220), (35.25,65.25)):
        component.update(x=x,y=y)
    board["routes"]["keepouts"] = []
    for x, y in ((65,35.25),(120,105.25),(175,65.25)):
        board["routes"]["keepouts"].extend([
            dict(points=[[x-5,0],[x+5,0],[x+5,y-4.25],[x-5,y-4.25]],layers=[1]),
            dict(points=[[x-5,y+4.25],[x+5,y+4.25],[x+5,180],[x-5,180]],layers=[1])])
    return normalize_board(board)


def alternating_gates_options(enabled=False):
    """基准使用相同全局预算、规则和网格，只切换几何候选开关。"""
    return options(max_passes=1,max_expansions=50000,max_seconds=30,max_net_seconds=30,
                   geometry_search=dict(enabled=enabled,max_seconds=15,max_nodes=256,max_edges=40000))


def candidate(board, config=None):
    config = config or options()
    stats, budget = {}, _Budget(config)
    budget.enter("N")
    groups = _groups(board,"N")
    result = geometry_candidate(CollisionModel(board),"N",groups[0],groups[1],
                                _net_options(board,"N",config),config,stats,budget)
    return result, stats, budget


class GeometryTests(unittest.TestCase):
    def test_offgrid_narrow_channel_and_exact_commit(self):
        board = fixture()
        before = deepcopy(board)
        result, stats, budget = candidate(board)
        self.assertIsNotNone(result)
        accepted = _commit(board,"N",*result,len(_groups(board,"N")),budget)
        self.assertIsNotNone(accepted)
        self.assertEqual(check_board(accepted),[])
        self.assertEqual(board,before)
        self.assertTrue(stats["geometry_searches"][0]["found"])
        self.assertTrue(any(t["sy"] % 10 != 0 for t in result[0]))

    def test_obstacle_boundary_detour_has_multiple_turns(self):
        board=fixture()
        board["routes"]["keepouts"]=[dict(points=[[70,-20],[130,-20],[130,30],[70,30]],layers=[1])]
        result, _, budget = candidate(board)
        self.assertIsNotNone(result)
        accepted = _commit(board,"N",*result,2,budget)
        self.assertIsNotNone(accepted)
        self.assertEqual(check_board(accepted),[])
        self.assertGreater(len(result[0]),1)

    def test_full_height_wall_cannot_be_hidden(self):
        board=fixture()
        board["routes"]["keepouts"]=[dict(points=[[95,-100],[105,-100],[105,100],[95,100]],layers=[1])]
        result, stats, _ = candidate(board)
        self.assertIsNone(result)
        self.assertFalse(stats["geometry_searches"][0]["found"])

    def test_edge_budget_is_not_claimed_infeasible(self):
        cfg=options(geometry_search=dict(enabled=True,max_edges=1))
        with self.assertRaises(RoutingFailure) as caught:
            candidate(fixture(),cfg)
        self.assertEqual(caught.exception.diagnostics["code"],"GEOMETRY_BUDGET")
        self.assertIn("未证明无解",str(caught.exception))

    def test_default_disabled_unknown_and_boolean_rejected(self):
        self.assertFalse(validate_options()["enabled"])
        for value in ({"max_edges":True},{"unexpected":1},{"max_seconds":float("nan")}):
            with self.assertRaises(PipelineError):validate_options(value)
        result, stats, _ = candidate(fixture(),options(geometry_search={}))
        self.assertIsNone(result)
        self.assertEqual(stats,{})

    def test_refinement_options_are_strict_and_finite(self):
        self.assertEqual(validate_options()["refine_factors"],[2.0,4.0])
        self.assertEqual(validate_options({"refine_factors":[]})["refine_factors"],[])
        for values in ([1],[2,2],[float("inf")],[2,3,4,5],[True],[17]):
            with self.assertRaises(PipelineError):validate_options({"refine_factors":values})
        for count in (0,True,100001):
            with self.assertRaises(PipelineError):validate_options({"max_refine_expansions":count})

    def test_integrated_alternating_gates_legacy_fails_geometry_routes(self):
        board=alternating_gates_fixture()
        with self.assertRaises(RoutingFailure):
            route_board(board,alternating_gates_options(False))
        result,stats=route_board(board,alternating_gates_options(True))
        self.assertEqual(check_board(result),[])
        self.assertTrue(any(item["found"] for item in stats["geometry_searches"]))

    def test_via_must_respect_hidden_inner_layer_obstacle(self):
        board=fixture()
        board["rules"]["copper_layers"]=[1,15,2]
        board["components"][1]["layer"]=2
        board["components"][1]["pads"][0]["layer"]=2
        board["routes"]["keepouts"]=[dict(points=[[0,-100],[200,-100],[200,100],[0,100]],layers=[15])]
        cfg=options(allow_vias=True,trace_layers=[1,2])
        result,_,_=candidate(board,cfg)
        self.assertIsNone(result)

    def test_ordered_topology_wrapper_still_uses_exact_forbidden_geometry(self):
        board=alternating_gates_fixture()
        cfg=alternating_gates_options(True)
        cfg["topology_orders"]={"N":[dict(compId="A",padId="1"),dict(compId="B",padId="1")]}
        result,stats=route_board(board,cfg)
        self.assertEqual(check_board(result),[])
        self.assertTrue(stats["geometry_searches"][0]["found"])


if __name__ == "__main__":
    unittest.main()
