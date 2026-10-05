"""计划版本与双时态回放。

每生成或修改一次计划都产出一个不可变的 :class:`PlanVersion`，旧版本只追加、
不覆盖。版本同时携带两条时间轴：

- ``recorded_at``（事务时间／系统时刻）：这份方案是什么时候被算出来并存下的；
- ``effective_start``（生效时间）：这份方案从哪个物理时刻起生效。

"改过一次之后旧方案也能回放"靠的就是把两者分开：按生效时间回放回答"当天那时候
现场执行的是哪一版"，按记录时间回放回答"方案本身经历了哪些修订"。归属归属用的
是生效时间轴，所以跨零点重放结果不会翻案。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field

PLAN_KIND_DAY_AHEAD = "day_ahead"
PLAN_KIND_REVISION = "revision"


@dataclass(frozen=True)
class PlanVersion:
    """一份不可变的排程计划版本。"""

    version: int
    plan_id: str
    kind: str
    parent_version: int
    recorded_at: float
    effective_start: float
    effective_end: float
    policy: str
    horizon: dict
    allocations: tuple = ()
    rejections: tuple = ()
    holds: tuple = ()
    reserve: tuple = ()
    reconciliation: dict = field(default_factory=dict)
    note: str = ""

    def as_dict(self) -> dict:
        return {
            "version": self.version,
            "plan_id": self.plan_id,
            "kind": self.kind,
            "parent_version": self.parent_version,
            "recorded_at": round(self.recorded_at, 3),
            "effective_start": round(self.effective_start, 3),
            "effective_end": round(self.effective_end, 3),
            "policy": self.policy,
            "horizon": dict(self.horizon),
            "allocations": [dict(item) for item in self.allocations],
            "rejections": [dict(item) for item in self.rejections],
            "holds": [dict(item) for item in self.holds],
            "reserve": [dict(item) for item in self.reserve],
            "reconciliation": dict(self.reconciliation),
            "note": self.note,
        }

    @classmethod
    def from_dict(cls, raw: dict) -> "PlanVersion":
        return cls(
            version=int(raw.get("version", 0)),
            plan_id=str(raw.get("plan_id", "")),
            kind=str(raw.get("kind", PLAN_KIND_DAY_AHEAD)),
            parent_version=int(raw.get("parent_version", 0)),
            recorded_at=float(raw.get("recorded_at", 0.0)),
            effective_start=float(raw.get("effective_start", 0.0)),
            effective_end=float(raw.get("effective_end", 0.0)),
            policy=str(raw.get("policy", "")),
            horizon=dict(raw.get("horizon", {})),
            allocations=tuple(dict(item) for item in raw.get("allocations", [])),
            rejections=tuple(dict(item) for item in raw.get("rejections", [])),
            holds=tuple(dict(item) for item in raw.get("holds", [])),
            reserve=tuple(dict(item) for item in raw.get("reserve", [])),
            reconciliation=dict(raw.get("reconciliation", {})),
            note=str(raw.get("note", "")),
        )


class PlanHistory:
    """只追加的计划版本史：内存索引 + 原子快照文件。"""

    def __init__(self, store, doc_id: str = "schedule_versions") -> None:
        self._store = store
        self._doc_id = doc_id
        self._lock = threading.RLock()
        self._versions: list[PlanVersion] = self._load()

    def _load(self) -> list[PlanVersion]:
        raw = self._store.read(self._doc_id)
        if isinstance(raw, dict) and isinstance(raw.get("versions"), list):
            return [PlanVersion.from_dict(item) for item in raw["versions"]]
        return []

    def _persist(self) -> None:
        payload = {"versions": [version.as_dict() for version in self._versions]}
        self._store.write(self._doc_id, payload)

    def latest_version_number(self) -> int:
        with self._lock:
            return self._versions[-1].version if self._versions else 0

    def append(self, candidate: PlanVersion) -> PlanVersion:
        """落定一个版本：编号在历史内单调分配，防止外部传重号。"""

        with self._lock:
            number = self.latest_version_number() + 1
            stored = PlanVersion(
                version=number,
                plan_id=candidate.plan_id,
                kind=candidate.kind,
                parent_version=candidate.parent_version or self.latest_version_number(),
                recorded_at=candidate.recorded_at,
                effective_start=candidate.effective_start,
                effective_end=candidate.effective_end,
                policy=candidate.policy,
                horizon=candidate.horizon,
                allocations=candidate.allocations,
                rejections=candidate.rejections,
                holds=candidate.holds,
                reserve=candidate.reserve,
                reconciliation=candidate.reconciliation,
                note=candidate.note,
            )
            self._versions.append(stored)
            self._persist()
            return stored

    def versions(self) -> list[PlanVersion]:
        with self._lock:
            return list(self._versions)

    def get(self, version: int) -> PlanVersion | None:
        with self._lock:
            for item in self._versions:
                if item.version == version:
                    return item
        return None

    def latest(self) -> PlanVersion | None:
        with self._lock:
            return self._versions[-1] if self._versions else None

    def as_of_effective(self, ts: float) -> PlanVersion | None:
        """生效时间回放：``ts`` 时刻现场正在执行的那一版。

        必须同时满足两个条件：该版本已经被算出来（``recorded_at <= ts``）且
        此刻落在它的生效窗内。否则会出现"凌晨执行的是当天早班才修订出的方案"
        这种穿越。
        """

        with self._lock:
            eligible = [
                v
                for v in self._versions
                if v.recorded_at <= ts and v.effective_start <= ts < v.effective_end
            ]
        return eligible[-1] if eligible else None

    def recorded_before(self, ts: float) -> list[PlanVersion]:
        """事务时间回放：截至某系统时刻已经算出来的全部版本（按记录先后）。"""

        with self._lock:
            return [v for v in self._versions if v.recorded_at <= ts]
