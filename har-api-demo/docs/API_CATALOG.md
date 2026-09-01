# 股票资讯接口文档

> 本文档只保留股票行情、证券新闻、社区资讯和 IPO 数据接口。认证值、Cookie 值、账户标识、用户标识和设备标识均已脱敏；快照参数只调用对应的只读查询接口，其中主题查询使用 POST。

## 总览

| 指标 | 数量 |
|---|---:|
| HAR 总记录 | 160 |
| 原始业务/API 调用 | 143 |
| 保留的股票资讯调用 | 69 |
| 原始去重接口 | 69 |
| 保留接口（方法 + 域名 + 路径） | 26 |
| 在抓包中业务成功 | 26 |
| GET / POST | 20 / 6 |
| 域名 | 5 |

## 使用边界

- `GET` 仅表示语义上只读，仍然需要合法账户、有效凭据并遵守服务条款。
- 保留的 `POST` 主要是行情详情、排行和主题内容查询，但仍需在人工确认后使用。
- HAR 中观察到 Bearer Token 与 Cookie；本文档不保存它们的值。
- HTTP 200 不一定代表业务成功；本文档同时检查了 `ret/code/status/success/msg`。

## 按用途统计

| 用途类别 | 接口数 |
|---|---:|
| 行情数据 | 14 |
| 社区资讯 | 8 |
| 新闻资讯 | 3 |
| IPO 数据 | 1 |

## 接口索引

| 方法 | 域名与路径 | 用途 | 状态 | 风险 |
|---|---|---|---|---|
| GET | `community-service.laohu8.com/v1/feed/stock_latest` | 股票最新讨论 | 可用 | 只读 |
| GET | `community-service.laohu8.com/v1/feed/stock_recommend` | 股票推荐讨论 | 可用 | 只读 |
| GET | `community-service.laohu8.com/v1/feed/symbol/transaction-orders` | 社区晒单 | 可用 | 只读 |
| GET | `community-service.laohu8.com/v1/gpt/stock-daily` | 股票每日摘要 | 可用 | 只读 |
| GET | `community-service.laohu8.com/v1/order-sharing/candlestick` | 晒单K线 | 可用 | 只读 |
| GET | `community-service.laohu8.com/v4/symbol/trend/attitude/statistic` | 市场态度统计 | 可用 | 只读 |
| POST | `community-service.laohu8.com/v4/tweet/theme/symbol/page` | 主题内容 | 可用 | 数据查询（POST） |
| POST | `community-service.laohu8.com/v4/tweet/theme/symbol/relate/themes` | 主题内容 | 可用 | 数据查询（POST） |
| GET | `hq-depth.skytigris.cn/stock_info/ask_bid/blue-ocean/PYPL` | 买卖盘深度 | 可用 | 只读 |
| GET | `hq-depth.skytigris.cn/stock_info/ask_bid_all/PYPL` | 买卖盘深度 | 可用 | 只读 |
| POST | `hq2.skytigris.cn/api/global/related_bar` | 关联行情栏 | 可用 | 数据查询（POST） |
| POST | `hq2.skytigris.cn/api/stock/us/rank/hourTrading` | 盘前盘后排行 | 可用 | 数据查询（POST） |
| GET | `hq2.skytigris.cn/fundamental/corporate_actions/US/.IXIC` | 公司行动 | 可用 | 只读 |
| GET | `hq2.skytigris.cn/market/index/package_indices_IXIC` | 指数成分包 | 可用 | 只读 |
| GET | `hq2.skytigris.cn/market/relate/contract/.IXIC` | 关联合约 | 可用 | 只读 |
| POST | `hq2.skytigris.cn/stock_info/detail` | 股票详情行情 | 可用 | 数据查询（POST） |
| POST | `hq2.skytigris.cn/stock_info/detail/all` | 股票详情行情 | 可用 | 数据查询（POST） |
| GET | `hq2.skytigris.cn/stock_info/time_trend/hour_trading_detail/PYPL` | 分时走势 | 可用 | 只读 |
| GET | `hq2.skytigris.cn/stock_info/trade_price_list/PYPL` | 成交价分布 | 可用 | 只读 |
| GET | `hq2.skytigris.cn/stock_info/trade_tick/PYPL` | 逐笔成交 | 可用 | 只读 |
| GET | `hq2.skytigris.cn/v2/market` | 市场总览配置 | 可用 | 只读 |
| GET | `hq2.skytigris.cn/value_analysis/index/.IXIC` | 估值分析 | 可用 | 只读 |
| GET | `stock-news.laohu8.com/v1/news/suture/list` | 证券资讯 | 可用 | 只读 |
| GET | `stock-news.laohu8.com/v1/top/news/list` | 证券资讯 | 可用 | 只读 |
| GET | `stock-news.laohu8.com/v2/news/list` | 证券资讯 | 可用 | 只读 |
| GET | `trade.skytigris.cn/ipos` | IPO 列表 | 可用 | 只读 |

## 接口详情

### 1. `GET community-service.laohu8.com/v1/feed/stock_latest`

- **用途：** 社区资讯 / 股票最新讨论
- **说明：** 分页获取指定股票的最新社区内容。
- **抓包表现：** 调用 2 次，平均 206.0 ms，业务成功
- **认证：** Bearer Token；Cookie 名称：JSESSIONID, acw_tc, ngxid
- **重放边界：** 可生成只读模板，仍需合法凭据

**业务查询参数**

| 参数 | 类型 | 抓包中每次出现 | 说明 |
|---|---|---|---|
| `feedTweetType` | string | 是 | 抓包中观察到的参数 |
| `pageCount` | integer | 是 | 页码/批次 |
| `pageSize` | integer | 是 | 每页数量 |
| `startFromId` | integer | 是 | 抓包中观察到的参数 |
| `startFromTime` | integer | 是 | 抓包中观察到的参数 |
| `symbol` | string | 是 | 证券代码 |

**公共客户端上下文：** `__v_account__, appName, appVer, channel, community_region, device, device_model, deviceId, edition, grayTest, keyfrom, lang, langContent, langShow, license, location, openFlag, os, osVer, platform, region, screenH, screenW, skin, uuid, vendor`

**响应结构：** `code`、`message`、`description`、`status`、`success`、`data`

**业务状态样例（脱敏）：** `{"code": "62000000", "status": "200", "success": true, "message": "Success"}`

### 2. `GET community-service.laohu8.com/v1/feed/stock_recommend`

- **用途：** 社区资讯 / 股票推荐讨论
- **说明：** 获取指定股票的推荐社区内容流。
- **抓包表现：** 调用 1 次，平均 243.0 ms，业务成功
- **认证：** Bearer Token；Cookie 名称：JSESSIONID, acw_tc, ngxid
- **重放边界：** 可生成只读模板，仍需合法凭据

**业务查询参数**

| 参数 | 类型 | 抓包中每次出现 | 说明 |
|---|---|---|---|
| `feedTweetType` | string | 是 | 抓包中观察到的参数 |
| `pageCount` | integer | 是 | 页码/批次 |
| `pageSize` | integer | 是 | 每页数量 |
| `startFromId` | integer | 是 | 抓包中观察到的参数 |
| `startFromTime` | integer | 是 | 抓包中观察到的参数 |
| `symbol` | string | 是 | 证券代码 |

**公共客户端上下文：** `__v_account__, appName, appVer, channel, community_region, device, device_model, deviceId, edition, grayTest, keyfrom, lang, langContent, langShow, license, location, openFlag, os, osVer, platform, region, screenH, screenW, skin, uuid, vendor`

**响应结构：** `code`、`message`、`description`、`status`、`success`、`data`

**业务状态样例（脱敏）：** `{"code": "62000000", "status": "200", "success": true, "message": "Success"}`

### 3. `GET community-service.laohu8.com/v1/feed/symbol/transaction-orders`

- **用途：** 社区资讯 / 社区晒单
- **说明：** 获取与证券相关的公开交易分享内容。
- **抓包表现：** 调用 1 次，平均 66.0 ms，业务成功
- **认证：** Bearer Token；Cookie 名称：JSESSIONID, acw_tc, ngxid
- **重放边界：** 可生成只读模板，仍需合法凭据

**业务查询参数**

| 参数 | 类型 | 抓包中每次出现 | 说明 |
|---|---|---|---|
| `symbol` | string | 是 | 证券代码 |

**公共客户端上下文：** `__v_account__, appName, appVer, channel, community_region, device, device_model, deviceId, edition, grayTest, keyfrom, lang, lang_content, langShow, license, location, openFlag, os, osVer, platform, region, screenH, screenW, skin, uuid, vendor`

**响应结构：** `code`、`message`、`description`、`status`、`success`、`data`

**业务状态样例（脱敏）：** `{"code": "62000000", "status": "200", "success": true, "message": "Success"}`

### 4. `GET community-service.laohu8.com/v1/gpt/stock-daily`

- **用途：** 社区资讯 / 股票每日摘要
- **说明：** 获取面向社区展示的股票每日智能摘要。
- **抓包表现：** 调用 1 次，平均 60.0 ms，业务成功
- **认证：** Bearer Token；Cookie 名称：JSESSIONID, acw_tc, ngxid
- **重放边界：** 可生成只读模板，仍需合法凭据

**业务查询参数**

| 参数 | 类型 | 抓包中每次出现 | 说明 |
|---|---|---|---|
| `symbol` | string | 是 | 证券代码 |

**公共客户端上下文：** `__v_account__, appName, appVer, channel, community_region, device, device_model, deviceId, edition, grayTest, keyfrom, lang, langContent, langShow, license, location, openFlag, os, osVer, platform, region, screenH, screenW, skin, uuid, vendor`

**响应结构：** `code`、`message`、`description`、`status`、`success`、`data`

**业务状态样例（脱敏）：** `{"code": "62000000", "status": "200", "success": true, "message": "Success"}`

### 5. `GET community-service.laohu8.com/v1/order-sharing/candlestick`

- **用途：** 社区资讯 / 晒单K线
- **说明：** 获取订单分享场景需要的简化 K 线数据。
- **抓包表现：** 调用 1 次，平均 73.0 ms，业务成功
- **认证：** Bearer Token；Cookie 名称：JSESSIONID, acw_tc, ngxid
- **重放边界：** 可生成只读模板，仍需合法凭据

**业务查询参数**

| 参数 | 类型 | 抓包中每次出现 | 说明 |
|---|---|---|---|
| `period` | string | 是 | 周期 |
| `symbol` | string | 是 | 证券代码 |

**公共客户端上下文：** `__v_account__, appName, appVer, channel, community_region, device, device_model, deviceId, edition, grayTest, keyfrom, lang, license, location, openFlag, os, osVer, platform, region, screenH, screenW, skin, uuid, vendor`

**响应结构：** `code`、`message`、`description`、`status`、`success`、`data`

**业务状态样例（脱敏）：** `{"code": "62000000", "status": "200", "success": true, "message": "Success"}`

### 6. `GET community-service.laohu8.com/v4/symbol/trend/attitude/statistic`

- **用途：** 社区资讯 / 市场态度统计
- **说明：** 统计社区用户对标的走势的观点。
- **抓包表现：** 调用 1 次，平均 84.0 ms，业务成功
- **认证：** Bearer Token；Cookie 名称：JSESSIONID, acw_tc, ngxid
- **重放边界：** 可生成只读模板，仍需合法凭据

**业务查询参数**

| 参数 | 类型 | 抓包中每次出现 | 说明 |
|---|---|---|---|
| `symbol` | string | 是 | 证券代码 |

**公共客户端上下文：** `__v_account__, appName, appVer, channel, community_region, device, device_model, deviceId, edition, grayTest, keyfrom, lang, license, location, openFlag, os, osVer, platform, region, screenH, screenW, skin, uuid, vendor`

**响应结构：** `code`、`message`、`description`、`status`、`success`、`data`

**业务状态样例（脱敏）：** `{"code": "62000000", "status": "200", "success": true, "message": "成功"}`

### 7. `POST community-service.laohu8.com/v4/tweet/theme/symbol/page`

- **用途：** 社区资讯 / 主题内容
- **说明：** 获取或关联股票主题与社区帖子。
- **抓包表现：** 调用 1 次，平均 139.0 ms，业务成功
- **认证：** Bearer Token；Cookie 名称：JSESSIONID, acw_tc, ngxid
- **重放边界：** 可生成数据查询模板，仍需合法凭据并人工确认

**业务查询参数：** 未观察到独立业务参数。

**公共客户端上下文：** `__v_account__, appName, appVer, channel, community_region, device, device_model, deviceId, edition, grayTest, keyfrom, lang, license, location, openFlag, os, osVer, platform, region, screenH, screenW, skin, uuid, vendor`

**请求体：** `application/json`；结构：`pageSize`、`symbol`

**响应结构：** `code`、`message`、`description`、`status`、`success`、`data`

**业务状态样例（脱敏）：** `{"code": "62000000", "status": "200", "success": true, "message": "成功"}`

### 8. `POST community-service.laohu8.com/v4/tweet/theme/symbol/relate/themes`

- **用途：** 社区资讯 / 主题内容
- **说明：** 获取或关联股票主题与社区帖子。
- **抓包表现：** 调用 1 次，平均 131.0 ms，业务成功
- **认证：** Bearer Token；Cookie 名称：JSESSIONID, acw_tc, ngxid
- **重放边界：** 可生成数据查询模板，仍需合法凭据并人工确认

**业务查询参数：** 未观察到独立业务参数。

**公共客户端上下文：** `__v_account__, appName, appVer, channel, community_region, device, device_model, deviceId, edition, grayTest, keyfrom, lang, license, location, openFlag, os, osVer, platform, region, screenH, screenW, skin, uuid, vendor`

**请求体：** `application/json`；结构：`pageCount`、`pageSize`、`symbol`

**响应结构：** `code`、`message`、`description`、`status`、`success`、`data`

**业务状态样例（脱敏）：** `{"code": "62000000", "status": "200", "success": true, "message": "成功"}`

### 9. `GET hq-depth.skytigris.cn/stock_info/ask_bid/blue-ocean/PYPL`

- **用途：** 行情数据 / 买卖盘深度
- **说明：** 获取证券买一卖一或完整盘口深度。
- **抓包表现：** 调用 2 次，平均 95.0 ms，业务成功
- **认证：** Bearer Token；Cookie 名称：JSESSIONID, acw_tc, ngxid
- **重放边界：** 可生成只读模板，仍需合法凭据

**业务查询参数**

| 参数 | 类型 | 抓包中每次出现 | 说明 |
|---|---|---|---|
| `isMergePrice` | integer | 是 | 抓包中观察到的参数 |
| `props` | string | 是 | 行情字段集合 |

**公共客户端上下文：** `__v_account__, appName, appVer, channel, device, device_model, deviceId, edition, keyfrom, lang, license, location, os, osVer, platform, region, screenH, screenW, skin, uuid, vendor`

**响应结构：** `ret`、`serverTime`、`askBidDepth`、`timestamp`

**业务状态样例（脱敏）：** `{"ret": 0}`

### 10. `GET hq-depth.skytigris.cn/stock_info/ask_bid_all/PYPL`

- **用途：** 行情数据 / 买卖盘深度
- **说明：** 获取证券买一卖一或完整盘口深度。
- **抓包表现：** 调用 2 次，平均 86.0 ms，业务成功
- **认证：** Bearer Token；Cookie 名称：JSESSIONID, acw_tc, ngxid
- **重放边界：** 可生成只读模板，仍需合法凭据

**业务查询参数**

| 参数 | 类型 | 抓包中每次出现 | 说明 |
|---|---|---|---|
| `isMergePrice` | integer | 是 | 抓包中观察到的参数 |
| `props` | string | 是 | 行情字段集合 |

**公共客户端上下文：** `__v_account__, appName, appVer, channel, device, device_model, deviceId, edition, keyfrom, lang, license, location, os, osVer, platform, region, screenH, screenW, skin, uuid, vendor`

**响应结构：** `ret`、`serverTime`、`arca`

**业务状态样例（脱敏）：** `{"ret": 0}`

### 11. `POST hq2.skytigris.cn/api/global/related_bar`

- **用途：** 行情数据 / 关联行情栏
- **说明：** 获取标的关联的行情或导航数据。
- **抓包表现：** 调用 1 次，平均 191.0 ms，业务成功
- **认证：** Bearer Token；Cookie 名称：JSESSIONID, acw_tc, ngxid, org.springframework.web.servlet.i18n.CookieLocaleResolver.LOCALE
- **重放边界：** 可生成数据查询模板，仍需合法凭据并人工确认

**业务查询参数：** 未观察到独立业务参数。

**公共客户端上下文：** `__v_account__, appName, appVer, channel, device, device_model, deviceId, edition, keyfrom, lang, license, location, os, osVer, platform, region, screenH, screenW, skin, uuid, vendor`

**请求体：** `application/json`；结构：`symbol`

**响应结构：** `ret`、`serverTime`、`data`

**业务状态样例（脱敏）：** `{"ret": 0}`

### 12. `POST hq2.skytigris.cn/api/stock/us/rank/hourTrading`

- **用途：** 行情数据 / 盘前盘后排行
- **说明：** 获取延长交易时段的股票排行。
- **抓包表现：** 调用 2 次，平均 260.0 ms，业务成功
- **认证：** Bearer Token；Cookie 名称：JSESSIONID, acw_tc, ngxid, org.springframework.web.servlet.i18n.CookieLocaleResolver.LOCALE
- **重放边界：** 可生成数据查询模板，仍需合法凭据并人工确认

**业务查询参数：** 未观察到独立业务参数。

**公共客户端上下文：** `__v_account__, appName, appVer, channel, device, device_model, deviceId, edition, keyfrom, lang, license, location, os, osVer, platform, region, screenH, screenW, skin, uuid, vendor`

**请求体：** `application/json`；结构：`page`、`order`、`topicId`、`compare`、`withTopics`、`filters`

**响应结构：** `ret`、`serverTime`、`data`

**业务状态样例（脱敏）：** `{"ret": 0}`

### 13. `GET hq2.skytigris.cn/fundamental/corporate_actions/US/.IXIC`

- **用途：** 行情数据 / 公司行动
- **说明：** 获取分红、拆股等公司行动数据。
- **抓包表现：** 调用 1 次，平均 65.0 ms，业务成功
- **认证：** Bearer Token；Cookie 名称：JSESSIONID, acw_tc, ngxid, org.springframework.web.servlet.i18n.CookieLocaleResolver.LOCALE
- **重放边界：** 可生成只读模板，仍需合法凭据

**业务查询参数：** 未观察到独立业务参数。

**公共客户端上下文：** `__v_account__, appName, appVer, channel, device, device_model, deviceId, edition, keyfrom, lang, license, location, os, osVer, platform, region, screenH, screenW, skin, uuid, vendor`

**响应结构：** `ret`、`serverTime`、`msg`、`data`

**业务状态样例（脱敏）：** `{"ret": 0, "msg": "success"}`

### 14. `GET hq2.skytigris.cn/market/index/package_indices_IXIC`

- **用途：** 行情数据 / 指数成分包
- **说明：** 获取指数相关的成分或组合标的。
- **抓包表现：** 调用 7 次，平均 76.3 ms，业务成功
- **认证：** Bearer Token；Cookie 名称：JSESSIONID, acw_tc, ngxid, org.springframework.web.servlet.i18n.CookieLocaleResolver.LOCALE
- **重放边界：** 可生成只读模板，仍需合法凭据

**业务查询参数**

| 参数 | 类型 | 抓包中每次出现 | 说明 |
|---|---|---|---|
| `page` | integer | 是 | 页码 |
| `size` | integer | 是 | 返回数量 |

**公共客户端上下文：** `__v_account__, appName, appVer, channel, device, device_model, deviceId, edition, keyfrom, lang, license, location, os, osVer, platform, region, screenH, screenW, skin, uuid, vendor`

**响应结构：** `ret`、`delay`、`topics`、`serverTime`

**业务状态样例（脱敏）：** `{"ret": 0}`

### 15. `GET hq2.skytigris.cn/market/relate/contract/.IXIC`

- **用途：** 行情数据 / 关联合约
- **说明：** 获取指数或证券关联的可交易合约。
- **抓包表现：** 调用 7 次，平均 72.6 ms，业务成功
- **认证：** Bearer Token；Cookie 名称：JSESSIONID, acw_tc, ngxid, org.springframework.web.servlet.i18n.CookieLocaleResolver.LOCALE
- **重放边界：** 可生成只读模板，仍需合法凭据

**业务查询参数：** 未观察到独立业务参数。

**公共客户端上下文：** `__v_account__, appName, appVer, channel, device, device_model, deviceId, edition, keyfrom, lang, license, location, os, osVer, platform, region, screenH, screenW, skin, uuid, vendor`

**响应结构：** `ret`、`serverTime`、`index`、`fut`、`etf`

**业务状态样例（脱敏）：** `{"ret": 0}`

### 16. `POST hq2.skytigris.cn/stock_info/detail`

- **用途：** 行情数据 / 股票详情行情
- **说明：** 获取股票最新价、涨跌、交易状态及扩展行情字段。
- **抓包表现：** 调用 9 次，平均 166.9 ms，业务成功
- **认证：** Bearer Token；Cookie 名称：JSESSIONID, acw_tc, ngxid, org.springframework.web.servlet.i18n.CookieLocaleResolver.LOCALE
- **重放边界：** 可生成数据查询模板，仍需合法凭据并人工确认

**业务查询参数**

| 参数 | 类型 | 抓包中每次出现 | 说明 |
|---|---|---|---|
| `withOvernight` | integer | 是 | 抓包中观察到的参数 |

**公共客户端上下文：** `__v_account__, appName, appVer, channel, device, device_model, deviceId, edition, keyfrom, lang, license, location, os, osVer, platform, region, screenH, screenW, skin, uuid, vendor`

**请求体：** `application/json`；结构：`items`

**响应结构：** `ret`、`serverTime`、`items`

**业务状态样例（脱敏）：** `{"ret": 0}`

### 17. `POST hq2.skytigris.cn/stock_info/detail/all`

- **用途：** 行情数据 / 股票详情行情
- **说明：** 获取股票最新价、涨跌、交易状态及扩展行情字段。
- **抓包表现：** 调用 3 次，平均 125.7 ms，业务成功
- **认证：** Bearer Token；Cookie 名称：JSESSIONID, acw_tc, ngxid, org.springframework.web.servlet.i18n.CookieLocaleResolver.LOCALE
- **重放边界：** 可生成数据查询模板，仍需合法凭据并人工确认

**业务查询参数**

| 参数 | 类型 | 抓包中每次出现 | 说明 |
|---|---|---|---|
| `lite` | integer | 是 | 抓包中观察到的参数 |
| `refreshBondForex` | integer | 是 | 抓包中观察到的参数 |
| `refreshCfd` | integer | 是 | 抓包中观察到的参数 |
| `refreshCryptoCurrency` | integer | 是 | 抓包中观察到的参数 |
| `refreshOptFut` | integer | 是 | 抓包中观察到的参数 |
| `supportBond` | integer | 是 | 抓包中观察到的参数 |
| `withOvernight` | integer | 是 | 抓包中观察到的参数 |

**公共客户端上下文：** `__v_account__, appName, appVer, channel, device, device_model, deviceId, edition, keyfrom, lang, license, location, os, osVer, platform, region, screenH, screenW, skin, uuid, vendor`

**请求体：** `application/json`；结构：`items`、`delay`

**响应结构：** `ret`、`serverTime`、`items`

**业务状态样例（脱敏）：** `{"ret": 0}`

### 18. `GET hq2.skytigris.cn/stock_info/time_trend/hour_trading_detail/PYPL`

- **用途：** 行情数据 / 分时走势
- **说明：** 获取盘中、盘前或盘后分时走势明细。
- **抓包表现：** 调用 2 次，平均 85.5 ms，业务成功
- **数据边界：** 抓包仅观察到 `period=day`，且两个响应的 `items` 均只有 1 条增量记录；未观察到 OHLCV 字段、其他周期枚举、分页参数或服务端条数上限。
- **认证：** Bearer Token；Cookie 名称：JSESSIONID, acw_tc, ngxid, org.springframework.web.servlet.i18n.CookieLocaleResolver.LOCALE
- **重放边界：** 可生成只读模板，仍需合法凭据

**业务查询参数**

| 参数 | 类型 | 抓包中每次出现 | 说明 |
|---|---|---|---|
| `beginTime` | integer | 是 | 开始时间 |
| `manualRefresh` | integer | 是 | 抓包中观察到的参数 |
| `period` | string | 是 | 周期 |
| `symbol` | string | 是 | 证券代码 |
| `tradingStatus` | integer | 是 | 抓包中观察到的参数 |
| `withOvernight` | integer | 是 | 抓包中观察到的参数 |

**公共客户端上下文：** `__v_account__, appName, appVer, channel, device, device_model, deviceId, edition, keyfrom, lang, license, location, os, osVer, platform, region, screenH, screenW, skin, uuid, vendor`

**响应结构：** `ret`、`serverTime`、`symbol`、`period`、`totalVolume`、`preClose`、`preMarket`、`afterHours`、`tradingStatus`、`detail`、`openAndCloseTimeList`、`items`

**业务状态样例（脱敏）：** `{"ret": 0}`

### 19. `GET hq2.skytigris.cn/stock_info/trade_price_list/PYPL`

- **用途：** 行情数据 / 成交价分布
- **说明：** 获取证券成交价档位及成交量分布。
- **抓包表现：** 调用 2 次，平均 80.5 ms，业务成功
- **认证：** Bearer Token；Cookie 名称：JSESSIONID, acw_tc, ngxid, org.springframework.web.servlet.i18n.CookieLocaleResolver.LOCALE
- **重放边界：** 可生成只读模板，仍需合法凭据

**业务查询参数**

| 参数 | 类型 | 抓包中每次出现 | 说明 |
|---|---|---|---|
| `manualRefresh` | integer | 是 | 抓包中观察到的参数 |
| `page` | integer | 是 | 页码 |
| `size` | integer | 是 | 返回数量 |

**公共客户端上下文：** `__v_account__, appName, appVer, channel, device, device_model, deviceId, edition, keyfrom, lang, license, location, os, osVer, platform, region, screenH, screenW, skin, uuid, vendor`

**响应结构：** `ret`、`serverTime`、`data`、`page`、`totalPage`

**业务状态样例（脱敏）：** `{"ret": 0}`

### 20. `GET hq2.skytigris.cn/stock_info/trade_tick/PYPL`

- **用途：** 行情数据 / 逐笔成交
- **说明：** 获取指定证券的逐笔成交记录与序号范围。
- **抓包表现：** 调用 4 次，平均 105.0 ms，业务成功
- **认证：** Bearer Token；Cookie 名称：JSESSIONID, acw_tc, ngxid, org.springframework.web.servlet.i18n.CookieLocaleResolver.LOCALE
- **重放边界：** 可生成只读模板，仍需合法凭据

**业务查询参数**

| 参数 | 类型 | 抓包中每次出现 | 说明 |
|---|---|---|---|
| `beginIndex` | string | 是 | 起始序号 |
| `endIndex` | string | 是 | 结束序号 |
| `limit` | integer | 是 | 数量上限 |
| `manualRefresh` | integer | 是 | 抓包中观察到的参数 |
| `needStat` | integer | 是 | 抓包中观察到的参数 |
| `tradingStatus` | integer | 是 | 抓包中观察到的参数 |

**公共客户端上下文：** `__v_account__, appName, appVer, channel, device, device_model, deviceId, edition, keyfrom, lang, license, location, os, osVer, platform, region, screenH, screenW, skin, uuid, vendor`

**响应结构：** `ret`、`serverTime`、`skipRecent`、`endIndex`、`beginIndex`、`items`、`stats`

**业务状态样例（脱敏）：** `{"ret": 0}`

### 21. `GET hq2.skytigris.cn/v2/market`

- **用途：** 行情数据 / 市场总览配置
- **说明：** 获取市场板块、指数、ETF 与排行入口数据。
- **抓包表现：** 调用 3 次，平均 92.3 ms，业务成功
- **认证：** Bearer Token；Cookie 名称：JSESSIONID, acw_tc, ngxid, org.springframework.web.servlet.i18n.CookieLocaleResolver.LOCALE
- **重放边界：** 可生成只读模板，仍需合法凭据

**业务查询参数**

| 参数 | 类型 | 抓包中每次出现 | 说明 |
|---|---|---|---|
| `boardLimit` | integer | 是 | 抓包中观察到的参数 |
| `conceptLimit` | integer | 是 | 抓包中观察到的参数 |
| `configIndices` | integer | 是 | 抓包中观察到的参数 |
| `etfTopicLimit` | integer | 是 | 抓包中观察到的参数 |
| `filterLeverage` | integer | 否 | 抓包中观察到的参数 |
| `limit` | integer | 是 | 数量上限 |
| `optionLimit` | integer | 是 | 抓包中观察到的参数 |
| `optionRankingId` | string | 是 | 抓包中观察到的参数 |
| `optionRankingVer` | string | 是 | 抓包中观察到的参数 |
| `withConcept` | integer | 是 | 抓包中观察到的参数 |
| `withEtf` | integer | 是 | 抓包中观察到的参数 |
| `withEtfTopic` | integer | 是 | 抓包中观察到的参数 |
| `withFearGreedIndex` | integer | 是 | 抓包中观察到的参数 |
| `withHeatMap` | integer | 是 | 抓包中观察到的参数 |
| `withImportIndices` | integer | 是 | 抓包中观察到的参数 |
| `withShortSaleTopic` | integer | 是 | 抓包中观察到的参数 |
| `withThumb` | integer | 是 | 抓包中观察到的参数 |
| `withTigerIndices` | integer | 是 | 抓包中观察到的参数 |
| `withToday` | integer | 是 | 抓包中观察到的参数 |
| `withTopOptionBulkOrder` | integer | 是 | 抓包中观察到的参数 |
| `withUpDownSummary` | integer | 是 | 抓包中观察到的参数 |

**公共客户端上下文：** `__v_account__, appName, appVer, channel, device, device_model, deviceId, edition, keyfrom, lang, license, location, os, osVer, platform, region, screenH, screenW, skin, uuid, vendor`

**响应结构：** `ret`、`serverTime`、`market`、`delay`、`boards`、`concepts`、`indices`、`thumb`、`upDownSummary`、`topics`、`topic`、`rankings`、`ranking`、`etfTopics`、`etfTopic`、`fearGreedIndex`

**业务状态样例（脱敏）：** `{"ret": 0}`

### 22. `GET hq2.skytigris.cn/value_analysis/index/.IXIC`

- **用途：** 行情数据 / 估值分析
- **说明：** 获取指数估值、分位点及统计比较结果。
- **抓包表现：** 调用 7 次，平均 324.1 ms，业务成功
- **认证：** Bearer Token；Cookie 名称：JSESSIONID, acw_tc, ngxid, org.springframework.web.servlet.i18n.CookieLocaleResolver.LOCALE
- **重放边界：** 可生成只读模板，仍需合法凭据

**业务查询参数**

| 参数 | 类型 | 抓包中每次出现 | 说明 |
|---|---|---|---|
| `algorithm` | integer | 是 | 抓包中观察到的参数 |
| `factor` | string | 是 | 抓包中观察到的参数 |
| `limit` | integer | 否 | 数量上限 |
| `order` | string | 否 | 抓包中观察到的参数 |
| `period` | string | 是 | 周期 |
| `rankingId` | string | 否 | 抓包中观察到的参数 |
| `statFactor` | string | 否 | 抓包中观察到的参数 |
| `withConstituent` | integer | 是 | 抓包中观察到的参数 |
| `withQuantilePoint` | integer | 是 | 抓包中观察到的参数 |
| `withStdDeviation` | integer | 是 | 抓包中观察到的参数 |
| `withValueCompare` | integer | 是 | 抓包中观察到的参数 |

**公共客户端上下文：** `__v_account__, appName, appVer, channel, device, device_model, deviceId, edition, keyfrom, lang, license, location, os, osVer, platform, region, screenH, screenW, skin, uuid, vendor`

**响应结构：** `ret`、`serverTime`、`item`

**业务状态样例（脱敏）：** `{"ret": 0}`

### 23. `GET stock-news.laohu8.com/v1/news/suture/list`

- **用途：** 新闻资讯 / 证券资讯
- **说明：** 获取证券新闻、置顶资讯或公告提示。
- **抓包表现：** 调用 1 次，平均 393.0 ms，业务成功
- **认证：** Bearer Token；Cookie 名称：acw_tc, ngxid
- **重放边界：** 可生成只读模板，仍需合法凭据

**业务查询参数**

| 参数 | 类型 | 抓包中每次出现 | 说明 |
|---|---|---|---|
| `pageCount` | integer | 是 | 页码/批次 |
| `pageSize` | integer | 是 | 每页数量 |
| `symbol` | string | 是 | 证券代码 |

**公共客户端上下文：** `__v_account__, appName, appVer, channel, device, device_model, deviceId, edition, keyfrom, lang, lang_content, license, location, os, osVer, platform, region, screenH, screenW, skin, uuid, vendor`

**响应结构：** `items`、`pageSize`、`totalPage`、`pageCount`、`totalSize`

### 24. `GET stock-news.laohu8.com/v1/top/news/list`

- **用途：** 新闻资讯 / 证券资讯
- **说明：** 获取证券新闻、置顶资讯或公告提示。
- **抓包表现：** 调用 2 次，平均 199.0 ms，业务成功
- **认证：** Bearer Token；Cookie 名称：acw_tc, ngxid
- **重放边界：** 可生成只读模板，仍需合法凭据

**业务查询参数**

| 参数 | 类型 | 抓包中每次出现 | 说明 |
|---|---|---|---|
| `include_expiry` | integer | 是 | 抓包中观察到的参数 |
| `pageCount` | integer | 是 | 页码/批次 |
| `pageSize` | integer | 是 | 每页数量 |
| `symbol` | string | 是 | 证券代码 |

**公共客户端上下文：** `__v_account__, appName, appVer, channel, device, device_model, deviceId, edition, keyfrom, lang, lang_content, license, location, os, osVer, platform, region, screenH, screenW, skin, uuid, vendor`

**响应结构：** `items`、`pageSize`、`totalPage`、`pageCount`、`totalSize`

### 25. `GET stock-news.laohu8.com/v2/news/list`

- **用途：** 新闻资讯 / 证券资讯
- **说明：** 获取证券新闻、置顶资讯或公告提示。
- **抓包表现：** 调用 3 次，平均 591.3 ms，业务成功
- **认证：** Bearer Token；Cookie 名称：acw_tc, ngxid
- **重放边界：** 可生成只读模板，仍需合法凭据

**业务查询参数**

| 参数 | 类型 | 抓包中每次出现 | 说明 |
|---|---|---|---|
| `isLive` | integer | 否 | 抓包中观察到的参数 |
| `pageCount` | integer | 是 | 页码/批次 |
| `pageSize` | integer | 是 | 每页数量 |
| `property` | string | 否 | 资讯属性 |
| `symbols` | string | 是 | 证券代码列表 |

**公共客户端上下文：** `__v_account__, appName, appVer, channel, device, device_model, deviceId, edition, keyfrom, lang, lang_content, license, location, os, osVer, platform, region, screenH, screenW, skin, uuid, vendor`

**响应结构：** `items`、`pageSize`、`totalPage`、`pageCount`、`totalSize`、`code`、`status`

**业务状态样例（脱敏）：** `{"code": "91000000", "status": "200"}`

### 26. `GET trade.skytigris.cn/ipos`

- **用途：** IPO 数据 / IPO 列表
- **说明：** 获取当前可查看或可申购的 IPO 数据。
- **抓包表现：** 调用 2 次，平均 89.0 ms，业务成功
- **认证：** Bearer Token；Cookie 名称：JSESSIONID, ngxid
- **重放边界：** 可生成只读模板，仍需合法凭据

**业务查询参数：** 未观察到独立业务参数。

**公共客户端上下文：** `__v_account__, appName, appVer, channel, device, device_model, deviceId, edition, keyfrom, lang, license, location, os, osVer, platform, region, screenH, screenW, skin, uuid, vendor`

**响应结构：** `status`、`msg`、`data`、`timestamp`

**业务状态样例（脱敏）：** `{"status": "ok", "msg": "ok"}`
