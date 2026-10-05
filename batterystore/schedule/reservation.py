"""冲突归属与申请排序。

同一簇在同一时段只能被一条申请占用。当两条申请同时盯上同一簇时，归属口径在
这里唯一收口：``submit`` 看提交墙上时刻，``effective`` 看申请在窗口内的
生效时刻。两者在窗口不跨零点时结论一致；一旦谷窗口跨过运营日零点，同一对
单子按两种口径会排出相反的归属，这正是必须明确选一种的原因。

排序键附带 ``bid_id`` 作为最后的确定性裁决：时刻相同的两条单子，按标识
字典序固定先后，保证同一输入在任何机器上重算结果一致（回放的前提）。
"""

from __future__ import annotations

from .models import POLICIES, POLICY_EFFECTIVE, POLICY_SUBMIT, Bid


def precedence_timestamp(bid: Bid, window_start: float, policy: str) -> float:
    """按归属口径返回这条单子的裁决时刻。

    * ``submit``：提交墙上时刻，单调不随零点回退，跨零点稳定；
    * ``effective``：窗口内生效时刻，未显式给出时取窗口起点（可能已跨零点）。
    """

    if policy == POLICY_SUBMIT:
        return float(bid.submitted_at)
    if policy == POLICY_EFFECTIVE:
        return bid.effective_or(window_start)
    raise ValueError(f"未知的冲突归属口径: {policy}，可选 {POLICIES}")


def order_bids(bids: list[Bid], window_start: float, policy: str) -> list[Bid]:
    """按归属口径把申请排成先后（先排的先占簇）。"""

    return sorted(bids, key=lambda bid: (precedence_timestamp(bid, window_start, policy), bid.bid_id))


def explain_cross_midnight(bid_a: Bid, bid_b: Bid, window_start: float) -> dict:
    """给出同一对单子在两种口径下跨零点的裁决差异，供界面说明与审计。"""

    submit_order = [bid.bid_id for bid in order_bids([bid_a, bid_b], window_start, POLICY_SUBMIT)]
    effective_order = [bid.bid_id for bid in order_bids([bid_a, bid_b], window_start, POLICY_EFFECTIVE)]
    return {
        "window_start": round(float(window_start), 3),
        "submit_winner": submit_order[0],
        "effective_winner": effective_order[0],
        "diverges": submit_order != effective_order,
        "submit_order": submit_order,
        "effective_order": effective_order,
    }
