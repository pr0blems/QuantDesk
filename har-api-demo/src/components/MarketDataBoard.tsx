import { lazy, Suspense, useMemo, useState } from "react";
import type { CSSProperties } from "react";
import { useMarketRefresh } from "../hooks/useMarketRefresh";
import type { FearGreedIndex, MarketBreadth, RefreshableIndexQuote } from "../hooks/useMarketRefresh";

const FearGreedComparison = lazy(() => import("./FearGreedComparison").then((module) => ({ default: module.FearGreedComparison })));

type IndexQuote = RefreshableIndexQuote;

type Leader = {
  name: string;
  change: string;
  symbol: string;
};

type SectorCard = {
  name: string;
  change: string;
  leader: Leader;
};

const indexQuotes: IndexQuote[] = [
  {
    symbol: ".DJI",
    name: "道琼斯",
    latest: 53559.99,
    previous: 53569.44,
    series: [53712.57, 53648.82, 53676.7, 53736.49, 53599.94, 53643.88, 53730.91, 53802.5, 53686.02, 53654.69, 53682.35, 53530.63, 53584.76, 53517.87, 53529.06, 53513.82, 53551.88, 53540.69, 53556.62, 53514.16, 53551.39, 53515.14, 53536.54, 53583.2, 53544.87, 53567.23, 53559.99],
  },
  {
    symbol: ".IXIC",
    name: "纳斯达克",
    latest: 26402.424,
    previous: 26541.352,
    series: [26527.824, 26597.184, 26547.11, 26583.715, 26475.61, 26518.596, 26547.15, 26609.807, 26678.182, 26688.906, 26655.363, 26668.705, 26659.291, 26585.78, 26507.232, 26469.059, 26468.7, 26420.375, 26441.438, 26388.393, 26404.68, 26426.91, 26411.484, 26419.994, 26396.879, 26412.98, 26400.414, 26365.385, 26386.494, 26405.783, 26400.893, 26419.748, 26402.424],
  },
  {
    symbol: ".SPX",
    name: "标普500",
    latest: 7711.76,
    previous: 7730.99,
    series: [7738.16, 7745.4, 7737.33, 7747.66, 7724.97, 7734.38, 7738.78, 7759.11, 7770.6, 7761.69, 7765.2, 7766.68, 7747.07, 7733.57, 7728.71, 7724.03, 7713.31, 7719.49, 7717.74, 7706.36, 7709.25, 7706.24, 7712.49, 7709.81, 7712.92, 7713.13, 7707.75, 7712.03, 7709.12, 7702.49, 7707.22, 7712.08, 7710.59, 7711.22, 7713.73, 7711.76],
  },
];

const initialBreadth: MarketBreadth = { up: 2252, flat: 1208, down: 3771, serverTime: 1788243054686 };
const initialFearGreed: FearGreedIndex = {
  type: "US",
  latestValue: 59.79,
  prevDayValue: 63.72,
  prevWeekValue: 62.4,
  prevMonthValue: 53.98,
  symbol: ".SPX",
  latestTimestamp: 1788148800000,
  latestTime: "2026-08-31 19:45 EDT",
  latestComparedValue: 7686.14,
};
const initialCryptoFearGreed: FearGreedIndex = {
  type: "CC",
  latestValue: 70,
  prevDayValue: 61,
  prevWeekValue: 73,
  prevMonthValue: 26,
  symbol: "BTC.USD.CC",
  latestTimestamp: 1788242400000,
  latestTime: "2026-09-01 14:00 CST",
  latestComparedValue: 79142.88,
};

const concepts: SectorCard[] = [
  { name: "热门中概股", change: "-1.41%", leader: { symbol: "CHA", name: "霸王茶姬", change: "+4.35%" } },
  { name: "AI", change: "-0.74%", leader: { symbol: "NOW", name: "ServiceNow", change: "+4.54%" } },
  { name: "DeepSeek概念股", change: "+1.16%", leader: { symbol: "NOW", name: "ServiceNow", change: "+4.54%" } },
  { name: "哈里斯概念", change: "+2.55%", leader: { symbol: "AMZN", name: "亚马逊", change: "+3.97%" } },
  { name: "宅经济概念", change: "+2.53%", leader: { symbol: "DPZ", name: "达美乐比萨", change: "+5.40%" } },
  { name: "外卖概念", change: "+2.30%", leader: { symbol: "UBER", name: "优步", change: "+2.43%" } },
];

const industries: SectorCard[] = [
  { name: "家用器具与特殊消费品", change: "+4.70%", leader: { symbol: "NWL", name: "纽威品牌", change: "+4.93%" } },
  { name: "综合零售", change: "+3.55%", leader: { symbol: "AMZN", name: "亚马逊", change: "+3.97%" } },
  { name: "轮胎与橡胶", change: "+3.25%", leader: { symbol: "GT", name: "固特异轮胎橡胶", change: "+3.25%" } },
];

const premarketRanking = [
  { symbol: "AEHL", name: "羚羊控股", price: "6.01", change: "+69.77%", volume: "63,709" },
  { symbol: "GOLF", name: "Acushnet Holdings", price: "140.00", change: "+61.08%", volume: "1" },
  { symbol: "YDDL", name: "One and one Green", price: "2.54", change: "+53.77%", volume: "10,582" },
  { symbol: "COOT", name: "Australian Oilseeds", price: "0.71", change: "+53.72%", volume: "31,451" },
  { symbol: "NCRA", name: "Nocera, Inc.", price: "2.39", change: "+26.43%", volume: "16,281" },
];

const etfRanking = [
  { symbol: "SPY", name: "标普500 ETF", price: "769.35", change: "-0.23%", volume: "283.23亿" },
  { symbol: "QQQ", name: "纳指100 ETF", price: "716.43", change: "-0.65%", volume: "245.05亿" },
  { symbol: "GLD", name: "黄金 ETF-SPDR", price: "408.89", change: "-3.24%", volume: "103.78亿" },
  { symbol: "SOXL", name: "三倍做多半导体 ETF", price: "111.34", change: "-9.52%", volume: "69.85亿" },
  { symbol: "VOO", name: "Vanguard 标普500 ETF", price: "707.24", change: "-0.21%", volume: "57.23亿" },
];

const dividendRanking = [
  { symbol: "DTK", name: "DTE Energy 6.25% Debenture", price: "23.20", change: "20.11%", volume: "09/16/2026" },
  { symbol: "AGNC", name: "AGNC Investment", price: "10.90", change: "13.21%", volume: "08/31/2026" },
  { symbol: "TU", name: "TELUS", price: "9.67", change: "12.44%", volume: "09/10/2026" },
  { symbol: "NLY", name: "Annaly Capital", price: "23.04", change: "12.37%", volume: "--" },
  { symbol: "PBR.A", name: "巴西石油", price: "16.77", change: "10.85%", volume: "08/25/2026" },
];

const ipos = [
  { symbol: "AFA", name: "Farlong Holding Corporation", market: "US", status: "申购中", price: "$4.00", date: "待公布" },
  { symbol: "03231", name: "优地机器人", market: "HK", status: "申购中", price: "HK$14.45–19.55", date: "09/09" },
  { symbol: "02041", name: "麦科田", market: "HK", status: "申购中", price: "HK$15.42", date: "09/07" },
  { symbol: "09976", name: "江波龙", market: "HK", status: "申购中", price: "HK$240.60", date: "09/08" },
  { symbol: "00625", name: "希音-W", market: "HK", status: "已截止", price: "HK$47.60–49.50", date: "09/01" },
];

const interfaceGroups = [
  {
    title: "核心行情与资讯",
    items: [
      ["GET", "/v2/market", "市场聚合", "登录态"],
      ["GET", "/market/v2/indices", "全球指数", "登录态"],
      ["GET", "/api/global/fear_greed_index?type=US|CC", "美股/虚拟币恐贪指数", "登录态"],
      ["GET", "/discovery/api/v4/activities/market/list", "明星异动", "登录态"],
      ["GET", "/ipos", "新股日历", "登录态"],
      ["POST", "/stock_info/brief/all", "批量简行情", "登录态"],
      ["POST", "/stock_info/detail/all", "批量详细行情", "登录态"],
      ["POST", "/stock_info/thumbnail/all", "批量缩略走势", "登录态"],
      ["GET", "/api/etf/market", "ETF市场", "登录态"],
      ["POST", "/api/stock/US/rank/dividend", "股息排行", "登录态"],
    ],
  },
  {
    title: "个股深度与资金",
    items: [
      ["POST", "/stock_info/detail", "个股详细行情", "登录态"],
      ["GET", "/stock_info/ask_bid/arca/GPRO?props=askBidDepth", "40档深度盘口", "行情权限"],
      ["GET", "/stock_info/ask_bid/arca/GPRO?props=askBidHist", "买卖40档快照", "行情权限"],
      ["GET", "/stock_info/trade_tick/GPRO", "逐笔成交与统计", "登录态"],
      ["GET", "/stock_info/trade_price_list/GPRO", "成交价分布", "登录态"],
      ["GET", "/stock_info/fund_related/GPRO?withPublicityFund=1", "当日资金与筹码", "登录态"],
      ["GET", "/stock_info/fund_related/GPRO?withFundFlowTrend=1", "分时资金流向", "登录态"],
      ["GET", "/stock_info/fund_related/GPRO?withPositionChange=1", "5日大单变化", "登录态"],
    ],
  },
  {
    title: "辅助元数据",
    items: [
      ["GET", "/stock_info/exchangeRate", "汇率", "免登录"],
      ["GET", "/api/common/symbol_pool", "标的池", "登录态"],
      ["GET", "/api/global/tick-size", "最小报价单位", "登录态"],
      ["POST", "/apihub/symbolLabelsInfo", "证券标签", "客户端上下文"],
    ],
  },
] as const;

function changeClass(value: string | number) {
  return String(value).trim().startsWith("-") ? "negative" : "positive";
}

function SourceBadge({ children }: { children: string }) {
  return <code className="market-source">{children}</code>;
}

function Sparkline({ quote, source }: { quote: IndexQuote; source: "snapshot" | "live" }) {
  const width = 260;
  const height = 70;
  const min = Math.min(...quote.series);
  const max = Math.max(...quote.series);
  const hasRange = max > min;
  const points = quote.series.map((value, index) => {
    const x = index / Math.max(quote.series.length - 1, 1) * width;
    const y = hasRange ? 5 + (max - value) / (max - min) * (height - 10) : height / 2;
    return `${x.toFixed(1)},${y.toFixed(1)}`;
  }).join(" ");
  const isUp = quote.latest >= quote.previous;
  const seriesLabel = source === "live" ? "启动后实时采样走势" : "HAR 缩略走势";
  return <svg className={isUp ? "market-spark positive-line" : "market-spark negative-line"} viewBox={`0 0 ${width} ${height}`} role="img" aria-label={`${quote.name} ${seriesLabel}，共 ${quote.series.length} 个采样点`}>
    <polyline points={points} />
    {quote.series.length === 1 ? <circle cx={width / 2} cy={height / 2} r="2.5" /> : null}
  </svg>;
}

function IndexCard({ quote, source }: { quote: IndexQuote; source: "snapshot" | "live" }) {
  const change = quote.latest - quote.previous;
  const rate = change / quote.previous * 100;
  return <article className="market-index-card">
    <header><div><strong>{quote.name}</strong><span>{quote.symbol}</span></div><span className={`market-live-dot ${source}`}>{source === "live" ? "LIVE" : "HAR 200"}</span></header>
    <div className="market-index-value">{quote.latest.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}</div>
    <div className={changeClass(change)}>{change >= 0 ? "+" : ""}{change.toFixed(2)} &nbsp; {rate >= 0 ? "+" : ""}{rate.toFixed(2)}%</div>
    <Sparkline quote={quote} source={source} />
  </article>;
}

function formatRefreshTime(value: number | null) {
  if (!value) return "等待首次检查";
  return new Date(value).toLocaleTimeString("zh-CN", { hour12: false });
}

type MarketRefreshResult = ReturnType<typeof useMarketRefresh>;

function LiveMarketStrip({ market }: { market: MarketRefreshResult }) {
  const { quotes, breadth, state, actions, liveConfigured } = market;
  const totalBreadth = breadth.up + breadth.flat + breadth.down;
  const upWidth = breadth.up / Math.max(totalBreadth, 1) * 100;
  const flatWidth = breadth.flat / Math.max(totalBreadth, 1) * 100;
  const statusLabel = state.status === "paused"
    ? "已暂停"
    : state.status === "error"
      ? `重试中 · ${Math.round(state.retryDelayMs / 1000)}秒`
      : state.source === "live"
        ? state.status === "refreshing" ? "正在刷新" : "实时连接"
        : "快照巡检";
  const detailLabel = state.error
    ? state.error
    : liveConfigured
      ? `${state.latencyMs ?? "--"}ms · 未变化 ${state.unchangedCount} 次`
      : `每秒检查 · 快照未变化 ${state.unchangedCount} 次`;

  return <>
    <section className={`market-refresh-bar ${state.status}`} aria-label="行情刷新状态">
      <div className="market-refresh-state"><i/><div><strong>{statusLabel}</strong><span>{detailLabel}</span></div></div>
      <dl>
        <div><dt>刷新间隔</dt><dd>1秒</dd></div>
        <div><dt>上次检查</dt><dd>{formatRefreshTime(state.lastCheckedAt)}</dd></div>
        <div><dt>检查次数</dt><dd>{state.checkCount.toLocaleString("zh-CN")}</dd></div>
      </dl>
      <div className="market-refresh-actions">
        <button onClick={actions.refreshNow} disabled={!state.enabled} type="button">立即检查</button>
        <button className={state.enabled ? "active" : ""} onClick={actions.toggle} aria-pressed={state.enabled} type="button">{state.enabled ? "暂停刷新" : "继续刷新"}</button>
      </div>
    </section>
    <section className="market-indices" aria-label="三大指数">
      {quotes.map((quote) => <IndexCard key={quote.symbol} quote={quote} source={state.source} />)}
      <article className="market-breadth-card">
        <header><strong>市场宽度</strong><div><span className={`market-live-dot ${state.breadthSource}`}>{state.breadthSource === "live" ? "LIVE" : "HAR 快照"}</span><SourceBadge>{breadth.provider ?? "/v2/market"}</SourceBadge></div></header>
        <div className="market-breadth-numbers"><span><b className="positive">{breadth.up.toLocaleString("en-US")}</b>上涨</span><span><b>{breadth.flat.toLocaleString("en-US")}</b>平盘</span><span><b className="negative">{breadth.down.toLocaleString("en-US")}</b>下跌</span></div>
        <div className="market-breadth-bar"><i style={{ width: `${upWidth}%` }} /><i style={{ width: `${flatWidth}%` }} /><i /></div>
        <small>上涨占比 {upWidth.toFixed(1)}% · 下跌占比 {(breadth.down / Math.max(totalBreadth, 1) * 100).toFixed(1)}% · 接口响应 {formatRefreshTime(breadth.serverTime ?? null)}</small>
      </article>
    </section>
  </>;
}

function fearGreedLabel(value: number) {
  if (value >= 75) return "极度贪婪";
  if (value >= 55) return "贪婪";
  if (value >= 45) return "中性";
  if (value >= 25) return "恐惧";
  return "极度恐惧";
}

function FearGreedCard({
  usData,
  cryptoData,
  usSource,
  cryptoSource,
}: {
  usData: FearGreedIndex;
  cryptoData: FearGreedIndex;
  usSource: "snapshot" | "live";
  cryptoSource: "snapshot" | "live";
}) {
  const [view, setView] = useState<"US" | "CC">("US");
  const data = view === "US" ? usData : cryptoData;
  const source = view === "US" ? usSource : cryptoSource;
  const angle = Math.max(-90, Math.min(90, data.latestValue / 100 * 180 - 90));
  const dayDelta = data.latestValue - data.prevDayValue;
  const summary = source === "live"
    ? `较前一日${dayDelta >= 0 ? "上升" : "下降"} ${Math.abs(dayDelta).toFixed(2)} 点 · 数据时间 ${data.latestTime ?? "--"}`
    : `显示 HAR 抓包值 · 数据时间 ${data.latestTime ?? "--"}`;

  return <section className="market-panel market-fear">
    <header className="market-panel-head">
      <div><h2>恐贪指数</h2><span className={`market-live-dot ${source}`}>{source === "live" ? "LIVE" : "HAR 快照"}</span><SourceBadge>{data.provider ?? "/fear_greed_index"}</SourceBadge></div>
      <div className="market-tabs" role="tablist" aria-label="恐贪指数市场">
        <button className={view === "US" ? "active" : ""} onClick={() => setView("US")} role="tab" aria-selected={view === "US"} type="button">美股</button>
        <button className={view === "CC" ? "active" : ""} onClick={() => setView("CC")} role="tab" aria-selected={view === "CC"} type="button">虚拟币</button>
      </div>
    </header>
    <div className="market-fear-gauge" style={{ "--fear-angle": `${angle.toFixed(1)}deg` } as CSSProperties} aria-label={`当前恐贪指数 ${data.latestValue.toFixed(2)}，${fearGreedLabel(data.latestValue)}`}>
      <i className="market-fear-needle" aria-hidden="true" />
      <strong>{data.latestValue.toFixed(2)}</strong>
    </div>
    <div className="market-fear-status">{fearGreedLabel(data.latestValue)}</div>
    <dl>
      <div><dt>前一日</dt><dd>{data.prevDayValue.toFixed(2)}</dd></div>
      <div><dt>一周前</dt><dd>{data.prevWeekValue.toFixed(2)}</dd></div>
      <div><dt>一个月前</dt><dd>{data.prevMonthValue.toFixed(2)}</dd></div>
    </dl>
    <p>{summary}</p>
    <p className="market-fear-response">接口响应 {formatRefreshTime(data.serverTime ?? null)} · 参数 type={view}</p>
  </section>;
}

function SectorGrid({ items }: { items: SectorCard[] }) {
  return <div className="market-sector-grid">{items.map((item) => <article key={item.name}>
    <div><strong>{item.name}</strong><b className={changeClass(item.change)}>{item.change}</b></div>
    <small>领涨股</small>
    <footer><span>{item.leader.name}<em>{item.leader.symbol}</em></span><b className={changeClass(item.leader.change)}>{item.leader.change}</b></footer>
  </article>)}</div>;
}

export function MarketDataBoard() {
  const hasLiveIndices = import.meta.env.VITE_MARKET_LIVE_ENABLED === "true";
  const market = useMarketRefresh(indexQuotes, initialBreadth, initialFearGreed, initialCryptoFearGreed);
  const [sectorView, setSectorView] = useState<"concept" | "industry">("concept");
  const [rankingView, setRankingView] = useState<"premarket" | "etf" | "dividend">("premarket");
  const selectedRanking = useMemo(() => {
    if (rankingView === "etf") return { rows: etfRanking, third: "涨跌幅", fourth: "成交额" };
    if (rankingView === "dividend") return { rows: dividendRanking, third: "股息率", fourth: "下次除息" };
    return { rows: premarketRanking, third: "盘前涨幅", fourth: "成交量" };
  }, [rankingView]);
  const scrollToInterfaces = () => document.getElementById("market-interface-catalog")?.scrollIntoView({ behavior: "smooth", block: "start" });

  return <main className="market-board">
    <section className="market-hero">
      <div>
        <span className="market-eyebrow">US MARKET · {hasLiveIndices ? "三大指数与双市场恐贪实时" : "HAR 抓包快照"}</span>
        <h1>可用接口数据工作台</h1>
        <p>展示新 HAR 中已确认业务成功的市场、行情、ETF、新股与异动数据。所有内容均已脱敏，不在前端保存 App 令牌。</p>
      </div>
      <div className="market-summary-metrics">
        <div><strong>22</strong><span>可用数据接口</span></div>
        <div><strong>18</strong><span>核心接口</span></div>
        <div><strong>4</strong><span>辅助接口</span></div>
        <button type="button" onClick={scrollToInterfaces}>查看接口目录 <span>→</span></button>
      </div>
    </section>

    <LiveMarketStrip market={market} />

    <div className="market-main-grid">
      <section className="market-panel market-sectors">
        <header className="market-panel-head">
          <div><h2>市场主题</h2><SourceBadge>/v2/market</SourceBadge></div>
          <div className="market-tabs" role="tablist" aria-label="市场主题类型">
            <button className={sectorView === "concept" ? "active" : ""} onClick={() => setSectorView("concept")} role="tab" aria-selected={sectorView === "concept"} type="button">热门概念</button>
            <button className={sectorView === "industry" ? "active" : ""} onClick={() => setSectorView("industry")} role="tab" aria-selected={sectorView === "industry"} type="button">热门行业</button>
          </div>
        </header>
        <SectorGrid items={sectorView === "concept" ? concepts : industries} />
      </section>

      <FearGreedCard
        usData={market.fearGreed}
        cryptoData={market.cryptoFearGreed}
        usSource={market.state.fearGreedSource}
        cryptoSource={market.state.cryptoFearGreedSource}
      />
    </div>

    <Suspense fallback={<section className="market-panel fear-comparison empty"><h2>美股恐贪 × 标普500</h2><p>正在加载历史对比分析…</p></section>}>
      <FearGreedComparison data={market.fearGreed} source={market.state.fearGreedSource} />
    </Suspense>

    <div className="market-main-grid lower">
      <section className="market-panel market-ranking">
        <header className="market-panel-head">
          <div><h2>实时榜单</h2><SourceBadge>{rankingView === "dividend" ? "/api/stock/US/rank/dividend" : rankingView === "etf" ? "/api/etf/market" : "/v2/market"}</SourceBadge></div>
          <div className="market-tabs" role="tablist" aria-label="榜单类型">
            <button className={rankingView === "premarket" ? "active" : ""} onClick={() => setRankingView("premarket")} type="button">盘前涨幅</button>
            <button className={rankingView === "etf" ? "active" : ""} onClick={() => setRankingView("etf")} type="button">ETF热度</button>
            <button className={rankingView === "dividend" ? "active" : ""} onClick={() => setRankingView("dividend")} type="button">高股息</button>
          </div>
        </header>
        <div className="market-table" role="table">
          <div className="market-table-row head" role="row"><span>代码 / 名称</span><span>最新价</span><span>{selectedRanking.third}</span><span>{selectedRanking.fourth}</span></div>
          {selectedRanking.rows.map((row) => <div className="market-table-row" role="row" key={row.symbol}><span><b>{row.symbol}</b><small>{row.name}</small></span><span>{row.price}</span><strong className={changeClass(row.change)}>{row.change}</strong><span>{row.volume}</span></div>)}
        </div>
      </section>

      <aside className="market-side-stack">
        <section className="market-panel market-mover">
          <header className="market-panel-head"><div><h2>明星异动</h2><SourceBadge>/activities/market/list</SourceBadge></div></header>
          <div className="market-mover-main"><span>US</span><div><strong>Grab Holdings</strong><small>GRAB · 盘前</small></div><b className="negative">-5.82%</b></div>
          <footer><span>最新价 $3.40</span><strong className="negative">夜盘大跌</strong></footer>
        </section>
        <section className="market-panel market-etf-links">
          <header className="market-panel-head"><div><h2>指数关联 ETF</h2><SourceBadge>/api/etf/market</SourceBadge></div></header>
          <div>{[["SPY","标普500","-0.23%"],["QQQ","纳指100","-0.65%"],["IWM","罗素2000","-1.35%"],["UVXY","波动率","+1.65%"],["TLT","20+年国债","-0.30%"],["YINN","中国A50","+1.91%"]].map(([symbol, name, change]) => <article key={symbol}><b>{symbol}</b><span>{name}</span><strong className={changeClass(change)}>{change}</strong></article>)}</div>
        </section>
      </aside>
    </div>

    <section className="market-panel market-ipos">
      <header className="market-panel-head"><div><h2>新股日历</h2><SourceBadge>trade.skytigris.cn/ipos</SourceBadge></div><span className="market-record-count">抓包返回 6 条 · 展示 5 条</span></header>
      <div className="market-ipo-grid">{ipos.map((ipo) => <article key={ipo.symbol}><header><span>{ipo.market}</span><b className={ipo.status === "申购中" ? "positive" : ""}>{ipo.status}</b></header><strong>{ipo.name}</strong><small>{ipo.symbol}</small><dl><div><dt>发行价</dt><dd>{ipo.price}</dd></div><div><dt>预计上市</dt><dd>{ipo.date}</dd></div></dl></article>)}</div>
    </section>

    <section className="market-panel market-interfaces" id="market-interface-catalog">
      <header className="market-panel-head"><div><h2>接口可用性</h2><span>HAR 响应成功不等于公开 API；21 个接口依赖登录态、行情权限或客户端上下文。</span></div><span className="market-public-badge">1 个免登录</span></header>
      {interfaceGroups.map((group) => <div className="market-interface-group" key={group.title}><h3>{group.title}</h3><div>{group.items.map(([method, path, purpose, auth]) => <article key={`${method}-${path}`}><span className={`market-method ${method.toLowerCase()}`}>{method}</span><code>{path}</code><strong>{purpose}</strong><em className={auth === "免登录" ? "public" : ""}>{auth}</em></article>)}</div></div>)}
      <footer><span>本次新增：40档盘口、逐笔成交、资金流向、5日大单、成交价与筹码分布；仍未捕获完整K线和新闻正文。</span><button onClick={scrollToInterfaces} type="button">当前目录：22 个 →</button></footer>
    </section>
  </main>;
}
