class BacktestWorkbench extends window.QuantDeskPageController {
  constructor(host) {
    super(host, { shadow: true });
    this.started = false;
    this.loading = false;
    this.runningBacktest = false;
    this.sessionGeneration = 0;
    this.catalog = { strategies: [], symbols: [], timeframes: [], bounds: {} };
    this.symbolOptions = [];
    this.selectedSymbols = [];
    this.draftSymbols = [];
    this.activeConfigSymbol = "";
    this.symbolConfigurations = {};
    this.symbolConfigurationDirty = new Set();
    this.symbolProfileLoaded = new Set();
    this.symbolProfileRequests = new Map();
    this.applyingSymbolConfiguration = false;
    this.profileRequest = 0;
    this.profileSaveInFlight = false;
    this.history = [];
    this.historyLoaded = false;
    this.activeDetail = null;
    this.batchResults = [];
    this.batchFailures = [];
    this.expandedBatchIndex = -1;
    this.batchExpandToken = 0;
    this.lotCalculatorQuote = null;
    this.lotCalculatorRequest = 0;
    this.lotCalculatorManualBoxRange = null;
    this.strategyId = "";
    this.category = "全部";
    this.resizeObserver = null;
    this.priceChartState = {
      dataKey: "",
      candles: [],
      trades: [],
      viewStart: 0,
      viewEnd: 0,
      hover: null,
      layout: null,
      dragging: false,
      pointerId: null,
      dragStartX: 0,
      dragViewStart: 0,
    };
    this.priceChartFrame = 0;
    this.computationProgressTimer = 0;
    this.computationProgressValue = 0;
    this.resultReplayFrame = 0;
    this.resultReplayState = null;
    this.handleStrategiesChanged = () => { this.started = false; };
    this.renderShell();
  }

  connectedCallback() {
    this.bindEvents();
    window.addEventListener("quantdesk:strategies-changed", this.handleStrategiesChanged);
    if ("ResizeObserver" in window && !this.resizeObserver) {
      this.resizeObserver = new ResizeObserver(() => {
        if (this.resultReplayState) this.drawReplayFrame(this.resultReplayState.visibleCandles);
        else if (this.activeDetail) this.drawCharts(this.unpackDetail(this.activeDetail).result);
      });
      this.resizeObserver.observe(this.q("#chart-panel"));
    }
  }

  disconnectedCallback() {
    this.pause();
    window.removeEventListener("quantdesk:strategies-changed", this.handleStrategiesChanged);
    if (this.resizeObserver) this.resizeObserver.disconnect();
    this.resizeObserver = null;
  }

  renderShell() {
    this.shadowRoot.innerHTML = `
      <link rel="stylesheet" href="/next/assets/backtest.css?v=20260905-calculator-dialog-wide-8">
      <main class="backtest-workbench">
        <header class="workbench-head">
          <div class="head-copy">
            <div class="title-line"><span class="title-mark" aria-hidden="true">测</span><h1>数据回测</h1><span class="beta">RESEARCH</span></div>
            <p>用历史行情验证策略逻辑、成本敏感度与风险边界</p>
          </div>
          <div class="integrity-strip" aria-label="回测可信度约束">
            <span><i></i>下一根开盘成交</span><button id="open-backtest-config" class="open-config-button" type="button" aria-controls="backtest-config-dialog" aria-expanded="false"><b>＋</b>开始回测</button><span><i></i>手续费/滑点已计</span><span><i></i>无未来函数</span>
          </div>
        </header>

        <div id="global-banner" class="global-banner hidden" role="status" aria-live="polite"></div>

        <div class="workbench-layout">
          <div id="backtest-config-dialog" class="config-dialog-backdrop hidden" role="presentation">
          <form id="backtest-form" class="config-dialog" role="dialog" aria-modal="true" aria-labelledby="backtest-config-title" novalidate>
            <header class="config-dialog-head">
              <div><span>BACKTEST SETUP</span><h2 id="backtest-config-title">配置并开始回测</h2><p>选择策略、交易品种和资金参数；支持多品种分别回测。</p></div>
              <button id="close-backtest-config" class="dialog-close" type="button" aria-label="关闭回测配置">×</button>
            </header>
            <div class="config-dialog-body">
            <section class="config-section strategy-section">
              <div class="section-head"><div><span class="section-index">01</span><strong>选择策略</strong></div><small id="strategy-count">--</small></div>
              <div id="category-filter" class="category-filter" aria-label="策略分类"></div>
              <div id="strategy-list" class="strategy-list"><div class="rail-loading">正在读取我的策略…</div></div>
              <p id="strategy-description" class="strategy-description">策略参数将随策略中心配置动态加载。</p>
            </section>

            <div class="config-form-grid">
            <section id="market-section" class="config-section">
              <div class="section-head"><div><span class="section-index">02</span><strong>市场与区间</strong></div><small id="data-bound">等候行情目录</small></div>
              <div class="field-grid two">
                <div class="symbol-field">
                  <span class="field-label">交易品种</span>
                  <input id="symbol" name="symbol" type="hidden">
                  <button id="open-symbol-picker" class="symbol-selection-trigger" type="button" aria-controls="symbol-picker-dialog" aria-expanded="false" disabled>
                    <span><strong id="symbol-selection-title">正在加载交易品种…</strong><small id="symbol-selection-summary">请稍候</small></span><b>选择</b>
                  </button>
                  <span id="selected-symbols" class="selected-symbols" aria-label="已选择的交易品种"></span>
                  <small class="field-help">可添加多个品种同时回测；结果分别保存，不混合计算资金。</small>
                </div>
                <label class="market-timeframe-field"><span class="field-label">数据周期</span><select id="timeframe" name="timeframe" required><option value="">加载中…</option></select></label>
              </div>
              <div class="field-grid data-source-fields">
                <label id="market-source-field">行情数据源
                  <select id="market-data-source" name="market_data_source">
                    <option value="auto">自动选择（Tiger 优先，失败转 Binance）</option>
                    <option value="tiger">Tiger 历史优先（不足转 Binance）</option>
                    <option value="binance" selected>仅使用 Binance 合约行情</option>
                  </select>
                  <small id="market-source-help" class="field-help">默认使用 Binance 合约历史 K 线；Tiger 仅在手动选择时启用。</small>
                </label>
              </div>
              <div class="symbol-config-switcher">
                <div class="symbol-config-switcher-head"><span>按交易品种配置</span><small>切换后，下方数据范围、资金、成本与策略参数均独立保存</small></div>
                <div id="symbol-config-tabs" class="symbol-config-tabs" role="tablist" aria-label="按交易品种切换回测参数"></div>
              </div>
              <div id="data-availability" class="data-availability" role="status" aria-live="polite">
                <span>可用数据范围</span>
                <strong id="available-range">正在读取…</strong>
                <small id="available-bars">按当前品种与周期统计</small>
              </div>
              <div class="field-grid two date-fields">
                <label>开始日期<input id="start-date" name="start_date" type="date" required></label>
                <label>结束日期<input id="end-date" name="end_date" type="date" required></label>
              </div>
              <div class="range-presets" aria-label="快速回测区间">
                <button type="button" data-months="3" aria-pressed="false">近 3 月</button><button type="button" data-months="6" aria-pressed="false">6 个月</button><button type="button" data-months="12" aria-pressed="false">近 1 年</button><button type="button" data-months="all" aria-pressed="false">可用最大</button>
              </div>
              <small id="range-feedback" class="range-feedback" role="status" aria-live="polite">点击快捷区间可调整回测日期</small>
            </section>

            <section id="capital-section" class="config-section">
              <div class="section-head"><div><span class="section-index">03</span><strong>资金与仓位</strong></div><small>单标的 · 单仓</small></div>
              <div class="field-grid two">
                <label>初始资金 (USDT)<input id="initial-capital" name="initial_capital" type="number" min="1" step="1" value="10000" required></label>
                <label id="position-field">单次仓位 (%)<input id="position-size" name="position_size_pct" type="number" min="0.01" max="100" step="0.01" value="10" required></label>
                <label id="leverage-field">Binance 杠杆<select id="leverage" name="leverage" required><option value="1">1x</option><option value="2">2x</option><option value="3">3x</option><option value="5">5x</option><option value="10">10x</option><option value="20">20x</option></select></label>
                <label id="holding-field">最大持有 (K线)<input id="max-holding" name="max_holding_bars" type="number" min="0" max="50000" step="1" value="120" required></label>
              </div>
            </section>

            <section id="cost-section" class="config-section">
              <div class="section-head"><div><span class="section-index">04</span><strong>成本与退出</strong></div><small>结果均为净值</small></div>
              <div class="field-grid two">
                <label>手续费 (bp)<input id="fee" name="fee_bps" type="number" min="0" max="1000" step="0.1" value="4" required></label>
                <label>滑点 (bp)<input id="slippage" name="slippage_bps" type="number" min="0" max="1000" step="0.1" value="2" required></label>
                <label id="stop-field">止损 (%)<input id="stop-loss" name="stop_loss_pct" type="number" min="0" max="99.9" step="0.1" value="5" required></label>
                <label id="take-profit-field">止盈 (%)<input id="take-profit" name="take_profit_pct" type="number" min="0" max="99.9" step="0.1" value="10" required></label>
              </div>
            </section>

            <section id="parameter-section" class="config-section hidden">
              <div class="section-head"><div><span class="section-index">05</span><strong>策略参数</strong><button id="lot-calculator-trigger" class="lot-calculator-trigger hidden" type="button" aria-label="打开仓位与止盈计算器">计算器</button></div><button id="reset-params" class="text-button" type="button">恢复默认</button></div>
              <div id="strategy-params" class="strategy-parameter-groups"></div>
            </section>
            </div>

            <div id="basket-profile-note" class="execution-note hidden"><span>篮子回放</span><strong>可选 Tiger / Binance 行情 → 马丁篮子引擎</strong><small>自动模式优先使用 Tiger，无法使用时直接回退 Binance；首单、加仓、分级止盈、金额止损、逐仓杠杆与强平均纳入回放。</small></div>
            <div id="standard-execution-note" class="execution-note"><span>Binance 逐仓模型</span><strong>信号收盘确认 → 下一根开盘撮合</strong><small>手续费、双边滑点、合约价格/数量步进、最小名义价值、止盈止损与强平均计入；暂不含资金费。</small></div>
            </div>
            <footer class="config-dialog-foot">
            <div class="backtest-action-row">
              <button id="save-default-profile" class="profile-save-button" type="button" disabled><strong>保存默认交易策略参数</strong><small>未指定币种时使用</small></button>
              <button id="save-symbol-profile" class="profile-save-button symbol-profile" type="button" disabled><strong>保存专有币种策略参数</strong><small>当前所选币种优先</small></button>
              <button id="run-backtest" class="run-button" type="submit" disabled><span aria-hidden="true">▶</span><strong>运行回测</strong></button>
            </div>
            <div id="profile-status" class="profile-status" role="status" aria-live="polite">选择策略后可加载和保存交易参数。</div>
            </footer>
          </form>
          </div>

          <section class="result-stage">
            <div class="stage-toolbar">
              <div><strong>回测结果</strong><span id="stage-status" class="stage-status idle"><i></i>等待运行</span></div>
              <div class="toolbar-meta"><span id="active-run-meta">选择策略并配置参数</span><button id="open-history" type="button">历史回测数据</button></div>
            </div>

            <section id="batch-result-overview" class="batch-result-overview hidden" aria-label="本次回测结果">
              <div class="batch-result-head">
                <div><span>RUN RESULTS</span><strong>本次回测结果</strong><small id="batch-result-summary">回测结果默认收起，点击交易品种展开完整视图。</small></div>
                <span id="batch-result-count">0 个品种</span>
              </div>
              <div id="batch-result-list" class="batch-result-list"></div>
            </section>

            <div class="stage-layout">
              <div class="result-primary">
                <div id="empty-result" class="empty-result">
                  <div class="flask" aria-hidden="true"><i></i></div>
                  <h2>选择策略并开始回测</h2>
                  <p>先用当前可用区间快速验证逻辑，再随历史数据积累扩大区间。结果会展示收益、回撤、逐笔交易及行情完整度。</p>
                  <div class="empty-checks"><span>01 参数可复现</span><span>02 成本可配置</span><span>03 数据质量可见</span></div>
                </div>

                <div id="running-result" class="running-result hidden" aria-live="polite">
                  <div class="running-orbit" aria-hidden="true"><i></i><i></i><i></i></div>
                  <div class="running-copy">
                    <span>BACKTEST PIPELINE</span>
                    <h2 id="running-title">正在准备历史行情</h2>
                    <p id="running-description">正在提交参数并检查可用数据区间。</p>
                  </div>
                  <div class="running-progress" role="progressbar" aria-label="回测计算阶段" aria-valuemin="0" aria-valuemax="100" aria-valuenow="0"><i id="running-progress-bar"></i></div>
                  <div class="running-progress-meta"><strong id="running-progress-value">0%</strong><span id="running-progress-count">等待服务端响应</span></div>
                  <ol class="running-steps">
                    <li data-running-step="0" class="active"><b>01</b><span>准备行情</span><small>读取 K 线与交易规则</small></li>
                    <li data-running-step="1"><b>02</b><span>指标预热</span><small>建立策略所需窗口</small></li>
                    <li data-running-step="2"><b>03</b><span>策略计算</span><small>服务端逐根执行策略</small></li>
                    <li data-running-step="3"><b>04</b><span>可视化回放</span><small>K 线与成交点逐步呈现</small></li>
                    <li data-running-step="4"><b>05</b><span>结果结算</span><small>核对费用、盈亏与回撤</small></li>
                  </ol>
                  <div class="running-preview"><span></span><span></span><span></span><span></span><span></span><span></span><span></span></div>
                  <p class="running-note">计算阶段显示服务端当前工作状态；收到结果后，将按真实历史时间轴逐根回放。</p>
                </div>

                <div id="result-content" class="result-content hidden">
                  <div class="result-title">
                    <div class="result-identity"><span id="result-kicker">BACKTEST COMPLETE</span><h2 id="result-name">--</h2><p id="result-period">--</p></div>
                    <div class="result-capital-summary" aria-label="回测资金摘要">
                      <article><span>初始资金</span><strong id="result-initial-capital">--</strong><small>本次回测本金</small></article>
                      <article><span>盈利金额</span><strong id="result-net-profit">--</strong><small>已扣手续费与滑点</small></article>
                    </div>
                    <span id="result-return" class="return-badge">--</span>
                  </div>
                  <section id="metric-grid" class="metric-grid" aria-label="回测核心指标"></section>

                  <section id="chart-panel" class="result-panel chart-panel">
                    <div class="panel-head"><div><strong>权益与回撤</strong><span>净值曲线与水下回撤同步观察</span></div><div id="chart-legend" class="chart-legend"><span class="equity-dot">权益</span><span class="drawdown-dot">回撤</span></div></div>
                    <div class="equity-chart-wrap"><canvas id="equity-chart" aria-label="回测权益曲线"></canvas></div>
                    <div class="drawdown-chart-wrap"><canvas id="drawdown-chart" aria-label="回测回撤曲线"></canvas></div>
                  </section>

                  <section id="price-panel" class="result-panel price-panel">
                    <div class="panel-head"><div><strong>K 线与成交点</strong><span>滚轮缩放 · 拖拽平移 · 双击复位 · 悬停查看成交；收益仅在平仓点显示</span></div><div class="trade-marker-legend"><span class="long-marker">买入</span><span class="short-marker">卖出</span><span class="exit-marker">平仓收益</span></div></div>
                    <div class="price-chart-wrap"><canvas id="price-chart" aria-label="回测 K 线与买卖点" tabindex="0"></canvas><div id="price-chart-tooltip" class="price-chart-tooltip hidden" role="status" aria-live="polite"></div><div id="price-chart-range" class="price-chart-range">全部数据</div></div>
                  </section>

                  <section id="quality-panel" class="result-panel quality-panel">
                    <div class="panel-head"><div><strong>数据质量</strong><span>先判断样本是否可信，再看收益</span></div><span id="quality-grade" class="quality-grade">--</span></div>
                    <div id="quality-facts" class="quality-facts"></div>
                    <div id="quality-message" class="quality-message"></div>
                  </section>

                </div>
              </div>
              <aside id="trade-cycle-rail" class="trade-cycle-rail hidden" aria-label="交易周期">
                <section class="result-panel trades-panel">
                  <div class="panel-head"><div><strong>交易周期</strong><span id="trade-summary">--</span></div><span id="trade-count" class="count-badge">0 组</span></div>
                  <div id="trade-replay-status" class="trade-replay-status hidden">等待 K 线回放到成交时刻</div>
                  <div id="trade-table" class="trade-table"></div>
                </section>
              </aside>
            </div>
          </section>
        </div>

        <div id="symbol-picker-dialog" class="symbol-dialog-backdrop hidden" role="presentation">
          <section class="symbol-dialog" role="dialog" aria-modal="true" aria-labelledby="symbol-dialog-title">
            <header class="symbol-dialog-head">
              <div><span>MARKET UNIVERSE</span><h2 id="symbol-dialog-title">选择交易品种</h2><p>搜索并勾选一个或多个品种，点击确定后应用到本次回测。</p></div>
              <button id="close-symbol-picker" class="dialog-close" type="button" aria-label="关闭交易品种选择">×</button>
            </header>
            <div class="symbol-dialog-body">
              <label class="symbol-search-field"><span>搜索代码或名称</span><input id="symbol-search" type="search" placeholder="例如 AAPL、TSLA、MU" autocomplete="off" spellcheck="false"></label>
              <div class="symbol-dialog-toolbar"><span id="symbol-match-count">--</span><button id="clear-symbol-selection" type="button">清空待选</button></div>
              <div id="symbol-choice-list" class="symbol-choice-list" role="listbox" aria-multiselectable="true"></div>
            </div>
            <footer class="symbol-dialog-foot">
              <span id="symbol-dialog-status" role="status" aria-live="polite">尚未选择交易品种</span>
              <div><button id="cancel-symbol-picker" type="button">取消</button><button id="confirm-symbol-picker" class="primary" type="button">确定选择</button></div>
            </footer>
          </section>
        </div>

        <div id="history-dialog" class="history-dialog-backdrop hidden" role="presentation">
          <section class="history-dialog" role="dialog" aria-modal="true" aria-labelledby="history-dialog-title">
            <header class="history-dialog-head">
              <div><span>BACKTEST ARCHIVE</span><h2 id="history-dialog-title">历史回测数据</h2><p>选择一条记录后加载对应的完整回测结果。</p></div>
              <div class="history-dialog-actions"><span id="history-count">0 条</span><button id="reload-history" type="button">刷新</button><button id="close-history" class="dialog-close" type="button" aria-label="关闭历史回测数据">×</button></div>
            </header>
            <div id="history-list" class="history-list"><div class="history-empty">点击后正在读取历史回测数据…</div></div>
            <footer class="history-dialog-foot">回测数据仅用于研究；加载记录不会重新运行策略，也不会触发交易。</footer>
          </section>
        </div>

        <div id="lot-calculator-dialog" class="calculator-dialog-backdrop hidden" role="presentation">
          <section class="lot-calculator-dialog" role="dialog" aria-modal="true" aria-labelledby="lot-calculator-title">
            <header class="calculator-dialog-head">
              <div><span>POSITION CALCULATOR</span><h2 id="lot-calculator-title">仓位与止盈计算器</h2><p>按止盈点数、当前合约价格、初始手数和 Binance 杠杆估算实际收益。</p></div>
              <button id="close-lot-calculator" class="calculator-close" type="button" aria-label="关闭仓位与止盈计算器">×</button>
            </header>
            <div class="calculator-dialog-body">
              <div id="calculator-loading" class="calculator-loading">正在读取 Binance 最新合约价格…</div>
              <div id="calculator-content" class="hidden">
                <div class="calculator-context">
                  <div><span>交易品种</span><strong id="calculator-symbol">--</strong></div>
                  <div><span>最新价格</span><strong id="calculator-price">--</strong><small id="calculator-source">--</small></div>
                </div>
                <div class="calculator-adjustments">
                  <label>初始资金 (USDT)<input id="calculator-initial-capital" type="number" min="1" step="any" value="10000"></label>
                  <label>初始手数<input id="calculator-lot" type="number" min="0.000001" step="any" value="0.01"></label>
                  <label>Binance 杠杆<select id="calculator-leverage"><option value="1">1x</option><option value="2">2x</option><option value="3">3x</option><option value="5">5x</option><option value="10">10x</option><option value="20">20x</option></select></label>
                </div>
                <div id="calculator-facts" class="calculator-facts"></div>
                <div class="calculator-explanation">
                  <div class="calculator-explanation-head">
                    <span id="calculator-summary-title">止盈计算表</span>
                    <strong id="calculator-market-change" class="neutral">Binance 24h 涨跌幅 --</strong>
                  </div>
                  <div class="calculator-summary-table-wrap">
                    <table class="calculator-summary-table" aria-label="多空止盈价格与收益计算">
                      <thead><tr><th scope="col">方向</th><th scope="col">止盈价格</th><th scope="col">目标涨跌幅</th><th scope="col">预计毛利润</th><th scope="col">杠杆毛 ROE</th></tr></thead>
                      <tbody id="calculator-summary"></tbody>
                    </table>
                  </div>
                  <p id="calculator-note">--</p>
                </div>
                <div class="calculator-points">
                  <section class="calculator-box-mode" aria-labelledby="calculator-box-mode-title">
                    <div class="calculator-box-mode-head"><div><strong id="calculator-box-mode-title">箱体计算方式</strong><small>与实际马丁 TP4 回测使用同一套日线 Wilder ATR 公式</small></div><label class="calculator-box-switch"><input id="calculator-auto-box" type="checkbox"><span>ATR 自适应箱体</span></label></div>
                    <div class="calculator-box-controls">
                      <label>日线 ATR 周期<input id="calculator-atr-period" type="number" min="2" max="365" step="1" value="30"></label>
                      <label>ATR 系数<input id="calculator-atr-factor" type="number" min="0.0001" max="10" step="0.01" value="0.2"></label>
                    </div>
                    <p id="calculator-atr-status" class="calculator-atr-status">正在读取箱体参数…</p>
                  </section>
                  <div class="calculator-points-head"><div><strong>分级止盈参数</strong><small>每档同时包含起始订单数和止盈点数；TP1 固定从第 1 单开始</small></div><small>修改基础 TP 会重新计算收益，并按马丁 TP4 默认比例更新 TP2–TP4 点数</small></div>
                  <div id="calculator-tier-preview" class="calculator-tier-preview">
                    <section class="calculator-tier-card calculator-tier-card-base">
                      <div class="calculator-tier-card-head"><span>TP1</span><strong>基础止盈</strong></div>
                      <div class="calculator-tier-card-fields">
                        <label class="calculator-point-field calculator-point-field-fixed"><small>起始订单数</small><div class="calculator-point-control"><input type="number" value="1" disabled><span>单</span></div></label>
                        <label class="calculator-point-field"><small>止盈点数</small><div class="calculator-point-control"><input id="calculator-base-points" data-calculator-param-key="TP" type="number" min="0.000001" step="any"><span>点</span></div></label>
                      </div>
                    </section>
                    <div id="calculator-tier-levels" class="calculator-tier-levels"></div>
                  </div>
                  <div class="calculator-secondary-head"><strong>网格、追踪与箱体参数</strong><small>网格阈值表示已有 N 手后，从第 N+1 手开始切换；“应用分级止盈”不会改动这些参数</small></div>
                  <div id="calculator-point-preview" class="calculator-point-preview"></div>
                  <p id="calculator-grid-status" class="calculator-grid-status" role="status" aria-live="polite"></p>
                </div>
                <div class="calculator-actions">
                  <button id="apply-position-settings" type="button">应用手数与杠杆</button>
                  <button id="apply-tp-points" type="button">应用分级止盈</button>
                  <button id="apply-all-points" class="primary" type="button">一键应用全部设置</button>
                </div>
                <p id="calculator-apply-status" class="calculator-apply-status" role="status" aria-live="polite"></p>
                <p class="calculator-disclaimer">估算未包含资金费率和强平阶梯；费用结果按当前回测手续费与滑点做双边近似扣减。</p>
              </div>
              <div id="calculator-error" class="calculator-error hidden"></div>
            </div>
          </section>
        </div>
      </main>`;
  }

  q(selector) {
    return this.shadowRoot.querySelector(selector);
  }

  qa(selector) {
    return [...this.shadowRoot.querySelectorAll(selector)];
  }

  node(tag, className = "", text = "") {
    const element = document.createElement(tag);
    if (className) element.className = className;
    if (text !== "") element.textContent = String(text);
    return element;
  }

  async api(path, options = {}) {
    if (typeof window.quantdeskApi !== "function") throw new Error("认证服务尚未就绪");
    return window.quantdeskApi(`/api/v2/backtests${path}`, options);
  }

  bindEvents() {
    if (this.dataset.bound === "1") return;
    this.dataset.bound = "1";
    this.q("#open-backtest-config").addEventListener("click", () => this.openConfigDialog());
    this.q("#close-backtest-config").addEventListener("click", () => this.closeConfigDialog());
    this.q("#backtest-config-dialog").addEventListener("keydown", (event) => {
      if (event.key === "Escape" && this.q("#symbol-picker-dialog").classList.contains("hidden")) this.closeConfigDialog();
    });
    const form = this.q("#backtest-form");
    form.addEventListener("submit", (event) => this.runBacktest(event));
    const captureSymbolConfiguration = (event) => {
      if (this.applyingSymbolConfiguration || event.target?.id === "timeframe") return;
      this.captureActiveSymbolConfiguration(true);
    };
    form.addEventListener("input", captureSymbolConfiguration);
    form.addEventListener("change", captureSymbolConfiguration);
    this.q("#open-symbol-picker").addEventListener("click", () => this.openSymbolPicker());
    this.q("#close-symbol-picker").addEventListener("click", () => this.closeSymbolPicker());
    this.q("#cancel-symbol-picker").addEventListener("click", () => this.closeSymbolPicker());
    this.q("#confirm-symbol-picker").addEventListener("click", () => this.confirmSymbolSelection());
    this.q("#clear-symbol-selection").addEventListener("click", () => {
      this.draftSymbols = [];
      this.renderSymbolChoices();
    });
    this.q("#symbol-search").addEventListener("input", () => this.renderSymbolChoices());
    this.q("#symbol-picker-dialog").addEventListener("keydown", (event) => {
      if (event.key !== "Escape") return;
      event.stopPropagation();
      this.closeSymbolPicker();
    });
    this.q("#save-default-profile").addEventListener("click", () => this.saveParameterProfile("default"));
    this.q("#save-symbol-profile").addEventListener("click", () => this.saveParameterProfile("symbol"));
    this.q("#timeframe").addEventListener("change", () => {
      this.captureActiveSymbolConfiguration(false);
      this.ensureSymbolConfigurations();
      this.syncBounds();
      this.renderSymbolConfigTabs();
    });
    this.q("#market-data-source").addEventListener("change", () => this.updateMarketSourceHelp());
    this.q("#open-history").addEventListener("click", () => this.openHistory());
    this.q("#reload-history").addEventListener("click", () => this.loadHistory(true));
    this.q("#close-history").addEventListener("click", () => this.closeHistory());
    this.q("#history-dialog").addEventListener("click", (event) => {
      if (event.target === this.q("#history-dialog")) this.closeHistory();
    });
    this.shadowRoot.addEventListener("keydown", (event) => {
      if (event.key === "Escape" && !this.q("#history-dialog").classList.contains("hidden")) this.closeHistory();
    });
    this.q("#close-lot-calculator").addEventListener("click", () => this.closeLotCalculator());
    this.q("#lot-calculator-trigger").addEventListener("click", () => void this.openLotCalculator());
    this.q("#calculator-initial-capital").addEventListener("input", () => this.renderLotCalculator());
    this.q("#calculator-lot").addEventListener("input", () => this.renderLotCalculator());
    this.q("#calculator-leverage").addEventListener("change", () => this.renderLotCalculator());
    this.q("#calculator-auto-box").addEventListener("change", () => {
      this.syncCalculatorBoxMode();
      if (this.q("#calculator-auto-box").checked) void this.refreshCalculatorAtr();
    });
    this.q("#calculator-atr-period").addEventListener("change", () => void this.refreshCalculatorAtr());
    this.q("#calculator-atr-factor").addEventListener("input", () => this.renderLotCalculator());
    this.q("#calculator-base-points").addEventListener("input", () => {
      this.updateCalculatorPointRatios();
      this.renderLotCalculator();
    });
    this.q("#apply-position-settings").addEventListener("click", () => this.applyCalculatedPoints("position"));
    this.q("#apply-tp-points").addEventListener("click", () => this.applyCalculatedPoints("take-profit"));
    this.q("#apply-all-points").addEventListener("click", () => this.applyCalculatedPoints("all"));
    this.q("#calculator-point-preview").addEventListener("input", (event) => {
      const input = event.target.closest?.('[data-calculator-param-key="BoxRange"]');
      if (input && !input.disabled) this.lotCalculatorManualBoxRange = input.value;
      if (event.target.closest?.('[data-calculator-param-key="GridDrift"]')) this.syncCalculatorGridMode();
    });
    this.q("#reset-params").addEventListener("click", () => {
      this.renderParameters(true);
      this.captureActiveSymbolConfiguration(true);
    });
    ["input", "change"].forEach((eventName) => {
      this.q("#strategy-params").addEventListener(eventName, (event) => {
        const input = event.target.closest?.("[data-param-key]");
        if (input) this.syncParameterDependencies();
      });
    });
    this.qa("[data-months]").forEach((button) => button.addEventListener("click", () => this.applyRange(button.dataset.months)));
    this.bindPriceChartEvents();
  }

  start() {
    if (this.started) return;
    this.started = true;
    const generation = this.sessionGeneration;
    void this.bootstrap(generation);
  }

  pause() {
    // 回测任务在服务端完成；切换页面不取消已提交的计算。
    this.stopComputationProgress();
    this.stopResultReplay();
  }

  resetSession() {
    this.sessionGeneration += 1;
    this.started = false;
    this.loading = false;
    this.runningBacktest = false;
    this.catalog = { strategies: [], symbols: [], timeframes: [], bounds: {} };
    this.symbolOptions = [];
    this.selectedSymbols = [];
    this.draftSymbols = [];
    this.activeConfigSymbol = "";
    this.symbolConfigurations = {};
    this.symbolConfigurationDirty.clear();
    this.symbolProfileLoaded.clear();
    this.symbolProfileRequests.clear();
    this.applyingSymbolConfiguration = false;
    this.profileRequest += 1;
    this.profileSaveInFlight = false;
    this.history = [];
    this.historyLoaded = false;
    this.activeDetail = null;
    this.batchResults = [];
    this.batchFailures = [];
    this.expandedBatchIndex = -1;
    this.batchExpandToken += 1;
    this.lotCalculatorQuote = null;
    this.lotCalculatorRequest += 1;
    this.lotCalculatorManualBoxRange = null;
    this.stopComputationProgress();
    this.stopResultReplay();
    this.resetPriceChartState();
    this.strategyId = "";
    this.category = "全部";
    this.q("#backtest-form").reset();
    this.q("#category-filter").replaceChildren();
    this.q("#strategy-count").textContent = "--";
    this.q("#strategy-list").replaceChildren(this.node("div", "rail-loading", "登录后加载我的策略…"));
    this.q("#strategy-description").textContent = "策略参数将随策略中心配置动态加载。";
    this.q("#strategy-params").replaceChildren();
    this.q("#parameter-section").classList.add("hidden");
    this.q("#data-bound").textContent = "等待行情目录";
    this.q("#available-range").textContent = "正在读取…";
    this.q("#available-bars").textContent = "按当前品种与周期统计";
    this.q("#data-availability").classList.remove("empty");
    const symbolInput = this.q("#symbol");
    symbolInput.value = "";
    this.q("#open-symbol-picker").disabled = true;
    this.renderSelectedSymbols();
    this.renderSymbolConfigTabs();
    this.q("#profile-status").textContent = "选择策略后可加载和保存交易参数。";
    const timeframeOption = this.node("option", "", "登录后加载…");
    timeframeOption.value = "";
    this.q("#timeframe").replaceChildren(timeframeOption);
    for (const id of ["#start-date", "#end-date"]) {
      const input = this.q(id);
      input.value = "";
      input.removeAttribute("min");
      input.removeAttribute("max");
    }
    this.q("#range-feedback").textContent = "点击快捷区间可调整回测日期";
    this.q("#range-feedback").classList.remove("limited");
    this.qa("[data-months]").forEach((button) => {
      button.classList.remove("active");
      button.setAttribute("aria-pressed", "false");
    });
    this.renderHistory();
    this.closeSymbolPicker(false);
    this.closeConfigDialog(false);
    this.closeHistory();
    this.closeLotCalculator();
    this.q("#empty-result").classList.remove("hidden");
    this.q("#running-result").classList.add("hidden");
    this.q("#result-content").classList.add("hidden");
    this.q("#trade-cycle-rail").classList.add("hidden");
    this.q(".stage-layout").classList.remove("has-result");
    this.hideBatchResults(true);
    this.q("#open-history").disabled = false;
    this.q("#reload-history").disabled = false;
    this.q("#active-run-meta").textContent = "选择策略并配置参数";
    this.showBanner("");
    this.setStageStatus("等待运行", "idle");
    this.setRunButton(false);
  }

  async bootstrap(generation = this.sessionGeneration) {
    this.setStageStatus("正在读取", "loading");
    const catalogResult = await Promise.resolve(this.api("/catalog")).then(
      (value) => ({ status: "fulfilled", value }),
      (reason) => ({ status: "rejected", reason }),
    );
    if (generation !== this.sessionGeneration) return;
    if (catalogResult.status === "fulfilled") {
      this.catalog = this.normalizeCatalog(catalogResult.value);
      this.renderCatalog();
      this.showBanner("");
    } else {
      this.showBanner(`回测目录加载失败：${catalogResult.reason.message}`, "error");
      this.q("#strategy-list").replaceChildren(this.node("div", "rail-loading error-text", "我的策略暂不可用"));
      this.started = false;
    }
    this.setStageStatus(catalogResult.status === "fulfilled" ? "等待运行" : "目录不可用", catalogResult.status === "fulfilled" ? "idle" : "error");
  }

  normalizeCatalog(payload = {}) {
    const normalizeOption = (item, fallbackKey) => {
      if (typeof item === "string") return { value: item, label: item };
      const value = item?.value ?? item?.id ?? item?.symbol ?? item?.timeframe ?? item?.[fallbackKey] ?? "";
      return { ...item, value: String(value), label: String(item?.label ?? item?.name ?? value) };
    };
    return {
      strategies: Array.isArray(payload.strategies) ? payload.strategies : [],
      symbols: (Array.isArray(payload.symbols) ? payload.symbols : []).map((item) => normalizeOption(item, "symbol")),
      timeframes: (Array.isArray(payload.timeframes) ? payload.timeframes : []).map((item) => normalizeOption(item, "timeframe")),
      bounds: payload.bounds || {},
      limits: payload.limits || {},
    };
  }

  renderCatalog() {
    const categories = ["全部", ...new Set(this.catalog.strategies.map((item) => item.category).filter(Boolean))];
    const categoryBox = this.q("#category-filter");
    categoryBox.replaceChildren(...categories.map((category) => {
      const button = this.node("button", category === this.category ? "active" : "", category);
      button.type = "button";
      button.setAttribute("aria-pressed", String(category === this.category));
      button.addEventListener("click", () => {
        this.category = category;
        const selectedVisible = this.catalog.strategies.some((item) => String(item.id ?? "") === this.strategyId && (category === "全部" || item.category === category));
        if (!selectedVisible) {
          this.strategyId = "";
          this.q("#strategy-params").replaceChildren();
        }
        this.renderCatalog();
      });
      return button;
    }));
    const strategyList = this.catalog.strategies.filter((item) => this.category === "全部" || item.category === this.category);
    this.q("#strategy-count").textContent = `${this.catalog.strategies.length} 个策略`;
    const list = this.q("#strategy-list");
    list.replaceChildren(...strategyList.map((strategy) => {
      const id = String(strategy.id ?? "");
      const button = this.node("button", `strategy-card${id === this.strategyId ? " active" : ""}`);
      button.type = "button";
      button.dataset.strategyId = id;
      button.setAttribute("aria-pressed", String(id === this.strategyId));
      const title = this.node("strong", "", strategy.name || id || "未命名策略");
      const category = this.node("span", "strategy-category", strategy.category || "自定义");
      const summary = this.node("small", "", strategy.description || "使用当前策略参数开始验证");
      button.append(title, category, summary);
      button.addEventListener("click", () => this.selectStrategy(id));
      return button;
    }));
    if (!strategyList.length) {
      const empty = this.node("div", "rail-loading", this.catalog.strategies.length ? "该分类暂无策略" : "策略中心还没有可回测策略");
      if (!this.catalog.strategies.length) {
        const manage = this.node("button", "text-button manage-strategies", "去策略中心新增");
        manage.type = "button";
        manage.addEventListener("click", () => document.dispatchEvent(new CustomEvent("quantdesk:navigate", { detail: "strategies" })));
        empty.append(manage);
      }
      list.replaceChildren(empty);
    }

    if (!this.strategyId && strategyList.length) this.selectStrategy(String(strategyList[0].id ?? ""));
    else this.syncStrategyProfile(false);
  }

  populateSelect(select, options, placeholder) {
    const previous = select.value;
    const placeholderOption = this.node("option", "", placeholder);
    placeholderOption.value = "";
    const nodes = options.map((option) => {
      const node = this.node("option", "", option.label);
      node.value = option.value;
      return node;
    });
    select.replaceChildren(placeholderOption, ...nodes);
    if (options.some((item) => item.value === previous)) select.value = previous;
    else if (options.length) select.value = options[0].value;
  }

  populateSymbolSearch(options, placeholder) {
    const input = this.q("#symbol");
    const previous = input.value.trim().toUpperCase();
    this.symbolOptions = options
      .map((option) => ({ ...option, value: String(option.value || "").trim().toUpperCase() }))
      .filter((option) => option.value);
    const allowed = new Set(this.symbolOptions.map((item) => item.value));
    this.selectedSymbols = this.selectedSymbols.filter((symbol) => allowed.has(symbol));
    const initial = allowed.has(previous) ? previous : this.symbolOptions[0]?.value || "";
    if (!this.selectedSymbols.length && initial) this.selectedSymbols = [initial];
    if (!this.selectedSymbols.includes(this.activeConfigSymbol)) this.activeConfigSymbol = this.selectedSymbols[0] || initial;
    input.value = this.primarySymbol() || initial;
    this.q("#open-symbol-picker").disabled = !this.symbolOptions.length;
    this.q("#symbol-selection-summary").textContent = this.symbolOptions.length ? placeholder : "当前策略没有可用品种";
    this.renderSelectedSymbols();
    this.renderSymbolConfigTabs();
    if (!this.q("#symbol-picker-dialog").classList.contains("hidden")) this.renderSymbolChoices();
  }

  openSymbolPicker() {
    if (!this.symbolOptions.length) return;
    this.draftSymbols = [...this.selectedSymbols];
    this.q("#symbol-search").value = "";
    this.q("#symbol-picker-dialog").classList.remove("hidden");
    this.q("#open-symbol-picker").setAttribute("aria-expanded", "true");
    this.renderSymbolChoices();
    this.q("#symbol-search").focus();
  }

  closeSymbolPicker(restoreFocus = true) {
    this.q("#symbol-picker-dialog")?.classList.add("hidden");
    const opener = this.q("#open-symbol-picker");
    opener?.setAttribute("aria-expanded", "false");
    if (restoreFocus) opener?.focus();
  }

  renderSymbolChoices() {
    const query = this.q("#symbol-search").value.trim().toUpperCase();
    const matches = this.symbolOptions.filter((option) => {
      const label = String(option.label || "").toUpperCase();
      return !query || option.value.includes(query) || label.includes(query);
    });
    this.q("#symbol-match-count").textContent = query
      ? `找到 ${matches.length} 个品种`
      : `全部 ${matches.length} 个品种`;
    this.q("#symbol-dialog-status").textContent = this.draftSymbols.length
      ? `待确认：已选择 ${this.draftSymbols.length} 个品种`
      : "请至少选择 1 个交易品种";
    this.q("#symbol-dialog-status").classList.toggle("error", !this.draftSymbols.length);
    const list = this.q("#symbol-choice-list");
    if (!matches.length) {
      list.replaceChildren(this.node("div", "symbol-choice-empty", "没有匹配的交易品种，请更换关键词。"));
      return;
    }
    list.replaceChildren(...matches.map((option) => {
      const selected = this.draftSymbols.includes(option.value);
      const button = this.node("button", `symbol-choice${selected ? " selected" : ""}`);
      button.type = "button";
      button.setAttribute("role", "option");
      button.setAttribute("aria-selected", String(selected));
      button.dataset.symbol = option.value;
      const mark = this.node("span", "symbol-choice-mark", selected ? "✓" : "");
      const copy = this.node("span", "symbol-choice-copy");
      copy.append(
        this.node("strong", "", option.value),
        this.node("small", "", option.label && option.label !== option.value ? option.label : "Binance 合约"),
      );
      button.append(mark, copy);
      button.addEventListener("click", () => {
        if (selected) this.draftSymbols = this.draftSymbols.filter((item) => item !== option.value);
        else this.draftSymbols = [...this.draftSymbols, option.value];
        this.renderSymbolChoices();
      });
      return button;
    }));
  }

  confirmSymbolSelection() {
    if (!this.draftSymbols.length) {
      this.q("#symbol-dialog-status").textContent = "请至少选择 1 个交易品种后再确定";
      this.q("#symbol-dialog-status").classList.add("error");
      return;
    }
    const baseConfiguration = this.captureActiveSymbolConfiguration(false) || this.readCurrentSymbolConfiguration();
    const allowed = new Set(this.symbolOptions.map((item) => item.value));
    this.selectedSymbols = this.draftSymbols.filter((symbol) => allowed.has(symbol));
    if (!this.selectedSymbols.includes(this.activeConfigSymbol)) this.activeConfigSymbol = this.selectedSymbols[0] || "";
    this.ensureSymbolConfigurations(baseConfiguration);
    this.q("#symbol").value = this.primarySymbol();
    this.closeSymbolPicker();
    this.switchActiveSymbol(this.primarySymbol(), { loadProfile: false, captureCurrent: false });
    void this.loadSelectedParameterProfiles();
  }

  renderSelectedSymbols() {
    const container = this.q("#selected-symbols");
    if (!container) return;
    const visible = this.selectedSymbols.slice(0, 4);
    container.replaceChildren(...visible.map((symbol) => {
      const chip = this.node("span", `symbol-chip${symbol === this.primarySymbol() ? " active" : ""}`);
      chip.append(this.node("b", "", symbol));
      return chip;
    }));
    if (this.selectedSymbols.length > visible.length) {
      container.append(this.node("span", "symbol-chip symbol-chip-more", `另 ${this.selectedSymbols.length - visible.length} 个`));
    }
    const count = this.selectedSymbols.length;
    this.q("#symbol-selection-title").textContent = count ? `已选择 ${count} 个交易品种` : "选择交易品种";
    this.q("#symbol-selection-summary").textContent = count
      ? this.selectedSymbols.slice(0, 3).join("、") + (count > 3 ? "…" : "")
      : this.symbolOptions.length ? "点击打开品种列表" : "当前策略没有可用品种";
  }

  symbolsForRun() {
    return [...this.selectedSymbols];
  }

  primarySymbol() {
    return this.selectedSymbols.includes(this.activeConfigSymbol)
      ? this.activeConfigSymbol
      : this.selectedSymbols[0] || "";
  }

  cloneSymbolConfiguration(configuration = {}) {
    return {
      market_data_source: String(configuration.market_data_source || "binance"),
      start_date: String(configuration.start_date || ""),
      end_date: String(configuration.end_date || ""),
      initial_capital: Number(configuration.initial_capital ?? 10000),
      position_size_pct: Number(configuration.position_size_pct ?? 10),
      leverage: Number(configuration.leverage ?? 1),
      fee_bps: Number(configuration.fee_bps ?? 4),
      slippage_bps: Number(configuration.slippage_bps ?? 2),
      stop_loss_pct: Number(configuration.stop_loss_pct ?? 5),
      take_profit_pct: Number(configuration.take_profit_pct ?? 10),
      max_holding_bars: Number(configuration.max_holding_bars ?? 120),
      params: { ...(configuration.params || {}) },
      profile_scope: configuration.profile_scope || null,
    };
  }

  readCurrentSymbolConfiguration() {
    return this.cloneSymbolConfiguration({
      market_data_source: this.q("#market-data-source").value,
      start_date: this.q("#start-date").value,
      end_date: this.q("#end-date").value,
      initial_capital: this.q("#initial-capital").value,
      ...this.executionProfile(),
      params: this.collectParams(),
    });
  }

  captureActiveSymbolConfiguration(markDirty = false) {
    const symbol = this.primarySymbol();
    if (!symbol || this.applyingSymbolConfiguration) return null;
    const configuration = this.readCurrentSymbolConfiguration();
    configuration.profile_scope = this.symbolConfigurations[symbol]?.profile_scope || null;
    this.symbolConfigurations[symbol] = configuration;
    if (markDirty) this.symbolConfigurationDirty.add(symbol);
    this.renderSymbolConfigTabs();
    return configuration;
  }

  ensureSymbolConfigurations(baseConfiguration = null, replace = false) {
    const base = this.cloneSymbolConfiguration(baseConfiguration || this.readCurrentSymbolConfiguration());
    this.selectedSymbols.forEach((symbol) => {
      if (replace || !this.symbolConfigurations[symbol]) {
        this.symbolConfigurations[symbol] = this.normalizeSymbolConfigurationRange(symbol, base);
      } else {
        this.symbolConfigurations[symbol] = this.normalizeSymbolConfigurationRange(symbol, this.symbolConfigurations[symbol]);
      }
    });
    if (!this.selectedSymbols.includes(this.activeConfigSymbol)) {
      this.activeConfigSymbol = this.selectedSymbols[0] || "";
    }
  }

  normalizeSymbolConfigurationRange(symbol, configuration = {}) {
    const normalized = this.cloneSymbolConfiguration(configuration);
    const { min, max } = this.resolveBounds(symbol);
    const fallbackEnd = max || this.dateOnly(new Date());
    if (!normalized.end_date || normalized.end_date > fallbackEnd || (min && normalized.end_date < min)) {
      normalized.end_date = fallbackEnd;
    }
    if (!normalized.start_date || normalized.start_date > normalized.end_date || (min && normalized.start_date < min)) {
      normalized.start_date = this.maxDate(min, this.shiftMonths(normalized.end_date, this.isBasketStrategy() ? -1 : -3));
    }
    return normalized;
  }

  applySymbolConfiguration(configuration = {}) {
    const value = this.cloneSymbolConfiguration(configuration);
    const mapping = {
      initial_capital: "#initial-capital",
      position_size_pct: "#position-size",
      leverage: "#leverage",
      fee_bps: "#fee",
      slippage_bps: "#slippage",
      stop_loss_pct: "#stop-loss",
      take_profit_pct: "#take-profit",
      max_holding_bars: "#max-holding",
      start_date: "#start-date",
      end_date: "#end-date",
    };
    this.applyingSymbolConfiguration = true;
    try {
      Object.entries(mapping).forEach(([key, selector]) => {
        const input = this.q(selector);
        if (input && value[key] != null) input.value = String(value[key]);
      });
      const source = this.q("#market-data-source");
      source.value = source.disabled ? "binance" : value.market_data_source;
      this.applyStrategyParameters(value.params);
      this.updateMarketSourceHelp();
    } finally {
      this.applyingSymbolConfiguration = false;
    }
  }

  switchActiveSymbol(symbol, { loadProfile = true, captureCurrent = true } = {}) {
    if (!this.selectedSymbols.includes(symbol)) return;
    const previous = this.primarySymbol();
    const previousConfiguration = captureCurrent ? this.captureActiveSymbolConfiguration(false) : null;
    if (!this.symbolConfigurations[symbol]) {
      this.symbolConfigurations[symbol] = this.cloneSymbolConfiguration(previousConfiguration || this.readCurrentSymbolConfiguration());
    }
    this.activeConfigSymbol = symbol;
    this.q("#symbol").value = symbol;
    this.applySymbolConfiguration(this.symbolConfigurations[symbol]);
    this.syncBounds();
    this.renderSelectedSymbols();
    this.renderSymbolConfigTabs();
    this.renderProfileStatus(symbol);
    if (loadProfile && symbol !== previous && !this.symbolProfileLoaded.has(symbol)) {
      void this.loadParameterProfile(symbol);
    }
  }

  renderSymbolConfigTabs() {
    const container = this.q("#symbol-config-tabs");
    if (!container) return;
    if (!this.selectedSymbols.length) {
      container.replaceChildren(this.node("span", "symbol-config-empty", "请选择至少一个交易品种"));
      return;
    }
    container.replaceChildren(...this.selectedSymbols.map((symbol) => {
      const active = symbol === this.primarySymbol();
      const bounds = this.resolveBounds(symbol);
      const button = this.node("button", `symbol-config-tab${active ? " active" : ""}`);
      button.type = "button";
      button.setAttribute("role", "tab");
      button.setAttribute("aria-selected", String(active));
      const count = bounds.bars == null ? "待校验" : `${new Intl.NumberFormat("zh-CN").format(bounds.bars)} 根`;
      button.append(this.node("strong", "", symbol), this.node("small", "", count));
      if (this.symbolConfigurationDirty.has(symbol)) button.append(this.node("i", "", "已调整"));
      button.addEventListener("click", () => this.switchActiveSymbol(symbol));
      return button;
    }));
  }

  renderProfileStatus(symbol = this.primarySymbol(), scope = null) {
    const status = this.q("#profile-status");
    if (!status || !symbol) return;
    const effectiveScope = scope || this.symbolConfigurations[symbol]?.profile_scope;
    status.textContent = effectiveScope === "symbol"
      ? `当前编辑：${symbol} 专有配置（优先于默认配置）`
      : effectiveScope === "default"
        ? `当前编辑：${symbol}，已加载策略默认交易配置`
        : `当前编辑：${symbol}，参数与其他币种相互独立`;
  }

  selectStrategy(id) {
    const changed = this.strategyId !== id;
    if (changed) {
      this.symbolConfigurations = {};
      this.symbolConfigurationDirty.clear();
      this.symbolProfileLoaded.clear();
      this.symbolProfileRequests.clear();
      this.activeConfigSymbol = "";
    }
    this.strategyId = id;
    this.qa(".strategy-card").forEach((card) => {
      const active = card.dataset.strategyId === id;
      card.classList.toggle("active", active);
      card.setAttribute("aria-pressed", String(active));
    });
    const strategy = this.selectedStrategy();
    this.q("#strategy-description").textContent = strategy?.description || "策略参数将随策略中心配置动态加载。";
    this.syncStrategyProfile(changed);
    this.renderParameters(changed);
    this.applyExecutionProfile({
      position_size_pct: 10,
      leverage: 1,
      fee_bps: 4,
      slippage_bps: 2,
      stop_loss_pct: 5,
      take_profit_pct: 10,
      max_holding_bars: 120,
      ...(strategy?.risk_defaults || {}),
    });
    this.ensureSymbolConfigurations(this.readCurrentSymbolConfiguration(), changed);
    this.applySymbolConfiguration(this.symbolConfigurations[this.primarySymbol()] || this.readCurrentSymbolConfiguration());
    this.syncBounds(changed && this.isBasketStrategy());
    this.renderSelectedSymbols();
    this.renderSymbolConfigTabs();
    void this.loadSelectedParameterProfiles();
  }

  selectedStrategy() {
    return this.catalog.strategies.find((item) => String(item.id ?? "") === this.strategyId) || null;
  }

  isBasketStrategy() {
    const strategy = this.selectedStrategy();
    return strategy?.backtest_profile === "martingale_tp4" || (strategy?.strategy_kind === "basket_strategy" && strategy?.engine_key === "martingale_tp4");
  }

  syncStrategyProfile(changed = false) {
    const strategy = this.selectedStrategy();
    const basket = this.isBasketStrategy();
    const normalizeOption = (item, fallbackKey) => {
      if (typeof item === "string") return { value: item, label: item };
      const value = item?.value ?? item?.symbol ?? item?.timeframe ?? item?.[fallbackKey] ?? "";
      return { ...item, value: String(value), label: String(item?.label ?? item?.name ?? value) };
    };
    const symbols = basket
      ? (Array.isArray(strategy?.supported_symbols) ? strategy.supported_symbols : []).map((item) => normalizeOption(item, "symbol"))
      : this.catalog.symbols;
    const timeframes = basket
      ? (Array.isArray(strategy?.supported_timeframes) ? strategy.supported_timeframes : ["1m", "5m", "15m", "30m", "1h"]).map((item) => normalizeOption(item, "timeframe"))
      : this.catalog.timeframes;
    this.populateSymbolSearch(symbols, basket ? "搜索 Tiger/Binance 映射" : "输入名称或代码搜索");
    this.populateSelect(this.q("#timeframe"), timeframes, "选择周期");
    if (basket && changed && timeframes.some((item) => item.value === "15m")) this.q("#timeframe").value = "15m";
    const sourceSelect = this.q("#market-data-source");
    const sourceHelp = this.q("#market-source-help");
    sourceSelect.disabled = !basket;
    if (basket) {
      if (changed || !["auto", "tiger", "binance"].includes(sourceSelect.value)) sourceSelect.value = "binance";
      this.updateMarketSourceHelp();
    } else {
      sourceSelect.value = "binance";
      sourceHelp.textContent = "当前策略固定使用 Binance 合约历史 K 线。";
    }

    ["#position-field", "#holding-field", "#stop-field", "#take-profit-field"].forEach((selector) => {
      this.q(selector).classList.toggle("hidden", basket);
    });
    this.q("#basket-profile-note").classList.toggle("hidden", !basket);
    this.q("#standard-execution-note").classList.toggle("hidden", basket);
    this.q("#capital-section .section-head small").textContent = basket ? "篮子仓位由策略参数控制 · Binance 逐仓" : "Binance 逐仓 · 单标的单仓";
    this.q("#cost-section .section-head small").textContent = basket ? "成本可调 · 退出由策略控制" : "结果均为净值";
    this.q("#run-backtest").disabled = !strategy || !symbols.length || !timeframes.length;
    this.setProfileButtons(false);
    this.syncBounds(basket && changed);
  }

  updateMarketSourceHelp() {
    const sourceSelect = this.q("#market-data-source");
    const sourceHelp = this.q("#market-source-help");
    if (sourceSelect.disabled) {
      sourceHelp.textContent = "当前策略固定使用 Binance 合约历史 K 线。";
    } else if (sourceSelect.value === "binance") {
      sourceHelp.textContent = "默认使用 Binance 合约历史 K 线；回测会自动向前补取指标预热数据。";
    } else if (sourceSelect.value === "tiger") {
      sourceHelp.textContent = "优先使用已入库或官方 Open API 回补的 Tiger 历史 K 线；不足时使用已验证的 Binance 映射合约。";
    } else {
      sourceHelp.textContent = "自动模式优先读取 Tiger；历史 K 线缺失、接口不可用或未配置时直接回退 Binance。";
    }
  }

  renderParameters(reset = false) {
    const params = (Array.isArray(this.selectedStrategy()?.params) ? this.selectedStrategy().params : [])
      .filter((param) => !this.isBasketStrategy() || param?.key !== "BoxTimeFrameMinutes");
    const section = this.q("#parameter-section");
    section.classList.toggle("hidden", !params.length);
    this.q("#lot-calculator-trigger").classList.toggle("hidden", !this.isBasketStrategy() || !params.some((param) => param?.key === "Lot"));
    const container = this.q("#strategy-params");
    const existing = reset ? {} : Object.fromEntries(this.qa("[data-param-key]").map((input) => [input.dataset.paramKey, input.type === "checkbox" ? input.checked : input.value]));
    const fields = params.map((param) => {
      const label = this.node("label");
      const fieldName = this.node("span", "field-label", param.label || param.key);
      label.append(fieldName);
      const type = String(param.type || "number").toLowerCase();
      const switchInteger = param.control === "switch" && type === "integer";
      let input;
      if (Array.isArray(param.options)) {
        input = this.node("select");
        input.append(...param.options.map((option) => {
          const value = typeof option === "object" ? option.value : option;
          const optionNode = this.node("option", "", typeof option === "object" ? option.label ?? value : value);
          optionNode.value = String(value);
          return optionNode;
        }));
      } else if (switchInteger || type === "boolean" || type === "bool") {
        input = this.node("input");
        input.type = "checkbox";
        input.setAttribute("role", "switch");
        label.classList.add("parameter-switch-field");
      } else {
        input = this.node("input");
        input.type = type === "integer" || type === "float" || type === "number" ? "number" : "text";
        if (param.min != null) input.min = String(param.min);
        if (param.max != null) input.max = String(param.max);
        // A number input defaults to step=1.  Many imported strategy schemas
        // only declare min/max, and their decimals are still valid strategy
        // values (for example Lot=0.01 or ATR factor=0.2).  Without an
        // explicit fallback the browser rejects those defaults before the
        // request reaches the backtest engine.
        input.step = param.step != null ? String(param.step) : "any";
      }
      input.dataset.paramKey = String(param.key || "");
      input.dataset.paramType = type;
      const value = Object.prototype.hasOwnProperty.call(existing, param.key) ? existing[param.key] : param.default;
      if (input.type === "checkbox") input.checked = value === true || Number(value) === 1;
      else if (value != null) input.value = String(value);
      if (input.type === "checkbox") {
        const control = this.node("span", "parameter-switch-control");
        const visual = this.node("span", "parameter-switch-visual");
        visual.append(this.node("i", "parameter-switch-knob"));
        control.append(input, visual, this.node("strong", "parameter-switch-state"));
        label.append(control);
      } else label.append(input);
      if (param.help) label.append(this.node("small", "field-help", param.help));
      label.append(this.node("small", "parameter-dependency-help hidden"));
      return label;
    });
    const grouped = new Map();
    params.forEach((param, index) => {
      const metadata = this.parameterGroupMetadata(param.group);
      if (!grouped.has(metadata.name)) grouped.set(metadata.name, { ...metadata, fields: [] });
      grouped.get(metadata.name).fields.push(fields[index]);
    });
    container.replaceChildren(...Array.from(grouped.values(), (group, index) => {
      const block = this.node("section", "strategy-parameter-group");
      block.dataset.parameterGroup = group.name;
      const header = this.node("header", "strategy-parameter-group-head");
      const title = this.node("div");
      title.append(
        this.node("span", "strategy-parameter-group-index", String(index + 1).padStart(2, "0")),
        this.node("strong", "", group.name),
      );
      header.append(title, this.node("small", "", group.description));
      const grid = group.name === "分级止盈"
        ? this.renderTakeProfitTiers(group.fields)
        : this.node("div", "field-grid two strategy-parameter-grid");
      if (group.name !== "分级止盈") grid.append(...group.fields);
      block.append(header, grid, this.node("p", "parameter-group-message hidden"));
      return block;
    }));
    this.syncParameterDependencies();
  }

  renderTakeProfitTiers(fields) {
    const byKey = new Map(fields.map((field) => {
      const key = field.querySelector("[data-param-key]")?.dataset.paramKey;
      return [key, field];
    }));
    const tiers = [
      { code: "TP1", title: "基础止盈", startKey: null, pointKey: "TP", fixedStart: "1" },
      { code: "TP2", title: "第二档止盈", startKey: "Kol_Ord_for_TP2", pointKey: "TP2" },
      { code: "TP3", title: "第三档止盈", startKey: "Kol_Ord_for_TP3", pointKey: "TP3" },
      { code: "TP4", title: "第四档止盈", startKey: "Kol_Ord_for_TP4", pointKey: "TP4" },
    ];
    const grid = this.node("div", "take-profit-tier-grid strategy-parameter-grid");
    tiers.forEach((tier) => {
      const pointField = byKey.get(tier.pointKey);
      if (!pointField) return;
      const card = this.node("article", "take-profit-tier");
      card.dataset.takeProfitTier = tier.code;
      const header = this.node("header", "take-profit-tier-head");
      header.append(
        this.node("span", "take-profit-tier-code", tier.code),
        this.node("strong", "", tier.title),
      );
      const body = this.node("div", "take-profit-tier-fields");
      if (tier.startKey) {
        const startField = byKey.get(tier.startKey);
        if (startField) {
          const label = startField.querySelector(".field-label");
          if (label) label.textContent = "起始订单数";
          body.append(startField);
        }
      } else {
        const fixed = this.node("div", "take-profit-static-field");
        fixed.append(
          this.node("span", "field-label", "起始订单数"),
          this.node("strong", "", tier.fixedStart),
          this.node("small", "", "固定"),
        );
        body.append(fixed);
      }
      const pointLabel = pointField.querySelector(".field-label");
      if (pointLabel) pointLabel.textContent = "止盈点数";
      body.append(pointField);
      card.append(header, body);
      grid.append(card);
    });
    return grid;
  }

  parameterGroupMetadata(rawName) {
    const groups = {
      "运行模式": ["交易模式与周期", "选择运行方式，并控制是否允许建立新的交易周期"],
      "仓位递增": ["仓位与网格加仓", "设置首单手数、递增规则、订单上限和网格加仓距离"],
      "执行约束": ["成交与点差约束", "限制允许成交的市场点差，避免异常报价触发交易"],
      "分级止盈": ["分级止盈", "根据当前篮子订单数量，自动切换对应的止盈点数"],
      "止损与追踪": ["金额止损与追踪止盈", "控制篮子金额止损，以及盈利后的动态回撤保护"],
      "首尾覆盖": ["篮子首尾覆盖", "订单较多时，用首尾订单组合盈利覆盖中间持仓"],
      "运行时段": ["自动交易时段", "限制自动模式建立新交易周期的小时范围"],
      "突破箱体": ["突破箱体与 ATR", "设置箱体识别、ATR 自适应范围和突破确认缓冲"],
      "MQ4 兼容": ["兼容与显示设置", "仅用于兼容原 MQ4 策略参数，通常无需修改"],
    };
    const fallback = String(rawName || "其他策略参数").trim() || "其他策略参数";
    const [name, description] = groups[fallback] || [fallback, "当前策略运行所需的参数配置"];
    return { name, description };
  }

  parameterSwitchEnabled(key) {
    const input = this.strategyParamInput(key);
    if (!input) return false;
    return input.type === "checkbox" ? input.checked : Number(input.value) !== 0;
  }

  setParameterDisabled(key, disabled, message = "") {
    const input = this.strategyParamInput(key);
    if (!input) return;
    input.disabled = Boolean(disabled);
    input.setAttribute("aria-disabled", String(Boolean(disabled)));
    const field = input.closest("label");
    field?.classList.toggle("parameter-field-disabled", Boolean(disabled));
    const help = field?.querySelector(".parameter-dependency-help");
    if (help) {
      help.textContent = disabled ? message : "";
      help.classList.toggle("hidden", !disabled || !message);
    }
  }

  setParameterGroupMessage(groupName, message = "", tone = "info") {
    const group = this.q(`[data-parameter-group="${groupName}"]`);
    const target = group?.querySelector(".parameter-group-message");
    if (!target) return;
    target.textContent = message;
    target.className = `parameter-group-message ${message ? tone : "hidden"}`;
  }

  syncParameterDependencies() {
    this.qa(".parameter-switch-field input[type=checkbox]").forEach((input) => {
      const field = input.closest("label");
      const state = field?.querySelector(".parameter-switch-state");
      if (state) state.textContent = input.checked ? "已开启" : "已关闭";
      input.setAttribute("aria-checked", String(input.checked));
      field?.classList.toggle("enabled", input.checked);
    });

    const autoBox = this.parameterSwitchEnabled("AutoBoxRange");
    this.setParameterDisabled("BoxRange", autoBox, "ATR 自适应箱体已开启，固定箱体范围不参与计算。");
    this.setParameterDisabled("AutoBoxRangeDailyATRperiod", !autoBox, "开启 ATR 自适应箱体后才可配置。");
    this.setParameterDisabled("AutoBoxRangeDailyATRfactor", !autoBox, "开启 ATR 自适应箱体后才可配置。");
    this.setParameterGroupMessage(
      "突破箱体与 ATR",
      autoBox
        ? "当前使用：日线 ATR × ATR 系数计算箱体范围；固定箱体范围已停用。"
        : "当前使用：固定箱体范围；ATR 周期和系数已停用。",
    );

    const configuredMode = Number(this.strategyParamInput("ChooseTrading")?.value ?? 0);
    const gridDrift = Math.max(1, Math.round(Number(this.strategyParamInput("GridDrift")?.value || 1)));
    const maxOrders = Math.max(1, Math.round(Number(this.strategyParamInput("MaxOrders")?.value || 1)));
    this.setParameterDisabled(
      "GridDrift",
      configuredMode === 2,
      "当前已选择网格模式，将从首单开始按网格逻辑运行。",
    );
    const gridMessage = configuredMode === 2
      ? "当前使用：从首单开始直接运行网格模式，切换阈值不参与判断。"
      : gridDrift < maxOrders
        ? `当前使用：已有 ${gridDrift} 手后，从第 ${gridDrift + 1} 手开始切换为网格模式。`
        : `当前不会切换网格：阈值 ${gridDrift} 手不小于最大订单数 ${maxOrders} 手。`;
    this.setParameterGroupMessage(
      "仓位与网格加仓",
      gridMessage,
      configuredMode !== 2 && gridDrift >= maxOrders ? "warning" : "info",
    );

    const overlap = this.parameterSwitchEnabled("Overlap");
    this.setParameterDisabled("OverlapOrderNumber", !overlap, "开启篮子首尾覆盖后才可配置。");
    this.setParameterDisabled("OverlapPercent", !overlap, "开启篮子首尾覆盖后才可配置。");

    const trailStart = Number(this.strategyParamInput("TrailStart")?.value || 0);
    const trailDistance = Number(this.strategyParamInput("TrailDistance")?.value || 0);
    this.setParameterDisabled("TrailDistance", trailStart <= 0, "追踪启动为 0 时，追踪止盈处于关闭状态。");
    const takeProfitPoints = ["TP", "TP2", "TP3", "TP4"]
      .map((key) => Number(this.strategyParamInput(key)?.value))
      .filter((value) => Number.isFinite(value) && value > 0);
    const maximumTakeProfit = takeProfitPoints.length ? Math.max(...takeProfitPoints) : 0;
    let trailingMessage = "";
    let trailingTone = "info";
    if (trailStart <= 0) trailingMessage = "追踪止盈已关闭；填写大于 0 的追踪启动点数后启用。";
    else if (maximumTakeProfit > 0 && trailStart >= maximumTakeProfit) {
      trailingMessage = `追踪启动 ${trailStart} 点不低于最高止盈 ${maximumTakeProfit} 点，固定止盈会先触发，追踪止盈不会生效。`;
      trailingTone = "warning";
    } else if (trailDistance >= trailStart) {
      trailingMessage = "追踪距离必须小于追踪启动点数，否则无法形成有效的盈利保护区间。";
      trailingTone = "warning";
    } else trailingMessage = `盈利达到 ${trailStart} 点后启动追踪，从最佳价格回撤 ${trailDistance} 点时平仓。`;
    this.setParameterGroupMessage("金额止损与追踪止盈", trailingMessage, trailingTone);
  }

  strategyParamInput(key) {
    return this.qa("[data-param-key]").find((input) => input.dataset.paramKey === key) || null;
  }

  openConfigDialog() {
    const dialog = this.q("#backtest-config-dialog");
    dialog.classList.remove("hidden");
    this.q("#open-backtest-config").setAttribute("aria-expanded", "true");
    this.q("#close-backtest-config").focus();
  }

  closeConfigDialog(restoreFocus = true) {
    this.closeSymbolPicker(false);
    this.q("#backtest-config-dialog")?.classList.add("hidden");
    const opener = this.q("#open-backtest-config");
    opener?.setAttribute("aria-expanded", "false");
    if (restoreFocus) opener?.focus();
  }

  closeLotCalculator() {
    this.q("#lot-calculator-dialog")?.classList.add("hidden");
  }

  async openLotCalculator() {
    const symbol = this.primarySymbol();
    const lotInput = this.strategyParamInput("Lot");
    if (!this.isBasketStrategy() || !lotInput || !symbol) {
      this.showBanner("请先选择马丁篮子策略和有效交易品种。", "error");
      return;
    }
    const dialog = this.q("#lot-calculator-dialog");
    this.q("#calculator-initial-capital").value = this.q("#initial-capital").value;
    this.q("#calculator-lot").value = lotInput.value;
    this.q("#calculator-leverage").value = this.q("#leverage").value;
    this.q("#calculator-base-points").value = this.strategyParamInput("TP")?.value || "100";
    this.q("#calculator-auto-box").checked = this.parameterSwitchEnabled("AutoBoxRange");
    this.q("#calculator-atr-period").value = this.strategyParamInput("AutoBoxRangeDailyATRperiod")?.value || "30";
    this.q("#calculator-atr-factor").value = this.strategyParamInput("AutoBoxRangeDailyATRfactor")?.value || "0.2";
    this.lotCalculatorManualBoxRange = this.strategyParamInput("BoxRange")?.value || "30";
    this.q("#calculator-apply-status").textContent = "";
    dialog.classList.remove("hidden");
    this.q("#calculator-loading").classList.remove("hidden");
    this.q("#calculator-content").classList.add("hidden");
    this.q("#calculator-error").classList.add("hidden");
    const requestId = ++this.lotCalculatorRequest;
    try {
      const atrPeriod = this.calculatorAtrPeriod();
      const quote = await this.api(`/position-calculator?symbol=${encodeURIComponent(symbol)}&atr_period=${atrPeriod}`);
      if (requestId !== this.lotCalculatorRequest || dialog.classList.contains("hidden")) return;
      this.lotCalculatorQuote = quote;
      this.q("#calculator-loading").classList.add("hidden");
      this.q("#calculator-content").classList.remove("hidden");
      this.renderLotCalculator(true);
      this.q("#calculator-base-points").focus();
    } catch (error) {
      if (requestId !== this.lotCalculatorRequest) return;
      this.q("#calculator-loading").classList.add("hidden");
      const errorBox = this.q("#calculator-error");
      errorBox.textContent = error.message || "仓位计算器暂时不可用";
      errorBox.classList.remove("hidden");
    }
  }

  calculatorAtrPeriod() {
    const value = Math.round(Number(this.q("#calculator-atr-period")?.value || 30));
    return Number.isFinite(value) ? Math.min(365, Math.max(2, value)) : 30;
  }

  calculatorAtrFactor() {
    const value = Number(this.q("#calculator-atr-factor")?.value || 0.2);
    return Number.isFinite(value) && value > 0 ? Math.min(10, value) : 0.2;
  }

  calculatorEffectiveBoxPoints() {
    if (!this.q("#calculator-auto-box")?.checked) return null;
    const quote = this.lotCalculatorQuote || {};
    if (Number(quote.daily_atr_period) !== this.calculatorAtrPeriod()) return null;
    const atr = Number(quote.daily_atr);
    const pointSize = Number(quote.strategy_point_size || 0.01);
    if (!Number.isFinite(atr) || atr <= 0 || !Number.isFinite(pointSize) || pointSize <= 0) return null;
    return atr * this.calculatorAtrFactor() / pointSize;
  }

  async refreshCalculatorAtr() {
    const dialog = this.q("#lot-calculator-dialog");
    if (!dialog || dialog.classList.contains("hidden") || !this.q("#calculator-auto-box").checked) {
      this.syncCalculatorBoxMode();
      return;
    }
    const symbol = this.primarySymbol();
    const status = this.q("#calculator-atr-status");
    status.textContent = "正在按新周期计算日线 ATR…";
    status.className = "calculator-atr-status loading";
    const requestId = ++this.lotCalculatorRequest;
    try {
      const quote = await this.api(`/position-calculator?symbol=${encodeURIComponent(symbol)}&atr_period=${this.calculatorAtrPeriod()}`);
      if (requestId !== this.lotCalculatorRequest || dialog.classList.contains("hidden")) return;
      this.lotCalculatorQuote = quote;
      this.renderLotCalculator();
    } catch (error) {
      if (requestId !== this.lotCalculatorRequest) return;
      status.textContent = error.message || "日线 ATR 暂时无法计算";
      status.className = "calculator-atr-status error";
      this.syncCalculatorBoxMode();
    }
  }

  lotCalculatorValues() {
    const quote = this.lotCalculatorQuote || {};
    const price = Number(quote.price);
    const initialCapital = Number(this.q("#calculator-initial-capital").value);
    const lot = Number(this.q("#calculator-lot").value);
    const leverage = Number(this.q("#calculator-leverage").value);
    const basePoints = Number(this.q("#calculator-base-points").value);
    const pointSize = Number(quote.strategy_point_size || 0.01);
    if (![price, initialCapital, lot, leverage, basePoints, pointSize].every((value) => Number.isFinite(value) && value > 0)) return null;
    const oneLotNotional = price;
    const oneLotMargin = oneLotNotional / leverage;
    const positionNotional = oneLotNotional * lot;
    const positionMargin = positionNotional / leverage;
    const remainingAvailableBalance = initialCapital - positionMargin;
    const priceMove = basePoints * pointSize;
    const priceMovePct = priceMove / price * 100;
    const longTakeProfitPrice = price + priceMove;
    const shortTakeProfitPrice = priceMove < price ? price - priceMove : null;
    const grossProfit = priceMove * lot;
    const grossRoePct = grossProfit / positionMargin * 100;
    const roundTripCostRate = (Number(this.q("#fee").value || 0) + Number(this.q("#slippage").value || 0)) * 2 / 10000;
    const roundTripCost = positionNotional * roundTripCostRate;
    const estimatedNetProfit = grossProfit - roundTripCost;
    const estimatedNetRoePct = estimatedNetProfit / positionMargin * 100;
    return { price, initialCapital, lot, leverage, basePoints, pointSize, oneLotNotional, oneLotMargin, positionNotional, positionMargin, remainingAvailableBalance, priceMove, priceMovePct, longTakeProfitPrice, shortTakeProfitPrice, grossProfit, grossRoePct, roundTripCost, estimatedNetProfit, estimatedNetRoePct };
  }

  calculatedPointSettings(basePoints) {
    const scaled = (ratio, minimum = 1) => Math.max(minimum, Math.round(basePoints * ratio));
    return {
      TP: scaled(1),
      TP2: scaled(0.8),
      TP3: scaled(0.5),
      TP4: scaled(0.3),
      Distance: scaled(1.5),
      MaxSpred: scaled(0.5),
      TrailStart: scaled(6),
      TrailDistance: scaled(1),
      BoxRange: scaled(0.3),
      BoxBufferPips: scaled(0.05),
    };
  }

  calculatorFact(label, value, note = "") {
    const fact = this.node("div", "calculator-fact");
    fact.append(this.node("span", "", label), this.node("strong", "", value));
    if (note) fact.append(this.node("small", "", note));
    return fact;
  }

  calculatorTargetRow(direction, targetPrice, movementLabel, movementPercent, grossProfit, grossRoePct) {
    const row = this.node("tr", direction === "做多" ? "long" : "short");
    [
      ["方向", direction],
      ["止盈价格", targetPrice],
      ["目标涨跌幅", `${movementLabel} ${this.calculatorPercent(Math.abs(movementPercent))}`],
      ["预计毛利润", this.calculatorMoney(grossProfit, true)],
      ["杠杆毛 ROE", this.calculatorPercent(grossRoePct)],
    ].forEach(([label, value]) => {
      const cell = this.node("td");
      cell.dataset.label = label;
      cell.append(this.node("strong", "", value));
      row.append(cell);
    });
    return row;
  }

  calculatorMoney(value, signed = false) {
    const numeric = Number(value);
    if (!Number.isFinite(numeric)) return "--";
    const digits = numeric !== 0 && Math.abs(numeric) < 0.01 ? 6 : 2;
    const amount = Math.abs(numeric).toLocaleString("zh-CN", {
      minimumFractionDigits: 2,
      maximumFractionDigits: digits,
    });
    const sign = signed && numeric !== 0 ? (numeric > 0 ? "+" : "-") : numeric < 0 ? "-" : "";
    return `${sign}${amount} U`;
  }

  calculatorPercent(value, signed = false) {
    const numeric = Number(value);
    if (!Number.isFinite(numeric)) return "--";
    const sign = signed && numeric > 0 ? "+" : "";
    return `${sign}${numeric.toLocaleString("zh-CN", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}%`;
  }

  calculatorPointField(key, label, value, { unit = "点", integer = false, minimum = 0 } = {}) {
    const field = this.node("label", "calculator-point-field");
    const input = this.node("input");
    input.type = "number";
    input.min = String(minimum);
    input.step = integer ? "1" : "any";
    input.inputMode = integer ? "numeric" : "decimal";
    input.value = String(value);
    input.dataset.calculatorParamKey = key;
    field.append(this.node("small", "", label));
    const control = this.node("div", "calculator-point-control");
    control.append(input, this.node("span", "", unit));
    field.append(control);
    return field;
  }

  calculatorTakeProfitTier(code, title, startKey, startValue, pointKey, pointValue) {
    const card = this.node("section", "calculator-tier-card");
    const head = this.node("div", "calculator-tier-card-head");
    head.append(this.node("span", "", code), this.node("strong", "", title));
    const fields = this.node("div", "calculator-tier-card-fields");
    fields.append(
      this.calculatorPointField(startKey, "起始订单数", startValue, { unit: "单", integer: true, minimum: 1 }),
      this.calculatorPointField(pointKey, "止盈点数", pointValue, { unit: "点", minimum: 0.000001 }),
    );
    card.append(head, fields);
    return card;
  }

  calculatorStrategyParamValue(key, fallback, { integer = false } = {}) {
    const value = Number(this.strategyParamInput(key)?.value);
    if (!Number.isFinite(value) || value < 0) return fallback;
    return integer ? Math.max(1, Math.round(value)) : value;
  }

  syncCalculatorBoxMode() {
    const autoBox = Boolean(this.q("#calculator-auto-box")?.checked);
    const periodInput = this.q("#calculator-atr-period");
    const factorInput = this.q("#calculator-atr-factor");
    const boxInput = this.q('[data-calculator-param-key="BoxRange"]');
    const status = this.q("#calculator-atr-status");
    if (!periodInput || !factorInput || !status) return;
    periodInput.disabled = !autoBox;
    factorInput.disabled = !autoBox;
    if (!boxInput) return;
    boxInput.disabled = autoBox;
    boxInput.closest(".calculator-point-field")?.classList.toggle("calculator-point-field-disabled", autoBox);
    if (!autoBox) {
      if (this.lotCalculatorManualBoxRange != null && this.lotCalculatorManualBoxRange !== "") {
        boxInput.value = String(this.lotCalculatorManualBoxRange);
      }
      delete boxInput.dataset.atrComputed;
      status.textContent = "固定箱体模式：回测直接使用可编辑的“箱体范围（点）”。";
      status.className = "calculator-atr-status manual";
      return;
    }
    const quote = this.lotCalculatorQuote || {};
    const effectivePoints = this.calculatorEffectiveBoxPoints();
    boxInput.dataset.atrComputed = "1";
    if (Number.isFinite(effectivePoints) && effectivePoints > 0) {
      boxInput.value = String(Number(effectivePoints.toFixed(4)));
      status.textContent = `ATR 自适应箱体：日线 ATR ${this.price(quote.daily_atr)} USDT × ${this.number(this.calculatorAtrFactor(), 4)} ÷ ${this.price(quote.strategy_point_size || 0.01)} USDT/点 = ${this.number(effectivePoints, 2)} 点。实际回测会按每根日线 ATR 动态变化。`;
      status.className = "calculator-atr-status ready";
      return;
    }
    boxInput.value = "";
    status.textContent = `ATR 自适应箱体已开启，但当前 ${this.calculatorAtrPeriod()} 日 ATR 暂不可用；实际回测仍会按历史日线数据严格计算。`;
    status.className = "calculator-atr-status error";
  }

  syncCalculatorGridMode() {
    const status = this.q("#calculator-grid-status");
    const input = this.q('[data-calculator-param-key="GridDrift"]');
    if (!status || !input) return;
    const gridDrift = Math.max(1, Math.round(Number(input.value || 1)));
    const maxOrders = Math.max(1, Math.round(Number(this.strategyParamInput("MaxOrders")?.value || 1)));
    if (gridDrift < maxOrders) {
      status.textContent = `模式切换：已有 ${gridDrift} 手后，第 ${gridDrift + 1} 手起按网格间距和同方向订单序列加仓。`;
      status.className = "calculator-grid-status ready";
      return;
    }
    status.textContent = `当前不会切换网格：阈值 ${gridDrift} 手不小于最大订单数 ${maxOrders} 手。请将阈值设置为小于 ${maxOrders}。`;
    status.className = "calculator-grid-status warning";
  }

  updateCalculatorPointRatios() {
    const basePoints = Number(this.q("#calculator-base-points").value);
    if (!Number.isFinite(basePoints) || basePoints < 0) return;
    const settings = this.calculatedPointSettings(basePoints);
    Object.entries(settings).forEach(([key, value]) => {
      if (key === "TP") return;
      const input = this.q(`[data-calculator-param-key="${key}"]`);
      if (input) input.value = String(value);
      if (key === "BoxRange" && !this.q("#calculator-auto-box")?.checked) {
        this.lotCalculatorManualBoxRange = String(value);
      }
    });
  }

  calculatorPointSettings() {
    const settings = {};
    this.qa("[data-calculator-param-key]").forEach((input) => {
      if (input.disabled) return;
      const value = Number(input.value);
      if (Number.isFinite(value) && value >= 0) settings[input.dataset.calculatorParamKey] = value;
    });
    return settings;
  }

  renderLotCalculator(resetPointFields = false) {
    const values = this.lotCalculatorValues();
    if (!values) return;
    const quote = this.lotCalculatorQuote;
    const settings = this.calculatedPointSettings(values.basePoints);
    const manualBoxRange = Number(this.lotCalculatorManualBoxRange);
    if (Number.isFinite(manualBoxRange) && manualBoxRange >= 0) settings.BoxRange = manualBoxRange;
    const shortTakeProfitLabel = values.shortTakeProfitPrice == null ? "不可用" : `${this.price(values.shortTakeProfitPrice)} USDT`;
    const rawMarketChangePercent = quote.price_change_percent_24h;
    const marketChangePercent = Number(rawMarketChangePercent);
    const hasMarketChange = rawMarketChangePercent != null && Number.isFinite(marketChangePercent);
    this.q("#calculator-symbol").textContent = quote.symbol || this.primarySymbol();
    this.q("#calculator-price").textContent = `${this.price(values.price)} USDT`;
    this.q("#calculator-source").textContent = `Binance 24h 实时行情 · 策略 1 点=${this.price(values.pointSize)} USDT`;
    this.q("#calculator-facts").replaceChildren(
      this.calculatorFact("回测初始资金", this.calculatorMoney(values.initialCapital), `开仓后剩余可用 ${this.calculatorMoney(values.remainingAvailableBalance)}`),
      this.calculatorFact("1 手名义价值", this.calculatorMoney(values.oneLotNotional), `${values.leverage}x 保证金 ${this.calculatorMoney(values.oneLotMargin)}`),
      this.calculatorFact(`${this.quantity(values.lot)} 手名义价值`, this.calculatorMoney(values.positionNotional), `开仓保证金 ${this.calculatorMoney(values.positionMargin)}`),
      this.calculatorFact(`基础止盈 ${this.number(values.basePoints, 2)} 点`, `做多 ${this.price(values.longTakeProfitPrice)} USDT`, `做空 ${shortTakeProfitLabel} · 价差 ±${this.price(values.priceMove)} USDT（${this.number(values.priceMovePct, 4)}%）`),
      this.calculatorFact("预计毛利润", this.calculatorMoney(values.grossProfit, true), `保证金毛 ROE ${this.calculatorPercent(values.grossRoePct)}`),
      this.calculatorFact("预计净利润", this.calculatorMoney(values.estimatedNetProfit, true), `双边成本约 ${this.calculatorMoney(values.roundTripCost)}`),
      this.calculatorFact("预计净 ROE", this.calculatorPercent(values.estimatedNetRoePct), `${values.leverage}x 逐仓保证金口径`),
    );
    const marketChange = this.q("#calculator-market-change");
    marketChange.textContent = hasMarketChange
      ? `Binance 24h 涨跌幅 ${this.calculatorPercent(marketChangePercent, true)}`
      : "Binance 24h 涨跌幅暂缺";
    marketChange.className = hasMarketChange ? this.toneClass(marketChangePercent) : "neutral";
    this.q("#calculator-summary-title").textContent = `基础止盈 ${this.number(values.basePoints, 2)} 点 · 止盈计算表`;
    this.q("#calculator-summary").replaceChildren(
      this.calculatorTargetRow(
        "做多",
        `${this.price(values.longTakeProfitPrice)} USDT`,
        "涨幅",
        values.priceMovePct,
        values.grossProfit,
        values.grossRoePct,
      ),
      this.calculatorTargetRow(
        "做空",
        shortTakeProfitLabel,
        "跌幅",
        values.priceMovePct,
        values.grossProfit,
        values.grossRoePct,
      ),
    );
    this.q("#calculator-note").textContent = `扣除双边手续费与滑点后：预计净利润 ${this.calculatorMoney(values.estimatedNetProfit, true)} · 净 ROE ${this.calculatorPercent(values.estimatedNetRoePct)} · ${this.quantity(values.lot)} 手占用保证金 ${this.calculatorMoney(values.positionMargin)}。`;
    if (resetPointFields || !this.q("#calculator-tier-levels").children.length) {
      this.q("#calculator-tier-levels").replaceChildren(
        this.calculatorTakeProfitTier(
          "TP2",
          "第二档止盈",
          "Kol_Ord_for_TP2",
          this.calculatorStrategyParamValue("Kol_Ord_for_TP2", 2, { integer: true }),
          "TP2",
          this.calculatorStrategyParamValue("TP2", settings.TP2),
        ),
        this.calculatorTakeProfitTier(
          "TP3",
          "第三档止盈",
          "Kol_Ord_for_TP3",
          this.calculatorStrategyParamValue("Kol_Ord_for_TP3", 5, { integer: true }),
          "TP3",
          this.calculatorStrategyParamValue("TP3", settings.TP3),
        ),
        this.calculatorTakeProfitTier(
          "TP4",
          "第四档止盈",
          "Kol_Ord_for_TP4",
          this.calculatorStrategyParamValue("Kol_Ord_for_TP4", 7, { integer: true }),
          "TP4",
          this.calculatorStrategyParamValue("TP4", settings.TP4),
        ),
      );
    }
    if (resetPointFields || !this.q("#calculator-point-preview").children.length) {
      this.q("#calculator-point-preview").replaceChildren(...[
        ["GridDrift", "第几手后切换网格", this.calculatorStrategyParamValue("GridDrift", 100, { integer: true }), { unit: "手", integer: true, minimum: 1 }],
        ["Distance", "网格间距", settings.Distance], ["MaxSpred", "最大点差", settings.MaxSpred],
        ["TrailStart", "追踪启动", settings.TrailStart], ["TrailDistance", "追踪距离", settings.TrailDistance],
        ["BoxRange", "箱体范围", settings.BoxRange], ["BoxBufferPips", "箱体缓冲", settings.BoxBufferPips],
      ].map(([key, label, value, options]) => this.calculatorPointField(key, label, value, options)));
    }
    this.syncCalculatorBoxMode();
    this.syncCalculatorGridMode();
  }

  applyCalculatorBoxSettings() {
    const autoBox = Boolean(this.q("#calculator-auto-box")?.checked);
    const values = {
      AutoBoxRange: autoBox ? 1 : 0,
      AutoBoxRangeDailyATRperiod: this.calculatorAtrPeriod(),
      AutoBoxRangeDailyATRfactor: this.calculatorAtrFactor(),
    };
    Object.entries(values).forEach(([key, value]) => {
      const input = this.strategyParamInput(key);
      if (!input) return;
      if (input.type === "checkbox") input.checked = Boolean(value);
      else input.value = String(value);
      input.dispatchEvent(new Event(input.type === "checkbox" ? "change" : "input", { bubbles: true }));
    });
    this.syncParameterDependencies();
  }

  applyCalculatorPositionSettings() {
    const values = this.lotCalculatorValues();
    const lotInput = this.strategyParamInput("Lot");
    if (!values || !lotInput) return false;
    this.q("#initial-capital").value = String(values.initialCapital);
    this.q("#initial-capital").dispatchEvent(new Event("input", { bubbles: true }));
    lotInput.value = String(values.lot);
    lotInput.dispatchEvent(new Event("input", { bubbles: true }));
    this.q("#leverage").value = String(values.leverage);
    this.q("#leverage").dispatchEvent(new Event("change", { bubbles: true }));
    return true;
  }

  applyCalculatedPoints(scope) {
    const values = this.lotCalculatorValues();
    if (!values) return;
    if (scope === "position") {
      if (!this.applyCalculatorPositionSettings()) return;
      const message = `已写入初始资金 ${this.calculatorMoney(values.initialCapital)}、初始手数 ${this.quantity(values.lot)} 和 Binance ${values.leverage}x 杠杆；点击右上角 × 关闭。`;
      this.q("#calculator-apply-status").textContent = message;
      this.showBanner(message, "success");
      return;
    }
    const settings = this.calculatorPointSettings();
    const takeProfitKeys = new Set([
      "TP", "Kol_Ord_for_TP2", "TP2", "Kol_Ord_for_TP3", "TP3", "Kol_Ord_for_TP4", "TP4",
    ]);
    if (scope === "take-profit" || scope === "all") {
      const thresholds = [
        1,
        Number(settings.Kol_Ord_for_TP2),
        Number(settings.Kol_Ord_for_TP3),
        Number(settings.Kol_Ord_for_TP4),
      ];
      const validThresholds = thresholds.slice(1).every((value) => Number.isInteger(value) && value >= 1)
        && thresholds.every((value, index) => index === 0 || value > thresholds[index - 1]);
      if (!validThresholds) {
        const message = "分级止盈的起始订单数必须是递增整数：TP1 固定为 1，且 TP2 < TP3 < TP4。";
        this.q("#calculator-apply-status").textContent = message;
        this.showBanner(message, "error");
        return;
      }
    }
    let applied = 0;
    Object.entries(settings).forEach(([key, value]) => {
      if (scope === "take-profit" && !takeProfitKeys.has(key)) return;
      const input = this.strategyParamInput(key);
      if (!input) return;
      input.value = String(value);
      input.dispatchEvent(new Event("input", { bubbles: true }));
      applied += 1;
    });
    if (scope === "all") {
      this.applyCalculatorPositionSettings();
      this.applyCalculatorBoxSettings();
    }
    const message = scope === "take-profit"
      ? `已写入 ${applied} 项分级止盈参数（起始订单数与止盈点数）；点击右上角 × 关闭。`
      : `已写入初始资金、手数、杠杆和 ${applied} 项可编辑点数；点击右上角 × 关闭。`;
    this.q("#calculator-apply-status").textContent = message;
    this.showBanner(message, "success");
  }

  resolveBounds(symbol = this.primarySymbol(), timeframe = this.q("#timeframe").value) {
    const source = this.catalog.bounds || {};
    let bound = source;
    if (Array.isArray(source)) {
      bound = source.find((item) => (!item.symbol || item.symbol === symbol) && (!item.timeframe || item.timeframe === timeframe)) || {};
    } else if (source[symbol]?.[timeframe]) bound = source[symbol][timeframe];
    else if (source[symbol]) bound = source[symbol];
    else if (source[`${symbol}:${timeframe}`]) bound = source[`${symbol}:${timeframe}`];
    const min = this.dateOnly(bound?.min_date ?? bound?.start_date ?? bound?.start ?? source.min_date ?? source.start_date ?? source.start);
    const max = this.dateOnly(bound?.max_date ?? bound?.end_date ?? bound?.end ?? source.max_date ?? source.end_date ?? source.end);
    const barsValue = Number(bound?.bars);
    const bars = Number.isFinite(barsValue) && barsValue >= 0 ? barsValue : null;
    return { min, max, bars };
  }

  renderAvailability({ min = "", max = "", bars = null } = {}) {
    const symbol = this.primarySymbol();
    const timeframe = this.q("#timeframe").value;
    const container = this.q("#data-availability");
    if (min && max) {
      this.q("#available-range").textContent = `${min} — ${max}`;
      this.q("#available-bars").textContent = `${bars == null ? "数量待校验" : `${new Intl.NumberFormat("zh-CN").format(bars)} 根`} · ${timeframe || "--"} K 线 · 当前历史库存`;
      container.classList.remove("empty");
      this.q("#data-bound").textContent = bars == null ? "已有历史数据" : `${this.compactNumber(bars)} 根可用`;
      return;
    }
    this.q("#available-range").textContent = symbol && timeframe ? "暂无已缓存的数据范围" : "请选择品种与周期";
    this.q("#available-bars").textContent = this.isBasketStrategy()
      ? "首次回测将按需同步；完成后显示具体范围"
      : "运行回测时将按行情库存动态校验";
    container.classList.add("empty");
    this.q("#data-bound").textContent = this.isBasketStrategy() ? "历史 K 线按需同步" : "按行情库存动态校验";
  }

  syncBounds(resetBasketRange = false) {
    const bounds = this.resolveBounds();
    const { min, max } = bounds;
    this.renderAvailability(bounds);
    this.qa("[data-months]").forEach((button) => {
      button.classList.remove("active");
      button.setAttribute("aria-pressed", "false");
    });
    const feedback = this.q("#range-feedback");
    feedback.textContent = min && max ? `当前历史库存覆盖 ${min} — ${max}` : "点击快捷区间可调整回测日期";
    feedback.classList.remove("limited");
    if (this.isBasketStrategy()) {
      const start = this.q("#start-date");
      const end = this.q("#end-date");
      const today = this.dateOnly(new Date());
      const availableEnd = max || today;
      if (!end.value || end.value > availableEnd || (min && end.value < min)) end.value = availableEnd;
      // A basket replay needs daily ATR history before its evaluation window.
      // Start new basket selections with a shorter research window so the
      // default request leaves enough warmup, while keeping longer ranges
      // available through the explicit range controls.
      if (resetBasketRange || !start.value || start.value > end.value) {
        start.value = this.maxDate(min, this.shiftMonths(end.value, -1));
      }
      if (min && start.value < min) start.value = min;
      start.min = min || "";
      start.max = availableEnd;
      end.min = min || "";
      end.max = availableEnd;
      this.captureActiveSymbolConfiguration(false);
      return;
    }
    const start = this.q("#start-date");
    const end = this.q("#end-date");
    start.min = min || "";
    start.max = max || "";
    end.min = min || "";
    end.max = max || "";
    if (max) {
      end.value = !end.value || end.value > max || (min && end.value < min) ? max : end.value;
      const defaultStart = this.shiftMonths(max, -3);
      start.value = !start.value || start.value > end.value || (min && start.value < min) ? this.maxDate(min, defaultStart) : start.value;
    } else {
      const today = this.dateOnly(new Date());
      if (!end.value) end.value = today;
      if (!start.value) start.value = this.shiftMonths(today, -3);
    }
    this.captureActiveSymbolConfiguration(false);
  }

  applyRange(months) {
    const { min, max } = this.resolveBounds();
    const end = max || this.q("#end-date").value || this.dateOnly(new Date());
    const requestedStart = months === "all" ? (min || this.shiftMonths(end, -12)) : this.shiftMonths(end, -Number(months));
    const start = this.maxDate(min, requestedStart);
    this.q("#end-date").value = end;
    this.q("#start-date").value = start;
    this.qa("[data-months]").forEach((button) => {
      const active = button.dataset.months === String(months);
      button.classList.toggle("active", active);
      button.setAttribute("aria-pressed", String(active));
    });
    const labels = { 3: "近 3 月", 6: "6 个月", 12: "近 1 年", all: "可用最大" };
    const clipped = months !== "all" && Boolean(min && requestedStart && requestedStart < min);
    const feedback = this.q("#range-feedback");
    feedback.classList.toggle("limited", clipped);
    if (clipped) {
      feedback.textContent = `${labels[months] || "所选区间"}超出历史库存，已自动使用最大可用范围 ${start} — ${end}`;
    } else if (months === "all" && !min) {
      feedback.textContent = `暂无明确的历史起点，暂按最近 1 年 ${start} — ${end}`;
    } else {
      feedback.textContent = `已切换至${labels[months] || "所选区间"}：${start} — ${end}`;
    }
    this.captureActiveSymbolConfiguration(true);
  }

  collectParams() {
    const params = {};
    this.qa("[data-param-key]").forEach((input) => {
      const type = input.dataset.paramType;
      const value = input.type === "checkbox" || type === "boolean" || type === "bool"
        ? (input.checked ? 1 : 0)
        : Number(input.value);
      if (Number.isFinite(value)) params[input.dataset.paramKey] = value;
    });
    return params;
  }

  executionProfile() {
    return {
      position_size_pct: Number(this.q("#position-size").value),
      leverage: Number(this.q("#leverage").value),
      margin_mode: "isolated",
      fee_bps: Number(this.q("#fee").value),
      slippage_bps: Number(this.q("#slippage").value),
      stop_loss_pct: Number(this.q("#stop-loss").value),
      take_profit_pct: Number(this.q("#take-profit").value),
      max_holding_bars: Number(this.q("#max-holding").value),
    };
  }

  applyExecutionProfile(execution = {}) {
    const mapping = {
      position_size_pct: "#position-size",
      leverage: "#leverage",
      fee_bps: "#fee",
      slippage_bps: "#slippage",
      stop_loss_pct: "#stop-loss",
      take_profit_pct: "#take-profit",
      max_holding_bars: "#max-holding",
    };
    Object.entries(mapping).forEach(([key, selector]) => {
      if (execution[key] == null) return;
      const input = this.q(selector);
      if (input) input.value = String(execution[key]);
    });
  }

  applyStrategyParameters(parameters = {}) {
    Object.entries(parameters).forEach(([key, value]) => {
      const input = this.strategyParamInput(key);
      if (!input || value == null) return;
      if (input.type === "checkbox") input.checked = Boolean(Number(value));
      else input.value = String(value);
    });
    this.syncParameterDependencies();
  }

  loadSelectedParameterProfiles() {
    return Promise.allSettled(this.symbolsForRun().map((symbol) => this.loadParameterProfile(symbol)));
  }

  async loadParameterProfile(symbol = this.primarySymbol()) {
    const strategyId = this.strategyId;
    if (!strategyId || !symbol || this.symbolProfileLoaded.has(symbol)) return;
    const requestId = ++this.profileRequest;
    this.symbolProfileRequests.set(symbol, requestId);
    const status = this.q("#profile-status");
    if (symbol === this.primarySymbol()) status.textContent = `正在读取 ${symbol} 的策略参数…`;
    try {
      const detail = await this.api(`/strategy-parameters/${encodeURIComponent(strategyId)}?symbol=${encodeURIComponent(symbol)}`);
      if (this.symbolProfileRequests.get(symbol) !== requestId || strategyId !== this.strategyId) return;
      const scope = detail?.effective?.scope;
      if (!this.symbolConfigurationDirty.has(symbol)) {
        const current = this.symbolConfigurations[symbol] || this.readCurrentSymbolConfiguration();
        this.symbolConfigurations[symbol] = this.cloneSymbolConfiguration({
          ...current,
          ...(detail?.effective?.execution || {}),
          params: { ...(current.params || {}), ...(detail?.effective?.parameters || {}) },
          profile_scope: scope,
        });
        if (symbol === this.primarySymbol()) {
          this.applySymbolConfiguration(this.symbolConfigurations[symbol]);
          this.syncBounds();
        }
      }
      this.symbolProfileLoaded.add(symbol);
      this.renderSymbolConfigTabs();
      if (symbol === this.primarySymbol()) this.renderProfileStatus(symbol, scope);
    } catch (error) {
      if (this.symbolProfileRequests.get(symbol) !== requestId || strategyId !== this.strategyId) return;
      if (symbol === this.primarySymbol()) status.textContent = `参数配置读取失败：${error.message}`;
    }
  }

  async saveParameterProfile(scope) {
    if (this.profileSaveInFlight || !this.validateParameterProfile()) return;
    this.captureActiveSymbolConfiguration(true);
    const activeSymbol = this.primarySymbol();
    const symbols = scope === "symbol" ? [activeSymbol].filter(Boolean) : [null];
    if (scope === "symbol" && !activeSymbol) {
      this.showBanner("请先选择至少一个交易品种。", "error");
      return;
    }
    this.profileSaveInFlight = true;
    this.setProfileButtons(true);
    const status = this.q("#profile-status");
    status.textContent = scope === "default" ? "正在保存策略默认交易参数…" : `正在保存 ${activeSymbol} 的专有参数…`;
    try {
      for (const symbol of symbols) {
        const configuration = this.symbolConfigurations[symbol || activeSymbol] || this.readCurrentSymbolConfiguration();
        const body = {
          scope,
          params: { ...(configuration.params || {}) },
          execution: this.executionProfileFromConfiguration(configuration),
        };
        await this.api(`/strategy-parameters/${encodeURIComponent(this.strategyId)}`, {
          method: "PUT",
          body: JSON.stringify({ ...body, symbol }),
        });
      }
      const message = scope === "default"
        ? "默认交易策略参数已保存；没有币种专有配置时，模拟盘与实盘将使用这组参数。"
        : `${activeSymbol} 的专有交易策略参数已保存；模拟盘与实盘将优先使用。`;
      if (scope === "symbol") {
        this.symbolConfigurations[activeSymbol].profile_scope = "symbol";
        this.symbolConfigurationDirty.delete(activeSymbol);
        this.symbolProfileLoaded.add(activeSymbol);
        this.renderSymbolConfigTabs();
      }
      status.textContent = message;
      this.showBanner(message, "success");
    } catch (error) {
      status.textContent = `保存失败：${error.message}`;
      this.showBanner(`策略参数保存失败：${error.message}`, "error");
    } finally {
      this.profileSaveInFlight = false;
      this.setProfileButtons(false);
    }
  }

  setProfileButtons(disabled) {
    const unavailable = disabled || this.runningBacktest || !this.strategyId;
    this.q("#save-default-profile").disabled = unavailable;
    this.q("#save-symbol-profile").disabled = unavailable || !this.symbolsForRun().length;
  }

  validateParameterProfile() {
    if (!this.strategyId) {
      this.showBanner("请先选择一个策略。", "error");
      return false;
    }
    const fields = [
      ...this.qa("[data-param-key]"),
      this.q("#initial-capital"), this.q("#position-size"), this.q("#leverage"), this.q("#fee"), this.q("#slippage"),
      this.q("#stop-loss"), this.q("#take-profit"), this.q("#max-holding"),
    ].filter(Boolean);
    const invalid = fields.find((field) => !field.checkValidity());
    if (invalid) {
      invalid.reportValidity();
      this.showBanner("请先修正无效的策略参数。", "error");
      return false;
    }
    return true;
  }

  executionProfileFromConfiguration(configuration = {}) {
    return {
      initial_capital: Number(configuration.initial_capital),
      position_size_pct: Number(configuration.position_size_pct),
      leverage: Number(configuration.leverage),
      margin_mode: "isolated",
      fee_bps: Number(configuration.fee_bps),
      slippage_bps: Number(configuration.slippage_bps),
      stop_loss_pct: Number(configuration.stop_loss_pct),
      take_profit_pct: Number(configuration.take_profit_pct),
      max_holding_bars: Number(configuration.max_holding_bars),
    };
  }

  symbolConfigurationError(configuration = {}) {
    const numericRules = {
      initial_capital: [1, Number.POSITIVE_INFINITY, "初始资金"],
      position_size_pct: [0.01, 100, "单次仓位"],
      leverage: [1, 125, "杠杆"],
      fee_bps: [0, 1000, "手续费"],
      slippage_bps: [0, 1000, "滑点"],
      stop_loss_pct: [0, 99.9, "止损"],
      take_profit_pct: [0, 99.9, "止盈"],
      max_holding_bars: [0, 50000, "最大持有 K 线"],
    };
    for (const [key, [minimum, maximum, label]] of Object.entries(numericRules)) {
      const value = Number(configuration[key]);
      if (!Number.isFinite(value) || value < minimum || value > maximum) return `${label}参数无效`;
    }
    const definitions = (Array.isArray(this.selectedStrategy()?.params) ? this.selectedStrategy().params : [])
      .filter((param) => !this.isBasketStrategy() || param?.key !== "BoxTimeFrameMinutes");
    for (const definition of definitions) {
      const key = String(definition?.key || "");
      if (!key) continue;
      const value = configuration.params?.[key];
      const type = String(definition.type || "number").toLowerCase();
      if (type === "boolean" || type === "bool") continue;
      const numeric = Number(value);
      if (!Number.isFinite(numeric)) return `${definition.label || key}参数无效`;
      if (definition.min != null && numeric < Number(definition.min)) return `${definition.label || key}不能小于 ${definition.min}`;
      if (definition.max != null && numeric > Number(definition.max)) return `${definition.label || key}不能大于 ${definition.max}`;
    }
    return "";
  }

  payload(symbol = this.primarySymbol(), configuration = null) {
    const values = this.cloneSymbolConfiguration(configuration || this.symbolConfigurations[symbol] || this.readCurrentSymbolConfiguration());
    return {
      strategy_id: this.strategyId,
      symbol,
      timeframe: this.q("#timeframe").value,
      market_data_source: values.market_data_source,
      start_date: values.start_date,
      end_date: values.end_date,
      initial_capital: Number(values.initial_capital),
      position_size_pct: Number(values.position_size_pct),
      leverage: Number(values.leverage),
      margin_mode: "isolated",
      fee_bps: Number(values.fee_bps),
      slippage_bps: Number(values.slippage_bps),
      stop_loss_pct: Number(values.stop_loss_pct),
      take_profit_pct: Number(values.take_profit_pct),
      max_holding_bars: Number(values.max_holding_bars),
      params: { ...(values.params || {}) },
    };
  }

  validate() {
    const form = this.q("#backtest-form");
    if (!this.strategyId) {
      this.showBanner("请先选择一个策略。", "error");
      return false;
    }
    if (!this.symbolsForRun().length) {
      this.showBanner("请至少选择一个有效交易品种。", "error");
      return false;
    }
    this.captureActiveSymbolConfiguration(false);
    this.ensureSymbolConfigurations();
    if (!form.checkValidity()) {
      form.reportValidity();
      this.showBanner("请检查回测参数，所有必填项都需要有效值。", "error");
      return false;
    }
    for (const symbol of this.symbolsForRun()) {
      const configuration = this.symbolConfigurations[symbol];
      const configurationError = configuration ? this.symbolConfigurationError(configuration) : "回测参数不完整";
      if (configurationError) {
        this.switchActiveSymbol(symbol, { loadProfile: false });
        this.showBanner(`${symbol}：${configurationError}，请检查后重试。`, "error");
        return false;
      }
      if (!configuration.start_date || !configuration.end_date || configuration.start_date > configuration.end_date) {
        this.switchActiveSymbol(symbol, { loadProfile: false });
        this.showBanner(`${symbol} 的开始日期不能晚于结束日期。`, "error");
        return false;
      }
      const start = new Date(`${configuration.start_date}T00:00:00`);
      const end = new Date(`${configuration.end_date}T00:00:00`);
      if ((end - start) / 86400000 > 366) {
        this.switchActiveSymbol(symbol, { loadProfile: false });
        this.showBanner(`${symbol} 的单次回测最长为 366 天，请缩短日期区间后重试。`, "error");
        return false;
      }
    }
    return true;
  }

  async runBacktest(event) {
    event.preventDefault();
    if (this.runningBacktest || !this.validate()) return;
    const generation = this.sessionGeneration;
    const symbols = this.symbolsForRun();
    const payloads = new Map(symbols.map((symbol) => [symbol, this.payload(symbol, this.symbolConfigurations[symbol])]));
    this.closeConfigDialog(false);
    this.runningBacktest = true;
    this.activeDetail = null;
    this.stopResultReplay();
    this.showBanner("");
    this.setRunButton(true);
    this.setStageStatus("计算中", "loading");
    this.q("#active-run-meta").textContent = `${symbols.length} 个品种 · ${this.q("#timeframe").value} · 服务端正在计算`;
    this.showComputationProgress();
    try {
      const successes = [];
      const failures = [];
      let cursor = 0;
      const concurrency = Math.max(1, Math.min(2, Number(this.catalog.limits?.max_concurrent_backtests_per_user || 2), symbols.length));
      const runNext = async () => {
        while (cursor < symbols.length) {
          const symbol = symbols[cursor++];
          try {
            let detail = await this.api("", { method: "POST", body: JSON.stringify(payloads.get(symbol)) });
            const id = this.runId(detail);
            if (!detail?.result && id) detail = await this.api(`/${encodeURIComponent(id)}`);
            successes.push({ symbol, detail });
          } catch (error) {
            failures.push({ symbol, message: error.message || "运行失败" });
          }
        }
      };
      await Promise.all(Array.from({ length: concurrency }, () => runNext()));
      if (generation !== this.sessionGeneration) return;
      if (!successes.length) throw new Error(failures.map((item) => `${item.symbol}: ${item.message}`).join("；"));
      this.stopComputationProgress();
      const symbolOrder = new Map(symbols.map((symbol, index) => [symbol, index]));
      successes.sort((left, right) => (symbolOrder.get(left.symbol) ?? 0) - (symbolOrder.get(right.symbol) ?? 0));
      failures.sort((left, right) => (symbolOrder.get(left.symbol) ?? 0) - (symbolOrder.get(right.symbol) ?? 0));
      this.renderBatchResults(successes, failures);
      await this.loadHistory(false, generation);
      if (generation !== this.sessionGeneration) return;
      this.setStageStatus(failures.length ? "部分完成" : "已完成", "success");
      const message = symbols.length === 1
        ? "回测完成。结果已收起，点击交易品种可展开完整视图。"
        : `多品种回测完成：成功 ${successes.length}，失败 ${failures.length}。结果已按品种列出，点击展开查看完整回测视图。`;
      this.showBanner(message, "success");
    } catch (error) {
      if (generation !== this.sessionGeneration) return;
      this.stopComputationProgress();
      this.stopResultReplay();
      this.q("#running-title").textContent = "回测未完成";
      this.q("#running-description").textContent = error.message;
      this.q("#running-result").classList.remove("hidden");
      this.setStageStatus("运行失败", "error");
      this.showBanner(`回测失败：${error.message}`, "error");
    } finally {
      if (generation === this.sessionGeneration) {
        this.runningBacktest = false;
        this.setRunButton(false);
      }
    }
  }

  hideBatchResults(clear = false) {
    const overview = this.q("#batch-result-overview");
    if (overview) overview.classList.add("hidden");
    if (!clear) return;
    this.batchExpandToken += 1;
    this.expandedBatchIndex = -1;
    this.batchResults = [];
    this.batchFailures = [];
    const list = this.q("#batch-result-list");
    if (list) list.replaceChildren();
  }

  renderBatchResults(successes = [], failures = []) {
    this.stopResultReplay();
    this.batchExpandToken += 1;
    this.batchResults = [...successes];
    this.batchFailures = [...failures];
    this.expandedBatchIndex = -1;
    this.activeDetail = null;
    this.q("#empty-result").classList.add("hidden");
    this.q("#running-result").classList.add("hidden");
    this.q("#result-content").classList.add("hidden");
    this.q("#trade-cycle-rail").classList.add("hidden");
    this.q(".stage-layout").classList.remove("has-result");
    this.resetPriceChartState();
    this.q("#batch-result-overview").classList.remove("hidden");
    this.q("#batch-result-count").textContent = `${successes.length + failures.length} 个品种`;
    this.q("#batch-result-summary").textContent = `成功 ${successes.length} · 失败 ${failures.length} · 结果默认收起，点击交易品种展开完整视图。`;
    this.q("#active-run-meta").textContent = `${successes.length + failures.length} 个结果 · 点击交易品种展开`;
    this.renderBatchResultCards();
  }

  renderBatchResultCards() {
    const cards = this.batchResults.map((item, index) => {
      const { run, result } = this.unpackDetail(item.detail);
      const metrics = result.metrics || {};
      const totalReturn = this.metric(metrics, ["total_return_pct", "return_pct", "total_return"]);
      const drawdown = this.metric(metrics, ["max_drawdown_pct", "max_drawdown"]);
      const winRate = this.metric(metrics, ["win_rate_pct", "win_rate"]);
      const trades = this.metric(metrics, ["trade_count", "total_trades", "trades"]);
      const expanded = index === this.expandedBatchIndex;
      const card = this.node("article", `batch-result-card${expanded ? " expanded" : ""}`);
      const head = this.node("div", "batch-result-card-head");
      const identity = this.node("div", "batch-result-identity");
      identity.append(
        this.node("span", "batch-result-order", String(index + 1).padStart(2, "0")),
        this.node("strong", "", run.symbol || item.symbol || "--"),
        this.node("small", "", `${run.strategy_name || run.strategy_id || "策略回测"} · ${run.timeframe || "--"}`),
      );
      head.append(identity, this.node("strong", `batch-result-return ${this.toneClass(totalReturn)}`, this.percent(totalReturn)));
      const facts = this.node("div", "batch-result-metrics");
      for (const [label, value, tone] of [
        ["最大回撤", this.percent(drawdown, false), -Math.abs(Number(drawdown) || 0)],
        ["胜率", this.percent(winRate, false), winRate],
        ["交易次数", this.integer(trades), 0],
        ["数据区间", `${this.dateOnly(run.start_date || run.start_at) || "--"} — ${this.dateOnly(run.end_date || run.end_at) || "--"}`, 0],
      ]) {
        const fact = this.node("div", "batch-result-metric");
        fact.append(this.node("span", "", label), this.node("strong", this.toneClass(tone), value));
        facts.append(fact);
      }
      const button = this.node("button", "batch-result-toggle", expanded ? "收起回测结果" : "展开回测结果");
      button.type = "button";
      button.setAttribute("aria-expanded", String(expanded));
      button.addEventListener("click", () => { void this.toggleBatchResult(index); });
      card.append(head, facts, button);
      return card;
    });
    const failedCards = this.batchFailures.map((item) => {
      const card = this.node("article", "batch-result-card failed");
      const head = this.node("div", "batch-result-card-head");
      const identity = this.node("div", "batch-result-identity");
      identity.append(this.node("span", "batch-result-order", "!"), this.node("strong", "", item.symbol || "--"), this.node("small", "", "该品种未产生可展示结果"));
      head.append(identity, this.node("strong", "batch-result-return negative", "失败"));
      card.append(head, this.node("p", "batch-result-error", item.message || "运行失败"));
      return card;
    });
    this.q("#batch-result-list").replaceChildren(...cards, ...failedCards);
  }

  async toggleBatchResult(index) {
    if (index === this.expandedBatchIndex) {
      this.collapseBatchResult();
      return;
    }
    const item = this.batchResults[index];
    if (!item?.detail) return;
    const token = ++this.batchExpandToken;
    const generation = this.sessionGeneration;
    this.stopResultReplay();
    this.expandedBatchIndex = index;
    this.activeDetail = item.detail;
    this.renderBatchResultCards();
    await this.replayResult(item.detail, generation, { preserveBatch: true, expandToken: token });
    if (generation !== this.sessionGeneration || token !== this.batchExpandToken) return;
    this.setStageStatus("已展开", "success");
    this.renderBatchResultCards();
  }

  collapseBatchResult() {
    this.batchExpandToken += 1;
    this.stopResultReplay();
    this.expandedBatchIndex = -1;
    this.activeDetail = null;
    this.q("#result-content").classList.add("hidden");
    this.q("#trade-cycle-rail").classList.add("hidden");
    this.q(".stage-layout").classList.remove("has-result");
    this.resetPriceChartState();
    this.q("#active-run-meta").textContent = `${this.batchResults.length + this.batchFailures.length} 个结果 · 点击交易品种展开`;
    this.setStageStatus(this.batchFailures.length ? "部分完成" : "已完成", "success");
    this.renderBatchResultCards();
  }

  async loadHistory(showFeedback = false, generation = this.sessionGeneration) {
    const buttons = [this.q("#open-history"), this.q("#reload-history")].filter(Boolean);
    buttons.forEach((button) => { button.disabled = true; });
    this.q("#history-list").replaceChildren(this.node("div", "history-empty", "正在读取历史回测数据…"));
    try {
      const data = await this.api("?limit=100");
      if (generation !== this.sessionGeneration) return;
      this.history = Array.isArray(data?.items) ? data.items : [];
      this.historyLoaded = true;
      this.renderHistory();
      if (showFeedback) this.showBanner("历史回测数据已刷新。", "success");
    } catch (error) {
      if (generation !== this.sessionGeneration) return;
      this.q("#history-list").replaceChildren(this.node("div", "history-empty error-text", `历史回测数据读取失败：${error.message}`));
      if (showFeedback) this.showBanner(`历史记录刷新失败：${error.message}`, "error");
    } finally {
      if (generation === this.sessionGeneration) buttons.forEach((button) => { button.disabled = false; });
    }
  }

  openHistory() {
    const dialog = this.q("#history-dialog");
    dialog.classList.remove("hidden");
    this.q("#close-history").focus();
    if (!this.historyLoaded) void this.loadHistory(false);
  }

  closeHistory() {
    this.q("#history-dialog")?.classList.add("hidden");
  }

  renderHistory() {
    const list = this.q("#history-list");
    this.q("#history-count").textContent = `${this.history.length} 条`;
    if (!this.history.length) {
      list.replaceChildren(this.node("div", "history-empty", this.historyLoaded ? "暂无回测记录，首次运行后会保存在这里。" : "打开窗口后读取历史回测数据。"));
      return;
    }
    list.replaceChildren(...this.history.map((item) => {
      const run = item.run || item;
      const metrics = this.unpackDetail(item).result.metrics;
      const id = this.runId(item);
      const button = this.node("button", "history-item");
      button.type = "button";
      if (id) button.dataset.runId = id;
      const top = this.node("div", "history-item-top");
      top.append(this.node("strong", "", run.strategy_name || run.strategy_id || "策略回测"), this.node("span", this.toneClass(this.metric(metrics, ["total_return_pct", "return_pct", "total_return"])), this.percent(this.metric(metrics, ["total_return_pct", "return_pct", "total_return"]))));
      const middle = this.node("div", "history-item-middle", `${run.symbol || "--"} · ${run.timeframe || "--"}`);
      const bottom = this.node("div", "history-item-bottom");
      bottom.append(this.node("span", "", this.shortDate(run.completed_at || run.created_at || run.started_at)), this.node("span", "", `回撤 ${this.percent(this.metric(metrics, ["max_drawdown_pct", "max_drawdown"]), false)}`));
      button.append(top, middle, bottom);
      if (id) button.addEventListener("click", () => this.loadRun(id, button));
      else button.disabled = true;
      return button;
    }));
  }

  async loadRun(id, button) {
    const generation = this.sessionGeneration;
    this.qa(".history-item").forEach((item) => item.classList.toggle("loading", item === button));
    this.setStageStatus("读取记录", "loading");
    try {
      const detail = await this.api(`/${encodeURIComponent(id)}`);
      if (generation !== this.sessionGeneration) return;
      this.activeDetail = detail;
      this.renderResult(detail);
      this.closeHistory();
      this.setStageStatus("历史结果", "success");
      this.showBanner("");
    } catch (error) {
      if (generation !== this.sessionGeneration) return;
      this.setStageStatus("读取失败", "error");
      this.showBanner(`回测详情加载失败：${error.message}`, "error");
    } finally {
      if (generation === this.sessionGeneration) {
        this.qa(".history-item").forEach((item) => item.classList.remove("loading"));
      }
    }
  }

  renderResult(detail = {}, options = {}) {
    if (!options.preserveBatch) this.hideBatchResults(true);
    const unpacked = this.unpackDetail(detail);
    const run = unpacked.run;
    const result = unpacked.result;
    const account = result.account || {};
    const metrics = result.metrics || {};
    this.q("#empty-result").classList.add("hidden");
    this.q("#running-result").classList.add("hidden");
    this.q("#result-content").classList.remove("hidden");
    this.q("#trade-cycle-rail").classList.remove("hidden");
    this.q(".stage-layout").classList.add("has-result");
    this.q("#result-kicker").textContent = options.deferCharts ? "BACKTEST REPLAY" : "BACKTEST COMPLETE";
    const strategy = run.strategy_name || this.catalog.strategies.find((item) => String(item.id) === String(run.strategy_id))?.name || run.strategy_id || "策略回测";
    this.q("#result-name").textContent = `${strategy} · ${run.symbol || "--"}`;
    this.q("#result-period").textContent = `${this.dateOnly(run.start_date || run.start_at) || "--"} 至 ${this.dateOnly(run.end_date || run.end_at) || "--"} · ${run.timeframe || "--"}`;
    const initialCapital = this.numericValue(account.initial_capital ?? run.initial_capital);
    const finalEquity = this.numericValue(account.final_equity ?? account.final_balance ?? account.ending_capital ?? account.equity);
    const reportedNetProfit = this.numericValue(account.net_profit ?? metrics.net_profit ?? run.net_profit);
    const netProfit = reportedNetProfit ?? (initialCapital != null && finalEquity != null ? finalEquity - initialCapital : null);
    this.q("#result-initial-capital").textContent = this.money(initialCapital);
    const profitNode = this.q("#result-net-profit");
    profitNode.textContent = this.signedMoney(netProfit);
    profitNode.className = this.toneClass(netProfit);
    const totalReturn = this.metric(metrics, ["total_return_pct", "return_pct", "total_return"]);
    const returnNode = this.q("#result-return");
    returnNode.textContent = this.percent(totalReturn);
    returnNode.className = `return-badge ${this.toneClass(totalReturn)}`;
    this.q("#active-run-meta").textContent = `完成于 ${this.shortDate(run.completed_at || run.updated_at || run.created_at, true)}`;
    this.renderMetrics(metrics, account);
    this.renderQuality(result.data_quality || {});
    this.renderTrades(Array.isArray(result.trades) ? result.trades : [], result.data_quality || {});
    if (!options.deferCharts) window.requestAnimationFrame(() => this.drawCharts(result));
  }

  showComputationProgress() {
    this.stopComputationProgress();
    this.hideBatchResults(true);
    this.q("#empty-result").classList.add("hidden");
    this.q("#result-content").classList.add("hidden");
    this.q("#trade-cycle-rail").classList.add("hidden");
    this.q(".stage-layout").classList.remove("has-result");
    this.q("#running-result").classList.remove("hidden");
    this.q("#running-title").textContent = "正在准备历史行情";
    this.q("#running-description").textContent = "正在提交参数并检查可用数据区间。";
    this.computationProgressValue = 4;
    this.updateRunProgress(4, 0, "等待服务端响应");
    const startedAt = performance.now();
    this.computationProgressTimer = window.setInterval(() => {
      const elapsed = performance.now() - startedAt;
      const stage = elapsed < 1400 ? 0 : elapsed < 3300 ? 1 : 2;
      const ceiling = stage === 0 ? 24 : stage === 1 ? 47 : 78;
      const increment = stage === 2 ? .7 : 1.3;
      this.computationProgressValue = Math.min(ceiling, this.computationProgressValue + increment);
      const titles = ["正在准备历史行情", "正在预热策略指标", "正在执行策略计算"];
      const descriptions = [
        "正在读取 K 线、合约规格与策略参数。",
        "正在补齐指标预热窗口并检查数据连续性。",
        "服务端正在逐根执行策略、撮合订单并计算账户权益。",
      ];
      this.q("#running-title").textContent = titles[stage];
      this.q("#running-description").textContent = descriptions[stage];
      this.updateRunProgress(this.computationProgressValue, stage, `已计算 ${Math.max(1, Math.round(elapsed / 1000))} 秒`);
    }, 180);
  }

  stopComputationProgress() {
    if (this.computationProgressTimer) window.clearInterval(this.computationProgressTimer);
    this.computationProgressTimer = 0;
  }

  updateRunProgress(value, activeStep, detail) {
    const safeValue = Math.max(0, Math.min(100, Number(value) || 0));
    const progress = this.q(".running-progress");
    if (!progress) return;
    progress.setAttribute("aria-valuenow", String(Math.round(safeValue)));
    this.q("#running-progress-bar").style.width = `${safeValue}%`;
    this.q("#running-progress-value").textContent = `${Math.round(safeValue)}%`;
    this.q("#running-progress-count").textContent = detail;
    this.qa("[data-running-step]").forEach((step) => {
      const index = Number(step.dataset.runningStep);
      step.classList.toggle("active", index === activeStep);
      step.classList.toggle("complete", index < activeStep);
    });
  }

  async replayResult(detail, generation = this.sessionGeneration, options = {}) {
    const { result, run } = this.unpackDetail(detail);
    const candles = Array.isArray(result.price_candles) ? result.price_candles : (result.data_quality?.price_candles || []);
    const trades = Array.isArray(result.trades) ? result.trades : [];
    const curve = this.normalizeCurve(result.equity_curve || result.curve || []);
    const replayIsCurrent = () => generation === this.sessionGeneration
      && (options.expandToken == null || options.expandToken === this.batchExpandToken);
    this.renderResult(detail, { deferCharts: true, preserveBatch: Boolean(options.preserveBatch) });
    this.q("#result-kicker").textContent = "BACKTEST REPLAY";
    this.q("#trade-replay-status").classList.remove("hidden");
    this.setStageStatus("逐根回放", "loading");
    this.resetPriceChartState();
    if (!candles.length) {
      if (!replayIsCurrent()) return;
      this.drawCharts(result);
      this.finishResultReplay(result, run);
      return;
    }
    const duration = Math.max(5200, Math.min(10000, 3600 + candles.length * 4));
    const startedAt = performance.now();
    await new Promise((resolve) => {
      this.resultReplayState = { result, candles, trades, curve, visibleCandles: 1, lastPaintAt: 0, resolve };
      const tick = (now) => {
        const state = this.resultReplayState;
        if (!state || !replayIsCurrent()) {
          resolve();
          return;
        }
        const ratio = Math.min(1, (now - startedAt) / duration);
        const eased = 1 - Math.pow(1 - ratio, 2.2);
        state.visibleCandles = Math.max(1, Math.min(candles.length, Math.ceil(candles.length * eased)));
        if (now - state.lastPaintAt >= 45 || ratio >= 1) {
          state.lastPaintAt = now;
          this.drawReplayFrame(state.visibleCandles);
        }
        if (ratio >= 1) {
          this.resultReplayFrame = 0;
          this.resultReplayState = null;
          resolve();
          return;
        }
        this.resultReplayFrame = window.requestAnimationFrame(tick);
      };
      this.resultReplayFrame = window.requestAnimationFrame(tick);
    });
    if (!replayIsCurrent()) return;
    this.finishResultReplay(result, run);
  }

  drawReplayFrame(visibleCount) {
    const state = this.resultReplayState;
    if (!state) return;
    const visibleCandles = state.candles.slice(0, visibleCount);
    const lastCandle = visibleCandles[visibleCandles.length - 1] || {};
    const lastTs = this.chartTimestamp(lastCandle.ts ?? lastCandle.timestamp ?? lastCandle.open_time);
    const visibleTrades = state.trades.filter((trade) => {
      const entryTs = this.chartTimestamp(trade?.entry_ts ?? trade?.entry_at ?? trade?.entry_time);
      return !Number.isFinite(entryTs) || entryTs <= lastTs;
    }).map((trade) => {
      const exitTs = this.chartTimestamp(trade?.exit_ts ?? trade?.exit_at ?? trade?.exit_time);
      if (!Number.isFinite(exitTs) || exitTs <= lastTs) return trade;
      return {
        ...trade,
        exit_ts: null,
        exit_at: null,
        exit_time: null,
        closed_at: null,
        exit_price: null,
        pnl: null,
        net_pnl: null,
        profit: null,
        return_pct: null,
        pnl_pct: null,
        account_return_pct: null,
        exit_reason: null,
        executions: Array.isArray(trade.executions)
          ? trade.executions.filter((execution) => this.chartTimestamp(execution?.timestamp) <= lastTs)
          : trade.executions,
      };
    });
    const curveCount = Math.max(2, Math.min(state.curve.length, Math.ceil(state.curve.length * visibleCount / state.candles.length)));
    this.drawLineChart(this.q("#equity-chart"), state.curve.slice(0, curveCount), "equity");
    this.drawLineChart(this.q("#drawdown-chart"), state.curve.slice(0, curveCount), "drawdown");
    this.drawPriceChart(this.q("#price-chart"), visibleCandles, visibleTrades);
    this.renderTrades(visibleTrades, { ...(state.result.data_quality || {}), trades_total: visibleTrades.length, trades_truncated: false });
    this.q("#trade-replay-status").textContent = visibleTrades.length
      ? `已回放 ${visibleTrades.length} 个交易周期`
      : "等待 K 线回放到首个成交时刻";
    this.setStageStatus(`逐根回放 ${this.integer(visibleCount)} / ${this.integer(state.candles.length)}`, "loading");
    this.q("#active-run-meta").textContent = `正在回放 ${this.integer(visibleCount)} / ${this.integer(state.candles.length)} 根 K 线`;
    this.updateRunProgress(80 + visibleCount / state.candles.length * 18, 3, `K 线 ${this.integer(visibleCount)} / ${this.integer(state.candles.length)} · 成交周期 ${this.integer(visibleTrades.length)}`);
  }

  finishResultReplay(result, run = {}) {
    this.stopResultReplay(false);
    this.q("#result-kicker").textContent = "BACKTEST COMPLETE";
    this.q("#trade-replay-status").classList.add("hidden");
    this.renderTrades(Array.isArray(result.trades) ? result.trades : [], result.data_quality || {});
    this.q("#active-run-meta").textContent = `完成于 ${this.shortDate(run.completed_at || run.updated_at || run.created_at, true)}`;
    this.updateRunProgress(100, 4, "回放与结算完成");
    window.requestAnimationFrame(() => this.drawCharts(result));
  }

  stopResultReplay(resolvePending = true) {
    if (this.resultReplayFrame) window.cancelAnimationFrame(this.resultReplayFrame);
    this.resultReplayFrame = 0;
    const pending = this.resultReplayState;
    this.resultReplayState = null;
    if (resolvePending && pending?.resolve) pending.resolve();
  }

  renderMetrics(metrics, account) {
    const definitions = [
      ["累计收益", this.percent(this.metric(metrics, ["total_return_pct", "return_pct", "total_return"])), "扣除交易成本", this.metric(metrics, ["total_return_pct", "return_pct", "total_return"])],
      ["年化收益", this.percent(this.metric(metrics, ["annualized_return_pct", "annual_return_pct", "cagr"])), `期末 ${this.money(account.final_equity ?? account.ending_capital ?? account.equity)}`, this.metric(metrics, ["annualized_return_pct", "annual_return_pct", "cagr"])],
      ["最大回撤", this.percent(this.metric(metrics, ["max_drawdown_pct", "max_drawdown"]), false), "峰值至谷底", -Math.abs(Number(this.metric(metrics, ["max_drawdown_pct", "max_drawdown"])) || 0)],
      ["夏普比率", this.number(this.metric(metrics, ["sharpe_ratio", "sharpe"]), 2), "风险调整收益", this.metric(metrics, ["sharpe_ratio", "sharpe"])],
      ["胜率", this.percent(this.metric(metrics, ["win_rate_pct", "win_rate"]), false), `盈亏比 ${this.number(this.metric(metrics, ["profit_factor", "payoff_ratio"]), 2)}`, this.metric(metrics, ["win_rate_pct", "win_rate"])],
      ["交易次数", this.integer(this.metric(metrics, ["trade_count", "total_trades", "trades"])), `手续费 ${this.money(account.total_fees ?? metrics.total_fees)}`, 0],
    ];
    const cards = definitions.map(([label, value, note, tone]) => {
      const card = this.node("article", "metric-card");
      card.append(this.node("span", "", label), this.node("strong", this.toneClass(tone), value), this.node("small", "", note));
      return card;
    });
    this.q("#metric-grid").replaceChildren(...cards);
  }

  renderQuality(quality) {
    const object = typeof quality === "object" && quality ? quality : { message: String(quality || "") };
    const rawSource = String(object.source || object.market_data_source || "").toLowerCase();
    const sourceLabel = rawSource.includes("tiger") ? "Tiger" : rawSource.includes("binance") ? "Binance" : (rawSource || "--");
    let coverage = Number(object.coverage_pct ?? object.coverage ?? object.completeness_pct);
    if (Number.isFinite(coverage) && coverage <= 1) coverage *= 100;
    const missing = Number(object.missing_bars ?? object.missing ?? object.gaps ?? 0);
    const actual = object.actual_bars ?? object.bars_used ?? object.bars_loaded ?? object.bar_count ?? object.rows ?? "--";
    const expected = object.expected_bars ?? object.expected ?? (Number.isFinite(Number(actual)) ? Number(actual) + missing : "--");
    const grade = object.grade || (Number.isFinite(coverage) ? (coverage >= 99 ? "优秀" : coverage >= 95 ? "可用" : "需注意") : (missing ? "需注意" : "已检查"));
    const gradeNode = this.q("#quality-grade");
    gradeNode.textContent = grade;
    gradeNode.className = `quality-grade ${grade === "需注意" ? "warning" : "good"}`;
    const facts = [
      ["实际数据源", sourceLabel],
      ["行情覆盖率", Number.isFinite(coverage) ? `${coverage.toFixed(2)}%` : "已完成检查"],
      ["实际 K 线", this.integer(actual)],
      ["预期 K 线", this.integer(expected)],
      ["缺口数量", this.integer(missing)],
    ].map(([label, value]) => {
      const fact = this.node("div", "quality-fact");
      fact.append(this.node("span", "", label), this.node("strong", "", value));
      return fact;
    });
    this.q("#quality-facts").replaceChildren(...facts);
    const warnings = Array.isArray(object.warnings) ? object.warnings.filter(Boolean) : [];
    const assumptions = Array.isArray(object.assumptions) ? object.assumptions.filter(Boolean) : [];
    const messages = [];
    if (object.message || object.note) messages.push(object.message || object.note);
    else if (warnings.length) messages.push(warnings.join("；"));
    else if (missing) messages.push("行情存在缺口，结果可能受样本连续性影响。");
    else messages.push("行情覆盖满足本次研究要求。");
    if (object.trades_truncated) {
      messages.push(`成交明细返回 ${this.integer(object.trades_returned)} / ${this.integer(object.trades_total)} 笔，汇总指标按全部成交计算。`);
    }
    if (assumptions.length) messages.push(`模型假设：${assumptions.join("；")}`);
    this.q("#quality-message").textContent = messages.join("\n");
  }

  renderTrades(trades, quality = {}) {
    const total = Number(quality?.trades_total);
    const totalTrades = Number.isFinite(total) ? total : trades.length;
    this.q("#trade-count").textContent = `${this.integer(totalTrades)} 组`;
    const wins = trades.filter((trade) => Number(trade.pnl ?? trade.net_pnl ?? 0) > 0).length;
    const truncated = Boolean(quality?.trades_truncated) || totalTrades > trades.length;
    this.q("#trade-summary").textContent = trades.length
      ? `${wins} 胜 / ${trades.length - wins} 负${truncated ? ` · 返回 ${trades.length} / ${totalTrades} 组` : ""} · 按开仓到平仓分组，显示最近 100 个周期`
      : "暂无成交样本";
    const container = this.q("#trade-table");
    if (!trades.length) {
      const empty = this.node("div", "table-empty");
      empty.append(this.node("strong", "", "本次没有触发交易"), this.node("span", "", "可扩大回测区间，或调整策略入场参数后再次验证。"));
      container.replaceChildren(empty);
      return;
    }
    const list = this.node("div", "trade-cycle-list");
    trades.slice(-100).reverse().forEach((trade, visibleIndex) => {
      const sideValue = String(trade.side ?? trade.direction ?? "").toLowerCase();
      const isLong = ["long", "buy", "1", "多"].includes(sideValue) || Number(trade.side) > 0;
      const isBasket = Boolean(trade.is_mixed_basket)
        || String(trade.position_structure ?? "").toLowerCase() === "mixed_basket"
        || ["basket", "mixed", "双向", "篮子"].includes(sideValue);
      const pnl = Number(trade.net_pnl ?? trade.pnl ?? trade.profit ?? 0);
      const initialMargin = trade.initial_margin ?? trade.peak_initial_margin ?? trade.margin;
      const availableBalance = trade.remaining_available_balance ?? trade.minimum_available_balance ?? trade.available_balance;
      const entryTime = trade.entry_time ?? trade.entry_at ?? trade.opened_at ?? trade.entry_ts;
      const exitTime = trade.exit_time ?? trade.exit_at ?? trade.closed_at ?? trade.exit_ts;
      const sequence = Number(trade.cycle_sequence) || totalTrades - visibleIndex;

      const group = this.node("article", "trade-cycle-group");
      const head = this.node("header", "trade-cycle-head");
      const identity = this.node("div", "trade-cycle-identity");
      const titleLine = this.node("div", "trade-cycle-title");
      titleLine.append(
        this.node("strong", "", `交易周期 #${this.integer(sequence)}`),
        this.node("span", exitTime ? "trade-status closed" : "trade-status open", exitTime ? "已平仓" : "持仓中"),
        this.node("span", isBasket ? "trade-direction basket" : isLong ? "trade-direction long" : "trade-direction short", isBasket ? "双向篮子" : isLong ? "做多" : "做空"),
      );
      identity.append(titleLine, this.node("span", "trade-cycle-caption", `${this.cycleModeLabel(trade.cycle_mode)} · ${this.exitReason(trade.exit_reason ?? trade.reason)}`));
      const outcome = this.node("div", `trade-cycle-outcome ${this.toneClass(pnl)}`);
      outcome.append(this.node("small", "", "本周期净盈亏"), this.node("strong", "", this.signedMoney(pnl)), this.node("span", "", `账户 ${this.percent(trade.account_return_pct ?? trade.return_pct ?? trade.pnl_pct, true)}`));
      head.append(identity, outcome);

      const timing = this.node("div", "trade-cycle-timing");
      const entryNode = this.node("div", "trade-time-node entry");
      entryNode.append(this.node("span", "", "开仓时间"), this.node("strong", "", this.shortDate(entryTime, true)), this.node("small", "", isBasket ? "建立多头与空头腿" : isLong ? "买入开多" : "卖出开空"));
      const arrow = this.node("div", "trade-time-arrow", "→");
      const exitNode = this.node("div", "trade-time-node exit");
      exitNode.append(this.node("span", "", "平仓时间"), this.node("strong", "", this.shortDate(exitTime, true)), this.node("small", "", isBasket ? "结清全部方向腿" : isLong ? "卖出平多" : "买入平空"));
      timing.append(entryNode, arrow, exitNode);

      const executions = this.tradeExecutions(trade, { isBasket, isLong, entryTime, exitTime });
      const executionSection = this.node("section", "trade-execution-section");
      const executionHead = this.node("div", "trade-subhead");
      executionHead.append(this.node("strong", "", "买卖执行明细"), this.node("span", "", `${executions.length} 个动作 · 按时间排序`));
      const executionGrid = this.node("div", "trade-execution-grid");
      executions.forEach((execution, executionIndex) => {
        const orderSide = String(execution.order_side || "").toLowerCase();
        const positionSide = String(execution.position_side || "").toLowerCase();
        const action = String(execution.action || "").toLowerCase();
        const phase = execution.phase === "exit" ? "exit" : "entry";
        const actionLabel = this.executionLabel({ orderSide, positionSide, action, phase });
        const event = this.node("div", `trade-execution ${orderSide === "buy" ? "buy" : "sell"}`);
        const eventTop = this.node("div", "trade-execution-top");
        eventTop.append(this.node("span", "trade-execution-index", String(executionIndex + 1).padStart(2, "0")), this.node("strong", "", actionLabel), this.node("span", "trade-execution-phase", phase === "entry" ? "开仓阶段" : "平仓阶段"));
        const eventBody = this.node("div", "trade-execution-body");
        eventBody.append(
          this.tradeExecutionFact(orderSide === "buy" ? "买入时间" : "卖出时间", this.shortDate(execution.timestamp, true)),
          this.tradeExecutionFact("成交价格", this.price(execution.price)),
          this.tradeExecutionFact("成交数量", this.quantity(execution.quantity)),
          this.tradeExecutionFact("方向腿", positionSide === "short" ? "空头" : "多头"),
        );
        event.append(eventTop, eventBody);
        executionGrid.append(event);
      });
      executionSection.append(executionHead, executionGrid);

      const metrics = this.node("div", "trade-cycle-metrics");
      [
        ["总仓位", this.quantity(trade.quantity ?? trade.qty ?? trade.position_size), ""],
        ["开仓保证金", this.money(initialMargin), ""],
        ["杠杆倍率", `${this.integer(trade.leverage ?? 1)}x`, ""],
        ["最低剩余可用", this.money(availableBalance), this.toneClass(availableBalance)],
        ["平仓后可用", this.money(trade.available_balance_after_close ?? trade.available_balance), this.toneClass(trade.available_balance_after_close ?? trade.available_balance)],
        ["手续费", this.money(trade.fees ?? trade.fee), "muted"],
        ["账户收益率", this.percent(trade.account_return_pct ?? trade.return_pct ?? trade.pnl_pct, true), this.toneClass(trade.account_return_pct ?? trade.return_pct ?? trade.pnl_pct)],
        ["保证金 ROE", this.percent(trade.margin_return_pct ?? trade.return_pct ?? trade.pnl_pct, true), this.toneClass(trade.margin_return_pct ?? trade.return_pct ?? trade.pnl_pct)],
        ["持有周期", `${this.integer(trade.holding_bars ?? trade.bars_held ?? trade.duration_bars)} 根`, ""],
        ["退出原因", this.exitReason(trade.exit_reason ?? trade.reason), ""],
      ].forEach(([label, value, tone]) => {
        const metric = this.node("div", "trade-cycle-metric");
        metric.append(this.node("span", "", label), this.node("strong", tone, value));
        metrics.append(metric);
      });
      group.append(head, timing, executionSection, metrics);
      list.append(group);
    });
    container.replaceChildren(list);
  }

  tradeExecutions(trade, context) {
    const supplied = Array.isArray(trade.executions) ? trade.executions.filter((item) => item && item.timestamp) : [];
    if (supplied.length) {
      return supplied.slice().sort((left, right) => Number(left.timestamp) - Number(right.timestamp) || Number(left.sequence || 0) - Number(right.sequence || 0));
    }
    const fallback = [];
    const add = (phase, positionSide, timestamp, price, quantity, action) => {
      if (!timestamp || !Number.isFinite(Number(price))) return;
      fallback.push({
        phase,
        position_side: positionSide,
        order_side: phase === "entry" ? (positionSide === "long" ? "buy" : "sell") : (positionSide === "long" ? "sell" : "buy"),
        timestamp,
        price,
        quantity,
        action,
      });
    };
    if (context.isBasket) {
      if (Number(trade.long_quantity) > 0) {
        add("entry", "long", context.entryTime, trade.long_entry_price, trade.long_quantity, "open");
        add("exit", "long", context.exitTime, trade.long_exit_price, trade.long_quantity, "close_all");
      }
      if (Number(trade.short_quantity) > 0) {
        add("entry", "short", context.entryTime, trade.short_entry_price, trade.short_quantity, "open");
        add("exit", "short", context.exitTime, trade.short_exit_price, trade.short_quantity, "close_all");
      }
    } else {
      const positionSide = context.isLong ? "long" : "short";
      const quantity = trade.quantity ?? trade.qty ?? trade.position_size;
      add("entry", positionSide, context.entryTime, trade.entry_price ?? trade.open_price, quantity, "open");
      add("exit", positionSide, context.exitTime, trade.exit_price ?? trade.close_price, quantity, "close_all");
    }
    return fallback.sort((left, right) => Number(left.timestamp) - Number(right.timestamp) || (left.phase === "entry" ? -1 : 1));
  }

  executionLabel({ orderSide, positionSide, action, phase }) {
    if (phase === "entry") {
      if (action === "add") return positionSide === "short" ? "卖出加空" : "买入加多";
      return positionSide === "short" ? "卖出开空" : "买入开多";
    }
    return orderSide === "buy" ? "买入平空" : "卖出平多";
  }

  tradeExecutionFact(label, value) {
    const fact = this.node("div", "trade-execution-fact");
    fact.append(this.node("span", "", label), this.node("b", "", value));
    return fact;
  }

  cycleModeLabel(value) {
    const labels = { auto: "自动模式", grid: "网格模式", recovery: "恢复模式", manual: "手动模式" };
    const key = String(value || "auto").toLowerCase();
    return labels[key] || value || "策略模式";
  }

  drawCharts(result) {
    const curve = this.normalizeCurve(result.equity_curve || result.curve || []);
    this.drawLineChart(this.q("#equity-chart"), curve, "equity");
    this.drawLineChart(this.q("#drawdown-chart"), curve, "drawdown");
    this.drawPriceChart(
      this.q("#price-chart"),
      result.price_candles || result.data_quality?.price_candles || [],
      Array.isArray(result.trades) ? result.trades : [],
    );
  }

  resetPriceChartState() {
    if (this.priceChartFrame) window.cancelAnimationFrame(this.priceChartFrame);
    this.priceChartFrame = 0;
    this.priceChartState = {
      dataKey: "",
      candles: [],
      trades: [],
      viewStart: 0,
      viewEnd: 0,
      hover: null,
      layout: null,
      dragging: false,
      pointerId: null,
      dragStartX: 0,
      dragViewStart: 0,
    };
    const tooltip = this.q("#price-chart-tooltip");
    if (tooltip) tooltip.classList.add("hidden");
    const range = this.q("#price-chart-range");
    if (range) range.textContent = "全部数据";
  }

  chartTimestamp(value) {
    if (value == null) return NaN;
    if (typeof value === "string" && !/^\d+(\.\d+)?$/.test(value)) return Date.parse(value) / 1000;
    const numeric = Number(value);
    return numeric > 100000000000 ? numeric / 1000 : numeric;
  }

  bindPriceChartEvents() {
    const canvas = this.q("#price-chart");
    if (!canvas || canvas.dataset.interactive === "1") return;
    canvas.dataset.interactive = "1";
    canvas.addEventListener("wheel", (event) => this.handlePriceChartWheel(event), { passive: false });
    canvas.addEventListener("pointerdown", (event) => this.handlePriceChartPointerDown(event));
    canvas.addEventListener("pointermove", (event) => this.handlePriceChartPointerMove(event));
    canvas.addEventListener("pointerup", (event) => this.handlePriceChartPointerUp(event));
    canvas.addEventListener("pointercancel", (event) => this.handlePriceChartPointerUp(event));
    canvas.addEventListener("pointerleave", () => {
      if (!this.priceChartState.dragging) this.hidePriceChartTooltip();
    });
    canvas.addEventListener("dblclick", () => {
      const total = this.priceChartState.candles.length;
      if (!total) return;
      this.priceChartState.viewStart = 0;
      this.priceChartState.viewEnd = total;
      this.priceChartState.hover = null;
      this.hidePriceChartTooltip();
      this.schedulePriceChartDraw();
    });
    canvas.addEventListener("keydown", (event) => {
      if (["ArrowLeft", "ArrowRight", "+", "=", "-", "0", "Home"].includes(event.key)) event.preventDefault();
      if (event.key === "ArrowLeft") this.panPriceChart(-Math.max(1, Math.round(this.visiblePriceCandleCount() * .12)));
      if (event.key === "ArrowRight") this.panPriceChart(Math.max(1, Math.round(this.visiblePriceCandleCount() * .12)));
      if (event.key === "+" || event.key === "=") this.zoomPriceChart(.8, .5);
      if (event.key === "-") this.zoomPriceChart(1.25, .5);
      if (event.key === "0" || event.key === "Home") {
        this.priceChartState.viewStart = 0;
        this.priceChartState.viewEnd = this.priceChartState.candles.length;
        this.schedulePriceChartDraw();
      }
    });
  }

  visiblePriceCandleCount() {
    return Math.max(0, this.priceChartState.viewEnd - this.priceChartState.viewStart);
  }

  setPriceChartWindow(start, count) {
    const total = this.priceChartState.candles.length;
    if (!total) return;
    const safeCount = Math.max(1, Math.min(total, Math.round(count)));
    const safeStart = Math.max(0, Math.min(total - safeCount, Math.round(start)));
    this.priceChartState.viewStart = safeStart;
    this.priceChartState.viewEnd = safeStart + safeCount;
    this.priceChartState.hover = null;
    this.hidePriceChartTooltip();
    this.schedulePriceChartDraw();
  }

  zoomPriceChart(factor, anchorRatio = .5) {
    const total = this.priceChartState.candles.length;
    const currentCount = this.visiblePriceCandleCount() || total;
    if (!total || !currentCount) return;
    const minimum = Math.min(total, 24);
    const nextCount = Math.max(minimum, Math.min(total, Math.round(currentCount * factor)));
    const anchor = Math.max(0, Math.min(1, anchorRatio));
    const anchorIndex = this.priceChartState.viewStart + currentCount * anchor;
    this.setPriceChartWindow(anchorIndex - nextCount * anchor, nextCount);
  }

  panPriceChart(deltaBars) {
    const count = this.visiblePriceCandleCount();
    if (!count) return;
    this.setPriceChartWindow(this.priceChartState.viewStart + deltaBars, count);
  }

  handlePriceChartWheel(event) {
    const layout = this.priceChartState.layout;
    if (!layout || !this.priceChartState.candles.length) return;
    event.preventDefault();
    const horizontal = event.shiftKey || Math.abs(event.deltaX) > Math.abs(event.deltaY);
    if (horizontal) {
      const delta = event.shiftKey ? event.deltaY : event.deltaX;
      this.panPriceChart(Math.round(delta / Math.max(8, layout.plotWidth) * this.visiblePriceCandleCount()));
      return;
    }
    const anchor = (event.offsetX - layout.padding.left) / Math.max(1, layout.plotWidth);
    this.zoomPriceChart(event.deltaY < 0 ? .8 : 1.25, anchor);
  }

  handlePriceChartPointerDown(event) {
    if (event.button !== 0 || !this.priceChartState.layout) return;
    const state = this.priceChartState;
    state.dragging = true;
    state.pointerId = event.pointerId;
    state.dragStartX = event.offsetX;
    state.dragViewStart = state.viewStart;
    state.hover = null;
    this.hidePriceChartTooltip();
    this.schedulePriceChartDraw();
    event.currentTarget.classList.add("dragging");
    event.currentTarget.setPointerCapture?.(event.pointerId);
  }

  handlePriceChartPointerMove(event) {
    const state = this.priceChartState;
    const layout = state.layout;
    if (!layout) return;
    if (state.dragging) {
      const candleCount = this.visiblePriceCandleCount();
      const shift = Math.round((state.dragStartX - event.offsetX) / Math.max(1, layout.plotWidth) * candleCount);
      const total = state.candles.length;
      const nextStart = Math.max(0, Math.min(total - candleCount, state.dragViewStart + shift));
      if (nextStart !== state.viewStart) {
        state.viewStart = nextStart;
        state.viewEnd = nextStart + candleCount;
        this.schedulePriceChartDraw();
      }
      return;
    }
    const inside = event.offsetX >= layout.padding.left
      && event.offsetX <= layout.width - layout.padding.right
      && event.offsetY >= layout.padding.top
      && event.offsetY <= layout.height - layout.padding.bottom;
    if (!inside) {
      this.hidePriceChartTooltip();
      return;
    }
    state.hover = { x: event.offsetX, y: event.offsetY };
    this.renderPriceChartTooltip(event.offsetX, event.offsetY);
    this.schedulePriceChartDraw();
  }

  handlePriceChartPointerUp(event) {
    const state = this.priceChartState;
    if (!state.dragging) return;
    state.dragging = false;
    state.pointerId = null;
    event.currentTarget.classList.remove("dragging");
    if (event.currentTarget.hasPointerCapture?.(event.pointerId)) event.currentTarget.releasePointerCapture(event.pointerId);
    this.handlePriceChartPointerMove(event);
  }

  schedulePriceChartDraw() {
    if (this.priceChartFrame) return;
    this.priceChartFrame = window.requestAnimationFrame(() => {
      this.priceChartFrame = 0;
      this.drawPriceChart(this.q("#price-chart"));
    });
  }

  hidePriceChartTooltip() {
    const tooltip = this.q("#price-chart-tooltip");
    if (tooltip) tooltip.classList.add("hidden");
    if (this.priceChartState) {
      const hadHover = Boolean(this.priceChartState.hover);
      this.priceChartState.hover = null;
      if (hadHover && !this.priceChartState.dragging) this.schedulePriceChartDraw();
    }
  }

  renderPriceChartTooltip(pointerX, pointerY) {
    const tooltip = this.q("#price-chart-tooltip");
    const wrap = this.q(".price-chart-wrap");
    const layout = this.priceChartState.layout;
    if (!tooltip || !wrap || !layout) return;
    let target = null;
    let distance = Infinity;
    layout.markers.forEach((marker) => {
      const value = Math.hypot(marker.x - pointerX, marker.y - pointerY);
      if (value < distance) {
        distance = value;
        target = marker;
      }
    });
    if (distance > 16) {
      target = layout.candles.reduce((nearest, candle) => (
        !nearest || Math.abs(candle.x - pointerX) < Math.abs(nearest.x - pointerX) ? candle : nearest
      ), null);
    }
    if (!target) {
      this.hidePriceChartTooltip();
      return;
    }

    const rows = [];
    let title = "K 线行情";
    let tone = "";
    if (target.trade) {
      const trade = target.trade;
      const execution = target.execution || {};
      const phase = execution.phase === "exit" || target.kind === "exit" ? "exit" : "entry";
      const orderSide = String(execution.order_side || "").toLowerCase();
      const positionSide = String(execution.position_side || "").toLowerCase();
      const action = String(execution.action || "").toLowerCase();
      const isExit = phase === "exit";
      const isFinalExit = Boolean(target.isFinalExit);
      const tradePnl = Number(trade.net_pnl ?? trade.pnl ?? trade.profit ?? 0);
      const executionPnl = Number(execution.net_pnl);
      const pnl = isFinalExit || !Number.isFinite(executionPnl) ? tradePnl : executionPnl;
      title = this.executionLabel({ orderSide, positionSide, action, phase });
      tone = isExit ? (pnl > 0 ? "profit" : pnl < 0 ? "loss" : "") : "";
      rows.push(
        ["成交时间", this.shortDate(target.time, true)],
        ["成交动作", title],
        ["成交价格", this.price(execution.price)],
        ["成交数量", this.quantity(execution.quantity)],
        ["方向腿", positionSide === "short" ? "空头" : "多头"],
        ["杠杆倍率", `${this.integer(trade.leverage ?? 1)}x`],
      );
      if (isExit) {
        const executionFee = Number(execution.fee);
        rows.push(
          [isFinalExit ? "本周期净盈亏" : "本次平仓净盈亏", this.signedMoney(pnl), tone],
          ["本次平仓手续费", this.signedMoney(-Math.abs(Number.isFinite(executionFee) ? executionFee : 0))],
        );
        if (isFinalExit) {
          rows.push(
            ["账户收益率", this.percent(trade.account_return_pct ?? trade.return_pct ?? trade.pnl_pct, true), tone],
            ["保证金 ROE", this.percent(trade.margin_return_pct ?? trade.return_pct ?? trade.pnl_pct, true), tone],
            ["平仓后可用金额", this.money(trade.available_balance_after_close ?? trade.available_balance)],
            ["退出原因", this.exitReason(trade.exit_reason ?? trade.reason)],
          );
        }
      } else {
        rows.push(
          ["开仓保证金", this.money(trade.initial_margin ?? trade.peak_initial_margin ?? trade.margin)],
          ["剩余可用金额", this.money(trade.remaining_available_balance ?? trade.minimum_available_balance ?? trade.available_balance)],
        );
      }
    } else {
      const candle = target.candle;
      rows.push(
        ["时间", this.shortDate(candle.ts, true)],
        ["开盘", this.price(candle.open)],
        ["最高", this.price(candle.high)],
        ["最低", this.price(candle.low)],
        ["收盘", this.price(candle.close)],
        ["成交量", this.quantity(candle.volume)],
      );
    }
    const head = this.node("strong", "price-tooltip-title", title);
    const body = this.node("div", "price-tooltip-grid");
    rows.forEach(([label, value, rowTone = ""]) => {
      const row = this.node("div", rowTone);
      row.append(this.node("span", "", label), this.node("b", "", value));
      body.append(row);
    });
    tooltip.className = `price-chart-tooltip ${tone}`.trim();
    tooltip.replaceChildren(head, body);
    const canvas = this.q("#price-chart");
    const originX = canvas?.offsetLeft || 0;
    const originY = canvas?.offsetTop || 0;
    tooltip.style.left = `${Math.max(8, Math.min(wrap.clientWidth - 286, originX + pointerX + 14))}px`;
    tooltip.style.top = `${Math.max(8, Math.min(wrap.clientHeight - tooltip.offsetHeight - 8, originY + pointerY - 18))}px`;
  }

  drawPriceChart(canvas, rawCandles, trades) {
    if (!canvas) return;
    const width = Math.floor(canvas.clientWidth);
    const height = Math.floor(canvas.clientHeight);
    if (!width || !height) return;
    const ratio = Math.min(window.devicePixelRatio || 1, 2);
    canvas.width = Math.floor(width * ratio);
    canvas.height = Math.floor(height * ratio);
    const context = canvas.getContext("2d");
    context.setTransform(ratio, 0, 0, ratio, 0, 0);
    context.clearRect(0, 0, width, height);
    context.font = "12px ui-monospace, SFMono-Regular, Menlo, monospace";

    const state = this.priceChartState;
    if (rawCandles !== undefined) {
      const candles = (Array.isArray(rawCandles) ? rawCandles : []).map((item) => ({
        ts: this.chartTimestamp(item?.ts ?? item?.timestamp ?? item?.open_time),
        open: Number(item?.open), high: Number(item?.high), low: Number(item?.low),
        close: Number(item?.close), volume: Number(item?.volume || 0),
      })).filter((item) => Number.isFinite(item.ts) && [item.open, item.high, item.low, item.close].every(Number.isFinite));
      const tradeList = Array.isArray(trades) ? trades : [];
      const firstTrade = tradeList[0] || {};
      const lastTrade = tradeList[tradeList.length - 1] || {};
      const dataKey = [
        candles.length,
        candles[0]?.ts,
        candles[candles.length - 1]?.ts,
        tradeList.length,
        firstTrade.entry_ts ?? firstTrade.entry_at ?? firstTrade.entry_time,
        lastTrade.exit_ts ?? lastTrade.exit_at ?? lastTrade.exit_time,
      ].join(":");
      if (dataKey !== state.dataKey) {
        state.dataKey = dataKey;
        state.candles = candles;
        state.trades = tradeList;
        state.viewStart = 0;
        state.viewEnd = candles.length;
        state.hover = null;
        this.hidePriceChartTooltip();
      } else {
        state.candles = candles;
        state.trades = tradeList;
      }
    }
    const candles = state.candles;
    if (!candles.length) {
      state.layout = null;
      context.fillStyle = "#64778d";
      context.fillText("当前历史记录没有保存 K 线快照，请重新运行一次回测", 18, 31);
      const range = this.q("#price-chart-range");
      if (range) range.textContent = "暂无 K 线";
      return;
    }

    const padding = { left: 12, right: 66, top: 16, bottom: 28 };
    const plotWidth = Math.max(1, width - padding.left - padding.right);
    const plotHeight = Math.max(1, height - padding.top - padding.bottom);
    const total = candles.length;
    const viewStart = Math.max(0, Math.min(total - 1, state.viewStart));
    const viewEnd = Math.max(viewStart + 1, Math.min(total, state.viewEnd || total));
    state.viewStart = viewStart;
    state.viewEnd = viewEnd;
    const visibleCandles = candles.slice(viewStart, viewEnd);
    const lows = visibleCandles.map((item) => item.low);
    const highs = visibleCandles.map((item) => item.high);
    let minPrice = Math.min(...lows);
    let maxPrice = Math.max(...highs);
    const pricePadding = (maxPrice - minPrice || Math.abs(maxPrice) * .01 || 1) * .08;
    minPrice -= pricePadding;
    maxPrice += pricePadding;
    const priceRange = maxPrice - minPrice || 1;
    const firstTs = visibleCandles[0].ts;
    const lastTs = visibleCandles[visibleCandles.length - 1].ts;
    const timeRange = lastTs - firstTs || 1;
    const xTime = (ts) => visibleCandles.length === 1
      ? padding.left + plotWidth / 2
      : padding.left + Math.max(0, Math.min(1, (ts - firstTs) / timeRange)) * plotWidth;
    const yPrice = (price) => padding.top + (maxPrice - price) / priceRange * plotHeight;
    const layout = { width, height, padding, plotWidth, plotHeight, markers: [], candles: [] };
    state.layout = layout;

    context.strokeStyle = "rgba(126, 154, 181, .14)";
    context.fillStyle = "#657b91";
    context.lineWidth = 1;
    for (let line = 0; line <= 4; line += 1) {
      const price = maxPrice - priceRange * line / 4;
      const y = yPrice(price);
      context.beginPath(); context.moveTo(padding.left, y); context.lineTo(width - padding.right, y); context.stroke();
      context.fillText(this.price(price), width - padding.right + 7, y + 4);
    }

    const candleWidth = Math.max(.7, Math.min(12, plotWidth / visibleCandles.length * .68));
    visibleCandles.forEach((item) => {
      const x = xTime(item.ts);
      layout.candles.push({ x, candle: item });
      const rising = item.close >= item.open;
      context.strokeStyle = rising ? "#31d4a0" : "#f06478";
      context.fillStyle = context.strokeStyle;
      context.beginPath(); context.moveTo(x, yPrice(item.high)); context.lineTo(x, yPrice(item.low)); context.stroke();
      const top = yPrice(Math.max(item.open, item.close));
      const bottom = yPrice(Math.min(item.open, item.close));
      context.fillRect(x - candleWidth / 2, top, candleWidth, Math.max(1, bottom - top));
    });

    const markerBounds = [];
    const marker = (ts, price, color, shape, label, trade, kind, execution, isFinalExit = false) => {
      if (!Number.isFinite(ts) || !Number.isFinite(price) || ts < firstTs || ts > lastTs) return;
      const baseX = xTime(ts);
      const baseY = yPrice(price);
      context.save();
      context.font = "bold 11px sans-serif";
      const labelWidth = Math.max(24, Math.ceil(context.measureText(label).width));
      context.restore();
      const candidates = [
        [0, 0], [0, -20], [0, 20], [36, 0], [-36, 0],
        [36, -20], [-36, -20], [36, 20], [-36, 20],
        [0, -40], [0, 40], [72, 0], [-72, 0],
      ];
      let placement = null;
      for (const [offsetX, offsetY] of candidates) {
        const candidateX = Math.max(padding.left + 7, Math.min(width - padding.right - labelWidth - 12, baseX + offsetX));
        const candidateY = Math.max(padding.top + 12, Math.min(height - padding.bottom - 12, baseY + offsetY));
        const labelAbove = offsetY <= 0;
        const labelX = candidateX + 8;
        const labelY = candidateY + (labelAbove ? -8 : 14);
        const bounds = {
          left: candidateX - 7,
          right: labelX + labelWidth + 3,
          top: Math.min(candidateY - 9, labelY - 12),
          bottom: Math.max(candidateY + 9, labelY + 3),
        };
        const collides = markerBounds.some((item) => !(
          bounds.right + 3 < item.left
          || bounds.left - 3 > item.right
          || bounds.bottom + 3 < item.top
          || bounds.top - 3 > item.bottom
        ));
        if (!collides) {
          placement = { x: candidateX, y: candidateY, labelX, labelY, bounds };
          break;
        }
      }
      if (!placement) {
        const overflowIndex = markerBounds.length;
        const direction = overflowIndex % 2 ? 1 : -1;
        const distance = 20 + Math.ceil(overflowIndex / 2) * 18;
        const x = Math.max(padding.left + 7, Math.min(width - padding.right - labelWidth - 12, baseX));
        const y = Math.max(padding.top + 12, Math.min(height - padding.bottom - 12, baseY + direction * distance));
        const labelX = x + 8;
        const labelY = y + (direction < 0 ? -8 : 14);
        placement = {
          x,
          y,
          labelX,
          labelY,
          bounds: {
            left: x - 7,
            right: labelX + labelWidth + 3,
            top: Math.min(y - 9, labelY - 12),
            bottom: Math.max(y + 9, labelY + 3),
          },
        };
      }
      const { x, y, labelX, labelY, bounds } = placement;
      markerBounds.push(bounds);
      layout.markers.push({ x, y, baseX, baseY, kind, trade, execution, isFinalExit, time: ts });
      context.save();
      if (Math.abs(x - baseX) > 1 || Math.abs(y - baseY) > 1) {
        context.setLineDash([2, 3]);
        context.strokeStyle = color;
        context.globalAlpha = .55;
        context.lineWidth = 1;
        context.beginPath();
        context.moveTo(baseX, baseY);
        context.lineTo(x, y);
        context.stroke();
        context.setLineDash([]);
        context.globalAlpha = 1;
      }
      context.fillStyle = color;
      context.strokeStyle = "#07111b";
      context.lineWidth = 1.5;
      context.beginPath();
      if (shape === "up") {
        context.moveTo(x, y - 8); context.lineTo(x - 6, y + 3); context.lineTo(x + 6, y + 3);
      } else if (shape === "down") {
        context.moveTo(x, y + 8); context.lineTo(x - 6, y - 3); context.lineTo(x + 6, y - 3);
      } else {
        context.arc(x, y, 4.5, 0, Math.PI * 2);
      }
      context.closePath(); context.fill(); context.stroke();
      context.fillStyle = color;
      context.font = "bold 11px sans-serif";
      context.fillText(label, labelX, labelY);
      context.restore();
    };
    state.trades.forEach((trade) => {
      const side = String(trade?.side ?? trade?.direction ?? "").toLowerCase();
      const isLong = ["long", "buy", "1", "多"].includes(side) || Number(trade?.side) > 0;
      const isBasket = Boolean(trade?.is_mixed_basket)
        || String(trade?.position_structure ?? "").toLowerCase() === "mixed_basket"
        || ["basket", "mixed", "双向", "篮子"].includes(side);
      const executions = this.tradeExecutions(trade, {
        isBasket,
        isLong,
        entryTime: trade?.entry_ts ?? trade?.entry_at ?? trade?.entry_time,
        exitTime: trade?.exit_ts ?? trade?.exit_at ?? trade?.exit_time,
      });
      const finalExitIndex = executions.reduce((latest, execution, index) => execution.phase === "exit" ? index : latest, -1);
      executions.forEach((execution, index) => {
        const phase = execution.phase === "exit" ? "exit" : "entry";
        const orderSide = String(execution.order_side || "").toLowerCase();
        const action = String(execution.action || "").toLowerCase();
        const isBuy = orderSide === "buy";
        const label = phase === "exit"
          ? (isBuy ? "买平" : "卖平")
          : action === "add" ? (isBuy ? "买加" : "卖加") : (isBuy ? "买开" : "卖开");
        marker(
          this.chartTimestamp(execution.timestamp),
          Number(execution.price),
          phase === "exit" ? "#e6b850" : isBuy ? "#31d4a0" : "#f06478",
          phase === "exit" ? "exit" : isBuy ? "up" : "down",
          label,
          trade,
          phase,
          execution,
          index === finalExitIndex,
        );
      });
    });

    if (state.hover) {
      context.save();
      context.setLineDash([4, 4]);
      context.strokeStyle = "rgba(180, 198, 216, .34)";
      context.lineWidth = 1;
      context.beginPath();
      context.moveTo(state.hover.x, padding.top);
      context.lineTo(state.hover.x, height - padding.bottom);
      context.moveTo(padding.left, state.hover.y);
      context.lineTo(width - padding.right, state.hover.y);
      context.stroke();
      context.restore();
    }

    context.fillStyle = "#657b91";
    const dateLabel = (seconds) => new Date(seconds * 1000).toLocaleString("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false });
    context.fillText(dateLabel(firstTs), padding.left, height - 8);
    const endLabel = dateLabel(lastTs);
    context.fillText(endLabel, Math.max(padding.left, width - padding.right - context.measureText(endLabel).width), height - 8);
    const range = this.q("#price-chart-range");
    if (range) range.textContent = visibleCandles.length === total
      ? `全部 ${this.integer(total)} 根`
      : `可视 ${this.integer(viewStart + 1)}–${this.integer(viewEnd)} / ${this.integer(total)} 根`;
  }

  normalizeCurve(raw) {
    let peak = -Infinity;
    return (Array.isArray(raw) ? raw : []).map((point, index) => {
      const time = Array.isArray(point) ? point[0] : point?.timestamp ?? point?.ts ?? point?.at ?? point?.date ?? index;
      const equity = Number(Array.isArray(point) ? point[1] : point?.equity ?? point?.value ?? point?.balance);
      if (!Number.isFinite(equity)) return null;
      peak = Math.max(peak, equity);
      const supplied = Number(Array.isArray(point) ? point[2] : point?.drawdown_pct ?? point?.drawdown);
      const drawdown = Number.isFinite(supplied) ? (supplied > 0 ? -supplied : supplied) : (peak ? (equity / peak - 1) * 100 : 0);
      return { time, equity, drawdown };
    }).filter(Boolean);
  }

  drawLineChart(canvas, points, type) {
    const width = Math.floor(canvas.clientWidth);
    const height = Math.floor(canvas.clientHeight);
    if (!width || !height) return;
    const ratio = Math.min(window.devicePixelRatio || 1, 2);
    canvas.width = Math.floor(width * ratio);
    canvas.height = Math.floor(height * ratio);
    const context = canvas.getContext("2d");
    context.setTransform(ratio, 0, 0, ratio, 0, 0);
    context.clearRect(0, 0, width, height);
    const padding = { left: 14, right: 62, top: 15, bottom: 25 };
    const plotWidth = width - padding.left - padding.right;
    const plotHeight = height - padding.top - padding.bottom;
    context.font = "16px Inter, sans-serif";
    if (points.length < 2) {
      context.fillStyle = "#64778d";
      context.fillText("权益样本不足，暂无可绘制曲线", 18, 30);
      return;
    }
    const values = points.map((point) => type === "equity" ? point.equity : point.drawdown);
    let min = Math.min(...values);
    let max = Math.max(...values);
    const rangePadding = (max - min || Math.abs(max) * 0.02 || 1) * 0.08;
    min -= rangePadding;
    max += rangePadding;
    const range = max - min || 1;
    const x = (index) => padding.left + index / (points.length - 1) * plotWidth;
    const y = (value) => padding.top + (max - value) / range * plotHeight;
    context.strokeStyle = "rgba(126, 154, 181, .13)";
    context.fillStyle = "#657b91";
    context.lineWidth = 1;
    for (let line = 0; line <= 3; line += 1) {
      const value = max - range * line / 3;
      const yValue = y(value);
      context.beginPath();
      context.moveTo(padding.left, yValue);
      context.lineTo(width - padding.right, yValue);
      context.stroke();
      context.fillText(type === "equity" ? this.compactNumber(value) : `${value.toFixed(1)}%`, width - padding.right + 7, yValue + 3);
    }
    const color = type === "equity" ? "#31d4a0" : "#f06478";
    const gradient = context.createLinearGradient(0, padding.top, 0, height - padding.bottom);
    gradient.addColorStop(0, type === "equity" ? "rgba(49, 212, 160, .24)" : "rgba(240, 100, 120, .24)");
    gradient.addColorStop(1, "rgba(10, 20, 32, 0)");
    context.beginPath();
    points.forEach((point, index) => {
      const position = [x(index), y(type === "equity" ? point.equity : point.drawdown)];
      if (index) context.lineTo(...position);
      else context.moveTo(...position);
    });
    context.lineTo(x(points.length - 1), height - padding.bottom);
    context.lineTo(x(0), height - padding.bottom);
    context.closePath();
    context.fillStyle = gradient;
    context.fill();
    context.beginPath();
    points.forEach((point, index) => {
      const position = [x(index), y(type === "equity" ? point.equity : point.drawdown)];
      if (index) context.lineTo(...position);
      else context.moveTo(...position);
    });
    context.strokeStyle = color;
    context.lineWidth = 2;
    context.lineJoin = "round";
    context.stroke();
    context.fillStyle = "#657b91";
    context.fillText(this.chartDate(points[0].time), padding.left, height - 6);
    const endLabel = this.chartDate(points[points.length - 1].time);
    const endWidth = context.measureText(endLabel).width;
    context.fillText(endLabel, width - padding.right - endWidth, height - 6);
  }

  setRunButton(running) {
    const button = this.q("#run-backtest");
    button.disabled = running || !this.catalog.strategies.length;
    button.classList.toggle("loading", running);
    button.querySelector("span").textContent = running ? "◌" : "▶";
    button.querySelector("strong").textContent = running ? "正在回放行情…" : "运行回测";
    this.setProfileButtons(running);
  }

  setStageStatus(text, tone) {
    const status = this.q("#stage-status");
    status.className = `stage-status ${tone}`;
    status.lastChild.textContent = text;
  }

  showBanner(message, tone = "") {
    const banner = this.q("#global-banner");
    banner.textContent = message;
    banner.className = `global-banner${message ? "" : " hidden"}${tone ? ` ${tone}` : ""}`;
  }

  runId(value) {
    const id = value?.run?.id ?? value?.run?.run_id ?? value?.id ?? value?.run_id;
    return id == null ? "" : String(id);
  }

  unpackDetail(detail = {}) {
    const run = detail.run || detail;
    if (detail.result) return { run, result: detail.result || {} };
    const extendedMetrics = run.metrics_json && typeof run.metrics_json === "object" ? run.metrics_json : {};
    const metrics = {
      ...extendedMetrics,
      total_return_pct: run.total_return_pct ?? extendedMetrics.total_return_pct,
      max_drawdown_pct: run.max_drawdown_pct ?? extendedMetrics.max_drawdown_pct,
      sharpe_ratio: run.sharpe_ratio ?? extendedMetrics.sharpe_ratio,
      win_rate_pct: run.win_rate_pct ?? extendedMetrics.win_rate_pct,
      profit_factor: run.profit_factor ?? extendedMetrics.profit_factor,
      trade_count: run.trade_count ?? extendedMetrics.trade_count,
    };
    return {
      run,
      result: {
        account: {
          initial_capital: run.initial_capital,
          final_equity: run.final_equity,
          net_profit: run.net_profit,
          total_fees: extendedMetrics.total_fees,
        },
        metrics,
        equity_curve: run.equity_curve_json || [],
        trades: Array.isArray(run.trades) ? run.trades : [],
        data_quality: run.data_quality_json || {},
      },
    };
  }

  metric(metrics, keys) {
    for (const key of keys) if (metrics?.[key] != null) return metrics[key];
    return null;
  }

  toneClass(value) {
    const numeric = this.numericValue(value);
    if (numeric == null || numeric === 0) return "neutral";
    return numeric > 0 ? "positive" : "negative";
  }

  numericValue(value) {
    if (value == null || (typeof value === "string" && value.trim() === "")) return null;
    const numeric = Number(value);
    return Number.isFinite(numeric) ? numeric : null;
  }

  number(value, digits = 2) {
    const numeric = this.numericValue(value);
    if (numeric == null) return "--";
    return numeric.toLocaleString("zh-CN", { minimumFractionDigits: 0, maximumFractionDigits: digits });
  }

  integer(value) {
    const numeric = this.numericValue(value);
    return numeric == null ? "--" : Math.round(numeric).toLocaleString("zh-CN");
  }

  money(value) {
    const numeric = this.numericValue(value);
    return numeric == null ? "--" : `${numeric.toLocaleString("zh-CN", { minimumFractionDigits: 0, maximumFractionDigits: 2 })} U`;
  }

  signedMoney(value) {
    const numeric = this.numericValue(value);
    return numeric == null ? "--" : `${numeric > 0 ? "+" : ""}${this.number(numeric)} U`;
  }

  percent(value, signed = true) {
    const numeric = this.numericValue(value);
    if (numeric == null) return "--";
    return `${signed && numeric > 0 ? "+" : ""}${this.number(numeric, 2)}%`;
  }

  price(value) {
    const numeric = this.numericValue(value);
    if (numeric == null) return "--";
    return numeric >= 100 ? numeric.toFixed(2) : numeric >= 1 ? numeric.toFixed(4) : numeric.toFixed(6);
  }

  quantity(value) {
    const numeric = this.numericValue(value);
    if (numeric == null) return "--";
    if (numeric !== 0 && Math.abs(numeric) < 0.0001) return numeric.toExponential(4);
    return this.number(numeric, 8);
  }

  exitReason(value) {
    const labels = {
      strategy_reversal: "策略反转",
      stop_loss: "止损",
      take_profit: "止盈",
      liquidation: "强制平仓",
      max_holding_bars: "达到最长持有",
      end_of_data: "数据结束",
      basket_take_profit: "篮子止盈",
      basket_stop_loss: "篮子止损",
      basket_trailing_stop: "篮子追踪止盈",
    };
    return labels[value] || value || "策略退出";
  }

  compactNumber(value) {
    const numeric = Number(value);
    if (!Number.isFinite(numeric)) return "--";
    return new Intl.NumberFormat("zh-CN", { notation: "compact", maximumFractionDigits: 1 }).format(numeric);
  }

  dateOnly(value) {
    if (!value) return "";
    if (typeof value === "string" && /^\d{4}-\d{2}-\d{2}/.test(value)) return value.slice(0, 10);
    const numeric = Number(value);
    const date = new Date(Number.isFinite(numeric) && numeric > 0 && numeric < 1e12 ? numeric * 1000 : value);
    if (Number.isNaN(date.getTime())) return "";
    const year = date.getFullYear();
    const month = String(date.getMonth() + 1).padStart(2, "0");
    const day = String(date.getDate()).padStart(2, "0");
    return `${year}-${month}-${day}`;
  }

  shiftMonths(value, amount) {
    const date = new Date(`${value}T12:00:00`);
    if (Number.isNaN(date.getTime())) return "";
    date.setMonth(date.getMonth() + amount);
    return this.dateOnly(date);
  }

  maxDate(left, right) {
    if (!left) return right;
    if (!right) return left;
    return left > right ? left : right;
  }

  shortDate(value, includeTime = false) {
    if (!value) return "--";
    const numeric = Number(value);
    const date = new Date(Number.isFinite(numeric) && numeric > 0 ? (numeric < 1e12 ? numeric * 1000 : numeric) : value);
    if (Number.isNaN(date.getTime())) return String(value).slice(0, includeTime ? 16 : 10);
    return date.toLocaleString("zh-CN", includeTime ? { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false } : { year: "numeric", month: "2-digit", day: "2-digit" });
  }

  chartDate(value) {
    if (typeof value === "number" && value < 1e9) return String(value);
    const date = new Date(typeof value === "number" && value < 1e12 ? value * 1000 : value);
    if (Number.isNaN(date.getTime())) return String(value ?? "").slice(0, 10);
    return `${date.getMonth() + 1}/${date.getDate()}`;
  }
}

window.quantdeskRegisterPageController("backtest-workbench", BacktestWorkbench);
