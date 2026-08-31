# QuantDesk 物理删除与最终收口计划

日期：2026-08-31

基线提交：`88989fd8aee759a57119816a4af0c4259b403aca`

安全标签：`pre-full-cleanup-20260831-e14edb0`

## 目标与不可突破的边界

本轮目标是删除已经退出生产主链路的兼容实现、旧运行标记和旧页面壳，而不是重做产品。以下内容必须保持不变：

- 当前 React 外壳和六个业务页面 controller 的视觉、布局和交互；
- API 路径、交易方向、时间周期、止盈止损和订单幂等语义；
- 历史回测、成交、持仓、策略修订和审计事实；
- Binance 对账、保护单、异常恢复以及执行核心中的风险约束；
- 实盘总开关保持关闭，迁移期间不允许产生真实资金订单。

用户已明确本轮不做数据库备份。数据库迁移因此设计为不可逆升级，不提供自动 downgrade。

## 生产盘点结果

删除前只读盘点得到：

- 旧类型策略模板 19 条；
- 旧类型用户策略 40 条，其中 38 条是正在使用的系统内置算法，2 条为已归档人工策略；
- 策略修订 41 条；
- 引用这些修订的部署 18 条，包括运行中的模拟盘 3 条、暂停或错误的实盘 3 条以及停止的模拟盘；
- 关联回测 4 条、持仓快照 38 条、策略制品 41 条、运行清单 22 条、校验记录 41 条；
- 实盘总开关为关闭，实盘账户 4 个、启用账户 0 个、运行中实盘部署 0 个。

这些记录不是可删除垃圾。直接删除会破坏历史事实和外键关系，因此采用“保留主键与事实、原位改为正式内置策略类型”的方式收口。

## 执行批次

### A. 策略运行时兼容层

- 将 `legacy_signal` 正式迁移为 `builtin_strategy`；
- 将 `legacy_v1` 正式迁移为 `builtin_v1`；
- 同步策略模板、用户策略、修订快照、模拟盘快照、实盘快照和校验报告；
- 删除模拟盘配置里的旧迁移辅助字段；
- 数据库约束和默认值不再允许写入旧类型；
- 删除旧审计脚本和只服务旧切换流程的测试；
- 运行时统一使用 `BuiltinEvidenceBuilder`、`evaluate_builtin` 和 `execution_tuple`。

### B. 旧前端页面壳

- 删除 `src/quantdesk_v2/static/index.html`；
- 删除 `src/quantdesk_v2/static/app.js`；
- 主路由只允许返回构建后的 React 页面；构建缺失时明确返回 503，不再静默回退旧 UI；
- 保留当前 React 页面实际调用的 `controller-runtime.js`、`monitor.js`、`ai-monitor.js`、`paper.js`、`live.js`、`strategies.js`、`backtest.js` 和全部现行样式。

线上删除前已验证 `/` 与 `/next/` 返回同一份 React 根页面，均不存在旧侧栏页面壳。

### C. 明确保留的历史兼容数据

以下内容不能因为包含 `legacy` 字样就批量删除：

- Alembic 历史迁移文件；
- 已结算预测、旧新闻记录和审计文本中的历史来源说明；
- 对旧数据只读展示的“旧记录字段缺失”提示；
- 用户仍可能访问的安全重定向，例如 `/credentials` 到 `/settings`；
- 当前 React 页面仍调用的 controller 资产。

它们要么是不可变历史，要么仍有真实调用，不属于死代码。

## 验收门槛

本地必须全部满足：

1. Ruff 和全量 pytest 通过；
2. Alembic 只有 `0080_builtin_strategy_cutover` 一个 head；
3. OpenAPI 类型检查、ESLint、TypeScript 和 Vite 构建通过；
4. 构建后的 JS/CSS 与改造前业务 controller 资产一致，页面入口仍加载同一批 controller；
5. 源码中不存在旧策略运行时枚举和旧静态页面壳引用；
6. 未跟踪的用户目录 `har-api-demo/` 不进入提交。

上线必须按以下顺序执行：

1. 停止 API 与全部 worker，避免旧进程在约束切换期间继续写旧枚举；
2. `git pull --ff-only`；
3. 安装当前代码、构建 React 并同步静态产物；
4. 执行 `alembic upgrade head`；
5. 启动 API 与 worker；
6. 运行 `scripts/audit_strategy_runtime_data.py --fail-on-retired`；
7. 验证 API 健康、worker 心跳、页面路由、静态资产以及实盘总开关。

## 完成定义

只有同时满足以下条件才算“物理清理完成”：

- 旧策略类型、旧模拟盘模式和旧策略快照计数全部为 0；
- 正式内置策略数量与迁移前有效记录一一对应；
- 旧 `index.html`、旧 `app.js` 和旧审计脚本已从 Git 物理删除；
- 当前 UI 仍由相同 React 页面与业务 controller 提供；
- 服务全部 healthy，实盘仍为关闭状态；
- 生产提交、数据库 revision 与 GitHub 主分支一致。
