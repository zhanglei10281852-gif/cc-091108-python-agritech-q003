"""处方审签。

处方从起草到审签只有一次状态跃迁：draft -> signed。
审签时把适用作物、药剂批次、目标剂量、安全间隔期整体冻结并生成签名摘要，
之后任何字段都不可再改；作业任务只能引用已审签的处方。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace

from .declaration import DeclarationRegistry
from .models import Prescription, PrescriptionState


class PrescriptionError(ValueError):
    pass


def _canonical(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def prescription_content(rx: Prescription) -> dict:
    """参与审签签名的冻结内容。"""
    return {
        "prescription_id": rx.prescription_id,
        "field_id": rx.field_id,
        "crop": rx.crop,
        "chemical_id": rx.chemical_id,
        "batch_id": rx.batch_id,
        "dose_ml_per_mu": rx.dose_ml_per_mu,
        "safety_interval_days": rx.safety_interval_days,
        "reentry_hours": rx.reentry_hours,
        "created_by": rx.created_by,
        "created_at": rx.created_at,
    }


def sign_content(content: dict, reviewer: str, signed_at: str) -> str:
    payload = {"content": content, "reviewer": reviewer, "signed_at": signed_at}
    return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()


class PrescriptionService:
    def __init__(self, declarations: DeclarationRegistry):
        self._declarations = declarations
        self._by_id: dict[str, Prescription] = {}

    def create_draft(
        self,
        prescription_id: str,
        field_id: str,
        chemical_id: str,
        batch_id: str,
        dose_ml_per_mu: float,
        safety_interval_days: int,
        created_by: str,
        created_at: str,
    ) -> Prescription:
        if prescription_id in self._by_id:
            raise PrescriptionError(f"处方 {prescription_id} 已存在")
        declaration = self._declarations.current(field_id)
        label = next(
            (lb for lb in declaration.labels if lb.chemical_id == chemical_id), None
        )
        if label is None:
            raise PrescriptionError(f"田块 {field_id} 的申报资料中没有药剂 {chemical_id} 的标签")
        if not (label.min_ml_per_mu <= dose_ml_per_mu <= label.max_ml_per_mu):
            raise PrescriptionError(
                f"目标剂量 {dose_ml_per_mu} 毫升/亩超出标签范围 "
                f"[{label.min_ml_per_mu}, {label.max_ml_per_mu}]"
            )
        if safety_interval_days <= 0:
            raise PrescriptionError("安全间隔期必须为正整数天")
        rx = Prescription(
            prescription_id=prescription_id,
            field_id=field_id,
            crop=declaration.parcel.crop,
            chemical_id=chemical_id,
            batch_id=batch_id,
            dose_ml_per_mu=dose_ml_per_mu,
            safety_interval_days=safety_interval_days,
            reentry_hours=label.reentry_hours,
            created_by=created_by,
            created_at=created_at,
        )
        self._by_id[prescription_id] = rx
        return rx

    def review_and_sign(
        self,
        prescription_id: str,
        reviewer: str,
        signed_at: str,
        approve: bool,
        comment: str = "",
    ) -> Prescription:
        """审签。批准后处方冻结；驳回则处方作废（保持草稿态并记录意见）。"""
        rx = self._require(prescription_id)
        if rx.state is PrescriptionState.SIGNED:
            raise PrescriptionError(f"处方 {prescription_id} 已审签，不能重复审签")
        if not approve:
            self._by_id[prescription_id] = replace(
                rx, reviewer=reviewer, review_comment=comment or "审签驳回"
            )
            return self._by_id[prescription_id]
        content = prescription_content(rx)
        signed = replace(
            rx,
            state=PrescriptionState.SIGNED,
            reviewer=reviewer,
            signed_at=signed_at,
            review_comment=comment,
            signature=sign_content(content, reviewer, signed_at),
        )
        self._by_id[prescription_id] = signed
        return signed

    def get(self, prescription_id: str) -> Prescription:
        return self._require(prescription_id)

    def require_signed(self, prescription_id: str) -> Prescription:
        rx = self._require(prescription_id)
        if rx.state is not PrescriptionState.SIGNED:
            raise PrescriptionError(f"处方 {prescription_id} 尚未审签，不能用于作业")
        return rx

    @staticmethod
    def verify_signature(rx: Prescription) -> bool:
        """核验审签签名与冻结内容是否一致。"""
        if rx.state is not PrescriptionState.SIGNED or not rx.signature:
            return False
        expect = sign_content(prescription_content(rx), rx.reviewer or "", rx.signed_at or "")
        return expect == rx.signature

    def _require(self, prescription_id: str) -> Prescription:
        rx = self._by_id.get(prescription_id)
        if rx is None:
            raise PrescriptionError(f"处方 {prescription_id} 不存在")
        return rx
