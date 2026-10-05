"""簇级充放排程。

排程子系统回答四个问题：哪些簇在哪个时段充、哪些放、给临时调峰留多少余量、
排不上的卡在哪。它是纯计划层：读现场能力快照与外部约束（检修、限电、额度），
产出可执行的簇级方案与对账报告，并把每一版方案只追加地落盘，旧方案可按时间
回放、按落盘输入重算。

对外主入口：

* :class:`~batterystore.schedule.planner.Planner`：纯排程计算；
* :class:`~batterystore.schedule.service.Scheduler`：平台侧服务（收单、对账、
  版本落盘、回放重算）；
* :func:`~batterystore.schedule.models.preferred_policy`：默认冲突归属口径。
"""

from __future__ import annotations

from .models import (
    CHARGE,
    DISCHARGE,
    IDLE,
    PARTIAL,
    POLICIES,
    POLICY_EFFECTIVE,
    POLICY_SUBMIT,
    REJECTED,
    SCHEDULED,
    Allocation,
    Bid,
    ClusterContext,
    Rejection,
    ScheduleResult,
    Slot,
    preferred_policy,
)

__all__ = [
    "CHARGE",
    "DISCHARGE",
    "IDLE",
    "PARTIAL",
    "POLICIES",
    "POLICY_EFFECTIVE",
    "POLICY_SUBMIT",
    "REJECTED",
    "SCHEDULED",
    "Allocation",
    "Bid",
    "ClusterContext",
    "Rejection",
    "ScheduleResult",
    "Slot",
    "preferred_policy",
]
