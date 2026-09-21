"""航迹分析：覆盖面积、越界片段、缓冲带侵入、时间倒退、剂量异常。

所有分析都是纯函数：输入点序列与参考数据，输出结构化结果，
不修改任何状态。同一点集（按 point_id 去重）重复分析必得同一结果。
"""

from __future__ import annotations

from dataclasses import dataclass

from . import geo
from .models import COVERAGE_CELL_M, MU_M2, FindingKind


@dataclass
class AnalysisResult:
    leg_id: str
    point_ids: list[str]
    covered_m2: float
    covered_mu: float
    excursions: list[dict]          # 越界片段
    intrusions: list[dict]          # 缓冲带侵入片段
    reversals: list[dict]           # 时间倒退
    dose_anomalies: list[dict]      # 异常剂量
    batch_mismatches: list[dict]    # 批次不符
    unknown_tanks: list[dict]       # 无装载记录的药箱
    nearest_sensitive: list[dict]   # 每个敏感区最近距离
    usage_ml_by_batch: dict[str, float]
    used_tanks: list[str]

    def to_dict(self) -> dict:
        return {
            "leg_id": self.leg_id,
            "point_ids": self.point_ids,
            "covered_m2": round(self.covered_m2, 2),
            "covered_mu": round(self.covered_mu, 3),
            "findings": {
                FindingKind.BOUNDARY_EXCURSION.value: self.excursions,
                FindingKind.BUFFER_INTRUSION.value: self.intrusions,
                FindingKind.TIME_REVERSAL.value: self.reversals,
                FindingKind.DOSE_ANOMALY.value: self.dose_anomalies,
                FindingKind.BATCH_MISMATCH.value: self.batch_mismatches,
                FindingKind.UNKNOWN_TANK.value: self.unknown_tanks,
            },
            "nearest_sensitive": self.nearest_sensitive,
            "usage_ml_by_batch": {k: round(v, 2) for k, v in self.usage_ml_by_batch.items()},
            "used_tanks": self.used_tanks,
        }


def analyze_leg(
    leg_id: str,
    points: list[dict],
    field_boundary: list[tuple[float, float]],
    sensitive_areas: list[dict],
    label: dict,
    tank_to_batch: dict[str, str],
    approved_batches: list[str],
    cell_m: float = COVERAGE_CELL_M,
) -> AnalysisResult:
    """points 已按 point_id 去重；元素含 device_at/position/tank_id/spray_ml/dose_ml_per_mu。

    几何分析按设备时间排序；时间倒退按点的提交/接收顺序判定。
    """
    by_device = sorted(points, key=lambda p: p["device_at"])
    lat0 = sum(p["position"][1] for p in by_device) / len(by_device) if by_device else 0.0

    covered: set[tuple[int, int]] = set()
    excursions: list[dict] = []
    intrusions: list[dict] = []
    reversals: list[dict] = []
    dose_anomalies: list[dict] = []
    batch_mismatches: list[dict] = []
    unknown_tanks: list[dict] = []
    usage: dict[str, float] = {}
    used_tanks: list[str] = []

    # 时间倒退按「同一同步批次内」的摄入顺序判定（设备单次上报的记录应按
    # 时间递增）；跨批次迟到补传属于允许的离线同步，不在此列。
    prev_by_batch: dict[str, str] = {}
    for p in points:
        batch = p.get("sync_batch", "")
        prev = prev_by_batch.get(batch)
        if prev is not None and p["device_at"] < prev:
            reversals.append({
                "point_id": p["point_id"], "device_at": p["device_at"],
                "previous_device_at": prev, "sync_batch": batch,
                "detail": "同一同步批次内设备时间早于其前一记录（时钟倒退），单独呈现",
            })
        prev_by_batch[batch] = p["device_at"]

    for i, p in enumerate(by_device):
        pos = p["position"]

        # 药箱/批次核对
        tank = p["tank_id"]
        if tank not in used_tanks:
            used_tanks.append(tank)
        batch = tank_to_batch.get(tank)
        if batch is None:
            unknown_tanks.append({"point_id": p["point_id"], "tank_id": tank,
                                  "device_at": p["device_at"]})
        elif approved_batches and batch not in approved_batches:
            batch_mismatches.append({"point_id": p["point_id"], "tank_id": tank,
                                     "batch_id": batch, "device_at": p["device_at"]})
        # 用药量按「实际装载批次」累计：即使批次不在处方内也如实记录，
        # 配合 batch_mismatches 回答「中途换箱后到底用了哪批药」。
        if p.get("spray_ml") is not None:
            key = batch if batch is not None else f"UNKNOWN::{tank}"
            usage[key] = usage.get(key, 0.0) + p["spray_ml"]

        # 逐点剂量异常
        d = p.get("dose_ml_per_mu")
        if d is not None and (d < label["min_ml_per_mu"] or d > label["max_ml_per_mu"]):
            dose_anomalies.append({
                "point_id": p["point_id"], "device_at": p["device_at"],
                "dose_ml_per_mu": d, "allowed": [label["min_ml_per_mu"], label["max_ml_per_mu"]],
                "kind": "BELOW_LABEL" if d < label["min_ml_per_mu"] else "ABOVE_LABEL",
            })

        # 覆盖栅格：点栅格 + 与前一点的连线栅格
        covered |= geo.raster_covered_cells([pos], cell_m)
        if i > 0:
            covered |= geo.segment_cells(by_device[i - 1]["position"], pos, cell_m, lat0)

    # 越界片段与缓冲带侵入：逐线段
    buffers = [
        {"area": s, "poly": geo.point_buffer_polygon(tuple(s["point"]), s["buffer_m"])}
        for s in sensitive_areas
    ]
    for a, b in zip(by_device, by_device[1:]):
        pa, pb = a["position"], b["position"]
        seg_len = geo.haversine_m(pa, pb)
        in_a = geo.point_in_polygon(pa, field_boundary)
        in_b = geo.point_in_polygon(pb, field_boundary)
        if not (in_a and in_b):
            # 估算越界长度：全段在外记全长，一端在外记一半
            outside_frac = 1.0 if not in_a and not in_b else 0.5
            excursions.append({
                "from_point": a["point_id"], "to_point": b["point_id"],
                "from_at": a["device_at"], "to_at": b["device_at"],
                "estimated_outside_m": round(seg_len * outside_frac, 2),
                "from_position": list(pa), "to_position": list(pb),
            })
        for buf in buffers:
            s = buf["area"]
            if (not geo.point_in_polygon(pa, buf["poly"])
                    and not geo.point_in_polygon(pb, buf["poly"])):
                continue
            # 侵入缓冲圆：用到圆心距离确认
            dmin = min(geo.haversine_m(pa, tuple(s["point"])),
                       geo.haversine_m(pb, tuple(s["point"])))
            if dmin < s["buffer_m"]:
                intrusions.append({
                    "sensitive_id": s["id"], "kind": s["kind"], "buffer_m": s["buffer_m"],
                    "from_point": a["point_id"], "to_point": b["point_id"],
                    "from_at": a["device_at"], "to_at": b["device_at"],
                    "nearest_m": round(dmin, 2),
                    "encroachment_m": round(s["buffer_m"] - dmin, 2),
                })

    # 最近敏感区距离（无论是否侵入都要可汇报）
    nearest = []
    for s in sensitive_areas:
        if not by_device:
            continue
        dmin = min(geo.haversine_m(p["position"], tuple(s["point"])) for p in by_device)
        nearest.append({
            "sensitive_id": s["id"], "kind": s["kind"], "buffer_m": s["buffer_m"],
            "nearest_m": round(dmin, 2), "clearance_m": round(dmin - s["buffer_m"], 2),
        })

    covered_m2 = len(covered) * cell_m * cell_m
    covered_mu = covered_m2 / MU_M2

    # 航段整体剂量：总量 / 覆盖亩数，落在标签外则整段异常
    total_ml = sum(usage.values())
    if covered_mu > 0 and total_ml > 0:
        overall = total_ml / covered_mu
        if overall < label["min_ml_per_mu"] or overall > label["max_ml_per_mu"]:
            dose_anomalies.append({
                "point_id": None, "device_at": None, "dose_ml_per_mu": round(overall, 2),
                "allowed": [label["min_ml_per_mu"], label["max_ml_per_mu"]],
                "kind": "LEG_TOTAL_" + ("BELOW_LABEL" if overall < label["min_ml_per_mu"]
                                        else "ABOVE_LABEL"),
                "total_spray_ml": round(total_ml, 2), "covered_mu": round(covered_mu, 3),
            })

    return AnalysisResult(
        leg_id=leg_id,
        point_ids=[p["point_id"] for p in by_device],
        covered_m2=covered_m2,
        covered_mu=covered_mu,
        excursions=excursions,
        intrusions=intrusions,
        reversals=reversals,
        dose_anomalies=dose_anomalies,
        batch_mismatches=batch_mismatches,
        unknown_tanks=unknown_tanks,
        nearest_sensitive=nearest,
        usage_ml_by_batch=usage,
        used_tanks=used_tanks,
    )
