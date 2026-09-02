const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => document.querySelectorAll(selector);
const API_ROOT = "/api/v2/admin";
const VIEWS = {
  overview: ["OPERATIONS / 01", "运行总览"],
  collectors: ["OPERATIONS / 02", "采集器"],
  "market-data": ["OPERATIONS / 03", "市场数据与信号门控"],
  "stock-library": ["TRADFI MASTER / 04", "证券资料库"],
  alerts: ["SIGNALS / 05", "提醒事件"],
  rules: ["SIGNALS / 06", "信号规则"],
  sources: ["INTELLIGENCE / 07", "舆情来源"],
  news: ["INTELLIGENCE / 08", "采集新闻"],
  symbols: ["MARKET DATA / 09", "合约数据"],
  "ai-model": ["GOVERNANCE / 10", "全局 AI 模型"],
  users: ["GOVERNANCE / 11", "用户权限"],
  storage: ["GOVERNANCE / 12", "存储维护"],
  audit: ["GOVERNANCE / 13", "审计日志"],
};

let accessToken = "";
let adminUser = null;
let activeView = "overview";
let refreshPromise = null;
let toastTimer = null;
let cleanupPayload = null;
let newsSources = [];
let newsAiPollTimer = null;
let stockDetailTrigger = null;

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (character) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", "\"": "&quot;", "'": "&#39;",
  })[character]);
}

function safeLink(value) {
  try {
    const url = new URL(String(value || ""));
    return ["http:", "https:"].includes(url.protocol) ? escapeHtml(url.href) : "#";
  } catch (_) {
    return "#";
  }
}

function formatTime(value, milliseconds = false) {
  if (!value) return "--";
  let date;
  if (typeof value === "string" && !/^\d+$/.test(value)) {
    date = new Date(value.endsWith("Z") || /[+-]\d\d:\d\d$/.test(value) ? value : `${value}Z`);
  } else {
    const number = Number(value);
    date = new Date(milliseconds || number > 1e12 ? number : number * 1000);
  }
  return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString("zh-CN", { hour12: false });
}

function compactNumber(value) {
  const number = Number(value || 0);
  return new Intl.NumberFormat("zh-CN", { notation: "compact", maximumFractionDigits: 1 }).format(number);
}

function formatPrice(value) {
  const number = Number(value);
  if (!Number.isFinite(number)) return "--";
  return number.toLocaleString("en-US", { maximumFractionDigits: number < 10 ? 4 : 2 });
}

function objectSummary(value) {
  if (!value || typeof value !== "object" || !Object.keys(value).length) return "--";
  return Object.entries(value).map(([key, count]) => `${escapeHtml(key)} ${compactNumber(count)}`).join(" · ");
}

function sentimentView(value) {
  return {
    bull: { label: "看多", className: "bull" },
    bear: { label: "看空", className: "bear" },
    neutral: { label: "中性", className: "neutral" },
  }[value] || { label: value || "未知", className: "neutral" };
}

function newsAiBatchStatus(value) {
  return {
    pending: { label: "等待执行", className: "warning" },
    running: { label: "分析中", className: "warning" },
    completed: { label: "分析完成", className: "ok" },
    partial: { label: "部分完成", className: "warning" },
    failed: { label: "分析失败", className: "error" },
  }[value] || { label: "尚未运行", className: "" };
}

function renderNewsAiBatch(data) {
  const batch = data.items?.[0];
  const statusBox = $("#news-ai-status");
  const progress = $("#news-ai-progress");
  const conclusion = $("#news-ai-conclusion");
  const buttons = $$('[data-news-ai-count]');
  const retryButton = $("#news-ai-retry");
  if (!batch) {
    statusBox.innerHTML = '<span class="pill">尚未运行</span><small>请选择最近 300 或 500 条新闻开始分析</small>';
    progress.classList.add("hidden");
    conclusion.classList.add("hidden");
    buttons.forEach((button) => { button.disabled = false; });
    retryButton.classList.add("hidden");
    scheduleNewsAiPoll(false);
    return;
  }
  const status = newsAiBatchStatus(batch.status);
  const active = ["pending", "running"].includes(batch.status);
  retryButton.dataset.batchId = batch.id;
  retryButton.classList.toggle(
    "hidden",
    !["failed", "partial"].includes(batch.status) || batch.processed_count >= batch.selected_count,
  );
  retryButton.disabled = active;
  const progressValue = Math.max(0, Math.min(1, Number(batch.progress || 0)));
  const model = [batch.provider_code, batch.model_name].filter(Boolean).join(" / ") || "等待载入默认模型";
  statusBox.innerHTML = `<span class="pill ${status.className}">${status.label}</span><small>${batch.processed_count} / ${batch.selected_count || batch.requested_count} 条 · 失败 ${batch.failed_count} 条<br>${escapeHtml(model)} · ${formatTime(batch.created_at)}</small>`;
  progress.classList.toggle("hidden", !active && progressValue === 0);
  progress.querySelector("i").style.width = `${Math.round(progressValue * 100)}%`;
  buttons.forEach((button) => { button.disabled = active; });
  if (batch.market_summary) {
    const market = sentimentView(batch.market_sentiment);
    const confidence = batch.market_confidence == null ? "--" : `${Math.round(Number(batch.market_confidence) * 100)}%`;
    const drivers = Array.isArray(batch.result?.key_drivers) ? batch.result.key_drivers.slice(0, 5).join("；") : "";
    const focus = Array.isArray(batch.result?.focus_stocks) ? batch.result.focus_stocks.slice(0, 12).map((item) => item.symbol).filter(Boolean).join(" · ") : "";
    conclusion.innerHTML = `<strong>${escapeHtml(market.label)} · 置信度 ${confidence}</strong><p>${escapeHtml(batch.market_summary)}</p><small>${drivers ? `关键驱动：${escapeHtml(drivers)}` : ""}${drivers && focus ? "<br>" : ""}${focus ? `重点美股：${escapeHtml(focus)}` : ""}</small>`;
    conclusion.classList.remove("hidden");
  } else if (batch.error_message && !active) {
    conclusion.innerHTML = `<strong>任务未完成</strong><p>${escapeHtml(batch.error_message)}</p>`;
    conclusion.classList.remove("hidden");
  } else {
    conclusion.classList.add("hidden");
  }
  scheduleNewsAiPoll(active);
}

function scheduleNewsAiPoll(active) {
  if (newsAiPollTimer) clearTimeout(newsAiPollTimer);
  newsAiPollTimer = null;
  if (!active) return;
  newsAiPollTimer = setTimeout(() => {
    if (activeView !== "news") return;
    loadNews().catch((error) => toast(error.message, "error"));
  }, 3000);
}

function errorMessage(payload, fallback = "请求失败") {
  const detail = payload?.detail;
  if (typeof detail === "string" && detail) return detail;
  if (Array.isArray(detail)) return detail.map((item) => item.msg || "参数无效").join("；");
  return fallback;
}

async function refreshAccess() {
  if (refreshPromise) return refreshPromise;
  refreshPromise = (async () => {
    try {
      const response = await fetch("/api/v2/auth/refresh", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: "{}",
        credentials: "include",
      });
      if (!response.ok) return false;
      const payload = await response.json();
      accessToken = String(payload.access_token || "");
      return Boolean(accessToken);
    } catch (_) {
      return false;
    }
  })();
  try {
    return await refreshPromise;
  } finally {
    refreshPromise = null;
  }
}

async function api(path, options = {}, retry = true) {
  const headers = new Headers(options.headers || {});
  if (options.body && !headers.has("Content-Type")) headers.set("Content-Type", "application/json");
  if (accessToken) {
    headers.set("Authorization", `Bearer ${accessToken}`);
    if (adminUser?.id) headers.set("X-QuantDesk-User-ID", String(adminUser.id));
  }
  const response = await fetch(path.startsWith("/api/") ? path : `${API_ROOT}${path}`, {
    ...options,
    headers,
    credentials: "include",
  });
  if (response.status === 401 && retry && !path.includes("/auth/")) {
    if (await refreshAccess()) return api(path, options, false);
    showLogin("管理会话已过期，请重新登录。");
  }
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(errorMessage(payload));
  return payload;
}

function setStatus(message, kind = "") {
  const target = $("#global-status");
  target.textContent = message;
  target.className = `status-chip ${kind}`.trim();
}

function toast(message, kind = "") {
  const target = $("#toast");
  clearTimeout(toastTimer);
  target.textContent = message;
  target.className = `toast ${kind}`.trim();
  toastTimer = setTimeout(() => target.classList.add("hidden"), 3600);
}

function showLogin(message = "") {
  accessToken = "";
  adminUser = null;
  $("#admin-boot").classList.add("hidden");
  $("#admin-shell").classList.add("hidden");
  $("#admin-login").classList.remove("hidden");
  const error = $("#admin-login-error");
  error.textContent = message;
  error.classList.toggle("hidden", !message);
  history.replaceState({}, "", "/admin/login");
}

function showShell(user) {
  adminUser = user;
  $("#admin-boot").classList.add("hidden");
  $("#admin-login").classList.add("hidden");
  $("#admin-shell").classList.remove("hidden");
  $("#admin-name").textContent = user.username;
  $("#admin-avatar").textContent = String(user.username || "A").slice(0, 1).toUpperCase();
  history.replaceState({}, "", `/admin#${activeView}`);
  navigate(location.hash.slice(1) || "overview", false);
}

async function verifyAdmin() {
  const user = await api("/api/v2/me");
  if (!user.is_admin) throw new Error("此账号没有管理员权限。");
  return user;
}

async function boot() {
  if (!(await refreshAccess())) {
    showLogin();
    return;
  }
  try {
    showShell(await verifyAdmin());
  } catch (error) {
    showLogin(error.message);
  }
}

function navigate(view, push = true) {
  if (!VIEWS[view]) view = "overview";
  activeView = view;
  $$('[data-view]').forEach((section) => section.classList.toggle("hidden", section.dataset.view !== view));
  $$('[data-view-target]').forEach((button) => button.classList.toggle("active", button.dataset.viewTarget === view));
  $("#view-kicker").textContent = VIEWS[view][0];
  $("#view-title").textContent = VIEWS[view][1];
  document.title = `${VIEWS[view][1]} · QuantDesk 管理中心`;
  if (push) history.pushState({}, "", `/admin#${view}`);
  loadView(view);
}

async function loadView(view = activeView) {
  setStatus("正在同步", "loading");
  $("#refresh-view").disabled = true;
  try {
    const loaders = {
      overview: loadOverview,
      collectors: loadCollectors,
      "market-data": loadUwMarketData,
      "stock-library": loadStockLibrary,
      alerts: loadAlerts,
      rules: loadRules,
      sources: loadSources,
      news: loadNews,
      symbols: loadSymbols,
      "ai-model": loadAiModel,
      users: loadUsers,
      storage: loadStorage,
      audit: loadAudit,
    };
    await loaders[view]();
    setStatus(`已更新 ${new Date().toLocaleTimeString("zh-CN", { hour12: false })}`, "ok");
  } catch (error) {
    setStatus("同步失败", "error");
    toast(error.message, "error");
  } finally {
    $("#refresh-view").disabled = false;
  }
}

async function loadOverview() {
  const [data, collectors] = await Promise.all([api("/overview"), api("/collectors")]);
  const health = $("#overview-health");
  health.textContent = data.health === "ok" ? "全部链路正常" : `${data.unhealthy_collectors} 个链路需关注`;
  health.className = `health-badge ${data.health}`;
  const metrics = [
    ["提醒总量", data.alerts.total, `${data.alerts.unread} 条未读`],
    ["舆情总量", data.news.total, `${data.news.sources} 个来源`],
    ["行情合约", data.ticker.total, data.ticker.lag_seconds == null ? "暂无行情" : `延迟 ${data.ticker.lag_seconds}s`],
    ["评分记录", data.scores.total, `最新 ${formatTime(data.scores.newest, true)}`],
    ["平台用户", data.users.total, `${data.users.active} 个启用`],
    ["24H 活动", data.last_24h.alerts + data.last_24h.news, `${data.last_24h.alerts} 信号 / ${data.last_24h.news} 舆情`],
  ];
  $("#overview-metrics").innerHTML = metrics.map(([label, value, hint]) => `<article class="metric-card"><span>${escapeHtml(label)}</span><strong>${compactNumber(value)}</strong><small>${escapeHtml(hint)}</small></article>`).join("");
  $("#overview-collectors").innerHTML = collectors.map((item) => `<div class="compact-row"><strong>${escapeHtml(item.name)}</strong><span class="pill ${escapeHtml(item.health)}">${escapeHtml(item.health)}</span><small>${item.heartbeat_at ? `${item.lag_seconds}s 前心跳` : "尚无心跳"}</small><span>${compactNumber(item.items)} 条</span></div>`).join("");
  const activity = [
    ["提醒事件", data.last_24h.alerts, objectSummary(data.last_24h.alert_kinds)],
    ["舆情内容", data.last_24h.news, objectSummary(data.last_24h.news_sentiment)],
    ["信号方向", Object.values(data.last_24h.alert_directions).reduce((sum, item) => sum + item, 0), objectSummary(data.last_24h.alert_directions)],
    ["异常采集器", data.unhealthy_collectors, data.health === "ok" ? "运行稳定" : "建议检查心跳与错误"],
  ];
  $("#overview-activity").innerHTML = activity.map(([label, value, hint]) => `<div class="activity-card"><span>${escapeHtml(label)}</span><strong>${compactNumber(value)}</strong><small>${hint}</small></div>`).join("");
}

async function loadStockLibrary() {
  const data = await api(`/stock-library?${formQuery($("#stock-library-filter"))}`);
  $("#stock-library-total").textContent = `${data.total} 个标的`;
  const binance = data.binance || {};
  $("#stock-library-status").textContent = `Binance 在交易 ${binance.trading || 0}/${binance.total || 0} · 已核验 ${data.verified} · 待复核 ${data.review_required} · 待补充 ${data.pending || 0}`;
  $("#stock-library-table").innerHTML = data.items.length ? data.items.map((item) => {
    const profile = item.profile || {};
    const analysis = item.analysis || {};
    const mapping = (item.mappings || []).find((row) => row.source === "binance_tradfi") || {};
    const marketCap = profile.market_cap == null
      ? (item.security_type === "ETF" ? "基金规模待补充" : item.security_type === "PRE_IPO" ? "未上市" : "市值待补充")
      : compactNumber(profile.market_cap * 1000000);
    const isBaseline = analysis.analysis_version === "baseline-v1";
    const score = analysis.overall_score == null ? (isBaseline ? "" : "财务分析待生成") : `${Number(analysis.overall_score).toFixed(1)} 分`;
    const verified = ["VERIFIED", "AUTO_VERIFIED"].includes(item.verification_status);
    const verifyClass = item.verification_status === "REVIEW_REQUIRED" ? "warning" : verified ? "active" : "pending";
    const verifyLabel = item.verification_status === "REVIEW_REQUIRED" ? "需要复核" : verified ? "已核验" : "待补充";
    const chineseName = item.company_name_zh || profile.legal_name || item.company_name || "待同步";
    const englishName = item.company_name_zh ? (profile.legal_name || item.company_name || "") : "";
    const chineseIndustry = [profile.sector_zh, profile.industry_zh].filter(Boolean).join(" · ");
    const englishIndustry = [profile.sector, profile.industry].filter(Boolean).join(" · ");
    const analysisSummary = analysis.business_summary || (profile.source ? "基础资料已同步，等待生成分析摘要" : "基础资料尚未同步");
    const contract = mapping.source_symbol || "未关联合约";
    const sourceStatus = mapping.source_status || "UNKNOWN";
    const gate = mapping.live_trading_enabled ? "监控 / 策略 / 实盘" : mapping.strategy_enabled ? "监控 / 策略" : mapping.monitor_enabled ? "仅监控" : "已停用";
    const syncAction = item.profile_sync_supported
      ? `<button data-stock-sync="${escapeHtml(item.symbol)}">同步资料</button>`
      : '<span class="pill">无需 Finnhub</span>';
    return `<tr><td><strong>${escapeHtml(item.symbol)}</strong><small>${escapeHtml(item.exchange)} · ${escapeHtml(contract)}</small></td><td><button class="stock-detail-trigger" type="button" data-stock-detail="${escapeHtml(item.symbol)}" aria-label="查看 ${escapeHtml(chineseName)} 详情"><strong>${escapeHtml(chineseName)}</strong></button><small>${escapeHtml(chineseIndustry || "暂无中文行业资料")}</small>${englishName ? `<small>${escapeHtml(englishName)}${englishIndustry ? ` · ${escapeHtml(englishIndustry)}` : ""}</small>` : ""}</td><td><span class="pill">${escapeHtml(item.security_type)}</span><small>${escapeHtml(mapping.underlying_type || "-")} · ${escapeHtml(gate)}</small></td><td><span class="pill ${verifyClass}">${verifyLabel}</span><small>Binance ${escapeHtml(sourceStatus)}</small></td><td>${marketCap}</td><td>${score ? `<strong>${escapeHtml(score)}</strong>` : ""}<small>${escapeHtml(analysisSummary)}</small></td><td>${formatTime(item.updated_at)}</td><td>${syncAction}</td></tr>`;
  }).join("") : '<tr><td class="empty" colspan="8">资料库为空，请点击“同步 Binance 合约”。</td></tr>';
}

function stockDetailVerification(status) {
  return {
    VERIFIED: { label: "已核验", className: "active" },
    AUTO_VERIFIED: { label: "已核验", className: "active" },
    REVIEW_REQUIRED: { label: "需要复核", className: "warning" },
  }[status] || { label: "待补充", className: "pending" };
}

function stockDetailEvidence(value) {
  if (!value || typeof value !== "object") return '<p class="stock-detail-empty">暂无结构化证据。</p>';
  const entries = Object.entries(value).slice(0, 16);
  if (!entries.length) return '<p class="stock-detail-empty">暂无结构化证据。</p>';
  return `<dl class="stock-evidence-list">${entries.map(([key, item]) => {
    const content = typeof item === "string" ? item : JSON.stringify(item);
    return `<div><dt>${escapeHtml(key)}</dt><dd>${escapeHtml(content)}</dd></div>`;
  }).join("")}</dl>`;
}

function renderStockDetail(data) {
  const profile = data.profile || {};
  const analysis = data.analysis || {};
  const mappings = data.mappings || [];
  const sources = data.research_sources || [];
  const verification = stockDetailVerification(data.verification_status);
  const displayName = data.company_name_zh || profile.legal_name || data.company_name || data.symbol;
  const englishName = data.company_name_zh ? (profile.legal_name || data.company_name || "") : "";
  const industries = [profile.sector_zh, profile.industry_zh].filter(Boolean).join(" · ")
    || [profile.sector, profile.industry].filter(Boolean).join(" · ")
    || "暂无行业资料";
  const marketCap = profile.market_cap == null ? "待补充" : compactNumber(Number(profile.market_cap) * 1000000);
  const website = safeLink(profile.website);
  const score = analysis.overall_score == null ? "--" : Number(analysis.overall_score).toFixed(1);
  const confidence = analysis.confidence_score == null ? "--" : `${(Number(analysis.confidence_score) * 100).toFixed(1)}%`;
  $("#stock-detail-title").textContent = `${data.symbol} · ${displayName}`;
  $("#stock-detail-subtitle").textContent = [englishName, industries].filter(Boolean).join(" · ");
  $("#stock-detail-body").innerHTML = `
    <section class="stock-detail-summary">
      <article><span>证券类型</span><strong>${escapeHtml(data.security_type || "--")}</strong><small>${escapeHtml(data.exchange || "--")} · ${escapeHtml(data.country || "--")}</small></article>
      <article><span>核验状态</span><strong><i class="pill ${verification.className}">${verification.label}</i></strong><small>${data.is_active ? "主数据启用中" : "主数据已停用"}</small></article>
      <article><span>市值 / 基金规模</span><strong>${escapeHtml(marketCap)}</strong><small>资料来源 ${escapeHtml(profile.source || "--")}</small></article>
      <article><span>基础面评分</span><strong>${escapeHtml(score)}</strong><small>置信度 ${escapeHtml(confidence)}</small></article>
    </section>
    <section class="stock-detail-grid">
      <article class="stock-detail-card">
        <header><span>COMPANY PROFILE</span><h3>公司资料</h3></header>
        <dl class="stock-detail-fields">
          <div><dt>证券代码</dt><dd>${escapeHtml(data.symbol || "--")}</dd></div>
          <div><dt>CIK</dt><dd>${escapeHtml(data.cik || "--")}</dd></div>
          <div><dt>行业</dt><dd>${escapeHtml(industries)}</dd></div>
          <div><dt>资料更新时间</dt><dd>${formatTime(profile.source_updated_at || data.updated_at)}</dd></div>
          <div class="full"><dt>官方网站</dt><dd>${website === "#" ? "--" : `<a href="${website}" target="_blank" rel="noopener noreferrer">${escapeHtml(profile.website)}</a>`}</dd></div>
        </dl>
      </article>
      <article class="stock-detail-card">
        <header><span>BINANCE CONTRACT MAP</span><h3>合约关联</h3></header>
        <div class="stock-mapping-list">${mappings.length ? mappings.map((mapping) => `
          <div>
            <strong>${escapeHtml(mapping.source_symbol || mapping.normalized_symbol || "--")}</strong>
            <span class="pill ${mapping.source_status === "TRADING" ? "active" : "warning"}">${escapeHtml(mapping.source_status || "UNKNOWN")}</span>
            <small>${escapeHtml(mapping.source || "--")} · ${escapeHtml(mapping.contract_type || "--")} · ${escapeHtml(mapping.underlying_type || "--")}</small>
            <small>监控 ${mapping.monitor_enabled ? "开" : "关"} · 策略 ${mapping.strategy_enabled ? "开" : "关"} · 实盘 ${mapping.live_trading_enabled ? "开" : "关"}</small>
          </div>`).join("") : '<p class="stock-detail-empty">尚未关联外部合约。</p>'}</div>
      </article>
      <article class="stock-detail-card stock-detail-wide">
        <header><span>FUNDAMENTAL ANALYSIS</span><h3>基础面分析</h3><small>${escapeHtml(analysis.analysis_version || "尚未生成")} · ${escapeHtml(analysis.as_of_date || "--")}</small></header>
        <div class="stock-analysis-copy"><div><h4>业务摘要</h4><p>${escapeHtml(analysis.business_summary || "暂无业务分析摘要。")}</p></div><div><h4>风险分析</h4><p>${escapeHtml(analysis.risk_analysis || "暂无风险分析。")}</p></div></div>
        ${stockDetailEvidence(analysis.evidence)}
      </article>
      <article class="stock-detail-card stock-detail-wide">
        <header><span>RESEARCH SOURCES</span><h3>研究来源</h3><small>${sources.length} 条</small></header>
        <div class="stock-research-list">${sources.length ? sources.map((source) => {
          const url = safeLink(source.url);
          const title = escapeHtml(source.title || "未命名资料");
          return `<article><div>${url === "#" ? `<strong>${title}</strong>` : `<a href="${url}" target="_blank" rel="noopener noreferrer">${title}</a>`}<small>${escapeHtml(source.publisher || source.source_type || "--")} · ${formatTime(source.published_at)}</small></div><p>${escapeHtml(source.content_summary || "暂无摘要")}</p></article>`;
        }).join("") : '<p class="stock-detail-empty">暂无研究来源。</p>'}</div>
      </article>
    </section>`;
}

async function openStockDetail(symbol, trigger) {
  const dialog = $("#stock-detail-dialog");
  stockDetailTrigger = trigger || document.activeElement;
  $("#stock-detail-title").textContent = `${symbol} · 正在加载`;
  $("#stock-detail-subtitle").textContent = "正在读取证券主数据与基础面资料";
  $("#stock-detail-body").innerHTML = '<div class="stock-detail-loading" role="status">正在加载详情…</div>';
  if (!dialog.open) dialog.showModal();
  try {
    renderStockDetail(await api(`/stock-library/${encodeURIComponent(symbol)}`));
  } catch (error) {
    $("#stock-detail-title").textContent = `${symbol} · 加载失败`;
    $("#stock-detail-body").innerHTML = `<div class="stock-detail-error" role="alert"><strong>暂时无法读取证券详情</strong><p>${escapeHtml(error.message)}</p><button type="button" data-stock-detail-retry="${escapeHtml(symbol)}">重新加载</button></div>`;
  }
}

async function importStockLibrary() {
  const result = await api("/stock-library/import", { method: "POST", body: "{}" });
  toast(`同步完成：Binance ${result.remote_trading}/${result.remote_total}，新合约 ${result.new_contracts}，新增主数据 ${result.created}，待补资料 ${result.pending_profiles}`);
  await loadStockLibrary();
}

async function syncStockProfile(symbol) {
  await api(`/stock-library/${encodeURIComponent(symbol)}/sync`, { method: "POST", body: "{}" });
  toast(`${symbol} 公司资料同步完成`);
  await loadStockLibrary();
}

async function loadCollectors() {
  const items = await api("/collectors");
  $("#collectors-table").innerHTML = items.length ? items.map((item) => {
    const details = Object.entries(item.details || {}).map(([key, value]) => `${key}: ${value}`).join(" · ");
    return `<tr><td><strong>${escapeHtml(item.name)}</strong><small>${item.paused ? "管理员已暂停" : "自动运行"}</small></td><td><span class="pill ${escapeHtml(item.health)}">${escapeHtml(item.health)}</span></td><td>${item.heartbeat_at ? `${item.lag_seconds}s` : "--"}<small>${formatTime(item.heartbeat_at)}</small></td><td>${compactNumber(item.cycles)}</td><td>${compactNumber(item.items)}</td><td class="truncate" title="${escapeHtml(details)}">${escapeHtml(details || "--")}</td><td class="truncate" title="${escapeHtml(item.last_error || "")}">${escapeHtml(item.last_error || "--")}</td><td><div class="button-group"><button data-collector="${escapeHtml(item.name)}" data-action="${item.paused ? "resume" : "pause"}">${item.paused ? "恢复" : "暂停"}</button></div></td></tr>`;
  }).join("") : '<tr><td class="empty" colspan="8">暂无采集器状态</td></tr>';
}

const UW_WEIGHT_FIELDS = ["news", "technical", "market_context", "options_flow", "gex", "institutional_flow"];
let uwRetentionConfig = null;

function uwStreamStatusView(health) {
  if (health?.connected) return { label: "实时连接", className: "ok" };
  if (["connecting", "reconnecting"].includes(health?.status)) return { label: "正在重连", className: "warning" };
  if (health?.status === "disabled") return { label: "未启动", className: "" };
  return { label: "连接中断", className: "error" };
}

async function loadUwMarketData() {
  const data = await api("/market-data/unusual-whales");
  const config = data.config || {};
  uwRetentionConfig = config.retention && typeof config.retention === "object" ? { ...config.retention } : null;
  const health = data.health || {};
  const stream = health.websocket || {};
  const streamView = uwStreamStatusView(stream);
  const state = $("#uw-market-state");
  state.textContent = !config.enabled ? "平台已关闭" : !data.configured ? "凭据未配置" : config.websocket_enabled ? streamView.label : "WebSocket 已关闭";
  state.className = `health-badge ${!data.configured ? "error" : streamView.className}`;

  const subscriptions = Array.isArray(stream.subscriptions) ? stream.subscriptions.length : 0;
  const lastEvent = stream.last_event_at_ms ? formatTime(stream.last_event_at_ms, true) : "尚无事件";
  const metrics = [
    ["API 凭据", data.configured ? "已配置" : "缺失", data.api_key_fingerprint || "未保存"],
    ["运行模式", { record: "只记录", score: "参与评分", gate: "硬门控" }[config.mode] || config.mode, `配置版本 ${data.version || 0}`],
    ["WebSocket", streamView.label, `${subscriptions} 个订阅`],
    ["最后事件", lastEvent, `接收 ${compactNumber(stream.received || 0)} · 去重 ${compactNumber(stream.duplicates || 0)}`],
    ["数据版本", data.feature_version || "--", data.decision_version || "--"],
    ["错误状态", stream.last_error ? "需处理" : "正常", stream.last_error || "未发现连接错误"],
  ];
  const leadership = health.leadership || {};
  const retention = health.retention || {};
  metrics.push(
    ["采集主实例", leadership.is_leader ? "当前主实例" : "热备待命", `${leadership.mode || "--"} · 接管 ≤ ${leadership.standby_takeover_seconds ?? "--"}s`],
    ["分级保留", retention.status || "pending", `原始 ${retention.raw_event_days ?? "--"} 天 · 特征 ${retention.feature_snapshot_days ?? "--"} 天 · 待清理 ${compactNumber((retention.event_backlog || 0) + (retention.feature_backlog || 0))}`],
  );
  $("#uw-health-grid").innerHTML = metrics.map(([label, value, hint]) => `<article><span>${escapeHtml(label)}</span><strong>${escapeHtml(value ?? "--")}</strong><small title="${escapeHtml(hint)}">${escapeHtml(hint)}</small></article>`).join("");

  const form = $("#uw-market-data-form");
  form.elements.namedItem("api_key").value = "";
  form.elements.namedItem("enabled").checked = config.enabled !== false;
  form.elements.namedItem("mode").value = config.mode || "record";
  form.elements.namedItem("rest_enabled").checked = Boolean(config.rest_enabled);
  form.elements.namedItem("websocket_enabled").checked = Boolean(config.websocket_enabled);
  form.querySelectorAll("[data-uw-channel]").forEach((input) => {
    input.checked = Boolean(config.channels?.[input.dataset.uwChannel]);
  });
  Object.entries(config.thresholds || {}).forEach(([key, value]) => {
    const input = form.elements.namedItem(key);
    if (input) input.value = value;
  });
  UW_WEIGHT_FIELDS.forEach((key) => {
    const input = form.elements.namedItem(`weight_${key}`);
    if (input) input.value = Math.round(Number(config.weights?.[key] || 0) * 1000) / 10;
  });
  $("#uw-market-message").textContent = `${data.weights_version || "--"} · ${data.credential_source === "database" ? "数据库加密凭据" : "环境变量兼容凭据"}`;

  const channelState = health.channels || {};
  const channelNames = Object.keys(config.channels || {});
  const activeCount = channelNames.filter((name) => config.channels[name]).length;
  $("#uw-channel-summary").textContent = `${activeCount} / ${channelNames.length} 个频道启用`;
  $("#uw-channel-health").innerHTML = channelNames.map((name) => {
    const item = channelState[name] || {};
    const enabled = Boolean(config.channels[name]);
    const fresh = item.status === "live" || item.fresh === true;
    const status = !enabled ? "已关闭" : fresh ? "实时" : stream.connected ? "等待首条数据" : "连接不可用";
    const statusClass = !enabled ? "" : fresh ? "ok" : "warning";
    const eventTime = item.last_event_at_ms || item.last_event_time_ms;
    const lag = item.lag_ms == null ? "--" : `${Math.round(Number(item.lag_ms))} ms`;
    return `<div><strong>${escapeHtml(name)}</strong><span class="pill ${statusClass}">${escapeHtml(status)}</span><small>最后事件 ${eventTime ? formatTime(eventTime, true) : "--"}</small><small>延迟 ${escapeHtml(lag)}</small><small>${compactNumber(item.received || 0)} 条</small></div>`;
  }).join("") || '<p class="empty">尚未配置实时频道</p>';
}

function unusualWhalesFormPayload(form) {
  const weights = Object.fromEntries(UW_WEIGHT_FIELDS.map((key) => [key, Number(form.elements.namedItem(`weight_${key}`).value) / 100]));
  const weightTotal = Object.values(weights).reduce((sum, value) => sum + value, 0);
  if (Math.abs(weightTotal - 1) > 0.000001) throw new Error(`评分域权重当前合计 ${(weightTotal * 100).toFixed(1)}%，必须为 100%`);
  const thresholds = {};
  ["quote_age_regular_ms", "quote_age_extended_ms", "spread_hard_max_bps", "source_divergence_max_bps", "min_data_coverage", "event_block_before_minutes", "event_block_after_minutes", "halt_cooldown_minutes"].forEach((key) => {
    thresholds[key] = Number(form.elements.namedItem(key).value);
  });
  const payload = {
    enabled: form.elements.namedItem("enabled").checked,
    mode: form.elements.namedItem("mode").value,
    rest_enabled: form.elements.namedItem("rest_enabled").checked,
    websocket_enabled: form.elements.namedItem("websocket_enabled").checked,
    channels: Object.fromEntries([...form.querySelectorAll("[data-uw-channel]")].map((input) => [input.dataset.uwChannel, input.checked])),
    thresholds,
    weights,
    ...(uwRetentionConfig ? { retention: { ...uwRetentionConfig } } : {}),
  };
  const apiKey = String(form.elements.namedItem("api_key").value || "").trim();
  if (apiKey) payload.api_key = apiKey;
  return payload;
}

async function saveUwMarketData(event) {
  event.preventDefault();
  const form = event.currentTarget;
  if (!form.reportValidity()) return;
  const submit = form.querySelector('button[type="submit"]');
  submit.disabled = true;
  try {
    const payload = unusualWhalesFormPayload(form);
    await api("/market-data/unusual-whales", { method: "PUT", body: JSON.stringify(payload) });
    form.elements.namedItem("api_key").value = "";
    toast("市场数据与信号门控配置已发布");
    await loadUwMarketData();
  } finally {
    submit.disabled = false;
  }
}

async function testUwMarketData() {
  const button = $("#uw-market-test");
  button.disabled = true;
  try {
    const result = await api("/market-data/unusual-whales/test", { method: "POST", body: "{}" });
    toast(result.message || "Unusual Whales REST 连接正常");
  } finally {
    button.disabled = false;
  }
}

async function fallbackUwToRecordOnly() {
  const form = $("#uw-market-data-form");
  form.elements.namedItem("mode").value = "record";
  form.requestSubmit();
}

function formQuery(form) {
  const params = new URLSearchParams();
  new FormData(form).forEach((value, key) => { if (String(value).trim()) params.set(key, String(value).trim()); });
  return params.toString();
}

async function loadAlerts() {
  const query = formQuery($("#alerts-filter"));
  const data = await api(`/alerts?limit=100&${query}`);
  $("#alerts-total").textContent = `${data.total} 条`;
  $("#alerts-table").innerHTML = data.items.length ? data.items.map((item) => `<tr><td>${formatTime(item.ts)}</td><td><strong>${escapeHtml(item.username || `#${item.user_id}`)}</strong></td><td><strong>${escapeHtml(item.symbol)}</strong></td><td><span class="pill">${escapeHtml(item.kind)}</span></td><td><span class="pill ${escapeHtml(item.direction)}">${escapeHtml(item.direction)}</span></td><td>${item.score ?? "--"}</td><td class="truncate" title="${escapeHtml(item.message)}">${escapeHtml(item.message)}</td><td><span class="pill ${item.read ? "" : "unread"}">${item.read ? "已读" : "未读"}</span></td></tr>`).join("") : '<tr><td class="empty" colspan="8">没有符合条件的提醒事件</td></tr>';
}

async function loadRules() {
  const data = await api("/alert-rules");
  const form = $("#rules-form");
  Object.entries(data.rules).forEach(([key, value]) => {
    const input = form.elements.namedItem(key);
    if (!input || key === "enabled_timeframes") return;
    if (input.type === "checkbox") input.checked = Boolean(value);
    else input.value = value;
  });
  form.querySelectorAll('[name="tf"]').forEach((input) => { input.checked = data.rules.enabled_timeframes.includes(input.value); });
  $("#rules-version").textContent = `VERSION ${data.version}`;
}

async function loadSources() {
  const sources = await api("/news-sources");
  newsSources = sources;
  $("#sources-total").textContent = `${sources.length} 个来源`;
  $("#sources-table").innerHTML = sources.length ? sources.map((item) => `<tr><td><strong>${escapeHtml(item.name)}</strong><small>${escapeHtml(item.lang)} · ${item.feed_type === "unusual_whales" ? "Unusual Whales API" : item.feed_type === "taoz_flash" ? "快讯 JSON" : "RSS / Atom"}</small><small class="truncate" title="${escapeHtml(item.url)}">${escapeHtml(item.url)}</small></td><td><span class="pill ${item.enabled ? "active" : "disabled"}">${item.enabled ? "启用" : "停用"}</span>${item.slow ? '<small>慢速轮询</small>' : ""}</td><td><label>权重<input class="table-input" data-field="weight" type="number" value="${Number(item.weight)}" min="1" max="1000"></label><label>限额<input class="table-input" data-field="hourly_limit" type="number" value="${Number(item.hourly_limit)}" min="1" max="10000"></label></td><td>${compactNumber(item.fetched_items)} / ${compactNumber(item.inserted_items)}</td><td>${formatTime(item.last_success_at)}</td><td class="truncate" title="${escapeHtml(item.last_error || "")}">${escapeHtml(item.last_error || "--")}</td><td><div class="button-group" data-source-row="${escapeHtml(item.name)}"><button data-source-action="edit">编辑</button><button data-source-action="toggle" data-enabled="${item.enabled}">${item.enabled ? "停用" : "启用"}</button><button data-source-action="slow" data-slow="${item.slow}">${item.slow ? "正常频率" : "设为慢速"}</button><button data-source-action="save">保存参数</button><button data-source-action="test">测试</button><button class="danger" data-source-action="delete">删除</button></div></td></tr>`).join("") : '<tr><td class="empty" colspan="7">暂无舆情来源，请点击“新增来源”创建。</td></tr>';
}

async function loadNews() {
  const newsFilter = $("#news-filter");
  const [sources, news, batches] = await Promise.all([
    api("/news-sources"),
    api(`/news?${formQuery(newsFilter)}`),
    api("/news-ai-batches?limit=5"),
  ]);
  const sourceSelect = newsFilter.elements.namedItem("source");
  const selectedSource = sourceSelect.value;
  sourceSelect.innerHTML = '<option value="">全部来源</option>' + sources.map((item) => `<option value="${escapeHtml(item.name)}">${escapeHtml(item.name)}</option>`).join("");
  sourceSelect.value = sources.some((item) => item.name === selectedSource) ? selectedSource : "";
  renderNewsAiBatch(batches);
  $("#news-total").textContent = `${compactNumber(news.total)} 条新闻 · 当前显示 ${news.items.length} 条`;
  $("#news-list").innerHTML = news.items.length ? news.items.map((item) => {
    const sentiment = sentimentView(item.ai_sentiment || item.sentiment);
    const ruleSentiment = sentimentView(item.rule_sentiment || item.sentiment);
    const title = item.title_zh || item.title || "无标题";
    const summary = item.summary || (item.title_zh && item.title_zh !== item.title ? item.title : "") || "暂无摘要";
    const link = safeLink(item.link);
    const stocks = Array.isArray(item.related_us_stocks) ? item.related_us_stocks : [];
    const stockList = stocks.length ? stocks.map((stock) => `<span class="news-stock" title="相关度 ${Math.round(Number(stock.relevance || 0) * 100)}% · ${escapeHtml(sentimentView(stock.direction).label)}">${escapeHtml(stock.symbol)}</span>`).join("") : '<span class="pill">待分析</span>';
    const confidence = item.ai_confidence == null ? "" : `${Math.round(Number(item.ai_confidence) * 100)}%`;
    const aiMeta = item.ai_sentiment ? `AI ${confidence} · 规则 ${ruleSentiment.label}` : `规则 ${ruleSentiment.label} · 等待 AI`;
    const reason = item.ai_reason || "尚未进行批量 AI 语义研判";
    const reasonMeta = [item.ai_category, item.ai_impact_strength, item.ai_time_horizon, item.ai_model].filter(Boolean).join(" · ");
    return `<tr><td>${formatTime(item.ts)}</td><td class="news-source"><strong>${escapeHtml(item.source || "未知来源")}</strong><small title="${escapeHtml(item.id || "")}">${escapeHtml(item.id || "--")}</small></td><td><div class="news-stock-list">${stockList}</div></td><td><span class="pill ${sentiment.className}">${escapeHtml(sentiment.label)}</span><small>${escapeHtml(aiMeta)}</small></td><td class="news-title"><strong>${escapeHtml(title)}</strong><small title="${escapeHtml(summary)}">${escapeHtml(summary)}</small></td><td class="news-ai-reason"><span title="${escapeHtml(reason)}">${escapeHtml(reason)}</span><small>${escapeHtml(reasonMeta)}</small></td><td>${escapeHtml(item.lang || "--")}</td><td>${link === "#" ? "--" : `<a class="news-link" href="${link}" target="_blank" rel="noopener noreferrer">查看 ↗</a>`}</td></tr>`;
  }).join("") : '<tr><td class="empty" colspan="8">没有符合条件的采集新闻</td></tr>';
}

async function startNewsAiBatch(count) {
  if (!(await confirmAction(
    `分析最近 ${count} 条新闻`,
    `系统将调用 y0ur 的全局 DeepSeek，按每组 5 条分析并生成美股整体结论。该操作会消耗模型额度，确认开始？`,
  ))) return;
  const result = await api("/news-ai-batches", {
    method: "POST",
    body: JSON.stringify({ count }),
  });
  toast(`AI 新闻分析批次已创建：${result.requested_count} 条`);
  await loadNews();
}

async function loadAiModel() {
  const data = await api("/ai-model");
  const configured = Boolean(data.configured);
  const enabled = Boolean(data.is_enabled);
  const state = $("#ai-model-state");
  state.textContent = !data.owner_exists ? "y0ur 不存在" : !configured ? "未配置" : enabled ? "已启用" : "已停用";
  state.className = `health-badge ${configured && enabled ? "ok" : "warning"}`;
  $("#ai-model-owner").textContent = data.owner_username || "y0ur";
  $("#ai-model-base-url").textContent = data.base_url || "--";
  $("#ai-model-fingerprint").textContent = data.api_key_fingerprint || "尚未配置";
  $("#ai-model-key-version").textContent = String(data.api_key_version || 0);
  $("#ai-model-updated-at").textContent = formatTime(data.updated_at);
  const modelSelect = $("#ai-model-name");
  const models = Array.isArray(data.models) ? [...data.models] : [];
  if (data.model_name && !models.includes(data.model_name)) models.unshift(data.model_name);
  modelSelect.innerHTML = models.map((model) => `<option value="${escapeHtml(model)}">${escapeHtml(model)}</option>`).join("");
  modelSelect.value = data.model_name || data.default_model || models[0] || "deepseek-v4-flash";
  $("#ai-model-enabled").checked = enabled;
  $("#ai-model-form").elements.namedItem("api_key").value = "";
  $("#ai-model-message").textContent = configured
    ? `密钥 ${data.api_key_fingerprint} · 所有用户统一调用`
    : "首次保存必须填写 DeepSeek API Key";
  $("#ai-model-test").disabled = !(configured && enabled);
}

async function saveAiModel(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const submit = form.querySelector('button[type="submit"]');
  const apiKey = String(form.elements.namedItem("api_key").value || "").trim();
  const payload = {
    model_name: form.elements.namedItem("model_name").value,
    is_enabled: form.elements.namedItem("is_enabled").checked,
  };
  if (apiKey) payload.api_key = apiKey;
  submit.disabled = true;
  try {
    await api("/ai-model", { method: "PUT", body: JSON.stringify(payload) });
    form.elements.namedItem("api_key").value = "";
    toast("全局 DeepSeek 配置已保存，所有用户立即生效");
    await loadAiModel();
  } finally {
    submit.disabled = false;
  }
}

async function testAiModel() {
  const button = $("#ai-model-test");
  button.disabled = true;
  try {
    const result = await api("/ai-model/test", { method: "POST", body: "{}" });
    toast(result.message || "全局 DeepSeek 连接正常");
  } finally {
    button.disabled = false;
  }
}

async function retryNewsAiBatch(batchId) {
  if (!(await confirmAction(
    "继续未完成 AI 批次",
    "系统会保留已成功的新闻结果，只分析尚未完成的新闻并重新生成整体结论。确认继续？",
  ))) return;
  await api(`/news-ai-batches/${encodeURIComponent(batchId)}/retry`, {
    method: "POST",
    body: "{}",
  });
  toast("AI 新闻批次已继续执行");
  await loadNews();
}

async function loadSymbols() {
  const data = await api(`/symbols?${formQuery($("#symbols-filter"))}`);
  $("#symbols-total").textContent = `${data.total} 个合约 · ${data.healthy} 个行情正常`;
  $("#symbols-table").innerHTML = data.items.length ? data.items.map((item) => {
    const score = (tf) => item.scores[tf]?.score ?? "--";
    const bars = Object.entries(item.kline_bars).map(([tf, count]) => `${tf} ${compactNumber(count)}`).join(" · ") || "--";
    const social = item.social ? `消息 ${compactNumber(item.social.st_msgs)} · 提及 ${compactNumber(item.social.ape_mentions)}` : "--";
    return `<tr><td><strong>${escapeHtml(item.symbol)}</strong><small>${formatTime(item.onboard_date, true)} 上线</small></td><td>${escapeHtml(item.underlying_type || "--")}<small>${escapeHtml(item.underlying_sub_types.join(" / "))}</small></td><td>${formatPrice(item.price)}<small>量 ${compactNumber(item.quote_volume)}</small></td><td class="${Number(item.pct_24h) >= 0 ? "positive" : "negative"}">${item.pct_24h == null ? "--" : `${Number(item.pct_24h).toFixed(2)}%`}</td><td><span class="pill ${escapeHtml(item.health)}">${escapeHtml(item.health)}</span><small>${item.ticker_lag_seconds == null ? "无行情" : `${item.ticker_lag_seconds}s`}</small></td><td>${score("15m")} / ${score("1h")} / ${score("4h")}</td><td>${escapeHtml(bars)}</td><td>${escapeHtml(social)}</td></tr>`;
  }).join("") : '<tr><td class="empty" colspan="8">没有符合条件的合约</td></tr>';
}

async function loadUsers() {
  const data = await api(`/users?limit=200&${formQuery($("#users-filter"))}`);
  $("#users-total").textContent = `${data.total} 个用户`;
  $("#users-table").innerHTML = data.items.length ? data.items.map((item) => `<tr><td><strong>${escapeHtml(item.username)}</strong><small>#${item.id} · ${escapeHtml(item.email || "未设置邮箱")}</small></td><td><span class="pill ${item.is_active ? "active" : "disabled"}">${item.is_active ? "启用" : "停用"}</span> ${item.is_admin ? '<span class="pill admin">管理员</span>' : '<span class="pill">普通用户</span>'}</td><td>${formatTime(item.last_login_at)}</td><td>${item.active_sessions}</td><td>${item.alert_count} / ${item.unread_alerts}</td><td>${item.binance_key_fingerprint ? `<span class="pill active">已配置</span><small>${escapeHtml(item.binance_key_fingerprint)}</small>` : '<span class="pill">未配置</span>'}</td><td><div class="button-group"><button data-user="${item.id}" data-user-action="active" data-value="${!item.is_active}">${item.is_active ? "停用" : "启用"}</button><button data-user="${item.id}" data-user-action="admin" data-value="${!item.is_admin}">${item.is_admin ? "取消管理员" : "设为管理员"}</button><button data-user="${item.id}" data-user-action="sessions">撤销会话</button></div></td></tr>`).join("") : '<tr><td class="empty" colspan="7">没有符合条件的用户</td></tr>';
}

async function loadStorage() {
  const items = await api("/storage");
  $("#storage-cards").innerHTML = items.map((item) => `<article class="storage-card"><span>${escapeHtml(item.table)}</span><strong>${compactNumber(item.total)}</strong><small>${item.oldest ? `${formatTime(item.oldest, ["scores", "klines"].includes(item.table))} → ${formatTime(item.newest, ["scores", "klines"].includes(item.table))}` : "无时间范围"}</small></article>`).join("");
}

async function loadAudit() {
  const data = await api(`/audit?limit=200&${formQuery($("#audit-filter"))}`);
  $("#audit-total").textContent = `${data.total} 条记录`;
  $("#audit-table").innerHTML = data.items.length ? data.items.map((item) => `<tr><td>${formatTime(item.created_at)}</td><td><strong>${escapeHtml(item.username || "system")}</strong></td><td><span class="pill">${escapeHtml(item.action)}</span></td><td>${escapeHtml(item.resource_type || "--")}<small>${escapeHtml(item.resource_id || "--")}</small></td><td>${escapeHtml(item.ip_address || "--")}</td><td class="truncate" title="${escapeHtml(JSON.stringify(item.metadata || {}))}">${escapeHtml(JSON.stringify(item.metadata || {}))}</td></tr>`).join("") : '<tr><td class="empty" colspan="6">没有符合条件的审计记录</td></tr>';
}

function confirmAction(title, message) {
  const dialog = $("#confirm-dialog");
  $("#confirm-title").textContent = title;
  $("#confirm-message").textContent = message;
  dialog.showModal();
  return new Promise((resolve) => {
    dialog.addEventListener("close", () => resolve(dialog.returnValue === "confirm"), { once: true });
  });
}

async function updateCollector(button) {
  const action = button.dataset.action;
  const name = button.dataset.collector;
  if (!(await confirmAction(action === "pause" ? "暂停采集器" : "恢复采集器", `确认${action === "pause" ? "暂停" : "恢复"} ${name}？变更将在下一轮采集生效。`))) return;
  await api(`/collectors/${encodeURIComponent(name)}/${action}`, { method: "POST" });
  toast(`${name} 已${action === "pause" ? "暂停" : "恢复"}`);
  await loadCollectors();
}

async function updateSource(button) {
  const row = button.closest("tr");
  const controls = button.closest("[data-source-row]");
  const name = controls.dataset.sourceRow;
  const action = button.dataset.sourceAction;
  if (action === "edit") {
    openSourceEditor(newsSources.find((item) => item.name === name));
    return;
  }
  if (action === "delete") {
    await removeSource(name);
    return;
  }
  if (action === "test") {
    button.disabled = true;
    try {
      const result = await api(`/news-sources/${encodeURIComponent(name)}/test`, { method: "POST" });
      toast(`${name} 测试成功，读取到 ${result.items.length} 条内容`);
    } finally { button.disabled = false; }
    return;
  }
  let payload;
  if (action === "toggle") payload = { enabled: button.dataset.enabled !== "true" };
  else if (action === "slow") payload = { slow: button.dataset.slow !== "true" };
  else payload = Object.fromEntries([...row.querySelectorAll("[data-field]")].map((input) => [input.dataset.field, Number(input.value)]));
  await api(`/news-sources/${encodeURIComponent(name)}`, { method: "PATCH", body: JSON.stringify(payload) });
  toast(`${name} 已更新`);
  await loadSources();
}

function openSourceEditor(source = null) {
  const dialog = $("#source-dialog");
  const form = $("#source-form");
  form.reset();
  form.mode.value = source ? "edit" : "create";
  form.name.readOnly = Boolean(source);
  form.name.value = source?.name || "";
  form.url.value = source?.url || "";
  form.feed_type.value = source?.feed_type || "rss";
  form.lang.value = source?.lang || "en";
  form.weight.value = source?.weight ?? 100;
  form.hourly_limit.value = source?.hourly_limit ?? 600;
  form.enabled.checked = source ? Boolean(source.enabled) : true;
  form.slow.checked = source ? Boolean(source.slow) : false;
  $("#source-dialog-title").textContent = source ? `编辑来源 · ${source.name}` : "新增舆情来源";
  $("#source-delete").classList.toggle("hidden", !source);
  $("#source-form-error").classList.add("hidden");
  dialog.showModal();
  setTimeout(() => (source ? form.url : form.name).focus(), 0);
}

function sourceFormPayload(form) {
  return {
    name: form.name.value.trim(),
    url: form.url.value.trim(),
    feed_type: form.feed_type.value,
    lang: form.lang.value.trim(),
    enabled: form.enabled.checked,
    slow: form.slow.checked,
    weight: Number(form.weight.value),
    hourly_limit: Number(form.hourly_limit.value),
  };
}

async function saveSource(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const submit = form.querySelector('button[type="submit"]');
  const error = $("#source-form-error");
  error.classList.add("hidden");
  if (!form.reportValidity()) return;
  submit.disabled = true;
  try {
    const payload = sourceFormPayload(form);
    if (form.mode.value === "create") {
      await api("/news-sources", { method: "POST", body: JSON.stringify(payload) });
      toast(`来源 ${payload.name} 已新增`);
    } else {
      const name = payload.name;
      delete payload.name;
      await api(`/news-sources/${encodeURIComponent(name)}`, { method: "PATCH", body: JSON.stringify(payload) });
      toast(`来源 ${name} 已更新`);
    }
    $("#source-dialog").close();
    await loadSources();
  } catch (caught) {
    error.textContent = caught.message;
    error.classList.remove("hidden");
  } finally {
    submit.disabled = false;
  }
}

async function removeSource(name, closeEditor = false) {
  if (!(await confirmAction("删除舆情来源", `确认删除 ${name}？历史舆情内容会保留，但该来源将不再参与采集。`))) return;
  await api(`/news-sources/${encodeURIComponent(name)}`, { method: "DELETE" });
  if (closeEditor) $("#source-dialog").close();
  toast(`来源 ${name} 已删除`);
  await loadSources();
}

async function deleteSource() {
  const form = $("#source-form");
  const name = form.name.value.trim();
  if (!name || form.mode.value !== "edit") return;
  await removeSource(name, true);
}

async function updateUser(button) {
  const userId = button.dataset.user;
  const action = button.dataset.userAction;
  const messages = {
    active: `${button.dataset.value === "true" ? "启用" : "停用"}用户 #${userId}？停用会同时撤销其所有活跃会话。`,
    admin: `${button.dataset.value === "true" ? "授予" : "移除"}用户 #${userId} 的管理员权限？`,
    sessions: `撤销用户 #${userId} 的全部活跃会话？`,
  };
  if (!(await confirmAction("确认权限操作", messages[action]))) return;
  if (action === "sessions") await api(`/users/${userId}/revoke-sessions`, { method: "POST" });
  else await api(`/users/${userId}`, { method: "PATCH", body: JSON.stringify({ [action === "active" ? "is_active" : "is_admin"]: button.dataset.value === "true" }) });
  toast("用户权限已更新");
  await loadUsers();
}

async function previewCleanup() {
  const form = $("#cleanup-form");
  cleanupPayload = Object.fromEntries([...new FormData(form)].map(([key, value]) => [key, Number(value)]));
  const result = await api("/maintenance/cleanup-preview", { method: "POST", body: JSON.stringify(cleanupPayload) });
  const counts = result.delete_counts;
  $("#cleanup-preview-result").innerHTML = `预计删除：提醒 <strong>${counts.alerts}</strong> 条 · 新闻 <strong>${counts.news}</strong> 条 · 评分 <strong>${counts.scores}</strong> 条。请核对后再执行。`;
  $("#cleanup-run").disabled = Object.values(counts).every((value) => value === 0);
}

async function runCleanup() {
  if (!cleanupPayload) return;
  if (!(await confirmAction("执行历史数据清理", "此操作会永久删除预览范围内的数据且不可恢复。确认继续？"))) return;
  const result = await api("/maintenance/cleanup", { method: "POST", body: JSON.stringify({ ...cleanupPayload, confirm: true }) });
  toast(`清理完成：共删除 ${Object.values(result.deleted).reduce((sum, value) => sum + value, 0)} 条记录`);
  cleanupPayload = null;
  $("#cleanup-run").disabled = true;
  await loadStorage();
}

function bindEvents() {
  $("#admin-login-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    const submit = form.querySelector('button[type="submit"]');
    submit.disabled = true;
    try {
      const values = Object.fromEntries(new FormData(form));
      const response = await fetch("/api/v2/auth/login", {
        method: "POST", headers: { "Content-Type": "application/json" }, credentials: "include",
        body: JSON.stringify({ ...values, client_type: "web" }),
      });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(errorMessage(payload, "登录失败"));
      accessToken = payload.access_token;
      showShell(await verifyAdmin());
      form.reset();
    } catch (error) {
      showLogin(error.message);
    } finally { submit.disabled = false; }
  });
  $$('[data-view-target]').forEach((button) => button.addEventListener("click", () => navigate(button.dataset.viewTarget)));
  $$('[data-jump]').forEach((button) => button.addEventListener("click", () => navigate(button.dataset.jump)));
  $("#refresh-view").addEventListener("click", () => loadView());
  $("#admin-logout").addEventListener("click", async () => {
    await fetch("/api/v2/auth/logout", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}", credentials: "include" }).catch(() => {});
    showLogin();
  });
  ["stock-library-filter", "alerts-filter", "news-filter", "symbols-filter", "users-filter", "audit-filter"].forEach((id) => $("#" + id).addEventListener("submit", (event) => { event.preventDefault(); loadView(); }));
  $("#stock-library-import").addEventListener("click", () => importStockLibrary().catch((error) => toast(error.message, "error")));
  $("#stock-library-table").addEventListener("click", (event) => {
    const detailButton = event.target.closest("[data-stock-detail]");
    if (detailButton) {
      openStockDetail(detailButton.dataset.stockDetail, detailButton);
      return;
    }
    const syncButton = event.target.closest("[data-stock-sync]");
    if (syncButton) syncStockProfile(syncButton.dataset.stockSync).catch((error) => toast(error.message, "error"));
  });
  $("#stock-detail-close").addEventListener("click", () => $("#stock-detail-dialog").close());
  $("#stock-detail-dialog").addEventListener("click", (event) => {
    if (event.target === event.currentTarget) event.currentTarget.close();
    const retryButton = event.target.closest("[data-stock-detail-retry]");
    if (retryButton) openStockDetail(retryButton.dataset.stockDetailRetry, stockDetailTrigger);
  });
  $("#stock-detail-dialog").addEventListener("close", () => {
    if (stockDetailTrigger?.isConnected) stockDetailTrigger.focus();
    stockDetailTrigger = null;
  });
  $("#rules-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    const payload = {
      score_alert_long: Number(form.score_alert_long.value), score_alert_short: Number(form.score_alert_short.value),
      score_alert_position: Number(form.score_alert_position.value), spike_alert_pct_5m: Number(form.spike_alert_pct_5m.value),
      watchlist_only: form.watchlist_only.checked, enabled_timeframes: [...form.querySelectorAll('[name="tf"]:checked')].map((input) => input.value),
    };
    if (!payload.enabled_timeframes.length) { toast("至少启用一个评分周期", "error"); return; }
    const result = await api("/alert-rules", { method: "PUT", body: JSON.stringify(payload) });
    $("#rules-version").textContent = `VERSION ${result.version}`;
    $("#rules-message").textContent = `版本 ${result.version} 已发布`;
    toast("信号规则已发布");
  });
  $("#collectors-table").addEventListener("click", (event) => { const button = event.target.closest("[data-collector]"); if (button) updateCollector(button).catch((error) => toast(error.message, "error")); });
  $("#uw-market-data-form").addEventListener("submit", (event) => saveUwMarketData(event).catch((error) => toast(error.message, "error")));
  $("#uw-market-test").addEventListener("click", () => testUwMarketData().catch((error) => toast(error.message, "error")));
  $("#uw-record-only").addEventListener("click", () => fallbackUwToRecordOnly().catch((error) => toast(error.message, "error")));
  $("#sources-table").addEventListener("click", (event) => { const button = event.target.closest("[data-source-action]"); if (button) updateSource(button).catch((error) => toast(error.message, "error")); });
  $("#source-create").addEventListener("click", () => openSourceEditor());
  $("#source-form").addEventListener("submit", saveSource);
  $("#source-dialog-close").addEventListener("click", () => $("#source-dialog").close());
  $("#source-cancel").addEventListener("click", () => $("#source-dialog").close());
  $("#source-delete").addEventListener("click", () => deleteSource().catch((error) => toast(error.message, "error")));
  $("#news-refresh").addEventListener("click", () => loadView("news"));
  $("#news-filter-reset").addEventListener("click", () => { $("#news-filter").reset(); loadView("news"); });
  $$("[data-news-ai-count]").forEach((button) => button.addEventListener("click", () => {
    startNewsAiBatch(Number(button.dataset.newsAiCount)).catch((error) => toast(error.message, "error"));
  }));
  $("#news-ai-retry").addEventListener("click", (event) => {
    retryNewsAiBatch(event.currentTarget.dataset.batchId).catch((error) => toast(error.message, "error"));
  });
  $("#ai-model-form").addEventListener("submit", (event) => saveAiModel(event).catch((error) => toast(error.message, "error")));
  $("#ai-model-test").addEventListener("click", () => testAiModel().catch((error) => toast(error.message, "error")));
  $("#users-table").addEventListener("click", (event) => { const button = event.target.closest("[data-user-action]"); if (button) updateUser(button).catch((error) => toast(error.message, "error")); });
  $("#cleanup-preview").addEventListener("click", () => previewCleanup().catch((error) => toast(error.message, "error")));
  $("#cleanup-run").addEventListener("click", () => runCleanup().catch((error) => toast(error.message, "error")));
  addEventListener("popstate", () => navigate(location.hash.slice(1) || "overview", false));
  setInterval(() => { $("#sidebar-clock").textContent = new Date().toLocaleTimeString("zh-CN", { hour12: false }); }, 1000);
}

bindEvents();
boot();
