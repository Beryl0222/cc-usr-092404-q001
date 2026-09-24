"""转诊资料包的数据合同：读取时分层校验，版本化迁移，不做任何猜测解释。

校验分三层，各层错误类型不同，便于收件方定位问题性质：

1. JSON 结构层   —— 报文必须是合法的 JSON 对象（``JsonStructureError``）。
2. 字段合同层   —— 必填字段齐全、类型正确、无未知字段（``FieldContractError``）。
3. 业务语义层   —— 标识非空、领域在允许清单内、时间必须带时区、
   修订号为正整数（``SemanticContractError``）。

版本路由：

- ``schema_version == 1``：当前结构，直接校验。
- ``schema_version == 0``：兼容期旧版，经 ``migrate_v0_to_v1`` 可追溯迁移
  进入当前结构；迁移假设（本地时区、修订号从 0 起）显式记录
  在 ``MigrationTrace.assumptions`` 中，原始报文逐字保留。
- ``schema_version > 1``：未来版本，抛出 ``FutureVersionError``，
  调用方必须保留原文等待升级，禁止猜测解释。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Optional

#: 当前结构版本。
CURRENT_SCHEMA_VERSION = 1

#: 兼容期旧版版本号。v0 报文的时间允许不带时区（按本地交接时区解释）、
#: 修订号允许从 0 起；迁移时按 ``MigrationTrace.assumptions`` 中的显式假设修正。
LEGACY_SCHEMA_VERSION = 0

#: 迁移旧版无时区时间时使用的本地交接时区（明示假设，非隐式猜测）。
LEGACY_LOCAL_TIMEZONE = "+08:00"

#: 允许进入收件流程的业务领域清单。
ALLOWED_DOMAINS = frozenset({"referral_packet"})

#: 当前结构（v1）的字段全集，未知字段一律拒绝，避免静默吞掉合同漂移。
V1_FIELDS = frozenset(
    {"schema_version", "record_id", "domain", "occurred_at", "revision", "source"}
)


class ContractError(Exception):
    """合同校验错误的基类。"""


class JsonStructureError(ContractError):
    """第 1 层：报文不是合法 JSON，或顶层不是对象。"""


class FieldContractError(ContractError):
    """第 2 层：字段缺失、类型不符或出现未知字段。"""


class SemanticContractError(ContractError):
    """第 3 层：字段值违反业务语义（空标识、未知领域、无时区时间、非正修订号）。"""


class FutureVersionError(ContractError):
    """报文版本高于当前结构：必须保留原文等待升级，不得猜测解释。"""

    def __init__(self, schema_version: Any, raw: Any) -> None:
        super().__init__(
            f"报文版本 {schema_version!r} 高于当前支持的 {CURRENT_SCHEMA_VERSION}，"
            "已保留原文等待升级"
        )
        self.schema_version = schema_version
        #: 原始报文逐字保留，升级后可重新进入解析。
        self.raw = raw


@dataclass(frozen=True)
class MigrationTrace:
    """一次迁移的可追溯记录：从哪个版本迁来、应用了哪些步骤与显式假设。"""

    from_version: int
    to_version: int
    steps: tuple[str, ...]
    assumptions: tuple[str, ...]


@dataclass(frozen=True)
class DomainRecord:
    """当前结构（v1）的领域记录。``occurred_at`` 一律为带时区时间。"""

    schema_version: int
    record_id: str
    domain: str
    occurred_at: datetime
    revision: int
    source: str


@dataclass(frozen=True)
class ParsedPacket:
    """一次读取的完整结果：当前结构记录 + 原始报文 + 版本与迁移轨迹。"""

    record: DomainRecord
    #: 原始报文逐字保留（迁移与未来版本场景下供追溯与重放）。
    raw: Mapping[str, Any]
    #: 报文自声明的版本（迁移前版本）。
    detected_version: int
    #: 旧版迁移轨迹；原生当前版本为 ``None``。
    migration: Optional[MigrationTrace] = None


def _is_int(value: Any) -> bool:
    # bool 是 int 的子类，但合同里修订号/版本号不接受布尔。
    return isinstance(value, int) and not isinstance(value, bool)


def _require_object(payload: Any) -> Mapping[str, Any]:
    if not isinstance(payload, dict):
        raise JsonStructureError(f"报文顶层必须是 JSON 对象，实际为 {type(payload).__name__}")
    return payload


def _parse_json_text(text: str) -> Mapping[str, Any]:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise JsonStructureError(f"报文不是合法 JSON：{exc}") from exc
    return _require_object(payload)


def _detect_version(payload: Mapping[str, Any]) -> int:
    if "schema_version" not in payload:
        raise FieldContractError("缺少必填字段：schema_version")
    version = payload["schema_version"]
    if not _is_int(version):
        raise FieldContractError(
            f"schema_version 必须是整数，实际为 {version!r}"
        )
    return version


def _parse_iso8601(value: str, *, field_name: str = "occurred_at") -> datetime:
    text = value.strip()
    # 兼容常见的 Z 后缀写法，统一转为显式偏移。
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise SemanticContractError(
            f"{field_name} 不是合法 ISO 8601 时间：{value!r}"
        ) from exc
    return parsed


def _require_aware(moment: datetime, *, field_name: str = "occurred_at") -> datetime:
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise SemanticContractError(
            f"{field_name} 必须带时区偏移（如 2026-09-20T09:00:00+08:00），"
            f"实际为 {moment.isoformat()!r}"
        )
    return moment


def _validate_v1_fields(payload: Mapping[str, Any]) -> None:
    """第 2 层：v1 字段合同。"""

    missing = sorted(V1_FIELDS - payload.keys())
    if missing:
        raise FieldContractError(f"缺少必填字段：{', '.join(missing)}")
    unknown = sorted(set(payload) - V1_FIELDS)
    if unknown:
        raise FieldContractError(f"出现未知字段：{', '.join(unknown)}")
    if payload["schema_version"] != CURRENT_SCHEMA_VERSION:
        raise FieldContractError(
            f"schema_version 应为 {CURRENT_SCHEMA_VERSION}，实际为 {payload['schema_version']!r}"
        )
    for name in ("record_id", "domain", "occurred_at", "source"):
        if not isinstance(payload[name], str):
            raise FieldContractError(
                f"{name} 必须是字符串，实际为 {type(payload[name]).__name__}"
            )
    if not _is_int(payload["revision"]):
        raise FieldContractError(
            f"revision 必须是整数，实际为 {payload['revision']!r}"
        )


def _validate_v1_semantics(payload: Mapping[str, Any]) -> DomainRecord:
    """第 3 层：v1 业务语义。"""

    record_id = payload["record_id"].strip()
    if not record_id:
        raise SemanticContractError("record_id 不能为空")
    domain = payload["domain"].strip()
    if domain not in ALLOWED_DOMAINS:
        raise SemanticContractError(
            f"domain {domain!r} 不在允许清单 {sorted(ALLOWED_DOMAINS)} 内"
        )
    occurred_at = _require_aware(_parse_iso8601(payload["occurred_at"]))
    revision = payload["revision"]
    if revision < 1:
        raise SemanticContractError(
            f"revision 必须是不小于 1 的递增正整数，实际为 {revision}"
        )
    if not payload["source"].strip():
        raise SemanticContractError("source 不能为空")
    return DomainRecord(
        schema_version=CURRENT_SCHEMA_VERSION,
        record_id=record_id,
        domain=domain,
        occurred_at=occurred_at,
        revision=revision,
        source=payload["source"],
    )


def _migrate_v0_to_v1(payload: Mapping[str, Any]) -> tuple[dict[str, Any], MigrationTrace]:
    """把兼容期旧版（v0）报文迁移为当前结构，迁移假设全部显式记录。

    v0 与 v1 字段同名，差异仅在语义：v0 允许无时区时间（按本地交接时区
    解释）和从 0 起的修订号（迁移后整体 +1，与当前结构从 1 起对齐）。
    """

    steps: list[str] = []
    assumptions: list[str] = []

    unknown = sorted(set(payload) - V1_FIELDS)
    if unknown:
        raise FieldContractError(f"旧版报文出现未知字段：{', '.join(unknown)}")
    missing = sorted((V1_FIELDS - {"schema_version"}) - payload.keys())
    if missing:
        raise FieldContractError(f"旧版报文缺少必填字段：{', '.join(missing)}")
    for name in ("record_id", "domain", "occurred_at", "source"):
        if not isinstance(payload[name], str):
            raise FieldContractError(
                f"旧版报文 {name} 必须是字符串，实际为 {type(payload[name]).__name__}"
            )
    if not _is_int(payload["revision"]):
        raise FieldContractError(
            f"旧版报文 revision 必须是整数，实际为 {payload['revision']!r}"
        )
    if payload["revision"] < 0:
        raise SemanticContractError(
            f"旧版报文 revision 允许从 0 起，但不允许为负：{payload['revision']}"
        )

    migrated = {name: payload[name] for name in V1_FIELDS if name in payload}
    migrated["schema_version"] = CURRENT_SCHEMA_VERSION

    moment = _parse_iso8601(str(payload["occurred_at"]))
    if moment.tzinfo is None or moment.utcoffset() is None:
        assumptions.append(
            f"旧版 occurred_at 未带时区，按本地交接时区 {LEGACY_LOCAL_TIMEZONE} 解释"
        )
        moment = _parse_iso8601(moment.isoformat() + LEGACY_LOCAL_TIMEZONE)
        steps.append("occurred_at 补记时区偏移")
    migrated["occurred_at"] = moment.isoformat()

    migrated["revision"] = payload["revision"] + 1
    steps.append("revision 从 0 起编号迁移为从 1 起（+1）")

    trace = MigrationTrace(
        from_version=LEGACY_SCHEMA_VERSION,
        to_version=CURRENT_SCHEMA_VERSION,
        steps=tuple(steps),
        assumptions=tuple(assumptions),
    )
    return migrated, trace


def parse_record(payload: Any) -> ParsedPacket:
    """解析单条报文：三层校验 + 版本路由。

    - 当前版本：校验通过后直接返回。
    - 兼容期旧版：可追溯迁移为当前结构后再做完整校验。
    - 未来版本：抛出 ``FutureVersionError``，原文随异常保留。
    """

    obj = _require_object(payload)
    version = _detect_version(obj)
    if version > CURRENT_SCHEMA_VERSION:
        raise FutureVersionError(version, raw=dict(obj))
    if version == LEGACY_SCHEMA_VERSION:
        migrated, trace = _migrate_v0_to_v1(obj)
        _validate_v1_fields(migrated)
        record = _validate_v1_semantics(migrated)
        return ParsedPacket(
            record=record, raw=dict(obj), detected_version=version, migration=trace
        )
    if version != CURRENT_SCHEMA_VERSION:
        raise FieldContractError(f"不支持的 schema_version：{version!r}")
    _validate_v1_fields(obj)
    record = _validate_v1_semantics(obj)
    return ParsedPacket(
        record=record, raw=dict(obj), detected_version=version, migration=None
    )


def parse_json_text(text: str) -> ParsedPacket:
    """从 JSON 文本解析（先做第 1 层结构校验，再进入版本路由）。"""

    return parse_record(_parse_json_text(text))


def load_record(path: Path) -> DomainRecord:
    """读取单个报文文件并返回当前结构记录（保持既有调用方式）。

    旧版报文会被可追溯迁移；未来版本抛出 ``FutureVersionError``。
    需要迁移轨迹时请改用 ``parse_json_text`` / ``parse_record``。
    """

    return parse_json_text(Path(path).read_text(encoding="utf-8")).record
