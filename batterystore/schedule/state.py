"""排程内部的逐簇能量与占用状态。

引擎在离散槽位上推进每簇的可用电量：充进去要留库仑效率损失（多用电），放出来
也要留余量（少上网）。占用表记录每个槽位这一簇已经被哪条计划用掉，同簇同槽位
互斥就查它。临时调峰保留线在这里是硬地板，任何放电都不许把簇压到保留 SOC
对应的电量以下。
"""

from __future__ import annotations

from .roster import ClusterCapability


class ClusterState:
    """一簇在排程推进过程中的电量与占用状态。"""

    def __init__(self, capability: ClusterCapability) -> None:
        self.cap = capability
        self.energy_kwh = self._initial_energy()
        self.occupant: dict[int, str] = {}
        self.actions: dict[int, str] = {}

    def _initial_energy(self) -> float:
        """把当前 SOC 线性折算成电量：SOC 0% 对应电量下限，100% 对应上限。"""

        span = self.cap.energy_max_kwh - self.cap.energy_min_kwh
        fraction = max(0.0, min(1.0, self.cap.soc_pct / 100.0))
        return self.cap.energy_min_kwh + span * fraction

    def reserve_energy_kwh(self) -> float:
        """保留 SOC 对应的电量地板（kWh）。"""

        if self.cap.energy_max_kwh <= 0:
            return 0.0
        return self.cap.energy_max_kwh * self.cap.reserve_soc_pct / 100.0

    def discharge_headroom_kwh(self) -> float:
        """还能向电网放出多少电量而不跌破保留地板（电池侧可用能量）。"""

        return max(0.0, self.energy_kwh - self.reserve_energy_kwh())

    def charge_headroom_kwh(self) -> float:
        """还能充进多少电量（电池侧）而不超过上限。"""

        return max(0.0, self.cap.energy_max_kwh - self.energy_kwh)

    def busy(self, slot_index: int) -> str | None:
        return self.occupant.get(slot_index)

    def place(self, slot_index: int, request_id: str, action: str) -> None:
        self.occupant[slot_index] = request_id
        self.actions[slot_index] = action

    def apply_charge(self, battery_kwh: float) -> None:
        """登记电池侧充入电量，夹在上限内。"""

        self.energy_kwh = min(self.cap.energy_max_kwh, self.energy_kwh + max(0.0, battery_kwh))

    def apply_discharge(self, battery_kwh: float) -> None:
        """登记电池侧放出电量，夹在保留地板以上。"""

        self.energy_kwh = max(self.reserve_energy_kwh(), self.energy_kwh - max(0.0, battery_kwh))


def build_states(roster) -> dict[str, ClusterState]:
    """为名册里每簇建一个状态。"""

    return {cluster.rack_id: ClusterState(cluster) for cluster in roster.all()}
