class StrategyCenter extends HTMLElement {
  constructor() {
    super();
    this.initialized = false;
    this.started = false;
    this.loading = false;
    this.sessionGeneration = 0;
    this.loadVersion = 0;
    this.items = [];
    this.templates = [];
    this.indicators = [];
    this.indicatorDefaults = {};
    this.sourceRuntime = {};
    this.selectedIndicators = new Map();
    this.deployments = [];
    this.signals = [];
    this.section = "strategies";
    this.query = "";
    this.statusFilter = "all";
    this.categoryFilter = "all";
    this.activeItem = null;
    this.editorMode = "edit";
    this.editScope = "parameters";
    this.createMode = "indicators";
    this.preview = null;
    this.codeBuffers = {};
    this.sourceWorkbenchDirty = false;
    this.sourceWorkbenchTab = "ai";
    this.sourceParameterSchema = [];
    this.sourceParameterValues = {};
    this.sourceRiskDefaults = {};
    this.sourceBacktestCatalog = null;
    this.sourceBacktestResult = null;
    this.sourceBacktestRunning = false;
    this.sourceBacktestScope = "single";
    this.sourceBacktestSuite = [];
    this.sourceBacktestFailures = [];
    this.sourceBacktestAnalysis = null;
    this.sourceOptimizationValidation = null;
    this.sourceComposition = null;
    this.sourceCompositionDirty = false;
    this.aiConversation = [];
    this.aiPromptQueue = [];
    this.aiBusy = false;
    this.aiTurnSequence = 0;
    this.aiWorkingSource = "";
    this.aiWorkingSpec = null;
    this.aiWorkingProposed = null;
    this.aiWorkingProfile = null;
    this.aiWorkingComposition = null;
    this.aiProcessSteps = [];
  }

  connectedCallback() {
    if (this.initialized) return;
    this.initialized = true;
    this.renderShell();
    this.bindEvents();
  }

  renderShell() {
    this.innerHTML = `
      <main class="strategy-center-shell">
        <header class="strategy-center-head">
          <div class="strategy-title-copy">
            <span class="strategy-kicker">STRATEGY LIBRARY</span>
            <div class="strategy-title-line"><span class="strategy-title-mark" aria-hidden="true">策</span><h1>策略中心</h1><span class="strategy-db-badge"><i></i>数据库同步</span></div>
            <p>完整策略负责市场环境、入场、退出与风控；也可从指标库组合生成可回测、可运行的新策略。</p>
          </div>
          <button id="strategy-create" class="strategy-create-button" type="button"><span aria-hidden="true">＋</span>新增策略</button>
        </header>

        <div id="strategy-notice" class="strategy-notice hidden" role="status" aria-live="polite"></div>

        <nav class="strategy-section-tabs" aria-label="策略中心功能">
          <button class="active" type="button" data-section="strategies" aria-pressed="true"><span>完整策略</span><strong id="strategy-tab-full">0</strong></button>
          <button type="button" data-section="indicators" aria-pressed="false"><span>指标库</span><strong id="strategy-tab-indicators">0</strong></button>
          <button type="button" data-section="deployments" aria-pressed="false"><span>运行部署</span><strong id="strategy-tab-deployments">0</strong></button>
          <button type="button" data-section="signals" aria-pressed="false"><span>信号记录</span><strong id="strategy-tab-signals">0</strong></button>
          <button type="button" data-section="legacy" aria-pressed="false"><span>旧版策略</span><strong id="strategy-tab-legacy">0</strong></button>
        </nav>

        <section class="strategy-overview" aria-label="策略统计">
          <article><span>完整策略</span><strong id="strategy-total">--</strong><small>具备入场、退出与风控</small></article>
          <article><span>运行中部署</span><strong id="strategy-active">--</strong><small>模拟盘按账户独立运行</small></article>
          <article><span>标准指标</span><strong id="strategy-defaults">--</strong><small>统一计算口径与版本</small></article>
          <article><span>指标组合策略</span><strong id="strategy-latest">--</strong><small>由两个以上指标组成</small></article>
        </section>

        <section class="strategy-library-card">
          <header class="strategy-library-toolbar">
            <label class="strategy-search"><span aria-hidden="true">⌕</span><input id="strategy-search" type="search" autocomplete="off" placeholder="搜索策略名称、分类或说明" aria-label="搜索策略"></label>
            <div id="strategy-status-filter" class="strategy-segments" aria-label="策略状态筛选">
              <button class="active" type="button" data-status="all" aria-pressed="true">全部</button>
              <button type="button" data-status="default" aria-pressed="false">默认副本</button>
              <button type="button" data-status="custom" aria-pressed="false">我新增的</button>
            </div>
            <label class="strategy-category-picker"><span>分类</span><select id="strategy-category-filter" aria-label="按策略分类筛选"><option value="all">全部分类</option></select></label>
            <button id="strategy-refresh" class="strategy-refresh-button" type="button" aria-label="刷新策略列表">刷新</button>
          </header>
          <div id="strategy-grid" class="strategy-card-grid" aria-live="polite" aria-busy="true"></div>
        </section>
      </main>

      <div id="strategy-dialog-layer" class="strategy-dialog-layer hidden" aria-hidden="true">
        <section class="strategy-editor" role="dialog" aria-modal="true" aria-labelledby="strategy-editor-title">
          <header class="strategy-editor-head">
            <div><span id="strategy-editor-kicker">EDIT STRATEGY</span><h2 id="strategy-editor-title">编辑策略</h2><p id="strategy-editor-subtitle">保存后将生成新版本，回测会读取最新配置。</p></div>
            <div class="strategy-editor-head-actions">
              <div id="strategy-workbench-actions" class="strategy-workbench-actions hidden">
                <span id="strategy-workbench-dirty" class="strategy-workbench-dirty clean"><i></i><b>已同步</b></span>
                <button id="strategy-workbench-validate" class="strategy-quiet-button" type="button">校验源码</button>
                <button id="strategy-workbench-save" class="strategy-save-button" type="button">保存版本</button>
                <button id="strategy-workbench-run" class="strategy-run-button" type="button"><span aria-hidden="true">▶</span><strong>保存并回测</strong></button>
              </div>
              <button id="strategy-editor-close" class="strategy-close-button" type="button" aria-label="关闭策略编辑器">×</button>
            </div>
          </header>

          <div class="strategy-editor-body">
            <form id="strategy-form" class="strategy-form" novalidate>
              <div id="strategy-version-strip" class="strategy-version-strip"><span>当前版本</span><strong id="strategy-editor-version">--</strong><small>乐观锁保护</small></div>

              <div id="strategy-edit-scope-block" class="strategy-form-block strategy-edit-scope-block hidden">
                <div class="strategy-section-heading"><div><span>00</span><strong>维护方式</strong></div><small id="strategy-edit-scope-help">选择修改完整策略代码或仅调整参数</small></div>
                <div id="strategy-edit-scope" class="strategy-segments strategy-edit-scope" aria-label="策略维护方式">
                  <button id="strategy-source-scope" class="active" type="button" data-edit-scope="source" aria-pressed="true">Python 源码</button>
                  <button type="button" data-edit-scope="parameters" aria-pressed="false">参数配置</button>
                  <button id="strategy-dsl-scope" class="hidden" type="button" data-edit-scope="code" aria-pressed="false" tabindex="-1">DSL 配置</button>
                </div>
              </div>

              <div id="strategy-source-composition-block" class="strategy-form-block strategy-source-composition-block hidden">
                <div class="strategy-section-heading"><div><span>01</span><strong>指标与运行约束</strong></div><small>由 AI 根据自然语言自动选择，可人工复核</small></div>
                <p class="strategy-composition-copy">每轮对话都会由 AI 重新判断指标组合、周期、方向和参数。你仍可手工微调；手工结构变化需重新生成并复核源码草稿。</p>
                <div id="strategy-source-indicator-picker" class="strategy-indicator-picker"></div>
                <div class="strategy-compose-settings">
                  <label>运行周期<select id="strategy-source-timeframe"><option value="15m">15 分钟</option><option value="1h">1 小时</option><option value="4h">4 小时</option></select></label>
                  <label>确认阈值 (%)<input id="strategy-source-confirmation-threshold" type="number" min="1" max="100" step="1" value="60"></label>
                  <label>信号有效 K 线<input id="strategy-source-signal-valid-bars" type="number" min="1" max="10" step="1" value="2"></label>
                  <fieldset class="strategy-direction-picker"><legend>允许方向</legend><label><input id="strategy-source-direction-long" type="checkbox" checked>做多</label><label><input id="strategy-source-direction-short" type="checkbox" checked>做空</label></fieldset>
                </div>
                <div id="strategy-source-selected-indicators" class="strategy-selected-indicators"></div>
                <div class="strategy-composition-actions">
                  <span id="strategy-source-composition-state">已与当前源码同步</span>
                  <button id="strategy-source-composition-ai" class="strategy-quiet-button" type="button">按当前选择重新生成源码</button>
                </div>
              </div>

              <div id="strategy-composer-block" class="strategy-form-block hidden">
                <div class="strategy-section-heading"><div><span>01</span><strong id="strategy-create-mode-title">选择指标</strong></div><small id="strategy-create-mode-help">至少选择 2 个，参数会动态出现</small></div>
                <div id="strategy-create-mode" class="strategy-segments strategy-create-mode" aria-label="策略创建方式">
                  <button class="active" type="button" data-create-mode="indicators" aria-pressed="true">指标组合</button>
                  <button type="button" data-create-mode="template" aria-pressed="false">系统模板</button>
                  <button type="button" data-create-mode="source" aria-pressed="false">Python 源码</button>
                </div>
                <div id="strategy-indicator-composer">
                  <div id="strategy-indicator-picker" class="strategy-indicator-picker"></div>
                  <div class="strategy-compose-settings">
                    <label>运行周期<select id="strategy-timeframe"><option value="15m">15 分钟</option><option value="1h">1 小时</option><option value="4h">4 小时</option></select></label>
                    <label>确认阈值 (%)<input id="strategy-confirmation-threshold" type="number" min="1" max="100" step="1" value="60"></label>
                    <label>信号有效 K 线<input id="strategy-signal-valid-bars" type="number" min="1" max="10" step="1" value="2"></label>
                    <fieldset class="strategy-direction-picker"><legend>允许方向</legend><label><input id="strategy-direction-long" type="checkbox" checked>做多</label><label><input id="strategy-direction-short" type="checkbox" checked>做空</label></fieldset>
                  </div>
                  <div id="strategy-selected-indicators" class="strategy-selected-indicators"></div>
                </div>
                <div id="strategy-template-composer" class="strategy-template-composer hidden">
                  <label>系统策略模板<select id="strategy-template" aria-label="系统策略模板"></select></label>
                  <p>复制受约束的系统模板，随后可在当前账户中独立调整参数和风险默认值。</p>
                </div>
              </div>

              <div id="strategy-basic-block" class="strategy-form-block">
                <div class="strategy-section-heading"><div><span id="strategy-basic-index">01</span><strong>基本信息</strong></div><small>AI 自动拟定，可人工修改</small></div>
                <div class="strategy-field-grid two">
                  <label>策略名称<input id="strategy-name" type="text" minlength="2" maxlength="64" autocomplete="off" required></label>
                  <label>策略分类<input id="strategy-category" type="text" minlength="2" maxlength="32" autocomplete="off" required></label>
                </div>
                <label>策略说明<textarea id="strategy-description" rows="3" maxlength="500" placeholder="说明入场逻辑、适用行情与风险边界"></textarea></label>
              </div>

              <div id="strategy-code-block" class="strategy-form-block strategy-code-block hidden">
                <div class="strategy-section-heading"><div><span>02</span><strong id="strategy-code-title">策略 DSL 配置</strong></div><small id="strategy-code-runtime">JSON · 同一运行时校验 · 保存即生成新版本</small></div>
                <label><span id="strategy-code-label">完整策略 DSL</span><span class="strategy-code-frame"><span id="strategy-code-lines" class="strategy-code-lines" aria-hidden="true">1</span><textarea id="strategy-code-editor" class="strategy-code-editor" rows="22" spellcheck="false" autocomplete="off" aria-label="策略代码编辑器"></textarea></span></label>
                <div class="strategy-code-actions">
                  <button id="strategy-code-format" class="strategy-quiet-button" type="button">格式化</button>
                  <button id="strategy-code-validate" class="strategy-quiet-button" type="button">校验代码</button>
                  <span id="strategy-code-status" class="strategy-code-status">尚未校验</span>
                </div>
                <p id="strategy-code-guard" class="strategy-code-guard">DSL 是声明式配置，不是底层源码。Python 源码在独立进程中运行，并禁止文件、网络与系统调用。</p>
              </div>

              <div id="strategy-parameters-block" class="strategy-form-block">
                <div class="strategy-section-heading"><div><span id="strategy-parameters-index">02</span><strong>策略参数</strong></div><small id="strategy-parameters-help">由 Python 源码中的 PARAMETERS 动态生成</small></div>
                <div id="strategy-parameter-fields" class="strategy-field-grid two"></div>
              </div>

              <div id="strategy-risk-block" class="strategy-form-block">
                <div class="strategy-section-heading"><div><span id="strategy-risk-index">03</span><strong>风险默认值</strong></div><small>回测时仍可按次调整</small></div>
                <div id="strategy-risk-fields" class="strategy-field-grid two"></div>
              </div>

              <div id="strategy-form-error" class="strategy-form-error hidden" role="alert"></div>
              <div class="strategy-form-actions">
                <button id="strategy-cancel" class="strategy-quiet-button" type="button">取消</button>
                <button id="strategy-save" class="strategy-save-button" type="submit"><span aria-hidden="true">✓</span><strong>保存策略</strong></button>
              </div>
            </form>

            <aside id="strategy-ai-panel" class="strategy-ai-panel">
              <nav id="strategy-workbench-tabs" class="strategy-workbench-tabs hidden" aria-label="源码策略工具">
                <button class="active" type="button" data-workbench-tab="ai" aria-pressed="true">AI 助手</button>
                <button type="button" data-workbench-tab="backtest" aria-pressed="false">回测与结果</button>
              </nav>

              <section id="strategy-ai-pane" class="strategy-workbench-pane strategy-ai-pane">
                <header>
                  <div><span>AI STRATEGY COMPOSER</span><h3 id="strategy-ai-title">用自然语言修改策略</h3></div>
                  <span id="strategy-ai-status" class="strategy-ai-status"><i></i>受约束配置</span>
                </header>
                <p id="strategy-ai-description">描述你想调整的逻辑或风险参数。创建策略时，模型会把已选指标与自然语言规则生成完整、可编辑的 Python 源码。</p>
                <section class="strategy-ai-conversation" aria-label="AI 连续优化会话">
                  <header><div><strong>连续优化会话</strong><small>每一轮都基于上一轮草稿继续调整</small></div><span id="strategy-ai-turn-count">0 轮</span></header>
                  <div id="strategy-ai-messages" class="strategy-ai-messages" aria-live="polite"></div>
                </section>
                <section id="strategy-ai-process" class="strategy-ai-process hidden" aria-live="polite">
                  <header><strong>分析与校验进度</strong><small>展示可审计处理阶段与结论摘要，不展示模型隐式推理</small></header>
                  <div id="strategy-ai-process-steps" class="strategy-ai-process-steps"></div>
                </section>
                <label>自然语言要求<textarea id="strategy-ai-prompt" rows="5" maxlength="1200" placeholder="例如：做一个 1 小时趋势策略，用 EMA、MACD 和成交量确认，减少噪声并把止损控制在 3%。"></textarea></label>
                <div class="strategy-ai-examples" aria-label="AI 编辑示例">
                  <button type="button" data-ai-example="创建 1 小时趋势策略，使用 EMA、MACD、ADX 和成交量确认，参数偏稳健。">趋势组合</button>
                  <button type="button" data-ai-example="创建 15 分钟反转策略，使用 RSI、布林带和 ATR 过滤，减少噪声。">反转组合</button>
                  <button type="button" data-ai-example="创建 4 小时突破策略，使用 Donchian、ADX 和成交量确认，只做多。">突破组合</button>
                </div>
                <button id="strategy-ai-preview-button" class="strategy-ai-preview-button" type="button"><span aria-hidden="true">✦</span><strong>发送给 AI</strong></button>
                <div class="strategy-safety-note"><span aria-hidden="true">!</span><p><strong>只生成源码预览，不执行交易</strong>。源码必须通过服务端安全校验并保存版本，之后仍需通过回测与模拟盘验证。</p></div>

                <section id="strategy-ai-preview" class="strategy-ai-preview hidden" aria-live="polite">
                  <header><div><span id="strategy-ai-provider">--</span><strong>修改预览</strong></div><span id="strategy-ai-base-version">--</span></header>
                  <p id="strategy-ai-summary"></p>
                  <div id="strategy-ai-changes" class="strategy-change-list"></div>
                  <div class="strategy-ai-actions">
                    <button id="strategy-ai-discard" class="strategy-quiet-button" type="button">放弃预览</button>
                    <button id="strategy-ai-apply" class="strategy-ai-apply-button" type="button"><span aria-hidden="true">✓</span><strong>确认应用</strong></button>
                  </div>
                </section>
                <div id="strategy-ai-error" class="strategy-form-error hidden" role="alert"></div>
              </section>

              <section id="strategy-backtest-pane" class="strategy-workbench-pane strategy-backtest-pane hidden">
                <header class="strategy-runner-head"><div><span>STRATEGY REPLAY</span><h3>源码回测</h3></div><span id="strategy-runner-status" class="strategy-runner-status idle"><i></i>等候配置</span></header>
                <p class="strategy-runner-copy">运行前会校验并保存当前源码为不可变版本，再使用历史 K 线按下一根开盘价撮合。</p>
                <div id="strategy-runner-notice" class="strategy-runner-notice hidden" role="status"></div>
                <div class="strategy-runner-scope" aria-label="回测品种范围">
                  <button class="active" type="button" data-runner-scope="single" aria-pressed="true"><strong>单品种</strong><small>搜索并测试一个合约</small></button>
                  <button type="button" data-runner-scope="all" aria-pressed="false"><strong>全品种</strong><small id="strategy-runner-universe-count">读取可用品种…</small></button>
                </div>
                <div class="strategy-runner-fields">
                  <label id="strategy-runner-symbol-field" class="strategy-runner-symbol-field"><span>交易品种</span><input id="strategy-runner-symbol-search" type="search" autocomplete="off" placeholder="输入代码搜索，例如 NVDA" aria-label="搜索回测品种"><select id="strategy-runner-symbol" size="1" aria-label="交易品种"><option value="">读取行情目录…</option></select><small id="strategy-runner-symbol-match">等待行情目录</small></label>
                  <label>触发周期（源码）<select id="strategy-runner-timeframe" title="由当前已保存 Python 源码的 TRIGGER_TIMEFRAME 决定"><option value="">读取行情目录…</option></select></label>
                  <label>开始日期<input id="strategy-runner-start" type="date"></label>
                  <label>结束日期<input id="strategy-runner-end" type="date"></label>
                  <label>初始资金<input id="strategy-runner-capital" type="number" min="1" step="100" value="10000"></label>
                  <label>单次仓位 (%)<input id="strategy-runner-position" type="number" min="0.01" max="100" step="0.1" value="10"></label>
                  <label>杠杆倍数<input id="strategy-runner-leverage" type="number" min="1" max="20" step="1" value="1"></label>
                  <label>最大持有 (K)<input id="strategy-runner-holding" type="number" min="0" max="50000" step="1" value="120"></label>
                  <label>止损 (%)<input id="strategy-runner-stop" type="number" min="0" max="99.9" step="0.1" value="5"></label>
                  <label>止盈 (%)<input id="strategy-runner-take" type="number" min="0" max="99.9" step="0.1" value="10"></label>
                  <label>手续费 (bp)<input id="strategy-runner-fee" type="number" min="0" max="1000" step="0.1" value="4"></label>
                  <label>滑点 (bp)<input id="strategy-runner-slippage" type="number" min="0" max="1000" step="0.1" value="2"></label>
                </div>
                <p id="strategy-runner-risk-mode" class="strategy-runner-risk-mode"></p>
                <button id="strategy-runner-submit" class="strategy-runner-submit" type="button"><span aria-hidden="true">▶</span><strong>保存当前版本并运行回测</strong></button>
                <div id="strategy-runner-empty" class="strategy-runner-empty"><span aria-hidden="true">⌁</span><strong>尚未运行当前源码</strong><small>回测完成后将在这里显示收益、回撤、胜率、权益曲线与最近成交。</small></div>
                <section id="strategy-runner-analysis" class="strategy-runner-analysis hidden">
                  <header><div><span>CROSS-MARKET ANALYSIS</span><strong>结果分析</strong></div><b id="strategy-runner-verdict">--</b></header>
                  <div id="strategy-runner-analysis-metrics" class="strategy-runner-analysis-metrics"></div>
                  <div id="strategy-runner-insights" class="strategy-runner-insights"></div>
                  <div id="strategy-runner-ranking" class="strategy-runner-ranking"></div>
                  <button id="strategy-runner-ai-optimize" class="strategy-runner-ai-optimize" type="button"><span aria-hidden="true">✦</span><strong>AI 分析并生成优化参数</strong></button>
                  <p>AI 只生成符合当前 PARAMETERS 范围的候选参数，不会自动部署；应用后请再次运行全品种回测复验。</p>
                </section>
                <section id="strategy-runner-result" class="strategy-runner-result hidden">
                  <header><div><span id="strategy-runner-result-meta">BACKTEST COMPLETE</span><strong id="strategy-runner-result-title">--</strong></div><b id="strategy-runner-return">--</b></header>
                  <div id="strategy-runner-metrics" class="strategy-runner-metrics"></div>
                  <div class="strategy-runner-chart"><div><strong>权益曲线</strong><span>已计手续费与滑点</span></div><canvas id="strategy-runner-canvas" aria-label="源码策略回测权益曲线"></canvas></div>
                  <div id="strategy-runner-trades" class="strategy-runner-trades"></div>
                </section>
              </section>
            </aside>
          </div>
        </section>
      </div>

      <div id="strategy-detail-layer" class="strategy-dialog-layer hidden" aria-hidden="true">
        <section class="strategy-detail-dialog" role="dialog" aria-modal="true" aria-labelledby="strategy-detail-title">
          <header class="strategy-editor-head">
            <div><span>STRATEGY RECORD</span><h2 id="strategy-detail-title">策略详情</h2><p id="strategy-detail-subtitle">版本、验证结果和最近信号</p></div>
            <button id="strategy-detail-close" class="strategy-close-button" type="button" aria-label="关闭策略详情">×</button>
          </header>
          <div id="strategy-detail-content" class="strategy-detail-content"></div>
        </section>
      </div>`;
  }

  bindEvents() {
    this.querySelector("#strategy-create").addEventListener("click", () => this.openCreate());
    this.querySelector("#strategy-refresh").addEventListener("click", () => this.load(true));
    this.querySelectorAll("[data-section]").forEach((button) => button.addEventListener("click", () => {
      this.section = button.dataset.section || "strategies";
      this.querySelectorAll("[data-section]").forEach((item) => {
        const active = item === button;
        item.classList.toggle("active", active);
        item.setAttribute("aria-pressed", String(active));
      });
      this.renderSection();
    }));
    this.querySelector("#strategy-search").addEventListener("input", (event) => {
      this.query = String(event.target.value || "").trim().toLocaleLowerCase("zh-CN");
      this.renderCards();
    });
    this.querySelector("#strategy-category-filter").addEventListener("change", (event) => {
      this.categoryFilter = event.target.value || "all";
      this.renderCards();
    });
    this.querySelectorAll("[data-status]").forEach((button) => button.addEventListener("click", () => {
      this.statusFilter = button.dataset.status || "all";
      this.querySelectorAll("[data-status]").forEach((item) => {
        const active = item === button;
        item.classList.toggle("active", active);
        item.setAttribute("aria-pressed", String(active));
      });
      this.renderCards();
    }));
    this.querySelector("#strategy-editor-close").addEventListener("click", () => this.closeEditor());
    this.querySelector("#strategy-cancel").addEventListener("click", () => this.closeEditor());
    this.querySelector("#strategy-dialog-layer").addEventListener("click", (event) => {
      if (event.target === event.currentTarget) this.closeEditor();
    });
    this.querySelectorAll("[data-create-mode]").forEach((button) => button.addEventListener("click", () => {
      this.switchCreateMode(button.dataset.createMode || "indicators");
    }));
    this.querySelectorAll("[data-edit-scope]").forEach((button) => button.addEventListener("click", () => {
      void this.requestEditScope(button.dataset.editScope || "parameters");
    }));
    this.querySelector("#strategy-code-format").addEventListener("click", () => this.formatStrategyCode());
    this.querySelector("#strategy-code-validate").addEventListener("click", () => this.validateStrategyCode());
    this.querySelector("#strategy-code-editor").addEventListener("input", (event) => {
      if (["code", "source"].includes(this.editScope)) this.codeBuffers[this.editScope] = event.target.value;
      if (this.editorMode === "create" && this.createMode === "source") this.codeBuffers.source = event.target.value;
      if (this.isSourceWorkbench()) this.aiWorkingSource = event.target.value;
      this.setCodeStatus("代码已修改，尚未校验");
      this.renderCodeLines();
      if (this.isSourceWorkbench()) this.setSourceWorkbenchDirty(true);
    });
    this.querySelector("#strategy-code-editor").addEventListener("scroll", (event) => {
      this.querySelector("#strategy-code-lines").scrollTop = event.target.scrollTop;
    });
    ["#strategy-name", "#strategy-category", "#strategy-description"].forEach((selector) => {
      this.querySelector(selector).addEventListener("input", () => {
        if (this.isSourceWorkbench()) {
          this.aiWorkingProfile = {
            name: this.querySelector("#strategy-name").value,
            category: this.querySelector("#strategy-category").value,
            description: this.querySelector("#strategy-description").value,
            risk_defaults: this.collectConfig("risk", this.sourceRiskDefaults),
          };
          this.setSourceWorkbenchDirty(true);
        }
      });
    });
    const strategyForm = this.querySelector("#strategy-form");
    strategyForm.addEventListener("invalid", (event) => this.localizeFieldValidation(event.target), true);
    strategyForm.addEventListener("input", (event) => {
      if (typeof event.target?.setCustomValidity === "function") event.target.setCustomValidity("");
      this.showFormError("");
      if (!this.isSourceWorkbench() || event.target?.dataset?.configGroup !== "risk") return;
      this.sourceRiskDefaults = this.collectConfig("risk", this.sourceRiskDefaults);
      this.aiWorkingProfile = {
        ...(this.aiWorkingProfile || {}),
        name: this.querySelector("#strategy-name").value,
        category: this.querySelector("#strategy-category").value,
        description: this.querySelector("#strategy-description").value,
        risk_defaults: { ...this.sourceRiskDefaults },
      };
    });
    this.querySelector("#strategy-workbench-validate").addEventListener("click", () => this.validateStrategyCode());
    this.querySelector("#strategy-workbench-save").addEventListener("click", () => this.persistSourceWorkbench());
    this.querySelector("#strategy-workbench-run").addEventListener("click", () => this.runSourceBacktest());
    this.querySelector("#strategy-runner-submit").addEventListener("click", () => this.runSourceBacktest());
    this.querySelector("#strategy-runner-ai-optimize").addEventListener("click", () => this.optimizeSourceBacktestParameters());
    this.querySelector("#strategy-runner-symbol-search").addEventListener("input", (event) => this.filterSourceBacktestSymbols(event.target.value));
    this.querySelector("#strategy-runner-symbol").addEventListener("change", () => this.syncSourceBacktestBounds());
    this.querySelector("#strategy-runner-timeframe").addEventListener("change", () => this.syncSourceBacktestBounds());
    this.querySelectorAll("[data-runner-scope]").forEach((button) => button.addEventListener("click", () => {
      this.setSourceBacktestScope(button.dataset.runnerScope || "single");
    }));
    this.querySelectorAll("[data-workbench-tab]").forEach((button) => button.addEventListener("click", () => {
      this.switchSourceWorkbenchTab(button.dataset.workbenchTab || "backtest");
    }));
    this.querySelector("#strategy-template").addEventListener("change", (event) => this.applyTemplate(event.target.value));
    this.querySelector("#strategy-detail-close").addEventListener("click", () => this.closeDetails());
    this.querySelector("#strategy-detail-layer").addEventListener("click", (event) => {
      if (event.target === event.currentTarget) this.closeDetails();
    });
    strategyForm.addEventListener("submit", (event) => this.save(event));
    this.querySelector("#strategy-ai-preview-button").addEventListener("click", () => this.requestAiPreview());
    this.querySelector("#strategy-ai-prompt").addEventListener("keydown", (event) => {
      if ((event.ctrlKey || event.metaKey) && event.key === "Enter") {
        event.preventDefault();
        this.requestAiPreview();
      }
    });
    this.querySelector("#strategy-source-composition-block").addEventListener("input", () => this.markSourceCompositionDirty());
    this.querySelector("#strategy-source-composition-block").addEventListener("change", () => this.markSourceCompositionDirty());
    this.querySelector("#strategy-source-composition-ai").addEventListener("click", () => {
      const prompt = "请严格按照左侧当前选择的指标、周期、方向与参数，重构完整 Python 策略源码；保留合理的入场、退出与风险提案，并确保所有 PARAMETERS 都完整声明。";
      this.requestAiPreview({ prompt, forceMode: "source", autoCompose: false });
    });
    this.querySelector("#strategy-ai-discard").addEventListener("click", () => this.clearPreview());
    this.querySelector("#strategy-ai-apply").addEventListener("click", () => this.applyAiPreview());
    this.querySelectorAll("[data-ai-example]").forEach((button) => button.addEventListener("click", () => {
      this.querySelector("#strategy-ai-prompt").value = button.dataset.aiExample || "";
      this.querySelector("#strategy-ai-prompt").focus();
    }));
    document.addEventListener("keydown", (event) => {
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "s" && this.isSourceWorkbench()) {
        event.preventDefault();
        void this.persistSourceWorkbench();
        return;
      }
      if ((event.ctrlKey || event.metaKey) && event.key === "Enter" && this.isSourceWorkbench()) {
        event.preventDefault();
        void this.runSourceBacktest();
        return;
      }
      if (event.key === "Escape" && !this.querySelector("#strategy-dialog-layer").classList.contains("hidden")) this.closeEditor();
      else if (event.key === "Escape" && !this.querySelector("#strategy-detail-layer").classList.contains("hidden")) this.closeDetails();
    });
  }

  async api(path = "", options = {}) {
    if (typeof window.quantdeskApi !== "function") throw new Error("认证服务尚未就绪");
    return window.quantdeskApi(`/api/v2/strategies${path}`, options);
  }

  start() {
    if (this.started) return;
    this.started = true;
    void this.load();
  }

  pause() {
    // 筛选状态保留；离开策略中心时关闭编辑层，避免锁住页面滚动。
    if (!this.querySelector("#strategy-dialog-layer").classList.contains("hidden")) this.closeEditor();
  }

  resetSession() {
    this.sessionGeneration += 1;
    this.started = false;
    this.loading = false;
    this.loadVersion += 1;
    this.items = [];
    this.templates = [];
    this.indicators = [];
    this.indicatorDefaults = {};
    this.sourceRuntime = {};
    this.selectedIndicators = new Map();
    this.deployments = [];
    this.signals = [];
    this.section = "strategies";
    this.query = "";
    this.statusFilter = "all";
    this.categoryFilter = "all";
    this.activeItem = null;
    this.createMode = "indicators";
    this.preview = null;
    this.codeBuffers = {};
    this.sourceWorkbenchDirty = false;
    this.sourceWorkbenchTab = "ai";
    this.sourceParameterSchema = [];
    this.sourceParameterValues = {};
    this.sourceRiskDefaults = {};
    this.sourceBacktestCatalog = null;
    this.sourceBacktestResult = null;
    this.sourceBacktestRunning = false;
    this.sourceBacktestScope = "single";
    this.sourceBacktestSuite = [];
    this.sourceBacktestFailures = [];
    this.sourceBacktestAnalysis = null;
    this.sourceOptimizationValidation = null;
    this.sourceComposition = null;
    this.sourceCompositionDirty = false;
    this.aiConversation = [];
    this.aiPromptQueue = [];
    this.aiBusy = false;
    this.aiTurnSequence = 0;
    this.aiWorkingSource = "";
    this.aiWorkingSpec = null;
    this.aiWorkingProposed = null;
    this.aiWorkingProfile = null;
    this.aiWorkingComposition = null;
    this.aiProcessSteps = [];
    this.querySelector("#strategy-search").value = "";
    this.querySelector("#strategy-category-filter").replaceChildren(this.option("all", "全部分类"));
    this.querySelectorAll("[data-section]").forEach((button) => {
      const active = button.dataset.section === "strategies";
      button.classList.toggle("active", active);
      button.setAttribute("aria-pressed", String(active));
    });
    this.querySelectorAll("[data-status]").forEach((button) => {
      const active = button.dataset.status === "all";
      button.classList.toggle("active", active);
      button.setAttribute("aria-pressed", String(active));
    });
    this.closeEditor(true);
    this.closeDetails();
    this.renderLoading("登录后读取个人策略…");
    this.renderStats();
    this.showNotice("");
  }

  async load(force = false) {
    if (this.loading && !force) return;
    this.loading = true;
    const generation = this.sessionGeneration;
    const requestVersion = ++this.loadVersion;
    this.renderLoading("正在从数据库读取个人策略…");
    this.querySelector("#strategy-refresh").disabled = true;
    this.showNotice("");
    try {
      const [payload, indicatorPayload, deploymentPayload, signalPayload] = await Promise.all([
        this.api(),
        this.api("/indicators/catalog"),
        this.api("/deployments"),
        this.api("/signals?limit=100"),
      ]);
      if (generation !== this.sessionGeneration || requestVersion !== this.loadVersion) return;
      this.items = Array.isArray(payload?.items) ? payload.items.map((item) => this.normalizeItem(item)) : [];
      this.templates = Array.isArray(payload?.templates) ? payload.templates.map((item) => this.normalizeTemplate(item)) : [];
      this.indicators = Array.isArray(indicatorPayload?.items) ? indicatorPayload.items : [];
      this.indicatorDefaults = this.plainObject(indicatorPayload?.defaults);
      this.sourceRuntime = this.plainObject(payload?.source_runtime);
      this.deployments = Array.isArray(deploymentPayload?.items) ? deploymentPayload.items : [];
      this.signals = Array.isArray(signalPayload?.items) ? signalPayload.items : [];
      this.renderFilters();
      this.renderStats();
      this.renderSection();
    } catch (error) {
      if (generation !== this.sessionGeneration || requestVersion !== this.loadVersion) return;
      this.renderError(this.localizedErrorMessage(error, "策略列表加载失败"));
    } finally {
      if (generation === this.sessionGeneration && requestVersion === this.loadVersion) {
        this.loading = false;
        this.querySelector("#strategy-refresh").disabled = false;
      }
    }
  }

  normalizeItem(item = {}) {
    const schema = Array.isArray(item.parameter_schema)
      ? item.parameter_schema
      : (Array.isArray(item.params_schema) ? item.params_schema : (Array.isArray(item.params) ? item.params : []));
    return {
      ...item,
      public_id: String(item.public_id ?? item.id ?? ""),
      name: String(item.name ?? "未命名策略"),
      description: String(item.description ?? ""),
      category: String(item.category ?? "自定义"),
      status: String(item.status ?? "active").toLowerCase(),
      version: Number(item.version ?? 1),
      engine_key: String(item.engine_key ?? "rule_engine"),
      strategy_kind: String(item.strategy_kind ?? "legacy_signal"),
      lifecycle_status: String(item.lifecycle_status ?? "draft"),
      spec: this.plainObject(item.spec),
      source_language: String(item.source_language ?? ""),
      source_code: String(item.source_code ?? ""),
      source_hash: String(item.source_hash ?? ""),
      source_validation: this.plainObject(item.source_validation),
      is_default: Boolean(item.is_default),
      parameter_schema: schema,
      parameters: this.plainObject(item.parameters),
      risk_defaults: this.plainObject(item.risk_defaults),
    };
  }

  normalizeTemplate(item = {}) {
    const schema = Array.isArray(item.parameter_schema)
      ? item.parameter_schema
      : (Array.isArray(item.params_schema) ? item.params_schema : []);
    return {
      ...item,
      template_key: String(item.template_key ?? item.key ?? item.id ?? ""),
      name: String(item.name ?? "未命名模板"),
      description: String(item.description ?? ""),
      category: String(item.category ?? "自定义"),
      template_kind: String(item.template_kind ?? "legacy_signal"),
      spec: this.plainObject(item.spec),
      parameter_schema: schema,
      parameters: this.plainObject(item.parameters),
      risk_defaults: this.plainObject(item.risk_defaults),
    };
  }

  plainObject(value) {
    return value && typeof value === "object" && !Array.isArray(value) ? { ...value } : {};
  }

  lifecycleLabel(status) {
    return {
      draft: "草稿",
      validated: "已校验",
      backtested: "已回测",
      shadow: "影子运行",
      paper: "模拟盘",
      micro_live: "微型实盘",
      live: "正式实盘",
      published: "旧版已发布",
      retired: "已退役",
    }[String(status || "")] || String(status || "未知");
  }

  async promoteLifecycle(item, targetStatus, approvalNote = null) {
    const payload = await this.api(`/${encodeURIComponent(item.public_id)}/promote`, {
      method: "POST",
      body: JSON.stringify({
        expected_version: item.version,
        target_status: targetStatus,
        confirmed: true,
        ...(approvalNote ? { approval_note: approvalNote } : {}),
      }),
    });
    const promoted = this.normalizeItem(payload?.strategy ?? item);
    this.upsertItem(promoted);
    if (this.activeItem?.public_id === promoted.public_id) this.activeItem = promoted;
    return { item: promoted, readiness: this.plainObject(payload?.readiness) };
  }

  option(value, label) {
    const option = document.createElement("option");
    option.value = String(value);
    option.textContent = String(label);
    return option;
  }

  node(tag, className = "", text = "") {
    const element = document.createElement(tag);
    if (className) element.className = className;
    if (text !== "") element.textContent = String(text);
    return element;
  }

  renderLoading(message) {
    const grid = this.querySelector("#strategy-grid");
    grid.setAttribute("aria-busy", "true");
    const state = this.node("div", "strategy-grid-state strategy-grid-loading");
    const icon = this.node("span", "strategy-spinner");
    icon.setAttribute("aria-hidden", "true");
    state.append(icon, this.node("strong", "", message), this.node("small", "", "策略按用户隔离，不会读取其他账户数据"));
    grid.replaceChildren(state);
  }

  renderError(message) {
    const grid = this.querySelector("#strategy-grid");
    grid.setAttribute("aria-busy", "false");
    const state = this.node("div", "strategy-grid-state error");
    state.append(this.node("span", "strategy-state-icon", "!"), this.node("strong", "", "策略列表暂不可用"), this.node("small", "", message));
    const retry = this.node("button", "strategy-quiet-button", "重新加载");
    retry.type = "button";
    retry.addEventListener("click", () => this.load(true));
    state.append(retry);
    grid.replaceChildren(state);
  }

  renderFilters() {
    const categories = [...new Set(this.items.map((item) => item.category).filter(Boolean))].sort((a, b) => a.localeCompare(b, "zh-CN"));
    const select = this.querySelector("#strategy-category-filter");
    select.replaceChildren(this.option("all", "全部分类"), ...categories.map((category) => this.option(category, category)));
    if (categories.includes(this.categoryFilter)) select.value = this.categoryFilter;
    else this.categoryFilter = "all";
  }

  renderStats() {
    const full = this.items.filter((item) => ["full_strategy", "source_strategy"].includes(item.strategy_kind)).length;
    const composite = this.items.filter((item) => item.spec?.strategy_type === "indicator_composite").length;
    const legacy = this.items.filter((item) => !["full_strategy", "source_strategy"].includes(item.strategy_kind)).length;
    const running = this.deployments.filter((item) => item.status === "running").length;
    this.querySelector("#strategy-total").textContent = String(full).padStart(2, "0");
    this.querySelector("#strategy-active").textContent = String(running).padStart(2, "0");
    this.querySelector("#strategy-defaults").textContent = String(this.indicators.length).padStart(2, "0");
    this.querySelector("#strategy-latest").textContent = String(composite).padStart(2, "0");
    this.querySelector("#strategy-tab-full").textContent = String(full);
    this.querySelector("#strategy-tab-indicators").textContent = String(this.indicators.length);
    this.querySelector("#strategy-tab-deployments").textContent = String(this.deployments.length);
    this.querySelector("#strategy-tab-signals").textContent = String(this.signals.length);
    this.querySelector("#strategy-tab-legacy").textContent = String(legacy);
  }

  filteredItems() {
    return this.items.filter((item) => {
      const complete = ["full_strategy", "source_strategy"].includes(item.strategy_kind);
      const kindMatches = this.section === "legacy" ? !complete : complete;
      const isDefault = Boolean(item.is_default || item.source_template_key);
      const statusMatches = this.statusFilter === "all"
        || (this.statusFilter === "default" && isDefault)
        || (this.statusFilter === "custom" && !isDefault);
      const categoryMatches = this.categoryFilter === "all" || item.category === this.categoryFilter;
      const haystack = `${item.name} ${item.category} ${item.description} ${item.engine_key}`.toLocaleLowerCase("zh-CN");
      return kindMatches && statusMatches && categoryMatches && (!this.query || haystack.includes(this.query));
    });
  }

  renderSection() {
    const strategyView = ["strategies", "legacy"].includes(this.section);
    this.querySelector("#strategy-create").classList.toggle("hidden", this.section !== "strategies");
    this.querySelector("#strategy-status-filter").classList.toggle("hidden", !strategyView);
    this.querySelector("#strategy-category-filter").closest("label").classList.toggle("hidden", !strategyView);
    const placeholders = {
      strategies: "搜索完整策略名称、分类或说明",
      legacy: "搜索旧版指标信号",
      indicators: "搜索指标名称、类别或输出",
      deployments: "搜索模拟盘或回测部署",
      signals: "搜索标的、方向、状态或周期",
    };
    this.querySelector("#strategy-search").placeholder = placeholders[this.section] || "搜索";
    this.renderCards();
  }

  renderCards() {
    const grid = this.querySelector("#strategy-grid");
    grid.setAttribute("aria-busy", "false");
    if (this.section === "indicators") {
      const items = this.indicators.filter((item) => {
        const text = `${item.name || ""} ${item.category || ""} ${(item.outputs || []).join(" ")}`.toLocaleLowerCase("zh-CN");
        return !this.query || text.includes(this.query);
      });
      grid.replaceChildren(...(items.length ? items.map((item) => this.indicatorCard(item)) : [this.emptyState("没有匹配的指标", "指标由统一计算内核提供，不包含入场和退出规则。")]));
      return;
    }
    if (this.section === "deployments") {
      const items = this.deployments.filter((item) => {
        const text = `${item.name || ""} ${item.mode || ""} ${item.status || ""}`.toLocaleLowerCase("zh-CN");
        return !this.query || text.includes(this.query);
      });
      grid.replaceChildren(...(items.length ? items.map((item) => this.deploymentCard(item)) : [this.emptyState("还没有运行部署", "创建模拟盘或完成一次数据库策略回测后，会在这里形成独立部署记录。")]));
      return;
    }
    if (this.section === "signals") {
      const items = this.signals.filter((item) => {
        const text = `${item.symbol || ""} ${item.decision || ""} ${item.status || ""} ${item.timeframe || ""}`.toLocaleLowerCase("zh-CN");
        return !this.query || text.includes(this.query);
      });
      grid.replaceChildren(...(items.length ? items.map((item) => this.signalCard(item)) : [this.emptyState("还没有策略信号", "策略部署产生的方向、置信度、风控判断和处理状态会记录在这里。")]));
      return;
    }
    const items = this.filteredItems();
    if (!items.length) {
      const empty = this.node("div", "strategy-grid-state");
      empty.append(this.node("span", "strategy-state-icon", this.items.length ? "⌕" : "策"));
      empty.append(this.node("strong", "", this.items.length ? "没有匹配的策略" : "还没有个人策略"));
      empty.append(this.node("small", "", this.section === "legacy" ? "旧版信号仅用于兼容，不建议继续新增。" : "从指标库选择多个指标后，可用于回测和独立模拟盘。"));
      const action = this.node("button", "strategy-create-button", this.items.length ? "清除筛选" : "新增策略");
      action.type = "button";
      action.addEventListener("click", () => {
        if (this.items.length) {
          this.query = "";
          this.statusFilter = "all";
          this.categoryFilter = "all";
          this.querySelector("#strategy-search").value = "";
          this.renderFilters();
          this.querySelectorAll("[data-status]").forEach((button) => {
            const active = button.dataset.status === "all";
            button.classList.toggle("active", active);
            button.setAttribute("aria-pressed", String(active));
          });
          this.renderCards();
        } else if (this.section === "strategies") this.openCreate();
      });
      empty.append(action);
      grid.replaceChildren(empty);
      return;
    }

    grid.replaceChildren(...items.map((item) => this.strategyCard(item)));
  }

  strategyCard(item) {
    const isSource = item.strategy_kind === "source_strategy";
    const isFull = item.strategy_kind === "full_strategy" || isSource;
    const card = this.node("article", `strategy-card-item ${isFull ? "full-strategy" : "legacy-signal"}`);
    const head = this.node("header", "strategy-card-head");
    const icon = this.node("span", "strategy-card-icon", this.strategyInitial(item.name));
    const title = this.node("div", "strategy-card-title");
    title.append(this.node("strong", "", item.name), this.node("small", "", `${item.category} · ${isSource ? "Python 源码策略" : (isFull ? "完整策略" : "旧版信号")}`));
    const lifecycleReady = ["validated", "backtested", "shadow", "paper", "micro_live", "live"].includes(item.lifecycle_status);
    const state = this.node("span", `strategy-state ${lifecycleReady ? "active" : "draft"}`, this.lifecycleLabel(item.lifecycle_status));
    head.append(icon, title, state);

    const description = this.node("p", "strategy-card-description", item.description || "尚未填写策略说明");
    const tags = this.node("div", "strategy-card-tags");
    const timeframes = this.plainObject(item.spec?.timeframes);
    const compositeIndicators = Array.isArray(item.spec?.indicators) ? item.spec.indicators : [];
    if (item.spec?.strategy_type === "indicator_composite") {
      tags.append(this.node("span", "strategy-kind-tag", `指标组合 · ${timeframes.trigger || "1h"}`));
      compositeIndicators.slice(0, 4).forEach((selection) => {
        const indicator = this.indicators.find((candidate) => candidate.key === selection.key);
        tags.append(this.node("span", "", indicator?.name || selection.key));
      });
    } else if (isSource) tags.append(this.node("span", "strategy-kind-tag", `Python · ${String(item.source_hash || "").slice(0, 10) || "未发布"}`));
    else if (isFull) tags.append(this.node("span", "strategy-kind-tag", `多周期 ${timeframes.regime || "4h"}/${timeframes.setup || "1h"}/${timeframes.trigger || "15m"}`));
    else tags.append(this.node("span", "strategy-legacy-tag", "兼容指标信号"));
    const schema = Array.isArray(item.parameter_schema) ? item.parameter_schema : [];
    schema.slice(0, 3).forEach((field) => {
      const key = String(field.key ?? "");
      const value = Object.prototype.hasOwnProperty.call(item.parameters, key) ? item.parameters[key] : field.default;
      tags.append(this.node("span", "", `${field.label || key} ${this.displayValue(value)}`));
    });
    if (!schema.length) tags.append(this.node("span", "", item.engine_key || "规则策略"));
    if (schema.length > 3) tags.append(this.node("span", "", `+${schema.length - 3} 参数`));

    const meta = this.node("div", "strategy-card-meta");
    const identity = this.node("div");
    identity.append(this.node("span", "", item.is_default || item.source_template_key ? "默认策略副本" : "自建策略"), this.node("small", "", `v${item.version} · ${this.shortDate(item.updated_at || item.created_at)}`));
    const edit = this.node("button", "strategy-edit-button", "编辑");
    edit.type = "button";
    edit.setAttribute("aria-label", `编辑策略 ${item.name}`);
    edit.addEventListener("click", () => this.openEdit(item));
    const details = this.node("button", "strategy-edit-button secondary", "详情");
    details.type = "button";
    details.setAttribute("aria-label", `查看策略 ${item.name} 的版本详情`);
    details.addEventListener("click", () => void this.openDetails(item));
    const validate = this.node("button", "strategy-edit-button secondary", "验证");
    validate.type = "button";
    validate.setAttribute("aria-label", `验证策略 ${item.name}`);
    validate.addEventListener("click", () => void this.validateItem(item, validate));
    const archive = this.node("button", "strategy-edit-button danger", "归档");
    archive.type = "button";
    archive.setAttribute("aria-label", `归档策略 ${item.name}`);
    archive.addEventListener("click", () => void this.archiveItem(item, archive));
    const actions = this.node("div", "strategy-card-actions");
    actions.append(details, validate, edit, archive);
    meta.append(identity, actions);
    card.append(head, description, tags, meta);
    return card;
  }

  indicatorCard(item) {
    const card = this.node("article", "strategy-card-item indicator-card");
    const head = this.node("header", "strategy-card-head");
    const title = this.node("div", "strategy-card-title");
    title.append(this.node("strong", "", item.name || item.key), this.node("small", "", `${item.category || "标准指标"} · v${item.version || 1}`));
    head.append(this.node("span", "strategy-card-icon", "ƒ"), title, this.node("span", "strategy-state active", "统一口径"));
    const description = this.node("p", "strategy-card-description", item.description || "标准化指标，可加入多指标组合策略。");
    const tags = this.node("div", "strategy-card-tags");
    (item.outputs || []).forEach((output) => tags.append(this.node("span", "", `输出 ${output}`)));
    const meta = this.node("div", "strategy-card-meta");
    const identity = this.node("div");
    identity.append(this.node("span", "", `${item.role === "filter" ? "过滤指标" : "方向指标"} · ${item.key}`), this.node("small", "", `${(item.parameters || []).length} 个可调参数`));
    meta.append(identity);
    card.append(head, description, tags, meta);
    return card;
  }

  deploymentCard(item) {
    const card = this.node("article", "strategy-card-item deployment-card");
    const head = this.node("header", "strategy-card-head");
    const title = this.node("div", "strategy-card-title");
    const modeLabel = { paper: "模拟盘", backtest: "回测", shadow: "影子", live: "实盘" }[item.mode] || item.mode;
    const statusLabel = { running: "运行中", paused: "已暂停", stopped: "已结束", error: "异常", created: "待启动" }[item.status] || item.status;
    title.append(this.node("strong", "", item.name || "未命名部署"), this.node("small", "", `${modeLabel} · 固定策略修订 #${item.strategy_revision_id}`));
    head.append(this.node("span", "strategy-card-icon", item.mode === "paper" ? "P" : "R"), title, this.node("span", `strategy-state ${item.status === "running" ? "active" : "draft"}`, statusLabel));
    const description = this.node("p", "strategy-card-description", item.last_error_code ? `最近错误：${item.last_error_code}` : "部署按当前用户隔离，运行期间始终绑定同一个不可变策略版本。");
    const tags = this.node("div", "strategy-card-tags");
    tags.append(this.node("span", "", `模式 ${modeLabel}`), this.node("span", "", `目标 #${item.target_account_id ?? "--"}`));
    const meta = this.node("div", "strategy-card-meta");
    const identity = this.node("div");
    identity.append(this.node("span", "", `部署 ${String(item.id || "").slice(0, 8)}`), this.node("small", "", `创建于 ${this.shortDate(item.created_at)}`));
    meta.append(identity);
    card.append(head, description, tags, meta);
    return card;
  }

  signalCard(item) {
    const card = this.node("article", "strategy-card-item signal-card");
    const head = this.node("header", "strategy-card-head");
    const title = this.node("div", "strategy-card-title");
    const decision = String(item.decision || "neutral").toLowerCase();
    const directionLabel = { long: "做多", short: "做空", neutral: "观望", close: "平仓" }[decision] || decision;
    const statusLabel = { accepted: "已接受", rejected: "已拒绝", executed: "已执行", expired: "已过期", pending: "待处理" }[item.status] || item.status || "已记录";
    title.append(this.node("strong", "", item.symbol || "未知标的"), this.node("small", "", `${item.timeframe || "--"} · ${directionLabel}`));
    head.append(this.node("span", "strategy-card-icon", decision === "long" ? "多" : (decision === "short" ? "空" : "信")), title, this.node("span", `strategy-state ${["accepted", "executed"].includes(item.status) ? "active" : "draft"}`, statusLabel));
    const reasonCodes = Array.isArray(item.reason_codes) ? item.reason_codes : [];
    const description = this.node("p", "strategy-card-description", reasonCodes.length ? reasonCodes.join(" · ") : "策略运行时生成的结构化信号记录。");
    const tags = this.node("div", "strategy-card-tags");
    tags.append(this.node("span", "strategy-kind-tag", `方向 ${directionLabel}`));
    if (item.confidence != null) {
      const confidence = Number(item.confidence);
      if (Number.isFinite(confidence)) tags.append(this.node("span", "", `置信度 ${Math.round(confidence <= 1 ? confidence * 100 : confidence)}%`));
    }
    const risk = this.plainObject(item.risk_decision);
    if (risk.decision || risk.status) tags.append(this.node("span", "", `风控 ${risk.decision || risk.status}`));
    const meta = this.node("div", "strategy-card-meta");
    const identity = this.node("div");
    identity.append(this.node("span", "", `部署 #${String(item.deployment_id || "--").slice(0, 8)}`), this.node("small", "", this.shortDateTime(item.created_at)));
    meta.append(identity);
    card.append(head, description, tags, meta);
    return card;
  }

  emptyState(title, detail) {
    const empty = this.node("div", "strategy-grid-state");
    empty.append(this.node("span", "strategy-state-icon", "·"), this.node("strong", "", title), this.node("small", "", detail));
    return empty;
  }

  strategyInitial(name) {
    const text = String(name || "策").trim();
    return text.slice(0, 1).toLocaleUpperCase("zh-CN") || "策";
  }

  shortDate(value) {
    if (!value) return "刚刚";
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return "刚刚";
    return new Intl.DateTimeFormat("zh-CN", { month: "2-digit", day: "2-digit" }).format(date);
  }

  shortDateTime(value) {
    if (!value) return "刚刚";
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return "刚刚";
    return new Intl.DateTimeFormat("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" }).format(date);
  }

  displayValue(value) {
    if (value === null || value === undefined || value === "") return "--";
    if (typeof value === "boolean") return value ? "是" : "否";
    if (typeof value === "object") return "已配置";
    return String(value).slice(0, 18);
  }

  openCreate() {
    this.editorMode = "create";
    this.editScope = "parameters";
    this.createMode = "indicators";
    this.activeItem = null;
    this.preview = null;
    this.codeBuffers = {};
    this.sourceComposition = null;
    this.sourceCompositionDirty = false;
    this.sourceWorkbenchTab = "ai";
    this.sourceParameterSchema = [];
    this.sourceParameterValues = {};
    this.sourceRiskDefaults = {};
    this.resetAiConversation();
    this.resetSourceBacktestResult();
    this.querySelector("#strategy-editor-kicker").textContent = "CREATE STRATEGY";
    this.querySelector("#strategy-editor-title").textContent = "新增策略";
    this.querySelector("#strategy-editor-subtitle").textContent = "描述策略目标，AI 自动拟定身份、指标、约束、风险与可回测 Python 源码。";
    this.querySelector("#strategy-version-strip").classList.add("hidden");
    this.querySelector("#strategy-edit-scope-block").classList.add("hidden");
    this.querySelector("#strategy-code-block").classList.add("hidden");
    this.querySelector("#strategy-composer-block").classList.remove("hidden");
    this.querySelector("#strategy-basic-index").textContent = "02";
    this.querySelector("#strategy-parameters-index").textContent = "--";
    this.querySelector("#strategy-risk-index").textContent = "03";
    this.querySelector("#strategy-ai-panel").classList.remove("hidden");
    this.querySelector("#strategy-ai-status").lastChild.textContent = "Python 隔离运行时";
    this.querySelector("#strategy-ai-title").textContent = "用自然语言编排完整策略";
    this.querySelector("#strategy-ai-description").textContent = "直接描述目标即可。AI 会自动拟定策略名称、分类、说明、指标组合、运行约束、风险参数与完整 Python 源码。";
    this.querySelector("#strategy-ai-preview-button strong").textContent = "发送给 AI";
    this.querySelector("#strategy-save strong").textContent = "创建策略";
    this.populateTemplateSelect();
    this.querySelector("#strategy-form").reset();
    this.querySelector("#strategy-name").value = "多指标组合策略";
    this.querySelector("#strategy-category").value = "指标组合";
    this.querySelector("#strategy-description").value = "多个标准指标加权确认，并通过波动与成交量过滤。";
    this.querySelector("#strategy-timeframe").value = String(this.indicatorDefaults.timeframe || "1h");
    this.querySelector("#strategy-confirmation-threshold").value = String(this.indicatorDefaults.confirmation_threshold || 60);
    this.querySelector("#strategy-signal-valid-bars").value = String(this.indicatorDefaults.signal_valid_bars || 2);
    const directions = Array.isArray(this.indicatorDefaults.directions) ? this.indicatorDefaults.directions : ["long", "short"];
    this.querySelector("#strategy-direction-long").checked = directions.includes("long");
    this.querySelector("#strategy-direction-short").checked = directions.includes("short");
    this.switchCreateMode("indicators", { resetValues: false });
    this.setSelectedIndicators(["ema", "adx", "volume_ratio"].filter((key) => this.indicators.some((item) => item.key === key)).map((key) => ({ key })), "create");
    this.renderParameterFields([], {});
    this.renderRiskFields(this.plainObject(this.indicatorDefaults.risk_defaults));
    this.querySelector("#strategy-ai-prompt").value = "";
    this.clearPreview();
    this.showFormError("");
    this.showAiError("");
    this.showEditor();
  }

  openEdit(item) {
    this.editorMode = "edit";
    this.activeItem = this.normalizeItem(item);
    this.editScope = ["full_strategy", "source_strategy"].includes(this.activeItem.strategy_kind)
      ? "source"
      : "parameters";
    this.preview = null;
    this.sourceCompositionDirty = false;
    this.sourceWorkbenchTab = "ai";
    this.sourceParameterSchema = Array.isArray(this.activeItem.parameter_schema)
      ? this.activeItem.parameter_schema.map((definition) => ({ ...definition }))
      : [];
    this.sourceParameterValues = { ...this.plainObject(this.activeItem.parameters) };
    this.sourceRiskDefaults = { ...this.plainObject(this.activeItem.risk_defaults) };
    this.sourceComposition = this.deriveSourceComposition(this.activeItem);
    this.aiWorkingSource = this.activeItem.source_code || "";
    this.resetAiConversation();
    this.resetSourceBacktestResult();
    this.querySelector("#strategy-editor-kicker").textContent = "EDIT STRATEGY";
    this.querySelector("#strategy-editor-title").textContent = this.activeItem.name;
    this.querySelector("#strategy-editor-subtitle").textContent = "保存后生成新版本；已完成的回测仍保留当时的策略快照。";
    this.querySelector("#strategy-version-strip").classList.remove("hidden");
    const maintainable = ["full_strategy", "source_strategy"].includes(this.activeItem.strategy_kind);
    this.querySelector("#strategy-edit-scope-block").classList.toggle("hidden", !maintainable);
    this.querySelector("#strategy-dsl-scope").classList.add("hidden");
    this.querySelector("#strategy-editor-version").textContent = `v${this.activeItem.version}`;
    this.querySelector("#strategy-composer-block").classList.add("hidden");
    this.querySelector("#strategy-basic-index").textContent = "01";
    this.querySelector("#strategy-parameters-index").textContent = "02";
    this.querySelector("#strategy-risk-index").textContent = "03";
    this.querySelector("#strategy-ai-panel").classList.remove("hidden");
    this.querySelector("#strategy-ai-status").lastChild.textContent = "受约束配置";
    this.querySelector("#strategy-ai-title").textContent = "用自然语言修改策略";
    this.querySelector("#strategy-ai-description").textContent = "描述你想调整的逻辑或风险参数。模型只能提出结构化配置修改。";
    this.querySelector("#strategy-ai-preview-button strong").textContent = "发送给 AI";
    this.querySelector("#strategy-save strong").textContent = "保存新版本";
    this.querySelector("#strategy-name").value = this.activeItem.name;
    this.querySelector("#strategy-category").value = this.activeItem.category;
    this.querySelector("#strategy-description").value = this.activeItem.description;
    this.renderParameterFields(this.activeItem.parameter_schema, this.activeItem.parameters);
    this.renderRiskFields(this.activeItem.risk_defaults);
    this.populateSourceCompositionEditor(this.sourceComposition);
    this.querySelector("#strategy-code-editor").value = this.activeItem.strategy_kind === "source_strategy"
      ? this.activeItem.source_code
      : this.strategyCode(this.activeItem.spec);
    this.codeBuffers = {
      code: this.strategyCode(this.activeItem.spec),
      source: this.activeItem.source_code || String(this.sourceRuntime.conversion_starter_source || this.sourceRuntime.starter_source || ""),
    };
    this.setCodeStatus("当前版本代码，尚未重新校验");
    this.switchEditScope(this.editScope, { resetCode: true });
    this.querySelector("#strategy-ai-prompt").value = "";
    this.clearPreview();
    this.showFormError("");
    this.showAiError("");
    this.showEditor();
  }

  showEditor() {
    const layer = this.querySelector("#strategy-dialog-layer");
    layer.classList.remove("hidden");
    layer.setAttribute("aria-hidden", "false");
    document.body.classList.add("strategy-dialog-open");
    window.setTimeout(() => (this.isSourceWorkbench() ? this.querySelector("#strategy-code-editor") : this.querySelector("#strategy-name")).focus(), 0);
  }

  closeEditor(force = false) {
    const layer = this.querySelector("#strategy-dialog-layer");
    if (!force && !layer.classList.contains("hidden") && this.isSourceWorkbench() && this.sourceWorkbenchDirty) {
      if (!window.confirm("当前源码尚未保存，确认关闭工作台并放弃修改？")) return;
    }
    layer.classList.add("hidden");
    layer.setAttribute("aria-hidden", "true");
    document.body.classList.remove("strategy-dialog-open");
    this.aiSessionGeneration = Number(this.aiSessionGeneration || 0) + 1;
    this.aiPromptQueue = [];
    this.aiBusy = false;
    this.setButtonBusy(this.querySelector("#strategy-save"), false);
    this.setButtonBusy(this.querySelector("#strategy-ai-preview-button"), false);
    this.setButtonBusy(this.querySelector("#strategy-ai-apply"), false);
    this.setSourceWorkbenchActive(false);
    this.setSourceWorkbenchShellActive(false);
  }

  isSourceWorkbench() {
    return (this.editorMode === "edit" && this.editScope === "source")
      || (this.editorMode === "create" && this.createMode === "source");
  }

  setSourceWorkbenchActive(active) {
    const editor = this.querySelector(".strategy-editor");
    const enabled = Boolean(active);
    editor.classList.toggle("source-workbench", enabled);
    this.querySelector("#strategy-workbench-actions").classList.toggle("hidden", !enabled);
    this.querySelector("#strategy-workbench-tabs").classList.toggle("hidden", !enabled);
    if (!enabled) {
      this.querySelector("#strategy-backtest-pane").classList.add("hidden");
      this.querySelector("#strategy-ai-pane").classList.remove("hidden");
      return;
    }
    this.querySelector("#strategy-editor-kicker").textContent = "STRATEGY WORKBENCH";
    this.querySelector("#strategy-editor-subtitle").textContent = "AI 修改 · Python 源码 · 动态参数 · 校验与回测同屏完成";
    this.renderCodeLines();
    this.setSourceWorkbenchDirty(false);
    this.switchSourceWorkbenchTab(this.sourceWorkbenchTab || "backtest");
    this.applySourceRiskDefaults();
    void this.loadSourceBacktestCatalog();
  }

  setSourceWorkbenchShellActive(active) {
    this.querySelector(".strategy-editor").classList.toggle("source-workbench-shell", Boolean(active));
  }

  setSourceWorkbenchDirty(dirty) {
    this.sourceWorkbenchDirty = Boolean(dirty);
    const status = this.querySelector("#strategy-workbench-dirty");
    status.className = `strategy-workbench-dirty ${dirty ? "dirty" : "clean"}`;
    status.querySelector("b").textContent = dirty ? "未保存" : "已同步";
  }

  resetSourceBacktestResult() {
    this.sourceBacktestResult = null;
    this.sourceBacktestSuite = [];
    this.sourceBacktestFailures = [];
    this.sourceBacktestAnalysis = null;
    this.sourceOptimizationValidation = null;
    const empty = this.querySelector("#strategy-runner-empty");
    const result = this.querySelector("#strategy-runner-result");
    if (empty) empty.classList.remove("hidden");
    if (result) result.classList.add("hidden");
    const analysis = this.querySelector("#strategy-runner-analysis");
    if (analysis) analysis.classList.add("hidden");
    if (this.querySelector("#strategy-runner-symbol-search")) this.querySelector("#strategy-runner-symbol-search").value = "";
    if (this.querySelector("[data-runner-scope]")) this.setSourceBacktestScope("single");
    if (this.querySelector("#strategy-runner-notice")) this.showSourceRunnerNotice("");
  }

  setSourceBacktestScope(scope) {
    this.sourceBacktestScope = scope === "all" ? "all" : "single";
    this.querySelectorAll("[data-runner-scope]").forEach((button) => {
      const active = button.dataset.runnerScope === this.sourceBacktestScope;
      button.classList.toggle("active", active);
      button.setAttribute("aria-pressed", String(active));
    });
    const allSymbols = this.sourceBacktestScope === "all";
    this.querySelector("#strategy-runner-symbol-field").classList.toggle("disabled", allSymbols);
    this.querySelector("#strategy-runner-symbol-search").disabled = allSymbols;
    this.querySelector("#strategy-runner-symbol").disabled = allSymbols;
    this.querySelector("#strategy-runner-submit strong").textContent = allSymbols
      ? "保存当前版本并运行全品种回测"
      : "保存当前版本并运行回测";
    const count = this.sourceBacktestCatalog?.symbols?.length || 0;
    const concurrency = this.sourceBacktestConcurrency();
    if (allSymbols && count) this.showSourceRunnerNotice(`将按最多 ${concurrency} 路并发测试 ${count} 个品种；容量冲突会自动排队重试。`);
    else if (!this.sourceBacktestRunning) this.showSourceRunnerNotice("");
    if (this.sourceBacktestCatalog) this.syncSourceBacktestBounds();
  }

  filterSourceBacktestSymbols(query = "") {
    const catalog = this.sourceBacktestCatalog || { symbols: [] };
    const normalized = String(query || "").trim().toLocaleUpperCase("zh-CN");
    const matches = catalog.symbols.filter((item) => !normalized
      || item.value.toLocaleUpperCase("zh-CN").includes(normalized)
      || item.label.toLocaleUpperCase("zh-CN").includes(normalized));
    const select = this.querySelector("#strategy-runner-symbol");
    const previous = select.value;
    select.replaceChildren(...(matches.length
      ? matches.map((item) => this.option(item.value, item.label))
      : [this.option("", "没有匹配的品种")]));
    if (matches.some((item) => item.value === previous)) select.value = previous;
    else if (matches.length) select.value = matches[0].value;
    this.querySelector("#strategy-runner-symbol-match").textContent = normalized
      ? `匹配 ${matches.length} / ${catalog.symbols.length} 个品种`
      : `共 ${catalog.symbols.length} 个可测试品种`;
    this.syncSourceBacktestBounds();
  }

  renderCodeLines() {
    const editor = this.querySelector("#strategy-code-editor");
    const count = Math.max(1, String(editor.value || "").split("\n").length);
    this.querySelector("#strategy-code-lines").textContent = Array.from({ length: count }, (_, index) => index + 1).join("\n");
  }

  switchSourceWorkbenchTab(tab) {
    this.sourceWorkbenchTab = tab === "ai" ? "ai" : "backtest";
    this.querySelectorAll("[data-workbench-tab]").forEach((button) => {
      const active = button.dataset.workbenchTab === this.sourceWorkbenchTab;
      button.classList.toggle("active", active);
      button.setAttribute("aria-pressed", String(active));
    });
    this.querySelector("#strategy-ai-pane").classList.toggle("hidden", this.sourceWorkbenchTab !== "ai");
    this.querySelector("#strategy-backtest-pane").classList.toggle("hidden", this.sourceWorkbenchTab !== "backtest");
    if (this.sourceWorkbenchTab === "backtest" && this.sourceBacktestResult) {
      window.requestAnimationFrame(() => this.drawSourceBacktestChart(this.sourceBacktestResult.result || {}));
    }
  }

  async backtestApi(path = "", options = {}) {
    if (typeof window.quantdeskApi !== "function") throw new Error("认证服务尚未就绪");
    return window.quantdeskApi(`/api/v2/backtests${path}`, options);
  }

  applySourceRiskDefaults() {
    const risk = this.plainObject(this.activeItem?.risk_defaults || this.indicatorDefaults.risk_defaults);
    const values = {
      "#strategy-runner-position": risk.position_size_pct ?? 10,
      "#strategy-runner-leverage": risk.leverage ?? 1,
      "#strategy-runner-holding": risk.max_holding_bars ?? 120,
      "#strategy-runner-stop": risk.stop_loss_pct ?? 5,
      "#strategy-runner-take": risk.take_profit_pct ?? 10,
      "#strategy-runner-fee": risk.fee_bps ?? 4,
      "#strategy-runner-slippage": risk.slippage_bps ?? 2,
    };
    Object.entries(values).forEach(([selector, value]) => { this.querySelector(selector).value = String(value); });
    this.syncSourceRiskMode();
  }

  sourceUsesRiskProposal(item = this.activeItem) {
    const validation = this.plainObject(item?.source_validation);
    if (typeof validation.uses_risk_proposal === "boolean") return validation.uses_risk_proposal;
    return /["']risk_proposal["']\s*:/.test(String(item?.source_code || ""));
  }

  syncSourceRiskMode() {
    const sourceControlled = this.sourceUsesRiskProposal();
    ["#strategy-runner-stop", "#strategy-runner-take"].forEach((selector) => {
      const input = this.querySelector(selector);
      input.disabled = sourceControlled;
      input.closest("label")?.classList.toggle("disabled", sourceControlled);
    });
    const mode = this.querySelector("#strategy-runner-risk-mode");
    mode.className = `strategy-runner-risk-mode ${sourceControlled ? "source" : "config"}`;
    mode.textContent = sourceControlled
      ? "当前源码返回 risk_proposal：ATR 风险参数接管止损/止盈，页面百分比仅作备用，不参与本版本回测优化。"
      : "当前源码未接管风险距离：回测使用页面配置的百分比止损与止盈。";
  }

  sourceTriggerTimeframe(item = this.activeItem) {
    const validation = this.plainObject(item?.source_validation);
    const dataRequirements = this.plainObject(validation.data_requirements);
    const candidates = [
      validation.trigger_timeframe,
      dataRequirements.trigger_timeframe,
      this.sourceComposition?.timeframe,
    ];
    return candidates.map((value) => String(value || "")).find((value) => ["15m", "1h", "4h"].includes(value)) || "";
  }

  async loadSourceBacktestCatalog(force = false) {
    if (!this.isSourceWorkbench()) return;
    if (this.sourceBacktestCatalog && !force) {
      this.renderSourceBacktestCatalog();
      return;
    }
    this.setSourceRunnerStatus("读取行情", "loading");
    this.showSourceRunnerNotice("");
    try {
      const payload = await this.backtestApi("/catalog");
      this.sourceBacktestCatalog = {
        strategies: Array.isArray(payload?.strategies) ? payload.strategies : [],
        symbols: (Array.isArray(payload?.symbols) ? payload.symbols : []).map((item) => ({
          value: String(item?.value ?? item?.symbol ?? item ?? ""),
          label: String(item?.label ?? item?.name ?? item?.symbol ?? item ?? ""),
          available: item?.available !== false,
          timeframes: Array.isArray(item?.timeframes) ? item.timeframes : [],
        })).filter((item) => item.value),
        timeframes: (Array.isArray(payload?.timeframes) ? payload.timeframes : []).map((item) => ({
          value: String(item?.value ?? item?.timeframe ?? item ?? ""),
          label: String(item?.label ?? item?.timeframe ?? item ?? ""),
        })).filter((item) => item.value),
        bounds: this.plainObject(payload?.bounds),
        limits: this.plainObject(payload?.limits),
      };
      this.renderSourceBacktestCatalog();
      this.setSourceRunnerStatus("可以回测", "ready");
    } catch (error) {
      this.setSourceRunnerStatus("目录不可用", "error");
      this.showSourceRunnerNotice(`回测目录读取失败：${this.localizedErrorMessage(error, "请稍后重试")}`, "error");
    }
  }

  renderSourceBacktestCatalog() {
    const catalog = this.sourceBacktestCatalog || { symbols: [], timeframes: [] };
    const populate = (selector, items, placeholder) => {
      const select = this.querySelector(selector);
      const previous = select.value;
      select.replaceChildren(this.option("", placeholder), ...items.map((item) => this.option(item.value, item.label)));
      if (items.some((item) => item.value === previous)) select.value = previous;
      else if (items.length) select.value = items[0].value;
    };
    this.filterSourceBacktestSymbols(this.querySelector("#strategy-runner-symbol-search").value);
    populate("#strategy-runner-timeframe", catalog.timeframes, "暂无可用周期");
    this.querySelector("#strategy-runner-universe-count").textContent = `${catalog.symbols.length} 个可测试品种`;
    const timeframe = this.querySelector("#strategy-runner-timeframe");
    const preferred = this.sourceTriggerTimeframe();
    const sourceLocked = Boolean(preferred && catalog.timeframes.some((item) => item.value === preferred));
    if (sourceLocked) timeframe.value = preferred;
    timeframe.disabled = sourceLocked;
    timeframe.title = sourceLocked
      ? `当前源码触发周期为 ${preferred}，回测将自动使用该周期。`
      : "请选择回测数据周期";
    this.setSourceBacktestScope(this.sourceBacktestScope);
    this.syncSourceBacktestBounds();
  }

  syncSourceBacktestBounds() {
    const symbol = this.querySelector("#strategy-runner-symbol").value;
    const timeframe = this.querySelector("#strategy-runner-timeframe").value;
    let bound = this.plainObject(this.sourceBacktestCatalog?.bounds?.[symbol]?.[timeframe]);
    if (this.sourceBacktestScope === "all") {
      const availableBounds = Object.values(this.plainObject(this.sourceBacktestCatalog?.bounds))
        .map((item) => this.plainObject(this.plainObject(item)[timeframe]))
        .filter((item) => item.min_date || item.start || item.max_date || item.end);
      const commonMin = availableBounds.map((item) => String(item.min_date || item.start || "")).filter(Boolean).sort().at(-1) || "";
      const commonMax = availableBounds.map((item) => String(item.max_date || item.end || "")).filter(Boolean).sort().at(0) || "";
      bound = commonMin && commonMax && commonMin <= commonMax
        ? { min_date: commonMin, max_date: commonMax }
        : {};
    }
    const min = String(bound.min_date || bound.start || "");
    const max = String(bound.max_date || bound.end || "");
    const start = this.querySelector("#strategy-runner-start");
    const end = this.querySelector("#strategy-runner-end");
    start.min = min;
    start.max = max;
    end.min = min;
    end.max = max;
    const endValue = max || new Date().toISOString().slice(0, 10);
    const startCandidate = new Date(`${endValue}T00:00:00Z`);
    startCandidate.setUTCDate(startCandidate.getUTCDate() - 90);
    const startValue = startCandidate.toISOString().slice(0, 10);
    end.value = !end.value || (max && end.value > max) ? endValue : end.value;
    start.value = !start.value || start.value > end.value || (min && start.value < min)
      ? (min && startValue < min ? min : startValue)
      : start.value;
  }

  setSourceRunnerStatus(message, tone = "idle") {
    const status = this.querySelector("#strategy-runner-status");
    status.className = `strategy-runner-status ${tone}`;
    status.lastChild.textContent = message;
  }

  showSourceRunnerNotice(message, tone = "") {
    const notice = this.querySelector("#strategy-runner-notice");
    notice.textContent = message;
    notice.className = `strategy-runner-notice${message ? "" : " hidden"}${tone ? ` ${tone}` : ""}`;
  }

  async persistSourceWorkbench() {
    if (!this.isSourceWorkbench()) return null;
    const form = this.querySelector("#strategy-form");
    if (!form.checkValidity()) {
      form.reportValidity();
      this.showSourceRunnerNotice("请先补全有效的策略名称、分类和风险参数。", "error");
      return null;
    }
    let sourceCode;
    try { sourceCode = this.parseStrategyCode(); } catch (error) {
      this.setCodeStatus(error?.message || "策略源码无法解析", "error");
      this.showSourceRunnerNotice(error?.message || "策略源码无法解析", "error");
      return null;
    }
    const button = this.querySelector("#strategy-workbench-save");
    this.setButtonBusy(button, true, "保存中…");
    this.showSourceRunnerNotice("");
    try {
      const body = {
        name: this.querySelector("#strategy-name").value.trim(),
        description: this.querySelector("#strategy-description").value.trim(),
        category: this.querySelector("#strategy-category").value.trim(),
        language: "python",
        source_code: sourceCode,
        risk_defaults: this.collectConfig("risk", this.sourceRiskDefaults),
      };
      let result;
      if (this.editorMode === "create") {
        result = await this.api("/source", {
          method: "POST",
          body: JSON.stringify({
            ...body,
            risk_defaults: {
              ...this.collectConfig("risk", this.plainObject(this.indicatorDefaults.risk_defaults)),
              position_size_pct: Number(this.querySelector("#strategy-runner-position").value),
              leverage: Number(this.querySelector("#strategy-runner-leverage").value),
              max_holding_bars: Number(this.querySelector("#strategy-runner-holding").value),
              stop_loss_pct: Number(this.querySelector("#strategy-runner-stop").value),
              take_profit_pct: Number(this.querySelector("#strategy-runner-take").value),
              fee_bps: Number(this.querySelector("#strategy-runner-fee").value),
              slippage_bps: Number(this.querySelector("#strategy-runner-slippage").value),
            },
            ...(this.sourceComposition ? { composition: this.sourceComposition } : {}),
          }),
        });
      } else {
        result = await this.api(`/${encodeURIComponent(this.activeItem.public_id)}/source`, {
          method: "PUT",
          body: JSON.stringify({ ...body, version: this.activeItem.version }),
        });
      }
      let item = this.normalizeItem(result?.item ?? result);
      if (["draft", "published"].includes(item.lifecycle_status)) {
        const promoted = await this.promoteLifecycle(item, "validated");
        item = promoted.item;
      }
      this.activeItem = item;
      this.sourceComposition = this.deriveSourceComposition(item);
      this.populateSourceCompositionEditor(this.sourceComposition);
      this.sourceParameterSchema = item.parameter_schema.map((definition) => ({ ...definition }));
      this.sourceParameterValues = { ...item.parameters };
      this.sourceRiskDefaults = { ...this.plainObject(item.risk_defaults) };
      this.editorMode = "edit";
      this.editScope = "source";
      this.codeBuffers.source = item.source_code || sourceCode;
      this.upsertItem(item);
      this.querySelector("#strategy-editor-title").textContent = item.name;
      this.querySelector("#strategy-editor-version").textContent = `v${item.version}`;
      this.querySelector("#strategy-version-strip").classList.remove("hidden");
      this.querySelector("#strategy-edit-scope-block").classList.remove("hidden");
      this.querySelector("#strategy-composer-block").classList.add("hidden");
      this.querySelector("#strategy-basic-index").textContent = "01";
      this.querySelector("#strategy-parameters-index").textContent = "02";
      this.querySelector("#strategy-risk-index").textContent = "03";
      this.querySelector("#strategy-dsl-scope").classList.add("hidden");
      this.setSourceWorkbenchDirty(false);
      this.setCodeStatus(`已保存 v${item.version} · ${String(item.source_hash || "").slice(0, 12)}`, "success");
      this.showSourceRunnerNotice(`源码已保存为 v${item.version}，该版本可用于回测。`, "success");
      this.renderFilters();
      this.renderStats();
      this.renderCards();
      this.notifyStrategiesChanged();
      this.sourceBacktestCatalog = null;
      await this.loadSourceBacktestCatalog(true);
      return item;
    } catch (error) {
      const message = this.friendlyMutationError(error);
      this.setCodeStatus(message, "error");
      this.showSourceRunnerNotice(message, "error");
      return null;
    } finally {
      this.setButtonBusy(button, false);
    }
  }

  sourceBacktestPayload(symbol = "") {
    const numericParameters = {};
    Object.entries(this.plainObject(this.activeItem?.parameters)).forEach(([key, value]) => {
      const number = Number(value);
      if (Number.isFinite(number)) numericParameters[key] = number;
    });
    return {
      strategy_id: this.activeItem.public_id,
      symbol: symbol || this.querySelector("#strategy-runner-symbol").value,
      timeframe: this.sourceTriggerTimeframe() || this.querySelector("#strategy-runner-timeframe").value,
      start_date: this.querySelector("#strategy-runner-start").value,
      end_date: this.querySelector("#strategy-runner-end").value,
      initial_capital: Number(this.querySelector("#strategy-runner-capital").value),
      position_size_pct: Number(this.querySelector("#strategy-runner-position").value),
      leverage: Number(this.querySelector("#strategy-runner-leverage").value),
      fee_bps: Number(this.querySelector("#strategy-runner-fee").value),
      slippage_bps: Number(this.querySelector("#strategy-runner-slippage").value),
      stop_loss_pct: Number(this.querySelector("#strategy-runner-stop").value),
      take_profit_pct: Number(this.querySelector("#strategy-runner-take").value),
      max_holding_bars: Number(this.querySelector("#strategy-runner-holding").value),
      params: numericParameters,
    };
  }

  async executeSourceBacktest(payload) {
    let detail = await this.backtestApi("", { method: "POST", body: JSON.stringify(payload) });
    const id = detail?.run?.id ?? detail?.run?.run_id ?? detail?.id ?? detail?.run_id;
    if (!detail?.result && id != null) detail = await this.backtestApi(`/${encodeURIComponent(id)}`);
    const run = detail?.run || detail || {};
    const result = detail?.result || {
      account: { initial_capital: run.initial_capital, final_equity: run.final_equity },
      metrics: this.plainObject(run.metrics_json),
      equity_curve: Array.isArray(run.equity_curve_json) ? run.equity_curve_json : [],
      trades: Array.isArray(run.trades) ? run.trades : [],
      data_quality: this.plainObject(run.data_quality_json),
    };
    return { run, result, symbol: String(run.symbol || payload.symbol) };
  }

  sourceBacktestConcurrency() {
    const limits = this.plainObject(this.sourceBacktestCatalog?.limits);
    const configured = Number(limits.max_concurrent_backtests_per_user ?? limits.max_concurrent_backtests ?? 1);
    return Math.max(1, Math.min(4, Number.isFinite(configured) ? Math.floor(configured) : 1));
  }

  sourceBacktestFailureCategory(error) {
    const status = Number(error?.status || 0);
    const message = String(error?.message || "").toLowerCase();
    if ([409, 429].includes(status) || message.includes("concurrent backtest") || message.includes("capacity is busy")) return "capacity";
    if ([502, 503, 504].includes(status) || message.includes("timeout") || message.includes("temporarily")) return "transient";
    if (status === 422 && (message.includes("historical") || message.includes("kline") || message.includes("行情"))) return "market_data";
    if (status === 422 && (message.includes("源码") || message.includes("strategy"))) return "strategy";
    return "unknown";
  }

  async executeSourceBacktestWithRetry(payload, maxAttempts = 7) {
    let lastError;
    for (let attempt = 1; attempt <= maxAttempts; attempt += 1) {
      try {
        return await this.executeSourceBacktest(payload);
      } catch (error) {
        lastError = error;
        const category = this.sourceBacktestFailureCategory(error);
        if (!["capacity", "transient"].includes(category) || attempt >= maxAttempts) throw error;
        const retryAfter = Math.max(0, Number(error?.retryAfter || 0) * 1000);
        const delay = Math.max(retryAfter, Math.min(5000, 350 * (2 ** (attempt - 1))));
        await new Promise((resolve) => window.setTimeout(resolve, delay));
      }
    }
    throw lastError;
  }

  async runSourceBacktestSuite(basePayload, symbols) {
    const completed = [];
    const failures = [];
    let cursor = 0;
    const updateProgress = () => {
      const finished = completed.length + failures.length;
      this.setSourceRunnerStatus(`${finished} / ${symbols.length}`, "loading");
      this.showSourceRunnerNotice(`全品种回测进行中：已完成 ${finished} / ${symbols.length}，成功 ${completed.length}，失败 ${failures.length}。`);
    };
    const worker = async () => {
      while (cursor < symbols.length) {
        const index = cursor;
        cursor += 1;
        const symbol = symbols[index];
        try {
          completed.push(await this.executeSourceBacktestWithRetry({ ...basePayload, symbol }));
        } catch (error) {
          failures.push({
            symbol,
            status: Number(error?.status || 0) || null,
            category: this.sourceBacktestFailureCategory(error),
            message: this.localizedErrorMessage(error, "行情或策略执行失败"),
          });
        }
        updateProgress();
      }
    };
    updateProgress();
    await Promise.all(Array.from({ length: Math.min(this.sourceBacktestConcurrency(), symbols.length) }, () => worker()));
    return { completed, failures };
  }

  sourceBacktestMetric(entry) {
    const metrics = this.plainObject(entry?.result?.metrics || entry?.run?.metrics_json);
    const quality = this.plainObject(entry?.result?.data_quality || entry?.run?.data_quality_json);
    const read = (...keys) => {
      for (const key of keys) if (metrics[key] != null && Number.isFinite(Number(metrics[key]))) return Number(metrics[key]);
      return NaN;
    };
    return {
      symbol: String(entry?.symbol || entry?.run?.symbol || "--"),
      returnPct: read("total_return_pct", "return_pct", "total_return"),
      drawdownPct: Math.abs(read("max_drawdown_pct", "max_drawdown")),
      sharpe: read("sharpe_ratio", "sharpe"),
      winRate: read("win_rate_pct", "win_rate"),
      profitFactor: read("profit_factor", "payoff_ratio"),
      trades: Math.max(0, Math.round(read("trade_count", "total_trades", "trades") || 0)),
      coverage: Number(quality.coverage_pct ?? quality.coverage),
    };
  }

  buildSourceBacktestAnalysis(entries, failures = [], expectedCount = entries.length) {
    const rows = entries.map((entry) => this.sourceBacktestMetric(entry));
    const finite = (values) => values.filter(Number.isFinite);
    const average = (values) => {
      const usable = finite(values);
      return usable.length ? usable.reduce((sum, value) => sum + value, 0) / usable.length : NaN;
    };
    const median = (values) => {
      const usable = finite(values).sort((left, right) => left - right);
      if (!usable.length) return NaN;
      const middle = Math.floor(usable.length / 2);
      return usable.length % 2 ? usable[middle] : (usable[middle - 1] + usable[middle]) / 2;
    };
    rows.forEach((row) => {
      row.score = (Number.isFinite(row.returnPct) ? row.returnPct : -100)
        - (Number.isFinite(row.drawdownPct) ? row.drawdownPct * 0.35 : 0)
        + (Number.isFinite(row.sharpe) ? Math.max(-3, Math.min(3, row.sharpe)) * 0.2 : 0);
    });
    const totalTrades = rows.reduce((sum, row) => sum + row.trades, 0);
    const positiveCount = rows.filter((row) => row.returnPct > 0).length;
    const positiveRate = rows.length ? positiveCount / rows.length * 100 : 0;
    const averageReturn = average(rows.map((row) => row.returnPct));
    const medianReturn = median(rows.map((row) => row.returnPct));
    const worstDrawdown = Math.max(0, ...finite(rows.map((row) => row.drawdownPct)));
    const averageSharpe = average(rows.map((row) => row.sharpe));
    const weightedWinRate = totalTrades
      ? rows.reduce((sum, row) => sum + (Number.isFinite(row.winRate) ? row.winRate * row.trades : 0), 0) / totalTrades
      : NaN;
    const failureCategories = failures.reduce((result, failure) => {
      const category = String(failure?.category || "unknown");
      result[category] = (result[category] || 0) + 1;
      return result;
    }, {});
    let verdict = "需要优化";
    if (!rows.length) verdict = "无有效结果";
    else if (rows.length < Math.min(5, expectedCount) || totalTrades < Math.max(10, rows.length * 2)) verdict = "样本不足";
    else if (averageReturn > 0 && medianReturn > 0 && positiveRate >= 60 && worstDrawdown <= 20) verdict = "跨品种较稳";
    else if (averageReturn > 0 && positiveRate >= 45) verdict = "收益较集中";
    const insights = [];
    if (rows.length) insights.push(`成功覆盖 ${rows.length} / ${expectedCount} 个品种，${failures.length} 个失败。`);
    if (failures.length) {
      const labels = { capacity: "容量冲突", transient: "临时服务异常", market_data: "行情数据", strategy: "策略执行", unknown: "其他" };
      const summary = Object.entries(failureCategories).map(([category, count]) => `${labels[category] || category} ${count}`).join("、");
      insights.push(`失败分类：${summary}；系统失败不会计入策略盈亏统计。`);
    }
    if (Number.isFinite(averageReturn)) insights.push(`平均收益 ${averageReturn.toFixed(2)}%，中位数 ${medianReturn.toFixed(2)}%，正收益品种占比 ${positiveRate.toFixed(1)}%。`);
    if (Number.isFinite(worstDrawdown)) insights.push(`最差回撤 ${worstDrawdown.toFixed(2)}%；共 ${totalTrades} 笔交易，避免只凭少数品种判断。`);
    if (verdict === "收益较集中") insights.push("平均收益为正但分布不均，参数可能依赖少数强势品种。建议优先降低过拟合和回撤。 ");
    if (verdict === "需要优化") insights.push("当前参数在跨品种样本上的收益或稳定性不足，可让 AI 在既有参数边界内生成候选后重新复验。 ");
    if (verdict === "样本不足") insights.push("有效品种或成交样本不足，暂不适合据此放大仓位或判断策略有效。 ");
    return {
      verdict,
      expectedCount,
      successCount: rows.length,
      failureCount: failures.length,
      positiveRate,
      averageReturn,
      medianReturn,
      worstDrawdown,
      averageSharpe,
      weightedWinRate,
      totalTrades,
      failureCategories,
      rows: rows.sort((left, right) => right.score - left.score),
      failures,
      insights,
    };
  }

  renderSourceBacktestAnalysis(analysis) {
    this.sourceBacktestAnalysis = analysis;
    const panel = this.querySelector("#strategy-runner-analysis");
    panel.classList.remove("hidden");
    this.querySelector("#strategy-runner-empty").classList.add("hidden");
    this.querySelector("#strategy-runner-verdict").textContent = analysis.verdict;
    const format = (value, suffix = "", digits = 2) => Number.isFinite(Number(value))
      ? `${Number(value).toLocaleString("zh-CN", { maximumFractionDigits: digits })}${suffix}`
      : "--";
    const definitions = [
      ["成功 / 总数", `${analysis.successCount} / ${analysis.expectedCount}`],
      ["正收益品种", format(analysis.positiveRate, "%", 1)],
      ["平均收益", format(analysis.averageReturn, "%")],
      ["收益中位数", format(analysis.medianReturn, "%")],
      ["最差回撤", format(-Math.abs(analysis.worstDrawdown), "%")],
      ["总交易数", format(analysis.totalTrades, "", 0)],
    ];
    this.querySelector("#strategy-runner-analysis-metrics").replaceChildren(...definitions.map(([label, value]) => {
      const node = this.node("article");
      node.append(this.node("span", "", label), this.node("strong", "", value));
      return node;
    }));
    this.querySelector("#strategy-runner-insights").replaceChildren(...analysis.insights.map((message, index) => {
      const row = this.node("p");
      row.append(this.node("span", "", String(index + 1).padStart(2, "0")), this.node("strong", "", message));
      return row;
    }));
    const ranking = this.querySelector("#strategy-runner-ranking");
    const visibleRows = analysis.rows.slice(0, 5);
    const worstRows = analysis.rows.length > 5 ? analysis.rows.slice(-5).reverse() : [];
    const ranked = [...visibleRows, ...worstRows.filter((row) => !visibleRows.includes(row))];
    const head = this.node("header");
    head.append(this.node("strong", "", "品种表现"), this.node("span", "", `显示最佳与最弱 ${ranked.length} 项`));
    ranking.replaceChildren(head, ...ranked.map((row, index) => {
      const item = this.node("article");
      item.append(
        this.node("span", "", String(index + 1).padStart(2, "0")),
        this.node("strong", "", row.symbol),
        this.node("b", row.returnPct > 0 ? "positive" : (row.returnPct < 0 ? "negative" : ""), format(row.returnPct, "%")),
        this.node("small", "", `${row.trades} 笔 · 回撤 ${format(-Math.abs(row.drawdownPct), "%")}`),
      );
      return item;
    }));
    if (analysis.failures.length) {
      const failed = this.node("details", "strategy-runner-failures");
      failed.append(this.node("summary", "", `失败品种 ${analysis.failures.length} 个`));
      analysis.failures.slice(0, 30).forEach((item) => failed.append(this.node("p", "", `${item.symbol} · ${item.message}`)));
      ranking.append(failed);
    }
  }

  async ensureSourceBacktestEligibility() {
    const item = this.activeItem;
    if (!item?.public_id) throw new Error("请先保存当前策略，再运行回测。");
    const eligibleStatuses = ["validated", "backtested", "shadow", "paper", "micro_live", "live"];
    if (eligibleStatuses.includes(item.lifecycle_status)) return item;
    if (!["draft", "published"].includes(item.lifecycle_status)) {
      throw new Error(`当前策略状态“${this.lifecycleLabel(item.lifecycle_status)}”不能运行回测。`);
    }
    this.setSourceRunnerStatus("校验当前版本", "loading");
    this.showSourceRunnerNotice("正在校验当前源码并取得回测资格…");
    const promoted = await this.promoteLifecycle(item, "validated");
    this.activeItem = promoted.item;
    return promoted.item;
  }

  async runSourceBacktest() {
    if (!this.isSourceWorkbench() || this.sourceBacktestRunning) return;
    this.switchSourceWorkbenchTab("backtest");
    if (this.sourceWorkbenchDirty || this.editorMode === "create" || !this.activeItem?.public_id) {
      const saved = await this.persistSourceWorkbench();
      if (!saved) return;
    }
    try {
      await this.ensureSourceBacktestEligibility();
    } catch (error) {
      this.setSourceRunnerStatus("校验失败", "error");
      this.showSourceRunnerNotice(`回测准备失败：${this.localizedErrorMessage(error, "当前策略未通过源码校验")}`, "error");
      return;
    }
    if (!this.sourceBacktestCatalog) await this.loadSourceBacktestCatalog(true);
    const payload = this.sourceBacktestPayload();
    const symbols = this.sourceBacktestScope === "all"
      ? (this.sourceBacktestCatalog?.symbols || []).map((item) => item.value)
      : [payload.symbol].filter(Boolean);
    if (!symbols.length || !payload.timeframe || !payload.start_date || !payload.end_date) {
      this.showSourceRunnerNotice("请选择可用的交易品种、周期和回测区间。", "error");
      return;
    }
    if (payload.start_date > payload.end_date) {
      this.showSourceRunnerNotice("回测开始日期不能晚于结束日期。", "error");
      return;
    }
    this.sourceBacktestRunning = true;
    const buttons = [this.querySelector("#strategy-workbench-run"), this.querySelector("#strategy-runner-submit")];
    buttons.forEach((button) => this.setButtonBusy(button, true, "回放中…"));
    this.setSourceRunnerStatus(this.sourceBacktestScope === "all" ? `0 / ${symbols.length}` : "回放行情", "loading");
    this.showSourceRunnerNotice(this.sourceBacktestScope === "all"
      ? `正在对 ${symbols.length} 个品种执行同参数回测，请保持页面打开。`
      : "正在使用当前不可变源码版本回放历史行情，请稍候…");
    try {
      let completed;
      let failures;
      if (this.sourceBacktestScope === "all") {
        const suite = await this.runSourceBacktestSuite(payload, symbols);
        completed = suite.completed;
        failures = suite.failures;
      } else {
        completed = [await this.executeSourceBacktest({ ...payload, symbol: symbols[0] })];
        failures = [];
      }
      this.sourceBacktestSuite = completed;
      this.sourceBacktestFailures = failures;
      this.sourceBacktestResult = completed[0] || null;
      if (completed.length && this.activeItem?.lifecycle_status === "validated") {
        const promoted = await this.promoteLifecycle(this.activeItem, "backtested");
        this.activeItem = promoted.item;
      }
      if (this.sourceBacktestScope === "single" && completed[0]) {
        this.renderSourceBacktestResult(completed[0].run, completed[0].result);
      } else {
        this.querySelector("#strategy-runner-result").classList.add("hidden");
      }
      const analysis = this.buildSourceBacktestAnalysis(completed, failures, symbols.length);
      this.renderSourceBacktestAnalysis(analysis);
      this.setSourceRunnerStatus("回测完成", "ready");
      this.showSourceRunnerNotice(this.sourceBacktestScope === "all"
        ? `全品种回测完成：成功 ${completed.length}，失败 ${failures.length}。请先看跨品种分布，再决定是否优化参数。`
        : "回测完成。已生成结果分析，可让 AI 在当前参数边界内提出优化候选。", "success");
    } catch (error) {
      this.setSourceRunnerStatus("回测失败", "error");
      this.showSourceRunnerNotice(`回测失败：${this.localizedErrorMessage(error, "请检查行情数据与策略输出")}`, "error");
    } finally {
      this.sourceBacktestRunning = false;
      buttons.forEach((button) => this.setButtonBusy(button, false));
    }
  }

  sourceOptimizationPartitions() {
    const entries = [...this.sourceBacktestSuite].sort((left, right) => String(left?.symbol || "").localeCompare(String(right?.symbol || "")));
    const validation = entries.filter((_, index) => index % 4 === 0);
    const training = entries.filter((_, index) => index % 4 !== 0);
    return { training, validation };
  }

  sourceOptimizationEligibility(analysis) {
    if (this.sourceBacktestScope !== "all" || analysis.expectedCount < 8) return "请先运行全品种回测，再进行参数优化。";
    const coverage = analysis.expectedCount ? analysis.successCount / analysis.expectedCount : 0;
    if (coverage < 0.9) return `有效覆盖率仅 ${(coverage * 100).toFixed(1)}%，请先解决系统失败后再优化参数。`;
    if (analysis.totalTrades < 20) return `当前仅 ${analysis.totalTrades} 笔交易，样本不足以生成可靠候选。请先调整源码信号逻辑。`;
    const partitions = this.sourceOptimizationPartitions();
    if (partitions.training.length < 6 || partitions.validation.length < 2) return "训练组或验证组样本不足，无法执行隔离验证。";
    return "";
  }

  sourceOptimizationScore(analysis) {
    const value = (candidate, fallback = 0) => Number.isFinite(Number(candidate)) ? Number(candidate) : fallback;
    return value(analysis.medianReturn) * 0.55
      + value(analysis.averageReturn) * 0.25
      + value(analysis.positiveRate) * 0.02
      + Math.max(-3, Math.min(3, value(analysis.averageSharpe))) * 0.1
      - value(analysis.worstDrawdown) * 0.25;
  }

  validateSourceOptimizationAnalysis(baseline, candidate) {
    const baselineScore = this.sourceOptimizationScore(baseline);
    const candidateScore = this.sourceOptimizationScore(candidate);
    const minimumCoverage = Math.max(2, Math.floor(baseline.successCount * 0.95));
    const minimumTrades = Math.max(10, Math.floor(baseline.totalTrades * 0.75));
    const drawdownLimit = Math.max(baseline.worstDrawdown + 0.25, baseline.worstDrawdown * 1.1);
    const reasons = [];
    if (candidate.successCount < minimumCoverage) reasons.push(`验证覆盖不足（${candidate.successCount}/${baseline.successCount}）`);
    if (candidate.totalTrades < minimumTrades) reasons.push(`验证交易数下降过多（${candidate.totalTrades}/${baseline.totalTrades}）`);
    if (candidate.medianReturn < baseline.medianReturn) reasons.push("验证组收益中位数没有改善");
    if (candidate.worstDrawdown > drawdownLimit) reasons.push("验证组最差回撤恶化");
    if (candidateScore <= baselineScore + 0.02) reasons.push("综合验证得分没有显著提高");
    const approved = reasons.length === 0;
    return {
      approved,
      baseline,
      candidate,
      baselineScore,
      candidateScore,
      reasons,
      summary: approved
        ? `隔离验证通过：综合得分 ${baselineScore.toFixed(2)} → ${candidateScore.toFixed(2)}，可以确认应用。`
        : `隔离验证未通过：${reasons.join("；")}。候选不会允许应用。`,
    };
  }

  async validateSourceOptimizationCandidate(proposed, validationEntries) {
    const sourceRisk = this.sourceUsesRiskProposal();
    const risk = this.plainObject(proposed.risk_defaults);
    const basePayload = this.sourceBacktestPayload();
    const candidatePayload = {
      ...basePayload,
      params: { ...this.plainObject(proposed.parameters) },
      max_holding_bars: Number(risk.max_holding_bars ?? basePayload.max_holding_bars),
      stop_loss_pct: sourceRisk ? basePayload.stop_loss_pct : Number(risk.stop_loss_pct ?? basePayload.stop_loss_pct),
      take_profit_pct: sourceRisk ? basePayload.take_profit_pct : Number(risk.take_profit_pct ?? basePayload.take_profit_pct),
    };
    const symbols = validationEntries.map((entry) => String(entry?.symbol || "")).filter(Boolean);
    this.showSourceRunnerNotice(`候选已生成，正在对隔离验证组 ${symbols.length} 个品种复测；结果未改善时不会允许应用。`);
    const suite = await this.runSourceBacktestSuite(candidatePayload, symbols);
    const baseline = this.buildSourceBacktestAnalysis(validationEntries, [], symbols.length);
    const candidate = this.buildSourceBacktestAnalysis(suite.completed, suite.failures, symbols.length);
    return this.validateSourceOptimizationAnalysis(baseline, candidate);
  }

  async optimizeSourceBacktestParameters() {
    const analysis = this.sourceBacktestAnalysis;
    if (!analysis || !this.activeItem?.public_id) return;
    const ineligible = this.sourceOptimizationEligibility(analysis);
    if (ineligible) {
      this.showSourceRunnerNotice(ineligible, "error");
      return;
    }
    const partitions = this.sourceOptimizationPartitions();
    const trainingAnalysis = this.buildSourceBacktestAnalysis(partitions.training, [], partitions.training.length);
    const button = this.querySelector("#strategy-runner-ai-optimize");
    this.setButtonBusy(button, true, "AI 分析中…");
    this.sourceBacktestRunning = true;
    this.showSourceRunnerNotice(`AI 仅读取训练组 ${partitions.training.length} 个品种；另有 ${partitions.validation.length} 个品种保留用于隔离验证。`);
    try {
      const compactRows = [
        ...trainingAnalysis.rows.slice(0, 3),
        ...trainingAnalysis.rows.slice(-3),
      ].map((row) => ({
        symbol: row.symbol,
        return_pct: Number.isFinite(row.returnPct) ? Number(row.returnPct.toFixed(3)) : null,
        drawdown_pct: Number.isFinite(row.drawdownPct) ? Number(row.drawdownPct.toFixed(3)) : null,
        sharpe: Number.isFinite(row.sharpe) ? Number(row.sharpe.toFixed(3)) : null,
        trades: row.trades,
      }));
      const parameterBounds = this.activeItem.parameter_schema.filter((definition) => definition.key !== "signal_valid_bars").slice(0, 40).map((definition) => ({
        key: definition.key,
        type: definition.type,
        min: definition.min,
        max: definition.max,
        step: definition.step,
      }));
      const prompt = [
        `你是参数优化助手。仅修改提供的 parameters，以及${this.sourceUsesRiskProposal() ? "最大持有K线" : "止损、止盈、最大持有K线"}；不改名称、分类、说明、源码、仓位、杠杆、手续费和滑点，不新增或删除参数。signal_valid_bars 属于实时信号有效期，不纳入历史收益优化。`,
        "目标是提高跨品种中位数收益、正收益品种占比与稳定性，同时控制最差回撤；不要追逐单一最佳品种，也不要承诺盈利。",
        `当前参数：${JSON.stringify(this.activeItem.parameters)}`,
        `参数约束：${JSON.stringify(parameterBounds)}`,
        `当前风险：${JSON.stringify(this.activeItem.risk_defaults)}`,
        `汇总：${JSON.stringify({
          tested: trainingAnalysis.expectedCount,
          success: trainingAnalysis.successCount,
          failed: trainingAnalysis.failureCount,
          average_return_pct: trainingAnalysis.averageReturn,
          median_return_pct: trainingAnalysis.medianReturn,
          positive_symbol_pct: trainingAnalysis.positiveRate,
          worst_drawdown_pct: trainingAnalysis.worstDrawdown,
          average_sharpe: trainingAnalysis.averageSharpe,
          total_trades: trainingAnalysis.totalTrades,
        })}`,
        `代表品种：${JSON.stringify(compactRows)}`,
        "给出一组保守、可解释的候选参数。变化幅度应小，成交样本不足时优先减少复杂度而不是激进调参。",
      ].join("\n").slice(0, 1950);
      const result = await this.api(`/${encodeURIComponent(this.activeItem.public_id)}/ai-preview`, {
        method: "POST",
        body: JSON.stringify({ prompt }),
      });
      const normalized = this.normalizeBacktestOptimizationProposal(result?.proposed);
      if (!normalized.changes.length) throw new Error("AI 没有生成可执行的有效参数变化");
      const optimizationValidation = await this.validateSourceOptimizationCandidate(normalized.proposed, partitions.validation);
      this.preview = {
        base_version: Number(result?.base_version ?? this.activeItem.version),
        provider: String(result?.provider ?? "AI model"),
        summary: `${String(result?.summary ?? "AI 已根据训练组生成参数候选。")} ${optimizationValidation.summary}`,
        changes: normalized.changes,
        proposed: normalized.proposed,
        proposed_spec: {},
        strategy_code: "",
        source_code: "",
        draft: {},
        profile: null,
        risk_defaults: null,
        composition: null,
        parameter_schema: [],
        parameters: {},
        generated_from_composition: false,
        mode: "parameters",
        optimization_validation: optimizationValidation,
      };
      this.sourceOptimizationValidation = optimizationValidation;
      this.aiWorkingProposed = this.preview.proposed;
      const turn = ++this.aiTurnSequence;
      this.aiConversation.push(
        { id: `optimizer-user-${turn}`, turn, role: "user", content: `基于 ${partitions.training.length} 个训练品种优化，并用 ${partitions.validation.length} 个隔离品种验证`, status: "complete" },
        { id: `optimizer-ai-${turn}`, turn, role: "assistant", content: this.preview.summary, status: optimizationValidation.approved ? "complete" : "error", meta: `${this.providerLabel(this.preview.provider)} · ${this.preview.changes.length} 项候选变化 · ${optimizationValidation.approved ? "验证通过" : "已拒绝"}` },
      );
      this.completeAiProcess(this.preview);
      this.renderAiConversation();
      this.renderPreview();
      this.switchSourceWorkbenchTab("ai");
      this.showAiError(optimizationValidation.approved
        ? "候选已通过隔离品种验证，可以确认应用；应用后仍需运行完整全品种回测。"
        : optimizationValidation.summary, optimizationValidation.approved ? "success" : "error");
    } catch (error) {
      this.showSourceRunnerNotice(`AI 参数优化失败：${this.friendlyMutationError(error)}`, "error");
    } finally {
      this.sourceBacktestRunning = false;
      this.setButtonBusy(button, false);
    }
  }

  normalizeBacktestOptimizationProposal(candidate) {
    const proposed = this.plainObject(candidate);
    const rawParameters = this.plainObject(proposed.parameters);
    const currentParameters = this.plainObject(this.activeItem?.parameters);
    const parameters = {};
    const quantize = (rawValue, definition, fallback) => {
      let value = Number(rawValue);
      if (!Number.isFinite(value)) value = Number(fallback);
      const minimum = Number(definition.min);
      const maximum = Number(definition.max);
      const step = Number(definition.step);
      if (Number.isFinite(minimum)) value = Math.max(minimum, value);
      if (Number.isFinite(maximum)) value = Math.min(maximum, value);
      if (Number.isFinite(step) && step > 0) {
        const origin = Number.isFinite(minimum) ? minimum : 0;
        value = origin + Math.round((value - origin) / step) * step;
      }
      if (definition.type === "integer") value = Math.round(value);
      return Number(value.toFixed(8));
    };
    this.activeItem.parameter_schema.forEach((definition) => {
      const key = String(definition.key || "");
      if (!key) return;
      parameters[key] = key === "signal_valid_bars"
        ? quantize(currentParameters[key], definition, definition.default)
        : quantize(rawParameters[key], definition, currentParameters[key] ?? definition.default);
    });
    const currentRisk = this.plainObject(this.activeItem?.risk_defaults);
    const rawRisk = this.plainObject(proposed.risk_defaults);
    const riskDefaults = { ...currentRisk };
    const riskBounds = this.sourceUsesRiskProposal()
      ? { max_holding_bars: [0, 50000, true] }
      : {
        stop_loss_pct: [0, 99.9, false],
        take_profit_pct: [0, 99.9, false],
        max_holding_bars: [0, 50000, true],
      };
    Object.entries(riskBounds).forEach(([key, [minimum, maximum, integer]]) => {
      const candidateValue = Number(rawRisk[key]);
      if (!Number.isFinite(candidateValue)) return;
      const bounded = Math.max(minimum, Math.min(maximum, candidateValue));
      riskDefaults[key] = integer ? Math.round(bounded) : Number(bounded.toFixed(6));
    });
    const safeProposed = {
      name: this.activeItem.name,
      description: this.activeItem.description,
      category: this.activeItem.category,
      parameters,
      risk_defaults: riskDefaults,
    };
    const changes = [];
    Object.entries(parameters).forEach(([key, value]) => {
      if (Number(currentParameters[key]) !== Number(value)) changes.push({ path: `parameters.${key}`, before: currentParameters[key], after: value });
    });
    Object.entries(riskDefaults).forEach(([key, value]) => {
      if (Number(currentRisk[key]) !== Number(value)) changes.push({ path: `risk_defaults.${key}`, before: currentRisk[key], after: value });
    });
    return { proposed: safeProposed, changes };
  }

  renderSourceBacktestResult(run, result) {
    const metrics = this.plainObject(result?.metrics);
    const account = this.plainObject(result?.account);
    const metric = (...keys) => {
      for (const key of keys) if (metrics[key] != null) return Number(metrics[key]);
      return NaN;
    };
    const number = (value, digits = 2) => Number.isFinite(Number(value)) ? Number(value).toLocaleString("zh-CN", { maximumFractionDigits: digits }) : "--";
    const percent = (value) => Number.isFinite(Number(value)) ? `${Number(value) > 0 ? "+" : ""}${number(value)}%` : "--";
    const totalReturn = metric("total_return_pct", "return_pct", "total_return");
    this.querySelector("#strategy-runner-empty").classList.add("hidden");
    this.querySelector("#strategy-runner-result").classList.remove("hidden");
    this.querySelector("#strategy-runner-result-meta").textContent = `${run.symbol || "--"} · ${run.timeframe || "--"} · v${this.activeItem.version}`;
    this.querySelector("#strategy-runner-result-title").textContent = this.activeItem.name;
    const returnNode = this.querySelector("#strategy-runner-return");
    returnNode.textContent = percent(totalReturn);
    returnNode.className = Number(totalReturn) > 0 ? "positive" : (Number(totalReturn) < 0 ? "negative" : "neutral");
    const definitions = [
      ["累计收益", percent(totalReturn)],
      ["最大回撤", percent(-Math.abs(metric("max_drawdown_pct", "max_drawdown")))],
      ["夏普比率", number(metric("sharpe_ratio", "sharpe"))],
      ["胜率", percent(metric("win_rate_pct", "win_rate"))],
      ["收益因子", number(metric("profit_factor", "payoff_ratio"))],
      ["交易次数", number(metric("trade_count", "total_trades", "trades"), 0)],
    ];
    this.querySelector("#strategy-runner-metrics").replaceChildren(...definitions.map(([label, value]) => {
      const node = this.node("article");
      node.append(this.node("span", "", label), this.node("strong", "", value));
      return node;
    }));
    const trades = Array.isArray(result?.trades) ? result.trades : [];
    const tradeBox = this.querySelector("#strategy-runner-trades");
    const quality = this.plainObject(result?.data_quality);
    const coverage = Number(quality.coverage_pct ?? quality.coverage);
    const finalEquity = account.final_equity ?? account.ending_capital ?? account.equity;
    tradeBox.replaceChildren(
      this.node("strong", "", `最近成交 · ${trades.length} 笔`),
      this.node("span", "", `期末权益 ${number(finalEquity)} · 行情覆盖 ${Number.isFinite(coverage) ? `${number(coverage <= 1 ? coverage * 100 : coverage)}%` : "已检查"}`),
    );
    trades.slice(-4).reverse().forEach((trade) => {
      const pnl = Number(trade.net_pnl ?? trade.pnl ?? 0);
      const row = this.node("div");
      row.append(
        this.node("span", "", String(trade.side ?? trade.direction ?? "--").toUpperCase()),
        this.node("span", "", `${number(trade.entry_price)} → ${number(trade.exit_price)}`),
        this.node("b", pnl > 0 ? "positive" : (pnl < 0 ? "negative" : ""), `${pnl > 0 ? "+" : ""}${number(pnl)}`),
      );
      tradeBox.append(row);
    });
    window.requestAnimationFrame(() => this.drawSourceBacktestChart(result));
  }

  drawSourceBacktestChart(result) {
    const canvas = this.querySelector("#strategy-runner-canvas");
    const width = Math.floor(canvas.clientWidth);
    const height = Math.floor(canvas.clientHeight);
    if (!width || !height) return;
    const points = (Array.isArray(result?.equity_curve) ? result.equity_curve : []).map((point) => Number(Array.isArray(point) ? point[1] : point?.equity ?? point?.value ?? point?.balance)).filter(Number.isFinite);
    const ratio = Math.min(window.devicePixelRatio || 1, 2);
    canvas.width = Math.floor(width * ratio);
    canvas.height = Math.floor(height * ratio);
    const context = canvas.getContext("2d");
    context.setTransform(ratio, 0, 0, ratio, 0, 0);
    context.clearRect(0, 0, width, height);
    if (points.length < 2) {
      context.fillStyle = "#61798f";
      context.font = "12px Inter, sans-serif";
      context.fillText("权益样本不足，暂无曲线", 16, 28);
      return;
    }
    const padding = { left: 8, right: 8, top: 14, bottom: 14 };
    let min = Math.min(...points);
    let max = Math.max(...points);
    const extra = (max - min || Math.abs(max) * .01 || 1) * .08;
    min -= extra;
    max += extra;
    const x = (index) => padding.left + index / (points.length - 1) * (width - padding.left - padding.right);
    const y = (value) => padding.top + (max - value) / (max - min || 1) * (height - padding.top - padding.bottom);
    context.strokeStyle = "rgba(125, 155, 183, .12)";
    for (let row = 0; row <= 3; row += 1) {
      const yValue = padding.top + row / 3 * (height - padding.top - padding.bottom);
      context.beginPath(); context.moveTo(padding.left, yValue); context.lineTo(width - padding.right, yValue); context.stroke();
    }
    const gradient = context.createLinearGradient(0, 0, 0, height);
    gradient.addColorStop(0, "rgba(43, 215, 163, .24)");
    gradient.addColorStop(1, "rgba(43, 215, 163, 0)");
    context.beginPath();
    points.forEach((value, index) => index ? context.lineTo(x(index), y(value)) : context.moveTo(x(index), y(value)));
    context.lineTo(x(points.length - 1), height - padding.bottom); context.lineTo(x(0), height - padding.bottom); context.closePath();
    context.fillStyle = gradient; context.fill();
    context.beginPath();
    points.forEach((value, index) => index ? context.lineTo(x(index), y(value)) : context.moveTo(x(index), y(value)));
    context.strokeStyle = "#2bd7a3"; context.lineWidth = 2; context.lineJoin = "round"; context.stroke();
  }

  async requestEditScope(scope) {
    if (this.editorMode !== "edit" || scope === this.editScope) return;
    if (scope === "parameters" && this.editScope === "source") {
      if (this.sourceWorkbenchDirty) {
        this.setCodeStatus("请先保存当前源码版本，再配置由源码生成的参数", "error");
        this.showAiError("参数结构来自已保存的 Python 源码。请先校验并保存当前源码，再进入参数配置。", "error");
        return;
      }
      if (this.activeItem?.strategy_kind === "source_strategy") {
        const validation = await this.validateStrategyCode();
        if (!validation) return;
      }
    }
    this.switchEditScope(scope, { resetCode: false });
  }

  syncSourceParameterContract(validation = {}) {
    const declared = Array.isArray(validation.parameter_schema)
      ? validation.parameter_schema.map((definition) => ({ ...definition }))
      : [];
    const parameterKeys = new Set(
      Array.isArray(validation.parameter_keys) ? validation.parameter_keys.map(String) : []
    );
    const existingDefinitions = new Map(
      [
        ...(Array.isArray(this.activeItem?.parameter_schema) ? this.activeItem.parameter_schema : []),
        ...(Array.isArray(this.sourceParameterSchema) ? this.sourceParameterSchema : []),
      ].map((definition) => [String(definition.key || ""), { ...definition }])
    );
    const schema = declared.length
      ? declared
      : [...existingDefinitions.values()].filter((definition) => parameterKeys.has(String(definition.key)));
    const defaults = this.plainObject(validation.parameters);
    const current = {
      ...this.plainObject(this.activeItem?.parameters),
      ...this.plainObject(this.sourceParameterValues),
    };
    const values = {};
    schema.forEach((definition) => {
      const key = String(definition.key || "");
      let value = Object.prototype.hasOwnProperty.call(current, key)
        ? current[key]
        : (defaults[key] ?? definition.default);
      const numeric = Number(value);
      if (
        !Number.isFinite(numeric)
        || (definition.min != null && numeric < Number(definition.min))
        || (definition.max != null && numeric > Number(definition.max))
      ) value = defaults[key] ?? definition.default;
      values[key] = value;
    });
    this.sourceParameterSchema = schema;
    this.sourceParameterValues = values;
    if (this.editScope === "parameters") this.renderParameterFields(schema, values);
  }

  switchEditScope(scope, { resetCode = true } = {}) {
    if (this.editorMode !== "edit") return;
    const previousScope = this.editScope;
    if (["code", "source"].includes(previousScope)) {
      this.codeBuffers[previousScope] = this.querySelector("#strategy-code-editor").value;
    }
    const dslAvailable = this.activeItem?.strategy_kind === "full_strategy" && this.plainObject(this.activeItem?.spec);
    const sourceAvailable = ["full_strategy", "source_strategy"].includes(this.activeItem?.strategy_kind);
    this.editScope = scope === "code" && dslAvailable
      ? "code"
      : (scope === "source" && sourceAvailable ? "source" : "parameters");
    this.querySelectorAll("[data-edit-scope]").forEach((button) => {
      const active = button.dataset.editScope === this.editScope;
      button.classList.toggle("active", active);
      button.setAttribute("aria-pressed", String(active));
    });
    const codeMode = ["code", "source"].includes(this.editScope);
    const sourceMode = this.editScope === "source";
    this.setSourceWorkbenchActive(sourceMode);
    this.setSourceWorkbenchShellActive(
      sourceAvailable && ["source", "parameters"].includes(this.editScope),
    );
    this.querySelector("#strategy-code-block").classList.toggle("hidden", !codeMode);
    this.querySelector("#strategy-source-composition-block").classList.add("hidden");
    if (codeMode) {
      this.querySelector("#strategy-parameters-block").classList.add("hidden");
      this.querySelector("#strategy-risk-block").classList.add("hidden");
      if (resetCode) {
        this.codeBuffers[this.editScope] = sourceMode
          ? (this.activeItem.source_code || String(this.sourceRuntime.conversion_starter_source || this.sourceRuntime.starter_source || ""))
          : this.strategyCode(this.activeItem.spec);
      }
      this.querySelector("#strategy-code-editor").value = this.codeBuffers[this.editScope] || "";
      this.renderCodeLines();
      this.setCodeStatus("当前版本代码，尚未重新校验");
      this.querySelector("#strategy-code-title").textContent = sourceMode ? "Python 策略源码" : "策略 DSL 配置";
      this.querySelector("#strategy-code-runtime").textContent = sourceMode ? "Python · sandbox v1 · PARAMETERS 驱动配置" : "JSON · 声明式 DSL · 保存生成新版本";
      this.querySelector("#strategy-code-label").textContent = sourceMode ? "可执行 evaluate(context, params) 源码" : "完整策略 DSL";
      this.querySelector("#strategy-code-format").classList.toggle("hidden", sourceMode);
      this.querySelector("#strategy-code-guard").textContent = sourceMode
        ? "这是真正执行的 Python 策略逻辑。顶层 PARAMETERS 决定参数配置项；禁止 import、对象属性、文件、网络、系统调用和动态执行。"
        : "DSL 是声明式策略配置，不是 Python 源码；由平台固定求值器执行。";
      this.querySelector("#strategy-edit-scope-help").textContent = sourceMode ? "维护真正执行的 Python 策略函数" : "维护声明式入场、退出、风险与执行 DSL";
      this.querySelector("#strategy-ai-title").textContent = sourceMode ? "用自然语言编排完整策略" : "用自然语言修改策略代码";
      this.querySelector("#strategy-ai-description").textContent = sourceMode
        ? "描述策略目标即可。AI 会同步拟定名称、分类、说明、指标组合、运行约束、风险参数与完整 Python 源码。"
        : "描述要调整的指标组合、方向、周期、入场、退出或风险逻辑。模型只生成受控策略 DSL 配置预览。";
      this.querySelector("#strategy-ai-status").lastChild.textContent = sourceMode ? "Python 隔离运行时" : "受控策略 DSL";
      this.querySelector("#strategy-ai-preview-button strong").textContent = "发送给 AI";
      this.querySelector("#strategy-ai-prompt").placeholder = "例如：做一个 1 小时超买超卖反转策略，自动选择合适指标，只做胜率优先的保守信号。";
    } else {
      this.querySelector("#strategy-code-format").classList.remove("hidden");
      const sourceStrategy = this.activeItem?.strategy_kind === "source_strategy";
      this.querySelector("#strategy-source-composition-block").classList.toggle("hidden", !sourceStrategy);
      this.querySelector("#strategy-basic-index").textContent = sourceStrategy ? "02" : "01";
      this.querySelector("#strategy-parameters-index").textContent = sourceStrategy ? "03" : "02";
      this.querySelector("#strategy-risk-index").textContent = sourceStrategy ? "04" : "03";
      if (sourceStrategy) this.renderIndicatorComposer("edit");
      this.renderParameterFields(
        sourceStrategy ? this.sourceParameterSchema : this.activeItem.parameter_schema,
        sourceStrategy ? this.sourceParameterValues : this.activeItem.parameters,
      );
      this.renderRiskFields(sourceStrategy ? this.sourceRiskDefaults : this.activeItem.risk_defaults);
      this.querySelector("#strategy-edit-scope-help").textContent = sourceStrategy
        ? "字段由已保存 Python 源码中的 PARAMETERS 动态生成"
        : "仅调整已定义参数，不改变策略结构";
      this.querySelector("#strategy-parameters-help").textContent = sourceStrategy
        ? "由 Python 源码中的 PARAMETERS 动态生成"
        : "字段范围由策略模型约束";
      this.querySelector("#strategy-ai-title").textContent = sourceStrategy ? "用自然语言编排完整策略" : "用自然语言维护策略参数";
      this.querySelector("#strategy-ai-description").textContent = sourceStrategy
        ? "可连续描述策略目标。AI 每轮都会结合上一轮草稿，自动调整策略身份、指标与运行约束、源码参数和风险默认值。"
        : "可连续讨论参数或风险默认值。每轮都基于上一轮草稿继续优化，不会直接保存。";
      this.querySelector("#strategy-ai-status").lastChild.textContent = "受约束参数";
      this.querySelector("#strategy-ai-preview-button strong").textContent = "发送给 AI";
      this.querySelector("#strategy-ai-prompt").placeholder = sourceStrategy
        ? "例如：使用 EMA、RSI、MACD 和布林带，只在超买超卖共振时开单；策略名称和参数也请自动调整。"
        : "例如：把止损改为 2%，止盈改为 6%，最大持仓改成 72 根 K 线。";
    }
    this.querySelector("#strategy-ai-prompt").value = "";
    this.clearPreview();
    this.showAiError("");
  }

  strategyCode(spec) {
    try { return JSON.stringify(this.plainObject(spec), null, 2); } catch (_) { return "{}"; }
  }

  parseStrategyCode() {
    const raw = this.querySelector("#strategy-code-editor").value.trim();
    if (!raw) throw new Error("策略代码不能为空。");
    if (this.editScope === "source" || (this.editorMode === "create" && this.createMode === "source")) return `${raw}\n`;
    let spec;
    try { spec = JSON.parse(raw); } catch (error) {
      throw new Error(`JSON 语法错误：${error?.message || "无法解析"}`);
    }
    if (!spec || Array.isArray(spec) || typeof spec !== "object") throw new Error("策略代码根节点必须是 JSON 对象。");
    return spec;
  }

  formatStrategyCode() {
    try {
      const spec = this.parseStrategyCode();
      this.querySelector("#strategy-code-editor").value = JSON.stringify(spec, null, 2);
      this.setCodeStatus("格式化完成，尚未校验");
      this.showFormError("");
    } catch (error) {
      this.setCodeStatus(error?.message || "代码格式化失败", "error");
    }
  }

  async validateStrategyCode() {
    const sourceCreate = this.editorMode === "create" && this.createMode === "source";
    if (!sourceCreate && (!this.activeItem?.public_id || !["code", "source"].includes(this.editScope))) return null;
    let code;
    try { code = this.parseStrategyCode(); } catch (error) {
      this.setCodeStatus(error?.message || "策略代码无法解析", "error");
      return null;
    }
    const button = this.querySelector("#strategy-code-validate");
    this.setButtonBusy(button, true, "校验中…");
    try {
      const sourceMode = sourceCreate || this.editScope === "source";
      const path = sourceCreate
        ? "/runtime/python/validate"
        : (sourceMode
          ? `/${encodeURIComponent(this.activeItem.public_id)}/source/validate`
          : `/${encodeURIComponent(this.activeItem.public_id)}/code/validate`);
      const result = await this.api(path, {
        method: "POST",
        body: JSON.stringify(sourceMode ? { language: "python", source_code: code } : { spec: code }),
      });
      if (result?.normalized_spec) {
        this.querySelector("#strategy-code-editor").value = JSON.stringify(result.normalized_spec, null, 2);
        this.codeBuffers.code = this.querySelector("#strategy-code-editor").value;
      }
      if (sourceMode) this.syncSourceParameterContract(result);
      this.setCodeStatus(`校验通过 · ${String(result?.source_hash || result?.spec_hash || "").slice(0, 12)}`, "success");
      this.showFormError("");
      return result;
    } catch (error) {
      this.setCodeStatus(this.friendlyMutationError(error), "error");
      return null;
    } finally {
      this.setButtonBusy(button, false);
    }
  }

  setCodeStatus(message, tone = "") {
    const target = this.querySelector("#strategy-code-status");
    target.textContent = message;
    target.className = `strategy-code-status${tone ? ` ${tone}` : ""}`;
  }

  populateTemplateSelect() {
    const select = this.querySelector("#strategy-template");
    const ordered = [...this.templates].sort((a, b) => Number(b.template_kind === "strategy") - Number(a.template_kind === "strategy"));
    if (!ordered.length) {
      select.replaceChildren(this.option("", "暂无可用模板"));
      select.disabled = true;
      return;
    }
    select.disabled = false;
    select.replaceChildren(...ordered.map((template) => this.option(template.template_key, `${template.template_kind === "strategy" ? "完整策略" : "旧版信号"} · ${template.name}`)));
  }

  switchCreateMode(mode, { resetValues = true, keepComposition = false } = {}) {
    if (this.editorMode !== "create") return;
    if (this.createMode === "source") this.codeBuffers.source = this.querySelector("#strategy-code-editor").value;
    this.createMode = ["template", "source"].includes(mode) ? mode : "indicators";
    if (this.createMode === "source" && !keepComposition) this.sourceComposition = null;
    this.querySelectorAll("[data-create-mode]").forEach((button) => {
      const active = button.dataset.createMode === this.createMode;
      button.classList.toggle("active", active);
      button.setAttribute("aria-pressed", String(active));
    });
    const templateMode = this.createMode === "template";
    const sourceMode = this.createMode === "source";
    this.setSourceWorkbenchActive(sourceMode);
    this.setSourceWorkbenchShellActive(sourceMode);
    this.querySelector("#strategy-source-composition-block").classList.add("hidden");
    this.querySelector("#strategy-indicator-composer").classList.toggle("hidden", templateMode || sourceMode);
    this.querySelector("#strategy-template-composer").classList.toggle("hidden", !templateMode);
    this.querySelector("#strategy-code-block").classList.toggle("hidden", !sourceMode);
    this.querySelector("#strategy-parameters-block").classList.toggle("hidden", !templateMode);
    this.querySelector("#strategy-ai-panel").classList.toggle("hidden", templateMode);
    this.querySelector("#strategy-create-mode-title").textContent = sourceMode ? "源码运行时" : (templateMode ? "选择模板" : "指标与运行约束");
    this.querySelector("#strategy-create-mode-help").textContent = sourceMode ? "Python sandbox v1" : (templateMode ? "复制系统策略后独立管理版本" : "由 AI 自动选择，可在应用草稿后人工复核");
    this.querySelector("#strategy-parameters-index").textContent = templateMode ? "03" : "--";
    this.querySelector("#strategy-risk-index").textContent = templateMode ? "04" : "03";
    if (sourceMode) {
      this.querySelector("#strategy-code-title").textContent = "Python 策略源码";
      this.querySelector("#strategy-code-runtime").textContent = "Python · sandbox v1 · PARAMETERS 自动生成参数配置";
      this.querySelector("#strategy-code-label").textContent = "可执行 evaluate(context, params) 源码";
      this.querySelector("#strategy-code-format").classList.add("hidden");
      this.querySelector("#strategy-code-guard").textContent = "这是真正执行的 Python 策略逻辑；顶层 PARAMETERS 会自动生成参数配置，禁止 import、文件、网络、系统调用和动态执行。";
      if (resetValues && !this.codeBuffers.source) this.codeBuffers.source = String(this.sourceRuntime.starter_source || "");
      this.querySelector("#strategy-code-editor").value = this.codeBuffers.source || String(this.sourceRuntime.starter_source || "");
      this.renderCodeLines();
      this.querySelector("#strategy-ai-title").textContent = "用自然语言编排完整策略";
      this.querySelector("#strategy-ai-description").textContent = "AI 会同步生成策略身份、指标与运行约束、风险默认值和完整 Python 源码，服务端审查通过后才可保存。";
      this.querySelector("#strategy-ai-status").lastChild.textContent = "Python 隔离运行时";
      this.querySelector("#strategy-ai-preview-button strong").textContent = "发送给 AI";
    } else {
      this.querySelector("#strategy-code-format").classList.remove("hidden");
      if (!templateMode) {
        this.querySelector("#strategy-ai-title").textContent = "用自然语言编排完整策略";
        this.querySelector("#strategy-ai-description").textContent = "AI 会根据描述自动选择指标与运行约束，并同步生成名称、分类、说明、风险默认值和完整 Python 源码。";
        this.querySelector("#strategy-ai-status").lastChild.textContent = "自动编排 + Python 沙箱";
        this.querySelector("#strategy-ai-preview-button strong").textContent = "发送给 AI";
        this.querySelector("#strategy-ai-prompt").placeholder = "例如：使用 EMA、RSI、MACD 和布林带，只在超买超卖共振时开单；名称、参数与风险也请自动设置。";
      }
    }
    if (!resetValues) return;
    if (sourceMode) {
      this.querySelector("#strategy-name").value = "Python EMA 策略";
      this.querySelector("#strategy-category").value = "源码策略";
      this.querySelector("#strategy-description").value = "可编辑 Python 源码，参数与代码独立版本化。";
      this.renderRiskFields(this.plainObject(this.indicatorDefaults.risk_defaults));
    } else if (templateMode) {
      const select = this.querySelector("#strategy-template");
      this.applyTemplate(select.value);
    } else {
      this.querySelector("#strategy-name").value = "多指标组合策略";
      this.querySelector("#strategy-category").value = "指标组合";
      this.querySelector("#strategy-description").value = "多个标准指标加权确认，并通过波动与成交量过滤。";
      this.renderParameterFields([], {});
      this.renderRiskFields(this.plainObject(this.indicatorDefaults.risk_defaults));
    }
  }

  applyTemplate(key) {
    const template = this.templates.find((item) => item.template_key === String(key));
    if (!template) {
      this.querySelector("#strategy-name").value = "";
      this.querySelector("#strategy-category").value = "自定义";
      this.querySelector("#strategy-description").value = "";
      this.renderParameterFields([], {});
      this.renderRiskFields({});
      return;
    }
    this.querySelector("#strategy-name").value = template.name;
    this.querySelector("#strategy-category").value = template.category;
    this.querySelector("#strategy-description").value = template.description;
    this.renderParameterFields(template.parameter_schema, template.parameters);
    this.renderRiskFields(template.risk_defaults);
  }

  setSelectedIndicators(selections, context = "create") {
    this.selectedIndicators = new Map();
    (Array.isArray(selections) ? selections : []).forEach((selection) => {
      const key = String(selection?.key || "");
      const indicator = this.indicators.find((item) => item.key === key);
      if (!indicator || this.selectedIndicators.has(key)) return;
      const supplied = this.plainObject(selection.parameters);
      const parameters = {};
      (indicator.parameters || []).forEach((definition) => {
        parameters[definition.key] = supplied[definition.key] ?? definition.default;
      });
      this.selectedIndicators.set(key, {
        key,
        weight: Number(selection.weight ?? 1),
        parameters,
      });
    });
    this.renderIndicatorComposer(context);
  }

  indicatorComposerTargets(context = "create") {
    return context === "edit"
      ? { picker: "#strategy-source-indicator-picker", selected: "#strategy-source-selected-indicators" }
      : { picker: "#strategy-indicator-picker", selected: "#strategy-selected-indicators" };
  }

  renderIndicatorComposer(context = "create") {
    const targets = this.indicatorComposerTargets(context);
    const picker = this.querySelector(targets.picker);
    picker.replaceChildren(...this.indicators.map((indicator) => {
      const label = this.node("label", `strategy-indicator-option ${this.selectedIndicators.has(indicator.key) ? "selected" : ""}`);
      const checkbox = document.createElement("input");
      checkbox.type = "checkbox";
      checkbox.checked = this.selectedIndicators.has(indicator.key);
      checkbox.addEventListener("change", () => this.toggleIndicator(indicator.key, checkbox.checked, context));
      const copy = this.node("span");
      copy.append(this.node("strong", "", indicator.name), this.node("small", "", `${indicator.category} · ${indicator.role === "filter" ? "过滤" : "方向"}`));
      label.append(checkbox, copy);
      return label;
    }));
    const selected = this.querySelector(targets.selected);
    if (!this.selectedIndicators.size) {
      selected.replaceChildren(this.node("div", "strategy-selection-empty", "请从上方至少勾选两个指标。"));
      return;
    }
    selected.replaceChildren(...[...this.selectedIndicators.values()].map((selection) => this.selectedIndicatorPanel(selection, context)));
  }

  toggleIndicator(key, enabled, context = "create") {
    this.syncSelectedIndicatorValues(context);
    if (enabled) {
      const indicator = this.indicators.find((item) => item.key === key);
      if (indicator && !this.selectedIndicators.has(key)) {
        this.selectedIndicators.set(key, {
          key,
          weight: 1,
          parameters: Object.fromEntries((indicator.parameters || []).map((definition) => [definition.key, definition.default])),
        });
      }
    } else this.selectedIndicators.delete(key);
    this.renderIndicatorComposer(context);
    if (context === "edit") this.markSourceCompositionDirty();
  }

  selectedIndicatorPanel(selection, context = "create") {
    const indicator = this.indicators.find((item) => item.key === selection.key) || {};
    const panel = this.node("section", "strategy-selected-indicator");
    const header = this.node("header");
    header.append(this.node("strong", "", indicator.name || selection.key), this.node("small", "", indicator.description || "标准指标参数"));
    const fields = this.node("div", "strategy-field-grid two");
    const definitions = [
      { key: "weight", label: "组合权重", type: "number", min: 0.1, max: 5, step: 0.1, default: 1 },
      ...(indicator.parameters || []),
    ];
    definitions.forEach((definition) => {
      const current = definition.key === "weight" ? selection.weight : selection.parameters?.[definition.key];
      const field = this.configField(definition, current, context === "edit" ? "source-indicator" : "indicator");
      const input = field.querySelector("input, select");
      input.dataset.indicatorKey = selection.key;
      input.dataset.indicatorParam = definition.key;
      input.dataset.indicatorContext = context;
      fields.append(field);
    });
    panel.append(header, fields);
    return panel;
  }

  syncSelectedIndicatorValues(context = "create") {
    this.querySelectorAll(`[data-indicator-context="${context}"][data-indicator-key][data-indicator-param]`).forEach((input) => {
      const selection = this.selectedIndicators.get(input.dataset.indicatorKey);
      if (!selection) return;
      const value = Number(input.value);
      if (!Number.isFinite(value)) return;
      if (input.dataset.indicatorParam === "weight") selection.weight = value;
      else selection.parameters[input.dataset.indicatorParam] = input.dataset.configType === "integer" ? Math.trunc(value) : value;
    });
  }

  collectIndicatorSelections(context = "create") {
    this.syncSelectedIndicatorValues(context);
    return [...this.selectedIndicators.values()].map((selection) => ({
      key: selection.key,
      weight: selection.weight,
      parameters: { ...selection.parameters },
    }));
  }

  deriveSourceComposition(item = {}) {
    const source = String(item.source_code || "").toLowerCase();
    const values = this.plainObject(item.parameters);
    const validation = this.plainObject(item.source_validation);
    const dataRequirements = this.plainObject(validation.data_requirements);
    const indicatorAliases = {
      ema: ["ema(", "ema_"], macd: ["macd", "macd_"], rsi: ["rsi(", "rsi_"],
      bollinger: ["bollinger", "bollinger_"], adx: ["adx(", "adx_"],
      donchian: ["donchian", "donchian_"], volume_ratio: ["volume", "volume_ratio_"],
      atr: ["atr(", "atr_"],
    };
    const valueKeys = Object.keys(values);
    const selections = this.indicators.filter((indicator) => {
      const aliases = indicatorAliases[indicator.key] || [`${indicator.key}_`];
      return valueKeys.some((key) => key.startsWith(`${indicator.key}_`))
        || aliases.some((alias) => source.includes(alias));
    }).map((indicator) => {
      const parameters = {};
      (indicator.parameters || []).forEach((definition) => {
        const canonical = `${indicator.key}_${definition.key}`;
        const genericPeriod = definition.key.includes("period") ? `${indicator.key}_period` : "";
        parameters[definition.key] = values[canonical]
          ?? (genericPeriod ? values[genericPeriod] : undefined)
          ?? definition.default;
      });
      return {
        key: indicator.key,
        weight: Number(values[`${indicator.key}_weight`] ?? 1),
        parameters,
      };
    });
    return {
      indicators: selections,
      timeframe: String(validation.trigger_timeframe || dataRequirements.trigger_timeframe || "1h"),
      directions: Array.isArray(validation.directions) && validation.directions.length
        ? [...validation.directions]
        : ["long", "short"],
      confirmation_threshold: Number(values.confirmation_threshold ?? 60),
      signal_valid_bars: Number(values.signal_valid_bars ?? validation.valid_for_bars ?? 2),
    };
  }

  populateSourceCompositionEditor(composition = {}) {
    const value = this.plainObject(composition);
    this.setSelectedIndicators(Array.isArray(value.indicators) ? value.indicators : [], "edit");
    this.querySelector("#strategy-source-timeframe").value = ["15m", "1h", "4h"].includes(value.timeframe) ? value.timeframe : "1h";
    this.querySelector("#strategy-source-confirmation-threshold").value = String(value.confirmation_threshold ?? 60);
    this.querySelector("#strategy-source-signal-valid-bars").value = String(value.signal_valid_bars ?? 2);
    const directions = Array.isArray(value.directions) ? value.directions : ["long", "short"];
    this.querySelector("#strategy-source-direction-long").checked = directions.includes("long");
    this.querySelector("#strategy-source-direction-short").checked = directions.includes("short");
    this.sourceCompositionDirty = false;
    const state = this.querySelector("#strategy-source-composition-state");
    state.textContent = this.selectedIndicators.size >= 2 ? "已从当前源码识别指标约束" : "未识别到完整指标组合，可重新选择";
    state.className = this.selectedIndicators.size >= 2 ? "synced" : "warning";
  }

  collectSourceComposition() {
    const indicators = this.collectIndicatorSelections("edit");
    const directions = [
      this.querySelector("#strategy-source-direction-long").checked ? "long" : null,
      this.querySelector("#strategy-source-direction-short").checked ? "short" : null,
    ].filter(Boolean);
    if (indicators.length < 2) throw new Error("指标组合至少需要两个指标。");
    if (!directions.length) throw new Error("请至少选择一个允许交易方向。");
    return {
      indicators,
      timeframe: this.querySelector("#strategy-source-timeframe").value,
      directions,
      confirmation_threshold: Number(this.querySelector("#strategy-source-confirmation-threshold").value),
      signal_valid_bars: Number(this.querySelector("#strategy-source-signal-valid-bars").value),
    };
  }

  normalizeSourceAiDraft(draft) {
    const value = this.plainObject(draft);
    if (!Array.isArray(value.indicators) || value.indicators.length < 2) {
      throw new Error("AI 没有返回完整的指标组合，请重新描述策略目标。");
    }
    return {
      profile: {
        name: String(value.name || this.activeItem?.name || "AI 源码策略").slice(0, 64),
        category: String(value.category || this.activeItem?.category || "源码策略").slice(0, 32),
        description: String(value.description || this.activeItem?.description || "").slice(0, 500),
        risk_defaults: this.plainObject(value.risk_defaults),
      },
      composition: {
        indicators: value.indicators.map((item) => ({
          key: String(item.key || ""),
          weight: Number(item.weight ?? 1),
          parameters: this.plainObject(item.parameters),
        })),
        timeframe: String(value.timeframe || "1h"),
        directions: Array.isArray(value.directions) ? value.directions.map(String) : ["long", "short"],
        confirmation_threshold: Number(value.confirmation_threshold ?? 60),
        signal_valid_bars: Number(value.signal_valid_bars ?? 2),
      },
    };
  }

  sourceAiDraftChanges(profile, composition) {
    const changes = [];
    const currentProfile = this.aiWorkingProfile || {
      name: this.querySelector("#strategy-name").value,
      category: this.querySelector("#strategy-category").value,
      description: this.querySelector("#strategy-description").value,
      risk_defaults: this.sourceRiskDefaults,
    };
    ["name", "category", "description"].forEach((key) => {
      if (String(currentProfile[key] || "") !== String(profile[key] || "")) {
        changes.push({ path: key, before: currentProfile[key], after: profile[key] });
      }
    });
    const currentRisk = this.plainObject(currentProfile.risk_defaults);
    Object.entries(this.plainObject(profile.risk_defaults)).forEach(([key, after]) => {
      if (currentRisk[key] !== after) changes.push({ path: `risk_defaults.${key}`, before: currentRisk[key], after });
    });
    const previous = this.aiWorkingComposition || this.sourceComposition || {};
    const indicatorNames = (value) => (Array.isArray(value?.indicators) ? value.indicators : [])
      .map((item) => this.indicators.find((definition) => definition.key === item.key)?.name || item.key)
      .filter(Boolean)
      .join(" + ");
    const beforeIndicators = indicatorNames(previous);
    const afterIndicators = indicatorNames(composition);
    if (beforeIndicators !== afterIndicators) changes.push({ path: "indicators", before: beforeIndicators || "未识别", after: afterIndicators });
    ["timeframe", "confirmation_threshold", "signal_valid_bars"].forEach((key) => {
      if (String(previous[key] ?? "") !== String(composition[key] ?? "")) {
        changes.push({ path: key, before: previous[key], after: composition[key] });
      }
    });
    const beforeDirections = Array.isArray(previous.directions) ? previous.directions.join(" / ") : "";
    const afterDirections = composition.directions.join(" / ");
    if (beforeDirections !== afterDirections) changes.push({ path: "directions", before: beforeDirections || "未识别", after: afterDirections });
    return changes;
  }

  markSourceCompositionDirty() {
    if (this.editorMode !== "edit" || this.activeItem?.strategy_kind !== "source_strategy") return;
    this.sourceCompositionDirty = true;
    const state = this.querySelector("#strategy-source-composition-state");
    state.textContent = "指标约束已变化，尚未同步到源码";
    state.className = "dirty";
  }

  renderParameterFields(schema, values) {
    const block = this.querySelector("#strategy-parameters-block");
    const container = this.querySelector("#strategy-parameter-fields");
    const fields = Array.isArray(schema) ? schema : [];
    block.classList.toggle("hidden", !fields.length);
    container.replaceChildren(...fields.map((definition) => this.configField(definition, values?.[definition.key], "parameter")));
  }

  renderRiskFields(values) {
    const block = this.querySelector("#strategy-risk-block");
    const source = this.plainObject(values);
    const definitions = [
      { key: "stop_loss_pct", label: "止损 (%)", type: "number", min: 0, max: 99.9, step: 0.1 },
      { key: "take_profit_pct", label: "止盈 (%)", type: "number", min: 0, max: 99.9, step: 0.1 },
      { key: "position_size_pct", label: "单次仓位 (%)", type: "number", min: 0.01, max: 100, step: 0.01 },
      { key: "leverage", label: "杠杆倍数", type: "integer", min: 1, max: 20, step: 1 },
      { key: "max_holding_bars", label: "最大持有 (K线)", type: "integer", min: 0, max: 50000, step: 1 },
      { key: "fee_bps", label: "手续费 (bp)", type: "number", min: 0, max: 1000, step: 0.1 },
      { key: "slippage_bps", label: "滑点 (bp)", type: "number", min: 0, max: 1000, step: 0.1 },
    ];
    const known = new Set(definitions.map((item) => item.key));
    Object.keys(source).filter((key) => !known.has(key) && ["number", "boolean", "string"].includes(typeof source[key])).forEach((key) => {
      definitions.push({ key, label: this.humanizeKey(key), type: typeof source[key] === "boolean" ? "boolean" : (typeof source[key] === "number" ? "number" : "string") });
    });
    const available = definitions.filter((definition) => Object.prototype.hasOwnProperty.call(source, definition.key));
    block.classList.toggle("hidden", !available.length);
    this.querySelector("#strategy-risk-fields").replaceChildren(...available.map((definition) => this.configField(definition, source[definition.key], "risk")));
  }

  configField(definition = {}, currentValue, group) {
    const key = String(definition.key ?? "");
    const label = this.node("label");
    label.append(this.node("span", "strategy-field-label", definition.label || this.humanizeKey(key)));
    const type = String(definition.type || "number").toLowerCase();
    let input;
    if (Array.isArray(definition.options)) {
      input = document.createElement("select");
      definition.options.forEach((option) => {
        const value = typeof option === "object" ? option.value : option;
        input.append(this.option(value, typeof option === "object" ? option.label ?? value : value));
      });
    } else if (["boolean", "bool"].includes(type)) {
      input = document.createElement("select");
      input.append(this.option("true", "启用"), this.option("false", "关闭"));
    } else if (["number", "integer", "float"].includes(type)) {
      input = document.createElement("input");
      input.type = "number";
      if (definition.min != null) input.min = String(definition.min);
      if (definition.max != null) input.max = String(definition.max);
      input.step = String(definition.step ?? (type === "integer" ? 1 : "any"));
    } else {
      input = document.createElement("input");
      input.type = "text";
      input.maxLength = Number(definition.max_length ?? 120);
    }
    input.dataset.configKey = key;
    input.dataset.configGroup = group;
    input.dataset.configType = type;
    const value = currentValue !== undefined ? currentValue : definition.default;
    if (["boolean", "bool"].includes(type)) input.value = value === false || value === "false" || value === 0 ? "false" : "true";
    else if (value !== undefined && value !== null) input.value = String(value);
    if (type !== "integer" && input instanceof HTMLInputElement && input.type === "number" && input.validity.stepMismatch) {
      // 兼容旧版 AI 源码里“min=0.1、step=1、default=1”这类自相矛盾的参数定义。
      // 服务端本来只校验数值范围；这里不应让一个已保存的默认值被浏览器拒绝。
      input.dataset.declaredStep = input.step;
      input.step = "any";
    }
    if (definition.help) label.append(input, this.node("small", "strategy-field-help", definition.help));
    else label.append(input);
    return label;
  }

  localizeFieldValidation(field) {
    if (!field || typeof field.setCustomValidity !== "function") return;
    field.setCustomValidity("");
    const validity = field.validity;
    let message = "";
    if (validity.valueMissing) message = "请填写此字段。";
    else if (validity.badInput || validity.typeMismatch) message = "请输入有效的值。";
    else if (validity.rangeUnderflow) message = `请输入不小于 ${field.min} 的值。`;
    else if (validity.rangeOverflow) message = `请输入不大于 ${field.max} 的值。`;
    else if (validity.stepMismatch) {
      const nearest = this.nearestStepValues(field);
      message = nearest
        ? `请输入符合步长 ${field.step} 的值。最接近的有效值是 ${nearest[0]} 和 ${nearest[1]}。`
        : `请输入符合步长 ${field.step} 的值。`;
    } else if (validity.tooShort) message = `请至少输入 ${field.minLength} 个字符。`;
    else if (validity.tooLong) message = `最多只能输入 ${field.maxLength} 个字符。`;
    else if (validity.patternMismatch) message = "输入格式不正确，请重新检查。";
    else if (!validity.valid) message = "请输入有效的值。";
    field.setCustomValidity(message);
  }

  nearestStepValues(field) {
    const value = Number(field.value);
    const step = Number(field.step);
    const minimum = Number(field.min);
    if (!Number.isFinite(value) || !Number.isFinite(step) || step <= 0) return null;
    const base = Number.isFinite(minimum) ? minimum : 0;
    const ratio = (value - base) / step;
    const precision = Math.min(10, Math.max(this.decimalPlaces(base), this.decimalPlaces(step), this.decimalPlaces(value)));
    const format = (candidate) => Number(candidate.toFixed(precision)).toString();
    return [format(base + Math.floor(ratio) * step), format(base + Math.ceil(ratio) * step)];
  }

  decimalPlaces(value) {
    const text = String(value).toLowerCase();
    if (text.includes("e-")) return Number(text.split("e-")[1]) || 0;
    return (text.split(".")[1] || "").length;
  }

  humanizeKey(key) {
    const labels = {
      fee_bps: "手续费 (bp)",
      slippage_bps: "滑点 (bp)",
      confirmation_bars: "确认 K 线数",
      volume_ratio: "成交量倍数",
    };
    return labels[key] || String(key).replaceAll("_", " ");
  }

  collectConfig(group, base = {}) {
    const output = { ...this.plainObject(base) };
    this.querySelectorAll(`[data-config-group="${group}"]`).forEach((input) => {
      const key = input.dataset.configKey;
      const type = input.dataset.configType;
      if (["number", "integer", "float"].includes(type)) {
        const number = Number(input.value);
        if (Number.isFinite(number)) output[key] = type === "integer" ? Math.trunc(number) : number;
      } else if (["boolean", "bool"].includes(type)) output[key] = input.value === "true";
      else output[key] = input.value;
    });
    return output;
  }

  async save(event) {
    event.preventDefault();
    const form = this.querySelector("#strategy-form");
    if (!form.checkValidity()) {
      form.reportValidity();
      this.showFormError("请检查名称、分类和参数范围。所有必填项都需要有效值。");
      return;
    }
    const name = this.querySelector("#strategy-name").value.trim();
    const description = this.querySelector("#strategy-description").value.trim();
    const category = this.querySelector("#strategy-category").value.trim();
    const codeEdit = this.editorMode === "edit" && this.editScope === "code";
    const sourceEdit = (this.editorMode === "edit" && this.editScope === "source") || (this.editorMode === "create" && this.createMode === "source");
    let codeSpec = null;
    if (codeEdit || sourceEdit) {
      try { codeSpec = this.parseStrategyCode(); } catch (error) {
        this.showFormError(error?.message || "策略代码无法解析。");
        this.setCodeStatus(error?.message || "策略代码无法解析", "error");
        return;
      }
    }
    const indicatorCreate = this.editorMode === "create" && this.createMode === "indicators";
    const indicatorSelections = indicatorCreate ? this.collectIndicatorSelections() : [];
    const directions = indicatorCreate
      ? [this.querySelector("#strategy-direction-long").checked ? "long" : null, this.querySelector("#strategy-direction-short").checked ? "short" : null].filter(Boolean)
      : [];
    if (indicatorCreate && indicatorSelections.length < 2) {
      this.showFormError("请至少选择两个指标，才能形成指标组合策略。");
      return;
    }
    if (indicatorCreate && !directions.length) {
      this.showFormError("请至少选择一个允许交易方向。");
      return;
    }
    if (
      this.editorMode === "edit"
      && this.editScope === "parameters"
      && this.activeItem?.strategy_kind === "source_strategy"
      && this.sourceCompositionDirty
    ) {
      this.showFormError("指标结构已经变化。请先点击“让 AI 按当前指标重构源码”，应用并校验源码草稿后再保存新版本。");
      return;
    }
    const button = this.querySelector("#strategy-save");
    this.setButtonBusy(button, true, this.editorMode === "create" ? "正在创建…" : "正在保存…");
    this.showFormError("");
    try {
      let result;
      if (this.editorMode === "create") {
        if (sourceEdit) {
          result = await this.api("/source", {
            method: "POST",
            body: JSON.stringify({
              name,
              description,
              category,
              language: "python",
              source_code: codeSpec,
              risk_defaults: this.collectConfig("risk", this.plainObject(this.indicatorDefaults.risk_defaults)),
              ...(this.sourceComposition ? { composition: this.sourceComposition } : {}),
            }),
          });
        } else {
        const body = indicatorCreate
          ? {
              name,
              description,
              category,
              indicators: indicatorSelections,
              timeframe: this.querySelector("#strategy-timeframe").value,
              directions,
              confirmation_threshold: Number(this.querySelector("#strategy-confirmation-threshold").value),
              signal_valid_bars: Number(this.querySelector("#strategy-signal-valid-bars").value),
              risk_defaults: this.collectConfig("risk", this.plainObject(this.indicatorDefaults.risk_defaults)),
            }
          : {
              name,
              description,
              category,
              template_key: this.querySelector("#strategy-template").value,
              parameters: this.collectConfig("parameter"),
              risk_defaults: this.collectConfig("risk"),
            };
        if (!indicatorCreate && !body.template_key) throw new Error("请选择一个系统策略模板。");
        result = await this.api("", { method: "POST", body: JSON.stringify(body) });
        }
      } else if (codeEdit) {
        const body = {
          name,
          description,
          category,
          spec: codeSpec,
          version: this.activeItem.version,
        };
        result = await this.api(`/${encodeURIComponent(this.activeItem.public_id)}/code`, { method: "PUT", body: JSON.stringify(body) });
      } else if (sourceEdit) {
        const body = {
          name,
          description,
          category,
          language: "python",
          source_code: codeSpec,
          risk_defaults: this.collectConfig("risk", this.sourceRiskDefaults),
          version: this.activeItem.version,
        };
        result = await this.api(`/${encodeURIComponent(this.activeItem.public_id)}/source`, { method: "PUT", body: JSON.stringify(body) });
      } else {
        const parameterBase = this.activeItem.strategy_kind === "source_strategy"
          ? this.sourceParameterValues
          : this.activeItem.parameters;
        const body = {
          name,
          description,
          category,
          parameters: this.collectConfig("parameter", parameterBase),
          risk_defaults: this.collectConfig("risk", this.activeItem.risk_defaults),
          version: this.activeItem.version,
        };
        result = await this.api(`/${encodeURIComponent(this.activeItem.public_id)}`, { method: "PUT", body: JSON.stringify(body) });
      }
      const item = this.normalizeItem(result?.item ?? result);
      if (sourceEdit) this.setSourceWorkbenchDirty(false);
      this.upsertItem(item);
      this.renderFilters();
      this.renderStats();
      this.renderCards();
      this.closeEditor();
      this.showNotice(this.editorMode === "create" ? `策略“${item.name}”已创建。` : `策略“${item.name}”已保存为 v${item.version}。`, "success");
      this.notifyStrategiesChanged();
    } catch (error) {
      this.showFormError(this.friendlyMutationError(error));
    } finally {
      this.setButtonBusy(button, false);
    }
  }

  async validateItem(item, button) {
    this.setButtonBusy(button, true, "验证中…");
    this.showNotice("");
    try {
      const result = await this.api(`/${encodeURIComponent(item.public_id)}/validate`, { method: "POST", body: "{}" });
      if (["draft", "published"].includes(item.lifecycle_status)) {
        const promoted = await this.promoteLifecycle(item, "validated");
        item = promoted.item;
        this.renderCards();
      }
      const warnings = Array.isArray(result?.warnings) ? result.warnings.filter(Boolean) : [];
      const suffix = warnings.length ? `；${warnings.join("；")}` : "；数据要求和策略规格均有效";
      this.showNotice(`策略“${item.name}”验证通过${suffix}。`, "success");
    } catch (error) {
      this.showNotice(this.friendlyMutationError(error));
    } finally {
      this.setButtonBusy(button, false);
    }
  }

  async archiveItem(item, button) {
    if (!window.confirm(`确认归档策略“${item.name}”？归档后不会再用于新的部署。`)) return;
    this.setButtonBusy(button, true, "归档中…");
    this.showNotice("");
    try {
      await this.api(`/${encodeURIComponent(item.public_id)}`, { method: "DELETE" });
      this.items = this.items.filter((candidate) => candidate.public_id !== item.public_id);
      this.renderFilters();
      this.renderStats();
      this.renderCards();
      this.showNotice(`策略“${item.name}”已归档，历史版本和回测快照仍然保留。`, "success");
      this.notifyStrategiesChanged();
    } catch (error) {
      this.showNotice(this.friendlyMutationError(error));
    } finally {
      this.setButtonBusy(button, false);
    }
  }

  async openDetails(item) {
    const layer = this.querySelector("#strategy-detail-layer");
    const content = this.querySelector("#strategy-detail-content");
    this.querySelector("#strategy-detail-title").textContent = item.name;
    this.querySelector("#strategy-detail-subtitle").textContent = `正在读取 v${item.version} 的版本记录与验证结果…`;
    content.replaceChildren(this.node("div", "strategy-grid-state strategy-grid-loading", "正在读取策略详情…"));
    layer.classList.remove("hidden");
    layer.setAttribute("aria-hidden", "false");
    document.body.classList.add("strategy-dialog-open");
    try {
      const [detailPayload, revisionPayload, validation, readiness] = await Promise.all([
        this.api(`/${encodeURIComponent(item.public_id)}`),
        this.api(`/${encodeURIComponent(item.public_id)}/revisions`),
        this.api(`/${encodeURIComponent(item.public_id)}/validate`, { method: "POST", body: "{}" }),
        this.api(`/${encodeURIComponent(item.public_id)}/readiness`),
      ]);
      const detail = this.normalizeItem(detailPayload?.item ?? detailPayload);
      const revisions = Array.isArray(revisionPayload?.items) ? revisionPayload.items : [];
      this.querySelector("#strategy-detail-subtitle").textContent = `当前 v${detail.version} · ${this.lifecycleLabel(detail.lifecycle_status)} · ${detail.engine_key}`;
      content.replaceChildren(this.detailSummary(detail, validation, readiness), this.revisionLedger(revisions));
    } catch (error) {
      const state = this.node("div", "strategy-grid-state error");
      state.append(this.node("span", "strategy-state-icon", "!"), this.node("strong", "", "策略详情暂不可用"), this.node("small", "", error?.message || "读取失败，请稍后重试。"));
      content.replaceChildren(state);
    }
  }

  detailSummary(item, validation = {}, readiness = {}) {
    const section = this.node("section", "strategy-detail-summary");
    const heading = this.node("header", "strategy-detail-section-head");
    heading.append(this.node("div", "", "CURRENT SNAPSHOT"), this.node("strong", "", "当前策略快照"));
    const grid = this.node("div", "strategy-detail-metrics");
    const values = [
      ["版本", `v${item.version}`],
      ["生命周期", this.lifecycleLabel(item.lifecycle_status)],
      ["策略引擎", item.engine_key],
      ["验证状态", validation?.valid ? "通过" : "未通过"],
    ];
    values.forEach(([label, value]) => {
      const metric = this.node("article");
      metric.append(this.node("span", "", label), this.node("strong", "", value));
      grid.append(metric);
    });
    const description = this.node("p", "strategy-detail-description", item.description || "尚未填写策略说明。");
    const specs = this.node("div", "strategy-detail-specs");
    specs.append(
      this.node("span", "", `类别 ${item.category}`),
      this.node("span", "", `类型 ${item.strategy_kind}`),
      this.node("span", "", `规格哈希 ${item.spec_hash || "--"}`),
    );
    const warnings = Array.isArray(validation?.warnings) ? validation.warnings : [];
    if (warnings.length) specs.append(this.node("span", "warning", warnings.join("；")));
    const readinessBox = this.node("div", "strategy-detail-readiness");
    const promotionChecks = Array.isArray(readiness?.promotion_checks) ? readiness.promotion_checks : [];
    readinessBox.append(this.node(
      "p",
      "strategy-detail-description",
      readiness?.next_status
        ? `下一阶段：${this.lifecycleLabel(readiness.next_status)}。${readiness?.can_promote ? "当前证据已满足晋级条件。" : "仍有晋级条件未满足。"}`
        : "当前修订没有后续自动晋级阶段。",
    ));
    if (promotionChecks.length) {
      const checkList = this.node("div", "strategy-card-tags");
      promotionChecks.forEach((check) => checkList.append(
        this.node("span", check?.passed ? "" : "warning", `${check?.passed ? "✓" : "×"} ${check?.label || check?.code}`),
      ));
      readinessBox.append(checkList);
    }
    if (readiness?.can_promote && readiness?.next_status) {
      const promote = this.node("button", "strategy-save-button", `晋级到${this.lifecycleLabel(readiness.next_status)}`);
      promote.type = "button";
      promote.addEventListener("click", async () => {
        let approvalNote = null;
        if (["micro_live", "live"].includes(readiness.next_status)) {
          approvalNote = window.prompt("请输入审批说明（至少 10 个字符）：", "已复核当前修订证据与资金风险边界");
          if (!approvalNote) return;
        }
        this.setButtonBusy(promote, true, "晋级中…");
        try {
          const promoted = await this.promoteLifecycle(item, readiness.next_status, approvalNote);
          this.showNotice(`策略“${item.name}”已晋级到${this.lifecycleLabel(readiness.next_status)}。`, "success");
          await this.openDetails(promoted.item);
        } catch (error) {
          this.showNotice(this.friendlyMutationError(error));
        } finally {
          this.setButtonBusy(promote, false);
        }
      });
      readinessBox.append(promote);
    }
    section.append(heading, grid, description, specs, readinessBox);
    return section;
  }

  revisionLedger(revisions) {
    const section = this.node("section", "strategy-revision-ledger");
    const heading = this.node("header", "strategy-detail-section-head");
    heading.append(this.node("div", "", "REVISION LEDGER"), this.node("strong", "", `历史版本 · ${revisions.length}`));
    const list = this.node("div", "strategy-revision-list");
    if (!revisions.length) list.append(this.emptyState("暂无历史版本", "首次保存策略后会生成不可变的版本记录。"));
    revisions.forEach((revision) => {
      const row = this.node("article", "strategy-revision-row");
      const version = this.node("span", "strategy-revision-version", `v${revision.version ?? "--"}`);
      const copy = this.node("div");
      copy.append(this.node("strong", "", revision.change_summary || "策略配置更新"), this.node("small", "", `${this.lifecycleLabel(revision.lifecycle_status)} · ${revision.change_source || "manual"} · ${this.shortDateTime(revision.created_at)}`));
      const hash = this.node("code", "", String(revision.spec_hash || "--").slice(0, 14));
      row.append(version, copy, hash);
      list.append(row);
    });
    section.append(heading, list);
    return section;
  }

  closeDetails() {
    const layer = this.querySelector("#strategy-detail-layer");
    if (!layer) return;
    layer.classList.add("hidden");
    layer.setAttribute("aria-hidden", "true");
    document.body.classList.remove("strategy-dialog-open");
  }

  resetAiConversation() {
    this.aiSessionGeneration = Number(this.aiSessionGeneration || 0) + 1;
    this.aiConversation = [];
    this.aiPromptQueue = [];
    this.aiBusy = false;
    this.aiTurnSequence = 0;
    this.aiWorkingSource = this.activeItem?.source_code || this.codeBuffers.source || "";
    this.aiWorkingSpec = null;
    this.aiWorkingProposed = null;
    this.aiWorkingProfile = this.activeItem ? {
      name: this.activeItem.name,
      category: this.activeItem.category,
      description: this.activeItem.description,
      risk_defaults: { ...this.sourceRiskDefaults },
    } : null;
    this.aiWorkingComposition = this.sourceComposition
      ? JSON.parse(JSON.stringify(this.sourceComposition))
      : null;
    this.aiProcessSteps = [];
    this.renderAiConversation();
    this.renderAiProcess();
    this.updateAiQueueStatus();
  }

  renderAiConversation() {
    const container = this.querySelector("#strategy-ai-messages");
    if (!container) return;
    this.querySelector("#strategy-ai-turn-count").textContent = `${this.aiConversation.filter((item) => item.role === "user").length} 轮`;
    if (!this.aiConversation.length) {
      const empty = this.node("div", "strategy-ai-message-empty");
      empty.append(
        this.node("strong", "", "从一个目标开始，然后继续追问"),
        this.node("span", "", "例如先调整指标，再说“把刚才的版本改得更保守”，AI 会接着当前草稿继续。"),
      );
      container.replaceChildren(empty);
      return;
    }
    container.replaceChildren(...this.aiConversation.map((message) => {
      const article = this.node("article", `strategy-ai-message ${message.role} ${message.status || "complete"}`);
      const header = this.node("header");
      header.append(
        this.node("strong", "", message.role === "user" ? "你" : "AI 助手"),
        this.node("span", "", message.status === "queued" ? "排队中" : (message.status === "processing" ? "处理中" : (message.status === "error" ? "失败" : `第 ${message.turn || "--"} 轮`))),
      );
      article.append(header, this.node("p", "", message.content));
      if (message.meta) article.append(this.node("small", "", message.meta));
      return article;
    }));
    container.scrollTop = container.scrollHeight;
  }

  renderAiProcess() {
    const panel = this.querySelector("#strategy-ai-process");
    const container = this.querySelector("#strategy-ai-process-steps");
    if (!panel || !container) return;
    panel.classList.toggle("hidden", !this.aiProcessSteps.length);
    container.replaceChildren(...this.aiProcessSteps.map((step, index) => {
      const row = this.node("article", `strategy-ai-process-step ${step.status || "pending"}`);
      row.append(
        this.node("span", "", String(index + 1).padStart(2, "0")),
        this.node("div", "", ""),
      );
      row.lastChild.append(this.node("strong", "", step.label), this.node("small", "", step.detail || "等待处理"));
      return row;
    }));
  }

  setAiProcess(stage, detail = "") {
    const definitions = [
      ["understand", "理解本轮要求"],
      ["context", "读取会话与当前草稿"],
      ["compose", "生成策略身份与指标方案"],
      ["generate", "生成候选策略"],
      ["validate", "服务端安全校验"],
    ];
    const activeIndex = Math.max(0, definitions.findIndex(([key]) => key === stage));
    this.aiProcessSteps = definitions.map(([key, label], index) => ({
      key,
      label,
      status: index < activeIndex ? "complete" : (index === activeIndex ? "active" : "pending"),
      detail: index === activeIndex ? detail : (index < activeIndex ? "已完成" : "等待处理"),
    }));
    this.renderAiProcess();
  }

  completeAiProcess(preview) {
    const count = Array.isArray(preview?.changes) ? preview.changes.length : 0;
    const parameterCount = Array.isArray(preview?.parameter_schema) ? preview.parameter_schema.length : 0;
    this.aiProcessSteps = [
      { label: "理解本轮要求", status: "complete", detail: "已结合连续会话识别本轮修改目标" },
      { label: "读取会话与当前草稿", status: "complete", detail: "已基于上一轮未保存草稿继续调整" },
      { label: "生成策略身份与指标方案", status: "complete", detail: preview?.profile ? "名称、分类、说明、指标与运行约束已生成" : "沿用当前策略结构" },
      { label: "生成候选策略", status: "complete", detail: `生成 ${count || 1} 项候选变化` },
      { label: "服务端安全校验", status: "complete", detail: preview?.mode === "source" ? `Python 沙箱校验通过 · ${parameterCount} 个动态参数` : "字段、范围与版本约束校验通过" },
    ];
    this.renderAiProcess();
  }

  updateAiQueueStatus() {
    const status = this.querySelector("#strategy-ai-status");
    const button = this.querySelector("#strategy-ai-preview-button strong");
    const queued = this.aiPromptQueue.length;
    status.classList.toggle("working", this.aiBusy);
    status.lastChild.textContent = this.aiBusy ? `处理中${queued ? ` · 待处理 ${queued}` : ""}` : "连续会话就绪";
    button.textContent = this.aiBusy ? (queued ? `继续发送 · 已排队 ${queued}` : "继续发送下一条") : "发送给 AI";
  }

  conversationPrompt(prompt) {
    const history = this.aiConversation
      .filter((item) => item.status === "complete")
      .slice(-6)
      .map((item) => `${item.role === "user" ? "用户" : "助手"}：${String(item.content).slice(0, 220)}`)
      .join("\n");
    const workingChanges = Array.isArray(this.preview?.changes)
      ? this.preview.changes.slice(0, 12).map((item) => `${item.path || item.field}=${this.changeValue(item.after)}`).join("；")
      : "";
    const profile = this.aiWorkingProfile || (this.activeItem ? {
      name: this.activeItem.name,
      category: this.activeItem.category,
      description: this.activeItem.description,
      risk_defaults: this.sourceRiskDefaults,
    } : null);
    const composition = this.aiWorkingComposition || this.sourceComposition;
    const strategyContext = profile ? `当前策略资料：${JSON.stringify(profile)}` : "";
    const compositionContext = composition ? `当前指标与运行约束：${JSON.stringify(composition)}` : "";
    const combined = [
      history ? `连续会话摘要：\n${history}` : "",
      workingChanges ? `当前未保存草稿变化：${workingChanges}` : "",
      strategyContext,
      compositionContext,
      `本轮要求：${prompt}`,
      "请根据本轮要求同步维护策略名称、分类、说明、指标组合、运行约束、风险参数和源码；未要求推翻的部分保持与前序草稿一致。",
    ].filter(Boolean).join("\n");
    return combined.slice(-1950);
  }

  requestAiPreview({ prompt = "", forceMode = "", autoCompose = true } = {}) {
    if (this.editorMode !== "create" && !this.activeItem?.public_id) return;
    const input = this.querySelector("#strategy-ai-prompt");
    const content = String(prompt || input.value || "").trim();
    if (content.length < 4) {
      this.showAiError("请先描述希望修改的参数、指标或规则，至少输入 4 个字符。");
      return;
    }
    const turn = ++this.aiTurnSequence;
    const message = { id: `turn-${turn}`, turn, role: "user", content, status: this.aiBusy ? "queued" : "processing" };
    this.aiConversation.push(message);
    this.aiPromptQueue.push({ turn, prompt: content, forceMode, autoCompose, message });
    input.value = "";
    this.showAiError("");
    this.renderAiConversation();
    this.updateAiQueueStatus();
    void this.processAiPromptQueue();
  }

  async processAiPromptQueue() {
    if (this.aiBusy) return;
    this.aiBusy = true;
    const generation = this.aiSessionGeneration;
    this.updateAiQueueStatus();
    while (this.aiPromptQueue.length && generation === this.aiSessionGeneration) {
      const turn = this.aiPromptQueue.shift();
      turn.message.status = "processing";
      this.renderAiConversation();
      this.updateAiQueueStatus();
      this.setAiProcess("understand", `正在解析第 ${turn.turn} 轮修改目标`);
      try {
        const preview = await this.executeAiTurn(turn);
        if (generation !== this.aiSessionGeneration) return;
        turn.message.status = "complete";
        this.aiConversation.push({
          id: `assistant-${turn.turn}`,
          turn: turn.turn,
          role: "assistant",
          content: preview.summary,
          status: "complete",
          meta: `${this.providerLabel(preview.provider)} · ${preview.changes.length || 0} 项变化 · 待你确认应用`,
        });
        this.completeAiProcess(preview);
      } catch (error) {
        if (generation !== this.aiSessionGeneration) return;
        turn.message.status = "error";
        const message = String(error?.message || "AI 修改预览生成失败，请稍后重试。");
        const friendly = /timeout|timed out|超时|Gateway Timeout/i.test(message)
          ? "本轮生成超时，没有覆盖上一轮草稿。你可以继续发送更小的调整要求。"
          : this.friendlyMutationError(error);
        this.aiConversation.push({ id: `assistant-${turn.turn}`, turn: turn.turn, role: "assistant", content: friendly, status: "error" });
        this.aiProcessSteps = this.aiProcessSteps.map((step) => step.status === "active" ? { ...step, status: "error", detail: friendly } : step);
        this.renderAiProcess();
        this.showAiError(friendly);
      }
      this.renderAiConversation();
    }
    this.aiBusy = false;
    this.updateAiQueueStatus();
  }

  async executeAiTurn(turn) {
    const prompt = this.conversationPrompt(turn.prompt);
    this.setAiProcess("context", "正在读取最近会话、当前源码与未保存草稿");
    try {
      const codeEdit = this.editorMode === "edit" && this.editScope === "code";
      const indicatorSourceCreate = this.editorMode === "create" && this.createMode === "indicators";
      const sourceStrategy = this.editorMode === "edit" && this.activeItem?.strategy_kind === "source_strategy";
      const sourceEdit = indicatorSourceCreate
        || (this.editorMode === "edit" && this.editScope === "source")
        || (this.editorMode === "create" && this.createMode === "source")
        || sourceStrategy;
      let composition = null;
      let profile = null;
      let compositionPreview = null;
      const shouldAutoCompose = sourceEdit && !codeEdit && turn.autoCompose !== false;
      if (shouldAutoCompose) {
        this.setAiProcess("compose", "AI 正在拟定策略名称、分类、说明、指标组合与运行约束");
        compositionPreview = await this.api("/compose/ai-preview", {
          method: "POST",
          body: JSON.stringify({ prompt }),
        });
        const normalizedDraft = this.normalizeSourceAiDraft(compositionPreview?.draft);
        composition = normalizedDraft.composition;
        profile = normalizedDraft.profile;
      } else if (indicatorSourceCreate) {
        const indicators = this.collectIndicatorSelections();
        const directions = [
          this.querySelector("#strategy-direction-long").checked ? "long" : null,
          this.querySelector("#strategy-direction-short").checked ? "short" : null,
        ].filter(Boolean);
        if (indicators.length < 2) throw new Error("请至少选择两个指标，再生成 Python 源码。");
        if (!directions.length) throw new Error("请至少选择一个允许交易方向。");
        composition = {
          indicators,
          timeframe: this.querySelector("#strategy-timeframe").value,
          directions,
          confirmation_threshold: Number(this.querySelector("#strategy-confirmation-threshold").value),
          signal_valid_bars: Number(this.querySelector("#strategy-signal-valid-bars").value),
        };
      } else if (sourceStrategy && sourceEdit) {
        composition = this.collectSourceComposition();
      }
      const path = sourceEdit
        ? (this.editorMode === "create" ? "/runtime/python/ai-preview" : `/${encodeURIComponent(this.activeItem.public_id)}/source/ai-preview`)
        : (this.editorMode === "create"
          ? "/compose/ai-preview"
          : `/${encodeURIComponent(this.activeItem.public_id)}${codeEdit ? "/code/ai-preview" : "/ai-preview"}`);
      const requestBody = sourceEdit
        ? {
            prompt,
            language: "python",
            source_code: this.aiWorkingSource || (indicatorSourceCreate
              ? String(this.sourceRuntime.conversion_starter_source || this.sourceRuntime.starter_source || "")
              : (this.editScope === "source"
                ? this.parseStrategyCode()
                : (this.codeBuffers.source || this.activeItem?.source_code || ""))),
            ...(composition ? { composition } : {}),
          }
        : (codeEdit ? { prompt, spec: this.aiWorkingSpec || this.parseStrategyCode() } : { prompt });
      this.setAiProcess("generate", "模型正在生成完整候选方案；你仍可继续发送下一条要求");
      const result = await this.api(path, {
        method: "POST",
        body: JSON.stringify(requestBody),
      });
      const profileChanges = profile ? this.sourceAiDraftChanges(profile, composition) : [];
      const sourceChanges = Array.isArray(result?.changes) ? result.changes : [];
      const compositionSummary = profile ? String(compositionPreview?.summary || "AI 已生成策略身份与指标方案。") : "";
      const sourceSummary = String(result?.summary ?? "模型已生成受约束的指标组合建议。");
      this.preview = {
        base_version: Number(result?.base_version ?? this.activeItem?.version ?? 1),
        provider: String(result?.provider ?? "AI model"),
        summary: profile ? `${compositionSummary}；${sourceSummary}` : sourceSummary,
        changes: [...profileChanges, ...sourceChanges],
        proposed: this.plainObject(result?.proposed),
        proposed_spec: this.plainObject(result?.proposed_spec),
        strategy_code: String(result?.strategy_code ?? ""),
        source_code: String(result?.source_code ?? ""),
        draft: this.plainObject(result?.draft),
        profile,
        risk_defaults: profile ? this.plainObject(profile.risk_defaults) : null,
        composition: result?.composition ? this.plainObject(result.composition) : composition,
        parameter_schema: Array.isArray(result?.parameter_schema) ? result.parameter_schema : [],
        parameters: this.plainObject(result?.parameters),
        generated_from_composition: indicatorSourceCreate,
        mode: sourceEdit ? "source" : (codeEdit ? "code" : "parameters"),
      };
      if (this.preview.mode === "source") {
        this.aiWorkingSource = this.preview.source_code;
        if (profile) this.aiWorkingProfile = JSON.parse(JSON.stringify(profile));
        if (composition) this.aiWorkingComposition = JSON.parse(JSON.stringify(composition));
      }
      if (this.preview.mode === "code") this.aiWorkingSpec = this.preview.proposed_spec;
      if (this.preview.mode === "parameters") this.aiWorkingProposed = this.preview.proposed;
      this.setAiProcess("validate", "候选方案已返回，正在整理校验结论与变化摘要");
      this.renderPreview();
      return this.preview;
    } catch (error) {
      throw error;
    }
  }

  renderPreview() {
    if (!this.preview) return;
    const panel = this.querySelector("#strategy-ai-preview");
    panel.classList.remove("hidden");
    this.querySelector("#strategy-ai-provider").textContent = this.providerLabel(this.preview.provider);
    this.querySelector("#strategy-ai-base-version").textContent = this.editorMode === "create" ? "新策略草案" : `基于 v${this.preview.base_version}`;
    this.querySelector("#strategy-ai-summary").textContent = this.preview.summary;
    this.querySelector("#strategy-ai-apply strong").textContent = this.preview.mode === "source"
      ? "应用完整策略草稿"
      : (this.preview.mode === "code" ? "应用到代码编辑器" : "确认应用");
    const changes = this.querySelector("#strategy-ai-changes");
    if (!this.preview.changes.length) {
      changes.replaceChildren(this.node("div", "strategy-change-empty", "模型未发现需要修改的有效字段"));
      this.querySelector("#strategy-ai-apply").disabled = true;
      return;
    }
    changes.replaceChildren(...this.preview.changes.map((change) => {
      const row = this.node("article", "strategy-change-row");
      const path = this.node("strong", "", this.changePathLabel(change.path ?? change.field));
      const before = this.node("span", "strategy-change-before", this.changeValue(change.before));
      const arrow = this.node("i", "", "→");
      arrow.setAttribute("aria-hidden", "true");
      const after = this.node("span", "strategy-change-after", this.changeValue(change.after));
      row.append(path, before, arrow, after);
      return row;
    }));
    const validationRejected = this.preview.mode === "parameters"
      && this.preview.optimization_validation?.approved === false;
    if (validationRejected) {
      changes.append(this.node("div", "strategy-change-empty error", "候选未通过隔离验证，已禁止应用。请保留当前版本并重新调整策略逻辑。"));
    }
    this.querySelector("#strategy-ai-apply").disabled = validationRejected;
  }

  clearPreview() {
    this.preview = null;
    this.sourceOptimizationValidation = null;
    this.querySelector("#strategy-ai-preview").classList.add("hidden");
    this.querySelector("#strategy-ai-changes").replaceChildren();
    this.querySelector("#strategy-ai-apply").disabled = false;
    this.querySelector("#strategy-ai-apply strong").textContent = "确认应用";
  }

  async applyAiPreview() {
    if (!this.preview) return;
    if (this.preview.mode === "parameters" && this.preview.optimization_validation?.approved === false) {
      this.showAiError("候选参数未通过隔离验证，不能应用到策略版本。", "error");
      return;
    }
    if (this.editorMode === "create" && this.preview.mode !== "source") {
      this.applyCompositionDraft(this.preview.draft);
      return;
    }
    if (["code", "source"].includes(this.preview.mode)) {
      const pendingPreview = this.preview;
      const generatedFromComposition = this.editorMode === "create" && this.preview.generated_from_composition;
      const composition = this.preview.composition;
      const code = this.preview.mode === "source"
        ? this.preview.source_code
        : (this.preview.strategy_code || this.strategyCode(this.preview.proposed_spec));
      if (generatedFromComposition) {
        this.sourceWorkbenchTab = "ai";
        this.switchCreateMode("source", { resetValues: false, keepComposition: true });
        this.sourceComposition = composition;
      }
      this.codeBuffers[this.preview.mode] = code;
      if (pendingPreview.mode === "source" && this.editorMode === "edit" && this.editScope !== "source") {
        this.sourceComposition = composition || this.sourceComposition;
        this.sourceCompositionDirty = false;
        this.switchEditScope("source", { resetCode: false });
      }
      this.querySelector("#strategy-code-editor").value = code;
      this.renderCodeLines();
      if (pendingPreview.mode === "source") {
        this.sourceComposition = composition || this.sourceComposition;
        this.sourceCompositionDirty = false;
        this.sourceParameterSchema = pendingPreview.parameter_schema.map((definition) => ({ ...definition }));
        this.sourceParameterValues = { ...pendingPreview.parameters };
        if (pendingPreview.profile) {
          this.querySelector("#strategy-name").value = pendingPreview.profile.name;
          this.querySelector("#strategy-category").value = pendingPreview.profile.category;
          this.querySelector("#strategy-description").value = pendingPreview.profile.description;
          this.querySelector("#strategy-editor-title").textContent = pendingPreview.profile.name;
          this.aiWorkingProfile = JSON.parse(JSON.stringify(pendingPreview.profile));
        }
        if (pendingPreview.risk_defaults) {
          this.sourceRiskDefaults = { ...this.plainObject(pendingPreview.risk_defaults) };
          this.renderRiskFields(this.sourceRiskDefaults);
        }
        if (this.sourceComposition) {
          this.aiWorkingComposition = JSON.parse(JSON.stringify(this.sourceComposition));
          this.populateSourceCompositionEditor(this.sourceComposition);
        }
        this.aiWorkingSource = code;
        this.setSourceWorkbenchDirty(true);
      }
      this.querySelector("#strategy-ai-prompt").value = "";
      this.clearPreview();
      this.setCodeStatus(generatedFromComposition ? "AI 源码已通过服务端静态审查，请复核后保存" : "AI 修改已写入编辑器，请校验后保存新版本", "success");
      this.showAiError(generatedFromComposition
        ? "AI 已生成并应用完整策略草稿。名称、说明、指标、参数、风险与源码都尚未保存，也不会执行交易。"
        : "完整策略草稿已应用到编辑器，尚未保存，也不会执行交易。", "success");
      this.aiConversation.push({
        id: `applied-${Date.now()}`,
        turn: this.aiTurnSequence,
        role: "assistant",
        content: "完整策略草稿已应用：策略身份、指标约束、参数、风险与源码已同步。请完成源码校验和回测后再保存新版本。",
        status: "complete",
        meta: "仅更新未保存草稿 · 未执行交易",
      });
      this.renderAiConversation();
      return;
    }
    if (!this.activeItem?.public_id) return;
    const button = this.querySelector("#strategy-ai-apply");
    this.setButtonBusy(button, true, "正在应用…");
    this.showAiError("");
    try {
      const result = await this.api(`/${encodeURIComponent(this.activeItem.public_id)}/ai-apply`, {
        method: "POST",
        body: JSON.stringify({ base_version: this.preview.base_version, proposed: this.preview.proposed }),
      });
      const item = this.normalizeItem(result?.item ?? result);
      this.upsertItem(item);
      this.activeItem = item;
      if (item.strategy_kind === "source_strategy") {
        const priorScope = this.sourceBacktestScope;
        this.sourceParameterSchema = item.parameter_schema.map((definition) => ({ ...definition }));
        this.sourceParameterValues = { ...item.parameters };
        this.sourceRiskDefaults = { ...this.plainObject(item.risk_defaults) };
        this.applySourceRiskDefaults();
        this.resetSourceBacktestResult();
        this.setSourceBacktestScope(priorScope);
        this.setSourceWorkbenchDirty(false);
      }
      this.querySelector("#strategy-editor-title").textContent = item.name;
      this.querySelector("#strategy-editor-version").textContent = `v${item.version}`;
      this.querySelector("#strategy-name").value = item.name;
      this.querySelector("#strategy-category").value = item.category;
      this.querySelector("#strategy-description").value = item.description;
      this.renderParameterFields(item.parameter_schema, item.parameters);
      this.renderRiskFields(item.risk_defaults);
      this.querySelector("#strategy-ai-prompt").value = "";
      this.clearPreview();
      this.renderFilters();
      this.renderStats();
      this.renderCards();
      this.showAiError(`AI 修改已应用并保存为 v${item.version}；请继续进行数据回测验证。`, "success");
      this.notifyStrategiesChanged();
    } catch (error) {
      this.showAiError(this.friendlyMutationError(error));
    } finally {
      this.setButtonBusy(button, false);
    }
  }

  applyCompositionDraft(draft) {
    if (!draft || !Array.isArray(draft.indicators)) {
      this.showAiError("AI 返回的指标方案不完整，请重新生成。");
      return;
    }
    this.querySelector("#strategy-name").value = String(draft.name || "AI 指标组合策略");
    this.querySelector("#strategy-description").value = String(draft.description || "");
    this.querySelector("#strategy-category").value = String(draft.category || "指标组合");
    this.querySelector("#strategy-timeframe").value = String(draft.timeframe || "1h");
    this.querySelector("#strategy-confirmation-threshold").value = String(draft.confirmation_threshold ?? 60);
    this.querySelector("#strategy-signal-valid-bars").value = String(draft.signal_valid_bars ?? 2);
    const directions = Array.isArray(draft.directions) ? draft.directions : ["long", "short"];
    this.querySelector("#strategy-direction-long").checked = directions.includes("long");
    this.querySelector("#strategy-direction-short").checked = directions.includes("short");
    this.setSelectedIndicators(draft.indicators);
    this.renderRiskFields(this.plainObject(draft.risk_defaults));
    this.clearPreview();
    this.showAiError("AI 指标方案已填入表单；请检查参数后点击“创建策略”。", "success");
  }

  providerLabel(provider) {
    const value = String(provider || "AI model");
    if (value.toLowerCase().includes("local")) return "本地语义引擎";
    if (value.toLowerCase().includes("openai")) return "OpenAI · 结构化输出";
    return value.slice(0, 48);
  }

  changePathLabel(path) {
    const raw = String(path || "配置");
    const parts = raw.split(".");
    const key = parts[parts.length - 1];
    const labels = {
      name: "策略名称",
      description: "策略说明",
      category: "策略分类",
      parameters: "策略参数",
      risk_defaults: "风险默认值",
      indicators: "指标组合",
      timeframe: "运行周期",
      directions: "允许方向",
      confirmation_threshold: "确认阈值",
      signal_valid_bars: "信号有效 K 线",
      stop_loss_pct: "止损 (%)",
      take_profit_pct: "止盈 (%)",
      position_size_pct: "单次仓位 (%)",
      max_holding_bars: "最大持有 (K线)",
    };
    return labels[key] || this.humanizeKey(key || raw);
  }

  changeValue(value) {
    if (value === null || value === undefined) return "未设置";
    if (typeof value === "boolean") return value ? "启用" : "关闭";
    if (typeof value === "object") {
      try { return JSON.stringify(value).slice(0, 100); } catch (_) { return "结构化配置"; }
    }
    return String(value).slice(0, 100);
  }

  upsertItem(item) {
    const index = this.items.findIndex((current) => current.public_id === item.public_id);
    if (index >= 0) this.items.splice(index, 1, item);
    else this.items.unshift(item);
  }

  notifyStrategiesChanged() {
    window.dispatchEvent(new CustomEvent("quantdesk:strategies-changed", { detail: { strategyId: this.activeItem?.public_id || "" } }));
  }

  friendlyMutationError(error) {
    const message = this.localizedErrorMessage(error, "保存失败，请稍后重试。");
    if (/version|版本|conflict|冲突/i.test(message)) return "策略已在其他页面更新。请关闭编辑器并刷新列表后再修改。";
    return message;
  }

  localizedErrorMessage(error, fallback = "操作失败，请稍后重试。") {
    const message = String(error?.message || "").trim();
    if (!message) return fallback;
    const replacements = [
      [/source strategy trigger timeframe must be\s+([\w]+)/i, (_, timeframe) => `源码策略触发周期必须是 ${timeframe}。`],
      [/full strategy trigger timeframe must be\s+([\w]+)/i, (_, timeframe) => `完整策略触发周期必须是 ${timeframe}。`],
      [/invalid source strategy:\s*(.*)/i, (_, detail) => `源码策略无效：${detail || "请检查源码结构"}`],
      [/source strategy evaluation failed:\s*(.*)/i, (_, detail) => `源码策略执行失败：${detail || "请检查策略输出"}`],
      [/invalid backtest timeframe/i, () => "回测周期格式无效。"],
      [/unsupported backtest timeframe/i, () => "暂不支持该回测周期。"],
      [/strategy is archived/i, () => "策略已归档，不能继续修改。"],
      [/strategy not found/i, () => "未找到该策略。"],
      [/source strategy is incomplete/i, () => "源码策略不完整，请先补全并校验。"],
      [/strategy revision is unavailable/i, () => "策略版本快照不可用。"],
      [/active strategy limit reached/i, () => "可用策略数量已达到上限。"],
      [/failed to fetch|networkerror|network request failed/i, () => "网络请求失败，请检查连接后重试。"],
    ];
    for (const [pattern, replacement] of replacements) {
      if (pattern.test(message)) return message.replace(pattern, replacement);
    }
    return /[\u3400-\u9fff]/.test(message) ? message : fallback;
  }

  showNotice(message, tone = "") {
    const notice = this.querySelector("#strategy-notice");
    notice.textContent = message;
    notice.className = `strategy-notice${message ? "" : " hidden"}${tone ? ` ${tone}` : ""}`;
  }

  showFormError(message, tone = "") {
    const target = this.querySelector("#strategy-form-error");
    target.textContent = message;
    target.className = `strategy-form-error${message ? "" : " hidden"}${tone ? ` ${tone}` : ""}`;
  }

  showAiError(message, tone = "") {
    const target = this.querySelector("#strategy-ai-error");
    target.textContent = message;
    target.className = `strategy-form-error${message ? "" : " hidden"}${tone ? ` ${tone}` : ""}`;
  }

  setButtonBusy(button, busy, busyText = "处理中…") {
    if (!button) return;
    const label = button.querySelector("strong");
    if (busy) {
      button.dataset.idleText = label?.textContent || button.textContent;
      if (label) label.textContent = busyText;
      else button.textContent = busyText;
    } else if (button.dataset.idleText) {
      if (label) label.textContent = button.dataset.idleText;
      else button.textContent = button.dataset.idleText;
      delete button.dataset.idleText;
    }
    button.disabled = Boolean(busy);
    button.classList.toggle("loading", Boolean(busy));
  }
}

if (!customElements.get("strategy-center")) customElements.define("strategy-center", StrategyCenter);
