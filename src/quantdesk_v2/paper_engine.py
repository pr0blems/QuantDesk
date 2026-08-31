"""Tenant-isolated multi-account paper trading engine."""

from __future__ import annotations

import hashlib
import json
import math
import threading
import time
import uuid
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from quantdesk_v2.strategy_evaluator import (
    StrategyCandle,
    StrategyEvaluationError,
    resolve_legacy_strategy_timeframe,
    resolve_strategy_timing_policy,
    strategy_timeframe_seconds,
)
from quantdesk_v2.strategy_runtime import (
    evaluate_strategy,
    validate_strategy_spec,
)
from quantdesk_v2.strategy_source_runtime import (
    evaluate_source,
    validate_source,
)

from . import indicators as ind
from . import market_store as store
from .application.paper_reconciliation import PaperExecutionReconciliationService
from .application.risk import RiskPolicy
from .application.safety import ExecutionSafetyController
from .application.strategy_execution import (
    build_entry_basis_snapshot as build_shared_entry_basis_snapshot,
)
from .application.strategy_execution import (
    evaluate_account_strategy,
    record_strategy_decision,
    strategy_snapshots,
)
from .application.strategy_signals import build_legacy_signal_evidence
from .domain.execution import (
    ExecutionMode,
    ExecutionState,
    IntentAction,
    OrderIntent,
)
from .domain.exit_policy import DEFAULT_EXIT_POLICY
from .domain.trading import (
    AccountSnapshot,
    AccountType,
    BrokerError,
    BrokerOrder,
    InstrumentRules,
    MarketOrder,
    OrderReference,
    OrderSide,
    OrderStatus,
    OrderType,
    Position,
    PositionDirection,
    PositionSide,
)
from .infrastructure.callback_broker import PaperBroker
from .infrastructure.paper_execution import PaperExecutionRuntime
from .infrastructure.persistence.paper_projections import MySqlPaperProjectionStore
from .infrastructure.store_market_data import StoreMarketDataFeed
from .live_risk import (
    OpenPositionRisk,
    account_loss_limits,
    atr_risk_position_size,
    closed_bar_signal_freshness,
    market_data_freshness,
    policy_from_config,
    signal_freshness,
    symbol_admission,
    symbol_risk_profile,
    tighten_policy_with_strategy,
    total_open_risk,
)
from .market_config import tradfi_symbols

DEFAULT_INITIAL_BALANCE = 10_000.0
DEFAULT_LEVERAGE = 20
DEFAULT_MAX_POSITIONS = 15
DEFAULT_MARGIN_CAP = 0.80
DEFAULT_FEE_BPS = 5.0
DEFAULT_SLIPPAGE_BPS = 3.0
DEFAULT_FUNDING_RATE_8H_BPS = 1.0
DEFAULT_STOP_LOSS_PCT = 3.0
DEFAULT_TAKE_PROFIT_PCT = 5.0
DEFAULT_MAX_HOLDING_BARS = 12
ATR_STOP_MULTIPLIER = 1.5
ATR_TAKE_PROFIT_MULTIPLIER = 2.5
FUNDING_INTERVAL_SECONDS = 8 * 60 * 60
LEGACY_PAPER_SIGNAL_MODE = "legacy_score_v1"
STRATEGY_EVENT_SIGNAL_MODE = "strategy_event_v2"
LEGACY_ENTRY_SCORE = 60.0
LEGACY_TREND_MA_PERIOD = 150
PAPER_MAX_FUTURE_TICKER_SKEW_SECONDS = 15
_lock = threading.RLock()
_paper_execution_safety: dict[int, ExecutionSafetyController] = {}
_paper_reconciliation = PaperExecutionReconciliationService(
    MySqlPaperProjectionStore(store)
)


class _PriceSnapshot(dict[str, float]):
    """Latest prices plus their observation timestamps.

    It remains a normal ``dict`` for the paper UI and existing call sites, while
    the worker can fail closed when the stored ticker is stale.
    """

    def __init__(self) -> None:
        super().__init__()
        self.timestamps: dict[str, int | float] = {}


class PaperAccountUnavailable(RuntimeError):
    pass


def _json_object(value: Any, default: dict[str, Any] | None = None) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError, json.JSONDecodeError):
            return dict(default or {})
        return parsed if isinstance(parsed, dict) else dict(default or {})
    return dict(default or {})


def _strategy_snapshots(account: dict[str, Any]) -> list[dict[str, Any]]:
    """Compatibility alias for account readers while execution uses the public service."""

    return strategy_snapshots(account)


def _strategy_display_name(account: dict[str, Any]) -> str:
    names = [str(item["name"]) for item in _strategy_snapshots(account) if item.get("name")]
    return " + ".join(names) or "未命名策略"


def _account(user_id: int, account_id: int) -> dict[str, Any]:
    rows = store.query(
        """SELECT a.*,d.id AS deployment_id,d.strategy_revision_id
           FROM paper_accounts a
           LEFT JOIN strategy_deployments d
             ON d.user_id=a.user_id AND d.mode='paper' AND d.target_account_id=a.id
            AND d.strategy_id=a.strategy_id
           WHERE a.id=? AND a.user_id=? AND a.status<>'archived'
           ORDER BY d.id DESC LIMIT 1""",
        (account_id, user_id),
    )
    if not rows:
        raise PaperAccountUnavailable("paper account is unavailable")
    account = dict(rows[0])
    account["config_json"] = _json_object(account.get("config_json"))
    account["strategy_snapshot_json"] = _json_object(account.get("strategy_snapshot_json"))
    return account


def _tracked_accounts(account_id: int | None = None) -> list[dict[str, Any]]:
    base = """SELECT a.*,d.id AS deployment_id,d.strategy_revision_id
              FROM paper_accounts a
              LEFT JOIN strategy_deployments d
                ON d.user_id=a.user_id AND d.mode='paper' AND d.target_account_id=a.id
               AND d.strategy_id=a.strategy_id
              WHERE a.status<>'archived'"""
    if account_id is None:
        rows = store.query(base + " ORDER BY a.id,d.id DESC")
    else:
        rows = store.query(base + " AND a.id=? ORDER BY d.id DESC", (account_id,))
    result = []
    seen: set[int] = set()
    for row in rows:
        account = dict(row)
        if int(account["id"]) in seen:
            continue
        seen.add(int(account["id"]))
        account["config_json"] = _json_object(account.get("config_json"))
        account["strategy_snapshot_json"] = _json_object(
            account.get("strategy_snapshot_json")
        )
        result.append(account)
    return result


def _config(account: dict[str, Any]) -> dict[str, float | int]:
    raw = account["config_json"]
    try:
        funding_rate_8h_bps = float(
            raw.get("funding_rate_8h_bps", DEFAULT_FUNDING_RATE_8H_BPS)
        )
    except (TypeError, ValueError):
        funding_rate_8h_bps = DEFAULT_FUNDING_RATE_8H_BPS
    if not math.isfinite(funding_rate_8h_bps):
        funding_rate_8h_bps = DEFAULT_FUNDING_RATE_8H_BPS
    return {
        "leverage": max(1, min(int(raw.get("leverage", DEFAULT_LEVERAGE)), 20)),
        "max_positions": max(
            1, min(int(raw.get("max_positions", DEFAULT_MAX_POSITIONS)), 20)
        ),
        "margin_cap": max(0.05, min(float(raw.get("margin_cap", DEFAULT_MARGIN_CAP)), 0.95)),
        "position_size_pct": max(0.1, min(float(raw.get("position_size_pct", 10)), 100)),
        "fee_bps": max(0.0, min(float(raw.get("fee_bps", DEFAULT_FEE_BPS)), 100)),
        "slippage_bps": max(
            0.0, min(float(raw.get("slippage_bps", DEFAULT_SLIPPAGE_BPS)), 100)
        ),
        # Historical funding rates are not available in the local market store.
        # Keep the paper approximation explicit and configurable instead of
        # silently treating perpetual funding as zero. Positive rates charge
        # longs and credit shorts; negative rates do the reverse.
        "funding_rate_8h_bps": max(-100.0, min(funding_rate_8h_bps, 100.0)),
        "stop_loss_pct": max(
            0.01, min(float(raw.get("stop_loss_pct", DEFAULT_STOP_LOSS_PCT)), 99.9)
        ),
        "take_profit_pct": max(
            0.01, min(float(raw.get("take_profit_pct", DEFAULT_TAKE_PROFIT_PCT)), 99.9)
        ),
        "max_holding_bars": max(
            0, min(int(raw.get("max_holding_bars", DEFAULT_MAX_HOLDING_BARS)), 10_000)
        ),
    }


def _positions(account: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in store.query(
            "SELECT * FROM paper_positions WHERE paper_account_id=? AND user_id=? ORDER BY id",
            (account["id"], account["user_id"]),
        )
    ]


def _paper_timeframe(account: dict[str, Any]) -> str:
    try:
        return resolve_strategy_timing_policy(
            (_strategy_snapshots(account) or [{}])[0],
            account.get("config_json") if isinstance(account.get("config_json"), dict) else None,
            default_max_holding_bars=DEFAULT_MAX_HOLDING_BARS,
        ).trigger_timeframe
    except (KeyError, StrategyEvaluationError, TypeError, ValueError):
        return "1h"


def _paper_execution_key(account: dict[str, Any], suffix: str) -> str:
    started = account.get("started_at")
    run = started.isoformat() if isinstance(started, datetime) else str(started or "initial")
    digest = hashlib.sha256(f"{run}:{suffix}".encode()).hexdigest()[:32]
    return f"paper:{account['id']}:{digest}"


def _paper_intent(
    account: dict[str, Any],
    *,
    suffix: str,
    symbol: str,
    action: IntentAction,
    side: OrderSide,
    quantity: Decimal,
    signal_time: int,
    valid_until: int,
    reduce_only: bool,
) -> OrderIntent | None:
    deployment_id = account.get("deployment_id")
    revision_id = account.get("strategy_revision_id")
    if deployment_id is None or revision_id is None:
        print(f"[paper] account {account.get('id')} has no immutable paper deployment")
        return None
    key = _paper_execution_key(account, suffix)
    now = datetime.now(UTC)
    normalized_valid_until = max(int(valid_until), int(now.timestamp()) + 300)
    return OrderIntent(
        intent_id=f"intent-{hashlib.sha256(key.encode('utf-8')).hexdigest()[:48]}",
        idempotency_key=key,
        strategy_version_id=f"revision-{revision_id}",
        tenant_scope=f"tenant:{account['user_id']}",
        user_scope=f"user:{account['user_id']}",
        account_scope=f"paper-account:{account['id']}",
        deployment_scope=f"deployment:{deployment_id}",
        mode=ExecutionMode.PAPER,
        market="binance_usdm",
        symbol=symbol,
        timeframe=_paper_timeframe(account),
        action=action,
        side=side,
        quantity=quantity,
        signal_time=datetime.fromtimestamp(signal_time, tz=UTC),
        valid_until=datetime.fromtimestamp(normalized_valid_until, tz=UTC),
        created_at=now,
        reduce_only=reduce_only,
    )


def _paper_account_snapshot(
    account: dict[str, Any], positions: list[dict[str, Any]]
) -> AccountSnapshot:
    prices = _prices()
    equity, unrealized = _equity(account, prices, positions)
    now = datetime.now(UTC)
    normalized_positions: list[Position] = []
    for row in positions:
        side = int(row["side"])
        mark = Decimal(str(prices.get(row["symbol"], row["avg_entry"])))
        quantity = Decimal(str(row["qty"]))
        normalized_positions.append(
            Position(
                symbol=str(row["symbol"]),
                direction=PositionDirection.LONG if side > 0 else PositionDirection.SHORT,
                position_side=PositionSide.BOTH,
                quantity=quantity,
                entry_price=Decimal(str(row["avg_entry"])),
                mark_price=mark,
                liquidation_price=(Decimal(str(row["liq_price"])) if row.get("liq_price") is not None else None),
                notional=quantity * mark,
                initial_margin=Decimal(str(row["margin"])),
                unrealized_pnl=Decimal(str(_upnl(row, float(mark)))),
                leverage=int(row["leverage"]),
                updated_at_ms=int(now.timestamp() * 1000),
            )
        )
    return AccountSnapshot(
        account_type=AccountType.USD_M_FUTURES,
        can_trade=account.get("status") == "active",
        wallet_balance=Decimal(str(equity)),
        available_balance=Decimal(str(account["balance"])),
        unrealized_pnl=Decimal(str(unrealized)),
        currency="USDT",
        updated_at=now,
        observed_at=now,
        positions=tuple(normalized_positions),
    )


def _paper_rules(symbol: str) -> InstrumentRules:
    return InstrumentRules(
        symbol=symbol,
        quantity_step=Decimal("0.000000000000000001"),
        minimum_quantity=Decimal("0.000000000000000001"),
        maximum_quantity=Decimal("1000000000000"),
        price_tick=Decimal("0.00000001"),
        minimum_notional=Decimal("0.00000001"),
    )


def _paper_order(row: dict[str, Any] | Any) -> BrokerOrder:
    return BrokerOrder(
        reference=OrderReference(str(row["client_order_id"]), str(row["symbol"])),
        exchange_order_id=f"paper-{row['id']}",
        symbol=str(row["symbol"]),
        side=OrderSide(str(row["side"])),
        position_side=PositionSide(str(row["position_side"])),
        order_type=OrderType.MARKET,
        status=OrderStatus(str(row["status"])),
        exchange_status=f"PAPER_{row['status']}",
        quantity=Decimal(str(row["quantity"])),
        executed_quantity=Decimal(str(row["executed_quantity"])),
        average_price=Decimal(str(row["average_price"])),
        reduce_only=str(row["action"]) == "close",
    )


def _paper_lookup(reference: OrderReference) -> BrokerOrder:
    rows = store.query(
        "SELECT * FROM paper_order_executions WHERE client_order_id=? LIMIT 1",
        (reference.client_order_id,),
    )
    if not rows:
        raise BrokerError("order_not_found")
    return _paper_order(rows[0])


def _reconcile_paper_account(account: dict[str, Any]) -> bool:
    state_key = f"paper:{account['id']}:projection_health"
    result = _paper_reconciliation.reconcile_account(
        user_id=int(account["user_id"]),
        paper_account_id=int(account["id"]),
    )
    if not result.ready:
        details = ",".join((*result.drift_codes, *result.errors)) or "pending_projection"
        previous = store.user_state_get(account["user_id"], state_key, {})
        health = {
            "status": "blocked",
            "pending_count": result.remaining,
            "drift_count": len(result.drift_codes),
            "failed_count": result.failed,
            "details": details[:500],
            "checked_at": int(time.time()),
        }
        if not isinstance(previous, dict) or (
            previous.get("status") != "blocked"
            or previous.get("details") != health["details"]
        ):
            store.user_state_set(account["user_id"], state_key, health)
            store.add_alert(
                "SYSTEM",
                "paper_projection_blocked",
                "warning",
                None,
                f"模拟账户「{account['name']}」成交投影异常，已隔离新订单。",
                {
                    "paper_account_id": account["public_id"],
                    "paper_account_name": account["name"],
                    **health,
                },
                user_id=account["user_id"],
            )
        print(
            f"[paper] account {account['id']} projection blocked: "
            f"remaining={result.remaining} details={details[:500]}"
        )
        return False
    previous = store.user_state_get(account["user_id"], state_key, {})
    if isinstance(previous, dict) and previous.get("status") == "blocked":
        recovered_at = int(time.time())
        store.user_state_set(
            account["user_id"],
            state_key,
            {
                "status": "healthy",
                "pending_count": 0,
                "drift_count": 0,
                "failed_count": 0,
                "checked_at": recovered_at,
                "last_success_at": recovered_at,
            },
        )
        store.add_alert(
            "SYSTEM",
            "paper_projection_recovered",
            "info",
            None,
            f"模拟账户「{account['name']}」成交投影已恢复。",
            {
                "paper_account_id": account["public_id"],
                "paper_account_name": account["name"],
                "recovered_at": recovered_at,
            },
            user_id=account["user_id"],
        )
    rows = store.query(
        "SELECT balance FROM paper_accounts WHERE id=? AND user_id=? LIMIT 1",
        (account["id"], account["user_id"]),
    )
    if not rows:
        print(f"[paper] account {account['id']} disappeared during reconciliation")
        return False
    try:
        balance = float(rows[0]["balance"])
    except (KeyError, TypeError, ValueError):
        return False
    if not math.isfinite(balance) or balance < 0:
        return False
    account["balance"] = balance
    return True


def _paper_execute(
    account: dict[str, Any],
    positions: list[dict[str, Any]],
    intent: OrderIntent,
    submit: Any,
) -> Any:
    account_id = int(account["id"])
    safety = _paper_execution_safety.setdefault(account_id, ExecutionSafetyController())
    broker = PaperBroker(
        account_scope=intent.account_scope,
        physical_account_id=f"paper-wallet:{account_id}",
        feed=StoreMarketDataFeed(),
        account_snapshot=lambda: _paper_account_snapshot(account, _positions(account)),
        rules=_paper_rules,
        submit=submit,
        lookup=_paper_lookup,
    )
    runtime = PaperExecutionRuntime(
        broker=broker,
        engine=store.get_engine(),
        tenant_scope=intent.tenant_scope,
        user_scope=intent.user_scope,
        account_scope=intent.account_scope,
        physical_account_id=broker.physical_account_id,
        risk_policy=RiskPolicy(
            max_open_positions=int(_config(account)["max_positions"]),
            max_notional_to_equity=Decimal(str(_config(account)["leverage"])),
            allowed_symbols=frozenset(tradfi_symbols()),
        ),
        safety=safety,
    )
    return runtime.execute(intent)


def _prices() -> dict[str, float]:
    snapshot = _PriceSnapshot()
    for row in store.query("SELECT symbol,price,ts FROM ticker WHERE price IS NOT NULL"):
        try:
            symbol = str(row["symbol"])
            price = float(row["price"])
        except (KeyError, TypeError, ValueError):
            continue
        if not math.isfinite(price) or price <= 0:
            continue
        snapshot[symbol] = price
        timestamp = row.get("ts")
        if timestamp is not None:
            snapshot.timestamps[symbol] = timestamp
    return snapshot


def _used_margin(positions: list[dict[str, Any]]) -> float:
    return sum(float(position["margin"]) for position in positions)


def _upnl(position: dict[str, Any], price: float) -> float:
    return (
        (price - float(position["avg_entry"]))
        * float(position["qty"])
        * int(position["side"])
    )


def _equity(
    account: dict[str, Any], prices: dict[str, float], positions: list[dict[str, Any]]
) -> tuple[float, float]:
    unrealized = sum(
        _upnl(position, prices.get(position["symbol"], float(position["avg_entry"])))
        - float(position.get("funding_acc") or 0)
        for position in positions
    )
    return float(account["balance"]) + _used_margin(positions) + unrealized, unrealized


def _paper_risk_policy(
    account: dict[str, Any], signal_evidence: dict[str, Any] | None = None
) -> Any:
    """Return the paper risk policy, optionally tightened by a full strategy.

    Strategy-authored risk proposals are advisory ceilings. They may reduce an
    account risk limit, but the user-selected paper leverage remains fixed.
    """

    raw = account.get("config_json")
    config = raw if isinstance(raw, dict) else {}
    policy_config = dict(config)
    # Paper exposes only ``leverage``.  A shared-risk migration persisted the
    # unrelated 10x live default as ``risk_max_leverage`` and silently overrode
    # configured 20x paper accounts.  The visible paper setting is authoritative
    # and every new paper position must use that exact leverage.
    policy_config["risk_max_leverage"] = max(
        1, min(int(config.get("leverage", DEFAULT_LEVERAGE)), 20)
    )
    policy = policy_from_config(policy_config)
    if "round_trip_cost_bps" not in config:
        configured_cost = Decimal(
            str(
                2
                * (
                    float(config.get("fee_bps", DEFAULT_FEE_BPS))
                    + float(config.get("slippage_bps", DEFAULT_SLIPPAGE_BPS))
                )
            )
        )
        policy = replace(policy, round_trip_cost_bps=max(configured_cost, Decimal(0)))

    snapshots = _strategy_snapshots(account)
    full_strategy_indexes = [
        index
        for index, snapshot in enumerate(snapshots)
        if snapshot.get("strategy_kind") in {"full_strategy", "source_strategy"}
    ]
    if not full_strategy_indexes or signal_evidence is None:
        return policy
    evidence = signal_evidence if isinstance(signal_evidence, dict) else {}
    components = evidence.get("strategy_signals")
    component_list = components if isinstance(components, list) else []
    tightened = policy
    for index in full_strategy_indexes:
        component_evidence = evidence
        if len(snapshots) > 1:
            try:
                component = component_list[index]
            except IndexError as exc:
                raise ValueError("full strategy risk proposal is unavailable") from exc
            if not isinstance(component, dict):
                raise ValueError("full strategy risk proposal is unavailable")
            candidate = component.get("evidence")
            component_evidence = candidate if isinstance(candidate, dict) else {}
        proposal = component_evidence.get("risk_proposal")
        if not isinstance(proposal, dict):
            raise ValueError("full strategy risk proposal is unavailable")
        tightened = tighten_policy_with_strategy(tightened, proposal)
    return replace(tightened, max_leverage=policy.max_leverage)


def _paper_signal_mode(account: dict[str, Any]) -> str:
    """Select the paper-only compatibility boundary for entry signals.

    Full strategies keep their versioned event evaluator.  Built-in strategies
    use the original QuantDesk paper semantics only when the account migration
    or account-creation API explicitly persists that compatibility mode.
    """

    snapshots = _strategy_snapshots(account)
    if len(snapshots) > 1:
        return STRATEGY_EVENT_SIGNAL_MODE
    snapshot = snapshots[0] if snapshots else {}
    if snapshot.get("strategy_kind") in {"full_strategy", "source_strategy"}:
        return STRATEGY_EVENT_SIGNAL_MODE
    raw = account.get("config_json") or {}
    configured = str(raw.get("signal_mode") or "").strip()
    if configured == LEGACY_PAPER_SIGNAL_MODE:
        return configured
    return STRATEGY_EVENT_SIGNAL_MODE


def _current_open_risk(
    positions: list[dict[str, Any]],
    config: dict[str, float | int],
    policy: Any,
) -> Decimal:
    risks: list[OpenPositionRisk] = []
    for position in positions:
        try:
            quantity = abs(Decimal(str(position.get("qty") or 0)))
            entry = Decimal(str(position.get("avg_entry") or 0))
            side = int(position.get("side") or 0)
        except (InvalidOperation, TypeError, ValueError, OverflowError):
            continue
        if quantity <= 0 or entry <= 0 or side not in {-1, 1}:
            continue
        try:
            stop = Decimal(str(position.get("stop") or 0))
        except (InvalidOperation, TypeError, ValueError):
            stop = Decimal(0)
        if stop <= 0 or (entry - stop) * side <= 0:
            repaired_stop, _ = _exit_levels(
                float(entry), side, position.get("atr_entry"), config
            )
            if repaired_stop is not None:
                stop = Decimal(str(repaired_stop))
        if stop <= 0 or (entry - stop) * side <= 0:
            # A legacy position without a usable stop must reserve at least its
            # full posted margin instead of disappearing from portfolio risk.
            margin = max(Decimal(str(position.get("margin") or 0)), Decimal(0))
            distance = margin / quantity if margin > 0 else entry
            stop = entry - Decimal(side) * distance
            if stop <= 0:
                stop = entry * Decimal("0.000001")
        risks.append(
            OpenPositionRisk(
                quantity=quantity,
                entry_price=entry,
                stop_price=stop,
                exit_cost_bps=policy.round_trip_cost_bps,
            )
        )
    return total_open_risk(risks)


def _price_is_fresh(
    prices: dict[str, float], symbol: str, now: int, policy: Any
) -> bool:
    timestamps = getattr(prices, "timestamps", None)
    # Plain dicts are retained as a compatibility seam for deterministic unit
    # tests and internal callers. Production snapshots always carry timestamps.
    if timestamps is None:
        return True
    timestamp = timestamps.get(symbol)
    if timestamp is None:
        return False
    try:
        return market_data_freshness(
            timestamp,
            now=now,
            max_age_seconds=policy.max_ticker_age_seconds,
            max_future_skew_seconds=PAPER_MAX_FUTURE_TICKER_SKEW_SECONDS,
        ).fresh
    except (TypeError, ValueError):
        return False


def _epoch_seconds(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(parsed):
        return None
    while abs(parsed) >= 100_000_000_000:
        parsed /= 1000
    return parsed


def _snapshot_signal_is_fresh(
    snapshot: dict[str, Any],
    config: dict[str, Any] | None,
    signal_time: Any,
    evidence: dict[str, Any],
    now: int,
    policy: Any,
) -> bool:
    try:
        if snapshot.get("strategy_kind") in {"full_strategy", "source_strategy"}:
            decision = signal_freshness(
                signal_time,
                now=now,
                max_age_seconds=policy.max_signal_age_seconds,
            )
            if not decision.fresh:
                return False
            valid_until = _epoch_seconds(evidence.get("valid_until"))
            return valid_until is not None and now <= valid_until
        timeframe = resolve_legacy_strategy_timeframe(
            snapshot,
            config,
        )
        timeframe_seconds = strategy_timeframe_seconds(timeframe)
        closed = closed_bar_signal_freshness(
            signal_time,
            timeframe_seconds=timeframe_seconds,
            now=now,
            valid_bars=max(
                1,
                math.ceil(policy.max_signal_age_seconds / timeframe_seconds),
            ),
        )
        maximum_age = signal_freshness(
            signal_time,
            now=now,
            max_age_seconds=policy.max_signal_age_seconds,
        )
        return closed.fresh and maximum_age.fresh
    except (TypeError, ValueError):
        return False


def _signal_is_fresh(
    account: dict[str, Any],
    signal_time: int,
    signal_evidence: dict[str, Any] | None,
    now: int,
    policy: Any,
) -> bool:
    snapshots = _strategy_snapshots(account)
    if not snapshots:
        return False
    config = (
        account.get("config_json")
        if isinstance(account.get("config_json"), dict)
        else None
    )
    evidence = signal_evidence if isinstance(signal_evidence, dict) else {}
    components = evidence.get("strategy_signals")
    if len(snapshots) > 1:
        if not isinstance(components, list) or len(components) != len(snapshots):
            return False
        for snapshot, component in zip(snapshots, components, strict=True):
            if not isinstance(component, dict) or component.get("direction") not in {-1, 1}:
                return False
            component_evidence = component.get("evidence")
            if not _snapshot_signal_is_fresh(
                snapshot,
                config,
                component.get("signal_time"),
                component_evidence if isinstance(component_evidence, dict) else {},
                now,
                policy,
            ):
                return False
        return True
    return _snapshot_signal_is_fresh(
        snapshots[0], config, signal_time, evidence, now, policy
    )


def _paper_signal_is_fresh(
    account: dict[str, Any],
    signal_time: int,
    signal_evidence: dict[str, Any] | None,
    now: int,
    policy: Any,
) -> bool:
    """Validate paper signals without weakening live-trading freshness.

    The original score is a state attached to the latest closed trigger bar, not
    a one-shot crossing. The legacy engine deliberately keeps that latest state
    until a newer closed score replaces it; ticker freshness and portfolio risk
    are still enforced independently before any order can be created.
    """

    if _paper_signal_mode(account) != LEGACY_PAPER_SIGNAL_MODE:
        return _signal_is_fresh(account, signal_time, signal_evidence, now, policy)
    try:
        opened = _epoch_seconds(signal_time)
        snapshot = (_strategy_snapshots(account) or [{}])[0]
        timing_policy = resolve_strategy_timing_policy(
            snapshot,
            account.get("config_json")
            if isinstance(account.get("config_json"), dict)
            else None,
            evidence=signal_evidence,
            default_max_holding_bars=DEFAULT_MAX_HOLDING_BARS,
        )
        return (
            opened is not None
            and now >= opened + timing_policy.timeframe_seconds
        )
    except (StrategyEvaluationError, TypeError, ValueError):
        return False


def _accrue_estimated_funding(
    account: dict[str, Any],
    position: dict[str, Any],
    price: float,
    now: int,
    config: dict[str, float | int],
) -> float:
    """Accrue a fixed-rate paper estimate on UTC eight-hour boundaries.

    The local store has no historical Binance funding-rate series. The estimate
    uses the configured rate and latest local price for every elapsed boundary.
    ``funding_ts`` is a durable checkpoint, so a restart catches up missed
    boundaries without charging one twice.
    """

    try:
        opened_ts = max(int(position.get("opened_ts") or 0), 0)
        stored_funding_ts = int(position.get("funding_ts") or 0)
        previous_ts = max(stored_funding_ts, opened_ts)
        quantity = abs(float(position.get("qty") or 0))
        side = int(position.get("side") or 0)
        funding_price = float(price)
        rate_bps = float(config["funding_rate_8h_bps"])
    except (KeyError, TypeError, ValueError, OverflowError):
        return 0.0
    if (
        side not in {-1, 1}
        or quantity <= 0
        or not math.isfinite(quantity)
        or funding_price <= 0
        or not math.isfinite(funding_price)
        or not math.isfinite(rate_bps)
    ):
        return 0.0

    first_boundary = (
        previous_ts // FUNDING_INTERVAL_SECONDS + 1
    ) * FUNDING_INTERVAL_SECONDS
    if first_boundary > int(now):
        return 0.0
    periods = (int(now) - first_boundary) // FUNDING_INTERVAL_SECONDS + 1
    last_boundary = first_boundary + (periods - 1) * FUNDING_INTERVAL_SECONDS
    delta = quantity * funding_price * side * rate_bps / 10_000 * periods
    if not math.isfinite(delta):
        return 0.0
    updated = store.execute(
        """UPDATE paper_positions
           SET funding_acc=funding_acc+?,funding_ts=?
           WHERE id=? AND paper_account_id=? AND user_id=? AND funding_ts=?""",
        (
            delta,
            last_boundary,
            position["id"],
            account["id"],
            account["user_id"],
            stored_funding_ts,
        ),
    )
    if updated != 1:
        return 0.0
    position["funding_acc"] = float(position.get("funding_acc") or 0) + delta
    position["funding_ts"] = last_boundary
    return delta


def _local_day_start_ts(now: int, timezone_offset_minutes: int) -> int:
    offset_seconds = int(timezone_offset_minutes) * 60
    local_now = int(now) + offset_seconds
    return local_now - local_now % 86_400 - offset_seconds


def _today_pnl(
    account: dict[str, Any],
    current_equity: float,
    now: int,
    timezone_offset_minutes: int,
) -> float:
    """Return the account equity change since the user's local day began."""

    day_start_ts = _local_day_start_ts(now, timezone_offset_minutes)
    rows = store.query(
        """SELECT equity FROM paper_equity
           WHERE paper_account_id=? AND user_id=? AND ts<?
           ORDER BY ts DESC LIMIT 1""",
        (account["id"], account["user_id"], day_start_ts),
    )
    baseline = float(account["initial_balance"])
    if rows:
        try:
            candidate = float(rows[0]["equity"])
        except (KeyError, TypeError, ValueError):
            candidate = math.nan
        if math.isfinite(candidate):
            baseline = candidate
    return current_equity - baseline


def _entry_loss_guard(
    account: dict[str, Any], current_equity: float, now: int, policy: Any
) -> bool:
    """Block new positions after the same daily/drawdown limits as live trading."""

    if not math.isfinite(current_equity) or current_equity <= 0:
        return False
    day_start_ts = _local_day_start_ts(now, 0)
    day_rows = store.query(
        """SELECT equity FROM paper_equity
           WHERE paper_account_id=? AND user_id=? AND ts<?
           ORDER BY ts DESC LIMIT 1""",
        (account["id"], account["user_id"], day_start_ts),
    )
    baseline = float(account["initial_balance"])
    if day_rows:
        try:
            candidate = float(day_rows[0]["equity"])
        except (KeyError, TypeError, ValueError):
            candidate = math.nan
        if math.isfinite(candidate) and candidate > 0:
            baseline = candidate
    high_rows = store.query(
        """SELECT MAX(equity) AS high_watermark FROM paper_equity
           WHERE paper_account_id=? AND user_id=?""",
        (account["id"], account["user_id"]),
    )
    high_watermark = max(float(account["initial_balance"]), current_equity)
    if high_rows:
        try:
            candidate = float(high_rows[0]["high_watermark"])
        except (KeyError, TypeError, ValueError):
            candidate = math.nan
        if math.isfinite(candidate) and candidate > 0:
            high_watermark = max(high_watermark, candidate)
    try:
        return account_loss_limits(
            current_equity=current_equity,
            start_of_day_equity=baseline,
            high_watermark_equity=high_watermark,
            policy=policy,
        ).allow_new_entries
    except (TypeError, ValueError):
        return False


def _set_balance(account: dict[str, Any], balance: float) -> None:
    safe_balance = max(round(balance, 8), 0.0)
    store.execute(
        "UPDATE paper_accounts SET balance=?,updated_at=CURRENT_TIMESTAMP WHERE id=? AND user_id=?",
        (safe_balance, account["id"], account["user_id"]),
    )
    account["balance"] = safe_balance


def _candles(
    symbol: str, timeframe: str
) -> tuple[list[StrategyCandle], list[dict[str, Any]]]:
    rows = store.get_klines(symbol, timeframe, 600)
    candles = [
        StrategyCandle(
            ts=int(row["open_time"]),
            open=float(row["open"]),
            high=float(row["high"]),
            low=float(row["low"]),
            close=float(row["close"]),
            volume=float(row["volume"]),
        )
        for row in rows
    ]
    return candles, rows


def _legacy_score_signal(
    account: dict[str, Any], symbol: str, price: float | None = None
) -> tuple[int, float | None, list[str], int | None, dict[str, Any]]:
    """Reproduce the original paper account's persistent 4h score signal.

    The compatibility path intentionally reads the shared score table and uses
    the historical ``abs(score) >= 60`` plus MA150 trend filter.  Position
    sizing and all portfolio guards still run through the current risk engine.
    """

    score_rows = store.query(
        """SELECT score,detail,open_time FROM scores
           WHERE symbol=? AND tf='4h' ORDER BY open_time DESC LIMIT 1""",
        (symbol,),
    )
    if not score_rows:
        return 0, None, [], None, {"signal_mode": LEGACY_PAPER_SIGNAL_MODE}
    row = score_rows[0]
    try:
        score = float(row["score"])
        signal_time = int(row["open_time"])
    except (KeyError, TypeError, ValueError, OverflowError):
        return 0, None, [], None, {"signal_mode": LEGACY_PAPER_SIGNAL_MODE}
    if not math.isfinite(score):
        return 0, None, [], None, {"signal_mode": LEGACY_PAPER_SIGNAL_MODE}

    candles, rows = _candles(symbol, "4h")
    if len(candles) < LEGACY_TREND_MA_PERIOD:
        return 0, None, [], signal_time, {
            "signal_mode": LEGACY_PAPER_SIGNAL_MODE,
            "score": score,
            "threshold": LEGACY_ENTRY_SCORE,
            "reason_codes": ["INSUFFICIENT_MA150_HISTORY"],
        }
    closes = [item.close for item in candles]
    ma150 = sum(closes[-LEGACY_TREND_MA_PERIOD:]) / LEGACY_TREND_MA_PERIOD
    observed_price = float(price) if price is not None else closes[-1]
    atr = None
    if len(rows) > 15:
        atr = ind.atr(
            [float(item["high"]) for item in rows],
            [float(item["low"]) for item in rows],
            [float(item["close"]) for item in rows],
        )

    threshold_passed = abs(score) >= LEGACY_ENTRY_SCORE
    side = 1 if score > 0 else -1 if score < 0 else 0
    trend_passed = side != 0 and (
        (side > 0 and observed_price >= ma150)
        or (side < 0 and observed_price <= ma150)
    )
    direction = side if threshold_passed and trend_passed else 0
    reason_codes = []
    if threshold_passed:
        reason_codes.append("LEGACY_SCORE_THRESHOLD")
    if trend_passed:
        reason_codes.append("MA150_TREND_FILTER")
    evidence = {
        "signal_mode": LEGACY_PAPER_SIGNAL_MODE,
        "engine_key": LEGACY_PAPER_SIGNAL_MODE,
        "score": score,
        "threshold": LEGACY_ENTRY_SCORE,
        "reason_codes": reason_codes,
        "indicators": {
            "price": observed_price,
            "ma150": ma150,
            "atr": atr,
        },
    }
    basis = [
        f"策略：{account.get('strategy_snapshot_json', {}).get('name') or '老版评分策略'}",
        f"4h 评分：{score:+g} / 阈值：±{LEGACY_ENTRY_SCORE:g}",
        f"MA150：{ma150:.8g}",
    ]
    return direction, atr, basis, signal_time, evidence


def _engine_signal_evidence(
    engine_key: str,
    candles: list[StrategyCandle],
    parameters: dict[str, Any],
) -> dict[str, Any]:
    """Compatibility alias for the public legacy evidence builder."""

    return build_legacy_signal_evidence(engine_key, candles, parameters)


def _strategy_signal(
    account: dict[str, Any], symbol: str, snapshot: dict[str, Any] | None = None
) -> tuple[int, float | None, list[str], int | None, dict[str, Any]]:
    return evaluate_account_strategy(
        account,
        symbol,
        snapshot,
        load_klines=lambda selected_symbol, timeframe, limit: store.get_klines(
            selected_symbol, timeframe, limit
        ),
        record_decision=_record_full_strategy_decision,
        full_validator=validate_strategy_spec,
        full_evaluator=evaluate_strategy,
        source_validator=validate_source,
        source_evaluator=evaluate_source,
    ).legacy_tuple()


def _paper_strategy_signal(
    account: dict[str, Any], symbol: str, price: float | None = None
) -> tuple[int, float | None, list[str], int | None, dict[str, Any]]:
    if _paper_signal_mode(account) == LEGACY_PAPER_SIGNAL_MODE:
        return _legacy_score_signal(account, symbol, price)
    snapshots = _strategy_snapshots(account)
    if len(snapshots) <= 1:
        return _strategy_signal(account, symbol, snapshots[0] if snapshots else None)
    return _combined_strategy_signal(account, snapshots, symbol)


def _signal_consumption_value(
    signal_time: int, signal_evidence: dict[str, Any] | None
) -> int | str:
    evidence = signal_evidence if isinstance(signal_evidence, dict) else {}
    combination_key = evidence.get("combination_key")
    return str(combination_key) if combination_key else signal_time


def _combined_strategy_signal(
    account: dict[str, Any],
    snapshots: list[dict[str, Any]],
    symbol: str,
) -> tuple[int, float | None, list[str], int | None, dict[str, Any]]:
    """Require every selected strategy to emit the same non-zero direction."""

    results = [
        _strategy_signal(account, symbol, snapshot)
        for snapshot in snapshots
    ]
    directions = [int(result[0]) for result in results]
    signal_times = [result[3] for result in results]
    all_same_direction = (
        bool(directions)
        and directions[0] in {-1, 1}
        and all(direction == directions[0] for direction in directions)
        and all(signal_time is not None for signal_time in signal_times)
    )
    direction = directions[0] if all_same_direction else 0
    matched_count = (
        len(directions)
        if all_same_direction
        else max(directions.count(1), directions.count(-1))
    )
    components: list[dict[str, Any]] = []
    combined_basis = [
        f"组合条件：{matched_count}/{len(snapshots)} 策略同向满足（全部满足才开仓）"
    ]
    for snapshot, result in zip(snapshots, results, strict=True):
        component_direction, component_atr, basis, signal_time, evidence = result
        combined_basis.extend(basis)
        components.append(
            {
                "strategy_id": snapshot.get("public_id"),
                "strategy_name": snapshot.get("name"),
                "strategy_kind": snapshot.get("strategy_kind"),
                "engine_key": snapshot.get("engine_key"),
                "direction": int(component_direction),
                "atr": component_atr,
                "signal_time": signal_time,
                "evidence": evidence,
            }
        )
    primary_evidence = results[0][4] if results and isinstance(results[0][4], dict) else {}
    aggregate_time = max(
        (int(value) for value in signal_times if value is not None),
        default=None,
    )
    combination_key = "|".join(
        f"{snapshot.get('public_id') or index}:{signal_time}:{component_direction}"
        for index, (snapshot, signal_time, component_direction) in enumerate(
            zip(snapshots, signal_times, directions, strict=True)
        )
    )
    evidence = {
        "combination_mode": "all",
        "required_count": len(snapshots),
        "matched_count": matched_count,
        "combination_key": combination_key,
        "strategy_signals": components,
        "risk_proposal": primary_evidence.get("risk_proposal"),
        "score": primary_evidence.get("score"),
    }
    atr = next((result[1] for result in results if result[1] is not None), None)
    return direction, atr, combined_basis, aggregate_time, evidence


def _record_full_strategy_decision(
    account: dict[str, Any],
    symbol: str,
    spec: dict[str, Any],
    decision: Any,
    snapshot: dict[str, Any] | None = None,
) -> bool:
    return record_strategy_decision(
        account,
        symbol,
        spec,
        decision,
        snapshot,
        query=store.query,
        execute=store.execute,
        log_mode="paper",
    )


def build_entry_basis_snapshot(
    account: dict[str, Any],
    *,
    mode: str,
    symbol: str,
    direction: int,
    signal_time: int | None,
    reasons: list[str],
    evidence: dict[str, Any] | None,
    entry_price: float,
    atr: float | None,
    stop: float | None,
    target: float | None,
    leverage: int,
    margin: float | None,
) -> tuple[dict[str, Any], int | None]:
    return build_shared_entry_basis_snapshot(
        account,
        mode=mode,
        symbol=symbol,
        direction=direction,
        signal_time=signal_time,
        reasons=reasons,
        evidence=evidence,
        entry_price=entry_price,
        atr=atr,
        stop=stop,
        target=target,
        leverage=leverage,
        margin=margin,
        query=store.query,
        default_max_holding_bars=DEFAULT_MAX_HOLDING_BARS,
    )


def _execution_price(price: float, side: int, opening: bool, slippage_bps: float) -> float:
    adverse = (side > 0) == opening
    adjustment = price * slippage_bps / 10_000
    return price + adjustment if adverse else price - adjustment


def _exit_levels(
    entry: float,
    side: int,
    atr: float | None,
    config: dict[str, float | int],
) -> tuple[float | None, float | None]:
    """Compatibility wrapper around the shared exit-level policy."""

    plan = DEFAULT_EXIT_POLICY.resolve_levels(
        entry,
        side,
        stop_loss_pct=config.get("stop_loss_pct"),
        take_profit_pct=config.get("take_profit_pct"),
        atr=atr,
        atr_stop_multiplier=ATR_STOP_MULTIPLIER,
        atr_take_profit_multiplier=ATR_TAKE_PROFIT_MULTIPLIER,
    )
    return (plan.stop, plan.target) if plan is not None else (None, None)


def _signal_exit_levels(
    entry: float,
    side: int,
    atr: float | None,
    config: dict[str, float | int],
    signal_evidence: dict[str, Any] | None,
) -> tuple[float | None, float | None]:
    """Use the full strategy's immutable distances, else the legacy ATR rule."""

    proposal = (signal_evidence or {}).get("risk_proposal")
    plan = DEFAULT_EXIT_POLICY.resolve_levels(
        entry,
        side,
        stop_loss_pct=config.get("stop_loss_pct"),
        take_profit_pct=config.get("take_profit_pct"),
        atr=atr,
        risk_proposal=proposal if isinstance(proposal, dict) else None,
        atr_stop_multiplier=ATR_STOP_MULTIPLIER,
        atr_take_profit_multiplier=ATR_TAKE_PROFIT_MULTIPLIER,
    )
    return (plan.stop, plan.target) if plan is not None else (None, None)


def _repair_missing_target(
    account: dict[str, Any],
    position: dict[str, Any],
    atr: float | None,
    config: dict[str, float | int],
) -> None:
    """Backfill existing zero or invalid targets before evaluating exits."""

    entry = float(position["avg_entry"])
    side = int(position["side"])
    try:
        current_target = float(position.get("target") or 0)
    except (TypeError, ValueError):
        current_target = 0.0
    if (
        math.isfinite(current_target)
        and current_target > 0
        and (current_target - entry) * side > 0
    ):
        return
    _, target = _exit_levels(entry, side, position.get("atr_entry") or atr, config)
    if target is None:
        return
    store.execute(
        """UPDATE paper_positions SET target=?
           WHERE id=? AND paper_account_id=? AND user_id=?""",
        (target, position["id"], account["id"], account["user_id"]),
    )
    position["target"] = target


def _open_position(
    account: dict[str, Any],
    symbol: str,
    side: int,
    price: float,
    atr: float | None,
    basis: list[str],
    positions: list[dict[str, Any]],
    now: int,
    signal_time: int | None = None,
    signal_evidence: dict[str, Any] | None = None,
) -> bool:
    config = _config(account)
    try:
        policy = _paper_risk_policy(account, signal_evidence)
    except ValueError:
        return False
    equity, _ = _equity(account, _prices(), positions)
    available = min(
        float(account["balance"]),
        equity * float(config["margin_cap"]) - _used_margin(positions),
    )
    if not math.isfinite(equity) or equity <= 0 or available <= 0:
        return False
    execution = _execution_price(price, side, True, float(config["slippage_bps"]))
    stop, target = _signal_exit_levels(execution, side, atr, config, signal_evidence)
    if stop is None or target is None:
        return False
    stop_distance = abs(Decimal(str(execution)) - Decimal(str(stop)))
    leverage = int(config["leverage"])
    entry_fee_rate = float(config["fee_bps"]) / 10_000
    available_for_margin = available / (1 + leverage * entry_fee_rate)
    sizing = atr_risk_position_size(
        equity=equity,
        available_balance=available_for_margin,
        entry_price=execution,
        stop_distance=stop_distance,
        requested_leverage=leverage,
        current_open_risk=_current_open_risk(positions, config, policy),
        direction=side,
        high_risk=symbol_risk_profile(symbol).high_risk,
        policy=policy,
    )
    if not sizing.allowed:
        return False
    quantity = float(sizing.quantity)
    notional = quantity * execution
    margin = notional / sizing.effective_leverage
    leverage = sizing.effective_leverage
    fee = notional * entry_fee_rate
    if (
        not math.isfinite(quantity)
        or quantity <= 0
        or not math.isfinite(margin)
        or margin <= 0
        or margin + fee > available + 1e-8
    ):
        return False
    liquidation = execution * (1 - side * (1 / leverage - 0.005))
    entry_basis, _ = build_entry_basis_snapshot(
        account,
        mode="paper",
        symbol=symbol,
        direction=side,
        signal_time=signal_time,
        reasons=basis,
        evidence=signal_evidence,
        entry_price=execution,
        atr=atr,
        stop=stop,
        target=target,
        leverage=leverage,
        margin=margin,
    )
    entry_basis["execution"].update(
        {
            "entry_fee": fee,
            "fee_bps": float(config["fee_bps"]),
            "slippage_bps": float(config["slippage_bps"]),
            "estimated_loss_at_stop": float(sizing.estimated_loss_at_stop),
            "risk_budget": float(sizing.risk_budget),
        }
    )
    score = entry_basis["signal"].get("score")
    stored_score = int(score) if isinstance(score, (int, float)) else None
    debit = margin + fee
    intent = _paper_intent(
        account,
        suffix=f"{symbol}:{signal_time or now}:open:{side}",
        symbol=symbol,
        action=IntentAction.OPEN,
        side=OrderSide.BUY if side > 0 else OrderSide.SELL,
        quantity=Decimal(str(quantity)),
        signal_time=int(signal_time or now),
        valid_until=now + 300,
        reduce_only=False,
    )
    if intent is None:
        return False
    projection = {
        "schema_version": 1,
        "action": "open",
        "balance_debit": debit,
        "position": {
            "side": side,
            "qty": quantity,
            "avg_entry": execution,
            "margin": margin,
            "leverage": leverage,
            "stop": stop,
            "target": target,
            "opened_ts": now,
            "last_add_ts": now,
            "open_score": stored_score,
            "basis": entry_basis,
            "liq_price": liquidation,
            "funding_ts": now,
            "atr_entry": atr,
            "peak_price": execution,
        },
    }

    def submit(order: MarketOrder) -> BrokerOrder:
        with store.transaction() as transaction:
            transaction.execute(
                """INSERT INTO paper_order_executions(
                   public_id,user_id,paper_account_id,deployment_id,intent_id,client_order_id,
                   symbol,action,side,position_side,quantity,executed_quantity,average_price,
                   status,response_json,projection_status,projection_version,projection_json
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,'FILLED',?,'pending',
                            'paper_projection_v1',?)""",
                (
                    str(uuid.uuid4()), account["user_id"], account["id"],
                    account["deployment_id"], intent.intent_id, order.client_order_id,
                    symbol, "open", order.side.value, order.position_side.value,
                    order.quantity, order.quantity, execution,
                    json.dumps({"simulated": True, "entry_basis": entry_basis}, ensure_ascii=False),
                    json.dumps(projection, ensure_ascii=False),
                ),
            )
            return _paper_order(
                {
                    "id": order.client_order_id,
                    "client_order_id": order.client_order_id,
                    "symbol": symbol,
                    "side": order.side.value,
                    "position_side": order.position_side.value,
                    "quantity": order.quantity,
                    "executed_quantity": order.quantity,
                    "average_price": execution,
                    "status": "FILLED",
                    "action": "open",
                }
            )

    result = _paper_execute(account, positions, intent, submit)
    if result.state is not ExecutionState.FILLED:
        print(f"[paper] unified open blocked: {result.error_code or result.state.value}")
        return False
    if not _reconcile_paper_account(account):
        return False
    direction = "long" if side > 0 else "short"
    store.add_alert(
        symbol,
        "paper_open",
        direction,
        None,
        f"模拟盘【{account['name']}】{symbol} {direction} 开仓 @ {execution:.8g}",
        {
            "paper_account_id": account["public_id"],
            "paper_account_name": account["name"],
            "strategy_name": _strategy_display_name(account),
            "entry_basis": entry_basis,
            "price": execution,
            "quantity": quantity,
        },
        user_id=account["user_id"],
    )
    return True


def _close_position(
    account: dict[str, Any], position: dict[str, Any], price: float, reason: str, now: int
) -> bool:
    config = _config(account)
    execution = _execution_price(
        price, int(position["side"]), False, float(config["slippage_bps"])
    )
    quantity = float(position["qty"])
    fee = quantity * execution * float(config["fee_bps"]) / 10_000
    pnl = (
        (execution - float(position["avg_entry"])) * quantity * int(position["side"])
        - float(position.get("funding_acc") or 0)
    )
    margin = float(position["margin"])
    returned = max(margin + pnl - fee, 0.0)
    trade_basis = _json_object(position.get("basis"))
    exit_decision = DEFAULT_EXIT_POLICY.decision_for_reason(
        reason, price, observed_at=now
    )
    if exit_decision is not None:
        trade_basis["exit_decision"] = exit_decision.snapshot(
            mode="paper",
            execution_price=execution,
        )
    close_side = OrderSide.SELL if int(position["side"]) > 0 else OrderSide.BUY
    intent = _paper_intent(
        account,
        suffix=f"position:{position['id']}:{position['opened_ts']}:close",
        symbol=str(position["symbol"]),
        action=IntentAction.CLOSE,
        side=close_side,
        quantity=Decimal(str(quantity)),
        signal_time=now,
        valid_until=now + 300,
        reduce_only=True,
    )
    if intent is None:
        return False
    projection = {
        "schema_version": 1,
        "action": "close",
        "position_id": int(position["id"]),
        "balance_credit": returned,
        "trade": {
            "side": int(position["side"]),
            "qty": quantity,
            "entry_price": float(position["avg_entry"]),
            "exit_price": execution,
            "margin": margin,
            "pnl": max(pnl, -margin),
            "fee": fee,
            "funding": float(position.get("funding_acc") or 0),
            "reason": reason,
            "open_score": position.get("open_score"),
            "opened_ts": int(position["opened_ts"]),
            "closed_ts": now,
            "entry_basis": trade_basis,
        },
    }

    def submit(order: MarketOrder) -> BrokerOrder:
        with store.transaction() as transaction:
            transaction.execute(
                """INSERT INTO paper_order_executions(
                   public_id,user_id,paper_account_id,deployment_id,intent_id,client_order_id,
                   symbol,action,side,position_side,quantity,executed_quantity,average_price,
                   status,response_json,projection_status,projection_version,projection_json
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,'FILLED',?,'pending',
                            'paper_projection_v1',?)""",
                (
                    str(uuid.uuid4()), account["user_id"], account["id"],
                    account["deployment_id"], intent.intent_id, order.client_order_id,
                    position["symbol"], "close", order.side.value, order.position_side.value,
                    order.quantity, order.quantity, execution,
                    json.dumps({"simulated": True, "reason": reason}, ensure_ascii=False),
                    json.dumps(projection, ensure_ascii=False),
                ),
            )
            return _paper_order(
                {
                    "id": order.client_order_id,
                    "client_order_id": order.client_order_id,
                    "symbol": position["symbol"],
                    "side": order.side.value,
                    "position_side": order.position_side.value,
                    "quantity": order.quantity,
                    "executed_quantity": order.quantity,
                    "average_price": execution,
                    "status": "FILLED",
                    "action": "close",
                }
            )

    result = _paper_execute(account, [position], intent, submit)
    if result.state is not ExecutionState.FILLED:
        print(f"[paper] unified close blocked: {result.error_code or result.state.value}")
        return False
    if not _reconcile_paper_account(account):
        return False
    direction = "long" if int(position["side"]) > 0 else "short"
    store.add_alert(
        position["symbol"],
        "paper_close",
        direction,
        None,
        f"模拟盘【{account['name']}】{position['symbol']} {direction} 平仓"
        f" @ {execution:.8g}，盈亏 {pnl - fee:+.2f} USDT（{reason}）",
        {
            "paper_account_id": account["public_id"],
            "paper_account_name": account["name"],
            "strategy_name": _strategy_display_name(account),
            "price": execution,
            "pnl": pnl - fee,
            "reason": reason,
        },
        user_id=account["user_id"],
    )
    return True


def _tick_account(account: dict[str, Any], prices: dict[str, float], now: int) -> None:
    config = _config(account)
    policy = _paper_risk_policy(account)
    positions = _positions(account)
    closed_symbols: set[str] = set()
    for position in list(positions):
        price = prices.get(position["symbol"])
        funding_price = (
            float(price)
            if price is not None and math.isfinite(float(price)) and float(price) > 0
            else float(position["avg_entry"])
        )
        _accrue_estimated_funding(account, position, funding_price, now, config)
        if (
            not price
            or not math.isfinite(float(price))
            or float(price) <= 0
            or not _price_is_fresh(prices, position["symbol"], now, policy)
        ):
            continue
        side = int(position["side"])
        if side not in {-1, 1}:
            print(f"[paper] invalid side skipped: position={position.get('id')}")
            continue
        # Stored exchange-risk levels must remain effective even when indicator
        # calculation or historical market data is temporarily unavailable.
        _repair_missing_target(account, position, None, config)
        entry_basis = _json_object(position.get("basis"))
        captured_timing = entry_basis.get("execution_policy")
        try:
            timing_policy = resolve_strategy_timing_policy(
                (_strategy_snapshots(account) or [{}])[0],
                account.get("config_json")
                if isinstance(account.get("config_json"), dict)
                else None,
                captured=(
                    captured_timing if isinstance(captured_timing, dict) else None
                ),
                default_max_holding_bars=DEFAULT_MAX_HOLDING_BARS,
            )
            holding_period_expired = timing_policy.expired(
                opened_at=position["opened_ts"],
                observed_at=now,
            )
        except (KeyError, StrategyEvaluationError, TypeError, ValueError):
            holding_period_expired = False
            print(
                f"[paper] holding policy unavailable for position={position.get('id')}"
            )
        exit_decision = DEFAULT_EXIT_POLICY.evaluate_mark(
            price,
            side,
            stop=position.get("stop"),
            target=position.get("target"),
            liquidation=position.get("liq_price"),
            observed_at=now,
        )
        strategy_reversal = False
        if exit_decision is None:
            direction = 0
            signal_time = None
            signal_evidence: dict[str, Any] = {}
            try:
                direction, _, _, signal_time, signal_evidence = _paper_strategy_signal(
                    account, position["symbol"], price
                )
            except Exception as exc:
                print(
                    f"[paper] signal unavailable for {position['symbol']}: "
                    f"{type(exc).__name__}"
                )
            strategy_reversal = bool(
                signal_time is not None
                and _paper_signal_is_fresh(
                    account, signal_time, signal_evidence, now, policy
                )
                and direction == -side
            )
        selected_exit = DEFAULT_EXIT_POLICY.select(
            price=price,
            observed_at=now,
            market_decision=exit_decision,
            strategy_reversal=strategy_reversal,
            holding_period_expired=holding_period_expired,
        )
        if selected_exit is not None:
            if _close_position(
                account, position, price, selected_exit.reason, now
            ):
                closed_symbols.add(position["symbol"])
            positions = _positions(account)

    universe = tradfi_symbols()
    equity, _ = _equity(account, prices, positions)
    occupied_symbols = {str(position["symbol"]).upper() for position in positions}
    candidates = [
        symbol
        for symbol in universe
        if symbol not in closed_symbols and symbol not in occupied_symbols
    ]
    allow_new_entries = bool(candidates) and _entry_loss_guard(
        account, equity, now, policy
    )
    if allow_new_entries and len(positions) < int(config["max_positions"]):
        for symbol in candidates:
            if len(positions) >= int(config["max_positions"]):
                break
            if symbol in closed_symbols:
                continue
            if symbol in occupied_symbols:
                continue
            price = prices.get(symbol)
            if (
                not price
                or not _price_is_fresh(prices, symbol, now, policy)
                or not symbol_admission(
                    symbol, sorted(occupied_symbols), policy=policy
                ).allowed
            ):
                continue
            direction, atr, basis, signal_time, signal_evidence = _paper_strategy_signal(
                account, symbol, price
            )
            if (
                direction not in {-1, 1}
                or signal_time is None
                or not _paper_signal_is_fresh(
                    account, signal_time, signal_evidence, now, policy
                )
            ):
                continue
            signal_mode = _paper_signal_mode(account)
            state_key = f"paper:{account['id']}:signal:{symbol}"
            consumption_value = _signal_consumption_value(signal_time, signal_evidence)
            if (
                signal_mode == STRATEGY_EVENT_SIGNAL_MODE
                and store.user_state_get(account["user_id"], state_key)
                == consumption_value
            ):
                continue
            if _open_position(
                account,
                symbol,
                direction,
                price,
                atr,
                basis,
                positions,
                now,
                signal_time,
                signal_evidence,
            ):
                if signal_mode == STRATEGY_EVENT_SIGNAL_MODE:
                    store.user_state_set(
                        account["user_id"], state_key, consumption_value
                    )
                positions = _positions(account)
                occupied_symbols = {
                    str(position["symbol"]).upper() for position in positions
                }

    _record_equity(account, prices, positions, now)


def _record_equity(
    account: dict[str, Any], prices: dict[str, float], positions: list[dict[str, Any]], now: int
) -> None:
    equity, _ = _equity(account, prices, positions)
    minute = now - now % 60
    store.execute(
        "REPLACE INTO paper_equity(paper_account_id,user_id,ts,equity,balance) VALUES(?,?,?,?,?)",
        (account["id"], account["user_id"], minute, round(equity, 2), account["balance"]),
    )
    store.execute(
        "UPDATE paper_accounts SET last_tick_at=CURRENT_TIMESTAMP WHERE id=? AND user_id=?",
        (account["id"], account["user_id"]),
    )


def tick(account_id: int | None = None) -> None:
    with store.advisory_lock("quantdesk-paper-tick", 0) as acquired:
        if not acquired:
            return
        with _lock:
            now = int(time.time())
            ready_accounts: list[dict[str, Any]] = []
            for account in _tracked_accounts(account_id):
                try:
                    if _reconcile_paper_account(account):
                        ready_accounts.append(account)
                except Exception as exc:
                    print(
                        f"[paper] account {account.get('id')} isolated: "
                        f"{type(exc).__name__}: {exc}"
                    )
            prices = _prices()
            if not prices:
                return
            for account in ready_accounts:
                try:
                    if account["status"] == "active":
                        _tick_account(account, prices, now)
                    else:
                        _record_equity(account, prices, _positions(account), now)
                except Exception as exc:
                    # One tenant's malformed projection must never stop another
                    # paper account. The pending fact remains blocked and is
                    # retried before that account may generate new intents.
                    print(
                        f"[paper] account {account.get('id')} isolated: "
                        f"{type(exc).__name__}: {exc}"
                    )


def paper_loop() -> None:
    print("[paper] multi-user paper engine started")
    while True:
        if store.collector_paused("paper"):
            time.sleep(5)
            continue
        try:
            tick()
            store.collector_report("paper", success=True)
        except Exception as exc:
            print("[paper] tick error:", exc)
            store.collector_report("paper", success=False, error=str(exc))
        time.sleep(5)


def reset(user_id: int, account_id: int) -> None:
    with _lock:
        _account(user_id, account_id)
        store.execute(
            "DELETE FROM paper_positions WHERE paper_account_id=? AND user_id=?",
            (account_id, user_id),
        )
        store.execute(
            "DELETE FROM paper_trades WHERE paper_account_id=? AND user_id=?",
            (account_id, user_id),
        )
        store.execute(
            "DELETE FROM paper_order_executions WHERE paper_account_id=? AND user_id=?",
            (account_id, user_id),
        )
        store.execute(
            "DELETE FROM paper_equity WHERE paper_account_id=? AND user_id=?",
            (account_id, user_id),
        )
        store.execute(
            "DELETE FROM user_states WHERE user_id=? AND k LIKE ?",
            (user_id, f"paper:{account_id}:%"),
        )
        store.execute(
            """UPDATE paper_accounts SET balance=initial_balance,started_at=CURRENT_TIMESTAMP,
                      last_tick_at=NULL,updated_at=CURRENT_TIMESTAMP
               WHERE id=? AND user_id=?""",
            (account_id, user_id),
        )


def api_data(
    user_id: int, account_id: int, timezone_offset_minutes: int = 0
) -> dict[str, Any]:
    account = _account(user_id, account_id)
    prices = _prices()
    positions = _positions(account)
    equity, unrealized = _equity(account, prices, positions)
    config = _config(account)
    now = int(time.time())
    today_pnl = _today_pnl(account, equity, now, timezone_offset_minutes)
    output_positions = []
    for position in positions:
        price = prices.get(position["symbol"], float(position["avg_entry"]))
        price_pnl = _upnl(position, price)
        funding = float(position.get("funding_acc") or 0)
        upnl = price_pnl - funding
        margin = float(position["margin"])
        basis = _json_object(position.get("basis"))
        output_positions.append(
            {
                **position,
                "price": price,
                "upnl": round(upnl, 2),
                "price_pnl": round(price_pnl, 2),
                "funding": round(funding, 8),
                "pnl_pct": round(upnl / margin * 100 if margin else 0, 2),
                "hold_h": round((now - int(position["opened_ts"])) / 3600, 1),
                "reasons": basis.get("reasons", []),
            }
        )
    trades = []
    for row in store.query(
            """SELECT * FROM paper_trades WHERE paper_account_id=? AND user_id=?
               ORDER BY closed_ts DESC LIMIT 100""",
            (account_id, user_id),
        ):
        trade = dict(row)
        entry_basis = _json_object(trade.get("entry_basis_json"))
        if not entry_basis:
            entry_basis = {
                "schema_version": 1,
                "availability": "legacy_missing",
                "reasons": ["历史成交未保存开仓依据，无法可靠复原"],
            }
        try:
            quantity = abs(float(trade.get("qty") or 0))
            entry_price = float(trade.get("entry_price") or 0)
            exit_price = float(trade.get("exit_price") or 0)
            side = int(trade.get("side") or 0)
            funding = float(trade.get("funding") or 0)
            margin = max(float(trade.get("margin") or 0), 0.0)
            price_pnl = (exit_price - entry_price) * quantity * side
            pnl_before_fees = max(price_pnl - funding, -margin)
        except (TypeError, ValueError, OverflowError):
            price_pnl = float(trade.get("pnl") or 0)
            pnl_before_fees = price_pnl
        execution = entry_basis.get("execution")
        captured_entry_fee = execution.get("entry_fee") if isinstance(execution, dict) else None
        try:
            entry_fee = float(captured_entry_fee)
        except (TypeError, ValueError, OverflowError):
            entry_fee = (
                abs(float(trade.get("qty") or 0) * float(trade.get("entry_price") or 0))
                * float(config["fee_bps"])
                / 10_000
            )
        exit_fee = max(float(trade.get("fee") or 0), 0.0)
        total_fee = max(entry_fee, 0.0) + exit_fee
        trade["entry_basis"] = entry_basis
        trade.update(
            {
                "price_pnl": price_pnl,
                "pnl": pnl_before_fees,
                "entry_fee": max(entry_fee, 0.0),
                "exit_fee": exit_fee,
                "fee": total_fee,
                "net_pnl": pnl_before_fees - total_fee,
            }
        )
        trades.append(trade)
    trade_count_rows = store.query(
        """SELECT COUNT(*) AS total FROM paper_trades
           WHERE paper_account_id=? AND user_id=?""",
        (account_id, user_id),
    )
    try:
        closed_trade_count = int(trade_count_rows[0]["total"])
    except (IndexError, KeyError, TypeError, ValueError, OverflowError):
        closed_trade_count = len(trades)
    curve_rows = store.query(
        """SELECT ts,equity FROM paper_equity WHERE paper_account_id=? AND user_id=?
           ORDER BY ts DESC LIMIT 2880""",
        (account_id, user_id),
    )
    curve = [(row["ts"], row["equity"]) for row in reversed(curve_rows)]
    wins = [trade for trade in trades if float(trade.get("net_pnl") or 0) > 0]
    losses = [trade for trade in trades if float(trade.get("net_pnl") or 0) <= 0]
    gross_win = sum(float(trade.get("net_pnl") or 0) for trade in wins)
    gross_loss = abs(sum(float(trade.get("net_pnl") or 0) for trade in losses))
    initial = float(account["initial_balance"])
    peak = initial
    drawdown = 0.0
    for _, value in curve:
        peak = max(peak, float(value))
        if peak:
            drawdown = max(drawdown, (peak - float(value)) / peak * 100)
    strategies = _strategy_snapshots(account)
    strategy = strategies[0] if strategies else {}
    strategy_ids = [str(item["public_id"]) for item in strategies if item.get("public_id")]
    strategy_names = [str(item["name"]) for item in strategies if item.get("name")]
    signal_mode = _paper_signal_mode(account)
    entry_count = closed_trade_count + sum(
        1 + max(int(position.get("adds") or 0), 0) for position in positions
    )
    entry_count += sum(
        len(trade.get("entry_basis", {}).get("additions", []))
        for trade in trades
        if isinstance(trade.get("entry_basis"), dict)
        and isinstance(trade["entry_basis"].get("additions"), list)
    )
    return {
        "paper_account": {
            "id": account["public_id"],
            "name": account["name"],
            "status": account["status"],
            "strategy_id": strategy.get("public_id"),
            "strategy_name": " + ".join(strategy_names) or strategy.get("name"),
            "strategy_ids": strategy_ids,
            "strategy_names": strategy_names,
            "strategies": [
                {
                    "id": item.get("public_id"),
                    "name": item.get("name"),
                    "engine_key": item.get("engine_key"),
                    "strategy_kind": item.get("strategy_kind"),
                    "version": item.get("version"),
                }
                for item in strategies
            ],
            "combination_mode": "all",
            "engine_key": strategy.get("engine_key"),
            "signal_mode": signal_mode,
        },
        "account": {
            "start": initial,
            "balance": round(float(account["balance"]), 2),
            "equity": round(equity, 2),
            "used_margin": round(_used_margin(positions), 2),
            "margin_usage": round(_used_margin(positions) / equity * 100 if equity else 0, 2),
            "upnl": round(unrealized, 2),
            "today_pnl": round(today_pnl, 2),
            "ret_pct": round((equity - initial) / initial * 100, 2),
            "leverage": config["leverage"],
            "max_positions": config["max_positions"],
            "started_ts": int(account["started_at"].replace(tzinfo=UTC).timestamp()),
        },
        "positions": output_positions,
        "trades": trades[:50],
        "curve": curve[-1440:],
        "stats": {
            "entries": entry_count,
            "trades": len(trades),
            "closed_total": closed_trade_count,
            "win_rate": round(len(wins) / len(trades) * 100 if trades else 0, 2),
            "profit_factor": round(gross_win / gross_loss, 2) if gross_loss else None,
            "max_drawdown": round(drawdown, 2),
            "realized": round(
                sum(float(trade.get("net_pnl") or 0) for trade in trades),
                2,
            ),
            "wins": len(wins),
            "losses": len(losses),
        },
        "rules": {
            "tiers": (
                "老版兼容：4h 评分达到 ±60，并通过 MA150 趋势过滤"
                if signal_mode == LEGACY_PAPER_SIGNAL_MODE
                else (
                    f"组合策略（全部满足才开仓）：{' + '.join(strategy_names)}"
                    if len(strategies) > 1
                    else f"绑定策略：{strategy.get('name') or strategy.get('engine_key')}"
                )
            ),
            "exits": "1.5×ATR 止损 / 2.5×ATR 止盈 / 策略反转 / 最大持仓周期 / 强平",
            "costs": (
                f"手续费 {config['fee_bps']} bps + 滑点 {config['slippage_bps']} bps"
                f" + 资金费率估算 {config['funding_rate_8h_bps']} bps/8h"
            ),
            "limits": f"逐仓 {config['leverage']}x / 最多 {config['max_positions']} 仓",
        },
        "disclaimer": "模拟交易仅用于策略验证与学习，不构成投资建议。",
    }
