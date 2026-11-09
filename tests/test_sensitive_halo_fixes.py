"""显式敏感区范围必须在候选、占用栅格和独立检查中一致。"""
import sys
from pathlib import Path
from copy import deepcopy
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pcb_pipeline_common import CollisionModel, check_board, normalize_board
from pcb_solver_hardened import sensitive_halo_targets
from pcb_geometry import GeometryError
from pcb_routing import _net_options, algorithm_options
from pcb_occupancy import OccupancyGrid


def fixture(kind="NORMAL", **scope):
    components = [dict(id="S", x=100, y=50, rotation=0, layer=1, width=20, height=20, pads=[])]
    for name, x in (("A", 20), ("B", 180)):
        components.append(dict(id=name, x=x, y=25, rotation=0, layer=1, width=4, height=4,
            pads=[dict(padId="1", net="N", localX=0, localY=0, localW=4, localH=4, shape="RECTANGLE")]))
    return normalize_board(dict(units="mil", board_bbox=[0,0,200,100], components=components,
        nets={"N":[dict(compId=n,padId="1") for n in ("A","B")]},
        tags=dict(component_tags={"S":dict(role="SENSITIVE_IC",keepout_halo=20,**scope)},
                  net_tags={"N":dict(type=kind)}), routes={}, metadata={},
        rules=dict(copper_layers=[1,2],board_edge_clearance=1,component_courtyard_margin=0,
                   default_clearance=2,clearance_matrix=[])))


class HaloTests(unittest.TestCase):
    def accepted(self, board):
        candidate = CollisionModel(board).trace_ok("N",1,20,25,180,25,4)
        routed = deepcopy(board)
        routed["routes"]["traces"] = [dict(net="N",layer=1,sx=20,sy=25,ex=180,ey=25,width=4)]
        errors = check_board(routed)
        self.assertEqual(candidate, not errors, errors)
        grid = OccupancyGrid(CollisionModel(board),"N",_net_options(board,"N",dict(width=4)),5,
                             max_cells=100000,stats={})
        self.assertEqual(grid.trace_free(1,20,5), candidate)
        return candidate

    def test_explicit_normal_net_and_type_are_protected(self):
        self.assertFalse(self.accepted(fixture(halo_nets=["N"])))
        self.assertFalse(self.accepted(fixture(halo_net_types=["NORMAL"])))

    def test_old_defaults_and_explicit_intersection(self):
        self.assertTrue(self.accepted(fixture()))
        self.assertFalse(self.accepted(fixture("SEWAGE")))
        self.assertTrue(self.accepted(fixture(halo_nets=["N"],halo_net_types=["SEWAGE"])))

    def test_bad_selectors_and_typo_fail_closed(self):
        for scope in ({"halo_nets":[]},{"halo_nets":["missing"]},{"halo_nets":["N","N"]},
                      {"halo_net_types":["typo"]},{"halo_net_types":None}):
            with self.subTest(scope=scope):
                with self.assertRaises(ValueError): fixture(**scope)
                board=fixture()
                board["tags"]["component_tags"]["S"].update(scope)
                with self.assertRaises(GeometryError): CollisionModel(board)
                self.assertTrue(check_board(board))
        with self.assertRaises(GeometryError): sensitive_halo_targets(dict(halo_nets=["N"]),{"N":[]})
        board=fixture()
        board["tags"]["component_tags"]["S"]["halo_net_typo"]=["NORMAL"]
        self.assertTrue(check_board(board))

    def test_new_routing_options_strict_and_old_defaults(self):
        self.assertFalse(algorithm_options()["negotiated_grid_cache"])
        self.assertEqual(algorithm_options()["negotiated_bend_penalty"],0)
        for options in (dict(negotiated_grid_cache=1),dict(negotiated_bend_penalty=True),
                        dict(negotiated_bend_penalty=-1),dict(negotiated_bend_penalty=float("nan")),
                        dict(negotiated_grid_cache=True),dict(unknown_new_field=True)):
            with self.subTest(options=options), self.assertRaises(ValueError): algorithm_options(options)


if __name__ == "__main__": unittest.main()
