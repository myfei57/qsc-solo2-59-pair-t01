"""排程版本历史与回放。

每出一版方案都做三件事，且三者口径一致：

1. 把完整结果不可变地存成版本文档（``schedule_<date>_v###.json``，原子写），
   旧版本永不被覆盖，"改完找不到原来是怎么排的"在物理上不可能发生；
2. 往只追加的 ``schedule`` 流水写一条版本头（创建／被取代、所用归属口径、
   总量、申请数、改单原因），时间线与审计据此回放；
3. 原子更新"当前版本"指针文档，值班员默认读到最新一版，但随时能按版本号或
   时间取回任意旧版。

落盘的输入（申请单、检修、限电、余量策略、归属口径）也随版本保存，配合版本
结果里的簇能力快照，可以在事后用当年输入原样重算，校验"存的结果"与"算的
结果"一致。
"""

from __future__ import annotations

import threading
from typing import Any

from ..errors import StorageError
from ..store.jsonfile import JsonStore
from ..store.journal import JsonlJournal

CURRENT_DOC = "schedule_current"
JOURNAL_TOPIC = "schedule"


class ScheduleHistory:
    """排程版本的只追加存储与回放入口。"""

    def __init__(self, store: JsonStore) -> None:
        self._store = store
        self._journal = JsonlJournal(store.root, JOURNAL_TOPIC, segment_limit=512)
        self._lock = threading.RLock()

    @property
    def journal(self) -> JsonlJournal:
        return self._journal

    def _version_doc(self, plan_date: str, version: int) -> str:
        return f"schedule_{plan_date}_v{version:03d}"

    def _inputs_doc(self, plan_date: str, version: int) -> str:
        return f"schedule_{plan_date}_v{version:03d}_inputs"

    def next_version(self, plan_date: str) -> int:
        """返回该运营日下一版号（从 1 起）。"""

        with self._lock:
            pointer = self._read_pointer()
            if pointer and pointer.get("plan_date") == plan_date:
                return int(pointer.get("version", 0)) + 1
            existing = self._version_numbers(plan_date)
            return (max(existing) + 1) if existing else 1

    def save(
        self,
        result: dict[str, Any],
        inputs: dict[str, Any],
        *,
        created_at: float,
        reason: str = "",
    ) -> dict:
        """落盘一版方案（结果 + 输入 + 版本头 + 指针），返回版本头。"""

        plan_date = str(result["plan_date"])
        with self._lock:
            previous = self._read_pointer()
            version = int(result["version"])
            self._store.write(self._version_doc(plan_date, version), result)
            self._store.write(self._inputs_doc(plan_date, version), inputs)
            header = {
                "ts": round(float(created_at), 3),
                "plan_date": plan_date,
                "version": version,
                "policy": result.get("policy"),
                "action": "created",
                "supersedes": previous.get("version") if previous and previous.get("plan_date") == plan_date else None,
                "reason": reason,
                "charge_kwh": result.get("totals", {}).get("charge_kwh", 0.0),
                "discharge_kwh": result.get("totals", {}).get("discharge_kwh", 0.0),
                "bids": len(inputs.get("bids", [])),
                "rejections": len(result.get("rejections", [])),
            }
            self._journal.append(header)
            self._store.write(CURRENT_DOC, {"plan_date": plan_date, "version": version, "ts": header["ts"]})
            return dict(header)

    def latest_pointer(self) -> dict | None:
        """返回当前版本指针。"""

        with self._lock:
            return self._read_pointer()

    def load_version(self, plan_date: str, version: int) -> dict | None:
        """读取某一版完整结果。"""

        with self._lock:
            return self._store.read(self._version_doc(plan_date, version))

    def load_inputs(self, plan_date: str, version: int) -> dict | None:
        """读取某一版的排程输入。"""

        with self._lock:
            return self._store.read(self._inputs_doc(plan_date, version))

    def latest(self) -> dict | None:
        """读取当前版本完整结果。"""

        pointer = self.latest_pointer()
        if not pointer:
            return None
        return self.load_version(str(pointer["plan_date"]), int(pointer["version"]))

    def versions(self, plan_date: str) -> list[dict]:
        """按版本号升序返回某运营日的版本头（时间回放用）。"""

        with self._lock:
            records = [
                item
                for item in self._journal.read_all()
                if item.get("plan_date") == plan_date and item.get("action") == "created"
            ]
        records.sort(key=lambda item: (int(item.get("version", 0)), float(item.get("ts", 0.0))))
        return records

    def replay(self, plan_date: str) -> list[dict]:
        """按时间／版本升序回放某运营日每一版方案（结果随版本头一起返回）。"""

        timeline: list[dict] = []
        for header in self.versions(plan_date):
            version = int(header["version"])
            result = self.load_version(plan_date, version)
            inputs = self.load_inputs(plan_date, version)
            timeline.append({"header": dict(header), "result": result, "inputs": inputs})
        return timeline

    def replay_timeline(self) -> list[dict]:
        """跨运营日按时间升序回放全部版本头。"""

        with self._lock:
            records = [item for item in self._journal.read_all() if item.get("action") == "created"]
        return sorted(records, key=lambda item: (float(item.get("ts", 0.0)), str(item.get("plan_date", ""))))

    def _read_pointer(self) -> dict | None:
        raw = self._store.read(CURRENT_DOC)
        if not isinstance(raw, dict):
            return None
        try:
            return {"plan_date": str(raw["plan_date"]), "version": int(raw["version"]), "ts": raw.get("ts")}
        except (KeyError, TypeError, ValueError) as error:
            raise StorageError("当前排程指针文档损坏", component="schedule") from error

    def _version_numbers(self, plan_date: str) -> list[int]:
        prefix = f"schedule_{plan_date}_v"
        numbers: list[int] = []
        for doc_id in self._store.list_ids():
            if not doc_id.startswith(prefix) or doc_id.endswith("_inputs"):
                continue
            tail = doc_id[len(prefix):]
            if tail.isdigit():
                numbers.append(int(tail))
        return numbers
