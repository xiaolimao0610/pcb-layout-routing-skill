"""原理图局部连线与反例。全部坐标来自合成图页。"""

import copy
import unittest

from schematic_wiring import audit_wires, native_name_seeds, route_page


def pin(ref, net, x, y, direction, **kw):
    return dict(ref=ref, pin=1, net=net, x=x, y=y, dir=direction, **kw)


def wire(net, *points, show_name=False):
    return {"net": net, "points": points, "show_name": show_name}


class SchematicWiringTests(unittest.TestCase):
    def page(self, pins, **kw):
        return dict(bounds=[0, 0, 120, 100], grid=5, pins=pins, **kw)

    def test_local_net_connects_three_real_pins_with_visible_branch(self):
        p = self.page(
            [
                pin("R", "S", 0, 50, "R"),
                pin("U", "S", 100, 50, "L"),
                pin("C", "S", 50, 80, "D"),
            ]
        )
        candidate = route_page(p)
        self.assertTrue(candidate["completed"])
        self.assertFalse(any(w["show_name"] for w in candidate["wires"]))
        ends = [tuple(w["points"][i]) for w in candidate["wires"] for i in (0, -1)]
        self.assertTrue(any(ends.count(point) == 3 for point in ends))
        self.assertFalse(audit_wires(p, candidate["wires"])["errors"])

    def test_same_name_stubs_cannot_satisfy_local_net(self):
        p = self.page([pin("IC", "S", 0, 50, "R"), pin("J", "S", 100, 50, "L")])
        wrong = [
            wire("S", (0, 50), (10, 50), show_name=True),
            wire("S", (90, 50), (100, 50), show_name=True),
        ]
        self.assertTrue(audit_wires(p, wrong)["errors"])

    def test_endpoint_on_foreign_net_and_collinear_short_rejected(self):
        p = self.page(
            [
                pin("A", "H", 0, 50, "R"),
                pin("B", "H", 100, 50, "L"),
                pin("C", "V", 50, 80, "D"),
            ]
        )
        for point in [(50, 50), (80, 50)]:
            with self.subTest(point=point):
                second = [(50, 80), (50, 50)]
                if point != (50, 50):
                    second.append(point)
                self.assertTrue(
                    audit_wires(
                        p,
                        [
                            wire("H", (0, 50), (100, 50)),
                            wire("V", *second, show_name=True),
                        ],
                    )["errors"]
                )

    def test_only_unbroken_orthogonal_crossings_accepted(self):
        p = self.page(
            [
                pin("A", "H", 0, 50, "R"),
                pin("B", "H", 100, 50, "L"),
                pin("C", "V", 50, 0, "U"),
                pin("D", "V", 50, 100, "D"),
            ]
        )
        h = wire("H", (0, 50), (100, 50))
        v = wire("V", (50, 0), (50, 100))
        audit = audit_wires(p, [h, v])
        self.assertFalse(audit["errors"])
        self.assertEqual(audit["crossings"], 1)
        split = [wire("V", (50, 0), (50, 50)), wire("V", (50, 50), (50, 100))]
        self.assertTrue(audit_wires(p, [h] + split)["errors"])

    def test_separate_shared_power_groups_each_get_visible_name(self):
        p = self.page(
            [
                pin("A", "PWR", 0, 10, "R", block="模拟"),
                pin("B", "PWR", 80, 70, "L", block="数字"),
            ],
            shared_nets=["PWR"],
        )
        candidate = route_page(p)
        self.assertTrue(candidate["completed"])
        self.assertEqual(len([w for w in candidate["wires"] if w["show_name"]]), 2)
        self.assertFalse(candidate["label_conflicts"])
        wrong = copy.deepcopy(candidate["wires"])
        wrong[0]["show_name"] = False
        self.assertTrue(audit_wires(p, wrong)["errors"])

    def test_same_net_unsplit_branch_is_not_a_verified_junction(self):
        page = self.page(
            [
                pin("A", "SIG", 0, 40, "R"),
                pin("B", "SIG", 100, 40, "L"),
                pin("C", "SIG", 50, 90, "D"),
            ]
        )
        trunk = wire("SIG", (0, 40), (100, 40))
        branch = wire("SIG", (50, 90), (50, 40))
        self.assertTrue(audit_wires(page, [trunk, branch])["errors"])

    def test_native_net_carrier_is_unique_per_electrically_connected_tree(self):
        page = self.page(
            [
                pin("A", "S", 0, 50, "R"),
                pin("B", "S", 100, 50, "L"),
                pin("C", "S", 50, 80, "D"),
                pin("X", "V", 20, 10, "R", block="1"),
                pin("Y", "V", 80, 90, "L", block="2"),
            ],
            shared_nets=["V"],
        )
        first = route_page(page)
        native = native_name_seeds(page, first["wires"])
        self.assertEqual(native["explicit_net_count"], 3)
        self.assertEqual(
            sum(w["explicit_net"] for w in native["wires"] if w["net"] == "S"), 1
        )
        self.assertEqual(
            sum(w["explicit_net"] for w in native["wires"] if w["net"] == "V"), 2
        )
        self.assertFalse(any(w["show_name"] for w in first["wires"] if w["net"] == "S"))
        with self.assertRaises(ValueError):
            native_name_seeds(page, [])

    def test_dense_pin_label_can_clear_adjacent_printed_text(self):
        p = self.page(
            [pin("U", "CTRL", 10, 50, "R"), pin("R", "CTRL", 65, 50, "L")],
            cross_page_nets=["CTRL"],
            label_obstacles=[[0, 38, 120, 45], [0, 55, 120, 58]],
        )
        result = route_page(p)
        self.assertTrue(result["completed"])
        self.assertFalse(result["label_conflicts"])
        (visible,) = [w for w in result["wires"] if w["show_name"]]
        self.assertGreater(visible["label"][1], 62)

    def test_local_signal_rejects_independent_port_override(self):
        with self.assertRaises(ValueError):
            route_page(self.page([pin("A", "SIG", 0, 10, "R", port=True)]))

    def test_budget_and_impassable_obstacles_report_unrouted(self):
        pins = [pin("A", "N", 0, 50, "R"), pin("B", "N", 100, 50, "L")]
        for kw in ({"max_expansions": 1}, {"obstacles": [[33, 0, 34, 100]]}):
            with self.subTest(kw=kw):
                result = route_page(self.page(pins, **kw))
                self.assertFalse(result["completed"])
                self.assertTrue(result["unrouted"])
        self.assertTrue(
            route_page(self.page(pins, obstacles=[[32, 40, 38, 60]]))["completed"]
        )

    def test_false_pin_landing_orphan_and_nc_rejected(self):
        p = self.page(
            [
                pin("A", "S", 0, 50, "R"),
                pin("B", "S", 100, 50, "L"),
                pin("NC", None, 50, 50, "U"),
            ]
        )
        good = route_page(p)
        self.assertTrue(good["completed"])
        self.assertTrue(audit_wires(p, [wire("S", (0, 50), (100, 50))])["errors"])
        self.assertTrue(
            audit_wires(p, good["wires"] + [wire("S", (30, 20), (40, 20))])["errors"]
        )

    def test_grid_and_duplicate_pin_errors_do_not_snap(self):
        with self.assertRaises(ValueError):
            route_page(self.page([pin("A", "SIG", 0.1, 30, "L")]))
        with self.assertRaises(ValueError):
            route_page(
                self.page([pin("A", "SIG", 50, 30, "L"), pin("A", "SIG", 55, 30, "R")])
            )

    def test_cross_page_signal_routes_within_page_keeps_one_label(self):
        p = self.page(
            [pin("A", "PWM", 0, 40, "R"), pin("B", "PWM", 100, 40, "L")],
            cross_page_nets=["PWM"],
        )
        result = route_page(p)
        self.assertTrue(result["completed"])
        names = [w for w in result["wires"] if w["show_name"]]
        self.assertEqual(len(names), 1)
        self.assertEqual(len(names[0]["label"]), 2)


if __name__ == "__main__":
    unittest.main()
