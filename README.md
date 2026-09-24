# 转诊资料最小交付

基层与县医院按病情和用途交换最小充分的转诊资料包。

`fixtures/` 保存经过脱敏的业务样例：`packet_manifest.json` 为当前结构（v1），
`packet_manifest_legacy_v0.json` 为兼容期旧版，`packet_manifest_future_v2.json`
为未来版本。源代码定义读取与接收这些报文所需的合同与收件流程。

## 数据合同（`referral_packet.contracts`）

读取时分三层校验，各层错误类型不同，便于定位问题性质：

1. **JSON 结构层**（`JsonStructureError`）：报文必须是合法 JSON 对象。
2. **字段合同层**（`FieldContractError`）：必填字段齐全、类型正确、无未知字段。
3. **业务语义层**（`SemanticContractError`）：`record_id` 非空、`domain` 在允许
   清单内、`occurred_at` 必须带时区偏移、`revision` 必须是不小于 1 的整数。

负修订号与无时区时间会在语义层被明确拒绝，不再被当成正常资料。

### 版本路由

- `schema_version == 1`：当前结构，直接校验。
- `schema_version == 0`：兼容期旧版，经可追溯迁移进入当前结构。迁移假设
  （无时区时间按本地交接时区 `+08:00` 解释、修订号从 0 起迁为从 1 起）显式
  记录在 `MigrationTrace.assumptions` / `steps` 中，原始报文逐字保留在
  `ParsedPacket.raw`。
- `schema_version > 1`：抛出 `FutureVersionError`，原文随异常保留，等待结构
  升级后重放，不做任何猜测解释。

## 收件流程（`referral_packet.intake`）

`Inbox.receive_batch` 批量收件：

- 坏记录（任一层校验失败）只隔离自身进入隔离区，合法记录严格保持输入顺序
  继续处理；回执（`Receipt`）按输入顺序返回。
- 同一记录完全重传（内容逐字一致，字段顺序无关）返回既有处理结论
  （`duplicate`），不重复落库、不重复通知。
- 同一记录内容变化进入复核区（`review`），既有材料一律不覆盖；已签章材料
  （`Registry.seal`）施加签章保护（`sealed_protected`）。
- 未来版本进入待升级区（`held_future_version`），原文保留。

### 停机恢复

每条合法记录依次经过 `persisted`（落库）→ `notified`（到件通知）两个阶段，
阶段写入登记表。落库与通知之间发生停机时批次中止，`BatchResult.resume_position`
指出未处理到的下一个原始位置；`Inbox.recover()` 只对停在 `persisted` 的记录
补做通知，已通知的不重放、已落库的不重落，恢复动作幂等。

### 追溯

`Inbox.trace(position)` 按原始位置取处理回执；隔离/待升级/复核区记录
（`registry.blocked[position]`）保留原文、失败层、迁移轨迹与处理结论，
接收方可从每个阻塞点追到原始位置、迁移版本和处理结论。

## 测试与构建

执行测试：

```bash
python3 -m unittest discover -s tests
```

执行编译检查：

```bash
python3 -m compileall -q src tests
```
