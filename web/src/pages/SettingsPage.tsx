import { useCallback, useEffect, useState, type FormEvent } from "react";

import { settingsApi } from "../api/quantdesk";
import type { ApiObject, CurrentUser } from "../api/types";
import { asObject, booleanValue, stringValue } from "../utils/data";

export function SettingsPage({ user }: { user: CurrentUser }) {
  const [account, setAccount] = useState<ApiObject>({});
  const [working, setWorking] = useState(false);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");

  const load = useCallback(async () => {
    try {
      setAccount(await settingsApi.binanceAccount());
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "系统设置暂时无法读取");
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const accountData = asObject(account.account ?? account);
  const configured = booleanValue(
    accountData.configured ?? user.binance_credentials_configured,
  );

  async function saveCredentials(event: FormEvent<HTMLFormElement>): Promise<void> {
    event.preventDefault();
    const formElement = event.currentTarget;
    const form = new FormData(formElement);
    setWorking(true);
    setError("");
    try {
      await settingsApi.saveBinanceCredentials({
        api_key: stringValue(form.get("api_key"), "").trim(),
        api_secret: stringValue(form.get("api_secret"), "").trim(),
        permissions: ["READ", "TRADE"],
      });
      formElement.reset();
      setMessage("Binance API 凭据已加密保存。完整密钥不会再次显示。");
      await load();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "凭据保存失败");
    } finally {
      setWorking(false);
    }
  }

  async function deleteCredentials(): Promise<void> {
    if (!window.confirm("确定删除当前用户的 Binance 凭据？相关实盘连接将立即不可用。")) {
      return;
    }
    setWorking(true);
    setError("");
    try {
      await settingsApi.deleteBinanceCredentials();
      setMessage("Binance API 凭据已删除。");
      await load();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "凭据删除失败");
    } finally {
      setWorking(false);
    }
  }

  return (
    <>
      <div className="page-heading">
        <div>
          <span className="eyebrow">SYSTEM SETTINGS</span>
          <h1>系统设置</h1>
          <p>管理当前账户的安全设置与交易所连接</p>
        </div>
      </div>
      <div className="settings-layout">
        <nav className="settings-subnav" aria-label="系统设置二级导航">
          <span className="settings-subnav-label">设置分类</span>
          <button className="settings-subnav-item active" type="button" aria-current="page">
            <span aria-hidden="true">钥</span>
            <span>
              <strong>API 凭证</strong>
              <small>当前账户交易所</small>
            </span>
          </button>
        </nav>
        <div className="settings-content">
          <section className="settings-section" aria-labelledby="api-credentials-title">
            <header className="settings-section-head">
              <div>
                <span className="eyebrow">API CREDENTIALS</span>
                <h2 id="api-credentials-title">API 凭证</h2>
                <p>交易所密钥按用户隔离并加密保存，不会再次回显。</p>
              </div>
              <span className="settings-security-badge"><i />用户级加密隔离</span>
            </header>
            <article className="card credentials-card">
              <div className="credential-copy">
                <span className="section-number">01</span>
                <h2>Binance API 凭据</h2>
                <p>用于读取账户与执行已授权的交易。请关闭提现权限，并绑定生产服务器出口 IP。</p>
                <div className="credential-state">
                  <span>当前状态</span>
                  <strong>{configured ? "已配置" : "尚未配置"}</strong>
                </div>
              </div>
              <form onSubmit={(event) => void saveCredentials(event)}>
                <label>API Key<input name="api_key" type="password" autoComplete="new-password" minLength={16} placeholder="输入 Binance API Key" required /></label>
                <label>API Secret<input name="api_secret" type="password" autoComplete="new-password" minLength={16} placeholder="输入 Binance API Secret" required /></label>
                <div className="permission-box">
                  <span>申请权限</span><strong>READ · TRADE</strong><small>系统不接受 WITHDRAW 权限</small>
                </div>
                <div className="button-row">
                  <button className="primary" type="submit" disabled={working}>加密保存</button>
                  <button className="danger" type="button" disabled={working || !configured} onClick={() => void deleteCredentials()}>删除凭据</button>
                </div>
              </form>
            </article>
            <p className={`message settings-inline-message${error ? " error" : message ? " success" : ""}`} aria-live="polite">{error || message}</p>
            <article className="card ai-model-settings-card">
              <header className="ai-model-settings-head">
                <div className="ai-model-title">
                  <span className="section-number">02</span>
                  <div>
                    <span className="eyebrow">GLOBAL AI SERVICE</span>
                    <h2>DeepSeek 由管理员统一配置</h2>
                    <p>新闻分析、机会研判与策略 AI 均使用 y0ur 在管理后台维护的全局 DeepSeek。</p>
                  </div>
                </div>
              </header>
              <div className="ai-provider-strip"><span>DeepSeek</span><span>全局共享</span><span>密钥不下发前端</span></div>
              <div className="ai-model-data-notice">
                <span aria-hidden="true">!</span>
                <p><strong>无需个人 API Key</strong>：普通用户不能新增、编辑或测试模型密钥。若模型不可用，请联系管理员在管理后台检查全局配置。</p>
              </div>
            </article>
          </section>
        </div>
      </div>
    </>
  );
}
