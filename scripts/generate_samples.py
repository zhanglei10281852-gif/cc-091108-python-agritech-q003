"""生成两份离线航迹样例。

样例 A（TASK-A / L1）：正常作业，TANK-01 中途换 TANK-02，
设备离线后批量补传，另有 3 个点更晚才同步（封存前到达）。

样例 B（TASK-B / L1）：问题航次——越界扑向蜂场缓冲带、
设备时间倒退、单点剂量超标签、换用未列入处方的批次 BATCH-777；
另有 2 个点在封存后才同步，供补遗演示。
"""

from __future__ import annotations

import json
from pathlib import Path

from crop_spray import geo
from crop_spray.models import MU_M2

OUT = Path(__file__).resolve().parents[1] / "reference"
TZ = "+08:00"


def ts(base: str, seconds: int) -> str:
    from datetime import datetime, timedelta

    return (datetime.fromisoformat(base) + timedelta(seconds=seconds)).isoformat()


def make_rows() -> list[tuple[float, float]]:
    pts: list[tuple[float, float]] = []
    lats = [30.2017 + k * 0.0003 for k in range(9)]
    lngs = [120.1015 + k * 0.0003 for k in range(11)]
    for r, lat in enumerate(lats):
        row = lngs if r % 2 == 0 else list(reversed(lngs))
        for lng in row:
            pts.append((round(lng, 6), round(lat, 6)))
    return pts


def sample_a() -> list[dict]:
    coords = make_rows()
    n = len(coords)
    base = f"2026-09-11T08:00:10{TZ}"
    bulk = f"2026-09-11T09:35:00{TZ}"  # 设备降落联网后批量补传
    late_cut = n - 3
    points = []
    for i, (lng, lat) in enumerate(coords):
        tank = "TANK-01" if i < n // 2 else "TANK-02"
        points.append({
            "point_id": f"A-{i + 1:03d}",
            "device_at": ts(base, i * 4),
            "received_at": ts(bulk, i) if i < late_cut else ts(f"2026-09-11T14:10:00{TZ}", i),
            "position": [lng, lat],
            "tank_id": tank,
            "dose_ml_per_mu": 22.0,
        })
    # 按覆盖面积均摊用药，使航段整体剂量恰好 22 ml/亩（标签 18–25）
    cells = geo.raster_covered_cells(coords, 2.0)
    for a, b in zip(coords, coords[1:]):
        lat0 = sum(p[1] for p in coords) / len(coords)
        cells |= geo.segment_cells(a, b, 2.0, lat0)
    covered_mu = len(cells) * 4.0 / MU_M2
    per_point = round(22.0 * covered_mu / n, 2)
    for p in points:
        p["spray_ml"] = per_point
    return points


def sample_b() -> list[dict]:
    # 场内一排点，随后东出边界扑向蜂场（120.106,30.203；缓冲 120 m）再折返
    seq = [
        (120.1020, 30.2030, "TANK-03", 21.0),
        (120.1026, 30.2030, "TANK-03", 21.0),
        (120.1032, 30.2030, "TANK-03", 21.0),
        (120.1038, 30.2030, "TANK-03", 21.0),
        (120.1044, 30.2030, "TANK-03", 21.0),
        (120.1052, 30.2030, "TANK-03", 21.0),   # 越出东边界
        (120.1065, 30.2030, "TANK-03", 40.0),   # 距蜂场约 48 m，侵入缓冲；剂量 40 超标
        (120.1052, 30.2030, "TANK-09", 21.0),   # 返航越界段；换药箱（未列入处方批次）
        (120.1044, 30.2030, "TANK-09", 21.0),
        (120.1038, 30.2026, "TANK-09", 21.0),
        (120.1032, 30.2026, "TANK-09", 21.0),
        # 以下两点封存后才同步（设备持续离线）
        (120.1026, 30.2026, "TANK-09", 21.0),
        (120.1020, 30.2026, "TANK-09", 21.0),
    ]
    base = f"2026-09-12T07:40:00{TZ}"
    bulk = f"2026-09-12T08:20:00{TZ}"
    late = f"2026-09-12T18:05:00{TZ}"
    points = []
    for i, (lng, lat, tank, dose) in enumerate(seq):
        points.append({
            "point_id": f"B-{i + 1:03d}",
            "device_at": ts(base, i * 5),
            "received_at": ts(bulk, i) if i < 11 else ts(late, i),
            "position": [lng, lat],
            "tank_id": tank,
            "dose_ml_per_mu": dose,
            "spray_ml": 3.0 if dose <= 25 else 8.0,
        })
    # 制造设备时间倒退：文件摄入顺序中 B-006 出现在 B-007 之后
    points[5], points[6] = points[6], points[5]
    return points


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for name, fn in (("track_sample_a.json", sample_a),
                     ("track_sample_b.json", sample_b)):
        data = fn()
        (OUT / name).write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"wrote {name}: {len(data)} points")


if __name__ == "__main__":
    main()
