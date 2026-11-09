"""分区初始化的公开入口与保留原铜、附属孔、来源和网表的集成回归。"""
from copy import deepcopy
from pathlib import Path
import sys
import unittest

from shapely.geometry import box

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pcb_pipeline_common import normalize_board, check_board, PipelineError
from pcb_placement import place_components, _envelope
from pcb_pipeline import run_pipeline
from pcb_compact import compact_layout


def board_fixture():
    """无入参；返回含旧铜接口和板外器件的合成板；非法规范化输入直接抛错。"""
    components = []
    for cid, x, y in (("P", 70, 100), ("A", 14000, 16000)):
        components.append(dict(id=cid, x=x, y=y, rotation=0, layer=1, width=30, height=24,
            pads=[dict(padId="1", net="N", localX=0, localY=0,
                       shape="RECTANGLE", localW=20, localH=16)]))
    return normalize_board(dict(units="mil", board_bbox=[0, 0, 500, 300], components=components,
        nets={"N": [dict(compId=cid, padId="1") for cid in ("P", "A")]},
        routes=dict(traces=[dict(id="old-trace", net="N", layer=1, sx=70, sy=100, ex=120, ey=100, width=4)]),
        tags={}, metadata={"board_usage": "test_only", "provenance_note": "合成来源保留标记"},
        rules=dict(copper_layers=[1, 2], minimum_trace_width=1, default_clearance=2, clearance_matrix=[],
                   board_edge_clearance=2, component_courtyard_margin=2)))


def placement_options():
    """无入参；返回具有显式预算的分区初始化配置。"""
    return dict(method="analytic_sa", initialization="auto", rotations=[0, 90], grid_step=10,
                max_candidates=600, max_seconds=10, global_iterations=0, sa_temperatures=0,
                cluster_intra_steps=0, functional_groups=[dict(id="logic", components=["A"],
                                                              region=[220, 50, 450, 250])])


class InitialPlaceIntegrationTests(unittest.TestCase):
    def test_existing_copper_and_provenance_unchanged(self):
        board = board_fixture(); before = deepcopy(board)
        result, stats = place_components(board, placement_options())
        self.assertEqual(board, before)
        self.assertEqual(result["components"][0], before["components"][0])
        self.assertEqual(result["routes"], before["routes"])
        self.assertEqual(result["metadata"], before["metadata"])
        self.assertEqual(result["nets"], before["nets"])
        self.assertEqual(check_board(result, ignore_connectivity=True), [])
        self.assertTrue(box(220, 50, 450, 250).covers(_envelope(result["components"][1], 0.001)))
        self.assertIn("P", stats["copper_locked_components"])

    def test_complete_pipeline_and_zone_intersection(self):
        board = board_fixture()
        options = placement_options()
        options["regions"] = {"A": [250, 60, 430, 240]}
        report = run_pipeline(dict(schema_version=1, board=board,
            constraints=[dict(operator="ZONE_ANCHOR", component="A", region=[260, 80, 410, 220])],
            options=dict(max_iterations=1, placement=options,
                         routing=dict(grid_step=5, width=4, max_seconds=10, max_net_seconds=10,
                                      trace_layers=[1], allow_vias=False))))
        self.assertEqual(report["status"], "offline_passed", report)
        result = report["board"]
        self.assertEqual(result["nets"], board["nets"])
        self.assertEqual(result["routes"]["traces"][0], board["routes"]["traces"][0])
        component = next(c for c in result["components"] if c["id"] == "A")
        self.assertTrue(box(260, 80, 410, 220).covers(_envelope(component, 0.001)))
        self.assertEqual(check_board(result), [])

    def test_attached_via_moves_with_parent_and_keeps_dimensions(self):
        board = board_fixture()
        parent = board["components"][1]
        parent["metadata"] = dict(attached_via_ids=["attached-via"], footprint_via_movement="rigid")
        board["routes"]["vias"] = [dict(id="attached-via", net="N", x=parent["x"]+3, y=parent["y"],
            diameter=10, hole=4, layers=[1, 2], metadata=dict(parent_component_id="A",
                source_local_x=3, source_local_y=0, source_hole_diameter=4, source_copper_diameter=10))]
        before = deepcopy(board)
        opts = placement_options(); opts["rotations"] = [0]
        result, _ = place_components(board, opts)
        moved = result["components"][1]; via = result["routes"]["vias"][0]
        self.assertEqual(board, before)
        self.assertAlmostEqual(via["x"], moved["x"]+3)
        self.assertAlmostEqual(via["y"], moved["y"])
        for field in ("id", "diameter", "hole", "layers", "net", "metadata"):
            self.assertEqual(via[field], before["routes"]["vias"][0][field])
        self.assertEqual(check_board(result, ignore_connectivity=True), [])

    def test_fixed_outside_region_rejected_without_source_mutation(self):
        board = board_fixture(); board["components"][1]["locked"] = True
        before = deepcopy(board)
        with self.assertRaises(PipelineError):
            place_components(board, placement_options())
        self.assertEqual(board, before)

    def test_failure_pipeline_does_not_return_partial_board(self):
        opts = placement_options()
        opts["functional_groups"][0]["region"] = [200, 100, 205, 105]
        report = run_pipeline(dict(schema_version=1, board=board_fixture(), constraints=[],
                                   options=dict(max_iterations=1, placement=opts)))
        self.assertEqual(report["status"], "failed")
        self.assertIsNone(report["board"])
        self.assertIsNone(report["writeback_plan"])

    def test_group_cannot_weaken_existing_region(self):
        opts = placement_options(); opts["regions"] = {"A": [10, 10, 50, 50]}
        with self.assertRaises(PipelineError):
            place_components(board_fixture(), opts)

    def test_compact_public_entry_uses_the_same_initializer(self):
        board = board_fixture()
        with self.assertRaisesRegex(PipelineError, "unrouted source"):
            compact_layout(board, [[500, 300]], placement_options=placement_options())
        # 缩板入口的旧铜保护保持；另用未布线合成输入验证新初始化接入。
        board["routes"] = {}
        board["components"][0]["locked"] = True
        board = normalize_board(board)
        original = deepcopy(board)
        result, report = compact_layout(board, [[500, 300]], contract_centers=False,
                                        placement_options=placement_options())
        self.assertTrue(report["geometry_passed"], report)
        self.assertEqual(board, original)
        self.assertEqual(result["nets"], original["nets"])
        self.assertEqual(result["routes"], original["routes"])
        self.assertEqual(result["components"][0], original["components"][0])
        self.assertEqual(check_board(result, ignore_connectivity=True), [])
        self.assertTrue(box(220, 50, 450, 250).covers(_envelope(result["components"][1], 0.001)))


if __name__ == "__main__":
    unittest.main()
