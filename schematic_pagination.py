"""按可阅读尺寸摆放功能块。矩形包括符号、字号、局部导线及标签，无需依靠缩小字体合页。"""

import argparse
import json
import math
import time
from pathlib import Path


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


def _rect(values):
    if (not isinstance(values, (tuple, list)) or len(values) != 4 or not all(map(_number, values))
            or values[2] <= values[0] or values[3] <= values[1]):
        raise ValueError("矩形应为 [左,下,右,上]，且宽、高为正")
    return tuple(values)


def pack_blocks(spec):
    """按共享模板分页。入参为 bounds、blocked、blocks 及可选预算。返回各块左下角和页码。

    各块宽高取可读版式的外包络。related 显式指定跨块关联。输入无效抛出 ValueError。
    候选搜索到达上限时返回未排入的块及原因；完整结果仍须原生布线及网表复核。
    """
    left, bottom, right, top = _rect(spec["bounds"])
    blocked = [_rect(r) for r in spec.get("blocked", [])]
    blocks = spec["blocks"]
    if not isinstance(blocks, list) or not blocks:
        raise ValueError("至少需要一个功能块")
    gap, page_limit = spec.get("gap", 10), spec.get("max_pages", len(blocks))
    search_limit, seconds = spec.get("max_candidates", 100000), spec.get("max_seconds", 5)
    if (not _number(gap) or gap < 0 or type(page_limit) is not int or page_limit <= 0 or
            type(search_limit) is not int or search_limit < 1 or not _number(seconds) or seconds <= 0):
        raise ValueError("间距或预算无效")
    references = set()
    related = {}
    for item in blocks:
        name = item.get("id")
        if (not isinstance(name, str) or not name or name in references
                or not all(_number(item.get(k)) and item[k] > 0 for k in ("width", "height"))):
            raise ValueError("功能块标识或尺寸无效")
        neighbors = item.get("related", [])
        if not isinstance(neighbors, list) or not all(isinstance(n, str) for n in neighbors):
            raise ValueError("related 应为功能块标识列表")
        related[name] = set(neighbors)
        references.add(name)
    for name, peers in related.items():
        if name in peers or peers - references:
            raise ValueError("related 中存在自身或未知功能块")
    # 先摆大块。高度、面积均相同的重复通道保持输入顺序，以便横向对照。
    ordered = sorted(blocks, key=lambda b: (-b["height"], -b["width"]))
    pages, placements = [], {}
    checked = 0
    deadline = time.monotonic() + seconds

    def result(reason=None):
        return {"complete": reason is None, "reason": reason, "pages_used": len(pages),
                "placements": placements, "pending": [b["id"] for b in blocks if b["id"] not in placements],
                "candidates_checked": checked}

    for item in ordered:
        name, w, h = item["id"], item["width"], item["height"]
        best = None
        for index in range(min(page_limit, len(pages) + 1)):
            if index == len(pages) and best is not None:
                break
            occupied = blocked + (pages[index] if index < len(pages) else [])
            xs, ys = {left}, {top-h}
            for x0, y0, x1, y1 in occupied:
                xs.update([x1 + gap, x0 - gap - w])
                ys.update([y0 - gap - h, y1 + gap])
            for y in sorted(ys, reverse=True):
                for x in sorted(xs):
                    checked += 1
                    if checked > search_limit or time.monotonic() >= deadline:
                        return result("预算内未找到完整排版")
                    if x < left or x+w > right or y < bottom or y+h > top:
                        continue
                    if any(x < c+gap and x+w > a-gap and y < d+gap and y+h > b-gap
                           for a,b,c,d in occupied):
                        continue
                    neighbors = [v for n,v in placements.items()
                                 if v["page"] == index+1 and (n in related[name] or name in related[n])]
                    distance = sum(abs(x+w/2-n["x"]-n["width"]/2)+abs(y+h/2-n["y"]-n["height"]/2)
                                   for n in neighbors)
                    rank = (-len(neighbors), distance, index, -y, x)
                    if best is None or rank < best[0]:
                        best = (rank, index, x, y)
        if best is None:
            return result("给定纸张、间距与页数内未找到可用位置")
        _, index, x, y = best
        if index == len(pages):
            pages.append([])
        pages[index].append((x, y, x+w, y+h))
        placements[name] = {"page":index+1, "x":x, "y":y, "width":w, "height":h}
    return result()


def main():
    """按 JSON 输入生成排版候选；成功返回 0，未排完或输入无效返回 2。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    if args.input.resolve() == args.output.resolve() or (args.output.exists() and args.output.samefile(args.input)):
        parser.error("输出路径与输入重合")
    try:
        result = pack_blocks(json.loads(args.input.read_text(encoding="utf-8-sig")))
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    except (KeyError, ValueError, OSError) as exc:
        parser.error(str(exc))
    return 0 if result["complete"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
