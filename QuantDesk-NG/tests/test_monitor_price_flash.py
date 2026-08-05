from __future__ import annotations

from pathlib import Path

STATIC = Path(__file__).parents[1] / "src" / "quantdesk_v2" / "static"


def test_contract_matrix_emphasizes_ai_direction_and_live_price_moves() -> None:
    script = (STATIC / "monitor.js").read_text(encoding="utf-8")
    stylesheet = (STATIC / "monitor.css").read_text(encoding="utf-8")

    assert 'current > previous ? "up" : "down"' in script
    assert 'item.priceMove === "up" ? "tick-up"' in script
    assert 'item.priceMove === "down" ? "tick-down"' in script
    assert 'class="matrix-row matrix-${ai.tone}' in script
    assert "item.pct_2m" in script
    assert "item.pct_5m" in script
    assert "item.pct_10m" in script
    assert 'label: { long: "看多", short: "看空", neutral: "观望" }[tone]' in script
    assert ".matrix-row.matrix-long" in stylesheet
    assert ".matrix-row.matrix-short" in stylesheet
    assert ".matrix-row.matrix-neutral" in stylesheet
    assert ".matrix-row.tick-up td:nth-child(5)" in stylesheet
    assert ".matrix-row.tick-down td:nth-child(5)" in stylesheet
    assert "green_flashes_30m" in script
    assert "red_flashes_30m" in script
    assert "bid_depth_notional" in script
    assert "ask_depth_notional" in script
    assert '"100档订单池"' in script
    assert "最近 30 分钟价格方向高亮" in script
    assert "多空博弈预测" in script
    assert "启发式未校准" in script
    assert "battle-horizon" in stylesheet
    assert "prefers-reduced-motion: reduce" in stylesheet


def test_contract_matrix_is_ai_first_and_every_visible_indicator_sorts() -> None:
    script = (STATIC / "monitor.js").read_text(encoding="utf-8")
    stylesheet = (STATIC / "monitor.css").read_text(encoding="utf-8")
    index = (STATIC / "index.html").read_text(encoding="utf-8")

    assert 'matrixSort: { key: "ai", direction: "desc" }' in script
    assert 'data-matrix-sort="${key}"' in script
    assert 'class="contract-matrix"' in script
    assert '"AI 结论"' in script
    assert '"信心"' in script
    assert '"2m 涨跌"' in script
    assert '"5m 涨跌"' in script
    assert '"10m 涨跌"' in script
    assert '"30m 力量"' in script
    assert '"100档订单池"' in script
    assert '"成交额"' in script
    assert "matrixSortValue(item, key)" in script
    assert "longForce / totalForce * 100" in script
    assert 'item.battle?.["5m"]' in script
    assert "item.annotation || item.underlying" in script
    assert "contract-matrix { width: 100%; min-width: 1380px" in stylesheet
    assert ".matrix-confidence" in stylesheet
    assert ".matrix-force" in stylesheet
    assert ".matrix-book" in stylesheet
    assert "monitor.js?v=20260805-10" in index
    assert "monitor.css?v=20260805-10" in script


def test_watchlist_clear_and_position_sync_are_visible_and_persistent() -> None:
    script = (STATIC / "monitor.js").read_text(encoding="utf-8")
    stylesheet = (STATIC / "monitor.css").read_text(encoding="utf-8")

    assert 'id="btn-clear-watchlist"' in script
    assert 'id="btn-sync-positions"' in script
    assert "async clearWatchlist()" in script
    assert "await this.saveWatchlist(new Set())" in script
    assert "window.confirm" in script
    assert "async syncPositionsToMonitor()" in script
    assert 'window.quantdeskApi("/api/v2/me/binance-account")' in script
    assert "Array.isArray(account.positions)" in script
    assert 'method: "PUT"' in script
    assert 'this.state.watchlist = new Set(Array.isArray(saved)' in script
    assert 'this.syncBinanceWatchlist();' not in script
    assert 'setInterval(() => this.syncBinanceWatchlist(), 60000)' not in script
    assert 'if (key === "watch") return item.watch ? 1 : 0;' in script
    assert "const pinnedComparison = Number(Boolean(right.watch)) - Number(Boolean(left.watch));" in script
    assert "if (pinnedComparison !== 0) return pinnedComparison;" in script
    assert '["watch", "持仓/自选"]' in script
    assert 'class="matrix-watch ${item.watch ? "on" : ""}"' in script
    assert 'event.stopPropagation()' in script
    assert '暂无自选合约' in script
    assert ".matrix-watch.on" in stylesheet
    assert ".filters .watchlist-action" in stylesheet
