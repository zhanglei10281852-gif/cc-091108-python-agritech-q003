"""航迹同步幂等、异常分类、封存与补遗、补飞证据链测试。"""

from __future__ import annotations

from tests._support import ServiceCase

T = "2026-09-11"


class SyncIdempotencyTest(ServiceCase):
    def _ready(self, task="TASK-1", leg="L1"):
        self.approve("RX-1", legs=[{"leg_id": leg}])
        self.svc.create_task(task, "RX-1", f"{T}T07:30:00+08:00")
        self.svc.load_tank("TANK-01", "BATCH-2601", task, f"{T}T07:40:00+08:00")
        self.svc.observe_weather(task, f"{T}T07:55:00+08:00", 2.0, 0.0)
        self.svc.takeoff(task, leg, f"{T}T08:00:00+08:00")
        self.svc.land(task, leg, f"{T}T08:30:00+08:00")

    def test_duplicate_sync_not_double_counted(self):
        self._ready()
        pts = [self.pt("P1", f"{T}T08:00:01+08:00", 120.102, 30.202, spray=3.0),
               self.pt("P2", f"{T}T08:00:05+08:00", 120.1024, 30.202, spray=3.0)]
        self.svc.sync_track_points("TASK-1", "L1", "S1", pts)
        again = self.svc.sync_track_points("TASK-1", "L1", "S2", pts)
        self.assertEqual(len(again["rejected"]), 2)
        self.assertEqual({r["reason"] for r in again["rejected"]}, {"DUPLICATE"})
        a = self.svc.analyze("TASK-1", "L1")
        self.assertEqual(len(a["point_ids"]), 2)
        self.assertEqual(a["usage_ml_by_batch"]["BATCH-2601"], 6.0)

    def test_late_out_of_order_points_sort_on_device_time(self):
        self._ready()
        # domain.json 的乱序样例：P-2 设备时间晚却先到
        first = [self.pt("P-2", f"{T}T09:00:02+08:00", 120.102, 30.202)]
        second = [self.pt("P-1", f"{T}T09:00:01+08:00", 120.1015, 30.202)]
        self.svc.sync_track_points("TASK-1", "L1", "S1", first)
        self.svc.sync_track_points("TASK-1", "L1", "S2", second)
        a = self.svc.analyze("TASK-1", "L1")
        # 几何分析按设备时间，首点应为 P-1
        self.assertEqual(a["point_ids"], ["P-1", "P-2"])
        self.assertEqual(a["findings"]["TIME_REVERSAL"], [])  # 跨批次补传、设备时间本身不倒退


class FindingClassificationTest(ServiceCase):
    def test_sample_b_findings_separately_presented(self):
        _, sample_b = self.load_samples()
        self.approve("RX-B", batches=["BATCH-2601"],
                     legs=[{"leg_id": "B-L1", "planned_area_mu": 6.5}])
        self.svc.create_task("TASK-B", "RX-B", "2026-09-12T07:20:00+08:00")
        self.svc.load_tank("TANK-03", "BATCH-2601", "TASK-B",
                           "2026-09-12T07:25:00+08:00")
        self.svc.load_tank("TANK-09", "BATCH-777", "TASK-B",
                           "2026-09-12T08:05:00+08:00")
        bulk = [p for p in sample_b
                if p["received_at"].startswith("2026-09-12T08:20")]
        self.svc.sync_track_points("TASK-B", "B-L1", "S1", bulk)
        a = self.svc.analyze("TASK-B", "B-L1")
        f = a["findings"]
        self.assertGreaterEqual(len(f["BOUNDARY_EXCURSION"]), 1)
        self.assertGreaterEqual(len(f["BUFFER_INTRUSION"]), 1)
        self.assertEqual(len(f["TIME_REVERSAL"]), 1)
        # B-007 单点 40 ml/亩 超标签上限 25
        self.assertTrue(any(x["kind"] == "ABOVE_LABEL"
                            and x["point_id"] == "B-007"
                            for x in f["DOSE_ANOMALY"]))
        # TANK-09 -> BATCH-777 不在处方批次内
        mm = {(x["point_id"], x["batch_id"]) for x in f["BATCH_MISMATCH"]}
        self.assertTrue(any(batch == "BATCH-777" for _, batch in mm))
        # 实际用量仍按真实批次分别汇总
        self.assertIn("BATCH-777", a["usage_ml_by_batch"])
        self.assertIn("BATCH-2601", a["usage_ml_by_batch"])
        # 最近敏感区距离可汇报（负余量 = 侵入）
        near = a["nearest_sensitive"][0]
        self.assertEqual(near["sensitive_id"], "APIARY-8")
        self.assertLess(near["nearest_m"], near["buffer_m"])

    def test_unknown_tank_flagged(self):
        self.approve("RX-1", legs=[{"leg_id": "L1"}])
        self.svc.create_task("TASK-1", "RX-1", f"{T}T07:30:00+08:00")
        self.svc.load_tank("TANK-01", "BATCH-2601", "TASK-1", f"{T}T07:40:00+08:00")
        pts = [self.pt("P1", f"{T}T08:00:01+08:00", 120.102, 30.202,
                       tank="TANK-GHOST")]
        self.svc.sync_track_points("TASK-1", "L1", "S1", pts)
        a = self.svc.analyze("TASK-1", "L1")
        self.assertEqual(a["findings"]["UNKNOWN_TANK"][0]["tank_id"], "TANK-GHOST")
        self.assertIn("UNKNOWN::TANK-GHOST", a["usage_ml_by_batch"])


class SealAddendumTest(ServiceCase):
    def _sealed_task(self):
        self.approve("RX-1", legs=[{"leg_id": "L1"}])
        self.svc.create_task("TASK-1", "RX-1", f"{T}T07:30:00+08:00")
        self.svc.load_tank("TANK-01", "BATCH-2601", "TASK-1", f"{T}T07:40:00+08:00")
        pts = [self.pt("P1", f"{T}T08:00:01+08:00", 120.102, 30.202, spray=3.0),
               self.pt("P2", f"{T}T08:00:05+08:00", 120.1024, 30.202, spray=3.0)]
        self.svc.sync_track_points("TASK-1", "L1", "S1", pts)
        seal = self.svc.seal_conclusion("TASK-1", "审核人", f"{T}T10:00:00+08:00")
        return seal

    def test_seal_then_late_points_go_to_addendum_without_changing_hash(self):
        seal = self._sealed_task()
        late = [self.pt("P3", f"{T}T08:00:09+08:00", 120.1028, 30.202, spray=3.0,
                        received=f"{T}T18:00:00+08:00")]
        r = self.svc.sync_track_points("TASK-1", "L1", "S2", late)
        self.assertTrue(r["routed_to_addendum"])
        add = self.svc.record_addendum("TASK-1", "审核人", f"{T}T18:30:00+08:00")
        # 原封存哈希不变，补遗有独立哈希
        self.assertEqual(self.svc.seals["TASK-1"]["seal_hash"], seal["seal_hash"])
        self.assertEqual(len(add["addendum_hash"]), 64)
        self.assertNotEqual(add["addendum_hash"], seal["seal_hash"])
        # 补遗引用原封存哈希
        ev = [e for e in self.svc.ledger.events if e.type == "ADDENDUM_RECORDED"][-1]
        self.assertEqual(ev.payload["original_seal_hash"], seal["seal_hash"])
        self.assertEqual(ev.payload["legs"][0]["new_point_ids"], ["P3"])
        # 再补遗无新点则拒绝
        with self.assertRaisesRegex(Exception, "没有封存后的新定位点"):
            self.svc.record_addendum("TASK-1", "审核人", f"{T}T19:00:00+08:00")

    def test_double_seal_rejected(self):
        self._sealed_task()
        with self.assertRaisesRegex(Exception, "已封存"):
            self.svc.seal_conclusion("TASK-1", "审核人", f"{T}T11:00:00+08:00")

    def test_late_data_does_not_alter_sealed_usage(self):
        self._sealed_task()
        before = self.svc.export_summary("TASK-1")
        late = [self.pt("P9", f"{T}T08:00:20+08:00", 120.103, 30.202, spray=99.0,
                        received=f"{T}T18:00:00+08:00")]
        self.svc.sync_track_points("TASK-1", "L1", "S2", late)
        self.svc.record_addendum("TASK-1", "审核人", f"{T}T18:30:00+08:00")
        after = self.svc.export_summary("TASK-1")
        self.assertEqual(before["seal_hash"], after["seal_hash"])
        self.assertEqual(len(after["addendum_hashes"]), 1)

    def test_reflight_is_separate_evidence_chain(self):
        self._sealed_task()
        self.svc.file_complaint("COMP-1", "TASK-1", f"{T}T09:00:00+08:00",
                                "蜂场主", "怀疑药害")
        # 补飞使用新处方/新任务，指向原任务
        self.approve(rx_id="RX-2", legs=[{"leg_id": "R1"}])
        self.svc.create_task("TASK-2", "RX-2", "2026-09-14T07:00:00+08:00",
                             parent_task_id="TASK-1")
        trace = self.svc.complaint_trace("COMP-1")
        self.assertEqual(trace["child_reflight_tasks"], ["TASK-2"])
        self.assertIsNone(trace["parent_task_id"])
        self.assertEqual(self.svc.tasks["TASK-2"]["parent_task_id"], "TASK-1")


class ComplaintTraceTest(ServiceCase):
    def test_trace_links_reviewer_areas_swaps_and_distance(self):
        sample_a, _ = self.load_samples()
        self.approve(rx_id="RX-A",
                     batches=["BATCH-2601", "BATCH-2602"],
                     legs=[{"leg_id": "A-L1", "planned_area_mu": 6.5}])
        self.svc.create_task("TASK-A", "RX-A", "2026-09-11T07:30:00+08:00")
        self.svc.load_tank("TANK-01", "BATCH-2601", "TASK-A",
                           "2026-09-11T07:40:00+08:00")
        self.svc.load_tank("TANK-02", "BATCH-2602", "TASK-A",
                           "2026-09-11T08:16:00+08:00")
        self.svc.sync_track_points("TASK-A", "A-L1", "S1", sample_a)
        self.svc.seal_conclusion("TASK-A", "县植保站-王审签",
                                 "2026-09-11T15:00:00+08:00")
        self.svc.file_complaint("COMP-9", "TASK-A", "2026-09-13T09:00:00+08:00",
                                "蜂场主-周某", "蜂群死亡")
        trace = self.svc.complaint_trace("COMP-9")
        self.assertEqual(trace["prescription"]["reviewer"], "县植保站-王审签")
        self.assertEqual(len(trace["prescription"]["snapshot_hash"]), 64)
        self.assertGreater(trace["legs"][0]["covered_mu"], 1.0)
        swaps = trace["tank_swaps"]
        self.assertEqual(swaps[0]["from_tank"], "TANK-01")
        self.assertEqual(swaps[0]["to_tank"], "TANK-02")
        self.assertEqual(swaps[0]["batch_after"], "BATCH-2602")
        self.assertIsNotNone(trace["seal"]["seal_hash"])
        near = trace["legs"][0]["nearest_sensitive"][0]
        self.assertEqual(near["sensitive_id"], "APIARY-8")
        self.assertGreater(near["clearance_m"], 0)  # A 航次保持安全距离


if __name__ == "__main__":
    unittest.main()
