import type { Catalog } from "../types";

export function SecurityPanel({ catalog }: { catalog: Catalog }) {
  const expiry = catalog.summary.jwtExpiryUtc ? new Date(catalog.summary.jwtExpiryUtc) : null;
  return <main className="content-panel security-panel">
    <div className="page-intro"><div><h1>安全说明</h1><p>这个 Demo 只读取已脱敏的本地数据，不携带真实令牌，也不会向抓包中的服务域名发起请求。</p></div></div>
    <section className="security-callout">
      <div className="shield-mark">✓</div>
      <div><h2>默认离线，明确区分“演示”与“重放”</h2><p>“运行离线示例”只展示 HAR 中保存并脱敏后的响应；“复制 cURL 模板”使用占位令牌，POST 请求体明确标记为需要人工审核。</p></div>
    </section>
    <section className="security-grid">
      <article className="open-panel"><span className="security-index">01</span><h2>敏感字段已处理</h2><p>Authorization、Cookie 值、账户、用户、设备、电话号码、邮箱与会话字段不会进入前端数据文件。</p></article>
      <article className="open-panel"><span className="security-index">02</span><h2>Bearer 不是长期凭据</h2><p>抓包中观察到的 JWT 声明过期时间为 {expiry ? expiry.toLocaleString("zh-CN") : "未知"}；实际还可能被提前撤销。</p></article>
      <article className="open-panel"><span className="security-index">03</span><h2>POST 仍需人工确认</h2><p>保留的 POST 主要用于行情详情、排行和主题内容查询，但私有接口语义可能变化，不应批量重放。</p></article>
      <article className="open-panel"><span className="security-index">04</span><h2>仅用于合法账户</h2><p>文档说明的是抓包中观察到的客户端接口，不代表公开 API，也不等同于稳定、授权的第三方集成能力。</p></article>
    </section>
    <section className="redaction-table open-panel">
      <div className="panel-title"><h2>脱敏策略</h2><span>生成脚本可重复执行</span></div>
      <div className="redaction-row"><code>authorization / token / cookie</code><span>完全删除值，仅保留是否观察到</span></div>
      <div className="redaction-row"><code>account / user / device / id</code><span>值替换为 &lt;redacted&gt;</span></div>
      <div className="redaction-row"><code>response arrays</code><span>仅保留前 2 项并限制递归深度</span></div>
      <div className="redaction-row"><code>POST replay</code><span>UI 不提供线上执行入口</span></div>
    </section>
  </main>;
}
