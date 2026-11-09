"""来源明确的相对关系模板：从本板焊盘生成种子，不保存历史世界坐标。"""
from __future__ import annotations

from copy import deepcopy
import math

from pcb_fanout import _world
from pcb_pipeline_common import PipelineError, finite, rules_for, strict_options


_SIDES = {"left": (-1, 0), "right": (1, 0), "top": (0, -1), "bottom": (0, 1)}


def cell_members(cell):
    """输入已验证模板，输出锚点在首位的器件 ID 元组。"""
    return (cell["anchor"]["compId"], *(r["neighbor"]["compId"] for r in cell["relations"]))


def validate_cells(value, board):
    """输入模板列表与完整板，返回严格副本；引用、参数和重叠归属错误抛异常。

    anchor/neighbor 的 padId 可省略，此时明确指器件原点；提供时必须是真实
    焊盘。distances 与 lateral_offsets 为 mil，rotations 为相对锚点角度。
    不按位号、网名或相邻历史坐标猜测功能关系。
    """
    if not isinstance(value, list) or len(value) > 128:
        raise PipelineError("functional_cells 必须为最多 128 个模板的列表")
    components = {c["id"]: c for c in board["components"]}
    pads = {(c["id"], p["padId"]) for c in board["components"] for p in c["pads"]}
    result, ids, used = [], set(), set()
    def reference(raw):
        ref = deepcopy(strict_options(raw, {"compId", "padId"}, "functional_cells.reference"))
        if not isinstance(ref.get("compId"), str) or ref["compId"] not in components:
            raise PipelineError("functional_cells 引用未知器件")
        if "padId" in ref and (not isinstance(ref["padId"], str) or (ref["compId"], ref["padId"]) not in pads):
            raise PipelineError("functional_cells 引用未知焊盘")
        return ref
    for raw in value:
        cell = deepcopy(strict_options(raw, {"id", "anchor", "relations", "provenance", "max_candidates"}, "functional_cells"))
        if {"id", "anchor", "relations", "provenance"} - set(cell):
            raise PipelineError("functional_cells 需要 id/anchor/relations/provenance")
        if not isinstance(cell["id"], str) or not cell["id"] or cell["id"] in ids:
            raise PipelineError("functional_cells.id 必须为唯一非空字符串")
        ids.add(cell["id"])
        if not isinstance(cell["provenance"], str) or not cell["provenance"].strip():
            raise PipelineError("functional_cells.provenance 需要手册或明确设计约束的来源说明")
        cell["max_candidates"] = cell.get("max_candidates", 128)
        if type(cell["max_candidates"]) is not int or not 1 <= cell["max_candidates"] <= 100000:
            raise PipelineError("functional_cells.max_candidates 必须为 1..100000 的整数")
        cell["anchor"] = reference(cell["anchor"])
        relations = cell["relations"]
        if not isinstance(relations, list) or not 1 <= len(relations) <= 32:
            raise PipelineError("functional_cells.relations 需要 1..32 个显式邻件关系")
        normalized = []
        for raw_relation in relations:
            relation = deepcopy(strict_options(raw_relation, {"neighbor", "sides", "distances", "rotations", "lateral_offsets", "tolerance"}, "functional_cells.relations"))
            if {"neighbor", "sides", "distances", "rotations"} - set(relation):
                raise PipelineError("functional_cells.relations 需要 neighbor/sides/distances/rotations")
            relation["neighbor"] = reference(relation["neighbor"])
            sides = relation["sides"]
            if not isinstance(sides, list) or not sides or any(not isinstance(v, str) or v not in _SIDES for v in sides) or len(set(sides)) != len(sides):
                raise PipelineError("functional_cells.sides 仅支持唯一 left/right/top/bottom 列表")
            for key, default, minimum in (("distances", None, 0), ("rotations", None, -float("inf")), ("lateral_offsets", [0], -float("inf"))):
                values = relation.get(key, default)
                if not isinstance(values, list) or not 1 <= len(values) <= 16:
                    raise PipelineError(f"functional_cells.{key} 需要 1..16 个候选")
                values = [finite(v, "functional_cells."+key, minimum=minimum, positive=key == "distances") for v in values]
                if key == "rotations":
                    values = [v % 360 for v in values]
                if len(values) != len(set(values)):
                    raise PipelineError(f"functional_cells.{key} 不允许重复候选")
                relation[key] = values
            relation["tolerance"] = finite(relation.get("tolerance", 0.1), "functional_cells.tolerance", minimum=0)
            if relation["tolerance"] >= min(relation["distances"]):
                raise PipelineError("关系容差必须小于全部候选距离，不能取消侧向约束")
            normalized.append(relation)
        cell["relations"] = normalized
        members = cell_members(cell)
        if len(set(members)) != len(members) or set(members) & used:
            raise PipelineError("功能单元内不允许重复器件，也不允许跨单元重叠归属")
        used.update(members)
        result.append(cell)
    return result


def _reference_point(component, reference):
    if "padId" not in reference:
        return component["x"], component["y"]
    pad = next(p for p in component["pads"] if p["padId"] == reference["padId"])
    return _world(component, (pad["localX"], pad["localY"]))


def _relation_error(components, cell, relation):
    anchor = components[cell["anchor"]["compId"]]
    neighbor = components[relation["neighbor"]["compId"]]
    ax, ay = _reference_point(anchor, cell["anchor"])
    nx, ny = _reference_point(neighbor, relation["neighbor"])
    angle = math.radians(anchor["rotation"])
    dx = (nx-ax)*math.cos(angle)+(ny-ay)*math.sin(angle)
    dy = -(nx-ax)*math.sin(angle)+(ny-ay)*math.cos(angle)
    if anchor["layer"] == 2:
        dx = -dx
    deviation = min(math.hypot(dx-sx*distance+sy*lateral, dy-sy*distance-sx*lateral)
                    for side in relation["sides"] for sx, sy in [_SIDES[side]]
                    for distance in relation["distances"] for lateral in relation["lateral_offsets"])
    rotation_delta = (neighbor["rotation"]-anchor["rotation"]) % 360
    rotation_error = min(abs((rotation_delta-target+180) % 360-180) for target in relation["rotations"])
    return deviation, rotation_error


def check_relations(board, cells, *, partial=False):
    """独立重建当前姿态的关系证据，返回错误列表；不信任种子统计或缓存。

    输入完整或显式 partial 验证板、已验证的 cells；输出带实际偏差的错误。
    完整板缺失端子也报错，局部阶段只跳过尚未全部出现的两端。配置需先经
    validate_cells；非法引用抛 PipelineError。此检查不取代 check_board。
    """
    components = {c["id"]: c for c in board["components"]}
    errors = []
    for cell in cells:
        for relation in cell["relations"]:
            pair = cell["anchor"]["compId"], relation["neighbor"]["compId"]
            if not set(pair).issubset(components):
                if not partial:
                    errors.append(f"FUNCTIONAL_CELL_MISSING: {cell['id']} 缺少 {pair}")
                continue
            try:
                distance, rotation = _relation_error(components, cell, relation)
            except (KeyError, StopIteration) as exc:
                raise PipelineError("功能单元检查缺少原始焊盘或姿态字段") from exc
            if distance > relation["tolerance"]+1e-7 or rotation > 1e-7:
                errors.append(f"FUNCTIONAL_CELL_RELATION: {cell['id']} {pair[1]} 位置偏差={distance:.6g}mil，相对角度偏差={rotation:.6g}°")
    return errors


def generate_seed(board, components, cells, options, immovable, stats, budget):
    """从当前焊盘与相对模板生成完整器件映射副本，不移动受保护器件。

    每个单元采用有限深度优先候选，入参 budget 共同限制总候选和时间；
    只做单元包络初筛，所有结果仍须实际板框/区域/原铜的原检查器精检。
    不返回部分单元。未找到抛 PlacementFailure，非法配置抛 PipelineError。
    """
    from pcb_global_place import PlacementFailure
    from pcb_placement import _envelope
    current = dict(components)
    records = stats.setdefault("functional_cells", [])
    rules = rules_for(board)
    for cell in cells:
        budget.check()
        record = dict(id=cell["id"], provenance=cell["provenance"], candidate_checks=0,
                      members=list(cell_members(cell)), status="running", full_board_accepted=False)
        records.append(record)
        members_board = dict(board, components=[current[cid] for cid in cell_members(cell)])
        if not check_relations(members_board, [cell]):
            record["status"] = "preserved_valid_relations"
            continue
        anchor = current[cell["anchor"]["compId"]]
        ax, ay = _reference_point(anchor, cell["anchor"])
        origin = _world(anchor, (0, 0))
        relations = cell["relations"]
        pools = []
        for relation in relations:
            neighbor = current[relation["neighbor"]["compId"]]
            pool = []
            if neighbor["id"] in immovable:
                if not check_relations(dict(board, components=[anchor, neighbor]), [dict(cell, relations=[relation])]):
                    pool = [neighbor]
            else:
                for relative in relation["rotations"]:
                    rotation = (anchor["rotation"]+relative) % 360
                    if not any(abs((rotation-angle+180) % 360-180) < 1e-8 for angle in options["rotations"]):
                        continue
                    zero = dict(neighbor, x=0., y=0., rotation=rotation)
                    px, py = _reference_point(zero, relation["neighbor"])
                    for side in relation["sides"]:
                        dx, dy = _SIDES[side]
                        for distance in relation["distances"]:
                            for lateral in relation["lateral_offsets"]:
                                budget.check()
                                # 使用当前锚点局部轴，底层镜像和任意角度由 _world 处理。
                                vx, vy = _world(anchor, (dx*distance-dy*lateral, dy*distance+dx*lateral))
                                pool.append(dict(neighbor, x=ax+vx-origin[0]-px, y=ay+vy-origin[1]-py, rotation=rotation))
            pools.append(pool)
        # 少候选伙伴优先；完整内部结构建立后才能交给刚体分区布局。
        ordered = sorted(zip(relations, pools), key=lambda entry: (len(entry[1]), entry[0]["neighbor"]["compId"]))
        states, indices, index = [{anchor["id"]: anchor}], [0], 0
        while index < len(ordered):
            budget.check()
            relation, pool = ordered[index]
            if indices[index] >= len(pool):
                if index == 0:
                    record["status"] = "not_found"
                    raise PlacementFailure("PLACEMENT_CELL_NOT_FOUND", "相对模板有限候选中未找到完整功能单元，未证明无解", cell=record)
                states.pop(); indices.pop(); index -= 1
                continue
            if record["candidate_checks"] >= cell["max_candidates"]:
                record["status"] = "budget_exhausted"
                raise PlacementFailure("PLACEMENT_CELL_BUDGET", "功能单元候选预算内未找到完整结构，未证明无解", cell=record)
            budget.candidate(); record["candidate_checks"] += 1
            candidate = pool[indices[index]]; indices[index] += 1
            envelope = _envelope(candidate, rules.curve_tolerance)
            if any(candidate["layer"] == placed["layer"] and envelope.distance(_envelope(placed, rules.curve_tolerance)) < max(rules.default_clearance, rules.component_courtyard_margin)-1e-9
                   for placed in states[index].values()):
                continue
            # 相交必须单独拒绝，零间距规则不能把重叠当成合法候选。
            if any(candidate["layer"] == placed["layer"] and envelope.intersection(_envelope(placed, rules.curve_tolerance)).area > 1e-9
                   for placed in states[index].values()):
                continue
            states.append(dict(states[index], **{candidate["id"]: candidate}))
            index += 1
            indices.append(0)
        current.update(states[-1])
        if check_relations(dict(board, components=list(current.values())), [cell]):
            raise PlacementFailure("PLACEMENT_CELL_VERIFICATION", "功能单元种子未通过独立关系重建")
        budget.check()
        record["status"] = "seed_generated_requires_board_check"
    return current
