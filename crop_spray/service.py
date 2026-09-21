"""作业台核心服务：事件溯源。

所有写操作都追加到哈希链账本，状态由重放得到。关键规则：

* 处方审签即冻结作物/药剂批次/安全间隔/气象阈值，任务只引用冻结快照；
* 气象越限时，未起飞航段自动失效，空中航段必须记录返航决定；
* 航迹点按 point_id 幂等去重，重复同步不会多算用药量；
* 结论封存后迟到数据进入「补遗」，原结论哈希不变；
* 补飞是带 parent_task_id 的独立任务，证据链分开。
"""

from __future__ import annotations

from . import geo
from .analysis import analyze_leg
from .ledger import Ledger, digest
from .models import LegStatus, TaskStatus


class ServiceError(Exception):
    pass


class SprayService:
    def __init__(self, ledger: Ledger):
        self.ledger = ledger
        self._replay()

    # ------------------------------------------------------------------ 重放

    def _replay(self) -> None:
        e = self
        e.fields: dict[str, dict] = {}
        e.sensitive: dict[str, list[dict]] = {}  # field_id -> areas
        e.labels: dict[str, dict] = {}
        e.batches: dict[str, dict] = {}
        e.declarations: dict[str, dict] = {}
        e.prescriptions: dict[str, dict] = {}
        e.tasks: dict[str, dict] = {}
        e.tank_loads: dict[str, list[dict]] = {}   # tank_id -> 装载事实
        e.points: dict[str, dict] = {}             # point_id -> 点记录
        e.point_task: dict[str, str] = {}
        e.leg_points: dict[str, list[str]] = {}    # leg_id -> 到达顺序 point_id
        e.seals: dict[str, dict] = {}
        e.addenda: dict[str, list[dict]] = {}
        e.complaints: dict[str, dict] = {}
        e.weather: dict[str, list[dict]] = {}

        for ev in self.ledger.events:
            p = ev.payload
            h = ev.type
            if h == "FIELD_REGISTERED":
                e.fields[p["id"]] = p
                e.sensitive.setdefault(p["id"], [])
            elif h == "SENSITIVE_REGISTERED":
                e.sensitive.setdefault(p["field_id"], []).append(p)
            elif h == "LABEL_REGISTERED":
                e.labels[p["chemical_id"]] = p
            elif h == "BATCH_DECLARED":
                e.batches[p["batch_id"]] = p
            elif h == "FIELD_DECLARED":
                e.declarations[p["declaration_id"]] = p
            elif h == "PRESCRIPTION_SUBMITTED":
                e.prescriptions[p["rx_id"]] = {**p, "approved": False}
            elif h == "PRESCRIPTION_APPROVED":
                rx = e.prescriptions[p["rx_id"]]
                rx["approved"] = True
                rx["reviewer"] = p["reviewer"]
                rx["approved_at"] = p["approved_at"]
                rx["snapshot_hash"] = p["snapshot_hash"]
            elif h == "TASK_CREATED":
                e.tasks[p["task_id"]] = {
                    **p, "status": TaskStatus.OPEN.value,
                    "legs": {l["leg_id"]: {**l, "status": LegStatus.PLANNED.value,
                                           "events": []} for l in p["legs"]},
                }
            elif h == "TANK_LOADED":
                e.tank_loads.setdefault(p["tank_id"], []).append(p)
            elif h == "WEATHER_OBSERVED":
                e.weather.setdefault(p["task_id"], []).append(p)
                task = e.tasks.get(p["task_id"])
                if task is not None and not p["within"]:
                    for leg in task["legs"].values():
                        if leg["status"] == LegStatus.PLANNED.value:
                            leg["status"] = LegStatus.INVALIDATED.value
                            leg["events"].append(
                                {"kind": "AUTO_INVALIDATED", "at": p["at"],
                                 "reason": "气象越限：风速或降雨超过处方阈值"})
                        elif leg["status"] == LegStatus.AIRBORNE.value:
                            leg["status"] = LegStatus.BREACHED_AIRBORNE.value
                            leg["events"].append(
                                {"kind": "WEATHER_BREACH", "at": p["at"],
                                 "wind_m_s": p["wind_m_s"],
                                 "rainfall_mm_h": p["rainfall_mm_h"]})
            elif h == "LEG_TAKEN_OFF":
                leg = e._leg(p["task_id"], p["leg_id"])
                leg["status"] = LegStatus.AIRBORNE.value
                leg["events"].append({"kind": "TAKEN_OFF", "at": p["at"],
                                      "weather": p.get("weather")})
            elif h == "LEG_RECALL_DECIDED":
                leg = e._leg(p["task_id"], p["leg_id"])
                leg["events"].append({"kind": "RECALL_DECIDED", **p})
                if p["decision"] == "RETURN":
                    leg["status"] = LegStatus.RECALLED.value
            elif h == "LEG_LANDED":
                leg = e._leg(p["task_id"], p["leg_id"])
                if leg["status"] not in (LegStatus.RECALLED,):
                    leg["status"] = LegStatus.COMPLETED.value
                leg["events"].append({"kind": "LANDED", "at": p["at"]})
            elif h == "TRACK_POINTS_ACCEPTED":
                for rp in p["points"]:
                    e.points[rp["point_id"]] = rp
                    e.point_task[rp["point_id"]] = p["task_id"]
                    e.leg_points.setdefault(rp["leg_id"], []).append(rp["point_id"])
            elif h == "LEG_CONCLUSION_SEALED":
                e.seals[p["task_id"]] = p
            elif h == "ADDENDUM_RECORDED":
                e.addenda.setdefault(p["task_id"], []).append(p)
            elif h == "COMPLAINT_FILED":
                e.complaints[p["complaint_id"]] = p

    def _leg(self, task_id: str, leg_id: str) -> dict:
        task = self.tasks.get(task_id)
        if task is None:
            raise ServiceError(f"任务不存在: {task_id}")
        leg = task["legs"].get(leg_id)
        if leg is None:
            raise ServiceError(f"航段不存在: {leg_id}")
        return leg

    def _task_limits(self, task: dict) -> dict:
        return self.prescriptions[task["rx_id"]]["weather_limits"]

    # ------------------------------------------------------------- 基础数据

    def register_field(self, field: dict, actor: str = "系统") -> None:
        if field["id"] in self.fields:
            raise ServiceError(f"田块已存在: {field['id']}")
        ring = [tuple(x) for x in field["boundary"]]
        if not geo.boundary_closed(ring):
            raise ServiceError("田块边界必须首尾闭合")
        self.ledger.append("FIELD_REGISTERED", actor, field)
        self._replay()

    def register_sensitive_area(self, area: dict, actor: str = "系统") -> None:
        if area["field_id"] not in self.fields:
            raise ServiceError(f"田块不存在: {area['field_id']}")
        if any(s["id"] == area["id"] for s in self.sensitive[area["field_id"]]):
            raise ServiceError(f"敏感区已存在: {area['id']}")
        self.ledger.append("SENSITIVE_REGISTERED", actor, area)
        self._replay()

    def register_label(self, label: dict, actor: str = "系统") -> None:
        if not label["min_ml_per_mu"] < label["max_ml_per_mu"]:
            raise ServiceError("标签最小剂量必须小于最大剂量")
        self.ledger.append("LABEL_REGISTERED", actor, label)
        self._replay()

    def declare_batch(self, batch_id: str, chemical_id: str, qty_ml: float,
                      actor: str = "仓管") -> None:
        if chemical_id not in self.labels:
            raise ServiceError(f"药剂未登记: {chemical_id}")
        if batch_id in self.batches:
            raise ServiceError(f"批次已申报: {batch_id}")
        self.ledger.append("BATCH_DECLARED", actor,
                           {"batch_id": batch_id, "chemical_id": chemical_id,
                            "qty_ml": qty_ml})
        self._replay()

    # ------------------------------------------------------------- 申报审签

    def declare_field_planting(self, declaration_id: str, field_id: str, crop: str,
                               applicant: str, season: str) -> None:
        if field_id not in self.fields:
            raise ServiceError(f"田块不存在: {field_id}")
        if declaration_id in self.declarations:
            raise ServiceError(f"申报重复: {declaration_id}")
        self.ledger.append("FIELD_DECLARED", applicant, {
            "declaration_id": declaration_id, "field_id": field_id, "crop": crop,
            "applicant": applicant, "season": season})
        self._replay()

    def submit_prescription(self, rx: dict, actor: str) -> str:
        """rx 须含 rx_id/declaration_id/chemical_id/batches/legs/weather_limits。"""
        decl = self.declarations.get(rx["declaration_id"])
        if decl is None:
            raise ServiceError("田块申报不存在")
        label = self.labels.get(rx["chemical_id"])
        if label is None:
            raise ServiceError("药剂标签不存在")
        if label["crop"] != decl["crop"]:
            raise ServiceError(f"药剂 {rx['chemical_id']} 不适用于作物 {decl['crop']}")
        for b in rx["batches"]:
            if b not in self.batches:
                raise ServiceError(f"药剂批次未申报: {b}")
            if self.batches[b]["chemical_id"] != rx["chemical_id"]:
                raise ServiceError(f"批次 {b} 与处方药剂不符")
        for k in ("max_wind_m_s", "max_rainfall_mm_h"):
            if k not in rx["weather_limits"]:
                raise ServiceError(f"气象阈值缺项: {k}")
        rx = {**rx, "crop": decl["crop"], "reentry_hours": label["reentry_hours"],
              "dose_window_ml_per_mu": [label["min_ml_per_mu"], label["max_ml_per_mu"]]}
        self.ledger.append("PRESCRIPTION_SUBMITTED", actor, rx)
        self._replay()
        return rx["rx_id"]

    def approve_prescription(self, rx_id: str, reviewer: str, at: str) -> str:
        rx = self.prescriptions.get(rx_id)
        if rx is None:
            raise ServiceError("处方不存在")
        if rx["approved"]:
            raise ServiceError("处方已审签，内容固定不可改")
        # 冻结快照：作物、批次、安全间隔、阈值全部入哈希
        frozen = {k: rx[k] for k in (
            "rx_id", "declaration_id", "crop", "chemical_id", "batches",
            "reentry_hours", "dose_window_ml_per_mu", "weather_limits", "legs")}
        snapshot_hash = digest(frozen)
        self.ledger.append("PRESCRIPTION_APPROVED", reviewer, {
            "rx_id": rx_id, "reviewer": reviewer, "approved_at": at,
            "snapshot_hash": snapshot_hash, "frozen": frozen})
        self._replay()
        return snapshot_hash

    # ---------------------------------------------------------------- 任务

    def create_task(self, task_id: str, rx_id: str, at: str,
                    parent_task_id: str | None = None) -> None:
        rx = self.prescriptions.get(rx_id)
        if rx is None or not rx["approved"]:
            raise ServiceError("处方不存在或未经审签")
        if parent_task_id is not None:
            if parent_task_id not in self.tasks:
                raise ServiceError(f"原任务不存在: {parent_task_id}")
            if task_id == parent_task_id:
                raise ServiceError("补飞任务必须使用新任务号，不能与原任务同号")
        if task_id in self.tasks:
            raise ServiceError(f"任务号已存在: {task_id}")
        legs = [{"leg_id": l["leg_id"], "planned_area_mu": l.get("planned_area_mu")}
                for l in rx["legs"]]
        self.ledger.append("TASK_CREATED", "调度", {
            "task_id": task_id, "rx_id": rx_id, "parent_task_id": parent_task_id,
            "created_at": at, "rx_snapshot_hash": rx["snapshot_hash"], "legs": legs})
        self._replay()

    def load_tank(self, tank_id: str, batch_id: str, task_id: str, at: str,
                  actor: str = "加药员") -> None:
        if batch_id not in self.batches:
            raise ServiceError(f"批次未申报: {batch_id}")
        # 换箱节点：同一药箱重新装载或新编号都是一次装载事实
        self.ledger.append("TANK_LOADED", actor, {
            "tank_id": tank_id, "batch_id": batch_id, "task_id": task_id,
            "loaded_at": at})
        self._replay()

    def observe_weather(self, task_id: str, at: str, wind_m_s: float,
                        rainfall_mm_h: float) -> dict:
        if task_id not in self.tasks:
            raise ServiceError("任务不存在")
        limits = self._task_limits(self.tasks[task_id])
        within = wind_m_s <= limits["max_wind_m_s"] and rainfall_mm_h <= limits["max_rainfall_mm_h"]
        self.ledger.append("WEATHER_OBSERVED", "气象站", {
            "task_id": task_id, "at": at, "wind_m_s": wind_m_s,
            "rainfall_mm_h": rainfall_mm_h, "within": within,
            "limits": limits})
        self._replay()
        # 必须在 replay 之后取最新状态对象（replay 会重建状态字典）
        task = self.tasks[task_id]
        effects = {"within": within, "invalidated": [], "airborne_breach": []}
        for leg in task["legs"].values():
            last = leg["events"][-1] if leg["events"] else None
            if last and last["kind"] == "AUTO_INVALIDATED" and last["at"] == at:
                effects["invalidated"].append(leg["leg_id"])
            elif last and last["kind"] == "WEATHER_BREACH" and last["at"] == at:
                effects["airborne_breach"].append(leg["leg_id"])
        return effects

    def takeoff(self, task_id: str, leg_id: str, at: str, actor: str = "飞手") -> None:
        # 起飞当时必须满足气象条件；未起飞航段在越限观测时已自动失效
        latest = self._latest_weather(task_id, at)
        if latest is not None and not latest["within"]:
            raise ServiceError("当前气象越限，航段不得起飞（未起飞航段应自动失效）")
        leg = self._leg(task_id, leg_id)
        if leg["status"] != LegStatus.PLANNED.value:
            raise ServiceError(f"航段 {leg_id} 状态 {leg['status']}，不能起飞")
        self.ledger.append("LEG_TAKEN_OFF", actor, {
            "task_id": task_id, "leg_id": leg_id, "at": at,
            "weather": latest})
        self._replay()

    def _latest_weather(self, task_id: str, at: str) -> dict | None:
        obs = [w for w in self.weather.get(task_id, []) if w["at"] <= at]
        return obs[-1] if obs else None

    def record_recall_decision(self, task_id: str, leg_id: str, at: str,
                               decision: str, decided_by: str, note: str = "") -> None:
        """气象越限时空中航段必须记录决定：RETURN(返航) 或 CONTINUE(继续，需书面理由)。"""
        leg = self._leg(task_id, leg_id)
        if decision not in ("RETURN", "CONTINUE"):
            raise ServiceError("决定必须是 RETURN 或 CONTINUE")
        if decision == "RETURN":
            if leg["status"] not in (LegStatus.AIRBORNE.value,
                                     LegStatus.BREACHED_AIRBORNE.value):
                raise ServiceError("仅空中航段可记录返航")
        if decision == "CONTINUE" and not note:
            raise ServiceError("继续作业必须填写书面理由")
        self.ledger.append("LEG_RECALL_DECIDED", decided_by, {
            "task_id": task_id, "leg_id": leg_id, "at": at, "decision": decision,
            "decided_by": decided_by, "note": note})
        self._replay()

    def land(self, task_id: str, leg_id: str, at: str, actor: str = "飞手") -> None:
        leg = self._leg(task_id, leg_id)
        # RECALLED 也允许补记降落事实，但航段结论保持「返航」
        if leg["status"] not in (LegStatus.AIRBORNE.value,
                                 LegStatus.BREACHED_AIRBORNE.value,
                                 LegStatus.RECALLED.value):
            raise ServiceError(f"航段 {leg_id} 状态 {leg['status']}，不能降落")
        if leg["status"] == LegStatus.BREACHED_AIRBORNE.value:
            raise ServiceError("气象越限中的航段必须先记录返航/继续决定")
        self.ledger.append("LEG_LANDED", actor,
                           {"task_id": task_id, "leg_id": leg_id, "at": at})
        self._replay()

    # ------------------------------------------------------------- 航迹同步

    def sync_track_points(self, task_id: str, leg_id: str, sync_batch: str,
                          points: list[dict]) -> dict:
        """接收一批设备定位点（允许离线后补传）。

        * point_id 全局幂等：重复同步只登记拒绝事件，绝不重复计药；
        * 封存后到达的点进入补遗，不改写原结论。
        """
        self._leg(task_id, leg_id)
        accepted, rejected = [], []
        for raw in points:
            pid = raw["point_id"]
            if pid in self.points:
                rejected.append({"point_id": pid, "reason": "DUPLICATE",
                                 "detail": "该定位点已同步过，忽略以保证用药量不重复计算"})
                continue
            rp = {**raw, "task_id": task_id, "leg_id": leg_id,
                  "sync_batch": sync_batch}
            accepted.append(rp)
        if accepted:
            sealed = task_id in self.seals
            self.ledger.append("TRACK_POINTS_ACCEPTED", "设备同步", {
                "task_id": task_id, "leg_id": leg_id, "sync_batch": sync_batch,
                "points": accepted, "after_seal": sealed})
            self._replay()
        for r in rejected:
            self.ledger.append("TRACK_POINT_REJECTED", "设备同步", {
                "task_id": task_id, "leg_id": leg_id, "sync_batch": sync_batch, **r})
        self._replay()
        return {"accepted": [p["point_id"] for p in accepted],
                "rejected": rejected,
                "routed_to_addendum": task_id in self.seals and bool(accepted)}

    # ------------------------------------------------------------- 分析/封存

    def _tank_batch_map(self, task_id: str) -> dict[str, str]:
        m = {}
        for tank, loads in self.tank_loads.items():
            mine = [l for l in loads if l["task_id"] == task_id]
            if mine:
                m[tank] = mine[-1]["batch_id"]  # 最近一次装载
        return m

    def _leg_analysis_input(self, task_id: str, leg_id: str,
                            point_ids: list[str] | None = None) -> list[dict]:
        ids = point_ids if point_ids is not None else self.leg_points.get(leg_id, [])
        return [self.points[i] for i in ids]

    def analyze(self, task_id: str, leg_id: str,
                point_ids: list[str] | None = None) -> dict:
        task = self.tasks[task_id]
        rx = self.prescriptions[task["rx_id"]]
        decl = self.declarations[rx["declaration_id"]]
        field = self.fields[decl["field_id"]]
        boundary = [tuple(x) for x in field["boundary"]]
        label = self.labels[rx["chemical_id"]]
        points = self._leg_analysis_input(task_id, leg_id, point_ids)
        result = analyze_leg(
            leg_id, points, boundary,
            self.sensitive[decl["field_id"]], label,
            self._tank_batch_map(task_id), rx["batches"])
        return result.to_dict()

    def analyze_task(self, task_id: str) -> dict:
        task = self.tasks[task_id]
        return {lid: self.analyze(task_id, lid) for lid in task["legs"]}

    def seal_conclusion(self, task_id: str, sealed_by: str, at: str) -> dict:
        """封存任务结论：快照各航段分析与汇总，哈希固定。"""
        if task_id not in self.tasks:
            raise ServiceError("任务不存在")
        if task_id in self.seals:
            raise ServiceError("任务结论已封存，不可重复封存；迟到数据请走补遗")
        analyses = self.analyze_task(task_id)
        content = self._seal_content(task_id, analyses, at, sealed_by, kind="ORIGINAL")
        seal_hash = digest(content)
        head_after = self.ledger.head
        self.ledger.append("LEG_CONCLUSION_SEALED", sealed_by, {
            "task_id": task_id, "sealed_by": sealed_by, "sealed_at": at,
            "kind": "ORIGINAL", "head_hash": head_after, "seal_hash": seal_hash,
            "content": content})
        self._replay()
        return {"seal_hash": seal_hash, "head_hash": head_after}

    def _seal_content(self, task_id: str, analyses: dict, at: str,
                      sealed_by: str, kind: str) -> dict:
        task = self.tasks[task_id]
        rx = self.prescriptions[task["rx_id"]]
        decl = self.declarations[rx["declaration_id"]]
        total_ml: dict[str, float] = {}
        for a in analyses.values():
            for b, v in a["usage_ml_by_batch"].items():
                total_ml[b] = total_ml.get(b, 0.0) + v
        tank_swaps = self._tank_swap_nodes(task_id)
        return {
            "kind": kind,
            "task_id": task_id,
            "parent_task_id": task.get("parent_task_id"),
            "rx_id": task["rx_id"],
            "rx_snapshot_hash": task["rx_snapshot_hash"],
            "field_id": decl["field_id"],
            "crop": decl["crop"],
            "applicant": decl["applicant"],
            "reviewer": rx["reviewer"],
            "chemical_id": rx["chemical_id"],
            "approved_batches": rx["batches"],
            "reentry_hours": rx["reentry_hours"],
            "legs": [
                {"leg_id": lid, "status": l["status"],
                 "covered_mu": analyses[lid]["covered_mu"],
                 "planned_area_mu": l.get("planned_area_mu"),
                 "events": l["events"],
                 "findings": analyses[lid]["findings"],
                 "nearest_sensitive": analyses[lid]["nearest_sensitive"]}
                for lid, l in task["legs"].items()
            ],
            "tank_swaps": tank_swaps,
            "usage_ml_by_batch": {k: round(v, 2) for k, v in total_ml.items()},
            "point_ids": {lid: analyses[lid]["point_ids"] for lid in task["legs"]},
            "sealed_by": sealed_by,
            "sealed_at": at,
        }

    def _tank_swap_nodes(self, task_id: str) -> list[dict]:
        """换箱节点：按设备时间顺序，相邻航迹点药箱编号变化处。"""
        nodes: list[dict] = []
        for leg_id in self.tasks[task_id]["legs"]:
            pts = sorted(self._leg_analysis_input(task_id, leg_id),
                         key=lambda p: p["device_at"])
            prev_tank = None
            for p in pts:
                if prev_tank is not None and p["tank_id"] != prev_tank:
                    nodes.append({"leg_id": leg_id, "at": p["device_at"],
                                  "from_tank": prev_tank, "to_tank": p["tank_id"],
                                  "point_id": p["point_id"],
                                  "batch_after": self._tank_batch_map(task_id)
                                  .get(p["tank_id"])})
                prev_tank = p["tank_id"]
        nodes.sort(key=lambda n: n["at"])
        return nodes

    def record_addendum(self, task_id: str, sealed_by: str, at: str) -> dict:
        """封存后到达的数据形成补遗：单独分析、单独哈希，原结论不动。

        每个补遗只覆盖此前原结论与既往补遗均未计入的新点，
        因此重复同步/重复补遗都不会多算用药量。
        """
        seal = self.seals.get(task_id)
        if seal is None:
            raise ServiceError("任务尚未封存，无需补遗")
        covered_ids: set[str] = {
            pid for ids in seal["content"]["point_ids"].values() for pid in ids}
        for a in self.addenda.get(task_id, []):
            for leg in a["legs"]:
                covered_ids.update(leg["new_point_ids"])

        task = self.tasks[task_id]
        fresh_by_leg: dict[str, list[str]] = {}
        for lid in task["legs"]:
            fresh = [i for i in self.leg_points.get(lid, []) if i not in covered_ids]
            if fresh:
                fresh_by_leg[lid] = fresh
        if not fresh_by_leg:
            raise ServiceError("没有封存后的新定位点需要补遗")

        analyses = {lid: self.analyze(task_id, lid, ids)
                    for lid, ids in fresh_by_leg.items()}
        content = {
            "kind": "ADDENDUM", "task_id": task_id, "recorded_at": at,
            "recorded_by": sealed_by,
            "original_seal_hash": seal["seal_hash"],
            "note": "本补遗仅基于封存后到达的离线点重新分析，原结论及其哈希保持不变",
            "legs": [
                {"leg_id": lid, "new_point_ids": analyses[lid]["point_ids"],
                 "findings": analyses[lid]["findings"],
                 "covered_mu_increment": analyses[lid]["covered_mu"],
                 "usage_ml_by_batch": analyses[lid]["usage_ml_by_batch"],
                 "nearest_sensitive": analyses[lid]["nearest_sensitive"]}
                for lid in analyses
            ],
        }
        addendum_hash = digest(content)
        content["addendum_hash"] = addendum_hash
        self.ledger.append("ADDENDUM_RECORDED", sealed_by, content)
        self._replay()
        return {"task_id": task_id, "addendum_hash": addendum_hash,
                "legs": list(analyses)}

    # ------------------------------------------------------------- 投诉追溯

    def file_complaint(self, complaint_id: str, task_id: str, at: str,
                       complainant: str, allegation: str) -> None:
        if task_id not in self.tasks:
            raise ServiceError("任务不存在")
        if complaint_id in self.complaints:
            raise ServiceError("投诉号重复")
        self.ledger.append("COMPLAINT_FILED", complainant, {
            "complaint_id": complaint_id, "task_id": task_id, "at": at,
            "complainant": complainant, "allegation": allegation})
        self._replay()

    def complaint_trace(self, complaint_id: str) -> dict:
        """一次投诉 -> 审核人/航段覆盖/换箱节点/敏感区距离/封存哈希的完整链条。"""
        comp = self.complaints.get(complaint_id)
        if comp is None:
            raise ServiceError("投诉不存在")
        task_id = comp["task_id"]
        task = self.tasks[task_id]
        seal = self.seals.get(task_id)
        analyses = self.analyze_task(task_id) if seal is None else None
        rx = self.prescriptions[task["rx_id"]]
        decl = self.declarations[rx["declaration_id"]]
        legs_view = []
        for lid, leg in task["legs"].items():
            if seal:
                sealed_leg = next(l for l in seal["content"]["legs"]
                                  if l["leg_id"] == lid)
                covered_mu = sealed_leg["covered_mu"]
                findings = sealed_leg["findings"]
                nearest = sealed_leg["nearest_sensitive"]
            else:
                covered_mu = analyses[lid]["covered_mu"]
                findings = analyses[lid]["findings"]
                nearest = analyses[lid]["nearest_sensitive"]
            legs_view.append({
                "leg_id": lid, "status": leg["status"],
                "planned_area_mu": leg.get("planned_area_mu"),
                "covered_mu": covered_mu,
                "findings": findings, "nearest_sensitive": nearest,
                "decision_events": [e for e in leg["events"]
                                    if e["kind"] in ("RECALL_DECIDED", "AUTO_INVALIDATED",
                                                     "WEATHER_BREACH")],
            })
        return {
            "complaint_id": complaint_id, "allegation": comp["allegation"],
            "task_id": task_id,
            "parent_task_id": task.get("parent_task_id"),
            "child_reflight_tasks": [
                t for t, v in self.tasks.items()
                if v.get("parent_task_id") == task_id],
            "declaration": {"declaration_id": decl["declaration_id"],
                            "field_id": decl["field_id"], "crop": decl["crop"],
                            "applicant": decl["applicant"]},
            "prescription": {"rx_id": rx["rx_id"], "reviewer": rx.get("reviewer"),
                             "approved_at": rx.get("approved_at"),
                             "snapshot_hash": rx.get("snapshot_hash"),
                             "chemical_id": rx["chemical_id"],
                             "approved_batches": rx["batches"],
                             "reentry_hours": rx["reentry_hours"]},
            "legs": legs_view,
            "tank_swaps": self._tank_swap_nodes(task_id),
            "usage_ml_by_batch": (seal["content"]["usage_ml_by_batch"] if seal
                                  else self._aggregate_usage(task_id)),
            "seal": (None if seal is None else
                     {"seal_hash": seal["seal_hash"], "head_hash": seal["head_hash"],
                      "sealed_at": seal["sealed_at"], "sealed_by": seal["sealed_by"]}),
            "addenda": [{"addendum_hash": a["addendum_hash"], "recorded_at": a["recorded_at"],
                         "legs": [{"leg_id": l["leg_id"],
                                   "new_point_ids": l["new_point_ids"],
                                   "findings": l["findings"]} for l in a["legs"]]}
                        for a in self.addenda.get(task_id, [])],
            "ledger_head": self.ledger.head,
        }

    def _aggregate_usage(self, task_id: str) -> dict:
        total: dict[str, float] = {}
        for a in self.analyze_task(task_id).values():
            for b, v in a["usage_ml_by_batch"].items():
                total[b] = total.get(b, 0.0) + v
        return {k: round(v, 2) for k, v in total.items()}

    def export_summary(self, task_id: str) -> dict:
        """导出可校验摘要：含账本路径无关的链头与封存哈希。"""
        if task_id not in self.tasks:
            raise ServiceError("任务不存在")
        seal = self.seals.get(task_id)
        return {
            "task_id": task_id,
            "ledger_head": self.ledger.head,
            "event_count": len(self.ledger.events),
            "seal_hash": seal["seal_hash"] if seal else None,
            "addendum_hashes": [a["addendum_hash"] for a in self.addenda.get(task_id, [])],
            "rx_snapshot_hash": self.tasks[task_id]["rx_snapshot_hash"],
            "exported_for_verification": True,
        }

    def verify(self) -> dict:
        """重算哈希链并复核封存快照与导出摘要一致。"""
        self.ledger.verify()
        seals = {}
        for task_id, seal in self.seals.items():
            seals[task_id] = digest(seal["content"]) == seal["seal_hash"]
        return {"ledger_ok": True, "events": len(self.ledger.events),
                "head": self.ledger.head, "seals_ok": seals}
