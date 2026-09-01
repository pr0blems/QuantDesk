import type { Catalog } from "../types";

export function AnalysisPanel({ catalog }: { catalog: Catalog }) {
  const maxCategory = catalog.summary.categoryCounts[0]?.count ?? 1;
  const flows = [
    { index: "01", title: "证券详情页", source: "hq2 + hq-depth", output: "报价、逐笔、盘口、分时、估值", tone: "lime" },
    { index: "02", title: "证券新闻", source: "stock-news", output: "证券新闻、置顶新闻与资讯聚合", tone: "blue" },
    { index: "03", title: "社区资讯", source: "community-service", output: "讨论、态度、晒单与每日摘要", tone: "amber" },
    { index: "04", title: "IPO 数据", source: "trade", output: "IPO 标的与发行信息", tone: "violet" },
  ];
  return <main className="content-panel analysis-panel">
    <div className="page-intro"><div><h1>股票资讯数据分析</h1><p>保留接口共同拼装出证券详情页、新闻流、社区观点和 IPO 数据。</p></div></div>
    <section className="analysis-layout">
      <article className="open-panel category-panel">
        <div className="panel-title"><h2>用途分类</h2><span>按接口去重统计</span></div>
        <div className="category-bars">
          {catalog.summary.categoryCounts.map((category) => <div className="category-row" key={category.name}><span>{category.name}</span><div><i style={{ width: `${(category.count / maxCategory) * 100}%` }} /></div><strong>{category.count}</strong></div>)}
        </div>
      </article>
      <article className="open-panel data-facts">
        <h2>响应数据特征</h2>
        <dl>
          <div><dt>统一但不一致的成功码</dt><dd>`ret=0`、`ret=200`、`code=200`、`code=62000000`、`code=91000000` 同时存在，接入层必须做域名级适配。</dd></div>
          <div><dt>客户端上下文占比很高</dt><dd>大量请求重复携带版本、设备、地区和皮肤参数；真正的业务参数通常只有 symbol、market、period 与分页字段。</dd></div>
          <div><dt>市场数据按页面并行加载</dt><dd>同一证券 PYPL 与指数 .IXIC 在 1 分钟内触发多组行情、资讯、社区和提醒请求，符合移动端页面聚合模式。</dd></div>
          <div><dt>股票资讯接口全部成功</dt><dd>筛选后 26 个接口在抓包中均取得业务成功响应，失败的行情提醒接口已被移除。</dd></div>
        </dl>
      </article>
    </section>
    <section className="flow-section">
      <h2>从接口到产品功能</h2>
      <div className="flow-list">
        {flows.map((flow) => <article className={`flow-item ${flow.tone}`} key={flow.index}><span>{flow.index}</span><div><strong>{flow.title}</strong><code>{flow.source}</code></div><i aria-hidden="true" /><p>{flow.output}</p></article>)}
      </div>
    </section>
  </main>;
}
