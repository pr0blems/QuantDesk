from __future__ import annotations

from pathlib import Path

STATIC = Path(__file__).parents[1] / "src" / "quantdesk_v2" / "static"


def test_contract_cards_flash_in_the_price_move_direction() -> None:
    script = (STATIC / "monitor.js").read_text(encoding="utf-8")
    stylesheet = (STATIC / "monitor.css").read_text(encoding="utf-8")

    assert 'current > previous ? "up" : "down"' in script
    assert 'item.priceMove === "up" ? "tick-up"' in script
    assert 'item.priceMove === "down" ? "tick-down"' in script
    assert ".contract-card.tick-up::after { color: var(--m-up); }" in stylesheet
    assert ".contract-card.tick-down::after { color: var(--m-down); }" in stylesheet
    assert "contract-price-pulse 1.5s" in stylesheet
    assert "price-move-fade 1.5s" in stylesheet
    assert "green_flashes_30m" in script
    assert "red_flashes_30m" in script
    assert "多头力量" in script
    assert "空头力量" in script
    assert "多空博弈预测" in script
    assert "启发式未校准" in script
    assert "battle-compact" in stylesheet
    assert "battle-horizon" in stylesheet
    assert "prefers-reduced-motion: reduce" in stylesheet
