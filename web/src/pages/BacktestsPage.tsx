import { useCallback, useEffect, useMemo, useState, type FormEvent } from "react";

import { backtestApi } from "../api/quantdesk";
import type { ApiObject, BacktestRunRequest } from "../api/types";
import { DataTable, EmptyState, ErrorPanel, FormActions, JsonPreview, LoadingPanel, MetricCard, Notice, PageHeader, Panel, StatusPill } from "../components/ui";
import { asObject, firstList, numberValue, parseJsonObject, stringValue } from "../utils/data";
import { formatDate, formatMoney } from "../utils/format";

function dateValue(date: Date): string {
  return date.toISOString().slice(0, 10);
}

function catalogRows(payload: ApiObject, key: string) {
  const value = payload[key];
  if (!Array.isArray(value)) return [];
  return value.map((item) => typeof item === "string" ? { value: item, label: item } : asObject(item));
}

function resultParts(detail: ApiObject) {
  const run = asObject(detail.run ?? detail);
  const result = asObject(detail.result ?? detail.output);
  return { run, result, metrics: asObject(result.metrics ?? detail.metrics), account: asObject(result.account ?? detail.account) };
}

export function BacktestsPage() {
  const [catalog, setCatalog] = useState<ApiObject | null>(null);
  const [history, setHistory] = useState<ApiObject | null>(null);
  const [detail, setDetail] = useState<ApiObject | null>(null);
  const [loading, setLoading] = useState(true);
  const [working, setWorking] = useState(false);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const [nextCatalog, nextHistory] = await Promise.all([backtestApi.catalog(), backtestApi.history()]);
      setCatalog(nextCatalog);
      setHistory(nextHistory);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "回测目录加载失败");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { void load(); }, [load]);

  const strategies = useMemo(() => catalog ? catalogRows(catalog, "strategies") : [], [catalog]);
  const symbols = useMemo(() => catalog ? catalogRows(catalog, "symbols") : [], [catalog]);
  const timeframes = useMemo(() => catalog ? catalogRows(catalog, "timeframes") : [], [catalog]);
  const historyRows = useMemo(() => history ? firstList(history, "items", "runs") : [], [history]);

  async function run(event: FormEvent<HTMLFormElement>): Promise<void> {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    setWorking(true);
    setError("");
    setMessage("");
    try {
      const input: BacktestRunRequest = {
        strategy_id: stringValue(form.get("strategy_id"), ""),
        symbol: stringValue(form.get("symbol"), "").toUpperCase(),
        timeframe: stringValue(form.get("timeframe"), "1h") as BacktestRunRequest["timeframe"],
        start_date: stringValue(form.get("start_date"), ""),
        end_date: stringValue(form.get("end_date"), ""),
        initial_capital: Number(form.get("initial_capital")),
        position_size_pct: Number(form.get("position_size_pct")),
        leverage: Number(form.get("leverage")),
        fee_bps: Number(form.get("fee_bps")),
        slippage_bps: Number(form.get("slippage_bps")),
        stop_loss_pct: Number(form.get("stop_loss_pct")),
        take_profit_pct: Number(form.get("take_profit_pct")),
        max_holding_bars: Number(form.get("max_holding_bars")),
        params: parseJsonObject(stringValue(form.get("params"), "{}"), "策略参数"),
      };
      let nextDetail = await backtestApi.run(input);
      const id = stringValue(nextDetail.id ?? asObject(nextDetail.run).id, "");
      if (!nextDetail.result && id) nextDetail = await backtestApi.detail(id);
      setDetail(nextDetail);
      setMessage("回测已完成，请优先检查数据质量和最大回撤");
      setHistory(await backtestApi.history());
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "回测运行失败");
    } finally {
      setWorking(false);
    }
  }

  async function openDetail(row: ApiObject): Promise<void> {
    const run = asObject(row.run ?? row);
    const id = stringValue(run.id ?? row.id, "");
    if (!id) return;
    setWorking(true);
    try {
      setDetail(await backtestApi.detail(id));
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "回测详情加载失败");
    } finally {
      setWorking(false);
    }
  }

  const end = new Date();
  const start = new Date(end);
  start.setUTCMonth(start.getUTCMonth() - 3);
  const parts = detail ? resultParts(detail) : null;
  const trades = parts ? firstList(parts.result, "trades") : [];
  const quality = parts ? asObject(parts.result.data_quality) : {};

  return (
    <>
      <PageHeader eyebrow="RESEARCH LAB" title="回测实验室" description="选择策略、行情区间与交易成本，运行可重现的历史验证并查看逐笔结果。" actions={<button className="button button--secondary" type="button" onClick={() => void load()} disabled={loading || working}>刷新目录</button>} />
      {loading && !catalog ? <LoadingPanel label="正在读取回测目录…" /> : null}
      {error ? <ErrorPanel message={error} onRetry={() => void load()} /> : null}
      {message ? <Notice tone="success">{message}</Notice> : null}

      {catalog ? (
        <section className="two-column-layout two-column-layout--wide-left">
          <Panel eyebrow="CONFIGURATION" title="运行新回测">
            {strategies.length && symbols.length ? (
              <form className="form-grid" onSubmit={(event) => void run(event)}>
                <label><span>策略</span><select name="strategy_id" required>{strategies.map((item) => <option key={stringValue(item.id ?? item.value)} value={stringValue(item.id ?? item.value)}>{stringValue(item.name ?? item.label)}</option>)}</select></label>
                <label><span>品种</span><select name="symbol" required>{symbols.map((item) => <option key={stringValue(item.value ?? item.symbol)} value={stringValue(item.value ?? item.symbol)}>{stringValue(item.label ?? item.symbol)}</option>)}</select></label>
                <label><span>周期</span><select name="timeframe" required>{timeframes.map((item) => <option key={stringValue(item.value ?? item.timeframe)} value={stringValue(item.value ?? item.timeframe)}>{stringValue(item.label ?? item.timeframe)}</option>)}</select></label>
                <label><span>开始日期</span><input name="start_date" type="date" required defaultValue={dateValue(start)} /></label>
                <label><span>结束日期</span><input name="end_date" type="date" required defaultValue={dateValue(end)} /></label>
                <label><span>初始资金</span><input name="initial_capital" type="number" min="1" defaultValue="10000" /></label>
                <label><span>仓位比例 %</span><input name="position_size_pct" type="number" min="0.01" max="100" step="0.01" defaultValue="10" /></label>
                <label><span>杠杆</span><input name="leverage" type="number" min="1" max="20" defaultValue="3" /></label>
                <label><span>手续费 bps</span><input name="fee_bps" type="number" min="0" max="1000" step="0.01" defaultValue="4" /></label>
                <label><span>滑点 bps</span><input name="slippage_bps" type="number" min="0" max="1000" step="0.01" defaultValue="2" /></label>
                <label><span>止损 %</span><input name="stop_loss_pct" type="number" min="0" max="99.9" step="0.1" defaultValue="3" /></label>
                <label><span>止盈 %</span><input name="take_profit_pct" type="number" min="0" max="99.9" step="0.1" defaultValue="6" /></label>
                <label><span>最大持有 K 线</span><input name="max_holding_bars" type="number" min="0" max="50000" defaultValue="120" /></label>
                <label className="form-grid__wide"><span>策略参数 JSON</span><textarea name="params" rows={6} defaultValue="{}" /></label>
                <FormActions><button className="button button--primary" type="submit" disabled={working}>{working ? "正在回放行情…" : "运行回测"}</button></FormActions>
              </form>
            ) : <EmptyState title="暂无可回测策略或行情" description="请先在策略中心启用策略并确认行情库存。" />}
          </Panel>

          <Panel eyebrow="HISTORY" title="最近回测" actions={<StatusPill>{historyRows.length} 条</StatusPill>}>
            <DataTable rows={historyRows} columns={[
              { key: "strategy", label: "策略", render: (row) => stringValue(asObject(row.run ?? row).strategy_name ?? asObject(row.run ?? row).strategy_id) },
              { key: "symbol", label: "品种", render: (row) => stringValue(asObject(row.run ?? row).symbol) },
              { key: "timeframe", label: "周期", render: (row) => stringValue(asObject(row.run ?? row).timeframe) },
              { key: "status", label: "状态", render: (row) => <StatusPill>{stringValue(asObject(row.run ?? row).status)}</StatusPill> },
              { key: "created_at", label: "时间", render: (row) => formatDate(stringValue(asObject(row.run ?? row).completed_at ?? asObject(row.run ?? row).created_at, "")) },
              { key: "action", label: "操作", render: (row) => <button type="button" onClick={() => void openDetail(row)}>查看</button> },
            ]} empty="暂无回测记录" />
          </Panel>
        </section>
      ) : null}

      {parts ? (
        <>
          <Panel eyebrow="RESULT" title={`${stringValue(parts.run.strategy_name ?? parts.run.strategy_id)} · ${stringValue(parts.run.symbol)}`} actions={<StatusPill tone="success">{stringValue(parts.run.status, "completed")}</StatusPill>}>
            <section className="metric-grid">
              <MetricCard label="累计收益" value={`${numberValue(parts.metrics.total_return_pct ?? parts.metrics.return_pct).toFixed(2)}%`} note={`期末 ${formatMoney(numberValue(parts.account.final_equity ?? parts.account.equity))}`} tone={numberValue(parts.metrics.total_return_pct ?? parts.metrics.return_pct) >= 0 ? "success" : "danger"} />
              <MetricCard label="最大回撤" value={`${numberValue(parts.metrics.max_drawdown_pct ?? parts.metrics.max_drawdown).toFixed(2)}%`} note="峰值至谷底" tone="warning" />
              <MetricCard label="夏普比率" value={numberValue(parts.metrics.sharpe_ratio ?? parts.metrics.sharpe).toFixed(2)} note="风险调整收益" />
              <MetricCard label="胜率" value={`${numberValue(parts.metrics.win_rate_pct ?? parts.metrics.win_rate).toFixed(1)}%`} note={`盈亏比 ${numberValue(parts.metrics.profit_factor).toFixed(2)}`} />
              <MetricCard label="交易次数" value={numberValue(parts.metrics.trade_count ?? trades.length)} note={`手续费 ${formatMoney(numberValue(parts.account.total_fees ?? parts.metrics.total_fees))}`} />
              <MetricCard label="数据质量" value={stringValue(quality.grade, "已检查")} note={`覆盖率 ${numberValue(quality.coverage_pct ?? quality.coverage).toFixed(2)}%`} tone="info" />
            </section>
            <JsonPreview value={{ equity_curve: parts.result.equity_curve, drawdown_curve: parts.result.drawdown_curve, data_quality: quality }} label="权益、回撤与数据质量" />
          </Panel>
          <Panel eyebrow="TRADES" title="逐笔成交" actions={<StatusPill>{trades.length} 笔</StatusPill>}>
            <DataTable rows={trades} columns={[
              { key: "entry_at", label: "开仓", render: (row) => formatDate(stringValue(row.entry_at, "")) },
              { key: "side", label: "方向" },
              { key: "entry_price", label: "开仓价" },
              { key: "exit_price", label: "平仓价" },
              { key: "quantity", label: "数量" },
              { key: "net_pnl", label: "净盈亏", render: (row) => formatMoney(numberValue(row.net_pnl ?? row.pnl)) },
              { key: "return_pct", label: "收益率", render: (row) => `${numberValue(row.return_pct).toFixed(2)}%` },
              { key: "exit_reason", label: "退出原因" },
            ]} empty="本次没有触发交易" />
          </Panel>
        </>
      ) : null}
      {detail ? <JsonPreview value={detail} label="完整回测证据" /> : null}
    </>
  );
}
