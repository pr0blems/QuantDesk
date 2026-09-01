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
  error: string | null;
};

const ONE_SECOND = 1000;
const MAX_RETRY_DELAY = 15000;
const liveEndpoint = (import.meta.env.VITE_MARKET_DATA_URL ?? "").trim();
const liveConfigured = import.meta.env.VITE_MARKET_LIVE_ENABLED === "true" && liveEndpoint.length > 0;

function flattenIndices(indices: ApiIndex[] = []) {
  return indices.flatMap((item) => item.indices ?? [item]);
}

function mergeQuotes(current: RefreshableIndexQuote[], payload: MarketApiPayload) {
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
      : priceChanged
        ? [...quote.series.slice(-78), latest]
        : quote.series;
    if (priceChanged || previous !== quote.previous || series !== quote.series) changed = true;
    return { ...quote, latest, previous, series };
  });
  return { quotes, changed };
}

export function useMarketRefresh(initialQuotes: RefreshableIndexQuote[], initialBreadth: MarketBreadth) {
  const [quotes, setQuotes] = useState(initialQuotes);
  const [breadth, setBreadth] = useState(initialBreadth);
  const [enabled, setEnabled] = useState(true);
  const [visible, setVisible] = useState(() => typeof document === "undefined" || !document.hidden);
  const [refreshNonce, setRefreshNonce] = useState(0);
  const quotesRef = useRef(initialQuotes);
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
        const merged = mergeQuotes(quotesRef.current, payload);
        const dataChanged = merged.changed;
        quotesRef.current = merged.quotes;
        setQuotes(merged.quotes);
        if (payload.upDownSummary) {
          setBreadth((current) => ({
            up: payload.upDownSummary?.up ?? current.up,
            flat: payload.upDownSummary?.flat ?? current.flat,
            down: payload.upDownSummary?.down ?? current.down,
          }));
        }
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

  return { quotes, breadth, state, actions, liveConfigured };
}
