"""航段执行。

任务由已审签的处方派生，拆成若干航段。气象（风速/降雨）越过作业条件时：
- 未起飞航段自动失效（VOIDED），原因留痕；
- 已起飞航段记录返航决定（ReturnDecision），航段标记 RETURNED。
补飞任务必须指向原任务并承接其失效航段，证据链与原任务分开。
"""

from __future__ import annotations

from .models import (
    OperatingLimits,
    ReturnDecision,
    SegmentPlan,
    SegmentState,
    SegmentStatus,
    TaskPlan,
    WeatherEvent,
)
from .prescription import PrescriptionService


class ExecutionError(ValueError):
    pass


class ExecutionEngine:
    def __init__(self, prescriptions: PrescriptionService):
        self._prescriptions = prescriptions
        self._tasks: dict[str, TaskPlan] = {}

    # ------------------------------------------------------------ 任务建立

    def create_task(
        self,
        task_id: str,
        prescription_id: str,
        segments: list[SegmentPlan],
        limits: OperatingLimits,
        created_at: str,
    ) -> TaskPlan:
        rx = self._prescriptions.require_signed(prescription_id)
        if task_id in self._tasks:
            raise ExecutionError(f"任务 {task_id} 已存在")
        if not segments:
            raise ExecutionError("任务至少需要一个航段")
        task = TaskPlan(
            task_id=task_id,
            prescription_id=prescription_id,
            field_id=rx.field_id,
            segments={s.segment_id: SegmentState(plan=s) for s in segments},
            limits=limits,
            created_at=created_at,
        )
        self._tasks[task_id] = task
        return task

    def create_reflight(
        self,
        task_id: str,
        parent_task_id: str,
        segments: list[SegmentPlan],
        created_at: str,
    ) -> TaskPlan:
        """为原任务的失效航段建立补飞任务。

        补飞沿用原任务的处方与作业条件，但任务编号独立，
        航迹、摘要各自归档，与原任务证据链清晰分开。
        """
        parent = self.require_task(parent_task_id)
        voided = {
            sid for sid, st in parent.segments.items() if st.status is SegmentStatus.VOIDED
        }
        if not voided:
            raise ExecutionError(f"原任务 {parent_task_id} 没有失效航段，不能补飞")
        task = self.create_task(
            task_id=task_id,
            prescription_id=parent.prescription_id,
            segments=segments,
            limits=parent.limits,
            created_at=created_at,
        )
        task.parent_task_id = parent_task_id
        task.covers_segment_ids = tuple(sorted(voided))
        return task

    # ------------------------------------------------------------ 航段生命周期

    def start_segment(self, task_id: str, segment_id: str, at: str) -> None:
        state = self._segment(task_id, segment_id)
        if state.status is not SegmentStatus.PENDING:
            raise ExecutionError(f"航段 {segment_id} 当前状态 {state.status} 不能起飞")
        state.status = SegmentStatus.ACTIVE
        state.started_at = at

    def complete_segment(self, task_id: str, segment_id: str, at: str) -> None:
        state = self._segment(task_id, segment_id)
        if state.status is not SegmentStatus.ACTIVE:
            raise ExecutionError(f"航段 {segment_id} 当前状态 {state.status} 不能完成")
        state.status = SegmentStatus.COMPLETED
        state.ended_at = at

    # ------------------------------------------------------------ 气象闸门

    def apply_weather(self, task_id: str, event: WeatherEvent) -> list[str]:
        """气象越限：未起飞航段自动失效，已起飞航段记录返航决定。返回处置说明。"""
        task = self.require_task(task_id)
        breaches = []
        if event.wind_m_s > task.limits.max_wind_m_s:
            breaches.append(f"风速 {event.wind_m_s} m/s 超过上限 {task.limits.max_wind_m_s} m/s")
        if event.rain_mm_per_h > task.limits.max_rain_mm_per_h:
            breaches.append(
                f"降雨 {event.rain_mm_per_h} mm/h 超过上限 {task.limits.max_rain_mm_per_h} mm/h"
            )
        if not breaches:
            return []
        reason = "；".join(breaches)
        actions: list[str] = []
        for sid, state in task.segments.items():
            if state.status is SegmentStatus.PENDING:
                state.status = SegmentStatus.VOIDED
                state.void_reason = reason
                state.ended_at = event.observed_at
                actions.append(f"航段 {sid} 未起飞，自动失效：{reason}")
            elif state.status is SegmentStatus.ACTIVE:
                state.status = SegmentStatus.RETURNED
                state.ended_at = event.observed_at
                task.return_decisions.append(
                    ReturnDecision(
                        segment_id=sid,
                        decided_at=event.observed_at,
                        reason=reason,
                        wind_m_s=event.wind_m_s,
                        rain_mm_per_h=event.rain_mm_per_h,
                    )
                )
                actions.append(f"航段 {sid} 已起飞，记录返航决定：{reason}")
        return actions

    # ------------------------------------------------------------ 查询

    def require_task(self, task_id: str) -> TaskPlan:
        task = self._tasks.get(task_id)
        if task is None:
            raise ExecutionError(f"任务 {task_id} 不存在")
        return task

    def tasks_for_field(self, field_id: str) -> list[TaskPlan]:
        return [t for t in self._tasks.values() if t.field_id == field_id]

    def _segment(self, task_id: str, segment_id: str) -> SegmentState:
        task = self.require_task(task_id)
        state = task.segments.get(segment_id)
        if state is None:
            raise ExecutionError(f"任务 {task_id} 没有航段 {segment_id}")
        return state
