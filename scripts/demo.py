"""端到端演示：承接两份离线航迹样例，跑通报批、审签、执行、归档与投诉追溯。

用法：
    PYTHONPATH=. python3 -m scripts.demo
"""

from __future__ import annotations

import json
from pathlib import Path

from crop_spray import Ledger, SprayService

ROOT = Path(__file__).resolve().parents[1]
REF = ROOT / "reference"
OUT = ROOT / "runtime"
TZ = "+08:00"


def load(name: str):
    return json.loads((REF / name).read_text(encoding="utf-8"))


def bootstrap(svc: SprayService) -> None:
    d = load("domain.json")
    svc.register_field(d["field"])
    svc.register_sensitive_area({
        "id": d["sensitive_areas"][0]["id"], "field_id": d["field"]["id"],
        "kind": d["sensitive_areas"][0]["kind"],
        "buffer_m": d["sensitive_areas"][0]["buffer_m"],
        "point": d["sensitive_areas"][0]["point"]})
    svc.register_label(d["chemical_label"])
    svc.declare_batch("BATCH-2601", "CHEM-A17", 200_000)
    svc.declare_batch("BATCH-2602", "CHEM-A17", 200_000)
    svc.declare_batch("BATCH-777", "CHEM-A17", 100_000)  # 未列入处方的批次


def prescription(svc: SprayService, rx_id: str, decl_id: str, legs: list[dict]) -> None:
    svc.submit_prescription({
        "rx_id": rx_id,
        "declaration_id": decl_id,
        "chemical_id": "CHEM-A17",
        "batches": ["BATCH-2601", "BATCH-2602"],
        "weather_limits": {"max_wind_m_s": 3.0, "max_rainfall_mm_h": 0.0},
        "legs": legs,
    }, actor="飞防组织-李某")
    svc.approve_prescription(rx_id, reviewer="县植保站-王审签",
                             at=f"2026-09-10T16:30:00{TZ}")


def main() -> None:
    OUT.mkdir(exist_ok=True)
    ledger_path = OUT / "spray_ledger.jsonl"
    if ledger_path.exists():
        ledger_path.unlink()

    svc = SprayService(Ledger(ledger_path))
    bootstrap(svc)

    # ---- 田块申报 ----
    svc.declare_field_planting("DECL-01", "FIELD-204", "水稻",
                               applicant="种植户-赵某", season="2026 晚稻")

    sample_a = load("track_sample_a.json")
    sample_b = load("track_sample_b.json")

    # ================= 任务 A：正常航次，含未起飞航段自动失效 =================
    prescription(svc, "RX-A", "DECL-01",
                 [{"leg_id": "A-L1", "planned_area_mu": 6.5},
                  {"leg_id": "A-L2", "planned_area_mu": 2.0}])
    svc.create_task("TASK-A", "RX-A", at=f"2026-09-11T07:30:00{TZ}")
    svc.load_tank("TANK-01", "BATCH-2601", "TASK-A", f"2026-09-11T07:40:00{TZ}")
    svc.load_tank("TANK-02", "BATCH-2602", "TASK-A", f"2026-09-11T08:16:00{TZ}")

    svc.observe_weather("TASK-A", f"2026-09-11T07:55:00{TZ}", 2.1, 0.0)
    svc.takeoff("TASK-A", "A-L1", f"2026-09-11T08:00:00{TZ}")

    # A-L1 第一批离线点（received 09:35 批量补传，文件按接收顺序，时间乱序）
    bulk_a = [p for p in sample_a if p["received_at"].startswith("2026-09-11T09:35")]
    r = svc.sync_track_points("TASK-A", "A-L1", "SYNC-A-1", bulk_a)
    print("[A] 首批同步:", len(r["accepted"]), "点")

    # 风速越限：A-L1 已在空中（记录返航），A-L2 未起飞自动失效
    breach = svc.observe_weather("TASK-A", f"2026-09-11T08:28:00{TZ}", 4.6, 0.0)
    print("[A] 气象越限效果:", breach)
    svc.record_recall_decision("TASK-A", "A-L1", f"2026-09-11T08:29:00{TZ}",
                               decision="RETURN", decided_by="飞手-陈某",
                               note="瞬时风速 4.6 m/s 超过处方阈值，架次返航")
    svc.land("TASK-A", "A-L1", f"2026-09-11T08:33:00{TZ}")

    # 迟到 3 点（封存前到达）
    late_a = [p for p in sample_a if p["point_id"] not in {x["point_id"] for x in bulk_a}]
    r = svc.sync_track_points("TASK-A", "A-L1", "SYNC-A-2", late_a)
    print("[A] 迟到点同步:", r["accepted"])

    # 重复同步：不得多算
    r_dup = svc.sync_track_points("TASK-A", "A-L1", "SYNC-A-DUP", bulk_a[:5])
    print("[A] 重复同步拒绝:", len(r_dup["rejected"]), "点")

    seal_a = svc.seal_conclusion("TASK-A", "县植保站-王审签",
                                 f"2026-09-11T15:00:00{TZ}")
    print("[A] 封存:", seal_a["seal_hash"][:16], "...")

    # ================= 任务 B：问题航次（投诉对象）=================
    prescription(svc, "RX-B", "DECL-01",
                 [{"leg_id": "B-L1", "planned_area_mu": 6.5}])
    svc.create_task("TASK-B", "RX-B", at=f"2026-09-12T07:20:00{TZ}")
    svc.load_tank("TANK-03", "BATCH-2601", "TASK-B", f"2026-09-12T07:25:00{TZ}")
    svc.load_tank("TANK-09", "BATCH-777", "TASK-B", f"2026-09-12T08:05:00{TZ}")
    svc.observe_weather("TASK-B", f"2026-09-12T07:35:00{TZ}", 1.8, 0.0)
    svc.takeoff("TASK-B", "B-L1", f"2026-09-12T07:38:00{TZ}")
    svc.record_recall_decision("TASK-B", "B-L1", f"2026-09-12T08:06:00{TZ}",
                               decision="RETURN", decided_by="飞手-陈某",
                               note="发现航线接近蜂场，操纵返航")
    svc.land("TASK-B", "B-L1", f"2026-09-12T08:15:00{TZ}")

    bulk_b = [p for p in sample_b if p["received_at"].startswith("2026-09-12T08:20")]
    r = svc.sync_track_points("TASK-B", "B-L1", "SYNC-B-1", bulk_b)
    print("[B] 首批同步:", len(r["accepted"]), "点（含乱序与异常）")

    seal_b = svc.seal_conclusion("TASK-B", "县植保站-王审签",
                                 f"2026-09-12T10:00:00{TZ}")

    # 封存后又有 2 点同步 -> 补遗，原结论哈希不变
    late_b = [p for p in sample_b if p["point_id"] not in {x["point_id"] for x in bulk_b}]
    r = svc.sync_track_points("TASK-B", "B-L1", "SYNC-B-2", late_b)
    print("[B] 封存后到达:", r)
    add = svc.record_addendum("TASK-B", "县植保站-王审签",
                              f"2026-09-12T18:30:00{TZ}")
    print("[B] 补遗:", add["addendum_hash"][:16], "... 原封存:", seal_b["seal_hash"][:16])

    # 再次补遗（无新点）应被拒绝
    try:
        svc.record_addendum("TASK-B", "县植保站-王审签", f"2026-09-12T19:00:00{TZ}")
    except Exception as exc:
        print("[B] 重复补遗被拒绝:", exc)

    # 投诉与追溯
    svc.file_complaint("COMP-9", "TASK-B", f"2026-09-13T09:00:00{TZ}",
                       complainant="蜂场主-周某",
                       allegation="蜂群大量死亡，怀疑 9 月 12 日喷洒侵入蜂场缓冲带")

    # ================= 补飞：独立任务、独立证据链 =================
    prescription(svc, "RX-B2", "DECL-01",
                 [{"leg_id": "C-L1", "planned_area_mu": 3.0}])
    svc.create_task("TASK-C", "RX-B2", at=f"2026-09-14T07:00:00{TZ}",
                    parent_task_id="TASK-B")
    svc.load_tank("TANK-11", "BATCH-2602", "TASK-C", f"2026-09-14T07:10:00{TZ}")
    print("[C] 补飞任务 TASK-C 已建立，parent=TASK-B")

    trace = svc.complaint_trace("COMP-9")
    summary_b = svc.export_summary("TASK-B")
    verify = svc.verify()

    OUT.mkdir(exist_ok=True)
    (OUT / "complaint_trace.json").write_text(
        json.dumps(trace, ensure_ascii=False, indent=2), encoding="utf-8")
    (OUT / "summary_TASK-A.json").write_text(
        json.dumps(svc.export_summary("TASK-A"), ensure_ascii=False, indent=2),
        encoding="utf-8")
    (OUT / "summary_TASK-B.json").write_text(
        json.dumps(summary_b, ensure_ascii=False, indent=2), encoding="utf-8")
    (OUT / "verification.json").write_text(
        json.dumps(verify, ensure_ascii=False, indent=2), encoding="utf-8")

    print("\n===== 投诉追溯摘要 =====")
    print("审核人:", trace["prescription"]["reviewer"],
          "| 审签时间:", trace["prescription"]["approved_at"])
    for leg in trace["legs"]:
        f = leg["findings"]
        print(f"航段 {leg['leg_id']} 状态={leg['status']} "
              f"覆盖={leg['covered_mu']} 亩 "
              f"越界={len(f['BOUNDARY_EXCURSION'])} "
              f"缓冲侵入={len(f['BUFFER_INTRUSION'])} "
              f"时间倒退={len(f['TIME_REVERSAL'])} "
              f"剂量异常={len(f['DOSE_ANOMALY'])} "
              f"批次不符={len(f['BATCH_MISMATCH'])}")
        for z in f["BUFFER_INTRUSION"]:
            print("   ↳ 侵入", z["sensitive_id"], "最近", z["nearest_m"],
                  "m，深入缓冲", z["encroachment_m"], "m")
        for s in leg["nearest_sensitive"]:
            print("   ↳ 邻近", s["sensitive_id"], s["kind"],
                  "最近", s["nearest_m"], "m，缓冲余量", s["clearance_m"], "m")
    print("换箱节点:")
    for n in trace["tank_swaps"]:
        print("  ", n["at"], n["from_tank"], "->", n["to_tank"],
              "批次:", n["batch_after"])
    print("各批次实际用量(ml):", trace["usage_ml_by_batch"])
    print("补飞任务:", trace["child_reflight_tasks"],
          "| 补遗数:", len(trace["addenda"]))
    print("封存哈希:", trace["seal"]["seal_hash"][:24], "...")
    print("账本校验:", verify)


if __name__ == "__main__":
    main()
