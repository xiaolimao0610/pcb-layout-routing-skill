"""局部合法化的敏感区投影，不改源标签，不用局部网表冒充完整验收。"""
from copy import deepcopy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pcb_pipeline_common import normalize_board, check_board
from pcb_placement import _subset, place_components
from pcb_pipeline import run_pipeline
from pcb_compact import compact_layout


def fixture(**scope):
    """输入可选敏感区筛选，返回固定敏感件与尚未放置外部网络的合法板。"""
    parts = [dict(id="S", x=100, y=50, rotation=0, layer=1, width=20, height=20, pads=[], locked=True)]
    nets = {"NOISE": [], "QUIET": []}
    for net, y in (("NOISE", 90), ("QUIET", 110)):
        for index, x in enumerate((20, 180)):
            cid = net+str(index)
            parts.append(dict(id=cid, x=x, y=y, rotation=0, layer=1, width=4, height=4,
                pads=[dict(padId="1", net=net, localX=0, localY=0, shape="RECTANGLE", localW=4, localH=4)]))
            nets[net].append(dict(compId=cid, padId="1"))
    return normalize_board(dict(units="mil", board_bbox=[0,0,200,140], components=parts,nets=nets,
        routes={}, metadata={"board_usage":"test_only"},
        tags=dict(component_tags={"S":dict(role="SENSITIVE_IC",keepout_halo=20,**(scope or {"halo_nets":["NOISE"]}))},
                  net_tags={"NOISE":dict(type="SEWAGE"),"QUIET":dict(type="NORMAL")}),
        rules=dict(copper_layers=[1,2],minimum_trace_width=1,default_clearance=2,clearance_matrix=[],
                   board_edge_clearance=1,component_courtyard_margin=0)))


def options(method):
    return dict(method=method,rotations=[0],grid_step=10,max_candidates=1000,max_seconds=5,
                global_iterations=0,sa_temperatures=0,cluster_intra_steps=0,repair_attempt=1 if method=="legacy" else 0)


class PartialHaloTests(unittest.TestCase):
    def test_partial_external_net_and_source_unchanged(self):
        board=fixture()
        original=deepcopy(board)
        partial=_subset(board,[board["components"][0]])
        self.assertEqual(check_board(partial,ignore_connectivity=True),[])
        self.assertEqual(board,original)
        complete=_subset(board,board["components"])
        self.assertEqual(complete["tags"],original["tags"])
        self.assertEqual(complete["routes"],original["routes"])

    def test_analytic_and_legacy_complete_placement(self):
        for method in ("legacy","analytic_sa"):
            with self.subTest(method=method):
                board=fixture();original=deepcopy(board)
                result,stats=place_components(board,options(method))
                self.assertEqual(check_board(result,ignore_connectivity=True),[])
                self.assertEqual(result["tags"],board["tags"])
                self.assertEqual(result["nets"],board["nets"])
                self.assertFalse(result["routes"].get("keepouts"))
                self.assertEqual(board,original)

    def test_full_pipeline_both_methods(self):
        for method in ("legacy","analytic_sa"):
            with self.subTest(method=method):
                board=fixture();original=deepcopy(board)
                report=run_pipeline(dict(schema_version=1,board=board,constraints=[],options=dict(
                    placement=options(method),routing=dict(grid_step=5,width=4,max_seconds=10,max_net_seconds=5,
                                                         trace_layers=[1]))))
                self.assertEqual(report["status"],"offline_passed",report)
                self.assertEqual(report["board"]["tags"]["component_tags"],board["tags"]["component_tags"])
                self.assertFalse(report["board"]["routes"].get("keepouts"))
                self.assertEqual(board,original)

    def test_seed_and_compact_entries_preserve_complete_rules(self):
        board=fixture();original=deepcopy(board)
        seeded=dict(options("analytic_sa"),initial_poses={"NOISE0":dict(x=30,y=90,rotation=0,layer=1)})
        result,_=place_components(board,seeded)
        self.assertEqual(result["tags"],board["tags"])
        self.assertFalse(result["routes"].get("keepouts"))
        for method,size in (("legacy",[180,140]),("analytic_sa",[200,140])):
            with self.subTest(method=method):
                result,report=compact_layout(board,[size],placement_options=options(method),contract_centers=False)
                self.assertTrue(report["geometry_passed"],report)
                self.assertEqual(result["tags"],board["tags"])
                self.assertFalse(result["routes"].get("keepouts"))
                self.assertFalse(report["full_board_accepted"])
        self.assertEqual(board,original)

    def test_missing_targets_do_not_broaden_type_intersection(self):
        board=fixture(halo_nets=["NOISE"],halo_net_types=["NORMAL","SEWAGE"])
        board["routes"]["traces"]=[dict(net="QUIET",layer=1,sx=70,sy=50,ex=130,ey=50,width=4)]
        kept=[c for c in board["components"] if not c["id"].startswith("NOISE")]
        partial=_subset(board,kept)
        self.assertEqual(check_board(partial,ignore_connectivity=True),[])

    def test_partial_intersection_keeps_present_and_absent_target_protection(self):
        board=fixture(halo_nets=["NOISE","QUIET"],halo_net_types=["SEWAGE"])
        board["routes"]["traces"]=[dict(net="NOISE",layer=1,sx=70,sy=50,ex=130,ey=50,width=4)]
        kept=[c for c in board["components"] if not c["id"].startswith("NOISE")]
        partial=_subset(board,kept)
        self.assertTrue(any("KEEPOUT" in e for e in check_board(partial,ignore_connectivity=True)))

    def test_retained_trace_via_and_pour_are_still_protected(self):
        copper=[("traces",dict(net="NOISE",layer=1,sx=70,sy=50,ex=130,ey=50,width=4)),
                ("vias",dict(net="NOISE",x=100,y=50,hole=4,diameter=8,layers=[1,2])),
                ("copper_pours",dict(net="NOISE",layer=1,filled_polygons=[dict(points=[[95,45],[105,45],[105,55],[95,55]])]))]
        for kind,item in copper:
            with self.subTest(kind=kind):
                board=fixture();board["routes"][kind]=[item];original=deepcopy(board)
                partial=_subset(board,[board["components"][0]])
                self.assertEqual(partial["routes"][kind],board["routes"][kind])
                self.assertTrue(any("KEEPOUT" in e for e in check_board(partial,ignore_connectivity=True)))
                self.assertTrue(any("KEEPOUT" in e for e in check_board(board,ignore_connectivity=True)))
                self.assertEqual(board,original)

    def test_type_only_projection_uses_original_classification(self):
        for selected,blocked in ((["SEWAGE"],True),(["NORMAL"],False)):
            board=fixture(halo_net_types=selected)
            board["routes"]["traces"]=[dict(net="NOISE",layer=1,sx=70,sy=50,ex=130,ey=50,width=4)]
            partial=_subset(board,[board["components"][0]])
            self.assertEqual(any("KEEPOUT" in e for e in check_board(partial,ignore_connectivity=True)),blocked)

    def test_unknown_selector_rejected_before_partial_projection(self):
        board=fixture();board["tags"]["component_tags"]["S"]["halo_nets"]=["MISSING"]
        self.assertTrue(check_board(board,ignore_connectivity=True))
        with self.assertRaises(ValueError): _subset(board,[board["components"][0]])
        for method in ("legacy","analytic_sa"):
            with self.assertRaises(ValueError): place_components(board,options(method))


if __name__=="__main__": unittest.main()
