# QuantDesk 冗余清理最终状态

> 收尾日期：2026-08-31
> 安全基线标签：`pre-full-cleanup-20260831-e14edb0`
> 约束：不改变现有 UI、URL、API、权限、交易语义和历史事实；服务器只执行 Git 快进部署。

## 1. 最终结论

本轮计划内的工程清理已经完成，没有剩余的代码开发批次。生产部署后只需完成只读 legacy 数据盘点和运行状态验收；这两项是生产核验，不是继续重构代码。

本轮没有直接删除数据库历史，也没有重写交易记录。用户已明确不需要数据库备份，因此部署流程不会创建数据库备份，但仍会执行迁移版本、健康状态和只读数据关系检查。

## 2. 完成明细

| 编号 | 工作 | 状态 | 完成证据 |
| --- | --- | --- | --- |
| C1 | 市场 worker 与模拟盘运行时解耦 | 完成 | 独立 paper runtime；架构测试禁止反向依赖 |
| C2 | AI Monitor 领域职责下沉 | 完成 | 机会持久化、新闻评分、事件门禁、预测结算、Projection/read model 均进入 application 或 persistence 边界 |
| C3 | API 单体拆分 | 完成 | 账户路由迁入 `trading_accounts.py`；运行/新闻/回放路由迁入 `ai_monitor_runs.py`；OpenAPI 路由拆分前后零差异 |
| C4 | 历史回放解除私有依赖 | 完成 | `historical_replay.py` 不再依赖 `ai_monitor._*` |
| C5 | legacy 新增入口封闭与只读审计 | 完成 | 新 paper 只写 `strategy_event_v2`；新增 `audit_legacy_strategy_data.py`，只 rollback、不写数据库 |
| C6 | 前端死代码清理 | 完成 | 删除未接入入口的 React 管理页试验代码；生产静态控制器与样式保留 |

## 3. 本轮结构变化

- `ai_monitor.py` 减少约 2,777 行，将确定性领域逻辑和数据库适配器移出单体。
- `api.py` 减少约 1,440 行，将 paper/live 账户接口按域拆分。
- AI Monitor 查询模型的实现迁入 `infrastructure/persistence/ai_monitor_read_models.py`，旧路径只保留导出兼容。
- AI Monitor 运行、新闻分析和回放接口独立成 router。
- 删除 3 个没有生产入口引用的 React 文件及对应未使用 API client，共约 560 行。
- 新增 legacy 策略数据只读盘点工具，用活动部署、paper 模式和 live 快照关系判断能否移除运行时兼容。

## 4. UI 不变证明

清理前后执行相同的前端生产构建，以下用户实际加载的制品哈希完全一致：

| 制品 | SHA-256 |
| --- | --- |
| 主 JavaScript | `44D9B0369034B9C949EFACF6E04754B65F54DC425787E7D7F11A3A6586669E7F` |
| 主 CSS | `82B6B2A4207E1545D1D5451A8531463833670C2462BE74A70E41B321DA7687FB` |
| 主入口 HTML | `0CF4994BDA47B5749DBDA09C8FFF6F0947CA34FC504E8307484E3EB3AAD69D98` |
| 管理后台 HTML | `EB691BDB7D47F76E2EFF3E33E501DCAD3FAA021F5D0BF3F765EFD1B565554770` |

因此本轮前端删除不会改变布局、颜色、文案、按钮、弹窗、筛选、展开/收起或实时刷新行为。

## 5. 刻意保留、不是待完成项

以下代码仍有生产职责，不能为了减少行数而删除：

- 当前页面使用的静态 JS/CSS 控制器；React 负责入口和生命周期，静态控制器仍负责已验收 UI 行为。
- `ai_monitor.py` 的运行调度、流式行情入口和机会扫描顶层编排；领域计算和持久化权威已经下沉，顶层编排本身不是重复实现。
- `legacy_signal` 历史读取兼容；只有生产只读审计证明不存在活动部署、legacy paper 模式和 live 快照关联后，才允许进一步收缩枚举和模型。
- paper/live 成交事实、幂等 claim、worker 租约、Binance 对账、保护单、风险控制和审计事实。
- 真实资金 Canary。当前无实盘，不启用账户、不下单；Canary 属于获得明确资金授权后的生产验收，不是代码清理任务。

## 6. 验收结果

- 全量 Pytest：通过。
- Ruff（`src`、`tests`、`scripts`）：通过。
- OpenAPI artifact 与 TypeScript schema：已重新生成并通过一致性检查。
- ESLint：通过。
- TypeScript：通过。
- Vite 生产构建：通过。
- 前端生产制品哈希：清理前后完全一致。

## 7. 部署后检查

1. Git 必须快进到本轮提交，服务器源码不得手改。
2. 数据库 revision 必须保持 `0079_position_snapshot_facts` 或迁移到仓库 head。
3. API、market/ai/paper/shadow/live/ops worker 必须全部 active。
4. `/api/v2/health` 返回正常，`/api/v2/ready` 返回 ready。
5. 运行 `scripts/audit_legacy_strategy_data.py`，记录生产 active blockers；不自动删除历史记录。
6. 保持 `GLOBAL_LIVE_ENABLED=0`、活动 live 账户为 0，除非用户另行明确授权。
7. 浏览器验证首页、发现机会、策略、模拟盘、实盘、回测和管理后台，并检查控制台无新增错误。
