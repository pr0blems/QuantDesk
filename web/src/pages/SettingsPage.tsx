import { useCallback, useEffect, useMemo, useState, type FormEvent } from "react";

import { settingsApi } from "../api/quantdesk";
import type { AiModelConfigCreate, ApiObject, CurrentUser } from "../api/types";
import { asList, asObject, booleanValue, firstList, stringValue } from "../utils/data";

function providerMark(code: unknown): string {
  return ({ deepseek: "DS", doubao: "豆", qwen: "千", kimi: "KM", minimax: "MM", openai: "OA" } as Record<string, string>)[stringValue(code, "")] ?? stringValue(code, "AI").slice(0, 2).toUpperCase();
}

export function SettingsPage({ user }: { user: CurrentUser }) {
  const [account, setAccount] = useState<ApiObject>({});
  const [providers, setProviders] = useState<ApiObject>({});
  const [configs, setConfigs] = useState<ApiObject>({});
  const [editing, setEditing] = useState<ApiObject | null>(null);
  const [editorOpen, setEditorOpen] = useState(false);
  const [working, setWorking] = useState(false);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");

  const load = useCallback(async () => {
    const results = await Promise.allSettled([settingsApi.binanceAccount(), settingsApi.aiProviders(), settingsApi.aiConfigs()]);
    if (results[0].status === "fulfilled") setAccount(results[0].value);
    if (results[1].status === "fulfilled") setProviders(results[1].value);
    if (results[2].status === "fulfilled") setConfigs(results[2].value);
    if (results.every((result) => result.status === "rejected")) setError("系统设置暂时无法读取");
  }, []);
  useEffect(() => { void load(); }, [load]);

  const accountData = asObject(account.account ?? account);
  const configured = booleanValue(accountData.configured ?? user.binance_credentials_configured);
  const configRows = useMemo(() => { const rows = firstList(configs, "items", "configs"); return rows.length ? rows : asList(configs); }, [configs]);
  const providerRows = useMemo(() => { const rows = firstList(providers, "items", "providers"); return rows.length ? rows : asList(providers); }, [providers]);

  async function saveCredentials(event: FormEvent<HTMLFormElement>): Promise<void> {
    event.preventDefault(); const form = new FormData(event.currentTarget); setWorking(true); setError("");
    try { await settingsApi.saveBinanceCredentials({ api_key: stringValue(form.get("api_key"), "").trim(), api_secret: stringValue(form.get("api_secret"), "").trim(), permissions: ["READ", "TRADE"] }); event.currentTarget.reset(); setMessage("Binance API 凭据已加密保存。完整密钥不会再次显示。"); await load(); }
    catch (caught) { setError(caught instanceof Error ? caught.message : "凭据保存失败"); } finally { setWorking(false); }
  }

  async function deleteCredentials(): Promise<void> {
    if (!window.confirm("确定删除当前用户的 Binance 凭据？相关实盘连接将立即不可用。")) return;
    setWorking(true); try { await settingsApi.deleteBinanceCredentials(); setMessage("Binance API 凭据已删除。"); await load(); } catch (caught) { setError(caught instanceof Error ? caught.message : "凭据删除失败"); } finally { setWorking(false); }
  }

  function openEditor(config: ApiObject | null): void { setEditing(config); setEditorOpen(true); setError(""); }

  async function saveModel(event: FormEvent<HTMLFormElement>): Promise<void> {
    event.preventDefault(); const form = new FormData(event.currentTarget); const id = stringValue(editing?.id, ""); const apiKey = stringValue(form.get("api_key"), "").trim();
    const base = { provider_code: stringValue(form.get("provider_code"), "openai") as AiModelConfigCreate["provider_code"], display_name: stringValue(form.get("display_name"), "").trim(), model_name: stringValue(form.get("model_name"), "").trim(), is_enabled: form.get("is_enabled") === "on", is_default: form.get("is_default") === "on" };
    setWorking(true); setError("");
    try { if (id) await settingsApi.updateAiConfig(id, { ...base, ...(apiKey ? { api_key: apiKey } : {}) }); else { if (!apiKey) throw new Error("新建配置必须填写 API Key"); await settingsApi.createAiConfig({ ...base, api_key: apiKey }); } setMessage(id ? "AI 模型配置已更新。" : "AI 模型配置已创建。"); setEditorOpen(false); setEditing(null); await load(); }
    catch (caught) { setError(caught instanceof Error ? caught.message : "模型配置保存失败"); } finally { setWorking(false); }
  }

  async function modelAction(row: ApiObject, action: "default" | "delete" | "test" | "toggle"): Promise<void> {
    const id = stringValue(row.id, ""); if (!id) return;
    if (action === "delete" && !window.confirm(`确定删除“${stringValue(row.display_name ?? row.displayName)}”模型配置？`)) return;
    setWorking(true); setError("");
    try {
      if (action === "test") { const result = await settingsApi.testAiConfig(id); setMessage(stringValue(result.message, "模型 API 测试成功。")); }
      else if (action === "delete") { await settingsApi.deleteAiConfig(id); setMessage("模型配置已删除。"); }
      else if (action === "toggle") { await settingsApi.updateAiConfig(id, { is_enabled: !booleanValue(row.is_enabled ?? row.isEnabled), ...(booleanValue(row.is_enabled ?? row.isEnabled) ? { is_default: false } : {}) }); setMessage("模型启用状态已更新。"); }
      else { await settingsApi.updateAiConfig(id, { is_enabled: true, is_default: true }); setMessage("默认模型已切换。"); }
      await load();
    } catch (caught) { setError(caught instanceof Error ? caught.message : "模型操作失败"); } finally { setWorking(false); }
  }

  return <>
    <div className="page-heading"><div><span className="eyebrow">SYSTEM SETTINGS</span><h1>系统设置</h1><p>管理当前账户的安全设置、交易所与 AI 服务连接</p></div></div>
    <div className="settings-layout">
      <nav className="settings-subnav" aria-label="系统设置二级导航"><span className="settings-subnav-label">设置分类</span><button className="settings-subnav-item active" type="button" aria-current="page"><span aria-hidden="true">钥</span><span><strong>API 凭证</strong><small>交易所与 AI 模型</small></span></button></nav>
      <div className="settings-content"><section className="settings-section" aria-labelledby="api-credentials-title">
        <header className="settings-section-head"><div><span className="eyebrow">API CREDENTIALS</span><h2 id="api-credentials-title">API 凭证</h2><p>每位用户独立配置，敏感密钥加密保存且不会再次回显。</p></div><span className="settings-security-badge"><i />用户级加密隔离</span></header>
        <article className="card credentials-card"><div className="credential-copy"><span className="section-number">01</span><h2>Binance API 凭据</h2><p>用于读取账户与执行已授权的交易。请关闭提现权限，并绑定生产服务器出口 IP。</p><div className="credential-state"><span>当前状态</span><strong>{configured ? "已配置" : "尚未配置"}</strong></div></div><form onSubmit={(event) => void saveCredentials(event)}><label>API Key<input name="api_key" type="password" autoComplete="new-password" minLength={16} placeholder="输入 Binance API Key" required /></label><label>API Secret<input name="api_secret" type="password" autoComplete="new-password" minLength={16} placeholder="输入 Binance API Secret" required /></label><div className="permission-box"><span>申请权限</span><strong>READ · TRADE</strong><small>系统不接受 WITHDRAW 权限</small></div><div className="button-row"><button className="primary" type="submit" disabled={working}>加密保存</button><button className="danger" type="button" disabled={working || !configured} onClick={() => void deleteCredentials()}>删除凭据</button></div></form></article>
        <p className={`message settings-inline-message${error ? " error" : message ? " success" : ""}`} aria-live="polite">{error || message}</p>
        <article className="card ai-model-settings-card"><header className="ai-model-settings-head"><div className="ai-model-title"><span className="section-number">02</span><div><span className="eyebrow">AI MODEL PROVIDERS</span><h2>AI 模型配置</h2><p>为策略语义编辑选择自己的模型服务。服务地址由系统托管，API Key 仅当前用户可用。</p></div></div><button className="primary" type="button" onClick={() => openEditor(null)}><span aria-hidden="true">＋</span>新增模型</button></header><div className="ai-provider-strip"><span>DeepSeek</span><span>豆包</span><span>千问</span><span>Kimi</span><span>MiniMax</span><span>OpenAI</span></div><div className="ai-model-data-notice"><span aria-hidden="true">!</span><p><strong>第三方数据说明</strong>：使用策略 AI 功能时，当前策略快照与修改指令会发送给所选模型服务商，请勿在指令中填写交易密钥或其他敏感信息。</p></div>
          {!configRows.length ? <div className="ai-model-list-status empty">尚未配置 AI 模型。新增后可作为策略语义编辑的模型来源。</div> : <div className="ai-model-config-list">{configRows.map((config) => { const enabled = booleanValue(config.is_enabled ?? config.isEnabled); const isDefault = booleanValue(config.is_default ?? config.isDefault); return <article className={`ai-model-config-card${isDefault ? " is-default" : ""}${enabled ? "" : " is-disabled"}`} key={stringValue(config.id)}><span className="ai-model-provider-mark">{providerMark(config.provider_code ?? config.providerCode)}</span><div className="ai-model-config-main"><div className="ai-model-config-title"><strong>{stringValue(config.display_name ?? config.displayName)}</strong>{isDefault ? <span className="ai-model-badge default">默认</span> : null}<span className={`ai-model-badge ${enabled ? "enabled" : "disabled"}`}>{enabled ? "已启用" : "已停用"}</span></div><span>{stringValue(config.provider_name ?? config.provider_code)} · {stringValue(config.model_name ?? config.modelName)}</span><small>{stringValue(config.base_url, "系统托管服务地址")} · 密钥 {stringValue(config.api_key_fingerprint ?? config.apiKeyFingerprint, "已加密")}</small></div><div className="ai-model-config-actions"><button type="button" onClick={() => void modelAction(config, "test")}>测试</button><button type="button" onClick={() => openEditor(config)}>编辑</button><button type="button" onClick={() => void modelAction(config, "toggle")}>{enabled ? "停用" : "启用"}</button>{!isDefault ? <button type="button" data-ai-action="default" onClick={() => void modelAction(config, "default")}>设为默认</button> : null}<button type="button" data-ai-action="delete" onClick={() => void modelAction(config, "delete")}>删除</button></div></article>; })}</div>}
        </article>
      </section></div>
    </div>
    {editorOpen ? <dialog className="ai-model-dialog" open><form className="ai-model-form" onSubmit={(event) => void saveModel(event)}><header className="ai-model-dialog-head"><div><span className="eyebrow">MODEL CONNECTION</span><h2>{editing ? "编辑 AI 模型" : "新增 AI 模型"}</h2><p>{editing ? "API Key 留空会保留现有密钥，其他字段保存后立即生效。" : "API Key 将加密保存，提交后不再回显。"}</p></div><button className="ai-model-dialog-close" type="button" aria-label="关闭" onClick={() => setEditorOpen(false)}>×</button></header><div className="ai-model-field-grid"><label>服务商<select name="provider_code" defaultValue={stringValue(editing?.provider_code ?? editing?.providerCode, stringValue(providerRows[0]?.code, "openai"))}>{(providerRows.length ? providerRows : ["openai", "deepseek", "doubao", "qwen", "kimi", "minimax"].map((code) => ({ code, name: code }))).map((provider) => <option value={stringValue(provider.code)} key={stringValue(provider.code)}>{stringValue(provider.name ?? provider.code)}</option>)}</select></label><label>显示名称<input name="display_name" defaultValue={stringValue(editing?.display_name ?? editing?.displayName, "")} required /></label><label>模型名称<input name="model_name" defaultValue={stringValue(editing?.model_name ?? editing?.modelName, "")} required /></label><label>API Key<input name="api_key" type="password" autoComplete="off" required={!editing} placeholder={editing ? "留空则保留当前已加密密钥" : "输入服务商 API Key"} /></label></div><div className="ai-provider-endpoint"><span>服务地址</span><strong>由系统配置并安全托管</strong><small>前端不可修改服务地址，避免密钥被发送到非受信端点。</small></div><div className="ai-model-switches"><label><input name="is_enabled" type="checkbox" defaultChecked={editing ? booleanValue(editing.is_enabled ?? editing.isEnabled) : true} /><span><strong>启用模型</strong><small>允许策略 AI 功能调用此配置</small></span></label><label><input name="is_default" type="checkbox" defaultChecked={booleanValue(editing?.is_default ?? editing?.isDefault)} /><span><strong>设为默认</strong><small>策略编辑器优先使用此配置</small></span></label></div><div className="ai-model-dialog-actions"><button className="secondary" type="button" onClick={() => setEditorOpen(false)}>取消</button><button className="primary" type="submit" disabled={working}>保存配置</button></div></form></dialog> : null}
  </>;
}
