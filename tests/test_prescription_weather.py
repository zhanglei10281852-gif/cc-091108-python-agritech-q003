"""处方审签冻结与任务规则测试。"""

from __future__ import annotations

from tests._support import ServiceCase


class PrescriptionTest(ServiceCase):
    def test_approval_freezes_snapshot_and_task_carries_hash(self):
        snap = self.approve("RX-1", batches=["BATCH-2601", "BATCH-2602"],
                            legs=[{"leg_id": "L1", "planned_area_mu": 6.0}])
        self.assertEqual(len(snap), 64)
        self.svc.create_task("TASK-1", "RX-1", "2026-09-11T07:30:00+08:00")
        self.assertEqual(self.svc.tasks["TASK-1"]["rx_snapshot_hash"], snap)
        # 重复审签被拒
        with self.assertRaisesRegex(Exception, "已审签"):
            self.svc.approve_prescription("RX-1", "另一人", "2026-09-11T08:00:00+08:00")

    def test_crop_mismatch_rejected(self):
        self.svc.declare_field_planting("DECL-2", "FIELD-204", "小麦",
                                        applicant="钱某", season="2026 冬小麦")
        # CHEM-A17 标签作物为水稻
        with self.assertRaisesRegex(Exception, "不适用"):
            self.svc.submit_prescription({
                "rx_id": "RX-X", "declaration_id": "DECL-2",
                "chemical_id": "CHEM-A17", "batches": ["BATCH-2601"],
                "weather_limits": {"max_wind_m_s": 3.0, "max_rainfall_mm_h": 0.0},
                "legs": [{"leg_id": "L1"}]}, actor="飞防组织")

    def test_undeclared_batch_rejected(self):
        with self.assertRaisesRegex(Exception, "批次未申报"):
            self.svc.submit_prescription({
                "rx_id": "RX-X", "declaration_id": "DECL-01",
                "chemical_id": "CHEM-A17", "batches": ["BATCH-NOPE"],
                "weather_limits": {"max_wind_m_s": 3.0, "max_rainfall_mm_h": 0.0},
                "legs": [{"leg_id": "L1"}]}, actor="飞防组织")

    def test_task_requires_approved_rx(self):
        self.svc.submit_prescription({
            "rx_id": "RX-D", "declaration_id": "DECL-01",
            "chemical_id": "CHEM-A17", "batches": ["BATCH-2601"],
            "weather_limits": {"max_wind_m_s": 3.0, "max_rainfall_mm_h": 0.0},
            "legs": [{"leg_id": "L1"}]}, actor="飞防组织")
        with self.assertRaisesRegex(Exception, "未经审签"):
            self.svc.create_task("TASK-X", "RX-D", "2026-09-11T07:30:00+08:00")

    def test_reflight_must_be_new_task_id(self):
        self.approve("RX-1", legs=[{"leg_id": "L1"}])
        self.svc.create_task("TASK-1", "RX-1", "2026-09-11T07:30:00+08:00")
        with self.assertRaisesRegex(Exception, "新任务号"):
            self.svc.create_task("TASK-1", "RX-1", "2026-09-14T07:00:00+08:00",
                                 parent_task_id="TASK-1")


class WeatherTest(ServiceCase):
    def _task(self):
        self.approve("RX-1", legs=[{"leg_id": "L1"}, {"leg_id": "L2"}])
        self.svc.create_task("TASK-1", "RX-1", "2026-09-11T07:30:00+08:00")

    def test_unflown_leg_auto_invalidates(self):
        self._task()
        self.svc.observe_weather("TASK-1", "2026-09-11T07:50:00+08:00", 2.0, 0.0)
        self.svc.takeoff("TASK-1", "L1", "2026-09-11T08:00:00+08:00")
        eff = self.svc.observe_weather("TASK-1", "2026-09-11T08:20:00+08:00", 4.5, 0.0)
        self.assertEqual(eff["invalidated"], ["L2"])
        self.assertEqual(eff["airborne_breach"], ["L1"])
        legs = self.svc.tasks["TASK-1"]["legs"]
        self.assertEqual(legs["L2"]["status"], "INVALIDATED")
        self.assertEqual(legs["L1"]["status"], "BREACHED_AIRBORNE")
        # 失效航段不能再起飞
        with self.assertRaisesRegex(Exception, "不得起飞|自动失效"):
            self.svc.takeoff("TASK-1", "L2", "2026-09-11T09:00:00+08:00")

    def test_airborne_must_record_recall_decision_before_landing(self):
        self._task()
        self.svc.observe_weather("TASK-1", "2026-09-11T07:50:00+08:00", 2.0, 0.0)
        self.svc.takeoff("TASK-1", "L1", "2026-09-11T08:00:00+08:00")
        self.svc.observe_weather("TASK-1", "2026-09-11T08:20:00+08:00", 4.5, 2.0)
        with self.assertRaisesRegex(Exception, "返航/继续决定"):
            self.svc.land("TASK-1", "L1", "2026-09-11T08:25:00+08:00")
        self.svc.record_recall_decision(
            "TASK-1", "L1", "2026-09-11T08:21:00+08:00", "RETURN",
            decided_by="飞手", note="风速越限返航")
        self.assertEqual(self.svc.tasks["TASK-1"]["legs"]["L1"]["status"], "RECALLED")
        self.svc.land("TASK-1", "L1", "2026-09-11T08:25:00+08:00")

    def test_takeoff_blocked_when_weather_over_limit(self):
        self._task()
        self.svc.observe_weather("TASK-1", "2026-09-11T07:50:00+08:00", 5.0, 0.0)
        # 越限观测时未起飞航段已自动失效，再起飞被拒
        with self.assertRaisesRegex(Exception, "不得起飞|INVALIDATED"):
            self.svc.takeoff("TASK-1", "L1", "2026-09-11T08:00:00+08:00")

    def test_continue_requires_written_reason(self):
        self._task()
        self.svc.observe_weather("TASK-1", "2026-09-11T07:50:00+08:00", 2.0, 0.0)
        self.svc.takeoff("TASK-1", "L1", "2026-09-11T08:00:00+08:00")
        self.svc.observe_weather("TASK-1", "2026-09-11T08:20:00+08:00", 4.5, 0.0)
        with self.assertRaisesRegex(Exception, "书面理由"):
            self.svc.record_recall_decision(
                "TASK-1", "L1", "2026-09-11T08:21:00+08:00", "CONTINUE",
                decided_by="飞手")


if __name__ == "__main__":
    unittest.main()
