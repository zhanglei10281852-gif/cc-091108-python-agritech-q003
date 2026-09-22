"""作业台核心数据模型。

约定：
- 坐标一律 WGS84 (lon, lat)，多边形首尾闭合；
- 时间一律带时区的 ISO8601，内部使用 aware datetime；
- 药箱编号代表一次装载事实，换箱后即使药剂相同也必须使用新编号；
- 处方一经审签即冻结，适用作物、药剂批次、安全间隔期不可再改。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


def parse_ts(value: str) -> datetime:
    """解析 ISO8601 时间戳，要求带时区。"""
    ts = datetime.fromisoformat(value)
    if ts.tzinfo is None:
        raise ValueError(f"时间戳缺少时区: {value!r}")
    return ts


# ---------------------------------------------------------------- 申报资料

@dataclass(frozen=True)
class SensitiveArea:
    """敏感区域（点状），缓冲距离来自其自身类别。"""

    area_id: str
    kind: str
    buffer_m: float
    point: tuple[float, float]


@dataclass(frozen=True)
class NoFlyZone:
    zone_id: str
    kind: str
    polygon: tuple[tuple[float, float], ...]


@dataclass(frozen=True)
class ChemicalLabel:
    """药剂标签：剂量范围（毫升/亩）与再进入间隔。"""

    chemical_id: str
    crop: str
    min_ml_per_mu: float
    max_ml_per_mu: float
    reentry_hours: float


@dataclass(frozen=True)
class FieldParcel:
    field_id: str
    crop: str
    boundary: tuple[tuple[float, float], ...]


# ---------------------------------------------------------------- 处方

class PrescriptionState(str, Enum):
    DRAFT = "draft"
    SIGNED = "signed"


@dataclass(frozen=True)
class Prescription:
    """处方。审签后冻结：作物、药剂批次、安全间隔期固定。"""

    prescription_id: str
    field_id: str
    crop: str
    chemical_id: str
    batch_id: str               # 药剂批次，审签后固定
    dose_ml_per_mu: float       # 目标剂量，须落在标签范围内
    safety_interval_days: int   # 安全间隔期（采收间隔），审签后固定
    reentry_hours: float        # 来自标签的再进入间隔
    created_by: str
    created_at: str
    state: PrescriptionState = PrescriptionState.DRAFT
    reviewer: str | None = None     # 审核人
    signed_at: str | None = None
    review_comment: str | None = None
    signature: str | None = None    # 审签内容摘要，用于核验


# ---------------------------------------------------------------- 任务与航段

class SegmentStatus(str, Enum):
    PENDING = "pending"        # 未起飞
    ACTIVE = "active"          # 在飞
    COMPLETED = "completed"    # 完成
    VOIDED = "voided"          # 气象越限，未起飞航段自动失效
    RETURNED = "returned"      # 已起飞后返航


@dataclass(frozen=True)
class SegmentPlan:
    segment_id: str
    lane: tuple[tuple[float, float], ...]  # 计划航线折线
    swath_m: float


@dataclass
class SegmentState:
    plan: SegmentPlan
    status: SegmentStatus = SegmentStatus.PENDING
    started_at: str | None = None
    ended_at: str | None = None
    void_reason: str | None = None


@dataclass(frozen=True)
class OperatingLimits:
    max_wind_m_s: float
    max_rain_mm_per_h: float


@dataclass(frozen=True)
class WeatherEvent:
    observed_at: str
    wind_m_s: float
    rain_mm_per_h: float


@dataclass(frozen=True)
class ReturnDecision:
    """已起飞航段在气象越限时记录的返航决定。"""

    segment_id: str
    decided_at: str
    reason: str
    wind_m_s: float
    rain_mm_per_h: float


@dataclass
class TaskPlan:
    task_id: str
    prescription_id: str
    field_id: str
    segments: dict[str, SegmentState]
    limits: OperatingLimits
    created_at: str
    parent_task_id: str | None = None      # 补飞任务指向原任务
    covers_segment_ids: tuple[str, ...] = ()  # 补飞承接的原任务航段
    return_decisions: list[ReturnDecision] = field(default_factory=list)


# ---------------------------------------------------------------- 航迹与药箱

@dataclass(frozen=True)
class TrackPoint:
    """离线航迹点。device_at 为设备时间，received_at 为接收时间。"""

    point_id: str
    device_at: str
    received_at: str
    position: tuple[float, float]
    tank_id: str
    spraying: bool
    flow_ml_per_min: float


@dataclass(frozen=True)
class TankLoad:
    """一次装药事实：药箱编号绑定到具体药剂批次。"""

    tank_id: str
    task_id: str
    chemical_id: str
    batch_id: str
    loaded_at: str
    loaded_by: str


@dataclass(frozen=True)
class TankSwap:
    """换箱节点：航迹时间线上药箱编号发生变化的位置。"""

    at: str
    position: tuple[float, float]
    from_tank: str
    to_tank: str
    from_batch: str
    to_batch: str


# ---------------------------------------------------------------- 异常

class AnomalyKind(str, Enum):
    OUT_OF_BOUNDS = "out_of_bounds"        # 越界片段（飞出田块）
    NO_FLY_INTRUSION = "no_fly_intrusion"  # 进入禁飞区
    BUFFER_INTRUSION = "buffer_intrusion"  # 进入敏感区缓冲带
    TIME_REGRESSION = "time_regression"    # 时间倒退
    ABNORMAL_DOSE = "abnormal_dose"        # 异常剂量
    POINT_CONFLICT = "point_conflict"      # 后到数据与原数据冲突（未采纳）


@dataclass(frozen=True)
class Anomaly:
    kind: AnomalyKind
    point_ids: tuple[str, ...]
    detail: dict
    first_seen_revision: int
