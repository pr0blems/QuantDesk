# QuantDesk 外部 API v1

`/api/v1` 是面向未来外部集成的稳定版本前缀。本轮只发布只读套餐能力和管理员权益控制；交易、账户凭据、模型配置均不会通过此 API 向外暴露。

所有需要身份的请求使用已有的短时 JWT：`Authorization: Bearer <access_token>`。浏览器会话刷新令牌不能作为外部 API 凭据。

| Endpoint | 用途 | 身份 |
|---|---|---|
| `GET /api/v1/plans` | 读取计划和额度说明 | 无 |
| `GET /api/v1/openapi.json` | 机器可读的 v1 OpenAPI 合约 | 无 |
| `GET /api/v1/entitlements/me` | 当前用户的实际权益 | JWT |
| `PUT /api/v1/admin/entitlements/{user_id}` | 人工设置计划和覆写 | 管理员 JWT + `X-QuantDesk-User-ID` |
| `GET /metrics` | Prometheus 指标 | 生产环境须 Bearer `METRICS_TOKEN` |

## 兼容性与弃用

- v1 字段只会新增，不会在一个主版本内改名或改变含义。
- 破坏性变化会发布到 `/api/v2`，并至少保留 v1 一个发布周期。
- `/api/v2` 是当前 Web 工作台的内部应用 API，不承诺外部兼容性。

## 支付状态

支付 Provider 是显式 feature gate：本部署没有商户凭据、签名校验或回调端点，因此 `payment_available` 始终为 `false`。不能把计划切换接口当作收款、订阅或发票系统。

## 生产依赖

- 反向代理应仅把 `/metrics` 暴露给监控网络，并用 `METRICS_TOKEN` 鉴权。
- 生产环境需要受管 TLS 数据库、`DB_SSL_CA`、`JWT_SECRET`、`CREDENTIAL_MASTER_KEY` 和密钥轮换流程。
- GitHub Actions 只做质量门禁；真正发布需在受保护环境中提供镜像仓库、部署目标、备份恢复演练和告警接收端。
