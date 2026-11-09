"""同源 Gerber 热辐条端帽补证；仅调整已证明内界，绝不缩小间距外界。"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import math
from pathlib import Path
import re
import time
from types import MappingProxyType
import zipfile

from shapely.geometry import LineString

from pcb_geometry import GeometryError, _checked, _rounded
from pcb_native_poured_geometry import native_stroke_bounds, validate_native_pour_snapshot, _native_records


class EvidenceError(GeometryError):
    """来源、语法或资源预算不足；不能据此断言原板无解。"""


class Budget:
    """输入显式资源上限；计量操作无返回值，耗尽抛出 EvidenceError。"""
    DEFAULTS = dict(seconds=120, max_archive_bytes=50000000, max_member_bytes=20000000,
                    max_members=256, max_commands=500000, max_primitives=100000,
                    max_vertices=1000000, max_match_tests=10000000)

    def __init__(self, limits=None):
        self.limits = dict(self.DEFAULTS)
        if limits:
            if set(limits) - set(self.limits):
                raise EvidenceError('未知 Gerber 预算字段')
            self.limits.update(limits)
        if any(isinstance(v, bool) or not isinstance(v, (float, int)) or not math.isfinite(v) or v <= 0
               for v in self.limits.values()):
            raise EvidenceError('Gerber 预算必须是有限正数')
        self.started, self.counts = time.monotonic(), {}

    def use(self, key=None, count=1):
        if time.monotonic() - self.started > self.limits['seconds']:
            raise EvidenceError('budget_exhausted: Gerber 总时间预算耗尽，补证未完成')
        if key:
            self.counts[key] = self.counts.get(key, 0) + count
            if self.counts[key] > self.limits[key]:
                raise EvidenceError(f'budget_exhausted: {key} 预算耗尽，补证未完成')


def _digest(value):
    return 'sha256:' + hashlib.sha256(value).hexdigest()


def _json_digest(value):
    return _digest(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'),
                              allow_nan=False).encode('utf-8'))


def _without_volatile_members(text):
    # 保留所有非易变字段的原始字节表示，与 eda_adapter.mjs 的算法相同。
    start, end = text.index('{'), text.rindex('}')
    members, begin, depth, quoted, escaped = [], start + 1, 0, False, False
    for index in range(begin, end):
        ch = text[index]
        if quoted:
            if escaped:
                escaped = False
            elif ch == '\\':
                escaped = True
            elif ch == '"':
                quoted = False
        elif ch == '"':
            quoted = True
        elif ch in '{[':
            depth += 1
        elif ch in '}]':
            depth -= 1
        elif ch == ',' and depth == 0:
            members.append(text[begin:index]); begin = index + 1
    members.append(text[begin:end])
    kept = []
    for member in members:
        key = re.match(r'^\s*("(?:[^"\\]|\\.)*")\s*:', member)
        if not key or json.loads(key[1]) not in ('client', 'updateTime', 'version'):
            kept.append(member)
    return text[:start + 1] + ','.join(kept) + text[end:]


def source_fingerprint(source):
    """输入完整原生 UTF-8 文本，输出与适配器一致的指纹；非法记录抛 EvidenceError。"""
    if not isinstance(source, str):
        raise EvidenceError('需要完整原生来源文本')
    parts, headers = re.split(r'(\r?\n)', source), 0
    for index in range(0, len(parts), 2):
        line = parts[index]
        if not line.strip():
            continue
        if '||' not in line:
            raise EvidenceError('不支持的原生来源记录')
        split = line.index('||') + 2
        end = line.rfind('|') if line.strip().endswith('|') else len(line)
        try:
            header, payload = json.loads(line[:split - 2]), json.loads(line[split:end])
        except (ValueError, TypeError) as exc:
            raise EvidenceError('原生来源 JSON 无效') from exc
        if header.get('type') == 'DOCHEAD':
            headers += 1
            parts[index] = line[:split] + _without_volatile_members(line[split:end]) + line[end:]
    if headers != 1:
        raise EvidenceError('需要唯一 DOCHEAD 来源记录')
    return _digest(''.join(parts).encode('utf-8'))


def _tokens(text, budget):
    position = 0
    while position < len(text):
        if text[position].isspace():
            position += 1; continue
        extended = text[position] == '%'
        end = text.find('%' if extended else '*', position + 1)
        if end < 0:
            raise EvidenceError('Gerber 命令未完整终止')
        block = text[position + 1:end] if extended else text[position:end]
        if extended and (not block.endswith('*') or block.count('*') != 1):
            raise EvidenceError('unsupported: 宏或复合扩展命令尚未支持')
        budget.use('max_commands')
        yield extended, block[:-1] if extended else block
        position = end + 1


def parse_gerber(text, *, budget=None, profile='round_caps'):
    """输入完整 Gerber 文本、配置和预算，输出圆直线候选、量化步长及可选最终铜图。

    round_caps 只接受正片暗铜；final_image 额外处理有序暗/清、区域与光圈
    LM/LR/LS 变换。宏、重复、块光圈及未知命令抛 EvidenceError，完整扫描
    后才返回候选，不在找到匹配片段后提前停止。几何失败抛 GeometryError。
    """
    budget = budget or Budget()
    if profile not in ('round_caps', 'final_image'):
        raise EvidenceError('unsupported: 未实现的 Gerber 配置')
    renderer = None
    if profile == 'final_image':
        from pcb_gerber_image import ImageRenderer
        renderer = ImageRenderer(budget)
    fmt = unit = None
    apertures, selected, position, mode = {}, None, None, 1
    region, contour, ended, multiquadrant = False, False, False, False
    draws, file_function = [], None
    for ordinal, (extended, command) in enumerate(_tokens(text, budget)):
        if command.startswith('G04'):
            continue
        if ended:
            raise EvidenceError('unsupported: M02 后存在命令')
        if extended:
            if region and command in ('LPD', 'LPC'):
                raise EvidenceError('unsupported: 区域中途改变极性')
            match = re.fullmatch(r'FSLAX([1-6])([1-6])Y([1-6])([1-6])', command)
            if match:
                if fmt is not None or match.groups()[:2] != match.groups()[2:]:
                    raise EvidenceError('unsupported: 格式重复或 XY 精度不同')
                fmt = (int(match[1]), int(match[2])); continue
            if command in ('MOMM', 'MOIN'):
                if unit is not None:
                    raise EvidenceError('unsupported: 单位重复')
                unit = 1.0 if command == 'MOMM' else 25.4; continue
            match = re.fullmatch(r'ADD(\d+)([CRO]),([0-9.]+)(?:X([0-9.]+))?', command)
            if match:
                aperture, shape = int(match[1]), match[2]
                sizes = [float(match[3])] + ([] if match[4] is None else [float(match[4])])
                if (aperture < 10 or aperture in apertures or unit is None
                        or len(sizes) != (1 if shape == 'C' else 2)
                        or any(not math.isfinite(v) or v <= 0 for v in sizes)):
                    raise EvidenceError('unsupported: 重复、带孔或非法光圈')
                apertures[aperture] = (shape, [v * unit for v in sizes]); continue
            if command in ('LPD', 'LPC') and renderer is not None:
                renderer.polarity(command == 'LPD'); continue
            transform_match = re.fullmatch(r'(LM)(N|X|Y|XY)|(LR|LS)([+-]?[0-9]+(?:\.[0-9]+)?)', command)
            if transform_match and renderer is not None:
                if region:
                    raise EvidenceError('unsupported: 区域中途改变光圈变换')
                if transform_match[1] == 'LM':
                    renderer.mirror = transform_match[2]
                else:
                    value = float(transform_match[4])
                    if not math.isfinite(value) or (transform_match[3] == 'LS' and not 1e-6 <= value <= 1e6):
                        raise EvidenceError('unsupported: 光圈变换超出数值范围')
                    if transform_match[3] == 'LR': renderer.rotation = value % 360
                    else: renderer.scale = value
                continue
            if command in ('LPD', 'IPPOS'):
                continue
            if command.startswith(('TF.', 'TA.', 'TO.')) or command == 'TD' or command.startswith('TD.'):
                if command.startswith('TF.FilePolarity,') and command != 'TF.FilePolarity,Positive':
                    raise EvidenceError('unsupported: 负片文件极性')
                if command.startswith('TF.FileFunction,'):
                    if file_function is not None:
                        raise EvidenceError('文件层属性重复，不能确定层映射')
                    file_function = command.split(',')[1:]
                continue
            raise EvidenceError(f'unsupported: 扩展命令 {command[:60]}')
        if command == 'M02':
            if region:
                raise EvidenceError('未闭合 Gerber 区域')
            ended = True; continue
        if command == 'G75':
            multiquadrant = True; continue
        if command == 'G36':
            if region:
                raise EvidenceError('区域嵌套无效')
            region, contour = True, False
            if renderer is not None: renderer.begin_region()
            continue
        if command == 'G37':
            if not region or not contour:
                raise EvidenceError('空区域或区域终止无效')
            region, contour = False, False
            if renderer is not None: renderer.end_region()
            continue
        match = re.fullmatch(r'(?:G54)?D(\d+)', command)
        if match and int(match[1]) >= 10:
            if int(match[1]) not in apertures or region:
                raise EvidenceError('未定义光圈或区域内选择光圈')
            selected = apertures[int(match[1])]; continue
        if command in ('G01', 'G02', 'G03'):
            mode = int(command[1:]); continue
        match = re.fullmatch(r'(?:G0([123]))?(?:X([+-]?\d+))?(?:Y([+-]?\d+))?'
                             r'(?:I([+-]?\d+))?(?:J([+-]?\d+))?D0?([123])', command)
        if not match or fmt is None or unit is None:
            raise EvidenceError(f'unsupported: 绘制命令 {command[:60]}')
        if match[1]:
            mode = int(match[1])
        def coordinate(value, previous=None):
            if value is None:
                if previous is None:
                    raise EvidenceError('初始坐标缺失')
                return previous
            if len(value.lstrip('+-')) > sum(fmt):
                raise EvidenceError('坐标超出声明的格式')
            return int(value) * unit / 10 ** fmt[1]
        end = tuple(coordinate(match[i + 2], None if position is None else position[i]) for i in range(2))
        operation = int(match[6])
        if operation == 1:
            center = None
            if position is None or (not region and selected is None):
                raise EvidenceError('绘制起点或光圈缺失')
            if mode == 1 and (match[4] is not None or match[5] is not None):
                raise EvidenceError('直线不能带圆心偏移')
            if mode != 1:
                if not multiquadrant or (match[4] is None and match[5] is None):
                    raise EvidenceError('unsupported: 圆弧需要 G75 和显式圆心')
                center = (position[0] + coordinate(match[4], 0), position[1] + coordinate(match[5], 0))
                radii = math.dist(position, center), math.dist(end, center)
                if min(radii) <= 0 or abs(radii[0] - radii[1]) > 4 * unit / 10 ** fmt[1]:
                    raise EvidenceError('圆弧端点与圆心不一致')
            if region and not contour:
                raise EvidenceError('区域轮廓需要 D02 起点')
            if not region and mode == 1 and selected[0] == 'C' and position != end:
                budget.use('max_primitives')
                if renderer is None or renderer.dark:
                    draws.append(dict(start=position, end=end,
                                      width=selected[1][0] * (1 if renderer is None else renderer.scale), command=ordinal))
            if renderer is not None:
                renderer.draw(selected, position, end, mode, center, region)
        elif operation == 2:
            if match[4] is not None or match[5] is not None:
                raise EvidenceError('移动不能带圆心')
            contour = region
            if renderer is not None and region: renderer.move_region(end)
        elif region or selected is None:
            raise EvidenceError('区域内闪光或闪光缺少光圈')
        elif match[4] is not None or match[5] is not None:
            raise EvidenceError('闪光不能带圆心')
        elif renderer is not None:
            renderer.flash(selected, end)
        position = end
    if not ended or fmt is None or unit is None:
        raise EvidenceError('Gerber 缺少完整格式、单位或终止符')
    return dict(draws=draws, quantum_mm=unit / 10 ** fmt[1], commands=ordinal + 1,
                final_image=None if renderer is None else renderer.finish(), file_function=file_function)


def _board_signature(board):
    return _json_digest({key: board.get(key) for key in
                         ('units', 'components', 'routes', 'nets', 'tags', 'board_bbox', 'rules', 'metadata')})


def _rules_digest(rules):
    values = dict(vars(rules))
    values['clearance_matrix'] = sorted([list(pair) + [gap] for pair, gap in rules.clearance_matrix.items()])
    return _json_digest(values)


_CONTEXT_KEY = object()


class VerifiedGerberContext:
    """工厂创建的只读铺铜补证上下文；建立后不能替换输入几何。

    原生来源互证只覆盖完整 POURED 集合。完整输入指纹封存不能证明其他
    器件、普通走线或网表已经与原生来源互证；它们仍依赖原适配器和读回。
    """
    __slots__ = ('_signature', '_parts_signature', '_proofs', '_rules', '_key', '_sealed')

    def __setattr__(self, name, value):
        if getattr(self, '_sealed', False):
            raise AttributeError('已验证上下文不可修改；请从新鲜文件重新建立')
        object.__setattr__(self, name, value)

    def __init__(self, board, proofs, key=None):
        if key is not _CONTEXT_KEY:
            raise EvidenceError('必须从同源制造文件重新建立补证上下文')
        self._signature = _board_signature(board)
        self._parts_signature = _json_digest([board['components'], board.get('routes', {}), board['nets'],
                                             board.get('tags', {}), board['board_bbox']])
        self._proofs = MappingProxyType({key: MappingProxyType(dict(value)) for key, value in proofs.items()})
        from pcb_pipeline_common import rules_for
        self._rules = _rules_digest(rules_for(board))
        self._key = key
        self._sealed = True

    def validate_board(self, board):
        """输入完整板；不匹配时抛 EvidenceError，成功无返回值。"""
        if self._key is not _CONTEXT_KEY or _board_signature(board) != self._signature:
            raise EvidenceError('stale: 补证上下文与当前完整工程不一致')

    def validate_parts(self, components, routes, nets, tags, board_bbox, rules):
        """输入独立检查器的完整几何；内容改变时拒绝复用来源补证。"""
        if self._key is not _CONTEXT_KEY or _rules_digest(rules) != self._rules or _json_digest([list(components.values()), routes, nets, tags, board_bbox]) != self._parts_signature:
            raise EvidenceError('stale: 独立检查器几何与补证输入不一致')

    def region_bounds(self, pour, index, region, tolerance):
        """输入已绑定的对象与区域，返回补证内界和原外界；伪造对象抛 EvidenceError。"""
        from pcb_geometry import filled_region_bounds
        original = filled_region_bounds(region, tolerance)
        proof = self._proofs.get((pour.get('id'), index))
        if proof is None:
            return original
        if _json_digest(pour) != proof['pour_digest'] or _json_digest(region) != proof['region_digest']:
            raise EvidenceError('stale: 铜对象或线宽路径改变')
        stroke = region['native_stroke_path']
        radius = stroke['width'] / 2 - proof['margin_mil']
        if radius <= 0:
            return original
        certified = _rounded(LineString(stroke['points']), radius, tolerance / 2).inner
        # 不改变已存在的保守外界，新增内界必须仍位于该外界内。
        inner = original.inner.union(certified).intersection(original.outer)
        if proof.get('final_inner_mil') is not None:
            # 二期必须裁剪整个内界，不能让原平头内界重新填回被清铜删除的窄边缘。
            inner = inner.intersection(proof['final_inner_mil'])
        return _checked(inner, original.outer)


def require_context(context, board=None):
    """输入可选上下文与完整板，拒绝字典或任意鸭子类型伪造证明。"""
    if not isinstance(context, VerifiedGerberContext):
        raise EvidenceError('geometry_context 必须是本次从文件校验建立的上下文')
    if board is not None:
        context.validate_board(board)
    return context


def build_geometry_context(board, receipt, archive_path, layer_map, *, profile='round_caps', limits=None):
    """输入完整原生板、导出收据、ZIP 和显式层映射，输出上下文及逐对象报告。

    来源、散列、层或语法不符抛 EvidenceError；预算耗尽也拒绝部分补证。
    证据只调整同源热辐条内界，不代表其他输入对象已与原生来源互证，
    更不代表原生 DRC 或整板验收已经通过。建立后的任何输入改变均拒绝复用。
    """
    if not isinstance(board, dict) or board.get('units') != 'mil':
        raise EvidenceError('Gerber补证需要显式mil单位的完整适配板')
    budget = Budget(limits)
    if profile not in ('round_caps', 'final_image'):
        raise EvidenceError('unsupported: 未实现的 Gerber 补证配置')
    source = board.get('metadata', {}).get('source')
    native_profile = board.get('metadata', {}).get('native_pour_geometry', {})
    calibration = native_profile.get('calibration', {})
    native = calibration.get('source_native', calibration.get('source_before'))
    if (not isinstance(source, dict) or not source.get('document_uuid') or source_fingerprint(native) != source.get('fingerprint')):
        raise EvidenceError('来源 UUID/sourceFingerprint 与完整原生记录不一致')
    validate_native_pour_snapshot(board, {'coordinate_profile': native_profile})
    if (not isinstance(receipt, dict) or receipt.get('success') is not True or receipt.get('status') != 'exported'
            or receipt.get('package_kind') != 'gerber' or receipt.get('source') != source):
        raise EvidenceError('必须提供同源成功的原生 Gerber 导出收据')
    path = Path(archive_path)
    if path.stat().st_size > budget.limits['max_archive_bytes']:
        raise EvidenceError('budget_exhausted: ZIP 文件超出预算')
    archive = path.read_bytes()
    archive_hash = _digest(archive)
    matches = [f for f in receipt.get('files', []) if f.get('kind') == 'fabrication'
               and f.get('name') == path.name and f.get('sha256') == archive_hash and f.get('size') == len(archive)]
    if len(matches) != 1:
        raise EvidenceError('原生收据与 ZIP 文件名、大小或 SHA256 不一致')
    if (not isinstance(layer_map, dict) or not layer_map or any(isinstance(k, bool) or not isinstance(k, int) for k in layer_map)
            or len(set(layer_map.values())) != len(layer_map)
            or set(layer_map) != set(board.get('rules', {}).get('copper_layers', (1, 2)))):
        raise EvidenceError('必须明确映射完整铜层，层与成员不能重复')
    parent_records = {h.get('id'): p for h, p in _native_records(native, source['document_uuid']) if h.get('type') == 'POUR'}
    parsed, manifest = {}, []
    # 散列与解析使用同一份已读取字节，避免校验后文件被替换的时间窗口。
    with zipfile.ZipFile(io.BytesIO(archive)) as zipped:
        infos = zipped.infolist()
        if len(infos) > budget.limits['max_members'] or len({i.filename for i in infos}) != len(infos):
            raise EvidenceError('budget_exhausted: ZIP 成员重复或超过数量预算')
        if sum(i.file_size for i in infos) > budget.limits['max_archive_bytes']:
            raise EvidenceError('budget_exhausted: ZIP 解压总量超出预算')
        for info in infos:
            budget.use()
            if info.file_size > budget.limits['max_member_bytes'] or info.flag_bits & 1:
                raise EvidenceError('budget_exhausted: ZIP 成员过大或加密')
            data = zipped.read(info)
            manifest.append(dict(member=info.filename, size=len(data), sha256=_digest(data)))
        for layer, member in layer_map.items():
            if member not in {i.filename for i in infos}:
                raise EvidenceError('指定铜层成员不存在')
            try:
                text = zipped.read(member).decode('ascii')
            except UnicodeError as exc:
                raise EvidenceError('Gerber 铜层不是受支持的 ASCII 文本') from exc
            parsed[layer] = parse_gerber(text, budget=budget, profile=profile)
            function = parsed[layer]['file_function']
            if function is not None:
                order = board.get('rules', {}).get('copper_layers', [1, 2]).index(layer) + 1
                if len(function) < 2 or function[0] != 'Copper' or function[1] != f'L{order}':
                    raise EvidenceError('错层: 显式层映射与 Gerber FileFunction 属性矛盾')
    final_layers = {}
    if profile == 'final_image':
        from shapely import affinity
        for layer, data in parsed.items():
            budget.use()
            final_layers[layer] = tuple(affinity.scale(shape, xfact=1/.0254, yfact=1/.0254, origin=(0, 0))
                                       for shape in (data['final_image'].inner, data['final_image'].outer))
    proofs, reports = {}, []
    for pour in board.get('routes', {}).get('copper_pours', []):
        if pour.get('source_kind') != 'Poured':
            continue
        try:
            compound_id = json.loads(pour['id'])
        except (ValueError, TypeError) as exc:
            raise EvidenceError('铺铜 ID 缺少明确原生父 POUR 绑定') from exc
        if not isinstance(compound_id, list) or len(compound_id) != 2 or compound_id[0] != 'POURED':
            raise EvidenceError('铺铜 ID 缺少明确原生父 POUR 绑定')
        parent = parent_records.get(compound_id[1])
        if parent is None or parent.get('layerId') != pour.get('layer') or parent.get('netName') != pour.get('net'):
            raise EvidenceError('错层/错网: 铺铜对象与完整原生父 POUR 不一致')
        parsed_layer = parsed[pour['layer']]
        for index, region in enumerate(pour['filled_polygons']):
            if 'native_stroke_path' not in region:
                continue
            stroke = region['native_stroke_path']; native_stroke_bounds(region)
            points = [tuple(v * .0254 for v in point) for point in stroke['points']]
            width = stroke['width'] * .0254
            quantum = parsed_layer['quantum_mm']
            candidates = []
            for draw in parsed_layer['draws']:
                budget.use('max_match_tests')
                delta = min(max(math.dist(points[0], draw['start']), math.dist(points[1], draw['end'])),
                            max(math.dist(points[1], draw['start']), math.dist(points[0], draw['end'])))
                width_delta = abs(width - draw['width'])
                if delta <= math.sqrt(2) * quantum * 1.00001 and width_delta <= quantum * 1.00001:
                    candidates.append((delta + width_delta / 2, draw))
            raw = pour['metadata']['native_poured_regions'][index]
            record = dict(pour_id=pour['id'], region_id=raw['id'], layer=pour['layer'], net=pour['net'],
                          member=layer_map[pour['layer']], path_width_digest=_json_digest(region))
            if candidates:
                margin, draw = min(candidates, key=lambda c: c[0])
                margin_mil = margin / .0254 + 1e-9
                if margin_mil >= stroke['width'] / 2:
                    record.update(status='quantization_unknown', uncertainty_mil=margin_mil)
                    reports.append(record)
                    continue
                proofs[pour['id'], index] = dict(pour_digest=_json_digest(pour), region_digest=_json_digest(region), margin_mil=margin_mil)
                record.update(status='verified', command=draw['command'], uncertainty_mil=margin_mil)
                if profile == 'final_image':
                    old = native_stroke_bounds(region)
                    final_inner, final_outer = final_layers[pour['layer']]
                    # 若最终制造铜面已删除原生平头核心，则来源表示存在冲突，不能补证盖过它。
                    if not final_outer.covers(old.inner.buffer(-(margin_mil + .001))):
                        raise EvidenceError('conflict: 最终 Gerber 清铜删除了原生热辐条核心，补证拒绝')
                    proofs[pour['id'], index]['final_inner_mil'] = final_inner
                    record['final_copper_applied'] = True
            else:
                record.update(status='unmatched')
            reports.append(record)
    budget.use()
    report = dict(schema_version=1, profile=profile, source=source, archive_sha256=archive_hash,
                  source_scope='native_poured_records', non_pour_source_verified=False,
                  input_board_fingerprint=_board_signature(board),
                  geometry_scope='完整输入几何的离线检查；非铺铜对象仍需适配器与原生读回独立互证',
                  layer_map=layer_map, members=manifest, strokes=reports, total=len(reports),
                  verified=sum(r['status'] == 'verified' for r in reports),
                  complete=bool(reports) and all(r['status'] == 'verified' for r in reports),
                  elapsed_seconds=time.monotonic() - budget.started, budget=budget.limits,
                  full_board_accepted=False,
                  scope='仅同源 POURED/热辐条端帽补证；输入封存不证明非铺铜对象的原生来源，外界与原生验收要求不变')
    return VerifiedGerberContext(board, proofs, _CONTEXT_KEY), report


def main(argv=None):
    """CLI 输入文件路径，输出铺铜来源补证及完整输入的离线检查；未知/失败退出 2。

    此入口不采集或认证非铺铜对象的原生来源，不能产生整板原生验收结论。
    """
    parser = argparse.ArgumentParser(description=__doc__)
    for flag in ('board', 'receipt', 'archive', 'layer-map', 'out'):
        parser.add_argument('--' + flag, required=True)
    parser.add_argument('--profile', default='round_caps')
    parser.add_argument('--seconds', type=float, default=120)
    args = parser.parse_args(argv)
    def read(path):
        return json.loads(Path(path).read_text(encoding='utf-8-sig'))
    output = Path(args.out).resolve()
    for input_path in (args.board, args.receipt, args.archive, args.layer_map):
        target = Path(input_path).resolve()
        if output == target or (output.exists() and target.exists() and output.samefile(target)):
            # 不生成错误报告覆盖输入；直接告知调用者并拒绝执行。
            print('输出路径与输入文件冲突，未写入任何文件')
            return 2
    try:
        board = read(args.board)
        context, report = build_geometry_context(board, read(args.receipt), args.archive,
                                                {int(k): v for k, v in read(args.layer_map).items()},
                                                profile=args.profile, limits={'seconds': args.seconds})
        from pcb_pipeline_common import check_board
        errors = check_board(board, geometry_context=context)
        report.update(geometry_connectivity_passed=not errors, geometry_errors=errors)
        # 离线补证不产生 full_board_accepted；仍需要原生闭环和其他验收证据。
        code = 0 if report['complete'] and not errors else 2
    except (ValueError, KeyError, OSError, zipfile.BadZipFile) as exc:
        report, code = dict(complete=False, full_board_accepted=False, error=str(exc)), 2
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    return code


if __name__ == '__main__':
    # CLI 与独立检查器必须共享同一个模块身份，避免 __main__ 的重复类定义。
    from pcb_gerber_evidence import main as module_main
    raise SystemExit(module_main())
