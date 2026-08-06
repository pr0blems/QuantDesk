class ProxyDashboard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this.running = false;
    this.state = null;
    this.render();
  }

  connectedCallback() { this.bind(); }
  disconnectedCallback() { this.pause(); }

  render() {
    this.shadowRoot.innerHTML = `
      <style>
        :host{display:block;color:var(--text,#edf4ef)}*{box-sizing:border-box}.wrap{max-width:1280px;margin:auto;padding:6px 0 30px}.head{display:flex;justify-content:space-between;gap:20px;align-items:end;border-bottom:1px solid var(--line,#26332a);padding-bottom:20px}.eyebrow{font:700 11px/1 system-ui;letter-spacing:.13em;color:#6ee7a7}.head h1{font-size:30px;margin:7px 0}.head p,.hint{color:var(--muted,#9aa59e);margin:0;line-height:1.6}.state{border:1px solid #2d7655;border-radius:999px;padding:8px 12px;font-weight:700;font-size:12px}.grid{display:grid;grid-template-columns:1.2fr .8fr;gap:16px;margin-top:18px}.card{background:var(--card,#121714);border:1px solid var(--line,#28342b);border-radius:14px;padding:18px}.card h2{font-size:15px;margin:0 0 10px}.metrics{display:grid;grid-template-columns:repeat(3,1fr);gap:10px}.metric{background:var(--field,#0b100d);border-radius:10px;padding:12px}.metric span{display:block;color:var(--muted,#9aa59e);font-size:12px}.metric strong{display:block;margin-top:5px;font-size:17px}.list{display:grid;gap:8px;margin-top:12px}.node{display:grid;grid-template-columns:1fr auto auto;align-items:center;gap:10px;background:var(--field,#0b100d);border-radius:10px;padding:11px}.node small{display:block;color:var(--muted,#9aa59e);margin-top:3px}.ok{color:#67e8a7}.bad{color:#fb7185}.unknown{color:#facc15}button{border:1px solid var(--line,#344137);background:transparent;color:inherit;border-radius:8px;padding:7px 10px;cursor:pointer}button.primary{background:#1f9d63;border-color:#1f9d63;color:#08110c;font-weight:800}button:disabled{opacity:.45;cursor:not-allowed}.forms{display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-top:16px}label{display:grid;gap:5px;font-size:12px;color:var(--muted,#9aa59e)}input,select,textarea{width:100%;color:inherit;background:var(--field,#0b100d);border:1px solid var(--line,#344137);border-radius:8px;padding:9px;font:inherit}textarea{min-height:120px;resize:vertical}.row{display:flex;gap:8px;align-items:center;margin-top:10px}.message{min-height:20px;margin-top:10px;font-size:13px}.message.error{color:#fb7185}.message.success{color:#67e8a7}@media(max-width:900px){.grid,.forms{grid-template-columns:1fr}.metrics{grid-template-columns:1fr 1fr}.head{align-items:start;flex-direction:column}.node{grid-template-columns:1fr auto}}
      </style>
      <main class="wrap">
        <header class="head"><div><span class="eyebrow">BINANCE COLLECTOR ROUTING</span><h1>代理管理</h1><p>仅后端行情采集流量经代理；浏览器、账户与交易请求不会被转发。节点凭据和订阅认证仅以加密形式保存。</p></div><span id="state" class="state">读取状态中</span></header>
        <section class="grid"><article class="card"><h2>运行路由</h2><div id="metrics" class="metrics"></div><div class="row"><select id="mode"><option value="direct">直连</option><option value="auto">自动优选</option><option value="manual">手动选择</option></select><select id="active"></select><button id="save-runtime" class="primary">应用到采集器</button></div><p class="hint">自动模式仅选择测速成功且延迟最低的节点；没有健康节点时系统自动直连，不中断行情。</p></article><article class="card"><h2>安全边界</h2><p class="hint">订阅内容只在导入时驻留内存，绝不写入数据库或审计日志。HTTP/SOCKS5 以 fstream.binance.com:443 CONNECT 握手测速，不携带 Binance API 凭据。</p><p class="hint">当前版本不下载任意订阅 URL；请在可信网络中手动粘贴订阅内容再导入。</p></article></section>
        <section class="card" style="margin-top:16px"><h2>节点池</h2><div id="nodes" class="list"></div></section>
        <section id="admin-forms" class="forms"><article class="card"><h2>导入 Clash / Base64 订阅</h2><label>订阅名称<input id="subscription-name" maxlength="96" placeholder="例如：主线路"></label><label style="margin-top:8px">订阅内容（YAML 或 Base64 URL 列表）<textarea id="subscription-content" autocomplete="off" spellcheck="false" placeholder="proxies:\n  - name: node-1\n    type: socks5\n    server: proxy.example.com\n    port: 1080"></textarea></label><button id="import" class="primary" style="margin-top:10px">安全导入</button></article><article class="card"><h2>手动添加 HTTP / SOCKS5</h2><label>名称<input id="node-name" maxlength="160" placeholder="香港节点"></label><div class="row"><label>协议<select id="node-protocol"><option value="socks5">SOCKS5</option><option value="http">HTTP CONNECT</option></select></label><label>端口<input id="node-port" type="number" min="1" max="65535" value="1080"></label></div><label style="margin-top:8px">主机<input id="node-host" placeholder="127.0.0.1 或 proxy.example.com"></label><div class="row"><button id="create-node">添加节点</button></div></article></section><p id="message" class="message" role="status"></p>
      </main>`;
  }

  bind() {
    this.$("#save-runtime").addEventListener("click", () => this.saveRuntime());
    this.$("#import").addEventListener("click", () => this.importSubscription());
    this.$("#create-node").addEventListener("click", () => this.createNode());
    this.$("#nodes").addEventListener("click", (event) => {
      const id = event.target.closest("button[data-test]")?.dataset.test;
      if (id) this.testNode(id);
    });
  }
  $(selector) { return this.shadowRoot.querySelector(selector); }
  message(value, kind = "") { const el = this.$("#message"); el.textContent = value; el.className = `message ${kind}`; }
  async request(path, options) { return window.quantdeskApi(path, options); }
  async start() { this.running = true; await this.refresh(); }
  pause() { this.running = false; }
  async refresh() {
    try { this.state = await this.request("/api/v2/proxy/status"); this.draw(); }
    catch (error) { this.message(error.message, "error"); }
  }
  draw() {
    const { runtime, nodes = [], subscriptions = [] } = this.state;
    const selected = nodes.find((node) => node.id === runtime.active_node_id);
    this.$("#state").textContent = runtime.fallback_state === "proxy_active" ? "代理采集已启用" : "安全直连回退";
    this.$("#metrics").innerHTML = `<div class="metric"><span>模式</span><strong>${runtime.selection_mode}</strong></div><div class="metric"><span>活跃节点</span><strong>${selected?.name || "直连"}</strong></div><div class="metric"><span>订阅 / 节点</span><strong>${subscriptions.length} / ${nodes.length}</strong></div>`;
    this.$("#mode").value = runtime.selection_mode;
    this.$("#active").innerHTML = `<option value="">选择节点</option>${nodes.filter((node) => node.enabled).map((node) => `<option value="${node.id}" ${node.id === runtime.active_node_id ? "selected" : ""}>${this.escape(node.name)} · ${node.protocol} · ${node.health_status}</option>`).join("")}`;
    this.$("#nodes").innerHTML = nodes.length ? nodes.map((node) => `<div class="node"><div><strong>${this.escape(node.name)}</strong><small>${this.escape(node.protocol)}://${this.escape(node.host)}:${node.port} · ${node.has_credentials ? "已加密认证" : "无认证"}${node.last_error ? ` · ${this.escape(node.last_error)}` : ""}</small></div><span class="${node.health_status === "healthy" ? "ok" : node.health_status === "unhealthy" ? "bad" : "unknown"}">${node.health_status}${node.last_latency_ms ? ` ${node.last_latency_ms}ms` : ""}</span><button data-test="${node.id}">测速</button></div>`).join("") : `<p class="hint">尚无节点。导入订阅或手动添加后先测速，再启用自动/手动代理。</p>`;
  }
  escape(value) { const node = document.createElement("span"); node.textContent = String(value ?? ""); return node.innerHTML; }
  async saveRuntime() {
    const selection_mode = this.$("#mode").value;
    const active = Number(this.$("#active").value) || null;
    try { await this.request("/api/v2/proxy/runtime", { method: "PUT", body: JSON.stringify({ enabled: selection_mode !== "direct", selection_mode, active_node_id: active }) }); this.message("采集路由已更新", "success"); await this.refresh(); }
    catch (error) { this.message(error.message, "error"); }
  }
  async importSubscription() {
    const name = this.$("#subscription-name").value.trim(); const content = this.$("#subscription-content").value;
    if (!name || !content.trim()) return this.message("请填写订阅名称和内容", "error");
    try { const result = await this.request("/api/v2/proxy/subscriptions", { method: "POST", body: JSON.stringify({ name, content }) }); this.$("#subscription-content").value = ""; this.message(`已导入 ${result.imported_nodes} 个节点，跳过 ${result.skipped_nodes} 个不支持条目`, "success"); await this.refresh(); }
    catch (error) { this.message(error.message, "error"); }
  }
  async createNode() {
    const payload = { name: this.$("#node-name").value.trim(), protocol: this.$("#node-protocol").value, host: this.$("#node-host").value.trim(), port: Number(this.$("#node-port").value) };
    if (!payload.name || !payload.host || !payload.port) return this.message("请完整填写节点信息", "error");
    try { await this.request("/api/v2/proxy/nodes", { method: "POST", body: JSON.stringify(payload) }); this.message("节点已创建，请测速后启用", "success"); await this.refresh(); }
    catch (error) { this.message(error.message, "error"); }
  }
  async testNode(id) { try { await this.request(`/api/v2/proxy/nodes/${id}/test`, { method: "POST" }); this.message("测速完成", "success"); await this.refresh(); } catch (error) { this.message(error.message, "error"); } }
}
customElements.define("proxy-dashboard", ProxyDashboard);
