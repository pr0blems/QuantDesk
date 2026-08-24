class AiMonitorDashboard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this.state = {
      view: "opportunities",
      overview: null,
      macroMarket: null,
      macroAi: null,
      macroAiLoading: false,
      macroAiError: "",
      config: null,
      scorePolicy: null,
      liveCopy: null,
      liveCopyError: "",
      liveCopyLoading: false,
      liveCopyConfigLoading: false,
      liveCopyHistory: null,
      liveCopyHistoryLoading: false,
      liveCopyHistoryError: "",
      manualFollowOpportunityId: "",
      manualFollowAttemptId: "",
      manualFollowLoading: false,
      uwToggleLoading: false,
      finnhubToggleLoading: false,
      indicators: [],
      indicatorTemplates: [],
      indicatorConflictPairs: [],
      symbols: [],
      news: [],
      runs: [],
      opportunities: [],
      opportunityCache: { current: [], history: [] },
      opportunityPages: { current: 1, history: 1 },
      opportunityPageSize: 20,
      opportunityPagination: { page: 1, page_size: 20, total: 0, total_pages: 1 },
      opportunityPaginationByTab: {},
      opportunityAnalytics: null,
      predictionFilters: {
        dateFrom: "",
        dateTo: "",
        symbol: "",
        newsScoreMin: 0,
        indicatorScoreMin: 0,
        combinedScoreMin: 0,
        optionFlowScoreMin: 0,
        gexScoreMin: 0,
        dataCoverageMin: 0,
        featureVersion: "",
        decisionVersion: "",
        settlementVersion: "current",
        direction: "all",
        marketSession: "all",
        quoteQuality: "all",
        eventRisk: "all",
        exitReason: "all",
      },
      predictionPage: 1,
      predictionPageSize: 20,
      displayLeverage: 10,
      predictionAnalyticsRequestId: 0,
      predictionAnalyticsLoading: false,
      predictionAnalyticsLastLoadedAt: 0,
      predictionReadinessLoading: false,
      draftSymbols: new Set(),
      symbolSearch: "",
      opportunityTab: "current",
      opportunityStatusFilter: "all",
      expandedOpportunityIds: new Set(),
      opportunityStatusCounts: { all: 0, candidate: 0, ready: 0, triggered: 0, blocked: 0, data_error: 0 },
      opportunityDirectionCounts: { long: 0, short: 0 },
      historyOpportunityDirectionCounts: { long: 0, short: 0 },
      historyOpportunitySettlementCounts: { total: 0, pending: 0, unavailable: 0 },
      opportunityRequestId: 0,
      opportunitiesLoading: false,
      opportunityLoadingTab: "",
      opportunitiesLoadedTab: "",
      liveStateLoading: false,
      marketContextLoading: false,
      fullLoadLoading: false,
      lastSuccessfulRefreshAt: null,
      lastRefreshError: "",
      incrementalUpdateCount: 0,
      updateStreamStatus: "idle",
      newsRenderSignature: "",
      running: false,
      busyRun: "",
      newsSearch: "",
      newsMode: "all",
      analyzingNewsIds: new Set(),
      conclusionView: "analysis",
    };
    this.timers = [];
    this.scrollPaused = false;
    this.eventsBound = false;
    this.conclusionPanels = { fundamentals: "", news: "", memory: "", market: "", analysis: "" };
    this.conclusionNewsRequestId = 0;
    this.conclusionMemoryRequestId = 0;
    this.conclusionFundamentalRequestId = 0;
    this.newsLogicFocus = null;
    this.newsModelCalls = [];
    this.newsModelCallIndex = 0;
    this.newsModelCallsRequestId = 0;
    this.newsSystemPromptFocus = null;
    this.newsSystemPromptDefault = "";
    this.newsSystemPromptIsCustom = false;
    this.newsSystemPromptRequestId = 0;
    this.historicalJudgmentFocus = null;
    this.historicalJudgmentRequestId = 0;
    this.scoreTrendFocus = null;
    this.scoreTrendOpportunity = null;
    this.orderBookFocus = null;
    this.orderBookOpportunity = null;
    this.orderBookSnapshot = null;
    this.orderBookPreviousSnapshot = null;
    this.orderBookLimit = 100;
    this.orderBookPaused = false;
    this.orderBookRequestId = 0;
    this.orderBookTimer = null;
    this.predictionAnalyticsAbortController = null;
    this.opportunitiesAbortController = null;
    this.newsScrollAnimationFrame = null;
    this.newsScrollLastTick = 0;
    this.updateStreamAbort = null;
    this.updateStreamRetryTimer = null;
    this.updateStreamRefreshTimer = null;
    this.updateStreamScopes = new Set();
    this.lastUpdateStreamEventId = "";
    this.handleVisibilityChange = () => {
      if (document.visibilityState === "visible" && this.state.running) this.loadLiveState();
    };
    this.renderShell();
    this.restorePredictionFiltersFromUrl();
    this.syncPredictionFilterInputs();
  }

  connectedCallback() {
    this.bindEvents();
    document.addEventListener("visibilitychange", this.handleVisibilityChange);
  }

  disconnectedCallback() {
    this.closeOrderBook(false);
    this.pause();
    document.removeEventListener("visibilitychange", this.handleVisibilityChange);
  }

  renderShell() {
    this.shadowRoot.innerHTML = `
      <link rel="stylesheet" href="/assets/ai-monitor.css?v=20260820-51">
      <div class="ai-monitor">
        <header class="ai-head">
          <div>
            <span class="eyebrow">DISCOVERED OPPORTUNITIES</span>
            <h1><i aria-hidden="true">✦</i> 发现机会</h1>
            <p>每 15 分钟分析最新 10 条新闻 × 美股技术指标确认 · 机会只生成预测，不执行真实交易</p>
          </div>
          <div class="ai-head-actions">
            <span id="ai-clock" class="ai-clock"></span>
            <span id="scheduler-state" class="status-badge idle">读取中</span>
            <button id="live-copy-toggle" class="live-copy-toggle loading" type="button" aria-pressed="false" aria-busy="true">
              <span class="live-copy-toggle-track" aria-hidden="true"><i></i></span>
              <span><b>实盘跟单</b><small>读取中</small></span>
            </button>
            <button id="live-copy-config-button" class="live-copy-config-button" type="button">跟单配置</button>
            <button id="live-copy-history-button" class="live-copy-config-button live-copy-history-button" type="button">跟单记录</button>
            <button id="uw-usage-toggle" class="uw-usage-toggle loading" type="button" aria-pressed="false" disabled>
              <span class="uw-toggle-track" aria-hidden="true"><i></i></span>
              <span><b>Unusual Whales</b><small>读取中</small></span>
            </button>
            <button id="finnhub-usage-toggle" class="market-data-toggle finnhub-usage-toggle loading" type="button" aria-pressed="false" disabled>
              <span class="market-data-toggle-track" aria-hidden="true"><i></i></span>
              <span><b>Finnhub 美股现货</b><small>读取中</small></span>
            </button>
            <button id="run-news" type="button">分析新闻</button>
            <button id="run-opportunity" class="primary-action" type="button">发现机会</button>
            <details class="ai-settings-menu">
              <summary>设置</summary>
              <div>
                <button id="open-news-config" type="button">新闻分析配置</button>
                <button id="open-weight-config" type="button">权重设置</button>
                <button id="open-config" type="button">指标配置</button>
              </div>
            </details>
            <button id="ai-refresh" type="button">刷新</button>
          </div>
        </header>
        <div id="ai-banner" class="ai-banner hidden" role="status"></div>
        <div id="live-copy-modal" class="live-copy-modal hidden" aria-hidden="true">
          <button class="live-copy-backdrop" type="button" data-live-copy-close aria-label="关闭实盘跟单设置"></button>
          <section class="live-copy-dialog" role="dialog" aria-modal="true" aria-labelledby="live-copy-title" tabindex="-1">
            <header>
              <div><span>AI SIGNAL LIVE EXECUTION</span><h2 id="live-copy-title">发现机会独立实盘跟单</h2><p>只执行本页“发现机会”产生的新信号；独立于实盘交易页的其他策略、账户开关和部署状态。</p></div>
              <button type="button" data-live-copy-close aria-label="关闭实盘跟单设置">×</button>
            </header>
            <div id="live-copy-body" class="live-copy-body"><div class="live-copy-loading">正在读取 AI 机会独立执行域…</div></div>
          </section>
        </div>
        <div id="live-copy-config-modal" class="live-copy-modal hidden" aria-hidden="true">
          <button class="live-copy-backdrop" type="button" data-live-copy-config-close aria-label="关闭实盘跟单配置"></button>
          <section class="live-copy-dialog live-copy-config-dialog" role="dialog" aria-modal="true" aria-labelledby="live-copy-config-title" tabindex="-1">
            <header>
              <div><span>ISOLATED LIVE RISK POLICY</span><h2 id="live-copy-config-title">实盘跟单配置</h2><p>只影响“发现机会”独立实盘账户的新开仓；不会修改其他策略。保存配置不会自动开启已关闭的跟单。</p></div>
              <button type="button" data-live-copy-config-close aria-label="关闭实盘跟单配置">×</button>
            </header>
            <div id="live-copy-config-body" class="live-copy-body"><div class="live-copy-loading">正在读取独立实盘风险参数…</div></div>
          </section>
        </div>
        <div id="live-copy-history-modal" class="live-copy-modal hidden" aria-hidden="true">
          <button class="live-copy-backdrop" type="button" data-live-copy-history-close aria-label="关闭跟单记录"></button>
          <section class="live-copy-dialog live-copy-history-dialog" role="dialog" aria-modal="true" aria-labelledby="live-copy-history-title" tabindex="-1">
            <header>
              <div><span>MANUAL COPY LEDGER</span><h2 id="live-copy-history-title">跟单记录</h2><p>本地订单台账与 Binance 收益流水合并统计；已实现盈亏、手续费、资金费与当前浮动盈亏。</p></div>
              <button type="button" data-live-copy-history-close aria-label="关闭跟单记录">×</button>
            </header>
            <div id="live-copy-history-body" class="live-copy-body"><div class="live-copy-loading">正在读取真实成交与收益流水…</div></div>
          </section>
        </div>
        <div id="manual-follow-modal" class="live-copy-modal hidden" aria-hidden="true">
          <button class="live-copy-backdrop" type="button" data-manual-follow-close aria-label="关闭立即跟买确认"></button>
          <section class="live-copy-dialog manual-follow-dialog" role="dialog" aria-modal="true" aria-labelledby="manual-follow-title" tabindex="-1">
            <header>
              <div><span>MANUAL LIVE COPY</span><h2 id="manual-follow-title">立即跟买</h2><p>只执行当前选中的一条机会；提交前后仍由统一实盘风控和 Binance 账户状态决定是否下单。</p></div>
              <button type="button" data-manual-follow-close aria-label="关闭立即跟买确认">×</button>
            </header>
            <div id="manual-follow-body" class="live-copy-body"><div class="live-copy-loading">正在读取当前信号与实盘配置…</div></div>
          </section>
        </div>
        <section id="macro-market-panel" class="macro-market-panel" aria-label="美股宏观大盘环境">
          <div class="macro-market-loading"><span>US MARKET REGIME</span><strong>正在读取美股大盘环境…</strong></div>
        </section>
        <div id="macro-impact-modal" class="live-copy-modal hidden" aria-hidden="true">
          <button class="live-copy-backdrop" type="button" data-macro-impact-close aria-label="关闭宏观因素判断"></button>
          <section class="live-copy-dialog macro-impact-dialog" role="dialog" aria-modal="true" aria-labelledby="macro-impact-title" tabindex="-1">
            <header>
              <div><span>MACRO ADMISSION CONTROL</span><h2 id="macro-impact-title">宏观因素判断</h2><p>直接收益率、全球央行、资金撤退确认与行业敏感度共同控制准入门槛和仓位，不替代 Binance 交易与结算价格。</p></div>
              <button type="button" data-macro-impact-close aria-label="关闭宏观因素判断">×</button>
            </header>
            <div id="macro-impact-body" class="macro-impact-body"><div class="live-copy-loading">正在整理当前宏观判断…</div></div>
          </section>
        </div>
        <section id="signal-health-strip" class="signal-health-strip" aria-label="实时数据健康与风险状态" aria-live="polite">
          <div class="signal-health-loading"><span>DATA PIPELINE</span><strong>正在检查行情与信号数据覆盖…</strong></div>
        </section>
        <div class="ai-layout">
          <nav class="ai-module-nav" aria-label="发现机会菜单">
            <button class="ai-nav-root active" type="button" data-ai-view="opportunities"><span>◆</span><strong>发现机会</strong><small>机会总览</small></button>
            <div class="ai-subnav-group" aria-label="发现机会二级菜单">
              <button class="ai-nav-child" type="button" data-ai-view="news"><span>01</span><strong>新闻列表</strong><small>实时滚动</small></button>
              <button class="ai-nav-child" type="button" data-ai-view="runs"><span>02</span><strong>分析记录</strong><small>任务与结论</small></button>
              <button class="ai-nav-child" type="button" data-ai-view="predictions"><span>03</span><strong>预测统计分析</strong><small>历史命中</small></button>
            </div>
          </nav>
          <main class="ai-content">
            <section id="view-news" class="ai-view">
              <div class="view-head">
                <div><span class="eyebrow">LIVE NEWS STREAM</span><h2>当前新闻滚动</h2><p>每 15 分钟从数据库读取最新 10 条未分析新闻，识别情绪、行业和关联美股。</p></div>
                <div class="view-tools"><input id="news-search" aria-label="搜索新闻" placeholder="搜索标题 / 来源 / 行业 / 股票"><select id="news-mode" aria-label="新闻筛选"><option value="all">全部新闻</option><option value="analyzed">已 AI 分析</option><option value="pending">待分析</option><option value="bull">偏多新闻</option><option value="bear">偏空新闻</option><option value="neutral">中性新闻</option></select></div>
              </div>
              <div id="news-stream" class="news-stream"><div class="empty-state">正在连接新闻流…</div></div>
            </section>
            <section id="view-runs" class="ai-view">
              <div class="view-head"><div><span class="eyebrow">ANALYSIS LEDGER</span><h2>分析记录</h2><p>保留每轮新闻分析和机会扫描的输入、结果与失败原因。</p></div></div>
              <div id="run-list" class="run-list"><div class="empty-state">暂无分析记录</div></div>
            </section>
            <section id="view-config" class="ai-view">
              <div class="view-head"><div><span class="eyebrow">SIGNAL CONFIGURATION</span><h2>指标配置</h2><p>设置新闻分析周期、机会扫描周期和技术确认条件。</p></div></div>
              <form id="ai-config-form" class="config-form">
                <section class="config-block automation-block">
                  <div><strong>后台周期监控</strong><small>开启后，服务端会按配置周期持续运行；关闭仍可手动执行。</small></div>
                  <label class="toggle"><input id="config-enabled" type="checkbox"><span></span><b id="config-enabled-label">已暂停</b></label>
                </section>
                <div id="model-warning" class="model-warning hidden">管理员尚未配置全局 DeepSeek。请联系管理员在管理后台完成配置。</div>
                <section class="config-grid">
                  <label><span>新闻分析间隔</span><div><input id="config-news-interval" type="number" min="5" max="1440" step="1" required><em>分钟</em></div><small>每轮读取最新 10 条尚未完成 AI 研判的新闻</small></label>
                  <label><span>机会发现间隔</span><div><input id="config-opportunity-interval" type="number" min="5" max="1440" step="1" required><em>分钟</em></div><small>组合新闻和技术指标重新扫描</small></label>
                  <label><span>AI 新闻记忆范围</span><div><input id="config-lookback" type="number" min="1" max="168" step="1" required><em>小时</em></div><small>历史研判只作为上下文记忆，不会单独触发新机会</small></label>
                  <label><span>技术指标周期</span><select id="config-timeframe"><option value="15m">15 分钟</option><option value="1h">1 小时</option><option value="4h">4 小时</option></select><small>采用对应周期最新一根已收盘 K 线</small></label>
                  <label><span>预测最大持有周期</span><div><input id="config-max-holding-bars" type="number" min="1" max="24" step="1" required><em>根 K 线</em></div><small>与技术周期独立；默认持有 4 根，退出条件仍可提前触发</small></label>
                  <label><span>最低新闻置信度</span><div><input id="config-confidence" type="number" min="0" max="100" step="1" required><em>%</em></div><small>AI 置信度 × 股票相关度</small></label>
                  <label><span>最少关联新闻</span><div><input id="config-mentions" type="number" min="1" max="20" step="1" required><em>条</em></div><small>同一股票达到数量后才进入指标确认</small></label>
                  <label><span>最低技术强度</span><div><input id="config-indicator-score" type="number" min="0" max="100" step="1" required><em>分</em></div><small>按最佳有效策略组计算，达到门槛后再参与组合评分</small></label>
                  <label><span>最低组合评分</span><div><input id="config-combined-score" type="number" min="75" max="100" step="1" required><em>分</em></div><small>安全下限 75 分；仅作为影子准入门槛</small></label>
                  <label><span>最大实时行情延迟</span><div><input id="config-market-age" type="number" min="5" max="3600" step="1" required><em>秒</em></div><small>超过时只保留研究信号</small></label>
                  <label><span>最低资金流数据质量</span><div><input id="config-market-flow-quality" type="number" min="0" max="100" step="1" required><em>%</em></div><small>资金盘口权重启用时，低于门槛仅保留研究信号</small></label>
                  <label><span>预测因子最低质量</span><div><input id="config-feature-quality" type="number" min="0" max="100" step="1" required><em>%</em></div><small>仅在选择预测因子时生效</small></label>
                  <label><span>历史校准样本门槛</span><div><input id="config-calibration-samples" type="number" min="30" max="5000" step="10" required><em>条</em></div><small>默认按实盘研究标准要求 1,000 条</small></label>
                  <label><span>成本安全边际</span><div><input id="config-safety-margin" type="number" min="0" max="500" step="0.5" required><em>bps</em></div><small>毛优势置信下限还需额外覆盖该数值</small></label>
                </section>
                <section class="signal-safety-policy" aria-label="当前固定安全策略">
                  <header><div><span>ENTRY QUALITY POLICY</span><strong>新事件与行情质量双重准入</strong><small>旧新闻继续作为 AI 一周记忆，但不能自行重复触发预测。</small></div><b>固定启用</b></header>
                  <div><span><i>01</i><b>4 小时新事件</b><small>至少一条触发窗口内新闻</small></span><span><i>02</i><b>未消费新闻 ID</b><small>同一事件只准入一次</small></span><span><i>03</i><b>行情质量通过</b><small>实时价与已收盘 K 线新鲜</small></span><span><i>04</i><b>组合分 ≥ 75</b><small>低分仅保留研究候选</small></span></div>
                </section>
                <section id="weight-config" class="weight-config">
                  <header><div><span class="weight-kicker">SIX-DOMAIN SCORE</span><strong>组合评分权重</strong><small>六类证据共同决定机会组合分；缺失域只会降级或按后端规则重归一，不会用虚构数据补分。</small></div><aside><b id="weight-policy-mode">参与组合评分</b><small id="weight-permission-state">仅管理员可改</small><span id="weight-total-state">合计 100% · 可保存</span></aside></header>
                  <div class="weight-grid">
                    <label class="news"><span><i>01</i><b>新闻与历史研判</b><small>当前新闻、7 天记忆与历史判断变化</small></span><div><input id="config-news-weight" class="score-weight-input" data-weight-domain="news" type="number" min="0" max="100" step="0.1" required><em>%</em></div></label>
                    <label class="technical"><span><i>02</i><b>技术指标</b><small>趋势、突破、反转及多周期技术强度</small></span><div><input id="config-technical-weight" class="score-weight-input" data-weight-domain="technical" type="number" min="0" max="100" step="0.1" required><em>%</em></div></label>
                    <label class="options"><span><i>03</i><b>个股期权资金流</b><small>主动买卖、开仓/扫单与多周期持续性</small></span><div><input id="config-options-flow-weight" class="score-weight-input" data-weight-domain="options_flow" type="number" min="0" max="100" step="0.1" required><em>%</em></div></label>
                    <label class="macro"><span><i>04</i><b>宏观与板块环境</b><small>指数、VIX、Market Tide 与板块共振</small></span><div><input id="config-market-context-weight" class="score-weight-input" data-weight-domain="market_context" type="number" min="0" max="100" step="0.1" required><em>%</em></div></label>
                    <label class="gex"><span><i>05</i><b>GEX 与波动率结构</b><small>Gamma 墙、Flip、磁吸位与期限结构</small></span><div><input id="config-gex-weight" class="score-weight-input" data-weight-domain="gex" type="number" min="0" max="100" step="0.1" required><em>%</em></div></label>
                    <label class="institutional"><span><i>06</i><b>Lit / Off-lit 机构确认</b><small>场内外成交、暗池关键价与机构方向确认</small></span><div><input id="config-institutional-flow-weight" class="score-weight-input" data-weight-domain="institutional_flow" type="number" min="0" max="100" step="0.1" required><em>%</em></div></label>
                    <input id="config-market-flow-weight" type="hidden" value="50" aria-hidden="true">
                  </div>
                  <div class="weight-preview" aria-label="六域权重占比预览"><i class="news"></i><i class="technical"></i><i class="options-flow"></i><i class="market-context"></i><i class="gex"></i><i class="institutional-flow"></i></div>
                  <footer><span><i class="news"></i>新闻研判</span><span><i class="technical"></i>技术</span><span><i class="options-flow"></i>期权流</span><span><i class="market-context"></i>宏观板块</span><span><i class="gex"></i>GEX</span><span><i class="institutional-flow"></i>机构确认</span><small>保存后从下一轮新信号生效；历史机会保留生成时的冻结权重与版本，硬门控仍独立执行。</small><button id="save-score-policy" type="button">保存六域权重</button></footer>
                </section>
                <section class="symbol-block">
                  <header><div><strong>监控品种</strong><small>机会扫描只处理这里配置的美股合约；候选还需通过策略组、评分及资金冲突校验。</small></div><span id="symbol-count">正在读取</span></header>
                  <div class="symbol-mode">
                    <label><input id="config-all-symbols" type="checkbox"><span>扫描全部可用品种</span></label>
                    <div class="symbol-tools"><input id="symbol-search" type="search" aria-label="搜索监控品种" placeholder="搜索 AAPL / AAPLUSDT"><button id="symbols-visible" type="button">选择筛选结果</button><button id="symbols-clear" type="button">清空选择</button></div>
                  </div>
                  <div id="symbol-picker" class="symbol-picker"><div class="empty-state">正在读取可监控品种…</div></div>
                </section>
                <section id="indicator-config" class="indicator-block">
                  <header><div><strong>技术指标（多选）</strong><small>趋势、突破、回踩、反转按策略组择一确认；至少 2 项核心指标同向，盘口指标参与评分与冲突校验。</small></div><span id="indicator-count">已选 0 项</span></header>
                  <div id="indicator-templates" class="indicator-templates"></div>
                  <div id="indicator-conflict-warning" class="indicator-conflict-warning hidden"></div>
                  <div id="indicator-picker" class="indicator-picker"><div class="empty-state">正在读取指标目录…</div></div>
                </section>
                <div class="config-footer"><span id="config-saved-at">尚未保存用户配置</span><button class="save-config" type="submit">保存配置</button></div>
              </form>
            </section>
            <section id="view-opportunities" class="ai-view active">
              <div class="view-head opportunity-view-head"><div><span class="eyebrow">DECISION WORKSPACE</span><h2>机会决策</h2><p>优先查看触发状态、方向与价格；展开卡片可查看完整证据。</p></div></div>
              <nav class="opportunity-tabs" role="tablist" aria-label="机会记录范围">
                <button class="active" type="button" role="tab" aria-selected="true" data-opportunity-tab="current"><span>当前机会</span><small id="current-direction-counts" class="direction-counts"><b class="long">多 --</b><i>/</i><b class="short">空 --</b></small></button>
                <button type="button" role="tab" aria-selected="false" data-opportunity-tab="history"><span>历史机会</span><small id="history-direction-counts" class="direction-counts"><b class="long">多 --</b><i>/</i><b class="short">空 --</b></small></button>
              </nav>
              <nav id="opportunity-status-tabs" class="opportunity-status-tabs" role="tablist" aria-label="当前机会触发状态">
                <button class="active" type="button" role="tab" aria-selected="true" data-opportunity-status="all"><span>全部</span><b id="opportunity-status-all-count">0</b></button>
                <button type="button" role="tab" aria-selected="false" data-opportunity-status="triggered"><span>已触发</span><b id="opportunity-status-triggered-count">0</b></button>
                <button type="button" role="tab" aria-selected="false" data-opportunity-status="ready"><span>可触发</span><b id="opportunity-status-ready-count">0</b></button>
                <button type="button" role="tab" aria-selected="false" data-opportunity-status="candidate"><span>待评估</span><b id="opportunity-status-candidate-count">0</b></button>
                <button type="button" role="tab" aria-selected="false" data-opportunity-status="blocked"><span>数据阻断</span><b id="opportunity-status-blocked-count">0</b></button>
                <button type="button" role="tab" aria-selected="false" data-opportunity-status="data_error"><span>异常</span><b id="opportunity-status-data_error-count">0</b></button>
                <button class="status-compat hidden" type="button" tabindex="-1" aria-hidden="true" data-opportunity-status="waiting"><span>兼容候选</span><b id="opportunity-status-waiting-count">0</b></button>
                <button class="status-compat hidden" type="button" tabindex="-1" aria-hidden="true" data-opportunity-status="failed"><span>兼容异常</span><b id="opportunity-status-failed-count">0</b></button>
              </nav>
              <div id="opportunity-list" class="opportunity-list"><div class="empty-state">暂无符合新闻条件的美股候选</div></div>
            </section>
            <section id="view-predictions" class="ai-view">
              <div class="view-head"><div><span class="eyebrow">OPPORTUNITY ANALYTICS</span><h2>预测统计分析</h2><p id="prediction-note">按止盈、止损、综合评分退出和最大持有期退出统计预测表现。</p></div></div>
              <section id="strategy-readiness" class="strategy-readiness"><div class="analytics-loading">正在评估实盘准备门槛…</div></section>
              <section id="adaptive-exit-policy" class="adaptive-exit-policy"><div class="analytics-loading">正在读取退出保护策略…</div></section>
              <section id="market-ablation" class="market-ablation" aria-label="市场数据模块消融对比"><div class="analytics-loading">正在计算冻结快照消融对比…</div></section>
              <div class="analytics-control-grid">
                <form id="prediction-filter-form" class="analytics-filters">
                  <header><div><strong>筛选统计</strong><small>汇总指标和下方明细使用相同条件；只填一个日期时按该自然日筛选</small></div><span id="prediction-filter-result">全部历史样本</span></header>
                  <div class="analytics-filter-grid">
                    <label><span>信号日期从</span><input id="prediction-date-from" type="date"></label>
                    <label><span>信号日期至</span><input id="prediction-date-to" type="date"></label>
                    <label><span>股票代码</span><input id="prediction-symbol" type="search" maxlength="24" autocomplete="off" placeholder="AAPL"></label>
                    <label><span>新闻评分不低于</span><div><input id="prediction-news-score-min" type="number" min="0" max="100" step="1" value="0"><em>分</em></div></label>
                    <label><span>指标评分不低于</span><div><input id="prediction-indicator-score-min" type="number" min="0" max="100" step="1" value="0"><em>分</em></div></label>
                    <label><span>组合评分不低于</span><div><input id="prediction-combined-score-min" type="number" min="0" max="100" step="1" value="0"><em>分</em></div></label>
                    <label><span>期权流评分不低于</span><div><input id="prediction-option-flow-score-min" type="number" min="0" max="100" step="1" value="0"><em>分</em></div></label>
                    <label><span>GEX 评分不低于</span><div><input id="prediction-gex-score-min" type="number" min="0" max="100" step="1" value="0"><em>分</em></div></label>
                    <label><span>数据覆盖率不低于</span><div><input id="prediction-data-coverage-min" type="number" min="0" max="100" step="1" value="0"><em>%</em></div></label>
                    <label><span>特征版本</span><input id="prediction-feature-version" type="search" maxlength="32" autocomplete="off" placeholder="全部版本"></label>
                    <label><span>决策版本</span><input id="prediction-decision-version" type="search" maxlength="32" autocomplete="off" placeholder="全部版本"></label>
                    <label><span>结算策略版本</span><select id="prediction-settlement-version"><option value="current">当前版本</option><option value="all">全部版本</option></select></label>
                    <label><span>方向筛选</span><select id="prediction-direction"><option value="all">全部方向</option><option value="long">只看做多</option><option value="short">只看做空</option></select></label>
                    <label><span>交易时段</span><select id="prediction-market-session"><option value="all">全部时段</option><option value="premarket">盘前</option><option value="regular">盘中</option><option value="postmarket">盘后</option><option value="closed">休市</option></select></label>
                    <label><span>行情质量</span><select id="prediction-quote-quality"><option value="all">全部质量</option><option value="passed">NBBO 已通过</option><option value="partial">仅现货价快照</option><option value="blocked">未通过</option><option value="missing">数据缺失</option></select></label>
                    <label><span>事件风险</span><select id="prediction-event-risk"><option value="all">全部事件状态</option><option value="clear">无临近事件</option><option value="warning">事件预警</option><option value="blocked">事件阻断</option></select></label>
                    <label><span>退出原因</span><select id="prediction-exit-reason"><option value="all">全部退出原因</option><option value="take_profit">止盈</option><option value="stop_loss">止损</option><option value="profit_lock">浮盈保护</option><option value="trailing_profit">移动保护</option><option value="score_reversal">评分反转</option><option value="max_holding">最大持有期</option></select></label>
                    <div class="analytics-filter-actions"><button id="prediction-filter-reset" type="button">重置</button><button id="prediction-filter-apply" class="primary" type="submit">应用筛选</button></div>
                  </div>
                </form>
                <form id="prediction-cost-form" class="analytics-cost-config">
                  <header><div><strong>成本计算</strong><small>可独立启用，净收益与准备度即时重算</small></div><span id="prediction-cost-total">1h 往返 -- bps</span></header>
                  <div class="analytics-cost-grid">
                    <label><input id="prediction-fee-enabled" type="checkbox"><span><b>手续费</b><small>单边</small></span><div><input id="prediction-fee-bps" type="number" min="0" max="500" step="0.1" value="5"><em>bps</em></div></label>
                    <label><input id="prediction-slippage-enabled" type="checkbox"><span><b>滑点</b><small>单边</small></span><div><input id="prediction-slippage-bps" type="number" min="0" max="500" step="0.1" value="3"><em>bps</em></div></label>
                    <label><input id="prediction-funding-enabled" type="checkbox"><span><b>资金成本</b><small>每 8h</small></span><div><input id="prediction-funding-bps" type="number" min="0" max="500" step="0.1" value="1"><em>bps</em></div></label>
                    <button id="prediction-cost-apply" class="primary" type="submit">保存并重算</button>
                  </div>
                </form>
              </div>
              <section id="analytics-summary" class="analytics-summary" aria-label="历史机会命中统计"><div class="analytics-loading">正在计算历史机会表现…</div></section>
              <div id="prediction-list" class="prediction-list"><div class="empty-state">正在读取历史机会…</div></div>
            </section>
          </main>
        </div>
      </div>
      <contract-monitor id="opportunity-research" research-only aria-label="股票合约 K 线研究弹窗"></contract-monitor>
      <div id="score-trend-modal" class="score-trend-modal hidden" aria-hidden="true">
        <button class="score-trend-backdrop" type="button" data-score-trend-close aria-label="关闭评分走势"></button>
        <section class="score-trend-dialog" role="dialog" aria-modal="true" aria-labelledby="score-trend-title">
          <header class="score-trend-head">
            <div><span class="eyebrow">LIVE SCORE HISTORY</span><h2 id="score-trend-title">组合评分走势</h2><p id="score-trend-subtitle">展示每次机会扫描保存的评分变化。</p></div>
            <button id="score-trend-close" class="ai-conclusion-close" type="button" data-score-trend-close aria-label="关闭评分走势">×</button>
          </header>
          <div id="score-trend-body" class="score-trend-body"></div>
          <footer class="score-trend-foot"><span>当前机会评分随扫描更新</span><strong>预测入场评分保持冻结</strong></footer>
        </section>
      </div>
      <div id="order-book-modal" class="order-book-modal hidden" aria-hidden="true">
        <button class="order-book-backdrop" type="button" data-order-book-close aria-label="关闭实时盘口"></button>
        <section class="order-book-dialog" role="dialog" aria-modal="true" aria-labelledby="order-book-title">
          <header class="order-book-head">
            <div><span class="eyebrow">BINANCE FUTURES LIVE DEPTH</span><h2 id="order-book-title">实时100档盘口</h2><p id="order-book-subtitle">读取 Binance Futures 本地同步订单簿。</p></div>
            <div class="order-book-actions">
              <span id="order-book-live-state" class="order-book-live-state syncing">同步中</span>
              <div class="order-book-limit" role="group" aria-label="盘口档位"><button type="button" data-order-book-limit="20">20档</button><button type="button" data-order-book-limit="50">50档</button><button class="active" type="button" data-order-book-limit="100">100档</button></div>
              <button id="order-book-pause" type="button">暂停刷新</button>
              <button id="order-book-close" class="ai-conclusion-close" type="button" data-order-book-close aria-label="关闭实时盘口">×</button>
            </div>
          </header>
          <div id="order-book-body" class="order-book-body"><div class="order-book-loading">正在同步 Binance 实时盘口…</div></div>
          <footer class="order-book-foot"><span>页面每秒读取一次本地 WebSocket 订单簿，底层深度流约 500ms 更新</span><strong>金额为 Binance 合约可见限价单名义金额，不代表真实主力资金</strong></footer>
        </section>
      </div>
      <div id="ai-conclusion-modal" class="ai-conclusion-modal hidden" aria-hidden="true">
        <button class="ai-conclusion-backdrop" type="button" data-conclusion-close aria-label="关闭 AI 分析结论"></button>
        <section class="ai-conclusion-dialog" role="dialog" aria-modal="true" aria-labelledby="ai-conclusion-title">
          <nav class="ai-conclusion-nav" role="tablist" aria-label="机会分析详情">
            <button type="button" role="tab" aria-selected="false" data-conclusion-view="fundamentals"><span>01</span><strong>基本面信息</strong><small id="ai-conclusion-fundamental-state">正在读取</small></button>
            <button type="button" role="tab" aria-selected="false" data-conclusion-view="news"><span>02</span><strong>相关新闻列表</strong><small id="ai-conclusion-news-count">正在读取</small></button>
            <button type="button" role="tab" aria-selected="false" data-conclusion-view="memory"><span>03</span><strong>AI新闻分析记录</strong><small id="ai-conclusion-memory-count">一周记忆</small></button>
            <button type="button" role="tab" aria-selected="false" data-conclusion-view="market"><span>04</span><strong>市场与资金</strong><small id="ai-conclusion-market-state">信号快照</small></button>
            <button class="active" type="button" role="tab" aria-selected="true" data-conclusion-view="analysis"><span>05</span><strong>AI分析结论</strong><small>综合研判</small></button>
          </nav>
          <div class="ai-conclusion-main">
            <header class="ai-conclusion-head">
              <div><span class="eyebrow">AI ANALYSIS CONCLUSION</span><h2 id="ai-conclusion-title">AI 分析结论</h2><p id="ai-conclusion-subtitle">读取生成该机会时保存的新闻研判与技术指标证据。</p></div>
              <div class="ai-conclusion-head-actions">
                <button id="open-news-system-prompt" class="news-system-prompt-trigger" type="button">系统提示词配置</button>
                <button id="open-historical-judgment" class="historical-judgment-trigger" type="button">历史研判</button>
                <button id="open-news-logic" class="news-logic-trigger" type="button">新闻分析逻辑</button>
                <button id="ai-conclusion-close" class="ai-conclusion-close" type="button" data-conclusion-close aria-label="关闭">×</button>
              </div>
            </header>
            <div id="ai-conclusion-body" class="ai-conclusion-body"></div>
            <footer class="ai-conclusion-foot"><span>结论基于信号生成时的数据快照</span><strong>仅作预测研究，不会触发实盘交易</strong></footer>
          </div>
        </section>
        <div id="news-logic-modal" class="news-logic-modal hidden" aria-hidden="true">
          <button class="news-logic-backdrop" type="button" data-news-logic-close aria-label="关闭新闻分析逻辑"></button>
          <section class="news-logic-dialog" role="dialog" aria-modal="true" aria-labelledby="news-logic-title">
            <header class="news-logic-head">
              <div><span class="eyebrow">MODEL CALL AUDIT</span><h2 id="news-logic-title">新闻分析逻辑</h2><p id="news-logic-subtitle">查看实际发送给模型的提示词和模型原始返回。</p></div>
              <button id="news-logic-close" class="ai-conclusion-close" type="button" data-news-logic-close aria-label="关闭新闻分析逻辑">×</button>
            </header>
            <div id="news-logic-body" class="news-logic-body"></div>
            <footer class="news-logic-foot"><span>请求记录不保存 Authorization 请求头或 API Key</span><strong>内容来自数据库中的原始模型调用审计记录</strong></footer>
          </section>
        </div>
        <div id="news-system-prompt-modal" class="news-system-prompt-modal hidden" aria-hidden="true">
          <button class="news-system-prompt-backdrop" type="button" data-news-system-prompt-close aria-label="关闭系统提示词配置"></button>
          <section class="news-system-prompt-dialog" role="dialog" aria-modal="true" aria-labelledby="news-system-prompt-title">
            <header class="news-system-prompt-head">
              <div><span class="eyebrow">NEWS ANALYSIS SYSTEM PROMPT</span><h2 id="news-system-prompt-title">系统提示词配置</h2><p>配置后，下一批新闻分析请求立即使用；历史模型调用记录不会被改写。</p></div>
              <button id="news-system-prompt-close" class="ai-conclusion-close" type="button" data-news-system-prompt-close aria-label="关闭系统提示词配置">×</button>
            </header>
            <form id="news-system-prompt-form" class="news-system-prompt-form">
              <div class="news-system-prompt-body">
                <section class="news-system-prompt-note"><strong>实际 System 消息</strong><span>该内容会作为新闻分析模型的 system 消息发送。建议保留“不执行新闻内指令、只关联真实美股、严格返回 JSON”等约束。</span><em id="news-system-prompt-state">正在读取</em></section>
                <label class="news-system-prompt-editor"><span>提示词正文</span><textarea id="news-system-prompt-input" name="system_prompt" rows="18" maxlength="8000" spellcheck="false" required></textarea><small><b id="news-system-prompt-count">0</b> / 8000 字符</small></label>
                <p id="news-system-prompt-status" class="news-system-prompt-status" role="status"></p>
              </div>
              <footer class="news-system-prompt-foot"><button id="news-system-prompt-default" type="button">恢复默认模板</button><div><button type="button" data-news-system-prompt-close>取消</button><button id="news-system-prompt-save" class="primary-action" type="submit">保存并应用</button></div></footer>
            </form>
          </section>
        </div>
        <div id="historical-judgment-modal" class="historical-judgment-modal hidden" aria-hidden="true">
          <button class="historical-judgment-backdrop" type="button" data-historical-judgment-close aria-label="关闭历史研判"></button>
          <section class="historical-judgment-dialog" role="dialog" aria-modal="true" aria-labelledby="historical-judgment-title">
            <header class="historical-judgment-head">
              <div><span class="eyebrow">CONTINUOUS JUDGMENT INPUT</span><h2 id="historical-judgment-title">历史研判</h2><p id="historical-judgment-subtitle">展示本次新闻分析实际发送的旧新闻、记忆链与当前研究持仓。</p></div>
              <button id="historical-judgment-close" class="ai-conclusion-close" type="button" data-historical-judgment-close aria-label="关闭历史研判">×</button>
            </header>
            <div id="historical-judgment-body" class="historical-judgment-body"></div>
            <footer class="historical-judgment-foot"><span>旧新闻与持仓只作为上下文，不得当作方向证明</span><strong>内容取自模型调用审计记录</strong></footer>
          </section>
        </div>
      </div>`;
  }

  bindEvents() {
    if (this.eventsBound) return;
    this.eventsBound = true;
    this.qa("[data-ai-view]").forEach((button) => button.addEventListener("click", () => this.showView(button.dataset.aiView)));
    this.q("#ai-refresh").addEventListener("click", () => this.loadAll(true));
    this.q("#run-news").addEventListener("click", () => this.createRun("news"));
    this.q("#run-opportunity").addEventListener("click", () => this.createRun("opportunity"));
    this.q("#live-copy-toggle").addEventListener("click", (event) => {
      event.preventDefault();
      event.stopPropagation();
      this.openLiveCopyModal();
    });
    this.q("#live-copy-config-button").addEventListener("click", () => this.openLiveCopyConfigModal());
    this.q("#live-copy-history-button").addEventListener("click", () => this.openLiveCopyHistoryModal());
    this.qa("[data-live-copy-close]").forEach((button) => button.addEventListener("click", () => this.closeLiveCopyModal()));
    this.qa("[data-live-copy-config-close]").forEach((button) => button.addEventListener("click", () => this.closeLiveCopyConfigModal()));
    this.qa("[data-live-copy-history-close]").forEach((button) => button.addEventListener("click", () => this.closeLiveCopyHistoryModal()));
    this.qa("[data-manual-follow-close]").forEach((button) => button.addEventListener("click", () => this.closeManualFollowModal()));
    this.q("#live-copy-body").addEventListener("submit", (event) => this.submitLiveCopy(event));
    this.q("#live-copy-config-body").addEventListener("submit", (event) => this.submitLiveCopyConfig(event));
    this.q("#live-copy-history-body").addEventListener("click", (event) => {
      if (event.target.closest("[data-live-copy-history-refresh]")) void this.loadLiveCopyHistory();
    });
    this.q("#manual-follow-body").addEventListener("submit", (event) => this.submitManualFollow(event));
    this.q("#live-copy-config-body").addEventListener("change", (event) => {
      if (!event.target.closest('[name="position_size_basis"]')) return;
      this.syncLiveCopyPositionSizing(event.target.closest("form"));
    });
    this.q("#live-copy-body").addEventListener("click", (event) => {
      if (!event.target.closest("[data-live-copy-retry]")) return;
      this.state.liveCopyError = "";
      this.renderLiveCopyModal();
      void this.loadLiveCopyStatus();
    });
    this.q("#uw-usage-toggle").addEventListener("click", () => this.toggleUnusualWhales());
    this.q("#finnhub-usage-toggle").addEventListener("click", () => this.toggleFinnhub());
    this.qa("[data-macro-impact-close]").forEach((button) => button.addEventListener("click", () => this.closeMacroImpact()));
    this.q("#macro-impact-body").addEventListener("click", (event) => {
      const button = event.target.closest("[data-refresh-macro-ai]");
      if (button) void this.loadMacroAiAnalysis({ force: true });
    });
    this.q("#macro-market-panel").addEventListener("click", (event) => {
      if (event.target.closest("[data-open-macro-impact]")) this.openMacroImpact();
    });
    this.q("#open-news-config").addEventListener("click", () => this.openConfig("news"));
    this.q("#open-weight-config").addEventListener("click", () => this.openConfig("weights"));
    this.q("#open-config").addEventListener("click", () => this.openConfig("indicators"));
    this.q("#ai-config-form").addEventListener("submit", (event) => this.saveConfig(event));
    this.q("#config-enabled").addEventListener("change", (event) => this.renderEnabledLabel(event.target.checked));
    this.qa(".score-weight-input").forEach((input) => input.addEventListener("input", () => {
      this.state.weightDraftDirty = true;
      this.updateScoreWeightPreview();
    }));
    this.q("#save-score-policy").addEventListener("click", () => this.saveScorePolicy());
    this.q("#indicator-picker").addEventListener("change", () => this.updateIndicatorCount());
    this.q("#indicator-templates").addEventListener("click", (event) => this.applyIndicatorTemplate(event));
    this.q("#config-all-symbols").addEventListener("change", () => this.renderSymbolPicker());
    this.q("#symbol-search").addEventListener("input", (event) => { this.state.symbolSearch = event.target.value.trim().toUpperCase(); this.renderSymbolPicker(); });
    this.q("#symbol-picker").addEventListener("change", (event) => this.updateDraftSymbol(event));
    this.q("#symbols-visible").addEventListener("click", () => this.selectVisibleSymbols());
    this.q("#symbols-clear").addEventListener("click", () => { this.q("#config-all-symbols").checked = false; this.state.draftSymbols.clear(); this.renderSymbolPicker(); });
    this.q("#news-search").addEventListener("input", (event) => { this.state.newsSearch = event.target.value.trim().toLowerCase(); this.renderNews(); });
    this.q("#news-mode").addEventListener("change", (event) => { this.state.newsMode = event.target.value; this.renderNews(); });
    this.q("#prediction-filter-form").addEventListener("submit", (event) => { event.preventDefault(); this.applyPredictionFilters(); });
    this.q("#prediction-filter-reset").addEventListener("click", () => this.resetPredictionFilters());
    this.q("#prediction-list").addEventListener("click", (event) => {
      const button = event.target.closest("[data-prediction-page]");
      if (!button || button.disabled) return;
      this.setPredictionPage(Number(button.dataset.predictionPage));
    });
    this.q("#prediction-cost-form").addEventListener("submit", (event) => this.savePredictionCostConfig(event));
    this.qa('#prediction-cost-form input[type="checkbox"]').forEach((input) => input.addEventListener("change", () => this.updatePredictionCostControls()));
    this.qa('#prediction-cost-form input[type="number"]').forEach((input) => input.addEventListener("input", () => this.updatePredictionCostControls()));
    this.qa("[data-opportunity-tab]").forEach((button) => button.addEventListener("click", () => this.setOpportunityTab(button.dataset.opportunityTab)));
    this.qa("[data-opportunity-status]").forEach((button) => button.addEventListener("click", () => this.setOpportunityStatusFilter(button.dataset.opportunityStatus)));
    const opportunityList = this.q("#opportunity-list");
    opportunityList.addEventListener("click", (event) => {
      const pageButton = event.target.closest("[data-opportunity-page]");
      if (pageButton && !pageButton.disabled) {
        this.setOpportunityPage(Number(pageButton.dataset.opportunityPage));
        return;
      }
      const detailButton = event.composedPath().find((node) => node?.matches?.("[data-toggle-opportunity-details]"));
      if (!detailButton) return;
      event.preventDefault();
      event.stopPropagation();
      this.toggleOpportunityDetails(detailButton);
    }, true);
    opportunityList.addEventListener("click", (event) => {
      const manualFollowButton = event.target.closest("[data-manual-follow]");
      if (manualFollowButton && !manualFollowButton.disabled) {
        this.openManualFollowModal(manualFollowButton.dataset.manualFollow);
        return;
      }
      const orderBookButton = event.target.closest("[data-order-book]");
      if (orderBookButton) {
        this.openOrderBook(orderBookButton.dataset.orderBook, orderBookButton);
        return;
      }
      const marketFlowButton = event.target.closest("[data-market-flow-trend]");
      if (marketFlowButton) {
        this.openMarketFlowTrend(marketFlowButton.dataset.marketFlowTrend, marketFlowButton);
        return;
      }
      const scoreButton = event.target.closest("[data-score-trend]");
      if (scoreButton) {
        this.openScoreTrend(scoreButton.dataset.scoreTrend, scoreButton);
        return;
      }
      const conclusionButton = event.target.closest("[data-ai-conclusion]");
      if (conclusionButton) {
        this.openAiConclusion(conclusionButton.dataset.aiConclusion, conclusionButton);
        return;
      }
      const button = event.target.closest("[data-open-contract]");
      if (!button || button.disabled) return;
      const research = this.q("#opportunity-research");
      if (typeof research?.openResearch !== "function") {
        this.showBanner("合约 K 线组件尚未加载，请刷新页面后重试。", "error");
        return;
      }
      const opportunity = this.state.opportunities.find((item) => item.id === button.dataset.opportunityId);
      const evidence = opportunity?.evidence || {};
      research.openResearch(button.dataset.openContract, button.dataset.timeframe || "1h", opportunity ? {
        id: opportunity.id,
        direction: opportunity.direction,
        combined_score: opportunity.combined_score,
        news_score: opportunity.news_score,
        indicator_score: opportunity.indicator_score,
        signal_time: opportunity.discovered_at,
        expires_at: opportunity.expires_at,
        entry_price: evidence.market?.price,
        technical_confirmed: evidence.confirmed === true,
        historical: this.state.opportunityTab === "history",
        outcome_result: opportunity.outcome?.result,
        exit_price: opportunity.outcome?.exit_price,
        directional_return_bps: opportunity.outcome?.directional_return_bps,
        settled_price_at: opportunity.outcome?.settled_price_at,
      } : null);
    });
    this.qa("[data-order-book-close]").forEach((button) => button.addEventListener("click", () => this.closeOrderBook()));
    this.qa("[data-order-book-limit]").forEach((button) => button.addEventListener("click", () => this.setOrderBookLimit(Number(button.dataset.orderBookLimit))));
    this.q("#order-book-pause").addEventListener("click", () => this.toggleOrderBookPause());
    this.q("#order-book-body").addEventListener("click", (event) => {
      const row = event.target.closest("[data-order-book-side][data-order-book-rank]");
      if (row) this.selectOrderBookLevel(row.dataset.orderBookSide, Number(row.dataset.orderBookRank));
    });
    this.qa("[data-score-trend-close]").forEach((button) => button.addEventListener("click", () => this.closeScoreTrend()));
    this.qa("[data-conclusion-close]").forEach((button) => button.addEventListener("click", () => this.closeAiConclusion()));
    this.qa("[data-conclusion-view]").forEach((button) => button.addEventListener("click", () => this.showAiConclusionView(button.dataset.conclusionView)));
    this.q("#open-news-logic").addEventListener("click", (event) => this.openNewsAnalysisLogic(event.currentTarget));
    this.q("#open-news-system-prompt").addEventListener("click", (event) => this.openNewsSystemPrompt(event.currentTarget));
    this.q("#open-historical-judgment").addEventListener("click", (event) => this.openHistoricalJudgment(event.currentTarget));
    this.qa("[data-news-logic-close]").forEach((button) => button.addEventListener("click", () => this.closeNewsAnalysisLogic()));
    this.qa("[data-news-system-prompt-close]").forEach((button) => button.addEventListener("click", () => this.closeNewsSystemPrompt()));
    this.qa("[data-historical-judgment-close]").forEach((button) => button.addEventListener("click", () => this.closeHistoricalJudgment()));
    this.q("#news-system-prompt-form").addEventListener("submit", (event) => this.saveNewsSystemPrompt(event));
    this.q("#news-system-prompt-input").addEventListener("input", () => this.updateNewsSystemPromptCount());
    this.q("#news-system-prompt-default").addEventListener("click", () => this.restoreDefaultNewsSystemPrompt());
    this.q("#news-logic-body").addEventListener("click", (event) => {
      const callButton = event.target.closest("[data-news-call-index]");
      if (callButton) {
        this.newsModelCallIndex = Number(callButton.dataset.newsCallIndex || 0);
        this.renderNewsAnalysisLogic();
        return;
      }
      const copyButton = event.target.closest("[data-copy-call-section]");
      if (copyButton) this.copyNewsModelCallSection(copyButton.dataset.copyCallSection, copyButton);
    });
    this.shadowRoot.addEventListener("keydown", (event) => {
      if (event.key !== "Escape") return;
      if (!this.q("#manual-follow-modal").classList.contains("hidden")) {
        this.closeManualFollowModal();
      } else if (!this.q("#macro-impact-modal").classList.contains("hidden")) {
        this.closeMacroImpact();
      } else if (!this.q("#order-book-modal").classList.contains("hidden")) {
        this.closeOrderBook();
      } else if (!this.q("#score-trend-modal").classList.contains("hidden")) {
        this.closeScoreTrend();
      } else if (!this.q("#historical-judgment-modal").classList.contains("hidden")) {
        this.closeHistoricalJudgment();
      } else if (!this.q("#news-system-prompt-modal").classList.contains("hidden")) {
        this.closeNewsSystemPrompt();
      } else if (!this.q("#news-logic-modal").classList.contains("hidden")) {
        this.closeNewsAnalysisLogic();
      } else if (!this.q("#ai-conclusion-modal").classList.contains("hidden")) {
        this.closeAiConclusion();
      }
    });
    this.q("#news-stream").addEventListener("mouseenter", () => { this.scrollPaused = true; });
    this.q("#news-stream").addEventListener("mouseleave", () => { this.scrollPaused = false; });
    this.q("#news-stream").addEventListener("click", (event) => {
      const button = event.target.closest("[data-analyze-news]");
      if (button) this.analyzeNewsItem(button.dataset.analyzeNews);
    });
  }

  toggleOpportunityDetails(button) {
    const card = button?.closest?.(".opportunity-item[data-opportunity-card]");
    if (!card) return;
    const opportunityId = String(button.dataset.toggleOpportunityDetails || card.dataset.opportunityCard || "");
    if (!opportunityId) return;
    // DOM 是用户此刻看到的真实状态；增量刷新期间 Set 可能短暂落后，不能用它反推下一步。
    const nextExpanded = !card.classList.contains("is-expanded");
    if (nextExpanded) this.state.expandedOpportunityIds.add(opportunityId);
    else this.state.expandedOpportunityIds.delete(opportunityId);
    card.classList.toggle("is-expanded", nextExpanded);
    card.dataset.detailsExpanded = String(nextExpanded);
    button.setAttribute("aria-expanded", String(nextExpanded));
    button.setAttribute("aria-label", `${nextExpanded ? "收起" : "展开"} ${card.querySelector(".opportunity-symbol")?.textContent?.trim() || "机会"}详情`);
    const icon = document.createElement("i");
    icon.setAttribute("aria-hidden", "true");
    icon.textContent = nextExpanded ? "⌃" : "⌄";
    button.replaceChildren(document.createTextNode(nextExpanded ? "收起详情 " : "展开详情 "), icon);
  }

  start() {
    if (this.state.running) return;
    this.state.running = true;
    this.tickClock();
    this.loadAll();
    this.startUpdateStream();
    this.timers.push(window.setInterval(() => this.tickClock(), 1000));
    this.timers.push(window.setInterval(() => this.loadMarketContext(), 30000));
    this.timers.push(window.setInterval(() => this.loadLiveState(), 60000));
    this.startNewsAutoScroll();
  }

  pause() {
    this.state.running = false;
    this.timers.forEach((timer) => window.clearInterval(timer));
    this.timers = [];
    window.clearTimeout(this.updateStreamRetryTimer);
    window.clearTimeout(this.updateStreamRefreshTimer);
    this.updateStreamRetryTimer = null;
    this.updateStreamRefreshTimer = null;
    this.updateStreamAbort?.abort();
    this.updateStreamAbort = null;
    this.predictionAnalyticsAbortController?.abort();
    this.predictionAnalyticsAbortController = null;
    this.opportunitiesAbortController?.abort();
    this.opportunitiesAbortController = null;
    window.clearInterval(this.orderBookTimer);
    this.orderBookTimer = null;
    if (this.newsScrollAnimationFrame != null) {
      window.cancelAnimationFrame(this.newsScrollAnimationFrame);
      this.newsScrollAnimationFrame = null;
    }
    this.newsScrollLastTick = 0;
    this.updateStreamScopes.clear();
    this.state.updateStreamStatus = "idle";
  }

  q(selector) { return this.shadowRoot.querySelector(selector); }
  qa(selector) { return [...this.shadowRoot.querySelectorAll(selector)]; }
  api(path = "", options = {}) { return window.quantdeskApi(`/api/v2/ai-monitor${path}`, options); }
  stream(path = "", options = {}) { return window.quantdeskApiStream(`/api/v2/ai-monitor${path}`, options); }

  startUpdateStream() {
    if (!this.state.running || this.updateStreamAbort || typeof window.quantdeskApiStream !== "function") return;
    const controller = new AbortController();
    this.updateStreamAbort = controller;
    this.state.updateStreamStatus = "connecting";
    this.consumeUpdateStream(controller).catch(() => {}).finally(() => {
      if (this.updateStreamAbort === controller) this.updateStreamAbort = null;
      if (!this.state.running || controller.signal.aborted) return;
      this.state.updateStreamStatus = "reconnecting";
      this.renderSignalHealth();
      this.updateStreamRetryTimer = window.setTimeout(() => this.startUpdateStream(), 3000);
    });
  }

  async consumeUpdateStream(controller) {
    const headers = new Headers();
    if (this.lastUpdateStreamEventId) headers.set("Last-Event-ID", this.lastUpdateStreamEventId);
    const response = await this.stream("/events", { signal: controller.signal, headers });
    if (!response.body) throw new Error("incremental stream is unavailable");
    this.state.updateStreamStatus = "connected";
    this.renderSignalHealth();
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    while (!controller.signal.aborted) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true }).replace(/\r\n/g, "\n");
      let boundary = buffer.indexOf("\n\n");
      while (boundary >= 0) {
        const frame = buffer.slice(0, boundary);
        buffer = buffer.slice(boundary + 2);
        this.handleUpdateStreamFrame(frame);
        boundary = buffer.indexOf("\n\n");
      }
    }
  }

  handleUpdateStreamFrame(frame) {
    const parsed = { event: "message", id: "", data: [] };
    String(frame || "").split("\n").forEach((line) => {
      if (!line || line.startsWith(":")) return;
      const separator = line.indexOf(":");
      const field = separator >= 0 ? line.slice(0, separator) : line;
      const value = separator >= 0 ? line.slice(separator + 1).replace(/^ /, "") : "";
      if (field === "event") parsed.event = value;
      if (field === "id") parsed.id = value;
      if (field === "data") parsed.data.push(value);
    });
    if (parsed.id) this.lastUpdateStreamEventId = parsed.id;
    if (!parsed.data.length || !["ready", "update"].includes(parsed.event)) return;
    try {
      const payload = JSON.parse(parsed.data.join("\n"));
      const scopes = Array.isArray(payload.scopes) ? payload.scopes : [];
      this.queueStreamRefresh(scopes);
    } catch (_) {
      // Ignore one malformed event and keep the authenticated stream alive.
    }
  }

  queueStreamRefresh(scopes) {
    scopes.forEach((scope) => this.updateStreamScopes.add(String(scope)));
    if (this.updateStreamRefreshTimer) return;
    this.updateStreamRefreshTimer = window.setTimeout(async () => {
      this.updateStreamRefreshTimer = null;
      const pending = new Set(this.updateStreamScopes);
      this.updateStreamScopes.clear();
      if (!this.state.running || document.visibilityState === "hidden") return;
      if (["opportunities", "runs", "news"].some((scope) => pending.has(scope))) {
        await this.loadLiveState();
      } else if (pending.has("market")) {
        await this.loadMarketContext();
      }
    }, 750);
  }

  escape(value) {
    return String(value ?? "").replace(/[&<>'"]/g, (character) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" })[character]);
  }

  tickClock() {
    const target = this.q("#ai-clock");
    if (target) target.textContent = new Intl.DateTimeFormat("zh-CN", { hour12: false, hour: "2-digit", minute: "2-digit", second: "2-digit" }).format(new Date());
    this.tickMarketCountdown();
  }

  tickMarketCountdown() {
    const target = this.q("[data-market-countdown]");
    if (!target) return;
    const value = target.querySelector("[data-market-countdown-value]");
    const state = target.querySelector("[data-market-countdown-state]");
    const targetAt = new Date(target.dataset.targetAt || "");
    if (!value || Number.isNaN(targetAt.getTime())) {
      if (value) value.textContent = "--:--:--";
      return;
    }
    const remainingSeconds = Math.max(0, Math.floor((targetAt.getTime() - Date.now()) / 1000));
    const days = Math.floor(remainingSeconds / 86400);
    const hours = Math.floor((remainingSeconds % 86400) / 3600);
    const minutes = Math.floor((remainingSeconds % 3600) / 60);
    const seconds = remainingSeconds % 60;
    const clock = [hours, minutes, seconds].map((part) => String(part).padStart(2, "0")).join(":");
    value.textContent = days > 0 ? `${days}天 ${clock}` : clock;
    target.classList.toggle("imminent", remainingSeconds > 0 && remainingSeconds <= 1800);
    target.classList.toggle("elapsed", remainingSeconds === 0);
    if (state) state.textContent = remainingSeconds === 0 ? "交易时段即将切换" : target.dataset.targetLabel || "交易时段倒计时";
  }

  async loadAll(showSuccess = false) {
    if (this.state.fullLoadLoading) return;
    this.state.fullLoadLoading = true;
    this.q("#ai-refresh").disabled = true;
    try {
      // Opportunity/history and analytics queries are independent from the
      // header/config payloads.  Start the active view immediately so a slow
      // news/config request cannot leave the primary workspace empty.
      const viewRequest = this.loadView(this.state.view);
      const [overview, news, indicators, symbols, macroMarket, scorePolicy, liveCopy] = await Promise.all([
        this.api("/overview"),
        this.api("/news?limit=160"),
        this.api("/indicators?timeframe=1h"),
        this.api("/symbols"),
        this.api("/market-context").catch(() => this.state.macroMarket || { available: false }),
        this.api("/score-policy").catch(() => this.state.scorePolicy),
        this.api("/live-copy").catch(() => this.state.liveCopy),
      ]);
      this.state.overview = overview;
      this.state.config = overview.config;
      this.state.news = news.items || [];
      this.state.indicators = indicators.items || [];
      this.state.indicatorTemplates = indicators.templates || [];
      this.state.indicatorConflictPairs = indicators.conflict_pairs || [];
      this.state.symbols = symbols.items || [];
      this.state.macroMarket = macroMarket;
      if (scorePolicy) this.state.scorePolicy = scorePolicy;
      if (liveCopy) this.state.liveCopy = liveCopy;
      this.renderOverview();
      this.renderMacroMarket();
      this.renderNews();
      this.renderConfig();
      await viewRequest;
      this.state.lastSuccessfulRefreshAt = new Date().toISOString();
      this.state.lastRefreshError = "";
      this.renderSignalHealth();
      if (showSuccess) this.showBanner("发现机会数据已刷新。", "success");
    } catch (error) {
      this.state.lastRefreshError = error.message || "发现机会数据读取失败";
      this.renderSignalHealth();
      this.showBanner(error.message || "发现机会数据读取失败", "error");
    } finally {
      this.state.fullLoadLoading = false;
      this.q("#ai-refresh").disabled = false;
    }
  }

  async loadLiveState() {
    if (!this.state.running || this.state.busyRun || this.state.fullLoadLoading || this.state.liveStateLoading || document.visibilityState === "hidden") return;
    this.state.liveStateLoading = true;
    try {
      const [overview, news, macroMarket, scorePolicy, liveCopy] = await Promise.all([
        this.api("/overview"),
        this.api("/news?limit=160"),
        this.api("/market-context").catch(() => this.state.macroMarket || { available: false }),
        this.api("/score-policy").catch(() => this.state.scorePolicy),
        this.api("/live-copy").catch(() => this.state.liveCopy),
      ]);
      this.state.overview = overview;
      this.state.config = overview.config;
      this.state.news = news.items || [];
      this.state.macroMarket = macroMarket;
      if (scorePolicy) this.state.scorePolicy = scorePolicy;
      if (liveCopy) this.state.liveCopy = liveCopy;
      this.renderOverview();
      this.renderMacroMarket();
      this.renderNews();
      if (this.state.view === "runs") await this.loadRuns();
      if (this.state.view === "opportunities") await this.loadOpportunities();
      if (this.state.view === "predictions") await this.loadPredictionAnalytics({ background: true });
      this.state.lastSuccessfulRefreshAt = new Date().toISOString();
      this.state.lastRefreshError = "";
      this.state.incrementalUpdateCount += 1;
      this.renderSignalHealth();
    } catch (error) {
      this.state.lastRefreshError = error.message || "自动刷新失败";
      this.renderSignalHealth();
      this.showBanner(error.message || "自动刷新失败", "error");
    } finally {
      this.state.liveStateLoading = false;
    }
  }

  async loadMarketContext() {
    if (!this.state.running || this.state.marketContextLoading || this.state.fullLoadLoading || document.visibilityState === "hidden") return;
    this.state.marketContextLoading = true;
    try {
      const data = await this.api("/market-context");
      if (data?.captured_at === this.state.macroMarket?.captured_at && data?.stale === this.state.macroMarket?.stale) return;
      this.state.macroMarket = data;
      this.renderMacroMarket();
      this.renderUnusualWhalesToggle();
      this.renderFinnhubToggle();
      this.renderSignalHealth();
    } catch (_) {
      // Keep the last valid market snapshot; the normal 20-second refresh owns banners.
    } finally {
      this.state.marketContextLoading = false;
    }
  }

  showView(view) {
    if (!["news", "runs", "config", "opportunities", "predictions"].includes(view)) return;
    this.state.view = view;
    this.qa("[data-ai-view]").forEach((button) => button.classList.toggle("active", button.dataset.aiView === view));
    this.qa(".ai-view").forEach((section) => section.classList.toggle("active", section.id === `view-${view}`));
    this.loadView(view);
  }

  openConfig(section) {
    this.showView("config");
    if (section === "weights") this.loadScorePolicy({ quiet: true });
    window.requestAnimationFrame(() => {
      const target = section === "news"
        ? this.q("#config-news-interval")
        : section === "weights"
        ? this.q("#weight-config")
        : this.q("#indicator-config");
      target?.scrollIntoView({ behavior: "smooth", block: "center" });
      if (section === "news") target?.focus();
      if (section === "weights") this.q("#config-news-weight")?.focus();
    });
  }

  async loadView(view) {
    if (view === "runs") return this.loadRuns();
    if (view === "opportunities") return this.loadOpportunities();
    if (view === "predictions") {
      void this.loadPredictionReadiness();
      return this.loadPredictionAnalytics({ force: true, background: true });
    }
    return undefined;
  }

  async loadScorePolicy({ quiet = false } = {}) {
    try {
      this.state.scorePolicy = await this.api("/score-policy");
      this.renderUnusualWhalesToggle();
      this.renderFinnhubToggle();
      if (!this.state.weightDraftDirty) this.renderScorePolicy(this.state.config || {});
      return this.state.scorePolicy;
    } catch (error) {
      if (!quiet) this.showBanner(error.message || "六域评分策略读取失败", "error");
      return null;
    }
  }

  renderOverview() {
    const data = this.state.overview || {};
    const config = data.config || {};
    const state = this.q("#scheduler-state");
    state.textContent = config.enabled ? "自动监控中" : "自动监控已暂停";
    state.className = `status-badge ${config.enabled ? "running" : "idle"}`;
    this.renderLiveCopyToggle();
    this.renderUnusualWhalesToggle();
    this.renderFinnhubToggle();
    this.q("#model-warning").classList.toggle("hidden", Boolean(data.model_configured));
    this.renderSignalHealth();
  }

  renderLiveCopyToggle() {
    const button = this.q("#live-copy-toggle");
    if (!button) return;
    const status = this.state.liveCopy;
    const loading = !status || this.state.liveCopyLoading;
    const enabled = status?.enabled === true;
    const suspended = status?.requested_enabled === true && !enabled;
    const ready = status?.ready_to_enable === true;
    button.className = `live-copy-toggle ${loading ? "loading" : enabled ? "enabled" : suspended ? "suspended" : ready ? "ready" : "disabled"}`;
    button.setAttribute("aria-pressed", enabled ? "true" : "false");
    button.setAttribute("aria-busy", loading ? "true" : "false");
    button.disabled = false;
    button.title = loading
      ? "正在读取 AI 机会独立跟单状态"
      : enabled
      ? "本页 AI 新信号正在独立执行；点击查看或停止新开仓"
      : suspended
      ? "独立跟单配置仍在，但 Binance 凭据或订单状态当前不可执行"
      : ready
      ? "点击查看独立风控并确认开启实盘跟单"
      : "当前不可开启；点击查看缺少的 Binance 执行条件";
    const label = button.querySelector("small");
    if (label) label.textContent = loading ? "读取中" : enabled ? "独立域 · 已开启" : suspended ? "独立域暂停" : ready ? "独立域 · 可开启" : "独立域 · 未就绪";
  }

  async loadLiveCopyStatus({ quiet = false } = {}) {
    try {
      this.state.liveCopy = await this.api("/live-copy");
      this.state.liveCopyError = "";
      this.renderLiveCopyToggle();
      this.renderLiveCopyModal();
      this.renderLiveCopyConfigModal();
      this.renderManualFollowModal();
      return this.state.liveCopy;
    } catch (error) {
      this.state.liveCopyError = error.message || "实盘跟单状态读取失败";
      this.renderLiveCopyToggle();
      this.renderLiveCopyModal();
      this.renderLiveCopyConfigModal();
      this.renderManualFollowModal();
      if (!quiet) this.showBanner(error.message || "实盘跟单状态读取失败", "error");
      return null;
    }
  }

  openLiveCopyModal() {
    const modal = this.q("#live-copy-modal");
    modal.classList.remove("hidden");
    modal.setAttribute("aria-hidden", "false");
    this.renderLiveCopyModal();
    const dialog = modal.querySelector(".live-copy-dialog");
    requestAnimationFrame(() => dialog?.focus({ preventScroll: true }));
    if (!this.state.liveCopyLoading) void this.loadLiveCopyStatus({ quiet: true });
  }

  closeLiveCopyModal() {
    const modal = this.q("#live-copy-modal");
    modal.classList.add("hidden");
    modal.setAttribute("aria-hidden", "true");
  }

  openLiveCopyConfigModal() {
    const modal = this.q("#live-copy-config-modal");
    modal.classList.remove("hidden");
    modal.setAttribute("aria-hidden", "false");
    this.renderLiveCopyConfigModal();
    const dialog = modal.querySelector(".live-copy-dialog");
    requestAnimationFrame(() => dialog?.focus({ preventScroll: true }));
    if (!this.state.liveCopyLoading) void this.loadLiveCopyStatus({ quiet: true });
  }

  closeLiveCopyConfigModal() {
    const modal = this.q("#live-copy-config-modal");
    modal.classList.add("hidden");
    modal.setAttribute("aria-hidden", "true");
  }

  openLiveCopyHistoryModal() {
    const modal = this.q("#live-copy-history-modal");
    modal.classList.remove("hidden");
    modal.setAttribute("aria-hidden", "false");
    this.renderLiveCopyHistoryModal();
    requestAnimationFrame(() => modal.querySelector(".live-copy-history-dialog")?.focus({ preventScroll: true }));
    void this.loadLiveCopyHistory();
  }

  closeLiveCopyHistoryModal() {
    const modal = this.q("#live-copy-history-modal");
    modal.classList.add("hidden");
    modal.setAttribute("aria-hidden", "true");
  }

  async loadLiveCopyHistory() {
    if (this.state.liveCopyHistoryLoading) return;
    this.state.liveCopyHistoryLoading = true;
    this.state.liveCopyHistoryError = "";
    this.renderLiveCopyHistoryModal();
    try {
      this.state.liveCopyHistory = await this.api("/live-copy/history?limit=50");
    } catch (error) {
      this.state.liveCopyHistoryError = error.message || "跟单记录读取失败";
    } finally {
      this.state.liveCopyHistoryLoading = false;
      this.renderLiveCopyHistoryModal();
    }
  }

  renderLiveCopyHistoryModal() {
    const target = this.q("#live-copy-history-body");
    if (!target) return;
    const data = this.state.liveCopyHistory;
    if (!data || (this.state.liveCopyHistoryLoading && !data)) {
      target.innerHTML = this.state.liveCopyHistoryError
        ? `<div class="live-copy-loading"><strong>跟单记录读取失败</strong><p>${this.escape(this.state.liveCopyHistoryError)}</p><button type="button" data-live-copy-history-refresh>重新读取</button></div>`
        : '<div class="live-copy-loading">正在读取真实成交与收益流水…</div>';
      return;
    }
    const summary = data.summary || {};
    const records = Array.isArray(data.records) ? data.records : [];
    const pnl = (value, digits = 4) => {
      const numeric = Number(value);
      if (value == null || !Number.isFinite(numeric)) return "--";
      return `${numeric > 0 ? "+" : ""}${numeric.toFixed(digits)} USDT`;
    };
    const tone = (value) => Number(value) > 0 ? "positive" : Number(value) < 0 ? "negative" : "flat";
    const statusLabel = { open: "持仓中", closed: "已结束", reconciling: "对账中" };
    const reasonLabel = {
      position_state_unverified: "旧版执行器状态误判（已修复）",
      protection_missing: "旧版保护单校验退出",
      liquidation_buffer_unsafe: "旧版强平缓冲退出",
      max_holding_bars: "旧版持仓时限退出",
      strategy_reversal: "旧版反向信号退出",
      exchange_position_absent: "交易所仓位已结束",
    };
    const availability = data.history_status === "available"
      ? "Binance 收益流水完整"
      : data.history_status === "partial"
      ? "只统计 Binance 可查询时段"
      : `收益流水暂不可用${data.history_error ? ` · ${this.escape(data.history_error)}` : ""}`;
    const rows = records.map((item) => `<tr>
      <td><strong>${this.escape(item.symbol || "--")}</strong><small>${item.direction === "short" ? "做空" : "做多"} · ${this.escape(item.position_side || "--")}</small></td>
      <td>${this.formatDate(item.opened_at)}<small>${item.closed_at ? `结束 ${this.formatDate(item.closed_at)}` : "仍在 Binance 持仓"}</small></td>
      <td>${item.entry_price == null ? "--" : this.escape(this.compactNumber(item.entry_price))}<small>${item.exit_price == null ? `现价 ${item.mark_price == null ? "--" : this.escape(this.compactNumber(item.mark_price))}` : `退出 ${this.escape(this.compactNumber(item.exit_price))}`}</small></td>
      <td class="${tone(item.realized_pnl)}">${pnl(item.realized_pnl)}<small>手续费 ${pnl(item.commission)}</small></td>
      <td class="${tone(item.unrealized_pnl)}">${pnl(item.unrealized_pnl)}<small>资金费 ${pnl(item.funding)}</small></td>
      <td class="${tone(item.net_pnl)}"><strong>${pnl(item.net_pnl)}</strong><small>${this.escape(reasonLabel[item.close_reason] || item.close_reason || "手动持有，不自动结束")}</small></td>
      <td><b class="live-copy-record-status ${this.escape(item.status || "")}">${this.escape(statusLabel[item.status] || item.status || "--")}</b></td>
    </tr>`).join("");
    target.innerHTML = `
      <section class="live-copy-history-note"><strong>手动跟单不会再被后台策略自动结束</strong><span>交易所原生止损 / 止盈仍然有效；你也可以在 Binance 主动平仓。系统只对账，不再因反向信号、持仓时长或状态字段缺失提交市价平仓。</span></section>
      <section class="live-copy-history-summary">
        <article><span>累计净盈亏</span><strong class="${tone(summary.net_pnl)}">${pnl(summary.net_pnl)}</strong><small>已实现 + 浮盈亏 + 手续费 + 资金费</small></article>
        <article><span>已实现盈亏</span><strong class="${tone(summary.realized_pnl)}">${pnl(summary.realized_pnl)}</strong><small>Binance REALIZED_PNL</small></article>
        <article><span>当前浮动盈亏</span><strong class="${tone(summary.unrealized_pnl)}">${pnl(summary.unrealized_pnl)}</strong><small>${this.number(summary.open_count || 0)} 笔持仓中</small></article>
        <article><span>胜率</span><strong>${summary.win_rate_pct == null ? "--" : `${this.number(summary.win_rate_pct)}%`}</strong><small>${this.number(summary.wins || 0)} 胜 / ${this.number(summary.losses || 0)} 负</small></article>
        <article><span>手续费</span><strong class="${tone(summary.commission)}">${pnl(summary.commission)}</strong><small>按 Binance 流水计入净收益</small></article>
      </section>
      <div class="live-copy-history-toolbar"><span>${this.escape(availability)} · 共 ${this.number(summary.total || 0)} 笔</span><button type="button" data-live-copy-history-refresh ${this.state.liveCopyHistoryLoading ? "disabled" : ""}>${this.state.liveCopyHistoryLoading ? "刷新中…" : "刷新记录"}</button></div>
      <div class="live-copy-history-table-wrap"><table class="live-copy-history-table"><thead><tr><th>合约</th><th>时间</th><th>价格</th><th>已实现 / 手续费</th><th>浮盈亏 / 资金费</th><th>净盈亏 / 结束原因</th><th>状态</th></tr></thead><tbody>${rows || '<tr><td colspan="7" class="live-copy-history-empty">还没有手动跟单成交记录</td></tr>'}</tbody></table></div>`;
  }

  openManualFollowModal(opportunityId) {
    const item = this.state.opportunities.find((opportunity) => String(opportunity.id) === String(opportunityId));
    if (!item || !["long", "short"].includes(String(item.direction)) || this.state.opportunityTab !== "current") {
      this.showBanner("该机会已经更新，请刷新后重试。", "error");
      return;
    }
    this.state.manualFollowOpportunityId = String(item.id);
    this.state.manualFollowAttemptId = globalThis.crypto.randomUUID();
    this.renderManualFollowModal();
    const modal = this.q("#manual-follow-modal");
    modal.classList.remove("hidden");
    modal.setAttribute("aria-hidden", "false");
    requestAnimationFrame(() => modal.querySelector(".manual-follow-dialog")?.focus({ preventScroll: true }));
    if (!this.state.liveCopyLoading) void this.loadLiveCopyStatus({ quiet: true });
  }

  closeManualFollowModal() {
    if (this.state.manualFollowLoading) return;
    const modal = this.q("#manual-follow-modal");
    modal.classList.add("hidden");
    modal.setAttribute("aria-hidden", "true");
    this.state.manualFollowOpportunityId = "";
    this.state.manualFollowAttemptId = "";
  }

  renderManualFollowModal() {
    const target = this.q("#manual-follow-body");
    if (!target) return;
    const item = this.state.opportunities.find((opportunity) => String(opportunity.id) === this.state.manualFollowOpportunityId);
    if (!item) {
      target.innerHTML = '<div class="live-copy-loading">请选择一条当前有效机会。</div>';
      return;
    }
    const status = this.state.liveCopy;
    if (!status) {
      target.innerHTML = this.state.liveCopyError
        ? `<div class="live-copy-loading"><strong>实盘状态读取失败</strong><p>${this.escape(this.state.liveCopyError)}</p></div>`
        : '<div class="live-copy-loading">正在读取当前信号与实盘配置…</div>';
      return;
    }
    const account = status.account || {};
    const risk = account.risk || {};
    const direction = item.direction === "short" ? "short" : "long";
    const directionLabel = direction === "short" ? "做空（开 SHORT 仓）" : "做多（开 LONG 仓）";
    const capitalLabel = risk.position_size_basis === "copy_total_amount"
      ? `固定跟单总金额 ${this.number(risk.copy_total_amount)} USDT`
      : "Binance 账户权益";
    const entryState = this.virtualEntryState(item, this.virtualEntryGate(item));
    const manualOverride = !["ready", "triggered"].includes(entryState.tone);
    const accountRunnable = status.enabled === true && account.status === "active" && Boolean(account.id);
    const runnable = accountRunnable;
    const unavailable = accountRunnable ? "" : `<div class="live-copy-config-alert"><strong>当前不能提交手动跟单</strong><p>${status.requested_enabled ? "独立执行域已安全停机，请先处理账户错误并恢复运行。" : "请先开启页面顶部的独立实盘跟单，并完成真实资金确认。"}</p></div>`;
    target.innerHTML = `${unavailable}
      <section class="manual-follow-summary ${direction}">
        <span>${item.prediction_id ? "EXACT PREDICTION" : "EXACT OPPORTUNITY"} · ONE ORDER MAX</span>
        <strong>${this.escape(item.symbol)} / ${this.escape(item.contract_symbol)} · ${directionLabel}</strong>
        <p>${item.prediction_id ? `预测 ${this.escape(item.prediction_id)}` : `待评估机会 ${this.escape(item.id)}`}。人工确认后直接使用 Binance 即时合约价格开仓，不再检查行情缓存时效、预测状态、信号期限、评分、交易时段或研究准入；账户权限、已有仓位、资金与交易所规则仍会校验。</p>
      </section>
      <dl class="manual-follow-risk">
        <div><dt>资金基准</dt><dd>${this.escape(capitalLabel)}</dd></div>
        <div><dt>单笔名义仓位</dt><dd>${this.number(risk.position_size_pct)}%</dd></div>
        <div><dt>杠杆上限</dt><dd>${this.number(risk.leverage)}x</dd></div>
        <div><dt>保护</dt><dd>成交后立即设置止损 / 止盈</dd></div>
        <div><dt>研究状态</dt><dd>${manualOverride ? `${this.escape(entryState.label)} · 人工覆盖准入` : this.escape(entryState.label)}</dd></div>
      </dl>
      <form class="live-copy-form manual-follow-form" data-manual-follow-form>
        <input type="hidden" name="account_id" value="${this.escape(account.id || "")}">
        <input type="hidden" name="opportunity_id" value="${this.escape(item.id)}">
        <input type="hidden" name="prediction_id" value="${this.escape(item.prediction_id || "")}">
        <input type="hidden" name="manual_attempt_id" value="${this.escape(this.state.manualFollowAttemptId)}">
        <input type="hidden" name="expected_contract_symbol" value="${this.escape(item.contract_symbol)}">
        <input type="hidden" name="expected_direction" value="${direction}">
        <label class="live-copy-ack"><input name="acknowledge_real_funds" type="checkbox" required ${this.state.manualFollowLoading ? "disabled" : ""}><span>我确认这会按当前信号方向提交真实 Binance 合约市价单，可能产生真实资金损失。${direction === "short" ? " 当前信号会开空仓，不是买入现货。" : ""}</span></label>
        <button class="danger" type="submit" ${!runnable || this.state.manualFollowLoading ? "disabled" : ""}>${this.state.manualFollowLoading ? "正在提交 Binance…" : `确认${direction === "short" ? "开空" : "跟买"}`}</button>
      </form>`;
  }

  async submitManualFollow(event) {
    const form = event.target.closest("[data-manual-follow-form]");
    if (!form) return;
    event.preventDefault();
    if (this.state.manualFollowLoading) return;
    const data = new FormData(form);
    const item = this.state.opportunities.find((opportunity) => String(opportunity.id) === String(data.get("opportunity_id") || ""));
    if (!item || !["long", "short"].includes(String(item.direction))) {
      this.showBanner("该机会已经更新，请刷新后重试。", "error");
      this.renderManualFollowModal();
      this.renderOpportunities();
      return;
    }
    if (data.get("acknowledge_real_funds") !== "on") {
      this.showBanner("请先确认本次操作会使用真实资金。", "error");
      return;
    }
    const payload = {
      account_id: String(data.get("account_id") || ""),
      opportunity_id: String(data.get("opportunity_id") || ""),
      prediction_id: String(data.get("prediction_id") || "") || null,
      manual_attempt_id: String(data.get("manual_attempt_id") || ""),
      expected_contract_symbol: String(data.get("expected_contract_symbol") || ""),
      expected_direction: String(data.get("expected_direction") || ""),
      acknowledge_real_funds: true,
    };
    this.state.manualFollowLoading = true;
    this.renderManualFollowModal();
    this.renderOpportunities();
    try {
      const result = await this.api("/live-copy/manual-follow", { method: "POST", body: JSON.stringify(payload) });
      const successful = ["filled", "duplicate"].includes(String(result.status || ""));
      this.showBanner(result.message || (successful ? "手动跟单已处理。" : "手动跟单未执行。"), successful ? "success" : "error");
      if (successful) {
        this.state.manualFollowLoading = false;
        this.closeManualFollowModal();
      }
      await Promise.all([this.loadLiveCopyStatus({ quiet: true }), this.loadOpportunities()]);
    } catch (error) {
      this.showBanner(error.message || "手动跟单请求失败", "error");
    } finally {
      this.state.manualFollowLoading = false;
      this.renderManualFollowModal();
      this.renderOpportunities();
    }
  }

  openMacroImpact() {
    const modal = this.q("#macro-impact-modal");
    this.renderMacroImpact();
    modal.classList.remove("hidden");
    modal.setAttribute("aria-hidden", "false");
    requestAnimationFrame(() => modal.querySelector(".macro-impact-dialog")?.focus({ preventScroll: true }));
    void this.loadMacroAiAnalysis();
  }

  closeMacroImpact() {
    const modal = this.q("#macro-impact-modal");
    modal.classList.add("hidden");
    modal.setAttribute("aria-hidden", "true");
  }

  async loadMacroAiAnalysis({ force = false } = {}) {
    if (this.state.macroAiLoading) return;
    this.state.macroAiLoading = true;
    this.state.macroAiError = "";
    this.renderMacroImpact();
    try {
      this.state.macroAi = await this.api(`/market-context/ai-analysis/refresh${force ? "?force=true" : ""}`, { method: "POST" });
    } catch (error) {
      this.state.macroAiError = error.message || "AI 宏观分析暂不可用";
      try {
        this.state.macroAi = await this.api("/market-context/ai-analysis");
      } catch (_) {
        // Keep the last successful interpretation visible.
      }
    } finally {
      this.state.macroAiLoading = false;
      this.renderMacroImpact();
    }
  }

  renderMacroImpact() {
    const target = this.q("#macro-impact-body");
    if (!target) return;
    const data = this.state.macroMarket || {};
    const policy = data.entry_policy || {};
    const curve = data.treasury_curve || {};
    const shock = curve.shock || {};
    const retreat = data.capital_retreat || {};
    const banks = data.central_banks || {};
    const numberOrDash = (value, digits = 2) => Number.isFinite(Number(value)) ? Number(value).toFixed(digits) : "--";
    const bp = (value) => Number.isFinite(Number(value)) ? `${Number(value) >= 0 ? "+" : ""}${Number(value).toFixed(1)}bp` : "--";
    const nominal = Array.isArray(curve.nominal) ? curve.nominal : [];
    const curveItems = [curve.real_10y, curve.breakeven_10y, ...(Array.isArray(curve.curves) ? curve.curves : [])].filter(Boolean);
    const yieldCards = nominal.map((item) => `<article class="macro-yield-card ${item.available ? "" : "unavailable"}"><header><span>${this.escape(item.key || "--")}</span><b>${numberOrDash(item.value, 3)}%</b></header><div><em>1日 ${bp(item.change_bps?.["1d"])}</em><em>5日 ${bp(item.change_bps?.["5d"])}</em><em>20日 ${bp(item.change_bps?.["20d"])}</em></div><small>Z-Score ${numberOrDash(item.zscore, 2)} · ${this.escape(item.as_of || "等待官方数据")}</small></article>`).join("");
    const curveCards = curveItems.map((item) => `<article><span>${this.escape(item.label || item.key || "--")}</span><b>${numberOrDash(item.value, 2)}${item.unit === "bps" ? "bp" : "%"}</b><small>5日 ${bp(item.change_bps?.["5d"])}</small></article>`).join("");
    const bankRows = (Array.isArray(banks.rows) ? banks.rows : []).map((item) => `<tr><td><strong>${this.escape(item.label || item.key)}</strong></td><td>${this.escape(item.policy_rate || "--")}</td><td>${this.escape(item.last_action || "--")}</td><td>${this.escape(item.vote_split || "--")}</td><td>${this.escape(item.next_meeting || "--")}</td><td class="${item.market_path?.available ? "positive" : "flat"}">${this.escape(item.market_path?.label || "未接入")}</td></tr>`).join("");
    const retreatChecks = (Array.isArray(retreat.checks) ? retreat.checks : []).map((item) => `<article class="${item.met ? "met" : item.available ? "clear" : "unknown"}"><span>${item.met ? "✓" : item.available ? "○" : "--"}</span><div><strong>${this.escape(item.label || "确认项")}</strong><small>${this.escape(item.detail || "暂无说明")}</small></div></article>`).join("");
    const sectorRows = (Array.isArray(data.sector_impacts) ? data.sector_impacts : []).map((item) => `<article class="${this.escape(item.state || "neutral")}"><header><strong>${this.escape(item.label || item.key)}</strong><b>${Number(item.adjustment) >= 0 ? "+" : ""}${numberOrDash(item.adjustment, 1)} 分</b></header><p>${this.escape((item.reasons || []).join("；") || "当前无显著宏观偏置")}</p></article>`).join("");
    const sources = Array.isArray(data.data_sources) ? data.data_sources : [];
    const sourceRows = sources.map((item) => `<article class="macro-source-card ${this.escape(item.status || "unavailable")}">
      <header><span>${this.escape(item.tier || "source")}</span><b>${this.escape({ healthy: "正常", scheduled: "等待时段", stale: "旧快照", fallback: "官方回退", degraded: "降级", disabled: "已关闭", unavailable: "不可用" }[item.status] || item.status || "未知")}</b></header>
      <strong>${this.escape(item.label || item.key)}</strong><p>${this.escape(item.role || "")}</p>
      <dl><div><dt>来源</dt><dd>${this.escape(item.source || "--")}</dd></div><div><dt>更新</dt><dd>${this.escape(item.cadence || "--")}</dd></div><div><dt>数据日</dt><dd>${this.escape(item.as_of || "--")}</dd></div><div><dt>最近成功</dt><dd>${item.last_success_at ? this.formatDate(item.last_success_at) : "--"}</dd></div><div><dt>下次检查</dt><dd>${item.next_refresh_at ? this.formatDate(item.next_refresh_at) : "随行情流"}</dd></div></dl>
    </article>`).join("");
    const aiState = this.state.macroAi || {};
    const ai = aiState.analysis || {};
    const aiSectorRows = (Array.isArray(ai.sector_impacts) ? ai.sector_impacts : []).map((item) => `<span class="${this.escape(item.direction || "neutral")}"><b>${this.escape(item.sector || "行业")}</b>${this.escape(item.reason || "")}</span>`).join("");
    const aiConstraints = (Array.isArray(ai.trading_constraints) ? ai.trading_constraints : []).map((item) => `<li>${this.escape(item)}</li>`).join("");
    const aiRisks = (Array.isArray(ai.risks) ? ai.risks : []).map((item) => `<li>${this.escape(item)}</li>`).join("");
    const aiLimitations = (Array.isArray(ai.data_limitations) ? ai.data_limitations : []).map((item) => `<li>${this.escape(item)}</li>`).join("");
    const aiStatus = this.state.macroAiLoading ? "正在调用全局 DeepSeek 分析最新宏观快照…" : this.state.macroAiError || aiState.last_error || (!aiState.configured ? "全局 DeepSeek 尚未配置" : "等待首次宏观分析");
    const policyTone = policy.state === "major_event_credit" || policy.state === "rate_liquidity_shock" ? "danger" : policy.state === "tightening" ? "warning" : "healthy";
    target.innerHTML = `<section class="macro-policy-hero ${policyTone}">
      <div><span>当前准入状态</span><h3>${this.escape(policy.label || "宏观数据不足")}</h3><p>${this.escape((policy.reasons || []).join("；") || "等待形成有效宏观判断")}</p></div>
      <div class="macro-policy-metrics"><article><span>入场门槛</span><b>+${numberOrDash(policy.threshold_delta, 0)} 分</b></article><article><span>多头仓位</span><b>${numberOrDash(Number(policy.long_position_multiplier) * 100, 0)}%</b></article><article><span>空头仓位</span><b>${numberOrDash(Number(policy.short_position_multiplier) * 100, 0)}%</b></article><article><span>顺势多单</span><b>${policy.pause_new_trend_longs ? "暂停新增" : "允许"}</b></article></div>
    </section>
    <section class="macro-impact-section macro-ai-analysis ${this.escape(ai.regime || "neutral")}"><header><div><span>AI · MACRO INTERPRETATION</span><h3>AI 宏观分析</h3></div><div class="macro-ai-actions"><small>${aiState.generated_at ? `${this.formatDate(aiState.generated_at)} · ${this.escape(aiState.model || "DeepSeek")} · ${this.escape(aiState.trigger_reason || "自动更新")}` : "确定性规则先计算，AI 后解释"}</small><button type="button" data-refresh-macro-ai ${this.state.macroAiLoading ? "disabled" : ""}>${this.state.macroAiLoading ? "分析中…" : "重新分析"}</button></div></header>
      ${aiState.available && ai ? `<div class="macro-ai-hero"><div><span>${this.escape(ai.regime || "neutral")}</span><h4>${this.escape(ai.headline || "宏观环境解读")}</h4><p>${this.escape(ai.summary || "")}</p></div><strong>${numberOrDash(ai.confidence, 0)}<small>AI 置信度</small></strong></div>
      <div class="macro-ai-evidence"><article><span>利率冲击</span><p>${this.escape(ai.rate_analysis || "未形成明确结论")}</p></article><article><span>央行路径</span><p>${this.escape(ai.central_bank_analysis || "未形成明确结论")}</p></article><article><span>流动性 / 撤资</span><p>${this.escape(ai.liquidity_analysis || "未形成明确结论")}</p></article></div>
      ${aiSectorRows ? `<div class="macro-ai-sectors">${aiSectorRows}</div>` : ""}
      <div class="macro-ai-lists"><article><strong>执行约束</strong><ul>${aiConstraints || "<li>沿用确定性准入门槛和仓位系数</li>"}</ul></article><article><strong>主要风险</strong><ul>${aiRisks || "<li>暂无新增风险说明</li>"}</ul></article><article><strong>数据限制</strong><ul>${aiLimitations || "<li>无额外说明</li>"}</ul></article></div>` : `<div class="macro-ai-empty"><strong>${this.escape(aiStatus)}</strong><p>AI 只解释当前数据、利率冲击、央行差异和行业影响；不会直接改分、下单或覆盖 Binance 主价格。</p></div>`}
      <footer><b>触发规则</b><span>宏观语义快照变化、分析超过 6 小时或管理员手动刷新；相同快照 15 分钟内去重。</span><em>仅作风险解释，不参与确定性准入计算</em></footer>
    </section>
    <section class="macro-impact-section macro-source-health"><header><div><span>DATA PROVENANCE & SCHEDULE</span><h3>数据来源与更新状态</h3></div><small>官方主源 + 实时参考 + 非阻断回退；失败不覆盖最后成功快照</small></header><div class="macro-source-grid">${sourceRows || "<p>数据来源状态暂不可用</p>"}</div></section>
    <section class="macro-impact-section"><header><div><span>01 · DIRECT TREASURY CURVE</span><h3>美国财政部直接收益率</h3></div><small>${this.escape(curve.as_of || "等待更新")} · 日频官方曲线</small></header><div class="macro-yield-grid">${yieldCards || "<p>收益率曲线暂不可用</p>"}</div><div class="macro-curve-grid">${curveCards}</div><div class="macro-shock-summary ${this.escape(shock.severity || "unknown")}"><strong>${this.escape(shock.label || "冲击类型待确认")}</strong><p>${this.escape([...(shock.reasons || []), ...(shock.impacts || [])].join("；") || "尚未形成可验证的利率冲击分类")}</p></div></section>
    <section class="macro-impact-section"><header><div><span>02 · GLOBAL CENTRAL BANKS</span><h3>全球央行矩阵</h3></div><small>市场预期路径缺源时明确标记，不做推测</small></header><div class="macro-bank-table"><table><thead><tr><th>央行</th><th>政策利率</th><th>最近动作</th><th>投票分歧</th><th>下次会议</th><th>市场路径</th></tr></thead><tbody>${bankRows}</tbody></table></div><div class="macro-bank-spreads">${(banks.spreads || []).map((item) => `<span>${this.escape(item.label)} <b>${numberOrDash(item.value_bps, 1)}bp</b><small>5日 ${bp(item.change_5d_bps)}</small></span>`).join("")}</div></section>
    <section class="macro-impact-section"><header><div><span>03 · CAPITAL RETREAT</span><h3>资金撤退确认 · ${retreat.confirmed ? "已确认" : "未确认"}</h3></div><small>${this.number(retreat.met_count)} / ${this.number(retreat.required_count)} 条满足</small></header><div class="macro-retreat-grid">${retreatChecks}</div><p class="macro-impact-note">只有至少两个独立条件同时成立，才定义为美股全面撤资；不可用的数据不会被当作满足。</p></section>
    <section class="macro-impact-section"><header><div><span>04 · SECTOR SENSITIVITY</span><h3>行业敏感度映射</h3></div><small>影响个股方向评分与仓位，不做全市场统一扣分</small></header><div class="macro-sector-impact-grid">${sectorRows}</div></section>
    <footer class="macro-impact-source"><strong>执行口径</strong><span>Binance 映射合约始终作为交易与结算主价格。宏观层只控制准入门槛、方向放行与仓位系数。</span><small>${this.escape(data.source_note || "")}</small></footer>`;
  }

  renderLiveCopyConfigModal() {
    const target = this.q("#live-copy-config-body");
    if (!target) return;
    const status = this.state.liveCopy;
    if (!status) {
      target.innerHTML = this.state.liveCopyError
        ? `<div class="live-copy-loading"><strong>实盘参数读取失败</strong><p>${this.escape(this.state.liveCopyError)}</p></div>`
        : `<div class="live-copy-loading">正在读取独立实盘风险参数…</div>`;
      return;
    }
    const account = status.account || {};
    const risk = account.risk || {};
    const positionSizeBasis = risk.position_size_basis === "copy_total_amount" ? "copy_total_amount" : "account_equity";
    const modeMismatch = account.last_error_code === "position_mode_changed";
    const modeWarning = modeMismatch
      ? `<div class="live-copy-config-alert"><strong>当前未下单的直接原因：持仓模式不一致</strong><p>独立账户已启用，但 Binance 实际为双向持仓；当前配置缺少对应模式，因此执行器已安全停机且没有提交任何订单。这里已建议“对冲 / 双向持仓”，保存后会重新校验。</p></div>`
      : "";
    const directionLabel = `${risk.allow_long !== false ? "做多" : ""}${risk.allow_long !== false && risk.allow_short !== false ? " + " : ""}${risk.allow_short !== false ? "做空" : ""}`;
    target.innerHTML = `${modeWarning}
      <section class="live-copy-config-summary">
        <span>${status.enabled ? "实盘运行中" : status.requested_enabled ? "已启用但安全停机" : "当前关闭"}</span>
        <strong>新信号方向：${this.escape(directionLabel || "未配置")}</strong>
        <p>开仓使用市价单，成交后立即建立交易所止损和止盈；参数只对后续新开仓生效，已有仓位不重写。</p>
      </section>
      <form class="live-copy-config-form" data-live-copy-config-form>
        <input type="hidden" name="account_id" value="${this.escape(account.id || "")}">
        <fieldset><legend>账户与持仓模式</legend><div class="live-copy-config-grid">
          <label><span>Binance 持仓模式</span><select name="position_mode"><option value="one_way" ${risk.position_mode === "one_way" ? "selected" : ""}>单向持仓</option><option value="hedge" ${risk.position_mode === "hedge" ? "selected" : ""}>对冲 / 双向持仓</option></select><small>必须与交易所账户实际模式一致</small></label>
          <label><span>杠杆上限</span><input name="leverage" type="number" min="1" max="20" step="1" value="${this.escape(risk.leverage ?? 10)}"><small>最高 20x；实际仓位仍受风险预算约束</small></label>
          <label><span>最大同时持仓</span><input name="max_positions" type="number" min="1" max="20" step="1" value="${this.escape(risk.max_positions ?? 10)}"><small>仅本独立执行域</small></label>
          <label><span>开仓资金基准</span><select name="position_size_basis"><option value="account_equity" ${positionSizeBasis === "account_equity" ? "selected" : ""}>账户权益</option><option value="copy_total_amount" ${positionSizeBasis === "copy_total_amount" ? "selected" : ""}>固定跟单总金额</option></select><small>决定仓位与风险百分比的计算基数</small></label>
          <label data-copy-total-amount-field><span>跟单总金额（USDT）</span><input name="copy_total_amount" type="number" min="1" max="1000000000" step="0.01" value="${this.escape(risk.copy_total_amount ?? 1000)}"><small>仅固定总金额模式生效；不会突破真实可用余额</small></label>
          <label><span>单笔名义仓位</span><input name="position_size_pct" type="number" min="0.1" max="20" step="0.1" value="${this.escape(risk.position_size_pct ?? 2)}"><small data-capital-basis-help="single">占账户权益 %</small></label>
        </div></fieldset>
        <fieldset><legend>风险边界</legend><div class="live-copy-config-grid">
          <label><span>单笔风险上限</span><input name="risk_per_trade_pct" type="number" min="0.1" max="5" step="0.1" value="${this.escape(risk.risk_per_trade_pct ?? 0.5)}"><small data-capital-basis-help="single">占账户权益 %</small></label>
          <label><span>总风险上限</span><input name="max_total_risk_pct" type="number" min="0.5" max="50" step="0.1" value="${this.escape(risk.max_total_risk_pct ?? 4)}"><small data-capital-basis-help="total">全部独立跟单仓位合计，占账户权益 %</small></label>
          <label><span>保证金占用上限</span><input name="margin_cap_pct" type="number" min="1" max="100" step="0.1" value="${this.escape(risk.margin_cap_pct ?? 20)}"><small data-capital-basis-help="single">占账户权益 %</small></label>
          <label><span>单日亏损上限</span><input name="daily_loss_limit_pct" type="number" min="0.5" max="20" step="0.1" value="${this.escape(risk.daily_loss_limit_pct ?? 2)}"><small>触发后停止新开仓</small></label>
          <label><span>最大回撤上限</span><input name="max_drawdown_pct" type="number" min="1" max="50" step="0.1" value="${this.escape(risk.max_drawdown_pct ?? 6)}"><small>触发后安全停机</small></label>
          <label><span>往返成本估计</span><input name="round_trip_cost_bps" type="number" min="16" max="500" step="0.1" value="${this.escape(Math.max(16, Number(risk.round_trip_cost_bps ?? 16)))}"><small>手续费 + 滑点，最低按 16 bps 计入</small></label>
        </div></fieldset>
        <fieldset><legend>信号准入</legend><div class="live-copy-config-grid">
          <label><span>最低组合评分</span><input name="minimum_combined_score" type="number" min="0" max="100" step="0.1" value="${this.escape(risk.minimum_combined_score ?? 70)}"><small>低于该分数不会实盘开仓</small></label>
          <label><span>信号最大延迟</span><input name="signal_max_age_seconds" type="number" min="60" max="1800" step="30" value="${this.escape(risk.signal_max_age_seconds ?? 300)}"><small>秒；超时信号不追单</small></label>
          <label class="live-copy-direction"><span>允许方向</span><span><input name="allow_long" type="checkbox" ${risk.allow_long !== false ? "checked" : ""}> 做多</span><span><input name="allow_short" type="checkbox" ${risk.allow_short !== false ? "checked" : ""}> 做空</span><small>至少保留一个方向</small></label>
        </div></fieldset>
        <div class="live-copy-config-actions"><span>保存不会开启当前已关闭的实盘跟单。</span><button type="submit" ${this.state.liveCopyConfigLoading ? "disabled" : ""}>${this.state.liveCopyConfigLoading ? "保存中…" : "保存实盘跟单配置"}</button></div>
      </form>`;
    this.syncLiveCopyPositionSizing(target.querySelector("[data-live-copy-config-form]"));
  }

  syncLiveCopyPositionSizing(form) {
    if (!form) return;
    const select = form.querySelector('[name="position_size_basis"]');
    const amount = form.querySelector('[name="copy_total_amount"]');
    const amountField = form.querySelector("[data-copy-total-amount-field]");
    const fixedAmount = select?.value === "copy_total_amount";
    if (amount) {
      amount.readOnly = !fixedAmount;
      amount.setAttribute("aria-disabled", fixedAmount ? "false" : "true");
    }
    amountField?.classList.toggle("inactive", !fixedAmount);
    form.querySelectorAll("[data-capital-basis-help]").forEach((help) => {
      const base = fixedAmount ? "跟单总金额" : "账户权益";
      help.textContent = help.dataset.capitalBasisHelp === "total"
        ? `全部独立跟单仓位合计，占${base} %`
        : `占${base} %`;
    });
  }

  async submitLiveCopyConfig(event) {
    const form = event.target.closest("[data-live-copy-config-form]");
    if (!form) return;
    event.preventDefault();
    if (this.state.liveCopyConfigLoading) return;
    const data = new FormData(form);
    const payload = {
      account_id: String(data.get("account_id") || "") || null,
      position_mode: String(data.get("position_mode") || "one_way"),
      leverage: Number(data.get("leverage")),
      max_positions: Number(data.get("max_positions")),
      position_size_basis: String(data.get("position_size_basis") || "account_equity"),
      copy_total_amount: Number(data.get("copy_total_amount")),
      position_size_pct: Number(data.get("position_size_pct")),
      risk_per_trade_pct: Number(data.get("risk_per_trade_pct")),
      max_total_risk_pct: Number(data.get("max_total_risk_pct")),
      margin_cap_pct: Number(data.get("margin_cap_pct")),
      daily_loss_limit_pct: Number(data.get("daily_loss_limit_pct")),
      max_drawdown_pct: Number(data.get("max_drawdown_pct")),
      round_trip_cost_bps: Number(data.get("round_trip_cost_bps")),
      signal_max_age_seconds: Number(data.get("signal_max_age_seconds")),
      minimum_combined_score: Number(data.get("minimum_combined_score")),
      allow_long: data.get("allow_long") === "on",
      allow_short: data.get("allow_short") === "on",
    };
    if (!payload.allow_long && !payload.allow_short) {
      this.showBanner("做多和做空不能同时关闭", "error");
      return;
    }
    if (payload.position_size_basis === "copy_total_amount" && (!Number.isFinite(payload.copy_total_amount) || payload.copy_total_amount <= 0)) {
      this.showBanner("跟单总金额必须大于 0 USDT", "error");
      return;
    }
    this.state.liveCopyConfigLoading = true;
    this.renderLiveCopyConfigModal();
    try {
      this.state.liveCopy = await this.api("/live-copy/config", { method: "PUT", body: JSON.stringify(payload) });
      this.renderLiveCopyToggle();
      this.renderLiveCopyModal();
      this.closeLiveCopyConfigModal();
      this.showBanner("发现机会独立实盘参数已保存；只对后续新开仓生效。", "success");
    } catch (error) {
      this.showBanner(error.message || "实盘跟单配置保存失败", "error");
    } finally {
      this.state.liveCopyConfigLoading = false;
      this.renderLiveCopyConfigModal();
    }
  }

  renderLiveCopyModal() {
    const target = this.q("#live-copy-body");
    if (!target) return;
    const status = this.state.liveCopy;
    if (!status) {
      target.innerHTML = this.state.liveCopyError
        ? `<div class="live-copy-loading"><strong>独立跟单状态读取失败</strong><p>${this.escape(this.state.liveCopyError)}</p><button type="button" data-live-copy-retry>重新读取</button></div>`
        : `<div class="live-copy-loading">正在读取 AI 机会独立执行域…</div>`;
      return;
    }
    const account = status.account || null;
    const risk = account?.risk || {};
    const blockers = Array.isArray(status.blockers) ? status.blockers : [];
    const riskMarkup = account ? `<section class="live-copy-account">
      <header><div><span>AI 机会独立执行域</span><strong>${this.escape(account.name)}</strong><small>${account.provisioned ? "独立账户与订单命名空间已建立" : "首次开启时自动建立；无需前往实盘交易页"}</small></div><b class="${account.status === "active" ? "active" : "paused"}">${account.status === "active" ? "运行中" : "独立待命"}</b></header>
      <dl>
        <div><dt>持仓模式</dt><dd>${risk.position_mode === "hedge" ? "双向" : "单向"}</dd></div>
        <div><dt>杠杆上限</dt><dd>${this.number(risk.leverage)}x</dd></div>
        <div><dt>最大持仓</dt><dd>${this.number(risk.max_positions)} 个</dd></div>
        <div><dt>开仓资金基准</dt><dd>${risk.position_size_basis === "copy_total_amount" ? "固定总金额" : "账户权益"}</dd></div>
        ${risk.position_size_basis === "copy_total_amount" ? `<div><dt>跟单总金额</dt><dd>${this.number(risk.copy_total_amount)} USDT</dd></div>` : ""}
        <div><dt>单笔仓位</dt><dd>${this.number(risk.position_size_pct)}%</dd></div>
        <div><dt>单笔风险</dt><dd>${this.number(risk.risk_per_trade_pct)}%</dd></div>
        <div><dt>总风险上限</dt><dd>${this.number(risk.max_total_risk_pct)}%</dd></div>
        <div><dt>保证金上限</dt><dd>${this.number(risk.margin_cap_pct)}%</dd></div>
        <div><dt>日亏损上限</dt><dd>${this.number(risk.daily_loss_limit_pct)}%</dd></div>
        <div><dt>回撤上限</dt><dd>${this.number(risk.max_drawdown_pct)}%</dd></div>
        <div><dt>信号时效</dt><dd>${this.number(risk.signal_max_age_seconds)} 秒</dd></div>
        <div><dt>最低组合分</dt><dd>${this.number(risk.minimum_combined_score)}</dd></div>
        <div><dt>允许开仓时段</dt><dd>${risk.regular_session_only !== false ? "仅美股常规盘" : "不限时段"}</dd></div>
        <div><dt>允许方向</dt><dd>${risk.allow_long !== false ? "多" : ""}${risk.allow_long !== false && risk.allow_short !== false ? " / " : ""}${risk.allow_short !== false ? "空" : ""}</dd></div>
      </dl>
    </section>` : "";
    const isolation = `<section class="live-copy-isolation"><span>ISOLATED SIGNAL SOURCE</span><strong>不读取、不启停、不改写实盘交易页的其他策略</strong><p>本执行域只消费“发现机会”信号，并维护独立的账户记录、部署状态、幂等订单键和风控快照。Binance API 凭据及交易所总权益仍属于同一用户账户。</p></section>`;
    const guarantees = `<section class="live-copy-guarantees"><strong>执行边界</strong><ul><li>只接收开启后生成、入场条件全部通过的本页新信号</li><li>第一阶段仅在美股常规交易时段允许新开仓；交易、盈亏和结算价格均以 Binance 映射合约为准</li><li>Finnhub、Unusual Whales、新闻、期权/GEX 与暗池只参与辅助评分和风控，不作为执行价格</li><li>普通实盘策略暂停、启用或修改，不影响这里的跟单开关</li><li>同一预测使用唯一订单键，重复刷新不会重复开仓</li><li>成交后必须建立交易所止损与止盈；保护失败会平仓并停机</li><li>关闭跟单只停止新开仓，不会擅自平掉已有持仓</li></ul></section>`;
    if (status.enabled) {
      target.innerHTML = `<div class="live-copy-danger"><span>REAL FUNDS ACTIVE</span><strong>发现机会独立跟单已开启</strong><p>仅本页满足准入条件的新 AI 信号会自动提交 Binance 订单。</p></div>${isolation}${riskMarkup}${guarantees}<form class="live-copy-form" data-live-copy-mode="disable"><input type="hidden" name="account_id" value="${this.escape(account?.id || "")}"><div class="live-copy-disable-note">停止后不再开新仓；本执行域已有仓位的止损、止盈和退出管理继续运行。</div><button class="danger" type="submit" ${this.state.liveCopyLoading ? "disabled" : ""}>停止独立实盘跟单</button></form>`;
      return;
    }
    const blockerMarkup = blockers.length
      ? `<section class="live-copy-blockers"><strong>独立执行域尚缺少必要条件</strong>${blockers.map((item) => `<span>× ${this.escape(item)}</span>`).join("")}</section>`
      : "";
    const enableForm = status.ready_to_enable
      ? `<form class="live-copy-form" data-live-copy-mode="enable">
          <input type="hidden" name="account_id" value="${this.escape(account?.id || "")}">
          <button class="danger" type="submit" ${this.state.liveCopyLoading ? "disabled" : ""}>确认开启独立实盘跟单</button>
        </form>`
      : "";
    target.innerHTML = `<div class="live-copy-safe"><span>DEFAULT OFF</span><strong>发现机会独立跟单当前关闭</strong><p>本页仍只记录预测；开启后系统会自动建立独立执行域，不要求实盘交易页的其他策略处于启用状态。</p></div>${isolation}${riskMarkup}${blockerMarkup}${guarantees}${enableForm}`;
  }

  async submitLiveCopy(event) {
    const form = event.target.closest("[data-live-copy-mode]");
    if (!form) return;
    event.preventDefault();
    if (this.state.liveCopyLoading) return;
    const mode = form.dataset.liveCopyMode;
    const data = new FormData(form);
    const payload = mode === "enable"
      ? {
          enabled: true,
          account_id: String(data.get("account_id") || "") || null,
        }
      : {
          enabled: false,
          account_id: String(data.get("account_id") || "") || null,
        };
    this.state.liveCopyLoading = true;
    this.renderLiveCopyToggle();
    this.renderLiveCopyModal();
    try {
      this.state.liveCopy = await this.api("/live-copy", { method: "PUT", body: JSON.stringify(payload) });
      this.renderLiveCopyToggle();
      this.closeLiveCopyModal();
      this.showBanner(
        payload.enabled
          ? "发现机会独立跟单已开启：只处理本页开启后产生的新信号，不受其他实盘策略开关影响。"
          : "发现机会独立跟单已停止：不再开新仓，已有仓位的保护与退出管理保持有效。",
        payload.enabled ? "error" : "success",
      );
    } catch (error) {
      this.showBanner(error.message || "实盘跟单设置保存失败", "error");
    } finally {
      this.state.liveCopyLoading = false;
      this.renderLiveCopyToggle();
      this.renderLiveCopyModal();
    }
  }

  renderUnusualWhalesToggle() {
    const button = this.q("#uw-usage-toggle");
    if (!button) return;
    const policy = this.state.scorePolicy;
    const enabled = policy?.enabled !== false;
    const loading = !policy || this.state.uwToggleLoading;
    const session = String(this.state.macroMarket?.market_session?.key || "closed");
    const collecting = enabled && session === "regular";
    button.className = `uw-usage-toggle ${loading ? "loading" : enabled ? "enabled" : "disabled"}`;
    button.setAttribute("aria-pressed", enabled ? "true" : "false");
    button.disabled = loading || policy?.can_edit !== true;
    button.title = policy?.can_edit === false
      ? "仅管理员可以调整平台 Unusual Whales 总开关"
      : enabled
      ? collecting
        ? "美股常规交易时段正在采集；REST 最多每 5 分钟刷新一次"
        : "已启用，休市期间不会连接或调用 Unusual Whales"
      : "已关闭；采集、评分与门控均不使用 Unusual Whales。点击启用";
    const status = button.querySelector("small");
    if (status) status.textContent = loading ? "切换中" : !enabled ? "已关闭" : collecting ? "盘中 5分钟/次" : "休市待机";
  }

  async toggleUnusualWhales() {
    const policy = this.state.scorePolicy;
    if (!policy || policy.can_edit !== true || this.state.uwToggleLoading) return;
    const enabled = policy.enabled === false;
    this.state.uwToggleLoading = true;
    this.renderUnusualWhalesToggle();
    try {
      this.state.scorePolicy = await this.api("/unusual-whales-enabled", {
        method: "PUT",
        body: JSON.stringify({ enabled }),
      });
      this.renderUnusualWhalesToggle();
      await Promise.all([
        this.loadMarketContext(),
        this.state.view === "opportunities" ? this.loadOpportunities() : Promise.resolve(),
      ]);
      this.showBanner(
        enabled
          ? "Unusual Whales 已启用，REST 数据按 5 分钟频率刷新"
          : "Unusual Whales 已关闭，采集、评分与门控已完全旁路",
        "success",
      );
    } catch (error) {
      this.showBanner(error.message || "Unusual Whales 开关保存失败", "error");
    } finally {
      this.state.uwToggleLoading = false;
      this.renderUnusualWhalesToggle();
    }
  }

  renderFinnhubToggle() {
    const button = this.q("#finnhub-usage-toggle");
    if (!button) return;
    const policy = this.state.scorePolicy;
    const enabled = policy?.finnhub_enabled !== false;
    const loading = !policy || this.state.finnhubToggleLoading;
    const session = String(this.state.macroMarket?.market_session?.key || "closed");
    const collecting = enabled && session === "regular";
    button.className = `market-data-toggle finnhub-usage-toggle ${loading ? "loading" : enabled ? "enabled" : "disabled"} ${collecting ? "collecting" : "standby"}`;
    button.setAttribute("aria-pressed", enabled ? "true" : "false");
    button.disabled = loading || policy?.can_edit !== true;
    button.title = policy?.can_edit === false
      ? "仅管理员可以调整平台 Finnhub 美股现货总开关"
      : enabled
      ? collecting
        ? "美股常规交易时段正在采集；最新报价按股票写入数据库"
        : "已启用，休市期间暂停调用；页面继续读取各股票最新入库报价"
      : "已关闭；不采集，也不把 Finnhub 现货用于机会展示";
    const status = button.querySelector("small");
    if (status) status.textContent = loading ? "切换中" : !enabled ? "已关闭" : collecting ? "盘中采集" : "休市待机";
  }

  async toggleFinnhub() {
    const policy = this.state.scorePolicy;
    if (!policy || policy.can_edit !== true || this.state.finnhubToggleLoading) return;
    const enabled = policy.finnhub_enabled === false;
    this.state.finnhubToggleLoading = true;
    this.renderFinnhubToggle();
    try {
      this.state.scorePolicy = await this.api("/finnhub-enabled", {
        method: "PUT",
        body: JSON.stringify({ enabled }),
      });
      this.renderFinnhubToggle();
      await Promise.all([
        this.loadMarketContext(),
        this.state.view === "opportunities" ? this.loadOpportunities() : Promise.resolve(),
      ]);
      this.showBanner(
        enabled
          ? "Finnhub 美股现货已启用；仅常规交易时段采集并持续写入数据库"
          : "Finnhub 美股现货已关闭；机会卡片不再使用该数据源",
        "success",
      );
    } catch (error) {
      this.showBanner(error.message || "Finnhub 美股现货开关保存失败", "error");
    } finally {
      this.state.finnhubToggleLoading = false;
      this.renderFinnhubToggle();
    }
  }

  firstValue(...values) {
    return values.find((value) => value !== undefined && value !== null && value !== "");
  }

  featureIsAvailable(source) {
    if (!source || typeof source !== "object" || Array.isArray(source)) return false;
    if (source.available === false || source.missing === true) return false;
    const state = String(this.firstValue(source.status, source.state, source.data_status, "")).toLowerCase();
    if (["missing", "unavailable", "not_available", "not_evaluated"].includes(state)) return false;
    return Object.keys(source).some((key) => !["available", "status", "state", "data_status", "note", "reason"].includes(key) && source[key] !== null && source[key] !== undefined && source[key] !== "");
  }

  patchStablePanel(target, markup) {
    const template = document.createElement("template");
    template.innerHTML = markup;
    const incoming = [...template.content.children];
    const existing = [...target.children];
    const keyFor = (node, index) => node.dataset?.patchKey || `${node.tagName}:${node.className || index}`;
    const structureMatches = existing.length === incoming.length
      && existing.every((node, index) => keyFor(node, index) === keyFor(incoming[index], index));
    if (!structureMatches) {
      target.replaceChildren(...incoming);
      return;
    }
    incoming.forEach((nextNode, index) => {
      const currentNode = existing[index];
      if (currentNode.outerHTML === nextNode.outerHTML) return;
      const previousCountdown = currentNode.querySelector?.("[data-market-countdown-value]")?.textContent;
      const nextCountdown = nextNode.querySelector?.("[data-market-countdown-value]");
      if (previousCountdown && nextCountdown) nextCountdown.textContent = previousCountdown;
      nextNode.dataset.updated = "true";
      currentNode.replaceWith(nextNode);
      window.setTimeout(() => nextNode.removeAttribute("data-updated"), 700);
    });
  }

  normalizeFeatureSnapshot(item) {
    const evidence = item?.evidence || {};
    const stableFlow = item?.flow && typeof item.flow === "object" ? item.flow : {};
    const scoreComponents = item?.score_components && typeof item.score_components === "object" ? item.score_components : {};
    const quote = { ...(item?.quote || evidence.quote || evidence.market_quote || evidence.market_quality?.quote || {}) };
    const optionFlow = { ...(item?.option_flow || stableFlow.option_flow || evidence.option_flow || evidence.options_flow || {}) };
    const gex = { ...(item?.gex || stableFlow.gex || evidence.gex || evidence.gex_levels || {}) };
    const institutional = { ...(item?.institutional_flow || stableFlow.institutional_flow || evidence.institutional_flow || evidence.offlit || evidence.off_lit || {}) };
    const dataQuality = { ...(item?.data_quality || evidence.data_quality || evidence.feature_coverage || {}) };
    if (quote.quality_passed == null && item?.quote_quality_passed != null) quote.quality_passed = item.quote_quality_passed;
    if (quote.age_ms == null && item?.quote_age_ms != null) quote.age_ms = item.quote_age_ms;
    if (quote.spread_bps == null && item?.quote_spread_bps != null) quote.spread_bps = item.quote_spread_bps;
    if (quote.session == null && item?.market_session) quote.session = item.market_session;
    if (optionFlow.score == null && scoreComponents.option_flow != null) optionFlow.score = scoreComponents.option_flow;
    if (optionFlow.score == null && item?.option_flow_score != null) optionFlow.score = item.option_flow_score;
    if (optionFlow.bias == null && item?.option_flow_bias) optionFlow.bias = item.option_flow_bias;
    if (gex.score == null && scoreComponents.gex != null) gex.score = scoreComponents.gex;
    if (gex.score == null && item?.gex_score != null) gex.score = item.gex_score;
    if (gex.regime == null && item?.gex_regime) gex.regime = item.gex_regime;
    if (institutional.score == null && scoreComponents.institutional_flow != null) institutional.score = scoreComponents.institutional_flow;
    if (institutional.score == null && item?.institutional_score != null) institutional.score = item.institutional_score;
    if (dataQuality.coverage == null && item?.data_coverage != null) dataQuality.coverage = item.data_coverage;
    if (dataQuality.status == null && item?.data_quality_status) dataQuality.status = item.data_quality_status;
    const riskEvents = Array.isArray(item?.risk_events)
      ? [...item.risk_events]
      : Array.isArray(evidence.risk_events)
      ? [...evidence.risk_events]
      : Array.isArray(evidence.events)
      ? [...evidence.events]
      : [];
    if (!riskEvents.length && item?.event_risk && item.event_risk !== "clear") {
      riskEvents.push({ risk_level: item.event_risk, title: item.event_title || "事件风险快照" });
    }
    return {
      quote,
      optionFlow,
      gex,
      institutional,
      dataQuality,
      riskEvents,
      scoreComponents,
      version: item?.version && typeof item.version === "object" ? item.version : {},
      apiVersion: this.firstValue(item?.api_version, item?.version?.api, "--"),
      signalSnapshot: item?.signal_snapshot && typeof item.signal_snapshot === "object" ? item.signal_snapshot : {},
    };
  }

  renderSignalHealth() {
    const target = this.q("#signal-health-strip");
    if (!target) return;
    const overview = this.state.overview || {};
    const macro = this.state.macroMarket || {};
    const health = overview.data_health || overview.market_data_health || overview.realtime_health || overview.streaming || {};
    const sourceHealth = health.sources || overview.data_sources || {};
    const providers = macro.providers || {};
    const websocketConnected = Boolean(this.firstValue(
      health.websocket_connected,
      health.ws_connected,
      health.stream_connected,
      sourceHealth.websocket?.connected,
      sourceHealth.unusual_whales?.websocket_connected,
      false,
    ));
    const incrementalStreamConnected = this.state.updateStreamStatus === "connected";
    const restHealthy = this.firstValue(
      health.rest_healthy,
      health.api_healthy,
      sourceHealth.rest?.healthy,
      providers.unusual_whales_configured ? true : undefined,
      Boolean(this.state.lastSuccessfulRefreshAt),
    ) !== false;
    const lastEventAt = this.firstValue(
      health.last_event_at,
      health.last_message_at,
      sourceHealth.websocket?.last_message_at,
      macro.captured_at,
      this.state.lastSuccessfulRefreshAt,
    );
    const quoteHealth = health.quote || health.quotes || {};
    const quoteAgeMs = Number(this.firstValue(quoteHealth.age_ms, quoteHealth.quote_age_ms, health.quote_age_ms));
    const quoteCoverageRaw = Number(this.firstValue(quoteHealth.coverage, quoteHealth.coverage_ratio, health.quote_coverage));
    const opportunities = Array.isArray(this.state.opportunities) ? this.state.opportunities : [];
    const moduleCounts = opportunities.reduce((counts, item) => {
      const snapshot = this.normalizeFeatureSnapshot(item);
      if (this.featureIsAvailable(snapshot.quote)) counts.quote += 1;
      if (this.featureIsAvailable(snapshot.optionFlow)) counts.optionFlow += 1;
      if (this.featureIsAvailable(snapshot.gex)) counts.gex += 1;
      if (this.featureIsAvailable(snapshot.institutional)) counts.institutional += 1;
      if (snapshot.riskEvents.length) counts.events += 1;
      return counts;
    }, { quote: 0, optionFlow: 0, gex: 0, institutional: 0, events: 0 });
    const sampleCount = opportunities.length;
    const quoteCoverage = Number.isFinite(quoteCoverageRaw)
      ? (quoteCoverageRaw <= 1 ? quoteCoverageRaw * 100 : quoteCoverageRaw)
      : sampleCount ? moduleCounts.quote / sampleCount * 100 : 0;
    const moduleCoverage = sampleCount
      ? (moduleCounts.optionFlow + moduleCounts.gex + moduleCounts.institutional) / (sampleCount * 3) * 100
      : 0;
    const pipelineTone = this.state.lastRefreshError ? "danger" : websocketConnected || incrementalStreamConnected ? "healthy" : restHealthy ? "degraded" : "danger";
    const pipelineLabel = this.state.lastRefreshError
      ? "更新异常"
      : websocketConnected
      ? "行情实时流在线"
      : incrementalStreamConnected
      ? "页面增量推送在线"
      : restHealthy
      ? "REST 轮询降级"
      : "数据源离线";
    const quoteTone = quoteCoverage >= 90 && (!Number.isFinite(quoteAgeMs) || quoteAgeMs <= 2000) ? "healthy" : quoteCoverage > 0 ? "degraded" : "neutral";
    const featureTone = moduleCoverage >= 80 ? "healthy" : moduleCoverage > 0 ? "degraded" : "neutral";
    const versions = health.versions || overview.versions || {};
    this.patchStablePanel(target, `<header data-patch-key="health-heading"><span>DATA HEALTH</span><strong>触发数据状态</strong><small>异常只阻断新触发，不会清空已展示机会。</small></header>
      <div class="signal-health-grid" data-patch-key="health-grid">
        <article class="${pipelineTone}"><span>传输状态</span><b><i></i>${this.escape(pipelineLabel)}</b><small>${lastEventAt ? `最后事件 ${this.formatDate(lastEventAt)}` : "等待首次数据"}</small></article>
        <article class="${quoteTone}"><span>Quote 行情质量</span><b>${quoteCoverage.toFixed(0)}% 覆盖</b><small>${Number.isFinite(quoteAgeMs) ? `最新延迟 ${Math.round(quoteAgeMs)} ms` : "按机会数据覆盖估算"}</small></article>
        <article class="${featureTone}"><span>增强特征覆盖</span><b>${moduleCoverage.toFixed(0)}%</b><small>期权流 ${moduleCounts.optionFlow}/${sampleCount} · GEX ${moduleCounts.gex}/${sampleCount} · 机构 ${moduleCounts.institutional}/${sampleCount}</small></article>
        <article class="neutral"><span>决策版本</span><b>${this.escape(this.firstValue(versions.decision, health.decision_version, "兼容模式"))}</b><small>特征 ${this.escape(this.firstValue(versions.feature, health.feature_version, "--"))} · 权重 ${this.escape(this.firstValue(versions.weights, health.weights_version, "--"))}</small></article>
      </div>`);
  }

  renderMacroMarket() {
    const target = this.q("#macro-market-panel");
    if (!target) return;
    const data = this.state.macroMarket || {};
    const numberOrDash = (value, digits = 2) => Number.isFinite(Number(value)) ? Number(value).toFixed(digits) : "--";
    const percent = (value) => Number.isFinite(Number(value)) ? `${Number(value) >= 0 ? "+" : ""}${Number(value).toFixed(2)}%` : "--";
    const tone = (value) => Number(value) > 0 ? "positive" : Number(value) < 0 ? "negative" : "flat";
    const sentiment = data.sentiment || {};
    const indices = Array.isArray(data.indices) ? data.indices : [];
    const sectors = Array.isArray(data.sectors) ? data.sectors : [];
    const assets = Array.isArray(data.macro_assets) ? data.macro_assets : [];
    const breadth = data.breadth || {};
    const vix = data.vix || {};
    const session = data.market_session || {};
    const tide = data.market_tide || {};
    const providers = data.providers || {};
    const eventState = data.events || {};
    const entryPolicy = data.entry_policy || {};
    const nextEvent = eventState.next_event || null;
    const indexCards = indices.map((item) => {
      const liveProxy = item.realtime_proxy || null;
      const proxyLine = liveProxy?.available
        ? `<span class="macro-live-proxy"><i></i>${this.escape(liveProxy.provider_symbol || "ETF")} ${numberOrDash(liveProxy.price)} · ${this.escape(({ premarket: "盘前", regular: "盘中", postmarket: "盘后", closed: "休市" })[liveProxy.market_time] || session.label || "行情")}</span>`
        : "";
      return `<article class="macro-index-card ${item.available ? "" : "unavailable"}">
      <header><strong>${this.escape(item.label || item.key)}</strong><small>${this.escape(item.provider_symbol || "--")} ${item.proxy ? "代理" : "指数"}</small></header>
      <b>${numberOrDash(item.price)}</b>
      <span class="${tone(item.change_percent)}">${percent(item.change_percent)}</span>
      <footer><span>日内 ${percent(item.intraday_change_percent)}</span><span>振幅 ${percent(item.amplitude_percent)}</span>${item.rsi_14_1h == null ? "" : `<span>RSI ${numberOrDash(item.rsi_14_1h, 1)}</span>`}${proxyLine}</footer>
    </article>`;
    }).join("");
    const sectorPills = sectors.map((item) => `<span class="macro-sector ${tone(item.change_percent)}"><em>${this.escape(item.label || item.key)}</em><b>${percent(item.change_percent)}</b><small>${this.escape(item.provider_symbol || "--")} 代理</small></span>`).join("");
    const assetPills = assets.map((item) => `<span class="macro-asset ${tone(item.change_percent)}"><em>${this.escape(item.label || item.key)}</em><b>${numberOrDash(item.price)}</b><small>${percent(item.change_percent)} · ${this.escape(item.provider_symbol || "--")}</small></span>`).join("");
    const breadthRatio = Number.isFinite(Number(breadth.advance_decline_ratio)) ? Number(breadth.advance_decline_ratio).toFixed(2) : "--";
    const vixTone = Number(vix.value) >= 30 ? "danger" : Number(vix.value) >= 25 ? "warning" : "normal";
    const eventTone = eventState.risk_level === "critical" || eventState.risk_level === "high" ? "danger" : eventState.risk_level === "medium" ? "warning" : "normal";
    const tideTone = tide.bias === "bull" ? "positive" : tide.bias === "bear" ? "negative" : "flat";
    const tideLabel = tide.bias === "bull" ? "资金偏多" : tide.bias === "bear" ? "资金偏空" : "资金中性";
    const sessionKey = session.key || "closed";
    const sessionActive = Boolean(session.realtime_expected && session.upstream_confirmed);
    const countdownTarget = session.countdown_target_at ? new Date(session.countdown_target_at) : null;
    const countdownTargetValid = countdownTarget && !Number.isNaN(countdownTarget.getTime());
    const countdownEt = countdownTargetValid ? new Intl.DateTimeFormat("zh-CN", { timeZone: "America/New_York", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false }).format(countdownTarget) : "--";
    const countdownLocal = countdownTargetValid ? new Intl.DateTimeFormat("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false }).format(countdownTarget) : "--";
    const captured = data.captured_at ? new Date(data.captured_at) : null;
    const capturedLabel = captured && !Number.isNaN(captured.getTime()) ? new Intl.DateTimeFormat("zh-CN", { hour12: false, hour: "2-digit", minute: "2-digit", second: "2-digit" }).format(captured) : "--";
    const eventCountdown = nextEvent && Number.isFinite(Number(nextEvent.hours_until))
      ? Number(nextEvent.hours_until) <= 24
        ? `${Math.max(0, Number(nextEvent.hours_until)).toFixed(1)} 小时后`
        : `${Math.ceil(Number(nextEvent.hours_until) / 24)} 天后`
      : "暂无临近事件";
    this.patchStablePanel(target, `<header class="macro-market-heading" data-patch-key="macro-heading">
      <div><span>US MARKET REGIME</span><strong>宏观大盘环境</strong><small>${this.escape(data.source_note || "指数、波动率、市场宽度和事件风险")}</small></div>
      <div class="macro-session ${this.escape(sessionKey)} ${sessionActive ? "live" : ""}"><span><i></i>${this.escape(session.label || "休市")}</span><b>${sessionActive ? "实时波动" : session.realtime_expected ? "等待实时确认" : "行情静止"}</b><small>美东 ${this.escape(String(session.local_time || "").slice(11, 19) || "--")} · 更新 ${capturedLabel}</small></div>
      <button class="macro-regime ${this.escape(sentiment.key || "neutral")}" type="button" data-open-macro-impact aria-label="查看宏观因素判断"><span>环境结论</span><b>${this.escape(entryPolicy.label || sentiment.label || "数据不足")}</b><small>门槛 +${numberOrDash(entryPolicy.threshold_delta, 0)} · 多头仓位 ${numberOrDash(Number(entryPolicy.long_position_multiplier) * 100, 0)}% · 点击查看依据</small></button>
      <div class="macro-countdown ${this.escape(sessionKey)}" data-market-countdown data-target-at="${this.escape(session.countdown_target_at || "")}" data-target-label="${this.escape(session.countdown_label || "交易时段倒计时")}"><span data-market-countdown-state>${this.escape(session.countdown_label || "交易时段倒计时")}</span><b data-market-countdown-value>--:--:--</b><small>${countdownEt} ET · 本地 ${countdownLocal}</small></div>
    </header>
    <div class="macro-market-body" data-patch-key="macro-body">
      <div class="macro-index-grid">${indexCards || '<div class="macro-empty">大盘实时行情暂不可用，个股评分不会应用宏观调整。</div>'}</div>
      <aside class="macro-risk-stack">
        <div class="macro-vix ${vixTone}"><span>VIX 恐慌指数</span><b>${numberOrDash(vix.value, 2)}</b><small>${vix.available ? `${percent(vix.change_percent)} · 真实指数` : "暂不可用"}</small></div>
        <div class="macro-breadth ${breadth.available ? "available" : "unavailable"}"><span>市场涨跌家数</span><b>${this.number(breadth.advancers)} <i>/</i> ${this.number(breadth.decliners)}</b><small>上涨 / 下跌 · A/D ${breadthRatio}${breadth.available ? "" : " · 样本不足"}</small></div>
        <div class="macro-tide ${tideTone}"><span>Market Tide</span><b>${tide.available ? tideLabel : "暂不可用"}</b><small>${tide.available ? `净量 ${this.number(tide.net_volume)} · ${sessionActive ? "5m 实时潮汐" : "最近交易日潮汐"}` : providers.unusual_whales_enabled === false ? "Unusual Whales 已关闭" : providers.unusual_whales_configured ? "已配置，等待上游数据" : "未配置 Unusual Whales"}</small></div>
        <div class="macro-event ${eventTone}"><span>宏观事件风险</span><b>${nextEvent ? this.escape(nextEvent.event_type) : "正常"}</b><small>${nextEvent ? `${eventCountdown} · ${this.escape(nextEvent.title)}` : "未来 24 小时无已登记重大事件"}</small></div>
      </aside>
    </div>
    <footer class="macro-market-footer" data-patch-key="macro-footer"><div><span>板块热度</span>${sectorPills || "<small>暂无板块行情</small>"}</div><div><span>利率 / 美元代理</span>${assetPills || "<small>暂无宏观资产行情</small>"}</div></footer>`);
    this.tickMarketCountdown();
  }

  renderNews() {
    const search = this.state.newsSearch;
    const mode = this.state.newsMode;
    const items = this.state.news.filter((item) => {
      const related = (item.related_us_stocks || []).map((stock) => stock.symbol).join(" ");
      const industries = (item.related_industries || []).map((industry) => industry.name).join(" ");
      const haystack = `${item.title_zh || ""} ${item.title || ""} ${item.source || ""} ${industries} ${related}`.toLowerCase();
      if (search && !haystack.includes(search)) return false;
      if (mode === "analyzed" && !item.ai_analyzed_at) return false;
      if (mode === "pending" && item.ai_analyzed_at) return false;
      if (mode === "bull" && item.ai_sentiment !== "bull") return false;
      if (mode === "bear" && item.ai_sentiment !== "bear") return false;
      if (mode === "neutral" && item.ai_sentiment !== "neutral") return false;
      return true;
    });
    const target = this.q("#news-stream");
    const renderSignature = JSON.stringify([
      search,
      mode,
      [...this.state.analyzingNewsIds].sort(),
      items,
    ]);
    if (renderSignature === this.state.newsRenderSignature) return;
    const previousScrollTop = target.scrollTop;
    const hadPreviousRender = Boolean(this.state.newsRenderSignature);
    this.state.newsRenderSignature = renderSignature;
    if (!items.length) {
      target.innerHTML = '<div class="empty-state">没有符合当前筛选条件的新闻</div>';
      return;
    }
    target.innerHTML = items.map((item) => {
      const sentiment = item.ai_sentiment || item.sentiment || "pending";
      const analyzed = Boolean(item.ai_analyzed_at);
      const stocks = (item.related_us_stocks || []).slice(0, 6).map((stock) => `<span class="stock-chip ${this.escape(stock.direction || "neutral")}">${this.escape(stock.symbol)} <b>${Math.round(Number(stock.relevance || 0) * 100)}%</b></span>`).join("");
      const industries = (item.related_industries || []).slice(0, 5).map((industry) => `<span class="industry-chip ${this.escape(industry.direction || "neutral")}">${this.escape(industry.name)} <b>${Math.round(Number(industry.relevance || 0) * 100)}%</b></span>`).join("");
      const title = item.title_zh || item.title || "未命名新闻";
      const safeLink = this.safeUrl(item.link);
      const headline = safeLink ? `<a href="${this.escape(safeLink)}" target="_blank" rel="noopener noreferrer">${this.escape(title)}</a>` : `<strong>${this.escape(title)}</strong>`;
      const analyzing = this.state.analyzingNewsIds.has(item.id);
      return `<article class="news-item ${this.sentimentClass(sentiment)}">
        <time>${this.formatUnix(item.ts)}</time>
        <div class="news-source"><span>${this.escape(item.source || "未知来源")}</span>${analyzed ? `<em>AI 已分析</em>` : "<em class=\"pending\">等待批次</em>"}<small>${this.escape(item.lang || "--")}</small></div>
        <div class="news-body">${headline}
          <div class="news-analysis-line">${analyzed ? `<span class="sentiment-pill ${this.sentimentClass(sentiment)}">${this.sentimentLabel(sentiment)}</span><span>${this.impactLabel(item.ai_impact_strength)}</span><span>${this.horizonLabel(item.ai_time_horizon)}</span><span>${this.categoryLabel(item.ai_category)}</span>` : '<span class="sentiment-pill pending">每 15 分钟 · 最新 10 条</span>'}</div>
          <p>${this.escape(item.ai_reason || (analyzed ? "AI 未提供进一步说明" : "等待下一轮新闻分析；系统会优先处理数据库中最新的未分析新闻。"))}</p>
          <div class="news-relations"><div><em>行业</em><div class="industry-chips">${industries || `<span class="relation-empty">${analyzed ? "无直接关联行业" : "待分析"}</span>`}</div></div><div><em>美股</em><div class="stock-chips">${stocks || `<span class="relation-empty">${analyzed ? "无直接关联美股" : "待分析"}</span>`}</div></div></div>
        </div>
        <div class="news-score"><b>${item.ai_confidence == null ? "--" : `${Math.round(Number(item.ai_confidence) * 100)}%`}</b><small>${analyzed ? "AI 置信度" : "等待 AI"}</small><button class="news-analyze-action" type="button" data-analyze-news="${this.escape(item.id)}"${analyzing ? " disabled" : ""}>${analyzing ? "分析中…" : analyzed ? "重新分析" : "立即分析"}</button></div>
      </article>`;
    }).join("");
    if (hadPreviousRender) {
      target.scrollTop = Math.min(previousScrollTop, Math.max(0, target.scrollHeight - target.clientHeight));
    }
  }

  async analyzeNewsItem(newsId) {
    if (!newsId || this.state.analyzingNewsIds.has(newsId)) return;
    this.state.analyzingNewsIds.add(newsId);
    this.renderNews();
    try {
      const run = await this.api("/news/analyze", {
        method: "POST",
        body: JSON.stringify({ news_id: newsId }),
      });
      this.showBanner("该条新闻已提交 AI 分析，请稍候…", "success");
      const completed = await this.waitForRun(run.id);
      if (completed.status === "failed") throw new Error(completed.error_message || "该条新闻分析失败");
      const [news, overview] = await Promise.all([
        this.api("/news?limit=160"),
        this.api("/overview"),
      ]);
      this.state.news = news.items || [];
      this.state.overview = overview;
      this.state.config = overview.config;
      this.renderNews();
      this.renderOverview();
      await this.loadRuns();
      this.showBanner("该条新闻已完成 AI 分析。", "success");
    } catch (error) {
      this.showBanner(error.message || "该条新闻分析失败", "error");
    } finally {
      this.state.analyzingNewsIds.delete(newsId);
      this.renderNews();
    }
  }

  async waitForRun(runId, timeoutMilliseconds = 120000) {
    const deadline = Date.now() + timeoutMilliseconds;
    while (Date.now() < deadline) {
      await new Promise((resolve) => window.setTimeout(resolve, 1000));
      const data = await this.api("/runs?limit=100");
      this.state.runs = data.items || [];
      const run = this.state.runs.find((item) => item.id === runId);
      if (run && ["completed", "partial", "failed"].includes(run.status)) return run;
    }
    throw new Error("新闻分析等待超时，请到分析记录查看任务状态");
  }

  startNewsAutoScroll() {
    if (this.newsScrollAnimationFrame != null) return;
    const step = (timestamp) => {
      this.newsScrollAnimationFrame = null;
      if (!this.state.running) return;
      if (timestamp - this.newsScrollLastTick >= 80) {
        this.newsScrollLastTick = timestamp;
        this.autoScrollNews();
      }
      this.newsScrollAnimationFrame = window.requestAnimationFrame(step);
    };
    this.newsScrollAnimationFrame = window.requestAnimationFrame(step);
  }

  autoScrollNews() {
    if (
      !this.state.running
      || document.visibilityState !== "visible"
      || this.state.view !== "news"
      || this.scrollPaused
      || window.matchMedia?.("(prefers-reduced-motion: reduce)")?.matches
    ) return;
    const target = this.q("#news-stream");
    if (!target || target.scrollHeight <= target.clientHeight + 2) return;
    target.scrollTop += 1;
    if (target.scrollTop + target.clientHeight >= target.scrollHeight - 2) target.scrollTop = 0;
  }

  async loadRuns() {
    try {
      const data = await this.api("/runs?limit=100");
      this.state.runs = data.items || [];
      this.renderRuns();
    } catch (error) {
      this.showBanner(error.message || "分析记录读取失败", "error");
    }
  }

  renderRuns() {
    const target = this.q("#run-list");
    if (!this.state.runs.length) { target.innerHTML = '<div class="empty-state">暂无分析记录；可使用页面右上角手动执行一次。</div>'; return; }
    target.innerHTML = this.state.runs.map((run) => {
      const summary = run.summary || {};
      const text = run.run_type === "news"
        ? summary.market_summary || "新闻语义分析正在等待结果"
        : `候选 ${summary.candidate_count ?? run.input_count} 个，新增机会 ${summary.discovered_count ?? run.matched_count} 个，重复输入 ${summary.duplicate_count ?? 0} 个。`;
      return `<article class="run-item ${this.escape(run.status)}">
        <div class="run-marker"><i></i><span>${this.runTypeLabel(run.run_type)}</span></div>
        <div class="run-main"><header><strong>${this.runTypeLabel(run.run_type)}</strong><em class="run-status">${this.statusLabel(run.status)}</em><time>${this.formatDate(run.created_at)}</time></header><p>${this.escape(run.error_message || text)}</p><footer><span>输入 ${this.number(run.input_count)}</span><span>完成 / 发现 ${this.number(run.matched_count)}</span>${summary.model_name ? `<span>模型 ${this.escape(summary.model_name)}</span>` : ""}${summary.timeframe ? `<span>周期 ${this.escape(summary.timeframe)}</span>` : ""}</footer></div>
      </article>`;
    }).join("");
  }

  renderConfig() {
    const config = this.state.config;
    if (!config) return;
    this.q("#config-enabled").checked = Boolean(config.enabled);
    this.renderEnabledLabel(Boolean(config.enabled));
    this.q("#config-news-interval").value = config.news_interval_minutes;
    this.q("#config-opportunity-interval").value = config.opportunity_interval_minutes;
    this.q("#config-lookback").value = config.news_lookback_hours;
    this.q("#config-timeframe").value = config.timeframe;
    this.q("#config-max-holding-bars").value = Number(config.prediction_max_holding_bars ?? 4);
    this.q("#config-confidence").value = Math.round(Number(config.minimum_news_confidence) * 100);
    this.q("#config-mentions").value = config.minimum_news_mentions;
    this.q("#config-indicator-score").value = Number(config.minimum_indicator_score ?? 65);
    this.q("#config-combined-score").value = Number(config.minimum_combined_score ?? 75);
    this.q("#config-market-age").value = Number(config.maximum_market_age_seconds ?? 120);
    this.q("#config-market-flow-quality").value = Math.round(Number(config.minimum_market_flow_quality ?? 0.5) * 100);
    this.q("#config-feature-quality").value = Math.round(Number(config.minimum_feature_quality ?? 0.7) * 100);
    this.q("#config-calibration-samples").value = Number(config.minimum_calibration_samples ?? 1000);
    this.q("#config-safety-margin").value = Number(config.live_safety_margin_bps ?? 10);
    this.renderScorePolicy(config);
    this.renderPredictionCostConfig(config);
    this.state.draftSymbols = new Set(config.monitor_symbols || []);
    this.q("#config-all-symbols").checked = this.state.draftSymbols.size === 0;
    this.renderSymbolPicker();
    const selected = new Set(config.indicator_keys || []);
    const groups = new Map();
    this.state.indicators.forEach((item) => {
      const key = `${item.source}:${item.category}`;
      if (!groups.has(key)) groups.set(key, { label: `${item.source === "prediction" ? "预测因子" : "K线策略"} · ${item.category}`, items: [] });
      groups.get(key).items.push(item);
    });
    this.q("#indicator-picker").innerHTML = [...groups.values()].map((group) => `<section><h3>${this.escape(group.label)}</h3>${group.items.map((item) => `<label class="indicator-option"><input type="checkbox" value="${this.escape(item.key)}"${selected.has(item.key) ? " checked" : ""}><span><strong>${this.escape(item.name)}</strong><small>${this.escape(item.description)}</small></span><i></i></label>`).join("")}</section>`).join("");
    this.q("#indicator-templates").innerHTML = `<span>推荐组合</span>${this.state.indicatorTemplates.map((template) => `<button type="button" data-template="${this.escape(template.key)}" title="${this.escape(template.description)}">${this.escape(template.name)}</button>`).join("")}`;
    this.updateIndicatorCount();
    this.q("#config-saved-at").textContent = config.persisted ? `上次保存 ${this.formatDate(config.updated_at)}` : "当前为安全默认值，保存后才启用周期任务";
  }

  renderPredictionCostConfig(config = this.state.config || {}) {
    this.q("#prediction-fee-enabled").checked = Boolean(config.prediction_fee_enabled ?? true);
    this.q("#prediction-fee-bps").value = Number(config.prediction_fee_bps_per_side ?? 5);
    this.q("#prediction-slippage-enabled").checked = Boolean(config.prediction_slippage_enabled ?? true);
    this.q("#prediction-slippage-bps").value = Number(config.prediction_slippage_bps_per_side ?? 3);
    this.q("#prediction-funding-enabled").checked = Boolean(config.prediction_funding_enabled ?? true);
    this.q("#prediction-funding-bps").value = Number(config.prediction_funding_bps_per_8h ?? 1);
    this.updatePredictionCostControls();
  }

  updatePredictionCostControls() {
    const parts = [
      ["fee", 2],
      ["slippage", 2],
      ["funding", 1 / 8],
    ];
    let oneHourCost = 0;
    parts.forEach(([key, multiplier]) => {
      const enabled = this.q(`#prediction-${key}-enabled`).checked;
      const input = this.q(`#prediction-${key}-bps`);
      input.disabled = !enabled;
      input.closest("label")?.classList.toggle("disabled", !enabled);
      if (enabled) oneHourCost += Math.max(0, Number(input.value) || 0) * multiplier;
    });
    this.q("#prediction-cost-total").textContent = `1h 往返 ${oneHourCost.toFixed(2)} bps`;
  }

  async savePredictionCostConfig(event) {
    event.preventDefault();
    const button = this.q("#prediction-cost-apply");
    button.disabled = true;
    button.textContent = "保存中…";
    try {
      const payload = {
        prediction_fee_enabled: this.q("#prediction-fee-enabled").checked,
        prediction_fee_bps_per_side: Math.max(0, Number(this.q("#prediction-fee-bps").value) || 0),
        prediction_slippage_enabled: this.q("#prediction-slippage-enabled").checked,
        prediction_slippage_bps_per_side: Math.max(0, Number(this.q("#prediction-slippage-bps").value) || 0),
        prediction_funding_enabled: this.q("#prediction-funding-enabled").checked,
        prediction_funding_bps_per_8h: Math.max(0, Number(this.q("#prediction-funding-bps").value) || 0),
      };
      this.state.config = await this.api("/cost-config", { method: "PUT", body: JSON.stringify(payload) });
      this.renderPredictionCostConfig(this.state.config);
      await Promise.all([
        this.loadPredictionAnalytics({ force: true, interactive: true }),
        this.loadPredictionReadiness({ force: true }),
      ]);
      this.showBanner("成本配置已保存；命中率、净收益和实盘准备度已按当前设置重算。", "success");
    } catch (error) {
      this.showBanner(error.message || "成本配置保存失败", "error");
    } finally {
      button.disabled = false;
      button.textContent = "保存并重算";
    }
  }

  renderEnabledLabel(enabled) {
    this.q("#config-enabled-label").textContent = enabled ? "已开启" : "已暂停";
  }

  renderScorePolicy(config = this.state.config || {}) {
    const policy = this.state.scorePolicy;
    if (!this.state.weightDraftDirty) {
      const signalPolicyWeights = this.normalizedSignalPolicyWeights(policy || config);
      Object.entries(signalPolicyWeights).forEach(([domain, value]) => {
        const input = this.q(`[data-weight-domain="${domain}"]`);
        if (input) input.value = Number(value.toFixed(1));
      });
      this.q("#config-market-flow-weight").value = this.legacyMarketFlowWeight(signalPolicyWeights);
      this.state.savedSignalPolicyWeights = policy
        ? this.signalPolicyWeightSignature(signalPolicyWeights)
        : config.persisted
        ? this.signalPolicyWeightSignature(signalPolicyWeights)
        : null;
    }
    const canEdit = policy?.can_edit === true || policy?.editable === true;
    const mode = String(policy?.mode || "record");
    this.qa(".score-weight-input").forEach((input) => { input.disabled = !canEdit; });
    const saveButton = this.q("#save-score-policy");
    saveButton.disabled = !canEdit;
    saveButton.textContent = canEdit ? "保存六域权重" : "仅管理员可改";
    this.q("#weight-permission-state").textContent = canEdit ? "管理员可编辑" : "仅管理员可改";
    const modeState = this.q("#weight-policy-mode");
    modeState.textContent = ["score", "gate"].includes(mode) ? "参与组合评分" : "当前仅记录";
    modeState.classList.toggle("inactive", !["score", "gate"].includes(mode));
    this.updateScoreWeightPreview();
  }

  signalPolicyWeightDefaults() {
    return {
      news: 20,
      technical: 30,
      options_flow: 20,
      market_context: 10,
      gex: 10,
      institutional_flow: 10,
    };
  }

  normalizedSignalPolicyWeights(config = {}) {
    const defaults = this.signalPolicyWeightDefaults();
    const source = config.weights
      || config.signal_policy_weights
      || config.unusual_whales_signal_policy?.weights
      || config.unusual_whales_weights;
    if (source && typeof source === "object") {
      const raw = Object.fromEntries(Object.keys(defaults).map((key) => [key, Number(source[key])]));
      const values = Object.values(raw);
      const hasEveryDomain = values.every((value) => Number.isFinite(value) && value >= 0);
      if (hasEveryDomain) {
        const decimalPayload = values.every((value) => value <= 1) && values.reduce((sum, value) => sum + value, 0) <= 1.001;
        return Object.fromEntries(Object.entries(raw).map(([key, value]) => [key, value * (decimalPayload ? 100 : 1)]));
      }
    }
    const legacyNews = Number(config.news_score_weight);
    const legacyTechnical = Number(config.technical_score_weight);
    const legacyMarket = Number(config.market_flow_score_weight);
    if ([legacyNews, legacyTechnical, legacyMarket].every((value) => Number.isFinite(value) && value >= 0)) {
      return {
        news: legacyNews,
        technical: legacyTechnical,
        options_flow: legacyMarket * 0.4,
        market_context: legacyMarket * 0.2,
        gex: legacyMarket * 0.2,
        institutional_flow: legacyMarket * 0.2,
      };
    }
    return defaults;
  }

  scoreWeightValues() {
    return Object.fromEntries(this.qa(".score-weight-input[data-weight-domain]").map((input) => [
      input.dataset.weightDomain,
      Math.max(0, Number(input.value) || 0),
    ]));
  }

  legacyMarketFlowWeight(values) {
    return Number(values.options_flow || 0)
      + Number(values.market_context || 0)
      + Number(values.gex || 0)
      + Number(values.institutional_flow || 0);
  }

  signalPolicyWeightSignature(values) {
    return Object.keys(this.signalPolicyWeightDefaults())
      .map((key) => Number(values[key] || 0).toFixed(3))
      .join("|");
  }

  updateScoreWeightPreview() {
    const values = this.scoreWeightValues();
    const total = Object.values(values).reduce((sum, value) => sum + value, 0);
    const valid = Math.abs(total - 100) <= 0.01;
    const state = this.q("#weight-total-state");
    const saved = valid && this.state.savedSignalPolicyWeights === this.signalPolicyWeightSignature(values);
    const status = !valid ? "需调整为 100%" : saved ? "已保存" : "待保存";
    state.textContent = `合计 ${total.toFixed(1).replace(".0", "")}% · ${status}`;
    state.classList.toggle("invalid", !valid);
    state.classList.toggle("dirty", valid && !saved);
    const scale = total > 0 ? 100 / total : 0;
    Object.entries(values).forEach(([domain, value]) => {
      const segment = this.q(`.weight-preview .${domain.replaceAll("_", "-")}`);
      if (segment) segment.style.width = `${value * scale}%`;
    });
    this.q("#config-market-flow-weight").value = this.legacyMarketFlowWeight(values);
    return valid;
  }

  async saveScorePolicy({ quietSuccess = false } = {}) {
    if (!this.updateScoreWeightPreview()) {
      this.showBanner("六域组合评分权重合计必须为 100%。", "error");
      this.q("#weight-config")?.scrollIntoView({ behavior: "smooth", block: "center" });
      return false;
    }
    const canEdit = this.state.scorePolicy?.can_edit === true || this.state.scorePolicy?.editable === true;
    if (!canEdit) {
      this.showBanner("六域评分策略为平台级配置，仅管理员可以修改。", "error");
      return false;
    }
    const button = this.q("#save-score-policy");
    button.disabled = true;
    button.textContent = "保存中…";
    try {
      const scoreWeights = this.scoreWeightValues();
      this.state.scorePolicy = await this.api("/score-policy", {
        method: "PUT",
        body: JSON.stringify({
          weights: Object.fromEntries(Object.entries(scoreWeights).map(([key, value]) => [key, value / 100])),
        }),
      });
      this.state.weightDraftDirty = false;
      this.renderScorePolicy();
      if (!quietSuccess) this.showBanner("六域权重已发布；从下一轮新信号开始参与组合评分，历史信号保持冻结。", "success");
      return true;
    } catch (error) {
      this.showBanner(error.message || "六域权重保存失败", "error");
      return false;
    } finally {
      const editable = this.state.scorePolicy?.can_edit === true || this.state.scorePolicy?.editable === true;
      button.disabled = !editable;
      button.textContent = editable ? "保存六域权重" : "仅管理员可改";
    }
  }

  updateIndicatorCount() {
    const selected = this.qa('#indicator-picker input[type="checkbox"]:checked').map((input) => input.value);
    const count = selected.length;
    this.q("#indicator-count").textContent = `已选 ${count} 项 · 分组确认`;
    const conflicts = this.indicatorSelectionConflicts(selected);
    const warning = this.q("#indicator-conflict-warning");
    warning.classList.toggle("hidden", conflicts.length === 0);
    warning.textContent = conflicts.length ? `当前组合存在互斥条件：${conflicts.map(([left, right]) => `${this.indicatorName(left)} / ${this.indicatorName(right)}`).join("；")}` : "";
  }

  indicatorName(key) {
    return this.state.indicators.find((item) => item.key === key)?.name || key;
  }

  indicatorSelectionConflicts(selected) {
    const keys = new Set(selected);
    return this.state.indicatorConflictPairs.filter(([left, right]) => keys.has(left) && keys.has(right));
  }

  applyIndicatorTemplate(event) {
    const button = event.target.closest("button[data-template]");
    if (!button) return;
    const template = this.state.indicatorTemplates.find((item) => item.key === button.dataset.template);
    if (!template) return;
    const selected = new Set(template.indicator_keys || []);
    this.qa('#indicator-picker input[type="checkbox"]').forEach((input) => { input.checked = selected.has(input.value); });
    this.updateIndicatorCount();
    this.showBanner(`已应用“${template.name}”组合；系统将按策略组门槛和加权评分确认。`, "success");
  }

  filteredSymbols() {
    const search = this.state.symbolSearch;
    return this.state.symbols.filter((item) => !search || `${item.symbol} ${item.contract_symbol}`.includes(search));
  }

  renderSymbolPicker() {
    const allSymbols = this.q("#config-all-symbols").checked;
    const items = this.filteredSymbols();
    const target = this.q("#symbol-picker");
    target.classList.toggle("all-selected", allSymbols);
    if (!items.length) {
      target.innerHTML = '<div class="empty-state">没有符合搜索条件的品种</div>';
    } else {
      target.innerHTML = items.map((item) => `<label class="symbol-option"><input type="checkbox" value="${this.escape(item.contract_symbol)}"${this.state.draftSymbols.has(item.contract_symbol) ? " checked" : ""}${allSymbols ? " disabled" : ""}><span><strong>${this.escape(item.symbol)}</strong><small>${this.escape(item.contract_symbol)}</small></span></label>`).join("");
    }
    const count = this.state.draftSymbols.size;
    this.q("#symbol-count").textContent = allSymbols ? `全部 ${this.state.symbols.length} 项` : `已选 ${count} / ${this.state.symbols.length} 项`;
  }

  updateDraftSymbol(event) {
    if (!event.target.matches('input[type="checkbox"]')) return;
    if (event.target.checked) this.state.draftSymbols.add(event.target.value);
    else this.state.draftSymbols.delete(event.target.value);
    this.renderSymbolPicker();
  }

  selectVisibleSymbols() {
    this.q("#config-all-symbols").checked = false;
    this.filteredSymbols().forEach((item) => this.state.draftSymbols.add(item.contract_symbol));
    this.renderSymbolPicker();
  }

  async saveConfig(event) {
    event.preventDefault();
    if (!this.updateScoreWeightPreview()) { this.showBanner("六域组合评分权重合计必须为 100%。", "error"); this.q("#weight-config")?.scrollIntoView({ behavior: "smooth", block: "center" }); return; }
    if (this.state.weightDraftDirty && !(await this.saveScorePolicy({ quietSuccess: true }))) return;
    const scoreWeights = this.scoreWeightValues();
    const indicatorKeys = this.qa('#indicator-picker input[type="checkbox"]:checked').map((input) => input.value);
    if (!indicatorKeys.length) { this.showBanner("请至少选择一个技术指标。", "error"); return; }
    if (this.indicatorSelectionConflicts(indicatorKeys).length) { this.showBanner("当前指标组合存在无法同时满足的互斥条件，请改用推荐组合或取消冲突指标。", "error"); return; }
    const allSymbols = this.q("#config-all-symbols").checked;
    const monitorSymbols = allSymbols ? [] : [...this.state.draftSymbols];
    if (!allSymbols && !monitorSymbols.length) { this.showBanner("请至少选择一个监控品种，或开启扫描全部品种。", "error"); return; }
    const button = this.q(".save-config");
    button.disabled = true;
    button.textContent = "保存中…";
    try {
      const payload = {
        enabled: this.q("#config-enabled").checked,
        news_interval_minutes: Number(this.q("#config-news-interval").value),
        opportunity_interval_minutes: Number(this.q("#config-opportunity-interval").value),
        news_lookback_hours: Number(this.q("#config-lookback").value),
        timeframe: this.q("#config-timeframe").value,
        prediction_max_holding_bars: Number(this.q("#config-max-holding-bars").value),
        indicator_keys: indicatorKeys,
        monitor_symbols: monitorSymbols,
        minimum_news_confidence: Number(this.q("#config-confidence").value) / 100,
        minimum_news_mentions: Number(this.q("#config-mentions").value),
        minimum_indicator_score: Number(this.q("#config-indicator-score").value),
        minimum_combined_score: Number(this.q("#config-combined-score").value),
        maximum_market_age_seconds: Number(this.q("#config-market-age").value),
        minimum_market_flow_quality: Number(this.q("#config-market-flow-quality").value) / 100,
        minimum_feature_quality: Number(this.q("#config-feature-quality").value) / 100,
        minimum_calibration_samples: Number(this.q("#config-calibration-samples").value),
        live_safety_margin_bps: Number(this.q("#config-safety-margin").value),
        news_score_weight: scoreWeights.news,
        technical_score_weight: scoreWeights.technical,
        market_flow_score_weight: this.legacyMarketFlowWeight(scoreWeights),
      };
      this.state.config = await this.api("/config", { method: "PUT", body: JSON.stringify(payload) });
      this.renderConfig();
      await this.loadOverviewOnly();
      const scope = monitorSymbols.length ? `${monitorSymbols.length} 个监控品种` : "全部可用品种";
      this.showBanner(`配置已保存；${scope}，${indicatorKeys.length} 个指标采用策略组确认规则。`, "success");
    } catch (error) {
      this.showBanner(error.message || "配置保存失败", "error");
    } finally {
      button.disabled = false;
      button.textContent = "保存配置";
    }
  }

  async loadOverviewOnly() {
    this.state.overview = await this.api("/overview");
    this.state.config = this.state.overview.config;
    this.renderOverview();
  }

  async createRun(runType) {
    if (this.state.busyRun) return;
    this.state.busyRun = runType;
    const button = this.q(runType === "news" ? "#run-news" : "#run-opportunity");
    const original = button.textContent;
    button.disabled = true;
    button.textContent = runType === "news" ? "正在提交…" : "正在扫描…";
    try {
      await this.api("/runs", { method: "POST", body: JSON.stringify({ run_type: runType }) });
      this.showBanner(runType === "news" ? "新闻 AI 分析任务已启动，可在“分析记录”查看进度。" : "机会扫描任务已启动，可在“发现机会”查看结果。", "success");
      await this.loadOverviewOnly();
      await this.loadRuns();
      if (runType === "opportunity") await this.loadOpportunities();
    } catch (error) {
      this.showBanner(error.message || "任务启动失败", "error");
    } finally {
      this.state.busyRun = "";
      button.disabled = false;
      button.textContent = original;
    }
  }

  async _loadOpportunitiesLegacy({ showLoading = false } = {}) {
    const tab = this.state.opportunityTab;
    if (this.state.opportunitiesLoading && this.state.opportunityLoadingTab === tab) return;
    const requestId = ++this.state.opportunityRequestId;
    const target = this.q("#opportunity-list");
    const switchingTab = this.state.opportunitiesLoadedTab && this.state.opportunitiesLoadedTab !== tab;
    const firstLoad = !this.state.opportunitiesLoadedTab;
    this.state.opportunitiesLoading = true;
    this.state.opportunityLoadingTab = tab;
    target.classList.add("is-refreshing");
    target.setAttribute("aria-busy", "true");
    if (showLoading || switchingTab || firstLoad) {
      target.innerHTML = `<div class="empty-state opportunity-empty"><strong>正在读取${tab === "history" ? "历史" : "当前"}机会…</strong></div>`;
    }
    try {
      const data = await this.api("/opportunities?limit=300&include_expired=true");
      if (requestId !== this.state.opportunityRequestId || tab !== this.state.opportunityTab) return;
      const now = Date.now();
      const bySignalTimeDesc = (left, right) => {
        const leftTime = this.parseDate(left.discovered_at).getTime();
        const rightTime = this.parseDate(right.discovered_at).getTime();
        const timeDifference = (Number.isFinite(rightTime) ? rightTime : 0) - (Number.isFinite(leftTime) ? leftTime : 0);
        return timeDifference || String(right.id || "").localeCompare(String(left.id || ""));
      };
      const items = [...(data.items || [])].sort(bySignalTimeDesc);
      const isActive = (item) => {
        const status = String(item.lifecycle_state || item.state || item.status || "").toLowerCase();
        const activeStatus = ["candidate", "discovered", "ready", "triggered", "holding", "entered", "active"].includes(status);
        return activeStatus && this.parseDate(item.expires_at).getTime() > now;
      };
      const isAwaitingSettlement = (item) => ["pending", "unavailable"].includes(String(item.prediction_status || ""));
      const currentItems = items.filter((item) => isActive(item) && item.prediction_status !== "completed").sort(bySignalTimeDesc);
      const historyItems = items.filter(isAwaitingSettlement).sort(bySignalTimeDesc);
      const visible = (tab === "history" ? historyItems : currentItems)
        .map((item) => ({ ...item, outcome: null }));
      if (tab === "current") {
        const unique = new Map();
        visible.forEach((item) => {
          const instrument = String(item.contract_symbol || item.symbol || "").trim().toUpperCase();
          if (!unique.has(instrument)) unique.set(instrument, item);
        });
        this.state.opportunities = [...unique.values()];
      } else {
        this.state.opportunities = visible;
      }
      const countDirections = (records) => records.reduce((counts, item) => {
        if (item.direction === "short") counts.short += 1;
        else counts.long += 1;
        return counts;
      }, { long: 0, short: 0 });
      const uniqueCurrent = new Map();
      currentItems.forEach((item) => {
        const instrument = String(item.contract_symbol || item.symbol || "").trim().toUpperCase();
        if (!uniqueCurrent.has(instrument)) uniqueCurrent.set(instrument, item);
      });
      const uniqueCurrentItems = [...uniqueCurrent.values()];
      this.state.opportunityDirectionCounts = countDirections(uniqueCurrentItems);
      this.state.historyOpportunityDirectionCounts = countDirections(historyItems);
      this.state.historyOpportunitySettlementCounts = historyItems.reduce((counts, item) => {
        counts.total += 1;
        if (item.prediction_status === "pending") counts.pending += 1;
        if (item.prediction_status === "unavailable") counts.unavailable += 1;
        return counts;
      }, { total: 0, pending: 0, unavailable: 0 });
      this.state.opportunityStatusCounts = uniqueCurrentItems.reduce((counts, item) => {
        const status = this.virtualEntryState(item, this.virtualEntryGate(item)).tone;
        counts.all += 1;
        if (Object.prototype.hasOwnProperty.call(counts, status)) counts[status] += 1;
        return counts;
      }, { all: 0, candidate: 0, ready: 0, triggered: 0, blocked: 0, data_error: 0 });
      this.renderOpportunityDirectionCounts();
      this.renderOpportunityStatusCounts();
      this.renderOpportunities();
      this.renderSignalHealth();
      this.state.opportunitiesLoadedTab = tab;
    } catch (error) {
      if (requestId !== this.state.opportunityRequestId) return;
      this.showBanner(error.message || "发现机会读取失败", "error");
      if (!this.state.opportunitiesLoadedTab) {
        target.innerHTML = '<div class="empty-state opportunity-empty"><strong>机会读取失败</strong><span>保留页面后重试，不会触发任何交易。</span></div>';
      }
    } finally {
      if (requestId === this.state.opportunityRequestId) {
        this.state.opportunitiesLoading = false;
        this.state.opportunityLoadingTab = "";
        target.classList.remove("is-refreshing");
        target.removeAttribute("aria-busy");
      }
    }
  }

  async loadOpportunities({ showLoading = false } = {}) {
    const tab = this.state.opportunityTab;
    this.opportunitiesAbortController?.abort();
    const controller = new AbortController();
    this.opportunitiesAbortController = controller;
    const requestId = ++this.state.opportunityRequestId;
    const target = this.q("#opportunity-list");
    this.state.opportunitiesLoading = true;
    this.state.opportunityLoadingTab = tab;
    target.classList.add("is-refreshing");
    target.setAttribute("aria-busy", "true");
    if (showLoading && !(this.state.opportunityCache[tab] || []).length) {
      target.innerHTML = '<div class="empty-state opportunity-empty"><strong>正在读取机会…</strong></div>';
    }
    try {
      const params = new URLSearchParams({
        scope: tab,
        limit: String(this.state.opportunityPageSize),
        page: String(this.state.opportunityPages[tab] || 1),
      });
      const data = await this.api(`/opportunities?${params}`, { signal: controller.signal });
      if (requestId !== this.state.opportunityRequestId || tab !== this.state.opportunityTab) return;
      const sorted = [...(data.items || [])].sort((left, right) => {
        const leftTime = this.parseDate(left.discovered_at).getTime();
        const rightTime = this.parseDate(right.discovered_at).getTime();
        const delta = (Number.isFinite(rightTime) ? rightTime : 0) - (Number.isFinite(leftTime) ? leftTime : 0);
        return delta || String(right.id || "").localeCompare(String(left.id || ""));
      }).map((item) => ({ ...item, outcome: null }));
      this.state.opportunities = sorted;
      this.state.opportunityCache[tab] = sorted;
      this.state.opportunityPagination = data.pagination || {
        page: 1,
        page_size: this.state.opportunityPageSize,
        total: sorted.length,
        total_pages: 1,
      };
      this.state.opportunityPaginationByTab[tab] = this.state.opportunityPagination;
      this.state.opportunityPages[tab] = Number(this.state.opportunityPagination.page || 1);
      const directions = data.direction_counts || { long: 0, short: 0 };
      if (tab === "current") this.state.opportunityDirectionCounts = directions;
      else this.state.historyOpportunityDirectionCounts = directions;
      if (tab === "history") {
        this.state.historyOpportunitySettlementCounts = data.settlement_counts || { total: 0, pending: 0, unavailable: 0 };
      }
      this.state.opportunityStatusCounts = sorted.reduce((counts, item) => {
        const status = this.virtualEntryState(item, this.virtualEntryGate(item)).tone;
        counts.all += 1;
        if (Object.prototype.hasOwnProperty.call(counts, status)) counts[status] += 1;
        return counts;
      }, { all: 0, candidate: 0, ready: 0, triggered: 0, blocked: 0, data_error: 0 });
      this.renderOpportunityDirectionCounts();
      this.renderOpportunityStatusCounts();
      this.renderOpportunities();
      this.renderSignalHealth();
      this.state.opportunitiesLoadedTab = tab;
    } catch (error) {
      if (error?.name === "AbortError") return;
      if (requestId !== this.state.opportunityRequestId) return;
      this.showBanner(error.message || "发现机会读取失败", "error");
      if (!(this.state.opportunityCache[tab] || []).length) {
        target.innerHTML = '<div class="empty-state opportunity-empty"><strong>机会读取失败</strong><span>页面会保留并自动重试。</span></div>';
      }
    } finally {
      if (requestId === this.state.opportunityRequestId) {
        this.state.opportunitiesLoading = false;
        this.state.opportunityLoadingTab = "";
        if (this.opportunitiesAbortController === controller) this.opportunitiesAbortController = null;
        target.classList.remove("is-refreshing");
        target.removeAttribute("aria-busy");
      }
    }
  }

  renderOpportunityDirectionCounts() {
    const groups = [
      ["#current-direction-counts", this.state.opportunityDirectionCounts, "当前机会"],
      ["#history-direction-counts", this.state.historyOpportunityDirectionCounts, "历史机会"],
    ];
    groups.forEach(([selector, counts, label]) => {
      const target = this.q(selector);
      if (!target) return;
      const settlementPrefix = selector === "#history-direction-counts"
        ? `<b class="pending">未完成 ${this.number(this.state.historyOpportunitySettlementCounts.total)}</b><i>·</i>`
        : "";
      target.innerHTML = `${settlementPrefix}<b class="long">多 ${this.number(counts.long)}</b><i>/</i><b class="short">空 ${this.number(counts.short)}</b>`;
      target.setAttribute("aria-label", `${label}：做多 ${counts.long} 次，做空 ${counts.short} 次`);
    });
  }

  setOpportunityTab(tab) {
    if (!["current", "history"].includes(tab) || tab === this.state.opportunityTab) return;
    this.state.opportunityTab = tab;
    this.qa("[data-opportunity-tab]").forEach((button) => {
      const active = button.dataset.opportunityTab === tab;
      button.classList.toggle("active", active);
      button.setAttribute("aria-selected", String(active));
    });
    this.q("#opportunity-status-tabs").classList.toggle("hidden", tab === "history");
    this.state.opportunities = this.state.opportunityCache[tab] || [];
    this.state.opportunityPagination = this.state.opportunityPaginationByTab[tab] || {
      page: this.state.opportunityPages[tab] || 1,
      page_size: this.state.opportunityPageSize,
      total: 0,
      total_pages: 1,
    };
    this.renderOpportunities();
    this.loadOpportunities({ showLoading: !this.state.opportunities.length });
  }

  setOpportunityPage(page) {
    const tab = this.state.opportunityTab;
    const totalPages = Number(this.state.opportunityPagination?.total_pages || 1);
    const nextPage = Math.min(totalPages, Math.max(1, Number(page) || 1));
    if (nextPage === Number(this.state.opportunityPages[tab] || 1)) return;
    this.state.opportunityPages[tab] = nextPage;
    this.loadOpportunities();
  }

  setOpportunityStatusFilter(status) {
    if (this.state.opportunityTab !== "current") return;
    const allowed = ["all", "candidate", "ready", "triggered", "blocked", "data_error"];
    if (!allowed.includes(status) || status === this.state.opportunityStatusFilter) return;
    this.state.opportunityStatusFilter = status;
    this.qa("[data-opportunity-status]").forEach((button) => {
      const active = button.dataset.opportunityStatus === status;
      button.classList.toggle("active", active);
      button.setAttribute("aria-selected", String(active));
    });
    this.renderOpportunities();
  }

  renderOpportunityStatusCounts() {
    const counts = this.state.opportunityStatusCounts;
    ["all", "candidate", "ready", "triggered", "blocked", "data_error"].forEach((status) => {
      const target = this.q(`#opportunity-status-${status}-count`);
      if (target) target.textContent = this.number(counts[status] || 0);
    });
  }

  opportunityScoreHistory(item) {
    const evidence = item?.evidence || {};
    const stableScores = item?.score_components && typeof item.score_components === "object" ? item.score_components : {};
    const stableFlow = item?.flow && typeof item.flow === "object" ? item.flow : {};
    const currentFlow = item?.current_market_flow && typeof item.current_market_flow === "object" ? item.current_market_flow : {};
    const raw = Array.isArray(evidence.score_history) ? evidence.score_history : [];
    const history = [];
    const numeric = (...values) => {
      const value = this.firstValue(...values);
      const parsed = Number(value);
      return Number.isFinite(parsed) ? parsed : null;
    };
    const flowPoint = (point = {}, fallback = {}) => {
      const snapshot = point?.market_flow_snapshot && typeof point.market_flow_snapshot === "object" ? point.market_flow_snapshot : {};
      return {
        market_flow: numeric(point?.market_flow, point?.components?.market_flow, snapshot.score, fallback.score),
        main_force_ratio: numeric(snapshot.main_force_ratio, point?.main_force_ratio, fallback.main_force_ratio),
        active_buy_ratio: numeric(snapshot.active_buy_ratio, point?.active_buy_ratio, fallback.active_buy_ratio),
        book_imbalance: numeric(snapshot.book_imbalance, point?.book_imbalance, fallback.book_imbalance),
        book_imbalance_5: numeric(snapshot.book_imbalance_5, point?.book_imbalance_5, fallback.book_imbalance_5),
        bid_depth_notional: numeric(snapshot.bid_depth_notional, point?.bid_depth_notional, fallback.bid_depth_notional),
        ask_depth_notional: numeric(snapshot.ask_depth_notional, point?.ask_depth_notional, fallback.ask_depth_notional),
        bid_depth_notional_5: numeric(snapshot.bid_depth_notional_5, point?.bid_depth_notional_5, fallback.bid_depth_notional_5),
        ask_depth_notional_5: numeric(snapshot.ask_depth_notional_5, point?.ask_depth_notional_5, fallback.ask_depth_notional_5),
        bid_depth_change_30s_pct: numeric(snapshot.bid_depth_change_30s_pct, point?.bid_depth_change_30s_pct, fallback.bid_depth_change_30s_pct),
        ask_depth_change_30s_pct: numeric(snapshot.ask_depth_change_30s_pct, point?.ask_depth_change_30s_pct, fallback.ask_depth_change_30s_pct),
        data_quality: numeric(snapshot.data_quality, point?.data_quality, fallback.data_quality),
      };
    };
    const entryPoint = {
      calculated_at: item?.prediction_created_at,
      news: numeric(item?.prediction_news_score),
      technical: numeric(item?.prediction_indicator_score),
      market_flow: numeric(item?.prediction_market_flow_score),
      option_flow: numeric(item?.prediction_option_flow_score, item?.prediction_feature_scores?.option_flow),
      gex: numeric(item?.prediction_gex_score, item?.prediction_feature_scores?.gex),
      institutional: numeric(item?.prediction_institutional_score, item?.prediction_feature_scores?.institutional),
      macro: numeric(item?.prediction_macro_score, item?.prediction_feature_scores?.macro),
      combined: numeric(item?.prediction_combined_score),
    };
    if (entryPoint.calculated_at && Number.isFinite(entryPoint.combined)) {
      history.push(entryPoint);
    }
    history.push(...raw.map((point) => ({
      calculated_at: point?.calculated_at,
      news: numeric(point?.news, point?.components?.news),
      technical: numeric(point?.technical, point?.components?.technical),
      ...flowPoint(point),
      option_flow: numeric(point?.option_flow, point?.components?.option_flow),
      gex: numeric(point?.gex, point?.components?.gex),
      institutional: numeric(point?.institutional, point?.institutional_flow, point?.components?.institutional),
      macro: numeric(point?.macro, point?.macro_context, point?.components?.macro),
      combined: numeric(point?.combined, point?.score),
    })).filter((point) => point.calculated_at && Number.isFinite(point.combined)));
    if (currentFlow.fresh === true && currentFlow.observed_at && Number.isFinite(Number(currentFlow.score))) {
      history.push({
        calculated_at: currentFlow.observed_at,
        news: numeric(stableScores.news, item?.news_score),
        technical: numeric(stableScores.technical, item?.indicator_score),
        ...flowPoint({}, currentFlow),
        option_flow: numeric(stableScores.option_flow, stableFlow.option_flow?.score),
        gex: numeric(stableScores.gex, item?.gex?.score),
        institutional: numeric(stableScores.institutional_flow, stableFlow.institutional_flow?.score),
        macro: numeric(stableScores.macro),
        combined: numeric(stableScores.combined, item?.combined_score),
        live_depth: true,
      });
    }
    const snapshot = evidence.score_snapshot || {};
    // Historical score points must only use their signal-time snapshot.  Live
    // Binance depth is appended above as a separately timestamped point.
    const fallbackFlow = { ...(evidence.market_flow || {}) };
    const fallback = {
      calculated_at: snapshot.calculated_at || item?.updated_at || item?.discovered_at,
      news: numeric(snapshot.news, stableScores.news, item?.news_score),
      technical: numeric(snapshot.technical, stableScores.technical, item?.indicator_score),
      ...flowPoint(snapshot, fallbackFlow),
      option_flow: numeric(snapshot.option_flow, stableScores.option_flow, stableFlow.option_flow?.score, item?.option_flow?.score, evidence.option_flow?.score),
      gex: numeric(snapshot.gex, stableScores.gex, item?.gex?.score, evidence.gex?.score),
      institutional: numeric(snapshot.institutional, snapshot.institutional_flow, stableScores.institutional_flow, stableFlow.institutional_flow?.score, item?.institutional_flow?.score, evidence.institutional_flow?.score),
      macro: numeric(snapshot.macro, snapshot.macro_context, evidence.market_environment?.score),
      combined: numeric(snapshot.combined, stableScores.combined, item?.combined_score),
    };
    if (fallback.calculated_at && Number.isFinite(fallback.combined)) history.push(fallback);
    const unique = new Map();
    history.forEach((point) => unique.set(String(point.calculated_at), point));
    return [...unique.values()].sort((left, right) => this.parseDate(left.calculated_at).getTime() - this.parseDate(right.calculated_at).getTime());
  }

  scoreTrendState(history) {
    const latest = history.at(-1);
    const previous = history.at(-2);
    const delta = latest && previous ? latest.combined - previous.combined : 0;
    const direction = delta > 0.05 ? "up" : delta < -0.05 ? "down" : "flat";
    return {
      latest,
      previous,
      delta,
      direction,
      arrow: direction === "up" ? "↑" : direction === "down" ? "↓" : "→",
      badge: previous ? `${delta > 0 ? "+" : ""}${delta.toFixed(1)}` : "首个点",
    };
  }

  marketFlowTrendState(history) {
    const points = history.filter((point) => Number.isFinite(point.market_flow));
    const latest = points.at(-1);
    const previous = points.at(-2);
    const delta = latest && previous ? latest.market_flow - previous.market_flow : 0;
    const direction = delta > 0.05 ? "up" : delta < -0.05 ? "down" : "flat";
    const totalDepth = (point) => {
      if (!point) return null;
      const bid = Number(point.bid_depth_notional);
      const ask = Number(point.ask_depth_notional);
      if (!Number.isFinite(bid) && !Number.isFinite(ask)) return null;
      return (Number.isFinite(bid) ? bid : 0) + (Number.isFinite(ask) ? ask : 0);
    };
    const latestDepth = totalDepth(latest);
    const previousDepth = totalDepth(previous);
    const depthDeltaPct = latestDepth != null && previousDepth > 0 ? (latestDepth / previousDepth - 1) * 100 : null;
    return {
      points,
      latest,
      previous,
      delta,
      direction,
      arrow: direction === "up" ? "↑" : direction === "down" ? "↓" : "→",
      badge: previous ? `${delta > 0 ? "+" : ""}${delta.toFixed(1)}` : "首个点",
      latestDepth,
      depthDeltaPct,
    };
  }

  renderMarketFlowTrendChart(item, history) {
    const state = this.marketFlowTrendState(history);
    if (!state.points.length) return '<div class="score-trend-empty">暂无资金盘口历史，下一轮机会扫描后开始记录。</div>';
    const width = 920;
    const scoreHeight = 245;
    const amountHeight = 250;
    const padding = { left: 58, right: 24, top: 20, bottom: 42 };
    const scoreWidth = width - padding.left - padding.right;
    const scoreChartHeight = scoreHeight - padding.top - padding.bottom;
    const x = (index, length) => padding.left + (length === 1 ? scoreWidth / 2 : scoreWidth * index / (length - 1));
    const scoreY = (value) => padding.top + scoreChartHeight * (1 - Math.max(0, Math.min(100, Number(value))) / 100);
    const scoreGrid = [0, 25, 50, 75, 100].map((value) => `<g><line x1="${padding.left}" y1="${scoreY(value)}" x2="${width - padding.right}" y2="${scoreY(value)}"></line><text x="${padding.left - 10}" y="${scoreY(value) + 4}">${value}</text></g>`).join("");
    const scoreLine = state.points.map((point, index) => `${x(index, state.points.length).toFixed(2)},${scoreY(point.market_flow).toFixed(2)}`).join(" ");
    const scoreDots = state.points.map((point, index) => `<circle cx="${x(index, state.points.length).toFixed(2)}" cy="${scoreY(point.market_flow).toFixed(2)}" r="4"><title>${this.escape(this.formatDate(point.calculated_at))} · 盘口评分 ${point.market_flow.toFixed(1)}</title></circle>`).join("");
    const scoreLabels = [0, Math.floor((state.points.length - 1) / 2), state.points.length - 1].filter((value, index, values) => values.indexOf(value) === index).map((index, position, values) => `<text class="score-time-label" x="${x(index, state.points.length)}" y="${scoreHeight - 12}" text-anchor="${position === 0 ? "start" : position === values.length - 1 ? "end" : "middle"}">${this.escape(this.formatDate(state.points[index].calculated_at))}</text>`).join("");
    const amountPoints = history.filter((point) => Number.isFinite(point.bid_depth_notional) || Number.isFinite(point.ask_depth_notional));
    const amounts = amountPoints.flatMap((point) => [point.bid_depth_notional, point.ask_depth_notional]).filter(Number.isFinite);
    const amountMinRaw = amounts.length ? Math.min(...amounts) : 0;
    const amountMaxRaw = amounts.length ? Math.max(...amounts) : 0;
    const amountSpan = Math.max(amountMaxRaw - amountMinRaw, Math.abs(amountMaxRaw) * 0.08, 1);
    const amountMin = Math.max(0, amountMinRaw - amountSpan * 0.08);
    const amountMax = amountMaxRaw + amountSpan * 0.08;
    const amountChartHeight = amountHeight - padding.top - padding.bottom;
    const amountY = (value) => padding.top + amountChartHeight * (1 - (Number(value) - amountMin) / Math.max(amountMax - amountMin, 1));
    const amountGridValues = [amountMin, (amountMin + amountMax) / 2, amountMax];
    const amountGrid = amountGridValues.map((value) => `<g><line x1="${padding.left}" y1="${amountY(value)}" x2="${width - padding.right}" y2="${amountY(value)}"></line><text x="${padding.left - 10}" y="${amountY(value) + 4}">${this.escape(this.compactNumber(value))}</text></g>`).join("");
    const amountLine = (key, className) => {
      const points = amountPoints.map((point, index) => Number.isFinite(point[key]) ? `${x(index, amountPoints.length).toFixed(2)},${amountY(point[key]).toFixed(2)}` : "").filter(Boolean).join(" ");
      return points ? `<g class="score-line ${className}"><polyline points="${points}"></polyline></g>` : "";
    };
    const amountLabels = amountPoints.length ? [0, Math.floor((amountPoints.length - 1) / 2), amountPoints.length - 1].filter((value, index, values) => values.indexOf(value) === index).map((index, position, values) => `<text class="score-time-label" x="${x(index, amountPoints.length)}" y="${amountHeight - 12}" text-anchor="${position === 0 ? "start" : position === values.length - 1 ? "end" : "middle"}">${this.escape(this.formatDate(amountPoints[index].calculated_at))}</text>`).join("") : "";
    const scoreValues = state.points.map((point) => point.market_flow);
    const latest = state.latest;
    const bias = latest.market_flow >= 55 ? "买方偏强" : latest.market_flow <= 45 ? "卖方偏强" : "资金中性";
    const latestBid = Number.isFinite(latest.bid_depth_notional) ? this.compactNumber(latest.bid_depth_notional) : "--";
    const latestAsk = Number.isFinite(latest.ask_depth_notional) ? this.compactNumber(latest.ask_depth_notional) : "--";
    const rows = state.points.map((point, index) => {
      const previous = state.points[index - 1];
      const delta = previous ? point.market_flow - previous.market_flow : null;
      const deltaClass = delta == null || Math.abs(delta) <= 0.05 ? "flat" : delta > 0 ? "up" : "down";
      const percentage = (value) => Number.isFinite(value) ? `${(value * 100).toFixed(1)}%` : "--";
      return { point, delta, deltaClass, percentage };
    }).slice(-12).reverse().map(({ point, delta, deltaClass, percentage }) => `<tr><td>${this.escape(this.formatDate(point.calculated_at))}</td><td>${point.market_flow.toFixed(1)}</td><td class="${deltaClass}">${delta == null ? "--" : `${delta > 0 ? "+" : ""}${delta.toFixed(1)}`}</td><td>${Number.isFinite(point.bid_depth_notional) ? this.escape(this.compactNumber(point.bid_depth_notional)) : "--"}</td><td>${Number.isFinite(point.ask_depth_notional) ? this.escape(this.compactNumber(point.ask_depth_notional)) : "--"}</td><td>${Number.isFinite(point.main_force_ratio) ? point.main_force_ratio.toFixed(3) : "--"}</td><td>${percentage(point.active_buy_ratio)}</td><td>${Number.isFinite(point.bid_depth_change_30s_pct) ? `${point.bid_depth_change_30s_pct.toFixed(1)}%` : "--"} / ${Number.isFinite(point.ask_depth_change_30s_pct) ? `${point.ask_depth_change_30s_pct.toFixed(1)}%` : "--"}</td></tr>`).join("");
    const amountChart = amountPoints.length
      ? `<section class="score-trend-chart-panel market-flow-amount-panel"><header><div><strong>买卖盘名义资金量变化</strong><small>当前扫描保存的前 100 档名义金额；不同时间点同口径比较</small></div><div class="score-trend-legend"><span class="bid-depth"><i></i>买方深度</span><span class="ask-depth"><i></i>卖方深度</span></div></header><svg class="score-trend-chart" viewBox="0 0 ${width} ${amountHeight}" role="img" aria-label="${this.escape(item.symbol)} 买卖盘名义资金量走势"><g class="score-grid">${amountGrid}</g>${amountLine("bid_depth_notional", "bid-depth")}${amountLine("ask_depth_notional", "ask-depth")}${amountLabels}</svg></section>`
      : '<section class="market-flow-amount-empty"><strong>资金量历史尚在积累</strong><span>已有盘口强弱评分；买卖盘名义金额会从下一轮扫描开始逐点保存，不会用当前快照伪造历史。</span></section>';
    return `<section class="score-trend-summary market-flow-summary">
        <article><span>当前盘口评分</span><b>${latest.market_flow.toFixed(1)}</b><small>${this.escape(this.formatDate(latest.calculated_at))}</small></article>
        <article class="${state.direction}"><span>较上次</span><b>${state.arrow} ${state.badge}</b><small>${state.points.length} 个资金扫描点</small></article>
        <article><span>区间最高 / 最低</span><b>${Math.max(...scoreValues).toFixed(1)} / ${Math.min(...scoreValues).toFixed(1)}</b><small>仅当前机会生命周期</small></article>
        <article><span>买方 / 卖方深度</span><b>${this.escape(latestBid)} / ${this.escape(latestAsk)}</b><small>${state.depthDeltaPct == null ? "较上次暂无可比数据" : `总深度较上次 ${state.depthDeltaPct >= 0 ? "+" : ""}${state.depthDeltaPct.toFixed(1)}%`}</small></article>
        <article><span>资金方向</span><b>${bias}</b><small>主力量比 ${Number.isFinite(latest.main_force_ratio) ? latest.main_force_ratio.toFixed(3) : "--"}</small></article>
      </section>
      <section class="score-trend-chart-panel market-flow-score-panel"><header><div><strong>资金盘口强度走势</strong><small>纵轴 0–100 分；50 为中性，越高表示越支持当前预测方向</small></div><div class="score-trend-legend"><span class="market_flow"><i></i>资金盘口评分</span></div></header><svg class="score-trend-chart" viewBox="0 0 ${width} ${scoreHeight}" role="img" aria-label="${this.escape(item.symbol)} 资金盘口评分走势"><g class="score-grid">${scoreGrid}</g><g class="score-threshold neutral"><line x1="${padding.left}" y1="${scoreY(50)}" x2="${width - padding.right}" y2="${scoreY(50)}"></line><text x="${width - padding.right - 4}" y="${scoreY(50) - 6}">中性线 50</text></g><g class="score-line market_flow"><polyline points="${scoreLine}"></polyline>${scoreDots}</g>${scoreLabels}</svg></section>
      ${amountChart}
      <section class="score-trend-ledger market-flow-ledger"><header><strong>最近资金盘口明细</strong><small>评分是方向强度，不等同于资金金额；金额为 Binance 合约可见盘口名义值</small></header><div><table><thead><tr><th>计算时间</th><th>盘口评分</th><th>变化</th><th>买方深度</th><th>卖方深度</th><th>主力量比</th><th>主动买入</th><th>30秒挂单增速 买/卖</th></tr></thead><tbody>${rows}</tbody></table></div></section>`;
  }

  renderScoreTrendChart(item, history) {
    if (!history.length) return '<div class="score-trend-empty">暂无评分历史，下一轮机会扫描后开始记录。</div>';
    const width = 920;
    const height = 330;
    const padding = { left: 48, right: 22, top: 20, bottom: 42 };
    const chartWidth = width - padding.left - padding.right;
    const chartHeight = height - padding.top - padding.bottom;
    const x = (index) => padding.left + (history.length === 1 ? chartWidth / 2 : chartWidth * index / (history.length - 1));
    const y = (value) => padding.top + chartHeight * (1 - Math.max(0, Math.min(100, Number(value))) / 100);
    const allSeries = [
      { key: "combined", label: "组合评分", color: "#ad9cff" },
      { key: "news", label: "新闻评分", color: "#dfbd67" },
      { key: "technical", label: "技术指标", color: "#5bd6aa" },
      { key: "market_flow", label: "资金盘口", color: "#5dc4d8" },
      { key: "option_flow", label: "期权流", color: "#7ec8ff" },
      { key: "gex", label: "GEX", color: "#ff9bd5" },
      { key: "institutional", label: "机构流", color: "#f0a85f" },
      { key: "macro", label: "宏观环境", color: "#9fcb68" },
    ];
    const series = allSeries.filter((definition) => definition.key === "combined" || history.some((point) => Number.isFinite(point[definition.key])));
    const grid = [0, 25, 50, 75, 100].map((value) => `<g><line x1="${padding.left}" y1="${y(value)}" x2="${width - padding.right}" y2="${y(value)}"></line><text x="${padding.left - 9}" y="${y(value) + 4}">${value}</text></g>`).join("");
    const threshold = Number(this.state.config?.minimum_combined_score ?? 75);
    const lines = series.map((definition) => {
      const points = history.map((point, index) => Number.isFinite(point[definition.key]) ? `${x(index).toFixed(2)},${y(point[definition.key]).toFixed(2)}` : "").filter(Boolean).join(" ");
      const dots = definition.key === "combined" ? history.map((point, index) => `<circle cx="${x(index).toFixed(2)}" cy="${y(point.combined).toFixed(2)}" r="3.5"><title>${this.escape(this.formatDate(point.calculated_at))} · ${point.combined.toFixed(1)}</title></circle>`).join("") : "";
      return `<g class="score-line ${definition.key}"><polyline points="${points}"></polyline>${dots}</g>`;
    }).join("");
    const timeLabels = history.length === 1
      ? [{ index: 0, anchor: "middle" }]
      : [{ index: 0, anchor: "start" }, { index: Math.floor((history.length - 1) / 2), anchor: "middle" }, { index: history.length - 1, anchor: "end" }];
    const axes = timeLabels.map(({ index, anchor }) => `<text class="score-time-label" x="${x(index)}" y="${height - 12}" text-anchor="${anchor}">${this.escape(this.formatDate(history[index].calculated_at))}</text>`).join("");
    const state = this.scoreTrendState(history);
    const values = history.map((point) => point.combined);
    const entryScore = item.prediction_combined_score == null ? null : Number(item.prediction_combined_score);
    const recentRows = history.slice(-12).reverse().map((point, index) => {
      const prior = history[history.length - 2 - index];
      const delta = prior ? point.combined - prior.combined : null;
      const deltaClass = delta == null || Math.abs(delta) <= 0.05 ? "flat" : delta > 0 ? "up" : "down";
      return `<tr><td>${this.escape(this.formatDate(point.calculated_at))}</td><td>${point.combined.toFixed(1)}</td><td class="${deltaClass}">${delta == null ? "--" : `${delta > 0 ? "+" : ""}${delta.toFixed(1)}`}</td>${series.filter((definition) => definition.key !== "combined").map((definition) => `<td>${Number.isFinite(point[definition.key]) ? point[definition.key].toFixed(1) : "--"}</td>`).join("")}</tr>`;
    }).join("");
    return `<section class="score-trend-summary">
        <article><span>当前组合分</span><b>${state.latest.combined.toFixed(1)}</b><small>${this.escape(this.formatDate(state.latest.calculated_at))}</small></article>
        <article class="${state.direction}"><span>较上次</span><b>${state.arrow} ${state.badge}</b><small>${history.length} 个扫描点</small></article>
        <article><span>区间最高 / 最低</span><b>${Math.max(...values).toFixed(1)} / ${Math.min(...values).toFixed(1)}</b><small>仅当前机会生命周期</small></article>
        <article><span>预测入场分</span><b>${entryScore == null ? "--" : entryScore.toFixed(1)}</b><small>${entryScore == null ? "尚未生成预测" : "冻结，不随行情改写"}</small></article>
      </section>
      <section class="score-trend-chart-panel">
        <header><div><strong>评分变化折线图</strong><small>纵轴 0–100 分 · 横轴为机会扫描时间</small></div><div class="score-trend-legend">${series.map((definition) => `<span class="${definition.key}"><i></i>${definition.label}</span>`).join("")}</div></header>
        <svg class="score-trend-chart" viewBox="0 0 ${width} ${height}" role="img" aria-label="${this.escape(item.symbol)} 组合评分走势">
          <g class="score-grid">${grid}</g>
          <g class="score-threshold"><line x1="${padding.left}" y1="${y(threshold)}" x2="${width - padding.right}" y2="${y(threshold)}"></line><text x="${width - padding.right - 4}" y="${y(threshold) - 6}">准入线 ${threshold.toFixed(0)}</text></g>
          ${lines}${axes}
        </svg>
      </section>
      <section class="score-trend-ledger"><header><strong>最近评分明细</strong><small>分项缺失显示 --；最多保留 96 个扫描点</small></header><div><table><thead><tr><th>计算时间</th><th>组合分</th><th>变化</th>${series.filter((definition) => definition.key !== "combined").map((definition) => `<th>${definition.label}</th>`).join("")}</tr></thead><tbody>${recentRows}</tbody></table></div></section>`;
  }

  async openOrderBook(opportunityId, trigger) {
    const item = this.state.opportunities.find((opportunity) => opportunity.id === opportunityId);
    if (!item) return;
    this.orderBookOpportunity = item;
    this.orderBookFocus = trigger || null;
    this.orderBookSnapshot = null;
    this.orderBookPreviousSnapshot = null;
    this.orderBookLimit = 100;
    this.orderBookPaused = false;
    this.q("#order-book-title").textContent = `${item.symbol} · 实时100档盘口`;
    this.q("#order-book-subtitle").textContent = `${item.contract_symbol} · Binance Futures 映射合约 · 买卖各 100 档`;
    this.q("#order-book-body").innerHTML = '<div class="order-book-loading">正在同步 Binance 实时盘口…</div>';
    const modal = this.q("#order-book-modal");
    modal.classList.remove("hidden");
    modal.setAttribute("aria-hidden", "false");
    this.syncOrderBookControls();
    this.q("#order-book-close").focus({ preventScroll: true });
    await this.loadOrderBook();
    this.startOrderBookPolling();
  }

  closeOrderBook(restoreFocus = true) {
    window.clearInterval(this.orderBookTimer);
    this.orderBookTimer = null;
    this.orderBookRequestId += 1;
    const modal = this.q("#order-book-modal");
    if (!modal || modal.classList.contains("hidden")) return;
    modal.classList.add("hidden");
    modal.setAttribute("aria-hidden", "true");
    this.orderBookOpportunity = null;
    this.orderBookSnapshot = null;
    this.orderBookPreviousSnapshot = null;
    const focusTarget = this.orderBookFocus;
    this.orderBookFocus = null;
    if (restoreFocus && focusTarget?.isConnected) focusTarget.focus({ preventScroll: true });
  }

  startOrderBookPolling() {
    window.clearInterval(this.orderBookTimer);
    this.orderBookTimer = null;
    if (this.orderBookPaused || !this.orderBookOpportunity) return;
    this.orderBookTimer = window.setInterval(() => {
      if (document.visibilityState === "visible") this.loadOrderBook();
    }, 1000);
  }

  setOrderBookLimit(limit) {
    if (![20, 50, 100].includes(limit) || limit === this.orderBookLimit) return;
    this.orderBookLimit = limit;
    this.orderBookPreviousSnapshot = null;
    this.syncOrderBookControls();
    this.loadOrderBook();
  }

  toggleOrderBookPause() {
    this.orderBookPaused = !this.orderBookPaused;
    if (this.orderBookPaused) {
      // Invalidate an in-flight frame so the visible snapshot really freezes
      // at the moment the operator presses pause.
      this.orderBookRequestId += 1;
      window.clearInterval(this.orderBookTimer);
      this.orderBookTimer = null;
    } else {
      this.loadOrderBook();
      this.startOrderBookPolling();
    }
    this.syncOrderBookControls();
  }

  syncOrderBookControls() {
    this.qa("[data-order-book-limit]").forEach((button) => button.classList.toggle("active", Number(button.dataset.orderBookLimit) === this.orderBookLimit));
    const pause = this.q("#order-book-pause");
    if (pause) {
      pause.classList.toggle("active", this.orderBookPaused);
      pause.textContent = this.orderBookPaused ? "继续刷新" : "暂停刷新";
    }
    const liveState = this.q("#order-book-live-state");
    if (liveState && this.orderBookPaused) {
      liveState.className = "order-book-live-state paused";
      liveState.textContent = "已暂停";
    }
  }

  async loadOrderBook() {
    const item = this.orderBookOpportunity;
    if (!item) return;
    const requestId = ++this.orderBookRequestId;
    try {
      const snapshot = await this.api(`/opportunities/${encodeURIComponent(item.id)}/order-book?limit=${this.orderBookLimit}`);
      if (requestId !== this.orderBookRequestId || this.orderBookOpportunity?.id !== item.id) return;
      this.orderBookPreviousSnapshot = this.orderBookSnapshot;
      this.orderBookSnapshot = snapshot;
      this.renderOrderBook();
    } catch (error) {
      if (requestId !== this.orderBookRequestId || !this.orderBookOpportunity) return;
      const liveState = this.q("#order-book-live-state");
      liveState.className = "order-book-live-state error";
      liveState.textContent = "同步中断";
      if (!this.orderBookSnapshot) this.q("#order-book-body").innerHTML = `<div class="order-book-empty"><strong>实时盘口暂未同步</strong><span>${this.escape(error.message || "Binance 深度采集器正在重连，请稍后重试。")}</span><button type="button" data-order-book-retry>重新读取</button></div>`;
      this.q("[data-order-book-retry]")?.addEventListener("click", () => this.loadOrderBook());
    }
  }

  orderBookDelta(row, side) {
    const previousRows = this.orderBookPreviousSnapshot?.[side] || [];
    const previous = previousRows.find((entry) => Number(entry.price) === Number(row.price));
    if (!previous) return null;
    const delta = Number(row.quantity) - Number(previous.quantity);
    return Number.isFinite(delta) ? delta : null;
  }

  renderOrderBookChart(snapshot) {
    const bids = Array.isArray(snapshot.bids) ? snapshot.bids : [];
    const asks = Array.isArray(snapshot.asks) ? snapshot.asks : [];
    if (!bids.length || !asks.length) return '<div class="order-book-chart-empty">累计深度不足</div>';
    const width = 780;
    const height = 230;
    const padding = { left: 28, right: 28, top: 20, bottom: 28 };
    const prices = [...bids, ...asks].map((row) => Number(row.price)).filter(Number.isFinite);
    const minPrice = Math.min(...prices);
    const maxPrice = Math.max(...prices);
    const maxDepth = Math.max(...bids.map((row) => Number(row.cumulative_notional) || 0), ...asks.map((row) => Number(row.cumulative_notional) || 0), 1);
    const x = (price) => padding.left + ((Number(price) - minPrice) / Math.max(maxPrice - minPrice, Number.EPSILON)) * (width - padding.left - padding.right);
    const y = (depth) => height - padding.bottom - (Number(depth) / maxDepth) * (height - padding.top - padding.bottom);
    const points = (rows) => rows.map((row) => `${x(row.price).toFixed(1)},${y(row.cumulative_notional).toFixed(1)}`).join(" ");
    const midX = x(snapshot.mid_price);
    return `<svg class="order-book-chart" viewBox="0 0 ${width} ${height}" role="img" aria-label="${this.escape(snapshot.contract_symbol)} 累计深度图">
      <defs><linearGradient id="bid-depth-fill" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#5bd6aa" stop-opacity=".26"/><stop offset="1" stop-color="#5bd6aa" stop-opacity="0"/></linearGradient><linearGradient id="ask-depth-fill" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#ff7888" stop-opacity=".26"/><stop offset="1" stop-color="#ff7888" stop-opacity="0"/></linearGradient></defs>
      <g class="order-book-chart-grid"><line x1="${padding.left}" y1="${height - padding.bottom}" x2="${width - padding.right}" y2="${height - padding.bottom}"/><line x1="${midX}" y1="${padding.top}" x2="${midX}" y2="${height - padding.bottom}"/></g>
      <polygon class="bid-fill" points="${points(bids)} ${x(bids[bids.length - 1].price)},${height - padding.bottom} ${x(bids[0].price)},${height - padding.bottom}"/><polygon class="ask-fill" points="${points(asks)} ${x(asks[asks.length - 1].price)},${height - padding.bottom} ${x(asks[0].price)},${height - padding.bottom}"/>
      <polyline class="bid-line" points="${points(bids)}"/><polyline class="ask-line" points="${points(asks)}"/>
      <text x="${padding.left}" y="${height - 8}">${this.escape(this.compactNumber(minPrice))}</text><text x="${midX}" y="${height - 8}" text-anchor="middle">中间价 ${this.escape(this.compactNumber(snapshot.mid_price))}</text><text x="${width - padding.right}" y="${height - 8}" text-anchor="end">${this.escape(this.compactNumber(maxPrice))}</text>
    </svg>`;
  }

  renderOrderBook() {
    const snapshot = this.orderBookSnapshot;
    if (!snapshot) return;
    const bids = Array.isArray(snapshot.bids) ? snapshot.bids : [];
    const asks = Array.isArray(snapshot.asks) ? snapshot.asks : [];
    const age = Number(snapshot.age_seconds);
    const live = Number.isFinite(age) && age <= 4;
    const liveState = this.q("#order-book-live-state");
    liveState.className = `order-book-live-state ${this.orderBookPaused ? "paused" : live ? "live" : "stale"}`;
    liveState.textContent = this.orderBookPaused ? "已暂停" : live ? `实时 · ${age.toFixed(0)}s` : `延迟 · ${Number.isFinite(age) ? `${age.toFixed(0)}s` : "--"}`;
    const rows = Math.max(bids.length, asks.length);
    const maxNotional = Math.max(...bids.map((row) => Number(row.notional) || 0), ...asks.map((row) => Number(row.notional) || 0), 1);
    const largestBidRank = Number(snapshot.largest_bid_wall?.rank);
    const largestAskRank = Number(snapshot.largest_ask_wall?.rank);
    const deltaMarkup = (row, side) => {
      if (!row) return "";
      const delta = this.orderBookDelta(row, side);
      if (delta == null || Math.abs(delta) < Number.EPSILON) return '<small class="flat">—</small>';
      return `<small class="${delta > 0 ? "up" : "down"}">${delta > 0 ? "+" : ""}${this.escape(this.compactNumber(delta))}</small>`;
    };
    const levelCell = (row, side, content, extraClass = "") => row ? `<button class="order-book-level ${side} ${extraClass}" type="button" data-order-book-side="${side}" data-order-book-rank="${row.rank}" style="--depth:${Math.max(2, Number(row.notional) / maxNotional * 100).toFixed(1)}%">${content}</button>` : "--";
    const bodyRows = Array.from({ length: rows }, (_, index) => {
      const bid = bids[index];
      const ask = asks[index];
      const bidWall = bid && bid.rank === largestBidRank ? '<i>买墙</i>' : "";
      const askWall = ask && ask.rank === largestAskRank ? '<i>卖墙</i>' : "";
      return `<tr>
        <td>${bid ? this.escape(this.compactMetric(bid.cumulative_notional)) : "--"}</td><td>${bid ? levelCell(bid, "bid", `${this.escape(this.compactMetric(bid.notional))}${deltaMarkup(bid, "bids")}`) : "--"}</td><td>${bid ? levelCell(bid, "bid", `${this.escape(this.compactNumber(bid.quantity))}${bidWall}`) : "--"}</td><td class="distance bid">${bid ? `${Number(bid.distance_bps).toFixed(1)} bps` : "--"}</td>
        <td class="order-book-prices"><span class="bid">${bid ? this.escape(this.compactNumber(bid.price)) : "--"}</span><span class="ask">${ask ? this.escape(this.compactNumber(ask.price)) : "--"}</span></td>
        <td>${ask ? levelCell(ask, "ask", `${this.escape(this.compactNumber(ask.quantity))}${askWall}`) : "--"}</td><td>${ask ? levelCell(ask, "ask", `${this.escape(this.compactMetric(ask.notional))}${deltaMarkup(ask, "asks")}`) : "--"}</td><td>${ask ? this.escape(this.compactMetric(ask.cumulative_notional)) : "--"}</td><td class="distance ask">${ask ? `+${Number(ask.distance_bps).toFixed(1)} bps` : "--"}</td>
      </tr>`;
    }).join("");
    const ratio = Number(snapshot.bid_ask_ratio);
    const ratioTone = !Number.isFinite(ratio) ? "flat" : ratio > 1.05 ? "up" : ratio < .95 ? "down" : "flat";
    const change = (value) => value == null || !Number.isFinite(Number(value)) ? "--" : `${Number(value) > 0 ? "+" : ""}${Number(value).toFixed(1)}%`;
    this.q("#order-book-body").innerHTML = `<section class="order-book-summary">
      <article class="bid"><span>买方 ${snapshot.limit} 档</span><b>${this.escape(this.compactMetric(snapshot.bid_depth_notional, " U"))}</b><small>30秒 ${change(snapshot.bid_depth_change_30s_pct)}</small></article>
      <article class="ask"><span>卖方 ${snapshot.limit} 档</span><b>${this.escape(this.compactMetric(snapshot.ask_depth_notional, " U"))}</b><small>30秒 ${change(snapshot.ask_depth_change_30s_pct)}</small></article>
      <article class="${ratioTone}"><span>买卖深度比</span><b>${Number.isFinite(ratio) ? ratio.toFixed(3) : "--"}</b><small>失衡 ${Number(snapshot.book_imbalance || 0).toFixed(3)}</small></article>
      <article><span>最优价差</span><b>${Number(snapshot.spread_bps).toFixed(2)} bps</b><small>${this.escape(this.compactNumber(snapshot.best_bid))} / ${this.escape(this.compactNumber(snapshot.best_ask))}</small></article>
      <article class="bid"><span>最大买墙</span><b>${this.escape(this.compactMetric(snapshot.largest_bid_wall?.notional, " U"))}</b><small>${this.escape(this.compactNumber(snapshot.largest_bid_wall?.price))} · 第 ${this.number(snapshot.largest_bid_wall?.rank)} 档</small></article>
      <article class="ask"><span>最大卖墙</span><b>${this.escape(this.compactMetric(snapshot.largest_ask_wall?.notional, " U"))}</b><small>${this.escape(this.compactNumber(snapshot.largest_ask_wall?.price))} · 第 ${this.number(snapshot.largest_ask_wall?.rank)} 档</small></article>
    </section>
    <div class="order-book-workspace"><section class="order-book-ladder"><header><strong>买卖盘口梯形表</strong><small>更新 ${this.escape(this.formatUnix(snapshot.captured_at))} · Update ID ${this.escape(snapshot.last_update_id)}</small></header><div><table><thead><tr><th>买方累计</th><th>买方金额 / Δ数量</th><th>买方数量</th><th>距中间价</th><th>买价 / 卖价</th><th>卖方数量</th><th>卖方金额 / Δ数量</th><th>卖方累计</th><th>距中间价</th></tr></thead><tbody>${bodyRows}</tbody></table></div></section>
      <aside class="order-book-visual"><section><header><strong>累计深度走势</strong><small>绿色买盘 · 红色卖盘</small></header>${this.renderOrderBookChart(snapshot)}</section><section id="order-book-selection" class="order-book-selection"><span>LEVEL INSPECTOR</span><strong>点击任意档位查看详情</strong><small>展示该档价格、数量、名义金额与距中间价距离。</small></section><section class="order-book-definition"><strong>数据口径</strong><p>买方/卖方深度为当前可见限价挂单的价格 × 数量汇总。撤单可能瞬间消失，因此它是流动性快照，不等同于成交资金或主力净流入。</p></section></aside></div>`;
  }

  selectOrderBookLevel(side, rank) {
    const rows = side === "ask" ? this.orderBookSnapshot?.asks : this.orderBookSnapshot?.bids;
    const row = (rows || []).find((entry) => Number(entry.rank) === rank);
    const target = this.q("#order-book-selection");
    if (!row || !target) return;
    const delta = this.orderBookDelta(row, side === "ask" ? "asks" : "bids");
    target.className = `order-book-selection ${side}`;
    target.innerHTML = `<span>${side === "ask" ? "SELL LEVEL" : "BUY LEVEL"} · 第 ${rank} 档</span><strong>${this.escape(this.compactNumber(row.price))}</strong><div><b>数量 ${this.escape(this.compactNumber(row.quantity))}</b><b>名义金额 ${this.escape(this.compactMetric(row.notional, " U"))}</b><b>累计 ${this.escape(this.compactMetric(row.cumulative_notional, " U"))}</b><b>距中间价 ${Number(row.distance_bps).toFixed(2)} bps</b></div><small>较上一帧数量 ${delta == null ? "无可比快照" : `${delta > 0 ? "+" : ""}${this.escape(this.compactNumber(delta))}`}</small>`;
  }

  openScoreTrend(opportunityId, trigger) {
    const item = this.state.opportunities.find((opportunity) => opportunity.id === opportunityId);
    if (!item) return;
    const history = this.opportunityScoreHistory(item);
    const modal = this.q("#score-trend-modal");
    this.scoreTrendOpportunity = item;
    this.scoreTrendFocus = trigger || null;
    this.q("#score-trend-title").textContent = `${item.symbol} · 组合评分走势`;
    this.q("#score-trend-subtitle").textContent = `${item.contract_symbol} · ${item.timeframe} 周期 · ${history.length} 个扫描点`;
    this.q("#score-trend-body").innerHTML = this.renderScoreTrendChart(item, history);
    this.q(".score-trend-foot").innerHTML = "<span>当前机会评分随扫描更新</span><strong>预测入场评分保持冻结</strong>";
    modal.classList.remove("hidden");
    modal.setAttribute("aria-hidden", "false");
    this.q("#score-trend-close").focus({ preventScroll: true });
  }

  openMarketFlowTrend(opportunityId, trigger) {
    const item = this.state.opportunities.find((opportunity) => opportunity.id === opportunityId);
    if (!item) return;
    const history = this.opportunityScoreHistory(item);
    const state = this.marketFlowTrendState(history);
    const modal = this.q("#score-trend-modal");
    this.scoreTrendOpportunity = item;
    this.scoreTrendFocus = trigger || null;
    this.q("#score-trend-title").textContent = `${item.symbol} · 资金盘口变化`;
    this.q("#score-trend-subtitle").textContent = `${item.contract_symbol} · ${item.timeframe} 周期 · ${state.points.length} 个资金扫描点`;
    this.q("#score-trend-body").innerHTML = this.renderMarketFlowTrendChart(item, history);
    this.q(".score-trend-foot").innerHTML = "<span>盘口评分与买卖盘名义资金量随扫描更新</span><strong>不把评分冒充真实资金金额</strong>";
    modal.classList.remove("hidden");
    modal.setAttribute("aria-hidden", "false");
    this.q("#score-trend-close").focus({ preventScroll: true });
  }

  closeScoreTrend() {
    const modal = this.q("#score-trend-modal");
    if (!modal || modal.classList.contains("hidden")) return;
    modal.classList.add("hidden");
    modal.setAttribute("aria-hidden", "true");
    this.scoreTrendOpportunity = null;
    const focusTarget = this.scoreTrendFocus;
    this.scoreTrendFocus = null;
    if (focusTarget?.isConnected) focusTarget.focus({ preventScroll: true });
  }

  virtualEntryGate(item) {
    const evidence = item?.evidence || {};
    const frozenGate = item?.prediction_entry_gate;
    const liveGate = evidence.virtual_entry_gate;
    const stableGate = item?.gate_summary && typeof item.gate_summary === "object" ? item.gate_summary : null;
    const hasCurrentDecisionChecks = stableGate
      && stableGate.decision_checks
      && typeof stableGate.decision_checks === "object"
      && Object.keys(stableGate.decision_checks).length > 0;
    const source = frozenGate && Array.isArray(frozenGate.checks)
      ? frozenGate
      : !hasCurrentDecisionChecks && liveGate && Array.isArray(liveGate.checks)
      ? liveGate
      : null;
    if (source) return { ...source, frozen: source === frozenGate };
    const config = this.state.config || {};
    const indicatorPolicy = evidence.indicator_policy || {};
    const marketFlow = item?.flow && typeof item.flow === "object" ? item.flow : evidence.market_flow || {};
    const market = evidence.market || {};
    const snapshot = this.normalizeFeatureSnapshot(item);
    const scores = snapshot.scoreComponents;
    const newsScore = Number(this.firstValue(item?.news_score, scores.news, 0));
    const indicatorScore = Number(this.firstValue(item?.indicator_score, scores.technical, 0));
    const combinedScore = Number(this.firstValue(item?.combined_score, scores.combined, 0));
    const entryPrice = Number(this.firstValue(
      item?.prediction_entry_price,
      item?.binance_contract_quote?.price,
      market.price,
      snapshot.quote.price,
      snapshot.quote.last_price,
      0,
    ));
    const minimumNewsScore = Number(config.minimum_news_confidence ?? 0.6) * 100;
    const minimumIndicatorScore = Number(config.minimum_indicator_score ?? 65);
    const minimumCombinedScore = Number(config.minimum_combined_score ?? 75);
    const trigger = evidence.news_trigger || {};
    const marketQuality = evidence.market_quality || {};
    const gateLabels = {
      price_available: ["实时价格", "必须取得有效参考价格"],
      ticker_fresh: ["行情时效", "实时行情必须在允许延迟内"],
      kline_fresh: ["K 线时效", "技术 K 线必须已收盘且新鲜"],
      feature_quality: ["技术质量", "技术特征质量必须达到门槛"],
      quote_fresh: ["Quote 时效", "参考 Quote 必须足够新鲜"],
      spread_acceptable: ["买卖点差", "点差必须低于风险上限"],
      quote_sane: ["Quote 合法性", "买卖价必须有效且顺序正确"],
      not_halted: ["交易状态", "标的不得停牌或处于冷却期"],
      data_coverage: ["数据覆盖", "关键行情输入必须达到覆盖门槛"],
      event_window_clear: ["事件窗口", "不得处于高影响事件窗口"],
      directional_conflict_clear: ["盘口冲突", "资金方向不得与候选方向强冲突"],
    };
    const stableChecksSource = stableGate?.decision_checks || stableGate?.checks || {};
    const stableChecks = stableGate
      ? Object.entries(stableChecksSource).map(([key, value]) => ({
          key,
          label: gateLabels[key]?.[0] || key,
          passed: value === true,
          current: value == null ? null : value,
          required: true,
          detail: value == null ? `${gateLabels[key]?.[1] || key}：无数据` : gateLabels[key]?.[1] || key,
          source: "stable_gate_summary",
        }))
      : [];
    if (stableGate && !stableChecks.length && ["not_evaluated", "unavailable", "missing"].includes(String(stableGate.status || "").toLowerCase())) {
      stableChecks.push({ key: "market_data_evaluation", label: "行情门控", passed: false, current: null, required: true, detail: "无数据：尚未完成行情与风险门控评估", source: "stable_gate_summary" });
    }
    const checks = [
      ...(config.require_new_news_trigger ? [{ key: "new_news_trigger", label: "新事件", passed: trigger.has_new_news === true, current: trigger.has_new_news === true, required: true, detail: "触发窗口内存在未消费的新新闻" }] : []),
      { key: "news_candidate", label: "新闻候选", passed: newsScore >= minimumNewsScore, current: newsScore, required: minimumNewsScore, detail: "新闻评分达到候选门槛" },
      { key: "indicator_policy", label: "策略组", passed: indicatorPolicy.passed === true || evidence.technical_confirmed === true, current: indicatorPolicy.passed === true, required: true, detail: "至少一个核心技术策略组通过" },
      { key: "indicator_score", label: "技术评分", passed: indicatorScore >= minimumIndicatorScore, current: indicatorScore, required: minimumIndicatorScore, detail: "方向一致的技术强度" },
      { key: "combined_score", label: "组合评分", passed: combinedScore >= minimumCombinedScore, current: combinedScore, required: minimumCombinedScore, detail: "新闻、技术与盘口加权结果" },
      ...(stableChecks.length ? stableChecks : [
        { key: "market_flow_conflict", label: "盘口冲突", passed: marketFlow.hard_conflict !== true, current: marketFlow.hard_conflict === true, required: false, detail: "候选方向不得存在资金强冲突" },
        ...(config.require_market_quality_for_prediction ? [{ key: "market_quality", label: "行情质量", passed: marketQuality.passed === true, current: marketQuality.passed === true, required: true, detail: marketQuality.passed == null ? "无数据：未保存行情质量评估" : "实时价、已收盘 K 线与预测因子新鲜可用" }] : []),
      ]),
      { key: "entry_price", label: "入场价格", passed: entryPrice > 0, current: entryPrice > 0 ? entryPrice : null, required: "> 0", detail: "取得真实扫描参考价后才能冻结" },
    ];
    const signalConfirmed = checks.filter((check) => check.key !== "entry_price").every((check) => check.passed);
    return {
      version: stableGate?.decision_version || "frontend_legacy_fallback",
      execution_mode: "prediction_only",
      real_order_enabled: false,
      direction: item?.direction === "short" ? "short" : "long",
      signal_confirmed: signalConfirmed,
      entry_ready: signalConfirmed && checks.at(-1).passed,
      reference_price: entryPrice > 0 ? entryPrice : null,
      checked_at: stableGate?.evaluated_at || item?.prediction_created_at || item?.updated_at || item?.discovered_at,
      checks,
      frozen: item?.prediction_entry_price != null,
      gate_summary: stableGate,
    };
  }

  opportunityLifecycleState(item, gate) {
    const evidence = item?.evidence || {};
    const explicit = String(this.firstValue(item?.lifecycle_status, item?.lifecycle_state, item?.state, evidence.lifecycle_state, evidence.decision_state, "")).toLowerCase();
    const explicitMap = {
      candidate: "candidate",
      discovered: "candidate",
      confirmed: "candidate",
      ready: "ready",
      triggered: "triggered",
      holding: "triggered",
      entered: "triggered",
      blocked: "blocked",
      rejected: "blocked",
      expired: "blocked",
      dismissed: "blocked",
      data_error: "data_error",
      error: "data_error",
      failed: "data_error",
    };
    const hasPrediction = Boolean(item?.prediction_status || item?.prediction_created_at);
    const entryPrice = Number(item?.prediction_entry_price || 0);
    if (explicitMap[explicit]) {
      if (hasPrediction && entryPrice > 0 && ["candidate", "ready"].includes(explicitMap[explicit])) return "triggered";
      if (explicit === "confirmed" && gate?.entry_ready) return "ready";
      if (!["candidate", "confirmed", "discovered"].includes(explicit)) return explicitMap[explicit];
    }
    if (hasPrediction && entryPrice > 0) return "triggered";
    if (hasPrediction) return "data_error";
    const storageStatus = String(item?.status || "").toLowerCase();
    if (["blocked", "rejected"].includes(storageStatus)) return "blocked";
    if (["data_error", "error", "failed"].includes(storageStatus)) return "data_error";
    const snapshot = this.normalizeFeatureSnapshot(item);
    const dataQualityState = String(this.firstValue(snapshot.dataQuality.status, snapshot.dataQuality.state, "")).toLowerCase();
    const dataErrors = snapshot.dataQuality.errors || snapshot.dataQuality.error_codes || [];
    if (["error", "failed", "invalid"].includes(dataQualityState) || (Array.isArray(dataErrors) && dataErrors.length)) return "data_error";
    const hardRiskBlock = evidence.risk_gate?.blocked === true
      || evidence.event_gate?.blocked === true
      || evidence.halt?.active === true
      || snapshot.riskEvents.some((event) => event?.blocked === true || ["critical", "blocked"].includes(String(event?.risk_level || event?.severity || "").toLowerCase()));
    const hardFailedKeys = new Set(["market_quality", "market_flow_conflict", "order_book_quality", "order_book_direction", "order_book_usable", "quote_freshness", "quote_spread", "halt", "risk_event", "event_gate", "price_available", "ticker_fresh", "kline_fresh", "feature_quality", "quote_fresh", "spread_acceptable", "quote_sane", "not_halted", "data_coverage", "event_window_clear", "directional_conflict_clear"]);
    const hardGateFailure = (gate?.checks || []).some((check) => !check.passed && hardFailedKeys.has(check.key));
    const stableGateBlocked = String(item?.gate_summary?.status || "").toLowerCase() === "blocked" || item?.gate_summary?.passed === false && Array.isArray(item?.gate_summary?.blocking_reasons) && item.gate_summary.blocking_reasons.length > 0;
    if (hardRiskBlock || hardGateFailure || stableGateBlocked) return "blocked";
    if (gate?.entry_ready) return "ready";
    return "candidate";
  }

  virtualEntryState(item, gate) {
    const hasPrediction = Boolean(item?.prediction_status || item?.prediction_created_at);
    const entryPrice = Number(item?.prediction_entry_price || 0);
    const direction = item?.direction === "short" ? "做空" : "做多";
    const lifecycle = this.opportunityLifecycleState(item, gate);
    if (lifecycle === "triggered" && hasPrediction && entryPrice > 0) {
      const suffix = item.prediction_status === "completed"
        ? "已结算"
        : item.prediction_status === "unavailable"
        ? "退出行情不足"
        : "监控退出条件";
      return { tone: "triggered", label: direction, detail: suffix, triggered: true };
    }
    if (lifecycle === "data_error") {
      return { tone: "data_error", label: "数据异常", detail: hasPrediction ? "已生成记录但缺少有效入场价格" : "关键数据无效，系统已停止触发", triggered: false };
    }
    if (lifecycle === "blocked") {
      const failed = (gate.checks || []).filter((check) => !check.passed);
      const reasonCodes = Array.isArray(item?.gate_summary?.blocking_reasons) ? item.gate_summary.blocking_reasons : [];
      const retryableCodes = new Set(["EXECUTION_PRICE_STALE", "TECHNICAL_BAR_STALE", "REFERENCE_QUOTE_UNAVAILABLE", "REFERENCE_QUOTE_STALE", "MARKET_DATA_COVERAGE_LOW"]);
      const retryable = reasonCodes.length > 0 && reasonCodes.every((code) => retryableCodes.has(String(code)));
      const retryableLabels = {
        EXECUTION_PRICE_STALE: "实时价格过期",
        TECHNICAL_BAR_STALE: "等待新 K 线",
        REFERENCE_QUOTE_UNAVAILABLE: "等待买卖报价",
        REFERENCE_QUOTE_STALE: "买卖报价过期",
        MARKET_DATA_COVERAGE_LOW: "关键行情未到齐",
      };
      const reason = reasonCodes.map((code) => retryableLabels[code]).filter(Boolean)[0];
      return {
        tone: "blocked",
        label: retryable ? "等待行情" : "风险阻断",
        detail: retryable ? `${reason || "行情暂不可用"}，下一轮扫描自动重试` : failed[0]?.detail || "事件、停牌或资金风险门控未通过",
        triggered: false,
        retryable,
      };
    }
    if (lifecycle === "ready") {
      return { tone: "ready", label: "条件已满足", detail: "等待预测记录写入", triggered: false };
    }
    const failed = (gate.checks || []).filter((check) => !check.passed);
    return { tone: "candidate", label: "候选观察", detail: failed.length ? `仍有 ${failed.length} 项条件未满足` : "等待下一轮扫描", triggered: false };
  }

  virtualPositionSnapshot(item) {
    const source = item?.virtual_position;
    const evidence = item?.evidence || {};
    const snapshot = this.normalizeFeatureSnapshot(item);
    const fallbackRiskPlan = item?.trade_plan || evidence.trade_plan || evidence.risk_plan || {};
    if (source && typeof source === "object") {
      const netReturnBps = this.firstValue(source.net_return_bps, source.directional_return_bps);
      return {
        ...source,
        current_price: this.firstValue(source.current_price, source.mark_price, source.last_price, snapshot.quote.price, snapshot.quote.last_price),
        market_at: this.firstValue(source.market_at, source.quote_at, source.updated_at, snapshot.quote.timestamp, snapshot.quote.captured_at),
        net_return_pct: this.firstValue(source.net_return_pct, netReturnBps != null ? Number(netReturnBps) / 100 : undefined),
        risk_plan: source.risk_plan || fallbackRiskPlan,
      };
    }
    const entry = Number(item?.prediction_entry_price || 0);
    const current = Number(this.firstValue(snapshot.quote.price, snapshot.quote.last_price, snapshot.quote.current_price, evidence.market?.price, 0));
    if (!(entry > 0) || !(current > 0)) return { available: false };
    const directionFactor = item?.direction === "short" ? -1 : 1;
    const grossBps = (current / entry - 1) * 10000 * directionFactor;
    return {
      available: true,
      entry_price: entry,
      current_price: current,
      market_at: this.firstValue(snapshot.quote.timestamp, snapshot.quote.captured_at, snapshot.quote.updated_at, item?.updated_at),
      valuation_state: "scan_fallback",
      gross_return_bps: grossBps,
      gross_return_pct: grossBps / 100,
      net_return_bps: grossBps,
      net_return_pct: grossBps / 100,
      gross_pnl_per_unit: (current - entry) * directionFactor,
      net_pnl_per_unit: (current - entry) * directionFactor,
      net_pnl_per_10000: grossBps,
      profit_state: grossBps > 0 ? "profit" : grossBps < 0 ? "loss" : "flat",
      target_state: "active",
      risk_plan: fallbackRiskPlan,
    };
  }

  predictionSettlementState(item) {
    const source = item?.prediction_settlement || {};
    const dueAt = source.due_at || item?.prediction_due_at;
    const dueMs = this.parseDate(dueAt).getTime();
    const graceHours = Math.max(1, Number(source.grace_hours) || 6);
    const graceDeadline = source.grace_deadline || (Number.isFinite(dueMs) ? new Date(dueMs + graceHours * 3600000).toISOString() : null);
    const graceMs = this.parseDate(graceDeadline).getTime();
    const now = Date.now();
    const phase = source.phase || (item?.prediction_status === "completed"
      ? "completed"
      : item?.prediction_status === "unavailable"
      ? "unavailable"
      : Number.isFinite(dueMs) && now < dueMs
      ? "monitoring_exit"
      : Number.isFinite(graceMs) && now > graceMs
      ? "overdue"
      : "awaiting_market_data");
    const labels = {
      monitoring_exit: "正在监控退出条件",
      scheduled: "正在监控退出条件",
      awaiting_market_data: "已达持有上限，等待行情",
      overdue: "退出处理已超时",
      completed: "退出结算已完成",
      unavailable: "退出行情不足",
    };
    const details = {
      monitoring_exit: "后台正在按 15 分钟 K 线监控止盈、止损与评分转弱/反转；最大持有上限前任一条件先触发即退出。",
      scheduled: "后台正在按 15 分钟 K 线监控止盈、止损与评分转弱/反转；最大持有上限前任一条件先触发即退出。",
      awaiting_market_data: `已经达到最大持有上限；退出 K 线未到齐时每 ${Number(source.retry_interval_minutes || 5)} 分钟重试。`,
      overdue: `已经超过 ${graceHours} 小时宽限期，后台补偿任务将其转为“行情不足”。`,
      completed: "已按最先触发的价格、评分或最大持有条件退出，并完成方向收益与命中结果计算。",
      unavailable: `宽限期内始终没有取得有效退出行情，因此不计入命中率。`,
    };
    return {
      phase,
      label: labels[phase] || "监控退出条件",
      detail: details[phase] || "等待后台根据价格、评分或最大持有条件处理退出。",
      dueAt,
      graceDeadline,
      lastAttemptAt: source.last_attempt_at,
      nextRetryAt: source.next_retry_at,
      priceTimeframe: source.price_timeframe || "15m",
    };
  }

  signedMetric(value, digits = 2, suffix = "") {
    const number = Number(value);
    if (!Number.isFinite(number)) return "--";
    return `${number > 0 ? "+" : ""}${number.toFixed(digits)}${suffix}`;
  }

  compactMetric(value, suffix = "") {
    const number = Number(value);
    if (!Number.isFinite(number)) return "--";
    return `${new Intl.NumberFormat("en-US", { notation: "compact", maximumFractionDigits: 2 }).format(number)}${suffix}`;
  }

  opportunityFeatureMarkup(item) {
    if (this.state.scorePolicy?.enabled === false) {
      return `<section class="opportunity-feature-disabled" data-patch-key="enhanced-features"><strong>Unusual Whales 已关闭</strong><span>本卡不使用 Quote、期权流、GEX、场内/场外成交和事件门控；仅按新闻、技术指标与基础行情评估。</span></section>`;
    }
    const evidence = item?.evidence || {};
    const stableGate = item?.gate_summary && typeof item.gate_summary === "object" ? item.gate_summary : {};
    const snapshot = this.normalizeFeatureSnapshot(item);
    const quote = snapshot.quote;
    const optionFlow = snapshot.optionFlow;
    const gex = snapshot.gex;
    const institutional = snapshot.institutional;
    const dataQuality = snapshot.dataQuality;
    const market = evidence.market || {};
    const quoteAvailable = this.featureIsAvailable(quote);
    const quotePrice = Number(this.firstValue(quote.price, quote.last_price, quote.current_price, quote.mark_price, market.price));
    const quoteAgeMs = Number(this.firstValue(quote.quote_age_ms, quote.age_ms, quote.latency_ms));
    const quoteSpreadBps = Number(this.firstValue(quote.spread_bps, quote.bid_ask_spread_bps));
    const stableQuoteCheckKeys = ["price_available", "ticker_fresh", "quote_fresh", "spread_acceptable", "quote_sane", "not_halted"];
    const stableQuoteCheckValues = stableQuoteCheckKeys.filter((key) => Object.prototype.hasOwnProperty.call(stableGate.checks || {}, key)).map((key) => stableGate.checks[key]);
    const stableQuotePassed = stableQuoteCheckValues.some((value) => value === false) ? false : stableQuoteCheckValues.length ? stableQuoteCheckValues.every((value) => value === true) : undefined;
    const quotePassed = this.firstValue(quote.passed, quote.quality_passed, stableQuotePassed, evidence.market_quality?.passed);
    const quoteStale = quote.stale === true || quotePassed === false || (Number.isFinite(quoteAgeMs) && quoteAgeMs > Number(this.state.config?.maximum_market_age_seconds || 120) * 1000);
    const quoteTone = quoteStale ? "blocked" : quotePassed === true ? "passed" : quoteAvailable || Number.isFinite(quotePrice) ? "neutral" : "missing";
    const quoteLabel = quoteStale ? "行情未通过" : quoteTone === "passed" ? "行情已通过" : quoteTone === "neutral" ? "有数据 · 待评估" : "无 Quote 数据";
    const optionAvailable = this.featureIsAvailable(optionFlow);
    const optionScore = Number(this.firstValue(optionFlow.score, optionFlow.directional_score, optionFlow.flow_score));
    const optionBias = String(this.firstValue(optionFlow.bias, optionFlow.direction, optionFlow.signal, "neutral")).toLowerCase();
    const optionTone = optionFlow.hard_conflict === true || optionFlow.conflicts_direction === true ? "blocked" : optionFlow.confirms_direction === true ? "passed" : optionAvailable ? "neutral" : "missing";
    const optionLabel = optionTone === "blocked" ? "方向冲突" : optionTone === "passed" ? "同向确认" : optionAvailable ? ({ bull: "偏多", long: "偏多", bear: "偏空", short: "偏空" })[optionBias] || "中性观察" : "尚未接入";
    const gexAvailable = this.featureIsAvailable(gex);
    const gexScore = Number(this.firstValue(gex.score, gex.directional_score));
    const gexRegime = String(this.firstValue(gex.regime, gex.gamma_regime, gex.bias, "")).toLowerCase();
    const gexConflict = gex.conflicts_direction === true || gex.hard_conflict === true;
    const gexTone = gexConflict ? "blocked" : gex.confirms_direction === true ? "passed" : gexAvailable ? "neutral" : "missing";
    const gexLabel = gexConflict ? "关键位冲突" : gexRegime.includes("negative") ? "负 Gamma" : gexRegime.includes("positive") ? "正 Gamma" : gexAvailable ? "关键位已载入" : "尚未接入";
    const institutionAvailable = this.featureIsAvailable(institutional);
    const institutionScore = Number(this.firstValue(institutional.score, institutional.confirmation_score));
    const institutionTone = institutional.hard_conflict === true || institutional.conflicts_direction === true ? "blocked" : institutional.confirms_direction === true ? "passed" : institutionAvailable ? "neutral" : "missing";
    const institutionLabel = institutionTone === "blocked" ? "机构流冲突" : institutionTone === "passed" ? "场内确认" : institutionAvailable ? "机构流观察" : "尚未接入";
    const eventGate = item?.event_gate || evidence.event_gate || {};
    const riskEvents = snapshot.riskEvents;
    const primaryEvent = riskEvents[0] || eventGate.next_event || null;
    const stableEventClear = stableGate.checks && Object.prototype.hasOwnProperty.call(stableGate.checks, "event_window_clear") ? stableGate.checks.event_window_clear : null;
    const eventBlocked = stableEventClear === false || eventGate.blocked === true || riskEvents.some((event) => event?.blocked === true || ["critical", "blocked"].includes(String(event?.severity || event?.risk_level || "").toLowerCase()));
    const eventWarning = eventBlocked || riskEvents.some((event) => ["high", "medium", "warning"].includes(String(event?.severity || event?.risk_level || "").toLowerCase()));
    const eventAvailable = stableEventClear != null || Object.keys(eventGate).length > 0 || riskEvents.length > 0;
    const eventTone = eventBlocked ? "blocked" : eventWarning ? "warning" : eventAvailable ? "passed" : "missing";
    const eventLabel = eventBlocked ? "事件阻断" : eventWarning ? "事件预警" : eventAvailable ? "事件窗口安全" : "事件数据缺失";
    const moduleAvailability = [quoteAvailable, optionAvailable, gexAvailable, institutionAvailable, eventAvailable];
    const coverageRaw = Number(this.firstValue(dataQuality.coverage_ratio, dataQuality.coverage, dataQuality.score, item?.data_coverage));
    const coverage = Number.isFinite(coverageRaw)
      ? Math.max(0, Math.min(100, coverageRaw <= 1 ? coverageRaw * 100 : coverageRaw))
      : moduleAvailability.filter(Boolean).length / moduleAvailability.length * 100;
    const qualityState = String(this.firstValue(dataQuality.status, dataQuality.state, dataQuality.data_status, "")).toLowerCase();
    const qualityPassed = dataQuality.passed === true || ["passed", "live"].includes(qualityState);
    const qualityBlocked = dataQuality.passed === false || ["error", "failed", "blocked", "invalid"].includes(qualityState);
    const coverageTone = qualityBlocked ? "blocked" : qualityPassed && coverage >= 80 ? "passed" : coverage > 0 ? "warning" : "missing";
    const requiredMissing = Array.isArray(dataQuality.missing_required) ? dataQuality.missing_required : Array.isArray(dataQuality.missing) ? dataQuality.missing : [];
    const eventTime = primaryEvent?.scheduled_at || primaryEvent?.event_time || primaryEvent?.starts_at;
    return `<section class="opportunity-feature-grid" data-patch-key="enhanced-features" aria-label="行情、资金与风险增强特征">
      <article class="${quoteTone}"><header><span>QUOTE QUALITY</span><b>${this.escape(quoteLabel)}</b></header><strong data-live-field="quote-spread" data-live-value="${Number.isFinite(quoteSpreadBps) ? quoteSpreadBps : ""}">${Number.isFinite(quoteSpreadBps) ? `${quoteSpreadBps.toFixed(1)} bps` : "--"}</strong><small>${Number.isFinite(quoteAgeMs) ? `延迟 ${Math.round(quoteAgeMs)}ms` : "时效 --"} · ${this.escape(this.firstValue(quote.market_time, quote.session, "时段未知"))}</small></article>
      <article class="${optionTone}"><header><span>OPTION FLOW</span><b>${this.escape(optionLabel)}</b></header><strong>${Number.isFinite(optionScore) ? optionScore.toFixed(1) : "--"}</strong><small>${optionFlow.persistence != null && Number.isFinite(Number(optionFlow.persistence)) ? `持续 ${Number(optionFlow.persistence).toFixed(0)}%` : "持续性 --"} · ${optionFlow.acceleration != null && Number.isFinite(Number(optionFlow.acceleration)) ? `加速 ${this.signedMetric(optionFlow.acceleration, 1)}` : "加速 --"}</small></article>
      <article class="${gexTone}"><header><span>GEX REGIME</span><b>${this.escape(gexLabel)}</b></header><strong>${Number.isFinite(gexScore) ? gexScore.toFixed(1) : this.firstValue(gex.gamma_flip, gex.flip_price) != null ? this.escape(this.compactNumber(this.firstValue(gex.gamma_flip, gex.flip_price))) : "--"}</strong><small>Call ${this.firstValue(gex.call_wall, gex.call_wall_price) != null ? this.escape(this.compactNumber(this.firstValue(gex.call_wall, gex.call_wall_price))) : "--"} · Put ${this.firstValue(gex.put_wall, gex.put_wall_price) != null ? this.escape(this.compactNumber(this.firstValue(gex.put_wall, gex.put_wall_price))) : "--"}</small></article>
      <article class="${institutionTone}"><header><span>LIT / OFF-LIT</span><b>${this.escape(institutionLabel)}</b></header><strong>${Number.isFinite(institutionScore) ? institutionScore.toFixed(1) : institutional.offlit_adv_ratio != null && Number.isFinite(Number(institutional.offlit_adv_ratio)) ? `${Number(institutional.offlit_adv_ratio).toFixed(2)}×` : "--"}</strong><small>场外 ${this.compactMetric(this.firstValue(institutional.offlit_notional, institutional.off_lit_notional))} · 场内 ${this.compactMetric(this.firstValue(institutional.lit_notional, institutional.lit_value))}</small></article>
      <article class="${eventTone}"><header><span>EVENT GATE</span><b>${this.escape(eventLabel)}</b></header><strong>${primaryEvent ? this.escape(primaryEvent.event_type || primaryEvent.type || "宏观") : "--"}</strong><small>${primaryEvent ? `${eventTime ? this.formatDate(eventTime) : "时间待定"} · ${this.escape(primaryEvent.title || primaryEvent.name || "事件")}` : "未保存事件窗口快照"}</small></article>
      <article class="${coverageTone}"><header><span>DATA COVERAGE</span><b>${coverageTone === "blocked" ? "关键数据缺失" : coverageTone === "passed" ? "覆盖已通过" : coverage > 0 ? "覆盖待评估" : "无覆盖数据"}</b></header><strong>${coverage.toFixed(0)}%</strong><small>${requiredMissing.length ? `缺 ${requiredMissing.slice(0, 2).map((value) => this.escape(value)).join("、")}` : `Quote/Flow/GEX/Lit/Event ${moduleAvailability.filter(Boolean).length}/5`}</small></article>
    </section>`;
  }

  patchOpportunityCards(target, markup) {
    const template = document.createElement("template");
    template.innerHTML = markup;
    const incoming = [...template.content.children];
    const currentCards = new Map(
      [...target.children]
        .filter((node) => node.matches?.(".opportunity-item[data-opportunity-card]"))
        .map((node) => [node.dataset.opportunityCard, node]),
    );
    const activeElement = this.shadowRoot.activeElement;
    const existingCards = [...target.querySelectorAll(":scope > .opportunity-item[data-opportunity-card]")];
    const anchorCard = existingCards
      .map((node) => ({ node, distance: Math.abs(node.getBoundingClientRect().top - 8) }))
      .sort((left, right) => left.distance - right.distance)[0]?.node || null;
    const anchorKey = anchorCard?.dataset.opportunityCard || "";
    const anchorTop = anchorCard?.getBoundingClientRect().top;

    const syncAttributes = (current, next) => {
      [...current.attributes].forEach((attribute) => {
        if (!next.hasAttribute(attribute.name)) current.removeAttribute(attribute.name);
      });
      [...next.attributes].forEach((attribute) => current.setAttribute(attribute.name, attribute.value));
    };

    const patchKeyedSections = (currentCard, nextCard) => {
      if (currentCard.dataset.layoutState !== nextCard.dataset.layoutState) return false;
      const currentSections = new Map([...currentCard.children].filter((node) => node.dataset?.patchKey).map((node) => [node.dataset.patchKey, node]));
      const nextSections = new Map([...nextCard.children].filter((node) => node.dataset?.patchKey).map((node) => [node.dataset.patchKey, node]));
      if (currentSections.size !== nextSections.size || [...nextSections.keys()].some((key) => !currentSections.has(key))) return false;
      syncAttributes(currentCard, nextCard);
      nextSections.forEach((nextSection, key) => {
        const currentSection = currentSections.get(key);
        if (activeElement && currentSection.contains(activeElement)) return;
        const changed = currentSection.className !== nextSection.className || currentSection.innerHTML !== nextSection.innerHTML;
        if (!changed) return;
        if (key === "header" && currentSection.querySelector("[data-toggle-opportunity-details]")) {
          // 行情流会频繁改变评分。只更新评分按钮，保留头部交互按钮的 DOM 身份，
          // 否则按钮会在 pointerdown/click 之间被替换，表现为偶发“点击无反应”。
          syncAttributes(currentSection, nextSection);
          ["[data-order-book]", "[data-market-flow-trend]", "[data-score-trend]"].forEach((selector) => {
            const currentScore = currentSection.querySelector(selector);
            const nextScore = nextSection.querySelector(selector);
            if (!currentScore || !nextScore) return;
            syncAttributes(currentScore, nextScore);
            currentScore.replaceChildren(...nextScore.childNodes);
          });
          return;
        }
        syncAttributes(currentSection, nextSection);
        currentSection.replaceChildren(...nextSection.childNodes);
        currentSection.classList.remove("data-updated");
        void currentSection.offsetWidth;
        currentSection.classList.add("data-updated");
        window.setTimeout(() => currentSection.isConnected && currentSection.classList.remove("data-updated"), 700);
      });
      return true;
    };

    [...target.children]
      .filter((node) => !node.matches?.(".opportunity-item[data-opportunity-card]"))
      .forEach((node) => node.remove());

    incoming.forEach((nextCard) => {
      const key = nextCard.dataset.opportunityCard;
      const currentCard = currentCards.get(key);
      if (!currentCard) {
        target.appendChild(nextCard);
        return;
      }
      currentCards.delete(key);
      const hasFocus = activeElement && currentCard.contains(activeElement);
      if (!hasFocus && (currentCard.className !== nextCard.className || currentCard.innerHTML !== nextCard.innerHTML)) {
        if (!patchKeyedSections(currentCard, nextCard)) {
          syncAttributes(currentCard, nextCard);
          currentCard.replaceChildren(...nextCard.childNodes);
        }
      }
      target.appendChild(currentCard);
    });
    currentCards.forEach((node) => node.remove());
    if (anchorKey && Number.isFinite(anchorTop)) {
      window.requestAnimationFrame(() => {
        const nextAnchor = target.querySelector(`.opportunity-item[data-opportunity-card="${CSS.escape(anchorKey)}"]`);
        if (!nextAnchor) return;
        const delta = nextAnchor.getBoundingClientRect().top - anchorTop;
        if (Math.abs(delta) > 1) window.scrollBy({ top: delta, left: 0, behavior: "auto" });
      });
    }
  }

  opportunityPaginationMarkup() {
    const pagination = this.state.opportunityPagination || {};
    const page = Math.max(1, Number(pagination.page) || 1);
    const pageSize = Math.max(1, Number(pagination.page_size) || this.state.opportunityPageSize);
    const total = Math.max(0, Number(pagination.total) || 0);
    const totalPages = Math.max(1, Number(pagination.total_pages) || Math.ceil(total / pageSize) || 1);
    if (totalPages <= 1 && total <= pageSize) return "";
    const entries = [];
    const values = totalPages <= 7
      ? Array.from({ length: totalPages }, (_unused, index) => index + 1)
      : [...new Set([1, page - 1, page, page + 1, totalPages].filter((value) => value >= 1 && value <= totalPages))].sort((left, right) => left - right);
    values.forEach((value, index) => {
      if (index && value - values[index - 1] > 1) entries.push("ellipsis");
      entries.push(value);
    });
    return `<nav class="prediction-pagination opportunity-pagination" aria-label="机会列表分页">
      <span>共 <strong>${this.number(total)}</strong> 条 · 每页 ${this.number(pageSize)} 条</span>
      <div>
        <button type="button" data-opportunity-page="${page - 1}" ${page <= 1 ? "disabled" : ""}>上一页</button>
        ${entries.map((entry) => entry === "ellipsis" ? '<i aria-hidden="true">…</i>' : `<button type="button" class="${entry === page ? "active" : ""}" data-opportunity-page="${entry}" ${entry === page ? 'aria-current="page" disabled' : ""}>${entry}</button>`).join("")}
        <button type="button" data-opportunity-page="${page + 1}" ${page >= totalPages ? "disabled" : ""}>下一页</button>
      </div>
      <small>第 ${this.number(page)} / ${this.number(totalPages)} 页</small>
    </nav>`;
  }

  renderOpportunities() {
    const target = this.q("#opportunity-list");
    const historicalTab = this.state.opportunityTab === "history";
    const statusFilter = this.state.opportunityStatusFilter;
    const visibleOpportunities = historicalTab || statusFilter === "all"
      ? this.state.opportunities
      : this.state.opportunities.filter((item) => this.virtualEntryState(item, this.virtualEntryGate(item)).tone === statusFilter);
    if (!visibleOpportunities.length) {
      const statusLabel = ({ candidate: "待评估", ready: "可触发", triggered: "已触发", blocked: "数据阻断", data_error: "异常" })[statusFilter];
      const emptyMarkup = historicalTab
        ? '<div class="empty-state opportunity-empty"><strong>暂无历史机会</strong><span>信号过期或结束后会自动归入这里。</span></div>'
        : statusFilter !== "all"
        ? `<div class="empty-state opportunity-empty"><strong>当前没有“${statusLabel}”机会</strong><span>可切换其他状态，或等待下一轮机会扫描。</span></div>`
        : '<div class="empty-state opportunity-empty"><strong>尚未发现当前有效的美股候选</strong><span>系统会按新闻回看范围、置信度和关联股票继续扫描。</span></div>';
      const emptyWithPagination = emptyMarkup + this.opportunityPaginationMarkup();
      if (target.innerHTML !== emptyWithPagination) target.innerHTML = emptyWithPagination;
      return;
    }
    const waitingSettlementCount = visibleOpportunities.filter((item) => item.prediction_status === "pending").length;
    const unavailableSettlementCount = visibleOpportunities.filter((item) => item.prediction_status === "unavailable").length;
    const settlementGuide = historicalTab ? `<aside class="settlement-guide" aria-label="历史机会退出生命周期说明">
      <div><span>EXIT LIFECYCLE</span><strong>“监控退出条件”不是等待买入</strong><p>预测已触发；后台持续检查 15 分钟 K 线的止盈、止损以及评分转弱/反转，任一条件先满足即退出。最大持有时间只是强制退出上限。</p></div>
      <b>${this.number(waitingSettlementCount + unavailableSettlementCount)}<small>未完成：待结 ${this.number(waitingSettlementCount)} · 行情不足 ${this.number(unavailableSettlementCount)}</small></b>
      <ul><li>后台每 20 秒扫描</li><li>价格 / 评分条件优先</li><li>到上限强制退出</li><li>缺行情每 5 分钟重试</li></ul>
    </aside>` : "";
    const markup = settlementGuide + visibleOpportunities.map((item) => {
      const expanded = this.state.expandedOpportunityIds.has(String(item.id));
      const evidence = item.evidence || {};
      const indicatorItems = evidence.indicators || [];
      const indicatorPolicy = evidence.indicator_policy || {};
      const matchedCount = Number(evidence.matched_indicator_count ?? indicatorItems.filter((indicator) => indicator.matched).length);
      const requiredCount = Number(evidence.required_indicator_count ?? indicatorItems.length);
      const availableCount = Number(evidence.available_indicator_count ?? indicatorItems.filter((indicator) => indicator.available !== false).length);
      const passedGroupCount = Array.isArray(indicatorPolicy.passed_groups) ? indicatorPolicy.passed_groups.length : 0;
      const indicators = indicatorItems.slice(0, 6).map((indicator) => `<span class="evidence-chip ${indicator.available === false ? "unavailable" : indicator.matched ? "matched" : "unmatched"}">${indicator.available === false ? "--" : indicator.matched ? "✓" : "×"} ${this.escape(indicator.name)} <b>${indicator.available === false ? "不可用" : Number(indicator.strength ?? 0).toFixed(0)}</b></span>`).join("");
      const indicatorRemainder = indicatorItems.length > 6 ? `<span class="evidence-chip remainder">另有 ${indicatorItems.length - 6} 项</span>` : "";
      const news = (evidence.news || []).slice(0, 2).map((entry) => `<li><time>${this.formatUnix(entry.ts)}</time><span>${this.escape(entry.title)}</span><b>${Math.round(Number(entry.score || 0) * 100)}%</b></li>`).join("");
      const market = evidence.market || {};
      const priceComparison = item.price_comparison || {};
      const priceSources = priceComparison.sources || {};
      const binanceQuote = priceSources.binance || item.binance_contract_quote || {};
      const finnhubSpot = priceSources.finnhub || item.finnhub_spot_quote || {};
      const unusualWhalesQuote = priceSources.unusual_whales || {};
      const marketEnvironment = evidence.market_environment || evidence.score_snapshot?.macro_market || {};
      const macroSnapshot = evidence.macro_market_snapshot || {};
      const macroIndices = Object.fromEntries((macroSnapshot.indices || []).map((entry) => [entry.key, entry]));
      const macroSectors = Object.fromEntries((macroSnapshot.sectors || []).map((entry) => [entry.key, entry]));
      const macroNdx = macroIndices.NDX || {};
      const macroSector = macroSectors[marketEnvironment.sector_key] || {};
      const macroAdjustment = Number(marketEnvironment.adjustment || 0);
      const macroResonance = marketEnvironment.resonance || "unknown";
      const signalSession = marketEnvironment.market_session || macroSnapshot.market_session || {};
      const signalTide = marketEnvironment.market_tide || macroSnapshot.market_tide || {};
      const signalTideLabel = signalTide.bias === "bull" ? "资金潮偏多" : signalTide.bias === "bear" ? "资金潮偏空" : "资金潮中性";
      const macroFactors = (marketEnvironment.factors || []).slice(0, 3).map((factor) => `${factor.label} ${Number(factor.points) >= 0 ? "+" : ""}${Number(factor.points).toFixed(0)}`).join(" · ");
      const macroReference = marketEnvironment.available ? `<section class="opportunity-macro ${this.escape(macroResonance)}" data-patch-key="macro-context" aria-label="大盘环境参考">
        <span class="macro-resonance"><i>${macroResonance === "resonant" ? "✓" : macroResonance === "divergent" ? "⚠" : "•"}</i><b>${this.escape(marketEnvironment.resonance_label || "大盘中性")}</b><small>信号时：${this.escape(signalSession.label || "时段未知")} · 大盘环境</small></span>
        <span><em>纳指 100</em><b class="${Number(macroNdx.change_percent) >= 0 ? "positive" : "negative"}">${Number.isFinite(Number(macroNdx.change_percent)) ? `${Number(macroNdx.change_percent) >= 0 ? "+" : ""}${Number(macroNdx.change_percent).toFixed(2)}%` : "--"}</b><small>RSI ${marketEnvironment.market_rsi == null ? "--" : Number(marketEnvironment.market_rsi).toFixed(1)} · ${this.escape(macroNdx.provider_symbol || "QQQ")} ${macroNdx.proxy ? "代理" : "指数"}</small></span>
        <span><em>VIX / 板块</em><b>VIX ${marketEnvironment.vix == null ? "--" : Number(marketEnvironment.vix).toFixed(1)}</b><small>${this.escape(marketEnvironment.sector_label || "大盘")} ${Number.isFinite(Number(macroSector.change_percent)) ? `${Number(macroSector.change_percent) >= 0 ? "+" : ""}${Number(macroSector.change_percent).toFixed(2)}%` : "--"} · ${this.escape(signalTide.available ? signalTideLabel : "资金潮缺失")}</small></span>
        <span class="macro-adjustment ${macroAdjustment > 0 ? "positive" : macroAdjustment < 0 ? "negative" : "flat"}"><em>评分调整</em><b>${macroAdjustment > 0 ? "+" : ""}${macroAdjustment.toFixed(1)} 分</b><small>${this.escape(macroFactors || "当前无宏观加减分")}</small></span>
      </section>` : `<section class="opportunity-macro unavailable" data-patch-key="macro-context" aria-label="大盘环境参考"><span class="macro-resonance"><i>--</i><b>大盘数据不足</b><small>该条信号未应用宏观调整</small></span></section>`;
      const confirmed = item.lifecycle_status === "confirmed" || item.status === "discovered" || evidence.confirmed === true;
      const readiness = evidence.live_readiness || {};
      const shadowReady = readiness.status === "shadow_ready";
      const readinessBadge = `<span class="readiness-badge ${shadowReady ? "shadow" : "research"}">${shadowReady ? "影子候选" : "研究信号"}</span>`;
      const newsTrigger = evidence.news_trigger || {};
      const triggerAge = Number(newsTrigger.newest_news_age_minutes);
      const triggerBadge = newsTrigger.version
        ? `<span class="quality-badge ${newsTrigger.has_new_news ? "passed" : "blocked"}" title="触发窗口 ${Number(newsTrigger.trigger_window_hours || 4)} 小时 · AI 记忆 ${Number(newsTrigger.memory_window_hours || 168)} 小时">${newsTrigger.has_new_news ? `新事件 ${Number.isFinite(triggerAge) ? `${Math.max(0, Math.round(triggerAge))}m` : "已确认"}` : "无新事件"}</span>`
        : `<span class="quality-badge legacy" title="旧版信号未保存新闻触发快照">旧规则</span>`;
      const marketQuality = evidence.market_quality || {};
      const stableGateStatus = String(item?.gate_summary?.status || "").toLowerCase();
      const marketQualityBadge = stableGateStatus === "degraded"
        ? '<span class="quality-badge legacy">行情降级</span>'
        : stableGateStatus === "passed" || !stableGateStatus && marketQuality.passed === true
        ? '<span class="quality-badge passed">行情新鲜</span>'
        : stableGateStatus === "blocked" || !stableGateStatus && marketQuality.passed === false
        ? '<span class="quality-badge blocked" title="实时价格、已收盘 K 线或预测因子不符合准入要求">行情受限</span>'
        : '<span class="quality-badge legacy">行情无评估数据</span>';
      const marketQualityText = stableGateStatus === "passed"
        ? "行情质量通过"
        : stableGateStatus === "degraded"
        ? "行情降级可用"
        : stableGateStatus === "blocked"
        ? "行情质量未通过"
        : marketQuality.passed === true
        ? "行情质量通过"
        : marketQuality.passed === false
        ? "行情质量未通过"
        : "无行情质量评估数据";
      const historyState = item.prediction_status === "pending"
        ? "等待结算"
        : item.prediction_status === "unavailable"
        ? "行情不足"
        : item.status === "dismissed" ? "已结束" : "已过期";
      const marketAvailable = evidence.market_available !== false;
      const directionLabel = item.direction === "short" ? "做空" : "做多";
      const directionClass = item.direction === "short" ? "short" : "long";
      const signalStart = this.formatDate(item.prediction_created_at || item.discovered_at);
      const signalEnd = this.formatDate(item.expires_at);
      const signalDuration = this.formatDuration(item.prediction_created_at || item.discovered_at, item.expires_at);
      const scoreUpdatedAt = item.updated_at || evidence.score_snapshot?.calculated_at || item.discovered_at;
      const frozenCombinedScore = item.prediction_combined_score;
      const scoreDelta = frozenCombinedScore == null
        ? ""
        : ` · 入场 ${Number(frozenCombinedScore).toFixed(1)}`;
      const scoreHistory = this.opportunityScoreHistory(item);
      const scoreTrend = this.scoreTrendState(scoreHistory);
      const marketFlowTrend = this.marketFlowTrendState(scoreHistory);
      const currentFlow = { ...(evidence.market_flow || {}), ...(item.flow || {}) };
      const displayedMarketFlowScore = Number(this.firstValue(marketFlowTrend.latest?.market_flow, item.score_components?.market_flow, currentFlow.score));
      const marketFlowBidDepth = this.firstValue(marketFlowTrend.latest?.bid_depth_notional, currentFlow.bid_depth_notional);
      const marketFlowAskDepth = this.firstValue(marketFlowTrend.latest?.ask_depth_notional, currentFlow.ask_depth_notional);
      const marketFlowDepthLabel = Number.isFinite(Number(marketFlowBidDepth)) || Number.isFinite(Number(marketFlowAskDepth))
        ? `买 ${Number.isFinite(Number(marketFlowBidDepth)) ? this.compactNumber(Number(marketFlowBidDepth)) : "--"} · 卖 ${Number.isFinite(Number(marketFlowAskDepth)) ? this.compactNumber(Number(marketFlowAskDepth)) : "--"}`
        : "等待资金量快照";
      const marketFlowControl = `<button class="market-flow-score ${marketFlowTrend.direction}" type="button" data-market-flow-trend="${this.escape(item.id)}" title="查看 ${this.escape(item.symbol)} 资金盘口变化走势"><span>资金盘口</span><strong><i>${marketFlowTrend.arrow}</i>${Number.isFinite(displayedMarketFlowScore) ? displayedMarketFlowScore.toFixed(1) : "--"}</strong><small>${this.escape(marketFlowDepthLabel)}</small><em>${marketFlowTrend.badge}</em></button>`;
      const liveBookRatio = Number(marketFlowAskDepth) > 0 && Number.isFinite(Number(marketFlowBidDepth)) ? Number(marketFlowBidDepth) / Number(marketFlowAskDepth) : null;
      const orderBookControl = marketAvailable ? `<button class="order-book-trigger" type="button" data-order-book="${this.escape(item.id)}" title="查看 ${this.escape(item.contract_symbol)} Binance 实时买卖各100档"><span>盘口100档</span><small>${liveBookRatio == null ? "实时买卖梯形表" : `买卖比 ${liveBookRatio.toFixed(2)}`}</small></button>` : "";
      const displayedCombinedScore = Number(this.firstValue(item.combined_score, item.score_components?.combined));
      const displayedNewsScore = Number(this.firstValue(item.news_score, item.score_components?.news));
      const displayedIndicatorScore = Number(this.firstValue(item.indicator_score, item.score_components?.technical));
      const entryGate = this.virtualEntryGate(item);
      const entryState = this.virtualEntryState(item, entryGate);
      const manualFollowControl = !historicalTab && ["long", "short"].includes(String(item.direction))
        ? `<button class="manual-follow-trigger" type="button" data-manual-follow="${this.escape(item.id)}" ${this.state.manualFollowLoading ? "disabled" : ""} title="按当前${item.direction === "short" ? "做空" : "做多"}方向提交一次真实 Binance 合约订单；待评估机会将由人工确认覆盖研究准入门槛">${this.state.manualFollowLoading && this.state.manualFollowOpportunityId === String(item.id) ? "提交中…" : "立即跟买"}</button>`
        : "";
      const signalGateChecks = (entryGate.checks || []).filter((check) => check.key !== "entry_price");
      const passedGateCount = signalGateChecks.filter((check) => check.passed).length;
      const entryGateChecks = signalGateChecks.map((check) => {
        const current = check.source === "stable_gate_summary"
          ? check.current == null ? "无数据" : check.passed ? "通过" : "未通过"
          : check.key === "indicator_policy" || check.key === "new_news_trigger" || check.key === "market_quality"
          ? check.passed ? "通过" : "未通过"
          : check.key === "market_flow_conflict"
          ? check.passed ? "无冲突" : "有冲突"
          : check.current == null
          ? "--"
          : Number.isFinite(Number(check.current))
          ? Number(check.current).toFixed(check.key === "entry_price" ? 2 : 1)
          : String(check.current);
        const required = typeof check.required === "number" ? check.required.toFixed(1) : String(check.required ?? "");
        return `<span class="virtual-entry-check ${check.passed ? "passed" : check.current == null ? "missing" : "blocked"}" title="${this.escape(check.detail || "")}"><i>${check.passed ? "✓" : check.current == null ? "--" : "×"}</i><em>${this.escape(check.label)}</em><b>${this.escape(current)}</b><small>${check.source === "stable_gate_summary" || ["market_flow_conflict", "indicator_policy", "new_news_trigger", "market_quality"].includes(check.key) ? "" : `门槛 ${this.escape(required)}`}</small></span>`;
      }).join("");
      const triggerPriceValue = Number(item.prediction_entry_price || 0);
      const liveReferencePrice = Number(binanceQuote.price ?? entryGate.reference_price ?? market.price ?? 0);
      const displayedEntryPrice = triggerPriceValue > 0 ? triggerPriceValue : liveReferencePrice;
      const entryPriceLabel = triggerPriceValue > 0 ? "冻结触发价格" : "当前参考价 · 未冻结";
      const entryTime = item.prediction_created_at || entryGate.checked_at;
      const gateScope = entryGate.frozen ? "触发时条件已冻结" : "当前扫描条件";
      const triggeredPosition = entryState.triggered && !historicalTab;
      const virtualEntryPanel = `<section class="virtual-entry-gate ${entryState.tone} ${triggeredPosition ? "position-active" : ""}" data-patch-key="entry-gate" aria-label="买入触发条件与状态">
        <div class="virtual-entry-state"><span>ENTRY GATE</span><strong>${this.escape(entryState.label)}</strong><small>${this.escape(entryState.detail)} · ${gateScope} · 真实订单关闭</small></div>
        <div class="virtual-entry-checks">${entryGateChecks}</div>
        ${triggeredPosition ? "" : `<div class="virtual-entry-price"><span>${entryPriceLabel}</span><b>${displayedEntryPrice > 0 ? this.escape(this.compactNumber(displayedEntryPrice)) : "--"}</b><small>${entryState.triggered ? `触发 ${this.formatDate(entryTime)}` : `检查 ${this.formatDate(entryTime)}`}</small></div>`}
      </section>`;
      const position = this.virtualPositionSnapshot(item);
      const rawRiskPlan = position.risk_plan || {};
      const riskPlan = {
        ...rawRiskPlan,
        stop_loss_price: this.firstValue(rawRiskPlan.stop_loss_price, rawRiskPlan.stop_price, rawRiskPlan.stop_loss),
        stop_loss_pct: this.firstValue(rawRiskPlan.stop_loss_pct, rawRiskPlan.risk_pct, rawRiskPlan.stop_pct),
        take_profit_price: this.firstValue(rawRiskPlan.take_profit_price, rawRiskPlan.target_price, rawRiskPlan.take_profit),
        take_profit_pct: this.firstValue(rawRiskPlan.take_profit_pct, rawRiskPlan.reward_pct, rawRiskPlan.target_pct),
        risk_reward_ratio: this.firstValue(rawRiskPlan.risk_reward_ratio, rawRiskPlan.rr_ratio, 2),
      };
      const positionTone = position.profit_state || "flat";
      const markLabel = position.valuation_state === "settled"
        ? "实际退出价"
        : position.market_stale
        ? "最近价格 · 行情延迟"
        : "实时价格";
      const targetStateLabel = position.target_state === "take_profit_reached"
        ? position.valuation_state === "settled" ? "已按止盈结算" : "越过参考止盈·待K线确认"
        : position.target_state === "stop_loss_reached"
        ? position.valuation_state === "settled" ? "已按止损结算" : "越过参考止损·待K线确认"
        : position.valuation_state === "settled"
        ? "预测已结算"
        : "持仓观察中";
      const positionPanel = triggeredPosition ? `<section class="virtual-position ${positionTone}" data-patch-key="position" aria-label="持仓实时盈亏">
        <div class="virtual-position-title"><span>POSITION · ${this.state.displayLeverage}X DISPLAY</span><strong>${directionLabel} · ${targetStateLabel}</strong><small>触发 ${this.formatDate(entryTime)} · 有效至 ${signalEnd} · 不会发送真实订单</small></div>
        <div class="virtual-position-metrics">
          <span><em>冻结入场价</em><b>${position.entry_price > 0 ? this.escape(this.compactNumber(position.entry_price)) : "--"}</b><small>触发时价格，不随行情变化</small></span>
          <span class="live-mark"><em>${markLabel}</em><b>${position.current_price > 0 ? this.escape(this.compactNumber(position.current_price)) : "--"}</b><small>${position.market_at ? this.formatDate(position.market_at) : "等待最新行情"}</small></span>
          <span class="position-pnl ${positionTone}"><em>当前 ${this.state.displayLeverage}x 仓位ROE</em><b>${position.available ? this.formatLeveragedReturnFromPercent(position.net_return_pct) : "--"}</b><small>${position.available ? `标的净收益 ${this.signedMetric(position.net_return_pct, 2, "%")} · 保证金口径，不代表账户总收益` : "暂无可用实时行情"}</small></span>
          <span class="risk-stop"><em>参考止损价</em><b>${riskPlan.stop_loss_price > 0 ? this.escape(this.compactNumber(riskPlan.stop_loss_price)) : "--"}</b><small>风险 ${riskPlan.stop_loss_pct == null ? "--" : `-${Number(riskPlan.stop_loss_pct).toFixed(2)}%`}</small></span>
          <span class="risk-target"><em>参考止盈价</em><b>${riskPlan.take_profit_price > 0 ? this.escape(this.compactNumber(riskPlan.take_profit_price)) : "--"}</b><small>目标 ${riskPlan.take_profit_pct == null ? "--" : `+${Number(riskPlan.take_profit_pct).toFixed(2)}%`}</small></span>
          <span><em>风险收益比</em><b>1 : ${Number(riskPlan.risk_reward_ratio || 2).toFixed(1)}</b><small>${riskPlan.method === "atr14_x_1_5" ? "ATR(14) 波动率冻结" : "按周期默认风险"} · 仅观察线</small></span>
        </div>
      </section>` : "";
      const waitingDetail = entryState.tone === "ready"
        ? "条件已满足，等待预测写入"
        : `${signalGateChecks.length - passedGateCount} 项条件未满足 · 尚未入场`;
      const signalSummaryPanel = `<div class="opportunity-signal ${historicalTab ? "historical-signal" : "candidate-signal"}" data-patch-key="signal-summary" aria-label="${historicalTab ? "历史信号" : "待触发候选"}信息">
        <span><em>信号时间</em><b>${signalStart}</b></span>
        <span class="validity"><em>信号有效期间</em><b>${signalDuration}</b><small>${signalStart} — ${signalEnd}</small></span>
        <span><em>${historicalTab ? "历史方向" : "候选方向"}</em><b class="signal-${directionClass}">${directionLabel}</b><small>${historicalTab ? "按该方向统计结果" : "仅为研判方向，尚未买入"}</small></span>
        <span class="trigger-progress ${entryState.tone}"><em>${historicalTab ? "触发结果" : "触发进度"}</em><b>${passedGateCount} / ${signalGateChecks.length}</b><small>${historicalTab ? entryState.triggered ? "已生成预测" : "未生成预测" : waitingDetail}</small></span>
      </div>`;
      const outcome = historicalTab ? item.outcome : null;
      const predictionStatus = String(item.prediction_status || "");
      const settlementState = this.predictionSettlementState(item);
      const outcomeResult = outcome?.result
        || (predictionStatus === "pending" ? "pending" : predictionStatus === "unavailable" ? "unavailable" : "not_created");
      const outcomeLabel = outcome
        ? this.analyticsResultLabel(outcomeResult)
        : outcomeResult === "pending"
        ? "监控退出条件"
        : outcomeResult === "unavailable"
        ? "行情不足"
        : confirmed
        ? "未生成预测"
        : "技术未确认";
      const outcomeReturn = outcome?.directional_return_bps != null
        ? `${this.state.displayLeverage}x 净收益 ${this.formatLeveragedReturnFromBps(outcome.directional_return_bps)} · 标的 ${this.formatBps(outcome.directional_return_bps)}`
        : outcomeResult === "pending"
        ? `最大持有上限 ${this.formatDate(settlementState.dueAt)} · ${settlementState.label}`
        : outcomeResult === "unavailable"
        ? "退出行情宽限期内未取得行情"
        : confirmed
        ? "历史信号未关联预测记录"
        : `核心同向 ${Number(indicatorPolicy.core_matched_count ?? matchedCount)} 项 · 通过 ${passedGroupCount} 个策略组`;
      const outcomeMetric = historicalTab
        ? `<span class="history-result ${this.escape(outcomeResult)}"><em>${outcome ? "是否命中" : "预测状态"}</em><b>${this.escape(outcomeLabel)}</b><small>${this.escape(outcomeReturn)}</small></span>`
        : "";
      const settlementPanel = historicalTab && outcomeResult === "pending" ? `<section class="settlement-detail ${this.escape(settlementState.phase)}" data-patch-key="settlement" aria-label="退出生命周期详情">
        <div><span>SETTLEMENT STATUS</span><strong>${this.escape(settlementState.label)}</strong><small>${this.escape(settlementState.detail)}</small></div>
        <span><em>最大持有上限</em><b>${this.formatDate(settlementState.dueAt)}</b><small>due_at 仅是强制退出上限，价格或评分条件可提前退出</small></span>
        <span><em>下次预计处理</em><b>${settlementState.nextRetryAt ? this.formatDate(settlementState.nextRetryAt) : "后台下轮扫描"}</b><small>${settlementState.lastAttemptAt ? `最近尝试 ${this.formatDate(settlementState.lastAttemptAt)}` : `使用 ${this.escape(settlementState.priceTimeframe)} K 线监控退出`}</small></span>
        <span><em>行情补偿截止</em><b>${this.formatDate(settlementState.graceDeadline)}</b><small>达到持有上限后仍无退出行情，超过此时间则不计入统计</small></span>
      </section>` : "";
      const symbolControl = marketAvailable
        ? `<button class="opportunity-symbol" type="button" data-opportunity-id="${this.escape(item.id)}" data-open-contract="${this.escape(item.contract_symbol)}" data-timeframe="${this.escape(item.timeframe)}" title="打开 ${this.escape(item.symbol)} 的合约 K 线研究与预测模拟">${this.escape(item.symbol)}</button>`
        : `<button class="opportunity-symbol unavailable" type="button" disabled title="该股票暂无对应的合约技术行情">${this.escape(item.symbol)}</button>`;
      const conclusionControl = `<button class="ai-conclusion-trigger" type="button" data-ai-conclusion="${this.escape(item.id)}" title="查看 ${this.escape(item.symbol)} 的 AI 分析结论">AI分析结论</button>`;
      const detailControl = `<button class="opportunity-detail-toggle" type="button" data-toggle-opportunity-details="${this.escape(item.id)}" aria-expanded="${expanded}">${expanded ? "收起详情 <i>⌃</i>" : "展开详情 <i>⌄</i>"}</button>`;
      const providerQuoteBadge = (source, tone, liveLabel) => source?.available && Number(source.price) > 0
        ? `<span class="provider-quote-badge ${tone} ${source.fresh ? "live" : "stale"}" title="${this.escape(liveLabel)} · ${source.fresh ? "新鲜" : "延迟/休市"} · ${this.formatDate(source.observed_at)}"><i>${this.escape(source.label || "--")}</i><b>${this.escape(this.compactNumber(Number(source.price)))}</b><small>${source.fresh ? "实时" : "延迟"}</small></span>`
        : "";
      const binancePriceControl = providerQuoteBadge(binanceQuote, "binance", "Binance 映射合约执行参考价");
      const finnhubSpotControl = providerQuoteBadge(finnhubSpot, "finnhub", "Finnhub 美股现货参考价");
      const unusualWhalesControl = providerQuoteBadge(unusualWhalesQuote, "unusual-whales", "Unusual Whales 美股 NBBO/成交参考价");
      const finiteComparisonValue = (value) => value != null && value !== "" && Number.isFinite(Number(value))
        ? Number(value)
        : null;
      const basisBps = finiteComparisonValue(priceComparison.basis_bps);
      const snapshotGapBps = finiteComparisonValue(priceComparison.snapshot_gap_bps);
      const previousCloseGapBps = finiteComparisonValue(priceComparison.previous_close_gap_bps);
      const providerDivergenceBps = finiteComparisonValue(priceComparison.provider_divergence_bps);
      const liveBasisMode = priceComparison.comparable === true;
      const openingForecast = priceComparison.opening_forecast || {};
      const forecastDirection = String(openingForecast.direction || "neutral");
      const relatedNewsCount = Math.max(0, Number(openingForecast.related_news_count ?? openingForecast.news_count ?? item.news_ids?.length ?? evidence.news?.length ?? 0));
      const newNewsCount = Math.max(0, Number(openingForecast.new_news_count ?? newsTrigger.new_news_ids?.length ?? 0));
      const reusedNewsCount = Math.max(0, Number(openingForecast.reused_news_count ?? newsTrigger.reused_news_count ?? 0));
      const memoryWindowHours = Math.max(1, Number(openingForecast.memory_window_hours ?? newsTrigger.memory_window_hours ?? 168));
      const memoryWindowLabel = memoryWindowHours % 24 === 0 ? `${memoryWindowHours / 24} 天` : `${memoryWindowHours} 小时`;
      const displayedGapBps = liveBasisMode ? basisBps : snapshotGapBps ?? previousCloseGapBps;
      const displayedReferencePrice = liveBasisMode
        ? finiteComparisonValue(priceComparison.reference_price)
        : finiteComparisonValue(priceComparison.snapshot_reference_price) ?? finiteComparisonValue(priceComparison.previous_close_price);
      const basisState = String(priceComparison.state || "reference_unavailable");
      const basisStateLabel = basisState === "spread_watch"
        ? "价差观察"
        : basisState === "aligned"
        ? "价格接近"
        : basisState === "opening_gap_watch"
        ? "跨时段预判"
        : basisState === "execution_unavailable"
        ? "执行价缺失"
        : "参考价缺失";
      const livePairDirection = priceComparison.pair_direction === "short_binance_long_spot"
        ? "BN 偏高：空合约 / 多现货"
        : priceComparison.pair_direction === "long_binance_short_spot"
        ? "BN 偏低：多合约 / 空现货"
        : "价差在观察阈值内";
      const forecastLabel = openingForecast.label === "bearish_open"
        ? `偏空开盘 · ${Number(openingForecast.confidence || 0).toFixed(0)}分`
        : openingForecast.label === "bullish_open"
        ? `偏多开盘 · ${Number(openingForecast.confidence || 0).toFixed(0)}分`
        : openingForecast.available
        ? "中性观察"
        : "等待参考快照";
      const comparisonConclusion = liveBasisMode ? livePairDirection : forecastLabel;
      const gapLabel = liveBasisMode ? "BN / 实时现货基差" : "BN / 最近现货差";
      const referenceLabel = liveBasisMode ? "实时现货参考" : "最近现货快照";
      const divergenceSuffix = priceComparison.provider_divergence_mode === "snapshot" ? " · 快照" : "";
      const basisExplanation = liveBasisMode
        ? "实时多源报价仅用于基差观察；只有两端均可执行时才具备套利研究意义"
        : openingForecast.available
        ? `${newNewsCount > 0 ? `本轮新增 ${newNewsCount} 条` : "本轮没有新增新闻"} · 当前机会关联 ${relatedNewsCount} 条${reusedNewsCount > 0 ? `（沿用 ${reusedNewsCount} 条）` : ""} · ${memoryWindowLabel} AI 记忆用于新新闻回溯；${openingForecast.gap_aligned ? "价格与新闻方向共振" : "价格与新闻方向未共振"}。这是开盘概率预判，不是无风险套利`
        : "缺少可用现货快照或新闻方向，暂不生成开盘缺口预判";
      const basisPanel = `<section class="cross-venue-basis ${this.escape(basisState)} forecast-${this.escape(forecastDirection)}" data-patch-key="price-comparison" aria-label="跨市场价格比对与开盘预判">
        <strong><i>${liveBasisMode ? "LIVE BASIS" : "OPEN GAP"}</i>${this.escape(basisStateLabel)}</strong>
        <span><em>${gapLabel}</em><b>${displayedGapBps == null ? "--" : `${displayedGapBps >= 0 ? "+" : ""}${displayedGapBps.toFixed(1)} bps`}</b><small>${!liveBasisMode && previousCloseGapBps != null ? `BN / 昨收 ${previousCloseGapBps >= 0 ? "+" : ""}${previousCloseGapBps.toFixed(1)} bps` : ""}</small></span>
        <span><em>${referenceLabel}</em><b>${displayedReferencePrice == null ? "--" : this.escape(this.compactNumber(displayedReferencePrice))}</b><small>${!liveBasisMode && priceComparison.previous_close_price ? `昨收 ${this.escape(this.compactNumber(Number(priceComparison.previous_close_price)))}` : ""}</small></span>
        <span><em>FH / UW 分歧${divergenceSuffix}</em><b>${providerDivergenceBps == null ? "--" : `${providerDivergenceBps.toFixed(1)} bps`}</b></span>
        <span class="basis-direction"><em>${liveBasisMode ? "配对观察" : "开盘预判"}</em><b>${this.escape(comparisonConclusion)}</b></span>
        <small>${this.escape(basisExplanation)}</small>
      </section>`;
      return `<article class="opportunity-item ${this.escape(item.status)} state-${this.escape(entryState.tone)} ${expanded ? "is-expanded" : ""} ${historicalTab ? `historical outcome-${this.escape(outcomeResult)}` : ""}" data-opportunity-card="${this.escape(item.id)}" data-layout-state="${this.escape(entryState.tone)}:${this.escape(entryState.label)}:${historicalTab ? "history" : "current"}">
        <header data-patch-key="header"><div><span class="direction ${confirmed ? "confirmed" : "candidate"}">${confirmed ? "技术已确认" : "新闻候选"}</span><span class="lifecycle-badge ${this.escape(entryState.tone)}">${this.escape(entryState.label)}</span>${triggerBadge}${marketQualityBadge}${symbolControl}<small>${marketAvailable ? this.escape(item.contract_symbol) : "暂无技术行情"}</small>${binancePriceControl}${finnhubSpotControl}${unusualWhalesControl}${orderBookControl}${detailControl}${conclusionControl}</div>${marketFlowControl}<button class="opportunity-score ${scoreTrend.direction}" type="button" data-score-trend="${this.escape(item.id)}" title="查看 ${this.escape(item.symbol)} 评分变化走势"><span class="score-current"><i>${scoreTrend.arrow}</i><b data-live-field="combined-score" data-live-value="${Number.isFinite(displayedCombinedScore) ? displayedCombinedScore : ""}">${Number.isFinite(displayedCombinedScore) ? displayedCombinedScore.toFixed(1) : "无数据"}</b></span><span>当前组合评分${scoreDelta}</span><em>${scoreTrend.badge}</em></button></header>
        ${basisPanel}
        ${virtualEntryPanel}
        ${macroReference}
        ${this.opportunityFeatureMarkup(item)}
        <div class="opportunity-metrics ${historicalTab ? "with-result" : ""}" data-patch-key="core-metrics"><span><em>新闻评分</em><b>${Number.isFinite(displayedNewsScore) ? displayedNewsScore.toFixed(1) : "无数据"}</b><small>${newsTrigger.version ? `${Number(newsTrigger.new_news_ids?.length || 0)} 条新事件 · 记忆 ${Number(newsTrigger.memory_window_hours || 168)}h` : "旧版记录"}</small></span><span><em>指标强度</em><b>${Number.isFinite(displayedIndicatorScore) ? displayedIndicatorScore.toFixed(1) : "无数据"}</b><small>${matchedCount} 项同向 · ${availableCount}/${requiredCount} 可用</small></span><span><em>确认周期</em><b>${this.escape(item.timeframe)}</b><small>${this.escape(marketQualityText)}</small></span><span class="opportunity-signal-status"><em>信号状态</em><b>${this.escape(entryState.label)}</b><small>${this.escape(entryState.detail)}</small>${manualFollowControl}</span>${outcomeMetric}</div>
        ${positionPanel || signalSummaryPanel}
        ${settlementPanel}
        <div class="evidence-chips" data-patch-key="indicator-evidence">${indicators}${indicatorRemainder}</div>
        <ul class="opportunity-news" data-patch-key="news-evidence">${news}</ul>
        <footer data-patch-key="footer"><span>发现 ${this.formatDate(item.discovered_at)}</span><span>评分更新 ${this.formatDate(scoreUpdatedAt)}</span><span>有效至 ${this.formatDate(item.expires_at)}</span><em>${historicalTab ? `历史机会 · ${outcomeLabel}` : entryState.tone === "blocked" ? `已阻断 · ${this.escape(entryState.detail)}` : entryState.tone === "data_error" ? `数据异常 · ${this.escape(entryState.detail)}` : shadowReady ? "影子候选 · 仍不执行交易" : confirmed ? `研究预测 · ${(readiness.failed_reasons || ["未通过影子准入"]).slice(0, 1).join("")}` : item.status === "candidate" ? marketAvailable ? "等待策略组与评分确认" : "新闻候选 · 暂无技术行情" : "历史机会"}</em></footer>
      </article>`;
    }).join("");
    this.patchOpportunityCards(target, markup + this.opportunityPaginationMarkup());
  }

  openAiConclusion(opportunityId, trigger) {
    const item = this.state.opportunities.find((opportunity) => opportunity.id === opportunityId);
    if (!item) {
      this.showBanner("该机会的 AI 分析结论已更新，请刷新后重试。", "error");
      return;
    }
    const evidence = item.evidence || {};
    const newsItems = Array.isArray(evidence.news) ? evidence.news : [];
    const newsTrigger = evidence.news_trigger && typeof evidence.news_trigger === "object" ? evidence.news_trigger : {};
    const relatedNewsCount = Math.max(newsItems.length, Array.isArray(item.news_ids) ? item.news_ids.length : 0);
    const newNewsCount = Array.isArray(newsTrigger.new_news_ids) ? newsTrigger.new_news_ids.length : 0;
    const memoryWindowHours = Math.max(1, Number(newsTrigger.memory_window_hours || 168));
    const memoryWindowLabel = memoryWindowHours % 24 === 0 ? `${memoryWindowHours / 24} 天` : `${memoryWindowHours} 小时`;
    const indicatorItems = Array.isArray(evidence.indicators) ? evidence.indicators : [];
    const legacyMarketFlow = evidence.market_flow && typeof evidence.market_flow === "object" ? evidence.market_flow : {};
    const marketFlow = { ...legacyMarketFlow, ...(item?.flow && typeof item.flow === "object" ? item.flow : {}) };
    const indicatorPolicy = evidence.indicator_policy || {};
    const scoreWeightLabel = this.scoreWeightSummary(evidence);
    const enhancedSnapshot = this.normalizeFeatureSnapshot(item);
    const matchedCount = Number(evidence.matched_indicator_count ?? indicatorItems.filter((indicator) => indicator.matched).length);
    const requiredCount = Number(evidence.required_indicator_count ?? indicatorItems.length);
    const availableCount = Number(evidence.available_indicator_count ?? indicatorItems.filter((indicator) => indicator.available !== false).length);
    const coreMatchedCount = Number(indicatorPolicy.core_matched_count ?? matchedCount);
    const passedGroups = Array.isArray(indicatorPolicy.passed_groups) ? indicatorPolicy.passed_groups : [];
    const entryGate = this.virtualEntryGate(item);
    const entryState = this.virtualEntryState(item, entryGate);
    const confirmed = item.lifecycle_status === "confirmed" || item.status === "discovered" || evidence.confirmed === true;
    const directionClass = item.direction === "short" ? "short" : "long";
    const directionLabel = item.direction === "short" ? "做空" : "做多";
    const directionText = item.direction === "short" ? "偏空" : "偏多";
    const uniqueReasons = [...new Set(newsItems.map((entry) => String(entry.reason || "").trim()).filter(Boolean))];
    const primaryReason = uniqueReasons[0] || "关联新闻已形成方向判断，但模型未提供更详细的文字理由。";
    const flowConflict = marketFlow.hard_conflict === true;
    const technicalConclusion = entryState.tone === "triggered"
      ? `技术策略组与稳定门控均已确认：${coreMatchedCount} 项核心指标同向，${passedGroups.length} 个策略组达标；预测记录已经生成。`
      : entryState.tone === "blocked"
      ? `技术与行情输入已完成评估，但稳定风险门控阻断：${entryState.detail}。该机会不会被标记为已通过。`
      : confirmed
      ? `技术策略组已确认：${coreMatchedCount} 项核心指标同向，${passedGroups.length} 个策略组达标；仍需等待行情、事件与资金门控全部通过。`
      : flowConflict
      ? `技术策略组已达到门槛，但资金盘口与预测方向强冲突，暂不进入预测。`
      : `当前 ${coreMatchedCount} 项核心指标同向、${availableCount}/${requiredCount} 项有可用数据，通过 ${passedGroups.length} 个策略组；技术强度或组合评分尚未达到准入线。`;
    const newsMarkup = newsItems.length
      ? newsItems.map((entry) => {
          const score = Math.round(Number(entry.score || 0) * 100);
          const confidence = Math.round(Number(entry.confidence || 0) * 100);
          const relevance = Math.round(Number(entry.relevance || 0) * 100);
          return `<article class="ai-conclusion-news-item">
            <header><div><span>${this.escape(entry.source || "未知来源")}</span><time>${this.formatUnix(entry.ts)}</time></div><b>${score}%</b></header>
            <h4>${this.escape(entry.title || "未命名新闻")}</h4>
            <p>${this.escape(entry.reason || "AI 未提供进一步说明")}</p>
            <footer><span>AI 置信度 ${confidence}%</span><span>股票关联度 ${relevance}%</span><span>${this.escape(directionText)}</span></footer>
          </article>`;
        }).join("")
      : '<div class="ai-conclusion-empty">该机会没有可展示的新闻证据。</div>';
    const indicatorMarkup = indicatorItems.length
      ? indicatorItems.map((indicator) => {
          const metrics = (Array.isArray(indicator.metrics) ? indicator.metrics : []).slice(0, 5).map((metric) => {
            const label = metric && typeof metric === "object" ? metric.label : "指标值";
            const value = metric && typeof metric === "object" ? metric.value : metric;
            return `<span><em>${this.escape(label || "指标值")}</em><b>${this.escape(value ?? "--")}</b></span>`;
          }).join("");
          const availabilityLabel = indicator.available === false ? "数据不可用 · 不参与门槛" : indicator.matched ? "已满足" : "未满足";
          return `<article class="ai-conclusion-indicator ${indicator.available === false ? "unavailable" : indicator.matched ? "matched" : "unmatched"}">
            <header><strong>${indicator.available === false ? "--" : indicator.matched ? "✓" : "×"} ${this.escape(indicator.name || indicator.key)}</strong><span>${availabilityLabel}${indicator.available === false ? "" : ` · 强度 ${Number(indicator.strength ?? 0).toFixed(1)}`}</span></header>
            <p>${this.escape(indicator.summary || "暂无指标摘要")}</p>
            ${metrics ? `<div>${metrics}</div>` : ""}
          </article>`;
        }).join("")
      : '<div class="ai-conclusion-empty">该机会没有可展示的技术指标证据。</div>';
    const statusLabel = entryState.tone === "triggered" ? "门控已通过" : entryState.tone === "blocked" ? "稳定门控已阻断" : confirmed ? "技术已确认 · 门控待定" : flowConflict ? "资金盘口反向" : "等待技术确认";
    const marketPriceSource = this.firstValue(item?.quote?.price, item?.quote?.last_price, evidence.market?.price);
    const marketPrice = marketPriceSource == null ? "无数据" : this.compactNumber(marketPriceSource);
    const analysisMarkup = `
      <section class="ai-conclusion-refresh-meta" aria-label="AI 结论更新口径">
        <article><span>当前机会证据</span><b>${relatedNewsCount} 条</b><small>弹窗“相关新闻”使用同一组新闻 ID</small></article>
        <article><span>本轮新增新闻</span><b>${newNewsCount} 条</b><small>${newNewsCount > 0 ? "已触发 AI 研判与机会重算" : "本轮沿用最近一次有效研判"}</small></article>
        <article><span>历史记忆窗口</span><b>${this.escape(memoryWindowLabel)}</b><small>旧新闻与前序判断作为上下文，不冒充新事实</small></article>
        <article><span>结论更新时间</span><b>${this.escape(this.formatDate(item.updated_at || item.discovered_at))}</b><small>新相关新闻分析完成后自动刷新</small></article>
      </section>
      <section class="ai-conclusion-summary ${directionClass}">
        <div><span>综合研判</span><h3>AI 新闻分析${directionText} ${this.escape(item.symbol)}</h3><p>${this.escape(primaryReason)} ${this.escape(technicalConclusion)}</p></div>
        <strong>${directionLabel}<small>${statusLabel}</small></strong>
      </section>
      <section class="ai-conclusion-scores" aria-label="AI 分析评分">
        <article><span>新闻评分</span><b>${Number(item.news_score).toFixed(1)}</b><small>AI 置信度 × 股票关联度</small></article>
        <article><span>技术指标</span><b>${Number(item.indicator_score).toFixed(1)}</b><small>${coreMatchedCount} 项核心同向 · ${passedGroups.length} 组通过</small></article>
        <article><span>组合评分</span><b>${Number(item.combined_score).toFixed(1)}</b><small>${this.escape(scoreWeightLabel)}</small></article>
        <article><span>参考价格</span><b>${this.escape(marketPrice)}</b><small>信号扫描时行情</small></article>
      </section>
      <section class="ai-decision-coverage"><header><div><span>DECISION INPUT COVERAGE</span><h3>决策输入覆盖</h3></div><small>缺失输入不会被当前行情补造</small></header>${this.opportunityFeatureMarkup(item)}</section>
      <section class="ai-conclusion-section"><header><div><span>01</span><h3>最终结论</h3></div><small>${statusLabel}</small></header><div class="ai-conclusion-verdict"><p>方向判断：<strong class="${directionClass}">${directionLabel} ${this.escape(item.symbol)}</strong>。${this.escape(technicalConclusion)}</p><p>主要依据：${this.escape(uniqueReasons.join("；") || primaryReason)}</p></div></section>
      <section class="ai-conclusion-section"><header><div><span>02</span><h3>AI 新闻研判</h3></div><small>${newsItems.length} 条关联新闻</small></header><div class="ai-conclusion-news-list">${newsMarkup}</div></section>
      <section class="ai-conclusion-section"><header><div><span>03</span><h3>技术指标验证</h3></div><small>${availableCount} / ${requiredCount} 可用 · ${passedGroups.length} 组通过</small></header><div class="ai-conclusion-indicator-list">${indicatorMarkup}</div></section>`;
    this.conclusionOpportunity = item;
    this.conclusionPanels = {
      fundamentals: '<div class="ai-conclusion-loading">正在读取数据库基本面信息…</div>',
      analysis: analysisMarkup,
      news: this.renderAiRelatedNewsList(newsItems, item, true),
      memory: '<div class="ai-conclusion-loading">正在读取近 7 天 AI 新闻分析记录…</div>',
      market: this.renderMarketFlowPanel(item),
    };
    const enhancedCount = [enhancedSnapshot.quote, enhancedSnapshot.optionFlow, enhancedSnapshot.gex, enhancedSnapshot.institutional].filter((value) => this.featureIsAvailable(value)).length;
    const stableMarketFlowScore = this.firstValue(item?.score_components?.market_flow, item?.flow?.score, marketFlow.score);
    this.q("#ai-conclusion-market-state").textContent = marketFlow.version || enhancedCount || stableMarketFlowScore != null ? `盘口 ${stableMarketFlowScore == null || !Number.isFinite(Number(stableMarketFlowScore)) ? "--" : Number(stableMarketFlowScore).toFixed(1)} · 增强 ${enhancedCount}/4` : "旧信号无快照";
    this.q("#ai-conclusion-news-count").textContent = `关联 ${Math.max(newsItems.length, (item.news_ids || []).length)} 条`;
    this.showAiConclusionView("analysis");
    this.conclusionFocus = trigger || null;
    const modal = this.q("#ai-conclusion-modal");
    modal.classList.remove("hidden");
    modal.setAttribute("aria-hidden", "false");
    this.q("#ai-conclusion-close").focus({ preventScroll: true });
    this.loadAiRelatedNews(item, newsItems);
    this.loadAiNewsAnalysisRecords(item);
    this.loadAiFundamentals(item);
  }

  showAiConclusionView(view) {
    if (!["fundamentals", "news", "memory", "market", "analysis"].includes(view)) return;
    this.state.conclusionView = view;
    this.qa("[data-conclusion-view]").forEach((button) => {
      const active = button.dataset.conclusionView === view;
      button.classList.toggle("active", active);
      button.setAttribute("aria-selected", String(active));
    });
    const item = this.conclusionOpportunity;
    if (item) {
      const viewTitle = ({ fundamentals: "基本面信息", news: "相关新闻列表", memory: "AI 新闻分析记录", market: "市场与资金", analysis: "AI 分析结论" })[view];
      this.q("#ai-conclusion-title").textContent = `${item.symbol} · ${viewTitle}`;
      this.q("#ai-conclusion-subtitle").textContent = `${item.contract_symbol} · ${this.formatDate(item.discovered_at)} · ${item.timeframe} 周期`;
    }
    const target = this.q("#ai-conclusion-body");
    target.innerHTML = this.conclusionPanels[view] || '<div class="ai-conclusion-loading">正在读取相关内容…</div>';
    target.scrollTop = 0;
  }

  renderMarketFlowPanel(item) {
    const snapshot = this.normalizeFeatureSnapshot(item);
    const legacyFlow = item?.evidence?.market_flow && typeof item.evidence.market_flow === "object" ? item.evidence.market_flow : {};
    const stableFlow = item?.flow && typeof item.flow === "object" ? item.flow : {};
    const flow = { ...legacyFlow, ...stableFlow };
    const hasFlow = Object.keys(flow).some((key) => !["option_flow", "institutional_flow"].includes(key));
    const hasEnhanced = [snapshot.quote, snapshot.optionFlow, snapshot.gex, snapshot.institutional].some((value) => this.featureIsAvailable(value)) || snapshot.riskEvents.length > 0;
    if (!hasFlow && !hasEnhanced) {
      return '<div class="ai-market-flow-empty"><strong>该历史信号没有资金与行情快照</strong><span>新一轮信号会保存 Quote、期权流、GEX、Lit/Off-lit、事件门控和盘口变化；旧记录不会用当前行情反向补造。</span></div>';
    }
    const value = (raw, suffix = "") => raw == null || !Number.isFinite(Number(raw)) ? "--" : `${Number(raw).toFixed(2)}${suffix}`;
    const ratio = (raw) => raw == null || !Number.isFinite(Number(raw)) ? "--" : Number(raw).toFixed(3);
    const amount = (raw) => raw == null || !Number.isFinite(Number(raw)) ? "--" : this.compactNumber(Number(raw));
    const directionClass = item.direction === "short" ? "short" : "long";
    const directionLabel = item.direction === "short" ? "做空" : "做多";
    const stateLabel = flow.hard_conflict === true ? "强反向冲突" : flow.confirms_direction === true ? "确认预测方向" : flow.hard_conflict === false || flow.confirms_direction === false ? "中性观察" : "无方向判定";
    const turnoverLabel = flow.turnover_source === "underlying_volume_over_shares" ? "美股成交量 ÷ 总股本" : flow.turnover_source === "contract_value_over_market_cap_proxy" ? "合约成交额 ÷ 市值代理" : "暂无可靠分母";
    const sourceLabel = flow.sources?.depth === "binance_futures_market_by_price" ? "Binance 合约深度" : "深度暂缺";
    const activeSource = flow.sources?.active_flow === "binance_futures_taker" ? "Taker 主动成交" : "盘口压力代理";
    return `<section class="ai-market-flow-panel">
      <header class="${directionClass}"><div><span class="eyebrow">MARKET FLOW SNAPSHOT</span><h3>资金流与盘口结构</h3><p>生成信号时保存的实时快照，不使用当前行情覆盖历史判断。</p></div><strong>${flow.score == null || !Number.isFinite(Number(flow.score)) ? "--" : Number(flow.score).toFixed(1)}<small>${directionLabel}资金评分 · ${stateLabel}</small></strong></header>
      <section class="ai-market-flow-grid" aria-label="资金盘口指标">
        <article><span>换手率</span><b>${value(flow.turnover_rate_pct, "%")}</b><small>${this.escape(turnoverLabel)}</small></article>
        <article class="${flow.confirms_direction === true ? "positive" : flow.hard_conflict === true ? "negative" : ""}"><span>主力量比</span><b>${ratio(flow.main_force_ratio)}</b><small>${flow.main_force_ratio == null ? "无数据 · 未参与门控" : "0 买方弱 · 1 买方强"}</small></article>
        <article><span>主动买入占比</span><b>${value(flow.active_buy_ratio == null ? null : Number(flow.active_buy_ratio) * 100, "%")}</b><small>${this.escape(activeSource)}</small></article>
        <article><span>盘口失衡 / 近5档</span><b>${value(flow.book_imbalance)} / ${value(flow.book_imbalance_5)}</b><small>-1 卖压 · +1 买压</small></article>
        <article><span>买方 / 卖方深度</span><b>${amount(flow.bid_depth_notional)} / ${amount(flow.ask_depth_notional)}</b><small>前100档名义金额</small></article>
        <article><span>近5档买 / 卖</span><b>${amount(flow.bid_depth_notional_5)} / ${amount(flow.ask_depth_notional_5)}</b><small>最接近成交价的挂单</small></article>
        <article><span>买 / 卖挂单档位</span><b>${Number(flow.bid_level_count || 0)} / ${Number(flow.ask_level_count || 0)}</b><small>价格档数代理，非真实订单笔数</small></article>
        <article><span>5秒挂单增速</span><b>${value(flow.bid_depth_change_5s_pct, "%")} / ${value(flow.ask_depth_change_5s_pct, "%")}</b><small>买方 / 卖方深度变化</small></article>
        <article><span>30秒挂单增速</span><b>${value(flow.bid_depth_change_30s_pct, "%")} / ${value(flow.ask_depth_change_30s_pct, "%")}</b><small>过滤瞬时盘口噪声</small></article>
        <article><span>买卖价差</span><b>${value(flow.spread_bps, " bps")}</b><small>越低通常流动性越好</small></article>
      </section>
      <section class="ai-market-enhanced" aria-label="增强行情与资金数据">
        <header><strong>增强数据输入</strong><small>Quote、期权流、GEX、机构成交和事件门控均使用信号时快照；缺失项不会以当前数据回填。</small></header>
        ${this.opportunityFeatureMarkup(item)}
      </section>
      <section class="ai-market-flow-method"><div><strong>如何参与机会判断</strong><span>${this.escape(this.scoreWeightSummary(item.evidence || {}))}</span></div><p>主力量比综合主动成交 50%、近5档盘口压力 30%、挂单增速 20%。当至少两类资金数据有效且与预测方向的得分低于 35 分时，作为强反向冲突阻止生成预测。</p><footer><span>${this.escape(sourceLabel)}</span><span>数据质量 ${flow.data_quality == null ? "-- · 无数据" : value(Number(flow.data_quality) * 100, "%")}</span><span>${this.escape(flow.note || "买卖挂单数量使用可见价格档位代理。")}</span></footer></section>
    </section>`;
  }

  scoreWeightSummary(evidence = {}) {
    const sixDomainWeights = evidence.enhanced_effective_weights
      || evidence.enhanced_domain_scoring?.effective_weights
      || evidence.signal_policy_effective_weights
      || evidence.signal_policy_weights
      || evidence.enhanced_configured_weights
      || evidence.unusual_whales_signal_policy?.weights;
    const weights = evidence.score_weights && typeof evidence.score_weights === "object" ? evidence.score_weights : {};
    const percent = (value, fallback) => {
      const parsed = Number(value);
      const normalized = Number.isFinite(parsed) ? (parsed <= 1 ? parsed * 100 : parsed) : fallback;
      return Number(normalized.toFixed(1)).toString();
    };
    const domains = [
        ["新闻研判", "news", 20],
        ["技术", "technical", 30],
        ["期权流", "options_flow", 20],
        ["宏观板块", "market_context", 10],
        ["GEX", "gex", 10],
        ["机构确认", "institutional_flow", 10],
      ];
    if (sixDomainWeights && typeof sixDomainWeights === "object" && domains.every(([, key]) => Number.isFinite(Number(sixDomainWeights[key])))) {
      return domains.map(([label, key, fallback]) => `${label} ${percent(sixDomainWeights[key], fallback)}%`).join(" + ");
    }
    return `新闻 ${percent(weights.news, 45)}% + 技术指标 ${percent(weights.technical, 35)}% + 资金盘口 ${percent(weights.market_flow, 20)}%`;
  }

  async loadAiRelatedNews(item, fallbackItems) {
    const requestId = ++this.conclusionNewsRequestId;
    try {
      const data = await this.api(`/opportunities/${encodeURIComponent(item.id)}/news`);
      if (requestId !== this.conclusionNewsRequestId || this.conclusionOpportunity?.id !== item.id) return;
      const items = Array.isArray(data.items) ? data.items : [];
      this.conclusionPanels.news = this.renderAiRelatedNewsList(items, item, false);
      this.q("#ai-conclusion-news-count").textContent = `关联 ${items.length} 条`;
      if (this.state.conclusionView === "news") this.showAiConclusionView("news");
    } catch {
      if (requestId !== this.conclusionNewsRequestId || this.conclusionOpportunity?.id !== item.id) return;
      this.conclusionPanels.news = this.renderAiRelatedNewsList(fallbackItems, item, true);
      this.q("#ai-conclusion-news-count").textContent = `关联 ${fallbackItems.length} 条`;
      if (this.state.conclusionView === "news") this.showAiConclusionView("news");
    }
  }

  async loadAiNewsAnalysisRecords(item) {
    const requestId = ++this.conclusionMemoryRequestId;
    try {
      const data = await this.api(`/opportunities/${encodeURIComponent(item.id)}/news-analysis-records`);
      if (requestId !== this.conclusionMemoryRequestId || this.conclusionOpportunity?.id !== item.id) return;
      const records = Array.isArray(data.items) ? data.items : [];
      const memoryMeta = {
        currentOpportunityTotal: Number(data.current_opportunity_total || 0),
        excludedTotal: Number(data.excluded_total || 0),
        truncated: data.truncated === true,
      };
      this.conclusionPanels.memory = this.renderAiNewsAnalysisRecords(records, item, Number(data.window_days || 7), memoryMeta);
      this.q("#ai-conclusion-memory-count").textContent = `7天记忆 ${records.length} 条 · 当前 ${memoryMeta.currentOpportunityTotal} 条`;
      if (this.state.conclusionView === "memory") this.showAiConclusionView("memory");
    } catch {
      if (requestId !== this.conclusionMemoryRequestId || this.conclusionOpportunity?.id !== item.id) return;
      this.conclusionPanels.memory = '<div class="ai-memory-empty"><strong>新闻分析记录读取失败</strong><span>数据库暂时无法返回该股票的一周滚动研判记忆。</span></div>';
      this.q("#ai-conclusion-memory-count").textContent = "读取失败";
      if (this.state.conclusionView === "memory") this.showAiConclusionView("memory");
    }
  }

  renderAiNewsAnalysisRecords(records, opportunity, windowDays = 7, meta = {}) {
    const currentOpportunityTotal = Math.max(0, Number(meta.currentOpportunityTotal || 0));
    const excludedTotal = Math.max(0, Number(meta.excludedTotal || 0));
    if (!records.length) return `<div class="ai-memory-empty"><strong>${this.escape(opportunity.symbol)} 暂无可信 AI 新闻分析记录</strong><span>${excludedTotal ? `已隐藏 ${excludedTotal} 条未通过股票关联校验的历史记录。` : `功能启用后，通过关联校验的新分析会写入 ${windowDays} 天滚动记忆。`}</span></div>`;
    const directionLabel = (value) => ({ bull: "偏多", bear: "偏空", neutral: "中性" })[value] || "中性";
    const effectLabel = (value) => ({ initial: "首次判断", maintain: "维持判断", strengthen: "增强判断", weaken: "减弱判断", reverse: "判断反转" })[value] || "完成回溯";
    const positionEffectLabel = (value) => ({ hold: "维持", strengthen: "增强", caution: "谨慎", exit: "退出", reverse: "反向" })[value] || "未调整";
    const textItems = (value) => (Array.isArray(value) ? value : []).map((item) => String(item || "").trim()).filter(Boolean).slice(0, 5);
    const evidenceList = (items, emptyText) => items.length
      ? `<ul>${items.map((item) => `<li>${this.escape(item)}</li>`).join("")}</ul>`
      : `<p class="empty">${this.escape(emptyText)}</p>`;
    const chain = records.map((record, index) => {
      const direction = ["bull", "bear", "neutral"].includes(record.direction) ? record.direction : "neutral";
      const effect = ["initial", "maintain", "strengthen", "weaken", "reverse"].includes(record.memory_effect) ? record.memory_effect : "maintain";
      const memoryLinkStatus = ["initial", "linked", "context_missing"].includes(record.memory_link_status) ? record.memory_link_status : (record.prior_record_id ? "linked" : "initial");
      const effectText = memoryLinkStatus === "context_missing" ? "前序记忆遗漏" : effectLabel(effect);
      const title = record.news_title || record.news_original_title || "未命名新闻";
      const link = this.safeUrl(record.news_link);
      const titleMarkup = link ? `<a href="${this.escape(link)}" target="_blank" rel="noopener noreferrer">${this.escape(title)}</a>` : `<h4>${this.escape(title)}</h4>`;
      const prior = record.prior_record_id ? `承接 #${this.escape(record.prior_record_id)}` : (memoryLinkStatus === "context_missing" ? "旧记录：前序记忆未进入模型" : "无前序判断");
      const basis = record.judgment_basis && typeof record.judgment_basis === "object" ? record.judgment_basis : {};
      const modelFacts = textItems(basis.key_facts);
      const facts = modelFacts.length ? modelFacts : textItems([title, record.news_summary]);
      const supporting = textItems(basis.supporting_evidence);
      const counter = textItems(basis.counter_evidence);
      const uncertainties = textItems(basis.uncertainties);
      const previousConfidence = record.previous_confidence == null ? null : Math.round(Number(record.previous_confidence || 0) * 100);
      const currentConfidence = Math.round(Number(record.confidence || 0) * 100);
      const confidenceDelta = previousConfidence == null ? (memoryLinkStatus === "context_missing" ? "无法比较" : "首次判断") : `${currentConfidence - previousConfidence >= 0 ? "+" : ""}${currentConfidence - previousConfidence} pct`;
      const historicalComparison = record.memory_reason || "当时未保存历史对照说明";
      const impactMechanism = basis.impact_mechanism || record.analysis_reason || "当时未保存影响传导说明";
      const decisionSummary = basis.decision_summary || `形成${directionLabel(direction)}判断，置信度 ${currentConfidence}%。`;
      const positionImpact = record.position_effect ? `<div class="ai-memory-position-basis"><strong>持仓影响</strong><b>${positionEffectLabel(record.position_effect)}</b><span>${this.escape(record.position_reason || "当时未保存持仓调整说明")}</span></div>` : "";
      const evidenceGroups = supporting.length || counter.length || uncertainties.length ? `<div class="ai-memory-evidence-groups">
        <section class="support"><header>支持因素 <b>${supporting.length}</b></header>${evidenceList(supporting, "无额外支持因素")}</section>
        <section class="counter"><header>反向证据 <b>${counter.length}</b></header>${evidenceList(counter, "未识别到明确反向证据")}</section>
        <section class="uncertainty"><header>不确定性 <b>${uncertainties.length}</b></header>${evidenceList(uncertainties, "未单独记录不确定性")}</section>
      </div>` : '<p class="ai-memory-legacy-note">旧记录未保存结构化支持因素、反向证据和不确定性；系统不会根据当前信息反向补造。</p>';
      const symbolContextCount = Math.max(0, Number(record.symbol_context_count || 0));
      const sharedContextCount = Math.max(0, Number(record.shared_context_count || 0));
      return `<article class="ai-memory-record ${direction} effect-${effect} memory-${memoryLinkStatus}">
        <div class="ai-memory-line"><i></i><span>${String(records.length - index).padStart(2, "0")}</span></div>
        <div class="ai-memory-card">
          <header><div><span>${this.escape(record.news_source || "未知来源")}</span><time>${this.formatUnix(record.news_published_at)}</time>${record.belongs_to_opportunity ? '<em>本机会关联</em>' : ""}</div><strong class="${memoryLinkStatus === "context_missing" ? "context-missing" : effect}">${effectText}</strong></header>
          ${titleMarkup}
          <section class="ai-memory-verdict"><b class="${direction}">${directionLabel(direction)}</b><span>置信度 ${Math.round(Number(record.confidence || 0) * 100)}%</span><span>关联度 ${Math.round(Number(record.relevance || 0) * 100)}%</span><span>${this.impactLabel(record.impact_strength)}</span><span>${this.horizonLabel(record.time_horizon)}</span></section>
          <section class="ai-memory-reasoning">
            <header><strong>判断依据与过程</strong><span>可审计理由摘要，不包含模型隐藏思维链</span></header>
            <div class="ai-memory-reasoning-steps">
              <article><b>01</b><div><strong>${modelFacts.length ? "事实输入" : "原始新闻输入"}</strong>${evidenceList(facts, "旧记录未保存逐项事实依据")}</div></article>
              <article><b>02</b><div><strong>影响传导</strong><p>${this.escape(impactMechanism)}</p></div></article>
              <article><b>03</b><div><strong>历史对照</strong><p>${this.escape(historicalComparison)}</p><small>${record.previous_direction ? `${directionLabel(record.previous_direction)} ${previousConfidence}% → ${directionLabel(direction)} ${currentConfidence}%` : (memoryLinkStatus === "context_missing" ? "前序记忆未进入当时模型上下文" : "无前序判断")} · ${confidenceDelta}</small></div></article>
              <article><b>04</b><div><strong>结论形成</strong><p>${this.escape(decisionSummary)}</p></div></article>
            </div>
            ${evidenceGroups}${positionImpact}
          </section>
          <footer><span>${prior}</span><span>${this.escape(record.symbol || "--")} 前序记忆 ${symbolContextCount} 条</span><span>批次公共记忆 ${sharedContextCount} 条</span><span>模型 ${this.escape(record.model_name || "--")}</span><time>分析 ${this.formatDate(record.analyzed_at)}</time></footer>
        </div>
      </article>`;
    }).join("");
    const filterNote = excludedTotal ? `已隐藏 ${excludedTotal} 条未通过当前股票关联校验的历史记录。` : "所有展示记录均已通过当前股票关联校验。";
    return `<section class="ai-memory-panel"><header><div><span class="eyebrow">AI NEWS MEMORY</span><h3>${this.escape(opportunity.symbol)} · 一周新闻研判追踪</h3><p>七天记忆与当前机会采用不同口径；当前机会关联 ${currentOpportunityTotal} 条。${filterNote}</p></div><strong>${records.length}<small>条七天记忆</small></strong></header><div class="ai-memory-summary"><span>追踪窗口 <b>${windowDays} 天</b></span><span>当前机会 <b>${currentOpportunityTotal} 条</b></span><span>最新方向 <b class="${this.escape(records[0].direction || "neutral")}">${directionLabel(records[0].direction)}</b></span><span>已过滤历史污染 <b>${excludedTotal} 条</b></span></div><div class="ai-memory-timeline">${chain}</div></section>`;
  }

  async openNewsSystemPrompt(trigger) {
    this.newsSystemPromptFocus = trigger || null;
    const requestId = ++this.newsSystemPromptRequestId;
    const modal = this.q("#news-system-prompt-modal");
    const input = this.q("#news-system-prompt-input");
    const saveButton = this.q("#news-system-prompt-save");
    const state = this.q("#news-system-prompt-state");
    const status = this.q("#news-system-prompt-status");
    input.value = "";
    input.disabled = true;
    saveButton.disabled = true;
    state.textContent = "正在读取";
    state.className = "loading";
    status.textContent = "";
    status.className = "news-system-prompt-status";
    this.updateNewsSystemPromptCount();
    modal.classList.remove("hidden");
    modal.setAttribute("aria-hidden", "false");
    this.q("#news-system-prompt-close").focus({ preventScroll: true });
    try {
      const data = await this.api("/news-system-prompt");
      if (requestId !== this.newsSystemPromptRequestId || modal.classList.contains("hidden")) return;
      this.newsSystemPromptDefault = String(data.default_system_prompt || "").trim();
      this.newsSystemPromptIsCustom = data.is_custom === true;
      input.value = String(data.system_prompt || this.newsSystemPromptDefault);
      state.textContent = this.newsSystemPromptIsCustom ? "自定义提示词生效中" : "当前使用系统默认模板";
      state.className = this.newsSystemPromptIsCustom ? "custom" : "default";
      input.disabled = false;
      saveButton.disabled = false;
      this.updateNewsSystemPromptCount();
      input.focus({ preventScroll: true });
    } catch (error) {
      if (requestId !== this.newsSystemPromptRequestId) return;
      state.textContent = "读取失败";
      state.className = "error";
      status.textContent = error.message || "系统提示词读取失败，请稍后重试。";
      status.className = "news-system-prompt-status error";
    }
  }

  updateNewsSystemPromptCount() {
    const input = this.q("#news-system-prompt-input");
    const count = this.q("#news-system-prompt-count");
    if (input && count) count.textContent = String(input.value.length);
  }

  restoreDefaultNewsSystemPrompt() {
    const input = this.q("#news-system-prompt-input");
    if (!input || !this.newsSystemPromptDefault) return;
    input.value = this.newsSystemPromptDefault;
    this.q("#news-system-prompt-status").textContent = "已载入默认模板，点击“保存并应用”后恢复默认配置。";
    this.q("#news-system-prompt-status").className = "news-system-prompt-status notice";
    this.updateNewsSystemPromptCount();
    input.focus({ preventScroll: true });
  }

  async saveNewsSystemPrompt(event) {
    event.preventDefault();
    const input = this.q("#news-system-prompt-input");
    const saveButton = this.q("#news-system-prompt-save");
    const status = this.q("#news-system-prompt-status");
    const value = input.value.trim();
    if (value.length < 40) {
      status.textContent = "系统提示词至少需要 40 个字符。";
      status.className = "news-system-prompt-status error";
      input.focus({ preventScroll: true });
      return;
    }
    saveButton.disabled = true;
    input.disabled = true;
    status.textContent = "正在保存并应用…";
    status.className = "news-system-prompt-status loading";
    try {
      const useDefault = value === this.newsSystemPromptDefault;
      const data = await this.api("/news-system-prompt", {
        method: "PUT",
        body: JSON.stringify({ system_prompt: useDefault ? null : value }),
      });
      this.newsSystemPromptDefault = String(data.default_system_prompt || this.newsSystemPromptDefault);
      this.newsSystemPromptIsCustom = data.is_custom === true;
      this.closeNewsSystemPrompt();
      this.showBanner(this.newsSystemPromptIsCustom ? "系统提示词已保存，下一批新闻分析将使用新配置。" : "已恢复默认系统提示词，下一批新闻分析将使用默认模板。", "success");
    } catch (error) {
      status.textContent = error.message || "系统提示词保存失败，请稍后重试。";
      status.className = "news-system-prompt-status error";
      saveButton.disabled = false;
      input.disabled = false;
    }
  }

  closeNewsSystemPrompt(restoreFocus = true) {
    const modal = this.q("#news-system-prompt-modal");
    if (!modal || modal.classList.contains("hidden")) return;
    modal.classList.add("hidden");
    modal.setAttribute("aria-hidden", "true");
    this.newsSystemPromptRequestId += 1;
    const focusTarget = this.newsSystemPromptFocus;
    this.newsSystemPromptFocus = null;
    if (restoreFocus && focusTarget?.isConnected) focusTarget.focus({ preventScroll: true });
  }

  async openHistoricalJudgment(trigger) {
    if (!this.conclusionOpportunity) return;
    this.historicalJudgmentFocus = trigger || null;
    const requestId = ++this.historicalJudgmentRequestId;
    const item = this.conclusionOpportunity;
    this.q("#historical-judgment-title").textContent = `${item.symbol} · 历史研判`;
    this.q("#historical-judgment-subtitle").textContent = `${item.contract_symbol} · 正在读取该机会冻结的模型调用上下文`;
    this.q("#historical-judgment-body").innerHTML = '<div class="historical-judgment-empty"><strong>正在读取连续研判上下文</strong><span>正在提取旧新闻、记忆链与当前研究持仓快照…</span></div>';
    const modal = this.q("#historical-judgment-modal");
    modal.classList.remove("hidden");
    modal.setAttribute("aria-hidden", "false");
    this.q("#historical-judgment-close").focus({ preventScroll: true });
    try {
      const data = await this.api(`/opportunities/${encodeURIComponent(item.id)}/model-calls`);
      if (requestId !== this.historicalJudgmentRequestId || this.conclusionOpportunity?.id !== item.id) return;
      const calls = Array.isArray(data.items) ? data.items : [];
      const snapshots = [];
      calls.forEach((call) => {
        const request = call?.request_json && typeof call.request_json === "object" ? call.request_json : {};
        const messages = Array.isArray(request.messages) ? request.messages : [];
        messages.filter((message) => message?.role === "user").forEach((message) => {
          try {
            const input = typeof message.content === "string" ? JSON.parse(message.content) : message.content;
            const memory = Array.isArray(input?.historical_analysis_memory) ? input.historical_analysis_memory : [];
            const historicalNews = Array.isArray(input?.historical_related_news) ? input.historical_related_news : [];
            const positions = Array.isArray(input?.open_research_positions) ? input.open_research_positions : [];
            snapshots.push({ call, memory, historicalNews, positions, windowDays: Number(input?.memory_window_days || 7) });
          } catch {
            snapshots.push({ call, memory: [], historicalNews: [], positions: [], windowDays: 7, unreadable: true });
          }
        });
      });
      this.q("#historical-judgment-subtitle").textContent = `${item.contract_symbol} · ${calls.length} 次模型调用 · 仅展示当时实际发送的历史上下文`;
      this.q("#historical-judgment-body").innerHTML = this.renderHistoricalJudgment(snapshots, item, data.note || "");
    } catch (error) {
      if (requestId !== this.historicalJudgmentRequestId) return;
      this.q("#historical-judgment-body").innerHTML = `<div class="historical-judgment-empty error"><strong>历史研判读取失败</strong><span>${this.escape(error.message || "请稍后重试")}</span></div>`;
    }
  }

  renderHistoricalJudgment(snapshots, item, note = "") {
    const effectLabel = (value) => ({ initial: "首次判断", maintain: "维持", strengthen: "增强", weaken: "减弱", reverse: "反转" })[value] || "历史判断";
    const directionLabel = (value) => ({ bull: "偏多", bear: "偏空", neutral: "中性" })[value] || "中性";
    const positionEffectLabel = (value) => ({ hold: "维持", strengthen: "增强", caution: "谨慎", exit: "退出", reverse: "反向" })[value] || "未调整";
    const memoryCount = snapshots.reduce((total, snapshot) => total + snapshot.memory.length, 0);
    const oldNewsCount = snapshots.reduce((total, snapshot) => total + snapshot.historicalNews.length, 0);
    const positionCount = snapshots.reduce((total, snapshot) => total + snapshot.positions.length, 0);
    const historicalBasis = (record) => {
      const basis = record?.judgment_basis && typeof record.judgment_basis === "object" ? record.judgment_basis : {};
      const facts = (Array.isArray(basis.key_facts) ? basis.key_facts : []).map((value) => String(value || "").trim()).filter(Boolean).slice(0, 5);
      const supporting = (Array.isArray(basis.supporting_evidence) ? basis.supporting_evidence : []).map((value) => String(value || "").trim()).filter(Boolean).slice(0, 5);
      const counter = (Array.isArray(basis.counter_evidence) ? basis.counter_evidence : []).map((value) => String(value || "").trim()).filter(Boolean).slice(0, 5);
      const uncertainties = (Array.isArray(basis.uncertainties) ? basis.uncertainties : []).map((value) => String(value || "").trim()).filter(Boolean).slice(0, 5);
      const chips = (label, values) => values.length ? `<div><strong>${label}</strong>${values.map((value) => `<span>${this.escape(value)}</span>`).join("")}</div>` : "";
      if (!facts.length && !supporting.length && !counter.length && !uncertainties.length && !basis.impact_mechanism && !basis.decision_summary) return '<p class="historical-basis-empty">该旧记录未保存结构化判断依据。</p>';
      return `<section class="historical-judgment-basis"><header>当时的判断依据</header><p><strong>影响传导：</strong>${this.escape(basis.impact_mechanism || record.analysis_reason || "未保存")}</p><p><strong>结论逻辑：</strong>${this.escape(basis.decision_summary || record.analysis_reason || "未保存")}</p><div>${chips("事实", facts)}${chips("支持", supporting)}${chips("反向", counter)}${chips("不确定", uncertainties)}</div></section>`;
    };
    if (!snapshots.length) return `<div class="historical-judgment-empty"><strong>${this.escape(item.symbol)} 没有可审计的历史研判输入</strong><span>${this.escape(note || "该机会生成时尚未保存模型调用请求，系统不会用当前记录反向补造。")}</span></div>`;
    const sections = snapshots.map((snapshot, callIndex) => {
      const positions = snapshot.positions.map((position) => {
        const pnl = position.unrealized_bps == null ? Number.NaN : Number(position.unrealized_bps);
        const pnlText = Number.isFinite(pnl) ? `${pnl >= 0 ? "+" : ""}${pnl.toFixed(2)} bps` : "行情暂缺";
        return `<article class="historical-position-card ${pnl < 0 ? "loss" : "gain"}">
          <header><div><strong>${this.escape(position.symbol || "--")}</strong><span>${position.direction === "short" ? "做空" : "做多"}</span></div><b>${pnlText}</b></header>
          <div><span>入场价 <b>${position.entry_price == null ? "--" : this.compactNumber(position.entry_price)}</b></span><span>当前价 <b>${position.current_price == null ? "--" : this.compactNumber(position.current_price)}</b></span><span>组合分 <b>${position.current_combined_score == null ? "--" : Number(position.current_combined_score).toFixed(1)}</b></span></div>
          <footer><span>止损 ${position.stop_loss_price == null ? "--" : this.compactNumber(position.stop_loss_price)}</span><span>止盈 ${position.take_profit_price == null ? "--" : this.compactNumber(position.take_profit_price)}</span><time>到期 ${this.formatDate(position.due_at)}</time></footer>
        </article>`;
      }).join("");
      const oldNews = snapshot.historicalNews.map((news, index) => `<article class="historical-old-news-card">
        <header><b>#${String(index + 1).padStart(2, "0")}</b><span>${this.escape(news.source || "未知来源")}</span><time>${this.formatUnix(news.published_at)}</time></header>
        <h3>${this.escape(news.title || "未命名历史新闻")}</h3>
        <p>${this.escape(news.summary || "无历史摘要")}</p>
        <footer>${(Array.isArray(news.prior_judgments) ? news.prior_judgments : []).map((judgment) => `<span>${this.escape(judgment.symbol || "--")} · ${directionLabel(judgment.direction)} · ${effectLabel(judgment.memory_effect)}</span>`).join("")}</footer>
      </article>`).join("");
      const records = snapshot.memory.map((record, index) => `<article class="historical-judgment-card ${this.escape(record.direction || "neutral")}">
        <header><div><b>#${String(index + 1).padStart(2, "0")}</b><strong>${this.escape(record.symbol || "--")}</strong><span>${directionLabel(record.direction)}</span></div><em>${effectLabel(record.memory_effect)}</em></header>
        <h3>${this.escape(record.news_title || "未命名历史新闻")}</h3>
        <p><strong>历史结论：</strong>${this.escape(record.analysis_reason || "无研判说明")}</p>
        <p class="memory-impact"><strong>当时的记忆变化：</strong>${this.escape(record.memory_reason || "无变化说明")}</p>
        ${historicalBasis(record)}
        ${record.position_effect ? `<p class="position-impact"><strong>当时对持仓影响：</strong>${positionEffectLabel(record.position_effect)} · ${this.escape(record.position_reason || "无持仓变化说明")}</p>` : ""}
        <footer><span>置信度 ${Math.round(Number(record.confidence || 0) * 100)}%</span><span>关联度 ${Math.round(Number(record.relevance || 0) * 100)}%</span><span>${this.escape(record.impact_strength || "--")}</span><span>${this.escape(record.time_horizon || "--")}</span><time>${this.escape(record.analyzed_at || "--")}</time></footer>
      </article>`).join("");
      return `<section class="historical-judgment-snapshot"><header><div><span>MODEL CALL ${callIndex + 1}</span><h3>${this.escape(snapshot.call.model_name || "AI 模型")}</h3></div><strong>${snapshot.memory.length + snapshot.historicalNews.length + snapshot.positions.length}<small>条连续上下文 · ${snapshot.windowDays} 天</small></strong></header>
        ${positions ? `<section class="historical-context-block"><h4>当前研究持仓快照 <span>${snapshot.positions.length}</span></h4><div class="historical-position-grid">${positions}</div></section>` : ""}
        ${oldNews ? `<section class="historical-context-block"><h4>相关旧新闻 <span>${snapshot.historicalNews.length}</span></h4>${oldNews}</section>` : ""}
        ${records ? `<section class="historical-context-block"><h4>一周研判记忆链 <span>${snapshot.memory.length}</span></h4>${records}</section>` : `<div class="historical-judgment-empty compact"><strong>本次没有历史记忆链</strong><span>${snapshot.unreadable ? "User 消息不是可解析的结构化 JSON。" : "没有找到与本批新闻或当前持仓相关的旧记录。"}</span></div>`}
      </section>`;
    }).join("");
    return `<section class="historical-judgment-panel"><div class="historical-judgment-summary"><article><span>模型调用</span><b>${snapshots.length}</b></article><article><span>旧新闻 / 记忆</span><b>${oldNewsCount} / ${memoryCount}</b></article><article><span>持仓快照</span><b>${positionCount}</b></article><article><span>影响路径</span><b>连续研判 → 机会与持仓</b></article></div><p class="historical-judgment-note">模型会同时比较新新闻、相关旧新闻、一周记忆链和当前未结算研究持仓，判断原观点是维持、增强、谨慎、退出还是反向；持仓只作为上下文，不能反过来充当方向证据。</p>${sections}</section>`;
  }

  closeHistoricalJudgment(restoreFocus = true) {
    const modal = this.q("#historical-judgment-modal");
    if (!modal || modal.classList.contains("hidden")) return;
    modal.classList.add("hidden");
    modal.setAttribute("aria-hidden", "true");
    this.historicalJudgmentRequestId += 1;
    const focusTarget = this.historicalJudgmentFocus;
    this.historicalJudgmentFocus = null;
    if (restoreFocus && focusTarget?.isConnected) focusTarget.focus({ preventScroll: true });
  }

  openNewsAnalysisLogic(trigger) {
    if (!this.conclusionOpportunity) return;
    this.newsLogicFocus = trigger || null;
    this.newsModelCalls = [];
    this.newsModelCallIndex = 0;
    this.q("#news-logic-title").textContent = `${this.conclusionOpportunity.symbol} · 新闻分析逻辑`;
    this.q("#news-logic-subtitle").textContent = `${this.conclusionOpportunity.contract_symbol} · 正在读取模型调用审计记录`;
    this.q("#news-logic-body").innerHTML = '<div class="news-model-call-loading">正在读取实际提示词与模型原始响应…</div>';
    const modal = this.q("#news-logic-modal");
    modal.classList.remove("hidden");
    modal.setAttribute("aria-hidden", "false");
    this.q("#news-logic-close").focus({ preventScroll: true });
    this.loadNewsModelCalls(this.conclusionOpportunity);
  }

  async loadNewsModelCalls(item) {
    const requestId = ++this.newsModelCallsRequestId;
    try {
      const data = await this.api(`/opportunities/${encodeURIComponent(item.id)}/model-calls`);
      if (requestId !== this.newsModelCallsRequestId || this.conclusionOpportunity?.id !== item.id) return;
      this.newsModelCalls = Array.isArray(data.items) ? data.items : [];
      this.newsModelCallIndex = 0;
      this.newsModelCallsNote = data.note || "";
      this.renderNewsAnalysisLogic();
    } catch (error) {
      if (requestId !== this.newsModelCallsRequestId || this.conclusionOpportunity?.id !== item.id) return;
      this.q("#news-logic-body").innerHTML = `<div class="news-model-call-empty error"><strong>模型调用记录读取失败</strong><span>${this.escape(error.message || "请稍后重试")}</span></div>`;
    }
  }

  renderNewsAnalysisLogic() {
    const item = this.conclusionOpportunity;
    if (!item) return;
    const calls = this.newsModelCalls;
    this.q("#news-logic-title").textContent = `${item.symbol} · 新闻分析逻辑`;
    this.q("#news-logic-subtitle").textContent = `${item.contract_symbol} · 实际提示词与模型原始返回`;
    if (!calls.length) {
      this.q("#news-logic-body").innerHTML = `<div class="news-model-call-empty"><strong>该机会没有原始模型调用记录</strong><span>${this.escape(this.newsModelCallsNote || "该机会生成时尚未启用原始调用审计；重新分析关联新闻后即可查看。")}</span><small>系统不会根据解析结果伪造历史提示词或原始响应。</small></div>`;
      return;
    }
    const selectedIndex = Math.max(0, Math.min(this.newsModelCallIndex, calls.length - 1));
    this.newsModelCallIndex = selectedIndex;
    const call = calls[selectedIndex];
    const request = call.request_json && typeof call.request_json === "object" ? call.request_json : {};
    const messages = Array.isArray(request.messages) ? request.messages : [];
    const systemPrompt = messages.filter((entry) => entry?.role === "system").map((entry) => String(entry.content || "")).join("\n\n");
    const userPrompt = messages.filter((entry) => entry?.role === "user").map((entry) => String(entry.content || "")).join("\n\n");
    const requestParameters = { ...request };
    delete requestParameters.messages;
    const rawResponse = call.response_text || "";
    const responseEnvelope = call.response_envelope || "";
    const tabs = calls.map((entry, index) => `<button class="${index === selectedIndex ? "active" : ""}" type="button" data-news-call-index="${index}"><span>调用 ${String(index + 1).padStart(2, "0")}</span><strong>${this.escape(entry.model_name || "未知模型")}</strong><small>${this.escape((entry.news_ids || []).length)} 条新闻 · ${this.formatDate(entry.started_at)}</small></button>`).join("");
    const promptBlock = (title, role, content, key) => `<article class="news-model-raw-block"><header><div><span>${role}</span><h3>${title}</h3></div><button type="button" data-copy-call-section="${key}">复制原文</button></header><pre><code>${this.escape(content || "（空）")}</code></pre></article>`;
    this.q("#news-logic-body").innerHTML = `
      <nav class="news-model-call-tabs" aria-label="模型调用记录">${tabs}</nav>
      <section class="news-model-call-meta">
        <article><span>服务商</span><b>${this.escape(call.provider_code || "--")}</b></article>
        <article><span>模型</span><b>${this.escape(call.model_name || "--")}</b></article>
        <article><span>新闻数量</span><b>${(call.news_ids || []).length} 条</b></article>
        <article><span>调用状态</span><b class="${call.status === "completed" ? "success" : "failed"}">${call.status === "completed" ? "调用成功" : `调用失败 · ${this.escape(call.error_category || "unknown")}`}</b></article>
        <article><span>调用时间</span><b>${this.formatDate(call.started_at)}</b></article>
      </section>
      <section class="news-model-audit-note"><strong>数据库原始审计</strong><span>以下内容是本次请求实际发送和收到的原文；认证请求头与 API Key 从未写入记录。</span><em>批次 ${this.escape(call.batch_id || "--")}</em></section>
      ${promptBlock("System 提示词", "SYSTEM", systemPrompt, "system")}
      ${promptBlock("User 提示词 / 新闻输入", "USER", userPrompt, "user")}
      ${promptBlock("请求参数", "REQUEST", this.prettyJson(requestParameters), "request")}
      ${promptBlock("模型返回原始文本", "RAW OUTPUT", rawResponse || "模型未返回可读取的 message content", "response")}
      ${promptBlock("服务商完整响应包", "HTTP BODY", this.prettyJson(responseEnvelope) || "服务商未返回响应正文", "envelope")}`;
    this.q("#news-logic-body").scrollTop = 0;
  }

  prettyJson(value) {
    if (value == null || value === "") return "";
    try {
      const parsed = typeof value === "string" ? JSON.parse(value) : value;
      return JSON.stringify(parsed, null, 2);
    } catch {
      return String(value);
    }
  }

  async copyNewsModelCallSection(section, button) {
    const call = this.newsModelCalls[this.newsModelCallIndex];
    if (!call) return;
    const request = call.request_json && typeof call.request_json === "object" ? call.request_json : {};
    const messages = Array.isArray(request.messages) ? request.messages : [];
    const values = {
      system: messages.filter((entry) => entry?.role === "system").map((entry) => String(entry.content || "")).join("\n\n"),
      user: messages.filter((entry) => entry?.role === "user").map((entry) => String(entry.content || "")).join("\n\n"),
      request: this.prettyJson(request),
      response: String(call.response_text || ""),
      envelope: this.prettyJson(call.response_envelope),
    };
    const value = values[section];
    if (value == null) return;
    try {
      await navigator.clipboard.writeText(value);
      const original = button.textContent;
      button.textContent = "已复制";
      window.setTimeout(() => { if (button.isConnected) button.textContent = original; }, 1200);
    } catch {
      this.showBanner("浏览器未允许复制，请直接选择代码块内容。", "error");
    }
  }

  closeNewsAnalysisLogic(restoreFocus = true) {
    const modal = this.q("#news-logic-modal");
    if (!modal || modal.classList.contains("hidden")) return;
    modal.classList.add("hidden");
    modal.setAttribute("aria-hidden", "true");
    this.newsModelCallsRequestId += 1;
    this.newsModelCalls = [];
    const focusTarget = this.newsLogicFocus;
    this.newsLogicFocus = null;
    if (restoreFocus && focusTarget?.isConnected) focusTarget.focus({ preventScroll: true });
  }

  renderAiRelatedNewsList(items, opportunity, compactEvidence = false) {
    if (!items.length) return '<div class="ai-related-news-empty"><strong>暂无相关新闻</strong><span>当前机会记录中没有可展示的关联新闻。</span></div>';
    const cards = items.map((item) => {
      const relatedStocks = Array.isArray(item.related_us_stocks) ? item.related_us_stocks : [];
      const relatedIndustries = Array.isArray(item.related_industries) ? item.related_industries : [];
      const stock = relatedStocks.find((entry) => String(entry.symbol || "").toUpperCase().replace(/(?:USDT|USD1)$/i, "") === opportunity.symbol) || {};
      const confidence = Number(item.ai_confidence ?? item.confidence ?? 0);
      const relevance = Number(stock.relevance ?? item.relevance ?? 0);
      const score = Number(item.score ?? confidence * relevance);
      const rawDirection = String(stock.direction || item.direction || "").toLowerCase();
      const directionClass = ["bear", "bearish", "short"].includes(rawDirection) ? "bear" : ["bull", "bullish", "long"].includes(rawDirection) ? "bull" : "neutral";
      const directionLabel = directionClass === "bear" ? "偏空" : directionClass === "bull" ? "偏多" : "中性";
      const title = item.title_zh || item.title || "未命名新闻";
      const link = this.safeUrl(item.link);
      const titleMarkup = link ? `<a href="${this.escape(link)}" target="_blank" rel="noopener noreferrer">${this.escape(title)}</a>` : `<h3>${this.escape(title)}</h3>`;
      const reason = item.ai_reason || item.reason || item.summary || "AI 未提供进一步说明";
      const industries = relatedIndustries.slice(0, 5).map((entry) => `<span>${this.escape(entry.name || entry.industry || entry.label || "相关行业")}</span>`).join("");
      const stocks = relatedStocks.slice(0, 6).map((entry) => `<span class="${this.escape(String(entry.direction || "neutral").toLowerCase())}">${this.escape(entry.symbol || "--")} <b>${Math.round(Number(entry.relevance || 0) * 100)}%</b></span>`).join("");
      return `<article class="ai-related-news-card ${directionClass}">
        <header><div><span>${this.escape(item.source || "未知来源")}</span><time>${this.formatUnix(item.ts)}</time></div><strong>${Math.round(score * 100)}%</strong></header>
        ${titleMarkup}
        <p>${this.escape(reason)}</p>
        <div class="ai-related-news-tags"><span class="sentiment-pill ${directionClass}">${directionLabel}</span><span>${this.impactLabel(item.ai_impact_strength)}</span><span>${this.horizonLabel(item.ai_time_horizon)}</span><span>${this.categoryLabel(item.ai_category)}</span><span>AI 置信度 ${Math.round(confidence * 100)}%</span><span>关联度 ${Math.round(relevance * 100)}%</span></div>
        ${!compactEvidence && (industries || stocks) ? `<div class="ai-related-news-relations"><div><em>行业</em><div>${industries || "无直接关联行业"}</div></div><div><em>美股</em><div>${stocks || "无其他关联美股"}</div></div></div>` : ""}
      </article>`;
    }).join("");
    return `<section class="ai-related-news-panel"><header><div><span class="eyebrow">RELATED NEWS</span><h3>${this.escape(opportunity.symbol)} 相关新闻</h3><p>这里展示生成该机会时关联的全部新闻及其 AI 研判依据。</p></div><strong>${items.length}<small>条新闻</small></strong></header><div class="ai-related-news-list">${cards}</div></section>`;
  }

  async loadAiFundamentals(item) {
    const requestId = ++this.conclusionFundamentalRequestId;
    try {
      const data = await this.api(`/opportunities/${encodeURIComponent(item.id)}/fundamentals`);
      if (requestId !== this.conclusionFundamentalRequestId || this.conclusionOpportunity?.id !== item.id) return;
      this.conclusionPanels.fundamentals = this.renderAiFundamentals(data, item);
      this.q("#ai-conclusion-fundamental-state").textContent = data.available ? "数据库资料" : "暂无资料";
      if (this.state.conclusionView === "fundamentals") this.showAiConclusionView("fundamentals");
    } catch {
      if (requestId !== this.conclusionFundamentalRequestId || this.conclusionOpportunity?.id !== item.id) return;
      this.conclusionPanels.fundamentals = '<div class="ai-fundamental-empty"><strong>基本面读取失败</strong><span>数据库暂时无法返回该股票的基本面资料。</span></div>';
      this.q("#ai-conclusion-fundamental-state").textContent = "读取失败";
      if (this.state.conclusionView === "fundamentals") this.showAiConclusionView("fundamentals");
    }
  }

  renderAiFundamentals(data, opportunity) {
    if (!data?.available) return `<div class="ai-fundamental-empty"><strong>${this.escape(opportunity.symbol)} 暂无基本面资料</strong><span>证券资料库尚未收录该标的。</span></div>`;
    const security = data.security || {};
    const profile = data.profile || {};
    const analysis = data.analysis || {};
    const financials = data.financials || {};
    const evidence = analysis.evidence || {};
    const companyName = security.company_name_zh || security.company_name || profile.legal_name || opportunity.symbol;
    const legalName = profile.legal_name || security.company_name || "--";
    const typeLabel = ({ COMMON_STOCK: "普通股", ETF: "ETF", ADR: "ADR", UNKNOWN: "待核验" })[security.security_type] || security.security_type || "--";
    const industry = profile.industry_zh || profile.industry || "待补充";
    const sector = profile.sector_zh || profile.sector || "待补充";
    const marketCap = this.formatMarketCapMillions(profile.market_cap, security.currency || "USD");
    const shares = profile.shares_outstanding == null ? "--" : `${this.compactNumber(profile.shares_outstanding)} M`;
    const employeeCount = profile.employee_count == null ? "--" : this.number(profile.employee_count);
    const score = (value) => value == null ? "--" : Number(value).toFixed(1);
    const confidence = analysis.confidence_score == null ? "--" : `${Math.round(Number(analysis.confidence_score) * 100)}%`;
    const complete = evidence.fundamental_data_complete === true || evidence.financial_metrics_complete === true;
    const financialStatus = financials.data_status || evidence.financial_metrics_status || "PARTIAL";
    const financialCurrency = financials.currency || security.currency || "USD";
    const financialStatusLabel = financialStatus === "NOT_APPLICABLE" ? "非公司财务口径" : financialStatus === "COMPLETE" ? "完整基本面" : "基本面待核验";
    const financialStatusCopy = financialStatus === "NOT_APPLICABLE" ? "该标的不是经营性公司，营收、利润率、现金流、债务和公司估值已明确标记为不适用。" : financialStatus === "COMPLETE" ? "营收、利润率、现金流、负债与估值指标均已接入数据库。" : "部分财务分类仍缺少可核验的公开数据。";
    const unavailable = financialStatus === "COMPLETE" || financialStatus === "NOT_APPLICABLE" ? "N/A" : "--";
    const amount = (value) => value == null ? unavailable : this.formatFinancialAmount(value, financialCurrency);
    const percent = (value) => value == null ? unavailable : `${Number(value).toFixed(2)}%`;
    const ratio = (value, suffix = "x") => value == null ? unavailable : `${Number(value).toFixed(2)}${suffix}`;
    const website = this.safeUrl(profile.website);
    const websiteMarkup = website ? `<a href="${this.escape(website)}" target="_blank" rel="noopener noreferrer">公司网站 ↗</a>` : '<span>公司网站待补充</span>';
    const sections = [
      ["业务概况", analysis.business_summary || profile.description],
      ["成长分析", analysis.growth_analysis],
      ["盈利能力", analysis.profitability_analysis],
      ["估值分析", analysis.valuation_analysis],
      ["风险分析", analysis.risk_analysis],
    ].filter(([, content]) => content).map(([title, content]) => `<article><h4>${title}</h4><p>${this.escape(content)}</p></article>`).join("");
    const listItems = (values) => (Array.isArray(values) ? values : []).map((value) => `<li>${this.escape(typeof value === "string" ? value : value?.title || value?.summary || JSON.stringify(value))}</li>`).join("");
    const catalysts = listItems(analysis.catalysts);
    const risks = listItems(analysis.risk_factors);
    return `<section class="ai-fundamental-panel">
      <header><div><span class="eyebrow">FUNDAMENTAL PROFILE</span><h3>${this.escape(companyName)} <small>${this.escape(opportunity.symbol)}</small></h3><p>${this.escape(legalName)}</p></div><strong>${score(analysis.overall_score)}<small>综合评分</small></strong></header>
      <div class="ai-fundamental-status ${complete ? "complete" : "baseline"}"><strong>${financialStatusLabel}</strong><span>${financialStatusCopy}</span><em>覆盖率 ${financials.coverage_pct == null ? "--" : `${Number(financials.coverage_pct).toFixed(0)}%`} · 置信度 ${confidence}</em></div>
      <section class="ai-fundamental-metrics">
        <article><span>总市值</span><b>${this.escape(marketCap)}</b><small>数据库口径</small></article>
        <article><span>所属行业</span><b>${this.escape(industry)}</b><small>${this.escape(sector)}</small></article>
        <article><span>证券类型</span><b>${this.escape(typeLabel)}</b><small>${this.escape(security.exchange || "US")} · ${this.escape(security.currency || "USD")}</small></article>
        <article><span>上市日期</span><b>${this.escape(profile.ipo_date || "--")}</b><small>${this.escape(security.country || "国家待补充")}</small></article>
        <article><span>流通股</span><b>${this.escape(shares)}</b><small>百万股</small></article>
        <article><span>员工人数</span><b>${this.escape(employeeCount)}</b><small>公司档案</small></article>
      </section>
      <section class="ai-fundamental-financials">
        <article><span>TTM 营收</span><b>${amount(financials.revenue_ttm)}</b><small>同比 ${percent(financials.revenue_growth_yoy_pct)}</small></article>
        <article><span>毛利率</span><b>${percent(financials.gross_margin_pct)}</b><small>营业利润率 ${percent(financials.operating_margin_pct)}</small></article>
        <article><span>净利率</span><b>${percent(financials.net_margin_pct)}</b><small>ROE ${percent(financials.return_on_equity_pct)}</small></article>
        <article><span>经营现金流</span><b>${amount(financials.operating_cash_flow_ttm)}</b><small>TTM / 最新年报</small></article>
        <article><span>自由现金流</span><b>${amount(financials.free_cash_flow_ttm)}</b><small>经营现金流减资本开支</small></article>
        <article><span>现金及等价物</span><b>${amount(financials.cash_and_equivalents)}</b><small>最近报告期</small></article>
        <article><span>总债务</span><b>${amount(financials.total_debt)}</b><small>债务权益比 ${ratio(financials.debt_to_equity, "")}</small></article>
        <article><span>估值</span><b>P/E ${ratio(financials.pe_ratio, "")}</b><small>P/S ${ratio(financials.price_to_sales_ratio, "")} · P/B ${ratio(financials.price_to_book_ratio, "")}</small></article>
        <article><span>企业价值</span><b>${amount(financials.enterprise_value)}</b><small>EV/EBITDA ${ratio(financials.ev_to_ebitda, "")}</small></article>
        <article><span>资产 / 负债</span><b>${amount(financials.total_assets)}</b><small>负债 ${amount(financials.total_liabilities)}</small></article>
      </section>
      <section class="ai-fundamental-score-grid">
        <article><span>质量</span><b>${score(analysis.quality_score)}</b></article><article><span>成长</span><b>${score(analysis.growth_score)}</b></article><article><span>估值</span><b>${score(analysis.valuation_score)}</b></article><article><span>财务健康</span><b>${score(analysis.financial_health_score)}</b></article><article><span>综合</span><b>${score(analysis.overall_score)}</b></article>
      </section>
      <section class="ai-fundamental-analysis"><header><h3>基本面分析</h3><small>截至 ${this.escape(analysis.as_of_date || "--")}</small></header><div>${sections || '<article><h4>分析状态</h4><p>基本面分析内容待补充。</p></article>'}</div></section>
      ${catalysts || risks ? `<section class="ai-fundamental-lists">${catalysts ? `<article><h3>潜在催化</h3><ul>${catalysts}</ul></article>` : ""}${risks ? `<article><h3>风险因素</h3><ul>${risks}</ul></article>` : ""}</section>` : ""}
      <footer><span>资料来源：${this.escape(financials.source || profile.source || "数据库")}</span><span>财务快照：${this.escape(financials.snapshot_date || "--")} · 报告期：${this.escape(financials.fiscal_period_end || "--")}</span><span>更新：${this.formatDate(financials.retrieved_at || profile.source_updated_at || security.updated_at)}</span>${websiteMarkup}</footer>
    </section>`;
  }

  closeAiConclusion() {
    const modal = this.q("#ai-conclusion-modal");
    if (!modal || modal.classList.contains("hidden")) return;
    this.closeNewsAnalysisLogic(false);
    this.closeNewsSystemPrompt(false);
    this.closeHistoricalJudgment(false);
    modal.classList.add("hidden");
    modal.setAttribute("aria-hidden", "true");
    this.conclusionNewsRequestId += 1;
    this.conclusionMemoryRequestId += 1;
    this.conclusionFundamentalRequestId += 1;
    this.conclusionOpportunity = null;
    const focusTarget = this.conclusionFocus;
    this.conclusionFocus = null;
    if (focusTarget?.isConnected) focusTarget.focus({ preventScroll: true });
  }

  predictionUrlState() {
    return { params: new URLSearchParams(window.location.search) };
  }

  restorePredictionFiltersFromUrl() {
    const { params } = this.predictionUrlState();
    const clampScore = (value) => Math.min(100, Math.max(0, Number(value) || 0));
    const cleanDate = (value) => /^\d{4}-\d{2}-\d{2}$/.test(String(value || "")) ? String(value) : "";
    const cleanSymbol = (value) => String(value || "").trim().toUpperCase().replace(/[^A-Z0-9._-]/g, "").slice(0, 24);
    const cleanVersion = (value) => String(value || "").trim().slice(0, 32);
    const oneOf = (value, choices, fallback = "all") => choices.includes(value) ? value : fallback;
    let dateFrom = cleanDate(params.get("date_from"));
    let dateTo = cleanDate(params.get("date_to"));
    if (dateFrom && !dateTo) dateTo = dateFrom;
    if (!dateFrom && dateTo) dateFrom = dateTo;
    this.state.predictionFilters = {
      ...this.state.predictionFilters,
      dateFrom,
      dateTo,
      symbol: cleanSymbol(params.get("symbol")),
      newsScoreMin: clampScore(params.get("news_score_min")),
      indicatorScoreMin: clampScore(params.get("indicator_score_min")),
      combinedScoreMin: clampScore(params.get("combined_score_min")),
      optionFlowScoreMin: clampScore(params.get("option_flow_score_min")),
      gexScoreMin: clampScore(params.get("gex_score_min")),
      dataCoverageMin: clampScore(params.get("min_data_coverage")),
      featureVersion: cleanVersion(params.get("feature_version")),
      decisionVersion: cleanVersion(params.get("decision_version")),
      settlementVersion: cleanVersion(params.get("settlement_version")) || "current",
      direction: oneOf(params.get("direction"), ["long", "short"]),
      marketSession: oneOf(params.get("market_session"), ["premarket", "regular", "postmarket", "closed"]),
      quoteQuality: oneOf(params.get("quote_quality"), ["passed", "partial", "blocked", "missing"]),
      eventRisk: oneOf(params.get("event_risk"), ["clear", "warning", "blocked"]),
      exitReason: params.get("exit_reason") && params.get("exit_reason") !== "all" ? String(params.get("exit_reason")).trim().slice(0, 64) : "all",
    };
    this.state.predictionPage = Math.max(1, Math.min(100000, Number(params.get("page")) || 1));
  }

  syncPredictionFilterInputs() {
    const filters = this.state.predictionFilters;
    const values = {
      "prediction-date-from": filters.dateFrom,
      "prediction-date-to": filters.dateTo,
      "prediction-symbol": filters.symbol,
      "prediction-news-score-min": filters.newsScoreMin,
      "prediction-indicator-score-min": filters.indicatorScoreMin,
      "prediction-combined-score-min": filters.combinedScoreMin,
      "prediction-option-flow-score-min": filters.optionFlowScoreMin,
      "prediction-gex-score-min": filters.gexScoreMin,
      "prediction-data-coverage-min": filters.dataCoverageMin,
      "prediction-feature-version": filters.featureVersion,
      "prediction-decision-version": filters.decisionVersion,
      "prediction-settlement-version": filters.settlementVersion,
      "prediction-direction": filters.direction,
      "prediction-market-session": filters.marketSession,
      "prediction-quote-quality": filters.quoteQuality,
      "prediction-event-risk": filters.eventRisk,
      "prediction-exit-reason": filters.exitReason,
    };
    Object.entries(values).forEach(([id, value]) => {
      const input = this.q(`#${id}`);
      if (input) input.value = String(value ?? "");
    });
  }

  renderSettlementVersionOptions(items) {
    const select = this.q("#prediction-settlement-version");
    if (!select) return;
    const versions = Array.isArray(items) ? items : [];
    const selected = String(this.state.predictionFilters.settlementVersion || "current");
    select.innerHTML = [
      '<option value="all">全部版本（跨策略对比）</option>',
      ...versions.map((item) => {
        const value = String(item?.value || "").slice(0, 32);
        if (!value) return "";
        const label = String(item?.label || value);
        const current = item?.current ? " · 当前" : "";
        return `<option value="${this.escape(value)}">${this.escape(label)}${current} · ${this.number(item?.count)}条</option>`;
      }),
    ].join("");
    const available = [...select.options].some((option) => option.value === selected);
    select.value = available
      ? selected
      : versions.find((item) => item?.current)?.value || "all";
    this.state.predictionFilters.settlementVersion = select.value;
  }

  adoptPredictionResponseFilters(responseFilters) {
    if (!responseFilters || typeof responseFilters !== "object") return;
    const filters = this.state.predictionFilters;
    const has = (key) => Object.prototype.hasOwnProperty.call(responseFilters, key);
    const score = (value) => Math.min(100, Math.max(0, Number(value) || 0));
    if (has("date_from")) filters.dateFrom = String(responseFilters.date_from || "");
    if (has("date_to")) filters.dateTo = String(responseFilters.date_to || "");
    if (has("symbol")) filters.symbol = String(responseFilters.symbol || "").toUpperCase();
    if (has("news_score_min")) filters.newsScoreMin = score(responseFilters.news_score_min);
    if (has("indicator_score_min")) filters.indicatorScoreMin = score(responseFilters.indicator_score_min);
    if (has("combined_score_min")) filters.combinedScoreMin = score(responseFilters.combined_score_min);
    if (has("option_flow_score_min")) filters.optionFlowScoreMin = score(responseFilters.option_flow_score_min);
    if (has("gex_score_min")) filters.gexScoreMin = score(responseFilters.gex_score_min);
    if (has("min_data_coverage")) filters.dataCoverageMin = score(responseFilters.min_data_coverage);
    if (has("feature_version")) filters.featureVersion = String(responseFilters.feature_version || "");
    if (has("decision_version")) filters.decisionVersion = String(responseFilters.decision_version || "");
    if (has("settlement_version")) filters.settlementVersion = String(responseFilters.settlement_version || "current");
    if (has("direction")) filters.direction = ["long", "short"].includes(responseFilters.direction) ? responseFilters.direction : "all";
    if (has("market_session")) filters.marketSession = ["premarket", "regular", "postmarket", "closed"].includes(responseFilters.market_session) ? responseFilters.market_session : "all";
    if (has("quote_quality")) filters.quoteQuality = ["passed", "partial", "blocked", "missing"].includes(responseFilters.quote_quality) ? responseFilters.quote_quality : "all";
    if (has("event_risk")) filters.eventRisk = ["clear", "warning", "blocked"].includes(responseFilters.event_risk) ? responseFilters.event_risk : "all";
    if (has("exit_reason")) filters.exitReason = responseFilters.exit_reason && responseFilters.exit_reason !== "all" ? String(responseFilters.exit_reason) : "all";
  }

  syncPredictionFiltersToUrl() {
    const { params } = this.predictionUrlState();
    const filters = this.state.predictionFilters;
    const values = {
      date_from: filters.dateFrom,
      date_to: filters.dateTo,
      symbol: filters.symbol,
      news_score_min: filters.newsScoreMin || "",
      indicator_score_min: filters.indicatorScoreMin || "",
      combined_score_min: filters.combinedScoreMin || "",
      option_flow_score_min: filters.optionFlowScoreMin || "",
      gex_score_min: filters.gexScoreMin || "",
      min_data_coverage: filters.dataCoverageMin || "",
      feature_version: filters.featureVersion,
      decision_version: filters.decisionVersion,
      settlement_version: filters.settlementVersion === "current" ? "" : filters.settlementVersion,
      direction: filters.direction === "all" ? "" : filters.direction,
      market_session: filters.marketSession === "all" ? "" : filters.marketSession,
      quote_quality: filters.quoteQuality === "all" ? "" : filters.quoteQuality,
      event_risk: filters.eventRisk === "all" ? "" : filters.eventRisk,
      exit_reason: filters.exitReason === "all" ? "" : filters.exitReason,
      page: this.state.predictionPage > 1 ? this.state.predictionPage : "",
    };
    Object.entries(values).forEach(([key, value]) => {
      if (value === "" || value === null || value === undefined) params.delete(key);
      else params.set(key, String(value));
    });
    const query = params.toString();
    const nextUrl = `${window.location.pathname}${query ? `?${query}` : ""}${window.location.hash}`;
    const currentUrl = `${window.location.pathname}${window.location.search}${window.location.hash}`;
    if (nextUrl !== currentUrl) window.history.replaceState(window.history.state, "", nextUrl);
  }

  async loadPredictionReadiness({ force = false } = {}) {
    if (this.state.predictionReadinessLoading) return;
    if (!force && this.state.opportunityAnalytics?.readiness) return;
    this.state.predictionReadinessLoading = true;
    try {
      const readiness = await this.api("/opportunity-readiness");
      this.state.opportunityAnalytics = {
        ...(this.state.opportunityAnalytics || {}),
        readiness,
      };
      if (this.state.view === "predictions") this.renderPredictionAnalytics();
    } catch (error) {
      this.showBanner(error.message || "实盘准备度读取失败", "error");
    } finally {
      this.state.predictionReadinessLoading = false;
    }
  }

  async loadPredictionAnalytics({ scrollToList = false, interactive = false, background = false, force = false } = {}) {
    const cacheAge = Date.now() - Number(this.state.predictionAnalyticsLastLoadedAt || 0);
    if (background && !force && (this.state.predictionAnalyticsLoading || cacheAge < 300000)) return;
    if (interactive) this.predictionAnalyticsAbortController?.abort();
    else if (this.state.predictionAnalyticsLoading) return;

    const requestId = ++this.state.predictionAnalyticsRequestId;
    const previousAnalytics = this.state.opportunityAnalytics || {};
    const controller = new AbortController();
    this.predictionAnalyticsAbortController = controller;
    this.state.predictionAnalyticsLoading = true;
    const applyButton = this.q("#prediction-filter-apply");
    const filterForm = this.q("#prediction-filter-form");
    if (interactive && applyButton) {
      applyButton.dataset.idleLabel ||= applyButton.textContent || "应用筛选";
      applyButton.textContent = "正在筛选…";
      applyButton.setAttribute("aria-busy", "true");
    }
    if (interactive) filterForm?.setAttribute("aria-busy", "true");
    try {
      const filters = this.state.predictionFilters;
      const params = new URLSearchParams({
        limit: String(this.state.predictionPageSize),
        page: String(this.state.predictionPage),
        timezone_offset_minutes: String(-new Date().getTimezoneOffset()),
        news_score_min: String(filters.newsScoreMin),
        indicator_score_min: String(filters.indicatorScoreMin),
        combined_score_min: String(filters.combinedScoreMin),
        option_flow_score_min: String(filters.optionFlowScoreMin),
        gex_score_min: String(filters.gexScoreMin),
        direction: filters.direction,
        market_session: filters.marketSession,
        quote_quality: filters.quoteQuality,
        event_risk: filters.eventRisk,
        exit_reason: filters.exitReason,
        settlement_version: filters.settlementVersion || "current",
        include_readiness: "false",
        include_ablation: "false",
      });
      if (filters.dateFrom) params.set("date_from", filters.dateFrom);
      if (filters.dateTo) params.set("date_to", filters.dateTo);
      if (filters.symbol) params.set("symbol", filters.symbol);
      params.set("min_data_coverage", String(filters.dataCoverageMin));
      if (filters.featureVersion) params.set("feature_version", filters.featureVersion);
      if (filters.decisionVersion) params.set("decision_version", filters.decisionVersion);
      const data = await this.api(`/opportunity-analytics?${params}`, { signal: controller.signal });
      if (requestId !== this.state.predictionAnalyticsRequestId) return;
      const currentAnalytics = this.state.opportunityAnalytics || {};
      this.state.opportunityAnalytics = {
        ...previousAnalytics,
        ...currentAnalytics,
        ...data,
        readiness: data.readiness || currentAnalytics.readiness || previousAnalytics.readiness || null,
      };
      this.adoptPredictionResponseFilters(data.filters);
      this.renderSettlementVersionOptions(data.settlement_versions);
      this.state.predictionPage = Number(data.pagination?.page || this.state.predictionPage || 1);
      this.state.predictionAnalyticsLastLoadedAt = Date.now();
      if (!background) {
        this.syncPredictionFilterInputs();
        this.syncPredictionFiltersToUrl();
      }
      this.renderPredictionAnalytics();
      if (scrollToList) window.requestAnimationFrame(() => this.q("#prediction-list")?.scrollIntoView({ behavior: "auto", block: "start" }));
    } catch (error) {
      if (error?.name === "AbortError") return;
      this.showBanner(error.message || "预测统计分析读取失败", "error");
    } finally {
      if (requestId === this.state.predictionAnalyticsRequestId) {
        this.state.predictionAnalyticsLoading = false;
        if (this.predictionAnalyticsAbortController === controller) this.predictionAnalyticsAbortController = null;
      }
      if (interactive && applyButton && requestId === this.state.predictionAnalyticsRequestId) {
        applyButton.textContent = applyButton.dataset.idleLabel || "应用筛选";
        applyButton.removeAttribute("aria-busy");
        filterForm?.removeAttribute("aria-busy");
      }
    }
  }

  applyPredictionFilters() {
    const clampScore = (value) => Math.min(100, Math.max(0, Number(value) || 0));
    let dateFrom = this.q("#prediction-date-from").value;
    let dateTo = this.q("#prediction-date-to").value;
    if (dateFrom && !dateTo) dateTo = dateFrom;
    if (!dateFrom && dateTo) dateFrom = dateTo;
    if (dateFrom && dateTo && dateFrom > dateTo) {
      this.showBanner("信号开始日期不能晚于结束日期。", "error");
      return;
    }
    const directionValue = this.q("#prediction-direction").value;
    const marketSessionValue = this.q("#prediction-market-session").value;
    const quoteQualityValue = this.q("#prediction-quote-quality").value;
    const eventRiskValue = this.q("#prediction-event-risk").value;
    const exitReasonValue = this.q("#prediction-exit-reason").value;
    this.state.predictionFilters = {
      dateFrom,
      dateTo,
      symbol: this.q("#prediction-symbol").value.trim().toUpperCase().replace(/[^A-Z0-9._-]/g, "").slice(0, 24),
      newsScoreMin: clampScore(this.q("#prediction-news-score-min").value),
      indicatorScoreMin: clampScore(this.q("#prediction-indicator-score-min").value),
      combinedScoreMin: clampScore(this.q("#prediction-combined-score-min").value),
      optionFlowScoreMin: clampScore(this.q("#prediction-option-flow-score-min").value),
      gexScoreMin: clampScore(this.q("#prediction-gex-score-min").value),
      dataCoverageMin: clampScore(this.q("#prediction-data-coverage-min").value),
      featureVersion: this.q("#prediction-feature-version").value.trim().slice(0, 32),
      decisionVersion: this.q("#prediction-decision-version").value.trim().slice(0, 32),
      settlementVersion: this.q("#prediction-settlement-version").value || "current",
      direction: ["long", "short"].includes(directionValue) ? directionValue : "all",
      marketSession: ["premarket", "regular", "postmarket", "closed"].includes(marketSessionValue) ? marketSessionValue : "all",
      quoteQuality: ["passed", "partial", "blocked", "missing"].includes(quoteQualityValue) ? quoteQualityValue : "all",
      eventRisk: ["clear", "warning", "blocked"].includes(eventRiskValue) ? eventRiskValue : "all",
      exitReason: exitReasonValue && exitReasonValue !== "all" ? exitReasonValue : "all",
    };
    this.syncPredictionFilterInputs();
    this.state.predictionPage = 1;
    this.syncPredictionFiltersToUrl();
    this.loadPredictionAnalytics({ scrollToList: true, interactive: true, force: true });
  }

  resetPredictionFilters() {
    this.state.predictionFilters = {
      dateFrom: "",
      dateTo: "",
      symbol: "",
      newsScoreMin: 0,
      indicatorScoreMin: 0,
      combinedScoreMin: 0,
      optionFlowScoreMin: 0,
      gexScoreMin: 0,
      dataCoverageMin: 0,
      featureVersion: "",
      decisionVersion: "",
      settlementVersion: "current",
      direction: "all",
      marketSession: "all",
      quoteQuality: "all",
      eventRisk: "all",
      exitReason: "all",
    };
    this.syncPredictionFilterInputs();
    this.state.predictionPage = 1;
    this.syncPredictionFiltersToUrl();
    this.loadPredictionAnalytics({ scrollToList: true, interactive: true, force: true });
  }

  setPredictionPage(page) {
    const totalPages = Number(this.state.opportunityAnalytics?.pagination?.total_pages || 1);
    const nextPage = Math.min(totalPages, Math.max(1, Number(page) || 1));
    if (nextPage === this.state.predictionPage) return;
    this.state.predictionPage = nextPage;
    this.syncPredictionFiltersToUrl();
    this.loadPredictionAnalytics({ scrollToList: true, interactive: true, force: true });
  }

  async startHistoricalReplay(event) {
    event.preventDefault();
    if (this.historicalReplayActive()) return;
    const button = this.q("#replay-start");
    button.disabled = true;
    try {
      const payload = {
        days: Number(this.q("#replay-days").value) || 365,
        timeframe: this.q("#replay-timeframe").value,
        symbols: [],
      };
      const replay = await this.api("/replays", { method: "POST", body: JSON.stringify(payload) });
      const replayData = this.state.historicalReplays || {};
      const previousItems = Array.isArray(replayData.items) ? replayData.items : [];
      this.state.historicalReplays = {
        ...replayData,
        items: [replay, ...previousItems.filter((item) => item.id !== replay.id)],
      };
      this.renderHistoricalReplay();
      this.showBanner("历史回放已启动；任务会从币安官方归档补采并校验数据。", "success");
      await this.loadPredictionAnalytics({ force: true, background: true });
    } catch (error) {
      this.showBanner(error.message || "历史回放启动失败", "error");
    } finally {
      this.syncHistoricalReplayButton();
    }
  }

  historicalReplayActive() {
    const items = this.state.historicalReplays?.items || [];
    return items.some((item) => ["pending", "running"].includes(item.status));
  }

  syncHistoricalReplayButton() {
    const button = this.q("#replay-start");
    if (button) button.disabled = this.historicalReplayActive();
  }

  renderHistoricalReplay() {
    const replayData = this.state.historicalReplays || {};
    const items = replayData.items || [];
    const latest = items[0] || null;
    const readiness = (this.state.opportunityAnalytics || {}).historical_replay_readiness || replayData.readiness || {};
    const status = this.q("#replay-status");
    const labels = { pending: "排队中", running: "正在回放", completed: "已完成", failed: "失败", cancelled: "已取消" };
    status.textContent = latest ? `${labels[latest.status] || latest.status} · ${latest.completed_symbols}/${latest.total_symbols} 品种` : "尚未运行";
    status.className = latest?.status || "idle";
    this.syncHistoricalReplayButton();
    const criteria = Array.isArray(readiness.criteria) ? readiness.criteria : [];
    const progress = latest && latest.total_symbols ? Math.round(latest.completed_symbols / latest.total_symbols * 100) : 0;
    this.q("#replay-result").innerHTML = `
      <div class="replay-progress"><span style="width:${progress}%"></span></div>
      <div class="replay-summary">
        <article><span>任务进度</span><b>${latest ? `${progress}%` : "--"}</b><small>${latest ? `${this.number(latest.generated_signals)} 个去重信号` : "等待首次运行"}</small></article>
        <article><span>样本外信号</span><b>${this.number(readiness.oos_summary?.sample_count)}</b><small>多 ${this.number(readiness.oos_summary?.long_count)} / 空 ${this.number(readiness.oos_summary?.short_count)}</small></article>
        <article><span>样本外平均 ${this.state.displayLeverage}x 仓位ROE</span><b class="${Number(readiness.oos_summary?.average_net_return_bps || 0) > 0 ? "positive" : "negative"}">${readiness.oos_summary?.average_net_return_bps == null ? "--" : this.formatLeveragedReturnFromBps(readiness.oos_summary.average_net_return_bps)}</b><small>标的 ${readiness.oos_summary?.average_net_return_bps == null ? "--" : this.formatBps(readiness.oos_summary.average_net_return_bps)} · 强制成本后 · 非账户收益率</small></article>
        <article><span>样本外准入</span><b class="${readiness.quantitative_ready ? "positive" : "negative"}">${readiness.passed_count || 0} / ${readiness.total_count || 8}</b><small>${readiness.quantitative_ready ? "可进入影子验证" : "仍限研究"}</small></article>
      </div>
      ${criteria.length ? `<div class="replay-criteria">${criteria.map((item) => `<span class="${item.passed ? "passed" : "blocked"}">${item.passed ? "✓" : "×"} ${this.escape(item.label)} <b>${item.current == null ? "--" : this.escape(item.current)}</b></span>`).join("")}</div>` : `<p class="replay-note">${this.escape(readiness.note || "完成回放后显示样本外准入结果。")}</p>`}
      ${latest?.error ? `<p class="replay-error">${this.escape(latest.error)}</p>` : ""}`;
  }

  predictionFeatureSnapshot(item) {
    const snapshot = this.normalizeFeatureSnapshot(item);
    const availability = item?.feature_availability && typeof item.feature_availability === "object" ? item.feature_availability : {};
    const quoteCheckKeys = ["price_available", "ticker_fresh", "quote_fresh", "spread_acceptable", "quote_sane", "not_halted"];
    const quoteCheckValues = quoteCheckKeys.filter((key) => Object.prototype.hasOwnProperty.call(item?.gate_summary?.checks || {}, key)).map((key) => item.gate_summary.checks[key]);
    const gateQuotePassed = quoteCheckValues.some((value) => value === false) ? false : quoteCheckValues.length ? quoteCheckValues.every((value) => value === true) : undefined;
    const quotePassed = this.firstValue(snapshot.quote.passed, snapshot.quote.quality_passed, gateQuotePassed, item?.quote_quality_passed);
    const explicitQuoteQuality = String(item?.quote_quality || "").toLowerCase();
    const quoteQuality = ["passed", "partial", "blocked", "missing"].includes(explicitQuoteQuality)
      ? explicitQuoteQuality
      : quotePassed === false
      ? "blocked"
      : quotePassed === true
      ? "passed"
      : "missing";
    const session = String(this.firstValue(item?.market_session, snapshot.quote.market_session, snapshot.quote.session, "unknown")).toLowerCase();
    const event = snapshot.riskEvents[0] || {};
    const stableEventClear = item?.gate_summary?.checks && Object.prototype.hasOwnProperty.call(item.gate_summary.checks, "event_window_clear") ? item.gate_summary.checks.event_window_clear : null;
    const eventRisk = String(this.firstValue(item?.event_risk, event.risk_level, event.severity, stableEventClear === false ? "blocked" : stableEventClear === true ? "clear" : "unknown")).toLowerCase();
    const coverageRaw = Number(this.firstValue(snapshot.dataQuality.coverage_ratio, snapshot.dataQuality.coverage, snapshot.dataQuality.score, item?.data_coverage));
    const coverage = Number.isFinite(coverageRaw) ? Math.max(0, Math.min(100, coverageRaw <= 1 ? coverageRaw * 100 : coverageRaw)) : null;
    const score = (source, ...keys) => {
      const value = this.firstValue(...keys.map((key) => source?.[key]));
      return value == null || !Number.isFinite(Number(value)) ? null : Number(value);
    };
    return {
      quoteQuality,
      quoteAgeMs: score(snapshot.quote, "age_ms", "quote_age_ms", "last_trade_age_ms", "latency_ms"),
      spreadBps: score(snapshot.quote, "spread_bps", "bid_ask_spread_bps"),
      session,
      optionScore: score(snapshot.optionFlow, "score", "directional_score", "flow_score"),
      optionBias: String(this.firstValue(snapshot.optionFlow.bias, snapshot.optionFlow.direction, "unknown")),
      gexScore: score(snapshot.gex, "score", "directional_score"),
      gexRegime: String(this.firstValue(snapshot.gex.regime, snapshot.gex.gamma_regime, "unknown")),
      institutionalScore: score(snapshot.institutional, "score", "confirmation_score"),
      eventRisk,
      coverage,
      apiVersion: snapshot.apiVersion,
      featureVersion: this.firstValue(snapshot.version.feature, item?.feature_version, item?.feature_set_version, snapshot.dataQuality.version, "--"),
      decisionVersion: this.firstValue(snapshot.version.decision, item?.decision_version, item?.strategy_version, item?.policy_version, "--"),
      snapshotId: this.firstValue(snapshot.signalSnapshot.id, item?.feature_snapshot_id, item?.snapshot_id, "--"),
      availability,
    };
  }

  predictionFeatureCells(item) {
    const feature = this.predictionFeatureSnapshot(item);
    const sessionLabel = ({ premarket: "盘前", regular: "盘中", postmarket: "盘后", closed: "休市", unknown: "时段未知" })[feature.session] || feature.session;
    const quoteReason = String(feature.availability?.quote?.reason || "");
    const quoteLabel = feature.quoteQuality === "passed"
      ? "参考 NBBO 已通过"
      : feature.quoteQuality === "partial" && quoteReason === "reference_quote_stale"
      ? "参考盘口已过期"
      : feature.quoteQuality === "partial"
      ? "现货价快照（非盘口）"
      : feature.quoteQuality === "blocked"
      ? "参考行情异常"
      : "参考行情缺失";
    const reasonLabel = (reason) => ({
      available: "已采集",
      stale: "信号时已过期",
      channel_disabled: "采集通道关闭",
      uw_disabled_at_signal: "采集关闭",
      legacy_snapshot_missing: "历史未冻结",
      market_feature_not_linked: "信号时无快照",
      not_captured_at_signal: "信号时无数据",
      finnhub_last_trade_only: "Finnhub 现货快照",
      last_trade_only: "仅现货价",
      reference_quote_stale: "参考盘口已过期",
      reference_quote_blocked: "参考行情异常",
      no_signal_time_quote: "信号时无行情",
    })[String(reason || "")] || "未采集";
    const optionReason = reasonLabel(feature.availability?.option_flow?.reason);
    const gexReason = reasonLabel(feature.availability?.gex?.reason);
    const institutionalReason = reasonLabel(feature.availability?.institutional_flow?.reason);
    const eventTone = ["critical", "blocked", "high"].includes(feature.eventRisk) ? "blocked" : ["medium", "warning"].includes(feature.eventRisk) ? "warning" : ["clear", "normal"].includes(feature.eventRisk) ? "passed" : "missing";
    const eventLabel = ({ clear: "无临近事件", normal: "无临近事件", medium: "事件预警", warning: "事件预警", high: "高风险事件", critical: "重大事件阻断", blocked: "事件阻断", unknown: "事件数据缺失" })[feature.eventRisk] || feature.eventRisk;
    return `<td><span class="prediction-context ${feature.quoteQuality}">${this.escape(sessionLabel)} · ${this.escape(quoteLabel)}</span><small>${feature.quoteAgeMs == null ? reasonLabel(feature.availability?.quote?.reason) : `信号时延 ${Math.round(feature.quoteAgeMs)}ms`} · ${feature.spreadBps == null ? "无 NBBO 点差" : `${feature.spreadBps.toFixed(1)} bps`}</small></td>
      <td><span class="prediction-feature-score">期权 ${feature.optionScore == null ? this.escape(optionReason) : feature.optionScore.toFixed(1)}</span><small>GEX ${feature.gexScore == null ? this.escape(gexReason) : feature.gexScore.toFixed(1)} · 机构 ${feature.institutionalScore == null ? this.escape(institutionalReason) : feature.institutionalScore.toFixed(1)}</small></td>
      <td><span class="prediction-context ${eventTone}">${this.escape(eventLabel)}</span><small>覆盖 ${feature.coverage == null ? "--" : `${feature.coverage.toFixed(0)}%`} · API ${this.escape(feature.apiVersion)}</small><small>F ${this.escape(feature.featureVersion)} / D ${this.escape(feature.decisionVersion)} · 快照 ${this.escape(feature.snapshotId)}</small></td>`;
  }

  renderMarketAblation(ablation) {
    const target = this.q("#market-ablation");
    if (!target) return;
    if (ablation?.status === "deferred") {
      target.className = "market-ablation hidden";
      target.innerHTML = "";
      return;
    }
    const variants = Array.isArray(ablation?.variants) ? ablation.variants : [];
    if (!ablation || ablation.status === "unavailable" || !variants.length) {
      target.className = "market-ablation unavailable";
      target.innerHTML = `<header><div><span>FROZEN SIGNAL ABLATION</span><h3>市场模块消融对比暂不可用</h3><p>尚无带信号时刻冻结快照的已结算样本；系统不会用当前行情、0 分或中性值补齐。</p></div><strong>不可用<small>等待冻结样本</small></strong></header>`;
      return;
    }
    const preferredOrder = ["baseline", "quote_halt", "option_flow", "full"];
    const ordered = preferredOrder.map((key) => variants.find((item) => item.key === key)).filter(Boolean);
    const metric = (value, suffix = "", digits = 1) => value == null || !Number.isFinite(Number(value)) ? "--" : `${Number(value).toFixed(digits)}${suffix}`;
    const coverage = ablation.data_coverage && typeof ablation.data_coverage === "object" ? ablation.data_coverage : {};
    const coverageLabels = { quote_halt: "Quote/Halt", option_flow: "Option Flow", gex: "GEX", institutional_flow: "机构流", event_window: "事件窗" };
    const cards = ordered.map((variant, index) => {
      const unavailable = variant.status === "unavailable";
      const selected = Number(variant.sample_count || 0);
      const available = Number(variant.available_count || 0);
      const total = Number(variant.total_settled_count || ablation.total_settled_count || 0);
      return `<article class="ablation-variant ${unavailable ? "unavailable" : "available"}" data-ablation-variant="${this.escape(variant.key)}">
        <header><span>${String(index + 1).padStart(2, "0")}</span><div><strong>${this.escape(variant.label || variant.key)}</strong><small>${unavailable ? "冻结数据缺失" : `保留 ${this.number(selected)} · 拒绝 ${this.number(variant.rejected_count)}`}</small></div><b>${unavailable ? "不可用" : `${metric(variant.data_coverage_rate, "%", 0)} 覆盖`}</b></header>
        <dl>
          <div><dt>可用样本</dt><dd>${unavailable ? "--" : `${this.number(available)} / ${this.number(total)}`}</dd></div>
          <div><dt>命中率</dt><dd>${unavailable ? "--" : metric(variant.hit_rate, "%")}</dd></div>
          <div><dt>净期望</dt><dd class="${Number(variant.average_net_return_bps || 0) >= 0 ? "positive" : "negative"}">${unavailable ? "--" : metric(variant.average_net_return_bps, " bps", 2)}</dd></div>
          <div><dt>Profit Factor</dt><dd>${unavailable ? "--" : metric(variant.profit_factor, "", 2)}</dd></div>
          <div><dt>最大回撤</dt><dd class="negative">${unavailable ? "--" : metric(variant.maximum_drawdown_bps, " bps", 2)}</dd></div>
          <div><dt>信号保留</dt><dd>${unavailable ? "--" : metric(variant.signal_retention_rate, "%", 0)}</dd></div>
        </dl>
      </article>`;
    }).join("");
    const coverageMarkup = Object.entries(coverageLabels).map(([key, label]) => {
      const item = coverage[key] || {};
      const unavailable = item.status === "unavailable" || Number(item.available_count || 0) === 0;
      return `<span class="${unavailable ? "missing" : "available"}"><i></i>${this.escape(label)} <b>${unavailable ? "不可用" : metric(item.coverage_rate, "%", 0)}</b></span>`;
    }).join("");
    target.className = "market-ablation";
    target.innerHTML = `
      <header><div><span>FROZEN SIGNAL ABLATION</span><h3>市场数据模块消融对比</h3><p>使用信号生成时冻结证据，对比逐层加入 Quote/Halt、Option Flow 与完整市场模块后的已结算表现。</p></div><strong>${ablation.causal_replay === true ? "因果回放" : "观察性消融"}<small>${this.number(ablation.total_settled_count)} 条已结算样本</small></strong></header>
      <div class="ablation-variants">${cards}</div>
      <footer><div class="ablation-coverage"><strong>冻结数据覆盖</strong>${coverageMarkup}</div><p>${ablation.causal_replay === true ? "结果来自因果回放。" : "不是反事实因果回放：只衡量已生成信号的保留结果，未生成的信号不能据此推断。"}</p></footer>`;
  }

  renderPredictionAnalytics() {
    const data = this.state.opportunityAnalytics || {};
    const summary = data.summary || {};
    const items = data.items || [];
    const readiness = data.readiness || {};
    const costConfig = data.cost_config || {};
    const metricBps = (value) => value == null ? "--" : this.formatBps(value);
    const hitRate = summary.hit_rate == null ? "--" : `${Number(summary.hit_rate).toFixed(1)}%`;
    const responseFilters = data.filters || {};
    const filters = {
      date_from: this.firstValue(responseFilters.date_from, this.state.predictionFilters.dateFrom, ""),
      date_to: this.firstValue(responseFilters.date_to, this.state.predictionFilters.dateTo, ""),
      symbol: this.firstValue(responseFilters.symbol, this.state.predictionFilters.symbol, ""),
      news_score_min: this.firstValue(responseFilters.news_score_min, this.state.predictionFilters.newsScoreMin, 0),
      indicator_score_min: this.firstValue(responseFilters.indicator_score_min, this.state.predictionFilters.indicatorScoreMin, 0),
      combined_score_min: this.firstValue(responseFilters.combined_score_min, this.state.predictionFilters.combinedScoreMin, 0),
      option_flow_score_min: this.firstValue(responseFilters.option_flow_score_min, this.state.predictionFilters.optionFlowScoreMin, 0),
      gex_score_min: this.firstValue(responseFilters.gex_score_min, this.state.predictionFilters.gexScoreMin, 0),
      min_data_coverage: this.firstValue(responseFilters.min_data_coverage, this.state.predictionFilters.dataCoverageMin, 0),
      feature_version: this.firstValue(responseFilters.feature_version, this.state.predictionFilters.featureVersion, ""),
      decision_version: this.firstValue(responseFilters.decision_version, this.state.predictionFilters.decisionVersion, ""),
      settlement_version: this.firstValue(responseFilters.settlement_version, this.state.predictionFilters.settlementVersion, "current"),
      direction: this.firstValue(responseFilters.direction, this.state.predictionFilters.direction, "all"),
      market_session: this.firstValue(responseFilters.market_session, this.state.predictionFilters.marketSession, "all"),
      quote_quality: this.firstValue(responseFilters.quote_quality, this.state.predictionFilters.quoteQuality, "all"),
      event_risk: this.firstValue(responseFilters.event_risk, this.state.predictionFilters.eventRisk, "all"),
      exit_reason: this.firstValue(responseFilters.exit_reason, this.state.predictionFilters.exitReason, "all"),
    };
    const directionLabel = ({ long: "做多", short: "做空", all: "全部方向" })[filters.direction] || "全部方向";
    const sessionLabel = ({ premarket: "盘前", regular: "盘中", postmarket: "盘后", closed: "休市", all: "全部时段" })[filters.market_session] || "全部时段";
    const filterResult = this.q("#prediction-filter-result");
    const dateScope = filters.date_from && filters.date_to && filters.date_from === filters.date_to
      ? `日期 ${filters.date_from}`
      : [filters.date_from && `从 ${filters.date_from}`, filters.date_to && `至 ${filters.date_to}`].filter(Boolean).join(" · ");
    const scopeLabel = [dateScope, filters.symbol && `股票 ${filters.symbol}`, filters.feature_version && `F ${filters.feature_version}`, filters.decision_version && `D ${filters.decision_version}`, filters.settlement_version && `策略 ${filters.settlement_version}`].filter(Boolean).join(" · ");
    if (filterResult) filterResult.textContent = `${this.number(summary.historical_count)} 条 · ${directionLabel} · ${sessionLabel} · 新闻 ≥ ${Number(filters.news_score_min || 0).toFixed(0)} · 指标 ≥ ${Number(filters.indicator_score_min || 0).toFixed(0)} · 覆盖 ≥ ${Number(filters.min_data_coverage || 0).toFixed(0)}%${scopeLabel ? ` · ${scopeLabel}` : ""}`;
    const costTotal = this.q("#prediction-cost-total");
    if (costTotal && costConfig.example_one_hour_total_bps != null) costTotal.textContent = `1h 往返 ${Number(costConfig.example_one_hour_total_bps).toFixed(2)} bps`;
    const readinessTarget = this.q("#strategy-readiness");
    if (readinessTarget) {
      const criteria = Array.isArray(readiness.criteria) ? readiness.criteria : [];
      readinessTarget.innerHTML = `
        <header><div><span>LIVE READINESS GATE</span><h3>${readiness.quantitative_ready ? "量化门槛已通过，可进入影子验证" : "当前仅限研究，尚未达到影子验证门槛"}</h3><p>${this.escape(readiness.note || "量化门槛通过后仍需模拟盘、影子运行和人工审批。")}</p></div><strong class="${readiness.quantitative_ready ? "passed" : "blocked"}">${this.number(readiness.passed_count)} / ${this.number(readiness.total_count)}<small>通过项</small></strong></header>
        <div class="readiness-criteria">${criteria.map((item) => `<article class="${item.passed ? "passed" : "blocked"}"><span>${item.passed ? "✓" : "×"} ${this.escape(item.label)}</span><b>${item.current == null ? "--" : this.escape(item.current)}</b><small>要求 ${this.escape(item.required)}</small></article>`).join("")}</div>
        <footer><strong>实盘仍需完成</strong>${(readiness.paper_and_shadow_requirements || []).map((item) => `<span>${this.escape(item)}</span>`).join("")}</footer>`;
    }
    const exitPolicyTarget = this.q("#adaptive-exit-policy");
    if (exitPolicyTarget) {
      const exitCounts = summary.exit_reason_counts || {};
      const protectedCount = Number(exitCounts.profit_lock || 0) + Number(exitCounts.trailing_profit || 0);
      const policyVersion = String(summary.settlement_policy_version || "--");
      const isV7 = policyVersion.endsWith("_v7");
      const isV6 = policyVersion.endsWith("_v6");
      const policyTag = policyVersion === "all"
        ? "MULTI-VERSION EXIT COMPARISON"
        : isV7
          ? "RISK UNIT EXIT GUARD V7"
          : isV6
            ? "FROZEN EXIT GUARD V6"
            : "FROZEN LEGACY EXIT GUARD";
      const policyTitle = policyVersion === "all"
        ? "跨结算策略版本对比"
        : isV7
          ? "R 单位递进锁盈与失败跟随早退"
          : "历史结算规则审计视图";
      const policyDescription = policyVersion === "all"
        ? "当前结果混合多个冻结策略版本，只适合横向审计；各版本不会互相重算。建议逐一选择版本比较命中率和成本后 ROE。"
        : isV7
          ? "浮盈覆盖交易成本并达到 0.5R 后，至少锁定成本后的 0.25R 净利润，并按峰值回撤 0.35R 递进抬高保护线；达到 1R 后切换为回撤 0.5R。旧版本规则保持冻结，不重写历史结果。"
          : isV6
            ? "V6 在 0.5R 后只锁定成本加 2bps，可能显示约 0.2% 的 10x 模拟 ROE。该规则已经停用，仅保留历史审计。"
            : "该版本使用生成信号时冻结的历史退出规则，仅供复盘，不会用 V7 重新计算。";
      exitPolicyTarget.innerHTML = `
        <header><div><span>${this.escape(policyTag)}</span><h3>${this.escape(policyTitle)}</h3><p>${this.escape(policyDescription)}</p></div><strong>虚拟回放<small>${this.escape(policyVersion)}</small></strong></header>
        <div>
          <article class="profit"><span>递进锁盈</span><b>0.5R / 净锁 0.25R</b><small>先覆盖交易成本，再锁定至少 0.25R；保护线只升不降 · 已触发 ${this.number(exitCounts.profit_lock)}</small></article>
          <article class="profit"><span>移动保护</span><b>1R / 回吐 0.5R</b><small>按本笔 ATR 风险归一，不再使用所有品种统一 30 bps · 已触发 ${this.number(exitCounts.trailing_profit)}</small></article>
          <article class="risk"><span>跟随失败</span><b>仍按持有周期确认</b><small>与盈利保护分离，避免短线噪声秒平 · 已触发 ${this.number(exitCounts.failed_follow_through)}</small></article>
          <article><span>保护退出合计</span><b>${this.number(protectedCount)}</b><small>ATR 硬止损、2R 止盈和评分反转仍然保留</small></article>
        </div>`;
    }
    this.renderMarketAblation(data.ablation);
    this.q("#prediction-note").textContent = `${data.note || "按历史预测的成本后结果统计，不执行任何下单。"} 页面展示 ${this.state.displayLeverage}x 仓位保证金 ROE，并保留标的原始收益；仓位 ROE 不代表账户总收益。`;
    this.q("#analytics-summary").innerHTML = `
      <article><span>筛选样本</span><strong>${this.number(summary.historical_count)}</strong><small>当前策略 ${this.escape(summary.settlement_policy_version || "--")} · 等待 ${this.number(summary.pending_count)} · 已剔除行情不足 ${this.number(summary.discarded_unavailable_count)}</small></article>
      <article><span>多 / 空方向</span><strong class="analytics-directions"><b>${this.number(summary.long_count)}</b><i>/</i><em>${this.number(summary.short_count)}</em></strong><small>做多 / 做空样本分布</small></article>
      <article class="positive"><span>命中次数</span><strong>${this.number(summary.win_count)}</strong><small>未命中 ${this.number(summary.loss_count)} · 持平 ${this.number(summary.flat_count)}</small></article>
      <article class="${Number(summary.hit_rate || 0) >= 50 ? "positive" : "negative"}"><span>命中概率</span><strong>${hitRate}</strong><small>命中 ÷ 有方向结果</small></article>
      <article class="${Number(summary.average_directional_return_bps || 0) >= 0 ? "positive" : "negative"}"><span>平均虚拟持仓 ${this.state.displayLeverage}x 成本后 ROE</span><strong>${this.formatLeveragedReturnFromBps(summary.average_directional_return_bps)}</strong><small>标的净 ${metricBps(summary.average_directional_return_bps)} · 毛 ${metricBps(summary.average_gross_return_bps)}</small><small>${this.state.displayLeverage}x 成本 ${this.formatLeveragedReturnFromBps(-Math.abs(Number(summary.average_estimated_cost_bps || 0)))} · 模拟值，非账户实际收益</small></article>
      <article><span>平均 MFE / MAE</span><strong class="analytics-range"><b>${metricBps(summary.average_max_favorable_bps)}</b><i>${metricBps(summary.average_max_adverse_bps)}</i></strong><small>持有期最大有利 / 不利波动</small></article>
      <article><span>影子候选 / 研究</span><strong class="analytics-directions"><b>${this.number(summary.shadow_ready_count)}</b><i>/</i><em>${this.number(summary.research_only_count)}</em></strong><small>生成信号时的准入状态</small></article>`;
    const target = this.q("#prediction-list");
    if (!items.length) { target.innerHTML = '<div class="empty-state opportunity-empty"><strong>没有符合当前筛选条件的历史机会</strong><span>请降低评分下限、切换方向或重置筛选。</span></div>'; return; }
    const analyticsScore = (value) => value == null || !Number.isFinite(Number(value)) ? "--" : Number(value).toFixed(1);
    const predictionRows = items.map((item) => `<tr>
      <td>${this.formatDate(item.signal_time)}<small>${this.escape(this.firstValue(item.signal_id, item.opportunity_id, ""))}</small></td>
      <td><strong>${this.escape(item.symbol)}</strong><small>${this.escape(item.contract_symbol)}</small></td>
      <td><span class="prediction-direction ${item.direction === "short" ? "short" : ""}">${item.direction === "long" ? "做多" : "做空"}</span></td>
      <td><span class="technical-state ${item.technical_confirmed ? "confirmed" : "candidate"}">${item.technical_confirmed ? "技术已确认" : "新闻候选"}</span><small>${this.escape(this.firstValue(item.lifecycle_state, item.entry_state, "--"))}</small></td>
      <td>${analyticsScore(item.news_score)} / ${analyticsScore(item.indicator_score)}<small>组合 ${analyticsScore(item.combined_score)}</small></td>
      ${this.predictionFeatureCells(item)}
      <td>${item.entry_price == null ? "--" : this.escape(this.compactNumber(item.entry_price))}<small>${item.entry_at ? this.formatDate(item.entry_at) : "入场时刻快照"}</small></td>
      <td>${item.exit_price == null ? "--" : this.escape(this.compactNumber(item.exit_price))}<small>${this.exitReasonLabel(item.exit_reason, item.exit_detail)} · ${item.settled_price_at ? this.formatDate(item.settled_price_at) : "退出行情不足"}</small><small>策略 ${this.escape(item.settlement_version || "--")}</small></td>
      <td class="${Number(item.net_directional_return_bps || 0) >= 0 ? "positive" : "negative"}">${this.formatLeveragedReturnFromBps(item.net_directional_return_bps)}<small>毛利润率 ${this.formatLeveragedReturnFromBps(item.gross_directional_return_bps)} · 标的净 ${metricBps(item.net_directional_return_bps)}</small><small>${this.state.displayLeverage}x 成本 ${this.formatLeveragedReturnFromBps(-Math.abs(Number(item.estimated_cost_bps || 0)))} · 强制保守成本</small></td>
      <td>${metricBps(item.max_favorable_bps)}<small>${metricBps(item.max_adverse_bps)}</small></td>
      <td><span class="prediction-result ${this.escape(item.result || "unavailable")}">${this.analyticsResultLabel(item.result)}</span></td>
      <td>${this.formatDate(item.expires_at)}<small>计划 ${this.number(item.max_holding_bars || 1)} 根 · ${this.formatDuration(item.signal_time, item.expires_at)}</small><small>实际 ${this.formatDuration(item.signal_time, item.settled_price_at || item.exit_at)}</small></td>
    </tr>`).join("");
    target.innerHTML = `<div class="table-wrap enhanced-analytics-table"><table><thead><tr><th>信号时间</th><th>股票 / 合约</th><th>方向</th><th>技术状态</th><th>新闻 / 指标评分</th><th>时段 / 行情质量</th><th>期权 / GEX / 机构</th><th>事件 / 数据覆盖</th><th>入场价格</th><th>退出价格 / 原因 / 策略</th><th>虚拟持仓 ${this.state.displayLeverage}x 成本后 ROE</th><th>MFE / MAE</th><th>成本后结果</th><th>计划 / 实际持有</th></tr></thead><tbody>${predictionRows}</tbody></table></div>`;
    target.insertAdjacentHTML("beforeend", this.predictionPaginationMarkup(data.pagination || {}, items.length));
  }

  predictionPaginationMarkup(pagination, visibleCount) {
    const page = Math.max(1, Number(pagination.page) || 1);
    const pageSize = Math.max(1, Number(pagination.page_size) || this.state.predictionPageSize);
    const total = Math.max(0, Number(pagination.total) || 0);
    const totalPages = Math.max(1, Number(pagination.total_pages) || Math.ceil(total / pageSize) || 1);
    const pageEntries = [];
    if (totalPages <= 7) {
      for (let value = 1; value <= totalPages; value += 1) pageEntries.push(value);
    } else {
      const candidates = [...new Set([1, page - 1, page, page + 1, totalPages].filter((value) => value >= 1 && value <= totalPages))].sort((left, right) => left - right);
      candidates.forEach((value, index) => {
        if (index && value - candidates[index - 1] > 1) pageEntries.push("ellipsis");
        pageEntries.push(value);
      });
    }
    return `<nav class="prediction-pagination" aria-label="预测统计分析分页">
      <span>共 <strong>${this.number(total)}</strong> 条 · 每页 ${this.number(pageSize)} 条 · 当前显示 ${this.number(visibleCount)} 条</span>
      <div>
        <button type="button" data-prediction-page="${page - 1}" ${page <= 1 ? "disabled" : ""}>上一页</button>
        ${pageEntries.map((entry) => entry === "ellipsis" ? '<i aria-hidden="true">…</i>' : `<button type="button" class="${entry === page ? "active" : ""}" data-prediction-page="${entry}" ${entry === page ? 'aria-current="page" disabled' : ""}>${entry}</button>`).join("")}
        <button type="button" data-prediction-page="${page + 1}" ${page >= totalPages ? "disabled" : ""}>下一页</button>
      </div>
      <small>第 ${this.number(page)} / ${this.number(totalPages)} 页</small>
    </nav>`;
  }

  showBanner(message, tone = "success") {
    const target = this.q("#ai-banner");
    target.textContent = message;
    target.className = `ai-banner ${tone}`;
    window.clearTimeout(this.bannerTimer);
    this.bannerTimer = window.setTimeout(() => target.classList.add("hidden"), 6000);
  }

  number(value) { return new Intl.NumberFormat("zh-CN").format(Number(value || 0)); }
  safeUrl(value) { try { const url = new URL(String(value || "")); return ["http:", "https:"].includes(url.protocol) ? url.href : ""; } catch { return ""; } }
  compactNumber(value) { return Number(value).toLocaleString("en-US", { maximumFractionDigits: 6 }); }
  formatMarketCapMillions(value, currency = "USD") { const numeric = Number(value); if (!Number.isFinite(numeric) || numeric <= 0) return "--"; const prefix = currency === "USD" ? "$" : `${currency} `; if (numeric >= 1_000_000) return `${prefix}${(numeric / 1_000_000).toFixed(2)}T`; if (numeric >= 1_000) return `${prefix}${(numeric / 1_000).toFixed(2)}B`; return `${prefix}${numeric.toFixed(2)}M`; }
  formatFinancialAmount(value, currency = "USD") { const numeric = Number(value); if (!Number.isFinite(numeric)) return "--"; const prefix = currency === "USD" ? "$" : `${currency} `; const absolute = Math.abs(numeric); if (absolute >= 1e12) return `${prefix}${(numeric / 1e12).toFixed(2)}T`; if (absolute >= 1e9) return `${prefix}${(numeric / 1e9).toFixed(2)}B`; if (absolute >= 1e6) return `${prefix}${(numeric / 1e6).toFixed(2)}M`; return `${prefix}${numeric.toLocaleString("en-US", { maximumFractionDigits: 0 })}`; }
  formatLeveragedReturnFromBps(value) { if (value == null) return "--"; const numeric = Number(value); return Number.isFinite(numeric) ? this.signedMetric((numeric / 100) * this.state.displayLeverage, 2, "%") : "--"; }
  formatLeveragedReturnFromPercent(value) { if (value == null) return "--"; const numeric = Number(value); return Number.isFinite(numeric) ? this.signedMetric(numeric * this.state.displayLeverage, 2, "%") : "--"; }
  formatBps(value) { const numeric = Number(value); return `${numeric > 0 ? "+" : ""}${numeric.toFixed(2)} bps`; }
  formatUnix(value) { const numeric = Number(value); return numeric ? this.formatDate(new Date(numeric * (numeric > 1e12 ? 1 : 1000))) : "--"; }
  parseDate(value) {
    if (value instanceof Date) return value;
    if (!value) return new Date(Number.NaN);
    const raw = String(value).trim();
    const normalized = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}/.test(raw) && !/(?:Z|[+-]\d{2}:?\d{2})$/i.test(raw) ? `${raw}Z` : raw;
    return new Date(normalized);
  }
  formatDate(value) { if (!value) return "--"; const date = this.parseDate(value); return Number.isNaN(date.getTime()) ? "--" : new Intl.DateTimeFormat("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false }).format(date); }
  formatDuration(start, end) { const milliseconds = this.parseDate(end).getTime() - this.parseDate(start).getTime(); if (!Number.isFinite(milliseconds) || milliseconds <= 0) return "--"; const minutes = Math.round(milliseconds / 60000); return minutes >= 60 && minutes % 60 === 0 ? `${minutes / 60} 小时` : `${minutes} 分钟`; }
  runTypeLabel(value) { return value === "news" ? "新闻分析" : "机会扫描"; }
  statusLabel(value) { return ({ pending: "等待执行", running: "运行中", completed: "已完成", partial: "部分完成", failed: "失败", skipped: "已跳过" })[value] || value || "未知"; }
  sentimentLabel(value) { return ({ bull: "偏多", bear: "偏空", neutral: "中性" })[value] || "已分析"; }
  sentimentClass(value) { return ({ bull: "bull", bear: "bear", neutral: "neutral" })[value] || "pending"; }
  impactLabel(value) { return ({ high: "高影响", medium: "中影响", low: "低影响" })[value] || "影响待定"; }
  horizonLabel(value) { return ({ intraday: "日内", short_term: "短期", medium_term: "中期", long_term: "长期" })[value] || "周期待定"; }
  categoryLabel(value) { return ({ macro: "宏观", company: "公司", earnings: "财报", policy: "政策", geopolitics: "地缘", commodity: "商品", crypto: "加密", other: "其他" })[value] || "分类待定"; }
  analyticsResultLabel(value) { return ({ win: "命中", loss: "未命中", flat: "持平", unavailable: "行情不足" })[value] || "行情不足"; }
  exitReasonLabel(value, detail = "") {
    const detailLabels = { hard_target: "命中最终止盈价", profit_lock: "递进锁盈退出", trailing_profit: "移动止盈退出", failed_follow_through: "跟随失败早退" };
    if (detailLabels[detail]) return detailLabels[detail];
    return ({ take_profit: "触发止盈", stop_loss: "触发止损", score_breakdown: "综合评分转弱", score_reversal: "方向反转", max_holding_time: "最大持有期退出", legacy_horizon_close: "旧版到期结算" })[value] || "退出原因待确认";
  }
}

if (!window.customElements.get("ai-monitor-dashboard")) {
  window.customElements.define("ai-monitor-dashboard", AiMonitorDashboard);
}
