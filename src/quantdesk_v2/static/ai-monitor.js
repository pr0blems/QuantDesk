class AiMonitorDashboard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this.state = {
      view: "opportunities",
      overview: null,
      config: null,
      indicators: [],
      indicatorTemplates: [],
      indicatorConflictPairs: [],
      symbols: [],
      news: [],
      runs: [],
      opportunities: [],
      opportunityAnalytics: null,
      draftSymbols: new Set(),
      symbolSearch: "",
      opportunityTab: "current",
      opportunityDirectionCounts: { long: 0, short: 0 },
      opportunityRequestId: 0,
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
    this.conclusionPanels = { fundamentals: "", analysis: "", news: "" };
    this.conclusionNewsRequestId = 0;
    this.conclusionFundamentalRequestId = 0;
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
      <link rel="stylesheet" href="/assets/ai-monitor.css?v=20260810-18">
      <div class="ai-monitor">
        <header class="ai-head">
          <div>
            <span class="eyebrow">DISCOVERED OPPORTUNITIES</span>
            <h1><i aria-hidden="true">✦</i> 发现机会</h1>
            <p>每 15 分钟分析最新 10 条新闻 × 美股技术指标确认 · 机会只生成虚拟预测</p>
          </div>
          <div class="ai-head-actions">
            <span id="ai-clock" class="ai-clock"></span>
            <span id="scheduler-state" class="status-badge idle">读取中</span>
            <button id="open-news-config" type="button">新闻分析配置</button>
            <button id="run-news" type="button">立即分析新闻</button>
            <button id="run-opportunity" class="primary-action" type="button">立即发现机会</button>
            <button id="open-config" type="button">指标配置</button>
            <button id="ai-refresh" type="button">刷新</button>
          </div>
        </header>
        <div id="ai-banner" class="ai-banner hidden" role="status"></div>
        <section class="ai-stats" aria-label="发现机会摘要">
          <article><span>当前新闻</span><strong id="stat-news">--</strong><small id="stat-news-note">正在读取</small></article>
          <article><span>24h 已分析</span><strong id="stat-analyzed">--</strong><small id="stat-pending">等待数据</small></article>
          <article><span>发现机会</span><strong id="stat-opportunities">--</strong><small id="stat-opportunity-note">新闻 + 指标</small></article>
          <article><span>任务状态</span><strong id="stat-runs">--</strong><small id="stat-run-note">等待调度</small></article>
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
                <div id="model-warning" class="model-warning hidden">尚未配置默认 AI 模型。新闻分析前请先到 <a href="#/settings">系统设置</a> 完成配置。</div>
                <section class="config-grid">
                  <label><span>新闻分析间隔</span><div><input id="config-news-interval" type="number" min="5" max="1440" step="1" required><em>分钟</em></div><small>每轮读取最新 10 条尚未完成 AI 研判的新闻</small></label>
                  <label><span>机会发现间隔</span><div><input id="config-opportunity-interval" type="number" min="5" max="1440" step="1" required><em>分钟</em></div><small>组合新闻和技术指标重新扫描</small></label>
                  <label><span>新闻回看范围</span><div><input id="config-lookback" type="number" min="1" max="168" step="1" required><em>小时</em></div><small>仅采用该时间范围内的已分析新闻</small></label>
                  <label><span>技术指标周期</span><select id="config-timeframe"><option value="15m">15 分钟</option><option value="1h">1 小时</option><option value="4h">4 小时</option></select><small>采用对应周期最新一根已收盘 K 线</small></label>
                  <label><span>最低新闻置信度</span><div><input id="config-confidence" type="number" min="0" max="100" step="1" required><em>%</em></div><small>AI 置信度 × 股票相关度</small></label>
                  <label><span>最少关联新闻</span><div><input id="config-mentions" type="number" min="1" max="20" step="1" required><em>条</em></div><small>同一股票达到数量后才进入指标确认</small></label>
                </section>
                <section class="symbol-block">
                  <header><div><strong>监控品种</strong><small>机会扫描只处理这里配置的美股合约；扫描仍需新闻偏多且技术指标全部满足。</small></div><span id="symbol-count">正在读取</span></header>
                  <div class="symbol-mode">
                    <label><input id="config-all-symbols" type="checkbox"><span>扫描全部可用品种</span></label>
                    <div class="symbol-tools"><input id="symbol-search" type="search" aria-label="搜索监控品种" placeholder="搜索 AAPL / AAPLUSDT"><button id="symbols-visible" type="button">选择筛选结果</button><button id="symbols-clear" type="button">清空选择</button></div>
                  </div>
                  <div id="symbol-picker" class="symbol-picker"><div class="empty-state">正在读取可监控品种…</div></div>
                </section>
                <section id="indicator-config" class="indicator-block">
                  <header><div><strong>技术指标（多选）</strong><small>采用 AND 规则：新闻候选会显示在“发现机会”；所选指标全部满足后才写入虚拟预测。</small></div><span id="indicator-count">已选 0 项</span></header>
                  <div id="indicator-templates" class="indicator-templates"></div>
                  <div id="indicator-conflict-warning" class="indicator-conflict-warning hidden"></div>
                  <div id="indicator-picker" class="indicator-picker"><div class="empty-state">正在读取指标目录…</div></div>
                </section>
                <div class="config-footer"><span id="config-saved-at">尚未保存用户配置</span><button class="save-config" type="submit">保存配置</button></div>
              </form>
            </section>
            <section id="view-opportunities" class="ai-view active">
              <div class="view-head"><div><span class="eyebrow">DISCOVERED OPPORTUNITIES</span><h2>发现机会</h2><p>先展示新闻识别出的美股候选，再标明技术指标确认进度；全部满足后才生成虚拟预测。</p></div></div>
              <nav class="opportunity-tabs" role="tablist" aria-label="机会记录范围">
                <button class="active" type="button" role="tab" aria-selected="true" data-opportunity-tab="current"><span>当前机会</span><small id="current-direction-counts" class="direction-counts"><b class="long">多 --</b><i>/</i><b class="short">空 --</b></small></button>
                <button type="button" role="tab" aria-selected="false" data-opportunity-tab="history"><span>历史机会</span><small>过期记录</small></button>
              </nav>
              <div id="opportunity-list" class="opportunity-list"><div class="empty-state">暂无符合新闻条件的美股候选</div></div>
            </section>
            <section id="view-predictions" class="ai-view">
              <div class="view-head"><div><span class="eyebrow">OPPORTUNITY ANALYTICS</span><h2>预测统计分析</h2><p id="prediction-note">基于历史机会的入场价格与有效期结束价格，统计实际方向表现。</p></div></div>
              <section id="analytics-summary" class="analytics-summary" aria-label="历史机会命中统计"><div class="analytics-loading">正在计算历史机会表现…</div></section>
              <div id="prediction-list" class="prediction-list"><div class="empty-state">正在读取历史机会…</div></div>
            </section>
          </main>
        </div>
      </div>
      <contract-monitor id="opportunity-research" research-only aria-label="股票合约 K 线研究弹窗"></contract-monitor>
      <div id="ai-conclusion-modal" class="ai-conclusion-modal hidden" aria-hidden="true">
        <button class="ai-conclusion-backdrop" type="button" data-conclusion-close aria-label="关闭 AI 分析结论"></button>
        <section class="ai-conclusion-dialog" role="dialog" aria-modal="true" aria-labelledby="ai-conclusion-title">
          <nav class="ai-conclusion-nav" role="tablist" aria-label="机会分析详情">
            <button type="button" role="tab" aria-selected="false" data-conclusion-view="fundamentals"><span>01</span><strong>基本面信息</strong><small id="ai-conclusion-fundamental-state">正在读取</small></button>
            <button type="button" role="tab" aria-selected="false" data-conclusion-view="news"><span>02</span><strong>相关新闻列表</strong><small id="ai-conclusion-news-count">正在读取</small></button>
            <button class="active" type="button" role="tab" aria-selected="true" data-conclusion-view="analysis"><span>03</span><strong>AI分析结论</strong><small>综合研判</small></button>
          </nav>
          <div class="ai-conclusion-main">
            <header class="ai-conclusion-head">
              <div><span class="eyebrow">AI ANALYSIS CONCLUSION</span><h2 id="ai-conclusion-title">AI 分析结论</h2><p id="ai-conclusion-subtitle">读取生成该机会时保存的新闻研判与技术指标证据。</p></div>
              <button id="ai-conclusion-close" class="ai-conclusion-close" type="button" data-conclusion-close aria-label="关闭">×</button>
            </header>
            <div id="ai-conclusion-body" class="ai-conclusion-body"></div>
            <footer class="ai-conclusion-foot"><span>结论基于信号生成时的数据快照</span><strong>仅作虚拟预测研究，不会触发实盘交易</strong></footer>
          </div>
        </section>
      </div>`;
  }

  bindEvents() {
    if (this.eventsBound) return;
    this.eventsBound = true;
    this.qa("[data-ai-view]").forEach((button) => button.addEventListener("click", () => this.showView(button.dataset.aiView)));
    this.q("#ai-refresh").addEventListener("click", () => this.loadAll(true));
    this.q("#run-news").addEventListener("click", () => this.createRun("news"));
    this.q("#run-opportunity").addEventListener("click", () => this.createRun("opportunity"));
    this.q("#open-news-config").addEventListener("click", () => this.openConfig("news"));
    this.q("#open-config").addEventListener("click", () => this.openConfig("indicators"));
    this.q("#ai-config-form").addEventListener("submit", (event) => this.saveConfig(event));
    this.q("#config-enabled").addEventListener("change", (event) => this.renderEnabledLabel(event.target.checked));
    this.q("#indicator-picker").addEventListener("change", () => this.updateIndicatorCount());
    this.q("#indicator-templates").addEventListener("click", (event) => this.applyIndicatorTemplate(event));
    this.q("#config-all-symbols").addEventListener("change", () => this.renderSymbolPicker());
    this.q("#symbol-search").addEventListener("input", (event) => { this.state.symbolSearch = event.target.value.trim().toUpperCase(); this.renderSymbolPicker(); });
    this.q("#symbol-picker").addEventListener("change", (event) => this.updateDraftSymbol(event));
    this.q("#symbols-visible").addEventListener("click", () => this.selectVisibleSymbols());
    this.q("#symbols-clear").addEventListener("click", () => { this.q("#config-all-symbols").checked = false; this.state.draftSymbols.clear(); this.renderSymbolPicker(); });
    this.q("#news-search").addEventListener("input", (event) => { this.state.newsSearch = event.target.value.trim().toLowerCase(); this.renderNews(); });
    this.q("#news-mode").addEventListener("change", (event) => { this.state.newsMode = event.target.value; this.renderNews(); });
    this.qa("[data-opportunity-tab]").forEach((button) => button.addEventListener("click", () => this.setOpportunityTab(button.dataset.opportunityTab)));
    this.q("#opportunity-list").addEventListener("click", (event) => {
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
    this.qa("[data-conclusion-close]").forEach((button) => button.addEventListener("click", () => this.closeAiConclusion()));
    this.qa("[data-conclusion-view]").forEach((button) => button.addEventListener("click", () => this.showAiConclusionView(button.dataset.conclusionView)));
    this.shadowRoot.addEventListener("keydown", (event) => {
      if (event.key === "Escape" && !this.q("#ai-conclusion-modal").classList.contains("hidden")) this.closeAiConclusion();
    });
    this.q("#news-stream").addEventListener("mouseenter", () => { this.scrollPaused = true; });
    this.q("#news-stream").addEventListener("mouseleave", () => { this.scrollPaused = false; });
    this.q("#news-stream").addEventListener("click", (event) => {
      const button = event.target.closest("[data-analyze-news]");
      if (button) this.analyzeNewsItem(button.dataset.analyzeNews);
    });
  }

  start() {
    if (this.state.running) return;
    this.state.running = true;
    this.tickClock();
    this.loadAll();
    this.timers.push(window.setInterval(() => this.tickClock(), 1000));
    this.timers.push(window.setInterval(() => this.loadLiveState(), 20000));
    this.timers.push(window.setInterval(() => this.autoScrollNews(), 80));
  }

  pause() {
    this.state.running = false;
    this.timers.forEach((timer) => window.clearInterval(timer));
    this.timers = [];
  }

  q(selector) { return this.shadowRoot.querySelector(selector); }
  qa(selector) { return [...this.shadowRoot.querySelectorAll(selector)]; }
  api(path = "", options = {}) { return window.quantdeskApi(`/api/v2/ai-monitor${path}`, options); }

  escape(value) {
    return String(value ?? "").replace(/[&<>'"]/g, (character) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" })[character]);
  }

  tickClock() {
    const target = this.q("#ai-clock");
    if (target) target.textContent = new Intl.DateTimeFormat("zh-CN", { hour12: false, hour: "2-digit", minute: "2-digit", second: "2-digit" }).format(new Date());
  }

  async loadAll(showSuccess = false) {
    this.q("#ai-refresh").disabled = true;
    try {
      const [overview, news, indicators, symbols] = await Promise.all([
        this.api("/overview"),
        this.api("/news?limit=160"),
        this.api("/indicators?timeframe=1h"),
        this.api("/symbols"),
      ]);
      this.state.overview = overview;
      this.state.config = overview.config;
      this.state.news = news.items || [];
      this.state.indicators = indicators.items || [];
      this.state.indicatorTemplates = indicators.templates || [];
      this.state.indicatorConflictPairs = indicators.conflict_pairs || [];
      this.state.symbols = symbols.items || [];
      this.renderOverview();
      this.renderNews();
      this.renderConfig();
      await this.loadView(this.state.view);
      if (showSuccess) this.showBanner("发现机会数据已刷新。", "success");
    } catch (error) {
      this.showBanner(error.message || "发现机会数据读取失败", "error");
    } finally {
      this.q("#ai-refresh").disabled = false;
    }
  }

  async loadLiveState() {
    if (!this.state.running || this.state.busyRun) return;
    try {
      const [overview, news] = await Promise.all([this.api("/overview"), this.api("/news?limit=160")]);
      this.state.overview = overview;
      this.state.config = overview.config;
      this.state.news = news.items || [];
      this.renderOverview();
      this.renderNews();
      if (this.state.view === "runs") await this.loadRuns();
      if (this.state.view === "opportunities") await this.loadOpportunities();
      if (this.state.view === "predictions") await this.loadPredictionAnalytics();
    } catch (error) {
      this.showBanner(error.message || "自动刷新失败", "error");
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
    window.requestAnimationFrame(() => {
      const target = section === "news" ? this.q("#config-news-interval") : this.q("#indicator-config");
      target?.scrollIntoView({ behavior: "smooth", block: "center" });
      if (section === "news") target?.focus();
    });
  }

  async loadView(view) {
    if (view === "runs") return this.loadRuns();
    if (view === "opportunities") return this.loadOpportunities();
    if (view === "predictions") return this.loadPredictionAnalytics();
    return undefined;
  }

  renderOverview() {
    const data = this.state.overview || {};
    const config = data.config || {};
    const scheduler = data.scheduler || {};
    const news = data.news || {};
    const opportunities = data.opportunities || {};
    this.q("#stat-news").textContent = this.number(news.total);
    this.q("#stat-news-note").textContent = news.latest_ts ? `最新 ${this.formatUnix(news.latest_ts)}` : "等待新闻采集";
    this.q("#stat-analyzed").textContent = this.number(news.analyzed_24h);
    this.q("#stat-pending").textContent = `待分析 ${this.number(news.pending)} 条`;
    this.q("#stat-opportunities").textContent = this.number(opportunities.active);
    this.q("#stat-opportunity-note").textContent = `${config.timeframe || "1h"} · ${config.indicator_keys?.length || 0} 项全部满足`;
    this.q("#stat-runs").textContent = scheduler.active_runs ? `${scheduler.active_runs} 运行中` : "空闲";
    this.q("#stat-run-note").textContent = data.latest_run ? `${this.runTypeLabel(data.latest_run.run_type)} · ${this.statusLabel(data.latest_run.status)}` : "暂无执行记录";
    const state = this.q("#scheduler-state");
    state.textContent = config.enabled ? "自动监控中" : "自动监控已暂停";
    state.className = `status-badge ${config.enabled ? "running" : "idle"}`;
    this.q("#model-warning").classList.toggle("hidden", Boolean(data.model_configured));
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

  autoScrollNews() {
    if (!this.state.running || this.state.view !== "news" || this.scrollPaused) return;
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
    this.q("#config-confidence").value = Math.round(Number(config.minimum_news_confidence) * 100);
    this.q("#config-mentions").value = config.minimum_news_mentions;
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

  renderEnabledLabel(enabled) {
    this.q("#config-enabled-label").textContent = enabled ? "已开启" : "已暂停";
  }

  updateIndicatorCount() {
    const selected = this.qa('#indicator-picker input[type="checkbox"]:checked').map((input) => input.value);
    const count = selected.length;
    this.q("#indicator-count").textContent = `已选 ${count} 项 · 全部满足`;
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
    this.showBanner(`已应用“${template.name}”组合；组合内条件仍采用全部满足规则。`, "success");
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
        indicator_keys: indicatorKeys,
        monitor_symbols: monitorSymbols,
        minimum_news_confidence: Number(this.q("#config-confidence").value) / 100,
        minimum_news_mentions: Number(this.q("#config-mentions").value),
      };
      this.state.config = await this.api("/config", { method: "PUT", body: JSON.stringify(payload) });
      this.renderConfig();
      await this.loadOverviewOnly();
      const scope = monitorSymbols.length ? `${monitorSymbols.length} 个监控品种` : "全部可用品种";
      this.showBanner(`配置已保存；${scope}，${indicatorKeys.length} 个指标采用全部满足规则。`, "success");
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

  async loadOpportunities() {
    const tab = this.state.opportunityTab;
    const requestId = ++this.state.opportunityRequestId;
    const target = this.q("#opportunity-list");
    target.innerHTML = `<div class="empty-state opportunity-empty"><strong>正在读取${tab === "history" ? "历史" : "当前"}机会…</strong></div>`;
    try {
      const [data, analytics] = await Promise.all([
        this.api(`/opportunities?limit=${tab === "history" ? "300" : "160"}&include_expired=${tab === "history" ? "true" : "false"}`),
        tab === "history"
          ? this.api("/opportunity-analytics?limit=300").catch(() => null)
          : Promise.resolve(null),
      ]);
      if (requestId !== this.state.opportunityRequestId || tab !== this.state.opportunityTab) return;
      const now = Date.now();
      if (analytics) this.state.opportunityAnalytics = analytics;
      const outcomes = new Map((analytics?.items || []).map((item) => [item.id, item]));
      const visible = (data.items || []).filter((item) => {
        const active = ["candidate", "discovered"].includes(item.status) && this.parseDate(item.expires_at).getTime() > now;
        return tab === "history" ? !active : active;
      }).map((item) => ({ ...item, outcome: outcomes.get(item.id) || null }));
      if (tab === "current") {
        const unique = new Map();
        visible.forEach((item) => {
          const instrument = String(item.contract_symbol || item.symbol || "").trim().toUpperCase();
          if (!unique.has(instrument)) unique.set(instrument, item);
        });
        this.state.opportunities = [...unique.values()];
        this.state.opportunityDirectionCounts = this.state.opportunities.reduce((counts, item) => {
          if (item.direction === "short") counts.short += 1;
          else counts.long += 1;
          return counts;
        }, { long: 0, short: 0 });
        this.renderOpportunityDirectionCounts();
      } else {
        this.state.opportunities = visible;
      }
      this.renderOpportunities();
    } catch (error) {
      if (requestId !== this.state.opportunityRequestId) return;
      this.showBanner(error.message || "发现机会读取失败", "error");
    }
  }

  renderOpportunityDirectionCounts() {
    const target = this.q("#current-direction-counts");
    if (!target) return;
    const counts = this.state.opportunityDirectionCounts;
    target.innerHTML = `<b class="long">多 ${this.number(counts.long)}</b><i>/</i><b class="short">空 ${this.number(counts.short)}</b>`;
    target.setAttribute("aria-label", `做多 ${counts.long} 个，做空 ${counts.short} 个`);
  }

  setOpportunityTab(tab) {
    if (!["current", "history"].includes(tab) || tab === this.state.opportunityTab) return;
    this.state.opportunityTab = tab;
    this.qa("[data-opportunity-tab]").forEach((button) => {
      const active = button.dataset.opportunityTab === tab;
      button.classList.toggle("active", active);
      button.setAttribute("aria-selected", String(active));
    });
    this.loadOpportunities();
  }

  renderOpportunities() {
    const target = this.q("#opportunity-list");
    const historicalTab = this.state.opportunityTab === "history";
    if (!this.state.opportunities.length) {
      target.innerHTML = historicalTab
        ? '<div class="empty-state opportunity-empty"><strong>暂无历史机会</strong><span>信号过期或结束后会自动归入这里。</span></div>'
        : '<div class="empty-state opportunity-empty"><strong>尚未发现当前有效的美股候选</strong><span>系统会按新闻回看范围、置信度和关联股票继续扫描。</span></div>';
      return;
    }
    target.innerHTML = this.state.opportunities.map((item) => {
      const evidence = item.evidence || {};
      const indicatorItems = evidence.indicators || [];
      const matchedCount = Number(evidence.matched_indicator_count ?? indicatorItems.filter((indicator) => indicator.matched).length);
      const requiredCount = Number(evidence.required_indicator_count ?? indicatorItems.length);
      const indicators = indicatorItems.slice(0, 10).map((indicator) => `<span class="evidence-chip ${indicator.matched ? "matched" : "unmatched"}">${indicator.matched ? "✓" : "×"} ${this.escape(indicator.name)}</span>`).join("");
      const indicatorRemainder = indicatorItems.length > 10 ? `<span class="evidence-chip remainder">另有 ${indicatorItems.length - 10} 项</span>` : "";
      const news = (evidence.news || []).slice(0, 3).map((entry) => `<li><time>${this.formatUnix(entry.ts)}</time><span>${this.escape(entry.title)}</span><b>${Math.round(Number(entry.score || 0) * 100)}%</b></li>`).join("");
      const market = evidence.market || {};
      const confirmed = item.status === "discovered";
      const historyState = item.status === "dismissed" ? "已结束" : "已过期";
      const marketAvailable = evidence.market_available !== false;
      const directionLabel = item.direction === "short" ? "做空" : "做多";
      const directionClass = item.direction === "short" ? "short" : "long";
      const signalStart = this.formatDate(item.discovered_at);
      const signalEnd = this.formatDate(item.expires_at);
      const signalDuration = this.formatDuration(item.discovered_at, item.expires_at);
      const buyPrice = market.price == null ? "--" : this.compactNumber(market.price);
      const outcome = historicalTab ? item.outcome : null;
      const outcomeResult = outcome?.result || "unavailable";
      const outcomeLabel = this.analyticsResultLabel(outcomeResult);
      const outcomeReturn = outcome?.directional_return_bps == null ? "到期行情不足" : `方向收益 ${this.formatBps(outcome.directional_return_bps)}`;
      const outcomeMetric = historicalTab
        ? `<span class="history-result ${this.escape(outcomeResult)}"><em>是否命中</em><b>${this.escape(outcomeLabel)}</b><small>${this.escape(outcomeReturn)}</small></span>`
        : "";
      const symbolControl = marketAvailable
        ? `<button class="opportunity-symbol" type="button" data-opportunity-id="${this.escape(item.id)}" data-open-contract="${this.escape(item.contract_symbol)}" data-timeframe="${this.escape(item.timeframe)}" title="打开 ${this.escape(item.symbol)} 的合约 K 线研究与预测模拟">${this.escape(item.symbol)}</button>`
        : `<button class="opportunity-symbol unavailable" type="button" disabled title="该股票暂无对应的合约技术行情">${this.escape(item.symbol)}</button>`;
      const conclusionControl = `<button class="ai-conclusion-trigger" type="button" data-ai-conclusion="${this.escape(item.id)}" title="查看 ${this.escape(item.symbol)} 的 AI 分析结论">AI分析结论</button>`;
      return `<article class="opportunity-item ${this.escape(item.status)} ${historicalTab ? `historical outcome-${this.escape(outcomeResult)}` : ""}">
        <header><div><span class="direction ${confirmed ? "confirmed" : "candidate"}">${confirmed ? "技术已确认" : "新闻候选"}</span>${symbolControl}<small>${marketAvailable ? this.escape(item.contract_symbol) : "暂无技术行情"}</small>${conclusionControl}</div><div class="opportunity-score"><b>${Number(item.combined_score).toFixed(1)}</b><span>组合评分</span></div></header>
        <div class="opportunity-metrics ${historicalTab ? "with-result" : ""}"><span><em>新闻评分</em><b>${Number(item.news_score).toFixed(1)}</b></span><span><em>指标满足</em><b>${matchedCount} / ${requiredCount}</b></span><span><em>确认周期</em><b>${this.escape(item.timeframe)}</b></span><span><em>信号状态</em><b>${historicalTab ? historyState : confirmed ? "技术已确认" : "等待确认"}</b></span>${outcomeMetric}</div>
        <div class="opportunity-signal" aria-label="虚拟买入信号信息">
          <span><em>信号时间</em><b>${signalStart}</b></span>
          <span class="validity"><em>信号有效期间</em><b>${signalDuration}</b><small>${signalStart} — ${signalEnd}</small></span>
          <span><em>买入方向</em><b class="signal-${directionClass}">${directionLabel}</b></span>
          <span><em>买入价格</em><b>${this.escape(buyPrice)}</b><small>扫描时参考价</small></span>
        </div>
        <div class="evidence-chips">${indicators}${indicatorRemainder}</div>
        <ul class="opportunity-news">${news}</ul>
        <footer><span>发现 ${this.formatDate(item.discovered_at)}</span><span>有效至 ${this.formatDate(item.expires_at)}</span><em>${historicalTab ? `历史机会 · ${outcomeLabel}` : confirmed ? "已写入虚拟预测" : item.status === "candidate" ? marketAvailable ? "等待全部指标确认" : "新闻候选 · 暂无技术行情" : "历史机会"}</em></footer>
      </article>`;
    }).join("");
  }

  openAiConclusion(opportunityId, trigger) {
    const item = this.state.opportunities.find((opportunity) => opportunity.id === opportunityId);
    if (!item) {
      this.showBanner("该机会的 AI 分析结论已更新，请刷新后重试。", "error");
      return;
    }
    const evidence = item.evidence || {};
    const newsItems = Array.isArray(evidence.news) ? evidence.news : [];
    const indicatorItems = Array.isArray(evidence.indicators) ? evidence.indicators : [];
    const matchedCount = Number(evidence.matched_indicator_count ?? indicatorItems.filter((indicator) => indicator.matched).length);
    const requiredCount = Number(evidence.required_indicator_count ?? indicatorItems.length);
    const confirmed = item.status === "discovered" || evidence.confirmed === true;
    const directionClass = item.direction === "short" ? "short" : "long";
    const directionLabel = item.direction === "short" ? "做空" : "做多";
    const directionText = item.direction === "short" ? "偏空" : "偏多";
    const uniqueReasons = [...new Set(newsItems.map((entry) => String(entry.reason || "").trim()).filter(Boolean))];
    const primaryReason = uniqueReasons[0] || "关联新闻已形成方向判断，但模型未提供更详细的文字理由。";
    const technicalConclusion = confirmed
      ? `配置的 ${requiredCount} 项技术条件已全部满足，信号已进入虚拟预测。`
      : `当前满足 ${matchedCount}/${requiredCount} 项技术条件，仍属于新闻候选，尚未形成正式虚拟预测。`;
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
          return `<article class="ai-conclusion-indicator ${indicator.matched ? "matched" : "unmatched"}">
            <header><strong>${indicator.matched ? "✓" : "×"} ${this.escape(indicator.name || indicator.key)}</strong><span>${indicator.matched ? "已满足" : "未满足"}</span></header>
            <p>${this.escape(indicator.summary || "暂无指标摘要")}</p>
            ${metrics ? `<div>${metrics}</div>` : ""}
          </article>`;
        }).join("")
      : '<div class="ai-conclusion-empty">该机会没有可展示的技术指标证据。</div>';
    const statusLabel = confirmed ? "技术已确认" : "等待技术确认";
    const marketPrice = evidence.market?.price == null ? "--" : this.compactNumber(evidence.market.price);
    const analysisMarkup = `
      <section class="ai-conclusion-summary ${directionClass}">
        <div><span>综合研判</span><h3>AI 新闻分析${directionText} ${this.escape(item.symbol)}</h3><p>${this.escape(primaryReason)} ${this.escape(technicalConclusion)}</p></div>
        <strong>${directionLabel}<small>${statusLabel}</small></strong>
      </section>
      <section class="ai-conclusion-scores" aria-label="AI 分析评分">
        <article><span>新闻评分</span><b>${Number(item.news_score).toFixed(1)}</b><small>AI 置信度 × 股票关联度</small></article>
        <article><span>技术指标</span><b>${matchedCount} / ${requiredCount}</b><small>全部满足才确认</small></article>
        <article><span>组合评分</span><b>${Number(item.combined_score).toFixed(1)}</b><small>新闻 55% + 指标 45%</small></article>
        <article><span>参考价格</span><b>${this.escape(marketPrice)}</b><small>信号扫描时行情</small></article>
      </section>
      <section class="ai-conclusion-section"><header><div><span>01</span><h3>最终结论</h3></div><small>${statusLabel}</small></header><div class="ai-conclusion-verdict"><p>方向判断：<strong class="${directionClass}">${directionLabel} ${this.escape(item.symbol)}</strong>。${this.escape(technicalConclusion)}</p><p>主要依据：${this.escape(uniqueReasons.join("；") || primaryReason)}</p></div></section>
      <section class="ai-conclusion-section"><header><div><span>02</span><h3>AI 新闻研判</h3></div><small>${newsItems.length} 条关联新闻</small></header><div class="ai-conclusion-news-list">${newsMarkup}</div></section>
      <section class="ai-conclusion-section"><header><div><span>03</span><h3>技术指标验证</h3></div><small>${matchedCount} / ${requiredCount} 满足</small></header><div class="ai-conclusion-indicator-list">${indicatorMarkup}</div></section>`;
    this.conclusionOpportunity = item;
    this.conclusionPanels = {
      fundamentals: '<div class="ai-conclusion-loading">正在读取数据库基本面信息…</div>',
      analysis: analysisMarkup,
      news: this.renderAiRelatedNewsList(newsItems, item, true),
    };
    this.q("#ai-conclusion-news-count").textContent = `关联 ${Math.max(newsItems.length, (item.news_ids || []).length)} 条`;
    this.showAiConclusionView("analysis");
    this.conclusionFocus = trigger || null;
    const modal = this.q("#ai-conclusion-modal");
    modal.classList.remove("hidden");
    modal.setAttribute("aria-hidden", "false");
    this.q("#ai-conclusion-close").focus({ preventScroll: true });
    this.loadAiRelatedNews(item, newsItems);
    this.loadAiFundamentals(item);
  }

  showAiConclusionView(view) {
    if (!["fundamentals", "news", "analysis"].includes(view)) return;
    this.state.conclusionView = view;
    this.qa("[data-conclusion-view]").forEach((button) => {
      const active = button.dataset.conclusionView === view;
      button.classList.toggle("active", active);
      button.setAttribute("aria-selected", String(active));
    });
    const item = this.conclusionOpportunity;
    if (item) {
      const viewTitle = ({ fundamentals: "基本面信息", news: "相关新闻列表", analysis: "AI 分析结论" })[view];
      this.q("#ai-conclusion-title").textContent = `${item.symbol} · ${viewTitle}`;
      this.q("#ai-conclusion-subtitle").textContent = `${item.contract_symbol} · ${this.formatDate(item.discovered_at)} · ${item.timeframe} 周期`;
    }
    const target = this.q("#ai-conclusion-body");
    target.innerHTML = this.conclusionPanels[view] || '<div class="ai-conclusion-loading">正在读取相关内容…</div>';
    target.scrollTop = 0;
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
    modal.classList.add("hidden");
    modal.setAttribute("aria-hidden", "true");
    this.conclusionNewsRequestId += 1;
    this.conclusionFundamentalRequestId += 1;
    this.conclusionOpportunity = null;
    const focusTarget = this.conclusionFocus;
    this.conclusionFocus = null;
    if (focusTarget?.isConnected) focusTarget.focus({ preventScroll: true });
  }

  async loadPredictionAnalytics() {
    try {
      const data = await this.api("/opportunity-analytics?limit=300");
      this.state.opportunityAnalytics = data;
      this.renderPredictionAnalytics();
    } catch (error) {
      this.showBanner(error.message || "预测统计分析读取失败", "error");
    }
  }

  renderPredictionAnalytics() {
    const data = this.state.opportunityAnalytics || {};
    const summary = data.summary || {};
    const items = data.items || [];
    const metricBps = (value) => value == null ? "--" : this.formatBps(value);
    const hitRate = summary.hit_rate == null ? "--" : `${Number(summary.hit_rate).toFixed(1)}%`;
    const coverage = `${Number(summary.coverage_rate || 0).toFixed(1)}% 可结算`;
    this.q("#prediction-note").textContent = data.note || "按历史机会到期行情统计，不执行任何下单。";
    this.q("#analytics-summary").innerHTML = `
      <article><span>历史机会</span><strong>${this.number(summary.historical_count)}</strong><small>当前统计样本</small></article>
      <article><span>有效样本</span><strong>${this.number(summary.settled_count)}</strong><small>${coverage} · 已剔除行情不足 ${this.number(summary.discarded_unavailable_count)}</small></article>
      <article class="positive"><span>命中次数</span><strong>${this.number(summary.win_count)}</strong><small>未命中 ${this.number(summary.loss_count)} · 持平 ${this.number(summary.flat_count)}</small></article>
      <article class="${Number(summary.hit_rate || 0) >= 50 ? "positive" : "negative"}"><span>命中概率</span><strong>${hitRate}</strong><small>命中 ÷ 有方向结果</small></article>
      <article class="${Number(summary.average_directional_return_bps || 0) >= 0 ? "positive" : "negative"}"><span>平均方向收益</span><strong>${metricBps(summary.average_directional_return_bps)}</strong><small>按买入方向计算</small></article>
      <article><span>最佳 / 最差</span><strong class="analytics-range"><b>${metricBps(summary.best_directional_return_bps)}</b><i>${metricBps(summary.worst_directional_return_bps)}</i></strong><small>方向收益区间</small></article>`;
    const target = this.q("#prediction-list");
    if (!items.length) { target.innerHTML = '<div class="empty-state opportunity-empty"><strong>暂无历史机会统计</strong><span>当前机会到期后，会自动进入这里计算命中表现。</span></div>'; return; }
    target.innerHTML = `<div class="table-wrap"><table><thead><tr><th>信号时间</th><th>股票 / 合约</th><th>方向</th><th>技术状态</th><th>新闻 / 指标评分</th><th>买入价格</th><th>到期价格</th><th>方向收益</th><th>结果</th><th>有效期结束</th></tr></thead><tbody>${items.map((item) => `<tr><td>${this.formatDate(item.signal_time)}</td><td><strong>${this.escape(item.symbol)}</strong><small>${this.escape(item.contract_symbol)}</small></td><td><span class="prediction-direction ${item.direction === "short" ? "short" : ""}">${item.direction === "long" ? "做多" : "做空"}</span></td><td><span class="technical-state ${item.technical_confirmed ? "confirmed" : "candidate"}">${item.technical_confirmed ? "技术已确认" : "新闻候选"}</span></td><td>${Number(item.news_score).toFixed(1)} / ${Number(item.indicator_score).toFixed(1)}<small>组合 ${Number(item.combined_score).toFixed(1)}</small></td><td>${item.entry_price == null ? "--" : this.escape(this.compactNumber(item.entry_price))}</td><td>${item.exit_price == null ? "--" : this.escape(this.compactNumber(item.exit_price))}<small>${item.settled_price_at ? this.formatDate(item.settled_price_at) : "到期行情不足"}</small></td><td class="${Number(item.directional_return_bps || 0) >= 0 ? "positive" : "negative"}">${item.directional_return_bps == null ? "--" : this.formatBps(item.directional_return_bps)}</td><td><span class="prediction-result ${this.escape(item.result || "unavailable")}">${this.analyticsResultLabel(item.result)}</span></td><td>${this.formatDate(item.expires_at)}<small>${this.escape(item.timeframe)} 信号</small></td></tr>`).join("")}</tbody></table></div>`;
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
}

if (!window.customElements.get("ai-monitor-dashboard")) {
  window.customElements.define("ai-monitor-dashboard", AiMonitorDashboard);
}
