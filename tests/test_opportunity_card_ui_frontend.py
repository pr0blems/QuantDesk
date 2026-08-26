from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = (ROOT / "src/quantdesk_v2/static/ai-monitor.js").read_text(encoding="utf-8")
STYLES = (ROOT / "src/quantdesk_v2/static/ai-monitor.css").read_text(encoding="utf-8")


def test_opportunity_card_has_distinct_summary_details_and_actions() -> None:
    for marker in (
        'class="opportunity-identity"',
        'class="opportunity-symbol-line"',
        'class="opportunity-badges"',
        'class="opportunity-quotes"',
        'class="opportunity-top-metrics"',
        'class="opportunity-details-shell opportunity-details-primary"',
        'class="opportunity-details-shell opportunity-details-secondary"',
        'class="opportunity-details-content"',
        'class="opportunity-card-footer"',
        'class="opportunity-card-actions"',
    ):
        assert marker in SCRIPT

    assert "预测仅供研判，不构成投资建议" in SCRIPT
    assert 'const positionPanel = triggeredPosition ?' in SCRIPT
    assert 'const candidatePositionPanel = candidateSummaryVisible ?' in SCRIPT
    assert '${compactSummaryPanel}' in SCRIPT
    assert '${expandedSignalSummaryPanel}' in SCRIPT
    assert '${conclusionControl}${detailControl}' in SCRIPT


def test_opportunity_details_toggle_updates_accessible_state() -> None:
    assert 'button.dataset.state = nextExpanded ? "expanded" : "collapsed"' in SCRIPT
    assert 'details.setAttribute("aria-hidden", String(!nextExpanded))' in SCRIPT
    assert 'button.replaceChildren(label)' in SCRIPT
    assert 'card.querySelectorAll(".opportunity-details-shell").forEach((details) =>' in SCRIPT


def test_opportunity_card_styles_animate_and_keep_summary_visible() -> None:
    assert ".opportunity-details-shell" in STYLES
    assert '.opportunity-details-shell[aria-hidden="true"] .opportunity-details-content' in STYLES
    assert "grid-template-rows: 0fr" in STYLES
    assert ".opportunity-item.is-expanded .opportunity-details-shell" in STYLES
    assert "grid-template-rows: 1fr" in STYLES
    assert ".opportunity-list { align-items: start" in STYLES
    assert "grid-column: 1 / -1" in STYLES
    assert ".opportunity-item:is(.has-position, .has-candidate-summary):not(.is-expanded) > .opportunity-signal" in STYLES
    assert ".opportunity-item > .virtual-position" in STYLES
    assert ".opportunity-list .opportunity-item > header > .opportunity-identity" in STYLES
    assert ".virtual-position.direction-long .virtual-position-title" in STYLES
    assert ".virtual-position.direction-short .virtual-position-title" in STYLES
    assert ".opportunity-item > .opportunity-card-footer" in STYLES
    assert "@container (max-width: 680px)" in STYLES
    assert "@container (max-width: 520px)" in STYLES


def test_expanded_card_keeps_compact_summary_before_details() -> None:
    summary = SCRIPT.index("${compactSummaryPanel}")
    primary = SCRIPT.index('data-patch-key="details-primary"', summary)
    signal = SCRIPT.index("${expandedSignalSummaryPanel}", primary)
    secondary = SCRIPT.index('data-patch-key="details-secondary"', signal)
    footer = SCRIPT.index('data-patch-key="card-actions"', secondary)
    assert summary < primary < signal < secondary < footer


def test_live_candidate_summary_is_honest_and_uses_real_fields() -> None:
    assert 'aria-label="候选信号摘要"' in SCRIPT
    assert "尚未入场" in SCRIPT
    assert "实时观察价，尚未冻结入场" in SCRIPT
    assert "当前参考价" in SCRIPT
    assert "触发进度" in SCRIPT
