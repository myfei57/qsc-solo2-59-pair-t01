"""端到端归属口径：跨零点谷窗口下两种口径排出相反的赢家。"""

from __future__ import annotations

import calendar
import time

from batterystore.schedule.constraints import GridCurtailment, QuotaBudget, ReservePolicy
from batterystore.schedule.models import Bid, ClusterContext
from batterystore.schedule.planner import Planner
from batterystore.schedule.timegrid import build_slots


def _utc(date: str, hour: int) -> float:
    return float(calendar.timegm(time.strptime(f"{date} {hour:02d}:00:00", "%Y-%m-%d %H:%M:%S")))


def _valley_kind(ts: float) -> str:
    hour = int((ts % 86400.0) // 3600.0)
    return "valley" if hour >= 22 or hour < 8 else "peak"


def _build():
    window_start = _utc("2025-09-16", 22)
    slots = build_slots(window_start, 4.0, _valley_kind, slot_s=3600.0)
    # 单簇：500kWh、SOC 50→90 只能再进 200kWh，两个申请只能满足一个。
    clusters = (ClusterContext("rk-01", 50.0, 10.0, 90.0, 500.0, 100.0, False, False),)
    bids = [
        Bid(
            "overnight-reserve",
            "charge",
            200.0,
            submitted_at=_utc("2025-09-16", 21),
            effective_at=_utc("2025-09-16", 23),
        ),
        Bid(
            "intraday-rush",
            "charge",
            200.0,
            submitted_at=_utc("2025-09-17", 0) + 60.0,
            effective_at=window_start,
        ),
    ]

    def make(policy: str) -> Planner:
        return Planner(
            slots,
            clusters,
            curtailment=GridCurtailment(1e5, 1e5),
            quota=QuotaBudget(1e9, 1e9),
            reserve=ReservePolicy(0.0, 0.0),
            policy=policy,
        )

    return make, bids


def _scheduled(result, bid_id: str) -> float:
    return round(sum(a.energy_kwh for a in result.decisions if a.bid_id == bid_id), 6)


def test_submit_policy_favours_overnight_reserve():
    make, bids = _build()
    result = make("submit").plan(bids, plan_date="2025-09-16", version=1, generated_at=0.0)
    assert _scheduled(result, "overnight-reserve") == 200.0
    assert _scheduled(result, "intraday-rush") == 0.0
    assert {item.bid_id for item in result.rejections} == {"intraday-rush"}


def test_effective_policy_favours_intraday_rush():
    make, bids = _build()
    result = make("effective").plan(bids, plan_date="2025-09-16", version=1, generated_at=0.0)
    assert _scheduled(result, "intraday-rush") == 200.0
    assert _scheduled(result, "overnight-reserve") == 0.0
    assert {item.bid_id for item in result.rejections} == {"overnight-reserve"}


def test_policy_is_recorded_in_version_and_audit(platform):
    platform.start()
    date = time.strftime("%Y-%m-%d", time.gmtime(platform.clock.now()))
    platform.submit_schedule_bid("c1", "charge", 10.0)
    out = platform.generate_schedule(date, policy="effective")
    assert out["result"]["policy"] == "effective"
    assert out["header"]["policy"] == "effective"
    events = [event.as_dict() for event in platform.bus.history("schedule")]
    generated = [event for event in events if event["action"] == "schedule_generated"]
    assert generated and generated[-1]["payload"]["policy"] == "effective"
