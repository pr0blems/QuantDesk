const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => document.querySelectorAll(selector);
const TAB_USER_ID_KEY = "quantdesk.tab-user-id";
const TAB_USERNAME_KEY = "quantdesk.tab-username";
const THEME_STORAGE_KEY = "quantdesk.theme";
const AUTH_IDENTITY_CHANGED_MESSAGE = "检测到登录身份已变化。为防止数据写入其他用户，本次请求已中止，请重新登录。";
let accessToken = "";
let isAuthenticated = false;
let authBootResolved = false;
let authSessionVersion = 0;
let authenticatedUserId = readTabIdentity(TAB_USER_ID_KEY);
let authenticatedUsername = readTabIdentity(TAB_USERNAME_KEY);
let refreshAccessPromise = null;
let dashboardPerformance = null;
let binanceDashboardPerformance = null;
let currentUserHasBinanceCredentials = false;
let binancePerformanceAsset = "";
let performanceViewMonth = new Date(new Date().getFullYear(), new Date().getMonth(), 1);
let performanceRequestVersion = 0;
let binanceAccountRequestVersion = 0;
let aiModelProviders = [];
let aiModelConfigs = [];
let aiModelSettingsLoaded = false;
let aiModelSettingsLoading = false;
let aiModelSettingsRequestVersion = 0;

function preferredTheme() {
  try {
    const savedTheme = localStorage.getItem(THEME_STORAGE_KEY);
    if (savedTheme === "light" || savedTheme === "dark") return savedTheme;
  } catch (_) {
    // Storage can be unavailable in privacy-restricted browsers.
  }
  return "dark";
}

function applyTheme(theme, { persist = false, notify = false } = {}) {
  const selected = theme === "light" ? "light" : "dark";
  document.documentElement.dataset.theme = selected;
  document.documentElement.style.colorScheme = selected;
  document.querySelectorAll("[data-theme-toggle]").forEach((toggle) => {
    const isLight = selected === "light";
    toggle.setAttribute("aria-pressed", String(isLight));
    toggle.setAttribute("aria-label", isLight ? "切换深色主题" : "切换浅色主题");
    toggle.setAttribute("title", isLight ? "切换深色主题" : "切换浅色主题");
    const icon = toggle.querySelector("span");
    const label = toggle.querySelector("b");
    if (icon) icon.textContent = isLight ? "◐" : "☼";
    if (label) label.textContent = isLight ? "深色" : "浅色";
  });
  const themeMeta = document.querySelector('meta[name="theme-color"]');
  if (themeMeta) themeMeta.setAttribute("content", selected === "light" ? "#f1f2ef" : "#0c0e0c");
  if (persist) {
    try {
      localStorage.setItem(THEME_STORAGE_KEY, selected);
    } catch (_) {
      // Theme remains active for this page even when storage is blocked.
    }
  }
  if (notify) window.dispatchEvent(new CustomEvent("quantdesk:theme-change", { detail: { theme: selected } }));
}

applyTheme(preferredTheme());

const AI_PROVIDER_FALLBACKS = Object.freeze([
  { code: "deepseek", name: "DeepSeek", base_url: "", default_model: "deepseek-v4-flash", models: ["deepseek-v4-flash", "deepseek-v4-pro"] },
  { code: "doubao", name: "豆包", base_url: "", default_model: "doubao-seed-2-0-lite-260215", models: ["doubao-seed-2-0-lite-260215"] },
  { code: "qwen", name: "千问", base_url: "", default_model: "qwen3.7-plus", models: ["qwen3.7-plus", "qwen-plus"] },
  { code: "kimi", name: "Kimi", base_url: "", default_model: "kimi-k3", models: ["kimi-k3"] },
  { code: "minimax", name: "MiniMax", base_url: "", default_model: "MiniMax-M2.7", models: ["MiniMax-M2.7"] },
  { code: "openai", name: "OpenAI", base_url: "", default_model: "", models: [] },
]);
const panelNames = {
  overview: "工作台",
  monitor: "合约监控",
  "ai-monitor": "发现机会",
  paper: "模拟盘",
  live: "实盘交易",
  settings: "系统设置",
  strategies: "策略中心",
  backtest: "数据回测",
};

const LOGIN_PATH = "/login";
const DEFAULT_PANEL = "monitor";
const panelPaths = Object.freeze({
  monitor: "/monitor",
  "ai-monitor": "/ai-monitor",
  paper: "/paper",
  live: "/live",
  overview: "/overview",
  settings: "/settings",
  strategies: "/strategies",
  backtest: "/backtest",
});
const panelByPath = new Map(Object.entries(panelPaths).map(([panel, path]) => [path, panel]));
panelByPath.set("/credentials", "settings");
panelByPath.set("/orders", "live");
panelByPath.set("/risk", "overview");
panelByPath.set("/audit", "overview");

function panelFromPath(pathname = window.location.pathname) {
  return panelByPath.get(pathname) || "";
}

function safeNextPath(value) {
  if (typeof value !== "string") return "";
  const panel = panelByPath.get(value);
  return panel ? panelPaths[panel] : "";
}

function nextPathFromLoginUrl() {
  if (window.location.pathname !== LOGIN_PATH) return "";
  return safeNextPath(new URLSearchParams(window.location.search).get("next"));
}

function loginUrl(nextPath = "") {
  const safeNext = safeNextPath(nextPath);
  return safeNext ? `${LOGIN_PATH}?${new URLSearchParams({ next: safeNext })}` : LOGIN_PATH;
}

function replaceRoute(path, state = {}) {
  if (`${window.location.pathname}${window.location.search}` === path) return;
  window.history.replaceState(state, "", path);
}

function syncPanelRoute(panel, historyMode = "push") {
  const path = panelPaths[panel];
  if (!path || historyMode === "none") return;
  const current = `${window.location.pathname}${window.location.search}`;
  if (current === path) return;
  const state = { quantdeskPanel: panel };
  if (historyMode === "replace") window.history.replaceState(state, "", path);
  else window.history.pushState(state, "", path);
}

function restoreAuthenticatedRoute() {
  let panel = panelFromPath();
  if (!panel && window.location.pathname === LOGIN_PATH) {
    panel = panelFromPath(nextPathFromLoginUrl());
  }
  if (!panel) panel = DEFAULT_PANEL;
  replaceRoute(panelPaths[panel], { quantdeskPanel: panel });
  openPanel(panel, { historyMode: "none" });
}

function showLoginRoute({ preserveNext = true } = {}) {
  const directPanel = panelFromPath();
  const nextPath = preserveNext
    ? (directPanel ? panelPaths[directPanel] : nextPathFromLoginUrl())
    : "";
  replaceRoute(loginUrl(nextPath), { quantdeskLogin: true });
  document.title = "登录 · QuantDesk";
  setAuthenticated(false);
}

function finishAuthBoot() {
  if (authBootResolved) return;
  authBootResolved = true;
  document.documentElement.classList.remove("auth-booting");
  const boot = $("#auth-boot");
  if (boot) boot.setAttribute("aria-hidden", "true");
}

function handleRouteChange() {
  if (!authBootResolved) return;
  if (isAuthenticated) restoreAuthenticatedRoute();
  else showLoginRoute({ preserveNext: true });
}

function showMessage(target, message, kind = "") {
  target.textContent = message;
  target.className = `message ${kind}`.trim();
}

function apiErrorMessage(detail) {
  if (typeof detail === "string" && detail.trim()) return detail;
  if (Array.isArray(detail)) {
    const messages = detail.map((item) => {
      const location = Array.isArray(item?.loc)
        ? item.loc.filter((part) => part !== "body").join(".")
        : "";
      const message = item?.msg || item?.message || "参数无效";
      return location ? `${location}：${message}` : message;
    }).filter(Boolean);
    if (messages.length) return messages.join("；");
  }
  if (detail && typeof detail === "object") return detail.message || "请求参数无效";
  return "请求失败";
}

function readTabIdentity(key) {
  try {
    return window.sessionStorage.getItem(key) || "";
  } catch (_) {
    return "";
  }
}

function writeTabIdentity(key, value) {
  try {
    if (value) window.sessionStorage.setItem(key, value);
    else window.sessionStorage.removeItem(key);
  } catch (_) {
    // Browsers can disable session storage. The in-memory identity check remains active.
  }
}

function rememberAuthenticatedUser(user) {
  const userId = String(user?.id || "").trim();
  if (!/^\d+$/.test(userId)) throw new Error("服务器返回了无效的用户身份");
  authenticatedUserId = userId;
  authenticatedUsername = String(user?.username || "").trim();
  writeTabIdentity(TAB_USER_ID_KEY, authenticatedUserId);
  writeTabIdentity(TAB_USERNAME_KEY, authenticatedUsername);
}

function clearAuthenticatedUser() {
  authenticatedUserId = "";
  authenticatedUsername = "";
  writeTabIdentity(TAB_USER_ID_KEY, "");
  writeTabIdentity(TAB_USERNAME_KEY, "");
}

function rejectChangedIdentity(actualUser) {
  accessToken = "";
  const expected = authenticatedUsername || (authenticatedUserId ? `用户 ${authenticatedUserId}` : "当前用户");
  const actual = String(actualUser?.username || "").trim();
  const message = actual
    ? `${AUTH_IDENTITY_CHANGED_MESSAGE}（标签页：${expected}，刷新身份：${actual}）`
    : AUTH_IDENTITY_CHANGED_MESSAGE;
  showLoginRoute({ preserveNext: true });
  finishAuthBoot();
  showMessage($("#auth-message"), message, "error");
  const error = new Error(message);
  error.code = "AUTH_IDENTITY_CHANGED";
  throw error;
}

async function api(path, options = {}, retry = true) {
  const headers = new Headers(options.headers || {});
  if (options.body && !headers.has("Content-Type")) headers.set("Content-Type", "application/json");
  if (accessToken) {
    headers.set("Authorization", `Bearer ${accessToken}`);
    if (authenticatedUserId) headers.set("X-QuantDesk-User-ID", authenticatedUserId);
  }
  const response = await fetch(path, { ...options, headers, credentials: "include" });
  if (response.status === 401 && retry && !path.includes("/auth/")) {
    const refreshed = await refreshAccess();
    if (refreshed) return api(path, options, false);
  }
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    const error = new Error(apiErrorMessage(payload.detail));
    error.status = response.status;
    error.retryAfter = Number(response.headers.get("Retry-After") || 0);
    throw error;
  }
  return payload;
}

window.quantdeskApi = api;

async function apiStream(path, options = {}, retry = true) {
  const headers = new Headers(options.headers || {});
  headers.set("Accept", "text/event-stream");
  if (accessToken) {
    headers.set("Authorization", `Bearer ${accessToken}`);
    if (authenticatedUserId) headers.set("X-QuantDesk-User-ID", authenticatedUserId);
  }
  const response = await fetch(path, { ...options, headers, credentials: "include" });
  if (response.status === 401 && retry && !path.includes("/auth/")) {
    const refreshed = await refreshAccess();
    if (refreshed) return apiStream(path, options, false);
  }
  if (!response.ok) {
    const payload = await response.json().catch(() => ({}));
    throw new Error(apiErrorMessage(payload.detail));
  }
  return response;
}

window.quantdeskApiStream = apiStream;

async function openAiMonitorWebSocket() {
  if (!accessToken) {
    const restored = await refreshAccess();
    if (!restored) throw new Error("登录状态已失效，请重新登录");
  }
  const endpoint = new URL("/api/v2/ai-monitor/ws", window.location.origin);
  endpoint.protocol = endpoint.protocol === "https:" ? "wss:" : "ws:";
  return new WebSocket(endpoint, [
    "quantdesk.ai-monitor.v1",
    `quantdesk.auth.${accessToken}`,
  ]);
}

window.quantdeskOpenAiMonitorSocket = openAiMonitorWebSocket;

async function performRefreshAccess() {
  let response;
  try {
    response = await fetch("/api/v2/auth/refresh", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: "{}",
      credentials: "include",
    });
  } catch (_) {
    return false;
  }
  if (!response.ok) return false;
  const data = await response.json().catch(() => ({}));
  const candidateToken = String(data.access_token || "");
  if (!candidateToken) return false;

  if (authenticatedUserId) {
    let identityResponse;
    try {
      identityResponse = await fetch("/api/v2/me", {
        headers: { Authorization: `Bearer ${candidateToken}` },
        credentials: "include",
      });
    } catch (_) {
      return false;
    }
    if (!identityResponse.ok) return false;
    const actualUser = await identityResponse.json().catch(() => null);
    if (!actualUser || String(actualUser.id) !== authenticatedUserId) {
      rejectChangedIdentity(actualUser);
    }
  }

  accessToken = candidateToken;
  return true;
}

async function refreshAccess() {
  if (refreshAccessPromise) return refreshAccessPromise;
  const pending = performRefreshAccess();
  refreshAccessPromise = pending;
  try {
    return await pending;
  } finally {
    if (refreshAccessPromise === pending) refreshAccessPromise = null;
  }
}

function closeSidebar() {
  $("#sidebar").classList.remove("open");
  $("#sidebar-backdrop").classList.remove("open");
  $("#menu-toggle").setAttribute("aria-expanded", "false");
}

function openPanel(name, { historyMode = "push" } = {}) {
  const selected = panelNames[name] ? name : DEFAULT_PANEL;
  syncPanelRoute(selected, historyMode);
  document.title = `${panelNames[selected]} · QuantDesk`;
  if (selected !== "settings") $("#credential-form").reset();
  $$("[data-panel]").forEach((panel) => panel.classList.toggle("hidden", panel.dataset.panel !== selected));
  $$("[data-panel-target]").forEach((item) => {
    const active = item.dataset.panelTarget === selected;
    item.classList.toggle("active", active);
    if (active) item.setAttribute("aria-current", "page");
    else item.removeAttribute("aria-current");
  });
  $("#mobile-title").textContent = panelNames[selected];
  $(".workspace-content").classList.toggle("monitor-mode", ["monitor", "paper", "live", "backtest"].includes(selected));
  $(".workspace-content").classList.toggle("ai-monitor-mode", selected === "ai-monitor");
  const monitor = $("#contract-monitor");
  const aiMonitor = $("#ai-monitor-dashboard");
  const paper = $("#paper-dashboard");
  const live = $("#live-dashboard");
  const strategies = $("#strategy-center");
  const backtest = $("#backtest-workbench");
  if (monitor && selected === "monitor" && typeof monitor.start === "function") monitor.start();
  if (monitor && selected !== "monitor" && typeof monitor.pause === "function") monitor.pause();
  if (aiMonitor && selected === "ai-monitor" && typeof aiMonitor.start === "function") aiMonitor.start();
  if (aiMonitor && selected !== "ai-monitor" && typeof aiMonitor.pause === "function") aiMonitor.pause();
  if (paper && selected === "paper" && typeof paper.start === "function") paper.start();
  if (paper && selected !== "paper" && typeof paper.pause === "function") paper.pause();
  if (live && selected === "live" && typeof live.start === "function") live.start();
  if (live && selected !== "live" && typeof live.pause === "function") live.pause();
  if (strategies && selected === "strategies" && typeof strategies.start === "function") strategies.start();
  if (strategies && selected !== "strategies" && typeof strategies.pause === "function") strategies.pause();
  if (backtest && selected === "backtest" && typeof backtest.start === "function") backtest.start();
  if (backtest && selected !== "backtest" && typeof backtest.pause === "function") backtest.pause();
  if (selected === "overview" && isAuthenticated && authBootResolved) {
    refreshBinanceAccount(currentUserHasBinanceCredentials);
  }
  if (selected === "settings" && isAuthenticated) {
    void loadAiModelSettings();
  }
  closeSidebar();
  const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  window.scrollTo({ top: 0, behavior: reducedMotion ? "auto" : "smooth" });
}

function setAuthenticated(authenticated) {
  if (!authenticated && isAuthenticated) authSessionVersion += 1;
  isAuthenticated = authenticated;
  $("#login-page").classList.toggle("hidden", authenticated);
  $("#app-shell").classList.toggle("hidden", !authenticated);
  if (!authenticated) {
    resetAiModelSettings();
    $("#credential-form").reset();
    resetBinanceAccount();
    resetPerformancePanel();
    const monitor = $("#contract-monitor");
    const paper = $("#paper-dashboard");
    const live = $("#live-dashboard");
    const strategies = $("#strategy-center");
    const backtest = $("#backtest-workbench");
    if (monitor && typeof monitor.pause === "function") monitor.pause();
    if (paper && typeof paper.pause === "function") paper.pause();
    if (live && typeof live.pause === "function") live.pause();
    if (strategies && typeof strategies.resetSession === "function") strategies.resetSession();
    else if (strategies && typeof strategies.pause === "function") strategies.pause();
    if (backtest && typeof backtest.resetSession === "function") backtest.resetSession();
    else if (backtest && typeof backtest.pause === "function") backtest.pause();
    closeSidebar();
  }
}

function updateCredentialStatus(configured, fingerprint = "") {
  const text = configured ? `已配置 · ${fingerprint}` : "尚未配置";
  $$('[data-credential-status]').forEach((target) => { target.textContent = text; });
}

function aiModelElement(tagName, className = "", text = "") {
  const element = document.createElement(tagName);
  if (className) element.className = className;
  if (text !== "") element.textContent = text;
  return element;
}

function normalizeAiModelProvider(item) {
  const code = String(item?.code || "").trim().toLowerCase();
  if (!code) return null;
  const rawModels = Array.isArray(item?.models) ? item.models : [];
  const models = [...new Set(rawModels.map((model) => String(model || "").trim()).filter(Boolean))];
  const defaultModel = String(item?.default_model || "").trim();
  if (defaultModel && !models.includes(defaultModel)) models.unshift(defaultModel);
  return {
    code,
    name: String(item?.name || code).trim(),
    baseUrl: String(item?.base_url || "").trim(),
    defaultModel,
    models,
  };
}

function aiModelProviderList(payload) {
  const source = Array.isArray(payload) ? payload : (Array.isArray(payload?.items) ? payload.items : []);
  return source.map(normalizeAiModelProvider).filter(Boolean);
}

function fallbackAiModelProviders() {
  return AI_PROVIDER_FALLBACKS.map(normalizeAiModelProvider).filter(Boolean);
}

function aiModelProvider(code) {
  const normalizedCode = String(code || "").trim().toLowerCase();
  return aiModelProviders.find((provider) => provider.code === normalizedCode)
    || fallbackAiModelProviders().find((provider) => provider.code === normalizedCode)
    || null;
}

function normalizeAiModelConfig(item) {
  return {
    id: String(item?.id || ""),
    providerCode: String(item?.provider_code || "").trim().toLowerCase(),
    providerName: String(item?.provider_name || "").trim(),
    displayName: String(item?.display_name || "").trim(),
    baseUrl: String(item?.base_url || "").trim(),
    modelName: String(item?.model_name || "").trim(),
    apiKeyConfigured: Boolean(item?.api_key_configured),
    apiKeyFingerprint: String(item?.api_key_fingerprint || "").trim(),
    isEnabled: Boolean(item?.is_enabled),
    isDefault: Boolean(item?.is_default),
    createdAt: String(item?.created_at || ""),
    updatedAt: String(item?.updated_at || ""),
  };
}

function setAiModelListStatus(message, kind = "") {
  const status = $("#ai-model-list-status");
  status.textContent = message;
  status.className = `ai-model-list-status ${kind}`.trim();
  status.classList.toggle("hidden", !message);
}

function aiModelProviderMark(code) {
  return {
    deepseek: "DS",
    doubao: "豆",
    qwen: "千",
    kimi: "KM",
    minimax: "MM",
    openai: "OA",
  }[code] || String(code || "AI").slice(0, 2).toUpperCase();
}

function createAiModelBadge(text, kind = "") {
  return aiModelElement("span", `ai-model-badge ${kind}`.trim(), text);
}

function createAiModelAction(label, action, configId) {
  const button = aiModelElement("button", "", label);
  button.type = "button";
  button.dataset.aiAction = action;
  button.dataset.aiConfigId = configId;
  return button;
}

function renderAiModelConfigs() {
  const list = $("#ai-model-config-list");
  list.replaceChildren();
  list.setAttribute("aria-busy", "false");
  if (!aiModelConfigs.length) {
    setAiModelListStatus("尚未配置 AI 模型。新增后可作为策略语义编辑的模型来源。", "empty");
    return;
  }
  setAiModelListStatus("");

  aiModelConfigs.forEach((config) => {
    const provider = aiModelProvider(config.providerCode);
    const providerName = config.providerName || provider?.name || config.providerCode || "AI 服务";
    const card = aiModelElement("article", "ai-model-config-card");
    card.classList.toggle("is-default", config.isDefault);
    card.classList.toggle("is-disabled", !config.isEnabled);
    card.dataset.aiConfigId = config.id;

    card.append(aiModelElement("span", "ai-model-provider-mark", aiModelProviderMark(config.providerCode)));

    const main = aiModelElement("div", "ai-model-config-main");
    const title = aiModelElement("div", "ai-model-config-title");
    title.append(aiModelElement("strong", "", config.displayName || `${providerName} 配置`));
    if (config.isDefault) title.append(createAiModelBadge("默认", "default"));
    title.append(createAiModelBadge(config.isEnabled ? "已启用" : "已停用", config.isEnabled ? "enabled" : "disabled"));
    main.append(title);
    main.append(aiModelElement("span", "", `${providerName} · ${config.modelName || "未指定模型"}`));
    const endpoint = config.baseUrl || provider?.baseUrl || "系统托管服务地址";
    const keyState = config.apiKeyConfigured
      ? `密钥 ${config.apiKeyFingerprint || "已加密"}`
      : "尚未配置 API Key";
    main.append(aiModelElement("small", "", `${endpoint} · ${keyState}`));
    card.append(main);

    const actions = aiModelElement("div", "ai-model-config-actions");
    actions.append(createAiModelAction("测试", "test", config.id));
    actions.append(createAiModelAction("编辑", "edit", config.id));
    actions.append(createAiModelAction(config.isEnabled ? "停用" : "启用", "toggle", config.id));
    if (!config.isDefault) actions.append(createAiModelAction("设为默认", "default", config.id));
    actions.append(createAiModelAction("删除", "delete", config.id));
    card.append(actions);
    list.append(card);
  });
}

function renderAiModelProviderOptions(selectedCode = "") {
  const select = $("#ai-model-provider");
  const current = selectedCode || select.value;
  select.replaceChildren();
  const providers = aiModelProviders.length ? aiModelProviders : fallbackAiModelProviders();
  providers.forEach((provider) => {
    const option = aiModelElement("option", "", provider.name);
    option.value = provider.code;
    select.append(option);
  });
  if (current && providers.some((provider) => provider.code === current)) select.value = current;
  else if (providers.length) select.value = providers[0].code;
  updateAiModelProviderDetails(false);
}

function updateAiModelProviderDetails(useDefaultModel = true) {
  const provider = aiModelProvider($("#ai-model-provider").value);
  const endpoint = $("#ai-provider-endpoint strong");
  endpoint.textContent = provider?.baseUrl || "由系统配置并安全托管";
  endpoint.title = provider?.baseUrl || "";

  const datalist = $("#ai-model-name-options");
  datalist.replaceChildren();
  (provider?.models || []).forEach((model) => {
    const option = document.createElement("option");
    option.value = model;
    datalist.append(option);
  });
  if (useDefaultModel && !$("#ai-model-name").value.trim() && provider?.defaultModel) {
    $("#ai-model-name").value = provider.defaultModel;
  }
}

function closeAiModelDialog() {
  const dialog = $("#ai-model-dialog");
  if (dialog.open) dialog.close();
  $("#ai-model-form").reset();
  $("#ai-model-form-message").textContent = "";
}

function openAiModelDialog(config = null) {
  const form = $("#ai-model-form");
  form.reset();
  form.dataset.originalProviderCode = config?.providerCode || "";
  $("#ai-model-config-id").value = config?.id || "";
  $("#ai-model-dialog-title").textContent = config ? "编辑 AI 模型" : "新增 AI 模型";
  $("#ai-model-dialog-subtitle").textContent = config
    ? "API Key 留空会保留现有密钥，其他字段保存后立即生效。"
    : "API Key 将加密保存，提交后不再回显。";
  $("#ai-model-form-message").textContent = "";
  renderAiModelProviderOptions(config?.providerCode || "");
  $("#ai-model-display-name").value = config?.displayName || "";
  $("#ai-model-name").value = config?.modelName || "";
  $("#ai-model-enabled").checked = config ? config.isEnabled : true;
  $("#ai-model-default").checked = config ? config.isDefault : false;
  const apiKey = $("#ai-model-api-key");
  apiKey.value = "";
  apiKey.required = !config;
  apiKey.placeholder = config ? "留空则保留当前已加密密钥" : "输入服务商 API Key";
  $("#ai-model-api-key-hint").textContent = config
    ? `当前${config.apiKeyConfigured ? `已配置 ${config.apiKeyFingerprint || "加密密钥"}` : "尚未配置密钥"}；留空不会替换。`
    : "保存后不会再次显示完整密钥。";
  updateAiModelProviderDetails(!config);
  const dialog = $("#ai-model-dialog");
  if (!dialog.open) dialog.showModal();
  window.setTimeout(() => $("#ai-model-provider").focus(), 0);
}

function resetAiModelSettings() {
  aiModelSettingsRequestVersion += 1;
  aiModelProviders = [];
  aiModelConfigs = [];
  aiModelSettingsLoaded = false;
  aiModelSettingsLoading = false;
  const list = $("#ai-model-config-list");
  if (list) {
    list.replaceChildren();
    list.setAttribute("aria-busy", "false");
  }
  const status = $("#ai-model-list-status");
  if (status) setAiModelListStatus("进入系统设置后加载模型配置。");
  const dialog = $("#ai-model-dialog");
  if (dialog?.open) dialog.close();
}

async function loadAiModelSettings(force = false) {
  if (!isAuthenticated || aiModelSettingsLoading || (aiModelSettingsLoaded && !force)) return;
  aiModelSettingsLoading = true;
  const requestVersion = ++aiModelSettingsRequestVersion;
  const sessionVersion = authSessionVersion;
  $("#ai-model-config-list").setAttribute("aria-busy", "true");
  setAiModelListStatus("正在读取当前用户的 AI 模型配置…", "loading");
  try {
    const [providerPayload, configPayload] = await Promise.all([
      api("/api/v2/me/ai-model-providers"),
      api("/api/v2/me/ai-model-configs"),
    ]);
    if (!isAuthenticated || sessionVersion !== authSessionVersion || requestVersion !== aiModelSettingsRequestVersion) return;
    aiModelProviders = aiModelProviderList(providerPayload);
    if (!aiModelProviders.length) aiModelProviders = fallbackAiModelProviders();
    const configSource = Array.isArray(configPayload)
      ? configPayload
      : (Array.isArray(configPayload?.items) ? configPayload.items : []);
    aiModelConfigs = configSource.map(normalizeAiModelConfig).filter((config) => config.id);
    aiModelSettingsLoaded = true;
    renderAiModelConfigs();
  } catch (error) {
    if (!isAuthenticated || sessionVersion !== authSessionVersion || requestVersion !== aiModelSettingsRequestVersion) return;
    aiModelSettingsLoaded = false;
    $("#ai-model-config-list").setAttribute("aria-busy", "false");
    setAiModelListStatus(`模型配置加载失败：${error.message}`, "error");
  } finally {
    if (requestVersion === aiModelSettingsRequestVersion) aiModelSettingsLoading = false;
  }
}

async function saveAiModelConfig(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const configId = $("#ai-model-config-id").value;
  const providerCode = $("#ai-model-provider").value;
  const apiKey = $("#ai-model-api-key").value.trim();
  if (configId && providerCode !== form.dataset.originalProviderCode && !apiKey) {
    showMessage($("#ai-model-form-message"), "更换服务商时必须输入对应的新 API Key。", "error");
    $("#ai-model-api-key").focus();
    return;
  }
  const payload = {
    provider_code: providerCode,
    display_name: $("#ai-model-display-name").value.trim(),
    model_name: $("#ai-model-name").value.trim(),
    is_enabled: $("#ai-model-enabled").checked,
    is_default: $("#ai-model-default").checked,
  };
  if (apiKey) payload.api_key = apiKey;

  const saveButton = $("#ai-model-save");
  const previousLabel = saveButton.textContent;
  saveButton.disabled = true;
  saveButton.textContent = "保存中…";
  showMessage($("#ai-model-form-message"), "");
  try {
    await api(configId ? `/api/v2/me/ai-model-configs/${configId}` : "/api/v2/me/ai-model-configs", {
      method: configId ? "PUT" : "POST",
      body: JSON.stringify(payload),
    });
    closeAiModelDialog();
    aiModelSettingsLoaded = false;
    await loadAiModelSettings(true);
    if (aiModelSettingsLoaded) setAiModelListStatus(configId ? "模型配置已更新。" : "模型配置已创建。", "success");
  } catch (error) {
    showMessage($("#ai-model-form-message"), error.message, "error");
  } finally {
    saveButton.disabled = false;
    saveButton.textContent = previousLabel;
  }
}

async function runAiModelConfigAction(button) {
  const config = aiModelConfigs.find((item) => item.id === button.dataset.aiConfigId);
  if (!config) return;
  const action = button.dataset.aiAction;
  if (action === "edit") {
    openAiModelDialog(config);
    return;
  }
  if (action === "delete" && !window.confirm(`确定删除“${config.displayName || config.providerName}”模型配置？`)) return;

  button.disabled = true;
  const previousLabel = button.textContent;
  try {
    if (action === "test") {
      button.textContent = "测试中…";
      const result = await api(`/api/v2/me/ai-model-configs/${config.id}/test`, { method: "POST" });
      setAiModelListStatus(result.message || "API 测试成功，模型服务可正常使用", "success");
      button.textContent = previousLabel;
      button.disabled = false;
      return;
    } else if (action === "delete") {
      await api(`/api/v2/me/ai-model-configs/${config.id}`, { method: "DELETE" });
    } else if (action === "toggle") {
      const enabled = !config.isEnabled;
      await api(`/api/v2/me/ai-model-configs/${config.id}`, {
        method: "PUT",
        body: JSON.stringify({ is_enabled: enabled, ...(enabled ? {} : { is_default: false }) }),
      });
    } else if (action === "default") {
      await api(`/api/v2/me/ai-model-configs/${config.id}`, {
        method: "PUT",
        body: JSON.stringify({ is_enabled: true, is_default: true }),
      });
    } else {
      return;
    }
    aiModelSettingsLoaded = false;
    await loadAiModelSettings(true);
  } catch (error) {
    setAiModelListStatus(action === "test" ? `API 测试失败：${error.message}` : error.message, "error");
    button.textContent = previousLabel;
    button.disabled = false;
  }
}

function accountTypeLabel(value) {
  const type = String(value || "").trim().toUpperCase();
  return {
    SPOT: "现货账户",
    MARGIN: "杠杆账户",
    FUTURES: "合约账户",
    UM_FUTURE: "U 本位合约",
    USD_M_FUTURES: "U 本位合约",
    "USDⓈ-M FUTURES": "U 本位合约",
    CM_FUTURE: "币本位合约",
    PORTFOLIO_MARGIN: "统一账户",
    UNIFIED: "统一账户",
  }[type] || (type ? `${type.slice(0, 20)} 账户` : "Binance 账户");
}

function formatAccountAmount(value, asset, signed = false) {
  if (value === null || value === undefined || value === "") return "--";
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) return "--";
  const amount = numeric.toLocaleString("zh-CN", { minimumFractionDigits: 2, maximumFractionDigits: 8 });
  return `${signed && numeric > 0 ? "+" : ""}${amount} ${asset}`;
}

function resetBinanceAccount() {
  const card = $("#binance-account-card");
  card.className = "metric-card account-metric-card loading";
  $("#binance-account-state").textContent = "检查中";
  $("#binance-wallet-balance").textContent = "--";
  $("#binance-account-detail").textContent = "正在读取当前用户的账户状态";
  $("#binance-account-detail").removeAttribute("title");
  $("#binance-account-action").classList.add("hidden");
  $("#binance-account-action").dataset.accountAction = "settings";
}

function firstFinite(...values) {
  for (const value of values) {
    const numeric = Number(value);
    if (value !== null && value !== undefined && value !== "" && Number.isFinite(numeric)) return numeric;
  }
  return null;
}

function performanceDate(value) {
  if (value === null || value === undefined || value === "") return null;
  if (typeof value === "string" && /^\d{4}-\d{2}-\d{2}$/.test(value)) {
    const [year, month, day] = value.split("-").map(Number);
    return new Date(year, month - 1, day);
  }
  const numeric = Number(value);
  const date = new Date(Number.isFinite(numeric) && numeric > 0 ? (numeric < 1e12 ? numeric * 1000 : numeric) : value);
  return Number.isNaN(date.getTime()) ? null : date;
}

function performanceDateKey(value) {
  const date = value instanceof Date ? value : performanceDate(value);
  if (!date) return "";
  return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, "0")}-${String(date.getDate()).padStart(2, "0")}`;
}

function monthStart(value) {
  const date = value instanceof Date ? value : performanceDate(value);
  const resolved = date || new Date();
  return new Date(resolved.getFullYear(), resolved.getMonth(), 1);
}

function shiftPerformanceMonth(value, amount) {
  return new Date(value.getFullYear(), value.getMonth() + amount, 1);
}

function formatPerformancePercent(value, signed = true) {
  if (value === null || value === undefined || value === "") return "--";
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) return "--";
  return `${signed && numeric > 0 ? "+" : ""}${numeric.toLocaleString("zh-CN", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}%`;
}

function formatPerformancePnl(value, currency = "USDT") {
  if (value === null || value === undefined || value === "") return "--";
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) return "--";
  const amount = Math.abs(numeric).toLocaleString("zh-CN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  return `${numeric > 0 ? "+" : numeric < 0 ? "-" : ""}${amount} ${currency}`;
}

function performanceMonthKey(value = performanceViewMonth) {
  return `${value.getFullYear()}-${String(value.getMonth() + 1).padStart(2, "0")}`;
}

function performanceTone(value) {
  if (value === null || value === undefined || value === "") return "flat";
  const numeric = Number(value);
  if (!Number.isFinite(numeric) || Math.abs(numeric) < 0.005) return "flat";
  return numeric > 0 ? "profit" : "loss";
}

function normalizeDailyEntries(value) {
  if (Array.isArray(value)) return value;
  if (value && typeof value === "object") {
    return Object.entries(value).map(([date, entry]) => (
      entry && typeof entry === "object" ? { date, ...entry } : { date, return_pct: entry }
    ));
  }
  return [];
}

function normalizeDashboardPerformance(payload, fallbackSource = "模拟盘") {
  const root = payload?.performance && typeof payload.performance === "object" ? payload.performance : (payload || {});
  const summary = root.summary || root.metrics || {};
  const account = root.account || {};
  const stats = root.stats || {};
  const trades = Array.isArray(root.trades) ? root.trades : [];
  const curve = Array.isArray(root.curve) ? root.curve : (Array.isArray(root.equity_curve) ? root.equity_curve : []);
  const daily = new Map();

  const explicitDaily = normalizeDailyEntries(root.daily_returns || root.daily || root.calendar?.days || root.calendar || root.returns_calendar?.days);
  explicitDaily.forEach((entry) => {
    const date = performanceDateKey(entry.date ?? entry.day ?? entry.ts ?? entry.timestamp);
    const returnPct = firstFinite(entry.return_pct, entry.ret_pct, entry.pnl_pct, entry.return);
    const pnl = firstFinite(entry.pnl, entry.net_pnl);
    if (!date || (returnPct === null && pnl === null)) return;
    daily.set(date, {
      date,
      returnPct,
      pnl,
      trades: firstFinite(entry.trades, entry.trade_count, entry.events) ?? 0,
      wins: firstFinite(entry.wins) ?? 0,
      losses: firstFinite(entry.losses) ?? 0,
      explicit: true,
    });
  });

  const curvePoints = curve.map((point) => {
    const timestamp = Array.isArray(point) ? point[0] : point?.ts ?? point?.timestamp ?? point?.date;
    const equity = firstFinite(Array.isArray(point) ? point[1] : point?.equity, point?.value, point?.balance);
    const date = performanceDate(timestamp);
    return date && equity !== null ? { date, equity } : null;
  }).filter(Boolean).sort((left, right) => left.date - right.date);
  let previousEquity = null;
  const curveDays = new Map();
  curvePoints.forEach((point) => {
    const date = performanceDateKey(point.date);
    if (!curveDays.has(date)) curveDays.set(date, { start: previousEquity ?? point.equity, end: point.equity });
    curveDays.get(date).end = point.equity;
    previousEquity = point.equity;
  });
  curveDays.forEach((entry, date) => {
    if (daily.has(date) || !entry.start) return;
    daily.set(date, { date, returnPct: (entry.end / entry.start - 1) * 100, pnl: entry.end - entry.start, trades: 0, curve: true });
  });

  const startingEquity = firstFinite(account.start, account.initial_capital, summary.start_equity, root.start_equity) || 10000;
  let grossProfit = 0;
  let grossLoss = 0;
  let calculatedWins = 0;
  trades.forEach((trade) => {
    const date = performanceDateKey(trade.closed_ts ?? trade.exit_ts ?? trade.exit_at ?? trade.closed_at);
    const pnl = firstFinite(trade.net_pnl, trade.pnl, trade.profit);
    if (!date || pnl === null) return;
    const netPnl = trade.net_pnl !== undefined ? pnl : pnl - (firstFinite(trade.fee, trade.fees) || 0);
    if (netPnl > 0) {
      grossProfit += netPnl;
      calculatedWins += 1;
    } else grossLoss += Math.abs(netPnl);
    const entry = daily.get(date) || { date, returnPct: 0, pnl: 0, trades: 0 };
    entry.trades = Number(entry.trades || 0) + 1;
    if (!entry.curve && !entry.explicit) {
      entry.pnl = Number(entry.pnl || 0) + netPnl;
      entry.returnPct = entry.pnl / startingEquity * 100;
    }
    daily.set(date, entry);
  });

  const dailyReturns = [...daily.values()].sort((left, right) => left.date.localeCompare(right.date));
  const tradeCount = firstFinite(summary.trade_count, summary.trades, summary.income_records, stats.trades, root.trade_count) ?? trades.length;
  const wins = firstFinite(summary.wins, stats.wins) ?? calculatedWins;
  const losses = firstFinite(summary.losses, stats.losses) ?? Math.max(0, tradeCount - wins);
  const breakeven = firstFinite(summary.breakeven, stats.breakeven) ?? 0;
  let totalReturn = firstFinite(summary.total_return_pct, summary.return_pct, root.total_return_pct, account.ret_pct);
  const percentageDays = dailyReturns.filter((day) => day.returnPct !== null && day.returnPct !== undefined);
  if (totalReturn === null && percentageDays.length) {
    totalReturn = (percentageDays.reduce((value, day) => value * (1 + day.returnPct / 100), 1) - 1) * 100;
  }
  let winRate = firstFinite(summary.win_rate_pct, summary.win_rate, root.win_rate_pct, stats.win_rate);
  if (winRate === null && tradeCount) winRate = wins / tradeCount * 100;
  let profitFactor = firstFinite(summary.profit_factor, summary.profit_loss_ratio, root.profit_factor, stats.profit_factor);
  if (profitFactor === null && grossLoss > 0) profitFactor = grossProfit / grossLoss;
  const maxDrawdown = firstFinite(summary.max_drawdown_pct, summary.max_drawdown, root.max_drawdown_pct, stats.max_drawdown);
  return {
    totalReturn,
    winRate,
    profitFactor,
    maxDrawdown,
    tradeCount,
    wins,
    losses,
    breakeven,
    totalPnl: firstFinite(summary.total_pnl, summary.net_income, root.total_pnl, root.net_income),
    realizedPnl: firstFinite(summary.realized_pnl, root.realized_pnl),
    unrealizedPnl: firstFinite(summary.unrealized_pnl, root.unrealized_pnl),
    averageProfit: firstFinite(summary.average_profit, root.average_profit),
    profitFactorStatus: summary.profit_factor_status || root.profit_factor_status || "",
    startEquity: firstFinite(account.start, account.initial_capital, summary.start_equity, root.start_equity),
    currentEquity: firstFinite(account.equity, account.final_equity, summary.current_equity, root.current_equity),
    dailyReturns,
    source: root.source === "paper_account" || root.scope === "user_account"
      ? "个人模拟盘 · 独立"
      : String(root.source_label || root.source || fallbackSource).slice(0, 30),
    scope: root.scope || "user_account",
    currency: String(root.currency || "USDT").toUpperCase().slice(0, 12),
    calendarMonth: root.calendar?.month || root.month || "",
    monthPnl: firstFinite(root.calendar?.total_pnl, root.month_pnl),
    activeDays: firstFinite(root.calendar?.active_days, root.active_days),
    timezoneLabel: root.calendar?.timezone_label || "",
    stale: Boolean(root.stale),
    dataAsOf: root.data_as_of || "",
    periodStart: root.period_start || summary.period_start,
    periodEnd: root.period_end || summary.period_end,
  };
}

async function requestDashboardPerformance(month = performanceViewMonth) {
  const monthQuery = encodeURIComponent(performanceMonthKey(month));
  const timezoneOffset = -new Date(month.getFullYear(), month.getMonth(), 15).getTimezoneOffset();
  const performance = await api(`/api/v2/dashboard/performance?month=${monthQuery}&timezone_offset_minutes=${timezoneOffset}`);
  return normalizeDashboardPerformance(performance, "个人模拟盘 · 独立");
}

function normalizeBinanceAssetPerformance(value) {
  const asset = String(value?.asset || "").trim().toUpperCase().slice(0, 32);
  const dailyReturns = normalizeDailyEntries(value?.days).map((entry) => {
    const date = performanceDateKey(entry.date);
    const pnl = firstFinite(entry.net_income, entry.pnl);
    if (!date || pnl === null) return null;
    return {
      date,
      returnPct: null,
      pnl,
      trades: firstFinite(entry.realized_records, entry.events) ?? 0,
      wins: firstFinite(entry.wins) ?? 0,
      losses: firstFinite(entry.losses) ?? 0,
      breakeven: firstFinite(entry.breakeven) ?? 0,
      explicit: true,
    };
  }).filter(Boolean).sort((left, right) => left.date.localeCompare(right.date));
  return {
    asset,
    netIncome: firstFinite(value?.net_income),
    realizedPnl: firstFinite(value?.realized_pnl),
    fundingFee: firstFinite(value?.funding_fee),
    commission: firstFinite(value?.commission),
    unrealizedPnl: firstFinite(value?.current_unrealized_pnl),
    tradeCount: firstFinite(value?.realized_records) ?? 0,
    wins: firstFinite(value?.wins) ?? 0,
    losses: firstFinite(value?.losses) ?? 0,
    breakeven: firstFinite(value?.breakeven) ?? 0,
    winRate: firstFinite(value?.win_rate_pct),
    profitFactor: firstFinite(value?.profit_factor),
    profitFactorStatus: value?.profit_factor_status || "",
    dailyReturns,
  };
}

function withBinancePerformanceAsset(performance, requestedAsset = binancePerformanceAsset) {
  const assets = performance.assetViews || [];
  const normalizedRequest = String(requestedAsset || "").toUpperCase();
  const selected = assets.find((entry) => entry.asset === normalizedRequest)
    || assets.find((entry) => entry.asset === "USDT")
    || assets.find((entry) => ["USDC", "BUSD"].includes(entry.asset))
    || assets[0]
    || null;
  binancePerformanceAsset = selected?.asset || "";
  return {
    ...performance,
    selectedAsset: binancePerformanceAsset,
    currency: selected?.asset || performance.account?.currency || "USDT",
    totalPnl: selected?.netIncome ?? null,
    realizedPnl: selected?.realizedPnl ?? null,
    unrealizedPnl: selected?.unrealizedPnl ?? null,
    fundingFee: selected?.fundingFee ?? null,
    commission: selected?.commission ?? null,
    tradeCount: selected?.tradeCount ?? 0,
    wins: selected?.wins ?? 0,
    losses: selected?.losses ?? 0,
    breakeven: selected?.breakeven ?? 0,
    winRate: selected?.winRate ?? null,
    profitFactor: selected?.profitFactor ?? null,
    profitFactorStatus: selected?.profitFactorStatus || "",
    dailyReturns: selected?.dailyReturns || [],
    monthPnl: selected?.netIncome ?? null,
  };
}

function normalizeBinanceDashboardPerformance(payload) {
  const root = payload || {};
  const configured = Boolean(root.configured ?? root.account?.configured);
  const connected = Boolean(root.connected ?? root.account?.connected);
  const rawAccount = root.account && typeof root.account === "object" ? root.account : null;
  const account = {
    ...(rawAccount || {}),
    configured,
    connected,
    error_category: root.error_category || rawAccount?.error_category || "",
  };
  const assetViews = (Array.isArray(root.assets) ? root.assets : [])
    .map(normalizeBinanceAssetPerformance)
    .filter((entry) => entry.asset);
  const fallback = normalizeDashboardPerformance(payload, "Binance 实盘");
  const performance = {
    ...fallback,
    configured,
    connected,
    source: "Binance 实盘",
    accountType: root.account_type || account?.account_type || "",
    errorCategory: root.error_category || account?.error_category || "",
    historyStatus: root.history_status || (connected ? "available" : "request_failed"),
    historyComplete: Boolean(root.history_complete),
    monthComplete: Boolean(root.month_complete),
    account,
    assetViews,
    selectedAsset: "",
    currency: String(root.currency || account?.currency || fallback.currency || "USDT").toUpperCase().slice(0, 12),
    fundingFee: firstFinite(root.metrics?.funding_fee, root.funding_fee),
    commission: firstFinite(root.metrics?.commission, root.commission),
    truncated: Boolean(root.truncated) || (root.history_status === "available" && root.history_complete === false),
    totalsByAsset: root.totals_by_asset && typeof root.totals_by_asset === "object"
      ? root.totals_by_asset
      : Object.fromEntries(assetViews.map((entry) => [entry.asset, entry])),
    generatedAt: root.generated_at || "",
    dataAsOf: root.data_as_of || fallback.dataAsOf,
    calendarMonth: root.calendar?.month || root.month || fallback.calendarMonth,
    timezoneLabel: root.calendar?.timezone_label || root.timezone_label || fallback.timezoneLabel,
    calendarBasis: root.calendar?.basis || root.income_basis || "",
    recordsIncluded: firstFinite(root.records_included) ?? fallback.tradeCount,
    aggregationPolicy: root.aggregation_policy || "",
  };
  return assetViews.length ? withBinancePerformanceAsset(performance) : performance;
}

async function requestBinanceDashboardPerformance(month = performanceViewMonth) {
  const monthQuery = encodeURIComponent(performanceMonthKey(month));
  const timezoneOffset = -new Date(month.getFullYear(), month.getMonth(), 15).getTimezoneOffset();
  const performance = await api(`/api/v2/dashboard/binance-performance?month=${monthQuery}&timezone_offset_minutes=${timezoneOffset}`);
  return normalizeBinanceDashboardPerformance(performance);
}

function setPerformanceLoading() {
  $("#virtual-performance-panel").setAttribute("aria-busy", "true");
  $$("#virtual-performance-panel .performance-metric").forEach((card) => card.classList.add("loading"));
  $("#performance-total-return").textContent = "--";
  $("#performance-win-rate").textContent = "--";
  $("#performance-profit-factor").textContent = "--";
  $("#performance-drawdown").textContent = "--";
  $("#performance-status").className = "performance-status loading";
  $("#performance-status").textContent = "正在加载绩效数据…";
  setPerformanceControlsLoading(true);
}

function setPerformanceControlsLoading(loading) {
  ["#calendar-prev", "#calendar-next", "#calendar-today", "#performance-refresh"].forEach((id) => {
    $(id).disabled = loading;
  });
  if (!loading) {
    updatePerformanceMonthControls();
    $("#performance-refresh").disabled = false;
  }
}

function setBinancePerformanceLoading() {
  $("#binance-performance-panel").setAttribute("aria-busy", "true");
  $$("#binance-performance-panel .performance-metric").forEach((card) => card.classList.add("loading"));
  $("#binance-performance-net-income").textContent = "--";
  $("#binance-performance-realized").textContent = "--";
  $("#binance-performance-win-rate").textContent = "--";
  $("#binance-performance-profit-factor").textContent = "--";
  $("#binance-performance-status").className = "performance-status loading";
  $("#binance-performance-status").textContent = "正在加载 Binance 实盘收益…";
  $("#binance-performance-callout").classList.add("hidden");
  $("#binance-performance-asset").disabled = true;
}

function resetPerformancePanel() {
  performanceRequestVersion += 1;
  dashboardPerformance = null;
  binanceDashboardPerformance = null;
  currentUserHasBinanceCredentials = false;
  binancePerformanceAsset = "";
  performanceViewMonth = monthStart(new Date());
  $("#performance-dashboard").setAttribute("aria-busy", "true");
  setPerformanceLoading();
  setBinancePerformanceLoading();
  $("#performance-source").textContent = "个人模拟盘 · 独立";
  $("#binance-performance-source").textContent = "检查中";
  renderPerformanceCalendar();
  renderBinancePerformanceCalendar();
}

function setMetricValue(id, value, tone = "flat") {
  const target = $(id);
  target.textContent = value;
  target.className = tone;
}

function renderDashboardPerformance(performance) {
  dashboardPerformance = performance;
  if (/^\d{4}-\d{2}$/.test(performance.calendarMonth || "")) performanceViewMonth = monthStart(`${performance.calendarMonth}-01`);
  $("#virtual-performance-panel").setAttribute("aria-busy", "false");
  $$("#virtual-performance-panel .performance-metric").forEach((card) => card.classList.remove("loading"));
  const hasTrades = Number(performance.tradeCount) > 0;
  const winRate = firstFinite(performance.winRate);
  setMetricValue("#performance-total-return", formatPerformancePnl(performance.totalPnl, performance.currency), performanceTone(performance.totalPnl));
  setMetricValue("#performance-win-rate", hasTrades && winRate !== null ? formatPerformancePercent(winRate, false) : "--", hasTrades && winRate !== null ? performanceTone(winRate - 50) : "flat");
  const factor = firstFinite(performance.profitFactor);
  const noLosses = hasTrades && performance.profitFactorStatus === "no_losses";
  const factorText = noLosses ? "∞ : 1" : hasTrades && Number.isFinite(factor) ? `${factor.toFixed(2)} : 1` : "--";
  setMetricValue("#performance-profit-factor", factorText, noLosses ? "profit" : hasTrades && Number.isFinite(factor) ? performanceTone(factor - 1) : "flat");
  setMetricValue("#performance-drawdown", performance.maxDrawdown === null ? "--" : formatPerformancePercent(Math.abs(performance.maxDrawdown), false), performance.maxDrawdown === null ? "flat" : "loss");
  const equityNote = performance.totalReturn !== null
    ? `自重置 · 收益率 ${formatPerformancePercent(performance.totalReturn)}`
    : performance.startEquity !== null && performance.currentEquity !== null
    ? `自重置 · ${Number(performance.startEquity).toLocaleString("zh-CN", { maximumFractionDigits: 2 })} → ${Number(performance.currentEquity).toLocaleString("zh-CN", { maximumFractionDigits: 2 })}`
    : `自重置 · ${performance.dailyReturns.length} 个日收益样本`;
  $("#performance-return-note").textContent = equityNote;
  const flatNote = performance.breakeven ? ` / ${performance.breakeven} 平` : "";
  $("#performance-win-note").textContent = hasTrades ? `${performance.wins} 胜 / ${performance.losses} 负${flatNote} · ${performance.tradeCount} 笔平仓` : "暂无平仓记录";
  $("#performance-factor-note").textContent = hasTrades && performance.averageProfit !== null
    ? `平均每笔 ${formatPerformancePnl(performance.averageProfit, performance.currency)}`
    : hasTrades ? "总盈利 / 总亏损" : "产生交易后开始统计";
  $("#performance-drawdown-note").textContent = "自重置以来完整净值峰谷";
  $("#performance-source").textContent = `${performance.source || "模拟盘"}${performance.stale ? " · 数据延迟" : ""}`;
  if (performance.dataAsOf) $("#performance-source").title = `数据时间：${new Date(performance.dataAsOf).toLocaleString("zh-CN", { hour12: false })}`;
  else $("#performance-source").removeAttribute("title");
  const timezoneText = performance.timezoneLabel ? `（${performance.timezoneLabel}）` : "";
  $("#calendar-subtitle").textContent = `按平仓日汇总已实现净 PnL${timezoneText}；持仓浮盈不计入`;
  renderPerformanceCalendar();
}

function renderPerformanceFailure() {
  dashboardPerformance = null;
  $("#virtual-performance-panel").setAttribute("aria-busy", "false");
  $$("#virtual-performance-panel .performance-metric").forEach((card) => card.classList.remove("loading"));
  ["#performance-total-return", "#performance-win-rate", "#performance-profit-factor", "#performance-drawdown"].forEach((id) => setMetricValue(id, "--"));
  $("#performance-return-note").textContent = "暂时无法读取净值";
  $("#performance-win-note").textContent = "暂时无法统计成交";
  $("#performance-factor-note").textContent = "等待数据恢复";
  $("#performance-drawdown-note").textContent = "等待数据恢复";
  $("#performance-source").textContent = "数据不可用";
  renderPerformanceCalendar();
  $("#performance-status").className = "performance-status failed";
  $("#performance-status").textContent = "绩效数据暂时无法加载，请稍后刷新。";
}

function clearBinancePerformanceMetrics() {
  [
    "#binance-performance-net-income",
    "#binance-performance-realized",
    "#binance-performance-win-rate",
    "#binance-performance-profit-factor",
  ].forEach((id) => setMetricValue(id, "--"));
}

function renderBinancePerformanceAssetPicker(performance) {
  const wrapper = $("#binance-performance-asset-wrap");
  const select = $("#binance-performance-asset");
  const assets = performance?.assetViews || [];
  wrapper.classList.toggle("hidden", !assets.length);
  select.disabled = assets.length < 2;
  const options = assets.map((entry) => {
    const option = document.createElement("option");
    option.value = entry.asset;
    option.textContent = entry.asset;
    return option;
  });
  select.replaceChildren(...options);
  if (performance?.selectedAsset) select.value = performance.selectedAsset;
}

function showBinancePerformanceCallout(title, message, buttonText = "", tone = "warning") {
  const callout = $("#binance-performance-callout");
  callout.className = `binance-performance-callout ${tone}`;
  $("#binance-performance-callout-title").textContent = title;
  $("#binance-performance-callout-message").textContent = message;
  const button = $("#binance-performance-configure");
  button.textContent = buttonText || "配置 API 凭据";
  button.classList.toggle("hidden", !buttonText);
}

function binancePerformanceErrorHint(category) {
  return {
    not_configured: "尚未配置 Binance API 凭据，无法读取实盘收益。",
    credential_error: "本地凭据无法解密，请重新配置 Binance API。",
    authentication: "Binance 拒绝了凭据，请检查 API Key 和 IP 白名单。",
    permission: "API 缺少读取收益历史所需权限，请检查 Binance API 权限。",
    forbidden: "Binance 未授权读取收益历史，请检查账户与 API 权限。",
    income_permission: "API 缺少收益历史读取权限，请在 Binance 更新权限。",
    timestamp: "服务器时间与 Binance 不同步，请校准后重试。",
    rate_limit: "Binance 请求受限，请稍后刷新。",
    timeout: "Binance 响应超时，请稍后刷新。",
    network: "服务器暂时无法连接 Binance。",
    upstream: "Binance 收益服务暂时不可用。",
  }[category] || "暂时无法读取 Binance 实盘收益，请检查 API 权限后重试。";
}

function binanceHistoryState(status, errorCategory = "") {
  if (status === "history_unavailable") return {
    title: "所选月份已超出 Binance 历史范围",
    message: "Binance 收益历史仅保留最近约 3 个月；此月份无法提供完整实盘数据。",
    tone: "info",
    source: "历史不可用",
    buttonText: "配置 API 凭据",
  };
  if (status === "history_limited") return {
    title: "所选月份只能取得部分历史",
    message: "该月份跨越 Binance 可查询边界；为避免把部分数据当作整月结果，本页不显示伪完整收益。",
    tone: "info",
    source: "历史不完整",
    buttonText: "配置 API 凭据",
  };
  if (status === "future_month") return {
    title: "所选月份尚未开始",
    message: "未来月份没有可验证的 Binance 实盘收益数据。",
    tone: "info",
    source: "未来月份",
  };
  if (status === "request_failed") return {
    title: "Binance 收益历史读取失败",
    message: binancePerformanceErrorHint(errorCategory),
    tone: "error",
    source: "收益读取异常",
    buttonText: "配置 API 凭据",
  };
  return null;
}

function renderBinanceDashboardPerformance(performance) {
  binanceDashboardPerformance = performance;
  $("#binance-performance-panel").setAttribute("aria-busy", "false");
  $$("#binance-performance-panel .performance-metric").forEach((card) => card.classList.remove("loading"));
  $("#binance-performance-callout").classList.add("hidden");
  if (/^\d{4}-\d{2}$/.test(performance.calendarMonth || "")) performanceViewMonth = monthStart(`${performance.calendarMonth}-01`);
  renderBinancePerformanceAssetPicker(performance);
  renderBinancePerformanceCalendar();

  if (!performance.configured) {
    clearBinancePerformanceMetrics();
    $("#binance-performance-source").textContent = "未配置";
    $("#binance-performance-income-note").textContent = "尚未连接实盘账户";
    $("#binance-performance-realized-note").textContent = "等待配置 API 凭据";
    $("#binance-performance-win-note").textContent = "暂无实盘收益记录";
    $("#binance-performance-factor-note").textContent = "暂无实盘收益记录";
    showBinancePerformanceCallout("尚未配置 Binance API", "配置当前用户的 Binance API 凭据后，才能读取真实收益流水。", "配置 API 凭据");
    $("#binance-performance-status").className = "performance-status empty";
    $("#binance-performance-status").textContent = "实盘未配置；虚拟盘数据不会用于填充此区域。";
    return;
  }

  if (!performance.connected) {
    clearBinancePerformanceMetrics();
    const permissionIssue = ["permission", "forbidden", "income_permission"].includes(performance.errorCategory);
    $("#binance-performance-source").textContent = permissionIssue ? "权限不足" : "连接异常";
    $("#binance-performance-income-note").textContent = "无法读取 Binance 收益";
    $("#binance-performance-realized-note").textContent = "请检查账户连接";
    $("#binance-performance-win-note").textContent = "暂无可验证的实盘记录";
    $("#binance-performance-factor-note").textContent = "暂无可验证的实盘记录";
    const hint = binancePerformanceErrorHint(performance.errorCategory);
    showBinancePerformanceCallout(permissionIssue ? "缺少收益读取权限" : "Binance 实盘连接异常", hint, permissionIssue ? "检查 API 权限" : "检查 API 凭据", "error");
    $("#binance-performance-status").className = "performance-status failed";
    $("#binance-performance-status").textContent = hint;
    return;
  }

  const historyState = binanceHistoryState(performance.historyStatus, performance.errorCategory);
  if (historyState) {
    clearBinancePerformanceMetrics();
    $("#binance-performance-source").textContent = historyState.source;
    $("#binance-performance-income-note").textContent = "未生成可验证的整月结果";
    $("#binance-performance-realized-note").textContent = "不会以 0.00 冒充无收益";
    $("#binance-performance-win-note").textContent = "暂无可用的已实现记录";
    $("#binance-performance-factor-note").textContent = "暂无可用的已实现记录";
    showBinancePerformanceCallout(historyState.title, historyState.message, historyState.buttonText || "", historyState.tone);
    $("#binance-performance-status").className = `performance-status ${historyState.tone === "error" ? "failed" : "empty"}`;
    $("#binance-performance-status").textContent = historyState.message;
    return;
  }

  const hasRecords = Number(performance.tradeCount) > 0;
  const winRate = firstFinite(performance.winRate);
  const factor = firstFinite(performance.profitFactor);
  const noLosses = hasRecords && performance.profitFactorStatus === "no_losses";
  setMetricValue("#binance-performance-net-income", formatPerformancePnl(performance.totalPnl, performance.currency), performanceTone(performance.totalPnl));
  setMetricValue("#binance-performance-realized", formatPerformancePnl(performance.realizedPnl, performance.currency), performanceTone(performance.realizedPnl));
  setMetricValue("#binance-performance-win-rate", hasRecords && winRate !== null ? formatPerformancePercent(winRate, false) : "--", hasRecords && winRate !== null ? performanceTone(winRate - 50) : "flat");
  const factorText = noLosses ? "∞ : 1" : hasRecords && Number.isFinite(factor) ? `${factor.toFixed(2)} : 1` : "--";
  setMetricValue("#binance-performance-profit-factor", factorText, noLosses ? "profit" : hasRecords && Number.isFinite(factor) ? performanceTone(factor - 1) : "flat");
  const costParts = [];
  if (performance.fundingFee !== null) costParts.push(`资金费 ${formatPerformancePnl(performance.fundingFee, performance.currency)}`);
  if (performance.commission !== null) costParts.push(`手续费 ${formatPerformancePnl(performance.commission, performance.currency)}`);
  const otherAssets = Math.max(0, (performance.assetViews?.length || 0) - 1);
  if (performance.selectedAsset) costParts.unshift(`仅显示 ${performance.selectedAsset}`);
  if (otherAssets) costParts.push(`另有 ${otherAssets} 个资产未合并`);
  $("#binance-performance-income-note").textContent = costParts.join(" · ") || "已计入手续费与资金费";
  $("#binance-performance-realized-note").textContent = performance.unrealizedPnl !== null
    ? `未实现 ${formatPerformancePnl(performance.unrealizedPnl, performance.currency)}`
    : "来自 Binance 收益流水";
  const flatNote = performance.breakeven ? ` / ${performance.breakeven} 平` : "";
  $("#binance-performance-win-note").textContent = hasRecords ? `${performance.wins} 胜 / ${performance.losses} 负${flatNote} · ${performance.tradeCount} 条已实现记录` : "暂无已实现收益记录";
  $("#binance-performance-factor-note").textContent = hasRecords ? "盈利记录 / 亏损记录" : "产生已实现记录后开始统计";
  const assetBadge = performance.selectedAsset
    ? `仅显示 ${performance.selectedAsset}${otherAssets ? ` · 另有 ${otherAssets} 个资产` : ""}`
    : `${accountTypeLabel(performance.accountType)} · 已连接`;
  $("#binance-performance-source").textContent = `${assetBadge}${performance.monthComplete ? "" : " · 月度进行中"}${performance.truncated ? " · 部分数据" : ""}`;
  if (performance.dataAsOf) $("#binance-performance-source").title = `${accountTypeLabel(performance.accountType)} · Binance 数据时间：${new Date(performance.dataAsOf).toLocaleString("zh-CN", { hour12: false })}`;
  else $("#binance-performance-source").removeAttribute("title");
  const timezoneText = performance.timezoneLabel ? `（${performance.timezoneLabel}）` : "";
  const assetText = performance.selectedAsset ? `仅汇总 ${performance.selectedAsset}，不跨资产换算；` : "";
  $("#binance-calendar-subtitle").textContent = `${assetText}按收益事件日期汇总净 PnL${timezoneText}`;
  if (performance.truncated) {
    showBinancePerformanceCallout("本月 Binance 数据可能不完整", "接口已返回部分收益历史；指标和日历仅代表当前可取得的数据。", "", "info");
  } else if (!hasRecords && !performance.dailyReturns.length) {
    showBinancePerformanceCallout("本月暂无实盘收益历史", "账户已连接，但所选月份没有 Binance 收益流水。", "配置 API 凭据", "info");
  }
}

function renderBinancePerformanceFailure(configured = currentUserHasBinanceCredentials) {
  const fallback = {
    configured,
    connected: false,
    errorCategory: configured ? "upstream" : "not_configured",
    dailyReturns: [],
    currency: "USDT",
    calendarMonth: performanceMonthKey(),
    monthPnl: null,
  };
  renderBinanceDashboardPerformance(fallback);
}

function performanceMonthBounds() {
  const current = monthStart(new Date());
  return { min: shiftPerformanceMonth(current, -35), max: current };
}

function renderCalendarView({
  performance,
  calendarSelector,
  statusSelector,
  monthTotalSelector,
  daysSummarySelector,
  emptyMessage,
  eventLabel,
  dayLabel,
}) {
  const calendar = $(calendarSelector);
  const year = performanceViewMonth.getFullYear();
  const month = performanceViewMonth.getMonth();
  const today = new Date();
  const todayKey = performanceDateKey(today);
  const byDate = new Map((performance?.dailyReturns || []).map((day) => [day.date, day]));
  const weekdays = document.createElement("div");
  weekdays.className = "calendar-weekdays";
  weekdays.setAttribute("role", "row");
  ["一", "二", "三", "四", "五", "六", "日"].forEach((label) => {
    const header = document.createElement("span");
    header.textContent = label;
    header.setAttribute("role", "columnheader");
    weekdays.append(header);
  });
  const days = document.createElement("div");
  days.className = "calendar-days";
  days.setAttribute("role", "rowgroup");
  const offset = (new Date(year, month, 1).getDay() + 6) % 7;
  const dayCount = new Date(year, month + 1, 0).getDate();
  for (let index = 0; index < offset; index += 1) {
    const blank = document.createElement("span");
    blank.className = "calendar-day blank";
    blank.setAttribute("aria-hidden", "true");
    days.append(blank);
  }
  const monthReturns = [];
  for (let dayNumber = 1; dayNumber <= dayCount; dayNumber += 1) {
    const date = new Date(year, month, dayNumber);
    const key = performanceDateKey(date);
    const value = byDate.get(key);
    const displayValue = value?.returnPct ?? value?.pnl;
    const tone = value ? performanceTone(displayValue) : "empty";
    const cell = document.createElement("div");
    cell.className = `calendar-day ${tone}${key === todayKey ? " today" : ""}`;
    cell.setAttribute("role", "gridcell");
    const number = document.createElement("span");
    number.className = "calendar-day-number";
    number.textContent = String(dayNumber);
    cell.append(number);
    if (key === todayKey) {
      const marker = document.createElement("em");
      marker.textContent = "今天";
      cell.append(marker);
    }
    const result = document.createElement("strong");
    result.textContent = value
      ? (value.returnPct !== null && value.returnPct !== undefined
        ? formatPerformancePercent(value.returnPct)
        : formatPerformancePnl(value.pnl, performance?.currency || "USDT"))
      : "--";
    cell.append(result);
    const tradeCount = Number(value?.trades || 0);
    const note = document.createElement("small");
    note.textContent = value ? (tradeCount ? `${tradeCount} ${eventLabel}` : "当日汇总") : "无数据";
    cell.append(note);
    const accessibleDate = `${year}年${month + 1}月${dayNumber}日`;
    cell.setAttribute("aria-label", value ? `${accessibleDate}，盈亏 ${result.textContent}，${note.textContent}` : `${accessibleDate}，无收益数据`);
    if (value) monthReturns.push(value);
    days.append(cell);
  }
  const totalCells = offset + dayCount;
  for (let index = totalCells; index % 7 !== 0; index += 1) {
    const blank = document.createElement("span");
    blank.className = "calendar-day blank";
    blank.setAttribute("aria-hidden", "true");
    days.append(blank);
  }
  calendar.replaceChildren(weekdays, days);
  const responseMonthMatches = performance?.calendarMonth === performanceMonthKey(performanceViewMonth);
  const monthPnl = responseMonthMatches && performance?.monthPnl != null
    ? performance.monthPnl
    : (monthReturns.length ? monthReturns.reduce((sum, day) => sum + (Number(day.pnl) || 0), 0) : null);
  setMetricValue(monthTotalSelector, monthPnl === null ? "--" : formatPerformancePnl(monthPnl, performance?.currency || "USDT"), performanceTone(monthPnl));
  const profitDays = monthReturns.filter((day) => Number(day.returnPct ?? day.pnl) > 0).length;
  const lossDays = monthReturns.filter((day) => Number(day.returnPct ?? day.pnl) < 0).length;
  $(daysSummarySelector).textContent = monthReturns.length ? `${monthReturns.length} 个${dayLabel} · ${profitDays} 盈 / ${lossDays} 亏` : `0 个${dayLabel}`;
  const status = $(statusSelector);
  if (performance) {
    status.className = `performance-status${monthReturns.length ? " hidden" : " empty"}`;
    status.textContent = monthReturns.length ? "" : emptyMessage;
  }
}

function updatePerformanceMonthControls() {
  const year = performanceViewMonth.getFullYear();
  const month = performanceViewMonth.getMonth();
  const today = new Date();
  $("#calendar-month").textContent = `${year} 年 ${month + 1} 月`;
  const bounds = performanceMonthBounds();
  $("#calendar-prev").disabled = performanceViewMonth <= bounds.min;
  $("#calendar-next").disabled = performanceViewMonth >= bounds.max;
  $("#calendar-today").disabled = year === today.getFullYear() && month === today.getMonth();
}

function renderPerformanceCalendar() {
  renderCalendarView({
    performance: dashboardPerformance,
    calendarSelector: "#returns-calendar",
    statusSelector: "#performance-status",
    monthTotalSelector: "#calendar-month-return",
    daysSummarySelector: "#calendar-trading-days",
    emptyMessage: "本月尚无平仓记录；持仓浮盈不计入此日历。",
    eventLabel: "笔平仓",
    dayLabel: "平仓日",
  });
  updatePerformanceMonthControls();
}

function renderBinancePerformanceCalendar() {
  renderCalendarView({
    performance: binanceDashboardPerformance,
    calendarSelector: "#binance-returns-calendar",
    statusSelector: "#binance-performance-status",
    monthTotalSelector: "#binance-calendar-month-return",
    daysSummarySelector: "#binance-calendar-trading-days",
    emptyMessage: "本月没有可用的 Binance 收益流水。",
    eventLabel: "条已实现",
    dayLabel: "收益日",
  });
}

async function refreshDashboardPerformance() {
  const requestVersion = ++performanceRequestVersion;
  renderPerformanceCalendar();
  renderBinancePerformanceCalendar();
  $("#performance-dashboard").setAttribute("aria-busy", "true");
  setPerformanceLoading();
  setBinancePerformanceLoading();
  const [virtualResult, binanceResult] = await Promise.allSettled([
    requestDashboardPerformance(),
    requestBinanceDashboardPerformance(),
  ]);
  if (requestVersion !== performanceRequestVersion) return;

  if (virtualResult.status === "fulfilled") renderDashboardPerformance(virtualResult.value);
  else renderPerformanceFailure();

  if (binanceResult.status === "fulfilled") {
    currentUserHasBinanceCredentials = binanceResult.value.configured;
    renderBinanceDashboardPerformance(binanceResult.value);
  } else {
    renderBinancePerformanceFailure();
  }
  $("#performance-dashboard").setAttribute("aria-busy", "false");
  setPerformanceControlsLoading(false);
}

function renderBinanceAccount(account, configuredFallback = false) {
  const card = $("#binance-account-card");
  const state = $("#binance-account-state");
  const wallet = $("#binance-wallet-balance");
  const detail = $("#binance-account-detail");
  const action = $("#binance-account-action");
  const configured = Boolean(account?.configured ?? configuredFallback);
  const connected = Boolean(account?.connected);
  card.className = "metric-card account-metric-card";
  action.classList.remove("hidden");

  if (!configured) {
    card.classList.add("unconfigured");
    state.textContent = "未配置";
    wallet.textContent = "尚未配置";
    detail.textContent = "配置 API 凭据后可读取钱包与可用余额";
    detail.removeAttribute("title");
    action.textContent = "去配置";
    action.dataset.accountAction = "settings";
    return;
  }

  if (!connected) {
    card.classList.add("connection-error");
    state.textContent = "连接异常";
    wallet.textContent = "暂时无法读取";
    const errorHint = {
      credential_error: "本地凭据无法解密，请重新配置",
      authentication: "请检查 API Key、IP 白名单及合约读取权限",
      timestamp: "服务器时间不同步，请校准时间后重试",
      rate_limit: "Binance 请求受限，请稍后再试",
      timeout: "Binance 响应超时，请稍后再试",
      network: "服务器暂时无法连接 Binance",
      upstream: "Binance 服务暂时不可用",
      invalid_response: "Binance 返回了无法识别的账户数据",
    }[account?.error_category];
    detail.textContent = errorHint || "请稍后重试，或检查 API 权限与服务器网络";
    detail.removeAttribute("title");
    const credentialIssue = ["credential_error", "authentication"].includes(account?.error_category);
    action.textContent = credentialIssue ? "检查 API 凭据" : "重新连接";
    action.dataset.accountAction = credentialIssue ? "settings" : "retry";
    return;
  }

  const asset = String(account.valuation_currency || account.currency || account.asset || "USD").trim().toUpperCase().slice(0, 12) || "USD";
  card.classList.add("connected");
  state.textContent = "已连接";
  wallet.textContent = formatAccountAmount(account.wallet_balance, asset);
  const available = formatAccountAmount(account.available_balance, asset);
  const detailParts = [accountTypeLabel(account.account_type), `可用 ${available}`];
  if (account.unrealized_pnl !== null && account.unrealized_pnl !== undefined) {
    detailParts.push(`未实现盈亏 ${formatAccountAmount(account.unrealized_pnl, asset, true)}`);
  }
  detail.textContent = detailParts.join(" · ");
  const dataTime = account.updated_at || account.fetched_at;
  if (dataTime) detail.title = `账户数据时间：${new Date(dataTime).toLocaleString("zh-CN", { hour12: false })}`;
  else detail.removeAttribute("title");
  action.classList.add("hidden");
  action.dataset.accountAction = "settings";
}

async function refreshBinanceAccount(configuredFallback = false) {
  const requestVersion = ++binanceAccountRequestVersion;
  resetBinanceAccount();
  try {
    const account = await api("/api/v2/me/binance-account");
    if (requestVersion !== binanceAccountRequestVersion) return null;
    renderBinanceAccount(account, configuredFallback);
    return account;
  } catch (_) {
    if (requestVersion !== binanceAccountRequestVersion) return null;
    renderBinanceAccount({ configured: configuredFallback, connected: false }, configuredFallback);
    return null;
  }
}

async function loadDashboard() {
  let user;
  try {
    user = await api("/api/v2/me");
  } catch (_) {
    accessToken = "";
    showLoginRoute({ preserveNext: true });
    finishAuthBoot();
    return false;
  }

  if (authenticatedUserId && String(user.id) !== authenticatedUserId) {
    rejectChangedIdentity(user);
  }
  rememberAuthenticatedUser(user);

  $("#username").textContent = user.username;
  $("#sidebar-username").textContent = user.username;
  $("#user-avatar").textContent = user.username.slice(0, 2);
  currentUserHasBinanceCredentials = Boolean(user.binance_credentials_configured);
  updateCredentialStatus(user.binance_credentials_configured, user.binance_key_fingerprint || "");
  const authenticatedSession = ++authSessionVersion;
  setAuthenticated(true);
  restoreAuthenticatedRoute();
  finishAuthBoot();

  resetBinanceAccount();
  const performanceVersion = ++performanceRequestVersion;
  $("#performance-dashboard").setAttribute("aria-busy", "true");
  setPerformanceLoading();
  setBinancePerformanceLoading();

  try {
    const binancePerformancePromise = requestBinanceDashboardPerformance();
    const accountRequest = api("/api/v2/me/binance-account")
      .then((account) => ({ ok: true, account }))
      .catch(() => ({ ok: false, account: null }));
    const performanceRequest = requestDashboardPerformance()
      .then((performance) => ({ ok: true, performance }))
      .catch(() => ({ ok: false, performance: null }));
    const binancePerformanceRequest = binancePerformancePromise
      .then((performance) => ({ ok: true, performance }))
      .catch(() => ({ ok: false, performance: null }));
    const [, accountResult, performanceResult, binancePerformanceResult] = await Promise.all([
      api("/api/v2/health").catch(() => null), accountRequest, performanceRequest, binancePerformanceRequest,
    ]);

    if (!isAuthenticated || authSessionVersion !== authenticatedSession) return true;
    $("#username").textContent = user.username;
    $("#sidebar-username").textContent = user.username;
    $("#user-avatar").textContent = user.username.slice(0, 2);
    currentUserHasBinanceCredentials = Boolean(user.binance_credentials_configured);
    updateCredentialStatus(user.binance_credentials_configured, user.binance_key_fingerprint || "");
    if (accountResult.ok) renderBinanceAccount(accountResult.account, user.binance_credentials_configured);
    else renderBinanceAccount({ configured: user.binance_credentials_configured, connected: false }, user.binance_credentials_configured);
    if (performanceVersion === performanceRequestVersion) {
      if (performanceResult.ok) renderDashboardPerformance(performanceResult.performance);
      else renderPerformanceFailure();
      if (binancePerformanceResult.ok) {
        currentUserHasBinanceCredentials = binancePerformanceResult.performance.configured;
        renderBinanceDashboardPerformance(binancePerformanceResult.performance);
      } else {
        renderBinancePerformanceFailure(user.binance_credentials_configured);
      }
      $("#performance-dashboard").setAttribute("aria-busy", "false");
      setPerformanceControlsLoading(false);
    }
  } catch (_) {
    if (!isAuthenticated || authSessionVersion !== authenticatedSession) return true;
    renderBinanceAccount({ configured: user.binance_credentials_configured, connected: false }, user.binance_credentials_configured);
    if (performanceVersion === performanceRequestVersion) {
      renderPerformanceFailure();
      renderBinancePerformanceFailure(user.binance_credentials_configured);
      $("#performance-dashboard").setAttribute("aria-busy", "false");
      setPerformanceControlsLoading(false);
    }
  }
  return true;
}

$("#tab-login").addEventListener("click", () => {
  $("#tab-login").classList.add("active");
  $("#tab-login").setAttribute("aria-selected", "true");
  $("#tab-register").classList.remove("active");
  $("#tab-register").setAttribute("aria-selected", "false");
  $("#login-form").classList.remove("hidden");
  $("#register-form").classList.add("hidden");
  showMessage($("#auth-message"), "");
});

$("#tab-register").addEventListener("click", () => {
  $("#tab-register").classList.add("active");
  $("#tab-register").setAttribute("aria-selected", "true");
  $("#tab-login").classList.remove("active");
  $("#tab-login").setAttribute("aria-selected", "false");
  $("#register-form").classList.remove("hidden");
  $("#login-form").classList.add("hidden");
  showMessage($("#auth-message"), "");
});

$("#register-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const formElement = event.currentTarget;
  const form = new FormData(formElement);
  try {
    await api("/api/v2/auth/register", {
      method: "POST",
      body: JSON.stringify({
        username: form.get("username"),
        email: form.get("email") || null,
        password: form.get("password"),
      }),
    });
    formElement.reset();
    $("#tab-login").click();
    showMessage($("#auth-message"), "注册成功，请使用新账户登录。", "success");
  } catch (error) {
    showMessage($("#auth-message"), error.message, "error");
  }
});

$("#login-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const formElement = event.currentTarget;
  const form = new FormData(formElement);
  try {
    const data = await api("/api/v2/auth/login", {
      method: "POST",
      body: JSON.stringify({
        username: form.get("username"),
        password: form.get("password"),
        client_type: "web",
      }),
    });
    clearAuthenticatedUser();
    accessToken = data.access_token;
    formElement.reset();
    await loadDashboard();
  } catch (error) {
    showMessage($("#auth-message"), error.message, "error");
  }
});

$("#credential-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const formElement = event.currentTarget;
  const form = new FormData(formElement);
  try {
    const data = await api("/api/v2/me/binance-credentials", {
      method: "PUT",
      body: JSON.stringify({
        api_key: form.get("api_key"),
        api_secret: form.get("api_secret"),
        permissions: ["READ", "TRADE"],
      }),
    });
    formElement.reset();
    currentUserHasBinanceCredentials = true;
    updateCredentialStatus(true, data.fingerprint);
    showMessage($("#dashboard-message"), "凭据已加密保存。", "success");
    await refreshBinanceAccount(true);
    await refreshDashboardPerformance();
  } catch (error) {
    showMessage($("#dashboard-message"), error.message, "error");
  }
});

$("#delete-credentials").addEventListener("click", async () => {
  if (!window.confirm("确定删除当前用户的 Binance API 凭据？")) return;
  try {
    await api("/api/v2/me/binance-credentials", { method: "DELETE" });
    currentUserHasBinanceCredentials = false;
    updateCredentialStatus(false);
    renderBinanceAccount({ configured: false, connected: false });
    renderBinancePerformanceFailure(false);
    showMessage($("#dashboard-message"), "凭据已删除。", "success");
  } catch (error) {
    showMessage($("#dashboard-message"), error.message, "error");
  }
});

$("#ai-model-create").addEventListener("click", () => openAiModelDialog());
$("#ai-model-dialog-close").addEventListener("click", closeAiModelDialog);
$("#ai-model-cancel").addEventListener("click", closeAiModelDialog);
$("#ai-model-dialog").addEventListener("cancel", (event) => {
  event.preventDefault();
  closeAiModelDialog();
});
$("#ai-model-dialog").addEventListener("click", (event) => {
  if (event.target === event.currentTarget) closeAiModelDialog();
});
$("#ai-model-provider").addEventListener("change", () => {
  $("#ai-model-name").value = "";
  updateAiModelProviderDetails(true);
});
$("#ai-model-enabled").addEventListener("change", (event) => {
  if (!event.currentTarget.checked) $("#ai-model-default").checked = false;
});
$("#ai-model-default").addEventListener("change", (event) => {
  if (event.currentTarget.checked) $("#ai-model-enabled").checked = true;
});
$("#ai-model-form").addEventListener("submit", saveAiModelConfig);
$("#ai-model-config-list").addEventListener("click", (event) => {
  const button = event.target.closest("button[data-ai-action]");
  if (button) void runAiModelConfigAction(button);
});

$("#logout").addEventListener("click", async () => {
  await fetch("/api/v2/auth/logout", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: "{}",
    credentials: "include",
  }).catch(() => {});
  accessToken = "";
  clearAuthenticatedUser();
  showLoginRoute({ preserveNext: false });
});

$$('[data-theme-toggle]').forEach((toggle) => toggle.addEventListener("click", () => {
  const nextTheme = document.documentElement.dataset.theme === "light" ? "dark" : "light";
  applyTheme(nextTheme, { persist: true, notify: true });
}));

$("#menu-toggle").addEventListener("click", () => {
  const open = !$("#sidebar").classList.contains("open");
  $("#sidebar").classList.toggle("open", open);
  $("#sidebar-backdrop").classList.toggle("open", open);
  $("#menu-toggle").setAttribute("aria-expanded", String(open));
});

$("#sidebar-backdrop").addEventListener("click", closeSidebar);
$("#performance-refresh").addEventListener("click", refreshDashboardPerformance);
$("#binance-performance-asset").addEventListener("change", (event) => {
  if (!binanceDashboardPerformance) return;
  renderBinanceDashboardPerformance(withBinancePerformanceAsset(binanceDashboardPerformance, event.currentTarget.value));
});
$("#calendar-prev").addEventListener("click", () => {
  performanceViewMonth = shiftPerformanceMonth(performanceViewMonth, -1);
  refreshDashboardPerformance();
});
$("#calendar-next").addEventListener("click", () => {
  performanceViewMonth = shiftPerformanceMonth(performanceViewMonth, 1);
  refreshDashboardPerformance();
});
$("#calendar-today").addEventListener("click", () => {
  performanceViewMonth = monthStart(new Date());
  refreshDashboardPerformance();
});
$$("[data-panel-target]").forEach((item) => item.addEventListener("click", (event) => {
  if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
  event.preventDefault();
  openPanel(item.dataset.panelTarget);
}));
$$("[data-open-panel]").forEach((item) => item.addEventListener("click", () => {
  if (item.dataset.accountAction === "retry") {
    refreshBinanceAccount(true);
    return;
  }
  openPanel(item.dataset.openPanel);
}));
document.addEventListener("keydown", (event) => { if (event.key === "Escape") closeSidebar(); });
document.addEventListener("quantdesk:navigate", (event) => openPanel(event.detail));
window.addEventListener("popstate", handleRouteChange);

(async () => {
  try {
    if (!accessToken) await refreshAccess();
    if (accessToken) await loadDashboard();
    else showLoginRoute({ preserveNext: true });
  } catch (_) {
    accessToken = "";
    showLoginRoute({ preserveNext: true });
  } finally {
    finishAuthBoot();
  }
})();
