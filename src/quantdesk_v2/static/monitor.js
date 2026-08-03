class ContractMonitor extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this.state = {
      overview: [],
      watchlist: new Set(),
      lastAlertId: 0,
      modal: { symbol: null, tf: "1h" },
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
      <link rel="stylesheet" href="/assets/monitor.css?v=20260804-1">
      <div class="monitor">
        <header class="monitor-head">
          <div class="monitor-logo">⚡ QuantDesk <small>币安 TradFi 合约监控</small></div>
          <div class="monitor-actions">
            <span class="clock" id="monitor-clock"></span>
            <span id="engine-state" class="badge">连接中…</span>
            <button id="btn-refresh" type="button">刷新</button>
            <button id="btn-notify" type="button">通知</button>
            <button id="btn-sound" class="on" type="button">音效</button>
          </div>
        </header>
        <div id="error-banner" class="error-banner hidden"></div>
        <div class="monitor-layout">
          <section class="panel grid-panel">
            <div class="panel-title">
              <span>合约监控 <em id="sym-count"></em></span>
              <div class="filters">
                <input id="search" aria-label="搜索合约" placeholder="搜索…">
                <select id="filter" aria-label="筛选合约">
                  <option value="all">全部</option>
                  <option value="mine">自选</option>
                  <option value="long">偏多 ≥60</option>
                  <option value="short">偏空 ≤-60</option>
                </select>
                <select id="sort" aria-label="合约排序">
                  <option value="score">按|评分|</option>
                  <option value="pct">按涨跌幅</option>
                  <option value="alpha">按代码</option>
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
              <div class="panel-title"><span>舆情流 <em id="news-count"></em></span></div>
              <div id="news" class="news"></div>
            </section>
          </aside>
        </div>
      </div>
      <div id="modal" class="modal hidden">
        <div class="modal-box">
          <div class="modal-head">
            <div>
              <strong id="modal-symbol" class="modal-symbol"></strong>
              <span id="modal-price" class="modal-price"></span>
              <span id="modal-pct"></span>
              <button id="modal-watch" class="watch" type="button" aria-label="切换自选">☆</button>
            </div>
            <div>
              <span class="tf-switch">
                <button data-tf="15m" type="button">15m</button>
                <button data-tf="1h" class="on" type="button">1h</button>
                <button data-tf="4h" type="button">4h</button>
              </span>
              <button id="modal-close" type="button">关闭</button>
            </div>
          </div>
          <canvas id="chart" class="chart" width="960" height="380"></canvas>
          <div id="score-summary" class="score-summary"></div>
          <div id="report" class="report"></div>
          <div class="factor-title">因子明细（当前周期）</div>
          <div id="factors" class="factors"></div>
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
    await Promise.allSettled([this.pollOverview(), this.pollAlerts(), this.pollNews()]);
  }

  bindEvents() {
    ["search", "filter", "sort"].forEach((id) => {
      this.q(`#${id}`).addEventListener("input", () => this.renderGrid());
    });
    this.q("#btn-refresh").addEventListener("click", () => this.refreshAll());
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
    this.qa(".tf-switch button").forEach((button) => {
      button.addEventListener("click", () => {
        this.state.modal.tf = button.dataset.tf;
        this.refreshModal();
      });
    });
    this.q("#modal-watch").addEventListener("click", () => this.toggleWatchlist());
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

  showError(message = "") {
    const banner = this.q("#error-banner");
    banner.textContent = message;
    banner.classList.toggle("hidden", !message);
  }

  renderGrid() {
    const keyword = this.q("#search").value.trim().toUpperCase();
    const filter = this.q("#filter").value;
    const sort = this.q("#sort").value;
    let items = this.state.overview.filter((item) => !keyword || item.symbol.includes(keyword));
    if (filter === "mine") items = items.filter((item) => item.watch);
    if (filter === "long") items = items.filter((item) => item.score != null && item.score >= 60);
    if (filter === "short") items = items.filter((item) => item.score != null && item.score <= -60);
    items.sort((left, right) => {
      if (left.watch !== right.watch) return right.watch ? 1 : -1;
      if (sort === "pct") return (right.pct_24h ?? -999) - (left.pct_24h ?? -999);
      if (sort === "alpha") return left.symbol.localeCompare(right.symbol);
      return Math.abs(right.score ?? 0) - Math.abs(left.score ?? 0);
    });
    this.q("#sym-count").textContent = `${items.length}/${this.state.overview.length}`;
    this.q("#contract-grid").innerHTML = items.map((item) => {
      const score = item.score;
      const alertClass = score >= 60 ? "alert-long" : score <= -60 ? "alert-short" : "";
      const pctClass = item.pct_24h == null ? "dim" : item.pct_24h > 0 ? "up" : item.pct_24h < 0 ? "down" : "flat";
      const tags = `${item.watch ? "★" : ""}${item.trending ? "🔥" : ""}`;
      return `<article class="contract-card ${alertClass}" data-symbol="${this.escape(item.symbol)}">
        <div class="symbol">${this.escape(item.symbol.replace("USDT", ""))}<span class="tags">${tags}</span></div>
        <div class="signal ${this.signalTone(score)}">${this.signalLabel(score)}</div>
        <div class="price">${this.formatPrice(item.price)}</div>
        <div class="pct ${pctClass}">${this.formatPercent(item.pct_24h)} ${score == null ? "" : `<span class="score ${this.signalTone(score)}">(${score > 0 ? "+" : ""}${score})</span>`}</div>
        <div class="scorebar"><i data-score="${score ?? ""}"></i></div>
      </article>`;
    }).join("") || '<div class="empty">没有符合条件的合约</div>';
    this.qa(".contract-card").forEach((card) => card.addEventListener("click", () => this.openModal(card.dataset.symbol)));
    this.qa(".scorebar i").forEach((bar) => {
      const score = Number(bar.dataset.score);
      if (!bar.dataset.score) return;
      const width = Math.min(Math.abs(score), 100) / 2;
      bar.style.width = `${width}%`;
      bar.style.background = score >= 0 ? "#2ebd85" : "#f6465d";
      if (score >= 0) bar.style.left = "50%";
      else bar.style.right = "50%";
    });
  }

  async pollOverview() {
    try {
      const [overview, breadth, watchlist] = await Promise.all([
        this.api("/overview"), this.api("/breadth"), this.api("/watchlist"),
      ]);
      this.state.overview = overview.items;
      this.state.watchlist = new Set(watchlist);
      this.state.overview.forEach((item) => { item.watch = this.state.watchlist.has(item.symbol); });
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
        const sentimentText = { bull: "利好", bear: "利空", neutral: "中性" }[item.sentiment] || "";
        return `<article class="news-item">
          <span class="source">${this.escape(item.source)}</span><span class="time">${this.timeString(item.ts)}</span>
          <span class="sentiment ${this.escape(item.sentiment)}">${sentimentText}</span>
          <div class="news-title"><a href="${this.safeUrl(item.link)}" target="_blank" rel="noopener noreferrer">${this.escape(item.title_zh || item.title)}</a></div>
          ${item.title_zh && item.lang === "en" ? `<div class="time">${this.escape(item.title)}</div>` : ""}
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
    this.q("#modal").classList.remove("hidden");
    await this.refreshModal();
  }

  closeModal() {
    this.q("#modal").classList.add("hidden");
  }

  async refreshModal() {
    const symbol = this.state.modal.symbol;
    const timeframe = this.state.modal.tf;
    if (!symbol) return;
    const overview = this.state.overview.find((item) => item.symbol === symbol) || {};
    this.q("#modal-symbol").textContent = symbol;
    this.q("#modal-price").textContent = this.formatPrice(overview.price);
    this.q("#modal-pct").textContent = this.formatPercent(overview.pct_24h);
    this.q("#modal-pct").className = overview.pct_24h > 0 ? "up" : overview.pct_24h < 0 ? "down" : "flat";
    this.q("#modal-watch").textContent = overview.watch ? "★" : "☆";
    this.q("#modal-watch").classList.toggle("on", Boolean(overview.watch));
    this.qa(".tf-switch button").forEach((button) => button.classList.toggle("on", button.dataset.tf === timeframe));
    try {
      const encoded = encodeURIComponent(symbol);
      const [klines, scores, report] = await Promise.all([
        this.api(`/klines?symbol=${encoded}&tf=${timeframe}&limit=120`),
        this.api(`/score?symbol=${encoded}`),
        this.api(`/report?symbol=${encoded}`),
      ]);
      this.drawChart(this.q("#chart"), klines);
      this.renderScoreSummary(scores, report);
      this.renderReport(report);
      this.renderFactors(scores[timeframe]);
    } catch (error) {
      this.q("#report").innerHTML = `<div class="error-banner">${this.escape(error.message || "详情加载失败")}</div>`;
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
        <span class="sentiment ${this.escape(item.sentiment)}">${({ bull: "利好", bear: "利空", neutral: "中性" })[item.sentiment] || ""}</span>
      </li>`).join("") || '<li class="dim">近 48 小时无相关资讯</li>';
      return `<article class="horizon ${border}">
        <div class="horizon-head"><span>${this.escape(horizon.name)}</span><strong class="score ${this.tone(horizon.score)}">${this.escape(horizon.suggestion)}</strong>${horizon.score == null ? "" : `<span class="dim">周期分 ${horizon.score > 0 ? "+" : ""}${horizon.score}</span>`}</div>
        ${levels}
        <div class="horizon-section"><div class="section-title">理论依据</div><ul>${basis}</ul></div>
        <div class="horizon-section"><div class="section-title">新闻支撑</div><ul>${news}</ul></div>
      </article>`;
    }).join("");
    this.q("#report").innerHTML = `${horizons}<div class="disclaimer">${this.escape(report.disclaimer)}</div>`;
  }

  renderFactors(current) {
    this.q("#factors").innerHTML = current ? current.factors.map((factor) => {
      const contribution = factor.weight === 0 ? "参考" : factor.contribution > 0 ? `+${factor.contribution}` : `${factor.contribution}`;
      return `<article class="factor"><strong>${this.escape(factor.name)}</strong><strong class="score ${this.tone(factor.contribution)}">${contribution}</strong><span>${this.escape(factor.reason)} <small class="dim">（${factor.weight === 0 ? "信息项" : `权重 ${factor.weight}`}）</small></span></article>`;
    }).join("") : '<div class="empty">该周期评分尚未生成</div>';
  }

  async toggleWatchlist() {
    const symbol = this.state.modal.symbol;
    if (!symbol) return;
    if (this.state.watchlist.has(symbol)) this.state.watchlist.delete(symbol);
    else this.state.watchlist.add(symbol);
    await this.api("/watchlist", {
      method: "PUT",
      body: JSON.stringify({ symbols: [...this.state.watchlist] }),
    });
    await this.pollOverview();
    const selected = this.state.overview.find((item) => item.symbol === symbol);
    this.q("#modal-watch").textContent = selected?.watch ? "★" : "☆";
    this.q("#modal-watch").classList.toggle("on", Boolean(selected?.watch));
  }

  drawChart(canvas, klines) {
    const context = canvas.getContext("2d");
    const width = canvas.width;
    const height = canvas.height;
    context.clearRect(0, 0, width, height);
    if (!klines.length) {
      context.fillStyle = "#77808f";
      context.fillText("数据加载中…", 20, 30);
      return;
    }
    const padding = { left: 10, right: 70, top: 10, bottom: 46 };
    const volumeHeight = 36;
    const priceHeight = height - padding.top - padding.bottom - volumeHeight;
    const high = Math.max(...klines.map((item) => item.high));
    const low = Math.min(...klines.map((item) => item.low));
    const range = high - low || 1;
    const x = (index) => padding.left + (index + 0.5) * (width - padding.left - padding.right) / klines.length;
    const y = (value) => padding.top + (high - value) / range * priceHeight;
    context.strokeStyle = "#1e2530";
    context.fillStyle = "#77808f";
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
    movingAverage(20, "#f0b90b");
    movingAverage(50, "#b37feb");
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
