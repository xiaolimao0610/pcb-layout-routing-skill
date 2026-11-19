"""比对嘉立创原生 JSON 网表，核对器件身份、属性及逐网络的物理引脚集合。"""

import argparse
import hashlib
import json
import sys
from collections import defaultdict
from pathlib import Path


def unique_keys(pairs):
    """从 JSON 键值对构建字典，重复键会覆盖端子证据，因此抛出 ValueError。"""
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"出现重复 JSON 键 {key!r}")
        result[key] = value
    return result


def _index(data):
    """索引一份网表。返回位号属性、逐脚记录、网络端子及未接脚，结构不完整时抛出 ValueError。"""
    components = data.get("components") if isinstance(data, dict) else None
    if not isinstance(components, dict) or not components:
        raise ValueError("原生网表必须包含非空 components 字典")
    identities, pins, networks, unconnected = {}, {}, defaultdict(list), []
    for key, item in components.items():
        if not isinstance(item, dict):
            raise ValueError(f"{key} 的器件记录无效")
        props, pin_map = item.get("props"), item.get("pinInfoMap")
        if not isinstance(props, dict) or not isinstance(pin_map, dict) or not pin_map:
            raise ValueError(f"{key} 缺失属性或引脚字典")
        ref, uid = props.get("Designator"), props.get("Unique ID")
        if not isinstance(ref, str) or not ref or ref in identities:
            raise ValueError(f"位号为空或重复 {ref!r}")
        if not isinstance(uid, str) or not uid or uid != key:
            raise ValueError(f"{ref} 的原生器件键与 Unique ID 不一致或为空")
        identities[ref] = {k: v for k, v in item.items() if k != "pinInfoMap"}
        for number, pin in pin_map.items():
            if (not isinstance(pin, dict) or str(pin.get("number", "")) != number
                    or not number or "net" not in pin or not isinstance(pin["net"], str)):
                raise ValueError(f"{ref}.{number} 缺少明确的网络或引脚号不一致")
            endpoint = (ref, number)
            pins[endpoint] = pin
            if pin["net"]:
                networks[pin["net"]].append(endpoint)
            else:
                unconnected.append(endpoint)
    return identities, pins, {n: sorted(e) for n, e in networks.items()}, sorted(unconnected)


def compare(before, after):
    """入参为重排前后两份已解析原生网表，返回含 passed 与全部差异的字典。格式无效时抛出 ValueError。

    空网名表示原生网表记录的未连接引脚，显式 NC 图元需要另外从原理图读回。
    """
    left = _index(before)
    right = _index(after)
    errors = []
    scopes = ("component", "pin", "net")
    for scope, first, second in zip(scopes, left, right):
        for key in sorted(set(first) | set(second)):
            if first.get(key) != second.get(key):
                errors.append({"scope": scope, "key": key, "before": first.get(key),
                               "after": second.get(key)})
    for key in sorted((before.keys() | after.keys()) - {"components"}):
        if key not in before or key not in after or before[key] != after[key]:
            errors.append({"scope": "document", "key": key, "before": before.get(key),
                           "after": after.get(key)})
    def counts(index):
        return dict(zip(("components", "pins", "nets", "unconnected_pins"), map(len, index)))
    return {"passed": not errors, "before": counts(left), "after": counts(right),
            "difference_count": len(errors), "differences": errors}


def main(argv=None):
    """有报告路径时输出摘要，无路径或指定完整输出时返回完整 JSON；相同、差异、异常分别退出 0、1、2。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("before", type=Path)
    parser.add_argument("after", type=Path)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--full-output", action="store_true", help="在终端也输出完整逐引脚比对报告")
    args = parser.parse_args(argv)
    try:
        files = (args.before, args.after)
        if args.report and any(args.report.resolve() == p.resolve() or
                               (args.report.exists() and args.report.samefile(p)) for p in files):
            raise ValueError("报告路径与网表输入重合")
        raw = [p.read_bytes() for p in files]
        parsed = [json.loads(b.decode("utf-8-sig"), object_pairs_hook=unique_keys) for b in raw]
        report = compare(*parsed)
        report["input_sha256"] = [hashlib.sha256(b).hexdigest() for b in raw]
        output = json.dumps(report, ensure_ascii=False, indent=2)
        if args.report:
            args.report.write_text(output + "\n", encoding="utf-8")
            if not args.full_output:
                summary = {k: report[k] for k in ("passed", "before", "after", "difference_count")}
                summary["report"] = str(args.report.resolve())
                if report["differences"]:
                    # 优先给出实际有差异的网络，再按引脚、器件、文档顺序给出定位键。
                    from itertools import islice
                    ordered = (row for scope in ("net", "pin", "component", "document")
                               for row in report["differences"] if row["scope"] == scope)
                    sample = []
                    for row in islice(ordered, 5):
                        key = row["key"]
                        fields = list(key) if isinstance(key, tuple) else [key]
                        clipped = [field if len(field) <= 128 else field[:127] + "…" for field in fields]
                        item = {"scope": row["scope"], "key": clipped if isinstance(key, tuple) else clipped[0]}
                        if fields != clipped:
                            item["key_truncated"] = True
                        sample.append(item)
                    summary["differences"] = sample
                    summary["additional_differences"] = report["difference_count"] - len(sample)
                output = json.dumps(summary, ensure_ascii=False, separators=(",", ":"))
        print(output)
        return 0 if report["passed"] else 1
    except (ValueError, OSError) as exc:
        print(f"无法完成网表核对：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
