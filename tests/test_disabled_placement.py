"""禁用布局仍验证完整输入，但不得为纯布线重算器件旧铜锁。"""
from copy import deepcopy
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pcb_placement as placement
from pcb_pipeline_common import normalize_board, PipelineError


def fixture():
    """返回带原铜、真实形状和完整端子表的合成板，不连接原生编辑器。"""
    return normalize_board(dict(units="mil", board_bbox=[0, 0, 400, 300],
        metadata={"board_usage": "test_only"}, tags={},
        components=[dict(id=cid, x=x, y=100, rotation=0, layer=1,
            width=20, height=20, pads=[dict(padId="1", net="N", localX=0,
                localY=0, shape="RECTANGLE", localW=8, localH=8)])
            for cid, x in (("A", 100), ("B", 200))],
        nets={"N": [dict(compId=cid, padId="1") for cid in ("A", "B")]},
        routes={"traces": [dict(id="old", net="N", layer=1,
                               sx=100, sy=100, ex=200, ey=100, width=2)]},
        rules=dict(copper_layers=[1, 2], minimum_trace_width=1,
            default_clearance=2, clearance_matrix=[], board_edge_clearance=1,
            component_courtyard_margin=0)))


class DisabledPlacementTests(unittest.TestCase):
    def test_disabled_keeps_geometry_and_skips_copper_lock_for_both_methods(self):
        for method in ("legacy", "analytic_sa"):
            board = fixture()
            original = deepcopy(board)
            with self.subTest(method=method), patch.object(placement, "_copper_locked",
                    side_effect=AssertionError("禁用布局不应扫描铜锁")) as copper_lock, \
                    patch.object(placement, "check_board", wraps=placement.check_board) as check:
                candidate, stats = placement.place_components(board, dict(
                    enabled=False, method=method, max_seconds=5))
            copper_lock.assert_not_called()
            self.assertGreater(check.call_count, 0)
            self.assertEqual(candidate, original)
            self.assertEqual(board, original)
            self.assertTrue(stats["copper_lock_scan_skipped"])

    def test_disabled_rejects_invalid_copper_and_region(self):
        invalid_copper = fixture()
        invalid_copper["routes"]["traces"][0]["ex"] = 500
        for method in ("legacy", "analytic_sa"):
            for board, extra in ((invalid_copper, {}),
                    (fixture(), {"regions": {"A": [250, 200, 300, 250]}})):
                with self.subTest(method=method, extra=extra), \
                        patch.object(placement, "_copper_locked",
                            side_effect=AssertionError("不应扫描铜锁")), \
                        self.assertRaises(PipelineError):
                    placement.place_components(board, dict(enabled=False,
                        method=method, max_seconds=5, **extra))

    def test_disabled_does_not_accept_pose_mutation(self):
        with patch.object(placement, "_copper_locked",
                side_effect=AssertionError("不应扫描铜锁")), \
                self.assertRaisesRegex(PipelineError, "requires enabled placement"):
            placement.place_components(fixture(), dict(enabled=False,
                initial_poses={"A": dict(x=150, y=100, rotation=0, layer=1)}))

    def test_enabled_still_scans_and_protects_attached_copper(self):
        native = placement._copper_locked
        board = fixture()
        with patch.object(placement, "_copper_locked", wraps=native) as copper_lock:
            candidate, stats = placement.place_components(board, dict(enabled=True))
        copper_lock.assert_called_once()
        self.assertEqual(stats["copper_locked_components"], ["A", "B"])
        self.assertFalse(stats["copper_lock_scan_skipped"])
        self.assertEqual(candidate, board)


if __name__ == "__main__":
    unittest.main()
