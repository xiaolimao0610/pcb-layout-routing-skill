"""原生原理图网表的断线、换脚、器件身份及未接脚反例。"""

import copy
import json
import tempfile
import unittest
from pathlib import Path

from schematic_netlist import compare, main, unique_keys


def fixture():
    pins = {"1": {"number": "1", "net": "SENSE"},
            "2": {"number": "2", "net": "PGND"}, "3": {"number": "3", "net": ""}}
    return {"components": {"id1": {"props": {"Designator": "U1", "Unique ID": "id1",
                                             "Footprint": "QFN"}, "pinInfoMap": pins}},
            "version": "2.0.0"}


class SchematicNetlistTests(unittest.TestCase):
    def test_order_insensitive(self):
        old = fixture()
        new = copy.deepcopy(old)
        pins = new["components"]["id1"]["pinInfoMap"]
        new["components"]["id1"]["pinInfoMap"] = dict(reversed(list(pins.items())))
        report = compare(old, new)
        self.assertTrue(report["passed"])
        self.assertEqual(report["before"]["unconnected_pins"], 1)

    def test_disconnect_bridge_or_swapped_pins(self):
        for a, b in [("", "PGND"), ("PGND", "PGND"), ("PGND", "SENSE")]:
            old = fixture()
            new = copy.deepcopy(old)
            pins = new["components"]["id1"]["pinInfoMap"]
            pins["1"]["net"], pins["2"]["net"] = a, b
            report = compare(old, new)
            self.assertFalse(report["passed"])
            self.assertIn("net", {e["scope"] for e in report["differences"]})

    def test_missing_empty_pin_or_assigned_nc(self):
        for missing in (False, True):
            old = fixture()
            new = copy.deepcopy(old)
            pins = new["components"]["id1"]["pinInfoMap"]
            if missing:
                del pins["3"]
            else:
                pins["3"]["net"] = "SENSE"
            self.assertFalse(compare(old, new)["passed"])

    def test_identity_bom_and_rules(self):
        old = fixture()
        new = copy.deepcopy(old)
        new["components"]["id2"] = new["components"].pop("id1")
        new["components"]["id2"]["props"]["Unique ID"] = "id2"
        self.assertFalse(compare(old, new)["passed"])
        for field in ("Footprint", "Add into BOM"):
            new = copy.deepcopy(old)
            new["components"]["id1"]["props"][field] = "changed"
            self.assertFalse(compare(old, new)["passed"])
        new = copy.deepcopy(old)
        new["netClass"] = {}
        self.assertFalse(compare(old, new)["passed"])

    def test_invalid_keys_and_missing_net(self):
        with self.assertRaises(ValueError):
            json.loads('{"1":{}, "1":{}}', object_pairs_hook=unique_keys)
        new = fixture()
        del new["components"]["id1"]["pinInfoMap"]["3"]["net"]
        with self.assertRaises(ValueError):
            compare(new, new)

    def test_report_cannot_overwrite_input(self):
        with tempfile.TemporaryDirectory() as root:
            source = Path(root) / "source.enet"
            data = json.dumps(fixture()).encode()
            source.write_bytes(data)
            self.assertEqual(main([str(source), str(source), "--report", str(source)]), 2)
            self.assertEqual(source.read_bytes(), data)
