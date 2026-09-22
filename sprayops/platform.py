"""平台装配与参考资料装载。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .declaration import Declaration, DeclarationRegistry
from .evidence import EvidenceArchive
from .execution import ExecutionEngine
from .models import (
    ChemicalLabel,
    FieldParcel,
    NoFlyZone,
    OperatingLimits,
    SensitiveArea,
    TrackPoint,
)
from .prescription import PrescriptionService


@dataclass(frozen=True)
class ReferenceData:
    """reference/domain.json 的结构化视图。"""

    parcel: FieldParcel
    sensitive_areas: tuple[SensitiveArea, ...]
    no_fly_zones: tuple[NoFlyZone, ...]
    label: ChemicalLabel
    limits: OperatingLimits
    track_points: tuple[TrackPoint, ...]          # 离线航迹样例一（原任务）
    reflight_track_points: tuple[TrackPoint, ...]  # 离线航迹样例二（补飞）
    late_track_points: tuple[TrackPoint, ...]      # 迟到补传点（含冲突点）


def _track_point(raw: dict) -> TrackPoint:
    return TrackPoint(
        point_id=raw["point_id"],
        device_at=raw["device_at"],
        received_at=raw["received_at"],
        position=(raw["position"][0], raw["position"][1]),
        tank_id=raw["tank_id"],
        spraying=bool(raw.get("spraying", False)),
        flow_ml_per_min=float(raw.get("flow_ml_per_min", 0.0)),
    )


def load_reference(path: str | Path) -> ReferenceData:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    field = data["field"]
    label = data["chemical_label"]
    limits = data.get("operating_limits", {})
    return ReferenceData(
        parcel=FieldParcel(
            field_id=field["id"],
            crop=field["crop"],
            boundary=tuple((p[0], p[1]) for p in field["boundary"]),
        ),
        sensitive_areas=tuple(
            SensitiveArea(
                area_id=a["id"],
                kind=a["kind"],
                buffer_m=float(a["buffer_m"]),
                point=(a["point"][0], a["point"][1]),
            )
            for a in data.get("sensitive_areas", [])
        ),
        no_fly_zones=tuple(
            NoFlyZone(
                zone_id=z["id"],
                kind=z["kind"],
                polygon=tuple((p[0], p[1]) for p in z["polygon"]),
            )
            for z in data.get("no_fly_zones", [])
        ),
        label=ChemicalLabel(
            chemical_id=label["chemical_id"],
            crop=label["crop"],
            min_ml_per_mu=float(label["min_ml_per_mu"]),
            max_ml_per_mu=float(label["max_ml_per_mu"]),
            reentry_hours=float(label["reentry_hours"]),
        ),
        limits=OperatingLimits(
            max_wind_m_s=float(limits.get("max_wind_m_s", 6.0)),
            max_rain_mm_per_h=float(limits.get("max_rain_mm_per_h", 0.2)),
        ),
        track_points=tuple(_track_point(p) for p in data.get("track_points", [])),
        reflight_track_points=tuple(
            _track_point(p) for p in data.get("reflight_track_points", [])
        ),
        late_track_points=tuple(_track_point(p) for p in data.get("late_track_points", [])),
    )


class SprayPlatform:
    """作业台门面：申报、处方、执行、证据四个子系统。"""

    def __init__(self) -> None:
        self.declarations = DeclarationRegistry()
        self.prescriptions = PrescriptionService(self.declarations)
        self.execution = ExecutionEngine(self.prescriptions)
        self.archive = EvidenceArchive()

    def declare(
        self,
        declarant: str,
        declared_at: str,
        ref: ReferenceData,
    ) -> Declaration:
        """田块申报：登记田块边界、敏感区域、禁飞区与药剂标签。"""
        return self.declarations.submit(
            declarant=declarant,
            declared_at=declared_at,
            parcel=ref.parcel,
            sensitive_areas=list(ref.sensitive_areas),
            no_fly_zones=list(ref.no_fly_zones),
            labels=[ref.label],
        )
