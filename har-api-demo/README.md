# 知势 Pulse 股票情报工作台 Demo

根据 HAR 静态生成的股票情报产品样例。它把筛选后的 26 个股票资讯接口封装成面向投资者的聚合研究工作台，并保留接口目录作为技术视图。

## 已交付内容

- `docs/API_CATALOG.md`：26 个保留接口的参数、响应结构和业务状态。
- `docs/DATA_ANALYSIS.md`：筛选口径与股票资讯数据分析。
- `src/data/catalog.json`：前端使用的脱敏接口目录。
- `scripts/build_catalog.py`：可重复运行的 HAR 解析、脱敏与股票资讯筛选脚本。
- React + Vite Demo：搜索、分类筛选、接口详情、离线响应演示、cURL 占位模板与文档导出。
- “知势 Pulse”产品主屏：自选、分时行情、AI 速览、真实脱敏盘口快照、成交、新闻、社区情绪和 IPO 聚合视图。
- 交互 K 线：提供 M5、M15、M30、H1、日K、周K、月K 七个周期；逐根展示开盘、最高、最低、收盘、涨跌与成交量，支持鼠标悬停、键盘逐点查看和按住图表左右拖拽。前端单周期最多加载 2,000 根，视窗固定绘制 72 根以保持拖拽流畅。
- K 线样例生成遵循盘中连续性：分钟/小时周期同一交易日内下一根开盘等于上一根收盘，仅跨交易日允许小幅跳空；提示框分别展示“实体涨跌”和“较前收”，避免颜色与涨跌口径混淆。
- 每根样例 K 线会生成周期内价格路径，再由路径最大值/最小值计算高低价；因此自然混合双影线、单影线和无影线形态，不再强制每根上下两端都有影线。
- 40 档盘口：使用 `askBidDepth` 中的 Blue Ocean Level 2 数据，支持买卖双边、仅卖、仅买三种视图，展示价格、数量与委托笔数。Demo 内含 PYPL、NVDA、AAPL 三组脱敏离线快照，自选切换时盘口同步更新；指数 `.IXIC` 不提供普通证券盘口。
- 股票新闻：合并 `/v1/top/news/list`、`/v1/news/suture/list` 和 `/v2/news/list` 三路数据，按新闻 ID 去重；PYPL、NVDA、AAPL 和 `.IXIC` 均有独立离线快照，自选切换时同步更新。
- 社区情绪：讨论总数与热门话题来自社区接口原始字段；看空/中性/看多为每只标的最新帖子样本的关键词倾向统计，UI 明确显示样本量与“估算”标记。

## 本地运行

```powershell
npm install
npm run dev -- --host 127.0.0.1 --port 4178
```

浏览器打开 `http://127.0.0.1:4178`。

## 重新生成目录

```powershell
python .\scripts\build_catalog.py --har "D:\path\capture.har"
```

默认生成过程只读取 HAR。若要使用 HAR 中的请求模板抓取快照，可显式增加 `--depth-symbols NVDA,AAPL --news-symbols PYPL,NVDA,AAPL,.IXIC --community-symbols PYPL,NVDA,AAPL,.IXIC`。盘口和新闻使用 GET；社区主题查询使用只读 POST。输出会脱敏，不会保存认证头和 Cookie。不要把原始 HAR 复制进项目或提交到版本库。

## 安全边界

- “运行离线示例”只展示脱敏后的抓包响应，不调用线上接口。
- HAR 只捕获到 `period=day` 的分时增量请求，响应 `items` 仅 1 条；没有捕获 M5、M15、M30、H1、日K、周K、月K 历史 OHLCV 接口，因此无法确认服务端周期枚举和单次最大返回量。七周期 K 线是确定性离线交互样例，不能视为真实历史行情。
- “复制 cURL 模板”使用 `<YOUR_TOKEN>` 占位令牌。
- POST 模板固定标记 `<REVIEW_REQUIRED>`，需要人工确认接口语义。
