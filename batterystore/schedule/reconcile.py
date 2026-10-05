"""排后对账：成稿计划回头核对检修、限电、额度。

排程时引擎已经守过一遍约束，对账独立地从成稿的分配出发重新累计，用来回答
"这份计划和三处台账到底对得上吗"。重算而不是复用引擎中间状态，才能在计划被
外部改动（人工调整、历史版本重放）后仍然发现越界。

输出四类结果：检修冲突、限电越限、额度超用、保留破线。全部为空才算对得上。
"""

from __future__ import annotations

from .model import CHARGE, DISCHARGE, ConstraintBreach

SOURCE_MAINTENANCE = "maintenance"
SOURCE_CURTAILMENT = "curtailment"
SOURCE_QUOTA = "quota"
SOURCE_RESERVE = "reserve"


def reconcile(*, allocations, roster, slots, maintenance, curtailment, quota_policy):
    """核对一份成稿计划，返回越界清单与分项核对表。"""

    breaches: list[ConstraintBreach] = []
    slot_by_index = {slot.index: slot for slot in slots}

    import_by_slot: dict[int, float] = {}
    export_by_slot: dict[int, float] = {}
    charge_by_period: dict[str, float] = {}
    discharge_by_period: dict[str, float] = {}
    end_energy: dict[str, float] = {}

    for allocation in allocations:
        slot = slot_by_index.get(allocation.slot_index)
        if slot is None:
            breaches.append(
                ConstraintBreach(SOURCE_MAINTENANCE, allocation.rack_id, "引用了不存在的槽位", allocation.energy_kwh)
            )
            continue
        rack_id = allocation.rack_id
        if not roster.has(rack_id):
            breaches.append(ConstraintBreach(SOURCE_MAINTENANCE, rack_id, "分配给了名册外的簇", allocation.energy_kwh))
            continue

        window = maintenance.blocked(rack_id, slot.start, slot.end)
        if window is not None:
            breaches.append(
                ConstraintBreach(SOURCE_MAINTENANCE, rack_id, f"占用了检修窗口（{window.reason or 'maintenance'}）", allocation.energy_kwh)
            )

        if allocation.action == CHARGE:
            import_by_slot[slot.index] = import_by_slot.get(slot.index, 0.0) + allocation.power_kw
            key = quota_policy.period_key(slot.start)
            charge_by_period[key] = charge_by_period.get(key, 0.0) + allocation.energy_kwh
        elif allocation.action == DISCHARGE:
            export_by_slot[slot.index] = export_by_slot.get(slot.index, 0.0) + allocation.power_kw
            key = quota_policy.period_key(slot.start)
            discharge_by_period[key] = discharge_by_period.get(key, 0.0) + allocation.energy_kwh

    # 逐簇末态必须按槽位时间顺序演化：白天先放后充与先充后放末态不同，
    # 不能按分配在列表里的出现顺序累加。
    valid = [a for a in allocations if a.slot_index in slot_by_index and roster.has(a.rack_id)]
    valid.sort(key=lambda a: (a.rack_id, a.slot_index))
    end_energy: dict[str, float] = {}
    for allocation in valid:
        cap = roster.get(allocation.rack_id)
        state = end_energy.get(allocation.rack_id)
        if state is None:
            span = cap.energy_max_kwh - cap.energy_min_kwh
            state = cap.energy_min_kwh + span * max(0.0, min(1.0, cap.soc_pct / 100.0))
        if allocation.action == CHARGE:
            state += allocation.energy_kwh * cap.efficiency
        else:
            state -= allocation.energy_kwh / cap.efficiency
        end_energy[allocation.rack_id] = state

    for index, total_kw in import_by_slot.items():
        export_limit, import_limit = curtailment.limits_at(slot_by_index[index].start)
        if total_kw - import_limit > 1e-6:
            breaches.append(ConstraintBreach(SOURCE_CURTAILMENT, f"slot-{index}", "用电功率超过限电上限", total_kw - import_limit))
    for index, total_kw in export_by_slot.items():
        export_limit, _import_limit = curtailment.limits_at(slot_by_index[index].start)
        if total_kw - export_limit > 1e-6:
            breaches.append(ConstraintBreach(SOURCE_CURTAILMENT, f"slot-{index}", "上网功率超过限电上限", total_kw - export_limit))

    _check_quota(breaches, CHARGE, charge_by_period, quota_policy)
    _check_quota(breaches, DISCHARGE, discharge_by_period, quota_policy)

    for rack_id, energy in end_energy.items():
        cap = roster.get(rack_id)
        reserve = cap.energy_max_kwh * cap.reserve_soc_pct / 100.0
        if energy < reserve - 1e-6:
            breaches.append(ConstraintBreach(SOURCE_RESERVE, rack_id, "计划末态跌破临时调峰保留地板", reserve - energy))
        if energy > cap.energy_max_kwh + 1e-6:
            breaches.append(ConstraintBreach(SOURCE_RESERVE, rack_id, "计划末态超过簇容量上限", energy - cap.energy_max_kwh))

    return {
        "ok": not breaches,
        "breaches": [breach.as_dict() for breach in breaches],
        "checks": {
            SOURCE_MAINTENANCE: not any(b.source == SOURCE_MAINTENANCE for b in breaches),
            SOURCE_CURTAILMENT: not any(b.source == SOURCE_CURTAILMENT for b in breaches),
            SOURCE_QUOTA: not any(b.source == SOURCE_QUOTA for b in breaches),
            SOURCE_RESERVE: not any(b.source == SOURCE_RESERVE for b in breaches),
        },
        "totals": {
            "import_kw_by_slot": {str(k): round(v, 3) for k, v in sorted(import_by_slot.items())},
            "export_kw_by_slot": {str(k): round(v, 3) for k, v in sorted(export_by_slot.items())},
            "charge_kwh_by_period": {k: round(v, 6) for k, v in sorted(charge_by_period.items())},
            "discharge_kwh_by_period": {k: round(v, 6) for k, v in sorted(discharge_by_period.items())},
        },
    }


def _check_quota(breaches, action, totals, quota_policy):
    cap = quota_policy.capacity(action)
    for key, total in totals.items():
        if total - cap > 1e-6:
            breaches.append(ConstraintBreach(SOURCE_QUOTA, key, f"{action} 电量超过周期额度", total - cap))
