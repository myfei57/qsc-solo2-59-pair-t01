"""排程器：竞争、约束扣减、余量与拒绝诊断。"""

from __future__ import annotations

import pytest

from batterystore.schedule.constraints import (
    GridCurtailment,
    MaintenanceWindow,
    QuotaBudget,
    ReservePolicy,
)
from batterystore.schedule.models import (
    CHARGE,
    DISCHARGE,
    PARTIAL,
    REJECTED,
    Bid,
    ClusterContext,
)
from batterystore.schedule.planner import Planner


def _slots():
    slots = []
    for index in range(8):
        begin = index * 1800.0
        kind = "valley" if index < 4 else "peak"
        from batterystore.schedule.models import Slot

        slots.append(Slot(index=index, start=begin, end=begin + 1800.0, kind=kind))
    return tuple(slots)


def _cluster(rack_id: str, *, soc: float = 50.0, energy: float = 500.0, power: float = 500.0,
             fault: bool = False, maintained: bool = False,
             soc_min: float = 10.0, soc_max: float = 90.0) -> ClusterContext:
    return ClusterContext(
        rack_id=rack_id,
        soc_pct=soc,
        soc_min_pct=soc_min,
        soc_max_pct=soc_max,
        rated_energy_kwh=energy,
        max_power_kw=power,
        fault=fault,
        maintained=maintained,
    )


def _curtail(import_kw: float = 10_000.0, export_kw: float = 10_000.0) -> GridCurtailment:
    return GridCurtailment(default_import_kw=import_kw, default_export_kw=export_kw)


def _planner(clusters, **kwargs):
    return Planner(
        _slots(),
        tuple(clusters),
        curtailment=kwargs.get("curtailment", _curtail()),
        quota=kwargs.get("quota", QuotaBudget(1e9, 1e9)),
        reserve=kwargs.get("reserve", ReservePolicy(0.0, 0.0)),
        maintenance=kwargs.get("maintenance", ()),
        policy=kwargs.get("policy", "submit"),
    )


def test_charge_only_in_valley_and_discharge_only_in_peak():
    planner = _planner([_cluster("rk-01")])
    result = planner.plan(
        [Bid("c", CHARGE, 100.0, 0.0), Bid("d", DISCHARGE, 100.0, 0.0)],
        plan_date="2025-09-16",
        version=1,
        generated_at=0.0,
    )
    charge_slots = {a.slot_index for a in result.decisions if a.direction == CHARGE}
    discharge_slots = {a.slot_index for a in result.decisions if a.direction == DISCHARGE}
    assert charge_slots and max(charge_slots) < 4
    assert discharge_slots and min(discharge_slots) >= 4


def test_no_double_booking_same_cluster_same_slot():
    planner = _planner([_cluster("rk-01")])
    result = planner.plan(
        [Bid("a", CHARGE, 1000.0, 0.0), Bid("b", CHARGE, 1000.0, 1.0)],
        plan_date="d",
        version=1,
        generated_at=0.0,
    )
    owners = {}
    for alloc in result.decisions:
        assert owners.setdefault((alloc.slot_index, alloc.rack_id), alloc.bid_id) == alloc.bid_id


def test_earlier_submitted_bid_gets_priority():
    planner = _planner([_cluster("rk-01")])
    # 谷窗口总可充：SOC 50→90 = 40% * 500 = 200 kWh。
    result = planner.plan(
        [Bid("late", CHARGE, 200.0, 100.0), Bid("early", CHARGE, 200.0, 50.0)],
        plan_date="d",
        version=1,
        generated_at=0.0,
    )
    by_bid = {}
    for alloc in result.decisions:
        by_bid[alloc.bid_id] = by_bid.get(alloc.bid_id, 0.0) + alloc.energy_kwh
    assert by_bid.get("early", 0.0) == pytest.approx(200.0)
    rejected = {item.bid_id: item for item in result.rejections}
    assert "late" in rejected
    assert rejected["late"].status == REJECTED


def test_partial_bid_reports_shortfall_and_reasons():
    planner = _planner([_cluster("rk-01", soc=80.0)])
    # 可充空间 80→90 = 10% * 500 = 50 kWh，申请 200。
    result = planner.plan([Bid("c", CHARGE, 200.0, 0.0)], plan_date="d", version=1, generated_at=0.0)
    rejection = result.rejections[0]
    assert rejection.status == PARTIAL
    assert rejection.scheduled_kwh == pytest.approx(50.0, abs=1e-3)
    assert rejection.short_kwh == pytest.approx(150.0, abs=1e-3)
    assert "cluster_soc_bound" in rejection.reasons


def test_soc_stays_within_window_across_charge_then_discharge():
    cluster = _cluster("rk-01", soc=50.0)
    planner = _planner([cluster])
    result = planner.plan(
        [Bid("c", CHARGE, 1e6, 0.0), Bid("d", DISCHARGE, 1e6, 0.0)],
        plan_date="d",
        version=1,
        generated_at=0.0,
    )
    charge_kwh = sum(a.energy_kwh for a in result.decisions if a.direction == CHARGE)
    discharge_kwh = sum(a.energy_kwh for a in result.decisions if a.direction == DISCHARGE)
    # 50→90 = 200 kWh；充满后从 90 放到 10 = 400 kWh（8 段里峰段 4 段各 250kW·0.5h）。
    assert charge_kwh == pytest.approx(200.0, abs=1e-3)
    assert discharge_kwh == pytest.approx(min(400.0, 4 * 500 * 0.5), abs=1e-3)


def test_station_curtailment_caps_aggregate_power_across_clusters():
    clusters = [_cluster(f"rk-0{i}") for i in range(1, 4)]
    planner = _planner(clusters, curtailment=_curtail(import_kw=300.0, export_kw=10_000.0))
    result = planner.plan([Bid("c", CHARGE, 1e6, 0.0)], plan_date="d", version=1, generated_at=0.0)
    for slot_index in range(4):
        used = sum(a.power_kw for a in result.decisions if a.slot_index == slot_index)
        assert used <= 300.0 + 1e-6


def test_reserve_policy_holds_power_back():
    clusters = [_cluster(f"rk-0{i}") for i in range(1, 3)]
    # 谷窗口限额 1000kW，留 50% 不出租，最多只能排 500kW 同时充。
    planner = _planner(
        clusters,
        curtailment=_curtail(import_kw=1000.0, export_kw=1000.0),
        reserve=ReservePolicy(0.5, 0.0),
    )
    result = planner.plan([Bid("c", CHARGE, 1e6, 0.0)], plan_date="d", version=1, generated_at=0.0)
    for slot_index in range(4):
        used = sum(a.power_kw for a in result.decisions if a.slot_index == slot_index)
        assert used <= 500.0 + 1e-6
    # 余量报告里每个谷时段都记录了保留功率。
    valley_reserves = [
        slot["reserve_kw"] for slot in result.reserve["slots"] if slot.get("direction") == CHARGE
    ]
    assert valley_reserves and all(value == 500.0 for value in valley_reserves)


def test_quota_budget_caps_total_energy():
    planner = _planner([_cluster("rk-01", soc=10.0)], quota=QuotaBudget(charge_kwh=60.0, discharge_kwh=1e9))
    result = planner.plan([Bid("c", CHARGE, 1e6, 0.0)], plan_date="d", version=1, generated_at=0.0)
    charge_kwh = sum(a.energy_kwh for a in result.decisions if a.direction == CHARGE)
    assert charge_kwh == pytest.approx(60.0, abs=1e-3)
    assert "quota_insufficient" in result.rejections[0].reasons or result.rejections[0].reasons


def test_maintained_and_fault_clusters_never_scheduled():
    clusters = [
        _cluster("rk-01", maintained=True),
        _cluster("rk-02", fault=True),
        _cluster("rk-03"),
    ]
    maintenance = (MaintenanceWindow("rk-01", 0.0, 14_400.0, "检修"),)
    planner = _planner(clusters, maintenance=maintenance)
    result = planner.plan([Bid("c", CHARGE, 1e6, 0.0)], plan_date="d", version=1, generated_at=0.0)
    used_racks = {a.rack_id for a in result.decisions}
    assert used_racks == {"rk-03"}
    assert any("cluster_in_maintenance" in item.reasons for item in result.rejections)
    assert any("cluster_fault" in item.reasons for item in result.rejections)
