"""排程领域模型。

排程层只描述"谁在哪个时段对哪簇做什么、做多少"，不直接驱动 PCS：它产出的
是一份可执行计划，再由实时内核按当前时刻逐槽执行。时间一律用绝对 Unix
时间戳（秒），跨零点的窗口因此天然单调，重放同一份输入不会因"几点钟比大小"
而翻案。

动作只有三种：``charge``（充电，吃谷电）、``discharge``（放电，上峰电）、
``hold``（保留，簇在该时段空着，把容量让给临时调峰）。
"""

from __future__ import annotations

from dataclasses import dataclass

CHARGE = "charge"
DISCHARGE = "discharge"
HOLD = "hold"
ACTIONS = (CHARGE, DISCHARGE, HOLD)

# 排不上的原因码。排程必须说清卡在哪，而不是悄悄丢掉一条申请。
REASON_MAINTENANCE = "maintenance_window"
REASON_CURTAILMENT = "grid_curtailment"
REASON_CLUSTER_BUSY = "cluster_busy"
REASON_QUOTA = "quota_exhausted"
REASON_SOC_EMPTY = "soc_too_low_to_discharge"
REASON_SOC_FULL = "soc_too_high_to_charge"
REASON_ENERGY_FULL = "energy_window_full"
REASON_RESERVE = "reserve_protected"
REASON_NO_WINDOW = "outside_action_window"
REASON_UNKNOWN_CLUSTER = "unknown_cluster"
REASON_BAD_POWER = "power_below_minimum"


@dataclass(frozen=True)
class Slot:
    """一个离散时段：左闭右开的绝对时间区间。"""

    index: int
    start: float
    end: float

    @property
    def duration_s(self) -> float:
        return self.end - self.start

    def as_dict(self) -> dict:
        return {"index": self.index, "start": round(self.start, 3), "end": round(self.end, 3)}


@dataclass(frozen=True)
class Request:
    """一条充放电申请。

    ``window`` 给申请允许生效的绝对时间区间（跨零点就给跨过零点的区间，判定
    仍按绝对时刻）。``rack_id`` 为空表示这条申请不挑簇，由排程在公用池里挑；
    非空则是某一拨人点名要这簇。``submitted_at`` 是单子递交时刻，仅在两条申请
    的生效区间完全相同时用于先到先得的平局裁决。
    """

    request_id: str
    action: str
    energy_kwh: float
    submitted_at: float
    window_start: float
    window_end: float
    rack_id: str | None = None
    party: str = ""
    flexible: bool = True

    def overlaps(self, start: float, end: float) -> bool:
        return self.window_start < end and start < self.window_end

    def as_dict(self) -> dict:
        return {
            "request_id": self.request_id,
            "action": self.action,
            "energy_kwh": round(self.energy_kwh, 6),
            "submitted_at": round(self.submitted_at, 3),
            "window_start": round(self.window_start, 3),
            "window_end": round(self.window_end, 3),
            "rack_id": self.rack_id,
            "party": self.party,
            "flexible": self.flexible,
        }


@dataclass(frozen=True)
class Allocation:
    """一条申请落到某簇某时段的可执行分配。"""

    request_id: str
    rack_id: str
    action: str
    slot_index: int
    start: float
    end: float
    power_kw: float
    energy_kwh: float

    def as_dict(self) -> dict:
        return {
            "request_id": self.request_id,
            "rack_id": self.rack_id,
            "action": self.action,
            "slot_index": self.slot_index,
            "start": round(self.start, 3),
            "end": round(self.end, 3),
            "power_kw": round(self.power_kw, 3),
            "energy_kwh": round(self.energy_kwh, 6),
        }


@dataclass(frozen=True)
class Rejection:
    """一条申请排不进去的记录：缺多少、卡在哪一道约束。"""

    request_id: str
    reason: str
    shortfall_kwh: float
    detail: str

    def as_dict(self) -> dict:
        return {
            "request_id": self.request_id,
            "reason": self.reason,
            "shortfall_kwh": round(self.shortfall_kwh, 6),
            "detail": self.detail,
        }


@dataclass(frozen=True)
class ConstraintBreach:
    """排完后与检修／限电／额度对账时发现的一条越界。"""

    source: str
    scope: str
    detail: str
    amount: float = 0.0

    def as_dict(self) -> dict:
        return {
            "source": self.source,
            "scope": self.scope,
            "detail": self.detail,
            "amount": round(self.amount, 6),
        }
