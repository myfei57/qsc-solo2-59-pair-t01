"""排程约束台账：检修、限电、充放电额度。

三处外部约束在这里用同一份时间口径（绝对时间戳）描述，排程引擎与排后对账
都只读这一份，避免"排的时候看的是一张表、对的时候看的是另一张表"。

- **检修计划**：某簇在某区间内整体不可用，充放都不行；
- **限电限制**：按时间区间给全站充（用电）／放（上网）功率上限，区间外回落到
  并网点的铭牌限额；
- **充放电额度**：按记账周期给周期内充／放电量上限，跨周期不串账，周期长度
  与实时配额台账 :class:`batterystore.quota.period.PeriodTracker` 同口径。
"""

from __future__ import annotations

from dataclasses import dataclass

DAY_S = 86400.0


@dataclass(frozen=True)
class MaintenanceWindow:
    """一簇的一段检修停用区间。"""

    rack_id: str
    start: float
    end: float
    reason: str = ""

    def covers(self, slot_start: float, slot_end: float) -> bool:
        return self.start < slot_end and slot_start < self.end

    def as_dict(self) -> dict:
        return {
            "rack_id": self.rack_id,
            "start": round(self.start, 3),
            "end": round(self.end, 3),
            "reason": self.reason,
        }


class MaintenancePlan:
    """检修计划。"""

    def __init__(self, windows: list[MaintenanceWindow] | None = None) -> None:
        self._windows = list(windows or [])

    def blocked(self, rack_id: str, slot_start: float, slot_end: float) -> MaintenanceWindow | None:
        """返回让该簇在此时段不可用的检修窗口，没有则 ``None``。"""

        for window in self._windows:
            if window.rack_id == rack_id and window.covers(slot_start, slot_end):
                return window
        return None

    def as_dict(self) -> dict:
        return {"windows": [window.as_dict() for window in self._windows]}


@dataclass(frozen=True)
class CurtailmentInterval:
    """一段全站功率限制。"""

    start: float
    end: float
    export_limit_kw: float
    import_limit_kw: float

    def covers(self, ts: float) -> bool:
        return self.start <= ts < self.end

    def as_dict(self) -> dict:
        return {
            "start": round(self.start, 3),
            "end": round(self.end, 3),
            "export_limit_kw": round(self.export_limit_kw, 3),
            "import_limit_kw": round(self.import_limit_kw, 3),
        }


class CurtailmentPlan:
    """限电曲线：特殊区间覆盖铭牌限额。"""

    def __init__(
        self,
        intervals: list[CurtailmentInterval] | None = None,
        *,
        default_export_kw: float,
        default_import_kw: float,
    ) -> None:
        self._intervals = list(intervals or [])
        self._default_export = float(default_export_kw)
        self._default_import = float(default_import_kw)

    def limits_at(self, ts: float) -> tuple[float, float]:
        """返回某时刻的（上网限额，用电限额）。"""

        for interval in self._intervals:
            if interval.covers(ts):
                return interval.export_limit_kw, interval.import_limit_kw
        return self._default_export, self._default_import

    def as_dict(self) -> dict:
        return {
            "default_export_kw": round(self._default_export, 3),
            "default_import_kw": round(self._default_import, 3),
            "intervals": [interval.as_dict() for interval in self._intervals],
        }


class QuotaAllowance:
    """按记账周期的充放电额度（kWh），跨周期不串账。"""

    def __init__(self, period_hours: float, charge_kwh: float, discharge_kwh: float) -> None:
        if period_hours <= 0:
            raise ValueError("记账周期必须为正")
        self._period_s = float(period_hours) * 3600.0
        self._charge_cap = float(charge_kwh)
        self._discharge_cap = float(discharge_kwh)
        self._used_charge: dict[str, float] = {}
        self._used_discharge: dict[str, float] = {}

    def period_key(self, ts: float) -> str:
        index = int(float(ts) // self._period_s)
        return f"p{index:06d}"

    def remaining(self, action: str, ts: float) -> float:
        key = self.period_key(ts)
        if action == "charge":
            return max(0.0, self._charge_cap - self._used_charge.get(key, 0.0))
        return max(0.0, self._discharge_cap - self._used_discharge.get(key, 0.0))

    def capacity(self, action: str) -> float:
        """返回该动作的单周期电量上限。"""

        return self._charge_cap if action == "charge" else self._discharge_cap

    def consume(self, action: str, ts: float, energy_kwh: float) -> float:
        """登记一笔用量，返回实际登记量（不超过剩余额度）。"""

        key = self.period_key(ts)
        bucket = self._used_charge if action == "charge" else self._used_discharge
        cap = self._charge_cap if action == "charge" else self._discharge_cap
        allowed = max(0.0, min(float(energy_kwh), cap - bucket.get(key, 0.0)))
        bucket[key] = bucket.get(key, 0.0) + allowed
        return allowed

    def usage(self) -> dict:
        return {
            "charge": {key: round(value, 6) for key, value in sorted(self._used_charge.items())},
            "discharge": {key: round(value, 6) for key, value in sorted(self._used_discharge.items())},
        }

    def as_dict(self) -> dict:
        return {
            "period_hours": round(self._period_s / 3600.0, 4),
            "charge_cap_kwh": round(self._charge_cap, 3),
            "discharge_cap_kwh": round(self._discharge_cap, 3),
            "used": self.usage(),
        }
