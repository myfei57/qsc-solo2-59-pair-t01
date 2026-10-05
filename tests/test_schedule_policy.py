"""冲突归属口径：按提交时刻还是按生效时刻。

这组用例把"跨零点算出来不一样"固定成可执行事实：同两条申请、同一个跨零点
谷窗口，``submit`` 与 ``effective`` 排出相反的归属。默认口径必须是
``submit``，理由见 :func:`batterystore.schedule.models.preferred_policy`。
"""

from __future__ import annotations

import calendar
import time

from batterystore.schedule.models import (
    POLICY_EFFECTIVE,
    POLICY_SUBMIT,
    Bid,
    preferred_policy,
)
from batterystore.schedule.reservation import explain_cross_midnight, order_bids


def _utc(date: str, hour: int) -> float:
    return float(calendar.timegm(time.strptime(f"{date} {hour:02d}:00:00", "%Y-%m-%d %H:%M:%S")))


# 谷窗口：2025-09-16 22:00 起，跨零点到 2025-09-17 08:00。
WINDOW_START = _utc("2025-09-16", 22)
MIDNIGHT = _utc("2025-09-17", 0)


def test_default_policy_is_submit():
    assert preferred_policy() == POLICY_SUBMIT


def test_same_window_no_crossing_policies_agree():
    early = Bid("a", "charge", 10.0, submitted_at=100.0, effective_at=500.0)
    late = Bid("b", "charge", 10.0, submitted_at=200.0, effective_at=600.0)
    submit = [bid.bid_id for bid in order_bids([early, late], 1000.0, POLICY_SUBMIT)]
    effective = [bid.bid_id for bid in order_bids([early, late], 1000.0, POLICY_EFFECTIVE)]
    assert submit == effective == ["a", "b"]


def test_cross_midnight_policies_diverge():
    # 隔夜预留单：头一天 21:00 就提交，计划 23:00 开始充。
    reserved = Bid(
        "overnight-reserve",
        "charge",
        10.0,
        submitted_at=WINDOW_START - 3600.0,
        effective_at=WINDOW_START + 3600.0,
    )
    # 当天临时单：运营日零点后才提交（提交晚），但把生效时刻报到窗口起点
    # 22:00（比预留单的生效时刻更早），模拟"当天单子抢在窗口第一刻生效"。
    intraday = Bid(
        "intraday-rush",
        "charge",
        10.0,
        submitted_at=MIDNIGHT + 60.0,
        effective_at=WINDOW_START,
    )
    submit_order = [bid.bid_id for bid in order_bids([reserved, intraday], WINDOW_START, POLICY_SUBMIT)]
    effective_order = [bid.bid_id for bid in order_bids([reserved, intraday], WINDOW_START, POLICY_EFFECTIVE)]

    # 按提交时刻：隔夜预留单头一天就报，先到先得。
    assert submit_order == ["overnight-reserve", "intraday-rush"]
    # 按生效时刻：临时单生效在窗口第一刻，反而压过隔夜预留单。
    assert effective_order == ["intraday-rush", "overnight-reserve"]

    explanation = explain_cross_midnight(reserved, intraday, WINDOW_START)
    assert explanation["diverges"] is True
    assert explanation["submit_winner"] == "overnight-reserve"
    assert explanation["effective_winner"] == "intraday-rush"


def test_effective_policy_uses_window_start_when_effective_omitted():
    bid = Bid("x", "discharge", 1.0, submitted_at=MIDNIGHT)
    # 未给生效时刻时，生效口径回退到窗口起点；窗口跨零点也不抛错。
    ordered = order_bids([bid], WINDOW_START, POLICY_EFFECTIVE)
    assert ordered[0].effective_or(WINDOW_START) == WINDOW_START


def test_tie_break_is_deterministic_on_bid_id():
    a = Bid("z", "charge", 1.0, submitted_at=100.0)
    b = Bid("a", "charge", 1.0, submitted_at=100.0)
    assert [bid.bid_id for bid in order_bids([a, b], 0.0, POLICY_SUBMIT)] == ["a", "z"]
