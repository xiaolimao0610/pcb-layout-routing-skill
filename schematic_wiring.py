"""按原理图的真实端点生成有限的正交走线候选。坐标沿用编辑器，纵轴向上。"""

import argparse
import heapq
import itertools
import json
import math
import time
from collections import defaultdict, deque
from pathlib import Path

DIR = {"R": (1, 0), "L": (-1, 0), "U": (0, 1), "D": (0, -1)}
STEP = tuple(DIR.values())
OPPOSITE = (1, 0, 3, 2)


def _edge(a, b):
    return (a, b) if a < b else (b, a)


def _compress(points):
    out = []
    for p in points:
        if len(out) >= 2:
            a, b = out[-2:]
            if a[0] == b[0] == p[0] or a[1] == b[1] == p[1]:
                out.pop()
        out.append(p)
    return out


def _rect(rect):
    if len(rect) != 4 or not all(
        isinstance(v, (float, int)) and math.isfinite(v) for v in rect
    ):
        raise ValueError("矩形需要四个有限坐标")
    x1, y1, x2, y2 = rect
    if x1 > x2 or y1 > y2:
        raise ValueError("矩形边界顺序错误")
    return x1, y1, x2, y2


class _Sheet:
    def __init__(self, page):
        self.grid = page.get("grid", 5)
        if (
            not isinstance(self.grid, (int, float))
            or not math.isfinite(self.grid)
            or self.grid <= 0
        ):
            raise ValueError("网格必须是有限正数")
        x1, y1, x2, y2 = _rect(page["bounds"])
        self.bounds = (
            math.ceil(x1 / self.grid),
            math.ceil(y1 / self.grid),
            math.floor(x2 / self.grid),
            math.floor(y2 / self.grid),
        )
        a, b, c, d = self.bounds
        self.cell_limit = page.get("max_cells", 2_000_000)
        if c < a or d < b or (c - a + 1) * (d - b + 1) > self.cell_limit:
            raise ValueError("图页为空或超过网格单元预算")
        self.blocked = set()
        # 形状边缘也不得穿过。半格细障碍必须覆盖跨越它的那条边。
        self.fences = set()
        for rectangle in page.get("obstacles", []):
            x1, y1, x2, y2 = (v / self.grid for v in _rect(rectangle))
            lo, hi = max(a, math.ceil(x1)), min(c, math.floor(x2))
            bot, top = max(b, math.ceil(y1)), min(d, math.floor(y2))
            self.blocked.update(
                (x, y) for x in range(lo, hi + 1) for y in range(bot, top + 1)
            )
            if lo > hi and 0 < lo - a <= c - a and lo - 1 < x1 <= x2 < lo:
                self.fences.update(
                    _edge((lo - 1, y), (lo, y)) for y in range(bot, top + 1)
                )
            if bot > top and 0 < bot - b <= d - b and bot - 1 < y1 <= y2 < bot:
                self.fences.update(
                    _edge((x, bot - 1), (x, bot)) for x in range(lo, hi + 1)
                )
        self.occupancy = defaultdict(dict)
        self.pins = {}
        self.forbidden = set()
        self.groups = defaultdict(list)
        self.shared = set(page.get("shared_nets", []))
        self.cross_page = set(page.get("cross_page_nets", []))
        self.segment_edges = defaultdict(set)
        self.errors = []
        seen = set()
        for pin in page["pins"]:
            key = (pin["ref"], str(pin["pin"]))
            if key in seen:
                raise ValueError(f"重复引脚 {key}")
            seen.add(key)
            at = self.snap(pin["x"], pin["y"])
            net = pin["net"]
            if at in self.pins and self.pins[at] != net:
                raise ValueError(f"异网引脚重合 {key}")
            if not self.inside(at):
                raise ValueError(f"引脚超出图纸 {key}")
            self.pins[at] = net
            if net is None:
                self.forbidden.add(at)
                continue
            if not isinstance(net, str) or not net:
                raise ValueError(f"引脚的网名无效 {key}")
            dx, dy = DIR[pin["dir"]]
            has_port = pin.get("port", False)
            if has_port and net not in self.shared:
                raise ValueError(f"独立端口仅能用于明确允许的共用网络 {key}")
            partition = (
                key
                if has_port
                else (pin.get("block", "") if net in self.shared else None)
            )
            group = (net, partition)
            length = self.snap(pin.get("stub", self.grid), 0)[0]
            if length < 1 or length > max(c - a, d - b):
                raise ValueError(f"引脚引出长度超出范围 {key}")
            path = [(at[0] + dx * i, at[1] + dy * i) for i in range(length + 1)]
            self.groups[group].append((key, path))
        for group, entries in self.groups.items():
            net = group[0]
            for key, path in entries:
                bad = [
                    p
                    for p in path
                    if not self.inside(p)
                    or p in self.blocked
                    or p in self.forbidden
                    or (p in self.pins and self.pins[p] != net)
                    or any(n != net for n in self.occupancy.get(p, {}))
                ]
                if bad or any(
                    _edge(a, b) in self.fences for a, b in itertools.pairwise(path)
                ):
                    self.errors.append(
                        {
                            "net": net,
                            "pin": key,
                            "reason": "引脚出线与器件、边界或其他引脚冲突",
                        }
                    )
                    continue
                self.add(group, path)

    def snap(self, x, y):
        vals = (x / self.grid, y / self.grid)
        if any(not math.isfinite(v) or abs(v - round(v)) > 1e-7 for v in vals):
            raise ValueError(f"端点不在指定网格 ({x}, {y})，禁止改变引脚坐标来对齐")
        return tuple(round(v) for v in vals)

    def inside(self, p):
        a, b, c, d = self.bounds
        return a <= p[0] <= c and b <= p[1] <= d

    def add(self, group, path):
        net = group[0]
        for a, b in itertools.pairwise(path):
            direction = STEP.index((b[0] - a[0], b[1] - a[1]))
            self.occupancy[a][net] = self.occupancy[a].get(net, 0) | (1 << direction)
            self.occupancy[b][net] = self.occupancy[b].get(net, 0) | (
                1 << OPPOSITE[direction]
            )
            self.segment_edges[group].add(_edge(a, b))

    def available(self, p, direction, net):
        if not self.inside(p) or p in self.blocked or p in self.forbidden:
            return None
        if p in self.pins:
            return None
        masks = self.occupancy.get(p)
        if not masks:
            return 0
        # 异网仅可在两根导线的直线内部正交相交。折点、端点和三通不可相交。
        cross_mask = 12 if direction < 2 else 3
        for other, mask in masks.items():
            if other != net and mask != cross_mask:
                return None
        return 1 if any(n != net for n in masks) else 0

    def paths(self, group, entries, budget):
        net = group[0]
        seed, *pending = sorted(entries, key=lambda entry: entry[0])
        tree = set(seed[1][1:])
        while pending:
            # 与树上已有导线任一点接入，包括线段中点。
            def distance(p):
                return min(abs(t[0] - p[0]) + abs(t[1] - p[1]) for t in tree)

            choice = min(range(len(pending)), key=lambda i: distance(pending[i][1][-1]))
            key, stub = pending.pop(choice)
            start = stub[-1]
            if start not in tree:
                path, reason = self.search(start, tree, net, budget)
                if path is None:
                    self.errors.append({"net": net, "pin": key, "reason": reason})
                    continue
                self.add(group, path)
                tree.update(path)
            tree.update(stub[1:])

    def search(self, start, tree, net, budget):
        # A* 到树包围盒的曼哈顿距离是可采纳的下界。
        xs, ys = zip(*tree)
        xmin, xmax, ymin, ymax = min(xs), max(xs), min(ys), max(ys)

        def lower(p):
            return max(xmin - p[0], 0, p[0] - xmax) + max(ymin - p[1], 0, p[1] - ymax)

        serial = itertools.count()
        first = (start, -1)
        opened = [(lower(start), 0, next(serial), first)]
        costs = {first: 0}
        previous = {}
        while opened:
            if budget["left"] <= 0 or time.monotonic() >= budget["deadline"]:
                return None, "预算内未找到路径"
            _, cost, _, state = heapq.heappop(opened)
            if costs.get(state) != cost:
                continue
            here, incoming = state
            if here in tree and all(n == net for n in self.occupancy.get(here, {})):
                path = [here]
                while state != first:
                    state = previous[state]
                    path.append(state[0])
                return path[::-1], None
            budget["left"] -= 1
            for direction, (dx, dy) in enumerate(STEP):
                if incoming >= 0 and direction == OPPOSITE[incoming]:
                    continue
                there = here[0] + dx, here[1] + dy
                cross = self.available(there, direction, net)
                if cross is None or _edge(here, there) in self.fences:
                    continue
                # 经过异网交点时禁止沿途拐弯或增加接点。
                if (
                    any(n != net for n in self.occupancy.get(here, {}))
                    and incoming != direction
                ):
                    continue
                extra = (
                    1
                    + 8 * cross
                    + (4 if incoming >= 0 and incoming != direction else 0)
                )
                new_cost = cost + extra
                nxt = there, direction
                if new_cost < costs.get(nxt, math.inf):
                    costs[nxt] = new_cost
                    previous[nxt] = state
                    heapq.heappush(
                        opened, (new_cost + lower(there), new_cost, next(serial), nxt)
                    )
        return None, "当前版式与网格内未找到路径"

    def emit(self):
        wires = []
        for group, edges in self.segment_edges.items():
            net = group[0]
            adjacent = defaultdict(set)
            for a, b in edges:
                adjacent[a].add(b)
                adjacent[b].add(a)
            # 把各分支拆成原生线段，让 T 点成为所有相连导线的真实端点。
            cuts = {p for p, v in adjacent.items() if len(v) != 2 or p in self.pins}
            used = set()
            first_index = len(wires)
            for point in sorted(cuts):
                for nxt in sorted(adjacent[point]):
                    if _edge(point, nxt) in used:
                        continue
                    path = [point, nxt]
                    used.add(_edge(point, nxt))
                    while path[-1] not in cuts:
                        (after,) = adjacent[path[-1]] - {path[-2]}
                        if _edge(path[-1], after) in used:
                            break
                        used.add(_edge(path[-1], after))
                        path.append(after)
                    points = [
                        [x * self.grid, y * self.grid] for x, y in _compress(path)
                    ]
                    wires.append({"net": net, "points": points, "show_name": False})
            if len(used) != len(edges):
                self.errors.append({"net": net, "reason": "存在孤立闭环线段"})
            need_name = (
                net in self.shared
                or net in self.cross_page
                or len(self.groups[group]) == 1
            )
            candidates = wires[first_index:]
            if need_name and candidates:
                longest = max(
                    candidates,
                    key=lambda w: max(
                        abs(a[0] - b[0]) + abs(a[1] - b[1])
                        for a, b in zip(w["points"], w["points"][1:])
                    ),
                )
                longest["show_name"] = True
        return wires


def audit_wires(page, wires):
    """以实际折线重建导通和相交关系。输入图页与输出折线，返回差异及物理指标。

    数字网格坐标、实体障碍或输入引脚不合法时抛出 ValueError/KeyError。
    无引脚线岛、同块断开、穿越符号/NC、意外相连均进入 errors。
    """
    sheet = _Sheet(page)
    return _audit(sheet, wires)


def _audit(sheet, wires):
    segments = defaultdict(lambda: defaultdict(set))
    named = defaultdict(set)
    end_or_bend = defaultdict(set)
    endpoints = defaultdict(lambda: defaultdict(int))
    masks = defaultdict(dict)
    errors = list(sheet.errors)
    known_nets = {g[0] for g in sheet.groups}
    length = 0
    for i, wire in enumerate(wires):
        net = wire["net"]
        if net not in known_nets:
            errors.append({"wire": i, "reason": "导线网络不在原图页"})
            continue
        points = [sheet.snap(*p) for p in wire["points"]]
        if len(points) < 2:
            errors.append({"wire": i, "reason": "导线缺少端点"})
            continue
        end_or_bend[net].update(points)
        endpoints[net][points[0]] += 1
        endpoints[net][points[-1]] += 1
        if wire.get("show_name"):
            named[net].add(points[0])
        for a, b in itertools.pairwise(points):
            if (
                a == b
                or (a[0] != b[0] and a[1] != b[1])
                or not sheet.inside(a)
                or not sheet.inside(b)
            ):
                errors.append({"wire": i, "reason": "越界、零长或非正交线段"})
                continue
            delta = ((b[0] > a[0]) - (b[0] < a[0]), (b[1] > a[1]) - (b[1] < a[1]))
            direction = STEP.index(delta)
            p = a
            while p != b:
                q = p[0] + delta[0], p[1] + delta[1]
                if q in segments[net][p]:
                    errors.append({"wire": i, "reason": "重复布置同一条线段", "at": p})
                segments[net][p].add(q)
                segments[net][q].add(p)
                masks[p][net] = masks[p].get(net, 0) | (1 << direction)
                masks[q][net] = masks[q].get(net, 0) | (1 << OPPOSITE[direction])
                if _edge(p, q) in sheet.fences:
                    errors.append({"wire": i, "reason": "穿越薄障碍", "at": p})
                p = q
                length += 1
    crossings = 0
    for p, nets in masks.items():
        for net, mask in nets.items():
            count = mask.bit_count()
            if (p in sheet.pins or count >= 3) and endpoints[net].get(p, 0) != count:
                errors.append(
                    {
                        "at": p,
                        "net": net,
                        "reason": "引脚或分支必须由各方向真实线端相接",
                    }
                )
        if p in sheet.blocked or p in sheet.forbidden:
            errors.append({"at": p, "reason": "穿过实体或未连接引脚"})
        if p in sheet.pins and set(nets) != {sheet.pins[p]}:
            errors.append({"at": p, "reason": "导线与异网引脚接触"})
        if len(nets) > 1:
            if sorted(nets.values()) != [3, 12] or any(
                p in end_or_bend[n] for n in nets
            ):
                errors.append(
                    {
                        "at": p,
                        "nets": sorted(nets),
                        "reason": "异网端点、结点或共线段相接",
                    }
                )
            else:
                crossings += 1
    islands = {}
    required_named = set()
    connected_groups = 0
    for (net, partition), entries in sheet.groups.items():
        graph = segments[net]
        if net not in islands:
            assignment = {}
            for origin in graph:
                if origin in assignment:
                    continue
                assignment[origin] = origin
                queue = deque([origin])
                while queue:
                    at = queue.popleft()
                    for near in graph[at]:
                        if near not in assignment:
                            assignment[near] = origin
                            queue.append(near)
            islands[net] = assignment
            occupied = {
                i
                for p, i in assignment.items()
                if p in sheet.pins and sheet.pins[p] == net
            }
            if occupied != set(assignment.values()):
                errors.append({"net": net, "reason": "存在未接任何引脚的线岛"})
        endpoints = [stub[0] for _, stub in entries]
        assignment = islands[net]
        if (
            any(p not in assignment for p in endpoints)
            or len({assignment[p] for p in endpoints if p in assignment}) != 1
        ):
            errors.append(
                {
                    "net": net,
                    "block": partition,
                    "reason": "应直接连接的引脚未连在同一线树",
                }
            )
        else:
            connected_groups += 1
            if net in sheet.shared or net in sheet.cross_page or len(entries) == 1:
                required_named.add((net, assignment[endpoints[0]]))
    for net, origin in sorted(required_named, key=str):
        if not any(islands[net].get(p) == origin for p in named[net]):
            errors.append({"net": net, "reason": "跨区或跨页的线岛缺少可见网名"})
    return {
        "errors": errors,
        "crossings": crossings,
        "length": length * sheet.grid,
        "connected_groups": connected_groups,
        "groups": len(sheet.groups),
    }


def _text_size(text, size, measure):
    """输入文字、字号与可选字体测量函数，返回图纸单位宽高，拒绝无效尺寸。"""
    if isinstance(size, bool) or not isinstance(size, (int, float)) or not math.isfinite(size) or size <= 0:
        raise ValueError("原理图文字字号必须是有限正数")
    if measure is None:
        return (sum(1 if ord(c) > 127 else 0.65 for c in text) * size + 3, 9 * size / 7)
    dimensions = measure(text, size)
    if not isinstance(dimensions, (list, tuple)) or len(dimensions) != 2 or not all(
        isinstance(v, (int, float)) and math.isfinite(v) and v > 0 for v in dimensions
    ):
        raise ValueError("字体测量函数必须返回有限正数宽高")
    return dimensions


def _text_box(position, size, align):
    """按原生属性锚点、宽高和对齐方式计算文字占用矩形，未知对齐或坐标错误时抛出 ValueError。"""
    if not isinstance(position, (list, tuple)) or len(position) != 2:
        raise ValueError("网名需要两个坐标")
    x, y = position
    _rect((x, y, x, y))
    if not isinstance(align, str):
        raise ValueError(f"不能核验文字对齐方式 {align!r}")
    horizontal, _, vertical = align.partition("_")
    x_offsets = {"LEFT": 0, "CENTER": 0.5, "RIGHT": 1}
    y_offsets = {"BOTTOM": 0, "MIDDLE": 0.5, "TOP": 1}
    if horizontal not in x_offsets or vertical not in y_offsets:
        raise ValueError(f"不能核验文字对齐方式 {align!r}")
    w, h = size
    left, bottom = x-w*x_offsets[horizontal], y-h*y_offsets[vertical]
    return (left, bottom, left+w, bottom+h)


def place_names(page, wires, *, preserve_existing=False, measure=None):
    """在相应导线旁选择可见网名的文字中心。返回找不到无重叠位置的网名。

    preserve_existing 为真时先预留已有 label，按原生线宽复核，再为其余网名选位。
    measure(text, size) 可传入排版时使用的实测字宽和字高。默认延用原有字号估算和搜索。
    返回冲突网名，原地填入缺少的坐标。坐标、对齐或测量尺寸无效时抛出 ValueError。
    """
    bounds = _rect(page["bounds"])
    occupied = list(page.get("obstacles", [])) + list(page.get("label_obstacles", []))
    for n in page.get("notes", []):
        size = n.get("size", 10)
        occupied.append(
            (n["x"], n["y"] - size * 0.3, n["x"] + len(n["text"]) * size, n["y"] + size)
        )
    if preserve_existing:
        for part in page.get("parts", []):
            for key, text in (("designator", part["ref"]), ("value", part.get("value"))):
                style = part.get("style", {}).get(key)
                if text and style:
                    dimensions = _text_size(text, style["size"], measure)
                    occupied.append(_text_box((style["x"], style["y"]), dimensions, style.get("align", "LEFT_MIDDLE")))
    # 原生 LINE 的笔宽是 1 个图纸单位，复核时连笔画边沿一并考虑。
    margin = 0.5 if preserve_existing else 0
    traces = [
        (min(a[0], b[0])-margin, min(a[1], b[1])-margin, max(a[0], b[0])+margin, max(a[1], b[1])+margin)
        for w in wires
        for a, b in zip(w["points"], w["points"][1:])
    ]
    unresolved = []

    def overlaps(a, b):
        return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]

    def outside(rect):
        return rect[0] < bounds[0] or rect[1] < bounds[1] or rect[2] > bounds[2] or rect[3] > bounds[3]

    visible = [w for w in wires if w.get("show_name")]
    for wire in sorted(visible, key=lambda w: (preserve_existing and "label" not in w, len(w["points"]), -len(w["net"]))):
        name = wire["net"]
        style = wire.get("label_style", {})
        w, h = _text_size(name, style.get("size", 7), measure)
        align = style.get("align", "CENTER_MIDDLE")
        if preserve_existing and "label" in wire:
            rect = _text_box(wire["label"], (w, h), align)
            if outside(rect) or any(overlaps(rect, b) for b in itertools.chain(occupied, traces)):
                unresolved.append(name)
            occupied.append(rect)
            continue
        best = None
        for a, b in zip(wire["points"], wire["points"][1:]):
            horizontal = a[1] == b[1]
            length = abs(a[0] - b[0]) + abs(a[1] - b[1])
            for frac in (0.5, 0.25, 0.75, 0, 1):
                px = a[0] + (b[0] - a[0]) * frac
                py = a[1] + (b[1] - a[1]) * frac
                # 紧邻密脚时保留一个字高以内的文字退让，并允许向引线外侧延伸。
                if horizontal:
                    offsets = [
                        (dx, dy)
                        for gap in (6, 8, 10, 13)
                        for dx in (0, -w / 2, w / 2)
                        for dy in (gap, -gap)
                    ]
                else:
                    offsets = [
                        (dx, dy)
                        for gap in (2, 5, 8)
                        for dy in (0, 5, -5)
                        for dx in (-w / 2 - gap, w / 2 + gap)
                    ] + [(0, dy) for dy in (7, -7, 10, -10)]
                for count, (dx, dy) in enumerate(offsets):
                    x, y = px + dx, py + dy
                    pos = [round(x, 2), round(y, 2)]
                    rect = _text_box(pos if preserve_existing else (x, y), (w, h), align)
                    overlaps_count = sum(overlaps(rect, other) for other in occupied)
                    score = (
                        outside(rect),
                        overlaps_count,
                        sum(overlaps(rect, r) for r in traces),
                        count + abs(frac - 0.5),
                        not horizontal,
                        -length,
                    )
                    if best is None or score < best[0]:
                        best = (score, rect, pos)
        if best:
            score, rect, pos = best
            wire["label"] = pos
            occupied.append(rect)
            if score[0] or score[1] or (preserve_existing and score[2]):
                unresolved.append(name)
    return unresolved


def wire_tree_indices(page, wires):
    """返回图页内各物理线树对应的折线下标列表。每棵树共享一个原生 WIRE。

    输入为真实图页和已核验的候选折线。独立检查断连、共线和异网接触，失败抛出 ValueError。
    只沿同网且实际相接的折线端点合并。相同网名但分开的岛各自保留。
    """
    audit = audit_wires(page, wires)
    if audit["errors"]:
        raise ValueError("原生网名分配前的导线检查失败：" + str(audit["errors"][:3]))
    by_endpoint = defaultdict(list)
    for i, wire in enumerate(wires):
        for point in (wire["points"][0], wire["points"][-1]):
            by_endpoint[wire["net"], tuple(point)].append(i)
    visited = set()
    islands = []
    for start in range(len(wires)):
        if start in visited:
            continue
        visited.add(start)
        island = []
        queue = [start]
        while queue:
            i = queue.pop()
            island.append(i)
            branch = wires[i]
            for pt in (branch["points"][0], branch["points"][-1]):
                for neighbor in by_endpoint[branch["net"], tuple(pt)]:
                    if neighbor not in visited:
                        queue.append(neighbor)
                        visited.add(neighbor)

        islands.append(island)
    return islands


def native_name_seeds(page, wires, *, measure=None):
    """从已布成的真实线树中选择原生网名载体，适用于保存时强制显示 NET 的编辑器。

    入参为 route_page 的原始图页和已核对的 wires。返回新的 wires 和 label_conflicts，
    每个同网物理岛仅一条线 explicit_net 为真。其余线的逻辑 net 仍保留在候选中。
    已指定的载体和文字坐标保留原值，未定位的网名通过原有算法搜索位置。可传入排字用的
    measure(text, size) 复核文字尺寸。若有重复载体、断线或意外接触则抛出 ValueError。
    """
    islands = wire_tree_indices(page, wires)
    out = [dict(w) for w in wires]
    carriers = []
    for island in islands:

        def rank(index):
            w = wires[index]
            horizontal = max(
                (
                    abs(a[0] - b[0])
                    for a, b in itertools.pairwise(w["points"])
                    if a[1] == b[1]
                ),
                default=0,
            )
            return w.get("show_name", False), horizontal, -index

        reviewed = [i for i in island if wires[i].get("explicit_net")]
        if len(reviewed) > 1:
            raise ValueError(f"同一原生线树指定了多个网名载体：{wires[reviewed[0]]['net']}")
        chosen = reviewed[0] if reviewed else max(island, key=rank)
        carriers.append(chosen)
        for i in island:
            out[i]["explicit_net"] = out[i]["show_name"] = i == chosen
            if i != chosen:
                out[i].pop("label", None)
    conflicts = place_names(page, out, preserve_existing=True, measure=measure)
    return {
        "wires": out,
        "explicit_net_count": len(carriers),
        "wire_trees": islands,
        "label_conflicts": conflicts,
    }


def route_page(page):
    """为图页候选布线。入参含 bounds、真实 pins、实体 obstacles 与搜索预算。

    返回正交折线、显示网名标志、未布通清单和预算统计。输入坐标、端点或预算无效时
    抛出 ValueError 或 KeyError。无法连接的局部网络保留在 unrouted，不授权写入。
    """
    t0 = time.monotonic()
    limit = page.get("max_expansions", 500000)
    seconds = page.get("max_seconds", 30)
    if (
        not isinstance(limit, int)
        or limit < 1
        or not math.isfinite(seconds)
        or seconds <= 0
    ):
        raise ValueError("搜索预算必须为正")
    sheet = _Sheet(page)
    budget = {"left": limit, "deadline": t0 + seconds}

    def order(item):
        (net, _), entries = item
        xs = [p[-1][0] for _, p in entries]
        ys = [p[-1][1] for _, p in entries]
        return net in sheet.shared, max(xs) - min(xs) + max(ys) - min(ys), net

    if not sheet.errors:
        for group, entries in sorted(sheet.groups.items(), key=order):
            sheet.paths(group, entries, budget)
    wires = sheet.emit()
    label_conflicts = place_names(page, wires)
    proof = _audit(sheet, wires)
    return {
        "wires": wires,
        "unrouted": proof["errors"],
        "completed": not proof["errors"],
        "geometry": {k: v for k, v in proof.items() if k != "errors"},
        "label_conflicts": label_conflicts,
        "stats": {
            "pins": len(page["pins"]),
            "wires": len(wires),
            "expansions": limit - budget["left"],
            "elapsed_seconds": round(time.monotonic() - t0, 4),
        },
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("page", type=Path)
    parser.add_argument("out", type=Path)
    parser.add_argument(
        "--native-names",
        action="store_true",
        help="在每个物理线岛仅保留一个显式原生网名",
    )
    args = parser.parse_args()
    if args.page.resolve() == args.out.resolve():
        parser.error("输出路径须区别于输入")
    page = json.loads(args.page.read_text(encoding="utf-8-sig"))
    result = route_page(page)
    if args.native_names and result["completed"]:
        result.update(native_name_seeds(page, result["wires"]))
    args.out.write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return 0 if result["completed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
