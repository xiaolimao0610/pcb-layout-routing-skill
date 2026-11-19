"""为原理图的位号与参数寻找相邻空白位置，返回候选坐标和未放下的原文。"""
from __future__ import annotations

import math
import time
import unicodedata


def _number(value, title):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{title}必须是有限数")
    return value


def _point(value):
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise ValueError("二维坐标需要两个数")
    return tuple(_number(v, "坐标") for v in value)


def _rect(value):
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        raise ValueError("矩形需要四个坐标")
    x1, y1, x2, y2 = (_number(v, "矩形坐标") for v in value)
    if x1 > x2 or y1 > y2:
        raise ValueError("矩形范围颠倒")
    return x1, y1, x2, y2


def _approximate(text, size):
    """估计未指定字体的排印宽高；可传入原生字体实测函数替换。"""
    units = sum(0 if unicodedata.combining(c) else
                (1 if unicodedata.east_asian_width(c) in ("W", "F") or c in "MW@%" else 0.76)
                for c in text)
    return (units+0.25)*size, 1.3*size


def _measure(f, text, size):
    w, h = _point(f(text, size))
    if w <= 0 or h <= 0:
        raise ValueError("非空文本的测量宽高必须为正")
    return w, h


def _wire_obstacles(routed, measure, label_size):
    result = []
    for wire in routed["wires"]:
        pts = [_point(p) for p in wire["points"]]
        if len(pts) < 2:
            raise ValueError("导线至少需要两点")
        for (x, y), (ex, ey) in zip(pts, pts[1:]):
            if x != ex and y != ey:
                raise ValueError("输入候选含非正交导线")
            result.append((min(x, ex), min(y, ey), max(x, ex), max(y, ey)))
        if wire.get("show_name"):
            # 与 schematic_wiring 的 label 协议一致，坐标为水平文字中心。
            x, y = _point(wire.get("label"))
            w, h = _measure(measure, wire["net"], label_size)
            result.append((x-w/2, y-h/2, x+w/2, y+h/2))
    return result


def _options(options):
    settings = dict(ref_size=7.5, value_size=7, wire_name_size=7, gap=2, max_offset=48,
                    max_candidates_per_part=320, max_candidates=100000, max_seconds=10)
    unknown = set(options) - set(settings)
    if unknown:
        raise ValueError(f"不支持的排字选项 {sorted(unknown)}")
    settings.update(options)
    for key, value in settings.items():
        _number(value, key)
        if value <= 0 or (key.startswith("max_candidates") and not isinstance(value, int)):
            raise ValueError(f"{key}必须为正，候选数必须为整数")
    return settings


def _candidates(body, width, height, config):
    x1, y1, x2, y2 = body
    cx, cy = (x1+x2)/2, (y1+y2)/2
    limit, gap, em = config["max_offset"], config["gap"], config["ref_size"]
    # 有限的四侧候选；先在符号近处寻找，再增加边缘距离或沿边滑动。
    distances = sorted({min(limit, gap+0.01+em*k) for k in (0, 0.5, 1, 2, 3, 4, 6)})
    shifts = sorted({factor*min(limit, em*k) for k in (0, 1, 2, 3, 4, 6) for factor in (-1, 1)})
    choices = set()
    for d in distances:
        for s in shifts:
            choices.update(((cx+s-width/2, y2+d+height), (cx+s-width/2, y1-d),
                            (x2+d, cy+s+height/2), (x1-d-width, cy+s+height/2)))

    def distance(p):
        x, top = p
        dx, dy = max(0, x-x2, x1-x-width), max(0, top-height-y2, y1-top)
        return (math.hypot(dx,dy) + 0.15*math.hypot(x+width/2-cx, top-height/2-cy), -top, x)

    return sorted(choices, key=distance)[:config["max_candidates_per_part"]]


def annotate_page(page, routed, options=None, measure=None):
    """在已布线图页排列位号和参数，所有坐标沿用原理图图纸单位，正 Y 朝上。

    入参 page 提供 bounds、parts（ref、value、body）及可选 obstacles（含标题、图框等禁占矩形）。
    routed 提供 wires；可见网名采用 schematic_wiring 的水平居中 label。options 控制大小、最小间隙、
    最大偏移与搜索预算。measure 是可选的字体测量函数，入参为文字、字号，返回图纸单位的宽、高。
    出参 styles 以位号索引，包含左中对齐的候选、包围盒；unplaced 保留未排文字及证据。
    本函数不改输入对象。预算耗尽时返回局部结果，输入无效或测量不合规时抛出 ValueError。
    """
    config = _options(options or {})
    metric = measure or _approximate
    bounds = _rect(page["bounds"])
    obstacles = [_rect(o) for o in page.get("obstacles", [])]
    obstacles.extend(_wire_obstacles(routed, metric, config["wire_name_size"]))
    by_ref = {}
    for part in page["parts"]:
        ref, value = part["ref"], part["value"]
        if not isinstance(ref, str) or not ref.strip() or ref in by_ref or not isinstance(value, str):
            raise ValueError("器件位号须唯一且非空，参数文字须为字符串")
        if any(c in ref+value for c in "\r\n"):
            raise ValueError("本模块只排列单行位号和单行参数")
        body = _rect(part["body"])
        by_ref[ref] = (value, body)
        obstacles.append(body)
    deadline = time.monotonic()+config["max_seconds"]
    styles, unplaced = {}, []
    total = 0
    for ref, (value, body) in sorted(by_ref.items()):
        rw, rh = _measure(metric, ref, config["ref_size"])
        vw, vh = _measure(metric, value, config["value_size"]) if value else (0,0)
        gap = config["gap"]
        width, height = max(rw,vw), rh+(gap+vh if value else 0)
        tried = 0
        reason = "在限定邻域内未找到无重叠位置"
        for x, top in _candidates(body, width, height, config):
            if total >= config["max_candidates"] or time.monotonic() >= deadline:
                reason = "总候选数或时间预算耗尽"
                break
            tried += 1
            total += 1
            a,b,c,d = x-gap, top-height-gap, x+width+gap, top+gap
            if a < bounds[0] or b < bounds[1] or c > bounds[2] or d > bounds[3]:
                continue
            if any(a <= ex and c >= ox and b <= ey and d >= oy for ox,oy,ex,ey in obstacles):
                continue
            style = {"designator":{"x":x,"y":top-rh/2,"size":config["ref_size"],"align":"LEFT_MIDDLE"}}
            if value:
                style["value"] = {"x":x,"y":top-rh-gap-vh/2,"size":config["value_size"],"align":"LEFT_MIDDLE"}
            box = [x, top-height, x+width, top]
            styles[ref] = {"style":style,"bounds":box}
            obstacles.append(box)
            break
        if ref not in styles:
            unplaced.append({"ref":ref,"value":value,"action":"preserve_existing",
                             "reason":reason,"candidates_checked":tried})
    return {"completed":not unplaced, "styles":styles, "unplaced":unplaced,
            "candidates_checked":total, "metrics":"supplied" if measure else "estimated"}
