class PaperDashboard extends window.QuantDeskPageController {
  constructor(host) {
    super(host, { shadow: true });
    this.running = false;
    this.loading = false;
    this.timer = null;
    this.resizeObserver = null;
    this.data = null;
    this.accounts = [];
    this.selectedAccountId = null;
    this.loadSequence = 0;
    this.strategyCatalog = [];
    this.adjustStrategyRequest = 0;
    this.adjustSymbol = "";
    this.symbolCatalog = [];
    this.marketChartData = new Map();
    this.marketChartViews = new Map();
    this.marketTradeMarkers = new Map();
    this.marketChartRequest = 0;
    this.marketChartKey = "";
    this.marketChartRefreshAt = 0;
    this.marketStreamKey = "";
    this.marketStreamSymbols = [];
    this.marketStreamSocket = null;
    this.marketStreamGeneration = 0;
    this.marketStreamReconnectTimer = null;
    this.marketStreamWatchdogTimer = null;
    this.marketStreamLastMessageAt = 0;
    this.marketStreamStatus = "idle";
    this.marketRedrawFrame = null;
    this.storageKey = "quantdesk.paper.selected-account";
    this.eventsBound = false;
    this.renderShell();
  }

  connectedCallback() {
    if (!this.eventsBound) {
      this.bindEvents();
      this.eventsBound = true;
    }
    if ("ResizeObserver" in window && !this.resizeObserver) {
      this.resizeObserver = new ResizeObserver(() => {
        if (this.data) this.drawCurve(this.data.curve || [], this.data.account?.start || 10000);
        this.redrawMarketCharts();
      });
      this.resizeObserver.observe(this.q(".paper-dashboard"));
    }
  }

  disconnectedCallback() {
    this.pause();
    if (this.resizeObserver) this.resizeObserver.disconnect();
    this.resizeObserver = null;
  }

  renderShell() {
    this.shadowRoot.innerHTML = `
      <link rel="stylesheet" href="/next/assets/paper.css?v=20260905-paper-box-signals">
      <main class="paper-dashboard">
        <nav class="account-switcher" aria-label="模拟盘切换">
          <div id="paper-account-tabs" class="account-tabs" role="tablist" aria-label="我的模拟盘">
            <span class="tabs-loading">正在读取模拟盘…</span>
          </div>
          <button id="paper-create" class="create-account-button" type="button">
            <span class="create-icon" aria-hidden="true">＋</span><span class="create-label">新增模拟盘</span>
          </button>
        </nav>

        <div id="paper-account-content" role="tabpanel">
        <header class="paper-head">
          <div class="paper-title-group">
            <div class="title-row">
              <span class="title-icon" aria-hidden="true">↗</span>
              <h1>AI 自动模拟盘</h1>
              <span class="simulation-badge">PAPER</span>
            </div>
            <p id="paper-subtitle">10,000 USDT 起步 · 20x 杠杆 · 信号自动开平仓</p>
            <p id="paper-rules" class="paper-rules">正在加载策略规则…</p>
          </div>
          <div class="paper-actions">
            <span id="paper-state" class="running-state"><i></i>连接中</span>
            <span id="paper-updated" class="updated-time">--:--:--</span>
            <button id="paper-refresh" class="action-button" type="button">刷新</button>
            <button id="paper-toggle-status" class="action-button status-button" type="button" disabled>暂停运行</button>
            <button id="paper-rename" class="action-button manage-button" type="button" disabled>修改名称</button>
            <button id="paper-adjust" class="action-button manage-button" type="button" disabled>调整参数</button>
            <button id="paper-add-symbol" class="action-button symbol-button" type="button" disabled>添加交易品种</button>
            <button id="paper-reset" class="action-button reset-button" type="button" disabled title="重置当前模拟盘，不影响其他模拟盘">重置账户</button>
            <button id="paper-delete" class="action-button delete-button" type="button" disabled title="删除当前模拟盘">删除</button>
          </div>
        </header>

        <div id="paper-banner" class="paper-banner hidden" role="status" aria-live="polite"></div>

        <section id="paper-cards" class="paper-cards" aria-label="模拟账户概览">
          <article class="paper-card" data-metric="equity"><span>账户权益</span><strong>--</strong><small>收益率 --</small></article>
          <article class="paper-card" data-metric="balance"><span>可用余额</span><strong>--</strong><small>占用保证金 --</small></article>
          <article class="paper-card" data-metric="upnl"><span>浮动盈亏</span><strong>--</strong><small>今日盈亏 --</small></article>
          <article class="paper-card" data-metric="realized"><span>已实现盈亏</span><strong>--</strong><small>成交统计 --</small></article>
          <article class="paper-card" data-metric="win-rate"><span>胜率</span><strong>--</strong><small>盈亏比 --</small></article>
          <article class="paper-card" data-metric="drawdown"><span>最大回撤</span><strong>--</strong><small>仓位 --</small></article>
        </section>

        <section id="paper-market-panel" class="paper-panel market-panel">
          <div class="panel-title market-panel-title">
            <span><i class="chart-mark" aria-hidden="true"></i>交易品种 K 线 <em>M15</em></span>
            <span id="paper-market-summary" class="panel-meta">点击“添加交易品种”开始</span>
          </div>
          <div id="paper-market-charts" class="market-chart-grid">
            <div class="market-chart-empty"><strong>尚未添加交易品种</strong><span>添加后自动加载对应的 M15 K 线图，并限定当前模拟盘的新开仓范围。</span></div>
          </div>
        </section>

        <section class="paper-panel equity-panel">
          <div class="panel-title">
            <span><i class="chart-mark" aria-hidden="true"></i>账户权益曲线</span>
            <span id="curve-summary" class="panel-meta">每分钟记录一次</span>
          </div>
          <div id="chart-wrap" class="chart-wrap">
            <canvas id="paper-chart" aria-label="模拟账户权益曲线">当前浏览器不支持绘制权益曲线。</canvas>
          </div>
        </section>

        <div class="paper-columns">
          <section class="paper-panel table-panel">
            <div class="panel-title">
              <span>当前持仓 <em id="paper-pos-count">0 个</em></span>
              <span class="panel-meta">自动信号持仓</span>
            </div>
            <div id="paper-positions" class="paper-table">
              <div class="empty-state">正在读取持仓…</div>
            </div>
          </section>

          <section class="paper-panel table-panel">
            <div class="panel-title">
              <span>历史成交 <em id="paper-trade-count">0 笔</em></span>
              <span class="panel-meta">最近 50 笔</span>
            </div>
            <div id="paper-trades" class="paper-table">
              <div class="empty-state">正在读取成交…</div>
            </div>
          </section>
        </div>

        <footer id="paper-disclaimer" class="paper-disclaimer">
          模拟交易仅用于策略验证与学习，不构成投资建议。
        </footer>
        </div>

        <div id="paper-create-modal" class="paper-modal hidden" aria-hidden="true">
          <button class="modal-backdrop" type="button" data-modal-close aria-label="关闭新增模拟盘窗口"></button>
          <section class="modal-card" role="dialog" aria-modal="true" aria-labelledby="paper-create-title">
            <header class="modal-head">
              <div>
                <span class="modal-kicker">NEW PAPER ACCOUNT</span>
                <h2 id="paper-create-title">新增模拟盘</h2>
                <p>可绑定多个策略；全部策略同向满足时才会开仓。</p>
              </div>
              <button class="modal-close" type="button" data-modal-close aria-label="关闭">×</button>
            </header>
            <form id="paper-create-form" class="create-form">
              <div class="form-field form-field-wide">
                <span>绑定策略（多选）</span>
                <div id="paper-create-strategies" class="strategy-picker" role="group" aria-label="选择模拟盘策略"></div>
                <small id="paper-create-strategy-note">至少选择 1 个，最多 10 个；全部策略同向满足才开仓。</small>
              </div>
              <label class="form-field form-field-wide">
                <span>模拟盘名称</span>
                <input id="paper-create-name" type="text" maxlength="100" autocomplete="off" placeholder="例如：趋势突破测试" required>
              </label>
              <label class="form-field">
                <span>初始资金（USDT）</span>
                <input id="paper-create-balance" type="number" min="1" max="1000000000" step="any" value="10000" required>
              </label>
              <label class="form-field">
                <span>杠杆倍数</span>
                <input id="paper-create-leverage" type="number" min="1" max="20" step="1" value="20" required>
              </label>
              <p id="paper-create-error" class="form-error hidden" role="alert"></p>
              <footer class="modal-actions">
                <button class="action-button" type="button" data-modal-close>取消</button>
                <button id="paper-create-submit" class="create-account-button" type="submit">创建并运行</button>
              </footer>
            </form>
          </section>
        </div>

        <div id="paper-adjust-modal" class="paper-modal hidden" aria-hidden="true">
          <button class="modal-backdrop" type="button" data-adjust-modal-close aria-label="关闭调整参数窗口"></button>
          <section class="modal-card strategy-parameter-modal" role="dialog" aria-modal="true" aria-labelledby="paper-adjust-title">
            <header class="modal-head">
              <div>
                <span class="modal-kicker">STRATEGY PARAMETERS</span>
                <h2 id="paper-adjust-title">调整策略参数</h2>
                <p id="paper-adjust-description">修改当前模拟盘所绑定策略的默认运行参数。</p>
              </div>
              <button class="modal-close" type="button" data-adjust-modal-close aria-label="关闭">×</button>
            </header>
            <form id="paper-adjust-form" class="strategy-parameter-form">
              <label class="form-field form-field-wide">
                <span>当前绑定策略</span>
                <select id="paper-adjust-strategy" aria-label="选择要调整参数的策略"></select>
                <small id="paper-adjust-strategy-note">正在读取策略参数…</small>
              </label>
              <div id="paper-adjust-parameters" class="strategy-parameter-groups" aria-live="polite"></div>
              <p id="paper-adjust-scope-note" class="form-note">保存的是策略默认运行参数，下一轮信号计算生效；已有币种专属参数时，币种参数仍然优先。</p>
              <p id="paper-adjust-error" class="form-error hidden" role="alert"></p>
              <footer class="modal-actions">
                <button class="action-button" type="button" data-adjust-modal-close>取消</button>
                <button id="paper-adjust-submit" class="create-account-button" type="submit">保存策略参数</button>
              </footer>
            </form>
          </section>
        </div>

        <div id="paper-symbol-modal" class="paper-modal hidden" aria-hidden="true">
          <button class="modal-backdrop" type="button" data-symbol-modal-close aria-label="关闭交易品种窗口"></button>
          <section class="modal-card symbol-modal-card" role="dialog" aria-modal="true" aria-labelledby="paper-symbol-title">
            <header class="modal-head">
              <div>
                <span class="modal-kicker">TRADING SYMBOLS · M15</span>
                <h2 id="paper-symbol-title">添加交易品种</h2>
                <p>所选品种会自动加载 M15 K 线，并作为当前模拟盘的新开仓范围。</p>
              </div>
              <button class="modal-close" type="button" data-symbol-modal-close aria-label="关闭">×</button>
            </header>
            <form id="paper-symbol-form" class="symbol-form">
              <label class="form-field form-field-wide">
                <span>搜索交易品种</span>
                <input id="paper-symbol-search" type="search" maxlength="32" autocomplete="off" placeholder="输入代码，例如 AAPL、XAU">
                <small id="paper-symbol-note">最多选择 20 个品种。</small>
              </label>
              <div id="paper-symbol-picker" class="symbol-picker" role="group" aria-label="选择模拟盘交易品种"></div>
              <p id="paper-symbol-error" class="form-error hidden" role="alert"></p>
              <footer class="modal-actions">
                <button class="action-button" type="button" data-symbol-modal-close>取消</button>
                <button id="paper-symbol-submit" class="create-account-button" type="submit">保存并加载 K 线</button>
              </footer>
            </form>
          </section>
        </div>
      </main>`;
  }

  q(selector) {
    return this.shadowRoot.querySelector(selector);
  }

  async api(path = "", options = {}) {
    if (typeof window.quantdeskApi !== "function") throw new Error("认证服务尚未就绪");
    return window.quantdeskApi(`/api/v2/paper${path}`, options);
  }

  bindEvents() {
    this.q("#paper-refresh").addEventListener("click", () => this.load());
    this.q("#paper-reset").addEventListener("click", () => this.resetAccount());
    this.q("#paper-rename").addEventListener("click", () => this.renameAccount());
    this.q("#paper-adjust").addEventListener("click", () => this.openAdjustDialog());
    this.q("#paper-add-symbol").addEventListener("click", () => this.openSymbolDialog());
    this.q("#paper-delete").addEventListener("click", () => this.deleteAccount());
    this.q("#paper-toggle-status").addEventListener("click", () => this.toggleAccountStatus());
    this.q("#paper-create").addEventListener("click", () => this.openCreateDialog());
    this.q("#paper-account-tabs").addEventListener("click", (event) => {
      const tab = event.target.closest("[data-account-id]");
      if (tab) this.selectAccount(tab.dataset.accountId);
    });
    this.q("#paper-account-tabs").addEventListener("keydown", (event) => this.handleTabKeydown(event));
    this.q("#paper-create-form").addEventListener("submit", (event) => this.createAccount(event));
    this.q("#paper-create-strategies").addEventListener("change", () => this.applyStrategyDefaults());
    this.q("#paper-adjust-form").addEventListener("submit", (event) => this.adjustAccount(event));
    this.q("#paper-adjust-strategy").addEventListener("change", () => this.loadAdjustStrategyParameters());
    this.q("#paper-symbol-form").addEventListener("submit", (event) => this.savePaperSymbols(event));
    this.q("#paper-symbol-search").addEventListener("input", () => this.filterSymbolPicker());
    this.q("#paper-symbol-picker").addEventListener("change", () => this.updateSymbolSelectionNote());
    this.shadowRoot.querySelectorAll("[data-modal-close]").forEach((button) => {
      button.addEventListener("click", () => this.closeCreateDialog());
    });
    this.shadowRoot.querySelectorAll("[data-adjust-modal-close]").forEach((button) => {
      button.addEventListener("click", () => this.closeAdjustDialog());
    });
    this.shadowRoot.querySelectorAll("[data-symbol-modal-close]").forEach((button) => {
      button.addEventListener("click", () => this.closeSymbolDialog());
    });
    this.shadowRoot.addEventListener("keydown", (event) => {
      if (event.key !== "Escape") return;
      if (!this.q("#paper-symbol-modal").classList.contains("hidden")) this.closeSymbolDialog();
      else if (!this.q("#paper-adjust-modal").classList.contains("hidden")) this.closeAdjustDialog();
      else if (!this.q("#paper-create-modal").classList.contains("hidden")) this.closeCreateDialog();
    });
  }

  start() {
    if (this.running) return;
    this.running = true;
    this.setConnectionState("连接中", "loading");
    this.loadAccounts()
      .then(() => this.load())
      .catch((error) => {
        this.setConnectionState("连接异常", "error");
        this.showBanner(`模拟盘账户加载失败：${error.message}`, "error");
      });
    this.timer = window.setInterval(() => {
      if (!this.loading) this.load();
    }, 10000);
  }

  pause() {
    this.running = false;
    if (this.timer) window.clearInterval(this.timer);
    this.timer = null;
    this.stopMarketStream();
  }

  async load() {
    const requestId = ++this.loadSequence;
    const accountId = this.selectedAccountId;
    this.loading = true;
    const refreshButton = this.q("#paper-refresh");
    refreshButton.disabled = true;
    refreshButton.textContent = "刷新中";
    try {
      if (!accountId) {
        this.data = null;
        this.showBanner("还没有模拟盘，点击右上角“新增模拟盘”绑定第一个策略。", "empty");
        this.setConnectionState("待创建", "loading");
        this.q("#paper-toggle-status").disabled = true;
        this.q("#paper-reset").disabled = true;
        this.q("#paper-rename").disabled = true;
        this.q("#paper-adjust").disabled = true;
        this.q("#paper-add-symbol").disabled = true;
        this.q("#paper-delete").disabled = true;
        return;
      }
      const query = new URLSearchParams({
        account_id: accountId,
        timezone_offset_minutes: String(-new Date().getTimezoneOffset()),
      });
      const data = await this.api(`?${query.toString()}`);
      if (requestId !== this.loadSequence || accountId !== this.selectedAccountId) return;
      this.data = data;
      this.renderData(data);
      const environment = data.account?.execution_environment || {};
      const syncedTradfiSymbols = data.account?.synced_tradfi_symbols;
      const syncCountKnown = syncedTradfiSymbols !== undefined
        && syncedTradfiSymbols !== null
        && syncedTradfiSymbols !== ""
        && Number.isFinite(Number(syncedTradfiSymbols));
      const environmentMessages = {
        binance_credentials_required: "高保真模拟已阻止新开仓：请先在系统设置中配置 Binance API，只读权限即可同步账户真实费率与杠杆档位。",
        binance_private_sync_failed: "高保真模拟已阻止新开仓：Binance 账户费率或杠杆档位同步失败，请检查 API 权限与网络。",
        binance_profile_incomplete: "高保真模拟已阻止新开仓：Binance 返回的账户交易参数不完整。",
        binance_contract_rules_stale: "高保真模拟已阻止新开仓：Binance 合约规则快照已过期。",
        binance_mark_price_stale: "高保真模拟已阻止新开仓：Binance Mark Price 快照已过期。",
        binance_session_closed_or_stale: "当前 TradFi 市场休市，或交易时段快照已过期；系统不会新开仓。",
        binance_symbol_not_trading: "目标合约当前不是 Binance TRADING 状态，系统不会新开仓。",
      };
      if (!environment.ready && environmentMessages[environment.reason]) {
        this.showBanner(environmentMessages[environment.reason], "warning");
      } else if (syncCountKnown && Number(syncedTradfiSymbols) === 0) {
        this.showBanner("Binance 实盘环境尚未完成同步，系统已安全阻止新开仓。", "warning");
      } else {
        this.showBanner("");
      }
      const status = data.paper_account?.status;
      this.setConnectionState(status === "paused" ? "已暂停" : "运行中", status === "paused" ? "paused" : "success");
      this.q("#paper-updated").textContent = new Date().toLocaleTimeString("zh-CN", { hour12: false });
    } catch (error) {
      if (requestId !== this.loadSequence || accountId !== this.selectedAccountId) return;
      this.setConnectionState("连接异常", "error");
      this.showBanner(`模拟盘数据加载失败：${error.message}`, "error");
    } finally {
      if (requestId === this.loadSequence) {
        this.loading = false;
        refreshButton.disabled = false;
        refreshButton.textContent = "刷新";
      }
    }
  }

  async loadAccounts(preferredId = null) {
    const accounts = await this.api("/accounts");
    this.accounts = Array.isArray(accounts) ? accounts : [];
    const available = new Set(this.accounts.map((item) => item.id));
    const rememberedId = this.readRememberedAccount();
    this.selectedAccountId = preferredId && available.has(preferredId)
      ? preferredId
      : available.has(this.selectedAccountId)
        ? this.selectedAccountId
        : available.has(rememberedId)
          ? rememberedId
          : this.accounts.find((item) => item.status === "active")?.id || this.accounts[0]?.id || null;
    this.rememberAccount(this.selectedAccountId);
    this.renderAccountTabs();
  }

  renderAccountTabs() {
    const tabs = this.q("#paper-account-tabs");
    if (!this.accounts.length) {
      tabs.innerHTML = '<span class="tabs-empty">暂无模拟盘</span>';
      return;
    }
    tabs.innerHTML = this.accounts.map((account) => {
      const selected = account.id === this.selectedAccountId;
      const status = account.status === "paused" ? "paused" : "active";
      const strategy = account.strategy_name || account.engine_key || "未命名策略";
      return `<button class="account-tab ${status}${selected ? " selected" : ""}" type="button"
        role="tab" aria-selected="${selected}" aria-controls="paper-account-content" tabindex="${selected ? "0" : "-1"}"
        data-account-id="${this.escape(account.id)}" title="${this.escape(`${account.name} · ${strategy}`)}">
          <i aria-hidden="true"></i><span>${this.escape(account.name)}</span>
          ${account.status === "paused" ? '<small>已暂停</small>' : ""}
        </button>`;
    }).join("");
    window.requestAnimationFrame(() => {
      this.q('.account-tab[aria-selected="true"]')?.scrollIntoView({ block: "nearest", inline: "nearest" });
    });
  }

  selectAccount(accountId) {
    if (!accountId || accountId === this.selectedAccountId) return;
    if (!this.accounts.some((account) => account.id === accountId)) return;
    this.selectedAccountId = accountId;
    this.rememberAccount(accountId);
    this.renderAccountTabs();
    this.setConnectionState("切换中", "loading");
    this.load();
  }

  handleTabKeydown(event) {
    if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
    const tabs = [...this.q("#paper-account-tabs").querySelectorAll("[data-account-id]")];
    if (!tabs.length) return;
    const current = Math.max(tabs.indexOf(event.target.closest("[data-account-id]")), 0);
    let next = current;
    if (event.key === "ArrowLeft") next = (current - 1 + tabs.length) % tabs.length;
    if (event.key === "ArrowRight") next = (current + 1) % tabs.length;
    if (event.key === "Home") next = 0;
    if (event.key === "End") next = tabs.length - 1;
    event.preventDefault();
    tabs[next].focus();
    this.selectAccount(tabs[next].dataset.accountId);
  }

  async openCreateDialog() {
    try {
      const createButton = this.q("#paper-create");
      createButton.disabled = true;
      this.q("#paper-create .create-label").textContent = "读取策略…";
      await this.loadStrategyCatalog();
      if (!this.strategyCatalog.length) throw new Error("请先在策略中心创建或启用策略");
      this.renderStrategyPicker("#paper-create-strategies", [this.strategyCatalog[0].id]);
      this.q("#paper-create-balance").value = "10000";
      this.applyStrategyDefaults();
      this.showCreateError("");
      const modal = this.q("#paper-create-modal");
      modal.classList.remove("hidden");
      modal.setAttribute("aria-hidden", "false");
      window.requestAnimationFrame(() => this.q("#paper-create-strategies input")?.focus());
    } catch (error) {
      this.showBanner(`无法新增模拟盘：${error.message}`, "error");
    } finally {
      const createButton = this.q("#paper-create");
      createButton.disabled = false;
      this.q("#paper-create .create-label").textContent = "新增模拟盘";
    }
  }

  closeCreateDialog() {
    const modal = this.q("#paper-create-modal");
    modal.classList.add("hidden");
    modal.setAttribute("aria-hidden", "true");
    this.showCreateError("");
    this.q("#paper-create").focus();
  }

  async createAccount(event) {
    event.preventDefault();
    const submit = this.q("#paper-create-submit");
    const name = this.q("#paper-create-name").value.trim();
    const strategyIds = this.selectedStrategyIds("#paper-create-strategies");
    const initialBalance = Number(this.q("#paper-create-balance").value);
    const leverage = Number(this.q("#paper-create-leverage").value);
    if (!strategyIds.length || strategyIds.length > 10) {
      this.showCreateError("请选择 1 到 10 个策略；全部策略同向满足时才会开仓。");
      return;
    }
    if (!Number.isFinite(initialBalance) || initialBalance <= 0 || initialBalance > 1_000_000_000) {
      this.showCreateError("初始资金必须大于 0，且不能超过 1,000,000,000 USDT。");
      return;
    }
    if (!Number.isInteger(leverage) || leverage < 1 || leverage > 20) {
      this.showCreateError("杠杆倍数必须是 1 到 20 之间的整数。");
      return;
    }
    submit.disabled = true;
    submit.textContent = "创建中…";
    this.showCreateError("");
    try {
      const created = await this.api("/accounts", {
        method: "POST",
        body: JSON.stringify({ name, strategy_ids: strategyIds, initial_balance: initialBalance, leverage }),
      });
      this.closeCreateDialog();
      await this.loadAccounts(created.id);
      await this.load();
      this.showBanner(`模拟盘已创建；${strategyIds.length} 个策略必须全部同向满足才会开仓。`, "success");
    } catch (error) {
      this.showCreateError(`创建失败：${error.message}`);
    } finally {
      submit.disabled = false;
      submit.textContent = "创建并运行";
    }
  }

  showCreateError(message) {
    const target = this.q("#paper-create-error");
    target.textContent = message;
    target.classList.toggle("hidden", !message);
  }

  applyStrategyDefaults() {
    const strategyIds = this.selectedStrategyIds("#paper-create-strategies");
    const strategy = this.strategyCatalog.find((item) => item.id === strategyIds[0]);
    if (!strategy) return;
    this.q("#paper-create-name").value = `${strategy.name} 模拟盘`;
    this.q("#paper-create-leverage").value = String(Math.min(20, Number(strategy.risk_defaults?.leverage) || 20));
    const names = this.strategyCatalog.filter((item) => strategyIds.includes(item.id)).map((item) => item.name);
    this.q("#paper-create-strategy-note").textContent = `已选 ${names.length} 个：${names.join(" + ")}。全部同向满足才开仓。`;
  }

  async loadStrategyCatalog() {
    if (this.strategyCatalog.length) return;
    const catalog = await window.quantdeskApi("/api/v2/strategies");
    this.strategyCatalog = (catalog.items || []).filter((item) => item.status === "active");
  }

  renderStrategyPicker(selector, selectedIds = []) {
    const selected = new Set(selectedIds);
    this.q(selector).innerHTML = this.strategyCatalog.map((strategy) => `
      <label class="strategy-option">
        <input type="checkbox" value="${this.escape(strategy.id)}"${selected.has(strategy.id) ? " checked" : ""}>
        <span>
          <strong>${this.escape(strategy.name)}</strong>
          <small>${this.escape(strategy.category || strategy.engine_key || "策略")} · v${this.escape(strategy.version || "1")}</small>
        </span>
      </label>`).join("");
  }

  selectedStrategyIds(selector) {
    return [...this.q(selector).querySelectorAll('input[type="checkbox"]:checked')]
      .map((input) => input.value)
      .filter(Boolean);
  }

  async openAdjustDialog(rawSymbol = "") {
    const account = this.accounts.find((item) => item.id === this.selectedAccountId);
    if (!account) return;
    const symbol = String(rawSymbol || "").trim().toUpperCase();
    const selectedSymbols = Array.isArray(account.symbols) ? account.symbols : [];
    if (symbol && !selectedSymbols.includes(symbol)) {
      this.showBanner("该品种已不在当前模拟盘的交易范围内，请刷新后重试。", "error");
      return;
    }
    this.adjustSymbol = symbol;
    const button = symbol
      ? this.shadowRoot.querySelector(`[data-market-strategy="${symbol}"]`)
      : this.q("#paper-adjust");
    if (button) {
      button.disabled = true;
      button.textContent = "读取参数…";
    }
    try {
      await this.loadStrategyCatalog();
      if (!this.strategyCatalog.length) throw new Error("请先在策略中心创建或启用策略");
      const currentStrategyIds = Array.isArray(account.strategy_ids) && account.strategy_ids.length
        ? account.strategy_ids
        : [account.strategy_id].filter(Boolean);
      const boundStrategies = currentStrategyIds
        .map((strategyId) => this.strategyCatalog.find((item) => item.id === strategyId))
        .filter(Boolean);
      if (!boundStrategies.length) throw new Error("当前模拟盘绑定的策略不存在或已停用");
      const selector = this.q("#paper-adjust-strategy");
      selector.innerHTML = boundStrategies.map((strategy) => (
        `<option value="${this.escape(strategy.id)}">${this.escape(strategy.name)} · v${this.escape(strategy.version || "1")}</option>`
      )).join("");
      selector.disabled = boundStrategies.length === 1;
      this.q("#paper-adjust-title").textContent = symbol
        ? `${this.symbol(symbol)} 策略参数配置`
        : "调整策略参数";
      this.q("#paper-adjust-description").textContent = symbol
        ? `为 ${symbol} 单独设置交易策略参数，不影响其他品种。`
        : "修改当前模拟盘所绑定策略的默认运行参数。";
      this.q("#paper-adjust-scope-note").textContent = symbol
        ? `${symbol} 专属参数优先于策略默认参数，并在模拟盘下一轮信号计算时生效。`
        : "保存的是策略默认运行参数，下一轮信号计算生效；已有币种专属参数时，币种参数仍然优先。";
      this.q("#paper-adjust-parameters").innerHTML = '<p class="parameter-loading">正在读取策略参数…</p>';
      this.q("#paper-adjust-strategy-note").textContent = "正在读取策略参数…";
      this.showAdjustError("");
      const modal = this.q("#paper-adjust-modal");
      modal.classList.remove("hidden");
      modal.setAttribute("aria-hidden", "false");
      await this.loadAdjustStrategyParameters();
      window.requestAnimationFrame(() => (
        this.q("[data-paper-param-key]") || selector
      )?.focus());
    } catch (error) {
      this.showBanner(`无法调整参数：${error.message}`, "error");
    } finally {
      if (button) {
        button.textContent = symbol ? "策略参数" : "调整参数";
        button.disabled = !this.selectedAccountId;
      }
    }
  }

  closeAdjustDialog() {
    const symbol = this.adjustSymbol;
    const modal = this.q("#paper-adjust-modal");
    modal.classList.add("hidden");
    modal.setAttribute("aria-hidden", "true");
    this.showAdjustError("");
    const focusTarget = symbol
      ? this.shadowRoot.querySelector(`[data-market-strategy="${symbol}"]`)
      : this.q("#paper-adjust");
    this.adjustSymbol = "";
    focusTarget?.focus();
  }

  async loadAdjustStrategyParameters() {
    const strategyId = this.q("#paper-adjust-strategy").value;
    const strategy = this.strategyCatalog.find((item) => item.id === strategyId);
    if (!strategy) return;
    const requestId = ++this.adjustStrategyRequest;
    const submit = this.q("#paper-adjust-submit");
    submit.disabled = true;
    this.q("#paper-adjust-parameters").innerHTML = '<p class="parameter-loading">正在读取策略参数…</p>';
    this.q("#paper-adjust-strategy-note").textContent = `${strategy.name} · v${strategy.version || 1}`;
    this.showAdjustError("");
    try {
      const symbolQuery = this.adjustSymbol
        ? `?symbol=${encodeURIComponent(this.adjustSymbol)}`
        : "";
      const detail = await window.quantdeskApi(
        `/api/v2/backtests/strategy-parameters/${encodeURIComponent(strategyId)}${symbolQuery}`,
      );
      if (requestId !== this.adjustStrategyRequest) return;
      this.renderAdjustStrategyParameters(strategy, detail?.effective?.parameters || {});
      const scope = this.adjustSymbol
        ? (detail?.symbol_profile ? `${this.adjustSymbol} 已启用品种专属参数` : `${this.adjustSymbol} 当前继承策略默认参数`)
        : (detail?.default_profile ? "已加载保存过的默认参数" : "当前使用策略原始参数");
      this.q("#paper-adjust-strategy-note").textContent = `${strategy.name} · v${strategy.version || 1} · ${scope}`;
    } catch (error) {
      if (requestId !== this.adjustStrategyRequest) return;
      this.q("#paper-adjust-parameters").innerHTML = "";
      this.showAdjustError(`参数读取失败：${error.message}`);
    } finally {
      if (requestId === this.adjustStrategyRequest) submit.disabled = false;
    }
  }

  renderAdjustStrategyParameters(strategy, effectiveParameters = {}) {
    const definitions = Array.isArray(strategy.parameter_schema)
      ? strategy.parameter_schema
      : (Array.isArray(strategy.params) ? strategy.params : []);
    if (!definitions.length) {
      this.q("#paper-adjust-parameters").innerHTML = '<p class="parameter-loading">该策略没有可调整的运行参数。</p>';
      return;
    }
    const groups = new Map();
    definitions.forEach((parameter) => {
      const group = String(parameter.group || "策略参数");
      if (!groups.has(group)) groups.set(group, []);
      groups.get(group).push(parameter);
    });
    this.q("#paper-adjust-parameters").innerHTML = [...groups.entries()].map(([group, parameters]) => `
      <section class="strategy-parameter-group">
        <h3>${this.escape(group)}</h3>
        <div class="strategy-parameter-grid">
          ${parameters.map((parameter) => this.adjustParameterField(parameter, effectiveParameters)).join("")}
        </div>
      </section>`).join("");
  }

  adjustParameterField(parameter, effectiveParameters) {
    const key = String(parameter.key || "");
    const label = this.escape(parameter.label || key);
    const type = String(parameter.type || "number").toLowerCase();
    const rawValue = Object.prototype.hasOwnProperty.call(effectiveParameters, key)
      ? effectiveParameters[key]
      : parameter.default;
    const data = `data-paper-param-key="${this.escape(key)}" data-paper-param-type="${this.escape(type)}"`;
    const help = parameter.help ? `<small>${this.escape(parameter.help)}</small>` : "";
    if (parameter.control === "switch" || type === "boolean" || type === "bool") {
      const checked = Number(rawValue) !== 0 || rawValue === true ? " checked" : "";
      return `<label class="form-field strategy-switch"><input type="checkbox" ${data}${checked}><span>${label}</span>${help}</label>`;
    }
    if (Array.isArray(parameter.options)) {
      const options = parameter.options.map((option) => {
        const value = typeof option === "object" ? option.value : option;
        const optionLabel = typeof option === "object" ? option.label ?? value : value;
        return `<option value="${this.escape(value)}"${String(value) === String(rawValue) ? " selected" : ""}>${this.escape(optionLabel)}</option>`;
      }).join("");
      return `<label class="form-field"><span>${label}</span><select ${data}>${options}</select>${help}</label>`;
    }
    const min = parameter.min != null ? ` min="${this.escape(parameter.min)}"` : "";
    const max = parameter.max != null ? ` max="${this.escape(parameter.max)}"` : "";
    const step = parameter.step != null ? parameter.step : (type === "integer" ? 1 : "any");
    const value = rawValue != null ? ` value="${this.escape(rawValue)}"` : "";
    return `<label class="form-field"><span>${label}</span><input type="number" ${data}${min}${max} step="${this.escape(step)}"${value} required>${help}</label>`;
  }

  async adjustAccount(event) {
    event.preventDefault();
    const submit = this.q("#paper-adjust-submit");
    const strategyId = this.q("#paper-adjust-strategy").value;
    const strategy = this.strategyCatalog.find((item) => item.id === strategyId);
    const fields = [...this.shadowRoot.querySelectorAll("[data-paper-param-key]")];
    const invalid = fields.find((field) => !field.checkValidity());
    if (!strategy || !fields.length) {
      this.showAdjustError("当前策略没有可保存的运行参数。");
      return;
    }
    if (invalid) {
      invalid.reportValidity();
      this.showAdjustError("请先修正超出允许范围的策略参数。");
      return;
    }
    const params = Object.fromEntries(fields.map((field) => [
      field.dataset.paperParamKey,
      field.type === "checkbox" ? (field.checked ? 1 : 0) : Number(field.value),
    ]));
    submit.disabled = true;
    submit.textContent = "保存中…";
    this.showAdjustError("");
    try {
      await window.quantdeskApi(`/api/v2/backtests/strategy-parameters/${encodeURIComponent(strategyId)}`, {
        method: "PUT",
        body: JSON.stringify({
          scope: this.adjustSymbol ? "symbol" : "default",
          ...(this.adjustSymbol ? { symbol: this.adjustSymbol } : {}),
          params,
        }),
      });
      const scopeLabel = this.adjustSymbol
        ? `${this.adjustSymbol} 的品种专属策略参数`
        : `${strategy.name} 的策略默认参数`;
      this.marketChartRefreshAt = 0;
      await this.load();
      this.closeAdjustDialog();
      this.showBanner(`${scopeLabel}已保存，模拟盘将在下一轮信号计算时使用。`, "success");
    } catch (error) {
      this.showAdjustError(`保存失败：${error.message}`);
    } finally {
      submit.disabled = false;
      submit.textContent = "保存策略参数";
    }
  }

  showAdjustError(message) {
    const target = this.q("#paper-adjust-error");
    target.textContent = message;
    target.classList.toggle("hidden", !message);
  }

  async loadSymbolCatalog() {
    if (this.symbolCatalog.length) return;
    const response = await this.api("/symbols");
    this.symbolCatalog = Array.isArray(response?.items) ? response.items : [];
  }

  async openSymbolDialog() {
    const account = this.accounts.find((item) => item.id === this.selectedAccountId);
    if (!account) return;
    const button = this.q("#paper-add-symbol");
    button.disabled = true;
    button.textContent = "读取品种…";
    try {
      await this.loadSymbolCatalog();
      if (!this.symbolCatalog.length) throw new Error("当前没有可用的交易品种");
      const selected = new Set(Array.isArray(account.symbols) ? account.symbols : []);
      this.q("#paper-symbol-picker").innerHTML = this.symbolCatalog.map((item) => {
        const symbol = String(item.symbol || "");
        const base = this.symbol(symbol);
        const pair = String(item.pair || symbol);
        return `<label class="symbol-option" data-symbol-search="${this.escape(`${symbol} ${base} ${pair}`.toLowerCase())}">
          <input type="checkbox" value="${this.escape(symbol)}"${selected.has(symbol) ? " checked" : ""}>
          <span><strong>${this.escape(base)}</strong><small>${this.escape(symbol)} · ${this.escape(pair)}</small></span>
        </label>`;
      }).join("");
      this.q("#paper-symbol-search").value = "";
      this.updateSymbolSelectionNote();
      this.showSymbolError("");
      const modal = this.q("#paper-symbol-modal");
      modal.classList.remove("hidden");
      modal.setAttribute("aria-hidden", "false");
      window.requestAnimationFrame(() => this.q("#paper-symbol-search")?.focus());
    } catch (error) {
      this.showBanner(`无法读取交易品种：${error.message}`, "error");
    } finally {
      button.textContent = "添加交易品种";
      button.disabled = !this.selectedAccountId;
    }
  }

  closeSymbolDialog() {
    const modal = this.q("#paper-symbol-modal");
    modal.classList.add("hidden");
    modal.setAttribute("aria-hidden", "true");
    this.showSymbolError("");
    this.q("#paper-add-symbol").focus();
  }

  filterSymbolPicker() {
    const keyword = this.q("#paper-symbol-search").value.trim().toLowerCase();
    this.shadowRoot.querySelectorAll("#paper-symbol-picker .symbol-option").forEach((option) => {
      option.classList.toggle("hidden", Boolean(keyword) && !option.dataset.symbolSearch.includes(keyword));
    });
  }

  selectedPaperSymbols() {
    return [...this.shadowRoot.querySelectorAll('#paper-symbol-picker input[type="checkbox"]:checked')]
      .map((input) => input.value)
      .filter(Boolean);
  }

  updateSymbolSelectionNote() {
    const symbols = this.selectedPaperSymbols();
    this.q("#paper-symbol-note").textContent = symbols.length
      ? `已选择 ${symbols.length} / 20 个：${symbols.map((symbol) => this.symbol(symbol)).join("、")}`
      : "请至少选择 1 个交易品种，最多选择 20 个。";
  }

  async savePaperSymbols(event) {
    event.preventDefault();
    const symbols = this.selectedPaperSymbols();
    if (!symbols.length || symbols.length > 20) {
      this.showSymbolError("请选择 1 到 20 个交易品种。");
      return;
    }
    const submit = this.q("#paper-symbol-submit");
    submit.disabled = true;
    submit.textContent = "保存中…";
    this.showSymbolError("");
    try {
      const updated = await this.api(`/accounts/${encodeURIComponent(this.selectedAccountId)}/symbols`, {
        method: "PUT",
        body: JSON.stringify({ symbols }),
      });
      this.marketChartKey = "";
      this.marketChartRefreshAt = 0;
      await this.loadAccounts(updated.id);
      await this.load();
      this.closeSymbolDialog();
      this.showBanner(`已添加 ${symbols.length} 个交易品种，并加载 M15 K 线。`, "success");
    } catch (error) {
      this.showSymbolError(`保存失败：${error.message}`);
    } finally {
      submit.disabled = false;
      submit.textContent = "保存并加载 K 线";
    }
  }

  showSymbolError(message) {
    const target = this.q("#paper-symbol-error");
    target.textContent = message;
    target.classList.toggle("hidden", !message);
  }

  readRememberedAccount() {
    try {
      return window.localStorage.getItem(this.storageKey);
    } catch {
      return null;
    }
  }

  rememberAccount(accountId) {
    try {
      if (accountId) window.localStorage.setItem(this.storageKey, accountId);
      else window.localStorage.removeItem(this.storageKey);
    } catch {
      // Selection persistence is optional when browser storage is unavailable.
    }
  }

  async toggleAccountStatus() {
    const account = this.accounts.find((item) => item.id === this.selectedAccountId);
    if (!account) return;
    const nextStatus = account.status === "paused" ? "active" : "paused";
    const button = this.q("#paper-toggle-status");
    button.disabled = true;
    button.textContent = nextStatus === "paused" ? "暂停中…" : "启动中…";
    try {
      const updated = await this.api(`/accounts/${encodeURIComponent(account.id)}`, {
        method: "PATCH",
        body: JSON.stringify({ status: nextStatus }),
      });
      this.accounts = this.accounts.map((item) => item.id === updated.id ? updated : item);
      this.renderAccountTabs();
      await this.load();
      this.showBanner(nextStatus === "paused" ? "当前模拟盘已暂停，不再执行新的策略信号。" : "当前模拟盘已恢复运行。", "success");
    } catch (error) {
      this.showBanner(`状态更新失败：${error.message}`, "error");
    } finally {
      const latest = this.accounts.find((item) => item.id === this.selectedAccountId);
      button.disabled = !latest;
      button.textContent = latest?.status === "paused" ? "继续运行" : "暂停运行";
    }
  }

  async resetAccount() {
    const resetButton = this.q("#paper-reset");
    if (resetButton.disabled) return;
    if (!window.confirm("确定重置当前模拟盘？此操作只会清空该账户的持仓、成交和权益历史。")) return;
    resetButton.disabled = true;
    resetButton.textContent = "重置中";
    try {
      await this.api(`/reset?account_id=${encodeURIComponent(this.selectedAccountId)}`, { method: "POST" });
      await this.load();
      this.showBanner("当前模拟盘已恢复初始资金。", "success");
    } catch (error) {
      this.showBanner(`重置失败：${error.message}`, "error");
    } finally {
      const canReset = Boolean(this.data?.permissions?.can_reset);
      resetButton.disabled = !canReset;
      resetButton.textContent = "重置账户";
    }
  }

  async deleteAccount() {
    const account = this.accounts.find((item) => item.id === this.selectedAccountId);
    if (!account) return;
    if (Array.isArray(this.data?.positions) && this.data.positions.length) {
      this.showBanner("当前模拟盘仍有持仓，请先完成平仓后再删除。", "error");
      return;
    }
    const typed = window.prompt(`删除后“${account.name}”将从模拟盘列表移除，历史数据仅保留用于审计。请输入完整模拟盘名称确认：`, "");
    if (typed === null) return;
    if (typed !== account.name) {
      this.showBanner("输入的模拟盘名称不匹配，未执行删除。", "error");
      return;
    }
    const button = this.q("#paper-delete");
    button.disabled = true;
    button.textContent = "删除中…";
    try {
      await this.api(`/accounts/${encodeURIComponent(account.id)}`, {
        method: "PATCH",
        body: JSON.stringify({ status: "archived" }),
      });
      this.data = null;
      this.selectedAccountId = null;
      this.rememberAccount(null);
      await this.loadAccounts();
      await this.load();
      this.showBanner(`模拟盘“${account.name}”已删除。`, "success");
    } catch (error) {
      this.showBanner(`删除失败：${error.message}`, "error");
    } finally {
      button.textContent = "删除";
      button.disabled = !this.selectedAccountId;
    }
  }

  async renameAccount() {
    const account = this.accounts.find((item) => item.id === this.selectedAccountId);
    if (!account) return;
    const entered = window.prompt("请输入新的模拟盘名称：", account.name);
    if (entered === null) return;
    const name = entered.trim();
    if (!name) {
      this.showBanner("模拟盘名称不能为空。", "error");
      return;
    }
    if (name === account.name) return;
    const button = this.q("#paper-rename");
    button.disabled = true;
    button.textContent = "保存中…";
    try {
      const updated = await this.api(`/accounts/${encodeURIComponent(account.id)}`, {
        method: "PATCH",
        body: JSON.stringify({ name }),
      });
      await this.loadAccounts(updated.id);
      await this.load();
      this.showBanner(`模拟盘名称已修改为“${updated.name}”。`, "success");
    } catch (error) {
      this.showBanner(`名称修改失败：${error.message}`, "error");
    } finally {
      button.textContent = "修改名称";
      button.disabled = !this.selectedAccountId;
    }
  }

  renderData(data) {
    const account = data.account || {};
    const accountMeta = data.paper_account || {};
    const stats = data.stats || {};
    const positions = Array.isArray(data.positions) ? data.positions : [];
    const trades = Array.isArray(data.trades) ? data.trades : [];
    const rules = data.rules || {};
    const closedTotal = stats.closed_total ?? stats.trades ?? 0;
    const canReset = Boolean(data.permissions?.can_reset);

    this.q("#paper-subtitle").textContent = `${accountMeta.name || "当前模拟盘"} · ${accountMeta.strategy_name || accountMeta.engine_key || "独立策略"} · ${this.number(account.start, 0)} USDT 起步 · ${this.number(account.leverage, 0)}x 杠杆`;
    this.q("#paper-rules").textContent = [rules.tiers, rules.exits, rules.limits].filter(Boolean).join(" ｜ ") || "策略规则加载中";
    this.q("#curve-summary").textContent = `本金 ${this.number(account.start, 0)} U · 最大回撤 ${this.number(stats.max_drawdown)}%`;

    const resetButton = this.q("#paper-reset");
    resetButton.disabled = !canReset;
    resetButton.textContent = "重置账户";
    resetButton.title = "只重置当前用户选中的模拟盘";
    this.q("#paper-delete").disabled = false;
    this.q("#paper-rename").disabled = false;
    this.q("#paper-adjust").disabled = false;
    this.q("#paper-add-symbol").disabled = false;
    const statusButton = this.q("#paper-toggle-status");
    statusButton.disabled = false;
    statusButton.textContent = accountMeta.status === "paused" ? "继续运行" : "暂停运行";
    statusButton.classList.toggle("resume-button", accountMeta.status === "paused");

    this.renderMetric("equity", `${this.number(account.equity)} U`, `收益率 ${this.signed(account.ret_pct)}%`, this.tone(account.ret_pct));
    this.renderMetric("balance", `${this.number(account.balance)} U`, `占用保证金 ${this.number(account.used_margin)} U（${this.number(account.margin_usage, 1)}%）`, "neutral");
    this.renderMetric("upnl", `${this.signed(account.upnl)} U`, `今日盈亏 ${this.signed(account.today_pnl)} U`, this.tone(account.upnl));
    this.renderMetric("realized", `${this.signed(stats.realized)} U`, `开仓 ${this.number(stats.entries, 0)} 次 · 已平仓 ${this.number(closedTotal, 0)} 笔（近百笔 ${this.number(stats.wins, 0)}胜/${this.number(stats.losses, 0)}负）`, this.tone(stats.realized));
    this.renderMetric("win-rate", Number(stats.trades) ? `${this.number(stats.win_rate, 1)}%` : "--", `盈亏比 ${stats.profit_factor ?? "--"}`, "warning");
    const riskSummary = account.risk_per_trade_pct != null
      ? ` · 单笔风险 ${this.number(account.risk_per_trade_pct, 2)}%`
      : "";
    this.renderMetric("drawdown", `${this.number(stats.max_drawdown)}%`, `仓位 ${positions.length}/${this.number(account.max_positions, 0)}${riskSummary}`, Number(stats.max_drawdown) > 10 ? "negative" : "warning");

    this.renderPositions(positions);
    this.renderTrades(trades);
    this.marketTradeMarkers = this.paperMarketTradeMarkers(positions, trades);
    this.q("#paper-disclaimer").textContent = `风险提示：${data.disclaimer || "模拟交易仅用于策略验证与学习，不构成投资建议。"}${rules.costs ? ` 成本模型：${rules.costs}` : ""}`;
    window.requestAnimationFrame(() => this.drawCurve(data.curve || [], account.start || 10000));
    const selectedAccount = this.accounts.find((item) => item.id === this.selectedAccountId);
    const selectedSymbols = Array.isArray(accountMeta.symbols) && accountMeta.symbols.length
      ? accountMeta.symbols
      : (Array.isArray(selectedAccount?.symbols) ? selectedAccount.symbols : []);
    this.syncMarketCharts(selectedSymbols);
    this.redrawMarketCharts();
  }

  syncMarketCharts(rawSymbols) {
    const symbols = [...new Set((Array.isArray(rawSymbols) ? rawSymbols : [])
      .map((symbol) => String(symbol || "").trim().toUpperCase())
      .filter(Boolean))];
    const key = `${this.selectedAccountId || ""}:${symbols.join(",")}`;
    if (!symbols.length) {
      this.stopMarketStream();
      this.marketChartKey = key;
      this.marketChartData.clear();
      this.marketChartViews.clear();
      this.q("#paper-market-summary").textContent = "点击“添加交易品种”开始";
      this.q("#paper-market-charts").innerHTML = '<div class="market-chart-empty"><strong>尚未添加交易品种</strong><span>添加后自动加载对应的 M15 K 线图，并限定当前模拟盘的新开仓范围。</span></div>';
      return;
    }
    const now = Date.now();
    if (key === this.marketChartKey && now < this.marketChartRefreshAt) {
      this.startMarketStream(symbols, key);
      return;
    }
    const changed = key !== this.marketChartKey;
    this.marketChartKey = key;
    this.marketChartRefreshAt = now + 60_000;
    if (changed) {
      this.stopMarketStream();
      this.marketChartData.clear();
      this.marketChartViews.clear();
      this.q("#paper-market-charts").innerHTML = symbols.map((symbol) => `
        <article class="market-chart-card loading">
          <header><strong>${this.escape(this.symbol(symbol))}</strong><span>M15 · 正在加载</span></header>
          <div class="market-chart-loading">读取 K 线…</div>
        </article>`).join("");
    }
    this.loadMarketCharts(symbols, key);
  }

  async loadMarketCharts(symbols, key) {
    const requestId = ++this.marketChartRequest;
    const results = await Promise.all(symbols.map(async (symbol) => {
      try {
        const [candles, overlay] = await Promise.all([
          window.quantdeskApi(
            `/api/v2/monitor/klines?symbol=${encodeURIComponent(symbol)}&tf=15m&limit=160`,
          ),
          this.api(
            `/accounts/${encodeURIComponent(this.selectedAccountId)}/chart-overlay?symbol=${encodeURIComponent(symbol)}`,
          ).catch((error) => ({ available: false, reason: error.message || "箱体读取失败", levels: [] })),
        ]);
        const historical = this.normalizeMarketCandles(candles);
        const previous = this.marketChartData.get(symbol) || {};
        const current = previous.candles || [];
        return [symbol, {
          ...previous,
          candles: this.mergeMarketCandles(historical, current),
          boxAvailable: Boolean(overlay?.available),
          boxReason: String(overlay?.reason || ""),
          boxSource: String(overlay?.source || ""),
          boxTimeframe: String(overlay?.timeframe || "15m"),
          boxLevels: this.normalizeMarketBoxLevels(overlay?.levels),
          error: "",
        }];
      } catch (error) {
        const previous = this.marketChartData.get(symbol) || {};
        const current = previous.candles || [];
        return [symbol, { ...previous, candles: current, error: current.length ? "" : (error.message || "K 线读取失败") }];
      }
    }));
    if (requestId !== this.marketChartRequest || key !== this.marketChartKey) return;
    this.marketChartData = new Map(results);
    this.renderMarketCharts(symbols);
    this.startMarketStream(symbols, key);
  }

  renderMarketCharts(symbols) {
    this.q("#paper-market-charts").innerHTML = symbols.map((symbol) => {
      const state = this.marketChartData.get(symbol) || { candles: [], error: "" };
      const last = state.candles[state.candles.length - 1] || {};
      const livePrice = Number(state.lastPrice);
      const close = Number.isFinite(livePrice) ? livePrice : Number(last.close);
      const open = Number(last.open);
      const changePercent = Number(state.changePercent);
      const changePrice = Number(state.changePrice);
      const toneValue = Number.isFinite(changePercent) ? changePercent : close - open;
      const tone = Number.isFinite(toneValue) ? (toneValue >= 0 ? "up" : "down") : "";
      const changeText = Number.isFinite(changePrice) && Number.isFinite(changePercent)
        ? `今日 ${this.signedPrice(changePrice)}（${this.signed(changePercent)}%）`
        : "今日 --（--%）";
      const marketStatus = state.lastQuoteAt
        ? `M15 · 实时 · ${this.marketCandleTime(last.open_time)}`
        : "M15 · 等待实时行情";
      const boxLegendTitle = state.boxAvailable
        ? `突破箱体 · ${String(state.boxTimeframe || "15m").toUpperCase()}`
        : (state.boxReason || "当前策略没有可绘制的突破箱体");
      if (!state.candles.length) {
        return `<article class="market-chart-card unavailable" data-market-symbol="${this.escape(symbol)}">
          <header>
            <div class="market-symbol-heading">
              <div class="market-symbol-name"><strong>${this.escape(this.symbol(symbol))}</strong><small>${this.escape(symbol)}</small></div>
              <button class="market-strategy-button" type="button" data-market-strategy="${this.escape(symbol)}" aria-label="配置 ${this.escape(symbol)} 专属策略参数">策略参数</button>
            </div>
            <span>M15</span>
          </header>
          <div class="market-chart-loading">${this.escape(state.error || "暂无 M15 K 线")}</div>
        </article>`;
      }
      return `<article class="market-chart-card" data-market-symbol="${this.escape(symbol)}">
        <header>
          <div class="market-symbol-heading">
            <div class="market-symbol-name"><strong>${this.escape(this.symbol(symbol))}</strong><small>${this.escape(symbol)}</small></div>
            <button class="market-strategy-button" type="button" data-market-strategy="${this.escape(symbol)}" aria-label="配置 ${this.escape(symbol)} 专属策略参数">策略参数</button>
          </div>
          <div class="market-chart-quote ${tone}">
            <strong data-market-price>${this.price(close)}</strong>
            <span class="market-chart-change" data-market-change title="今日涨跌价格（今日涨跌幅）">${changeText}</span>
            <span data-market-status>${marketStatus}</span>
          </div>
        </header>
        <div class="market-canvas-wrap">
          <canvas class="market-canvas" data-market-canvas="${this.escape(symbol)}" tabindex="0" aria-label="${this.escape(this.symbol(symbol))} M15 可拖拽实时 K 线图"></canvas>
          <span class="market-chart-gesture">拖拽平移 · 滚轮缩放 · 双击回到最新</span>
          <div class="market-chart-legend" title="${this.escape(boxLegendTitle)}">
            <span class="box-upper">箱体上沿</span><span class="box-lower">箱体下沿</span>
            <span class="signal-buy">买点</span><span class="signal-sell">卖点</span><span class="signal-close">平仓点</span>
          </div>
        </div>
        <footer data-market-ohlc>O ${this.price(last.open)}　H ${this.price(last.high)}　L ${this.price(last.low)}　C ${this.price(last.close)}</footer>
      </article>`;
    }).join("");
    this.bindMarketChartInteractions();
    this.updateMarketChartSummary(symbols);
    window.requestAnimationFrame(() => this.redrawMarketCharts());
  }

  redrawMarketCharts() {
    this.marketChartData.forEach((state, symbol) => {
      if (!state?.candles?.length) return;
      const card = this.shadowRoot.querySelector(`[data-market-symbol="${symbol}"]`);
      const canvas = card?.querySelector("canvas");
      if (canvas) this.drawMarketCandles(canvas, state.candles, symbol);
    });
  }

  redrawMarketChart(symbol) {
    const state = this.marketChartData.get(symbol);
    const canvas = this.shadowRoot.querySelector(`[data-market-symbol="${symbol}"] canvas`);
    if (canvas && state?.candles?.length) this.drawMarketCandles(canvas, state.candles, symbol);
  }

  normalizeMarketCandles(rawCandles) {
    const normalized = (Array.isArray(rawCandles) ? rawCandles : []).map((item) => {
      const rawTime = Number(item.open_time);
      return {
        open_time: rawTime > 10_000_000_000 ? rawTime : rawTime * 1000,
        open: Number(item.open),
        high: Number(item.high),
        low: Number(item.low),
        close: Number(item.close),
        volume: Number(item.volume || 0),
        realtime: Boolean(item.realtime),
      };
    }).filter((item) => Number.isFinite(item.open_time)
      && [item.open, item.high, item.low, item.close].every(Number.isFinite));
    const byTime = new Map(normalized.map((item) => [item.open_time, item]));
    return [...byTime.values()].sort((left, right) => left.open_time - right.open_time).slice(-300);
  }

  normalizeMarketBoxLevels(rawLevels) {
    return (Array.isArray(rawLevels) ? rawLevels : []).map((item) => {
      const rawTime = Number(item?.open_time);
      const high = item?.high == null ? null : Number(item.high);
      const low = item?.low == null ? null : Number(item.low);
      return {
        open_time: rawTime > 10_000_000_000 ? rawTime : rawTime * 1000,
        high: Number.isFinite(high) ? high : null,
        low: Number.isFinite(low) ? low : null,
      };
    }).filter((item) => Number.isFinite(item.open_time))
      .sort((left, right) => left.open_time - right.open_time)
      .slice(-500);
  }

  paperMarketTradeMarkers(positions, trades) {
    const markers = new Map();
    const add = (symbol, type, rawTime, rawPrice, status = "closed") => {
      const normalizedSymbol = String(symbol || "").trim().toUpperCase();
      let timestamp = Number(rawTime);
      const price = Number(rawPrice);
      if (!normalizedSymbol || !Number.isFinite(timestamp) || !Number.isFinite(price) || price <= 0) return;
      if (timestamp < 10_000_000_000) timestamp *= 1000;
      const list = markers.get(normalizedSymbol) || [];
      const key = `${type}:${Math.round(timestamp)}:${price.toPrecision(12)}`;
      if (!list.some((item) => item.key === key)) list.push({ key, type, time: timestamp, price, status });
      markers.set(normalizedSymbol, list);
    };
    (Array.isArray(trades) ? trades : []).forEach((trade) => {
      const type = Number(trade.side) >= 0 ? "buy" : "sell";
      add(trade.symbol, type, trade.opened_ts, trade.entry_price);
      add(trade.symbol, "close", trade.closed_ts, trade.exit_price);
    });
    (Array.isArray(positions) ? positions : []).forEach((position) => {
      const type = Number(position.side) >= 0 ? "buy" : "sell";
      add(position.symbol, type, position.opened_ts, position.avg_entry, "open");
    });
    markers.forEach((items) => items.sort((left, right) => left.time - right.time));
    return markers;
  }

  mergeMarketCandles(historical, current) {
    const byTime = new Map(this.normalizeMarketCandles(historical).map((item) => [item.open_time, item]));
    this.normalizeMarketCandles(current).forEach((item) => {
      const stored = byTime.get(item.open_time);
      if (!stored) byTime.set(item.open_time, item);
      else if (item.realtime) byTime.set(item.open_time, {
        ...stored,
        high: Math.max(stored.high, item.high),
        low: Math.min(stored.low, item.low),
        close: item.close,
        volume: Math.max(stored.volume || 0, item.volume || 0),
        realtime: true,
      });
    });
    return [...byTime.values()].sort((left, right) => left.open_time - right.open_time).slice(-300);
  }

  marketChartView(symbol, candleCount) {
    let view = this.marketChartViews.get(symbol);
    if (!view) {
      const visible = Math.min(80, candleCount);
      view = { start: Math.max(0, candleCount - visible), visible, followLatest: true, layout: null };
      this.marketChartViews.set(symbol, view);
    }
    view.visible = Math.max(1, Math.min(view.visible || 80, candleCount));
    const maxStart = Math.max(0, candleCount - view.visible);
    view.start = view.followLatest ? maxStart : Math.max(0, Math.min(view.start || 0, maxStart));
    return view;
  }

  drawMarketCandles(canvas, rawCandles, symbol = canvas.dataset.marketCanvas || "") {
    const width = Math.floor(canvas.clientWidth);
    const height = Math.floor(canvas.clientHeight);
    if (!width || !height) return;
    const ratio = Math.min(window.devicePixelRatio || 1, 2);
    canvas.width = Math.floor(width * ratio);
    canvas.height = Math.floor(height * ratio);
    const context = canvas.getContext("2d");
    context.setTransform(ratio, 0, 0, ratio, 0, 0);
    context.clearRect(0, 0, width, height);
    const allCandles = this.normalizeMarketCandles(rawCandles);
    if (!allCandles.length) return;
    const view = this.marketChartView(symbol, allCandles.length);
    const candles = allCandles.slice(view.start, view.start + view.visible);
    const padding = { left: 10, right: 62, top: 13, bottom: 23 };
    const plotWidth = Math.max(1, width - padding.left - padding.right);
    const plotHeight = Math.max(1, height - padding.top - padding.bottom);
    const candleWidth = plotWidth / candles.length;
    view.layout = { candleWidth, plotWidth, left: padding.left };
    const lowerBound = (timestamp) => {
      let left = 0;
      let right = allCandles.length;
      while (left < right) {
        const middle = Math.floor((left + right) / 2);
        if (allCandles[middle].open_time < timestamp) left = middle + 1;
        else right = middle;
      }
      return left;
    };
    const candleIndexAt = (timestamp) => {
      const next = lowerBound(timestamp);
      if (next < allCandles.length && allCandles[next].open_time === timestamp) return next;
      return Math.max(0, Math.min(allCandles.length - 1, next - 1));
    };
    const state = this.marketChartData.get(symbol) || {};
    const boxLevels = Array.isArray(state.boxLevels) ? state.boxLevels : [];
    const boxSegments = [];
    boxLevels.forEach((level, index) => {
      if (!Number.isFinite(level.high) || !Number.isFinite(level.low)) return;
      const nextTime = boxLevels[index + 1]?.open_time
        ?? (level.open_time + (state.boxTimeframe === "1h" ? 3_600_000 : state.boxTimeframe === "30m" ? 1_800_000 : 900_000));
      const startIndex = lowerBound(level.open_time);
      const endIndex = lowerBound(nextTime);
      if (endIndex < view.start || startIndex > view.start + candles.length) return;
      boxSegments.push({ ...level, startIndex, endIndex });
    });
    const markers = (this.marketTradeMarkers.get(symbol) || []).map((marker) => ({
      ...marker,
      index: candleIndexAt(marker.time),
    })).filter((marker) => marker.index >= view.start && marker.index < view.start + candles.length);
    let high = Math.max(...candles.map((item) => item.high));
    let low = Math.min(...candles.map((item) => item.low));
    boxSegments.forEach((segment) => {
      high = Math.max(high, segment.high);
      low = Math.min(low, segment.low);
    });
    markers.forEach((marker) => {
      high = Math.max(high, marker.price);
      low = Math.min(low, marker.price);
    });
    const pricePadding = (high - low || Math.abs(high) * .005 || 1) * .08;
    high += pricePadding;
    low -= pricePadding;
    const priceRange = high - low || 1;
    const x = (index) => padding.left + (index + .5) * plotWidth / candles.length;
    const y = (price) => padding.top + (high - price) / priceRange * plotHeight;
    context.font = "11px ui-monospace, SFMono-Regular, Menlo, monospace";
    context.lineWidth = 1;
    for (let line = 0; line <= 4; line += 1) {
      const price = high - priceRange * line / 4;
      const lineY = y(price);
      context.strokeStyle = "rgba(110, 136, 160, .16)";
      context.beginPath(); context.moveTo(padding.left, lineY); context.lineTo(width - padding.right, lineY); context.stroke();
      context.fillStyle = "#71859a";
      context.fillText(this.price(price), width - padding.right + 6, lineY + 4);
    }
    context.save();
    context.beginPath();
    context.rect(padding.left, padding.top, plotWidth, plotHeight);
    context.clip();
    context.lineWidth = 1.35;
    boxSegments.forEach((segment) => {
      const startX = Math.max(padding.left, padding.left + (segment.startIndex - view.start) * candleWidth);
      const endX = Math.min(width - padding.right, padding.left + (segment.endIndex - view.start) * candleWidth);
      if (endX <= startX) return;
      context.strokeStyle = "rgba(28, 222, 214, .96)";
      context.beginPath(); context.moveTo(startX, y(segment.high)); context.lineTo(endX, y(segment.high)); context.stroke();
      context.strokeStyle = "rgba(28, 222, 214, .78)";
      context.beginPath(); context.moveTo(startX, y(segment.low)); context.lineTo(endX, y(segment.low)); context.stroke();
    });
    context.restore();
    const bodyWidth = Math.max(1, Math.min(7, plotWidth / candles.length * .68));
    candles.forEach((candle, index) => {
      const candleX = x(index);
      const rising = candle.close >= candle.open;
      context.strokeStyle = rising ? "#26d69c" : "#f05b72";
      context.fillStyle = context.strokeStyle;
      context.beginPath(); context.moveTo(candleX, y(candle.high)); context.lineTo(candleX, y(candle.low)); context.stroke();
      const top = y(Math.max(candle.open, candle.close));
      const bottom = y(Math.min(candle.open, candle.close));
      context.fillRect(candleX - bodyWidth / 2, top, bodyWidth, Math.max(1, bottom - top));
    });
    const last = candles[candles.length - 1];
    const lastY = y(last.close);
    context.setLineDash([4, 4]);
    context.strokeStyle = "rgba(216, 226, 236, .5)";
    context.beginPath(); context.moveTo(padding.left, lastY); context.lineTo(width - padding.right, lastY); context.stroke();
    context.setLineDash([]);
    context.fillStyle = "#dce8f4";
    context.fillText(this.price(last.close), width - padding.right + 6, lastY + 4);
    markers.forEach((marker) => {
      const markerX = x(marker.index - view.start);
      const markerY = y(marker.price);
      const style = marker.type === "buy"
        ? { color: "#35e8a8", label: "买" }
        : marker.type === "sell"
          ? { color: "#ff6178", label: "卖" }
          : { color: "#ffd34e", label: "平" };
      context.save();
      context.fillStyle = style.color;
      context.strokeStyle = "rgba(5, 10, 15, .95)";
      context.lineWidth = 2;
      context.beginPath();
      if (marker.type === "buy") {
        context.moveTo(markerX, markerY - 8);
        context.lineTo(markerX - 6, markerY + 3);
        context.lineTo(markerX + 6, markerY + 3);
      } else if (marker.type === "sell") {
        context.moveTo(markerX, markerY + 8);
        context.lineTo(markerX - 6, markerY - 3);
        context.lineTo(markerX + 6, markerY - 3);
      } else {
        context.moveTo(markerX, markerY - 6);
        context.lineTo(markerX + 6, markerY);
        context.lineTo(markerX, markerY + 6);
        context.lineTo(markerX - 6, markerY);
      }
      context.closePath();
      context.stroke();
      context.fill();
      context.font = "bold 10px system-ui, sans-serif";
      context.textAlign = "center";
      context.fillText(style.label, markerX, marker.type === "sell" ? markerY - 9 : markerY + 15);
      context.restore();
    });
    const first = candles[0];
    context.fillStyle = "#62768b";
    context.fillText(this.marketCandleTime(first.open_time), padding.left, height - 6);
    const lastLabel = this.marketCandleTime(last.open_time);
    context.fillText(lastLabel, Math.max(padding.left, width - padding.right - context.measureText(lastLabel).width), height - 6);
  }

  bindMarketChartInteractions() {
    this.shadowRoot.querySelectorAll("[data-market-strategy]").forEach((button) => {
      button.addEventListener("click", () => this.openAdjustDialog(button.dataset.marketStrategy));
    });
    this.shadowRoot.querySelectorAll("[data-market-canvas]").forEach((canvas) => {
      const symbol = canvas.dataset.marketCanvas;
      const move = (event) => {
        const drag = canvas.marketDrag;
        if (!drag) return;
        const state = this.marketChartData.get(symbol);
        const view = this.marketChartViews.get(symbol);
        if (!state?.candles?.length || !view) return;
        const candleWidth = Math.max(1, view.layout?.candleWidth || canvas.clientWidth / view.visible);
        const shift = Math.round((event.clientX - drag.x) / candleWidth);
        const maxStart = Math.max(0, state.candles.length - view.visible);
        view.start = Math.max(0, Math.min(drag.start - shift, maxStart));
        view.followLatest = view.start === maxStart;
        this.redrawMarketChart(symbol);
      };
      const end = (event) => {
        if (!canvas.marketDrag) return;
        canvas.marketDrag = null;
        canvas.classList.remove("dragging");
        if (canvas.hasPointerCapture?.(event.pointerId)) canvas.releasePointerCapture(event.pointerId);
      };
      canvas.addEventListener("pointerdown", (event) => {
        if (event.button !== 0) return;
        const state = this.marketChartData.get(symbol);
        const view = this.marketChartView(symbol, state?.candles?.length || 0);
        canvas.marketDrag = { x: event.clientX, start: view.start };
        canvas.classList.add("dragging");
        canvas.setPointerCapture?.(event.pointerId);
        event.preventDefault();
      });
      canvas.addEventListener("pointermove", move);
      canvas.addEventListener("pointerup", end);
      canvas.addEventListener("pointercancel", end);
      canvas.addEventListener("wheel", (event) => {
        const rect = canvas.getBoundingClientRect();
        const anchor = Math.max(0, Math.min(1, (event.clientX - rect.left) / Math.max(1, rect.width)));
        this.zoomMarketChart(symbol, event.deltaY > 0 ? 1.18 : .84, anchor);
        event.preventDefault();
      }, { passive: false });
      canvas.addEventListener("dblclick", () => this.resetMarketChartView(symbol));
      canvas.addEventListener("keydown", (event) => this.handleMarketChartKey(event, symbol));
    });
  }

  zoomMarketChart(symbol, factor, anchor = .5) {
    const state = this.marketChartData.get(symbol);
    if (!state?.candles?.length) return;
    const view = this.marketChartView(symbol, state.candles.length);
    const minVisible = Math.min(24, state.candles.length);
    const nextVisible = Math.max(minVisible, Math.min(state.candles.length, Math.round(view.visible * factor)));
    const anchorIndex = view.start + view.visible * anchor;
    view.visible = nextVisible;
    const maxStart = Math.max(0, state.candles.length - nextVisible);
    view.start = Math.max(0, Math.min(Math.round(anchorIndex - nextVisible * anchor), maxStart));
    view.followLatest = view.start === maxStart;
    this.redrawMarketChart(symbol);
  }

  resetMarketChartView(symbol) {
    const state = this.marketChartData.get(symbol);
    if (!state?.candles?.length) return;
    const visible = Math.min(80, state.candles.length);
    this.marketChartViews.set(symbol, {
      start: Math.max(0, state.candles.length - visible), visible, followLatest: true, layout: null,
    });
    this.redrawMarketChart(symbol);
  }

  handleMarketChartKey(event, symbol) {
    const state = this.marketChartData.get(symbol);
    if (!state?.candles?.length) return;
    const view = this.marketChartView(symbol, state.candles.length);
    const maxStart = Math.max(0, state.candles.length - view.visible);
    if (event.key === "ArrowLeft" || event.key === "ArrowRight") {
      const direction = event.key === "ArrowLeft" ? -1 : 1;
      view.start = Math.max(0, Math.min(view.start + direction * Math.max(1, Math.round(view.visible * .1)), maxStart));
      view.followLatest = view.start === maxStart;
      this.redrawMarketChart(symbol);
    } else if (event.key === "+" || event.key === "=") this.zoomMarketChart(symbol, .84);
    else if (event.key === "-") this.zoomMarketChart(symbol, 1.18);
    else if (event.key === "Home" || event.key === "0") this.resetMarketChartView(symbol);
    else return;
    event.preventDefault();
  }

  startMarketStream(symbols, key) {
    if (!this.running || !symbols.length || typeof window.quantdeskOpenPaperMarketSocket !== "function") return;
    const connected = this.marketStreamSocket && this.marketStreamSocket.readyState <= WebSocket.OPEN;
    if (this.marketStreamKey === key && (connected || this.marketStreamReconnectTimer)) return;
    this.stopMarketStream();
    this.marketStreamKey = key;
    this.marketStreamSymbols = [...symbols];
    this.marketStreamStatus = "connecting";
    const generation = ++this.marketStreamGeneration;
    this.updateMarketChartSummary(symbols);
    this.connectMarketStream(generation);
    this.marketStreamWatchdogTimer = window.setInterval(() => {
      if (generation !== this.marketStreamGeneration || !this.marketStreamSocket) return;
      if (this.marketStreamSocket.readyState === WebSocket.OPEN
          && this.marketStreamLastMessageAt
          && Date.now() - this.marketStreamLastMessageAt > 12_000) {
        this.marketStreamSocket.close(4000, "market stream timeout");
      }
    }, 3000);
  }

  async connectMarketStream(generation) {
    try {
      const socket = await window.quantdeskOpenPaperMarketSocket(this.marketStreamSymbols);
      if (generation !== this.marketStreamGeneration || !this.running) {
        socket.close();
        return;
      }
      this.marketStreamSocket = socket;
      socket.addEventListener("open", () => {
        if (generation !== this.marketStreamGeneration) return;
        this.marketStreamLastMessageAt = Date.now();
        this.marketStreamStatus = "live";
        this.updateMarketChartSummary(this.marketStreamSymbols);
      });
      socket.addEventListener("message", (event) => {
        if (generation !== this.marketStreamGeneration) return;
        this.marketStreamLastMessageAt = Date.now();
        try {
          const message = JSON.parse(event.data);
          if (message.event === "markets" && Array.isArray(message.data?.items)) {
            this.applyMarketSnapshots(message.data.items, message.data.server_sent_at_ms);
          }
        } catch {
          // Ignore one malformed frame; the watchdog will reconnect a stalled stream.
        }
      });
      socket.addEventListener("error", () => socket.close());
      socket.addEventListener("close", () => {
        if (this.marketStreamSocket === socket) this.marketStreamSocket = null;
        if (generation !== this.marketStreamGeneration || !this.running || !this.marketStreamKey) return;
        this.marketStreamStatus = "reconnecting";
        this.updateMarketChartSummary(this.marketStreamSymbols);
        this.marketStreamReconnectTimer = window.setTimeout(() => {
          this.marketStreamReconnectTimer = null;
          this.connectMarketStream(generation);
        }, 2500);
      });
    } catch {
      if (generation !== this.marketStreamGeneration || !this.running || !this.marketStreamKey) return;
      this.marketStreamStatus = "reconnecting";
      this.updateMarketChartSummary(this.marketStreamSymbols);
      this.marketStreamReconnectTimer = window.setTimeout(() => {
        this.marketStreamReconnectTimer = null;
        this.connectMarketStream(generation);
      }, 2500);
    }
  }

  stopMarketStream() {
    this.marketStreamGeneration += 1;
    if (this.marketStreamReconnectTimer) window.clearTimeout(this.marketStreamReconnectTimer);
    if (this.marketStreamWatchdogTimer) window.clearInterval(this.marketStreamWatchdogTimer);
    if (this.marketRedrawFrame) window.cancelAnimationFrame(this.marketRedrawFrame);
    this.marketStreamReconnectTimer = null;
    this.marketStreamWatchdogTimer = null;
    this.marketRedrawFrame = null;
    const socket = this.marketStreamSocket;
    this.marketStreamSocket = null;
    if (socket && socket.readyState < WebSocket.CLOSING) socket.close(1000, "paper page paused");
    this.marketStreamKey = "";
    this.marketStreamSymbols = [];
    this.marketStreamStatus = "idle";
    this.marketStreamLastMessageAt = 0;
  }

  applyMarketSnapshots(items, serverSentAt) {
    const changed = [];
    items.forEach((snapshot) => {
      const symbol = String(snapshot?.symbol || "").trim().toUpperCase();
      const price = Number(snapshot?.price);
      const state = this.marketChartData.get(symbol);
      if (!state?.candles?.length || !Number.isFinite(price) || price <= 0) return;
      const rawTimestamp = Number(snapshot.ticker_updated_at || serverSentAt || Date.now());
      const timestamp = rawTimestamp > 10_000_000_000 ? rawTimestamp : rawTimestamp * 1000;
      const bucket = Math.floor(timestamp / 900_000) * 900_000;
      const candles = state.candles;
      let last = candles[candles.length - 1];
      if (bucket === last.open_time) {
        last.high = Math.max(last.high, price);
        last.low = Math.min(last.low, price);
        last.close = price;
        last.realtime = true;
      } else if (bucket > last.open_time) {
        const previousClose = last.close;
        candles.push({
          open_time: bucket,
          open: previousClose,
          high: Math.max(previousClose, price),
          low: Math.min(previousClose, price),
          close: price,
          volume: 0,
          realtime: true,
        });
        if (candles.length > 300) {
          const removed = candles.length - 300;
          candles.splice(0, removed);
          const currentView = this.marketChartViews.get(symbol);
          if (currentView && !currentView.followLatest) currentView.start = Math.max(0, currentView.start - removed);
        }
        last = candles[candles.length - 1];
        const view = this.marketChartViews.get(symbol);
        if (view?.followLatest) view.start = Math.max(0, candles.length - view.visible);
      } else {
        return;
      }
      state.lastQuoteAt = timestamp;
      state.lastPrice = price;
      const changePercent = Number(snapshot.pct_24h);
      let changePrice = Number(snapshot.price_change_24h);
      if (!Number.isFinite(changePrice) && Number.isFinite(changePercent) && changePercent > -100) {
        changePrice = price - price / (1 + changePercent / 100);
      }
      state.changePercent = Number.isFinite(changePercent) ? changePercent : null;
      state.changePrice = Number.isFinite(changePrice) ? changePrice : null;
      const card = this.shadowRoot.querySelector(`[data-market-symbol="${symbol}"]`);
      const quote = card?.querySelector(".market-chart-quote");
      const priceNode = card?.querySelector("[data-market-price]");
      const changeNode = card?.querySelector("[data-market-change]");
      const statusNode = card?.querySelector("[data-market-status]");
      const footer = card?.querySelector("[data-market-ohlc]");
      const changeTone = Number.isFinite(changePercent) ? changePercent : last.close - last.open;
      quote?.classList.toggle("up", changeTone >= 0);
      quote?.classList.toggle("down", changeTone < 0);
      if (priceNode) priceNode.textContent = this.price(price);
      if (changeNode) {
        changeNode.textContent = Number.isFinite(changePrice) && Number.isFinite(changePercent)
          ? `今日 ${this.signedPrice(changePrice)}（${this.signed(changePercent)}%）`
          : "今日 --（--%）";
      }
      if (statusNode) statusNode.textContent = `M15 · 实时 · ${this.marketCandleTime(last.open_time)}`;
      if (footer) footer.textContent = `O ${this.price(last.open)}　H ${this.price(last.high)}　L ${this.price(last.low)}　C ${this.price(last.close)}`;
      changed.push(symbol);
    });
    if (changed.length && !this.marketRedrawFrame) {
      this.marketRedrawFrame = window.requestAnimationFrame(() => {
        this.marketRedrawFrame = null;
        changed.forEach((symbol) => this.redrawMarketChart(symbol));
      });
    }
  }

  updateMarketChartSummary(symbols = this.marketStreamSymbols) {
    if (!symbols.length) return;
    const ready = symbols.filter((symbol) => this.marketChartData.get(symbol)?.candles?.length).length;
    const labels = {
      live: "实时行情已连接",
      connecting: "正在连接实时行情",
      reconnecting: "实时行情重连中",
      idle: "历史 K 线",
    };
    const summary = this.q("#paper-market-summary");
    summary.textContent = `${symbols.length} 个交易品种 · ${ready} 个已加载 · ${labels[this.marketStreamStatus] || labels.idle}`;
    summary.dataset.streamStatus = this.marketStreamStatus;
  }

  marketCandleTime(timestamp) {
    const numeric = Number(timestamp);
    if (!Number.isFinite(numeric)) return "--";
    const date = new Date(numeric > 10_000_000_000 ? numeric : numeric * 1000);
    return date.toLocaleString("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false });
  }

  renderMetric(name, value, subtitle, tone) {
    const card = this.q(`[data-metric="${name}"]`);
    const valueNode = card.querySelector("strong");
    valueNode.textContent = value;
    valueNode.className = tone;
    card.querySelector("small").textContent = subtitle;
  }

  renderPositions(positions) {
    this.q("#paper-pos-count").textContent = `${positions.length} 个`;
    if (!positions.length) {
      this.q("#paper-positions").innerHTML = '<div class="empty-state"><strong>暂无持仓</strong><span>等待所选策略全部给出同一方向信号后自动开仓</span></div>';
      return;
    }
    const rows = positions.map((position) => {
      const sideClass = Number(position.side) > 0 ? "positive" : "negative";
      const reason = this.escape((position.reasons || [])[0] || "--");
      const added = Number(position.adds) ? `<small class="tag">+${this.number(position.adds, 0)} 加</small>` : "";
      const partial = position.tp_done ? '<small class="tag profit-tag">已止盈</small>' : "";
      const liquidationClass = position.liq_dist != null && Number(position.liq_dist) < 3 ? "negative" : "muted";
      const riskClass = position.risk_policy_compliant ? "muted" : "negative";
      const riskText = position.risk_at_stop != null
        ? `止损风险 ${this.number(position.risk_at_stop)} U · ${this.number(position.risk_pct, 2)}%`
        : "止损风险 --";
      return `<tr>
        <td><strong>${this.escape(this.symbol(position.symbol))}</strong><div class="symbol-tags">${added}${partial}</div></td>
        <td class="${sideClass}">${Number(position.side) > 0 ? "多" : "空"} ${this.number(position.leverage, 0)}x</td>
        <td>${this.number(position.qty, 4)}</td>
        <td>${this.price(position.avg_entry)}</td>
        <td>${this.price(position.price)}</td>
        <td><strong>${this.number(position.margin)}</strong><small class="${riskClass}">${riskText}</small></td>
        <td class="${this.tone(position.upnl)}"><strong>${this.signed(position.upnl)}</strong><small>${this.signed(position.pnl_pct, 1)}%</small></td>
        <td class="muted"><small>止 ${this.price(position.stop)}</small><small>目 ${Number(position.target) ? this.price(position.target) : "--"}</small></td>
        <td class="${liquidationClass}"><small>${position.liq_price ? this.price(position.liq_price) : "--"}</small><small>${position.liq_dist != null ? `距 ${this.number(position.liq_dist)}%` : ""}</small></td>
        <td class="muted">${this.number(position.hold_h, 1)}h</td>
        <td class="basis"><small>评分 ${this.signed(position.open_score, 0)}</small><span title="${reason}">${reason}</span></td>
      </tr>`;
    }).join("");
    this.q("#paper-positions").innerHTML = `<table class="positions-table">
      <thead><tr><th>合约</th><th>方向</th><th>数量</th><th>均价</th><th>现价</th><th>保证金</th><th>浮盈</th><th>止损/目标</th><th>强平价</th><th>持仓</th><th>开仓依据</th></tr></thead>
      <tbody>${rows}</tbody>
    </table>`;
  }

  renderTrades(trades) {
    this.q("#paper-trade-count").textContent = `${trades.length} 笔`;
    if (!trades.length) {
      this.q("#paper-trades").innerHTML = '<div class="empty-state"><strong>暂无成交记录</strong><span>完成平仓后，交易结果会显示在这里</span></div>';
      return;
    }
    const rows = trades.map((trade) => {
      const netPnl = Number(trade.pnl || 0) - Number(trade.fee || 0);
      const sideClass = Number(trade.side) > 0 ? "positive" : "negative";
      return `<tr>
        <td class="muted"><small>${this.escape(this.time(trade.closed_ts))}</small></td>
        <td><strong>${this.escape(this.symbol(trade.symbol))}</strong></td>
        <td class="${sideClass}">${Number(trade.side) > 0 ? "多" : "空"}</td>
        <td class="muted"><small>${this.price(trade.entry_price)} → ${this.price(trade.exit_price)}</small></td>
        <td class="${this.tone(netPnl)}"><strong>${this.signed(netPnl)} U</strong></td>
        <td class="muted">${this.escape(trade.reason || "--")}</td>
      </tr>`;
    }).join("");
    this.q("#paper-trades").innerHTML = `<table class="trades-table">
      <thead><tr><th>时间</th><th>合约</th><th>方向</th><th>开 → 平</th><th>盈亏</th><th>平仓原因</th></tr></thead>
      <tbody>${rows}</tbody>
    </table>`;
  }

  drawCurve(rawCurve, rawStart) {
    const canvas = this.q("#paper-chart");
    const width = Math.floor(canvas.clientWidth);
    const height = Math.floor(canvas.clientHeight);
    if (!width || !height) return;
    const pixelRatio = Math.min(window.devicePixelRatio || 1, 2);
    canvas.width = Math.floor(width * pixelRatio);
    canvas.height = Math.floor(height * pixelRatio);
    const context = canvas.getContext("2d");
    context.setTransform(pixelRatio, 0, 0, pixelRatio, 0, 0);
    context.clearRect(0, 0, width, height);

    const curve = (Array.isArray(rawCurve) ? rawCurve : [])
      .map((point) => [Number(point?.[0]), Number(point?.[1])])
      .filter((point) => Number.isFinite(point[0]) && Number.isFinite(point[1]));
    const start = Number(rawStart) || 10000;
    const padding = { left: 16, right: width < 560 ? 52 : 70, top: 24, bottom: 34 };
    const plotWidth = width - padding.left - padding.right;
    const plotHeight = height - padding.top - padding.bottom;

    if (!curve.length) {
      context.fillStyle = "#718096";
      context.font = "20.8px system-ui, sans-serif";
      context.fillText("权益数据积累中（每分钟记录一次）…", padding.left + 6, padding.top + 20);
      return;
    }

    const values = curve.map((point) => point[1]).concat(start);
    const high = Math.max(...values);
    const low = Math.min(...values);
    const range = high - low || Math.max(high * 0.01, 1);
    const chartHigh = high + range * 0.08;
    const chartLow = low - range * 0.08;
    const chartRange = chartHigh - chartLow || 1;
    const x = (index) => padding.left + index * plotWidth / Math.max(curve.length - 1, 1);
    const y = (value) => padding.top + (chartHigh - value) / chartRange * plotHeight;

    context.font = "17.6px system-ui, sans-serif";
    context.lineWidth = 1;
    for (let index = 0; index <= 4; index += 1) {
      const value = chartHigh - chartRange * index / 4;
      const yValue = y(value);
      context.strokeStyle = "#202936";
      context.beginPath();
      context.moveTo(padding.left, yValue);
      context.lineTo(width - padding.right, yValue);
      context.stroke();
      context.fillStyle = "#718096";
      context.fillText(this.number(value, 0), width - padding.right + 8, yValue + 4);
    }

    context.strokeStyle = "#f0b90b";
    context.setLineDash([6, 6]);
    context.beginPath();
    context.moveTo(padding.left, y(start));
    context.lineTo(width - padding.right, y(start));
    context.stroke();
    context.setLineDash([]);
    context.fillStyle = "#f0b90b";
    context.font = "600 17.6px system-ui, sans-serif";
    context.fillText(`本金 ${this.number(start, 0)}`, padding.left + 6, Math.max(13, y(start) - 7));

    const lastValue = curve[curve.length - 1][1];
    const lineColor = lastValue >= start ? "#2bd7a3" : "#f6465d";
    const gradient = context.createLinearGradient(0, padding.top, 0, height - padding.bottom);
    gradient.addColorStop(0, lastValue >= start ? "rgba(43, 215, 163, .28)" : "rgba(246, 70, 93, .26)");
    gradient.addColorStop(1, lastValue >= start ? "rgba(43, 215, 163, .01)" : "rgba(246, 70, 93, .02)");
    context.beginPath();
    curve.forEach((point, index) => {
      if (index) context.lineTo(x(index), y(point[1]));
      else context.moveTo(x(index), y(point[1]));
    });
    context.lineTo(x(curve.length - 1), height - padding.bottom);
    context.lineTo(x(0), height - padding.bottom);
    context.closePath();
    context.fillStyle = gradient;
    context.fill();

    context.beginPath();
    curve.forEach((point, index) => {
      if (index) context.lineTo(x(index), y(point[1]));
      else context.moveTo(x(index), y(point[1]));
    });
    context.strokeStyle = lineColor;
    context.lineWidth = 2.2;
    context.lineJoin = "round";
    context.stroke();

    context.fillStyle = "#718096";
    context.font = "17.6px system-ui, sans-serif";
    const labels = [0, Math.floor((curve.length - 1) / 2), curve.length - 1];
    labels.forEach((index, labelIndex) => {
      const label = this.shortTime(curve[index][0]);
      const measured = context.measureText(label).width;
      let labelX = x(index) - measured / 2;
      if (labelIndex === 0) labelX = padding.left;
      if (labelIndex === labels.length - 1) labelX = width - padding.right - measured;
      context.fillText(label, labelX, height - 9);
    });
  }

  setConnectionState(label, state) {
    const target = this.q("#paper-state");
    target.className = `running-state ${state}`;
    target.lastChild.textContent = label;
  }

  showBanner(message, kind = "") {
    const banner = this.q("#paper-banner");
    banner.textContent = message;
    banner.className = message ? `paper-banner ${kind}`.trim() : "paper-banner hidden";
  }

  tone(value) {
    const number = Number(value);
    if (!Number.isFinite(number) || number === 0) return "neutral";
    return number > 0 ? "positive" : "negative";
  }

  number(value, digits = 2) {
    const number = Number(value);
    if (!Number.isFinite(number)) return "--";
    return number.toLocaleString("zh-CN", { minimumFractionDigits: digits, maximumFractionDigits: digits });
  }

  signed(value, digits = 2) {
    const number = Number(value);
    if (!Number.isFinite(number)) return "--";
    return `${number >= 0 ? "+" : ""}${this.number(number, digits)}`;
  }

  price(value) {
    const number = Number(value);
    if (!Number.isFinite(number)) return "--";
    const digits = Math.abs(number) >= 100 ? 2 : Math.abs(number) >= 1 ? 4 : 6;
    return this.number(number, digits);
  }

  signedPrice(value) {
    const number = Number(value);
    if (!Number.isFinite(number)) return "--";
    return `${number >= 0 ? "+" : "-"}${this.price(Math.abs(number))}`;
  }

  symbol(value) {
    return String(value || "--").replace(/(USDT|USD1)$/i, "");
  }

  time(timestamp) {
    const value = Number(timestamp);
    if (!Number.isFinite(value)) return "--";
    return new Date(value * 1000).toLocaleString("zh-CN", { hour12: false });
  }

  shortTime(timestamp) {
    const date = new Date(Number(timestamp) * 1000);
    if (Number.isNaN(date.getTime())) return "--";
    const month = date.getMonth() + 1;
    const day = date.getDate();
    const hour = String(date.getHours()).padStart(2, "0");
    const minute = String(date.getMinutes()).padStart(2, "0");
    return `${month}/${day} ${hour}:${minute}`;
  }

  escape(value) {
    return String(value ?? "").replace(/[&<>'"]/g, (character) => ({
      "&": "&amp;",
      "<": "&lt;",
      ">": "&gt;",
      "'": "&#39;",
      '"': "&quot;",
    })[character]);
  }
}

window.quantdeskRegisterPageController("paper-dashboard", PaperDashboard);
