"""Planar copper geometry with inner/outer bounds for curved boundaries.

Lengths, coordinates and tolerance use the caller's common unit. Angles are
degrees. Curves are bracketed by polygons rather than treated as exact copper.
GEOS uses floating-point arithmetic; this is not an electromagnetic model.
"""

from dataclasses import dataclass
from functools import lru_cache
import json
import math
from numbers import Real
from collections.abc import Mapping

from shapely import affinity
from shapely.geometry import Point, MultiPoint, LineString, Polygon, box
from shapely.geometry.base import BaseGeometry
from shapely.validation import explain_validity


class GeometryError(ValueError):
    """Missing, invalid or unrepresentable geometric input."""


@dataclass(frozen=True)
class Bounds:
    """Inner subset of true copper, itself a subset of outer.

    An empty inner geometry is allowed, for example for a very thin ring.
    Polygonal input has identical inner and outer geometries.
    """

    inner: BaseGeometry
    outer: BaseGeometry


def _finite(value, name):
    if isinstance(value, bool) or not isinstance(value, Real):
        raise GeometryError(f"{name} must be a finite number")
    try:
        value = float(value)
    except OverflowError as exc:
        raise GeometryError(f"{name} must be finite") from exc
    if not math.isfinite(value):
        raise GeometryError(f"{name} must be finite")
    return value


def _number(spec, name, *, positive=False):
    if name not in spec:
        raise GeometryError(f"missing {name}")
    value = _finite(spec[name], name)
    if positive and value <= 0:
        raise GeometryError(f"{name} must be positive")
    return value


def _mapping(spec):
    if not isinstance(spec, Mapping):
        raise GeometryError("shape or primitive specification must be a mapping")


def _tolerance(tolerance):
    tolerance = _finite(tolerance, "tolerance")
    if tolerance <= 0:
        raise GeometryError("tolerance must be positive")
    return tolerance


def _ring(points, name):
    if isinstance(points, (str, bytes)):
        raise GeometryError(f"{name} must contain coordinate pairs")
    try:
        result = []
        for point in points:
            if len(point) != 2:
                raise GeometryError(f"{name} must contain 2D coordinate pairs")
            result.append((_finite(point[0], name), _finite(point[1], name)))
    except (TypeError, IndexError) as exc:
        raise GeometryError(f"{name} must contain coordinate pairs") from exc
    if len(set(result)) < 3:
        raise GeometryError(f"{name} needs at least three distinct vertices")
    return result


def solid_polygon(points, holes=()):
    """Return a valid, positive-area Polygon; preserve the supplied coordinates."""
    shell = _ring(points, "points")
    try:
        interiors = [_ring(ring, "hole") for ring in holes]
    except TypeError as exc:
        raise GeometryError("holes must be a sequence of rings") from exc
    polygon = Polygon(shell, interiors)
    if polygon.is_empty or not polygon.is_valid or polygon.area <= 0:
        raise GeometryError(f"invalid polygon: {explain_validity(polygon)}")
    if not math.isfinite(polygon.area):
        raise GeometryError("polygon area exceeds floating-point range")
    return polygon


def _native_segments(path):
    """Parse the deliberately limited, explicitly closed EasyEDA L/ARC profile."""
    if not isinstance(path, (list, tuple)) or not 8 <= len(path) <= 8192:
        raise GeometryError("native_path must be a bounded flat closed L/ARC path")
    start = (_finite(path[0], "native_path.x"), _finite(path[1], "native_path.y"))
    points, segments = [start], []
    i, line_mode, arc_count = 2, False, 0
    while i < len(path):
        token = path[i]
        if isinstance(token, str):
            i += 1
            if token == "L":
                line_mode = True
                if i + 1 >= len(path) or isinstance(path[i], str):
                    raise GeometryError("native_path L needs an endpoint")
            elif token == "ARC":
                if i + 2 >= len(path):
                    raise GeometryError("native_path ARC needs angle and endpoint")
                angle = _finite(path[i], "native_path ARC angle")
                if not 0 < abs(angle) < 180:
                    raise GeometryError("native_path supports nonzero minor ARC angles only")
                end = (_finite(path[i + 1], "native_path ARC x"),
                       _finite(path[i + 2], "native_path ARC y"))
                i += 3
                arc_count += 1
                if arc_count > 128:
                    raise GeometryError("native_path exceeds 128 arcs")
                segments.append((points[-1], end, angle))
                points.append(end)
                line_mode = False
                if end == segments[-1][0]:
                    raise GeometryError("native_path contains a zero-length segment")
                if len(segments) > 1024:
                    raise GeometryError("native_path exceeds 1024 segments")
                continue
            else:
                raise GeometryError("native_path supports explicit L and ARC commands only")
        if not line_mode or i + 1 >= len(path):
            raise GeometryError("native_path needs an explicit L or ARC command")
        end = (_finite(path[i], "native_path L x"),
               _finite(path[i + 1], "native_path L y"))
        i += 2
        if end == points[-1]:
            raise GeometryError("native_path contains a zero-length segment")
        segments.append((points[-1], end, 0.0))
        points.append(end)
        if len(segments) > 1024:
            raise GeometryError("native_path exceeds 1024 segments")
    if len(segments) < 3 or points[-1] != start:
        raise GeometryError("native_path must close explicitly at its exact starting point")
    return points[:-1], segments


def _minor_arc_cap(a, b, angle, tolerance, remaining_vertices):
    """Bracket the circular segment between a minor arc and its chord.

    Positive angle means counter-clockwise, as in EasyEDA's polygon format.
    Inscribed arc chords form the inner segment. Intersections of adjacent
    endpoint tangents form the outer segment. Their maximum radial errors are
    r*(1-cos(step/2)) and r*(sec(step/2)-1), respectively, both <= tolerance.
    Endpoints are retained verbatim, so their chord is shared with the base.
    """
    theta = math.radians(angle)
    dx, dy = b[0] - a[0], b[1] - a[1]
    length = math.hypot(dx, dy)
    tangent, sine = math.tan(theta / 2), math.sin(abs(theta) / 2)
    if not math.isfinite(length) or length <= 0 or tangent == 0 or sine == 0:
        raise GeometryError("native_path arc collapsed at the supplied coordinate scale")
    radius = length / (2 * sine)
    height = length / (2 * tangent)
    center = ((a[0] + dx / 2) - dy / length * height,
              (a[1] + dy / 2) + dx / length * height)
    if not all(math.isfinite(v) for v in (*center, radius)):
        raise GeometryError("native_path arc center/radius exceeds floating-point range")
    scale = max(abs(v) for v in (*a, *b, *center, radius))
    # GEOS is floating-point geometry. Refuse a requested error budget too small
    # to distinguish the arc rather than silently relaxing it or snapping it.
    if tolerance <= 128 * math.ulp(scale):
        raise GeometryError("native_path tolerance is below coordinate-scale resolution")
    cosine = radius / (radius + tolerance)
    if not 0 < cosine < 1:
        raise GeometryError("native_path tolerance is below arc-radius resolution")
    n = max(2, math.ceil(abs(theta) / (2 * math.acos(cosine))))
    if 2 * n + 3 > remaining_vertices:
        raise GeometryError("native_path exceeds 65536 generated arc vertices")
    start = math.atan2(a[1] - center[1], a[0] - center[0])
    samples = [a] + [(center[0] + radius * math.cos(start + theta * k / n),
                      center[1] + radius * math.sin(start + theta * k / n))
                     for k in range(1, n)] + [b]
    outer_radius = radius / math.cos(theta / (2 * n))
    if not math.isfinite(outer_radius) or outer_radius - radius > tolerance:
        raise GeometryError("native_path cannot meet the supplied arc tolerance")
    tangents = [(center[0] + outer_radius * math.cos(start + theta * (k + .5) / n),
                 center[1] + outer_radius * math.sin(start + theta * (k + .5) / n))
                for k in range(n)]
    inner = solid_polygon(samples)
    # A circular segment and its tangent bound are convex. Taking this hull
    # also retains the exact shared endpoints when floating-point trig leaves
    # an inscribed sample a few ulps beyond a computed tangent edge. No buffer,
    # coordinate snapping, or geometric-error allowance is added.
    outer = MultiPoint([a, *tangents, b, *samples]).convex_hull
    _checked(inner, outer)
    if not outer.covers(inner):
        raise GeometryError("native_path arc bounds are numerically ambiguous")
    return Bounds(inner, outer), 2 * n + 3


@lru_cache(maxsize=512)
def _cached_native_region_bounds(encoded, tolerance):
    return _filled_region_bounds(json.loads(encoded), tolerance)


def filled_region_bounds(region, tolerance=0.001):
    if isinstance(region, Mapping) and "native_stroke_path" in region:
        from pcb_native_poured_geometry import native_stroke_bounds
        return native_stroke_bounds(region, tolerance)
    if isinstance(region, Mapping) and "native_compound_path" in region:
        # Native Poured data uses multiple curved rings. Its coordinate profile
        # is proven by the collector; this helper proves conservative geometry.
        from pcb_native_poured_geometry import native_compound_bounds
        return native_compound_bounds(region, tolerance)
    # Native copper stays immutable during a route search. Reuse its proven
    # bounds instead of rebuilding hundreds of curved teardrops per candidate.
    if isinstance(region, Mapping) and "native_path" in region:
        tolerance = _tolerance(tolerance)
        try:
            encoded = json.dumps(region, sort_keys=True, separators=(",", ":"), allow_nan=False)
        except (TypeError, ValueError):
            return _filled_region_bounds(region, tolerance)
        cached = _cached_native_region_bounds(encoded, tolerance)
        return Bounds(cached.inner, cached.outer)
    return _filled_region_bounds(region, tolerance)


def _filled_region_bounds(region, tolerance=0.001):
    """Build filled copper from polygon points or a proven native L/ARC path.

    ``points`` is the exact ordered endpoint preview of ``native_path``; it
    cannot replace or override the curved geometry. Curved rings support no
    holes. A simple chord polygon and mutually separate circular-segment outer
    bounds prove the supported ring's topology. Complex or ambiguous paths are
    rejected, never repaired. The original points/holes-only path is unchanged.
    """
    _mapping(region)
    if any(k not in {"points", "holes", "native_path"} for k in region):
        raise GeometryError("filled polygon supports points, holes and native_path only")
    if "native_path" not in region:
        return shape_from_spec({"type": "POLYGON", "points": region["points"],
                                "holes": region.get("holes", ())}, tolerance)
    tolerance = _tolerance(tolerance)
    holes = region.get("holes", ())
    if not isinstance(holes, (list, tuple)) or holes:
        raise GeometryError("native_path does not support curved regions with holes")
    points, segments = _native_segments(region["native_path"])
    raw_preview = region.get("points")
    if not isinstance(raw_preview, (list, tuple)) or not 3 <= len(raw_preview) <= 1025:
        raise GeometryError("native_path points preview must contain at most 1025 coordinate pairs")
    preview = _ring(raw_preview, "native_path points preview")
    if preview[-1] == preview[0]:
        preview = preview[:-1]
    if preview != points:
        raise GeometryError("native_path points preview disagrees with path endpoints")
    base = solid_polygon(points)
    orientation = 1 if base.exterior.is_ccw else -1
    caps, remaining = [], 65536
    for idx, (a, b, angle) in enumerate(segments):
        if angle == 0:
            continue
        bounds, used = _minor_arc_cap(a, b, angle, tolerance, remaining)
        remaining -= used
        # Only its own chord and shared endpoints may meet the base boundary.
        endpoints = MultiPoint([a, b])
        for j, (c, d, _) in enumerate(segments):
            if j != idx and not bounds.outer.intersection(LineString([c, d])).difference(endpoints).is_empty:
                raise GeometryError("native_path arc bound meets another chord edge")
        outward = orientation * angle > 0
        if outward:
            if bounds.outer.intersection(base).area != 0:
                raise GeometryError("native_path outward arc bound enters chord polygon")
        elif not base.covers(bounds.outer):
            raise GeometryError("native_path inward arc bound leaves chord polygon")
        for old_a, old_b, _, old in caps:
            common = set((a, b)).intersection((old_a, old_b))
            intersection = bounds.outer.intersection(old.outer)
            if common:
                intersection = intersection.difference(MultiPoint(list(common)))
            if not intersection.is_empty:
                raise GeometryError("native_path circular-segment bounds intersect")
        caps.append((a, b, outward, bounds))
    inner, outer = base, base
    for _, _, outward, bounds in caps:
        if outward:
            inner, outer = inner.union(bounds.inner), outer.union(bounds.outer)
    for _, _, outward, bounds in caps:
        if not outward:
            inner, outer = inner.difference(bounds.outer), outer.difference(bounds.inner)
    result = _checked(inner, outer)
    if not outer.covers(inner):
        raise GeometryError("native_path final bounds are numerically ambiguous")
    return result


def _checked(inner, outer):
    for name, geom in (("inner", inner), ("outer", outer)):
        if not geom.is_valid:
            raise GeometryError(f"invalid {name} geometry: {explain_validity(geom)}")
        if not geom.is_empty and not all(math.isfinite(v) for v in geom.bounds):
            raise GeometryError(f"non-finite {name} geometry")
        if not math.isfinite(geom.area):
            raise GeometryError(f"{name} geometry area exceeds floating-point range")
    if outer.is_empty or outer.area <= 0:
        raise GeometryError("geometry collapsed at the supplied coordinate scale")
    return Bounds(inner, outer)


def _quad_segments(radius, tolerance):
    # A 4*q-sided outer polygon has circumradius r / cos(pi/(4*q)).
    # Its maximum radial excess is bounded by tolerance, as is the
    # corresponding inner polygon's deficit.
    cosine = radius / (radius + tolerance)
    if cosine >= 1.0:
        raise GeometryError("tolerance is below floating-point resolution for this radius")
    q = max(1, math.ceil(math.pi / (4.0 * math.acos(cosine))))
    if q > 65536:
        raise GeometryError("tolerance requires more than 65536 segments per quadrant")
    return q


def _rounded(base, radius, tolerance):
    q = _quad_segments(radius, tolerance)
    outer_radius = radius / math.cos(math.pi / (4.0 * q))
    if not math.isfinite(outer_radius):
        raise GeometryError("outer radius exceeds floating-point range")
    inner = base.buffer(radius, quad_segs=q, cap_style="round", join_style="round")
    outer = base.buffer(outer_radius, quad_segs=q, cap_style="round", join_style="round")
    return _checked(inner, outer)


def _core(half_width, half_height):
    if half_width == 0 and half_height == 0:
        return Point(0, 0)
    if half_width == 0:
        return LineString([(0, -half_height), (0, half_height)])
    if half_height == 0:
        return LineString([(-half_width, 0), (half_width, 0)])
    return box(-half_width, -half_height, half_width, half_height)


def shape_from_spec(spec, tolerance=0.001):
    """Build a local shape. ``type`` (or ``shape``) selects an explicit shape.

    RECTANGLE: width, height; CIRCLE: diameter; ELLIPSE/OVAL: width, height;
    ROUNDRECT: width, height, radius; POLYGON: points and optional holes.
    Parametric shapes are centered at (0, 0). POLYGON points are already local
    coordinates and are not silently recentered. No dimensions are inferred.
    """
    _mapping(spec)
    tolerance = _tolerance(tolerance)
    names = [spec[k] for k in ("type", "shape") if k in spec]
    if not names or any(not isinstance(name, str) for name in names):
        raise GeometryError("an explicit shape type is required")
    names = [name.strip().upper() for name in names]
    if len(set(names)) != 1:
        raise GeometryError("type and shape disagree")
    kind = names[0]

    if kind == "POLYGON":
        if "points" not in spec:
            raise GeometryError("missing points")
        polygon = solid_polygon(spec["points"], spec.get("holes", ()))
        return _checked(polygon, polygon)
    if kind == "CIRCLE":
        return _rounded(Point(0, 0), _number(spec, "diameter", positive=True) / 2, tolerance)
    if kind not in {"RECTANGLE", "ELLIPSE", "OVAL", "ROUNDRECT"}:
        raise GeometryError(f"unsupported shape type: {kind}")
    w = _number(spec, "width", positive=True)
    h = _number(spec, "height", positive=True)
    if kind == "RECTANGLE":
        polygon = box(-w / 2, -h / 2, w / 2, h / 2)
        return _checked(polygon, polygon)
    if kind == "ELLIPSE":
        radius = max(w, h) / 2
        circle = _rounded(Point(0, 0), radius, tolerance)
        return _checked(*(affinity.scale(g, xfact=w / (2 * radius),
                                        yfact=h / (2 * radius), origin=(0, 0))
                          for g in (circle.inner, circle.outer)))
    radius = min(w, h) / 2 if kind == "OVAL" else _number(spec, "radius")
    if radius < 0 or radius > min(w, h) / 2:
        raise GeometryError("radius must be between zero and half the smaller dimension")
    if radius == 0:
        polygon = box(-w / 2, -h / 2, w / 2, h / 2)
        return _checked(polygon, polygon)
    return _rounded(_core(w / 2 - radius, h / 2 - radius), radius, tolerance)


def transform(bounds, x, y, angle, flipped=False):
    """Mirror all local geometry in X, rotate in degrees, then translate."""
    if not isinstance(bounds, Bounds):
        raise GeometryError("transform requires Bounds")
    x, y, angle = _finite(x, "x"), _finite(y, "y"), _finite(angle, "angle")
    if not isinstance(flipped, bool):
        raise GeometryError("flipped must be boolean")
    result = []
    for geom in (bounds.inner, bounds.outer):
        if flipped:
            geom = affinity.scale(geom, xfact=-1, yfact=1, origin=(0, 0))
        geom = affinity.rotate(geom, angle % 360, origin=(0, 0))
        result.append(affinity.translate(geom, xoff=x, yoff=y))
    return _checked(*result)


def trace_bounds(tr, tolerance=0.001):
    """Copper capsule for sx, sy, ex, ey, width; a zero-length trace is a disk."""
    _mapping(tr)
    tolerance = _tolerance(tolerance)
    sx, sy, ex, ey = (_number(tr, key) for key in ("sx", "sy", "ex", "ey"))
    radius = _number(tr, "width", positive=True) / 2
    base = Point(sx, sy) if (sx, sy) == (ex, ey) else LineString([(sx, sy), (ex, ey)])
    return _rounded(base, radius, tolerance)


def via_bounds(v, tolerance=0.001):
    """Return (copper annulus Bounds, drill Bounds), using x/y/diameter/hole."""
    _mapping(v)
    tolerance = _tolerance(tolerance)
    x, y = _number(v, "x"), _number(v, "y")
    diameter = _number(v, "diameter", positive=True)
    hole = _number(v, "hole", positive=True)
    if hole >= diameter:
        raise GeometryError("hole must be smaller than diameter")
    disk = _rounded(Point(x, y), diameter / 2, tolerance)
    drill = _rounded(Point(x, y), hole / 2, tolerance)
    copper = _checked(disk.inner.difference(drill.outer),
                      disk.outer.difference(drill.inner))
    return copper, drill


__all__ = ["GeometryError", "Bounds", "shape_from_spec", "filled_region_bounds", "transform",
           "trace_bounds", "via_bounds", "solid_polygon"]
