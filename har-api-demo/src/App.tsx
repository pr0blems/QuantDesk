import { useEffect, useMemo, useState } from "react";
import catalogData from "./data/catalog.json";
import documentation from "../docs/API_CATALOG.md?raw";
import { AnalysisPanel } from "./components/AnalysisPanel";
import { EndpointInspector } from "./components/EndpointInspector";
import { EndpointTable } from "./components/EndpointTable";
import { AnalysisIcon, ApiIcon, ExportIcon, OverviewIcon, SearchIcon, ShieldIcon } from "./components/Icons";
import { MetricStrip } from "./components/MetricStrip";
import { OverviewPanel } from "./components/OverviewPanel";
import { SecurityPanel } from "./components/SecurityPanel";
import { ProductDemo } from "./components/ProductDemo";
import type { Catalog, Endpoint } from "./types";
import "./product.css";

type Page = "product" | "overview" | "catalog" | "analysis" | "security";
const catalog = catalogData as unknown as Catalog;
const PAGE_SIZE = 12;

const navigation: Array<{ id: Page; label: string; icon: typeof OverviewIcon }> = [
  { id: "product", label: "产品样例", icon: OverviewIcon },
  { id: "overview", label: "总览", icon: OverviewIcon },
  { id: "catalog", label: "接口目录", icon: ApiIcon },
  { id: "analysis", label: "数据分析", icon: AnalysisIcon },
  { id: "security", label: "安全说明", icon: ShieldIcon },
];

function downloadDocumentation() {
  const blob = new Blob([documentation], { type: "text/markdown;charset=utf-8" });
  const link = document.createElement("a");
  link.href = URL.createObjectURL(blob);
  link.download = "STOCK-INFORMATION-API-CATALOG.md";
  link.click();
  URL.revokeObjectURL(link.href);
}

function CatalogPanel() {
  const [search, setSearch] = useState("");
  const [method, setMethod] = useState("ALL");
  const [category, setCategory] = useState("ALL");
  const [state, setState] = useState("ALL");
  const [page, setPage] = useState(1);
  const [selected, setSelected] = useState<Endpoint>(() => catalog.endpoints[0]);

  const filtered = useMemo(() => {
    const term = search.trim().toLowerCase();
    return catalog.endpoints.filter((endpoint) => {
      const matchesTerm = !term || `${endpoint.host}${endpoint.path}${endpoint.purpose}${endpoint.category}`.toLowerCase().includes(term);
      return matchesTerm
        && (method === "ALL" || endpoint.method === method)
        && (category === "ALL" || endpoint.category === category)
        && (state === "ALL" || (state === "OK" ? endpoint.usable : !endpoint.usable));
    });
  }, [search, method, category, state]);

  const pageCount = Math.max(1, Math.ceil(filtered.length / PAGE_SIZE));
  const visible = filtered.slice((page - 1) * PAGE_SIZE, page * PAGE_SIZE);
  const pageNumbers = Array.from({ length: Math.min(pageCount, 5) }, (_, index) => {
    const firstPage = Math.min(Math.max(1, page - 2), Math.max(1, pageCount - 4));
    return firstPage + index;
  });
  useEffect(() => setPage(1), [search, method, category, state]);
  useEffect(() => { if (page > pageCount) setPage(pageCount); }, [page, pageCount]);
  useEffect(() => {
    if (filtered.length > 0 && !filtered.some((endpoint) => endpoint.id === selected.id)) {
      setSelected(filtered[0]);
    }
  }, [filtered, selected.id]);

  function resetFilters() {
    setSearch(""); setMethod("ALL"); setCategory("ALL"); setState("ALL");
  }

  return <main className="catalog-layout">
    <section className="catalog-main">
      <MetricStrip metrics={[
        { label: "接口", value: catalog.summary.endpointCount },
        { label: "可用", value: catalog.summary.usableCount, tone: "success" },
        { label: "GET", value: catalog.summary.getCount },
        { label: "POST", value: catalog.summary.postCount, tone: "post" },
      ]} />
      <div className="filter-bar">
        <label className="search-control"><SearchIcon /><input value={search} onChange={(event) => setSearch(event.target.value)} placeholder="搜索域名、路径或用途" aria-label="搜索接口" /></label>
        <label><span>方法</span><select value={method} onChange={(event) => setMethod(event.target.value)}><option value="ALL">全部</option><option>GET</option><option>POST</option></select></label>
        <label><span>用途</span><select value={category} onChange={(event) => setCategory(event.target.value)}><option value="ALL">全部</option>{catalog.summary.categoryCounts.map((item) => <option key={item.name}>{item.name}</option>)}</select></label>
        <label><span>成功状态</span><select value={state} onChange={(event) => setState(event.target.value)}><option value="ALL">全部</option><option value="OK">可用</option><option value="FAILED">业务失败</option></select></label>
        <button className="reset-button" onClick={resetFilters} type="button">重置</button>
      </div>
      <EndpointTable endpoints={visible} selectedId={selected.id} onSelect={setSelected} />
      <footer className="table-footer"><span>共 {filtered.length} 条</span><div className="pagination"><button disabled={page === 1} onClick={() => setPage((value) => value - 1)} type="button">上一页</button>{pageNumbers.map((pageNumber) => <button className={page === pageNumber ? "current" : ""} aria-current={page === pageNumber ? "page" : undefined} onClick={() => setPage(pageNumber)} key={pageNumber} type="button">{pageNumber}</button>)}<button disabled={page === pageCount} onClick={() => setPage((value) => value + 1)} type="button">下一页</button></div><span>{PAGE_SIZE} 条/页</span></footer>
    </section>
    <EndpointInspector endpoint={selected} />
  </main>;
}

export function App() {
  const [page, setPage] = useState<Page>("product");
  if (page === "product") return <ProductDemo orderBooks={catalog.orderBooks} newsSnapshots={catalog.newsSnapshots} communitySnapshots={catalog.communitySnapshots} onOpenCatalog={() => setPage("catalog")} />;
  return <div className="app-shell">
    <header className="topbar">
      <div><h1>股票资讯接口观察台</h1><p>只保留行情、新闻、社区资讯与 IPO 数据接口</p></div>
      <button className="export-button" onClick={downloadDocumentation} type="button"><ExportIcon />导出文档</button>
    </header>
    <aside className="sidebar">
      <nav aria-label="主要页面">
        {navigation.map((item) => {
          const Icon = item.icon;
          return <button className={page === item.id ? "active" : ""} onClick={() => setPage(item.id)} key={item.id} type="button"><Icon />{item.label}</button>;
        })}
      </nav>
      <div className="sidebar-foot"><span>HAR</span><small>OFFLINE</small></div>
    </aside>
    <div className="workspace">
      {page === "overview" && <OverviewPanel catalog={catalog} onOpenCatalog={() => setPage("catalog")} />}
      {page === "catalog" && <CatalogPanel />}
      {page === "analysis" && <AnalysisPanel catalog={catalog} />}
      {page === "security" && <SecurityPanel catalog={catalog} />}
    </div>
  </div>;
}
