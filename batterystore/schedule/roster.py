"""簇名册：排程看到的簇能力视图。

排程不直接翻 BMS/PCS 内部状态，它只需要每簇的四件事：可用电量区间、当前
SOC、充放电功率上下限、临时调峰保留线。名册把这些收成一份只读视图，既可以
由实时内核的真实簇造（上线排程走这条），也可以按编号批量合成（验证两百多簇
规模时不用真的建两百簇模型）。
"""

from __future__ import annotations

from dataclasses import dataclass

DEFAULT_RESERVE_SOC_PCT = 20.0


@dataclass(frozen=True)
class ClusterCapability:
    """单簇排程能力。"""

    rack_id: str
    zone: str
    energy_min_kwh: float
    energy_max_kwh: float
    soc_pct: float
    max_charge_kw: float
    max_discharge_kw: float
    min_power_kw: float
    efficiency: float
    reserve_soc_pct: float

    def as_dict(self) -> dict:
        return {
            "rack_id": self.rack_id,
            "zone": self.zone,
            "energy_min_kwh": round(self.energy_min_kwh, 3),
            "energy_max_kwh": round(self.energy_max_kwh, 3),
            "soc_pct": round(self.soc_pct, 3),
            "max_charge_kw": round(self.max_charge_kw, 3),
            "max_discharge_kw": round(self.max_discharge_kw, 3),
            "min_power_kw": round(self.min_power_kw, 3),
            "efficiency": round(self.efficiency, 4),
            "reserve_soc_pct": round(self.reserve_soc_pct, 3),
        }


class Roster:
    """按编号索引的簇能力集合。"""

    def __init__(self, clusters: list[ClusterCapability]) -> None:
        if not clusters:
            raise ValueError("名册至少需要一簇")
        seen: set[str] = set()
        for cluster in clusters:
            if cluster.rack_id in seen:
                raise ValueError(f"簇编号重复: {cluster.rack_id}")
            seen.add(cluster.rack_id)
        self._clusters = {cluster.rack_id: cluster for cluster in clusters}

    def count(self) -> int:
        return len(self._clusters)

    def ids(self) -> list[str]:
        return sorted(self._clusters)

    def get(self, rack_id: str) -> ClusterCapability:
        return self._clusters[rack_id]

    def has(self, rack_id: str) -> bool:
        return rack_id in self._clusters

    def all(self) -> list[ClusterCapability]:
        return [self._clusters[key] for key in sorted(self._clusters)]

    def as_dict(self) -> dict:
        return {"count": self.count(), "clusters": [cluster.as_dict() for cluster in self.all()]}


def _rack_usable_kwh(config, rack) -> tuple[float, float, float]:
    """从真实簇折算（下限电量，上限电量，当前电量），单位 kWh。"""

    current = rack.energy_kwh(config.soc.capacity_ah)
    usable = rack.usable_energy_kwh(config.rack, config.soc.capacity_ah)
    rated = current / max(rack.soc_pct(), 1e-9) * 100.0 if rack.soc_pct() > 0 else usable
    span = (config.rack.soc_max_pct - config.rack.soc_min_pct) / 100.0
    energy_min = rated * config.rack.soc_min_pct / 100.0
    energy_max = energy_min + rated * span
    return energy_min, energy_max, current


def roster_from_platform(platform, reserve_soc_pct: float = DEFAULT_RESERVE_SOC_PCT) -> Roster:
    """从实时内核的真实簇造名册。"""

    clusters: list[ClusterCapability] = []
    for rack_id in sorted(platform.racks):
        rack = platform.racks[rack_id]
        energy_min, energy_max, _current = _rack_usable_kwh(platform.config, rack)
        clusters.append(
            ClusterCapability(
                rack_id=rack_id,
                zone=rack.zone,
                energy_min_kwh=energy_min,
                energy_max_kwh=energy_max,
                soc_pct=rack.soc_pct(),
                max_charge_kw=platform.config.pcs.max_power_kw,
                max_discharge_kw=platform.config.grid.export_limit_kw,
                min_power_kw=platform.config.pcs.min_power_kw,
                efficiency=platform.config.pcs.efficiency,
                reserve_soc_pct=float(reserve_soc_pct),
            )
        )
    return Roster(clusters)


def synthetic_roster(
    count: int,
    *,
    rated_kwh: float = 500.0,
    soc_min_pct: float = 10.0,
    soc_max_pct: float = 95.0,
    initial_soc_pct: float = 50.0,
    max_charge_kw: float = 500.0,
    max_discharge_kw: float = 400.0,
    min_power_kw: float = 5.0,
    efficiency: float = 0.97,
    reserve_soc_pct: float = DEFAULT_RESERVE_SOC_PCT,
    zone_count: int = 4,
) -> Roster:
    """批量合成簇名册，用于验证两百多簇规模。"""

    if count <= 0:
        raise ValueError("簇数必须为正")
    clusters: list[ClusterCapability] = []
    for index in range(count):
        rack_id = f"rk-{index + 1:03d}"
        energy_min = rated_kwh * soc_min_pct / 100.0
        energy_max = rated_kwh * soc_max_pct / 100.0
        clusters.append(
            ClusterCapability(
                rack_id=rack_id,
                zone=f"Z{(index % zone_count) + 1}",
                energy_min_kwh=energy_min,
                energy_max_kwh=energy_max,
                soc_pct=float(initial_soc_pct),
                max_charge_kw=max_charge_kw,
                max_discharge_kw=max_discharge_kw,
                min_power_kw=min_power_kw,
                efficiency=efficiency,
                reserve_soc_pct=float(reserve_soc_pct),
            )
        )
    return Roster(clusters)
