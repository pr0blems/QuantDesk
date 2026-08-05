"""Forward outcome labels and calibration feedback for every market opportunity."""

from __future__ import annotations

import json
import time
from typing import Any

from . import battle, prediction_validation, store

HORIZONS = (30, 60, 300, 900, 3_600, 14_400)


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except (TypeError, ValueError):
            return {}
        return decoded if isinstance(decoded, dict) else {}
    return {}


def seed_outcomes(limit: int = 500) -> int:
    rows = store.query(
        """SELECT o.id,o.direction,o.entry_price,o.evidence_json,UNIX_TIMESTAMP(o.updated_at) created_ts
           FROM market_opportunities o
           WHERE o.direction IN ('long','short') AND o.current_marker=1
             AND NOT EXISTS (SELECT 1 FROM opportunity_outcomes x WHERE x.opportunity_id=o.id)
           ORDER BY o.id DESC LIMIT ?""",
        (limit,),
    )
    inserted = 0
    for row in rows:
        evidence = _json_object(row["evidence_json"])
        features = evidence.get("features") if isinstance(evidence.get("features"), dict) else {}
        trigger = features.get("15m") if isinstance(features.get("15m"), dict) else {}
        entry = float(row.get("entry_price") or trigger.get("close") or 0)
        if entry <= 0:
            continue
        atr_pct = max(0.15, float(trigger.get("atr_pct") or 0.5))
        target_bps = max(20.0, atr_pct * 100)
        stop_bps = max(15.0, target_bps * 0.7)
        created_ts = int(row.get("created_ts") or time.time())
        context = {
            "scanner_key": evidence.get("scanner_key"),
            "cost_model": "spread_plus_2bps",
            "label_version": 1,
        }
        for horizon in HORIZONS:
            inserted += store.execute(
                """INSERT IGNORE INTO opportunity_outcomes(
                       opportunity_id,horizon_seconds,status,direction,entry_price,target_bps,stop_bps,
                       due_at,cost_bps,context_json,created_at,updated_at)
                   VALUES(?,?,'pending',?,?,?,?,?,2,?,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)""",
                (
                    row["id"],
                    horizon,
                    row["direction"],
                    entry,
                    target_bps,
                    stop_bps,
                    created_ts + horizon,
                    json.dumps(context, ensure_ascii=False),
                ),
            )
    return inserted


def update_pending(limit: int = 2_000) -> dict[str, int]:
    now = int(time.time())
    rows = store.query(
        """SELECT x.id,x.direction,x.entry_price,x.target_bps,x.stop_bps,x.due_at,
                  x.max_favorable_bps,x.max_adverse_bps,t.price,m.spread_bps
           FROM opportunity_outcomes x
           JOIN market_opportunities o ON o.id=x.opportunity_id
           JOIN ticker t ON t.symbol=o.symbol
           LEFT JOIN market_microstructure m ON m.symbol=o.symbol
           WHERE x.status='pending' ORDER BY x.due_at LIMIT ?""",
        (limit,),
    )
    completed = updated = 0
    for row in rows:
        entry = float(row["entry_price"])
        price = float(row["price"])
        raw_bps = (price / entry - 1) * 10_000
        direction_mult = 1 if row["direction"] == "long" else -1
        directional = raw_bps * direction_mult
        favorable = max(float(row.get("max_favorable_bps") or 0), directional)
        adverse = min(float(row.get("max_adverse_bps") or 0), directional)
        target = float(row["target_bps"])
        stop = float(row["stop_bps"])
        hit = "target" if favorable >= target else "stop" if adverse <= -stop else None
        cost = max(2.0, float(row.get("spread_bps") or 0) + 2.0)
        if now >= int(row["due_at"]):
            store.execute(
                """UPDATE opportunity_outcomes SET status='completed',exit_price=?,raw_return_bps=?,
                       directional_return_bps=?,max_favorable_bps=?,max_adverse_bps=?,hit_result=?,
                       completed_at=?,cost_bps=?,updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                (
                    price,
                    raw_bps,
                    directional - cost,
                    favorable,
                    adverse,
                    hit or "neither",
                    now,
                    cost,
                    row["id"],
                ),
            )
            completed += 1
        else:
            store.execute(
                "UPDATE opportunity_outcomes SET max_favorable_bps=?,max_adverse_bps=?,hit_result=COALESCE(hit_result,?),cost_bps=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (favorable, adverse, hit, cost, row["id"]),
            )
            updated += 1
    return {"completed": completed, "updated": updated}


def outcome_loop(stop_event=None) -> None:
    while stop_event is None or not stop_event.is_set():
        try:
            seeded = seed_outcomes()
            result = update_pending()
            battle_result = battle.update_prediction_outcomes()
            validation = prediction_validation.refresh_validation_metrics()
            store.collector_report(
                "outcome_labeler",
                success=True,
                items=seeded + result["completed"] + battle_result["completed"],
                details={"seeded": seeded, **result, "battle": battle_result, "validation": len(validation)},
            )
        except Exception as exc:
            store.collector_report("outcome_labeler", success=False, error=str(exc))
        if stop_event is None:
            time.sleep(5)
        elif stop_event.wait(5):
            break
