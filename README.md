# QuantDesk

面向多用户的 Binance TradFi 量化交易系统。当前主应用统一运行在
`http://127.0.0.1:8200`，不再包含旧桌面工作台和本地文件数据库存储链路。

## 启动

1. 从 `.env.example` 创建 `.env`，配置 MySQL/MariaDB、JWT 和凭据加密密钥。
2. 安装依赖：

   ```powershell
   python -m pip install -e ".[dev]"
   ```

3. 检查数据库并执行迁移：

   ```powershell
   quantdesk-v2 check-db
   alembic upgrade head
   ```

4. 双击 `start.bat`，或执行：

   ```powershell
   quantdesk-v2 serve
   ```

5. 首次启用管理后台时创建管理员：

   ```powershell
   $env:QUANTDESK_ADMIN_PASSWORD = "使用至少 12 位的独立强密码"
   quantdesk-v2 create-admin --username admin
   ```

## 数据与配置

- 用户、会话、Binance/AI 模型加密凭据、策略、回测、行情、提醒、实盘持仓快照和模拟盘数据全部存入 MySQL/MariaDB。
- `config/settings.json` 只保存无密钥的行情采集参数。
- `config/tradfi_symbols.json` 保存受支持合约的静态元数据。
- Binance API 密钥和当前用户的 AI 模型 API Key 统一在“系统设置 → API 凭证”录入；服务端加密保存且不会回显明文。
- AI 模型配置按用户隔离，可分别维护 DeepSeek、豆包、千问、Kimi、MiniMax 与 OpenAI，并选择一个启用的默认模型供策略语义编辑使用。
- 项目只使用 MySQL/MariaDB 持久化业务数据，不读取本地数据库或共享密钥文件。

## 主要页面

- `/monitor`：合约监控
- `/paper`：多账户模拟盘
- `/strategies`：用户策略与 AI 语义编辑
- `/backtest`：策略数据回测
- `/overview`：虚拟盘与 Binance 实盘绩效
- `/settings`：系统设置；二级分类“API 凭证”管理 Binance 与用户自己的 AI 模型连接
- `/admin`：独立管理应用（独立登录与 UI），细分为运行总览、采集器、提醒事件、信号规则、舆情来源、合约数据、用户权限、存储维护和审计日志（仅管理员）

## 生产要求

- 使用 HTTPS，并设置 `APP_ENV=production`、`APP_COOKIE_SECURE=true`。
- MySQL/MariaDB 必须启用 TLS、证书主机名校验和 CA 验证。
- 数据库防火墙仅允许应用服务器访问 3306。
- Binance API Key 禁止提现权限，并绑定应用出口 IP。
- `JWT_SECRET`、`CREDENTIAL_MASTER_KEY` 和数据库密码应由密钥管理系统注入；`OPENAI_API_KEY` 仅作为未配置用户模型时的可选服务端回退密钥。

详细部署和测试说明见 [V2 运行说明](docs/V2运行说明.md)。
