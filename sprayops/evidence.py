"""证据归档。

航迹按批次同步入库，规则如下：
- 同一批次重复同步直接忽略；不同批次中的同名点按点去重，
  因此重复同步不会多算用药量；
- 同名点内容不一致时保留先到版本，后到版本记入冲突清单单独呈现，
  不允许后到数据悄悄改写原结论；
- 每接受一批新定位点，任务的证据修订号（revision）加一，
  每个修订号的指标快照独立保存，可对照、可回溯；
- 越界片段、时间倒退、异常剂量、缓冲带侵入按类别单独成项。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field

from . import geo
from .declaration import Declaration
from .models import (
    Anomaly,
    AnomalyKind,
    ChemicalLabel,
    TankLoad,
    TankSwap,
    TaskPlan,
    TrackPoint,
    parse_ts,
)

ROUND = 6


def _r(x: float) -> float:
    return round(x, ROUND)


def _canonical(payload) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _point_payload(p: TrackPoint) -> dict:
    """点的实质内容（不含接收时间），用于冲突判定。"""
    return {
        "point_id": p.point_id,
        "device_at": p.device_at,
        "position": [p.position[0], p.position[1]],
        "tank_id": p.tank_id,
        "spraying": p.spraying,
        "flow_ml_per_min": p.flow_ml_per_min,
    }


def _anomaly_key(kind: AnomalyKind, point_ids: tuple[str, ...]) -> str:
    return f"{kind.value}:{'+'.join(point_ids)}"


@dataclass(frozen=True)
class IngestReport:
    task_id: str
    batch_id: str
    accepted: tuple[str, ...]
    duplicates: tuple[str, ...]
    conflicts: tuple[Anomaly, ...]
    time_regressions: tuple[Anomaly, ...]
    revision: int
    batch_already_archived: bool = False


@dataclass
class _TaskEvidence:
    task: TaskPlan
    declaration: Declaration
    label: ChemicalLabel
    default_swath_m: float
    points: dict[str, TrackPoint] = field(default_factory=dict)
    ingest_seq: dict[str, int] = field(default_factory=dict)
    seq_counter: int = 0
    batches: dict[str, dict] = field(default_factory=dict)
    ingest_anomalies: list[Anomaly] = field(default_factory=list)  # 时间倒退、冲突
    timeline_anomalies: dict[str, Anomaly] = field(default_factory=dict)
    tanks: dict[str, TankLoad] = field(default_factory=dict)
    revision: int = 0
    snapshots: dict[int, dict] = field(default_factory=dict)


class EvidenceArchive:
    def __init__(self) -> None:
        self._tasks: dict[str, _TaskEvidence] = {}

    # ------------------------------------------------------------ 登记

    def register_task(
        self,
        task: TaskPlan,
        declaration: Declaration,
        label: ChemicalLabel,
        default_swath_m: float | None = None,
    ) -> None:
        if task.task_id in self._tasks:
            return
        if default_swath_m is None:
            default_swath_m = next(iter(task.segments.values())).plan.swath_m
        self._tasks[task.task_id] = _TaskEvidence(
            task=task,
            declaration=declaration,
            label=label,
            default_swath_m=default_swath_m,
        )

    def register_tank_load(self, load: TankLoad) -> None:
        ev = self._require(load.task_id)
        ev.tanks[load.tank_id] = load

    # ------------------------------------------------------------ 同步入库

    def ingest_batch(self, task_id: str, batch_id: str, points: list[TrackPoint]) -> IngestReport:
        ev = self._require(task_id)
        manifest = {
            "batch_id": batch_id,
            "points": [_point_payload(p) for p in points],
        }
        manifest["sha256"] = hashlib.sha256(_canonical(manifest["points"]).encode()).hexdigest()

        if batch_id in ev.batches:
            stored = ev.batches[batch_id]
            if stored["sha256"] != manifest["sha256"]:
                raise ValueError(f"批次 {batch_id} 与已归档批次同名但内容不一致")
            return IngestReport(
                task_id=task_id,
                batch_id=batch_id,
                accepted=(),
                duplicates=tuple(p.point_id for p in points),
                conflicts=(),
                time_regressions=(),
                revision=ev.revision,
                batch_already_archived=True,
            )

        accepted: list[str] = []
        duplicates: list[str] = []
        conflicts: list[Anomaly] = []
        regressions: list[Anomaly] = []

        for p in points:
            existing = ev.points.get(p.point_id)
            if existing is None:
                ev.points[p.point_id] = p
                ev.seq_counter += 1
                ev.ingest_seq[p.point_id] = ev.seq_counter
                accepted.append(p.point_id)
            elif _point_payload(existing) == _point_payload(p):
                duplicates.append(p.point_id)
            else:
                conflicts.append(
                    Anomaly(
                        kind=AnomalyKind.POINT_CONFLICT,
                        point_ids=(p.point_id,),
                        detail={
                            "batch_id": batch_id,
                            "kept": _point_payload(existing),
                            "rejected": _point_payload(p),
                            "note": "同名点内容不一致，保留先到版本，后到版本未采纳",
                        },
                        first_seen_revision=ev.revision + 1,
                    )
                )

        # 时间倒退：同一批次中新记录点的设备时间在文件顺序上应当单调不减
        accepted_set = set(accepted)
        running_max = None
        running_max_id = None
        for p in points:
            if p.point_id not in accepted_set:
                continue
            ts = parse_ts(p.device_at)
            if running_max is not None and ts < running_max:
                regressions.append(
                    Anomaly(
                        kind=AnomalyKind.TIME_REGRESSION,
                        point_ids=(p.point_id,),
                        detail={
                            "device_at": p.device_at,
                            "previous_max_device_at": running_max_id[1],
                            "previous_max_point_id": running_max_id[0],
                            "batch_id": batch_id,
                        },
                        first_seen_revision=ev.revision + 1,
                    )
                )
            else:
                running_max = ts
                running_max_id = (p.point_id, p.device_at)

        ev.batches[batch_id] = manifest
        ev.ingest_anomalies.extend(regressions)
        # 冲突按（点、内容）去重，保证重复同步幂等
        new_conflicts = 0
        for c in conflicts:
            key = _anomaly_key(c.kind, c.point_ids)
            known = {
                _anomaly_key(a.kind, a.point_ids)
                for a in ev.ingest_anomalies
                if a.kind is AnomalyKind.POINT_CONFLICT
            }
            if key not in known:
                ev.ingest_anomalies.append(c)
                new_conflicts += 1

        if accepted or regressions or new_conflicts:
            ev.revision += 1
            self._refresh_timeline_anomalies(ev)
            ev.snapshots[ev.revision] = {
                "revision": ev.revision,
                "points_digest": self.points_digest(task_id),
                "metrics": self.metrics(task_id),
                "anomaly_keys": sorted(self.anomaly_keys(task_id)),
            }

        return IngestReport(
            task_id=task_id,
            batch_id=batch_id,
            accepted=tuple(accepted),
            duplicates=tuple(duplicates),
            conflicts=tuple(conflicts),
            time_regressions=tuple(regressions),
            revision=ev.revision,
        )

    # ------------------------------------------------------------ 时间线与指标

    def timeline(self, task_id: str) -> list[TrackPoint]:
        ev = self._require(task_id)
        return sorted(
            ev.points.values(),
            key=lambda p: (parse_ts(p.device_at), ev.ingest_seq[p.point_id]),
        )

    def points_digest(self, task_id: str) -> str:
        ev = self._require(task_id)
        payload = [_point_payload(p) for p in self.timeline(task_id)]
        return hashlib.sha256(_canonical(payload).encode()).hexdigest()

    def tank_swaps(self, task_id: str) -> list[TankSwap]:
        ev = self._require(task_id)
        swaps: list[TankSwap] = []
        line = self.timeline(task_id)
        for prev, cur in zip(line, line[1:]):
            if prev.tank_id != cur.tank_id:
                from_load = ev.tanks.get(prev.tank_id)
                to_load = ev.tanks.get(cur.tank_id)
                swaps.append(
                    TankSwap(
                        at=cur.device_at,
                        position=cur.position,
                        from_tank=prev.tank_id,
                        to_tank=cur.tank_id,
                        from_batch=from_load.batch_id if from_load else "未登记",
                        to_batch=to_load.batch_id if to_load else "未登记",
                    )
                )
        return swaps

    def metrics(self, task_id: str) -> dict:
        """按当前时间线计算航段覆盖、用药量与敏感区距离。"""
        ev = self._require(task_id)
        line = self.timeline(task_id)
        boundary = list(ev.declaration.parcel.boundary)
        sensitive = ev.declaration.sensitive_areas

        segments: dict[str, dict] = {
            sid: {
                "status": st.status.value,
                "coverage_mu": 0.0,
                "volume_ml": 0.0,
                "sprayed_points": 0,
                "min_distance_to_sensitive_m": {a.area_id: None for a in sensitive},
            }
            for sid, st in ev.task.segments.items()
        }
        unattributed = {"coverage_mu": 0.0, "volume_ml": 0.0, "intervals": []}
        tank_volume: dict[str, float] = {}
        totals_min_dist: dict[str, float | None] = {a.area_id: None for a in sensitive}

        def note_distance(sid: str | None, p: TrackPoint) -> None:
            if not p.spraying:
                return
            for area in sensitive:
                d = geo.haversine_m(p.position, area.point)
                if sid is not None:
                    cur = segments[sid]["min_distance_to_sensitive_m"][area.area_id]
                    if cur is None or d < cur:
                        segments[sid]["min_distance_to_sensitive_m"][area.area_id] = _r(d)
                cur_all = totals_min_dist[area.area_id]
                if cur_all is None or d < cur_all:
                    totals_min_dist[area.area_id] = d

        for p in line:
            note_distance(self._segment_for_point(ev, p), p)

        for a, b in zip(line, line[1:]):
            dt_s = (parse_ts(b.device_at) - parse_ts(a.device_at)).total_seconds()
            if dt_s <= 0 or not a.spraying:
                continue
            dist_m = geo.haversine_m(a.position, b.position)
            sid = self._segment_for_interval(ev, a)
            swath = (
                ev.task.segments[sid].plan.swath_m if sid else ev.default_swath_m
            )
            area_mu = geo.m2_to_mu(dist_m * swath)
            if area_mu <= 0:
                continue
            volume = a.flow_ml_per_min * (dt_s / 60.0)
            tank_volume[a.tank_id] = tank_volume.get(a.tank_id, 0.0) + volume
            if sid is None:
                unattributed["coverage_mu"] += area_mu
                unattributed["volume_ml"] += volume
                unattributed["intervals"].append((a.point_id, b.point_id))
            else:
                segments[sid]["coverage_mu"] += area_mu
                segments[sid]["volume_ml"] += volume

        for sid in ev.task.segments:
            seg = segments[sid]
            seg["coverage_mu"] = _r(seg["coverage_mu"])
            seg["volume_ml"] = _r(seg["volume_ml"])
            seg["dose_ml_per_mu"] = (
                _r(seg["volume_ml"] / seg["coverage_mu"]) if seg["coverage_mu"] > 0 else None
            )
            seg["sprayed_points"] = sum(
                1
                for p in line
                if p.spraying and self._segment_for_point(ev, p) == sid
            )
        unattributed["coverage_mu"] = _r(unattributed["coverage_mu"])
        unattributed["volume_ml"] = _r(unattributed["volume_ml"])

        total_cov = _r(
            sum(s["coverage_mu"] for s in segments.values()) + unattributed["coverage_mu"]
        )
        total_vol = _r(
            sum(s["volume_ml"] for s in segments.values()) + unattributed["volume_ml"]
        )
        return {
            "segments": segments,
            "unattributed": unattributed,
            "tank_volume_ml": {k: _r(v) for k, v in sorted(tank_volume.items())},
            "totals": {
                "coverage_mu": total_cov,
                "volume_ml": total_vol,
                "dose_ml_per_mu": _r(total_vol / total_cov) if total_cov > 0 else None,
                "track_points": len(line),
                "min_distance_to_sensitive_m": {
                    k: (_r(v) if v is not None else None) for k, v in totals_min_dist.items()
                },
            },
        }

    # ------------------------------------------------------------ 异常

    def anomalies(self, task_id: str) -> list[Anomaly]:
        ev = self._require(task_id)
        return sorted(
            [*ev.ingest_anomalies, *ev.timeline_anomalies.values()],
            key=lambda a: (a.kind.value, a.point_ids),
        )

    def anomaly_keys(self, task_id: str) -> set[str]:
        return {_anomaly_key(a.kind, a.point_ids) for a in self.anomalies(task_id)}

    def revision(self, task_id: str) -> int:
        return self._require(task_id).revision

    def snapshot(self, task_id: str, revision: int) -> dict:
        ev = self._require(task_id)
        snap = ev.snapshots.get(revision)
        if snap is None:
            raise KeyError(f"任务 {task_id} 没有修订号 {revision}")
        return snap

    def revisions(self, task_id: str) -> list[int]:
        return sorted(self._require(task_id).snapshots)

    def batches(self, task_id: str) -> dict[str, dict]:
        return dict(self._require(task_id).batches)

    def tank_loads(self, task_id: str) -> dict[str, TankLoad]:
        return dict(self._require(task_id).tanks)

    # ------------------------------------------------------------ 内部

    def _require(self, task_id: str) -> _TaskEvidence:
        ev = self._tasks.get(task_id)
        if ev is None:
            raise KeyError(f"任务 {task_id} 未在证据库登记")
        return ev

    @staticmethod
    def _segment_for_point(ev: _TaskEvidence, p: TrackPoint) -> str | None:
        ts = parse_ts(p.device_at)
        for sid, st in ev.task.segments.items():
            if st.started_at and st.ended_at:
                if parse_ts(st.started_at) <= ts <= parse_ts(st.ended_at):
                    return sid
        return None

    def _segment_for_interval(self, ev: _TaskEvidence, start: TrackPoint) -> str | None:
        return self._segment_for_point(ev, start)

    def _refresh_timeline_anomalies(self, ev: _TaskEvidence) -> None:
        """按当前时间线全量重算可复算异常；新出现的异常记录首次出现的修订号。"""
        found: dict[str, Anomaly] = {}
        line = self.timeline(ev.task.task_id)
        boundary = list(ev.declaration.parcel.boundary)
        label = ev.label

        def put(kind: AnomalyKind, point_ids: tuple[str, ...], detail: dict) -> None:
            key = _anomaly_key(kind, point_ids)
            first_seen = ev.revision
            existing = ev.timeline_anomalies.get(key)
            if existing is not None:
                first_seen = existing.first_seen_revision
            found[key] = Anomaly(
                kind=kind,
                point_ids=point_ids,
                detail=detail,
                first_seen_revision=first_seen,
            )

        def runs(pred) -> list[list[TrackPoint]]:
            """时间线上满足条件的连续点段。"""
            groups: list[list[TrackPoint]] = []
            cur: list[TrackPoint] = []
            for p in line:
                if pred(p):
                    cur.append(p)
                elif cur:
                    groups.append(cur)
                    cur = []
            if cur:
                groups.append(cur)
            return groups

        # 越界片段：位于田块边界之外的连续航迹
        for group in runs(lambda p: not geo.point_in_polygon(p.position, boundary)):
            put(
                AnomalyKind.OUT_OF_BOUNDS,
                tuple(p.point_id for p in group),
                {
                    "positions": [list(p.position) for p in group],
                    "device_span": [group[0].device_at, group[-1].device_at],
                    "note": "航迹点位于田块边界之外",
                },
            )

        # 禁飞区侵入
        zones = [list(z.polygon) for z in ev.declaration.no_fly_zones]
        if zones:
            for group in runs(
                lambda p: any(geo.point_in_polygon(p.position, z) for z in zones)
            ):
                put(
                    AnomalyKind.NO_FLY_INTRUSION,
                    tuple(p.point_id for p in group),
                    {
                        "positions": [list(p.position) for p in group],
                        "device_span": [group[0].device_at, group[-1].device_at],
                        "note": "航迹点落入禁飞区",
                    },
                )

        # 缓冲带侵入：喷洒状态下距敏感区域不超过缓冲距离
        next_of = {id(a): b for a, b in zip(line, line[1:])}
        for area in ev.declaration.sensitive_areas:
            for group in runs(
                lambda p, area=area: p.spraying
                and geo.haversine_m(p.position, area.point) <= area.buffer_m
            ):
                min_d = min(geo.haversine_m(p.position, area.point) for p in group)
                volume_ml = 0.0
                for p in group:
                    nxt = next_of.get(id(p))
                    if nxt is not None:
                        dt_s = (
                            parse_ts(nxt.device_at) - parse_ts(p.device_at)
                        ).total_seconds()
                        if dt_s > 0:
                            volume_ml += p.flow_ml_per_min * (dt_s / 60.0)
                put(
                    AnomalyKind.BUFFER_INTRUSION,
                    tuple(p.point_id for p in group),
                    {
                        "sensitive_area_id": area.area_id,
                        "kind": area.kind,
                        "buffer_m": area.buffer_m,
                        "min_distance_m": _r(min_d),
                        "sprayed_volume_ml": _r(volume_ml),
                        "device_span": [group[0].device_at, group[-1].device_at],
                        "note": "喷洒状态下进入敏感区域缓冲带",
                    },
                )

        # 异常剂量：喷洒区间的实测剂量超出标签范围
        for a, b in zip(line, line[1:]):
            dt_s = (parse_ts(b.device_at) - parse_ts(a.device_at)).total_seconds()
            if dt_s <= 0 or not a.spraying:
                continue
            sid = self._segment_for_interval(ev, a)
            swath = ev.task.segments[sid].plan.swath_m if sid else ev.default_swath_m
            area_mu = geo.m2_to_mu(geo.haversine_m(a.position, b.position) * swath)
            if area_mu <= 0:
                continue
            volume = a.flow_ml_per_min * (dt_s / 60.0)
            dose = volume / area_mu
            if dose < label.min_ml_per_mu or dose > label.max_ml_per_mu:
                put(
                    AnomalyKind.ABNORMAL_DOSE,
                    (a.point_id, b.point_id),
                    {
                        "dose_ml_per_mu": _r(dose),
                        "label_range_ml_per_mu": [label.min_ml_per_mu, label.max_ml_per_mu],
                        "volume_ml": _r(volume),
                        "area_mu": _r(area_mu),
                        "segment_id": sid or "未归属",
                        "device_span": [a.device_at, b.device_at],
                    },
                )

        ev.timeline_anomalies = found
