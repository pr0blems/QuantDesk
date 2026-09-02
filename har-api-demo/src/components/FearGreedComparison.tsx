import { useMemo, useState } from "react";
import {
  CartesianGrid,
  Line,
  LineChart,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import type { FearGreedIndex, FearGreedHistoryPoint } from "../hooks/useMarketRefresh";

type RangeKey = "1M" | "3M" | "6M" | "1Y" | "ALL";
type ChartPoint = FearGreedHistoryPoint & { date: string; shortDate: string };

const ranges: Array<{ key: RangeKey; label: string; days: number | null }> = [
  { key: "1M", label: "近1月", days: 31 },
  { key: "3M", label: "近3月", days: 93 },
  { key: "6M", label: "近6月", days: 186 },
  { key: "1Y", label: "近1年", days: 366 },
  { key: "ALL", label: "全部", days: null },
];

function pearson(xs: number[], ys: number[]) {
  if (xs.length < 3 || xs.length !== ys.length) return null;
  const xMean = xs.reduce((sum, value) => sum + value, 0) / xs.length;
  const yMean = ys.reduce((sum, value) => sum + value, 0) / ys.length;
  let numerator = 0;
  let xDenominator = 0;
  let yDenominator = 0;
  xs.forEach((x, index) => {
    const xd = x - xMean;
    const yd = ys[index] - yMean;
    numerator += xd * yd;
    xDenominator += xd * xd;
    yDenominator += yd * yd;
  });
  const denominator = Math.sqrt(xDenominator * yDenominator);
  return denominator ? numerator / denominator : null;
}

function maxDrawdown(points: ChartPoint[]) {
  let peak = points[0]?.comparedValue ?? 0;
  let drawdown = 0;
  points.forEach((point) => {
    peak = Math.max(peak, point.comparedValue);
    if (peak) drawdown = Math.min(drawdown, point.comparedValue / peak - 1);
  });
  return drawdown * 100;
}

function fearLabel(value: number) {
  if (value >= 75) return "极度贪婪";
  if (value >= 55) return "贪婪";
  if (value >= 45) return "中性";
  if (value >= 25) return "恐惧";
  return "极度恐惧";
}

function fearZone(value: number) {
  if (value >= 75) return "extreme-greed";
  if (value >= 55) return "greed";
  if (value >= 45) return "neutral";
  if (value >= 25) return "fear";
  return "extreme-fear";
}

function conditionalForwardStats(points: ChartPoint[], horizon = 5) {
  const latest = points.at(-1);
  if (!latest || points.length <= horizon) return null;
  const zone = fearZone(latest.value);
  const returns: number[] = [];
  for (let index = 0; index + horizon < points.length; index += 1) {
    if (fearZone(points[index].value) !== zone || !points[index].comparedValue) continue;
    returns.push((points[index + horizon].comparedValue / points[index].comparedValue - 1) * 100);
  }
  if (!returns.length) return null;
  return {
    count: returns.length,
    averageReturn: returns.reduce((sum, value) => sum + value, 0) / returns.length,
    winRate: returns.filter((value) => value > 0).length / returns.length * 100,
  };
}

function correlationLabel(value: number | null) {
  if (value === null) return "样本不足";
  const strength = Math.abs(value) >= 0.6 ? "较强" : Math.abs(value) >= 0.3 ? "中等" : "偏弱";
  return `${strength}${value >= 0 ? "正" : "负"}相关`;
}

function formatSigned(value: number, suffix = "") {
  return `${value >= 0 ? "+" : ""}${value.toFixed(2)}${suffix}`;
}

function ComparisonTooltip({ active, payload }: { active?: boolean; payload?: Array<{ payload?: ChartPoint }> }) {
  const point = payload?.[0]?.payload;
  if (!active || !point) return null;
  return <div className="fear-compare-tooltip">
    <strong>{point.date}</strong>
    <span><i className="fear-dot" />恐贪指数 <b>{point.value.toFixed(2)}</b></span>
    <span><i className="spx-dot" />标普500 <b>{point.comparedValue.toLocaleString("en-US", { maximumFractionDigits: 2 })}</b></span>
  </div>;
}

function buildAnalysis(points: ChartPoint[], correlation: number | null) {
  if (points.length < 2) return { regime: "样本不足", divergence: "等待更多历史数据", action: "暂不形成观察信号", tone: "neutral" };
  const last = points.at(-1) as ChartPoint;
  const lookback = points[Math.max(0, points.length - 21)];
  const fearChange = last.value - lookback.value;
  const spxReturn = (last.comparedValue / lookback.comparedValue - 1) * 100;
  const sameDirection = Math.sign(fearChange) === Math.sign(spxReturn);
  const divergence = sameDirection
    ? `情绪与价格同向，近20个样本形成${spxReturn >= 0 ? "多头" : "空头"}确认`
    : fearChange < 0 && spxReturn > 0
      ? "指数走强但情绪降温，出现谨慎型背离"
      : "指数走弱但情绪修复，出现修复型背离";
  const regime = `${fearLabel(last.value)} · ${correlationLabel(correlation)}`;
  const action = last.value >= 75
    ? "避免追涨，关注回撤、波动率抬升与宽度转弱"
    : last.value <= 25
      ? "进入逆向观察区，等待价格止跌和市场宽度确认"
      : fearChange < -8 && spxReturn > 0
        ? "价格仍强但风险偏好下降，宜收紧风险预算"
        : spxReturn > 0 && fearChange > 0
          ? "趋势与情绪共振，持仓管理优先于逆势猜顶"
          : "信号未形成共振，等待方向和宽度进一步确认";
  const tone = last.value >= 75 || (fearChange < -8 && spxReturn > 0) ? "risk" : last.value <= 25 ? "watch" : "neutral";
  return { regime, divergence, action, tone };
}

export function FearGreedComparison({ data, source }: { data: FearGreedIndex; source: "snapshot" | "live" }) {
  const [range, setRange] = useState<RangeKey>("1Y");
  const allPoints = useMemo<ChartPoint[]>(() => (data.items ?? []).map((item) => {
    const date = new Date(item.timestamp);
    return {
      ...item,
      date: date.toLocaleDateString("zh-CN", { year: "numeric", month: "2-digit", day: "2-digit" }),
      shortDate: date.toLocaleDateString("zh-CN", { month: "2-digit", day: "2-digit" }),
    };
  }).sort((a, b) => a.timestamp - b.timestamp), [data.items]);

  const points = useMemo(() => {
    const selected = ranges.find((item) => item.key === range);
    if (!selected?.days || !allPoints.length) return allPoints;
    const cutoff = (allPoints.at(-1)?.timestamp ?? 0) - selected.days * 86400000;
    return allPoints.filter((point) => point.timestamp >= cutoff);
  }, [allPoints, range]);

  const stats = useMemo(() => {
    if (points.length < 2) return null;
    const first = points[0];
    const last = points.at(-1) as ChartPoint;
    const fearDeltas: number[] = [];
    const spxReturns: number[] = [];
    for (let index = 1; index < points.length; index += 1) {
      fearDeltas.push(points[index].value - points[index - 1].value);
      spxReturns.push((points[index].comparedValue / points[index - 1].comparedValue - 1) * 100);
    }
    const correlation = pearson(fearDeltas, spxReturns);
    return {
      fearChange: last.value - first.value,
      spxReturn: (last.comparedValue / first.comparedValue - 1) * 100,
      correlation,
      drawdown: maxDrawdown(points),
      conditional: conditionalForwardStats(points),
      analysis: buildAnalysis(points, correlation),
    };
  }, [points]);

  if (!points.length) {
    return <section className="market-panel fear-comparison empty"><h2>美股恐贪 × 标普500</h2><p>正在等待历史序列。</p></section>;
  }

  const latest = points.at(-1) as ChartPoint;
  return <section className="market-panel fear-comparison" aria-label="美股恐贪指数与标普500对比分析">
    <header className="fear-comparison-head">
      <div><span className="market-eyebrow">SENTIMENT × PRICE</span><h2>美股恐贪 × 标普500</h2><p>同日双轴走势 · 日变化相关性 · 背离观察</p></div>
      <div><span className={`market-live-dot ${source}`}>{source === "live" ? "LIVE" : "HAR 快照"}</span><code>/fear_greed_index?type=US</code></div>
    </header>

    <div className="fear-stat-grid">
      <article><span>当前恐贪</span><strong>{latest.value.toFixed(2)}</strong><small>{fearLabel(latest.value)}</small></article>
      <article><span>标普500</span><strong>{latest.comparedValue.toLocaleString("en-US", { maximumFractionDigits: 2 })}</strong><small>{formatSigned(stats?.spxReturn ?? 0, "%")} 区间</small></article>
      <article><span>日变化相关性</span><strong>{stats?.correlation?.toFixed(2) ?? "--"}</strong><small>{correlationLabel(stats?.correlation ?? null)}</small></article>
      <article><span>区间最大回撤</span><strong className="negative">{stats?.drawdown.toFixed(2) ?? "--"}%</strong><small>标普500</small></article>
    </div>

    <div className="fear-chart-wrap">
      <div className="fear-chart-legend"><span><i className="fear-dot" />恐贪指数</span><span><i className="spx-dot" />标普500</span><em>{points.length} 个日频样本</em></div>
      <ResponsiveContainer width="100%" height={330}>
        <LineChart data={points} margin={{ top: 14, right: 10, bottom: 4, left: 2 }}>
          <CartesianGrid stroke="#253038" strokeDasharray="3 5" vertical={false} />
          <XAxis dataKey="shortDate" tick={{ fill: "#74838b", fontSize: 10 }} tickLine={false} axisLine={{ stroke: "#344149" }} minTickGap={45} />
          <YAxis yAxisId="fear" domain={[0, 100]} ticks={[0, 20, 40, 60, 80, 100]} tick={{ fill: "#ff8a57", fontSize: 10 }} tickLine={false} axisLine={false} width={34} />
          <YAxis yAxisId="spx" orientation="right" domain={["dataMin - 100", "dataMax + 100"]} tick={{ fill: "#9ba7b7", fontSize: 10 }} tickLine={false} axisLine={false} width={58} tickFormatter={(value) => Number(value).toLocaleString("en-US", { maximumFractionDigits: 0 })} />
          <ReferenceLine yAxisId="fear" y={25} stroke="#4778df" strokeDasharray="4 4" opacity={0.45} />
          <ReferenceLine yAxisId="fear" y={75} stroke="#ef6565" strokeDasharray="4 4" opacity={0.45} />
          <Tooltip content={<ComparisonTooltip />} cursor={{ stroke: "#d7dede", strokeDasharray: "4 4" }} />
          <Line yAxisId="fear" type="monotone" dataKey="value" stroke="#ff7b43" strokeWidth={2} dot={false} activeDot={{ r: 4, fill: "#ff7b43", stroke: "#11191d" }} isAnimationActive={false} />
          <Line yAxisId="spx" type="monotone" dataKey="comparedValue" stroke="#a8b1c3" strokeWidth={2} dot={false} activeDot={{ r: 4, fill: "#a8b1c3", stroke: "#11191d" }} isAnimationActive={false} />
        </LineChart>
      </ResponsiveContainer>
      <div className="fear-range-tabs" role="tablist" aria-label="对比区间">
        {ranges.map((item) => <button key={item.key} className={range === item.key ? "active" : ""} onClick={() => setRange(item.key)} role="tab" aria-selected={range === item.key} type="button">{item.label}</button>)}
      </div>
    </div>

    <div className="fear-analysis-grid">
      <article><span>市场状态</span><strong>{stats?.analysis.regime}</strong><p>恐贪区间与日变化相关性共同判断环境，不以单点数值直接交易。</p></article>
      <article><span>背离检测</span><strong>{stats?.analysis.divergence}</strong><p>比较近20个样本的情绪变化和标普收益方向，用于识别趋势质量。</p></article>
      <article className={stats?.analysis.tone}><span>操盘观察</span><strong>{stats?.analysis.action}</strong><p>建议同时确认市场宽度、价格结构和波动率；该结论仅作研究辅助。</p></article>
      <article><span>同区间后5日</span><strong>{stats?.conditional ? `${stats.conditional.winRate.toFixed(0)}% 上涨` : "样本不足"}</strong><p>{stats?.conditional ? `${stats.conditional.count} 个历史样本，标普500平均收益 ${formatSigned(stats.conditional.averageReturn, "%")}。这是条件统计，不代表未来收益。` : "当前区间内没有足够的前瞻样本。"}</p></article>
    </div>
  </section>;
}
