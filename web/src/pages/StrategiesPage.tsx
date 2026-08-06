import { useCallback, useEffect, useMemo, useState, type FormEvent } from "react";

import { strategyApi } from "../api/quantdesk";
import type {
  ApiObject,
  Strategy,
  StrategyCreateRequest,
  StrategyDeploymentsResponse,
  StrategyListResponse,
  StrategyUpdateRequest,
} from "../api/types";
import {
  DataTable,
  EmptyState,
  ErrorPanel,
  FormActions,
  JsonPreview,
  LoadingPanel,
  MetricCard,
  Notice,
  PageHeader,
  Panel,
  StatusPill,
  Tabs,
} from "../components/ui";
import { asObject, firstList, numberValue, parseJsonObject, stringValue } from "../utils/data";
import { formatDate } from "../utils/format";

type StrategyTab = "catalog" | "editor" | "detail" | "ai";
type StrategyView = { catalog: StrategyListResponse; deployments: StrategyDeploymentsResponse };

const defaultIndicators = JSON.stringify([
  { key: "ema_cross", weight: 1, parameters: { fast: 12, slow: 26 } },
  { key: "rsi", weight: 1, parameters: { period: 14, oversold: 30, overbought: 70 } },
], null, 2);

function strategyTone(status: string): "success" | "warning" | "neutral" {
  if (status === "active") return "success";
  if (status === "paused" || status === "draft") return "warning";
  return "neutral";
}

function numberRecord(value: unknown): Record<string, number> {
  const result: Record<string, number> = {};
  for (const [key, item] of Object.entries(asObject(value))) {
    const parsed = typeof item === "number" ? item : Number(item);
    if (Number.isFinite(parsed)) result[key] = parsed;
  }
  return result;
}

export function StrategiesPage() {
  const [tab, setTab] = useState<StrategyTab>("catalog");
  const [view, setView] = useState<StrategyView | null>(null);
  const [selectedId, setSelectedId] = useState("");
  const [detail, setDetail] = useState<ApiObject | null>(null);
  const [revisions, setRevisions] = useState<ApiObject | null>(null);
  const [signals, setSignals] = useState<ApiObject | null>(null);
  const [indicatorCatalog, setIndicatorCatalog] = useState<ApiObject | null>(null);
  const [aiPreview, setAiPreview] = useState<ApiObject | null>(null);
  const [loading, setLoading] = useState(true);
  const [working, setWorking] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");

  const [createName, setCreateName] = useState("");
  const [createDescription, setCreateDescription] = useState("");
  const [createCategory, setCreateCategory] = useState("自定义");
  const [templateKey, setTemplateKey] = useState("");
  const [timeframe, setTimeframe] = useState<"15m" | "1h" | "4h">("1h");
  const [threshold, setThreshold] = useState(60);
  const [validBars, setValidBars] = useState(2);
  const [indicatorJson, setIndicatorJson] = useState(defaultIndicators);
  const [parameterJson, setParameterJson] = useState("{}");
  const [riskJson, setRiskJson] = useState('{"max_position_pct":10,"stop_loss_pct":2}');
  const [aiPrompt, setAiPrompt] = useState("");

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const [catalog, deployments, nextSignals, nextIndicators] = await Promise.all([
        strategyApi.list(),
        strategyApi.deployments(),
        strategyApi.signals(100),
        strategyApi.indicators(),
      ]);
      setView({ catalog, deployments });
      setSignals(nextSignals);
      setIndicatorCatalog(nextIndicators);
      setSelectedId((current) => catalog.items.some((item) => item.public_id === current) ? current : catalog.items[0]?.public_id ?? "");
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "策略数据加载失败");
    } finally {
      setLoading(false);
    }
  }, []);

  const loadDetail = useCallback(async (publicId: string) => {
    if (!publicId) {
      setDetail(null);
      setRevisions(null);
      return;
    }
    setWorking(true);
    setError("");
    try {
      const [nextDetail, nextRevisions] = await Promise.all([
        strategyApi.detail(publicId),
        strategyApi.revisions(publicId),
      ]);
      setDetail(nextDetail);
      setRevisions(nextRevisions);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "策略详情读取失败");
    } finally {
      setWorking(false);
    }
  }, []);

  useEffect(() => { void load(); }, [load]);
  useEffect(() => { if (selectedId) void loadDetail(selectedId); }, [loadDetail, selectedId]);

  const selected = useMemo(
    () => view?.catalog.items.find((item) => item.public_id === selectedId) ?? null,
    [selectedId, view],
  );
  const activeStrategies = view?.catalog.items.filter((item) => item.status === "active").length ?? 0;
  const runningDeployments = view?.deployments.items.filter((item) => item.status === "running").length ?? 0;

  async function perform(action: () => Promise<unknown>, success: string, reload = true): Promise<void> {
    setWorking(true);
    setError("");
    setNotice("");
    try {
      await action();
      setNotice(success);
      if (reload) await load();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "操作失败");
    } finally {
      setWorking(false);
    }
  }

  async function createStrategy(event: FormEvent): Promise<void> {
    event.preventDefault();
    let indicators: StrategyCreateRequest["indicators"];
    try {
      if (!templateKey) {
        const parsed: unknown = JSON.parse(indicatorJson);
        if (!Array.isArray(parsed) || parsed.length < 2) throw new Error("组合策略至少需要两个指标");
        indicators = parsed as StrategyCreateRequest["indicators"];
      }
      const input: StrategyCreateRequest = {
        name: createName.trim(),
        description: createDescription.trim(),
        category: createCategory.trim() || "自定义",
        template_key: templateKey || null,
        ...(indicators === undefined ? {} : { indicators }),
        timeframe,
        directions: ["long", "short"],
        confirmation_threshold: threshold,
        signal_valid_bars: validBars,
        parameters: parseJsonObject(parameterJson, "参数"),
        risk_defaults: parseJsonObject(riskJson, "风险默认值"),
      };
      await perform(async () => {
        const created = await strategyApi.create(input);
        const publicId = stringValue(created.public_id ?? asObject(created.item).public_id);
        if (publicId) setSelectedId(publicId);
      }, "策略已创建");
      setTab("catalog");
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "策略表单无效");
    }
  }

  async function updateSelected(event: FormEvent<HTMLFormElement>): Promise<void> {
    event.preventDefault();
    if (!selected) return;
    const values = new FormData(event.currentTarget);
    try {
      const input: StrategyUpdateRequest = {
        name: stringValue(values.get("name"), selected.name),
        description: stringValue(values.get("description")),
        category: stringValue(values.get("category"), selected.category),
        parameters: parseJsonObject(stringValue(values.get("parameters"), "{}"), "参数"),
        risk_defaults: parseJsonObject(stringValue(values.get("risk_defaults"), "{}"), "风险默认值"),
        version: selected.version,
      };
      await perform(() => strategyApi.update(selected.public_id, input), "策略新版本已保存");
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "参数 JSON 无效");
    }
  }

  async function previewAi(event: FormEvent): Promise<void> {
    event.preventDefault();
    if (!aiPrompt.trim()) return;
    setWorking(true);
    setError("");
    setAiPreview(null);
    try {
      const response = selected
        ? await strategyApi.aiPreview(selected.public_id, { prompt: aiPrompt.trim() })
        : await strategyApi.compositionPreview({ prompt: aiPrompt.trim() });
      setAiPreview(response);
      setNotice("AI 变更草案已生成，尚未写入策略");
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "AI 草案生成失败");
    } finally {
      setWorking(false);
    }
  }

  async function applyAi(): Promise<void> {
    if (!selected || !aiPreview) return;
    const proposed = asObject(aiPreview.proposed);
    await perform(
      () => strategyApi.aiApply(selected.public_id, {
        base_version: numberValue(aiPreview.base_version, selected.version),
        proposed: {
          name: stringValue(proposed.name, selected.name),
          description: stringValue(proposed.description, selected.description),
          category: stringValue(proposed.category, selected.category),
          parameters: numberRecord(proposed.parameters),
          risk_defaults: numberRecord(proposed.risk_defaults),
        },
      }),
      "AI 草案已应用为新的策略版本",
    );
    setAiPreview(null);
  }

  function useAiDraftForCreate(): void {
    if (!aiPreview) return;
    const draft = asObject(aiPreview.draft ?? aiPreview.proposed);
    setCreateName(stringValue(draft.name, createName));
    setCreateDescription(stringValue(draft.description, createDescription));
    setCreateCategory(stringValue(draft.category, createCategory));
    const draftTimeframe = stringValue(draft.timeframe, timeframe);
    if (draftTimeframe === "15m" || draftTimeframe === "1h" || draftTimeframe === "4h") setTimeframe(draftTimeframe);
    setThreshold(numberValue(draft.confirmation_threshold, threshold));
    setValidBars(numberValue(draft.signal_valid_bars, validBars));
    if (Array.isArray(draft.indicators)) setIndicatorJson(JSON.stringify(draft.indicators, null, 2));
    if (draft.parameters) setParameterJson(JSON.stringify(draft.parameters, null, 2));
    if (draft.risk_defaults) setRiskJson(JSON.stringify(draft.risk_defaults, null, 2));
    setTab("editor");
    setNotice("AI 组合草案已带入创建表单，请核对后再保存");
  }

  function startEdit(item: Strategy): void {
    setSelectedId(item.public_id);
    setTab("detail");
  }

  return (
    <>
      <PageHeader
        eyebrow="STRATEGY REGISTRY"
        title="策略中心"
        description="创建模板副本或指标组合，管理版本、参数、验证结果、信号和 AI 变更草案。"
        actions={<button className="button button--secondary" type="button" onClick={() => void load()} disabled={loading || working}>刷新策略</button>}
      />
      <Tabs value={tab} onChange={setTab} label="策略功能" items={[
        { value: "catalog", label: "策略目录" },
        { value: "editor", label: "创建策略" },
        { value: "detail", label: "版本与参数" },
        { value: "ai", label: "AI 辅助" },
      ]} />
      {notice ? <Notice tone="success">{notice}</Notice> : null}
      {error ? <ErrorPanel message={error} onRetry={() => void load()} /> : null}
      {loading && !view ? <LoadingPanel label="正在读取个人策略…" /> : null}

      {view ? <section className="metric-grid metric-grid--three" aria-label="策略统计">
        <MetricCard label="个人策略" value={view.catalog.items.length} note={`启用上限 ${view.catalog.limits.max_active_strategies}`} />
        <MetricCard label="当前启用" value={activeStrategies} note="可绑定模拟或实盘账户" tone="success" />
        <MetricCard label="运行部署" value={runningDeployments} note={`${view.deployments.items.length} 个部署记录`} tone={runningDeployments ? "info" : "neutral"} />
      </section> : null}

      {view && tab === "catalog" ? <section className="strategy-layout">
        <Panel eyebrow="MY STRATEGIES" title="策略目录" actions={<button className="button button--primary button--small" type="button" onClick={() => setTab("editor")}>创建策略</button>}>
          {view.catalog.items.length === 0 ? <EmptyState title="尚无个人策略" description="从模板复制，或创建至少包含两个指标的组合策略。" /> : <div className="strategy-list">
            {view.catalog.items.map((item) => <article className={`strategy-card${selectedId === item.public_id ? " strategy-card--selected" : ""}`} key={item.public_id}>
              <div className="strategy-card__top"><span className="strategy-card__index">v{item.version}</span><StatusPill tone={strategyTone(item.status)}>{item.status}</StatusPill></div>
              <h3>{item.name}</h3><p>{item.description || "尚未填写策略说明。"}</p>
              <dl><div><dt>类别</dt><dd>{item.category}</dd></div><div><dt>引擎</dt><dd>{item.engine_key}</dd></div><div><dt>生命周期</dt><dd>{item.lifecycle_status}</dd></div><div><dt>风险等级</dt><dd>{item.risk_level}</dd></div></dl>
              <FormActions>
                <button className="button button--secondary button--small" type="button" onClick={() => startEdit(item)}>详情 / 编辑</button>
                <button className="button button--secondary button--small" type="button" disabled={working} onClick={() => void perform(() => strategyApi.validate(item.public_id), "策略验证完成")}>验证</button>
                <button className="button button--danger button--small" type="button" disabled={working || item.status === "archived"} onClick={() => { if (window.confirm(`确认归档策略“${item.name}”？`)) void perform(() => strategyApi.archive(item.public_id), "策略已归档"); }}>归档</button>
              </FormActions>
              <footer><span>{item.is_default ? "系统模板副本" : "用户策略"}</span><time>{formatDate(item.updated_at)}</time></footer>
            </article>)}
          </div>}
        </Panel>
        <Panel eyebrow="DEPLOYMENTS" title="部署状态">
          {view.deployments.items.length === 0 ? <EmptyState title="暂无部署" description="绑定模拟或实盘账户后，部署状态会显示在这里。" /> : <div className="deployment-list">{view.deployments.items.map((deployment) => <div key={deployment.id}><span className={`run-dot run-dot--${deployment.status}`} /><p><strong>{deployment.name}</strong><small>{deployment.mode} · {deployment.status}</small></p><time>{formatDate(deployment.updated_at)}</time></div>)}</div>}
        </Panel>
      </section> : null}

      {view && tab === "editor" ? <div className="two-column-layout"><Panel eyebrow="CREATE" title="创建可执行策略">
        <form className="stack-form panel-form" onSubmit={(event) => void createStrategy(event)}>
          <div className="form-grid">
            <label><span>策略名称</span><input value={createName} onChange={(event) => setCreateName(event.target.value)} required maxLength={80} /></label>
            <label><span>类别</span><input value={createCategory} onChange={(event) => setCreateCategory(event.target.value)} /></label>
            <label><span>模板（可选）</span><select value={templateKey} onChange={(event) => setTemplateKey(event.target.value)}><option value="">自定义指标组合</option>{view.catalog.templates.map((template) => <option key={template.template_key} value={template.template_key}>{template.name}</option>)}</select></label>
            <label><span>周期</span><select value={timeframe} onChange={(event) => setTimeframe(event.target.value as typeof timeframe)}><option>15m</option><option>1h</option><option>4h</option></select></label>
            <label><span>确认阈值</span><input type="number" min="1" max="100" value={threshold} onChange={(event) => setThreshold(Number(event.target.value))} /></label>
            <label><span>信号有效 K 线数</span><input type="number" min="1" max="20" value={validBars} onChange={(event) => setValidBars(Number(event.target.value))} /></label>
          </div>
          <label><span>描述</span><textarea rows={3} value={createDescription} onChange={(event) => setCreateDescription(event.target.value)} /></label>
          {!templateKey ? <label><span>指标组合 JSON（至少两个）</span><textarea className="code-input" rows={10} value={indicatorJson} onChange={(event) => setIndicatorJson(event.target.value)} /></label> : <Notice>选择模板时会复制其可执行配置；下方参数可覆盖模板默认值。</Notice>}
          <div className="form-grid"><label><span>参数 JSON</span><textarea className="code-input" rows={6} value={parameterJson} onChange={(event) => setParameterJson(event.target.value)} /></label><label><span>风险默认值 JSON</span><textarea className="code-input" rows={6} value={riskJson} onChange={(event) => setRiskJson(event.target.value)} /></label></div>
          <FormActions><button className="button button--primary" type="submit" disabled={working}>{working ? "创建中…" : "创建策略"}</button></FormActions>
        </form>
      </Panel><Panel eyebrow="INDICATOR CATALOG" title="可用指标目录"><DataTable rows={firstList(indicatorCatalog ?? {}, "items", "indicators")} columns={[{ key: "key", label: "指标键" }, { key: "name", label: "名称" }, { key: "category", label: "类别" }, { key: "description", label: "说明" }]} />{indicatorCatalog ? <JsonPreview value={indicatorCatalog} label="指标参数规格" /> : null}</Panel></div> : null}

      {view && tab === "detail" ? <div className="two-column-layout">
        <Panel eyebrow="EDITABLE VERSION" title="版本化参数">
          {!selected ? <EmptyState title="请选择策略" description="从策略目录选择一个策略进入编辑。" /> : <form className="stack-form panel-form" onSubmit={(event) => void updateSelected(event)} key={`${selected.public_id}-${selected.version}`}>
            <label><span>当前策略</span><select value={selectedId} onChange={(event) => setSelectedId(event.target.value)}>{view.catalog.items.map((item) => <option key={item.public_id} value={item.public_id}>{item.name} · v{item.version}</option>)}</select></label>
            <div className="form-grid"><label><span>名称</span><input name="name" defaultValue={selected.name} required /></label><label><span>类别</span><input name="category" defaultValue={selected.category} required /></label></div>
            <label><span>描述</span><textarea name="description" rows={3} defaultValue={selected.description} /></label>
            <label><span>参数 JSON</span><textarea className="code-input" name="parameters" rows={7} defaultValue={JSON.stringify(selected.parameters, null, 2)} /></label>
            <label><span>风险默认值 JSON</span><textarea className="code-input" name="risk_defaults" rows={7} defaultValue={JSON.stringify(selected.risk_defaults, null, 2)} /></label>
            <FormActions><button className="button button--primary" type="submit" disabled={working}>保存为 v{selected.version + 1}</button><button className="button button--secondary" type="button" disabled={working} onClick={() => void perform(() => strategyApi.validate(selected.public_id), "策略验证完成", false)}>运行验证</button></FormActions>
          </form>}
        </Panel>
        <div className="stack-panels">
          <Panel eyebrow="REVISION LEDGER" title="历史版本"><DataTable rows={firstList(revisions ?? {}, "items", "revisions")} columns={[{ key: "version", label: "版本" }, { key: "status", label: "状态" }, { key: "spec_hash", label: "规格哈希" }, { key: "created_at", label: "创建时间", render: (row) => formatDate(stringValue(row.created_at)) }]} /></Panel>
          <Panel eyebrow="SIGNALS" title="最近信号"><DataTable rows={firstList(signals ?? {}, "items", "signals").filter((row) => !selected || stringValue(row.strategy_id ?? row.strategy_public_id) === selected.public_id).slice(0, 20)} columns={[{ key: "symbol", label: "标的" }, { key: "direction", label: "方向" }, { key: "status", label: "状态" }, { key: "created_at", label: "时间", render: (row) => formatDate(stringValue(row.created_at)) }]} /></Panel>
          {detail ? <JsonPreview value={detail} label="完整策略证据" /> : null}
        </div>
      </div> : null}

      {tab === "ai" ? <div className="two-column-layout">
        <Panel eyebrow="AI DRAFT" title="自然语言策略变更">
          <form className="stack-form panel-form" onSubmit={(event) => void previewAi(event)}>
            <label><span>目标策略</span><select value={selectedId} onChange={(event) => setSelectedId(event.target.value)}><option value="">创建新组合草案</option>{view?.catalog.items.map((item) => <option key={item.public_id} value={item.public_id}>{item.name} · v{item.version}</option>)}</select></label>
            <label><span>变更要求</span><textarea rows={8} value={aiPrompt} onChange={(event) => setAiPrompt(event.target.value)} placeholder="例如：降低最大仓位，将止损收紧到 1.5%，并说明改动理由。" required /></label>
            <Notice tone="warning">AI 只生成可审阅草案；应用时仍使用版本号做并发校验。</Notice>
            <FormActions><button className="button button--primary" type="submit" disabled={working}>生成草案</button></FormActions>
          </form>
        </Panel>
        <Panel eyebrow="REVIEW" title="草案审阅" actions={aiPreview ? selected ? <button className="button button--primary button--small" type="button" disabled={working} onClick={() => void applyAi()}>应用为新版本</button> : <button className="button button--primary button--small" type="button" onClick={useAiDraftForCreate}>带入创建表单</button> : undefined}>
          {aiPreview ? <div className="panel-body"><p className="muted-copy">{stringValue(aiPreview.summary, "请核对 proposed、changes 与基础版本后再应用。")}</p><JsonPreview value={aiPreview} label="完整 AI 草案" /></div> : <EmptyState title="尚未生成草案" description="输入自然语言要求后，系统会返回结构化的可审阅变更。" />}
        </Panel>
      </div> : null}
    </>
  );
}
