import { useEffect, useMemo, useState } from "react";
import { CopyIcon, PlayIcon } from "./Icons";
import type { Endpoint, QueryParam, SchemaNode } from "../types";

type DetailTab = "params" | "request" | "response";

function schemaLines(schema: SchemaNode | null, prefix = "", depth = 0): string[] {
  if (!schema || depth > 3) return [];
  if (schema.type === "object") {
    return Object.entries(schema.properties ?? {}).slice(0, 18).flatMap(([key, child]) => {
      const line = `${prefix}${key}: ${child.type}`;
      return [line, ...schemaLines(child, `${prefix}  `, depth + 1)];
    });
  }
  if (schema.type === "array" && schema.items) return [`${prefix}[]: ${schema.items.type}`, ...schemaLines(schema.items, `${prefix}  `, depth + 1)];
  return [];
}

function placeholder(param: QueryParam): string {
  if (param.example === "<redacted>") return `<${param.name}>`;
  if (param.name === "symbol") return "PYPL";
  return String(param.example ?? `<${param.name}>`);
}

function buildCurl(endpoint: Endpoint): string {
  const query = endpoint.queryParams
    .filter((param) => !param.context)
    .slice(0, 10)
    .map((param) => `${encodeURIComponent(param.name)}=${encodeURIComponent(placeholder(param))}`)
    .join("&");
  const url = `https://${endpoint.host}${endpoint.path}${query ? `?${query}` : ""}`;
  const lines = [`curl --request ${endpoint.method} '${url}'`, "  --header 'Authorization: Bearer <YOUR_TOKEN>'"];
  if (endpoint.method !== "GET") lines.push("  --header 'Content-Type: application/json'", "  --data '<REVIEW_REQUIRED>'");
  return lines.join(" " + String.fromCharCode(92) + "\n");
}

export function EndpointInspector({ endpoint }: { endpoint: Endpoint }) {
  const [tab, setTab] = useState<DetailTab>("params");
  const [demoState, setDemoState] = useState<"idle" | "running" | "done">("idle");
  const [copied, setCopied] = useState(false);
  const schema = useMemo(() => schemaLines(endpoint.response.schema), [endpoint]);

  useEffect(() => { setDemoState("idle"); setCopied(false); setTab("params"); }, [endpoint.id]);

  async function copyCurl() {
    await navigator.clipboard.writeText(buildCurl(endpoint));
    setCopied(true);
    window.setTimeout(() => setCopied(false), 1400);
  }

  function runOfflineDemo() {
    setDemoState("running");
    window.setTimeout(() => setDemoState("done"), 650);
  }

  return <aside className="inspector" aria-label="接口详情">
    <section className="inspector-section endpoint-summary">
      <div className="section-heading"><h2>接口说明</h2><span className={`method method-${endpoint.method.toLowerCase()}`}>{endpoint.method}</span></div>
      <dl>
        <dt>域名</dt><dd><code>{endpoint.host}</code></dd>
        <dt>路径</dt><dd><code>{endpoint.path}</code></dd>
        <dt>用途</dt><dd>{endpoint.category} / {endpoint.purpose}</dd>
        <dt>状态</dt><dd><span className={`status ${endpoint.usable ? "ok" : "failed"}`}><i />{endpoint.usable ? "抓包中成功" : "业务返回失败"}</span></dd>
        <dt>耗时</dt><dd>{endpoint.avgDurationMs} ms 平均</dd>
      </dl>
      <p>{endpoint.description}</p>
      <div className="inspector-actions">
        <button className="primary-button" onClick={runOfflineDemo} disabled={demoState === "running"} type="button"><PlayIcon />{demoState === "running" ? "正在模拟" : "运行离线示例"}</button>
        <button className="secondary-button" onClick={() => void copyCurl()} type="button"><CopyIcon />{copied ? "已复制" : "复制 cURL 模板"}</button>
      </div>
      {demoState === "done" && <div className="demo-result" role="status"><span>OFFLINE</span><strong>{endpoint.usable ? "200 captured" : "200 / business failed"}</strong><small>{endpoint.avgDurationMs} ms · 已脱敏响应</small></div>}
    </section>

    <section className="inspector-section inspector-tabs">
      <div className="tab-list" role="tablist">
        <button className={tab === "params" ? "active" : ""} onClick={() => setTab("params")} role="tab" type="button">请求参数</button>
        <button className={tab === "request" ? "active" : ""} onClick={() => setTab("request")} role="tab" type="button">请求体</button>
        <button className={tab === "response" ? "active" : ""} onClick={() => setTab("response")} role="tab" type="button">响应结构</button>
      </div>
      {tab === "params" && <div className="param-table">
        <div className="param-row param-head"><span>参数</span><span>类型</span><span>说明</span></div>
        {endpoint.queryParams.filter((item) => !item.context).slice(0, 12).map((param) => <div className="param-row" key={param.name}><code>{param.name}</code><span>{param.type}</span><span>{param.description}</span></div>)}
        {!endpoint.queryParams.some((item) => !item.context) && <p className="muted-copy">未观察到独立业务查询参数。</p>}
        <p className="context-note">另有 {endpoint.queryParams.filter((item) => item.context).length} 个客户端上下文参数，文档中已完整列出。</p>
      </div>}
      {tab === "request" && <pre className="json-view">{JSON.stringify(endpoint.requestBody.preview ?? { note: endpoint.method === "GET" ? "GET 请求无请求体" : "请求体未保存或为二进制" }, null, 2)}</pre>}
      {tab === "response" && <div className="schema-view">{schema.length ? schema.map((line, index) => <code key={`${line}-${index}`}>{line}</code>) : <p className="muted-copy">响应为空或仅命中 304 缓存。</p>}</div>}
    </section>

    <section className="inspector-section response-preview">
      <div className="section-heading"><h2>脱敏响应预览</h2><span>{endpoint.mimeTypes[0]}</span></div>
      <pre className="json-view">{JSON.stringify(endpoint.response.preview ?? endpoint.response.businessSignals[0] ?? { note: "HAR 未保存响应体" }, null, 2)}</pre>
    </section>
  </aside>;
}
