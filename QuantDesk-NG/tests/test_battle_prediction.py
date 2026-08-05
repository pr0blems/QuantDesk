from __future__ import annotations

import pytest

from quantdesk import battle


def _features(direction: float = 1.0) -> dict[str, float]:
    return {
        "data_quality": 1.0,
        "micro_age_ms": 1_000,
        "positioning_age_ms": 60_000,
        "aggressive_flow": 0.8 * direction,
        "book_imbalance": 0.7 * direction,
        "book_imbalance_5": 0.6 * direction,
        "depth_levels": 100,
        "velocity": 0.6 * direction,
        "flash_imbalance": 0.5 * direction,
        "taker_flow": 0.7 * direction,
        "price_oi_impulse": 0.5 * direction,
        "trend_15m": 0.7 * direction,
        "trend_1h": 0.6 * direction,
        "trend_4h": 0.5 * direction,
        "account_crowding": 0.0,
        "funding_crowding": 0.0,
        "realized_volatility_bps": 12.0,
    }


@pytest.mark.parametrize("horizon", battle.HORIZONS)
def test_battle_prediction_is_three_way_and_explicitly_heuristic(horizon: int) -> None:
    bullish = battle.predict(_features(), horizon)
    bearish = battle.predict(_features(-1), horizon)

    assert bullish["result"] == "long"
    assert bearish["result"] == "short"
    assert bullish["prediction_state"] == "heuristic"
    assert bullish["confidence_label"] in {"low", "medium"}
    assert sum(
        bullish[name]
        for name in ("long_probability", "short_probability", "neutral_probability")
    ) == pytest.approx(1.0)
    assert bullish["long_probability"] > bullish["short_probability"]
    assert bearish["short_probability"] > bearish["long_probability"]


def test_battle_prediction_abstains_when_market_data_is_stale() -> None:
    features = _features()
    features["micro_age_ms"] = 30_000

    result = battle.predict(features, 900)

    assert result["prediction_state"] == "data_insufficient"
    assert result["result"] == "neutral"
    assert result["neutral_probability"] == pytest.approx(0.8)
    assert "DATA_INSUFFICIENT" in result["reason_codes"]


def test_feature_vector_normalizes_flashes_and_price_open_interest() -> None:
    now_ms = 1_800_000
    current = {
        "snapshot_at_ms": now_ms,
        "open_interest": 110,
        "mark_price": 101,
        "global_long_short_ratio": 1.2,
        "taker_buy_sell_ratio": 1.5,
        "quality_json": {
            "open_interest": True,
            "account_ratio": True,
            "taker": True,
        },
    }
    previous = {"open_interest": 100, "mark_price": 100}
    micro = {
        "received_at": now_ms - 1_000,
        "book_imbalance": 0.2,
        "book_imbalance_5": 0.3,
        "depth_levels": 100,
        "aggressive_buy_ratio": 0.6,
        "price_velocity_bps_60s": 4,
        "realized_volatility_60s": 8,
        "spread_bps": 1.5,
    }

    features, quality = battle.build_feature_vector(
        positioning=current,
        previous_positioning=previous,
        microstructure=micro,
        scores={"15m": 50, "1h": 40, "4h": 20},
        up_count=90,
        down_count=30,
        now_ms=now_ms,
    )

    assert quality == 1.0
    assert features["flash_imbalance"] == pytest.approx(0.5)
    assert features["price_oi_impulse"] > 0
    assert features["taker_flow"] > 0


def test_binance_positioning_client_uses_public_tradfi_endpoints(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def fake_get(url: str, **_: object):
        calls.append(url)
        if "/fapi/v1/depth?" in url:
            return {"bids": [["100", "1"]], "asks": [["101", "1"]]}
        return [] if "/futures/data/" in url else {}

    monkeypatch.setattr(battle.binance_client, "_get", fake_get)

    battle.binance_client.fetch_open_interest("aaplusdt")
    battle.binance_client.fetch_global_long_short_ratio("aaplusdt")
    battle.binance_client.fetch_taker_buy_sell_ratio("aaplusdt")
    battle.binance_client.fetch_order_book("aaplusdt", limit=20)

    assert any("/fapi/v1/openInterest?" in url and "AAPLUSDT" in url for url in calls)
    assert any("/futures/data/globalLongShortAccountRatio?" in url for url in calls)
    assert any("/futures/data/takerlongshortRatio?" in url for url in calls)
    assert any("/fapi/v1/depth?" in url and "limit=20" in url for url in calls)
