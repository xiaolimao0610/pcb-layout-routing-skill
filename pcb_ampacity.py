"""用 IPC-2152 数据的公开拟合估算静止空气基线线宽，只提高现有下限。

依据 Brooks/Adam《Trace Currents and Temperatures Revisited》(2015)，式3-2、
表3-1及附录6；不是 IPC 发布的通用闭式公式，也不是实板温升认证。
仅用标量解析运算，不进行无预算搜索；模型条件和来源随结果返回。
"""
from __future__ import annotations

import argparse
import json
import math

from pcb_pipeline_common import PipelineError, finite, rules_for
from pcb_width_policy import hard_width_minimum


MODEL = "ipc2152_brooks_adam_still_air_2015"
SOURCES = [
    "https://www.ipc.org/TOC/IPC-2152.pdf",
    "https://www.mathscinotes.com/wp-content/uploads/2016/06/pcbtempr.pdf",
    "https://docs.kicad.org/doxygen/tracks__width__versus__current__formula_8h_source.html",
]
# 内层取公开拟合系数范围的上端；不把不同铜厚四舍五入成相邻的标称铜厚。
INTERNAL = {0.5: (130.0, 2.0, -1.10, -1.52),
            1.0: (200.0, 1.9, -1.10, -1.52),
            2.0: (300.3, 2.0, -1.15, -1.52),
            3.0: (300.0, 1.9, -1.15, -1.52)}
EXTERNAL = (215.3, 2.0, -1.15, -1.0)
CURRENT_FIELDS = {"current_a", "temp_rise_c", "copper_oz"}


def validate_current_rule(rule):
    """校验 CHANNEL_SPEC 的电流三参数，不改变输入。

    入参：规则字典；无电流参数时返回None并保持旧规则。有电流时三个参数
    必须同时出现，范围为0..20A、1..100℃、0.5/1/2/3oz。非法或超出本模块
    有限拟合范围抛PipelineError；范围限制是本实现边界，不是IPC强制限额。
    """
    if not isinstance(rule, dict):
        raise PipelineError("载流规则必须为对象")
    present = CURRENT_FIELDS & set(rule)
    if not present:
        return
    if present != CURRENT_FIELDS:
        raise PipelineError("CHANNEL_SPEC 电流计算必须同时提供 current_a、temp_rise_c、copper_oz")
    current = finite(rule["current_a"], "current_a", minimum=0)
    rise = finite(rule["temp_rise_c"], "temp_rise_c", positive=True)
    copper = finite(rule["copper_oz"], "copper_oz", positive=True)
    if current > 20 or not 1 <= rise <= 100 or copper not in INTERNAL:
        raise PipelineError("IPC2152_FIT_RANGE: 本拟合只接受0..20A、1..100℃及0.5/1/2/3oz；超界需实际曲线或热模型")


def minimum_width(current_a, temp_rise_c, copper_oz, *, internal=False):
    """计算单类铜层的基线下限，返回带原始宽度、系数和模型边界的证据。

    入参：电流A、允许温升℃、标称铜厚oz及严格布尔内层标识。出参字典的
    width_mil为向上保守舍入的建议下限。0A返回0；正电流低于5mil的拟合值
    取5mil，不外推细线。超过200mil或数值非有限抛PipelineError，不夹到上限。
    厚度沿用原论文1oz=1.3mil，不等同成品铜厚保证；不扣除平面散热收益。
    """
    validate_current_rule(dict(current_a=current_a, temp_rise_c=temp_rise_c, copper_oz=copper_oz))
    if type(internal) is not bool:
        raise PipelineError("internal 必须为布尔值")
    current, rise, oz = float(current_a), float(temp_rise_c), float(copper_oz)
    thickness = 1.3 * oz
    k, a, b, c = INTERNAL[oz] if internal else EXTERNAL
    raw = 0.0 if current == 0 else math.exp((math.log(rise) - math.log(k) - a*math.log(current)
                                            - c*math.log(thickness)) / b)
    if not math.isfinite(raw) or raw > 200.0:
        raise PipelineError("IPC2152_FIT_RANGE: 计算需要超过200mil的线宽，未在本拟合有效范围内找到宽度；需实际曲线或热模型")
    width = 0.0 if current == 0 else math.ceil(max(5.0, raw)*10000.0)/10000.0
    return dict(model=MODEL, layer_kind="internal" if internal else "external",
                current_a=current, temp_rise_c=rise, copper_oz=oz, nominal_thickness_mil=thickness,
                nominal_thickness_um=thickness*25.4, raw_width_mil=raw, width_mil=width,
                lower_model_floor_applied=bool(current and raw < 5),
                coefficients=dict(K=k, a=a, b=b, c=c),
                formula="delta_T = K * I**a * W_mil**b * thickness_mil**c",
                coefficient_policy="内层取已发表系数范围上端，外层用式3-2；不取相邻铜厚替代",
                environment="still_air_baseline", sources=list(SOURCES),
                standard_compliance_certified=False,
                excluded_effects=["成品铜厚公差", "邻近热源与空气流动", "具体板厚/材料/铜面散热修正",
                                  "焊盘和铜颈载流", "过孔孔壁载流", "并联分流与瞬态", "真空环境"])


def current_width_requirement(board, rule, tag=None):
    """按真实允许层逐一计算并取最大线宽要求，返回证据或None。

    入参为完整规范板、CHANNEL_SPEC和可选当前网络标签；不修改任何输入。
    出参包含每层模型与最终max下限。未知网络/层、空层交集、输入范围错误
    抛PipelineError。电流未给出返回None，保持原规则；不检查实板温升。
    """
    validate_current_rule(rule)
    if "current_a" not in rule:
        return None
    net = rule.get("net")
    if not isinstance(net, str) or net not in board["nets"]:
        raise PipelineError("电流规则引用未知网络")
    rules = rules_for(board)
    stack = list(rules.copper_layers)
    if tag is None:
        tag = board.get("tags", {}).get("net_tags", {}).get(net, {})
    allowed = set(stack)
    for fields in (rule, tag):
        if "allowed_layers" in fields:
            layers = fields["allowed_layers"]
            if (not isinstance(layers, (list, tuple)) or not layers
                    or any(type(layer) is not int or layer not in stack for layer in layers)
                    or len(set(layers)) != len(layers)):
                raise PipelineError("电流规则 allowed_layers 必须为唯一真实铜层列表")
            allowed &= set(layers)
    if not allowed:
        raise PipelineError("电流规则没有共同允许铜层")
    per_layer = []
    for layer in stack:
        if layer in allowed:
            estimate = minimum_width(rule["current_a"], rule["temp_rise_c"], rule["copper_oz"],
                                     internal=layer not in (stack[0], stack[-1]))
            per_layer.append(dict(layer=layer, **estimate))
    calculated = max(value["width_mil"] for value in per_layer)
    explicit = [rule.get("min_width", 0), tag.get("min_width", 0), tag.get("width", 0)]
    if "diff_pair" in rule:
        explicit.append(rule["diff_pair"]["width"])
    values = [finite(value, "已有线宽下限", minimum=0) for value in explicit]
    # 仅在没有显式宽度声明时保留普通信号默认10mil；模型下限始终参与最终max。
    existing = max(hard_width_minimum(board, net, tag), *values, 0.0 if any(values) else 10.0)
    return dict(net=net, model=MODEL, calculated_width_mil=calculated, existing_minimum_mil=existing,
                selected_min_width_mil=max(calculated, existing), per_layer=per_layer,
                scope="基于IPC-2152数据的静止空气拟合线宽下限；不包含焊盘/孔/铺铜及实际热验收",
                standard_compliance_certified=False)


def apply_current_rule(board, rule, tag):
    """将计算下限并入给定网络标签，返回计算证据或None。

    入参为完整规范板、规则及可写标签字典；仅更新该标签min_width，不缩窄
    原宽度、不修改既有铜。配置错误按PipelineError传播，且发生在标签更新前。
    """
    evidence = current_width_requirement(board, rule, tag)
    if evidence is not None:
        tag["min_width"] = evidence["selected_min_width_mil"]
    return evidence


def current_errors(board, rule):
    """从参数重新计算每条实际走线的下限，不信任缓存统计或已写入标签。

    入参为完整板和规则。出参为明确对象的错误字符串列表；不变更输入。
    无电流约束返回空列表；无效配置抛PipelineError，由原约束入口按失败处理。
    本检查只验证trace宽度，不能据无错误声称铺铜、孔或整板载流已验收。
    """
    evidence = current_width_requirement(board, rule)
    if evidence is None:
        return []
    minimum = evidence["selected_min_width_mil"]
    return [f"CHANNEL_SPEC_CURRENT: {rule['net']} trace {trace.get('id', index)} width {trace['width']:g} < {minimum:g}mil"
            for index, trace in enumerate(board.get("routes", {}).get("traces", []))
            if trace["net"] == rule["net"] and trace["width"] < minimum]


def main(argv=None):
    """读取单次参数并输出可复核JSON；非法值退出1，不改PCB或文件。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--current-a", type=float, required=True)
    parser.add_argument("--temp-rise-c", type=float, required=True)
    parser.add_argument("--copper-oz", type=float, required=True)
    parser.add_argument("--internal", action="store_true")
    args = parser.parse_args(argv)
    try:
        print(json.dumps(minimum_width(args.current_a, args.temp_rise_c, args.copper_oz,
                                       internal=args.internal), ensure_ascii=False, allow_nan=False, indent=2))
        return 0
    except (PipelineError, ValueError, OverflowError) as exc:
        print(json.dumps(dict(passed=False, error=str(exc)), ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
