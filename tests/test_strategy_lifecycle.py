from types import SimpleNamespace

from quantdesk_v2.strategy_lifecycle import (
    BACKTEST_ELIGIBLE_STATUSES,
    LIVE_ELIGIBLE_STATUSES,
    PAPER_ELIGIBLE_STATUSES,
    SHADOW_ELIGIBLE_STATUSES,
    _static_validation_check,
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
