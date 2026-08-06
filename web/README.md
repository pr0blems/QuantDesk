# QuantDesk Web

QuantDesk 的 React + TypeScript 前端工程。它与 FastAPI `/api/v2` 契约通信，并以旧版终端为严格视觉与交互基线。

为保证 UI 一致性，登录页、应用外壳、工作台、设置和订单页使用与旧版相同的 DOM 语义与 CSS；监控、模拟盘、实盘、策略和回测直接挂载原版 Web Components，通过 React 会话层提供认证请求。旧版资源因此也是当前像素级兼容层，不应在未完成等价替换前删除。

## 当前范围

- 会话恢复、登录、注册、退出和用户边界校验。
- 市场监控、市场宽度、观察列表、新闻、告警、机会、标的报告和预测算法。
- 策略模板/组合创建、参数版本、验证、归档、信号、部署和 AI 草案审阅/应用。
- 回测目录、运行参数、历史记录、指标、数据质量和成交证据。
- 模拟账户创建、策略绑定、持仓、成交、权益曲线、暂停、重置和归档。
- 实盘账户创建、策略/风控快照、显式真实资金确认、Arm、暂停、归档及交易所执行证据。
- Binance 凭证、账户、订单与 AI 模型配置管理。
- 与旧版一致的终端横向导航、主题、间距、组件状态和响应式布局。
- 独立管理端仍使用后端 `/admin` 原版入口，与普通用户终端保持隔离。

资金相关操作仍由服务端权限、Preflight、风险控制、审计和执行状态机裁决；前端确认框不替代服务端安全边界。

## 本地开发

前置条件：Node.js 20.19+ 或 22.12+，以及已运行的 QuantDesk FastAPI 服务。

```powershell
cd web
Copy-Item .env.example .env.local
npm install
npm run dev
```

默认地址为 `http://127.0.0.1:5173/next/`，Vite 会把 `/api/*` 和旧版兼容组件使用的 `/assets/*` 代理到 `http://127.0.0.1:8200`。如后端使用不同地址，修改 `.env.local` 中的 `VITE_DEV_API_TARGET`。

生产镜像会构建该工程并将其作为 `/next/` 灰度入口提供；旧前端仍是默认入口。

## 质量检查

```powershell
npm run lint
npm run typecheck
npm run build
```

`npm run check` 会依次运行全部检查。TypeScript 启用了 strict、`noUncheckedIndexedAccess` 和 `exactOptionalPropertyTypes`。

`npm run generate:api` 会根据已提交的 `openapi.json` 重新生成
`src/api/schema.d.ts`。后端契约变化后，先在仓库根目录运行
`python scripts/export_openapi.py --output web/openapi.json`，再运行前端检查；CI 会拒绝过期的
OpenAPI 快照。

## 目录

```text
web/
├── src/
│   ├── api/          # API 请求、认证会话与服务端响应类型
│   ├── components/   # 可复用展示组件
│   ├── pages/        # 按业务页面拆分的迁移切片
│   ├── App.tsx       # 会话边界、导航与页面编排
│   └── styles.css    # 设计令牌和响应式样式
├── .env.example
├── eslint.config.js
├── tsconfig.*.json
└── vite.config.ts
```

## API 约定

- 浏览器请求默认同源，并携带 `credentials: include`。
- API 根路径固定为 `/api/v2`；跨源部署还需同时配置服务端 `APP_ALLOWED_ORIGINS`。刷新 Cookie 使用 `SameSite=Lax`，因此完全跨站的部署不能可靠恢复会话，生产环境推荐使用当前 `/next/` 同源入口。
- 访问令牌只保存在内存，不写入 Local Storage。
- 刷新 Cookie 由 FastAPI 设置为 HttpOnly；客户端收到 401 时只自动刷新一次。
- 已认证请求携带 `X-QuantDesk-User-ID`，用于发现同一浏览器标签页中的身份漂移。
- 页面不直接调用 `fetch`；所有请求必须经过 `src/api/client.ts` 与领域 API 模块。

## 验收与切换原则

1. 以完整业务切片迁移，而不是逐个复制 DOM。
2. 资金相关写操作必须配套服务端安全检查与契约测试。
3. 新旧页面共存期间不删除旧资源、不改变现有 URL。
4. 后端 OpenAPI 响应模型稳定后，引入自动类型生成，替换手写传输类型。
5. 当前 React 入口已完成能力迁移，须在测试数据环境完成逐流程浏览器验收后再切换根入口。

详细阶段与切换方案见 [前端迁移说明](../docs/前端迁移说明.md)。
