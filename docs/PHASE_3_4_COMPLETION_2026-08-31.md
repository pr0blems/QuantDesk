# QuantDesk 第三、第四阶段收尾说明

> 日期：2026-08-31
> 范围：实盘执行接管、AI Monitor 治理
> 原则：服务器只通过 Git 快进部署；不擅自启用实盘账户，不以真实资金验证代替用户授权。

## 1. 状态结论

| 阶段 | 工程状态 | 生产验收状态 | 说明 |
| --- | --- | --- | --- |
| 第三阶段：实盘接管 | 已完成 | 等待一次用户授权的真实账户 Canary 窗口 | 实盘判断已使用统一执行内核；新增可持久化、可审计、只观察的 Canary 证据链 |
| 第四阶段：AI Monitor 治理 | 已完成 | 部署后核对 Projection 与 worker 健康度 | 七个职责已有明确应用服务边界；执行、统计和结算均标记为确定性权限；当前机会查询不再回退源表 |

第三阶段的“工程完成”和“真实资金生产验收通过”必须分开描述。当前代码已经具备完整的 Canary 工具，但系统不会自行选择账户、启用账户或提交订单。因此，在用户没有明确指定账户、观察时长、最少成交数和风险预算前，不会把真实资金 Canary 标记为通过。

## 2. 第三阶段完成内容

实盘链路已经统一为：

```text
DecisionEnvelope
  -> ExecutionService
  -> LiveExecutionRuntime
  -> Binance Broker
  -> Execution / Position facts
  -> ProtectionService
  -> Reconciliation / Recovery
```

已经完成的工程验收项：

- `live_engine` 不再维护第二套策略方向、周期或退出判断。
- 开仓、平仓、保护单、对账、持仓同步和异常恢复通过公共应用服务与 Broker 适配器执行。
- 订单意图和执行幂等状态持久化，worker 重启不会为同一信号重复开仓。
- UNKNOWN、长时间未决、保护单不完整和本地/交易所事实差异都有恢复路径与只读审计。
- 新增 `live_canary_runs` 和 `live_canary_samples`，记录完整观察窗口和每次采样事实。
- ops worker 会持续采样运行中的 Canary；任何瞬时异常都会永久保留并使该窗口失败，不能在最终时刻被“正常状态”掩盖。
- Canary 启动只允许观察已经由用户主动启用的账户；命令本身不会启用账户，也不会提交订单。

Canary 命令：

```text
quantdesk-v2 start-live-canary --account-id <ID> --window-minutes <分钟> --minimum-open-fills <数量> --confirm
quantdesk-v2 audit-live-canary --run-id <PUBLIC_ID>
quantdesk-v2 cancel-live-canary --run-id <PUBLIC_ID> --confirm
```

通过条件：完整观察窗口内重复意图、UNKNOWN、超时未决、未保护持仓、worker/tick 过期和账户错误均为 0，并达到事先声明的最少开仓成交数。

## 3. 第四阶段完成内容

AI Monitor 已建立以下七个应用服务边界：

| 服务 | 权限与职责 |
| --- | --- |
| `opportunity_generation` | 确定性机会扫描、运行结果和 Projection 刷新编排 |
| `market_features` | 行情特征读取与标准化，不决定成交 |
| `news_scoring` | 新闻证据聚合与方向候选，不直接产生订单 |
| `macro_regime` | 宏观状态快照与上下文计算 |
| `event_gate` | 事件风险和可行动性门控 |
| `prediction_settlement` | 确定性结算、旧记录恢复和统计投影刷新 |
| `opportunity_projection` | 当前机会的只读查询模型，禁止静默读取源表兜底 |

治理规则：

- AI 只负责解释、候选方案和参数建议；AI 输出不是订单事实，也不能直接结算预测。
- 方向门控、执行、收益统计和预测结算由确定性代码负责。
- 每个阶段返回权限标记和版本，运行记录可以解释使用了哪套规则。
- 当前机会接口只从 `ai_monitor_opportunity_current` 读取。Projection 未部署或落后时返回明确的 `503 opportunity_projection_not_ready`，不再把源表结果伪装成当前状态。
- SQLAlchemy 查询实现位于 infrastructure 适配器；application 层保持框架无关，架构边界测试会阻止依赖倒退。

为降低一次性重写风险，原 `ai_monitor.py` 中仍保留兼容函数实现，但主编排已经通过上述服务边界执行。这些兼容函数不再构成第二套权威链路，后续可按第五、第六阶段的页面和数据模型迁移逐批删除。

## 4. 数据库变更

迁移：`0077_live_canary_observations`

新增表：

- `live_canary_runs`：账户、观察窗口、状态、失败码、聚合指标和完成时间。
- `live_canary_samples`：每次采样的原始失败码与指标快照。

回滚迁移只删除这两张观测表，不改变实盘账户、订单、成交或持仓事实。

## 5. 上线验收清单

- [ ] Git 服务器工作树无本轮 tracked 修改，并快进到目标提交。
- [ ] Alembic 当前版本为 `0077_live_canary_observations`。
- [ ] API、market、AI、paper、shadow、live、ops worker 全部 active。
- [ ] `/api/v2/ready` 返回 `ready`。
- [ ] AI Monitor Projection 对账无落后和价格来源违规。
- [ ] `audit-paper --json` 与 `audit-live` 无阻断异常。
- [ ] 用户指定 Canary 账户、观察窗口、最少成交数和风险预算后，完成真实观察窗口。

最后一项属于真实资金操作授权，不由部署动作自动执行。
