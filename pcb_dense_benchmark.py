"""隔离比较旧版与实验版的离线布线；不连接EDA、不自动切换默认值。"""
from __future__ import annotations

import argparse
from copy import deepcopy
import hashlib
import json
import math
import os
from pathlib import Path
import statistics
import subprocess
import sys
import time


def _worker(package, payload):
    """输入工具目录和单次任务，返回离线证据；搜索失败记录原因，不填造部分布通率。"""
    sys.path.insert(0, str(Path(package).resolve()))
    from pcb_constraints import compile_constraints
    from pcb_pipeline_common import check_board
    from pcb_routing import route_board, check_routed_nets, deferred_ground_nets
    started = time.monotonic()
    result = {"passed": False, "full_board_accepted": False, "native_drc": "not_run",
              "completed_nets": None, "trace_length_mil": None, "via_count": None,
              "peak_process_memory_bytes": None}
    try:
        board, options, _ = compile_constraints(payload["board"], {"routing": payload["options"]}, payload.get("constraints"))
        routed, stats = route_board(board, options["routing"])
        deferred = deferred_ground_nets(board, options["routing"])
        target = [net for net in board["nets"] if net not in deferred]
        errors = check_routed_nets(routed, target) if deferred else check_board(routed)
        result.update(passed=not errors, errors=errors, stats=stats,
                      acceptance_scope="offline_signals" if deferred else "offline_board",
                      deferred_ground_nets=deferred, target_nets=len(target),
                      completed_nets=sum(not check_routed_nets(routed, [net]) for net in target),
                      trace_length_mil=sum(math.hypot(t["ex"]-t["sx"], t["ey"]-t["sy"])
                                           for t in routed["routes"].get("traces", [])),
                      via_count=sum(len(routed["routes"].get(kind, [])) for kind in ("vias", "shield_vias")))
    except Exception as exc:
        # 失败板没有返回候选，不能把未知的部分完成数写成0或声称整板通过。
        result.update(error=str(exc), error_type=type(exc).__name__, diagnostics=getattr(exc, "diagnostics", {}))
    result["elapsed_seconds"] = time.monotonic() - started
    return result


def run_suite(suite_path, baseline_dir, candidate_dir, repeats=1):
    """读取显式用例，独立子进程运行两个版本并返回对比报告。

    入参为JSON用例文件、两版工具目录和重复次数；返回可序列化报告。
    用例升级部分仅允许算法开关，禁止改变线宽、间距、孔径或搜索总预算。
    非法配置抛ValueError；单次超时保留失败证据，不据此断言无解。
    """
    suite_path = Path(suite_path).resolve()
    suite = json.loads(suite_path.read_text(encoding="utf-8-sig"))
    if not isinstance(suite, dict) or set(suite) != {"cases"} or not isinstance(suite["cases"], list) or not suite["cases"]:
        raise ValueError("基准文件须恰含非空cases列表")
    if type(repeats) is not int or not 1 <= repeats <= 10:
        raise ValueError("repeats须为1至10的整数")
    variants = {"legacy": Path(baseline_dir).resolve(), "upgraded": Path(candidate_dir).resolve()}
    for directory in variants.values():
        if not (directory / "pcb_routing.py").is_file():
            raise ValueError("基准工具目录缺少pcb_routing.py")
    report = {"schema_version": 1, "scope": "offline_only", "default_switch": False,
              "native_drc": "not_run", "repeats": repeats, "cases": [],
              "packages": {key: {"path": str(path), "sha256": {
                  f.name: hashlib.sha256(f.read_bytes()).hexdigest() for f in sorted(path.glob("*.py"))}}
                           for key, path in variants.items()}}
    seen = set()
    for case in suite["cases"]:
        if (not isinstance(case, dict) or set(case) - {"name", "board", "options", "upgrade", "constraints"}
                or not {"name", "board", "options", "upgrade"} <= set(case)):
            raise ValueError("基准用例字段非法或缺失")
        if not isinstance(case["name"], str) or not case["name"] or case["name"] in seen:
            raise ValueError("基准名称须唯一且非空")
        seen.add(case["name"])
        if (not isinstance(case["options"], dict) or not isinstance(case["upgrade"], dict)
                or set(case["upgrade"]) - {"geometry_search", "fanout", "shove", "diff_layer_change", "method", "occupancy_grid",
                                          "negotiated_grid_cache", "negotiated_bend_penalty"}):
            raise ValueError("upgrade仅接受新算法开关，不得改变工艺或总预算")
        path = (suite_path.parent / case["board"]).resolve()
        board = json.loads(path.read_text(encoding="utf-8-sig"))
        options = deepcopy(case["options"])
        timeout = options.get("max_seconds", 120)
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= 3600:
            raise ValueError("基准总预算须在(0,3600]秒内")
        item = {"name": case["name"], "board": str(path), "board_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "base_options": options, "upgrade": deepcopy(case["upgrade"]), "runs": {key: [] for key in variants}}
        for repeat in range(repeats):
            # 交替运行顺序，减小缓存与热状态的系统性偏差；算法输入保持相同。
            order = list(variants) if repeat % 2 == 0 else list(reversed(variants))
            for name in order:
                payload = {"board": board, "options": dict(options, **(case["upgrade"] if name == "upgraded" else {})),
                           "constraints": case.get("constraints")}
                try:
                    process = subprocess.run([sys.executable, "-B", "-X", "utf8", str(Path(__file__).resolve()),
                                              "--worker", str(variants[name])],
                                             input=json.dumps(payload, ensure_ascii=False), text=True, encoding="utf-8",
                                             stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                             timeout=timeout+30, creationflags=0x08000000 if os.name == "nt" else 0)
                    if process.returncode:
                        result = {"passed": False, "error": process.stderr[-4000:], "completed_nets": None}
                    else:
                        result = json.loads(process.stdout)
                except subprocess.TimeoutExpired:
                    result = {"passed": False, "error": "进程预算耗尽，未找到完成证据，未证明无解", "completed_nets": None}
                item["runs"][name].append(result)
        item["summary"] = {name: {"passed_runs": sum(row["passed"] for row in rows),
                                "median_elapsed_seconds": statistics.median([row["elapsed_seconds"] for row in rows if "elapsed_seconds" in row])
                                if any("elapsed_seconds" in row for row in rows) else None}
                           for name, rows in item["runs"].items()}
        report["cases"].append(item)
    report["summary"] = {name: {"passed_runs": sum(case["summary"][name]["passed_runs"] for case in report["cases"]),
                               "total_runs": len(report["cases"])*repeats} for name in variants}
    return report


def main(argv=None):
    """命令行入口；工作子进程仅输出JSON，父进程只写显式新报告文件。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker")
    parser.add_argument("--suite")
    parser.add_argument("--baseline-dir")
    parser.add_argument("--candidate-dir", default=str(Path(__file__).resolve().parent))
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--output")
    args = parser.parse_args(argv)
    if args.worker:
        print(json.dumps(_worker(args.worker, json.load(sys.stdin)), ensure_ascii=False))
        return 0
    if not args.suite or not args.baseline_dir or not args.output:
        parser.error("须指定--suite、--baseline-dir和--output")
    output = Path(args.output).resolve()
    if output.exists():
        parser.error("基准输出必须是新文件，不覆盖已有输入或证据")
    result = run_suite(args.suite, args.baseline_dir, args.candidate_dir, args.repeats)
    with output.open("x", encoding="utf-8") as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2)
    print(json.dumps(result["summary"], ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
