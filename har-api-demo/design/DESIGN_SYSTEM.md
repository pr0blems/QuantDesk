# HAR 接口观察台设计系统

- 视觉基准：`har-api-console-concept.png`，原生画布 1536 × 1024。
- 背景：真正的冷黑色 `#0b0f12`；表面 `#11171c`；抬升表面 `#171e24`。
- 文字：主文字 `#f3f5f6`，次文字 `#8d98a1`；路径和 JSON 使用等宽字体。
- 强调：成功与选中 `#b8f229`，POST `#ffad29`，失败 `#ff5964`。
- 容器：左侧导航 rail、中部开放表格、右侧详情 inspector；不使用卡片网格。
- 边界：1px 冷灰发丝线；控件与 inspector 使用 10–12px 圆角；几乎无阴影。
- 控件：36–40px 高，13px 字号，明确 hover/focus/selected 状态。
- 响应式：低于 1080px 时 inspector 变成底部面板；低于 720px 时导航变成横向标签、表格只保留关键列。
- 动效：180ms 的颜色、边框与位移过渡；尊重 `prefers-reduced-motion`。
