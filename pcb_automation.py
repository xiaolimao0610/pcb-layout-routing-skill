"""有界局部布局反馈与流水线续算；仅产生候选，绝不派发原生写回。"""
from copy import deepcopy
import json
import math
import os
from pathlib import Path
import uuid

from shapely.geometry import box

from pcb_pipeline_common import (PipelineError, check_board, fingerprint, rules_for,
                                 strict_options)


def validate_options(value, options):
    """输入自动化配置及完整阶段选项，返回规范配置；未知字段/冲突抛异常。"""
    result = dict(enabled=False, max_seconds=600.0, max_layout_repairs=2,
                  repair_radius=200.0, max_moved_components=8, stagnation_rounds=2)
    result.update(strict_options(value, set(result), 'automation'))
    if type(result['enabled']) is not bool:
        raise PipelineError('automation.enabled 必须为布尔值')
    for key in ('max_seconds', 'repair_radius'):
        v = result[key]
        if type(v) not in (int, float) or not math.isfinite(v) or v <= 0:
            raise PipelineError(f'automation.{key} 必须为有限正数')
    for key, cap in (('max_layout_repairs', 19), ('max_moved_components', 1000), ('stagnation_rounds', 20)):
        v = result[key]
        if type(v) is not int or not 1 <= v <= cap:
            raise PipelineError(f'automation.{key} 必须为1..{cap}的整数')
    if result['enabled']:
        placement, routing = options.get('placement', {}), options.get('routing', {})
        if not placement.get('enabled', True) or placement.get('method', 'legacy') != 'analytic_sa':
            raise PipelineError('局部布局闭环要求显式启用 analytic_sa')
        if routing.get('checkpoint') is not True:
            raise PipelineError('局部布局闭环要求 routing.checkpoint=true')
        if routing.get('fanout', {}).get('enabled', False):
            raise PipelineError('局部布局闭环暂不支持BGA fanout重生成；可单独使用检查点续算')
    return result


def verify_calculation_board(original, options, candidate):
    """核对候选的全网表、原铜、移动授权和几何；返回None，失败抛异常。

    输入均为规范完整板及原始阶段配置。只允许原授权的器件姿态变化和新增
    铜，不能靠检查点变更来源、规则、封装、固定件或延期网。本函数允许网络
    未布通；它不授予成功结果、写回或整板验收。
    """
    from pcb_pipeline import build_writeback_plan
    from pcb_placement import _options, _copper_locked, _envelope
    if not isinstance(candidate, dict) or set(candidate) != set(original):
        raise PipelineError('续算候选的完整板字段不一致')
    expected = deepcopy(original)
    if (not isinstance(candidate.get('components'), list)
            or len(candidate['components']) != len(original['components'])):
        raise PipelineError('续算候选不能增删器件')
    by_id = {c['id']: c for c in candidate['components']}
    if set(by_id) != {c['id'] for c in original['components']}:
        raise PipelineError('续算候选器件身份不一致')
    rules = rules_for(original)
    placement = _options(options.get('placement', {}), original, rules)
    locked = _copper_locked(original, rules.contact_tolerance) if placement['enabled'] else set()
    tags = original.get('tags', {}).get('component_tags', {})
    for component in expected['components']:
        cid, new = component['id'], by_id[component['id']]
        changed = any(component[k] != new[k] for k in ('x', 'y', 'rotation', 'layer'))
        protected = (not placement['enabled'] or cid not in placement['movable_ids'] or cid in locked
                     or component.get('locked') or any(t.get('fixed') or 'fixed_pose' in t
                        for t in (component, tags.get(cid, {}))))
        if changed and protected:
            raise PipelineError('续算候选移动受保护器件：'+cid)
        if new['layer'] != component['layer']:
            raise PipelineError('续算候选不得翻面：'+cid)
        if new['rotation'] != component['rotation'] and not any(
                abs((new['rotation']-r+180) % 360-180) < 1e-8 for r in placement['rotations']):
            raise PipelineError('续算候选旋转未授权：'+cid)
        for key in ('x', 'y', 'rotation', 'layer'):
            component[key] = new[key]
        if cid in placement['regions'] and not placement['regions'][cid].covers(_envelope(component, rules.curve_tolerance)):
            raise PipelineError('续算候选超出原分区：'+cid)
    expected['routes'] = deepcopy(candidate['routes'])
    # 引出阶段报告不参与几何授权；仅允许已显式启用的两个报告键变化。
    for key, option in (('fanout_candidate', 'fanout'), ('escape_candidate', 'escape')):
        if options.get('routing', {}).get(option, {}).get('enabled', False):
            if key in candidate.get('metadata', {}):
                expected.setdefault('metadata', {})[key] = deepcopy(candidate['metadata'][key])
    if expected != candidate:
        raise PipelineError('续算候选改变了来源、规则、端子、元数据或其他不可变量')
    build_writeback_plan(original, candidate)  # 仅复核增量前缀，不执行计划。
    errors = check_board(candidate, ignore_connectivity=True)
    from pcb_functional_cells import check_relations
    errors += check_relations(candidate, placement.get('functional_cells', []))
    if errors:
        raise PipelineError('续算候选硬几何不合格：'+'; '.join(errors[:8]))


def local_layout_input(original, checkpoint_board, options, feedback, settings):
    """选择实际失败区内可动件，拆除其关联网络的本轮铜；返回板/布局配置/证据。

    不猜位号，不越过原分区，不移动固定/原铜连接件；簇作为整体扩展。输入
    检查点须先由路由恢复器验证。旧铜永久保留，无关网络的新增铜原样保留。
    没有合法局部动作时抛PipelineError，不退化为全板重新布局。
    """
    from pcb_placement import _options, _copper_locked, _envelope
    verify_calculation_board(original, options, checkpoint_board)
    cfg = deepcopy(options.get('placement', {}))
    parsed = _options(cfg, original, rules_for(original))
    fixed_copper = _copper_locked(original, rules_for(original).contact_tolerance)
    tags = original.get('tags', {}).get('component_tags', {})
    failed = set(feedback.get('failed_nets', []))
    hot = [box(*h['bounds']).buffer(settings['repair_radius']) for h in feedback.get('hotspots', [])]
    eligible, ranked = set(), []
    for c in checkpoint_board['components']:
        cid = c['id']
        if (cid not in parsed['movable_ids'] or cid in fixed_copper or c.get('locked')
                or any(t.get('fixed') or 'fixed_pose' in t for t in (c, tags.get(cid, {})))):
            continue
        eligible.add(cid)
        nets = {p.get('net') for p in c['pads']}
        envelope = _envelope(c, rules_for(original).curve_tolerance)
        in_hot = any(h.intersects(envelope) for h in hot)
        if failed & nets or in_hot:
            ranked.append((not bool(failed & nets), not in_hot, cid))
    selected = {v[2] for v in sorted(ranked)[:settings['max_moved_components']]}
    # 显式刚体簇不可因局部白名单而被拆开；超预算则拒绝本轮。
    changed = True
    while changed:
        previous = set(selected)
        for cluster in cfg.get('clusters', []):
            members = set(cluster.get('components', []))
            if members & selected:
                selected.update(members)
        for cell in cfg.get('functional_cells', []):
            members = {cell['anchor']['compId']} | {r['neighbor']['compId'] for r in cell['relations']}
            if members & selected:
                selected.update(members)
        changed = selected != previous
    if not selected or not selected <= eligible or len(selected) > settings['max_moved_components']:
        raise PipelineError('LOCAL_LAYOUT_NOT_FOUND：局部可动件为空、刚体包含固定件或超过数量预算')
    touched = {p.get('net') for c in checkpoint_board['components'] if c['id'] in selected
               for p in c['pads'] if p.get('net')}
    trial = deepcopy(checkpoint_board)
    removed, retained = {}, {}
    for kind in ('traces', 'vias', 'copper_pours'):
        baseline = original.get('routes', {}).get(kind, [])
        rows = checkpoint_board.get('routes', {}).get(kind, [])
        extra = rows[len(baseline):]
        kept = [r for r in extra if r.get('net') not in touched]
        trial['routes'][kind] = deepcopy(rows[:len(baseline)]+kept)
        removed[kind], retained[kind] = len(extra)-len(kept), len(kept)
    cfg.update(movable_ids=sorted(selected), initialization='preserve', feedback=deepcopy(feedback))
    # 不能用首轮绝对种子把修复器件再次拉回原始位置。
    cfg.pop('initial_poses', None)
    verify_calculation_board(original, options, trial)
    return trial, cfg, dict(moved_candidates=sorted(selected), affected_nets=sorted(touched),
                            removed_generated=removed, retained_generated=retained,
                            original_copper_preserved=True)


_KEYS = {'schema_version', 'kind', 'task_fingerprint', 'routing_input', 'routing_options', 'routing_checkpoint', 'completed_history',
         'attempt', 'usage', 'feedback', 'full_board_accepted', 'writeback_allowed'}


def make_checkpoint(task, routing_input, routing_options, routing_checkpoint, attempt, elapsed, feedback, history=()):
    """封装路由器已精检工件与原任务身份；返回诊断工件，不创建验收计划。"""
    return dict(schema_version=1, kind='pipeline_calculation_checkpoint',
                task_fingerprint=fingerprint(task), routing_input=deepcopy(routing_input),
                routing_options=deepcopy(routing_options),
                routing_checkpoint=deepcopy(routing_checkpoint), attempt=attempt,
                completed_history=list(history),
                usage={'elapsed_seconds': elapsed}, feedback=deepcopy(feedback),
                full_board_accepted=False, writeback_allowed=False)


def restore_checkpoint(task, original, options, checkpoint):
    """严格复核任务身份及布局输入，返回封装副本；路由内层由route_board再验。

    任何源、配置、姿态授权、状态或预算格式不符抛PipelineError。调用方仍须
    用文件摘要/任务键核查工件，不把同文件自带哈希视为数字签名或现场新鲜证明。
    """
    c = checkpoint
    if (not isinstance(c, dict) or set(c) != _KEYS or type(c['schema_version']) is not int
            or c['schema_version'] != 1 or c['kind'] != 'pipeline_calculation_checkpoint'
            or c['task_fingerprint'] != fingerprint(task) or c['full_board_accepted'] is not False
            or c['writeback_allowed'] is not False):
        raise PipelineError('流水线检查点格式、任务身份或验收标记非法')
    if type(c['attempt']) is not int or not 0 <= c['attempt'] < options.get('max_iterations', 3):
        raise PipelineError('流水线检查点轮数超出原预算')
    history = c['completed_history']
    if (not isinstance(history, list) or len(history) > c['attempt']
            or any(type(n) is not int or not 0 <= n <= len(original['nets']) for n in history)):
        raise PipelineError('流水线检查点停滞历史非法')
    usage = c['usage']
    if (not isinstance(usage, dict) or set(usage) != {'elapsed_seconds'}
            or type(usage['elapsed_seconds']) not in (int, float)
            or not math.isfinite(usage['elapsed_seconds']) or usage['elapsed_seconds'] < 0):
        raise PipelineError('流水线检查点累计耗时非法')
    inner_time = c.get('routing_checkpoint', {}).get('usage', {}).get('elapsed_seconds')
    if (type(inner_time) not in (int, float) or not math.isfinite(inner_time) or inner_time < 0
            or usage['elapsed_seconds']+1e-6 < inner_time):
        raise PipelineError('流水线累计耗时不得少于路由累计耗时')
    # 反馈只能引用原网/有效数值，复用严格布局契约。
    from pcb_placement import _options
    _options(dict(options.get('placement', {}), feedback=c['feedback']), original, rules_for(original))
    expected = deepcopy(options.get('routing', {}))
    actual = deepcopy(c['routing_options'])
    automation = validate_options(options.get('automation', {}), options)
    if automation['enabled']:
        limit = actual.pop('max_seconds', None)
        original_limit = expected.pop('max_seconds', 120.0)
        if (type(limit) not in (int, float) or not math.isfinite(limit) or limit <= 0
                or limit > min(original_limit, automation['max_seconds'])):
            raise PipelineError('续算路由时限不能超过原始阶段和总时限')
    if actual != expected:
        raise PipelineError('续算路由配置发生变化')
    verify_calculation_board(original, options, c['routing_input'])
    return deepcopy(c)


def write_checkpoint(path, checkpoint):
    """原子保存独立续算文件；临时文件同目录，IO错误传播，不写其他工件。"""
    path = Path(path)
    data = (json.dumps(checkpoint, ensure_ascii=False, allow_nan=False, indent=2)+'\n').encode('utf-8')
    if len(data) > 32*1024*1024:
        raise PipelineError('计算检查点超过32MiB读取上限，未保存；不能声称可续算')
    temporary = path.with_name(path.name+'.'+uuid.uuid4().hex+'.tmp')
    try:
        with temporary.open('xb') as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def read_checkpoint(path, expected_sha256):
    """按上次记录的字节摘要读有限JSON；缺摘要、变化或重复字段均拒绝。"""
    import hashlib
    from pcb_project_state import _bytes, _json_bytes
    data = _bytes(path)
    if not isinstance(expected_sha256, str) or hashlib.sha256(data).hexdigest() != expected_sha256:
        raise PipelineError('续算文件摘要不符或缺失；不能手改摘要替换原工件')
    return _json_bytes(data)
