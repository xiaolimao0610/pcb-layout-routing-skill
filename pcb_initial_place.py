"""有界分区初始排布：逻辑分组限定区域，显式簇保持刚体，候选仍交原检查器。"""
from __future__ import annotations

import math

from shapely.geometry import box
from shapely.ops import unary_union

from pcb_pipeline_common import board_polygon, rules_for


def translation_bounds(components, unit, available, regions, tolerance):
    """求同面刚体的平移矩形必要条件。

    入参：器件映射、含 members 的刚体、退板边后的几何、器件区域和曲线容差。
    出参：所有成员同时满足板框与各自区域包围盒的平移范围；空集返回 None。
    这里只作候选裁剪，非矩形板、孔洞和实体间距仍需精检；几何异常沿原调用上抛。
    """
    from pcb_placement import _envelope
    if available.is_empty:
        return None
    lo_x = lo_y = -math.inf
    hi_x = hi_y = math.inf
    for cid in unit["members"]:
        allowed = available
        region = regions.get(cid)
        if region is not None:
            region = box(*region) if isinstance(region, (list, tuple)) else region
            allowed = allowed.intersection(region)
        if allowed.is_empty:
            return None
        x0, y0, x1, y1 = allowed.bounds
        sx0, sy0, sx1, sy1 = _envelope(components[cid], tolerance).bounds
        lo_x, lo_y = max(lo_x, x0-sx0), max(lo_y, y0-sy0)
        hi_x, hi_y = min(hi_x, x1-sx1), min(hi_y, y1-sy1)
    if lo_x > hi_x or lo_y > hi_y:
        return None
    return lo_x, lo_y, hi_x, hi_y


def _clamp(point, bounds):
    return (min(bounds[2], max(bounds[0], point[0])),
            min(bounds[3], max(bounds[1], point[1])))


def _preferred(board, components, unit, placed, bounds, options, preserve):
    """输入当前刚体及已放置件，返回有界偏好平移，不把未放置原坐标当网络锚点。"""
    from pcb_placement import _envelope
    if preserve:
        return _clamp((0, 0), bounds)
    rules, polygon = rules_for(board), board_polygon(board)
    cx = sum(components[cid]["x"] for cid in unit["members"])/len(unit["members"])
    cy = sum(components[cid]["y"] for cid in unit["members"])/len(unit["members"])
    target = (bounds[0], bounds[1])
    # 只用已放真实成员的邻接关系；原理图导入的板外坐标不参与初始位置评分。
    own = set(unit["members"])
    neighbors = sorted({pin["compId"] for refs in board["nets"].values()
                        if any(pin["compId"] in own for pin in refs)
                        for pin in refs if pin["compId"] in placed and pin["compId"] not in own})
    if neighbors:
        target = (sum(placed[cid]["x"] for cid in neighbors)/len(neighbors)-cx,
                  sum(placed[cid]["y"] for cid in neighbors)/len(neighbors)-cy)
    x0, y0, x1, y1 = polygon.bounds
    for edge in options.get("edge_components", []):
        if edge["component"] not in own:
            continue
        sx0, sy0, sx1, sy1 = _envelope(components[edge["component"]], rules.curve_tolerance).bounds
        distance = max(rules.board_edge_clearance, edge["target_distance"])
        tx, ty = target
        choices = dict(left=(x0+distance-sx0, ty), right=(x1-distance-sx1, ty),
                       bottom=(tx, y0+distance-sy0), top=(tx, y1-distance-sy1))
        target = choices.get(edge["edge"], choices["left"])
        break
    return _clamp(target, bounds)


def _offsets(board, components, unit, placed, shapes, bounds, preferred, options, budget):
    """生成空隙边界与有限网格候选。

    输入平移范围、同层已放包络和偏好点；输出平移量迭代器。空隙只用保守
    AABB 预筛，不作最终合法性结论。调用方限制候选总量，时间耗尽沿预算异常上抛。
    """
    from pcb_placement import _envelope, _grid_points
    rules = rules_for(board)
    gap = max(rules.component_courtyard_margin, rules.default_clearance) + 2*rules.curve_tolerance
    obstacles = []
    for cid in unit["members"]:
        budget.check()
        x0, y0, x1, y1 = _envelope(components[cid], rules.curve_tolerance).bounds
        for oid, other in shapes.items():
            if components[cid]["layer"] != placed[oid]["layer"]:
                continue
            ox0, oy0, ox1, oy1 = other.bounds
            obstacles.append(box(ox0-gap-x1, oy0-gap-y1, ox1+gap-x0, oy1+gap-y0))
    points = {preferred, (bounds[0], bounds[1]), (bounds[0], bounds[3]),
              (bounds[2], bounds[1]), (bounds[2], bounds[3])}
    if bounds[0] < bounds[2] and bounds[1] < bounds[3]:
        free = box(*bounds)
        if obstacles:
            free = free.difference(unary_union(obstacles))
        pieces = list(free.geoms) if hasattr(free, "geoms") else [free]
        for piece in pieces:
            budget.check()
            if piece.is_empty:
                continue
            if piece.geom_type == "Polygon":
                for ring in [piece.exterior, *piece.interiors]:
                    points.update((float(x), float(y)) for x, y in ring.coords)
            point = piece.representative_point()
            points.add((point.x, point.y))
        # 将偏好点投到空隙边界，避免接口被较近的内侧空隙顶点吸引而偏离板边。
        if not free.is_empty:
            from shapely.geometry import Point
            from shapely.ops import nearest_points
            projected = nearest_points(free, Point(*preferred))[0]
            points.add((projected.x, projected.y))
    budget.check()
    for point in sorted(points, key=lambda p: (math.hypot(p[0]-preferred[0], p[1]-preferred[1]), p[1], p[0])):
        yield point
    for _, dx, dy in _grid_points(bounds, preferred, options.get("grid_step", rules.grid_step)):
        yield dx, dy


def pack_units(board, components, units, options, stats, budget, *, stage="initialization", preserve=False):
    """按有效分区排布固定件、显式接口和大件，再放小件。

    入参：完整板、器件映射、刚体单元、规范化选项及共享预算/统计；preserve
    决定是否偏好当前位置。出参：所有器件齐全且经原检查器通过的姿态映射。
    不改输入、原铜或固定件；候选/时间耗尽及无有限候选抛 PlacementFailure。
    """
    from pcb_global_place import PlacementFailure, _legal, _rotation_deltas, _rotated, _translated
    from pcb_placement import _envelope
    polygon, rules = board_polygon(board), rules_for(board)
    available = polygon.buffer(-rules.board_edge_clearance)
    if available.is_empty:
        raise PlacementFailure("PLACEMENT_NO_CANDIDATE", "板内区域为空，本次未找到合法布局，未证明无解")
    record = stats.setdefault(stage, {})
    record.update(method="region_rows_and_free_space", candidate_attempts=0, exact_checks=0,
                  placed_units=0, placement_order=[], status="running")
    placed = {cid: components[cid] for unit in units if unit["fixed"] for cid in unit["members"]}
    if placed:
        record["exact_checks"] += 1
        if not _legal(board, placed, options, budget, partial=True):
            raise PlacementFailure("PLACEMENT_FIXED_GEOMETRY", "固定簇或既有铜保护区域未通过原几何检查")
    shapes = {cid: _envelope(c, rules.curve_tolerance) for cid, c in placed.items()}
    edge_ids = {entry["component"] for entry in options.get("edge_components", [])}
    pending = [unit for unit in units if not unit["fixed"]]
    pending.sort(key=lambda unit: (not bool(set(unit["members"]) & edge_ids),
                     -sum(_envelope(components[cid], rules.curve_tolerance).area for cid in unit["members"]), unit["id"]))
    for unit in pending:
        budget.check()
        current = components
        if not preserve:
            cx = sum(components[cid]["x"] for cid in unit["members"])/len(unit["members"])
            cy = sum(components[cid]["y"] for cid in unit["members"])/len(unit["members"])
            current = _translated(components, unit["members"], -cx, -cy)
        streams = []
        for delta in [0.0] + _rotation_deltas(current, unit, options):
            rotated = current if delta == 0 else _rotated(current, unit, delta)
            bounds = translation_bounds(rotated, unit, available, options.get("regions", {}), rules.curve_tolerance)
            if bounds is not None:
                preferred = _preferred(board, rotated, unit, placed, bounds, options, preserve)
                streams.append((rotated, iter(_offsets(board, rotated, unit, placed, shapes, bounds, preferred, options, budget)), set()))
        if not streams:
            record.update(status="not_found", failed_unit=unit["id"], reason="unit_bounds_exceed_region")
            hint = "；clusters保持内部相对距离，逻辑分区请使用functional_groups" if unit["cluster"] else ""
            raise PlacementFailure("PLACEMENT_UNIT_FIT", "允许角度的刚体包络未能落入有效区域"+hint+"，本次未找到候选", unit=unit["id"])
        accepted = None
        # 每单元有独立上界，且所有快速预筛也计入共享候选预算。
        per_unit = max(32, options.get("max_candidates", 4000)//max(1, len(pending))*4)
        attempts = 0
        while streams and attempts < per_unit:
            budget.check()
            rotated, stream, seen = streams.pop(0)
            try:
                dx, dy = next(stream)
            except StopIteration:
                continue
            streams.append((rotated, stream, seen))
            if (dx, dy) in seen:
                continue
            seen.add((dx, dy))
            budget.candidate()
            attempts += 1
            record["candidate_attempts"] += 1
            moved = _translated(rotated, unit["members"], dx, dy)
            trial = {**placed, **{cid: moved[cid] for cid in unit["members"]}}
            new_shapes = {cid: _envelope(trial[cid], rules.curve_tolerance) for cid in unit["members"]}
            if any(not available.covers(shape) for shape in new_shapes.values()):
                continue
            if any((shape.intersects(other) or shape.distance(other) < rules.component_courtyard_margin)
                   for cid, shape in new_shapes.items() for oid, other in shapes.items()
                   if trial[cid]["layer"] == placed[oid]["layer"]):
                continue
            record["exact_checks"] += 1
            if _legal(board, trial, options, budget, partial=True, count=False):
                accepted = trial
                shapes.update(new_shapes)
                break
        if accepted is None:
            record.update(status="not_found", failed_unit=unit["id"], reason="bounded_candidates_rejected")
            raise PlacementFailure("PLACEMENT_NO_CANDIDATE", "分区/空隙有限候选内未找到合法布局，未证明无解", unit=unit["id"])
        placed = accepted
        record["placed_units"] += 1
        record["placement_order"].append(unit["id"])
    record["exact_checks"] += 1
    if not _legal(board, placed, options, budget):
        raise PlacementFailure("PLACEMENT_VERIFICATION", "分区初始候选未通过完整几何和约束检查")
    record["status"] = "passed"
    return placed
