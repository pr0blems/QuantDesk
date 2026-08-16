"""Repair legacy hard-cap prediction settlements without rewriting their horizon.

The old settlement path used the final closed 15-minute candle *before* the
configured due time.  That shortened every hard-cap position and could select
a price from before entry for a 15-minute prediction.  This tool preserves each
record's frozen due_at, uses the first executable candle open at/after due_at,
recalculates outcome/cost/path fields, and writes an audit marker.

Dry-run is the default.  Pass ``--apply`` to back up and commit the repair.
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import uuid4

from sqlalchemy import select

from quantdesk_v2.ai_monitor import (
    _TIMEFRAME_SECONDS,
    PREDICTION_FEE_BPS_PER_SIDE,
    PREDICTION_FUNDING_BPS_PER_8H,
    PREDICTION_SETTLEMENT_VERSION,
    PREDICTION_SLIPPAGE_BPS_PER_SIDE,
    _datetime_ms,
    historical_closed_settlement_price,
    prediction_cost_breakdown,
    prediction_estimated_cost_bps,
    prediction_net_outcome,
    prediction_outcome,
    prediction_path_metrics,
    utcnow,
)
from quantdesk_v2.config import get_settings
from quantdesk_v2.database import SessionLocal, engine
from quantdesk_v2.models import AiMonitorPrediction
from quantdesk_v2.monitor import MonitorRepository


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="commit the repair")
    parser.add_argument("--user-id", type=int, help="limit repair to one tenant")
    parser.add_argument(
        "--backup-dir",
        type=Path,
        default=Path("backups"),
        help="directory for the pre-repair JSON backup",
    )
    return parser.parse_args()


def json_default(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.replace(tzinfo=UTC).isoformat()
    if isinstance(value, Decimal):
        return str(value)
    raise TypeError(f"unsupported backup value: {type(value)!r}")


def row_backup(item: AiMonitorPrediction) -> dict[str, Any]:
    return {
        column.name: getattr(item, column.name)
        for column in AiMonitorPrediction.__table__.columns
    }


def frozen_cost_config(evidence: dict[str, Any]) -> dict[str, Any]:
    source = evidence.get("cost_model")
    source = dict(source) if isinstance(source, dict) else {}
    return {
        "prediction_fee_enabled": bool(source.get("fee_enabled", True)),
        "prediction_fee_bps_per_side": float(
            source.get("fee_bps_per_side", PREDICTION_FEE_BPS_PER_SIDE)
        ),
        "prediction_slippage_enabled": bool(
            source.get("slippage_enabled", True)
        ),
        "prediction_slippage_bps_per_side": float(
            source.get(
                "slippage_bps_per_side", PREDICTION_SLIPPAGE_BPS_PER_SIDE
            )
        ),
        "prediction_funding_enabled": bool(source.get("funding_enabled", True)),
        "prediction_funding_bps_per_8h": float(
            source.get(
                "funding_bps_per_8h", PREDICTION_FUNDING_BPS_PER_8H
            )
        ),
    }


def legacy_holding_marker(item: AiMonitorPrediction) -> dict[str, Any]:
    seconds = max(1, round((item.due_at - item.predicted_at).total_seconds()))
    timeframe_seconds = _TIMEFRAME_SECONDS.get(item.timeframe, 3600)
    return {
        "version": "legacy_frozen_horizon_v1",
        "bars": max(1, round(seconds / timeframe_seconds)),
        "timeframe": item.timeframe,
        "seconds": seconds,
        "due_at": item.due_at.replace(tzinfo=UTC).isoformat(),
    }


def load_candles(
    repository: MonitorRepository,
    rows: list[AiMonitorPrediction],
) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[AiMonitorPrediction]] = defaultdict(list)
    for item in rows:
        grouped[item.contract_symbol].append(item)
    result: dict[str, list[dict[str, Any]]] = {}
    interval_ms = 15 * 60 * 1_000
    for symbol, items in grouped.items():
        result[symbol] = repository.kline_range(
            symbol,
            "15m",
            min(_datetime_ms(item.predicted_at) for item in items) - interval_ms,
            max(_datetime_ms(item.due_at) for item in items) + interval_ms * 2,
        )
    return result


def main() -> int:
    args = parse_args()
    repository = MonitorRepository(engine, get_settings().monitor_symbols_config)
    batch_id = f"max-holding-repair-{datetime.now(UTC):%Y%m%dT%H%M%SZ}-{uuid4().hex[:8]}"

    with SessionLocal() as db:
        statement = select(AiMonitorPrediction).where(
            AiMonitorPrediction.status == "completed"
        )
        if args.user_id is not None:
            statement = statement.where(AiMonitorPrediction.user_id == args.user_id)
        if args.apply:
            statement = statement.with_for_update()
        completed = list(db.scalars(statement.order_by(AiMonitorPrediction.id)).all())
        legacy = [
            item
            for item in completed
            if not isinstance((item.evidence_json or {}).get("max_holding"), dict)
        ]
        targets = [item for item in legacy if item.exit_reason == "max_holding_time"]
        candles_by_symbol = load_candles(repository, targets)

        settlements: dict[int, dict[str, Any]] = {}
        for item in targets:
            settlement = historical_closed_settlement_price(
                candles_by_symbol.get(item.contract_symbol, []),
                _datetime_ms(item.due_at),
                not_before_ms=_datetime_ms(item.predicted_at),
            )
            if settlement is None:
                raise RuntimeError(
                    f"causal settlement market data missing for prediction {item.id}"
                )
            if int(settlement["price_time_ms"]) < _datetime_ms(item.due_at):
                raise RuntimeError(f"prediction {item.id} would still exit before due_at")
            if int(settlement["price_time_ms"]) < _datetime_ms(item.predicted_at):
                raise RuntimeError(f"prediction {item.id} would exit before entry")
            settlements[item.id] = settlement

        changed = sum(
            item.exit_at is None
            or int(item.exit_at.replace(tzinfo=UTC).timestamp() * 1_000)
            != int(settlements[item.id]["price_time_ms"])
            or abs(float(item.exit_price or 0) - float(settlements[item.id]["price"]))
            > 1e-12
            for item in targets
        )
        summary = {
            "batch_id": batch_id,
            "mode": "apply" if args.apply else "dry-run",
            "completed_rows": len(completed),
            "legacy_rows_tagged": len(legacy),
            "hard_cap_rows_repaired": len(targets),
            "changed_settlements": changed,
            "missing_market_rows": 0,
            "user_id": args.user_id,
        }
        if not args.apply:
            print(json.dumps(summary, ensure_ascii=False, indent=2))
            return 0

        args.backup_dir.mkdir(parents=True, exist_ok=True)
        backup_path = args.backup_dir / f"{batch_id}.json"
        backup_payload = {
            "repair": summary,
            "created_at": datetime.now(UTC),
            "rows": [row_backup(item) for item in legacy],
        }
        backup_path.write_text(
            json.dumps(
                backup_payload,
                ensure_ascii=False,
                indent=2,
                default=json_default,
            ),
            encoding="utf-8",
        )

        repaired_at = utcnow()
        for item in legacy:
            evidence = dict(item.evidence_json or {})
            evidence["max_holding"] = legacy_holding_marker(item)
            if item.id in settlements:
                settlement = settlements[item.id]
                exit_price = float(settlement["price"])
                exit_at = datetime.fromtimestamp(
                    int(settlement["price_time_ms"]) / 1_000,
                    UTC,
                ).replace(tzinfo=None)
                outcome = prediction_outcome(
                    float(item.entry_price or 0), exit_price, item.direction
                )
                cost_config = frozen_cost_config(evidence)
                estimated_cost = prediction_estimated_cost_bps(
                    item.predicted_at, exit_at, cost_config
                )
                net_outcome = prediction_net_outcome(
                    float(outcome["directional_return_bps"]), estimated_cost
                )
                path_metrics = prediction_path_metrics(
                    candles_by_symbol.get(item.contract_symbol, []),
                    float(item.entry_price or 0),
                    item.direction,
                    _datetime_ms(item.predicted_at),
                    int(settlement["price_time_ms"]),
                )
                previous_settlement = evidence.get("settlement")
                previous_settlement = (
                    dict(previous_settlement)
                    if isinstance(previous_settlement, dict)
                    else {}
                )
                repair_audit = {
                    "batch_id": batch_id,
                    "repaired_at": repaired_at.replace(tzinfo=UTC).isoformat(),
                    "reason": "legacy_hard_cap_used_pre_due_candle",
                    "previous_exit_at": (
                        item.exit_at.replace(tzinfo=UTC).isoformat()
                        if item.exit_at is not None
                        else None
                    ),
                    "previous_exit_price": (
                        float(item.exit_price) if item.exit_price is not None else None
                    ),
                    "previous_price_source": previous_settlement.get("price_source"),
                    "horizon_preserved": True,
                }
                evidence["settlement"] = {
                    **previous_settlement,
                    "version": PREDICTION_SETTLEMENT_VERSION,
                    "exit_reason": "max_holding_time",
                    "exit_subreason": None,
                    "exit_at": exit_at.replace(tzinfo=UTC).isoformat(),
                    "exit_price": exit_price,
                    "price_source": settlement["price_source"],
                    "cost_breakdown": prediction_cost_breakdown(
                        item.predicted_at, exit_at, cost_config
                    ),
                    "policy": "causal_first_executable_open_after_frozen_hard_cap",
                    "repair": repair_audit,
                }
                item.exit_price = Decimal(str(exit_price))
                item.exit_at = exit_at
                item.exit_reason = "max_holding_time"
                item.result = str(outcome["result"])
                item.raw_return_bps = Decimal(str(outcome["raw_return_bps"]))
                item.directional_return_bps = Decimal(
                    str(outcome["directional_return_bps"])
                )
                item.estimated_cost_bps = Decimal(
                    str(net_outcome["estimated_cost_bps"])
                )
                item.net_directional_return_bps = Decimal(
                    str(net_outcome["net_directional_return_bps"])
                )
                item.net_result = str(net_outcome["net_result"])
                item.max_favorable_bps = (
                    Decimal(str(path_metrics["max_favorable_bps"]))
                    if path_metrics["max_favorable_bps"] is not None
                    else None
                )
                item.max_adverse_bps = (
                    Decimal(str(path_metrics["max_adverse_bps"]))
                    if path_metrics["max_adverse_bps"] is not None
                    else None
                )
                item.settlement_version = PREDICTION_SETTLEMENT_VERSION
            item.evidence_json = evidence
            item.updated_at = repaired_at

        db.commit()
        summary["backup_path"] = str(backup_path.resolve())
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
