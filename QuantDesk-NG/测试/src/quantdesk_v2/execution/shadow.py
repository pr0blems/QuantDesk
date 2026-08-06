"""Deterministic shadow execution adapter; it never sends an exchange order."""

from __future__ import annotations

import json
import time
import uuid

from quantdesk import store


def process_shadow_intents(limit: int = 100) -> int:
    rows = store.query(
        """SELECT i.id,i.public_id,i.user_id,i.exchange_account_id,i.symbol,i.quantity,
                  i.side,t.price,m.spread_bps
           FROM order_intents i
           JOIN exchange_accounts a ON a.id=i.exchange_account_id AND a.user_id=i.user_id
           JOIN ticker t ON t.symbol=i.symbol
           LEFT JOIN market_microstructure m ON m.symbol=i.symbol
           WHERE i.state='approved' AND a.status='shadow'
           ORDER BY i.id LIMIT ?""",
        (limit,),
    )
    processed = 0
    for row in rows:
        spread_bps = max(0.0, float(row.get("spread_bps") or 0))
        half_spread = spread_bps / 20_000
        reference_price = float(row["price"])
        fill_price = reference_price * (
            1 + half_spread if row["side"] == "buy" else 1 - half_spread
        )
        client_order_id = f"qd-shadow-{row['id']}"
        event_key = f"shadow-fill:{row['public_id']}"
        trade_id = f"shadow-{uuid.uuid4().hex}"
        raw = {
            "mode": "shadow",
            "reference_price": reference_price,
            "spread_bps": spread_bps,
            "notice": "No order was sent to Binance.",
        }
        with store.transaction() as tx:
            claimed = tx.execute(
                "UPDATE order_intents SET state='submitting',updated_at=CURRENT_TIMESTAMP WHERE id=? AND state='approved'",
                (row["id"],),
            )
            if not claimed:
                continue
            tx.execute(
                """INSERT INTO exchange_orders(
                       intent_id,user_id,exchange_account_id,exchange_order_id,client_order_id,state,
                       requested_quantity,filled_quantity,average_price,raw_json,submitted_at,updated_at)
                   VALUES(?,?,?,?,?,'filled',?,?,?, ?,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)""",
                (
                    row["id"],
                    row["user_id"],
                    row["exchange_account_id"],
                    trade_id,
                    client_order_id,
                    row["quantity"],
                    row["quantity"],
                    fill_price,
                    json.dumps(raw, ensure_ascii=False),
                ),
            )
            exchange_order_id = int(tx.query("SELECT LAST_INSERT_ID() AS id")[0]["id"])
            tx.execute(
                """INSERT INTO order_events(exchange_order_id,event_key,event_type,previous_state,
                       next_state,payload_json,exchange_ts,received_at)
                   VALUES(?,?,'SHADOW_FILL','submitting','filled',?,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)""",
                (exchange_order_id, event_key, json.dumps(raw, ensure_ascii=False)),
            )
            tx.execute(
                """INSERT INTO fills(exchange_order_id,user_id,exchange_account_id,exchange_trade_id,
                       price,quantity,commission,commission_asset,realized_pnl,filled_at,raw_json)
                   VALUES(?,?,?,?,?,?,0,'USDT',NULL,CURRENT_TIMESTAMP,?)""",
                (
                    exchange_order_id,
                    row["user_id"],
                    row["exchange_account_id"],
                    trade_id,
                    fill_price,
                    row["quantity"],
                    json.dumps(raw, ensure_ascii=False),
                ),
            )
            tx.execute(
                "UPDATE order_intents SET state='filled',updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (row["id"],),
            )
            tx.execute(
                """INSERT IGNORE INTO outbox_events(event_key,aggregate_type,aggregate_id,event_type,
                       payload_json,status,attempts,available_at,created_at)
                   VALUES(?,'order_intent',?,'shadow_order.filled',?,'pending',0,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)""",
                (
                    f"outbox:{event_key}",
                    row["public_id"],
                    json.dumps({"exchange_order_id": exchange_order_id, **raw}, ensure_ascii=False),
                ),
            )
        processed += 1
    return processed


def shadow_loop(stop_event=None) -> None:
    while stop_event is None or not stop_event.is_set():
        try:
            processed = process_shadow_intents()
            store.collector_report("shadow_execution", success=True, items=processed)
        except Exception as exc:
            store.collector_report("shadow_execution", success=False, error=str(exc))
        if stop_event is None:
            time.sleep(2)
        elif stop_event.wait(2):
            break
