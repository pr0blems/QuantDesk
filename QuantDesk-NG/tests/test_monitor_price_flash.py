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
    assert "force-band" in stylesheet
    assert "battle-horizon" in stylesheet
    assert "prefers-reduced-motion: reduce" in stylesheet


def test_contract_cards_use_force_band_layout() -> None:
    script = (STATIC / "monitor.js").read_text(encoding="utf-8")
    stylesheet = (STATIC / "monitor.css").read_text(encoding="utf-8")
    index = (STATIC / "index.html").read_text(encoding="utf-8")

    assert 'class="band-card-head"' in script
    assert 'class="force-band ${forceTone}"' in script
    assert 'class="force-bar"' in script
    assert 'class="force-labels"' in script
    assert 'class="band-meta"' in script
    assert "longForce / totalForce * 100" in script
    assert 'item.battle?.["15m"]' in script
    assert "多头 ${longForce}" in script
    assert "空头 ${shortForce}" in script
    assert "暂无波动" in script
    assert "grid-template-columns: repeat(auto-fill, minmax(210px, 1fr))" in stylesheet
    assert ".force-bar rect.long { fill: var(--m-up); }" in stylesheet
    assert ".force-bar rect.short { fill: var(--m-down); }" in stylesheet
    assert ".band-meta { display: grid; grid-template-columns: repeat(3" in stylesheet
    assert "<svg class=\"force-bar\"" in script
    assert 'style="width:' not in script
    assert "@container (min-width: 270px)" in stylesheet
    assert "@container (min-width: 340px)" in stylesheet
    assert "monitor.js?v=20260804-14" in index


def test_watchlist_import_and_pinning_are_visible_and_persistent() -> None:
    script = (STATIC / "monitor.js").read_text(encoding="utf-8")
    stylesheet = (STATIC / "monitor.css").read_text(encoding="utf-8")

    assert 'id="btn-import-watchlist"' in script
    assert 'id="watchlist-import-input"' in script
    assert 'id="watchlist-import-merge"' in script
    assert 'id="watchlist-import-replace"' in script
    assert 'aliases.set(symbol.slice(0, -4), symbol)' in script
    assert '.split(/[\\s,;]+/)' in script
    assert 'method: "PUT"' in script
    assert 'this.state.watchlist = new Set(Array.isArray(saved)' in script
    assert 'if (left.watch !== right.watch) return right.watch ? 1 : -1' in script
    assert 'class="card-watch ${item.watch ? "on" : ""}"' in script
    assert 'event.stopPropagation()' in script
    assert '暂无本地自选合约' in script
    assert ".contract-card.is-watch" in stylesheet
    assert ".card-watch.on" in stylesheet
    assert ".watchlist-import-box" in stylesheet
