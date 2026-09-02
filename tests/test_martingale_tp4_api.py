from __future__ import annotations

from quantdesk_v2.interfaces.api.martingale_tp4 import router


def test_martingale_control_plane_exposes_research_routes_without_trade_route() -> None:
    paths = {route.path for route in router.routes}

    assert "/api/v2/basket-strategies/martingale-tp4/validate" in paths
    assert "/api/v2/basket-strategies/martingale-tp4/mq4/import" in paths
    assert "/api/v2/basket-strategies/martingale-tp4/mq4/csv/import" in paths
    assert "/api/v2/basket-strategies/martingale-tp4/mq4/export" in paths
    assert "/api/v2/basket-strategies/martingale-tp4/backtests" in paths
    assert "/api/v2/basket-strategies/martingale-tp4/tiger-bars/backfill" in paths
    assert "/api/v2/basket-strategies/martingale-tp4/shadow/cycles" in paths
    assert "/api/v2/basket-strategies/martingale-tp4/shadow/cycles/{cycle_id}" in paths
    assert not any(path.endswith(("/execute", "/order", "/arm")) for path in paths)
