"""排程服务：平台侧的排程入口。

:class:`Scheduler` 把排程器、对账器与版本历史粘起来，向 :class:`Platform`
暴露一条主链：收申请 → 组装现场快照 → 排程 → 对账 → 落版本 → 发审计事件。
旧方案永不覆盖，改单走新版本；任意旧版都能按时间回放、按落盘输入重算复核。

服务自己持有跨次排程的输入台账（申请单、检修窗口、逐时段限电覆盖、调峰余量
策略）。这些输入是计划层事实，和设备运行态分离：设备只管能不能充放，计划层
记录谁申请、哪簇在修、哪个时段限多少电。
"""

from __future__ import annotations

import threading
from typing import Callable

from ..clock import Clock
from ..eventbus import Event, EventBus
from ..store.jsonfile import JsonStore
from .constraints import GridCurtailment, MaintenanceWindow, QuotaBudget, ReservePolicy
from .context import build_cluster_contexts
from .history import ScheduleHistory
from .models import Bid
from .planner import Planner
from .reconciliation import reconcile
from .reservation import explain_cross_midnight
from .timegrid import day_slots, merge_windows, plan_date_of


class Scheduler:
    """平台排程服务。

    ``context_fn`` 返回 ``(config, racks, fault_rack_ids, quota_budget,
    base_curtailment)``，由平台在调用当口从真实组件取，服务因此不直接依赖
    :class:`Platform`，避免双向导入。
    """

    def __init__(
        self,
        clock: Clock,
        store: JsonStore,
        bus: EventBus,
        context_fn: Callable[[], tuple],
        *,
        tariff_kind: Callable[[float], str],
        slot_s: float = 1800.0,
    ) -> None:
        self._clock = clock
        self._store = store
        self._bus = bus
        self._context_fn = context_fn
        self._tariff_kind = tariff_kind
        self._slot_s = float(slot_s)
        self._history = ScheduleHistory(store)
        self._lock = threading.RLock()
        self._bids: list[Bid] = []
        self._maintenance: list[MaintenanceWindow] = []
        self._curtail_overrides: dict[int, dict[str, float]] = {}
        self._reserve = ReservePolicy()
        self._restore()

    @property
    def history(self) -> ScheduleHistory:
        return self._history

    # ---- 输入台账 ---------------------------------------------------------

    def submit_bid(
        self,
        bid_id: str,
        direction: str,
        energy_kwh: float,
        *,
        effective_at: float | None = None,
        power_kw: float | None = None,
        owner: str = "",
        note: str = "",
    ) -> dict:
        """登记一条充／放申请单，提交时刻取当前挂钟，先报先得。"""

        with self._lock:
            if any(existing.bid_id == bid_id for existing in self._bids):
                raise ValueError(f"申请单重复提交: {bid_id}")
            bid = Bid(
                bid_id=bid_id,
                direction=direction,
                energy_kwh=energy_kwh,
                submitted_at=self._clock.now(),
                effective_at=effective_at,
                power_kw=power_kw,
                owner=owner,
                note=note,
            )
            self._bids.append(bid)
            self._persist()
            self._bus.publish(
                Event.of(
                    "schedule",
                    "schedule",
                    "bid_submitted",
                    severity="notice",
                    bid_id=bid_id,
                    direction=direction,
                    energy_kwh=round(energy_kwh, 6),
                    submitted_at=round(bid.submitted_at, 3),
                )
            )
            return bid.as_dict()

    def add_maintenance(self, rack_id: str, start: float, end: float, reason: str = "") -> dict:
        """登记一簇的检修区间。"""

        if float(end) <= float(start):
            raise ValueError("检修区间结束必须晚于开始")
        with self._lock:
            window = MaintenanceWindow(rack_id, float(start), float(end), reason)
            self._maintenance.append(window)
            self._persist()
            self._bus.publish(
                Event.of(
                    "schedule",
                    "schedule",
                    "maintenance_added",
                    severity="notice",
                    rack_id=rack_id,
                    start=round(window.start, 3),
                    end=round(window.end, 3),
                )
            )
            return window.as_dict()

    def set_curtailment(self, slot_index: int, *, import_kw: float | None = None, export_kw: float | None = None) -> dict:
        """对某个时段设置临时限电覆盖（千瓦），未给的方向保持基线限额。"""

        with self._lock:
            entry = dict(self._curtail_overrides.get(int(slot_index), {}))
            if import_kw is not None:
                entry["charge"] = max(0.0, float(import_kw))
            if export_kw is not None:
                entry["discharge"] = max(0.0, float(export_kw))
            self._curtail_overrides[int(slot_index)] = entry
            self._persist()
            return {"slot_index": int(slot_index), **entry}

    def set_reserve(self, **kwargs: float) -> dict:
        """更新临时调峰余量策略。"""

        current = self._reserve.as_dict()
        current.update(kwargs)
        with self._lock:
            self._reserve = ReservePolicy(
                charge_reserve_ratio=current["charge_reserve_ratio"],
                discharge_reserve_ratio=current["discharge_reserve_ratio"],
                charge_reserve_kw=current["charge_reserve_kw"],
                discharge_reserve_kw=current["discharge_reserve_kw"],
            )
            self._persist()
            return self._reserve.as_dict()

    def inputs(self) -> dict:
        """返回当前排程输入台账。"""

        with self._lock:
            return {
                "bids": [bid.as_dict() for bid in self._bids],
                "maintenance": [window.as_dict() for window in self._maintenance],
                "curtail_overrides": {
                    str(slot): {direction: round(value, 3) for direction, value in limits.items()}
                    for slot, limits in sorted(self._curtail_overrides.items())
                },
                "reserve": self._reserve.as_dict(),
            }

    def explain_policy(self, plan_date: str) -> dict:
        """说明该运营日两种归属口径在跨零点窗口上会不会排出不同结果。"""

        slots = day_slots(plan_date, self._tariff_kind, self._slot_s)
        windows = merge_windows(slots)
        crossing = [window for window in windows if window["crosses_midnight"]]
        with self._lock:
            bids = list(self._bids)

        divergence = None
        if crossing and len(bids) >= 2:
            target = crossing[0]
            window_bids = [bid for bid in bids if bid.direction == ("charge" if target["kind"] == "valley" else "discharge")]
            # 在同方向申请里找一对真正会因口径不同而互换归属的单子；找不到就
            # 取提交最早的两条如实标注"不会分歧"。
            candidates = window_bids if len(window_bids) >= 2 else sorted(bids, key=lambda bid: bid.submitted_at)
            ordered = sorted(candidates, key=lambda bid: bid.submitted_at)
            divergence = explain_cross_midnight(ordered[0], ordered[1], target["start"])
            for index, first in enumerate(ordered):
                for second in ordered[index + 1:]:
                    probe = explain_cross_midnight(first, second, target["start"])
                    if probe["diverges"]:
                        divergence = probe
                        break
                if divergence and divergence.get("diverges"):
                    break
        return {
            "plan_date": plan_date,
            "default_policy": "submit",
            "windows": [
                {
                    "kind": window["kind"],
                    "start": round(window["start"], 3),
                    "end": round(window["end"], 3),
                    "crosses_midnight": window["crosses_midnight"],
                }
                for window in windows
            ],
            "divergence": divergence,
        }

    def clear_bids(self) -> int:
        """清空申请单（出完方案后开启下一运营日用），返回清掉的条数。"""

        with self._lock:
            count = len(self._bids)
            self._bids.clear()
            self._persist()
            return count

    # ---- 出方案 -----------------------------------------------------------

    def generate(self, plan_date: str | None = None, *, policy: str | None = None, reason: str = "") -> dict:
        """按当前输入为某运营日出一版方案，完成排程、对账与版本落盘。"""

        with self._lock:
            config, racks, fault_ids, quota_budget, base_curtail = self._context_fn()
            date = plan_date or plan_date_of(self._clock.now())
            slots = day_slots(date, self._tariff_kind, self._slot_s)
            maintenance = tuple(
                window for window in self._maintenance if window.overlaps(slots[0].start, slots[-1].end)
            )
            clusters = build_cluster_contexts(
                config, racks, fault_rack_ids=set(fault_ids), maintained_rack_ids=set()
            )
            curtailment = self._apply_overrides(base_curtail)
            chosen_policy = policy or self._stored_policy()
            planner = Planner(
                slots,
                clusters,
                maintenance=maintenance,
                curtailment=curtailment,
                quota=quota_budget,
                reserve=self._reserve,
                policy=chosen_policy,
            )
            version = self._history.next_version(date)
            result = planner.plan(list(self._bids), plan_date=date, version=version, generated_at=self._clock.now())
            report = reconcile(
                result,
                maintenance=maintenance,
                curtailment=curtailment,
                quota=quota_budget,
                reserve=self._reserve,
            )
            inputs_snapshot = self._snapshot_inputs(
                chosen_policy, date, clusters, maintenance, curtailment, quota_budget
            )
            header = self._history.save(result.as_dict(), inputs_snapshot, created_at=self._clock.now(), reason=reason)
            self._store_policy(chosen_policy)
            self._bus.publish(
                Event.of(
                    "schedule",
                    "schedule",
                    "schedule_generated",
                    severity="notice" if report["ok"] else "warning",
                    station=getattr(config, "name", ""),
                    plan_date=date,
                    version=version,
                    policy=chosen_policy,
                    reconciled=report["ok"],
                    violations=";".join(report["violations"]),
                    charge_kwh=result.as_dict()["totals"]["charge_kwh"],
                    discharge_kwh=result.as_dict()["totals"]["discharge_kwh"],
                    reason=reason,
                    op_id=f"sched-{date}-v{version:03d}",
                )
            )
            return {"result": result.as_dict(), "reconciliation": report, "header": header}

    def current_schedule(self) -> dict | None:
        """读取当前版本方案。"""

        return self._history.latest()

    def get_version(self, plan_date: str, version: int) -> dict | None:
        """读取某运营日某一版方案。"""

        return self._history.load_version(plan_date, version)

    def replay_day(self, plan_date: str) -> list[dict]:
        """按版本升序回放某运营日的全部方案（用于复盘每次改动）。"""

        return self._history.replay(plan_date)

    def replay_all_versions(self) -> list[dict]:
        """跨运营日按时间回放全部版本头。"""

        return self._history.replay_timeline()

    def recompute(self, plan_date: str, version: int) -> dict:
        """用某版落盘输入原样重算，核对与存档结果是否逐字段一致。"""

        with self._lock:
            stored = self._history.load_version(plan_date, version)
            inputs = self._history.load_inputs(plan_date, version)
            if stored is None or inputs is None:
                raise KeyError(f"找不到方案 {plan_date} v{version}")
            rebuilt = self._rebuild_from_inputs(inputs)
            stored_decisions = stored.get("decisions", [])
            rebuilt_decisions = rebuilt["decisions"]
            consistent = stored_decisions == rebuilt_decisions
            return {
                "plan_date": plan_date,
                "version": version,
                "consistent": consistent,
                "stored_decisions": stored_decisions,
                "recomputed_decisions": rebuilt_decisions,
                "policy": stored.get("policy"),
            }

    # ---- 内部辅助 ---------------------------------------------------------

    def _apply_overrides(self, base: GridCurtailment) -> GridCurtailment:
        charge_limits = dict(base.charge_limits)
        discharge_limits = dict(base.discharge_limits)
        for slot_index, limits in self._curtail_overrides.items():
            if "charge" in limits:
                charge_limits[slot_index] = limits["charge"]
            if "discharge" in limits:
                discharge_limits[slot_index] = limits["discharge"]
        return GridCurtailment(
            default_import_kw=base.default_import_kw,
            default_export_kw=base.default_export_kw,
            charge_limits=charge_limits,
            discharge_limits=discharge_limits,
        )

    def _snapshot_inputs(
        self,
        policy: str,
        plan_date: str,
        clusters: tuple,
        maintenance: tuple[MaintenanceWindow, ...],
        curtailment: GridCurtailment,
        quota: QuotaBudget,
    ) -> dict:
        return {
            "plan_date": plan_date,
            "policy": policy,
            "slot_s": self._slot_s,
            "bids": [bid.as_dict() for bid in self._bids],
            "clusters": [cluster.as_dict() for cluster in clusters],
            "maintenance": [window.as_dict() for window in maintenance],
            "curtailment": curtailment.as_dict(),
            "quota": quota.as_dict(),
            "reserve": self._reserve.as_dict(),
        }

    def _rebuild_from_inputs(self, inputs: dict) -> dict:
        from .models import ClusterContext

        plan_date = str(inputs["plan_date"])
        slots = day_slots(plan_date, self._tariff_kind, float(inputs.get("slot_s", self._slot_s)))
        stored_clusters = tuple(ClusterContext.from_dict(item) for item in inputs["clusters"])
        maintenance = tuple(MaintenanceWindow.from_dict(item) for item in inputs.get("maintenance", ()))
        curtailment = GridCurtailment.from_dict(inputs["curtailment"])
        quota = QuotaBudget.from_dict(inputs["quota"])
        reserve = ReservePolicy.from_dict(inputs["reserve"])
        bids = [Bid.from_dict(item) for item in inputs["bids"]]
        planner = Planner(
            slots,
            stored_clusters,
            maintenance=maintenance,
            curtailment=curtailment,
            quota=quota,
            reserve=reserve,
            policy=inputs.get("policy", "submit"),
        )
        return planner.plan(bids, plan_date=plan_date, version=0, generated_at=0.0).as_dict()

    def _persist(self) -> None:
        self._store.write(
            "schedule_inputs",
            {
                "bids": [bid.as_dict() for bid in self._bids],
                "maintenance": [window.as_dict() for window in self._maintenance],
                "curtail_overrides": {
                    str(slot): limits for slot, limits in self._curtail_overrides.items()
                },
                "reserve": self._reserve.as_dict(),
            },
        )

    def _restore(self) -> None:
        raw = self._store.read("schedule_inputs")
        if not isinstance(raw, dict):
            return
        self._bids = [Bid.from_dict(item) for item in raw.get("bids", [])]
        self._maintenance = [MaintenanceWindow.from_dict(item) for item in raw.get("maintenance", [])]
        overrides = raw.get("curtail_overrides", {})
        self._curtail_overrides = {
            int(slot): {direction: float(value) for direction, value in limits.items()}
            for slot, limits in overrides.items()
        }
        reserve = raw.get("reserve")
        if isinstance(reserve, dict):
            self._reserve = ReservePolicy.from_dict(reserve)

    def _stored_policy(self) -> str:
        raw = self._store.read("schedule_policy")
        if isinstance(raw, dict) and raw.get("policy") in ("submit", "effective"):
            return str(raw["policy"])
        return "submit"

    def _store_policy(self, policy: str) -> None:
        self._store.write("schedule_policy", {"policy": policy})
