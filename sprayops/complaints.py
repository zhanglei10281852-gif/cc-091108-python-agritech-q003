"""投诉追溯。

从一次投诉出发，把涉及的任务（含补飞，证据链分开标注）串成一份报告：
审核人、处方冻结内容、每个航段的覆盖面积、换箱节点、
与邻近敏感区域的距离、分项异常和各次摘要的校验值。
"""

from __future__ import annotations

from dataclasses import dataclass

from .evidence import EvidenceArchive
from .execution import ExecutionEngine
from .models import parse_ts
from .prescription import PrescriptionService


@dataclass(frozen=True)
class Complaint:
    complaint_id: str
    field_id: str
    filed_by: str
    filed_at: str
    window_start: str          # 投诉涉及的时间段（含）
    window_end: str            # 投诉涉及的时间段（含）
    sensitive_area_id: str | None  # 投诉指向的敏感区域，可为空
    description: str = ""


def _task_window_overlap(archive: EvidenceArchive, task_id: str, start, end) -> bool:
    line = archive.timeline(task_id)
    if not line:
        return False
    first = parse_ts(line[0].device_at)
    last = parse_ts(line[-1].device_at)
    return first <= end and last >= start


def trace_complaint(
    complaint: Complaint,
    execution: ExecutionEngine,
    archive: EvidenceArchive,
    prescriptions: PrescriptionService,
) -> dict:
    """生成投诉追溯报告（JSON 可序列化）。"""
    start = parse_ts(complaint.window_start)
    end = parse_ts(complaint.window_end)

    chains: list[dict] = []
    for task in execution.tasks_for_field(complaint.field_id):
        if not _task_window_overlap(archive, task.task_id, start, end):
            continue
        rx = prescriptions.get(task.prescription_id)
        metrics = archive.metrics(task.task_id)
        anomalies = archive.anomalies(task.task_id)
        swaps = archive.tank_swaps(task.task_id)

        sensitive_distance = None
        if complaint.sensitive_area_id:
            sensitive_distance = metrics["totals"]["min_distance_to_sensitive_m"].get(
                complaint.sensitive_area_id
            )

        chains.append(
            {
                "task_id": task.task_id,
                "chain_role": "补飞任务" if task.parent_task_id else "原任务",
                "parent_task_id": task.parent_task_id,
                "covers_segment_ids": list(task.covers_segment_ids),
                "prescription": {
                    "prescription_id": rx.prescription_id,
                    "reviewer": rx.reviewer,
                    "signed_at": rx.signed_at,
                    "crop": rx.crop,
                    "chemical_id": rx.chemical_id,
                    "batch_id": rx.batch_id,
                    "safety_interval_days": rx.safety_interval_days,
                    "reentry_hours": rx.reentry_hours,
                    "signature": rx.signature,
                },
                "segments": metrics["segments"],
                "tank_swaps": [
                    {
                        "at": s.at,
                        "position": list(s.position),
                        "from_tank": s.from_tank,
                        "to_tank": s.to_tank,
                        "from_batch": s.from_batch,
                        "to_batch": s.to_batch,
                    }
                    for s in swaps
                ],
                "return_decisions": [
                    {
                        "segment_id": d.segment_id,
                        "decided_at": d.decided_at,
                        "reason": d.reason,
                    }
                    for d in task.return_decisions
                ],
                "totals": metrics["totals"],
                "distance_to_complaint_area_m": sensitive_distance,
                "anomalies": [
                    {
                        "kind": a.kind.value,
                        "point_ids": list(a.point_ids),
                        "detail": a.detail,
                        "first_seen_revision": a.first_seen_revision,
                    }
                    for a in anomalies
                ],
                "evidence": {
                    "revision": archive.revision(task.task_id),
                    "points_digest": archive.points_digest(task.task_id),
                    "batches": sorted(archive.batches(task.task_id)),
                },
            }
        )

    chains.sort(key=lambda c: (c["parent_task_id"] is not None, c["task_id"]))
    return {
        "complaint_id": complaint.complaint_id,
        "field_id": complaint.field_id,
        "filed_by": complaint.filed_by,
        "filed_at": complaint.filed_at,
        "window": [complaint.window_start, complaint.window_end],
        "sensitive_area_id": complaint.sensitive_area_id,
        "description": complaint.description,
        "chains": chains,
    }


def render_trace_text(report: dict) -> str:
    """把追溯报告渲染为植保员可直接阅读的文本。"""
    lines = [
        f"投诉 {report['complaint_id']}（{report['filed_by']} 于 {report['filed_at']} 提出）",
        f"田块 {report['field_id']}，涉及时段 {report['window'][0]} ~ {report['window'][1]}",
        f"说明：{report['description']}",
        "",
    ]
    for chain in report["chains"]:
        rx = chain["prescription"]
        lines.append(
            f"== {chain['chain_role']} {chain['task_id']}"
            + (
                f"（承接 {chain['parent_task_id']} 的失效航段 {', '.join(chain['covers_segment_ids'])}）"
                if chain["parent_task_id"]
                else ""
            )
        )
        lines.append(
            f"  处方 {rx['prescription_id']}：审核人 {rx['reviewer']}，"
            f"审签于 {rx['signed_at']}；作物 {rx['crop']}，药剂 {rx['chemical_id']}，"
            f"批次 {rx['batch_id']}，安全间隔期 {rx['safety_interval_days']} 天，"
            f"再进入间隔 {rx['reentry_hours']} 小时"
        )
        for sid, seg in chain["segments"].items():
            lines.append(
                f"  航段 {sid}[{seg['status']}]：覆盖 {seg['coverage_mu']} 亩，"
                f"用药 {seg['volume_ml']} 毫升，实测剂量 {seg['dose_ml_per_mu']} 毫升/亩"
            )
        for swap in chain["tank_swaps"]:
            lines.append(
                f"  换箱节点 {swap['at']}：{swap['from_tank']}（批次 {swap['from_batch']}）"
                f" → {swap['to_tank']}（批次 {swap['to_batch']}），位置 {swap['position']}"
            )
        for dec in chain["return_decisions"]:
            lines.append(f"  返航决定 {dec['decided_at']}：航段 {dec['segment_id']}，{dec['reason']}")
        if chain["distance_to_complaint_area_m"] is not None:
            lines.append(
                f"  距投诉敏感区 {report['sensitive_area_id']} 最近 "
                f"{chain['distance_to_complaint_area_m']} 米"
            )
        for a in chain["anomalies"]:
            lines.append(f"  异常[{a['kind']}] 点 {', '.join(a['point_ids'])}：{a['detail']}")
        ev = chain["evidence"]
        lines.append(
            f"  证据修订号 r{ev['revision']}，定位点摘要 {ev['points_digest'][:16]}…，"
            f"批次 {', '.join(ev['batches'])}"
        )
        lines.append("")
    return "\n".join(lines)
