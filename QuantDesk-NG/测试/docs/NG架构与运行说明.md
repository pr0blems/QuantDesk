# QuantDesk NG 架构与运行说明

## 安全边界

QuantDesk NG 0.2.0 是研究、机会发现、回测、模拟盘和受控执行基础版本。
`LIVE_EXECUTION_ENABLED=true` 会被配置校验直接拒绝；项目尚未实现真实下单适配器。

## 进程模型

- `quantdesk-ng serve`：只运行 FastAPI 和前端，不启动永久任务。
- `quantdesk-ng worker --role market`：价格、Ticker、K 线、指标和机会扫描。
- `quantdesk-ng worker --role news`：新闻与社交舆情。
- `quantdesk-ng worker --role paper`：多用户模拟盘。
- `quantdesk-ng worker-status`：查看数据库中的 Worker 租约和心跳。

每个 Worker 角色使用 `worker_leases` 获取单实例租约。重复启动同一角色时，后启动进程退出；
进程失联并超过 TTL 后，新实例可以接管。Worker 收到 SIGINT/SIGTERM 后会通知内部循环停止并释放租约。

## 执行领域基础

迁移 `0018_execution_foundation` 新增：

- `exchange_accounts`：用户交易所账户与 demo/shadow/canary/live 门禁。
- `risk_decisions`：不可变的风控批准或拒绝证据。
- `order_intents`：策略信号通过风控前后的幂等订单意图。
- `exchange_orders`：交易所订单当前快照。
- `order_events`：只追加的状态变更事件。
- `fills`：只追加的逐笔成交事实。
- `outbox_events`：业务事务与异步处理之间的可靠 Outbox。
- `worker_leases`：后台角色租约与心跳。

纯领域模块 `quantdesk_v2.execution` 使用 `Decimal` 计算下单名义价值和敞口，并拒绝非法订单状态跳转。

## Docker 开发启动

1. 复制 `.env.example` 为 `.env`，生成应用密钥并替换所有 `change-*`：

   ```powershell
   python -m quantdesk_v2.cli generate-secrets
   ```

2. 容器内数据库主机必须设置为：

   ```dotenv
   APP_HOST=0.0.0.0
   DB_HOST=mysql
   DB_SSL_REQUIRED=false
   DB_SSL_VERIFY_IDENTITY=false
   ```

3. 启动：

   ```powershell
   docker compose up --build -d
   docker compose ps
   docker compose logs -f api worker-market worker-paper
   ```

访问 `http://127.0.0.1:8200`。生产环境必须重新启用数据库 TLS，并使用反向代理提供 HTTPS。

## 本机进程启动

安装依赖并迁移后，在四个终端分别运行 API、market、news 和 paper 命令。数据库必须先升级到最新版本：

```powershell
.\.venv\Scripts\alembic.exe upgrade head
.\.venv\Scripts\quantdesk-ng.exe serve
.\.venv\Scripts\quantdesk-ng.exe worker --role market
.\.venv\Scripts\quantdesk-ng.exe worker --role news
.\.venv\Scripts\quantdesk-ng.exe worker --role paper
.\.venv\Scripts\quantdesk-ng.exe worker --role intelligence
```

## 机会智能闭环

迁移 `0019_opportunity_intelligence` 增加以下能力：

- `market` Worker 通过分片 Binance WebSocket 接收 `bookTicker`、`aggTrade` 和全市场 miniTicker；REST 价格轮询保留为断线兜底。
- `market_microstructure` 保存点差、盘口失衡、主动买入比例、60 秒成交强度、实现波动率和价格速度的最新快照。
- 机会扫描拆分为市场偏向、波动扩张、回调延续和订单流共振扫描器；当前记录与只追加历史事件分离。
- `intelligence` Worker 为所有非中性当前机会生成 30 秒至 4 小时结果标签，并持续记录成本后收益、MFE、MAE 与目标/止损命中。
- Shadow 执行适配器只消费 `shadow` 账户的已批准意图，生成完整订单、事件、成交与 Outbox 记录，绝不会向 Binance 发送真实订单。
- 合约监控页顶部显示实时数据覆盖、扫描器数量、标签进度、方向命中率和 Shadow 执行反馈。

真实下单仍由 `LIVE_EXECUTION_ENABLED` 强制锁定；Shadow 结果不能视为实盘收益承诺。

## 下一执行阶段

真实下单前仍必须完成 Binance Demo 执行适配器、用户数据 WebSocket、REST 对账、交易规则同步、
交易所侧止损、Kill Switch、Outbox Dispatcher、Shadow 验收和极小额 Canary 门禁。
