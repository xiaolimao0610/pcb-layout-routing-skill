"""从原生图元区别间断标签、沿线直连、异网误接及引脚漏采。"""
import json
import tempfile
import unittest
from pathlib import Path

from schematic_readback import audit_snapshot, pin_on_segment


def fixture(root, split=False, wrong_net=False, omit_last=False):
    rows, comps, geometry = [], {}, []
    def append(kind, uid, body):
        rows.append(json.dumps({"type":kind,"id":uid}) + "||" + json.dumps(body))
    for i, x in enumerate((0,30), 1):
        name = f"J{i}"
        comps[name] = {"props":{"Designator":name,"Unique ID":name}, "pinInfoMap":{"1":{"net":"A"}}}
        append("COMPONENT", name, {"x":x, "y":0, "rotation":0, "isMirror":False})
        append("ATTR", f"d{i}", {"key":"Designator","value":name,"parentId":name})
        if i == 1 or not omit_last:
            geometry.append({"ComponentType":"part","Designator":name,"PrimitiveId":name,
                             "X":x, "Y":0, "Rotation":0, "Mirror":False, "pins":[
                {"PinNumber":"1","PrimitiveId":name+"-e1","X":x,"Y":0,"NoConnected":False}]})
    for i,(x,y) in enumerate([(0,5),(25,30)] if split else [(0,30)]):
        w = f"w{i}"
        append("WIRE",w,{})
        append("LINE",f"l{i}",{"lineGroup":w,"startX":x,"startY":0,"endX":y,"endY":0})
        append("ATTR",f"a{i}",{"key":"NET","value":"B" if wrong_net else "A","valueVisible":split,"parentId":w})
    (root/"netlist.enet").write_text(json.dumps({"components":comps}))
    (root/"sheet-geometry.json").write_text(json.dumps(geometry))
    (root/"sheet.esch").write_text("|\n".join(rows))


class ReadbackTests(unittest.TestCase):
    def test_removed_native_component_even_when_geometry_and_netlist_agree(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)
            fixture(path)
            source = path / "sheet.esch"
            lines = source.read_text().splitlines()
            # 模拟丢失器件及其属性，保留上一批次的同页实测引脚和网表。
            source.write_text("\n".join(line for line in lines
                if json.loads(line.split("||")[0])["id"] not in {"J2", "d2"}))
            with self.assertRaisesRegex(ValueError, "J2"):
                audit_snapshot(path)

    def test_stale_pose_and_netlist_uid_are_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)
            fixture(path)
            geometry = path / "sheet-geometry.json"
            parts = json.loads(geometry.read_text())
            parts[1]["X"] += 5
            geometry.write_text(json.dumps(parts))
            with self.assertRaisesRegex(ValueError, "J2"):
                audit_snapshot(path)
            fixture(path)
            enet = path / "netlist.enet"
            board = json.loads(enet.read_text())
            board["components"]["J3"] = board["components"].pop("J2")
            board["components"]["J3"]["props"]["Unique ID"] = "J3"
            enet.write_text(json.dumps(board))
            with self.assertRaisesRegex(ValueError, "J2"):
                audit_snapshot(path)

    def test_geometry_from_another_pin_or_extra_page_refused(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)
            fixture(path)
            geometry = path / "sheet-geometry.json"
            parts = json.loads(geometry.read_text())
            parts[0]["pins"][0]["PrimitiveId"] = "J2-e7"
            geometry.write_text(json.dumps(parts))
            with self.assertRaisesRegex(ValueError, "J1"):
                audit_snapshot(path)
            fixture(path)
            (path/"old-geometry.json").write_bytes(geometry.read_bytes())
            with self.assertRaisesRegex(ValueError, "逐页"):
                audit_snapshot(path)

    def test_native_rotated_pin_floating_point_tail(self):
        segment = ((360,640),(365,640))
        self.assertTrue(pin_on_segment((359.99999999999994,640),segment))
        self.assertFalse(pin_on_segment((359.9999999,640),segment))
        self.assertFalse(pin_on_segment((360,640.0000001),segment))

    def test_named_stubs_are_not_physical_wires_between_parts(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)
            fixture(path, split=True)
            self.assertEqual(audit_snapshot(path)["totals"]["pins_direct_to_other_component"],0)
            fixture(path)
            result = audit_snapshot(path)["totals"]
            self.assertEqual(result["pins_direct_to_other_component"],2)
            self.assertEqual(result["issues"],0)

    def test_bridge_fails_and_missing_geometry_refused(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)
            fixture(path, wrong_net=True)
            self.assertGreater(audit_snapshot(path)["totals"]["issues"],0)
            fixture(path, omit_last=True)
            with self.assertRaises(ValueError):
                audit_snapshot(path)

    def test_unnamed_branch_inherits_only_from_continuous_geometry(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)
            fixture(path, split=True)
            source = path/"sheet.esch"
            rows = []
            for line in source.read_text().splitlines():
                h,b = map(json.loads,line.removesuffix("|").split("||"))
                if h["id"] == "a1":
                    continue
                if h["id"] == "l1":
                    b["startX"] = 5
                rows.append(json.dumps(h)+"||"+json.dumps(b))
            source.write_text("|\n".join(rows))
            result = audit_snapshot(path)["totals"]
            self.assertEqual(result["issues"],0)
            self.assertEqual(result["pins_direct_to_other_component"],2)
            board = json.loads((path/"netlist.enet").read_text())
            board["components"]["J2"]["pinInfoMap"]["1"]["net"] = "B"
            (path/"netlist.enet").write_text(json.dumps(board))
            self.assertGreater(audit_snapshot(path)["totals"]["issues"],0)
