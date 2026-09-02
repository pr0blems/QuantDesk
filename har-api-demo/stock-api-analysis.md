# GPRO 个股接口分析

> DEMO 聚合入口已泛化为 `GET /api/stock/:symbol`。HAR 中的 GPRO 请求仅作为鉴权、请求头和参数模板；服务端会把路径与 `/stock_info/detail` 的 POST body 替换为当前自选股票代码。AAPL、NVDA、PYPL、GPRO 已验证可实时返回各自数据。非 GPRO 标的不会错误回退到 GPRO 快照。

来源：`f070acb1c8448ef12b56b0267f77af37.har`，抓包时间 2026-09-01，标的 `US.GPRO`。

## 可用于 DEMO 的接口

| 接口 | 方法 | 关键参数 | 有效数据 | DEMO 用途 |
|---|---|---|---|---|
| `/stock_info/detail` | POST | body: `{"items":[{"symbol":"GPRO"}]}` | 最新价、昨收、开高低、成交量额、振幅、量比、股本、EPS、盘前/盘后 | 个股行情详情 |
| `/stock_info/ask_bid/arca/GPRO` | GET | `props=askBidDepth` | 买卖各40档，含价格、总量和子委托量 | 40档深度盘口 |
| `/stock_info/ask_bid/arca/GPRO` | GET | `props=askBidHist` | 买卖各40档 `[price, volume]` 快照 | 深度历史快照 |
| `/stock_info/trade_tick/GPRO` | GET | `limit=100&needStat=1` | 100笔逐笔成交，方向、条件、交易时段；附主动买卖统计 | 逐笔成交 |
| `/stock_info/trade_price_list/GPRO` | GET | `page=0&size=150` | 150个成交价位的买入、卖出、中性量与占比 | 成交价分布 |
| `/stock_info/fund_related/GPRO` | GET | `withPublicityFund=1&withMainFundDeal=1&withChipsDistribution=1` | 当日流入/流出、大中小单、60条主力成交、135档筹码分布 | 今日资金与筹码 |
| `/stock_info/fund_related/GPRO` | GET | `withFundFlowTrend=1` | 390个分钟级累计资金流数据点 | 分时资金流向 |
| `/stock_info/fund_related/GPRO` | GET | `withPositionChange=1` | 最近5日大单净变化 | 5日大单 |

## 抓包样本概况

- 深度盘口：买40档、卖40档。
- 逐笔成交：单次最多返回100笔，可用 `beginIndex/endIndex` 继续分页。
- 成交价分布：抓包请求 `size=150`，响应150档。
- 筹码分布：135个价位；字段包含支撑、压力、平均成本。
- 分时资金流：390个分钟点。
- 当前抓包中的资金净流为 `-151.85万`，用于展示数据结构，不等于实时交易建议。

## 使用限制

- 所有核心个股接口都依赖 Bearer 登录态；40档盘口额外依赖 `usStockQuoteLv2Arca` 行情权限。
- 请求令牌只由本地 Vite 代理从 HAR 读取，前端不会保存或返回令牌。
- 本地代理优先请求实时上游；登录失效或网络失败时自动回退到 HAR 响应快照，并在 UI 标注 `HAR 快照` 或 `实时 + HAR`。
- `/v1/top/news/list?symbol=GPRO` 在本次 HAR 中是 `304` 且没有响应正文，因此没有作为新闻数据源。
- 抓包只出现 `CONNECT` 到行情主机，没有记录可复用的 WebSocket 帧；本次实现采用 HTTPS 接口轮询。
