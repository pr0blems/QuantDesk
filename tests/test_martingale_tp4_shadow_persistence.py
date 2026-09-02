from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from quantdesk_v2.application.martingale_tp4.market_gate import (
    BinanceExecutionQuote,
    TigerReferenceQuote,
)
from quantdesk_v2.application.martingale_tp4.shadow import (
    apply_shadow_evaluation,
    evaluate_shadow_tick,
)
from quantdesk_v2.domain.martingale_tp4 import (
    MartingaleTp4Config,
    Mq4Inputs,
    strategy_parameters_from_mq4,
)
from quantdesk_v2.domain.martingale_tp4_engine import BasketSnapshot, DecisionAction
from quantdesk_v2.infrastructure.persistence.martingale_tp4 import (
    record_shadow_transition,
    restore_shadow_basket,
)
from quantdesk_v2.models import (
    StrategyBasketEvent,
    StrategyBasketLeg,
    StrategyDeployment,
    StrategySignal,
    User,
    utcnow,
)
from quantdesk_v2.strategy_catalog import ensure_user_default_strategies


def _config() -> MartingaleTp4Config:
    return MartingaleTp4Config.model_validate(
        {
            "market_data": {
                "underlying_symbol": "AMD",
                "contract_symbol": "AMDUSDT",
                "maximum_basis_bps": "500",
            },
            "parameters": strategy_parameters_from_mq4(
                Mq4Inputs(
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


def test_shadow_ledger_recovers_cycle_and_deduplicates_tick(
    mysql_test_engine: Engine,
) -> None:
    now = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)
    config = _config()
    with Session(mysql_test_engine, expire_on_commit=False) as db:
        user = User(
            username="martingale-shadow-ledger",
            password_hash="test-only",  # noqa: S106 - isolated test fixture credential
        )
        db.add(user)
        db.flush()
        strategy = next(
            item
            for item in ensure_user_default_strategies(db, user.id)
            if item.engine_key == "martingale_tp4"
        )
        revision = strategy.revisions[0]
        deployment = StrategyDeployment(
            public_id=str(uuid.uuid4()),
            user_id=user.id,
            strategy_id=strategy.id,
            strategy_revision_id=revision.id,
            mode="shadow",
            name="Martingale TP4 Shadow",
            status="running",
            universe_override_json={"symbols": ["AMDUSDT"]},
            risk_override_json=strategy.risk_defaults_json,
            runtime_state_json={},
            started_at=utcnow(),
            created_at=utcnow(),
            updated_at=utcnow(),
        )
        db.add(deployment)
        db.flush()
        tiger = TigerReferenceQuote(
            "AMD", Decimal("100.01"), Decimal("100.02"), now
        )
        binance = BinanceExecutionQuote(
            "AMDUSDT",
            Decimal("99.49"),
            Decimal("99.50"),
            Decimal("99.495"),
            now,
        )
        basket = BasketSnapshot(
            box_high=Decimal("100"),
            box_low=Decimal("90"),
            previous_bid=Decimal("99.99"),
        )
        evaluation = evaluate_shadow_tick(
            config,
            basket,
            tiger=tiger,
            binance=binance,
            mapping_verified=True,
            point_size=Decimal("0.01"),
            hour=12,
            account_balance=Decimal("10000"),
            deployment_scope=deployment.public_id,
            event_id="tiger-depth-1001:binance-depth-2001",
            now=now,
        )
        transition = apply_shadow_evaluation(
            evaluation,
            binance=binance,
            tiger_bid=tiger.bid,
            fee_bps=Decimal("5"),
        )
        recorded = record_shadow_transition(
            db,
            deployment,
            config,
            transition,
            occurred_at=now,
            signal_time_ms=int(now.timestamp() * 1000),
            account_balance=Decimal("10000"),
        )
        db.commit()
        deployment_id = deployment.id
        signal_id = recorded.signal.id

    with Session(mysql_test_engine, expire_on_commit=False) as db:
        deployment = db.get(StrategyDeployment, deployment_id)
        assert deployment is not None
        restored = restore_shadow_basket(
            db, deployment, contract_symbol="AMDUSDT", for_update=True
        )
        assert restored.cycle is not None
        assert len(restored.basket.legs) == 1
        assert restored.basket.legs[0].entry_price == Decimal("99.500000000000")

        duplicate = record_shadow_transition(
            db,
            deployment,
            config,
            transition,
            occurred_at=now,
            signal_time_ms=int(now.timestamp() * 1000),
            account_balance=Decimal("10000"),
        )
        db.commit()

        assert duplicate.idempotent is True
        assert duplicate.signal.id == signal_id
        assert db.scalar(select(StrategySignal).where(StrategySignal.id == signal_id))
        assert len(db.scalars(select(StrategyBasketLeg)).all()) == 1
        assert len(db.scalars(select(StrategyBasketEvent)).all()) == 1
        assert transition.evaluation.decision.action == DecisionAction.OPEN

        stale_evaluation = evaluate_shadow_tick(
            config,
            basket,
            tiger=tiger,
            binance=binance,
            mapping_verified=True,
            point_size=Decimal("0.01"),
            hour=12,
            account_balance=Decimal("10000"),
            deployment_scope=deployment.public_id,
            event_id="tiger-depth-1002:binance-depth-2002",
            now=now,
        )
        stale_transition = apply_shadow_evaluation(
            stale_evaluation,
            binance=binance,
            tiger_bid=tiger.bid,
        )
        with pytest.raises(RuntimeError, match="stale martingale shadow transition"):
            record_shadow_transition(
                db,
                deployment,
                config,
                stale_transition,
                occurred_at=now,
                signal_time_ms=int(now.timestamp() * 1000) + 1,
                account_balance=Decimal("10000"),
            )
