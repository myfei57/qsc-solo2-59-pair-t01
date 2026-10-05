"""同簇竞争的归属策略。

同一段时间，一个簇只能被一条计划占用。两条申请盯上同一簇同一时段时，谁拿到，
有两种朴素做法：

- **按提交时刻（``submit``）**：谁先递单子谁占。归属依据是"递交先后"，和真正
  要占的物理时段是两个维度。跨零点那天，夜里递交的单与凌晨递交的单在时间轴上
  首尾相接，仅靠递交时刻排序会让后补的单凭"递得早"长期霸占黄金段，也会让空闲
  簇排不进本该排的套利。
- **按生效时刻（``effective``，本系统采用）**：归属锚定在"哪条申请的生效时间
  更早到达这台设备"。比较的是生效区间起点的**绝对时间戳**，跨零点天然单调：
  同一份输入无论从哪一天的哪个时刻重算，先后次序都一致，不会跨零点翻案。仅当
  两条申请生效区间完全相同（起点也相同）时，才退回提交时刻先到先得，作为无
  歧义的平局裁决。

之所以选生效时刻：排程要回答的是"这个物理时段归谁"，判定键就必须是物理时段
本身；提交时刻属于流程信息，只配在物理时段无法区分时出场。
"""

from __future__ import annotations

EFFECTIVE = "effective"
SUBMIT = "submit"
POLICIES = (EFFECTIVE, SUBMIT)

# 默认策略：按生效时刻归属。
DEFAULT_POLICY = EFFECTIVE


def normalize(policy: str) -> str:
    if policy not in POLICIES:
        raise ValueError(f"未知归属策略: {policy}")
    return policy


def order_key(policy: str, window_start: float, submitted_at: float):
    """返回某条申请在给定策略下的全序排序键（升序，靠前的先选簇）。

    生效时刻策略以生效起点为主键、提交时刻为平局裁决；提交时刻策略反过来。
    两个键都是绝对时间戳，跨零点不会出现次序翻转。
    """

    normalize(policy)
    if policy == SUBMIT:
        return (submitted_at, window_start)
    return (window_start, submitted_at)


def describe(policy: str) -> dict:
    """给出策略说明，便于在排程结果里交代归属依据。"""

    normalize(policy)
    return {
        "policy": policy,
        "primary": "window_start" if policy == EFFECTIVE else "submitted_at",
        "tie_breaker": "submitted_at" if policy == EFFECTIVE else "window_start",
        "midnight_safe": policy == EFFECTIVE,
        "rationale": (
            "归属锚定物理生效时段，绝对时间戳比较保证跨零点重算结果一致"
            if policy == EFFECTIVE
            else "归属锚定递交先后，跨零点重算可能因时间轴首尾相接而翻案"
        ),
    }
