class AiMonitorDashboard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this.state = {
      view: "opportunities",
      overview: null,
      macroMarket: null,
      config: null,
      indicators: [],
      indicatorTemplates: [],
      indicatorConflictPairs: [],
      symbols: [],
      news: [],
      runs: [],
      opportunities: [],
      opportunityAnalytics: null,
      predictionFilters: { newsScoreMin: 0, indicatorScoreMin: 0, direction: "all" },
      predictionPage: 1,
      predictionPageSize: 20,
      displayLeverage: 10,
      predictionAnalyticsRequestId: 0,
      draftSymbols: new Set(),
      symbolSearch: "",
      opportunityTab: "current",
      opportunityStatusFilter: "all",
      opportunityStatusCounts: { all: 0, triggered: 0, ready: 0, waiting: 0, failed: 0 },
      opportunityDirectionCounts: { long: 0, short: 0 },
      historyOpportunityDirectionCounts: { long: 0, short: 0 },
      historyOpportunitySettlementCounts: { total: 0, pending: 0, unavailable: 0 },
      opportunityRequestId: 0,
      opportunitiesLoading: false,
      opportunityLoadingTab: "",
      opportunitiesLoadedTab: "",
      liveStateLoading: false,
      fullLoadLoading: false,
      newsRenderSignature: "",
      running: false,
      busyRun: "",
      newsSearch: "",
      newsMode: "all",
      analyzingNewsIds: new Set(),
      conclusionView: "analysis",
    };
    this.timers = [];
    this.scrollPaused = false;
    this.eventsBound = false;
    this.conclusionPanels = { fundamentals: "", news: "", memory: "", market: "", analysis: "" };
    this.conclusionNewsRequestId = 0;
    this.conclusionMemoryRequestId = 0;
    this.conclusionFundamentalRequestId = 0;
    this.newsLogicFocus = null;
    this.newsModelCalls = [];
    this.newsModelCallIndex = 0;
    this.newsModelCallsRequestId = 0;
    this.newsSystemPromptFocus = null;
    this.newsSystemPromptDefault = "";
    this.newsSystemPromptIsCustom = false;
    this.newsSystemPromptRequestId = 0;
    this.historicalJudgmentFocus = null;
    this.historicalJudgmentRequestId = 0;
    this.scoreTrendFocus = null;
    this.scoreTrendOpportunity = null;
    this.handleVisibilityChange = () => {
      if (document.visibilityState === "visible" && this.state.running) this.loadLiveState();
    };
    this.renderShell();
  }

  connectedCallback() {
    this.bindEvents();
    document.addEventListener("visibilitychange", this.handleVisibilityChange);
  }

  disconnectedCallback() {
    this.pause();
    document.removeEventListener("visibilitychange", this.handleVisibilityChange);
  }

  renderShell() {
    this.shadowRoot.innerHTML = `
      <link rel="stylesheet" href="/assets/ai-monitor.css?v=20260816-27">
      <div class="ai-monitor">
        <header class="ai-head">
          <div>
            <span class="eyebrow">DISCOVERED OPPORTUNITIES</span>
            <h1><i aria-hidden="true">✦</i> 发现机会</h1>
            <p>每 15 分钟分析最新 10 条新闻 × 美股技术指标确认 · 机会只生成预测，不执行真实交易</p>
          </div>
          <div class="ai-head-actions">
            <span id="ai-clock" class="ai-clock"></span>
            <span id="scheduler-state" class="status-badge idle">读取中</span>
            <button id="open-news-config" type="button">新闻分析配置</button>
            <button id="run-news" type="button">立即分析新闻</button>
            <button id="run-opportunity" class="primary-action" type="button">立即发现机会</button>
            <button id="open-weight-config" type="button">权重设置</button>
            <button id="open-config" type="button">指标配置</button>
            <button id="ai-refresh" type="button">刷新</button>
          </div>
        </header>
        <div id="ai-banner" class="ai-banner hidden" role="status"></div>
        <section id="macro-market-panel" class="macro-market-panel" aria-label="美股宏观大盘环境">
          <div class="macro-market-loading"><span>US MARKET REGIME</span><strong>正在读取美股大盘环境…</strong></div>
        </section>
        <div class="ai-layout">
          <nav class="ai-module-nav" aria-label="发现机会菜单">
            <button class="ai-nav-root active" type="button" data-ai-view="opportunities"><span>◆</span><strong>发现机会</strong><small>机会总览</small></button>
            <div class="ai-subnav-group" aria-label="发现机会二级菜单">
              <button class="ai-nav-child" type="button" data-ai-view="news"><span>01</span><strong>新闻列表</strong><small>实时滚动</small></button>
              <button class="ai-nav-child" type="button" data-ai-view="runs"><span>02</span><strong>分析记录</strong><small>任务与结论</small></button>
              <button class="ai-nav-child" type="button" data-ai-view="predictions"><span>03</span><strong>预测统计分析</strong><small>历史命中</small></button>
            </div>
          </nav>
          <main class="ai-content">
            <section id="view-news" class="ai-view">
              <div class="view-head">
                <div><span class="eyebrow">LIVE NEWS STREAM</span><h2>当前新闻滚动</h2><p>每 15 分钟从数据库读取最新 10 条未分析新闻，识别情绪、行业和关联美股。</p></div>
                <div class="view-tools"><input id="news-search" aria-label="搜索新闻" placeholder="搜索标题 / 来源 / 行业 / 股票"><select id="news-mode" aria-label="新闻筛选"><option value="all">全部新闻</option><option value="analyzed">已 AI 分析</option><option value="pending">待分析</option><option value="bull">偏多新闻</option><option value="bear">偏空新闻</option><option value="neutral">中性新闻</option></select></div>
              </div>
              <div id="news-stream" class="news-stream"><div class="empty-state">正在连接新闻流…</div></div>
            </section>
            <section id="view-runs" class="ai-view">
              <div class="view-head"><div><span class="eyebrow">ANALYSIS LEDGER</span><h2>分析记录</h2><p>保留每轮新闻分析和机会扫描的输入、结果与失败原因。</p></div></div>
              <div id="run-list" class="run-list"><div class="empty-state">暂无分析记录</div></div>
            </section>
            <section id="view-config" class="ai-view">
              <div class="view-head"><div><span class="eyebrow">SIGNAL CONFIGURATION</span><h2>指标配置</h2><p>设置新闻分析周期、机会扫描周期和技术确认条件。</p></div></div>
              <form id="ai-config-form" class="config-form">
                <section class="config-block automation-block">
                  <div><strong>后台周期监控</strong><small>开启后，服务端会按配置周期持续运行；关闭仍可手动执行。</small></div>
                  <label class="toggle"><input id="config-enabled" type="checkbox"><span></span><b id="config-enabled-label">已暂停</b></label>
                </section>
                <div id="model-warning" class="model-warning hidden">管理员尚未配置全局 DeepSeek。请联系管理员在管理后台完成配置。</div>
                <section class="config-grid">
                  <label><span>新闻分析间隔</span><div><input id="config-news-interval" type="number" min="5" max="1440" step="1" required><em>分钟</em></div><small>每轮读取最新 10 条尚未完成 AI 研判的新闻</small></label>
                  <label><span>机会发现间隔</span><div><input id="config-opportunity-interval" type="number" min="5" max="1440" step="1" required><em>分钟</em></div><small>组合新闻和技术指标重新扫描</small></label>
                  <label><span>AI 新闻记忆范围</span><div><input id="config-lookback" type="number" min="1" max="168" step="1" required><em>小时</em></div><small>历史研判只作为上下文记忆，不会单独触发新机会</small></label>
                  <label><span>技术指标周期</span><select id="config-timeframe"><option value="15m">15 分钟</option><option value="1h">1 小时</option><option value="4h">4 小时</option></select><small>采用对应周期最新一根已收盘 K 线</small></label>
                  <label><span>预测最大持有周期</span><div><input id="config-max-holding-bars" type="number" min="1" max="24" step="1" required><em>根 K 线</em></div><small>与技术周期独立；默认持有 4 根，退出条件仍可提前触发</small></label>
                  <label><span>最低新闻置信度</span><div><input id="config-confidence" type="number" min="0" max="100" step="1" required><em>%</em></div><small>AI 置信度 × 股票相关度</small></label>
                  <label><span>最少关联新闻</span><div><input id="config-mentions" type="number" min="1" max="20" step="1" required><em>条</em></div><small>同一股票达到数量后才进入指标确认</small></label>
                  <label><span>最低技术强度</span><div><input id="config-indicator-score" type="number" min="0" max="100" step="1" required><em>分</em></div><small>按最佳有效策略组计算，达到门槛后再参与组合评分</small></label>
                  <label><span>最低组合评分</span><div><input id="config-combined-score" type="number" min="75" max="100" step="1" required><em>分</em></div><small>安全下限 75 分；仅作为影子准入门槛</small></label>
                  <label><span>最大实时行情延迟</span><div><input id="config-market-age" type="number" min="5" max="3600" step="1" required><em>秒</em></div><small>超过时只保留研究信号</small></label>
                  <label><span>最低资金流数据质量</span><div><input id="config-market-flow-quality" type="number" min="0" max="100" step="1" required><em>%</em></div><small>资金盘口权重启用时，低于门槛仅保留研究信号</small></label>
                  <label><span>预测因子最低质量</span><div><input id="config-feature-quality" type="number" min="0" max="100" step="1" required><em>%</em></div><small>仅在选择预测因子时生效</small></label>
                  <label><span>历史校准样本门槛</span><div><input id="config-calibration-samples" type="number" min="30" max="5000" step="10" required><em>条</em></div><small>默认按实盘研究标准要求 1,000 条</small></label>
                  <label><span>成本安全边际</span><div><input id="config-safety-margin" type="number" min="0" max="500" step="0.5" required><em>bps</em></div><small>毛优势置信下限还需额外覆盖该数值</small></label>
                </section>
                <section class="signal-safety-policy" aria-label="当前固定安全策略">
                  <header><div><span>ENTRY QUALITY POLICY</span><strong>新事件与行情质量双重准入</strong><small>旧新闻继续作为 AI 一周记忆，但不能自行重复触发预测。</small></div><b>固定启用</b></header>
                  <div><span><i>01</i><b>4 小时新事件</b><small>至少一条触发窗口内新闻</small></span><span><i>02</i><b>未消费新闻 ID</b><small>同一事件只准入一次</small></span><span><i>03</i><b>行情质量通过</b><small>实时价与已收盘 K 线新鲜</small></span><span><i>04</i><b>组合分 ≥ 75</b><small>低分仅保留研究候选</small></span></div>
                </section>
                <section id="weight-config" class="weight-config">
                  <header><div><strong>组合评分权重</strong><small>控制新闻、技术指标和资金盘口对机会组合评分的影响；合计必须为 100%。</small></div><span id="weight-total-state">合计 100%</span></header>
                  <div class="weight-grid">
                    <label class="news"><span><b>新闻评分</b><small>AI 置信度 × 股票关联度</small></span><div><input id="config-news-weight" class="score-weight-input" type="number" min="0" max="100" step="0.1" required><em>%</em></div></label>
                    <label class="technical"><span><b>技术指标</b><small>所选技术条件的连续方向强度</small></span><div><input id="config-technical-weight" class="score-weight-input" type="number" min="0" max="100" step="0.1" required><em>%</em></div></label>
                    <label class="market"><span><b>资金盘口</b><small>主力量比、交易深度与挂单增速</small></span><div><input id="config-market-flow-weight" class="score-weight-input" type="number" min="0" max="100" step="0.1" required><em>%</em></div></label>
                  </div>
                  <div class="weight-preview" aria-label="权重占比预览"><i class="news"></i><i class="technical"></i><i class="market"></i></div>
                  <footer><span><i class="news"></i>新闻</span><span><i class="technical"></i>技术指标</span><span><i class="market"></i>资金盘口</span><small>权重影响组合分；策略组门槛、最低技术强度和盘口强反向冲突独立生效。保存后从下一轮扫描生效，历史信号保留生成时权重。</small></footer>
                </section>
                <section class="symbol-block">
                  <header><div><strong>监控品种</strong><small>机会扫描只处理这里配置的美股合约；候选还需通过策略组、评分及资金冲突校验。</small></div><span id="symbol-count">正在读取</span></header>
                  <div class="symbol-mode">
                    <label><input id="config-all-symbols" type="checkbox"><span>扫描全部可用品种</span></label>
                    <div class="symbol-tools"><input id="symbol-search" type="search" aria-label="搜索监控品种" placeholder="搜索 AAPL / AAPLUSDT"><button id="symbols-visible" type="button">选择筛选结果</button><button id="symbols-clear" type="button">清空选择</button></div>
                  </div>
                  <div id="symbol-picker" class="symbol-picker"><div class="empty-state">正在读取可监控品种…</div></div>
                </section>
                <section id="indicator-config" class="indicator-block">
                  <header><div><strong>技术指标（多选）</strong><small>趋势、突破、回踩、反转按策略组择一确认；至少 2 项核心指标同向，盘口指标参与评分与冲突校验。</small></div><span id="indicator-count">已选 0 项</span></header>
                  <div id="indicator-templates" class="indicator-templates"></div>
                  <div id="indicator-conflict-warning" class="indicator-conflict-warning hidden"></div>
                  <div id="indicator-picker" class="indicator-picker"><div class="empty-state">正在读取指标目录…</div></div>
                </section>
                <div class="config-footer"><span id="config-saved-at">尚未保存用户配置</span><button class="save-config" type="submit">保存配置</button></div>
              </form>
            </section>
            <section id="view-opportunities" class="ai-view active">
              <div class="view-head"><div><span class="eyebrow">DISCOVERED OPPORTUNITIES</span><h2>发现机会</h2><p>先展示新闻识别出的美股候选，再按策略组、技术强度、组合评分及资金冲突生成预测。</p></div></div>
              <nav class="opportunity-tabs" role="tablist" aria-label="机会记录范围">
                <button class="active" type="button" role="tab" aria-selected="true" data-opportunity-tab="current"><span>当前机会</span><small id="current-direction-counts" class="direction-counts"><b class="long">多 --</b><i>/</i><b class="short">空 --</b></small></button>
                <button type="button" role="tab" aria-selected="false" data-opportunity-tab="history"><span>历史机会</span><small id="history-direction-counts" class="direction-counts"><b class="long">多 --</b><i>/</i><b class="short">空 --</b></small></button>
              </nav>
              <nav id="opportunity-status-tabs" class="opportunity-status-tabs" role="tablist" aria-label="当前机会触发状态">
                <button class="active" type="button" role="tab" aria-selected="true" data-opportunity-status="all"><span>全部</span><b id="opportunity-status-all-count">0</b></button>
                <button type="button" role="tab" aria-selected="false" data-opportunity-status="triggered"><span>已触发</span><b id="opportunity-status-triggered-count">0</b></button>
                <button type="button" role="tab" aria-selected="false" data-opportunity-status="ready"><span>条件满足</span><b id="opportunity-status-ready-count">0</b></button>
                <button type="button" role="tab" aria-selected="false" data-opportunity-status="waiting"><span>待触发</span><b id="opportunity-status-waiting-count">0</b></button>
                <button type="button" role="tab" aria-selected="false" data-opportunity-status="failed"><span>触发异常</span><b id="opportunity-status-failed-count">0</b></button>
              </nav>
              <div id="opportunity-list" class="opportunity-list"><div class="empty-state">暂无符合新闻条件的美股候选</div></div>
            </section>
            <section id="view-predictions" class="ai-view">
              <div class="view-head"><div><span class="eyebrow">OPPORTUNITY ANALYTICS</span><h2>预测统计分析</h2><p id="prediction-note">按止盈、止损、综合评分退出和最大持有期退出统计预测表现。</p></div></div>
              <section id="strategy-readiness" class="strategy-readiness"><div class="analytics-loading">正在评估实盘准备门槛…</div></section>
              <section id="adaptive-exit-policy" class="adaptive-exit-policy"><div class="analytics-loading">正在读取退出保护策略…</div></section>
              <div class="analytics-control-grid">
                <form id="prediction-filter-form" class="analytics-filters">
                  <header><div><strong>筛选统计</strong><small>汇总指标和下方明细使用相同条件</small></div><span id="prediction-filter-result">全部历史样本</span></header>
                  <div class="analytics-filter-grid">
                    <label><span>新闻评分不低于</span><div><input id="prediction-news-score-min" type="number" min="0" max="100" step="1" value="0"><em>分</em></div></label>
                    <label><span>指标评分不低于</span><div><input id="prediction-indicator-score-min" type="number" min="0" max="100" step="1" value="0"><em>分</em></div></label>
                    <label><span>方向筛选</span><select id="prediction-direction"><option value="all">全部方向</option><option value="long">只看做多</option><option value="short">只看做空</option></select></label>
                    <div class="analytics-filter-actions"><button id="prediction-filter-reset" type="button">重置</button><button id="prediction-filter-apply" class="primary" type="submit">应用筛选</button></div>
                  </div>
                </form>
                <form id="prediction-cost-form" class="analytics-cost-config">
                  <header><div><strong>成本计算</strong><small>可独立启用，净收益与准备度即时重算</small></div><span id="prediction-cost-total">1h 往返 -- bps</span></header>
                  <div class="analytics-cost-grid">
                    <label><input id="prediction-fee-enabled" type="checkbox"><span><b>手续费</b><small>单边</small></span><div><input id="prediction-fee-bps" type="number" min="0" max="500" step="0.1" value="5"><em>bps</em></div></label>
                    <label><input id="prediction-slippage-enabled" type="checkbox"><span><b>滑点</b><small>单边</small></span><div><input id="prediction-slippage-bps" type="number" min="0" max="500" step="0.1" value="3"><em>bps</em></div></label>
                    <label><input id="prediction-funding-enabled" type="checkbox"><span><b>资金成本</b><small>每 8h</small></span><div><input id="prediction-funding-bps" type="number" min="0" max="500" step="0.1" value="1"><em>bps</em></div></label>
                    <button id="prediction-cost-apply" class="primary" type="submit">保存并重算</button>
                  </div>
                </form>
              </div>
              <section id="analytics-summary" class="analytics-summary" aria-label="历史机会命中统计"><div class="analytics-loading">正在计算历史机会表现…</div></section>
              <div id="prediction-list" class="prediction-list"><div class="empty-state">正在读取历史机会…</div></div>
            </section>
          </main>
        </div>
      </div>
      <contract-monitor id="opportunity-research" research-only aria-label="股票合约 K 线研究弹窗"></contract-monitor>
      <div id="score-trend-modal" class="score-trend-modal hidden" aria-hidden="true">
        <button class="score-trend-backdrop" type="button" data-score-trend-close aria-label="关闭评分走势"></button>
        <section class="score-trend-dialog" role="dialog" aria-modal="true" aria-labelledby="score-trend-title">
          <header class="score-trend-head">
            <div><span class="eyebrow">LIVE SCORE HISTORY</span><h2 id="score-trend-title">组合评分走势</h2><p id="score-trend-subtitle">展示每次机会扫描保存的评分变化。</p></div>
            <button id="score-trend-close" class="ai-conclusion-close" type="button" data-score-trend-close aria-label="关闭评分走势">×</button>
          </header>
          <div id="score-trend-body" class="score-trend-body"></div>
          <footer class="score-trend-foot"><span>当前机会评分随扫描更新</span><strong>预测入场评分保持冻结</strong></footer>
        </section>
      </div>
      <div id="ai-conclusion-modal" class="ai-conclusion-modal hidden" aria-hidden="true">
        <button class="ai-conclusion-backdrop" type="button" data-conclusion-close aria-label="关闭 AI 分析结论"></button>
        <section class="ai-conclusion-dialog" role="dialog" aria-modal="true" aria-labelledby="ai-conclusion-title">
          <nav class="ai-conclusion-nav" role="tablist" aria-label="机会分析详情">
            <button type="button" role="tab" aria-selected="false" data-conclusion-view="fundamentals"><span>01</span><strong>基本面信息</strong><small id="ai-conclusion-fundamental-state">正在读取</small></button>
            <button type="button" role="tab" aria-selected="false" data-conclusion-view="news"><span>02</span><strong>相关新闻列表</strong><small id="ai-conclusion-news-count">正在读取</small></button>
            <button type="button" role="tab" aria-selected="false" data-conclusion-view="memory"><span>03</span><strong>AI新闻分析记录</strong><small id="ai-conclusion-memory-count">一周记忆</small></button>
            <button type="button" role="tab" aria-selected="false" data-conclusion-view="market"><span>04</span><strong>资金盘口指标</strong><small id="ai-conclusion-market-state">信号快照</small></button>
            <button class="active" type="button" role="tab" aria-selected="true" data-conclusion-view="analysis"><span>05</span><strong>AI分析结论</strong><small>综合研判</small></button>
          </nav>
          <div class="ai-conclusion-main">
            <header class="ai-conclusion-head">
              <div><span class="eyebrow">AI ANALYSIS CONCLUSION</span><h2 id="ai-conclusion-title">AI 分析结论</h2><p id="ai-conclusion-subtitle">读取生成该机会时保存的新闻研判与技术指标证据。</p></div>
              <div class="ai-conclusion-head-actions">
                <button id="open-news-system-prompt" class="news-system-prompt-trigger" type="button">系统提示词配置</button>
                <button id="open-historical-judgment" class="historical-judgment-trigger" type="button">历史研判</button>
                <button id="open-news-logic" class="news-logic-trigger" type="button">新闻分析逻辑</button>
                <button id="ai-conclusion-close" class="ai-conclusion-close" type="button" data-conclusion-close aria-label="关闭">×</button>
              </div>
            </header>
            <div id="ai-conclusion-body" class="ai-conclusion-body"></div>
            <footer class="ai-conclusion-foot"><span>结论基于信号生成时的数据快照</span><strong>仅作预测研究，不会触发实盘交易</strong></footer>
          </div>
        </section>
        <div id="news-logic-modal" class="news-logic-modal hidden" aria-hidden="true">
          <button class="news-logic-backdrop" type="button" data-news-logic-close aria-label="关闭新闻分析逻辑"></button>
          <section class="news-logic-dialog" role="dialog" aria-modal="true" aria-labelledby="news-logic-title">
            <header class="news-logic-head">
              <div><span class="eyebrow">MODEL CALL AUDIT</span><h2 id="news-logic-title">新闻分析逻辑</h2><p id="news-logic-subtitle">查看实际发送给模型的提示词和模型原始返回。</p></div>
              <button id="news-logic-close" class="ai-conclusion-close" type="button" data-news-logic-close aria-label="关闭新闻分析逻辑">×</button>
            </header>
            <div id="news-logic-body" class="news-logic-body"></div>
            <footer class="news-logic-foot"><span>请求记录不保存 Authorization 请求头或 API Key</span><strong>内容来自数据库中的原始模型调用审计记录</strong></footer>
          </section>
        </div>
        <div id="news-system-prompt-modal" class="news-system-prompt-modal hidden" aria-hidden="true">
          <button class="news-system-prompt-backdrop" type="button" data-news-system-prompt-close aria-label="关闭系统提示词配置"></button>
          <section class="news-system-prompt-dialog" role="dialog" aria-modal="true" aria-labelledby="news-system-prompt-title">
            <header class="news-system-prompt-head">
              <div><span class="eyebrow">NEWS ANALYSIS SYSTEM PROMPT</span><h2 id="news-system-prompt-title">系统提示词配置</h2><p>配置后，下一批新闻分析请求立即使用；历史模型调用记录不会被改写。</p></div>
              <button id="news-system-prompt-close" class="ai-conclusion-close" type="button" data-news-system-prompt-close aria-label="关闭系统提示词配置">×</button>
            </header>
            <form id="news-system-prompt-form" class="news-system-prompt-form">
              <div class="news-system-prompt-body">
                <section class="news-system-prompt-note"><strong>实际 System 消息</strong><span>该内容会作为新闻分析模型的 system 消息发送。建议保留“不执行新闻内指令、只关联真实美股、严格返回 JSON”等约束。</span><em id="news-system-prompt-state">正在读取</em></section>
                <label class="news-system-prompt-editor"><span>提示词正文</span><textarea id="news-system-prompt-input" name="system_prompt" rows="18" maxlength="8000" spellcheck="false" required></textarea><small><b id="news-system-prompt-count">0</b> / 8000 字符</small></label>
                <p id="news-system-prompt-status" class="news-system-prompt-status" role="status"></p>
              </div>
              <footer class="news-system-prompt-foot"><button id="news-system-prompt-default" type="button">恢复默认模板</button><div><button type="button" data-news-system-prompt-close>取消</button><button id="news-system-prompt-save" class="primary-action" type="submit">保存并应用</button></div></footer>
            </form>
          </section>
        </div>
        <div id="historical-judgment-modal" class="historical-judgment-modal hidden" aria-hidden="true">
          <button class="historical-judgment-backdrop" type="button" data-historical-judgment-close aria-label="关闭历史研判"></button>
          <section class="historical-judgment-dialog" role="dialog" aria-modal="true" aria-labelledby="historical-judgment-title">
            <header class="historical-judgment-head">
              <div><span class="eyebrow">CONTINUOUS JUDGMENT INPUT</span><h2 id="historical-judgment-title">历史研判</h2><p id="historical-judgment-subtitle">展示本次新闻分析实际发送的旧新闻、记忆链与当前研究持仓。</p></div>
              <button id="historical-judgment-close" class="ai-conclusion-close" type="button" data-historical-judgment-close aria-label="关闭历史研判">×</button>
            </header>
            <div id="historical-judgment-body" class="historical-judgment-body"></div>
            <footer class="historical-judgment-foot"><span>旧新闻与持仓只作为上下文，不得当作方向证明</span><strong>内容取自模型调用审计记录</strong></footer>
          </section>
        </div>
      </div>`;
  }

  bindEvents() {
    if (this.eventsBound) return;
    this.eventsBound = true;
    this.qa("[data-ai-view]").forEach((button) => button.addEventListener("click", () => this.showView(button.dataset.aiView)));
    this.q("#ai-refresh").addEventListener("click", () => this.loadAll(true));
    this.q("#run-news").addEventListener("click", () => this.createRun("news"));
    this.q("#run-opportunity").addEventListener("click", () => this.createRun("opportunity"));
    this.q("#open-news-config").addEventListener("click", () => this.openConfig("news"));
    this.q("#open-weight-config").addEventListener("click", () => this.openConfig("weights"));
    this.q("#open-config").addEventListener("click", () => this.openConfig("indicators"));
    this.q("#ai-config-form").addEventListener("submit", (event) => this.saveConfig(event));
    this.q("#config-enabled").addEventListener("change", (event) => this.renderEnabledLabel(event.target.checked));
    this.qa(".score-weight-input").forEach((input) => input.addEventListener("input", () => this.updateScoreWeightPreview()));
    this.q("#indicator-picker").addEventListener("change", () => this.updateIndicatorCount());
    this.q("#indicator-templates").addEventListener("click", (event) => this.applyIndicatorTemplate(event));
    this.q("#config-all-symbols").addEventListener("change", () => this.renderSymbolPicker());
    this.q("#symbol-search").addEventListener("input", (event) => { this.state.symbolSearch = event.target.value.trim().toUpperCase(); this.renderSymbolPicker(); });
    this.q("#symbol-picker").addEventListener("change", (event) => this.updateDraftSymbol(event));
    this.q("#symbols-visible").addEventListener("click", () => this.selectVisibleSymbols());
    this.q("#symbols-clear").addEventListener("click", () => { this.q("#config-all-symbols").checked = false; this.state.draftSymbols.clear(); this.renderSymbolPicker(); });
    this.q("#news-search").addEventListener("input", (event) => { this.state.newsSearch = event.target.value.trim().toLowerCase(); this.renderNews(); });
    this.q("#news-mode").addEventListener("change", (event) => { this.state.newsMode = event.target.value; this.renderNews(); });
    this.q("#prediction-filter-form").addEventListener("submit", (event) => { event.preventDefault(); this.applyPredictionFilters(); });
    this.q("#prediction-filter-reset").addEventListener("click", () => this.resetPredictionFilters());
    this.q("#prediction-list").addEventListener("click", (event) => {
      const button = event.target.closest("[data-prediction-page]");
      if (!button || button.disabled) return;
      this.setPredictionPage(Number(button.dataset.predictionPage));
    });
    this.q("#prediction-cost-form").addEventListener("submit", (event) => this.savePredictionCostConfig(event));
    this.qa('#prediction-cost-form input[type="checkbox"]').forEach((input) => input.addEventListener("change", () => this.updatePredictionCostControls()));
    this.qa('#prediction-cost-form input[type="number"]').forEach((input) => input.addEventListener("input", () => this.updatePredictionCostControls()));
    this.qa("[data-opportunity-tab]").forEach((button) => button.addEventListener("click", () => this.setOpportunityTab(button.dataset.opportunityTab)));
    this.qa("[data-opportunity-status]").forEach((button) => button.addEventListener("click", () => this.setOpportunityStatusFilter(button.dataset.opportunityStatus)));
    this.q("#opportunity-list").addEventListener("click", (event) => {
      const scoreButton = event.target.closest("[data-score-trend]");
      if (scoreButton) {
        this.openScoreTrend(scoreButton.dataset.scoreTrend, scoreButton);
        return;
      }
      const conclusionButton = event.target.closest("[data-ai-conclusion]");
      if (conclusionButton) {
        this.openAiConclusion(conclusionButton.dataset.aiConclusion, conclusionButton);
        return;
      }
      const button = event.target.closest("[data-open-contract]");
      if (!button || button.disabled) return;
      const research = this.q("#opportunity-research");
      if (typeof research?.openResearch !== "function") {
        this.showBanner("合约 K 线组件尚未加载，请刷新页面后重试。", "error");
        return;
      }
      const opportunity = this.state.opportunities.find((item) => item.id === button.dataset.opportunityId);
      const evidence = opportunity?.evidence || {};
      research.openResearch(button.dataset.openContract, button.dataset.timeframe || "1h", opportunity ? {
        id: opportunity.id,
        direction: opportunity.direction,
        combined_score: opportunity.combined_score,
        news_score: opportunity.news_score,
        indicator_score: opportunity.indicator_score,
        signal_time: opportunity.discovered_at,
        expires_at: opportunity.expires_at,
        entry_price: evidence.market?.price,
        technical_confirmed: evidence.confirmed === true,
        historical: this.state.opportunityTab === "history",
        outcome_result: opportunity.outcome?.result,
        exit_price: opportunity.outcome?.exit_price,
        directional_return_bps: opportunity.outcome?.directional_return_bps,
        settled_price_at: opportunity.outcome?.settled_price_at,
      } : null);
    });
    this.qa("[data-score-trend-close]").forEach((button) => button.addEventListener("click", () => this.closeScoreTrend()));
    this.qa("[data-conclusion-close]").forEach((button) => button.addEventListener("click", () => this.closeAiConclusion()));
    this.qa("[data-conclusion-view]").forEach((button) => button.addEventListener("click", () => this.showAiConclusionView(button.dataset.conclusionView)));
    this.q("#open-news-logic").addEventListener("click", (event) => this.openNewsAnalysisLogic(event.currentTarget));
    this.q("#open-news-system-prompt").addEventListener("click", (event) => this.openNewsSystemPrompt(event.currentTarget));
    this.q("#open-historical-judgment").addEventListener("click", (event) => this.openHistoricalJudgment(event.currentTarget));
    this.qa("[data-news-logic-close]").forEach((button) => button.addEventListener("click", () => this.closeNewsAnalysisLogic()));
    this.qa("[data-news-system-prompt-close]").forEach((button) => button.addEventListener("click", () => this.closeNewsSystemPrompt()));
    this.qa("[data-historical-judgment-close]").forEach((button) => button.addEventListener("click", () => this.closeHistoricalJudgment()));
    this.q("#news-system-prompt-form").addEventListener("submit", (event) => this.saveNewsSystemPrompt(event));
    this.q("#news-system-prompt-input").addEventListener("input", () => this.updateNewsSystemPromptCount());
    this.q("#news-system-prompt-default").addEventListener("click", () => this.restoreDefaultNewsSystemPrompt());
    this.q("#news-logic-body").addEventListener("click", (event) => {
      const callButton = event.target.closest("[data-news-call-index]");
      if (callButton) {
        this.newsModelCallIndex = Number(callButton.dataset.newsCallIndex || 0);
        this.renderNewsAnalysisLogic();
        return;
      }
      const copyButton = event.target.closest("[data-copy-call-section]");
      if (copyButton) this.copyNewsModelCallSection(copyButton.dataset.copyCallSection, copyButton);
    });
    this.shadowRoot.addEventListener("keydown", (event) => {
      if (event.key !== "Escape") return;
      if (!this.q("#score-trend-modal").classList.contains("hidden")) {
        this.closeScoreTrend();
      } else if (!this.q("#historical-judgment-modal").classList.contains("hidden")) {
        this.closeHistoricalJudgment();
      } else if (!this.q("#news-system-prompt-modal").classList.contains("hidden")) {
        this.closeNewsSystemPrompt();
      } else if (!this.q("#news-logic-modal").classList.contains("hidden")) {
        this.closeNewsAnalysisLogic();
      } else if (!this.q("#ai-conclusion-modal").classList.contains("hidden")) {
        this.closeAiConclusion();
      }
    });
    this.q("#news-stream").addEventListener("mouseenter", () => { this.scrollPaused = true; });
    this.q("#news-stream").addEventListener("mouseleave", () => { this.scrollPaused = false; });
    this.q("#news-stream").addEventListener("click", (event) => {
      const button = event.target.closest("[data-analyze-news]");
      if (button) this.analyzeNewsItem(button.dataset.analyzeNews);
    });
  }

  start() {
    if (this.state.running) return;
    this.state.running = true;
    this.tickClock();
    this.loadAll();
    this.timers.push(window.setInterval(() => this.tickClock(), 1000));
    this.timers.push(window.setInterval(() => this.loadLiveState(), 20000));
    this.timers.push(window.setInterval(() => this.autoScrollNews(), 80));
  }

  pause() {
    this.state.running = false;
    this.timers.forEach((timer) => window.clearInterval(timer));
    this.timers = [];
  }

  q(selector) { return this.shadowRoot.querySelector(selector); }
  qa(selector) { return [...this.shadowRoot.querySelectorAll(selector)]; }
  api(path = "", options = {}) { return window.quantdeskApi(`/api/v2/ai-monitor${path}`, options); }

  escape(value) {
    return String(value ?? "").replace(/[&<>'"]/g, (character) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" })[character]);
  }

  tickClock() {
    const target = this.q("#ai-clock");
    if (target) target.textContent = new Intl.DateTimeFormat("zh-CN", { hour12: false, hour: "2-digit", minute: "2-digit", second: "2-digit" }).format(new Date());
  }

  async loadAll(showSuccess = false) {
    if (this.state.fullLoadLoading) return;
    this.state.fullLoadLoading = true;
    this.q("#ai-refresh").disabled = true;
    try {
      const [overview, news, indicators, symbols, macroMarket] = await Promise.all([
        this.api("/overview"),
        this.api("/news?limit=160"),
        this.api("/indicators?timeframe=1h"),
        this.api("/symbols"),
        this.api("/market-context").catch(() => this.state.macroMarket || { available: false }),
      ]);
      this.state.overview = overview;
      this.state.config = overview.config;
      this.state.news = news.items || [];
      this.state.indicators = indicators.items || [];
      this.state.indicatorTemplates = indicators.templates || [];
      this.state.indicatorConflictPairs = indicators.conflict_pairs || [];
      this.state.symbols = symbols.items || [];
      this.state.macroMarket = macroMarket;
      this.renderOverview();
      this.renderMacroMarket();
      this.renderNews();
      this.renderConfig();
      await this.loadView(this.state.view);
      if (showSuccess) this.showBanner("发现机会数据已刷新。", "success");
    } catch (error) {
      this.showBanner(error.message || "发现机会数据读取失败", "error");
    } finally {
      this.state.fullLoadLoading = false;
      this.q("#ai-refresh").disabled = false;
    }
  }

  async loadLiveState() {
    if (!this.state.running || this.state.busyRun || this.state.fullLoadLoading || this.state.liveStateLoading || document.visibilityState === "hidden") return;
    this.state.liveStateLoading = true;
    try {
      const [overview, news, macroMarket] = await Promise.all([
        this.api("/overview"),
        this.api("/news?limit=160"),
        this.api("/market-context").catch(() => this.state.macroMarket || { available: false }),
      ]);
      this.state.overview = overview;
      this.state.config = overview.config;
      this.state.news = news.items || [];
      this.state.macroMarket = macroMarket;
      this.renderOverview();
      this.renderMacroMarket();
      this.renderNews();
      if (this.state.view === "runs") await this.loadRuns();
      if (this.state.view === "opportunities") await this.loadOpportunities();
      if (this.state.view === "predictions") await this.loadPredictionAnalytics();
    } catch (error) {
      this.showBanner(error.message || "自动刷新失败", "error");
    } finally {
      this.state.liveStateLoading = false;
    }
  }

  showView(view) {
    if (!["news", "runs", "config", "opportunities", "predictions"].includes(view)) return;
    this.state.view = view;
    this.qa("[data-ai-view]").forEach((button) => button.classList.toggle("active", button.dataset.aiView === view));
    this.qa(".ai-view").forEach((section) => section.classList.toggle("active", section.id === `view-${view}`));
    this.loadView(view);
  }

  openConfig(section) {
    this.showView("config");
    window.requestAnimationFrame(() => {
      const target = section === "news"
        ? this.q("#config-news-interval")
        : section === "weights"
        ? this.q("#weight-config")
        : this.q("#indicator-config");
      target?.scrollIntoView({ behavior: "smooth", block: "center" });
      if (section === "news") target?.focus();
      if (section === "weights") this.q("#config-news-weight")?.focus();
    });
  }

  async loadView(view) {
    if (view === "runs") return this.loadRuns();
    if (view === "opportunities") return this.loadOpportunities();
    if (view === "predictions") return this.loadPredictionAnalytics();
    return undefined;
  }

  renderOverview() {
    const data = this.state.overview || {};
    const config = data.config || {};
    const state = this.q("#scheduler-state");
    state.textContent = config.enabled ? "自动监控中" : "自动监控已暂停";
    state.className = `status-badge ${config.enabled ? "running" : "idle"}`;
    this.q("#model-warning").classList.toggle("hidden", Boolean(data.model_configured));
  }

  renderMacroMarket() {
    const target = this.q("#macro-market-panel");
    if (!target) return;
    const data = this.state.macroMarket || {};
    const numberOrDash = (value, digits = 2) => Number.isFinite(Number(value)) ? Number(value).toFixed(digits) : "--";
    const percent = (value) => Number.isFinite(Number(value)) ? `${Number(value) >= 0 ? "+" : ""}${Number(value).toFixed(2)}%` : "--";
    const tone = (value) => Number(value) > 0 ? "positive" : Number(value) < 0 ? "negative" : "flat";
    const sentiment = data.sentiment || {};
    const indices = Array.isArray(data.indices) ? data.indices : [];
    const sectors = Array.isArray(data.sectors) ? data.sectors : [];
    const assets = Array.isArray(data.macro_assets) ? data.macro_assets : [];
    const breadth = data.breadth || {};
    const vix = data.vix || {};
    const eventState = data.events || {};
    const nextEvent = eventState.next_event || null;
    const indexCards = indices.map((item) => `<article class="macro-index-card ${item.available ? "" : "unavailable"}">
      <header><strong>${this.escape(item.label || item.key)}</strong><small>${this.escape(item.provider_symbol || "--")} ${item.proxy ? "代理" : "指数"}</small></header>
      <b>${numberOrDash(item.price)}</b>
      <span class="${tone(item.change_percent)}">${percent(item.change_percent)}</span>
      <footer><span>日内 ${percent(item.intraday_change_percent)}</span><span>振幅 ${percent(item.amplitude_percent)}</span>${item.rsi_14_1h == null ? "" : `<span>RSI ${numberOrDash(item.rsi_14_1h, 1)}</span>`}</footer>
    </article>`).join("");
    const sectorPills = sectors.map((item) => `<span class="macro-sector ${tone(item.change_percent)}"><em>${this.escape(item.label || item.key)}</em><b>${percent(item.change_percent)}</b><small>${this.escape(item.provider_symbol || "--")} 代理</small></span>`).join("");
    const assetPills = assets.map((item) => `<span class="macro-asset ${tone(item.change_percent)}"><em>${this.escape(item.label || item.key)}</em><b>${numberOrDash(item.price)}</b><small>${percent(item.change_percent)} · ${this.escape(item.provider_symbol || "--")}</small></span>`).join("");
    const breadthRatio = Number.isFinite(Number(breadth.advance_decline_ratio)) ? Number(breadth.advance_decline_ratio).toFixed(2) : "--";
    const vixTone = Number(vix.value) >= 30 ? "danger" : Number(vix.value) >= 25 ? "warning" : "normal";
    const eventTone = eventState.risk_level === "critical" || eventState.risk_level === "high" ? "danger" : eventState.risk_level === "medium" ? "warning" : "normal";
    const eventCountdown = nextEvent && Number.isFinite(Number(nextEvent.hours_until))
      ? Number(nextEvent.hours_until) <= 24
        ? `${Math.max(0, Number(nextEvent.hours_until)).toFixed(1)} 小时后`
        : `${Math.ceil(Number(nextEvent.hours_until) / 24)} 天后`
      : "暂无临近事件";
    target.innerHTML = `<header class="macro-market-heading">
      <div><span>US MARKET REGIME</span><strong>宏观大盘环境</strong><small>${this.escape(data.source_note || "指数、波动率、市场宽度和事件风险")}</small></div>
      <div class="macro-regime ${this.escape(sentiment.key || "neutral")}"><span>环境结论</span><b>${this.escape(sentiment.label || "数据不足")}</b><small>${Number.isFinite(Number(sentiment.score)) ? `情绪 ${Number(sentiment.score).toFixed(0)} / 100` : "等待行情"}${data.stale ? " · 缓存数据" : ""}</small></div>
    </header>
    <div class="macro-market-body">
      <div class="macro-index-grid">${indexCards || '<div class="macro-empty">大盘实时行情暂不可用，个股评分不会应用宏观调整。</div>'}</div>
      <aside class="macro-risk-stack">
        <div class="macro-vix ${vixTone}"><span>VIX 恐慌指数</span><b>${numberOrDash(vix.value, 2)}</b><small>${vix.available ? `${percent(vix.change_percent)} · 真实指数` : "暂不可用"}</small></div>
        <div class="macro-breadth ${breadth.available ? "available" : "unavailable"}"><span>市场涨跌家数</span><b>${this.number(breadth.advancers)} <i>/</i> ${this.number(breadth.decliners)}</b><small>上涨 / 下跌 · A/D ${breadthRatio}${breadth.available ? "" : " · 样本不足"}</small></div>
        <div class="macro-event ${eventTone}"><span>宏观事件风险</span><b>${nextEvent ? this.escape(nextEvent.event_type) : "正常"}</b><small>${nextEvent ? `${eventCountdown} · ${this.escape(nextEvent.title)}` : "未来 24 小时无已登记重大事件"}</small></div>
      </aside>
    </div>
    <footer class="macro-market-footer"><div><span>板块热度</span>${sectorPills || "<small>暂无板块行情</small>"}</div><div><span>利率 / 美元代理</span>${assetPills || "<small>暂无宏观资产行情</small>"}</div></footer>`;
  }

  renderNews() {
    const search = this.state.newsSearch;
    const mode = this.state.newsMode;
    const items = this.state.news.filter((item) => {
      const related = (item.related_us_stocks || []).map((stock) => stock.symbol).join(" ");
      const industries = (item.related_industries || []).map((industry) => industry.name).join(" ");
      const haystack = `${item.title_zh || ""} ${item.title || ""} ${item.source || ""} ${industries} ${related}`.toLowerCase();
      if (search && !haystack.includes(search)) return false;
      if (mode === "analyzed" && !item.ai_analyzed_at) return false;
      if (mode === "pending" && item.ai_analyzed_at) return false;
      if (mode === "bull" && item.ai_sentiment !== "bull") return false;
      if (mode === "bear" && item.ai_sentiment !== "bear") return false;
      if (mode === "neutral" && item.ai_sentiment !== "neutral") return false;
      return true;
    });
    const target = this.q("#news-stream");
    const renderSignature = JSON.stringify([
      search,
      mode,
      [...this.state.analyzingNewsIds].sort(),
      items,
    ]);
    if (renderSignature === this.state.newsRenderSignature) return;
    const previousScrollTop = target.scrollTop;
    const hadPreviousRender = Boolean(this.state.newsRenderSignature);
    this.state.newsRenderSignature = renderSignature;
    if (!items.length) {
      target.innerHTML = '<div class="empty-state">没有符合当前筛选条件的新闻</div>';
      return;
    }
    target.innerHTML = items.map((item) => {
      const sentiment = item.ai_sentiment || item.sentiment || "pending";
      const analyzed = Boolean(item.ai_analyzed_at);
      const stocks = (item.related_us_stocks || []).slice(0, 6).map((stock) => `<span class="stock-chip ${this.escape(stock.direction || "neutral")}">${this.escape(stock.symbol)} <b>${Math.round(Number(stock.relevance || 0) * 100)}%</b></span>`).join("");
      const industries = (item.related_industries || []).slice(0, 5).map((industry) => `<span class="industry-chip ${this.escape(industry.direction || "neutral")}">${this.escape(industry.name)} <b>${Math.round(Number(industry.relevance || 0) * 100)}%</b></span>`).join("");
      const title = item.title_zh || item.title || "未命名新闻";
      const safeLink = this.safeUrl(item.link);
      const headline = safeLink ? `<a href="${this.escape(safeLink)}" target="_blank" rel="noopener noreferrer">${this.escape(title)}</a>` : `<strong>${this.escape(title)}</strong>`;
      const analyzing = this.state.analyzingNewsIds.has(item.id);
      return `<article class="news-item ${this.sentimentClass(sentiment)}">
        <time>${this.formatUnix(item.ts)}</time>
        <div class="news-source"><span>${this.escape(item.source || "未知来源")}</span>${analyzed ? `<em>AI 已分析</em>` : "<em class=\"pending\">等待批次</em>"}<small>${this.escape(item.lang || "--")}</small></div>
        <div class="news-body">${headline}
          <div class="news-analysis-line">${analyzed ? `<span class="sentiment-pill ${this.sentimentClass(sentiment)}">${this.sentimentLabel(sentiment)}</span><span>${this.impactLabel(item.ai_impact_strength)}</span><span>${this.horizonLabel(item.ai_time_horizon)}</span><span>${this.categoryLabel(item.ai_category)}</span>` : '<span class="sentiment-pill pending">每 15 分钟 · 最新 10 条</span>'}</div>
          <p>${this.escape(item.ai_reason || (analyzed ? "AI 未提供进一步说明" : "等待下一轮新闻分析；系统会优先处理数据库中最新的未分析新闻。"))}</p>
          <div class="news-relations"><div><em>行业</em><div class="industry-chips">${industries || `<span class="relation-empty">${analyzed ? "无直接关联行业" : "待分析"}</span>`}</div></div><div><em>美股</em><div class="stock-chips">${stocks || `<span class="relation-empty">${analyzed ? "无直接关联美股" : "待分析"}</span>`}</div></div></div>
        </div>
        <div class="news-score"><b>${item.ai_confidence == null ? "--" : `${Math.round(Number(item.ai_confidence) * 100)}%`}</b><small>${analyzed ? "AI 置信度" : "等待 AI"}</small><button class="news-analyze-action" type="button" data-analyze-news="${this.escape(item.id)}"${analyzing ? " disabled" : ""}>${analyzing ? "分析中…" : analyzed ? "重新分析" : "立即分析"}</button></div>
      </article>`;
    }).join("");
    if (hadPreviousRender) {
      target.scrollTop = Math.min(previousScrollTop, Math.max(0, target.scrollHeight - target.clientHeight));
    }
  }

  async analyzeNewsItem(newsId) {
    if (!newsId || this.state.analyzingNewsIds.has(newsId)) return;
    this.state.analyzingNewsIds.add(newsId);
    this.renderNews();
    try {
      const run = await this.api("/news/analyze", {
        method: "POST",
        body: JSON.stringify({ news_id: newsId }),
      });
      this.showBanner("该条新闻已提交 AI 分析，请稍候…", "success");
      const completed = await this.waitForRun(run.id);
      if (completed.status === "failed") throw new Error(completed.error_message || "该条新闻分析失败");
      const [news, overview] = await Promise.all([
        this.api("/news?limit=160"),
        this.api("/overview"),
      ]);
      this.state.news = news.items || [];
      this.state.overview = overview;
      this.state.config = overview.config;
      this.renderNews();
      this.renderOverview();
      await this.loadRuns();
      this.showBanner("该条新闻已完成 AI 分析。", "success");
    } catch (error) {
      this.showBanner(error.message || "该条新闻分析失败", "error");
    } finally {
      this.state.analyzingNewsIds.delete(newsId);
      this.renderNews();
    }
  }

  async waitForRun(runId, timeoutMilliseconds = 120000) {
    const deadline = Date.now() + timeoutMilliseconds;
    while (Date.now() < deadline) {
      await new Promise((resolve) => window.setTimeout(resolve, 1000));
      const data = await this.api("/runs?limit=100");
      this.state.runs = data.items || [];
      const run = this.state.runs.find((item) => item.id === runId);
      if (run && ["completed", "partial", "failed"].includes(run.status)) return run;
    }
    throw new Error("新闻分析等待超时，请到分析记录查看任务状态");
  }

  autoScrollNews() {
    if (!this.state.running || this.state.view !== "news" || this.scrollPaused) return;
    const target = this.q("#news-stream");
    if (!target || target.scrollHeight <= target.clientHeight + 2) return;
    target.scrollTop += 1;
    if (target.scrollTop + target.clientHeight >= target.scrollHeight - 2) target.scrollTop = 0;
  }

  async loadRuns() {
    try {
      const data = await this.api("/runs?limit=100");
      this.state.runs = data.items || [];
      this.renderRuns();
    } catch (error) {
      this.showBanner(error.message || "分析记录读取失败", "error");
    }
  }

  renderRuns() {
    const target = this.q("#run-list");
    if (!this.state.runs.length) { target.innerHTML = '<div class="empty-state">暂无分析记录；可使用页面右上角手动执行一次。</div>'; return; }
    target.innerHTML = this.state.runs.map((run) => {
      const summary = run.summary || {};
      const text = run.run_type === "news"
        ? summary.market_summary || "新闻语义分析正在等待结果"
        : `候选 ${summary.candidate_count ?? run.input_count} 个，新增机会 ${summary.discovered_count ?? run.matched_count} 个，重复输入 ${summary.duplicate_count ?? 0} 个。`;
      return `<article class="run-item ${this.escape(run.status)}">
        <div class="run-marker"><i></i><span>${this.runTypeLabel(run.run_type)}</span></div>
        <div class="run-main"><header><strong>${this.runTypeLabel(run.run_type)}</strong><em class="run-status">${this.statusLabel(run.status)}</em><time>${this.formatDate(run.created_at)}</time></header><p>${this.escape(run.error_message || text)}</p><footer><span>输入 ${this.number(run.input_count)}</span><span>完成 / 发现 ${this.number(run.matched_count)}</span>${summary.model_name ? `<span>模型 ${this.escape(summary.model_name)}</span>` : ""}${summary.timeframe ? `<span>周期 ${this.escape(summary.timeframe)}</span>` : ""}</footer></div>
      </article>`;
    }).join("");
  }

  renderConfig() {
    const config = this.state.config;
    if (!config) return;
    this.q("#config-enabled").checked = Boolean(config.enabled);
    this.renderEnabledLabel(Boolean(config.enabled));
    this.q("#config-news-interval").value = config.news_interval_minutes;
    this.q("#config-opportunity-interval").value = config.opportunity_interval_minutes;
    this.q("#config-lookback").value = config.news_lookback_hours;
    this.q("#config-timeframe").value = config.timeframe;
    this.q("#config-max-holding-bars").value = Number(config.prediction_max_holding_bars ?? 4);
    this.q("#config-confidence").value = Math.round(Number(config.minimum_news_confidence) * 100);
    this.q("#config-mentions").value = config.minimum_news_mentions;
    this.q("#config-indicator-score").value = Number(config.minimum_indicator_score ?? 65);
    this.q("#config-combined-score").value = Number(config.minimum_combined_score ?? 75);
    this.q("#config-market-age").value = Number(config.maximum_market_age_seconds ?? 120);
    this.q("#config-market-flow-quality").value = Math.round(Number(config.minimum_market_flow_quality ?? 0.5) * 100);
    this.q("#config-feature-quality").value = Math.round(Number(config.minimum_feature_quality ?? 0.7) * 100);
    this.q("#config-calibration-samples").value = Number(config.minimum_calibration_samples ?? 1000);
    this.q("#config-safety-margin").value = Number(config.live_safety_margin_bps ?? 10);
    this.q("#config-news-weight").value = Number(config.news_score_weight ?? 45);
    this.q("#config-technical-weight").value = Number(config.technical_score_weight ?? 35);
    this.q("#config-market-flow-weight").value = Number(config.market_flow_score_weight ?? 20);
    this.updateScoreWeightPreview();
    this.renderPredictionCostConfig(config);
    this.state.draftSymbols = new Set(config.monitor_symbols || []);
    this.q("#config-all-symbols").checked = this.state.draftSymbols.size === 0;
    this.renderSymbolPicker();
    const selected = new Set(config.indicator_keys || []);
    const groups = new Map();
    this.state.indicators.forEach((item) => {
      const key = `${item.source}:${item.category}`;
      if (!groups.has(key)) groups.set(key, { label: `${item.source === "prediction" ? "预测因子" : "K线策略"} · ${item.category}`, items: [] });
      groups.get(key).items.push(item);
    });
    this.q("#indicator-picker").innerHTML = [...groups.values()].map((group) => `<section><h3>${this.escape(group.label)}</h3>${group.items.map((item) => `<label class="indicator-option"><input type="checkbox" value="${this.escape(item.key)}"${selected.has(item.key) ? " checked" : ""}><span><strong>${this.escape(item.name)}</strong><small>${this.escape(item.description)}</small></span><i></i></label>`).join("")}</section>`).join("");
    this.q("#indicator-templates").innerHTML = `<span>推荐组合</span>${this.state.indicatorTemplates.map((template) => `<button type="button" data-template="${this.escape(template.key)}" title="${this.escape(template.description)}">${this.escape(template.name)}</button>`).join("")}`;
    this.updateIndicatorCount();
    this.q("#config-saved-at").textContent = config.persisted ? `上次保存 ${this.formatDate(config.updated_at)}` : "当前为安全默认值，保存后才启用周期任务";
  }

  renderPredictionCostConfig(config = this.state.config || {}) {
    this.q("#prediction-fee-enabled").checked = Boolean(config.prediction_fee_enabled ?? true);
    this.q("#prediction-fee-bps").value = Number(config.prediction_fee_bps_per_side ?? 5);
    this.q("#prediction-slippage-enabled").checked = Boolean(config.prediction_slippage_enabled ?? true);
    this.q("#prediction-slippage-bps").value = Number(config.prediction_slippage_bps_per_side ?? 3);
    this.q("#prediction-funding-enabled").checked = Boolean(config.prediction_funding_enabled ?? true);
    this.q("#prediction-funding-bps").value = Number(config.prediction_funding_bps_per_8h ?? 1);
    this.updatePredictionCostControls();
  }

  updatePredictionCostControls() {
    const parts = [
      ["fee", 2],
      ["slippage", 2],
      ["funding", 1 / 8],
    ];
    let oneHourCost = 0;
    parts.forEach(([key, multiplier]) => {
      const enabled = this.q(`#prediction-${key}-enabled`).checked;
      const input = this.q(`#prediction-${key}-bps`);
      input.disabled = !enabled;
      input.closest("label")?.classList.toggle("disabled", !enabled);
      if (enabled) oneHourCost += Math.max(0, Number(input.value) || 0) * multiplier;
    });
    this.q("#prediction-cost-total").textContent = `1h 往返 ${oneHourCost.toFixed(2)} bps`;
  }

  async savePredictionCostConfig(event) {
    event.preventDefault();
    const button = this.q("#prediction-cost-apply");
    button.disabled = true;
    button.textContent = "保存中…";
    try {
      const payload = {
        prediction_fee_enabled: this.q("#prediction-fee-enabled").checked,
        prediction_fee_bps_per_side: Math.max(0, Number(this.q("#prediction-fee-bps").value) || 0),
        prediction_slippage_enabled: this.q("#prediction-slippage-enabled").checked,
        prediction_slippage_bps_per_side: Math.max(0, Number(this.q("#prediction-slippage-bps").value) || 0),
        prediction_funding_enabled: this.q("#prediction-funding-enabled").checked,
        prediction_funding_bps_per_8h: Math.max(0, Number(this.q("#prediction-funding-bps").value) || 0),
      };
      this.state.config = await this.api("/cost-config", { method: "PUT", body: JSON.stringify(payload) });
      this.renderPredictionCostConfig(this.state.config);
      await this.loadPredictionAnalytics();
      this.showBanner("成本配置已保存；命中率、净收益和实盘准备度已按当前设置重算。", "success");
    } catch (error) {
      this.showBanner(error.message || "成本配置保存失败", "error");
    } finally {
      button.disabled = false;
      button.textContent = "保存并重算";
    }
  }

  renderEnabledLabel(enabled) {
    this.q("#config-enabled-label").textContent = enabled ? "已开启" : "已暂停";
  }

  scoreWeightValues() {
    return {
      news: Math.max(0, Number(this.q("#config-news-weight").value) || 0),
      technical: Math.max(0, Number(this.q("#config-technical-weight").value) || 0),
      market: Math.max(0, Number(this.q("#config-market-flow-weight").value) || 0),
    };
  }

  updateScoreWeightPreview() {
    const values = this.scoreWeightValues();
    const total = values.news + values.technical + values.market;
    const valid = Math.abs(total - 100) <= 0.01;
    const state = this.q("#weight-total-state");
    state.textContent = `合计 ${total.toFixed(1).replace(".0", "")}%${valid ? " · 可保存" : " · 需调整为 100%"}`;
    state.classList.toggle("invalid", !valid);
    const scale = total > 0 ? 100 / total : 0;
    this.q(".weight-preview .news").style.width = `${values.news * scale}%`;
    this.q(".weight-preview .technical").style.width = `${values.technical * scale}%`;
    this.q(".weight-preview .market").style.width = `${values.market * scale}%`;
    return valid;
  }

  updateIndicatorCount() {
    const selected = this.qa('#indicator-picker input[type="checkbox"]:checked').map((input) => input.value);
    const count = selected.length;
    this.q("#indicator-count").textContent = `已选 ${count} 项 · 分组确认`;
    const conflicts = this.indicatorSelectionConflicts(selected);
    const warning = this.q("#indicator-conflict-warning");
    warning.classList.toggle("hidden", conflicts.length === 0);
    warning.textContent = conflicts.length ? `当前组合存在互斥条件：${conflicts.map(([left, right]) => `${this.indicatorName(left)} / ${this.indicatorName(right)}`).join("；")}` : "";
  }

  indicatorName(key) {
    return this.state.indicators.find((item) => item.key === key)?.name || key;
  }

  indicatorSelectionConflicts(selected) {
    const keys = new Set(selected);
    return this.state.indicatorConflictPairs.filter(([left, right]) => keys.has(left) && keys.has(right));
  }

  applyIndicatorTemplate(event) {
    const button = event.target.closest("button[data-template]");
    if (!button) return;
    const template = this.state.indicatorTemplates.find((item) => item.key === button.dataset.template);
    if (!template) return;
    const selected = new Set(template.indicator_keys || []);
    this.qa('#indicator-picker input[type="checkbox"]').forEach((input) => { input.checked = selected.has(input.value); });
    this.updateIndicatorCount();
    this.showBanner(`已应用“${template.name}”组合；系统将按策略组门槛和加权评分确认。`, "success");
  }

  filteredSymbols() {
    const search = this.state.symbolSearch;
    return this.state.symbols.filter((item) => !search || `${item.symbol} ${item.contract_symbol}`.includes(search));
  }

  renderSymbolPicker() {
    const allSymbols = this.q("#config-all-symbols").checked;
    const items = this.filteredSymbols();
    const target = this.q("#symbol-picker");
    target.classList.toggle("all-selected", allSymbols);
    if (!items.length) {
      target.innerHTML = '<div class="empty-state">没有符合搜索条件的品种</div>';
    } else {
      target.innerHTML = items.map((item) => `<label class="symbol-option"><input type="checkbox" value="${this.escape(item.contract_symbol)}"${this.state.draftSymbols.has(item.contract_symbol) ? " checked" : ""}${allSymbols ? " disabled" : ""}><span><strong>${this.escape(item.symbol)}</strong><small>${this.escape(item.contract_symbol)}</small></span></label>`).join("");
    }
    const count = this.state.draftSymbols.size;
    this.q("#symbol-count").textContent = allSymbols ? `全部 ${this.state.symbols.length} 项` : `已选 ${count} / ${this.state.symbols.length} 项`;
  }

  updateDraftSymbol(event) {
    if (!event.target.matches('input[type="checkbox"]')) return;
    if (event.target.checked) this.state.draftSymbols.add(event.target.value);
    else this.state.draftSymbols.delete(event.target.value);
    this.renderSymbolPicker();
  }

  selectVisibleSymbols() {
    this.q("#config-all-symbols").checked = false;
    this.filteredSymbols().forEach((item) => this.state.draftSymbols.add(item.contract_symbol));
    this.renderSymbolPicker();
  }

  async saveConfig(event) {
    event.preventDefault();
    if (!this.updateScoreWeightPreview()) { this.showBanner("新闻、技术指标与资金盘口权重合计必须为 100%。", "error"); this.q("#weight-config")?.scrollIntoView({ behavior: "smooth", block: "center" }); return; }
    const scoreWeights = this.scoreWeightValues();
    const indicatorKeys = this.qa('#indicator-picker input[type="checkbox"]:checked').map((input) => input.value);
    if (!indicatorKeys.length) { this.showBanner("请至少选择一个技术指标。", "error"); return; }
    if (this.indicatorSelectionConflicts(indicatorKeys).length) { this.showBanner("当前指标组合存在无法同时满足的互斥条件，请改用推荐组合或取消冲突指标。", "error"); return; }
    const allSymbols = this.q("#config-all-symbols").checked;
    const monitorSymbols = allSymbols ? [] : [...this.state.draftSymbols];
    if (!allSymbols && !monitorSymbols.length) { this.showBanner("请至少选择一个监控品种，或开启扫描全部品种。", "error"); return; }
    const button = this.q(".save-config");
    button.disabled = true;
    button.textContent = "保存中…";
    try {
      const payload = {
        enabled: this.q("#config-enabled").checked,
        news_interval_minutes: Number(this.q("#config-news-interval").value),
        opportunity_interval_minutes: Number(this.q("#config-opportunity-interval").value),
        news_lookback_hours: Number(this.q("#config-lookback").value),
        timeframe: this.q("#config-timeframe").value,
        prediction_max_holding_bars: Number(this.q("#config-max-holding-bars").value),
        indicator_keys: indicatorKeys,
        monitor_symbols: monitorSymbols,
        minimum_news_confidence: Number(this.q("#config-confidence").value) / 100,
        minimum_news_mentions: Number(this.q("#config-mentions").value),
        minimum_indicator_score: Number(this.q("#config-indicator-score").value),
        minimum_combined_score: Number(this.q("#config-combined-score").value),
        maximum_market_age_seconds: Number(this.q("#config-market-age").value),
        minimum_market_flow_quality: Number(this.q("#config-market-flow-quality").value) / 100,
        minimum_feature_quality: Number(this.q("#config-feature-quality").value) / 100,
        minimum_calibration_samples: Number(this.q("#config-calibration-samples").value),
        live_safety_margin_bps: Number(this.q("#config-safety-margin").value),
        news_score_weight: scoreWeights.news,
        technical_score_weight: scoreWeights.technical,
        market_flow_score_weight: scoreWeights.market,
      };
      this.state.config = await this.api("/config", { method: "PUT", body: JSON.stringify(payload) });
      this.renderConfig();
      await this.loadOverviewOnly();
      const scope = monitorSymbols.length ? `${monitorSymbols.length} 个监控品种` : "全部可用品种";
      this.showBanner(`配置已保存；${scope}，${indicatorKeys.length} 个指标采用策略组确认规则。`, "success");
    } catch (error) {
      this.showBanner(error.message || "配置保存失败", "error");
    } finally {
      button.disabled = false;
      button.textContent = "保存配置";
    }
  }

  async loadOverviewOnly() {
    this.state.overview = await this.api("/overview");
    this.state.config = this.state.overview.config;
    this.renderOverview();
  }

  async createRun(runType) {
    if (this.state.busyRun) return;
    this.state.busyRun = runType;
    const button = this.q(runType === "news" ? "#run-news" : "#run-opportunity");
    const original = button.textContent;
    button.disabled = true;
    button.textContent = runType === "news" ? "正在提交…" : "正在扫描…";
    try {
      await this.api("/runs", { method: "POST", body: JSON.stringify({ run_type: runType }) });
      this.showBanner(runType === "news" ? "新闻 AI 分析任务已启动，可在“分析记录”查看进度。" : "机会扫描任务已启动，可在“发现机会”查看结果。", "success");
      await this.loadOverviewOnly();
      await this.loadRuns();
      if (runType === "opportunity") await this.loadOpportunities();
    } catch (error) {
      this.showBanner(error.message || "任务启动失败", "error");
    } finally {
      this.state.busyRun = "";
      button.disabled = false;
      button.textContent = original;
    }
  }

  async loadOpportunities({ showLoading = false } = {}) {
    const tab = this.state.opportunityTab;
    if (this.state.opportunitiesLoading && this.state.opportunityLoadingTab === tab) return;
    const requestId = ++this.state.opportunityRequestId;
    const target = this.q("#opportunity-list");
    const switchingTab = this.state.opportunitiesLoadedTab && this.state.opportunitiesLoadedTab !== tab;
    const firstLoad = !this.state.opportunitiesLoadedTab;
    this.state.opportunitiesLoading = true;
    this.state.opportunityLoadingTab = tab;
    target.classList.add("is-refreshing");
    target.setAttribute("aria-busy", "true");
    if (showLoading || switchingTab || firstLoad) {
      target.innerHTML = `<div class="empty-state opportunity-empty"><strong>正在读取${tab === "history" ? "历史" : "当前"}机会…</strong></div>`;
    }
    try {
      const data = await this.api("/opportunities?limit=300&include_expired=true");
      if (requestId !== this.state.opportunityRequestId || tab !== this.state.opportunityTab) return;
      const now = Date.now();
      const bySignalTimeDesc = (left, right) => {
        const leftTime = this.parseDate(left.discovered_at).getTime();
        const rightTime = this.parseDate(right.discovered_at).getTime();
        const timeDifference = (Number.isFinite(rightTime) ? rightTime : 0) - (Number.isFinite(leftTime) ? leftTime : 0);
        return timeDifference || String(right.id || "").localeCompare(String(left.id || ""));
      };
      const items = [...(data.items || [])].sort(bySignalTimeDesc);
      const isActive = (item) => {
        const active = ["candidate", "discovered"].includes(item.status) && this.parseDate(item.expires_at).getTime() > now;
        return active;
      };
      const isAwaitingSettlement = (item) => ["pending", "unavailable"].includes(String(item.prediction_status || ""));
      const currentItems = items.filter((item) => isActive(item) && item.prediction_status !== "completed").sort(bySignalTimeDesc);
      const historyItems = items.filter(isAwaitingSettlement).sort(bySignalTimeDesc);
      const visible = (tab === "history" ? historyItems : currentItems)
        .map((item) => ({ ...item, outcome: null }));
      if (tab === "current") {
        const unique = new Map();
        visible.forEach((item) => {
          const instrument = String(item.contract_symbol || item.symbol || "").trim().toUpperCase();
          if (!unique.has(instrument)) unique.set(instrument, item);
        });
        this.state.opportunities = [...unique.values()];
      } else {
        this.state.opportunities = visible;
      }
      const countDirections = (records) => records.reduce((counts, item) => {
        if (item.direction === "short") counts.short += 1;
        else counts.long += 1;
        return counts;
      }, { long: 0, short: 0 });
      const uniqueCurrent = new Map();
      currentItems.forEach((item) => {
        const instrument = String(item.contract_symbol || item.symbol || "").trim().toUpperCase();
        if (!uniqueCurrent.has(instrument)) uniqueCurrent.set(instrument, item);
      });
      const uniqueCurrentItems = [...uniqueCurrent.values()];
      this.state.opportunityDirectionCounts = countDirections(uniqueCurrentItems);
      this.state.historyOpportunityDirectionCounts = countDirections(historyItems);
      this.state.historyOpportunitySettlementCounts = historyItems.reduce((counts, item) => {
        counts.total += 1;
        if (item.prediction_status === "pending") counts.pending += 1;
        if (item.prediction_status === "unavailable") counts.unavailable += 1;
        return counts;
      }, { total: 0, pending: 0, unavailable: 0 });
      this.state.opportunityStatusCounts = uniqueCurrentItems.reduce((counts, item) => {
        const status = this.virtualEntryState(item, this.virtualEntryGate(item)).tone;
        counts.all += 1;
        counts[status] += 1;
        return counts;
      }, { all: 0, triggered: 0, ready: 0, waiting: 0, failed: 0 });
      this.renderOpportunityDirectionCounts();
      this.renderOpportunityStatusCounts();
      this.renderOpportunities();
      this.state.opportunitiesLoadedTab = tab;
    } catch (error) {
      if (requestId !== this.state.opportunityRequestId) return;
      this.showBanner(error.message || "发现机会读取失败", "error");
      if (!this.state.opportunitiesLoadedTab) {
        target.innerHTML = '<div class="empty-state opportunity-empty"><strong>机会读取失败</strong><span>保留页面后重试，不会触发任何交易。</span></div>';
      }
    } finally {
      if (requestId === this.state.opportunityRequestId) {
        this.state.opportunitiesLoading = false;
        this.state.opportunityLoadingTab = "";
        target.classList.remove("is-refreshing");
        target.removeAttribute("aria-busy");
      }
    }
  }

  renderOpportunityDirectionCounts() {
    const groups = [
      ["#current-direction-counts", this.state.opportunityDirectionCounts, "当前机会"],
      ["#history-direction-counts", this.state.historyOpportunityDirectionCounts, "历史机会"],
    ];
    groups.forEach(([selector, counts, label]) => {
      const target = this.q(selector);
      if (!target) return;
      const settlementPrefix = selector === "#history-direction-counts"
        ? `<b class="pending">未完成 ${this.number(this.state.historyOpportunitySettlementCounts.total)}</b><i>·</i>`
        : "";
      target.innerHTML = `${settlementPrefix}<b class="long">多 ${this.number(counts.long)}</b><i>/</i><b class="short">空 ${this.number(counts.short)}</b>`;
      target.setAttribute("aria-label", `${label}：做多 ${counts.long} 次，做空 ${counts.short} 次`);
    });
  }

  setOpportunityTab(tab) {
    if (!["current", "history"].includes(tab) || tab === this.state.opportunityTab) return;
    this.state.opportunityTab = tab;
    this.qa("[data-opportunity-tab]").forEach((button) => {
      const active = button.dataset.opportunityTab === tab;
      button.classList.toggle("active", active);
      button.setAttribute("aria-selected", String(active));
    });
    this.q("#opportunity-status-tabs").classList.toggle("hidden", tab === "history");
    this.loadOpportunities({ showLoading: true });
  }

  setOpportunityStatusFilter(status) {
    if (this.state.opportunityTab !== "current") return;
    const allowed = ["all", "triggered", "ready", "waiting", "failed"];
    if (!allowed.includes(status) || status === this.state.opportunityStatusFilter) return;
    this.state.opportunityStatusFilter = status;
    this.qa("[data-opportunity-status]").forEach((button) => {
      const active = button.dataset.opportunityStatus === status;
      button.classList.toggle("active", active);
      button.setAttribute("aria-selected", String(active));
    });
    this.renderOpportunities();
  }

  renderOpportunityStatusCounts() {
    const counts = this.state.opportunityStatusCounts;
    ["all", "triggered", "ready", "waiting", "failed"].forEach((status) => {
      const target = this.q(`#opportunity-status-${status}-count`);
      if (target) target.textContent = this.number(counts[status]);
    });
  }

  opportunityScoreHistory(item) {
    const evidence = item?.evidence || {};
    const raw = Array.isArray(evidence.score_history) ? evidence.score_history : [];
    const history = [];
    const entryPoint = {
      calculated_at: item?.prediction_created_at,
      news: Number(item?.prediction_news_score),
      technical: Number(item?.prediction_indicator_score),
      market_flow: Number(item?.prediction_market_flow_score),
      combined: Number(item?.prediction_combined_score),
    };
    if (entryPoint.calculated_at && [entryPoint.news, entryPoint.technical, entryPoint.market_flow, entryPoint.combined].every(Number.isFinite)) {
      history.push(entryPoint);
    }
    history.push(...raw.map((point) => ({
      calculated_at: point?.calculated_at,
      news: Number(point?.news),
      technical: Number(point?.technical),
      market_flow: Number(point?.market_flow),
      combined: Number(point?.combined),
    })).filter((point) => point.calculated_at && [point.news, point.technical, point.market_flow, point.combined].every(Number.isFinite)));
    const snapshot = evidence.score_snapshot || {};
    const fallback = {
      calculated_at: snapshot.calculated_at || item?.updated_at || item?.discovered_at,
      news: Number(snapshot.news ?? item?.news_score),
      technical: Number(snapshot.technical ?? item?.indicator_score),
      market_flow: Number(snapshot.market_flow ?? evidence.market_flow?.score ?? 50),
      combined: Number(snapshot.combined ?? item?.combined_score),
    };
    if (fallback.calculated_at && [fallback.news, fallback.technical, fallback.market_flow, fallback.combined].every(Number.isFinite)) history.push(fallback);
    const unique = new Map();
    history.forEach((point) => unique.set(String(point.calculated_at), point));
    return [...unique.values()].sort((left, right) => this.parseDate(left.calculated_at).getTime() - this.parseDate(right.calculated_at).getTime());
  }

  scoreTrendState(history) {
    const latest = history.at(-1);
    const previous = history.at(-2);
    const delta = latest && previous ? latest.combined - previous.combined : 0;
    const direction = delta > 0.05 ? "up" : delta < -0.05 ? "down" : "flat";
    return {
      latest,
      previous,
      delta,
      direction,
      arrow: direction === "up" ? "↑" : direction === "down" ? "↓" : "→",
      badge: previous ? `${delta > 0 ? "+" : ""}${delta.toFixed(1)}` : "首个点",
    };
  }

  renderScoreTrendChart(item, history) {
    if (!history.length) return '<div class="score-trend-empty">暂无评分历史，下一轮机会扫描后开始记录。</div>';
    const width = 920;
    const height = 330;
    const padding = { left: 48, right: 22, top: 20, bottom: 42 };
    const chartWidth = width - padding.left - padding.right;
    const chartHeight = height - padding.top - padding.bottom;
    const x = (index) => padding.left + (history.length === 1 ? chartWidth / 2 : chartWidth * index / (history.length - 1));
    const y = (value) => padding.top + chartHeight * (1 - Math.max(0, Math.min(100, Number(value))) / 100);
    const series = [
      { key: "combined", label: "组合评分", color: "#ad9cff" },
      { key: "news", label: "新闻评分", color: "#dfbd67" },
      { key: "technical", label: "技术指标", color: "#5bd6aa" },
      { key: "market_flow", label: "资金盘口", color: "#5dc4d8" },
    ];
    const grid = [0, 25, 50, 75, 100].map((value) => `<g><line x1="${padding.left}" y1="${y(value)}" x2="${width - padding.right}" y2="${y(value)}"></line><text x="${padding.left - 9}" y="${y(value) + 4}">${value}</text></g>`).join("");
    const threshold = Number(this.state.config?.minimum_combined_score ?? 75);
    const lines = series.map((definition) => {
      const points = history.map((point, index) => `${x(index).toFixed(2)},${y(point[definition.key]).toFixed(2)}`).join(" ");
      const dots = definition.key === "combined" ? history.map((point, index) => `<circle cx="${x(index).toFixed(2)}" cy="${y(point.combined).toFixed(2)}" r="3.5"><title>${this.escape(this.formatDate(point.calculated_at))} · ${point.combined.toFixed(1)}</title></circle>`).join("") : "";
      return `<g class="score-line ${definition.key}"><polyline points="${points}"></polyline>${dots}</g>`;
    }).join("");
    const timeLabels = history.length === 1
      ? [{ index: 0, anchor: "middle" }]
      : [{ index: 0, anchor: "start" }, { index: Math.floor((history.length - 1) / 2), anchor: "middle" }, { index: history.length - 1, anchor: "end" }];
    const axes = timeLabels.map(({ index, anchor }) => `<text class="score-time-label" x="${x(index)}" y="${height - 12}" text-anchor="${anchor}">${this.escape(this.formatDate(history[index].calculated_at))}</text>`).join("");
    const state = this.scoreTrendState(history);
    const values = history.map((point) => point.combined);
    const entryScore = item.prediction_combined_score == null ? null : Number(item.prediction_combined_score);
    const recentRows = history.slice(-12).reverse().map((point, index) => {
      const prior = history[history.length - 2 - index];
      const delta = prior ? point.combined - prior.combined : null;
      const deltaClass = delta == null || Math.abs(delta) <= 0.05 ? "flat" : delta > 0 ? "up" : "down";
      return `<tr><td>${this.escape(this.formatDate(point.calculated_at))}</td><td>${point.combined.toFixed(1)}</td><td class="${deltaClass}">${delta == null ? "--" : `${delta > 0 ? "+" : ""}${delta.toFixed(1)}`}</td><td>${point.news.toFixed(1)}</td><td>${point.technical.toFixed(1)}</td><td>${point.market_flow.toFixed(1)}</td></tr>`;
    }).join("");
    return `<section class="score-trend-summary">
        <article><span>当前组合分</span><b>${state.latest.combined.toFixed(1)}</b><small>${this.escape(this.formatDate(state.latest.calculated_at))}</small></article>
        <article class="${state.direction}"><span>较上次</span><b>${state.arrow} ${state.badge}</b><small>${history.length} 个扫描点</small></article>
        <article><span>区间最高 / 最低</span><b>${Math.max(...values).toFixed(1)} / ${Math.min(...values).toFixed(1)}</b><small>仅当前机会生命周期</small></article>
        <article><span>预测入场分</span><b>${entryScore == null ? "--" : entryScore.toFixed(1)}</b><small>${entryScore == null ? "尚未生成预测" : "冻结，不随行情改写"}</small></article>
      </section>
      <section class="score-trend-chart-panel">
        <header><div><strong>评分变化折线图</strong><small>纵轴 0–100 分 · 横轴为机会扫描时间</small></div><div class="score-trend-legend">${series.map((definition) => `<span class="${definition.key}"><i></i>${definition.label}</span>`).join("")}</div></header>
        <svg class="score-trend-chart" viewBox="0 0 ${width} ${height}" role="img" aria-label="${this.escape(item.symbol)} 组合评分走势">
          <g class="score-grid">${grid}</g>
          <g class="score-threshold"><line x1="${padding.left}" y1="${y(threshold)}" x2="${width - padding.right}" y2="${y(threshold)}"></line><text x="${width - padding.right - 4}" y="${y(threshold) - 6}">准入线 ${threshold.toFixed(0)}</text></g>
          ${lines}${axes}
        </svg>
      </section>
      <section class="score-trend-ledger"><header><strong>最近评分明细</strong><small>最多保留 96 个扫描点</small></header><div><table><thead><tr><th>计算时间</th><th>组合分</th><th>变化</th><th>新闻</th><th>技术</th><th>资金盘口</th></tr></thead><tbody>${recentRows}</tbody></table></div></section>`;
  }

  openScoreTrend(opportunityId, trigger) {
    const item = this.state.opportunities.find((opportunity) => opportunity.id === opportunityId);
    if (!item) return;
    const history = this.opportunityScoreHistory(item);
    const modal = this.q("#score-trend-modal");
    this.scoreTrendOpportunity = item;
    this.scoreTrendFocus = trigger || null;
    this.q("#score-trend-title").textContent = `${item.symbol} · 组合评分走势`;
    this.q("#score-trend-subtitle").textContent = `${item.contract_symbol} · ${item.timeframe} 周期 · ${history.length} 个扫描点`;
    this.q("#score-trend-body").innerHTML = this.renderScoreTrendChart(item, history);
    modal.classList.remove("hidden");
    modal.setAttribute("aria-hidden", "false");
    this.q("#score-trend-close").focus({ preventScroll: true });
  }

  closeScoreTrend() {
    const modal = this.q("#score-trend-modal");
    if (!modal || modal.classList.contains("hidden")) return;
    modal.classList.add("hidden");
    modal.setAttribute("aria-hidden", "true");
    this.scoreTrendOpportunity = null;
    const focusTarget = this.scoreTrendFocus;
    this.scoreTrendFocus = null;
    if (focusTarget?.isConnected) focusTarget.focus({ preventScroll: true });
  }

  virtualEntryGate(item) {
    const evidence = item?.evidence || {};
    const frozenGate = item?.prediction_entry_gate;
    const liveGate = evidence.virtual_entry_gate;
    const source = frozenGate && Array.isArray(frozenGate.checks)
      ? frozenGate
      : liveGate && Array.isArray(liveGate.checks)
      ? liveGate
      : null;
    if (source) return { ...source, frozen: source === frozenGate };
    const config = this.state.config || {};
    const indicatorPolicy = evidence.indicator_policy || {};
    const marketFlow = evidence.market_flow || {};
    const market = evidence.market || {};
    const newsScore = Number(item?.news_score || 0);
    const indicatorScore = Number(item?.indicator_score || 0);
    const combinedScore = Number(item?.combined_score || 0);
    const entryPrice = Number(item?.prediction_entry_price ?? market.price ?? 0);
    const minimumNewsScore = Number(config.minimum_news_confidence ?? 0.6) * 100;
    const minimumIndicatorScore = Number(config.minimum_indicator_score ?? 65);
    const minimumCombinedScore = Number(config.minimum_combined_score ?? 75);
    const trigger = evidence.news_trigger || {};
    const marketQuality = evidence.market_quality || {};
    const checks = [
      ...(config.require_new_news_trigger ? [{ key: "new_news_trigger", label: "新事件", passed: trigger.has_new_news === true, current: trigger.has_new_news === true, required: true, detail: "触发窗口内存在未消费的新新闻" }] : []),
      { key: "news_candidate", label: "新闻候选", passed: newsScore >= minimumNewsScore, current: newsScore, required: minimumNewsScore, detail: "新闻评分达到候选门槛" },
      { key: "indicator_policy", label: "策略组", passed: indicatorPolicy.passed === true || evidence.technical_confirmed === true, current: indicatorPolicy.passed === true, required: true, detail: "至少一个核心技术策略组通过" },
      { key: "indicator_score", label: "技术评分", passed: indicatorScore >= minimumIndicatorScore, current: indicatorScore, required: minimumIndicatorScore, detail: "方向一致的技术强度" },
      { key: "combined_score", label: "组合评分", passed: combinedScore >= minimumCombinedScore, current: combinedScore, required: minimumCombinedScore, detail: "新闻、技术与盘口加权结果" },
      { key: "market_flow_conflict", label: "盘口冲突", passed: marketFlow.hard_conflict !== true, current: marketFlow.hard_conflict === true, required: false, detail: "候选方向不得存在资金强冲突" },
      ...(config.require_market_quality_for_prediction ? [{ key: "market_quality", label: "行情质量", passed: marketQuality.passed === true, current: marketQuality.passed === true, required: true, detail: "实时价、已收盘 K 线与预测因子新鲜可用" }] : []),
      { key: "entry_price", label: "入场价格", passed: entryPrice > 0, current: entryPrice > 0 ? entryPrice : null, required: "> 0", detail: "取得真实扫描参考价后才能冻结" },
    ];
    const signalConfirmed = checks.slice(0, -1).every((check) => check.passed);
    return {
      version: "frontend_legacy_fallback",
      execution_mode: "virtual_prediction_only",
      real_order_enabled: false,
      direction: item?.direction === "short" ? "short" : "long",
      signal_confirmed: signalConfirmed,
      entry_ready: signalConfirmed && checks.at(-1).passed,
      reference_price: entryPrice > 0 ? entryPrice : null,
      checked_at: item?.prediction_created_at || item?.updated_at || item?.discovered_at,
      checks,
      frozen: item?.prediction_entry_price != null,
    };
  }

  virtualEntryState(item, gate) {
    const hasPrediction = Boolean(item?.prediction_status || item?.prediction_created_at);
    const entryPrice = Number(item?.prediction_entry_price || 0);
    const direction = item?.direction === "short" ? "做空" : "做多";
    if (hasPrediction && entryPrice > 0) {
      const suffix = item.prediction_status === "completed"
        ? "已结算"
        : item.prediction_status === "unavailable"
        ? "退出行情不足"
        : "监控退出条件";
      return { tone: "triggered", label: direction, detail: suffix, triggered: true };
    }
    if (hasPrediction) {
      return { tone: "failed", label: "触发失败", detail: "未取得有效入场价格", triggered: false };
    }
    if (gate.entry_ready) {
      return { tone: "ready", label: "条件已满足", detail: "等待预测记录写入", triggered: false };
    }
    const failed = (gate.checks || []).filter((check) => !check.passed);
    return { tone: "waiting", label: "尚未触发", detail: failed.length ? `仍有 ${failed.length} 项条件未满足` : "等待下一轮扫描", triggered: false };
  }

  virtualPositionSnapshot(item) {
    const source = item?.virtual_position;
    if (source && typeof source === "object") return source;
    const entry = Number(item?.prediction_entry_price || 0);
    const current = Number(item?.evidence?.market?.price || 0);
    if (!(entry > 0) || !(current > 0)) return { available: false };
    const directionFactor = item?.direction === "short" ? -1 : 1;
    const grossBps = (current / entry - 1) * 10000 * directionFactor;
    return {
      available: true,
      entry_price: entry,
      current_price: current,
      market_at: item?.updated_at,
      valuation_state: "scan_fallback",
      gross_return_bps: grossBps,
      gross_return_pct: grossBps / 100,
      net_return_bps: grossBps,
      net_return_pct: grossBps / 100,
      gross_pnl_per_unit: (current - entry) * directionFactor,
      net_pnl_per_unit: (current - entry) * directionFactor,
      net_pnl_per_10000: grossBps,
      profit_state: grossBps > 0 ? "profit" : grossBps < 0 ? "loss" : "flat",
      target_state: "active",
      risk_plan: item?.evidence?.risk_plan || {},
    };
  }

  predictionSettlementState(item) {
    const source = item?.prediction_settlement || {};
    const dueAt = source.due_at || item?.prediction_due_at;
    const dueMs = this.parseDate(dueAt).getTime();
    const graceHours = Math.max(1, Number(source.grace_hours) || 6);
    const graceDeadline = source.grace_deadline || (Number.isFinite(dueMs) ? new Date(dueMs + graceHours * 3600000).toISOString() : null);
    const graceMs = this.parseDate(graceDeadline).getTime();
    const now = Date.now();
    const phase = source.phase || (item?.prediction_status === "completed"
      ? "completed"
      : item?.prediction_status === "unavailable"
      ? "unavailable"
      : Number.isFinite(dueMs) && now < dueMs
      ? "monitoring_exit"
      : Number.isFinite(graceMs) && now > graceMs
      ? "overdue"
      : "awaiting_market_data");
    const labels = {
      monitoring_exit: "正在监控退出条件",
      scheduled: "正在监控退出条件",
      awaiting_market_data: "已达持有上限，等待行情",
      overdue: "退出处理已超时",
      completed: "退出结算已完成",
      unavailable: "退出行情不足",
    };
    const details = {
      monitoring_exit: "后台正在按 15 分钟 K 线监控止盈、止损与评分转弱/反转；最大持有上限前任一条件先触发即退出。",
      scheduled: "后台正在按 15 分钟 K 线监控止盈、止损与评分转弱/反转；最大持有上限前任一条件先触发即退出。",
      awaiting_market_data: `已经达到最大持有上限；退出 K 线未到齐时每 ${Number(source.retry_interval_minutes || 5)} 分钟重试。`,
      overdue: `已经超过 ${graceHours} 小时宽限期，后台补偿任务将其转为“行情不足”。`,
      completed: "已按最先触发的价格、评分或最大持有条件退出，并完成方向收益与命中结果计算。",
      unavailable: `宽限期内始终没有取得有效退出行情，因此不计入命中率。`,
    };
    return {
      phase,
      label: labels[phase] || "监控退出条件",
      detail: details[phase] || "等待后台根据价格、评分或最大持有条件处理退出。",
      dueAt,
      graceDeadline,
      lastAttemptAt: source.last_attempt_at,
      nextRetryAt: source.next_retry_at,
      priceTimeframe: source.price_timeframe || "15m",
    };
  }

  signedMetric(value, digits = 2, suffix = "") {
    const number = Number(value);
    if (!Number.isFinite(number)) return "--";
    return `${number > 0 ? "+" : ""}${number.toFixed(digits)}${suffix}`;
  }

  patchOpportunityCards(target, markup) {
    const template = document.createElement("template");
    template.innerHTML = markup;
    const incoming = [...template.content.children];
    const currentCards = new Map(
      [...target.children]
        .filter((node) => node.matches?.(".opportunity-item[data-opportunity-card]"))
        .map((node) => [node.dataset.opportunityCard, node]),
    );
    const activeElement = this.shadowRoot.activeElement;
    const previousScrollTop = target.scrollTop;

    [...target.children]
      .filter((node) => !node.matches?.(".opportunity-item[data-opportunity-card]"))
      .forEach((node) => node.remove());

    incoming.forEach((nextCard) => {
      const key = nextCard.dataset.opportunityCard;
      const currentCard = currentCards.get(key);
      if (!currentCard) {
        target.appendChild(nextCard);
        return;
      }
      currentCards.delete(key);
      const hasFocus = activeElement && currentCard.contains(activeElement);
      if (!hasFocus && (currentCard.className !== nextCard.className || currentCard.innerHTML !== nextCard.innerHTML)) {
        currentCard.className = nextCard.className;
        currentCard.replaceChildren(...nextCard.childNodes);
      }
      target.appendChild(currentCard);
    });
    currentCards.forEach((node) => node.remove());
    target.scrollTop = Math.min(previousScrollTop, Math.max(0, target.scrollHeight - target.clientHeight));
  }

  renderOpportunities() {
    const target = this.q("#opportunity-list");
    const historicalTab = this.state.opportunityTab === "history";
    const statusFilter = this.state.opportunityStatusFilter;
    const visibleOpportunities = historicalTab || statusFilter === "all"
      ? this.state.opportunities
      : this.state.opportunities.filter((item) => this.virtualEntryState(item, this.virtualEntryGate(item)).tone === statusFilter);
    if (!visibleOpportunities.length) {
      const statusLabel = ({ triggered: "已触发", ready: "条件满足", waiting: "待触发", failed: "触发异常" })[statusFilter];
      const emptyMarkup = historicalTab
        ? '<div class="empty-state opportunity-empty"><strong>暂无历史机会</strong><span>信号过期或结束后会自动归入这里。</span></div>'
        : statusFilter !== "all"
        ? `<div class="empty-state opportunity-empty"><strong>当前没有“${statusLabel}”机会</strong><span>可切换其他状态，或等待下一轮机会扫描。</span></div>`
        : '<div class="empty-state opportunity-empty"><strong>尚未发现当前有效的美股候选</strong><span>系统会按新闻回看范围、置信度和关联股票继续扫描。</span></div>';
      if (target.innerHTML !== emptyMarkup) target.innerHTML = emptyMarkup;
      return;
    }
    const waitingSettlementCount = visibleOpportunities.filter((item) => item.prediction_status === "pending").length;
    const unavailableSettlementCount = visibleOpportunities.filter((item) => item.prediction_status === "unavailable").length;
    const settlementGuide = historicalTab ? `<aside class="settlement-guide" aria-label="历史机会退出生命周期说明">
      <div><span>EXIT LIFECYCLE</span><strong>“监控退出条件”不是等待买入</strong><p>预测已触发；后台持续检查 15 分钟 K 线的止盈、止损以及评分转弱/反转，任一条件先满足即退出。最大持有时间只是强制退出上限。</p></div>
      <b>${this.number(waitingSettlementCount + unavailableSettlementCount)}<small>未完成：待结 ${this.number(waitingSettlementCount)} · 行情不足 ${this.number(unavailableSettlementCount)}</small></b>
      <ul><li>后台每 20 秒扫描</li><li>价格 / 评分条件优先</li><li>到上限强制退出</li><li>缺行情每 5 分钟重试</li></ul>
    </aside>` : "";
    const markup = settlementGuide + visibleOpportunities.map((item) => {
      const evidence = item.evidence || {};
      const indicatorItems = evidence.indicators || [];
      const indicatorPolicy = evidence.indicator_policy || {};
      const matchedCount = Number(evidence.matched_indicator_count ?? indicatorItems.filter((indicator) => indicator.matched).length);
      const requiredCount = Number(evidence.required_indicator_count ?? indicatorItems.length);
      const availableCount = Number(evidence.available_indicator_count ?? indicatorItems.filter((indicator) => indicator.available !== false).length);
      const passedGroupCount = Array.isArray(indicatorPolicy.passed_groups) ? indicatorPolicy.passed_groups.length : 0;
      const indicators = indicatorItems.slice(0, 6).map((indicator) => `<span class="evidence-chip ${indicator.available === false ? "unavailable" : indicator.matched ? "matched" : "unmatched"}">${indicator.available === false ? "--" : indicator.matched ? "✓" : "×"} ${this.escape(indicator.name)} <b>${indicator.available === false ? "不可用" : Number(indicator.strength ?? 0).toFixed(0)}</b></span>`).join("");
      const indicatorRemainder = indicatorItems.length > 6 ? `<span class="evidence-chip remainder">另有 ${indicatorItems.length - 6} 项</span>` : "";
      const news = (evidence.news || []).slice(0, 2).map((entry) => `<li><time>${this.formatUnix(entry.ts)}</time><span>${this.escape(entry.title)}</span><b>${Math.round(Number(entry.score || 0) * 100)}%</b></li>`).join("");
      const market = evidence.market || {};
      const marketEnvironment = evidence.market_environment || evidence.score_snapshot?.macro_market || {};
      const macroSnapshot = evidence.macro_market_snapshot || {};
      const macroIndices = Object.fromEntries((macroSnapshot.indices || []).map((entry) => [entry.key, entry]));
      const macroSectors = Object.fromEntries((macroSnapshot.sectors || []).map((entry) => [entry.key, entry]));
      const macroNdx = macroIndices.NDX || {};
      const macroSector = macroSectors[marketEnvironment.sector_key] || {};
      const macroAdjustment = Number(marketEnvironment.adjustment || 0);
      const macroResonance = marketEnvironment.resonance || "unknown";
      const macroFactors = (marketEnvironment.factors || []).slice(0, 3).map((factor) => `${factor.label} ${Number(factor.points) >= 0 ? "+" : ""}${Number(factor.points).toFixed(0)}`).join(" · ");
      const macroReference = marketEnvironment.available ? `<section class="opportunity-macro ${this.escape(macroResonance)}" aria-label="大盘环境参考">
        <span class="macro-resonance"><i>${macroResonance === "resonant" ? "✓" : macroResonance === "divergent" ? "⚠" : "•"}</i><b>${this.escape(marketEnvironment.resonance_label || "大盘中性")}</b><small>大盘环境参考</small></span>
        <span><em>纳指 100</em><b class="${Number(macroNdx.change_percent) >= 0 ? "positive" : "negative"}">${Number.isFinite(Number(macroNdx.change_percent)) ? `${Number(macroNdx.change_percent) >= 0 ? "+" : ""}${Number(macroNdx.change_percent).toFixed(2)}%` : "--"}</b><small>RSI ${marketEnvironment.market_rsi == null ? "--" : Number(marketEnvironment.market_rsi).toFixed(1)} · ${this.escape(macroNdx.provider_symbol || "QQQ")} ${macroNdx.proxy ? "代理" : "指数"}</small></span>
        <span><em>VIX / 板块</em><b>VIX ${marketEnvironment.vix == null ? "--" : Number(marketEnvironment.vix).toFixed(1)}</b><small>${this.escape(marketEnvironment.sector_label || "大盘")} ${Number.isFinite(Number(macroSector.change_percent)) ? `${Number(macroSector.change_percent) >= 0 ? "+" : ""}${Number(macroSector.change_percent).toFixed(2)}%` : "--"}</small></span>
        <span class="macro-adjustment ${macroAdjustment > 0 ? "positive" : macroAdjustment < 0 ? "negative" : "flat"}"><em>评分调整</em><b>${macroAdjustment > 0 ? "+" : ""}${macroAdjustment.toFixed(1)} 分</b><small>${this.escape(macroFactors || "当前无宏观加减分")}</small></span>
      </section>` : `<section class="opportunity-macro unavailable" aria-label="大盘环境参考"><span class="macro-resonance"><i>--</i><b>大盘数据不足</b><small>该条信号未应用宏观调整</small></span></section>`;
      const confirmed = item.status === "discovered" || evidence.confirmed === true;
      const readiness = evidence.live_readiness || {};
      const shadowReady = readiness.status === "shadow_ready";
      const readinessBadge = `<span class="readiness-badge ${shadowReady ? "shadow" : "research"}">${shadowReady ? "影子候选" : "研究信号"}</span>`;
      const newsTrigger = evidence.news_trigger || {};
      const triggerAge = Number(newsTrigger.newest_news_age_minutes);
      const triggerBadge = newsTrigger.version
        ? `<span class="quality-badge ${newsTrigger.has_new_news ? "passed" : "blocked"}" title="触发窗口 ${Number(newsTrigger.trigger_window_hours || 4)} 小时 · AI 记忆 ${Number(newsTrigger.memory_window_hours || 168)} 小时">${newsTrigger.has_new_news ? `新事件 ${Number.isFinite(triggerAge) ? `${Math.max(0, Math.round(triggerAge))}m` : "已确认"}` : "无新事件"}</span>`
        : `<span class="quality-badge legacy" title="旧版信号未保存新闻触发快照">旧规则</span>`;
      const marketQuality = evidence.market_quality || {};
      const marketQualityBadge = marketQuality.passed === true
        ? '<span class="quality-badge passed">行情新鲜</span>'
        : marketQuality.passed === false
        ? '<span class="quality-badge blocked" title="实时价格、已收盘 K 线或预测因子不符合准入要求">行情受限</span>'
        : '<span class="quality-badge legacy">质量未知</span>';
      const historyState = item.prediction_status === "pending"
        ? "等待结算"
        : item.prediction_status === "unavailable"
        ? "行情不足"
        : item.status === "dismissed" ? "已结束" : "已过期";
      const marketAvailable = evidence.market_available !== false;
      const directionLabel = item.direction === "short" ? "做空" : "做多";
      const directionClass = item.direction === "short" ? "short" : "long";
      const signalStart = this.formatDate(item.prediction_created_at || item.discovered_at);
      const signalEnd = this.formatDate(item.expires_at);
      const signalDuration = this.formatDuration(item.prediction_created_at || item.discovered_at, item.expires_at);
      const scoreUpdatedAt = item.updated_at || evidence.score_snapshot?.calculated_at || item.discovered_at;
      const frozenCombinedScore = item.prediction_combined_score;
      const scoreDelta = frozenCombinedScore == null
        ? ""
        : ` · 入场 ${Number(frozenCombinedScore).toFixed(1)}`;
      const scoreHistory = this.opportunityScoreHistory(item);
      const scoreTrend = this.scoreTrendState(scoreHistory);
      const entryGate = this.virtualEntryGate(item);
      const entryState = this.virtualEntryState(item, entryGate);
      const signalGateChecks = (entryGate.checks || []).filter((check) => check.key !== "entry_price");
      const passedGateCount = signalGateChecks.filter((check) => check.passed).length;
      const entryGateChecks = signalGateChecks.map((check) => {
        const current = check.key === "indicator_policy" || check.key === "new_news_trigger" || check.key === "market_quality"
          ? check.passed ? "通过" : "未通过"
          : check.key === "market_flow_conflict"
          ? check.passed ? "无冲突" : "有冲突"
          : check.current == null
          ? "--"
          : Number.isFinite(Number(check.current))
          ? Number(check.current).toFixed(check.key === "entry_price" ? 2 : 1)
          : String(check.current);
        const required = typeof check.required === "number" ? check.required.toFixed(1) : String(check.required ?? "");
        return `<span class="virtual-entry-check ${check.passed ? "passed" : "blocked"}" title="${this.escape(check.detail || "")}"><i>${check.passed ? "✓" : "×"}</i><em>${this.escape(check.label)}</em><b>${this.escape(current)}</b><small>${["market_flow_conflict", "indicator_policy", "new_news_trigger", "market_quality"].includes(check.key) ? "" : `门槛 ${this.escape(required)}`}</small></span>`;
      }).join("");
      const triggerPriceValue = Number(item.prediction_entry_price || 0);
      const liveReferencePrice = Number(entryGate.reference_price ?? market.price ?? 0);
      const displayedEntryPrice = triggerPriceValue > 0 ? triggerPriceValue : liveReferencePrice;
      const entryPriceLabel = triggerPriceValue > 0 ? "冻结触发价格" : "当前参考价 · 未冻结";
      const entryTime = item.prediction_created_at || entryGate.checked_at;
      const gateScope = entryGate.frozen ? "触发时条件已冻结" : "当前扫描条件";
      const triggeredPosition = entryState.triggered && !historicalTab;
      const virtualEntryPanel = `<section class="virtual-entry-gate ${entryState.tone} ${triggeredPosition ? "position-active" : ""}" aria-label="买入触发条件与状态">
        <div class="virtual-entry-state"><span>ENTRY GATE</span><strong>${this.escape(entryState.label)}</strong><small>${this.escape(entryState.detail)} · ${gateScope} · 真实订单关闭</small></div>
        <div class="virtual-entry-checks">${entryGateChecks}</div>
        ${triggeredPosition ? "" : `<div class="virtual-entry-price"><span>${entryPriceLabel}</span><b>${displayedEntryPrice > 0 ? this.escape(this.compactNumber(displayedEntryPrice)) : "--"}</b><small>${entryState.triggered ? `触发 ${this.formatDate(entryTime)}` : `检查 ${this.formatDate(entryTime)}`}</small></div>`}
      </section>`;
      const position = this.virtualPositionSnapshot(item);
      const riskPlan = position.risk_plan || {};
      const positionTone = position.profit_state || "flat";
      const markLabel = position.valuation_state === "settled"
        ? "实际退出价"
        : position.market_stale
        ? "最近价格 · 行情延迟"
        : "实时价格";
      const targetStateLabel = position.target_state === "take_profit_reached"
        ? "已越过止盈线"
        : position.target_state === "stop_loss_reached"
        ? "已越过止损线"
        : position.valuation_state === "settled"
        ? "预测已结算"
        : "持仓观察中";
      const positionPanel = triggeredPosition ? `<section class="virtual-position ${positionTone}" aria-label="持仓实时盈亏">
        <div class="virtual-position-title"><span>POSITION · ${this.state.displayLeverage}X DISPLAY</span><strong>${directionLabel} · ${targetStateLabel}</strong><small>触发 ${this.formatDate(entryTime)} · 有效至 ${signalEnd} · 不会发送真实订单</small></div>
        <div class="virtual-position-metrics">
          <span><em>冻结入场价</em><b>${position.entry_price > 0 ? this.escape(this.compactNumber(position.entry_price)) : "--"}</b><small>触发时价格，不随行情变化</small></span>
          <span class="live-mark"><em>${markLabel}</em><b>${position.current_price > 0 ? this.escape(this.compactNumber(position.current_price)) : "--"}</b><small>${position.market_at ? this.formatDate(position.market_at) : "等待最新行情"}</small></span>
          <span class="position-pnl ${positionTone}"><em>当前 ${this.state.displayLeverage}x 净收益率</em><b>${position.available ? this.formatLeveragedReturnFromPercent(position.net_return_pct) : "--"}</b><small>${position.available ? `标的净收益 ${this.signedMetric(position.net_return_pct, 2, "%")} · 含成本换算 · 不含强平` : "暂无可用实时行情"}</small></span>
          <span class="risk-stop"><em>参考止损价</em><b>${riskPlan.stop_loss_price > 0 ? this.escape(this.compactNumber(riskPlan.stop_loss_price)) : "--"}</b><small>风险 ${riskPlan.stop_loss_pct == null ? "--" : `-${Number(riskPlan.stop_loss_pct).toFixed(2)}%`}</small></span>
          <span class="risk-target"><em>参考止盈价</em><b>${riskPlan.take_profit_price > 0 ? this.escape(this.compactNumber(riskPlan.take_profit_price)) : "--"}</b><small>目标 ${riskPlan.take_profit_pct == null ? "--" : `+${Number(riskPlan.take_profit_pct).toFixed(2)}%`}</small></span>
          <span><em>风险收益比</em><b>1 : ${Number(riskPlan.risk_reward_ratio || 2).toFixed(1)}</b><small>${riskPlan.method === "atr14_x_1_5" ? "ATR(14) 波动率冻结" : "按周期默认风险"} · 仅观察线</small></span>
        </div>
      </section>` : "";
      const waitingDetail = entryState.tone === "ready"
        ? "条件已满足，等待预测写入"
        : `${signalGateChecks.length - passedGateCount} 项条件未满足 · 尚未入场`;
      const signalSummaryPanel = `<div class="opportunity-signal ${historicalTab ? "historical-signal" : "candidate-signal"}" aria-label="${historicalTab ? "历史信号" : "待触发候选"}信息">
        <span><em>信号时间</em><b>${signalStart}</b></span>
        <span class="validity"><em>信号有效期间</em><b>${signalDuration}</b><small>${signalStart} — ${signalEnd}</small></span>
        <span><em>${historicalTab ? "历史方向" : "候选方向"}</em><b class="signal-${directionClass}">${directionLabel}</b><small>${historicalTab ? "按该方向统计结果" : "仅为研判方向，尚未买入"}</small></span>
        <span class="trigger-progress ${entryState.tone}"><em>${historicalTab ? "触发结果" : "触发进度"}</em><b>${passedGateCount} / ${signalGateChecks.length}</b><small>${historicalTab ? entryState.triggered ? "已生成预测" : "未生成预测" : waitingDetail}</small></span>
      </div>`;
      const outcome = historicalTab ? item.outcome : null;
      const predictionStatus = String(item.prediction_status || "");
      const settlementState = this.predictionSettlementState(item);
      const outcomeResult = outcome?.result
        || (predictionStatus === "pending" ? "pending" : predictionStatus === "unavailable" ? "unavailable" : "not_created");
      const outcomeLabel = outcome
        ? this.analyticsResultLabel(outcomeResult)
        : outcomeResult === "pending"
        ? "监控退出条件"
        : outcomeResult === "unavailable"
        ? "行情不足"
        : confirmed
        ? "未生成预测"
        : "技术未确认";
      const outcomeReturn = outcome?.directional_return_bps != null
        ? `${this.state.displayLeverage}x 净收益 ${this.formatLeveragedReturnFromBps(outcome.directional_return_bps)} · 标的 ${this.formatBps(outcome.directional_return_bps)}`
        : outcomeResult === "pending"
        ? `最大持有上限 ${this.formatDate(settlementState.dueAt)} · ${settlementState.label}`
        : outcomeResult === "unavailable"
        ? "退出行情宽限期内未取得行情"
        : confirmed
        ? "历史信号未关联预测记录"
        : `核心同向 ${Number(indicatorPolicy.core_matched_count ?? matchedCount)} 项 · 通过 ${passedGroupCount} 个策略组`;
      const outcomeMetric = historicalTab
        ? `<span class="history-result ${this.escape(outcomeResult)}"><em>${outcome ? "是否命中" : "预测状态"}</em><b>${this.escape(outcomeLabel)}</b><small>${this.escape(outcomeReturn)}</small></span>`
        : "";
      const settlementPanel = historicalTab && outcomeResult === "pending" ? `<section class="settlement-detail ${this.escape(settlementState.phase)}" aria-label="退出生命周期详情">
        <div><span>SETTLEMENT STATUS</span><strong>${this.escape(settlementState.label)}</strong><small>${this.escape(settlementState.detail)}</small></div>
        <span><em>最大持有上限</em><b>${this.formatDate(settlementState.dueAt)}</b><small>due_at 仅是强制退出上限，价格或评分条件可提前退出</small></span>
        <span><em>下次预计处理</em><b>${settlementState.nextRetryAt ? this.formatDate(settlementState.nextRetryAt) : "后台下轮扫描"}</b><small>${settlementState.lastAttemptAt ? `最近尝试 ${this.formatDate(settlementState.lastAttemptAt)}` : `使用 ${this.escape(settlementState.priceTimeframe)} K 线监控退出`}</small></span>
        <span><em>行情补偿截止</em><b>${this.formatDate(settlementState.graceDeadline)}</b><small>达到持有上限后仍无退出行情，超过此时间则不计入统计</small></span>
      </section>` : "";
      const symbolControl = marketAvailable
        ? `<button class="opportunity-symbol" type="button" data-opportunity-id="${this.escape(item.id)}" data-open-contract="${this.escape(item.contract_symbol)}" data-timeframe="${this.escape(item.timeframe)}" title="打开 ${this.escape(item.symbol)} 的合约 K 线研究与预测模拟">${this.escape(item.symbol)}</button>`
        : `<button class="opportunity-symbol unavailable" type="button" disabled title="该股票暂无对应的合约技术行情">${this.escape(item.symbol)}</button>`;
      const conclusionControl = `<button class="ai-conclusion-trigger" type="button" data-ai-conclusion="${this.escape(item.id)}" title="查看 ${this.escape(item.symbol)} 的 AI 分析结论">AI分析结论</button>`;
      return `<article class="opportunity-item ${this.escape(item.status)} ${historicalTab ? `historical outcome-${this.escape(outcomeResult)}` : ""}" data-opportunity-card="${this.escape(item.id)}">
        <header><div><span class="direction ${confirmed ? "confirmed" : "candidate"}">${confirmed ? "技术已确认" : "新闻候选"}</span>${readinessBadge}${triggerBadge}${marketQualityBadge}${symbolControl}<small>${marketAvailable ? this.escape(item.contract_symbol) : "暂无技术行情"}</small>${conclusionControl}</div><button class="opportunity-score ${scoreTrend.direction}" type="button" data-score-trend="${this.escape(item.id)}" title="查看 ${this.escape(item.symbol)} 评分变化走势"><span class="score-current"><i>${scoreTrend.arrow}</i><b>${Number(item.combined_score).toFixed(1)}</b></span><span>当前组合评分${scoreDelta}</span><em>${scoreTrend.badge}</em></button></header>
        ${virtualEntryPanel}
        ${macroReference}
        <div class="opportunity-metrics ${historicalTab ? "with-result" : ""}"><span><em>新闻评分</em><b>${Number(item.news_score).toFixed(1)}</b><small>${newsTrigger.version ? `${Number(newsTrigger.new_news_ids?.length || 0)} 条新事件 · 记忆 ${Number(newsTrigger.memory_window_hours || 168)}h` : "旧版记录"}</small></span><span><em>指标强度</em><b>${Number(item.indicator_score).toFixed(1)}</b><small>${matchedCount} 项同向 · ${availableCount}/${requiredCount} 可用</small></span><span><em>确认周期</em><b>${this.escape(item.timeframe)}</b><small>${marketQuality.passed === true ? "行情质量通过" : "行情质量未通过"}</small></span><span><em>信号状态</em><b>${historicalTab ? historyState : shadowReady ? "可影子观察" : confirmed ? "仅研究" : "等待确认"}</b></span>${outcomeMetric}</div>
        ${positionPanel || signalSummaryPanel}
        ${settlementPanel}
        <div class="evidence-chips">${indicators}${indicatorRemainder}</div>
        <ul class="opportunity-news">${news}</ul>
        <footer><span>发现 ${this.formatDate(item.discovered_at)}</span><span>评分更新 ${this.formatDate(scoreUpdatedAt)}</span><span>有效至 ${this.formatDate(item.expires_at)}</span><em>${historicalTab ? `历史机会 · ${outcomeLabel}` : shadowReady ? "影子候选 · 仍不执行交易" : confirmed ? `研究预测 · ${(readiness.failed_reasons || ["未通过影子准入"]).slice(0, 1).join("")}` : item.status === "candidate" ? marketAvailable ? "等待策略组与评分确认" : "新闻候选 · 暂无技术行情" : "历史机会"}</em></footer>
      </article>`;
    }).join("");
    this.patchOpportunityCards(target, markup);
  }

  openAiConclusion(opportunityId, trigger) {
    const item = this.state.opportunities.find((opportunity) => opportunity.id === opportunityId);
    if (!item) {
      this.showBanner("该机会的 AI 分析结论已更新，请刷新后重试。", "error");
      return;
    }
    const evidence = item.evidence || {};
    const newsItems = Array.isArray(evidence.news) ? evidence.news : [];
    const indicatorItems = Array.isArray(evidence.indicators) ? evidence.indicators : [];
    const marketFlow = evidence.market_flow && typeof evidence.market_flow === "object" ? evidence.market_flow : {};
    const indicatorPolicy = evidence.indicator_policy || {};
    const scoreWeightLabel = this.scoreWeightSummary(evidence);
    const matchedCount = Number(evidence.matched_indicator_count ?? indicatorItems.filter((indicator) => indicator.matched).length);
    const requiredCount = Number(evidence.required_indicator_count ?? indicatorItems.length);
    const availableCount = Number(evidence.available_indicator_count ?? indicatorItems.filter((indicator) => indicator.available !== false).length);
    const coreMatchedCount = Number(indicatorPolicy.core_matched_count ?? matchedCount);
    const passedGroups = Array.isArray(indicatorPolicy.passed_groups) ? indicatorPolicy.passed_groups : [];
    const confirmed = item.status === "discovered" || evidence.confirmed === true;
    const directionClass = item.direction === "short" ? "short" : "long";
    const directionLabel = item.direction === "short" ? "做空" : "做多";
    const directionText = item.direction === "short" ? "偏空" : "偏多";
    const uniqueReasons = [...new Set(newsItems.map((entry) => String(entry.reason || "").trim()).filter(Boolean))];
    const primaryReason = uniqueReasons[0] || "关联新闻已形成方向判断，但模型未提供更详细的文字理由。";
    const flowConflict = marketFlow.hard_conflict === true;
    const technicalConclusion = confirmed
      ? `技术策略组已确认：${coreMatchedCount} 项核心指标同向，${passedGroups.length} 个策略组达标；技术强度与组合评分均已过线，资金盘口未出现强反向冲突，信号已进入预测。`
      : flowConflict
      ? `技术策略组已达到门槛，但资金盘口与预测方向强冲突，暂不进入预测。`
      : `当前 ${coreMatchedCount} 项核心指标同向、${availableCount}/${requiredCount} 项有可用数据，通过 ${passedGroups.length} 个策略组；技术强度或组合评分尚未达到准入线。`;
    const newsMarkup = newsItems.length
      ? newsItems.map((entry) => {
          const score = Math.round(Number(entry.score || 0) * 100);
          const confidence = Math.round(Number(entry.confidence || 0) * 100);
          const relevance = Math.round(Number(entry.relevance || 0) * 100);
          return `<article class="ai-conclusion-news-item">
            <header><div><span>${this.escape(entry.source || "未知来源")}</span><time>${this.formatUnix(entry.ts)}</time></div><b>${score}%</b></header>
            <h4>${this.escape(entry.title || "未命名新闻")}</h4>
            <p>${this.escape(entry.reason || "AI 未提供进一步说明")}</p>
            <footer><span>AI 置信度 ${confidence}%</span><span>股票关联度 ${relevance}%</span><span>${this.escape(directionText)}</span></footer>
          </article>`;
        }).join("")
      : '<div class="ai-conclusion-empty">该机会没有可展示的新闻证据。</div>';
    const indicatorMarkup = indicatorItems.length
      ? indicatorItems.map((indicator) => {
          const metrics = (Array.isArray(indicator.metrics) ? indicator.metrics : []).slice(0, 5).map((metric) => {
            const label = metric && typeof metric === "object" ? metric.label : "指标值";
            const value = metric && typeof metric === "object" ? metric.value : metric;
            return `<span><em>${this.escape(label || "指标值")}</em><b>${this.escape(value ?? "--")}</b></span>`;
          }).join("");
          const availabilityLabel = indicator.available === false ? "数据不可用 · 不参与门槛" : indicator.matched ? "已满足" : "未满足";
          return `<article class="ai-conclusion-indicator ${indicator.available === false ? "unavailable" : indicator.matched ? "matched" : "unmatched"}">
            <header><strong>${indicator.available === false ? "--" : indicator.matched ? "✓" : "×"} ${this.escape(indicator.name || indicator.key)}</strong><span>${availabilityLabel}${indicator.available === false ? "" : ` · 强度 ${Number(indicator.strength ?? 0).toFixed(1)}`}</span></header>
            <p>${this.escape(indicator.summary || "暂无指标摘要")}</p>
            ${metrics ? `<div>${metrics}</div>` : ""}
          </article>`;
        }).join("")
      : '<div class="ai-conclusion-empty">该机会没有可展示的技术指标证据。</div>';
    const statusLabel = confirmed ? "技术与盘口已确认" : flowConflict ? "资金盘口反向" : "等待技术确认";
    const marketPrice = evidence.market?.price == null ? "--" : this.compactNumber(evidence.market.price);
    const analysisMarkup = `
      <section class="ai-conclusion-summary ${directionClass}">
        <div><span>综合研判</span><h3>AI 新闻分析${directionText} ${this.escape(item.symbol)}</h3><p>${this.escape(primaryReason)} ${this.escape(technicalConclusion)}</p></div>
        <strong>${directionLabel}<small>${statusLabel}</small></strong>
      </section>
      <section class="ai-conclusion-scores" aria-label="AI 分析评分">
        <article><span>新闻评分</span><b>${Number(item.news_score).toFixed(1)}</b><small>AI 置信度 × 股票关联度</small></article>
        <article><span>技术指标</span><b>${Number(item.indicator_score).toFixed(1)}</b><small>${coreMatchedCount} 项核心同向 · ${passedGroups.length} 组通过</small></article>
        <article><span>组合评分</span><b>${Number(item.combined_score).toFixed(1)}</b><small>${this.escape(scoreWeightLabel)}</small></article>
        <article><span>参考价格</span><b>${this.escape(marketPrice)}</b><small>信号扫描时行情</small></article>
      </section>
      <section class="ai-conclusion-section"><header><div><span>01</span><h3>最终结论</h3></div><small>${statusLabel}</small></header><div class="ai-conclusion-verdict"><p>方向判断：<strong class="${directionClass}">${directionLabel} ${this.escape(item.symbol)}</strong>。${this.escape(technicalConclusion)}</p><p>主要依据：${this.escape(uniqueReasons.join("；") || primaryReason)}</p></div></section>
      <section class="ai-conclusion-section"><header><div><span>02</span><h3>AI 新闻研判</h3></div><small>${newsItems.length} 条关联新闻</small></header><div class="ai-conclusion-news-list">${newsMarkup}</div></section>
      <section class="ai-conclusion-section"><header><div><span>03</span><h3>技术指标验证</h3></div><small>${availableCount} / ${requiredCount} 可用 · ${passedGroups.length} 组通过</small></header><div class="ai-conclusion-indicator-list">${indicatorMarkup}</div></section>`;
    this.conclusionOpportunity = item;
    this.conclusionPanels = {
      fundamentals: '<div class="ai-conclusion-loading">正在读取数据库基本面信息…</div>',
      analysis: analysisMarkup,
      news: this.renderAiRelatedNewsList(newsItems, item, true),
      memory: '<div class="ai-conclusion-loading">正在读取近 7 天 AI 新闻分析记录…</div>',
      market: this.renderMarketFlowPanel(item),
    };
    this.q("#ai-conclusion-market-state").textContent = marketFlow.version ? `评分 ${Number(marketFlow.score ?? 50).toFixed(1)}` : "旧信号无快照";
    this.q("#ai-conclusion-news-count").textContent = `关联 ${Math.max(newsItems.length, (item.news_ids || []).length)} 条`;
    this.showAiConclusionView("analysis");
    this.conclusionFocus = trigger || null;
    const modal = this.q("#ai-conclusion-modal");
    modal.classList.remove("hidden");
    modal.setAttribute("aria-hidden", "false");
    this.q("#ai-conclusion-close").focus({ preventScroll: true });
    this.loadAiRelatedNews(item, newsItems);
    this.loadAiNewsAnalysisRecords(item);
    this.loadAiFundamentals(item);
  }

  showAiConclusionView(view) {
    if (!["fundamentals", "news", "memory", "market", "analysis"].includes(view)) return;
    this.state.conclusionView = view;
    this.qa("[data-conclusion-view]").forEach((button) => {
      const active = button.dataset.conclusionView === view;
      button.classList.toggle("active", active);
      button.setAttribute("aria-selected", String(active));
    });
    const item = this.conclusionOpportunity;
    if (item) {
      const viewTitle = ({ fundamentals: "基本面信息", news: "相关新闻列表", memory: "AI 新闻分析记录", market: "资金盘口指标", analysis: "AI 分析结论" })[view];
      this.q("#ai-conclusion-title").textContent = `${item.symbol} · ${viewTitle}`;
      this.q("#ai-conclusion-subtitle").textContent = `${item.contract_symbol} · ${this.formatDate(item.discovered_at)} · ${item.timeframe} 周期`;
    }
    const target = this.q("#ai-conclusion-body");
    target.innerHTML = this.conclusionPanels[view] || '<div class="ai-conclusion-loading">正在读取相关内容…</div>';
    target.scrollTop = 0;
  }

  renderMarketFlowPanel(item) {
    const flow = item?.evidence?.market_flow;
    if (!flow || typeof flow !== "object" || !flow.version) {
      return '<div class="ai-market-flow-empty"><strong>该历史信号没有资金盘口快照</strong><span>新一轮信号扫描会写入换手率、主力量比、交易深度和挂单变化；旧记录不会用当前行情反向补造。</span></div>';
    }
    const value = (raw, suffix = "") => raw == null || !Number.isFinite(Number(raw)) ? "--" : `${Number(raw).toFixed(2)}${suffix}`;
    const ratio = (raw) => raw == null || !Number.isFinite(Number(raw)) ? "--" : Number(raw).toFixed(3);
    const amount = (raw) => raw == null || !Number.isFinite(Number(raw)) ? "--" : this.compactNumber(Number(raw));
    const directionClass = item.direction === "short" ? "short" : "long";
    const directionLabel = item.direction === "short" ? "做空" : "做多";
    const stateLabel = flow.hard_conflict ? "强反向冲突" : flow.confirms_direction ? "确认预测方向" : "中性观察";
    const turnoverLabel = flow.turnover_source === "underlying_volume_over_shares" ? "美股成交量 ÷ 总股本" : flow.turnover_source === "contract_value_over_market_cap_proxy" ? "合约成交额 ÷ 市值代理" : "暂无可靠分母";
    const sourceLabel = flow.sources?.depth === "binance_futures_market_by_price" ? "Binance 合约深度" : "深度暂缺";
    const activeSource = flow.sources?.active_flow === "binance_futures_taker" ? "Taker 主动成交" : "盘口压力代理";
    return `<section class="ai-market-flow-panel">
      <header class="${directionClass}"><div><span class="eyebrow">MARKET FLOW SNAPSHOT</span><h3>资金流与盘口结构</h3><p>生成信号时保存的实时快照，不使用当前行情覆盖历史判断。</p></div><strong>${Number(flow.score ?? 50).toFixed(1)}<small>${directionLabel}资金评分 · ${stateLabel}</small></strong></header>
      <section class="ai-market-flow-grid" aria-label="资金盘口指标">
        <article><span>换手率</span><b>${value(flow.turnover_rate_pct, "%")}</b><small>${this.escape(turnoverLabel)}</small></article>
        <article class="${flow.confirms_direction ? "positive" : flow.hard_conflict ? "negative" : ""}"><span>主力量比</span><b>${ratio(flow.main_force_ratio)}</b><small>0 买方弱 · 1 买方强</small></article>
        <article><span>主动买入占比</span><b>${value(flow.active_buy_ratio == null ? null : Number(flow.active_buy_ratio) * 100, "%")}</b><small>${this.escape(activeSource)}</small></article>
        <article><span>盘口失衡 / 近5档</span><b>${value(flow.book_imbalance)} / ${value(flow.book_imbalance_5)}</b><small>-1 卖压 · +1 买压</small></article>
        <article><span>买方 / 卖方深度</span><b>${amount(flow.bid_depth_notional)} / ${amount(flow.ask_depth_notional)}</b><small>前100档名义金额</small></article>
        <article><span>近5档买 / 卖</span><b>${amount(flow.bid_depth_notional_5)} / ${amount(flow.ask_depth_notional_5)}</b><small>最接近成交价的挂单</small></article>
        <article><span>买 / 卖挂单档位</span><b>${Number(flow.bid_level_count || 0)} / ${Number(flow.ask_level_count || 0)}</b><small>价格档数代理，非真实订单笔数</small></article>
        <article><span>5秒挂单增速</span><b>${value(flow.bid_depth_change_5s_pct, "%")} / ${value(flow.ask_depth_change_5s_pct, "%")}</b><small>买方 / 卖方深度变化</small></article>
        <article><span>30秒挂单增速</span><b>${value(flow.bid_depth_change_30s_pct, "%")} / ${value(flow.ask_depth_change_30s_pct, "%")}</b><small>过滤瞬时盘口噪声</small></article>
        <article><span>买卖价差</span><b>${value(flow.spread_bps, " bps")}</b><small>越低通常流动性越好</small></article>
      </section>
      <section class="ai-market-flow-method"><div><strong>如何参与机会判断</strong><span>${this.escape(this.scoreWeightSummary(item.evidence || {}))}</span></div><p>主力量比综合主动成交 50%、近5档盘口压力 30%、挂单增速 20%。当至少两类资金数据有效且与预测方向的得分低于 35 分时，作为强反向冲突阻止生成预测。</p><footer><span>${this.escape(sourceLabel)}</span><span>数据质量 ${value(Number(flow.data_quality ?? 0) * 100, "%")}</span><span>${this.escape(flow.note || "买卖挂单数量使用可见价格档位代理。")}</span></footer></section>
    </section>`;
  }

  scoreWeightSummary(evidence = {}) {
    const weights = evidence.score_weights && typeof evidence.score_weights === "object" ? evidence.score_weights : {};
    const percent = (value, fallback) => {
      const parsed = Number(value);
      const normalized = Number.isFinite(parsed) ? (parsed <= 1 ? parsed * 100 : parsed) : fallback;
      return Number(normalized.toFixed(1)).toString();
    };
    return `新闻 ${percent(weights.news, 45)}% + 技术指标 ${percent(weights.technical, 35)}% + 资金盘口 ${percent(weights.market_flow, 20)}%`;
  }

  async loadAiRelatedNews(item, fallbackItems) {
    const requestId = ++this.conclusionNewsRequestId;
    try {
      const data = await this.api(`/opportunities/${encodeURIComponent(item.id)}/news`);
      if (requestId !== this.conclusionNewsRequestId || this.conclusionOpportunity?.id !== item.id) return;
      const items = Array.isArray(data.items) ? data.items : [];
      this.conclusionPanels.news = this.renderAiRelatedNewsList(items, item, false);
      this.q("#ai-conclusion-news-count").textContent = `关联 ${items.length} 条`;
      if (this.state.conclusionView === "news") this.showAiConclusionView("news");
    } catch {
      if (requestId !== this.conclusionNewsRequestId || this.conclusionOpportunity?.id !== item.id) return;
      this.conclusionPanels.news = this.renderAiRelatedNewsList(fallbackItems, item, true);
      this.q("#ai-conclusion-news-count").textContent = `关联 ${fallbackItems.length} 条`;
      if (this.state.conclusionView === "news") this.showAiConclusionView("news");
    }
  }

  async loadAiNewsAnalysisRecords(item) {
    const requestId = ++this.conclusionMemoryRequestId;
    try {
      const data = await this.api(`/opportunities/${encodeURIComponent(item.id)}/news-analysis-records`);
      if (requestId !== this.conclusionMemoryRequestId || this.conclusionOpportunity?.id !== item.id) return;
      const records = Array.isArray(data.items) ? data.items : [];
      const memoryMeta = {
        currentOpportunityTotal: Number(data.current_opportunity_total || 0),
        excludedTotal: Number(data.excluded_total || 0),
        truncated: data.truncated === true,
      };
      this.conclusionPanels.memory = this.renderAiNewsAnalysisRecords(records, item, Number(data.window_days || 7), memoryMeta);
      this.q("#ai-conclusion-memory-count").textContent = `7天记忆 ${records.length} 条 · 当前 ${memoryMeta.currentOpportunityTotal} 条`;
      if (this.state.conclusionView === "memory") this.showAiConclusionView("memory");
    } catch {
      if (requestId !== this.conclusionMemoryRequestId || this.conclusionOpportunity?.id !== item.id) return;
      this.conclusionPanels.memory = '<div class="ai-memory-empty"><strong>新闻分析记录读取失败</strong><span>数据库暂时无法返回该股票的一周滚动研判记忆。</span></div>';
      this.q("#ai-conclusion-memory-count").textContent = "读取失败";
      if (this.state.conclusionView === "memory") this.showAiConclusionView("memory");
    }
  }

  renderAiNewsAnalysisRecords(records, opportunity, windowDays = 7, meta = {}) {
    const currentOpportunityTotal = Math.max(0, Number(meta.currentOpportunityTotal || 0));
    const excludedTotal = Math.max(0, Number(meta.excludedTotal || 0));
    if (!records.length) return `<div class="ai-memory-empty"><strong>${this.escape(opportunity.symbol)} 暂无可信 AI 新闻分析记录</strong><span>${excludedTotal ? `已隐藏 ${excludedTotal} 条未通过股票关联校验的历史记录。` : `功能启用后，通过关联校验的新分析会写入 ${windowDays} 天滚动记忆。`}</span></div>`;
    const directionLabel = (value) => ({ bull: "偏多", bear: "偏空", neutral: "中性" })[value] || "中性";
    const effectLabel = (value) => ({ initial: "首次判断", maintain: "维持判断", strengthen: "增强判断", weaken: "减弱判断", reverse: "判断反转" })[value] || "完成回溯";
    const positionEffectLabel = (value) => ({ hold: "维持", strengthen: "增强", caution: "谨慎", exit: "退出", reverse: "反向" })[value] || "未调整";
    const textItems = (value) => (Array.isArray(value) ? value : []).map((item) => String(item || "").trim()).filter(Boolean).slice(0, 5);
    const evidenceList = (items, emptyText) => items.length
      ? `<ul>${items.map((item) => `<li>${this.escape(item)}</li>`).join("")}</ul>`
      : `<p class="empty">${this.escape(emptyText)}</p>`;
    const chain = records.map((record, index) => {
      const direction = ["bull", "bear", "neutral"].includes(record.direction) ? record.direction : "neutral";
      const effect = ["initial", "maintain", "strengthen", "weaken", "reverse"].includes(record.memory_effect) ? record.memory_effect : "maintain";
      const memoryLinkStatus = ["initial", "linked", "context_missing"].includes(record.memory_link_status) ? record.memory_link_status : (record.prior_record_id ? "linked" : "initial");
      const effectText = memoryLinkStatus === "context_missing" ? "前序记忆遗漏" : effectLabel(effect);
      const title = record.news_title || record.news_original_title || "未命名新闻";
      const link = this.safeUrl(record.news_link);
      const titleMarkup = link ? `<a href="${this.escape(link)}" target="_blank" rel="noopener noreferrer">${this.escape(title)}</a>` : `<h4>${this.escape(title)}</h4>`;
      const prior = record.prior_record_id ? `承接 #${this.escape(record.prior_record_id)}` : (memoryLinkStatus === "context_missing" ? "旧记录：前序记忆未进入模型" : "无前序判断");
      const basis = record.judgment_basis && typeof record.judgment_basis === "object" ? record.judgment_basis : {};
      const modelFacts = textItems(basis.key_facts);
      const facts = modelFacts.length ? modelFacts : textItems([title, record.news_summary]);
      const supporting = textItems(basis.supporting_evidence);
      const counter = textItems(basis.counter_evidence);
      const uncertainties = textItems(basis.uncertainties);
      const previousConfidence = record.previous_confidence == null ? null : Math.round(Number(record.previous_confidence || 0) * 100);
      const currentConfidence = Math.round(Number(record.confidence || 0) * 100);
      const confidenceDelta = previousConfidence == null ? (memoryLinkStatus === "context_missing" ? "无法比较" : "首次判断") : `${currentConfidence - previousConfidence >= 0 ? "+" : ""}${currentConfidence - previousConfidence} pct`;
      const historicalComparison = record.memory_reason || "当时未保存历史对照说明";
      const impactMechanism = basis.impact_mechanism || record.analysis_reason || "当时未保存影响传导说明";
      const decisionSummary = basis.decision_summary || `形成${directionLabel(direction)}判断，置信度 ${currentConfidence}%。`;
      const positionImpact = record.position_effect ? `<div class="ai-memory-position-basis"><strong>持仓影响</strong><b>${positionEffectLabel(record.position_effect)}</b><span>${this.escape(record.position_reason || "当时未保存持仓调整说明")}</span></div>` : "";
      const evidenceGroups = supporting.length || counter.length || uncertainties.length ? `<div class="ai-memory-evidence-groups">
        <section class="support"><header>支持因素 <b>${supporting.length}</b></header>${evidenceList(supporting, "无额外支持因素")}</section>
        <section class="counter"><header>反向证据 <b>${counter.length}</b></header>${evidenceList(counter, "未识别到明确反向证据")}</section>
        <section class="uncertainty"><header>不确定性 <b>${uncertainties.length}</b></header>${evidenceList(uncertainties, "未单独记录不确定性")}</section>
      </div>` : '<p class="ai-memory-legacy-note">旧记录未保存结构化支持因素、反向证据和不确定性；系统不会根据当前信息反向补造。</p>';
      const symbolContextCount = Math.max(0, Number(record.symbol_context_count || 0));
      const sharedContextCount = Math.max(0, Number(record.shared_context_count || 0));
      return `<article class="ai-memory-record ${direction} effect-${effect} memory-${memoryLinkStatus}">
        <div class="ai-memory-line"><i></i><span>${String(records.length - index).padStart(2, "0")}</span></div>
        <div class="ai-memory-card">
          <header><div><span>${this.escape(record.news_source || "未知来源")}</span><time>${this.formatUnix(record.news_published_at)}</time>${record.belongs_to_opportunity ? '<em>本机会关联</em>' : ""}</div><strong class="${memoryLinkStatus === "context_missing" ? "context-missing" : effect}">${effectText}</strong></header>
          ${titleMarkup}
          <section class="ai-memory-verdict"><b class="${direction}">${directionLabel(direction)}</b><span>置信度 ${Math.round(Number(record.confidence || 0) * 100)}%</span><span>关联度 ${Math.round(Number(record.relevance || 0) * 100)}%</span><span>${this.impactLabel(record.impact_strength)}</span><span>${this.horizonLabel(record.time_horizon)}</span></section>
          <section class="ai-memory-reasoning">
            <header><strong>判断依据与过程</strong><span>可审计理由摘要，不包含模型隐藏思维链</span></header>
            <div class="ai-memory-reasoning-steps">
              <article><b>01</b><div><strong>${modelFacts.length ? "事实输入" : "原始新闻输入"}</strong>${evidenceList(facts, "旧记录未保存逐项事实依据")}</div></article>
              <article><b>02</b><div><strong>影响传导</strong><p>${this.escape(impactMechanism)}</p></div></article>
              <article><b>03</b><div><strong>历史对照</strong><p>${this.escape(historicalComparison)}</p><small>${record.previous_direction ? `${directionLabel(record.previous_direction)} ${previousConfidence}% → ${directionLabel(direction)} ${currentConfidence}%` : (memoryLinkStatus === "context_missing" ? "前序记忆未进入当时模型上下文" : "无前序判断")} · ${confidenceDelta}</small></div></article>
              <article><b>04</b><div><strong>结论形成</strong><p>${this.escape(decisionSummary)}</p></div></article>
            </div>
            ${evidenceGroups}${positionImpact}
          </section>
          <footer><span>${prior}</span><span>${this.escape(record.symbol || "--")} 前序记忆 ${symbolContextCount} 条</span><span>批次公共记忆 ${sharedContextCount} 条</span><span>模型 ${this.escape(record.model_name || "--")}</span><time>分析 ${this.formatDate(record.analyzed_at)}</time></footer>
        </div>
      </article>`;
    }).join("");
    const filterNote = excludedTotal ? `已隐藏 ${excludedTotal} 条未通过当前股票关联校验的历史记录。` : "所有展示记录均已通过当前股票关联校验。";
    return `<section class="ai-memory-panel"><header><div><span class="eyebrow">AI NEWS MEMORY</span><h3>${this.escape(opportunity.symbol)} · 一周新闻研判追踪</h3><p>七天记忆与当前机会采用不同口径；当前机会关联 ${currentOpportunityTotal} 条。${filterNote}</p></div><strong>${records.length}<small>条七天记忆</small></strong></header><div class="ai-memory-summary"><span>追踪窗口 <b>${windowDays} 天</b></span><span>当前机会 <b>${currentOpportunityTotal} 条</b></span><span>最新方向 <b class="${this.escape(records[0].direction || "neutral")}">${directionLabel(records[0].direction)}</b></span><span>已过滤历史污染 <b>${excludedTotal} 条</b></span></div><div class="ai-memory-timeline">${chain}</div></section>`;
  }

  async openNewsSystemPrompt(trigger) {
    this.newsSystemPromptFocus = trigger || null;
    const requestId = ++this.newsSystemPromptRequestId;
    const modal = this.q("#news-system-prompt-modal");
    const input = this.q("#news-system-prompt-input");
    const saveButton = this.q("#news-system-prompt-save");
    const state = this.q("#news-system-prompt-state");
    const status = this.q("#news-system-prompt-status");
    input.value = "";
    input.disabled = true;
    saveButton.disabled = true;
    state.textContent = "正在读取";
    state.className = "loading";
    status.textContent = "";
    status.className = "news-system-prompt-status";
    this.updateNewsSystemPromptCount();
    modal.classList.remove("hidden");
    modal.setAttribute("aria-hidden", "false");
    this.q("#news-system-prompt-close").focus({ preventScroll: true });
    try {
      const data = await this.api("/news-system-prompt");
      if (requestId !== this.newsSystemPromptRequestId || modal.classList.contains("hidden")) return;
      this.newsSystemPromptDefault = String(data.default_system_prompt || "").trim();
      this.newsSystemPromptIsCustom = data.is_custom === true;
      input.value = String(data.system_prompt || this.newsSystemPromptDefault);
      state.textContent = this.newsSystemPromptIsCustom ? "自定义提示词生效中" : "当前使用系统默认模板";
      state.className = this.newsSystemPromptIsCustom ? "custom" : "default";
      input.disabled = false;
      saveButton.disabled = false;
      this.updateNewsSystemPromptCount();
      input.focus({ preventScroll: true });
    } catch (error) {
      if (requestId !== this.newsSystemPromptRequestId) return;
      state.textContent = "读取失败";
      state.className = "error";
      status.textContent = error.message || "系统提示词读取失败，请稍后重试。";
      status.className = "news-system-prompt-status error";
    }
  }

  updateNewsSystemPromptCount() {
    const input = this.q("#news-system-prompt-input");
    const count = this.q("#news-system-prompt-count");
    if (input && count) count.textContent = String(input.value.length);
  }

  restoreDefaultNewsSystemPrompt() {
    const input = this.q("#news-system-prompt-input");
    if (!input || !this.newsSystemPromptDefault) return;
    input.value = this.newsSystemPromptDefault;
    this.q("#news-system-prompt-status").textContent = "已载入默认模板，点击“保存并应用”后恢复默认配置。";
    this.q("#news-system-prompt-status").className = "news-system-prompt-status notice";
    this.updateNewsSystemPromptCount();
    input.focus({ preventScroll: true });
  }

  async saveNewsSystemPrompt(event) {
    event.preventDefault();
    const input = this.q("#news-system-prompt-input");
    const saveButton = this.q("#news-system-prompt-save");
    const status = this.q("#news-system-prompt-status");
    const value = input.value.trim();
    if (value.length < 40) {
      status.textContent = "系统提示词至少需要 40 个字符。";
      status.className = "news-system-prompt-status error";
      input.focus({ preventScroll: true });
      return;
    }
    saveButton.disabled = true;
    input.disabled = true;
    status.textContent = "正在保存并应用…";
    status.className = "news-system-prompt-status loading";
    try {
      const useDefault = value === this.newsSystemPromptDefault;
      const data = await this.api("/news-system-prompt", {
        method: "PUT",
        body: JSON.stringify({ system_prompt: useDefault ? null : value }),
      });
      this.newsSystemPromptDefault = String(data.default_system_prompt || this.newsSystemPromptDefault);
      this.newsSystemPromptIsCustom = data.is_custom === true;
      this.closeNewsSystemPrompt();
      this.showBanner(this.newsSystemPromptIsCustom ? "系统提示词已保存，下一批新闻分析将使用新配置。" : "已恢复默认系统提示词，下一批新闻分析将使用默认模板。", "success");
    } catch (error) {
      status.textContent = error.message || "系统提示词保存失败，请稍后重试。";
      status.className = "news-system-prompt-status error";
      saveButton.disabled = false;
      input.disabled = false;
    }
  }

  closeNewsSystemPrompt(restoreFocus = true) {
    const modal = this.q("#news-system-prompt-modal");
    if (!modal || modal.classList.contains("hidden")) return;
    modal.classList.add("hidden");
    modal.setAttribute("aria-hidden", "true");
    this.newsSystemPromptRequestId += 1;
    const focusTarget = this.newsSystemPromptFocus;
    this.newsSystemPromptFocus = null;
    if (restoreFocus && focusTarget?.isConnected) focusTarget.focus({ preventScroll: true });
  }

  async openHistoricalJudgment(trigger) {
    if (!this.conclusionOpportunity) return;
    this.historicalJudgmentFocus = trigger || null;
    const requestId = ++this.historicalJudgmentRequestId;
    const item = this.conclusionOpportunity;
    this.q("#historical-judgment-title").textContent = `${item.symbol} · 历史研判`;
    this.q("#historical-judgment-subtitle").textContent = `${item.contract_symbol} · 正在读取该机会冻结的模型调用上下文`;
    this.q("#historical-judgment-body").innerHTML = '<div class="historical-judgment-empty"><strong>正在读取连续研判上下文</strong><span>正在提取旧新闻、记忆链与当前研究持仓快照…</span></div>';
    const modal = this.q("#historical-judgment-modal");
    modal.classList.remove("hidden");
    modal.setAttribute("aria-hidden", "false");
    this.q("#historical-judgment-close").focus({ preventScroll: true });
    try {
      const data = await this.api(`/opportunities/${encodeURIComponent(item.id)}/model-calls`);
      if (requestId !== this.historicalJudgmentRequestId || this.conclusionOpportunity?.id !== item.id) return;
      const calls = Array.isArray(data.items) ? data.items : [];
      const snapshots = [];
      calls.forEach((call) => {
        const request = call?.request_json && typeof call.request_json === "object" ? call.request_json : {};
        const messages = Array.isArray(request.messages) ? request.messages : [];
        messages.filter((message) => message?.role === "user").forEach((message) => {
          try {
            const input = typeof message.content === "string" ? JSON.parse(message.content) : message.content;
            const memory = Array.isArray(input?.historical_analysis_memory) ? input.historical_analysis_memory : [];
            const historicalNews = Array.isArray(input?.historical_related_news) ? input.historical_related_news : [];
            const positions = Array.isArray(input?.open_research_positions) ? input.open_research_positions : [];
            snapshots.push({ call, memory, historicalNews, positions, windowDays: Number(input?.memory_window_days || 7) });
          } catch {
            snapshots.push({ call, memory: [], historicalNews: [], positions: [], windowDays: 7, unreadable: true });
          }
        });
      });
      this.q("#historical-judgment-subtitle").textContent = `${item.contract_symbol} · ${calls.length} 次模型调用 · 仅展示当时实际发送的历史上下文`;
      this.q("#historical-judgment-body").innerHTML = this.renderHistoricalJudgment(snapshots, item, data.note || "");
    } catch (error) {
      if (requestId !== this.historicalJudgmentRequestId) return;
      this.q("#historical-judgment-body").innerHTML = `<div class="historical-judgment-empty error"><strong>历史研判读取失败</strong><span>${this.escape(error.message || "请稍后重试")}</span></div>`;
    }
  }

  renderHistoricalJudgment(snapshots, item, note = "") {
    const effectLabel = (value) => ({ initial: "首次判断", maintain: "维持", strengthen: "增强", weaken: "减弱", reverse: "反转" })[value] || "历史判断";
    const directionLabel = (value) => ({ bull: "偏多", bear: "偏空", neutral: "中性" })[value] || "中性";
    const positionEffectLabel = (value) => ({ hold: "维持", strengthen: "增强", caution: "谨慎", exit: "退出", reverse: "反向" })[value] || "未调整";
    const memoryCount = snapshots.reduce((total, snapshot) => total + snapshot.memory.length, 0);
    const oldNewsCount = snapshots.reduce((total, snapshot) => total + snapshot.historicalNews.length, 0);
    const positionCount = snapshots.reduce((total, snapshot) => total + snapshot.positions.length, 0);
    const historicalBasis = (record) => {
      const basis = record?.judgment_basis && typeof record.judgment_basis === "object" ? record.judgment_basis : {};
      const facts = (Array.isArray(basis.key_facts) ? basis.key_facts : []).map((value) => String(value || "").trim()).filter(Boolean).slice(0, 5);
      const supporting = (Array.isArray(basis.supporting_evidence) ? basis.supporting_evidence : []).map((value) => String(value || "").trim()).filter(Boolean).slice(0, 5);
      const counter = (Array.isArray(basis.counter_evidence) ? basis.counter_evidence : []).map((value) => String(value || "").trim()).filter(Boolean).slice(0, 5);
      const uncertainties = (Array.isArray(basis.uncertainties) ? basis.uncertainties : []).map((value) => String(value || "").trim()).filter(Boolean).slice(0, 5);
      const chips = (label, values) => values.length ? `<div><strong>${label}</strong>${values.map((value) => `<span>${this.escape(value)}</span>`).join("")}</div>` : "";
      if (!facts.length && !supporting.length && !counter.length && !uncertainties.length && !basis.impact_mechanism && !basis.decision_summary) return '<p class="historical-basis-empty">该旧记录未保存结构化判断依据。</p>';
      return `<section class="historical-judgment-basis"><header>当时的判断依据</header><p><strong>影响传导：</strong>${this.escape(basis.impact_mechanism || record.analysis_reason || "未保存")}</p><p><strong>结论逻辑：</strong>${this.escape(basis.decision_summary || record.analysis_reason || "未保存")}</p><div>${chips("事实", facts)}${chips("支持", supporting)}${chips("反向", counter)}${chips("不确定", uncertainties)}</div></section>`;
    };
    if (!snapshots.length) return `<div class="historical-judgment-empty"><strong>${this.escape(item.symbol)} 没有可审计的历史研判输入</strong><span>${this.escape(note || "该机会生成时尚未保存模型调用请求，系统不会用当前记录反向补造。")}</span></div>`;
    const sections = snapshots.map((snapshot, callIndex) => {
      const positions = snapshot.positions.map((position) => {
        const pnl = position.unrealized_bps == null ? Number.NaN : Number(position.unrealized_bps);
        const pnlText = Number.isFinite(pnl) ? `${pnl >= 0 ? "+" : ""}${pnl.toFixed(2)} bps` : "行情暂缺";
        return `<article class="historical-position-card ${pnl < 0 ? "loss" : "gain"}">
          <header><div><strong>${this.escape(position.symbol || "--")}</strong><span>${position.direction === "short" ? "做空" : "做多"}</span></div><b>${pnlText}</b></header>
          <div><span>入场价 <b>${position.entry_price == null ? "--" : this.compactNumber(position.entry_price)}</b></span><span>当前价 <b>${position.current_price == null ? "--" : this.compactNumber(position.current_price)}</b></span><span>组合分 <b>${position.current_combined_score == null ? "--" : Number(position.current_combined_score).toFixed(1)}</b></span></div>
          <footer><span>止损 ${position.stop_loss_price == null ? "--" : this.compactNumber(position.stop_loss_price)}</span><span>止盈 ${position.take_profit_price == null ? "--" : this.compactNumber(position.take_profit_price)}</span><time>到期 ${this.formatDate(position.due_at)}</time></footer>
        </article>`;
      }).join("");
      const oldNews = snapshot.historicalNews.map((news, index) => `<article class="historical-old-news-card">
        <header><b>#${String(index + 1).padStart(2, "0")}</b><span>${this.escape(news.source || "未知来源")}</span><time>${this.formatUnix(news.published_at)}</time></header>
        <h3>${this.escape(news.title || "未命名历史新闻")}</h3>
        <p>${this.escape(news.summary || "无历史摘要")}</p>
        <footer>${(Array.isArray(news.prior_judgments) ? news.prior_judgments : []).map((judgment) => `<span>${this.escape(judgment.symbol || "--")} · ${directionLabel(judgment.direction)} · ${effectLabel(judgment.memory_effect)}</span>`).join("")}</footer>
      </article>`).join("");
      const records = snapshot.memory.map((record, index) => `<article class="historical-judgment-card ${this.escape(record.direction || "neutral")}">
        <header><div><b>#${String(index + 1).padStart(2, "0")}</b><strong>${this.escape(record.symbol || "--")}</strong><span>${directionLabel(record.direction)}</span></div><em>${effectLabel(record.memory_effect)}</em></header>
        <h3>${this.escape(record.news_title || "未命名历史新闻")}</h3>
        <p><strong>历史结论：</strong>${this.escape(record.analysis_reason || "无研判说明")}</p>
        <p class="memory-impact"><strong>当时的记忆变化：</strong>${this.escape(record.memory_reason || "无变化说明")}</p>
        ${historicalBasis(record)}
        ${record.position_effect ? `<p class="position-impact"><strong>当时对持仓影响：</strong>${positionEffectLabel(record.position_effect)} · ${this.escape(record.position_reason || "无持仓变化说明")}</p>` : ""}
        <footer><span>置信度 ${Math.round(Number(record.confidence || 0) * 100)}%</span><span>关联度 ${Math.round(Number(record.relevance || 0) * 100)}%</span><span>${this.escape(record.impact_strength || "--")}</span><span>${this.escape(record.time_horizon || "--")}</span><time>${this.escape(record.analyzed_at || "--")}</time></footer>
      </article>`).join("");
      return `<section class="historical-judgment-snapshot"><header><div><span>MODEL CALL ${callIndex + 1}</span><h3>${this.escape(snapshot.call.model_name || "AI 模型")}</h3></div><strong>${snapshot.memory.length + snapshot.historicalNews.length + snapshot.positions.length}<small>条连续上下文 · ${snapshot.windowDays} 天</small></strong></header>
        ${positions ? `<section class="historical-context-block"><h4>当前研究持仓快照 <span>${snapshot.positions.length}</span></h4><div class="historical-position-grid">${positions}</div></section>` : ""}
        ${oldNews ? `<section class="historical-context-block"><h4>相关旧新闻 <span>${snapshot.historicalNews.length}</span></h4>${oldNews}</section>` : ""}
        ${records ? `<section class="historical-context-block"><h4>一周研判记忆链 <span>${snapshot.memory.length}</span></h4>${records}</section>` : `<div class="historical-judgment-empty compact"><strong>本次没有历史记忆链</strong><span>${snapshot.unreadable ? "User 消息不是可解析的结构化 JSON。" : "没有找到与本批新闻或当前持仓相关的旧记录。"}</span></div>`}
      </section>`;
    }).join("");
    return `<section class="historical-judgment-panel"><div class="historical-judgment-summary"><article><span>模型调用</span><b>${snapshots.length}</b></article><article><span>旧新闻 / 记忆</span><b>${oldNewsCount} / ${memoryCount}</b></article><article><span>持仓快照</span><b>${positionCount}</b></article><article><span>影响路径</span><b>连续研判 → 机会与持仓</b></article></div><p class="historical-judgment-note">模型会同时比较新新闻、相关旧新闻、一周记忆链和当前未结算研究持仓，判断原观点是维持、增强、谨慎、退出还是反向；持仓只作为上下文，不能反过来充当方向证据。</p>${sections}</section>`;
  }

  closeHistoricalJudgment(restoreFocus = true) {
    const modal = this.q("#historical-judgment-modal");
    if (!modal || modal.classList.contains("hidden")) return;
    modal.classList.add("hidden");
    modal.setAttribute("aria-hidden", "true");
    this.historicalJudgmentRequestId += 1;
    const focusTarget = this.historicalJudgmentFocus;
    this.historicalJudgmentFocus = null;
    if (restoreFocus && focusTarget?.isConnected) focusTarget.focus({ preventScroll: true });
  }

  openNewsAnalysisLogic(trigger) {
    if (!this.conclusionOpportunity) return;
    this.newsLogicFocus = trigger || null;
    this.newsModelCalls = [];
    this.newsModelCallIndex = 0;
    this.q("#news-logic-title").textContent = `${this.conclusionOpportunity.symbol} · 新闻分析逻辑`;
    this.q("#news-logic-subtitle").textContent = `${this.conclusionOpportunity.contract_symbol} · 正在读取模型调用审计记录`;
    this.q("#news-logic-body").innerHTML = '<div class="news-model-call-loading">正在读取实际提示词与模型原始响应…</div>';
    const modal = this.q("#news-logic-modal");
    modal.classList.remove("hidden");
    modal.setAttribute("aria-hidden", "false");
    this.q("#news-logic-close").focus({ preventScroll: true });
    this.loadNewsModelCalls(this.conclusionOpportunity);
  }

  async loadNewsModelCalls(item) {
    const requestId = ++this.newsModelCallsRequestId;
    try {
      const data = await this.api(`/opportunities/${encodeURIComponent(item.id)}/model-calls`);
      if (requestId !== this.newsModelCallsRequestId || this.conclusionOpportunity?.id !== item.id) return;
      this.newsModelCalls = Array.isArray(data.items) ? data.items : [];
      this.newsModelCallIndex = 0;
      this.newsModelCallsNote = data.note || "";
      this.renderNewsAnalysisLogic();
    } catch (error) {
      if (requestId !== this.newsModelCallsRequestId || this.conclusionOpportunity?.id !== item.id) return;
      this.q("#news-logic-body").innerHTML = `<div class="news-model-call-empty error"><strong>模型调用记录读取失败</strong><span>${this.escape(error.message || "请稍后重试")}</span></div>`;
    }
  }

  renderNewsAnalysisLogic() {
    const item = this.conclusionOpportunity;
    if (!item) return;
    const calls = this.newsModelCalls;
    this.q("#news-logic-title").textContent = `${item.symbol} · 新闻分析逻辑`;
    this.q("#news-logic-subtitle").textContent = `${item.contract_symbol} · 实际提示词与模型原始返回`;
    if (!calls.length) {
      this.q("#news-logic-body").innerHTML = `<div class="news-model-call-empty"><strong>该机会没有原始模型调用记录</strong><span>${this.escape(this.newsModelCallsNote || "该机会生成时尚未启用原始调用审计；重新分析关联新闻后即可查看。")}</span><small>系统不会根据解析结果伪造历史提示词或原始响应。</small></div>`;
      return;
    }
    const selectedIndex = Math.max(0, Math.min(this.newsModelCallIndex, calls.length - 1));
    this.newsModelCallIndex = selectedIndex;
    const call = calls[selectedIndex];
    const request = call.request_json && typeof call.request_json === "object" ? call.request_json : {};
    const messages = Array.isArray(request.messages) ? request.messages : [];
    const systemPrompt = messages.filter((entry) => entry?.role === "system").map((entry) => String(entry.content || "")).join("\n\n");
    const userPrompt = messages.filter((entry) => entry?.role === "user").map((entry) => String(entry.content || "")).join("\n\n");
    const requestParameters = { ...request };
    delete requestParameters.messages;
    const rawResponse = call.response_text || "";
    const responseEnvelope = call.response_envelope || "";
    const tabs = calls.map((entry, index) => `<button class="${index === selectedIndex ? "active" : ""}" type="button" data-news-call-index="${index}"><span>调用 ${String(index + 1).padStart(2, "0")}</span><strong>${this.escape(entry.model_name || "未知模型")}</strong><small>${this.escape((entry.news_ids || []).length)} 条新闻 · ${this.formatDate(entry.started_at)}</small></button>`).join("");
    const promptBlock = (title, role, content, key) => `<article class="news-model-raw-block"><header><div><span>${role}</span><h3>${title}</h3></div><button type="button" data-copy-call-section="${key}">复制原文</button></header><pre><code>${this.escape(content || "（空）")}</code></pre></article>`;
    this.q("#news-logic-body").innerHTML = `
      <nav class="news-model-call-tabs" aria-label="模型调用记录">${tabs}</nav>
      <section class="news-model-call-meta">
        <article><span>服务商</span><b>${this.escape(call.provider_code || "--")}</b></article>
        <article><span>模型</span><b>${this.escape(call.model_name || "--")}</b></article>
        <article><span>新闻数量</span><b>${(call.news_ids || []).length} 条</b></article>
        <article><span>调用状态</span><b class="${call.status === "completed" ? "success" : "failed"}">${call.status === "completed" ? "调用成功" : `调用失败 · ${this.escape(call.error_category || "unknown")}`}</b></article>
        <article><span>调用时间</span><b>${this.formatDate(call.started_at)}</b></article>
      </section>
      <section class="news-model-audit-note"><strong>数据库原始审计</strong><span>以下内容是本次请求实际发送和收到的原文；认证请求头与 API Key 从未写入记录。</span><em>批次 ${this.escape(call.batch_id || "--")}</em></section>
      ${promptBlock("System 提示词", "SYSTEM", systemPrompt, "system")}
      ${promptBlock("User 提示词 / 新闻输入", "USER", userPrompt, "user")}
      ${promptBlock("请求参数", "REQUEST", this.prettyJson(requestParameters), "request")}
      ${promptBlock("模型返回原始文本", "RAW OUTPUT", rawResponse || "模型未返回可读取的 message content", "response")}
      ${promptBlock("服务商完整响应包", "HTTP BODY", this.prettyJson(responseEnvelope) || "服务商未返回响应正文", "envelope")}`;
    this.q("#news-logic-body").scrollTop = 0;
  }

  prettyJson(value) {
    if (value == null || value === "") return "";
    try {
      const parsed = typeof value === "string" ? JSON.parse(value) : value;
      return JSON.stringify(parsed, null, 2);
    } catch {
      return String(value);
    }
  }

  async copyNewsModelCallSection(section, button) {
    const call = this.newsModelCalls[this.newsModelCallIndex];
    if (!call) return;
    const request = call.request_json && typeof call.request_json === "object" ? call.request_json : {};
    const messages = Array.isArray(request.messages) ? request.messages : [];
    const values = {
      system: messages.filter((entry) => entry?.role === "system").map((entry) => String(entry.content || "")).join("\n\n"),
      user: messages.filter((entry) => entry?.role === "user").map((entry) => String(entry.content || "")).join("\n\n"),
      request: this.prettyJson(request),
      response: String(call.response_text || ""),
      envelope: this.prettyJson(call.response_envelope),
    };
    const value = values[section];
    if (value == null) return;
    try {
      await navigator.clipboard.writeText(value);
      const original = button.textContent;
      button.textContent = "已复制";
      window.setTimeout(() => { if (button.isConnected) button.textContent = original; }, 1200);
    } catch {
      this.showBanner("浏览器未允许复制，请直接选择代码块内容。", "error");
    }
  }

  closeNewsAnalysisLogic(restoreFocus = true) {
    const modal = this.q("#news-logic-modal");
    if (!modal || modal.classList.contains("hidden")) return;
    modal.classList.add("hidden");
    modal.setAttribute("aria-hidden", "true");
    this.newsModelCallsRequestId += 1;
    this.newsModelCalls = [];
    const focusTarget = this.newsLogicFocus;
    this.newsLogicFocus = null;
    if (restoreFocus && focusTarget?.isConnected) focusTarget.focus({ preventScroll: true });
  }

  renderAiRelatedNewsList(items, opportunity, compactEvidence = false) {
    if (!items.length) return '<div class="ai-related-news-empty"><strong>暂无相关新闻</strong><span>当前机会记录中没有可展示的关联新闻。</span></div>';
    const cards = items.map((item) => {
      const relatedStocks = Array.isArray(item.related_us_stocks) ? item.related_us_stocks : [];
      const relatedIndustries = Array.isArray(item.related_industries) ? item.related_industries : [];
      const stock = relatedStocks.find((entry) => String(entry.symbol || "").toUpperCase().replace(/(?:USDT|USD1)$/i, "") === opportunity.symbol) || {};
      const confidence = Number(item.ai_confidence ?? item.confidence ?? 0);
      const relevance = Number(stock.relevance ?? item.relevance ?? 0);
      const score = Number(item.score ?? confidence * relevance);
      const rawDirection = String(stock.direction || item.direction || "").toLowerCase();
      const directionClass = ["bear", "bearish", "short"].includes(rawDirection) ? "bear" : ["bull", "bullish", "long"].includes(rawDirection) ? "bull" : "neutral";
      const directionLabel = directionClass === "bear" ? "偏空" : directionClass === "bull" ? "偏多" : "中性";
      const title = item.title_zh || item.title || "未命名新闻";
      const link = this.safeUrl(item.link);
      const titleMarkup = link ? `<a href="${this.escape(link)}" target="_blank" rel="noopener noreferrer">${this.escape(title)}</a>` : `<h3>${this.escape(title)}</h3>`;
      const reason = item.ai_reason || item.reason || item.summary || "AI 未提供进一步说明";
      const industries = relatedIndustries.slice(0, 5).map((entry) => `<span>${this.escape(entry.name || entry.industry || entry.label || "相关行业")}</span>`).join("");
      const stocks = relatedStocks.slice(0, 6).map((entry) => `<span class="${this.escape(String(entry.direction || "neutral").toLowerCase())}">${this.escape(entry.symbol || "--")} <b>${Math.round(Number(entry.relevance || 0) * 100)}%</b></span>`).join("");
      return `<article class="ai-related-news-card ${directionClass}">
        <header><div><span>${this.escape(item.source || "未知来源")}</span><time>${this.formatUnix(item.ts)}</time></div><strong>${Math.round(score * 100)}%</strong></header>
        ${titleMarkup}
        <p>${this.escape(reason)}</p>
        <div class="ai-related-news-tags"><span class="sentiment-pill ${directionClass}">${directionLabel}</span><span>${this.impactLabel(item.ai_impact_strength)}</span><span>${this.horizonLabel(item.ai_time_horizon)}</span><span>${this.categoryLabel(item.ai_category)}</span><span>AI 置信度 ${Math.round(confidence * 100)}%</span><span>关联度 ${Math.round(relevance * 100)}%</span></div>
        ${!compactEvidence && (industries || stocks) ? `<div class="ai-related-news-relations"><div><em>行业</em><div>${industries || "无直接关联行业"}</div></div><div><em>美股</em><div>${stocks || "无其他关联美股"}</div></div></div>` : ""}
      </article>`;
    }).join("");
    return `<section class="ai-related-news-panel"><header><div><span class="eyebrow">RELATED NEWS</span><h3>${this.escape(opportunity.symbol)} 相关新闻</h3><p>这里展示生成该机会时关联的全部新闻及其 AI 研判依据。</p></div><strong>${items.length}<small>条新闻</small></strong></header><div class="ai-related-news-list">${cards}</div></section>`;
  }

  async loadAiFundamentals(item) {
    const requestId = ++this.conclusionFundamentalRequestId;
    try {
      const data = await this.api(`/opportunities/${encodeURIComponent(item.id)}/fundamentals`);
      if (requestId !== this.conclusionFundamentalRequestId || this.conclusionOpportunity?.id !== item.id) return;
      this.conclusionPanels.fundamentals = this.renderAiFundamentals(data, item);
      this.q("#ai-conclusion-fundamental-state").textContent = data.available ? "数据库资料" : "暂无资料";
      if (this.state.conclusionView === "fundamentals") this.showAiConclusionView("fundamentals");
    } catch {
      if (requestId !== this.conclusionFundamentalRequestId || this.conclusionOpportunity?.id !== item.id) return;
      this.conclusionPanels.fundamentals = '<div class="ai-fundamental-empty"><strong>基本面读取失败</strong><span>数据库暂时无法返回该股票的基本面资料。</span></div>';
      this.q("#ai-conclusion-fundamental-state").textContent = "读取失败";
      if (this.state.conclusionView === "fundamentals") this.showAiConclusionView("fundamentals");
    }
  }

  renderAiFundamentals(data, opportunity) {
    if (!data?.available) return `<div class="ai-fundamental-empty"><strong>${this.escape(opportunity.symbol)} 暂无基本面资料</strong><span>证券资料库尚未收录该标的。</span></div>`;
    const security = data.security || {};
    const profile = data.profile || {};
    const analysis = data.analysis || {};
    const financials = data.financials || {};
    const evidence = analysis.evidence || {};
    const companyName = security.company_name_zh || security.company_name || profile.legal_name || opportunity.symbol;
    const legalName = profile.legal_name || security.company_name || "--";
    const typeLabel = ({ COMMON_STOCK: "普通股", ETF: "ETF", ADR: "ADR", UNKNOWN: "待核验" })[security.security_type] || security.security_type || "--";
    const industry = profile.industry_zh || profile.industry || "待补充";
    const sector = profile.sector_zh || profile.sector || "待补充";
    const marketCap = this.formatMarketCapMillions(profile.market_cap, security.currency || "USD");
    const shares = profile.shares_outstanding == null ? "--" : `${this.compactNumber(profile.shares_outstanding)} M`;
    const employeeCount = profile.employee_count == null ? "--" : this.number(profile.employee_count);
    const score = (value) => value == null ? "--" : Number(value).toFixed(1);
    const confidence = analysis.confidence_score == null ? "--" : `${Math.round(Number(analysis.confidence_score) * 100)}%`;
    const complete = evidence.fundamental_data_complete === true || evidence.financial_metrics_complete === true;
    const financialStatus = financials.data_status || evidence.financial_metrics_status || "PARTIAL";
    const financialCurrency = financials.currency || security.currency || "USD";
    const financialStatusLabel = financialStatus === "NOT_APPLICABLE" ? "非公司财务口径" : financialStatus === "COMPLETE" ? "完整基本面" : "基本面待核验";
    const financialStatusCopy = financialStatus === "NOT_APPLICABLE" ? "该标的不是经营性公司，营收、利润率、现金流、债务和公司估值已明确标记为不适用。" : financialStatus === "COMPLETE" ? "营收、利润率、现金流、负债与估值指标均已接入数据库。" : "部分财务分类仍缺少可核验的公开数据。";
    const unavailable = financialStatus === "COMPLETE" || financialStatus === "NOT_APPLICABLE" ? "N/A" : "--";
    const amount = (value) => value == null ? unavailable : this.formatFinancialAmount(value, financialCurrency);
    const percent = (value) => value == null ? unavailable : `${Number(value).toFixed(2)}%`;
    const ratio = (value, suffix = "x") => value == null ? unavailable : `${Number(value).toFixed(2)}${suffix}`;
    const website = this.safeUrl(profile.website);
    const websiteMarkup = website ? `<a href="${this.escape(website)}" target="_blank" rel="noopener noreferrer">公司网站 ↗</a>` : '<span>公司网站待补充</span>';
    const sections = [
      ["业务概况", analysis.business_summary || profile.description],
      ["成长分析", analysis.growth_analysis],
      ["盈利能力", analysis.profitability_analysis],
      ["估值分析", analysis.valuation_analysis],
      ["风险分析", analysis.risk_analysis],
    ].filter(([, content]) => content).map(([title, content]) => `<article><h4>${title}</h4><p>${this.escape(content)}</p></article>`).join("");
    const listItems = (values) => (Array.isArray(values) ? values : []).map((value) => `<li>${this.escape(typeof value === "string" ? value : value?.title || value?.summary || JSON.stringify(value))}</li>`).join("");
    const catalysts = listItems(analysis.catalysts);
    const risks = listItems(analysis.risk_factors);
    return `<section class="ai-fundamental-panel">
      <header><div><span class="eyebrow">FUNDAMENTAL PROFILE</span><h3>${this.escape(companyName)} <small>${this.escape(opportunity.symbol)}</small></h3><p>${this.escape(legalName)}</p></div><strong>${score(analysis.overall_score)}<small>综合评分</small></strong></header>
      <div class="ai-fundamental-status ${complete ? "complete" : "baseline"}"><strong>${financialStatusLabel}</strong><span>${financialStatusCopy}</span><em>覆盖率 ${financials.coverage_pct == null ? "--" : `${Number(financials.coverage_pct).toFixed(0)}%`} · 置信度 ${confidence}</em></div>
      <section class="ai-fundamental-metrics">
        <article><span>总市值</span><b>${this.escape(marketCap)}</b><small>数据库口径</small></article>
        <article><span>所属行业</span><b>${this.escape(industry)}</b><small>${this.escape(sector)}</small></article>
        <article><span>证券类型</span><b>${this.escape(typeLabel)}</b><small>${this.escape(security.exchange || "US")} · ${this.escape(security.currency || "USD")}</small></article>
        <article><span>上市日期</span><b>${this.escape(profile.ipo_date || "--")}</b><small>${this.escape(security.country || "国家待补充")}</small></article>
        <article><span>流通股</span><b>${this.escape(shares)}</b><small>百万股</small></article>
        <article><span>员工人数</span><b>${this.escape(employeeCount)}</b><small>公司档案</small></article>
      </section>
      <section class="ai-fundamental-financials">
        <article><span>TTM 营收</span><b>${amount(financials.revenue_ttm)}</b><small>同比 ${percent(financials.revenue_growth_yoy_pct)}</small></article>
        <article><span>毛利率</span><b>${percent(financials.gross_margin_pct)}</b><small>营业利润率 ${percent(financials.operating_margin_pct)}</small></article>
        <article><span>净利率</span><b>${percent(financials.net_margin_pct)}</b><small>ROE ${percent(financials.return_on_equity_pct)}</small></article>
        <article><span>经营现金流</span><b>${amount(financials.operating_cash_flow_ttm)}</b><small>TTM / 最新年报</small></article>
        <article><span>自由现金流</span><b>${amount(financials.free_cash_flow_ttm)}</b><small>经营现金流减资本开支</small></article>
        <article><span>现金及等价物</span><b>${amount(financials.cash_and_equivalents)}</b><small>最近报告期</small></article>
        <article><span>总债务</span><b>${amount(financials.total_debt)}</b><small>债务权益比 ${ratio(financials.debt_to_equity, "")}</small></article>
        <article><span>估值</span><b>P/E ${ratio(financials.pe_ratio, "")}</b><small>P/S ${ratio(financials.price_to_sales_ratio, "")} · P/B ${ratio(financials.price_to_book_ratio, "")}</small></article>
        <article><span>企业价值</span><b>${amount(financials.enterprise_value)}</b><small>EV/EBITDA ${ratio(financials.ev_to_ebitda, "")}</small></article>
        <article><span>资产 / 负债</span><b>${amount(financials.total_assets)}</b><small>负债 ${amount(financials.total_liabilities)}</small></article>
      </section>
      <section class="ai-fundamental-score-grid">
        <article><span>质量</span><b>${score(analysis.quality_score)}</b></article><article><span>成长</span><b>${score(analysis.growth_score)}</b></article><article><span>估值</span><b>${score(analysis.valuation_score)}</b></article><article><span>财务健康</span><b>${score(analysis.financial_health_score)}</b></article><article><span>综合</span><b>${score(analysis.overall_score)}</b></article>
      </section>
      <section class="ai-fundamental-analysis"><header><h3>基本面分析</h3><small>截至 ${this.escape(analysis.as_of_date || "--")}</small></header><div>${sections || '<article><h4>分析状态</h4><p>基本面分析内容待补充。</p></article>'}</div></section>
      ${catalysts || risks ? `<section class="ai-fundamental-lists">${catalysts ? `<article><h3>潜在催化</h3><ul>${catalysts}</ul></article>` : ""}${risks ? `<article><h3>风险因素</h3><ul>${risks}</ul></article>` : ""}</section>` : ""}
      <footer><span>资料来源：${this.escape(financials.source || profile.source || "数据库")}</span><span>财务快照：${this.escape(financials.snapshot_date || "--")} · 报告期：${this.escape(financials.fiscal_period_end || "--")}</span><span>更新：${this.formatDate(financials.retrieved_at || profile.source_updated_at || security.updated_at)}</span>${websiteMarkup}</footer>
    </section>`;
  }

  closeAiConclusion() {
    const modal = this.q("#ai-conclusion-modal");
    if (!modal || modal.classList.contains("hidden")) return;
    this.closeNewsAnalysisLogic(false);
    this.closeNewsSystemPrompt(false);
    this.closeHistoricalJudgment(false);
    modal.classList.add("hidden");
    modal.setAttribute("aria-hidden", "true");
    this.conclusionNewsRequestId += 1;
    this.conclusionMemoryRequestId += 1;
    this.conclusionFundamentalRequestId += 1;
    this.conclusionOpportunity = null;
    const focusTarget = this.conclusionFocus;
    this.conclusionFocus = null;
    if (focusTarget?.isConnected) focusTarget.focus({ preventScroll: true });
  }

  async loadPredictionAnalytics({ scrollToList = false } = {}) {
    const requestId = ++this.state.predictionAnalyticsRequestId;
    try {
      const filters = this.state.predictionFilters;
      const params = new URLSearchParams({
        limit: String(this.state.predictionPageSize),
        page: String(this.state.predictionPage),
        news_score_min: String(filters.newsScoreMin),
        indicator_score_min: String(filters.indicatorScoreMin),
        direction: filters.direction,
      });
      const applyButton = this.q("#prediction-filter-apply");
      if (applyButton) applyButton.disabled = true;
      const data = await this.api(`/opportunity-analytics?${params}`);
      if (requestId !== this.state.predictionAnalyticsRequestId) return;
      this.state.opportunityAnalytics = data;
      this.state.predictionPage = Number(data.pagination?.page || this.state.predictionPage || 1);
      this.renderPredictionAnalytics();
      if (scrollToList) window.requestAnimationFrame(() => this.q("#prediction-list")?.scrollIntoView({ behavior: "smooth", block: "start" }));
    } catch (error) {
      this.showBanner(error.message || "预测统计分析读取失败", "error");
    } finally {
      const applyButton = this.q("#prediction-filter-apply");
      if (applyButton && requestId === this.state.predictionAnalyticsRequestId) applyButton.disabled = false;
    }
  }

  applyPredictionFilters() {
    const clampScore = (value) => Math.min(100, Math.max(0, Number(value) || 0));
    const directionValue = this.q("#prediction-direction").value;
    this.state.predictionFilters = {
      newsScoreMin: clampScore(this.q("#prediction-news-score-min").value),
      indicatorScoreMin: clampScore(this.q("#prediction-indicator-score-min").value),
      direction: ["long", "short"].includes(directionValue) ? directionValue : "all",
    };
    this.q("#prediction-news-score-min").value = String(this.state.predictionFilters.newsScoreMin);
    this.q("#prediction-indicator-score-min").value = String(this.state.predictionFilters.indicatorScoreMin);
    this.state.predictionPage = 1;
    this.loadPredictionAnalytics({ scrollToList: true });
  }

  resetPredictionFilters() {
    this.state.predictionFilters = { newsScoreMin: 0, indicatorScoreMin: 0, direction: "all" };
    this.q("#prediction-news-score-min").value = "0";
    this.q("#prediction-indicator-score-min").value = "0";
    this.q("#prediction-direction").value = "all";
    this.state.predictionPage = 1;
    this.loadPredictionAnalytics({ scrollToList: true });
  }

  setPredictionPage(page) {
    const totalPages = Number(this.state.opportunityAnalytics?.pagination?.total_pages || 1);
    const nextPage = Math.min(totalPages, Math.max(1, Number(page) || 1));
    if (nextPage === this.state.predictionPage) return;
    this.state.predictionPage = nextPage;
    this.loadPredictionAnalytics({ scrollToList: true });
  }

  async startHistoricalReplay(event) {
    event.preventDefault();
    if (this.historicalReplayActive()) return;
    const button = this.q("#replay-start");
    button.disabled = true;
    try {
      const payload = {
        days: Number(this.q("#replay-days").value) || 365,
        timeframe: this.q("#replay-timeframe").value,
        symbols: [],
      };
      const replay = await this.api("/replays", { method: "POST", body: JSON.stringify(payload) });
      const replayData = this.state.historicalReplays || {};
      const previousItems = Array.isArray(replayData.items) ? replayData.items : [];
      this.state.historicalReplays = {
        ...replayData,
        items: [replay, ...previousItems.filter((item) => item.id !== replay.id)],
      };
      this.renderHistoricalReplay();
      this.showBanner("历史回放已启动；任务会从币安官方归档补采并校验数据。", "success");
      await this.loadPredictionAnalytics();
    } catch (error) {
      this.showBanner(error.message || "历史回放启动失败", "error");
    } finally {
      this.syncHistoricalReplayButton();
    }
  }

  historicalReplayActive() {
    const items = this.state.historicalReplays?.items || [];
    return items.some((item) => ["pending", "running"].includes(item.status));
  }

  syncHistoricalReplayButton() {
    const button = this.q("#replay-start");
    if (button) button.disabled = this.historicalReplayActive();
  }

  renderHistoricalReplay() {
    const replayData = this.state.historicalReplays || {};
    const items = replayData.items || [];
    const latest = items[0] || null;
    const readiness = (this.state.opportunityAnalytics || {}).historical_replay_readiness || replayData.readiness || {};
    const status = this.q("#replay-status");
    const labels = { pending: "排队中", running: "正在回放", completed: "已完成", failed: "失败", cancelled: "已取消" };
    status.textContent = latest ? `${labels[latest.status] || latest.status} · ${latest.completed_symbols}/${latest.total_symbols} 品种` : "尚未运行";
    status.className = latest?.status || "idle";
    this.syncHistoricalReplayButton();
    const criteria = Array.isArray(readiness.criteria) ? readiness.criteria : [];
    const progress = latest && latest.total_symbols ? Math.round(latest.completed_symbols / latest.total_symbols * 100) : 0;
    this.q("#replay-result").innerHTML = `
      <div class="replay-progress"><span style="width:${progress}%"></span></div>
      <div class="replay-summary">
        <article><span>任务进度</span><b>${latest ? `${progress}%` : "--"}</b><small>${latest ? `${this.number(latest.generated_signals)} 个去重信号` : "等待首次运行"}</small></article>
        <article><span>样本外信号</span><b>${this.number(readiness.oos_summary?.sample_count)}</b><small>多 ${this.number(readiness.oos_summary?.long_count)} / 空 ${this.number(readiness.oos_summary?.short_count)}</small></article>
        <article><span>样本外平均 ${this.state.displayLeverage}x 净收益率</span><b class="${Number(readiness.oos_summary?.average_net_return_bps || 0) > 0 ? "positive" : "negative"}">${readiness.oos_summary?.average_net_return_bps == null ? "--" : this.formatLeveragedReturnFromBps(readiness.oos_summary.average_net_return_bps)}</b><small>标的 ${readiness.oos_summary?.average_net_return_bps == null ? "--" : this.formatBps(readiness.oos_summary.average_net_return_bps)} · 强制成本后</small></article>
        <article><span>样本外准入</span><b class="${readiness.quantitative_ready ? "positive" : "negative"}">${readiness.passed_count || 0} / ${readiness.total_count || 8}</b><small>${readiness.quantitative_ready ? "可进入影子验证" : "仍限研究"}</small></article>
      </div>
      ${criteria.length ? `<div class="replay-criteria">${criteria.map((item) => `<span class="${item.passed ? "passed" : "blocked"}">${item.passed ? "✓" : "×"} ${this.escape(item.label)} <b>${item.current == null ? "--" : this.escape(item.current)}</b></span>`).join("")}</div>` : `<p class="replay-note">${this.escape(readiness.note || "完成回放后显示样本外准入结果。")}</p>`}
      ${latest?.error ? `<p class="replay-error">${this.escape(latest.error)}</p>` : ""}`;
  }

  renderPredictionAnalytics() {
    const data = this.state.opportunityAnalytics || {};
    const summary = data.summary || {};
    const items = data.items || [];
    const readiness = data.readiness || {};
    const costConfig = data.cost_config || {};
    const metricBps = (value) => value == null ? "--" : this.formatBps(value);
    const hitRate = summary.hit_rate == null ? "--" : `${Number(summary.hit_rate).toFixed(1)}%`;
    const filters = data.filters || {};
    const directionLabel = ({ long: "做多", short: "做空", all: "全部方向" })[filters.direction] || "全部方向";
    const filterResult = this.q("#prediction-filter-result");
    if (filterResult) filterResult.textContent = `${this.number(summary.historical_count)} 条 · ${directionLabel} · 新闻 ≥ ${Number(filters.news_score_min || 0).toFixed(0)} · 指标 ≥ ${Number(filters.indicator_score_min || 0).toFixed(0)}`;
    const costTotal = this.q("#prediction-cost-total");
    if (costTotal && costConfig.example_one_hour_total_bps != null) costTotal.textContent = `1h 往返 ${Number(costConfig.example_one_hour_total_bps).toFixed(2)} bps`;
    const readinessTarget = this.q("#strategy-readiness");
    if (readinessTarget) {
      const criteria = Array.isArray(readiness.criteria) ? readiness.criteria : [];
      readinessTarget.innerHTML = `
        <header><div><span>LIVE READINESS GATE</span><h3>${readiness.quantitative_ready ? "量化门槛已通过，可进入影子验证" : "当前仅限研究，尚未达到影子验证门槛"}</h3><p>${this.escape(readiness.note || "量化门槛通过后仍需模拟盘、影子运行和人工审批。")}</p></div><strong class="${readiness.quantitative_ready ? "passed" : "blocked"}">${this.number(readiness.passed_count)} / ${this.number(readiness.total_count)}<small>通过项</small></strong></header>
        <div class="readiness-criteria">${criteria.map((item) => `<article class="${item.passed ? "passed" : "blocked"}"><span>${item.passed ? "✓" : "×"} ${this.escape(item.label)}</span><b>${item.current == null ? "--" : this.escape(item.current)}</b><small>要求 ${this.escape(item.required)}</small></article>`).join("")}</div>
        <footer><strong>实盘仍需完成</strong>${(readiness.paper_and_shadow_requirements || []).map((item) => `<span>${this.escape(item)}</span>`).join("")}</footer>`;
    }
    const exitPolicyTarget = this.q("#adaptive-exit-policy");
    if (exitPolicyTarget) {
      const exitCounts = summary.exit_reason_counts || {};
      const protectedCount = Number(exitCounts.profit_lock || 0) + Number(exitCounts.trailing_profit || 0);
      exitPolicyTarget.innerHTML = `
        <header><div><span>ADAPTIVE EXIT GUARD V4</span><h3>盈利保护与失败跟随早退</h3><p>所有退出只使用当时已经收盘的 15 分钟 K 线；不会用同一根 K 线的未来高低点回写结果。</p></div><strong>因果回放<small>${this.escape(summary.settlement_policy_version || "--")}</small></strong></header>
        <div>
          <article class="profit"><span>浮盈保护</span><b>+20 bps</b><small>前一根 K 线确认后冻结保护线 · 已触发 ${this.number(exitCounts.profit_lock)}</small></article>
          <article class="profit"><span>移动保护</span><b>峰值 -30 bps</b><small>峰值达到 50 bps 后启用 · 已触发 ${this.number(exitCounts.trailing_profit)}</small></article>
          <article class="risk"><span>跟随失败</span><b>3 × 15m</b><small>未曾浮盈 20 bps 且亏损达到 15 bps · 已触发 ${this.number(exitCounts.failed_follow_through)}</small></article>
          <article><span>保护退出合计</span><b>${this.number(protectedCount)}</b><small>ATR 硬止损、2R 止盈和评分反转仍然保留</small></article>
        </div>`;
    }
    this.q("#prediction-note").textContent = `${data.note || "按历史预测的成本后结果统计，不执行任何下单。"} 页面收益率按 ${this.state.displayLeverage} 倍杠杆换算展示，并保留标的原始收益；不改变信号或下单逻辑。`;
    this.q("#analytics-summary").innerHTML = `
      <article><span>筛选样本</span><strong>${this.number(summary.historical_count)}</strong><small>等待 ${this.number(summary.pending_count)} · 已剔除行情不足 ${this.number(summary.discarded_unavailable_count)} · 旧口径剔除 ${this.number(summary.excluded_legacy_settlement_count)}</small></article>
      <article><span>多 / 空方向</span><strong class="analytics-directions"><b>${this.number(summary.long_count)}</b><i>/</i><em>${this.number(summary.short_count)}</em></strong><small>做多 / 做空样本分布</small></article>
      <article class="positive"><span>命中次数</span><strong>${this.number(summary.win_count)}</strong><small>未命中 ${this.number(summary.loss_count)} · 持平 ${this.number(summary.flat_count)}</small></article>
      <article class="${Number(summary.hit_rate || 0) >= 50 ? "positive" : "negative"}"><span>命中概率</span><strong>${hitRate}</strong><small>命中 ÷ 有方向结果</small></article>
      <article class="${Number(summary.average_directional_return_bps || 0) >= 0 ? "positive" : "negative"}"><span>平均 ${this.state.displayLeverage}x 净收益率</span><strong>${this.formatLeveragedReturnFromBps(summary.average_directional_return_bps)}</strong><small>标的净 ${metricBps(summary.average_directional_return_bps)} · 毛 ${metricBps(summary.average_gross_return_bps)}</small><small>${this.state.displayLeverage}x 成本 ${this.formatLeveragedReturnFromBps(-Math.abs(Number(summary.average_estimated_cost_bps || 0)))} · 费/滑点/资金已计入</small></article>
      <article><span>平均 MFE / MAE</span><strong class="analytics-range"><b>${metricBps(summary.average_max_favorable_bps)}</b><i>${metricBps(summary.average_max_adverse_bps)}</i></strong><small>持有期最大有利 / 不利波动</small></article>
      <article><span>影子候选 / 研究</span><strong class="analytics-directions"><b>${this.number(summary.shadow_ready_count)}</b><i>/</i><em>${this.number(summary.research_only_count)}</em></strong><small>生成信号时的准入状态</small></article>`;
    const target = this.q("#prediction-list");
    if (!items.length) { target.innerHTML = '<div class="empty-state opportunity-empty"><strong>没有符合当前筛选条件的历史机会</strong><span>请降低评分下限、切换方向或重置筛选。</span></div>'; return; }
    target.innerHTML = `<div class="table-wrap"><table><thead><tr><th>信号时间</th><th>股票 / 合约</th><th>方向</th><th>技术状态</th><th>新闻 / 指标评分</th><th>入场价格</th><th>退出价格 / 原因</th><th>${this.state.displayLeverage}x 毛 / 净收益率</th><th>MFE / MAE</th><th>成本后结果</th><th>计划 / 实际持有</th></tr></thead><tbody>${items.map((item) => `<tr><td>${this.formatDate(item.signal_time)}</td><td><strong>${this.escape(item.symbol)}</strong><small>${this.escape(item.contract_symbol)}</small></td><td><span class="prediction-direction ${item.direction === "short" ? "short" : ""}">${item.direction === "long" ? "做多" : "做空"}</span></td><td><span class="technical-state ${item.technical_confirmed ? "confirmed" : "candidate"}">${item.technical_confirmed ? "技术已确认" : "新闻候选"}</span></td><td>${Number(item.news_score).toFixed(1)} / ${Number(item.indicator_score).toFixed(1)}<small>组合 ${Number(item.combined_score).toFixed(1)}</small></td><td>${item.entry_price == null ? "--" : this.escape(this.compactNumber(item.entry_price))}</td><td>${item.exit_price == null ? "--" : this.escape(this.compactNumber(item.exit_price))}<small>${this.exitReasonLabel(item.exit_reason, item.exit_detail)} · ${item.settled_price_at ? this.formatDate(item.settled_price_at) : "退出行情不足"}</small></td><td class="${Number(item.directional_return_bps || 0) >= 0 ? "positive" : "negative"}">${this.formatLeveragedReturnFromBps(item.gross_directional_return_bps)}<small>净 ${this.formatLeveragedReturnFromBps(item.net_directional_return_bps)} · 标的净 ${metricBps(item.net_directional_return_bps)}</small><small>${this.state.displayLeverage}x 成本 ${this.formatLeveragedReturnFromBps(-Math.abs(Number(item.estimated_cost_bps || 0)))} · 费/滑点/资金已计入</small></td><td>${metricBps(item.max_favorable_bps)}<small>${metricBps(item.max_adverse_bps)}</small></td><td><span class="prediction-result ${this.escape(item.result || "unavailable")}">${this.analyticsResultLabel(item.result)}</span></td><td>${this.formatDate(item.expires_at)}<small>计划 ${this.number(item.max_holding_bars || 1)} 根 · ${this.formatDuration(item.signal_time, item.expires_at)}</small><small>实际 ${this.formatDuration(item.signal_time, item.settled_price_at || item.exit_at)}</small></td></tr>`).join("")}</tbody></table></div>`;
    target.insertAdjacentHTML("beforeend", this.predictionPaginationMarkup(data.pagination || {}, items.length));
  }

  predictionPaginationMarkup(pagination, visibleCount) {
    const page = Math.max(1, Number(pagination.page) || 1);
    const pageSize = Math.max(1, Number(pagination.page_size) || this.state.predictionPageSize);
    const total = Math.max(0, Number(pagination.total) || 0);
    const totalPages = Math.max(1, Number(pagination.total_pages) || Math.ceil(total / pageSize) || 1);
    const pageEntries = [];
    if (totalPages <= 7) {
      for (let value = 1; value <= totalPages; value += 1) pageEntries.push(value);
    } else {
      const candidates = [...new Set([1, page - 1, page, page + 1, totalPages].filter((value) => value >= 1 && value <= totalPages))].sort((left, right) => left - right);
      candidates.forEach((value, index) => {
        if (index && value - candidates[index - 1] > 1) pageEntries.push("ellipsis");
        pageEntries.push(value);
      });
    }
    return `<nav class="prediction-pagination" aria-label="预测统计分析分页">
      <span>共 <strong>${this.number(total)}</strong> 条 · 每页 ${this.number(pageSize)} 条 · 当前显示 ${this.number(visibleCount)} 条</span>
      <div>
        <button type="button" data-prediction-page="${page - 1}" ${page <= 1 ? "disabled" : ""}>上一页</button>
        ${pageEntries.map((entry) => entry === "ellipsis" ? '<i aria-hidden="true">…</i>' : `<button type="button" class="${entry === page ? "active" : ""}" data-prediction-page="${entry}" ${entry === page ? 'aria-current="page" disabled' : ""}>${entry}</button>`).join("")}
        <button type="button" data-prediction-page="${page + 1}" ${page >= totalPages ? "disabled" : ""}>下一页</button>
      </div>
      <small>第 ${this.number(page)} / ${this.number(totalPages)} 页</small>
    </nav>`;
  }

  showBanner(message, tone = "success") {
    const target = this.q("#ai-banner");
    target.textContent = message;
    target.className = `ai-banner ${tone}`;
    window.clearTimeout(this.bannerTimer);
    this.bannerTimer = window.setTimeout(() => target.classList.add("hidden"), 6000);
  }

  number(value) { return new Intl.NumberFormat("zh-CN").format(Number(value || 0)); }
  safeUrl(value) { try { const url = new URL(String(value || "")); return ["http:", "https:"].includes(url.protocol) ? url.href : ""; } catch { return ""; } }
  compactNumber(value) { return Number(value).toLocaleString("en-US", { maximumFractionDigits: 6 }); }
  formatMarketCapMillions(value, currency = "USD") { const numeric = Number(value); if (!Number.isFinite(numeric) || numeric <= 0) return "--"; const prefix = currency === "USD" ? "$" : `${currency} `; if (numeric >= 1_000_000) return `${prefix}${(numeric / 1_000_000).toFixed(2)}T`; if (numeric >= 1_000) return `${prefix}${(numeric / 1_000).toFixed(2)}B`; return `${prefix}${numeric.toFixed(2)}M`; }
  formatFinancialAmount(value, currency = "USD") { const numeric = Number(value); if (!Number.isFinite(numeric)) return "--"; const prefix = currency === "USD" ? "$" : `${currency} `; const absolute = Math.abs(numeric); if (absolute >= 1e12) return `${prefix}${(numeric / 1e12).toFixed(2)}T`; if (absolute >= 1e9) return `${prefix}${(numeric / 1e9).toFixed(2)}B`; if (absolute >= 1e6) return `${prefix}${(numeric / 1e6).toFixed(2)}M`; return `${prefix}${numeric.toLocaleString("en-US", { maximumFractionDigits: 0 })}`; }
  formatLeveragedReturnFromBps(value) { if (value == null) return "--"; const numeric = Number(value); return Number.isFinite(numeric) ? this.signedMetric((numeric / 100) * this.state.displayLeverage, 2, "%") : "--"; }
  formatLeveragedReturnFromPercent(value) { if (value == null) return "--"; const numeric = Number(value); return Number.isFinite(numeric) ? this.signedMetric(numeric * this.state.displayLeverage, 2, "%") : "--"; }
  formatBps(value) { const numeric = Number(value); return `${numeric > 0 ? "+" : ""}${numeric.toFixed(2)} bps`; }
  formatUnix(value) { const numeric = Number(value); return numeric ? this.formatDate(new Date(numeric * (numeric > 1e12 ? 1 : 1000))) : "--"; }
  parseDate(value) {
    if (value instanceof Date) return value;
    if (!value) return new Date(Number.NaN);
    const raw = String(value).trim();
    const normalized = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}/.test(raw) && !/(?:Z|[+-]\d{2}:?\d{2})$/i.test(raw) ? `${raw}Z` : raw;
    return new Date(normalized);
  }
  formatDate(value) { if (!value) return "--"; const date = this.parseDate(value); return Number.isNaN(date.getTime()) ? "--" : new Intl.DateTimeFormat("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false }).format(date); }
  formatDuration(start, end) { const milliseconds = this.parseDate(end).getTime() - this.parseDate(start).getTime(); if (!Number.isFinite(milliseconds) || milliseconds <= 0) return "--"; const minutes = Math.round(milliseconds / 60000); return minutes >= 60 && minutes % 60 === 0 ? `${minutes / 60} 小时` : `${minutes} 分钟`; }
  runTypeLabel(value) { return value === "news" ? "新闻分析" : "机会扫描"; }
  statusLabel(value) { return ({ pending: "等待执行", running: "运行中", completed: "已完成", partial: "部分完成", failed: "失败", skipped: "已跳过" })[value] || value || "未知"; }
  sentimentLabel(value) { return ({ bull: "偏多", bear: "偏空", neutral: "中性" })[value] || "已分析"; }
  sentimentClass(value) { return ({ bull: "bull", bear: "bear", neutral: "neutral" })[value] || "pending"; }
  impactLabel(value) { return ({ high: "高影响", medium: "中影响", low: "低影响" })[value] || "影响待定"; }
  horizonLabel(value) { return ({ intraday: "日内", short_term: "短期", medium_term: "中期", long_term: "长期" })[value] || "周期待定"; }
  categoryLabel(value) { return ({ macro: "宏观", company: "公司", earnings: "财报", policy: "政策", geopolitics: "地缘", commodity: "商品", crypto: "加密", other: "其他" })[value] || "分类待定"; }
  analyticsResultLabel(value) { return ({ win: "命中", loss: "未命中", flat: "持平", unavailable: "行情不足" })[value] || "行情不足"; }
  exitReasonLabel(value, detail = "") {
    const detailLabels = { profit_lock: "浮盈保护退出", trailing_profit: "移动止盈退出", failed_follow_through: "跟随失败早退" };
    if (detailLabels[detail]) return detailLabels[detail];
    return ({ take_profit: "触发止盈", stop_loss: "触发止损", score_breakdown: "综合评分转弱", score_reversal: "方向反转", max_holding_time: "最大持有期退出", legacy_horizon_close: "旧版到期结算" })[value] || "退出原因待确认";
  }
}

if (!window.customElements.get("ai-monitor-dashboard")) {
  window.customElements.define("ai-monitor-dashboard", AiMonitorDashboard);
}
