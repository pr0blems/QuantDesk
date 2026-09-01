import { useEffect, useMemo, useRef, useState } from "react";
import type { CSSProperties } from "react";
import type { CommunitySnapshot, DepthLevel, NewsItem, NewsSnapshot, OrderBookSnapshot } from "../types";
import { MarketDataBoard } from "./MarketDataBoard";

type Stock = {
  symbol: string;
  name: string;
  exchange: string;
  price: number;
  change: number;
  changeRate: number;
  afterHours: number;
  afterRate: number;
  open: number;
  high: number;
  low: number;
  volume: string;
  volumeValue: number;
  color: string;
  series: number[];
};

const stocks: Stock[] = [
  {
    symbol: "PYPL", name: "PayPal Holdings", exchange: "NASDAQ", price: 53.66, change: -7.81, changeRate: -12.71,
    afterHours: 53.73, afterRate: 0.13, open: 53.74, high: 54.76, low: 52.62, volume: "3634万", volumeValue: 36340502, color: "#168bd2",
    series: [61.1,60.7,60.9,60.1,59.8,59.6,58.9,58.5,58.0,58.2,57.8,58.0,57.5,57.7,57.4,57.8,57.3,57.5,57.2,56.9,56.8,56.5,56.7,56.2,55.8,55.7,55.9,55.5,55.1,55.2,54.7,54.4,54.6,54.1,53.8,54.0,53.5,53.1,53.5,53.9,54.2,54.0,54.5,54.1,53.9,54.1,53.8,54.0,54.2,54.0,53.8,53.9,53.7,53.6,53.4,53.5,53.3,53.6,53.8,53.6,53.5,53.4,53.6,53.5,53.4,53.66],
  },
  {
    symbol: ".IXIC", name: "纳斯达克", exchange: "INDEX", price: 26402.42, change: -138.93, changeRate: -0.52,
    afterHours: 26402.42, afterRate: 0, open: 26515.99, high: 26700.68, low: 26359.27, volume: "75.3亿", volumeValue: 7530000000, color: "#5147c8",
    series: [26541,26580,26610,26585,26630,26675,26640,26610,26590,26620,26570,26530,26555,26510,26490,26540,26505,26470,26495,26455,26480,26460,26420,26455,26410,26390,26425,26402],
  },
  {
    symbol: "NVDA", name: "英伟达", exchange: "NASDAQ", price: 1034.64, change: -14.49, changeRate: -1.38,
    afterHours: 1037.2, afterRate: 0.25, open: 1048.2, high: 1056.8, low: 1026.1, volume: "4218万", volumeValue: 42180000, color: "#78b900",
    series: [1049,1052,1048,1055,1051,1046,1042,1045,1040,1038,1041,1036,1039,1034,1037,1033,1035,1032,1036,1034.64],
  },
  {
    symbol: "AAPL", name: "苹果", exchange: "NASDAQ", price: 194.25, change: -1.82, changeRate: -0.93,
    afterHours: 194.34, afterRate: 0.05, open: 195.8, high: 196.2, low: 193.9, volume: "5280万", volumeValue: 52800000, color: "#e9edf0",
    series: [196.1,195.8,195.9,195.6,195.3,195.5,195.1,194.9,195.0,194.7,194.8,194.5,194.7,194.4,194.6,194.3,194.5,194.25],
  },
];

const periods = ["1D", "5D", "1M", "6M", "1Y"];

function Icon({ name }: { name: "search" | "star" | "more" | "chevron" | "bookmark" | "code" | "expand" }) {
  const common = { fill: "none", stroke: "currentColor", strokeWidth: 1.8, strokeLinecap: "round" as const, strokeLinejoin: "round" as const };
  if (name === "search") return <svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="11" cy="11" r="7" {...common}/><path d="m20 20-4-4" {...common}/></svg>;
  if (name === "star") return <svg viewBox="0 0 24 24" aria-hidden="true"><path d="m12 3 2.7 5.5 6.1.9-4.4 4.3 1 6.1-5.4-2.9-5.4 2.9 1-6.1-4.4-4.3 6.1-.9L12 3Z" {...common}/></svg>;
  if (name === "more") return <svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="5" r="1" fill="currentColor"/><circle cx="12" cy="12" r="1" fill="currentColor"/><circle cx="12" cy="19" r="1" fill="currentColor"/></svg>;
  if (name === "chevron") return <svg viewBox="0 0 24 24" aria-hidden="true"><path d="m7 14 5-5 5 5" {...common}/></svg>;
  if (name === "bookmark") return <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M6 4h12v17l-6-4-6 4V4Z" {...common}/></svg>;
  if (name === "code") return <svg viewBox="0 0 24 24" aria-hidden="true"><path d="m8 8-4 4 4 4M16 8l4 4-4 4M14 5l-4 14" {...common}/></svg>;
  return <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M8 3H3v5M16 3h5v5M8 21H3v-5M16 21h5v-5" {...common}/></svg>;
}

function MiniLogo({ stock }: { stock: Stock }) {
  return <span className="pulse-stock-logo" style={{ "--stock-color": stock.color } as CSSProperties}>{stock.symbol === ".IXIC" ? ".IX" : stock.symbol.slice(0, 1)}</span>;
}

function Watchlist({ selected, onSelect }: { selected: Stock; onSelect: (stock: Stock) => void }) {
  return <aside className="pulse-watchlist">
    <div className="pulse-section-title"><h2>自选</h2><div><button aria-label="添加自选" type="button">＋</button><button aria-label="更多自选操作" type="button"><Icon name="more" /></button></div></div>
    <div className="pulse-watch-rows">
      {stocks.map((stock) => <button className={selected.symbol === stock.symbol ? "active" : ""} onClick={() => onSelect(stock)} key={stock.symbol} type="button">
        <MiniLogo stock={stock}/><span><strong>{stock.symbol}</strong><small>{stock.name}</small></span><span className="pulse-watch-price"><strong>{stock.price.toLocaleString("en-US", { maximumFractionDigits: 2 })}</strong><small className={stock.changeRate >= 0 ? "positive" : "negative"}>{stock.changeRate > 0 ? "+" : ""}{stock.changeRate.toFixed(2)}%</small></span>
      </button>)}
    </div>
    <button className="pulse-manage" type="button"><span>▣</span> 管理自选</button>
  </aside>;
}

type ChartDatum = {
  price: number;
  volume: number;
  label: string;
  shortLabel: string;
  change: number;
  changeRate: number;
};

const periodPointCount: Record<string, number> = { "1D": 66, "5D": 5, "1M": 22, "6M": 126, "1Y": 252 };

function interpolateSeries(series: number[], count: number) {
  if (count <= 1 || series.length <= 1) return [series[0] ?? 0];
  return Array.from({ length: count }, (_, index) => {
    const position = index * (series.length - 1) / (count - 1);
    const start = Math.floor(position);
    const end = Math.min(start + 1, series.length - 1);
    const ratio = position - start;
    return series[start] + (series[end] - series[start]) * ratio;
  });
}

function getBusinessDates(count: number) {
  const result: Date[] = [];
  const cursor = new Date(Date.UTC(2026, 7, 31));
  while (result.length < count) {
    const day = cursor.getUTCDay();
    if (day !== 0 && day !== 6) result.unshift(new Date(cursor));
    cursor.setUTCDate(cursor.getUTCDate() - 1);
  }
  return result;
}

function compactVolume(value: number) {
  if (value >= 100000000) return (value / 100000000).toFixed(2) + "亿";
  if (value >= 10000) return (value / 10000).toFixed(2) + "万";
  return Math.round(value).toLocaleString("zh-CN");
}

function buildChartData(stock: Stock, period: string): ChartDatum[] {
  const count = periodPointCount[period] ?? 66;
  const prices = interpolateSeries(stock.series, count);
  const seed = stock.symbol.split("").reduce((total, character) => total + character.charCodeAt(0), 0);
  const dates = period === "1D" ? [] : getBusinessDates(count);
  const rawVolumes = prices.map((_, index) => 0.58 + ((index * 29 + index * index * 7 + seed) % 73) / 100);
  const intradayScale = stock.volumeValue / Math.max(rawVolumes.reduce((total, value) => total + value, 0), 1);

  return prices.map((price, index) => {
    const previous = index === 0 ? stock.open : prices[index - 1];
    const change = price - previous;
    const changeRate = previous === 0 ? 0 : change / previous * 100;
    if (period === "1D") {
      const minutes = Math.round(index * 390 / Math.max(count - 1, 1));
      const hour = 9 + Math.floor((30 + minutes) / 60);
      const minute = (30 + minutes) % 60;
      const time = String(hour).padStart(2, "0") + ":" + String(minute).padStart(2, "0");
      return { price, change, changeRate, volume: Math.round(rawVolumes[index] * intradayScale), label: "2026-08-31 " + time, shortLabel: time };
    }
    const date = dates[index];
    const fullDate = date.toISOString().slice(0, 10);
    return { price, change, changeRate, volume: Math.round(stock.volumeValue * rawVolumes[index]), label: fullDate, shortLabel: fullDate.slice(5) };
  });
}

function LegacyPriceChart({ stock, period, onPeriod }: { stock: Stock; period: string; onPeriod: (period: string) => void }) {
  const geometry = useMemo(() => {
    const width = 1000; const top = 18; const bottom = 258; const left = 40; const right = 980;
    const min = Math.min(...stock.series); const max = Math.max(...stock.series); const range = Math.max(max - min, 1);
    const points = stock.series.map((value, index) => {
      const x = left + index * ((right - left) / Math.max(stock.series.length - 1, 1));
      const y = top + ((max - value) / range) * (bottom - top);
      return { x, y };
    });
    const line = points.map((point, index) => `${index ? "L" : "M"}${point.x.toFixed(1)},${point.y.toFixed(1)}`).join(" ");
    const area = `${line} L${right},${bottom} L${left},${bottom} Z`;
    return { line, area, min, max };
  }, [stock]);
  const volumeBars = Array.from({ length: 58 }, (_, index) => 10 + ((index * 17 + index * index * 3) % 42));
  return <section className="pulse-chart-panel">
    <div className="pulse-price-head">
      <div><h1>{stock.name} <span>{stock.symbol} · {stock.exchange}</span></h1><div className="pulse-price-row"><strong>${stock.price.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}</strong><b className={stock.change >= 0 ? "positive" : "negative"}>{stock.change > 0 ? "+" : ""}{stock.change.toFixed(2)}&nbsp;&nbsp;{stock.changeRate > 0 ? "+" : ""}{stock.changeRate.toFixed(2)}%</b><span>已收盘</span><em>盘后</em><strong className="after-price">{stock.afterHours.toFixed(2)}</strong><b className="positive">{stock.afterRate > 0 ? "+" : ""}{stock.afterRate.toFixed(2)}%</b></div></div>
      <div className="pulse-head-actions"><button aria-label="添加收藏" type="button"><Icon name="star" /></button><button aria-label="更多操作" type="button"><Icon name="more" /></button></div>
    </div>
    <div className="pulse-chart-toolbar"><div>{periods.map((item) => <button className={period === item ? "active" : ""} onClick={() => onPeriod(item)} key={item} type="button">{item}</button>)}</div><dl><div><dt>开</dt><dd>{stock.open.toFixed(2)}</dd></div><div><dt>高</dt><dd className="negative">{stock.high.toFixed(2)}</dd></div><div><dt>低</dt><dd className="positive">{stock.low.toFixed(2)}</dd></div><div><dt>量</dt><dd>{stock.volume}</dd></div></dl></div>
    <div className="pulse-chart-wrap" aria-label={`${stock.symbol} ${period} 分时走势`}>
      <svg viewBox="0 0 1000 330" role="img">
        <defs><linearGradient id="pulseArea" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stopColor="#ff5b57" stopOpacity=".23"/><stop offset="1" stopColor="#ff5b57" stopOpacity="0"/></linearGradient></defs>
        {[40,95,150,205,260].map((y) => <line x1="40" x2="980" y1={y} y2={y} key={y} className="pulse-grid-line"/>)}
        {[40,228,416,604,792,980].map((x) => <line x1={x} x2={x} y1="18" y2="300" key={x} className="pulse-grid-line"/>)}
        <path d={geometry.area} fill="url(#pulseArea)"/><path d={geometry.line} className="pulse-price-path"/>
        <line x1="40" x2="980" y1="70" y2="70" className="pulse-reference-line"/>
        {volumeBars.map((height, index) => <rect key={index} x={43 + index * 16.1} y={306 - height} width="4" height={height} className={index % 4 === 0 ? "pulse-vol-up" : "pulse-vol-down"}/>)}
        <g className="pulse-axis-labels"><text x="4" y="24">{geometry.max.toFixed(2)}</text><text x="4" y="146">{((geometry.max + geometry.min) / 2).toFixed(2)}</text><text x="4" y="260">{geometry.min.toFixed(2)}</text><text x="40" y="326">09:30</text><text x="225" y="326">10:30</text><text x="412" y="326">11:30</text><text x="594" y="326">13:30</text><text x="786" y="326">14:30</text><text x="942" y="326">16:00</text></g>
      </svg>
    </div>
  </section>;
}

function PriceChart({ stock, period, onPeriod }: { stock: Stock; period: string; onPeriod: (period: string) => void }) {
  const [hoveredIndex, setHoveredIndex] = useState<number | null>(null);
  const chartData = useMemo(() => buildChartData(stock, period), [stock, period]);
  const geometry = useMemo(() => {
    const top = 18; const bottom = 258; const left = 40; const right = 980;
    const values = chartData.map((datum) => datum.price);
    const min = Math.min(...values); const max = Math.max(...values); const range = Math.max(max - min, 1);
    const points = chartData.map((datum, index) => ({
      x: left + index * ((right - left) / Math.max(chartData.length - 1, 1)),
      y: top + ((max - datum.price) / range) * (bottom - top),
    }));
    const line = points.map((point, index) => (index ? "L" : "M") + point.x.toFixed(1) + "," + point.y.toFixed(1)).join(" ");
    const area = line + " L" + right + "," + bottom + " L" + left + "," + bottom + " Z";
    const maxVolume = Math.max(...chartData.map((datum) => datum.volume), 1);
    const referenceY = top + ((max - stock.open) / range) * (bottom - top);
    return { line, area, min, max, points, maxVolume, referenceY: Math.max(top, Math.min(bottom, referenceY)) };
  }, [chartData, stock.open]);
  const tickIndexes = useMemo(() => Array.from(new Set([0, .2, .4, .6, .8, 1].map((ratio) => Math.round((chartData.length - 1) * ratio)))), [chartData.length]);
  const activeIndex = hoveredIndex === null ? null : Math.max(0, Math.min(chartData.length - 1, hoveredIndex));
  const active = activeIndex === null ? null : { datum: chartData[activeIndex], point: geometry.points[activeIndex] };
  const barWidth = Math.max(1.5, Math.min(5, 760 / chartData.length));

  const movePointer = (event: React.PointerEvent<SVGSVGElement>) => {
    const bounds = event.currentTarget.getBoundingClientRect();
    const viewX = (event.clientX - bounds.left) / Math.max(bounds.width, 1) * 1000;
    const ratio = (Math.max(40, Math.min(980, viewX)) - 40) / 940;
    setHoveredIndex(Math.round(ratio * (chartData.length - 1)));
  };

  return <section className="pulse-chart-panel">
    <div className="pulse-price-head">
      <div><h1>{stock.name} <span>{stock.symbol} · {stock.exchange}</span></h1><div className="pulse-price-row"><strong>${stock.price.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}</strong><b className={stock.change >= 0 ? "positive" : "negative"}>{stock.change > 0 ? "+" : ""}{stock.change.toFixed(2)}&nbsp;&nbsp;{stock.changeRate > 0 ? "+" : ""}{stock.changeRate.toFixed(2)}%</b><span>已收盘</span><em>盘后</em><strong className="after-price">{stock.afterHours.toFixed(2)}</strong><b className="positive">{stock.afterRate > 0 ? "+" : ""}{stock.afterRate.toFixed(2)}%</b></div></div>
      <div className="pulse-head-actions"><button aria-label="添加收藏" type="button"><Icon name="star" /></button><button aria-label="更多操作" type="button"><Icon name="more" /></button></div>
    </div>
    <div className="pulse-chart-toolbar"><div>{periods.map((item) => <button className={period === item ? "active" : ""} onClick={() => { onPeriod(item); setHoveredIndex(null); }} key={item} type="button">{item}</button>)}</div><dl><div><dt>开</dt><dd>{stock.open.toFixed(2)}</dd></div><div><dt>高</dt><dd className="negative">{stock.high.toFixed(2)}</dd></div><div><dt>低</dt><dd className="positive">{stock.low.toFixed(2)}</dd></div><div><dt>量</dt><dd>{stock.volume}</dd></div></dl></div>
    <div className="pulse-chart-wrap">
      <svg viewBox="0 0 1000 330" role="img" tabIndex={0} aria-label={stock.symbol + " " + period + " 走势图，可用鼠标或左右方向键查看逐点价格和成交量"} onPointerMove={movePointer} onPointerLeave={() => setHoveredIndex(null)} onFocus={() => setHoveredIndex((current) => current ?? chartData.length - 1)} onBlur={() => setHoveredIndex(null)} onKeyDown={(event) => { if (event.key === "ArrowLeft" || event.key === "ArrowRight") { event.preventDefault(); setHoveredIndex((current) => Math.max(0, Math.min(chartData.length - 1, (current ?? chartData.length - 1) + (event.key === "ArrowRight" ? 1 : -1)))); } }}>
        <defs><linearGradient id="pulseAreaInteractive" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stopColor="#ff5b57" stopOpacity=".23"/><stop offset="1" stopColor="#ff5b57" stopOpacity="0"/></linearGradient></defs>
        {[40,95,150,205,260].map((y) => <line x1="40" x2="980" y1={y} y2={y} key={y} className="pulse-grid-line"/>)}
        {[40,228,416,604,792,980].map((x) => <line x1={x} x2={x} y1="18" y2="306" key={x} className="pulse-grid-line"/>)}
        <path d={geometry.area} fill="url(#pulseAreaInteractive)"/><path d={geometry.line} className="pulse-price-path"/>
        <line x1="40" x2="980" y1={geometry.referenceY} y2={geometry.referenceY} className="pulse-reference-line"/>
        {chartData.map((datum, index) => { const height = Math.max(3, datum.volume / geometry.maxVolume * 48); const point = geometry.points[index]; return <rect key={index} x={point.x - barWidth / 2} y={306 - height} width={barWidth} height={height} className={(datum.change >= 0 ? "pulse-vol-up" : "pulse-vol-down") + (activeIndex === index ? " pulse-volume-highlight" : "")}/>; })}
        {active ? <g aria-hidden="true"><line x1={active.point.x} x2={active.point.x} y1="18" y2="306" className="pulse-hover-line"/><line x1="40" x2="980" y1={active.point.y} y2={active.point.y} className="pulse-hover-line horizontal"/><circle cx={active.point.x} cy={active.point.y} r="5" className="pulse-hover-dot"/></g> : null}
        <rect x="40" y="18" width="940" height="288" fill="transparent"/>
        <g className="pulse-axis-labels"><text x="4" y="24">{geometry.max.toFixed(2)}</text><text x="4" y="146">{((geometry.max + geometry.min) / 2).toFixed(2)}</text><text x="4" y="260">{geometry.min.toFixed(2)}</text>{tickIndexes.map((index, tick) => <text key={index} x={geometry.points[index].x} y="326" textAnchor={tick === 0 ? "start" : tick === tickIndexes.length - 1 ? "end" : "middle"}>{chartData[index].shortLabel}</text>)}</g>
      </svg>
      {active ? <div className={"pulse-chart-tooltip" + (active.point.x > 720 ? " align-right" : "")} style={{ left: active.point.x / 10 + "%", top: active.point.y / 3.3 + "%" }} role="status" aria-live="polite">
        <header><strong>{active.datum.label}</strong><small>{period === "1D" ? "分时数据点" : "交易日数据点"} · 离线图表样例</small></header>
        <dl><div><dt>价格</dt><dd>{active.datum.price.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}</dd></div><div><dt>涨跌额</dt><dd className={active.datum.change >= 0 ? "positive" : "negative"}>{active.datum.change >= 0 ? "+" : ""}{active.datum.change.toFixed(2)}</dd></div><div><dt>涨跌幅</dt><dd className={active.datum.changeRate >= 0 ? "positive" : "negative"}>{active.datum.changeRate >= 0 ? "+" : ""}{active.datum.changeRate.toFixed(2)}%</dd></div><div><dt>成交量</dt><dd>{compactVolume(active.datum.volume)}</dd></div></dl>
      </div> : null}
    </div>
  </section>;
}

type KlinePeriod = "M5" | "M15" | "M30" | "H1" | "D1" | "W1" | "MN1";

type KlineConfig = {
  id: KlinePeriod;
  label: string;
  minutes?: number;
  count: number;
};

type CandleDatum = {
  time: Date;
  label: string;
  shortLabel: string;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
  bodyChange: number;
  bodyChangeRate: number;
  change: number;
  changeRate: number;
};

const MAX_KLINE_POINTS = 2000;
const VISIBLE_CANDLES = 72;
const klinePeriods: KlineConfig[] = [
  { id: "M5", label: "M5", minutes: 5, count: 2000 },
  { id: "M15", label: "M15", minutes: 15, count: 1600 },
  { id: "M30", label: "M30", minutes: 30, count: 1200 },
  { id: "H1", label: "H1", minutes: 60, count: 900 },
  { id: "D1", label: "日K", count: 504 },
  { id: "W1", label: "周K", count: 260 },
  { id: "MN1", label: "月K", count: 120 },
];

function previousBusinessDate(date: Date) {
  const result = new Date(date);
  do result.setUTCDate(result.getUTCDate() - 1); while (result.getUTCDay() === 0 || result.getUTCDay() === 6);
  return result;
}

function buildKlineTimes(config: KlineConfig) {
  const dates: Date[] = [];
  if (config.minutes) {
    const slots: number[] = [];
    for (let minute = 570; minute < 960; minute += config.minutes) slots.push(minute);
    let day = new Date(Date.UTC(2026, 7, 31));
    let slotIndex = slots.length - 1;
    while (dates.length < config.count) {
      const minute = slots[slotIndex];
      dates.unshift(new Date(Date.UTC(day.getUTCFullYear(), day.getUTCMonth(), day.getUTCDate(), Math.floor(minute / 60), minute % 60)));
      slotIndex -= 1;
      if (slotIndex < 0) { day = previousBusinessDate(day); slotIndex = slots.length - 1; }
    }
    return dates;
  }
  if (config.id === "D1") return getBusinessDates(config.count);
  if (config.id === "W1") {
    const cursor = new Date(Date.UTC(2026, 7, 31));
    for (let index = 0; index < config.count; index += 1) { dates.unshift(new Date(cursor)); cursor.setUTCDate(cursor.getUTCDate() - 7); }
    return dates;
  }
  const cursor = new Date(Date.UTC(2026, 7, 1));
  for (let index = 0; index < config.count; index += 1) { dates.unshift(new Date(cursor)); cursor.setUTCMonth(cursor.getUTCMonth() - 1); }
  return dates;
}

function klineDateLabel(date: Date, config: KlineConfig, short = false) {
  const fullDate = date.toISOString().slice(0, 10);
  if (config.minutes) {
    const time = String(date.getUTCHours()).padStart(2, "0") + ":" + String(date.getUTCMinutes()).padStart(2, "0");
    return short ? fullDate.slice(5) + " " + time : fullDate + " " + time;
  }
  if (config.id === "MN1") return short ? fullDate.slice(0, 7) : fullDate.slice(0, 7);
  return short ? fullDate.slice(5) : fullDate;
}

function buildCandles(stock: Stock, config: KlineConfig): CandleDatum[] {
  const times = buildKlineTimes(config);
  const seed = stock.symbol.split("").reduce((total, character) => total + character.charCodeAt(0), 0);
  const stepVolatility = ({ M5: .0016, M15: .0026, M30: .0036, H1: .0052, D1: .018, W1: .04, MN1: .085 } as Record<KlinePeriod, number>)[config.id];
  const gapVolatility = ({ M5: .0025, M15: .003, M30: .0035, H1: .004, D1: .006, W1: .012, MN1: .025 } as Record<KlinePeriod, number>)[config.id];
  const volumeScale = ({ M5: 1 / 78, M15: 1 / 26, M30: 1 / 13, H1: 1 / 7, D1: 1, W1: 5, MN1: 21 } as Record<KlinePeriod, number>)[config.id];
  const randomUnit = (index: number, salt: number) => {
    const value = Math.sin((index + 1) * 12.9898 + (seed + salt) * 78.233) * 43758.5453;
    return value - Math.floor(value);
  };
  const rawCloses: number[] = [];
  let rollingClose = stock.price * (.92 + randomUnit(-1, 1) * .16);
  for (let index = 0; index < times.length; index += 1) {
    const shock = (randomUnit(index, 1) + randomUnit(index, 2) + randomUnit(index, 3) - 1.5) * stepVolatility;
    rollingClose = Math.max(.01, rollingClose * Math.exp(shock));
    rawCloses.push(rollingClose);
  }
  const scale = stock.price / rawCloses[rawCloses.length - 1];
  const closes = rawCloses.map((value) => value * scale);
  closes[closes.length - 1] = stock.price;
  return closes.map((close, index) => {
    const previousClose = index === 0 ? close : closes[index - 1];
    const sameTradingDay = index > 0 && times[index].toISOString().slice(0, 10) === times[index - 1].toISOString().slice(0, 10);
    const continuousIntraday = Boolean(config.minutes && sameTradingDay);
    const gap = (randomUnit(index, 7) - .5) * 2 * gapVolatility;
    const open = index === 0 ? close * (1 - (randomUnit(index, 6) - .5) * stepVolatility) : continuousIntraday ? previousClose : previousClose * (1 + gap);
    const bodyChange = close - open;
    const bodyChangeRate = open === 0 ? 0 : bodyChange / open * 100;
    const rangeBase = close * stepVolatility;
    const bodyTop = Math.max(open, close);
    const bodyBottom = Math.min(open, close);
    const shadowMode = randomUnit(index, 12);
    const allowUpperShadow = (shadowMode >= .15 && shadowMode < .35) || shadowMode >= .55;
    const allowLowerShadow = shadowMode >= .35;
    const pathSteps = config.minutes ? 10 : 14;
    let pathMomentum = 0;
    let high = bodyTop;
    let low = bodyBottom;
    for (let step = 1; step < pathSteps; step += 1) {
      const progress = step / pathSteps;
      const bridgePrice = open + (close - open) * progress;
      pathMomentum = pathMomentum * .58 + (randomUnit(index, 20 + step) - .5) * 2;
      let pathPrice = bridgePrice + pathMomentum * rangeBase * .48 * Math.sin(Math.PI * progress);
      if (!allowUpperShadow) pathPrice = Math.min(pathPrice, bodyTop);
      if (!allowLowerShadow) pathPrice = Math.max(pathPrice, bodyBottom);
      high = Math.max(high, pathPrice);
      low = Math.min(low, pathPrice);
    }
    low = Math.max(.01, low);
    const activity = Math.min(2, Math.abs(close - previousClose) / Math.max(previousClose * stepVolatility, .01));
    const volume = Math.round(stock.volumeValue * volumeScale * (.5 + randomUnit(index, 11) * .55 + activity * .22));
    const change = close - previousClose;
    const changeRate = previousClose === 0 ? 0 : change / previousClose * 100;
    return { time: times[index], label: klineDateLabel(times[index], config), shortLabel: klineDateLabel(times[index], config, true), open, high, low, close, volume, bodyChange, bodyChangeRate, change, changeRate };
  });
}

function KlineChart({ stock, period, onPeriod }: { stock: Stock; period: string; onPeriod: (period: KlinePeriod) => void }) {
  const config = klinePeriods.find((item) => item.id === period) ?? klinePeriods[0];
  const chartData = useMemo(() => buildCandles(stock, config), [stock, config]);
  const [windowEnd, setWindowEnd] = useState(chartData.length);
  const [hoveredIndex, setHoveredIndex] = useState<number | null>(null);
  const [dragging, setDragging] = useState(false);
  const dragRef = useRef<{ startX: number; startEnd: number; pointerId: number } | null>(null);

  useEffect(() => { setWindowEnd(chartData.length); setHoveredIndex(null); }, [chartData]);

  const safeEnd = Math.max(Math.min(VISIBLE_CANDLES, chartData.length), Math.min(chartData.length, windowEnd));
  const windowStart = Math.max(0, safeEnd - VISIBLE_CANDLES);
  const visibleData = chartData.slice(windowStart, safeEnd);
  const geometry = useMemo(() => {
    const top = 18; const bottom = 238; const volumeTop = 258; const volumeBottom = 306; const left = 48; const right = 980;
    const min = Math.min(...visibleData.map((datum) => datum.low));
    const max = Math.max(...visibleData.map((datum) => datum.high));
    const range = Math.max(max - min, .01);
    const maxVolume = Math.max(...visibleData.map((datum) => datum.volume), 1);
    const slot = (right - left) / Math.max(visibleData.length, 1);
    const priceY = (value: number) => top + (max - value) / range * (bottom - top);
    const points = visibleData.map((datum, index) => ({ x: left + slot * (index + .5), openY: priceY(datum.open), highY: priceY(datum.high), lowY: priceY(datum.low), closeY: priceY(datum.close) }));
    return { top, bottom, volumeTop, volumeBottom, left, right, min, max, maxVolume, slot, points };
  }, [visibleData]);
  const tickIndexes = useMemo(() => Array.from(new Set([0, .2, .4, .6, .8, 1].map((ratio) => Math.round((visibleData.length - 1) * ratio)))), [visibleData.length]);
  const activeIndex = hoveredIndex === null ? null : Math.max(0, Math.min(visibleData.length - 1, hoveredIndex));
  const active = activeIndex === null ? null : { datum: visibleData[activeIndex], point: geometry.points[activeIndex] };
  const bodyWidth = Math.max(3, Math.min(9, geometry.slot * .62));

  const hoverAt = (event: React.PointerEvent<SVGSVGElement>) => {
    if (dragRef.current) {
      const bounds = event.currentTarget.getBoundingClientRect();
      const pixelsPerBar = bounds.width / VISIBLE_CANDLES;
      const deltaBars = Math.round((dragRef.current.startX - event.clientX) / Math.max(pixelsPerBar, 1));
      const nextEnd = Math.max(VISIBLE_CANDLES, Math.min(chartData.length, dragRef.current.startEnd + deltaBars));
      setWindowEnd(nextEnd); setHoveredIndex(null); return;
    }
    const bounds = event.currentTarget.getBoundingClientRect();
    const viewX = (event.clientX - bounds.left) / Math.max(bounds.width, 1) * 1000;
    setHoveredIndex(Math.max(0, Math.min(visibleData.length - 1, Math.floor((viewX - geometry.left) / geometry.slot))));
  };
  const stopDrag = (event: React.PointerEvent<SVGSVGElement>) => {
    if (dragRef.current && event.currentTarget.hasPointerCapture(event.pointerId)) event.currentTarget.releasePointerCapture(event.pointerId);
    dragRef.current = null; setDragging(false);
  };

  return <section className="pulse-chart-panel">
    <div className="pulse-price-head">
      <div><h1>{stock.name} <span>{stock.symbol} · {stock.exchange}</span></h1><div className="pulse-price-row"><strong>${stock.price.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}</strong><b className={stock.change >= 0 ? "positive" : "negative"}>{stock.change > 0 ? "+" : ""}{stock.change.toFixed(2)}&nbsp;&nbsp;{stock.changeRate > 0 ? "+" : ""}{stock.changeRate.toFixed(2)}%</b><span>已收盘</span><em>盘后</em><strong className="after-price">{stock.afterHours.toFixed(2)}</strong><b className="positive">{stock.afterRate > 0 ? "+" : ""}{stock.afterRate.toFixed(2)}%</b></div></div>
      <div className="pulse-head-actions"><button aria-label="添加收藏" type="button"><Icon name="star" /></button><button aria-label="更多操作" type="button"><Icon name="more" /></button></div>
    </div>
    <div className="pulse-chart-toolbar pulse-kline-toolbar"><div>{klinePeriods.map((item) => <button className={config.id === item.id ? "active" : ""} onClick={() => onPeriod(item.id)} key={item.id} type="button">{item.label}</button>)}</div><dl><div><dt>开</dt><dd>{stock.open.toFixed(2)}</dd></div><div><dt>高</dt><dd className="negative">{stock.high.toFixed(2)}</dd></div><div><dt>低</dt><dd className="positive">{stock.low.toFixed(2)}</dd></div><div><dt>量</dt><dd>{stock.volume}</dd></div></dl></div>
    <div className={"pulse-chart-wrap pulse-kline-wrap" + (dragging ? " dragging" : "")}>
      <div className="pulse-kline-note"><strong>{config.label}</strong><span>已加载 {chartData.length.toLocaleString("zh-CN")} / 上限 {MAX_KLINE_POINTS.toLocaleString("zh-CN")} 根</span><small>按住图表左右拖拽 · 当前窗口 {visibleData.length} 根</small></div>
      <svg viewBox="0 0 1000 330" role="img" tabIndex={0} aria-label={stock.symbol + " " + config.label + " K线图，已加载 " + chartData.length + " 根，可左右拖拽"} onPointerDown={(event) => { if (event.button !== 0) return; event.currentTarget.setPointerCapture(event.pointerId); dragRef.current = { startX: event.clientX, startEnd: safeEnd, pointerId: event.pointerId }; setDragging(true); }} onPointerMove={hoverAt} onPointerUp={stopDrag} onPointerCancel={stopDrag} onPointerLeave={() => { if (!dragRef.current) setHoveredIndex(null); }} onBlur={() => setHoveredIndex(null)} onKeyDown={(event) => { if (event.key === "ArrowLeft" || event.key === "ArrowRight") { event.preventDefault(); setHoveredIndex((current) => Math.max(0, Math.min(visibleData.length - 1, (current ?? visibleData.length - 1) + (event.key === "ArrowRight" ? 1 : -1)))); } }}>
        {[40,90,140,190,238].map((y) => <line x1="48" x2="980" y1={y} y2={y} key={y} className="pulse-grid-line"/>)}
        {[48,234,420,606,792,980].map((x) => <line x1={x} x2={x} y1="18" y2="306" key={x} className="pulse-grid-line"/>)}
        {visibleData.map((datum, index) => { const point = geometry.points[index]; const up = datum.close >= datum.open; const bodyY = Math.min(point.openY, point.closeY); const bodyHeight = Math.max(1.5, Math.abs(point.closeY - point.openY)); const volumeHeight = Math.max(2, datum.volume / geometry.maxVolume * (geometry.volumeBottom - geometry.volumeTop)); return <g key={datum.time.toISOString()} className={(up ? "pulse-candle-up" : "pulse-candle-down") + (activeIndex === index ? " active" : "")}><line x1={point.x} x2={point.x} y1={point.highY} y2={point.lowY} className="pulse-candle-wick"/><rect x={point.x - bodyWidth / 2} y={bodyY} width={bodyWidth} height={bodyHeight} className="pulse-candle-body"/><rect x={point.x - bodyWidth / 2} y={geometry.volumeBottom - volumeHeight} width={bodyWidth} height={volumeHeight} className="pulse-candle-volume"/></g>; })}
        {active ? <g aria-hidden="true"><line x1={active.point.x} x2={active.point.x} y1="18" y2="306" className="pulse-hover-line"/><line x1="48" x2="980" y1={active.point.closeY} y2={active.point.closeY} className="pulse-hover-line horizontal"/><circle cx={active.point.x} cy={active.point.closeY} r="4" className="pulse-hover-dot"/></g> : null}
        <rect x="48" y="18" width="932" height="288" fill="transparent"/>
        <g className="pulse-axis-labels"><text x="4" y="24">{geometry.max.toFixed(2)}</text><text x="4" y="132">{((geometry.max + geometry.min) / 2).toFixed(2)}</text><text x="4" y="240">{geometry.min.toFixed(2)}</text>{tickIndexes.map((index, tick) => <text key={index} x={geometry.points[index].x} y="326" textAnchor={tick === 0 ? "start" : tick === tickIndexes.length - 1 ? "end" : "middle"}>{visibleData[index].shortLabel}</text>)}</g>
      </svg>
      {active ? <div className={"pulse-chart-tooltip pulse-kline-tooltip" + (active.point.x > 720 ? " align-right" : "")} style={{ left: active.point.x / 10 + "%", top: active.point.closeY / 3.3 + "%" }} role="status" aria-live="polite"><header><strong>{active.datum.label}</strong><small>{config.label} · OHLCV 离线样例</small></header><dl><div><dt>开盘</dt><dd>{active.datum.open.toFixed(2)}</dd></div><div><dt>最高</dt><dd>{active.datum.high.toFixed(2)}</dd></div><div><dt>最低</dt><dd>{active.datum.low.toFixed(2)}</dd></div><div><dt>收盘</dt><dd>{active.datum.close.toFixed(2)}</dd></div><div><dt>实体涨跌</dt><dd className={active.datum.bodyChange >= 0 ? "positive" : "negative"}>{active.datum.bodyChange >= 0 ? "+" : ""}{active.datum.bodyChange.toFixed(2)} ({active.datum.bodyChangeRate >= 0 ? "+" : ""}{active.datum.bodyChangeRate.toFixed(2)}%)</dd></div><div><dt>较前收</dt><dd className={active.datum.change >= 0 ? "positive" : "negative"}>{active.datum.change >= 0 ? "+" : ""}{active.datum.change.toFixed(2)} ({active.datum.changeRate >= 0 ? "+" : ""}{active.datum.changeRate.toFixed(2)}%)</dd></div><div><dt>成交量</dt><dd>{compactVolume(active.datum.volume)}</dd></div></dl></div> : null}
    </div>
  </section>;
}

function InsightRail({ stock, collapsed, onToggle }: { stock: Stock; collapsed: boolean; onToggle: () => void }) {
  const bullets = [
    { title: "股价大幅下挫", body: `${stock.symbol} 当日变动 ${stock.changeRate.toFixed(2)}%，收于 ${stock.price.toFixed(2)}，价格处于日内偏弱区间。` },
    { title: "成交量显著放大", body: `成交量 ${stock.volume}，结合日内振幅观察，市场分歧与换手明显。` },
    { title: "盘后出现企稳迹象", body: `盘后价格 ${stock.afterHours.toFixed(2)}，变动 ${stock.afterRate.toFixed(2)}%，短线抛压有所缓解。` },
    { title: "宏观与新闻风险", body: "利率预期和地缘冲突仍在抬升避险情绪，需结合最新新闻持续跟踪。" },
  ];
  return <aside className={`pulse-insight ${collapsed ? "collapsed" : ""}`}>
    <div className="pulse-section-title"><h2>AI 速览</h2><button aria-label={collapsed ? "展开 AI 速览" : "收起 AI 速览"} onClick={onToggle} type="button"><Icon name="chevron" /></button></div>
    {!collapsed && <><div className="pulse-insight-list">{bullets.map((bullet) => <article key={bullet.title}><i/><div><h3>{bullet.title}</h3><p>{bullet.body}</p></div></article>)}</div><footer>由行情、新闻与社区接口聚合 · 离线演示</footer></>}
  </aside>;
}

function CompactDepthRows({ levels, side }: { levels: DepthLevel[]; side: "ask" | "bid" }) {
  return <>{levels.map((level, index) => <div className="pulse-book-row" key={side + "-" + level.price + "-" + index}><span>{(side === "ask" ? "卖" : "买") + (index + 1)}</span><strong className={side === "ask" ? "negative" : "positive"}>{level.price.toFixed(2)}</strong><span>{level.volume.toLocaleString("en-US")}</span></div>)}</>;
}

function OrderBook({ stock, orderBook, onExpand }: { stock: Stock; orderBook: OrderBookSnapshot | null; onExpand: () => void }) {
  if (!orderBook) {
    const message = stock.exchange === "INDEX" ? "指数不提供普通证券买卖盘" : "当前 HAR 未捕获该标的的盘口快照";
    return <section className="pulse-data-panel pulse-book-unavailable"><div className="pulse-data-title"><div><h2>40档盘口</h2><small>{stock.symbol}</small></div></div><div className="pulse-book-empty"><i>—</i><strong>{stock.symbol} 暂无盘口</strong><p>{message}</p><small>切换回 PYPL 可查看真实 Blue Ocean 40 档数据</small></div></section>;
  }
  return <section className="pulse-data-panel"><div className="pulse-data-title"><div><h2>40档盘口</h2><small>{orderBook.source}</small></div><button aria-label="展开40档盘口" onClick={onExpand} title="展开40档盘口" type="button"><Icon name="expand" /></button></div><div className="pulse-book-head"><span>卖盘 (Ask)</span><span>价格</span><span>数量</span></div><CompactDepthRows levels={orderBook.ask.slice(0, 3)} side="ask"/><div className="pulse-book-head bid"><span>买盘 (Bid)</span><span>价格</span><span>数量</span></div><CompactDepthRows levels={orderBook.bid.slice(0, 3)} side="bid"/><button className="pulse-depth-entry" onClick={onExpand} type="button">查看买40 / 卖40 <span>→</span></button></section>;
}

function DepthSide({ levels, side }: { levels: DepthLevel[]; side: "ask" | "bid" }) {
  const maxVolume = Math.max(...levels.map((level) => level.volume), 1);
  const label = side === "ask" ? "卖盘 Ask" : "买盘 Bid";
  return <section className={"pulse-depth-side " + side} aria-label={label}><div className="pulse-depth-side-title"><strong>{label}</strong><span>{levels.length} 档</span></div><div className="pulse-depth-columns"><span>档位</span><span>价格</span><span>数量</span><span>委托笔数</span></div><div className="pulse-depth-scroll">{levels.map((level, index) => <div className="pulse-depth-row" key={side + "-" + level.price + "-" + index}><i style={{ width: Math.max(3, level.volume / maxVolume * 100) + "%" }}/><span>{(side === "ask" ? "卖" : "买") + (index + 1)}</span><strong>{level.price.toFixed(2)}</strong><span>{level.volume.toLocaleString("en-US")}</span><span>{level.orderCount}</span></div>)}</div></section>;
}

function DepthModal({ orderBook, view, onView, onClose }: { orderBook: OrderBookSnapshot; view: "both" | "ask" | "bid"; onView: (view: "both" | "ask" | "bid") => void; onClose: () => void }) {
  const time = new Date(orderBook.timestamp).toLocaleString("zh-CN", { hour12: false });
  return <div className="pulse-depth-backdrop" onMouseDown={(event) => { if (event.target === event.currentTarget) onClose(); }}><div className={"pulse-depth-modal view-" + view} role="dialog" aria-modal="true" aria-labelledby="pulse-depth-title"><header><div><h2 id="pulse-depth-title">{orderBook.symbol} · 40档盘口</h2><p>{orderBook.source} · 买{orderBook.bid.length} / 卖{orderBook.ask.length} · 离线快照 {time}</p></div><button aria-label="关闭40档盘口" onClick={onClose} type="button">×</button></header><div className="pulse-depth-toolbar"><div role="group" aria-label="盘口显示方式">{[["both","双边"],["ask","仅卖"],["bid","仅买"]].map(([value, label]) => <button className={view === value ? "active" : ""} onClick={() => onView(value as "both" | "ask" | "bid")} key={value} type="button">{label}</button>)}</div><span>价格 / 数量 / 委托笔数</span></div><main>{view !== "bid" ? <DepthSide levels={orderBook.ask} side="ask"/> : null}{view !== "ask" ? <DepthSide levels={orderBook.bid} side="bid"/> : null}</main></div></div>;
}

function Trades() {
  const trades = [["15:59:58","53.66","100","▼"],["15:59:57","53.66","200","▼"],["15:59:56","53.65","300","▼"],["15:59:55","53.66","150","▲"],["15:59:54","53.66","250","▼"],["15:59:53","53.66","400","▼"],["15:59:52","53.67","100","▲"]];
  return <section className="pulse-data-panel"><div className="pulse-data-title"><h2>分时成交</h2><Icon name="expand" /></div><div className="pulse-trade-row head"><span>时间</span><span>价格</span><span>数量</span><span>方向</span></div>{trades.map((row) => <div className="pulse-trade-row" key={row[0]}><span>{row[0]}</span><strong className="negative">{row[1]}</strong><span>{row[2]}</span><b className={row[3] === "▲" ? "positive" : "negative"}>{row[3]}</b></div>)}</section>;
}

function newsTime(item: NewsItem) {
  return item.publishedAt.length > 10 ? item.publishedAt.slice(5) : item.publishedAt;
}

function NewsPanel({ snapshot, bookmarked, onBookmark, onMore }: { snapshot: NewsSnapshot | null; bookmarked: string[]; onBookmark: (id: string) => void; onMore: () => void }) {
  if (!snapshot) return <section className="pulse-data-panel pulse-news" id="pulse-news"><div className="pulse-data-title"><h2>新闻</h2></div><div className="pulse-news-empty">当前标的暂无新闻快照</div></section>;
  const preview = snapshot.items.slice(0, 2);
  return <section className="pulse-data-panel pulse-news" id="pulse-news"><div className="pulse-data-title"><div><h2>新闻</h2><small>{snapshot.symbol} · 3 路接口聚合</small></div><button aria-label={"展开" + snapshot.symbol + "全部新闻"} onClick={onMore} type="button"><Icon name="expand" /></button></div>{preview.map((news) => <article key={news.id}><div><h3 title={news.title}>{news.title}</h3><button className={bookmarked.includes(news.id) ? "active" : ""} aria-label={bookmarked.includes(news.id) ? "取消收藏新闻" : "收藏新闻"} onClick={() => onBookmark(news.id)} type="button"><Icon name="bookmark" /></button></div><small><em>{news.kind}</em>{news.source}<span>{newsTime(news)}</span></small><p>{news.summary || "该条快讯暂无摘要。"}</p></article>)}<button className="pulse-more-link" onClick={onMore} type="button">查看全部 {snapshot.items.length} 条新闻 <span>›</span></button></section>;
}

function NewsModal({ snapshot, onClose }: { snapshot: NewsSnapshot; onClose: () => void }) {
  return <div className="pulse-depth-backdrop" onMouseDown={(event) => { if (event.target === event.currentTarget) onClose(); }}><section className="pulse-news-modal" role="dialog" aria-modal="true" aria-labelledby="pulse-news-modal-title"><header><div><h2 id="pulse-news-modal-title">{snapshot.symbol} · 全部新闻</h2><p>{snapshot.source} · 3 路接口合并去重 · {snapshot.items.length} 条离线快照</p></div><button aria-label="关闭全部新闻" onClick={onClose} type="button">×</button></header><div className="pulse-news-modal-columns"><span>新闻内容</span><span>来源 / 时间</span></div><main>{snapshot.items.map((news) => <article key={news.id}><div><span>{news.kind}</span>{news.url ? <a href={news.url} rel="noreferrer" target="_blank">{news.title}</a> : <h3>{news.title}</h3>}<p>{news.summary || "该条快讯暂无摘要。"}</p></div><small><strong>{news.source}</strong>{news.publishedAt}</small></article>)}</main></section></div>;
}

function SentimentPanel({ snapshot, onMore }: { snapshot: CommunitySnapshot | null; onMore: () => void }) {
  if (!snapshot) return <section className="pulse-data-panel pulse-sentiment"><div className="pulse-data-title"><h2>社区情绪</h2></div><div className="pulse-news-empty">当前标的暂无社区快照</div></section>;
  const bearEnd = snapshot.bearish * .5;
  const neutralEnd = (snapshot.bearish + snapshot.neutral) * .5;
  const gauge = "conic-gradient(from 270deg, #ff5b57 0 " + bearEnd + "%, #858d92 " + bearEnd + "% " + neutralEnd + "%, #9be532 " + neutralEnd + "% 50%, transparent 50% 100%)";
  const needle = Math.max(-75, Math.min(75, (snapshot.bullish - snapshot.bearish) * .75));
  return <section className="pulse-data-panel pulse-sentiment"><div className="pulse-data-title"><div><h2>社区情绪</h2><small>{snapshot.symbol} · 内容倾向估算</small></div><Icon name="expand" /></div><strong className="pulse-discussions">{snapshot.tweetCount.toLocaleString("en-US")} <small>条讨论</small></strong><div className="pulse-gauge"><div className="pulse-gauge-arc" style={{ background: gauge }}/><i style={{ transform: "rotate(" + needle + "deg)" }}/></div><div className="pulse-legend"><span><i className="bear"/>看空 {snapshot.bearish}%</span><span><i className="neutral"/>中性 {snapshot.neutral}%</span><span><i className="bull"/>看多 {snapshot.bullish}%</span></div><div className="pulse-sample-note">基于最新 {snapshot.sampleSize} 条帖子样本</div><div className="pulse-topics"><small>热门话题</small><div>{snapshot.topics.length ? snapshot.topics.map((topic) => <span title={topic.name} key={topic.name}># {topic.name}<b>{topic.count}</b></span>) : <em>暂无关联主题</em>}</div></div><button className="pulse-more-link" onClick={onMore} type="button">查看采样说明 <span>›</span></button></section>;
}

function IpoPanel({ onMore }: { onMore: () => void }) {
  return <section className="pulse-data-panel pulse-ipo" id="pulse-ipo"><div className="pulse-data-title"><h2>IPO 关注</h2><Icon name="expand" /></div>{[{symbol:"00625",name:"希音-W",date:"2026/09/01",letter:"S"},{symbol:"09615",name:"梅卡曼德机器人",date:"2026/09/01",letter:"M"}].map((ipo) => <article key={ipo.symbol}><span>{ipo.letter}</span><div><strong>{ipo.name}</strong><small>{ipo.symbol}</small><p>预计上市&nbsp; {ipo.date}</p></div></article>)}<button className="pulse-more-link" onClick={onMore} type="button">查看全部 IPO <span>›</span></button></section>;
}

export function ProductDemo({ orderBooks, newsSnapshots, communitySnapshots, onOpenCatalog }: { orderBooks: OrderBookSnapshot[]; newsSnapshots: NewsSnapshot[]; communitySnapshots: CommunitySnapshot[]; onOpenCatalog: () => void }) {
  const [stock, setStock] = useState(stocks[0]);
  const [period, setPeriod] = useState<KlinePeriod>("M5");
  const [activeNav, setActiveNav] = useState("接口数据");
  const [collapsed, setCollapsed] = useState(false);
  const [bookmarked, setBookmarked] = useState<string[]>([]);
  const [query, setQuery] = useState("");
  const [toast, setToast] = useState("");
  const [depthOpen, setDepthOpen] = useState(false);
  const [depthView, setDepthView] = useState<"both" | "ask" | "bid">("both");
  const [newsOpen, setNewsOpen] = useState(false);
  const selectedOrderBook = orderBooks.find((book) => book.symbol === stock.symbol) ?? null;
  const selectedNews = newsSnapshots.find((snapshot) => snapshot.symbol === stock.symbol) ?? null;
  const selectedCommunity = communitySnapshots.find((snapshot) => snapshot.symbol === stock.symbol) ?? null;
  const toastTimer = useRef<number | null>(null);
  function showToast(message: string) {
    setToast(message);
    if (toastTimer.current) window.clearTimeout(toastTimer.current);
    toastTimer.current = window.setTimeout(() => setToast(""), 2200);
  }
  function navigate(label: string) {
    setActiveNav(label);
    if (label === "资讯" || label === "IPO") {
      window.setTimeout(() => document.getElementById(label === "资讯" ? "pulse-news" : "pulse-ipo")?.scrollIntoView({ behavior: "smooth", block: "center" }), 0);
    } else window.scrollTo({ top: 0, behavior: "smooth" });
  }
  function search() {
    const match = stocks.find((item) => `${item.symbol}${item.name}`.toLowerCase().includes(query.trim().toLowerCase()));
    if (match) { setStock(match); showToast(`已切换到 ${match.symbol}`); }
    else showToast(query.trim() ? "离线样例中暂无该标的" : "输入股票代码或名称进行搜索");
  }
  return <div className="pulse-shell">
    <header className="pulse-header">
      <button className="pulse-brand" onClick={() => navigate("接口数据")} type="button">知势 <span>Pulse</span></button>
      <nav aria-label="产品导航">{["接口数据","个股","资讯","IPO"].map((item) => <button className={activeNav === item ? "active" : ""} onClick={() => navigate(item)} key={item} type="button">{item}</button>)}</nav>
      <form className="pulse-search" onSubmit={(event) => { event.preventDefault(); search(); }}><Icon name="search"/><input aria-label="搜索股票、指数或新闻" onChange={(event) => setQuery(event.target.value)} placeholder="搜索股票、指数或新闻" value={query}/></form>
      <div className="pulse-header-actions"><button className="pulse-offline" onClick={() => showToast(import.meta.env.VITE_MARKET_LIVE_ENABLED === "true" ? "三大指数每秒实时刷新；其余模块展示 HAR 脱敏快照" : "当前展示 HAR 脱敏快照，不连接线上服务")} type="button"><i/>{import.meta.env.VITE_MARKET_LIVE_ENABLED === "true" ? "混合数据" : "离线演示"}</button><button className="pulse-code-button" aria-label="打开接口目录" onClick={() => activeNav === "接口数据" ? document.getElementById("market-interface-catalog")?.scrollIntoView({ behavior: "smooth", block: "start" }) : onOpenCatalog()} title="打开接口目录" type="button"><Icon name="code"/></button></div>
    </header>
    {activeNav === "接口数据" ? <MarketDataBoard/> : <>
      <div className="pulse-primary-grid"><Watchlist selected={stock} onSelect={(next) => { setStock(next); showToast(`已切换到 ${next.symbol}`); }}/><KlineChart stock={stock} period={period} onPeriod={(next) => { setPeriod(next); showToast(`已切换到 ${klinePeriods.find((item) => item.id === next)?.label ?? next}`); }}/><InsightRail stock={stock} collapsed={collapsed} onToggle={() => setCollapsed((value) => !value)}/></div>
      <div className="pulse-data-grid"><OrderBook stock={stock} orderBook={selectedOrderBook} onExpand={() => setDepthOpen(true)}/><Trades/><NewsPanel snapshot={selectedNews} bookmarked={bookmarked} onBookmark={(id) => setBookmarked((items) => items.includes(id) ? items.filter((item) => item !== id) : [...items, id])} onMore={() => setNewsOpen(true)}/><SentimentPanel snapshot={selectedCommunity} onMore={() => showToast("讨论数和热门话题来自接口；倾向基于最新帖子样本估算")}/><IpoPanel onMore={() => showToast("IPO 数据来自 trade.skytigris.cn")}/></div>
    </>}
    {depthOpen && selectedOrderBook ? <DepthModal orderBook={selectedOrderBook} view={depthView} onView={setDepthView} onClose={() => setDepthOpen(false)}/> : null}
    {newsOpen && selectedNews ? <NewsModal snapshot={selectedNews} onClose={() => setNewsOpen(false)}/> : null}
    {toast && <div className="pulse-toast" role="status">{toast}</div>}
  </div>;
}
