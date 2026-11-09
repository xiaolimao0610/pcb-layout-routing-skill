"""装配/丝印质量反例：保留对象、来源、层别与非零净距，不替代整板验收。"""
from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pcb_assembly import check_assembly, main
from pcb_pipeline_common import PipelineError


SOURCE = {"uuid": "fixture-board", "sourceFingerprint": "fixture-fingerprint"}


def fixture():
    """返回两个真实规范器件的独立小板；仅用于明确几何反例。"""
    components = []
    for name, x in (("A", 0), ("B", 35)):
        components.append(dict(id=name, x=x, y=0, rotation=0, layer=1,
            body_polygon=[[-10, -10], [10, -10], [10, 10], [-10, 10]],
            pads=[dict(padId="1", net=name, localX=0, localY=0, layer=1,
                       shape="RECTANGLE", localW=8, localH=8)]))
    return dict(units="mil", board_bbox=[-100, -100, 300, 300], components=components,
        nets={name: [dict(compId=name, padId="1")] for name in ("A", "B")},
        tags={}, routes={}, metadata={"source": deepcopy(SOURCE)})


def high_options():
    return dict(source=deepcopy(SOURCE), high_components=[dict(component_id="A",
        envelope=[[-10, -10], [10, -10], [10, 10], [-10, 10]], clearance=20,
        provenance="测试夹具明确焊接净距")])


def label(identity, x, y=50, layer=3):
    return dict(id=identity, layer=layer, kind="text_bounds",
        points=[[x, y], [x + 10, y], [x + 10, y + 10], [x, y + 10]],
        bounds_are_conservative=True, provenance="夹具显式保守字框；不是字体轮廓")


def silk_options(*objects):
    return dict(source=deepcopy(SOURCE), silkscreen=list(objects),
        silkscreen_expected_ids=[identity for item in objects for identity in item.get("source_ids", [item["id"]])],
        silkscreen_clearance=3, silkscreen_pad_clearance=3)


def outline():
    """明确含一个孔洞和第二个孤岛的合并轮廓，不能补洞或取凸包。"""
    return dict(id="outline-A", owner_id="A", layer=3, kind="outline",
        geometry={"type": "MultiPolygon", "coordinates": [
            [[[100, 50], [160, 50], [160, 110], [100, 110], [100, 50]],
             [[110, 60], [110, 100], [150, 100], [150, 60], [110, 60]]],
            [[[200, 100], [220, 100], [220, 120], [200, 120], [200, 100]]]]},
        source_ids=["native-line-1", "native-line-2", "native-arc-3"],
        bounds_are_conservative=True, provenance="已转换全部笔宽和弧形的显式保守实体")


class AssemblyTests(unittest.TestCase):
    def test_positive_clearance_and_no_mutation(self):
        board, options = fixture(), high_options()
        original_board, original_options = deepcopy(board), deepcopy(options)
        report = check_assembly(board, options)
        self.assertEqual(len(report["findings"]), 1)
        self.assertAlmostEqual(report["findings"][0]["measured_clearance_mil"], 15)
        self.assertFalse(report["findings"][0]["overlap_or_touch"])
        self.assertEqual(board, original_board)
        self.assertEqual(options, original_options)
        self.assertFalse(report["full_board_accepted"])

    def test_opposite_component_side_is_not_mixed(self):
        board = fixture()
        board["components"][1]["layer"] = 2
        report = check_assembly(board, high_options())
        self.assertTrue(report["passed"])
        self.assertEqual(report["input_inventory"]["components"], ["A", "B"])

    def test_bottom_transform_and_envelope_cannot_shrink(self):
        board = fixture()
        board["components"][0].update(layer=2, rotation=90)
        board["components"][1].update(layer=2, x=0, y=35)
        self.assertFalse(check_assembly(board, high_options())["passed"])
        options = high_options()
        options["high_components"][0]["envelope"] = [[-2, -2], [2, -2], [2, 2], [-2, 2]]
        with self.assertRaises(PipelineError):
            check_assembly(board, options)

    def test_text_gap_and_opposite_layers(self):
        options = silk_options(label("L1", 0), label("L2", 12))
        report = check_assembly(fixture(), options)
        self.assertEqual(len(report["findings"]), 1)
        finding = report["findings"][0]
        self.assertEqual(finding["measured_clearance_mil"], 2)
        self.assertTrue(finding["potential_text_conflict"])
        self.assertFalse(finding["overlap_or_touch"])
        options["silkscreen"][1]["layer"] = 4
        self.assertTrue(check_assembly(fixture(), options)["passed"])

    def test_line_stroke_width_and_text_overlap(self):
        line = dict(id="stroke", layer=3, kind="line", sx=0, sy=55, ex=8, ey=55,
                    width=8, provenance="明确笔宽和端点")
        report = check_assembly(fixture(), silk_options(line, label("text", 10)))
        self.assertTrue(report["findings"][0]["overlap_or_touch"])
        self.assertGreater(report["findings"][0]["overlap_area_mil2"], 0)

    def test_grouped_outline_preserves_holes_islands_and_raw_inventory(self):
        options = silk_options(outline(), label("inside-hole", 120, y=70), label("between-islands", 175, y=80))
        report = check_assembly(fixture(), options)
        self.assertTrue(report["passed"])
        self.assertEqual(len(report["input_inventory"]["silkscreen"]), 3)
        self.assertEqual(len(report["input_inventory"]["silkscreen_observed_source_ids"]), 5)
        self.assertEqual(report["input_inventory"]["silkscreen_missing"], [])
        self.assertTrue(check_assembly(fixture(), silk_options(outline()))["passed"])
        report = check_assembly(fixture(), silk_options(outline(), label("cross-outline", 103, y=70)))
        self.assertEqual(len(report["findings"]), 1)
        self.assertTrue(report["findings"][0]["potential_text_conflict"])

    def test_outline_invalid_hole_duplicate_source_and_missing_evidence(self):
        item = outline()
        item["geometry"]["coordinates"][0][1] = [[300, 300], [310, 300], [310, 310], [300, 300]]
        with self.assertRaises(PipelineError):
            check_assembly(fixture(), silk_options(item))
        item = outline()
        options = silk_options(item, label("native-line-1", 100))
        with self.assertRaises(PipelineError):
            check_assembly(fixture(), options)
        item = outline()
        del item["bounds_are_conservative"]
        report = check_assembly(fixture(), silk_options(item))
        self.assertFalse(report["passed"])
        self.assertTrue(any("轮廓" in message for message in report["unknown"]))

    def test_silkscreen_pad_gap_and_through_hole_both_faces(self):
        options = silk_options(label("text", 6, y=-2))
        options["silkscreen_pad_clearance"] = 3
        report = check_assembly(fixture(), options)
        self.assertTrue(any(f["kind"] == "silkscreen_pad_clearance" for f in report["findings"]))
        options["silkscreen"][0]["layer"] = 4
        self.assertTrue(check_assembly(fixture(), options)["passed"])
        board = fixture()
        board["components"][0]["pads"][0].update(layer=12, hole=2, plated=True)
        self.assertTrue(any(f["kind"] == "silkscreen_pad_clearance"
                            for f in check_assembly(board, options)["findings"]))

    def test_missing_evidence_is_unknown(self):
        options = silk_options(label("text", 100))
        del options["source"]
        del options["silkscreen_expected_ids"]
        del options["silkscreen"][0]["provenance"]
        del options["silkscreen"][0]["bounds_are_conservative"]
        report = check_assembly(fixture(), options)
        self.assertEqual(report["findings"], [])
        self.assertGreaterEqual(len(report["unknown"]), 4)
        self.assertFalse(report["passed"])

    def test_inventory_mismatch_and_budget_keep_all_objects(self):
        options = silk_options(label("a", 100), label("b", 120), label("c", 140))
        options["silkscreen_expected_ids"].append("missing")
        options["max_checks"] = 1
        report = check_assembly(fixture(), options)
        self.assertEqual(report["input_inventory"]["silkscreen"], ["a", "b", "c"])
        self.assertEqual(report["input_inventory"]["silkscreen_missing"], ["missing"])
        self.assertTrue(report["budget_exhausted"])
        self.assertFalse(report["complete"])
        self.assertFalse(report["passed"])

    def test_time_budget_and_locked_component_remain_advisory(self):
        board, options = fixture(), high_options()
        board["components"][1]["locked"] = True
        report = check_assembly(board, options)
        self.assertEqual(report["candidate_rules"][0]["action"], "review_fixed_component")
        self.assertFalse(report["findings"][0]["hard_drc_failure"])
        options["max_seconds"] = 1e-12
        report = check_assembly(board, options)
        self.assertTrue(report["budget_exhausted"])
        self.assertFalse(report["passed"])
        self.assertEqual(report["input_inventory"]["components"], ["A", "B"])

    def test_source_unknown_fields_and_duplicate_ids_rejected(self):
        invalid = [high_options() for _ in range(3)]
        invalid[0]["source"]["uuid"] = "wrong"
        invalid[1]["mystery"] = 1
        invalid[2]["high_components"][0]["mystery"] = 1
        invalid.append(silk_options(label("duplicate", 100), label("duplicate", 120)))
        for options in invalid:
            with self.subTest(options=options), self.assertRaises(PipelineError):
                check_assembly(fixture(), options)

    def test_cli_writes_report_only(self):
        with tempfile.TemporaryDirectory() as folder:
            paths = [Path(folder) / name for name in ("board.json", "options.json", "report.json")]
            paths[0].write_text(json.dumps(fixture()), encoding="utf-8")
            paths[1].write_text(json.dumps(high_options()), encoding="utf-8")
            before = paths[0].read_bytes()
            self.assertEqual(main(["--board", str(paths[0]), "--options", str(paths[1]), "--output", str(paths[2])]), 2)
            self.assertEqual(paths[0].read_bytes(), before)
            self.assertFalse(json.loads(paths[2].read_text(encoding="utf-8"))["full_board_accepted"])
            self.assertEqual(main(["--board", str(paths[0]), "--options", str(paths[1]), "--output", str(paths[0])]), 1)
            self.assertEqual(paths[0].read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
