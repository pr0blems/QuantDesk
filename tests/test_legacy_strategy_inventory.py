from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_legacy_inventory_is_read_only_and_defaults_to_dry_run() -> None:
    source = (ROOT / "scripts/audit_legacy_strategy_data.py").read_text(
        encoding="utf-8"
    )

    assert '"mode": "dry-run"' in source
    assert '"read_only": True' in source
    assert "db.rollback()" in source
    assert "db.commit()" not in source
    assert "delete(" not in source
    assert "update(" not in source
    assert "insert(" not in source


def test_new_paper_mutations_never_select_the_legacy_signal_mode() -> None:
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
