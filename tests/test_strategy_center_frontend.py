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
    ):
        assert api_contract in script


def test_react_canary_loads_the_current_strategy_component_asset() -> None:
    entrypoint = (ROOT / "web/src/main.tsx").read_text(encoding="utf-8")
    stylesheet = (ROOT / "src/quantdesk_v2/static/strategies.css").read_text(
        encoding="utf-8"
    )

    index = (ROOT / "web/index.html").read_text(encoding="utf-8")
    legacy_panel = (ROOT / "web/src/pages/LegacyPanel.tsx").read_text(encoding="utf-8")

    assert "/assets/strategies.js?v=20260824-code1" in index
    assert index.index("/assets/strategies.js?v=20260824-code1") < index.index("/src/main.tsx")
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
