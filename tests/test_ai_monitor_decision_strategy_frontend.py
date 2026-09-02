from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = (ROOT / "src/quantdesk_v2/static/ai-monitor.js").read_text(encoding="utf-8")
STYLES = (ROOT / "src/quantdesk_v2/static/ai-monitor.css").read_text(encoding="utf-8")


def test_decision_strategy_entry_precedes_scheduler_state() -> None:
    entry = 'id="decision-strategy-button"'
    scheduler = 'id="scheduler-state"'

    assert entry in SCRIPT
    assert SCRIPT.index(entry) < SCRIPT.index(scheduler)
    assert 'class="decision-strategy-trigger"' in SCRIPT
    assert "this.state.overview?.decision_strategy" in SCRIPT
    assert "<strong>${this.escape(strategy.name)}</strong>" in SCRIPT
    assert "actionable_entry_v11}</strong>" not in SCRIPT


def test_decision_strategy_modal_exposes_live_runtime_parameters() -> None:
    assert 'id="decision-strategy-modal"' in SCRIPT
    assert 'id="decision-strategy-title">当前决策策略' in SCRIPT
    assert 'name="minimum_combined_score"' in SCRIPT
    assert 'name="minimum_indicator_score"' in SCRIPT
    assert 'name="maximum_market_age_seconds"' in SCRIPT
    assert 'name="minimum_feature_quality"' in SCRIPT
    assert 'name="minimum_market_flow_quality"' in SCRIPT
    assert 'this.api("/config", { method: "PUT"' in SCRIPT
    assert "decisionConfigPayload" in SCRIPT


def test_decision_version_was_removed_from_data_health_grid() -> None:
    health_start = SCRIPT.index("renderSignalHealth()")
    health_end = SCRIPT.index("renderMacroMarket()", health_start)
    health_source = SCRIPT[health_start:health_end]

    assert "决策版本" not in health_source
    assert ".decision-strategy-dialog" in STYLES
    assert ".decision-parameter-grid" in STYLES
