# AI 交易提案与决策账本

## 目标与安全边界

本阶段将 AI 定位为“交易提案生成器”，而不是交易执行器。新增链路为：

```text
模型 JSON 输出
  → TradeProposal 严格验证
  → ProposalGate 确定性门控
  → append-only DecisionLedger
  → （未来）独立的确定性 RiskEngine
  → （未来）OrderIntent / Broker
```

当前代码没有把该链路注册到实盘路由，也没有向 `AiProposalService` 注入
`Broker`。所有阶段的 `ProposalGateResult.may_submit_order` 固定为 `false`；
`risk_review_required` 只表示允许交给后续确定性风控，并不表示允许下单。

## 对既有 AI 模块的审计结论

- `strategy_ai.py` 已对策略编辑预览做字段白名单、严格 JSON、数值边界、
  重复键和危险文本校验。其输出是策略草稿，不是交易指令。
- `news_ai.py` 已对新闻关联、情绪和批次摘要做结构校验与批次恢复。其输出
  是研究证据，不是交易指令。
- `ai_providers.py` 使用服务端提供商注册表固定网络目标，可以避免由数据库或
  请求参数注入任意上游 URL。
- `StrategyRevision` 是不可变策略快照，`StrategyDeployment` 固定 revision，
  因此提案门控使用 `strategy_version_id` 精确匹配，不接受“当前最新版”这种
  会漂移的引用。
- 通用 `AuditLog` 适合记录用户安全操作，但不适合作为高频模型决策账本：它
  没有事件 hash chain，也没有 prompt/model/input/output 哈希的稳定契约。

因此，新交易提案能力没有复用策略编辑或新闻分析的输出，也没有让上述模块
直接调用 Broker。

## TradeProposal v1

模型只允许输出以下结构：

```json
{
  "schema_version": 1,
  "strategy_version_id": "strategy-42:revision-7",
  "symbol": "BTCUSDT",
  "action": "OPEN_LONG",
  "confidence": 0.82,
  "thesis": ["trend_confirmed", "volume_expansion"],
  "invalidation": "close_below_structure",
  "observed_at": "2026-08-06T03:59:00+00:00",
  "valid_until": "2026-08-06T04:04:00+00:00",
  "requested_risk": {
    "risk_fraction": 0.002,
    "max_slippage_bps": 12,
    "leverage": 2,
    "stop_loss_pct": 1.5,
    "take_profit_pct": 4.0
  }
}
```

约束包括：

- 顶层和风险对象都禁止未知字段；模型不能加入订单类型、数量、账户或交易所
  字段。
- 只接受单个 JSON object；重复键、非有限数、错误 UTF-8、超大响应均拒绝。
- confidence 和风险字段必须是 JSON number，不能用字符串绕过类型约束。
- 开仓必须明确请求风险；平仓和 HOLD 不得请求新增风险。
- 时间必须带时区，`valid_until` 必须晚于行情观测时间。
- thesis 和 invalidation 有长度、数量、控制字符、URL、代码及常见密钥模式
  限制。
- `trade_proposal_json_schema()` 可传给支持结构化输出的模型；无论上游是否支持
  JSON Schema，返回内容仍必须经过 Pydantic 验证。

## ProposalGate

门控按稳定 reason code 检查：

- 策略 revision 精确匹配及发布状态；
- symbol 是否属于部署 universe；
- 行情观测时效、未来时钟偏差、提案到期时间和有效窗口；
- 最低置信度；
- risk fraction、滑点和杠杆上限；
- canary 使用比 live 更小的 risk fraction 上限。

发布阶段语义如下：

| 阶段 | 通过门控后的结果 | 是否可直接下单 |
|---|---|---:|
| replay | `record_only`；允许 draft revision | 否 |
| shadow | `record_only`；必须 published | 否 |
| manual | 等待人工批准；批准后进入确定性风控 | 否 |
| canary | 进入确定性风控，使用更紧风险上限 | 否 |
| live | 进入确定性风控 | 否 |

## 决策溯源与 append-only 账本

每次评估保存：

- `prompt_hash`：Prompt 模板 SHA-256；
- `model_hash`：provider、model name 和 model version 的规范化 SHA-256；
- `input_hash`：规范化输入快照 SHA-256；
- `output_hash`：模型原始输出字节 SHA-256；
- 通过验证的结构化提案（无原始 Prompt/行情上下文）；
- 门控结果、reason codes、发布阶段和评估时间；
- 无法解析时只保存脱敏的 validation code，不保存原始输出。

`DecisionLedger` 是应用层端口。内存实现使用全局递增 sequence、
`previous_record_hash` 和 `record_hash` 形成可验证链，并且不暴露 update/delete
方法。生产数据库适配器应使用 INSERT-only 表、唯一 `event_id`、唯一 sequence、
事务内锁定链尾，并限制应用数据库账号的 UPDATE/DELETE 权限。

## 后续接入要求

1. 模型调用处必须使用 `trade_proposal_json_schema()` 并将返回文本原样交给
   `AiProposalService.evaluate()`。
2. API 只应暴露 replay/shadow 试运行；canary/live 接入必须另行完成权限、
   Preflight、Kill Switch 和账户级风控评审。
3. 后续 RiskEngine 只能消费 `RISK_REVIEW_REQUIRED` 结果，并再次从可信行情源
   获取价格和账户状态，不能信任模型给出的价格、仓位或账户数据。
4. Broker 只能消费 RiskEngine 产生的 `ApprovedOrderIntent`，不得消费
   `TradeProposal`。
5. Prompt、模型或输入数据发生变化时必须产生新的 hash；生产账本记录不得覆盖。

