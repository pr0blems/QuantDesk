# 第一、第三（工程）与第六阶段收尾报告

> 日期：2026-08-31
>
> 数据库目标：`0079_position_snapshot_facts`
> 原则：不修改现有 UI，不调整交易策略参数，不以测试结果替代真实资金授权。

## 结论

- 第一阶段统一交易语义：**100% 完成**。
- 第三阶段实盘接管工程：**100% 完成**；真实资金 Canary 生产验收待用户明确授权。
- 第六阶段数据模型治理：**100% 完成**。

## 第一阶段验收闭环

系统继续以 `TimeframePolicy`、`ExitPolicy` 和 mode-neutral `DecisionEnvelope` 为统一语义来源。在已有回测、Shadow、模拟盘金样一致性测试基础上，生产 Canary 新增逐笔语义签名检查：

- 订单意图必须关联持久化 StrategySignal；
- 订单入口快照中的 DecisionEnvelope 必须与 StrategySignal 中的信封哈希完全一致；
- symbol、timeframe、decision、strategy revision、deployment 必须一致；
- 开仓 BUY/SELL 必须与 LONG_ENTRY/SHORT_ENTRY 对应；
- 缺失信封返回 `semantic_signature_missing`；
- 信封被改写返回 `semantic_signature_mismatch`。

因此同一历史事件在回测、Shadow、模拟盘和实盘交付层的语义漂移都会被测试或 Canary 阻断。

## 第三阶段工程闭环

实盘已经使用统一 ExecutionService、Binance Broker、保护单服务、订单对账、持仓同步与异常恢复。Canary 工具只观察用户已启用账户，不会自行启用账户或扩大资金范围；本次新增语义一致性指标后，它同时验收：

- 重复/UNKNOWN/长期未决订单；
- 过期执行 claim；
- 未保护受管持仓；
- worker 和 tick 新鲜度；
- 最少真实开仓成交数；
- 决策信封在实盘交付过程中没有被改写。

真实资金 Canary 必须另行明确：账户 ID、最大投入/单仓上限、允许方向、观察窗口和最少成交数。未获得这些边界前，不提交真实订单。

## 第六阶段数据边界

| 事实/模型 | 权威职责 |
| --- | --- |
| StrategyRevision | 不可变策略版本 |
| StrategyDeployment | paper/shadow/live 的真实部署关系 |
| BacktestRun | 独立回测任务及其修订归属 |
| StrategyRunManifest | Deployment 或 BacktestRun 的不可变运行输入 |
| StrategySignal | 可审计 Decision |
| OrderIntent / Execution Journal | 幂等订单意图与执行事实 |
| PositionSnapshot | 由持久成交结果产生的不可变持仓状态 |
| Projection | 可重建查询模型，不参与反向决策 |

### 迁移行为

`0078_data_model_governance`：

1. 为 BacktestRun 增加公开 ID、用户策略和不可变修订归属；
2. 将旧 backtest deployment 的归属迁移到 BacktestRun；
3. 将 RunManifest 改为互斥 owner；
4. 为所有历史回测补建缺失的 RunManifest；
5. 删除伪 backtest deployment，并在数据库层禁止再次创建。

`0079_position_snapshot_facts`：

1. 建立 append-only `position_snapshots`；
2. 使用来源执行键保证 paper/live 重放不重复；
3. 从既有 paper execution 和 live order intent 回填历史持仓事实；
4. 新成交在原事务/执行投影链中持续写入持仓快照。

## 验收门槛

- Ruff 全量通过；
- Pytest 全量通过；
- Alembic 只能保持单一 head；
- 生产升级到 `0079_position_snapshot_facts`；
- API、worker、readiness 和只读实盘审计均正常；
- 真实资金 Canary 仅在用户明确边界后执行。
