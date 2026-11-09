"""有限邻域拆线重布：只移除本轮新增普通网铜，失败候选立即丢弃。"""
from copy import deepcopy
import math
import time

from shapely.geometry import box
from shapely.ops import unary_union

from pcb_geometry import trace_bounds, via_bounds
from pcb_pipeline_common import PipelineError, rules_for, terminal_items


def validate_options(value=None):
    """输入局部修复配置，返回严格补齐默认值的副本；类型/未知字段错误抛异常。"""
    defaults = dict(enabled=False, max_attempts=3, max_ripup_nets=3, max_seconds=15.0, radius=120.0)
    if value is None:
        value = {}
    if not isinstance(value, dict) or set(value) - set(defaults):
        raise PipelineError('routing.local_repair 含未知字段或不是对象')
    result = dict(defaults, **value)
    if type(result['enabled']) is not bool:
        raise PipelineError('local_repair.enabled 必须为布尔值')
    for key, limit in (('max_attempts', 100), ('max_ripup_nets', 32)):
        if type(result[key]) is not int or not 1 <= result[key] <= limit:
            raise PipelineError(f'local_repair.{key} 必须为 1..{limit} 的整数')
    for key in ('max_seconds', 'radius'):
        if type(result[key]) not in (int, float) or not math.isfinite(result[key]) or result[key] <= 0:
            raise PipelineError(f'local_repair.{key} 必须为有限正数')
    return result


class _RepairBudget:
    """共享原总/逐网预算并叠加本次邻域搜索时限，不重置任何累计账本。"""
    def __init__(self, parent, seconds):
        self.parent, self.deadline = parent, time.monotonic() + seconds

    @property
    def context(self):
        return self.parent.context

    @context.setter
    def context(self, value):
        self.parent.context = value

    def check(self):
        from pcb_routing import RoutingFailure
        self.parent.check()
        if time.monotonic() >= self.deadline:
            raise RoutingFailure('LOCAL_REPAIR_BUDGET', '局部修复时限耗尽，未找到通过检查的候选',
                                 **self.context, budget_scope='local_repair')


def repair_net(original, candidate, failed_net, completed, options, settings, stats, budget,
               *, excluded_nets=()):
    """输入完整基线、当前候选与失败网，返回修复候选或 None，不修改输入板。

    按失败网端子包围盒寻找本轮新增铜的邻近网络，最多拆 max_ripup_nets 个。
    原铜、地网和调用者保护的特种网络不参与拆线；失败保留原候选。每轮均
    使用原布线精检，再独立复核完整硬几何和全部已完成网。预算耗尽返回未
    找到；原整板/逐网 TIME_BUDGET 或硬几何错误继续抛出，禁止绕过。
    """
    from pcb_routing import RoutingFailure, _route_net, check_routed_nets, deferred_ground_nets
    from pcb_routing_checkpoint import check_increment
    from pcb_differential import declarations
    config = validate_options(options.get('local_repair'))
    if not config['enabled']:
        return None
    check_increment(original, candidate)
    errors = check_routed_nets(candidate, sorted(completed))
    if errors:
        raise RoutingFailure('GEOMETRY_HARD_FAILURE', '局部修复输入未通过完整硬几何/已完成网检查', errors=errors[:8])
    record = stats.setdefault('local_repair', dict(attempts=0, successes=0, events=[]))
    record.setdefault('successes', 0)
    record.setdefault('events', [])
    if record['attempts'] >= config['max_attempts']:
        record['stop_reason'] = '累计邻域尝试预算耗尽，未找到'
        return None
    shapes = [item['inner'] for item in terminal_items(candidate, failed_net)]
    if not shapes:
        raise PipelineError('局部修复失败网没有完整声明端子')
    bounds = unary_union(shapes).bounds
    region = box(*bounds).buffer(config['radius'], join_style=2)
    constraints = candidate.get('metadata', {}).get('pipeline_constraints', [])
    pairs, groups = declarations(constraints)
    protected = set(excluded_nets) | set(deferred_ground_nets(candidate, options)) | set(options.get('topology_orders', {}))
    protected.update(item['net'] for item in constraints if item.get('operator') == 'TOPOLOGY_ORDER')
    protected.update(net for pair in pairs for net in (pair['p'], pair['n']))
    protected.update(net for group in groups for net in group['nets'])
    if failed_net in protected:
        record['stop_reason'] = '失败网络属于地网/差分/等长/有序拓扑保护范围'
        return None
    for kind in ('traces', 'vias'):
        protected.update(item['net'] for item in candidate['routes'].get(kind, []) if item.get('locked'))
    eligible = set(completed) - protected - {failed_net}
    tolerance = rules_for(candidate).curve_tolerance
    nearby = {}
    blocking = []
    for kind in ('traces', 'vias'):
        prefix = len(original['routes'].get(kind, []))
        for index, item in enumerate(candidate['routes'].get(kind, [])):
            shape = (trace_bounds(item, tolerance).outer if kind == 'traces'
                     else via_bounds(item, tolerance)[0].outer)
            if not shape.intersects(region):
                continue
            identity = dict(kind=kind, index=index, net=item['net'], id=item.get('id'),
                            bounds=list(shape.bounds), original=index < prefix)
            if len(blocking) < 64:
                blocking.append(identity)
            if index >= prefix and item['net'] in eligible:
                nearby[item['net']] = min(nearby.get(item['net'], math.inf),
                                         shape.distance(box(*bounds)))
    neighbors = sorted(nearby, key=lambda net: (nearby[net], net))[:config['max_ripup_nets']]
    event = dict(failed_net=failed_net, bounds=list(region.bounds), blocking_objects=blocking,
                 neighbor_nets=neighbors, trials=[], outcome='未找到')
    record['events'].append(event)
    if not neighbors:
        event['reason'] = '邻域内没有允许拆除的本轮新增普通网铜'
        return None
    local_budget = _RepairBudget(budget, config['max_seconds'])
    for count in range(1, len(neighbors) + 1):
        if record['attempts'] >= config['max_attempts']:
            event['reason'] = '累计邻域尝试预算耗尽'
            break
        selected = set(neighbors[:count])
        trial = deepcopy(candidate)
        # 仅移除已完成邻网的追加铜；原铜前缀和未完成网扇出均保持不变。
        for kind in ('traces', 'vias'):
            prefix = len(original['routes'].get(kind, []))
            objects = trial['routes'].get(kind, [])
            trial['routes'][kind] = objects[:prefix] + [item for item in objects[prefix:]
                                                        if item['net'] not in selected]
        attempt = dict(ripped_nets=sorted(selected), outcome='未找到')
        record['attempts'] += 1
        event['trials'].append(attempt)
        try:
            local_budget.check()
            for net in [failed_net] + sorted(selected):
                budget.enter(net)
                try:
                    trial = _route_net(trial, net, settings[net], options, stats, local_budget)
                    local_budget.check()
                finally:
                    budget.leave()
            check_increment(original, trial)
            errors = check_routed_nets(trial, sorted(set(completed) | {failed_net}))
            local_budget.check()
            if errors:
                attempt['errors'] = errors[:8]
                continue
            attempt['outcome'] = '通过独立几何及全已完成网连通检查'
            event['outcome'] = '已找到通过检查的候选'
            record['successes'] += 1
            return trial
        except PipelineError as exc:
            attempt['error'] = str(exc)
            if isinstance(exc, RoutingFailure):
                code = exc.diagnostics['code']
                if code in ('TIME_BUDGET', 'GEOMETRY_HARD_FAILURE', 'OCCUPANCY_BUDGET'):
                    raise
                if code == 'LOCAL_REPAIR_BUDGET':
                    event['reason'] = '局部修复时限耗尽'
                    break
    return None
