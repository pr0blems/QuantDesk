import { useCallback, useEffect, useMemo, useState, type FormEvent } from "react";

import { ApiError } from "../api/client";
import { liveApi, riskApi } from "../api/quantdesk";
import type {
  CurrentUser,
  LiveAccount,
  RiskControlScopeType,
  RuntimeIncident,
  TradingControlLatch,
  TradingReadiness,
} from "../api/types";
import {
  EmptyState,
  ErrorPanel,
  FormActions,
  LoadingPanel,
  MetricCard,
  Notice,
  PageHeader,
  Panel,
  StatusPill,
} from "../components/ui";

const scopeLabels: Record<RiskControlScopeType, string> = {
  global: "全局",
  account: "实盘账户",
  strategy_revision: "策略 Revision",
  symbol: "账户品种",
  data_source: "数据源",
  broker_connection: "交易通道",
};

function errorMessage(error: unknown): string {
  return error instanceof ApiError ? error.message : "风险控制请求失败";
}

function commandId(): string {
  return window.crypto.randomUUID();
}

function formText(values: FormData, name: string): string {
  const value = values.get(name);
  return typeof value === "string" ? value : "";
}

export function RiskPage({ user }: { user: CurrentUser }) {
  const [controls, setControls] = useState<TradingControlLatch[] | null>(null);
  const [accounts, setAccounts] = useState<LiveAccount[]>([]);
  const [readiness, setReadiness] = useState<TradingReadiness | null>(null);
  const [incidents, setIncidents] = useState<RuntimeIncident[]>([]);
  const [error, setError] = useState("");
  const [working, setWorking] = useState(false);
  const [scopeType, setScopeType] = useState<RiskControlScopeType>("account");
  const [accountId, setAccountId] = useState("");

  const load = useCallback(async () => {
    setError("");
    try {
      const [controlResponse, accountResponse, readinessResponse, incidentResponse] = await Promise.all([
        riskApi.controls(),
        liveApi.accounts(),
        riskApi.readiness(),
        riskApi.incidents(),
      ]);
      setControls(controlResponse.items);
      setAccounts(accountResponse.items);
      setReadiness(readinessResponse);
      setIncidents(incidentResponse.items);
      setAccountId((current) => current || accountResponse.items[0]?.id || "");
    } catch (nextError) {
      setError(errorMessage(nextError));
    }
  }, []);

  useEffect(() => { void load(); }, [load]);

  const engaged = useMemo(
    () => (controls ?? []).filter((item) => item.engaged),
    [controls],
  );

  function resolvedScopeKey(form: HTMLFormElement): string {
    const values = new FormData(form);
    if (scopeType === "global") return "*";
    if (scopeType === "account") return formText(values, "account_id");
    if (scopeType === "strategy_revision") {
      return formText(values, "strategy_revision").trim();
    }
    if (scopeType === "symbol") {
      const account = formText(values, "account_id").trim();
      const symbol = formText(values, "symbol").trim().toUpperCase();
      return `${account}:${symbol}`;
    }
    return formText(values, "service_key").trim().toLowerCase();
  }

  async function engageControl(event: FormEvent<HTMLFormElement>): Promise<void> {
    event.preventDefault();
    const form = event.currentTarget;
    const values = new FormData(form);
    const scopeKey = resolvedScopeKey(form);
    const reason = formText(values, "reason").trim();
    if (!window.confirm(`确认启用“${scopeLabels[scopeType]}”熔断？\n\n${reason}`)) return;
    const current = (controls ?? []).find(
      (item) => item.scope_type === scopeType && item.scope_key === scopeKey,
    );
    setWorking(true);
    setError("");
    try {
      await riskApi.transition({
        command_id: commandId(),
        action: "engage",
        scope_type: scopeType,
        scope_key: scopeKey,
        expected_version: current?.version ?? 0,
        reason_code: formText(values, "reason_code") || "operator_freeze",
        reason,
        confirmed: true,
      });
      form.reset();
      setAccountId(accounts[0]?.id || "");
      await load();
    } catch (nextError) {
      setError(errorMessage(nextError));
    } finally {
      setWorking(false);
    }
  }

  async function releaseControl(control: TradingControlLatch): Promise<void> {
    const reason = window.prompt(
      `解除“${scopeLabels[control.scope_type]} · ${control.scope_key}”熔断的原因：`,
      "风险复核和账户对账已经完成，人工确认恢复新增风险",
    )?.trim();
    if (!reason) return;
    if (!window.confirm("确认解除熔断？解除后符合其他门禁的策略可以再次开仓。")) return;
    setWorking(true);
    setError("");
    try {
      await riskApi.transition({
        command_id: commandId(),
        action: "release",
        scope_type: control.scope_type,
        scope_key: control.scope_key,
        expected_version: control.version,
        reason_code: "operator_recovery",
        reason,
        confirmed: true,
      });
      await load();
    } catch (nextError) {
      setError(errorMessage(nextError));
    } finally {
      setWorking(false);
    }
  }

  async function handleIncident(
    incident: RuntimeIncident,
    action: "acknowledge" | "resolve",
  ): Promise<void> {
    const verb = action === "acknowledge" ? "确认接管" : "解决";
    const note = window.prompt(
      `${verb}事故“${incident.title}”的处理说明：`,
      action === "acknowledge"
        ? "已由值班人员接管，正在执行风险复核与对账"
        : "故障原因已排除，证据和对账结果已复核",
    )?.trim();
    if (!note) return;
    setWorking(true);
    setError("");
    try {
      if (action === "acknowledge") {
        await riskApi.acknowledgeIncident(incident.id, note);
      } else {
        await riskApi.resolveIncident(incident.id, note);
      }
      await load();
    } catch (nextError) {
      setError(errorMessage(nextError));
    } finally {
      setWorking(false);
    }
  }

  if (controls === null && !error) return <LoadingPanel label="正在读取风险控制状态…" />;

  return <>
    <PageHeader
      eyebrow="RISK CONTROL PLANE"
      title="风险控制"
      description="持久化分层熔断只阻止新增风险；平仓与保护单仍保持可用。所有启用和恢复操作均要求确认、版本校验和审计。"
      actions={<button className="button button--secondary" type="button" onClick={() => void load()} disabled={working}>刷新状态</button>}
    />
    {error ? <ErrorPanel message={error} onRetry={() => void load()} /> : null}
    <section className="metric-grid">
      <MetricCard label="新增风险状态" value={readiness?.ready_for_new_risk ? "允许" : "阻断"} note={readiness?.blockers.join(" · ") || "全部基础设施门禁通过"} tone={readiness?.ready_for_new_risk ? "success" : "danger"} />
      <MetricCard label="Worker 在线" value={`${readiness?.workers.filter((item) => item.fresh).length ?? 0} / ${readiness?.workers.length ?? 0}`} note="行情、影子、模拟、实盘与运维进程独立心跳" />
      <MetricCard label="未解决 P0" value={readiness?.open_p0_incident_count ?? 0} note="P0 事故会阻断新增风险" tone={readiness?.open_p0_incident_count ? "danger" : "success"} />
      <MetricCard label="当前生效熔断" value={engaged.length} note={`${accounts.length} 个实盘账户；任一匹配层级都会拒绝新开仓`} tone={engaged.length ? "danger" : "success"} />
    </section>

    <Panel eyebrow="RUNTIME INCIDENTS" title="运行事故与告警">
      {incidents.length ? <div className="table-scroll"><table className="data-table"><thead><tr><th>级别</th><th>状态</th><th>事故</th><th>来源</th><th>次数</th><th>最后出现</th><th>操作</th></tr></thead><tbody>{incidents.map((incident) => <tr key={incident.id}><td><StatusPill tone={incident.severity === "P0" ? "danger" : incident.severity === "P1" ? "warning" : "neutral"}>{incident.severity}</StatusPill></td><td>{incident.status === "open" ? "未处理" : incident.status === "acknowledged" ? "已接管" : "已解决"}</td><td>{incident.title}<small className="table-note">{incident.category}</small></td><td><code>{incident.source_type}:{incident.source_key}</code></td><td>{incident.occurrence_count}</td><td>{new Date(incident.last_seen_at).toLocaleString()}</td><td>{user.is_admin && incident.status !== "resolved" ? <div className="table-actions">{incident.status === "open" ? <button className="button button--secondary button--small" type="button" disabled={working} onClick={() => void handleIncident(incident, "acknowledge")}>接管</button> : null}<button className="button button--secondary button--small" type="button" disabled={working} onClick={() => void handleIncident(incident, "resolve")}>解决</button></div> : "--"}</td></tr>)}</tbody></table></div> : <EmptyState title="当前没有运行事故" description="Worker 心跳、实盘订单未知状态与修订门禁均未触发告警。" />}
    </Panel>

    <div className="content-grid content-grid--wide">
      <Panel eyebrow="OPERATOR COMMAND" title="启用熔断">
        <form className="stack-form panel-form" onSubmit={(event) => void engageControl(event)}>
          <label><span>控制层级</span><select value={scopeType} onChange={(event) => setScopeType(event.target.value as RiskControlScopeType)}>
            <option value="account">实盘账户</option>
            <option value="strategy_revision">策略 Revision</option>
            <option value="symbol">账户品种</option>
            {user.is_admin ? <option value="global">全局</option> : null}
            {user.is_admin ? <option value="data_source">数据源</option> : null}
            {user.is_admin ? <option value="broker_connection">交易通道</option> : null}
          </select></label>
          {scopeType === "account" || scopeType === "symbol" ? <label><span>实盘账户</span><select name="account_id" required value={accountId} onChange={(event) => setAccountId(event.target.value)}><option value="" disabled>请选择账户</option>{accounts.map((account) => <option key={account.id} value={account.id}>{account.name} · {account.status}</option>)}</select></label> : null}
          {scopeType === "strategy_revision" ? <label><span>策略 Revision 控制键</span><input name="strategy_revision" required placeholder="strategy_public_id@version" pattern="[0-9a-fA-F-]{36}@[1-9][0-9]*" /></label> : null}
          {scopeType === "symbol" ? <label><span>合约品种</span><input name="symbol" required placeholder="AAPLUSDT" pattern="[A-Za-z0-9._-]{2,32}" /></label> : null}
          {scopeType === "data_source" || scopeType === "broker_connection" ? <label><span>{scopeType === "data_source" ? "数据源键" : "交易通道键"}</span><input name="service_key" required defaultValue={scopeType === "data_source" ? "market_data" : "binance-usdm"} pattern="[a-z0-9][a-z0-9._:/-]{0,63}" /></label> : null}
          {scopeType === "global" ? <Notice tone="danger">全局熔断将停止平台所有自动新增风险，但不会阻断保护单和风险降低操作。</Notice> : null}
          <label><span>原因代码</span><input name="reason_code" required defaultValue="operator_freeze" pattern="[a-z][a-z0-9_]{2,63}" /></label>
          <label><span>操作原因</span><textarea name="reason" required minLength={10} maxLength={500} placeholder="说明触发原因、影响范围和恢复前必须完成的检查" /></label>
          <FormActions><button className="button button--danger" type="submit" disabled={working || ((scopeType === "account" || scopeType === "symbol") && !accountId)}>确认启用熔断</button></FormActions>
        </form>
      </Panel>

      <Panel eyebrow="ACTIVE LATCHES" title="控制状态">
        {controls?.length ? <div className="table-scroll"><table className="data-table"><thead><tr><th>状态</th><th>层级</th><th>控制键</th><th>原因</th><th>版本</th><th>操作</th></tr></thead><tbody>{controls.map((control) => <tr key={control.id}><td><StatusPill tone={control.engaged ? "danger" : "neutral"}>{control.engaged ? "已熔断" : "已解除"}</StatusPill></td><td>{scopeLabels[control.scope_type]}</td><td><code>{control.scope_key}</code></td><td>{control.reason || "--"}<small className="table-note">{control.reason_code || "--"}</small></td><td>v{control.version}</td><td>{control.engaged && (control.owner_scope !== "global" || user.is_admin) ? <button className="button button--secondary button--small" type="button" disabled={working} onClick={() => void releaseControl(control)}>人工恢复</button> : "--"}</td></tr>)}</tbody></table></div> : <EmptyState title="当前没有风险控制记录" description="系统处于正常状态；首次启用后会保留完整版本和审计链。" />}
      </Panel>
    </div>
  </>;
}
