import { useEffect, useState } from "react";

import { onAuthenticationLost } from "./api/client";
import { authApi } from "./api/quantdesk";
import type { CurrentUser } from "./api/types";
import { LegacyPanel } from "./pages/LegacyPanel";
import { LoginPage } from "./pages/LoginPage";
import { OverviewPage } from "./pages/OverviewPage";
import { SettingsPage } from "./pages/SettingsPage";

type PageKey = "ai-monitor" | "backtest" | "live" | "monitor" | "overview" | "paper" | "settings" | "strategies";
type AuthState = { status: "booting" } | { status: "anonymous" } | { status: "authenticated"; user: CurrentUser };

const pageTitles: Record<PageKey, string> = {
  "ai-monitor": "发现机会",
  overview: "工作台", monitor: "合约监控", paper: "模拟盘", live: "实盘交易", strategies: "策略中心", backtest: "数据回测", settings: "系统设置",
};

const navigation: Array<{ badge?: string; icon: string; key: PageKey; label: string }> = [
  { key: "overview", icon: "概", label: "工作台" },
  { key: "monitor", icon: "监", label: "合约监控" },
  { key: "ai-monitor", icon: "机", label: "发现机会" },
  { key: "paper", icon: "模", label: "模拟盘" },
  { key: "live", icon: "实", label: "实盘交易", badge: "风控" },
  { key: "strategies", icon: "策", label: "策略中心" },
  { key: "backtest", icon: "测", label: "数据回测" },
  { key: "settings", icon: "设", label: "系统设置" },
];

function pageFromHash(): PageKey {
  const candidate = window.location.hash.replace(/^#\/?/, "").split("/")[0] || "overview";
  if (candidate === "backtests") return "backtest";
  if (candidate === "orders") return "live";
  return candidate in pageTitles ? candidate as PageKey : "overview";
}

function AppBoot() {
  return <div className="auth-boot auth-boot-visible" role="status" aria-live="polite"><div className="auth-boot-card"><span className="auth-boot-logo" aria-hidden="true">Q</span><div><strong>正在恢复登录状态</strong><small>正在安全连接 QuantDesk…</small></div><i className="auth-boot-spinner" aria-hidden="true" /></div></div>;
}

function Workspace({ user, onLogout }: { user: CurrentUser; onLogout: () => Promise<void> }) {
  const [page, setPage] = useState<PageKey>(pageFromHash);
  const [menuOpen, setMenuOpen] = useState(false);
  const [light, setLight] = useState(() => document.documentElement.dataset.theme === "light");
  const [loggingOut, setLoggingOut] = useState(false);

  useEffect(() => {
    if (!window.location.hash) window.history.replaceState(null, "", "#/overview");
    const sync = () => { setPage(pageFromHash()); setMenuOpen(false); };
    window.addEventListener("hashchange", sync);
    return () => window.removeEventListener("hashchange", sync);
  }, []);
  useEffect(() => { document.title = `${pageTitles[page]} · QuantDesk`; }, [page]);

  function toggleTheme(): void {
    const next = !light;
    setLight(next);
    document.documentElement.dataset.theme = next ? "light" : "dark";
    try { window.localStorage.setItem("quantdesk.theme", next ? "light" : "dark"); } catch { /* optional */ }
  }

  async function logout(): Promise<void> {
    setLoggingOut(true);
    try { await onLogout(); } finally { setLoggingOut(false); }
  }

  return <section className="app-shell">
    <button className={`sidebar-backdrop${menuOpen ? " open" : ""}`} type="button" aria-label="关闭功能菜单" onClick={() => setMenuOpen(false)} />
    <aside className={`sidebar${menuOpen ? " open" : ""}`} aria-label="主导航">
      <div className="sidebar-brand"><span>Q</span><div><strong>QUANTDESK</strong><small>NG · BINANCE</small></div></div>
      <nav className="side-nav">
        <p>交易工作区</p>
        {navigation.slice(0, 7).map((item) => <a className={`nav-item${page === item.key ? " active" : ""}`} href={`#/${item.key}`} aria-current={page === item.key ? "page" : undefined} key={item.key}><span>{item.icon}</span>{item.label}{item.badge ? <i>{item.badge}</i> : null}</a>)}
        <p>安全与管理</p>
        {navigation.slice(7).map((item) => <a className={`nav-item${page === item.key ? " active" : ""}`} href={`#/${item.key}`} aria-current={page === item.key ? "page" : undefined} key={item.key}><span>{item.icon}</span>{item.label}{item.badge ? <i>{item.badge}</i> : null}</a>)}
        {user.is_admin ? <a className="nav-item" href="/next/admin/#overview"><span>管</span>管理后台</a> : null}
      </nav>
      <div className="sidebar-account"><div className="avatar">{user.username.slice(0, 2)}</div><div><strong>{user.username}</strong><small>当前账户</small></div><button className="theme-toggle" type="button" title={light ? "切换深色主题" : "切换浅色主题"} aria-label={light ? "切换深色主题" : "切换浅色主题"} aria-pressed={light} onClick={toggleTheme}><span aria-hidden="true">{light ? "◐" : "☼"}</span><b>{light ? "深色" : "浅色"}</b></button><button type="button" title="退出登录" aria-label="退出登录" disabled={loggingOut} onClick={() => void logout()}>{loggingOut ? "退出中" : "退出"}</button></div>
    </aside>
    <div className="workspace">
      <header className="mobile-bar"><button className="menu-toggle" type="button" aria-label="打开功能菜单" aria-expanded={menuOpen} onClick={() => setMenuOpen(true)}>菜单</button><strong>{pageTitles[page]}</strong><span className="status-dot" title="服务状态" /></header>
      <main className={`workspace-content${page === "monitor" ? " monitor-mode" : page === "ai-monitor" ? " ai-monitor-mode" : ""}`}>
        <section className={`workspace-panel${page === "monitor" ? "" : " hidden"}`}><LegacyPanel active={page === "monitor"} tag="contract-monitor" /></section>
        <section className={`workspace-panel${page === "ai-monitor" ? "" : " hidden"}`}><LegacyPanel active={page === "ai-monitor"} tag="ai-monitor-dashboard" /></section>
        <section className={`workspace-panel${page === "paper" ? "" : " hidden"}`}><LegacyPanel active={page === "paper"} tag="paper-dashboard" /></section>
        <section className={`workspace-panel${page === "live" ? "" : " hidden"}`}><LegacyPanel active={page === "live"} tag="live-dashboard" /></section>
        <section className={`workspace-panel${page === "strategies" ? "" : " hidden"}`}><LegacyPanel active={page === "strategies"} tag="strategy-center" /></section>
        <section className={`workspace-panel${page === "backtest" ? "" : " hidden"}`}><LegacyPanel active={page === "backtest"} tag="backtest-workbench" /></section>
        {page === "overview" ? <section className="workspace-panel"><OverviewPage user={user} /></section> : null}
        {page === "settings" ? <section className="workspace-panel"><SettingsPage user={user} /></section> : null}
      </main>
    </div>
  </section>;
}

export function App() {
  const [auth, setAuth] = useState<AuthState>({ status: "booting" });
  useEffect(() => onAuthenticationLost(() => setAuth({ status: "anonymous" })), []);
  useEffect(() => {
    let active = true;
    void authApi.restore().then((user) => { if (active) setAuth(user ? { status: "authenticated", user } : { status: "anonymous" }); }).catch(() => { if (active) setAuth({ status: "anonymous" }); });
    return () => { active = false; };
  }, []);
  if (auth.status === "booting") return <AppBoot />;
  if (auth.status === "anonymous") return <LoginPage onAuthenticated={(user) => setAuth({ status: "authenticated", user })} />;
  return <Workspace user={auth.user} onLogout={async () => { try { await authApi.logout(); } finally { setAuth({ status: "anonymous" }); } }} />;
}
