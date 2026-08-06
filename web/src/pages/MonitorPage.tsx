import { useCallback, useEffect, useMemo, useState, type FormEvent } from "react";

import { monitorApi } from "../api/quantdesk";
import type { ApiObject, PredictionAlgorithmUpdate } from "../api/types";
import { DataTable, ErrorPanel, FormActions, JsonPreview, LoadingPanel, MetricCard, Notice, PageHeader, Panel, StatusPill, Tabs } from "../components/ui";
import { asList, asObject, compactJson, firstList, numberValue, stringList, stringValue } from "../utils/data";
import { formatDate, formatMoney } from "../utils/format";

type MonitorTab = "market" | "opportunities" | "alerts" | "predictions";

interface MonitorState {
  overview: ApiObject;
  breadth: ApiObject;
  intelligence: ApiObject;
  watchlist: ApiObject;
  opportunities: ApiObject;
  alerts: ApiObject;
  news: ApiObject;
}

function payloadRows(payload: ApiObject, ...keys: string[]) {
  const rows = firstList(payload, ...keys);
  return rows.length > 0 ? rows : asList(payload);
}

export function MonitorPage() {
  const [tab, setTab] = useState<MonitorTab>("market");
  const [state, setState] = useState<MonitorState | null>(null);
  const [selectedSymbol, setSelectedSymbol] = useState("");
  const [symbolDetail, setSymbolDetail] = useState<ApiObject | null>(null);
  const [history, setHistory] = useState<ApiObject | null>(null);
  const [algorithm, setAlgorithm] = useState<ApiObject | null>(null);
  const [watchlistDraft, setWatchlistDraft] = useState("");
  const [loading, setLoading] = useState(true);
  const [working, setWorking] = useState(false);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const [overview, breadth, intelligence, watchlist, opportunities, alerts, news] = await Promise.all([
        monitorApi.overview(),
        monitorApi.breadth(),
        monitorApi.intelligence(),
        monitorApi.watchlist(),
        monitorApi.opportunities(undefined, 60, true),
        monitorApi.alerts(),
        monitorApi.news(),
      ]);
      setState({ overview, breadth, intelligence, watchlist, opportunities, alerts, news });
      const symbols = stringList(watchlist.symbols);
      setWatchlistDraft(symbols.join("\n"));
      setSelectedSymbol((current) => current || symbols[0] || stringValue(overview.default_symbol, ""));
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "监控数据加载失败");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { void load(); }, [load]);

  const loadSymbol = useCallback(async (symbol: string) => {
    if (!symbol) return;
    setWorking(true);
    setError("");
    try {
      const [klines, score, report, opportunities] = await Promise.all([
        monitorApi.klines(symbol, "1h"),
        monitorApi.score(symbol),
        monitorApi.report(symbol),
        monitorApi.opportunities(symbol, 5, true),
      ]);
      setSymbolDetail({ klines, score, report, opportunities });
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "标的详情加载失败");
    } finally {
      setWorking(false);
    }
  }, []);

  useEffect(() => { if (selectedSymbol) void loadSymbol(selectedSymbol); }, [loadSymbol, selectedSymbol]);

  const overviewRows = useMemo(
    () => state ? payloadRows(state.overview, "items", "markets", "symbols", "contracts") : [],
    [state],
  );
  const opportunityRows = useMemo(
    () => state ? payloadRows(state.opportunities, "items", "opportunities") : [],
    [state],
  );
  const alertRows = useMemo(() => state ? payloadRows(state.alerts, "items", "alerts") : [], [state]);
  const newsRows = useMemo(() => state ? payloadRows(state.news, "items", "news") : [], [state]);

  async function saveWatchlist(event: FormEvent<HTMLFormElement>): Promise<void> {
    event.preventDefault();
    const symbols = Array.from(new Set(watchlistDraft.split(/[\s,，;；]+/).map((item) => item.trim().toUpperCase()).filter(Boolean)));
    setWorking(true);
    setError("");
    try {
      await monitorApi.saveWatchlist({ symbols });
      setMessage(`已保存 ${symbols.length} 个监控标的`);
      await load();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "自选列表保存失败");
    } finally {
      setWorking(false);
    }
  }

  async function markRead(): Promise<void> {
    setWorking(true);
    try {
      await monitorApi.markAlertsRead();
      setMessage("提醒已全部标记为已读");
      await load();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "提醒状态更新失败");
    } finally {
      setWorking(false);
    }
  }

  async function setPreference(publicId: string, action: "clear" | "ignore" | "watch"): Promise<void> {
    setWorking(true);
    try {
      await monitorApi.preference(publicId, { action, notify_enabled: action === "watch" });
      setMessage(action === "watch" ? "机会已加入关注" : action === "ignore" ? "机会已忽略" : "偏好已清除");
      await load();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "机会偏好更新失败");
    } finally {
      setWorking(false);
    }
  }

  async function openPredictions(): Promise<void> {
    setTab("predictions");
    setWorking(true);
    try {
      const [nextAlgorithm, nextHistory] = await Promise.all([
        monitorApi.predictionAlgorithm(),
        monitorApi.predictionHistory({ limit: 100 }),
      ]);
      setAlgorithm(nextAlgorithm);
      setHistory(nextHistory);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "预测数据加载失败");
    } finally {
      setWorking(false);
    }
  }

  async function saveAlgorithm(event: FormEvent<HTMLFormElement>): Promise<void> {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    try {
      const weightsText = stringValue(form.get("weights"), "{}");
      const parsedWeights: unknown = JSON.parse(weightsText);
      if (parsedWeights === null || typeof parsedWeights !== "object" || Array.isArray(parsedWeights)) throw new Error("权重必须是 JSON 对象");
      const input: PredictionAlgorithmUpdate = {
        direction_threshold: Number(form.get("direction_threshold")),
        min_data_quality: Number(form.get("min_data_quality")),
        account_crowding_penalty: Number(form.get("account_crowding_penalty")),
        funding_crowding_penalty: Number(form.get("funding_crowding_penalty")),
        weights: parsedWeights as PredictionAlgorithmUpdate["weights"],
      };
      setWorking(true);
      const result = await monitorApi.updatePredictionAlgorithm(input);
      setAlgorithm(result);
      setMessage("预测算法配置已保存");
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "预测算法保存失败");
    } finally {
      setWorking(false);
    }
  }

  const breadth = state ? asObject(state.breadth.summary ?? state.breadth) : {};
  const algorithmConfig = algorithm ? asObject(algorithm.config ?? algorithm) : {};

  return (
    <>
      <PageHeader
        eyebrow="MARKET INTELLIGENCE"
        title="市场监控"
        description="统一查看行情广度、自选合约、量化机会、提醒、新闻与预测算法。"
        actions={<button className="button button--secondary" type="button" onClick={() => void load()} disabled={loading || working}>刷新全部</button>}
      />
      <Tabs
        value={tab}
        onChange={(value) => value === "predictions" ? void openPredictions() : setTab(value)}
        label="监控模块"
        items={[
          { value: "market", label: "市场总览" },
          { value: "opportunities", label: "机会雷达" },
          { value: "alerts", label: "提醒与新闻" },
          { value: "predictions", label: "预测评估" },
        ]}
      />
      {loading && !state ? <LoadingPanel label="正在汇总市场数据…" /> : null}
      {error ? <ErrorPanel message={error} onRetry={() => void load()} /> : null}
      {message ? <Notice tone="success">{message}</Notice> : null}

      {state && tab === "market" ? (
        <>
          <section className="metric-grid">
            <MetricCard label="上涨标的" value={numberValue(breadth.advancing ?? breadth.up_count)} note="当前市场广度" tone="success" />
            <MetricCard label="下跌标的" value={numberValue(breadth.declining ?? breadth.down_count)} note="当前市场广度" tone="danger" />
            <MetricCard label="自选数量" value={stringList(state.watchlist.symbols).length} note="用户独立监控列表" />
            <MetricCard label="未读提醒" value={numberValue(state.alerts.unread_count)} note="可批量标记已读" tone="warning" />
          </section>
          <section className="two-column-layout">
            <Panel eyebrow="MARKET GRID" title="行情与评分">
              <DataTable
                rows={overviewRows}
                columns={[
                  { key: "symbol", label: "标的", render: (row) => <button className="link-button" type="button" onClick={() => setSelectedSymbol(stringValue(row.symbol, ""))}>{stringValue(row.symbol)}</button> },
                  { key: "price", label: "价格", render: (row) => formatMoney(numberValue(row.price ?? row.last_price), "") },
                  { key: "change", label: "涨跌", render: (row) => `${numberValue(row.change_pct ?? row.price_change_pct).toFixed(2)}%` },
                  { key: "score", label: "评分", render: (row) => stringValue(row.score ?? row.total_score) },
                  { key: "signal", label: "信号", render: (row) => <StatusPill>{stringValue(row.signal ?? row.direction)}</StatusPill> },
                ]}
                empty="暂无行情快照"
              />
            </Panel>
            <Panel eyebrow="WATCHLIST" title="自选列表">
              <form className="stack-form" onSubmit={(event) => void saveWatchlist(event)}>
                <label><span>合约代码</span><textarea rows={12} value={watchlistDraft} onChange={(event) => setWatchlistDraft(event.target.value)} placeholder="BTCUSDT\nETHUSDT" /></label>
                <FormActions><button className="button button--primary" type="submit" disabled={working}>保存自选</button></FormActions>
              </form>
            </Panel>
          </section>
          {selectedSymbol ? (
            <Panel eyebrow="SYMBOL DETAIL" title={`${selectedSymbol} · 量化详情`} actions={<button className="button button--secondary" type="button" onClick={() => void loadSymbol(selectedSymbol)}>刷新标的</button>}>
              {working && !symbolDetail ? <LoadingPanel label="正在读取标的因子…" /> : symbolDetail ? <JsonPreview value={symbolDetail} label="K 线、评分、报告与机会详情" /> : null}
            </Panel>
          ) : null}
        </>
      ) : null}

      {state && tab === "opportunities" ? (
        <Panel eyebrow="OPPORTUNITY RADAR" title="策略机会">
          <DataTable
            rows={opportunityRows}
            columns={[
              { key: "symbol", label: "标的" },
              { key: "direction", label: "方向", render: (row) => <StatusPill tone={stringValue(row.direction) === "long" ? "success" : "warning"}>{stringValue(row.direction)}</StatusPill> },
              { key: "score", label: "评分", render: (row) => stringValue(row.score ?? row.confidence) },
              { key: "reason", label: "依据", render: (row) => stringValue(row.summary ?? row.reason ?? row.thesis) },
              { key: "created_at", label: "时间", render: (row) => formatDate(stringValue(row.created_at ?? row.observed_at, "")) },
              { key: "actions", label: "操作", render: (row) => {
                const id = stringValue(row.public_id ?? row.id, "");
                return <div className="inline-actions"><button type="button" onClick={() => void setPreference(id, "watch")} disabled={!id || working}>关注</button><button type="button" onClick={() => void setPreference(id, "ignore")} disabled={!id || working}>忽略</button><button type="button" onClick={() => void setPreference(id, "clear")} disabled={!id || working}>清除</button></div>;
              } },
            ]}
            empty="暂无量化机会"
          />
        </Panel>
      ) : null}

      {state && tab === "alerts" ? (
        <section className="two-column-layout">
          <Panel eyebrow="ALERTS" title="监控提醒" actions={<button className="button button--secondary" type="button" onClick={() => void markRead()} disabled={working}>全部已读</button>}>
            <DataTable rows={alertRows} columns={[
              { key: "symbol", label: "标的" },
              { key: "kind", label: "类型" },
              { key: "message", label: "内容" },
              { key: "ts", label: "时间", render: (row) => formatDate(stringValue(row.created_at ?? row.ts, "")) },
            ]} empty="暂无提醒" />
          </Panel>
          <Panel eyebrow="NEWS" title="市场新闻">
            <DataTable rows={newsRows} columns={[
              { key: "source", label: "来源" },
              { key: "title", label: "标题", render: (row) => stringValue(row.title_zh ?? row.title) },
              { key: "sentiment", label: "情绪" },
              { key: "ts", label: "时间", render: (row) => formatDate(stringValue(row.created_at ?? row.ts, "")) },
            ]} empty="暂无新闻" />
          </Panel>
        </section>
      ) : null}

      {tab === "predictions" ? (
        <section className="two-column-layout">
          <Panel eyebrow="ALGORITHM" title="预测算法参数">
            {algorithm ? (
              <form className="form-grid" onSubmit={(event) => void saveAlgorithm(event)}>
                <label><span>方向阈值</span><input name="direction_threshold" type="number" min="0.05" max="0.5" step="0.01" defaultValue={numberValue(algorithmConfig.direction_threshold, 0.15)} /></label>
                <label><span>最小数据质量</span><input name="min_data_quality" type="number" min="0.5" max="1" step="0.01" defaultValue={numberValue(algorithmConfig.min_data_quality, 0.7)} /></label>
                <label><span>账户拥挤惩罚</span><input name="account_crowding_penalty" type="number" min="0" max="0.5" step="0.01" defaultValue={numberValue(algorithmConfig.account_crowding_penalty, 0.1)} /></label>
                <label><span>资金费率惩罚</span><input name="funding_crowding_penalty" type="number" min="0" max="0.5" step="0.01" defaultValue={numberValue(algorithmConfig.funding_crowding_penalty, 0.1)} /></label>
                <label className="form-grid__wide"><span>周期权重 JSON</span><textarea name="weights" rows={12} defaultValue={compactJson(algorithmConfig.weights ?? {})} /></label>
                <FormActions><button className="button button--primary" type="submit" disabled={working}>保存算法</button></FormActions>
              </form>
            ) : <LoadingPanel label="正在读取预测算法…" />}
          </Panel>
          <Panel eyebrow="HISTORY" title="预测历史与统计">
            {history ? (
              <>
                <DataTable rows={payloadRows(history, "items", "predictions")} columns={[
                  { key: "symbol", label: "标的" },
                  { key: "timeframe", label: "周期" },
                  { key: "direction", label: "方向" },
                  { key: "confidence", label: "置信度" },
                  { key: "status", label: "状态" },
                  { key: "created_at", label: "时间", render: (row) => formatDate(stringValue(row.created_at ?? row.predicted_at, "")) },
                ]} empty="暂无预测样本" />
                <JsonPreview value={history.statistics ?? history.summary ?? {}} label="预测统计" />
              </>
            ) : <LoadingPanel label="正在读取预测历史…" />}
          </Panel>
        </section>
      ) : null}
      {state ? <JsonPreview value={state.intelligence} label="市场情报原始证据" /> : null}
    </>
  );
}
