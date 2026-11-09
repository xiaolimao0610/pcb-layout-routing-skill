"""有界 Gerber 最终铜图：按文件顺序合并暗铜、扣除清铜，保存保守内外界。"""
import math

from shapely import affinity, get_num_coordinates
from shapely.geometry import GeometryCollection, LineString, Point, Polygon, box
from shapely.ops import unary_union

from pcb_geometry import Bounds, GeometryError, _checked, _quad_segments, _rounded


class ImageRenderer:
    """输入预算与毫米误差，逐命令构造最终铜图；不支持语法由上游拒绝。

    公共方法输入已解析的有限坐标/光圈；输出 Bounds 或无返回值。
    非简单区域、数值不稳定、非圆光圈圆弧和预算不足均抛 GeometryError。
    """
    def __init__(self, budget, tolerance_mm=.00001):
        self.budget, self.tolerance = budget, tolerance_mm
        self.inner = self.outer = GeometryCollection()
        self.pending, self.dark = [], True
        self.mirror, self.rotation, self.scale = 'N', 0.0, 1.0
        self.contours, self.current, self.arc_error = [], [], 0.0

    def _flush(self):
        self.budget.use()
        if not self.pending:
            return
        inner = unary_union([p.inner for p in self.pending])
        outer = unary_union([p.outer for p in self.pending])
        if self.dark:
            self.inner, self.outer = self.inner.union(inner), self.outer.union(outer)
        else:
            self.inner, self.outer = self.inner.difference(outer), self.outer.difference(inner)
        self.pending.clear()
        self.budget.use()

    def polarity(self, dark):
        """输入下一段暗/清极性，先结算先前命令；预算不足时抛异常。"""
        self._flush(); self.dark = dark

    def _add(self, bounds):
        self.budget.use('max_primitives')
        self.pending.append(bounds)
        if len(self.pending) >= 256:
            self._flush()

    def _round(self, base, radius, tolerance):
        # 在分配缓冲多边形之前计入保守顶点上界，而非仅计中心线端点。
        count = int(get_num_coordinates(base))
        self.budget.use('max_vertices', 8 * _quad_segments(radius, tolerance) * max(1, count) + 16)
        return _rounded(base, radius, tolerance)

    def _aperture(self, aperture):
        kind, size = aperture
        values = [v * self.scale for v in size]
        if kind == 'C':
            result = self._round(Point(0, 0), values[0] / 2, self.tolerance / 4)
        elif kind == 'R':
            poly = box(-values[0]/2, -values[1]/2, values[0]/2, values[1]/2)
            self.budget.use('max_vertices', 10)
            result = Bounds(poly, poly)
        else:
            w, h = values
            points = [(-(w-h)/2, 0), ((w-h)/2, 0)] if w >= h else [(0, -(h-w)/2), (0, (h-w)/2)]
            result = self._round(Point(0, 0) if w == h else LineString(points), min(w, h)/2, self.tolerance/4)
        def transform(shape):
            shape = affinity.scale(shape, xfact=-1 if 'X' in self.mirror else 1,
                                   yfact=-1 if 'Y' in self.mirror else 1, origin=(0, 0))
            return affinity.rotate(shape, self.rotation, origin=(0, 0))
        return _checked(transform(result.inner), transform(result.outer))

    def _arc(self, start, end, center, mode):
        radius = math.dist(start, center)
        error = abs(radius - math.dist(end, center))
        begin, stop = math.atan2(start[1]-center[1], start[0]-center[0]), math.atan2(end[1]-center[1], end[0]-center[0])
        sweep = (stop-begin) % (2*math.pi) if mode == 3 else -((begin-stop) % (2*math.pi))
        if start == end:
            sweep = 2*math.pi if mode == 3 else -2*math.pi
        step = min(math.pi/8, math.sqrt(2*self.tolerance/radius))
        count = max(1, math.ceil(abs(sweep)/step))
        self.budget.use('max_vertices', count + 1)
        points = [start] + [(center[0]+radius*math.cos(begin+sweep*j/count),
                            center[1]+radius*math.sin(begin+sweep*j/count)) for j in range(1, count)] + [end]
        return points, self.tolerance/4 + error

    def draw(self, aperture, start, end, mode, center=None, in_region=False):
        """输入光圈、端点、插补模式及可选圆心，累积保守铜界；不支持几何抛异常。"""
        if mode == 1:
            points, error = [start, end], 0.0
            self.budget.use('max_vertices', 2)
        else:
            points, error = self._arc(start, end, center, mode)
        if in_region:
            self.current.extend(points[1:]); self.arc_error = max(self.arc_error, error)
            return
        if start == end and mode == 1:
            self.flash(aperture, end); return
        if mode != 1 and aperture[0] != 'C':
            raise GeometryError('unsupported: 非圆光圈圆弧扫掠尚未支持')
        if aperture[0] == 'C':
            radius = aperture[1][0] * self.scale / 2
            if error >= radius:
                raise GeometryError('圆弧误差超过光圈半径')
            line = LineString(points)
            bounds = _checked(self._round(line, radius-error, self.tolerance/4).inner,
                              self._round(line, radius+error, self.tolerance/4).outer)
        else:
            aperture_bounds = self._aperture(aperture)
            # 标准 R/O 光圈都是凸集，其直线扫掠恰为两端平移后的凸包。
            def sweep(shape):
                return unary_union([affinity.translate(shape, *start), affinity.translate(shape, *end)]).convex_hull
            bounds = _checked(sweep(aperture_bounds.inner), sweep(aperture_bounds.outer))
        self._add(bounds)

    def flash(self, aperture, position):
        """输入标准光圈和闪光位置，累积变换后的铜界；非法几何或预算不足抛异常。"""
        bounds = self._aperture(aperture)
        self._add(_checked(affinity.translate(bounds.inner, *position), affinity.translate(bounds.outer, *position)))

    def begin_region(self):
        """开始由上游验证过非嵌套的区域，无入参与返回值。"""
        self.contours, self.current, self.arc_error = [], [], 0.0

    def move_region(self, position):
        """输入新轮廓起点，保存上一轮廓；闭合与有效性在区域终点校验。"""
        if self.current:
            self.contours.append(self.current)
        self.current = [position]

    def end_region(self):
        """结算所有轮廓的奇偶铜面，无返回值；开口、自交或预算不足抛异常。"""
        if self.current:
            self.contours.append(self.current)
        result = GeometryCollection()
        for points in self.contours:
            if len(points) < 4 or points[0] != points[-1]:
                raise GeometryError('unsupported: 区域轮廓必须显式闭合')
            polygon = Polygon(points)
            if not polygon.is_valid or polygon.area <= 0:
                raise GeometryError('unsupported: 非简单或退化区域')
            # Gerber 区域按奇偶规则填充；不推断环方向，不填掉孔洞。
            result = result.symmetric_difference(polygon)
            self.budget.use()
        if not result.is_valid:
            raise GeometryError('区域奇偶布尔运算无效')
        margin = self.arc_error + self.tolerance
        self.budget.use('max_vertices', 130 * int(get_num_coordinates(result)))
        self._add(_checked(result.buffer(-margin, quad_segs=16), result.buffer(margin/math.cos(math.pi/64), quad_segs=16)))
        self.contours, self.current = [], []

    def finish(self):
        """无入参，输出全文件最终铜面的保守内外界；保留所有孔洞和孤岛。"""
        self._flush()
        if self.outer.is_empty:
            return Bounds(GeometryCollection(), GeometryCollection())
        return _checked(self.inner, self.outer)
