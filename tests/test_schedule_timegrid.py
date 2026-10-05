"""排程时间网格与跨零点窗口。"""

from __future__ import annotations

import time

from batterystore.quota.tariff import PEAK_END_HOUR, PEAK_START_HOUR, Tariff
from batterystore.config import default_config
from batterystore.schedule.timegrid import (
    DAY_SECONDS,
    build_slots,
    calendar_day_start,
    day_slots,
    merge_windows,
    window_crosses_midnight,
)


def _kind(ts: float) -> str:
    return Tariff(default_config().quota).kind(ts)


def test_day_slots_cover_full_day_and_align_to_utc_midnight():
    slots = day_slots("2025-09-16", _kind, slot_s=1800.0)
    assert len(slots) == 48
    assert slots[0].start == calendar_day_start("2025-09-16")
    assert slots[-1].end == slots[0].start + DAY_SECONDS
    assert all(slot.duration_h == 0.5 for slot in slots)
    # 谷价窗口覆盖 UTC 凌晨小时。
    assert all(slot.kind == "valley" for slot in slots if slot.start < slots[0].start + PEAK_START_HOUR * 3600)
    assert all(slot.kind == "peak" for slot in slots[PEAK_START_HOUR * 2:PEAK_END_HOUR * 2])


def test_merge_windows_marks_cross_midnight_valley():
    # 取一段从 23:00 跨到次日 09:00 的视野，谷窗口必须跨零点。
    start = calendar_day_start("2025-09-16") + 23 * 3600
    slots = build_slots(start, 10.0, _kind, slot_s=1800.0)
    windows = merge_windows(slots)
    valley = [window for window in windows if window["kind"] == "valley"]
    assert valley, windows
    crossing = [window for window in valley if window["crosses_midnight"]]
    assert crossing, windows
    assert crossing[0]["plan_date_start"] == "2025-09-16"
    assert crossing[0]["plan_date_end"] == "2025-09-17"


def test_window_cross_midnight_boundary():
    midnight = calendar_day_start("2025-09-17")
    assert window_crosses_midnight(midnight - 1800, midnight + 1800) is True
    assert window_crosses_midnight(midnight, midnight + 3600) is False
