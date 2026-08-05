from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from quantdesk import battle, news, news_intelligence
from quantdesk.macro_events import classify_official_event, extract_structured_event


def test_legacy_sentiment_uses_token_boundaries() -> None:
    assert news.sentiment_of("Apple seeks injunction against OpenAI") == "bear"
    assert not news._contains_sentiment_term("against", "gain")


def test_entity_matching_does_not_confuse_gold_with_goldman() -> None:
    entities = news_intelligence.assess_entities(
        "Goldman Sachs-backed Attovia files for an IPO",
        "corporate_action",
        {"XAUUSDT": ["gold"], "GSUSDT": ["goldman sachs"]},
    )
    assert [item.symbol for item in entities] == ["GSUSDT"]


def test_ambiguous_treasury_requires_government_bond_context() -> None:
    aliases = {"TBTUSDT": ["treasury"]}
    assert not news_intelligence.assess_entities(
        "WLFI treasury moves 110m to a crypto wallet", "other", aliases
    )
    entities = news_intelligence.assess_entities(
        "US Treasury yields surge after auction", "macro_release", aliases
    )
    assert entities[0].relationship_type == "exposure"
    assert entities[0].directness < 0.7


def test_comparative_statement_has_target_specific_direction() -> None:
    entities = news_intelligence.assess_entities(
        "Broker favors Z.ai over Minimax after product review",
        "product",
        {"ZAIUSDT": ["z.ai"], "MINIMAXUSDT": ["minimax"]},
    )
    directions = {item.symbol: item.direction for item in entities}
    assert directions == {"ZAIUSDT": "long", "MINIMAXUSDT": "short"}


def test_lawsuit_roles_do_not_make_both_parties_bearish() -> None:
    entities = news_intelligence.assess_entities(
        "Apple seeks injunction against OpenAI in trade secrets case",
        "regulatory",
        {"AAPLUSDT": ["apple"], "OPENAIUSDT": ["openai"]},
    )
    directions = {item.symbol: item.direction for item in entities}
    assert directions == {"AAPLUSDT": "neutral", "OPENAIUSDT": "short"}


def test_rumor_language_caps_impact_confidence() -> None:
    entities = news_intelligence.assess_entities(
        "Sources say Acme may win contract",
        "product",
        {"ACMEUSDT": ["acme"]},
    )
    assert entities[0].direction == "long"
    assert entities[0].impact_confidence <= 0.42
    assert news_intelligence.claim_modality("Sources say Acme may win contract") == "rumor"


def test_source_tiers_prefer_primary_evidence() -> None:
    assert news_intelligence.source_tier("SEC Filings", "https://www.sec.gov/a")[0] == "S"
    assert news_intelligence.source_tier("Reuters", "https://www.reuters.com/a")[0] == "A"
    assert news_intelligence.source_tier("Blog", "https://www.coindesk.com/a")[0] == "C"


def test_structured_macro_event_requires_explicit_labels() -> None:
    event = extract_structured_event(
        "CPI release 2026-08-06 12:30 UTC actual: 3.1% consensus: 3.0%",
        published_at=1_754_482_200,
        source="BLS Latest Releases",
        affected_symbols=["BTCUSDT", "btcusdt"],
    )
    assert event.event_type == "macro_release"
    assert event.actual_value == 3.1
    assert event.consensus_value == 3.0
    assert event.surprise_value == pytest.approx(0.1)
    assert event.actual_at == 1_754_482_200
    assert event.scheduled_at is not None
    assert event.affected_symbols == ("BTCUSDT",)

    unlabeled = extract_structured_event("CPI rises 3.1%", source="BLS")
    assert unlabeled.actual_value is None
    assert unlabeled.consensus_value is None


def test_official_event_classifier_distinguishes_energy_and_policy() -> None:
    assert classify_official_event("FOMC statement: target range unchanged") == "monetary_policy"
    assert classify_official_event("Petroleum Status Report", source="EIA") == "energy_release"


def test_title_clustering_is_broad_but_not_unrelated() -> None:
    assert news_intelligence.title_similarity(
        "Nvidia raises revenue guidance after AI demand",
        "Nvidia raises guidance as AI demand lifts revenue",
    ) >= 0.55
    assert news_intelligence.title_similarity(
        "Nvidia raises revenue guidance",
        "Federal Reserve keeps rates unchanged",
    ) == 0


def test_news_feature_is_explicitly_shadow_only() -> None:
    features = {
        "data_quality": 1.0,
        "micro_age_ms": 0,
        "positioning_age_ms": 0,
        "verified_event_pressure": 1.0,
    }
    baseline = battle.predict(features, 900)
    features["verified_event_pressure"] = -1.0
    opposite = battle.predict(features, 900)
    assert baseline["battle_score"] == opposite["battle_score"]
    assert baseline["components"]["news_weight"] == 0.0


def test_news_intelligence_migration_follows_battle_head() -> None:
    migration_path = (
        Path(__file__).parents[1]
        / "migrations"
        / "versions"
        / "0024_news_intelligence_verification.py"
    )
    spec = importlib.util.spec_from_file_location("news_intelligence_migration_0024", migration_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.revision == "0024_news_intelligence"
    assert module.down_revision == "0023_battle_prediction"


def test_news_outcome_migration_follows_intelligence_head() -> None:
    migration_path = (
        Path(__file__).parents[1]
        / "migrations"
        / "versions"
        / "0025_news_outcome_calibration.py"
    )
    spec = importlib.util.spec_from_file_location("news_outcome_migration_0025", migration_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.revision == "0025_news_outcomes"
    assert module.down_revision == "0024_news_intelligence"


@pytest.mark.parametrize("invalid", ["", "not a date", "2026-99-99"])
def test_invalid_publication_time_never_becomes_current(invalid: str) -> None:
    assert news.parse_pub(invalid) == 0
