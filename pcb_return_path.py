"""对真实铺铜进行双向几何回流复核，分别输出线宽、保护带与孔净空变化。"""
from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import time

from shapely.geometry import Point, GeometryCollection
from shapely.ops import unary_union

from pcb_pipeline_common import (PipelineError, normalize_board, rules_for, strict_options, finite,
                                 check_board, copper_and_drills, fingerprint, polygons, polygon_record, CollisionModel,
                                 _fast_trace_bounds)
from pcb_routing import _groups


OPTION_KEYS = {"signal_nets", "reference_layers", "guard_band", "min_neck_width", "max_checks", "max_seconds"}


def validate_options(options, board):
    """输入明确的参考层/网络映射和信号网，输出规范配置；未知字段和非相邻层抛PipelineError。"""
    out = strict_options(options, OPTION_KEYS, "return_path")
    names = out.get("signal_nets")
    if (not isinstance(names, list) or not names or any(not isinstance(n,str) for n in names)
            or len(set(names)) != len(names) or set(names)-set(board["nets"])):
        raise PipelineError("return_path.signal_nets 必须为非空且唯一的实际信号网列表")
    refs = out.get("reference_layers")
    if not isinstance(refs,list) or not refs:
        raise PipelineError("return_path.reference_layers 必须为明确且非空的层/参考网列表")
    stack, seen = list(rules_for(board).copper_layers), set()
    for row in refs:
        if not isinstance(row,dict) or set(row) != {"signal_layer","reference_layer","reference_net"}:
            raise PipelineError("参考映射只接受signal_layer/reference_layer/reference_net三个字段")
        a,b,n = row["signal_layer"],row["reference_layer"],row["reference_net"]
        if type(a) is not int or type(b) is not int or a not in stack or b not in stack or abs(stack.index(a)-stack.index(b)) != 1:
            raise PipelineError("参考层必须是实际物理叠层中的相邻铜层")
        if not isinstance(n,str) or n not in board["nets"] or n in names:
            raise PipelineError("reference_net 必须为与信号不同的明确实际网络")
        if (a,b,n) in seen:
            raise PipelineError("参考层映射重复")
        seen.add((a,b,n))
    for key,default in (("guard_band",10), ("min_neck_width",0)):
        out[key] = finite(out.get(key,default), f"return_path.{key}", minimum=0)
    out.setdefault("max_checks",10000)
    if type(out["max_checks"]) is not int or not 1 <= out["max_checks"] <= 1000000:
        raise PipelineError("return_path.max_checks 必须为1..1000000整数")
    out["max_seconds"] = finite(out.get("max_seconds",30),"return_path.max_seconds",positive=True)
    return out


class _Budget:
    def __init__(self, options, report, started):
        self.start,self.options,self.report = started,options,report

    def check(self, count=False):
        if time.monotonic()-self.start >= self.options["max_seconds"]:
            raise _Stop("max_seconds")
        if count:
            if self.report["checks"] >= self.options["max_checks"]:
                raise _Stop("max_checks")
            self.report["checks"] += 1


class _Stop(Exception):
    pass


def _area_evidence(geometry):
    """返回可定位的实际缺失多边形证据；大几何限制报告顶点，标记是否完整。"""
    parts = polygons(geometry)
    remaining, records, complete = 4000, [], True
    for part in parts:
        count = len(part.exterior.coords)+sum(len(h.coords) for h in part.interiors)
        if count > remaining:
            complete = False
            continue
        records.append(polygon_record(part))
        remaining -= count
    return dict(area_mil2=float(geometry.area), bounds=list(geometry.bounds) if not geometry.is_empty else [],
                polygons=records, geometry_evidence_complete=complete)


def _reference_surfaces(board, options, budget, label):
    """仅使用已连接真实端子的该网实铜，返回每参考层的可证明铺铜及悬浮证据。"""
    copper, _ = copper_and_drills(board)
    refs, evidence = {}, []
    for net in sorted({r["reference_net"] for r in options["reference_layers"]}):
        budget.check()
        groups = _groups(board,net,budget)
        anchored = [g for g in groups if g["terminals"]]
        for layer in sorted({r["reference_layer"] for r in options["reference_layers"] if r["reference_net"] == net}):
            budget.check()
            actual_pours = unary_union([c["inner"] for c in copper if c["net"]==net and layer in c["layers"] and c["name"].startswith("pour ")])
            connected = unary_union([g["layers"].get(layer,GeometryCollection()) for g in anchored])
            proven = actual_pours.intersection(connected)
            floating = actual_pours.difference(proven)
            refs[(layer,net)] = proven
            evidence.append(dict(snapshot=label,reference_layer=layer,reference_net=net,
                                 anchored_terminal_groups=[sorted(g["terminals"]) for g in anchored],
                                 actual_pour_area_mil2=float(actual_pours.area), proven_area_mil2=float(proven.area),
                                 floating_area_mil2=float(floating.area),
                                 evidence_available=not proven.is_empty))
    return refs,evidence


def _projection(trace, guard=0, tolerance=0.001):
    """用原圆端线路保守外界投影，保护带等效增宽，不遗漏线端圆帽。"""
    return _fast_trace_bounds(dict(trace,width=trace["width"]+2*guard),tolerance).outer


def _trace_record(index, trace, mapping, scope):
    return dict(trace_index=index,trace_id=trace.get("id"),net=trace["net"],signal_layer=trace["layer"],
                reference_layer=mapping["reference_layer"],reference_net=mapping["reference_net"],
                start=[trace["sx"],trace["sy"]],end=[trace["ex"],trace["ey"]],width_mil=trace["width"],scope=scope)


def _new_geometry(before,after):
    """按完整图元指纹/重复数识别新增或改动铜与孔，不把数量相等当几何未变。"""
    result = []
    for kind in ("vias","shield_vias","traces","copper_pours","holes"):
        counts = Counter(fingerprint(v) for v in before["routes"].get(kind,[]))
        for index,item in enumerate(after["routes"].get(kind,[])):
            key = fingerprint(item)
            if counts[key]:
                counts[key] -= 1
            else:
                result.append((kind,index,item))
    return result


def _same_surface_connectivity(surface,trace):
    """检查线路两端投影是否仍能落在同一实际参考面连通分量上。"""
    a,b = Point(trace["sx"],trace["sy"]),Point(trace["ex"],trace["ey"])
    return any(p.covers(a) and p.covers(b) for p in polygons(surface))


def check_return_paths(board, options, *, before=None):
    """复核铺铜后的双向几何回流风险，返回完整结构化报告，不修改工程。

    入参：完整实际填铜板、明确信号/相邻参考层配置，以及可选修改前快照。
    出参：线宽投影缺失、保护带缺失、孔净空变化三类独立列表及原信号
    参考面变化/连通证据。未给before、缺少锚定铜或预算耗尽均不算双向完成。
    配置/来源/格式错误抛PipelineError；普通缺铜与几何违规作为报告证据返回。
    这是铜面几何风险检查，不是频域回流、电场求解或原生DRC通过证明。
    """
    started = time.monotonic()
    current = normalize_board(board)
    options = validate_options(options,current)
    prior = normalize_board(before) if before is not None else None
    if prior is not None:
        if tuple(rules_for(prior).copper_layers) != tuple(rules_for(current).copper_layers):
            raise PipelineError("双向回流复核不接受叠层变化")
        a = prior.get("metadata",{}).get("source",{}).get("document_uuid")
        b = current.get("metadata",{}).get("source",{}).get("document_uuid")
        if not a or not b or a != b:
            raise PipelineError("双向快照必须绑定同一真实文档UUID；来源指纹允许随实际修改变化")
        if not set(options["signal_nets"]).issubset(prior["nets"]):
            raise PipelineError("修改前快照缺少所声明信号网络")
    report = dict(passed=False,complete=False,full_board_accepted=False,checks=0,budget_exhausted=False,
                  forward_complete=False,backward_complete=False,
                  trace_projection_missing=[],guard_band_missing=[],via_clearance_change=[],
                  old_signal_reference_changes=[],reference_disconnections=[],reference_evidence=[],unknown=[],
                  geometry_errors=check_board(current,ignore_connectivity=True),
                  input_fingerprint=fingerprint(current),before_fingerprint=fingerprint(prior) if prior else None,
                  scope="真实实铜及端子锚定的双向几何复核，非电磁场或制造验收")
    budget = _Budget(options,report,started)
    tolerance = rules_for(current).contact_tolerance
    try:
        surfaces,evidence = _reference_surfaces(current,options,budget,"after")
        report["reference_evidence"].extend(evidence)
        for row in evidence:
            if not row["evidence_available"] or row["floating_area_mil2"] > tolerance*tolerance:
                report["unknown"].append({"code":"REFERENCE_COPPER_NOT_FULLY_ANCHORED",**row})
        signal_traces = [(i,t) for i,t in enumerate(current["routes"].get("traces",[])) if t["net"] in options["signal_nets"]]
        for net in options["signal_nets"]:
            if not any(t["net"]==net for _,t in signal_traces):
                report["unknown"].append({"code":"NO_SIGNAL_TRACES","net":net})
        for index,trace in signal_traces:
            mappings = [r for r in options["reference_layers"] if r["signal_layer"]==trace["layer"]]
            if not mappings:
                report["unknown"].append({"code":"NO_REFERENCE_MAPPING","trace_index":index,"signal_layer":trace["layer"]})
            for mapping in mappings:
                budget.check(count=True)
                plane = surfaces[(mapping["reference_layer"],mapping["reference_net"])]
                inner = _projection(trace,tolerance=rules_for(current).curve_tolerance)
                outer = _projection(trace,options["guard_band"],rules_for(current).curve_tolerance)
                missing = inner.difference(plane)
                record = _trace_record(index,trace,mapping,"after_signal")
                if missing.area > tolerance*tolerance:
                    report["trace_projection_missing"].append({**record,"code":"TRACE_WIDTH_PROJECTION_MISSING",**_area_evidence(missing)})
                guard_missing = outer.difference(inner).difference(plane)
                if guard_missing.area > tolerance*tolerance:
                    report["guard_band_missing"].append({**record,"code":"GUARD_BAND_MISSING","guard_band_mil":options["guard_band"],**_area_evidence(guard_missing)})
                if options["min_neck_width"]:
                    radius = options["min_neck_width"]/2
                    opened = plane.buffer(-radius).buffer(radius)
                    thin = inner.intersection(plane).difference(opened)
                    if thin.area > tolerance*tolerance:
                        report["guard_band_missing"].append({**record,"code":"REFERENCE_NECK_BELOW_LIMIT",
                            "min_neck_width_mil":options["min_neck_width"],**_area_evidence(thin)})
                if not _same_surface_connectivity(plane,trace):
                    report["reference_disconnections"].append({**record,"code":"REFERENCE_COMPONENT_DISCONTINUITY"})
        report["forward_complete"] = True
        if prior is None:
            report["unknown"].append({"code":"BEFORE_SNAPSHOT_REQUIRED","message":"缺少修改前实铜，尚未完成反向影响复核"})
        else:
            old_surfaces,old_evidence = _reference_surfaces(prior,options,budget,"before")
            report["reference_evidence"].extend(old_evidence)
            copper,drills = copper_and_drills(current)
            model = CollisionModel(current)
            new_items = _new_geometry(prior,current)
            for index,trace in enumerate(prior["routes"].get("traces",[])):
                if trace["net"] not in options["signal_nets"]:
                    continue
                mappings = [r for r in options["reference_layers"] if r["signal_layer"]==trace["layer"]]
                if not mappings:
                    report["unknown"].append({"code":"OLD_SIGNAL_NO_REFERENCE_MAPPING","trace_index":index})
                for mapping in mappings:
                    budget.check(count=True)
                    key = (mapping["reference_layer"],mapping["reference_net"])
                    old_plane,new_plane = old_surfaces[key],surfaces[key]
                    if old_plane.is_empty:
                        report["unknown"].append({"code":"BEFORE_REFERENCE_MISSING",**_trace_record(index,trace,mapping,"old_signal")})
                    corridor = _projection(trace,options["guard_band"],rules_for(prior).curve_tolerance)
                    lost = old_plane.difference(new_plane).intersection(corridor)
                    record = _trace_record(index,trace,mapping,"old_signal")
                    causes = []
                    for kind,number,item in new_items:
                        budget.check()
                        names = ([f"via {number}"] if kind=="vias" else
                                 [f"via {number+len(current['routes'].get('vias',[]))}"] if kind=="shield_vias" else
                                 [f"trace {number}"] if kind=="traces" else
                                 [f"NPTH {number}"] if kind=="holes" else
                                 [f"pour {number}.{j}" for j in range(len(item["filled_polygons"]))])
                        ref_net = mapping["reference_net"]
                        same_net = item.get("net") == ref_net
                        # 同参考网新增孔仅以实际钻孔影响定位；同网铜不会产生异网反焊盘。
                        records = drills if same_net else copper+drills
                        shapes = [c["outer"] for c in records if c["name"] in names and mapping["reference_layer"] in c["layers"]]
                        if not shapes:
                            continue
                        # 此包络只定位可能成因；实际缺铜量始终来自前后真实参考铜差集。
                        clearance = (model.rules.npth_clearance if kind == "holes" else 0.0 if same_net else
                                     model.rules.get_clearance(model.net_type(ref_net),model.net_type(item.get("net", ""))))
                        influence = unary_union(shapes).buffer(clearance)
                        affected = lost.intersection(influence)
                        if affected.area <= tolerance*tolerance:
                            continue
                        cause = dict(kind=kind,index=number,id=item.get("id"),net=item.get("net"),
                                     required_clearance_mil=clearance,
                                     attribution="新增对象净空邻域与真实失铜交叠，非热/电磁因果证明")
                        causes.append(cause)
                        if kind in ("vias","shield_vias","holes"):
                            report["via_clearance_change"].append({**record,"code":"VIA_CLEARANCE_REFERENCE_LOSS",
                                "object":cause,**_area_evidence(affected)})
                    if lost.area > tolerance*tolerance:
                        report["old_signal_reference_changes"].append({**record,"code":"OLD_SIGNAL_REFERENCE_LOSS",
                            "possible_new_objects":causes,**_area_evidence(lost)})
                    if _same_surface_connectivity(old_plane,trace) and not _same_surface_connectivity(new_plane,trace):
                        report["reference_disconnections"].append({**record,"code":"PREVIOUS_REFERENCE_COMPONENT_SPLIT",
                                                                   "possible_new_objects":causes})
            report["backward_complete"] = True
        budget.check()
    except _Stop as exc:
        report.update(budget_exhausted=True,stopped_by=str(exc))
        report["unknown"].append({"code":"CHECK_BUDGET","message":"预算内未完成全部复核，未检查项不能视为通过"})
    report["complete"] = report["forward_complete"] and report["backward_complete"] and not report["unknown"] and not report["budget_exhausted"]
    report["passed"] = report["complete"] and not any(report[k] for k in (
        "trace_projection_missing","guard_band_missing","via_clearance_change","old_signal_reference_changes",
        "reference_disconnections","geometry_errors"))
    report["elapsed_seconds"] = time.monotonic()-budget.start
    return report


def main(argv=None):
    """读取前后JSON快照并输出分项报告；检查失败返回2，输入或文件错误返回1。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--board",required=True,type=Path)
    parser.add_argument("--before",type=Path)
    parser.add_argument("--options",required=True,type=Path)
    parser.add_argument("--output",required=True,type=Path)
    args = parser.parse_args(argv)
    try:
        if args.output.exists():
            raise PipelineError("回流报告输出文件已存在，请选择新路径")
        read = lambda p: json.loads(p.read_text(encoding="utf-8-sig"))
        result = check_return_paths(read(args.board),read(args.options),before=read(args.before) if args.before else None)
        args.output.write_text(json.dumps(result,ensure_ascii=False,indent=2,allow_nan=False)+"\n",encoding="utf-8")
        return 0 if result["passed"] else 2
    except (OSError,ValueError,TypeError,KeyError) as exc:
        print(json.dumps({"passed":False,"error":str(exc)},ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
