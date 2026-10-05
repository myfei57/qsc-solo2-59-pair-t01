"""按电价窗口的簇级可执行排程。

这一层位于实时联控内核之上，回答"哪些簇在哪个时段充、哪些放、留多少给临时
调峰、排不上卡在哪"。归属默认按生效时刻（跨零点可重放），计划版本只追加、
可按生效时间或记录时间回放，排完即与检修、限电、额度三处对账。
"""

from __future__ import annotations

from .constraints import (
    CurtailmentInterval,
    CurtailmentPlan,
    MaintenancePlan,
    MaintenanceWindow,
    QuotaAllowance,
)
from .model import (
    ACTIONS,
    CHARGE,
    DISCHARGE,
    HOLD,
    Allocation,
    Request,
    Rejection,
    Slot,
)
from .ownership import DEFAULT_POLICY, EFFECTIVE, SUBMIT
from .reconcile import reconcile
from .roster import Roster, ClusterCapability, roster_from_platform, synthetic_roster
from .scheduler import Scheduler, scheduler_from_platform
from .version import PlanHistory, PlanVersion

__all__ = [
    "ACTIONS",
    "CHARGE",
    "DISCHARGE",
    "HOLD",
    "DEFAULT_POLICY",
    "EFFECTIVE",
    "SUBMIT",
    "Allocation",
    "ClusterCapability",
    "CurtailmentInterval",
    "CurtailmentPlan",
    "MaintenancePlan",
    "MaintenanceWindow",
    "PlanHistory",
    "PlanVersion",
    "QuotaAllowance",
    "Request",
    "Rejection",
    "Roster",
    "Scheduler",
    "Slot",
    "reconcile",
    "roster_from_platform",
    "scheduler_from_platform",
    "synthetic_roster",
]
