"""Versioned market-feature access boundary for AI Monitor."""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from .contracts import AiMonitorAuthority, AiMonitorStageResult


def _finite_number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def realtime_feature_payload(snapshot: Any | Mapping[str, Any] | None) -> dict[str, Any]:
    """Normalize a persisted feature snapshot without inventing absent domains."""

    if snapshot is None:
        return {}
    if isinstance(snapshot, Mapping):
        return dict(snapshot)
    quote = dict(snapshot.quote_snapshot_json or {})
    typed_quote = {
        "last_price": _finite_number(snapshot.last_price),
        "bid": _finite_number(snapshot.bid),
        "ask": _finite_number(snapshot.ask),
        "spread_bps": _finite_number(snapshot.spread_bps),
        "quote_age_ms": (
            int(snapshot.quote_age_ms) if snapshot.quote_age_ms is not None else None
        ),
        "size_imbalance": _finite_number(snapshot.size_imbalance),
        "market_session": snapshot.market_session,
    }
    quote.update({key: value for key, value in typed_quote.items() if value is not None})
    return {
        "id": snapshot.id,
        "symbol": snapshot.symbol,
        "bucket_at": snapshot.bucket_at.isoformat(),
        "captured_at": snapshot.captured_at.isoformat(),
        "quote": quote,
        "option_flow": dict(snapshot.option_flow_snapshot_json or {}),
        "gex": dict(snapshot.gex_snapshot_json or {}),
        "institutional_flow": dict(snapshot.institutional_flow_snapshot_json or {}),
        "halt_status": snapshot.halt_status,
        "data_coverage": _finite_number(snapshot.data_coverage) or 0.0,
        "stale_fields": list(snapshot.stale_fields_json or []),
        "quality": dict(snapshot.quality_json or {}),
        "feature_version": snapshot.feature_version,
    }


class MarketFeatureService:
    def __init__(
        self,
        *,
        latest: Callable[..., dict[str, Any]],
        normalize: Callable[[Any], dict[str, Any]],
        version: str,
    ) -> None:
        self._latest = latest
        self._normalize = normalize
        self._version = version

    def latest(self, db: Any, symbols: Sequence[str]) -> AiMonitorStageResult:
        rows = self._latest(db, symbols)
        return AiMonitorStageResult(
            stage="market_features",
            authority=AiMonitorAuthority.DETERMINISTIC,
            version=self._version,
            payload={"items": rows},
        )

    def normalize(self, value: Any) -> dict[str, Any]:
        return self._normalize(value)
