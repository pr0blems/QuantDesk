import { useCallback, useEffect, useMemo, useState, type FormEvent } from "react";

import { paperApi, strategyApi } from "../api/quantdesk";
import type { ApiObject, StrategyListResponse } from "../api/types";
import { DataTable, EmptyState, ErrorPanel, FormActions, JsonPreview, LoadingPanel, MetricCard, Notice, PageHeader, Panel, StatusPill } from "../components/ui";
import { asList, asObject, booleanValue, firstList, numberValue, stringValue } from "../utils/data";
import { formatDate, formatMoney } from "../utils/format";

function accountRows(payload: ApiObject) {
  const rows = firstList(payload, "items", "accounts");
  return rows.length > 0 ? rows : asList(payload);
}

export function PaperPage() {
  const [accountsPayload, setAccountsPayload] = useState<ApiObject | null>(null);
  const [strategies, setStrategies] = useState<StrategyListResponse | null>(null);
  const [selectedId, setSelectedId] = useState("");
  const [dashboard, setDashboard] = useState<ApiObject | null>(null);
  const [showCreate, setShowCreate] = useState(false);
  const [loading, setLoading] = useState(true);
  const [working, setWorking] = useState(false);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");

  const accounts = useMemo(() => accountsPayload ? accountRows(accountsPayload) : [], [accountsPayload]);
  const selected = useMemo(() => accounts.find((item) => stringValue(item.id, "") === selectedId) ?? null, [accounts, selectedId]);

  const loadAccounts = useCallback(async (preferredId?: string) => {
    setLoading(true);
    setError("");
    try {
      const [nextAccounts, nextStrategies] = await Promise.all([paperApi.accounts(), strategyApi.list()]);
      const rows = accountRows(nextAccounts);
      setAccountsPayload(nextAccounts);
      setStrategies(nextStrategies);
      setSelectedId((current) => {
        const wanted = preferredId || current;
        if (rows.some((item) => stringValue(item.id, "") === wanted)) return wanted;
        return stringValue(rows.find((item) => stringValue(item.status) === "active")?.id ?? rows[0]?.id, "");
      });
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "模拟盘账户加载失败");
    } finally {
      setLoading(false);
    }
  }, []);

  const loadDashboard = useCallback(async (accountId: string) => {
    if (!accountId) { setDashboard(null); return; }
    setWorking(true);
    try {
      setDashboard(await paperApi.dashboard(accountId));
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "模拟盘数据加载失败");
    } finally {
      setWorking(false);
    }
  }, []);

  useEffect(() => { void loadAccounts(); }, [loadAccounts]);
  useEffect(() => { void loadDashboard(selectedId); }, [loadDashboard, selectedId]);

  async function createAccount(event: FormEvent<HTMLFormElement>): Promise<void> {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    setWorking(true);
    setError("");
    try {
      const created = await paperApi.create({
        name: stringValue(form.get("name"), "").trim(),
        strategy_id: stringValue(form.get("strategy_id"), ""),
        initial_balance: Number(form.get("initial_balance")),
        leverage: Number(form.get("leverage")),
        max_positions: Number(form.get("max_positions")),
        position_size_pct: Number(form.get("position_size_pct")),
        margin_cap: Number(form.get("margin_cap")),
      });
      const id = stringValue(created.id, "");
      setShowCreate(false);
      setMessage("模拟盘已创建并开始独立运行策略快照");
      await loadAccounts(id);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "模拟盘创建失败");
    } finally {
      setWorking(false);
    }
  }

  async function updateAccount(input: { name?: string; status?: "active" | "archived" | "paused" }, success: string): Promise<void> {
    if (!selectedId) return;
    setWorking(true);
    setError("");
    try {
      await paperApi.update(selectedId, input);
      setMessage(success);
      await loadAccounts(selectedId);
      await loadDashboard(selectedId);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "模拟盘状态更新失败");
    } finally {
      setWorking(false);
    }
  }

  async function rename(event: FormEvent<HTMLFormElement>): Promise<void> {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    const name = stringValue(form.get("name"), "").trim();
    if (name) await updateAccount({ name }, `模拟盘已重命名为“${name}”`);
  }

  async function reset(): Promise<void> {
    if (!selectedId || !window.confirm("确定重置当前模拟盘？持仓、成交和权益历史将被清空。")) return;
    setWorking(true);
    try {
      await paperApi.reset(selectedId);
      setMessage("当前模拟盘已恢复初始资金");
      await loadDashboard(selectedId);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "模拟盘重置失败");
    } finally {
      setWorking(false);
    }
  }

  const account = dashboard ? asObject(dashboard.account) : {};
  const stats = dashboard ? asObject(dashboard.stats) : {};
  const paperAccount = dashboard ? asObject(dashboard.paper_account) : {};
  const positions = dashboard ? firstList(dashboard, "positions") : [];
  const trades = dashboard ? firstList(dashboard, "trades") : [];
  const canReset = dashboard ? booleanValue(asObject(dashboard.permissions).can_reset) : false;

  return (
    <>
      <PageHeader
        eyebrow="PAPER EXECUTION"
        title="模拟盘"
        description="创建独立模拟账户、绑定策略、管理运行状态并复盘持仓与成交。"
        actions={<div className="inline-actions"><button className="button button--primary" type="button" onClick={() => setShowCreate((value) => !value)}>新增模拟盘</button><button className="button button--secondary" type="button" onClick={() => void loadAccounts(selectedId)}>刷新</button></div>}
      />
      {loading && !accountsPayload ? <LoadingPanel label="正在读取模拟盘账户…" /> : null}
      {error ? <ErrorPanel message={error} onRetry={() => void loadAccounts(selectedId)} /> : null}
      {message ? <Notice tone="success">{message}</Notice> : null}

      {showCreate ? (
        <Panel eyebrow="NEW ACCOUNT" title="创建模拟盘">
          <form className="form-grid" onSubmit={(event) => void createAccount(event)}>
            <label><span>名称</span><input name="name" required maxLength={100} placeholder="趋势策略模拟盘" /></label>
            <label><span>绑定策略</span><select name="strategy_id" required>{strategies?.items.filter((item) => item.status === "active").map((item) => <option key={item.public_id} value={item.public_id}>{item.name} · {item.category}</option>)}</select></label>
            <label><span>初始资金</span><input name="initial_balance" type="number" min="1" max="1000000000" defaultValue="10000" /></label>
            <label><span>杠杆</span><input name="leverage" type="number" min="1" max="50" defaultValue="5" /></label>
            <label><span>最大持仓数</span><input name="max_positions" type="number" min="1" max="50" defaultValue="5" /></label>
            <label><span>单笔仓位 %</span><input name="position_size_pct" type="number" min="0.01" max="100" step="0.01" defaultValue="5" /></label>
            <label><span>保证金上限</span><input name="margin_cap" type="number" min="0.01" max="0.95" step="0.01" defaultValue="0.5" /></label>
            <FormActions><button className="button button--primary" type="submit" disabled={working}>创建并运行</button><button className="button button--secondary" type="button" onClick={() => setShowCreate(false)}>取消</button></FormActions>
          </form>
        </Panel>
      ) : null}

      {accounts.length > 0 ? (
        <div className="account-switcher" role="tablist" aria-label="模拟盘账户">
          {accounts.map((item) => {
            const id = stringValue(item.id, "");
            return <button key={id} type="button" role="tab" aria-selected={selectedId === id} className={selectedId === id ? "account-switcher__item account-switcher__item--active" : "account-switcher__item"} onClick={() => setSelectedId(id)}><strong>{stringValue(item.name)}</strong><small>{stringValue(item.status)} · {stringValue(item.strategy_name ?? item.engine_key)}</small></button>;
          })}
        </div>
      ) : !loading ? <EmptyState title="尚无模拟盘" description="创建第一个模拟盘后即可在实时行情中验证策略。" /> : null}

      {selected ? (
        <Panel eyebrow="ACCOUNT CONTROL" title={stringValue(selected.name)} actions={<StatusPill tone={stringValue(selected.status) === "active" ? "success" : "warning"}>{stringValue(selected.status)}</StatusPill>}>
          <div className="control-toolbar">
            <form className="inline-form" onSubmit={(event) => void rename(event)}><input name="name" defaultValue={stringValue(selected.name, "")} aria-label="模拟盘新名称" /><button type="submit" disabled={working}>修改名称</button></form>
            <div className="inline-actions">
              <button type="button" disabled={working} onClick={() => void updateAccount({ status: stringValue(selected.status) === "paused" ? "active" : "paused" }, stringValue(selected.status) === "paused" ? "模拟盘已恢复运行" : "模拟盘已暂停")}>{stringValue(selected.status) === "paused" ? "继续运行" : "暂停运行"}</button>
              <button type="button" disabled={working || !canReset} onClick={() => void reset()}>重置账户</button>
              <button className="danger-action" type="button" disabled={working || positions.length > 0} onClick={() => { if (window.confirm(`归档模拟盘“${stringValue(selected.name)}”？`)) void updateAccount({ status: "archived" }, "模拟盘已归档"); }}>归档</button>
            </div>
          </div>
        </Panel>
      ) : null}

      {working && selectedId && !dashboard ? <LoadingPanel label="正在同步模拟盘状态…" /> : null}
      {dashboard ? (
        <>
          <section className="metric-grid">
            <MetricCard label="账户权益" value={formatMoney(numberValue(account.equity))} note={`收益率 ${numberValue(account.ret_pct).toFixed(2)}%`} tone={numberValue(account.ret_pct) >= 0 ? "success" : "danger"} />
            <MetricCard label="可用余额" value={formatMoney(numberValue(account.balance))} note={`保证金使用 ${numberValue(account.margin_usage).toFixed(1)}%`} />
            <MetricCard label="未实现盈亏" value={formatMoney(numberValue(account.upnl))} note={`今日 ${formatMoney(numberValue(account.today_pnl))}`} tone={numberValue(account.upnl) >= 0 ? "success" : "danger"} />
            <MetricCard label="已实现盈亏" value={formatMoney(numberValue(stats.realized))} note={`${numberValue(stats.trades)} 笔 · 胜率 ${numberValue(stats.win_rate).toFixed(1)}%`} />
            <MetricCard label="最大回撤" value={`${numberValue(stats.max_drawdown).toFixed(2)}%`} note={`盈亏比 ${stringValue(stats.profit_factor)}`} tone={numberValue(stats.max_drawdown) > 10 ? "danger" : "warning"} />
            <MetricCard label="运行策略" value={stringValue(paperAccount.strategy_name ?? paperAccount.engine_key)} note={stringValue(paperAccount.status)} />
          </section>
          <Panel eyebrow="OPEN POSITIONS" title="当前持仓" actions={<StatusPill>{positions.length} 个</StatusPill>}>
            <DataTable rows={positions} columns={[
              { key: "symbol", label: "合约" },
              { key: "side", label: "方向", render: (row) => numberValue(row.side) > 0 ? "多" : "空" },
              { key: "leverage", label: "杠杆", render: (row) => `${numberValue(row.leverage)}x` },
              { key: "qty", label: "数量" },
              { key: "avg_entry", label: "均价" },
              { key: "price", label: "现价" },
              { key: "upnl", label: "浮盈", render: (row) => formatMoney(numberValue(row.upnl)) },
              { key: "stop", label: "止损" },
              { key: "target", label: "目标" },
            ]} empty="暂无模拟持仓" />
          </Panel>
          <Panel eyebrow="TRADE HISTORY" title="成交记录" actions={<StatusPill>{trades.length} 笔</StatusPill>}>
            <DataTable rows={trades} columns={[
              { key: "closed_ts", label: "时间", render: (row) => formatDate(stringValue(row.closed_at ?? row.closed_ts, "")) },
              { key: "symbol", label: "合约" },
              { key: "side", label: "方向", render: (row) => numberValue(row.side) > 0 ? "多" : "空" },
              { key: "entry_price", label: "开仓" },
              { key: "exit_price", label: "平仓" },
              { key: "pnl", label: "盈亏", render: (row) => formatMoney(numberValue(row.pnl) - numberValue(row.fee)) },
              { key: "reason", label: "原因" },
            ]} empty="暂无成交记录" />
          </Panel>
          <JsonPreview value={{ curve: dashboard.curve, rules: dashboard.rules, disclaimer: dashboard.disclaimer }} label="权益曲线与成本模型数据" />
        </>
      ) : null}
    </>
  );
}
