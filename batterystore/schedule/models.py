"""排程领域模型。

排程的输入与输出全部用不可变值对象表达：申请单（谁要、要多少、何时提交）、
时段（谷充峰放的连续半小时格）、分配（哪一簇在该时段充还是放）与决策（排上、
部分排上、没排上并写明卡点）。这些对象只携带数据与只读换算，不持有任何运行
状态，因此可以安全落盘、跨重启回放，也能被历史版本原样还原。

冲突归属（同一簇被两条计划同时盯上）有两种口径，用 :data:`POLICIES` 收口：

* ``submit``：按申请单提交时刻排先后。跨零点的窗口在上一天就开放，值班员
  提前一天提交的单子应当先占位，提交时刻不随运营日零点回退，结果稳定可复算。
* ``effective``：按申请单在窗口内的生效时刻排先后。两条单子只在窗口起点
  （可能已跨过零点）才开始比较，后开放的运营日里临时插入的单子可能压过
  前一天就提交的单子。

默认采用 ``submit``，理由见 :func:`preferred_policy`。
"""

from __future__ import annotations

from dataclasses import dataclass, field

CHARGE = "charge"
DISCHARGE = "discharge"
IDLE = "idle"
DIRECTIONS = (CHARGE, DISCHARGE)

SCHEDULED = "scheduled"
PARTIAL = "partial"
REJECTED = "rejected"

POLICY_SUBMIT = "submit"
POLICY_EFFECTIVE = "effective"
POLICIES = (POLICY_SUBMIT, POLICY_EFFECTIVE)


def preferred_policy() -> str:
    """返回默认冲突归属口径。

    选 ``submit``（按提交时刻）有三条工程理由：

    1. **跨零点稳定**：谷价窗口常在上一天夜里开始、跨过运营日零点。提交时刻
       是墙上时刻，单调递增、不随零点回退；生效时刻以窗口起点为基准，跨零点
       时同一条单子在两个运营日里算出的归属会不同（见
       ``tests/test_schedule.py`` 的跨零点用例）。
    2. **公平且可追责**：先报先得，值班员无法靠把单子的生效时刻改到窗口起点
       来抢占已经排定的簇；提交记录由审计流水固定，事后能复核。
    3. **与回放一致**：重放历史排程时，提交时刻已被流水固定，重算结果与当年
       一致；生效口径依赖"当时窗口怎么切"，回放时容易因窗口定义漂移而失真。

    需要"当天临时调峰优先于隔夜预留单"的站点可显式切换到 ``effective``，
    排程器会把所用口径写进版本头，历史方案按当时口径回放。
    """

    return POLICY_SUBMIT


@dataclass(frozen=True)
class Slot:
    """一个排程时段（半小时格），半开区间 ``[start, end)``。"""

    index: int
    start: float
    end: float
    kind: str

    @property
    def duration_s(self) -> float:
        return self.end - self.start

    @property
    def duration_h(self) -> float:
        return self.duration_s / 3600.0

    def as_dict(self) -> dict:
        return {
            "index": self.index,
            "start": round(self.start, 3),
            "end": round(self.end, 3),
            "duration_s": round(self.duration_s, 3),
            "kind": self.kind,
        }

    @classmethod
    def from_dict(cls, raw: dict) -> "Slot":
        return cls(
            index=int(raw["index"]),
            start=float(raw["start"]),
            end=float(raw["end"]),
            kind=str(raw["kind"]),
        )


@dataclass(frozen=True)
class Bid:
    """一条充／放申请单。

    ``submitted_at`` 是单子进系统的墙上时刻；``effective_at`` 是请求方希望
    单子开始生效的时刻（缺省取窗口起点）。两者分离正是跨零点归属差异的来源。
    """

    bid_id: str
    direction: str
    energy_kwh: float
    submitted_at: float
    effective_at: float | None = None
    power_kw: float | None = None
    owner: str = ""
    note: str = ""

    def __post_init__(self) -> None:
        if self.direction not in DIRECTIONS:
            raise ValueError(f"方向必须是 {DIRECTIONS} 之一")
        if float(self.energy_kwh) < 0:
            raise ValueError("申请电量不能为负")
        if float(self.submitted_at) < 0:
            raise ValueError("提交时刻不能为负")

    def effective_or(self, window_start: float) -> float:
        """返回生效时刻，未显式给出时取窗口起点。"""

        return float(self.effective_at) if self.effective_at is not None else float(window_start)

    def as_dict(self) -> dict:
        return {
            "bid_id": self.bid_id,
            "direction": self.direction,
            "energy_kwh": round(float(self.energy_kwh), 6),
            "power_kw": round(float(self.power_kw), 6) if self.power_kw is not None else None,
            "submitted_at": round(float(self.submitted_at), 3),
            "effective_at": round(float(self.effective_at), 3) if self.effective_at is not None else None,
            "owner": self.owner,
            "note": self.note,
        }

    @classmethod
    def from_dict(cls, raw: dict) -> "Bid":
        effective = raw.get("effective_at")
        power = raw.get("power_kw")
        return cls(
            bid_id=str(raw["bid_id"]),
            direction=str(raw["direction"]),
            energy_kwh=float(raw["energy_kwh"]),
            submitted_at=float(raw["submitted_at"]),
            effective_at=float(effective) if effective is not None else None,
            power_kw=float(power) if power is not None else None,
            owner=str(raw.get("owner", "")),
            note=str(raw.get("note", "")),
        )


@dataclass(frozen=True)
class ClusterContext:
    """一簇参与排程所需的全部能力与边界。

    排程只做计划，不直接动作设备，因此这里是能力快照而不是设备引用：可充上限、
    可放下限（SOC 百分点）、额定充放功率、当前 SOC 与检修／故障状态。并网点
    限电是**全站聚合**约束（同时充电的各簇功率之和不超过用电限额），不在簇级
    表达，而由排程器在分配时统一扣减，见 :class:`batterystore.schedule.planner.Planner`。
    """

    rack_id: str
    soc_pct: float
    soc_min_pct: float
    soc_max_pct: float
    rated_energy_kwh: float
    max_power_kw: float
    fault: bool
    maintained: bool

    @property
    def available(self) -> bool:
        """该簇整体是否可排（非检修、无故障、有可用容量窗口）。"""

        return (not self.maintained) and (not self.fault) and self.soc_min_pct < self.soc_max_pct

    def charge_room_kwh(self) -> float:
        """从当前 SOC 充到允许上限还能进多少电。"""

        room_pct = max(0.0, self.soc_max_pct - self.soc_pct)
        return self.rated_energy_kwh * room_pct / 100.0

    def discharge_room_kwh(self) -> float:
        """从当前 SOC 放到允许下限还能出多少电。"""

        room_pct = max(0.0, self.soc_pct - self.soc_min_pct)
        return self.rated_energy_kwh * room_pct / 100.0

    def power_cap_kw(self, direction: str) -> float:
        """返回该方向的设备功率上限；检修置零。并网点限额在排程器统一扣减。"""

        if self.maintained:
            return 0.0
        return max(0.0, float(self.max_power_kw))

    def as_dict(self) -> dict:
        return {
            "rack_id": self.rack_id,
            "soc_pct": round(self.soc_pct, 3),
            "soc_min_pct": round(self.soc_min_pct, 3),
            "soc_max_pct": round(self.soc_max_pct, 3),
            "rated_energy_kwh": round(self.rated_energy_kwh, 6),
            "max_power_kw": round(self.max_power_kw, 3),
            "fault": self.fault,
            "maintained": self.maintained,
            "charge_room_kwh": round(self.charge_room_kwh(), 6),
            "discharge_room_kwh": round(self.discharge_room_kwh(), 6),
            "available": self.available,
        }

    @classmethod
    def from_dict(cls, raw: dict) -> "ClusterContext":
        return cls(
            rack_id=str(raw["rack_id"]),
            soc_pct=float(raw["soc_pct"]),
            soc_min_pct=float(raw["soc_min_pct"]),
            soc_max_pct=float(raw["soc_max_pct"]),
            rated_energy_kwh=float(raw["rated_energy_kwh"]),
            max_power_kw=float(raw["max_power_kw"]),
            fault=bool(raw.get("fault", False)),
            maintained=bool(raw.get("maintained", False)),
        )


@dataclass(frozen=True)
class Allocation:
    """一簇在一个时段上的排程分配。"""

    slot_index: int
    rack_id: str
    direction: str
    power_kw: float
    energy_kwh: float
    bid_id: str

    def as_dict(self) -> dict:
        return {
            "slot_index": self.slot_index,
            "rack_id": self.rack_id,
            "direction": self.direction,
            "power_kw": round(float(self.power_kw), 3),
            "energy_kwh": round(float(self.energy_kwh), 6),
            "bid_id": self.bid_id,
        }

    @classmethod
    def from_dict(cls, raw: dict) -> "Allocation":
        return cls(
            slot_index=int(raw["slot_index"]),
            rack_id=str(raw["rack_id"]),
            direction=str(raw["direction"]),
            power_kw=float(raw["power_kw"]),
            energy_kwh=float(raw["energy_kwh"]),
            bid_id=str(raw["bid_id"]),
        )


@dataclass(frozen=True)
class Rejection:
    """一条申请没排上（或没排满）的卡点说明。"""

    bid_id: str
    status: str
    requested_kwh: float
    scheduled_kwh: float
    reasons: tuple[str, ...] = field(default_factory=tuple)

    @property
    def short_kwh(self) -> float:
        return round(self.requested_kwh - self.scheduled_kwh, 6)

    def as_dict(self) -> dict:
        return {
            "bid_id": self.bid_id,
            "status": self.status,
            "requested_kwh": round(float(self.requested_kwh), 6),
            "scheduled_kwh": round(float(self.scheduled_kwh), 6),
            "short_kwh": self.short_kwh,
            "reasons": list(self.reasons),
        }

    @classmethod
    def from_dict(cls, raw: dict) -> "Rejection":
        return cls(
            bid_id=str(raw["bid_id"]),
            status=str(raw["status"]),
            requested_kwh=float(raw["requested_kwh"]),
            scheduled_kwh=float(raw["scheduled_kwh"]),
            reasons=tuple(raw.get("reasons", ())),
        )


@dataclass(frozen=True)
class ScheduleResult:
    """一次排程的完整结果。"""

    plan_date: str
    version: int
    policy: str
    horizon_start: float
    horizon_end: float
    slots: tuple[Slot, ...]
    clusters: tuple[ClusterContext, ...]
    decisions: tuple[Allocation, ...]
    rejections: tuple[Rejection, ...]
    reserve: dict
    generated_at: float

    def bid_scheduled_kwh(self, bid_id: str) -> float:
        return round(sum(item.energy_kwh for item in self.decisions if item.bid_id == bid_id), 6)

    def as_dict(self) -> dict:
        return {
            "plan_date": self.plan_date,
            "version": self.version,
            "policy": self.policy,
            "horizon_start": round(self.horizon_start, 3),
            "horizon_end": round(self.horizon_end, 3),
            "generated_at": round(self.generated_at, 3),
            "slots": [slot.as_dict() for slot in self.slots],
            "clusters": [cluster.as_dict() for cluster in self.clusters],
            "decisions": [item.as_dict() for item in self.decisions],
            "rejections": [item.as_dict() for item in self.rejections],
            "reserve": dict(self.reserve),
            "totals": {
                "charge_kwh": round(sum(i.energy_kwh for i in self.decisions if i.direction == CHARGE), 6),
                "discharge_kwh": round(sum(i.energy_kwh for i in self.decisions if i.direction == DISCHARGE), 6),
            },
        }

    @classmethod
    def from_dict(cls, raw: dict) -> "ScheduleResult":
        return cls(
            plan_date=str(raw["plan_date"]),
            version=int(raw["version"]),
            policy=str(raw["policy"]),
            horizon_start=float(raw["horizon_start"]),
            horizon_end=float(raw["horizon_end"]),
            slots=tuple(Slot.from_dict(item) for item in raw.get("slots", ())),
            clusters=tuple(ClusterContext.from_dict(item) for item in raw.get("clusters", ())),
            decisions=tuple(Allocation.from_dict(item) for item in raw.get("decisions", ())),
            rejections=tuple(Rejection.from_dict(item) for item in raw.get("rejections", ())),
            reserve=dict(raw.get("reserve", {})),
            generated_at=float(raw.get("generated_at", 0.0)),
        )
