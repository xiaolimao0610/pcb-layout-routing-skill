"""整封装球阵列扇出必须覆盖全部球位并保留真实端子和来源。"""
from copy import deepcopy
from pathlib import Path
import sys
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pcb_fanout import fanout_board, validate_options
import pcb_fanout
from pcb_pipeline_common import PipelineError, check_board, normalize_board
from pcb_routing import _Budget, _groups, RoutingFailure


def fixture(size=2):
    pads=[]
    for row in range(size):
        for col in range(size):
            pid=f"{row}:{col}"
            pads.append(dict(padId=pid,net="N"+pid,localX=(col-(size-1)/2)*60,
                             localY=(row-(size-1)/2)*60,layer=1,
                             shape="CIRCLE",diameter=18))
    extent=size*30
    part=dict(id="U",x=0,y=0,rotation=0,layer=1,
              body_polygon=[[-extent,-extent],[extent,-extent],[extent,extent],[-extent,extent]],pads=pads)
    return normalize_board(dict(units="mil",board_bbox=[-300,-300,300,300],components=[part],
        nets={p["net"]:[dict(compId="U",padId=p["padId"])] for p in pads},tags={},routes={},
        rules=dict(copper_layers=[1,15,16,2],minimum_trace_width=1,default_clearance=4,
                   board_edge_clearance=0,component_courtyard_margin=0,
                   default_via_drill=8,default_via_diameter=18,hole_to_hole_clearance=4),
        metadata={"source":{"uuid":"fixture","sourceFingerprint":"fingerprint"}}))


def options(size=2, **fanout):
    config=dict(enabled=True,max_seconds=45,max_candidates=20000,max_backtracks=32,
                packages=[dict(component="U",balls=[[f"{r}:{c}" for c in range(size)] for r in range(size)],
                               escape_layers=[15,16,2],escape_margin=45,via_offsets=[0.5])])
    config.update(fanout)
    return dict(width=6,grid_step=10,trace_layers=[1,15,16,2],allow_vias=True,
                max_seconds=60,max_net_seconds=60,fanout=config)


def run(board, cfg=None):
    cfg=cfg or options()
    stats={}
    result=fanout_board(board,cfg,stats,_Budget(cfg))
    return result,stats


class FanoutTests(unittest.TestCase):
    def test_finer_pitch_array_with_explicit_manufacturing_rules(self):
        board = fixture(3)
        pitch = 31.5
        for pad in board["components"][0]["pads"]:
            pad["localX"] *= pitch / 60
            pad["localY"] *= pitch / 60
            pad["diameter"] = pad["localW"] = pad["localH"] = 8
        extent = pitch * 1.5
        board["components"][0]["body_polygon"] = [[-extent,-extent],[extent,-extent],[extent,extent],[-extent,extent]]
        board["rules"].update(default_clearance=2, default_via_drill=6, minimum_drill=4,
                              default_via_diameter=12, hole_to_hole_clearance=2)
        cfg = options(3)
        cfg.update(width=4)
        cfg["fanout"]["packages"][0]["escape_margin"] = 20
        result, stats = run(board, cfg)
        self.assertEqual(len(stats["fanout"]["escaped"]), 9)
        self.assertEqual(check_board(result), [])

    def test_all_four_balls_have_real_outlets_and_source_preserved(self):
        board=fixture(); before=deepcopy(board)
        result,stats=run(board)
        self.assertEqual(len(stats["fanout"]["escaped"]),4)
        self.assertTrue(stats["fanout"]["completed"])
        self.assertFalse(stats["fanout"]["full_board_accepted"])
        self.assertEqual(result["nets"],board["nets"])
        self.assertEqual(result["components"],board["components"])
        self.assertEqual(result["metadata"]["source"],board["metadata"]["source"])
        self.assertEqual(check_board(result),[])
        self.assertEqual(board,before)
        self.assertTrue(all(v["layers"]==[1,15,16,2] for v in result["routes"]["vias"]))
        self.assertTrue(all(len(_groups(result,net))==1 for net in result["nets"]))

    def test_three_by_three_includes_inner_ball(self):
        result,stats=run(fixture(3),options(3))
        self.assertEqual(len(stats["fanout"]["escaped"]),9)
        self.assertEqual({e["pad"] for e in stats["fanout"]["escaped"]},
                         {f"{r}:{c}" for r in range(3) for c in range(3)})
        self.assertEqual(stats["fanout"]["escaped"][-1]["ring"],1)
        self.assertEqual(check_board(result),[])

    def test_incomplete_array_and_unsupported_technology_rejected(self):
        cfg=options(); cfg["fanout"]["packages"][0]["balls"][0][0]=None
        with self.assertRaises(PipelineError):run(fixture(),cfg)
        with self.assertRaises(PipelineError):validate_options({"technology":"microvia"})
        with self.assertRaises(PipelineError):validate_options({"unknown":1})

    def test_ground_deferred_per_ball_without_deleting_terminal(self):
        board=fixture(); cfg=options(); cfg.update(ground_strategy="plane_then_drc",ground_nets=["N0:0"])
        result,stats=run(board,cfg)
        self.assertEqual(len(stats["fanout"]["escaped"]),3)
        self.assertEqual(stats["fanout"]["deferred"][0]["net"],"N0:0")
        self.assertEqual(result["nets"],board["nets"])
        self.assertFalse(any(v["net"]=="N0:0" for v in result["routes"]["vias"]))

    def test_budget_failure_atomic_and_truthful(self):
        board=fixture(); before=deepcopy(board)
        with self.assertRaises(RoutingFailure) as caught:run(board,options(max_candidates=1))
        self.assertEqual(caught.exception.diagnostics["code"],"FANOUT_BUDGET")
        self.assertEqual(board,before)
        self.assertIn("未证明无解",str(caught.exception))

    def test_blocked_all_layers_does_not_accept_partial_package(self):
        board=fixture(); board["routes"]["keepouts"]=[dict(points=[[-290,-290],[290,-290],[290,290],[-290,290]],layers=[15,16,2])]
        before=deepcopy(board)
        with self.assertRaises(RoutingFailure):run(board)
        self.assertEqual(board,before)

    def test_via_in_pad_is_soft_preference_not_hard_ban(self):
        cfg=options()
        cfg["fanout"]["packages"][0]["via_offsets"]=[0,0.5]
        _,normal=run(fixture(),cfg)
        cfg["via_in_pad_penalty"]=100
        _,preferred=run(fixture(),cfg)
        self.assertGreater(sum(e["via_in_pad"] for e in normal["fanout"]["escaped"]),
                           sum(e["via_in_pad"] for e in preferred["fanout"]["escaped"]))
        cfg["fanout"]["packages"][0]["via_offsets"]=[0]
        _,necessary=run(fixture(),cfg)
        self.assertTrue(any(e["via_in_pad"] for e in necessary["fanout"]["escaped"]))

    def test_bottom_mirror_and_rotation_preserve_ball_ids(self):
        board=fixture()
        board["components"][0].update(layer=2,rotation=90)
        for pad in board["components"][0]["pads"]:pad["layer"]=2
        cfg=options(); cfg["fanout"]["packages"][0]["escape_layers"]=[15,16,1]
        result,stats=run(board,cfg)
        self.assertEqual(len(stats["fanout"]["escaped"]),4)
        self.assertEqual(check_board(result),[])
        self.assertEqual(result["components"],board["components"])

    def test_fanout_does_not_inject_board_preview_or_acceptance_flags(self):
        board=fixture()
        result,stats=run(board)
        self.assertNotIn("preview_only",result["metadata"])
        self.assertNotIn("full_board_accepted",result["metadata"])
        self.assertFalse(stats["fanout"]["full_board_accepted"])
        board["metadata"]["preview_only"]=True
        result,_=run(board)
        self.assertTrue(result["metadata"]["preview_only"])

    def test_last_geometric_call_cannot_overrun_any_budget_and_report_complete(self):
        original_commit=pcb_fanout._commit_escape
        for scope in ("local","total","net"):
            with self.subTest(scope=scope):
                board=fixture(); before=deepcopy(board); cfg=options(); stats={}
                clock=[time.monotonic()]; accepted=[0]
                if scope=="local":cfg["fanout"]["max_seconds"]=1
                elif scope=="total":cfg["max_seconds"]=1
                else:cfg["max_net_seconds"]=1
                def delayed_last_commit(*args,**kwargs):
                    candidate=original_commit(*args,**kwargs)
                    if candidate is not None:
                        accepted[0]+=1
                        if accepted[0]==4:clock[0]+=2
                    return candidate
                with patch("pcb_fanout.time.monotonic",side_effect=lambda:clock[0]):
                    budget=_Budget(cfg)
                    with patch("pcb_fanout._commit_escape",side_effect=delayed_last_commit):
                        with self.assertRaises(RoutingFailure) as caught:
                            fanout_board(board,cfg,stats,budget)
                self.assertEqual(accepted[0],4)
                self.assertEqual(caught.exception.diagnostics["code"],"FANOUT_BUDGET" if scope=="local" else "TIME_BUDGET")
                self.assertFalse(stats["fanout"]["completed"])
                self.assertTrue(stats["fanout"]["budget_exhausted"])
                self.assertEqual(board,before)

    def test_final_report_copy_is_inside_local_budget(self):
        board=fixture(); cfg=options(max_seconds=1); stats={}
        clock=[time.monotonic()]
        def delayed_report_copy(value):
            result=deepcopy(value)
            if isinstance(value,dict) and value.get("method")=="explicit_array_bounded_fanout":
                clock[0]+=2
            return result
        with patch("pcb_fanout.time.monotonic",side_effect=lambda:clock[0]):
            budget=_Budget(cfg)
            with patch("pcb_fanout.deepcopy",side_effect=delayed_report_copy):
                with self.assertRaises(RoutingFailure) as caught:
                    fanout_board(board,cfg,stats,budget)
        self.assertEqual(caught.exception.diagnostics["code"],"FANOUT_BUDGET")
        self.assertFalse(stats["fanout"]["completed"])


if __name__=="__main__":
    unittest.main()
