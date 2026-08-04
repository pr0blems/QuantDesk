# 多轮新闻情报裁决

新闻模块把事实可信度、标的影响、市场确认和系统可用性分开保存。旧的标题情绪仅用于兼容展示，不参与交易或预测评分。

## 状态链

`DETECTED → PROVENANCE_OK → FACT_VERIFIED → IMPACT_ASSESSED → CHALLENGED → VALIDATED → MARKET_CONFIRMED → REFERENCE_ELIGIBLE`

争议、证伪、数据不足和过期分别使用 `DISPUTED`、`REFUTED`、`DATA_INSUFFICIENT` 和 `EXPIRED`。每次评估都写入 `news_assessment_rounds`，包含证据、拒绝原因、评估器和版本。

## 强制门槛

- 官方原始来源，或至少两个去除转载后的独立来源。
- 数值型事实必须得到官方来源或独立来源复核。
- 标的必须使用词边界、歧义上下文和事件角色进行直接映射。
- ETF 和反向产品按 `exposure` 关系处理，置信度低于直接实体。
- 必须经过延时价格和主动买卖流市场确认。
- 一小时前向结果至少积累 20 个无泄漏样本，且平均方向收益为正。
- 连续两轮方向稳定、事件未过期且没有未解决反证。

未满足全部条件的事件只能是 `display_only`、`observe`、`risk_only` 或 `blocked`。

## 交易隔离

`verified_event_pressure`、`rumor_pressure`、`event_risk_gate` 和 `news_data_quality` 会写入多空预测的不可变特征快照，但当前 `news_weight=0.0`、`feature_state=shadow_only`。新闻不能开仓、反手、修改止损或覆盖强制风控。

## 前向校准

方向裁决自动生成 15 分钟、1 小时和 4 小时结果任务。`news_event_outcomes` 保存入场价、到期价、原始收益、方向收益和命中标签；工作进程持续回填这些结果，用于后续走步校准，而不是把未校准分数称为概率。
