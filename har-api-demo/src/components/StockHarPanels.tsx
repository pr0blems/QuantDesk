import { useMemo, useState } from "react";
import {
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  Line,
  LineChart,
  Pie,
  PieChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import type { StockSnapshot } from "../hooks/useStockSnapshot";

function compact(value: number) {
  const absolute = Math.abs(value);
  if (absolute >= 100000000) return `${(value / 100000000).toFixed(2)}亿`;
  if (absolute >= 10000) return `${(value / 10000).toFixed(2)}万`;
  return Math.round(value).toLocaleString("zh-CN");
}

function fixed(value: number | undefined, digits = 2) {
  return typeof value === "number" ? value.toLocaleString("en-US", { minimumFractionDigits: digits, maximumFractionDigits: digits }) : "--";
}

function percent(value: number | undefined) {
  return typeof value === "number" ? fixed(value * 100) : "--";
}

function sourceLabel(source: StockSnapshot["source"]) {
  return source === "live" ? "LIVE" : source === "mixed" ? "实时 + HAR" : source === "partial" ? "部分实时" : "HAR 快照";
}

function FlowTooltip({ active, payload, label }: { active?: boolean; payload?: Array<{ value?: number; name?: string; color?: string }>; label?: string }) {
  if (!active || !payload?.length) return null;
  return <div className="stock-flow-tooltip"><strong>{label ?? payload[0].name}</strong>{payload.map((item) => <span key={item.name} style={{ color: item.color }}>{item.name}：{compact(Number(item.value ?? 0))}</span>)}</div>;
}

function StockFacts({ snapshot }: { snapshot: StockSnapshot }) {
  const quote = snapshot.quote;
  const hour = quote.hourTrading;
  const cards = [
    ["盘前", fixed(hour?.latestPrice, 4), `${fixed(hour?.change, 4)} · ${percent(hour?.changeRate)}%`],
    ["今开 / 昨收", fixed(quote.open, 4), fixed(quote.preClose, 4)],
    ["最高 / 最低", fixed(quote.high, 4), fixed(quote.low, 4)],
    ["成交量", compact(quote.volume ?? 0), `量比 ${fixed(quote.volumeRatio)}`],
    ["成交额", compact(quote.amount ?? 0), `振幅 ${percent(quote.amplitude)}%`],
    ["总股本", compact(quote.shares ?? 0), `流通 ${compact(quote.floatShares ?? 0)}`],
    ["每股收益", fixed(quote.eps), `TTM ${fixed(quote.ttmEps)}`],
  ];
  return <section className="stock-facts-panel" aria-label={`${snapshot.symbol} 行情详情`}>
    <header><div><span className="market-eyebrow">{snapshot.symbol} · STOCK DETAIL</span><h2>{snapshot.name} 个股数据接口</h2></div><div><span className={`stock-source ${snapshot.source}`}>{sourceLabel(snapshot.source)}</span><code>/stock_info/detail</code></div></header>
    <div className="stock-fact-grid">{cards.map(([label, value, note]) => <article key={label}><span>{label}</span><strong>{value}</strong><small>{note}</small></article>)}</div>
  </section>;
}

function FundFlowPanel({ snapshot }: { snapshot: StockSnapshot }) {
  const [tab, setTab] = useState<"today" | "trend" | "position">("today");
  const publicity = snapshot.publicityFund;
  const flowItems = useMemo(() => (publicity?.cashFlowList ?? []).map((item, index) => ({
    ...item,
    fill: item.id.toLowerCase().includes("inflow") ? ["#087f61", "#10b981", "#31d6a3"][index % 3] : ["#b91c35", "#ef4458", "#ff7584"][index % 3],
  })), [publicity]);
  const trend = useMemo(() => snapshot.fundTrend.map((item) => ({ ...item, label: new Date(item.time).toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit", hour12: false }) })), [snapshot.fundTrend]);
  const positions = useMemo(() => snapshot.positionChange.map((item) => ({ ...item, value: Number(item.change) || 0 })), [snapshot.positionChange]);
  const stat = publicity?.cashFlowStat;
  return <section className="stock-api-panel stock-fund-panel" aria-label={`${snapshot.symbol} 资金分析`}>
    <header><div><h2>资金分析</h2><code>/fund_related/{snapshot.symbol}</code></div><span>{sourceLabel(snapshot.source)}</span></header>
    <div className="stock-panel-tabs" role="tablist" aria-label="资金分析类型">
      {[{ id: "today", label: "今日资金" }, { id: "trend", label: "资金流向" }, { id: "position", label: "5日大单" }].map((item) => <button className={tab === item.id ? "active" : ""} onClick={() => setTab(item.id as typeof tab)} role="tab" aria-selected={tab === item.id} key={item.id} type="button">{item.label}</button>)}
    </div>
    {tab === "today" ? <div className="stock-fund-today">
      <div className="stock-donut-wrap"><ResponsiveContainer width="100%" height={250}><PieChart><Pie data={flowItems} dataKey="amount" nameKey="name" innerRadius={64} outerRadius={94} paddingAngle={2} stroke="#10171b" strokeWidth={2}>{flowItems.map((item) => <Cell key={item.id} fill={item.fill}/>)}</Pie><Tooltip content={<FlowTooltip/>}/></PieChart></ResponsiveContainer><div><strong>今日资金</strong><span className={(stat?.netflow ?? 0) >= 0 ? "positive" : "negative"}>{compact(stat?.netflow ?? 0)}</span></div></div>
      <div className="stock-flow-summary"><article><span>流入</span><strong className="positive">{compact(stat?.inflow ?? 0)}</strong></article><article><span>流出</span><strong className="negative">{compact(stat?.outflow ?? 0)}</strong></article>{flowItems.map((item) => <p key={item.id}><i style={{ background: item.fill }}/><span>{item.name}</span><strong>{item.count}</strong></p>)}</div>
    </div> : tab === "trend" ? <div className="stock-chart-area"><ResponsiveContainer width="100%" height={285}><LineChart data={trend} margin={{ top: 12, right: 8, bottom: 2, left: 4 }}><CartesianGrid stroke="#263238" strokeDasharray="3 5" vertical={false}/><XAxis dataKey="label" tick={{ fill: "#7b898f", fontSize: 9 }} minTickGap={55} tickLine={false}/><YAxis tick={{ fill: "#7b898f", fontSize: 9 }} tickFormatter={compact} width={62} tickLine={false}/><Tooltip content={<FlowTooltip/>}/><Line dataKey="amount" name="净流入" type="monotone" stroke="#32c99a" dot={false} strokeWidth={2} isAnimationActive={false}/></LineChart></ResponsiveContainer></div> : <div className="stock-chart-area"><ResponsiveContainer width="100%" height={285}><BarChart data={positions} margin={{ top: 12, right: 8, bottom: 2, left: 4 }}><CartesianGrid stroke="#263238" strokeDasharray="3 5" vertical={false}/><XAxis dataKey="date" tick={{ fill: "#7b898f", fontSize: 10 }} tickLine={false}/><YAxis tick={{ fill: "#7b898f", fontSize: 9 }} unit="万" width={54} tickLine={false}/><Tooltip content={<FlowTooltip/>}/><Bar dataKey="value" name="大单净变化" radius={[2, 2, 0, 0]}>{positions.map((item) => <Cell key={item.date} fill={item.value >= 0 ? "#23c795" : "#ef5d68"}/>)}</Bar></BarChart></ResponsiveContainer></div>}
    <footer>{stat && stat.netflow < 0 ? "资金净流出，短线追涨需要成交量与盘口继续确认。" : "资金净流入，关注价格能否站稳平均筹码成本。"}</footer>
  </section>;
}

function ChipsPanel({ snapshot, lastCheckedAt, unchangedCount }: { snapshot: StockSnapshot; lastCheckedAt: number | null; unchangedCount: number }) {
  const [tab, setTab] = useState<"chips" | "prices">("chips");
  const chips = snapshot.chipsDistribution;
  const chipData = useMemo(() => {
    const source = chips?.cumPdfList ?? [];
    const stride = Math.max(1, Math.ceil(source.length / 32));
    return source.filter((_, index) => index % stride === 0 || index === source.length - 1).map((item) => ({ ...item, label: item.price.toFixed(2) }));
  }, [chips]);
  const priceData = useMemo(() => {
    const source = snapshot.priceDistribution;
    const stride = Math.max(1, Math.ceil(source.length / 32));
    return source.filter((_, index) => index % stride === 0 || index === source.length - 1).map((item) => ({ ...item, label: item.price.toFixed(4) }));
  }, [snapshot.priceDistribution]);
  const current = snapshot.quote.hourTrading?.latestPrice ?? snapshot.quote.latestPrice ?? 0;
  const checkedTime = lastCheckedAt ? new Date(lastCheckedAt).toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false }) : "--:--:--";
  return <section className="stock-api-panel stock-chip-panel" aria-label={`${snapshot.symbol} 筹码与成交价分布`}>
    <header><div><h2>筹码分布</h2><code>/trade_price_list · /fund_related</code></div><div className="stock-chip-refresh"><span>{chips?.date ?? "--"} · 日级快照</span><small title="每2秒重新请求上游；只有筹码内容变化时柱形图才会更新"><i/> 每2秒检查 · {checkedTime}{unchangedCount > 0 ? ` · 未变化 ${unchangedCount} 次` : " · 已同步"}</small></div></header>
    <div className="stock-panel-tabs" role="tablist" aria-label="分布图类型"><button className={tab === "chips" ? "active" : ""} onClick={() => setTab("chips")} role="tab" aria-selected={tab === "chips"} type="button">筹码分布</button><button className={tab === "prices" ? "active" : ""} onClick={() => setTab("prices")} role="tab" aria-selected={tab === "prices"} type="button">成交价分布</button></div>
    <div className="stock-chip-metrics"><span>支撑 <b className="positive">{fixed(chips?.support)}</b></span><span>平均成本 <b>{fixed(chips?.avgPrice)}</b></span><span>压力 <b className="negative">{fixed(chips?.pressure)}</b></span></div>
    <div className="stock-chart-area"><ResponsiveContainer width="100%" height={285}>{tab === "chips" ? <BarChart data={chipData} layout="vertical" margin={{ top: 5, right: 10, bottom: 4, left: 5 }}><CartesianGrid stroke="#263238" strokeDasharray="3 5" horizontal={false}/><XAxis type="number" tick={{ fill: "#7b898f", fontSize: 9 }} tickFormatter={compact} tickLine={false}/><YAxis type="category" dataKey="label" tick={{ fill: "#7b898f", fontSize: 9 }} width={46} interval={3} tickLine={false}/><Tooltip content={<FlowTooltip/>}/><Bar dataKey="lot" name="筹码量" radius={[0, 2, 2, 0]}>{chipData.map((item) => <Cell key={item.label} fill={item.price >= current ? "#ef5f68" : "#29c899"}/>)}</Bar></BarChart> : <BarChart data={priceData} layout="vertical" margin={{ top: 5, right: 10, bottom: 4, left: 5 }}><CartesianGrid stroke="#263238" strokeDasharray="3 5" horizontal={false}/><XAxis type="number" tick={{ fill: "#7b898f", fontSize: 9 }} tickFormatter={compact} tickLine={false}/><YAxis type="category" dataKey="label" tick={{ fill: "#7b898f", fontSize: 9 }} width={52} interval={3} tickLine={false}/><Tooltip content={<FlowTooltip/>}/><Bar dataKey="buy" name="主动买" stackId="price" fill="#28c899"/><Bar dataKey="neutral" name="中性" stackId="price" fill="#718087"/><Bar dataKey="sell" name="主动卖" stackId="price" fill="#ef5f68"/></BarChart>}</ResponsiveContainer></div>
    <footer>{chips && current > chips.avgPrice ? "现价高于平均筹码成本，获利盘占优；注意压力位附近抛压。" : "现价未站稳平均筹码成本，反弹持续性仍需资金净流入确认。"}</footer>
  </section>;
}

export function StockHarPanels({ symbol, name, snapshot, status, error, lastCheckedAt, unchangedCount }: { symbol: string; name: string; snapshot: StockSnapshot | null; status: string; error: string | null; lastCheckedAt: number | null; unchangedCount: number }) {
  if (!snapshot) return <section className="stock-facts-panel stock-loading"><strong>{status === "error" ? `${symbol} 接口加载失败` : `正在加载 ${symbol} 个股接口…`}</strong><span>{error ?? `读取 ${name} 的行情、深度、逐笔、资金和筹码数据`}</span></section>;
  return <><StockFacts snapshot={snapshot}/><div className="stock-analytics-grid"><FundFlowPanel snapshot={snapshot}/><ChipsPanel snapshot={snapshot} lastCheckedAt={lastCheckedAt} unchangedCount={unchangedCount}/></div></>;
}
