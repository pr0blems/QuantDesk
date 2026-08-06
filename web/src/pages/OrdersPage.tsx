import { useCallback, useEffect, useMemo, useState } from "react";

import { settingsApi } from "../api/quantdesk";
import type { ApiObject } from "../api/types";
import { asList, asObject, firstList, stringValue } from "../utils/data";

function table(rows: ApiObject[], columns: Array<{ key: string; label: string }>, empty: string) {
  if (!rows.length) return <div className="orders-table-empty">{empty}</div>;
  return <table className="orders-table"><thead><tr>{columns.map((column) => <th key={column.key}>{column.label}</th>)}</tr></thead><tbody>{rows.map((row, index) => <tr key={stringValue(row.order_id ?? row.symbol, String(index))}>{columns.map((column) => <td className={column.key === "symbol" ? "symbol" : column.key === "side" ? stringValue(row[column.key]).toLowerCase() : ""} key={column.key}>{stringValue(row[column.key])}</td>)}</tr>)}</tbody></table>;
}

export function OrdersPage() {
  const [payload, setPayload] = useState<ApiObject | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const load = useCallback(async () => { setLoading(true); setError(""); try { setPayload(await settingsApi.binanceOrders()); } catch (caught) { setError(caught instanceof Error ? caught.message : "订单读取失败"); } finally { setLoading(false); } }, []);
  useEffect(() => { void load(); }, [load]);
  const data = asObject(payload?.account ?? payload);
  const positions = useMemo(() => payload ? (firstList(payload, "positions").length ? firstList(payload, "positions") : asList(data.positions)) : [], [data.positions, payload]);
  const orders = useMemo(() => payload ? (firstList(payload, "open_orders", "orders").length ? firstList(payload, "open_orders", "orders") : asList(data.open_orders)) : [], [data.open_orders, payload]);
  return <><div className="page-heading orders-heading"><div><span className="eyebrow">BINANCE LIVE STATE</span><h1>订单与持仓</h1><p>读取当前 Binance U 本位合约持仓与未成交订单</p></div><div className="orders-heading-actions"><span className="live-lock"><i />只读 · 不执行下单</span><button className="secondary" type="button" onClick={() => void load()}>刷新数据</button></div></div><div className="orders-summary"><article className="metric-card"><span>当前持仓</span><strong>{loading ? "--" : positions.length}</strong><small>非零合约仓位</small></article><article className="metric-card"><span>当前挂单</span><strong>{loading ? "--" : orders.length}</strong><small>尚未完全成交</small></article><article className="metric-card"><span>账户模式</span><strong>{stringValue(data.account_type)}</strong><small>{stringValue(data.updated_at, "等待同步")}</small></article></div><p className={`orders-message${error ? " error" : loading ? " loading" : " success"}`} role="status">{error || (loading ? "正在读取 Binance 账户数据…" : "账户数据已同步")}</p><div className="orders-data-grid"><article className="card orders-table-card"><header><div><span className="eyebrow">POSITIONS</span><h2>当前持仓</h2></div><span className="orders-count-badge">{positions.length} 个</span></header><div className="orders-table-wrap">{table(positions, [{ key: "symbol", label: "合约" }, { key: "position_side", label: "持仓方向" }, { key: "position_amt", label: "数量" }, { key: "entry_price", label: "开仓均价" }, { key: "unrealized_pnl", label: "未实现盈亏" }], "暂无持仓")}</div></article><article className="card orders-table-card"><header><div><span className="eyebrow">OPEN ORDERS</span><h2>当前挂单</h2></div><span className="orders-count-badge">{orders.length} 个</span></header><div className="orders-table-wrap">{table(orders, [{ key: "symbol", label: "合约" }, { key: "side", label: "方向" }, { key: "type", label: "类型" }, { key: "orig_qty", label: "数量" }, { key: "price", label: "价格" }, { key: "status", label: "状态" }], "暂无挂单")}</div></article></div></>;
}
