# 对外 AI 新闻接口

接口只返回 `ai_analyzed_at` 已写入的新闻，不返回待分析新闻、模型原始请求、原始响应或用户资料。

## 鉴权

- HTTP：请求头 `X-API-Key: <KEY>`，也支持 `Authorization: Bearer <KEY>`。
- WebSocket：服务端程序优先使用同样的请求头；浏览器可使用查询参数 `?key=<KEY>`。
- 生产环境必须通过 HTTPS/WSS 调用，避免 KEY 明文传输。

KEY 默认由服务端 `EXTERNAL_NEWS_API_KEY` 配置。修改配置后需要重启后端。

## HTTP

```http
GET /api/public/v1/news?limit=20
X-API-Key: <KEY>
```

响应中的 `next_cursor` 用于增量拉取：

```http
GET /api/public/v1/news?limit=100&cursor=<next_cursor>
X-API-Key: <KEY>
```

不传 `cursor` 时返回最新分析，按分析时间倒序；传入后返回游标之后的新分析，按时间正序。

## WebSocket

```text
ws://127.0.0.1:8200/api/public/v1/news/ws?key=<KEY>&limit=20
```

消息类型：

- `news.analysis.snapshot`：连接成功后的最新分析快照。
- `news.analysis.completed`：之后新完成的一条 AI 新闻分析。
- `heartbeat`：无新数据时的连接心跳。

新闻对象包含原文、译文、摘要、情绪、置信度、影响强度、影响周期、类别、判断依据、关联美股和关联行业。
