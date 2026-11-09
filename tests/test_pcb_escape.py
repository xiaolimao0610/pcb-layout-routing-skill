"""外围密脚引出的真实连通、硬边界、原铜与有限预算反例。"""
from copy import deepcopy
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pcb_escape import escape_board, validate_options
from pcb_pipeline_common import PipelineError, normalize_board, check_board
from pcb_routing import _Budget, _groups, RoutingFailure


def fixture():
    pads = [dict(padId=str(i), net="N"+str(i), localX=x, localY=y, layer=1,
                 shape="RECTANGLE", localW=12, localH=16) for i, (x, y) in enumerate([(-40,-20),(-40,20),(40,-20),(40,20)])]
    return normalize_board(dict(units="mil", board_bbox=[-150,-150,150,150], components=[dict(
        id="U", x=0, y=0, rotation=0, layer=1, body_polygon=[[-30,-40],[30,-40],[30,40],[-30,40]], pads=pads)],
        nets={p["net"]:[dict(compId="U",padId=p["padId"])] for p in pads}, tags={}, routes={},
        rules=dict(copper_layers=[1,15,16,2], minimum_trace_width=1, default_clearance=4,
                   board_edge_clearance=0, component_courtyard_margin=0,
                   default_via_drill=8, default_via_diameter=18, hole_to_hole_clearance=4),
        metadata={"source":{"uuid":"escape-fixture","sourceFingerprint":"original"}}))


def options(**overrides):
    cfg=dict(enabled=True,max_candidates=5000,groups=[
        dict(component="U",pads=["0","1"],sides=["left"],escape_layers=[1],provenance="显式测试引脚分组"),
        dict(component="U",pads=["2","3"],sides=["right"],escape_layers=[1],provenance="显式测试引脚分组")])
    cfg.update(overrides)
    return dict(width=6, grid_step=10, trace_layers=[1,15,16,2], allow_vias=True,
                max_seconds=30,max_net_seconds=15,escape=cfg)


def run(board,cfg=None):
    cfg=cfg or options(); stats={}
    return escape_board(board,cfg,stats,_Budget(cfg)),stats


class EscapeTests(unittest.TestCase):
    def test_reuse_requires_actual_geometry_and_partial_rerun_has_no_duplicates(self):
        original=fixture(); cfg=options()
        first,_=run(original,cfg)
        second,stats=run(first,cfg)
        self.assertEqual(second["routes"],first["routes"])
        self.assertEqual(len(stats["escape"]["reused"]),4)
        self.assertEqual(stats["escape"]["generated_outlets"],0)
        # 模拟局部布局只拆除 N0 本轮铜，其他三个出口必须复用实体证据。
        partial=deepcopy(first)
        partial["routes"]["traces"]=[t for t in partial["routes"]["traces"] if t["net"]!="N0"]
        rebuilt,stats=run(partial,cfg)
        self.assertEqual(len(stats["escape"]["reused"]),3)
        self.assertEqual(stats["escape"]["generated_outlets"],1)
        self.assertEqual(len(rebuilt["routes"]["traces"]),len(first["routes"]["traces"]))
        self.assertEqual(check_board(rebuilt),[])
        fake=deepcopy(original); fake["metadata"]["escape_candidate"]=first["metadata"]["escape_candidate"]
        rebuilt,stats=run(fake,cfg)
        self.assertEqual(len(stats["escape"]["reused"]),0)
        self.assertEqual(stats["escape"]["generated_outlets"],4)

    def test_reuse_full_stack_escape_does_not_insert_duplicate_holes(self):
        cfg=options()
        for group in cfg["escape"]["groups"]:group.update(escape_layers=[15],via_distances=[20],lateral_offsets=[0])
        first,_=run(fixture(),cfg)
        second,stats=run(first,cfg)
        self.assertEqual(second["routes"],first["routes"])
        self.assertEqual(len(stats["escape"]["reused"]),4)
        self.assertEqual(check_board(second),[])

    def test_backtracking_replaces_a_real_conflicting_exit_without_partial_output(self):
        # 候选顺序固定以重现狭通道冲突；是否相交及最终接回真实焊盘仍由原精检判定。
        import pcb_escape
        board=fixture(); cfg=options()
        cfg["escape"]["groups"]=cfg["escape"]["groups"][:1]
        def choices(current,task,options,check,limit):
            pad=task["pad"]; net=pad["net"]; pid=pad["padId"]
            y=-20 if pid=="0" else 20
            paths=([(-40,-20),(-80,20),(-100,20)],[(-40,-20),(-100,-20)]) if pid=="0" else ([(-40,20),(-100,20)],[(-40,20),(-100,20)])
            output=[]
            for points in paths:
                check()
                traces=[dict(net=net,layer=1,sx=a[0],sy=a[1],ex=b[0],ey=b[1],width=6) for a,b in zip(points,points[1:])]
                evidence=dict(component="U",pad=pid,net=net,exit_layer=1,exit_point=list(points[-1]),via_count=0)
                output.append((0,traces,[],evidence))
            return output
        before=deepcopy(board)
        with patch.object(pcb_escape,"_choices",choices):
            result,stats=run(board,cfg)
            self.assertEqual(stats["escape"]["backtracks"],1)
            self.assertEqual(check_board(result),[])
            self.assertEqual(stats["escape"]["escaped"][0]["exit_point"],[-100,-20])
            cfg["escape"]["max_backtracks"]=0
            with self.assertRaises(RoutingFailure) as caught:run(board,cfg)
            self.assertEqual(caught.exception.diagnostics["code"],"ESCAPE_BUDGET")
        self.assertEqual(board,before)

    def test_real_outlets_preserve_full_source_and_input(self):
        board=fixture(); original=deepcopy(board)
        result,stats=run(board)
        self.assertEqual(board,original)
        self.assertEqual(result["nets"],original["nets"])
        self.assertEqual(result["components"],original["components"])
        self.assertEqual(result["metadata"]["source"],original["metadata"]["source"])
        self.assertEqual(check_board(result),[])
        self.assertEqual(len(stats["escape"]["escaped"]),4)
        self.assertTrue(stats["escape"]["completed"])
        self.assertFalse(stats["escape"]["full_board_accepted"])
        self.assertTrue(all(len(_groups(result,net))==1 for net in result["nets"]))

    def test_full_stack_change_and_bottom_mirror(self):
        board=fixture(); component=board["components"][0]
        component.update(layer=2,rotation=90)
        for pad in component["pads"]: pad["layer"]=2
        cfg=options()
        for group in cfg["escape"]["groups"]:group.update(escape_layers=[15],via_distances=[20],lateral_offsets=[0])
        result,stats=run(board,cfg)
        self.assertEqual(check_board(result),[])
        self.assertTrue(result["routes"]["vias"])
        self.assertTrue(all(v["layers"]==[1,15,16,2] for v in result["routes"]["vias"]))
        self.assertEqual({e["exit_layer"] for e in stats["escape"]["escaped"]},{15})

    def test_original_copper_is_obstacle_and_never_removed(self):
        board=fixture()
        board["routes"]["traces"]=[dict(net="N2",layer=1,sx=40,sy=-20,ex=100,ey=-20,width=6)]
        result,_=run(board)
        self.assertEqual(result["routes"]["traces"][:1],board["routes"]["traces"])
        self.assertEqual(check_board(result),[])

    def test_board_edge_and_blocked_layer_return_no_partial(self):
        for kind in ("edge","keepout"):
            board=fixture(); cfg=options()
            if kind=="edge":
                for group in cfg["escape"]["groups"]:group["escape_margin"]=200
            else:
                for group in cfg["escape"]["groups"]:group["escape_layers"]=[15]
                board["routes"]["keepouts"]=[dict(points=[[-145,-145],[145,-145],[145,145],[-145,145]],layers=[15])]
            original=deepcopy(board)
            with self.assertRaises(RoutingFailure) as caught:run(board,cfg)
            self.assertIn("未找到",str(caught.exception))
            self.assertEqual(board,original)

    def test_budget_and_strict_schema(self):
        board=fixture(); original=deepcopy(board)
        with self.assertRaises(RoutingFailure) as caught:run(board,options(max_candidates=1))
        self.assertEqual(caught.exception.diagnostics["code"],"ESCAPE_BUDGET")
        self.assertEqual(board,original)
        with self.assertRaises(PipelineError):validate_options({"guess_nets":True})
        cfg=options(); cfg["escape"]["groups"][0]["pads"]=["imaginary"]
        with self.assertRaises(PipelineError):run(board,cfg)

    def test_ground_and_special_topology_remain_explicitly_deferred(self):
        cfg=options(); cfg.update(ground_strategy="plane_then_drc",ground_nets=["N0"],topology_orders={"N1":[]})
        result,stats=run(fixture(),cfg)
        self.assertEqual({e["net"] for e in stats["escape"]["deferred"]},{"N0","N1"})
        self.assertEqual({t["net"] for t in result["routes"]["traces"]},{"N2","N3"})
        self.assertEqual(len(result["nets"]),4)

    def test_differential_and_length_groups_are_never_escaped_as_single_ended(self):
        board=fixture()
        board["metadata"]["pipeline_constraints"]=[
            dict(operator="CHANNEL_SPEC",net="N0",diff_pair=dict(p="N0",n="N1",gap=8,width=6)),
            dict(operator="CHANNEL_SPEC",net="N2",length_group=dict(name="clock",tolerance=2))]
        result,stats=run(board)
        self.assertEqual({e["net"] for e in stats["escape"]["deferred"]},{"N0","N1","N2"})
        self.assertEqual({t["net"] for t in result["routes"]["traces"]},{"N3"})
        self.assertEqual(result["nets"],board["nets"])


if __name__=="__main__":unittest.main()
