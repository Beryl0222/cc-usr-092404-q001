"""结构版本迁移注册表。

每个旧版本必须在此**显式登记**一个迁移，说明字段如何变化，
不存在“猜着读”的兼容：

* 已登记的旧版本：按登记的规则迁移，迁移结果重新通过当前合同校验，
  迁移链（从某版到某版、迁移名称、依据的规则）随记录留痕；
* 未登记的旧版本：读取入口报合同错误并隔离，不静默接受；
* 未来版本：不在此处理，由解析器原文留存。

v0 → v1 的登记规则（基层旧系统导出格式）：

* ``domain`` 取值 ``"referral"`` 是旧领域别名，显式映射为
  ``"referral_packet"``，其他旧值一律拒绝；
* ``occurred_at`` 为本地无时区时间，v0 信封必须同时给出
  ``timezone``（旧系统部署时登记的固定时区）；迁移只按该**显式声明**
  附加时区，声明缺失时拒绝迁移而不是假设时区；
* ``revision`` 在 v0 中允许从 0 起编，迁移时显式加 1 对齐 v1 的
  “修订号必须为正且递增”语义。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable

from .errors import ContractError

# 当前结构版本由此处唯一来源决定，迁移链的终点永远对齐它。
from .contracts import CURRENT_SCHEMA_VERSION  # noqa: E402

# v0 旧领域别名 → 当前领域。显式表，不做模糊匹配。
V0_DOMAIN_ALIASES = {
    "referral": "referral_packet",
}

# v0 信封允许显式声明的时区。旧系统全部部署在 +08:00，
# 因此这里只接受这一个声明值；出现其他值说明来源超出本迁移的知识范围。
V0_DECLARED_TIMEZONES = {
    "+08:00": timezone(timedelta(hours=8)),
}

MIGRATE_RESULT = tuple[dict[str, object], list[dict[str, object]]]


@dataclass(frozen=True)
class Migration:
    """一条已登记的结构迁移。"""

    from_version: int
    to_version: int
    name: str
    rules: tuple[str, ...]
    apply: Callable[[dict[str, object]], dict[str, object]]

    def describe(self) -> dict[str, object]:
        return {
            "from_version": self.from_version,
            "to_version": self.to_version,
            "name": self.name,
            "rules": list(self.rules),
        }


def _migrate_v0_to_v1(payload: dict[str, object]) -> dict[str, object]:
    for key in ("record_id", "domain", "occurred_at", "revision", "source"):
        if key not in payload:
            raise ContractError(
                f"v0 记录缺少迁移所需字段 {key!r}",
                details={"reason": "legacy_field_missing", "field": key},
            )

    legacy_domain = payload["domain"]
    if legacy_domain not in V0_DOMAIN_ALIASES:
        raise ContractError(
            f"v0 领域值 {legacy_domain!r} 不在显式别名表内，无法迁移",
            details={"reason": "unknown_legacy_domain", "value": legacy_domain},
        )
    domain = V0_DOMAIN_ALIASES[legacy_domain]

    declared_tz = payload.get("timezone")
    if declared_tz not in V0_DECLARED_TIMEZONES:
        # 关键：没有显式时区声明时绝不假设一个时区。
        raise ContractError(
            "v0 记录缺少可信的时区声明，迁移不得猜测发生时间",
            details={"reason": "missing_timezone_declaration", "value": declared_tz},
        )
    tz = V0_DECLARED_TIMEZONES[declared_tz]

    naive_text = payload["occurred_at"]
    try:
        parsed = datetime.fromisoformat(str(naive_text))
    except ValueError as exc:
        raise ContractError(
            f"v0 occurred_at 无法解析：{naive_text!r}",
            details={"reason": "bad_legacy_timestamp"},
        ) from exc
    if parsed.tzinfo is not None:
        raise ContractError(
            "v0 occurred_at 不应带时区，来源格式与迁移登记不符",
            details={"reason": "unexpected_timezone"},
        )
    occurred_at = parsed.replace(tzinfo=tz)

    legacy_revision = payload["revision"]
    if not isinstance(legacy_revision, int) or isinstance(legacy_revision, bool):
        raise ContractError(
            "v0 revision 必须是整数",
            details={"reason": "bad_legacy_revision"},
        )
    if legacy_revision < 0:
        # 负数修订在旧版本同样没有定义：迁移不会把它“洗”成合法值。
        raise ContractError(
            "v0 revision 为负数，旧版本下亦无此语义，无法迁移",
            details={"reason": "negative_legacy_revision", "value": legacy_revision},
        )

    return {
        "schema_version": 1,
        "record_id": payload["record_id"],
        "domain": domain,
        "occurred_at": occurred_at.isoformat(),
        "revision": legacy_revision + 1,
        "source": payload["source"],
    }


_MIGRATIONS: dict[int, Migration] = {}


def register(migration: Migration) -> None:
    """登记一个迁移；重复登记同一来源版本属于程序错误。"""

    if migration.from_version in _MIGRATIONS:
        raise RuntimeError(f"版本 {migration.from_version} 的迁移已登记")
    _MIGRATIONS[migration.from_version] = migration


def registered_versions() -> tuple[int, ...]:
    return tuple(sorted(_MIGRATIONS))


register(
    Migration(
        from_version=0,
        to_version=1,
        name="v0_to_v1",
        rules=(
            "domain: 'referral' 别名显式映射为 'referral_packet'",
            "occurred_at: 仅按 v0 信封显式声明的 timezone(+08:00) 附加时区",
            "revision: 旧版从 0 起编，显式加 1 对齐 v1 正数递增语义",
        ),
        apply=_migrate_v0_to_v1,
    )
)


def migrate_to_current(
    payload: dict[str, object], observed_version: int
) -> MIGRATE_RESULT:
    """把已登记的旧版本载荷迁移到当前结构。

    返回迁移后的载荷与完整迁移链（每一跳都可追溯）。
    任何一跳未登记或失败都抛出 :class:`ContractError`，不做猜测。
    """

    chain: list[dict[str, object]] = []
    current = payload
    version = observed_version
    seen: set[int] = set()

    while version < CURRENT_SCHEMA_VERSION:
        if version in seen:
            raise ContractError(
                "迁移链出现循环，拒绝处理",
                details={"reason": "migration_cycle", "version": version},
            )
        seen.add(version)
        migration = _MIGRATIONS.get(version)
        if migration is None:
            raise ContractError(
                f"结构版本 {version} 没有登记迁移，无法进入当前结构",
                details={"reason": "unregistered_legacy_version", "version": version},
            )
        if migration.to_version <= version:
            # 迁移必须使结构版本前进，否则登记本身有误。
            raise ContractError(
                "登记的迁移没有使结构版本前进",
                details={"reason": "non_advancing_migration", "version": version},
            )
        current = migration.apply(current)
        chain.append(migration.describe())
        version = migration.to_version

    return current, chain
