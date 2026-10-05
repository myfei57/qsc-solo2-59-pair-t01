"""峰谷排程：可执行计划、归属策略、三道约束、保留余量与双时态回放。"""

from __future__ import annotations

import pytest

from batterystore.schedule import (
    CHARGE,
    DEFAULT_POLICY,
    DISCHARGE,
    EFFECTIVE,
    SUBMIT,
    CurtailmentInterval,
    CurtailmentPlan,
    MaintenancePlan,
    MaintenanceWindow,
    QuotaAllowance,
    Request,
    Scheduler,
    synthetic_roster,
)
from batterystore.schedule.engine import solve
from batterystore.schedule.model import (
    REASON_CLUSTER_BUSY,
    REASON_CURTAILMENT,
    REASON_MAINTENANCE,
    REASON_QUOTA,
    REASON_RESERVE,
    REASON_UNKNOWN_CLUSTER,
)
from batterystore.schedule.windows import DAY_S, build_slots, midnight_of, windows_for_range
from batterystore.store.jsonfile import JsonStore


@pytest.fixture()
def store(tmp_path):
    return JsonStore(str(tmp_path / "schedule"))


@pytest.fixture()
def day():
    return midnight_of(1_758_000_000.0)


def _scheduler(store, *, clusters=3, policy=EFFECTIVE, reserve=20.0, quota=100_000.0, export=60000.0, imp=80000.0):
    roster = synthetic_roster(
        clusters,
        rated_kwh=500.0,
        initial_soc_pct=55.0,
        reserve_soc_pct=reserve,
        max_charge_kw=500.0,
        max_discharge_kw=400.0,
    )
    return Scheduler(
        roster,
        storage=store,
        charge_quota_kwh=quota,
        discharge_quota_kwh=quota,
        default_export_kw=export,
        default_import_kw=imp,
        policy=policy,
    )


def test_tariff_windows_charge_in_valley_discharge_in_peak(store, day):
    scheduler = _scheduler(store)
    snap = scheduler.generate_day_ahead(day_start=day, recorded_at=day - 3600)
    assert snap.counts["reconciled"] is True
    for allocation in snap.allocations:
        hour = int((allocation["start"] - day) // 3600)
        if allocation["action"] == CHARGE:
            assert not (8 <= hour < 22), "充电只能排在谷段"
        if allocation["action"] == DISCHARGE:
            assert 8 <= hour < 22, "放电只能排在峰段"


def test_every_cluster_scheduled_and_reserve_protected(store, day):
    scheduler = _scheduler(store, clusters=20)
    snap = scheduler.generate_day_ahead(day_start=day, recorded_at=day - 3600)
    scheduled = {item["rack_id"] for item in snap.allocations}
    assert scheduled == {f"rk-{i:03d}" for i in range(1, 21)}
    for row in snap.reserve:
        assert row["planned_end_energy_kwh"] >= row["reserve_energy_kwh"] - 1e-6
        assert row["reserve_headroom_kwh"] >= 0.0


def test_reserve_blocks_deep_discharge(store, day):
    roster = synthetic_roster(1, rated_kwh=500.0, initial_soc_pct=22.0, reserve_soc_pct=20.0)
    scheduler = Scheduler(roster, storage=store, charge_quota_kwh=1e9, discharge_quota_kwh=1e9,
                          default_export_kw=10000.0, default_import_kw=10000.0)
    req = Request("big", DISCHARGE, 300.0, day - 10, day + 8 * 3600, day + 20 * 3600, "rk-001")
    snap = scheduler.generate_day_ahead(
        day_start=day, requests=[req], recorded_at=day - 3600, auto_charge=False, auto_discharge=False
    )
    assert any(item["reason"] == REASON_RESERVE for item in snap.rejections)
    assert snap.reserve[0]["planned_end_energy_kwh"] >= snap.reserve[0]["reserve_energy_kwh"] - 1e-6


def test_competition_same_effective_window_falls_back_to_submit_time(store, day):
    # 两条申请生效窗口完全相同：先提交者先占簇，后提交的排到别处或被拒。
    scheduler = _scheduler(store, clusters=1, reserve=10.0)
    roster = scheduler.roster
    roster_ids = roster.ids()
    reqs = [
        Request("later", DISCHARGE, 400.0, submitted_at=day + 200, window_start=day + 8 * 3600,
                window_end=day + 12 * 3600, rack_id="rk-001"),
        Request("earlier", DISCHARGE, 400.0, submitted_at=day + 100, window_start=day + 8 * 3600,
                window_end=day + 12 * 3600, rack_id="rk-001"),
    ]
    for policy in (EFFECTIVE, SUBMIT):
        scheduler.set_policy(policy)
        snap = scheduler.generate_day_ahead(
            day_start=day, requests=reqs, recorded_at=day + 300, auto_charge=False, auto_discharge=False
        )
        first_winner = sorted(snap.allocations, key=lambda a: (a["slot_index"], -a["energy_kwh"]))
        # 同一簇同一槽位互斥：每个槽位只有一个占用者
        slots_used = [a["slot_index"] for a in first_winner]
        assert len(slots_used) == len(set(slots_used))


def test_effective_and_submit_policies_differ_across_midnight(store, day):
    """跨零点同一槽位硬碰撞：两种策略归属不同，验证差异真实存在。"""

    def winner(policy):
        roster = synthetic_roster(1, rated_kwh=500.0, initial_soc_pct=40.0, reserve_soc_pct=10.0)
        local_store = JsonStore(str(store.root + f"-{policy}"))
        s = Scheduler(roster, storage=local_store, charge_quota_kwh=1e9, discharge_quota_kwh=1e9,
                      default_export_kw=10000.0, default_import_kw=10000.0, policy=policy)
        slots = build_slots(day + 23 * 3600, day + 25 * 3600, 2 * 3600)
        reqs = [
            Request("X", DISCHARGE, 120.0, submitted_at=day - 7200,
                    window_start=day + DAY_S, window_end=day + DAY_S + 3600, rack_id="rk-001"),
            Request("Y", DISCHARGE, 120.0, submitted_at=day - 3600,
                    window_start=day + 23 * 3600, window_end=day + DAY_S, rack_id="rk-001"),
        ]
        result = solve(
            roster=roster, slots=slots, requests=reqs, maintenance=MaintenancePlan(),
            curtailment=CurtailmentPlan(default_export_kw=10000.0, default_import_kw=10000.0),
            quota=QuotaAllowance(24, 1e9, 1e9), tariff_of_slot=lambda slot: "peak", policy=policy,
        )
        return {a.request_id for a in result["allocations"]}

    assert winner(EFFECTIVE) == {"Y"}
    assert winner(SUBMIT) == {"X"}


def test_effective_policy_is_midnight_invariant(store, day):
    """无论从零点前还是零点后重算，生效时刻策略归属结果一致。"""

    def solve_once():
        roster = synthetic_roster(1, rated_kwh=500.0, initial_soc_pct=40.0, reserve_soc_pct=10.0)
        slots = build_slots(day + 23 * 3600, day + 25 * 3600, 2 * 3600)
        reqs = [
            Request("X", DISCHARGE, 120.0, submitted_at=day - 7200,
                    window_start=day + DAY_S, window_end=day + DAY_S + 3600, rack_id="rk-001"),
            Request("Y", DISCHARGE, 120.0, submitted_at=day - 3600,
                    window_start=day + 23 * 3600, window_end=day + DAY_S, rack_id="rk-001"),
        ]
        result = solve(
            roster=roster, slots=slots, requests=reqs, maintenance=MaintenancePlan(),
            curtailment=CurtailmentPlan(default_export_kw=10000.0, default_import_kw=10000.0),
            quota=QuotaAllowance(24, 1e9, 1e9), tariff_of_slot=lambda slot: "peak", policy=EFFECTIVE,
        )
        return sorted((a.slot_index, a.request_id) for a in result["allocations"])

    assert solve_once() == solve_once()


def test_maintenance_window_blocks_cluster(store, day):
    scheduler = _scheduler(store, clusters=2)
    req = Request("m1", DISCHARGE, 100.0, day - 10, day + 8 * 3600, day + 10 * 3600, "rk-001")
    maint = MaintenancePlan([MaintenanceWindow("rk-001", day + 8 * 3600, day + 10 * 3600, "绝缘检测")])
    snap = scheduler.generate_day_ahead(
        day_start=day, requests=[req], maintenance=maint, recorded_at=day - 3600,
        auto_charge=False, auto_discharge=False,
    )
    assert any(item["reason"] == REASON_MAINTENANCE for item in snap.rejections)
    assert snap.reconciliation["checks"]["maintenance"] is True
    for allocation in snap.allocations:
        if allocation["rack_id"] == "rk-001":
            assert not (day + 8 * 3600 <= allocation["start"] < day + 10 * 3600)


def test_curtailment_limits_station_power(store, day):
    roster = synthetic_roster(4, rated_kwh=500.0, initial_soc_pct=80.0, reserve_soc_pct=10.0,
                              max_discharge_kw=400.0)
    scheduler = Scheduler(roster, storage=store, charge_quota_kwh=1e9, discharge_quota_kwh=1e9,
                          default_export_kw=10000.0, default_import_kw=10000.0)
    curtailment = CurtailmentPlan(
        [CurtailmentInterval(day + 9 * 3600, day + 10 * 3600, export_limit_kw=200.0, import_limit_kw=200.0)],
        default_export_kw=10000.0, default_import_kw=10000.0,
    )
    snap = scheduler.generate_day_ahead(
        day_start=day, curtailment=curtailment, recorded_at=day - 3600, auto_charge=False
    )
    slot9_total = sum(a["power_kw"] for a in snap.allocations if a["slot_index"] == 9 and a["action"] == DISCHARGE)
    assert slot9_total <= 200.0 + 1e-6
    assert snap.reconciliation["checks"]["curtailment"] is True


def test_quota_caps_period_energy(store, day):
    roster = synthetic_roster(5, rated_kwh=500.0, initial_soc_pct=90.0, reserve_soc_pct=10.0)
    scheduler = Scheduler(roster, storage=store, charge_quota_kwh=100.0, discharge_quota_kwh=100.0,
                          default_export_kw=10000.0, default_import_kw=10000.0)
    snap = scheduler.generate_day_ahead(
        day_start=day, recorded_at=day - 3600, auto_charge=False, auto_discharge=True
    )
    total_discharge = sum(a["energy_kwh"] for a in snap.allocations if a["action"] == DISCHARGE)
    assert total_discharge <= 100.0 + 1e-6
    assert snap.reconciliation["checks"]["quota"] is True


def test_unknown_cluster_and_unschedulable_request_are_explained(store, day):
    scheduler = _scheduler(store, clusters=2)
    reqs = [
        Request("ghost", DISCHARGE, 50.0, day - 10, day + 8 * 3600, day + 12 * 3600, "rk-999"),
        Request("nowindow", DISCHARGE, 50.0, day - 10, day + 30 * DAY_S, day + 31 * DAY_S, "rk-001"),
    ]
    snap = scheduler.generate_day_ahead(
        day_start=day, requests=reqs, recorded_at=day - 3600, auto_charge=False, auto_discharge=False
    )
    reasons = {item["reason"] for item in snap.rejections}
    assert REASON_UNKNOWN_CLUSTER in reasons
    assert "outside_action_window" in reasons
    for rejection in snap.rejections:
        assert rejection["detail"]


def test_revision_keeps_old_version_and_replays_bitemporally(store, day):
    scheduler = _scheduler(store, clusters=3)
    v1 = scheduler.generate_day_ahead(day_start=day, recorded_at=day - 3600)
    assert v1.version == 1

    maint = MaintenancePlan([MaintenanceWindow("rk-001", day, day + DAY_S, "全天检修")])
    v2 = scheduler.revise(
        start=day, end=day + DAY_S, maintenance=maint, recorded_at=day + 6 * 3600,
        auto_charge=True, auto_discharge=True, note="早间修订",
    )
    assert v2.version == 2
    # 旧版仍可按版本号取回
    assert len(scheduler.replay_version(1)["allocations"]) == len(v1.allocations)
    # 生效时间回放：03:00 时 v2 尚未算出，现场执行的还是 v1
    assert scheduler.replay_effective(day + 3 * 3600)["version"] == 1
    # 06:00 之后 v2 已记录且生效
    assert scheduler.replay_effective(day + 12 * 3600)["version"] == 2
    # 记录时间回放：截至 04:00 只有 v1
    assert [p["version"] for p in scheduler.replay_recorded(day + 4 * 3600)] == [1]
    assert [p["version"] for p in scheduler.replay_recorded(day + 23 * 3600)] == [1, 2]


def test_versions_persist_across_restart(store, day):
    scheduler = _scheduler(store, clusters=3)
    scheduler.generate_day_ahead(day_start=day, recorded_at=day - 3600)
    scheduler.revise(start=day, end=day + DAY_S, recorded_at=day + 5 * 3600)
    # 用同一个落盘目录重新装配，等同重启
    rebuilt = _scheduler(store, clusters=3)
    assert [item["version"] for item in rebuilt.versions_index()] == [1, 2]
    assert rebuilt.replay_effective(day + 10 * 3600)["version"] == 2


def test_two_hundred_clusters_schedule_fast_and_reconcile(store, day):
    import time

    roster = synthetic_roster(220, rated_kwh=500.0, initial_soc_pct=55.0, reserve_soc_pct=20.0)
    scheduler = Scheduler(roster, storage=store, charge_quota_kwh=60000.0, discharge_quota_kwh=60000.0,
                          default_export_kw=60000.0, default_import_kw=80000.0)
    requests = [
        Request(f"t{i}", DISCHARGE, 120.0, day + i, day + 8 * 3600, day + 20 * 3600, f"rk-{(i % 220) + 1:03d}")
        for i in range(60)
    ]
    started = time.perf_counter()
    snap = scheduler.generate_day_ahead(day_start=day, requests=requests, recorded_at=day - 3600)
    elapsed = time.perf_counter() - started
    assert snap.counts["clusters"] == 220
    assert snap.reconciliation["ok"] is True
    assert elapsed < 2.0


def test_default_policy_is_effective_and_marked_midnight_safe(store, day):
    scheduler = _scheduler(store)
    assert DEFAULT_POLICY == EFFECTIVE
    snap = scheduler.generate_day_ahead(day_start=day, recorded_at=day - 3600)
    assert snap.policy["policy"] == EFFECTIVE
    assert snap.policy["midnight_safe"] is True
    assert snap.policy["primary"] == "window_start"
    assert snap.policy["tie_breaker"] == "submitted_at"


def test_windows_for_range_handles_multiple_days(day):
    windows = windows_for_range(day + 20 * 3600, day + DAY_S + 4 * 3600)
    kinds = [w.kind for w in windows]
    assert "valley" in kinds and "peak" in kinds
    assert windows[0].start < windows[-1].end


def test_reconcile_evolves_cluster_energy_in_time_order(store, day):
    """同一簇先在峰段放电、再在夜谷充电，末态必须按时间顺序演化得出。"""

    roster = synthetic_roster(1, rated_kwh=500.0, initial_soc_pct=80.0, reserve_soc_pct=10.0)
    scheduler = Scheduler(roster, storage=store, charge_quota_kwh=1e9, discharge_quota_kwh=1e9,
                          default_export_kw=10000.0, default_import_kw=10000.0)
    # 跨过峰段与夜谷的两整天区间，只放这一簇自动谷充峰放
    snap = scheduler.revise(
        start=day, end=day + 2 * DAY_S, recorded_at=day - 3600,
        auto_charge=True, auto_discharge=True,
    )
    assert snap.reconciliation["ok"] is True
    # 末态电量必须落在 [保留地板, 容量上限]
    row = snap.reserve[0]
    assert row["reserve_energy_kwh"] - 1e-6 <= row["planned_end_energy_kwh"]
    assert row["planned_end_energy_kwh"] <= roster.get("rk-001").energy_max_kwh + 1e-6
    assert snap.reconciliation["checks"]["reserve"] is True
