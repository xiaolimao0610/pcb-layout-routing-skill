"""只供续算的完整路由检查点；不授予写回权限或整板验收。"""
from copy import deepcopy
import math
import re

from pcb_pipeline_common import PipelineError, check_board, fingerprint


_KEYS = {'schema_version', 'kind', 'source', 'baseline_fingerprint', 'options_fingerprint',
         'candidate_fingerprint', 'board', 'completed_nets', 'pending_nets', 'deferred_nets',
         'usage', 'pass_index', 'full_board_accepted', 'writeback_allowed', 'escape_completed', 'failure'}
_USAGE = {'elapsed_seconds', 'net_elapsed_seconds', 'expansions', 'repair_attempts'}


def _source(board):
    """提取完整来源身份；无真实格式的来源抛PipelineError，不臆造UUID。"""
    metadata = board.get('metadata', {})
    source = metadata.get('source', {})
    if (not isinstance(source, dict) or not isinstance(source.get('document_uuid'), str)
            or not source['document_uuid'] or not isinstance(source.get('fingerprint'), str)
            or re.fullmatch(r'sha256:[0-9a-f]{64}', source['fingerprint']) is None):
        raise PipelineError('路由检查点需要完整UUID与sha256来源指纹')
    usage = source.get('usage', metadata.get('board_usage', 'physical'))
    if usage not in ('physical', 'test_only'):
        raise PipelineError('路由检查点来源用途非法')
    if metadata.get('board_usage') == 'test_only':
        usage = 'test_only'
    return dict(document_uuid=source['document_uuid'], fingerprint=source['fingerprint'], usage=usage)


def options_fingerprint(options):
    """输入已规范化的路由选项，返回配置摘要；内部运行缓存不参与配置身份。"""
    return fingerprint({k:v for k,v in options.items() if not k.startswith('_')})


def check_increment(original, candidate):
    """验证完整不可变量和原铜前缀；成功返回None，修改原板/旧铜则抛异常。

    只允许追加走线、全栈孔和已有入口生成的铜区，不允许改规则、器件、
    网表、来源、禁区、旧孔或任何旧铜。此处不代替完整几何/连通复核。
    """
    if not isinstance(candidate, dict) or set(candidate) != set(original):
        raise PipelineError('检查点完整板字段与基线不符')
    for key in original:
        if key == 'metadata':
            # 两个引出阶段仅追加候选报告；来源、约束、验收等元数据仍逐字段一致。
            reports = {'fanout_candidate', 'escape_candidate'}
            a, b = original[key], candidate[key]
            if (not isinstance(b, dict) or {k:v for k,v in a.items() if k not in reports}
                    != {k:v for k,v in b.items() if k not in reports}
                    or any(not isinstance(b[k], dict) for k in reports if k in b)):
                raise PipelineError('检查点修改了原板元数据不可变量')
            continue
        if key != 'routes' and candidate[key] != original[key]:
            raise PipelineError(f'检查点修改了原板不可变量：{key}')
    old, new = original['routes'], candidate['routes']
    if not isinstance(new, dict) or set(new) - set(old) - {'traces', 'vias', 'copper_pours'}:
        raise PipelineError('检查点添加了不允许的铜类型或路由字段')
    for key in set(old) | set(new):
        a, b = old.get(key, []), new.get(key, [])
        if key in ('traces', 'vias', 'copper_pours'):
            if not isinstance(b, list) or b[:len(a)] != a:
                raise PipelineError(f'检查点改动或删除原铜前缀：{key}')
        elif a != b:
            raise PipelineError(f'检查点修改旧孔、禁区或其他固定路由：{key}')


def _nets(values, declared, label):
    if (not isinstance(values, list) or any(not isinstance(x, str) for x in values)
            or len(values) != len(set(values)) or not set(values) <= declared):
        raise PipelineError(f'检查点{label}必须为完整声明网的唯一子集')
    return set(values)


def _usage(value, nets):
    if not isinstance(value, dict) or set(value) != _USAGE:
        raise PipelineError('检查点预算字段不完整或含未知字段')
    def real(number):
        return type(number) in (int,float) and math.isfinite(number) and number >= 0
    if not real(value['elapsed_seconds']):
        raise PipelineError('检查点累计耗时非法')
    times = value['net_elapsed_seconds']
    if not isinstance(times, dict) or not set(times) <= nets or not all(real(x) for x in times.values()):
        raise PipelineError('检查点逐网累计耗时非法')
    if sum(times.values()) > value['elapsed_seconds'] + 1e-6:
        raise PipelineError('检查点逐网累计耗时超过总耗时')
    for key in ('expansions','repair_attempts'):
        if type(value[key]) is not int or value[key] < 0:
            raise PipelineError(f'检查点累计计数非法：{key}')


def make_checkpoint(original, candidate, options, completed_nets, deferred_nets, budget, stats, pass_index,
                    *, failure=None):
    """输入同源原板、候选、选项及累计预算，返回完整内部检查点。

    每次保存重新检查硬几何及已完成网；不修改原输入、不执行文件或EDA写入。
    非法来源、不可变量变化、虚报连通抛PipelineError。调用方将返回工件放在
    独立计算缓存，不能当作成功board/writeback_plan；保存耗时计入总时限。
    """
    from pcb_routing import check_routed_nets, _groups
    declared = set(original['nets'])
    completed = _nets(list(completed_nets), declared, '已完成网络')
    deferred = _nets(list(deferred_nets), declared, '暂缓网络')
    if completed & deferred:
        raise PipelineError('暂缓地网不能记入已完成信号')
    check_increment(original, candidate)
    errors = check_routed_nets(candidate, sorted(completed))
    # 半途扇出可保留，但任何新增铜组都必须锚定真实端子。
    for net in declared:
        if any(not group['terminals'] for group in _groups(candidate, net)):
            errors.append(f'FLOATING_COPPER: {net} 检查点存在无端子铜岛')
    if errors:
        raise PipelineError('检查点未通过独立检查：'+'; '.join(errors[:8]))
    budget.record(stats)
    usage = dict(elapsed_seconds=stats['elapsed_seconds'],
                 net_elapsed_seconds=deepcopy(stats['net_elapsed_seconds']),
                 expansions=stats.get('expansions', 0),
                 repair_attempts=stats.get('local_repair', {}).get('attempts', 0))
    _usage(usage, declared)
    return dict(schema_version=1, kind='calculation_checkpoint', source=_source(original),
                baseline_fingerprint=fingerprint(original), options_fingerprint=options_fingerprint(options),
                candidate_fingerprint=fingerprint(candidate), board=deepcopy(candidate),
                completed_nets=sorted(completed), pending_nets=sorted(declared-completed-deferred),
                deferred_nets=sorted(deferred), usage=usage, pass_index=pass_index,
                full_board_accepted=False, writeback_allowed=False, escape_completed=True,
                failure=deepcopy(failure))


def restore_checkpoint(original, options, checkpoint, deferred_nets):
    """复核完整检查点，返回(候选副本,完成网,预算副本,原pass编号)。

    格式、来源、配置、原铜或连通任何不符抛PipelineError；不重置预算。
    上层须用状态索引的文件摘要及唯一任务键防止回放更旧工件；此函数不
    将自带摘要当作签名，也不连接现场证明来源新鲜。
    """
    from pcb_routing import check_routed_nets, _groups
    c = checkpoint
    if (not isinstance(c, dict) or set(c) != _KEYS or c['schema_version'] != 1
            or type(c['schema_version']) is not int or c['kind'] != 'calculation_checkpoint'
            or c['full_board_accepted'] is not False or c['writeback_allowed'] is not False
            or c['escape_completed'] is not True):
        raise PipelineError('不接受验收板或未知格式作为内部路由检查点')
    if (c['source'] != _source(original) or c['baseline_fingerprint'] != fingerprint(original)
            or c['options_fingerprint'] != options_fingerprint(options)):
        raise PipelineError('检查点来源、规则、姿态或路由配置已经变化')
    if fingerprint(c['board']) != c['candidate_fingerprint']:
        raise PipelineError('检查点候选文件摘要不符')
    check_increment(original, c['board'])
    declared = set(original['nets'])
    done = _nets(c['completed_nets'], declared, '已完成网络')
    pending = _nets(c['pending_nets'], declared, '未完成网络')
    deferred = _nets(c['deferred_nets'], declared, '暂缓网络')
    if (deferred != set(deferred_nets) or done & pending or done & deferred or pending & deferred
            or done | pending | deferred != declared):
        raise PipelineError('检查点隐藏了未完成网络或改变了地网范围')
    _usage(c['usage'], declared)
    # 失败保存下一次允许尝试的编号；max_passes+1 表示已耗尽，不能恢复后再送一次。
    if type(c['pass_index']) is not int or not 1 <= c['pass_index'] <= options['max_passes'] + 1:
        raise PipelineError('检查点路由轮数不合法或超过原预算')
    if c['failure'] is not None and not isinstance(c['failure'], dict):
        raise PipelineError('检查点失败上下文必须为对象或 None')
    errors = check_routed_nets(c['board'], sorted(done))
    for net in declared:
        if any(not group['terminals'] for group in _groups(c['board'], net)):
            errors.append(f'FLOATING_COPPER: {net} 检查点存在无端子铜岛')
    if errors:
        raise PipelineError('检查点连通/几何重新验收失败：'+'; '.join(errors[:8]))
    return deepcopy(c['board']), done, deepcopy(c['usage']), c['pass_index']
