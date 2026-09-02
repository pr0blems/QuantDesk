from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from quantdesk_v2.application.martingale_tp4.market_gate import (
    BinanceExecutionQuote,
    TigerReferenceQuote,
)
from quantdesk_v2.application.martingale_tp4.shadow import (
    apply_shadow_evaluation,
    basket_from_payload,
    basket_payload,
    evaluate_shadow_tick,
    shadow_idempotency_key,
)
from quantdesk_v2.domain.martingale_tp4 import (
    MartingaleTp4Config,
    Mq4Inputs,
    strategy_parameters_from_mq4,
)
from quantdesk_v2.domain.martingale_tp4_engine import (
    BasketLeg,
    BasketSnapshot,
    DecisionAction,
    Direction,
)

NOW = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)


def _config(inputs: Mq4Inputs | None = None) -> MartingaleTp4Config:
    return MartingaleTp4Config.model_validate(
        {
            "market_data": {
                "underlying_symbol": "AMD",
                "contract_symbol": "AMDUSDT",
                "maximum_tiger_age_seconds": 15,
                "maximum_binance_age_seconds": 5,
                "maximum_clock_skew_seconds": 10,
                "maximum_basis_bps": "500",
            },
            "parameters": strategy_parameters_from_mq4(
                inputs
                or Mq4Inputs(
                    AutoBoxRange=False,
                    MaxSpred="20",
                    Start_Hour=0,
                    End_Hour=0,
                )
            ).model_dump(mode="json"),
            "live_risk": {
                "max_cycle_loss_pct": "2",
                "max_cycle_margin_pct": "20",
                "minimum_liquidation_buffer_pct": "8",
                "daily_loss_limit_pct": "5",
            },
        }
    )


def _tiger(
    bid: str,
    ask: str,
    *,
    observed_at: datetime = NOW,
) -> TigerReferenceQuote:
    return TigerReferenceQuote("AMD", Decimal(bid), Decimal(ask), observed_at)


def _binance(
    bid: str,
    ask: str,
    *,
    observed_at: datetime = NOW,
) -> BinanceExecutionQuote:
    return BinanceExecutionQuote(
        "AMDUSDT",
        Decimal(bid),
        Decimal(ask),
        (Decimal(bid) + Decimal(ask)) / Decimal("2"),
        observed_at,
    )


def test_shadow_uses_tiger_for_breakout_and_binance_for_fill() -> None:
    basket = BasketSnapshot(
        box_high=Decimal("100"),
        box_low=Decimal("90"),
        previous_bid=Decimal("99.99"),
    )
    tiger = _tiger("100.01", "100.02")
    binance = _binance("99.49", "99.50")

    evaluation = evaluate_shadow_tick(
        _config(),
        basket,
        tiger=tiger,
        binance=binance,
        mapping_verified=True,
        point_size=Decimal("0.01"),
        hour=12,
        account_balance=Decimal("10000"),
        deployment_scope="deployment-1",
        event_id="tick-1",
        now=NOW,
    )
    transition = apply_shadow_evaluation(
        evaluation,
        binance=binance,
        tiger_bid=tiger.bid,
    )

    assert evaluation.decision.action == DecisionAction.OPEN
    assert transition.fills[0].price == Decimal("99.50")
    assert transition.basket_after.legs[0].entry_price == Decimal("99.50")
    assert transition.audit_payload()["network_write"] is False


def test_stale_binance_blocks_new_risk() -> None:
    evaluation = evaluate_shadow_tick(
        _config(),
        BasketSnapshot(
            box_high=Decimal("100"),
            box_low=Decimal("90"),
            previous_bid=Decimal("99.99"),
        ),
        tiger=_tiger("100.01", "100.02"),
        binance=_binance(
            "100.00", "100.01", observed_at=NOW - timedelta(seconds=6)
        ),
        mapping_verified=True,
        point_size=Decimal("0.01"),
        hour=12,
        account_balance=Decimal("10000"),
        deployment_scope="deployment-1",
        event_id="tick-2",
        now=NOW,
    )

    assert evaluation.source_decision.action == DecisionAction.OPEN
    assert evaluation.decision.action == DecisionAction.HOLD
    assert evaluation.decision.reason_code == "market_data_gate_blocked_new_risk"
    assert "binance_quote_stale" in evaluation.market_gate.reason_codes


def test_tiger_spread_does_not_replace_binance_execution_spread() -> None:
    evaluation = evaluate_shadow_tick(
        _config(Mq4Inputs(AutoBoxRange=False, MaxSpred="5")),
        BasketSnapshot(
            box_high=Decimal("100"),
            box_low=Decimal("90"),
            previous_bid=Decimal("99.99"),
        ),
        tiger=_tiger("100.01", "101.00"),
        binance=_binance("100.00", "100.01"),
        mapping_verified=True,
        point_size=Decimal("0.01"),
        hour=12,
        account_balance=Decimal("10000"),
        deployment_scope="deployment-1",
        event_id="tick-wide-tiger",
        now=NOW,
    )

    assert evaluation.source_decision.action == DecisionAction.OPEN
    assert evaluation.decision.action == DecisionAction.OPEN


def test_binance_execution_spread_blocks_new_risk() -> None:
    evaluation = evaluate_shadow_tick(
        _config(Mq4Inputs(AutoBoxRange=False, MaxSpred="5")),
        BasketSnapshot(
            box_high=Decimal("100"),
            box_low=Decimal("90"),
            previous_bid=Decimal("99.99"),
        ),
        tiger=_tiger("100.01", "100.02"),
        binance=_binance("99.90", "100.10"),
        mapping_verified=True,
        point_size=Decimal("0.01"),
        hour=12,
        account_balance=Decimal("10000"),
        deployment_scope="deployment-1",
        event_id="tick-wide-binance",
        now=NOW,
    )

    assert evaluation.source_decision.action == DecisionAction.OPEN
    assert evaluation.decision.action == DecisionAction.HOLD
    assert evaluation.decision.reason_code == "binance_execution_spread_too_wide"
    assert evaluation.decision.evidence["execution_spread_points"] == "20"


def test_stale_tiger_never_blocks_a_binance_currency_stop_exit() -> None:
    config = _config(
        Mq4Inputs(
            ChooseTrading="grid",
            SL_Dollar="1",
            AutoBoxRange=False,
            MaxSpred="20",
        )
    )
    basket = BasketSnapshot(
        legs=(
            BasketLeg(
                leg_index=0,
                direction=Direction.BUY,
                quantity=Decimal("1"),
                entry_price=Decimal("100"),
            ),
        )
    )
    tiger = _tiger(
        "98.00", "98.01", observed_at=NOW - timedelta(seconds=60)
    )
    binance = _binance(
        "98.00", "98.01", observed_at=NOW - timedelta(seconds=6)
    )

    evaluation = evaluate_shadow_tick(
        config,
        basket,
        tiger=tiger,
        binance=binance,
        mapping_verified=False,
        point_size=Decimal("0.01"),
        hour=12,
        account_balance=Decimal("10000"),
        deployment_scope="deployment-1",
        event_id="tick-3",
        now=NOW,
    )
    transition = apply_shadow_evaluation(
        evaluation,
        binance=binance,
        tiger_bid=tiger.bid,
    )

    assert evaluation.decision.action == DecisionAction.CLOSE_ALL
    assert evaluation.market_gate.allowed is True
    assert set(evaluation.market_gate.warning_codes) >= {
        "market_mapping_unverified",
        "tiger_quote_stale",
        "binance_quote_stale",
    }
    assert transition.basket_after.legs == ()
    assert transition.realized_pnl_delta == Decimal("-2")


def test_shadow_state_round_trip_and_event_key_are_restart_stable() -> None:
    basket = BasketSnapshot(
        legs=(
            BasketLeg(0, Direction.SELL, Decimal("2"), Decimal("100"), Decimal("1")),
        ),
        box_high=Decimal("105"),
        box_low=Decimal("95"),
        previous_bid=Decimal("99"),
        favorable_high=Decimal("101"),
        favorable_low=Decimal("98"),
    )

    restored = basket_from_payload(basket_payload(basket))
    first = shadow_idempotency_key(
        deployment_scope="deployment-1",
        contract_symbol="AMDUSDT",
        event_id="source-sequence-123",
    )
    second = shadow_idempotency_key(
        deployment_scope="deployment-1",
        contract_symbol="amdusdt",
        event_id="source-sequence-123",
    )

    assert restored == basket
    assert first == second
    assert len(first) < 191


def test_live_cycle_loss_limit_preempts_strategy_addition() -> None:
    config = _config(
        Mq4Inputs(ChooseTrading="grid", SL_Dollar="0", AutoBoxRange=False)
    )
    basket = BasketSnapshot(
        legs=(
            BasketLeg(0, Direction.BUY, Decimal("1"), Decimal("100")),
        )
    )
    tiger = _tiger("97.99", "98.00")
    binance = _binance("98.00", "98.01")

    evaluation = evaluate_shadow_tick(
        config,
        basket,
        tiger=tiger,
        binance=binance,
        mapping_verified=True,
        point_size=Decimal("0.01"),
        hour=12,
        account_balance=Decimal("100"),
        deployment_scope="deployment-1",
        event_id="tick-loss-limit",
        now=NOW,
    )

    assert evaluation.decision.action == DecisionAction.CLOSE_ALL
    assert evaluation.decision.reason_code == "live_cycle_loss_limit"


def test_shadow_addition_can_be_disabled_by_live_risk_without_changing_mq4_logic() -> None:
    config = _config(Mq4Inputs(ChooseTrading="grid", AutoBoxRange=False))
    config = config.model_copy(
        update={
            "live_risk": config.live_risk.model_copy(
                update={"additions_enabled": False}
            )
        }
    )
    basket = BasketSnapshot(
        legs=(
            BasketLeg(0, Direction.BUY, Decimal("0.01"), Decimal("100")),
        )
    )

    evaluation = evaluate_shadow_tick(
        config,
        basket,
        tiger=_tiger("98.49", "98.50"),
        binance=_binance("98.49", "98.50"),
        mapping_verified=True,
        point_size=Decimal("0.01"),
        hour=12,
        account_balance=Decimal("10000"),
        deployment_scope="deployment-1",
        event_id="tick-add-disabled",
        now=NOW,
    )

    assert evaluation.source_decision.action == DecisionAction.ADD
    assert evaluation.decision.action == DecisionAction.HOLD
    assert evaluation.decision.reason_code == "live_additions_disabled"
