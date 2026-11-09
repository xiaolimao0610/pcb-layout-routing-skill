"""相对功能模板从散落导入生成种子，并独立复核关系与硬几何。"""
from copy import deepcopy
from pathlib import Path
import sys
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from pcb_functional_cells import check_relations, validate_cells
from pcb_global_place import PlacementFailure
from pcb_pipeline_common import normalize_board, check_board, PipelineError
from pcb_placement import place_components


def fixture():
    components=[dict(id=str(i),x=14000+i*2000,y=14000+i*1700,rotation=0,layer=1,
        width=20,height=10,pads=[dict(padId="1",net="N",localX=3,localY=0,
                                    shape="RECTANGLE",localW=4,localH=4)]) for i in range(2)]
    return normalize_board(dict(units="mil",board_bbox=[0,0,300,200],components=components,
        nets={"N":[dict(compId=str(i),padId="1") for i in range(2)]},routes={},tags={},
        metadata={"source":{"document_uuid":"cells-fixture","fingerprint":"sha256:example"}},
        rules=dict(copper_layers=[1,2],minimum_trace_width=1,default_clearance=2,
                   clearance_matrix=[],board_edge_clearance=5,component_courtyard_margin=4)))


def cells(**relation_changes):
    relation=dict(neighbor=dict(compId="1",padId="1"),sides=["right"],distances=[40],rotations=[0])
    relation.update(relation_changes)
    return [dict(id="supply_cell",anchor=dict(compId="0",padId="1"),relations=[relation],
                 provenance="合成数据手册实例：指定电源脚与电容脚的相对关系")]


def options(**extra):
    cfg=dict(method="analytic_sa",initialization="auto",rotations=[0,90,180,270],
        max_candidates=400,max_seconds=10,grid_step=10,global_iterations=0,
        sa_temperatures=0,cluster_intra_steps=0,functional_cells=cells())
    cfg.update(extra)
    return cfg


class FunctionalCellsTests(unittest.TestCase):
    def test_generated_from_real_pins_without_import_coordinate_dependence(self):
        board=fixture(); before=deepcopy(board); shifted=deepcopy(board)
        shifted["components"][0].update(x=-90000,y=80000)
        shifted["components"][1].update(x=120000,y=-70000)
        result,stats=place_components(board,options())
        other,_=place_components(shifted,options())
        self.assertEqual(result["components"],other["components"])
        self.assertEqual(board,before)
        self.assertEqual(result["nets"],board["nets"])
        self.assertEqual(result["tags"],board["tags"])
        self.assertEqual(result["metadata"],board["metadata"])
        self.assertEqual(check_board(result,ignore_connectivity=True),[])
        self.assertEqual(check_relations(result,validate_cells(cells(),result)),[])
        self.assertEqual(stats["functional_cells"][0]["status"],"seed_generated_requires_board_check")
        self.assertFalse(stats["full_board_accepted"])

    def test_fixed_anchor_moves_only_free_neighbor(self):
        board=fixture(); board["components"][0].update(x=100,y=100,locked=True)
        result,_=place_components(board,options())
        self.assertEqual(result["components"][0],board["components"][0])
        self.assertEqual(result["components"][1]["x"],140)
        self.assertEqual(check_board(result,ignore_connectivity=True),[])

    def test_bottom_mirror_and_pad_rotation_are_real(self):
        board=fixture()
        for c in board["components"]:
            c.update(layer=2,rotation=90)
            for p in c["pads"]:p["layer"]=2
        cfg=options(functional_cells=cells(rotations=[90]),rotations=[0,90,180,270])
        result,_=place_components(board,cfg)
        self.assertEqual(check_relations(result,validate_cells(cfg["functional_cells"],result)),[])
        self.assertEqual(check_board(result,ignore_connectivity=True),[])

    def test_tampered_final_relation_is_independently_rejected(self):
        result,_=place_components(fixture(),options())
        result["components"][1]["x"]+=10
        self.assertEqual(check_board(result,ignore_connectivity=True),[])
        errors=check_relations(result,validate_cells(cells(),result))
        self.assertTrue(errors)
        self.assertTrue(errors[0].startswith("FUNCTIONAL_CELL_RELATION"))
        incomplete=deepcopy(result); incomplete["components"].pop()
        self.assertTrue(check_relations(incomplete,cells()))
        self.assertFalse(check_relations(incomplete,cells(),partial=True))

    def test_immovable_neighbor_and_copper_lock_cannot_be_overridden(self):
        for kind in ("locked","copper"):
            board=fixture()
            board["components"][0].update(x=100,y=100)
            board["components"][1].update(x=200,y=100)
            if kind=="locked":board["components"][1]["locked"]=True
            else:board["routes"]["traces"]=[dict(net="N",layer=1,sx=203,sy=100,ex=230,ey=100,width=2)]
            before=deepcopy(board)
            with self.assertRaises(PlacementFailure):place_components(board,options())
            self.assertEqual(board,before)

    def test_candidate_budget_exhausted_does_not_return_half_seed(self):
        board=fixture(); before=deepcopy(board)
        declaration=cells(distances=[1,40]); declaration[0]["max_candidates"]=1
        with self.assertRaises(PlacementFailure) as caught:place_components(board,options(functional_cells=declaration))
        self.assertEqual(caught.exception.diagnostics["code"],"PLACEMENT_CELL_BUDGET")
        self.assertIn("未证明无解",str(caught.exception))
        self.assertEqual(board,before)

    def test_cell_must_fit_actual_region_and_board(self):
        cfg=options(functional_groups=[dict(id="tiny",components=["0","1"],region=[20,20,50,50])])
        with self.assertRaises(PlacementFailure):place_components(fixture(),cfg)

    def test_unknown_world_coordinates_duplicate_members_and_legacy_rejected(self):
        bad=cells(); bad[0]["x"]=1
        with self.assertRaises(PipelineError):place_components(fixture(),options(functional_cells=bad))
        bad=cells(); bad[0]["relations"][0]["neighbor"]["compId"]="0"
        with self.assertRaises(PipelineError):place_components(fixture(),options(functional_cells=bad))
        with self.assertRaises(PipelineError):place_components(fixture(),dict(method="legacy",functional_cells=cells()))
        with self.assertRaises(PipelineError):place_components(fixture(),options(clusters=[dict(id="conflict",components=["0","1"])]))


if __name__=="__main__":unittest.main()
