export function formatDate(value: string | null | undefined): string {
  if (!value) return "尚未记录";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "时间未知";
  return new Intl.DateTimeFormat("zh-CN", {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(date);
}

export function formatMoney(value: number | undefined, currency = "USDT"): string {
  if (typeof value !== "number" || !Number.isFinite(value)) return "--";
  return `${new Intl.NumberFormat("zh-CN", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  }).format(value)} ${currency}`;
}

export function formatPercent(value: number | undefined): string {
  if (typeof value !== "number" || !Number.isFinite(value)) return "--";
  return `${new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 2 }).format(value)}%`;
}
