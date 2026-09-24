"""资料落库：追加事件日志 + 原文归档。

设计原则（对应事故复盘要求）：

* **事件日志是唯一事实源**。``journal.jsonl`` 每行一个不可变事件，
  资料状态、签章状态、复核结论、通知是否送达，全部由重放事件得到；
  进程在任何两步之间停机，重启后重放即可还原。
* **原始报文先归档再解析**。每条输入原文（包括坏 JSON、未来版本）
  都原样写入 ``raw/<批次>/<序号>.json`` 并追加 ``raw_archived`` 事件，
  使之后每一个结论都能追回到原始位置与原始字节。
* **物化结果可重建**。内存里的当前状态没有任何不可重建的成分，
  删除后重放日志即可恢复。

一条记录在日志中可能经历的事件：

``batch_registered`` → ``raw_archived`` → 终态
（``record_accepted`` / ``record_quarantined`` /
``future_version_held`` / ``review_opened``）
，受理后再有 ``notification_delivered``，
复核人工处理后有 ``review_resolved``，
签章有 ``record_sealed``。
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

TERMINAL_EVENTS = (
    "record_accepted",
    "record_quarantined",
    "future_version_held",
    "review_opened",
    "record_replayed",
)


def sha256_text(data: str | bytes) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def canonical_hash(payload: dict[str, object]) -> str:
    """当前结构载荷的规范化哈希，用于识别“内容是否变化”。"""

    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class Journal:
    """只追加的 JSONL 事件日志。"""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = self._path.open("a", encoding="utf-8")

    @property
    def path(self) -> Path:
        return self._path

    def append(self, event: dict[str, object]) -> dict[str, object]:
        line = json.dumps(event, ensure_ascii=False, sort_keys=True)
        self._handle.write(line + "\n")
        # flush + fsync：事件落盘是“资料落库”的边界，
        # 停机不得丢失已确认受理的记录。
        self._handle.flush()
        os.fsync(self._handle.fileno())
        return event

    def read_all(self) -> list[dict[str, object]]:
        if not self._path.exists():
            return []
        with self._path.open("r", encoding="utf-8") as handle:
            return [json.loads(line) for line in handle if line.strip()]

    def close(self) -> None:
        self._handle.close()


class RawArchive:
    """原始报文归档：按 批次/序号 原样保存，不做任何改写。"""

    def __init__(self, root: Path) -> None:
        self._root = root
        self._root.mkdir(parents=True, exist_ok=True)

    def path_for(self, batch_id: str, slot: int) -> Path:
        return self._root / batch_id / f"{slot:04d}.json"

    def store(self, batch_id: str, slot: int, raw: str) -> Path:
        path = self.path_for(batch_id, slot)
        path.parent.mkdir(parents=True, exist_ok=True)
        # 同位置重传必然同内容（raw 哈希相同才走重传），覆盖等价；
        # tmp + rename 保证不会留下写了一半的“原文”。
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(raw, encoding="utf-8")
        os.replace(tmp, path)
        return path


@dataclass
class ReplayedState:
    """从重放日志得到的全部可查询状态。"""

    events: list[dict[str, object]] = field(default_factory=list)
    by_raw_hash: dict[str, dict[str, object]] = field(default_factory=dict)
    terminal_by_raw_hash: dict[str, dict[str, object]] = field(default_factory=dict)
    accepted: dict[str, list[dict[str, object]]] = field(default_factory=dict)
    sealed: set[tuple[str, int]] = field(default_factory=set)
    quarantine: list[dict[str, object]] = field(default_factory=list)
    held: list[dict[str, object]] = field(default_factory=list)
    reviews: dict[str, dict[str, object]] = field(default_factory=dict)
    open_reviews: dict[str, dict[str, object]] = field(default_factory=dict)
    batches: dict[str, dict[str, object]] = field(default_factory=dict)
    delivered: set[str] = field(default_factory=set)
    next_seq: int = 1

    def accepted_for(self, record_id: str) -> list[dict[str, object]]:
        return self.accepted.get(record_id, [])

    def is_sealed(self, record_id: str, revision: int) -> bool:
        return (record_id, revision) in self.sealed

    def pending_ingest(self) -> list[dict[str, object]]:
        """已归档原文但没有终态事件的位置（停机残留的半成品）。"""
        pending: list[dict[str, object]] = []
        for event in self.events:
            if event["type"] == "raw_archived":
                key = (event["batch_id"], event["slot"])
                if not any(
                    e["type"] in TERMINAL_EVENTS
                    and (e["batch_id"], e["slot"]) == key
                    for e in self.events
                ):
                    pending.append(event)
        return pending

    def pending_notifications(self) -> list[dict[str, object]]:
        """已受理（含复核批准追加受理）但通知未送达的事件。"""
        return [
            event
            for event in self.events
            if event["type"] in ("record_accepted", "review_admitted")
            and event["event_id"] not in self.delivered
        ]


def replay(events: Iterable[dict[str, object]]) -> ReplayedState:
    """从事件流重建状态。纯函数式重放，不做任何 I/O 副作用。"""

    state = ReplayedState()
    for event in events:
        state.events.append(event)
        seq = event.get("seq")
        if isinstance(seq, int):
            state.next_seq = max(state.next_seq, seq + 1)
        etype = event["type"]

        if etype == "batch_registered":
            state.batches[event["batch_id"]] = event
        elif etype == "raw_archived":
            state.by_raw_hash.setdefault(event["raw_sha256"], event)
        elif etype == "record_accepted":
            state.accepted.setdefault(event["record_id"], []).append(event)
            state.terminal_by_raw_hash[event["raw_sha256"]] = event
        elif etype == "record_quarantined":
            state.quarantine.append(event)
            state.terminal_by_raw_hash[event["raw_sha256"]] = event
        elif etype == "future_version_held":
            state.held.append(event)
            state.terminal_by_raw_hash[event["raw_sha256"]] = event
        elif etype == "review_opened":
            state.reviews[event["review_id"]] = event
            state.open_reviews[event["review_id"]] = event
            state.terminal_by_raw_hash[event["raw_sha256"]] = event
        elif etype == "review_resolved":
            review = state.reviews.get(event["review_id"])
            if review is not None:
                review["resolution"] = event
            state.open_reviews.pop(event["review_id"], None)
        elif etype == "review_admitted":
            # 复核批准后追加受理：历史受理事件仍保留，从不覆盖。
            state.accepted.setdefault(event["record_id"], []).append(event)
        elif etype == "record_sealed":
            state.sealed.add((event["record_id"], event["revision"]))
        elif etype == "notification_delivered":
            state.delivered.add(event["accepted_event_id"])
    return state
