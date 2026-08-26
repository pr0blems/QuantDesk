from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_ai_monitor_prefers_authenticated_websocket_with_stream_fallback() -> None:
    component = (ROOT / "src/quantdesk_v2/static/ai-monitor.js").read_text(
        encoding="utf-8"
    )
    client = (ROOT / "web/src/api/client.ts").read_text(encoding="utf-8")
    legacy_client = (ROOT / "src/quantdesk_v2/static/app.js").read_text(
        encoding="utf-8"
    )
    entrypoint = (ROOT / "web/src/main.tsx").read_text(encoding="utf-8")

    assert 'this.stream("/events"' in component
    assert 'headers.set("Last-Event-ID"' in component
    assert 'headers.set("Authorization", `Bearer ${accessToken}`)' in client
    assert 'headers.set("Accept", "text/event-stream")' in client
    assert "window.quantdeskApiStream" in entrypoint
    assert "window.quantdeskOpenAiMonitorSocket" in entrypoint
    assert 'new WebSocket(endpoint, [' in client
    assert '"quantdesk.ai-monitor.v1"' in client
    assert '`quantdesk.auth.${accessToken}`' in client
    assert "window.quantdeskOpenAiMonitorSocket" in legacy_client
    assert 'new URL("/api/v2/ai-monitor/ws", window.location.origin)' in legacy_client
    assert '`quantdesk.auth.${accessToken}`' in legacy_client
    assert "this.consumePreferredUpdateStream(controller)" in component
    assert "await this.consumeUpdateWebSocket(controller)" in component
    assert 'this.state.updateStreamTransport = "websocket"' in component
    assert 'this.state.updateStreamTransport = "sse"' in component
    assert "window.setInterval(() => this.loadMarketContext(), 30000)" in component
    assert "window.setInterval(() => this.loadLiveState(), 60000)" in component
    assert "this.updateStreamAbort?.abort()" in component
    assert "this.updateStreamConnectTimer" in component
    assert "connectionTimedOut" in component
    assert "}, 8000);" in component
    assert 'this.state.lastSuccessfulRefreshAt ? "polling" : "reconnecting"' in component
    assert component.count('if (this.state.updateStreamStatus === "connecting") this.state.updateStreamStatus = "polling";') == 2
    assert 'const pipelineInitializing = !this.state.lastSuccessfulRefreshAt' in component
    assert 'this.state.updateStreamStatus === "connected"' in component
    assert '"WS 实时"' in component
    assert '"SSE 实时"' in component
    assert '"REST 轮询降级"' in component


def test_ai_monitor_incremental_stream_never_places_token_in_query_string() -> None:
    component = (ROOT / "src/quantdesk_v2/static/ai-monitor.js").read_text(
        encoding="utf-8"
    )
    client = (ROOT / "web/src/api/client.ts").read_text(encoding="utf-8")
    legacy_client = (ROOT / "src/quantdesk_v2/static/app.js").read_text(
        encoding="utf-8"
    )

    assert 'this.stream("/events", { signal: controller.signal, headers })' in component
    assert "/events?token=" not in component
    assert "/ai-monitor/ws?" not in client
    assert "/ai-monitor/ws?" not in legacy_client
