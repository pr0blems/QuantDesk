import { useEffect, useMemo, useRef, useState } from "react";

export type RefreshableIndexQuote = {
  symbol: string;
  name: string;
  latest: number;
  previous: number;
  series: number[];
};

export type MarketBreadth = {
  up: number;
  flat: number;
  down: number;
  serverTime?: number;
};

export type FearGreedIndex = {
  latestValue: number;
  prevDayValue: number;
  prevWeekValue: number;
  prevMonthValue: number;
  type?: "US" | "CC";
  symbol?: string;
  latestTimestamp?: number;
  latestTime?: string;
  latestComparedValue?: number;
  serverTime?: number;
  items?: FearGreedHistoryPoint[];
};

export type FearGreedHistoryPoint = {
  timestamp: number;
  value: number;
  comparedTimestamp: number;
  comparedValue: number;
};

type ApiIndex = {
  symbol?: string;
  latestPrice?: number;
  preClose?: number;
  indices?: ApiIndex[];
};

type ApiThumbnail = {
  price?: number[];
};

type MarketApiPayload = {
  serverTime?: number;
  indices?: ApiIndex[];
  thumb?: Record<string, ApiThumbnail>;
  upDownSummary?: Partial<MarketBreadth>;
  fearGreedIndex?: Partial<FearGreedIndex>;
  cryptoFearGreedIndex?: Partial<FearGreedIndex>;
};

export type MarketRefreshState = {
  enabled: boolean;
  source: "snapshot" | "live";
  status: "snapshot" | "refreshing" | "live" | "paused" | "error";
  lastCheckedAt: number | null;
  lastChangedAt: number | null;
  latencyMs: number | null;
  checkCount: number;
  unchangedCount: number;
  retryDelayMs: number;
  breadthSource: "snapshot" | "live";
  fearGreedSource: "snapshot" | "live";
  cryptoFearGreedSource: "snapshot" | "live";
  error: string | null;
};

const ONE_SECOND = 1000;
const MAX_RETRY_DELAY = 15000;
const liveEndpoint = (import.meta.env.VITE_MARKET_DATA_URL ?? "").trim();
const liveConfigured = import.meta.env.VITE_MARKET_LIVE_ENABLED === "true" && liveEndpoint.length > 0;

function flattenIndices(indices: ApiIndex[] = []) {
  return indices.flatMap((item) => item.indices ?? [item]);
}

function mergeQuotes(current: RefreshableIndexQuote[], payload: MarketApiPayload, resetSeries: boolean) {
  const updates = new Map(flattenIndices(payload.indices).filter((item) => item.symbol).map((item) => [item.symbol as string, item]));
  let changed = false;
  const quotes = current.map((quote) => {
    const update = updates.get(quote.symbol);
    if (!update || typeof update.latestPrice !== "number") return quote;
    const latest = update.latestPrice;
    const previous = typeof update.preClose === "number" ? update.preClose : quote.previous;
    const remoteSeries = payload.thumb?.[quote.symbol]?.price?.filter((value) => Number.isFinite(value));
    const priceChanged = latest !== quote.latest;
    const series = remoteSeries?.length
      ? remoteSeries
      : resetSeries
        ? [latest]
        : [...quote.series.slice(-119), latest];
    if (priceChanged || previous !== quote.previous) changed = true;
    return { ...quote, latest, previous, series };
  });
  return { quotes, changed };
}

function mergeFearGreed(current: FearGreedIndex, update?: Partial<FearGreedIndex>) {
  if (!update) return current;
  return {
    latestValue: update.latestValue ?? current.latestValue,
    prevDayValue: update.prevDayValue ?? current.prevDayValue,
    prevWeekValue: update.prevWeekValue ?? current.prevWeekValue,
    prevMonthValue: update.prevMonthValue ?? current.prevMonthValue,
    type: update.type ?? current.type,
    symbol: update.symbol ?? current.symbol,
    latestTimestamp: update.latestTimestamp ?? current.latestTimestamp,
    latestTime: update.latestTime ?? current.latestTime,
    latestComparedValue: update.latestComparedValue ?? current.latestComparedValue,
    serverTime: update.serverTime ?? current.serverTime,
    items: update.items?.length ? update.items : current.items,
  };
}

export function useMarketRefresh(
  initialQuotes: RefreshableIndexQuote[],
  initialBreadth: MarketBreadth,
  initialFearGreed: FearGreedIndex,
  initialCryptoFearGreed: FearGreedIndex,
) {
  const [quotes, setQuotes] = useState(initialQuotes);
  const [breadth, setBreadth] = useState(initialBreadth);
  const [fearGreed, setFearGreed] = useState(initialFearGreed);
  const [cryptoFearGreed, setCryptoFearGreed] = useState(initialCryptoFearGreed);
  const [enabled, setEnabled] = useState(true);
  const [visible, setVisible] = useState(() => typeof document === "undefined" || !document.hidden);
  const [refreshNonce, setRefreshNonce] = useState(0);
  const quotesRef = useRef(initialQuotes);
  const breadthRef = useRef(initialBreadth);
  const hasReceivedLiveDataRef = useRef(false);
  const [state, setState] = useState<MarketRefreshState>({
    enabled: true,
    source: liveConfigured ? "live" : "snapshot",
    status: liveConfigured ? "refreshing" : "snapshot",
    lastCheckedAt: null,
    lastChangedAt: null,
    latencyMs: null,
    checkCount: 0,
    unchangedCount: 0,
    retryDelayMs: ONE_SECOND,
    breadthSource: "snapshot",
    fearGreedSource: "snapshot",
    cryptoFearGreedSource: "snapshot",
    error: null,
  });

  useEffect(() => {
    const handleVisibility = () => setVisible(!document.hidden);
    document.addEventListener("visibilitychange", handleVisibility);
    return () => document.removeEventListener("visibilitychange", handleVisibility);
  }, []);

  useEffect(() => {
    if (!enabled || !visible) {
      setState((current) => ({ ...current, enabled, status: "paused" }));
      return undefined;
    }

    let cancelled = false;
    let timeoutId: number | undefined;
    let failureCount = 0;
    let activeController: AbortController | null = null;

    const schedule = (delay: number, task: () => void) => {
      if (cancelled) return;
      timeoutId = window.setTimeout(task, delay);
    };

    const check = async () => {
      if (cancelled) return;
      const startedAt = Date.now();

      if (!liveConfigured) {
        setState((current) => ({
          ...current,
          enabled: true,
          source: "snapshot",
          status: "snapshot",
          lastCheckedAt: Date.now(),
          latencyMs: 0,
          checkCount: current.checkCount + 1,
          unchangedCount: current.unchangedCount + 1,
          retryDelayMs: ONE_SECOND,
          error: null,
        }));
        schedule(ONE_SECOND, check);
        return;
      }

      setState((current) => ({ ...current, enabled: true, status: "refreshing", error: null }));
      const requestController = new AbortController();
      activeController = requestController;
      const requestTimeoutId = window.setTimeout(() => requestController.abort(), 8000);
      try {
        const response = await fetch(liveEndpoint, { signal: requestController.signal, headers: { Accept: "application/json" }, cache: "no-store" });
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const payload = await response.json() as MarketApiPayload;
        const merged = mergeQuotes(quotesRef.current, payload, !hasReceivedLiveDataRef.current);
        hasReceivedLiveDataRef.current = true;
        quotesRef.current = merged.quotes;
        setQuotes(merged.quotes);
        let breadthChanged = false;
        if (payload.upDownSummary) {
          const nextBreadth = {
            up: payload.upDownSummary.up ?? breadthRef.current.up,
            flat: payload.upDownSummary.flat ?? breadthRef.current.flat,
            down: payload.upDownSummary.down ?? breadthRef.current.down,
            serverTime: payload.upDownSummary.serverTime ?? breadthRef.current.serverTime,
          };
          breadthChanged = nextBreadth.up !== breadthRef.current.up
            || nextBreadth.flat !== breadthRef.current.flat
            || nextBreadth.down !== breadthRef.current.down;
          breadthRef.current = nextBreadth;
          setBreadth(nextBreadth);
        }
        const dataChanged = merged.changed || breadthChanged;
        if (payload.fearGreedIndex) setFearGreed((current) => mergeFearGreed(current, payload.fearGreedIndex));
        if (payload.cryptoFearGreedIndex) setCryptoFearGreed((current) => mergeFearGreed(current, payload.cryptoFearGreedIndex));
        failureCount = 0;
        setState((current) => ({
          ...current,
          source: "live",
          status: "live",
          lastCheckedAt: Date.now(),
          lastChangedAt: dataChanged ? Date.now() : current.lastChangedAt,
          latencyMs: Date.now() - startedAt,
          checkCount: current.checkCount + 1,
          unchangedCount: dataChanged ? 0 : current.unchangedCount + 1,
          retryDelayMs: ONE_SECOND,
          breadthSource: payload.upDownSummary ? "live" : "snapshot",
          fearGreedSource: payload.fearGreedIndex ? "live" : "snapshot",
          cryptoFearGreedSource: payload.cryptoFearGreedIndex ? "live" : "snapshot",
          error: null,
        }));
        schedule(ONE_SECOND, check);
      } catch (error) {
        if (cancelled) return;
        failureCount += 1;
        const retryDelayMs = Math.min(MAX_RETRY_DELAY, ONE_SECOND * 2 ** Math.min(failureCount, 4));
        setState((current) => ({
          ...current,
          source: "live",
          status: "error",
          lastCheckedAt: Date.now(),
          latencyMs: Date.now() - startedAt,
          checkCount: current.checkCount + 1,
          retryDelayMs,
          error: error instanceof DOMException && error.name === "AbortError" ? "请求超时" : error instanceof Error ? error.message : "刷新失败",
        }));
        schedule(retryDelayMs, check);
      } finally {
        window.clearTimeout(requestTimeoutId);
        activeController = null;
      }
    };

    void check();
    return () => {
      cancelled = true;
      activeController?.abort();
      if (timeoutId !== undefined) window.clearTimeout(timeoutId);
    };
  }, [enabled, refreshNonce, visible]);

  const actions = useMemo(() => ({
    toggle: () => setEnabled((current) => !current),
    refreshNow: () => setRefreshNonce((current) => current + 1),
  }), []);

  return { quotes, breadth, fearGreed, cryptoFearGreed, state, actions, liveConfigured };
}
