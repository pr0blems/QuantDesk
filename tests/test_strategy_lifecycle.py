from types import SimpleNamespace

from quantdesk_v2 import strategy_lifecycle
from quantdesk_v2.strategy_catalog import ENGINE_PARAMETER_SCHEMAS
from quantdesk_v2.strategy_lifecycle import (
    BACKTEST_ELIGIBLE_STATUSES,
    LIVE_ELIGIBLE_STATUSES,
    PAPER_ELIGIBLE_STATUSES,
    SHADOW_ELIGIBLE_STATUSES,
    _static_validation_check,
    paper_eligibility,
)


def test_lifecycle_capability_sets_are_fail_closed() -> None:
    assert "draft" not in BACKTEST_ELIGIBLE_STATUSES
    assert "draft" not in PAPER_ELIGIBLE_STATUSES
    assert "draft" not in LIVE_ELIGIBLE_STATUSES
    assert "published" not in BACKTEST_ELIGIBLE_STATUSES
    assert "published" not in PAPER_ELIGIBLE_STATUSES
    assert "published" not in LIVE_ELIGIBLE_STATUSES
    assert "validated" in BACKTEST_ELIGIBLE_STATUSES
    assert "validated" not in PAPER_ELIGIBLE_STATUSES
    assert "backtested" not in PAPER_ELIGIBLE_STATUSES
    assert "backtested" not in SHADOW_ELIGIBLE_STATUSES
    assert "shadow" in SHADOW_ELIGIBLE_STATUSES
    assert "shadow" not in PAPER_ELIGIBLE_STATUSES
    assert "paper" in PAPER_ELIGIBLE_STATUSES
    assert LIVE_ELIGIBLE_STATUSES == {"micro_live", "live"}


def test_static_validation_is_bound_to_the_current_source_hash() -> None:
    strategy = SimpleNamespace(
        lifecycle_status="draft",
        strategy_kind="source_strategy",
        source_hash="source-hash-v1",
        source_code="def evaluate(context, params):\n    return {'decision': 'HOLD'}\n",
        spec_hash=None,
        spec_json=None,
    )
    revision = SimpleNamespace(
        lifecycle_status="draft",
        validation_json={"valid": True},
        source_hash="source-hash-v1",
        spec_hash=None,
    )

    valid, detail = _static_validation_check(strategy, revision)

    assert valid is True
    assert "一致" in detail

    revision.source_hash = "stale-source-hash"
    valid, detail = _static_validation_check(strategy, revision)

    assert valid is False
    assert "源码哈希" in detail


def test_static_validation_rejects_lifecycle_drift() -> None:
    strategy = SimpleNamespace(
        lifecycle_status="validated",
        strategy_kind="full_strategy",
        source_hash=None,
        source_code=None,
        spec_hash="spec-hash-v2",
        spec_json={"schema_version": 1},
    )
    revision = SimpleNamespace(
        lifecycle_status="draft",
        validation_json={"valid": True},
        source_hash=None,
        spec_hash="spec-hash-v2",
    )

    valid, detail = _static_validation_check(strategy, revision)

    assert valid is False
    assert "生命周期状态不一致" in detail


def test_curated_martingale_tp4_revision_is_paper_eligible_after_backtest(monkeypatch) -> None:
    parameters = {
        item["key"]: item["default"] for item in ENGINE_PARAMETER_SCHEMAS["martingale_tp4"]
    }
    strategy = SimpleNamespace(
        id=7,
        user_id=3,
        public_id="strategy-tp4",
        status="active",
        version=1,
        lifecycle_status="validated",
        strategy_kind="basket_strategy",
        engine_key="martingale_tp4",
        created_via="system_default",
        source_template_id=11,
        parameters_json=parameters,
        source_hash=None,
        source_code=None,
        spec_hash=None,
        spec_json=None,
    )
    revision = SimpleNamespace(
        id=19,
        version=1,
        lifecycle_status="validated",
        validation_json={"valid": True, "engine": "martingale_tp4_engine_v1"},
        snapshot_json={
            "version": 1,
            "engine_key": "martingale_tp4",
            "strategy_kind": "basket_strategy",
            "parameters": parameters,
        },
        source_hash=None,
        spec_hash=None,
    )
    monkeypatch.setattr(strategy_lifecycle, "_backtest_evidence", lambda _db, _id: (2, 31))

    valid, detail = _static_validation_check(strategy, revision)
    eligible, eligibility_detail = paper_eligibility(object(), strategy, revision)

    assert valid is True
    assert "一致" in detail
    assert eligible is True
    assert "2 次回测" in eligibility_detail
