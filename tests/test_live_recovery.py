from __future__ import annotations

from quantdesk_v2.application.live_recovery import (
    LiveAccountRecoveryService,
    LiveOrderReconciliationService,
    LiveOrderStatePending,
    LivePositionSyncService,
)


def test_order_reconciliation_resolves_market_and_protection_facts() -> None:
    updates: list[tuple[int, str]] = []
    credential = str(7)
    service = LiveOrderReconciliationService(
        load_intents=lambda *_args: (
            {
                "id": 1,
                "user_id": 7,
                "symbol": "AAPLUSDT",
                "action": "open",
                "client_order_id": "market-1",
            },
            {
                "id": 2,
                "user_id": 7,
                "symbol": "AAPLUSDT",
                "action": "stop",
                "client_order_id": "stop-2",
            },
        ),
        load_open_orders=lambda *_args: (),
        query_market_order=lambda *_args: {"status": "FILLED"},
        query_protection_order=lambda *_args: {"status": "FINISHED"},
        update_intent=lambda intent_id, _user_id, **values: updates.append(
            (intent_id, values["status"])
        ),
        normalize_open_order=dict,
        classify_status=lambda payload: (
            "filled" if payload["status"] in {"FILLED", "FINISHED"} else "unknown"
        ),
        is_order_not_found=lambda _exc: False,
    )

    outcome = service.reconcile(
        user_id=7,
        account_id=11,
        api_key="key",
        api_secret=credential,
    )

    assert outcome.market_state_changed
    assert outcome.reconciled_count == 2
    assert updates == [(1, "filled"), (2, "filled")]


def test_open_market_snapshot_fences_account_until_terminal() -> None:
    credential = str(7)
    service = LiveOrderReconciliationService(
        load_intents=lambda *_args: (
            {
                "id": 1,
                "user_id": 7,
                "symbol": "AAPLUSDT",
                "action": "open",
                "client_order_id": "market-1",
            },
        ),
        load_open_orders=lambda *_args: (
            {"client_order_id": "market-1", "status": "NEW"},
        ),
        query_market_order=lambda *_args: {},
        query_protection_order=lambda *_args: {},
        update_intent=lambda *_args, **_kwargs: None,
        normalize_open_order=dict,
        classify_status=lambda _payload: "submitted",
        is_order_not_found=lambda _exc: False,
    )

    try:
        service.reconcile(
            user_id=7,
            account_id=11,
            api_key="key",
            api_secret=credential,
        )
    except LiveOrderStatePending:
        pass
    else:
        raise AssertionError("non-terminal market intent must fence execution")


def test_position_projection_and_recovery_plan_are_generation_aware() -> None:
    rows = (
        {"id": 31, "symbol": "AAPLUSDT", "position_side": "LONG", "action": "open"},
        {"id": 30, "symbol": "AAPLUSDT", "position_side": "LONG", "action": "close"},
        {"id": 22, "symbol": "MSFTUSDT", "position_side": "SHORT", "action": "close"},
        {"id": 21, "symbol": "MSFTUSDT", "position_side": "SHORT", "action": "open"},
    )
    managed = LivePositionSyncService.managed_positions(rows)

    assert set(managed) == {("AAPLUSDT", "LONG")}
    actions = LiveAccountRecoveryService().plan(
        exchange_positions={
            ("AAPLUSDT", "LONG"): {
                "symbol": "AAPLUSDT",
                "position_side": "LONG",
            }
        },
        managed_positions=managed,
        protection_counts={},
        failed_close_keys=set(),
        grandfathered_keys=set(),
    )

    assert len(actions) == 1
    assert actions[0].kind == "close_and_fail"
    assert actions[0].reason == "recovery_protection_missing"


def test_recovery_records_exchange_close_but_preserves_grandfathered_exposure() -> None:
    managed = {
        ("AAPLUSDT", "LONG"): {"id": 31},
        ("OLDUSDT", "LONG"): {"id": 20},
    }
    actions = LiveAccountRecoveryService().plan(
        exchange_positions={("OLDUSDT", "LONG"): {"symbol": "OLDUSDT"}},
        managed_positions=managed,
        protection_counts={},
        failed_close_keys=set(),
        grandfathered_keys={("OLDUSDT", "LONG")},
    )

    assert [(item.kind, item.key) for item in actions] == [
        ("record_close", ("AAPLUSDT", "LONG"))
    ]
