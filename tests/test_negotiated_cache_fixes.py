"""协商缓存、方向状态及预算的独立几何回归，不访问真实工程。"""
from copy import deepcopy
import math
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import numpy as np
from shapely.geometry import box

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pcb_negotiated as negotiated
import pcb_routing as routing
from pcb_pipeline_common import CollisionModel, check_board, normalize_board


def board_fixture():
    """返回四端、双层、明确工艺的小板；所有坐标只用于单元测试。"""
    components, pins = [], []
    for index, (x, y) in enumerate(((20, 20), (200, 20), (20, 120), (200, 120))):
        name = str(index)
        components.append(dict(id=name, x=x, y=y, rotation=0, layer=1,
            width=8, height=8, pads=[dict(padId="1", net="N", localX=0, localY=0,
              localW=8, localH=8, shape="RECTANGLE")]))
        pins.append(dict(compId=name, padId="1"))
    return normalize_board(dict(units="mil", board_bbox=[0, 0, 220, 140],
        components=components, nets={"N": pins}, tags={}, routes=dict(traces=[], vias=[], keepouts=[]),
        metadata={"board_usage": "test_only"},
        rules=dict(copper_layers=[1, 2], minimum_trace_width=2, default_clearance=2,
                   clearance_matrix=[], board_edge_clearance=1, component_courtyard_margin=0)))


def context(board=None, **overrides):
    board = board or board_fixture()
    options = routing.algorithm_options(dict(method="negotiated", width=2, grid_step=10,
        max_seconds=30, max_net_seconds=20, negotiated_grid_cache=True,
        steiner_max_rounds=0, **overrides))
    model, budget = CollisionModel(board), routing._Budget(options)
    settings = routing._net_options(board, "N", options)
    resources = negotiated._Resources(model, {"N": settings}, options["grid_step"],
                                      options["occupancy_max_cells"], budget)
    return board, options, model, budget, settings, resources, {}


def get_grid(ctx, board=None, blocked=None, net="N", settings=None):
    original, options, model, budget, spec, resources, stats = ctx
    return negotiated._grid_for_search(CollisionModel(board) if board else model,
        net, settings or spec, options, stats, budget, resources, blocked or (set(), set()))


def six_net_context(limit=100000):
    """返回六个真实网络的缓存测试上下文，预算覆盖两轮循环访问。"""
    board = board_fixture()
    board["components"], board["nets"] = [], {}
    for index in range(6):
        net = f"N{index}"
        board["nets"][net] = []
        for end, x in enumerate((20,200)):
            name = f"C{index}_{end}"
            board["components"].append(dict(id=name,x=x,y=15+20*index,rotation=0,layer=1,width=8,height=8,
                pads=[dict(padId="1",net=net,localX=0,localY=0,localW=8,localH=8,shape="RECTANGLE")]))
            board["nets"][net].append(dict(compId=name,padId="1"))
    board = normalize_board(board)
    options = routing.algorithm_options(dict(method="negotiated",width=2,grid_step=10,
        max_seconds=30,max_net_seconds=20,negotiated_grid_cache=True,occupancy_max_cells=limit))
    model, budget = CollisionModel(board), routing._Budget(options)
    specs = {net:routing._net_options(board,net,options) for net in board["nets"]}
    resources = negotiated._Resources(model,specs,10,limit,budget)
    return board,options,model,budget,specs,resources,{}


class NegotiatedCacheTests(unittest.TestCase):
    def test_same_input_reused_and_rejection_masks_are_independent(self):
        ctx = context()
        blocked = ({(10, 7, 1)}, {(10, 7)})
        first = get_grid(ctx, blocked=blocked)
        self.assertFalse(first.trace_free(1, 10, 7))
        self.assertFalse(first.via_free(10, 7))
        second = get_grid(ctx)
        self.assertTrue(second.trace_free(1, 10, 7))
        self.assertTrue(second.via_free(10, 7))
        self.assertEqual(ctx[-1]["occupancy_builds"], 1)
        self.assertEqual(ctx[-1]["negotiated_grid_cache_hits"], 1)

    def test_append_and_remove_same_net_hole_updates_actual_via_mask(self):
        ctx = context()
        original = get_grid(ctx)
        changed = deepcopy(ctx[0])
        changed["routes"]["vias"].append(dict(net="N", x=100, y=70, hole=12, diameter=24, layers=[1,2]))
        self.assertTrue(original.via_free(10, 7))
        updated = get_grid(ctx, changed)
        self.assertFalse(updated.via_free(10, 7))
        fresh_ctx = context(changed)
        fresh = get_grid(fresh_ctx)
        for layer in updated.layers:
            np.testing.assert_array_equal(updated.blocked[layer], fresh.blocked[layer])
        np.testing.assert_array_equal(updated.via_blocked, fresh.via_blocked)
        restored = get_grid(ctx, ctx[0])
        self.assertTrue(restored.via_free(10, 7))
        self.assertEqual(ctx[-1]["occupancy_builds"], 1)
        self.assertEqual(ctx[-1]["occupancy_updates"], 2)

    def test_foreign_copper_is_not_hidden_by_cache(self):
        ctx = context()
        get_grid(ctx)
        changed = deepcopy(ctx[0])
        changed["routes"]["traces"].append(dict(net="X", layer=1, sx=80, sy=70, ex=120, ey=70, width=2))
        grid = get_grid(ctx, changed)
        self.assertFalse(grid.trace_free(1, 10, 7))
        self.assertEqual(ctx[-1]["occupancy_updates"], 1)

    def test_rules_tags_keepouts_and_network_force_separate_entries(self):
        for change in (
            lambda b: b["rules"].update(default_clearance=7),
            lambda b: b["tags"].update(net_tags={"N": {"min_width": 3}}),
            lambda b: b["routes"]["keepouts"].append(dict(points=[[80,60],[120,60],[120,80],[80,80]], layers=[1])),
        ):
            ctx = context()
            get_grid(ctx)
            changed = deepcopy(ctx[0])
            change(changed)
            get_grid(ctx, changed)
            self.assertEqual(ctx[-1]["occupancy_builds"], 2)
        ctx = context()
        get_grid(ctx)
        get_grid(ctx, net="OTHER")
        self.assertEqual(ctx[-1]["occupancy_builds"], 2)
        changed_spec = dict(ctx[4], width=3)
        get_grid(ctx, settings=changed_spec)
        self.assertEqual(ctx[-1]["occupancy_builds"], 3)

    def test_cache_has_resident_cell_limit(self):
        ctx = context(occupancy_max_cells=1100)
        for index in range(7):
            get_grid(ctx, net=str(index))
        self.assertEqual(len(ctx[5].grid_cache), 1)
        self.assertLessEqual(sum(v[0].cell_count for v in ctx[5].grid_cache.values()), 1100)
        self.assertGreater(ctx[-1]["negotiated_grid_cache_evictions"], 0)

    def test_six_real_networks_survive_two_rounds_when_cells_fit(self):
        ctx = six_net_context()
        original = deepcopy(ctx[0])
        for _ in range(2):
            for net, spec in ctx[4].items():
                get_grid(ctx,net=net,settings=spec)
        self.assertEqual(ctx[-1]["occupancy_builds"],6)
        self.assertEqual(ctx[-1]["negotiated_grid_cache_hits"],6)
        self.assertEqual(ctx[-1].get("negotiated_grid_cache_evictions",0),0)
        self.assertEqual(len(ctx[5].grid_cache),6)
        self.assertEqual(ctx[-1]["negotiated_grid_cache_cells"],6210)
        self.assertEqual(ctx[0],original)

    def test_insufficient_capacity_evicts_lru_and_never_exceeds_cells(self):
        ctx = six_net_context(limit=3105)  # 三张实际1035单元栅格。
        for net in ("N0","N1","N2","N0","N3"):
            get_grid(ctx,net=net,settings=ctx[4][net])
            self.assertLessEqual(sum(v[0].cell_count for v in ctx[5].grid_cache.values()),3105)
        self.assertEqual([key[0] for key in ctx[5].grid_cache],["N2","N0","N3"])
        self.assertEqual(ctx[-1]["occupancy_builds"],4)
        self.assertEqual(ctx[-1]["negotiated_grid_cache_hits"],1)
        self.assertEqual(ctx[-1]["negotiated_grid_cache_evictions"],1)
        for _ in range(2):
            for net,spec in ctx[4].items():
                get_grid(ctx,net=net,settings=spec)
                self.assertLessEqual(ctx[-1]["negotiated_grid_cache_cells"],3105)

    def test_hit_under_reduced_budget_evicts_other_resident_entries(self):
        ctx = six_net_context(limit=1100)
        get_grid(ctx,net="N0",settings=ctx[4]["N0"])
        ctx[1]["occupancy_max_cells"] = 100000
        for net in ("N1","N2","N3","N4","N5"):
            get_grid(ctx,net=net,settings=ctx[4][net])
        self.assertEqual(len(ctx[5].grid_cache),6)
        ctx[1]["occupancy_max_cells"] = 1100
        get_grid(ctx,net="N0",settings=ctx[4]["N0"])
        self.assertEqual(ctx[-1]["occupancy_builds"],6)
        self.assertEqual(ctx[-1]["negotiated_grid_cache_hits"],1)
        self.assertEqual(len(ctx[5].grid_cache),1)
        self.assertEqual(ctx[-1]["negotiated_grid_cache_cells"],1035)

    def test_global_budget_propagates_on_hit_and_failed_update(self):
        ctx = context()
        get_grid(ctx)
        with patch.object(ctx[3], "check", side_effect=routing.RoutingFailure("TIME_BUDGET", "测试预算耗尽")):
            with self.assertRaises(routing.RoutingFailure) as raised:
                get_grid(ctx)
        self.assertEqual(raised.exception.diagnostics["code"], "TIME_BUDGET")
        changed = deepcopy(ctx[0])
        changed["routes"]["vias"].append(dict(net="N",x=100,y=70,hole=12,diameter=24,layers=[1,2]))
        with patch.object(negotiated.OccupancyGrid, "update", side_effect=routing.RoutingFailure("TIME_BUDGET", "测试预算耗尽")):
            with self.assertRaises(routing.RoutingFailure):
                get_grid(ctx, changed)
        self.assertEqual(len(ctx[5].grid_cache), 0)

    def test_multiterminal_routing_has_same_copper_and_fewer_builds(self):
        results = []
        for enabled in (False, True):
            options = dict(method="negotiated", width=2, grid_step=10, allow_vias=False,
                           negotiated_grid_cache=enabled, steiner_max_rounds=0,
                           max_seconds=30, max_net_seconds=20)
            routed, stats = routing.route_board(board_fixture(), options)
            self.assertEqual(check_board(routed), [])
            results.append((routed["routes"], stats))
        self.assertEqual(results[0][0], results[1][0])
        self.assertGreater(results[0][1]["occupancy_builds"], results[1][1]["occupancy_builds"])
        self.assertGreater(results[1][1]["negotiated_grid_cache_hits"], 0)

    def test_positive_bend_path_commits_and_zero_keeps_default(self):
        board = board_fixture()
        for bend in (0, 2):
            routed, stats = routing.route_board(board, dict(method="negotiated", width=2,
                grid_step=10, diagonal=False, allow_vias=False, negotiated_bend_penalty=bend,
                max_seconds=30, max_net_seconds=20, steiner_max_rounds=0))
            self.assertEqual(check_board(routed), [])
            self.assertTrue(stats["negotiated_last_path_cost"] >= 0)

    def test_equal_length_corridors_prefer_two_turns_over_four(self):
        # 固定可走格形成两条等长走廊；方向状态不能只用位置合并。
        cells = {(1,1),(1,2),(1,3),(1,4),(1,5),(2,2),(2,3),(2,4),(2,5),(2,6),
                 (3,1),(3,2),(3,4),(3,5),(3,6),(4,2),(4,4),(4,5),(4,6),
                 (5,1),(5,2),(5,4),(5,5),(6,1),(6,3),(6,4),(6,5),(6,6)}
        class CorridorGrid:
            def trace_free(self, layer, x, y):
                return layer == 1 and (x,y) in cells
            def via_free(self, x, y):
                return False
        _, options, model, _, settings, resources, _ = context(
            diagonal=False, allow_vias=False, max_expansions=10000)
        lengths, turns, costs = [], [], []
        for penalty in (0,4):
            opt = dict(options, negotiated_bend_penalty=penalty)
            stats = {}
            with patch.object(negotiated, "_grid_for_search", return_value=CorridorGrid()), \
                 patch.object(resources, "footprint", return_value=set()):
                traces, _ = negotiated._search_impl(model,"N",dict(steiner=(10,10,1)),
                    dict(steiner=(60,60,1)),settings,opt,stats,routing._Budget(opt),
                    resources,1,(set(),set()))
            lengths.append(sum(math.hypot(t["ex"]-t["sx"],t["ey"]-t["sy"]) for t in traces))
            turns.append(len(traces)-1)
            costs.append(stats["negotiated_last_path_cost"])
        self.assertEqual(lengths, [100,100])
        self.assertEqual(turns, [4,2])
        self.assertEqual(costs, [100,108])

    def test_cache_hit_still_obeys_local_grid_budget(self):
        ctx = context()
        get_grid(ctx)
        ctx[1]["occupancy_max_seconds"] = 1e-12
        with self.assertRaises(routing.RoutingFailure) as raised:
            get_grid(ctx)
        self.assertEqual(raised.exception.diagnostics["code"], "OCCUPANCY_BUDGET")
        self.assertEqual(len(ctx[5].grid_cache), 0)

    def test_ordered_forbidden_geometry_cannot_reuse_old_mask(self):
        _, options, model, budget, settings, resources, stats = context()
        for forbidden in ({}, {1: box(90,60,110,80)}):
            view = negotiated._grid_for_search(SimpleNamespace(base=model, forbidden=forbidden),
                "N",settings,options,stats,budget,resources,(set(),set()))
            self.assertEqual(view.trace_free(1,10,7), not bool(forbidden))
        self.assertEqual(stats["occupancy_builds"], 2)

    def test_board_shape_change_rebuilds_and_rejects_new_outside(self):
        ctx = context()
        self.assertTrue(get_grid(ctx).trace_free(1,15,7))
        board = deepcopy(ctx[0])
        board["board_bbox"] = [0,0,140,140]
        self.assertFalse(get_grid(ctx,board).trace_free(1,15,7))
        self.assertEqual(ctx[-1]["occupancy_builds"], 2)


if __name__ == "__main__":
    unittest.main()
