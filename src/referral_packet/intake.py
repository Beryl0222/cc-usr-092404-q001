"""转诊资料包收件流程：隔离坏记录、幂等重传、复核与签章保护、停机恢复。

设计要点
--------

- **坏记录只隔离自身**：单条报文在 JSON 结构 / 字段合同 / 业务语义任一层
  失败，只进入隔离区，不影响同批后续记录；合法记录严格保持输入顺序处理。
- **未来版本保留原文**：``FutureVersionError`` 的报文进入待升级区，
  等待结构升级后重放，绝不猜测解释。
- **完全重传幂等**：同一 ``record_id`` 且报文内容逐字一致时，直接返回
  既有处理结论，不重复落库、不重复通知。
- **内容变化进复核**：同一 ``record_id`` 报文内容变化时进入复核区，
  既有材料（无论是否签章）一律不覆盖；已签章材料额外标记
  ``sealed_protected``，任何路径都不得改写。
- **停机恢复只补未完成动作**：每条合法记录依次经过
  ``persisted（落库）→ notified（到件通知）`` 两个阶段，阶段状态写入
  登记表。恢复时只对停在 ``persisted`` 的记录补做通知，已通知的不重放，
  已落库的不重落。
- **每个阻塞点可追溯**：回执（``Receipt``）与隔离/待升级/复核区记录
  （``BlockedRecord``）都带有原始位置、报文自声明版本、迁移轨迹
  （v0→v1 的步骤与显式假设）和处理结论。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Callable, Optional, Protocol, Sequence, Union

from .contracts import (
    ContractError,
    DomainRecord,
    FutureVersionError,
    FieldContractError,
    JsonStructureError,
    MigrationTrace,
    ParsedPacket,
    SemanticContractError,
    parse_json_text,
)

# ---- 处理结论 ----------------------------------------------------------------

ACCEPTED = "accepted"
DUPLICATE = "duplicate"
QUARANTINED = "quarantined"
HELD_FUTURE_VERSION = "held_future_version"
REVIEW = "review"

# ---- 处理阶段 ----------------------------------------------------------------

STAGE_PERSISTED = "persisted"
STAGE_NOTIFIED = "notified"


class OutageError(RuntimeError):
    """模拟/实际停机：资料已落库、到件通知尚未发出时中断。

    携带被中断记录的信息，接收方凭此在恢复前就能定位阻塞点。
    """

    def __init__(
        self,
        message: str,
        *,
        record: DomainRecord,
        detected_version: int,
        migration: Optional[MigrationTrace],
    ) -> None:
        super().__init__(message)
        self.record = record
        self.detected_version = detected_version
        self.migration = migration


@dataclass(frozen=True)
class IncomingItem:
    """一条入件：原始位置（批次内序号或上游文件标识）与 JSON 原文。"""

    position: Union[int, str]
    raw: str


@dataclass(frozen=True)
class Receipt:
    """单条报文的处理回执，接收方凭此追溯全部阻塞点。"""

    position: Union[int, str]
    status: str
    record_id: Optional[str] = None
    revision: Optional[int] = None
    detected_version: Optional[int] = None
    migration: Optional[MigrationTrace] = None
    #: accepted 记录到达的阶段（persisted/notified）；其余结论为 None。
    stage: Optional[str] = None
    reason: Optional[str] = None
    #: duplicate 时指向首次接收该记录的原始位置。
    duplicate_of: Optional[Union[int, str]] = None
    #: 内容变化命中已签章材料时为 True，材料未被覆盖。
    sealed_protected: bool = False
    #: 恢复补做时为 True，表示这是停机后补完的动作。
    recovered: bool = False


@dataclass(frozen=True)
class BlockedRecord:
    """隔离区 / 待升级区 / 复核区中的一条阻塞记录，原文逐字保留。"""

    kind: str  # quarantine / held_future_version / review
    position: Union[int, str]
    raw: str
    reason: str
    detected_version: Optional[int]
    migration: Optional[MigrationTrace]
    layer: Optional[str] = None  # json / field / semantic
    record_id: Optional[str] = None
    existing_revision: Optional[int] = None
    sealed_protected: bool = False


@dataclass(frozen=True)
class BatchResult:
    """一批收件的结果：回执严格按输入顺序排列。"""

    receipts: tuple[Receipt, ...]
    outage: Optional[OutageError] = None
    #: 停机时未处理到的下一个原始位置（无停机为 None）。
    resume_position: Optional[Union[int, str]] = None

    @property
    def accepted(self) -> tuple[Receipt, ...]:
        return tuple(r for r in self.receipts if r.status == ACCEPTED)

    @property
    def blocked(self) -> tuple[Receipt, ...]:
        return tuple(r for r in self.receipts if r.status in (QUARANTINED, HELD_FUTURE_VERSION, REVIEW))


@dataclass
class StoredEntry:
    """登记表中一条已落库记录（跨停机存活，是恢复的事实来源）。"""

    record: DomainRecord
    content_key: str
    first_position: Union[int, str]
    detected_version: int
    migration: Optional[MigrationTrace]
    sealed: bool = False
    stage: str = STAGE_PERSISTED


class Notifier(Protocol):
    """到件通知出口。"""

    def notify(self, record: DomainRecord) -> None: ...


class InMemoryNotifier:
    """默认通知出口：按发出顺序记录，自带幂等保护。"""

    def __init__(self) -> None:
        self.sent: list[tuple[str, int]] = []
        self._sent_keys: set[tuple[str, int]] = set()

    def notify(self, record: DomainRecord) -> None:
        key = (record.record_id, record.revision)
        if key in self._sent_keys:  # 双保险：恢复不得造成重复通知
            return
        self._sent_keys.add(key)
        self.sent.append(key)


class Registry:
    """资料登记表 + 各阻塞区，停机后连同其中状态一起保留。"""

    def __init__(self) -> None:
        self.entries: dict[str, StoredEntry] = {}
        self.blocked: dict[Union[int, str], BlockedRecord] = {}
        self.traces: dict[Union[int, str], Receipt] = {}

    def find(self, record_id: str) -> Optional[StoredEntry]:
        return self.entries.get(record_id)

    def put(self, entry: StoredEntry) -> None:
        self.entries[entry.record.record_id] = entry

    def seal(self, record_id: str) -> None:
        """标记材料已签章；签章后任何内容变化都不得覆盖。"""

        entry = self.entries[record_id]
        object.__setattr__(entry, "sealed", True)

    def block(self, item: BlockedRecord) -> None:
        self.blocked[item.position] = item


def _content_key(raw: str) -> str:
    """报文内容指纹：键序归一后序列化，字段排列差异不影响同一性判定。"""

    payload = json.loads(raw)
    return json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


@dataclass
class Inbox:
    """收件箱：批量收件、复核隔离、签章保护与停机恢复的入口。"""

    registry: Registry = field(default_factory=Registry)
    notifier: Notifier = field(default_factory=InMemoryNotifier)
    #: 落库完成后、通知发出前触发（测试停机注入用）。
    outage: Optional[Callable[[DomainRecord], None]] = None

    def receive_batch(
        self, items: Sequence[Union[IncomingItem, str]]
    ) -> BatchResult:
        receipts: list[Receipt] = []
        outage: Optional[OutageError] = None
        resume_position: Optional[Union[int, str]] = None

        for index, item in enumerate(items, start=1):
            incoming = item if isinstance(item, IncomingItem) else IncomingItem(index, item)
            try:
                receipt = self._process_one(incoming)
            except OutageError as exc:
                # 停机发生在落库之后、通知之前：中断记录已持久化，
                # 阶段停在 persisted；批次到此中止，等待 recover()。
                outage = exc
                resume_position = self._next_position(items, index)
                receipt = Receipt(
                    position=incoming.position,
                    status=ACCEPTED,
                    record_id=exc.record.record_id,
                    revision=exc.record.revision,
                    detected_version=exc.detected_version,
                    migration=exc.migration,
                    stage=STAGE_PERSISTED,
                    reason="已落库，到件通知因停机未发出，等待恢复补做",
                )
                receipts.append(receipt)
                self.registry.traces[incoming.position] = receipt
                break
            receipts.append(receipt)
            self.registry.traces[incoming.position] = receipt

        return BatchResult(
            receipts=tuple(receipts), outage=outage, resume_position=resume_position
        )

    @staticmethod
    def _next_position(
        items: Sequence[Union[IncomingItem, str]], index: int
    ) -> Optional[Union[int, str]]:
        """停机后未处理到的下一个原始位置（index 为 1 基的已处理数）。"""

        if index >= len(items):
            return None
        nxt = items[index]
        return nxt.position if isinstance(nxt, IncomingItem) else index + 1

    def _process_one(self, item: IncomingItem) -> Receipt:
        parsed, blocked_receipt = self._parse_or_block(item)
        if parsed is None:
            return blocked_receipt  # type: ignore[return-value]

        record_id = parsed.record.record_id
        existing = self.registry.find(record_id)
        if existing is not None:
            return self._handle_existing(item, parsed, existing)

        return self._accept_new(item, parsed)

    def _parse_or_block(
        self, item: IncomingItem
    ) -> tuple[Optional[ParsedPacket], Optional[Receipt]]:
        try:
            parsed = parse_json_text(item.raw)
        except FutureVersionError as exc:
            self.registry.block(
                BlockedRecord(
                    kind=HELD_FUTURE_VERSION,
                    position=item.position,
                    raw=item.raw,
                    reason=str(exc),
                    detected_version=_version_or_none(exc.schema_version),
                    migration=None,
                )
            )
            return None, Receipt(
                position=item.position,
                status=HELD_FUTURE_VERSION,
                detected_version=_version_or_none(exc.schema_version),
                reason=str(exc),
            )
        except JsonStructureError as exc:
            return None, self._quarantine(item, "json", str(exc))
        except FieldContractError as exc:
            return None, self._quarantine(item, "field", str(exc))
        except SemanticContractError as exc:
            return None, self._quarantine(item, "semantic", str(exc))
        except ContractError as exc:  # 兜底：未知合同失败也只隔离自身
            return None, self._quarantine(item, "contract", str(exc))
        return parsed, None

    def _quarantine(self, item: IncomingItem, layer: str, reason: str) -> Receipt:
        detected_version = _peek_version(item.raw)
        self.registry.block(
            BlockedRecord(
                kind=QUARANTINED,
                position=item.position,
                raw=item.raw,
                reason=f"[{layer}] {reason}",
                layer=layer,
                detected_version=detected_version,
                migration=None,
            )
        )
        record_id = _peek_record_id(item.raw)
        return Receipt(
            position=item.position,
            status=QUARANTINED,
            record_id=record_id,
            detected_version=detected_version,
            reason=f"[{layer}] {reason}",
        )

    def _handle_existing(
        self, item: IncomingItem, parsed: ParsedPacket, existing: StoredEntry
    ) -> Receipt:
        if _content_key(item.raw) == existing.content_key:
            # 完全重传：返回既有结论与既有阶段，不重复落库、不重复通知。
            return Receipt(
                position=item.position,
                status=DUPLICATE,
                record_id=existing.record.record_id,
                revision=existing.record.revision,
                detected_version=existing.detected_version,
                migration=existing.migration,
                stage=existing.stage,
                duplicate_of=existing.first_position,
                reason=f"与位置 {existing.first_position} 的已收件逐字一致，返回既有处理结论",
            )

        # 内容变化：进入复核，绝不覆盖既有材料；已签章材料施加签章保护。
        reason = (
            f"记录内容相对位置 {existing.first_position} 的修订 "
            f"rev{existing.record.revision} 发生变化，转入复核"
        )
        self.registry.block(
            BlockedRecord(
                kind=REVIEW,
                position=item.position,
                raw=item.raw,
                reason=reason,
                detected_version=parsed.detected_version,
                migration=parsed.migration,
                record_id=existing.record.record_id,
                existing_revision=existing.record.revision,
                sealed_protected=existing.sealed,
            )
        )
        return Receipt(
            position=item.position,
            status=REVIEW,
            record_id=existing.record.record_id,
            revision=parsed.record.revision,
            detected_version=parsed.detected_version,
            migration=parsed.migration,
            reason=reason,
            sealed_protected=existing.sealed,
        )

    def _accept_new(self, item: IncomingItem, parsed: ParsedPacket) -> Receipt:
        record = parsed.record
        entry = StoredEntry(
            record=record,
            content_key=_content_key(item.raw),
            first_position=item.position,
            detected_version=parsed.detected_version,
            migration=parsed.migration,
            stage=STAGE_PERSISTED,
        )
        self.registry.put(entry)  # 阶段 1：落库

        if self.outage is not None:
            # 停机发生在落库之后、通知之前；登记表停在 persisted。
            self.outage(record)
            raise OutageError(
                f"位置 {item.position} 的记录 {record.record_id} "
                "已落库，到件通知因停机未发出",
                record=record,
                detected_version=parsed.detected_version,
                migration=parsed.migration,
            )

        self.notifier.notify(record)  # 阶段 2：到件通知
        entry.stage = STAGE_NOTIFIED
        return Receipt(
            position=item.position,
            status=ACCEPTED,
            record_id=record.record_id,
            revision=record.revision,
            detected_version=parsed.detected_version,
            migration=parsed.migration,
            stage=STAGE_NOTIFIED,
        )

    def recover(self) -> tuple[Receipt, ...]:
        """停机恢复：只对停在 persisted 的记录补做到件通知。

        - 已 notified 的记录：不动，不重复通知；
        - 已 persisted 未 notified 的记录：只补通知并推进阶段；
        - 不重新解析、不重新落库任何记录。
        """

        recoveries: list[Receipt] = []
        for entry in self.registry.entries.values():
            if entry.stage != STAGE_PERSISTED:
                continue
            self.notifier.notify(entry.record)
            entry.stage = STAGE_NOTIFIED
            receipt = Receipt(
                position=entry.first_position,
                status=ACCEPTED,
                record_id=entry.record.record_id,
                revision=entry.record.revision,
                detected_version=entry.detected_version,
                migration=entry.migration,
                stage=STAGE_NOTIFIED,
                recovered=True,
                reason="恢复时补做到件通知（落库已在停机前完成，未重做）",
            )
            recoveries.append(receipt)
            self.registry.traces[entry.first_position] = receipt
        return tuple(recoveries)

    def trace(self, position: Union[int, str]) -> Receipt:
        """按原始位置取处理回执；阻塞区原文用 registry.blocked[position] 追取。"""

        return self.registry.traces[position]


def _version_or_none(value: object) -> Optional[int]:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _peek_version(raw: str) -> Optional[int]:
    """合同校验失败时尽力读取自声明版本，仅供隔离追溯，不参与任何解释。"""

    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None
    return _version_or_none(payload.get("schema_version"))


def _peek_record_id(raw: str) -> Optional[str]:
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None
    value = payload.get("record_id")
    return value if isinstance(value, str) and value.strip() else None
