"""转诊资料收件领域。

公共入口：

* :func:`load_record` / :mod:`referral_packet.parsing` —— 分层读取与校验；
* :class:`IntakeService` (:mod:`referral_packet.intake`) —— 批量收件、
  幂等、复核、签章保护与崩溃恢复；
* :class:`~referral_packet.storage.Journal` (:mod:`referral_packet.storage`)
  —— 追加事件日志，收件状态的唯一事实源。
"""

from .contracts import CURRENT_SCHEMA_VERSION, DomainRecord
from .errors import (
    ContractError,
    FutureVersionError,
    RecordError,
    SemanticError,
    StructuralError,
)
from .intake import (
    ACCEPTED,
    HELD_FUTURE,
    QUARANTINED,
    REVIEW,
    REPLAYED,
    BatchResult,
    IntakeService,
    SlotOutcome,
)
from .parsing import ParsedRecord, load_record, parse_json_text, parse_payload, parse_record_text

__all__ = [
    "CURRENT_SCHEMA_VERSION",
    "DomainRecord",
    "ParsedRecord",
    "RecordError",
    "StructuralError",
    "ContractError",
    "SemanticError",
    "FutureVersionError",
    "load_record",
    "parse_json_text",
    "parse_payload",
    "parse_record_text",
    "IntakeService",
    "BatchResult",
    "SlotOutcome",
    "ACCEPTED",
    "REPLAYED",
    "REVIEW",
    "QUARANTINED",
    "HELD_FUTURE",
]
