class PaperDashboard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this.running = false;
    this.loading = false;
    this.timer = null;
    this.resizeObserver = null;
    this.data = null;
    this.accounts = [];
    this.selectedAccountId = null;
    this.loadSequence = 0;
    this.strategyCatalog = [];
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
      });
      this.resizeObserver.observe(this.q("#chart-wrap"));
    }
  }

  disconnectedCallback() {
    this.pause();
    if (this.resizeObserver) this.resizeObserver.disconnect();
    this.resizeObserver = null;
  }

  renderShell() {
    this.shadowRoot.innerHTML = `
      <link rel="stylesheet" href="/assets/paper.css?v=20260804-5">
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
                <p>绑定一个策略，资金、持仓、成交和权益将独立运行。</p>
              </div>
              <button class="modal-close" type="button" data-modal-close aria-label="关闭">×</button>
            </header>
            <form id="paper-create-form" class="create-form">
              <label class="form-field form-field-wide">
                <span>绑定策略</span>
                <select id="paper-create-strategy" required></select>
                <small id="paper-create-strategy-note">每个模拟盘保存创建时的策略快照。</small>
              </label>
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
                <input id="paper-create-leverage" type="number" min="1" max="50" step="1" value="20" required>
              </label>
              <p id="paper-create-error" class="form-error hidden" role="alert"></p>
              <footer class="modal-actions">
                <button class="action-button" type="button" data-modal-close>取消</button>
                <button id="paper-create-submit" class="create-account-button" type="submit">创建并运行</button>
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
    this.q("#paper-delete").addEventListener("click", () => this.deleteAccount());
    this.q("#paper-toggle-status").addEventListener("click", () => this.toggleAccountStatus());
    this.q("#paper-create").addEventListener("click", () => this.openCreateDialog());
    this.q("#paper-account-tabs").addEventListener("click", (event) => {
      const tab = event.target.closest("[data-account-id]");
      if (tab) this.selectAccount(tab.dataset.accountId);
    });
    this.q("#paper-account-tabs").addEventListener("keydown", (event) => this.handleTabKeydown(event));
    this.q("#paper-create-form").addEventListener("submit", (event) => this.createAccount(event));
    this.q("#paper-create-strategy").addEventListener("change", () => this.applyStrategyDefaults());
    this.shadowRoot.querySelectorAll("[data-modal-close]").forEach((button) => {
      button.addEventListener("click", () => this.closeCreateDialog());
    });
    this.shadowRoot.addEventListener("keydown", (event) => {
      if (event.key === "Escape" && !this.q("#paper-create-modal").classList.contains("hidden")) {
        this.closeCreateDialog();
      }
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
      if (!this.strategyCatalog.length) {
        const catalog = await window.quantdeskApi("/api/v2/strategies");
        this.strategyCatalog = (catalog.items || []).filter((item) => item.status === "active");
      }
      if (!this.strategyCatalog.length) throw new Error("请先在策略中心创建或启用策略");
      const strategySelect = this.q("#paper-create-strategy");
      strategySelect.innerHTML = this.strategyCatalog.map((strategy) => (
        `<option value="${this.escape(strategy.id)}">${this.escape(strategy.name)} · ${this.escape(strategy.category || strategy.engine_key || "策略")}</option>`
      )).join("");
      this.q("#paper-create-balance").value = "10000";
      this.applyStrategyDefaults();
      this.showCreateError("");
      const modal = this.q("#paper-create-modal");
      modal.classList.remove("hidden");
      modal.setAttribute("aria-hidden", "false");
      window.requestAnimationFrame(() => strategySelect.focus());
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
    const strategyId = this.q("#paper-create-strategy").value;
    const initialBalance = Number(this.q("#paper-create-balance").value);
    const leverage = Number(this.q("#paper-create-leverage").value);
    if (!Number.isFinite(initialBalance) || initialBalance <= 0 || initialBalance > 1_000_000_000) {
      this.showCreateError("初始资金必须大于 0，且不能超过 1,000,000,000 USDT。");
      return;
    }
    if (!Number.isInteger(leverage) || leverage < 1 || leverage > 50) {
      this.showCreateError("杠杆倍数必须是 1 到 50 之间的整数。");
      return;
    }
    submit.disabled = true;
    submit.textContent = "创建中…";
    this.showCreateError("");
    try {
      const created = await this.api("/accounts", {
        method: "POST",
        body: JSON.stringify({ name, strategy_id: strategyId, initial_balance: initialBalance, leverage }),
      });
      this.closeCreateDialog();
      await this.loadAccounts(created.id);
      await this.load();
      this.showBanner("模拟盘已创建，每个账户将独立运行绑定策略。", "success");
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
    const strategyId = this.q("#paper-create-strategy").value;
    const strategy = this.strategyCatalog.find((item) => item.id === strategyId);
    if (!strategy) return;
    this.q("#paper-create-name").value = `${strategy.name} 模拟盘`;
    this.q("#paper-create-leverage").value = String(strategy.risk_defaults?.leverage || 20);
    this.q("#paper-create-strategy-note").textContent = `${strategy.description || "创建后使用独立策略快照运行。"}`;
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
    const statusButton = this.q("#paper-toggle-status");
    statusButton.disabled = false;
    statusButton.textContent = accountMeta.status === "paused" ? "继续运行" : "暂停运行";
    statusButton.classList.toggle("resume-button", accountMeta.status === "paused");

    this.renderMetric("equity", `${this.number(account.equity)} U`, `收益率 ${this.signed(account.ret_pct)}%`, this.tone(account.ret_pct));
    this.renderMetric("balance", `${this.number(account.balance)} U`, `占用保证金 ${this.number(account.used_margin)} U（${this.number(account.margin_usage, 1)}%）`, "neutral");
    this.renderMetric("upnl", `${this.signed(account.upnl)} U`, `今日盈亏 ${this.signed(account.today_pnl)} U`, this.tone(account.upnl));
    this.renderMetric("realized", `${this.signed(stats.realized)} U`, `共 ${this.number(stats.trades, 0)} 笔（${this.number(stats.wins, 0)}胜/${this.number(stats.losses, 0)}负）`, this.tone(stats.realized));
    this.renderMetric("win-rate", Number(stats.trades) ? `${this.number(stats.win_rate, 1)}%` : "--", `盈亏比 ${stats.profit_factor ?? "--"}`, "warning");
    const riskSummary = account.risk_per_trade_pct != null
      ? ` · 单笔风险 ${this.number(account.risk_per_trade_pct, 2)}%`
      : "";
    this.renderMetric("drawdown", `${this.number(stats.max_drawdown)}%`, `仓位 ${positions.length}/${this.number(account.max_positions, 0)}${riskSummary}`, Number(stats.max_drawdown) > 10 ? "negative" : "warning");

    this.renderPositions(positions);
    this.renderTrades(trades);
    this.q("#paper-disclaimer").textContent = `风险提示：${data.disclaimer || "模拟交易仅用于策略验证与学习，不构成投资建议。"}${rules.costs ? ` 成本模型：${rules.costs}` : ""}`;
    window.requestAnimationFrame(() => this.drawCurve(data.curve || [], account.start || 10000));
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
      this.q("#paper-positions").innerHTML = '<div class="empty-state"><strong>暂无持仓</strong><span>等待 4h 信号评分达到策略阈值后自动开仓</span></div>';
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
      context.font = "13px system-ui, sans-serif";
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

    context.font = "11px system-ui, sans-serif";
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
    context.font = "600 11px system-ui, sans-serif";
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
    context.font = "11px system-ui, sans-serif";
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

if (!customElements.get("paper-dashboard")) customElements.define("paper-dashboard", PaperDashboard);
