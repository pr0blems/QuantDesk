class StrategyCenter extends HTMLElement {
  constructor() {
    super();
    this.started = false;
    this.loading = false;
    this.sessionGeneration = 0;
    this.loadVersion = 0;
    this.items = [];
    this.templates = [];
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
            <p>管理当前账户的策略、参数和风险默认值；数据回测会直接读取这里的最新版本。</p>
          </div>
          <button id="strategy-create" class="strategy-create-button" type="button"><span aria-hidden="true">＋</span>新增策略</button>
        </header>

        <div id="strategy-notice" class="strategy-notice hidden" role="status" aria-live="polite"></div>

        <section class="strategy-overview" aria-label="策略统计">
          <article><span>我的策略</span><strong id="strategy-total">--</strong><small>按用户独立保存</small></article>
          <article><span>已启用</span><strong id="strategy-active">--</strong><small>可用于数据回测</small></article>
          <article><span>默认副本</span><strong id="strategy-defaults">--</strong><small>首次登录自动创建</small></article>
          <article><span>最近更新</span><strong id="strategy-latest">--</strong><small>版本变更可追溯</small></article>
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

              <div id="strategy-template-field" class="strategy-form-block hidden">
                <div class="strategy-section-heading"><div><span>01</span><strong>创建方式</strong></div><small>可从系统默认模板复制</small></div>
                <label>策略模板<select id="strategy-template"><option value="">空白策略</option></select></label>
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
                <div><span>AI SEMANTIC EDITOR</span><h3>用自然语言修改策略</h3></div>
                <span class="strategy-ai-status"><i></i>受约束配置</span>
              </header>
              <p>描述你想调整的逻辑或风险参数。模型只能提出结构化配置修改，不会生成或执行任意代码。</p>
              <label>修改要求<textarea id="strategy-ai-prompt" rows="5" maxlength="1200" placeholder="例如：把短期均线改为 12，长期均线改为 48，止损收紧到 3%，其他配置保持不变。"></textarea></label>
              <div class="strategy-ai-examples" aria-label="AI 编辑示例">
                <button type="button" data-ai-example="把策略调整得更稳健：缩小单次仓位，并把止损收紧到 3%。">更稳健</button>
                <button type="button" data-ai-example="减少噪声信号，适当增加确认周期，其他参数不变。">减少噪声</button>
                <button type="button" data-ai-example="保持入场条件不变，把止盈调整为止损的两倍。">优化盈亏比</button>
              </div>
              <button id="strategy-ai-preview-button" class="strategy-ai-preview-button" type="button"><span aria-hidden="true">✦</span><strong>生成修改预览</strong></button>
              <div class="strategy-safety-note"><span aria-hidden="true">!</span><p><strong>仅生成预览，不执行交易</strong>。确认应用后只会保存新策略版本，仍需通过回测与模拟盘验证。</p></div>

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
    this.querySelector("#strategy-template").addEventListener("change", (event) => this.applyTemplate(event.target.value));
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
    this.query = "";
    this.statusFilter = "all";
    this.categoryFilter = "all";
    this.activeItem = null;
    this.preview = null;
    this.querySelector("#strategy-search").value = "";
    this.querySelector("#strategy-category-filter").replaceChildren(this.option("all", "全部分类"));
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
      const payload = await this.api();
      if (generation !== this.sessionGeneration || requestVersion !== this.loadVersion) return;
      this.items = Array.isArray(payload?.items) ? payload.items.map((item) => this.normalizeItem(item)) : [];
      this.templates = Array.isArray(payload?.templates) ? payload.templates.map((item) => this.normalizeTemplate(item)) : [];
      this.renderFilters();
      this.renderStats();
      this.renderCards();
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
    const total = this.items.length;
    const active = this.items.filter((item) => item.status === "active").length;
    const defaults = this.items.filter((item) => item.is_default || item.source_template_key).length;
    const latest = [...this.items].sort((a, b) => new Date(b.updated_at || b.created_at || 0) - new Date(a.updated_at || a.created_at || 0))[0];
    this.querySelector("#strategy-total").textContent = total ? String(total).padStart(2, "0") : "0";
    this.querySelector("#strategy-active").textContent = active ? String(active).padStart(2, "0") : "0";
    this.querySelector("#strategy-defaults").textContent = defaults ? String(defaults).padStart(2, "0") : "0";
    this.querySelector("#strategy-latest").textContent = latest ? this.shortDate(latest.updated_at || latest.created_at) : "暂无";
  }

  filteredItems() {
    return this.items.filter((item) => {
      const isDefault = Boolean(item.is_default || item.source_template_key);
      const statusMatches = this.statusFilter === "all"
        || (this.statusFilter === "default" && isDefault)
        || (this.statusFilter === "custom" && !isDefault);
      const categoryMatches = this.categoryFilter === "all" || item.category === this.categoryFilter;
      const haystack = `${item.name} ${item.category} ${item.description} ${item.engine_key}`.toLocaleLowerCase("zh-CN");
      return statusMatches && categoryMatches && (!this.query || haystack.includes(this.query));
    });
  }

  renderCards() {
    const grid = this.querySelector("#strategy-grid");
    grid.setAttribute("aria-busy", "false");
    const items = this.filteredItems();
    if (!items.length) {
      const empty = this.node("div", "strategy-grid-state");
      empty.append(this.node("span", "strategy-state-icon", this.items.length ? "⌕" : "策"));
      empty.append(this.node("strong", "", this.items.length ? "没有匹配的策略" : "还没有个人策略"));
      empty.append(this.node("small", "", this.items.length ? "调整搜索词或筛选条件后再试" : "首次登录的默认策略正在创建；也可以手动新增策略"));
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
        } else this.openCreate();
      });
      empty.append(action);
      grid.replaceChildren(empty);
      return;
    }

    grid.replaceChildren(...items.map((item) => this.strategyCard(item)));
  }

  strategyCard(item) {
    const card = this.node("article", "strategy-card-item");
    const head = this.node("header", "strategy-card-head");
    const icon = this.node("span", "strategy-card-icon", this.strategyInitial(item.name));
    const title = this.node("div", "strategy-card-title");
    title.append(this.node("strong", "", item.name), this.node("small", "", item.category));
    const state = this.node("span", `strategy-state ${item.status === "active" ? "active" : "draft"}`, item.status === "active" ? "已启用" : "草稿");
    head.append(icon, title, state);

    const description = this.node("p", "strategy-card-description", item.description || "尚未填写策略说明");
    const tags = this.node("div", "strategy-card-tags");
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
    this.querySelector("#strategy-editor-subtitle").textContent = "从系统默认模板复制，或创建一个空白的安全参数策略。";
    this.querySelector("#strategy-version-strip").classList.add("hidden");
    this.querySelector("#strategy-template-field").classList.remove("hidden");
    this.querySelector("#strategy-basic-index").textContent = "02";
    this.querySelector("#strategy-parameters-index").textContent = "03";
    this.querySelector("#strategy-risk-index").textContent = "04";
    this.querySelector("#strategy-ai-panel").classList.add("hidden");
    this.querySelector("#strategy-save strong").textContent = "创建策略";
    this.populateTemplateSelect();
    this.querySelector("#strategy-form").reset();
    this.querySelector("#strategy-template").value = "";
    this.querySelector("#strategy-name").value = "";
    this.querySelector("#strategy-category").value = "自定义";
    this.querySelector("#strategy-description").value = "";
    this.renderParameterFields([], {});
    this.renderRiskFields({});
    this.showFormError("");
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
    this.querySelector("#strategy-template-field").classList.add("hidden");
    this.querySelector("#strategy-basic-index").textContent = "01";
    this.querySelector("#strategy-parameters-index").textContent = "02";
    this.querySelector("#strategy-risk-index").textContent = "03";
    this.querySelector("#strategy-ai-panel").classList.remove("hidden");
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

  populateTemplateSelect() {
    const select = this.querySelector("#strategy-template");
    select.replaceChildren(this.option("", "基础空白 · 趋势突破引擎"), ...this.templates.map((template) => this.option(template.template_key, `${template.name} · ${template.category}`)));
  }

  applyTemplate(key) {
    const template = this.templates.find((item) => item.template_key === String(key));
    if (!template) {
      this.querySelector("#strategy-name").value = "";
      this.querySelector("#strategy-category").value = "自定义";
      this.querySelector("#strategy-description").value = "";
      return;
    }
    this.querySelector("#strategy-name").value = template.name;
    this.querySelector("#strategy-category").value = template.category;
    this.querySelector("#strategy-description").value = template.description;
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
    const available = this.editorMode === "create" ? [] : definitions.filter((definition) => Object.prototype.hasOwnProperty.call(source, definition.key));
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
    const button = this.querySelector("#strategy-save");
    this.setButtonBusy(button, true, this.editorMode === "create" ? "正在创建…" : "正在保存…");
    this.showFormError("");
    try {
      let result;
      if (this.editorMode === "create") {
        const templateKey = this.querySelector("#strategy-template").value;
        const body = { name, description, category };
        if (templateKey) body.template_key = templateKey;
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
    if (!this.activeItem?.public_id) return;
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
      const result = await this.api(`/${encodeURIComponent(this.activeItem.public_id)}/ai-preview`, {
        method: "POST",
        body: JSON.stringify({ prompt }),
      });
      this.preview = {
        base_version: Number(result?.base_version ?? this.activeItem.version),
        provider: String(result?.provider ?? "AI model"),
        summary: String(result?.summary ?? "模型已生成受约束的策略修改建议。"),
        changes: Array.isArray(result?.changes) ? result.changes : [],
        proposed: this.plainObject(result?.proposed),
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
    this.querySelector("#strategy-ai-base-version").textContent = `基于 v${this.preview.base_version}`;
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
    if (!this.preview || !this.activeItem?.public_id) return;
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
