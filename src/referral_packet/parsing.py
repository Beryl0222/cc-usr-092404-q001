"""记录读取入口：严格区分三个层次。

读取一条转诊资料必须依次通过三层，失败点决定了它的处置方式：

1. **JSON 结构层** (:func:`parse_json_text`)：文本必须是合法 JSON，
   且顶层必须是对象。列表、数字、裸字符串都不是一份资料。
2. **字段合同层** (:func:`parse_payload`)：对象必须逐字段符合当前合同
   ——字段齐全、类型正确、时间是带时区的 ISO 串。旧版本在此分流到
   已登记迁移；未来版本直接交还原文，不解释。
3. **业务语义层** (:func:`validate_record_semantics` 与
   :func:`validate_revision_advances`)：合同成立之后再检查业务规则
   ——修订号为正、修订相对该记录上一版严格递增、领域被本院接收。

任何一层都不通过的记录不能进入资料存储；批量收件时只隔离其自身。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .contracts import (
    ACCEPTED_DOMAINS,
    CURRENT_SCHEMA_VERSION,
    DOMAIN_PATTERN,
    FIELDS,
    RECORD_ID_PATTERN,
    DomainRecord,
)
from .errors import ContractError, FutureVersionError, SemanticError, StructuralError
from .migrations import migrate_to_current


@dataclass(frozen=True)
class ParsedRecord:
    """解析产物：当前结构的记录 + 完整版本溯源。"""

    record: DomainRecord
    observed_version: int
    migration_chain: tuple[dict[str, object], ...]

    @property
    def migrated(self) -> bool:
        return bool(self.migration_chain)


# ---------------------------------------------------------------- 第一层

def parse_json_text(text: str | bytes) -> dict[str, object]:
    """结构层：解析 JSON 并要求顶层为对象。"""

    try:
        payload = json.loads(text)
    except (json.JSONDecodeError, TypeError) as exc:
        raise StructuralError(
            "内容不是合法 JSON",
            details={"reason": "invalid_json"},
        ) from exc

    if not isinstance(payload, dict):
        raise StructuralError(
            "JSON 顶层必须是对象，单条记录不能是数组或标量",
            details={"reason": "top_level_not_object", "json_type": type(payload).__name__},
        )
    return payload


# ---------------------------------------------------------------- 第二层

def _require_int(payload: dict[str, object], field: str) -> int:
    value = payload.get(field)
    # bool 是 int 的子类，合同明确不接受。
    if not isinstance(value, int) or isinstance(value, bool):
        raise ContractError(
            f"字段 {field!r} 必须是整数",
            details={"reason": "type_error", "field": field, "actual": type(value).__name__},
        )
    return value


def _require_str(payload: dict[str, object], field: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str):
        raise ContractError(
            f"字段 {field!r} 必须是字符串",
            details={"reason": "type_error", "field": field, "actual": type(value).__name__},
        )
    return value


def _validate_v1_fields(payload: dict[str, object]) -> dict[str, object]:
    """对当前结构版本执行逐字段合同校验。"""

    unknown = set(payload) - set(FIELDS)
    if unknown:
        # 未知字段必须显式处理（升级合同或迁移），不能静默忽略——
        # 否则发送方多传的关键字段会在无人察觉的情况下丢失。
        raise ContractError(
            f"出现当前合同未定义的字段：{sorted(unknown)}",
            details={"reason": "unknown_fields", "fields": sorted(unknown)},
        )
    missing = [f for f in FIELDS if f not in payload]
    if missing:
        raise ContractError(
            f"缺少合同要求的字段：{missing}",
            details={"reason": "missing_fields", "fields": missing},
        )

    record_id = _require_str(payload, "record_id")
    if not RECORD_ID_PATTERN.fullmatch(record_id):
        raise ContractError(
            "record_id 必须为 1-200 个非空白字符（允许 Unicode 字母数字，"
            "不得含空白或控制字符）",
            details={"reason": "bad_record_id", "value": record_id},
        )

    domain = _require_str(payload, "domain")
    if not DOMAIN_PATTERN.fullmatch(domain):
        raise ContractError(
            "domain 必须是小写下划线标识符",
            details={"reason": "bad_domain_shape", "value": domain},
        )

    occurred_text = _require_str(payload, "occurred_at")
    try:
        occurred_at = datetime.fromisoformat(occurred_text)
    except ValueError as exc:
        raise ContractError(
            "occurred_at 不是合法 ISO 8601 时间",
            details={"reason": "bad_timestamp", "value": occurred_text},
        ) from exc
    if occurred_at.tzinfo is None:
        # 本次事故的直接成因：没有时区就无法判断是否赶上接诊窗口。
        raise ContractError(
            "occurred_at 必须携带时区偏移，无时区时间不得入库",
            details={"reason": "timezone_required", "value": occurred_text},
        )

    source = _require_str(payload, "source")
    if not source.strip():
        raise ContractError(
            "source 不得为空",
            details={"reason": "empty_source"},
        )

    return {
        "schema_version": _require_int(payload, "schema_version"),
        "record_id": record_id,
        "domain": domain,
        "occurred_at": occurred_at,
        "revision": _require_int(payload, "revision"),
        "source": source,
    }


def parse_payload(payload: dict[str, object]) -> ParsedRecord:
    """合同层 + 版本分流，产出当前结构的记录（语义层尚未执行）。"""

    if "schema_version" not in payload:
        raise ContractError(
            "缺少结构版本字段 schema_version",
            details={"reason": "missing_fields", "fields": ["schema_version"]},
        )
    observed_version = _require_int(payload, "schema_version")
    # 保留原始观测版本：即便后续迁移到 v1，溯源仍要能回答
    # “这份资料最初是以哪个版本送达的”。
    original_version = observed_version
    migration_chain: tuple[dict[str, object], ...] = ()

    if observed_version > CURRENT_SCHEMA_VERSION:
        # 未来版本：保留原文、等待升级。这里不读取任何其他字段，
        # 避免对未来语义做出任何猜测。
        raise FutureVersionError(
            f"记录结构版本 {observed_version} 高于当前版本 {CURRENT_SCHEMA_VERSION}，"
            "原文留存等待软件升级",
            details={
                "observed_version": observed_version,
                "current_version": CURRENT_SCHEMA_VERSION,
            },
        )

    if observed_version < CURRENT_SCHEMA_VERSION:
        migrated, chain = migrate_to_current(payload, observed_version)
        payload = migrated
        migration_chain = tuple(chain)

    fields = _validate_v1_fields(payload)
    record = DomainRecord(
        schema_version=fields["schema_version"],
        record_id=fields["record_id"],
        domain=fields["domain"],
        occurred_at=fields["occurred_at"],
        revision=fields["revision"],
        source=fields["source"],
    )
    return ParsedRecord(
        record=record,
        observed_version=original_version,
        migration_chain=migration_chain,
    )


# ---------------------------------------------------------------- 第三层

def validate_record_semantics(record: DomainRecord) -> None:
    """不依赖历史状态的业务语义：修订号为正、领域被接收。"""

    if record.revision <= 0:
        # 负数修订是本次事故的另一个直接成因：它无法表达“覆盖哪一版”。
        raise SemanticError(
            f"revision 必须为正整数，收到 {record.revision}",
            details={"reason": "revision_not_positive", "value": record.revision},
        )
    if record.domain not in ACCEPTED_DOMAINS:
        raise SemanticError(
            f"本院不接收领域 {record.domain!r} 的资料",
            details={"reason": "domain_not_accepted", "value": record.domain},
        )


def validate_revision_advances(record: DomainRecord, previous_revision: int | None) -> None:
    """依赖历史状态的业务语义：修订必须相对上一版严格递增。"""

    if previous_revision is not None and record.revision <= previous_revision:
        raise SemanticError(
            f"revision 必须严格递增：该记录当前为 {previous_revision}，"
            f"收到 {record.revision}",
            details={
                "reason": "revision_not_advanced",
                "previous_revision": previous_revision,
                "received_revision": record.revision,
            },
        )


# ---------------------------------------------------------------- 组合入口

def parse_record_text(text: str | bytes) -> ParsedRecord:
    """结构层 → 合同层（含迁移/未来版本留存）→ 语义层。"""

    payload = parse_json_text(text)
    parsed = parse_payload(payload)
    validate_record_semantics(parsed.record)
    return parsed


def load_record(path: Path) -> DomainRecord:
    """从文件读取一条当前结构的记录。

    保持原有的最小入口形态，但内部走完整三层校验；
    旧版本经登记迁移读入，未来版本与坏记录在此失败而不是被静默接受。
    """

    return parse_record_text(Path(path).read_text(encoding="utf-8")).record
