from __future__ import annotations

import json

from quantdesk_v2 import cli


class _ProjectionStore:
    def audit_account(self, *, user_id: int, paper_account_id: int):
        if paper_account_id == 2:
            return 1, ("paper_balance_drift",), ()
        return 0, (), ("paper_equity_projection_stale",)


class _Settings:
    @staticmethod
    def validate_runtime() -> None:
        return None


def test_audit_paper_is_read_only_and_reports_blocked_accounts(monkeypatch, capsys) -> None:
    monkeypatch.setattr(cli, "get_settings", lambda: _Settings())
    monkeypatch.setattr(
        "quantdesk_v2.market_store.query",
        lambda _sql, _params=(): [
            {"id": 1, "user_id": 7, "name": "one"},
            {"id": 2, "user_id": 8, "name": "two"},
        ],
    )
    monkeypatch.setattr(
        "quantdesk_v2.infrastructure.persistence.paper_projections.MySqlPaperProjectionStore",
        lambda _backend: _ProjectionStore(),
    )

    result = cli.audit_paper(None, json_output=True)
    payload = json.loads(capsys.readouterr().out)

    assert result == 4
    assert payload["blocked"] is True
    assert payload["accounts"][0]["warning_codes"] == [
        "paper_equity_projection_stale"
    ]
    assert payload["accounts"][1]["drift_codes"] == ["paper_balance_drift"]


def test_audit_paper_reports_missing_requested_account(monkeypatch, capsys) -> None:
    monkeypatch.setattr(cli, "get_settings", lambda: _Settings())
    monkeypatch.setattr("quantdesk_v2.market_store.query", lambda _sql, _params=(): [])

    result = cli.audit_paper(999)

    assert result == 3
    assert "was not found" in capsys.readouterr().err


def test_reconcile_paper_requires_an_explicit_confirmation(capsys) -> None:
    result = cli.reconcile_paper(1)

    assert result == 2
    assert "requires --confirm" in capsys.readouterr().err
