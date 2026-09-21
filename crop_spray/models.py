"""领域状态枚举与常量。"""

from __future__ import annotations

from enum import Enum

MU_M2 = 2000.0 / 3.0  # 一亩 = 666.67 平方米
COVERAGE_CELL_M = 2.0  # 覆盖面积栅格边长（米）


class LegStatus(str, Enum):
    PLANNED = "PLANNED"            # 已计划，未起飞
    AIRBORNE = "AIRBORNE"          # 已起飞，未降落
    BREACHED_AIRBORNE = "BREACHED_AIRBORNE"  # 空中遇气象越限，待返航决定
    RECALLED = "RECALLED"          # 已记录返航决定
    COMPLETED = "COMPLETED"        # 正常完成
    INVALIDATED = "INVALIDATED"    # 未起飞即自动失效

    @property
    def terminal(self) -> bool:
        return self in (LegStatus.RECALLED, LegStatus.COMPLETED, LegStatus.INVALIDATED)


class TaskStatus(str, Enum):
    OPEN = "OPEN"
    SEALED = "SEALED"


# 航迹问题分类：必须分别呈现，不得合并或静默改写
class FindingKind(str, Enum):
    BOUNDARY_EXCURSION = "BOUNDARY_EXCURSION"   # 越界片段
    BUFFER_INTRUSION = "BUFFER_INTRUSION"       # 缓冲带侵入
    TIME_REVERSAL = "TIME_REVERSAL"             # 设备时间倒退
    DOSE_ANOMALY = "DOSE_ANOMALY"               # 异常剂量
    BATCH_MISMATCH = "BATCH_MISMATCH"           # 药箱批次与处方不符
    UNKNOWN_TANK = "UNKNOWN_TANK"               # 药箱无装载记录
