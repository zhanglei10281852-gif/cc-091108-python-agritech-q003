"""作业台行为测试。

运行：python -m unittest discover -s tests
"""

from __future__ import annotations

import unittest
from dataclasses import replace
from pathlib import Path

from sprayops import geo
from sprayops.complaints import Complaint, trace_complaint
from sprayops.declaration import DeclarationError, DeclarationRegistry
from sprayops.evidence import EvidenceArchive
from sprayops.execution import ExecutionEngine, ExecutionError
from sprayops.models import (
    AnomalyKind,
    ChemicalLabel,
    FieldParcel,
    NoFlyZone,
    OperatingLimits,
    SegmentPlan,
    SegmentStatus,
    SensitiveArea,
    TankLoad,
    TrackPoint,
    WeatherEvent,
)
from sprayops.platform import SprayPlatform, load_reference
from sprayops.prescription import PrescriptionError, PrescriptionService
from sprayops.summary import build_summary, verify_summary

ROOT = Path(__file__).resolve().parents[1]

PARCEL = FieldParcel(
    field_id="F-1",
    crop="水稻",
    boundary=((120.0, 30.0), (120.001, 30.0), (120.001, 30.001), (120.0, 30.001), (120.0, 30.0)),
)
LABEL = ChemicalLabel("CHEM-X", "水稻", 18.0, 25.0, 24.0)
LIMITS = OperatingLimits(max_wind_m_s=6.0, max_rain_mm_per_h=0.2)


def make_point(pid, device_at, lon, lat, tank="T-1", spraying=True, flow=40.0):
    return TrackPoint(pid, device_at, device_at, (lon, lat), tank, spraying, flow)


class GeoTest(unittest.TestCase):
    def test_point_in_polygon(self):
        boundary = PARCEL.boundary
        self.assertTrue(geo.point_in_polygon((120.0005, 30.0005), boundary))
        self.assertFalse(geo.point_in_polygon((120.002, 30.0005), boundary))
        self.assertTrue(geo.point_in_polygon((120.0, 30.0005), boundary))  # 边界上视为在内

    def test_distance_to_polygon(self):
        inside = geo.distance_to_polygon_m((120.0005, 30.0005), PARCEL.boundary)
        self.assertEqual(inside, 0.0)
        outside = geo.distance_to_polygon_m((120.002, 30.0005), PARCEL.boundary)
        # 北纬 30° 处 0.001° 经度约 96 米
        self.assertGreater(outside, 90.0)
        self.assertLess(outside, 110.0)

    def test_haversine_and_area(self):
        # 赤道附近 0.001 纬度约 111 米
        d = geo.haversine_m((120.0, 0.0), (120.0, 0.001))
        self.assertAlmostEqual(d, 111.2, delta=1.0)
        area = geo.polygon_area_m2(list(PARCEL.boundary))
        self.assertGreater(area, 10_000.0)
        self.assertLess(area, 13_000.0)


class DeclarationTest(unittest.TestCase):
    def test_submit_and_current(self):
        reg = DeclarationRegistry()
        decl = reg.submit("甲", "2026-09-01T08:00:00+08:00", PARCEL, [], [], [LABEL])
        self.assertEqual(reg.current("F-1").declaration_id, decl.declaration_id)
        self.assertGreater(decl.area_mu, 0)

    def test_open_boundary_rejected(self):
        reg = DeclarationRegistry()
        bad = replace(PARCEL, boundary=PARCEL.boundary[:-1])
        with self.assertRaises(DeclarationError):
            reg.submit("甲", "2026-09-01T08:00:00+08:00", bad, [], [], [LABEL])

    def test_label_crop_mismatch_rejected(self):
        reg = DeclarationRegistry()
        bad_label = replace(LABEL, crop="小麦")
        with self.assertRaises(DeclarationError):
            reg.submit("甲", "2026-09-01T08:00:00+08:00", PARCEL, [], [], [bad_label])

    def test_unknown_field_has_no_declaration(self):
        reg = DeclarationRegistry()
        with self.assertRaises(DeclarationError):
            reg.current("F-404")


class PrescriptionTest(unittest.TestCase):
    def setUp(self):
        self.reg = DeclarationRegistry()
        self.reg.submit("甲", "2026-09-01T08:00:00+08:00", PARCEL, [], [], [LABEL])
        self.svc = PrescriptionService(self.reg)

    def _draft(self, dose=20.0):
        return self.svc.create_draft(
            "RX-1", "F-1", "CHEM-X", "BATCH-1", dose, 14, "乙", "2026-09-02T08:00:00+08:00"
        )

    def test_sign_freezes_content(self):
        self._draft()
        rx = self.svc.review_and_sign("RX-1", "丙", "2026-09-02T09:00:00+08:00", True)
        self.assertEqual(rx.reviewer, "丙")
        self.assertEqual(rx.batch_id, "BATCH-1")
        self.assertEqual(rx.safety_interval_days, 14)
        self.assertTrue(self.svc.verify_signature(rx))
        with self.assertRaises(PrescriptionError):
            self.svc.review_and_sign("RX-1", "丁", "2026-09-02T10:00:00+08:00", True)

    def test_signature_detects_tamper(self):
        self._draft()
        rx = self.svc.review_and_sign("RX-1", "丙", "2026-09-02T09:00:00+08:00", True)
        tampered = replace(rx, batch_id="BATCH-9")
        self.assertFalse(self.svc.verify_signature(tampered))

    def test_dose_outside_label_rejected(self):
        with self.assertRaises(PrescriptionError):
            self._draft(dose=30.0)

    def test_unsigned_prescription_cannot_dispatch(self):
        self._draft()
        engine = ExecutionEngine(self.svc)
        segments = [SegmentPlan("S-1", ((120.0, 30.0), (120.0, 30.001)), 5.0)]
        with self.assertRaises(PrescriptionError):
            engine.create_task("T-1", "RX-1", segments, LIMITS, "2026-09-03T08:00:00+08:00")


class ExecutionTest(unittest.TestCase):
    def setUp(self):
        reg = DeclarationRegistry()
        reg.submit("甲", "2026-09-01T08:00:00+08:00", PARCEL, [], [], [LABEL])
        self.svc = PrescriptionService(reg)
        self.svc.create_draft(
            "RX-1", "F-1", "CHEM-X", "BATCH-1", 20.0, 14, "乙", "2026-09-02T08:00:00+08:00"
        )
        self.svc.review_and_sign("RX-1", "丙", "2026-09-02T09:00:00+08:00", True)
        self.engine = ExecutionEngine(self.svc)
        self.task = self.engine.create_task(
            "T-1",
            "RX-1",
            [
                SegmentPlan("S-1", ((120.0, 30.0), (120.0, 30.0005)), 5.0),
                SegmentPlan("S-2", ((120.0005, 30.0), (120.0005, 30.0005)), 5.0),
                SegmentPlan("S-3", ((120.0008, 30.0), (120.0008, 30.0005)), 5.0),
            ],
            LIMITS,
            "2026-09-03T08:00:00+08:00",
        )

    def test_weather_voids_pending_and_records_return(self):
        self.engine.start_segment("T-1", "S-1", "2026-09-03T09:00:00+08:00")
        self.engine.complete_segment("T-1", "S-1", "2026-09-03T09:30:00+08:00")
        self.engine.start_segment("T-1", "S-2", "2026-09-03T09:35:00+08:00")
        actions = self.engine.apply_weather(
            "T-1", WeatherEvent("2026-09-03T09:40:00+08:00", 8.0, 0.0)
        )
        self.assertEqual(len(actions), 2)
        task = self.engine.require_task("T-1")
        self.assertIs(task.segments["S-1"].status, SegmentStatus.COMPLETED)
        self.assertIs(task.segments["S-2"].status, SegmentStatus.RETURNED)
        self.assertIs(task.segments["S-3"].status, SegmentStatus.VOIDED)
        self.assertEqual(len(task.return_decisions), 1)
        self.assertEqual(task.return_decisions[0].segment_id, "S-2")
        self.assertIn("风速", task.segments["S-3"].void_reason)

    def test_rain_also_breaches(self):
        self.engine.start_segment("T-1", "S-1", "2026-09-03T09:00:00+08:00")
        actions = self.engine.apply_weather(
            "T-1", WeatherEvent("2026-09-03T09:10:00+08:00", 1.0, 0.5)
        )
        self.assertTrue(any("降雨" in a for a in actions))

    def test_within_limits_no_action(self):
        actions = self.engine.apply_weather(
            "T-1", WeatherEvent("2026-09-03T09:10:00+08:00", 5.0, 0.1)
        )
        self.assertEqual(actions, [])

    def test_reflight_requires_voided_segments(self):
        with self.assertRaises(ExecutionError):
            self.engine.create_reflight("T-2", "T-1", [], "2026-09-04T08:00:00+08:00")
        self.engine.apply_weather("T-1", WeatherEvent("2026-09-03T09:40:00+08:00", 9.0, 0.0))
        reflight = self.engine.create_reflight(
            "T-2",
            "T-1",
            [SegmentPlan("S-3B", ((120.0008, 30.0), (120.0008, 30.0005)), 5.0)],
            "2026-09-04T08:00:00+08:00",
        )
        self.assertEqual(reflight.parent_task_id, "T-1")
        self.assertEqual(reflight.covers_segment_ids, ("S-1", "S-2", "S-3"))
        self.assertEqual(reflight.prescription_id, "RX-1")


class ScenarioMixin:
    """基于 reference/domain.json 搭好的完整场景。"""

    def build_platform(self):
        ref = load_reference(ROOT / "reference" / "domain.json")
        platform = SprayPlatform()
        decl = platform.declare("陈立", "2026-09-09T10:00:00+08:00", ref)
        platform.prescriptions.create_draft(
            "RX-204-A17", "FIELD-204", "CHEM-A17", "BATCH-A17-01", 20.0, 14,
            "张航", "2026-09-10T15:00:00+08:00",
        )
        rx = platform.prescriptions.review_and_sign(
            "RX-204-A17", "李岚", "2026-09-10T18:30:00+08:00", True
        )
        segments = [
            SegmentPlan("SEG-1", ((120.102, 30.202), (120.102, 30.2032)), 5.0),
            SegmentPlan("SEG-2", ((120.1035, 30.2045), (120.1035, 30.2025)), 5.0),
            SegmentPlan("SEG-3", ((120.1045, 30.202), (120.1045, 30.204)), 5.0),
        ]
        task = platform.execution.create_task(
            "TASK-0907-A", "RX-204-A17", segments, ref.limits, "2026-09-11T07:30:00+08:00"
        )
        platform.archive.register_task(task, decl, ref.label)
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
        platform.execution.apply_weather(
            "TASK-0907-A", WeatherEvent("2026-09-11T09:07:10+08:00", 8.4, 0.0)
        )
        return platform, ref, decl, rx, task


class EvidenceTest(ScenarioMixin, unittest.TestCase):
    def setUp(self):
        self.platform, self.ref, self.decl, self.rx, self.task = self.build_platform()
        self.archive: EvidenceArchive = self.platform.archive

    def anomalies_of(self, kind):
        return [
            a for a in self.archive.anomalies("TASK-0907-A") if a.kind is kind
        ]

    def test_ingest_detects_all_anomaly_kinds(self):
        self.archive.ingest_batch("TASK-0907-A", "SYNC-A1", list(self.ref.track_points))
        self.assertEqual(len(self.anomalies_of(AnomalyKind.TIME_REGRESSION)), 1)
        self.assertEqual(len(self.anomalies_of(AnomalyKind.OUT_OF_BOUNDS)), 1)
        self.assertEqual(len(self.anomalies_of(AnomalyKind.BUFFER_INTRUSION)), 1)
        self.assertEqual(len(self.anomalies_of(AnomalyKind.ABNORMAL_DOSE)), 3)

    def test_buffer_intrusion_quantified(self):
        self.archive.ingest_batch("TASK-0907-A", "SYNC-A1", list(self.ref.track_points))
        intrusion = self.anomalies_of(AnomalyKind.BUFFER_INTRUSION)[0]
        self.assertEqual(intrusion.detail["sensitive_area_id"], "APIARY-8")
        self.assertLess(intrusion.detail["min_distance_m"], 120.0)
        self.assertGreater(intrusion.detail["sprayed_volume_ml"], 0.0)

    def test_resync_same_batch_is_idempotent(self):
        self.archive.ingest_batch("TASK-0907-A", "SYNC-A1", list(self.ref.track_points))
        digest1 = self.archive.points_digest("TASK-0907-A")
        totals1 = self.archive.metrics("TASK-0907-A")["totals"]
        report = self.archive.ingest_batch("TASK-0907-A", "SYNC-A1", list(self.ref.track_points))
        self.assertTrue(report.batch_already_archived)
        self.assertEqual(len(report.accepted), 0)
        self.assertEqual(self.archive.points_digest("TASK-0907-A"), digest1)
        self.assertEqual(self.archive.metrics("TASK-0907-A")["totals"], totals1)
        self.assertEqual(self.archive.revision("TASK-0907-A"), 1)

    def test_overlapping_batch_does_not_double_count(self):
        self.archive.ingest_batch("TASK-0907-A", "SYNC-A1", list(self.ref.track_points))
        totals1 = self.archive.metrics("TASK-0907-A")["totals"]
        # 新批次包含已入库点 + 一个新点
        extra = make_point("P-99", "2026-09-11T09:07:30+08:00", 120.1029, 30.2019,
                           tank="TANK-02", spraying=False, flow=0.0)
        report = self.archive.ingest_batch(
            "TASK-0907-A", "SYNC-A1B", [self.ref.track_points[0], self.ref.track_points[5], extra]
        )
        self.assertEqual(len(report.accepted), 1)
        self.assertEqual(len(report.duplicates), 2)
        self.assertEqual(
            self.archive.metrics("TASK-0907-A")["totals"]["volume_ml"], totals1["volume_ml"]
        )

    def test_conflicting_point_keeps_original(self):
        self.archive.ingest_batch("TASK-0907-A", "SYNC-A1", list(self.ref.track_points))
        self.archive.ingest_batch("TASK-0907-A", "SYNC-A2", list(self.ref.late_track_points))
        conflicts = self.anomalies_of(AnomalyKind.POINT_CONFLICT)
        self.assertEqual(len(conflicts), 1)
        conflict = conflicts[0]
        self.assertEqual(conflict.point_ids, ("P-18",))
        self.assertEqual(conflict.detail["kept"]["position"], [120.1056, 30.203])
        # 原结论未被改写：越界与缓冲带侵入仍然存在
        self.assertEqual(len(self.anomalies_of(AnomalyKind.OUT_OF_BOUNDS)), 1)
        self.assertEqual(len(self.anomalies_of(AnomalyKind.BUFFER_INTRUSION)), 1)

    def test_late_batch_bumps_revision_with_snapshot(self):
        self.archive.ingest_batch("TASK-0907-A", "SYNC-A1", list(self.ref.track_points))
        self.assertEqual(self.archive.revision("TASK-0907-A"), 1)
        self.archive.ingest_batch("TASK-0907-A", "SYNC-A2", list(self.ref.late_track_points))
        self.assertEqual(self.archive.revision("TASK-0907-A"), 2)
        snap1 = self.archive.snapshot("TASK-0907-A", 1)
        snap2 = self.archive.snapshot("TASK-0907-A", 2)
        self.assertNotEqual(snap1["points_digest"], snap2["points_digest"])

    def test_tank_swap_nodes_with_batches(self):
        self.archive.ingest_batch("TASK-0907-A", "SYNC-A1", list(self.ref.track_points))
        swaps = self.archive.tank_swaps("TASK-0907-A")
        self.assertEqual(len(swaps), 1)
        self.assertEqual(swaps[0].from_tank, "TANK-01")
        self.assertEqual(swaps[0].to_tank, "TANK-02")
        self.assertEqual(swaps[0].from_batch, "BATCH-A17-01")
        self.assertEqual(swaps[0].to_batch, "BATCH-A17-02")

    def test_segment_coverage_and_sensitive_distance(self):
        self.archive.ingest_batch("TASK-0907-A", "SYNC-A1", list(self.ref.track_points))
        metrics = self.archive.metrics("TASK-0907-A")
        seg1 = metrics["segments"]["SEG-1"]
        self.assertAlmostEqual(seg1["coverage_mu"], 0.834, places=2)
        self.assertAlmostEqual(seg1["dose_ml_per_mu"], 19.79, places=1)
        self.assertEqual(metrics["segments"]["SEG-3"]["coverage_mu"], 0.0)
        dist = metrics["totals"]["min_distance_to_sensitive_m"]["APIARY-8"]
        self.assertLess(dist, 120.0)


class SummaryTest(ScenarioMixin, unittest.TestCase):
    def setUp(self):
        self.platform, self.ref, self.decl, self.rx, self.task = self.build_platform()
        self.platform.archive.ingest_batch("TASK-0907-A", "SYNC-A1", list(self.ref.track_points))

    def test_summary_stable_and_verifiable(self):
        s1 = build_summary(self.platform.archive, self.task, self.rx, "2026-09-11T11:00:00+08:00")
        s2 = build_summary(self.platform.archive, self.task, self.rx, "2026-09-11T11:00:00+08:00")
        self.assertEqual(s1["sha256"], s2["sha256"])
        self.assertTrue(verify_summary(s1))

    def test_tampered_summary_fails_verification(self):
        s1 = build_summary(self.platform.archive, self.task, self.rx, "2026-09-11T11:00:00+08:00")
        tampered = dict(s1)
        tampered["content"] = dict(
            s1["content"], totals=dict(s1["content"]["totals"], volume_ml=1.0)
        )
        self.assertFalse(verify_summary(tampered))

    def test_late_data_creates_new_revision_not_rewrite(self):
        s1 = build_summary(self.platform.archive, self.task, self.rx, "2026-09-11T11:00:00+08:00")
        self.platform.archive.ingest_batch(
            "TASK-0907-A", "SYNC-A2", list(self.ref.late_track_points)
        )
        s2 = build_summary(self.platform.archive, self.task, self.rx, "2026-09-11T19:00:00+08:00")
        self.assertNotEqual(s1["sha256"], s2["sha256"])
        self.assertEqual(s2["content"]["revision"], 2)
        self.assertEqual(s2["content"]["supersedes"], s1["content"]["inputs"]["points_digest"])
        self.assertTrue(s2["content"]["changes_vs_previous"])
        # 原摘要仍可独立核验
        self.assertTrue(verify_summary(s1))
        self.assertTrue(verify_summary(s2))

    def test_summary_flags_batch_mismatch(self):
        summary = build_summary(
            self.platform.archive, self.task, self.rx, "2026-09-11T11:00:00+08:00"
        )
        findings = summary["content"]["prescription_findings"]
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["tank_id"], "TANK-02")
        self.assertEqual(findings[0]["loaded_batch"], "BATCH-A17-02")


class ComplaintTraceTest(ScenarioMixin, unittest.TestCase):
    def setUp(self):
        self.platform, self.ref, self.decl, self.rx, self.task = self.build_platform()
        self.platform.archive.ingest_batch("TASK-0907-A", "SYNC-A1", list(self.ref.track_points))
        self.platform.archive.ingest_batch(
            "TASK-0907-A", "SYNC-A2", list(self.ref.late_track_points)
        )
        # 补飞
        task_b = self.platform.execution.create_reflight(
            "TASK-0907-B",
            "TASK-0907-A",
            [SegmentPlan("SEG-3B", ((120.1045, 30.202), (120.1045, 30.204)), 5.0)],
            "2026-09-12T07:00:00+08:00",
        )
        self.platform.archive.register_task(task_b, self.decl, self.ref.label)
        self.platform.archive.register_tank_load(
            TankLoad("TANK-03", "TASK-0907-B", "CHEM-A17", "BATCH-A17-01",
                     "2026-09-12T07:30:00+08:00", "张航")
        )
        self.platform.execution.start_segment("TASK-0907-B", "SEG-3B", "2026-09-12T08:00:02+08:00")
        self.platform.execution.complete_segment(
            "TASK-0907-B", "SEG-3B", "2026-09-12T08:01:00+08:00"
        )
        self.platform.archive.ingest_batch(
            "TASK-0907-B", "SYNC-B1", list(self.ref.reflight_track_points)
        )
        complaint = Complaint(
            complaint_id="CMP-1",
            field_id="FIELD-204",
            filed_by="周某",
            filed_at="2026-09-11T20:00:00+08:00",
            window_start="2026-09-11T06:00:00+08:00",
            window_end="2026-09-12T12:00:00+08:00",
            sensitive_area_id="APIARY-8",
            description="蜂群药害",
        )
        self.report = trace_complaint(
            complaint, self.platform.execution, self.platform.archive, self.platform.prescriptions
        )

    def test_trace_links_reviewer_and_chains(self):
        chains = {c["task_id"]: c for c in self.report["chains"]}
        self.assertEqual(set(chains), {"TASK-0907-A", "TASK-0907-B"})
        self.assertEqual(chains["TASK-0907-A"]["chain_role"], "原任务")
        self.assertEqual(chains["TASK-0907-B"]["chain_role"], "补飞任务")
        self.assertEqual(chains["TASK-0907-A"]["prescription"]["reviewer"], "李岚")
        self.assertEqual(chains["TASK-0907-B"]["parent_task_id"], "TASK-0907-A")

    def test_trace_exposes_intrusion_and_swaps(self):
        chain_a = next(c for c in self.report["chains"] if c["task_id"] == "TASK-0907-A")
        self.assertLess(chain_a["distance_to_complaint_area_m"], 120.0)
        self.assertEqual(len(chain_a["tank_swaps"]), 1)
        kinds = {a["kind"] for a in chain_a["anomalies"]}
        self.assertIn("buffer_intrusion", kinds)
        self.assertIn("out_of_bounds", kinds)
        self.assertIn("time_regression", kinds)
        self.assertIn("abnormal_dose", kinds)
        self.assertIn("point_conflict", kinds)

    def test_reflight_chain_is_separate(self):
        chain_b = next(c for c in self.report["chains"] if c["task_id"] == "TASK-0907-B")
        self.assertEqual(chain_b["anomalies"], [])
        self.assertGreater(chain_b["distance_to_complaint_area_m"], 120.0)
        chain_a = next(c for c in self.report["chains"] if c["task_id"] == "TASK-0907-A")
        self.assertNotEqual(
            chain_a["evidence"]["points_digest"], chain_b["evidence"]["points_digest"]
        )


if __name__ == "__main__":
    unittest.main()
