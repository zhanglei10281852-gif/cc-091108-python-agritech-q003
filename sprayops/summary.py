"""可校验摘要。

摘要内容（content）是确定性的：只取决于任务当前修订号的归档数据，
对内容做规范化 JSON 序列化后取 SHA-256 作为校验值。
- 重复同步同一批数据，修订号不变，摘要内容不变，校验值不变；
- 迟到数据产生新修订号，新摘要记录 supersedes 与逐条变更对照，
  旧摘要原文保留、可随时核验，不会被悄悄改写；
- verify_summary 重新计算校验值，任何篡改都会失败。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict

from .evidence import EvidenceArchive
from .models import Anomaly, AnomalyKind, Prescription, TaskPlan

SCHEMA = "sprayops.summary/1"


def _canonical(payload) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest_content(content: dict) -> str:
    return hashlib.sha256(_canonical(content).encode("utf-8")).hexdigest()


def _anomaly_payload(a: Anomaly) -> dict:
    return {
        "kind": a.kind.value,
        "point_ids": list(a.point_ids),
        "detail": a.detail,
        "first_seen_revision": a.first_seen_revision,
    }


def build_summary(
    archive: EvidenceArchive,
    task: TaskPlan,
    prescription: Prescription,
    exported_at: str,
) -> dict:
    """按任务当前修订号生成摘要。相同修订号重复导出，内容完全一致。"""
    revision = archive.revision(task.task_id)
    metrics = archive.metrics(task.task_id)
    anomalies = archive.anomalies(task.task_id)
    batches = archive.batches(task.task_id)
    swaps = archive.tank_swaps(task.task_id)
    tanks = archive.tank_loads(task.task_id)

    by_kind: dict[str, list[dict]] = {kind.value: [] for kind in AnomalyKind}
    for a in anomalies:
        by_kind[a.kind.value].append(_anomaly_payload(a))

    # 处方符合性核对：装药批次与处方冻结批次是否一致
    findings = []
    for tank_id, load in sorted(tanks.items()):
        if load.batch_id != prescription.batch_id:
            findings.append(
                {
                    "finding": "tank_batch_mismatch",
                    "tank_id": tank_id,
                    "loaded_batch": load.batch_id,
                    "prescribed_batch": prescription.batch_id,
                    "note": f"药箱 {tank_id} 装载批次 {load.batch_id} 与处方批次 {prescription.batch_id} 不符",
                }
            )

    content = {
        "schema": SCHEMA,
        "task_id": task.task_id,
        "revision": revision,
        "parent_task_id": task.parent_task_id,
        "covers_segment_ids": list(task.covers_segment_ids),
        "prescription": {
            "prescription_id": prescription.prescription_id,
            "crop": prescription.crop,
            "chemical_id": prescription.chemical_id,
            "batch_id": prescription.batch_id,
            "dose_ml_per_mu": prescription.dose_ml_per_mu,
            "safety_interval_days": prescription.safety_interval_days,
            "reentry_hours": prescription.reentry_hours,
            "reviewer": prescription.reviewer,
            "signed_at": prescription.signed_at,
            "signature": prescription.signature,
        },
        "inputs": {
            "batches": [
                {"batch_id": bid, "sha256": m["sha256"], "points": len(m["points"])}
                for bid, m in sorted(batches.items())
            ],
            "points_digest": archive.points_digest(task.task_id),
        },
        "segments": metrics["segments"],
        "unattributed": metrics["unattributed"],
        "tank_volume_ml": metrics["tank_volume_ml"],
        "totals": metrics["totals"],
        "tank_swaps": [asdict(s) for s in swaps],
        "return_decisions": [asdict(d) for d in task.return_decisions],
        "segment_states": {
            sid: {
                "status": st.status.value,
                "started_at": st.started_at,
                "ended_at": st.ended_at,
                "void_reason": st.void_reason,
            }
            for sid, st in sorted(task.segments.items())
        },
        "anomalies": by_kind,
        "prescription_findings": findings,
        "supersedes": None,
        "changes_vs_previous": [],
    }

    revisions = archive.revisions(task.task_id)
    if len(revisions) >= 2:
        prev = archive.snapshot(task.task_id, revisions[-2])
        cur = archive.snapshot(task.task_id, revisions[-1])
        content["supersedes"] = prev["points_digest"]
        content["changes_vs_previous"] = _diff_snapshots(prev, cur)

    return {
        "content": content,
        "sha256": digest_content(content),
        "exported_at": exported_at,
    }


def _diff_snapshots(prev: dict, cur: dict) -> list[str]:
    """两个修订号之间的逐条变更对照。"""
    changes: list[str] = []
    pm, cm = prev["metrics"], cur["metrics"]
    for sid in sorted(set(pm["segments"]) | set(cm["segments"])):
        p = pm["segments"].get(sid, {})
        c = cm["segments"].get(sid, {})
        if p.get("coverage_mu") != c.get("coverage_mu") or p.get("volume_ml") != c.get("volume_ml"):
            changes.append(
                f"航段 {sid}：覆盖 {p.get('coverage_mu')} → {c.get('coverage_mu')} 亩，"
                f"用药 {p.get('volume_ml')} → {c.get('volume_ml')} 毫升"
            )
    pt, ct = pm["totals"], cm["totals"]
    if pt["coverage_mu"] != ct["coverage_mu"] or pt["volume_ml"] != ct["volume_ml"]:
        changes.append(
            f"合计：覆盖 {pt['coverage_mu']} → {ct['coverage_mu']} 亩，"
            f"用药 {pt['volume_ml']} → {ct['volume_ml']} 毫升"
        )
    if pt["track_points"] != ct["track_points"]:
        changes.append(f"定位点 {pt['track_points']} → {ct['track_points']} 个")
    new_anomalies = sorted(set(cur["anomaly_keys"]) - set(prev["anomaly_keys"]))
    for key in new_anomalies:
        changes.append(f"新增异常项：{key}")
    if not changes:
        changes.append("指标与异常项无变化（仅定位点集合或批次构成变化）")
    return changes


def verify_summary(summary: dict) -> bool:
    """核验摘要校验值；内容与校验值不符即被篡改。"""
    content = summary.get("content")
    if not isinstance(content, dict) or content.get("schema") != SCHEMA:
        return False
    return digest_content(content) == summary.get("sha256")


def export_summary(summary: dict, path) -> None:
    """导出摘要 JSON 文件。"""
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(summary, fh, ensure_ascii=False, indent=2, sort_keys=True)
        fh.write("\n")


def load_summary(path) -> dict:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)
