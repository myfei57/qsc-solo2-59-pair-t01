"""排程器：把申请单排成可执行方案。

排程器是纯计算：读入时段、簇能力快照、申请单与四类外部约束，产出
:class:`ScheduleResult`，不直接驱动任何设备。这样它既能在控制循环里被反复
调用（"如果这样排会怎样"），也能在历史回放时用当年的输入原样重算。

竞争规则（"谁充谁放、抢同一簇怎么办"）：

1. 每个时段按电价定性：谷段只排充、峰段只排放。
2. 同一方向的申请单按冲突归属口径（``submit``／``effective``）全局排定
   优先级，先者先占；优先级时刻相同时按申请单标识兜底，结果确定性可复算。
3. 一条申请单可跨时段、跨簇拆分；每个时段内按"最需要的簇先上"（充电选最空、
   放电选最满）选簇，同一簇同一时段只能被一条申请占用。
4. 每层约束都参与扣减：簇 SOC 余量、簇额定功率、全站限电（扣除临时调峰余量
   后的可出租功率）、充放电额度。先到先得，后到的单子拿不到就进拒绝清单并
   写明卡点，不做"先用了再说"。
"""

from __future__ import annotations

from .constraints import GridCurtailment, MaintenanceWindow, QuotaBudget, ReservePolicy
from .models import (
    CHARGE,
    DISCHARGE,
    IDLE,
    PARTIAL,
    REJECTED,
    SCHEDULED,
    Allocation,
    Bid,
    ClusterContext,
    Rejection,
    ScheduleResult,
    Slot,
)
from .reservation import order_bids

_EPS = 1e-6


class Planner:
    """把一组申请单在给定视野内排成簇级方案。"""

    def __init__(
        self,
        slots: tuple[Slot, ...],
        clusters: tuple[ClusterContext, ...],
        *,
        maintenance: tuple[MaintenanceWindow, ...] = (),
        curtailment: GridCurtailment | None = None,
        quota: QuotaBudget | None = None,
        reserve: ReservePolicy | None = None,
        policy: str = "submit",
    ) -> None:
        if not slots:
            raise ValueError("排程时段不能为空")
        if not clusters:
            raise ValueError("至少需要一簇")
        self._slots = slots
        self._clusters = {cluster.rack_id: cluster for cluster in clusters}
        self._maintenance = tuple(maintenance)
        self._curtail = curtailment or GridCurtailment(
            default_import_kw=max(cluster.max_power_kw for cluster in clusters) * len(clusters),
            default_export_kw=max(cluster.max_power_kw for cluster in clusters) * len(clusters),
        )
        self._quota = quota or QuotaBudget(charge_kwh=float("inf"), discharge_kwh=float("inf"))
        self._reserve = reserve or ReservePolicy()
        self._policy = policy

    def plan(
        self,
        bids: list[Bid],
        *,
        plan_date: str,
        version: int,
        generated_at: float,
    ) -> ScheduleResult:
        """执行排程，返回完整结果。"""

        slots = self._slots
        # 簇级、随时间推进的 SOC（充电升、放电降）。
        soc = {rack_id: cluster.soc_pct for rack_id, cluster in self._clusters.items()}
        # 每个时段每个方向已经占用的全站功率。
        used_power: dict[tuple[int, str], float] = {}
        # 每个时段每个簇被哪条申请占用。
        occupied: dict[tuple[int, str], str] = {}
        quota_left = {CHARGE: self._quota.cap_for(CHARGE), DISCHARGE: self._quota.cap_for(DISCHARGE)}

        # 同方向申请单按归属口径全局排优先级（用视野起点折算生效时刻）。
        horizon_start = slots[0].start
        bids_by_direction = {CHARGE: [], DISCHARGE: []}
        for bid in bids:
            bids_by_direction[bid.direction].append(bid)
        priority = {
            direction: order_bids(items, horizon_start, self._policy)
            for direction, items in bids_by_direction.items()
        }

        allocations: list[Allocation] = []
        scheduled_kwh: dict[str, float] = {bid.bid_id: 0.0 for bid in bids}

        for slot in slots:
            direction = CHARGE if slot.kind == "valley" else DISCHARGE if slot.kind == "peak" else None
            if direction is None:
                continue
            cap = self._curtail.cap_kw(direction, slot.index)
            reservable = self._reserve.reservable_kw(direction, cap)
            for bid in priority[direction]:
                want_kwh = bid.energy_kwh - scheduled_kwh[bid.bid_id]
                if want_kwh <= _EPS:
                    continue
                remaining_budget = min(quota_left[direction], want_kwh)
                if remaining_budget <= _EPS:
                    continue
                station_power_used = used_power.get((slot.index, direction), 0.0)
                station_headroom = max(0.0, reservable - station_power_used)
                if station_headroom <= _EPS:
                    continue
                for rack_id in self._pick_clusters(slot, direction, soc):
                    if occupied.get((slot.index, rack_id)):
                        continue
                    cluster = self._clusters[rack_id]
                    if self._under_maintenance(rack_id, slot):
                        continue
                    power = self._allocation_power(cluster, direction, slot, station_headroom)
                    if power <= _EPS:
                        continue
                    energy = min(power * slot.duration_h, remaining_budget)
                    energy = min(energy, self._soc_room(cluster, soc[rack_id], direction))
                    if energy <= _EPS:
                        continue
                    # 受簇功率限制，实际功率可能小于按能量反推的值，重算功率。
                    power = min(power, energy / slot.duration_h)
                    allocations.append(
                        Allocation(
                            slot_index=slot.index,
                            rack_id=rack_id,
                            direction=direction,
                            power_kw=power,
                            energy_kwh=energy,
                            bid_id=bid.bid_id,
                        )
                    )
                    occupied[(slot.index, rack_id)] = bid.bid_id
                    used_power[(slot.index, direction)] = station_power_used + power
                    station_power_used += power
                    station_headroom -= power
                    quota_left[direction] -= energy
                    remaining_budget -= energy
                    scheduled_kwh[bid.bid_id] += energy
                    soc[rack_id] = self._advance_soc(cluster, soc[rack_id], direction, energy)
                    want_kwh -= energy
                    if want_kwh <= _EPS or remaining_budget <= _EPS or station_headroom <= _EPS:
                        break

        rejections = self._build_rejections(bids, scheduled_kwh)
        reserve_report = self._reserve_report(allocations, used_power, occupied, soc)
        return ScheduleResult(
            plan_date=plan_date,
            version=version,
            policy=self._policy,
            horizon_start=slots[0].start,
            horizon_end=slots[-1].end,
            slots=slots,
            clusters=tuple(self._clusters[rack_id] for rack_id in sorted(self._clusters)),
            decisions=tuple(allocations),
            rejections=tuple(rejections),
            reserve=reserve_report,
            generated_at=generated_at,
        )

    # ---- 簇选择与逐簇约束 -------------------------------------------------

    def _under_maintenance(self, rack_id: str, slot: Slot) -> bool:
        return any(
            window.rack_id == rack_id and window.overlaps(slot.start, slot.end)
            for window in self._maintenance
        )

    def _pick_clusters(self, slot: Slot, direction: str, soc: dict[str, float]) -> list[str]:
        """返回该时段可用簇的填充顺序：充电最空先上、放电最满先上。"""

        candidates = []
        for rack_id, cluster in self._clusters.items():
            if not cluster.available or self._under_maintenance(rack_id, slot):
                continue
            if direction == CHARGE and self._soc_room(cluster, soc[rack_id], CHARGE) <= _EPS:
                continue
            if direction == DISCHARGE and self._soc_room(cluster, soc[rack_id], DISCHARGE) <= _EPS:
                continue
            candidates.append(rack_id)
        if direction == CHARGE:
            candidates.sort(key=lambda rid: (soc[rid], rid))
        else:
            candidates.sort(key=lambda rid: (-soc[rid], rid))
        return candidates

    def _allocation_power(
        self, cluster: ClusterContext, direction: str, slot: Slot, station_headroom: float
    ) -> float:
        """单簇单时段功率上限：设备额定与全站剩余可出租功率取小。"""

        device_cap = cluster.power_cap_kw(direction)
        requested = cluster.max_power_kw
        return max(0.0, min(device_cap, requested, station_headroom))

    def _soc_room(self, cluster: ClusterContext, soc_pct: float, direction: str) -> float:
        """按当前（推进后的）SOC 折算该方向还能容纳的电量。"""

        if direction == CHARGE:
            room_pct = max(0.0, cluster.soc_max_pct - soc_pct)
        else:
            room_pct = max(0.0, soc_pct - cluster.soc_min_pct)
        return cluster.rated_energy_kwh * room_pct / 100.0

    def _advance_soc(
        self, cluster: ClusterContext, soc_pct: float, direction: str, energy_kwh: float
    ) -> float:
        delta_pct = energy_kwh / max(cluster.rated_energy_kwh, 1e-9) * 100.0
        if direction == CHARGE:
            return min(cluster.soc_max_pct, soc_pct + delta_pct)
        return max(cluster.soc_min_pct, soc_pct - delta_pct)

    # ---- 拒绝诊断与余量报告 ----------------------------------------------

    def _build_rejections(self, bids: list[Bid], scheduled_kwh: dict[str, float]) -> list[Rejection]:
        """为没排满的申请汇总卡点原因。"""

        rejections: list[Rejection] = []
        for bid in bids:
            got = round(scheduled_kwh[bid.bid_id], 6)
            short = round(bid.energy_kwh - got, 6)
            if short <= _EPS:
                continue
            reasons = self._diagnose(bid, got)
            status = PARTIAL if got > _EPS else REJECTED
            rejections.append(
                Rejection(
                    bid_id=bid.bid_id,
                    status=status,
                    requested_kwh=bid.energy_kwh,
                    scheduled_kwh=got,
                    reasons=tuple(reasons),
                )
            )
        return rejections

    def _diagnose(self, bid: Bid, got_kwh: float) -> list[str]:
        """按"还缺多少"逐约束比较，给出这条申请的卡点，按约束面分类。"""

        direction = bid.direction
        short_kwh = bid.energy_kwh - got_kwh
        wanted_kind = "valley" if direction == CHARGE else "peak"
        matching = [slot for slot in self._slots if slot.kind == wanted_kind]
        reasons: list[str] = []
        if not matching:
            return ["no_matching_window"]

        quota = self._quota.cap_for(direction)
        # 全站限电（扣除调峰余量）在该方向全部匹配时段能提供的总电量。
        station_energy = 0.0
        any_curtail_zero = False
        reserve_held_any = False
        for slot in matching:
            cap = self._curtail.cap_kw(direction, slot.index)
            if cap <= _EPS:
                any_curtail_zero = True
            reservable = self._reserve.reservable_kw(direction, cap)
            if self._reserve.reserve_kw(direction, cap) > _EPS:
                reserve_held_any = True
            station_energy += reservable * slot.duration_h

        # 各簇 SOC 余量之和（用快照初始 SOC；保守上界）。
        soc_room_total = sum(
            cluster.charge_room_kwh() if direction == CHARGE else cluster.discharge_room_kwh()
            for cluster in self._clusters.values()
            if cluster.available
        )
        any_cluster_soc_bound = any(
            (cluster.charge_room_kwh() if direction == CHARGE else cluster.discharge_room_kwh()) <= _EPS
            for cluster in self._clusters.values()
            if cluster.available
        )
        any_maintained = any(cluster.maintained for cluster in self._clusters.values())
        any_fault = any(cluster.fault for cluster in self._clusters.values())

        # 逐约束判断：该约束给出的上限是否不足以覆盖缺口（已排部分视为其他约束放行）。
        if quota <= _EPS or quota < bid.energy_kwh - _EPS:
            reasons.append("quota_exhausted" if quota <= _EPS else "quota_insufficient")
        if station_energy < short_kwh - _EPS:
            if any_curtail_zero:
                reasons.append("grid_curtailment")
            elif reserve_held_any and station_energy + self._reserve_slack_energy(direction, matching) >= short_kwh - _EPS:
                reasons.append("reserve_held")
            else:
                reasons.append("window_capacity_insufficient")
        elif reserve_held_any:
            # 限电本身够，但被调峰余量挤占也单独标注，方便值班员理解余量取舍。
            reasons.append("reserve_held")
        if soc_room_total < short_kwh - _EPS or any_cluster_soc_bound:
            reasons.append("cluster_soc_bound")
        if any_maintained:
            reasons.append("cluster_in_maintenance")
        if any_fault:
            reasons.append("cluster_fault")
        # 簇额定功率（忽略 SOC／限电）的总吞吐能力。
        device_throughput = sum(
            cluster.max_power_kw
            for cluster in self._clusters.values()
            if cluster.available
        ) * matching[0].duration_h * len(matching)
        if device_throughput < short_kwh - _EPS and "window_capacity_insufficient" not in reasons:
            reasons.append("cluster_power_insufficient")
        if not reasons:
            # 各约束单独看都够，但单子仍没排满，只能是被更早的申请占走了容量。
            reasons.append("contended_by_earlier_bids")
        # 去重并保持稳定顺序。
        seen: set[str] = set()
        ordered: list[str] = []
        for reason in reasons:
            if reason not in seen:
                seen.add(reason)
                ordered.append(reason)
        return ordered

    def _reserve_slack_energy(self, direction: str, matching: list) -> float:
        """返回匹配时段里被调峰余量保留的总电量（用于判定是否余量导致）。"""

        total = 0.0
        for slot in matching:
            cap = self._curtail.cap_kw(direction, slot.index)
            total += self._reserve.reserve_kw(direction, cap) * slot.duration_h
        return total

    def _reserve_report(
        self,
        allocations: list[Allocation],
        used_power: dict[tuple[int, str], float],
        occupied: dict[tuple[int, str], str],
        final_soc: dict[str, float],
    ) -> dict:
        """汇总每个时段留给临时调峰的余量与实际占用，证明余量确实被留出来。"""

        per_slot: list[dict] = []
        for slot in self._slots:
            direction = CHARGE if slot.kind == "valley" else DISCHARGE if slot.kind == "peak" else IDLE
            entry: dict = {
                "slot_index": slot.index,
                "kind": slot.kind,
                "scheduled": False,
            }
            if direction in (CHARGE, DISCHARGE):
                cap = self._curtail.cap_kw(direction, slot.index)
                used = round(used_power.get((slot.index, direction), 0.0), 3)
                reservable = self._reserve.reservable_kw(direction, cap)
                entry.update(
                    {
                        "direction": direction,
                        "grid_cap_kw": round(cap, 3),
                        "used_kw": used,
                        "free_for_bids_kw": round(max(0.0, reservable - used), 3),
                        "reserve_kw": self._reserve.reserve_kw(direction, cap),
                        "reserve_within_cap": used <= reservable + 1e-6,
                        "idle_clusters": sorted(
                            rack_id
                            for rack_id in self._clusters
                            if not occupied.get((slot.index, rack_id))
                            and not self._under_maintenance(rack_id, slot)
                            and not self._clusters[rack_id].fault
                        ),
                    }
                )
                entry["scheduled"] = used > _EPS
            per_slot.append(entry)

        idle_energy_kwh = 0.0
        for slot in self._slots:
            direction = CHARGE if slot.kind == "valley" else DISCHARGE if slot.kind == "peak" else None
            if direction is None:
                continue
            for rack_id, cluster in self._clusters.items():
                if occupied.get((slot.index, rack_id)) or self._under_maintenance(rack_id, slot):
                    continue
                room = self._soc_room(cluster, final_soc[rack_id], direction)
                idle_energy_kwh += min(room, cluster.max_power_kw * slot.duration_h)

        return {
            "policy": self._reserve.as_dict(),
            "slots": per_slot,
            "idle_capacity_kwh": round(idle_energy_kwh, 6),
        }
