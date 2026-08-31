import { useCallback, useEffect, useMemo, useRef, useState, type FormEvent } from "react";

import { settingsApi } from "../api/quantdesk";
import type { AiModelConfigCreate, AiModelConfigUpdate, ApiObject, CurrentUser } from "../api/types";
import { asObject, booleanValue, stringValue } from "../utils/data";

type ProviderCode = AiModelConfigCreate["provider_code"];
type AiProvider = { code: ProviderCode; name: string; baseUrl: string; defaultModel: string; models: string[] };
type AiConfig = {
  id: string;
  providerCode: ProviderCode;
  providerName: string;
  displayName: string;
  baseUrl: string;
  modelName: string;
  apiKeyConfigured: boolean;
  apiKeyFingerprint: string;
  isEnabled: boolean;
  isDefault: boolean;
};

const fallbackProviders: AiProvider[] = [
  { code: "deepseek", name: "DeepSeek", baseUrl: "", defaultModel: "deepseek-v4-flash", models: ["deepseek-v4-flash", "deepseek-v4-pro"] },
  { code: "doubao", name: "豆包", baseUrl: "", defaultModel: "doubao-seed-2-0-lite-260215", models: ["doubao-seed-2-0-lite-260215"] },
  { code: "qwen", name: "千问", baseUrl: "", defaultModel: "qwen3.7-plus", models: ["qwen3.7-plus", "qwen-plus"] },
  { code: "kimi", name: "Kimi", baseUrl: "", defaultModel: "kimi-k3", models: ["kimi-k3"] },
  { code: "minimax", name: "MiniMax", baseUrl: "", defaultModel: "MiniMax-M2.7", models: ["MiniMax-M2.7"] },
  { code: "openai", name: "OpenAI", baseUrl: "", defaultModel: "", models: [] },
];

function list(value: unknown): ApiObject[] {
  if (Array.isArray(value)) return value.filter((item): item is ApiObject => Boolean(item) && typeof item === "object");
  const object = asObject(value);
  return Array.isArray(object.items) ? object.items.filter((item): item is ApiObject => Boolean(item) && typeof item === "object") : [];
}

function normalizeProvider(item: ApiObject): AiProvider | null {
  const code = stringValue(item.code, "").trim().toLowerCase() as ProviderCode;
  if (!fallbackProviders.some((provider) => provider.code === code)) return null;
  const models = Array.isArray(item.models) ? [...new Set(item.models.map((model) => String(model || "").trim()).filter(Boolean))] : [];
  const defaultModel = stringValue(item.default_model, "").trim();
  if (defaultModel && !models.includes(defaultModel)) models.unshift(defaultModel);
  return { code, name: stringValue(item.name, code), baseUrl: stringValue(item.base_url, ""), defaultModel, models };
}

function normalizeConfig(item: ApiObject): AiConfig | null {
  const id = stringValue(item.id, "");
  const providerCode = stringValue(item.provider_code, "") as ProviderCode;
  if (!id || !fallbackProviders.some((provider) => provider.code === providerCode)) return null;
  return {
    id,
    providerCode,
    providerName: stringValue(item.provider_name, ""),
    displayName: stringValue(item.display_name, ""),
    baseUrl: stringValue(item.base_url, ""),
    modelName: stringValue(item.model_name, ""),
    apiKeyConfigured: booleanValue(item.api_key_configured),
    apiKeyFingerprint: stringValue(item.api_key_fingerprint, ""),
    isEnabled: booleanValue(item.is_enabled),
    isDefault: booleanValue(item.is_default),
  };
}

function providerMark(code: ProviderCode): string {
  return ({ deepseek: "DS", doubao: "豆", qwen: "千", kimi: "KM", minimax: "MM", openai: "OA" })[code];
}

export function SettingsPage({ user }: { user: CurrentUser }) {
  const [account, setAccount] = useState<ApiObject>({});
  const [working, setWorking] = useState(false);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const [providers, setProviders] = useState<AiProvider[]>(fallbackProviders);
  const [configs, setConfigs] = useState<AiConfig[]>([]);
  const [modelsLoading, setModelsLoading] = useState(true);
  const [modelsMessage, setModelsMessage] = useState("");
  const [modelsTone, setModelsTone] = useState("");
  const [editing, setEditing] = useState<AiConfig | null | undefined>(undefined);
  const [selectedProvider, setSelectedProvider] = useState<ProviderCode>("deepseek");
  const [modelName, setModelName] = useState("");
  const [modelFormError, setModelFormError] = useState("");
  const dialogRef = useRef<HTMLDialogElement | null>(null);

  const loadAccount = useCallback(async () => {
    try {
      setAccount(await settingsApi.binanceAccount());
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "系统设置暂时无法读取");
    }
  }, []);

  const loadModels = useCallback(async (successMessage = "") => {
    setModelsLoading(true);
    if (!successMessage) { setModelsMessage("正在读取当前用户的 AI 模型配置…"); setModelsTone("loading"); }
    try {
      const [providerPayload, configPayload] = await Promise.all([settingsApi.aiProviders(), settingsApi.aiConfigs()]);
      const normalizedProviders = list(providerPayload).map(normalizeProvider).filter((item): item is AiProvider => Boolean(item));
      setProviders(normalizedProviders.length ? normalizedProviders : fallbackProviders);
      setConfigs(list(configPayload).map(normalizeConfig).filter((item): item is AiConfig => Boolean(item)));
      setModelsMessage(successMessage);
      setModelsTone(successMessage ? "success" : "");
    } catch (caught) {
      setModelsMessage(`模型配置加载失败：${caught instanceof Error ? caught.message : "请求失败"}`);
      setModelsTone("error");
    } finally {
      setModelsLoading(false);
    }
  }, []);

  useEffect(() => { void Promise.all([loadAccount(), loadModels()]); }, [loadAccount, loadModels]);

  const accountData = asObject(account.account ?? account);
  const configured = booleanValue(accountData.configured ?? user.binance_credentials_configured);
  const selectedProviderData = useMemo(
    () => providers.find((provider) => provider.code === selectedProvider) ?? fallbackProviders[0]!,
    [providers, selectedProvider],
  );

  async function saveCredentials(event: FormEvent<HTMLFormElement>): Promise<void> {
    event.preventDefault();
    const formElement = event.currentTarget;
    const form = new FormData(formElement);
    setWorking(true); setError(""); setMessage("");
    try {
      await settingsApi.saveBinanceCredentials({
        api_key: stringValue(form.get("api_key"), "").trim(),
        api_secret: stringValue(form.get("api_secret"), "").trim(),
        permissions: ["READ", "TRADE"],
      });
      formElement.reset();
      setMessage("Binance API 凭据已加密保存。完整密钥不会再次显示。");
      await loadAccount();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "凭据保存失败");
    } finally { setWorking(false); }
  }

  async function deleteCredentials(): Promise<void> {
    if (!window.confirm("确定删除当前用户的 Binance 凭据？相关实盘连接将立即不可用。")) return;
    setWorking(true); setError(""); setMessage("");
    try {
      await settingsApi.deleteBinanceCredentials();
      setMessage("Binance API 凭据已删除。");
      await loadAccount();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "凭据删除失败");
    } finally { setWorking(false); }
  }

  function openModelDialog(config?: AiConfig): void {
    const providerCode = config?.providerCode ?? providers[0]?.code ?? "deepseek";
    const provider = providers.find((item) => item.code === providerCode) ?? fallbackProviders[0]!;
    setEditing(config ?? null);
    setSelectedProvider(providerCode);
    setModelName(config?.modelName || provider.defaultModel);
    setModelFormError("");
    window.setTimeout(() => dialogRef.current?.showModal(), 0);
  }

  function closeModelDialog(): void {
    dialogRef.current?.close();
    setEditing(undefined);
    setModelFormError("");
  }

  async function saveModel(event: FormEvent<HTMLFormElement>): Promise<void> {
    event.preventDefault();
    const form = event.currentTarget;
    const data = new FormData(form);
    const apiKey = stringValue(data.get("api_key"), "").trim();
    if (editing && selectedProvider !== editing.providerCode && !apiKey) {
      setModelFormError("更换服务商时必须输入对应的新 API Key。");
      return;
    }
    setWorking(true); setModelFormError("");
    try {
      const common = {
        provider_code: selectedProvider,
        display_name: stringValue(data.get("display_name"), "").trim(),
        model_name: modelName.trim(),
        is_enabled: data.get("is_enabled") === "on",
        is_default: data.get("is_default") === "on",
      };
      if (editing) {
        const payload: AiModelConfigUpdate = { ...common };
        if (apiKey) payload.api_key = apiKey;
        await settingsApi.updateAiConfig(editing.id, payload);
      } else {
        await settingsApi.createAiConfig({ ...common, api_key: apiKey });
      }
      closeModelDialog();
      await loadModels(editing ? "模型配置已更新。" : "模型配置已创建。");
    } catch (caught) {
      setModelFormError(caught instanceof Error ? caught.message : "模型配置保存失败");
    } finally { setWorking(false); }
  }

  async function modelAction(config: AiConfig, action: "default" | "delete" | "test" | "toggle"): Promise<void> {
    if (action === "delete" && !window.confirm(`确定删除“${config.displayName || config.providerName}”模型配置？`)) return;
    setWorking(true); setModelsMessage(action === "test" ? "正在测试模型连接…" : ""); setModelsTone(action === "test" ? "loading" : "");
    try {
      if (action === "test") {
        const result = await settingsApi.testAiConfig(config.id);
        setModelsMessage(stringValue(result.message, "API 测试成功，模型服务可正常使用"));
        setModelsTone("success");
      } else if (action === "delete") {
        await settingsApi.deleteAiConfig(config.id);
        await loadModels();
      } else if (action === "toggle") {
        await settingsApi.updateAiConfig(config.id, { is_enabled: !config.isEnabled, ...(!config.isEnabled ? {} : { is_default: false }) });
        await loadModels();
      } else {
        await settingsApi.updateAiConfig(config.id, { is_enabled: true, is_default: true });
        await loadModels();
      }
    } catch (caught) {
      setModelsMessage(caught instanceof Error ? caught.message : "模型操作失败");
      setModelsTone("error");
    } finally { setWorking(false); }
  }

  return <>
    <div className="page-heading"><div><span className="eyebrow">SYSTEM SETTINGS</span><h1>系统设置</h1><p>管理当前账户的安全设置、交易所与 AI 服务连接</p></div></div>
    <div className="settings-layout">
      <nav className="settings-subnav" aria-label="系统设置二级导航"><span className="settings-subnav-label">设置分类</span><button className="settings-subnav-item active" type="button" aria-current="page"><span aria-hidden="true">钥</span><span><strong>API 凭证</strong><small>交易所与 AI 模型</small></span></button></nav>
      <div className="settings-content"><section className="settings-section" aria-labelledby="api-credentials-title">
        <header className="settings-section-head"><div><span className="eyebrow">API CREDENTIALS</span><h2 id="api-credentials-title">API 凭证</h2><p>每位用户独立配置，敏感密钥加密保存且不会再次回显。</p></div><span className="settings-security-badge"><i />用户级加密隔离</span></header>
        <article className="card credentials-card"><div className="credential-copy"><span className="section-number">01</span><h2>Binance API 凭据</h2><p>用于读取账户与执行已授权的交易。请关闭提现权限，并绑定生产服务器出口 IP。</p><div className="credential-state"><span>当前状态</span><strong>{configured ? "已配置" : "尚未配置"}</strong></div></div><form onSubmit={(event) => void saveCredentials(event)}><label>API Key<input name="api_key" type="password" autoComplete="new-password" minLength={16} placeholder="输入 Binance API Key" required /></label><label>API Secret<input name="api_secret" type="password" autoComplete="new-password" minLength={16} placeholder="输入 Binance API Secret" required /></label><div className="permission-box"><span>申请权限</span><strong>READ · TRADE</strong><small>系统不接受 WITHDRAW 权限</small></div><div className="button-row"><button className="primary" type="submit" disabled={working}>加密保存</button><button className="danger" type="button" disabled={working || !configured} onClick={() => void deleteCredentials()}>删除凭据</button></div></form></article>
        <p className={`message settings-inline-message${error ? " error" : message ? " success" : ""}`} aria-live="polite">{error || message}</p>
        <article className="card ai-model-settings-card" aria-labelledby="ai-model-settings-title">
          <header className="ai-model-settings-head"><div className="ai-model-title"><span className="section-number">02</span><div><span className="eyebrow">AI MODEL PROVIDERS</span><h2 id="ai-model-settings-title">AI 模型配置</h2><p>为策略语义编辑选择自己的模型服务。服务地址由系统托管，API Key 仅当前用户可用。</p></div></div><button className="primary" type="button" onClick={() => openModelDialog()}><span aria-hidden="true">＋</span>新增模型</button></header>
          <div className="ai-provider-strip" aria-label="支持的 AI 服务商"><span>DeepSeek</span><span>豆包</span><span>千问</span><span>Kimi</span><span>MiniMax</span><span>OpenAI</span></div>
          <div className="ai-model-data-notice"><span aria-hidden="true">!</span><p><strong>第三方数据说明</strong>：使用策略 AI 功能时，当前策略快照与修改指令会发送给所选模型服务商，请勿在指令中填写交易密钥或其他敏感信息。</p></div>
          {modelsMessage || (!modelsLoading && !configs.length) ? <div className={`ai-model-list-status ${modelsTone || (!configs.length ? "empty" : "")}`} role="status" aria-live="polite">{modelsMessage || "尚未配置 AI 模型。新增后可作为策略语义编辑的模型来源。"}</div> : null}
          <div className="ai-model-config-list" aria-live="polite" aria-busy={modelsLoading}>{configs.map((config) => {
            const provider = providers.find((item) => item.code === config.providerCode);
            const providerName = config.providerName || provider?.name || config.providerCode;
            return <article className={`ai-model-config-card${config.isDefault ? " is-default" : ""}${config.isEnabled ? "" : " is-disabled"}`} key={config.id}>
              <span className="ai-model-provider-mark">{providerMark(config.providerCode)}</span>
              <div className="ai-model-config-main"><div className="ai-model-config-title"><strong>{config.displayName || `${providerName} 配置`}</strong>{config.isDefault ? <span className="ai-model-badge default">默认</span> : null}<span className={`ai-model-badge ${config.isEnabled ? "enabled" : "disabled"}`}>{config.isEnabled ? "已启用" : "已停用"}</span></div><span>{providerName} · {config.modelName || "未指定模型"}</span><small>{config.baseUrl || provider?.baseUrl || "系统托管服务地址"} · {config.apiKeyConfigured ? `密钥 ${config.apiKeyFingerprint || "已加密"}` : "尚未配置 API Key"}</small></div>
              <div className="ai-model-config-actions"><button type="button" disabled={working} onClick={() => void modelAction(config, "test")}>测试</button><button type="button" disabled={working} onClick={() => openModelDialog(config)}>编辑</button><button type="button" disabled={working} onClick={() => void modelAction(config, "toggle")}>{config.isEnabled ? "停用" : "启用"}</button>{!config.isDefault ? <button type="button" disabled={working} onClick={() => void modelAction(config, "default")}>设为默认</button> : null}<button type="button" disabled={working} onClick={() => void modelAction(config, "delete")}>删除</button></div>
            </article>;
          })}</div>
        </article>
      </section></div>
    </div>
    <dialog ref={dialogRef} className="ai-model-dialog" aria-labelledby="ai-model-dialog-title">
      <form className="ai-model-form" onSubmit={(event) => void saveModel(event)}>
        <header className="ai-model-dialog-head"><div><span className="eyebrow">AI CREDENTIAL</span><h2 id="ai-model-dialog-title">{editing ? "编辑 AI 模型" : "新增 AI 模型"}</h2><p>{editing ? "API Key 留空会保留现有密钥，其他字段保存后立即生效。" : "API Key 将加密保存，提交后不再回显。"}</p></div><button className="ai-model-dialog-close" type="button" aria-label="关闭 AI 模型配置" onClick={closeModelDialog}>×</button></header>
        <div className="ai-model-field-grid"><label>模型服务商<select name="provider_code" value={selectedProvider} onChange={(event) => { const code = event.target.value as ProviderCode; setSelectedProvider(code); const provider = providers.find((item) => item.code === code); setModelName(provider?.defaultModel || ""); }}>{providers.map((provider) => <option value={provider.code} key={provider.code}>{provider.name}</option>)}</select></label><label>配置名称<input name="display_name" type="text" minLength={1} maxLength={80} autoComplete="off" defaultValue={editing?.displayName || ""} placeholder="例如：策略编辑主模型" required /></label></div>
        <div className="ai-provider-endpoint"><span>服务地址</span><strong title={selectedProviderData.baseUrl}>{selectedProviderData.baseUrl || "由系统配置并安全托管"}</strong><small>地址由系统安全托管，不接受自定义代理地址。</small></div>
        <label>模型名称<input name="model_name" type="text" minLength={1} maxLength={128} pattern="[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}" title="仅支持字母、数字、点、下划线、冒号、斜杠与连字符" list="ai-model-name-options" autoComplete="off" value={modelName} onChange={(event) => setModelName(event.target.value)} placeholder="选择或输入模型名称" required /><datalist id="ai-model-name-options">{selectedProviderData.models.map((model) => <option value={model} key={model} />)}</datalist></label>
        <label>API Key<input name="api_key" type="password" autoComplete="new-password" minLength={8} maxLength={2048} placeholder={editing ? "留空则保留当前已加密密钥" : "输入服务商 API Key"} required={!editing} /><small>{editing ? `当前${editing.apiKeyConfigured ? `已配置 ${editing.apiKeyFingerprint || "加密密钥"}` : "尚未配置密钥"}；留空不会替换。` : "保存后不会再次显示完整密钥。"}</small></label>
        <div className="ai-model-switches"><label><input name="is_enabled" type="checkbox" defaultChecked={editing?.isEnabled ?? true} /><span><strong>启用此配置</strong><small>关闭后策略功能不会调用此模型</small></span></label><label><input name="is_default" type="checkbox" defaultChecked={editing?.isDefault ?? false} /><span><strong>设为默认模型</strong><small>策略 AI 功能优先使用此配置</small></span></label></div>
        <p className={`message${modelFormError ? " error" : ""}`} role="alert" aria-live="assertive">{modelFormError}</p>
        <footer className="ai-model-dialog-actions"><button className="secondary" type="button" onClick={closeModelDialog}>取消</button><button className="primary" type="submit" disabled={working}>{working ? "保存中…" : "加密保存"}</button></footer>
      </form>
    </dialog>
  </>;
}
