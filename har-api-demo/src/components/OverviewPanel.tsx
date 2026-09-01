import type { Catalog } from "../types";
import { MetricStrip } from "./MetricStrip";

export function OverviewPanel({ catalog, onOpenCatalog }: { catalog: Catalog; onOpenCatalog: () => void }) {
  const { summary } = catalog;
  const maxHost = summary.hostCounts[0]?.count ?? 1;
  return <main className="content-panel overview-panel">
    <div className="page-intro"><div><h1>股票资讯接口总览</h1><p>已从原始抓包中移除账户、遥测、通知和运营配置，只保留可提供股票资讯的数据接口。</p></div><button className="primary-button" onClick={onOpenCatalog} type="button">查看 {summary.endpointCount} 个接口</button></div>
    <MetricStrip metrics={[
      { label: "接口", value: summary.endpointCount },
      { label: "可用", value: summary.usableCount, tone: "success" },
      { label: "GET", value: summary.getCount },
      { label: "POST", value: summary.postCount, tone: "post" },
    ]} />
    <section className="overview-grid">
      <article className="open-panel finding-panel">
        <h2>这批接口主要做什么</h2>
        <ol className="finding-list">
          <li><span>01</span><div><strong>行情数据是主链路</strong><p>股票详情、逐笔成交、买卖盘、分时走势、指数成分、公司行动与估值。</p></div></li>
          <li><span>02</span><div><strong>新闻接口提供证券资讯</strong><p>包含证券新闻聚合、置顶新闻和按证券代码查询的新闻列表。</p></div></li>
          <li><span>03</span><div><strong>社区资讯补充市场观点</strong><p>围绕 symbol 返回最新讨论、推荐内容、市场态度、晒单和每日摘要。</p></div></li>
          <li><span>04</span><div><strong>IPO 数据单独保留</strong><p>用于获取 IPO 标的列表，作为股票资讯范围的补充数据源。</p></div></li>
        </ol>
      </article>
      <article className="open-panel host-panel">
        <div className="panel-title"><h2>域名分布</h2><span>{summary.hostCount} 个服务域名</span></div>
        <div className="bar-list">
          {summary.hostCounts.slice(0, 9).map((host) => <div className="bar-row" key={host.name}><div><code>{host.name}</code><strong>{host.count}</strong></div><span><i style={{ width: `${(host.count / maxHost) * 100}%` }} /></span></div>)}
        </div>
      </article>
    </section>
    <section className="evidence-strip">
      <div><span>采集窗口</span><strong>{new Date(summary.capturedFrom).toLocaleString("zh-CN")} — {new Date(summary.capturedTo).toLocaleTimeString("zh-CN")}</strong></div>
      <div><span>保留请求证据</span><strong>{summary.apiCalls} 次调用，{summary.authenticatedEndpointCount} 个接口观察到 Bearer</strong></div>
      <div><span>筛选结果</span><strong>{summary.sourceEndpointCount} 个原始接口 → {summary.endpointCount} 个股票资讯接口</strong></div>
    </section>
  </main>;
}
