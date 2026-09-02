import { readFile } from "node:fs/promises";
import { defineConfig, loadEnv } from "vite";
import type { Connect, Plugin } from "vite";
import react from "@vitejs/plugin-react";

type HarHeader = { name: string; value: string };
type RequestTemplate = { url: string; headers: Record<string, string>; method: string; body?: string };
type FearGreedData = {
  type?: "US" | "CC";
  latestValue?: number;
  prevDayValue?: number;
  prevWeekValue?: number;
  prevMonthValue?: number;
  symbol?: string;
  latestTimestamp?: number;
  latestTime?: string;
  latestComparedValue?: number;
  items?: Array<{
    timestamp?: number;
    value?: number;
    comparedTimestamp?: number;
    comparedValue?: number;
  }>;
};
type MarketBreadthData = { up?: number; flat?: number; down?: number };

const excludedHeaders = new Set(["accept-encoding", "connection", "content-length", "cookie", "host", "origin", "referer"]);

function jsonResponse(res: Connect.ServerResponse, statusCode: number, payload: unknown) {
  res.statusCode = statusCode;
  res.setHeader("Content-Type", "application/json; charset=utf-8");
  res.setHeader("Cache-Control", "no-store");
  res.end(JSON.stringify(payload));
}

function decodeHarString(value: string) {
  return JSON.parse(`"${value}"`) as string;
}

// Some mobile HAR exporters leave an unescaped quote inside a response body.
// Read only the well-formed request blocks so those response encoding defects do not
// prevent us from recovering the URL and authenticated request headers.
function matchesQuery(url: URL, requiredQuery: Record<string, string>) {
  return Object.entries(requiredQuery).every(([name, value]) => url.searchParams.get(name) === value);
}

function extractTemplate(raw: string, pathname: string, requiredQuery: Record<string, string> = {}): RequestTemplate | null {
  const urlPattern = /"url"\s*:\s*"((?:\\.|[^"\\])*)"/g;
  for (const match of raw.matchAll(urlPattern)) {
    const url = decodeHarString(match[1]);
    let parsed: URL;
    try { parsed = new URL(url); }
    catch { continue; }
    if (parsed.pathname !== pathname || !matchesQuery(parsed, requiredQuery)) continue;

    const requestStart = raw.lastIndexOf('"request"', match.index ?? 0);
    if (requestStart < 0) continue;
    const requestBlock = raw.slice(requestStart, match.index);
    const method = requestBlock.match(/"method"\s*:\s*"([A-Z]+)"/)?.[1] ?? "GET";
    const bodyMatch = requestBlock.match(/"postData"\s*:\s*\{[\s\S]*?"text"\s*:\s*"((?:\\.|[^"\\])*)"/);
    const headersBlock = requestBlock.match(/"headers"\s*:\s*\[(.*?)\]\s*,\s*"queryString"/s)?.[1] ?? "";
    const headers: HarHeader[] = [];
    const headerPattern = /"name"\s*:\s*"((?:\\.|[^"\\])*)"\s*,\s*"value"\s*:\s*"((?:\\.|[^"\\])*)"/g;
    for (const headerMatch of headersBlock.matchAll(headerPattern)) {
      headers.push({ name: decodeHarString(headerMatch[1]), value: decodeHarString(headerMatch[2]) });
    }
    return {
      url,
      method,
      body: bodyMatch ? decodeHarString(bodyMatch[1]) : undefined,
      headers: Object.fromEntries(headers
        .filter((header) => !excludedHeaders.has(header.name.toLowerCase()))
        .map((header) => [header.name, header.value])),
    };
  }
  return null;
}

function extractResponsePayload(raw: string, pathname: string, requiredQuery: Record<string, string> = {}) {
  const urlPattern = /"url"\s*:\s*"((?:\\.|[^"\\])*)"/g;
  for (const match of raw.matchAll(urlPattern)) {
    let parsed: URL;
    try { parsed = new URL(decodeHarString(match[1])); }
    catch { continue; }
    if (parsed.pathname !== pathname || !matchesQuery(parsed, requiredQuery)) continue;
    const entryEnd = raw.indexOf('"startedDateTime"', match.index ?? 0);
    const responseBlock = raw.slice(match.index, entryEnd > 0 ? entryEnd : (match.index ?? 0) + 100000);
    let longest = "";
    const textPattern = /"text"\s*:\s*"((?:\\.|[^"\\])*)"/g;
    for (const textMatch of responseBlock.matchAll(textPattern)) {
      try {
        const decoded = decodeHarString(textMatch[1]);
        if (decoded.length > longest.length) longest = decoded;
      } catch { /* ignore malformed response strings from unrelated entries */ }
    }
    if (!longest) continue;
    try { return JSON.parse(longest) as Record<string, unknown>; }
    catch { continue; }
  }
  return null;
}

async function fetchJson(template: RequestTemplate) {
  const upstream = await fetch(template.url, {
    method: template.method,
    headers: template.headers,
    body: template.method === "GET" || template.method === "HEAD" ? undefined : template.body,
    cache: "no-store",
    signal: AbortSignal.timeout(8000),
  });
  if (!upstream.ok) throw new Error(`上游接口返回 HTTP ${upstream.status}`);
  return upstream.json() as Promise<Record<string, unknown>>;
}

function normalizeFearGreed(payload: Record<string, unknown>) {
  const data = payload.data as FearGreedData | undefined;
  if (!data || typeof data.latestValue !== "number") throw new Error("恐贪接口响应缺少 data.latestValue");
  return {
    type: data.type,
    latestValue: data.latestValue,
    prevDayValue: data.prevDayValue,
    prevWeekValue: data.prevWeekValue,
    prevMonthValue: data.prevMonthValue,
    symbol: data.symbol,
    latestTimestamp: data.latestTimestamp,
    latestTime: data.latestTime,
    latestComparedValue: data.latestComparedValue,
    items: (data.items ?? []).flatMap((item) => (
      typeof item.timestamp === "number" && typeof item.value === "number" && typeof item.comparedValue === "number"
        ? [{
          timestamp: item.timestamp,
          value: item.value,
          comparedTimestamp: item.comparedTimestamp ?? item.timestamp,
          comparedValue: item.comparedValue,
        }]
        : []
    )),
    serverTime: typeof payload.serverTime === "number" ? payload.serverTime : undefined,
  };
}

function normalizeBreadth(payload: Record<string, unknown>) {
  const data = payload.upDownSummary as MarketBreadthData | undefined;
  if (!data || typeof data.up !== "number" || typeof data.flat !== "number" || typeof data.down !== "number") {
    throw new Error("市场宽度响应缺少 upDownSummary");
  }
  return {
    up: data.up,
    flat: data.flat,
    down: data.down,
    serverTime: typeof payload.serverTime === "number" ? payload.serverTime : undefined,
  };
}

type TradingViewScanResponse = {
  totalCount?: number;
  data?: Array<{ s?: string; d?: unknown[] }>;
};

const publicProviderCache = new Map<string, { expiresAt: number; promise: Promise<unknown> }>();

function cachedPublicProvider<T>(key: string, ttlMs: number, load: () => Promise<T>) {
  const existing = publicProviderCache.get(key);
  if (existing && existing.expiresAt > Date.now()) return existing.promise as Promise<T>;
  const promise = load().catch((error) => {
    publicProviderCache.delete(key);
    throw error;
  });
  publicProviderCache.set(key, { expiresAt: Date.now() + ttlMs, promise });
  return promise;
}

async function fetchTradingViewScan(body: Record<string, unknown>, market = "america") {
  const upstream = await fetch(`https://scanner.tradingview.com/${market}/scan`, {
    method: "POST",
    headers: { "Content-Type": "application/json", Accept: "application/json", "User-Agent": "Mozilla/5.0" },
    body: JSON.stringify(body),
    cache: "no-store",
    signal: AbortSignal.timeout(8000),
  });
  if (!upstream.ok) throw new Error(`TradingView 返回 HTTP ${upstream.status}`);
  return upstream.json() as Promise<TradingViewScanResponse>;
}

function tradingViewBase(filter: Array<Record<string, unknown>>) {
  return {
    filter: [{ left: "type", operation: "equal", right: "stock" }, { left: "is_primary", operation: "equal", right: true }, ...filter],
    options: { lang: "zh" },
    markets: ["america"],
    symbols: { query: { types: [] }, tickers: [] },
    columns: ["change"],
    range: [0, 1],
  };
}

async function fetchPublicBreadth() {
  const [up, flat, down] = await Promise.all([
    fetchTradingViewScan(tradingViewBase([{ left: "change", operation: "greater", right: 0 }])),
    fetchTradingViewScan(tradingViewBase([{ left: "change", operation: "equal", right: 0 }])),
    fetchTradingViewScan(tradingViewBase([{ left: "change", operation: "less", right: 0 }])),
  ]);
  return { up: up.totalCount ?? 0, flat: flat.totalCount ?? 0, down: down.totalCount ?? 0, serverTime: Date.now(), provider: "TradingView 主上市股票" };
}

async function fetchPublicIndices() {
  const payload = await fetchTradingViewScan({
    symbols: { tickers: ["TVC:DJI", "NASDAQ:IXIC", "SP:SPX"], query: { types: [] } },
    columns: ["name", "description", "close", "change", "change_abs"],
    range: [0, 3],
  }, "global");
  const symbolMap: Record<string, string> = { "TVC:DJI": ".DJI", "NASDAQ:IXIC": ".IXIC", "SP:SPX": ".SPX" };
  return (payload.data ?? []).flatMap((item) => {
    const latestPrice = Number(item.d?.[2]);
    const change = Number(item.d?.[4]);
    const symbol = item.s ? symbolMap[item.s] : undefined;
    return symbol && Number.isFinite(latestPrice) && Number.isFinite(change)
      ? [{ symbol, name: String(item.d?.[1] ?? item.d?.[0] ?? symbol), latestPrice, preClose: latestPrice - change, timestamp: Date.now() }]
      : [];
  });
}

async function fetchPublicUsFearGreed() {
  const upstream = await fetch("https://production.dataviz.cnn.io/index/fearandgreed/graphdata", {
    headers: {
      Accept: "application/json",
      Referer: "https://www.cnn.com/markets/fear-and-greed",
      "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/140.0 Safari/537.36",
    },
    cache: "no-store",
    signal: AbortSignal.timeout(8000),
  });
  if (!upstream.ok) throw new Error(`CNN 恐贪接口返回 HTTP ${upstream.status}`);
  const payload = await upstream.json() as {
    fear_and_greed?: { score?: number; timestamp?: string; previous_close?: number; previous_1_week?: number; previous_1_month?: number };
    fear_and_greed_historical?: { data?: Array<{ x?: number; y?: number }> };
    market_momentum_sp500?: { data?: Array<{ x?: number; y?: number }> };
  };
  const current = payload.fear_and_greed;
  if (typeof current?.score !== "number") throw new Error("CNN 恐贪接口缺少 score");
  const spByDay = new Map((payload.market_momentum_sp500?.data ?? []).flatMap((item) => (
    typeof item.x === "number" && typeof item.y === "number" ? [[new Date(item.x).toISOString().slice(0, 10), { timestamp: item.x, value: item.y }] as const] : []
  )));
  const items = (payload.fear_and_greed_historical?.data ?? []).flatMap((item) => {
    if (typeof item.x !== "number" || typeof item.y !== "number") return [];
    const compared = spByDay.get(new Date(item.x).toISOString().slice(0, 10));
    return compared ? [{ timestamp: item.x, value: item.y, comparedTimestamp: compared.timestamp, comparedValue: compared.value }] : [];
  });
  const latestTimestamp = current.timestamp ? Date.parse(current.timestamp) : Date.now();
  return {
    type: "US" as const,
    latestValue: current.score,
    prevDayValue: current.previous_close,
    prevWeekValue: current.previous_1_week,
    prevMonthValue: current.previous_1_month,
    symbol: ".SPX",
    latestTimestamp,
    latestTime: current.timestamp,
    latestComparedValue: items.at(-1)?.comparedValue,
    items,
    serverTime: Date.now(),
    provider: "CNN Fear & Greed",
  };
}

async function fetchPublicCryptoFearGreed() {
  const upstream = await fetch("https://api.alternative.me/fng/?limit=365&format=json", {
    headers: { Accept: "application/json", "User-Agent": "Mozilla/5.0" },
    cache: "no-store",
    signal: AbortSignal.timeout(8000),
  });
  if (!upstream.ok) throw new Error(`Alternative.me 返回 HTTP ${upstream.status}`);
  const payload = await upstream.json() as { data?: Array<{ value?: string; timestamp?: string }> };
  const points = (payload.data ?? []).flatMap((item) => {
    const value = Number(item.value);
    const timestamp = Number(item.timestamp) * 1000;
    return Number.isFinite(value) && Number.isFinite(timestamp) ? [{ value, timestamp }] : [];
  });
  if (!points.length) throw new Error("虚拟币恐贪接口没有有效数据");
  return {
    type: "CC" as const,
    latestValue: points[0].value,
    prevDayValue: points[1]?.value,
    prevWeekValue: points[7]?.value,
    prevMonthValue: points[30]?.value,
    latestTimestamp: points[0].timestamp,
    latestTime: new Date(points[0].timestamp).toISOString(),
    serverTime: Date.now(),
    provider: "Alternative.me",
  };
}

const getPublicBreadth = () => cachedPublicProvider("market-breadth", 15000, fetchPublicBreadth);
const getPublicIndices = () => cachedPublicProvider("market-indices", 5000, fetchPublicIndices);
const getPublicUsFearGreed = () => cachedPublicProvider("us-fear-greed", 30000, fetchPublicUsFearGreed);
const getPublicCryptoFearGreed = () => cachedPublicProvider("crypto-fear-greed", 300000, fetchPublicCryptoFearGreed);

function marketProxyPlugin(harPath: string): Plugin {
  let templatesPromise: Promise<{ indices: RequestTemplate; overview: RequestTemplate; usFear: RequestTemplate; cryptoFear: RequestTemplate }> | null = null;

  const loadTemplates = () => {
    templatesPromise ??= readFile(harPath, "utf8").then((raw) => {
      const indices = extractTemplate(raw, "/market/v2/indices");
      const overview = extractTemplate(raw, "/v2/market");
      const usFear = extractTemplate(raw, "/api/global/fear_greed_index", { type: "US" });
      const cryptoFear = extractTemplate(raw, "/api/global/fear_greed_index", { type: "CC" });
      if (!indices) throw new Error("HAR 中没有 /market/v2/indices 请求模板");
      if (!overview) throw new Error("HAR 中没有 /v2/market 请求模板");
      if (!usFear || !cryptoFear) throw new Error("HAR 中缺少 type=US 或 type=CC 的恐贪指数请求模板");
      return { indices, overview, usFear, cryptoFear };
    });
    return templatesPromise;
  };

  const attachMiddleware = (middlewares: Connect.Server) => {
    middlewares.use("/api/market/breadth", async (req, res, next) => {
      if (req.method !== "GET") return next();
      try {
        const templates = await loadTemplates();
        return jsonResponse(res, 200, { ...normalizeBreadth(await fetchJson(templates.overview)), provider: "Tiger HAR 授权接口" });
      } catch {
        try { return jsonResponse(res, 200, await getPublicBreadth()); }
        catch (error) { return jsonResponse(res, 502, { error: error instanceof Error ? error.message : "市场宽度代理请求失败" }); }
      }
    });

    middlewares.use("/api/market/fear-greed", async (req, res, next) => {
      if (req.method !== "GET") return next();
      const type = new URL(req.url ?? "", "http://localhost").searchParams.get("type") === "CC" ? "CC" : "US";
      try {
        const templates = await loadTemplates();
        const payload = await fetchJson(type === "CC" ? templates.cryptoFear : templates.usFear);
        return jsonResponse(res, 200, { ...normalizeFearGreed(payload), provider: "Tiger HAR 授权接口" });
      } catch {
        try { return jsonResponse(res, 200, type === "CC" ? await getPublicCryptoFearGreed() : await getPublicUsFearGreed()); }
        catch (error) { return jsonResponse(res, 502, { error: error instanceof Error ? error.message : "恐贪指数代理请求失败" }); }
      }
    });

    middlewares.use("/api/market/indices", async (req, res, next) => {
      if (req.method !== "GET") return next();
      try {
        const templates = await loadTemplates();
        const [indicesResult, overviewResult, usFearResult, cryptoFearResult] = await Promise.allSettled([
          fetchJson(templates.indices),
          fetchJson(templates.overview),
          fetchJson(templates.usFear),
          fetchJson(templates.cryptoFear),
        ]);
        if (indicesResult.status === "rejected") throw indicesResult.reason;
        const payload = indicesResult.value as {
          ret?: number;
          serverTime?: number;
          indices?: Array<{
            market?: string;
            marketStatus?: string;
            tradingStatus?: number;
            timeZone?: string;
            indices?: Array<{ symbol?: string; name?: string; latestPrice?: number; preClose?: number; timestamp?: number }>;
          }>;
        };
        const usMarket = payload.indices?.find((item) => item.market === "US");
        return jsonResponse(res, 200, {
          ret: payload.ret,
          serverTime: payload.serverTime,
          market: "US",
          marketStatus: usMarket?.marketStatus,
          tradingStatus: usMarket?.tradingStatus,
          timeZone: usMarket?.timeZone,
          indices: usMarket?.indices ?? [],
          upDownSummary: overviewResult.status === "fulfilled" ? normalizeBreadth(overviewResult.value) : undefined,
          fearGreedIndex: usFearResult.status === "fulfilled" ? normalizeFearGreed(usFearResult.value) : undefined,
          cryptoFearGreedIndex: cryptoFearResult.status === "fulfilled" ? normalizeFearGreed(cryptoFearResult.value) : undefined,
          partialErrors: [
            overviewResult.status === "rejected" ? `breadth: ${String(overviewResult.reason)}` : null,
            usFearResult.status === "rejected" ? `US: ${String(usFearResult.reason)}` : null,
            cryptoFearResult.status === "rejected" ? `CC: ${String(cryptoFearResult.reason)}` : null,
          ].filter(Boolean),
        });
      } catch {
        try {
          const [indices, breadth, fearGreedIndex, cryptoFearGreedIndex] = await Promise.all([
            getPublicIndices(),
            getPublicBreadth(),
            getPublicUsFearGreed(),
            getPublicCryptoFearGreed(),
          ]);
          return jsonResponse(res, 200, {
            serverTime: Date.now(),
            market: "US",
            indices,
            upDownSummary: breadth,
            fearGreedIndex,
            cryptoFearGreedIndex,
            provider: "public-fallback",
          });
        } catch (error) {
          return jsonResponse(res, 502, { error: error instanceof Error ? error.message : "本地行情代理请求失败" });
        }
      }
    });
  };

  return {
    name: "local-har-market-proxy",
    configureServer(server) { attachMiddleware(server.middlewares); },
    configurePreviewServer(server) { attachMiddleware(server.middlewares); },
  };
}

type StockResourceName = "detail" | "depth" | "depthHistory" | "trades" | "priceDistribution" | "funds" | "fundTrend" | "positionChange";
type StockResourceDefinition = { pathname: (symbol: string) => string; query?: Record<string, string> };
type StockResourceSource = "live" | "snapshot" | "unavailable";
type StockResourceResult = { payload: Record<string, any>; source: StockResourceSource; error?: string };

const stockResourceDefinitions: Record<StockResourceName, StockResourceDefinition> = {
  detail: { pathname: () => "/stock_info/detail" },
  depth: { pathname: (symbol) => `/stock_info/ask_bid/arca/${symbol}`, query: { props: "askBidDepth" } },
  depthHistory: { pathname: (symbol) => `/stock_info/ask_bid/arca/${symbol}`, query: { props: "askBidHist" } },
  trades: { pathname: (symbol) => `/stock_info/trade_tick/${symbol}`, query: { needStat: "1" } },
  priceDistribution: { pathname: (symbol) => `/stock_info/trade_price_list/${symbol}` },
  funds: { pathname: (symbol) => `/stock_info/fund_related/${symbol}`, query: { withPublicityFund: "1", withChipsDistribution: "1", withMainFundDeal: "1" } },
  fundTrend: { pathname: (symbol) => `/stock_info/fund_related/${symbol}`, query: { withFundFlowTrend: "1" } },
  positionChange: { pathname: (symbol) => `/stock_info/fund_related/${symbol}`, query: { withPositionChange: "1" } },
};

function specializeStockTemplate(template: RequestTemplate, symbol: string): RequestTemplate {
  const url = new URL(template.url);
  url.pathname = url.pathname.replace(/\/GPRO(?=\/|$)/g, `/${symbol}`);
  let body = template.body;
  if (body && template.method !== "GET" && template.method !== "HEAD") {
    try {
      const parsed = JSON.parse(body) as { items?: Array<Record<string, unknown>> };
      if (Array.isArray(parsed.items)) parsed.items = parsed.items.map((item) => ({ ...item, symbol }));
      body = JSON.stringify(parsed);
    } catch {
      body = body.replace(/GPRO/g, symbol);
    }
  }
  return { ...template, url: url.toString(), body };
}

function normalizeStockResources(symbol: string, resources: Record<StockResourceName, StockResourceResult>) {
  const detail = resources.detail.payload.items?.[0] ?? {};
  const depth = resources.depth.payload.askBidDepth ?? {};
  const depthHistory = resources.depthHistory.payload.askBidHist ?? {};
  const trades = Array.isArray(resources.trades.payload.items) ? resources.trades.payload.items : [];
  const funds = resources.funds.payload.data ?? {};
  const fundTrend = resources.fundTrend.payload.data?.fundFlowTrend ?? {};
  const positionChange = resources.positionChange.payload.data?.positionChange ?? {};
  const levels = (items: any[]) => (Array.isArray(items) ? items : []).flatMap((item) => (
    typeof item?.price === "number" && typeof item?.volume === "number"
      ? [{ price: item.price, volume: item.volume, orderCount: Array.isArray(item.subVolume) ? item.subVolume.length : 0 }]
      : []
  ));
  const priceLevels = (items: any[]) => (Array.isArray(items) ? items : []).flatMap((item) => (
    Array.isArray(item) && typeof item[0] === "number" && typeof item[1] === "number" ? [{ price: item[0], volume: item[1] }] : []
  ));
  const sourceValues = Object.values(resources).map((resource) => resource.source);
  const source = sourceValues.every((item) => item === "live")
    ? "live"
    : sourceValues.every((item) => item === "snapshot")
      ? "snapshot"
      : sourceValues.includes("unavailable")
        ? "partial"
        : "mixed";
  return {
    symbol,
    name: detail.nameCN ?? detail.nameEN ?? symbol,
    source,
    serverTime: Math.max(...Object.values(resources).map((resource) => Number(resource.payload.serverTime) || 0)),
    quote: {
      symbol: detail.symbol,
      market: detail.market,
      exchange: detail.exchange,
      latestPrice: detail.latestPrice,
      preClose: detail.preClose,
      change: detail.change,
      changeRate: detail.changeRate,
      open: detail.open,
      high: detail.high,
      low: detail.low,
      volume: detail.volume,
      amount: detail.amount,
      amplitude: detail.amplitude,
      volumeRatio: detail.volumeRatio,
      floatShares: detail.floatShares,
      shares: detail.shares,
      eps: detail.eps,
      ttmEps: detail.ttmEps,
      latestTime: detail.latestTime,
      hourTrading: detail.hourTrading,
      preHourTrading: detail.preHourTrading,
      postHourTrading: detail.postHourTrading,
    },
    orderBook: {
      source: "NYSE Arca · Level 2",
      timestamp: resources.depth.payload.timestamp ?? resources.depth.payload.serverTime,
      ask: levels(depth.ask),
      bid: levels(depth.bid),
      historicalAsk: priceLevels(depthHistory.ask),
      historicalBid: priceLevels(depthHistory.bid),
    },
    trades: trades.flatMap((item: any) => (
      typeof item?.time === "number" && typeof item?.price === "number" && typeof item?.volume === "number"
        ? [{ time: item.time, price: item.price, volume: item.volume, type: item.type ?? "*", condition: item.cond ?? "", session: item.part ?? "" }]
        : []
    )),
    tradeStats: Array.isArray(resources.trades.payload.stats) ? resources.trades.payload.stats : [],
    priceDistribution: (Array.isArray(resources.priceDistribution.payload.data) ? resources.priceDistribution.payload.data : []).flatMap((item: any) => (
      typeof item?.price === "number" && typeof item?.total === "number"
        ? [{ price: item.price, total: item.total, buy: item.buy ?? 0, sell: item.sell ?? 0, neutral: item.mid ?? 0, percent: item.percent ?? 0 }]
        : []
    )),
    publicityFund: funds.publicityFund ?? null,
    mainFundDeal: funds.mainFundDeal ?? null,
    chipsDistribution: funds.chipsDistribution ?? null,
    fundTrend: Array.isArray(fundTrend.items) ? fundTrend.items : [],
    positionChange: Array.isArray(positionChange.items) ? positionChange.items : [],
    availability: Object.fromEntries(Object.entries(resources).map(([name, resource]) => [name, { source: resource.source, error: resource.error ?? null }])),
  };
}

function stockProxyPlugin(harPath: string): Plugin {
  let resourcesPromise: Promise<Record<StockResourceName, { template: RequestTemplate; snapshot: Record<string, unknown> }>> | null = null;
  const loadResources = () => {
    resourcesPromise ??= readFile(harPath, "utf8").then((raw) => Object.fromEntries(Object.entries(stockResourceDefinitions).map(([name, definition]) => {
      const query = definition.query ?? {};
      const capturedPathname = definition.pathname("GPRO");
      const template = extractTemplate(raw, capturedPathname, query);
      const snapshot = extractResponsePayload(raw, capturedPathname, query);
      if (!template || !snapshot) throw new Error(`HAR 中缺少 ${name} 的请求或响应样本`);
      return [name, { template, snapshot }];
    })) as Record<StockResourceName, { template: RequestTemplate; snapshot: Record<string, unknown> }>);
    return resourcesPromise;
  };

  const attachMiddleware = (middlewares: Connect.Server) => {
    middlewares.use("/api/stock", async (req, res, next) => {
      if (req.method !== "GET") return next();
      if (!harPath) return jsonResponse(res, 503, { error: "STOCK_HAR_PATH 未配置" });
      try {
        const symbol = decodeURIComponent(new URL(req.url ?? "", "http://localhost").pathname.replace(/^\//, "")).toUpperCase();
        if (!/^[A-Z0-9.^-]{1,12}$/.test(symbol)) return jsonResponse(res, 400, { error: "股票代码格式无效" });
        const definitions = await loadResources();
        const entries = await Promise.all((Object.keys(definitions) as StockResourceName[]).map(async (name) => {
          const definition = definitions[name];
          try {
            return [name, { payload: await fetchJson(specializeStockTemplate(definition.template, symbol)), source: "live" as const }] as const;
          } catch (error) {
            return [name, {
              payload: symbol === "GPRO" ? definition.snapshot : {},
              source: symbol === "GPRO" ? "snapshot" as const : "unavailable" as const,
              error: error instanceof Error ? error.message : "上游请求失败",
            }] as const;
          }
        }));
        return jsonResponse(res, 200, normalizeStockResources(symbol, Object.fromEntries(entries) as Record<StockResourceName, StockResourceResult>));
      } catch (error) {
        return jsonResponse(res, 502, { error: error instanceof Error ? error.message : "个股接口聚合失败" });
      }
    });
  };
  return {
    name: "local-har-stock-proxy",
    configureServer(server) { attachMiddleware(server.middlewares); },
    configurePreviewServer(server) { attachMiddleware(server.middlewares); },
  };
}

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), "");
  return {
    plugins: [react(), marketProxyPlugin(env.MARKET_HAR_PATH ?? ""), stockProxyPlugin(env.STOCK_HAR_PATH ?? "")],
    server: { port: 4178 },
    preview: { port: 4178 },
  };
});
