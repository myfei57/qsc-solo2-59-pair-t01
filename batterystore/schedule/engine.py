"""排程引擎：把一批申请排成可执行的簇×时段计划。

引擎是纯函数式的：同一份输入必出同一份计划，这是"改完能按时间回放"的前提。
分配顺序由归属策略（默认按生效时刻）唯一确定，同簇同槽位互斥，三道外部约束
（检修、限电、额度）与一道站内约束（保留地板、SOC 上下限）在每个槽位上就地
核对。排不进的申请不会被静默丢掉，而是带原因码与缺口电量进 ``rejections``。

能量方向换算：充电时电网侧多花一个效率损失（电池侧 = 上网电量 × 效率），
放电时电网侧少收一个效率损失（上网电量 = 电池侧 × 效率）。额度按电网侧
（实际计费）电量记账。
"""

from __future__ import annotations

from . import ownership
from .model import (
    CHARGE,
    DISCHARGE,
    HOLD,
    REASON_CLUSTER_BUSY,
    REASON_CURTAILMENT,
    REASON_ENERGY_FULL,
    REASON_MAINTENANCE,
    REASON_NO_WINDOW,
    REASON_QUOTA,
    REASON_RESERVE,
    REASON_UNKNOWN_CLUSTER,
    Allocation,
    Rejection,
)
from .state import build_states

HOUSE_REQUEST = "house-arbitrage"
_EPS = 1e-9


class Ledger:
    """槽位级全站功率占用与逐簇状态。"""

    def __init__(self, roster, slots):
        self.states = build_states(roster)
        self.used_import_kw = {slot.index: 0.0 for slot in slots}
        self.used_export_kw = {slot.index: 0.0 for slot in slots}

    def import_left_kw(self, slot, import_limit_kw: float) -> float:
        return max(0.0, import_limit_kw - self.used_import_kw[slot.index])

    def export_left_kw(self, slot, export_limit_kw: float) -> float:
        return max(0.0, export_limit_kw - self.used_export_kw[slot.index])

    def add_usage(self, slot_index: int, action: str, power_kw: float) -> None:
        if action == CHARGE:
            self.used_import_kw[slot_index] += power_kw
        else:
            self.used_export_kw[slot_index] += power_kw


def _battery_energy_for(action: str, grid_kwh: float, efficiency: float) -> float:
    return grid_kwh * efficiency if action == CHARGE else grid_kwh / efficiency


def _energy_blocker(action: str, state) -> str | None:
    """SOC／保留地板层面的阻塞原因。"""

    if action == DISCHARGE and state.discharge_headroom_kwh() <= _EPS:
        return REASON_RESERVE
    if action == CHARGE and state.charge_headroom_kwh() <= _EPS:
        return REASON_ENERGY_FULL
    return None


def _fit_in_slot(action, state, slot, want_grid_kwh, ledger, curtailment, quota):
    """在单一簇单一槽位上算出可落的（电网功率，电网电量），受四道约束取最小。"""

    cap = state.cap
    duration_h = slot.duration_s / 3600.0
    export_limit, import_limit = curtailment.limits_at(slot.start)
    if action == CHARGE:
        global_left_kw = ledger.import_left_kw(slot, import_limit)
        cluster_max_kw = cap.max_charge_kw
    else:
        global_left_kw = ledger.export_left_kw(slot, export_limit)
        cluster_max_kw = cap.max_discharge_kw
    power_cap_kw = min(cluster_max_kw, global_left_kw)

    quota_left_kwh = quota.remaining(action, slot.start)
    # 电池侧可承受的能量再换回电网侧。
    if action == CHARGE:
        battery_room = state.charge_headroom_kwh()
        energy_room_grid = battery_room / max(cap.efficiency, _EPS)
    else:
        battery_room = state.discharge_headroom_kwh()
        energy_room_grid = battery_room * cap.efficiency

    grid_kwh = min(want_grid_kwh, power_cap_kw * duration_h, quota_left_kwh, energy_room_grid)
    if grid_kwh <= _EPS:
        reasons = []
        if power_cap_kw < cap.min_power_kw:
            reasons.append(REASON_CURTAILMENT)
        if quota_left_kwh <= _EPS:
            reasons.append(REASON_QUOTA)
        if energy_room_grid <= _EPS:
            reasons.append(REASON_RESERVE if action == DISCHARGE else REASON_ENERGY_FULL)
        return 0.0, 0.0, reasons

    power_kw = grid_kwh / duration_h
    if power_kw < cap.min_power_kw:
        # 抬到最小功率会超量，宁可不在该槽位安排，避免发出低于 PCS 下限的指令。
        power_kw = cap.min_power_kw
        grid_kwh = power_kw * duration_h
        if grid_kwh > want_grid_kwh + _EPS:
            return 0.0, 0.0, [REASON_CURTAILMENT]
    return power_kw, grid_kwh, []


def solve(
    *,
    roster,
    slots,
    requests,
    maintenance,
    curtailment,
    quota,
    tariff_of_slot,
    policy: str = ownership.DEFAULT_POLICY,
    auto_charge: bool = False,
    auto_discharge: bool = False,
    auto_target_soc_pct: float = 95.0,
):
    """执行一次排程，返回分配、拒绝与保留汇总。"""

    ownership.normalize(policy)
    ledger = Ledger(roster, slots)
    allocations: list[Allocation] = []
    rejections: list[Rejection] = []

    ordered = sorted(
        (req for req in requests if req.action in (CHARGE, DISCHARGE)),
        key=lambda req: ownership.order_key(policy, req.window_start, req.submitted_at),
    )
    for request in ordered:
        if request.rack_id is not None and not roster.has(request.rack_id):
            rejections.append(Rejection(request.request_id, REASON_UNKNOWN_CLUSTER, request.energy_kwh, "点名的簇不在名册"))
            continue
        candidates = [slot for slot in slots if request.overlaps(slot.start, slot.end)]
        if not candidates:
            rejections.append(Rejection(request.request_id, REASON_NO_WINDOW, request.energy_kwh, "生效窗口内没有可排槽位"))
            continue
        target_ids = [request.rack_id] if request.rack_id else roster.ids()
        remaining = float(request.energy_kwh)
        last_reason = REASON_CLUSTER_BUSY
        last_detail = "窗口内没有足够空闲槽位容纳整单"
        for slot in candidates:
            if remaining <= 1e-6:
                break
            for rack_id in target_ids:
                if remaining <= 1e-6:
                    break
                state = ledger.states[rack_id]
                window = maintenance.blocked(rack_id, slot.start, slot.end)
                if window is not None:
                    last_reason, last_detail = REASON_MAINTENANCE, f"检修窗口: {window.reason or 'maintenance'}"
                    continue
                if state.busy(slot.index) is not None:
                    last_reason, last_detail = REASON_CLUSTER_BUSY, f"槽位 {slot.index} 已被 {state.busy(slot.index)} 占用"
                    continue
                blocker = _energy_blocker(request.action, state)
                if blocker is not None:
                    last_reason, last_detail = blocker, blocker
                    continue
                power_kw, grid_kwh, reasons = _fit_in_slot(
                    request.action, state, slot, remaining, ledger, curtailment, quota
                )
                if grid_kwh <= _EPS:
                    last_reason = reasons[0] if reasons else REASON_CURTAILMENT
                    last_detail = last_reason
                    continue
                grid_kwh = min(grid_kwh, remaining)
                power_kw = grid_kwh / (slot.duration_s / 3600.0)
                battery_kwh = _battery_energy_for(request.action, grid_kwh, state.cap.efficiency)
                if request.action == CHARGE:
                    state.apply_charge(battery_kwh)
                else:
                    state.apply_discharge(battery_kwh)
                state.place(slot.index, request.request_id, request.action)
                ledger.add_usage(slot.index, request.action, power_kw)
                quota.consume(request.action, slot.start, grid_kwh)
                allocations.append(
                    Allocation(request.request_id, rack_id, request.action, slot.index, slot.start, slot.end, power_kw, grid_kwh)
                )
                remaining -= grid_kwh
        if remaining > 1e-6:
            rejections.append(Rejection(request.request_id, last_reason, remaining, last_detail))

    if auto_charge:
        allocations.extend(_house_sweep(CHARGE, roster, slots, ledger, maintenance, curtailment, quota, tariff_of_slot))
    if auto_discharge:
        allocations.extend(_house_sweep(DISCHARGE, roster, slots, ledger, maintenance, curtailment, quota, tariff_of_slot))

    return _package(roster, slots, ledger, allocations, rejections, auto_target_soc_pct)


def _house_sweep(action, roster, slots, ledger, maintenance, curtailment, quota, tariff_of_slot):
    """站内套利：在与动作匹配的峰谷窗口里，把空闲簇尽量充满／放空（不破保留线）。"""

    wanted_kind = "valley" if action == CHARGE else "peak"
    placed: list[Allocation] = []
    for slot in slots:
        if tariff_of_slot(slot) != wanted_kind:
            continue
        for rack_id in roster.ids():
            state = ledger.states[rack_id]
            if maintenance.blocked(rack_id, slot.start, slot.end) is not None:
                continue
            if state.busy(slot.index) is not None:
                continue
            if _energy_blocker(action, state) is not None:
                continue
            if action == CHARGE:
                battery_target = state.cap.energy_max_kwh
                want_grid = (battery_target - state.energy_kwh) / max(state.cap.efficiency, _EPS)
            else:
                want_grid = state.discharge_headroom_kwh() * state.cap.efficiency
            power_kw, grid_kwh, _reasons = _fit_in_slot(action, state, slot, want_grid, ledger, curtailment, quota)
            if grid_kwh <= _EPS:
                continue
            battery_kwh = _battery_energy_for(action, grid_kwh, state.cap.efficiency)
            if action == CHARGE:
                state.apply_charge(battery_kwh)
            else:
                state.apply_discharge(battery_kwh)
            state.place(slot.index, HOUSE_REQUEST, action)
            ledger.add_usage(slot.index, action, power_kw)
            quota.consume(action, slot.start, grid_kwh)
            placed.append(
                Allocation(HOUSE_REQUEST, rack_id, action, slot.index, slot.start, slot.end, power_kw, grid_kwh)
            )
    return placed


def _package(roster, slots, ledger, allocations, rejections, auto_target_soc_pct):
    """汇总每簇保留余量与未安排槽位（hold）。"""

    holds = []
    reserve_rows = []
    for rack_id in roster.ids():
        state = ledger.states[rack_id]
        reserve_energy = state.reserve_energy_kwh()
        reserve_rows.append(
            {
                "rack_id": rack_id,
                "reserve_soc_pct": round(state.cap.reserve_soc_pct, 3),
                "reserve_energy_kwh": round(reserve_energy, 3),
                "planned_end_energy_kwh": round(state.energy_kwh, 3),
                "reserve_headroom_kwh": round(max(0.0, state.energy_kwh - reserve_energy), 3),
            }
        )
        for slot in slots:
            if state.busy(slot.index) is None:
                holds.append(
                    Allocation("", rack_id, HOLD, slot.index, slot.start, slot.end, 0.0, 0.0).as_dict()
                )
    return {
        "allocations": allocations,
        "rejections": rejections,
        "holds": holds,
        "reserve": reserve_rows,
        "states": ledger.states,
        "used_import_kw": ledger.used_import_kw,
        "used_export_kw": ledger.used_export_kw,
        "auto_target_soc_pct": auto_target_soc_pct,
    }
