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
    this.systemEnabled = false;
    this.renderShell();
    this.bindEvents();
  }

  renderShell() {
    this.shadowRoot.innerHTML = `
      <link rel="stylesheet" href="/assets/live.css?v=20260804-1">
      <main class="live-dashboard">
        <nav class="live-tabs" aria-label="实盘部署切换">
          <div id="live-tabs" class="tab-list"><span class="muted">正在读取实盘部署…</span></div>
          <button id="live-create" class="create" type="button">＋ 新增实盘策略</button>
        </nav>
        <section id="live-content">
          <header class="live-head">
            <div>
              <div class="title-row"><h1>Binance 实盘交易</h1><span class="live-badge">REAL FUNDS</span></div>
              <p id="live-subtitle" class="subtitle">策略部署默认暂停，必须通过实盘预检后才会执行。</p>
              <p id="live-rules" class="rules">单用户仅允许一个运行实例 · 仅支持 USD-M 单向持仓</p>
            </div>
            <div class="live-actions">
              <span id="live-state" class="state"><i></i><span>连接中</span></span>
              <button id="live-refresh" class="button" type="button">刷新</button>
              <button id="live-toggle" class="button danger" type="button" disabled>启用实盘</button>
            </div>
          </header>
          <div id="live-banner" class="banner hidden" role="status" aria-live="polite"></div>
          <div class="warning-row"><span>!</span><div><strong>真实资金风险</strong><br>暂停只会阻止新策略动作，不会自动平掉 Binance 已有仓位，也不会撤销人工订单。</div></div>
          <section class="metric-grid">
            <article class="metric"><span>钱包权益</span><strong id="live-wallet">--</strong><small id="live-account-type">等待 Binance</small></article>
            <article class="metric"><span>可用余额</span><strong id="live-available">--</strong><small>USD 估值</small></article>
            <article class="metric"><span>未实现盈亏</span><strong id="live-upnl">--</strong><small>交易所实时快照</small></article>
            <article class="metric"><span>部署状态</span><strong id="live-deployment-status">--</strong><small id="live-last-tick">尚未运行</small></article>
          </section>
          <section class="panel"><div class="panel-head"><h2>Binance 当前持仓</h2><span id="live-position-count">0 个</span></div><div id="live-positions" class="table-wrap"><div class="empty">正在读取持仓…</div></div></section>
          <section class="panel"><div class="panel-head"><h2>Binance 当前挂单</h2><span id="live-order-count">0 个</span></div><div id="live-orders" class="table-wrap"><div class="empty">正在读取挂单…</div></div></section>
          <section class="panel"><div class="panel-head"><h2>策略订单审计</h2><span id="live-intent-count">最近 0 条</span></div><div id="live-intents" class="table-wrap"><div class="empty">暂无策略订单意图</div></div></section>
          <p class="safety">系统只会管理带 QuantDesk 幂等订单号的策略订单。API 密钥与签名只在服务端使用；页面不会接收或回显密钥。任何订单状态不确定或保护单失败都会令部署进入错误状态并停止后续开仓。</p>
        </section>
        <div id="live-empty" class="panel empty hidden">还没有实盘策略。先在策略中心发布策略，再创建一个默认暂停的实盘部署。</div>
        <div id="live-modal" class="modal hidden" aria-hidden="true">
          <button class="backdrop" type="button" data-close aria-label="关闭"></button>
          <section class="modal-card" role="dialog" aria-modal="true" aria-labelledby="live-modal-title">
            <button class="close" type="button" data-close aria-label="关闭">×</button>
            <h2 id="live-modal-title">新增实盘策略</h2>
            <p>创建时只保存策略快照与风险边界，不会立即下单；创建后还需单独输入名称确认启用。</p>
            <form id="live-form" class="form-grid">
              <label class="field wide"><span>已发布策略</span><select id="live-strategy" required></select></label>
              <label class="field wide"><span>部署名称</span><input id="live-name" maxlength="100" required></label>
              <label class="field wide"><span>Binance 合约（逗号分隔，最多 5 个）</span><input id="live-symbols" value="BTCUSDT" maxlength="164" required></label>
              <label class="field"><span>杠杆（1–20x）</span><input id="live-leverage" type="number" min="1" max="20" step="1" value="3" required></label>
              <label class="field"><span>最大持仓数（1–5）</span><input id="live-max-positions" type="number" min="1" max="5" step="1" value="1" required></label>
              <label class="field"><span>单笔保证金占权益（≤10%）</span><input id="live-size" type="number" min="0.1" max="10" step="0.1" value="2" required></label>
              <label class="field"><span>总保证金上限（≤50%）</span><input id="live-cap" type="number" min="0.01" max="0.5" step="0.01" value="0.2" required></label>
              <label class="check wide"><input id="live-ack" type="checkbox" required><span>我理解这是实盘部署；创建后仍保持暂停，只有再次确认才会开始真实交易。</span></label>
              <p id="live-form-error" class="form-error hidden" role="alert"></p>
              <footer class="modal-actions wide"><button class="button" type="button" data-close>取消</button><button id="live-submit" class="primary" type="submit">创建为暂停</button></footer>
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
    this.q("#live-refresh").addEventListener("click", () => this.load());
    this.q("#live-create").addEventListener("click", () => this.openCreate());
    this.q("#live-form").addEventListener("submit", (event) => this.create(event));
    this.q("#live-strategy").addEventListener("change", () => this.applyStrategy());
    this.q("#live-toggle").addEventListener("click", () => this.toggle());
    this.q("#live-tabs").addEventListener("click", (event) => {
      const button = event.target.closest("[data-id]");
      if (button) this.select(button.dataset.id);
    });
    this.shadowRoot.querySelectorAll("[data-close]").forEach((button) => button.addEventListener("click", () => this.closeCreate()));
  }

  start() {
    if (this.running) return;
    this.running = true;
    this.loadAccounts().then(() => this.load()).catch((error) => this.banner(`实盘页面加载失败：${error.message}`, "error"));
    this.timer = window.setInterval(() => this.load(), 10000);
  }

  pause() {
    this.running = false;
    if (this.timer) window.clearInterval(this.timer);
    this.timer = null;
  }

  async loadAccounts(preferred = "") {
    const payload = await this.api("/accounts");
    this.accounts = Array.isArray(payload.items) ? payload.items : [];
    this.systemEnabled = Boolean(payload.system_enabled);
    const remembered = preferred || this.remembered();
    this.selectedAccountId = this.accounts.some((item) => item.id === remembered) ? remembered : this.accounts[0]?.id || null;
    this.renderTabs();
    this.q("#live-content").classList.toggle("hidden", !this.selectedAccountId);
    this.q("#live-empty").classList.toggle("hidden", Boolean(this.selectedAccountId));
    if (!this.selectedAccountId) {
      const notes = ["还没有实盘策略。先在策略中心发布策略，再创建一个默认暂停的实盘部署。"];
      if (!this.systemEnabled) notes.push("服务端实盘总开关当前关闭。");
      if (!payload.credentials_configured) notes.push("尚未配置 Binance READ + TRADE API 凭据。");
      this.q("#live-empty").textContent = notes.join(" ");
    }
    if (!this.systemEnabled) this.banner("服务端实盘总开关当前关闭。设置 BINANCE_LIVE_TRADING_ENABLED=true 并重启后，才可确认启用；当前页面仍可安全查看和创建暂停部署。", "");
    if (!payload.credentials_configured) this.banner("尚未配置 Binance API 凭据，请先到系统设置保存 READ + TRADE 权限的密钥。", "error");
  }

  renderTabs() {
    const root = this.q("#live-tabs");
    if (!this.accounts.length) { root.innerHTML = '<span class="muted">暂无实盘部署</span>'; return; }
    root.innerHTML = this.accounts.map((item) => `<button class="tab ${item.id === this.selectedAccountId ? "active" : ""}" type="button" data-id="${this.escape(item.id)}">${this.escape(item.name)} · ${this.statusLabel(item.status)}</button>`).join("");
  }

  async select(id) {
    this.selectedAccountId = id;
    try { window.localStorage.setItem("quantdesk.live.selected-account", id); } catch (_) {}
    this.renderTabs();
    await this.load();
  }

  remembered() { try { return window.localStorage.getItem("quantdesk.live.selected-account") || ""; } catch (_) { return ""; } }

  async load() {
    if (!this.selectedAccountId || this.loading) return;
    this.loading = true;
    try {
      const data = await this.api(`?account_id=${encodeURIComponent(this.selectedAccountId)}`);
      this.renderData(data);
    } catch (error) {
      this.state("连接异常", "error");
      this.banner(`实盘数据加载失败：${error.message}`, "error");
    } finally { this.loading = false; }
  }

  renderData(data) {
    const account = data.live_account || {};
    const binance = data.binance || {};
    const positions = Array.isArray(data.positions) ? data.positions : [];
    const orders = Array.isArray(data.open_orders) ? data.open_orders : [];
    const intents = Array.isArray(data.order_intents) ? data.order_intents : [];
    this.accounts = this.accounts.map((item) => item.id === account.id ? account : item);
    this.renderTabs();
    const config = account.config || {};
    this.q("#live-subtitle").textContent = `${account.name || "实盘部署"} · ${account.strategy_name || "策略"} · ${(config.symbols || []).join(" / ") || "未配置标的"}`;
    this.q("#live-rules").textContent = `${config.leverage || "--"}x 杠杆 · 单笔 ${config.position_size_pct || "--"}% · 最多 ${config.max_positions || "--"} 个仓位 · 总保证金上限 ${this.number(Number(config.margin_cap || 0) * 100, 0)}%`;
    this.q("#live-wallet").textContent = binance.connected ? `${this.number(binance.wallet_balance)} USD` : "--";
    this.q("#live-available").textContent = binance.connected ? `${this.number(binance.available_balance)} USD` : "--";
    this.q("#live-upnl").textContent = binance.connected ? this.signed(binance.unrealized_pnl, " USD") : "--";
    this.q("#live-upnl").className = this.tone(binance.unrealized_pnl);
    this.q("#live-account-type").textContent = binance.connected ? this.accountType(binance.account_type) : `Binance：${binance.error_category || "未连接"}`;
    this.q("#live-deployment-status").textContent = this.statusLabel(account.status);
    this.q("#live-last-tick").textContent = account.last_tick_at ? `检查于 ${this.time(account.last_tick_at)}` : (account.last_error_code ? `错误：${account.last_error_code}` : "尚未执行策略检查");
    const toggle = this.q("#live-toggle");
    toggle.disabled = false;
    toggle.textContent = account.status === "active" ? "暂停策略" : (account.status === "error" ? "清除错误并暂停" : "确认启用实盘");
    toggle.classList.toggle("danger", account.status === "active");
    this.state(binance.connected ? (account.status === "active" ? "实盘运行中" : "Binance 已连接") : "Binance 未连接", binance.connected ? (account.status === "active" ? "active" : "") : "error");
    if (account.last_error_code) this.banner(`部署已停止：${account.last_error_code}。先核对 Binance 仓位与挂单，再清除错误。`, "error");
    else if (this.systemEnabled && binance.connected) this.banner("", "");
    this.renderPositions(positions);
    this.renderOrders(orders);
    this.renderIntents(intents);
  }

  renderPositions(items) {
    this.q("#live-position-count").textContent = `${items.length} 个`;
    if (!items.length) { this.q("#live-positions").innerHTML = '<div class="empty">当前 Binance 账户没有非零 USD-M 仓位</div>'; return; }
    const rows = items.map((item) => `<tr><td><strong>${this.escape(item.symbol)}</strong></td><td class="${item.side === "short" ? "negative" : "positive"}">${item.side === "short" ? "空" : "多"} ${this.number(item.leverage, 0)}x</td><td>${this.number(item.amt, 6)}</td><td>${this.price(item.entry_price)}</td><td>${this.price(item.mark_price)}</td><td>${this.number(item.notional)}</td><td class="${this.tone(item.upnl)}">${this.signed(item.upnl)}</td></tr>`).join("");
    this.q("#live-positions").innerHTML = `<table><thead><tr><th>合约</th><th>方向</th><th>数量</th><th>开仓均价</th><th>标记价格</th><th>名义价值</th><th>未实现盈亏</th></tr></thead><tbody>${rows}</tbody></table>`;
  }

  renderOrders(items) {
    this.q("#live-order-count").textContent = `${items.length} 个`;
    if (!items.length) { this.q("#live-orders").innerHTML = '<div class="empty">当前没有未成交订单或保护单</div>'; return; }
    const rows = items.map((item) => `<tr><td>${this.escape(item.symbol)}</td><td class="${item.side === "SELL" ? "negative" : "positive"}">${this.escape(item.side)}</td><td>${this.escape(item.type)}</td><td>${this.number(item.quantity, 6)}</td><td>${item.stop_price ? this.price(item.stop_price) : this.price(item.price)}</td><td>${this.escape(item.status)}</td><td class="muted">${this.escape(item.client_order_id || "--")}</td></tr>`).join("");
    this.q("#live-orders").innerHTML = `<table><thead><tr><th>合约</th><th>方向</th><th>类型</th><th>数量</th><th>委托/触发价</th><th>状态</th><th>客户端订单号</th></tr></thead><tbody>${rows}</tbody></table>`;
  }

  renderIntents(items) {
    this.q("#live-intent-count").textContent = `最近 ${items.length} 条`;
    if (!items.length) { this.q("#live-intents").innerHTML = '<div class="empty">暂无策略订单意图；创建或启用部署本身不会生成订单。</div>'; return; }
    const actions = { open: "开仓", close: "平仓", stop: "止损保护", take_profit: "止盈保护" };
    const rows = items.map((item) => `<tr><td class="muted">${this.time(item.created_at)}</td><td>${this.escape(item.symbol)}</td><td>${actions[item.action] || this.escape(item.action)}</td><td>${this.escape(item.side)}</td><td>${this.escape(item.order_type)}</td><td>${item.quantity == null ? "全仓关闭" : this.number(item.quantity, 6)}</td><td><span class="status-pill ${this.escape(item.status)}">${this.escape(item.status)}</span></td><td class="muted">${this.escape(item.error_code || item.binance_order_id || "--")}</td></tr>`).join("");
    this.q("#live-intents").innerHTML = `<table><thead><tr><th>时间</th><th>合约</th><th>动作</th><th>方向</th><th>类型</th><th>数量</th><th>状态</th><th>订单/错误</th></tr></thead><tbody>${rows}</tbody></table>`;
  }

  async openCreate() {
    try {
      if (!this.catalog.length) {
        const data = await window.quantdeskApi("/api/v2/strategies");
        this.catalog = (data.items || []).filter((item) => item.status === "active" && item.lifecycle_status === "published");
      }
      if (!this.catalog.length) throw new Error("请先在策略中心创建并发布策略");
      this.q("#live-strategy").innerHTML = this.catalog.map((item) => `<option value="${this.escape(item.id)}">${this.escape(item.name)}</option>`).join("");
      this.applyStrategy();
      this.q("#live-ack").checked = false;
      this.formError("");
      this.q("#live-modal").classList.remove("hidden");
      this.q("#live-modal").setAttribute("aria-hidden", "false");
    } catch (error) { this.banner(`无法创建实盘部署：${error.message}`, "error"); }
  }

  closeCreate() { this.q("#live-modal").classList.add("hidden"); this.q("#live-modal").setAttribute("aria-hidden", "true"); }
  applyStrategy() {
    const strategy = this.catalog.find((item) => item.id === this.q("#live-strategy").value);
    if (!strategy) return;
    this.q("#live-name").value = `${strategy.name} 实盘`;
    this.q("#live-leverage").value = String(Math.min(Number(strategy.risk_defaults?.leverage || 3), 5));
  }

  async create(event) {
    event.preventDefault();
    const symbols = this.q("#live-symbols").value.split(/[,，\s]+/).map((item) => item.trim().toUpperCase()).filter(Boolean);
    if (!symbols.length || symbols.length > 5) { this.formError("请输入 1 到 5 个 Binance USD-M 合约。"); return; }
    const payload = {
      name: this.q("#live-name").value.trim(), strategy_id: this.q("#live-strategy").value, symbols,
      leverage: Number(this.q("#live-leverage").value), max_positions: Number(this.q("#live-max-positions").value),
      position_size_pct: Number(this.q("#live-size").value), margin_cap: Number(this.q("#live-cap").value),
    };
    const button = this.q("#live-submit"); button.disabled = true;
    try {
      const created = await this.api("/accounts", { method: "POST", body: JSON.stringify(payload) });
      this.closeCreate(); await this.loadAccounts(created.id); await this.load();
      this.banner("实盘策略已创建并保持暂停。核对风控参数后，可单独确认启用。", "success");
    } catch (error) { this.formError(`创建失败：${error.message}`); }
    finally { button.disabled = false; }
  }

  async toggle() {
    const account = this.accounts.find((item) => item.id === this.selectedAccountId);
    if (!account) return;
    const button = this.q("#live-toggle"); button.disabled = true;
    try {
      if (account.status === "active" || account.status === "error") {
        await this.api(`/accounts/${encodeURIComponent(account.id)}`, { method: "PATCH", body: JSON.stringify({ status: "paused" }) });
        await this.loadAccounts(account.id); await this.load();
        this.banner(account.status === "active" ? "策略已暂停；Binance 已有仓位和人工订单未被改动。" : "错误状态已清除并保持暂停，请核对交易所状态后再启用。", "success");
        return;
      }
      if (!this.systemEnabled) throw new Error("服务端实盘总开关尚未启用");
      const typed = window.prompt(`这是实盘资金操作。请输入部署名称“${account.name}”确认启用：`, "");
      if (typed === null) return;
      if (typed !== account.name) throw new Error("输入的部署名称不匹配");
      if (!window.confirm("最终确认：策略产生新信号后将通过 Binance API 下真实订单，并自动提交止损与止盈保护单。是否继续？")) return;
      await this.api(`/accounts/${encodeURIComponent(account.id)}/arm`, { method: "POST", body: JSON.stringify({ confirmation_name: typed, acknowledge_real_funds: true }) });
      await this.loadAccounts(account.id); await this.load();
      this.banner("实盘策略已启用。每个策略订单都会写入下方审计账本。", "success");
    } catch (error) { this.banner(`状态更新失败：${error.message}`, "error"); }
    finally { button.disabled = false; }
  }

  state(label, kind = "") { const node = this.q("#live-state"); node.className = `state ${kind}`.trim(); node.querySelector("span").textContent = label; }
  banner(message, kind = "") { const node = this.q("#live-banner"); node.textContent = message; node.className = message ? `banner ${kind}`.trim() : "banner hidden"; }
  formError(message) { const node = this.q("#live-form-error"); node.textContent = message; node.classList.toggle("hidden", !message); }
  statusLabel(value) { return ({ active: "运行中", paused: "已暂停", error: "错误停止", archived: "已归档" })[value] || value || "--"; }
  accountType(value) { return value === "UM_FUTURE" ? "U 本位合约" : value === "PORTFOLIO_MARGIN" ? "统一账户" : "--"; }
  tone(value) { const number = Number(value); return number > 0 ? "positive" : number < 0 ? "negative" : ""; }
  number(value, digits = 2) { const number = Number(value); return Number.isFinite(number) ? number.toLocaleString("zh-CN", { minimumFractionDigits: digits, maximumFractionDigits: digits }) : "--"; }
  signed(value, suffix = "") { const number = Number(value); return Number.isFinite(number) ? `${number >= 0 ? "+" : ""}${this.number(number)}${suffix}` : "--"; }
  price(value) { const number = Number(value); if (!Number.isFinite(number)) return "--"; return this.number(number, Math.abs(number) >= 100 ? 2 : Math.abs(number) >= 1 ? 4 : 6); }
  time(value) { const date = new Date(value); return Number.isNaN(date.getTime()) ? "--" : date.toLocaleString("zh-CN", { hour12: false }); }
  escape(value) { return String(value ?? "").replace(/[&<>'"]/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" })[char]); }
}

if (!customElements.get("live-dashboard")) customElements.define("live-dashboard", LiveDashboard);
