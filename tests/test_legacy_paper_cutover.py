from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_paper_mutation_endpoints_cannot_persist_legacy_signal_mode() -> None:
    source = (ROOT / "src/quantdesk_v2/api.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    endpoint_names = {"create_paper_account", "update_paper_account_strategy"}
    endpoints = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in endpoint_names:
            endpoints.append(node)

    assert len(endpoints) == len(endpoint_names)
    string_literals = [
        node.value
        for endpoint in endpoints
        for node in ast.walk(endpoint)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    ]
    assert "legacy_score_v1" not in string_literals
    assert string_literals.count("strategy_event_v2") == len(endpoint_names)


def test_legacy_cutover_migration_is_reversible_and_records_provenance() -> None:
    source = (
        ROOT / "migrations/versions/0076_disable_legacy_paper_signal.py"
    ).read_text(encoding="utf-8")

    assert 'down_revision: str | None = "0075_paper_reconciliation"' in source
    assert "legacy_previous_signal_mode" in source
    assert "legacy_signal_migrated_at" in source
    assert "legacy_signal_cutoff_revision" in source
    assert "strategy_event_v2" in source
    assert "def downgrade()" in source
