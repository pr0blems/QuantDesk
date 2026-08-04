class LiveDashboard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this.accounts = [];
    this.selectedAccountId = null;
    this.catalog = [];
    this.running = false;
    this.loading = false;
    this.timer = null;
    this.resizeObserver = null;
    this.data = null;
    this.systemEnabled = false;
    this.universeCount = 0;
    this.performance = null;
    this.performanceLoadedAt = 0;
    this.editingAccountId = null;
    this.renderShell();
    this.bindEvents();
  }

  connectedCallback() {
    if ("ResizeObserver" in window && !this.resizeObserver) {
      this.resizeObserver = new ResizeObserver(() => {
        if (this.data) this.drawCurve(this.data.curve || [], this.data.curveStart || 0);
      });
      this.resizeObserver.observe(this.q("#live-chart-wrap"));
    }
  }

  disconnectedCallback() {
    this.pause();
    this.resizeObserver?.disconnect();
    this.resizeObserver = null;
  }

  renderShell() {
    this.shadowRoot.innerHTML = `
      <link rel="stylesheet" href="/assets/paper.css?v=20260804-3">
      <link rel="stylesheet" href="/assets/live.css?v=20260804-3">
      <main class="paper-dashboard live-dashboard">
        <nav class="account-switcher" aria-label="实盘策略切换">
          <div id="live-tabs" class="account-tabs" role="tablist"><span class="tabs-loading">正在读取实盘策略…</span></div>
          <button id="live-create" class="create-account-button live-create" type="button"><span class="create-icon">＋</span><span class="create-label">新增实盘策略</span></button>
        </nav>

        <div id="live-content" role="tabpanel">
          <header class="paper-head">
            <div class="paper-title-group">
              <div class="title-row"><span class="title-icon live-icon">↗</span><h1>AI 自动实盘交易</h1><span class="simulation-badge live-badge">REAL FUNDS</span></div>
              <p id="live-subtitle">Binance USD-M 实盘 · 与模拟盘相同的 TradFi 品种池</p>
              <p id="live-rules" class="paper-rules">正在加载策略规则…</p>
            </div>
            <div class="paper-actions">
              <span id="live-state" class="running-state loading"><i></i><span>连接中</span></span>
              <span id="live-updated" class="updated-time">--:--:--</span>
              <button id="live-refresh" class="action-button" type="button">刷新</button>
              <button id="live-toggle" class="action-button status-button" type="button" disabled>启用实盘</button>
              <button id="live-adjust" class="action-button manage-button" type="button" disabled>调整策略</button>
              <button id="live-rename" class="action-button manage-button" type="button" disabled>修改名称</button>
              <button id="live-delete" class="action-button delete-button" type="button" disabled>删除</button>
            </div>
          </header>

          <div id="live-banner" class="paper-banner hidden" role="status" aria-live="polite"></div>
          <div class="live-risk"><strong>真实资金风险</strong><span>暂停只阻止新的策略动作，不会自动平掉 Binance 已有仓位，也不会撤销人工订单。</span></div>

          <section class="paper-cards" aria-label="实盘账户概览">
            <article class="paper-card" data-metric="equity"><span>账户权益</span><strong>--</strong><small>收益率 --</small></article>
            <article class="paper-card" data-metric="balance"><span>可用余额</span><strong>--</strong><small>占用保证金 --</small></article>
            <article class="paper-card" data-metric="upnl"><span>浮动盈亏</span><strong>--</strong><small>今日盈亏 --</small></article>
            <article class="paper-card" data-metric="realized"><span>已实现盈亏</span><strong>--</strong><small>Binance 月度流水</small></article>
            <article class="paper-card" data-metric="win-rate"><span>胜率</span><strong>--</strong><small>盈亏比 --</small></article>
            <article class="paper-card" data-metric="drawdown"><span>最大回撤</span><strong>--</strong><small>仓位 --</small></article>
          </section>

          <section class="paper-panel equity-panel">
            <div class="panel-title"><span><i class="chart-mark"></i>账户权益曲线</span><span id="curve-summary" class="panel-meta">每次刷新记录实盘快照</span></div>
            <div id="live-chart-wrap" class="chart-wrap"><canvas id="live-chart" aria-label="实盘账户权益曲线">当前浏览器不支持绘制权益曲线。</canvas></div>
          </section>

          <div class="paper-columns">
            <section class="paper-panel table-panel">
              <div class="panel-title"><span>当前持仓 <em id="live-pos-count">0 个</em></span><span class="panel-meta">Binance 实时持仓</span></div>
              <div id="live-positions" class="paper-table"><div class="empty-state">正在读取持仓…</div></div>
            </section>
            <section class="paper-panel table-panel">
              <div class="panel-title"><span>历史成交 <em id="live-trade-count">0 笔</em></span><span class="panel-meta">策略平仓记录 · 最近 50 笔</span></div>
              <div id="live-trades" class="paper-table"><div class="empty-state">正在读取成交…</div></div>
            </section>
          </div>

          <details class="live-details">
            <summary>实盘订单明细 <span id="live-detail-count">0 条</span></summary>
            <div class="live-order-grid">
              <section class="paper-panel table-panel"><div class="panel-title"><span>Binance 当前挂单 <em id="live-order-count">0 个</em></span></div><div id="live-orders" class="paper-table compact-table"></div></section>
              <section class="paper-panel table-panel"><div class="panel-title"><span>策略订单审计 <em id="live-intent-count">0 条</em></span></div><div id="live-intents" class="paper-table compact-table"></div></section>
            </div>
          </details>

          <footer class="paper-disclaimer">⚠ 实盘交易使用真实资金。系统仅管理带 QuantDesk 客户端订单号的策略订单；API 密钥只在服务端使用，页面不会接收或回显密钥。</footer>
        </div>

        <div id="live-empty" class="paper-panel empty-state hidden"><strong>还没有实盘策略</strong><span>先发布策略，再创建一个默认暂停的实盘部署。</span></div>

        <div id="live-modal" class="paper-modal hidden" aria-hidden="true">
          <button class="modal-backdrop" type="button" data-close aria-label="关闭实盘策略窗口"></button>
          <section class="modal-card live-modal-card" role="dialog" aria-modal="true" aria-labelledby="live-modal-title">
            <header class="modal-head"><div><span id="live-modal-kicker" class="modal-kicker live-kicker">NEW LIVE DEPLOYMENT</span><h2 id="live-modal-title">新增实盘策略</h2><p id="live-modal-description">交易品种和模拟盘一致；创建后保持暂停，确认启用后才会通过 Binance API 真实下单。</p></div><button class="modal-close" type="button" data-close aria-label="关闭">×</button></header>
            <form id="live-form" class="create-form live-form">
              <label class="form-field form-field-wide"><span>已发布策略</span><select id="live-strategy" required></select><small>保存为独立策略快照。</small></label>
              <label id="live-name-field" class="form-field form-field-wide"><span>部署名称</span><input id="live-name" maxlength="100" required></label>
              <div class="live-universe form-field-wide"><strong>交易品种</strong><span>与模拟盘完全相同的 Binance TradFi 股票及传统资产合约池（当前 <b id="live-universe-count">--</b> 个）</span></div>
              <label class="form-field"><span>请求杠杆（1–20x）</span><input id="live-leverage" type="number" min="1" max="20" step="1" value="10" required></label>
              <label class="form-field"><span>最大持仓数（1–20）</span><input id="live-max-positions" type="number" min="1" max="20" step="1" value="1" required></label>
              <label class="form-field"><span>单笔保证金上限（≤10%）</span><input id="live-size" type="number" min="0.1" max="10" step="0.1" value="2" required></label>
              <label class="form-field"><span>总保证金上限（%权益）</span><input id="live-cap" type="number" min="1" max="50" step="1" value="20" required></label>
              <label class="form-field"><span>单笔止损风险（%权益）</span><input id="live-risk-per-trade" type="number" min="0.05" max="1" step="0.05" value="0.5" required></label>
              <label class="form-field"><span>组合开放风险上限（%权益）</span><input id="live-total-risk" type="number" min="0.25" max="8" step="0.25" value="4" required></label>
              <label class="form-field"><span>风控杠杆上限（1–20x）</span><input id="live-risk-leverage" type="number" min="1" max="20" step="1" value="10" required></label>
              <label class="form-field"><span>止损距强平最小缓冲（%）</span><input id="live-liq-buffer" type="number" min="0.5" max="10" step="0.1" value="1.5" required></label>
              <label class="form-field"><span>同风险组最多持仓</span><input id="live-cluster-cap" type="number" min="1" max="20" step="1" value="2" required></label>
              <label class="form-field"><span>当日亏损熔断（%）</span><input id="live-daily-loss" type="number" min="0.25" max="20" step="0.25" value="2" required></label>
              <label class="form-field"><span>最大回撤熔断（%）</span><input id="live-max-drawdown" type="number" min="1" max="30" step="0.5" value="6" required></label>
              <label class="form-field"><span>空头风险系数（0–1）</span><input id="live-short-risk" type="number" min="0" max="1" step="0.1" value="0.5" required></label>
              <label class="form-field"><span>行情最长延迟（秒）</span><input id="live-ticker-age" type="number" min="30" max="900" step="10" value="120" required></label>
              <label class="form-field"><span>信号最长年龄（分钟）</span><input id="live-signal-age" type="number" min="5" max="2880" step="5" value="300" required></label>
              <label class="live-check form-field-wide"><input id="live-block-high-risk" type="checkbox" checked><span>禁止杠杆/反向 ETF、波动率和未正式上市参考产品</span></label>
              <label class="live-check form-field-wide"><input id="live-ack" type="checkbox" required><span id="live-ack-text">我理解这是实盘部署；创建后仍保持暂停，只有再次确认才会开始真实交易。</span></label>
              <p id="live-form-error" class="form-error hidden" role="alert"></p>
              <footer class="modal-actions"><button class="action-button" type="button" data-close>取消</button><button id="live-submit" class="create-account-button live-create" type="submit">创建为暂停</button></footer>
            </form>
          </section>
        </div>
      </main>`;
  }

  q(selector) { return this.shadowRoot.querySelector(selector); }
  async api(path = "", options = {}) {
    if (typeof window.quantdeskApi !== "function") throw new Error("认证服务尚未就绪");
    return window.quantdeskApi(`/api/v2/live${path}`, options);
  }

  bindEvents() {
    this.q("#live-refresh").addEventListener("click", () => this.load(true));
    this.q("#live-create").addEventListener("click", () => this.openCreate());
    this.q("#live-form").addEventListener("submit", (event) => this.create(event));
    this.q("#live-strategy").addEventListener("change", () => this.applyStrategy());
    this.q("#live-toggle").addEventListener("click", () => this.toggle());
    this.q("#live-adjust").addEventListener("click", () => this.openAdjust());
    this.q("#live-rename").addEventListener("click", () => this.renameDeployment());
    this.q("#live-delete").addEventListener("click", () => this.deleteDeployment());
    this.q("#live-tabs").addEventListener("click", (event) => { const tab = event.target.closest("[data-id]"); if (tab) this.select(tab.dataset.id); });
    this.shadowRoot.querySelectorAll("[data-close]").forEach((button) => button.addEventListener("click", () => this.closeCreate()));
    this.shadowRoot.addEventListener("keydown", (event) => { if (event.key === "Escape") this.closeCreate(); });
  }

  start() {
    if (this.running) return;
    this.running = true;
    this.loadAccounts().then(() => this.load()).catch((error) => this.banner(`实盘页面加载失败：${error.message}`, "error"));
    // Binance's all-symbol normal/algo order endpoints are high-weight. The
    // backend also runs the execution/reconciliation loop, so a dashboard poll
    // every 30 seconds is responsive without multiplying request weight per tab.
    this.timer = window.setInterval(() => this.load(), 30000);
  }

  pause() { this.running = false; if (this.timer) window.clearInterval(this.timer); this.timer = null; }

  async loadAccounts(preferred = "") {
    const payload = await this.api("/accounts");
    this.accounts = Array.isArray(payload.items) ? payload.items : [];
    this.systemEnabled = Boolean(payload.system_enabled);
    this.universeCount = Number(payload.universe?.count || 0);
    this.q("#live-universe-count").textContent = String(this.universeCount || "--");
    const remembered = preferred || this.remembered();
    this.selectedAccountId = this.accounts.some((item) => item.id === remembered) ? remembered : this.accounts[0]?.id || null;
    this.renderTabs();
    this.q("#live-content").classList.toggle("hidden", !this.selectedAccountId);
    this.q("#live-empty").classList.toggle("hidden", Boolean(this.selectedAccountId));
    if (!this.systemEnabled) this.banner("服务端实盘总开关当前关闭；页面可安全查看并创建暂停部署。", "");
    if (!payload.credentials_configured) this.banner("尚未配置 Binance READ + TRADE API 凭据。", "error");
  }

  renderTabs() {
    const root = this.q("#live-tabs");
    if (!this.accounts.length) { root.innerHTML = '<span class="tabs-empty">暂无实盘策略</span>'; return; }
    root.innerHTML = this.accounts.map((item) => `<button class="account-tab ${item.status === "paused" ? "paused" : "active"}${item.id === this.selectedAccountId ? " selected" : ""}" type="button" role="tab" aria-selected="${item.id === this.selectedAccountId}" data-id="${this.escape(item.id)}"><i></i><span>${this.escape(item.name)}</span>${item.status !== "active" ? `<small>${this.statusLabel(item.status)}</small>` : ""}</button>`).join("");
  }

  async select(id) { this.selectedAccountId = id; try { localStorage.setItem("quantdesk.live.selected-account", id); } catch (_) {} this.renderTabs(); await this.load(true); }
  remembered() { try { return localStorage.getItem("quantdesk.live.selected-account") || ""; } catch (_) { return ""; } }

  async load(forcePerformance = false) {
    if (!this.selectedAccountId || this.loading) return;
    this.loading = true;
    const button = this.q("#live-refresh"); button.disabled = true; button.textContent = "刷新中";
    try {
      const offset = -new Date().getTimezoneOffset();
      const query = new URLSearchParams({ account_id: this.selectedAccountId, timezone_offset_minutes: String(offset) });
      const data = await this.api(`?${query}`);
      if (forcePerformance || Date.now() - this.performanceLoadedAt > 60000) {
        this.performanceLoadedAt = Date.now();
        try { this.performance = await window.quantdeskApi(`/api/v2/dashboard/binance-performance?timezone_offset_minutes=${offset}`); } catch (_) { this.performance = null; }
      }
      this.data = this.prepareData(data);
      this.renderData(this.data);
      this.q("#live-updated").textContent = new Date().toLocaleTimeString("zh-CN", { hour12: false });
    } catch (error) { this.state("连接异常", "error"); this.banner(`实盘数据加载失败：${error.message}`, "error"); }
    finally { this.loading = false; button.disabled = false; button.textContent = "刷新"; }
  }

  prepareData(data) {
    const account = data.live_account || {};
    const binance = data.binance || {};
    const positions = Array.isArray(data.positions) ? data.positions : [];
    const orders = Array.isArray(data.open_orders) ? data.open_orders : [];
    const intents = Array.isArray(data.order_intents) ? data.order_intents : [];
    const equity = Number(binance.wallet_balance || 0) + Number(binance.unrealized_pnl || 0);
    const usedMargin = positions.reduce((sum, item) => {
      const leverage = Number(item.leverage || 0);
      const fallback = leverage > 0 ? Number(item.notional || 0) / leverage : 0;
      return sum + Number(item.initial_margin ?? fallback);
    }, 0);
    const curve = binance.connected ? this.recordEquity(account.id, equity) : this.readCurve(account.id);
    const curveStart = curve[0]?.[1] ?? equity;
    const maxDrawdown = this.maxDrawdown(curve);
    const performance = this.performanceSummary();
    return { ...data, account, binance, positions, orders, intents, equity, usedMargin, curve, curveStart, maxDrawdown, performance, trades: this.tradeHistory(intents) };
  }

  performanceSummary() {
    const assets = Array.isArray(this.performance?.assets) ? this.performance.assets : [];
    const summary = { realized: 0, net: 0, trades: 0, wins: 0, losses: 0, grossProfit: 0, grossLoss: 0, today: 0 };
    const today = new Date().toLocaleDateString("en-CA");
    assets.forEach((asset) => {
      summary.realized += Number(asset.realized_pnl || 0); summary.net += Number(asset.net_income || 0);
      summary.trades += Number(asset.realized_records || 0); summary.wins += Number(asset.wins || 0); summary.losses += Number(asset.losses || 0);
      summary.grossProfit += Number(asset.gross_profit || 0); summary.grossLoss += Number(asset.gross_loss_abs || 0);
      (asset.days || []).filter((day) => day.date === today).forEach((day) => { summary.today += Number(day.net_income || 0); });
    });
    summary.winRate = summary.wins + summary.losses ? summary.wins * 100 / (summary.wins + summary.losses) : null;
    summary.profitFactor = summary.grossLoss ? summary.grossProfit / summary.grossLoss : null;
    return summary;
  }

  recordEquity(accountId, equity) {
    const curve = this.readCurve(accountId); const ts = Math.floor(Date.now() / 60000) * 60;
    if (curve.at(-1)?.[0] === ts) curve[curve.length - 1] = [ts, equity]; else curve.push([ts, equity]);
    const trimmed = curve.slice(-2880);
    try { localStorage.setItem(`quantdesk.live.curve.${accountId}`, JSON.stringify(trimmed)); } catch (_) {}
    return trimmed;
  }

  readCurve(accountId) { try { const value = JSON.parse(localStorage.getItem(`quantdesk.live.curve.${accountId}`) || "[]"); return Array.isArray(value) ? value : []; } catch (_) { return []; } }
  maxDrawdown(curve) { let peak = 0; let worst = 0; curve.forEach(([, value]) => { peak = Math.max(peak, value); if (peak > 0) worst = Math.max(worst, (peak - value) * 100 / peak); }); return worst; }

  tradeHistory(intents) {
    const openByKey = new Map(); const trades = [];
    [...intents].reverse().forEach((item) => {
      if (item.status !== "filled") return;
      const key = `${item.symbol}:${item.position_side || "BOTH"}`;
      if (item.action === "open") { openByKey.set(key, item); return; }
      if (item.action !== "close") return;
      const open = openByKey.get(key); if (!open) return;
      const entry = Number(open.response?.avgPrice || 0); const exit = Number(item.response?.avgPrice || 0);
      const qty = Number(item.response?.executedQty || item.quantity || open.quantity || 0);
      const side = (open.position_side === "SHORT" || open.side === "SELL") ? -1 : 1;
      trades.push({ symbol: item.symbol, side, entry, exit, pnl: entry && exit ? (exit - entry) * qty * side : null, time: item.submitted_at || item.created_at, reason: item.request?.reason || "策略平仓", entryBasis: open.entry_basis || item.entry_basis || {} });
      openByKey.delete(key);
    });
    return trades.reverse().slice(0, 50);
  }

  renderData(data) {
    const { account, binance, positions, orders, intents, performance } = data;
    this.accounts = this.accounts.map((item) => item.id === account.id ? account : item); this.renderTabs();
    const config = account.config || {}; const count = Number(config.universe_count || this.universeCount || 0);
    this.q("#live-subtitle").textContent = `${account.name || "实盘策略"} · ${account.strategy_name || "独立策略"} · ${count || "--"} 个 TradFi 品种 · Binance ${this.accountType(binance.account_type)}`;
    const mode = config.position_mode === "hedge" ? "双向持仓" : config.position_mode === "one_way" ? "单向持仓" : "自动识别持仓模式";
    this.q("#live-rules").textContent = `${config.leverage || "--"}x 请求 / ${config.risk_max_leverage || 10}x 风控上限 ｜ 单笔止损风险 ${config.risk_per_trade_pct || 0.5}% ｜ 组合风险 ${config.max_total_risk_pct || 4}% ｜ 总保证金 ${this.number(Number(config.margin_cap || 0.2) * 100, 0)}% ｜ 最大 ${config.max_positions || "--"} 仓 ｜ ${mode}`;
    const ret = data.curveStart ? (data.equity - data.curveStart) * 100 / data.curveStart : 0;
    this.metric("equity", binance.connected ? `${this.number(data.equity)} U` : "--", `曲线期收益率 ${this.signed(ret)}%`, this.tone(ret));
    this.metric("balance", binance.connected ? `${this.number(binance.available_balance)} U` : "--", `占用保证金 ${this.number(data.usedMargin)} U（${data.equity ? this.number(data.usedMargin * 100 / data.equity, 1) : "--"}%）`, "neutral");
    this.metric("upnl", binance.connected ? `${this.signed(binance.unrealized_pnl)} U` : "--", `今日净收益 ${this.signed(performance.today)} U`, this.tone(binance.unrealized_pnl));
    this.metric("realized", this.performance ? `${this.signed(performance.realized)} U` : "--", `本月 ${performance.trades} 条实现盈亏流水`, this.tone(performance.realized));
    this.metric("win-rate", performance.winRate == null ? "--" : `${this.number(performance.winRate, 1)}%`, `盈亏比 ${performance.profitFactor == null ? "--" : this.number(performance.profitFactor)}`, "warning");
    this.metric("drawdown", `${this.number(data.maxDrawdown)}%`, `仓位 ${positions.length}/${this.number(config.max_positions || 0, 0)}`, data.maxDrawdown > 10 ? "negative" : "warning");
    this.q("#curve-summary").textContent = `曲线起点 ${this.number(data.curveStart)} U · 最大回撤 ${this.number(data.maxDrawdown)}%`;
    this.renderPositions(positions, orders, account); this.renderTrades(data.trades); this.renderOrders(orders); this.renderIntents(intents);
    const toggle = this.q("#live-toggle"); toggle.disabled = false; toggle.textContent = account.status === "active" ? "暂停策略" : account.status === "error" ? "清除错误并暂停" : "确认启用实盘"; toggle.classList.toggle("resume-button", account.status !== "active");
    this.q("#live-delete").disabled = false;
    this.q("#live-rename").disabled = false;
    this.q("#live-adjust").disabled = account.status !== "paused";
    const rateLimited = binance.error_category === "rate_limit" || account.last_error_code === "rate_limit";
    const riskReview = account.last_error_code === "risk_review_required";
    const auditPending = account.last_error_code === "filled_audit_pending";
    this.state(
      binance.connected ? (riskReview ? "实盘待人工复核" : account.status === "active" ? "实盘运行中" : "Binance 已连接") : rateLimited ? "Binance 限频冷却" : "Binance 未连接",
      binance.connected ? (riskReview ? "paused" : account.status === "active" ? "success" : "paused") : rateLimited ? "paused" : "error",
    );
    if (account.status === "error") {
      this.banner(`部署已停止：${account.last_error_code || "internal_error"}。`, "error");
    } else if (riskReview) {
      this.banner("历史/人工仓位沿用原保护，新开仓暂停，需人工复核。", "warning");
    } else if (auditPending) {
      this.banner("交易所已确认成交；保护动作优先执行，本地成交审计正在自动恢复。恢复前暂停新开仓。", "warning");
    } else if (rateLimited) {
      const retryAt = binance.retry_at ? this.time(binance.retry_at) : "等待 Binance 放行";
      this.banner(`Binance IP 正在限频冷却（预计 ${retryAt}）。部署仍保持启用，但恢复前不会执行新的策略动作。`, "warning");
    } else if (account.last_error_code) {
      this.banner(`Binance 连接暂时降级：${account.last_error_code}。部署仍保持启用并自动重试。`, "warning");
    } else if (binance.connected && this.systemEnabled) this.banner("", "");
    requestAnimationFrame(() => this.drawCurve(data.curve, data.curveStart));
  }

  metric(name, value, subtitle, tone) { const card = this.q(`[data-metric="${name}"]`); card.querySelector("strong").textContent = value; card.querySelector("strong").className = tone; card.querySelector("small").textContent = subtitle; }

  renderPositions(items, orders, account) {
    this.q("#live-pos-count").textContent = `${items.length} 个`;
    if (!items.length) { this.q("#live-positions").innerHTML = '<div class="empty-state"><strong>暂无持仓</strong><span>启用后等待策略信号；人工仓位也会只读显示在这里</span></div>'; return; }
    const rows = items.map((item) => {
      const lev = Number(item.leverage || 0); const margin = Number(item.initial_margin ?? (lev > 0 ? Number(item.notional || 0) / lev : 0)); const pnlPct = margin ? Number(item.upnl || 0) * 100 / margin : 0;
      const matching = orders.filter((order) => order.symbol === item.symbol && (!order.position_side || order.position_side === "BOTH" || order.position_side === item.position_side));
      const stop = matching.find((order) => /STOP/.test(order.type) && !/TAKE_PROFIT/.test(order.type)); const target = matching.find((order) => /TAKE_PROFIT/.test(order.type));
      const liq = Number(item.liquidation_price || 0); const distance = liq && item.mark_price ? Math.abs(Number(item.mark_price) - liq) * 100 / Number(item.mark_price) : null;
      const basis = item.entry_basis || {}; const basisText = (Array.isArray(basis.reasons) ? basis.reasons : []).join(" · ") || "开仓依据不可用"; const score = basis.signal?.score;
      return `<tr><td><strong>${this.escape(this.symbol(item.symbol))}</strong></td><td class="${item.side === "long" ? "positive" : "negative"}">${item.side === "long" ? "多" : "空"} ${lev > 0 ? `${this.number(lev, 0)}x` : "--"}</td><td>${this.number(item.amt, 4)}</td><td>${this.price(item.entry_price)}</td><td>${this.price(item.mark_price)}</td><td>${this.number(margin)}</td><td class="${this.tone(item.upnl)}"><strong>${this.signed(item.upnl)}</strong><small>${this.signed(pnlPct, 1)}%</small></td><td class="muted"><small>止 ${stop ? this.price(stop.stop_price) : "--"}</small><small>目 ${target ? this.price(target.stop_price) : "--"}</small></td><td class="${distance != null && distance < 3 ? "negative" : "muted"}"><small>${liq ? this.price(liq) : "--"}</small><small>${distance == null ? "" : `距 ${this.number(distance)}%`}</small></td><td class="muted">--</td><td class="basis"><small>${score == null ? (item.managed_by_strategy ? "策略仓位" : "外部仓位") : `实际评分 ${this.signed(score, 0)}`}</small><span title="${this.escape(basisText)}">${this.escape(basisText)}</span></td></tr>`;
    }).join("");
    this.q("#live-positions").innerHTML = `<table class="positions-table"><thead><tr><th>合约</th><th>方向</th><th>数量</th><th>均价</th><th>现价</th><th>保证金</th><th>浮盈</th><th>止损/目标</th><th>强平价</th><th>持仓</th><th>开仓依据</th></tr></thead><tbody>${rows}</tbody></table>`;
  }

  renderTrades(items) {
    this.q("#live-trade-count").textContent = `${items.length} 笔`;
    if (!items.length) { this.q("#live-trades").innerHTML = '<div class="empty-state"><strong>暂无策略平仓记录</strong><span>实盘策略完成一次平仓后显示在这里</span></div>'; return; }
    const rows = items.map((item) => { const basisText = (Array.isArray(item.entryBasis?.reasons) ? item.entryBasis.reasons : []).join(" · ") || "开仓依据不可用"; return `<tr><td class="muted"><small>${this.time(item.time)}</small></td><td><strong>${this.escape(this.symbol(item.symbol))}</strong></td><td class="${item.side > 0 ? "positive" : "negative"}">${item.side > 0 ? "多" : "空"}</td><td class="muted"><small>${this.price(item.entry)} → ${this.price(item.exit)}</small></td><td class="${this.tone(item.pnl)}"><strong>${item.pnl == null ? "--" : `${this.signed(item.pnl)} U`}</strong></td><td class="muted">${this.escape(item.reason)}</td><td class="basis"><span title="${this.escape(basisText)}">${this.escape(basisText)}</span></td></tr>`; }).join("");
    this.q("#live-trades").innerHTML = `<table class="trades-table"><thead><tr><th>时间</th><th>合约</th><th>方向</th><th>开 → 平</th><th>盈亏</th><th>平仓原因</th><th>开仓依据</th></tr></thead><tbody>${rows}</tbody></table>`;
  }

  renderOrders(items) {
    this.q("#live-order-count").textContent = `${items.length} 个`; this.q("#live-detail-count").textContent = `${items.length + Number(this.data?.intents?.length || 0)} 条`;
    if (!items.length) { this.q("#live-orders").innerHTML = '<div class="empty-state"><strong>暂无挂单</strong></div>'; return; }
    this.q("#live-orders").innerHTML = `<table><thead><tr><th>合约</th><th>方向</th><th>类型</th><th>数量</th><th>触发价</th><th>状态</th></tr></thead><tbody>${items.map((item) => `<tr><td>${this.escape(item.symbol)}</td><td>${this.escape(item.side)} · ${this.escape(item.position_side)}</td><td>${this.escape(item.type)}</td><td>${this.number(item.quantity, 4)}</td><td>${this.price(item.stop_price || item.price)}</td><td>${this.escape(item.status)}</td></tr>`).join("")}</tbody></table>`;
  }

  renderIntents(items) {
    this.q("#live-intent-count").textContent = `${items.length} 条`;
    if (!items.length) { this.q("#live-intents").innerHTML = '<div class="empty-state"><strong>暂无策略订单</strong></div>'; return; }
    this.q("#live-intents").innerHTML = `<table><thead><tr><th>时间</th><th>合约</th><th>动作</th><th>方向</th><th>数量</th><th>状态</th><th>订单/错误</th></tr></thead><tbody>${items.map((item) => `<tr><td class="muted">${this.time(item.created_at)}</td><td>${this.escape(item.symbol)}</td><td>${this.escape(({ open: "开仓", close: "平仓", stop: "止损", take_profit: "止盈" })[item.action] || item.action)}</td><td>${this.escape(item.side)} · ${this.escape(item.position_side)}</td><td>${item.quantity == null ? "全仓" : this.number(item.quantity, 4)}</td><td>${this.escape(item.status)}</td><td class="muted">${this.escape(item.error_code || item.binance_order_id || "--")}</td></tr>`).join("")}</tbody></table>`;
  }

  drawCurve(rawCurve, rawStart) {
    const canvas = this.q("#live-chart"); const width = Math.floor(canvas.clientWidth); const height = Math.floor(canvas.clientHeight); if (!width || !height) return;
    const ratio = Math.min(devicePixelRatio || 1, 2); canvas.width = width * ratio; canvas.height = height * ratio; const ctx = canvas.getContext("2d"); ctx.setTransform(ratio, 0, 0, ratio, 0, 0); ctx.clearRect(0, 0, width, height);
    const curve = (rawCurve || []).map((point) => [Number(point[0]), Number(point[1])]).filter((point) => Number.isFinite(point[0]) && Number.isFinite(point[1])); const start = Number(rawStart || curve[0]?.[1] || 0); const pad = { left: 16, right: width < 560 ? 52 : 70, top: 24, bottom: 34 };
    if (!curve.length) { ctx.fillStyle = "#718096"; ctx.font = "13px system-ui"; ctx.fillText("权益数据积累中…", 22, 44); return; }
    const values = curve.map((point) => point[1]).concat(start); const high = Math.max(...values); const low = Math.min(...values); const range = high - low || Math.max(Math.abs(high) * .01, 1); const hi = high + range * .08; const lo = low - range * .08; const total = hi - lo || 1; const plotW = width - pad.left - pad.right; const plotH = height - pad.top - pad.bottom; const x = (i) => pad.left + i * plotW / Math.max(curve.length - 1, 1); const y = (v) => pad.top + (hi - v) / total * plotH;
    ctx.font = "11px system-ui"; ctx.lineWidth = 1; for (let i = 0; i <= 4; i += 1) { const value = hi - total * i / 4; const yy = y(value); ctx.strokeStyle = "#202936"; ctx.beginPath(); ctx.moveTo(pad.left, yy); ctx.lineTo(width - pad.right, yy); ctx.stroke(); ctx.fillStyle = "#718096"; ctx.fillText(this.number(value, range < 5 ? 2 : 0), width - pad.right + 8, yy + 4); }
    ctx.strokeStyle = "#f0b90b"; ctx.setLineDash([6, 6]); ctx.beginPath(); ctx.moveTo(pad.left, y(start)); ctx.lineTo(width - pad.right, y(start)); ctx.stroke(); ctx.setLineDash([]); ctx.fillStyle = "#f0b90b"; ctx.font = "600 11px system-ui"; ctx.fillText(`起点 ${this.number(start, 0)}`, pad.left + 6, Math.max(13, y(start) - 7));
    const last = curve.at(-1)[1]; const color = last >= start ? "#2bd7a3" : "#f6465d"; const gradient = ctx.createLinearGradient(0, pad.top, 0, height - pad.bottom); gradient.addColorStop(0, last >= start ? "rgba(43,215,163,.28)" : "rgba(246,70,93,.26)"); gradient.addColorStop(1, "rgba(43,215,163,.01)"); ctx.beginPath(); curve.forEach((p, i) => i ? ctx.lineTo(x(i), y(p[1])) : ctx.moveTo(x(i), y(p[1]))); ctx.lineTo(x(curve.length - 1), height - pad.bottom); ctx.lineTo(x(0), height - pad.bottom); ctx.closePath(); ctx.fillStyle = gradient; ctx.fill(); ctx.beginPath(); curve.forEach((p, i) => i ? ctx.lineTo(x(i), y(p[1])) : ctx.moveTo(x(i), y(p[1]))); ctx.strokeStyle = color; ctx.lineWidth = 2.2; ctx.lineJoin = "round"; ctx.stroke();
    ctx.fillStyle = "#718096"; ctx.font = "11px system-ui"; [...new Set([0, Math.floor((curve.length - 1) / 2), curve.length - 1])].forEach((i, n, indexes) => { const label = this.shortTime(curve[i][0]); const measured = ctx.measureText(label).width; let xx = x(i) - measured / 2; if (!n) xx = pad.left; if (n === indexes.length - 1) xx = width - pad.right - measured; ctx.fillText(label, xx, height - 9); });
  }

  async loadCatalog() {
    if (!this.catalog.length) {
      const data = await window.quantdeskApi("/api/v2/strategies");
      this.catalog = (data.items || []).filter((item) => item.status === "active" && item.lifecycle_status === "published");
    }
    if (!this.catalog.length) throw new Error("请先在策略中心创建并发布策略");
    this.q("#live-strategy").innerHTML = this.catalog.map((item) => `<option value="${this.escape(item.id)}">${this.escape(item.name)}</option>`).join("");
  }

  showStrategyModal() {
    this.q("#live-ack").checked = false;
    this.formError("");
    this.q("#live-modal").classList.remove("hidden");
    this.q("#live-modal").setAttribute("aria-hidden", "false");
  }

  async openCreate() {
    try {
      await this.loadCatalog();
      this.editingAccountId = null;
      this.q("#live-modal-kicker").textContent = "NEW LIVE DEPLOYMENT";
      this.q("#live-modal-title").textContent = "新增实盘策略";
      this.q("#live-modal-description").textContent = "交易品种和模拟盘一致；创建后保持暂停，确认启用后才会通过 Binance API 真实下单。";
      this.q("#live-name-field").classList.remove("hidden");
      this.q("#live-name").required = true;
      this.q("#live-ack-text").textContent = "我理解这是实盘部署；创建后仍保持暂停，只有再次确认才会开始真实交易。";
      this.q("#live-submit").textContent = "创建为暂停";
      this.applyStrategy();
      this.showStrategyModal();
    } catch (error) { this.banner(`无法创建实盘策略：${error.message}`, "error"); }
  }

  async openAdjust() {
    const account = this.accounts.find((item) => item.id === this.selectedAccountId);
    if (!account) return;
    if (account.status !== "paused") {
      this.banner("请先暂停实盘策略，再调整策略和风控参数。", "error");
      return;
    }
    try {
      await this.loadCatalog();
      this.editingAccountId = account.id;
      this.q("#live-modal-kicker").textContent = "ADJUST LIVE STRATEGY";
      this.q("#live-modal-title").textContent = "调整实盘策略";
      this.q("#live-modal-description").textContent = "保存只更新本地策略快照和风控参数，不会下单、平仓或撤单；之后需要重新确认启用实盘。";
      this.q("#live-name-field").classList.add("hidden");
      this.q("#live-name").required = false;
      this.q("#live-strategy").value = this.catalog.some((item) => item.id === account.strategy_id)
        ? account.strategy_id
        : this.catalog[0].id;
      const config = account.config || {};
      this.q("#live-leverage").value = String(config.leverage || 3);
      this.q("#live-max-positions").value = String(Math.min(Number(config.max_positions || 1), 20));
      this.q("#live-size").value = String(config.position_size_pct || 2);
      this.q("#live-cap").value = String(Number(config.margin_cap || 0.2) * 100);
      this.q("#live-risk-per-trade").value = String(Math.max(0.05, Math.min(Number(config.risk_per_trade_pct || 0.5), 1)));
      this.q("#live-total-risk").value = String(Math.max(0.25, Math.min(Number(config.max_total_risk_pct || 4), 8)));
      this.q("#live-risk-leverage").value = String(config.risk_max_leverage || 10);
      this.q("#live-liq-buffer").value = String(config.liquidation_buffer_pct || 1.5);
      this.q("#live-cluster-cap").value = String(config.max_cluster_positions || 2);
      this.q("#live-daily-loss").value = String(config.daily_loss_limit_pct || 2);
      this.q("#live-max-drawdown").value = String(Math.max(1, Math.min(Number(config.max_drawdown_pct || 6), 30)));
      this.q("#live-short-risk").value = String(config.short_risk_multiplier ?? 0.5);
      this.q("#live-ticker-age").value = String(config.max_ticker_age_seconds || 120);
      this.q("#live-signal-age").value = String((config.max_signal_age_seconds || 18000) / 60);
      this.q("#live-block-high-risk").checked = config.block_high_risk_products !== false;
      this.q("#live-ack-text").textContent = "我确认当前实盘策略已暂停，并理解保存不会处理 Binance 已有仓位或挂单。";
      this.q("#live-submit").textContent = "保存调整";
      this.showStrategyModal();
    } catch (error) { this.banner(`无法调整实盘策略：${error.message}`, "error"); }
  }

  closeCreate() {
    this.q("#live-modal").classList.add("hidden");
    this.q("#live-modal").setAttribute("aria-hidden", "true");
    this.editingAccountId = null;
  }

  applyStrategy() {
    const strategy = this.catalog.find((item) => item.id === this.q("#live-strategy").value);
    if (!strategy) return;
    const risk = strategy.risk_defaults || {};
    if (!this.editingAccountId) this.q("#live-name").value = `${strategy.name} 实盘`;
    this.q("#live-leverage").value = String(Math.max(1, Math.min(Number(risk.leverage || 3), 20)));
    this.q("#live-max-positions").value = String(Math.max(1, Math.min(Number(risk.max_positions || 1), 20)));
    this.q("#live-size").value = String(Math.max(0.1, Math.min(Number(risk.position_size_pct || 2), 10)));
    this.q("#live-cap").value = String(Math.max(1, Math.min(Number(risk.margin_cap || 0.2) * 100, 50)));
    this.q("#live-risk-per-trade").value = String(Math.max(0.05, Math.min(Number(risk.risk_per_trade_pct || 0.5), 1)));
    this.q("#live-total-risk").value = String(Math.max(0.25, Math.min(Number(risk.max_total_risk_pct || 4), 8)));
    this.q("#live-risk-leverage").value = String(Math.max(1, Math.min(Number(risk.risk_max_leverage || 10), 20)));
    this.q("#live-liq-buffer").value = String(Math.max(0.5, Math.min(Number(risk.liquidation_buffer_pct || 1.5), 10)));
    this.q("#live-cluster-cap").value = String(Math.max(1, Math.min(Number(risk.max_cluster_positions || 2), 20)));
    this.q("#live-daily-loss").value = String(Math.max(0.25, Math.min(Number(risk.daily_loss_limit_pct || 2), 20)));
    this.q("#live-max-drawdown").value = String(Math.max(1, Math.min(Number(risk.max_drawdown_pct || 6), 30)));
    this.q("#live-short-risk").value = String(Math.max(0, Math.min(Number(risk.short_risk_multiplier ?? 0.5), 1)));
    this.q("#live-ticker-age").value = String(Math.max(30, Math.min(Number(risk.max_ticker_age_seconds || 120), 900)));
    this.q("#live-signal-age").value = String(Math.max(5, Math.min(Number(risk.max_signal_age_seconds || 18000) / 60, 2880)));
    this.q("#live-block-high-risk").checked = risk.block_high_risk_products !== false;
  }

  async create(event) {
    event.preventDefault();
    const payload = {
      strategy_id: this.q("#live-strategy").value,
      leverage: Number(this.q("#live-leverage").value),
      max_positions: Number(this.q("#live-max-positions").value),
      position_size_pct: Number(this.q("#live-size").value),
      margin_cap: Number(this.q("#live-cap").value) / 100,
      risk_per_trade_pct: Number(this.q("#live-risk-per-trade").value),
      max_total_risk_pct: Number(this.q("#live-total-risk").value),
      max_cluster_positions: Number(this.q("#live-cluster-cap").value),
      risk_max_leverage: Number(this.q("#live-risk-leverage").value),
      liquidation_buffer_pct: Number(this.q("#live-liq-buffer").value),
      daily_loss_limit_pct: Number(this.q("#live-daily-loss").value),
      max_drawdown_pct: Number(this.q("#live-max-drawdown").value),
      short_risk_multiplier: Number(this.q("#live-short-risk").value),
      max_ticker_age_seconds: Number(this.q("#live-ticker-age").value),
      max_signal_age_seconds: Number(this.q("#live-signal-age").value) * 60,
      block_high_risk_products: this.q("#live-block-high-risk").checked,
    };
    const editingId = this.editingAccountId;
    if (!editingId) payload.name = this.q("#live-name").value.trim();
    const button = this.q("#live-submit");
    button.disabled = true;
    try {
      const updated = editingId
        ? await this.api(`/accounts/${encodeURIComponent(editingId)}/strategy`, { method: "PUT", body: JSON.stringify(payload) })
        : await this.api("/accounts", { method: "POST", body: JSON.stringify(payload) });
      this.closeCreate();
      await this.loadAccounts(updated.id);
      await this.load(true);
      this.banner(editingId ? "实盘策略与风控参数已更新，当前保持暂停；未执行任何 Binance 交易。" : "实盘策略已创建并保持暂停。核对风控参数后可单独确认启用。", "success");
    } catch (error) { this.formError(`${editingId ? "调整" : "创建"}失败：${error.message}`); }
    finally { button.disabled = false; }
  }
  async toggle() { const account = this.accounts.find((item) => item.id === this.selectedAccountId); if (!account) return; const button = this.q("#live-toggle"); button.disabled = true; try { if (account.status === "active" || account.status === "error") { await this.api(`/accounts/${encodeURIComponent(account.id)}`, { method: "PATCH", body: JSON.stringify({ status: "paused" }) }); await this.loadAccounts(account.id); await this.load(true); this.banner("策略已暂停；Binance 已有仓位和人工订单未被改动。", "success"); return; } if (!this.systemEnabled) throw new Error("服务端实盘总开关尚未启用"); const typed = prompt(`这是真实资金操作。请输入部署名称“${account.name}”确认启用：`, ""); if (typed === null) return; if (typed !== account.name) throw new Error("输入的部署名称不匹配"); if (!confirm("最终确认：新信号将通过 Binance API 提交真实订单。是否继续？")) return; await this.api(`/accounts/${encodeURIComponent(account.id)}/arm`, { method: "POST", body: JSON.stringify({ confirmation_name: typed, acknowledge_real_funds: true }) }); await this.loadAccounts(account.id); await this.load(true); this.banner("实盘策略已启用。", "success"); } catch (error) { this.banner(`状态更新失败：${error.message}`, "error"); } finally { button.disabled = false; } }

  async deleteDeployment() {
    const account = this.accounts.find((item) => item.id === this.selectedAccountId);
    if (!account) return;
    if (account.status === "active") {
      this.banner("请先暂停实盘策略，核对 Binance 仓位和挂单后再删除。", "error");
      return;
    }
    const pending = (this.data?.intents || []).filter((item) => ["created", "submitted", "unknown"].includes(item.status));
    if (pending.length) {
      this.banner(`仍有 ${pending.length} 条未决策略订单，请先核对并处理后再删除。`, "error");
      return;
    }
    const typed = prompt(`删除后“${account.name}”将从实盘列表移除，但不会平仓或撤销 Binance 订单。请输入完整部署名称确认：`, "");
    if (typed === null) return;
    if (typed !== account.name) {
      this.banner("输入的部署名称不匹配，未执行删除。", "error");
      return;
    }
    const button = this.q("#live-delete"); button.disabled = true; button.textContent = "删除中…";
    try {
      await this.api(`/accounts/${encodeURIComponent(account.id)}`, { method: "PATCH", body: JSON.stringify({ status: "archived" }) });
      try { localStorage.removeItem(`quantdesk.live.curve.${account.id}`); } catch (_) {}
      this.selectedAccountId = null;
      try { localStorage.removeItem("quantdesk.live.selected-account"); } catch (_) {}
      await this.loadAccounts();
      if (this.selectedAccountId) await this.load(true);
      this.banner(`实盘策略“${account.name}”已删除；未对 Binance 仓位或订单执行任何操作。`, "success");
    } catch (error) {
      this.banner(`删除失败：${error.message}`, "error");
    } finally {
      button.textContent = "删除";
      button.disabled = !this.selectedAccountId;
    }
  }

  async renameDeployment() {
    const account = this.accounts.find((item) => item.id === this.selectedAccountId);
    if (!account) return;
    const entered = prompt("请输入新的实盘策略名称：", account.name);
    if (entered === null) return;
    const name = entered.trim();
    if (!name) { this.banner("实盘策略名称不能为空。", "error"); return; }
    if (name === account.name) return;
    const button = this.q("#live-rename"); button.disabled = true; button.textContent = "保存中…";
    try {
      const updated = await this.api(`/accounts/${encodeURIComponent(account.id)}`, { method: "PATCH", body: JSON.stringify({ name }) });
      await this.loadAccounts(updated.id);
      await this.load(true);
      this.banner(`实盘策略名称已修改为“${updated.name}”。`, "success");
    } catch (error) {
      this.banner(`名称修改失败：${error.message}`, "error");
    } finally {
      button.textContent = "修改名称";
      button.disabled = !this.selectedAccountId;
    }
  }

  state(label, kind = "") { const node = this.q("#live-state"); node.className = `running-state ${kind}`.trim(); node.querySelector("span").textContent = label; }
  banner(message, kind = "") { const node = this.q("#live-banner"); node.textContent = message; node.className = message ? `paper-banner ${kind}`.trim() : "paper-banner hidden"; }
  formError(message) { const node = this.q("#live-form-error"); node.textContent = message; node.classList.toggle("hidden", !message); }
  statusLabel(value) { return ({ active: "运行中", paused: "已暂停", error: "错误停止", archived: "已归档" })[value] || value || "--"; }
  accountType(value) { return value === "UM_FUTURE" ? "U 本位合约" : value === "PORTFOLIO_MARGIN" ? "统一账户" : ""; }
  tone(value) { const number = Number(value); return !Number.isFinite(number) || number === 0 ? "neutral" : number > 0 ? "positive" : "negative"; }
  number(value, digits = 2) { const number = Number(value); return Number.isFinite(number) ? number.toLocaleString("zh-CN", { minimumFractionDigits: digits, maximumFractionDigits: digits }) : "--"; }
  signed(value, digits = 2) { const number = Number(value); return Number.isFinite(number) ? `${number >= 0 ? "+" : ""}${this.number(number, digits)}` : "--"; }
  price(value) { const number = Number(value); if (!Number.isFinite(number) || !number) return "--"; return this.number(number, Math.abs(number) >= 100 ? 2 : Math.abs(number) >= 1 ? 4 : 6); }
  symbol(value) { return String(value || "--").replace(/(USDT|USD1)$/i, ""); }
  time(value) { const date = typeof value === "number" ? new Date(value * 1000) : new Date(value); return Number.isNaN(date.getTime()) ? "--" : date.toLocaleString("zh-CN", { hour12: false }); }
  shortTime(value) { const date = new Date(Number(value) * 1000); if (Number.isNaN(date.getTime())) return "--"; return `${date.getMonth() + 1}/${date.getDate()} ${String(date.getHours()).padStart(2, "0")}:${String(date.getMinutes()).padStart(2, "0")}`; }
  escape(value) { return String(value ?? "").replace(/[&<>'"]/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" })[char]); }
}

if (!customElements.get("live-dashboard")) customElements.define("live-dashboard", LiveDashboard);
