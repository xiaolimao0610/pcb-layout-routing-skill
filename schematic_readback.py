"""对嘉立创读回的各页导线和引脚做独立的物理直连统计。"""
import argparse
import json
import math
from collections import Counter, defaultdict
from pathlib import Path
from schematic_netlist import unique_keys


def parse(path):
    records = []
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        if line.strip():
            header, body = line.removesuffix("|").split("||", 1)
            records.append((json.loads(header, object_pairs_hook=unique_keys),
                            json.loads(body, object_pairs_hook=unique_keys)))
    return records


def on(p, s):
    a, b = s
    return (a[0] == b[0] == p[0] or a[1] == b[1] == p[1]) and all(
        min(x, y) <= z <= max(x, y) for x, y, z in zip(a, b, p))


def pin_on_segment(point, segment):
    """实测旋转引脚的浮点尾差按至多 8 个 ULP 核对；线段之间始终使用原始端点精确比较。"""
    a, b = segment
    scale = max(1.0, *(abs(x) for xy in (point, a, b) for x in xy))
    eps = min(8 * math.ulp(scale), 1e-10)
    return (a[0] == b[0] and abs(a[0]-point[0]) <= eps or
            a[1] == b[1] and abs(a[1]-point[1]) <= eps) and all(
        min(x,y)-eps <= z <= max(x,y)+eps for x,y,z in zip(a,b,point))


def _verify_page_components(rows, components, ids_by_ref):
    """核对原生页和实测器件清单。可提供位号到网表 UID 的索引；身份、姿态或引脚属性冲突抛出 ValueError。"""
    native = {}
    attrs = {}
    for header, body in rows:
        if header["type"] == "COMPONENT":
            uid = header["id"]
            if uid in native:
                raise ValueError(f"原生图页器件 ID 重复 {uid!r}")
            native[uid] = body
        if header["type"] == "ATTR" and body.get("key") in {"Designator", "Unique ID", "Pin Number", "NO_CONNECT"}:
            key = (body["parentId"], body["key"])
            if key in attrs:
                raise ValueError(f"原生图页身份或引脚属性重复 {key!r}")
            attrs[key] = body.get("value")
    seen, seen_pins = set(), set()
    for comp in components:
        uid, ref = comp.get("PrimitiveId"), comp.get("Designator")
        if uid not in native or uid in seen:
            raise ValueError(f"原生图页与几何的器件不一致或重复：{ref!r} / {uid!r}")
        seen.add(uid)
        part = native[uid]
        if (part["x"], -part["y"], part["rotation"] % 360, part["isMirror"]) != (
            comp["X"], comp["Y"], comp["Rotation"] % 360, comp["Mirror"]
        ):
            raise ValueError(f"原生图页与实测器件姿态不一致：{ref!r} / {uid}")
        if comp.get("ComponentType") == "sheet":
            if ref or comp["pins"] or (uid, "Designator") in attrs:
                raise ValueError(f"图框带有电气器件信息 {uid}")
            continue
        if not ref or attrs.get((uid, "Designator")) != ref or attrs.get((uid, "Unique ID"), uid) != uid:
            raise ValueError(f"图页器件身份与实测结果不一致：{ref!r} / {uid}")
        if ids_by_ref is not None and ids_by_ref.get(ref) != uid:
            raise ValueError(f"网表与图页器件身份不一致：{ref!r} / {uid}")
        for pin in comp["pins"]:
            pid, number = pin["PrimitiveId"], str(pin["PinNumber"])
            if not isinstance(pid, str) or not pid or pid in seen_pins or str(attrs.get((pid, "Pin Number"), number)) != number:
                raise ValueError(f"原生引脚身份重复或引脚号不一致：{ref}.{number}")
            owner = pid.rpartition("-")[0]
            if owner in native and owner != uid:
                raise ValueError(f"实测引脚 {pid} 归属于其他器件：{ref}.{number}")
            seen_pins.add(pid)
            flag = attrs.get((pid, "NO_CONNECT"))
            if flag not in (None, "no", "yes") or pin["NoConnected"] is not (flag == "yes"):
                raise ValueError(f"原生 NC 标记与实测引脚不一致：{ref}.{number}")
    if native.keys() != seen:
        raise ValueError(f"几何缺少原生图页中的器件 {sorted(native.keys() - seen)}")
    for owner, key in attrs:
        if (key in {"Designator", "Unique ID"} and owner not in native
                or key in {"Pin Number", "NO_CONNECT"} and owner not in seen_pins):
            raise ValueError(f"原生属性缺少对应的器件或引脚：{owner!r} / {key}")


def page_report(source, geometry, pins_by_ref, *, ids_by_ref=None):
    """输入同一页原生 .esch、实测引脚文件及全板引脚索引，返回物理直连、NC 标记及异常。

    ids_by_ref 为整板原生网表的位号、器件 UID 对应关系。原生 WIRE/LINE 可以由同一连续线树
    继承网名。纯内部十字交叉不计结点。身份、姿态不符或缺少配套文件、字段时抛出异常。
    """
    rows = parse(source)
    wire_ids = {r["id"] for r, v in rows if r["type"] == "WIRE"}
    named = {v["parentId"]: v for r, v in rows if r["type"] == "ATTR" and v["key"] == "NET"}
    segments, pins, warnings = [], [], []
    for r, v in rows:
        if r["type"] == "LINE" and v.get("lineGroup") in wire_ids:
            a, b = (v["startX"], -v["startY"]), (v["endX"], -v["endY"])
            net = named.get(v["lineGroup"], {}).get("value")
            segments.append((net, (a, b)))
            if a == b or (a[0] != b[0] and a[1] != b[1]):
                warnings.append(["线段为空或不正交", a, b, net])
        elif v.get("lineGroup") in wire_ids and r["type"] != "LINE":
            raise ValueError(f"未知电气线型 {r['type']}")
    components = json.loads(geometry.read_text(encoding="utf-8-sig"), object_pairs_hook=unique_keys)
    _verify_page_components(rows, components, ids_by_ref)
    for comp in components:
        if comp.get("ComponentType") == "sheet":
            continue
        ref = comp.get("Designator")
        if comp.get("ComponentType") != "part" or ref not in pins_by_ref:
            raise ValueError(f"未知器件或未支持的电气图元 {ref!r}，须由对应编辑器适配器复核")
        for pin in comp["pins"]:
            n = str(pin["PinNumber"])
            net = pins_by_ref[ref][n]["net"]
            point = (pin["X"], pin["Y"])
            pins.append(((ref, n), net, point, bool(pin["NoConnected"])))
    parent = list(range(len(segments)))
    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i
    for i, (_, seg) in enumerate(segments):
        for j, (_, second) in enumerate(segments[:i]):
            touching = any(on(x, second) for x in seg) or any(on(x, seg) for x in second)
            if touching:
                parent[find(i)] = find(j)
    roots = defaultdict(set)
    labels = defaultdict(set)
    for i, (net, _) in enumerate(segments):
        root = find(i)
        if net:
            labels[root].add(net)
    for key, net, p, nc in pins:
        hits = {find(i) for i, (_, seg) in enumerate(segments) if pin_on_segment(p, seg)}
        if not hits and net:
            warnings.append(["引脚没有导线", key, p, net])
        if (nc and net) or (hits and not net):
            warnings.append(["NC 状态与网表不一致", key, p, net, nc])
        for root in hits:
            roots[root].add(key)
            if net:
                labels[root].add(net)
    for root in {find(i) for i in range(len(segments))}:
        if len(labels[root]) != 1 or not roots[root]:
            warnings.append(["连续线树短接异网、未定网名或无实体引脚", sorted(labels[root]), sorted(roots[root])])
    direct = set().union(*(ks for ks in roots.values() if len({k[0] for k in ks}) > 1))
    nc_pins = sorted([list(k) for k, net, p, nc in pins if nc])
    return {"page": source.stem, "electrical_pins": sum(bool(n) for _, n, _, _ in pins),
            "pins_direct_to_other_component": len(direct), "named_visible": sum(
                bool(v.get("valueVisible")) for v in named.values()), "segments":len(segments),
            "wire_groups":len(wire_ids), "nc_marks":nc_pins, "issues":warnings,
            "endpoints":[k for k, _, _, _ in pins]}


def audit_snapshot(directory):
    """从原生网表、图页及编辑器实测锚点审查物理连线。返回逐页明细和合计。

    引脚须完整且唯一，网表中空网名与显式 NC 记号分别处理。漏页、重复引脚、
    输入无效或未知图元会抛出异常。布局可读性仍需另外逐页查看。
    """
    board = json.loads((directory / "netlist.enet").read_text(encoding="utf-8-sig"), object_pairs_hook=unique_keys)
    by_ref = {p["props"]["Designator"]: p["pinInfoMap"] for p in board["components"].values()}
    if not by_ref or len(by_ref) != len(board["components"]):
        raise ValueError("原生网表为空或含有重复位号")
    sheets = sorted(directory.glob("*.esch"))
    if not sheets:
        raise ValueError("未找到任何原生图页")
    if {p.name for p in directory.glob("*-geometry.json")} != {p.stem+"-geometry.json" for p in sheets}:
        raise ValueError("原生图页与实测几何文件未逐页一一对应")
    ids_by_ref = {}
    for uid, comp in board["components"].items():
        ref = comp["props"]["Designator"]
        if not uid or comp["props"].get("Unique ID", uid) != uid or not by_ref[ref]:
            raise ValueError(f"网表器件身份或引脚表无效：{ref!r} / {uid!r}")
        ids_by_ref[ref] = uid
    pages = [page_report(p, p.with_name(p.stem+"-geometry.json"), by_ref, ids_by_ref=ids_by_ref) for p in sheets]
    expected = {(ref, n) for ref, mapping in by_ref.items() for n in mapping}
    counted = Counter(key for page in pages for key in page.pop("endpoints"))
    if set(counted) != expected or any(n != 1 for n in counted.values()):
        raise ValueError("各图页实测的引脚总表与完整原生网表不一致")
    totals = {k: sum(p[k] for p in pages) for k in ["electrical_pins", "pins_direct_to_other_component", "named_visible", "segments", "wire_groups"]}
    totals["nc_count"] = sum(len(p["nc_marks"]) for p in pages)
    totals["issues"] = sum(len(p["issues"]) for p in pages)
    return {"totals": totals, "pages": pages}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("report", type=Path)
    parser.add_argument("--baseline", type=Path, help="一并核对重排前后的 NC 对象集合")
    args = parser.parse_args()
    try:
        directories = [args.directory] + ([args.baseline] if args.baseline else [])
        for directory in directories:
            inputs = [directory / "netlist.enet", *directory.glob("*.esch"), *directory.glob("*-geometry.json")]
            if any(args.report.resolve() == p.resolve() or (args.report.exists() and p.exists()
                   and args.report.samefile(p)) for p in inputs):
                raise ValueError("报告不能覆盖原始输入")
        result = audit_snapshot(args.directory)
        passed = result["totals"]["issues"] == 0
        if args.baseline:
            prior = audit_snapshot(args.baseline)
            def marks(report):
                return {tuple(pin) for p in report["pages"] for pin in p["nc_marks"]}
            before, after = marks(prior), marks(result)
            result["nc_removed"], result["nc_added"] = sorted(before-after), sorted(after-before)
            passed = passed and before == after
        result["passed"] = passed
        args.report.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(result["totals"])
        return 0 if passed else 1
    except (OSError, ValueError, KeyError, TypeError) as exc:
        parser.exit(2, f"无法核对原生线段与引脚：{exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
