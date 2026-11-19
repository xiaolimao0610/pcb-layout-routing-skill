"""验证同一原生线树的分支不重复命名，格式、坐标和来源一致。"""

import copy
import unittest
from schematic_wiring import route_page
from schematic_native_wires import (
    model_attributes_for_move,
    preserve_channel_ids,
    wire_records,
)


class NativeWireTests(unittest.TestCase):
    def setUp(self):
        self.page = {
            "uuid": "s1",
            "bounds": [0, 0, 120, 100],
            "grid": 5,
            "pins": [
                {"ref": r, "pin": "1", "net": "S", "x": x, "y": y, "dir": d}
                for r, x, y, d in [
                    ("R", 0, 50, "R"),
                    ("U", 100, 50, "L"),
                    ("C", 50, 80, "D"),
                ]
            ],
        }
        self.wires = route_page(self.page)["wires"]

    def make(self, existing=(), uid="s1", **options):
        return wire_records(
            self.page,
            self.wires,
            existing,
            project_uuid="project",
            page_uuid=uid,
            source_fingerprint="fingerprint",
            **options,
        )

    def test_encoding_preserves_reviewed_carrier_and_position(self):
        for index, wire in enumerate(self.wires):
            wire["explicit_net"] = wire["show_name"] = index == 2
        self.wires[2]["label"] = [61.25, 71.5]
        before = copy.deepcopy(self.wires)
        labels = [b for h,b in self.make() if b.get("key") == "NET"]
        self.assertEqual([(b["x"], b["y"]) for b in labels], [(61.25, -71.5)])
        self.assertEqual(self.wires, before)

    def test_invalid_reviewed_position_or_duplicate_carriers_refused(self):
        for index, wire in enumerate(self.wires):
            wire["explicit_net"] = wire["show_name"] = index == 2
        for label in ([130, 71], [50, 65], [float("nan"), 80]):
            with self.subTest(label=label):
                self.wires[2]["label"] = label
                with self.assertRaises(ValueError):
                    self.make()
        self.wires[2]["label"] = [60, 70]
        self.wires[1].update(show_name=True, explicit_net=True, label=[67, 77])
        with self.assertRaises(ValueError):
            self.make()

    def test_checked_position_uses_supplied_font_metric(self):
        for index, wire in enumerate(self.wires):
            wire["explicit_net"] = wire["show_name"] = index == 2
        self.wires[2]["label"] = [60, 70]
        self.make(measure=lambda text, size: (12, 8))
        with self.assertRaises(ValueError):
            self.make(measure=lambda text, size: (30, 10))

    def test_native_label_cannot_overlap_an_unrelated_wire(self):
        self.page["pins"] = [
            {"ref": net+direction, "pin": "1", "net": net, "x": x, "y": y, "dir": direction}
            for net, y in (("PIEZO_ADC1", 40), ("SGND", 55))
            for x, direction in ((10, "R"), (100, "L"))
        ]
        self.wires = route_page(self.page)["wires"]
        for wire in self.wires:
            wire.update(show_name=True, explicit_net=True, label=[55, 55 if wire["net"] == "PIEZO_ADC1" else 65])
        with self.assertRaisesRegex(ValueError, "PIEZO_ADC1"):
            self.make()
        adc = next(w for w in self.wires if w["net"] == "PIEZO_ADC1")
        adc["label"] = [55, 32]
        rows = self.make()
        self.assertEqual(next((b["x"], b["y"]) for _, b in rows if b.get("value") == "PIEZO_ADC1"), (55, -32))

    def test_one_visible_name_per_continuous_tree(self):
        rows = self.make()
        owners = {h["id"] for h, b in rows if h["type"] == "WIRE"}
        self.assertEqual(len(owners), 1)
        self.assertEqual([b["value"] for h, b in rows if b.get("key") == "NET"], ["S"])
        lines = [b for h, b in rows if h["type"] == "LINE"]
        self.assertEqual(len(lines), 3)
        self.assertTrue(all(b["lineGroup"] in owners for b in lines))
        self.assertEqual(
            {b["parentId"] for h, b in rows if h["type"] == "ATTR"}, owners
        )
        self.assertIn(-80, [b[k] for b in lines for k in ("startY", "endY")])

    def test_separate_trees_of_same_power_net_have_different_owners(self):
        self.page["shared_nets"] = ["S"]
        self.page["pins"] = [
            dict(ref="X", pin="1", net="S", x=10, y=35, dir="R", block="left"),
            dict(ref="Y", pin="1", net="S", x=90, y=75, dir="L", block="right"),
        ]
        self.wires = route_page(self.page)["wires"]
        rows = self.make()
        owners = {h["id"] for h, b in rows if h["type"] == "WIRE"}
        self.assertEqual(len(owners), 2)
        self.assertEqual(
            {b["lineGroup"] for h, b in rows if h["type"] == "LINE"}, owners
        )
        names = [b for h, b in rows if b.get("key") == "NET"]
        self.assertEqual(len(names), 2)
        self.assertEqual({b["parentId"] for b in names}, owners)
        self.assertTrue(all(b["value"] == "S" for b in names))

    def test_refuse_wrong_page_or_reused_id(self):
        with self.assertRaises(ValueError):
            self.make(uid="other")
        with self.assertRaises(ValueError):
            self.make(existing=[self.make()[0][0]["id"]])

    def test_preserve_derived_channel_id_in_existing_empty_native_attribute(self):
        part = ({"type": "COMPONENT", "id": "u"}, {})
        attr = (
            {"type": "ATTR", "id": "old", "ticket": 7},
            {"parentId": "u", "key": "Channel ID", "value": None, "visible": False},
        )
        enet = {
            "components": {"u": {"props": {"Unique ID": "u", "Channel ID": "$5I123"}}}
        }
        updated = preserve_channel_ids([part, attr], enet, ["u"])
        self.assertEqual(len(updated), 2)
        self.assertEqual(updated[1], (attr[0], {**attr[1], "value": "$5I123"}))
        self.assertIsNone(attr[1]["value"])
        self.assertEqual(preserve_channel_ids(updated, enet, ["u"]), updated)
        with self.assertRaises(ValueError):
            preserve_channel_ids([part, attr, attr], enet, ["u"])
        with self.assertRaises(ValueError):
            preserve_channel_ids(
                [part, (attr[0], {**attr[1], "value": "$4I99"})], enet, ["u"]
            )
        with self.assertRaises(ValueError):
            preserve_channel_ids([part], enet, ["wrong"])
        missing = preserve_channel_ids([part], enet, ["u"])
        self.assertEqual(len(missing), 2)
        self.assertEqual(missing[1][1]["value"], "$5I123")

    def test_preserve_inherited_models_on_page_move(self):
        records = [({"type": "COMPONENT", "id": "u"}, {})]
        enet = {
            "components": {
                "u": {
                    "props": {
                        "Unique ID": "u",
                        "3D Model": "original",
                        "3D Model Transform": "1,0,0",
                    }
                }
            }
        }
        extra = model_attributes_for_move(records, enet, ["u"])
        self.assertEqual(len(extra), 2)
        self.assertEqual(model_attributes_for_move(records + extra, enet, ["u"]), [])
        extra[0][1]["value"] = "other"
        with self.assertRaises(ValueError):
            model_attributes_for_move(records + extra, enet, ["u"])
