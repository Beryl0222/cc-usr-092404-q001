"""收件流程：批量接收、幂等、复核、签章保护与崩溃恢复。

流程对每条输入严格按序执行（合法记录的处理顺序等于输入顺序）：

1. 登记批次，**原文先归档**并记录 ``raw_archived``（含位置与原文哈希）；
2. 走三层读取（结构 / 合同 / 语义）与版本分流；
3. 按业务结论落入唯一终态：

   * ``ACCEPTED`` 受理并通知；
   * ``REPLAYED`` 内容完全重传，返回既有结论，不重复落库、不重复通知；
   * ``REVIEW``   同标识内容变化，进人工复核，不自动覆盖；
   * ``QUARANTINED`` 坏记录或撞上已签章修订，只隔离自身；
   * ``HELD_FUTURE`` 未来版本，原文留存等待升级。

任何一步之后停机都安全：事件日志是事实源，:meth:`IntakeService.recover`
只补做未完成的动作（未决结论的位置继续判定、已受理未通知的补通知），
绝不重复执行已完成动作。
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Protocol

from .errors import (
    FutureVersionError,
    RecordError,
    SemanticError,
    StructuralError,
    attach_location,
)
from .parsing import (
    ParsedRecord,
    parse_json_text,
    parse_payload,
    validate_record_semantics,
    validate_revision_advances,
)
from .storage import (
    Journal,
    RawArchive,
    canonical_hash,
    replay,
    sha256_text,
)

ACCEPTED = "ACCEPTED"
REPLAYED = "REPLAYED"
REVIEW = "REVIEW"
QUARANTINED = "QUARANTINED"
HELD_FUTURE = "HELD_FUTURE"


class Notifier(Protocol):
    """到件通知出口。返回通知回执编号。"""

    def deliver(self, accepted_event: dict[str, object]) -> str: ...


class LoggingNotifier:
    """默认通知出口：把通知写入独立的通知日志。"""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._path.parent.mkdir(parents=True, exist_ok=True)

    def deliver(self, accepted_event: dict[str, object]) -> str:
        receipt = f"ntf-{uuid.uuid4().hex[:12]}"
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    {
                        "receipt": receipt,
                        "accepted_event_id": accepted_event["event_id"],
                        "record_id": accepted_event["record_id"],
                        "revision": accepted_event["revision"],
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
                + "\n"
            )
        return receipt


@dataclass(frozen=True)
class SlotOutcome:
    """一个输入位置的处理结论。"""

    slot: int
    outcome: str
    location: str
    raw_path: str
    raw_sha256: str
    record_id: str | None = None
    revision: int | None = None
    observed_version: int | None = None
    reason: str | None = None
    detail: dict[str, object] = field(default_factory=dict)
    reference_event_id: str | None = None
    review_id: str | None = None

    @property
    def blocked(self) -> bool:
        """该位置是否是一个需要人工关注的阻塞点。"""

        return self.outcome in (QUARANTINED, REVIEW, HELD_FUTURE)


@dataclass(frozen=True)
class BatchResult:
    batch_id: str
    outcomes: tuple[SlotOutcome, ...]

    @property
    def accepted(self) -> tuple[SlotOutcome, ...]:
        return tuple(o for o in self.outcomes if o.outcome == ACCEPTED)

    @property
    def blocked(self) -> tuple[SlotOutcome, ...]:
        return tuple(o for o in self.outcomes if o.blocked)


class IntakeService:
    """事件日志支撑的收件服务；同一根目录下的实例共享同一事实源。"""

    def __init__(
        self,
        root: str | Path,
        *,
        notifier: Notifier | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._root = Path(root)
        self._root.mkdir(parents=True, exist_ok=True)
        self._journal = Journal(self._root / "journal.jsonl")
        self._archive = RawArchive(self._root / "raw")
        self._notifier = notifier or LoggingNotifier(self._root / "notifications.jsonl")
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    # ------------------------------------------------ 基础事件写入

    def close(self) -> None:
        self._journal.close()

    def __enter__(self) -> "IntakeService":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _state(self):
        return replay(self._journal.read_all())

    def _now(self) -> str:
        return self._clock().isoformat()

    def _emit(self, etype: str, **fields: object) -> dict[str, object]:
        state = self._state()
        event: dict[str, object] = {
            "seq": state.next_seq,
            "event_id": f"evt-{uuid.uuid4().hex[:16]}",
            "ts": self._now(),
            "type": etype,
        }
        event.update(fields)
        return self._journal.append(event)

    # ------------------------------------------------ 批量收件

    def ingest_batch(
        self,
        items: list[str | bytes],
        *,
        batch_id: str | None = None,
    ) -> BatchResult:
        """按输入顺序处理一批资料；坏记录只隔离自身。"""

        batch_id = batch_id or f"batch-{uuid.uuid4().hex[:12]}"
        state = self._state()
        if batch_id in state.batches:
            # 批次号也幂等：同一批次重投直接重建既有结论。
            return self._replay_batch_result(batch_id)

        self._emit("batch_registered", batch_id=batch_id, item_count=len(items))

        outcomes: list[SlotOutcome] = []
        for slot, raw_item in enumerate(items, start=1):
            outcomes.append(self._ingest_slot(batch_id, slot, raw_item))

        return BatchResult(batch_id=batch_id, outcomes=tuple(outcomes))

    def _ingest_slot(self, batch_id: str, slot: int, raw_item: str | bytes) -> SlotOutcome:
        """新收件位置：归档原文后再处理。"""

        if isinstance(raw_item, bytes):
            raw_text, decode_error = self._decode(raw_item)
        else:
            raw_text, decode_error = raw_item, None

        location = f"{batch_id} / #{slot}"
        raw_sha = sha256_text(raw_text)
        raw_path = self._archive.store(batch_id, slot, raw_text)
        self._emit(
            "raw_archived",
            batch_id=batch_id,
            slot=slot,
            location=location,
            raw_path=str(raw_path),
            raw_sha256=raw_sha,
        )

        if decode_error is not None:
            return self._quarantine(
                batch_id, slot, location, raw_path, raw_sha,
                StructuralError(
                    "原始字节不是合法 UTF-8 文本，无法读取",
                    details={"reason": "invalid_encoding"},
                ),
            )

        return self._process_archived(batch_id, slot, location, raw_path, raw_sha, raw_text)

    def _process_archived(
        self,
        batch_id: str,
        slot: int,
        location: str,
        raw_path: Path,
        raw_sha: str,
        raw_text: str,
    ) -> SlotOutcome:
        """处理一份已经归档的原文。崩溃恢复时直接调用本方法，
        因此恢复不会重复归档、也不会重复写 ``raw_archived`` 事件。"""

        parsed: ParsedRecord | None = None
        claimed: dict[str, object] = {}
        try:
            # 结构层单独执行：成功后才能看到申报字段。
            payload = parse_json_text(raw_text)
            # 申报身份未经合同确认，只用于隔离时帮助护士定位，
            # 绝不作为可信标识使用。
            claimed = {
                "claimed_record_id": payload.get("record_id"),
                "claimed_revision": payload.get("revision"),
                "claimed_schema_version": payload.get("schema_version"),
            }
            # 合同层（含迁移 / 未来版本分流）。
            parsed = parse_payload(payload)
            # 语义层单独执行：拒绝时记录身份已可留痕，
            # 护士能直接看到“是哪份清单的哪一版被拦”。
            validate_record_semantics(parsed.record)
        except FutureVersionError as exc:
            return self._hold_future(batch_id, slot, location, raw_path, raw_sha, exc)
        except RecordError as exc:
            attach_location(exc, batch_id, slot)
            return self._quarantine(
                batch_id, slot, location, raw_path, raw_sha, exc,
                parsed=parsed, claimed=claimed,
            )

        return self._decide_for_parsed(batch_id, slot, location, raw_path, raw_sha, parsed)

    @staticmethod
    def _decode(raw: bytes) -> tuple[str, Exception | None]:
        try:
            return raw.decode("utf-8"), None
        except UnicodeDecodeError as exc:
            return "", exc

    # ------------------------------------------------ 终态：隔离 / 留存

    def _quarantine(
        self,
        batch_id: str,
        slot: int,
        location: str,
        raw_path: Path,
        raw_sha: str,
        exc: RecordError,
        *,
        parsed: ParsedRecord | None = None,
        claimed: dict[str, object] | None = None,
    ) -> SlotOutcome:
        identity: dict[str, object] = {}
        if parsed is not None:
            # 语义层失败时合同字段已确认，记录身份可信留痕。
            identity = {
                "record_id": parsed.record.record_id,
                "revision": parsed.record.revision,
                "observed_version": parsed.observed_version,
                "migration_chain": list(parsed.migration_chain),
            }
        elif claimed:
            # 合同层失败：身份未经确认，仅以“申报值”留痕帮助定位。
            identity = {
                "claimed_record_id": claimed.get("claimed_record_id"),
                "claimed_revision": claimed.get("claimed_revision"),
                "claimed_schema_version": claimed.get("claimed_schema_version"),
            }
        event = self._emit(
            "record_quarantined",
            batch_id=batch_id,
            slot=slot,
            location=location,
            raw_path=str(raw_path),
            raw_sha256=raw_sha,
            error_layer=exc.layer,
            reason=exc.details.get("reason", "rejected"),
            detail=dict(exc.details),
            message=str(exc.message),
            **identity,
        )
        return SlotOutcome(
            slot=slot,
            outcome=QUARANTINED,
            location=location,
            raw_path=str(raw_path),
            raw_sha256=raw_sha,
            record_id=event.get("record_id", event.get("claimed_record_id")),
            revision=event.get("revision", event.get("claimed_revision")),
            observed_version=event.get(
                "observed_version", event.get("claimed_schema_version")
            ),
            reason=event["reason"],
            detail={"error_layer": exc.layer, **dict(exc.details)},
            reference_event_id=event["event_id"],
        )

    def _hold_future(
        self,
        batch_id: str,
        slot: int,
        location: str,
        raw_path: Path,
        raw_sha: str,
        exc: FutureVersionError,
    ) -> SlotOutcome:
        event = self._emit(
            "future_version_held",
            batch_id=batch_id,
            slot=slot,
            location=location,
            raw_path=str(raw_path),
            raw_sha256=raw_sha,
            observed_version=exc.details.get("observed_version"),
            current_version=exc.details.get("current_version"),
            reason="future_version",
            detail=dict(exc.details),
        )
        return SlotOutcome(
            slot=slot,
            outcome=HELD_FUTURE,
            location=location,
            raw_path=str(raw_path),
            raw_sha256=raw_sha,
            observed_version=exc.details.get("observed_version"),
            reason="future_version",
            detail=dict(exc.details),
            reference_event_id=event["event_id"],
        )

    # ------------------------------------------------ 终态：业务判定

    def _decide_for_parsed(
        self,
        batch_id: str,
        slot: int,
        location: str,
        raw_path: Path,
        raw_sha: str,
        parsed: ParsedRecord,
    ) -> SlotOutcome:
        record = parsed.record
        content_sha = canonical_hash(record.to_contract_payload())
        state = self._state()

        accepted_versions = state.accepted_for(record.record_id)

        # 1) 幂等优先：规范化内容与任一已受理版本完全相同即“完全重传”，
        #    返回既有结果——即便该修订此后已签章，重传也不是覆盖。
        same_accepted = next(
            (e for e in accepted_versions if e["content_sha256"] == content_sha),
            None,
        )
        if same_accepted is not None:
            return self._replayed(batch_id, slot, location, raw_path, raw_sha, same_accepted)

        # 与尚待复核的版本内容相同：返回“等待复核”这一既有结论。
        pending_review = self._find_pending_review(state, record.record_id)
        if pending_review is not None and pending_review["content_sha256"] == content_sha:
            return self._replayed(batch_id, slot, location, raw_path, raw_sha, pending_review)

        # 与已被人工驳回（含因签章被拒）的复核内容相同：
        # 重传返回既有驳回结论，不能绕过复核变成首次受理。
        rejected_review = self._find_rejected_review(state, record.record_id, content_sha)
        if rejected_review is not None:
            return self._replayed(batch_id, slot, location, raw_path, raw_sha, rejected_review)

        # 2) 内容不同却撞上已签章修订：隔离，绝不覆盖签章材料。
        if state.is_sealed(record.record_id, record.revision):
            return self._quarantine(
                batch_id, slot, location, raw_path, raw_sha,
                SemanticError(
                    f"修订 {record.record_id}#rev{record.revision} 已签章，"
                    "内容不同的报文不得覆盖签章材料",
                    details={"reason": "sealed_revision_protected",
                             "record_id": record.record_id,
                             "revision": record.revision},
                ),
                parsed=parsed,
            )

        # 3) 同标识内容变化：一律进人工复核，系统绝不自动覆盖旧版。
        #    “修订必须递增”在此经显式校验器判定，结论作为复核证据留痕；
        #    未递增的变化同样进复核（而不是被自动接受或自动改写修订号），
        #    由人工结合证据驳回。
        prior = accepted_versions[-1] if accepted_versions else pending_review
        if prior is not None:
            return self._open_review(
                batch_id, slot, location, raw_path, raw_sha,
                parsed, content_sha, prior,
            )

        # 4) 该记录首次送达（修订号为正已在语义层校验）。
        event = self._accept(
            batch_id, slot, location, raw_path, raw_sha, parsed, content_sha
        )
        return self._accepted_outcome(
            slot, location, raw_path, raw_sha, event, parsed
        )

    def _trace_fields(self, parsed: ParsedRecord) -> dict[str, object]:
        return {
            "record_id": parsed.record.record_id,
            "domain": parsed.record.domain,
            "revision": parsed.record.revision,
            "occurred_at": parsed.record.occurred_at_iso,
            "observed_version": parsed.observed_version,
            "schema_version": parsed.record.schema_version,
            "migration_chain": list(parsed.migration_chain),
        }

    def _accept(
        self,
        batch_id: str,
        slot: int,
        location: str,
        raw_path: Path,
        raw_sha: str,
        parsed: ParsedRecord,
        content_sha: str,
    ) -> dict[str, object]:
        return self._emit(
            "record_accepted",
            batch_id=batch_id,
            slot=slot,
            location=location,
            raw_path=str(raw_path),
            raw_sha256=raw_sha,
            content_sha256=content_sha,
            **self._trace_fields(parsed),
        )

    def _accepted_outcome(
        self, slot: int, location: str, raw_path: Path, raw_sha: str,
        event: dict[str, object], parsed: ParsedRecord,
    ) -> SlotOutcome:
        # 受理事件落盘后再通知：这正是“落库与通知之间可能停机”的边界。
        notification_pending = False
        try:
            self._deliver_for(event)
        except Exception:
            # 通知出口故障不回滚已落库资料；恢复时只补这条通知。
            notification_pending = True
        return SlotOutcome(
            slot=slot,
            outcome=ACCEPTED,
            location=location,
            raw_path=str(raw_path),
            raw_sha256=raw_sha,
            record_id=parsed.record.record_id,
            revision=parsed.record.revision,
            observed_version=parsed.observed_version,
            reference_event_id=event["event_id"],
            detail={"notification_pending": notification_pending},
        )

    def _replayed(
        self,
        batch_id: str,
        slot: int,
        location: str,
        raw_path: Path,
        raw_sha: str,
        prior: dict[str, object],
    ) -> SlotOutcome:
        state = self._state()
        if prior["type"] == "review_opened":
            review_id = prior.get("review_id", "")
            resolution = (state.reviews.get(review_id, prior).get("resolution") or {})
            # 只有复核批准（追加受理）才可能产生过通知；
            # 待复核与已驳回都没有送达过到件通知。
            notified = any(
                e["type"] == "review_admitted"
                and e.get("review_id") == review_id
                and e["event_id"] in state.delivered
                for e in state.events
            ) and resolution.get("decision") == "ADMIT"
        else:
            notified = prior["event_id"] in state.delivered
        event = self._emit(
            "record_replayed",
            batch_id=batch_id,
            slot=slot,
            location=location,
            raw_path=str(raw_path),
            raw_sha256=raw_sha,
            record_id=prior["record_id"],
            revision=prior["revision"],
            content_sha256=prior["content_sha256"],
            prior_event_id=prior["event_id"],
            prior_location=prior["location"],
            prior_type=prior["type"],
            prior_notification_delivered=notified,
        )
        return SlotOutcome(
            slot=slot,
            outcome=REPLAYED,
            location=location,
            raw_path=str(raw_path),
            raw_sha256=raw_sha,
            record_id=prior["record_id"],
            revision=prior["revision"],
            observed_version=prior.get("observed_version"),
            reference_event_id=event["event_id"],
            detail={
                "prior_event_id": prior["event_id"],
                "prior_location": prior["location"],
                "prior_outcome": prior["type"],
                "notification_delivered": notified,
            },
        )

    def _open_review(
        self,
        batch_id: str,
        slot: int,
        location: str,
        raw_path: Path,
        raw_sha: str,
        parsed: ParsedRecord,
        content_sha: str,
        prior: dict[str, object],
    ) -> SlotOutcome:
        review_id = f"review-{uuid.uuid4().hex[:12]}"
        previous_revision = int(prior["revision"])
        revision_check: dict[str, object]
        try:
            validate_revision_advances(parsed.record, previous_revision)
            revision_check = {
                "passed": True,
                "previous_revision": previous_revision,
                "received_revision": parsed.record.revision,
            }
        except SemanticError as exc:
            revision_check = {
                "passed": False,
                "previous_revision": previous_revision,
                "received_revision": parsed.record.revision,
                "reason": exc.details.get("reason", "revision_not_advanced"),
            }
        event = self._emit(
            "review_opened",
            reason="content_changed",
            review_id=review_id,
            batch_id=batch_id,
            slot=slot,
            location=location,
            raw_path=str(raw_path),
            raw_sha256=raw_sha,
            content_sha256=content_sha,
            prior_event_id=prior["event_id"],
            prior_location=prior["location"],
            prior_content_sha256=prior["content_sha256"],
            revision_check=revision_check,
            **self._trace_fields(parsed),
        )
        return SlotOutcome(
            slot=slot,
            outcome=REVIEW,
            location=location,
            raw_path=str(raw_path),
            raw_sha256=raw_sha,
            record_id=parsed.record.record_id,
            revision=parsed.record.revision,
            observed_version=parsed.observed_version,
            reason="content_changed",
            review_id=review_id,
            reference_event_id=event["event_id"],
            detail={
                "prior_event_id": prior["event_id"],
                "prior_location": prior["location"],
                "prior_revision": previous_revision,
                "revision_check": revision_check,
            },
        )

    # ------------------------------------------------ 复核处理

    def resolve_review(
        self, review_id: str, *, decision: str, reason: str = ""
    ) -> dict[str, object]:
        """人工复核结论。

        ``ADMIT`` 只以**追加**方式受理：旧版受理事件与签章材料原样保留；
        若该修订在复核期间被签章，ADMIT 被拒绝（绝不覆盖签章）。
        ``REJECT`` 则只记录驳回结论，资料不进入受理集合。
        """

        if decision not in ("ADMIT", "REJECT"):
            raise ValueError("decision 必须是 ADMIT 或 REJECT")
        state = self._state()
        review = state.reviews.get(review_id)
        if review is None:
            raise KeyError(f"复核单 {review_id} 不存在")
        if review_id not in state.open_reviews:
            raise ValueError(f"复核单 {review_id} 已处理，结论不可更改")

        if decision == "ADMIT" and state.is_sealed(review["record_id"], review["revision"]):
            # 即便人工批准，也不能覆盖复核期间产生的签章；必须先走新修订。
            self._emit(
                "review_resolved",
                review_id=review_id,
                decision="REJECTED_SEALED",
                reason=reason or "目标修订在复核期间已签章",
            )
            raise ValueError("目标修订已签章，复核批准不得覆盖签章材料")

        self._emit(
            "review_resolved",
            review_id=review_id,
            decision=decision,
            reason=reason,
            record_id=review["record_id"],
            revision=review["revision"],
        )

        if decision == "ADMIT":
            accepted = self._emit(
                "review_admitted",
                review_id=review_id,
                batch_id=review["batch_id"],
                slot=review["slot"],
                location=review["location"],
                raw_path=review["raw_path"],
                raw_sha256=review["raw_sha256"],
                content_sha256=review["content_sha256"],
                record_id=review["record_id"],
                domain=review["domain"],
                revision=review["revision"],
                occurred_at=review["occurred_at"],
                observed_version=review.get("observed_version"),
                schema_version=review.get("schema_version"),
                migration_chain=list(review.get("migration_chain", [])),
            )
            self._deliver_for(accepted)
            return accepted
        return self._state().reviews[review_id]["resolution"]

    # ------------------------------------------------ 签章

    def seal_accepted(self, record_id: str, revision: int) -> dict[str, object]:
        """对已受理的某一修订签章；签章后该修订内容永久不可覆盖。"""

        state = self._state()
        if not any(
            e["record_id"] == record_id and e["revision"] == revision
            for e in state.accepted_for(record_id)
        ):
            raise ValueError("只能对已受理的修订签章")
        if state.is_sealed(record_id, revision):
            raise ValueError("该修订已签章，不可重复签章")
        return self._emit("record_sealed", record_id=record_id, revision=revision)

    # ------------------------------------------------ 通知

    def _deliver_for(self, accepted_event: dict[str, object]) -> dict[str, object]:
        receipt = self._notifier.deliver(accepted_event)
        return self._emit(
            "notification_delivered",
            accepted_event_id=accepted_event["event_id"],
            record_id=accepted_event["record_id"],
            revision=accepted_event["revision"],
            receipt=receipt,
        )

    def flush_notifications(self) -> list[dict[str, object]]:
        """补送所有“已受理但未通知”的事件；每条只补一次。"""

        delivered: list[dict[str, object]] = []
        for accepted in self._state().pending_notifications():
            try:
                delivered.append(self._deliver_for(accepted))
            except Exception:
                # 出口仍不可用：保持未完成状态，等待下一次恢复。
                break
        return delivered

    # ------------------------------------------------ 崩溃恢复

    def recover(self) -> dict[str, object]:
        """停机后恢复：只补做未完成动作。

        * 已归档原文但没有终态的位置：从归档原文重新判定（幂等）；
        * 已受理但未送达的通知：逐条补送。
        已完成的动作（已有终态、已有通知事件）绝不重复执行。
        """

        resumed_slots: list[dict[str, object]] = []
        for pending in self._state().pending_ingest():
            raw_path = Path(pending["raw_path"])
            raw_text = raw_path.read_text(encoding="utf-8")
            # 只补判定动作：原文已归档、raw_archived 已存在，不重复写。
            outcome = self._process_archived(
                pending["batch_id"],
                pending["slot"],
                pending["location"],
                raw_path,
                pending["raw_sha256"],
                raw_text,
            )
            resumed_slots.append(
                {"location": pending["location"], "outcome": outcome.outcome}
            )

        # 通知对账与上面的判定相互独立：只补“已受理但无通知事件”的，
        # 已送达的绝不重复通知。
        notifications = self.flush_notifications()
        return {
            "resumed_slots": resumed_slots,
            "delivered_notifications": [
                {"record_id": e["record_id"], "revision": e["revision"], "receipt": e["receipt"]}
                for e in notifications
            ],
        }

    # ------------------------------------------------ 溯源

    def trace(self, *, location: str | None = None, review_id: str | None = None) -> dict[str, object]:
        """从任一阻塞点（隔离 / 留存 / 复核位置）追溯完整处理链。

        返回：原始批次位置、原文路径与哈希、观测版本与迁移链、
        每一步处理结论、复核结论与签章状态。
        """

        state = self._state()
        events = state.events

        def chain_for(predicate: Callable[[dict[str, object]], bool]) -> list[dict[str, object]]:
            chain: list[dict[str, object]] = []
            for e in events:
                if not predicate(e):
                    continue
                entry = {
                    "seq": e["seq"],
                    "ts": e["ts"],
                    "type": e["type"],
                    "location": e.get("location"),
                    "reason": e.get("reason"),
                    "detail": e.get("detail"),
                }
                if "revision_check" in e:
                    entry["revision_check"] = e["revision_check"]
                if e.get("type") == "record_replayed":
                    entry["prior_event_id"] = e.get("prior_event_id")
                    entry["prior_location"] = e.get("prior_location")
                chain.append(entry)
            return chain

        target_event: dict[str, object] | None = None
        if review_id is not None:
            target_event = state.reviews.get(review_id)
            if target_event is None:
                raise KeyError(f"复核单 {review_id} 不存在")
            loc = target_event["location"]
        else:
            loc = location

        if loc is None:
            raise ValueError("必须提供 location 或 review_id")

        raw_chain = [e for e in events if e.get("location") == loc]
        terminal_types = {
            "record_accepted", "record_quarantined", "future_version_held",
            "review_opened", "record_replayed",
        }
        terminal = next(
            (e for e in reversed(raw_chain) if e["type"] in terminal_types),
            None,
        )
        if terminal is None:
            raise KeyError(f"位置 {loc} 没有任何处理记录")
        chain = chain_for(lambda e: e.get("location") == loc)

        record_id = terminal.get("record_id")
        report: dict[str, object] = {
            "location": loc,
            "batch_id": terminal.get("batch_id"),
            "slot": terminal.get("slot"),
            "raw_path": terminal.get("raw_path"),
            "raw_sha256": terminal.get("raw_sha256"),
            "observed_version": terminal.get("observed_version"),
            "migration_chain": terminal.get("migration_chain", []),
            "conclusion": terminal["type"],
            "reason": terminal.get("reason"),
            "event_chain": chain,
        }
        if terminal["type"] == "record_quarantined":
            # 合同层失败时没有可信身份，只有申报值，单独呈现以免误用。
            report["claimed_record_id"] = terminal.get("claimed_record_id")
            report["claimed_revision"] = terminal.get("claimed_revision")
        if record_id is not None:
            report["record_id"] = record_id
            report["revision"] = terminal.get("revision")
            versions = [
                {
                    "revision": e["revision"],
                    "event_id": e["event_id"],
                    "location": e["location"],
                    "content_sha256": e["content_sha256"],
                    "sealed": state.is_sealed(record_id, e["revision"]),
                    "observed_version": e.get("observed_version"),
                }
                for e in state.accepted_for(record_id)
            ]
            report["accepted_versions"] = versions
            report["sealed_revisions"] = sorted(
                rev for rid, rev in state.sealed if rid == record_id
            )
        if terminal["type"] == "review_opened":
            review = next(
                (r for r in state.reviews.values() if r["location"] == loc), None
            )
            if review is not None:
                report["review_id"] = review["review_id"]
                report["review_open"] = review["review_id"] in state.open_reviews
                report["review_resolution"] = review.get("resolution")
        if terminal["type"] == "record_replayed":
            # 完全重传：从本阻塞点一路追到既有结论所在的原始位置。
            prior_id = terminal.get("prior_event_id")
            prior_event = next((e for e in events if e["event_id"] == prior_id), None)
            if prior_event is not None:
                prior: dict[str, object] = {
                    "prior_type": prior_event["type"],
                    "prior_location": prior_event.get("location"),
                    "prior_event_id": prior_id,
                }
                if prior_event["type"] == "review_opened":
                    review = state.reviews.get(prior_event.get("review_id", ""))
                    if review is not None:
                        prior["review_resolution"] = review.get("resolution")
                report["replayed_prior"] = prior
        return report

    def blocked_points(self) -> list[dict[str, object]]:
        """列出全部阻塞点（隔离 / 未来版本留存 / 待复核），供护士站巡查。"""

        state = self._state()
        points: list[dict[str, object]] = []
        for e in state.quarantine:
            points.append({"location": e["location"], "kind": QUARANTINED,
                           "reason": e["reason"], "raw_path": e["raw_path"]})
        for e in state.held:
            points.append({"location": e["location"], "kind": HELD_FUTURE,
                           "reason": "future_version", "raw_path": e["raw_path"],
                           "observed_version": e.get("observed_version")})
        for e in state.open_reviews.values():
            points.append({"location": e["location"], "kind": REVIEW,
                           "reason": "content_changed", "raw_path": e["raw_path"],
                           "review_id": e["review_id"],
                           "record_id": e["record_id"], "revision": e["revision"]})
        return points

    # ------------------------------------------------ 查询辅助

    def _find_pending_review(self, state, record_id: str) -> dict[str, object] | None:
        for review in state.open_reviews.values():
            if review["record_id"] == record_id:
                return review
        return None

    def _find_rejected_review(
        self, state, record_id: str, content_sha: str
    ) -> dict[str, object] | None:
        for review in state.reviews.values():
            if review["record_id"] != record_id:
                continue
            if review["review_id"] in state.open_reviews:
                continue
            if review["content_sha256"] != content_sha:
                continue
            decision = (review.get("resolution") or {}).get("decision")
            if decision in ("REJECT", "REJECTED_SEALED"):
                return review
        return None

    def _replay_batch_result(self, batch_id: str) -> BatchResult:
        state = self._state()
        outcomes: list[SlotOutcome] = []
        by_location: dict[str, SlotOutcome] = {}
        # 重放时按位置取该位置的终态事件重建结论。
        for e in state.events:
            if e.get("batch_id") != batch_id:
                continue
            etype = e["type"]
            mapping = {
                "record_accepted": ACCEPTED,
                "record_quarantined": QUARANTINED,
                "future_version_held": HELD_FUTURE,
                "review_opened": REVIEW,
                "record_replayed": REPLAYED,
            }
            if etype in mapping:
                by_location[e["location"]] = SlotOutcome(
                    slot=e["slot"],
                    outcome=mapping[etype],
                    location=e["location"],
                    raw_path=e.get("raw_path", ""),
                    raw_sha256=e.get("raw_sha256", ""),
                    record_id=e.get("record_id"),
                    revision=e.get("revision"),
                    observed_version=e.get("observed_version"),
                    reason=e.get("reason"),
                    review_id=e.get("review_id"),
                    reference_event_id=e.get("event_id"),
                )
        outcomes = [by_location[k] for k in sorted(by_location, key=lambda x: by_location[x].slot)]
        return BatchResult(batch_id=batch_id, outcomes=tuple(outcomes))
