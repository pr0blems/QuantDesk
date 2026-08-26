from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_legacy_strategy_center_exposes_complete_strategy_workflow() -> None:
    script = (ROOT / "src/quantdesk_v2/static/strategies.js").read_text(encoding="utf-8")

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
    assert "最多 2 路并发" in script
    assert "AI 只生成符合当前 PARAMETERS 范围的候选参数" in script


def test_react_canary_loads_the_current_strategy_component_asset() -> None:
    entrypoint = (ROOT / "web/src/main.tsx").read_text(encoding="utf-8")
    stylesheet = (ROOT / "src/quantdesk_v2/static/strategies.css").read_text(
        encoding="utf-8"
    )

    index = (ROOT / "web/index.html").read_text(encoding="utf-8")
    legacy_panel = (ROOT / "web/src/pages/LegacyPanel.tsx").read_text(encoding="utf-8")

    assert "/assets/strategies.js?v=20260826-strategy-suite1" in index
    assert index.index("/assets/strategies.js?v=20260826-strategy-suite1") < index.index("/src/main.tsx")
    assert "/assets/strategies.js" not in entrypoint
    assert "window.customElements.get(tag)" in legacy_panel
    assert "document.createElement(tag)" in legacy_panel
    assert "host.replaceChildren(element)" in legacy_panel
    assert 'import { createElement' not in legacy_panel
    assert "return createElement(tag" not in legacy_panel
    assert "功能组件加载失败" in legacy_panel
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
    assert '#strategy-basic-block > label:last-child' in stylesheet
    assert 'display: none' not in stylesheet.split(
        '#strategy-basic-block > label:last-child', 1
    )[1].split('}', 1)[0]


def test_legacy_shell_busts_the_current_strategy_asset_cache() -> None:
    index = (ROOT / "src/quantdesk_v2/static/index.html").read_text(encoding="utf-8")

    assert "/assets/strategies.js?v=20260826-strategy-suite1" in index


def test_strategy_custom_element_initializes_after_construction() -> None:
    script = (ROOT / "src/quantdesk_v2/static/strategies.js").read_text(encoding="utf-8")
    constructor = script.split("constructor() {", 1)[1].split("connectedCallback() {", 1)[0]
    connected = script.split("connectedCallback() {", 1)[1].split("renderShell() {", 1)[0]

    assert "this.renderShell()" not in constructor
    assert "this.bindEvents()" not in constructor
    assert "this.renderShell()" in connected
    assert "this.bindEvents()" in connected


def test_paper_strategy_editor_preserves_an_unavailable_current_strategy() -> None:
    page = (ROOT / "web/src/pages/PaperPage.tsx").read_text(encoding="utf-8")

    assert "const selectedStrategyIds = selected" in page
    assert "const unavailableStrategyIds = selectedStrategyIds.filter(" in page
    assert "defaultValue={selectedStrategyIds}" in page
    assert "unavailableStrategyIds.map" in page
    assert "<option key={strategyId} value={strategyId} disabled>" in page
    assert "当前绑定（已归档或不可用）" in page
    assert page.index("unavailableStrategyIds.map") < page.index(
        "activeStrategies.map", page.index("defaultValue={selectedStrategyIds}")
    )
