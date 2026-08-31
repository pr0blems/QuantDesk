# QuantDesk 一次性架构清理完成报告

## 范围

本报告覆盖安全标签 `pre-full-cleanup-20260831-e14edb0` 之后的一次性清理批次。目标是收缩 AI Monitor、API 和未使用前端代码，同时保持现有 UI、API 和交易语义不变。

## 提交序列

1. `d51bc56`：隔离机会候选写入。
2. `07b70b9`：隔离机会预测写入。
3. `29906ff`：新闻评分权威迁入应用层。
4. `5868daa`：隔离事件可见性边界。
5. `6ad9623`：隔离预测结算权威。
6. `9b3c86c`：查询模型迁入 persistence 层。
7. `d0a1211`：拆分模拟盘与实盘账户 API。
8. `46df38a`：拆分 AI Monitor 运行 API。
9. `4f41f4b`：增加只读 legacy 策略审计。
10. `01aa386`：删除未使用 React 管理页试验代码并同步 OpenAPI 制品。

## 行为保持措施

- 每个 API 路由拆分前后导出 OpenAPI 并做精确 JSON 对比，路由拆分本身没有产生契约差异。
- tracked OpenAPI 的 3 项更新来自拆分前已经存在的运行时契约：默认决策版本、部署模式去除 backtest、monitor overview 支持 symbol 查询。
- 前端生产 JS/CSS/HTML 哈希清理前后一致。
- AI Monitor 的历史 settlement version 和不可变预测事实不被重写。
- legacy 数据仅盘点，不自动迁移或删除。
- API 优雅关闭上限设为 20 秒，确保 SSE/WebSocket 长连接先由应用收敛，并在 systemd 的 30 秒停止上限前退出。

## 回退

- 代码回退锚点：Git 标签 `pre-full-cleanup-20260831-e14edb0`。
- 本轮无数据库 schema 变更，因此代码回退不需要数据库降级。
- 用户已明确免除数据库备份；部署不创建新备份。

## 最终验收

本地全量后端测试、静态检查、OpenAPI、前端 lint/typecheck/build 均通过。生产结果在部署后补充到部署记录和任务交付说明中。
