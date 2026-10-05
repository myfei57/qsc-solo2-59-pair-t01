"""排程外部约束。

排程结果必须与三处现场约束对得上，约束在这里各用一个只读值对象表达，排程器
只查询、不修改：

* :class:`MaintenanceWindow`：检修计划。某簇在某个时间区间内整体不可用，
  排程器既不能给它排充、也不能给它排放；
* :class:`GridCurtailment`：限电限制。按方向给出每个时段的全站功率上限
  （千瓦）。它是全站聚合约束：同一时段所有充电视在功率之和不超过用电限额、
  所有放电视在功率之和不超过上网限额。未显式给出的时段回退到并网点额定限额；
* :class:`QuotaBudget`：充放电额度。给出本次排程视野内还可新排的充、放
  电量上限（千瓦时），把额度台账的剩余额度翻译进来，排程器不会超额度排产；
* :class:`ReservePolicy`：临时调峰余量。要求每个方向、每个时段（或每个窗口）
  保留一定比例／固定千瓦不出租，保证临时调峰指令来时还有簇可用。
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class MaintenanceWindow:
    """一簇的一个检修区间（半开 ``[start, end)``）。"""

    rack_id: str
    start: float
    end: float
    reason: str = ""

    def overlaps(self, start: float, end: float) -> bool:
        return float(self.start) < float(end) and float(start) < float(self.end)

    def as_dict(self) -> dict:
        return {
            "rack_id": self.rack_id,
            "start": round(float(self.start), 3),
            "end": round(float(self.end), 3),
            "reason": self.reason,
        }

    @classmethod
    def from_dict(cls, raw: dict) -> "MaintenanceWindow":
        return cls(
            rack_id=str(raw["rack_id"]),
            start=float(raw["start"]),
            end=float(raw["end"]),
            reason=str(raw.get("reason", "")),
        )


@dataclass(frozen=True)
class GridCurtailment:
    """全站限电：按方向给出每个时段索引的功率上限（千瓦）。

    ``charge_limits``／``discharge_limits`` 以时段索引为键；查不到的时段回退到
    对应的并网点额定限额 ``default_import_kw``／``default_export_kw``。
    """

    default_import_kw: float
    default_export_kw: float
    charge_limits: dict[int, float] = field(default_factory=dict)
    discharge_limits: dict[int, float] = field(default_factory=dict)

    def cap_kw(self, direction: str, slot_index: int) -> float:
        """返回某方向某时段的全站功率上限。"""

        if direction == "charge":
            return max(0.0, float(self.charge_limits.get(slot_index, self.default_import_kw)))
        if direction == "discharge":
            return max(0.0, float(self.discharge_limits.get(slot_index, self.default_export_kw)))
        raise ValueError(f"未知方向: {direction}")

    def as_dict(self) -> dict:
        return {
            "default_import_kw": round(float(self.default_import_kw), 3),
            "default_export_kw": round(float(self.default_export_kw), 3),
            "charge_limits": {str(k): round(v, 3) for k, v in sorted(self.charge_limits.items())},
            "discharge_limits": {str(k): round(v, 3) for k, v in sorted(self.discharge_limits.items())},
        }

    @classmethod
    def from_dict(cls, raw: dict) -> "GridCurtailment":
        def parse_limits(mapping: dict) -> dict[int, float]:
            return {int(key): float(value) for key, value in dict(mapping).items()}

        return cls(
            default_import_kw=float(raw.get("default_import_kw", 0.0)),
            default_export_kw=float(raw.get("default_export_kw", 0.0)),
            charge_limits=parse_limits(raw.get("charge_limits", {})),
            discharge_limits=parse_limits(raw.get("discharge_limits", {})),
        )


@dataclass(frozen=True)
class QuotaBudget:
    """本次排程视野内可新排的充、放电量额度（千瓦时）。"""

    charge_kwh: float
    discharge_kwh: float

    def cap_for(self, direction: str) -> float:
        if direction == "charge":
            return max(0.0, float(self.charge_kwh))
        if direction == "discharge":
            return max(0.0, float(self.discharge_kwh))
        raise ValueError(f"未知方向: {direction}")

    def as_dict(self) -> dict:
        return {"charge_kwh": round(float(self.charge_kwh), 6), "discharge_kwh": round(float(self.discharge_kwh), 6)}

    @classmethod
    def from_dict(cls, raw: dict) -> "QuotaBudget":
        return cls(
            charge_kwh=float(raw.get("charge_kwh", 0.0)),
            discharge_kwh=float(raw.get("discharge_kwh", 0.0)),
        )


@dataclass(frozen=True)
class ReservePolicy:
    """临时调峰余量策略。

    ``charge_reserve_ratio``／``discharge_reserve_ratio`` 是每个时段对应方向
    全站可排功率必须留出的余量比例（0–1）；``charge_reserve_kw`` 等是额外
    固定千瓦余量。两者叠加：可出租功率 = 限额 ×(1−比例) − 固定值，夹到非负。
    """

    charge_reserve_ratio: float = 0.1
    discharge_reserve_ratio: float = 0.1
    charge_reserve_kw: float = 0.0
    discharge_reserve_kw: float = 0.0

    def __post_init__(self) -> None:
        for name, value in (
            ("charge_reserve_ratio", self.charge_reserve_ratio),
            ("discharge_reserve_ratio", self.discharge_reserve_ratio),
        ):
            if not 0.0 <= float(value) <= 1.0:
                raise ValueError(f"{name} 必须落在 0 到 1 之间")
        for name, value in (
            ("charge_reserve_kw", self.charge_reserve_kw),
            ("discharge_reserve_kw", self.discharge_reserve_kw),
        ):
            if float(value) < 0:
                raise ValueError(f"{name} 不能为负")

    def reservable_kw(self, direction: str, cap_kw: float) -> float:
        """给定时段全站限额，返回扣除调峰余量后还可出租的功率上限。"""

        cap = max(0.0, float(cap_kw))
        if direction == "charge":
            ratio, fixed = self.charge_reserve_ratio, self.charge_reserve_kw
        elif direction == "discharge":
            ratio, fixed = self.discharge_reserve_ratio, self.discharge_reserve_kw
        else:
            raise ValueError(f"未知方向: {direction}")
        return max(0.0, cap * (1.0 - float(ratio)) - float(fixed))

    def reserve_kw(self, direction: str, cap_kw: float) -> float:
        """返回该时段被保留、不参与排产的功率余量。"""

        return round(max(0.0, float(cap_kw)) - self.reservable_kw(direction, cap_kw), 3)

    def as_dict(self) -> dict:
        return {
            "charge_reserve_ratio": float(self.charge_reserve_ratio),
            "discharge_reserve_ratio": float(self.discharge_reserve_ratio),
            "charge_reserve_kw": round(float(self.charge_reserve_kw), 3),
            "discharge_reserve_kw": round(float(self.discharge_reserve_kw), 3),
        }

    @classmethod
    def from_dict(cls, raw: dict) -> "ReservePolicy":
        return cls(
            charge_reserve_ratio=float(raw.get("charge_reserve_ratio", 0.1)),
            discharge_reserve_ratio=float(raw.get("discharge_reserve_ratio", 0.1)),
            charge_reserve_kw=float(raw.get("charge_reserve_kw", 0.0)),
            discharge_reserve_kw=float(raw.get("discharge_reserve_kw", 0.0)),
        )
