"""功能块合页、标题栏保留、分页及预算反例。"""
import unittest

from schematic_pagination import pack_blocks


class PaginationTests(unittest.TestCase):
    def spec(self, n):
        return {"bounds":[0,0,120,100], "blocked":[[70,0,120,30]], "gap":2,
                "blocks":[{"id":str(i),"width":42,"height":28} for i in range(n)]}

    def test_repeated_channels_keep_order_and_avoid_title(self):
        result = pack_blocks(self.spec(5))
        self.assertTrue(result["complete"])
        self.assertEqual(result["pages_used"], 1)
        self.assertLess(result["placements"]["0"]["x"], result["placements"]["1"]["x"])
        for r in result["placements"].values():
            self.assertTrue(r["x"]+r["width"] <= 68 or r["y"] >= 32)

    def test_open_another_page_without_shrinking(self):
        result = pack_blocks(self.spec(9))
        self.assertTrue(result["complete"])
        self.assertGreater(result["pages_used"], 1)
        self.assertTrue(all(p["width"] == 42 for p in result["placements"].values()))

    def test_budgets_and_oversize_are_incomplete(self):
        for override in ({"max_pages":1}, {"max_candidates":1}, {"bounds":[0,0,30,20]}):
            spec = self.spec(9)
            spec.update(override)
            report = pack_blocks(spec)
            self.assertFalse(report["complete"])
            self.assertTrue(report["pending"])

    def test_unknown_or_duplicate_block_rejected(self):
        spec = self.spec(2)
        spec["blocks"][0]["related"] = ["missing"]
        with self.assertRaises(ValueError):
            pack_blocks(spec)
        spec["blocks"][0]["related"] = []
        spec["blocks"][0]["id"] = "1"
        with self.assertRaises(ValueError):
            pack_blocks(spec)
