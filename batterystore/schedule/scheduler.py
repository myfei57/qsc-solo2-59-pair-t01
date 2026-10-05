"""排程服务：站级可执行计划的生成、修订与回放。

``Scheduler`` 把名册、三道约束、归属策略、排程引擎、排后对账与版本史缝成一条
链。对外只暴露三件事：

- :meth:`generate_day_ahead`：生成一份可执行计划（可选地自动做谷充峰放），排不
  上的申请带原因返回，计划与检修／限电／额度对账后落一个不可变版本；
- :meth:`revise`：在现有约束上重算并生成下一版，旧版保留可回放；
- :meth:`replay`：按生效时间或记录时间回放历史版本。
"""

from __future__ import annotations

from ..ids import IdFactory
from . import ownership, windows
from .constraints import CurtailmentPlan, MaintenancePlan, QuotaAllowance
from .engine import solve
from .model import Request
from .reconcile import reconcile
from .roster import Roster, roster_from_platform
from .snapshot import ScheduleSnapshot
from .version import PLAN_KIND_DAY_AHEAD, PLAN_KIND_REVISION, PlanHistory, PlanVersion


class Scheduler:
    """站级排程服务。"""

    def __init__(
        self,
        roster: Roster,
        *,
        storage,
        quota_period_hours: float = 24.0,
        charge_quota_kwh: float = 2000.0,
        discharge_quota_kwh: float = 2000.0,
        default_export_kw: float = 400.0,
        default_import_kw: float = 450.0,
        policy: str = ownership.DEFAULT_POLICY,
        plan_prefix: str = "plan",
    ) -> None:
        self._roster = roster
        self._storage = storage
        self._quota_period_hours = float(quota_period_hours)
        self._charge_quota_kwh = float(charge_quota_kwh)
        self._discharge_quota_kwh = float(discharge_quota_kwh)
        self._default_export_kw = float(default_export_kw)
        self._default_import_kw = float(default_import_kw)
        self._policy = ownership.normalize(policy)
        self._ids = IdFactory(plan_prefix)
        self.history = PlanHistory(storage)

    @property
    def roster(self) -> Roster:
        return self._roster

    def replace_roster(self, roster: Roster) -> Roster:
        """用最新实时簇状态替换名册（每次生成前刷新 SOC／保留线）。"""

        self._roster = roster
        return self._roster

    @property
    def policy(self) -> str:
        return self._policy

    def set_policy(self, policy: str) -> str:
        """切换归属策略（默认生效时刻），便于并排对比两种做法。"""

        self._policy = ownership.normalize(policy)
        return self._policy

    def _fresh_quota(self) -> QuotaAllowance:
        return QuotaAllowance(self._quota_period_hours, self._charge_quota_kwh, self._discharge_quota_kwh)

    def generate_day_ahead(
        self,
        *,
        day_start: float,
        requests: list[Request] | None = None,
        slot_s: float = 3600.0,
        maintenance: MaintenancePlan | None = None,
        curtailment: CurtailmentPlan | None = None,
        recorded_at: float | None = None,
        auto_charge: bool = True,
        auto_discharge: bool = True,
        auto_target_soc_pct: float = 95.0,
        note: str = "",
    ) -> ScheduleSnapshot:
        """生成一份日前可执行计划并落版本。"""

        start = windows.midnight_of(day_start)
        end = start + windows.DAY_S
        return self._build(
            start=start,
            end=end,
            slot_s=slot_s,
            requests=list(requests or []),
            maintenance=maintenance or MaintenancePlan(),
            curtailment=curtailment
            or CurtailmentPlan(
                default_export_kw=self._default_export_kw,
                default_import_kw=self._default_import_kw,
            ),
            recorded_at=start if recorded_at is None else recorded_at,
            kind=PLAN_KIND_DAY_AHEAD,
            auto_charge=auto_charge,
            auto_discharge=auto_discharge,
            auto_target_soc_pct=auto_target_soc_pct,
            note=note,
        )

    def revise(
        self,
        *,
        requests: list[Request] | None = None,
        maintenance: MaintenancePlan | None = None,
        curtailment: CurtailmentPlan | None = None,
        start: float,
        end: float,
        slot_s: float = 3600.0,
        recorded_at: float,
        auto_charge: bool = False,
        auto_discharge: bool = False,
        auto_target_soc_pct: float = 95.0,
        note: str = "",
    ) -> ScheduleSnapshot:
        """按新的申请／约束重算区间，生成一个修订版本（旧版保留）。"""

        return self._build(
            start=float(start),
            end=float(end),
            slot_s=slot_s,
            requests=list(requests or []),
            maintenance=maintenance or MaintenancePlan(),
            curtailment=curtailment
            or CurtailmentPlan(
                default_export_kw=self._default_export_kw,
                default_import_kw=self._default_import_kw,
            ),
            recorded_at=float(recorded_at),
            kind=PLAN_KIND_REVISION,
            auto_charge=auto_charge,
            auto_discharge=auto_discharge,
            auto_target_soc_pct=auto_target_soc_pct,
            note=note,
        )

    def _build(
        self,
        *,
        start: float,
        end: float,
        slot_s: float,
        requests: list[Request],
        maintenance: MaintenancePlan,
        curtailment: CurtailmentPlan,
        recorded_at: float,
        kind: str,
        auto_charge: bool,
        auto_discharge: bool,
        auto_target_soc_pct: float,
        note: str,
    ) -> ScheduleSnapshot:
        slots = windows.build_slots(start, end, slot_s)
        tariff_windows = [window.as_dict() for window in windows.windows_for_range(start, end)]
        quota = self._fresh_quota()
        result = solve(
            roster=self._roster,
            slots=slots,
            requests=requests,
            maintenance=maintenance,
            curtailment=curtailment,
            quota=quota,
            tariff_of_slot=windows.slot_tariff,
            policy=self._policy,
            auto_charge=auto_charge,
            auto_discharge=auto_discharge,
            auto_target_soc_pct=auto_target_soc_pct,
        )
        allocations = [item.as_dict() for item in result["allocations"]]
        reconciliation = reconcile(
            allocations=result["allocations"],
            roster=self._roster,
            slots=slots,
            maintenance=maintenance,
            curtailment=curtailment,
            quota_policy=quota,
        )
        rejections = [item.as_dict() for item in result["rejections"]]
        candidate = PlanVersion(
            version=0,
            plan_id=self._ids.next(),
            kind=kind,
            parent_version=self.history.latest_version_number(),
            recorded_at=recorded_at,
            effective_start=start,
            effective_end=end,
            policy=self._policy,
            horizon={
                "start": round(start, 3),
                "end": round(end, 3),
                "slot_s": float(slot_s),
                "slots": len(slots),
                "tariff_windows": tariff_windows,
            },
            allocations=tuple(allocations),
            rejections=tuple(rejections),
            holds=tuple(result["holds"]),
            reserve=tuple(result["reserve"]),
            reconciliation=reconciliation,
            note=note,
        )
        stored = self.history.append(candidate)
        return self._to_snapshot(stored)

    def _to_snapshot(self, version: PlanVersion) -> ScheduleSnapshot:
        charge = sum(1 for item in version.allocations if item["action"] == "charge")
        discharge = sum(1 for item in version.allocations if item["action"] == "discharge")
        return ScheduleSnapshot(
            plan_id=version.plan_id,
            version=version.version,
            kind=version.kind,
            policy=ownership.describe(version.policy),
            horizon=version.horizon,
            counts={
                "clusters": self._roster.count(),
                "allocations": len(version.allocations),
                "charge_slots": charge,
                "discharge_slots": discharge,
                "rejections": len(version.rejections),
                "reconciled": bool(version.reconciliation.get("ok")),
            },
            allocations=list(version.allocations),
            rejections=list(version.rejections),
            reserve=list(version.reserve),
            reconciliation=version.reconciliation,
        )

    def latest(self) -> dict | None:
        version = self.history.latest()
        return self._to_snapshot(version).as_dict() if version else None

    def replay_version(self, version_number: int) -> dict | None:
        version = self.history.get(version_number)
        return self._to_snapshot(version).as_dict() if version else None

    def replay_effective(self, ts: float) -> dict | None:
        version = self.history.as_of_effective(ts)
        return self._to_snapshot(version).as_dict() if version else None

    def replay_recorded(self, ts: float) -> list[dict]:
        return [self._to_snapshot(version).as_dict() for version in self.history.recorded_before(ts)]

    def versions_index(self) -> list[dict]:
        return [
            {
                "version": item.version,
                "plan_id": item.plan_id,
                "kind": item.kind,
                "parent_version": item.parent_version,
                "recorded_at": round(item.recorded_at, 3),
                "effective_start": round(item.effective_start, 3),
                "effective_end": round(item.effective_end, 3),
                "policy": item.policy,
                "note": item.note,
            }
            for item in self.history.versions()
        ]


def scheduler_from_platform(platform, *, policy: str = ownership.DEFAULT_POLICY, reserve_soc_pct: float = 20.0) -> Scheduler:
    """从实时内核造一个与真实簇、限额、额度同口径的排程服务。"""

    roster = roster_from_platform(platform, reserve_soc_pct=reserve_soc_pct)
    quota_cfg = platform.config.quota
    grid_cfg = platform.config.grid
    return Scheduler(
        roster,
        storage=platform.store,
        quota_period_hours=quota_cfg.period_hours,
        charge_quota_kwh=quota_cfg.charge_quota_kwh,
        discharge_quota_kwh=quota_cfg.discharge_quota_kwh,
        default_export_kw=grid_cfg.export_limit_kw,
        default_import_kw=grid_cfg.import_limit_kw,
        policy=policy,
    )
