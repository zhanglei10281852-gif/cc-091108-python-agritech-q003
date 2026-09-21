"""地理与面积计算。

坐标统一 WGS84 经纬度。平面近似：以参考纬度把经纬度差换算成米，
适用于县域尺度（几十公里内）田块面积/距离计算。
"""

from __future__ import annotations

import math

EARTH_RADIUS_M = 6_371_000.0
LNG_PER_M = 360.0 / (2 * math.pi * EARTH_RADIUS_M)


def to_meters(dlng: float, dlat: float, lat0: float) -> tuple[float, float]:
    """经纬度差 -> 以 lat0 为参考的米制位移 (x=东, y=北)。"""
    x = math.radians(dlng) * EARTH_RADIUS_M * math.cos(math.radians(lat0))
    y = math.radians(dlat) * EARTH_RADIUS_M
    return x, y


def haversine_m(a: tuple[float, float], b: tuple[float, float]) -> float:
    lng1, lat1, lng2, lat2 = map(math.radians, (*a, *b))
    h = (
        math.sin((lat2 - lat1) / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin((lng2 - lng1) / 2) ** 2
    )
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(min(1.0, h)))


def point_in_polygon(point: tuple[float, float], polygon: list[tuple[float, float]]) -> bool:
    """射线法；多边形首尾闭合与否均可。"""
    x, y = point
    ring = polygon[:-1] if polygon[0] == polygon[-1] else polygon
    inside = False
    j = len(ring) - 1
    for i in range(len(ring)):
        xi, yi = ring[i]
        xj, yj = ring[j]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi) + xi:
            inside = not inside
        j = i
    return inside


def ring_area_m2(polygon: list[tuple[float, float]]) -> float:
    """球面多边形平面投影面积（鞋带公式）。"""
    ring = polygon[:-1] if polygon[0] == polygon[-1] else polygon
    if len(ring) < 3:
        return 0.0
    lat0 = sum(p[1] for p in ring) / len(ring)
    s = 0.0
    for (x1, y1), (x2, y2) in zip(ring, ring[1:] + ring[:1]):
        mx1, my1 = to_meters(x1, y1, lat0)
        mx2, my2 = to_meters(x2, y2, lat0)
        s += mx1 * my2 - mx2 * my1
    return abs(s) / 2.0


def boundary_closed(polygon: list[tuple[float, float]]) -> bool:
    return len(polygon) >= 4 and polygon[0] == polygon[-1]


def raster_covered_cells(
    points: list[tuple[float, float]], cell_m: float
) -> set[tuple[int, int]]:
    """将轨迹点投影到米制平面，按 cell_m 栅格去重，返回覆盖栅格集合。

    稀疏航迹下以点栅格代表喷洒覆盖（无人机飞行间隔通常小于栅格边长）。
    """
    if not points:
        return set()
    lat0 = sum(p[1] for p in points) / len(points)
    cells: set[tuple[int, int]] = set()
    for lng, lat in points:
        x, y = to_meters(lng, lat, lat0)
        cells.add((math.floor(x / cell_m), math.floor(y / cell_m)))
    return cells


def segment_cells(
    a: tuple[float, float], b: tuple[float, float], cell_m: float, lat0: float
) -> set[tuple[int, int]]:
    """线段 a-b 穿过的栅格（Bresenham 超采样），保证连续片段不漏格。"""
    ax, ay = to_meters(a[0], a[1], lat0)
    bx, by = to_meters(b[0], b[1], lat0)
    dist = math.hypot(bx - ax, by - ay)
    steps = max(1, math.ceil(dist / (cell_m / 2)))
    cells = set()
    for k in range(steps + 1):
        t = k / steps
        x = ax + (bx - ax) * t
        y = ay + (by - ay) * t
        cells.add((math.floor(x / cell_m), math.floor(y / cell_m)))
    return cells


def point_buffer_polygon(
    center: tuple[float, float], radius_m: float, segments: int = 32
) -> list[tuple[float, float]]:
    lng, lat = center
    half_lat = math.degrees(radius_m / EARTH_RADIUS_M)
    half_lng = half_lat / max(math.cos(math.radians(lat)), 1e-9)
    ring = [
        (lng + half_lng * math.cos(2 * math.pi * i / segments),
         lat + half_lat * math.sin(2 * math.pi * i / segments))
        for i in range(segments)
    ]
    ring.append(ring[0])
    return ring
