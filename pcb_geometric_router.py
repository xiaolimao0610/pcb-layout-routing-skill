"""有预算的连续坐标可见图候选；实体检查与最终提交仍由原流程负责。"""
from __future__ import annotations

import heapq
import math
import time
from collections import deque

from shapely.geometry import Point, box
from shapely.ops import nearest_points

from pcb_pipeline_common import PipelineError, finite, strict_options


_DEFAULTS = dict(enabled=False, max_seconds=10.0, max_nodes=512,
                 max_edges=12000, max_anchors=16, margin=100.0, boundary_samples=64,
                 refine_factors=[2.0, 4.0], max_refine_expansions=10000,
                 sampling="legacy", bend_penalty=0.0)


def validate_options(value=None):
    """输入嵌套 geometry_search 配置，返回默认关闭的严格副本。

    未知字段、布尔类型误用、非有限数值及超出预算范围抛 PipelineError。
    不读取或改变 PCB，也不改变旧路由默认值。
    """
    result = dict(_DEFAULTS, **strict_options(value, _DEFAULTS, "geometry_search"))
    if type(result["enabled"]) is not bool:
        raise PipelineError("geometry_search.enabled 必须为布尔值")
    if result["sampling"] not in ("legacy", "balanced"):
        raise PipelineError("geometry_search.sampling 必须为 legacy 或 balanced")
    result["bend_penalty"] = finite(result["bend_penalty"], "geometry_search.bend_penalty", minimum=0)
    for name, low, high in (("max_nodes", 2, 8192), ("max_edges", 1, 2000000),
                            ("max_anchors", 1, 256), ("boundary_samples", 4, 512),
                            ("max_refine_expansions", 1, 100000)):
        if type(result[name]) is not int or not low <= result[name] <= high:
            raise PipelineError(f"geometry_search.{name} 必须为 {low}..{high} 的整数")
    for name in ("max_seconds", "margin"):
        result[name] = finite(result[name], f"geometry_search.{name}", positive=True)
    factors = result["refine_factors"]
    if not isinstance(factors, list) or len(factors) > 3:
        raise PipelineError("geometry_search.refine_factors 需要最多3个细化倍数，空列表关闭细化")
    result["refine_factors"] = [finite(v, "geometry_search.refine_factors", positive=True) for v in factors]
    if (any(not 1 < v <= 16 for v in result["refine_factors"])
            or len(set(result["refine_factors"])) != len(result["refine_factors"])):
        raise PipelineError("geometry_search.refine_factors 需要不重复且大于1、不超过16的有限倍数")
    return result


class _SearchBudget:
    """共享父预算并限制本次图的节点、精确边检查数和耗时。"""
    def __init__(self, parent, config, record):
        self.parent, self.config, self.record = parent, config, record
        self.started = time.monotonic()

    def check(self, edge=False):
        from pcb_routing import RoutingFailure
        self.parent.check()
        elapsed = time.monotonic() - self.started
        if elapsed >= self.config["max_seconds"]:
            self.record["budget_exhausted"] = True
            raise RoutingFailure("GEOMETRY_BUDGET", "几何候选时间预算内未找到，未证明无解",
                                 budget_scope="geometry_seconds", geometry=self.record)
        if edge:
            if self.record["edge_checks"] >= self.config["max_edges"]:
                self.record["budget_exhausted"] = True
                raise RoutingFailure("GEOMETRY_BUDGET", "几何候选边检查预算内未找到，未证明无解",
                                     budget_scope="geometry_edges", geometry=self.record)
            self.record["edge_checks"] += 1


def _parts(shape):
    if shape.geom_type == "Polygon":
        if not shape.is_empty:
            yield shape
    elif hasattr(shape, "geoms"):
        for part in shape.geoms:
            yield from _parts(part)


def _anchors(group, other, allowed, limit, budget):
    """从真实同网铜岛取有界接入点，不用端子中心替代全部已有铜。"""
    result, seen = [], set()
    for layer in allowed:
        if layer not in group["layers"]:
            continue
        for shape in _parts(group["layers"][layer]):
            budget.check()
            points = [shape.representative_point()]
            for other_shape in other["layers"].values():
                budget.check()
                points.append(nearest_points(shape, other_shape)[0])
            for point in points:
                node = (float(point.x), float(point.y), layer)
                if node not in seen:
                    result.append(node)
                    seen.add(node)
                    if len(result) >= limit:
                        return result
    return result


def _boundaries(model, net, layer, width, region, budget):
    """生成候选边界而非放行障碍；所有实际障碍仍参与 trace_ok。"""
    radius = width / 2
    epsilon = max(model.rules.curve_tolerance * 4, 1e-6)
    physical = getattr(model, "base", model)
    kind = physical.net_type(net)
    for item in physical.copper:
        budget.check()
        if layer in item["layers"] and item["net"] != net and item["outer"].intersects(region):
            gap = model.rules.get_clearance(kind, physical.net_type(item["net"]))
            yield item["outer"].buffer(radius + gap + epsilon, join_style=2)
    for item in physical.drills:
        budget.check()
        if (layer in item["layers"] and item["outer"].intersects(region)
                and (not item["plated"] or item["net"] != net)):
            gap = 0 if item["plated"] else model.rules.npth_clearance
            yield item["outer"].buffer(radius + gap + epsilon, join_style=2)
    for shape, layers, gap, nets, types in physical.keepouts:
        budget.check()
        if (layer in layers and shape.intersects(region)
                and (nets is None or net in nets) and (types is None or kind in types)):
            yield shape.buffer(radius + gap + epsilon, join_style=2)
    # TOPOLOGY_ORDER 的同网禁触分支也提供候选边界，不能绕过包装模型精检。
    forbidden = getattr(model, "forbidden", {}).get(layer)
    if forbidden is not None and not forbidden.is_empty:
        yield forbidden.buffer(radius + epsilon, join_style=2)
    yield model.board.buffer(-(radius + model.rules.board_edge_clearance + epsilon), join_style=2)


def _direction(a, b):
    """输入非退化线段端点，返回归一化方向；零长段返回 None。"""
    length = math.dist(a, b)
    return tuple(round((b[i]-a[i])/length, 10) for i in (0, 1)) if length else None


def _edge_choices(model, net, first, second, settings, options, budget, incoming=None, penalty=0):
    """输入节点、入射方向和转弯代价，返回各出口方向的最便宜精检候选。

    输出项为(总代价, 走线, 过孔, 出射方向, 转弯数)。换层孔重置平面方向；
    无有限候选返回空列表，预算异常原样上抛，不放松实体检查。
    """
    from pcb_routing import _octilinear_doglegs, _via_preference_cost
    a, b = first[:2], second[:2]
    if first[2] != second[2]:
        stack = tuple(model.rules.copper_layers)
        if (a != b or not settings["vias"] or len(stack) < 2
                or not set(stack).issubset(settings["via_layers"])):
            return []
        budget.check(edge=True)
        if not model.via_ok(net, *a, settings["hole"], settings["diameter"], stack):
            return []
        via = dict(net=net, x=a[0], y=a[1], hole=settings["hole"],
                   diameter=settings["diameter"], layers=list(stack))
        cost = settings["via_cost"] + _via_preference_cost(model, *a, settings, stack)
        return [(cost, [], [via], None, 0)]
    if a == b:
        return [(0.0, [], [], incoming, 0)]
    choices = {}
    for points in _octilinear_doglegs(a, b, diagonal=options.get("diagonal", True), width=settings["width"]):
        traces, cost, heading, bends = [], 0.0, incoming, 0
        for p, q in zip(points, points[1:]):
            if p == q:
                continue
            budget.check(edge=True)
            if not model.trace_ok(net, first[2], *p, *q, settings["width"]):
                break
            traces.append(dict(net=net, layer=first[2], sx=p[0], sy=p[1],
                               ex=q[0], ey=q[1], width=settings["width"]))
            direction = _direction(p, q)
            if heading is not None and heading != direction:
                cost += penalty
                bends += 1
            heading = direction
            cost += math.dist(p, q) * settings["alpha"]
        else:
            # 非零罚分时保留不同到达方向，避免合并掉后续可能更优的路径。
            key = heading if penalty else None
            if key not in choices or cost < choices[key][0]:
                choices[key] = (cost, traces, [], heading, bends)
    return list(choices.values())


def _edge(model, net, first, second, settings, options, budget):
    """输入两节点和原规则，返回最短精检候选三元组或 None；预算异常上抛。"""
    choices = _edge_choices(model, net, first, second, settings, options, budget)
    return min(choices, key=lambda item: item[0])[:3] if choices else None


def _ring_points(ring, count, region, budget):
    """输入边界及点数上限，按分散顺序产出区内坐标；预算异常上抛。"""
    coords = ring.coords
    total = len(coords)-1
    count = min(total, count)
    # 位反序使最初少量样本覆盖整圈，而非只覆盖环的起始短弧。
    bits = max(1, (count-1).bit_length())
    for index in sorted(range(count), key=lambda i: int(f"{i:0{bits}b}"[::-1], 2)):
        budget.check()
        point = coords[index*total//count]
        if region.covers(Point(point)):
            yield point


def _balanced_nodes(model, net, settings, cfg, region, nodes, add, record, budget):
    """按层和边界轮转分配有限节点，输入原模型/预算，原地追加候选并记录覆盖。

    首先收集至多剩余节点数个边界，内存不随所有障碍无限增长。保留实际锚点，
    边界先在自身层采样，再用最多四分之一剩余空间提供稀疏换层坐标。
    返回 None；预算耗尽上抛，截断如实记录但不会省略真实碰撞检查。
    """
    layers = list(settings["layers"])
    available = cfg["max_nodes"]-len(nodes)
    queues = {layer: deque() for layer in layers}
    def rings_on_layer(layer):
        for shape in _boundaries(model, net, layer, settings["width"], region, budget):
            for part in _parts(shape):
                for ring in (part.exterior, *part.interiors):
                    budget.check()
                    if ring.intersects(region):
                        yield ring
    generators = {layer: rings_on_layer(layer) for layer in layers}
    seen = {layer: 0 for layer in layers}
    sampled = {layer: set() for layer in layers}
    transfer_points, rings, exhausted = {}, 0, set()
    while len(exhausted) < len(layers) and rings < available:
        for layer in layers:
            budget.check()
            if layer in exhausted:
                continue
            ring = next(generators[layer], None)
            if ring is None:
                exhausted.add(layer)
                continue
            queues[layer].append((rings, iter(_ring_points(ring, cfg["boundary_samples"], region, budget))))
            seen[layer] += 1
            transfer_points[rings] = []
            rings += 1
            if rings >= available:
                break
    discovery_complete = len(exhausted) == len(layers)

    def allocate(limit):
        while len(nodes) < limit and any(queues.values()):
            for layer in layers:
                budget.check()
                if not queues[layer]:
                    continue
                key, points = queues[layer].popleft()
                point = next(points, None)
                if point is None:
                    continue
                queues[layer].append((key, points))
                before = len(nodes)
                add(point, layer)
                if len(nodes) > before:
                    sampled[layer].add(key)
                    if len(transfer_points[key]) < 4:
                        transfer_points[key].append((point, layer))
                if len(nodes) >= limit:
                    break

    reserve = available//4 if settings["vias"] and len(layers) > 1 else 0
    allocate(cfg["max_nodes"]-reserve)
    transferred = 0
    for index in range(4):
        for points in transfer_points.values():
            if index >= len(points):
                continue
            point, source_layer = points[index]
            for layer in layers:
                budget.check()
                if layer == source_layer or len(nodes) >= cfg["max_nodes"] or transferred >= reserve:
                    continue
                before = len(nodes)
                add(point, layer)
                transferred += len(nodes)-before
    allocate(cfg["max_nodes"])
    pending = any(queues.values())
    record.update(boundaries_seen_by_layer=seen,
                  boundaries_sampled_by_layer={layer: len(keys) for layer, keys in sampled.items()},
                  boundary_discovery_complete=discovery_complete, transfer_nodes=transferred)
    record["node_candidates_truncated"] |= not discovery_complete or pending


def geometry_candidate(model, net, source, target, settings, options, stats, budget):
    """输入碰撞模型、两个真实铜组、规则、选项、统计和父预算，返回候选或 None。

    真实铜接入点与障碍膨胀边界构成连续坐标可见图，边通过原 trace_ok /
    via_ok；不查询粗网格，不据栅格封闭认定不可达。返回 (traces, vias)
    仍必须由原 _commit、check_board、check_routed_nets 和原生 DRC 验证。
    有限图穷尽返回 None；时间、边数或截断节点预算耗尽抛 RoutingFailure，
    非法配置抛 PipelineError。所有输入实体保持不变。
    """
    from pcb_routing import RoutingFailure
    cfg = validate_options(options.get("geometry_search"))
    if not cfg["enabled"]:
        return None
    record = dict(net=net, method="bounded_continuous_visibility", nodes=0,
                  edge_checks=0, expansions=0, budget_exhausted=False,
                  node_candidates_truncated=False, found=False, sampling=cfg["sampling"],
                  bend_penalty=cfg["bend_penalty"])
    stats.setdefault("geometry_searches", []).append(record)
    local = _SearchBudget(budget, cfg, record)
    try:
        starts = _anchors(source, target, settings["layers"], cfg["max_anchors"], local)
        goals = _anchors(target, source, settings["layers"], cfg["max_anchors"], local)
        if not starts or not goals:
            record["stop_reason"] = "真实铜在允许层上没有接入点"
            return None
        nodes = list(dict.fromkeys(starts + goals))
        if len(nodes) > cfg["max_nodes"]:
            record["budget_exhausted"] = True
            raise RoutingFailure("GEOMETRY_BUDGET", "真实接入点超过几何节点预算，未找到且未证明无解")
        seen = set(nodes)
        xs, ys = [p[0] for p in nodes], [p[1] for p in nodes]
        region = box(min(xs), min(ys), max(xs), max(ys)).buffer(cfg["margin"])
        # 先在所有允许层复用真实接入坐标；能否换层仍由全叠层孔检查决定。
        def add(point, layer):
            node = (float(point[0]), float(point[1]), layer)
            if node in seen:
                return True
            if len(nodes) >= cfg["max_nodes"]:
                record["node_candidates_truncated"] = True
                return False
            if model.board.covers(Point(node[:2])):
                seen.add(node)
                nodes.append(node)
            return True
        for point in starts + goals:
            for layer in settings["layers"]:
                add(point, layer)
        if cfg["sampling"] == "balanced":
            _balanced_nodes(model, net, settings, cfg, region, nodes, add, record, local)
        else:
            full = False
            for layer in settings["layers"]:
                for boundary in _boundaries(model, net, layer, settings["width"], region, local):
                    for part in _parts(boundary):
                        for ring in (part.exterior, *part.interiors):
                            coords = list(ring.coords)[:-1]
                            stride = max(1, math.ceil(len(coords) / cfg["boundary_samples"]))
                            for point in coords[::stride]:
                                local.check()
                                if not region.covers(Point(point)):
                                    continue
                                for face in settings["layers"]:
                                    if not add(point, face):
                                        full = True
                                        break
                                if full:
                                    break
                            if full:
                                break
                        if full:
                            break
                    if full:
                        break
                if full:
                    break
        record["nodes"] = len(nodes)
        record["nodes_by_layer"] = {layer: sum(node[2] == layer for node in nodes) for layer in settings["layers"]}
        target_set = set(goals)
        penalty = cfg["bend_penalty"]
        # 非零转弯代价保留入射方向；旧模式、零罚分仍按原坐标节点搜索。
        def state(node, heading=None):
            return (node, heading) if penalty else node

        def unpack(value):
            return value if penalty else (value, None)

        def heuristic(node):
            if cfg["sampling"] == "legacy":
                return 0.0
            return min(math.dist(node[:2], goal[:2]) for goal in goals) * settings["alpha"]

        costs, previous = {state(node): 0.0 for node in starts}, {}
        queue = [(heuristic(node), 0.0, index, state(node)) for index, node in enumerate(starts)]
        heapq.heapify(queue)
        serial, found = len(queue), None
        while queue:
            local.check()
            _, cost, _, current = heapq.heappop(queue)
            if cost != costs.get(current):
                continue
            node, heading = unpack(current)
            if node in target_set:
                found = current
                break
            record["expansions"] += 1
            # 不保存 O(N²) 的几何边表；方向状态也受精检次数和时间预算限制。
            for other in sorted(nodes, key=lambda item: (item not in target_set, math.dist(node[:2], item[:2]), item)):
                local.check()
                if node == other:
                    continue
                lower = math.dist(node[:2], other[:2]) * settings["alpha"]
                if not penalty and cost + lower >= costs.get(other, math.inf):
                    continue
                if penalty:
                    choices = _edge_choices(model, net, node, other, settings, options, local, heading, penalty)
                else:
                    edge = _edge(model, net, node, other, settings, options, local)
                    choices = [(*edge, None, 0)] if edge is not None else []
                for edge in choices:
                    destination = state(other, edge[3])
                    candidate = cost + edge[0]
                    if candidate < costs.get(destination, math.inf):
                        costs[destination], previous[destination] = candidate, (current, edge)
                        serial += 1
                        heapq.heappush(queue, (candidate+heuristic(other), candidate, serial, destination))
        record["states"] = len(costs)
        if found is None:
            if record["node_candidates_truncated"]:
                record["budget_exhausted"] = True
                raise RoutingFailure("GEOMETRY_BUDGET", "节点预算截断后的可见图内未找到，未证明无解", geometry=record)
            record["stop_reason"] = "有限边界与锚点候选中未找到"
            return None
        record["path_cost"] = costs[found]
        edges = []
        while found in previous:
            found, edge = previous[found]
            edges.append(edge)
        traces, vias, via_keys = [], [], set()
        record["bend_count"] = sum(edge[4] for edge in edges) if penalty else None
        record["bend_cost"] = record["bend_count"] * penalty if penalty else 0.0
        for _, segment, holes, _, _ in reversed(edges):
            traces.extend(segment)
            for via in holes:
                key = via["x"], via["y"]
                if key not in via_keys:
                    via_keys.add(key)
                    vias.append(via)
        local.check()
        record["found"] = True
        record["stop_reason"] = "找到待提交候选"
        return traces, vias
    except RoutingFailure as error:
        if "BUDGET" in error.diagnostics.get("code", ""):
            record["budget_exhausted"] = True
            record["stopped_by"] = error.diagnostics["code"]
        raise
    finally:
        record["elapsed_seconds"] = time.monotonic() - local.started
        stats["geometry_seconds"] = stats.get("geometry_seconds", 0.0) + record["elapsed_seconds"]
