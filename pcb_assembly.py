"""显式装配净距与丝印几何质量筛查；只报告证据和候选，不改变板或硬验收规则。"""
from __future__ import annotations

import argparse
from itertools import combinations
import json
from pathlib import Path
import time

from shapely.geometry import shape as geojson_shape
from shapely.ops import nearest_points

from pcb_geometry import shape_from_spec, transform
from pcb_pipeline_common import (PipelineError, normalize_board, rules_for, strict_options,
                                 finite, fingerprint, _fast_pad_bounds, _fast_trace_bounds)
from pcb_placement import _envelope
from pcb_solver_hardened import _body, _layers


OPTION_KEYS = {"source", "high_components", "silkscreen", "silkscreen_expected_ids",
               "silkscreen_clearance", "silkscreen_pad_clearance", "max_checks", "max_seconds"}


def _points(value, label):
    """验证有限二维简单多边形，返回坐标；缺项、退化或无效形状抛 PipelineError。"""
    if not isinstance(value, list) or len(value) < 3:
        raise PipelineError(f"{label} 必须为至少三个二维点")
    points = []
    for point in value:
        if not isinstance(point, (list, tuple)) or len(point) != 2:
            raise PipelineError(f"{label} 含非二维点")
        points.append([finite(v, label) for v in point])
    try:
        shape_from_spec({"type": "POLYGON", "points": points}, 0.001)
    except ValueError as exc:
        raise PipelineError(f"{label} 不是有效简单多边形：{exc}") from exc
    return points


def _outline_shape(value):
    """输入显式保守 GeoJSON Polygon/MultiPolygon，返回含原孔洞的实体；无效输入拒绝。

    不用凸包、自动补洞或 buffer(0) 修复不确定形状；每环坐标需有限且显式闭合。
    """
    if (not isinstance(value, dict) or set(value) != {"type", "coordinates"}
            or value["type"] not in ("Polygon", "MultiPolygon")):
        raise PipelineError("outline.geometry 仅接受 Polygon/MultiPolygon 的 type/coordinates")
    polygons = [value["coordinates"]] if value["type"] == "Polygon" else value["coordinates"]
    if not isinstance(polygons, list) or not polygons:
        raise PipelineError("轮廓多边形列表必须非空")
    for polygon in polygons:
        if not isinstance(polygon, list) or not polygon:
            raise PipelineError("轮廓必须有外环，可附真实内孔环")
        for ring in polygon:
            points = _points(ring, "轮廓环")
            if len(points) < 4 or points[0] != points[-1]:
                raise PipelineError("GeoJSON 轮廓环必须显式闭合")
    try:
        shape = geojson_shape(value)
        if shape.is_empty or not shape.is_valid or shape.area <= 0:
            raise PipelineError("轮廓及孔洞拓扑无效，不能自动补洞或合并修复")
        return shape
    except (ValueError, TypeError) as exc:
        raise PipelineError(f"无效轮廓：{exc}") from exc


def validate_options(options, board):
    """入参为显式质量配置和规范板，返回配置副本；未知字段或非法值抛 PipelineError。

    来源不符立即拒绝；缺少来源、对象覆盖或依据由检查报告列为 unknown，不冒充通过。
    """
    out = strict_options(options, OPTION_KEYS, "assembly")
    if not {"high_components", "silkscreen"}.intersection(out):
        raise PipelineError("至少明确提供 high_components 或 silkscreen 检查范围")
    if "source" in out:
        if not isinstance(out["source"], dict) or not out["source"]:
            raise PipelineError("source 必须为当前板的非空来源对象")
        if out["source"] != board.get("metadata", {}).get("source"):
            raise PipelineError("装配/丝印来源与当前板不一致")
    components = {c["id"]: c for c in board["components"]}
    high = out.get("high_components", [])
    if not isinstance(high, list):
        raise PipelineError("high_components 必须为列表")
    seen = set()
    for row in high:
        row = strict_options(row, {"component_id", "envelope", "clearance", "provenance"}, "高件")
        if not {"component_id", "envelope", "clearance"}.issubset(row):
            raise PipelineError("高件必须明确 component_id/envelope/clearance")
        cid = row["component_id"]
        if not isinstance(cid, str) or cid not in components or cid in seen:
            raise PipelineError("高件 ID 必须真实、唯一")
        seen.add(cid)
        _points(row["envelope"], "高件局部包络")
        finite(row["clearance"], "焊接净距", minimum=0)
        if "provenance" in row and not isinstance(row["provenance"], str):
            raise PipelineError("高件 provenance 必须为字符串")
    silk = out.get("silkscreen", [])
    if not isinstance(silk, list):
        raise PipelineError("silkscreen 必须为完整对象列表")
    seen, native_ids = set(), set()
    for row in silk:
        base = {"id", "layer", "kind", "provenance"}
        if not isinstance(row, dict) or row.get("kind") not in ("line", "text_bounds", "outline"):
            raise PipelineError("丝印仅接受 line/text_bounds/outline；不忽略未知对象")
        geometry = ({"sx", "sy", "ex", "ey", "width"} if row["kind"] == "line" else
                    {"points", "bounds_are_conservative"} if row["kind"] == "text_bounds" else
                    {"geometry", "source_ids", "owner_id", "bounds_are_conservative"})
        strict_options(row, base | geometry, "丝印对象")
        if not {"id", "layer"}.issubset(row) or not (geometry - {"bounds_are_conservative", "owner_id"}).issubset(row):
            raise PipelineError("丝印对象缺少 ID、层或真实几何")
        if not isinstance(row["id"], str) or not row["id"].strip() or row["id"] in seen:
            raise PipelineError("丝印 ID 必须非空且唯一")
        seen.add(row["id"])
        if type(row["layer"]) is not int or row["layer"] not in (3, 4):
            raise PipelineError("丝印层必须为顶层3或底层4")
        if "provenance" in row and not isinstance(row["provenance"], str):
            raise PipelineError("丝印 provenance 必须为字符串")
        if row["kind"] == "line":
            for key in geometry:
                finite(row[key], f"丝印{key}", positive=(key == "width"))
            if (row["sx"], row["sy"]) == (row["ex"], row["ey"]):
                raise PipelineError("丝印线段不得退化为零长度")
        elif row["kind"] == "text_bounds":
            _points(row["points"], "文字世界坐标包围多边形")
        else:
            _outline_shape(row["geometry"])
            ids = row["source_ids"]
            if (not isinstance(ids, list) or not ids
                    or any(not isinstance(i, str) or not i.strip() for i in ids)
                    or len(set(ids)) != len(ids)):
                raise PipelineError("outline.source_ids 必须覆盖合并前的唯一原生图元ID")
            if "owner_id" in row and (not isinstance(row["owner_id"], str) or row["owner_id"] not in components):
                raise PipelineError("outline.owner_id 必须为真实器件ID")
        if "bounds_are_conservative" in row and type(row["bounds_are_conservative"]) is not bool:
            raise PipelineError("bounds_are_conservative 必须为布尔值")
        represented = set(row.get("source_ids", [row["id"]]))
        if native_ids.intersection(represented):
            raise PipelineError("同一原生丝印图元不能被多个对象重复代表")
        native_ids.update(represented)
    if "silkscreen_expected_ids" in out:
        ids = out["silkscreen_expected_ids"]
        if (not isinstance(ids, list) or any(not isinstance(i, str) or not i.strip() for i in ids)
                or len(set(ids)) != len(ids)):
            raise PipelineError("silkscreen_expected_ids 必须为唯一非空 ID 的列表")
    for key in ("silkscreen_clearance", "silkscreen_pad_clearance"):
        if key in out:
            out[key] = finite(out[key], key, minimum=0)
    out.setdefault("max_checks", 100000)
    if type(out["max_checks"]) is not int or not 1 <= out["max_checks"] <= 1000000:
        raise PipelineError("max_checks 必须为1..1000000整数")
    out["max_seconds"] = finite(out.get("max_seconds", 30), "max_seconds", positive=True)
    return out


def check_assembly(board, options):
    """输入完整规范板和显式配置，返回质量筛查报告，不修改任何输入。

    净距单位为 mil；高件包络使用器件局部坐标，丝印使用当前来源的世界坐标。
    非法输入抛 PipelineError；证据不足、预算未覆盖或近接均不通过本次筛查。
    passed 只涵盖声明范围，full_board_accepted 始终为 False；不运行原生写回。
    """
    board = normalize_board(board)
    options = validate_options(options, board)
    started = time.monotonic()
    rules = rules_for(board)
    tolerance = rules.curve_tolerance
    components = {c["id"]: c for c in board["components"]}
    high, silk = options.get("high_components", []), options.get("silkscreen", [])
    unknown = []
    if "source" not in options:
        unknown.append("缺少与当前板匹配的 source 来源证据")
    if "high_components" in options and not high:
        unknown.append("高件检查范围为空，未证明全部适用器件均已列出")
    for row in high:
        if not row.get("provenance", "").strip():
            unknown.append(f"高件 {row['component_id']} 缺少包络/净距依据")
    if "silkscreen" in options:
        if "silkscreen_expected_ids" not in options:
            unknown.append("缺少原生丝印对象覆盖清单 silkscreen_expected_ids")
        for key in ("silkscreen_clearance", "silkscreen_pad_clearance"):
            if key not in options:
                unknown.append(f"缺少显式 {key}；仅报告重叠，不判净距已达标")
        for row in silk:
            if not row.get("provenance", "").strip():
                unknown.append(f"丝印 {row['id']} 缺少真实几何/字框依据")
            if row["kind"] == "text_bounds" and row.get("bounds_are_conservative") is not True:
                unknown.append(f"文字 {row['id']} 未证明包围几何保守，不能认定实际字形净距通过")
            if row["kind"] == "outline" and row.get("bounds_are_conservative") is not True:
                unknown.append(f"轮廓 {row['id']} 未证明是原生线宽/曲线的保守实体")
    expected = options.get("silkscreen_expected_ids", [])
    observed = [row["id"] for row in silk]
    observed_native = [identity for row in silk for identity in row.get("source_ids", [row["id"]])]
    missing, extra = sorted(set(expected) - set(observed_native)), sorted(set(observed_native) - set(expected))
    if "silkscreen_expected_ids" in options and (missing or extra):
        unknown.append("原生丝印覆盖清单与提供对象不一致")
    if not silk and "silkscreen" in options:
        unknown.append("丝印清单为空，未证明当前板没有可检查丝印")
    report = dict(scope="显式高件焊接净距与保守丝印几何筛查", passed=False,
                  full_board_accepted=False, native_writeback=False, findings=[], candidate_rules=[],
                  unknown=unknown, source=board.get("metadata", {}).get("source"),
                  board_fingerprint=fingerprint(board), checks=0, complete=True,
                  budget_exhausted=False, input_inventory=dict(
                      components=list(components), high_components=[r["component_id"] for r in high],
                      pads=[f"{c['id']}.{p['padId']}" for c in board["components"] for p in c["pads"]],
                      silkscreen=observed, silkscreen_expected=expected,
                      silkscreen_observed_source_ids=observed_native,
                      silkscreen_missing=missing, silkscreen_extra=extra),
                  evidence_limitations=["文字框重叠是潜在字形冲突，不能声称真实字体轮廓相交",
                      "线段采用含笔宽及端帽的保守外界；近接仅针对提供的几何与依据",
                      "独立质量筛查不代替硬DRC、原生字体显示、装配工艺或整板验收"],
                  max_checks=options["max_checks"], max_seconds=options["max_seconds"])
    envelopes = {cid: _envelope(c, tolerance) for cid, c in components.items()}
    high_shapes = {}
    for row in high:
        c = components[row["component_id"]]
        local = shape_from_spec({"type": "POLYGON", "points": row["envelope"]}, tolerance)
        shape = transform(local, c["x"], c["y"], c["rotation"], c["layer"] == 2).outer
        if not shape.covers(_body(c, tolerance).outer):
            raise PipelineError(f"高件 {c['id']} 显式包络未覆盖真实本体，不得缩小对象")
        high_shapes[c["id"]] = shape
    silk_shapes = {row["id"]: (_fast_trace_bounds(row, tolerance).outer if row["kind"] == "line"
        else _outline_shape(row["geometry"]) if row["kind"] == "outline"
        else shape_from_spec({"type": "POLYGON", "points": row["points"]}, tolerance).outer) for row in silk}
    pads = []
    for component in board["components"]:
        for pad in component["pads"]:
            layers = _layers(pad, component["layer"], rules, spanning=bool(pad.get("hole")))
            pads.append((f"{component['id']}.{pad['padId']}",
                         {3 if layer == 1 else 4 for layer in layers if layer in (1, 2)},
                         _fast_pad_bounds(component, pad, tolerance).outer))

    def pairs():
        # ponytail: 按预算逐对检查适合当前百余器件板；大清单可再增加空间索引，不截掉对象。
        for row in high:
            cid = row["component_id"]
            for other, component in components.items():
                if other != cid and component["layer"] == components[cid]["layer"]:
                    action = "review_fixed_component" if component.get("fixed") or component.get("locked") else "reposition_component"
                    yield ("assembly_clearance", cid, other, components[cid]["layer"],
                           high_shapes[cid], envelopes[other], row["clearance"], action, False)
        for a, b in combinations(silk, 2):
            if a["layer"] == b["layer"]:
                yield ("silkscreen_clearance", a["id"], b["id"], a["layer"],
                       silk_shapes[a["id"]], silk_shapes[b["id"]], options.get("silkscreen_clearance", 0),
                       "reposition_silkscreen", "text_bounds" in (a["kind"], b["kind"]))
        for row in silk:
            for pid, layers, shape in pads:
                if row["layer"] in layers:
                    yield ("silkscreen_pad_clearance", row["id"], pid, row["layer"],
                           silk_shapes[row["id"]], shape, options.get("silkscreen_pad_clearance", 0),
                           "reposition_silkscreen", row["kind"] == "text_bounds")

    processed = set()
    for kind, aid, bid, layer, a, b, gap, action, potential in pairs():
        if report["checks"] >= options["max_checks"] or time.monotonic() - started >= options["max_seconds"]:
            report.update(complete=False, budget_exhausted=True)
            unknown.append("检查预算耗尽，存在未覆盖对象对，不能判定全部质量通过")
            break
        report["checks"] += 1
        processed.update((aid, bid))
        distance = a.distance(b)
        overlap = a.intersects(b)
        if not overlap and distance >= gap:
            continue
        points = nearest_points(a, b)
        finding = dict(kind=kind, objects=[aid, bid], layer=layer,
            measured_clearance_mil=distance, required_clearance_mil=gap,
            overlap_or_touch=overlap, overlap_area_mil2=a.intersection(b).area,
            nearest_points=[[point.x, point.y] for point in points],
            bounds=[list(a.bounds), list(b.bounds)], potential_text_conflict=potential,
            severity="quality_advisory", hard_drc_failure=False)
        report["findings"].append(finding)
        report["candidate_rules"].append(dict(action=action, objects=[aid, bid], layer=layer,
            target_clearance_mil=gap, auto_apply=False, preserve_objects=True,
            preserve_text_size=True, requires_hard_recheck=True,
            rationale="调整位置后重新计算实际净距；保留来源、器件/焊盘/文字及原硬约束"))
    report["processed_object_ids"] = sorted(processed)
    report["elapsed_seconds"] = time.monotonic() - started
    report["passed"] = report["complete"] and not unknown and not report["findings"]
    return report


def main(argv=None):
    """读取 --board/--options 并写独立报告；退出0为范围内通过，2为发现/未知，1为输入错误。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--board", required=True)
    parser.add_argument("--options", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    try:
        if Path(args.output).resolve() in {Path(args.board).resolve(), Path(args.options).resolve()}:
            raise PipelineError("输出报告不得覆盖板文件或配置文件")
        board = json.loads(Path(args.board).read_text(encoding="utf-8"))
        options = json.loads(Path(args.options).read_text(encoding="utf-8"))
        report = check_assembly(board, options)
        Path(args.output).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"passed": report["passed"], "findings": len(report["findings"]),
                          "unknown": report["unknown"], "output": args.output}, ensure_ascii=False))
        return 0 if report["passed"] else 2
    except (OSError, ValueError, TypeError, KeyError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
