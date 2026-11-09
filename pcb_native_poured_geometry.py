"""Conservative geometry for calibrated native L/ARC filled-copper rings.

Coordinates supplied here must already be in PCB mil with separate source-bound
evidence. The original paths are retained by the caller. Sampling is only an
intermediate construction: erosion/dilation bound the curved copper, including
holes; a chord polygon is never returned as exact conductive geometry.
"""
from functools import lru_cache
import json
import math

from shapely.geometry import GeometryCollection, Polygon, LineString
from shapely.validation import explain_validity

from pcb_geometry import Bounds, GeometryError, _checked


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise GeometryError("native_compound_path requires finite numbers")
    return float(value)


def _sample_ring(path, tolerance, remaining):
    if not isinstance(path, (list, tuple)) or not 7 <= len(path) <= 65536:
        raise GeometryError("native_compound_path needs bounded flat L/ARC rings")
    start = (_number(path[0]), _number(path[1]))
    current, points, index, line_mode = start, [start], 2, False
    while index < len(path):
        token = path[index]
        if isinstance(token, str):
            index += 1
            if token == 'L':
                line_mode = True
                continue
            if token != 'ARC' or index + 2 >= len(path):
                raise GeometryError("native_compound_path only supports documented L/ARC tokens")
            angle = _number(path[index])
            end = (_number(path[index + 1]), _number(path[index + 2]))
            index += 3
            line_mode = False
            if not 0 < abs(angle) < 360 or current == end:
                raise GeometryError("native_compound_path supports nondegenerate two-endpoint arcs below 360 degrees")
            theta = math.radians(angle)
            dx, dy = end[0] - current[0], end[1] - current[1]
            chord = math.hypot(dx, dy)
            # A short arc can be bounded by its chord without constructing an
            # ill-conditioned, extremely distant circle center.
            sagitta = chord * math.tan(abs(theta) / 4) / 2
            if sagitta <= tolerance / 4:
                samples = [end]
            else:
                radius = chord / (2 * math.sin(abs(theta) / 2))
                height = chord / (2 * math.tan(theta / 2))
                center = (current[0] + dx / 2 - dy / chord * height,
                          current[1] + dy / 2 + dx / chord * height)
                if not all(math.isfinite(v) for v in (*center, radius)):
                    raise GeometryError("native_compound_path arc exceeds coordinate range")
                roundoff = 128 * max(math.ulp(v) for v in (*current, *end, *center, radius))
                if roundoff > tolerance / 8:
                    raise GeometryError("native_compound_path tolerance is below coordinate resolution")
                # 1-cos(step/2) <= step**2/8, so each chord's sagitta is
                # bounded by tolerance/4, including both half-circle signs.
                step = min(math.pi / 8, math.sqrt(2 * tolerance / radius))
                count = max(1, math.ceil(abs(theta) / step))
                if count > remaining - len(points):
                    raise GeometryError("native_compound_path exceeds generated-vertex budget")
                a = math.atan2(current[1] - center[1], current[0] - center[0])
                samples = [(center[0] + radius * math.cos(a + theta * j / count),
                            center[1] + radius * math.sin(a + theta * j / count))
                           for j in range(1, count)] + [end]
            points.extend(samples)
            current = end
        else:
            if not line_mode or index + 1 >= len(path):
                raise GeometryError("native_compound_path needs explicit L/ARC commands")
            end = (_number(path[index]), _number(path[index + 1]))
            index += 2
            if end != current:
                points.append(end)
            current = end
        if len(points) > remaining:
            raise GeometryError("native_compound_path exceeds generated-vertex budget")
    # TPCB_PolygonSourceArray specifies an implicit closing straight segment.
    # At a few ULPs that extra segment can make the sampled polygon self-cross.
    # Only in that numerical interval, approximate the last sample by the first
    # within the already allocated roundoff budget. The original path is not
    # edited, and the inner/outer buffers cover both native endpoint positions.
    if current != start:
        roundoff = 128 * max(math.ulp(v) for v in (*current, *start))
        if math.dist(current, start) <= roundoff < tolerance / 8:
            points[-1] = start
        else:
            points.append(start)
    if len(set(points)) < 3:
        raise GeometryError("native_compound_path rings must enclose positive area")
    polygon = Polygon(points)
    if not polygon.is_valid or polygon.is_empty or polygon.area <= 0:
        raise GeometryError("native_compound_path sampled ring is not simple; topology remains unproven: " + explain_validity(polygon))
    # Circumscribe the round-buffer approximation. Its minimum radius is
    # tolerance/2, more than sagitta+roundoff; total deviation stays below tol.
    distance = (tolerance / 2) / math.cos(math.pi / 64)
    inner = polygon.buffer(-distance, quad_segs=16)
    outer = polygon.buffer(distance, quad_segs=16)
    bounds = _checked(inner, outer)
    return polygon, bounds, len(points)


@lru_cache(maxsize=128)
def _compound_cached(encoded, tolerance):
    paths = json.loads(encoded)
    if not isinstance(paths, list) or not 1 <= len(paths) <= 2048:
        raise GeometryError("native_compound_path must contain 1..2048 native polygon rings")
    rings, remaining = [], 300000
    for path in paths:
        polygon, bounds, used = _sample_ring(path, tolerance, remaining)
        remaining -= used
        rings.append((polygon, bounds))
    containers = [set() for _ in rings]
    for i, (_, a) in enumerate(rings):
        for j in range(i):
            b = rings[j][1]
            if a.outer.disjoint(b.outer):
                continue
            if a.inner.covers(b.outer):
                containers[j].add(i)
            elif b.inner.covers(a.outer):
                containers[i].add(j)
            else:
                raise GeometryError("native_compound_path ring intersection/containment is uncertain at this tolerance")
    # For strictly nested alternating rings, nonzero and even-odd fill rules
    # agree. Reject same-direction nesting rather than inventing hole semantics.
    for i, parents in enumerate(containers):
        if parents:
            parent = min(parents, key=lambda j: rings[j][0].area)
            if rings[parent][0].exterior.is_ccw == rings[i][0].exterior.is_ccw:
                raise GeometryError("native_compound_path nested ring winding does not establish hole semantics")
    inner, outer = GeometryCollection(), GeometryCollection()
    for i in sorted(range(len(rings)), key=lambda i: len(containers[i])):
        bounds = rings[i][1]
        if len(containers[i]) % 2 == 0:
            inner, outer = inner.union(bounds.inner), outer.union(bounds.outer)
        else:
            inner, outer = inner.difference(bounds.outer), outer.difference(bounds.inner)
    result = _checked(inner, outer)
    if not result.outer.covers(result.inner):
        raise GeometryError("native_compound_path bounds are numerically inconsistent")
    return result


def native_compound_bounds(region, tolerance=0.001):
    if not isinstance(region, dict) or 'native_compound_path' not in region:
        raise GeometryError("native_compound_path region required")
    if any(k in region for k in ('points', 'holes', 'native_path')):
        raise GeometryError("native_compound_path cannot have a competing chord/holes representation")
    tolerance = _number(tolerance)
    if tolerance <= 0:
        raise GeometryError("native_compound_path tolerance must be positive")
    try:
        encoded = json.dumps(region['native_compound_path'], allow_nan=False, separators=(',', ':'))
    except (TypeError, ValueError) as error:
        raise GeometryError("native_compound_path must be serializable finite geometry") from error
    # Tiny fixed teardrops must not evict expensive multi-hole plane geometry
    # from the bounded cache during repeated physical connectivity checks.
    if len(encoded) < 512:
        return _compound_cached.__wrapped__(encoded, tolerance)
    return _compound_cached(encoded, tolerance)


def native_stroke_region(paths, width, factor):
    """输入原生两点直线、宽度和已取证比例，输出 mil 铜笔画；未知语法抛出 GeometryError。"""
    width, factor = _number(width), _number(factor)
    if (width <= 0 or factor <= 0 or not isinstance(paths, (list, tuple)) or len(paths) != 1
            or not isinstance(paths[0], (list, tuple)) or len(paths[0]) != 5 or paths[0][2] != 'L'):
        raise GeometryError('只支持具有正宽度的单条显式两点 L 铜笔画')
    p = paths[0]
    points = [[_number(p[0])*factor, _number(p[1])*factor], [_number(p[3])*factor, _number(p[4])*factor]]
    if points[0] == points[1] or not all(math.isfinite(v) for q in points for v in q) or not math.isfinite(width*factor):
        raise GeometryError('铜笔画退化或单位转换超出有限数范围')
    return {'native_stroke_path': {'points': points, 'width': width*factor}}


def native_stroke_bounds(region, tolerance=0.001):
    """输入 mil 铜笔画，输出保守内外界；字段、尺寸或精度非法时抛出 GeometryError。

    API 未给出端帽样式，因此内界使用平头、外界使用方头，覆盖平头/圆头/方头。
    连通只依据内界、间距依据外界；绝不将未知端帽当作已证导电区域。
    仅处理实际已见直线，不将曲线用弦替代。
    """
    if not isinstance(region, dict) or set(region) != {'native_stroke_path'}:
        raise GeometryError('铜笔画不能同时提供其他几何字段')
    stroke = region['native_stroke_path']
    if not isinstance(stroke, dict) or set(stroke) != {'points', 'width'}:
        raise GeometryError('铜笔画只支持 points 与 width')
    pts = stroke['points']
    if not isinstance(pts, (list, tuple)) or len(pts) != 2 or any(not isinstance(p, (list, tuple)) or len(p) != 2 for p in pts):
        raise GeometryError('铜笔画必须恰含两个端点')
    pts = [tuple(_number(v) for v in p) for p in pts]
    width, tolerance = _number(stroke['width']), _number(tolerance)
    if width <= 0 or tolerance <= 0 or pts[0] == pts[1]:
        raise GeometryError('铜笔画宽度和误差必须为正，端点必须不同')
    margin = max(128*math.ulp(max(1, abs(v))) for p in pts for v in p)
    margin = max(margin, 128*math.ulp(width))
    if margin >= min(tolerance, width/4, math.dist(*pts)/4):
        raise GeometryError('铜笔画坐标精度不足以建立保守界')
    line = LineString(pts)
    inner = line.buffer(width/2, cap_style=2).buffer(-margin, join_style=2)
    outer = line.buffer(width/2, cap_style=3).buffer(margin, join_style=2)
    return _checked(inner, outer)


def scale_native_paths(paths, factor):
    """Apply a separately proven positive scale to XY, never to arc angles.

    This function performs no inference and does not itself establish a native
    coordinate profile. Callers retain source-bound calibration evidence.
    """
    factor = _number(factor)
    if factor <= 0 or not isinstance(paths, (list, tuple)) or not paths:
        raise GeometryError("A proven positive scale and native polygon paths are required")
    result = []
    for path in paths:
        if not isinstance(path, (list, tuple)) or len(path) < 7:
            raise GeometryError("Invalid native coordinate path")
        scaled = [_number(path[0]) * factor, _number(path[1]) * factor]
        index, line_mode = 2, False
        while index < len(path):
            token = path[index]
            if isinstance(token, str):
                index += 1
                if token == 'L':
                    line_mode = True
                    scaled.append(token)
                elif token == 'ARC' and index + 2 < len(path):
                    scaled.extend([token, _number(path[index]), _number(path[index + 1]) * factor,
                                   _number(path[index + 2]) * factor])
                    index += 3
                    line_mode = False
                else:
                    raise GeometryError("Unsupported native coordinate path syntax")
            else:
                if not line_mode or index + 1 >= len(path):
                    raise GeometryError("Native coordinates need explicit L/ARC commands")
                scaled.extend([_number(path[index]) * factor, _number(path[index + 1]) * factor])
                index += 2
        if any(isinstance(v, float) and not math.isfinite(v) for v in scaled):
            raise GeometryError("Scaled native coordinates exceed finite range")
        result.append(scaled)
    return result


def verify_native_path_mapping(native_paths, mil_paths, factor, tolerance=1e-7):
    """Check every XY and ARC angle against independently established mil paths.

    This proves the supplied mapping, not the units of an arbitrary second API.
    The collector must retain the native source/Fill evidence establishing that
    mil_paths are in PCB mil. A matching bounding box is insufficient.
    """
    tolerance = _number(tolerance)
    if tolerance <= 0:
        raise GeometryError("Coordinate mapping tolerance must be positive")
    scaled = scale_native_paths(native_paths, factor)
    known = scale_native_paths(mil_paths, 1)
    if len(scaled) != len(known):
        raise GeometryError("Native and known-mil path ring counts differ")
    maximum, coordinates, angles = 0.0, 0, 0
    for expected, actual in zip(scaled, known):
        if len(expected) != len(actual):
            raise GeometryError("Native and known-mil path structures differ")
        for index, (a, b) in enumerate(zip(expected, actual)):
            if isinstance(a, str) or isinstance(b, str):
                if a != b:
                    raise GeometryError("Native and known-mil path commands differ")
            elif index and expected[index - 1] == 'ARC':
                if a != b:
                    raise GeometryError("Native to mil conversion changed an ARC angle")
                angles += 1
            else:
                error = abs(a - b)
                if error > tolerance:
                    raise GeometryError("Native coordinates do not match independently established mil geometry")
                maximum = max(maximum, error)
                coordinates += 1
    return dict(xy_scale=factor, coordinate_pairs=coordinates // 2,
                arc_angles_verified=angles, max_error_mil=maximum, tolerance_mil=tolerance)


def _native_records(text, document_uuid):
    if not isinstance(text, str):
        raise GeometryError('Complete native calibration source is required')
    result = []
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            head, payload = line.strip().removesuffix('|').split('||', 1)
            result.append((json.loads(head), json.loads(payload)))
        except (ValueError, TypeError) as error:
            raise GeometryError('Invalid native calibration record') from error
    heads = [v for h, v in result if h.get('type') == 'DOCHEAD']
    if len(heads) != 1 or heads[0].get('docType') != 'PCB' or heads[0].get('uuid') != document_uuid:
        raise GeometryError('Calibration native source belongs to another PCB')
    return result


def validate_via_anchor_calibration(calibration, document_uuid, expected_scale):
    """输入原始标定记录、文档 UUID 与比例，返回来源互证和锚点拟合证明。

    坐标仅允许同 ID 源码的四位小数编码；保留原始测量不变，使用源码坐标拟合。
    身份、单位、孔径、编码或锚点拟合不符时抛出 GeometryError，不放宽拟合容差。
    """
    rows = _native_records(calibration.get('source_native'), document_uuid)
    tolerance = _number(calibration.get('tolerance_mil', 1e-7))
    if not 0 < tolerance <= 1e-6:
        raise GeometryError('Via anchors need precise coordinate correspondence')
    parents = [v for h, v in rows if h.get('type') == 'POUR' and h.get('id') == calibration.get('pour_id')]
    poured = [v for h, v in rows if h.get('type') == 'POURED' and h.get('id') == calibration.get('poured_id')]
    try:
        identity = json.loads(calibration['poured_id'])
    except (ValueError, KeyError, TypeError):
        identity = None
    if len(parents) != 1 or len(poured) != 1 or identity != ['POURED', calibration.get('pour_id')]:
        raise GeometryError('Native Poured parent identity is unproven')
    regions = [r for r in poured[0].get('pourFill', []) if r.get('id') == calibration.get('region_id')]
    if (len(regions) != 1 or regions[0].get('fill') is not True or regions[0].get('strokeWidth') != 0
            or regions[0].get('path') != calibration.get('raw_paths')):
        raise GeometryError('Actual circular-hole paths differ from the native Poured source')
    api = calibration.get('via_api', [])
    if not isinstance(api, list) or len({v.get('id') for v in api}) != len(api):
        raise GeometryError('Unique actual via getters required')
    by_id = {v['id']: v for v in api}
    vias, getter_position_quantization_proofs = [], []
    for h, v in rows:
        if h.get('type') != 'VIA':
            continue
        a = by_id.get(h.get('id'), {})
        if v.get('viaType') != 'NORMAL' or a.get('via_type') != 0 or a.get('net') != v.get('netName'):
            raise GeometryError('Actual VIA source/getter identity differs')
        for key, field in [('x','centerX'),('y','centerY'),('hole','holeDiameter'),('diameter','viaDiameter')]:
            x, y = _number(a.get(key)), _number(v.get(field))
            if abs(x-y) > 16*math.ulp(max(1,abs(x),abs(y))):
                # 与 JavaScript Math.round 一致，避免 Python round 的偶数舍入差异。
                if key not in ('x', 'y') or abs(x-y) > .00005 or math.floor(x*10000+.5)/10000 != y:
                    raise GeometryError('Actual VIA source and API units/encoding differ')
                getter_position_quantization_proofs.append(dict(id=a['id'],field=key,api=x,
                    native_source=y,error_mil=abs(x-y),serialization_decimals=4))
        if a['net'] != parents[0].get('netName'):
            vias.append(dict(a,x=v['centerX'],y=v['centerY']))
    circles = []
    for index, path in enumerate(scale_native_paths(calibration['raw_paths'], 1)):
        if (len(path) == 10 and path[2] == path[6] == 'ARC' and path[3] == path[7] and abs(path[3]) == 180
                and math.dist(path[:2], path[-2:]) <= 32*max(math.ulp(v) for v in (*path[:2], *path[-2:]))):
            circles.append(dict(ring_index=index, x=(path[0]+path[4])/2, y=(path[1]+path[5])/2,
                                radius=math.dist(path[:2],path[4:6])/2))
    extent = max([1]+[abs(q[k]) for q in circles for k in ('x','y')])
    seeds = []
    for q in circles:
        for v in vias:
            key = 'x' if abs(q['x']) > abs(q['y']) else 'y'
            if not q[key]:
                continue
            factor = v[key]/q[key]
            if (factor > 0 and all(abs(q[k]*factor-v[k]) <= tolerance for k in ('x','y'))
                    and q['radius']*factor > v['diameter']/2
                    and not any(abs(s-factor)*extent <= tolerance for s in seeds)):
                seeds.append(factor)
    supported = []
    for factor in seeds:
        matches = []
        for q in circles:
            found = [v for v in vias if all(abs(q[k]*factor-v[k]) <= tolerance for k in ('x','y')) and q['radius']*factor > v['diameter']/2]
            if len(found) > 1:
                matches = [];break
            if found:
                matches.append(dict(q,via_id=found[0]['id'],mil_x=found[0]['x'],mil_y=found[0]['y']))
        if len(matches) < 8 or len({m['via_id'] for m in matches}) != len(matches):
            continue
        a = matches[0]
        b = next((b for b in matches if math.hypot(a['x']-b['x'], a['y']-b['y']) > tolerance), None)
        if b and any(abs((b['x']-a['x'])*(q['y']-a['y'])-(b['y']-a['y'])*(q['x']-a['x'])) > tolerance*tolerance for q in matches):
            supported.append((factor,matches))
    if len(supported) != 1:
        raise GeometryError('Exactly one scale needs eight unique non-collinear via anchors')
    _, matches = supported[0]
    mean = lambda key: sum(m[key] for m in matches)/len(matches)
    mx,my,vx,vy = [mean(k) for k in ('x','y','mil_x','mil_y')]
    numerator = sum((m['x']-mx)*(m['mil_x']-vx)+(m['y']-my)*(m['mil_y']-vy) for m in matches)
    denominator = sum((m['x']-mx)**2+(m['y']-my)**2 for m in matches)
    factor = numerator/denominator
    translation = [vx-factor*mx,vy-factor*my]
    expected_scale = _number(expected_scale)
    if (factor <= 0 or any(abs(v) > tolerance for v in translation)
            or any(abs(m[k]*expected_scale-m['mil_'+k]) > tolerance for m in matches for k in ('x','y'))):
        raise GeometryError('Actual via anchors do not prove this scale and zero translation')
    return dict(coordinate_scale=factor,translation_mil=translation,matched_circle_count=len(matches),circle_count=len(circles),matches=matches,
                getter_position_quantization_proofs=getter_position_quantization_proofs)


def validate_native_pour_snapshot(board, evidence):
    """输入导出板与取证记录，返回比例证明；来源、清单或几何不一致时抛出 GeometryError。

    每个铺铜对象还须与完整原生来源中的唯一记录一致，不能仅比较可同时改动的
    局部副本。调用方仍负责用真实原生读取建立来源指纹与取证的可信关系。
    """
    source = board.get('metadata', {}).get('source')
    profile = board.get('metadata', {}).get('native_pour_geometry')
    if (not isinstance(profile, dict) or evidence.get('coordinate_profile') != profile
            or profile.get('source') != source or profile.get('mode') != 'local_preview_only'):
        raise GeometryError('Actual adapter coordinate profile and matching native evidence are required')
    calibration = profile.get('calibration', {})
    if calibration.get('method') not in ('native_poured_convert_to_fill','native_poured_via_anchors'):
        raise GeometryError('Native Poured to Fill conversion records are required')

    if calibration['method'] == 'native_poured_via_anchors':
        proof = validate_via_anchor_calibration(calibration, source['document_uuid'], profile.get('coordinate_scale'))
    else:
        proof = _validate_conversion_records(calibration, source['document_uuid'], profile.get('coordinate_scale'))
    # 完整原生来源作为比对基准；禁止同时改动局部 source/API/几何副本蒙混过关。
    original_source = calibration.get('source_native') if calibration['method'] == 'native_poured_via_anchors' else calibration.get('source_before')
    original_records = _native_records(original_source, source['document_uuid'])
    original_pours = {}
    for header, payload in original_records:
        if header.get('type') != 'POURED':
            continue
        primitive_id = header.get('id')
        if not isinstance(primitive_id, str) or primitive_id in original_pours:
            raise GeometryError('完整原生来源中的铺铜对象 ID 必须唯一')
        original_pours[primitive_id] = {'type': 'POURED', 'id': primitive_id, 'payload': payload}
    exported_pours = [p for p in board.get('routes', {}).get('copper_pours', []) if p.get('source_kind') == 'Poured']
    exported_ids = [p.get('id') for p in exported_pours]
    if len(set(exported_ids)) != len(exported_ids) or set(exported_ids) != set(original_pours):
        raise GeometryError('导出铺铜对象全集必须与完整原生来源一致，不得遗漏或重复')
    factor = _number(profile.get('coordinate_scale'))
    for poured in exported_pours:
        metadata = poured.get('metadata', {})
        record = metadata.get('native_source_record', {})
        if record != original_pours[poured['id']]:
            raise GeometryError('铺铜局部来源记录与完整原生来源不一致')
        actual = record.get('payload', {}).get('pourFill', [])
        raw = metadata.get('native_poured_regions', [])
        polygons = poured.get('filled_polygons', [])
        if (record.get('type') != 'POURED' or record.get('id') != poured.get('id')
                or not raw or len(raw) != len(actual) or len(raw) != len(polygons)):
            raise GeometryError('Each actual Poured object must retain its complete native region inventory')
        if len({r.get('id') for r in raw}) != len(raw):
            raise GeometryError('Duplicate actual Poured region identity')
        for region, polygon in zip(raw, polygons):
            matching = [r for r in actual if r.get('id') == region.get('id')]
            if len(matching) != 1 or matching[0].get('path') != region.get('path'):
                raise GeometryError('Actual Poured source, API and mil coordinates must match every region')
            original = matching[0]
            if (('fill' in region and region['fill'] != original.get('fill'))
                    or ('lineWidth' in region and region['lineWidth'] != original.get('strokeWidth'))):
                raise GeometryError('原生铺铜 API 填充状态与线宽必须与完整来源一致')
            if original.get('fill') is True and original.get('strokeWidth') == 0:
                expected = {'native_compound_path': scale_native_paths(region['path'], factor)}
            elif original.get('fill') is False and original.get('strokeWidth', 0) > 0:
                if region.get('fill') is not False or region.get('lineWidth') != original['strokeWidth']:
                    raise GeometryError('铜笔画必须保留来源与 API 一致的填充状态及宽度')
                expected = native_stroke_region(region['path'], original['strokeWidth'], factor)
            else:
                raise GeometryError('原生铜区填充与笔画组合尚未支持')
            if polygon != expected:
                raise GeometryError('原生铜区或辐条的来源、API 与 mil 几何不一致')
    return proof


def _validate_conversion_records(calibration, document_uuid, factor):
    before, converted = _native_records(calibration.get('source_before'),document_uuid), _native_records(calibration.get('source_converted'),document_uuid)
    original = [v for h, v in before if h.get('type') == 'POURED' and h.get('id') == calibration.get('poured_id')]
    fills = [v for h, v in converted if h.get('type') == 'FILL' and h.get('id') == calibration.get('fill_id')]
    if (len(original) != 1 or len(fills) != 1 or fills[0].get('fillStyle') != 'SOLID'
            or any(h.get('type') == 'FILL' and h.get('id') == calibration.get('fill_id') for h, _ in before)):
        raise GeometryError('Unique original Poured and newly converted Fill required')
    selected = [r for r in original[0].get('pourFill', []) if r.get('id') == calibration.get('region_id')]
    if (len(selected) != 1 or selected[0].get('fill') is not True or selected[0].get('strokeWidth') != 0
            or selected[0].get('path') != calibration.get('raw_paths')):
        raise GeometryError('Calibration API paths differ from actual native Poured source')
    tolerance = _number(calibration.get('tolerance_mil', 1e-7))
    if not 0 < tolerance <= .001:
        raise GeometryError('Native coordinate calibration tolerance must be <=0.001 mil')
    proof = verify_native_path_mapping(calibration['raw_paths'], fills[0].get('path'), factor, tolerance)
    return proof
