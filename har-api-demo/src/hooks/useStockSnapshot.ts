import { useEffect, useRef, useState } from "react";

export type StockTrade = {
  time: number;
  price: number;
  volume: number;
  type: string;
  condition: string;
  session: string;
};

export type StockDepthLevel = {
  price: number;
  volume: number;
  orderCount: number;
};

export type StockSnapshot = {
  symbol: string;
  name: string;
  source: "live" | "snapshot" | "mixed" | "partial";
  serverTime: number;
  quote: {
    symbol?: string;
    market?: string;
    exchange?: string;
    latestPrice?: number;
    preClose?: number;
    change?: number;
    changeRate?: number;
    open?: number;
    high?: number;
    low?: number;
    volume?: number;
    amount?: number;
    amplitude?: number;
    volumeRatio?: number;
    floatShares?: number;
    shares?: number;
    eps?: number;
    ttmEps?: number;
    latestTime?: string;
    hourTrading?: {
      tag?: string;
      latestPrice?: number;
      preClose?: number;
      latestTime?: string;
      volume?: number;
      amount?: number;
      change?: number;
      changeRate?: number;
      amplitude?: number;
    };
  };
  orderBook: {
    source: string;
    timestamp: number;
    ask: StockDepthLevel[];
    bid: StockDepthLevel[];
    historicalAsk: Array<{ price: number; volume: number }>;
    historicalBid: Array<{ price: number; volume: number }>;
  };
  trades: StockTrade[];
  tradeStats: Array<{ timestamp: number; upVol: number; downVol: number; lastVol: number }>;
  priceDistribution: Array<{ price: number; total: number; buy: number; sell: number; neutral: number; percent: number }>;
  publicityFund: null | {
    cashFlowStat: { inflow: number; outflow: number; netflow: number; bigNetflow: number; medianNetflow: number; smallNetflow: number };
    cashFlowList: Array<{ color: string; name: string; amount: number; count: string; id: string; percent: string }>;
  };
  mainFundDeal: null | { items: Array<{ time: string; amount: number; mainAmountPercent: number; suctionCost: number; shipmentCost: number; netSales: number; price: number }> };
  chipsDistribution: null | {
    symbol: string;
    date: string;
    support: number;
    pressure: number;
    avgPrice: number;
    cumPdfList: Array<{ price: number; lot: number }>;
  };
  fundTrend: Array<{ amount: number; time: number }>;
  positionChange: Array<{ date: string; change: string }>;
  availability: Record<string, { source: "live" | "snapshot" | "unavailable"; error: string | null }>;
};

type StockSnapshotState = {
  data: StockSnapshot | null;
  status: "idle" | "loading" | "live" | "snapshot" | "mixed" | "partial" | "error";
  error: string | null;
  lastCheckedAt: number | null;
  lastDataChangedAt: number | null;
  checkCount: number;
  unchangedCount: number;
};

const endpoint = (import.meta.env.VITE_STOCK_DATA_URL ?? "/api/stock").trim().replace(/\/$/, "");

export function useStockSnapshot(symbol: string, enabled: boolean) {
  const [state, setState] = useState<StockSnapshotState>({ data: null, status: "idle", error: null, lastCheckedAt: null, lastDataChangedAt: null, checkCount: 0, unchangedCount: 0 });
  const chipsFingerprint = useRef<string | null>(null);

  useEffect(() => {
    if (!enabled) return undefined;
    chipsFingerprint.current = null;
    setState({ data: null, status: "loading", error: null, lastCheckedAt: null, lastDataChangedAt: null, checkCount: 0, unchangedCount: 0 });
    let cancelled = false;
    let timeoutId: number | undefined;
    let controller: AbortController | null = null;

    const load = async () => {
      controller = new AbortController();
      const abortId = window.setTimeout(() => controller?.abort(), 10000);
      setState((current) => ({ ...current, status: current.data ? current.status : "loading", error: null }));
      try {
        const response = await fetch(`${endpoint}/${encodeURIComponent(symbol)}`, { headers: { Accept: "application/json" }, cache: "no-store", signal: controller.signal });
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const data = await response.json() as StockSnapshot;
        if (cancelled) return;
        const nextFingerprint = JSON.stringify({
          date: data.chipsDistribution?.date ?? null,
          support: data.chipsDistribution?.support ?? null,
          pressure: data.chipsDistribution?.pressure ?? null,
          avgPrice: data.chipsDistribution?.avgPrice ?? null,
          points: data.chipsDistribution?.cumPdfList ?? [],
        });
        const changed = chipsFingerprint.current !== nextFingerprint;
        chipsFingerprint.current = nextFingerprint;
        const checkedAt = Date.now();
        setState((current) => ({
          data,
          status: data.source,
          error: null,
          lastCheckedAt: checkedAt,
          lastDataChangedAt: changed ? checkedAt : current.lastDataChangedAt,
          checkCount: current.checkCount + 1,
          unchangedCount: changed ? 0 : current.unchangedCount + 1,
        }));
      } catch (error) {
        if (cancelled) return;
        setState((current) => ({
          ...current,
          status: current.data ? current.status : "error",
          error: error instanceof DOMException && error.name === "AbortError" ? "请求超时" : error instanceof Error ? error.message : "请求失败",
          lastCheckedAt: Date.now(),
        }));
      } finally {
        window.clearTimeout(abortId);
        controller = null;
        if (!cancelled) timeoutId = window.setTimeout(load, 2000);
      }
    };

    void load();
    return () => {
      cancelled = true;
      controller?.abort();
      if (timeoutId !== undefined) window.clearTimeout(timeoutId);
    };
  }, [enabled, symbol]);

  return state;
}
