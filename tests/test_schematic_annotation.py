"""覆盖原理图文字避障、不能排下的局部结果与输入保护。"""
import copy
import unittest
from schematic_annotation import annotate_page


def sample():
    page = {"bounds":[-100,-100,160,150],"obstacles":[[-100,70,160,150]],"parts":[
        {"ref":"U1","value":"双运放","body":[-10,-15,15,20]},
        {"ref":"R2","value":"100 kΩ","body":[45,-2,65,2]}]}
    routed = {"wires":[{"points":[[-70,25],[100,25]],"net":"参考电压","show_name":True,"label":[45,36]}]}
    return page, routed


def intersects(a,b):
    return a[0] <= b[2] and a[2] >= b[0] and a[1] <= b[3] and a[3] >= b[1]


class SchematicAnnotationTests(unittest.TestCase):
    def test_positions_in_clear_space_leave_original_objects_untouched(self):
        p,r = sample()
        saved = copy.deepcopy((p,r))
        metric = lambda text,size: (len(text)*size, size)
        result=annotate_page(p,r,measure=metric)
        self.assertTrue(result["completed"])
        self.assertEqual((p,r),saved)
        self.assertEqual(result["metrics"],"supplied")
        blocked = p["obstacles"]+[v["body"] for v in p["parts"]]+[[-70,25,100,25],[31,32.5,59,39.5]]
        for attrs in result["styles"].values():
            box = attrs["bounds"]
            self.assertFalse(any(intersects(box,b) for b in blocked))
            blocked.append(box)
            self.assertEqual(attrs["style"]["value"]["align"],"LEFT_MIDDLE")

    def test_crowded_page_preserves_all_original_strings(self):
        p,r = sample()
        p["obstacles"].append(p["bounds"].copy())
        result=annotate_page(p,r)
        self.assertFalse(result["completed"])
        self.assertEqual(result["styles"],{})
        lost=[(v["ref"],v["value"],v["action"]) for v in result["unplaced"]]
        self.assertEqual(lost,[("R2","100 kΩ","preserve_existing"),("U1","双运放","preserve_existing")])

    def test_global_budget_exhaustion_is_reported(self):
        p,r=sample()
        p["obstacles"].append(p["bounds"].copy())
        result=annotate_page(p,r,{"max_candidates":1})
        self.assertEqual(result["candidates_checked"],1)
        self.assertTrue(all("预算耗尽" in v["reason"] for v in result["unplaced"]))

    def test_empty_original_value_does_not_invent_parameter_text(self):
        p,r=sample()
        p["parts"][0]["value"]=""
        self.assertNotIn("value",annotate_page(p,r)["styles"]["U1"]["style"])

    def test_geometry_inputs_and_options_are_checked(self):
        p,r=sample()
        for options in ({"misspelled":1},{"gap":float("inf")},{"max_candidates_per_part":False}):
            with self.assertRaises(ValueError):
                annotate_page(p,r,options)
        with self.assertRaises(ValueError):
            annotate_page(p,r,measure=lambda s,z:(-1,z))
        r["wires"][0]["points"]=[(0,0),(1,1)]
        with self.assertRaises(ValueError):
            annotate_page(p,r)
        p["parts"].append(p["parts"][0])
        with self.assertRaises(ValueError):
            annotate_page(p,{"wires":[]})


if __name__=="__main__":
    unittest.main()
