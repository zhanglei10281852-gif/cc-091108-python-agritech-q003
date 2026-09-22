"""田块申报：把田块边界、敏感区域、禁飞区和药剂标签登记进作业台。

申报只做校验与登记，不产生任何作业许可；处方必须引用已登记的田块。
登记后的资料不可原地修改，变更只能以新申报版本追加。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from . import geo
from .models import ChemicalLabel, FieldParcel, NoFlyZone, SensitiveArea


class DeclarationError(ValueError):
    """申报资料不满足登记条件。"""


@dataclass(frozen=True)
class Declaration:
    declaration_id: str
    declarant: str
    declared_at: str
    parcel: FieldParcel
    sensitive_areas: tuple[SensitiveArea, ...]
    no_fly_zones: tuple[NoFlyZone, ...]
    labels: tuple[ChemicalLabel, ...]
    area_mu: float


@dataclass
class DeclarationRegistry:
    """按田块保存申报记录；同一田块再次申报会追加为新版本。"""

    _by_field: dict[str, list[Declaration]] = field(default_factory=dict)
    _seq: int = 0

    def submit(
        self,
        declarant: str,
        declared_at: str,
        parcel: FieldParcel,
        sensitive_areas: list[SensitiveArea],
        no_fly_zones: list[NoFlyZone],
        labels: list[ChemicalLabel],
    ) -> Declaration:
        self._validate_parcel(parcel)
        for zone in no_fly_zones:
            self._validate_closed(zone.polygon, f"禁飞区 {zone.zone_id}")
        for area in sensitive_areas:
            if area.buffer_m <= 0:
                raise DeclarationError(f"敏感区域 {area.area_id} 缓冲距离必须为正")
        for label in labels:
            if label.min_ml_per_mu >= label.max_ml_per_mu:
                raise DeclarationError(f"标签 {label.chemical_id} 剂量上下限颠倒")
            if label.crop != parcel.crop:
                raise DeclarationError(
                    f"标签 {label.chemical_id} 适用作物 {label.crop} 与田块作物 {parcel.crop} 不一致"
                )

        self._seq += 1
        declaration = Declaration(
            declaration_id=f"DECL-{self._seq:04d}",
            declarant=declarant,
            declared_at=declared_at,
            parcel=parcel,
            sensitive_areas=tuple(sensitive_areas),
            no_fly_zones=tuple(no_fly_zones),
            labels=tuple(labels),
            area_mu=round(geo.m2_to_mu(geo.polygon_area_m2(list(parcel.boundary))), 6),
        )
        self._by_field.setdefault(parcel.field_id, []).append(declaration)
        return declaration

    def current(self, field_id: str) -> Declaration:
        versions = self._by_field.get(field_id)
        if not versions:
            raise DeclarationError(f"田块 {field_id} 尚未申报")
        return versions[-1]

    @staticmethod
    def _validate_closed(polygon: tuple[tuple[float, float], ...], what: str) -> None:
        if len(polygon) < 4:
            raise DeclarationError(f"{what} 边界点数不足")
        if polygon[0] != polygon[-1]:
            raise DeclarationError(f"{what} 边界未闭合")
        for lon, lat in polygon:
            if not (-180.0 <= lon <= 180.0 and -90.0 <= lat <= 90.0):
                raise DeclarationError(f"{what} 坐标超出 WGS84 范围: ({lon}, {lat})")

    @classmethod
    def _validate_parcel(cls, parcel: FieldParcel) -> None:
        cls._validate_closed(parcel.boundary, f"田块 {parcel.field_id}")
        if geo.polygon_area_m2(list(parcel.boundary)) <= 0:
            raise DeclarationError(f"田块 {parcel.field_id} 边界面积为零")
