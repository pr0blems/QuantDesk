from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from quantdesk_v2.application.ai_monitor import prepare_candidate_news_triggers


def _actionability(item: dict[str, Any]) -> dict[str, Any]:
    actionable = item.get("id") != "recap"
    return {
        "actionable": actionable,
        "reason_code": None if actionable else "CLOSING_RECAP_NOT_A_CATALYST",
    }


def test_prepare_candidate_news_triggers_preserves_admission_evidence() -> None:
    now = datetime(2026, 8, 31, 18, 0)
    now_ts = int(now.replace(tzinfo=UTC).timestamp())
    candidates = [
        {
            "symbol": "AAPL",
            "direction": "long",
            "news": [
                {"id": "fresh", "source": "wire-a", "category": "stocks", "ts": now_ts - 60},
                {"id": "recap", "source": "wire-b", "category": "stocks", "ts": now_ts - 120},
            ],
        },
        {
            "symbol": "MSFT",
            "direction": "short",
            "news": [
                {"id": "used", "source": "wire-c", "category": "stocks", "ts": now_ts - 180},
            ],
        },
    ]

    eligible, skipped = prepare_candidate_news_triggers(
        candidates,
        consumed_by_direction={"short": {"used"}},
        active_candidate_keys=set(),
        require_new_news=True,
        memory_window_hours=168,
        trigger_window_hours=4,
        now=now,
        actionability=_actionability,
    )

    assert [item["symbol"] for item in eligible] == ["AAPL"]
    assert skipped == 1
    assert eligible[0]["news_trigger"] == {
        "version": "fresh_actionable_news_v2",
        "required": True,
        "memory_window_hours": 168,
        "trigger_window_hours": 4,
        "has_new_news": True,
        "new_news_ids": ["fresh", "recap"],
        "has_actionable_new_news": True,
        "actionable_new_news_ids": ["fresh"],
        "non_actionable_news_ids": ["recap"],
        "non_actionable_reasons": ["CLOSING_RECAP_NOT_A_CATALYST"],
        "actionability": {
            "fresh": {"actionable": True, "reason_code": None},
            "recap": {
                "actionable": False,
                "reason_code": "CLOSING_RECAP_NOT_A_CATALYST",
            },
        },
        "reused_news_count": 0,
        "newest_news_age_minutes": 1.0,
    }


def test_prepare_candidate_news_triggers_keeps_active_reused_candidate() -> None:
    candidate = {
        "symbol": "MSFT",
        "direction": "short",
        "news": [{"id": "used", "source": "wire", "category": "stocks", "ts": 1}],
    }

    eligible, skipped = prepare_candidate_news_triggers(
        [candidate],
        consumed_by_direction={"short": {"used"}},
        active_candidate_keys={("MSFT", "short")},
        require_new_news=True,
        memory_window_hours=168,
        trigger_window_hours=4,
        now=datetime(2026, 8, 31, 18, 0),
        actionability=_actionability,
    )

    assert eligible == [candidate]
    assert skipped == 0
    assert candidate["news_trigger"]["has_new_news"] is False
    assert candidate["news_trigger"]["reused_news_count"] == 1
