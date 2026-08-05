class StrategyCenter extends HTMLElement {
  constructor() {
    super();
    this.started = false;
    this.loading = false;
    this.sessionGeneration = 0;
    this.loadVersion = 0;
    this.items = [];
    this.templates = [];
    this.indicators = [];
    this.indicatorDefaults = {};
    this.selectedIndicators = new Map();
    this.deployments = [];
    this.signals = [];
    this.section = "strategies";
    this.query = "";
    this.statusFilter = "all";
    this.categoryFilter = "all";
    this.activeItem = null;
    this.editorMode = "edit";
    this.preview = null;
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
          <button type="button" data-section="legacy" aria-pressed="false"><span>旧版信号</span><strong id="strategy-tab-legacy">0</strong></button>
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
            <button id="strategy-editor-close" class="strategy-close-button" type="button" aria-label="关闭策略编辑器">×</button>
          </header>

          <div class="strategy-editor-body">
            <form id="strategy-form" class="strategy-form" novalidate>
              <div id="strategy-version-strip" class="strategy-version-strip"><span>当前版本</span><strong id="strategy-editor-version">--</strong><small>乐观锁保护</small></div>

              <div id="strategy-composer-block" class="strategy-form-block hidden">
                <div class="strategy-section-heading"><div><span>01</span><strong>选择指标</strong></div><small>至少选择 2 个，参数会动态出现</small></div>
                <div id="strategy-indicator-picker" class="strategy-indicator-picker"></div>
                <div class="strategy-compose-settings">
                  <label>运行周期<select id="strategy-timeframe"><option value="15m">15 分钟</option><option value="1h">1 小时</option><option value="4h">4 小时</option></select></label>
                  <label>确认阈值 (%)<input id="strategy-confirmation-threshold" type="number" min="1" max="100" step="1" value="60"></label>
                  <label>信号有效 K 线<input id="strategy-signal-valid-bars" type="number" min="1" max="10" step="1" value="2"></label>
                  <fieldset class="strategy-direction-picker"><legend>允许方向</legend><label><input id="strategy-direction-long" type="checkbox" checked>做多</label><label><input id="strategy-direction-short" type="checkbox" checked>做空</label></fieldset>
                </div>
                <div id="strategy-selected-indicators" class="strategy-selected-indicators"></div>
              </div>

              <div class="strategy-form-block">
                <div class="strategy-section-heading"><div><span id="strategy-basic-index">01</span><strong>基本信息</strong></div><small>仅当前用户可见</small></div>
                <div class="strategy-field-grid two">
                  <label>策略名称<input id="strategy-name" type="text" minlength="2" maxlength="64" autocomplete="off" required></label>
                  <label>策略分类<input id="strategy-category" type="text" minlength="2" maxlength="32" autocomplete="off" required></label>
                </div>
                <label>策略说明<textarea id="strategy-description" rows="3" maxlength="500" placeholder="说明入场逻辑、适用行情与风险边界"></textarea></label>
              </div>

              <div id="strategy-parameters-block" class="strategy-form-block">
                <div class="strategy-section-heading"><div><span id="strategy-parameters-index">02</span><strong>策略参数</strong></div><small>字段范围由策略模型约束</small></div>
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
              <header>
                <div><span>AI STRATEGY COMPOSER</span><h3 id="strategy-ai-title">用自然语言修改策略</h3></div>
                <span class="strategy-ai-status"><i></i>受约束配置</span>
              </header>
              <p id="strategy-ai-description">描述你想调整的逻辑或风险参数。模型只能提出结构化配置修改，不会生成或执行任意代码。</p>
              <label>自然语言要求<textarea id="strategy-ai-prompt" rows="5" maxlength="1200" placeholder="例如：做一个 1 小时趋势策略，用 EMA、MACD 和成交量确认，减少噪声并把止损控制在 3%。"></textarea></label>
              <div class="strategy-ai-examples" aria-label="AI 编辑示例">
                <button type="button" data-ai-example="创建 1 小时趋势策略，使用 EMA、MACD、ADX 和成交量确认，参数偏稳健。">趋势组合</button>
                <button type="button" data-ai-example="创建 15 分钟反转策略，使用 RSI、布林带和 ATR 过滤，减少噪声。">反转组合</button>
                <button type="button" data-ai-example="创建 4 小时突破策略，使用 Donchian、ADX 和成交量确认，只做多。">突破组合</button>
              </div>
              <button id="strategy-ai-preview-button" class="strategy-ai-preview-button" type="button"><span aria-hidden="true">✦</span><strong>AI 生成指标方案</strong></button>
              <div class="strategy-safety-note"><span aria-hidden="true">!</span><p><strong>仅生成预览，不执行交易</strong>。采用方案后只会回填表单，点击“创建策略”才保存，之后仍需通过回测与模拟盘验证。</p></div>

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
            </aside>
          </div>
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
    this.querySelector("#strategy-form").addEventListener("submit", (event) => this.save(event));
    this.querySelector("#strategy-ai-preview-button").addEventListener("click", () => this.requestAiPreview());
    this.querySelector("#strategy-ai-discard").addEventListener("click", () => this.clearPreview());
    this.querySelector("#strategy-ai-apply").addEventListener("click", () => this.applyAiPreview());
    this.querySelectorAll("[data-ai-example]").forEach((button) => button.addEventListener("click", () => {
      this.querySelector("#strategy-ai-prompt").value = button.dataset.aiExample || "";
      this.querySelector("#strategy-ai-prompt").focus();
    }));
    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape" && !this.querySelector("#strategy-dialog-layer").classList.contains("hidden")) this.closeEditor();
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
    this.selectedIndicators = new Map();
    this.deployments = [];
    this.signals = [];
    this.section = "strategies";
    this.query = "";
    this.statusFilter = "all";
    this.categoryFilter = "all";
    this.activeItem = null;
    this.preview = null;
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
    this.closeEditor();
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
      this.deployments = Array.isArray(deploymentPayload?.items) ? deploymentPayload.items : [];
      this.signals = Array.isArray(signalPayload?.items) ? signalPayload.items : [];
      this.renderFilters();
      this.renderStats();
      this.renderSection();
    } catch (error) {
      if (generation !== this.sessionGeneration || requestVersion !== this.loadVersion) return;
      this.renderError(error?.message || "策略列表加载失败");
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
      lifecycle_status: String(item.lifecycle_status ?? "published"),
      spec: this.plainObject(item.spec),
      is_default: Boolean(item.is_default),
      parameter_schema: schema,
      parameters: this.plainObject(item.parameters),
      risk_defaults: this.plainObject(item.risk_defaults),
    };
  }

  normalizeTemplate(item = {}) {
    return {
      ...item,
      template_key: String(item.template_key ?? item.key ?? item.id ?? ""),
      name: String(item.name ?? "未命名模板"),
      description: String(item.description ?? ""),
      category: String(item.category ?? "自定义"),
      template_kind: String(item.template_kind ?? "legacy_signal"),
      spec: this.plainObject(item.spec),
    };
  }

  plainObject(value) {
    return value && typeof value === "object" && !Array.isArray(value) ? { ...value } : {};
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
    const full = this.items.filter((item) => item.strategy_kind === "full_strategy").length;
    const composite = this.items.filter((item) => item.spec?.strategy_type === "indicator_composite").length;
    const legacy = this.items.filter((item) => item.strategy_kind !== "full_strategy").length;
    const running = this.deployments.filter((item) => item.status === "running").length;
    this.querySelector("#strategy-total").textContent = String(full).padStart(2, "0");
    this.querySelector("#strategy-active").textContent = String(running).padStart(2, "0");
    this.querySelector("#strategy-defaults").textContent = String(this.indicators.length).padStart(2, "0");
    this.querySelector("#strategy-latest").textContent = String(composite).padStart(2, "0");
    this.querySelector("#strategy-tab-full").textContent = String(full);
    this.querySelector("#strategy-tab-indicators").textContent = String(this.indicators.length);
    this.querySelector("#strategy-tab-deployments").textContent = String(this.deployments.length);
    this.querySelector("#strategy-tab-legacy").textContent = String(legacy);
  }

  filteredItems() {
    return this.items.filter((item) => {
      const kindMatches = this.section === "legacy"
        ? item.strategy_kind !== "full_strategy"
        : item.strategy_kind === "full_strategy";
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
    const isFull = item.strategy_kind === "full_strategy";
    const card = this.node("article", `strategy-card-item ${isFull ? "full-strategy" : "legacy-signal"}`);
    const head = this.node("header", "strategy-card-head");
    const icon = this.node("span", "strategy-card-icon", this.strategyInitial(item.name));
    const title = this.node("div", "strategy-card-title");
    title.append(this.node("strong", "", item.name), this.node("small", "", `${item.category} · ${isFull ? "完整策略" : "旧版信号"}`));
    const state = this.node("span", `strategy-state ${item.status === "active" ? "active" : "draft"}`, item.status === "active" ? "已启用" : "草稿");
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
    } else if (isFull) tags.append(this.node("span", "strategy-kind-tag", `多周期 ${timeframes.regime || "4h"}/${timeframes.setup || "1h"}/${timeframes.trigger || "15m"}`));
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
    meta.append(identity, edit);
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

  displayValue(value) {
    if (value === null || value === undefined || value === "") return "--";
    if (typeof value === "boolean") return value ? "是" : "否";
    if (typeof value === "object") return "已配置";
    return String(value).slice(0, 18);
  }

  openCreate() {
    this.editorMode = "create";
    this.activeItem = null;
    this.preview = null;
    this.querySelector("#strategy-editor-kicker").textContent = "CREATE STRATEGY";
    this.querySelector("#strategy-editor-title").textContent = "新增策略";
    this.querySelector("#strategy-editor-subtitle").textContent = "勾选多个标准指标并配置参数，保存为可执行的完整策略。";
    this.querySelector("#strategy-version-strip").classList.add("hidden");
    this.querySelector("#strategy-composer-block").classList.remove("hidden");
    this.querySelector("#strategy-basic-index").textContent = "02";
    this.querySelector("#strategy-parameters-index").textContent = "--";
    this.querySelector("#strategy-risk-index").textContent = "03";
    this.querySelector("#strategy-ai-panel").classList.remove("hidden");
    this.querySelector("#strategy-ai-title").textContent = "用自然语言生成指标组合";
    this.querySelector("#strategy-ai-description").textContent = "描述周期、风格和风险偏好。AI 会从指标库选择多个指标并调整参数，只生成结构化方案。";
    this.querySelector("#strategy-ai-preview-button strong").textContent = "AI 生成指标方案";
    this.querySelector("#strategy-save strong").textContent = "创建策略";
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
    this.setSelectedIndicators(["ema", "adx", "volume_ratio"].filter((key) => this.indicators.some((item) => item.key === key)).map((key) => ({ key })));
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
    this.preview = null;
    this.querySelector("#strategy-editor-kicker").textContent = "EDIT STRATEGY";
    this.querySelector("#strategy-editor-title").textContent = this.activeItem.name;
    this.querySelector("#strategy-editor-subtitle").textContent = "保存后生成新版本；已完成的回测仍保留当时的策略快照。";
    this.querySelector("#strategy-version-strip").classList.remove("hidden");
    this.querySelector("#strategy-editor-version").textContent = `v${this.activeItem.version}`;
    this.querySelector("#strategy-composer-block").classList.add("hidden");
    this.querySelector("#strategy-basic-index").textContent = "01";
    this.querySelector("#strategy-parameters-index").textContent = "02";
    this.querySelector("#strategy-risk-index").textContent = "03";
    this.querySelector("#strategy-ai-panel").classList.remove("hidden");
    this.querySelector("#strategy-ai-title").textContent = "用自然语言修改策略";
    this.querySelector("#strategy-ai-description").textContent = "描述你想调整的逻辑或风险参数。模型只能提出结构化配置修改。";
    this.querySelector("#strategy-ai-preview-button strong").textContent = "生成修改预览";
    this.querySelector("#strategy-save strong").textContent = "保存新版本";
    this.querySelector("#strategy-name").value = this.activeItem.name;
    this.querySelector("#strategy-category").value = this.activeItem.category;
    this.querySelector("#strategy-description").value = this.activeItem.description;
    this.renderParameterFields(this.activeItem.parameter_schema, this.activeItem.parameters);
    this.renderRiskFields(this.activeItem.risk_defaults);
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
    window.setTimeout(() => this.querySelector("#strategy-name").focus(), 0);
  }

  closeEditor() {
    const layer = this.querySelector("#strategy-dialog-layer");
    layer.classList.add("hidden");
    layer.setAttribute("aria-hidden", "true");
    document.body.classList.remove("strategy-dialog-open");
    this.setButtonBusy(this.querySelector("#strategy-save"), false);
    this.setButtonBusy(this.querySelector("#strategy-ai-preview-button"), false);
    this.setButtonBusy(this.querySelector("#strategy-ai-apply"), false);
  }

  setSelectedIndicators(selections) {
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
    this.renderIndicatorComposer();
  }

  renderIndicatorComposer() {
    const picker = this.querySelector("#strategy-indicator-picker");
    picker.replaceChildren(...this.indicators.map((indicator) => {
      const label = this.node("label", `strategy-indicator-option ${this.selectedIndicators.has(indicator.key) ? "selected" : ""}`);
      const checkbox = document.createElement("input");
      checkbox.type = "checkbox";
      checkbox.checked = this.selectedIndicators.has(indicator.key);
      checkbox.addEventListener("change", () => this.toggleIndicator(indicator.key, checkbox.checked));
      const copy = this.node("span");
      copy.append(this.node("strong", "", indicator.name), this.node("small", "", `${indicator.category} · ${indicator.role === "filter" ? "过滤" : "方向"}`));
      label.append(checkbox, copy);
      return label;
    }));
    const selected = this.querySelector("#strategy-selected-indicators");
    if (!this.selectedIndicators.size) {
      selected.replaceChildren(this.node("div", "strategy-selection-empty", "请从上方至少勾选两个指标。"));
      return;
    }
    selected.replaceChildren(...[...this.selectedIndicators.values()].map((selection) => this.selectedIndicatorPanel(selection)));
  }

  toggleIndicator(key, enabled) {
    this.syncSelectedIndicatorValues();
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
    this.renderIndicatorComposer();
  }

  selectedIndicatorPanel(selection) {
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
      const field = this.configField(definition, current, "indicator");
      const input = field.querySelector("input, select");
      input.dataset.indicatorKey = selection.key;
      input.dataset.indicatorParam = definition.key;
      fields.append(field);
    });
    panel.append(header, fields);
    return panel;
  }

  syncSelectedIndicatorValues() {
    this.querySelectorAll("[data-indicator-key][data-indicator-param]").forEach((input) => {
      const selection = this.selectedIndicators.get(input.dataset.indicatorKey);
      if (!selection) return;
      const value = Number(input.value);
      if (!Number.isFinite(value)) return;
      if (input.dataset.indicatorParam === "weight") selection.weight = value;
      else selection.parameters[input.dataset.indicatorParam] = input.dataset.configType === "integer" ? Math.trunc(value) : value;
    });
  }

  collectIndicatorSelections() {
    this.syncSelectedIndicatorValues();
    return [...this.selectedIndicators.values()].map((selection) => ({
      key: selection.key,
      weight: selection.weight,
      parameters: { ...selection.parameters },
    }));
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
    if (definition.help) label.append(input, this.node("small", "strategy-field-help", definition.help));
    else label.append(input);
    return label;
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
    const indicatorSelections = this.editorMode === "create" ? this.collectIndicatorSelections() : [];
    const directions = this.editorMode === "create"
      ? [this.querySelector("#strategy-direction-long").checked ? "long" : null, this.querySelector("#strategy-direction-short").checked ? "short" : null].filter(Boolean)
      : [];
    if (this.editorMode === "create" && indicatorSelections.length < 2) {
      this.showFormError("请至少选择两个指标，才能形成指标组合策略。");
      return;
    }
    if (this.editorMode === "create" && !directions.length) {
      this.showFormError("请至少选择一个允许交易方向。");
      return;
    }
    const button = this.querySelector("#strategy-save");
    this.setButtonBusy(button, true, this.editorMode === "create" ? "正在创建…" : "正在保存…");
    this.showFormError("");
    try {
      let result;
      if (this.editorMode === "create") {
        const body = {
          name,
          description,
          category,
          indicators: indicatorSelections,
          timeframe: this.querySelector("#strategy-timeframe").value,
          directions,
          confirmation_threshold: Number(this.querySelector("#strategy-confirmation-threshold").value),
          signal_valid_bars: Number(this.querySelector("#strategy-signal-valid-bars").value),
          risk_defaults: this.collectConfig("risk", this.plainObject(this.indicatorDefaults.risk_defaults)),
        };
        result = await this.api("", { method: "POST", body: JSON.stringify(body) });
      } else {
        const body = {
          name,
          description,
          category,
          parameters: this.collectConfig("parameter", this.activeItem.parameters),
          risk_defaults: this.collectConfig("risk", this.activeItem.risk_defaults),
          version: this.activeItem.version,
        };
        result = await this.api(`/${encodeURIComponent(this.activeItem.public_id)}`, { method: "PUT", body: JSON.stringify(body) });
      }
      const item = this.normalizeItem(result?.item ?? result);
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

  async requestAiPreview() {
    if (this.editorMode !== "create" && !this.activeItem?.public_id) return;
    const prompt = this.querySelector("#strategy-ai-prompt").value.trim();
    if (prompt.length < 4) {
      this.showAiError("请先描述希望修改的参数或规则，至少输入 4 个字符。");
      return;
    }
    const button = this.querySelector("#strategy-ai-preview-button");
    this.setButtonBusy(button, true, "模型分析中…");
    this.showAiError("");
    this.clearPreview();
    try {
      const path = this.editorMode === "create"
        ? "/compose/ai-preview"
        : `/${encodeURIComponent(this.activeItem.public_id)}/ai-preview`;
      const result = await this.api(path, {
        method: "POST",
        body: JSON.stringify({ prompt }),
      });
      this.preview = {
        base_version: Number(result?.base_version ?? this.activeItem?.version ?? 1),
        provider: String(result?.provider ?? "AI model"),
        summary: String(result?.summary ?? "模型已生成受约束的指标组合建议。"),
        changes: Array.isArray(result?.changes) ? result.changes : [],
        proposed: this.plainObject(result?.proposed),
        draft: this.plainObject(result?.draft),
      };
      this.renderPreview();
    } catch (error) {
      this.showAiError(error?.message || "AI 修改预览生成失败，请稍后重试。");
    } finally {
      this.setButtonBusy(button, false);
    }
  }

  renderPreview() {
    if (!this.preview) return;
    const panel = this.querySelector("#strategy-ai-preview");
    panel.classList.remove("hidden");
    this.querySelector("#strategy-ai-provider").textContent = this.providerLabel(this.preview.provider);
    this.querySelector("#strategy-ai-base-version").textContent = this.editorMode === "create" ? "新策略草案" : `基于 v${this.preview.base_version}`;
    this.querySelector("#strategy-ai-summary").textContent = this.preview.summary;
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
    this.querySelector("#strategy-ai-apply").disabled = false;
  }

  clearPreview() {
    this.preview = null;
    this.querySelector("#strategy-ai-preview").classList.add("hidden");
    this.querySelector("#strategy-ai-changes").replaceChildren();
    this.querySelector("#strategy-ai-apply").disabled = false;
  }

  async applyAiPreview() {
    if (!this.preview) return;
    if (this.editorMode === "create") {
      this.applyCompositionDraft(this.preview.draft);
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
      confirmation_threshold: "确认阈值",
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
    const message = String(error?.message || "保存失败，请稍后重试。");
    if (/version|版本|conflict|冲突/i.test(message)) return "策略已在其他页面更新。请关闭编辑器并刷新列表后再修改。";
    return message;
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
