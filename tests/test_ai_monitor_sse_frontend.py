from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_ai_monitor_uses_authenticated_incremental_stream_with_polling_fallback() -> None:
    component = (ROOT / "src/quantdesk_v2/static/ai-monitor.js").read_text(
        encoding="utf-8"
    )
    client = (ROOT / "web/src/api/client.ts").read_text(encoding="utf-8")
    entrypoint = (ROOT / "web/src/main.tsx").read_text(encoding="utf-8")

    assert 'this.stream("/events"' in component
    assert 'headers.set("Last-Event-ID"' in component
    assert 'headers.set("Authorization", `Bearer ${accessToken}`)' in client
    assert 'headers.set("Accept", "text/event-stream")' in client
    assert "window.quantdeskApiStream" in entrypoint
    assert "window.setInterval(() => this.loadMarketContext(), 30000)" in component
    assert "window.setInterval(() => this.loadLiveState(), 60000)" in component
    assert "this.updateStreamAbort?.abort()" in component
    assert "this.updateStreamConnectTimer" in component
    assert "connectionTimedOut" in component
    assert "}, 8000);" in component
    assert 'this.state.lastSuccessfulRefreshAt ? "polling" : "reconnecting"' in component
    assert 'this.state.updateStreamStatus === "connected"' in component
    assert '"页面增量推送在线"' in component
    assert '"REST 轮询降级"' in component


def test_ai_monitor_incremental_stream_never_places_token_in_query_string() -> None:
    component = (ROOT / "src/quantdesk_v2/static/ai-monitor.js").read_text(
        encoding="utf-8"
    )

    assert 'this.stream("/events", { signal: controller.signal, headers })' in component
    assert "/events?token=" not in component
