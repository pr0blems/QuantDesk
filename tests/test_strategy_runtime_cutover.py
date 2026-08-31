from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_runtime_audit_is_read_only_and_checks_retired_markers() -> None:
    source = (ROOT / "scripts/audit_strategy_runtime_data.py").read_text(
        encoding="utf-8"
    )

    assert '"mode": "dry-run"' in source
    assert '"read_only": True' in source
    assert "retired_runtime_markers" in source
    assert "db.rollback()" in source
    assert "db.commit()" not in source
    assert "delete(" not in source
    assert "update(" not in source
    assert "insert(" not in source


def test_current_paper_mutations_only_write_strategy_event_mode() -> None:
    source = (
        ROOT / "src/quantdesk_v2/interfaces/api/trading_accounts.py"
    ).read_text(encoding="utf-8")
    create_block = source[
        source.index("def create_paper_account(") : source.index(
            "def update_paper_account_status("
        )
    ]

    assert '"signal_mode": "strategy_event_v2"' in create_block
    assert '"signal_mode": "legacy_score_v1"' not in create_block


def test_builtin_cutover_physically_removes_retired_runtime_markers() -> None:
    source = (
        ROOT / "migrations/versions/0080_builtin_strategy_cutover.py"
    ).read_text(encoding="utf-8")

    assert 'down_revision: str | None = "0079_position_snapshot_facts"' in source
    assert "UPDATE strategy_templates" in source
    assert "UPDATE user_strategies" in source
    assert "UPDATE strategy_revisions" in source
    assert "UPDATE paper_accounts" in source
    assert "UPDATE live_trading_accounts" in source
    assert "JSON_REMOVE" in source
    assert "include_legacy=False" in source
    assert "cannot be downgraded" in source
