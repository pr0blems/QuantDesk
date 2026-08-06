class ContractMonitor extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
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
      modal: { symbol: null, tf: "1h", opportunity: null, indicators: [], selectedIndicator: null },
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
      algorithm: { data: null, loading: false },
      matrixSort: { key: "ai", direction: "desc" },
      sound: true,
      notifyOn: false,
    };
    this.timers = [];
    this.newsTimer = null;
    this.running = false;
    this.audioContext = null;
    this.renderShell();
  }

  connectedCallback() {
    this.bindEvents();
  }

  disconnectedCallback() {
    this.pause();
  }

  renderShell() {
    this.shadowRoot.innerHTML = `
      <link rel="stylesheet" href="/assets/monitor.css?v=20260806-9">
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

          <section class="research-metrics" aria-label="标的关键行情">
            <article><span>最新价</span><strong id="modal-metric-price">--</strong><small>实时合约报价</small></article>
            <article><span>24h 涨跌</span><strong id="modal-metric-change">--</strong><small id="modal-metric-change-note">相对 24 小时前</small></article>
            <article><span>24h 成交额</span><strong id="modal-metric-volume">--</strong><small>计价资产口径</small></article>
            <article><span>订单池深度</span><strong id="modal-metric-depth">--</strong><small>买卖盘合计</small></article>
            <article><span>5m 博弈</span><strong id="modal-metric-battle">--</strong><small>多空概率对比</small></article>
            <article><span>机会质量</span><strong id="modal-metric-quality">--</strong><small id="modal-metric-quality-note">等待策略扫描</small></article>
          </section>

          <nav class="research-tabs" aria-label="研究内容导航">
            <button class="on" type="button" data-modal-section="#modal-trend">趋势</button>
            <button type="button" data-modal-section="#modal-indicator-section">策略指标</button>
            <button type="button" data-modal-section="#modal-battle-section">多空预测</button>
            <button type="button" data-modal-section="#modal-strategy-section">策略机会</button>
            <button type="button" data-modal-section="#modal-report-section">研判报告</button>
            <button type="button" data-modal-section="#modal-factor-section">评分因子</button>
          </nav>

          <section id="modal-trend" class="research-section research-trend">
            <div class="research-section-head trend-toolbar">
              <div>
                <strong>趋势与成交量</strong>
                <span>蜡烛图 · MA20 · MA50</span>
              </div>
              <span class="tf-switch" aria-label="图表周期">
                <button data-tf="15m" type="button">15 分</button>
                <button data-tf="1h" class="on" type="button">1 小时</button>
                <button data-tf="4h" type="button">4 小时</button>
              </span>
            </div>
            <div id="modal-ohlc" class="research-ohlc"><span>正在加载当前周期行情…</span></div>
            <canvas id="chart" class="chart" width="1280" height="430" aria-label="标的 K 线、均线与成交量图"></canvas>
          </section>

          <section id="modal-indicator-section" class="research-section strategy-indicator-section">
            <div class="research-section-head">
              <div><strong>策略指标</strong><span id="strategy-indicator-caption">当前周期：-- · 正在计算 12 项</span></div>
            </div>
            <div id="strategy-indicator-list" class="strategy-indicator-list" role="tablist" aria-label="选择策略指标"></div>
            <div id="strategy-indicator-detail" class="strategy-indicator-detail"></div>
          </section>
          <section id="modal-battle-section" class="research-section">
            <div id="battle-detail" class="battle-detail"></div>
          </section>
          <section id="modal-strategy-section" class="research-section">
            <div class="research-section-head"><div><strong>策略机会</strong><span>条件、有效期与匹配策略</span></div></div>
            <div id="opportunity-detail" class="opportunity-detail"></div>
          </section>
          <section id="modal-report-section" class="research-section">
            <div class="research-section-head"><div><strong>多周期研判报告</strong><span>价格结构与新闻仅作辅助证据</span></div></div>
            <div id="score-summary" class="score-summary"></div>
            <div id="report" class="report"></div>
          </section>
          <section id="modal-factor-section" class="research-section">
            <div class="research-section-head"><div><strong>评分因子</strong><span>当前图表周期的因子贡献</span></div></div>
            <div id="factors" class="factors"></div>
          </section>
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
                <th>结算标签 / 状态</th><th>方向命中 / 成本后收益</th><th>最大有利 / 不利</th>
              </tr></thead>
              <tbody id="prediction-history-body"><tr><td colspan="11" class="history-empty">点击后加载历史记录</td></tr></tbody>
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
      <div id="prediction-algorithm-modal" class="modal hidden" aria-hidden="true">
        <div class="modal-box prediction-algorithm-box" role="dialog" aria-modal="true" aria-labelledby="prediction-algorithm-title">
          <div class="modal-head prediction-algorithm-head">
            <div>
              <strong id="prediction-algorithm-title" class="modal-symbol">当前预测算法</strong>
              <span id="prediction-algorithm-version" class="dim">正在读取配置…</span>
            </div>
            <button id="prediction-algorithm-close" type="button">关闭</button>
          </div>
          <section class="algorithm-rules" aria-label="算法规则说明">
            <article><strong>综合评分</strong><span>各特征标准化到 -1～+1，按周期权重加总，再扣除账户拥挤与资金费率拥挤惩罚。</span></article>
            <article><strong>方向判断</strong><span>评分达到正阈值判为看多，低于负阈值判为看空，阈值之间保持中性。</span></article>
            <article><strong>数据门槛</strong><span>数据质量不足、微观行情过期或持仓数据过期时强制中性，不参与方向命中率。</span></article>
            <article><strong>结算规则</strong><span>分别在 5m、15m、1h 观察窗口结算，方向收益扣除价差成本后大于 0 记为命中。</span></article>
          </section>
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
                  <tr><th>主动成交流</th><td><input type="number" data-algorithm-weight="aggressive_flow" data-horizon="5m"></td><td><input type="number" data-algorithm-weight="aggressive_flow" data-horizon="15m"></td><td><input type="number" data-algorithm-weight="aggressive_flow" data-horizon="1h"></td><td>主动买入与主动卖出强弱</td></tr>
                  <tr><th>订单簿失衡</th><td><input type="number" data-algorithm-weight="book_imbalance" data-horizon="5m"></td><td><input type="number" data-algorithm-weight="book_imbalance" data-horizon="15m"></td><td><input type="number" data-algorithm-weight="book_imbalance" data-horizon="1h"></td><td>完整深度买卖盘力量差</td></tr>
                  <tr><th>近五档失衡</th><td><input type="number" data-algorithm-weight="book_imbalance_5" data-horizon="5m"></td><td><input type="number" data-algorithm-weight="book_imbalance_5" data-horizon="15m"></td><td><input type="number" data-algorithm-weight="book_imbalance_5" data-horizon="1h"></td><td>盘口近端流动性倾斜</td></tr>
                  <tr><th>价格速度</th><td><input type="number" data-algorithm-weight="velocity" data-horizon="5m"></td><td><input type="number" data-algorithm-weight="velocity" data-horizon="15m"></td><td><input type="number" data-algorithm-weight="velocity" data-horizon="1h"></td><td>最近一分钟价格变化速度</td></tr>
                  <tr><th>闪动失衡</th><td><input type="number" data-algorithm-weight="flash_imbalance" data-horizon="5m"></td><td><input type="number" data-algorithm-weight="flash_imbalance" data-horizon="15m"></td><td><input type="number" data-algorithm-weight="flash_imbalance" data-horizon="1h"></td><td>30 分钟上涨与下跌闪动差</td></tr>
                  <tr><th>Taker 流向</th><td><input type="number" data-algorithm-weight="taker_flow" data-horizon="5m"></td><td><input type="number" data-algorithm-weight="taker_flow" data-horizon="15m"></td><td><input type="number" data-algorithm-weight="taker_flow" data-horizon="1h"></td><td>Binance 主动买卖量比</td></tr>
                  <tr><th>价格 × 持仓量</th><td><input type="number" data-algorithm-weight="price_oi_impulse" data-horizon="5m"></td><td><input type="number" data-algorithm-weight="price_oi_impulse" data-horizon="15m"></td><td><input type="number" data-algorithm-weight="price_oi_impulse" data-horizon="1h"></td><td>价格变化与未平仓量联合冲量</td></tr>
                  <tr><th>周期趋势</th><td><input type="number" data-algorithm-weight="trend" data-horizon="5m"></td><td><input type="number" data-algorithm-weight="trend" data-horizon="15m"></td><td><input type="number" data-algorithm-weight="trend" data-horizon="1h"></td><td>15m 趋势；1h 使用 1h 与 4h 组合</td></tr>
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
        const target = this.q(button.dataset.modalSection);
        const box = this.q(".research-modal");
        if (!target || !box) return;
        this.qa("[data-modal-section]").forEach((item) => item.classList.toggle("on", item === button));
        box.scrollTo({
          top: Math.max(0, target.offsetTop - 118),
          behavior: window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth",
        });
      });
    });
    this.qa(".tf-switch button").forEach((button) => {
      button.addEventListener("click", () => {
        this.state.modal.tf = button.dataset.tf;
        this.refreshModal();
      });
    });
    this.q("#modal-watch").addEventListener("click", () => this.toggleWatchlist());
    this.q("#prediction-history-close").addEventListener("click", () => this.closePredictionHistory());
    this.q("#prediction-history-modal").addEventListener("click", (event) => {
      if (event.target === this.q("#prediction-history-modal")) this.closePredictionHistory();
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
    this.q("#prediction-algorithm-modal").addEventListener("click", (event) => {
      if (event.target === this.q("#prediction-algorithm-modal")) this.closePredictionAlgorithm();
    });
    this.q("#prediction-algorithm-form").addEventListener("input", () => this.renderAlgorithmWeightSums());
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
    this.q("#strategy-indicator-list").addEventListener("click", (event) => {
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

  async openModal(symbol) {
    this.state.modal.symbol = symbol;
    const modal = this.q("#modal");
    modal.classList.remove("hidden");
    modal.setAttribute("aria-hidden", "false");
    this.q(".research-modal").scrollTop = 0;
    this.qa("[data-modal-section]").forEach((button, index) => button.classList.toggle("on", index === 0));
    this.q("#modal-close").focus({ preventScroll: true });
    await this.refreshModal();
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

  closeModal() {
    const modal = this.q("#modal");
    this.q("#modal-close").blur();
    modal.classList.add("hidden");
    modal.setAttribute("aria-hidden", "true");
  }

  async openPredictionHistory() {
    const modal = this.q("#prediction-history-modal");
    modal.classList.remove("hidden");
    modal.setAttribute("aria-hidden", "false");
    if (this.state.history.timeRange.startMs == null) this.setRecentPredictionHistoryRange(false);
    await this.loadPredictionHistory(1);
  }

  closePredictionHistory() {
    const modal = this.q("#prediction-history-modal");
    modal.classList.add("hidden");
    modal.setAttribute("aria-hidden", "true");
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

  async loadPredictionAlgorithm() {
    const message = this.q("#prediction-algorithm-message");
    this.state.algorithm.loading = true;
    message.textContent = "正在读取当前算法…";
    try {
      const data = await this.api("/prediction-algorithm");
      this.state.algorithm.data = data;
      this.populatePredictionAlgorithm(data.config);
      const source = data.source === "custom" ? "自定义配置" : "系统默认配置";
      const updated = data.updated_at ? ` · 更新于 ${new Date(data.updated_at).toLocaleString("zh-CN", { hour12: false })}` : "";
      this.q("#prediction-algorithm-version").textContent = `${data.model_key} v${data.model_version} · 配置版本 ${data.config_version} · ${source}${updated}`;
      this.qa("#prediction-algorithm-form input").forEach((input) => { input.disabled = !data.editable; });
      this.q("#prediction-algorithm-defaults").disabled = !data.editable;
      this.q("#prediction-algorithm-save").disabled = !data.editable;
      message.textContent = data.editable ? "修改后需保存才会生效。" : "当前账号可查看规则，但只有管理员可以调整全局算法。";
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
      input.step = "0.01";
      input.value = Number(config.weights?.[input.dataset.horizon]?.[input.dataset.algorithmWeight] || 0).toFixed(2);
    });
    this.renderAlgorithmWeightSums();
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
    const config = { weights: { "5m": {}, "15m": {}, "1h": {} } };
    this.qa("[data-algorithm-scalar]").forEach((input) => {
      config[input.dataset.algorithmScalar] = Number(input.value);
    });
    this.qa("[data-algorithm-weight]").forEach((input) => {
      config.weights[input.dataset.horizon][input.dataset.algorithmWeight] = Number(input.value);
    });
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

  async savePredictionAlgorithm(event) {
    event.preventDefault();
    if (this.state.algorithm.loading || !this.state.algorithm.data?.editable) return;
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
      const data = await this.api("/prediction-algorithm", { method: "PUT", body: JSON.stringify(config) });
      this.state.algorithm.data = data;
      this.populatePredictionAlgorithm(data.config);
      this.q("#prediction-algorithm-version").textContent = `${data.model_key} v${data.model_version} · 配置版本 ${data.config_version} · 自定义配置`;
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
    body.innerHTML = '<tr><td colspan="11" class="history-empty">正在加载历史预测…</td></tr>';
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
      body.innerHTML = `<tr><td colspan="11" class="history-empty history-error">${this.escape(error.message || "历史预测加载失败")}</td></tr>`;
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
      body.innerHTML = '<tr><td colspan="11" class="history-empty">暂无历史预测记录</td></tr>';
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
    const overview = this.state.overview.find((item) => item.symbol === symbol) || {};
    this.q("#modal-symbol").textContent = symbol;
    this.q("#modal-price").textContent = this.formatPrice(overview.price);
    this.q("#modal-pct").textContent = this.formatPercent(overview.pct_24h);
    this.q("#modal-pct").className = `research-change ${overview.pct_24h > 0 ? "up" : overview.pct_24h < 0 ? "down" : "flat"}`;
    this.q("#modal-watch").textContent = overview.watch ? "★" : "☆";
    this.q("#modal-watch").classList.toggle("on", Boolean(overview.watch));
    this.qa(".tf-switch button").forEach((button) => button.classList.toggle("on", button.dataset.tf === timeframe));
    this.renderModalSummary(overview);
    this.q("#modal-ohlc").innerHTML = "<span>正在加载当前周期行情…</span>";
    this.q("#strategy-indicator-caption").textContent = `当前周期：${timeframe} · 正在计算 12 项`;
    this.q("#strategy-indicator-list").innerHTML = '<span class="strategy-indicator-loading">策略指标计算中…</span>';
    this.q("#strategy-indicator-detail").innerHTML = "";
    try {
      const encoded = encodeURIComponent(symbol);
      const [klines, scores, report, opportunities, indicatorScan] = await Promise.all([
        this.api(`/klines?symbol=${encoded}&tf=${timeframe}&limit=120`),
        this.api(`/score?symbol=${encoded}`),
        this.api(`/report?symbol=${encoded}`),
        this.api(`/opportunities?symbol=${encoded}&limit=1&include_ignored=true`),
        this.api(`/strategy-indicators?symbol=${encoded}&tf=${timeframe}`).catch((error) => ({
          timeframe,
          count: 12,
          triggered_count: 0,
          items: [],
          error: error.message || "策略指标加载失败",
        })),
      ]);
      this.state.modal.opportunity = opportunities.items?.[0] || null;
      this.renderStrategyIndicators(indicatorScan);
      this.renderModalSummary(overview, klines, report, this.state.modal.opportunity);
      this.drawChart(this.q("#chart"), klines);
      this.renderBattle(overview.battle || {});
      this.renderOpportunity(this.state.modal.opportunity);
      this.renderScoreSummary(scores, report);
      this.renderReport(report);
      this.renderFactors(scores[timeframe]);
    } catch (error) {
      this.q("#modal-ohlc").innerHTML = '<span class="down">当前周期行情加载失败</span>';
      this.q("#report").innerHTML = `<div class="error-banner">${this.escape(error.message || "详情加载失败")}</div>`;
    }
  }

  renderStrategyIndicators(scan) {
    const items = Array.isArray(scan?.items) ? scan.items : [];
    this.state.modal.indicators = items;
    const timeframeLabel = { "15m": "15 分", "1h": "1 小时", "4h": "4 小时" }[scan?.timeframe || this.state.modal.tf] || this.state.modal.tf;
    const caption = this.q("#strategy-indicator-caption");
    if (!items.length) {
      caption.textContent = `当前周期：${timeframeLabel} · 12 项指标暂不可用`;
      this.q("#strategy-indicator-list").innerHTML = `<span class="strategy-indicator-loading error">${this.escape(scan?.error || "暂无足够数据")}</span>`;
      this.q("#strategy-indicator-detail").innerHTML = "";
      return;
    }
    caption.textContent = `当前周期：${timeframeLabel} · 已触发 ${Number(scan.triggered_count) || 0}/${Number(scan.count) || items.length} · ${Number(scan.candle_count) || 0} 根 K 线`;
    const selectedExists = items.some((item) => item.key === this.state.modal.selectedIndicator);
    if (!selectedExists) {
      this.state.modal.selectedIndicator = items.find((item) => item.triggered)?.key || items[0].key;
    }
    this.q("#strategy-indicator-list").innerHTML = items.map((item) => {
      const selected = item.key === this.state.modal.selectedIndicator;
      const statusLabel = item.status === "triggered" ? "已触发" : item.status === "insufficient" ? "数据不足" : "未触发";
      return `<button type="button" role="tab" aria-selected="${selected}" class="strategy-indicator-chip ${this.escape(item.status)} ${selected ? "on" : ""}" data-strategy-indicator="${this.escape(item.key)}"><i aria-hidden="true"></i><span>${this.escape(item.name)}</span><small>${statusLabel}</small></button>`;
    }).join("");
    this.renderStrategyIndicatorSelection();
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
    const statusLabel = selected.status === "triggered" ? "已触发" : selected.status === "insufficient" ? "数据不足" : "当前未触发";
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
    this.q("#modal-source").textContent = `数据源：Binance Futures · ${equityMapped ? "美股映射 USDT 合约" : "USDT 永续合约"} · 量化结果仅供研究`;

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

  drawChart(canvas, klines) {
    const context = canvas.getContext("2d");
    const bounds = canvas.getBoundingClientRect();
    const width = Math.max(320, Math.round(bounds.width || 1280));
    const height = width < 700 ? 330 : 430;
    const density = Math.min(2, Math.max(1, window.devicePixelRatio || 1));
    canvas.width = Math.round(width * density);
    canvas.height = Math.round(height * density);
    canvas.style.height = `${height}px`;
    context.setTransform(density, 0, 0, density, 0, 0);
    context.clearRect(0, 0, width, height);
    if (!klines.length) {
      context.fillStyle = "#77808f";
      context.font = "12px sans-serif";
      context.fillText("数据加载中…", 20, 30);
      return;
    }
    const lightTheme = document.documentElement.dataset.theme === "light";
    const padding = { left: 10, right: 70, top: 10, bottom: 46 };
    const volumeHeight = 46;
    const priceHeight = height - padding.top - padding.bottom - volumeHeight;
    const high = Math.max(...klines.map((item) => item.high));
    const low = Math.min(...klines.map((item) => item.low));
    const range = high - low || 1;
    const x = (index) => padding.left + (index + 0.5) * (width - padding.left - padding.right) / klines.length;
    const y = (value) => padding.top + (high - value) / range * priceHeight;
    context.lineWidth = 1;
    context.strokeStyle = lightTheme ? "#e1e5dd" : "#203330";
    context.fillStyle = lightTheme ? "#747c71" : "#809795";
    context.font = "11px sans-serif";
    for (let index = 0; index <= 4; index += 1) {
      const value = high - range * index / 4;
      const yValue = y(value);
      context.beginPath();
      context.moveTo(padding.left, yValue);
      context.lineTo(width - padding.right, yValue);
      context.stroke();
      context.fillText(this.formatPrice(value), width - padding.right + 6, yValue + 4);
    }
    const movingAverage = (period, color) => {
      context.strokeStyle = color;
      context.beginPath();
      let started = false;
      for (let index = period - 1; index < klines.length; index += 1) {
        let total = 0;
        for (let offset = index - period + 1; offset <= index; offset += 1) total += klines[offset].close;
        const yValue = y(total / period);
        if (started) context.lineTo(x(index), yValue);
        else context.moveTo(x(index), yValue);
        started = true;
      }
      context.stroke();
    };
    movingAverage(20, lightTheme ? "#9a7a14" : "#c7aa42");
    movingAverage(50, lightTheme ? "#7255aa" : "#9a79ce");
    const candleWidth = Math.max(2, (width - padding.left - padding.right) / klines.length * 0.6);
    const maxVolume = Math.max(...klines.map((item) => item.volume)) || 1;
    klines.forEach((item, index) => {
      const rising = item.close >= item.open;
      context.strokeStyle = rising ? "#2ebd85" : "#f6465d";
      context.fillStyle = context.strokeStyle;
      context.beginPath();
      context.moveTo(x(index), y(item.high));
      context.lineTo(x(index), y(item.low));
      context.stroke();
      const top = y(Math.max(item.open, item.close));
      const bottom = y(Math.min(item.open, item.close));
      context.fillRect(x(index) - candleWidth / 2, top, candleWidth, Math.max(1, bottom - top));
      context.globalAlpha = 0.5;
      const volume = item.volume / maxVolume * volumeHeight;
      context.fillRect(x(index) - candleWidth / 2, height - padding.bottom + volumeHeight - volume - 8, candleWidth, volume);
      context.globalAlpha = 1;
    });
  }
}

customElements.define("contract-monitor", ContractMonitor);
