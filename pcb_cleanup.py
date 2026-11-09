"""有界布线后处理：精检候选，保护原铜，死枝和单层接入孔只取证。"""
from __future__ import annotations

import argparse
from copy import deepcopy
import json
import math
from pathlib import Path
import time

from shapely.geometry import Point

from pcb_pipeline_common import (PipelineError, normalize_board, rules_for, strict_options,
                                 finite, copper_and_drills, fingerprint)
from pcb_routing import check_routed_nets, _is_octilinear, _octilinear_doglegs, _trace


OPTION_KEYS = {"enabled", "straighten", "regularize_45", "remove_redundant_vias",
               "ground_nets", "shield_nets", "signal_nets", "deferred_nets", "protected_trace_ids", "protected_via_ids",
               "max_candidates", "max_passes", "max_seconds"}


def validate_options(options, board):
    """校验配置并补默认值；输入为配置/完整板，返回新字典，非法字段或引用抛PipelineError。"""
    out = strict_options(options, OPTION_KEYS, "cleanup")
    for key in ("enabled", "straighten", "regularize_45", "remove_redundant_vias"):
        out.setdefault(key, True)
        if type(out[key]) is not bool:
            raise PipelineError(f"cleanup.{key} 必须为布尔值")
    for key in ("ground_nets", "shield_nets", "signal_nets", "deferred_nets", "protected_trace_ids", "protected_via_ids"):
        values = out.setdefault(key, [])
        if not isinstance(values, list) or any(not isinstance(v, str) or not v for v in values) or len(set(values)) != len(values):
            raise PipelineError(f"cleanup.{key} 必须为唯一非空字符串列表")
        if key.endswith("nets"):
            allowed = set(board["nets"])
        else:
            kind = "traces" if key == "protected_trace_ids" else "vias"
            allowed = {item.get("id") for item in board["routes"].get(kind, [])}
        if set(values) - allowed:
            raise PipelineError(f"cleanup.{key} 引用未知对象")
    for key, default, limit in (("max_candidates", 500, 100000), ("max_passes", 3, 100)):
        out.setdefault(key, default)
        if type(out[key]) is not int or not 1 <= out[key] <= limit:
            raise PipelineError(f"cleanup.{key} 必须为1..{limit}整数")
    out["max_seconds"] = finite(out.get("max_seconds", 15), "cleanup.max_seconds", positive=True)
    return out


class _Budget:
    def __init__(self, options, stats, started):
        self.start, self.options, self.stats = started, options, stats

    def check(self, candidate=False):
        if time.monotonic() - self.start >= self.options["max_seconds"]:
            raise _Stop("max_seconds")
        if candidate:
            if self.stats["candidate_checks"] >= self.options["max_candidates"]:
                raise _Stop("max_candidates")
            self.stats["candidate_checks"] += 1


class _Stop(Exception):
    pass


def _length(traces):
    return sum(math.hypot(t["ex"]-t["sx"], t["ey"]-t["sy"]) for t in traces)


def _object(kind, index, item):
    return {"kind": kind, "index": index, "id": item.get("id"), "net": item["net"],
            "geometry_fingerprint": fingerprint(item)}


def _source_prefix(board, original):
    """保护真实原铜前缀和身份；输入完整板与原始板，返回各类旧铜数量，不符即拒绝。"""
    if original["nets"] != board["nets"] or original["components"] != board["components"]:
        raise PipelineError("cleanup原始板须与候选具有相同完整网表及器件姿态")
    for key in ("board_bbox", "rules"):
        if original.get(key) != board.get(key):
            raise PipelineError(f"cleanup原始板与候选的{key}不同")
    if original.get("metadata", {}).get("source") != board.get("metadata", {}).get("source"):
        raise PipelineError("cleanup原始板与候选来源身份不同")
    counts = {}
    for kind in ("traces", "vias", "shield_vias", "holes", "keepouts", "copper_pours"):
        old, current = original["routes"].get(kind, []), board["routes"].get(kind, [])
        if current[:len(old)] != old:
            raise PipelineError(f"cleanup不能覆盖或重排原始{kind}")
        counts[kind] = len(old)
    return counts


def _protected_nets(board, options):
    nets = set(options["ground_nets"]) | set(options["shield_nets"]) | set(options["deferred_nets"])
    for net, tag in board.get("tags", {}).get("net_tags", {}).items():
        if tag.get("type") == "GND" or tag.get("need_shield_check"):
            nets.add(net)
    for rule in board.get("metadata", {}).get("pipeline_constraints", []):
        if rule.get("operator") == "COMPANION_GEN":
            nets.add(rule["ground_net"])
            nets.update(rule["nets"])
        if rule.get("operator") == "CHANNEL_SPEC" and ("diff_pair" in rule or "length_group" in rule):
            # 差分耦合/补长由专门模块处理，通用拉直不破坏其设计目的。
            nets.add(rule["net"])
            if "diff_pair" in rule:
                nets.update((rule["diff_pair"]["p"], rule["diff_pair"]["n"]))
    return nets


def _metadata_protected(item):
    meta = item.get("metadata", {})
    return bool(item.get("locked") or meta.get("parent_component_id") or meta.get("source_only")
                or meta.get("shield") or meta.get("shielding") or meta.get("ground_stitch")
                or meta.get("length_tuned") or meta.get("diff_pair"))


def _contacts(board, kind, index, budget):
    """以真实铜交叠记录对象邻接；返回逐层接触名称及铺铜证据，非连通证明。"""
    copper, _ = copper_and_drills(board)
    item = board["routes"][kind][index]
    name = f"trace {index}" if kind == "traces" else f"via {index}"
    own = [c for c in copper if c["name"] == name]
    touched = {}
    for a in own:
        budget.check()
        layer = a["layers"][0]
        for b in copper:
            if b["name"] == name or b["net"] != item["net"] or b["layers"] != a["layers"]:
                continue
            overlap = a["inner"].intersection(b["inner"])
            tol = rules_for(board).contact_tolerance
            if overlap.area > tol*tol or overlap.length > tol:
                touched.setdefault(layer, set()).add(b["name"])
    return {str(layer): sorted(names) for layer, names in sorted(touched.items())}


def _via_evidence(board, index, options, protected, budget):
    via = board["routes"]["vias"][index]
    contacts = _contacts(board, "vias", index, budget)
    reasons = []
    if via["net"] in protected:
        reasons.append("地网、屏蔽、延期网络或明确匹配网络受保护")
    if via["net"] not in options["signal_nets"]:
        reasons.append("未显式声明为可优化的非接地非屏蔽信号网")
    if _metadata_protected(via):
        reasons.append("锁定、附属或明确用途元数据受保护")
    if via.get("id") in options["protected_via_ids"]:
        reasons.append("显式保护ID")
    if any(name.startswith("pour ") for values in contacts.values() for name in values):
        reasons.append("真实铜接触铺铜")
    # 同网同位置屏蔽孔不可因去重图只留下via记录而误删。
    if any(v["net"] == via["net"] and v["x"] == via["x"] and v["y"] == via["y"]
           for v in board["routes"].get("shield_vias", [])):
        reasons.append("与屏蔽孔位置重合")
    row = _object("via", index, via)
    row.update(contact_layers=sorted(int(k) for k in contacts), contacts=contacts,
               touches_pour=any(name.startswith("pour ") for names in contacts.values() for name in names),
               protected_reasons=reasons, geometry_only=True)
    return row


def _listed_candidates(board, options, protected, budget):
    """仅列死枝/单层接入孔及证据；不删除对象、不把几何缺少接触等同用途缺失。"""
    copper, _ = copper_and_drills(board)
    listed = []
    tol = rules_for(board).contact_tolerance
    for i, trace in enumerate(board["routes"].get("traces", [])):
        budget.check()
        ends = []
        for end, xy in (("start", (trace["sx"], trace["sy"])), ("end", (trace["ex"], trace["ey"]))):
            probe = Point(*xy).buffer(max(tol*2, trace["width"]*0.01))
            names = sorted({c["name"] for c in copper if c["name"] != f"trace {i}"
                            and c["net"] == trace["net"] and trace["layer"] in c["layers"]
                            and c["inner"].intersects(probe)})
            if not names:
                ends.append({"end": end, "point": list(xy), "contact_objects": names})
        if ends:
            row = _object("trace", i, trace)
            row.update(candidate_type="suspected_dead_branch", free_endpoints=ends,
                       auto_delete=False, proof_complete=False,
                       reason="端点局部未见其他同网铜；测试支线、回流或用途尚未证明，默认保留")
            listed.append(row)
    for i, via in enumerate(board["routes"].get("vias", [])):
        budget.check()
        row = _via_evidence(board, i, options, protected, budget)
        if len(row["contact_layers"]) <= 1:
            row.update(candidate_type="single_layer_or_unconnected_via", auto_delete=False, proof_complete=False,
                       reason="只在至多一个实际铜层接触；用途/铺铜/接地/屏蔽仍须逐对象证明，默认保留")
            listed.append(row)
    return listed


def cleanup_board(board, options=None, *, original_board=None):
    """生成后处理副本并返回逐对象证据，绝不执行原生写回。

    入参：完整已布信号板、严格配置及可选同来源同姿态的布线前原板。
    仅修改原板铜前缀之后的新图元；省略原板则全部原铜受保护。出参：
    (候选板,统计)，包含变化与列出的未删候选。原检查器拒绝非法候选；
    配置/来源/输入几何错误抛PipelineError，预算耗尽保留已精检结果并明确未找到更多。
    """
    started = time.monotonic()
    result = normalize_board(board)
    options = validate_options(options, result)
    original = normalize_board(board if original_board is None else original_board)
    counts = _source_prefix(result, original)
    protected = _protected_nets(result, options)
    selected = [n for n in result["nets"] if n not in options["deferred_nets"]]
    errors = check_routed_nets(result, selected)
    stats = dict(enabled=options["enabled"], candidate_checks=0, passes=0, changes=[], listed_candidates=[],
                 candidate_listing_complete=False,
                 passed=False, input_errors=errors, optimization_skipped_due_input_errors=bool(errors),
                 budget_exhausted=False, full_board_accepted=False, original_copper_preserved=True,
                 routed_scope=selected, deferred_nets=list(options["deferred_nets"]),
                 initial_length_mil=_length(result["routes"].get("traces", [])),
                 initial_via_count=len(result["routes"].get("vias", [])))
    budget = _Budget(options, stats, started)

    def editable(trace, index):
        return (index >= counts["traces"] and trace["net"] not in protected and not _metadata_protected(trace)
                and trace.get("id") not in options["protected_trace_ids"])

    def accept(candidate, change):
        nonlocal result
        budget.check(candidate=True)
        failures = check_routed_nets(candidate, selected)
        if failures:
            stats.setdefault("rejections", []).append({"operation": change["operation"], "errors": failures[:8]})
            return False
        _source_prefix(candidate, original)
        result = candidate
        stats["changes"].append(change)
        return True

    try:
        if options["enabled"] and not errors:
            for pass_index in range(options["max_passes"]):
                budget.check()
                stats["passes"] = pass_index+1
                changed = False
                traces = result["routes"].get("traces", [])
                if options["straighten"]:
                    for i in range(counts["traces"], len(traces)):
                        budget.check()
                        a = traces[i]
                        if not editable(a, i):
                            continue
                        for j in range(i+1, len(traces)):
                            budget.check()
                            b = traces[j]
                            if not editable(b, j) or any(a[k] != b[k] for k in ("net", "layer", "width")):
                                continue
                            aa, bb = [(a["sx"], a["sy"]), (a["ex"], a["ey"])], [(b["sx"], b["sy"]), (b["ex"], b["ey"])]
                            joint = [(x, y) for x in range(2) for y in range(2) if aa[x] == bb[y]]
                            if len(joint) != 1:
                                continue
                            x, y = joint[0]
                            start, end = aa[1-x], bb[1-y]
                            if start == end or math.dist(start, end) >= _length([a,b])-1e-8:
                                continue
                            paths = _octilinear_doglegs(start, end) if options["regularize_45"] else [[start, end]]
                            for path in paths:
                                replacements = [_trace(a["net"], a["layer"], p, q, a["width"])
                                                for p,q in zip(path,path[1:]) if p != q]
                                if _length(replacements) >= _length([a,b])-1e-8:
                                    continue
                                trial = deepcopy(result)
                                trial["routes"]["traces"] = [t for k,t in enumerate(traces) if k not in (i,j)] + replacements
                                if accept(trial, {"operation":"rubber_band", "before":[_object("trace",i,a),_object("trace",j,b)],
                                                  "after":deepcopy(replacements), "removed_length_mil":_length([a,b])-_length(replacements)}):
                                    changed = True
                                    break
                            if changed:
                                break
                        if changed:
                            break
                if not changed and options["regularize_45"]:
                    for i, trace in enumerate(traces):
                        budget.check()
                        start, end = (trace["sx"], trace["sy"]), (trace["ex"], trace["ey"])
                        if not editable(trace, i) or _is_octilinear(start,end):
                            continue
                        for path in _octilinear_doglegs(start,end):
                            replacements = [_trace(trace["net"],trace["layer"],p,q,trace["width"])
                                            for p,q in zip(path,path[1:]) if p != q]
                            trial = deepcopy(result)
                            trial["routes"]["traces"] = traces[:i] + traces[i+1:] + replacements
                            if accept(trial, {"operation":"regularize_45", "before":[_object("trace",i,trace)],
                                              "after":deepcopy(replacements)}):
                                changed = True
                                break
                        if changed:
                            break
                if not changed and options["remove_redundant_vias"]:
                    for i, via in enumerate(result["routes"].get("vias", [])):
                        budget.check()
                        if i < counts["vias"]:
                            continue
                        evidence = _via_evidence(result, i, options, protected, budget)
                        if evidence["protected_reasons"] or len(evidence["contact_layers"]) <= 1:
                            continue
                        trial = deepcopy(result)
                        del trial["routes"]["vias"][i]
                        if accept(trial, {"operation":"remove_redundant_via", "before":evidence,
                                          "proof":"原完整信号连通/硬几何/拓扑检查再次通过，且无铺铜/地网/屏蔽/附属接触证据"}):
                            changed = True
                            break
                if not changed:
                    break
            else:
                if changed:
                    stats.update(budget_exhausted=True, stopped_by="max_passes",
                                 message="后处理轮数预算耗尽，保留已验收候选；未证明不存在进一步改进")
        stats["listed_candidates"] = _listed_candidates(result, options, protected, budget)
        stats["candidate_listing_complete"] = True
    except _Stop as exc:
        stats.update(budget_exhausted=True, stopped_by=str(exc),
                     message="显式预算内未找到更多已验收改进；未证明无解")
    final_errors = check_routed_nets(result, selected)
    if final_errors and stats["changes"]:
        raise PipelineError("cleanup最终候选未通过原验收：" + "; ".join(final_errors))
    _source_prefix(result, original)
    stats.update(final_length_mil=_length(result["routes"].get("traces", [])),
                 passed=not final_errors, final_errors=final_errors,
                 final_via_count=len(result["routes"].get("vias", [])),
                 elapsed_seconds=time.monotonic()-budget.start,
                 source_identity_preserved=result.get("metadata", {}).get("source") == original.get("metadata", {}).get("source"))
    if stats["elapsed_seconds"] >= options["max_seconds"]:
        stats.update(budget_exhausted=True,stopped_by="max_seconds")
    return result, stats


def main(argv=None):
    """读取JSON并输出独立候选/证据；文件与配置错误返回非零，不生成原生写回计划。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--board", required=True, type=Path)
    parser.add_argument("--original-board", type=Path)
    parser.add_argument("--options", type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        read = lambda p: json.loads(p.read_text(encoding="utf-8-sig"))
        solved, report = cleanup_board(read(args.board), read(args.options) if args.options else None,
            original_board=read(args.original_board) if args.original_board else None)
        args.output_dir.mkdir(parents=True, exist_ok=False)
        solved.setdefault("metadata", {})["preview_only"] = True
        for name, value in (("board.json",solved),("result.json",report)):
            (args.output_dir/name).write_text(json.dumps(value,ensure_ascii=False,indent=2,allow_nan=False)+"\n",encoding="utf-8")
        return 0 if report["passed"] else 2
    except (OSError, ValueError, TypeError, KeyError) as exc:
        print(json.dumps({"passed":False,"error":str(exc)},ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
