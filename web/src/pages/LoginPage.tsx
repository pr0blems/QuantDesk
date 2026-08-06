import { useState, type FormEvent } from "react";

import { authApi } from "../api/quantdesk";
import type { CurrentUser } from "../api/types";

export function LoginPage({ onAuthenticated }: { onAuthenticated: (user: CurrentUser) => void }) {
  const [mode, setMode] = useState<"login" | "register">("login");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState("");
  const [light, setLight] = useState(() => document.documentElement.dataset.theme === "light");

  function toggleTheme(): void {
    const next = !light;
    setLight(next);
    document.documentElement.dataset.theme = next ? "light" : "dark";
    try { window.localStorage.setItem("quantdesk.theme", next ? "light" : "dark"); } catch { /* optional */ }
  }

  async function submit(event: FormEvent<HTMLFormElement>): Promise<void> {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    const usernameValue = form.get("username");
    const passwordValue = form.get("password");
    const emailValue = form.get("email");
    const username = typeof usernameValue === "string" ? usernameValue.trim() : "";
    const password = typeof passwordValue === "string" ? passwordValue : "";
    if (!username || !password) { setError("请输入用户名和密码。"); return; }
    setSubmitting(true);
    setError("");
    try {
      if (mode === "register") {
        if (password.length < 12) throw new Error("注册密码至少需要 12 位。");
        await authApi.register({ username, password, email: typeof emailValue === "string" && emailValue.trim() ? emailValue.trim() : null });
      }
      onAuthenticated(await authApi.login({ username, password }));
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "登录失败，请稍后重试。");
    } finally { setSubmitting(false); }
  }

  return (
    <section className="login-page">
      <button className="theme-toggle login-theme-toggle" type="button" title={light ? "切换深色主题" : "切换浅色主题"} aria-label={light ? "切换深色主题" : "切换浅色主题"} aria-pressed={light} onClick={toggleTheme}><span aria-hidden="true">{light ? "◐" : "☼"}</span><b>{light ? "深色" : "浅色"}</b></button>
      <div className="login-visual">
        <div className="brand-mark"><span>Q</span> QuantDesk</div>
        <div className="login-copy"><span className="eyebrow">BINANCE QUANT SYSTEM</span><h1>更少干扰，<br />更快决策。</h1><p>面向 Binance 合约的专业量化交易终端。行情、策略、订单与风险状态集中呈现，实盘执行默认关闭。</p></div>
        <div className="trust-row"><span><i />密钥加密存储</span><span><i />多用户数据隔离</span><span><i />全链路操作审计</span></div>
      </div>
      <div className="login-panel">
        <article className="auth-card">
          <div className="mobile-brand"><span>Q</span> QuantDesk</div>
          <div className="auth-heading"><span className="eyebrow">欢迎回来</span><h2>{mode === "login" ? "登录交易工作台" : "创建平台账户"}</h2><p>{mode === "login" ? "使用您的平台账户继续" : "创建独立的量化工作空间"}</p></div>
          <div className="tabs" role="tablist" aria-label="账户操作"><button className={mode === "login" ? "active" : ""} type="button" role="tab" aria-selected={mode === "login"} onClick={() => setMode("login")}>登录</button><button className={mode === "register" ? "active" : ""} type="button" role="tab" aria-selected={mode === "register"} onClick={() => setMode("register")}>注册</button></div>
          <form onSubmit={(event) => void submit(event)}>
            <label>用户名<input name="username" minLength={mode === "register" ? 3 : undefined} autoComplete="username" placeholder={mode === "register" ? "3–64 位字符" : "请输入用户名"} required /></label>
            {mode === "register" ? <label>邮箱（可选）<input name="email" type="email" autoComplete="email" placeholder="name@example.com" /></label> : null}
            <label>密码<input name="password" type="password" minLength={mode === "register" ? 12 : undefined} autoComplete={mode === "login" ? "current-password" : "new-password"} placeholder={mode === "register" ? "至少 12 位" : "请输入密码"} required /></label>
            <button className="primary wide" type="submit" disabled={submitting}>{submitting ? "正在处理…" : mode === "login" ? "登录" : "创建账户"}</button>
          </form>
          <p className={`message${error ? " error" : ""}`} aria-live="polite">{error}</p>
          <p className="security-note">登录即表示您了解：实盘执行需要单独启用并通过风控检查。</p>
        </article>
      </div>
    </section>
  );
}
