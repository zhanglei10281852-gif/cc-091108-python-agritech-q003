"""测试辅助：从 reference 资料构建已审签、可作业的服务。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from crop_spray import Ledger, SprayService

ROOT = Path(__file__).resolve().parents[1]
TZ = "+08:00"


class ServiceCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.svc = SprayService(Ledger(Path(self._tmp.name) / "ledger.jsonl"))
        d = json.loads((ROOT / "reference" / "domain.json").read_text(encoding="utf-8"))
        self.svc.register_field(d["field"])
        self.svc.register_sensitive_area({
            "id": d["sensitive_areas"][0]["id"], "field_id": d["field"]["id"],
            "kind": d["sensitive_areas"][0]["kind"],
            "buffer_m": d["sensitive_areas"][0]["buffer_m"],
            "point": d["sensitive_areas"][0]["point"]})
        self.svc.register_label(d["chemical_label"])
        self.svc.declare_batch("BATCH-2601", "CHEM-A17", 200_000)
        self.svc.declare_batch("BATCH-2602", "CHEM-A17", 200_000)
        self.svc.declare_batch("BATCH-777", "CHEM-A17", 100_000)
        self.svc.declare_field_planting("DECL-01", "FIELD-204", "水稻",
                                        applicant="种植户-赵某", season="2026 晚稻")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def approve(self, rx_id: str = "RX-1", batches=None, legs=None) -> str:
        batches = batches or ["BATCH-2601"]
        legs = legs or [{"leg_id": "L1", "planned_area_mu": 6.0}]
        self.svc.submit_prescription({
            "rx_id": rx_id, "declaration_id": "DECL-01",
            "chemical_id": "CHEM-A17", "batches": batches,
            "weather_limits": {"max_wind_m_s": 3.0, "max_rainfall_mm_h": 0.0},
            "legs": legs}, actor="飞防组织")
        return self.svc.approve_prescription(rx_id, reviewer="县植保站-王审签",
                                             at=f"2026-09-10T16:30:00{TZ}")

    def load_samples(self) -> tuple[list, list]:
        a = json.loads((ROOT / "reference" / "track_sample_a.json").read_text("utf-8"))
        b = json.loads((ROOT / "reference" / "track_sample_b.json").read_text("utf-8"))
        return a, b

    def pt(self, pid, at, lng, lat, tank="TANK-01", dose=22.0, spray=3.0,
           received=None):
        return {"point_id": pid, "device_at": at,
                "received_at": received or at,
                "position": [lng, lat], "tank_id": tank,
                "dose_ml_per_mu": dose, "spray_ml": spray}
