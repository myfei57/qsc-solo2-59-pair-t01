"""排程服务的平台集成：出方案、对账、版本回放、重算与重启恢复。"""

from __future__ import annotations

import calendar
import time

from batterystore.app import build_platform
from batterystore.clock import ManualClock
from batterystore.config import default_config


def _day_start(plan_date: str) -> float:
    return float(calendar.timegm(time.strptime(plan_date, "%Y-%m-%d")))


def _date_of(ts: float) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(ts))


def test_generate_schedule_reconciles_and_persists_version(platform):
    platform.start()
    date = _date_of(platform.clock.now())
    platform.submit_schedule_bid("c1", "charge", 200.0, owner="A")
    platform.submit_schedule_bid("d1", "discharge", 150.0, owner="B")

    out = platform.generate_schedule(date)
    assert out["reconciliation"]["ok"] is True
    assert out["result"]["policy"] == "submit"
    assert out["header"]["version"] == 1

    current = platform.current_schedule()
    assert current["plan_date"] == date
    assert current["version"] == 1
    assert current["totals"]["charge_kwh"] > 0
    assert current["totals"]["discharge_kwh"] > 0


def test_revision_creates_new_version_and_replays_history(platform):
    platform.start()
    date = _date_of(platform.clock.now())
    platform.submit_schedule_bid("c1", "charge", 50.0)
    platform.generate_schedule(date, reason="初版")
    platform.clock.advance(60.0)
    platform.submit_schedule_bid("c2", "charge", 50.0)
    platform.generate_schedule(date, reason="追加一单")

    versions = platform.schedule_versions()
    assert [header["version"] for header in versions] == [1, 2]
    assert versions[1]["reason"] == "追加一单"

    replay = platform.replay_schedule(date)
    assert [entry["header"]["version"] for entry in replay] == [1, 2]
    # 旧版本仍可单独取回，没被覆盖。
    v1 = platform.schedule_version(date, 1)
    v2 = platform.schedule_version(date, 2)
    assert v1 is not None and v2 is not None
    assert len(v1["decisions"]) != len(v2["decisions"])


def test_recompute_historical_version_is_consistent(platform):
    platform.start()
    date = _date_of(platform.clock.now())
    platform.submit_schedule_bid("c1", "charge", 120.0)
    platform.submit_schedule_bid("d1", "discharge", 80.0)
    platform.generate_schedule(date)
    platform.submit_schedule_bid("c2", "charge", 10.0)
    platform.generate_schedule(date)
    for version in (1, 2):
        report = platform.recompute_schedule(date, version)
        assert report["consistent"] is True, report
        assert report["stored_decisions"] == report["recomputed_decisions"]


def test_schedule_survives_restart(tmp_path):
    clock = ManualClock()
    store = str(tmp_path / "state")
    first = build_platform(default_config(), clock=clock, storage_root=store, rack_count=3)
    first.start()
    date = _date_of(clock.now())
    first.submit_schedule_bid("c1", "charge", 60.0)
    first.generate_schedule(date)

    second = build_platform(default_config(), clock=clock, storage_root=store, rack_count=3)
    current = second.current_schedule()
    assert current is not None
    assert current["version"] == 1
    # 申请单也从落盘恢复，可直接在此基础上再出一版。
    assert len(second.schedule_inputs()["bids"]) == 1
    second.submit_schedule_bid("d1", "discharge", 30.0)
    again = second.generate_schedule(date)
    assert again["header"]["version"] == 2


def test_maintenance_window_excludes_cluster(platform):
    platform.start()
    date = _date_of(platform.clock.now())
    day = _day_start(date)
    platform.add_maintenance_window("rk-01", day, day + 86400, "年检")
    platform.submit_schedule_bid("c1", "charge", 1e6)
    out = platform.generate_schedule(date)
    assert out["reconciliation"]["ok"] is True
    assert all(alloc["rack_id"] != "rk-01" for alloc in out["result"]["decisions"])


def test_curtailment_zero_slot_schedules_nothing_there(platform):
    platform.start()
    date = _date_of(platform.clock.now())
    # 第一个谷时段（UTC 00:00–00:30）限充电为 0。
    platform.set_curtailment(0, import_kw=0.0)
    platform.submit_schedule_bid("c1", "charge", 1e6)
    out = platform.generate_schedule(date)
    assert not [d for d in out["result"]["decisions"] if d["slot_index"] == 0]
    assert [d for d in out["result"]["decisions"] if d["direction"] == "charge"]


def test_quota_budget_taken_from_ledger(platform):
    platform.start()
    date = _date_of(platform.clock.now())
    # 台账里先登记一部分充电用量，剩余额度变小。
    platform.quota.record_charge(1900.0, platform.clock.now())
    platform.submit_schedule_bid("c1", "charge", 1000.0)
    out = platform.generate_schedule(date)
    charge_kwh = out["result"]["totals"]["charge_kwh"]
    # 剩余充电额度 100 kWh，排程不会超出。
    assert charge_kwh <= 100.0 + 1e-6
    assert out["reconciliation"]["ok"] is True


def test_rejected_bid_carries_actionable_reason(platform):
    platform.start()
    date = _date_of(platform.clock.now())
    # 申请量远超额度，拒绝清单必须写明卡点且对账仍通过。
    platform.submit_schedule_bid("huge", "charge", 1e9)
    out = platform.generate_schedule(date)
    rejection = out["result"]["rejections"][0]
    assert rejection["reasons"]
    assert rejection["short_kwh"] > 0
    assert out["reconciliation"]["ok"] is True


def test_reserve_holds_idle_capacity_for_peak_shaving(platform):
    platform.start()
    date = _date_of(platform.clock.now())
    platform.set_reserve(charge_reserve_ratio=0.5, discharge_reserve_ratio=0.5)
    platform.submit_schedule_bid("c1", "charge", 1e6)
    out = platform.generate_schedule(date)
    slots = out["result"]["reserve"]["slots"]
    scheduled_slots = [s for s in slots if s.get("direction")]
    assert scheduled_slots
    assert all(s["used_kw"] <= _import_cap(platform, s) + 1e-6 for s in scheduled_slots)
    assert all(s["reserve_within_cap"] for s in scheduled_slots)
    assert out["result"]["reserve"]["idle_capacity_kwh"] > 0


def _import_cap(platform, slot_report) -> float:
    # 可出租功率 = 用电限额的一半（50% 余量）。
    return slot_report["grid_cap_kw"] * 0.5
