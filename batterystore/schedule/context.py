"""排程上下文装配。

把平台运行期组件（配置、簇、BMS 闩锁、并网点限额、配额台账）翻译成排程器
需要的只读快照与约束。翻译在排程当口一次性完成，排程器此后不再触碰设备，
因此排程结果对应的是"出方案那一刻"的现场，重放历史时用的是落盘快照，两者
口径一致。
"""

from __future__ import annotations

from ..config import StationConfig
from ..grid.grid import GridTie
from ..quota.quota import QuotaLedger
from ..rack.rack import Rack
from .constraints import GridCurtailment, QuotaBudget
from .models import ClusterContext


def build_cluster_context(
    config: StationConfig, rack: Rack, *, fault: bool, maintained: bool
) -> ClusterContext:
    """把一簇与它的配置、故障／检修状态翻译成排程能力快照。"""

    cells = rack.cells()
    pack_voltage = sum(cell.rating_v for cell in cells)
    rated_energy_kwh = pack_voltage * config.soc.capacity_ah / 1000.0
    return ClusterContext(
        rack_id=rack.rack_id,
        soc_pct=rack.soc_pct(),
        soc_min_pct=config.rack.soc_min_pct,
        soc_max_pct=config.rack.soc_max_pct,
        rated_energy_kwh=rated_energy_kwh,
        max_power_kw=config.pcs.max_power_kw,
        fault=fault,
        maintained=maintained,
    )


def build_cluster_contexts(
    config: StationConfig,
    racks: dict[str, Rack],
    *,
    fault_rack_ids: set[str],
    maintained_rack_ids: set[str],
) -> tuple[ClusterContext, ...]:
    """批量翻译全部簇。"""

    return tuple(
        build_cluster_context(
            config,
            racks[rack_id],
            fault=rack_id in fault_rack_ids,
            maintained=rack_id in maintained_rack_ids,
        )
        for rack_id in sorted(racks)
    )


def build_curtailment(grid: GridTie) -> GridCurtailment:
    """从并网点限额构造默认限电（无逐时段临时限电时的基线）。"""

    limits = grid.limits
    return GridCurtailment(
        default_import_kw=limits.import_limit_kw,
        default_export_kw=limits.export_limit_kw,
    )


def build_quota_budget(quota: QuotaLedger, now: float) -> QuotaBudget:
    """把配额台账本周期剩余额度翻译成排程额度预算。

    排程只允许新排台账剩余额度以内的电量，出方案后由执行层在真正充放电时再次
    经台账记账——计划与执行共用同一处额度，避免"计划排满、执行时才发现超额"。
    """

    remaining = quota.remaining()
    return QuotaBudget(
        charge_kwh=remaining["charge_kwh"],
        discharge_kwh=remaining["discharge_kwh"],
    )
