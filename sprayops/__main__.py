"""端到端演示：从蜂场药害投诉场景走一遍完整流程。

    python -m sprayops

流程：田块申报 → 处方审签 → 航段执行（气象越限）→ 离线航迹同步 →
重复同步（幂等）→ 迟到补传（冲突留痕）→ 补飞 → 投诉追溯 → 摘要校验。
摘要与追溯报告写入 out/ 目录。
"""

from __future__ import annotations

import json
from pathlib import Path

from .complaints import Complaint, render_trace_text, trace_complaint
from .models import SegmentPlan, TankLoad, WeatherEvent
from .platform import SprayPlatform, load_reference
from .summary import build_summary, export_summary, verify_summary

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "out"


def main() -> None:
    OUT.mkdir(exist_ok=True)
    ref = load_reference(ROOT / "reference" / "domain.json")
    platform = SprayPlatform()

    print("=" * 72)
    print("1. 田块申报")
    decl = platform.declare("陈立（合作社）", "2026-09-09T10:00:00+08:00", ref)
    print(
        f"   申报号 {decl.declaration_id}：田块 {decl.parcel.field_id}（{decl.parcel.crop}，"
        f"{decl.area_mu} 亩），敏感区域 {len(decl.sensitive_areas)} 处，"
        f"禁飞区 {len(decl.no_fly_zones)} 处"
    )

    print("=" * 72)
    print("2. 处方审签（审签后作物/批次/安全间隔期冻结）")
    platform.prescriptions.create_draft(
        prescription_id="RX-204-A17",
        field_id="FIELD-204",
        chemical_id="CHEM-A17",
        batch_id="BATCH-A17-01",
        dose_ml_per_mu=20.0,
        safety_interval_days=14,
        created_by="张航（飞防队）",
        created_at="2026-09-10T15:00:00+08:00",
    )
    rx = platform.prescriptions.review_and_sign(
        "RX-204-A17", reviewer="李岚（县植保站）", signed_at="2026-09-10T18:30:00+08:00",
        approve=True, comment="剂量在标签范围内，同意作业",
    )
    print(
        f"   处方 {rx.prescription_id} 已审签：审核人 {rx.reviewer}，批次 {rx.batch_id}，"
        f"安全间隔期 {rx.safety_interval_days} 天，签名 {rx.signature[:16]}…"
    )
    print(f"   签名校验：{'通过' if platform.prescriptions.verify_signature(rx) else '失败'}")

    print("=" * 72)
    print("3. 航段执行（风速越限：未起飞航段自动失效，已起飞航段记录返航决定）")
    segments = [
        SegmentPlan("SEG-1", ((120.102, 30.202), (120.102, 30.2032)), 5.0),
        SegmentPlan("SEG-2", ((120.1035, 30.2045), (120.1035, 30.2025)), 5.0),
        SegmentPlan("SEG-3", ((120.1045, 30.202), (120.1045, 30.204)), 5.0),
    ]
    task_a = platform.execution.create_task(
        "TASK-0907-A", "RX-204-A17", segments, ref.limits, "2026-09-11T07:30:00+08:00"
    )
    platform.archive.register_task(task_a, decl, ref.label)
    platform.archive.register_tank_load(
        TankLoad("TANK-01", "TASK-0907-A", "CHEM-A17", "BATCH-A17-01",
                 "2026-09-11T08:30:00+08:00", "张航")
    )
    platform.archive.register_tank_load(
        TankLoad("TANK-02", "TASK-0907-A", "CHEM-A17", "BATCH-A17-02",
                 "2026-09-11T09:04:00+08:00", "张航")
    )
    platform.execution.start_segment("TASK-0907-A", "SEG-1", "2026-09-11T09:00:08+08:00")
    platform.execution.complete_segment("TASK-0907-A", "SEG-1", "2026-09-11T09:00:36+08:00")
    platform.execution.start_segment("TASK-0907-A", "SEG-2", "2026-09-11T09:02:05+08:00")
    actions = platform.execution.apply_weather(
        "TASK-0907-A",
        WeatherEvent("2026-09-11T09:07:10+08:00", wind_m_s=8.4, rain_mm_per_h=0.0),
    )
    for a in actions:
        print(f"   {a}")

    print("=" * 72)
    print("4. 离线航迹同步（设备离线期间的定位点稍后同步）")
    report = platform.archive.ingest_batch("TASK-0907-A", "SYNC-A1", list(ref.track_points))
    print(
        f"   批次 SYNC-A1：接受 {len(report.accepted)} 点，修订号 r{report.revision}"
    )
    for a in platform.archive.anomalies("TASK-0907-A"):
        print(f"   异常[{a.kind.value}] 点 {', '.join(a.point_ids)}")
    summary_a1 = build_summary(platform.archive, task_a, rx, "2026-09-11T11:00:00+08:00")
    export_summary(summary_a1, OUT / "summary_TASK-0907-A_r1.json")
    print(f"   摘要 r1 校验值 {summary_a1['sha256'][:24]}… 已导出")

    print("=" * 72)
    print("5. 重复同步同一批次（不得多算用药量）")
    before = platform.archive.metrics("TASK-0907-A")["totals"]
    again = platform.archive.ingest_batch("TASK-0907-A", "SYNC-A1", list(ref.track_points))
    after = platform.archive.metrics("TASK-0907-A")["totals"]
    print(
        f"   批次已归档={again.batch_already_archived}，新接受 {len(again.accepted)} 点；"
        f"用药量 {before['volume_ml']} → {after['volume_ml']} 毫升（不变）"
    )

    print("=" * 72)
    print("6. 迟到补传（后到数据不得悄悄改写原结论）")
    late = platform.archive.ingest_batch("TASK-0907-A", "SYNC-A2", list(ref.late_track_points))
    print(
        f"   批次 SYNC-A2：接受 {len(late.accepted)} 点（{', '.join(late.accepted)}），"
        f"冲突 {len(late.conflicts)} 起，修订号 r{late.revision}"
    )
    for c in late.conflicts:
        kept = c.detail["kept"]["position"]
        rejected = c.detail["rejected"]["position"]
        print(f"   冲突点 {c.point_ids[0]}：保留先到位置 {kept}，后到位置 {rejected} 未采纳")
    summary_a2 = build_summary(platform.archive, task_a, rx, "2026-09-11T19:00:00+08:00")
    export_summary(summary_a2, OUT / "summary_TASK-0907-A_r2.json")
    print("   摘要 r2 相对 r1 的变更对照：")
    for change in summary_a2["content"]["changes_vs_previous"]:
        print(f"     - {change}")
    print(f"   r1 摘要仍可独立核验：{'通过' if verify_summary(summary_a1) else '失败'}")

    print("=" * 72)
    print("7. 补飞（与原任务证据链分开）")
    task_b = platform.execution.create_reflight(
        "TASK-0907-B",
        "TASK-0907-A",
        [SegmentPlan("SEG-3B", ((120.1045, 30.202), (120.1045, 30.204)), 5.0)],
        "2026-09-12T07:00:00+08:00",
    )
    platform.archive.register_task(task_b, decl, ref.label)
    platform.archive.register_tank_load(
        TankLoad("TANK-03", "TASK-0907-B", "CHEM-A17", "BATCH-A17-01",
                 "2026-09-12T07:30:00+08:00", "张航")
    )
    platform.execution.start_segment("TASK-0907-B", "SEG-3B", "2026-09-12T08:00:02+08:00")
    platform.execution.complete_segment("TASK-0907-B", "SEG-3B", "2026-09-12T08:01:00+08:00")
    platform.archive.ingest_batch("TASK-0907-B", "SYNC-B1", list(ref.reflight_track_points))
    summary_b = build_summary(platform.archive, task_b, rx, "2026-09-12T09:00:00+08:00")
    export_summary(summary_b, OUT / "summary_TASK-0907-B_r1.json")
    print(
        f"   补飞任务 {task_b.task_id} 承接失效航段 {', '.join(task_b.covers_segment_ids)}，"
        f"独立证据链 r{platform.archive.revision('TASK-0907-B')}，"
        f"摘要校验值 {summary_b['sha256'][:24]}…"
    )

    print("=" * 72)
    print("8. 投诉追溯（蜂场药害）")
    complaint = Complaint(
        complaint_id="CMP-2026-0911",
        field_id="FIELD-204",
        filed_by="蜂场负责人 周某",
        filed_at="2026-09-11T20:00:00+08:00",
        window_start="2026-09-11T06:00:00+08:00",
        window_end="2026-09-12T12:00:00+08:00",
        sensitive_area_id="APIARY-8",
        description="蜂群大量死亡，怀疑邻近田块飞防飘移药害",
    )
    report_doc = trace_complaint(
        complaint, platform.execution, platform.archive, platform.prescriptions
    )
    print(render_trace_text(report_doc))
    with open(OUT / "complaint_CMP-2026-0911.json", "w", encoding="utf-8") as fh:
        json.dump(report_doc, fh, ensure_ascii=False, indent=2, sort_keys=True)
        fh.write("\n")

    print("=" * 72)
    print("9. 摘要校验")
    for name, summary in [
        ("TASK-0907-A r1", summary_a1),
        ("TASK-0907-A r2", summary_a2),
        ("TASK-0907-B r1", summary_b),
    ]:
        print(f"   {name}：{'通过' if verify_summary(summary) else '失败'}")
    print(f"   导出文件位于 {OUT}")


if __name__ == "__main__":
    main()
