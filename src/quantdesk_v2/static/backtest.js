class BacktestWorkbench extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this.started = false;
    this.loading = false;
    this.runningBacktest = false;
    this.sessionGeneration = 0;
    this.catalog = { strategies: [], symbols: [], timeframes: [], bounds: {} };
    this.history = [];
    this.activeDetail = null;
    this.strategyId = "";
    this.category = "全部";
    this.resizeObserver = null;
    this.handleStrategiesChanged = () => { this.started = false; };
    this.renderShell();
  }

  connectedCallback() {
    this.bindEvents();
    window.addEventListener("quantdesk:strategies-changed", this.handleStrategiesChanged);
    if ("ResizeObserver" in window && !this.resizeObserver) {
      this.resizeObserver = new ResizeObserver(() => {
        if (this.activeDetail) this.drawCharts(this.unpackDetail(this.activeDetail).result);
      });
      this.resizeObserver.observe(this.q("#chart-panel"));
    }
  }

  disconnectedCallback() {
    this.pause();
    window.removeEventListener("quantdesk:strategies-changed", this.handleStrategiesChanged);
    if (this.resizeObserver) this.resizeObserver.disconnect();
    this.resizeObserver = null;
  }

  renderShell() {
    this.shadowRoot.innerHTML = `
      <link rel="stylesheet" href="/assets/backtest.css?v=20260804-1">
      <main class="backtest-workbench">
        <header class="workbench-head">
          <div class="head-copy">
            <div class="title-line"><span class="title-mark" aria-hidden="true">测</span><h1>数据回测</h1><span class="beta">RESEARCH</span></div>
            <p>用历史行情验证策略逻辑、成本敏感度与风险边界</p>
          </div>
          <div class="integrity-strip" aria-label="回测可信度约束">
            <span><i></i>下一根开盘成交</span><span><i></i>手续费/滑点已计</span><span><i></i>无未来函数</span>
          </div>
        </header>

        <div id="global-banner" class="global-banner hidden" role="status" aria-live="polite"></div>

        <div class="workbench-layout">
          <form id="backtest-form" class="config-rail" novalidate>
            <section class="config-section strategy-section">
              <div class="section-head"><div><span class="section-index">01</span><strong>选择完整策略</strong></div><small id="strategy-count">--</small></div>
              <div id="category-filter" class="category-filter" aria-label="策略分类"></div>
              <div id="strategy-list" class="strategy-list"><div class="rail-loading">正在读取我的策略…</div></div>
              <p id="strategy-description" class="strategy-description">策略参数将随策略中心配置动态加载。</p>
            </section>

            <section class="config-section">
              <div class="section-head"><div><span class="section-index">02</span><strong>市场与区间</strong></div><small id="data-bound">等候行情目录</small></div>
              <div class="field-grid two">
                <label>交易品种<select id="symbol" name="symbol" required><option value="">加载中…</option></select></label>
                <label>策略触发周期<select id="timeframe" name="timeframe" required><option value="">加载中…</option></select><small id="strategy-timeframe-note" class="field-help">选择策略后自动确定</small></label>
              </div>
              <div class="field-grid two date-fields">
                <label>开始日期<input id="start-date" name="start_date" type="date" required></label>
                <label>结束日期<input id="end-date" name="end_date" type="date" required></label>
              </div>
              <div class="range-presets" aria-label="快速回测区间">
                <button type="button" data-months="3">近 3 月</button><button type="button" data-months="6">6 个月</button><button type="button" data-months="12">近 1 年</button><button type="button" data-months="all">可用最大</button>
              </div>
            </section>

            <section class="config-section">
              <div class="section-head"><div><span class="section-index">03</span><strong>资金与仓位</strong></div><small>单标的 · 单仓</small></div>
              <div class="field-grid two">
                <label>初始资金 (USDT)<input id="initial-capital" name="initial_capital" type="number" min="1" step="1" value="10000" required></label>
                <label>单次仓位 (%)<input id="position-size" name="position_size_pct" type="number" min="0.01" max="100" step="0.01" value="10" required></label>
                <label>杠杆倍数<input id="leverage" name="leverage" type="number" min="1" max="20" step="1" value="1" required></label>
                <label>最大持有 (K线)<input id="max-holding" name="max_holding_bars" type="number" min="0" max="50000" step="1" value="120" required></label>
              </div>
            </section>

            <section class="config-section">
              <div class="section-head"><div><span class="section-index">04</span><strong>成本与退出</strong></div><small>结果均为净值</small></div>
              <div class="field-grid two">
                <label>手续费 (bp)<input id="fee" name="fee_bps" type="number" min="0" max="1000" step="0.1" value="4" required></label>
                <label>滑点 (bp)<input id="slippage" name="slippage_bps" type="number" min="0" max="1000" step="0.1" value="2" required></label>
                <label>止损 (%)<input id="stop-loss" name="stop_loss_pct" type="number" min="0" max="99.9" step="0.1" value="5" required></label>
                <label>止盈 (%)<input id="take-profit" name="take_profit_pct" type="number" min="0" max="99.9" step="0.1" value="10" required></label>
              </div>
            </section>

            <section id="parameter-section" class="config-section hidden">
              <div class="section-head"><div><span class="section-index">05</span><strong>策略参数</strong></div><button id="reset-params" class="text-button" type="button">恢复默认</button></div>
              <div id="strategy-params" class="field-grid two"></div>
            </section>

            <div class="execution-note"><span>成交模型</span><strong>信号收盘确认 → 下一根开盘撮合</strong><small>本地所选区间没有 K 线时，将从 Binance 公共 API 按需补齐已收盘历史数据；手续费与双边滑点均计入。</small></div>
            <button id="run-backtest" class="run-button" type="submit" disabled><span aria-hidden="true">▶</span><strong>运行回测</strong></button>
          </form>

          <section class="result-stage">
            <div class="stage-toolbar">
              <div><strong>回测结果</strong><span id="stage-status" class="stage-status idle"><i></i>等待运行</span></div>
              <div class="toolbar-meta"><span id="active-run-meta">选择策略并配置参数</span><button id="refresh-history" type="button">刷新历史</button></div>
            </div>

            <div class="stage-layout">
              <div class="result-primary">
                <div id="empty-result" class="empty-result">
                  <div class="flask" aria-hidden="true"><i></i></div>
                  <h2>选择策略并开始回测</h2>
                  <p>先用当前可用区间快速验证逻辑，再随历史数据积累扩大区间。结果会展示收益、回撤、逐笔交易及行情完整度。</p>
                  <div class="empty-checks"><span>01 参数可复现</span><span>02 成本可配置</span><span>03 数据质量可见</span></div>
                </div>

                <div id="result-content" class="result-content hidden">
                  <div class="result-title">
                    <div><span id="result-kicker">BACKTEST COMPLETE</span><h2 id="result-name">--</h2><p id="result-period">--</p></div>
                    <span id="result-return" class="return-badge">--</span>
                  </div>
                  <section id="metric-grid" class="metric-grid" aria-label="回测核心指标"></section>

                  <section id="chart-panel" class="result-panel chart-panel">
                    <div class="panel-head"><div><strong>权益与回撤</strong><span>净值曲线与水下回撤同步观察</span></div><div id="chart-legend" class="chart-legend"><span class="equity-dot">权益</span><span class="drawdown-dot">回撤</span></div></div>
                    <div class="equity-chart-wrap"><canvas id="equity-chart" aria-label="回测权益曲线"></canvas></div>
                    <div class="drawdown-chart-wrap"><canvas id="drawdown-chart" aria-label="回测回撤曲线"></canvas></div>
                  </section>

                  <section id="quality-panel" class="result-panel quality-panel">
                    <div class="panel-head"><div><strong>数据质量</strong><span>先判断样本是否可信，再看收益</span></div><span id="quality-grade" class="quality-grade">--</span></div>
                    <div id="quality-facts" class="quality-facts"></div>
                    <div id="quality-message" class="quality-message"></div>
                  </section>

                  <section class="result-panel trades-panel">
                    <div class="panel-head"><div><strong>逐笔交易</strong><span id="trade-summary">--</span></div><span id="trade-count" class="count-badge">0 笔</span></div>
                    <div id="trade-table" class="trade-table"></div>
                  </section>
                </div>
              </div>

              <aside class="history-panel">
                <div class="history-head"><div><strong>最近回测</strong><span>当前用户 · 最近 12 次</span></div><span id="history-count">0</span></div>
                <div id="history-list" class="history-list"><div class="history-empty">暂无回测记录</div></div>
                <div class="research-tip"><strong>研究建议</strong><p>收益不是唯一结论。优先检查最大回撤、交易样本数和成本敏感度，再决定是否进入模拟盘。</p></div>
              </aside>
            </div>
          </section>
        </div>
      </main>`;
  }

  q(selector) {
    return this.shadowRoot.querySelector(selector);
  }

  qa(selector) {
    return [...this.shadowRoot.querySelectorAll(selector)];
  }

  node(tag, className = "", text = "") {
    const element = document.createElement(tag);
    if (className) element.className = className;
    if (text !== "") element.textContent = String(text);
    return element;
  }

  async api(path, options = {}) {
    if (typeof window.quantdeskApi !== "function") throw new Error("认证服务尚未就绪");
    return window.quantdeskApi(`/api/v2/backtests${path}`, options);
  }

  bindEvents() {
    if (this.dataset.bound === "1") return;
    this.dataset.bound = "1";
    this.q("#backtest-form").addEventListener("submit", (event) => this.runBacktest(event));
    this.q("#symbol").addEventListener("change", () => this.syncBounds());
    this.q("#timeframe").addEventListener("change", () => this.syncBounds());
    this.q("#refresh-history").addEventListener("click", () => this.loadHistory(true));
    this.q("#reset-params").addEventListener("click", () => this.renderParameters(true));
    this.qa("[data-months]").forEach((button) => button.addEventListener("click", () => this.applyRange(button.dataset.months)));
  }

  start() {
    if (this.started) return;
    this.started = true;
    const generation = this.sessionGeneration;
    void this.bootstrap(generation);
  }

  pause() {
    // 回测任务在服务端完成；切换页面不取消已提交的计算。
  }

  resetSession() {
    this.sessionGeneration += 1;
    this.started = false;
    this.loading = false;
    this.runningBacktest = false;
    this.catalog = { strategies: [], symbols: [], timeframes: [], bounds: {} };
    this.history = [];
    this.activeDetail = null;
    this.strategyId = "";
    this.category = "全部";
    this.q("#backtest-form").reset();
    this.q("#category-filter").replaceChildren();
    this.q("#strategy-count").textContent = "--";
    this.q("#strategy-list").replaceChildren(this.node("div", "rail-loading", "登录后加载我的策略…"));
    this.q("#strategy-description").textContent = "策略参数将随策略中心配置动态加载。";
    this.q("#strategy-params").replaceChildren();
    this.q("#parameter-section").classList.add("hidden");
    this.q("#data-bound").textContent = "等待行情目录";
    for (const id of ["#symbol", "#timeframe"]) {
      const option = this.node("option", "", "登录后加载…");
      option.value = "";
      this.q(id).replaceChildren(option);
    }
    this.q("#timeframe").disabled = false;
    this.q("#strategy-timeframe-note").textContent = "选择策略后自动确定";
    this.q("#strategy-timeframe-note").classList.remove("locked");
    for (const id of ["#start-date", "#end-date"]) {
      const input = this.q(id);
      input.value = "";
      input.removeAttribute("min");
      input.removeAttribute("max");
    }
    this.renderHistory();
    this.q("#empty-result").classList.remove("hidden");
    this.q("#result-content").classList.add("hidden");
    this.q("#refresh-history").disabled = false;
    this.q("#active-run-meta").textContent = "选择策略并配置参数";
    this.showBanner("");
    this.setStageStatus("等待运行", "idle");
    this.setRunButton(false);
  }

  async bootstrap(generation = this.sessionGeneration) {
    this.setStageStatus("正在读取", "loading");
    const [catalogResult, historyResult] = await Promise.allSettled([this.api("/catalog"), this.api("?limit=12")]);
    if (generation !== this.sessionGeneration) return;
    if (catalogResult.status === "fulfilled") {
      this.catalog = this.normalizeCatalog(catalogResult.value);
      this.renderCatalog();
      this.q("#run-backtest").disabled = !this.catalog.strategies.length || !this.catalog.symbols.length;
      this.showBanner("");
    } else {
      this.showBanner(`回测目录加载失败：${catalogResult.reason.message}`, "error");
      this.q("#strategy-list").replaceChildren(this.node("div", "rail-loading error-text", "我的策略暂不可用"));
      this.started = false;
    }
    if (historyResult.status === "fulfilled") {
      this.history = Array.isArray(historyResult.value?.items) ? historyResult.value.items : [];
      this.renderHistory();
    }
    this.setStageStatus(catalogResult.status === "fulfilled" ? "等待运行" : "目录不可用", catalogResult.status === "fulfilled" ? "idle" : "error");
  }

  normalizeCatalog(payload = {}) {
    const normalizeOption = (item, fallbackKey) => {
      if (typeof item === "string") return { value: item, label: item };
      const value = item?.value ?? item?.id ?? item?.symbol ?? item?.timeframe ?? item?.[fallbackKey] ?? "";
      return { ...item, value: String(value), label: String(item?.label ?? item?.name ?? value) };
    };
    return {
      strategies: (Array.isArray(payload.strategies) ? payload.strategies : []).filter((item) => (
        item?.strategy_kind === "full_strategy"
        && item?.lifecycle_status === "published"
        && item?.spec
      )),
      symbols: (Array.isArray(payload.symbols) ? payload.symbols : []).map((item) => normalizeOption(item, "symbol")),
      timeframes: (Array.isArray(payload.timeframes) ? payload.timeframes : []).map((item) => normalizeOption(item, "timeframe")),
      bounds: payload.bounds || {},
    };
  }

  renderCatalog() {
    const categories = ["全部", ...new Set(this.catalog.strategies.map((item) => item.category).filter(Boolean))];
    const categoryBox = this.q("#category-filter");
    categoryBox.replaceChildren(...categories.map((category) => {
      const button = this.node("button", category === this.category ? "active" : "", category);
      button.type = "button";
      button.setAttribute("aria-pressed", String(category === this.category));
      button.addEventListener("click", () => {
        this.category = category;
        const selectedVisible = this.catalog.strategies.some((item) => String(item.id ?? "") === this.strategyId && (category === "全部" || item.category === category));
        if (!selectedVisible) {
          this.strategyId = "";
          this.q("#strategy-params").replaceChildren();
        }
        this.renderCatalog();
      });
      return button;
    }));
    const strategyList = this.catalog.strategies.filter((item) => this.category === "全部" || item.category === this.category);
    this.q("#strategy-count").textContent = `${this.catalog.strategies.length} 个完整策略`;
    const list = this.q("#strategy-list");
    list.replaceChildren(...strategyList.map((strategy) => {
      const id = String(strategy.id ?? "");
      const button = this.node("button", `strategy-card${id === this.strategyId ? " active" : ""}`);
      button.type = "button";
      button.dataset.strategyId = id;
      button.setAttribute("aria-pressed", String(id === this.strategyId));
      const title = this.node("strong", "", strategy.name || id || "未命名策略");
      const triggerTimeframe = strategy.spec?.timeframes?.trigger;
      const strategyType = strategy.spec?.strategy_type === "indicator_composite" ? "指标组合" : "规则策略";
      const category = this.node("span", "strategy-category", triggerTimeframe ? `${strategyType} · ${triggerTimeframe}` : strategyType);
      const summary = this.node("small", "", strategy.description || "使用当前策略参数开始验证");
      button.append(title, category, summary);
      button.addEventListener("click", () => this.selectStrategy(id));
      return button;
    }));
    if (!strategyList.length) {
      const empty = this.node("div", "rail-loading", this.catalog.strategies.length ? "该分类暂无完整策略" : "策略中心还没有可回测的完整策略");
      if (!this.catalog.strategies.length) {
        const manage = this.node("button", "text-button manage-strategies", "去策略中心新增");
        manage.type = "button";
        manage.addEventListener("click", () => document.dispatchEvent(new CustomEvent("quantdesk:navigate", { detail: "strategies" })));
        empty.append(manage);
      }
      list.replaceChildren(empty);
    }

    this.populateSelect(this.q("#symbol"), this.catalog.symbols, "选择品种");
    this.populateSelect(this.q("#timeframe"), this.catalog.timeframes, "选择周期");
    if (!this.strategyId && strategyList.length) this.selectStrategy(String(strategyList[0].id ?? ""));
    this.syncBounds();
  }

  populateSelect(select, options, placeholder) {
    const previous = select.value;
    const placeholderOption = this.node("option", "", placeholder);
    placeholderOption.value = "";
    const nodes = options.map((option) => {
      const node = this.node("option", "", option.label);
      node.value = option.value;
      return node;
    });
    select.replaceChildren(placeholderOption, ...nodes);
    if (options.some((item) => item.value === previous)) select.value = previous;
    else if (options.length) select.value = options[0].value;
  }

  selectStrategy(id) {
    const changed = this.strategyId !== id;
    this.strategyId = id;
    this.qa(".strategy-card").forEach((card) => {
      const active = card.dataset.strategyId === id;
      card.classList.toggle("active", active);
      card.setAttribute("aria-pressed", String(active));
    });
    const strategy = this.selectedStrategy();
    if (!strategy) {
      this.strategyId = "";
      this.q("#strategy-description").textContent = "请从策略中心新增或发布完整策略。";
      this.q("#strategy-params").replaceChildren();
      this.q("#parameter-section").classList.add("hidden");
      this.syncStrategyTimeframe(null);
      return;
    }
    this.q("#strategy-description").textContent = strategy?.description || "策略参数将随策略中心配置动态加载。";
    this.renderParameters(changed);
    if (changed) this.applyStrategyDefaults(strategy);
    this.syncStrategyTimeframe(strategy);
    this.syncBounds();
  }

  selectedStrategy() {
    return this.catalog.strategies.find((item) => String(item.id ?? "") === this.strategyId) || null;
  }

  applyStrategyDefaults(strategy) {
    const defaults = strategy?.risk_defaults;
    if (!defaults || typeof defaults !== "object") return;
    const fields = {
      position_size_pct: "#position-size",
      leverage: "#leverage",
      fee_bps: "#fee",
      slippage_bps: "#slippage",
      stop_loss_pct: "#stop-loss",
      take_profit_pct: "#take-profit",
      max_holding_bars: "#max-holding",
    };
    Object.entries(fields).forEach(([key, selector]) => {
      const value = Number(defaults[key]);
      if (Number.isFinite(value)) this.q(selector).value = String(value);
    });
  }

  syncStrategyTimeframe(strategy) {
    const select = this.q("#timeframe");
    const note = this.q("#strategy-timeframe-note");
    const required = String(strategy?.spec?.timeframes?.trigger || "");
    const available = [...select.options].some((option) => option.value === required);
    if (required && available) {
      select.value = required;
      select.disabled = true;
      note.textContent = `由策略固定为 ${required}`;
      note.classList.add("locked");
    } else {
      select.disabled = false;
      note.textContent = required ? `行情目录暂不支持 ${required}` : "选择策略后自动确定";
      note.classList.remove("locked");
    }
  }

  renderParameters(reset = false) {
    const params = Array.isArray(this.selectedStrategy()?.params) ? this.selectedStrategy().params : [];
    const section = this.q("#parameter-section");
    section.classList.toggle("hidden", !params.length);
    const container = this.q("#strategy-params");
    const existing = reset ? {} : Object.fromEntries(this.qa("[data-param-key]").map((input) => [input.dataset.paramKey, input.type === "checkbox" ? input.checked : input.value]));
    const fields = params.map((param) => {
      const label = this.node("label");
      label.append(this.node("span", "field-label", param.label || param.key));
      const type = String(param.type || "number").toLowerCase();
      let input;
      if (Array.isArray(param.options)) {
        input = this.node("select");
        input.append(...param.options.map((option) => {
          const value = typeof option === "object" ? option.value : option;
          const optionNode = this.node("option", "", typeof option === "object" ? option.label ?? value : value);
          optionNode.value = String(value);
          return optionNode;
        }));
      } else if (type === "boolean" || type === "bool") {
        input = this.node("input");
        input.type = "checkbox";
        label.classList.add("checkbox-field");
      } else {
        input = this.node("input");
        input.type = type === "integer" || type === "float" || type === "number" ? "number" : "text";
        if (param.min != null) input.min = String(param.min);
        if (param.max != null) input.max = String(param.max);
        input.step = String(param.step ?? (type === "integer" ? 1 : "any"));
      }
      input.dataset.paramKey = String(param.key || "");
      input.dataset.paramType = type;
      const value = Object.prototype.hasOwnProperty.call(existing, param.key) ? existing[param.key] : param.default;
      if (input.type === "checkbox") input.checked = Boolean(value);
      else if (value != null) input.value = String(value);
      if (input.type === "checkbox") label.prepend(input);
      else label.append(input);
      if (param.help) label.append(this.node("small", "field-help", param.help));
      return label;
    });
    container.replaceChildren(...fields);
  }

  resolveBounds() {
    const source = this.catalog.bounds || {};
    const symbol = this.q("#symbol").value;
    const timeframe = this.q("#timeframe").value;
    let bound = source;
    if (Array.isArray(source)) {
      bound = source.find((item) => (!item.symbol || item.symbol === symbol) && (!item.timeframe || item.timeframe === timeframe)) || {};
    } else if (source[symbol]?.[timeframe]) bound = source[symbol][timeframe];
    else if (source[symbol]) bound = source[symbol];
    else if (source[`${symbol}:${timeframe}`]) bound = source[`${symbol}:${timeframe}`];
    const min = this.dateOnly(bound?.min_date ?? bound?.start_date ?? bound?.start ?? source.min_date ?? source.start_date ?? source.start);
    const max = this.dateOnly(bound?.max_date ?? bound?.end_date ?? bound?.end ?? source.max_date ?? source.end_date ?? source.end);
    return { min, max };
  }

  syncBounds() {
    const { min, max } = this.resolveBounds();
    const start = this.q("#start-date");
    const end = this.q("#end-date");
    const today = this.dateOnly(new Date());
    const earliestAllowed = this.shiftMonths(today, -12);
    start.min = earliestAllowed;
    start.max = today;
    end.min = earliestAllowed;
    end.max = today;
    if (max) {
      end.value = !end.value || end.value > today || end.value < earliestAllowed ? max : end.value;
      const defaultStart = this.shiftMonths(max, -3);
      const localDefaultStart = this.maxDate(min || earliestAllowed, defaultStart);
      start.value = !start.value || start.value > end.value || start.value < earliestAllowed ? localDefaultStart : start.value;
      this.q("#data-bound").textContent = min ? `本地 ${min} — ${max} · 缺失自动补齐` : `本地截至 ${max} · 缺失自动补齐`;
    } else {
      if (!end.value) end.value = today;
      if (!start.value) start.value = this.shiftMonths(today, -3);
      this.q("#data-bound").textContent = "本地无数据 · 运行时从 Binance 补齐";
    }
  }

  applyRange(months) {
    const { max } = this.resolveBounds();
    const end = this.q("#end-date").value || max || this.dateOnly(new Date());
    const earliestAllowed = this.shiftMonths(end, -12);
    this.q("#end-date").value = end;
    this.q("#start-date").value = months === "all" ? earliestAllowed : this.maxDate(earliestAllowed, this.shiftMonths(end, -Number(months)));
    this.qa("[data-months]").forEach((button) => button.classList.toggle("active", button.dataset.months === String(months)));
  }

  collectParams() {
    const params = {};
    this.qa("[data-param-key]").forEach((input) => {
      const type = input.dataset.paramType;
      const value = type === "boolean" || type === "bool" ? (input.checked ? 1 : 0) : Number(input.value);
      if (Number.isFinite(value)) params[input.dataset.paramKey] = value;
    });
    return params;
  }

  payload() {
    return {
      strategy_id: this.strategyId,
      symbol: this.q("#symbol").value,
      timeframe: this.q("#timeframe").value,
      start_date: this.q("#start-date").value,
      end_date: this.q("#end-date").value,
      initial_capital: Number(this.q("#initial-capital").value),
      position_size_pct: Number(this.q("#position-size").value),
      leverage: Number(this.q("#leverage").value),
      fee_bps: Number(this.q("#fee").value),
      slippage_bps: Number(this.q("#slippage").value),
      stop_loss_pct: Number(this.q("#stop-loss").value),
      take_profit_pct: Number(this.q("#take-profit").value),
      max_holding_bars: Number(this.q("#max-holding").value),
      params: this.collectParams(),
    };
  }

  validate() {
    const form = this.q("#backtest-form");
    if (!this.strategyId) {
      this.showBanner("请先选择一个策略。", "error");
      return false;
    }
    if (!form.checkValidity()) {
      form.reportValidity();
      this.showBanner("请检查回测参数，所有必填项都需要有效值。", "error");
      return false;
    }
    if (this.q("#start-date").value > this.q("#end-date").value) {
      this.showBanner("开始日期不能晚于结束日期。", "error");
      return false;
    }
    const start = new Date(`${this.q("#start-date").value}T00:00:00`);
    const end = new Date(`${this.q("#end-date").value}T00:00:00`);
    if ((end - start) / 86400000 > 366) {
      this.showBanner("单次回测最长为 366 天，请缩短日期区间后重试。", "error");
      return false;
    }
    return true;
  }

  async runBacktest(event) {
    event.preventDefault();
    if (this.runningBacktest || !this.validate()) return;
    const generation = this.sessionGeneration;
    this.runningBacktest = true;
    this.showBanner("");
    this.setRunButton(true);
    this.setStageStatus("检查数据", "loading");
    this.q("#active-run-meta").textContent = `${this.q("#symbol").value} · ${this.q("#timeframe").value} · 缺失时从 Binance 补齐后回放`;
    try {
      let detail = await this.api("", { method: "POST", body: JSON.stringify(this.payload()) });
      if (generation !== this.sessionGeneration) return;
      const id = this.runId(detail);
      if (!detail?.result && id) detail = await this.api(`/${encodeURIComponent(id)}`);
      if (generation !== this.sessionGeneration) return;
      this.activeDetail = detail;
      this.renderResult(detail);
      await this.loadHistory(false, generation);
      if (generation !== this.sessionGeneration) return;
      this.setStageStatus("已完成", "success");
      this.showBanner("回测完成。请先检查数据质量和最大回撤，再判断策略表现。", "success");
    } catch (error) {
      if (generation !== this.sessionGeneration) return;
      this.setStageStatus("运行失败", "error");
      this.showBanner(`回测失败：${error.message}`, "error");
    } finally {
      if (generation === this.sessionGeneration) {
        this.runningBacktest = false;
        this.setRunButton(false);
      }
    }
  }

  async loadHistory(showFeedback = false, generation = this.sessionGeneration) {
    const button = this.q("#refresh-history");
    button.disabled = true;
    try {
      const data = await this.api("?limit=12");
      if (generation !== this.sessionGeneration) return;
      this.history = Array.isArray(data?.items) ? data.items : [];
      this.renderHistory();
      if (showFeedback) this.showBanner("最近回测记录已刷新。", "success");
    } catch (error) {
      if (generation !== this.sessionGeneration) return;
      if (showFeedback) this.showBanner(`历史记录刷新失败：${error.message}`, "error");
    } finally {
      if (generation === this.sessionGeneration) button.disabled = false;
    }
  }

  renderHistory() {
    const list = this.q("#history-list");
    this.q("#history-count").textContent = String(this.history.length);
    if (!this.history.length) {
      list.replaceChildren(this.node("div", "history-empty", "暂无回测记录，首次运行后会保存在这里。"));
      return;
    }
    list.replaceChildren(...this.history.map((item) => {
      const run = item.run || item;
      const metrics = this.unpackDetail(item).result.metrics;
      const id = this.runId(item);
      const button = this.node("button", "history-item");
      button.type = "button";
      if (id) button.dataset.runId = id;
      const top = this.node("div", "history-item-top");
      top.append(this.node("strong", "", run.strategy_name || run.strategy_id || "策略回测"), this.node("span", this.toneClass(this.metric(metrics, ["total_return_pct", "return_pct", "total_return"])), this.percent(this.metric(metrics, ["total_return_pct", "return_pct", "total_return"]))));
      const middle = this.node("div", "history-item-middle", `${run.symbol || "--"} · ${run.timeframe || "--"}`);
      const bottom = this.node("div", "history-item-bottom");
      bottom.append(this.node("span", "", this.shortDate(run.completed_at || run.created_at || run.started_at)), this.node("span", "", `回撤 ${this.percent(this.metric(metrics, ["max_drawdown_pct", "max_drawdown"]), false)}`));
      button.append(top, middle, bottom);
      if (id) button.addEventListener("click", () => this.loadRun(id, button));
      else button.disabled = true;
      return button;
    }));
  }

  async loadRun(id, button) {
    const generation = this.sessionGeneration;
    this.qa(".history-item").forEach((item) => item.classList.toggle("loading", item === button));
    this.setStageStatus("读取记录", "loading");
    try {
      const detail = await this.api(`/${encodeURIComponent(id)}`);
      if (generation !== this.sessionGeneration) return;
      this.activeDetail = detail;
      this.renderResult(detail);
      this.setStageStatus("历史结果", "success");
      this.showBanner("");
    } catch (error) {
      if (generation !== this.sessionGeneration) return;
      this.setStageStatus("读取失败", "error");
      this.showBanner(`回测详情加载失败：${error.message}`, "error");
    } finally {
      if (generation === this.sessionGeneration) {
        this.qa(".history-item").forEach((item) => item.classList.remove("loading"));
      }
    }
  }

  renderResult(detail = {}) {
    const unpacked = this.unpackDetail(detail);
    const run = unpacked.run;
    const result = unpacked.result;
    const account = result.account || {};
    const metrics = result.metrics || {};
    this.q("#empty-result").classList.add("hidden");
    this.q("#result-content").classList.remove("hidden");
    const strategy = run.strategy_name || this.catalog.strategies.find((item) => String(item.id) === String(run.strategy_id))?.name || run.strategy_id || "策略回测";
    this.q("#result-name").textContent = `${strategy} · ${run.symbol || "--"}`;
    this.q("#result-period").textContent = `${this.dateOnly(run.start_date || run.start_at) || "--"} 至 ${this.dateOnly(run.end_date || run.end_at) || "--"} · ${run.timeframe || "--"} · 初始资金 ${this.money(account.initial_capital ?? run.initial_capital)}`;
    const totalReturn = this.metric(metrics, ["total_return_pct", "return_pct", "total_return"]);
    const returnNode = this.q("#result-return");
    returnNode.textContent = this.percent(totalReturn);
    returnNode.className = `return-badge ${this.toneClass(totalReturn)}`;
    this.q("#active-run-meta").textContent = `完成于 ${this.shortDate(run.completed_at || run.updated_at || run.created_at, true)}`;
    this.renderMetrics(metrics, account);
    this.renderQuality(result.data_quality || {});
    this.renderTrades(Array.isArray(result.trades) ? result.trades : [], result.data_quality || {});
    window.requestAnimationFrame(() => this.drawCharts(result));
  }

  renderMetrics(metrics, account) {
    const definitions = [
      ["累计收益", this.percent(this.metric(metrics, ["total_return_pct", "return_pct", "total_return"])), "扣除交易成本", this.metric(metrics, ["total_return_pct", "return_pct", "total_return"])],
      ["年化收益", this.percent(this.metric(metrics, ["annualized_return_pct", "annual_return_pct", "cagr"])), `期末 ${this.money(account.final_equity ?? account.ending_capital ?? account.equity)}`, this.metric(metrics, ["annualized_return_pct", "annual_return_pct", "cagr"])],
      ["最大回撤", this.percent(this.metric(metrics, ["max_drawdown_pct", "max_drawdown"]), false), "峰值至谷底", -Math.abs(Number(this.metric(metrics, ["max_drawdown_pct", "max_drawdown"])) || 0)],
      ["夏普比率", this.number(this.metric(metrics, ["sharpe_ratio", "sharpe"]), 2), "风险调整收益", this.metric(metrics, ["sharpe_ratio", "sharpe"])],
      ["胜率", this.percent(this.metric(metrics, ["win_rate_pct", "win_rate"]), false), `盈亏比 ${this.number(this.metric(metrics, ["profit_factor", "payoff_ratio"]), 2)}`, this.metric(metrics, ["win_rate_pct", "win_rate"])],
      ["交易次数", this.integer(this.metric(metrics, ["trade_count", "total_trades", "trades"])), `手续费 ${this.money(account.total_fees ?? metrics.total_fees)}`, 0],
    ];
    const cards = definitions.map(([label, value, note, tone]) => {
      const card = this.node("article", "metric-card");
      card.append(this.node("span", "", label), this.node("strong", this.toneClass(tone), value), this.node("small", "", note));
      return card;
    });
    this.q("#metric-grid").replaceChildren(...cards);
  }

  renderQuality(quality) {
    const object = typeof quality === "object" && quality ? quality : { message: String(quality || "") };
    let coverage = Number(object.coverage_pct ?? object.coverage ?? object.completeness_pct);
    if (Number.isFinite(coverage) && coverage <= 1) coverage *= 100;
    const missing = Number(object.missing_bars ?? object.missing ?? object.gaps ?? 0);
    const actual = object.actual_bars ?? object.bars_used ?? object.bars_loaded ?? object.bar_count ?? object.rows ?? "--";
    const expected = object.expected_bars ?? object.expected ?? (Number.isFinite(Number(actual)) ? Number(actual) + missing : "--");
    const grade = object.grade || (Number.isFinite(coverage) ? (coverage >= 99 ? "优秀" : coverage >= 95 ? "可用" : "需注意") : (missing ? "需注意" : "已检查"));
    const gradeNode = this.q("#quality-grade");
    gradeNode.textContent = grade;
    gradeNode.className = `quality-grade ${grade === "需注意" ? "warning" : "good"}`;
    const facts = [
      ["行情覆盖率", Number.isFinite(coverage) ? `${coverage.toFixed(2)}%` : "已完成检查"],
      ["实际 K 线", this.integer(actual)],
      ["预期 K 线", this.integer(expected)],
      ["缺口数量", this.integer(missing)],
    ].map(([label, value]) => {
      const fact = this.node("div", "quality-fact");
      fact.append(this.node("span", "", label), this.node("strong", "", value));
      return fact;
    });
    this.q("#quality-facts").replaceChildren(...facts);
    const warnings = Array.isArray(object.warnings) ? object.warnings.filter(Boolean) : [];
    const assumptions = Array.isArray(object.assumptions) ? object.assumptions.filter(Boolean) : [];
    const messages = [];
    if (object.message || object.note) messages.push(object.message || object.note);
    else if (warnings.length) messages.push(warnings.join("；"));
    else if (missing) messages.push("行情存在缺口，结果可能受样本连续性影响。");
    else messages.push("行情覆盖满足本次研究要求。");
    if (object.trades_truncated) {
      messages.push(`成交明细返回 ${this.integer(object.trades_returned)} / ${this.integer(object.trades_total)} 笔，汇总指标按全部成交计算。`);
    }
    const fetches = Array.isArray(object.on_demand_fetches) ? object.on_demand_fetches : [];
    if (fetches.length) {
      const summary = fetches.map((item) => `${item.timeframe || "--"} ${this.integer(item.bars_fetched)} 根`).join("、");
      messages.push(`数据来源：本次从 Binance 公共 API 按需补齐 ${summary} 已收盘 K 线，并已写入本地行情库。`);
    }
    if (assumptions.length) messages.push(`模型假设：${assumptions.join("；")}`);
    this.q("#quality-message").textContent = messages.join("\n");
  }

  renderTrades(trades, quality = {}) {
    const total = Number(quality?.trades_total);
    const totalTrades = Number.isFinite(total) ? total : trades.length;
    this.q("#trade-count").textContent = `${this.integer(totalTrades)} 笔`;
    const wins = trades.filter((trade) => Number(trade.pnl ?? trade.net_pnl ?? 0) > 0).length;
    const truncated = Boolean(quality?.trades_truncated) || totalTrades > trades.length;
    this.q("#trade-summary").textContent = trades.length
      ? `${wins} 胜 / ${trades.length - wins} 负${truncated ? ` · 返回 ${trades.length} / ${totalTrades} 笔` : ""} · 表格显示最近 100 笔`
      : "暂无成交样本";
    const container = this.q("#trade-table");
    if (!trades.length) {
      const empty = this.node("div", "table-empty");
      empty.append(this.node("strong", "", "本次没有触发交易"), this.node("span", "", "可扩大回测区间，或调整策略入场参数后再次验证。"));
      container.replaceChildren(empty);
      return;
    }
    const table = this.node("table");
    const thead = this.node("thead");
    const headRow = this.node("tr");
    ["开仓时间", "方向", "开仓 → 平仓", "仓位", "净盈亏", "收益率", "持有", "退出原因"].forEach((title) => headRow.append(this.node("th", "", title)));
    thead.append(headRow);
    const tbody = this.node("tbody");
    trades.slice(-100).reverse().forEach((trade) => {
      const row = this.node("tr");
      const sideValue = String(trade.side ?? trade.direction ?? "").toLowerCase();
      const isLong = ["long", "buy", "1", "多"].includes(sideValue) || Number(trade.side) > 0;
      const pnl = Number(trade.net_pnl ?? trade.pnl ?? trade.profit ?? 0);
      const values = [
        [this.shortDate(trade.entry_time ?? trade.entry_at ?? trade.opened_at ?? trade.entry_ts, true), "muted"],
        [isLong ? "做多" : "做空", isLong ? "positive" : "negative"],
        [`${this.price(trade.entry_price ?? trade.open_price)} → ${this.price(trade.exit_price ?? trade.close_price)}`, "prices"],
        [this.quantity(trade.quantity ?? trade.qty ?? trade.position_size), ""],
        [this.signedMoney(pnl), this.toneClass(pnl)],
        [this.percent(trade.return_pct ?? trade.pnl_pct, true), this.toneClass(trade.return_pct ?? trade.pnl_pct)],
        [`${this.integer(trade.holding_bars ?? trade.bars_held ?? trade.duration_bars)} 根`, "muted"],
        [this.exitReason(trade.exit_reason ?? trade.reason), "muted"],
      ];
      values.forEach(([value, className]) => row.append(this.node("td", className, value)));
      tbody.append(row);
    });
    table.append(thead, tbody);
    container.replaceChildren(table);
  }

  drawCharts(result) {
    const curve = this.normalizeCurve(result.equity_curve || result.curve || []);
    this.drawLineChart(this.q("#equity-chart"), curve, "equity");
    this.drawLineChart(this.q("#drawdown-chart"), curve, "drawdown");
  }

  normalizeCurve(raw) {
    let peak = -Infinity;
    return (Array.isArray(raw) ? raw : []).map((point, index) => {
      const time = Array.isArray(point) ? point[0] : point?.timestamp ?? point?.ts ?? point?.at ?? point?.date ?? index;
      const equity = Number(Array.isArray(point) ? point[1] : point?.equity ?? point?.value ?? point?.balance);
      if (!Number.isFinite(equity)) return null;
      peak = Math.max(peak, equity);
      const supplied = Number(Array.isArray(point) ? point[2] : point?.drawdown_pct ?? point?.drawdown);
      const drawdown = Number.isFinite(supplied) ? (supplied > 0 ? -supplied : supplied) : (peak ? (equity / peak - 1) * 100 : 0);
      return { time, equity, drawdown };
    }).filter(Boolean);
  }

  drawLineChart(canvas, points, type) {
    const width = Math.floor(canvas.clientWidth);
    const height = Math.floor(canvas.clientHeight);
    if (!width || !height) return;
    const ratio = Math.min(window.devicePixelRatio || 1, 2);
    canvas.width = Math.floor(width * ratio);
    canvas.height = Math.floor(height * ratio);
    const context = canvas.getContext("2d");
    context.setTransform(ratio, 0, 0, ratio, 0, 0);
    context.clearRect(0, 0, width, height);
    const padding = { left: 14, right: 62, top: 15, bottom: 25 };
    const plotWidth = width - padding.left - padding.right;
    const plotHeight = height - padding.top - padding.bottom;
    context.font = "10px Inter, sans-serif";
    if (points.length < 2) {
      context.fillStyle = "#64778d";
      context.fillText("权益样本不足，暂无可绘制曲线", 18, 30);
      return;
    }
    const values = points.map((point) => type === "equity" ? point.equity : point.drawdown);
    let min = Math.min(...values);
    let max = Math.max(...values);
    const rangePadding = (max - min || Math.abs(max) * 0.02 || 1) * 0.08;
    min -= rangePadding;
    max += rangePadding;
    const range = max - min || 1;
    const x = (index) => padding.left + index / (points.length - 1) * plotWidth;
    const y = (value) => padding.top + (max - value) / range * plotHeight;
    context.strokeStyle = "rgba(126, 154, 181, .13)";
    context.fillStyle = "#657b91";
    context.lineWidth = 1;
    for (let line = 0; line <= 3; line += 1) {
      const value = max - range * line / 3;
      const yValue = y(value);
      context.beginPath();
      context.moveTo(padding.left, yValue);
      context.lineTo(width - padding.right, yValue);
      context.stroke();
      context.fillText(type === "equity" ? this.compactNumber(value) : `${value.toFixed(1)}%`, width - padding.right + 7, yValue + 3);
    }
    const color = type === "equity" ? "#31d4a0" : "#f06478";
    const gradient = context.createLinearGradient(0, padding.top, 0, height - padding.bottom);
    gradient.addColorStop(0, type === "equity" ? "rgba(49, 212, 160, .24)" : "rgba(240, 100, 120, .24)");
    gradient.addColorStop(1, "rgba(10, 20, 32, 0)");
    context.beginPath();
    points.forEach((point, index) => {
      const position = [x(index), y(type === "equity" ? point.equity : point.drawdown)];
      if (index) context.lineTo(...position);
      else context.moveTo(...position);
    });
    context.lineTo(x(points.length - 1), height - padding.bottom);
    context.lineTo(x(0), height - padding.bottom);
    context.closePath();
    context.fillStyle = gradient;
    context.fill();
    context.beginPath();
    points.forEach((point, index) => {
      const position = [x(index), y(type === "equity" ? point.equity : point.drawdown)];
      if (index) context.lineTo(...position);
      else context.moveTo(...position);
    });
    context.strokeStyle = color;
    context.lineWidth = 2;
    context.lineJoin = "round";
    context.stroke();
    context.fillStyle = "#657b91";
    context.fillText(this.chartDate(points[0].time), padding.left, height - 6);
    const endLabel = this.chartDate(points[points.length - 1].time);
    const endWidth = context.measureText(endLabel).width;
    context.fillText(endLabel, width - padding.right - endWidth, height - 6);
  }

  setRunButton(running) {
    const button = this.q("#run-backtest");
    button.disabled = running || !this.catalog.strategies.length;
    button.classList.toggle("loading", running);
    button.querySelector("span").textContent = running ? "◌" : "▶";
    button.querySelector("strong").textContent = running ? "正在回放行情…" : "运行回测";
  }

  setStageStatus(text, tone) {
    const status = this.q("#stage-status");
    status.className = `stage-status ${tone}`;
    status.lastChild.textContent = text;
  }

  showBanner(message, tone = "") {
    const banner = this.q("#global-banner");
    banner.textContent = message;
    banner.className = `global-banner${message ? "" : " hidden"}${tone ? ` ${tone}` : ""}`;
  }

  runId(value) {
    const id = value?.run?.id ?? value?.run?.run_id ?? value?.id ?? value?.run_id;
    return id == null ? "" : String(id);
  }

  unpackDetail(detail = {}) {
    const run = detail.run || detail;
    if (detail.result) return { run, result: detail.result || {} };
    const extendedMetrics = run.metrics_json && typeof run.metrics_json === "object" ? run.metrics_json : {};
    const metrics = {
      ...extendedMetrics,
      total_return_pct: run.total_return_pct ?? extendedMetrics.total_return_pct,
      max_drawdown_pct: run.max_drawdown_pct ?? extendedMetrics.max_drawdown_pct,
      sharpe_ratio: run.sharpe_ratio ?? extendedMetrics.sharpe_ratio,
      win_rate_pct: run.win_rate_pct ?? extendedMetrics.win_rate_pct,
      profit_factor: run.profit_factor ?? extendedMetrics.profit_factor,
      trade_count: run.trade_count ?? extendedMetrics.trade_count,
    };
    return {
      run,
      result: {
        account: {
          initial_capital: run.initial_capital,
          final_equity: run.final_equity,
          net_profit: run.net_profit,
          total_fees: extendedMetrics.total_fees,
        },
        metrics,
        equity_curve: run.equity_curve_json || [],
        trades: Array.isArray(run.trades) ? run.trades : [],
        data_quality: run.data_quality_json || {},
      },
    };
  }

  metric(metrics, keys) {
    for (const key of keys) if (metrics?.[key] != null) return metrics[key];
    return null;
  }

  toneClass(value) {
    const numeric = Number(value);
    if (!Number.isFinite(numeric) || numeric === 0) return "neutral";
    return numeric > 0 ? "positive" : "negative";
  }

  number(value, digits = 2) {
    const numeric = Number(value);
    if (!Number.isFinite(numeric)) return "--";
    return numeric.toLocaleString("zh-CN", { minimumFractionDigits: 0, maximumFractionDigits: digits });
  }

  integer(value) {
    const numeric = Number(value);
    return Number.isFinite(numeric) ? Math.round(numeric).toLocaleString("zh-CN") : "--";
  }

  money(value) {
    const numeric = Number(value);
    return Number.isFinite(numeric) ? `${numeric.toLocaleString("zh-CN", { minimumFractionDigits: 0, maximumFractionDigits: 2 })} U` : "--";
  }

  signedMoney(value) {
    const numeric = Number(value);
    return Number.isFinite(numeric) ? `${numeric > 0 ? "+" : ""}${this.number(numeric)} U` : "--";
  }

  percent(value, signed = true) {
    const numeric = Number(value);
    if (!Number.isFinite(numeric)) return "--";
    return `${signed && numeric > 0 ? "+" : ""}${this.number(numeric, 2)}%`;
  }

  price(value) {
    const numeric = Number(value);
    if (!Number.isFinite(numeric)) return "--";
    return numeric >= 100 ? numeric.toFixed(2) : numeric >= 1 ? numeric.toFixed(4) : numeric.toFixed(6);
  }

  quantity(value) {
    const numeric = Number(value);
    if (!Number.isFinite(numeric)) return "--";
    if (numeric !== 0 && Math.abs(numeric) < 0.0001) return numeric.toExponential(4);
    return this.number(numeric, 8);
  }

  exitReason(value) {
    const labels = {
      strategy_reversal: "策略反转",
      stop_loss: "止损",
      take_profit: "止盈",
      liquidation: "强制平仓",
      max_holding_bars: "达到最长持有",
      end_of_data: "数据结束",
    };
    return labels[value] || value || "策略退出";
  }

  compactNumber(value) {
    const numeric = Number(value);
    if (!Number.isFinite(numeric)) return "--";
    return new Intl.NumberFormat("zh-CN", { notation: "compact", maximumFractionDigits: 1 }).format(numeric);
  }

  dateOnly(value) {
    if (!value) return "";
    if (typeof value === "string" && /^\d{4}-\d{2}-\d{2}/.test(value)) return value.slice(0, 10);
    const numeric = Number(value);
    const date = new Date(Number.isFinite(numeric) && numeric > 0 && numeric < 1e12 ? numeric * 1000 : value);
    if (Number.isNaN(date.getTime())) return "";
    const year = date.getFullYear();
    const month = String(date.getMonth() + 1).padStart(2, "0");
    const day = String(date.getDate()).padStart(2, "0");
    return `${year}-${month}-${day}`;
  }

  shiftMonths(value, amount) {
    const date = new Date(`${value}T12:00:00`);
    if (Number.isNaN(date.getTime())) return "";
    date.setMonth(date.getMonth() + amount);
    return this.dateOnly(date);
  }

  maxDate(left, right) {
    if (!left) return right;
    if (!right) return left;
    return left > right ? left : right;
  }

  shortDate(value, includeTime = false) {
    if (!value) return "--";
    const numeric = Number(value);
    const date = new Date(Number.isFinite(numeric) && numeric > 0 ? (numeric < 1e12 ? numeric * 1000 : numeric) : value);
    if (Number.isNaN(date.getTime())) return String(value).slice(0, includeTime ? 16 : 10);
    return date.toLocaleString("zh-CN", includeTime ? { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false } : { year: "numeric", month: "2-digit", day: "2-digit" });
  }

  chartDate(value) {
    if (typeof value === "number" && value < 1e9) return String(value);
    const date = new Date(typeof value === "number" && value < 1e12 ? value * 1000 : value);
    if (Number.isNaN(date.getTime())) return String(value ?? "").slice(0, 10);
    return `${date.getMonth() + 1}/${date.getDate()}`;
  }
}

if (!customElements.get("backtest-workbench")) customElements.define("backtest-workbench", BacktestWorkbench);
