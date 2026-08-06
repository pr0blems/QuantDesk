export function PlaceholderPage({ kind }: { kind: "audit" | "risk" }) {
  if (kind === "risk") return <><div className="page-heading"><div><span className="eyebrow">RISK CONTROL</span><h1>风险控制</h1><p>用户、策略和标的三级风险边界</p></div></div><article className="card empty-state"><span>风</span><h2>风控规则正在规划</h2><p>将覆盖仓位上限、每日亏损、最大回撤、行情过期和全局 Kill Switch。</p></article></>;
  return <><div className="page-heading"><div><span className="eyebrow">AUDIT TRAIL</span><h1>审计日志</h1><p>敏感操作和安全事件追踪</p></div></div><article className="card empty-state"><span>审</span><h2>审计查询界面正在建设</h2><p>注册、登录及凭据变更已经在服务端记录，后续将提供分级查询和导出功能。</p></article></>;
}
