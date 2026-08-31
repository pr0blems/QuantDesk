import { useCallback, useEffect, useMemo, useState } from "react";

import { dashboardApi, settingsApi } from "../api/quantdesk";
import type { BinancePerformance, CurrentUser, DashboardPerformance } from "../api/types";
import { asObject, booleanValue, numberValue, stringValue } from "../utils/data";

function monthKey(date: Date): string {
  return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, "0")}`;
}

function money(value: number | null | undefined, currency = "USDT"): string {
  if (typeof value !== "number" || !Number.isFinite(value)) return "--";
  const sign = value > 0 ? "+" : "";
  return `${sign}${value.toLocaleString("zh-CN", { minimumFractionDigits: 2, maximumFractionDigits: 2 })} ${currency}`;
}

function percent(value: number | null | undefined): string {
  if (typeof value !== "number" || !Number.isFinite(value)) return "--";
  return `${value > 0 ? "+" : ""}${value.toFixed(2)}%`;
}

function tone(value: number | null | undefined): "flat" | "loss" | "profit" {
  if (!value) return "flat";
  return value > 0 ? "profit" : "loss";
}

type CalendarRow = { date: string; pnl: number; trades?: number; realized_records?: number };

function ReturnsCalendar({ rows, currency, emptyText, eventName }: { rows: CalendarRow[]; currency: string; emptyText: string; eventName: string }) {
  const sourceMonth = rows[0]?.date.slice(0, 7) ?? monthKey(new Date());
  const [year, month] = sourceMonth.split("-").map(Number) as [number, number];
  const byDate = new Map(rows.map((row) => [row.date, row]));
  const offset = (new Date(year, month - 1, 1).getDay() + 6) % 7;
  const dayCount = new Date(year, month, 0).getDate();
  const today = new Date();
  const todayKey = `${today.getFullYear()}-${String(today.getMonth() + 1).padStart(2, "0")}-${String(today.getDate()).padStart(2, "0")}`;
  const cells: Array<{ blank?: true; day?: number; key: string; row?: CalendarRow }> = [];
  for (let index = 0; index < offset; index += 1) cells.push({ blank: true, key: `before-${index}` });
  for (let day = 1; day <= dayCount; day += 1) {
    const key = `${sourceMonth}-${String(day).padStart(2, "0")}`;
    const row = byDate.get(key);
    cells.push({ day, key, ...(row ? { row } : {}) });
  }
  while (cells.length % 7) cells.push({ blank: true, key: `after-${cells.length}` });

  return <>
    <div className="calendar-weekdays" role="row">{["一", "二", "三", "四", "五", "六", "日"].map((label) => <span role="columnheader" key={label}>{label}</span>)}</div>
    <div className="calendar-days" role="rowgroup">{cells.map((cell) => {
      if (cell.blank) return <span className="calendar-day blank" aria-hidden="true" key={cell.key} />;
      const row = cell.row;
      return <div className={`calendar-day ${row ? tone(row.pnl) : "empty"}${cell.key === todayKey ? " today" : ""}`} role="gridcell" key={cell.key} aria-label={`${cell.day}日，${row ? money(row.pnl, currency) : "无收益数据"}`}>
        <span className="calendar-day-number">{cell.day}</span>{cell.key === todayKey ? <em>今天</em> : null}<strong>{row ? money(row.pnl, currency) : "--"}</strong><small>{row ? `${row.trades ?? row.realized_records ?? 0} ${eventName}` : "无数据"}</small>
      </div>;
    })}</div>
    {rows.length === 0 ? <div className="performance-status empty" role="status">{emptyText}</div> : null}
  </>;
}

export function OverviewPage({ user }: { user: CurrentUser }) {
  const [viewMonth, setViewMonth] = useState(() => new Date(new Date().getFullYear(), new Date().getMonth(), 1));
  const [paper, setPaper] = useState<DashboardPerformance | null>(null);
  const [binance, setBinance] = useState<BinancePerformance | null>(null);
  const [selectedAssetCode, setSelectedAssetCode] = useState("");
  const [accountPayload, setAccountPayload] = useState<Record<string, unknown>>({});
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    setLoading(true);
    const month = monthKey(viewMonth);
    const [paperResult, binanceResult, accountResult] = await Promise.allSettled([dashboardApi.performance(month), dashboardApi.binancePerformance(month), settingsApi.binanceAccount()]);
    setPaper(paperResult.status === "fulfilled" ? paperResult.value : null);
    setBinance(binanceResult.status === "fulfilled" ? binanceResult.value : null);
    setAccountPayload(accountResult.status === "fulfilled" ? accountResult.value : {});
    setLoading(false);
  }, [viewMonth]);

  useEffect(() => { void load(); }, [load]);

  const account = asObject(accountPayload.account ?? accountPayload);
  const configured = booleanValue(account.configured ?? user.binance_credentials_configured);
  const connected = booleanValue(account.connected);
  const tradeEnabled = booleanValue(account.can_trade ?? account.trade_permission_requested);
  const asset = binance?.assets.find((item) => item.asset === selectedAssetCode) ?? binance?.assets[0] ?? null;
  const currentMonth = monthKey(new Date());
  const canNext = monthKey(viewMonth) < currentMonth;
  const paperRows = useMemo(() => (paper?.calendar.days ?? []).map((day) => ({ date: day.date, pnl: day.pnl, trades: day.trades })), [paper]);
  const binanceRows = useMemo(() => (asset?.days ?? []).map((day) => ({ date: day.date, pnl: day.net_income, realized_records: day.realized_records })), [asset]);

  useEffect(() => {
    if (!binance?.assets.length) { setSelectedAssetCode(""); return; }
    if (!binance.assets.some((item) => item.asset === selectedAssetCode)) setSelectedAssetCode(binance.assets[0]!.asset);
  }, [binance, selectedAssetCode]);

  function shiftMonth(delta: number): void {
    setViewMonth((current) => new Date(current.getFullYear(), current.getMonth() + delta, 1));
  }

  return <>
    <div className="page-heading"><div><span className="eyebrow">CONTROL CENTER</span><h1>交易工作台</h1><p>账户状态、系统连接与关键操作概览</p></div><span className="live-lock"><i />实盘默认关闭</span></div>
    <div className="summary-grid">
      <article className="metric-card"><span>当前用户</span><strong>{user.username}</strong><small>多用户隔离已启用</small></article>
      <article className={`metric-card account-metric-card ${loading ? "loading" : !configured ? "unconfigured" : connected ? tradeEnabled ? "trade-enabled" : "connected read-only" : "connection-error"}`} aria-live="polite"><div className="metric-card-label"><span>Binance 账户</span><span className="account-state">{loading ? "检查中" : !configured ? "未配置" : connected ? tradeEnabled ? "交易可用" : "只读连接" : "连接异常"}</span></div><strong>{money(numberValue(account.wallet_balance ?? account.total_wallet_balance), stringValue(account.currency, "USD"))}</strong><small>{!configured ? "配置 API 凭据后显示账户余额" : connected ? `可用 ${money(numberValue(account.available_balance), stringValue(account.currency, "USD"))}` : stringValue(account.error_category, "暂时无法连接 Binance")}</small>{!configured ? <button className="account-card-action" type="button" onClick={() => { window.location.hash = "#/settings"; }}>配置 API 凭据</button> : null}</article>
      <article className="metric-card"><span>Binance 凭据</span><strong>{configured ? "已配置" : "尚未配置"}</strong><small>只显示状态，不回显密钥</small></article>
    </div>

    <section className="performance-dashboard" aria-labelledby="performance-title" aria-busy={loading}>
      <div className="performance-heading"><div><span className="eyebrow">PERFORMANCE</span><h2 id="performance-title">收益概览</h2><p>并列查看系统虚拟盘累计表现与当前用户 Binance 月度实盘，统计口径独立标注</p></div><div className="performance-heading-actions"><div className="calendar-controls" aria-label="收益月份选择"><button type="button" aria-label="查看上一个月" onClick={() => shiftMonth(-1)}>‹</button><strong>{viewMonth.getFullYear()} 年 {viewMonth.getMonth() + 1} 月</strong><button type="button" aria-label="查看下一个月" disabled={!canNext} onClick={() => shiftMonth(1)}>›</button><button className="today-button" type="button" disabled={monthKey(viewMonth) === currentMonth} onClick={() => setViewMonth(new Date(new Date().getFullYear(), new Date().getMonth(), 1))}>今天</button></div><button type="button" onClick={() => void load()}>刷新</button></div></div>
      <div className="performance-columns">
        <section className="performance-side virtual-performance" aria-busy={loading}>
          <header className="performance-side-heading"><div><span className="side-kicker">MY PAPER ACCOUNT</span><h3>模拟盘收益</h3><p>当前用户模拟盘的独立策略测试结果</p></div><span className="performance-source paper">个人模拟盘 · 独立</span></header>
          <div className="performance-metrics"><article className={`performance-metric primary-performance${loading ? " loading" : ""}`}><span>累计总收益（自重置）</span><strong className={tone(paper?.metrics.total_pnl)}>{money(paper?.metrics.total_pnl, paper?.currency)}</strong><small>{paper ? `自重置 · 收益率 ${percent(paper.metrics.total_return_pct)}` : "暂时无法读取净值"}</small></article><article className={`performance-metric${loading ? " loading" : ""}`}><span>平仓胜率</span><strong className={tone((paper?.metrics.win_rate ?? 50) - 50)}>{paper?.metrics.trades ? percent(paper.metrics.win_rate) : "--"}</strong><small>{paper?.metrics.trades ? `${paper.metrics.wins} 胜 / ${paper.metrics.losses} 负 · ${paper.metrics.trades} 笔平仓` : "暂无平仓记录"}</small></article><article className={`performance-metric${loading ? " loading" : ""}`}><span>盈亏比</span><strong className="flat">{paper?.metrics.profit_factor == null ? "--" : `${paper.metrics.profit_factor.toFixed(2)} : 1`}</strong><small>总盈利 / 总亏损</small></article><article className={`performance-metric${loading ? " loading" : ""}`}><span>最大回撤</span><strong className="loss">{paper ? percent(Math.abs(paper.metrics.max_drawdown)) : "--"}</strong><small>自重置以来完整净值峰谷</small></article></div>
          <article className="returns-calendar-card"><header className="calendar-heading"><div><h3>平仓净收益日历</h3><p>按平仓日汇总已实现净 PnL；持仓浮盈不计入</p></div></header><div className="returns-calendar" role="grid"><ReturnsCalendar rows={paperRows} currency={paper?.currency ?? "USDT"} emptyText="本月尚无平仓记录；持仓浮盈不计入此日历。" eventName="笔平仓" /></div><footer className="calendar-footer"><div className="calendar-legend"><span className="profit">盈利</span><span className="loss">亏损</span><span className="flat">无交易</span></div><div className="month-summary"><span>当月平仓净收益</span><strong className={tone(paper?.calendar.total_pnl)}>{money(paper?.calendar.total_pnl, paper?.currency)}</strong><small>{paper?.calendar.active_days ?? 0} 个平仓日</small></div></footer></article>
        </section>

        <section className="performance-side binance-performance" aria-busy={loading}>
          <header className="performance-side-heading"><div><span className="side-kicker live">LIVE ACCOUNT</span><h3>Binance 实盘收益</h3><p>仅展示当前登录用户已授权账户的真实收益流水</p></div><div className="performance-side-tools">{(binance?.assets.length ?? 0) > 1 ? <label className="performance-asset-picker"><span>结算资产</span><select aria-label="选择 Binance 收益结算资产" value={asset?.asset ?? ""} onChange={(event) => setSelectedAssetCode(event.target.value)}>{binance?.assets.map((item) => <option value={item.asset} key={item.asset}>{item.asset}</option>)}</select></label> : null}<span className="performance-source live">{!binance?.configured ? "未配置" : binance.connected ? `${asset?.asset ?? "实盘"} · 已连接` : "连接异常"}</span></div></header>
          <div className="performance-metrics"><article className={`performance-metric live-primary${loading ? " loading" : ""}`}><span>当月已结算净收益</span><strong className={tone(asset?.net_income)}>{money(asset?.net_income, asset?.asset)}</strong><small>{asset ? `资金费 ${money(asset.funding_fee, asset.asset)} · 手续费 ${money(asset.commission, asset.asset)}` : "尚未连接实盘账户"}</small></article><article className={`performance-metric${loading ? " loading" : ""}`}><span>已实现盈亏</span><strong className={tone(asset?.realized_pnl)}>{money(asset?.realized_pnl, asset?.asset)}</strong><small>{asset?.current_unrealized_pnl == null ? "来自 Binance 收益流水" : `未实现 ${money(asset.current_unrealized_pnl, asset.asset)}`}</small></article><article className={`performance-metric${loading ? " loading" : ""}`}><span>已实现记录胜率</span><strong className={tone((asset?.win_rate_pct ?? 50) - 50)}>{asset?.realized_records ? percent(asset.win_rate_pct) : "--"}</strong><small>{asset?.realized_records ? `${asset.wins} 胜 / ${asset.losses} 负 · ${asset.realized_records} 条已实现记录` : "暂无实盘收益记录"}</small></article><article className={`performance-metric${loading ? " loading" : ""}`}><span>已实现记录盈亏比</span><strong className="flat">{asset?.profit_factor == null ? "--" : `${asset.profit_factor.toFixed(2)} : 1`}</strong><small>盈利记录 / 亏损记录</small></article></div>
          {!binance?.configured ? <div className="binance-performance-callout warning" role="status"><div><strong>尚未配置 Binance API</strong><p>配置当前用户的 Binance API 凭据后，才能读取真实收益流水。</p></div><button type="button" onClick={() => { window.location.hash = "#/settings"; }}>配置 API 凭据</button></div> : null}
          <article className="returns-calendar-card"><header className="calendar-heading"><div><h3>Binance 收益日历</h3><p>{asset ? `仅汇总 ${asset.asset}，不跨资产换算` : "按浏览器本地时区汇总账户收益事件"}</p></div></header><div className="returns-calendar" role="grid"><ReturnsCalendar rows={binanceRows} currency={asset?.asset ?? "USDT"} emptyText={binance?.configured ? "本月暂无 Binance 收益流水。" : "实盘未配置；虚拟盘数据不会用于填充此区域。"} eventName="条记录" /></div><footer className="calendar-footer"><div className="calendar-legend"><span className="profit">盈利</span><span className="loss">亏损</span><span className="flat">无流水</span></div><div className="month-summary"><span>当月已结算净收益</span><strong className={tone(asset?.net_income)}>{money(asset?.net_income, asset?.asset)}</strong><small>{binanceRows.length} 个收益日</small></div></footer></article>
        </section>
      </div>
    </section>
  </>;
}
