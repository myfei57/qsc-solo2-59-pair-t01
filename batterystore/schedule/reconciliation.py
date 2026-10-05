"""排程结果对账。

方案排完之后，要能和检修计划、限电限制、充放电额度三处独立地对得上：这里不
信任排程器的自我声明，而是从结果里的分配逐条反算，任何一处越界都列成违约项。
对账也校验排程自身的物理一致性（同一簇同一时段不重复占用、SOC 不出窗口、
充只在谷段、放只在峰段、能量等于功率乘时长）。

返回的报告可直接落审计与控制台展示：值班员看到的是"第几条、在哪、超了哪处
约束、超多少"，而不是一句笼统的不一致。
"""

from __future__ import annotations

from .constraints import GridCurtailment, MaintenanceWindow, QuotaBudget, ReservePolicy
from .models import CHARGE, DISCHARGE, Allocation, ClusterContext, ScheduleResult, Slot

_EPS = 1e-4


def reconcile(
    result: ScheduleResult,
    *,
    maintenance: tuple[MaintenanceWindow, ...] = (),
    curtailment: GridCurtailment,
    quota: QuotaBudget,
    reserve: ReservePolicy | None = None,
) -> dict:
    """对一版方案逐项核对，返回对账报告。"""

    reserve = reserve or ReservePolicy()
    slots = {slot.index: slot for slot in result.slots}
    clusters = {cluster.rack_id: cluster for cluster in result.clusters}
    violations: list[str] = []

    # 每个时段每个方向的全站功率；每个簇每个时段的占用。
    slot_power: dict[tuple[int, str], float] = {}
    slot_owner: dict[tuple[int, str], str] = {}
    totals = {CHARGE: 0.0, DISCHARGE: 0.0}

    for alloc in result.decisions:
        slot = slots.get(alloc.slot_index)
        cluster = clusters.get(alloc.rack_id)
        if slot is None:
            violations.append(f"{alloc.bid_id}:{alloc.rack_id}:slot_{alloc.slot_index}_missing")
            continue
        if cluster is None:
            violations.append(f"{alloc.bid_id}:{alloc.rack_id}:cluster_unknown")
            continue
        violations.extend(_check_allocation(alloc, slot, cluster))
        # 检修。
        if any(
            window.rack_id == alloc.rack_id and window.overlaps(slot.start, slot.end)
            for window in maintenance
        ):
            violations.append(f"{alloc.bid_id}:{alloc.rack_id}:slot_{alloc.slot_index}:scheduled_in_maintenance")
        # 同簇同时段重复占用。
        owner_key = (alloc.slot_index, alloc.rack_id)
        existing = slot_owner.get(owner_key)
        if existing is not None and existing != alloc.bid_id:
            violations.append(f"{alloc.rack_id}:slot_{alloc.slot_index}:double_booked")
        slot_owner[owner_key] = alloc.bid_id
        # 全站功率聚合。
        slot_power[(alloc.slot_index, alloc.direction)] = (
            slot_power.get((alloc.slot_index, alloc.direction), 0.0) + alloc.power_kw
        )
        totals[alloc.direction] += alloc.energy_kwh

    # 限电（扣余量前后都核对：功率绝不超并网点硬限额，且不占用调峰余量）。
    for (slot_index, direction), used in slot_power.items():
        hard_cap = curtailment.cap_kw(direction, slot_index)
        if used > hard_cap + _EPS:
            violations.append(
                f"slot_{slot_index}:{direction}:aggregate_power_{round(used, 2)}_over_curtail_{hard_cap}"
            )
        reservable = reserve.reservable_kw(direction, hard_cap)
        if used > reservable + _EPS:
            violations.append(
                f"slot_{slot_index}:{direction}:reserve_breached_{round(used - reservable, 2)}kw"
            )

    # 额度。
    if totals[CHARGE] > quota.cap_for(CHARGE) + _EPS:
        violations.append(f"charge_quota_{round(totals[CHARGE], 2)}_over_{quota.cap_for(CHARGE)}")
    if totals[DISCHARGE] > quota.cap_for(DISCHARGE) + _EPS:
        violations.append(f"discharge_quota_{round(totals[DISCHARGE], 2)}_over_{quota.cap_for(DISCHARGE)}")

    # 未排满的申请必须在拒绝清单里有记录，反之拒绝清单只能引用真实申请。
    _check_rejections(result, violations)

    return {
        "ok": not violations,
        "plan_date": result.plan_date,
        "version": result.version,
        "policy": result.policy,
        "violations": violations,
        "counts": {
            "allocations": len(result.decisions),
            "rejections": len(result.rejections),
            "charge_kwh": round(totals[CHARGE], 6),
            "discharge_kwh": round(totals[DISCHARGE], 6),
        },
        "quota": quota.as_dict(),
        "curtailment": curtailment.as_dict(),
    }


def _check_allocation(alloc: Allocation, slot: Slot, cluster: ClusterContext) -> list[str]:
    """核对单条分配的物理一致性。"""

    issues: list[str] = []
    prefix = f"{alloc.bid_id}:{alloc.rack_id}:slot_{alloc.slot_index}"
    expected_kind = "valley" if alloc.direction == CHARGE else "peak"
    if slot.kind != expected_kind:
        issues.append(f"{prefix}:{alloc.direction}_in_{slot.kind}_window")
    if alloc.power_kw > cluster.max_power_kw + _EPS:
        issues.append(f"{prefix}:power_{alloc.power_kw}_over_device_{cluster.max_power_kw}")
    if cluster.maintained:
        issues.append(f"{prefix}:cluster_marked_maintained")
    expected_energy = alloc.power_kw * slot.duration_h
    if abs(expected_energy - alloc.energy_kwh) > 1e-3:
        issues.append(f"{prefix}:energy_mismatch_{alloc.energy_kwh}_vs_{round(expected_energy, 6)}")
    if alloc.power_kw < -_EPS or alloc.energy_kwh < -_EPS:
        issues.append(f"{prefix}:negative_value")
    return issues


def _check_rejections(result: ScheduleResult, violations: list[str]) -> None:
    """拒绝清单必须与实际排程量对得上，且每条都给出卡点原因。"""

    for rejection in result.rejections:
        got = result.bid_scheduled_kwh(rejection.bid_id)
        if abs(got - rejection.scheduled_kwh) > 1e-3:
            violations.append(
                f"{rejection.bid_id}:rejection_scheduled_mismatch_{got}_vs_{rejection.scheduled_kwh}"
            )
        if rejection.status not in ("partial", "rejected"):
            violations.append(f"{rejection.bid_id}:bad_rejection_status_{rejection.status}")
        if not rejection.reasons:
            violations.append(f"{rejection.bid_id}:rejection_without_reason")
