# QuantDesk Web

QuantDesk 的唯一前端工程。正式页面、页面控制器、主题样式和独立管理后台源码均由本目录维护，`src/quantdesk_v2` 只负责后端与构建后的 `react_static` 制品。

## UI 冻结约束

- 当前线上页面是视觉与交互基线，不在架构迁移中重新设计。
- 页面控制器保留原 DOM 模板、CSS 选择器、文案和事件逻辑。
- 复杂页面由 React 的 `PageControllerPanel` 管理生命周期，但控制器和样式随 Vite 一起构建，不再从后端 `/assets/*.js` 动态加载。
- `har-api-demo` 是独立接口演示程序，不属于本工程，也不参与 QuantDesk 前端清理。

## 目录

```text
web/
├── admin-source/       # 独立管理后台源码，由 Vite 输出到 /next/admin/
├── public/assets/      # Shadow DOM 页面直接加载的原样式资源
├── src/
│   ├── api/            # 认证、API、SSE 和 WebSocket 客户端
│   ├── controllers/    # 六个现行业务页面控制器，进入 Vite JS 制品
│   ├── pages/          # React 页面与控制器宿主
│   ├── theme/          # 全局终端、策略与主题样式
│   ├── App.tsx         # 会话、导航和路由
│   └── main.tsx        # 唯一浏览器入口
└── vite.config.ts
```

## 本地开发

```powershell
cd web
Copy-Item .env.example .env.local
npm install
npm run dev
```

默认地址为 `http://127.0.0.1:5173/next/`。`/api/*` 代理到 `VITE_DEV_API_TARGET`，页面样式由 Vite 的 `/next/assets/*` 提供。

## 质量检查

```powershell
npm run check:api
npm run lint
npm run typecheck
npm run build
```

生产构建输出到 `web/dist`。部署时将其同步到 `src/quantdesk_v2/react_static`，FastAPI 的正式页面路径与 `/next/*` 都只使用这一份不可变制品。
