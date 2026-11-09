"""有界位号避让候选生成；不修改器件、铜或原生属性，不代替字体实测和制造检查。"""
from __future__ import annotations

import math
import time
from shapely.geometry import box
from shapely.geometry.base import BaseGeometry
from shapely.strtree import STRtree


def _assign_slots(slots, selected, order, gap, deadline, max_nodes, max_depth, max_rounds,
                  max_target_nodes):
    """输入合法静态候选及已有分配，返回不减覆盖的有限置换结果与统计。

    只比较不旋转的候选矩形，距离公式与矩形 Shapely 距离一致；失败分支完整回滚。
    节点数、递归深度、轮数和共享截止时间均显式限制；耗尽时保留已验证分配。
    """
    chosen = dict(selected)
    stats = dict(nodes=0, successful_reassignments=0, rounds=0, budget_exhausted=False,
                 target_budget_exhaustions=0, target_budget_exhausted_ids=[])
    target_limit = max_nodes

    def conflict(a, b):
        ax0, ay0, ax1, ay1 = a[5]
        bx0, by0, bx1, by1 = b[5]
        dx, dy = max(bx0-ax1, ax0-bx1, 0), max(by0-ay1, ay0-by1, 0)
        return dx*dx + dy*dy < gap*gap

    def tick():
        if stats['nodes'] >= max_nodes or time.monotonic() >= deadline:
            stats['budget_exhausted'] = True
            return False
        if stats['nodes'] >= target_limit:
            return False
        stats['nodes'] += 1
        return True

    def assign(identifier, depth, protected):
        branches = []
        # 先完整查看无冲突槽位，再尝试少量冲突的候选；仍保留靠近所属器件的评分。
        for candidate in slots.get(identifier, []):
            if not tick():
                return False
            conflicts = [other for other, value in chosen.items()
                         if other != identifier and conflict(candidate, value)]
            if not conflicts:
                chosen[identifier] = candidate
                return True
            if depth and len(conflicts) <= 3 and not protected.intersection(conflicts):
                branches.append((len(conflicts), candidate[0], candidate, conflicts))
        for _, _, candidate, conflicts in sorted(branches, key=lambda b: b[:2])[:48]:
            if not tick():
                return False
            previous = dict(chosen)
            chosen[identifier] = candidate
            for other in conflicts:
                chosen.pop(other)
            if all(assign(other, depth-1, protected | {identifier})
                   for other in sorted(conflicts, key=lambda i: (len(slots[i]), i))):
                return True
            chosen.clear()
            chosen.update(previous)
        return False

    for round_index in range(max_rounds):
        stats['rounds'] = round_index + 1
        progress = False
        for identifier in order:
            if identifier in chosen:
                continue
            before = dict(chosen)
            target_limit = min(max_nodes, stats['nodes'] + max_target_nodes)
            if assign(identifier, max_depth, frozenset()):
                stats['successful_reassignments'] += 1
                progress = True
            else:
                chosen.clear()
                chosen.update(before)
                if stats['nodes'] >= target_limit:
                    stats['target_budget_exhaustions'] += 1
                    if identifier not in stats['target_budget_exhausted_ids']:
                        stats['target_budget_exhausted_ids'].append(identifier)
            if stats['budget_exhausted']:
                break
        if not progress or stats['budget_exhausted'] or len(chosen) == len(order):
            break
    return chosen, stats


def place_labels(labels, obstacles, board_polygon, *, gap, max_seconds, max_candidates,
                 method='reassign', search_radius=420, max_reassign_nodes=100000,
                 max_reassign_depth=4, max_reassign_rounds=3, max_target_nodes=6000,
                 owner_shapes=None, association_max_distance=None, association_advantage=None):
    """输入显式位号及保守字框、障碍实体、板形和预算，返回文字中心候选与未找到清单。

    labels 每项含 id/owner/x/y/width/height/owner_bounds，单位与板一致；obstacles
    为 Shapely 同面实体列表，调用方负责真实来源、层别及字框保守性的证据。搜索只在
    原器件包络周围有限位置尝试 0/90 度，字框留 gap；输出不包含文字值更改或删除。
    method='greedy' 保留旧 ±240 mil 贪心行为；reassign 先得到相同贪心基线，再对
    未找到位号扩展到 search_radius 并有限置换。所有阶段共用 max_seconds。
    max_candidates 限几何测试，max_reassign_nodes 限置换候选访问；近器件评分不变。
    可选 owner_shapes 显式给出所属器件真实本体；其他器件本体不得交叠，自家本体
    仍为次选。真实焊盘/丝印距离仍按 gap，不从位号猜测器件形状。
    association_max_distance/association_advantage 默认均为None，保持旧行为。
    两者同时指定时要求完整 owner_shapes；距自身超过前者且其他器件至少近后者的
    槽位被拒绝，避免文字挪到别的器件旁。单位与板一致，属于可读性候选约束。
    参数非法抛 ValueError；预算不足如实返回未找到，绝不宣称无解或全局最优。
    """
    if not all(type(v) in (int, float) and math.isfinite(v) and v > 0
               for v in (gap, max_seconds)) or type(max_candidates) is not int or max_candidates <= 0:
        raise ValueError('净距、秒数和候选预算必须为正值')
    if (method not in ('greedy', 'reassign') or type(search_radius) is not int
            or not 240 <= search_radius <= 900 or search_radius % 30
            or any(type(v) is not int or v <= 0 for v in
                   (max_reassign_nodes, max_reassign_depth, max_reassign_rounds, max_target_nodes))
            or max_reassign_depth > 8 or max_reassign_rounds > 20):
        raise ValueError('方法、30 mil 整倍数搜索半径或置换预算不合法')
    association_enabled = association_max_distance is not None or association_advantage is not None
    if association_enabled and (owner_shapes is None or not all(
            type(v) in (int, float) and math.isfinite(v) and v >= 0
            for v in (association_max_distance, association_advantage))):
        raise ValueError('归属约束须同时提供两个有限非负距离以及完整本体映射')
    if not board_polygon.is_valid or board_polygon.is_empty:
        raise ValueError('板形必须有效且非空')
    required = {'id', 'owner', 'x', 'y', 'width', 'height', 'owner_bounds'}
    if not isinstance(labels, list):
        raise ValueError('位号清单必须为列表')
    seen = set()
    for label in labels:
        if not isinstance(label, dict) or set(label) != required:
            raise ValueError('位号候选字段不完整或含未知字段')
        if any(not isinstance(label[k], str) or not label[k] for k in ('id', 'owner')) or label['id'] in seen:
            raise ValueError('位号 ID/父器件无效或重复')
        seen.add(label['id'])
        if not all(type(label[k]) in (int, float) and math.isfinite(label[k]) for k in ('x', 'y', 'width', 'height')):
            raise ValueError('位号坐标与尺寸必须为有限数值')
        bounds = label['owner_bounds']
        if (min(label['width'], label['height']) <= 0 or not isinstance(bounds, (list, tuple))
                or len(bounds) != 4 or not all(type(v) in (int, float) and math.isfinite(v) for v in bounds)
                or not (bounds[0] < bounds[2] and bounds[1] < bounds[3])):
            raise ValueError('字框必须为正，器件包络必须为四维边界')
    start = time.monotonic()
    deadline = start + max_seconds
    tree = STRtree(obstacles)
    allowed_area = board_polygon.buffer(-gap)
    owner_keys, owner_tree = [], None
    if owner_shapes is not None:
        if (not isinstance(owner_shapes, dict) or {a['owner'] for a in labels} - set(owner_shapes)
                or any(not isinstance(k, str) or not k or not isinstance(value, BaseGeometry) or value.is_empty or not value.is_valid
                       or value.area <= 0 for k, value in owner_shapes.items())):
            raise ValueError('本体映射须覆盖所有位号父器件并包含有效非空面积')
        owner_keys = list(owner_shapes)
        owner_tree = STRtree([owner_shapes[key] for key in owner_keys])
    slots = {}
    tested = 0
    association_rejections = 0
    exhausted = False
    searched_radius = {}
    completed_search = {}
    tested_positions = {label['id']: set() for label in labels}

    def add_candidates(label, radius, initial):
        """追加有限合法候选；入参为已验证标签与半径，预算用尽则保留已算结果。"""
        nonlocal tested, exhausted, association_rejections
        identifier = label['id']
        candidates = slots.setdefault(identifier, [])
        searched_radius[identifier] = radius
        completed_search[identifier] = False
        x0, y0, x1, y1 = label['owner_bounds']
        cx, cy = (x0+x1)/2, (y0+y1)/2
        for rotation in (0, 90):
            width, height = (label['width'], label['height']) if rotation == 0 else (label['height'], label['width'])
            positions = []
            if initial:
                positions.append((cx, cy))
                for extra in (0, 12, 24, 40, 60, 90, 120, 160):
                    d = gap + extra
                    positions.extend((x, y) for x in (cx, x0+width/2, x1-width/2)
                                     for y in (y1+d+height/2, y0-d-height/2))
                    positions.extend((x, y) for x in (x0-d-width/2, x1+d+width/2)
                                     for y in (cy, y0+height/2, y1-height/2))
            positions.extend((cx+dx, cy+dy) for dx in range(-radius, radius+1, 30)
                             for dy in range(-radius, radius+1, 30)
                             if (dx or dy) and (initial or max(abs(dx), abs(dy)) > 240))
            for x, y in dict.fromkeys(positions):
                key = (rotation, x, y)
                if key in tested_positions[identifier]:
                    continue
                if tested >= max_candidates or time.monotonic() >= deadline:
                    exhausted = True
                    break
                tested += 1
                tested_positions[identifier].add(key)
                shape = box(x-width/2, y-height/2, x+width/2, y+height/2)
                if not allowed_area.covers(shape):
                    continue
                if owner_tree is not None and any(owner_keys[int(i)] != label['owner']
                       and shape.intersects(owner_shapes[owner_keys[int(i)]]) for i in owner_tree.query(shape)):
                    continue
                if any(shape.distance(obstacles[int(i)]) < gap for i in tree.query(shape.buffer(gap))):
                    continue
                if association_enabled:
                    own_distance = shape.distance(owner_shapes[label['owner']])
                    if own_distance > association_max_distance:
                        nearest_index = int(owner_tree.nearest(shape))
                        nearest_owner = owner_keys[nearest_index]
                        # 最近本体若是自身，任何其他本体都不可能更近；否则直接查实际实体距离。
                        if (nearest_owner != label['owner'] and own_distance >
                                shape.distance(owner_shapes[nearest_owner]) + association_advantage):
                            association_rejections += 1
                            continue
                score = math.hypot(x-cx, y-cy) + (35 if rotation else 0)
                if x0 < x < x1 and y0 < y < y1:
                    score += 120
                candidates.append((score, x, y, rotation, shape, shape.bounds))
            if exhausted:
                break
        candidates.sort(key=lambda item: item[:4])
        completed_search[identifier] = not exhausted

    for label in labels:
        add_candidates(label, 240, True)
        if exhausted:
            break
    selected = {}
    # 最难安置的文字先安排；固定输入产生固定顺序，不改变任何现有铜几何。
    for label in sorted(labels, key=lambda item: (len(slots.get(item['id'], [])), -item['width'], item['id'])):
        for candidate in slots.get(label['id'], []):
            if time.monotonic() >= deadline:
                exhausted = True
                break
            if any(candidate[4].distance(other[4]) < gap for other in selected.values()):
                continue
            selected[label['id']] = candidate
            break
    greedy_count = len(selected)
    reassign_stats = dict(nodes=0, successful_reassignments=0, rounds=0, budget_exhausted=False,
                         target_budget_exhaustions=0, target_budget_exhausted_ids=[])
    if method == 'reassign':
        for label in sorted((label for label in labels if label['id'] not in selected),
                            key=lambda label: (len(slots.get(label['id'], [])), label['id'])):
            if exhausted:
                break
            add_candidates(label, search_radius, False)
        order = [label['id'] for label in sorted(labels, key=lambda item: (len(slots.get(item['id'], [])), -item['width'], item['id']))]
        refreshed = {}
        for identifier in order:
            for candidate in slots.get(identifier, []):
                if time.monotonic() >= deadline:
                    exhausted = True
                    break
                if all(candidate[4].distance(other[4]) >= gap for other in refreshed.values()):
                    refreshed[identifier] = candidate
                    break
        if len(refreshed) > len(selected):
            selected = refreshed
        # 新排序中挤出的位号也能用同一有限半径；最多再扩展八个最常见的阻塞者，
        # 让已经放好的文字有地方迁移。所有新增位置仍计入原 max_candidates。
        by_id = {label['id']: label for label in labels}
        blockers = {}
        for identifier in order:
            if time.monotonic() >= deadline:
                exhausted = True
                break
            if identifier not in selected:
                if searched_radius.get(identifier, 0) < search_radius and not exhausted:
                    add_candidates(by_id[identifier], search_radius, False)
                for candidate in slots.get(identifier, [])[:48]:
                    if time.monotonic() >= deadline:
                        exhausted = True
                        break
                    for other, existing in selected.items():
                        if candidate[4].distance(existing[4]) < gap:
                            blockers[other] = blockers.get(other, 0) + 1
        for identifier in sorted(blockers, key=lambda i: (-blockers[i], i))[:8]:
            if searched_radius.get(identifier, 0) < search_radius and not exhausted:
                add_candidates(by_id[identifier], search_radius, False)
        selected, reassign_stats = _assign_slots(slots, selected, order, gap, deadline,
                                                max_reassign_nodes, max_reassign_depth, max_reassign_rounds,
                                                max_target_nodes)
    found = set(selected)
    result = [dict(id=label['id'], owner=label['owner'], x=selected[label['id']][1], y=selected[label['id']][2],
                   rotation=selected[label['id']][3], bounds=list(selected[label['id']][5])) for label in labels if label['id'] in found]
    reasons = {}
    for label in labels:
        identifier = label['id']
        if identifier not in found:
            reasons[identifier] = dict(reason=('候选生成预算耗尽，未找到' if exhausted else
                 '有限范围未找到避开固定障碍的槽位' if not slots.get(identifier) else
                 '置换预算耗尽，未找到' if reassign_stats['budget_exhausted'] or identifier in reassign_stats['target_budget_exhausted_ids'] else '有限置换未找到同时满足标签间距的分配'),
                 legal_slots=len(slots.get(identifier, [])), requested_search_radius=searched_radius.get(identifier, 0),
                 candidate_search_complete=completed_search.get(identifier, False))
    return dict(candidates=result, not_found=[item['id'] for item in labels if item['id'] not in found],
                not_found_reasons=reasons,
                legal_slots={item['id']: len(slots.get(item['id'], [])) for item in labels},
                stats=dict(tested=tested, elapsed_seconds=time.monotonic() - start,
                           budget_exhausted=exhausted or reassign_stats['budget_exhausted'] or bool(reassign_stats['target_budget_exhausted_ids']), max_seconds=max_seconds, max_candidates=max_candidates,
                           method=method, greedy_placed=greedy_count, reassign=reassign_stats,
                           search_radius=search_radius, max_reassign_nodes=max_reassign_nodes,
                           max_reassign_depth=max_reassign_depth, max_reassign_rounds=max_reassign_rounds,
                           max_target_nodes=max_target_nodes, owner_shapes_checked=owner_shapes is not None,
                           association_max_distance=association_max_distance,
                           association_advantage=association_advantage,
                           association_rejections=association_rejections,
                           max_conflicting_labels_per_branch=3, max_branches=48, max_expanded_blockers=8),
                scope='显式保守字框有限避让候选；字体实际轮廓、来源和原生读回须另证。存在未找到时不得直接部分写回，须纳入未移动旧字框重查。',
                full_board_accepted=False)


def _self_check():
    """运行固定障碍、标签冲突置换及预算反例；任何误接收均用断言明确失败。"""
    area = box(0, 0, 500, 500)
    labels = [dict(id='label-a', owner='part-a', x=250, y=250, width=90, height=40,
                   owner_bounds=[200, 200, 300, 300])]
    blocked = place_labels(labels, [area], area, gap=8, max_seconds=1, max_candidates=5000)
    assert blocked['not_found'] == ['label-a'] and not blocked['candidates']
    assert not blocked['stats']['budget_exhausted']
    result = place_labels(labels, [], area, gap=8, max_seconds=1, max_candidates=500)
    assert len(result['candidates']) == 1 and not result['not_found']
    limited = place_labels(labels, [], area, gap=8, max_seconds=1, max_candidates=1)
    assert limited['stats']['budget_exhausted'] and limited['stats']['tested'] == 1
    two = labels + [dict(labels[0], id='label-b', owner='part-b')]
    limited = place_labels(two, [], area, gap=8, max_seconds=1, max_candidates=1)
    assert limited['not_found'] == ['label-b']
    assert '预算耗尽' in limited['not_found_reasons']['label-b']['reason']
    def slot(score, x):
        shape = box(x-10, -10, x+10, 10)
        return (score, x, 0, 0, shape, shape.bounds)
    slots = {'a':[slot(0,0),slot(1,100)], 'b':[slot(0,0),slot(1,5)]}
    original = {'a':slots['a'][0]}
    selected, stats = _assign_slots(slots, original, ['a','b'], 8, time.monotonic()+1, 100, 2, 2, 100)
    assert set(selected) == {'a','b'} and selected['a'][1] == 100 and original['a'][1] == 0
    assert selected['a'][4].distance(selected['b'][4]) >= 8
    assert stats['successful_reassignments'] == 1
    body_blocked = place_labels(labels, [], area, gap=8, max_seconds=1, max_candidates=5000,
                                owner_shapes={'part-a':box(200,200,300,300),'part-b':area})
    assert body_blocked['not_found'] == ['label-a']
    # 原算法会把被挡位号放到另一器件旁；显式归属约束必须拒绝这种不重叠但会误认的槽位。
    near_labels = [dict(id='near-label', owner='own', x=120, y=280, width=20, height=20,
                       owner_bounds=[100,260,140,300])]
    bodies = {'own':box(100,260,140,300), 'other':box(340,260,380,300)}
    args = dict(gap=8, max_seconds=1, max_candidates=5000, search_radius=240, owner_shapes=bodies)
    legacy = place_labels(near_labels, [box(0,0,300,600)], box(0,0,600,600), **args)
    assert legacy['candidates']
    first = box(*legacy['candidates'][0]['bounds'])
    assert first.distance(bodies['own']) > 80 and first.distance(bodies['own']) > first.distance(bodies['other']) + 40
    associated = place_labels(near_labels, [box(0,0,300,600)], box(0,0,600,600),
                              association_max_distance=80, association_advantage=40, **args)
    assert associated['stats']['association_rejections'] > 0
    for item in associated['candidates']:
        selected_shape = box(*item['bounds'])
        own_distance = selected_shape.distance(bodies['own'])
        assert not (own_distance > 80 and own_distance > selected_shape.distance(bodies['other']) + 40)
    try:
        place_labels(near_labels, [], area, association_max_distance=80, **args)
    except ValueError:
        pass
    else:
        raise AssertionError('缺少成对归属参数必须拒绝')


if __name__ == '__main__':
    _self_check()
    print('位号避让最小反例通过')
