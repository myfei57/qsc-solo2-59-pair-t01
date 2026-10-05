"""排程时间网格。

把一个排程视野切成等长时段（默认半小时），并按峰谷电价给每段打上
``peak``／``valley`` 标记。充电只排在谷段、放电只排在峰段；连续同价的时段
合并成一个"窗口"（谷窗口、峰窗口），申请单按窗口竞争。

时间轴一律用 UTC 秒，与 :class:`batterystore.quota.tariff.Tariff` 的口径
一致：电价按 ``now % 86400`` 算小时，因此窗口起点天然按 UTC 零点对齐。谷价
窗口（22:00–次日 08:00）跨过午夜，:func:`window_crosses_midnight` 与
:func:`merge_windows` 显式标注这一点，跨零点归属差异就是针对这种窗口。
"""

from __future__ import annotations

import time
from typing import Callable

from .models import Slot

DAY_SECONDS = 86_400.0
DEFAULT_SLOT_S = 1_800.0


def day_start(ts: float) -> float:
    """返回 ``ts`` 所在 UTC 日的零点秒数。"""

    return float(ts) - float(ts) % DAY_SECONDS


def plan_date_of(ts: float) -> str:
    """返回 ``ts`` 对应的 UTC 运营日（``YYYY-MM-DD``）。"""

    return time.strftime("%Y-%m-%d", time.gmtime(float(ts)))


def build_slots(
    horizon_start: float,
    hours: float,
    tariff_kind: Callable[[float], str],
    slot_s: float = DEFAULT_SLOT_S,
) -> tuple[Slot, ...]:
    """把 ``[horizon_start, horizon_start + hours)`` 切成时段并打电价标记。"""

    if hours <= 0:
        raise ValueError("排程视野长度必须为正")
    if slot_s <= 0:
        raise ValueError("时段长度必须为正")
    total = int(round(hours * 3600.0 / slot_s))
    start = float(horizon_start)
    slots: list[Slot] = []
    for index in range(total):
        begin = start + index * slot_s
        slots.append(Slot(index=index, start=begin, end=begin + slot_s, kind=tariff_kind(begin)))
    return tuple(slots)


def day_slots(
    plan_date: str,
    tariff_kind: Callable[[float], str],
    slot_s: float = DEFAULT_SLOT_S,
) -> tuple[Slot, ...]:
    """按运营日字符串构造该 UTC 日 0 点到 24 点的全天时段。

    经 :meth:`calendar_day_start` 保证起点按 UTC 零点对齐，峰谷电价才不会因
    部署机器的本地时区而整体平移。
    """

    return build_slots(calendar_day_start(plan_date), 24.0, tariff_kind, slot_s)


def calendar_day_start(plan_date: str) -> float:
    """把运营日字符串解析成 UTC 零点秒数。"""

    import calendar

    parsed = time.strptime(plan_date, "%Y-%m-%d")
    return float(calendar.timegm(parsed))


def window_crosses_midnight(start: float, end: float) -> bool:
    """窗口的起止是否落在不同 UTC 日（即跨过零点）。"""

    return day_start(start) != day_start(end - 1e-6)


def merge_windows(slots: tuple[Slot, ...]) -> tuple[dict, ...]:
    """把连续同价时段合并成窗口，标注是否跨零点。"""

    windows: list[dict] = []
    current: dict | None = None
    for slot in slots:
        if current is None or slot.kind != current["kind"] or abs(slot.start - current["end"]) > 1e-6:
            if current is not None:
                windows.append(current)
            current = {"kind": slot.kind, "start": slot.start, "end": slot.end, "slot_indexes": []}
        current["end"] = slot.end
        current["slot_indexes"].append(slot.index)
    if current is not None:
        windows.append(current)
    for window in windows:
        window["crosses_midnight"] = window_crosses_midnight(window["start"], window["end"])
        window["plan_date_start"] = plan_date_of(window["start"])
        window["plan_date_end"] = plan_date_of(window["end"] - 1e-6)
    return tuple(windows)
