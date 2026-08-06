import { useCallback, useEffect, useMemo, useRef, useState, type FormEvent } from "react";

import { liveApi, strategyApi } from "../api/quantdesk";
import type { LiveAccountCreateRequest, LiveAccountsResponse, LiveDashboardResponse, StrategyListResponse } from "../api/types";
import { DataTable, EmptyState, ErrorPanel, FormActions, JsonPreview, LoadingPanel, MetricCard, Notice, PageHeader, Panel, StatusPill, Tabs } from "../components/ui";
import { asObject, numberValue, stringValue } from "../utils/data";
import { formatDate, formatMoney } from "../utils/format";

type LiveTab = "dashboard" | "accounts" | "risk";

const defaultRisk: Omit<LiveAccountCreateRequest, "name" | "strategy_id"> = {
  leverage: 3,
  margin_cap: 0.2,
  max_positions: 1,
  position_size_pct: 2,
  risk_per_trade_pct: 0.5,
  daily_loss_limit_pct: 2,
  max_drawdown_pct: 6,
  max_total_risk_pct: 4,
  risk_max_leverage: 10,
  short_risk_multiplier: 0.5,
  liquidation_buffer_pct: 1.5,
  max_cluster_positions: 2,
  max_signal_age_seconds: 18000,
  max_ticker_age_seconds: 120,
  block_high_risk_products: true,
};

function accountTone(status: string): "danger" | "success" | "warning" | "neutral" {
  if (status === "running") return "success";
  if (status === "armed") return "warning";
  if (status === "error" || status === "safe_mode") return "danger";
  return "neutral";
}

function numericForm(values: FormData, name: string, fallback: number): number {
  const parsed = Number(values.get(name));
  return Number.isFinite(parsed) ? parsed : fallback;
}

export function LivePage() {
  const [tab, setTab] = useState<LiveTab>("dashboard");
  const [accounts, setAccounts] = useState<LiveAccountsResponse | null>(null);
  const [strategies, setStrategies] = useState<StrategyListResponse | null>(null);
  const [selectedId, setSelectedId] = useState("");
  const [dashboard, setDashboard] = useState<LiveDashboardResponse | null>(null);
  const [loadingAccounts, setLoadingAccounts] = useState(true);
  const [loadingDashboard, setLoadingDashboard] = useState(false);
  const [working, setWorking] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [armName, setArmName] = useState("");
  const [armAcknowledged, setArmAcknowledged] = useState(false);
  const dashboardRequestSequence = useRef(0);

  const loadAccounts = useCallback(async () => {
    setLoadingAccounts(true);
    setError("");
    try {
      const [nextAccounts, nextStrategies] = await Promise.all([liveApi.accounts(), strategyApi.list()]);
      setAccounts(nextAccounts);
      setStrategies(nextStrategies);
      setSelectedId((current) => nextAccounts.items.some((item) => item.id === current) ? current : nextAccounts.items[0]?.id ?? "");
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "实盘账户读取失败");
    } finally {
      setLoadingAccounts(false);
    }
  }, []);

  const loadDashboard = useCallback(async (accountId: string) => {
    const requestSequence = ++dashboardRequestSequence.current;
    if (!accountId) { setDashboard(null); setLoadingDashboard(false); return; }
    setLoadingDashboard(true);
    setError("");
    try {
      const nextDashboard = await liveApi.dashboard(accountId);
      if (requestSequence !== dashboardRequestSequence.current) return;
      if (nextDashboard.live_account.id !== accountId) throw new Error("实盘账户响应身份不匹配");
      setDashboard(nextDashboard);
    } catch (caught) {
      if (requestSequence !== dashboardRequestSequence.current) return;
      setDashboard(null);
      setError(caught instanceof Error ? caught.message : "实盘状态读取失败");
    } finally {
      if (requestSequence === dashboardRequestSequence.current) setLoadingDashboard(false);
    }
  }, []);

  useEffect(() => { void loadAccounts(); }, [loadAccounts]);
  useEffect(() => { void loadDashboard(selectedId); }, [loadDashboard, selectedId]);

  const selectedAccount = useMemo(() => accounts?.items.find((item) => item.id === selectedId) ?? null, [accounts, selectedId]);
  const selectedConfig = asObject(selectedAccount?.config);

  function refresh(): void {
    void loadAccounts();
    if (selectedId) void loadDashboard(selectedId);
  }

  async function perform(action: () => Promise<unknown>, success: string): Promise<void> {
    setWorking(true); setError(""); setNotice("");
    try {
      await action();
      setNotice(success);
      await loadAccounts();
      if (selectedId) await loadDashboard(selectedId);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "实盘操作失败");
    } finally { setWorking(false); }
  }

  function accountPayload(values: FormData): LiveAccountCreateRequest {
    return {
      name: stringValue(values.get("name")),
      strategy_id: stringValue(values.get("strategy_id")),
      leverage: numericForm(values, "leverage", defaultRisk.leverage),
      margin_cap: numericForm(values, "margin_cap", defaultRisk.margin_cap),
      max_positions: numericForm(values, "max_positions", defaultRisk.max_positions),
      position_size_pct: numericForm(values, "position_size_pct", defaultRisk.position_size_pct),
      risk_per_trade_pct: numericForm(values, "risk_per_trade_pct", defaultRisk.risk_per_trade_pct),
      daily_loss_limit_pct: numericForm(values, "daily_loss_limit_pct", defaultRisk.daily_loss_limit_pct),
      max_drawdown_pct: numericForm(values, "max_drawdown_pct", defaultRisk.max_drawdown_pct),
      max_total_risk_pct: numericForm(values, "max_total_risk_pct", defaultRisk.max_total_risk_pct),
      risk_max_leverage: numericForm(values, "risk_max_leverage", defaultRisk.risk_max_leverage),
      short_risk_multiplier: numericForm(values, "short_risk_multiplier", defaultRisk.short_risk_multiplier),
      liquidation_buffer_pct: numericForm(values, "liquidation_buffer_pct", defaultRisk.liquidation_buffer_pct),
      max_cluster_positions: numericForm(values, "max_cluster_positions", defaultRisk.max_cluster_positions),
      max_signal_age_seconds: numericForm(values, "max_signal_age_seconds", defaultRisk.max_signal_age_seconds),
      max_ticker_age_seconds: numericForm(values, "max_ticker_age_seconds", defaultRisk.max_ticker_age_seconds),
      block_high_risk_products: values.get("block_high_risk_products") === "on",
    };
  }

  async function createAccount(event: FormEvent<HTMLFormElement>): Promise<void> {
    event.preventDefault();
    const input = accountPayload(new FormData(event.currentTarget));
    await perform(async () => {
      const result = await liveApi.create(input);
      const id = stringValue(result.id ?? asObject(result.item).id);
      if (id) setSelectedId(id);
      event.currentTarget.reset();
    }, "实盘账户已创建，尚未武装");
  }

  async function saveRisk(event: FormEvent<HTMLFormElement>): Promise<void> {
    event.preventDefault();
    if (!selectedAccount) return;
    const input = accountPayload(new FormData(event.currentTarget));
    await perform(() => liveApi.updateStrategy(selectedAccount.id, {
      strategy_id: input.strategy_id,
      leverage: input.leverage,
      margin_cap: input.margin_cap,
      max_positions: input.max_positions,
      position_size_pct: input.position_size_pct,
      risk_per_trade_pct: input.risk_per_trade_pct,
      daily_loss_limit_pct: input.daily_loss_limit_pct,
      max_drawdown_pct: input.max_drawdown_pct,
      max_total_risk_pct: input.max_total_risk_pct,
      risk_max_leverage: input.risk_max_leverage,
      short_risk_multiplier: input.short_risk_multiplier,
      liquidation_buffer_pct: input.liquidation_buffer_pct,
      max_cluster_positions: input.max_cluster_positions,
      max_signal_age_seconds: input.max_signal_age_seconds,
      max_ticker_age_seconds: input.max_ticker_age_seconds,
      block_high_risk_products: input.block_high_risk_products,
    }), "策略绑定与风险参数已更新");
  }

  return (
    <>
      <PageHeader eyebrow="LIVE EXECUTION" title="实盘控制台" description="创建与武装实盘账户，绑定版本化策略，维护风险边界，并核对交易所、持仓、挂单和执行意图。" actions={<div className="live-actions">{accounts?.items.length ? <label className="account-select"><span>实盘账户</span><select value={selectedId} onChange={(event) => setSelectedId(event.target.value)}>{accounts.items.map((account) => <option key={account.id} value={account.id}>{account.name}</option>)}</select></label> : null}<button className="button button--secondary" type="button" onClick={refresh} disabled={loadingAccounts || loadingDashboard || working}>刷新状态</button></div>} />
      <Tabs value={tab} onChange={setTab} label="实盘功能" items={[{ value: "dashboard", label: "执行仪表盘" }, { value: "accounts", label: "账户控制" }, { value: "risk", label: "策略与风控" }]} />
      {notice ? <Notice tone="success">{notice}</Notice> : null}
      {error ? <ErrorPanel message={error} onRetry={refresh} /> : null}
      {loadingAccounts && !accounts ? <LoadingPanel label="正在读取实盘账户…" /> : null}

      {accounts ? <section className="safety-banner"><div><span className={accounts.system_enabled ? "safety-banner__light safety-banner__light--on" : "safety-banner__light"} /><p><strong>{accounts.system_enabled ? "实盘系统已启用" : "实盘系统总开关关闭"}</strong><small>{accounts.universe.label} · {accounts.universe.count} 个标的</small></p></div><div className="safety-banner__checks"><StatusPill tone={accounts.credentials_configured ? "success" : "warning"}>凭证 {accounts.credentials_configured ? "已配置" : "缺失"}</StatusPill><StatusPill tone={accounts.trade_permission_requested ? "success" : "warning"}>交易权限 {accounts.trade_permission_requested ? "已申请" : "未申请"}</StatusPill></div></section> : null}

      {selectedAccount ? <section className="metric-grid"><MetricCard label="运行状态" value={selectedAccount.status.toUpperCase()} note={selectedAccount.last_error_code ?? "没有运行错误"} tone={accountTone(selectedAccount.status)} /><MetricCard label="绑定策略" value={selectedAccount.strategy_name ?? "未绑定"} note={selectedAccount.engine_key ?? "等待策略快照"} /><MetricCard label="交易所连接" value={dashboard?.binance.connected ? "CONNECTED" : loadingDashboard ? "CHECK" : "OFFLINE"} note={dashboard?.binance.error_category ?? "状态已同步"} tone={dashboard?.binance.connected ? "success" : "warning"} /><MetricCard label="最近心跳" value={selectedAccount.last_tick_at ? "RECEIVED" : "--"} note={formatDate(selectedAccount.last_tick_at)} tone={selectedAccount.last_tick_at ? "info" : "neutral"} /></section> : null}

      {tab === "dashboard" ? <>
        {!selectedAccount && accounts ? <EmptyState title="尚无实盘账户" description="切换到“账户控制”创建第一个账户；新账户默认不会触达真实资金。" /> : null}
        {loadingDashboard && !dashboard ? <LoadingPanel label="正在同步交易所状态…" /> : null}
        {dashboard ? <>
          <section className="live-grid"><Panel eyebrow="BINANCE ACCOUNT" title="账户快照" actions={<StatusPill tone={dashboard.binance.connected ? "success" : "warning"}>{dashboard.binance.connected ? "已连接" : "未连接"}</StatusPill>}><div className="balance-hero"><span>钱包余额</span><strong>{formatMoney(dashboard.binance.wallet_balance)}</strong><small>{dashboard.binance.account_type ?? "账户类型未知"}</small></div><dl className="balance-details"><div><dt>可用余额</dt><dd>{formatMoney(dashboard.binance.available_balance)}</dd></div><div><dt>未实现盈亏</dt><dd>{formatMoney(dashboard.binance.unrealized_pnl)}</dd></div><div><dt>持仓数量</dt><dd>{dashboard.positions.length}</dd></div><div><dt>当前挂单</dt><dd>{dashboard.open_orders.length}</dd></div></dl></Panel>
            <Panel eyebrow="ORDER INTENTS" title="最近执行意图" actions={<StatusPill tone="neutral">{dashboard.order_intents.length} 条</StatusPill>}><DataTable rows={dashboard.order_intents as unknown as Record<string, unknown>[]} columns={[{ key: "symbol", label: "标的" }, { key: "side", label: "方向" }, { key: "action", label: "动作" }, { key: "order_type", label: "类型" }, { key: "quantity", label: "数量" }, { key: "status", label: "状态" }, { key: "created_at", label: "时间", render: (row) => formatDate(stringValue(row.created_at)) }]} /></Panel></section>
          <div className="two-column-layout top-gap"><Panel eyebrow="POSITIONS" title="当前持仓"><DataTable rows={dashboard.positions} columns={[{ key: "symbol", label: "标的" }, { key: "position_side", label: "方向" }, { key: "position_amt", label: "数量" }, { key: "entry_price", label: "入场价" }, { key: "unrealized_pnl", label: "浮动盈亏" }]} /></Panel><Panel eyebrow="OPEN ORDERS" title="当前挂单"><DataTable rows={dashboard.open_orders} columns={[{ key: "symbol", label: "标的" }, { key: "side", label: "方向" }, { key: "type", label: "类型" }, { key: "status", label: "状态" }, { key: "order_id", label: "订单号" }]} /></Panel></div>
          <JsonPreview value={dashboard} label="完整执行证据" />
        </> : null}
      </> : null}

      {tab === "accounts" ? <div className="two-column-layout">
        <Panel eyebrow="NEW ACCOUNT" title="创建实盘账户"><form className="stack-form panel-form" onSubmit={(event) => void createAccount(event)}><div className="form-grid"><label><span>账户名称</span><input name="name" required maxLength={80} /></label><label><span>策略</span><select name="strategy_id" required defaultValue=""><option value="" disabled>请选择策略</option>{strategies?.items.filter((item) => item.status !== "archived").map((item) => <option key={item.public_id} value={item.public_id}>{item.name} · v{item.version}</option>)}</select></label><RiskFields values={defaultRisk} /></div><label className="check-field"><input type="checkbox" name="block_high_risk_products" defaultChecked /><span>阻止高风险产品</span></label><Notice tone="warning">创建只生成账户和风险快照，不会自动武装或下单。</Notice><FormActions><button className="button button--primary" type="submit" disabled={working}>创建账户</button></FormActions></form></Panel>
        <Panel eyebrow="ACCOUNT CONTROL" title="状态控制">{selectedAccount ? <div className="stack-form panel-form"><label><span>当前账户</span><select value={selectedId} onChange={(event) => setSelectedId(event.target.value)}>{accounts?.items.map((item) => <option key={item.id} value={item.id}>{item.name} · {item.status}</option>)}</select></label><form className="inline-form" onSubmit={(event) => { event.preventDefault(); const name = stringValue(new FormData(event.currentTarget).get("name")); if (name) void perform(() => liveApi.update(selectedAccount.id, { name }), "账户已重命名"); }}><input name="name" defaultValue={selectedAccount.name} aria-label="账户名称" /><button className="button button--secondary" type="submit" disabled={working}>重命名</button></form><div className="control-toolbar"><button className="button button--secondary" type="button" disabled={working || selectedAccount.status === "paused"} onClick={() => void perform(() => liveApi.update(selectedAccount.id, { status: "paused" }), "账户已暂停")}>暂停</button><button className="button button--danger" type="button" disabled={working || selectedAccount.status === "archived"} onClick={() => { if (window.confirm(`确认归档实盘账户“${selectedAccount.name}”？`)) void perform(() => liveApi.update(selectedAccount.id, { status: "archived" }), "账户已归档"); }}>归档</button></div><fieldset className="danger-zone"><legend>真实资金武装确认</legend><label><span>输入账户全名：{selectedAccount.name}</span><input value={armName} onChange={(event) => setArmName(event.target.value)} autoComplete="off" /></label><label className="check-field"><input type="checkbox" checked={armAcknowledged} onChange={(event) => setArmAcknowledged(event.target.checked)} /><span>我确认该操作可能使用真实资金</span></label><button className="button button--danger" type="button" disabled={working || !armAcknowledged || armName !== selectedAccount.name} onClick={() => void perform(() => liveApi.arm(selectedAccount.id, { confirmation_name: armName, acknowledge_real_funds: true }), "账户已武装")}>武装实盘账户</button></fieldset></div> : <EmptyState title="暂无可控账户" description="先创建一个实盘账户。" />}</Panel>
      </div> : null}

      {tab === "risk" ? <Panel eyebrow="STRATEGY SNAPSHOT" title="策略绑定与硬风险边界">{selectedAccount ? <form className="stack-form panel-form" onSubmit={(event) => void saveRisk(event)} key={`${selectedAccount.id}-${selectedAccount.updated_at}`}><input type="hidden" name="name" value={selectedAccount.name} /><div className="form-grid"><label><span>策略</span><select name="strategy_id" defaultValue={selectedAccount.strategy_id ?? ""} required>{strategies?.items.filter((item) => item.status !== "archived").map((item) => <option key={item.public_id} value={item.public_id}>{item.name} · v{item.version}</option>)}</select></label><RiskFields values={{ ...defaultRisk, ...Object.fromEntries(Object.keys(defaultRisk).map((key) => [key, selectedConfig[key] ?? defaultRisk[key as keyof typeof defaultRisk]])) }} /></div><label className="check-field"><input type="checkbox" name="block_high_risk_products" defaultChecked={selectedConfig.block_high_risk_products !== false} /><span>阻止高风险产品</span></label><Notice>保存后生成新的账户策略快照；服务端风控仍是最终裁决者。</Notice><FormActions><button className="button button--primary" type="submit" disabled={working}>保存策略与风控</button></FormActions></form> : <EmptyState title="请选择账户" description="账户创建后可维护版本化策略绑定和风险边界。" />}</Panel> : null}
    </>
  );
}

function RiskFields({ values }: { values: Record<string, unknown> }) {
  const field = (name: string, label: string, step = "0.1", min = "0") => <label key={name}><span>{label}</span><input type="number" name={name} step={step} min={min} defaultValue={numberValue(values[name])} required /></label>;
  return <>{field("leverage", "杠杆", "1", "1")}{field("margin_cap", "保证金上限", "0.01")}{field("max_positions", "最大持仓数", "1", "1")}{field("position_size_pct", "单仓比例 %")}{field("risk_per_trade_pct", "单笔风险 %")}{field("daily_loss_limit_pct", "日亏损上限 %")}{field("max_drawdown_pct", "最大回撤 %")}{field("max_total_risk_pct", "总风险上限 %")}{field("risk_max_leverage", "风控最大杠杆", "1", "1")}{field("short_risk_multiplier", "做空风险系数")}{field("liquidation_buffer_pct", "强平缓冲 %")}{field("max_cluster_positions", "同簇最大持仓", "1", "1")}{field("max_signal_age_seconds", "信号最大年龄（秒）", "1")}{field("max_ticker_age_seconds", "行情最大年龄（秒）", "1")}</>;
}
