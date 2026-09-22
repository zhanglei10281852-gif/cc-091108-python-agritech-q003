"""WGS84 平面近似计算。

所有公开函数输入经纬度（度），输出距离（米）或面积（平方米）。
在小范围田块尺度下使用等距圆柱投影做局部平面化，误差可忽略。
"""

from __future__ import annotations

import math
from typing import Sequence

EARTH_RADIUS_M = 6_371_008.8
MU_M2 = 2000.0 / 3.0  # 1 亩 = 2000/3 平方米

LonLat = Sequence[float]  # (lon, lat)


def haversine_m(a: LonLat, b: LonLat) -> float:
    """两点球面距离，米。"""
    lon1, lat1 = math.radians(a[0]), math.radians(a[1])
    lon2, lat2 = math.radians(b[0]), math.radians(b[1])
    dlon = lon2 - lon1
    dlat = lat2 - lat1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(h))


def _to_xy(p: LonLat, lat0: float) -> tuple[float, float]:
    """以 lat0（弧度）为参考纬度的局部平面坐标，米。"""
    return (
        EARTH_RADIUS_M * math.radians(p[0]) * math.cos(lat0),
        EARTH_RADIUS_M * math.radians(p[1]),
    )


def point_in_polygon(p: LonLat, polygon: Sequence[LonLat]) -> bool:
    """射线法判断点是否在闭合多边形内（边界上视为在内）。"""
    x, y = p[0], p[1]
    inside = False
    n = len(polygon)
    for i in range(n):
        x1, y1 = polygon[i][0], polygon[i][1]
        x2, y2 = polygon[(i + 1) % n][0], polygon[(i + 1) % n][1]
        # 边界上的点
        cross = (x - x1) * (y2 - y1) - (y - y1) * (x2 - x1)
        if abs(cross) < 1e-12 and min(x1, x2) - 1e-12 <= x <= max(x1, x2) + 1e-12 and \
                min(y1, y2) - 1e-12 <= y <= max(y1, y2) + 1e-12:
            return True
        if (y1 > y) != (y2 > y):
            xin = (x2 - x1) * (y - y1) / (y2 - y1) + x1
            if x <= xin:
                inside = not inside
    return inside


def _point_segment_m(
    p: tuple[float, float], a: tuple[float, float], b: tuple[float, float]
) -> float:
    ax, ay = a
    bx, by = b
    px, py = p
    dx, dy = bx - ax, by - ay
    if dx == 0 and dy == 0:
        return math.hypot(px - ax, py - ay)
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def distance_to_polygon_m(p: LonLat, polygon: Sequence[LonLat]) -> float:
    """点到多边形边界的最短距离；点在多边形内时返回 0。"""
    if point_in_polygon(p, polygon):
        return 0.0
    lat0 = math.radians(p[1])
    xy = _to_xy(p, lat0)
    best = math.inf
    n = len(polygon)
    for i in range(n):
        a = _to_xy(polygon[i], lat0)
        b = _to_xy(polygon[(i + 1) % n], lat0)
        best = min(best, _point_segment_m(xy, a, b))
    return best


def polygon_area_m2(polygon: Sequence[LonLat]) -> float:
    """闭合多边形面积（鞋带公式，局部平面化）。"""
    if len(polygon) < 3:
        return 0.0
    lat0 = math.radians(sum(pt[1] for pt in polygon) / len(polygon))
    pts = [_to_xy(pt, lat0) for pt in polygon]
    area = 0.0
    for i in range(len(pts) - 1):
        area += pts[i][0] * pts[i + 1][1] - pts[i + 1][0] * pts[i][1]
    return abs(area) / 2.0


def path_length_m(points: Sequence[LonLat]) -> float:
    """折线长度，米。"""
    return sum(haversine_m(points[i], points[i + 1]) for i in range(len(points) - 1))


def m2_to_mu(area_m2: float) -> float:
    return area_m2 / MU_M2
