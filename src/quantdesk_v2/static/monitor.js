class ContractMonitor extends window.QuantDeskPageController {
  constructor(host) {
    super(host, { shadow: true });
    this.state = {
      overview: [],
      activeMarket: "binance",
      usQuotes: [],
      usQuoteMeta: null,
      usMarketStatus: null,
      intelligence: null,
      watchlist: new Set(),
      lastAlertId: 0,
      spreadAlertStates: new Map(),
      modal: {
        symbol: null,
        tf: "1h",
        opportunity: null,
        indicators: [],
        selectedIndicator: null,
        predictionContext: null,
        overviewError: null,
        marketTransport: "idle",
        marketUpdatedAt: 0,
        tickerTransport: null,
        depthTransport: null,
      },
      chart: {
        klines: [],
        realCount: 0,
        signals: [],
        series: null,
        projection: null,
        visibleCount: 90,
        rightOffset: 0,
        hoverIndex: null,
        hoverY: null,
        dragging: false,
        dragStartX: 0,
        dragStartOffset: 0,
        overlays: new Set(["ma20", "ma50", "ma60", "boll", "volma", "signals", "projection"]),
      },
      history: {
        page: 1,
        pages: 1,
        total: 0,
        loading: false,
        statistics: null,
        hourlyStatistics: [],
        timeRange: { startMs: null, endMs: null },
        filters: { direction: "all", horizon: "all", hit: "all" },
      },
      predictionConfig: { id: null, loading: false },
      algorithm: {
        data: null, loading: false, optimizing: false, optimization: null, trace: null,
        historyRecords: [],
      },
      matrixSort: { key: "ai", direction: "desc" },
      sound: true,
      notifyOn: false,
    };
    this.timers = [];
    this.newsTimer = null;
    this.running = false;
    this.audioContext = null;
    this.chartResizeObserver = null;
    this.chartGeometry = null;
    this.modalMarketSocket = null;
    this.modalMarketGeneration = 0;
    this.modalMarketLastEventAt = 0;
    this.modalMarketFallbackTimer = null;
    this.modalMarketRestTimer = null;
    this.modalMarketReconnectTimer = null;
    this.modalMarketWatchdogTimer = null;
    this.renderShell();
  }

  connectedCallback() {
    this.bindEvents();
  }

  disconnectedCallback() {
    this.pause();
    this.chartResizeObserver?.disconnect();
  }

  renderShell() {
    this.shadowRoot.innerHTML = `
      <link rel="stylesheet" href="/assets/monitor.css?v=20260901-research-pages">
      <div class="monitor">
        <header class="monitor-head">
          <div class="monitor-logo">⚡ QuantDesk <small>多市场行情监控</small></div>
          <div class="monitor-actions">
            <span class="clock" id="monitor-clock"></span>
            <span id="us-market-state" class="badge hidden">美股状态…</span>
            <span id="engine-state" class="badge">连接中…</span>
            <button id="btn-refresh" type="button">刷新</button>
            <button id="btn-notify" type="button">通知</button>
            <button id="btn-sound" class="on" type="button">音效</button>
          </div>
        </header>
        <div id="error-banner" class="error-banner hidden"></div>
        <nav class="market-tabs" role="tablist" aria-label="切换市场数据源">
          <button class="market-tab active" type="button" role="tab" aria-selected="true" data-market="binance">
            <span>币安 TradFi 合约</span><small>Binance Futures · USDT 合约</small>
          </button>
          <button class="market-tab" type="button" role="tab" aria-selected="false" data-market="finnhub">
            <span>美股现货</span><small>Finnhub · US Equities</small>
          </button>
        </nav>
        <div id="binance-market-view" class="market-view">
        <section id="intelligence-strip" class="intelligence-strip" aria-label="机会引擎反馈">
          <article><span>实时数据覆盖</span><strong id="intel-coverage">--</strong><small>行情与战局预测</small></article>
          <article class="intel-scanner-card"><span>活跃扫描器</span><strong id="intel-scanners">--</strong><small id="intel-opportunities">等待数据</small><button id="btn-prediction-algorithm" class="intel-algorithm-open" type="button">查看预测算法</button></article>
          <article class="intel-result-card"><span class="intel-heading">结果标签 <button class="intel-help" type="button" aria-label="结果标签说明" data-tip="每轮会为每个合约生成 5m、15m、1h 三种预测。已完成表示观察窗口结束并取得有效价格后已经标注；待完成表示仍在等待窗口到期或标注处理。到期后仍无有效价格的样本会标为不可用，不计入已完成数和命中率。它们是评估样本，不是订单、持仓或待交易数量。">?</button></span><strong id="intel-labels">--</strong><small id="intel-pending">等待校准</small><button id="btn-prediction-history" class="intel-history-open" type="button">查看历史预测</button></article>
          <article><span class="intel-heading">方向命中率 <button class="intel-help" type="button" aria-label="方向命中率说明" data-tip="仅统计已经完成的看多/看空预测：看多后成本后价格上涨，或看空后成本后价格下跌，记为命中。中性预测不进入方向命中率分母。这是启发式模型的历史标注结果，不是实盘胜率或收益承诺。">?</button></span><strong id="intel-hit-rate">--</strong><small id="intel-return">完成样本后显示</small></article>
          <article><span>Shadow执行</span><strong id="intel-shadow">LOCKED</strong><small>实盘保持锁定</small></article>
        </section>
        <div class="monitor-layout">
          <section class="panel grid-panel">
            <div class="panel-title">
              <span>合约监控 <em id="sym-count"></em></span>
              <div class="filters">
                <button id="btn-import-watchlist" class="watchlist-action import-watchlist" type="button">维护自选</button>
                <button id="btn-clear-watchlist" class="watchlist-action clear-watchlist" type="button">清除自选</button>
                <button id="btn-sync-positions" class="watchlist-action sync-positions" type="button">同步持仓到监控</button>
                <input id="search" aria-label="搜索合约" placeholder="搜索…">
                <select id="filter" aria-label="筛选合约">
                  <option value="all">全部</option>
                  <option value="mine">自选</option>
                  <option value="long">多头机会</option>
                  <option value="short">空头机会</option>
                  <option value="neutral">中性观察</option>
                </select>
              </div>
            </div>
            <div id="breadth" class="breadth">市场结论计算中…</div>
            <div id="contract-grid" class="contract-grid"></div>
          </section>
          <aside class="monitor-side">
            <section class="panel">
              <div class="panel-title"><span>信号提醒</span><button id="btn-clear-alerts" type="button">全部已读</button></div>
              <div id="alerts" class="alerts"></div>
            </section>
            <section class="panel grow">
              <div class="panel-title"><span>多轮新闻研判 <em id="news-count"></em></span></div>
              <div id="news" class="news"></div>
            </section>
          </aside>
        </div>
        </div>
        <div id="finnhub-market-view" class="market-view us-market-view hidden">
          <section class="panel us-market-panel">
            <div class="panel-title us-panel-title">
              <div><span>美股现货行情 <em id="us-symbol-count">0/0</em></span><small>独立数据源：Finnhub，不使用 Binance 合约价格</small></div>
              <div class="filters us-filters">
                <input id="us-search" aria-label="搜索美股" placeholder="搜索股票…">
                <select id="us-sort" aria-label="美股排序">
                  <option value="pct">按涨跌幅</option>
                  <option value="alpha">按代码</option>
                  <option value="price">按价格</option>
                </select>
              </div>
            </div>
            <div id="us-market-summary" class="breadth">正在连接 Finnhub 美股行情…</div>
            <div id="us-stock-grid" class="contract-grid us-stock-grid"></div>
          </section>
        </div>
      </div>
      <div id="modal" class="modal hidden" aria-hidden="true">
        <div class="modal-box research-modal" role="dialog" aria-modal="true" aria-labelledby="modal-symbol">
          <aside class="research-sidebar">
            <nav class="research-tabs" role="tablist" aria-label="研究内容导航">
              <button class="on" type="button" role="tab" aria-selected="true" data-modal-page="overview" data-modal-section="#modal-trend"><span>01</span><strong>趋势</strong><small>K线与成交量</small></button>
              <button type="button" role="tab" aria-selected="false" data-modal-page="overview" data-modal-section="#modal-indicator-section"><span>02</span><strong>策略指标</strong><small>20项指标</small></button>
              <button type="button" role="tab" aria-selected="false" data-modal-page="battle" data-modal-section="#modal-battle-section"><span>03</span><strong>多空预测</strong><small>概率与方向</small></button>
              <button type="button" role="tab" aria-selected="false" data-modal-page="strategy" data-modal-section="#modal-strategy-section"><span>04</span><strong>策略机会</strong><small>条件与有效期</small></button>
              <button type="button" role="tab" aria-selected="false" data-modal-page="report" data-modal-section="#modal-report-section"><span>05</span><strong>研判报告</strong><small>多周期研判</small></button>
              <button type="button" role="tab" aria-selected="false" data-modal-page="factors" data-modal-section="#modal-factor-section"><span>06</span><strong>评分因子</strong><small>因子贡献</small></button>
              <button type="button" role="tab" aria-selected="false" data-modal-page="news" data-modal-section="#modal-news-section"><span>07</span><strong>新闻列表</strong><small>老虎证券资讯</small></button>
            </nav>
          </aside>
          <div class="research-workspace">
          <header class="modal-head research-modal-head">
            <div class="research-identity">
              <div class="research-title-line">
                <span id="modal-market" class="research-market">美股映射合约</span>
                <strong id="modal-symbol" class="modal-symbol"></strong>
                <span id="modal-price" class="modal-price"></span>
                <span id="modal-pct" class="research-change"></span>
                <button id="modal-watch" class="watch" type="button" aria-label="切换自选" title="加入或移出自选">☆</button>
              </div>
              <p id="modal-source" class="research-source">数据源：Binance Futures · 量化结果仅供研究</p>
            </div>
            <div class="research-head-actions">
              <span class="research-mode">量化研判</span>
              <button id="modal-close" class="research-close" type="button" aria-label="关闭证券研究弹窗">关闭</button>
            </div>
          </header>

          <section class="research-metrics" data-research-page="overview" aria-label="标的关键行情">
            <article><span>最新价</span><strong id="modal-metric-price">--</strong><small>实时合约报价</small></article>
            <article><span>24h 涨跌</span><strong id="modal-metric-change">--</strong><small id="modal-metric-change-note">相对 24 小时前</small></article>
            <article><span>24h 成交额</span><strong id="modal-metric-volume">--</strong><small>计价资产口径</small></article>
            <article><span>订单池深度</span><strong id="modal-metric-depth">--</strong><small>买卖盘合计</small></article>
            <article><span>5m 博弈</span><strong id="modal-metric-battle">--</strong><small>多空概率对比</small></article>
            <article><span>机会质量</span><strong id="modal-metric-quality">--</strong><small id="modal-metric-quality-note">等待策略扫描</small></article>
          </section>

          <section id="modal-trend" class="research-section research-trend" data-research-page="overview">
            <div class="research-section-head trend-toolbar">
              <div>
                <strong>趋势与成交量</strong>
                <span>拖拽浏览 · 滚轮缩放 · 悬停查看逐根数据</span>
              </div>
              <span class="tf-switch" aria-label="图表周期">
                <button data-tf="15m" type="button">15 分</button>
                <button data-tf="1h" class="on" type="button">1 小时</button>
                <button data-tf="4h" type="button">4 小时</button>
              </span>
            </div>
            <div class="chart-overlay-toolbar" role="toolbar" aria-label="图表叠加指标">
              <span>叠加指标</span>
              <button class="on" type="button" data-chart-overlay="ma20" aria-pressed="true"><i class="legend-line ma20"></i>MA20</button>
              <button class="on" type="button" data-chart-overlay="ma50" aria-pressed="true"><i class="legend-line ma50"></i>MA50</button>
              <button class="on" type="button" data-chart-overlay="ma60" aria-pressed="true"><i class="legend-line ma60"></i>MA60</button>
              <button class="on" type="button" data-chart-overlay="boll" aria-pressed="true"><i class="legend-line boll"></i>BOLL</button>
              <button class="on" type="button" data-chart-overlay="volma" aria-pressed="true"><i class="legend-line volma"></i>VOL MA20</button>
              <button class="on" type="button" data-chart-overlay="signals" aria-pressed="true"><i class="legend-signal buy">买</i><i class="legend-signal sell">卖</i>历史买卖点</button>
              <button class="on" type="button" data-chart-overlay="projection" aria-pressed="true"><i class="legend-line projection"></i>预测模拟</button>
              <button class="chart-reset" type="button" data-chart-action="reset">复位到最新</button>
              <small id="chart-range">--</small>
            </div>
            <div class="chart-signal-note"><b>时间口径：</b>图中买点是历史 MA 金叉/布林上破，卖点是历史 MA 死叉/布林下破；下方 12 项策略指标只判断最新一根 K 线。</div>
            <div id="chart-projection-note" class="chart-projection-note hidden" aria-live="polite"></div>
            <div id="modal-ohlc" class="research-ohlc"><span>正在加载当前周期行情…</span></div>
            <div id="chart-stage" class="chart-stage">
              <canvas id="chart" class="chart" width="1280" height="500" tabindex="0" aria-label="可拖拽缩放的 K 线、叠加指标、成交量和买卖点图"></canvas>
              <div id="chart-tooltip" class="chart-tooltip hidden" aria-hidden="true"></div>
              <div class="chart-gesture-hint">拖拽左右浏览 · 滚轮缩放 · 双击复位</div>
            </div>
          </section>

          <section id="modal-indicator-section" class="research-section strategy-indicator-section" data-research-page="overview">
            <div class="research-section-head">
              <div><strong>策略与预测指标</strong><span id="indicator-total-caption">共 20 项 · 正在读取真实数据</span></div>
            </div>
            <div class="strategy-indicator-group">
              <div class="strategy-indicator-group-head"><strong>K 线策略</strong><span id="strategy-indicator-caption">最新一根 K 线：-- · 正在计算 12 项</span></div>
              <div id="strategy-indicator-list" class="strategy-indicator-list" role="tablist" aria-label="选择 K 线策略指标"></div>
            </div>
            <div class="strategy-indicator-group prediction-feature-group">
              <div class="strategy-indicator-group-head"><strong>预测因子</strong><span id="prediction-feature-caption">最新预测快照：-- · 正在读取 8 项</span></div>
              <div id="prediction-feature-list" class="strategy-indicator-list prediction-feature-list" role="tablist" aria-label="选择预测因子"></div>
            </div>
            <div id="strategy-indicator-detail" class="strategy-indicator-detail"></div>
          </section>
          <section id="modal-battle-section" class="research-section" data-research-page="battle" hidden>
            <div id="battle-detail" class="battle-detail"></div>
          </section>
          <section id="modal-strategy-section" class="research-section" data-research-page="strategy" hidden>
            <div class="research-section-head"><div><strong>策略机会</strong><span>条件、有效期与匹配策略</span></div></div>
            <div id="opportunity-detail" class="opportunity-detail"></div>
          </section>
          <section id="modal-report-section" class="research-section" data-research-page="report" hidden>
            <div class="research-section-head"><div><strong>多周期研判报告</strong><span>价格结构与新闻仅作辅助证据</span></div></div>
            <div id="score-summary" class="score-summary"></div>
            <div id="report" class="report"></div>
          </section>
          <section id="modal-factor-section" class="research-section" data-research-page="factors" hidden>
            <div class="research-section-head"><div><strong>评分因子</strong><span>当前图表周期的因子贡献</span></div></div>
            <div id="factors" class="factors"></div>
          </section>
          <section id="modal-news-section" class="research-section research-news-section" data-research-page="news" hidden>
            <div class="research-section-head">
              <div><strong>新闻列表</strong><span id="research-news-caption">老虎证券三路新闻接口聚合</span></div>
            </div>
            <div id="research-news-list" class="research-news-list" aria-live="polite">
              <div class="research-news-state">打开标的后加载相关新闻…</div>
            </div>
          </section>
          </div>
        </div>
      </div>
      <div id="prediction-history-modal" class="modal hidden" aria-hidden="true">
        <div class="modal-box prediction-history-box" role="dialog" aria-modal="true" aria-labelledby="prediction-history-title">
          <div class="modal-head prediction-history-head">
            <div>
              <strong id="prediction-history-title" class="modal-symbol">历史预测</strong>
              <span class="dim">仅显示已出结果的看多 / 看空预测 · 按判断时间倒序 · 每页固定 50 条</span>
            </div>
            <button id="prediction-history-close" type="button">关闭</button>
          </div>
          <section class="prediction-history-overview" aria-label="历史预测统计与筛选">
            <div class="prediction-history-stats">
              <article><span>方向样本</span><strong id="history-stat-total">--</strong></article>
              <article><span>方向命中率</span><strong id="history-stat-hit-rate">--</strong></article>
              <article><span>成本后平均</span><strong id="history-stat-return">--</strong></article>
              <article><span>看多 / 看空</span><strong id="history-stat-directions">--</strong></article>
            </div>
            <div class="prediction-history-filters">
              <div class="history-time-filter"><span>时间范围</span><label>开始<input id="history-time-start" type="datetime-local" step="3600"></label><label>结束<input id="history-time-end" type="datetime-local" step="3600"></label><button id="history-time-apply" type="button">查询</button><button id="history-time-recent" type="button">最近24小时</button><button id="history-time-all" type="button">全部</button><small id="history-time-message">按整点小时筛选，最长 7 天</small></div>
              <div><span>方向</span><button class="on" type="button" data-history-filter="direction" data-filter-value="all">全部</button><button type="button" data-history-filter="direction" data-filter-value="long">看多</button><button type="button" data-history-filter="direction" data-filter-value="short">看空</button></div>
              <div><span>周期</span><button class="on" type="button" data-history-filter="horizon" data-filter-value="all">全部</button><button type="button" data-history-filter="horizon" data-filter-value="300">5m</button><button type="button" data-history-filter="horizon" data-filter-value="900">15m</button><button type="button" data-history-filter="horizon" data-filter-value="3600">1h</button></div>
              <div><span>结果</span><button class="on" type="button" data-history-filter="hit" data-filter-value="all">全部</button><button type="button" data-history-filter="hit" data-filter-value="hit">命中</button><button type="button" data-history-filter="hit" data-filter-value="miss">未命中</button></div>
            </div>
            <section class="prediction-hourly-panel" aria-label="每小时方向胜率">
              <header><strong>每小时胜率</strong><span id="history-hourly-caption">所选范围内每个小时的方向命中率</span></header>
              <div id="history-hourly-list" class="prediction-hourly-list"><span class="hourly-empty">正在加载小时统计…</span></div>
            </section>
          </section>
          <div class="prediction-history-table-wrap">
            <table class="prediction-history-table">
              <thead><tr>
                <th>判断时间</th><th>合约 / 周期</th><th>开盘价格</th><th>预测方向 / 评分</th>
                <th>多 / 空 / 中概率</th><th>置信度</th><th>结算时间</th><th>结算价格</th>
                <th>结算标签 / 状态</th><th>方向命中 / 成本后收益</th><th>最大有利 / 不利</th><th>操作</th>
              </tr></thead>
              <tbody id="prediction-history-body"><tr><td colspan="12" class="history-empty">点击后加载历史记录</td></tr></tbody>
            </table>
          </div>
          <footer class="prediction-history-footer">
            <span id="prediction-history-summary">共 0 条 · 每页 50 条</span>
            <div class="prediction-history-pages">
              <button type="button" data-history-page="first">首页</button>
              <button type="button" data-history-page="prev">上一页</button>
              <strong id="prediction-history-page">第 1 / 1 页</strong>
              <button type="button" data-history-page="next">下一页</button>
              <button type="button" data-history-page="last">末页</button>
            </div>
          </footer>
        </div>
      </div>
      <div id="prediction-config-modal" class="modal hidden" aria-hidden="true">
        <div class="modal-box prediction-config-box" role="dialog" aria-modal="true" aria-labelledby="prediction-config-title">
          <div class="modal-head prediction-config-head">
            <div>
              <strong id="prediction-config-title" class="modal-symbol">历史预测指标</strong>
              <span id="prediction-config-version" class="dim">正在读取该条预测的配置…</span>
            </div>
            <button id="prediction-config-close" type="button">关闭</button>
          </div>
          <div id="prediction-config-content" class="prediction-config-content" aria-live="polite">
            <p class="prediction-config-loading">正在加载预测指标…</p>
          </div>
        </div>
      </div>
      <div id="prediction-algorithm-modal" class="modal hidden" aria-hidden="true">
        <div class="modal-box prediction-algorithm-box" role="dialog" aria-modal="true" aria-labelledby="prediction-algorithm-title">
          <div class="modal-head prediction-algorithm-head">
            <div>
              <strong id="prediction-algorithm-title" class="modal-symbol">当前预测算法</strong>
              <span id="prediction-algorithm-version" class="dim">正在读取配置…</span>
            </div>
            <div class="prediction-algorithm-head-actions">
              <button id="prediction-algorithm-ai-trace" class="hidden" type="button">查看AI过程</button>
              <button id="prediction-algorithm-ai-history" class="hidden" type="button" disabled>查看历史分析记录</button>
              <button id="prediction-algorithm-optimize" class="primary" type="button">AI优化算法</button>
              <button id="prediction-algorithm-close" type="button">关闭</button>
            </div>
          </div>
          <section class="algorithm-rules" aria-label="算法规则说明">
            <article><strong>综合评分</strong><span>8 个市场因子标准化到 -1～+1；12 项 K 线策略触发记 +1、否则记 0。20 项按周期权重加总后再扣除拥挤惩罚。</span></article>
            <article><strong>方向判断</strong><span>评分达到正阈值判为看多，低于负阈值判为看空，阈值之间保持中性。</span></article>
            <article><strong>数据门槛</strong><span>数据质量不足、微观行情过期或持仓数据过期时强制中性，不参与方向命中率。</span></article>
            <article><strong>结算规则</strong><span>分别在 5m、15m、1h 观察窗口结算，方向收益扣除价差成本后大于 0 记为命中。</span></article>
          </section>
          <section id="prediction-algorithm-optimization" class="algorithm-optimization hidden" aria-live="polite"></section>
          <form id="prediction-algorithm-form" class="prediction-algorithm-form">
            <section class="algorithm-parameters">
              <label><span>方向阈值</span><input type="number" min="0.05" max="0.5" step="0.01" data-algorithm-scalar="direction_threshold"><small>越高越谨慎，中性预测越多</small></label>
              <label><span>最低数据质量</span><input type="number" min="0.5" max="1" step="0.01" data-algorithm-scalar="min_data_quality"><small>低于此值时强制中性</small></label>
              <label><span>账户拥挤惩罚</span><input type="number" min="0" max="0.5" step="0.01" data-algorithm-scalar="account_crowding_penalty"><small>逆向削弱拥挤方向评分</small></label>
              <label><span>资金费率拥挤惩罚</span><input type="number" min="0" max="0.5" step="0.01" data-algorithm-scalar="funding_crowding_penalty"><small>削弱资金费率过热方向</small></label>
            </section>
            <div class="algorithm-weight-wrap">
              <table class="algorithm-weight-table">
                <thead><tr><th>评分特征</th><th>5m 权重</th><th>15m 权重</th><th>1h 权重</th><th>规则含义</th></tr></thead>
                <tbody>
                  <tr class="algorithm-feature-group"><th colspan="5">市场与微观因子 · 8 项</th></tr>
                  <tr><th>主动成交流 <button type="button" data-algorithm-enabled="aggressive_flow">启用</button></th><td><input type="number" data-algorithm-weight="aggressive_flow" data-horizon="5m"></td><td><input type="number" data-algorithm-weight="aggressive_flow" data-horizon="15m"></td><td><input type="number" data-algorithm-weight="aggressive_flow" data-horizon="1h"></td><td>主动买入与主动卖出强弱</td></tr>
                  <tr><th>订单簿失衡 <button type="button" data-algorithm-enabled="book_imbalance">启用</button></th><td><input type="number" data-algorithm-weight="book_imbalance" data-horizon="5m"></td><td><input type="number" data-algorithm-weight="book_imbalance" data-horizon="15m"></td><td><input type="number" data-algorithm-weight="book_imbalance" data-horizon="1h"></td><td>完整深度买卖盘力量差</td></tr>
                  <tr><th>近五档失衡 <button type="button" data-algorithm-enabled="book_imbalance_5">启用</button></th><td><input type="number" data-algorithm-weight="book_imbalance_5" data-horizon="5m"></td><td><input type="number" data-algorithm-weight="book_imbalance_5" data-horizon="15m"></td><td><input type="number" data-algorithm-weight="book_imbalance_5" data-horizon="1h"></td><td>盘口近端流动性倾斜</td></tr>
                  <tr><th>价格速度 <button type="button" data-algorithm-enabled="velocity">启用</button></th><td><input type="number" data-algorithm-weight="velocity" data-horizon="5m"></td><td><input type="number" data-algorithm-weight="velocity" data-horizon="15m"></td><td><input type="number" data-algorithm-weight="velocity" data-horizon="1h"></td><td>最近一分钟价格变化速度</td></tr>
                  <tr><th>闪动失衡 <button type="button" data-algorithm-enabled="flash_imbalance">启用</button></th><td><input type="number" data-algorithm-weight="flash_imbalance" data-horizon="5m"></td><td><input type="number" data-algorithm-weight="flash_imbalance" data-horizon="15m"></td><td><input type="number" data-algorithm-weight="flash_imbalance" data-horizon="1h"></td><td>30 分钟上涨与下跌闪动差</td></tr>
                  <tr><th>Taker 流向 <button type="button" data-algorithm-enabled="taker_flow">启用</button></th><td><input type="number" data-algorithm-weight="taker_flow" data-horizon="5m"></td><td><input type="number" data-algorithm-weight="taker_flow" data-horizon="15m"></td><td><input type="number" data-algorithm-weight="taker_flow" data-horizon="1h"></td><td>Binance 主动买卖量比</td></tr>
                  <tr><th>价格 × 持仓量 <button type="button" data-algorithm-enabled="price_oi_impulse">启用</button></th><td><input type="number" data-algorithm-weight="price_oi_impulse" data-horizon="5m"></td><td><input type="number" data-algorithm-weight="price_oi_impulse" data-horizon="15m"></td><td><input type="number" data-algorithm-weight="price_oi_impulse" data-horizon="1h"></td><td>价格变化与未平仓量联合冲量</td></tr>
                  <tr><th>周期趋势 <button type="button" data-algorithm-enabled="trend">启用</button></th><td><input type="number" data-algorithm-weight="trend" data-horizon="5m"></td><td><input type="number" data-algorithm-weight="trend" data-horizon="15m"></td><td><input type="number" data-algorithm-weight="trend" data-horizon="1h"></td><td>15m 趋势；1h 使用 1h 与 4h 组合</td></tr>
                  <tr class="algorithm-feature-group"><th colspan="5">K 线策略信号 · 12 项（触发 +1，未触发或数据不足 0）</th></tr>
                  <tr><th>布林突破 <button type="button" data-algorithm-enabled="kline_bollinger_breakout">启用</button></th><td><input type="number" data-algorithm-weight="kline_bollinger_breakout" data-horizon="5m"></td><td><input type="number" data-algorithm-weight="kline_bollinger_breakout" data-horizon="15m"></td><td><input type="number" data-algorithm-weight="kline_bollinger_breakout" data-horizon="1h"></td><td>最新收盘由布林带内向上突破上轨</td></tr>
                  <tr><th>均线回踩反弹 <button type="button" data-algorithm-enabled="kline_moving_average_pullback_bounce">启用</button></th><td><input type="number" data-algorithm-weight="kline_moving_average_pullback_bounce" data-horizon="5m"></td><td><input type="number" data-algorithm-weight="kline_moving_average_pullback_bounce" data-horizon="15m"></td><td><input type="number" data-algorithm-weight="kline_moving_average_pullback_bounce" data-horizon="1h"></td><td>多头趋势中回踩并收复 MA20</td></tr>
                  <tr><th>趋势突破 <button type="button" data-algorithm-enabled="kline_trend_breakout">启用</button></th><td><input type="number" data-algorithm-weight="kline_trend_breakout" data-horizon="5m"></td><td><input type="number" data-algorithm-weight="kline_trend_breakout" data-horizon="15m"></td><td><input type="number" data-algorithm-weight="kline_trend_breakout" data-horizon="1h"></td><td>突破 20 期结构高点并获得量能确认</td></tr>
                  <tr><th>量价齐升 <button type="button" data-algorithm-enabled="kline_price_volume_rise">启用</button></th><td><input type="number" data-algorithm-weight="kline_price_volume_rise" data-horizon="5m"></td><td><input type="number" data-algorithm-weight="kline_price_volume_rise" data-horizon="15m"></td><td><input type="number" data-algorithm-weight="kline_price_volume_rise" data-horizon="1h"></td><td>价格上涨且成交量同步放大</td></tr>
                  <tr><th>新低反转 <button type="button" data-algorithm-enabled="kline_new_low_reversal">启用</button></th><td><input type="number" data-algorithm-weight="kline_new_low_reversal" data-horizon="5m"></td><td><input type="number" data-algorithm-weight="kline_new_low_reversal" data-horizon="15m"></td><td><input type="number" data-algorithm-weight="kline_new_low_reversal" data-horizon="1h"></td><td>创新低后收复前低并转强</td></tr>
                  <tr><th>缩量回踩 <button type="button" data-algorithm-enabled="kline_low_volume_pullback">启用</button></th><td><input type="number" data-algorithm-weight="kline_low_volume_pullback" data-horizon="5m"></td><td><input type="number" data-algorithm-weight="kline_low_volume_pullback" data-horizon="15m"></td><td><input type="number" data-algorithm-weight="kline_low_volume_pullback" data-horizon="1h"></td><td>上升趋势中缩量回踩 MA20</td></tr>
                  <tr><th>强势高开 <button type="button" data-algorithm-enabled="kline_strong_gap_open">启用</button></th><td><input type="number" data-algorithm-weight="kline_strong_gap_open" data-horizon="5m"></td><td><input type="number" data-algorithm-weight="kline_strong_gap_open" data-horizon="15m"></td><td><input type="number" data-algorithm-weight="kline_strong_gap_open" data-horizon="1h"></td><td>周期高开至少 2% 且收盘保持强势</td></tr>
                  <tr><th>均线多头 <button type="button" data-algorithm-enabled="kline_moving_average_bull">启用</button></th><td><input type="number" data-algorithm-weight="kline_moving_average_bull" data-horizon="5m"></td><td><input type="number" data-algorithm-weight="kline_moving_average_bull" data-horizon="15m"></td><td><input type="number" data-algorithm-weight="kline_moving_average_bull" data-horizon="1h"></td><td>价格与 MA10、MA20、MA50 多头排列</td></tr>
                  <tr><th>MA 金叉 <button type="button" data-algorithm-enabled="kline_ma_golden_cross">启用</button></th><td><input type="number" data-algorithm-weight="kline_ma_golden_cross" data-horizon="5m"></td><td><input type="number" data-algorithm-weight="kline_ma_golden_cross" data-horizon="15m"></td><td><input type="number" data-algorithm-weight="kline_ma_golden_cross" data-horizon="1h"></td><td>MA20 最新上穿 MA60</td></tr>
                  <tr><th>MACD 金叉放量 <button type="button" data-algorithm-enabled="kline_macd_golden_cross_volume">启用</button></th><td><input type="number" data-algorithm-weight="kline_macd_golden_cross_volume" data-horizon="5m"></td><td><input type="number" data-algorithm-weight="kline_macd_golden_cross_volume" data-horizon="15m"></td><td><input type="number" data-algorithm-weight="kline_macd_golden_cross_volume" data-horizon="1h"></td><td>MACD 最新金叉并获得量能确认</td></tr>
                  <tr><th>超跌反弹 <button type="button" data-algorithm-enabled="kline_oversold_bounce">启用</button></th><td><input type="number" data-algorithm-weight="kline_oversold_bounce" data-horizon="5m"></td><td><input type="number" data-algorithm-weight="kline_oversold_bounce" data-horizon="15m"></td><td><input type="number" data-algorithm-weight="kline_oversold_bounce" data-horizon="1h"></td><td>RSI 低位恢复并出现价格反弹</td></tr>
                  <tr><th>超跌反转 <button type="button" data-algorithm-enabled="kline_oversold_reversal">启用</button></th><td><input type="number" data-algorithm-weight="kline_oversold_reversal" data-horizon="5m"></td><td><input type="number" data-algorithm-weight="kline_oversold_reversal" data-horizon="15m"></td><td><input type="number" data-algorithm-weight="kline_oversold_reversal" data-horizon="1h"></td><td>RSI 从极端超卖区上穿确认</td></tr>
                </tbody>
                <tfoot><tr><th>权重合计</th><td id="algorithm-sum-5m">--</td><td id="algorithm-sum-15m">--</td><td id="algorithm-sum-1h">--</td><td>每个周期必须等于 1.000</td></tr></tfoot>
              </table>
            </div>
            <p id="prediction-algorithm-message" class="algorithm-message" aria-live="polite"></p>
            <footer class="algorithm-actions">
              <span>概率映射、置信度、波动率止盈止损公式保持固定；修改只影响保存后的新预测。</span>
              <button id="prediction-algorithm-defaults" type="button">恢复默认参数</button>
              <button id="prediction-algorithm-save" class="primary" type="submit">保存全局算法</button>
            </footer>
          </form>
        </div>
      </div>
      <div id="prediction-ai-history-modal" class="modal prediction-ai-history-modal hidden" aria-hidden="true">
        <div class="modal-box prediction-ai-history-box" role="dialog" aria-modal="true" aria-labelledby="prediction-ai-history-title">
          <div class="modal-head prediction-ai-history-head">
            <div>
              <strong id="prediction-ai-history-title" class="modal-symbol">AI 历史分析记录</strong>
              <span class="dim">DeepSeek 调优成功与失败审计</span>
            </div>
            <button id="prediction-ai-history-close" type="button">关闭</button>
          </div>
          <div class="prediction-ai-history-table-wrap">
            <table class="prediction-ai-history-table">
              <thead><tr><th>分析时间</th><th>配置版本</th><th>状态</th><th>模型</th><th>样本数</th><th>Token</th><th>优化周期</th><th>分析摘要</th><th>操作</th></tr></thead>
              <tbody id="prediction-ai-history-body"><tr><td colspan="9" class="ai-history-empty">点击后加载历史分析记录</td></tr></tbody>
            </table>
          </div>
          <footer id="prediction-ai-history-summary" class="prediction-ai-history-summary">最多显示最近 50 条记录</footer>
        </div>
      </div>
      <div id="prediction-ai-trace-modal" class="modal prediction-ai-trace-modal hidden" aria-hidden="true">
        <div class="modal-box prediction-ai-trace-box" role="dialog" aria-modal="true" aria-labelledby="prediction-ai-trace-title">
          <div class="modal-head prediction-ai-trace-head">
            <div>
              <strong id="prediction-ai-trace-title" class="modal-symbol">DeepSeek 提示词与推理摘要</strong>
              <span id="prediction-ai-trace-version" class="dim">尚无 AI 优化记录</span>
            </div>
            <button id="prediction-ai-trace-close" type="button">关闭</button>
          </div>
          <div id="prediction-ai-trace-content" class="prediction-ai-trace-content">
            <section class="prediction-ai-trace-notice">这里展示可审计的模型推理摘要，不展示原始隐藏思维链。提示词已排除 API Key 和隐藏验证集。</section>
            <section class="prediction-ai-trace-section prediction-ai-history-section">
              <h4>数据库版本历史分析（实际投喂）</h4>
              <div id="prediction-ai-history-analysis" class="prediction-ai-history-analysis"></div>
            </section>
            <section class="prediction-ai-trace-section">
              <h4>DeepSeek 推理摘要</h4>
              <ol id="prediction-ai-reasoning-steps"></ol>
            </section>
            <section class="prediction-ai-trace-section">
              <h4>系统提示词</h4>
              <pre id="prediction-ai-system-prompt"></pre>
            </section>
            <section class="prediction-ai-trace-section">
              <h4>用户提示词（实际提交 JSON）</h4>
              <pre id="prediction-ai-user-prompt"></pre>
            </section>
            <section class="prediction-ai-trace-section">
              <h4>请求参数</h4>
              <pre id="prediction-ai-request-options"></pre>
            </section>
            <section class="prediction-ai-trace-section">
              <h4>DeepSeek 原始返回</h4>
              <pre id="prediction-ai-raw-output"></pre>
            </section>
            <section class="prediction-ai-trace-section">
              <h4>服务端安全校正</h4>
              <pre id="prediction-ai-normalization"></pre>
            </section>
          </div>
        </div>
      </div>
      <div id="watchlist-import" class="modal hidden">
        <div class="modal-box watchlist-import-box" role="dialog" aria-modal="true" aria-labelledby="watchlist-import-title">
          <div class="modal-head">
            <div><strong id="watchlist-import-title" class="modal-symbol">维护本地自选</strong></div>
            <button id="watchlist-import-close" type="button">关闭</button>
          </div>
          <p class="watchlist-import-note">币安官方 API 不开放账户自选读取。已连接账户会在持仓或未成交订单刷新成功后，自动将相关合约加入本地自选并置顶；其他合约可从币安自选中复制代码粘贴到这里，支持代码、空格、逗号或换行分隔。</p>
          <label class="watchlist-import-field" for="watchlist-import-input">合约代码</label>
          <textarea id="watchlist-import-input" rows="8" spellcheck="false" placeholder="例如：&#10;AAPL&#10;TSLAUSDT&#10;NVDA, SNOW"></textarea>
          <div id="watchlist-import-preview" class="watchlist-import-preview"></div>
          <p id="watchlist-import-message" class="watchlist-import-message" aria-live="polite"></p>
          <div class="watchlist-import-actions">
            <button id="watchlist-import-merge" class="primary" type="button">追加导入并置顶</button>
            <button id="watchlist-import-replace" type="button">用输入内容覆盖</button>
          </div>
        </div>
      </div>`;
  }

  q(selector) {
    return this.shadowRoot.querySelector(selector);
  }

  qa(selector) {
    return this.shadowRoot.querySelectorAll(selector);
  }

  async api(path, options = {}) {
    if (typeof window.quantdeskApi !== "function") throw new Error("认证服务尚未就绪");
    return window.quantdeskApi(`/api/v2/monitor${path}`, options);
  }

  start() {
    if (this.running) return;
    this.running = true;
    this.refreshAll();
    this.timers.push(setInterval(() => this.pollOverview(), 2000));
    this.timers.push(setInterval(() => this.pollAlerts(), 5000));
    this.timers.push(setInterval(() => this.pollNews(), 30000));
    this.timers.push(setInterval(() => this.pollIntelligence(), 15000));
    this.timers.push(setInterval(() => this.pollUsMarketStatus(), 30000));
    this.timers.push(setInterval(() => this.pollUsQuotes(), 5000));
    this.timers.push(setInterval(() => this.updateClock(), 1000));
    this.updateClock();
  }

  pause() {
    this.running = false;
    this.timers.forEach((timer) => clearInterval(timer));
    this.timers = [];
    this.stopNewsAutoScroll();
    this.stopModalMarketStream();
  }

  async refreshAll() {
    await Promise.allSettled([
      this.pollOverview(), this.pollAlerts(), this.pollNews(), this.pollIntelligence(),
      this.pollUsMarketStatus(), this.pollUsQuotes(),
    ]);
  }

  async pollUsMarketStatus() {
    const badge = this.q("#us-market-state");
    try {
      if (typeof window.quantdeskApi !== "function") throw new Error("认证服务尚未就绪");
      const status = await window.quantdeskApi("/api/v2/market/us/status");
      this.state.usMarketStatus = status;
      if (!status.configured) {
        badge.textContent = "美股状态未配置";
        badge.className = "badge stale";
        badge.title = "服务器尚未配置 Finnhub API Key";
        this.renderUsMarketSummary();
        return;
      }
      if (!status.available) {
        badge.textContent = "美股状态异常";
        badge.className = "badge err";
        badge.title = `Finnhub：${status.error_category || "upstream"}`;
        this.renderUsMarketSummary();
        return;
      }
      const labels = {
        "pre-market": "美股盘前",
        regular: "美股开盘",
        "post-market": "美股盘后",
      };
      const label = status.holiday
        ? `美股休市 · ${status.holiday}`
        : (labels[status.session] || "美股休市");
      badge.textContent = `${label}${status.stale ? " · 延迟" : ""}`;
      badge.className = status.stale ? "badge stale" : (status.session === "regular" ? "badge ok" : "badge");
      badge.title = `Finnhub · ${status.timezone || "America/New_York"}`;
      this.renderUsMarketSummary();
    } catch (_) {
      this.state.usMarketStatus = null;
      badge.textContent = "美股状态连接失败";
      badge.className = "badge err";
      badge.title = "无法读取服务器的美股市场状态";
      this.renderUsMarketSummary();
    }
  }

  switchMarket(market) {
    if (!['binance', 'finnhub'].includes(market)) return;
    this.state.activeMarket = market;
    const isFinnhub = market === "finnhub";
    this.q("#binance-market-view").classList.toggle("hidden", isFinnhub);
    this.q("#finnhub-market-view").classList.toggle("hidden", !isFinnhub);
    this.q("#engine-state").classList.toggle("hidden", isFinnhub);
    this.q("#us-market-state").classList.toggle("hidden", !isFinnhub);
    this.q(".monitor-logo small").textContent = isFinnhub
      ? "Finnhub 美股现货"
      : "币安 TradFi 合约监控";
    this.qa(".market-tab").forEach((button) => {
      const active = button.dataset.market === market;
      button.classList.toggle("active", active);
      button.setAttribute("aria-selected", String(active));
    });
    if (isFinnhub) {
      this.renderUsMarketSummary();
      this.renderUsGrid();
    }
  }

  async pollUsQuotes() {
    try {
      if (typeof window.quantdeskApi !== "function") throw new Error("认证服务尚未就绪");
      const data = await window.quantdeskApi("/api/v2/market/us/quotes");
      const previousPrices = new Map(
        this.state.usQuotes.map((item) => [item.symbol, Number(item.price)]),
      );
      this.state.usQuoteMeta = data;
      this.state.usQuotes = Array.isArray(data.quotes) ? data.quotes : [];
      this.state.usQuotes.forEach((item) => {
        const previous = previousPrices.get(item.symbol);
        const current = Number(item.price);
        if (Number.isFinite(previous) && Number.isFinite(current) && current !== previous) {
          item.priceMove = current > previous ? "up" : "down";
        }
      });
      this.renderUsMarketSummary();
      this.renderUsGrid();
    } catch (error) {
      this.state.usQuoteMeta = { configured: false, error: error.message || "连接失败" };
      this.renderUsMarketSummary();
      this.renderUsGrid();
    }
  }

  renderUsMarketSummary() {
    const element = this.q("#us-market-summary");
    if (!element) return;
    const meta = this.state.usQuoteMeta;
    const status = this.state.usMarketStatus;
    if (!meta?.configured) {
      element.textContent = "Finnhub 尚未配置或连接失败；这里不会回退显示 Binance 合约数据。";
      return;
    }
    const sessionLabels = {
      "pre-market": "盘前交易",
      regular: "正常交易",
      "post-market": "盘后交易",
    };
    const session = status?.holiday
      ? `休市 · ${status.holiday}`
      : (sessionLabels[status?.session] || "休市");
    const feed = meta.stream_connected ? "WebSocket 实时成交" : "REST 低频快照";
    element.textContent = `美国市场：${session} · ${feed} · 已有报价 ${Number(meta.available) || 0}/${Number(meta.total) || 0}`;
  }

  renderUsGrid() {
    const grid = this.q("#us-stock-grid");
    if (!grid) return;
    const keyword = this.q("#us-search").value.trim().toUpperCase();
    const sort = this.q("#us-sort").value;
    let items = this.state.usQuotes.filter((item) => !keyword || item.symbol.includes(keyword));
    items.sort((left, right) => {
      if (left.available !== right.available) return right.available ? 1 : -1;
      if (sort === "alpha") return left.symbol.localeCompare(right.symbol);
      if (sort === "price") return (right.price ?? -1) - (left.price ?? -1);
      return (right.change_percent ?? -999) - (left.change_percent ?? -999);
    });
    this.q("#us-symbol-count").textContent = `${items.length}/${this.state.usQuotes.length}`;
    if (!this.state.usQuoteMeta?.configured) {
      grid.innerHTML = '<div class="empty us-empty"><strong>Finnhub 美股行情未配置</strong><span>请检查服务器 FINNHUB_API_KEY。币安合约数据不会显示在这个市场页。</span></div>';
      return;
    }
    grid.innerHTML = items.map((item) => {
      const pctClass = item.change_percent == null ? "dim" : item.change_percent > 0 ? "up" : item.change_percent < 0 ? "down" : "flat";
      const moveClass = item.priceMove === "up" ? "tick-up" : item.priceMove === "down" ? "tick-down" : "";
      const sourceLabel = item.live ? "实时成交" : item.available ? "报价快照" : "等待数据";
      const timeLabel = item.source_timestamp
        ? new Date(Number(item.source_timestamp) * 1000).toLocaleTimeString("zh-CN", { hour12: false })
        : "--";
      if (!item.available) {
        return `<article class="contract-card us-stock-card unavailable">
          <header class="band-card-head"><div class="symbol">${this.escape(item.symbol)}</div><span class="us-source">Finnhub</span></header>
          <div class="us-waiting">等待 Finnhub 报价</div>
          <footer class="us-card-foot"><span>${this.escape(item.error_category || "排队采集中")}</span><span>非 Binance 数据</span></footer>
        </article>`;
      }
      return `<article class="contract-card us-stock-card ${moveClass}">
        <header class="band-card-head">
          <div class="symbol">${this.escape(item.symbol)}</div><span class="us-source">Finnhub</span>
          <div class="price">${this.formatPrice(item.price)}</div>
          <div class="pct ${pctClass}">${this.formatPercent(item.change_percent)}</div>
        </header>
        <div class="us-session-row"><strong>${sourceLabel}</strong><span>${item.stale ? "数据延迟" : timeLabel}</span></div>
        <div class="us-ohlc">
          <span><em>开盘</em><b>${this.formatPrice(item.day_open)}</b></span>
          <span><em>最高</em><b>${this.formatPrice(item.day_high)}</b></span>
          <span><em>最低</em><b>${this.formatPrice(item.day_low)}</b></span>
          <span><em>昨收</em><b>${this.formatPrice(item.previous_close)}</b></span>
        </div>
        <footer class="us-card-foot"><span>US 现货</span><span>${item.live ? "WS" : "REST"}</span></footer>
      </article>`;
    }).join("") || '<div class="empty">没有符合条件的美股代码</div>';
    items.forEach((item) => { item.priceMove = null; });
  }

  bindEvents() {
    this.qa(".market-tab").forEach((button) => {
      button.addEventListener("click", () => this.switchMarket(button.dataset.market));
    });
    ["us-search", "us-sort"].forEach((id) => {
      this.q(`#${id}`).addEventListener("input", () => this.renderUsGrid());
    });
    ["search", "filter"].forEach((id) => {
      this.q(`#${id}`).addEventListener("input", () => this.renderGrid());
    });
    this.q("#contract-grid").addEventListener("click", (event) => {
      const button = event.target.closest("[data-matrix-sort]");
      if (!button) return;
      const key = button.dataset.matrixSort;
      this.state.matrixSort.direction = this.state.matrixSort.key === key
        && this.state.matrixSort.direction === "desc" ? "asc" : "desc";
      this.state.matrixSort.key = key;
      this.renderGrid();
    });
    this.q("#btn-refresh").addEventListener("click", () => this.refreshAll());
    this.q("#btn-prediction-history").addEventListener("click", () => this.openPredictionHistory());
    this.q("#btn-prediction-algorithm").addEventListener("click", () => this.openPredictionAlgorithm());
    this.q("#btn-import-watchlist").addEventListener("click", () => this.openWatchlistImport());
    this.q("#btn-clear-watchlist").addEventListener("click", () => this.clearWatchlist());
    this.q("#btn-sync-positions").addEventListener("click", () => this.syncPositionsToMonitor());
    this.q("#btn-sound").addEventListener("click", (event) => {
      this.state.sound = !this.state.sound;
      event.currentTarget.classList.toggle("on", this.state.sound);
      event.currentTarget.textContent = this.state.sound ? "音效" : "静音";
    });
    this.q("#btn-notify").addEventListener("click", async (event) => {
      if (!("Notification" in window)) return;
      const permission = await Notification.requestPermission();
      this.state.notifyOn = permission === "granted";
      event.currentTarget.classList.toggle("on", this.state.notifyOn);
    });
    this.q("#btn-clear-alerts").addEventListener("click", async () => {
      await this.api("/alerts/read", { method: "POST" });
      await this.pollAlerts();
    });
    this.q("#modal-close").addEventListener("click", () => this.closeModal());
    this.q("#modal").addEventListener("click", (event) => {
      if (event.target === this.q("#modal")) this.closeModal();
    });
    this.qa("[data-modal-section]").forEach((button) => {
      button.addEventListener("click", () => {
        this.activateResearchPage(button, { smooth: true });
      });
    });
    this.qa(".tf-switch button").forEach((button) => {
      button.addEventListener("click", () => {
        this.state.modal.tf = button.dataset.tf;
        this.refreshModal();
      });
    });
    this.qa("[data-chart-overlay]").forEach((button) => {
      button.addEventListener("click", () => {
        const overlay = button.dataset.chartOverlay;
        if (this.state.chart.overlays.has(overlay)) this.state.chart.overlays.delete(overlay);
        else this.state.chart.overlays.add(overlay);
        const enabled = this.state.chart.overlays.has(overlay);
        button.classList.toggle("on", enabled);
        button.setAttribute("aria-pressed", String(enabled));
        this.drawChart();
      });
    });
    this.q('[data-chart-action="reset"]').addEventListener("click", () => this.resetChartViewport());
    const chart = this.q("#chart");
    chart.addEventListener("pointerdown", (event) => this.handleChartPointerDown(event));
    chart.addEventListener("pointermove", (event) => this.handleChartPointerMove(event));
    chart.addEventListener("pointerup", (event) => this.handleChartPointerUp(event));
    chart.addEventListener("pointercancel", (event) => this.handleChartPointerUp(event));
    chart.addEventListener("pointerleave", () => this.handleChartPointerLeave());
    chart.addEventListener("wheel", (event) => this.handleChartWheel(event), { passive: false });
    chart.addEventListener("dblclick", () => this.resetChartViewport());
    chart.addEventListener("keydown", (event) => this.handleChartKeydown(event));
    if (typeof ResizeObserver !== "undefined") {
      this.chartResizeObserver = new ResizeObserver(() => this.drawChart());
      this.chartResizeObserver.observe(this.q("#chart-stage"));
    }
    this.q("#modal-watch").addEventListener("click", () => this.toggleWatchlist());
    this.q("#prediction-history-close").addEventListener("click", () => this.closePredictionHistory());
    this.q("#prediction-history-modal").addEventListener("click", (event) => {
      if (event.target === this.q("#prediction-history-modal")) this.closePredictionHistory();
    });
    this.q("#prediction-history-body").addEventListener("click", (event) => {
      const button = event.target.closest("[data-prediction-config]");
      if (button) this.openHistoricalPredictionConfig(button.dataset.predictionConfig);
    });
    this.q("#prediction-config-close").addEventListener("click", () => this.closeHistoricalPredictionConfig());
    this.q("#prediction-config-modal").addEventListener("click", (event) => {
      if (event.target === this.q("#prediction-config-modal")) this.closeHistoricalPredictionConfig();
    });
    this.qa("[data-history-page]").forEach((button) => {
      button.addEventListener("click", () => this.changePredictionHistoryPage(button.dataset.historyPage));
    });
    this.qa("[data-history-filter]").forEach((button) => {
      button.addEventListener("click", () => this.setPredictionHistoryFilter(
        button.dataset.historyFilter,
        button.dataset.filterValue,
      ));
    });
    this.q("#history-time-apply").addEventListener("click", () => this.applyPredictionHistoryTimeRange());
    this.q("#history-time-recent").addEventListener("click", () => this.setRecentPredictionHistoryRange());
    this.q("#history-time-all").addEventListener("click", () => this.clearPredictionHistoryTimeRange());
    this.q("#prediction-algorithm-close").addEventListener("click", () => this.closePredictionAlgorithm());
    this.q("#prediction-algorithm-ai-trace").addEventListener("click", () => this.openPredictionAiTrace());
    this.q("#prediction-algorithm-ai-history").addEventListener("click", () => this.openPredictionAiHistory());
    this.q("#prediction-algorithm-optimize").addEventListener("click", () => this.optimizePredictionAlgorithm());
    this.q("#prediction-algorithm-modal").addEventListener("click", (event) => {
      if (event.target === this.q("#prediction-algorithm-modal")) this.closePredictionAlgorithm();
    });
    this.q("#prediction-ai-trace-close").addEventListener("click", () => this.closePredictionAiTrace());
    this.q("#prediction-ai-trace-modal").addEventListener("click", (event) => {
      if (event.target === this.q("#prediction-ai-trace-modal")) this.closePredictionAiTrace();
    });
    this.q("#prediction-ai-history-close").addEventListener("click", () => this.closePredictionAiHistory());
    this.q("#prediction-ai-history-modal").addEventListener("click", (event) => {
      if (event.target === this.q("#prediction-ai-history-modal")) this.closePredictionAiHistory();
    });
    this.q("#prediction-ai-history-body").addEventListener("click", (event) => {
      const button = event.target.closest("[data-ai-history-audit]");
      if (button) this.openPredictionAiHistoryDetail(button.dataset.aiHistoryAudit, button);
    });
    this.q("#prediction-algorithm-form").addEventListener("input", () => this.renderAlgorithmWeightSums());
    this.q("#prediction-algorithm-form").addEventListener("click", (event) => {
      const button = event.target.closest("[data-algorithm-enabled]");
      if (button) this.toggleAlgorithmFeature(button);
    });
    this.q("#prediction-algorithm-defaults").addEventListener("click", () => this.restoreDefaultAlgorithm());
    this.q("#prediction-algorithm-form").addEventListener("submit", (event) => this.savePredictionAlgorithm(event));
    this.q("#watchlist-import-close").addEventListener("click", () => this.closeWatchlistImport());
    this.q("#watchlist-import").addEventListener("click", (event) => {
      if (event.target === this.q("#watchlist-import")) this.closeWatchlistImport();
    });
    this.q("#watchlist-import-input").addEventListener("input", () => this.renderWatchlistImportPreview());
    this.q("#watchlist-import-merge").addEventListener("click", () => this.importWatchlist("merge"));
    this.q("#watchlist-import-replace").addEventListener("click", () => this.importWatchlist("replace"));
    this.q("#opportunity-detail").addEventListener("click", (event) => {
      const button = event.target.closest("[data-opportunity-action]");
      if (!button) return;
      if (button.dataset.opportunityAction === "shadow") this.createShadowIntent();
      else this.setOpportunityPreference(button.dataset.opportunityAction);
    });
    this.q("#modal-indicator-section").addEventListener("click", (event) => {
      const button = event.target.closest("[data-strategy-indicator]");
      if (!button) return;
      this.state.modal.selectedIndicator = button.dataset.strategyIndicator;
      this.renderStrategyIndicatorSelection();
    });
    this.shadowRoot.addEventListener("keydown", (event) => {
      if (event.key === "Escape" && !this.q("#modal").classList.contains("hidden")) this.closeModal();
    });
  }

  updateClock() {
    this.q("#monitor-clock").textContent = new Date().toLocaleTimeString("zh-CN", { hour12: false });
  }

  escape(value) {
    return String(value ?? "").replace(/[&<>'"]/g, (character) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;",
    })[character]);
  }

  safeUrl(value) {
    try {
      const url = new URL(value, window.location.origin);
      return ["http:", "https:"].includes(url.protocol) ? url.href : "#";
    } catch (_) {
      return "#";
    }
  }

  parseWatchlistInput(value) {
    const aliases = new Map();
    this.state.overview.forEach((item) => {
      const symbol = String(item.symbol || "").trim().toUpperCase();
      if (!symbol) return;
      aliases.set(symbol, symbol);
      if (symbol.endsWith("USDT")) aliases.set(symbol.slice(0, -4), symbol);
    });
    const tokens = String(value || "")
      .toUpperCase()
      .replace(/[，；、]/g, ",")
      .split(/[\s,;]+/)
      .map((item) => item.trim())
      .filter(Boolean);
    const symbols = [];
    const invalid = [];
    tokens.forEach((token) => {
      const symbol = aliases.get(token);
      if (!symbol) invalid.push(token);
      else if (!symbols.includes(symbol)) symbols.push(symbol);
    });
    return { symbols, invalid: [...new Set(invalid)] };
  }

  updateWatchlistUi() {
    const importButton = this.q("#btn-import-watchlist");
    if (importButton) importButton.textContent = `维护自选 · ${this.state.watchlist.size}`;
    const clearButton = this.q("#btn-clear-watchlist");
    if (!clearButton) return;
    clearButton.disabled = this.state.watchlist.size === 0;
    clearButton.textContent = `清除自选 · ${this.state.watchlist.size}`;
  }

  async clearWatchlist() {
    const count = this.state.watchlist.size;
    if (!count || !window.confirm(`确认清除全部 ${count} 个自选合约？`)) return;
    try {
      await this.saveWatchlist(new Set());
      this.showError("");
    } catch (error) {
      this.showError(error.message || "清除自选失败，请稍后重试");
    }
  }

  async syncPositionsToMonitor() {
    if (typeof window.quantdeskApi !== "function") return;
    const button = this.q("#btn-sync-positions");
    const initialLabel = "同步持仓到监控";
    button.disabled = true;
    button.textContent = "正在同步…";
    try {
      const account = await window.quantdeskApi("/api/v2/me/binance-account");
      if (!account.configured) throw new Error("请先在账户页面连接币安 API");
      if (!account.connected) throw new Error("币安账户连接失败，请检查 API 权限与网络");
      const positions = Array.isArray(account.positions) ? account.positions : [];
      await this.pollOverview();
      button.textContent = positions.length ? `已同步 ${positions.length} 个持仓` : "当前无持仓";
      this.showError("");
      window.setTimeout(() => {
        button.textContent = initialLabel;
        button.disabled = false;
      }, 2200);
    } catch (error) {
      button.textContent = initialLabel;
      button.disabled = false;
      this.showError(error.message || "持仓合约同步失败，请稍后重试");
    }
  }

  openWatchlistImport() {
    this.q("#watchlist-import-input").value = "";
    this.q("#watchlist-import-message").textContent = "";
    this.q("#watchlist-import").classList.remove("hidden");
    this.renderWatchlistImportPreview();
    this.q("#watchlist-import-input").focus();
  }

  closeWatchlistImport() {
    this.q("#watchlist-import").classList.add("hidden");
  }

  renderWatchlistImportPreview() {
    const parsed = this.parseWatchlistInput(this.q("#watchlist-import-input").value);
    const current = [...this.state.watchlist].sort();
    const inputText = parsed.symbols.length ? `识别 ${parsed.symbols.length} 个：${parsed.symbols.join("、")}` : "尚未识别输入合约";
    const invalidText = parsed.invalid.length ? `；未识别：${parsed.invalid.join("、")}` : "";
    const currentText = current.length ? `当前自选 ${current.length} 个：${current.join("、")}` : "当前本地自选为空";
    this.q("#watchlist-import-preview").textContent = `${inputText}${invalidText}\n${currentText}`;
  }

  async saveWatchlist(symbols) {
    const saved = await this.api("/watchlist", {
      method: "PUT",
      body: JSON.stringify({ symbols: [...symbols] }),
    });
    this.state.watchlist = new Set(Array.isArray(saved) ? saved : [...symbols]);
    this.state.overview.forEach((item) => { item.watch = this.state.watchlist.has(item.symbol); });
    this.updateWatchlistUi();
    this.renderGrid();
  }

  async importWatchlist(mode) {
    const message = this.q("#watchlist-import-message");
    const parsed = this.parseWatchlistInput(this.q("#watchlist-import-input").value);
    if (!parsed.symbols.length) {
      message.textContent = "没有识别到可导入的监控合约。";
      return;
    }
    if (parsed.invalid.length) {
      message.textContent = `请先修正未识别代码：${parsed.invalid.join("、")}`;
      return;
    }
    const next = mode === "replace"
      ? new Set(parsed.symbols)
      : new Set([...this.state.watchlist, ...parsed.symbols]);
    message.textContent = "正在保存…";
    try {
      await this.saveWatchlist(next);
      this.showError("");
      this.closeWatchlistImport();
    } catch (error) {
      message.textContent = `自选保存失败：${error.message || "请稍后重试"}`;
    }
  }

  formatPrice(value) {
    if (value == null) return "--";
    return value >= 100 ? value.toFixed(2) : value >= 1 ? value.toFixed(3) : value.toFixed(5);
  }

  formatPercent(value) {
    if (value == null) return "--";
    return `${value >= 0 ? "+" : ""}${value.toFixed(2)}%`;
  }

  timeString(timestamp) {
    return new Date(timestamp * 1000).toLocaleString("zh-CN", { hour12: false });
  }

  barTimeString(timestamp) {
    if (!timestamp) return "--";
    const milliseconds = Number(timestamp) >= 100000000000 ? Number(timestamp) : Number(timestamp) * 1000;
    return new Date(milliseconds).toLocaleString("zh-CN", { hour12: false });
  }

  underlyingRelationLabel(relation) {
    return ({ direct: "直接映射", alias: "别名映射", benchmark: "参考标的", unlisted: "未上市" })[relation] || "现货映射";
  }

  underlyingMarketStateLabel(state) {
    return ({
      pre_market: "盘前",
      regular: "交易中",
      after_hours: "盘后",
      closed: "已收盘",
      unknown: "状态未知",
      unavailable: "暂无行情",
    })[state] || "状态未知";
  }

  underlyingAlignmentLabel(status) {
    return ({
      aligned: "时间已对齐",
      lagging: "时间未对齐",
      stale: "现货已延迟",
      closed: "现货已收盘",
      unavailable: "无法对齐",
    })[status] || "无法对齐";
  }

  spreadAlertLabel(level) {
    return ({ strong: "强价差提醒", watch: "价差关注", normal: "价差正常", disabled: "提醒暂停" })[level] || "提醒暂停";
  }

  formatAlignmentDelta(value) {
    const milliseconds = Number(value);
    if (!Number.isFinite(milliseconds)) return "";
    if (milliseconds < 1_000) return `${Math.round(milliseconds)}ms`;
    if (milliseconds < 60_000) return `${(milliseconds / 1_000).toFixed(1)}s`;
    return `${(milliseconds / 60_000).toFixed(1)}m`;
  }

  opportunityTone(direction) {
    return direction === "long" ? "strong-up" : direction === "short" ? "strong-down" : "neutral";
  }

  opportunityLabel(direction) {
    return { long: "多头机会", short: "空头机会", neutral: "中性观察" }[direction] || "暂无机会";
  }

  tone(score) {
    if (score == null) return "neutral";
    if (score >= 60) return "strong-up";
    if (score <= -60) return "strong-down";
    if (score > 15) return "mild-up";
    if (score < -15) return "mild-down";
    return "neutral";
  }

  signalTone(score) {
    if (score == null || Math.abs(score) < 40) return "neutral";
    return score > 0 ? "strong-up" : "strong-down";
  }

  signalLabel(score) {
    if (score == null) return "数据不足";
    if (score >= 75) return "强烈看多";
    if (score >= 40) return "看多";
    if (score <= -75) return "强烈看空";
    if (score <= -40) return "看空";
    return "中性观望";
  }

  aiConclusion(item) {
    const battle = item.battle?.["5m"];
    if (!battle || battle.state === "data_insufficient" || battle.stale) {
      return { tone: "neutral", label: "数据不足", confidence: null, rank: 0, edge: 0 };
    }
    const tone = ["long", "short"].includes(battle.result) ? battle.result : "neutral";
    const confidence = Number(battle.confidence_score);
    return {
      tone,
      label: { long: "看多", short: "看空", neutral: "观望" }[tone],
      confidence: Number.isFinite(confidence) ? Math.round(Math.max(0, Math.min(1, confidence)) * 100) : null,
      rank: { long: 3, neutral: 2, short: 1 }[tone],
      edge: Number(battle.long || 0) - Number(battle.short || 0),
    };
  }

  formatCompact(value) {
    const number = Number(value);
    if (!Number.isFinite(number)) return "--";
    if (number >= 1_000_000_000) return `${(number / 1_000_000_000).toFixed(2)}B`;
    if (number >= 1_000_000) return `${(number / 1_000_000).toFixed(2)}M`;
    if (number >= 1_000) return `${(number / 1_000).toFixed(1)}K`;
    return number.toFixed(0);
  }

  formatScore(value) {
    const number = Number(value);
    return Number.isFinite(number) ? `${number > 0 ? "+" : ""}${number.toFixed(0)}` : "--";
  }

  matrixSortMark(key) {
    if (this.state.matrixSort.key !== key) return "↕";
    return this.state.matrixSort.direction === "asc" ? "↑" : "↓";
  }

  matrixSortValue(item, key) {
    const ai = this.aiConclusion(item);
    if (key === "ai") return ai.rank;
    if (key === "confidence") return ai.confidence ?? -1;
    if (key === "watch") return item.watch ? 1 : 0;
    if (key === "symbol") return item.symbol;
    if (key === "basis") {
      const basis = Number(item.underlying_quote?.basis_bps);
      return item.underlying_quote?.basis_comparable && Number.isFinite(basis) ? basis : null;
    }
    if (key === "price") return Number(item.price) || 0;
    if (key === "pct2m") return Number(item.pct_2m) || 0;
    if (key === "pct5m") return Number(item.pct_5m) || 0;
    if (key === "pct10m") return Number(item.pct_10m) || 0;
    if (key === "pct24h") return Number(item.pct_24h) || 0;
    if (key === "score") return Number(item.score) || 0;
    if (key === "battle") return ai.edge;
    if (key === "book") return Number(item.book_imbalance) || 0;
    if (key === "force") return Number(item.green_flashes_30m || 0) - Number(item.red_flashes_30m || 0);
    if (key === "score15m") return Number(item.tf_scores?.["15m"]) || 0;
    if (key === "score1h") return Number(item.tf_scores?.["1h"]) || 0;
    if (key === "opportunity") return Number(item.opportunity?.quality_score) || -1;
    if (key === "volume") return Number(item.quote_volume) || 0;
    return 0;
  }

  battleLabel(result, state) {
    if (state === "data_insufficient") return "数据不足";
    return { long: "多头占优", short: "空头占优", neutral: "多空均衡" }[result] || "等待预测";
  }

  battleTone(result, state) {
    if (state === "data_insufficient") return "neutral";
    return result === "long" ? "long" : result === "short" ? "short" : "neutral";
  }

  reasonLabel(code) {
    return ({
      AGGRESSIVE_FLOW: "主动成交",
      BOOK_IMBALANCE: "100档订单池",
      BOOK_IMBALANCE_5: "近5档压力",
      VELOCITY: "价格速度",
      FLASH_IMBALANCE: "30分钟力量",
      TAKER_FLOW: "主动买卖量",
      PRICE_OI_IMPULSE: "价格与持仓量",
      TREND: "趋势结构",
      CROWDING_RISK: "持仓拥挤风险",
      DATA_INSUFFICIENT: "数据不足",
    })[code] || code;
  }

  showError(message = "") {
    const banner = this.q("#error-banner");
    banner.textContent = message;
    banner.classList.toggle("hidden", !message);
  }

  renderGrid() {
    const keyword = this.q("#search").value.trim().toUpperCase();
    const filter = this.q("#filter").value;
    let items = this.state.overview.filter((item) => !keyword || item.symbol.includes(keyword));
    if (filter === "mine") items = items.filter((item) => item.watch);
    if (["long", "short", "neutral"].includes(filter)) {
      items = items.filter((item) => this.aiConclusion(item).tone === filter);
    }
    items.sort((left, right) => {
      const pinnedComparison = Number(Boolean(right.watch)) - Number(Boolean(left.watch));
      if (pinnedComparison !== 0) return pinnedComparison;
      const leftValue = this.matrixSortValue(left, this.state.matrixSort.key);
      const rightValue = this.matrixSortValue(right, this.state.matrixSort.key);
      const leftMissing = leftValue == null;
      const rightMissing = rightValue == null;
      if (leftMissing !== rightMissing) return leftMissing ? 1 : -1;
      const comparison = typeof leftValue === "string"
        ? leftValue.localeCompare(rightValue, "zh-Hans")
        : leftValue - rightValue;
      if (comparison !== 0) return this.state.matrixSort.direction === "asc" ? comparison : -comparison;
      return left.symbol.localeCompare(right.symbol);
    });
    this.q("#sym-count").textContent = `${items.length}/${this.state.overview.length}`;
    const head = [
      ["ai", "AI 结论"], ["confidence", "信心"], ["watch", "持仓/自选"], ["symbol", "合约"],
      ["price", "最新"], ["pct2m", "2m 涨跌"], ["pct5m", "5m 涨跌"], ["pct10m", "10m 涨跌"], ["pct24h", "24h"], ["score", "综合分"],
      ["battle", "5m 博弈"], ["book", "100档订单池"], ["force", "30m 力量"], ["score15m", "15m 分"], ["score1h", "1h 分"],
      ["opportunity", "机会"], ["volume", "成交额"],
    ].map(([key, label]) => key === "symbol"
      ? `<th class="matrix-symbol-head"><div class="matrix-dual-sort"><button type="button" data-matrix-sort="symbol" aria-label="按合约排序">合约<i aria-hidden="true">${this.matrixSortMark("symbol")}</i></button><button type="button" data-matrix-sort="basis" aria-label="按最新差价排序">差价<i aria-hidden="true">${this.matrixSortMark("basis")}</i></button></div></th>`
      : `<th><button type="button" data-matrix-sort="${key}" aria-label="按${label}排序">${label}<i aria-hidden="true">${this.matrixSortMark(key)}</i></button></th>`).join("");
    const rows = items.map((item) => {
      const ai = this.aiConclusion(item);
      const opportunity = item.opportunity;
      const longForce = Math.max(0, Number(item.green_flashes_30m) || 0);
      const shortForce = Math.max(0, Number(item.red_flashes_30m) || 0);
      const totalForce = longForce + shortForce;
      const longForceWidth = totalForce ? longForce / totalForce * 100 : 0;
      const shortForceWidth = totalForce ? 100 - longForceWidth : 0;
      const bidPool = Number(item.bid_depth_notional);
      const askPool = Number(item.ask_depth_notional);
      const totalPool = bidPool + askPool;
      const depthLevels = Number(item.depth_levels) || 0;
      const bidPoolWidth = totalPool > 0 ? bidPool / totalPool * 100 : 0;
      const bookAvailable = Number.isFinite(bidPool) && Number.isFinite(askPool) && totalPool > 0;
      const bookTitle = bookAvailable
        ? `100档订单池（${depthLevels}档）：买方 ${this.formatCompact(bidPool)} / 卖方 ${this.formatCompact(askPool)}；近5档失衡 ${this.formatPercent(Number(item.book_imbalance_5) * 100)}`
        : "100档订单池数据采集中";
      const pct2mClass = item.pct_2m == null ? "dim" : item.pct_2m > 0 ? "up" : item.pct_2m < 0 ? "down" : "flat";
      const pct5mClass = item.pct_5m == null ? "dim" : item.pct_5m > 0 ? "up" : item.pct_5m < 0 ? "down" : "flat";
      const pct10mClass = item.pct_10m == null ? "dim" : item.pct_10m > 0 ? "up" : item.pct_10m < 0 ? "down" : "flat";
      const pct24hClass = item.pct_24h == null ? "dim" : item.pct_24h > 0 ? "up" : item.pct_24h < 0 ? "down" : "flat";
      const opportunityText = opportunity
        ? `${this.opportunityLabel(opportunity.direction)} ${Number(opportunity.quality_score).toFixed(0)}`
        : "--";
      const underlying = item.underlying_quote || {};
      const underlyingChange = underlying.change_pct == null ? null : Number(underlying.change_pct);
      const underlyingChangeClass = !Number.isFinite(underlyingChange)
        ? "flat"
        : underlyingChange > 0 ? "up" : underlyingChange < 0 ? "down" : "flat";
      const basis = Number(underlying.basis_bps);
      const basisToneClass = Number.isFinite(basis)
        ? basis > 0 ? "premium" : basis < 0 ? "discount" : "flat"
        : "flat";
      const basisText = underlying.basis_comparable && Number.isFinite(basis)
        ? `最新差价 ${basis >= 0 ? "+" : ""}${basis.toFixed(1)} bps`
        : "";
      const spreadAlert = underlying.spread_alert || (underlying.basis_comparable ? "normal" : "disabled");
      const spreadAlertClass = spreadAlert === "strong" ? "strong" : spreadAlert === "watch" ? "watch" : "normal";
      const alignmentText = `${this.underlyingAlignmentLabel(underlying.alignment_status)} ${this.formatAlignmentDelta(underlying.alignment_delta_ms)}`.trim();
      const underlyingSymbol = underlying.quote_symbol || item.symbol.replace(/(?:USDT|USD1)$/, "");
      const underlyingStatus = underlying.status === "ok"
        ? this.underlyingMarketStateLabel(underlying.market_state)
        : underlying.status === "unsupported" ? "暂无公开行情" : underlying.status === "stale" ? "行情延迟" : "行情采集中";
      const underlyingTime = underlying.market_time_ms ? this.barTimeString(underlying.market_time_ms) : "--";
      const moveClass = item.priceMove === "up" ? "tick-up" : item.priceMove === "down" ? "tick-down" : "";
      const mainRow = `<tr class="matrix-row matrix-${ai.tone} ${moveClass}" data-symbol="${this.escape(item.symbol)}">
        <td class="matrix-ai"><span>${ai.label}</span></td>
        <td class="matrix-confidence" title="5 分钟 AI 结论信心"><svg viewBox="0 0 100 6" preserveAspectRatio="none" aria-hidden="true"><rect class="base" width="100" height="6"></rect><rect class="fill" width="${ai.confidence || 0}" height="6"></rect></svg><b>${ai.confidence == null ? "--" : `${ai.confidence}%`}</b></td>
        <td><button class="matrix-watch ${item.watch ? "on" : ""}" type="button" data-watch-symbol="${this.escape(item.symbol)}" aria-label="${item.watch ? "取消自选" : "加入自选"}">${item.watch ? "★" : "☆"}</button></td>
        <td class="matrix-symbol"><strong>${this.escape(item.symbol.replace(/(?:USDT|USD1)$/, ""))}</strong>${item.trending ? '<em aria-label="热度上升">🔥</em>' : ""}<small>${this.escape(item.annotation || item.underlying || "标的")}</small></td>
        <td class="matrix-price">${this.formatPrice(item.price)}</td>
        <td class="${pct2mClass}">${this.formatPercent(item.pct_2m)}</td>
        <td class="${pct5mClass}">${this.formatPercent(item.pct_5m)}</td>
        <td class="${pct10mClass}">${this.formatPercent(item.pct_10m)}</td>
        <td class="${pct24hClass}">${this.formatPercent(item.pct_24h)}</td>
        <td class="score ${this.signalTone(item.score)}">${this.formatScore(item.score)}</td>
        <td class="matrix-battle" title="5 分钟多空博弈概率：多 ${Number(item.battle?.["5m"]?.long || 0).toFixed(1)}%，空 ${Number(item.battle?.["5m"]?.short || 0).toFixed(1)}%">多 ${Number(item.battle?.["5m"]?.long || 0).toFixed(0)} / 空 ${Number(item.battle?.["5m"]?.short || 0).toFixed(0)}</td>
        <td class="matrix-book" title="${this.escape(bookTitle)}">${bookAvailable ? `<small>买/卖</small> <b>${this.formatCompact(bidPool)} / ${this.formatCompact(askPool)}</b><svg viewBox="0 0 100 6" preserveAspectRatio="none" aria-hidden="true"><rect class="long" x="0" width="${bidPoolWidth}" height="6"></rect><rect class="short" x="${bidPoolWidth}" width="${100 - bidPoolWidth}" height="6"></rect></svg>` : "--"}</td>
        <td class="matrix-force" title="最近 30 分钟价格方向高亮：多 ${longForce}，空 ${shortForce}"><svg viewBox="0 0 100 6" preserveAspectRatio="none" aria-hidden="true"><rect class="long" x="0" width="${longForceWidth}" height="6"></rect><rect class="short" x="${longForceWidth}" width="${shortForceWidth}" height="6"></rect></svg><b>${longForce - shortForce > 0 ? "+" : ""}${longForce - shortForce}</b></td>
        <td class="score ${this.signalTone(item.tf_scores?.["15m"])}">${this.formatScore(item.tf_scores?.["15m"])}</td>
        <td class="score ${this.signalTone(item.tf_scores?.["1h"])}">${this.formatScore(item.tf_scores?.["1h"])}</td>
        <td class="matrix-opportunity ${opportunity?.direction || "none"}">${this.escape(opportunityText)}</td>
        <td>${this.formatCompact(item.quote_volume)}</td>
      </tr>`;
      const underlyingRow = `<tr class="underlying-row underlying-${ai.tone}" data-underlying-symbol="${this.escape(item.symbol)}">
        <td class="underlying-kind"><span class="underlying-branch" aria-hidden="true">↳</span>现货</td>
        <td class="underlying-relation-cell"><em class="underlying-relation">${this.escape(this.underlyingRelationLabel(underlying.relation))}</em></td>
        <td></td>
        <td class="underlying-symbol-cell"><div class="underlying-symbol-line"><strong>${this.escape(underlyingSymbol)}</strong><small>${this.escape(underlying.display_name || item.annotation || "对应现货")}</small>${basisText ? `<span class="underlying-inline-basis ${basisToneClass} ${spreadAlertClass}" title="计算公式：(合约最新价 / 现货最新价 - 1) × 10000 bps">${this.escape(basisText)}</span>` : ""}</div></td>
        <td class="underlying-price-cell">${this.formatPrice(underlying.price)}</td>
        <td class="underlying-change-cell ${underlyingChangeClass}">${this.formatPercent(Number.isFinite(underlyingChange) ? underlyingChange : null)}</td>
        <td class="underlying-market-cell" colspan="4"><span class="underlying-state ${underlying.stale ? "stale" : ""}">${this.escape(underlyingStatus)}</span><span>量 ${underlying.volume == null ? "--" : this.formatCompact(underlying.volume)}</span></td>
        <td class="underlying-basis-cell" colspan="3"></td>
        <td class="underlying-alignment-cell" colspan="2" title="合约与现货的行情时间对齐状态">↔ ${this.escape(alignmentText)}</td>
        <td class="underlying-time-cell" colspan="2" title="现货行情时间 ${this.escape(underlyingTime)}">${this.escape(underlying.exchange_name || "--")} · ${this.escape(underlying.currency || "--")} · ${this.escape(underlyingTime)}</td>
      </tr>`;
      return mainRow + underlyingRow;
    }).join("");
    this.q("#contract-grid").innerHTML = rows
      ? `<table class="contract-matrix"><thead><tr>${head}</tr></thead><tbody>${rows}</tbody></table>`
      : `<div class="empty">${filter === "mine" ? "暂无自选合约。可点击 ☆ 添加，或同步持仓合约。" : "没有符合条件的合约"}</div>`;
    this.qa(".matrix-watch").forEach((button) => button.addEventListener("click", async (event) => {
      event.stopPropagation();
      await this.toggleSymbolWatchlist(button.dataset.watchSymbol);
    }));
    this.qa(".matrix-row").forEach((row) => row.addEventListener("click", () => this.openModal(row.dataset.symbol)));
    items.forEach((item) => { item.priceMove = null; });
  }

  async pollOverview() {
    try {
      const [overview, breadth, watchlist] = await Promise.all([
        this.api("/overview"), this.api("/breadth"), this.api("/watchlist"),
      ]);
      const previousPrices = new Map(
        this.state.overview.map((item) => [item.symbol, Number(item.price)]),
      );
      this.state.overview = Array.isArray(overview.items) ? overview.items : [];
      this.state.overview.forEach((item) => {
        const previous = previousPrices.get(item.symbol);
        const current = Number(item.price);
        if (Number.isFinite(previous) && Number.isFinite(current) && current !== previous) {
          item.priceMove = current > previous ? "up" : "down";
        }
      });
      this.state.watchlist = new Set(watchlist);
      this.state.overview.forEach((item) => { item.watch = this.state.watchlist.has(item.symbol); });
      if (previousPrices.size) this.state.overview.forEach((item) => this.maybeNotifySpreadAlert(item));
      this.updateWatchlistUi();
      this.renderGrid();
      const breadthElement = this.q("#breadth");
      const conclusion = String(breadth.conclusion || "数据收集中…");
      if (!breadth.total) {
        breadthElement.textContent = `市场看板：${conclusion}`;
      } else {
        const summary = conclusion.split("（", 1)[0];
        const summaryTone = summary.includes("偏多") ? "bull" : summary.includes("偏空") ? "bear" : "neutral";
        breadthElement.innerHTML = `市场看板：<span class="breadth-summary ${summaryTone}">${this.escape(summary)}</span><span class="breadth-breakdown">（<strong class="bull">${Number(breadth.bull) || 0}多</strong><i>/</i><strong class="bear">${Number(breadth.bear) || 0}空</strong><i>/</i><strong class="neutral">${Number(breadth.neutral) || 0}中性</strong>）</span>`;
      }
      const status = this.q("#engine-state");
      status.textContent = overview.stale ? "● 数据延迟" : "● 运行中";
      status.className = overview.stale ? "badge stale" : "badge ok";
      this.showError("");
    } catch (error) {
      const status = this.q("#engine-state");
      status.textContent = "● 连接失败";
      status.className = "badge err";
      this.showError(error.message || "合约监控数据加载失败");
    }
  }

  async pollIntelligence() {
    try {
      const data = await this.api("/intelligence");
      this.state.intelligence = data;
      const market = data.market_data || {};
      const opportunities = data.opportunities || {};
      const outcomes = data.outcomes || {};
      const shadow = data.shadow_execution || {};
      this.q("#intel-coverage").textContent = `${Number(market.coverage_pct || 0).toFixed(1)}%`;
      this.q("#intel-scanners").textContent = Number(opportunities.scanners || 0);
      this.q("#intel-opportunities").textContent = `${Number(opportunities.active || 0)} 个活跃机会`;
      this.q("#intel-labels").textContent = Number(outcomes.completed || 0).toLocaleString("zh-CN");
      this.q("#intel-pending").textContent = `${Number(outcomes.pending || 0)} 个待完成`;
      this.q("#intel-hit-rate").textContent = outcomes.hit_rate == null
        ? "--" : `${(Number(outcomes.hit_rate) * 100).toFixed(1)}%`;
      this.q("#intel-return").textContent = outcomes.avg_return_bps == null
        ? "完成样本后显示" : `成本后均值 ${Number(outcomes.avg_return_bps).toFixed(1)} bps`;
      this.q("#intel-shadow").textContent = shadow.live_locked
        ? "LOCKED" : `${Number(shadow.filled || 0)} FILLED`;
    } catch (_) {}
  }

  renderAlerts(alerts) {
    this.q("#alerts").innerHTML = alerts.map((alert) => `
      <article class="alert-item ${this.escape(alert.direction)} ${alert.read ? "" : "unread"}" data-symbol="${this.escape(alert.symbol)}">
        <div>${this.escape(alert.message)}</div>
        <div class="time">${this.timeString(alert.ts)}</div>
      </article>`).join("") || '<div class="empty">暂无信号</div>';
    this.qa(".alert-item").forEach((item) => item.addEventListener("click", () => this.openModal(item.dataset.symbol)));
  }

  async pollAlerts() {
    try {
      const alerts = await this.api("/alerts?limit=80");
      this.renderAlerts(alerts);
      const newest = alerts[0];
      if (newest && this.state.lastAlertId && newest.id > this.state.lastAlertId) {
        const fresh = alerts.filter((alert) => alert.id > this.state.lastAlertId).reverse();
        fresh.forEach((alert) => {
          this.beep(alert.direction === "long" ? 980 : 420, alert.direction === "long" ? 2 : 3);
          if (this.state.notifyOn && Notification.permission === "granted") {
            new Notification("QuantDesk 信号", { body: alert.message });
          }
        });
      }
      if (newest) this.state.lastAlertId = newest.id;
    } catch (_) {}
  }

  async pollNews() {
    try {
      const news = await this.api("/news?limit=60");
      this.q("#news-count").textContent = news.length ? `${news.length}条` : "";
      const content = news.map((item) => {
        const stateText = {
          DETECTED: "已发现", PROVENANCE_OK: "来源通过", FACT_VERIFIED: "事实已验证",
          IMPACT_ASSESSED: "影响已评估", CHALLENGED: "已反证", VALIDATED: "多轮通过",
          MARKET_CONFIRMED: "市场确认", REFERENCE_ELIGIBLE: "可供参考", DISPUTED: "存在争议",
          REFUTED: "已证伪", DATA_INSUFFICIENT: "证据不足", EXPIRED: "已失效",
        }[item.verification_state] || "待验证";
        const assessments = (item.assessments || []).map((assessment) => {
          const direction = { long: "偏多", short: "偏空", neutral: "中性", conflicted: "冲突" }[assessment.direction] || "未定";
          const reference = { eligible: "可参考", risk_only: "仅风控", observe: "观察", display_only: "仅展示", blocked: "禁用" }[assessment.reference_status] || "仅展示";
          const tone = assessment.direction === "long" ? "bull" : assessment.direction === "short" ? "bear" : "neutral";
          const truth = assessment.truth_confidence == null ? "--" : `${Math.round(assessment.truth_confidence * 100)}%`;
          const impact = assessment.impact_confidence == null ? "--" : `${Math.round(assessment.impact_confidence * 100)}%`;
          const market = { confirmed: "市场确认", partial: "部分确认", pending: "等待复核", contrary: "市场反向" }[assessment.market_confirmation] || "未验证";
          const counter = (assessment.counterevidence || []).join("；");
          return `<div class="news-assessment ${tone}" title="${this.escape(counter)}">
            <strong>${this.escape(assessment.symbol)} · ${direction}</strong>
            <span>事实 ${truth} / 影响 ${impact} / ${market} / ${reference}</span>
          </div>`;
        }).join("");
        const stateTone = item.verification_state === "REFERENCE_ELIGIBLE" ? "verified" : ["DISPUTED", "REFUTED"].includes(item.verification_state) ? "blocked" : "pending";
        return `<article class="news-item">
          <span class="source">${this.escape(item.source)}${item.source_tier ? ` · ${this.escape(item.source_tier)}级` : ""}</span><span class="time">${this.timeString(item.ts)}</span>
          <span class="verification ${stateTone}">${stateText}</span>
          <div class="news-title"><a href="${this.safeUrl(item.link)}" target="_blank" rel="noopener noreferrer">${this.escape(item.title_zh || item.title)}</a></div>
          ${item.title_zh && item.lang === "en" ? `<div class="time">${this.escape(item.title)}</div>` : ""}
          ${assessments || '<div class="news-assessment neutral"><span>尚未形成标的级结论，不进入系统参考</span></div>'}
        </article>`;
      }).join("");
      const newsBox = this.q("#news");
      const duplicate = news.length >= 8;
      newsBox.dataset.duplicate = duplicate ? "1" : "0";
      newsBox.innerHTML = content ? (duplicate ? content + content : content) : '<div class="empty">舆情模块加载中…</div>';
      newsBox.onmouseenter = () => { newsBox.dataset.hover = "1"; };
      newsBox.onmouseleave = () => { newsBox.dataset.hover = "0"; };
      this.startNewsAutoScroll();
    } catch (_) {}
  }

  startNewsAutoScroll() {
    this.stopNewsAutoScroll();
    const box = this.q("#news");
    this.newsTimer = setInterval(() => {
      if (!this.running || box.dataset.hover === "1" || box.scrollHeight <= box.clientHeight) return;
      box.scrollTop += 1;
      const half = box.scrollHeight / 2;
      if (box.dataset.duplicate === "1" && box.scrollTop >= half) box.scrollTop -= half;
    }, 60);
  }

  stopNewsAutoScroll() {
    if (this.newsTimer) clearInterval(this.newsTimer);
    this.newsTimer = null;
  }

  beep(frequency, times) {
    if (!this.state.sound) return;
    try {
      this.audioContext = this.audioContext || new (window.AudioContext || window.webkitAudioContext)();
      for (let index = 0; index < times; index += 1) {
        const oscillator = this.audioContext.createOscillator();
        const gain = this.audioContext.createGain();
        oscillator.frequency.value = frequency;
        gain.gain.setValueAtTime(0.1, this.audioContext.currentTime + index * 0.22);
        gain.gain.exponentialRampToValueAtTime(0.001, this.audioContext.currentTime + index * 0.22 + 0.15);
        oscillator.connect(gain).connect(this.audioContext.destination);
        oscillator.start(this.audioContext.currentTime + index * 0.22);
        oscillator.stop(this.audioContext.currentTime + index * 0.22 + 0.15);
      }
    } catch (_) {}
  }

  activateResearchPage(button, { smooth = false } = {}) {
    const workspace = this.q(".research-workspace");
    if (!button || !workspace) return;
    const page = button.dataset.modalPage || "overview";
    const target = this.q(button.dataset.modalSection);
    this.qa("[data-research-page]").forEach((panel) => {
      panel.hidden = panel.dataset.researchPage !== page;
    });
    this.qa("[data-modal-section]").forEach((item) => {
      const selected = item === button;
      item.classList.toggle("on", selected);
      item.setAttribute("aria-selected", selected ? "true" : "false");
    });
    const top = page === "overview" && button.dataset.modalSection === "#modal-indicator-section" && target
      ? Math.max(0, target.offsetTop - 82)
      : 0;
    workspace.scrollTo({
      top,
      behavior: smooth && !window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "smooth" : "auto",
    });
    if (page === "overview") requestAnimationFrame(() => this.drawChart());
  }

  revealModal(symbol) {
    this.state.modal.symbol = symbol;
    const modal = this.q("#modal");
    modal.classList.remove("hidden");
    modal.setAttribute("aria-hidden", "false");
    this.activateResearchPage(this.q('[data-modal-section="#modal-trend"]'));
    this.q("#modal-close").focus({ preventScroll: true });
  }

  async openModal(symbol) {
    this.state.modal.predictionContext = null;
    this.state.modal.opportunity = null;
    this.state.modal.overviewError = null;
    this.revealModal(symbol);
    this.startModalMarketStream(symbol);
    await this.refreshModal();
  }

  async openResearch(symbol, timeframe = "1h", predictionContext = null) {
    const normalizedSymbol = String(symbol || "").trim().toUpperCase();
    if (!normalizedSymbol) return;
    this.state.modal.tf = ["15m", "1h", "4h"].includes(timeframe) ? timeframe : "1h";
    this.state.modal.predictionContext = this.normalizePredictionContext(predictionContext);
    this.state.modal.opportunity = null;
    this.state.modal.overviewError = null;
    this.state.modal.marketTransport = "snapshot";
    this.state.modal.marketUpdatedAt = Date.now();
    this.state.modal.tickerTransport = null;
    this.state.modal.depthTransport = null;
    const fallbackOverview = {
      ...this.state.modal.predictionContext?.marketOverview,
      symbol: normalizedSymbol,
      opportunity: this.state.modal.predictionContext ? {
        direction: this.state.modal.predictionContext.direction,
        quality_score: this.state.modal.predictionContext.combinedScore,
        status: this.state.modal.predictionContext.historical ? "completed" : "watching",
      } : null,
    };
    this.state.overview = [fallbackOverview];
    this.revealModal(normalizedSymbol);
    this.q("#modal-symbol").textContent = normalizedSymbol;
    this.q("#modal-price").textContent = this.formatPrice(fallbackOverview.price);
    this.q("#modal-pct").textContent = this.formatPercent(fallbackOverview.pct_24h);
    this.renderModalSummary(fallbackOverview);
    this.q("#modal-ohlc").innerHTML = "<span>正在读取合约行情与 K 线…</span>";
    this.startModalMarketStream(normalizedSymbol);
    const watchlistPromise = this.api("/watchlist").then((watchlist) => {
      this.state.watchlist = new Set(Array.isArray(watchlist) ? watchlist : []);
      this.state.overview.forEach((item) => { item.watch = this.state.watchlist.has(item.symbol); });
    }).catch(() => {});
    await Promise.allSettled([this.refreshModal(), watchlistPromise]);
  }

  normalizePredictionContext(context) {
    if (!context || typeof context !== "object") return null;
    const clampScore = (value) => Math.max(0, Math.min(100, Number(value) || 0));
    const entryPrice = Number(context.entry_price);
    const marketOverview = context.market_overview && typeof context.market_overview === "object"
      ? { ...context.market_overview }
      : {};
    return {
      id: String(context.id || ""),
      direction: context.direction === "short" ? "short" : "long",
      combinedScore: clampScore(context.combined_score),
      newsScore: clampScore(context.news_score),
      indicatorScore: clampScore(context.indicator_score),
      signalTime: context.signal_time || null,
      expiresAt: context.expires_at || null,
      entryPrice: Number.isFinite(entryPrice) && entryPrice > 0 ? entryPrice : null,
      technicalConfirmed: context.technical_confirmed === true,
      historical: context.historical === true,
      outcomeResult: ["win", "loss", "flat"].includes(context.outcome_result) ? context.outcome_result : "unavailable",
      marketOverview,
      exitPrice: Number(context.exit_price) > 0 ? Number(context.exit_price) : null,
      directionalReturnBps: Number.isFinite(Number(context.directional_return_bps)) ? Number(context.directional_return_bps) : null,
      settledPriceAt: context.settled_price_at || null,
    };
  }

  maybeNotifySpreadAlert(item) {
    const quote = item.underlying_quote || {};
    const level = quote.spread_alert || "disabled";
    const previous = this.state.spreadAlertStates.get(item.symbol);
    this.state.spreadAlertStates.set(item.symbol, level);
    if (!quote.basis_comparable || !["watch", "strong"].includes(level) || level === previous) return;
    const basis = Number(quote.basis_bps);
    const direction = basis >= 0 ? "溢价" : "折价";
    const message = `${item.symbol} ${direction} ${Number.isFinite(basis) ? `${basis >= 0 ? "+" : ""}${basis.toFixed(1)} bps` : "异常"}，${this.underlyingAlignmentLabel(quote.alignment_status)}`;
    this.beep(level === "strong" ? 1180 : 760, level === "strong" ? 3 : 2);
    if (this.state.notifyOn && "Notification" in window && Notification.permission === "granted") {
      new Notification("QuantDesk 价差提醒", { body: message });
    }
  }

  stopModalMarketStream() {
    this.modalMarketGeneration += 1;
    [
      "modalMarketFallbackTimer",
      "modalMarketRestTimer",
      "modalMarketReconnectTimer",
      "modalMarketWatchdogTimer",
    ].forEach((key) => {
      if (this[key] != null) window.clearTimeout(this[key]);
      this[key] = null;
    });
    const socket = this.modalMarketSocket;
    this.modalMarketSocket = null;
    if (socket && socket.readyState < 2) {
      try { socket.close(1000, "research modal closed"); } catch (_) {}
    }
    this.modalMarketLastEventAt = 0;
  }

  startModalMarketStream(symbol) {
    const normalized = String(symbol || "").trim().toUpperCase();
    if (!normalized) return;
    this.stopModalMarketStream();
    const generation = this.modalMarketGeneration;
    this.state.modal.marketTransport = "connecting";
    this.state.modal.marketUpdatedAt = Date.now();
    this.modalMarketLastEventAt = Date.now();
    this.renderCurrentModalMarketSummary();

    this.modalMarketFallbackTimer = window.setTimeout(() => {
      this.activateModalRestFallback(normalized, generation);
    }, 2500);
    this.modalMarketWatchdogTimer = window.setInterval(() => {
      if (generation !== this.modalMarketGeneration) return;
      if (Date.now() - this.modalMarketLastEventAt <= 8000) return;
      const socket = this.modalMarketSocket;
      if (socket && socket.readyState < 2) {
        try { socket.close(4000, "market stream stale"); } catch (_) {}
      } else {
        this.activateModalRestFallback(normalized, generation);
      }
    }, 1000);
    this.connectModalMarketStream(normalized, generation);
  }

  async connectModalMarketStream(symbol, generation) {
    if (generation !== this.modalMarketGeneration) return;
    if (typeof window.quantdeskOpenMonitorMarketSocket !== "function") {
      this.activateModalRestFallback(symbol, generation);
      return;
    }
    try {
      const socket = await window.quantdeskOpenMonitorMarketSocket(symbol);
      if (generation !== this.modalMarketGeneration) {
        socket.close(1000, "stale research request");
        return;
      }
      this.modalMarketSocket = socket;
      socket.addEventListener("open", () => {
        if (generation !== this.modalMarketGeneration) return;
        this.modalMarketLastEventAt = Date.now();
        this.state.modal.marketTransport = "connecting";
        this.renderCurrentModalMarketSummary();
      });
      socket.addEventListener("message", (event) => {
        if (generation !== this.modalMarketGeneration) return;
        let message;
        try { message = JSON.parse(event.data); } catch (_) { return; }
        this.modalMarketLastEventAt = Date.now();
        if (message?.event === "heartbeat") return;
        if (message?.event === "degraded") {
          this.activateModalRestFallback(symbol, generation);
          return;
        }
        if (message?.event !== "market" || !message.data) return;
        const transport = String(message.data.transport || "server_cache");
        this.state.modal.marketTransport = transport === "websocket"
          ? "websocket"
          : transport === "rest_fallback"
          ? "ws_rest_fallback"
          : "ws_cache";
        this.state.modal.tickerTransport = message.data.ticker_transport || null;
        this.state.modal.depthTransport = message.data.depth_transport || null;
        this.state.modal.marketUpdatedAt = Number(message.data.server_sent_at_ms) || Date.now();
        this.state.modal.overviewError = null;
        if (this.modalMarketFallbackTimer != null) {
          window.clearTimeout(this.modalMarketFallbackTimer);
          this.modalMarketFallbackTimer = null;
        }
        if (this.modalMarketRestTimer != null) {
          window.clearInterval(this.modalMarketRestTimer);
          this.modalMarketRestTimer = null;
        }
        this.applyModalMarketSnapshot(message.data);
      });
      socket.addEventListener("close", () => {
        if (generation !== this.modalMarketGeneration) return;
        if (this.modalMarketSocket === socket) this.modalMarketSocket = null;
        this.activateModalRestFallback(symbol, generation);
        if (this.modalMarketReconnectTimer == null) {
          this.modalMarketReconnectTimer = window.setTimeout(() => {
            this.modalMarketReconnectTimer = null;
            if (generation === this.modalMarketGeneration) {
              this.state.modal.marketTransport = "reconnecting";
              this.renderCurrentModalMarketSummary();
              this.connectModalMarketStream(symbol, generation);
            }
          }, 2500);
        }
      });
      socket.addEventListener("error", () => {
        if (generation === this.modalMarketGeneration) {
          this.activateModalRestFallback(symbol, generation);
        }
      });
    } catch (error) {
      if (generation !== this.modalMarketGeneration) return;
      this.state.modal.overviewError = error?.message || "WS 实时行情连接失败";
      this.activateModalRestFallback(symbol, generation);
      if (this.modalMarketReconnectTimer == null) {
        this.modalMarketReconnectTimer = window.setTimeout(() => {
          this.modalMarketReconnectTimer = null;
          this.connectModalMarketStream(symbol, generation);
        }, 2500);
      }
    }
  }

  activateModalRestFallback(symbol, generation) {
    if (generation !== this.modalMarketGeneration) return;
    if (this.modalMarketFallbackTimer != null) {
      window.clearTimeout(this.modalMarketFallbackTimer);
      this.modalMarketFallbackTimer = null;
    }
    const wsFresh = ["websocket", "ws_rest_fallback", "ws_cache"].includes(this.state.modal.marketTransport)
      && Date.now() - this.modalMarketLastEventAt <= 8000;
    if (wsFresh) return;
    this.state.modal.marketTransport = "rest";
    this.renderCurrentModalMarketSummary();
    this.refreshModalOverviewFallback(symbol, generation);
    if (this.modalMarketRestTimer == null) {
      this.modalMarketRestTimer = window.setInterval(() => {
        this.refreshModalOverviewFallback(symbol, generation);
      }, 5000);
    }
  }

  async refreshModalOverviewFallback(symbol, generation) {
    if (generation !== this.modalMarketGeneration) return;
    try {
      const encoded = encodeURIComponent(symbol);
      const overview = await this.api(`/overview?symbol=${encoded}`);
      if (generation !== this.modalMarketGeneration) return;
      const item = Array.isArray(overview.items) ? overview.items[0] : null;
      if (!item) return;
      item.watch = this.state.watchlist.has(item.symbol);
      this.state.modal.overviewError = null;
      this.state.modal.marketUpdatedAt = Date.now();
      this.applyModalMarketSnapshot(item);
    } catch (error) {
      if (generation !== this.modalMarketGeneration) return;
      this.state.modal.overviewError = `实时总览暂不可用，当前显示机会快照${error?.message ? `：${error.message}` : ""}`;
      this.renderCurrentModalMarketSummary();
    }
  }

  applyModalMarketSnapshot(snapshot) {
    const symbol = String(snapshot?.symbol || "").trim().toUpperCase();
    if (!symbol || symbol !== this.state.modal.symbol) return;
    const current = this.state.overview.find((item) => item.symbol === symbol) || { symbol };
    const merged = { ...current };
    [
      "price",
      "pct_24h",
      "quote_volume",
      "bid_depth_notional",
      "ask_depth_notional",
      "book_imbalance",
      "best_bid",
      "best_ask",
      "spread_bps",
      "depth_levels",
    ].forEach((key) => {
      if (snapshot[key] !== null && snapshot[key] !== undefined) merged[key] = snapshot[key];
    });
    if (snapshot.battle && typeof snapshot.battle === "object") merged.battle = snapshot.battle;
    if (snapshot.opportunity && typeof snapshot.opportunity === "object") merged.opportunity = snapshot.opportunity;
    merged.watch = this.state.watchlist.has(symbol) || Boolean(current.watch);
    const index = this.state.overview.findIndex((item) => item.symbol === symbol);
    if (index >= 0) this.state.overview.splice(index, 1, merged);
    else this.state.overview = [merged, ...this.state.overview];

    if (this.q("#modal").classList.contains("hidden")) return;
    this.q("#modal-price").textContent = this.formatPrice(merged.price);
    this.q("#modal-pct").textContent = this.formatPercent(merged.pct_24h);
    this.q("#modal-pct").className = `research-change ${merged.pct_24h > 0 ? "up" : merged.pct_24h < 0 ? "down" : "flat"}`;
    this.renderModalSummary(merged, [], null, this.state.modal.opportunity);
  }

  renderCurrentModalMarketSummary() {
    const symbol = this.state.modal.symbol;
    if (!symbol || this.q("#modal").classList.contains("hidden")) return;
    const overview = this.state.overview.find((item) => item.symbol === symbol) || { symbol };
    this.renderModalSummary(overview, [], null, this.state.modal.opportunity);
  }

  closeModal() {
    const modal = this.q("#modal");
    this.q("#modal-close").blur();
    modal.classList.add("hidden");
    modal.setAttribute("aria-hidden", "true");
    this.stopModalMarketStream();
    this.state.modal.marketTransport = "idle";
  }

  async openPredictionHistory() {
    const modal = this.q("#prediction-history-modal");
    modal.classList.remove("hidden");
    modal.setAttribute("aria-hidden", "false");
    if (this.state.history.timeRange.startMs == null) this.setRecentPredictionHistoryRange(false);
    await this.loadPredictionHistory(1);
  }

  closePredictionHistory() {
    this.closeHistoricalPredictionConfig();
    const modal = this.q("#prediction-history-modal");
    modal.classList.add("hidden");
    modal.setAttribute("aria-hidden", "true");
  }

  async openHistoricalPredictionConfig(predictionId) {
    if (!predictionId || this.state.predictionConfig.loading) return;
    const modal = this.q("#prediction-config-modal");
    const content = this.q("#prediction-config-content");
    this.state.predictionConfig = { id: predictionId, loading: true };
    modal.classList.remove("hidden");
    modal.setAttribute("aria-hidden", "false");
    this.q("#prediction-config-title").textContent = "历史预测指标";
    this.q("#prediction-config-version").textContent = "正在读取该条预测的不可变配置快照…";
    content.innerHTML = '<p class="prediction-config-loading">正在加载预测指标…</p>';
    try {
      const data = await this.api(`/prediction-history/${encodeURIComponent(predictionId)}/algorithm`);
      if (this.state.predictionConfig.id !== predictionId) return;
      this.renderHistoricalPredictionConfig(data);
    } catch (error) {
      if (this.state.predictionConfig.id !== predictionId) return;
      content.innerHTML = `<p class="prediction-config-warning error">${this.escape(error.message || "预测指标加载失败")}</p>`;
      this.q("#prediction-config-version").textContent = "配置读取失败";
    } finally {
      if (this.state.predictionConfig.id === predictionId) this.state.predictionConfig.loading = false;
    }
  }

  closeHistoricalPredictionConfig() {
    const modal = this.q("#prediction-config-modal");
    if (!modal) return;
    this.state.predictionConfig = { id: null, loading: false };
    modal.classList.add("hidden");
    modal.setAttribute("aria-hidden", "true");
  }

  renderHistoricalPredictionConfig(data) {
    const horizonKey = ({ 300: "5m", 900: "15m", 3600: "1h" })[Number(data.horizon_seconds)] || `${data.horizon_seconds}s`;
    const version = `模型 ${data.model_key || "--"} v${data.model_version ?? "--"} · 特征 v${data.feature_schema_version ?? "--"} · 配置 v${data.algorithm_config_version ?? 0}`;
    this.q("#prediction-config-title").textContent = `${data.symbol || "--"} · ${horizonKey} 预测指标`;
    this.q("#prediction-config-version").textContent = `${version} · ${this.barTimeString(data.predicted_at_ms)}`;
    const labels = {
      aggressive_flow: "主动成交流", book_imbalance: "订单簿失衡", book_imbalance_5: "近五档失衡",
      velocity: "价格速度", flash_imbalance: "闪动失衡", taker_flow: "Taker 流向",
      price_oi_impulse: "价格 × 持仓量", trend: "周期趋势",
      kline_bollinger_breakout: "布林突破", kline_moving_average_pullback_bounce: "均线回踩反弹",
      kline_trend_breakout: "趋势突破", kline_price_volume_rise: "量价齐升",
      kline_new_low_reversal: "新低反转", kline_low_volume_pullback: "缩量回踩",
      kline_strong_gap_open: "强势高开", kline_moving_average_bull: "均线多头",
      kline_ma_golden_cross: "MA 金叉", kline_macd_golden_cross_volume: "MACD 金叉放量",
      kline_oversold_bounce: "超跌反弹", kline_oversold_reversal: "超跌反转",
    };
    const featureKeys = Object.keys(labels);
    const config = data.algorithm_config;
    const components = data.components || {};
    const features = data.features || {};
    const timeframe = components.kline_strategy_timeframe || (horizonKey === "1h" ? "1h" : "15m");
    const klineValues = features.kline_strategies?.[timeframe]?.values || {};
    const inputValue = (key) => {
      if (key.startsWith("kline_")) return klineValues[key];
      if (key === "trend") return horizonKey === "1h"
        ? 0.65 * Number(features.trend_1h || 0) + 0.35 * Number(features.trend_4h || 0)
        : features.trend_15m;
      return features[key];
    };
    const number = (value, digits = 3) => Number.isFinite(Number(value)) ? Number(value).toFixed(digits) : "--";
    const weight = (period, key) => config ? number(config.weights?.[period]?.[key], 3) : "--";
    const rows = (keys, group) => `<tr class="prediction-config-group"><th colspan="7">${group}</th></tr>${keys.map((key) => {
      const contribution = Number(components[key]);
      const contributionClass = !Number.isFinite(contribution) ? "" : contribution > 0 ? "positive" : contribution < 0 ? "negative" : "neutral";
      return `<tr><th>${labels[key]}<small>${key}</small></th><td>${key.startsWith("kline_") ? "K 线策略" : "市场因子"}</td><td>${weight("5m", key)}</td><td>${weight("15m", key)}</td><td>${weight("1h", key)}</td><td>${number(inputValue(key), 4)}</td><td class="${contributionClass}">${number(components[key], 4)}</td></tr>`;
    }).join("")}`;
    const warning = data.algorithm_snapshot_available
      ? '<p class="prediction-config-warning ok">完整快照：以下参数就是生成这条预测时实际使用的配置，后续修改全局算法不会改变它。</p>'
      : '<p class="prediction-config-warning">旧数据未保存完整算法配置，不能用当前全局配置冒充。以下权重显示“--”，仍保留当时的模型/特征版本及指标贡献。</p>';
    const scalarCards = config ? `<section class="prediction-config-scalars">
      <article><span>方向阈值</span><strong>${number(config.direction_threshold, 3)}</strong></article>
      <article><span>最低数据质量</span><strong>${number(config.min_data_quality, 3)}</strong></article>
      <article><span>账户拥挤惩罚</span><strong>${number(config.account_crowding_penalty, 3)}</strong></article>
      <article><span>资金费率惩罚</span><strong>${number(config.funding_crowding_penalty, 3)}</strong></article>
    </section>` : "";
    const reasons = (data.reason_codes || []).length
      ? data.reason_codes.map((item) => `<span>${this.escape(item)}</span>`).join("") : "<span>无</span>";
    this.q("#prediction-config-content").innerHTML = `${warning}
      <section class="prediction-config-meta">
        <article><span>预测结果</span><strong>${({ long: "看多", short: "看空", neutral: "中性" })[data.prediction_result] || "--"}</strong></article>
        <article><span>综合评分</span><strong>${number(data.battle_score, 3)}</strong></article>
        <article><span>数据质量</span><strong>${number(data.quality_score, 3)}</strong></article>
        <article><span>快照状态</span><strong>${data.algorithm_snapshot_available ? "完整" : "历史缺失"}</strong></article>
      </section>
      ${scalarCards}
      <div class="prediction-config-table-wrap"><table class="prediction-config-table">
        <thead><tr><th>预测指标</th><th>类型</th><th>5m 权重</th><th>15m 权重</th><th>1h 权重</th><th>${horizonKey} 输入值</th><th>${horizonKey} 实际贡献</th></tr></thead>
        <tbody>${rows(featureKeys.slice(0, 8), "市场与微观因子 · 8 项")}${rows(featureKeys.slice(8), "K 线策略信号 · 12 项")}</tbody>
      </table></div>
      <section class="prediction-config-reasons"><strong>当时的主要原因码</strong><div>${reasons}</div></section>`;
  }

  async openPredictionAlgorithm() {
    const modal = this.q("#prediction-algorithm-modal");
    modal.classList.remove("hidden");
    modal.setAttribute("aria-hidden", "false");
    await this.loadPredictionAlgorithm();
  }

  closePredictionAlgorithm() {
    const modal = this.q("#prediction-algorithm-modal");
    modal.classList.add("hidden");
    modal.setAttribute("aria-hidden", "true");
  }

  async loadPredictionAiTrace() {
    const button = this.q("#prediction-algorithm-ai-trace");
    button.classList.add("hidden");
    this.state.algorithm.trace = null;
    try {
      const trace = await this.api("/prediction-algorithm/ai-trace");
      this.state.algorithm.trace = trace;
      button.classList.remove("hidden");
    } catch (_) {
      // A manual/default algorithm version legitimately has no DeepSeek trace.
    }
  }

  async openPredictionAiHistory() {
    if (!this.state.algorithm.data?.editable) return;
    const modal = this.q("#prediction-ai-history-modal");
    const body = this.q("#prediction-ai-history-body");
    modal.classList.remove("hidden");
    modal.setAttribute("aria-hidden", "false");
    body.innerHTML = '<tr><td colspan="9" class="ai-history-empty">正在读取历史分析记录…</td></tr>';
    this.q("#prediction-ai-history-summary").textContent = "正在读取审计日志…";
    try {
      const data = await this.api("/prediction-algorithm/ai-history?limit=50");
      const items = Array.isArray(data.items) ? data.items : [];
      this.state.algorithm.historyRecords = items;
      this.renderPredictionAiHistory(items);
      this.q("#prediction-ai-history-summary").textContent = `显示最近 ${items.length} 条 / 共 ${Number(data.total) || 0} 条分析记录`;
    } catch (error) {
      body.innerHTML = `<tr><td colspan="9" class="ai-history-empty error">${this.escape(error.message || "历史分析记录加载失败")}</td></tr>`;
      this.q("#prediction-ai-history-summary").textContent = "读取失败";
    }
  }

  renderPredictionAiHistory(items) {
    const body = this.q("#prediction-ai-history-body");
    if (!items.length) {
      body.innerHTML = '<tr><td colspan="9" class="ai-history-empty">暂无 DeepSeek 历史分析记录</td></tr>';
      return;
    }
    body.innerHTML = items.map((item) => {
      const saved = item.status !== "rejected" && item.saved_config_version != null;
      const status = saved ? "已保存" : "未保存";
      const version = saved
        ? `v${Number(item.source_config_version) || 0} → v${Number(item.saved_config_version) || 0}`
        : `v${Number(item.source_config_version) || 0}`;
      const created = item.created_at
        ? new Date(item.created_at).toLocaleString("zh-CN", { hour12: false }) : "--";
      const horizons = Array.isArray(item.optimized_horizons) && item.optimized_horizons.length
        ? item.optimized_horizons.join(" / ") : "--";
      return `<tr>
        <td>${this.escape(created)}</td>
        <td><strong>${this.escape(version)}</strong></td>
        <td><span class="ai-history-status ${saved ? "saved" : "rejected"}">${status}</span><small>${this.escape(item.failure_category || "")}</small></td>
        <td>${this.escape(item.response_model || item.model_name || "--")}</td>
        <td>${Number(item.sample_count).toLocaleString("zh-CN")}</td>
        <td>${Number.isFinite(Number(item.total_tokens)) ? Number(item.total_tokens).toLocaleString("zh-CN") : "--"}</td>
        <td>${this.escape(horizons)}</td>
        <td class="ai-history-summary-cell">${this.escape(item.summary || "--")}</td>
        <td><button class="ai-history-detail" type="button" data-ai-history-audit="${Number(item.audit_id)}">查看详情</button></td>
      </tr>`;
    }).join("");
  }

  async openPredictionAiHistoryDetail(auditId, button) {
    if (!auditId || button.disabled) return;
    const original = button.textContent;
    button.disabled = true;
    button.textContent = "读取中…";
    try {
      const trace = await this.api(`/prediction-algorithm/ai-history/${encodeURIComponent(auditId)}`);
      this.openPredictionAiTrace(trace);
    } catch (error) {
      button.textContent = error.message || "读取失败";
      return;
    } finally {
      button.disabled = false;
    }
    button.textContent = original;
  }

  closePredictionAiHistory() {
    const modal = this.q("#prediction-ai-history-modal");
    modal.classList.add("hidden");
    modal.setAttribute("aria-hidden", "true");
  }

  openPredictionAiTrace(trace = null) {
    const data = trace || this.state.algorithm.trace;
    if (!data) return;
    const prompt = data.submitted_prompt || {};
    let userPrompt = String(prompt.user || "");
    let promptPayload = null;
    try {
      promptPayload = JSON.parse(userPrompt);
      userPrompt = JSON.stringify(promptPayload, null, 2);
    } catch (_) {
      // Preserve the exact submitted text when it is not JSON.
    }
    const tokens = Number(data.usage?.total_tokens);
    const created = data.created_at
      ? ` · ${new Date(data.created_at).toLocaleString("zh-CN", { hour12: false })}` : "";
    const versionLabel = data.status === "rejected"
      ? `配置 v${Number(data.source_config_version) || 0} · 本次未保存`
      : `配置 v${Number(data.source_config_version) || 0} → v${Number(data.saved_config_version) || 0}`;
    this.q("#prediction-ai-trace-version").textContent = `${data.model_name || "DeepSeek"} · ${versionLabel}${Number.isFinite(tokens) ? ` · ${tokens} tokens` : ""}${created}`;
    const steps = Array.isArray(data.reasoning_steps) ? data.reasoning_steps : [];
    this.q("#prediction-ai-reasoning-steps").innerHTML = steps.length
      ? steps.map((step) => `<li>${this.escape(step)}</li>`).join("")
      : `<li>${this.escape(data.summary || "DeepSeek 未返回可展示的推理摘要。")}</li>`;
    const submittedHistory = promptPayload?.training_statistics?.history;
    const submittedHorizonItems = submittedHistory?.horizons
      && typeof submittedHistory.horizons === "object"
      ? Object.values(submittedHistory.horizons) : [];
    const submittedHasAnalysis = submittedHorizonItems.length > 0
      && submittedHorizonItems.every((item) => item?.training_history_analysis?.summary);
    const recomputed = data.database_history_analysis;
    const history = submittedHasAnalysis
      ? submittedHistory
      : recomputed?.available ? recomputed.history : null;
    const horizons = history?.horizons && typeof history.horizons === "object"
      ? Object.entries(history.horizons) : [];
    const number = (value, digits = 2) => Number.isFinite(Number(value))
      ? Number(value).toFixed(digits) : "--";
    const percent = (value) => Number.isFinite(Number(value))
      ? `${(Number(value) * 100).toFixed(1)}%` : "--";
    const bps = (value) => Number.isFinite(Number(value))
      ? `${Number(value) >= 0 ? "+" : ""}${Number(value).toFixed(2)} bps` : "--";
    this.q("#prediction-ai-history-analysis").innerHTML = horizons.length
      ? `
        <p>${submittedHasAnalysis ? "当时实际提交给 DeepSeek" : "旧审计记录 · 按源版本及审计截止时间从数据库回算"} · 精确配置 v${Number(promptPayload?.training_statistics?.algorithm?.config_version ?? recomputed?.source_config_version) || 0} · 数据库已结算 ${Number(history.sample_count) || 0} 条 · 较早 75% 训练，最近 25% 隐藏验证</p>
        <div>${horizons.map(([horizon, item]) => {
          const summary = item?.training_history_analysis?.summary || {};
          const drift = item?.training_history_analysis?.chronological_drift?.newest_25_percent || {};
          return `
            <article>
              <header><strong>${this.escape(horizon)}</strong><span>训练 ${Number(item.training_count) || 0} / 总计 ${Number(item.sample_count) || 0} · 隐藏 ${Number(item.validation_count_reserved) || 0}</span></header>
              <dl>
                <div><dt>方向样本 / 命中率</dt><dd>${Number(summary.directional_count) || 0} / ${percent(summary.hit_rate)}</dd></div>
                <div><dt>平均方向收益</dt><dd>${bps(summary.avg_directional_return_bps)}</dd></div>
                <div><dt>平均最大有利 / 不利</dt><dd>${bps(summary.avg_max_favorable_bps)} / ${bps(summary.avg_max_adverse_bps)}</dd></div>
                <div><dt>收益因子 / 最大顺序回撤</dt><dd>${number(summary.profit_factor)} / ${bps(summary.max_sequential_drawdown_bps)}</dd></div>
                <div><dt>置信度 / 校准差</dt><dd>${percent(summary.avg_confidence_score)} / ${percent(summary.confidence_calibration_gap)}</dd></div>
                <div><dt>训练集近期 25% 收益</dt><dd>${bps(drift.avg_directional_return_bps)}</dd></div>
              </dl>
            </article>`;
        }).join("")}</div>`
      : `<p>无法取得这个配置版本的实际历史值：${this.escape(recomputed?.reason || "审计数据不完整")}。</p>`;
    this.q("#prediction-ai-system-prompt").textContent = String(prompt.system || "--");
    this.q("#prediction-ai-user-prompt").textContent = userPrompt || "--";
    this.q("#prediction-ai-request-options").textContent = JSON.stringify({
      model: prompt.model || data.model_name || "--",
      ...(prompt.request_options || {}),
      model_attempts: data.model_attempts || [],
    }, null, 2);
    this.q("#prediction-ai-raw-output").textContent = JSON.stringify(
      data.raw_model_output || {}, null, 2,
    );
    this.q("#prediction-ai-normalization").textContent = JSON.stringify(
      data.normalization || { applied: false }, null, 2,
    );
    const modal = this.q("#prediction-ai-trace-modal");
    modal.classList.remove("hidden");
    modal.setAttribute("aria-hidden", "false");
  }

  closePredictionAiTrace() {
    const modal = this.q("#prediction-ai-trace-modal");
    modal.classList.add("hidden");
    modal.setAttribute("aria-hidden", "true");
  }

  async loadPredictionAlgorithm() {
    const message = this.q("#prediction-algorithm-message");
    const optimizationPanel = this.q("#prediction-algorithm-optimization");
    this.state.algorithm.loading = true;
    this.state.algorithm.optimization = null;
    this.state.algorithm.trace = null;
    this.q("#prediction-algorithm-ai-trace").classList.add("hidden");
    this.q("#prediction-algorithm-ai-history").classList.add("hidden");
    this.q("#prediction-algorithm-ai-history").disabled = true;
    optimizationPanel.innerHTML = "";
    optimizationPanel.classList.add("hidden");
    message.textContent = "正在读取当前算法…";
    try {
      const data = await this.api("/prediction-algorithm");
      this.state.algorithm.data = data;
      this.populatePredictionAlgorithm(data.config);
      const source = data.source === "custom" ? "自定义配置" : "系统默认配置";
      const updated = data.updated_at ? ` · 更新于 ${new Date(data.updated_at).toLocaleString("zh-CN", { hour12: false })}` : "";
      this.q("#prediction-algorithm-version").textContent = `${data.model_key} v${data.model_version} · ${Number(data.feature_count) || 20} 项特征 · 配置版本 ${data.config_version} · ${source}${updated}`;
      this.qa("#prediction-algorithm-form input").forEach((input) => { input.disabled = !data.editable; });
      this.qa("[data-algorithm-enabled]").forEach((button) => { button.disabled = !data.editable; });
      this.q("#prediction-algorithm-defaults").disabled = !data.editable;
      this.q("#prediction-algorithm-save").disabled = !data.editable;
      this.q("#prediction-algorithm-optimize").disabled = !data.editable;
      this.q("#prediction-algorithm-ai-history").classList.toggle("hidden", !data.editable);
      this.q("#prediction-algorithm-ai-history").disabled = !data.editable;
      message.textContent = data.editable ? "修改后需保存才会生效。" : "当前账号可查看规则，但只有管理员可以调整全局算法。";
      if (data.editable) await this.loadPredictionAiTrace();
    } catch (error) {
      message.textContent = error.message || "预测算法加载失败";
    } finally {
      this.state.algorithm.loading = false;
    }
  }

  populatePredictionAlgorithm(config) {
    if (!config) return;
    this.qa("[data-algorithm-scalar]").forEach((input) => {
      input.value = Number(config[input.dataset.algorithmScalar]).toFixed(2);
    });
    this.qa("[data-algorithm-weight]").forEach((input) => {
      input.min = "0";
      input.max = "1";
      input.step = "0.0001";
      input.value = Number(config.weights?.[input.dataset.horizon]?.[input.dataset.algorithmWeight] || 0).toFixed(4);
    });
    this.qa("[data-algorithm-enabled]").forEach((button) => {
      const enabled = config.enabled_features?.[button.dataset.algorithmEnabled] !== false;
      button.setAttribute("aria-pressed", String(enabled));
      button.textContent = enabled ? "启用" : "停用";
      button.classList.toggle("on", enabled);
      button.closest("tr")?.classList.toggle("algorithm-feature-disabled", !enabled);
    });
    this.renderAlgorithmWeightSums();
  }

  toggleAlgorithmFeature(button) {
    if (button.disabled || this.state.algorithm.loading || this.state.algorithm.optimizing) return;
    const enabled = button.getAttribute("aria-pressed") !== "true";
    button.setAttribute("aria-pressed", String(enabled));
    button.textContent = enabled ? "启用" : "停用";
    button.classList.toggle("on", enabled);
    button.closest("tr")?.classList.toggle("algorithm-feature-disabled", !enabled);
  }

  renderAlgorithmWeightSums() {
    ["5m", "15m", "1h"].forEach((horizon) => {
      const total = [...this.qa(`[data-algorithm-weight][data-horizon="${horizon}"]`)]
        .reduce((sum, input) => sum + (Number(input.value) || 0), 0);
      const output = this.q(`#algorithm-sum-${horizon}`);
      output.textContent = total.toFixed(3);
      output.className = Math.abs(total - 1) <= 0.001 ? "algorithm-sum-ok" : "algorithm-sum-error";
    });
  }

  collectPredictionAlgorithm() {
    const config = { enabled_features: {}, weights: { "5m": {}, "15m": {}, "1h": {} } };
    this.qa("[data-algorithm-scalar]").forEach((input) => {
      config[input.dataset.algorithmScalar] = Number(input.value);
    });
    this.qa("[data-algorithm-weight]").forEach((input) => {
      config.weights[input.dataset.horizon][input.dataset.algorithmWeight] = Number(input.value);
    });
    this.qa("[data-algorithm-enabled]").forEach((button) => {
      config.enabled_features[button.dataset.algorithmEnabled] = button.getAttribute("aria-pressed") === "true";
    });
    if (!Object.values(config.enabled_features).some(Boolean)) throw new Error("至少需要启用一个预测指标");
    for (const horizon of ["5m", "15m", "1h"]) {
      const total = Object.values(config.weights[horizon]).reduce((sum, value) => sum + value, 0);
      if (Math.abs(total - 1) > 0.001) throw new Error(`${horizon} 权重合计必须等于 1.000`);
    }
    return config;
  }

  restoreDefaultAlgorithm() {
    const defaults = this.state.algorithm.data?.defaults;
    if (!defaults) return;
    this.populatePredictionAlgorithm(defaults);
    this.q("#prediction-algorithm-message").textContent = "已填入系统默认参数，点击“保存全局算法”后生效。";
  }

  renderAlgorithmOptimization(data) {
    const panel = this.q("#prediction-algorithm-optimization");
    const percent = (value) => Number.isFinite(Number(value)) ? `${(Number(value) * 100).toFixed(1)}%` : "--";
    const bps = (value) => Number.isFinite(Number(value)) ? `${Number(value) >= 0 ? "+" : ""}${Number(value).toFixed(2)} bps` : "--";
    const featureLabels = {
      aggressive_flow: "主动成交流", book_imbalance: "订单簿失衡", book_imbalance_5: "近五档失衡",
      velocity: "价格速度", flash_imbalance: "闪动失衡", taker_flow: "Taker 流向",
      price_oi_impulse: "价格 × 持仓量", trend: "周期趋势", kline_bollinger_breakout: "布林突破",
      kline_moving_average_pullback_bounce: "均线回踩反弹", kline_trend_breakout: "趋势突破",
      kline_price_volume_rise: "量价齐升", kline_new_low_reversal: "新低反转",
      kline_low_volume_pullback: "缩量回踩", kline_strong_gap_open: "强势高开",
      kline_moving_average_bull: "均线多头", kline_ma_golden_cross: "MA 金叉",
      kline_macd_golden_cross_volume: "MACD 金叉放量", kline_oversold_bounce: "超跌反弹",
      kline_oversold_reversal: "超跌反转",
    };
    const statusLabels = {
      optimized: "通过验证，已保存新版本",
      no_validated_improvement: "验证集未改善，保持原权重",
      insufficient_samples: "样本不足，保持原权重",
    };
    const horizonCards = (data.horizons || []).map((item) => {
      const changes = (item.changes || []).filter((change) => Math.abs(Number(change.delta)) >= 0.0005).slice(0, 5);
      const changeHtml = changes.length ? changes.map((change) => `<li><span>${this.escape(featureLabels[change.feature] || change.feature)}</span><b>${Number(change.before).toFixed(4)} → ${Number(change.after).toFixed(4)}</b><em class="${Number(change.delta) >= 0 ? "up" : "down"}">${Number(change.delta) >= 0 ? "+" : ""}${Number(change.delta).toFixed(4)}</em></li>`).join("") : "<li><span>没有通过验证的安全调整</span></li>";
      return `<article class="algorithm-optimization-card ${item.status === "optimized" ? "accepted" : "held"}">
        <header><strong>${this.escape(item.horizon)}</strong><span>${this.escape(statusLabels[item.status] || item.status)}</span></header>
        <div class="algorithm-optimization-metrics">
          <span>样本 <b>${Number(item.sample_count) || 0}</b></span>
          <span>验证 <b>${Number(item.validation_count) || 0}</b></span>
          <span>命中 <b>${percent(item.baseline?.hit_rate)} → ${percent(item.optimized?.hit_rate)}</b></span>
          <span>覆盖 <b>${percent(item.baseline?.coverage)} → ${percent(item.optimized?.coverage)}</b></span>
          <span>净效用 <b>${bps(item.baseline?.utility_bps)} → ${bps(item.optimized?.utility_bps)}</b></span>
        </div><ul>${changeHtml}</ul></article>`;
    }).join("");
    const start = data.history_start_ms ? this.barTimeString(data.history_start_ms) : "--";
    const end = data.history_end_ms ? this.barTimeString(data.history_end_ms) : "--";
    panel.innerHTML = `<header class="algorithm-optimization-summary">
      <div><strong>DeepSeek AI 优化结果</strong><span>${this.escape(data.model_name || "DeepSeek")} · v${Number(data.source_config_version) || 0} → v${Number(data.saved_config_version) || 0}</span></div>
      <p>已统计当前版本 ${Number(data.sample_count) || 0} 条已结算完整快照（${start} — ${end}），仅将训练集聚合统计投喂给 DeepSeek，验证集不会发送给模型。通过时间外验证的权重已经保存为新版本。${data.summary ? ` ${this.escape(data.summary)}` : ""}</p>
    </header><div class="algorithm-optimization-grid">${horizonCards}</div>`;
    panel.classList.remove("hidden");
  }

  async optimizePredictionAlgorithm() {
    const data = this.state.algorithm.data;
    if (!data?.editable || this.state.algorithm.loading || this.state.algorithm.optimizing) return;
    if (!window.confirm(`将统计当前算法 v${data.config_version} 的已结算历史数据，调用 DeepSeek 计算权重；只有通过时间外验证才会自动保存为新版本。确定继续吗？`)) return;
    const button = this.q("#prediction-algorithm-optimize");
    const saveButton = this.q("#prediction-algorithm-save");
    const panel = this.q("#prediction-algorithm-optimization");
    const message = this.q("#prediction-algorithm-message");
    this.state.algorithm.optimizing = true;
    button.disabled = true;
    saveButton.disabled = true;
    button.textContent = "DeepSeek调优中…";
    panel.innerHTML = '<p class="algorithm-optimization-loading">正在统计当前版本历史数据、调用 DeepSeek 计算权重并执行隐藏验证集校验；thinking 模式预算最长约 120 秒，若未形成完整 JSON 将自动使用非思考模式重试…</p>';
    panel.classList.remove("hidden");
    message.textContent = "DeepSeek 返回且通过服务端验证后，将自动创建并保存新版本。";
    try {
      const result = await this.api("/prediction-algorithm/optimize", {
        method: "POST",
        body: JSON.stringify({ expected_config_version: data.config_version }),
      });
      this.state.algorithm.optimization = result;
      this.state.algorithm.trace = result;
      this.q("#prediction-algorithm-ai-trace").classList.remove("hidden");
      this.state.algorithm.data = result.algorithm;
      this.populatePredictionAlgorithm(result.algorithm.config);
      this.renderAlgorithmOptimization(result);
      this.q("#prediction-algorithm-version").textContent = `${result.algorithm.model_key} v${result.algorithm.model_version} · ${Number(result.algorithm.feature_count) || 20} 项特征 · 配置版本 ${result.algorithm.config_version} · DeepSeek 优化`;
      message.textContent = `DeepSeek 调优完成：算法 v${data.config_version} 已升级并保存为 v${result.saved_config_version}，将在 5 秒内用于后续预测。`;
    } catch (error) {
      panel.innerHTML = `<p class="algorithm-optimization-error">${this.escape(error.message || "AI 优化失败")}</p>`;
      message.textContent = `${error.message || "AI 优化失败"}；当前算法版本未修改。`;
      await this.loadPredictionAiTrace();
    } finally {
      this.state.algorithm.optimizing = false;
      button.disabled = !this.state.algorithm.data?.editable;
      saveButton.disabled = !this.state.algorithm.data?.editable;
      button.textContent = "AI优化算法";
    }
  }

  async savePredictionAlgorithm(event) {
    event.preventDefault();
    if (this.state.algorithm.loading || this.state.algorithm.optimizing || !this.state.algorithm.data?.editable) return;
    const message = this.q("#prediction-algorithm-message");
    let config;
    try {
      config = this.collectPredictionAlgorithm();
    } catch (error) {
      message.textContent = error.message;
      return;
    }
    if (!window.confirm("保存后将影响所有合约后续生成的新预测，历史预测不会重算。确定继续吗？")) return;
    this.state.algorithm.loading = true;
    this.q("#prediction-algorithm-save").disabled = true;
    message.textContent = "正在保存全局算法…";
    try {
      const data = await this.api("/prediction-algorithm", {
        method: "PUT",
        headers: { "X-QuantDesk-Algorithm-Version": String(this.state.algorithm.data.config_version) },
        body: JSON.stringify(config),
      });
      this.state.algorithm.data = data;
      this.state.algorithm.optimization = null;
      this.state.algorithm.trace = null;
      this.q("#prediction-algorithm-ai-trace").classList.add("hidden");
      this.populatePredictionAlgorithm(data.config);
      this.q("#prediction-algorithm-optimization").classList.add("hidden");
      this.q("#prediction-algorithm-version").textContent = `${data.model_key} v${data.model_version} · ${Number(data.feature_count) || 20} 项特征 · 配置版本 ${data.config_version} · 自定义配置`;
      message.textContent = "保存成功，新配置将在 5 秒内用于后续预测。";
    } catch (error) {
      message.textContent = error.message || "预测算法保存失败";
    } finally {
      this.state.algorithm.loading = false;
      this.q("#prediction-algorithm-save").disabled = false;
    }
  }

  async changePredictionHistoryPage(action) {
    if (this.state.history.loading) return;
    const current = this.state.history.page;
    const pages = this.state.history.pages;
    const target = action === "first" ? 1
      : action === "last" ? pages
        : action === "prev" ? current - 1 : current + 1;
    if (target < 1 || target > pages || target === current) return;
    await this.loadPredictionHistory(target);
  }

  async setPredictionHistoryFilter(name, value) {
    if (this.state.history.loading || !["direction", "horizon", "hit"].includes(name)) return;
    if (this.state.history.filters[name] === value) return;
    this.state.history.filters[name] = value;
    this.renderPredictionHistoryFilters();
    await this.loadPredictionHistory(1);
  }

  renderPredictionHistoryFilters() {
    this.qa("[data-history-filter]").forEach((button) => {
      button.classList.toggle(
        "on",
        this.state.history.filters[button.dataset.historyFilter] === button.dataset.filterValue,
      );
    });
  }

  historyHourInputValue(timestamp) {
    const date = new Date(timestamp);
    const local = new Date(timestamp - date.getTimezoneOffset() * 60 * 1000);
    return local.toISOString().slice(0, 16);
  }

  syncPredictionHistoryTimeInputs() {
    const { startMs, endMs } = this.state.history.timeRange;
    this.q("#history-time-start").value = startMs == null ? "" : this.historyHourInputValue(startMs);
    this.q("#history-time-end").value = endMs == null ? "" : this.historyHourInputValue(endMs - 3_600_000);
  }

  async setRecentPredictionHistoryRange(load = true) {
    const currentHour = Math.floor(Date.now() / 3_600_000) * 3_600_000;
    this.state.history.timeRange = {
      startMs: currentHour - 23 * 3_600_000,
      endMs: currentHour + 3_600_000,
    };
    this.syncPredictionHistoryTimeInputs();
    this.q("#history-time-message").textContent = "最近 24 个整点小时（包含当前小时）";
    if (load) await this.loadPredictionHistory(1);
  }

  async clearPredictionHistoryTimeRange() {
    if (this.state.history.loading) return;
    this.state.history.timeRange = { startMs: null, endMs: null };
    this.syncPredictionHistoryTimeInputs();
    this.q("#history-time-message").textContent = "全部记录；小时图显示最近 168 个有数据小时";
    await this.loadPredictionHistory(1);
  }

  async applyPredictionHistoryTimeRange() {
    if (this.state.history.loading) return;
    const startValue = this.q("#history-time-start").value;
    const endValue = this.q("#history-time-end").value;
    const message = this.q("#history-time-message");
    if (!startValue || !endValue) {
      message.textContent = "请选择开始小时和结束小时。";
      return;
    }
    const start = new Date(startValue);
    const end = new Date(endValue);
    start.setMinutes(0, 0, 0);
    end.setMinutes(0, 0, 0);
    const startMs = start.getTime();
    const endMs = end.getTime() + 3_600_000;
    if (!Number.isFinite(startMs) || !Number.isFinite(endMs) || startMs >= endMs) {
      message.textContent = "时间范围无效，请重新选择。";
      return;
    }
    const hours = (endMs - startMs) / 3_600_000;
    if (hours > 168) {
      message.textContent = "单次最多查看 168 小时（7 天）。";
      return;
    }
    this.state.history.timeRange = { startMs, endMs };
    this.syncPredictionHistoryTimeInputs();
    message.textContent = `已选择 ${hours} 个整点小时。`;
    await this.loadPredictionHistory(1);
  }

  async loadPredictionHistory(page) {
    const body = this.q("#prediction-history-body");
    this.state.history.loading = true;
    body.innerHTML = '<tr><td colspan="12" class="history-empty">正在加载历史预测…</td></tr>';
    try {
      const query = new URLSearchParams({ page: String(Math.max(1, Number(page) || 1)) });
      const { direction, horizon, hit } = this.state.history.filters;
      const { startMs, endMs } = this.state.history.timeRange;
      if (startMs != null && endMs != null) {
        query.set("start_ms", String(startMs));
        query.set("end_ms", String(endMs));
      }
      query.set("timezone_offset_minutes", String(-new Date().getTimezoneOffset()));
      if (direction !== "all") query.set("direction", direction);
      if (horizon !== "all") query.set("horizon", horizon);
      if (hit !== "all") query.set("hit", hit);
      const data = await this.api(`/prediction-history?${query}`);
      this.state.history.page = Number(data.page) || 1;
      this.state.history.pages = Number(data.pages) || 1;
      this.state.history.total = Number(data.total) || 0;
      this.state.history.statistics = data.statistics || null;
      this.state.history.hourlyStatistics = data.hourly_statistics || [];
      this.renderPredictionHistory(data.items || []);
      this.renderPredictionHistoryStatistics();
    } catch (error) {
      body.innerHTML = `<tr><td colspan="12" class="history-empty history-error">${this.escape(error.message || "历史预测加载失败")}</td></tr>`;
    } finally {
      this.state.history.loading = false;
      this.renderPredictionHistoryPager();
    }
  }

  renderPredictionHistoryStatistics() {
    const statistics = this.state.history.statistics || {};
    const total = Number(statistics.total) || 0;
    const hitRate = statistics.hit_rate == null ? "--" : `${(Number(statistics.hit_rate) * 100).toFixed(1)}%`;
    const average = statistics.avg_return_bps == null
      ? "--"
      : `${Number(statistics.avg_return_bps) >= 0 ? "+" : ""}${Number(statistics.avg_return_bps).toFixed(2)} bps`;
    this.q("#history-stat-total").textContent = total.toLocaleString("zh-CN");
    this.q("#history-stat-hit-rate").textContent = hitRate;
    this.q("#history-stat-return").textContent = average;
    this.q("#history-stat-return").className = statistics.avg_return_bps == null
      ? "history-neutral" : Number(statistics.avg_return_bps) >= 0 ? "history-hit" : "history-miss";
    this.q("#history-stat-directions").innerHTML = `<span class="history-long">${Number(statistics.long_count || 0).toLocaleString("zh-CN")}</span> / <span class="history-short">${Number(statistics.short_count || 0).toLocaleString("zh-CN")}</span>`;
    this.renderHourlyPredictionStatistics();
  }

  renderHourlyPredictionStatistics() {
    const hourly = this.state.history.hourlyStatistics || [];
    const list = this.q("#history-hourly-list");
    if (!hourly.length) {
      list.innerHTML = '<span class="hourly-empty">当前筛选条件下没有小时统计</span>';
      this.q("#history-hourly-caption").textContent = "所选范围内每个小时的方向命中率";
      return;
    }
    const label = (timestamp) => {
      const date = new Date(timestamp);
      const pad = (value) => String(value).padStart(2, "0");
      return `${pad(date.getMonth() + 1)}/${pad(date.getDate())} ${pad(date.getHours())}:00`;
    };
    list.innerHTML = hourly.map((item) => {
      const total = Number(item.total) || 0;
      const rate = item.hit_rate == null ? null : Math.max(0, Math.min(1, Number(item.hit_rate)));
      const value = rate == null ? "--" : `${(rate * 100).toFixed(1)}%`;
      const average = item.avg_return_bps == null ? "--" : `${Number(item.avg_return_bps) >= 0 ? "+" : ""}${Number(item.avg_return_bps).toFixed(2)} bps`;
      const title = `${label(item.hour_start_ms)} · ${total} 个样本 · 胜率 ${value} · 平均 ${average}`;
      return `<article class="hourly-win-card" title="${this.escape(title)}">
        <div class="prediction-hourly-chart ${rate == null ? "empty" : ""}" style="--hit-angle:${(rate || 0) * 360}deg" role="img" aria-label="${this.escape(title)}"><strong>${value}</strong></div>
        <span>${label(item.hour_start_ms)}</span><small>${total.toLocaleString("zh-CN")} 样本</small>
      </article>`;
    }).join("");
    this.q("#history-hourly-caption").textContent = `${label(hourly[0].hour_start_ms)} 至 ${label(hourly[hourly.length - 1].hour_start_ms)} · 绿色命中 / 红色未命中`;
  }

  renderPredictionHistory(items) {
    const body = this.q("#prediction-history-body");
    if (!items.length) {
      body.innerHTML = '<tr><td colspan="12" class="history-empty">暂无历史预测记录</td></tr>';
      return;
    }
    const directionLabel = (value) => ({ long: "看多", short: "看空", neutral: "中性" })[value] || "--";
    const statusLabel = (value) => ({ completed: "已完成", pending: "待完成", unavailable: "不可用" })[value] || "--";
    const barrierLabel = (value) => ({ target: "触及止盈", stop: "触及止损", neither: "未触及边界" })[value] || "--";
    const confidenceLabel = (value) => ({ low: "低", medium: "中", high: "高" })[value] || "--";
    const stateLabel = (value) => ({ heuristic: "启发式", calibrated: "已校准", data_insufficient: "数据不足" })[value] || "--";
    const bps = (value) => value == null ? "--" : `${Number(value) >= 0 ? "+" : ""}${Number(value).toFixed(2)} bps`;
    const probability = (value) => value == null ? "--" : `${(Number(value) * 100).toFixed(1)}%`;
    body.innerHTML = items.map((item) => {
      const direction = item.prediction_result || "neutral";
      const directionClass = direction === "long" ? "history-long" : direction === "short" ? "history-short" : "history-neutral";
      const hit = item.direction_hit == null ? (direction === "neutral" && item.status === "completed" ? "中性不计" : "--") : item.direction_hit ? "命中" : "未命中";
      const hitClass = item.direction_hit == null ? "history-neutral" : item.direction_hit ? "history-hit" : "history-miss";
      const settled = item.status === "completed";
      return `<tr>
        <td>${this.barTimeString(item.predicted_at_ms)}</td>
        <td><strong>${this.escape(item.symbol)}</strong><small>${this.escape({ 300: "5m", 900: "15m", 3600: "1h" }[item.horizon_seconds] || `${item.horizon_seconds}s`)}</small></td>
        <td>${this.formatPrice(item.entry_price)}</td>
        <td><strong class="${directionClass}">${directionLabel(direction)}</strong><small>${item.battle_score == null ? "--" : `${Number(item.battle_score) >= 0 ? "+" : ""}${Number(item.battle_score).toFixed(3)}`}</small></td>
        <td><span class="history-long">${probability(item.long_probability)}</span><span class="history-short">${probability(item.short_probability)}</span><span>${probability(item.neutral_probability)}</span></td>
        <td>${item.confidence_score == null ? "--" : probability(item.confidence_score)}<small>${confidenceLabel(item.confidence_label)} · ${stateLabel(item.prediction_state)}</small></td>
        <td>${settled ? this.barTimeString(item.completed_at_ms) : "--"}<small>${this.barTimeString(item.due_at_ms)}</small></td>
        <td>${settled ? this.formatPrice(item.exit_price) : "--"}<small>${settled ? bps(item.raw_return_bps) : "--"}</small></td>
        <td><strong>${settled ? directionLabel(item.actual_result) : "--"}</strong><small>${statusLabel(item.status)} · ${settled ? barrierLabel(item.hit_result) : "等待结算"}</small></td>
        <td><strong class="${hitClass}">${hit}</strong><small>${bps(item.directional_return_bps)}</small></td>
        <td><span>${bps(item.max_favorable_bps)}</span><small>${bps(item.max_adverse_bps)}</small></td>
        <td><button class="history-prediction-config" type="button" data-prediction-config="${this.escape(item.public_id)}">预测指标</button><small>模型 v${item.model_version ?? "--"} · 特征 v${item.feature_schema_version ?? "--"}<br>配置 v${item.algorithm_config_version ?? 0}${item.algorithm_snapshot_available ? " · 已存档" : " · 旧数据"}</small></td>
      </tr>`;
    }).join("");
  }

  renderPredictionHistoryPager() {
    const { page, pages, total, loading } = this.state.history;
    this.q("#prediction-history-summary").textContent = `共 ${total.toLocaleString("zh-CN")} 条 · 每页 50 条`;
    this.q("#prediction-history-page").textContent = `第 ${page} / ${pages} 页`;
    this.qa("[data-history-page]").forEach((button) => {
      const action = button.dataset.historyPage;
      button.disabled = loading || ((action === "first" || action === "prev") ? page <= 1 : page >= pages);
    });
  }

  async refreshModal() {
    const symbol = this.state.modal.symbol;
    const timeframe = this.state.modal.tf;
    if (!symbol) return;
    const initialOverview = this.state.overview.find((item) => item.symbol === symbol) || {};
    this.q("#modal-symbol").textContent = symbol;
    this.q("#modal-price").textContent = this.formatPrice(initialOverview.price);
    this.q("#modal-pct").textContent = this.formatPercent(initialOverview.pct_24h);
    this.q("#modal-pct").className = `research-change ${initialOverview.pct_24h > 0 ? "up" : initialOverview.pct_24h < 0 ? "down" : "flat"}`;
    this.q("#modal-watch").textContent = initialOverview.watch ? "★" : "☆";
    this.q("#modal-watch").classList.toggle("on", Boolean(initialOverview.watch));
    this.qa(".tf-switch button").forEach((button) => button.classList.toggle("on", button.dataset.tf === timeframe));
    this.renderModalSummary(initialOverview);
    this.q("#modal-ohlc").innerHTML = "<span>正在加载当前周期行情…</span>";
    this.q("#indicator-total-caption").textContent = "共 20 项 · 正在读取真实数据";
    this.q("#strategy-indicator-caption").textContent = `最新一根 K 线：${timeframe} · 正在计算 12 项`;
    this.q("#strategy-indicator-list").innerHTML = '<span class="strategy-indicator-loading">策略指标计算中…</span>';
    this.q("#prediction-feature-caption").textContent = "最新预测快照：-- · 正在读取 8 项";
    this.q("#prediction-feature-list").innerHTML = '<span class="strategy-indicator-loading">预测因子读取中…</span>';
    this.q("#strategy-indicator-detail").innerHTML = "";
    this.q("#research-news-caption").textContent = "老虎证券三路新闻接口聚合 · 正在读取";
    this.q("#research-news-list").innerHTML = '<div class="research-news-state">正在加载相关新闻…</div>';
    try {
      const encoded = encodeURIComponent(symbol);
      const [klines, scores, report, opportunities, indicatorScan, researchNews] = await Promise.all([
        this.api(`/klines?symbol=${encoded}&tf=${timeframe}&limit=300`),
        this.api(`/score?symbol=${encoded}`),
        this.api(`/report?symbol=${encoded}`),
        this.api(`/opportunities?symbol=${encoded}&limit=1&include_ignored=true`),
        this.api(`/strategy-indicators?symbol=${encoded}&tf=${timeframe}`).catch((error) => ({
          timeframe,
          count: 12,
          triggered_count: 0,
          items: [],
          prediction_features: { count: 8, items: [] },
          error: error.message || "策略指标加载失败",
        })),
        this.api(`/tiger-news?symbol=${encoded}&limit=30`).catch((error) => ({
          available: false,
          items: [],
          error_category: error.message || "upstream",
        })),
      ]);
      const opportunityItems = Array.isArray(opportunities.items) ? opportunities.items : [];
      this.state.modal.opportunity = opportunityItems[0] || null;
      const overview = this.state.overview.find((item) => item.symbol === symbol) || initialOverview;
      this.renderStrategyIndicators(indicatorScan);
      this.renderModalSummary(overview, klines, report, this.state.modal.opportunity);
      this.setChartData(klines);
      this.renderBattle(overview.battle || {});
      this.renderOpportunity(this.state.modal.opportunity);
      this.renderScoreSummary(scores, report);
      this.renderReport(report);
      this.renderFactors(scores[timeframe]);
      this.renderResearchNews(researchNews);
    } catch (error) {
      this.q("#modal-ohlc").innerHTML = '<span class="down">当前周期行情加载失败</span>';
      this.q("#report").innerHTML = `<div class="error-banner">${this.escape(error.message || "详情加载失败")}</div>`;
    }
  }

  renderResearchNews(payload) {
    const caption = this.q("#research-news-caption");
    const list = this.q("#research-news-list");
    const items = Array.isArray(payload?.items) ? payload.items : [];
    const sourceCount = Math.max(0, Number(payload?.source_count) || 0);
    const fetchedAt = payload?.fetched_at ? new Date(payload.fetched_at) : null;
    const fetchedLabel = fetchedAt && Number.isFinite(fetchedAt.getTime())
      ? fetchedAt.toLocaleString("zh-CN", { hour12: false })
      : "--";
    if (!payload?.available) {
      const messages = {
        not_configured: "老虎证券新闻凭据尚未配置",
        authentication: "老虎证券新闻认证已失效",
        rate_limit: "老虎证券新闻请求过于频繁，请稍后重试",
        invalid_symbol: "当前标的不支持新闻查询",
      };
      caption.textContent = "老虎证券新闻暂不可用";
      list.innerHTML = `<div class="research-news-state error">${this.escape(messages[payload?.error_category] || "老虎证券新闻读取失败，请稍后重试")}</div>`;
      return;
    }
    const staleLabel = payload.stale ? " · 最近有效快照" : "";
    const partialLabel = payload.partial ? " · 部分接口可用" : "";
    caption.textContent = `老虎证券 ${sourceCount}/3 路接口 · ${items.length} 条 · 更新 ${fetchedLabel}${staleLabel}${partialLabel}`;
    if (!items.length) {
      list.innerHTML = '<div class="research-news-state">当前标的暂无相关新闻</div>';
      return;
    }
    list.innerHTML = items.map((item) => {
      const sentiment = String(item.sentiment || "").trim();
      const sentimentClass = /positive|bull|利好|正面/i.test(sentiment)
        ? "positive"
        : /negative|bear|利空|负面/i.test(sentiment)
        ? "negative"
        : "neutral";
      const sentimentLabel = sentiment || "未标注情绪";
      const labels = Array.isArray(item.labels)
        ? item.labels.slice(0, 4).map((label) => `<span>${this.escape(label)}</span>`).join("")
        : "";
      const title = item.url
        ? `<a href="${this.safeUrl(item.url)}" target="_blank" rel="noopener noreferrer">${this.escape(item.title)}</a>`
        : `<strong>${this.escape(item.title)}</strong>`;
      const originalTitle = item.original_title
        ? `<small class="research-news-original">${this.escape(item.original_title)}</small>`
        : "";
      return `<article class="research-news-card">
        <div class="research-news-main">
          <div class="research-news-tags"><span class="kind">${this.escape(item.kind || "资讯")}</span>${labels}</div>
          <h3>${title}</h3>
          ${originalTitle}
          <p>${this.escape(item.summary || "该条新闻暂无摘要。")}</p>
        </div>
        <footer>
          <strong>${this.escape(item.source || "老虎证券资讯")}</strong>
          <time>${this.escape(item.published_at || "时间未知")}</time>
          <span class="sentiment ${sentimentClass}">${this.escape(sentimentLabel)}</span>
        </footer>
      </article>`;
    }).join("");
  }

  renderStrategyIndicators(scan) {
    const strategyItems = Array.isArray(scan?.items) ? scan.items : [];
    const featureScan = scan?.prediction_features || {};
    const featureItems = Array.isArray(featureScan.items) ? featureScan.items : [];
    const items = [...strategyItems, ...featureItems];
    this.state.modal.indicators = items;
    const timeframeLabel = { "15m": "15 分", "1h": "1 小时", "4h": "4 小时" }[scan?.timeframe || this.state.modal.tf] || this.state.modal.tf;
    const caption = this.q("#strategy-indicator-caption");
    const featureCaption = this.q("#prediction-feature-caption");
    this.q("#indicator-total-caption").textContent = `共 ${Number(scan?.total_count) || items.length || 20} 项 · K 线策略与预测引擎同源展示`;
    if (!items.length) {
      caption.textContent = `最新一根 K 线：${timeframeLabel} · 12 项指标暂不可用`;
      this.q("#strategy-indicator-list").innerHTML = `<span class="strategy-indicator-loading error">${this.escape(scan?.error || "暂无足够数据")}</span>`;
      featureCaption.textContent = "最新预测快照：-- · 8 项因子暂不可用";
      this.q("#prediction-feature-list").innerHTML = `<span class="strategy-indicator-loading error">${this.escape(scan?.error || "暂无预测快照")}</span>`;
      this.q("#strategy-indicator-detail").innerHTML = "";
      return;
    }
    caption.textContent = `最新一根 K 线：${timeframeLabel} · 已触发 ${Number(scan.triggered_count) || 0}/${Number(scan.count) || strategyItems.length} · 基于 ${Number(scan.candle_count) || 0} 根历史 K 线`;
    const quality = featureScan.quality_score == null ? Number.NaN : Number(featureScan.quality_score);
    const qualityLabel = Number.isFinite(quality) ? `${(quality * 100).toFixed(1)}%` : "--";
    const snapshotTime = featureScan.as_of_ms ? this.barTimeString(featureScan.as_of_ms) : "--";
    featureCaption.textContent = `最新预测快照：${snapshotTime} · 偏多 ${Number(featureScan.bullish_count) || 0} / 偏空 ${Number(featureScan.bearish_count) || 0} / 中性 ${Number(featureScan.neutral_count) || 0} · 质量 ${qualityLabel}`;
    const selectedExists = items.some((item) => item.key === this.state.modal.selectedIndicator);
    if (!selectedExists) {
      this.state.modal.selectedIndicator = strategyItems.find((item) => item.triggered)?.key || featureItems.find((item) => ["bullish", "bearish"].includes(item.status))?.key || items[0].key;
    }
    const renderItems = (list) => list.map((item) => {
      const selected = item.key === this.state.modal.selectedIndicator;
      const statusLabel = this.indicatorStatusLabel(item.status);
      return `<button type="button" role="tab" aria-selected="${selected}" class="strategy-indicator-chip ${this.escape(item.status)} ${selected ? "on" : ""}" data-strategy-indicator="${this.escape(item.key)}"><i aria-hidden="true"></i><span>${this.escape(item.name)}</span><small>${statusLabel}</small></button>`;
    }).join("");
    this.q("#strategy-indicator-list").innerHTML = renderItems(strategyItems) || '<span class="strategy-indicator-loading error">K 线策略暂不可用</span>';
    this.q("#prediction-feature-list").innerHTML = renderItems(featureItems) || '<span class="strategy-indicator-loading error">暂无预测快照</span>';
    this.renderStrategyIndicatorSelection();
  }

  indicatorStatusLabel(status, detail = false) {
    const labels = detail
      ? { triggered: "最新一根已触发", not_triggered: "最新一根未触发", bullish: "当前偏多", bearish: "当前偏空", neutral: "当前中性", insufficient: "数据不足" }
      : { triggered: "已触发", not_triggered: "未触发", bullish: "偏多", bearish: "偏空", neutral: "中性", insufficient: "数据不足" };
    return labels[status] || "未触发";
  }

  renderStrategyIndicatorSelection() {
    const items = this.state.modal.indicators || [];
    const selected = items.find((item) => item.key === this.state.modal.selectedIndicator) || items[0];
    this.qa("[data-strategy-indicator]").forEach((button) => {
      const active = button.dataset.strategyIndicator === selected?.key;
      button.classList.toggle("on", active);
      button.setAttribute("aria-selected", String(active));
    });
    const detail = this.q("#strategy-indicator-detail");
    if (!selected) {
      detail.innerHTML = "";
      return;
    }
    const statusLabel = this.indicatorStatusLabel(selected.status, true);
    const metrics = (selected.metrics || []).map((metric) => `<article><span>${this.escape(metric.label)}</span><strong>${this.escape(metric.value)}</strong></article>`).join("");
    detail.innerHTML = `<article class="strategy-indicator-detail-card ${this.escape(selected.status)}">
      <header><div><strong>${this.escape(selected.name)}</strong><span>${this.escape(selected.category || "策略指标")}</span></div><b>${statusLabel}</b></header>
      <p>${this.escape(selected.description || "")}</p>
      <div class="strategy-indicator-summary">${this.escape(selected.summary || "")}</div>
      <div class="strategy-indicator-metrics">${metrics}</div>
    </article>`;
  }

  renderModalSummary(overview, klines = [], report = null, opportunity = null) {
    const numeric = (value) => value == null || value === "" || !Number.isFinite(Number(value)) ? null : Number(value);
    const setMetric = (selector, value, tone = "") => {
      const element = this.q(selector);
      element.textContent = value;
      element.classList.remove("metric-up", "metric-down", "metric-neutral");
      if (tone) element.classList.add(`metric-${tone}`);
    };
    const underlying = String(overview.underlying || overview.annotation || "");
    const equityMapped = /stock|equity|tradfi/i.test(underlying);
    this.q("#modal-market").textContent = equityMapped ? "美股映射合约" : "永续合约";
    const transport = this.state.modal.marketTransport || "idle";
    const transportLabels = {
      websocket: "WS 实时（自动更新）",
      ws_rest_fallback: "WS 推送 · 上游 REST 备选",
      ws_cache: "WS 推送 · 服务器缓存备选",
      rest: "REST 备选轮询",
      snapshot: "机会快照",
      connecting: "正在连接 WS 实时行情",
      reconnecting: "WS 重连中 · REST 继续补位",
      idle: "等待实时行情",
    };
    const tickerSource = this.state.modal.tickerTransport === "websocket" ? "价格 WS" : "";
    const depthTransport = String(this.state.modal.depthTransport || "");
    const depthSource = depthTransport.startsWith("websocket") ? "盘口 WS" : depthTransport.startsWith("rest") ? "盘口 REST" : "";
    const sourceParts = [tickerSource, depthSource].filter(Boolean).join(" / ");
    const updatedAt = Number(this.state.modal.marketUpdatedAt) || 0;
    const updatedLabel = updatedAt ? new Date(updatedAt).toLocaleTimeString("zh-CN", { hour12: false }) : "--";
    const sourceElement = this.q("#modal-source");
    sourceElement.dataset.transport = transport;
    sourceElement.textContent = `数据源：Binance Futures · ${equityMapped ? "美股映射 USDT 合约" : "USDT 永续合约"} · ${transportLabels[transport] || "行情备选"}${sourceParts ? ` · ${sourceParts}` : ""} · 更新 ${updatedLabel} · 量化结果仅供研究${this.state.modal.overviewError ? ` · ${this.state.modal.overviewError}` : ""}`;

    const pct = numeric(overview.pct_24h);
    const pctTone = pct != null ? (pct > 0 ? "up" : pct < 0 ? "down" : "neutral") : "neutral";
    setMetric("#modal-metric-price", this.formatPrice(overview.price));
    setMetric("#modal-metric-change", this.formatPercent(overview.pct_24h), pctTone);
    const quoteVolume = numeric(overview.quote_volume);
    setMetric("#modal-metric-volume", quoteVolume == null ? "--" : `${this.formatCompact(quoteVolume)} USDT`);

    const bidDepth = numeric(overview.bid_depth_notional);
    const askDepth = numeric(overview.ask_depth_notional);
    const depth = bidDepth != null && askDepth != null ? bidDepth + askDepth : null;
    setMetric("#modal-metric-depth", depth == null ? "--" : `${this.formatCompact(depth)} USDT`);

    const battle = overview.battle?.["5m"];
    const battleText = battle && battle.state !== "data_insufficient"
      ? `多 ${Number(battle.long || 0).toFixed(0)} / 空 ${Number(battle.short || 0).toFixed(0)}`
      : "数据采集中";
    const battleTone = battle?.result === "long" ? "up" : battle?.result === "short" ? "down" : "neutral";
    setMetric("#modal-metric-battle", battleText, battleTone);

    const matchedOpportunity = opportunity || overview.opportunity;
    const quality = numeric(matchedOpportunity?.quality_score);
    const score = numeric(report?.combined ?? overview.score);
    const qualityValue = quality != null ? quality.toFixed(1) : score != null ? this.formatScore(score) : "--";
    const qualityTone = quality != null
      ? (matchedOpportunity?.direction === "long" ? "up" : matchedOpportunity?.direction === "short" ? "down" : "neutral")
      : score != null ? (score > 15 ? "up" : score < -15 ? "down" : "neutral") : "neutral";
    setMetric("#modal-metric-quality", qualityValue, qualityTone);
    this.q("#modal-metric-quality-note").textContent = quality != null
      ? `${this.opportunityLabel(matchedOpportunity.direction)} · ${matchedOpportunity.status || "有效"}`
      : score != null ? "综合量化评分" : "等待策略扫描";

    if (!Array.isArray(klines) || klines.length === 0) return;
    const current = klines[klines.length - 1];
    const previous = klines.length > 1 ? klines[klines.length - 2] : current;
    const barChange = Number(previous.close) ? (Number(current.close) / Number(previous.close) - 1) * 100 : null;
    const timeframeLabel = { "15m": "15 分", "1h": "1 小时", "4h": "4 小时" }[this.state.modal.tf] || this.state.modal.tf;
    this.q("#modal-ohlc").innerHTML = `
      <strong>${this.escape(timeframeLabel)} · ${this.barTimeString(current.open_time || current.ts || current.time)}</strong>
      <span>开 <b>${this.formatPrice(current.open)}</b></span>
      <span>高 <b>${this.formatPrice(current.high)}</b></span>
      <span>低 <b>${this.formatPrice(current.low)}</b></span>
      <span>收 <b>${this.formatPrice(current.close)}</b></span>
      <span class="${barChange > 0 ? "up" : barChange < 0 ? "down" : "flat"}">${barChange == null ? "--" : this.formatPercent(barChange)}</span>
      <span>量 <b>${this.formatCompact(current.volume)}</b></span>`;
  }

  renderBattle(battles) {
    const order = ["5m", "15m", "1h"];
    const cards = order.map((horizon) => {
      const battle = battles[horizon];
      if (!battle) return `<article class="battle-horizon pending"><div><strong>${horizon}</strong><span>数据采集中</span></div></article>`;
      const reasons = (battle.reason_codes || []).map((code) => `<span>${this.escape(this.reasonLabel(code))}</span>`).join("");
      const costText = battle.edge_after_cost_bps == null
        ? "账户费率待同步"
        : `扣费后边际 ${Number(battle.edge_after_cost_bps) >= 0 ? "+" : ""}${Number(battle.edge_after_cost_bps).toFixed(1)} bps`;
      const freshness = battle.stale ? "已过期" : "有效";
      return `<article class="battle-horizon ${this.battleTone(battle.result, battle.state)}">
        <div class="battle-horizon-head"><strong>${horizon}</strong><b>${this.battleLabel(battle.result, battle.state)}</b><em>${battle.state === "heuristic" ? "启发式未校准" : "数据不足"}</em></div>
        <div class="battle-probabilities"><span class="long">多 ${Number(battle.long).toFixed(1)}%</span><span>震荡 ${Number(battle.neutral).toFixed(1)}%</span><span class="short">空 ${Number(battle.short).toFixed(1)}%</span></div>
        <svg class="battle-bar large" viewBox="0 0 100 6" preserveAspectRatio="none" aria-hidden="true"><rect class="long" x="0" y="0" width="${Number(battle.long) || 0}" height="6"></rect><rect class="neutral" x="${Number(battle.long) || 0}" y="0" width="${Number(battle.neutral) || 0}" height="6"></rect><rect class="short" x="${(Number(battle.long) || 0) + (Number(battle.neutral) || 0)}" y="0" width="${Number(battle.short) || 0}" height="6"></rect></svg>
        <div class="battle-meta"><span>置信度 ${this.escape(battle.confidence || "低")}</span><span>${this.escape(costText)}</span><span>${freshness}</span></div>
        <div class="battle-reasons">${reasons}</div>
      </article>`;
    }).join("");
    this.q("#battle-detail").innerHTML = `<div class="battle-title"><strong>多空博弈预测</strong><span>仅用于 Shadow 验证，不触发自动下单</span></div><div class="battle-grid">${cards}</div>`;
  }

  renderOpportunity(opportunity) {
    const container = this.q("#opportunity-detail");
    if (!opportunity) {
      container.innerHTML = '<div class="opportunity-empty">当前没有有效的市场机会，旧评分仅作指标参考。</div>';
      return;
    }
    const direction = opportunity.direction;
    const reasons = (opportunity.reason_codes || []).map((code) => `<span class="chip">${this.escape(code)}</span>`).join("");
    const conditions = Object.entries(opportunity.conditions || {}).map(([name, passed]) =>
      `<span class="condition ${passed ? "passed" : "pending"}">${passed ? "✓" : "○"} ${this.escape(name)}</span>`
    ).join("");
    const strategies = (opportunity.matched_strategies || []).map((strategy) =>
      `<span class="strategy-match">${this.escape(strategy.name)} · v${Number(strategy.version) || 1}</span>`
    ).join("") || '<span class="dim">当前用户没有匹配此方向的已发布完整策略</span>';
    const state = opportunity.user_state;
    const shadowLocked = this.state.intelligence?.shadow_execution?.live_locked !== false;
    container.innerHTML = `<section class="opportunity-card ${this.escape(direction)}">
      <div class="opportunity-head">
        <div><span class="opportunity-direction ${this.opportunityTone(direction)}">${this.opportunityLabel(direction)}</span><strong>质量 ${Number(opportunity.quality_score).toFixed(1)}</strong><span class="chip">${this.escape(opportunity.status)}</span></div>
        <div class="opportunity-actions">
          ${direction === "neutral" ? "" : shadowLocked
            ? '<button type="button" disabled title="Shadow 执行后端尚未启用">Shadow锁定</button>'
            : '<button type="button" data-opportunity-action="shadow">Shadow验证</button>'}
          <button type="button" data-opportunity-action="watch" class="${state === "watching" ? "on" : ""}">关注并提醒</button>
          <button type="button" data-opportunity-action="ignore" class="${state === "ignored" ? "on" : ""}">忽略</button>
          ${state ? '<button type="button" data-opportunity-action="clear">清除偏好</button>' : ""}
        </div>
      </div>
      <div class="opportunity-summary">${this.escape(opportunity.summary || "可解释市场机会")}</div>
      <div class="opportunity-meta"><span>成本后排序 ${opportunity.expected_value_score == null ? "--" : `${Number(opportunity.expected_value_score).toFixed(1)} bps`}</span><span>扫描器 ${this.escape(opportunity.scanner_key || "--")}</span></div>
      <div class="opportunity-meta"><span>发现：${this.barTimeString(opportunity.detected_bar_time)}</span><span>有效至：${this.barTimeString(opportunity.expires_bar_time)}</span></div>
      <div class="condition-list">${conditions}</div>
      <div class="reason-list">${reasons}</div>
      <div class="strategy-list"><strong>匹配完整策略</strong>${strategies}</div>
    </section>`;
  }

  async setOpportunityPreference(action) {
    const opportunity = this.state.modal.opportunity;
    if (!opportunity || !["watch", "ignore", "clear"].includes(action)) return;
    try {
      await this.api(`/opportunities/${encodeURIComponent(opportunity.id)}/preference`, {
        method: "POST",
        body: JSON.stringify({ action, notify_enabled: action === "watch" }),
      });
      await Promise.all([this.refreshModal(), this.pollAlerts()]);
    } catch (error) {
      this.showError(error.message || "机会偏好保存失败");
    }
  }

  async createShadowIntent() {
    const opportunity = this.state.modal.opportunity;
    if (!opportunity) return;
    try {
      const result = await this.api(`/opportunities/${encodeURIComponent(opportunity.id)}/shadow`, {
        method: "POST",
        body: JSON.stringify({ notional_usdt: 100, leverage: 1 }),
      });
      this.showError("");
      window.alert(result.state === "approved"
        ? "Shadow 意图已通过风控，系统不会向 Binance 发送真实订单。"
        : `Shadow 意图被风控拒绝：${(result.reason_codes || []).join(", ")}`);
      await this.pollIntelligence();
    } catch (error) {
      this.showError(error.message || "Shadow 意图创建失败");
    }
  }

  renderScoreSummary(scores, report) {
    const chips = Object.entries(scores).map(([timeframe, data]) =>
      `<span class="chip score ${this.tone(data.score)}">${timeframe}: ${data.score > 0 ? "+" : ""}${data.score}</span>`
    ).join("");
    const stats = Object.entries(report.stats || {}).map(([name, value]) =>
      `<span class="chip dim">${this.escape(name)}：${this.escape(value)}</span>`
    ).join("");
    this.q("#score-summary").innerHTML = `
      <span class="score-big score ${this.tone(report.combined)}">${this.escape(report.label)}</span>
      <span class="dim">综合评分 ${report.combined == null ? "--" : `${report.combined > 0 ? "+" : ""}${report.combined}`}</span>
      ${chips}${stats}`;
  }

  renderReport(report) {
    const horizons = (report.horizons || []).map((horizon) => {
      const border = horizon.score > 15 ? "up-border" : horizon.score < -15 ? "down-border" : "";
      const levels = horizon.levels ? `<div class="levels">${Object.entries(horizon.levels).map(([name, value]) =>
        `<span class="dim">${this.escape(name)}：<strong>${this.escape(value)}</strong></span>`
      ).join("")}</div>` : "";
      const basis = (horizon.basis || []).map((item) => `<li>${this.escape(item)}</li>`).join("");
      const news = (horizon.news || []).map((item) => `<li>
        <a href="${this.safeUrl(item.link)}" target="_blank" rel="noopener noreferrer">${this.escape(item.title_zh || item.title)}</a>
        <span class="sentiment ${this.escape(item.sentiment)}">${({ bull: "偏多", bear: "偏空", neutral: "中性" })[item.sentiment] || ""}</span>
        <small class="dim">${this.escape(({ eligible: "可参考", risk_only: "仅风控", observe: "观察", display_only: "仅展示", blocked: "禁用" })[item.reference_status] || "仅展示")}</small>
      </li>`).join("") || '<li class="dim">近 48 小时无相关资讯</li>';
      return `<article class="horizon ${border}">
        <div class="horizon-head"><span>${this.escape(horizon.name)}</span><strong class="score ${this.tone(horizon.score)}">${this.escape(horizon.suggestion)}</strong>${horizon.score == null ? "" : `<span class="dim">周期分 ${horizon.score > 0 ? "+" : ""}${horizon.score}</span>`}</div>
        ${levels}
        <div class="horizon-section"><div class="section-title">理论依据</div><ul>${basis}</ul></div>
        <div class="horizon-section"><div class="section-title">多轮新闻研判（不参与当前评分）</div><ul>${news}</ul></div>
      </article>`;
    }).join("");
    this.q("#report").innerHTML = `${horizons}<div class="disclaimer">${this.escape(report.news_policy || "")}<br>${this.escape(report.disclaimer)}</div>`;
  }

  renderFactors(current) {
    this.q("#factors").innerHTML = current ? current.factors.map((factor) => {
      const contribution = factor.weight === 0 ? "参考" : factor.contribution > 0 ? `+${factor.contribution}` : `${factor.contribution}`;
      return `<article class="factor"><strong>${this.escape(factor.name)}</strong><strong class="score ${this.tone(factor.contribution)}">${contribution}</strong><span>${this.escape(factor.reason)} <small class="dim">（${factor.weight === 0 ? "信息项" : `权重 ${factor.weight}`}）</small></span></article>`;
    }).join("") : '<div class="empty">该周期评分尚未生成</div>';
  }

  async toggleSymbolWatchlist(symbol) {
    if (!symbol) return;
    const next = new Set(this.state.watchlist);
    if (next.has(symbol)) next.delete(symbol);
    else next.add(symbol);
    try {
      await this.saveWatchlist(next);
      this.showError("");
    } catch (error) {
      this.showError(`自选保存失败：${error.message || "请稍后重试"}`);
    }
  }

  async toggleWatchlist() {
    const symbol = this.state.modal.symbol;
    await this.toggleSymbolWatchlist(symbol);
    const selected = this.state.overview.find((item) => item.symbol === symbol);
    this.q("#modal-watch").textContent = selected?.watch ? "★" : "☆";
    this.q("#modal-watch").classList.toggle("on", Boolean(selected?.watch));
  }

  setChartData(rawKlines) {
    const realKlines = (Array.isArray(rawKlines) ? rawKlines : []).map((item) => ({
      ...item,
      open_time: Number(item.open_time || item.ts || item.time || 0),
      open: Number(item.open),
      high: Number(item.high),
      low: Number(item.low),
      close: Number(item.close),
      volume: Number(item.volume) || 0,
    })).filter((item) => item.open_time > 0 && [item.open, item.high, item.low, item.close].every(Number.isFinite));
    const closes = realKlines.map((item) => item.close);
    const volumes = realKlines.map((item) => item.volume);
    const ma20 = this.chartMovingAverage(closes, 20);
    const ma50 = this.chartMovingAverage(closes, 50);
    const realSeries = {
      ma20,
      ma50,
      ma60: this.chartMovingAverage(closes, 60),
      volma20: this.chartMovingAverage(volumes, 20),
      boll: this.chartBollinger(closes, 20, 2),
    };
    const projection = this.buildPredictionProjection(realKlines, this.state.modal.predictionContext);
    const futureCandles = projection && !projection.historical
      ? projection.candles.map((item) => ({ ...item, simulated: true }))
      : [];
    const klines = [...realKlines, ...futureCandles];
    const extension = Array(futureCandles.length).fill(null);
    const series = {
      ma20: [...realSeries.ma20, ...extension],
      ma50: [...realSeries.ma50, ...extension],
      ma60: [...realSeries.ma60, ...extension],
      volma20: [...realSeries.volma20, ...extension],
      boll: {
        middle: [...realSeries.boll.middle, ...extension],
        upper: [...realSeries.boll.upper, ...extension],
        lower: [...realSeries.boll.lower, ...extension],
      },
    };
    this.state.chart.klines = klines;
    this.state.chart.realCount = realKlines.length;
    this.state.chart.series = series;
    this.state.chart.signals = this.buildChartSignals(realKlines, realSeries);
    this.state.chart.projection = projection;
    this.state.chart.visibleCount = Math.min(90, klines.length || 90);
    this.state.chart.rightOffset = 0;
    this.state.chart.hoverIndex = null;
    this.state.chart.hoverY = null;
    this.q("#chart-tooltip").classList.add("hidden");
    this.renderPredictionProjection(projection);
    this.drawChart();
  }

  buildPredictionProjection(klines, context) {
    if (!context || !klines.length) return null;
    const timeframeMs = { "15m": 900_000, "1h": 3_600_000, "4h": 14_400_000 }[this.state.modal.tf] || 3_600_000;
    const signalTime = this.normalizeChartTime(new Date(context.signalTime).getTime());
    const expiresAt = this.normalizeChartTime(new Date(context.expiresAt).getTime());
    const validityMs = expiresAt > signalTime ? expiresAt - signalTime : timeframeMs * 2;
    const horizonBars = Math.max(1, Math.min(12, Math.ceil(validityMs / timeframeMs)));
    const historical = context.historical === true;
    const anchorIndex = historical ? this.nearestChartIndex(klines, signalTime) : klines.length - 1;
    if (anchorIndex < 0) return null;
    const anchor = klines[anchorIndex];
    const anchorPrice = context.entryPrice && Math.abs(context.entryPrice / anchor.close - 1) < 0.2
      ? context.entryPrice
      : anchor.close;
    const sampleStart = Math.max(1, anchorIndex - 30);
    const returns = [];
    for (let index = sampleStart; index <= anchorIndex; index += 1) {
      const previous = Number(klines[index - 1]?.close);
      const current = Number(klines[index]?.close);
      if (previous > 0 && current > 0) returns.push(current / previous - 1);
    }
    const volatility = Math.max(0.001, Math.min(0.05, returns.length
      ? Math.sqrt(returns.reduce((total, value) => total + value ** 2, 0) / returns.length)
      : 0.005));
    const confidence = context.combinedScore / 100;
    const newsEdge = Math.max(0, (context.newsScore - 50) / 50);
    const indicatorEdge = context.indicatorScore / 100;
    let magnitude = volatility * Math.sqrt(horizonBars) * (0.55 + confidence * 0.75)
      + newsEdge * 0.003 + indicatorEdge * 0.004;
    magnitude = Math.max(0.002, Math.min(0.06, magnitude));
    if (!context.technicalConfirmed) magnitude *= 0.78;
    const targetReturn = (context.direction === "short" ? -1 : 1) * magnitude;
    const uncertainty = Math.max(
      volatility * Math.sqrt(horizonBars) * 1.25,
      magnitude * (1 - confidence) * 1.1 + volatility * 0.7,
    );
    const averageVolume = klines.slice(Math.max(0, anchorIndex - 20), anchorIndex + 1)
      .reduce((total, item) => total + (Number(item.volume) || 0), 0) / Math.max(1, Math.min(21, anchorIndex + 1));
    const phase = (context.newsScore + context.indicatorScore) / 100 * Math.PI;
    const candles = [];
    let previousClose = anchorPrice;
    for (let step = 1; step <= horizonBars; step += 1) {
      const progress = step / horizonBars;
      const eased = progress * progress * (3 - 2 * progress);
      const wave = Math.sin(progress * Math.PI * 2 + phase) * volatility * 0.22 * (1 - progress * 0.35);
      const close = anchorPrice * (1 + targetReturn * eased + wave);
      const open = previousClose;
      const wick = anchorPrice * volatility * (0.42 + 0.12 * Math.cos(step + phase));
      const openTime = this.normalizeChartTime(anchor.open_time) + timeframeMs * step;
      const index = historical ? this.nearestChartIndex(klines, openTime) : anchorIndex + step;
      if (index >= 0) {
        candles.push({
          index,
          open_time: openTime,
          open,
          high: Math.max(open, close) + Math.abs(wick),
          low: Math.min(open, close) - Math.abs(wick),
          close,
          volume: averageVolume * (0.52 + confidence * 0.24 + progress * 0.12),
          upper: anchorPrice * (1 + targetReturn * eased + uncertainty * Math.sqrt(progress)),
          lower: anchorPrice * (1 + targetReturn * eased - uncertainty * Math.sqrt(progress)),
          simulated: true,
          projectionStep: step,
        });
      }
      previousClose = close;
    }
    if (!candles.length) return null;
    const last = candles[candles.length - 1];
    const replay = historical
      ? this.evaluatePredictionProjection(klines, candles, anchorPrice, context)
      : null;
    return {
      historical,
      direction: context.direction,
      confidence,
      combinedScore: context.combinedScore,
      newsScore: context.newsScore,
      indicatorScore: context.indicatorScore,
      technicalConfirmed: context.technicalConfirmed,
      anchorIndex,
      anchorPrice,
      targetPrice: last.close,
      targetReturnPct: targetReturn * 100,
      rangeLow: last.lower,
      rangeHigh: last.upper,
      horizonBars,
      expiresAt: context.expiresAt,
      candles,
      replay,
    };
  }

  evaluatePredictionProjection(klines, candles, anchorPrice, context) {
    const samples = candles.map((prediction) => {
      const actual = klines[prediction.index];
      if (!actual || !Number.isFinite(Number(actual.close)) || Number(actual.close) <= 0) return null;
      const actualClose = Number(actual.close);
      const halfBand = Math.max(Math.abs(Number(prediction.upper) - Number(prediction.lower)) / 2, anchorPrice * 0.002);
      const priceError = Math.abs(Number(prediction.close) - actualClose);
      const pathFit = Math.max(0, 1 - priceError / halfBand);
      const directionOf = (value) => Math.abs(value / anchorPrice - 1) < 0.0002 ? 0 : Math.sign(value - anchorPrice);
      return {
        pathFit,
        directionMatched: directionOf(Number(prediction.close)) === directionOf(actualClose),
        priceErrorPct: priceError / actualClose * 100,
        actualClose,
      };
    }).filter(Boolean);
    if (!samples.length) return null;
    const pathAccuracy = samples.reduce((total, item) => total + item.pathFit, 0) / samples.length * 100;
    const directionAccuracy = samples.filter((item) => item.directionMatched).length / samples.length * 100;
    const accuracy = pathAccuracy * 0.7 + directionAccuracy * 0.3;
    const endpoint = samples[samples.length - 1];
    return {
      accuracy: Math.round(accuracy * 10) / 10,
      pathAccuracy: Math.round(pathAccuracy * 10) / 10,
      directionAccuracy: Math.round(directionAccuracy * 10) / 10,
      endpointErrorPct: Math.round(endpoint.priceErrorPct * 100) / 100,
      actualEndPrice: endpoint.actualClose,
      sampleCount: samples.length,
      outcomeResult: context.outcomeResult,
      directionalReturnBps: context.directionalReturnBps,
    };
  }

  renderPredictionProjection(projection) {
    const note = this.q("#chart-projection-note");
    if (!projection) {
      note.classList.add("hidden");
      note.innerHTML = "";
      return;
    }
    const direction = projection.direction === "short" ? "做空" : "做多";
    const directionClass = projection.direction === "short" ? "down" : "up";
    const mode = projection.historical ? "历史信号情景复盘" : "当前信号未来模拟";
    const replay = projection.replay;
    const resultLabel = ({ win: "命中", loss: "未命中", flat: "持平", unavailable: "行情不足" })[replay?.outcomeResult] || "行情不足";
    const replayMetrics = replay
      ? `<span class="projection-accuracy">推演准确率 <b>${replay.accuracy.toFixed(1)}%</b></span><span>路径贴合 <b>${replay.pathAccuracy.toFixed(1)}%</b></span><span>方向一致 <b>${replay.directionAccuracy.toFixed(1)}%</b></span><span>结算结果 <b class="replay-${this.escape(replay.outcomeResult)}">${resultLabel}</b></span>`
      : "";
    const replayCaption = replay
      ? `复盘 ${replay.sampleCount} 根真实 K 线 · 准确率 = 70% 路径贴合 + 30% 逐根方向一致 · 终点价格偏差 ${replay.endpointErrorPct.toFixed(2)}%`
      : `${projection.horizonBars} 根 ${this.escape(this.state.modal.tf)} 模拟 K 线 · ${projection.technicalConfirmed ? "技术已确认" : "新闻候选，技术未完全确认"}`;
    note.innerHTML = `<strong>◆ ${mode}</strong><span>预测方向 <b class="${directionClass}">${direction}</b></span><span>组合评分 <b>${projection.combinedScore.toFixed(1)}</b></span><span>目标变化 <b class="${directionClass}">${this.formatPercent(projection.targetReturnPct)}</b></span><span>模拟目标 <b>${this.formatPrice(projection.targetPrice)}</b></span>${replayMetrics}<span>情景区间 <b>${this.formatPrice(projection.rangeLow)} — ${this.formatPrice(projection.rangeHigh)}</b></span><small>${replayCaption} · 紫色区域均为模型情景，不是真实行情</small>`;
    note.classList.remove("hidden");
  }

  chartMovingAverage(values, period) {
    const output = Array(values.length).fill(null);
    let total = 0;
    for (let index = 0; index < values.length; index += 1) {
      total += Number(values[index]) || 0;
      if (index >= period) total -= Number(values[index - period]) || 0;
      if (index >= period - 1) output[index] = total / period;
    }
    return output;
  }

  chartBollinger(values, period, multiplier) {
    const middle = this.chartMovingAverage(values, period);
    const upper = Array(values.length).fill(null);
    const lower = Array(values.length).fill(null);
    for (let index = period - 1; index < values.length; index += 1) {
      const mean = middle[index];
      let variance = 0;
      for (let offset = index - period + 1; offset <= index; offset += 1) {
        variance += (values[offset] - mean) ** 2;
      }
      const deviation = Math.sqrt(variance / period) * multiplier;
      upper[index] = mean + deviation;
      lower[index] = mean - deviation;
    }
    return { middle, upper, lower };
  }

  normalizeChartTime(value) {
    const numeric = Number(value) || 0;
    return numeric >= 100000000000 ? numeric : numeric * 1000;
  }

  nearestChartIndex(klines, timestamp) {
    const target = this.normalizeChartTime(timestamp);
    if (!target || !klines.length) return -1;
    const first = this.normalizeChartTime(klines[0].open_time);
    const last = this.normalizeChartTime(klines[klines.length - 1].open_time);
    const interval = klines.length > 1
      ? Math.max(1, this.normalizeChartTime(klines[1].open_time) - first)
      : 3_600_000;
    if (target < first || target >= last + interval) return -1;
    let low = 0;
    let high = klines.length - 1;
    while (low <= high) {
      const middle = Math.floor((low + high) / 2);
      const time = this.normalizeChartTime(klines[middle].open_time);
      if (time <= target) low = middle + 1;
      else high = middle - 1;
    }
    return Math.max(0, high);
  }

  buildChartSignals(klines, series) {
    const grouped = new Map();
    const add = (index, side, name, source, summary = "", quality = null) => {
      if (index < 0 || index >= klines.length || !["buy", "sell"].includes(side)) return;
      const key = `${index}:${side}`;
      const signal = grouped.get(key) || { index, side, names: [], sources: [], summaries: [], quality: null };
      if (name && !signal.names.includes(name)) signal.names.push(name);
      if (source && !signal.sources.includes(source)) signal.sources.push(source);
      if (summary && !signal.summaries.includes(summary)) signal.summaries.push(summary);
      if (Number.isFinite(Number(quality))) signal.quality = Math.max(signal.quality || 0, Number(quality));
      grouped.set(key, signal);
    };

    for (let index = 1; index < klines.length; index += 1) {
      const previousMa20 = series.ma20[index - 1];
      const previousMa60 = series.ma60[index - 1];
      const ma20 = series.ma20[index];
      const ma60 = series.ma60[index];
      if ([previousMa20, previousMa60, ma20, ma60].every(Number.isFinite)) {
        if (previousMa20 <= previousMa60 && ma20 > ma60) {
          add(index, "buy", "MA金叉", "MA20 上穿 MA60", "与下方 MA金叉策略指标使用相同周期");
        } else if (previousMa20 >= previousMa60 && ma20 < ma60) {
          add(index, "sell", "MA死叉", "MA20 下穿 MA60", "MA金叉规则的反向离场信号");
        }
      }
      const upper = series.boll.upper[index];
      const lower = series.boll.lower[index];
      const previousUpper = series.boll.upper[index - 1];
      const previousLower = series.boll.lower[index - 1];
      if ([upper, previousUpper].every(Number.isFinite)
        && klines[index - 1].close <= previousUpper && klines[index].close > upper) {
        add(index, "buy", "布林突破", "收盘突破 BOLL 上轨", "与下方布林突破策略指标使用相同规则");
      }
      if ([lower, previousLower].every(Number.isFinite)
        && klines[index - 1].close >= previousLower && klines[index].close < lower) {
        add(index, "sell", "布林跌破", "收盘跌破 BOLL 下轨", "布林突破规则的反向离场信号");
      }
    }
    return [...grouped.values()].sort((left, right) => left.index - right.index || left.side.localeCompare(right.side));
  }

  resetChartViewport() {
    const chart = this.state.chart;
    chart.visibleCount = Math.min(90, chart.klines.length || 90);
    chart.rightOffset = 0;
    chart.hoverIndex = null;
    chart.hoverY = null;
    this.q("#chart-tooltip").classList.add("hidden");
    this.drawChart();
  }

  clampChartViewport() {
    const chart = this.state.chart;
    const maximumVisible = Math.max(1, chart.klines.length);
    const minimumVisible = Math.min(24, maximumVisible);
    chart.visibleCount = Math.max(minimumVisible, Math.min(maximumVisible, Math.round(chart.visibleCount || maximumVisible)));
    chart.rightOffset = Math.max(0, Math.min(Math.max(0, maximumVisible - chart.visibleCount), Math.round(chart.rightOffset || 0)));
  }

  handleChartPointerDown(event) {
    if (event.button !== 0 || !this.state.chart.klines.length) return;
    event.preventDefault();
    const canvas = this.q("#chart");
    canvas.focus({ preventScroll: true });
    canvas.setPointerCapture?.(event.pointerId);
    this.state.chart.dragging = true;
    this.state.chart.dragStartX = event.clientX;
    this.state.chart.dragStartOffset = this.state.chart.rightOffset;
    canvas.classList.add("dragging");
  }

  handleChartPointerMove(event) {
    const chart = this.state.chart;
    const geometry = this.chartGeometry;
    if (!geometry || !chart.klines.length) return;
    if (chart.dragging) {
      const delta = event.clientX - chart.dragStartX;
      chart.rightOffset = chart.dragStartOffset + Math.round(delta / Math.max(1, geometry.candleStep));
      this.clampChartViewport();
      chart.hoverIndex = null;
      this.q("#chart-tooltip").classList.add("hidden");
      this.drawChart();
      return;
    }
    const bounds = this.q("#chart").getBoundingClientRect();
    const pointX = event.clientX - bounds.left;
    const pointY = event.clientY - bounds.top;
    if (pointX < geometry.padding.left || pointX > geometry.plotRight
      || pointY < geometry.padding.top || pointY > geometry.volumeBottom) {
      if (chart.hoverIndex != null) {
        chart.hoverIndex = null;
        chart.hoverY = null;
        this.q("#chart-tooltip").classList.add("hidden");
        this.drawChart();
      }
      return;
    }
    const localIndex = Math.max(0, Math.min(geometry.visible.length - 1,
      Math.floor((pointX - geometry.padding.left) / geometry.candleStep)));
    chart.hoverIndex = geometry.start + localIndex;
    chart.hoverY = pointY;
    this.drawChart();
  }

  handleChartPointerUp(event) {
    const canvas = this.q("#chart");
    if (this.state.chart.dragging) canvas.releasePointerCapture?.(event.pointerId);
    this.state.chart.dragging = false;
    canvas.classList.remove("dragging");
  }

  handleChartPointerLeave() {
    if (this.state.chart.dragging) return;
    this.state.chart.hoverIndex = null;
    this.state.chart.hoverY = null;
    this.q("#chart-tooltip").classList.add("hidden");
    const klines = this.state.chart.klines;
    if (klines.length) this.renderChartOhlc(Math.max(0, (this.state.chart.realCount || klines.length) - 1));
    this.drawChart();
  }

  handleChartWheel(event) {
    if (!this.state.chart.klines.length) return;
    event.preventDefault();
    const chart = this.state.chart;
    if (event.shiftKey || Math.abs(event.deltaX) > Math.abs(event.deltaY)) {
      chart.rightOffset += Math.sign(event.deltaX || event.deltaY) * 6;
      this.clampChartViewport();
      this.drawChart();
      return;
    }
    const oldVisible = chart.visibleCount;
    const bounds = this.q("#chart").getBoundingClientRect();
    const geometry = this.chartGeometry;
    const ratio = geometry
      ? Math.max(0, Math.min(1, (event.clientX - bounds.left - geometry.padding.left) / Math.max(1, geometry.plotWidth)))
      : 0.5;
    const oldEnd = chart.klines.length - chart.rightOffset;
    const oldStart = Math.max(0, oldEnd - oldVisible);
    const anchor = oldStart + ratio * oldVisible;
    chart.visibleCount += event.deltaY > 0 ? 12 : -12;
    this.clampChartViewport();
    const nextStart = anchor - ratio * chart.visibleCount;
    chart.rightOffset = chart.klines.length - (nextStart + chart.visibleCount);
    this.clampChartViewport();
    this.drawChart();
  }

  handleChartKeydown(event) {
    const chart = this.state.chart;
    const key = event.key;
    if (!["ArrowLeft", "ArrowRight", "+", "=", "-", "_", "Home"].includes(key)) return;
    event.preventDefault();
    if (key === "Home") {
      this.resetChartViewport();
      return;
    }
    if (key === "ArrowLeft") chart.rightOffset += 5;
    else if (key === "ArrowRight") chart.rightOffset -= 5;
    else chart.visibleCount += ["+", "="].includes(key) ? -10 : 10;
    this.clampChartViewport();
    this.drawChart();
  }

  chartTimeLabel(timestamp, detailed = false) {
    const date = new Date(this.normalizeChartTime(timestamp));
    if (!Number.isFinite(date.getTime())) return "--";
    const options = detailed
      ? { year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false }
      : { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false };
    return date.toLocaleString("zh-CN", options).replace(/\//g, "-");
  }

  renderChartOhlc(index) {
    const klines = this.state.chart.klines;
    const current = klines[index];
    if (!current) return;
    const previous = index > 0 ? klines[index - 1] : current;
    const barChange = Number(previous.close) ? (current.close / previous.close - 1) * 100 : null;
    const timeframeLabel = { "15m": "15 分", "1h": "1 小时", "4h": "4 小时" }[this.state.modal.tf] || this.state.modal.tf;
    this.q("#modal-ohlc").innerHTML = `
      <strong>${current.simulated ? "AI 模拟" : this.escape(timeframeLabel)} · ${this.chartTimeLabel(current.open_time, true)}</strong>
      <span>开 <b>${this.formatPrice(current.open)}</b></span>
      <span>高 <b>${this.formatPrice(current.high)}</b></span>
      <span>低 <b>${this.formatPrice(current.low)}</b></span>
      <span>收 <b>${this.formatPrice(current.close)}</b></span>
      <span class="${barChange > 0 ? "up" : barChange < 0 ? "down" : "flat"}">${barChange == null ? "--" : this.formatPercent(barChange)}</span>
      <span>${current.simulated ? "模拟量" : "量"} <b>${this.formatCompact(current.volume)}</b></span>
      ${current.simulated ? '<span class="projection-warning">非真实行情</span>' : ""}`;
  }

  updateChartTooltip(geometry) {
    const tooltip = this.q("#chart-tooltip");
    const index = this.state.chart.hoverIndex;
    const candle = this.state.chart.klines[index];
    if (index == null || !candle || index < geometry.start || index >= geometry.end) {
      tooltip.classList.add("hidden");
      tooltip.setAttribute("aria-hidden", "true");
      return;
    }
    const previous = index > 0 ? this.state.chart.klines[index - 1] : candle;
    const change = previous.close ? (candle.close / previous.close - 1) * 100 : 0;
    const signals = this.state.chart.signals.filter((signal) => signal.index === index);
    const projectionCandle = this.state.chart.projection?.candles.find((item) => item.index === index);
    const signalRows = signals.map((signal) => `<div class="tooltip-signal ${signal.side}"><b>${signal.side === "buy" ? "买" : "卖"}</b><span><em>历史触发</em>${this.escape(signal.names.join(" / "))}</span></div>`).join("");
    tooltip.innerHTML = `
      <strong>${candle.simulated ? "AI 模拟 · " : ""}${this.chartTimeLabel(candle.open_time, true)}</strong>
      <div class="tooltip-ohlc"><span>开 ${this.formatPrice(candle.open)}</span><span>高 ${this.formatPrice(candle.high)}</span><span>低 ${this.formatPrice(candle.low)}</span><span>收 ${this.formatPrice(candle.close)}</span></div>
      <div class="tooltip-change ${change > 0 ? "up" : change < 0 ? "down" : "flat"}">${this.formatPercent(change)} <span>${candle.simulated ? "模拟量" : "成交量"} ${this.formatCompact(candle.volume)}</span></div>
      ${projectionCandle ? `<div class="tooltip-projection"><b>预测路径</b><span>模拟收盘 ${this.formatPrice(projectionCandle.close)}</span><small>${this.formatPrice(projectionCandle.lower)} — ${this.formatPrice(projectionCandle.upper)}${this.state.chart.projection?.historical ? ` · 与真实收盘偏差 ${(Math.abs(projectionCandle.close / candle.close - 1) * 100).toFixed(2)}%` : ""} · 非真实行情</small></div>` : ""}
      ${signalRows}`;
    const localIndex = index - geometry.start;
    const candleX = geometry.x(localIndex);
    const positionLeft = candleX > geometry.width * 0.64 ? candleX - 220 : candleX + 14;
    const pointerY = Math.max(geometry.padding.top + 8, Math.min(geometry.priceBottom - 108, this.state.chart.hoverY || geometry.padding.top + 30));
    tooltip.style.left = `${Math.max(8, positionLeft)}px`;
    tooltip.style.top = `${pointerY}px`;
    tooltip.classList.remove("hidden");
    tooltip.setAttribute("aria-hidden", "false");
    this.renderChartOhlc(index);
  }

  drawChart() {
    const canvas = this.q("#chart");
    if (!canvas) return;
    const context = canvas.getContext("2d");
    const bounds = canvas.getBoundingClientRect();
    const width = Math.max(320, Math.round(bounds.width || this.q("#chart-stage")?.clientWidth || 1280));
    const height = width < 700 ? 370 : 500;
    const density = Math.min(2, Math.max(1, window.devicePixelRatio || 1));
    canvas.width = Math.round(width * density);
    canvas.height = Math.round(height * density);
    canvas.style.height = `${height}px`;
    context.setTransform(density, 0, 0, density, 0, 0);
    context.clearRect(0, 0, width, height);
    const chart = this.state.chart;
    if (!chart.klines.length || !chart.series) {
      context.fillStyle = "#77808f";
      context.font = "19.2px sans-serif";
      context.fillText("数据加载中…", 20, 30);
      return;
    }
    this.clampChartViewport();
    const end = chart.klines.length - chart.rightOffset;
    const start = Math.max(0, end - chart.visibleCount);
    const visible = chart.klines.slice(start, end);
    const lightTheme = document.documentElement.dataset.theme === "light";
    const colors = lightTheme ? {
      grid: "#dfe5df", text: "#66736d", up: "#159f70", down: "#d5535c", ma20: "#a27b00", ma50: "#7657a8",
      boll: "#0e9fb5", projection: "#7657b8", projectionFill: "rgba(118,87,184,.13)", volumeUp: "rgba(21,159,112,.42)", volumeDown: "rgba(213,83,92,.38)", cross: "#8e9c96",
    } : {
      grid: "#203330", text: "#809795", up: "#2ebd85", down: "#f6465d", ma20: "#d2af32", ma50: "#9a79ce",
      boll: "#2ec7d3", projection: "#b19cff", projectionFill: "rgba(177,156,255,.12)", volumeUp: "rgba(46,189,133,.38)", volumeDown: "rgba(246,70,93,.32)", cross: "#7d9490",
    };
    const padding = { left: 14, right: 72, top: 18, bottom: 30 };
    const volumeHeight = width < 700 ? 64 : 86;
    const volumeGap = 16;
    const plotRight = width - padding.right;
    const plotWidth = plotRight - padding.left;
    const volumeBottom = height - padding.bottom;
    const volumeTop = volumeBottom - volumeHeight;
    const priceBottom = volumeTop - volumeGap;
    const visibleProjection = chart.overlays.has("projection")
      ? (chart.projection?.candles || []).filter((item) => item.index >= start && item.index < end)
      : [];
    const projectionHighs = visibleProjection.flatMap((item) => [item.high, item.upper]);
    const projectionLows = visibleProjection.flatMap((item) => [item.low, item.lower]);
    const rawHigh = Math.max(...visible.map((item) => item.high), ...projectionHighs);
    const rawLow = Math.min(...visible.map((item) => item.low), ...projectionLows);
    const rawRange = rawHigh - rawLow || Math.max(Math.abs(rawHigh) * 0.01, 1);
    const high = rawHigh + rawRange * 0.08;
    const low = rawLow - rawRange * 0.08;
    const range = high - low || 1;
    const candleStep = plotWidth / Math.max(1, visible.length);
    const x = (localIndex) => padding.left + (localIndex + 0.5) * candleStep;
    const y = (value) => padding.top + (high - value) / range * (priceBottom - padding.top);
    const maxVolume = Math.max(...visible.map((item) => item.volume), 1);
    const volumeY = (value) => volumeBottom - Math.max(0, Number(value) || 0) / maxVolume * volumeHeight;
    const geometry = { width, height, padding, plotRight, plotWidth, priceBottom, volumeTop, volumeBottom, candleStep, start, end, visible, x, y, high, low, range };
    this.chartGeometry = geometry;

    context.font = "16px ui-monospace, SFMono-Regular, Consolas, monospace";
    context.lineWidth = 1;
    for (let tick = 0; tick <= 5; tick += 1) {
      const value = high - range * tick / 5;
      const yValue = y(value);
      context.strokeStyle = colors.grid;
      context.beginPath();
      context.moveTo(padding.left, yValue);
      context.lineTo(plotRight, yValue);
      context.stroke();
      context.fillStyle = colors.text;
      context.textAlign = "left";
      context.fillText(this.formatPrice(value), plotRight + 7, yValue + 3);
    }
    context.strokeStyle = colors.grid;
    context.beginPath();
    context.moveTo(padding.left, volumeTop - volumeGap / 2);
    context.lineTo(plotRight, volumeTop - volumeGap / 2);
    context.stroke();

    const timeTickCount = width < 700 ? 4 : 7;
    for (let tick = 0; tick < timeTickCount; tick += 1) {
      const localIndex = Math.min(visible.length - 1, Math.round(tick * (visible.length - 1) / Math.max(1, timeTickCount - 1)));
      const xValue = x(localIndex);
      context.strokeStyle = colors.grid;
      context.globalAlpha = 0.38;
      context.beginPath();
      context.moveTo(xValue, padding.top);
      context.lineTo(xValue, volumeBottom);
      context.stroke();
      context.globalAlpha = 1;
      context.fillStyle = colors.text;
      context.textAlign = tick === 0 ? "left" : tick === timeTickCount - 1 ? "right" : "center";
      context.fillText(this.chartTimeLabel(visible[localIndex].open_time), xValue, height - 10);
    }

    const candleWidth = Math.max(2, Math.min(13, candleStep * 0.64));
    visible.forEach((item, localIndex) => {
      if (item.simulated) {
        context.fillStyle = colors.projectionFill;
        context.fillRect(x(localIndex) - candleWidth / 2, volumeY(item.volume), candleWidth, Math.max(1, volumeBottom - volumeY(item.volume)));
        return;
      }
      const rising = item.close >= item.open;
      context.fillStyle = rising ? colors.volumeUp : colors.volumeDown;
      context.fillRect(x(localIndex) - candleWidth / 2, volumeY(item.volume), candleWidth, Math.max(1, volumeBottom - volumeY(item.volume)));
    });

    const plotLine = (values, color, valueY = y, lineWidth = 1.35, dash = []) => {
      context.save();
      context.strokeStyle = color;
      context.lineWidth = lineWidth;
      context.setLineDash(dash);
      context.beginPath();
      let started = false;
      for (let globalIndex = start; globalIndex < end; globalIndex += 1) {
        const value = values[globalIndex];
        if (!Number.isFinite(value)) {
          started = false;
          continue;
        }
        const localIndex = globalIndex - start;
        if (started) context.lineTo(x(localIndex), valueY(value));
        else context.moveTo(x(localIndex), valueY(value));
        started = true;
      }
      context.stroke();
      context.restore();
    };

    if (chart.overlays.has("volma")) plotLine(chart.series.volma20, "#f0a000", volumeY, 1.4);
    if (chart.overlays.has("boll")) {
      const upperPoints = [];
      const lowerPoints = [];
      for (let globalIndex = start; globalIndex < end; globalIndex += 1) {
        const upper = chart.series.boll.upper[globalIndex];
        const lowerBand = chart.series.boll.lower[globalIndex];
        if (Number.isFinite(upper) && Number.isFinite(lowerBand)) {
          upperPoints.push([x(globalIndex - start), y(upper)]);
          lowerPoints.push([x(globalIndex - start), y(lowerBand)]);
        }
      }
      if (upperPoints.length > 1) {
        context.save();
        context.fillStyle = colors.boll;
        context.globalAlpha = 0.055;
        context.beginPath();
        upperPoints.forEach(([pointX, pointY], index) => index ? context.lineTo(pointX, pointY) : context.moveTo(pointX, pointY));
        lowerPoints.reverse().forEach(([pointX, pointY]) => context.lineTo(pointX, pointY));
        context.closePath();
        context.fill();
        context.restore();
      }
      plotLine(chart.series.boll.upper, colors.boll, y, 1.1, [4, 3]);
      plotLine(chart.series.boll.lower, colors.boll, y, 1.1, [4, 3]);
    }

    visible.forEach((item, localIndex) => {
      if (item.simulated) return;
      const rising = item.close >= item.open;
      const color = rising ? colors.up : colors.down;
      context.strokeStyle = color;
      context.fillStyle = color;
      context.lineWidth = 1;
      context.beginPath();
      context.moveTo(x(localIndex), y(item.high));
      context.lineTo(x(localIndex), y(item.low));
      context.stroke();
      const top = y(Math.max(item.open, item.close));
      const bottom = y(Math.min(item.open, item.close));
      context.fillRect(x(localIndex) - candleWidth / 2, top, candleWidth, Math.max(1.3, bottom - top));
    });
    if (chart.overlays.has("ma20")) plotLine(chart.series.ma20, colors.ma20, y, 1.55);
    if (chart.overlays.has("ma50")) plotLine(chart.series.ma50, colors.ma50, y, 1.55);
    if (chart.overlays.has("ma60")) plotLine(chart.series.ma60, lightTheme ? "#b84b8d" : "#e378b4", y, 1.35, [6, 3]);

    if (visibleProjection.length) {
      const projection = chart.projection;
      const points = visibleProjection.map((item) => ({
        ...item,
        pointX: x(item.index - start),
        closeY: y(item.close),
        upperY: y(item.upper),
        lowerY: y(item.lower),
      }));
      const firstIndex = Math.max(start, Math.min(...points.map((item) => item.index)));
      const lastIndex = Math.min(end - 1, Math.max(...points.map((item) => item.index)));
      context.save();
      context.fillStyle = colors.projectionFill;
      context.fillRect(
        Math.max(padding.left, x(firstIndex - start) - candleStep * 0.58),
        padding.top,
        Math.max(candleStep, x(lastIndex - start) - x(firstIndex - start) + candleStep * 1.16),
        priceBottom - padding.top,
      );
      context.beginPath();
      points.forEach((item, index) => index ? context.lineTo(item.pointX, item.upperY) : context.moveTo(item.pointX, item.upperY));
      [...points].reverse().forEach((item) => context.lineTo(item.pointX, item.lowerY));
      context.closePath();
      context.fillStyle = colors.projectionFill;
      context.globalAlpha = 1;
      context.fill();

      const anchorVisible = projection.anchorIndex >= start && projection.anchorIndex < end;
      const pathStartX = anchorVisible ? x(projection.anchorIndex - start) : points[0].pointX;
      const pathStartY = anchorVisible ? y(projection.anchorPrice) : points[0].closeY;
      context.strokeStyle = colors.projection;
      context.lineWidth = 2;
      context.setLineDash([7, 4]);
      context.beginPath();
      context.moveTo(pathStartX, pathStartY);
      points.forEach((item) => context.lineTo(item.pointX, item.closeY));
      context.stroke();

      const projectionCandleWidth = Math.max(3, Math.min(12, candleStep * 0.56));
      points.forEach((item) => {
        context.strokeStyle = colors.projection;
        context.fillStyle = colors.projectionFill;
        context.lineWidth = 1.3;
        context.setLineDash([3, 2]);
        context.beginPath();
        context.moveTo(item.pointX, y(item.high));
        context.lineTo(item.pointX, y(item.low));
        context.stroke();
        const top = y(Math.max(item.open, item.close));
        const bottom = y(Math.min(item.open, item.close));
        context.fillRect(item.pointX - projectionCandleWidth / 2, top, projectionCandleWidth, Math.max(2, bottom - top));
        context.strokeRect(item.pointX - projectionCandleWidth / 2, top, projectionCandleWidth, Math.max(2, bottom - top));
      });
      const endpoint = points[points.length - 1];
      context.setLineDash([]);
      context.fillStyle = colors.projection;
      context.beginPath();
      context.arc(endpoint.pointX, endpoint.closeY, 4, 0, Math.PI * 2);
      context.fill();
      context.font = "bold 13px sans-serif";
      context.textAlign = endpoint.pointX > plotRight - 150 ? "right" : "left";
      context.fillText(
        projection.replay
          ? `推演准确率 ${projection.replay.accuracy.toFixed(1)}%`
          : `模拟 ${projection.targetReturnPct >= 0 ? "+" : ""}${projection.targetReturnPct.toFixed(2)}%`,
        endpoint.pointX + (endpoint.pointX > plotRight - 150 ? -7 : 7),
        Math.max(padding.top + 14, endpoint.closeY - 9),
      );
      if (anchorVisible) {
        const separatorX = x(projection.anchorIndex - start) + candleStep * 0.5;
        context.strokeStyle = colors.projection;
        context.globalAlpha = 0.72;
        context.setLineDash([4, 4]);
        context.beginPath();
        context.moveTo(separatorX, padding.top);
        context.lineTo(separatorX, priceBottom);
        context.stroke();
        context.globalAlpha = 1;
        context.setLineDash([]);
        context.fillStyle = colors.projection;
        context.textAlign = "left";
        context.fillText(projection.historical ? "历史预测模拟" : "未来预测模拟", Math.min(plotRight - 100, separatorX + 6), padding.top + 14);
      }
      context.restore();
    }

    const visibleSignals = chart.overlays.has("signals")
      ? chart.signals.filter((signal) => signal.index >= start && signal.index < end)
      : [];
    visibleSignals.forEach((signal) => {
      const item = chart.klines[signal.index];
      const localIndex = signal.index - start;
      const markerX = x(localIndex);
      const markerY = signal.side === "buy"
        ? Math.min(priceBottom - 15, y(item.low) + 13)
        : Math.max(padding.top + 15, y(item.high) - 13);
      const color = signal.side === "buy" ? colors.up : colors.down;
      context.fillStyle = color;
      context.strokeStyle = lightTheme ? "#ffffff" : "#0d181b";
      context.lineWidth = 1;
      context.beginPath();
      if (signal.side === "buy") {
        context.moveTo(markerX, markerY - 6);
        context.lineTo(markerX - 5, markerY + 4);
        context.lineTo(markerX + 5, markerY + 4);
      } else {
        context.moveTo(markerX, markerY + 6);
        context.lineTo(markerX - 5, markerY - 4);
        context.lineTo(markerX + 5, markerY - 4);
      }
      context.closePath();
      context.fill();
      context.stroke();
      context.font = "bold 14.4px sans-serif";
      context.textAlign = "center";
      context.fillStyle = color;
      context.fillText(signal.side === "buy" ? "买" : "卖", markerX, markerY + (signal.side === "buy" ? 15 : -9));
    });

    if (chart.hoverIndex != null && chart.hoverIndex >= start && chart.hoverIndex < end) {
      const localIndex = chart.hoverIndex - start;
      const crossX = x(localIndex);
      const crossY = Math.max(padding.top, Math.min(priceBottom, chart.hoverY || y(chart.klines[chart.hoverIndex].close)));
      context.save();
      context.strokeStyle = colors.cross;
      context.setLineDash([4, 4]);
      context.beginPath();
      context.moveTo(crossX, padding.top);
      context.lineTo(crossX, volumeBottom);
      context.moveTo(padding.left, crossY);
      context.lineTo(plotRight, crossY);
      context.stroke();
      context.restore();
      const hoverPrice = high - (crossY - padding.top) / Math.max(1, priceBottom - padding.top) * range;
      context.fillStyle = lightTheme ? "#52615b" : "#294642";
      context.fillRect(plotRight + 2, crossY - 9, padding.right - 4, 18);
      context.fillStyle = "#f4fbf8";
      context.textAlign = "center";
      context.font = "16px ui-monospace, monospace";
      context.fillText(this.formatPrice(hoverPrice), plotRight + padding.right / 2, crossY + 3);
    }

    const rangeSignalCount = chart.signals.filter((signal) => signal.index >= start && signal.index < end).length;
    const projectionText = visibleProjection.length ? ` · ${visibleProjection.length} 根预测模拟` : "";
    this.q("#chart-range").textContent = `${this.chartTimeLabel(visible[0].open_time)} — ${this.chartTimeLabel(visible[visible.length - 1].open_time)} · ${visible.length}/${chart.klines.length} 根 · ${rangeSignalCount} 个历史信号${projectionText}`;
    this.updateChartTooltip(geometry);
  }
}

window.quantdeskRegisterPageController("contract-monitor", ContractMonitor);
