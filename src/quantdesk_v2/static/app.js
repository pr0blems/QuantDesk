const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => document.querySelectorAll(selector);
let accessToken = "";

const panelNames = {
  overview: "工作台",
  credentials: "API 凭据",
  strategies: "策略中心",
  orders: "订单与持仓",
  risk: "风险控制",
  audit: "审计日志",
};

function showMessage(target, message, kind = "") {
  target.textContent = message;
  target.className = `message ${kind}`.trim();
}

async function api(path, options = {}, retry = true) {
  const headers = new Headers(options.headers || {});
  if (options.body && !headers.has("Content-Type")) headers.set("Content-Type", "application/json");
  if (accessToken) headers.set("Authorization", `Bearer ${accessToken}`);
  const response = await fetch(path, { ...options, headers, credentials: "include" });
  if (response.status === 401 && retry && !path.includes("/auth/")) {
    const refreshed = await refreshAccess();
    if (refreshed) return api(path, options, false);
  }
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(payload.detail || "请求失败");
  return payload;
}

async function refreshAccess() {
  try {
    const response = await fetch("/api/v2/auth/refresh", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: "{}",
      credentials: "include",
    });
    if (!response.ok) return false;
    const data = await response.json();
    accessToken = data.access_token;
    return true;
  } catch (_) {
    return false;
  }
}

function closeSidebar() {
  $("#sidebar").classList.remove("open");
  $("#sidebar-backdrop").classList.remove("open");
  $("#menu-toggle").setAttribute("aria-expanded", "false");
}

function openPanel(name) {
  const selected = panelNames[name] ? name : "overview";
  if (selected !== "credentials") $("#credential-form").reset();
  $$("[data-panel]").forEach((panel) => panel.classList.toggle("hidden", panel.dataset.panel !== selected));
  $$("[data-panel-target]").forEach((item) => {
    const active = item.dataset.panelTarget === selected;
    item.classList.toggle("active", active);
    if (active) item.setAttribute("aria-current", "page");
    else item.removeAttribute("aria-current");
  });
  $("#mobile-title").textContent = panelNames[selected];
  closeSidebar();
  const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  window.scrollTo({ top: 0, behavior: reducedMotion ? "auto" : "smooth" });
}

function setAuthenticated(authenticated) {
  $("#login-page").classList.toggle("hidden", authenticated);
  $("#app-shell").classList.toggle("hidden", !authenticated);
  if (!authenticated) {
    $("#credential-form").reset();
    closeSidebar();
  }
}

function updateCredentialStatus(configured, fingerprint = "") {
  const text = configured ? `已配置 · ${fingerprint}` : "尚未配置";
  $$('[data-credential-status]').forEach((target) => { target.textContent = text; });
}

async function loadDashboard() {
  try {
    const [user, health] = await Promise.all([api("/api/v2/me"), api("/api/v2/health")]);
    $("#username").textContent = user.username;
    $("#sidebar-username").textContent = user.username;
    $("#user-avatar").textContent = user.username.slice(0, 2);
    $("#db-status").textContent = `${health.database_dialect.toUpperCase()} 正常`;
    updateCredentialStatus(user.binance_credentials_configured, user.binance_key_fingerprint || "");
    setAuthenticated(true);
    openPanel("overview");
  } catch (_) {
    accessToken = "";
    setAuthenticated(false);
  }
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
  const form = new FormData(event.currentTarget);
  try {
    await api("/api/v2/auth/register", {
      method: "POST",
      body: JSON.stringify({
        username: form.get("username"),
        email: form.get("email") || null,
        password: form.get("password"),
      }),
    });
    event.currentTarget.reset();
    $("#tab-login").click();
    showMessage($("#auth-message"), "注册成功，请使用新账户登录。", "success");
  } catch (error) {
    showMessage($("#auth-message"), error.message, "error");
  }
});

$("#login-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = new FormData(event.currentTarget);
  try {
    const data = await api("/api/v2/auth/login", {
      method: "POST",
      body: JSON.stringify({
        username: form.get("username"),
        password: form.get("password"),
        client_type: "web",
      }),
    });
    accessToken = data.access_token;
    event.currentTarget.reset();
    await loadDashboard();
  } catch (error) {
    showMessage($("#auth-message"), error.message, "error");
  }
});

$("#credential-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = new FormData(event.currentTarget);
  try {
    const data = await api("/api/v2/me/binance-credentials", {
      method: "PUT",
      body: JSON.stringify({
        api_key: form.get("api_key"),
        api_secret: form.get("api_secret"),
        permissions: ["READ", "TRADE"],
      }),
    });
    event.currentTarget.reset();
    updateCredentialStatus(true, data.fingerprint);
    showMessage($("#dashboard-message"), "凭据已加密保存。", "success");
  } catch (error) {
    showMessage($("#dashboard-message"), error.message, "error");
  }
});

$("#delete-credentials").addEventListener("click", async () => {
  if (!window.confirm("确定删除当前用户的 Binance API 凭据？")) return;
  try {
    await api("/api/v2/me/binance-credentials", { method: "DELETE" });
    updateCredentialStatus(false);
    showMessage($("#dashboard-message"), "凭据已删除。", "success");
  } catch (error) {
    showMessage($("#dashboard-message"), error.message, "error");
  }
});

$("#logout").addEventListener("click", async () => {
  await fetch("/api/v2/auth/logout", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: "{}",
    credentials: "include",
  }).catch(() => {});
  accessToken = "";
  setAuthenticated(false);
});

$("#menu-toggle").addEventListener("click", () => {
  const open = !$("#sidebar").classList.contains("open");
  $("#sidebar").classList.toggle("open", open);
  $("#sidebar-backdrop").classList.toggle("open", open);
  $("#menu-toggle").setAttribute("aria-expanded", String(open));
});

$("#sidebar-backdrop").addEventListener("click", closeSidebar);
$$("[data-panel-target]").forEach((item) => item.addEventListener("click", () => openPanel(item.dataset.panelTarget)));
$$("[data-open-panel]").forEach((item) => item.addEventListener("click", () => openPanel(item.dataset.openPanel)));
document.addEventListener("keydown", (event) => { if (event.key === "Escape") closeSidebar(); });

(async () => {
  if (!accessToken) await refreshAccess();
  if (accessToken) await loadDashboard();
  else setAuthenticated(false);
})();
