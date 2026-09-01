type Metric = { label: string; value: number; tone?: "success" | "post" };

export function MetricStrip({ metrics }: { metrics: Metric[] }) {
  return <section className="metric-strip" aria-label="接口统计">
    {metrics.map((metric) => <div className={`metric${metric.tone ? ` ${metric.tone}` : ""}`} key={metric.label}>
      <strong>{metric.value}</strong>
      <span>{metric.label}</span>
    </div>)}
  </section>;
}
