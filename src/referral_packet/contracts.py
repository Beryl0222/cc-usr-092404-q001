"""当前版本的转诊资料字段合同。

合同只描述“一条记录长什么样”，不负责读取流程。
读取时的三层区分（结构 / 合同 / 语义）见 :mod:`referral_packet.parsing`，
版本迁移见 :mod:`referral_packet.migrations`。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

# 当前资料结构版本。只增不减：
# 旧版本经 migrations 中登记的迁移进入当前结构；
# 大于当前版本的记录一律原文留存，等待软件升级，不猜测其含义。
CURRENT_SCHEMA_VERSION = 1

# 本院接收的领域。领域存在且形态合法属于字段合同，
# 是否为本院接收的领域属于业务语义。
ACCEPTED_DOMAINS = frozenset({"referral_packet"})

RECORD_ID_PATTERN = re.compile(r"^[^\s\x00-\x1f\x7f]{1,200}$", re.UNICODE)
DOMAIN_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")

FIELDS = ("schema_version", "record_id", "domain", "occurred_at", "revision", "source")


@dataclass(frozen=True)
class DomainRecord:
    """通过当前合同与业务语义校验后的一条领域记录。

    ``occurred_at`` 始终是带时区的时间；需要字符串形式时使用
    :attr:`occurred_at_iso`，其输出保留原始偏移量。
    """

    schema_version: int
    record_id: str
    domain: str
    occurred_at: datetime
    revision: int
    source: str

    def __post_init__(self) -> None:
        if self.schema_version != CURRENT_SCHEMA_VERSION:
            raise ValueError("DomainRecord 只能承载当前结构版本")
        if self.occurred_at.tzinfo is None:
            raise ValueError("DomainRecord.occurred_at 必须带时区")

    @property
    def occurred_at_iso(self) -> str:
        return self.occurred_at.isoformat()

    def to_contract_payload(self) -> dict[str, object]:
        """转成符合当前字段合同的普通字典。"""

        return {
            "schema_version": self.schema_version,
            "record_id": self.record_id,
            "domain": self.domain,
            "occurred_at": self.occurred_at_iso,
            "revision": self.revision,
            "source": self.source,
        }
