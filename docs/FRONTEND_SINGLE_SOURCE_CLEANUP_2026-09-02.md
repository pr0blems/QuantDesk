# QuantDesk 前端单一源码清理记录

> 日期：2026-09-02
> 约束：保持现有 UI、DOM、文案、交互和业务功能不变
> 排除范围：`har-api-demo` 与 `http://127.0.0.1:4178/` 完整保留

## 结论

QuantDesk 主站前端现在只有一个源码归属：`web/`。

原先位于 `src/quantdesk_v2/static` 的页面控制器、样式和管理后台源码已经迁入 `web`，后端不再保存或发布第二套前端源码，也不再提供旧 `/assets/*.js` 控制器入口。

这次是源码归属与构建链路迁移，不是 UI 重构。六个现行业务页面继续使用已经在线上验收的 DOM、CSS 和事件逻辑。

## 新结构

```text
FastAPI /api/v2/*
       │
       └── React build: src/quantdesk_v2/react_static
                ├── /next/assets/index-*.js     React + 全部页面控制器
                ├── /next/assets/index-*.css    全局主题样式
                ├── /next/assets/*.css          Shadow DOM 页面样式
                └── /next/admin/*               独立管理后台制品
```

源码对应关系：

- `web/src/controllers`：合约监控、发现机会、模拟盘、实盘、策略中心、回测控制器；
- `web/src/theme`：全局终端、策略和主题样式；
- `web/public/assets`：Shadow DOM 页面按原 URL 语义加载的样式；
- `web/admin-source`：独立管理后台源码；
- `src/quantdesk_v2`：只保留 Python 后端和部署后的前端制品目录。

## 已移除

- `src/quantdesk_v2/static` 前端源码目录；
- `Settings.static_dir`；
- FastAPI `/assets` 静态目录挂载；
- `web/index.html` 中独立加载的 `controller-runtime.js` 和 `strategies.js`；
- `main.tsx` 中动态插入五个页面脚本的逻辑；
- Python 包配置中的 `static/*` 资源打包规则。

## UI 不变证据

1. 七个控制器文件与迁移前逐字对照；除 Shadow DOM 样式地址从 `/assets` 统一为 `/next/assets` 外，模板和业务逻辑完全一致。
2. CSS 文件内容原样移动，没有修改选择器、尺寸、颜色、间距或响应式断点。
3. 本地浏览器验证六个控制器全部挂载成功：合约监控、发现机会、模拟盘、实盘、策略中心、数据回测。
4. 浏览器控制台无错误和警告。
5. 静态前端契约测试、认证路由测试、TypeScript、ESLint 和生产构建通过。

## 后续维护规则

- 所有前端需求只修改 `web/`，禁止在 Python 包中重新建立页面源码目录。
- 页面控制器若逐步改写为 React，必须逐页完成截图、DOM、交互和 API 对照后再替换；不得以“清理”为理由改变现有 UI。
- `har-api-demo` 保持独立，不得被主站构建、清理或部署脚本覆盖。
