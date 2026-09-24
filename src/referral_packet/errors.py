"""收件链路的错误类型。

读取一条记录分为三个层次，任一层失败都抛出对应错误，
调用方（护士站、批量收件）可据此判断能否继续处理：

* :class:`StructuralError` —— JSON 本身无法解析，或顶层不是对象；
* :class:`ContractError`   —— JSON 是对象，但不符合字段合同
  （字段缺失、类型错误、时间不带时区等）；
* :class:`SemanticError`   —— 字段合同成立，但违反业务语义
  （修订号非正数、修订未递增、领域不被接收等）。

另有 :class:`FutureVersionError`，表示记录来自更新版本的软件，
当前版本不得猜测其含义，只能原文留存等待升级。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class RecordError(Exception):
    """所有与单条记录相关的错误的基类。

    ``location`` 为该记录在本次提交中的溯源位置（批次与序号），
    由收件服务在批量入口处填入；直接调用解析器时可以为空。
    """

    message: str
    layer: str = ""
    location: str | None = None
    details: dict[str, Any] = field(default_factory=dict)

    def __str__(self) -> str:  # pragma: no cover - 仅用于日志展示
        prefix = f"[{self.location}] " if self.location else ""
        return f"{prefix}{self.layer}: {self.message}"


class StructuralError(RecordError):
    """JSON 结构层失败：不是合法 JSON，或顶层不是 JSON 对象。"""

    def __init__(self, message: str, **kwargs: Any) -> None:
        super().__init__(message, layer="structure", **kwargs)


class ContractError(RecordError):
    """字段合同层失败：字段缺失、类型错误、取值形态不被允许。"""

    def __init__(self, message: str, **kwargs: Any) -> None:
        super().__init__(message, layer="contract", **kwargs)


class SemanticError(RecordError):
    """业务语义层失败：合同成立，但业务规则不允许接收。"""

    def __init__(self, message: str, **kwargs: Any) -> None:
        super().__init__(message, layer="semantic", **kwargs)


class FutureVersionError(RecordError):
    """记录使用当前软件不认识的未来结构版本。

    不属于任何校验层：解析器不会去猜测未来字段的含义，
    原文必须原样留存，待软件升级后再处理。
    """

    def __init__(self, message: str, **kwargs: Any) -> None:
        super().__init__(message, layer="version", **kwargs)


def error_location(batch_id: str, slot: int) -> str:
    """生成批次内的标准溯源位置，例如 ``batch-7 / #3``。"""

    return f"{batch_id} / #{slot}"


def attach_location(exc: RecordError, batch_id: str, slot: int) -> RecordError:
    """在错误上补填溯源位置，供隔离区与后续追查使用。"""

    if exc.location is None:
        exc.location = error_location(batch_id, slot)
    return exc
