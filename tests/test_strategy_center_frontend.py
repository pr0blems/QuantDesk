from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_strategy_center_exposes_complete_strategy_workflow() -> None:
    script = (ROOT / "web/src/controllers/strategies.js").read_text(encoding="utf-8")

    for visible_contract in (
        'data-section="signals"',
        'data-create-mode="indicators"',
        'data-create-mode="template"',
        'id="strategy-detail-layer"',
        'data-edit-scope="parameters"',
        'data-edit-scope="code"',
        'data-edit-scope="source"',
        'data-create-mode="source"',
        'id="strategy-code-editor"',
        'id="strategy-workbench-actions"',
        'data-workbench-tab="backtest"',
        'data-workbench-tab="ai"',
        'id="strategy-runner-submit"',
        'id="strategy-runner-canvas"',
        'id="strategy-runner-symbol-search"',
        'data-runner-scope="all"',
        'id="strategy-runner-analysis"',
        'id="strategy-runner-ai-optimize"',
        'id="strategy-source-composition-block"',
        'id="strategy-source-indicator-picker"',
        'id="strategy-ai-messages"',
        'id="strategy-ai-process-steps"',
        'this.node("button", "strategy-edit-button secondary", "详情")',
        'this.node("button", "strategy-edit-button secondary", "验证")',
        'this.node("button", "strategy-edit-button danger", "归档")',
    ):
        assert visible_contract in script

    for api_contract in (
        '/revisions`',
        '/validate`',
        'method: "DELETE"',
        'template_key: this.querySelector("#strategy-template").value',
        'this.signalCard(item)',
        '/code/validate`',
        '${codeEdit ? "/code/ai-preview" : "/ai-preview"}',
        '/code`, { method: "PUT"',
        '/source`, { method: "PUT"',
        '/source/validate`',
        '/source/ai-preview`',
        'source_code: codeSpec',
        'this.backtestApi("/catalog")',
        'this.runSourceBacktest()',
        'this.persistSourceWorkbench()',
        'indicatorSourceCreate',
        'composition: this.sourceComposition',
        'this.processAiPromptQueue()',
        'this.conversationPrompt(turn.prompt)',
        'normalizeSourceAiDraft',
        'sourceAiDraftChanges',
        'this.api("/compose/ai-preview"',
        'autoCompose: false',
        'this.sourceComposition = this.deriveSourceComposition(item);',
        'validation.trigger_timeframe || dataRequirements.trigger_timeframe',
        'sourceTriggerTimeframe(item = this.activeItem)',
        'dataRequirements.trigger_timeframe,',
        'timeframe.disabled = sourceLocked',
        'timeframe: this.sourceTriggerTimeframe() ||',
        'this.ensureSourceBacktestEligibility()',
        'this.promoteLifecycle(item, "validated")',
        'this.runSourceBacktestSuite(payload, symbols)',
        'this.buildSourceBacktestAnalysis(completed, failures, symbols.length)',
        'this.optimizeSourceBacktestParameters()',
    ):
        assert api_contract in script

    assert "用自然语言编排完整策略" in script
    assert "由 AI 根据自然语言自动选择，可人工复核" in script
    assert "按当前选择重新生成源码" in script
    assert "应用完整策略草稿" in script
    assert "risk_defaults: this.collectConfig(\"risk\", this.sourceRiskDefaults)" in script
    assert script.index('data-edit-scope="source"') < script.index(
        'data-edit-scope="parameters"'
    )
    assert script.index('data-workbench-tab="ai"') < script.index(
        'data-workbench-tab="backtest"'
    )
    assert 'id="strategy-dsl-scope" class="hidden"' in script
    assert "由 Python 源码中的 PARAMETERS 动态生成" in script
    assert "syncSourceParameterContract" in script
    assert "setSourceWorkbenchShellActive" in script
    assert "不展示模型隐式推理" in script
    assert "继续发送下一条" in script
    assert "sourceCompositionDirty" in script
    assert "aiPromptQueue" in script
    assert 'sourceAvailable && ["source", "parameters"].includes(this.editScope)' in script
    assert 'strategyForm.addEventListener("invalid"' in script
    assert 'strategyForm.addEventListener("input"' in script
    assert "localizeFieldValidation" in script
    assert "请填写此字段。" in script
    assert "最接近的有效值是" in script
    assert 'input.dataset.declaredStep = input.step' in script
    assert "localizedErrorMessage" in script
    assert script.index("await this.ensureSourceBacktestEligibility()") < script.index(
        "const payload = this.sourceBacktestPayload()"
    )
    assert "正在校验当前源码并取得回测资格" in script
    assert "sourceBacktestConcurrency" in script
    assert "executeSourceBacktestWithRetry" in script
    assert "容量冲突会自动排队重试" in script
    assert "sourceOptimizationPartitions" in script
    assert "validateSourceOptimizationCandidate" in script
    assert "候选未通过隔离验证，已禁止应用" in script
    assert "AI 只生成符合当前 PARAMETERS 范围的候选参数" in script


def test_react_frontend_mounts_the_current_strategy_controller_asset() -> None:
    entrypoint = (ROOT / "web/src/main.tsx").read_text(encoding="utf-8")
    stylesheet = (ROOT / "web/src/theme/strategies.css").read_text(
        encoding="utf-8"
    )

    index = (ROOT / "web/index.html").read_text(encoding="utf-8")
    controller_panel = (ROOT / "web/src/pages/PageControllerPanel.tsx").read_text(encoding="utf-8")

    assert "/assets/controller-runtime.js" not in index
    assert "/assets/strategies.js" not in index
    assert entrypoint.index('import "./controllers/controller-runtime.js"') < entrypoint.index(
        'import "./controllers/strategies.js"'
    )
    assert 'import "./controllers/strategies.js"' in entrypoint
    assert "window.quantdeskMountPageController(name, host)" in controller_panel
    assert "document.createElement" not in controller_panel
    assert "customElements" not in controller_panel
    assert "window.quantdeskUnmountPageController(host)" in controller_panel
    assert "功能组件加载失败" in controller_panel
    assert ".strategy-card-actions" in stylesheet
    assert ".strategy-detail-dialog" in stylesheet
    assert ".strategy-revision-row" in stylesheet
    assert ".strategy-code-editor" in stylesheet
    assert ".strategy-code-status.success" in stylesheet
    assert ".strategy-editor.source-workbench" in stylesheet
    assert ".strategy-editor.source-workbench-shell" in stylesheet
    assert ".strategy-runner-result" in stylesheet
    assert ".strategy-runner-scope" in stylesheet
    assert ".strategy-runner-symbol-field" in stylesheet
    assert ".strategy-runner-analysis" in stylesheet
    assert ".strategy-runner-ai-optimize" in stylesheet
    assert ".strategy-source-composition-block" in stylesheet
    assert ".strategy-ai-conversation" in stylesheet
    assert ".strategy-ai-process-step" in stylesheet
    assert ".strategy-parameter-group" in stylesheet
    assert ".strategy-switch-input:checked" in stylesheet
    assert '#strategy-basic-block > label:last-child' in stylesheet
    assert 'display: none' not in stylesheet.split(
        '#strategy-basic-block > label:last-child', 1
    )[1].split('}', 1)[0]


def test_react_shell_loads_the_current_strategy_controller() -> None:
    index = (ROOT / "web/index.html").read_text(encoding="utf-8")
    entrypoint = (ROOT / "web/src/main.tsx").read_text(encoding="utf-8")

    assert "/assets/controller-runtime.js" not in index
    assert "/assets/strategies.js" not in index
    assert 'import "./controllers/controller-runtime.js"' in entrypoint
    assert 'import "./controllers/strategies.js"' in entrypoint


def test_grouped_binary_parameters_render_as_switches_and_preserve_integer_contract() -> None:
    script = (ROOT / "web/src/controllers/strategies.js").read_text(encoding="utf-8")

    assert 'definition.control === "switch" && type === "integer"' in script
    assert 'input.dataset.binaryInteger = "true"' in script
    assert 'state.textContent = input.checked ? "已开启" : "已关闭"' in script
    assert 'output[key] = input.checked ? 1 : 0' in script
    assert '"技术指标开关": "开启的指标参与当前 AI 机会决策"' in script


def test_strategy_controller_initializes_after_mount() -> None:
    script = (ROOT / "web/src/controllers/strategies.js").read_text(encoding="utf-8")
    constructor = script.split("constructor(host) {", 1)[1].split("connectedCallback() {", 1)[0]
    connected = script.split("connectedCallback() {", 1)[1].split("renderShell() {", 1)[0]

    assert "this.renderShell()" not in constructor
    assert "this.bindEvents()" not in constructor
    assert "this.renderShell()" in connected
    assert "this.bindEvents()" in connected


def test_parameterized_complete_strategies_validate_without_revision_promotion() -> None:
    script = (ROOT / "web/src/controllers/strategies.js").read_text(encoding="utf-8")

    assert 'const revisionWorkflow = ["python_source", "strategy_dsl"].includes(item.management_mode);' in script
    assert script.count('const revisionWorkflow = ["python_source", "strategy_dsl"].includes(item.management_mode);') == 2
    assert 'if (revisionWorkflow && ["draft", "published"].includes(item.lifecycle_status))' in script


def test_all_supported_strategies_share_the_complete_strategy_view() -> None:
    script = (ROOT / "web/src/controllers/strategies.js").read_text(encoding="utf-8")

    assert 'data-section="legacy"' not in script
    assert '>旧版策略<' not in script
    assert '"旧版信号"' not in script
    assert 'isManagedAiMonitorStrategy(item = {})' in script
    assert 'templateKey === "ai_monitor_actionable_entry_v11"' in script
    assert 'managedPolicy === "ai_monitor_actionable_entry_v11"' in script
    assert 'return item.complete_strategy !== false;' in script
    assert 'const full = this.items.filter((item) => this.isCompleteStrategy(item)).length;' in script
    assert 'this.querySelector("#strategy-tab-legacy")' not in script
    assert '参数化完整策略' in script
    assert '参数引擎 · ${item.engine_key}' in script
    assert 'const lifecycleText = isManagedAiMonitor ? "自动监控中"' in script
    assert 'isManagedAiMonitor ? "自动监控当前策略"' in script


def test_controller_runtime_is_bundled_and_react_mounts_directly() -> None:
    runtime = (ROOT / "web/src/controllers/controller-runtime.js").read_text(encoding="utf-8")
    app = (ROOT / "web/src/App.tsx").read_text(encoding="utf-8")

    assert "function mountPageController(name, host)" in runtime
    assert "function unmountPageController(host)" in runtime
    assert "function getMountedPageController(host)" in runtime
    assert "class QuantDeskControllerElement extends HTMLElement" in runtime
    assert "global.quantdeskMountPageController = mountPageController" in runtime
    assert "global.quantdeskGetMountedPageController = getMountedPageController" in runtime
    assert "LegacyPanel" not in app
    assert app.count("<PageControllerPanel") == 6


def test_unused_alternate_react_pages_are_removed() -> None:
    page_dir = ROOT / "web/src/pages"
    for name in ("MonitorPage.tsx", "PaperPage.tsx", "LivePage.tsx", "StrategiesPage.tsx", "BacktestsPage.tsx"):
        assert not (page_dir / name).exists()


def test_frontend_source_has_a_single_web_owner() -> None:
    assert not (ROOT / "src/quantdesk_v2/static").exists()
    assert (ROOT / "web/src/controllers/ai-monitor.js").is_file()
    assert (ROOT / "web/src/theme/style.css").is_file()
    assert (ROOT / "web/public/assets/ai-monitor.css").is_file()

    backend = (ROOT / "src/quantdesk_v2/main.py").read_text(encoding="utf-8")
    config = (ROOT / "src/quantdesk_v2/config.py").read_text(encoding="utf-8")
    assert "runtime_settings.static_dir" not in backend
    assert 'app.mount(\n            "/assets"' not in backend
    assert "def static_dir" not in config
