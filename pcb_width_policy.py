"""区分实际制造下限与未声明线宽时的普通信号默认值。"""
from pcb_pipeline_common import PipelineError, finite, rules_for


def hard_width_minimum(board, net, tag=None):
    """输入板、网络及可选标签，返回真实硬下限；无效数值抛PipelineError。

    不含普通信号默认10mil，不改变原规则、已有铜或任何输入。
    """
    if net not in board["nets"]:
        raise PipelineError("线宽引用未知网络")
    if tag is None:
        tag = board.get("tags", {}).get("net_tags", {}).get(net, {})
    return max(rules_for(board).minimum_trace_width,
               *(finite(tag.get(key, 0), f"{net}.{key}", minimum=0)
                 for key in ("width", "min_width")))


def selected_width(board, net, width=None):
    """输入板、网络和显式宽度，返回实际选宽；低于硬下限抛PipelineError。

    width为None时保留普通信号max(10mil,硬下限)默认；不修改输入或已有铜。
    """
    minimum = hard_width_minimum(board, net)
    chosen = max(10.0, minimum) if width is None else finite(width, "线宽", positive=True)
    if chosen < minimum:
        raise PipelineError(f"routing {net}: width {chosen:g} is below required {minimum:g}")
    return chosen
