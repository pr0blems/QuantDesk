const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => document.querySelectorAll(selector);
const API_ROOT = "/api/v2/admin";
const VIEWS = {
  overview: ["OPERATIONS / 01", "运行总览"],
  collectors: ["OPERATIONS / 02", "采集器"],
  "stock-library": ["US EQUITIES / 03", "美股资料库"],
  alerts: ["SIGNALS / 04", "提醒事件"],
  rules: ["SIGNALS / 05", "信号规则"],
  sources: ["INTELLIGENCE / 06", "舆情来源"],
  news: ["INTELLIGENCE / 07", "采集新闻"],
  symbols: ["MARKET DATA / 08", "合约数据"],
  users: ["GOVERNANCE / 09", "用户权限"],
  storage: ["GOVERNANCE / 10", "存储维护"],
  audit: ["GOVERNANCE / 11", "审计日志"],
};

let accessToken = "";
let adminUser = null;
let activeView = "overview";
let refreshPromise = null;
let toastTimer = null;
let cleanupPayload = null;
let newsSources = [];
let newsAiPollTimer = null;

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
      "stock-library": loadStockLibrary,
      alerts: loadAlerts,
      rules: loadRules,
      sources: loadSources,
      news: loadNews,
      symbols: loadSymbols,
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
  $("#stock-library-status").textContent = `已核验 ${data.verified} · 待复核 ${data.review_required}`;
  $("#stock-library-table").innerHTML = data.items.length ? data.items.map((item) => {
    const profile = item.profile || {};
    const analysis = item.analysis || {};
    const marketCap = profile.market_cap == null
      ? (item.security_type === "ETF" ? "基金规模待补充" : item.security_type === "PRE_IPO" ? "未上市" : "市值待补充")
      : compactNumber(profile.market_cap * 1000000);
    const isBaseline = analysis.analysis_version === "baseline-v1";
    const score = analysis.overall_score == null ? (isBaseline ? "" : "财务分析待生成") : `${Number(analysis.overall_score).toFixed(1)} 分`;
    const verifyClass = item.verification_status === "REVIEW_REQUIRED" ? "warning" : "active";
    const chineseName = item.company_name_zh || profile.legal_name || item.company_name || "待同步";
    const englishName = item.company_name_zh ? (profile.legal_name || item.company_name || "") : "";
    const chineseIndustry = [profile.sector_zh, profile.industry_zh].filter(Boolean).join(" · ");
    const englishIndustry = [profile.sector, profile.industry].filter(Boolean).join(" · ");
    const analysisSummary = analysis.business_summary || (profile.source ? "基础资料已同步，等待生成分析摘要" : "基础资料尚未同步");
    return `<tr><td><strong>${escapeHtml(item.symbol)}</strong><small>${escapeHtml(item.exchange)}</small></td><td><strong>${escapeHtml(chineseName)}</strong><small>${escapeHtml(chineseIndustry || "暂无中文行业资料")}</small>${englishName ? `<small>${escapeHtml(englishName)}${englishIndustry ? ` · ${escapeHtml(englishIndustry)}` : ""}</small>` : ""}</td><td><span class="pill">${escapeHtml(item.security_type)}</span></td><td><span class="pill ${verifyClass}">${item.verification_status === "REVIEW_REQUIRED" ? "需要复核" : "已核验"}</span></td><td>${marketCap}</td><td>${score ? `<strong>${escapeHtml(score)}</strong>` : ""}<small>${escapeHtml(analysisSummary)}</small></td><td>${formatTime(item.updated_at)}</td><td><button data-stock-sync="${escapeHtml(item.symbol)}">同步资料</button></td></tr>`;
  }).join("") : '<tr><td class="empty" colspan="8">资料库为空，请点击“导入当前美股”。</td></tr>';
}

async function importStockLibrary() {
  const result = await api("/stock-library/import", { method: "POST", body: "{}" });
  toast(`导入完成：新增 ${result.created}，更新 ${result.updated}，待复核 ${result.review_required}`);
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
  $("#sources-table").innerHTML = sources.length ? sources.map((item) => `<tr><td><strong>${escapeHtml(item.name)}</strong><small>${escapeHtml(item.lang)} · ${item.feed_type === "taoz_flash" ? "快讯 JSON" : "RSS / Atom"}</small><small class="truncate" title="${escapeHtml(item.url)}">${escapeHtml(item.url)}</small></td><td><span class="pill ${item.enabled ? "active" : "disabled"}">${item.enabled ? "启用" : "停用"}</span>${item.slow ? '<small>慢速轮询</small>' : ""}</td><td><label>权重<input class="table-input" data-field="weight" type="number" value="${Number(item.weight)}" min="1" max="1000"></label><label>限额<input class="table-input" data-field="hourly_limit" type="number" value="${Number(item.hourly_limit)}" min="1" max="10000"></label></td><td>${compactNumber(item.fetched_items)} / ${compactNumber(item.inserted_items)}</td><td>${formatTime(item.last_success_at)}</td><td class="truncate" title="${escapeHtml(item.last_error || "")}">${escapeHtml(item.last_error || "--")}</td><td><div class="button-group" data-source-row="${escapeHtml(item.name)}"><button data-source-action="edit">编辑</button><button data-source-action="toggle" data-enabled="${item.enabled}">${item.enabled ? "停用" : "启用"}</button><button data-source-action="slow" data-slow="${item.slow}">${item.slow ? "正常频率" : "设为慢速"}</button><button data-source-action="save">保存参数</button><button data-source-action="test">测试</button></div></td></tr>`).join("") : '<tr><td class="empty" colspan="7">暂无舆情来源，请点击“新增来源”创建。</td></tr>';
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
    `系统将调用当前默认 AI 模型，按每组 5 条分析并生成美股整体结论。该操作会消耗模型额度，确认开始？`,
  ))) return;
  const result = await api("/news-ai-batches", {
    method: "POST",
    body: JSON.stringify({ count }),
  });
  toast(`AI 新闻分析批次已创建：${result.requested_count} 条`);
  await loadNews();
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

async function deleteSource() {
  const form = $("#source-form");
  const name = form.name.value.trim();
  if (!name || form.mode.value !== "edit") return;
  if (!(await confirmAction("删除舆情来源", `确认删除 ${name}？历史舆情内容会保留，但该来源将不再参与采集。`))) return;
  await api(`/news-sources/${encodeURIComponent(name)}`, { method: "DELETE" });
  $("#source-dialog").close();
  toast(`来源 ${name} 已删除`);
  await loadSources();
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
    const button = event.target.closest("[data-stock-sync]");
    if (button) syncStockProfile(button.dataset.stockSync).catch((error) => toast(error.message, "error"));
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
  $("#users-table").addEventListener("click", (event) => { const button = event.target.closest("[data-user-action]"); if (button) updateUser(button).catch((error) => toast(error.message, "error")); });
  $("#cleanup-preview").addEventListener("click", () => previewCleanup().catch((error) => toast(error.message, "error")));
  $("#cleanup-run").addEventListener("click", () => runCleanup().catch((error) => toast(error.message, "error")));
  addEventListener("popstate", () => navigate(location.hash.slice(1) || "overview", false));
  setInterval(() => { $("#sidebar-clock").textContent = new Date().toLocaleTimeString("zh-CN", { hour12: false }); }, 1000);
}

bindEvents();
boot();
