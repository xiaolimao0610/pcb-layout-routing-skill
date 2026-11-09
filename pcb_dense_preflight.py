"""报告密脚区域的局部净空与工艺限制；不把局部采样当作整板可布性证明。"""
from __future__ import annotations

import itertools
import time

from pcb_pipeline_common import CollisionModel, PipelineError, normalize_board, finite


def inspect_channels(board, net, routing_options=None, *, max_pairs=1000, max_seconds=5.0):
    """输入完整板、目标网和路由参数，返回同封装异网焊盘最短间隙报告。

    比较该最短横截面是否容纳线宽和两侧真实间距，不推断其他位置无法绕行。
    输出包含被检查数量、截断状态及工艺尺寸，不产生候选或修改输入。
    非法网名/参数抛PipelineError；预算耗尽返回未完成标记，不报告无解。
    """
    from pcb_routing import _net_options, algorithm_options
    if type(max_pairs) is not int or not 1 <= max_pairs <= 100000:
        raise PipelineError("max_pairs须为1至100000的整数")
    seconds = finite(max_seconds, "局部净空检查秒数", positive=True)
    started = time.monotonic()
    board = normalize_board(board)
    if net not in board["nets"]:
        raise PipelineError("净空检查需要真实目标网络")
    options = algorithm_options(routing_options)
    settings = _net_options(board, net, options)
    model = CollisionModel(board)
    groups = {}
    for copper in model.copper:
        terminal = copper.get("terminal")
        if terminal and copper["net"] != net:
            groups.setdefault(terminal[0], []).append(copper)
    rows, examined, exhausted = [], 0, False
    for cid, pads in groups.items():
        for a, b in itertools.combinations(pads, 2):
            if a["terminal"] == b["terminal"]:
                continue
            layers = sorted(set(a["layers"]) & set(b["layers"]) & set(settings["layers"]))
            if not layers:
                continue
            if examined >= max_pairs or time.monotonic()-started >= seconds:
                exhausted = True
                break
            examined += 1
            gap = a["outer"].distance(b["outer"])
            required = settings["width"] + sum(model.rules.get_clearance(model.net_type(net), model.net_type(p["net"])) for p in (a, b))
            if gap < required:
                rows.append({"component": cid, "pads": [list(a["terminal"]), list(b["terminal"])],
                             "layers": layers, "shortest_gap_mil": gap, "required_gap_mil": required,
                             "interpretation": "最短横截面不足；不代表其他位置不可绕行或整网无解"})
        if exhausted:
            break
    return {"net": net, "scope": "local_pad_gap_only", "width_mil": settings["width"],
            "via_diameter_mil": settings["diameter"], "via_drill_mil": settings["hole"],
            "new_via_technology": "complete_stack_through", "trace_layers": list(settings["layers"]),
            "examined_pairs": examined, "budget_exhausted": exhausted, "completed_scan": not exhausted,
            "narrow_cross_sections": rows, "elapsed_seconds": time.monotonic()-started,
            "whole_board_feasibility": "not_proven", "full_board_accepted": False}
