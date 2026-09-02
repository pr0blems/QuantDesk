# 《马丁 TP4》配置等价与策略内核开发计划

> 日期：2026-09-02
> 输入源码：`D:/Desktop/马丁TP4.mq4`（v02h，revision 23.02.2018）
> 目标：先建立可证明的 MQ4 参数与决策语义等价层，再接入老虎行情回放、Shadow、模拟盘和 Binance 受控实盘。
> 安全边界：本阶段不启用真实订单；研究兼容行为与实盘安全行为必须显式分层。

## 实施状态（2026-09-02）

- P0 配置等价层：已完成；
- P1 纯领域策略内核：已完成首版并通过表驱动测试；
- P2 老虎历史 Bar 回放：首版已完成；逐 Tick 成交顺序消歧待补充；
- P3 Shadow 与恢复：待开发；
- P4 模拟盘与 Canary：待 P2/P3 验收后开发；
- 当前实盘状态：未接入、未启用。

## 1. 源码结论

该 EA 不是单方向普通马丁，而是三种运行方式共用的一套多订单篮子管理器：

- `auto`：由自适应 ATR 箱体突破创建首单，随后在固定箱体上下沿之间反向开腿；
- `recovery`：首单由操作员给出，随后在首单保存的上下边界之间反向开腿；
- `grid`：首单由操作员给出，同方向每逆向移动 `Distance` 点继续加腿；
- `auto/recovery` 达到 `GridDrift` 订单数后临时切换到 `grid`，篮子清空后恢复原模式；
- `auto/recovery` 使用多空合并后的净敞口保本价，`grid` 按多、空方向分别计算保本价；
- TP 随订单数从 `TP` 依次切换到 `TP2/TP3/TP4`；
- `SL_Dollar` 是账户货币口径的整个篮子止损；
- `Overlap` 只在 `grid` 执行，最后一腿盈利覆盖第一腿亏损的 `100% + OverlapPercent` 后同时关闭首尾腿。

## 2. 参数逐项映射

| MQ4 参数 | QuantDesk 字段 | 精确语义 |
|---|---|---|
| `ChooseTrading` | `mode` | `0=auto, 1=recovery, 2=grid` |
| `NewCycle` | `new_cycle` | 只控制 auto 无持仓时能否创建新周期 |
| `Lot` | `sizing.initial_lot` | 固定首腿手数，也是自动手数公式的基数 |
| `Autolot` | `sizing.autolot` | 是否按余额动态计算首腿 |
| `Autolotsize` | `sizing.autolot_balance_unit` | `余额 / Autolotsize * Lot` |
| `mm` | `sizing.lot_multiplier` | 第 N 腿为首腿手数乘 `mm^N` |
| `MaxLot` | `sizing.max_lot` | 单腿手数上限 |
| `MaxOrders` | `sizing.max_orders` | auto/recovery 按总腿数，grid 按方向计数 |
| `GridDrift` | `ladder.grid_drift_order_count` | auto/recovery 转 grid 的腿数；允许大于 MaxOrders 表示不触发 |
| `MaxSpred` | `execution.max_spread_points` | 新开腿的最大执行点差 |
| `Distance` | `ladder.distance_points` | grid 同向逆势加腿间距 |
| `TP` | `take_profit.base_points` | 默认篮子 TP 点数 |
| `Kol_Ord_for_TP2` | `take_profit.tier2_min_orders` | TP2 生效腿数 |
| `TP2` | `take_profit.tier2_points` | 第二档 TP 点数 |
| `Kol_Ord_for_TP3` | `take_profit.tier3_min_orders` | TP3 生效腿数 |
| `TP3` | `take_profit.tier3_points` | 第三档 TP 点数 |
| `Kol_Ord_for_TP4` | `take_profit.tier4_min_orders` | TP4 生效腿数 |
| `TP4` | `take_profit.tier4_points` | 第四档 TP 点数 |
| `SL_Dollar` | `stop.basket_loss_currency` | 0 关闭；浮动损益低于负值时全平 |
| `TrailStart` | `trailing.start_points` | 0 关闭；且只有小于当前 TP 时才进入追踪逻辑 |
| `TrailDistance` | `trailing.distance_points` | 从有利极值回撤的点数 |
| `Overlap` | `overlap.enabled` | 是否允许 grid 首尾腿配对退出 |
| `OverlapOrderNumber` | `overlap.min_orders` | 单方向至少达到该腿数才判断 |
| `OverlapPercent` | `overlap.excess_percent` | 11 表示最后腿盈利至少覆盖首腿亏损的 111% |
| `Start_Hour/End_Hour` | `session.*` | 仅限制 auto 新周期，0 表示不限制 |
| `Magic` | `compatibility.magic` | 仅用于 MQ4 导入导出；平台实际使用 deployment/cycle ID |
| `Section` | `compatibility.section_points` | 原版依赖图表可视高度；生产环境不使用该 UI 耦合条件 |
| `Show*` | `compatibility.display.*` | 仅保存兼容，不参与服务端交易决策 |

箱体参数虽然不是 `extern/input`，仍作为高级参数开放：`BoxLength`、`BoxTimeFrame`、`BoxRange`、`AutoBoxRange`、D1 ATR 周期/系数及 `BoxBufferPips`。

## 3. 双语义边界

### 3.1 研究兼容层

- 严格复现模式切换、加腿方向、手数、TP 档位、金额止损和 Overlap 公式；
- 允许记录 `Magic`、`Section` 和 MT4 显示参数，以便导入旧预设；
- 回放输出每一次决策的 reason code、输入快照和目标价格；
- 对原版缺陷使用 `legacy_compatibility` 标签，不静默修改结果。

### 3.2 实盘安全层

- 行情或基差异常只阻止开仓/加仓，绝不阻止减仓、止损和强平保护；
- `Section` 不读取网页或图表视口，实盘固定禁用；
- Binance 数量精度、最小名义价值和保证金检查位于执行授权器；
- 策略止损之外必须配置灾难止损、周期损失、保证金占用和日损失上限；
- 任何重启均从成交和持仓事实恢复篮子，不能只依赖内存腿序号。

## 4. 开发阶段与验收

### P0：配置等价层（本批）

- 新增全部 MQ4 输入的强类型模型；
- 支持 MQ4 变量名 JSON 和旧版 CSV 顺序的无损导入/导出；
- 修复 `GridDrift`、`OverlapPercent`、`MaxSpred`、时段和追踪语义；
- 风险预览显示每腿手数、累计敞口以及不可达配置警告。

验收：默认配置导入后再次导出，所有交易参数逐项一致。

### P1：纯领域策略内核（本批）

- 实现有效模式切换；
- 实现 auto 箱体突破、recovery 反向腿、grid 同向腿；
- 实现 TP 档位、金额止损、追踪水平和 Overlap 首尾配对；
- 所有决策纯函数化，不依赖 FastAPI、SQLAlchemy、Tiger 或 Binance。

验收：表驱动测试覆盖多/空、三模式、阈值边界和退出优先级。

### P2：老虎历史回放

- 用已落库的 Tiger M15/D1 数据构建箱体和 ATR；
- 增加逐 bar 回放，必要时补 Tiger tick 数据进行成交顺序消歧；
- 输出数据集哈希、配置快照、滑点/手续费和逐腿账本。

验收：相同数据集与配置重复运行结果一致；缺口数据拒绝出结果。

实施结果（2026-09-02）：

- 已实现 Wilder D1 ATR 与仅使用已收盘历史数据的有状态动态箱体；箱体按包含关系延续、突破后失效，当前 K 线不会进入自身边界计算；
- 已实现 `auto`、`recovery`、`grid` 三模式逐 Bar 回放，输出逐腿成交、周期盈亏、权益、回撤、手续费和 reason code；
- 已加入手续费、滑点、合成点差、确定性 OHLC 路径、配置/数据集/运行哈希；
- 已加入 Tiger/Binance 核验映射、全局质量门禁、请求区间预热覆盖和盘中 K 线缺口门禁；
- 固定箱体不再错误依赖 D1 ATR；自适应箱体缺少 D1 预热数据时拒绝回放；
- 研究兼容策略与安全退出策略可显式选择，默认回放使用 `research_compatibility`；
- 当前只计算用于交易的上下边界数值，不绘制图表线段；可视化画线留到策略中心 UI 阶段；
- 原 MQ4 `_get_range` 用 `MODE_HIGH` 查找低点的明显缺陷未复刻，回放结果会显式记录该修正；
- 结果明确标记 Tiger 参考价格并非 Binance 合约成交价格，当前未计资金费率与合约乘数；
- API 为 `/api/v2/basket-strategies/martingale-tp4/backtests`，只写数据清单和审计日志，不改变实盘状态。

### P3：Shadow 与恢复

- 接 Tiger 实时信号和 Binance 只读执行快照；
- 验证断线、重复 tick、进程重启、部分成交和订单未知状态；
- 运行至少 20 个交易日，比较理论价格与可成交价格。

验收：无重复腿、无孤儿订单、退出永不被数据门禁阻塞。

### P4：模拟盘与 Canary

- 先接模拟执行，再按单账户、单标的、最小仓位开启 Canary；
- Canary 必须具备独立 kill switch、最大篮子名义价值和自动降级；
- 未达到预设回撤、成交偏差和故障恢复指标不得扩大范围。

## 5. 本批明确不做

- 不直接启用 Binance 实盘；
- 不把 AI 评分加入原策略；
- 不用 Binance K 线静默替代 Tiger 信号；
- 不复制原版“点差过大时阻止平仓”的危险行为到实盘；
- 不用新增指标掩盖行情缺口、映射错误或任务延迟。
