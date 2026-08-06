import { useCallback, useEffect, useMemo, useState, type FormEvent } from "react";

import { adminApi } from "../api/quantdesk";
import type { AdminAlertRulesUpdate, AdminCleanupRequest, ApiObject } from "../api/types";
import { DataTable, ErrorPanel, FormActions, JsonPreview, LoadingPanel, MetricCard, Notice, PageHeader, Panel, StatusPill, Tabs } from "../components/ui";
import { asList, asObject, booleanValue, firstList, numberValue, stringValue } from "../utils/data";
import { formatDate } from "../utils/format";

type AdminTab = "alerts" | "collectors" | "data" | "maintenance" | "news" | "overview" | "users";

function rows(payload: ApiObject | null, ...keys: string[]) {
  if (!payload) return [];
  const found = firstList(payload, ...keys);
  return found.length > 0 ? found : asList(payload);
}

export function AdminPage() {
  const [tab, setTab] = useState<AdminTab>("overview");
  const [payload, setPayload] = useState<ApiObject | null>(null);
  const [secondary, setSecondary] = useState<ApiObject | null>(null);
  const [tertiary, setTertiary] = useState<ApiObject | null>(null);
  const [cleanupPreview, setCleanupPreview] = useState<ApiObject | null>(null);
  const [loading, setLoading] = useState(true);
  const [working, setWorking] = useState(false);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");

  const loadTab = useCallback(async (selected: AdminTab) => {
    setLoading(true);
    setError("");
    setPayload(null);
    setSecondary(null);
    setTertiary(null);
    try {
      if (selected === "overview") {
        const [overview, storage] = await Promise.all([adminApi.overview(), adminApi.storage()]);
        setPayload(overview); setSecondary(storage);
      } else if (selected === "collectors") {
        setPayload(await adminApi.collectors());
      } else if (selected === "alerts") {
        const [alerts, rules] = await Promise.all([adminApi.alerts({ limit: 100 }), adminApi.alertRules()]);
        setPayload(alerts); setSecondary(rules);
      } else if (selected === "news") {
        const [sources, news, batches] = await Promise.all([adminApi.newsSources(), adminApi.news({ limit: 100 }), adminApi.newsBatches()]);
        setPayload(sources); setSecondary(news); setTertiary(batches);
      } else if (selected === "users") {
        setPayload(await adminApi.users({ limit: 200 }));
      } else if (selected === "data") {
        const [symbols, stocks, audit] = await Promise.all([adminApi.symbols({ limit: 200 }), adminApi.stockLibrary({ limit: 200 }), adminApi.audit({ limit: 200 })]);
        setPayload(symbols); setSecondary(stocks); setTertiary(audit);
      } else {
        setPayload(await adminApi.storage());
      }
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "管理数据加载失败");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { void loadTab(tab); }, [loadTab, tab]);

  async function action(run: () => Promise<unknown>, success: string): Promise<void> {
    setWorking(true);
    setError("");
    try {
      await run();
      setMessage(success);
      await loadTab(tab);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "管理操作失败");
    } finally {
      setWorking(false);
    }
  }

  async function saveAlertRules(event: FormEvent<HTMLFormElement>): Promise<void> {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    const input: AdminAlertRulesUpdate = {
      score_alert_long: Number(form.get("score_alert_long")),
      score_alert_short: Number(form.get("score_alert_short")),
      score_alert_position: Number(form.get("score_alert_position")),
      spike_alert_pct_5m: Number(form.get("spike_alert_pct_5m")),
      enabled_timeframes: form.getAll("timeframes").map(String) as NonNullable<AdminAlertRulesUpdate["enabled_timeframes"]>,
      watchlist_only: form.get("watchlist_only") === "on",
    };
    await action(() => adminApi.updateAlertRules(input), "提醒规则已保存");
  }

  async function createSource(event: FormEvent<HTMLFormElement>): Promise<void> {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    await action(() => adminApi.createNewsSource({
      name: stringValue(form.get("name"), "").trim(),
      url: stringValue(form.get("url"), "").trim(),
      lang: stringValue(form.get("lang"), "en"),
      feed_type: stringValue(form.get("feed_type"), "rss"),
      weight: Number(form.get("weight")),
      hourly_limit: Number(form.get("hourly_limit")),
      enabled: form.get("enabled") === "on",
      slow: form.get("slow") === "on",
    }), "新闻源已创建");
    event.currentTarget.reset();
  }

  function cleanupInput(form: FormData, confirm: boolean): AdminCleanupRequest {
    return {
      alerts_days: Number(form.get("alerts_days")),
      news_days: Number(form.get("news_days")),
      scores_days: Number(form.get("scores_days")),
      confirm,
    };
  }

  async function previewCleanup(event: FormEvent<HTMLFormElement>): Promise<void> {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    setWorking(true);
    try {
      setCleanupPreview(await adminApi.cleanupPreview(cleanupInput(form, false)));
      setMessage("清理预览已生成，确认范围后才能执行");
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "清理预览失败");
    } finally {
      setWorking(false);
    }
  }

  async function executeCleanup(event: FormEvent<HTMLFormElement>): Promise<void> {
    event.preventDefault();
    if (!cleanupPreview || !window.confirm("确定执行数据库清理？该操作会删除超过保留期的数据。")) return;
    const form = new FormData(event.currentTarget);
    await action(() => adminApi.cleanup(cleanupInput(form, true)), "存储清理已完成");
    setCleanupPreview(null);
  }

  const overview = asObject(payload?.summary ?? payload);
  const rules = asObject(secondary?.rules ?? secondary);
  const sourceRows = useMemo(() => rows(payload, "items", "sources"), [payload]);

  return (
    <>
      <PageHeader eyebrow="ADMINISTRATION" title="管理后台" description="管理采集器、提醒规则、新闻源、用户权限、数据目录、审计和存储维护。" actions={<button className="button button--secondary" type="button" onClick={() => void loadTab(tab)} disabled={loading || working}>刷新当前模块</button>} />
      <Tabs value={tab} onChange={setTab} label="管理模块" items={[
        { value: "overview", label: "运行总览" }, { value: "collectors", label: "采集器" }, { value: "alerts", label: "提醒规则" }, { value: "news", label: "新闻与 AI" }, { value: "users", label: "用户权限" }, { value: "data", label: "数据目录" }, { value: "maintenance", label: "存储维护" },
      ]} />
      {loading ? <LoadingPanel label="正在读取管理数据…" /> : null}
      {error ? <ErrorPanel message={error} onRetry={() => void loadTab(tab)} /> : null}
      {message ? <Notice tone="success">{message}</Notice> : null}

      {!loading && tab === "overview" ? (
        <>
          <section className="metric-grid">
            <MetricCard label="用户" value={numberValue(overview.user_count ?? overview.users)} note="平台账户" />
            <MetricCard label="活跃模拟盘" value={numberValue(overview.active_paper_accounts ?? overview.paper_accounts)} note="当前运行" tone="success" />
            <MetricCard label="提醒" value={numberValue(overview.alert_count ?? overview.alerts)} note="累计记录" />
            <MetricCard label="新闻" value={numberValue(overview.news_count ?? overview.news)} note="去重内容" />
            <MetricCard label="行情标的" value={numberValue(overview.symbol_count ?? overview.symbols)} note="支持市场" />
            <MetricCard label="审计事件" value={numberValue(overview.audit_count ?? overview.audit_events)} note="安全操作记录" tone="info" />
          </section>
          <section className="two-column-layout"><Panel eyebrow="RUNTIME" title="运行状态"><JsonPreview value={payload} label="系统运行详情" /></Panel><Panel eyebrow="STORAGE" title="存储概览"><JsonPreview value={secondary} label="数据库表与保留策略" /></Panel></section>
        </>
      ) : null}

      {!loading && tab === "collectors" ? (
        <Panel eyebrow="COLLECTORS" title="数据采集器">
          <DataTable rows={rows(payload, "items", "collectors")} columns={[
            { key: "name", label: "采集器" },
            { key: "status", label: "状态", render: (row) => <StatusPill tone={stringValue(row.status) === "running" ? "success" : "warning"}>{stringValue(row.status)}</StatusPill> },
            { key: "last_run_at", label: "最近运行", render: (row) => formatDate(stringValue(row.last_run_at ?? row.updated_at, "")) },
            { key: "last_error", label: "最近错误" },
            { key: "actions", label: "操作", render: (row) => { const name = stringValue(row.name, ""); return <div className="inline-actions"><button type="button" onClick={() => void action(() => adminApi.collectorAction(name, "start"), `${name} 已启动`)}>启动</button><button type="button" onClick={() => void action(() => adminApi.collectorAction(name, "stop"), `${name} 已停止`)}>停止</button><button type="button" onClick={() => void action(() => adminApi.collectorAction(name, "run"), `${name} 已触发`)}>立即运行</button></div>; } },
          ]} empty="暂无采集器" />
        </Panel>
      ) : null}

      {!loading && tab === "alerts" ? (
        <section className="two-column-layout">
          <Panel eyebrow="RULES" title="提醒规则">
            <form className="form-grid" onSubmit={(event) => void saveAlertRules(event)}>
              <label><span>看多评分阈值</span><input name="score_alert_long" type="number" min="40" max="100" defaultValue={numberValue(rules.score_alert_long, 60)} /></label>
              <label><span>看空评分阈值</span><input name="score_alert_short" type="number" min="-100" max="-40" defaultValue={numberValue(rules.score_alert_short, -60)} /></label>
              <label><span>持仓提醒阈值</span><input name="score_alert_position" type="number" min="20" max="100" defaultValue={numberValue(rules.score_alert_position, 40)} /></label>
              <label><span>5 分钟异动 %</span><input name="spike_alert_pct_5m" type="number" min="0.1" max="20" step="0.1" defaultValue={numberValue(rules.spike_alert_pct_5m, 2)} /></label>
              <fieldset className="form-grid__wide"><legend>启用周期</legend>{["15m", "1h", "4h"].map((tf) => <label className="check-field" key={tf}><input name="timeframes" type="checkbox" value={tf} defaultChecked /><span>{tf}</span></label>)}</fieldset>
              <label className="check-field form-grid__wide"><input name="watchlist_only" type="checkbox" defaultChecked={rules.watchlist_only === undefined ? true : booleanValue(rules.watchlist_only)} /><span>仅为自选标的生成提醒</span></label>
              <FormActions><button className="button button--primary" type="submit" disabled={working}>保存规则</button></FormActions>
            </form>
          </Panel>
          <Panel eyebrow="ALERT LOG" title="最近提醒"><DataTable rows={rows(payload, "items", "alerts")} columns={[{ key: "created_at", label: "时间", render: (row) => formatDate(stringValue(row.created_at ?? row.ts, "")) }, { key: "symbol", label: "标的" }, { key: "kind", label: "类型" }, { key: "message", label: "内容" }]} empty="暂无提醒" /></Panel>
        </section>
      ) : null}

      {!loading && tab === "news" ? (
        <>
          <section className="two-column-layout two-column-layout--wide-left">
            <Panel eyebrow="SOURCES" title="新闻源">
              <DataTable rows={sourceRows} columns={[
                { key: "name", label: "名称" }, { key: "feed_type", label: "类型" }, { key: "lang", label: "语言" }, { key: "enabled", label: "状态", render: (row) => <StatusPill tone={booleanValue(row.enabled) ? "success" : "neutral"}>{booleanValue(row.enabled) ? "启用" : "停用"}</StatusPill> },
                { key: "actions", label: "操作", render: (row) => { const name = stringValue(row.name, ""); return <div className="inline-actions"><button type="button" onClick={() => void action(() => adminApi.testNewsSource(name), `${name} 测试成功`)}>测试</button><button type="button" onClick={() => void action(() => adminApi.updateNewsSource(name, { enabled: !booleanValue(row.enabled) }), `${name} 状态已更新`)}>{booleanValue(row.enabled) ? "停用" : "启用"}</button><button className="danger-action" type="button" onClick={() => { if (window.confirm(`确定删除新闻源“${name}”？`)) void action(() => adminApi.deleteNewsSource(name), `${name} 已删除`); }}>删除</button></div>; } },
              ]} empty="暂无新闻源" />
            </Panel>
            <Panel eyebrow="NEW SOURCE" title="新增新闻源">
              <form className="stack-form" onSubmit={(event) => void createSource(event)}>
                <label><span>名称</span><input name="name" required maxLength={80} /></label><label><span>URL</span><input name="url" type="url" required /></label>
                <div className="form-grid"><label><span>类型</span><select name="feed_type"><option value="rss">RSS</option><option value="taoz_flash">淘金快讯</option></select></label><label><span>语言</span><input name="lang" defaultValue="en" /></label><label><span>权重</span><input name="weight" type="number" min="1" max="1000" defaultValue="100" /></label><label><span>每小时上限</span><input name="hourly_limit" type="number" min="1" max="10000" defaultValue="600" /></label></div>
                <label className="check-field"><input name="enabled" type="checkbox" defaultChecked /><span>立即启用</span></label><label className="check-field"><input name="slow" type="checkbox" /><span>慢速来源</span></label>
                <FormActions><button className="button button--primary" type="submit" disabled={working}>新增来源</button></FormActions>
              </form>
            </Panel>
          </section>
          <section className="two-column-layout"><Panel eyebrow="NEWS" title="新闻内容"><DataTable rows={rows(secondary, "items", "news")} columns={[{ key: "created_at", label: "时间", render: (row) => formatDate(stringValue(row.created_at ?? row.ts, "")) }, { key: "source", label: "来源" }, { key: "title", label: "标题", render: (row) => stringValue(row.title_zh ?? row.title) }, { key: "sentiment", label: "情绪" }]} empty="暂无新闻" /></Panel><Panel eyebrow="AI BATCHES" title="AI 新闻批次" actions={<div className="inline-actions"><button type="button" onClick={() => void action(() => adminApi.createNewsBatch({ count: 300 }), "300 条 AI 批次已创建")}>处理 300 条</button><button type="button" onClick={() => void action(() => adminApi.createNewsBatch({ count: 500 }), "500 条 AI 批次已创建")}>处理 500 条</button></div>}><DataTable rows={rows(tertiary, "items", "batches")} columns={[{ key: "id", label: "批次" }, { key: "status", label: "状态" }, { key: "total", label: "总数" }, { key: "processed", label: "已处理" }, { key: "action", label: "操作", render: (row) => <button type="button" onClick={() => void action(() => adminApi.retryNewsBatch(stringValue(row.id)), "批次已重试")}>重试</button> }]} empty="暂无批次" /></Panel></section>
        </>
      ) : null}

      {!loading && tab === "users" ? (
        <Panel eyebrow="USERS" title="用户与权限">
          <DataTable rows={rows(payload, "items", "users")} columns={[
            { key: "id", label: "ID" }, { key: "username", label: "用户名" }, { key: "email", label: "邮箱" },
            { key: "is_active", label: "状态", render: (row) => <StatusPill tone={booleanValue(row.is_active) ? "success" : "danger"}>{booleanValue(row.is_active) ? "启用" : "停用"}</StatusPill> },
            { key: "is_admin", label: "角色", render: (row) => booleanValue(row.is_admin) ? "管理员" : "用户" },
            { key: "created_at", label: "创建时间", render: (row) => formatDate(stringValue(row.created_at, "")) },
            { key: "actions", label: "操作", render: (row) => { const id = stringValue(row.id); return <div className="inline-actions"><button type="button" onClick={() => void action(() => adminApi.updateUser(id, { is_active: !booleanValue(row.is_active) }), "用户状态已更新")}>{booleanValue(row.is_active) ? "停用" : "启用"}</button><button type="button" onClick={() => void action(() => adminApi.updateUser(id, { is_admin: !booleanValue(row.is_admin) }), "用户角色已更新")}>{booleanValue(row.is_admin) ? "取消管理员" : "设为管理员"}</button><button type="button" onClick={() => void action(() => adminApi.revokeSessions(id), "用户会话已撤销")}>撤销会话</button></div>; } },
          ]} empty="暂无用户" />
        </Panel>
      ) : null}

      {!loading && tab === "data" ? (
        <>
          <Panel eyebrow="SYMBOLS" title="合约数据"><DataTable rows={rows(payload, "items", "symbols")} columns={[{ key: "symbol", label: "代码" }, { key: "name", label: "名称" }, { key: "market", label: "市场" }, { key: "status", label: "状态" }, { key: "updated_at", label: "更新时间", render: (row) => formatDate(stringValue(row.updated_at, "")) }]} empty="暂无合约数据" /></Panel>
          <Panel eyebrow="STOCK LIBRARY" title="股票资料库" actions={<button type="button" onClick={() => void action(() => adminApi.importStockLibrary(), "股票资料导入已触发")}>导入资料</button>}><DataTable rows={rows(secondary, "items", "stocks")} columns={[{ key: "symbol", label: "代码" }, { key: "name", label: "名称", render: (row) => stringValue(row.name_zh ?? row.name) }, { key: "industry", label: "行业" }, { key: "verification_status", label: "核验状态" }, { key: "action", label: "操作", render: (row) => <button type="button" onClick={() => void action(() => adminApi.syncStock(stringValue(row.symbol)), "股票资料已同步")}>同步</button> }]} empty="暂无股票资料" /></Panel>
          <Panel eyebrow="AUDIT" title="审计日志"><DataTable rows={rows(tertiary, "items", "logs")} columns={[{ key: "created_at", label: "时间", render: (row) => formatDate(stringValue(row.created_at, "")) }, { key: "username", label: "用户" }, { key: "action", label: "操作" }, { key: "resource_type", label: "资源" }, { key: "ip_address", label: "IP" }]} empty="暂无审计记录" /></Panel>
        </>
      ) : null}

      {!loading && tab === "maintenance" ? (
        <section className="two-column-layout">
          <Panel eyebrow="RETENTION" title="存储清理">
            <Notice tone="danger">执行清理会永久删除超过保留期的提醒、新闻和评分数据。必须先预览再确认。</Notice>
            <form className="form-grid" onSubmit={(event) => cleanupPreview ? void executeCleanup(event) : void previewCleanup(event)}>
              <label><span>提醒保留天数</span><input name="alerts_days" type="number" min="1" max="3650" defaultValue="30" /></label>
              <label><span>新闻保留天数</span><input name="news_days" type="number" min="1" max="3650" defaultValue="90" /></label>
              <label><span>评分保留天数</span><input name="scores_days" type="number" min="1" max="3650" defaultValue="180" /></label>
              <FormActions><button className={cleanupPreview ? "button button--danger" : "button button--primary"} type="submit" disabled={working}>{cleanupPreview ? "确认执行清理" : "生成清理预览"}</button>{cleanupPreview ? <button className="button button--secondary" type="button" onClick={() => setCleanupPreview(null)}>取消</button> : null}</FormActions>
            </form>
          </Panel>
          <Panel eyebrow="PREVIEW" title="影响范围">{cleanupPreview ? <JsonPreview value={cleanupPreview} label="待删除记录" /> : <JsonPreview value={payload} label="当前存储状态" />}</Panel>
        </section>
      ) : null}
    </>
  );
}
