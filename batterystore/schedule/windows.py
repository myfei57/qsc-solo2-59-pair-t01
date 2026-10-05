"""电价窗口与时段网格。

排程把计划周期切成等长的离散时段，并在时段上标出峰／谷。峰谷的小时定义直接
复用 :mod:`batterystore.quota.tariff` 的常量，排程看到的谷段就是配额结算看到
的谷段，不会出现"按谷价排进去、结算时却是平价"的两张表。

网格支持任意起点与跨天区间：槽位边界用绝对时间戳表示，跨零点只是两个相邻槽
位，归属判定因此不受日期翻转影响。
"""

from __future__ import annotations

from dataclasses import dataclass

from ..quota.tariff import PEAK_END_HOUR, PEAK_START_HOUR
from .model import Slot

DAY_S = 86400.0
HOUR_S = 3600.0


@dataclass(frozen=True)
class TariffWindow:
    """一个峰／谷窗口（左闭右开的绝对时间区间）。"""

    kind: str
    start: float
    end: float

    def as_dict(self) -> dict:
        return {"kind": self.kind, "start": round(self.start, 3), "end": round(self.end, 3)}


def midnight_of(ts: float) -> float:
    """返回 ``ts`` 所在 UTC 日的零点时间戳。"""

    return float((int(ts) // int(DAY_S)) * int(DAY_S))


def tariff_kind_at(ts: float) -> str:
    """按绝对时刻返回峰谷名，跨零点时小时数自动回绕。"""

    hour = int((float(ts) % DAY_S) // HOUR_S)
    return "peak" if PEAK_START_HOUR <= hour < PEAK_END_HOUR else "valley"


def day_tariff_windows(day_start: float) -> list[TariffWindow]:
    """给出某天零点起的两个峰谷窗口：谷（凌晨）、峰（白天）、谷（夜间）。"""

    midnight = midnight_of(day_start)
    peak_start = midnight + PEAK_START_HOUR * HOUR_S
    peak_end = midnight + PEAK_END_HOUR * HOUR_S
    next_midnight = midnight + DAY_S
    return [
        TariffWindow("valley", midnight, peak_start),
        TariffWindow("peak", peak_start, peak_end),
        TariffWindow("valley", peak_end, next_midnight),
    ]


def windows_for_range(start: float, end: float) -> list[TariffWindow]:
    """覆盖任意（可跨多天）区间的峰谷窗口，按时间升序。"""

    if end <= start:
        return []
    first = midnight_of(start)
    last = midnight_of(end - 1.0)
    windows: list[TariffWindow] = []
    day = first
    while day <= last:
        for window in day_tariff_windows(day):
            if window.end > start and window.start < end:
                windows.append(
                    TariffWindow(
                        window.kind,
                        max(window.start, start),
                        min(window.end, end),
                    )
                )
        day += DAY_S
    return windows


def build_slots(start: float, end: float, slot_s: float) -> list[Slot]:
    """把区间切成等长槽位，最后一段截到 ``end``。"""

    if slot_s <= 0:
        raise ValueError("槽位时长必须为正")
    if end <= start:
        return []
    slots: list[Slot] = []
    cursor = float(start)
    index = 0
    while cursor < end:
        nxt = min(cursor + float(slot_s), float(end))
        slots.append(Slot(index=index, start=cursor, end=nxt))
        cursor = nxt
        index += 1
    return slots


def slot_tariff(slot: Slot) -> str:
    """返回槽位所属峰谷（以槽位中点时刻计，避免边界抖动）。"""

    return tariff_kind_at((slot.start + slot.end) / 2.0)
