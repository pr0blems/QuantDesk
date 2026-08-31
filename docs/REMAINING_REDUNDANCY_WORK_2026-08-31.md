# QuantDesk 待完成的冗余清理工作清单

> 盘点日期：2026-08-31
> 适用基线：包含本文件的当前部署批次
> 文档性质：已完成事实、剩余工作、删除门槛与验收顺序
> 硬性约束：不改变当前 UI、交互、URL、API 契约、交易语义和生产数据；服务器只通过 Git 快进部署。

## 1. 当前结论

六阶段业务架构升级已经完成工程主体，但“冗余清理”才刚进入按边界逐批删除阶段。当前不应按文件大小直接删除代码，剩余工作的核心是先让生产调用离开兼容门面，再删除已经没有调用方的旧实现。

当前主要热点规模：

| 文件/资源 | 当前规模 | 说明 |
| --- | ---: | --- |
| `src/quantdesk_v2/ai_monitor.py` | 11,010 行 | 七个 AI Monitor 领域仍有较多兼容实现集中在单体中 |
| `src/quantdesk_v2/interfaces/api/ai_monitor.py` | 4,806 行 | 路由、查询装配、实盘跟单和配置职责仍集中 |
| `src/quantdesk_v2/api.py` | 3,783 行 | 模拟盘、实盘账户和其他旧路由仍待按域拆分 |
| `src/quantdesk_v2/historical_replay.py` | 1,815 行 | 仍有 1 处跨模块私有函数依赖 |
| `src/quantdesk_v2/static/*.js` | 9 个文件 / 18,625 行 | 当前生产页面控制器和回退实现，不能直接删除 |
| `src/quantdesk_v2/static/*.css` | 9 个文件 / 6,183 行 | 当前 UI 基准样式，禁止在架构清理中改动 |

因此，现阶段可以确认仍有大量兼容和职责混合代码，但不能把全部静态前端或 `legacy_signal` 一次性删除。清理必须继续按 C2 至 C6 顺序执行。

## 2. 本批已经完成

### C1：市场 worker 与模拟盘运行时解耦

- [x] 删除 `market_engine.start(include_paper=...)` 兼容分支。
- [x] 市场 worker 不再导入或启动模拟盘。
- [x] 模拟盘只由独立 `paper-runtime` 启动。
- [x] 增加架构门禁，禁止依赖重新出现。

### C2-1A：市场特征读取下沉（本批）

- [x] 在 `MonitorRepository` 建立公开 `market_flow_input_rows()` 边界。
- [x] API 和 AI Monitor 主扫描不再调用 `repository._query()`。
- [x] 新增 `infrastructure/persistence/ai_monitor_market_features.py`，负责最新特征快照和市场输入 Map 的数据库装配。
- [x] 将实时市场特征响应标准化迁入 `application/ai_monitor/market_features.py`。
- [x] 生产 API 不再调用 `ai_monitor._market_flow_input_maps`、`ai_monitor.latest_realtime_feature_snapshots` 或 `ai_monitor.realtime_feature_payload`。
- [x] 旧函数曾暂时保留为薄兼容门面，C2-1B 已在全仓库调用方归零后删除。
- [x] 增加响应形状、最新快照选择、公开仓储边界和架构防回退测试。

### C4：历史回放解除私有依赖

- [x] 将冻结消融分类迁入稳定的 `application.ai_monitor` 应用端口。
- [x] `historical_replay.py` 不再引用任何 `ai_monitor._*` 私有函数。
- [x] AI Monitor 内部也改为调用公开分类端口，旧函数只保留薄兼容委托。
- [x] 增加完整信号、缺失域、报价拒绝、方向冲突和架构防回退测试。

### 本批验证结果

- [x] 定向测试：8 项通过。
- [x] 全量后端测试：1,048 项通过，106 项按环境条件跳过。
- [x] Ruff：`src`、`tests`、`scripts` 全部通过。
- [x] 前端 OpenAPI、ESLint、TypeScript 和 Vite 构建全部通过。
- [x] `web/`、`src/quantdesk_v2/static/`、`src/quantdesk_v2/react_static/` 无 Git 差异。
- [x] 未修改数据库结构、策略参数、API 字段或页面资源。

## 3. 待完成工作（按执行顺序）

### C2-1B：删除市场特征兼容门面（已完成）

优先级：高；风险：低；数据库迁移：否。

- [x] 将 `ai_monitor.py` 内剩余的市场特征内部调用切到公开应用函数。
- [x] 盘点插件、CLI、测试和运维脚本，确认不再引用 3 个旧门面。
- [x] 在调用方归零并完成兼容观察后删除：
  - `ai_monitor._market_flow_input_maps`
  - `ai_monitor.latest_realtime_feature_snapshots`
  - `ai_monitor.realtime_feature_payload`
- [x] 保留 API 响应快照测试，确保字段、空值和数值类型完全不变。
- [x] 增加架构门禁，阻止旧门面定义再次进入 `ai_monitor.py`。

完成定义：全仓库不再调用上述旧入口，删除后全量测试与线上 AI Monitor 当前/历史机会切换通过。

### C2-2：机会生成实现下沉

优先级：高；风险：中；数据库迁移：否。

- [ ] 将候选生成、去重、新闻消费窗口和准入编排从 `ai_monitor.py` 迁入 `opportunity_generation` 应用服务。
  - [x] 迁移监控品种过滤、同事件候选去重、已消费新闻窗口和单品种最强候选选择。
  - [ ] 迁移候选持久化、准入决策编排与机会 Projection 写入。
- [ ] 将 SQL 查询放入 persistence adapter，应用层只接收确定性输入。
- [ ] 保留现有 decision/version、方向、多空数量和机会状态语义。
- [ ] 先保留薄门面；调用方全部切换后再删除旧实现。

完成定义：同一冻结输入产生完全相同的 symbol、direction、score、reason codes、expires_at 和 Projection。

### C2-3：新闻评分实现下沉

优先级：高；风险：中。

- [ ] 拆出新闻聚合、去重、方向选择、相关性和可行动性计算。
  - [x] 迁移同来源/分类快讯 burst 聚类，供评分和消费窗口共用。
  - [ ] 迁移新闻聚合、方向选择、相关性与可行动性主体实现。
- [ ] 保证 AI 只提供结构化解释/候选，确定性代码继续决定评分和消费窗口。
- [ ] 增加重复新闻、同一事件多股票、多空冲突和中性新闻金样测试。

完成定义：新闻关联数量、偏多/偏空方向和事件消费结果与当前生产完全一致。

### C2-4：宏观环境与事件门禁实现下沉

优先级：中；风险：中。

- [ ] 将 macro regime、市场广度、收益率代理和事件时间窗计算迁出单体。
- [ ] 将事件前后窗口、临近事件和 `无临近事件` 判定统一到稳定公共端口。
- [ ] 保留当前 UI 展示字段和门禁原因码。

完成定义：历史冻结时钟下的宏观状态、事件窗口和多空仓位系数逐条一致。

### C2-5：预测结算实现下沉

优先级：高；风险：高。

- [ ] 迁移到期查找、路径指标、退出原因、成本后一致收益和结算 Projection。
- [ ] 保持 `cost_consistent_exit_v8` 等历史版本可读，不重写历史事实。
- [ ] 用冻结行情覆盖多头、空头、止损、止盈、锁盈、到期和数据不可用。

完成定义：同一预测的 exit price、reason、gross/net return、MFE/MAE 和命中结果完全一致。

### C2-6：查询投影与单体收缩

优先级：中；风险：中。

- [ ] 将剩余 Projection 刷新和读取装配移入明确 adapter。
- [ ] 禁止 current opportunity 查询重新出现 source fallback。
- [ ] 删除七个领域已经无调用方的兼容实现。
- [ ] 重新统计 `ai_monitor.py`，目标是只保留入口编排与暂时兼容表面。

完成定义：Projection 不可用时继续明确失败，不使用源表结果伪装当前状态。

### C3：API 单体按路由域拆分

优先级：中；风险：中。

- [ ] 拆分 AI Monitor 查询、运行、配置、新闻、回放、手动跟单和市场详情路由。
- [ ] 拆分 `api.py` 中模拟账户、实盘账户和策略部署相关路由。
- [ ] 保存并对比 OpenAPI；URL、方法、参数、响应、状态码和权限不得变化。
- [ ] 每次只迁移一个 router，并保留旧 import 路径兼容期。

完成定义：OpenAPI 无非预期差异，前端无需修改任何 API 调用。

### C4：历史回放解除私有依赖（已完成）

优先级：高；风险：低。

- [x] 将 `historical_replay.py` 使用的 `ai_monitor._ablation_signal_state` 提升为稳定应用端口。
- [x] 增加冻结回放结果测试。
- [x] 删除跨模块私有调用并建立架构门禁。

完成定义：`historical_replay.py` 不再引用任何 `ai_monitor._*` 函数，历史回放结果不变。

### C5：legacy 数据盘点、停止新增与迁移

优先级：中；风险：高；数据库操作：需要独立审批和可恢复方案。

- [ ] 只读统计 legacy 用户策略、模板、修订、paper/live 关联和历史快照数量。
- [ ] 确认所有生产写入口不再新增 legacy 记录。
- [ ] 编写 dry-run 迁移：逐表数量、孤儿关系、版本映射、抽样内容哈希。
- [ ] 先备份和演练，再迁移；失败必须可恢复。
- [ ] 生产数量与哈希核对完成后，才允许删除兼容读取和模型枚举。

完成定义：生产 legacy 数量归零或被明确封存为只读历史，所有新事实只写标准模型。

### C6：旧前端与回退层清理

优先级：最后；风险：很高。

- [ ] 为六个页面建立截图、DOM、关键交互和接口调用基线。
- [ ] 证明 React 正式入口不再依赖旧自定义元素包装。
- [ ] 逐页面识别“生产控制器”“紧急回退包装”“真正无引用代码”，不得按目录整体删除。
- [ ] 每完成一个页面的生产等价与回退策略，再删除该页面确认无引用的包装代码。
- [ ] CSS 只允许删除无引用规则；不得改变当前视觉。

完成定义：删除前后截图、布局、文案、按钮、弹窗、筛选、滚动、展开/收起和实时刷新完全一致。

## 4. 独立生产验收项

### 第三阶段真实资金 Canary

状态：工程已完成，待用户明确授权；不属于冗余代码开发。

- [ ] 明确 live account ID。
- [ ] 明确最大投入、单仓上限、允许方向、观察窗口和最少成交数。
- [ ] 完成完整窗口，重复意图、长期 UNKNOWN、未保护持仓、持仓差异和风险结算超时均为 0。

未获得上述边界前，不启用账户、不下真实订单，也不把空仓审计当作 Canary 通过。

## 5. 当前禁止删除

- 当前页面控制器使用的 9 个静态 JS 和 9 个静态 CSS 文件。
- React 制品缺失时仍承担回退职责的静态入口。
- `paper_positions`、`paper_trades`、`paper_order_executions`。
- `live_order_intents`、Execution Journal、幂等 claim、worker 租约。
- Binance 对账、保护单、持仓同步和异常恢复。
- 风险预算、Safe Mode、Kill Switch 和审计事实。
- legacy 生产数据尚未完成迁移时需要的兼容读取。

## 6. 后续每批统一验收模板

每个清理批次必须同时满足：

1. 仅修改一个职责边界；不混入 UI、策略调参或无关重构。
2. Git diff 中 `web/`、静态页面资源和 React 构建产物保持不变，除非该批明确属于 C6。
3. 行为等价测试和架构防回退测试通过。
4. 全量 Pytest、Ruff、前端 OpenAPI/Lint/TypeScript/build 通过。
5. 服务器只执行 Git 快进拉取、构建、迁移（如有）和服务重启。
6. `/api/v2/health`、`/api/v2/ready`、全部 worker 与数据库 revision 正常。
7. 线上 AI Monitor 的当前/历史机会切换和浏览器控制台无新增错误。
8. 只有调用方归零、兼容窗口完成并有回退路径时，才删除旧门面或数据。

## 7. 推荐下一批

下一批继续 **C2-2：机会生成实现下沉**。候选过滤、事件去重、消费窗口和单品种选择已迁入应用层；下一步迁移候选持久化与准入决策编排，不改机会方向、评分、状态或 API 投影。
