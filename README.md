# 转诊资料收件（最小充分交付）

基层向县医院转诊时交换最小充分的资料包。本模块负责**资料合同**与**收件流程**：
坏记录不会再被当成正常资料，护士可以从任何一个阻塞点追到原始报文、迁移版本
和最终处理结论。

## 读取必须经过三层

`referral_packet.parsing.parse_record_text` 严格按层校验，失败点决定处置：

| 层次 | 检查内容 | 失败错误 |
| --- | --- | --- |
| JSON 结构层 | 文本是合法 JSON，顶层必须是对象 | `StructuralError` |
| 字段合同层 | 字段齐全、类型正确；`record_id`、`domain` 形态合法；`occurred_at` 是**带时区偏移**的 ISO 8601 时间；未知字段拒绝 | `ContractError` |
| 业务语义层 | `revision` 为正整数；相对该记录上一版**严格递增**（`validate_revision_advances`）；`domain` 在本院接收范围内 | `SemanticError` |

本次事故的两种坏资料——负数修订号、无时区时间——分别在语义层、合同层被拦下。

当前合同（`contracts.CURRENT_SCHEMA_VERSION = 1`）字段：

```json
{
  "schema_version": 1,
  "record_id": "sample-019",
  "domain": "referral_packet",
  "occurred_at": "2026-09-20T09:00:00+08:00",
  "revision": 1,
  "source": "业务样例"
}
```

## 版本策略：登记迁移、未来留存

* **旧版本**必须在 `migrations.py` **显式登记**迁移后才能读入，迁移结果重新通过
  当前合同，完整迁移链（版本跳变、迁移名、规则说明）随记录留痕。
  已登记的 v0 → v1：`referral` 领域别名显式映射；无时区时间**只按旧信封显式
  声明的 `timezone: "+08:00"`** 补时区（没有声明就拒绝，绝不猜测）；
  旧版 0 起编的修订号显式 +1。未登记的旧版本拒绝读入。
* **未来版本**（`schema_version` 大于当前版本）不解释任何字段，原文逐字留存
  （结论 `HELD_FUTURE`），等待软件升级后再处理。

## 批量收件

```python
from referral_packet.intake import IntakeService

with IntakeService("./intake-data") as svc:
    result = svc.ingest_batch(items)   # items: list[str | bytes]
    for o in result.outcomes:
        print(o.slot, o.outcome, o.location)
```

* 坏记录只产生自身的隔离结论（`QUARANTINED`），同批合法记录不受影响；
* 合法记录严格保持输入顺序受理；
* 每条原文先归档到 `raw/<批次>/<序号>.json` 再判定，任何结论都能回溯到原始字节。

五种结论：

| 结论 | 含义 |
| --- | --- |
| `ACCEPTED` | 首次受理，随后发送到件通知 |
| `REPLAYED` | 完全重传（规范化内容哈希一致），返回既有结论，不重复落库/通知 |
| `REVIEW` | 同标识内容变化，进人工复核，系统不自动覆盖旧版；事件中带“修订是否递增”的显式校验证据 |
| `QUARANTINED` | 坏记录、未登记版本，或不同内容撞已签章修订；只隔离自身 |
| `HELD_FUTURE` | 未来版本，原文留存等待升级 |

复核经 `svc.resolve_review(review_id, decision="ADMIT" | "REJECT")` 处理：
批准只以**追加事件**受理（`review_admitted`），历史版本从不改写或删除。

## 签章保护

`svc.seal_accepted(record_id, revision)` 对已受理修订签章后：

* 同修订号的不同内容一律 `QUARANTINED`（`sealed_revision_protected`），
  不得覆盖已签章材料；
* 签章材料的**完全重传**仍是 `REPLAYED`（重传不是覆盖）；
* 复核批准目标若在复核期间已签章，批准同样被拒。

## 崩溃恢复：只补未完成动作

状态全部由只追加的 `journal.jsonl` 重放得到，任何两步之间停机都安全。

```python
svc.recover()
# {'resumed_slots': [...], 'delivered_notifications': [...]}
```

* 原文已归档但没有终态结论的位置：从归档原文重新判定（不重复归档）；
* 已受理但没有 `notification_delivered` 事件的通知：补送一次；
* 已有终态/已送达的动作绝不重复（批次号重投同样幂等，零新事件）。

## 溯源

`svc.trace(location="B1 / #3")` 或 `svc.trace(review_id=...)` 从任一阻塞点返回：
批次位置、原文路径与 SHA-256、观测到的结构版本与迁移链、完整事件链、
当前结论、同标识全部已受理版本及签章状态；重传结论会继续指向既有结论的
原始位置（含复核的批准/驳回）。合同层失败时记录身份尚未确认，只以
`claimed_record_id` / `claimed_revision` 申报值呈现，避免被当作可信标识；
语义层失败（如负数修订）则给出确认过的 `record_id` / `revision`。
`svc.blocked_points()` 列出护士站待处理的全部阻塞点。

## 目录布局

```
intake-data/
├── journal.jsonl         # 唯一事实源：只追加事件
├── notifications.jsonl   # 通知出口回执（可替换为自己的 Notifier）
└── raw/<batch>/NNNN.json # 原始报文归档（含坏记录与未来版本）
```

## 测试与构建

```bash
python3 -m unittest discover -s tests
python3 -m compileall -q src tests
```
