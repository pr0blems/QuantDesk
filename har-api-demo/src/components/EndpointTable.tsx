import type { Endpoint } from "../types";

type Props = {
  endpoints: Endpoint[];
  selectedId: string;
  onSelect: (endpoint: Endpoint) => void;
};

function primaryStatus(endpoint: Endpoint): number {
  return endpoint.statuses[0]?.status ?? 0;
}

export function EndpointTable({ endpoints, selectedId, onSelect }: Props) {
  return <div className="table-shell">
    <table className="endpoint-table">
      <thead><tr><th>方法</th><th>域名与路径</th><th>用途</th><th>状态</th><th>调用次数</th></tr></thead>
      <tbody>
        {endpoints.map((endpoint) => <tr
          className={endpoint.id === selectedId ? "selected" : ""}
          key={endpoint.id}
          onClick={() => onSelect(endpoint)}
          tabIndex={0}
          onKeyDown={(event) => { if (event.key === "Enter" || event.key === " ") onSelect(endpoint); }}
        >
          <td><span className={`method method-${endpoint.method.toLowerCase()}`}>{endpoint.method}</span></td>
          <td><div className="endpoint-address"><strong>{endpoint.host}</strong><code>{endpoint.path}</code></div></td>
          <td>{endpoint.purpose}</td>
          <td><span className={`status ${endpoint.usable ? "ok" : "failed"}`}><i />{endpoint.usable ? primaryStatus(endpoint) : "业务失败"}</span></td>
          <td className="numeric">{endpoint.calls}</td>
        </tr>)}
      </tbody>
    </table>
    {!endpoints.length && <div className="empty-state"><strong>没有匹配的接口</strong><span>调整搜索词或筛选条件。</span></div>}
  </div>;
}
