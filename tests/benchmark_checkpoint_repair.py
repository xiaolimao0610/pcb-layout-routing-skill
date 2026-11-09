"""可重复的双网局部拆线模块比较；不能代表真实密板布通率或全局最优。"""
from copy import deepcopy
import argparse
import json
import math
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pcb_pipeline_common import check_board, fingerprint
from pcb_route_repair import repair_net
from pcb_routing import _Budget, _net_options, algorithm_options, check_routed_nets, route_board
from test_routing_checkpoint_repair import fixture, options


def run_benchmark():
    """返回同一候选/同一总时限下的两种模块结果；不合法输入或结果直接报错。

    旧分支保留前序普通网全部铜，仅补剩余网络；新分支显式调用一次本轮铜
    拆线模块。正常入口只有布线失败才自动触发新模块，此比较没有伪造失败，
    不代表端到端自动触发概率，也不据此启用新默认。
    """
    original = fixture()
    current = deepcopy(original)
    for start, end in (((20,75),(20,55)), ((20,55),(100,55)), ((100,55),(100,25)),
                       ((100,25),(165,25)), ((165,25),(165,75)), ((165,75),(180,75))):
        current['routes']['traces'].append(dict(net='B', layer=1, sx=start[0], sy=start[1],
                                              ex=end[0], ey=end[1], width=2))
    initial = deepcopy(current)
    errors = check_routed_nets(current, ['B'])
    if errors:
        raise RuntimeError('合成待布输入不合法：'+'; '.join(errors))
    common = options(checkpoint=False, max_passes=1, max_seconds=30, max_net_seconds=20)
    started = time.perf_counter()
    legacy, legacy_stats = route_board(deepcopy(current), common)
    legacy_seconds = time.perf_counter() - started
    upgraded_options = algorithm_options(dict(common, local_repair=dict(enabled=True, max_attempts=1,
                                          max_ripup_nets=1, max_seconds=30, radius=30)))
    settings = {net:_net_options(original, net, upgraded_options) for net in original['nets']}
    upgraded_stats = dict(expansions=0, connections=0, searches=0)
    started = time.perf_counter()
    upgraded = repair_net(original, deepcopy(current), 'A', {'B'}, upgraded_options, settings,
                          upgraded_stats, _Budget(upgraded_options))
    upgraded_seconds = time.perf_counter() - started
    if upgraded is None or current != initial:
        raise RuntimeError('局部模块未找到候选或修改了基线')

    def metrics(board, seconds):
        errors = check_board(board)
        if errors or board['nets'] != original['nets'] or board['components'] != original['components']:
            raise RuntimeError('基准结果未通过原检查器/完整网表/姿态复核：'+str(errors))
        traces = board['routes']['traces']
        return dict(connected_nets=len(board['nets']), declared_nets=len(original['nets']),
                    checker_errors=errors, traces=len(traces), vias=len(board['routes']['vias']),
                    total_length_mil=round(sum(math.hypot(t['ex']-t['sx'], t['ey']-t['sy']) for t in traces), 3),
                    wall_seconds=round(seconds, 6))
    baseline, upgraded_result = metrics(legacy, legacy_seconds), metrics(upgraded, upgraded_seconds)
    return dict(schema_version=1, fixture='synthetic_two_net_detour',
                scope='同一待布候选的局部模块比较；非实板、非全流程布通率结论，不用于切换默认',
                original_fingerprint=fingerprint(original), candidate_fingerprint=fingerprint(current),
                same_input_verified=True, original_unchanged=current == initial,
                common_budget=dict(max_seconds=30, max_net_seconds=20, max_passes=1, max_expansions=50000),
                legacy_finish_without_ripup=baseline, generated_only_local_ripup=upgraded_result,
                ripup_attempts=upgraded_stats['local_repair']['attempts'],
                length_reduction_percent=round(100*(baseline['total_length_mil']-upgraded_result['total_length_mil'])
                                              / baseline['total_length_mil'], 3),
                default_changed=False, original_copper_deletion_allowed=False)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, help='可选独立 JSON 证据输出路径')
    args = parser.parse_args()
    report = run_benchmark()
    text = json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False)+'\n'
    if args.output:
        args.output.write_text(text, encoding='utf-8')
    print(text, end='')
