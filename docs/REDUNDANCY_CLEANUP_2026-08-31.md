# QuantDesk 冗余清理与无回归约束

> 建立日期：2026-08-31
> 目标：在不改变当前 UI、交互、API 契约和交易语义的前提下，逐批移除新旧架构并存产生的重复入口与兼容代码。

## 1. 硬性约束

- 当前线上 UI 是唯一视觉与交互基准，不重做页面，不顺带调整样式或文案。
- 每个清理批次只处理一个职责边界；不得把页面迁移、策略优化和数据迁移混入同一提交。
- 仍被生产页面引用的静态 JS/CSS 不得按“旧代码”直接删除。
- 仍承载生产数据的 `legacy_signal` 兼容读取不得先于数据迁移删除。
- 服务器只允许 Git 快进拉取、构建和重启；不得直接修改服务器源码。
- 每批必须通过全量 Pytest、Ruff、前端 OpenAPI/Lint/TypeScript/build 和线上只读浏览器冒烟。

## 2. UI 冻结基线

- 冻结范围：`web/`、`src/quantdesk_v2/static/` 及六个现有页面控制器。
- 本批修改前，受 Git 管理的 UI 资源树指纹：`646490b5a53e65aeeb6e73270f32df4ca089ad2f`。
- 页面验收路径：`/ai-monitor` 加载 -> “历史机会” -> 返回“当前机会”。
- 验收内容：页面身份、非空渲染、无框架错误层、控制台健康、标签切换和数据恢复。

## 3. 已完成批次

### C1：移除市场 worker 内的模拟盘兼容启动分支

状态：已完成开发与本地回归。

- `market_engine.start()` 现在只启动行情、深度、K 线、新闻和社交采集。
- 模拟盘只允许由 `worker_runtime._start_paper()` 启动独立 `paper-runtime`。
- 删除 `include_paper` 参数和 `market_engine -> paper_engine` 延迟导入。
- 增加架构门禁，禁止市场 worker 再次导入或启动模拟盘运行时。
- 对外 API、数据库模型、策略规则、页面资源均未修改。

消除的风险：

- 市场 worker 与模拟盘 worker 同时消费同一模拟账户。
- 重启或部署配置差异导致模拟盘被重复启动。
- 市场采集职责重新侵入交易执行职责。

## 4. 后续清理顺序

### C2：AI Monitor 实现下沉

- 将七个应用服务的真实实现从 `ai_monitor.py` 逐个迁入明确模块。
- 原入口先保留为薄兼容门面，所有调用者切换完成并通过契约测试后再删除。
- 每次只迁移一个领域：机会生成、市场特征、新闻评分、宏观环境、事件门禁、预测结算、查询投影。

### C3：API 单体拆分

- 按路由域拆分 `interfaces/api/ai_monitor.py` 和 `api.py`。
- 保持 URL、请求参数、响应字段、状态码和权限完全不变。
- 为每个被迁移路由保存 OpenAPI 与响应契约测试。

### C4：历史回放解除私有依赖

- 将 `historical_replay.py` 使用的私有 AI Monitor 函数提升为稳定应用端口。
- 调用全部切换后删除跨模块私有函数依赖。

### C5：legacy 数据迁移

- 先盘点并迁移现存 legacy 用户策略、模板和 paper/live 快照。
- 迁移必须提供 dry-run、数量核对、抽样比对和可恢复备份。
- 生产数据全部完成迁移前，只停止新增 legacy 数据，不删除兼容读取。

### C6：前端旧实现清理

- 这是最后一批，不重新设计 UI。
- 只有在 React 页面实现与现有页面逐像素、逐交互、逐接口等价后，才删除对应旧 JS/CSS。
- 必须按页面逐个完成；不得一次删除全部静态控制器或回退入口。

## 5. 当前禁止删除

- 生产页面仍引用的 `monitor.js`、`ai-monitor.js`、`paper.js`、`live.js`、`backtest.js`、`strategies.js` 及相关 CSS。
- `paper_positions`、`paper_trades`、`paper_order_executions`。
- `live_order_intents`、Binance 对账、保护单和异常恢复逻辑。
- Execution Journal、幂等 claim、worker 租约、风险预算、Safe Mode、Kill Switch 和审计事实。
- legacy 生产数据仍存在时所需的兼容读取代码。

## 6. 完成定义

一个清理批次只有同时满足以下条件才可部署：

1. Git diff 不包含该批次范围以外的文件。
2. UI 冻结批次的 UI 资源树指纹保持一致。
3. 全量后端测试和前端检查通过。
4. 线上只读冒烟无新增控制台错误，关键交互与部署前一致。
5. 部署后 API、全部 worker、数据库 revision 和 readiness 均正常。
