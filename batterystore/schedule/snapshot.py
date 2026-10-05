"""排程结果快照。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ScheduleSnapshot:
    """一次排程对外暴露的只读结果。"""

    plan_id: str
    version: int
    kind: str
    policy: dict
    horizon: dict
    counts: dict
    allocations: list
    rejections: list
    reserve: list
    reconciliation: dict

    def as_dict(self) -> dict:
        return {
            "plan_id": self.plan_id,
            "version": self.version,
            "kind": self.kind,
            "policy": dict(self.policy),
            "horizon": dict(self.horizon),
            "counts": dict(self.counts),
            "allocations": list(self.allocations),
            "rejections": list(self.rejections),
            "reserve": list(self.reserve),
            "reconciliation": dict(self.reconciliation),
        }
