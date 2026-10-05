"""排程结果对账：检修、限电、额度三处独立核对。"""

from __future__ import annotations

from batterystore.schedule.constraints import (
    GridCurtailment,
    MaintenanceWindow,
    QuotaBudget,
    ReservePolicy,
)
from batterystore.schedule.models import Allocation, Rejection, ScheduleResult, Slot
from batterystore.schedule.reconciliation import reconcile


def _slot(index: int, kind: str) -> Slot:
    start = index * 1800.0
    return Slot(index=index, start=start, end=start + 1800.0, kind=kind)


def _result(decisions, rejections=(), clusters=()):
    slots = tuple(_slot(i, "valley" if i < 2 else "peak") for i in range(4))
    return ScheduleResult(
        plan_date="2025-09-16",
        version=1,
        policy="submit",
        horizon_start=0.0,
        horizon_end=4 * 1800.0,
        slots=slots,
        clusters=clusters,
        decisions=tuple(decisions),
        rejections=tuple(rejections),
        reserve={"policy": {}},
        generated_at=0.0,
    )


def _ctx(rack_id="rk-01", power=500.0, maintained=False):
    from batterystore.schedule.models import ClusterContext

    return ClusterContext(
        rack_id=rack_id,
        soc_pct=50.0,
        soc_min_pct=10.0,
        soc_max_pct=90.0,
        rated_energy_kwh=500.0,
        max_power_kw=power,
        fault=False,
        maintained=maintained,
    )


def _curtail(import_kw=1000.0, export_kw=1000.0):
    return GridCurtailment(default_import_kw=import_kw, default_export_kw=export_kw)


def test_clean_result_passes():
    alloc = Allocation(0, "rk-01", "charge", 250.0, 125.0, "c1")
    result = _result([alloc], clusters=(_ctx(),))
    report = reconcile(
        result,
        curtailment=_curtail(),
        quota=QuotaBudget(1000.0, 1000.0),
        reserve=ReservePolicy(0.0, 0.0),
    )
    assert report["ok"] is True
    assert report["violations"] == []


def test_detects_allocation_during_maintenance():
    alloc = Allocation(0, "rk-01", "charge", 250.0, 125.0, "c1")
    result = _result([alloc], clusters=(_ctx(),))
    maintenance = (MaintenanceWindow("rk-01", 0.0, 1800.0, "检修"),)
    report = reconcile(result, maintenance=maintenance, curtailment=_curtail(), quota=QuotaBudget(1000, 1000))
    assert any("scheduled_in_maintenance" in v for v in report["violations"])


def test_detects_aggregate_curtailment_breach():
    # 两个簇各 400kW，同时段合计 800kW，超过用电限额 600kW。
    decisions = [
        Allocation(0, "rk-01", "charge", 400.0, 200.0, "c1"),
        Allocation(0, "rk-02", "charge", 400.0, 200.0, "c1"),
    ]
    result = _result(decisions, clusters=(_ctx("rk-01"), _ctx("rk-02")))
    report = reconcile(result, curtailment=_curtail(import_kw=600.0), quota=QuotaBudget(10_000, 10_000))
    assert any("aggregate_power" in v and "over_curtail" in v for v in report["violations"])


def test_detects_reserve_breach_within_hard_limit():
    # 硬限额 1000kW、50% 余量（可租 500）；排 700kW 不超硬限但占了调峰余量。
    alloc = Allocation(0, "rk-01", "charge", 700.0, 350.0, "c1")
    result = _result([alloc], clusters=(_ctx(power=1000.0),))
    report = reconcile(
        result,
        curtailment=_curtail(import_kw=1000.0),
        quota=QuotaBudget(10_000, 10_000),
        reserve=ReservePolicy(0.5, 0.0),
    )
    assert any("reserve_breached" in v for v in report["violations"])
    assert not any("over_curtail" in v for v in report["violations"])


def test_detects_quota_breach():
    alloc = Allocation(0, "rk-01", "charge", 500.0, 250.0, "c1")
    result = _result([alloc], clusters=(_ctx(),))
    report = reconcile(result, curtailment=_curtail(), quota=QuotaBudget(charge_kwh=100.0, discharge_kwh=1000.0))
    assert any(v.startswith("charge_quota_") for v in report["violations"])


def test_detects_charge_in_peak_window():
    # 时段 2 是峰段，却排了充电。
    alloc = Allocation(2, "rk-01", "charge", 200.0, 100.0, "c1")
    result = _result([alloc], clusters=(_ctx(),))
    report = reconcile(result, curtailment=_curtail(), quota=QuotaBudget(10_000, 10_000))
    assert any("charge_in_peak_window" in v for v in report["violations"])


def test_detects_energy_power_mismatch_and_device_cap():
    bad_energy = Allocation(0, "rk-01", "charge", 100.0, 999.0, "c1")
    over_device = Allocation(1, "rk-01", "discharge", 999.0, 499.5, "d1")
    result = _result([bad_energy, over_device], clusters=(_ctx(power=500.0),))
    report = reconcile(
        result,
        curtailment=GridCurtailment(default_import_kw=10_000, default_export_kw=10_000),
        quota=QuotaBudget(10_000, 10_000),
    )
    joined = " ".join(report["violations"])
    assert "energy_mismatch" in joined
    assert "over_device" in joined


def test_detects_rejection_without_reason_and_mismatched_amount():
    alloc = Allocation(0, "rk-01", "charge", 100.0, 50.0, "c1")
    bad_rejection = Rejection("c1", "partial", 100.0, 99.0, reasons=())
    result = _result([alloc], rejections=(bad_rejection,), clusters=(_ctx(),))
    report = reconcile(result, curtailment=_curtail(), quota=QuotaBudget(10_000, 10_000))
    joined = " ".join(report["violations"])
    assert "rejection_scheduled_mismatch" in joined
    assert "rejection_without_reason" in joined
