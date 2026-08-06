import type { ApiList, ApiObject } from "../api/types";
import { compactJson, stringValue } from "../utils/data";
import type { ReactNode } from "react";

export type Tone = "danger" | "info" | "neutral" | "success" | "warning";

export function StatusPill({ children, tone = "neutral" }: { children: ReactNode; tone?: Tone }) {
  return <span className={`status-pill status-pill--${tone}`}>{children}</span>;
}

export function PageHeader({
  eyebrow,
  title,
  description,
  actions,
}: {
  eyebrow: string;
  title: string;
  description: string;
  actions?: ReactNode;
}) {
  return (
    <header className="page-header">
      <div>
        <span className="eyebrow">{eyebrow}</span>
        <h1>{title}</h1>
        <p>{description}</p>
      </div>
      {actions ? <div className="page-header__actions">{actions}</div> : null}
    </header>
  );
}

export function MetricCard({
  label,
  value,
  note,
  tone = "neutral",
}: {
  label: string;
  value: ReactNode;
  note: string;
  tone?: Tone;
}) {
  return (
    <article className={`metric-card metric-card--${tone}`}>
      <span>{label}</span>
      <strong>{value}</strong>
      <small>{note}</small>
    </article>
  );
}

export function LoadingPanel({ label = "正在加载…" }: { label?: string }) {
  return (
    <div className="state-panel" role="status" aria-live="polite">
      <span className="spinner" aria-hidden="true" />
      <strong>{label}</strong>
      <small>正在从 QuantDesk 服务读取最新状态</small>
    </div>
  );
}

export function ErrorPanel({
  message,
  onRetry,
}: {
  message: string;
  onRetry?: () => void;
}) {
  return (
    <div className="state-panel state-panel--error" role="alert">
      <span className="state-panel__symbol" aria-hidden="true">!</span>
      <strong>数据暂时不可用</strong>
      <small>{message}</small>
      {onRetry ? (
        <button className="button button--secondary" type="button" onClick={onRetry}>
          重新加载
        </button>
      ) : null}
    </div>
  );
}

export function EmptyState({
  title,
  description,
}: {
  title: string;
  description: string;
}) {
  return (
    <div className="state-panel">
      <span className="state-panel__symbol state-panel__symbol--empty" aria-hidden="true">○</span>
      <strong>{title}</strong>
      <small>{description}</small>
    </div>
  );
}

export function Panel({
  eyebrow,
  title,
  actions,
  children,
  className = "",
}: {
  eyebrow?: string;
  title: string;
  actions?: ReactNode;
  children: ReactNode;
  className?: string;
}) {
  return (
    <article className={`panel ${className}`.trim()}>
      <header className="panel__header">
        <div>{eyebrow ? <span className="eyebrow">{eyebrow}</span> : null}<h2>{title}</h2></div>
        {actions ? <div className="panel__actions">{actions}</div> : null}
      </header>
      {children}
    </article>
  );
}

export function Notice({ children, tone = "info" }: { children: ReactNode; tone?: Tone }) {
  return <div className={`notice notice--${tone}`} role={tone === "danger" ? "alert" : "status"}>{children}</div>;
}

export function Tabs<T extends string>({
  value,
  items,
  onChange,
  label,
}: {
  value: T;
  items: Array<{ value: T; label: string }>;
  onChange: (value: T) => void;
  label: string;
}) {
  return (
    <div className="section-tabs" role="tablist" aria-label={label}>
      {items.map((item) => (
        <button
          key={item.value}
          type="button"
          role="tab"
          aria-selected={value === item.value}
          className={value === item.value ? "section-tabs__item section-tabs__item--active" : "section-tabs__item"}
          onClick={() => onChange(item.value)}
        >
          {item.label}
        </button>
      ))}
    </div>
  );
}

export interface DataColumn {
  key: string;
  label: string;
  render?: (row: ApiObject) => ReactNode;
}

export function DataTable({
  rows,
  columns,
  empty = "暂无数据",
  rowKey,
}: {
  rows: ApiList;
  columns: DataColumn[];
  empty?: string;
  rowKey?: (row: ApiObject, index: number) => string;
}) {
  if (rows.length === 0) return <EmptyState title={empty} description="当前筛选条件下没有可显示的记录。" />;
  return (
    <div className="table-scroll">
      <table className="data-table">
        <thead><tr>{columns.map((column) => <th key={column.key}>{column.label}</th>)}</tr></thead>
        <tbody>
          {rows.map((row, index) => (
            <tr key={rowKey ? rowKey(row, index) : stringValue(row.id ?? row.public_id, String(index))}>
              {columns.map((column) => <td key={column.key}>{column.render ? column.render(row) : stringValue(row[column.key])}</td>)}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function JsonPreview({ value, label = "原始数据" }: { value: unknown; label?: string }) {
  return (
    <details className="json-preview">
      <summary>{label}</summary>
      <pre>{compactJson(value)}</pre>
    </details>
  );
}

export function FormActions({ children }: { children: ReactNode }) {
  return <div className="form-actions">{children}</div>;
}
