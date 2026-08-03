/* QuantDesk 前端逻辑 */
const $ = s => document.querySelector(s);
const state = {
  overview: [], watchlist: new Set(), lastAlertId: 0,
  modal: { symbol: null, tf: "1h" },
  sound: true, notifyOn: false,
};

/* ---------- 工具 ---------- */
const fmtPrice = p => p == null ? "--" : (p >= 100 ? p.toFixed(2) : p >= 1 ? p.toFixed(3) : p.toFixed(5));
const fmtPct = p => p == null ? "--" : (p >= 0 ? "+" : "") + p.toFixed(2) + "%";
const scoreColor = s => s == null ? "#77808f" : s >= 60 ? "#2ebd85" : s <= -60 ? "#f6465d" : s > 15 ? "#7fc8a9" : s < -15 ? "#e98a97" : "#77808f";
const clsPct = p => p == null ? "dim" : p >= 0 ? "up" : "down";
const timeStr = ts => new Date(ts * 1000).toLocaleString("zh-CN", { hour12: false });
const labelOf = s => s == null ? ["数据不足", "#77808f"] :
  s >= 75 ? ["强烈看多", "#2ebd85"] : s >= 40 ? ["看多", "#7fc8a9"] :
  s <= -75 ? ["强烈看空", "#f6465d"] : s <= -40 ? ["看空", "#e98a97"] : ["中性观望", "#77808f"];

async function api(path, opts) {
  const r = await fetch(path, opts);
  return r.json();
}

/* ---------- 音效 ---------- */
let actx;
function beep(freq = 880, dur = 0.15, times = 1) {
  if (!state.sound) return;
  try {
    actx = actx || new (window.AudioContext || window.webkitAudioContext)();
    for (let i = 0; i < times; i++) {
      const o = actx.createOscillator(), g = actx.createGain();
      o.frequency.value = freq; o.type = "sine";
      g.gain.setValueAtTime(0.12, actx.currentTime + i * 0.22);
      g.gain.exponentialRampToValueAtTime(0.001, actx.currentTime + i * 0.22 + dur);
      o.connect(g).connect(actx.destination);
      o.start(actx.currentTime + i * 0.22); o.stop(actx.currentTime + i * 0.22 + dur);
    }
  } catch (e) {}
}

/* ---------- 网格 ---------- */
function renderGrid() {
  const kw = $("#search").value.trim().toUpperCase();
  const f = $("#filter").value, s = $("#sort").value;
  let list = state.overview.filter(o => !kw || o.symbol.includes(kw));
  if (f === "mine") list = list.filter(o => o.watch || o.position);
  if (f === "long") list = list.filter(o => o.score != null && o.score >= 60);
  if (f === "short") list = list.filter(o => o.score != null && o.score <= -60);
  list.sort((a, b) => {
    const wa = (a.position ? 2 : 0) + (a.watch ? 1 : 0), wb = (b.position ? 2 : 0) + (b.watch ? 1 : 0);
    if (wa !== wb) return wb - wa;
    if (s === "pct") return (b.pct_24h ?? -999) - (a.pct_24h ?? -999);
    if (s === "alpha") return a.symbol.localeCompare(b.symbol);
    return Math.abs(b.score ?? 0) - Math.abs(a.score ?? 0);
  });
  $("#sym-count").textContent = `${list.length}/${state.overview.length}`;
  $("#grid").innerHTML = list.map(o => {
    const sc = o.score;
    const [lb, lbColor] = labelOf(sc);
    const barW = Math.min(Math.abs(sc ?? 0), 100) / 2;
    const barStyle = sc == null ? "" :
      sc >= 0 ? `left:50%;width:${barW}%;background:#2ebd85` : `right:50%;width:${barW}%;background:#f6465d`;
    const pos = o.position;
    return `<div class="card ${pos ? "held" : ""} ${sc >= 60 ? "alert-long" : sc <= -60 ? "alert-short" : ""}" data-sym="${o.symbol}">
      <div class="sym">${o.symbol.replace("USDT", "")}<span class="tags">${o.watch ? "⭐" : ""}${pos ? "💼" : ""}${o.trending ? "🔥" : ""}</span></div>
      <div class="score-num" style="color:${lbColor}">${lb}</div>
      <div class="price">${fmtPrice(o.price)}</div>
      <div class="pct ${clsPct(o.pct_24h)}">${fmtPct(o.pct_24h)} ${sc == null ? "" : `<span class="dim">(${sc > 0 ? "+" : ""}${sc})</span>`}</div>
      ${pos ? `<div class="pct ${pos.upnl >= 0 ? "up" : "down"}">💼 ${pos.side === "LONG" ? "多" : "空"} ${pos.leverage}x ${pos.upnl >= 0 ? "+" : ""}${pos.upnl.toFixed(2)}</div>` : ""}
      <div class="scorebar"><i style="${barStyle}"></i></div>
    </div>`;
  }).join("");
  document.querySelectorAll(".card").forEach(c => c.onclick = () => openModal(c.dataset.sym));
}

/* ---------- 提醒 ---------- */
function renderAlerts(alerts) {
  $("#alerts").innerHTML = alerts.map(a =>
    `<div class="alert-item ${a.direction} ${a.read ? "" : "unread"}" data-sym="${a.symbol}">
      <div>${a.message}</div><div class="t">${timeStr(a.ts)}</div>
    </div>`).join("") || '<div class="dim" style="padding:10px">暂无信号</div>';
  document.querySelectorAll(".alert-item").forEach(el => el.onclick = () => openModal(el.dataset.sym));
}

async function pollAlerts() {
  const alerts = await api("/api/alerts?limit=80");
  renderAlerts(alerts);
  const newest = alerts[0];
  if (newest && state.lastAlertId && newest.id > state.lastAlertId) {
    const fresh = alerts.filter(a => a.id > state.lastAlertId);
    for (const a of fresh.reverse()) {
      beep(a.direction === "long" ? 980 : 420, 0.18, a.direction === "long" ? 2 : 3);
      if (state.notifyOn && Notification.permission === "granted")
        new Notification("QuantDesk 信号", { body: a.message });
    }
  }
  if (newest) state.lastAlertId = newest.id;
}

/* ---------- 舆情 ---------- */
let newsScrollTimer = null;

function startNewsAutoScroll() {
  stopNewsAutoScroll();
  const box = $("#news");
  newsScrollTimer = setInterval(() => {
    if (box.dataset.hover === "1") return;         // 悬停暂停
    if (box.scrollHeight <= box.clientHeight) return; // 内容不足一屏不滚
    box.scrollTop += 1;
    // 滚到底（越过第一份内容）则无缝回卷
    const half = box.scrollHeight / 2;
    if (box.dataset.dup === "1" && box.scrollTop >= half) box.scrollTop -= half;
  }, 60);
}
function stopNewsAutoScroll() { if (newsScrollTimer) { clearInterval(newsScrollTimer); newsScrollTimer = null; } }

async function pollNews() {
  const news = await api("/api/news?limit=60");
  $("#news-count").textContent = news.length ? `${news.length}条` : "";
  const box = $("#news");
  const itemHtml = news.map(n =>
    `<div class="news-item">
      <span class="src">${n.source}</span><span class="t">${timeStr(n.ts)}</span>
      <span class="sent ${n.sentiment}">${{ bull: "利好", bear: "利空", neutral: "中性" }[n.sentiment] || ""}</span>
      <div class="zh"><a href="${n.link}" target="_blank">${n.title_zh || n.title}</a></div>
      ${n.title_zh && n.lang === "en" ? `<div class="t">${n.title}</div>` : ""}
    </div>`).join("");
  // 内容超过一屏时复制一份实现无缝循环滚动
  const needDup = news.length >= 8;
  box.dataset.dup = needDup ? "1" : "0";
  box.innerHTML = itemHtml
    ? (needDup ? itemHtml + itemHtml : itemHtml)
    : '<div class="dim" style="padding:10px">舆情模块加载中…</div>';
  box.onmouseenter = () => box.dataset.hover = "1";
  box.onmouseleave = () => box.dataset.hover = "0";
  startNewsAutoScroll();
}

/* ---------- K线图 ---------- */
function drawChart(canvas, klines) {
  const ctx = canvas.getContext("2d");
  const W = canvas.width, H = canvas.height;
  ctx.clearRect(0, 0, W, H);
  if (!klines.length) { ctx.fillStyle = "#77808f"; ctx.fillText("数据加载中…", 20, 30); return; }
  const pad = { l: 10, r: 70, t: 10, b: 46 };
  const volH = 36, priceH = H - pad.t - pad.b - volH;
  const n = klines.length;
  const hi = Math.max(...klines.map(k => k.high)), lo = Math.min(...klines.map(k => k.low));
  const rng = hi - lo || 1;
  const x = i => pad.l + (i + 0.5) * (W - pad.l - pad.r) / n;
  const y = v => pad.t + (hi - v) / rng * priceH;
  // 网格
  ctx.strokeStyle = "#1e2530"; ctx.fillStyle = "#77808f"; ctx.font = "11px sans-serif";
  for (let i = 0; i <= 4; i++) {
    const v = hi - rng * i / 4, yy = y(v);
    ctx.beginPath(); ctx.moveTo(pad.l, yy); ctx.lineTo(W - pad.r, yy); ctx.stroke();
    ctx.fillText(fmtPrice(v), W - pad.r + 6, yy + 4);
  }
  // MA
  const ma = (period, color) => {
    ctx.strokeStyle = color; ctx.lineWidth = 1.2; ctx.beginPath();
    let started = false;
    for (let i = period - 1; i < n; i++) {
      let s = 0; for (let j = i - period + 1; j <= i; j++) s += klines[j].close;
      const yy = y(s / period);
      started ? ctx.lineTo(x(i), yy) : ctx.moveTo(x(i), yy); started = true;
    }
    ctx.stroke(); ctx.lineWidth = 1;
  };
  ma(20, "#f0b90b"); ma(50, "#b37feb");
  // 蜡烛
  const bw = Math.max(2, (W - pad.l - pad.r) / n * 0.6);
  const vmax = Math.max(...klines.map(k => k.volume)) || 1;
  klines.forEach((k, i) => {
    const up = k.close >= k.open;
    ctx.strokeStyle = ctx.fillStyle = up ? "#2ebd85" : "#f6465d";
    ctx.beginPath(); ctx.moveTo(x(i), y(k.high)); ctx.lineTo(x(i), y(k.low)); ctx.stroke();
    const y1 = y(Math.max(k.open, k.close)), y2 = y(Math.min(k.open, k.close));
    ctx.fillRect(x(i) - bw / 2, y1, bw, Math.max(1, y2 - y1));
    // 量
    ctx.globalAlpha = 0.5;
    const vh = k.volume / vmax * volH;
    ctx.fillRect(x(i) - bw / 2, H - pad.b + volH - vh - 8, bw, vh);
    ctx.globalAlpha = 1;
  });
  // 时间轴
  ctx.fillStyle = "#77808f";
  [0, Math.floor(n / 2), n - 1].forEach(i => {
    const d = new Date(klines[i].open_time);
    ctx.fillText(`${d.getMonth() + 1}/${d.getDate()} ${String(d.getHours()).padStart(2, "0")}:00`, x(i) - 24, H - 8);
  });
  ctx.fillStyle = "#f0b90b"; ctx.fillText("MA20", pad.l + 6, pad.t + 12);
  ctx.fillStyle = "#b37feb"; ctx.fillText("MA50", pad.l + 48, pad.t + 12);
}

/* ---------- 详情弹窗 ---------- */
async function openModal(sym) {
  state.modal.symbol = sym;
  $("#modal").classList.remove("hidden");
  await refreshModal();
}
async function refreshModal() {
  const sym = state.modal.symbol, tf = state.modal.tf;
  const o = state.overview.find(x => x.symbol === sym) || {};
  $("#m-symbol").textContent = sym;
  $("#m-price").textContent = fmtPrice(o.price);
  $("#m-pct").textContent = fmtPct(o.pct_24h);
  $("#m-pct").className = clsPct(o.pct_24h);
  $("#m-position").textContent = o.position ? `💼 ${o.position.side === "LONG" ? "多" : "空"} ${o.position.leverage}x 盈亏 ${o.position.upnl.toFixed(2)}` : "";
  $("#m-watch").textContent = o.watch ? "★" : "☆";
  $("#m-watch").className = o.watch ? "on" : "";
  document.querySelectorAll(".tf-switch button").forEach(b => b.classList.toggle("on", b.dataset.tf === tf));
  const [klines, scoreDetail, report] = await Promise.all([
    api(`/api/klines?symbol=${sym}&tf=${tf}&limit=120`),
    api(`/api/score?symbol=${sym}`),
    api(`/api/report?symbol=${sym}`),
  ]);
  drawChart($("#chart"), klines);
  // 评分摘要（含明确结论）
  const chips = Object.entries(scoreDetail).map(([t, d]) =>
    `<span class="tf-chip" style="color:${scoreColor(d.score)}">${t}: ${d.score > 0 ? "+" : ""}${d.score}</span>`).join("");
  $("#m-score-summary").innerHTML =
    `<span class="score-big" style="color:${report.color}">${report.label}</span>
     <span class="dim">综合评分 ${report.combined == null ? "--" : (report.combined > 0 ? "+" : "") + report.combined}（15m/1h/4h 加权）</span>${chips}
     ${Object.entries(report.stats || {}).map(([k, v]) => `<span class="tf-chip dim">${k}：${v}</span>`).join("")}
     ${report.social && report.social.stocktwits ? `<span class="tf-chip" style="color:${report.social.st_bull_pct >= 60 ? "#2ebd85" : report.social.st_bull_pct <= 40 ? "#f6465d" : "#f0b90b"}">🗣 Stocktwits ${report.social.stocktwits}</span>` : ""}
     ${report.social && report.social.reddit ? `<span class="tf-chip dim">💬 ${report.social.reddit}</span>` : ""}
     ${report.social && report.social.trending ? `<span class="tf-chip" style="color:#f0b90b">${report.social.trending}</span>` : ""}`;
  // 三档操作建议报告
  $("#m-report").innerHTML = report.horizons.map(h => `
    <div class="hz" style="border-left-color:${h.color}">
      <div class="hz-head">
        <span class="hz-name">⏱ ${h.name}</span>
        <span class="hz-sug" style="color:${h.color}">${h.suggestion}</span>
        ${h.score != null ? `<span class="hz-score">周期分 ${h.score > 0 ? "+" : ""}${h.score}</span>` : ""}
      </div>
      ${h.levels ? `<div class="lv">${Object.entries(h.levels).map(([k, v]) => `<span class="dim">${k}：<b>${v}</b></span>`).join("")}</div>` : ""}
      <div class="hz-sec"><div class="sec-t">📐 理论依据</div><ul>${(h.basis || []).map(b => `<li>${b}</li>`).join("")}</ul></div>
      <div class="hz-sec"><div class="sec-t">📰 新闻支撑${report.news_direct ? "" : "（近期无直接相关新闻，以下为宏观背景）"}</div>
        <ul>${(h.news || []).length ? h.news.map(n =>
          `<li><a href="${n.link}" target="_blank">${n.title_zh || n.title}</a>
           <span class="sent ${n.sentiment}">${{ bull: "利好", bear: "利空", neutral: "中性" }[n.sentiment] || ""}</span>
           <span class="dim">${n.source} · ${timeStr(n.ts)}</span></li>`).join("")
          : "<li class='dim'>近 48 小时无相关资讯</li>"}</ul></div>
    </div>`).join("") + `<div class="disclaimer">⚠️ ${report.disclaimer}</div>`;
  // 因子明细（当前周期）
  const cur = scoreDetail[tf];
  $("#m-factors").innerHTML = cur ? cur.factors.map(f => {
    const fvText = f.weight === 0 ? "参考" :
      f.contribution === 0 ? '<span style="color:#77808f">中性</span>' :
      f.contribution > 0 ? `+${f.contribution}` : `${f.contribution}`;
    const fvColor = f.weight === 0 ? "#8ab4f8" : f.contribution > 0 ? "#2ebd85" : f.contribution < 0 ? "#f6465d" : "#77808f";
    return `<div class="factor">
      <span class="fw">${f.name}</span>
      <span class="fv" style="color:${fvColor}">${fvText}</span>
      <span class="fr">${f.reason}<span class="dim">（${f.weight === 0 ? "信息项" : "权重 " + f.weight}）</span></span>
    </div>`;
  }).join("") : '<div class="dim" style="padding:8px">该周期评分尚未生成（等待K线收盘后计算）</div>';
}
$("#m-close").onclick = () => $("#modal").classList.add("hidden");
$("#modal").onclick = e => { if (e.target.id === "modal") $("#modal").classList.add("hidden"); };
document.querySelectorAll(".tf-switch button").forEach(b => b.onclick = () => { state.modal.tf = b.dataset.tf; refreshModal(); });
$("#m-watch").onclick = async () => {
  const sym = state.modal.symbol;
  state.watchlist.has(sym) ? state.watchlist.delete(sym) : state.watchlist.add(sym);
  await api("/api/watchlist", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ symbols: [...state.watchlist] }) });
  await pollOverview(); refreshModal();
};

/* ---------- 顶栏 ---------- */
$("#btn-sound").onclick = e => { state.sound = !state.sound; e.target.classList.toggle("on", state.sound); e.target.textContent = state.sound ? "🔊 音效" : "🔇 静音"; };
$("#btn-notify").onclick = async e => {
  const p = await Notification.requestPermission();
  state.notifyOn = p === "granted";
  e.target.classList.toggle("on", state.notifyOn);
};
$("#btn-clear-alerts").onclick = async () => { await api("/api/alerts/read", { method: "POST" }); pollAlerts(); };
["search", "filter", "sort"].forEach(id => $("#" + id).oninput = renderGrid);
setInterval(() => $("#clock").textContent = new Date().toLocaleTimeString("zh-CN", { hour12: false }), 1000);

/* ---------- 轮询 ---------- */
async function pollOverview() {
  try {
    state.overview = await api("/api/overview");
    state.watchlist = new Set((await api("/api/watchlist")));
    renderGrid();
    const br = await api("/api/breadth");
    const bel = $("#breadth");
    bel.textContent = "🧭 " + br.conclusion;
    bel.style.color = br.color;
    $("#engine-state").textContent = "● 运行中";
    $("#engine-state").className = "badge ok";
  } catch (e) {
    $("#engine-state").textContent = "● 连接失败";
    $("#engine-state").className = "badge err";
  }
}
async function boot() {
  await pollOverview(); await pollAlerts(); await pollNews();
  setInterval(pollOverview, 2000);
  setInterval(pollAlerts, 5000);
  setInterval(pollNews, 5000);
}
boot();

/* ---------- 模拟盘 ---------- */
let paperTimer = null;

function drawPaperCurve(canvas, curve, start) {
  const ctx = canvas.getContext("2d");
  const W = canvas.width, H = canvas.height;
  ctx.clearRect(0, 0, W, H);
  const pad = { l: 10, r: 80, t: 14, b: 24 };
  if (!curve.length) {
    ctx.fillStyle = "#77808f"; ctx.font = "13px sans-serif";
    ctx.fillText("权益数据积累中（每分钟记录一次）…", 20, 40); return;
  }
  const vals = curve.map(c => c[1]).concat([start]);
  const hi = Math.max(...vals), lo = Math.min(...vals);
  const rng = (hi - lo) || 1;
  const x = i => pad.l + i * (W - pad.l - pad.r) / Math.max(curve.length - 1, 1);
  const y = v => pad.t + (hi - v) / rng * (H - pad.t - pad.b);
  // 网格 + 起始线
  ctx.strokeStyle = "#1e2530"; ctx.fillStyle = "#77808f"; ctx.font = "11px sans-serif";
  for (let i = 0; i <= 4; i++) {
    const v = hi - rng * i / 4, yy = y(v);
    ctx.beginPath(); ctx.moveTo(pad.l, yy); ctx.lineTo(W - pad.r, yy); ctx.stroke();
    ctx.fillText(v.toFixed(0), W - pad.r + 6, yy + 4);
  }
  ctx.strokeStyle = "#f0b90b"; ctx.setLineDash([4, 4]);
  ctx.beginPath(); ctx.moveTo(pad.l, y(start)); ctx.lineTo(W - pad.r, y(start)); ctx.stroke();
  ctx.setLineDash([]);
  ctx.fillStyle = "#f0b90b"; ctx.fillText(`本金 ${start}`, pad.l + 4, y(start) - 5);
  // 曲线（低于本金红、高于绿）
  const lastV = curve[curve.length - 1][1];
  ctx.strokeStyle = lastV >= start ? "#2ebd85" : "#f6465d";
  ctx.lineWidth = 1.8; ctx.beginPath();
  curve.forEach((c, i) => i ? ctx.lineTo(x(i), y(c[1])) : ctx.moveTo(x(i), y(c[1])));
  ctx.stroke(); ctx.lineWidth = 1;
  // 填充
  ctx.globalAlpha = 0.12; ctx.fillStyle = ctx.strokeStyle;
  ctx.lineTo(x(curve.length - 1), H - pad.b); ctx.lineTo(x(0), H - pad.b); ctx.closePath(); ctx.fill();
  ctx.globalAlpha = 1;
  // 时间轴
  ctx.fillStyle = "#77808f";
  [0, Math.floor(curve.length / 2), curve.length - 1].forEach(i => {
    const d = new Date(curve[i][0] * 1000);
    ctx.fillText(`${d.getMonth() + 1}/${d.getDate()} ${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`, x(i) - 20, H - 6);
  });
}

async function loadPaper() {
  let d;
  try { d = await api("/api/paper"); } catch (e) { return; }
  const a = d.account, s = d.stats;
  const pnlCls = v => v >= 0 ? "up" : "down";
  const sign = v => (v >= 0 ? "+" : "") + v.toFixed(2);
  $("#paper-cards").innerHTML = [
    ["账户权益", a.equity.toFixed(2) + " U", a.ret_pct >= 0 ? "#2ebd85" : "#f6465d", `收益率 ${sign(a.ret_pct)}%`],
    ["可用余额", a.balance.toFixed(2) + " U", "", `占用保证金 ${a.used_margin.toFixed(2)} U（${a.margin_usage}%）`],
    ["浮动盈亏", sign(a.upnl) + " U", a.upnl >= 0 ? "#2ebd85" : "#f6465d", `今日盈亏 ${sign(a.today_pnl)} U`],
    ["已实现盈亏", sign(s.realized) + " U", s.realized >= 0 ? "#2ebd85" : "#f6465d", `共 ${s.trades} 笔（${s.wins}胜/${s.losses}负）`],
    ["胜率", s.trades ? s.win_rate + "%" : "--", "#f0b90b", `盈亏比 ${s.profit_factor ?? "--"}`],
    ["最大回撤", s.max_drawdown + "%", s.max_drawdown > 10 ? "#f6465d" : "#f0b90b", `仓位 ${d.positions.length}/${a.max_positions}`],
  ].map(([t, v, c, sub]) => `<div class="pcard"><div class="pt">${t}</div><div class="pv" style="color:${c || "#e8ecf3"}">${v}</div><div class="ps dim">${sub}</div></div>`).join("");
  $("#paper-rules").textContent = `${d.rules.tiers} ｜ ${d.rules.exits} ｜ ${d.rules.limits}`;
  $("#paper-disclaimer").textContent = "⚠️ " + d.disclaimer + " 成本模型：" + d.rules.costs;
  drawPaperCurve($("#paper-chart"), d.curve, a.start);
  // 持仓表
  $("#paper-pos-count").textContent = `${d.positions.length}个`;
  $("#paper-positions").innerHTML = d.positions.length ? `<table><thead><tr>
      <th>合约</th><th>方向</th><th>数量</th><th>均价</th><th>现价</th><th>保证金</th>
      <th>浮盈</th><th>止损/目标</th><th>强平价</th><th>持仓</th><th>开仓依据</th></tr></thead><tbody>` +
    d.positions.map(p => `<tr>
      <td><b>${p.symbol.replace("USDT", "")}</b>${p.adds ? `<span class="dim">+${p.adds}加</span>` : ""}${p.tp_done ? `<span title="已分批止盈">💰</span>` : ""}</td>
      <td style="color:${p.side > 0 ? "#2ebd85" : "#f6465d"}">${p.side > 0 ? "多" : "空"} ${p.leverage}x</td>
      <td>${p.qty}</td><td>${fmtPrice(p.avg_entry)}</td><td>${fmtPrice(p.price)}</td>
      <td>${p.margin}</td>
      <td class="${pnlCls(p.upnl)}">${sign(p.upnl)}<br><small>${sign(p.pnl_pct)}%</small></td>
      <td class="dim"><small>止 ${fmtPrice(p.stop)}<br>目 ${p.target ? fmtPrice(p.target) : "--"}</small></td>
      <td style="color:${p.liq_dist != null && p.liq_dist < 3 ? "#f6465d" : "#77808f"}"><small>${p.liq_price ? fmtPrice(p.liq_price) : "--"}<br>${p.liq_dist != null ? "距" + p.liq_dist + "%" : ""}</small></td>
      <td class="dim">${p.hold_h}h</td>
      <td class="dim"><small>评分${p.open_score > 0 ? "+" : ""}${p.open_score}｜${(p.reasons[0] || "").slice(0, 40)}</small></td>
    </tr>`).join("") + "</tbody></table>"
    : '<div class="dim" style="padding:14px">暂无持仓——等待信号评分达到 ±60 自动开仓</div>';
  // 成交表
  $("#paper-trade-count").textContent = `${d.trades.length}笔`;
  $("#paper-trades").innerHTML = d.trades.length ? `<table><thead><tr>
      <th>时间</th><th>合约</th><th>方向</th><th>开→平</th><th>盈亏</th><th>平仓原因</th></tr></thead><tbody>` +
    d.trades.map(t => `<tr>
      <td class="dim"><small>${timeStr(t.closed_ts)}</small></td>
      <td><b>${t.symbol.replace("USDT", "")}</b></td>
      <td style="color:${t.side > 0 ? "#2ebd85" : "#f6465d"}">${t.side > 0 ? "多" : "空"}</td>
      <td class="dim"><small>${fmtPrice(t.entry_price)} → ${fmtPrice(t.exit_price)}</small></td>
      <td class="${pnlCls(t.pnl - t.fee)}">${sign(t.pnl - t.fee)} U</td>
      <td class="dim">${t.reason}</td>
    </tr>`).join("") + "</tbody></table>"
    : '<div class="dim" style="padding:14px">暂无成交记录</div>';
}

$("#btn-paper").onclick = () => {
  $("#paper-page").classList.remove("hidden");
  loadPaper();
  paperTimer = setInterval(loadPaper, 10000);
};
$("#paper-close").onclick = () => {
  $("#paper-page").classList.add("hidden");
  if (paperTimer) { clearInterval(paperTimer); paperTimer = null; }
};
$("#paper-reset").onclick = async () => {
  if (!confirm("确定重置模拟盘？将清空所有持仓与历史，回到 10,000 USDT。")) return;
  await api("/api/paper/reset", { method: "POST" });
  loadPaper();
};
