"""Fail-closed Binance live strategy executor.

The worker consumes the same immutable strategy snapshots as the paper engine,
but exchange balances, positions and orders remain authoritative.  Every order
is preceded by a unique local intent so a repeated strategy tick cannot emit a
second Binance order.
"""

from __future__ import annotations

import hashlib
import json
import math
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from . import macro_market
from . import market_store as store
from .ai_monitor import PREDICTION_SETTLEMENT_VERSION
from .application.execution_service import deterministic_client_order_id
from .application.live_recovery import (
    LiveAccountRecoveryService,
    LiveOrderReconciliationService,
    LiveOrderStatePending,
    LiveOrderStateUnknown,
    LivePositionSyncService,
    ProtectionRecoveryService,
)
from .application.protection import ProtectionInstallationError, ProtectionService
from .application.risk import RiskPolicy
from .application.strategy_execution import (
    ENTRY_BASIS_SCHEMA_VERSION,
    evaluate_account_strategy,
    record_strategy_decision,
)
from .application.strategy_execution import (
    build_entry_basis_snapshot as build_shared_entry_basis_snapshot,
)
from .binance_client import BinanceAccountClientError
from .binance_service import BinanceAccountService
from .binance_trading import BinanceUsdMTradingClient
from .config import Settings
from .domain.execution import (
    ExecutionMode,
    ExecutionState,
    IntentAction,
    OrderIntent,
)
from .domain.exit_policy import DEFAULT_EXIT_POLICY
from .domain.protection import ProtectionPlan
from .domain.trading import OrderSide, PositionSide
from .infrastructure.binance_broker import BinanceBroker
from .infrastructure.live_execution import LiveExecutionRuntime
from .live_risk import (
    OpenPositionRisk,
    account_loss_limits,
    atr_risk_position_size,
    closed_bar_signal_freshness,
    leverage_for_stop_distance,
    liquidation_stop_safety,
    market_data_freshness,
    policy_from_config,
    signal_freshness,
    symbol_admission,
    symbol_risk_profile,
    tighten_policy_with_strategy,
    total_open_risk,
)
from .market_config import tradfi_live_symbols
from .market_microstructure import order_book_gate_snapshot
from .security import CredentialCipher, SecurityError
from .strategy_evaluator import (
    StrategyEvaluationError,
    resolve_strategy_timing_policy,
)
from .trading_controls import effective_control_blockers

_settings: Settings | None = None
_account_service: BinanceAccountService | None = None
_trading_client: BinanceUsdMTradingClient | None = None
_started = False
_start_lock = threading.Lock()
_reconcile_lock = threading.Lock()
_execution_locks: dict[int, threading.Lock] = {}
_last_reconciled_at: dict[int, float] = {}
_reconciliation_failed: set[int] = set()
_RECONCILE_INTERVAL_SECONDS = 60.0
_position_mode_cache: dict[int, tuple[str, float]] = {}
_POSITION_MODE_TTL_SECONDS = 600.0
_account_backoff: dict[int, tuple[float, int]] = {}
_MAX_ACCOUNT_BACKOFF_SECONDS = 300.0
_LIVE_PROFIT_GUARD_VERSION = "risk_unit_live_guard_v1"
_LIVE_PROFIT_GUARD_ACTIVATION_R = 0.5
_LIVE_PROFIT_GUARD_TRAILING_ACTIVATION_R = 1.0
_LIVE_PROFIT_GUARD_GIVEBACK_R = 0.5
_LIVE_PROFIT_GUARD_PEAK_WRITE_STEP_R = 0.1
_LIVE_PROFIT_GUARD_COST_BUFFER_BPS = 2.0


@dataclass(frozen=True, slots=True)
class _PositionSizingCapital:
    """Normalized capital bases used by the live order sizing path."""

    basis: str
    configured_total_amount: Decimal | None
    effective_equity: Decimal
    margin_equity: Decimal


def configure(
    settings: Settings,
    account_service: BinanceAccountService,
    trading_client: BinanceUsdMTradingClient,
) -> None:
    global _settings, _account_service, _trading_client
    _settings = settings
    _account_service = account_service
    _trading_client = trading_client
    with _reconcile_lock:
        _last_reconciled_at.clear()
        _reconciliation_failed.clear()
        _position_mode_cache.clear()
        _account_backoff.clear()
        _execution_locks.clear()


def _account_execution_lock(account_id: int) -> threading.Lock:
    """Serialize manual and scheduled entry decisions for one live account."""

    with _reconcile_lock:
        lock = _execution_locks.get(account_id)
        if lock is None:
            lock = threading.Lock()
            _execution_locks[account_id] = lock
        return lock


def start() -> None:
    global _started
    with _start_lock:
        if _started or _settings is None:
            return
        _started = True
        threading.Thread(target=live_loop, daemon=True, name="binance-live").start()


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError, json.JSONDecodeError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _strategy_universe(config: dict[str, Any]) -> list[str]:
    """Return the server-owned paper universe, narrowed only by live preflight."""
    universe = tradfi_live_symbols()
    eligible = config.get("eligible_symbols")
    if not isinstance(eligible, list):
        return universe
    eligible_set = {str(value).upper() for value in eligible}
    return [symbol for symbol in universe if symbol in eligible_set]


def _utc_seconds(value: Any) -> float | None:
    if isinstance(value, datetime):
        normalized = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
        return normalized.timestamp()
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        normalized = parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)
        return normalized.timestamp()
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        parsed = float(value)
        while parsed >= 100_000_000_000:
            parsed /= 1000
        return parsed if math.isfinite(parsed) else None
    return None


def _current_order_book_gate(symbol: str, direction: str) -> dict[str, Any]:
    """Load and evaluate the latest Binance book immediately before auto entry."""

    try:
        rows = store.query(
            """SELECT symbol,bid_depth_notional,ask_depth_notional,
                      bid_depth_notional_5,ask_depth_notional_5,
                      book_imbalance,book_imbalance_5,depth_levels,
                      bid_level_count,ask_level_count,spread_bps,
                      bid_depth_change_5s_pct,ask_depth_change_5s_pct,
                      bid_depth_change_30s_pct,ask_depth_change_30s_pct,
                      imbalance_change_5s,ts
               FROM market_microstructure WHERE symbol=? LIMIT 1""",
            (str(symbol or "").upper(),),
        )
    except Exception:
        rows = []
    return order_book_gate_snapshot(
        dict(rows[0]) if rows else {},
        direction=direction,
        now_seconds=int(time.time()),
    )


def _ai_monitor_signal(
    account: dict[str, Any],
    symbol: str,
    *,
    price: float | None = None,
    prediction_public_id: str | None = None,
    opportunity_public_id: str | None = None,
) -> tuple[int, float | None, list[str], int | None, dict[str, Any]]:
    """Return an automatic prediction or one manually selected opportunity.

    This read path never writes or places an order.  The normal live executor
    remains responsible for signal freshness, account limits, position sizing,
    idempotency, fill verification and exchange-native protection orders.
    """

    config = account.get("config_json")
    config = config if isinstance(config, dict) else {}
    if not bool(config.get("ai_monitor_live_copy_enabled")):
        return 0, None, [], None, {}
    if bool(config.get("ai_monitor_live_regular_session_only", True)):
        execution_now = datetime.fromtimestamp(time.time(), tz=UTC)
        session_key = str(macro_market.us_market_session(execution_now).get("key") or "closed")
        if session_key != "regular":
            return 0, None, [], None, {}
    enabled_at = _utc_seconds(config.get("ai_monitor_live_copy_enabled_at"))
    if enabled_at is None:
        return 0, None, [], None, {}
    enabled_datetime = datetime.fromtimestamp(enabled_at, tz=UTC).replace(tzinfo=None)
    if prediction_public_id is not None and opportunity_public_id is None:
        return 0, None, [], None, {}
    manual_selection = opportunity_public_id is not None
    query_now = datetime.fromtimestamp(time.time(), tz=UTC).replace(tzinfo=None)
    if manual_selection and prediction_public_id is not None:
        rows = store.query(
            """SELECT p.public_id AS prediction_public_id,
                      o.public_id AS opportunity_public_id,
                      p.direction,p.timeframe,p.confidence_score,p.entry_price,
                      p.evidence_json,p.predicted_at,p.due_at,o.expires_at,
                      p.readiness_status,p.estimated_cost_bps,
                      p.expected_edge_lower_bound_bps
               FROM ai_monitor_predictions p
               JOIN ai_monitor_opportunities o
                 ON o.id=p.opportunity_id AND o.user_id=p.user_id
               WHERE p.user_id=? AND p.contract_symbol=? AND p.status='pending'
                  AND p.settlement_version=?
                  AND p.public_id=? AND o.public_id=?
                  AND o.status IN ('candidate','discovered')
                ORDER BY p.predicted_at DESC,p.id DESC LIMIT 1""",
            (
                account["user_id"],
                symbol,
                PREDICTION_SETTLEMENT_VERSION,
                prediction_public_id,
                opportunity_public_id,
            ),
        )
    elif manual_selection:
        rows = store.query(
            """SELECT NULL AS prediction_public_id,
                      o.public_id AS opportunity_public_id,
                      o.direction,o.timeframe,o.combined_score AS confidence_score,
                      NULL AS entry_price,o.evidence_json,
                      o.discovered_at AS predicted_at,
                      o.expires_at AS due_at,o.expires_at,
                      NULL AS readiness_status,NULL AS estimated_cost_bps,
                      NULL AS expected_edge_lower_bound_bps
               FROM ai_monitor_opportunities o
               WHERE o.user_id=? AND o.contract_symbol=? AND o.public_id=?
                 AND o.status IN ('candidate','discovered')
               ORDER BY o.discovered_at DESC,o.id DESC LIMIT 1""",
            (account["user_id"], symbol, opportunity_public_id),
        )
    else:
        rows = store.query(
            """SELECT p.public_id AS prediction_public_id,
                      o.public_id AS opportunity_public_id,
                      p.direction,p.timeframe,p.confidence_score,p.entry_price,
                      p.evidence_json,p.predicted_at,p.due_at,o.expires_at,
                      p.readiness_status,p.estimated_cost_bps,
                      p.expected_edge_lower_bound_bps
               FROM ai_monitor_predictions p
               JOIN ai_monitor_opportunities o
                 ON o.id=p.opportunity_id AND o.user_id=p.user_id
                WHERE p.user_id=? AND p.contract_symbol=? AND p.status='pending'
                  AND p.settlement_version=?
                  AND o.status IN ('candidate','discovered')
                  AND o.expires_at>? AND p.predicted_at>=?
                ORDER BY p.predicted_at DESC,p.id DESC LIMIT 1""",
            (
                account["user_id"],
                symbol,
                PREDICTION_SETTLEMENT_VERSION,
                query_now,
                enabled_datetime,
            ),
        )
    if not rows:
        return 0, None, [], None, {}
    row = dict(rows[0])
    evidence = _json_object(row.get("evidence_json"))
    gate = evidence.get("virtual_entry_gate")
    if not isinstance(gate, dict):
        return 0, None, [], None, {}
    entry_ready = gate.get("entry_ready") is True
    if not entry_ready and not manual_selection:
        return 0, None, [], None, {}
    direction_name = str(row.get("direction") or "").lower()
    direction = 1 if direction_name == "long" else -1 if direction_name == "short" else 0
    if direction == 0 or str(gate.get("direction") or direction_name) != direction_name:
        return 0, None, [], None, {}
    if direction > 0 and not bool(config.get("ai_monitor_live_allow_long", True)):
        return 0, None, [], None, {}
    if direction < 0 and not bool(config.get("ai_monitor_live_allow_short", True)):
        return 0, None, [], None, {}
    execution_order_book: dict[str, Any] | None = None
    if not manual_selection:
        execution_order_book = _current_order_book_gate(symbol, direction_name)
        if not (
            execution_order_book.get("passed") is True
            and execution_order_book.get("confirms_direction") is True
        ):
            return 0, None, [], None, {}
    market_environment = evidence.get("market_environment")
    market_environment = (
        market_environment if isinstance(market_environment, dict) else {}
    )
    macro_policy = evidence.get("macro_entry_policy")
    if not isinstance(macro_policy, dict):
        macro_policy = market_environment.get("entry_policy")
    macro_policy = macro_policy if isinstance(macro_policy, dict) else {}
    macro_allowed_key = "entry_allowed" if manual_selection else "live_entry_allowed"
    macro_entry_allowed = bool(
        macro_policy.get(
            macro_allowed_key,
            macro_policy.get("entry_allowed", True),
        )
    )
    if not macro_entry_allowed:
        return 0, None, [], None, {}
    try:
        macro_position_multiplier = float(
            macro_policy.get("position_multiplier", 1.0)
        )
    except (TypeError, ValueError):
        macro_position_multiplier = 1.0
    macro_position_multiplier = max(0.0, min(1.0, macro_position_multiplier))
    if macro_position_multiplier <= 0:
        return 0, None, [], None, {}
    try:
        combined_score = float(row.get("confidence_score"))
    except (TypeError, ValueError):
        return 0, None, [], None, {}
    readiness = evidence.get("live_readiness")
    readiness = readiness if isinstance(readiness, dict) else {}
    readiness_checks = readiness.get("checks")
    readiness_checks = readiness_checks if isinstance(readiness_checks, dict) else {}
    required_readiness_checks = {
        "indicator_policy_passed",
        "indicator_strength",
        "combined_score",
        "macro_entry_policy",
        "market_quality",
        "market_flow_available",
        "market_flow_freshness",
        "market_flow_quality",
        "calibration_samples",
        "cost_stress_edge",
    }
    try:
        expected_edge_lower_bound_bps = float(
            row.get("expected_edge_lower_bound_bps")
        )
        estimated_cost_bps = float(row.get("estimated_cost_bps"))
        required_gross_edge_bps = max(
            estimated_cost_bps + float(readiness.get("safety_margin_bps") or 0.0),
            float(readiness.get("required_gross_edge_bps") or 0.0),
        )
    except (TypeError, ValueError, OverflowError):
        expected_edge_lower_bound_bps = math.nan
        required_gross_edge_bps = math.inf
    persisted_edge_passed = bool(
        math.isfinite(expected_edge_lower_bound_bps)
        and math.isfinite(required_gross_edge_bps)
        and expected_edge_lower_bound_bps > required_gross_edge_bps
    )
    automatic_readiness_passed = bool(
        readiness.get("status") == "shadow_ready"
        and row.get("readiness_status") == "shadow_ready"
        and all(readiness_checks.get(key) is True for key in required_readiness_checks)
        and persisted_edge_passed
    )
    if not automatic_readiness_passed and not manual_selection:
        return 0, None, [], None, {}
    minimum_score = max(
        float(config.get("ai_monitor_live_min_combined_score", 70.0)),
        float(readiness.get("minimum_combined_score", 0.0) or 0.0),
    )
    if not math.isfinite(combined_score) or (
        combined_score < minimum_score and not manual_selection
    ):
        return 0, None, [], None, {}
    predicted_at = _utc_seconds(row.get("predicted_at"))
    due_at = _utc_seconds(row.get("due_at"))
    expires_at = _utc_seconds(row.get("expires_at"))
    if predicted_at is None or due_at is None or expires_at is None:
        return 0, None, [], None, {}
    maximum_age = max(
        60,
        min(int(config.get("ai_monitor_live_signal_max_age_seconds", 300)), 1800),
    )
    valid_until = min(predicted_at + maximum_age, due_at, expires_at)
    if time.time() > valid_until and not manual_selection:
        return 0, None, [], None, {}
    risk_plan = evidence.get("risk_plan")
    if not isinstance(risk_plan, dict):
        return 0, None, [], None, {}
    try:
        stop_loss_pct = float(risk_plan["stop_loss_pct"])
        take_profit_pct = float(risk_plan["take_profit_pct"])
        entry_reference = float(row.get("entry_price") or gate.get("reference_price") or 0)
        live_reference = float(price) if price is not None else entry_reference
    except (KeyError, TypeError, ValueError):
        return 0, None, [], None, {}
    if not (
        math.isfinite(live_reference)
        and live_reference > 0
        and math.isfinite(stop_loss_pct)
        and 0 < stop_loss_pct <= 20
        and math.isfinite(take_profit_pct)
        and 0 < take_profit_pct <= 50
    ):
        return 0, None, [], None, {}
    stop_distance = live_reference * stop_loss_pct / 100
    take_profit_distance = live_reference * take_profit_pct / 100
    atr_pct = risk_plan.get("atr_pct")
    try:
        atr = live_reference * float(atr_pct) / 100 if atr_pct is not None else None
    except (TypeError, ValueError):
        atr = None
    if atr is not None and (not math.isfinite(atr) or atr <= 0):
        atr = None
    max_leverage = max(
        1,
        min(
            int(config.get("leverage", 1)),
            int(config.get("risk_max_leverage", config.get("leverage", 1))),
            20,
        ),
    )
    risk_proposal = {
        "stop_distance": stop_distance,
        "take_profit_distance": take_profit_distance,
        "risk_per_trade_pct": float(config.get("risk_per_trade_pct", 0.5))
        * macro_position_multiplier,
        "max_margin_pct": float(
            config.get("max_margin_per_trade_pct", config.get("position_size_pct", 2))
        )
        * macro_position_multiplier,
        "max_leverage": max_leverage,
        "macro_position_multiplier": macro_position_multiplier,
    }
    signal_evidence = {
        "source": "ai_monitor_live_copy_v1",
        "execution_venue": "binance_usdm",
        "execution_price_source": "binance",
        "regular_session_only": bool(
            config.get("ai_monitor_live_regular_session_only", True)
        ),
        "score": combined_score,
        "valid_until": valid_until,
        "prediction_public_id": row.get("prediction_public_id"),
        "opportunity_public_id": row.get("opportunity_public_id"),
        "predicted_at": predicted_at,
        "enabled_at": enabled_at,
        "manual_selection": manual_selection,
        "manual_gate_override": bool(
            manual_selection
            and (
                not entry_ready
                or not automatic_readiness_passed
                or combined_score < minimum_score
            )
        ),
        "automatic_readiness_passed": automatic_readiness_passed,
        "persisted_edge_passed": persisted_edge_passed,
        "expected_edge_lower_bound_bps": (
            expected_edge_lower_bound_bps
            if math.isfinite(expected_edge_lower_bound_bps)
            else None
        ),
        "required_gross_edge_bps": (
            required_gross_edge_bps
            if math.isfinite(required_gross_edge_bps)
            else None
        ),
        "live_readiness": readiness,
        "entry_gate": gate,
        "execution_order_book": execution_order_book,
        "risk_plan": risk_plan,
        "risk_proposal": risk_proposal,
        "macro_policy": macro_policy,
    }
    basis = [
        "信号来源：AI 发现机会",
        f"机会：{row.get('opportunity_public_id')}",
        f"方向：{'做多' if direction > 0 else '做空'}",
        f"组合评分：{combined_score:.1f}",
        (
            "准入：人工确认覆盖研究门槛"
            if manual_selection
            and (
                not entry_ready
                or not automatic_readiness_passed
                or combined_score < minimum_score
            )
            else "准入：全部入场条件已满足"
        ),
    ]
    if row.get("prediction_public_id"):
        basis.insert(1, f"预测：{row['prediction_public_id']}")
    return direction, atr, basis, int(predicted_at), signal_evidence


def _execution_signal(
    account: dict[str, Any],
    symbol: str,
    *,
    price: float | None = None,
) -> tuple[int, float | None, list[str] | None, int | None, dict[str, Any]]:
    config = account.get("config_json")
    config = config if isinstance(config, dict) else {}
    if str(config.get("signal_source") or "strategy") == "ai_monitor":
        if str(config.get("execution_scope") or "") != "ai_monitor":
            # Pre-isolation accounts may still carry the earlier AI source flag.
            # Fail closed instead of resuming either AI or strategy entries.
            return 0, None, [], None, {}
        return _ai_monitor_signal(account, symbol, price=price)
    return _strategy_signal(account, symbol)


def _strategy_signal(
    account: dict[str, Any], symbol: str
) -> tuple[int, float | None, list[str], int | None, dict[str, Any]]:
    """Evaluate live entries through the same public service as paper execution."""

    return evaluate_account_strategy(
        account,
        symbol,
        load_klines=lambda selected_symbol, timeframe, limit: store.get_klines(
            selected_symbol, timeframe, limit
        ),
        record_decision=_record_live_strategy_decision,
    ).execution_tuple()


def _record_live_strategy_decision(
    account: dict[str, Any],
    symbol: str,
    spec: dict[str, Any],
    decision: Any,
    snapshot: dict[str, Any] | None = None,
    envelope: Any = None,
) -> bool:
    return record_strategy_decision(
        account,
        symbol,
        spec,
        decision,
        snapshot,
        envelope,
        query=store.query,
        execute=store.execute,
        log_mode="live",
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
    """Compatibility façade over the mode-neutral entry snapshot builder."""

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
    )


def _strategy_position_side(position_mode: str, direction: int) -> str:
    if position_mode == "hedge":
        return "LONG" if direction > 0 else "SHORT"
    return "BOTH"


def _position_key(position: dict[str, Any]) -> tuple[str, str]:
    symbol = str(position.get("symbol") or "").upper()
    raw_side = str(position.get("position_side") or "BOTH").upper()
    position_side = raw_side if raw_side in {"BOTH", "LONG", "SHORT"} else "BOTH"
    return symbol, position_side


def _active_accounts(account_id: int | None = None) -> list[dict[str, Any]]:
    base_query = """SELECT l.*,d.id AS deployment_id,d.strategy_revision_id,
                           d.runtime_state_json,
                           u.binance_api_key_encrypted,u.binance_api_secret_encrypted,
                           u.binance_key_version,u.binance_permissions,
                           u.binance_physical_account_id
                    FROM live_trading_accounts l
                    JOIN strategy_deployments d
                      ON d.user_id=l.user_id AND d.mode='live' AND d.target_account_id=l.id
                    JOIN users u ON u.id=l.user_id AND u.is_active=1
                    WHERE l.status='active' AND d.status='running'"""
    if account_id is None:
        rows = store.query(base_query + " ORDER BY l.id")
    else:
        rows = store.query(base_query + " AND l.id=? ORDER BY l.id", (account_id,))
    accounts: list[dict[str, Any]] = []
    for row in rows:
        account = dict(row)
        account["config_json"] = _json_object(account.get("config_json"))
        account["strategy_snapshot_json"] = _json_object(account.get("strategy_snapshot_json"))
        account["deployment_mode"] = "live"
        if (
            _settings is not None
            and not _settings.binance_live_trading_enabled
            and str(account["config_json"].get("execution_scope") or "")
            != "ai_monitor"
        ):
            continue
        accounts.append(account)
    return accounts


def _recovery_accounts() -> list[dict[str, Any]]:
    """Return stopped accounts whose crash-safe order intents still need resolution."""
    rows = store.query(
        """SELECT l.*,d.id AS deployment_id,d.strategy_revision_id,
                  d.runtime_state_json,
                  u.binance_api_key_encrypted,u.binance_api_secret_encrypted,
                  u.binance_key_version,u.binance_permissions,
                  u.binance_physical_account_id
           FROM live_trading_accounts l
           JOIN strategy_deployments d
             ON d.user_id=l.user_id AND d.mode='live' AND d.target_account_id=l.id
           JOIN users u ON u.id=l.user_id AND u.is_active=1
           WHERE l.status IN ('paused','error')
             AND (
                 EXISTS(
                     SELECT 1 FROM live_order_intents i
                     WHERE i.user_id=l.user_id AND i.live_account_id=l.id
                       AND (
                           i.status IN ('created','unknown')
                           OR (
                               i.action IN ('open','close')
                               AND i.status='submitted'
                           )
                       )
                 )
                 OR (
                     l.status='error'
                     AND EXISTS(
                         SELECT 1 FROM live_order_intents p
                         WHERE p.user_id=l.user_id AND p.live_account_id=l.id
                           AND p.action IN ('stop','take_profit')
                           AND p.status='submitted'
                     )
                 )
                 OR (
                     l.status='error'
                     AND EXISTS(
                         SELECT 1 FROM live_order_intents o
                         WHERE o.user_id=l.user_id AND o.live_account_id=l.id
                           AND o.action='open' AND o.status='filled'
                           AND NOT EXISTS(
                               SELECT 1 FROM live_order_intents c
                               WHERE c.user_id=o.user_id
                                 AND c.live_account_id=o.live_account_id
                                 AND c.symbol=o.symbol
                                 AND c.position_side=o.position_side
                                 AND c.action='close' AND c.status='filled'
                                 AND c.id>o.id
                           )
                     )
                 )
             )
           ORDER BY l.id"""
    )
    accounts: list[dict[str, Any]] = []
    for row in rows:
        account = dict(row)
        account["config_json"] = _json_object(account.get("config_json"))
        account["strategy_snapshot_json"] = _json_object(account.get("strategy_snapshot_json"))
        account["deployment_mode"] = "live"
        if (
            _settings is not None
            and not _settings.binance_live_trading_enabled
            and str(account["config_json"].get("execution_scope") or "")
            != "ai_monitor"
        ):
            continue
        accounts.append(account)
    return accounts


def _credentials(account: dict[str, Any]) -> tuple[str, str]:
    if _settings is None:
        raise RuntimeError("live engine is not configured")
    if int(account["credential_version"]) != int(account["binance_key_version"]):
        raise SecurityError("credential version changed")
    cipher = CredentialCipher(_settings.credential_master_key.get_secret_value())
    return (
        cipher.decrypt(str(account.get("binance_api_key_encrypted") or "")),
        cipher.decrypt(str(account.get("binance_api_secret_encrypted") or "")),
    )


def _cached_position_mode(account: dict[str, Any], api_key: str, api_secret: str) -> str:
    """Check the high-weight position-mode endpoint at most once per ten minutes."""

    if _trading_client is None:
        raise RuntimeError("live engine is not configured")
    account_id = int(account["id"])
    now = time.monotonic()
    with _reconcile_lock:
        cached = _position_mode_cache.get(account_id)
    if cached is not None and now < cached[1]:
        return cached[0]
    mode = _trading_client.position_mode(api_key, api_secret)
    with _reconcile_lock:
        _position_mode_cache[account_id] = (mode, now + _POSITION_MODE_TTL_SECONDS)
    return mode


def _client_id(account_id: int, key: str) -> str:
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:24]
    return f"qd{account_id:x}-{digest}"[:36]


def _safe_response(payload: dict[str, Any]) -> dict[str, Any]:
    allowed = {
        "orderId",
        "clientOrderId",
        "symbol",
        "status",
        "type",
        "side",
        "positionSide",
        "reduceOnly",
        "closePosition",
        "avgPrice",
        "origQty",
        "executedQty",
        "updateTime",
        "algoId",
        "clientAlgoId",
        "algoType",
        "algoStatus",
        "orderType",
        "triggerPrice",
    }
    return {key: payload[key] for key in allowed if key in payload}


def _create_intent(
    account: dict[str, Any],
    *,
    signal_key: str,
    symbol: str,
    action: str,
    side: str,
    position_side: str,
    order_type: str,
    quantity: Decimal | None,
    request_json: dict[str, Any],
    strategy_signal_id: int | None = None,
    entry_basis: dict[str, Any] | None = None,
    client_order_id: str | None = None,
) -> dict[str, Any] | None:
    client_id = client_order_id or _client_id(int(account["id"]), signal_key)
    created = store.execute(
        """INSERT IGNORE INTO live_order_intents(
               public_id,user_id,live_account_id,deployment_id,signal_key,client_order_id,
               symbol,action,side,position_side,order_type,quantity,status,request_json,
               strategy_signal_id,entry_basis_json,created_at,updated_at
           ) VALUES(UUID(),?,?,?,?,?,?,?,?,?,?,?,'created',?,?,?,UTC_TIMESTAMP(),UTC_TIMESTAMP())""",
        (
            account["user_id"],
            account["id"],
            account["deployment_id"],
            signal_key,
            client_id,
            symbol,
            action,
            side,
            position_side,
            order_type,
            quantity,
            json.dumps(request_json, ensure_ascii=False),
            strategy_signal_id,
            json.dumps(entry_basis, ensure_ascii=False) if entry_basis else None,
        ),
    )
    if created != 1:
        return None
    rows = store.query(
        "SELECT * FROM live_order_intents WHERE signal_key=? AND user_id=?",
        (signal_key, account["user_id"]),
    )
    return dict(rows[0]) if rows else None


def _update_intent(
    intent_id: int,
    user_id: int,
    *,
    status: str,
    response: dict[str, Any] | None = None,
    error_code: str | None = None,
) -> None:
    order_id = None
    if response is not None:
        exchange_id = response.get("orderId", response.get("algoId"))
        if exchange_id is not None:
            order_id = str(exchange_id)
    store.execute(
        """UPDATE live_order_intents
           SET status=?,binance_order_id=COALESCE(?,binance_order_id),response_json=?,
               error_code=?,submitted_at=COALESCE(submitted_at,UTC_TIMESTAMP()),
               updated_at=UTC_TIMESTAMP()
           WHERE id=? AND user_id=?""",
        (
            status,
            order_id,
            json.dumps(_safe_response(response), ensure_ascii=False) if response else None,
            error_code,
            intent_id,
            user_id,
        ),
    )


def _fail_account(account: dict[str, Any], code: str) -> None:
    store.execute(
        """UPDATE live_trading_accounts
           SET status='error',last_error_code=?,updated_at=UTC_TIMESTAMP()
           WHERE id=? AND user_id=?""",
        (code[:64], account["id"], account["user_id"]),
    )
    store.execute(
        """UPDATE strategy_deployments SET status='error',last_error_code=?,updated_at=UTC_TIMESTAMP()
           WHERE id=? AND user_id=?""",
        (code[:64], account["deployment_id"], account["user_id"]),
    )


def _execution_enabled(account: dict[str, Any], symbol: str) -> bool:
    """Fence every exchange write against a concurrent pause/error transition."""
    rows = store.query(
        """SELECT l.public_id AS live_account_public_id,
                  s.public_id AS strategy_public_id,
                  r.version AS strategy_revision_version
           FROM live_trading_accounts l
           JOIN strategy_deployments d
             ON d.id=? AND d.user_id=l.user_id AND d.mode='live'
                AND d.target_account_id=l.id
           JOIN strategy_revisions r
             ON r.id=d.strategy_revision_id AND r.user_id=d.user_id
           JOIN user_strategies s
             ON s.id=r.user_strategy_id AND s.user_id=r.user_id
           WHERE l.id=? AND l.user_id=?
             AND l.status='active' AND d.status='running'
             AND (
                 JSON_UNQUOTE(JSON_EXTRACT(l.config_json, '$.execution_scope'))='ai_monitor'
                 OR r.lifecycle_status IN ('micro_live','live')
             )
           LIMIT 1""",
        (account["deployment_id"], account["id"], account["user_id"]),
    )
    if not rows:
        return False
    binding = dict(rows[0])
    try:
        blockers = effective_control_blockers(
            store.query,
            owner_user_id=int(account["user_id"]),
            account_public_id=str(binding["live_account_public_id"]),
            strategy_public_id=str(binding["strategy_public_id"]),
            strategy_revision_version=int(binding["strategy_revision_version"]),
            symbol=symbol,
        )
    except Exception as exc:
        # A missing/corrupt control table must never silently re-enable entries.
        print(f"[live] trading control lookup failed: {type(exc).__name__}")
        return False
    if blockers:
        account["_trading_control_blockers"] = blockers
        return False
    account.pop("_trading_control_blockers", None)
    return True


def _safety_write_enabled(account: dict[str, Any]) -> bool:
    """Allow only risk-reducing writes after pause/error, never after archival."""
    rows = store.query(
        """SELECT 1
           FROM live_trading_accounts l
           JOIN strategy_deployments d
             ON d.id=? AND d.user_id=l.user_id AND d.mode='live'
                AND d.target_account_id=l.id
           WHERE l.id=? AND l.user_id=?
             AND l.status IN ('active','paused','error')
             AND d.status IN ('running','paused','error')
           LIMIT 1""",
        (account["deployment_id"], account["id"], account["user_id"]),
    )
    return bool(rows)


def _exchange_intent_status(response: dict[str, Any]) -> str:
    """Map regular/algo Binance states into the deliberately small local state set."""
    raw = str(
        response.get("algoStatus") or response.get("strategyStatus") or response.get("status") or ""
    ).upper()
    if raw in {
        "NEW",
        "PENDING_NEW",
        "PARTIALLY_FILLED",
        "ACCEPTED",
        "WORKING",
        "TRIGGERING",
    }:
        return "submitted"
    if raw in {"FILLED", "TRIGGERED", "FINISHED"}:
        return "filled"
    if raw in {"CANCELED", "CANCELLED", "EXPIRED", "EXPIRED_IN_MATCH"}:
        return "canceled"
    if raw == "REJECTED":
        return "rejected"
    return "unknown"


def _normalized_open_order_response(order: dict[str, Any]) -> dict[str, Any]:
    """Convert BinanceAccountService's normalized open order into audit fields."""
    conditional = order.get("conditional") is True
    response: dict[str, Any] = {
        "symbol": order.get("symbol"),
        "side": order.get("side"),
        "positionSide": order.get("position_side"),
    }
    if conditional:
        response.update(
            {
                "algoId": order.get("order_id"),
                "clientAlgoId": order.get("client_order_id"),
                "algoStatus": order.get("status") or "NEW",
                "orderType": order.get("type"),
                "triggerPrice": order.get("stop_price"),
            }
        )
    else:
        response.update(
            {
                "orderId": order.get("order_id"),
                "clientOrderId": order.get("client_order_id"),
                "status": order.get("status") or "NEW",
                "type": order.get("type"),
                "avgPrice": order.get("average_price"),
            }
        )
    return response


def _reconciliation_due(account_id: int, *, force: bool = False) -> bool:
    now = time.monotonic()
    with _reconcile_lock:
        previous = _last_reconciled_at.get(account_id)
        if not force and previous is not None and now - previous < _RECONCILE_INTERVAL_SECONDS:
            if account_id in _reconciliation_failed:
                raise BinanceAccountClientError("reconciliation_backoff")
            return False
        # Reserve the interval before I/O so concurrent callers cannot duplicate
        # the two high-weight all-symbol open-order requests.
        _last_reconciled_at[account_id] = now
    return True


def _finish_reconciliation(account_id: int, *, successful: bool) -> None:
    with _reconcile_lock:
        if successful:
            _reconciliation_failed.discard(account_id)
        else:
            _reconciliation_failed.add(account_id)


def _request_reconciliation(account_id: int) -> None:
    """Make a newly unknown local intent eligible for immediate recovery."""
    with _reconcile_lock:
        _last_reconciled_at.pop(account_id, None)
        _reconciliation_failed.discard(account_id)


def _account_backoff_active(account_id: int) -> bool:
    with _reconcile_lock:
        state = _account_backoff.get(account_id)
    return state is not None and time.monotonic() < state[0]


def _record_account_backoff(account_id: int, category: str) -> None:
    if category not in {"rate_limit", "network", "timeout", "upstream"}:
        return
    now = time.monotonic()
    with _reconcile_lock:
        previous = _account_backoff.get(account_id)
        failures = (previous[1] if previous is not None else 0) + 1
        base = 60.0 if category == "rate_limit" else 30.0
        delay = min(_MAX_ACCOUNT_BACKOFF_SECONDS, base * (2 ** (failures - 1)))
        _account_backoff[account_id] = (now + delay, failures)


def _clear_account_backoff(account_id: int) -> None:
    with _reconcile_lock:
        _account_backoff.pop(account_id, None)


def _reconcile_intents(
    account: dict[str, Any], api_key: str, api_secret: str, *, force: bool = False
) -> bool:
    """Resolve non-terminal intents through the application reconciliation service."""
    if _account_service is None or _trading_client is None:
        raise RuntimeError("live engine is not configured")
    if not _reconciliation_due(int(account["id"]), force=force):
        return False
    service = LiveOrderReconciliationService(
        load_intents=lambda user_id, account_id: store.query(
            """SELECT id,user_id,symbol,action,status,client_order_id
               FROM live_order_intents
               WHERE user_id=? AND live_account_id=?
                 AND status IN ('created','submitted','unknown')
               ORDER BY id""",
            (user_id, account_id),
        ),
        load_open_orders=lambda key, secret: _account_service.open_orders(
            key,
            secret,
            account_type="UM_FUTURE",
            force_refresh=True,
        ),
        query_market_order=lambda key, secret, symbol, client_id: (
            _trading_client.query_order(
                key,
                secret,
                symbol=symbol,
                client_order_id=client_id,
            )
        ),
        query_protection_order=lambda key, secret, client_id: (
            _trading_client.query_algo_order(
                key, secret, client_order_id=client_id
            )
        ),
        update_intent=_update_intent,
        normalize_open_order=_normalized_open_order_response,
        classify_status=_exchange_intent_status,
        is_order_not_found=lambda exc: (
            isinstance(exc, BinanceAccountClientError) and exc.code in {-2011, -2013}
        ),
    )
    try:
        outcome = service.reconcile(
            user_id=int(account["user_id"]),
            account_id=int(account["id"]),
            api_key=api_key,
            api_secret=api_secret,
        )
    except LiveOrderStatePending:
        _finish_reconciliation(int(account["id"]), successful=False)
        raise BinanceAccountClientError("order_state_pending") from None
    except LiveOrderStateUnknown:
        _finish_reconciliation(int(account["id"]), successful=False)
        raise BinanceAccountClientError("invalid_response") from None
    except Exception:
        # Keep the account fenced during a bounded retry interval. This avoids
        # hammering the two high-weight all-symbol endpoints during an outage.
        _finish_reconciliation(int(account["id"]), successful=False)
        raise
    _finish_reconciliation(int(account["id"]), successful=True)
    return outcome.market_state_changed


def _live_timeframe(account: dict[str, Any], entry_basis: dict[str, Any] | None) -> str:
    captured = (entry_basis or {}).get("execution_policy")
    if isinstance(captured, dict):
        value = str(captured.get("trigger_timeframe") or "").strip().lower()
        if value:
            return value
    try:
        snapshots = account.get("strategy_snapshot_json")
        snapshot = snapshots if isinstance(snapshots, dict) else {}
        return resolve_strategy_timing_policy(snapshot, account.get("config_json")).trigger_timeframe
    except (KeyError, StrategyEvaluationError, TypeError, ValueError):
        return "1h"


def _live_execution_runtime(
    account: dict[str, Any], api_key: str, api_secret: str
) -> LiveExecutionRuntime:
    if _account_service is None or _trading_client is None:
        raise RuntimeError("live engine is not configured")
    physical_account_id = str(account.get("binance_physical_account_id") or "").strip()
    if not physical_account_id:
        raise RuntimeError("trusted Binance physical account binding is missing")
    config = account.get("config_json")
    config = config if isinstance(config, dict) else {}
    symbols = frozenset(str(item).upper() for item in config.get("symbols", ()) if item)
    policy = RiskPolicy(
        max_open_positions=max(1, min(int(config.get("max_positions", 20)), 20)),
        max_notional_to_equity=Decimal(
            str(max(1, min(int(config.get("risk_max_leverage", config.get("leverage", 1))), 20)))
        ),
        allowed_symbols=symbols or frozenset(tradfi_live_symbols()),
    )
    return LiveExecutionRuntime(
        account_client=_account_service.client,
        trading_client=_trading_client,
        engine=store.get_engine(),
        api_key=api_key,
        api_secret=api_secret,
        tenant_scope=f"tenant:{account['user_id']}",
        user_scope=f"user:{account['user_id']}",
        account_scope=f"live-account:{account['id']}",
        physical_account_id=physical_account_id,
        risk_policy=policy,
    )


def _unified_live_intent(
    account: dict[str, Any],
    *,
    signal_key: str,
    symbol: str,
    action: str,
    side: str,
    position_side: str,
    quantity: Decimal,
    reduce_only: bool,
    signal_time: int | None,
    timeframe: str,
) -> OrderIntent:
    now = datetime.now(UTC)
    event_seconds = min(int(signal_time or now.timestamp()), int(now.timestamp()))
    normalized_key = signal_key
    if len(normalized_key) > 191:
        normalized_key = f"live:{hashlib.sha256(normalized_key.encode('utf-8')).hexdigest()}"
    return OrderIntent(
        intent_id=f"intent-{hashlib.sha256(normalized_key.encode('utf-8')).hexdigest()[:48]}",
        idempotency_key=normalized_key,
        strategy_version_id=f"revision-{account['strategy_revision_id']}",
        tenant_scope=f"tenant:{account['user_id']}",
        user_scope=f"user:{account['user_id']}",
        account_scope=f"live-account:{account['id']}",
        deployment_scope=f"deployment:{account['deployment_id']}",
        mode=ExecutionMode.LIVE,
        market="binance_usdm",
        symbol=symbol,
        timeframe=timeframe,
        action=IntentAction(action),
        side=OrderSide(side),
        quantity=quantity,
        signal_time=datetime.fromtimestamp(event_seconds, tz=UTC),
        valid_until=now + timedelta(minutes=5),
        created_at=now,
        position_side=PositionSide(position_side),
        reduce_only=reduce_only,
    )


def _project_unified_market_result(
    account: dict[str, Any],
    intent: OrderIntent,
    result: Any,
    *,
    signal_key: str,
    action: str,
    leverage: int | None,
    strategy_signal_id: int | None,
    entry_basis: dict[str, Any] | None,
    decision_trace: dict[str, Any] | None,
) -> dict[str, Any] | None:
    order = result.broker_order
    response = _unified_order_response(order)
    client_order_id = deterministic_client_order_id(intent)
    if result.state is ExecutionState.FILLED:
        _record_live_position_snapshot(
            account,
            intent,
            order,
            action=action,
            entry_basis=entry_basis,
        )
    projected = _create_intent(
        account,
        signal_key=signal_key,
        symbol=intent.symbol,
        action=action,
        side=intent.side.value,
        position_side=intent.position_side.value,
        order_type="MARKET",
        quantity=intent.quantity,
        request_json={
            "symbol": intent.symbol,
            "side": intent.side.value,
            "position_side": intent.position_side.value,
            "type": "MARKET",
            "quantity": format(intent.quantity, "f"),
            "reduce_only": intent.reduce_only,
            "leverage": leverage,
            **({"exit_decision": decision_trace} if isinstance(decision_trace, dict) else {}),
            "execution_core": "unified_v1",
        },
        strategy_signal_id=strategy_signal_id,
        entry_basis=entry_basis,
        client_order_id=client_order_id,
    )
    if projected is None:
        rows = store.query(
            "SELECT * FROM live_order_intents WHERE signal_key=? AND user_id=? LIMIT 1",
            (signal_key, account["user_id"]),
        )
        projected = dict(rows[0]) if rows else None
    if projected is not None:
        if result.state is ExecutionState.FILLED:
            status = "filled"
        elif result.state in {ExecutionState.SUBMITTED, ExecutionState.PARTIALLY_FILLED}:
            status = "submitted"
        elif result.state is ExecutionState.UNKNOWN:
            status = "unknown"
        elif result.state is ExecutionState.CANCELED:
            status = "canceled"
        else:
            status = "rejected"
        _update_intent(
            int(projected["id"]),
            int(account["user_id"]),
            status=status,
            response=response,
            error_code=result.error_code,
        )
    return response if result.state is ExecutionState.FILLED else None


def _record_live_position_snapshot(
    account: dict[str, Any],
    intent: OrderIntent,
    order: Any,
    *,
    action: str,
    entry_basis: dict[str, Any] | None,
) -> None:
    """Persist the position state emitted by a durable unified execution result."""

    basis = _json_object(entry_basis)
    execution_basis = _json_object(basis.get("execution"))
    average_price = getattr(order, "average_price", None)
    average_entry_price = (
        average_price if action == "open" else execution_basis.get("entry_price")
    )
    snapshot = {
        "schema_version": 1,
        "execution_intent_id": intent.intent_id,
        "action": action,
        "state": "open" if action == "open" else "closed",
        "symbol": intent.symbol,
        "position_side": intent.position_side.value,
        "quantity": str(intent.quantity) if action == "open" else "0",
        "average_entry_price": (
            str(average_entry_price) if average_entry_price is not None else None
        ),
        "mark_price": str(average_price) if average_price is not None else None,
        "strategy_signal_id": (
            _json_object(basis.get("signal")).get("strategy_signal_id")
        ),
    }
    encoded = json.dumps(
        snapshot,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    store.execute(
        """INSERT IGNORE INTO position_snapshots(
               public_id,user_id,deployment_id,strategy_revision_id,mode,
               account_scope,symbol,position_side,position_state,quantity,
               average_entry_price,mark_price,source_type,source_key,
               snapshot_json,snapshot_hash,observed_at,created_at
           ) VALUES(UUID(),?,?,?,'live',?,?,?,?,?,?,?,?,?,?,?,UTC_TIMESTAMP(6),
                    UTC_TIMESTAMP(6))""",
        (
            account["user_id"],
            account["deployment_id"],
            account["strategy_revision_id"],
            f"live:{account['id']}",
            intent.symbol,
            intent.position_side.value,
            snapshot["state"],
            intent.quantity if action == "open" else Decimal("0"),
            average_entry_price,
            average_price,
            "live_execution",
            intent.intent_id,
            encoded,
            hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
        ),
    )


def _unified_order_response(order: Any) -> dict[str, Any] | None:
    if order is None:
        return None
    return {
        "orderId": order.exchange_order_id,
        "clientOrderId": order.reference.client_order_id,
        "symbol": order.symbol,
        "status": order.exchange_status or order.status.value,
        "type": order.order_type.value,
        "side": order.side.value,
        "positionSide": order.position_side.value,
        "reduceOnly": order.reduce_only,
        "avgPrice": str(order.average_price or 0),
        "origQty": str(order.quantity),
        "executedQty": str(order.executed_quantity),
    }


def _place_market(
    account: dict[str, Any],
    api_key: str,
    api_secret: str,
    *,
    signal_key: str,
    symbol: str,
    action: str,
    side: str,
    position_side: str,
    quantity: Decimal,
    reduce_only: bool,
    leverage: int | None = None,
    strategy_signal_id: int | None = None,
    entry_basis: dict[str, Any] | None = None,
    decision_trace: dict[str, Any] | None = None,
    signal_time: int | None = None,
    timeframe: str | None = None,
) -> dict[str, Any] | None:
    write_enabled = (
        _safety_write_enabled(account)
        if action == "close" or reduce_only
        else _execution_enabled(account, symbol)
    )
    if not write_enabled:
        projected = _create_intent(
            account,
            signal_key=signal_key,
            symbol=symbol,
            action=action,
            side=side,
            position_side=position_side,
            order_type="MARKET",
            quantity=quantity,
            request_json={
                "symbol": symbol,
                "side": side,
                "position_side": position_side,
                "type": "MARKET",
                "quantity": format(quantity, "f"),
                "reduce_only": reduce_only,
                "leverage": leverage,
                "execution_core": "unified_v1",
            },
            strategy_signal_id=strategy_signal_id,
            entry_basis=entry_basis,
        )
        if projected is not None:
            _update_intent(
                int(projected["id"]),
                int(account["user_id"]),
                status="canceled",
                error_code=(
                    "kill_switch_engaged"
                    if account.get("_trading_control_blockers")
                    else "execution_stopped"
                ),
            )
        return None
    try:
        runtime = _live_execution_runtime(account, api_key, api_secret)
        if action == "open" and leverage is not None:
            runtime.broker.configure_leverage(symbol, leverage)
        intent = _unified_live_intent(
            account,
            signal_key=signal_key,
            symbol=symbol,
            action=action,
            side=side,
            position_side=position_side,
            quantity=quantity,
            reduce_only=reduce_only,
            signal_time=signal_time,
            timeframe=timeframe or _live_timeframe(account, entry_basis),
        )
        result = runtime.execute(intent)
    except Exception as exc:
        print(f"[live] unified execution failed: {type(exc).__name__}")
        _fail_account(account, "unified_execution_failed")
        return None
    try:
        response = _project_unified_market_result(
            account,
            intent,
            result,
            signal_key=signal_key,
            action=action,
            leverage=leverage,
            strategy_signal_id=strategy_signal_id,
            entry_basis=entry_basis,
            decision_trace=decision_trace,
        )
    except Exception as exc:
        # The durable execution journal is authoritative.  A compatibility
        # projection failure must not hide an exchange fill from the protection
        # workflow or cause a second order submission.
        print(f"[live] execution projection pending: {type(exc).__name__}")
        account["_local_audit_pending"] = True
        _request_reconciliation(int(account["id"]))
        response = (
            _unified_order_response(result.broker_order)
            if result.state is ExecutionState.FILLED
            else None
        )
    if result.state is ExecutionState.UNKNOWN:
        _request_reconciliation(int(account["id"]))
        _fail_account(account, "order_state_unknown")
    elif runtime.last_settlement_error:
        # The fill is returned so protection can be installed.  Durable risk
        # remains reserved, which blocks any additional exposure until the next
        # authoritative account reconciliation settles it.
        _request_reconciliation(int(account["id"]))
        print(f"[live] risk reflection pending: {runtime.last_settlement_error}")
    return response


def _place_protection(
    account: dict[str, Any],
    api_key: str,
    api_secret: str,
    *,
    symbol: str,
    side: str,
    position_side: str,
    quantity: Decimal | None,
    signal_time: int,
    stop: Decimal,
    target: Decimal,
    signal_key_suffix: str = "",
) -> bool:
    if not _safety_write_enabled(account):
        try:
            blocked_plan = ProtectionPlan.create(
                symbol=symbol,
                close_side=side,
                position_side=position_side,
                quantity=quantity,
                signal_time=signal_time,
                stop=stop,
                target=target,
            )
            blocked = blocked_plan.orders[0]
            projected = _create_intent(
                account,
                signal_key=blocked_plan.signal_key(account["deployment_id"], blocked.action),
                symbol=symbol,
                action=blocked.action.value,
                side=side,
                position_side=position_side,
                order_type=blocked.order_type.value,
                quantity=quantity,
                request_json={"execution_core": "unified_protection_v1"},
            )
            if projected is not None:
                _update_intent(
                    int(projected["id"]),
                    int(account["user_id"]),
                    status="canceled",
                    error_code="execution_stopped",
                )
        except (TypeError, ValueError):
            pass
        return False
    if _account_service is None or _trading_client is None:
        return False
    physical_account_id = str(account.get("binance_physical_account_id") or "").strip()
    if not physical_account_id:
        return False
    try:
        plan = ProtectionPlan.create(
            symbol=symbol,
            close_side=side,
            position_side=position_side,
            quantity=quantity,
            signal_time=signal_time,
            stop=stop,
            target=target,
        )
        broker = BinanceBroker(
            _account_service.client,
            _trading_client,
            api_key=api_key,
            api_secret=api_secret,
            account_scope=f"live-account:{account['id']}",
            physical_account_id=physical_account_id,
        )
        execution_scope = f"deployment:{account['deployment_id']}:signal:{signal_time}"
        if signal_key_suffix:
            execution_scope += f":{signal_key_suffix}"
        orders = ProtectionService(broker).ensure(plan, execution_scope=execution_scope)
    except (ProtectionInstallationError, TypeError, ValueError) as exc:
        print(f"[live] unified protection failed: {type(exc).__name__}")
        _request_reconciliation(int(account["id"]))
        return False
    except Exception as exc:
        print(f"[live] unified protection adapter failed: {type(exc).__name__}")
        _request_reconciliation(int(account["id"]))
        return False

    try:
        for order, specification in zip(orders, plan.orders, strict=True):
            action = specification.action.value
            signal_key = plan.signal_key(account["deployment_id"], specification.action)
            if signal_key_suffix:
                signal_key = f"{signal_key}:{signal_key_suffix}"
            projected = _create_intent(
                account,
                signal_key=signal_key,
                symbol=order.symbol,
                action=action,
                side=order.side.value,
                position_side=order.position_side.value,
                order_type=order.order_type.value,
                quantity=(order.quantity if order.quantity > 0 else None),
                request_json={
                    "symbol": order.symbol,
                    "side": order.side.value,
                    "position_side": order.position_side.value,
                    "type": order.order_type.value,
                    "stop_price": format(specification.trigger_price, "f"),
                    "quantity": format(order.quantity, "f") if order.quantity > 0 else None,
                    "close_position": quantity is None,
                    "working_type": "MARK_PRICE",
                    "execution_core": "unified_protection_v1",
                },
                client_order_id=order.reference.client_order_id,
            )
            if projected is None:
                rows = store.query(
                    "SELECT * FROM live_order_intents WHERE signal_key=? AND user_id=? LIMIT 1",
                    (signal_key, account["user_id"]),
                )
                projected = dict(rows[0]) if rows else None
            if projected is not None:
                response = {
                    "algoId": order.exchange_order_id,
                    "clientAlgoId": order.reference.client_order_id,
                    "symbol": order.symbol,
                    "algoStatus": order.exchange_status or order.status.value,
                    "orderType": order.order_type.value,
                    "side": order.side.value,
                    "positionSide": order.position_side.value,
                    "triggerPrice": str(order.trigger_price or specification.trigger_price),
                }
                _update_intent(
                    int(projected["id"]),
                    int(account["user_id"]),
                    status="submitted",
                    response=response,
                )
    except Exception as exc:
        # Exchange protection is already authoritative.  Keep the position
        # protected, fence additional entries, and rebuild the read projection
        # from deterministic client ids during reconciliation.
        print(f"[live] protection projection pending: {type(exc).__name__}")
        account["_local_audit_pending"] = True
        _request_reconciliation(int(account["id"]))
    return True


def _cancel_protection(
    account: dict[str, Any],
    api_key: str,
    api_secret: str,
    symbol: str,
    position_side: str,
) -> None:
    if _trading_client is None:
        return
    rows = store.query(
        """SELECT id,client_order_id FROM live_order_intents
           WHERE user_id=? AND live_account_id=? AND symbol=? AND position_side=?
             AND action IN ('stop','take_profit') AND status='submitted'""",
        (account["user_id"], account["id"], symbol, position_side),
    )
    for row in rows:
        try:
            _trading_client.cancel_algo_order(
                api_key,
                api_secret,
                client_order_id=row["client_order_id"],
            )
            _update_intent(row["id"], account["user_id"], status="canceled")
        except BinanceAccountClientError as exc:
            # The authoritative order lookup already proved this intent is no
            # longer open; do not leave a stale `submitted` row behind forever.
            if exc.code in {-2011, -2013}:
                _update_intent(
                    row["id"],
                    account["user_id"],
                    status="canceled",
                    error_code="exchange_order_not_found",
                )
            else:
                _fail_account(account, "protective_cancel_failed")


def _managed_positions(account: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
    rows = store.query(
        """SELECT id,symbol,position_side,action,side,status,quantity,strategy_signal_id,
                  entry_basis_json,submitted_at,created_at
           FROM live_order_intents
           WHERE user_id=? AND live_account_id=?
             AND action IN ('open','close') AND status='filled'
           ORDER BY id DESC""",
        (account["user_id"], account["id"]),
    )
    return LivePositionSyncService.managed_positions(rows)


def _managed_open(
    account: dict[str, Any], symbol: str, position_side: str
) -> dict[str, Any] | None:
    return _managed_positions(account).get((symbol, position_side))


def _pending_market_intent(
    account: dict[str, Any], symbol: str, position_side: str, action: str
) -> bool:
    rows = store.query(
        """SELECT 1 FROM live_order_intents
           WHERE user_id=? AND live_account_id=? AND symbol=? AND position_side=?
             AND action=? AND order_type='MARKET'
             AND status IN ('created','submitted','unknown')
           LIMIT 1""",
        (
            account["user_id"],
            account["id"],
            symbol,
            position_side,
            action,
        ),
    )
    return bool(rows)


def _protection_counts(
    account: dict[str, Any],
    managed: dict[tuple[str, str], dict[str, Any]] | None = None,
) -> dict[tuple[str, str], int]:
    managed = managed if managed is not None else _managed_positions(account)
    rows = store.query(
        """SELECT id,symbol,position_side,action FROM live_order_intents
           WHERE user_id=? AND live_account_id=?
             AND action IN ('stop','take_profit') AND status='submitted'
           ORDER BY id""",
        (account["user_id"], account["id"]),
    )
    return ProtectionRecoveryService.coverage_counts(rows, managed)


def _cancel_orphan_protections(
    account: dict[str, Any],
    api_key: str,
    api_secret: str,
    managed: dict[tuple[str, str], dict[str, Any]],
    exchange_position_keys: set[tuple[str, str]] | None = None,
) -> None:
    """Remove protection only when neither local nor exchange exposure exists.

    A pre-migration/manual exchange position may not have a managed open intent.
    Its already-live protection must not be canceled merely because local
    ownership metadata is incomplete.
    """
    occupied = exchange_position_keys or set()
    rows = store.query(
        """SELECT DISTINCT symbol,position_side FROM live_order_intents
           WHERE user_id=? AND live_account_id=?
             AND action IN ('stop','take_profit') AND status='submitted'""",
        (account["user_id"], account["id"]),
    )
    for key in ProtectionRecoveryService.orphan_keys(rows, managed, occupied):
        _cancel_protection(account, api_key, api_secret, *key)


def _failed_close_keys(
    account: dict[str, Any],
    managed: dict[tuple[str, str], dict[str, Any]],
) -> set[tuple[str, str]]:
    """Return current position generations with a terminal failed close attempt."""
    rows = store.query(
        """SELECT id,symbol,position_side FROM live_order_intents
           WHERE user_id=? AND live_account_id=? AND action='close'
             AND order_type='MARKET' AND status IN ('canceled','rejected')
           ORDER BY id DESC""",
        (account["user_id"], account["id"]),
    )
    return ProtectionRecoveryService.failed_close_keys(rows, managed)


def _current_stop_prices(
    account: dict[str, Any],
    managed: dict[tuple[str, str], dict[str, Any]],
) -> dict[tuple[str, str], float]:
    rows = store.query(
        """SELECT id,symbol,position_side,request_json
           FROM live_order_intents
           WHERE user_id=? AND live_account_id=?
             AND action='stop' AND status='submitted'
           ORDER BY id DESC""",
        (account["user_id"], account["id"]),
    )
    stops: dict[tuple[str, str], float] = {}
    for row in rows:
        key = (
            str(row["symbol"]).upper(),
            str(row.get("position_side") or "BOTH").upper(),
        )
        opened = managed.get(key)
        if opened is None or int(row["id"]) <= int(opened["id"]) or key in stops:
            continue
        request = _json_object(row.get("request_json"))
        try:
            stop = float(request.get("stop_price"))
        except (TypeError, ValueError):
            continue
        if math.isfinite(stop) and stop > 0:
            stops[key] = stop
    return stops


def _current_open_risk(
    snapshot: Any,
    managed: dict[tuple[str, str], dict[str, Any]],
    stop_prices: dict[tuple[str, str], float],
    *,
    exit_cost_bps: Decimal,
) -> Decimal:
    risks: list[OpenPositionRisk] = []
    for position in snapshot.positions:
        key = _position_key(position)
        opened = managed.get(key)
        stop = stop_prices.get(key)
        if opened is None or stop is None:
            continue
        try:
            quantity = Decimal(str(position["amt"]))
            entry = Decimal(str(position["entry_price"]))
        except (KeyError, TypeError, ValueError):
            continue
        if quantity <= 0 or entry <= 0:
            continue
        risks.append(
            OpenPositionRisk(
                quantity=quantity,
                entry_price=entry,
                stop_price=Decimal(str(stop)),
                exit_cost_bps=exit_cost_bps,
            )
        )
    return total_open_risk(risks)


def _current_initial_margin(
    positions: Any,
    *,
    fallback_leverage: int,
) -> Decimal:
    """Sum Binance's exact initial margin, deriving only missing values."""

    total = Decimal(0)
    for position in positions:
        raw_initial_margin = position.get("initial_margin")
        if raw_initial_margin is not None:
            try:
                initial_margin = Decimal(str(raw_initial_margin))
            except (InvalidOperation, TypeError, ValueError):
                initial_margin = None
            if (
                initial_margin is not None
                and initial_margin.is_finite()
                and initial_margin > 0
            ):
                total += initial_margin
                continue
        try:
            notional = abs(Decimal(str(position.get("notional") or 0)))
            leverage = max(int(position.get("leverage") or fallback_leverage), 1)
        except (InvalidOperation, TypeError, ValueError):
            continue
        if notional.is_finite():
            total += notional / Decimal(leverage)
    return total


def _position_sizing_capital(
    config: dict[str, Any],
    *,
    wallet: Decimal,
    equity: Decimal,
) -> _PositionSizingCapital:
    """Resolve the configured sizing base while respecting real collateral.

    Missing configuration preserves the account-equity behavior.  A fixed
    copy amount can only reduce the sizing base: it is capped by the account's
    non-negative wallet/equity collateral before any risk percentage is applied.
    """

    basis = str(config.get("position_size_basis") or "account_equity")
    if not wallet.is_finite() or not equity.is_finite():
        return _PositionSizingCapital(basis, None, Decimal(0), Decimal(0))
    account_margin_equity = max(Decimal(0), min(wallet, equity))
    if basis == "account_equity":
        return _PositionSizingCapital(
            basis,
            None,
            equity,
            account_margin_equity,
        )
    if basis != "copy_total_amount":
        return _PositionSizingCapital(basis, None, Decimal(0), Decimal(0))
    try:
        configured_total = Decimal(str(config.get("copy_total_amount")))
    except (InvalidOperation, TypeError, ValueError):
        configured_total = Decimal(0)
    if not configured_total.is_finite() or configured_total <= 0:
        return _PositionSizingCapital(basis, None, Decimal(0), Decimal(0))
    effective_total = min(configured_total, account_margin_equity)
    return _PositionSizingCapital(
        basis,
        configured_total,
        effective_total,
        effective_total,
    )


def _exchange_liquidation_is_safe(
    *,
    entry_price: float,
    stop_price: Decimal | float,
    liquidation_price: Decimal | float | int | str | None,
    direction: int,
    min_buffer_pct: float,
) -> bool:
    """Validate the exchange liquidation boundary without rejecting a valid zero.

    Binance USD-M Position Information V3 can return ``liquidationPrice: "0"``
    for a non-zero long position.  A zero long liquidation floor is below every
    valid positive protective stop, so it is safer than the requested stop.
    Missing values, negative values, and zero for short positions remain
    fail-closed because they cannot prove that the stop precedes liquidation.
    """

    if liquidation_price is None:
        return False
    try:
        liquidation = Decimal(str(liquidation_price))
        entry = Decimal(str(entry_price))
        stop = Decimal(str(stop_price))
    except (InvalidOperation, TypeError, ValueError):
        return False
    if not liquidation.is_finite() or not entry.is_finite() or not stop.is_finite():
        return False
    if liquidation < 0 or entry <= 0 or stop <= 0:
        return False
    if liquidation == 0:
        return direction > 0 and stop < entry
    try:
        return liquidation_stop_safety(
            entry_price=entry,
            stop_price=stop,
            liquidation_price=liquidation,
            direction=direction,
            min_buffer_pct=min_buffer_pct,
        ).safe
    except (TypeError, ValueError):
        return False


def _has_unmanaged_exposure(
    positions: dict[tuple[str, str], dict[str, Any]],
    managed: dict[tuple[str, str], dict[str, Any]],
) -> bool:
    """Block new entries when external positions or manual size changes exist."""

    for key, position in positions.items():
        opened = managed.get(key)
        if opened is None:
            return True
        try:
            actual = Decimal(str(position["amt"]))
            expected = Decimal(str(opened.get("quantity") or position["amt"]))
        except (InvalidOperation, KeyError, TypeError, ValueError):
            return True
        if actual != expected:
            return True
    return False


def _automatic_directional_exposure_allowed(
    account: dict[str, Any],
    direction: int,
    positions: dict[tuple[str, str], dict[str, Any]],
) -> bool:
    """Cap correlated AI-monitor beta exposure in the same direction.

    Binance equity perpetuals share a broad US-equity risk factor even when
    their narrower symbol clusters differ.  The ordinary symbol admission
    rule remains useful, but it must not allow a burst of unrelated-looking
    long (or short) entries from the same scan.
    """

    config = account.get("config_json")
    config = config if isinstance(config, dict) else {}
    if str(config.get("signal_source") or "strategy") != "ai_monitor":
        return True
    if direction not in {-1, 1}:
        return False
    try:
        configured_cap = int(
            config.get(
                "ai_monitor_live_max_same_direction_positions",
                min(int(config.get("max_cluster_positions", 2)), 2),
            )
        )
    except (TypeError, ValueError, OverflowError):
        configured_cap = 2
    cap = max(1, min(configured_cap, 5))
    same_direction = 0
    for position in positions.values():
        side = str(position.get("side") or "").lower()
        position_side = str(position.get("position_side") or "BOTH").upper()
        position_direction = (
            1
            if position_side == "LONG" or side == "long"
            else -1
            if position_side == "SHORT" or side == "short"
            else 0
        )
        if position_direction == direction:
            same_direction += 1
    return same_direction < cap


def _is_grandfathered_open(opened: dict[str, Any]) -> bool:
    """Return whether an open predates immutable live entry capture.

    Migration 0020 deliberately left historical rows NULL, while every new
    live open persists a versioned ``availability=captured`` snapshot before
    the exchange write. Unknown/malformed snapshots are treated as historical
    so a metadata problem cannot itself cause an automatic market close.
    """

    basis = _json_object(opened.get("entry_basis_json"))
    schema_version = basis.get("schema_version")
    return not (
        isinstance(schema_version, int)
        and not isinstance(schema_version, bool)
        and schema_version in {1, ENTRY_BASIS_SCHEMA_VERSION}
        and basis.get("availability") == "captured"
        and basis.get("mode") == "live"
    )


def _is_manual_follow_open(opened: dict[str, Any]) -> bool:
    """Return whether the managed position came from an explicit manual follow.

    Manual follows are user-controlled positions.  The exchange-native stop and
    take-profit orders created at entry remain active, but the background signal
    executor must never submit a market close for metadata gaps, holding time, or
    later strategy reversals.
    """

    basis = _json_object(opened.get("entry_basis_json"))
    signal = _json_object(basis.get("signal"))
    evidence = _json_object(signal.get("evidence"))
    return evidence.get("manual_follow") is True


def _risk_review_warnings(
    positions: dict[tuple[str, str], dict[str, Any]],
    managed: dict[tuple[str, str], dict[str, Any]],
    protection_counts: dict[tuple[str, str], int] | None = None,
) -> list[dict[str, Any]]:
    """Describe exposure that must be reviewed before another live entry."""

    warnings: list[dict[str, Any]] = []
    counts = protection_counts or {}
    for key in sorted(positions):
        symbol, position_side = key
        position = positions[key]
        opened = managed.get(key)
        if opened is None:
            warnings.append(
                {
                    "code": "unmanaged_exchange_position",
                    "symbol": symbol,
                    "position_side": position_side,
                    "message": (
                        "Exchange position is not owned by this deployment; "
                        "new entries are blocked until it is manually reviewed."
                    ),
                }
            )
            continue

        if _is_grandfathered_open(opened):
            warnings.append(
                {
                    "code": "historical_position_review_required",
                    "symbol": symbol,
                    "position_side": position_side,
                    "protection_count": int(counts.get(key, 0)),
                    "message": (
                        "Historical position has no captured entry basis; its existing "
                        "protection is retained and new-rule auto-close checks are skipped. "
                        "New entries remain blocked until manual review."
                    ),
                    "grandfathered_checks": [
                        "protection_completeness",
                        "liquidation_buffer",
                        "max_holding_bars",
                    ],
                }
            )

        try:
            actual = Decimal(str(position["amt"]))
            expected = Decimal(str(opened.get("quantity") or position["amt"]))
        except (InvalidOperation, KeyError, TypeError, ValueError):
            actual = None
            expected = None
        if actual is None or expected is None or actual != expected:
            warnings.append(
                {
                    "code": "managed_quantity_mismatch",
                    "symbol": symbol,
                    "position_side": position_side,
                    "expected_quantity": str(expected) if expected is not None else None,
                    "actual_quantity": str(actual) if actual is not None else None,
                    "message": (
                        "Exchange quantity differs from the managed open quantity; "
                        "new entries are blocked until it is manually reviewed."
                    ),
                }
            )
    return warnings


def _apply_risk_review_state(
    state: dict[str, Any], warnings: list[dict[str, Any]]
) -> None:
    state["risk_review_required"] = bool(warnings)
    state["risk_review_warnings"] = warnings
    raw_reasons = state.get("entry_block_reasons")
    reasons = list(raw_reasons) if isinstance(raw_reasons, list) else []
    reasons = [reason for reason in reasons if reason != "risk_review_required"]
    if warnings:
        reasons.append("risk_review_required")
    state["entry_block_reasons"] = reasons


def _persist_risk_review(
    account: dict[str, Any], warnings: list[dict[str, Any]]
) -> None:
    state = _json_object(account.get("runtime_state_json"))
    _apply_risk_review_state(state, warnings)
    store.execute(
        """UPDATE strategy_deployments SET runtime_state_json=?,updated_at=UTC_TIMESTAMP()
           WHERE id=? AND user_id=?""",
        (json.dumps(state, ensure_ascii=False), account["deployment_id"], account["user_id"]),
    )
    account["runtime_state_json"] = state


def _entry_loss_guard(
    account: dict[str, Any],
    snapshot: Any,
    policy: Any,
    review_warnings: list[dict[str, Any]] | None = None,
) -> bool:
    """Persist daily/high-water equity and block only new entries on breach."""

    state = _json_object(account.get("runtime_state_json"))
    equity = Decimal(str(snapshot.wallet_balance)) + Decimal(
        str(getattr(snapshot, "unrealized_pnl", 0))
    )
    if equity <= 0:
        if review_warnings is not None:
            _persist_risk_review(account, review_warnings)
        return False
    today = time.strftime("%Y-%m-%d", time.gmtime())
    if state.get("risk_day") != today:
        state["risk_day"] = today
        state["day_start_equity"] = float(equity)
    try:
        day_start = Decimal(str(state.get("day_start_equity")))
    except (InvalidOperation, TypeError, ValueError):
        day_start = equity
    try:
        high_watermark = max(Decimal(str(state.get("high_watermark_equity"))), equity)
    except (InvalidOperation, TypeError, ValueError):
        high_watermark = equity
    decision = account_loss_limits(
        current_equity=equity,
        start_of_day_equity=day_start,
        high_watermark_equity=high_watermark,
        policy=policy,
    )
    state.update(
        {
            "last_equity": float(equity),
            "high_watermark_equity": float(high_watermark),
            "daily_loss_pct": float(decision.daily_loss_pct),
            "drawdown_pct": float(decision.drawdown_pct),
            "entry_block_reasons": list(decision.reasons),
        }
    )
    if review_warnings is not None:
        _apply_risk_review_state(state, review_warnings)
    store.execute(
        """UPDATE strategy_deployments SET runtime_state_json=?,updated_at=UTC_TIMESTAMP()
           WHERE id=? AND user_id=?""",
        (json.dumps(state, ensure_ascii=False), account["deployment_id"], account["user_id"]),
    )
    account["runtime_state_json"] = state
    return decision.allow_new_entries and not review_warnings


def _signal_is_fresh(
    account: dict[str, Any],
    signal_time: int,
    policy: Any,
    evidence: dict[str, Any] | None = None,
) -> bool:
    if (evidence or {}).get("source") == "ai_monitor_live_copy_v1":
        maximum_age = signal_freshness(
            signal_time,
            max_age_seconds=policy.max_signal_age_seconds,
        )
        raw_valid_until = (evidence or {}).get("valid_until")
        try:
            valid_until = float(raw_valid_until)
        except (TypeError, ValueError):
            return False
        while valid_until >= 100_000_000_000:
            valid_until /= 1000
        return (
            maximum_age.fresh
            and math.isfinite(valid_until)
            and time.time() <= valid_until
        )
    snapshot = account.get("strategy_snapshot_json") or {}
    if snapshot.get("strategy_kind") == "builtin_strategy":
        try:
            timeframe_seconds = _execution_timeframe_seconds(account)
        except (StrategyEvaluationError, TypeError, ValueError):
            return False
        closed = closed_bar_signal_freshness(
            signal_time,
            timeframe_seconds=timeframe_seconds,
            valid_bars=max(
                1,
                math.ceil(policy.max_signal_age_seconds / timeframe_seconds),
            ),
        )
        maximum_age = signal_freshness(
            signal_time,
            max_age_seconds=policy.max_signal_age_seconds,
        )
        return closed.fresh and maximum_age.fresh

    maximum_age = signal_freshness(
        signal_time,
        max_age_seconds=policy.max_signal_age_seconds,
    )
    if not maximum_age.fresh:
        return False
    raw_valid_until = (evidence or {}).get("valid_until")
    if raw_valid_until is None:
        return False
    try:
        valid_until = float(raw_valid_until)
    except (TypeError, ValueError):
        return False
    while valid_until >= 100_000_000_000:
        valid_until /= 1000
    return math.isfinite(valid_until) and time.time() <= valid_until


def _execution_timeframe_seconds(account: dict[str, Any]) -> int:
    """Resolve the immutable trigger interval used by holding/freshness rules."""

    policy = resolve_strategy_timing_policy(
        account.get("strategy_snapshot_json") or {},
        account.get("config_json")
        if isinstance(account.get("config_json"), dict)
        else None,
    )
    return policy.timeframe_seconds


def _exit_levels(
    entry: float,
    direction: int,
    atr: float | None,
    config: dict[str, Any],
) -> tuple[float | None, float | None]:
    """Compatibility façade over the shared deterministic exit policy."""

    plan = DEFAULT_EXIT_POLICY.resolve_levels(
        entry,
        direction,
        stop_loss_pct=config.get("stop_loss_pct"),
        take_profit_pct=config.get("take_profit_pct"),
        atr=atr,
        atr_stop_multiplier=1.5,
        atr_take_profit_multiplier=2.5,
    )
    return (plan.stop, plan.target) if plan is not None else (None, None)


def _signal_exit_levels(
    entry: float,
    direction: int,
    atr: float | None,
    config: dict[str, Any],
    evidence: dict[str, Any] | None,
) -> tuple[float | None, float | None]:
    """Use a full strategy's fixed risk proposal, otherwise the built-in ATR rule."""

    proposal = (evidence or {}).get("risk_proposal")
    if not isinstance(proposal, dict):
        # Keep this compatibility seam for existing recovery paths and tests;
        # the paper helper itself delegates to the shared policy.
        return _exit_levels(entry, direction, atr, config)
    plan = DEFAULT_EXIT_POLICY.resolve_levels(
        entry,
        direction,
        stop_loss_pct=config.get("stop_loss_pct"),
        take_profit_pct=config.get("take_profit_pct"),
        atr=atr,
        risk_proposal=proposal,
    )
    return (plan.stop, plan.target) if plan is not None else (None, None)


def _opened_at_seconds(managed_open: dict[str, Any]) -> float | None:
    value = managed_open.get("submitted_at") or managed_open.get("created_at")
    if hasattr(value, "timestamp"):
        return float(value.timestamp())
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        parsed = float(value)
        while parsed >= 100_000_000_000:
            parsed /= 1000
        return parsed
    return None


def _live_profit_guard_snapshot(
    position: dict[str, Any],
    managed_open: dict[str, Any],
    previous: dict[str, Any] | None,
    *,
    exit_cost_bps: Decimal | float | int | str,
    observed_at: int,
) -> tuple[dict[str, Any] | None, bool]:
    """Adapt live position records to the shared causal profit guard."""

    basis = _json_object(managed_open.get("entry_basis_json"))
    execution = _json_object(basis.get("execution"))
    signal = _json_object(basis.get("signal"))
    evidence = _json_object(signal.get("evidence"))
    risk_plan = _json_object(evidence.get("risk_plan"))
    protection = _json_object(risk_plan.get("profit_protection"))
    direction = 1 if str(position.get("side") or "").lower() == "long" else -1
    if str(position.get("position_side") or "BOTH").upper() == "LONG":
        direction = 1
    elif str(position.get("position_side") or "BOTH").upper() == "SHORT":
        direction = -1
    guard, should_exit = DEFAULT_EXIT_POLICY.advance_profit_guard(
        entry_price=position.get("entry_price") or execution.get("entry_price"),
        mark_price=position.get("mark_price"),
        initial_stop=execution.get("stop"),
        direction=direction,
        previous=previous,
        exit_cost_bps=exit_cost_bps,
        observed_at=observed_at,
        activation_r=protection.get(
            "activation_r", _LIVE_PROFIT_GUARD_ACTIVATION_R
        ),
        trailing_activation_r=protection.get(
            "trailing_activation_r", _LIVE_PROFIT_GUARD_TRAILING_ACTIVATION_R
        ),
        maximum_giveback_r=protection.get(
            "maximum_giveback_r", _LIVE_PROFIT_GUARD_GIVEBACK_R
        ),
        minimum_protected_r=protection.get("minimum_protected_r", 0.0),
        cost_buffer_bps=_LIVE_PROFIT_GUARD_COST_BUFFER_BPS,
        peak_write_step_r=_LIVE_PROFIT_GUARD_PEAK_WRITE_STEP_R,
    )
    if guard is not None:
        guard.update(
            {
                "version": _LIVE_PROFIT_GUARD_VERSION,
                "open_intent_id": int(managed_open["id"]),
                "symbol": str(position.get("symbol") or "").upper(),
                "position_side": str(
                    position.get("position_side") or "BOTH"
                ).upper(),
            }
        )
    return guard, should_exit


def _live_profit_guard_key(managed_open: dict[str, Any]) -> str:
    return ":".join(
        (
            str(managed_open.get("id") or ""),
            str(managed_open.get("symbol") or "").upper(),
            str(managed_open.get("position_side") or "BOTH").upper(),
        )
    )


def _persist_live_profit_guards(
    account: dict[str, Any], guards: dict[str, dict[str, Any]]
) -> None:
    state = _json_object(account.get("runtime_state_json"))
    state["live_profit_guards"] = guards
    store.execute(
        """UPDATE strategy_deployments SET runtime_state_json=?,updated_at=UTC_TIMESTAMP()
           WHERE id=? AND user_id=?""",
        (
            json.dumps(state, ensure_ascii=False),
            account["deployment_id"],
            account["user_id"],
        ),
    )
    account["runtime_state_json"] = state


def _record_reconciled_close(account: dict[str, Any], managed_open: dict[str, Any]) -> None:
    """Close the local position generation after Binance reports no position."""
    symbol = str(managed_open["symbol"]).upper()
    position_side = str(managed_open.get("position_side") or "BOTH").upper()
    open_side = str(managed_open.get("side") or "BUY").upper()
    signal_key = (
        f"live:{account['deployment_id']}:{symbol}:{position_side}:"
        f"reconciled-close:{managed_open['id']}"
    )
    intent = _create_intent(
        account,
        signal_key=signal_key,
        symbol=symbol,
        action="close",
        side="SELL" if open_side == "BUY" else "BUY",
        position_side=position_side,
        order_type="RECONCILED",
        quantity=managed_open.get("quantity"),
        request_json={
            "reason": "exchange_position_absent",
            "open_intent_id": int(managed_open["id"]),
        },
        strategy_signal_id=managed_open.get("strategy_signal_id"),
        entry_basis=_json_object(managed_open.get("entry_basis_json")),
    )
    if intent is not None:
        _update_intent(
            intent["id"],
            account["user_id"],
            status="filled",
            response={"status": "FILLED", "symbol": symbol},
        )


def _close_position(
    account: dict[str, Any], api_key: str, api_secret: str, position: dict[str, Any], reason: str
) -> bool:
    if _trading_client is None:
        return False
    symbol = str(position["symbol"])
    position_side = str(position.get("position_side") or "BOTH").upper()
    managed = _managed_open(account, symbol, position_side)
    if managed is None:
        return False
    if _is_manual_follow_open(managed):
        # Defense in depth: even if a caller misses the lifecycle guard, an
        # explicitly confirmed manual position cannot be turned into an
        # executor-originated market close. Exchange stop/TP orders and an
        # exchange-side/user close are reconciled without entering this path.
        return False
    if _pending_market_intent(account, symbol, position_side, "close"):
        return False
    rules = _trading_client.symbol_rules(symbol)
    actual_quantity = Decimal(str(position["amt"]))
    managed_quantity = Decimal(str(managed.get("quantity") or actual_quantity))
    quantity = rules.quantity(min(actual_quantity, managed_quantity))
    side = "SELL" if position["side"] == "long" else "BUY"
    managed_identity = managed.get("id") or hashlib.sha256(
        str(managed.get("signal_key") or managed.get("client_order_id") or symbol).encode(
            "utf-8"
        )
    ).hexdigest()[:24]
    signal_key = (
        f"live:{account['deployment_id']}:{symbol}:{position_side}:close:"
        f"managed-open:{managed_identity}"
    )
    observed_at = int(time.time())
    exit_decision = DEFAULT_EXIT_POLICY.decision_for_reason(
        reason,
        position.get("mark_price") or position.get("entry_price"),
        observed_at=observed_at,
    )
    response = _place_market(
        account,
        api_key,
        api_secret,
        signal_key=signal_key,
        symbol=symbol,
        action="close",
        side=side,
        position_side=position_side,
        quantity=quantity,
        reduce_only=position_side == "BOTH",
        strategy_signal_id=managed.get("strategy_signal_id"),
        entry_basis=_json_object(managed.get("entry_basis_json")),
        decision_trace=(
            exit_decision.snapshot(mode="live")
            if exit_decision is not None
            else None
        ),
        signal_time=observed_at,
        timeframe=_live_timeframe(account, _json_object(managed.get("entry_basis_json"))),
    )
    if response is None:
        _fail_account(account, "position_close_failed")
        # Keep the exchange-side stop/target alive while the market close is
        # uncertain. Removing protection first creates an avoidable naked window.
        return False
    _cancel_protection(account, api_key, api_secret, symbol, position_side)
    return True


def _open_position(
    account: dict[str, Any],
    api_key: str,
    api_secret: str,
    snapshot: Any,
    *,
    symbol: str,
    direction: int,
    price: float,
    atr: float | None,
    signal_time: int,
    current_open_risk: Decimal = Decimal(0),
    basis: list[str] | None = None,
    signal_evidence: dict[str, Any] | None = None,
    signal_key_suffix: str = "",
) -> bool:
    if _trading_client is None:
        return False
    config = account["config_json"]
    policy = policy_from_config(config)
    strategy_snapshot = account.get("strategy_snapshot_json") or {}
    signal_source = str((signal_evidence or {}).get("source") or "")
    if (
        strategy_snapshot.get("strategy_kind") in {"full_strategy", "source_strategy"}
        or signal_source == "ai_monitor_live_copy_v1"
    ):
        proposal = (signal_evidence or {}).get("risk_proposal")
        if not isinstance(proposal, dict):
            return False
        try:
            policy = tighten_policy_with_strategy(policy, proposal)
        except ValueError:
            return False
    position_mode = str(config.get("position_mode") or "one_way")
    position_side = _strategy_position_side(position_mode, direction)
    requested_leverage = max(1, min(int(config.get("leverage", 3)), 20))
    preview_stop, preview_target = _signal_exit_levels(
        price,
        direction,
        atr,
        config,
        signal_evidence,
    )
    if preview_stop is None or preview_target is None:
        return False
    stop_distance = abs(Decimal(str(price)) - Decimal(str(preview_stop)))
    leverage = leverage_for_stop_distance(
        entry_price=price,
        stop_distance=stop_distance,
        requested_leverage=requested_leverage,
        policy=policy,
    )
    rules = _trading_client.symbol_rules(symbol)
    available = Decimal(str(snapshot.available_balance))
    wallet = Decimal(str(snapshot.wallet_balance))
    equity = wallet + Decimal(str(getattr(snapshot, "unrealized_pnl", 0)))
    sizing_capital = _position_sizing_capital(
        config,
        wallet=wallet,
        equity=equity,
    )
    current_margin = _current_initial_margin(
        snapshot.positions,
        fallback_leverage=leverage,
    )
    remaining_margin = min(
        available,
        max(
            Decimal(0),
            sizing_capital.margin_equity
            * Decimal(str(config.get("margin_cap", 0.20)))
            - current_margin,
        ),
    )
    sizing = atr_risk_position_size(
        equity=sizing_capital.effective_equity,
        available_balance=remaining_margin,
        entry_price=price,
        stop_distance=stop_distance,
        requested_leverage=leverage,
        current_open_risk=current_open_risk,
        direction=direction,
        high_risk=symbol_risk_profile(symbol).high_risk,
        policy=policy,
    )
    if not sizing.allowed:
        return False
    leverage = sizing.effective_leverage
    try:
        quantity = rules.quantity(sizing.quantity)
    except ValueError:
        return False
    if quantity * Decimal(str(price)) < rules.min_notional:
        return False
    requested_margin = quantity * Decimal(str(price)) / Decimal(leverage)
    side = "BUY" if direction > 0 else "SELL"
    close_side = "SELL" if direction > 0 else "BUY"
    signal_key = f"live:{account['deployment_id']}:{symbol}:{signal_time}:open:{direction}"
    if signal_key_suffix:
        signal_key = f"{signal_key}:{signal_key_suffix}"
    entry_basis, strategy_signal_id = build_entry_basis_snapshot(
        account,
        mode="live",
        symbol=symbol,
        direction=direction,
        signal_time=signal_time,
        reasons=list(basis or []),
        evidence=signal_evidence,
        entry_price=price,
        atr=atr,
        stop=preview_stop,
        target=preview_target,
        leverage=leverage,
        margin=float(requested_margin),
    )
    entry_basis["execution"].update(
        {
            "position_size_basis": sizing_capital.basis,
            "configured_copy_total_amount": (
                float(sizing_capital.configured_total_amount)
                if sizing_capital.configured_total_amount is not None
                else None
            ),
            "effective_sizing_capital": float(sizing_capital.effective_equity),
        }
    )
    response = _place_market(
        account,
        api_key,
        api_secret,
        signal_key=signal_key,
        symbol=symbol,
        action="open",
        side=side,
        position_side=position_side,
        quantity=quantity,
        reduce_only=False,
        leverage=leverage,
        strategy_signal_id=strategy_signal_id,
        entry_basis=entry_basis,
        signal_time=signal_time,
        timeframe=_live_timeframe(account, entry_basis),
    )
    if response is None:
        return False
    try:
        entry = float(response.get("avgPrice") or 0)
    except (TypeError, ValueError):
        entry = 0.0
    if (not math.isfinite(entry) or entry <= 0) and response.get("clientOrderId"):
        try:
            verified_response = _trading_client.query_order(
                api_key,
                api_secret,
                symbol=symbol,
                client_order_id=str(response["clientOrderId"]),
            )
            entry = float(verified_response.get("avgPrice") or 0)
        except (BinanceAccountClientError, TypeError, ValueError):
            entry = 0.0
    fallback_position = {
        "symbol": symbol,
        "amt": float(quantity),
        "side": "long" if direction > 0 else "short",
        "position_side": position_side,
    }
    if not math.isfinite(entry) or entry <= 0:
        # A RESULT response should contain avgPrice. If it does not, install the
        # pre-trade emergency stop/target before attempting a fail-closed exit.
        # This leaves protection in place if the exit request becomes uncertain.
        _place_protection(
            account,
            api_key,
            api_secret,
            symbol=symbol,
            side=close_side,
            position_side=position_side,
            quantity=quantity if position_side != "BOTH" else None,
            signal_time=signal_time,
            stop=rules.price(Decimal(str(preview_stop))),
            target=rules.price(Decimal(str(preview_target))),
            signal_key_suffix=signal_key_suffix,
        )
        _close_position(
            account,
            api_key,
            api_secret,
            fallback_position,
            "entry_price_unverified",
        )
        _fail_account(account, "entry_price_unverified")
        return False
    stop_raw, target_raw = _signal_exit_levels(
        entry,
        direction,
        atr,
        config,
        signal_evidence,
    )
    if stop_raw is None or target_raw is None:
        _close_position(
            account,
            api_key,
            api_secret,
            fallback_position,
            "missing_protection",
        )
        _fail_account(account, "missing_protection")
        return False
    stop = rules.price(Decimal(str(stop_raw)))
    target = rules.price(Decimal(str(target_raw)))
    # Protection is the first action after the fill price is known. Account
    # refreshes and audit enrichment happen only after both orders are live.
    if not _place_protection(
        account,
        api_key,
        api_secret,
        symbol=symbol,
        side=close_side,
        position_side=position_side,
        quantity=quantity if position_side != "BOTH" else None,
        signal_time=signal_time,
        stop=stop,
        target=target,
        signal_key_suffix=signal_key_suffix,
    ):
        _close_position(
            account,
            api_key,
            api_secret,
            fallback_position,
            "protection_failed",
        )
        _fail_account(account, "protection_failed")
        return False
    entry_basis["execution"].update(
        {
            "entry_price": entry,
            "stop": stop_raw,
            "target": target_raw,
            "quantity": float(quantity),
        }
    )
    final_risk_proposal = (signal_evidence or {}).get("risk_proposal")
    final_exit_plan = DEFAULT_EXIT_POLICY.resolve_levels(
        entry,
        direction,
        stop_loss_pct=config.get("stop_loss_pct"),
        take_profit_pct=config.get("take_profit_pct"),
        atr=atr,
        risk_proposal=(
            final_risk_proposal if isinstance(final_risk_proposal, dict) else None
        ),
    )
    if final_exit_plan is not None:
        entry_basis["exit_policy"] = final_exit_plan.snapshot()
    store.execute(
        """UPDATE live_order_intents SET entry_basis_json=?,updated_at=UTC_TIMESTAMP()
           WHERE user_id=? AND live_account_id=? AND signal_key=? AND action='open'""",
        (
            json.dumps(entry_basis, ensure_ascii=False),
            account["user_id"],
            account["id"],
            signal_key,
        ),
    )
    try:
        refreshed = (
            _account_service.account(api_key, api_secret, force_refresh=True)
            if _account_service
            else None
        )
    except BinanceAccountClientError:
        _close_position(
            account,
            api_key,
            api_secret,
            fallback_position,
            "position_state_unverified",
        )
        _fail_account(account, "position_state_unverified")
        return False
    refreshed_position = None
    if refreshed is not None:
        refreshed_position = next(
            (
                item
                for item in refreshed.positions
                if _position_key(item) == (symbol, position_side)
            ),
            None,
        )
    if refreshed_position is None:
        _close_position(
            account,
            api_key,
            api_secret,
            fallback_position,
            "position_state_unverified",
        )
        _fail_account(account, "position_state_unverified")
        return False
    liquidation_price = refreshed_position.get("liquidation_price")
    safety_ok = _exchange_liquidation_is_safe(
        entry_price=entry,
        stop_price=stop,
        liquidation_price=liquidation_price,
        direction=direction,
        min_buffer_pct=policy.liquidation_buffer_pct,
    )
    if not safety_ok:
        _close_position(
            account,
            api_key,
            api_secret,
            refreshed_position,
            "liquidation_buffer_unsafe",
        )
        _fail_account(account, "liquidation_buffer_unsafe")
        return False
    store.execute(
        """UPDATE strategy_signals SET status='executed'
           WHERE deployment_id=? AND user_id=? AND symbol=? AND signal_bar_time=?
             AND status='approved'""",
        (account["deployment_id"], account["user_id"], symbol, signal_time),
    )
    return True


def _manual_intent(account: dict[str, Any], signal_key: str) -> dict[str, Any] | None:
    rows = store.query(
        """SELECT public_id,status,error_code,quantity,created_at,updated_at
           FROM live_order_intents
           WHERE user_id=? AND live_account_id=? AND signal_key=? AND action='open'
           LIMIT 1""",
        (account["user_id"], account["id"], signal_key),
    )
    return dict(rows[0]) if rows else None


def _manual_follow_signal_context(
    account: dict[str, Any],
    *,
    symbol: str,
    direction_name: str,
    price: float,
    opportunity_public_id: str,
    prediction_public_id: str | None,
    selected_at: Any,
    selected_evidence: dict[str, Any] | None,
    selected_score: float | None,
) -> tuple[int, float | None, list[str], int, dict[str, Any]]:
    """Build execution metadata without re-running automatic signal admission."""

    config = account.get("config_json")
    config = config if isinstance(config, dict) else {}
    direction = 1 if direction_name == "long" else -1
    evidence = dict(selected_evidence or {})
    risk_plan = evidence.get("risk_plan")
    risk_plan = risk_plan if isinstance(risk_plan, dict) else {}

    def percentage(value: Any, fallback: float, maximum: float) -> float:
        try:
            parsed = float(value)
        except (TypeError, ValueError, OverflowError):
            parsed = fallback
        return parsed if math.isfinite(parsed) and 0 < parsed <= maximum else fallback

    stop_loss_pct = percentage(
        risk_plan.get("stop_loss_pct"),
        percentage(config.get("stop_loss_pct"), 1.5, 20.0),
        20.0,
    )
    take_profit_pct = percentage(
        risk_plan.get("take_profit_pct"),
        percentage(config.get("take_profit_pct"), 3.0, 50.0),
        50.0,
    )
    atr_pct = percentage(risk_plan.get("atr_pct"), 0.0, 20.0)
    atr = price * atr_pct / 100 if atr_pct > 0 else None
    max_leverage = max(
        1,
        min(
            int(config.get("leverage", 1)),
            int(config.get("risk_max_leverage", config.get("leverage", 1))),
            20,
        ),
    )
    selected_seconds = _utc_seconds(selected_at)
    if selected_seconds is None:
        stable_selection = f"{opportunity_public_id}:{prediction_public_id or ''}"
        selected_seconds = float(
            int(hashlib.sha256(stable_selection.encode("utf-8")).hexdigest()[:8], 16)
        )
    signal_time = int(selected_seconds)
    risk_proposal = {
        "stop_distance": price * stop_loss_pct / 100,
        "take_profit_distance": price * take_profit_pct / 100,
        "risk_per_trade_pct": float(config.get("risk_per_trade_pct", 0.5)),
        "max_margin_pct": float(
            config.get("max_margin_per_trade_pct", config.get("position_size_pct", 2.0))
        ),
        "max_leverage": max_leverage,
    }
    execution_evidence = {
        "source": "ai_monitor_manual_follow_v1",
        "execution_venue": "binance_usdm",
        "execution_price_source": "binance_live_ticker",
        "contract_symbol": symbol,
        "manual_selection": True,
        "manual_signal_override": True,
        "opportunity_public_id": opportunity_public_id,
        "prediction_public_id": prediction_public_id,
        "selected_at": selected_seconds,
        "score": selected_score,
        "risk_plan": {
            "stop_loss_pct": stop_loss_pct,
            "take_profit_pct": take_profit_pct,
            "atr_pct": atr_pct if atr is not None else None,
        },
        "risk_proposal": risk_proposal,
        "automatic_checks_bypassed": [
            "ticker_cache_freshness",
            "prediction_status",
            "signal_age",
            "research_gate",
            "score_gate",
            "market_session",
        ],
    }
    basis = [
        "执行方式：人工确认立即跟单",
        f"机会：{opportunity_public_id}",
        f"方向：{'做多' if direction > 0 else '做空'}",
        "价格：Binance 即时合约行情",
        "准入：人工指令覆盖自动信号判断",
    ]
    if prediction_public_id:
        basis.insert(1, f"预测：{prediction_public_id}")
    return direction, atr, basis, signal_time, execution_evidence


def _manual_follow_result(
    *,
    status: str,
    reason: str,
    symbol: str,
    direction: str,
    intent: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "status": status,
        "reason": reason,
        "symbol": symbol,
        "direction": direction,
        "intent": (
            {
                "id": intent.get("public_id"),
                "status": intent.get("status"),
                "quantity": (
                    float(intent["quantity"])
                    if intent.get("quantity") is not None
                    else None
                ),
            }
            if intent is not None
            else None
        ),
    }


def execute_ai_monitor_manual_follow(
    *,
    user_id: int,
    live_account_id: int,
    opportunity_public_id: str,
    prediction_public_id: str | None,
    manual_attempt_id: str,
    expected_symbol: str,
    expected_direction: str,
    selected_at: Any,
    selected_evidence: dict[str, Any] | None,
    selected_score: float | None,
) -> dict[str, Any]:
    """Execute one explicit manual instruction through the live order writer.

    This path deliberately does not manage or reverse existing positions.  It is
    new-entry only. Automatic ticker-cache freshness, prediction-state, score,
    age and session admission are bypassed; exchange state, loss limits, sizing,
    idempotency, fill verification and protection remain enforced.
    """

    symbol = str(expected_symbol or "").upper()
    direction_name = str(expected_direction or "").lower()
    if direction_name not in {"long", "short"}:
        return _manual_follow_result(
            status="blocked",
            reason="direction_invalid",
            symbol=symbol,
            direction=direction_name,
        )
    with _account_execution_lock(int(live_account_id)):
        accounts = [
            item
            for item in _active_accounts(int(live_account_id))
            if int(item.get("user_id") or 0) == int(user_id)
        ]
        if len(accounts) != 1:
            return _manual_follow_result(
                status="blocked",
                reason="live_copy_inactive",
                symbol=symbol,
                direction=direction_name,
            )
        account = accounts[0]
        config = account["config_json"]
        if (
            str(config.get("execution_scope") or "") != "ai_monitor"
            or str(config.get("signal_source") or "") != "ai_monitor"
            or not bool(config.get("ai_monitor_live_copy_enabled"))
        ):
            return _manual_follow_result(
                status="blocked",
                reason="live_copy_inactive",
                symbol=symbol,
                direction=direction_name,
            )
        if symbol not in _strategy_universe(config):
            return _manual_follow_result(
                status="blocked",
                reason="symbol_not_enabled",
                symbol=symbol,
                direction=direction_name,
            )
        if _account_service is None or _trading_client is None:
            return _manual_follow_result(
                status="blocked",
                reason="engine_unavailable",
                symbol=symbol,
                direction=direction_name,
            )

        api_key, api_secret = _credentials(account)
        snapshot = _account_service.account(api_key, api_secret, force_refresh=True)
        if snapshot.account_type != "UM_FUTURE":
            _fail_account(account, "portfolio_margin_unsupported")
            return _manual_follow_result(
                status="blocked",
                reason="portfolio_margin_unsupported",
                symbol=symbol,
                direction=direction_name,
            )
        configured_mode = str(config.get("position_mode") or "one_way")
        if _cached_position_mode(account, api_key, api_secret) != configured_mode:
            _fail_account(account, "position_mode_changed")
            return _manual_follow_result(
                status="blocked",
                reason="position_mode_changed",
                symbol=symbol,
                direction=direction_name,
            )

        market_state_changed = _reconcile_intents(account, api_key, api_secret)
        if market_state_changed:
            snapshot = _account_service.account(api_key, api_secret, force_refresh=True)
        positions = {_position_key(item): item for item in snapshot.positions}
        occupied_symbols = {key[0] for key in positions}
        if symbol in occupied_symbols:
            return _manual_follow_result(
                status="blocked",
                reason="symbol_already_open",
                symbol=symbol,
                direction=direction_name,
            )
        max_positions = max(1, min(int(config.get("max_positions", 1)), 20))
        if len(positions) >= max_positions:
            return _manual_follow_result(
                status="blocked",
                reason="max_positions_reached",
                symbol=symbol,
                direction=direction_name,
            )

        managed = _managed_positions(account)
        protection_counts = _protection_counts(account, managed)
        review_warnings = _risk_review_warnings(positions, managed, protection_counts)
        policy = policy_from_config(config)
        stop_prices = _current_stop_prices(account, managed)
        open_risk = _current_open_risk(
            snapshot,
            managed,
            stop_prices,
            exit_cost_bps=policy.round_trip_cost_bps,
        )
        if not _entry_loss_guard(
            account,
            snapshot,
            policy,
            review_warnings=review_warnings,
        ):
            return _manual_follow_result(
                status="blocked",
                reason=("risk_review_required" if review_warnings else "loss_limit_reached"),
                symbol=symbol,
                direction=direction_name,
            )
        if account.get("_local_audit_pending", False):
            return _manual_follow_result(
                status="blocked",
                reason="filled_audit_pending",
                symbol=symbol,
                direction=direction_name,
            )

        try:
            price = float(_trading_client.ticker_price(symbol))
        except (BinanceAccountClientError, TypeError, ValueError, OverflowError):
            return _manual_follow_result(
                status="blocked",
                reason="ticker_unavailable",
                symbol=symbol,
                direction=direction_name,
            )
        if not math.isfinite(price) or price <= 0:
            return _manual_follow_result(
                status="blocked",
                reason="ticker_unavailable",
                symbol=symbol,
                direction=direction_name,
            )
        admission = symbol_admission(symbol, sorted(occupied_symbols), policy=policy)
        if not admission.allowed:
            return _manual_follow_result(
                status="blocked",
                reason="symbol_risk_blocked",
                symbol=symbol,
                direction=direction_name,
            )

        direction, atr, basis, signal_time, signal_evidence = _manual_follow_signal_context(
            account,
            symbol=symbol,
            direction_name=direction_name,
            price=price,
            opportunity_public_id=opportunity_public_id,
            prediction_public_id=prediction_public_id,
            selected_at=selected_at,
            selected_evidence=selected_evidence,
            selected_score=selected_score,
        )
        signal_key_suffix = f"manual:{manual_attempt_id}"
        signal_key = (
            f"live:{account['deployment_id']}:{symbol}:{signal_time}:open:{direction}:"
            f"{signal_key_suffix}"
        )
        existing = _manual_intent(account, signal_key)
        if existing is not None:
            existing_status = str(existing.get("status") or "unknown")
            if existing_status == "filled":
                status = "duplicate"
                reason = "already_filled"
            elif existing_status in {"created", "submitted", "unknown"}:
                status = "pending"
                reason = "order_already_exists"
            else:
                status = "blocked"
                reason = "exchange_rejected"
            return _manual_follow_result(
                status=status,
                reason=reason,
                symbol=symbol,
                direction=direction_name,
                intent=existing,
            )

        opened = _open_position(
            account,
            api_key,
            api_secret,
            snapshot,
            symbol=symbol,
            direction=direction,
            price=price,
            atr=atr,
            signal_time=signal_time,
            current_open_risk=open_risk,
            basis=basis,
            signal_evidence={
                **signal_evidence,
                "manual_follow": True,
                "manual_attempt_id": manual_attempt_id,
            },
            signal_key_suffix=signal_key_suffix,
        )
        intent = _manual_intent(account, signal_key)
        if opened:
            return _manual_follow_result(
                status="filled",
                reason="filled_and_protected",
                symbol=symbol,
                direction=direction_name,
                intent=intent,
            )
        if intent is None:
            reason = "order_not_submitted"
            status = "blocked"
        elif str(intent.get("status") or "") in {"created", "submitted", "unknown"}:
            reason = "order_status_uncertain"
            status = "pending"
        elif str(intent.get("status") or "") in {"rejected", "canceled"}:
            reason = "exchange_rejected"
            status = "blocked"
        else:
            reason = "execution_failed_closed"
            status = "blocked"
        return _manual_follow_result(
            status=status,
            reason=reason,
            symbol=symbol,
            direction=direction_name,
            intent=intent,
        )


def _tick_account_unlocked(account: dict[str, Any]) -> None:
    if _account_service is None or _trading_client is None:
        raise RuntimeError("live engine is not configured")
    api_key, api_secret = _credentials(account)
    snapshot = _account_service.account(api_key, api_secret, force_refresh=True)
    if snapshot.account_type != "UM_FUTURE":
        _fail_account(account, "portfolio_margin_unsupported")
        return
    config = account["config_json"]
    policy = policy_from_config(config)
    try:
        _execution_timeframe_seconds(account)
    except (StrategyEvaluationError, TypeError, ValueError):
        _fail_account(account, "strategy_timeframe_invalid")
        return
    configured_mode = str(config.get("position_mode") or "one_way")
    current_mode = _cached_position_mode(account, api_key, api_secret)
    if current_mode != configured_mode:
        _fail_account(account, "position_mode_changed")
        return
    market_state_changed = _reconcile_intents(account, api_key, api_secret)
    if market_state_changed:
        snapshot = _account_service.account(api_key, api_secret, force_refresh=True)
    positions = {_position_key(item): item for item in snapshot.positions}
    managed = _managed_positions(account)
    # A contract removed from the entry universe must remain managed until its
    # existing position is reconciled and safely closed. Never orphan exits or
    # exchange-native protection merely because Binance stopped listing it.
    entry_symbols = _strategy_universe(config)
    symbols = list(
        dict.fromkeys(
            (
                *entry_symbols,
                *(symbol for symbol, _position_side in managed),
            )
        )
    )
    _cancel_orphan_protections(account, api_key, api_secret, managed, set(positions))
    protection_counts = _protection_counts(account, managed)
    stop_prices = _current_stop_prices(account, managed)
    runtime_state = _json_object(account.get("runtime_state_json"))
    raw_profit_guards = runtime_state.get("live_profit_guards")
    profit_guards = (
        {
            str(key): dict(value)
            for key, value in raw_profit_guards.items()
            if isinstance(value, dict)
        }
        if isinstance(raw_profit_guards, dict)
        else {}
    )
    active_profit_guard_keys = {
        _live_profit_guard_key(opened)
        for key, opened in managed.items()
        if key in positions and not _is_manual_follow_open(opened)
    }
    profit_guards_changed = any(
        key not in active_profit_guard_keys for key in profit_guards
    )
    profit_guards = {
        key: value
        for key, value in profit_guards.items()
        if key in active_profit_guard_keys
    }
    max_positions = max(1, min(int(config.get("max_positions", 1)), 20))
    positions_changed = False

    for key in managed:
        symbol, position_side = key
        if symbol not in symbols:
            continue
        position = positions.get(key)
        if position is None:
            _cancel_protection(account, api_key, api_secret, symbol, position_side)
            _record_reconciled_close(account, managed[key])
            continue
        if _is_manual_follow_open(managed[key]):
            # Manual positions remain under the user's control.  Reconciliation
            # above still observes an exchange-side close, while native stop/TP
            # protection remains on Binance until the position is gone.
            continue
        side = 1 if position_side == "LONG" or position["side"] == "long" else -1
        observed_at = int(time.time())
        profit_guard_key: str | None = None
        profit_guard_exit = False
        holding_period_expired = False
        if not _is_grandfathered_open(managed[key]):
            if protection_counts.get(key, 0) != 2:
                _close_position(account, api_key, api_secret, position, "protection_missing")
                _fail_account(account, "protection_missing")
                return
            stop_price = stop_prices.get(key)
            liquidation_price = position.get("liquidation_price")
            entry_price = position.get("entry_price")
            if not stop_price or liquidation_price is None or not entry_price:
                _close_position(
                    account,
                    api_key,
                    api_secret,
                    position,
                    "position_state_unverified",
                )
                _fail_account(account, "position_state_unverified")
                return
            if not _exchange_liquidation_is_safe(
                entry_price=entry_price,
                stop_price=stop_price,
                liquidation_price=liquidation_price,
                direction=side,
                min_buffer_pct=policy.liquidation_buffer_pct,
            ):
                _close_position(
                    account,
                    api_key,
                    api_secret,
                    position,
                    "liquidation_buffer_unsafe",
                )
                _fail_account(account, "liquidation_buffer_unsafe")
                return
            profit_guard_key = _live_profit_guard_key(managed[key])
            profit_guard, profit_guard_exit = _live_profit_guard_snapshot(
                position,
                managed[key],
                profit_guards.get(profit_guard_key),
                exit_cost_bps=policy.round_trip_cost_bps,
                observed_at=observed_at,
            )
            if profit_guard is not None and profit_guard != profit_guards.get(
                profit_guard_key
            ):
                profit_guards[profit_guard_key] = profit_guard
                profit_guards_changed = True
            opened_at = _opened_at_seconds(managed[key])
            entry_basis = _json_object(managed[key].get("entry_basis_json"))
            captured_timing = entry_basis.get("execution_policy")
            try:
                timing_policy = resolve_strategy_timing_policy(
                    account.get("strategy_snapshot_json") or {},
                    config,
                    captured=(
                        captured_timing
                        if isinstance(captured_timing, dict)
                        else None
                    ),
                )
                holding_period_expired = (
                    opened_at is not None
                    and timing_policy.expired(
                        opened_at=opened_at,
                        observed_at=observed_at,
                    )
                )
            except (StrategyEvaluationError, TypeError, ValueError):
                _fail_account(account, "position_holding_policy_invalid")
                return
        direction, _, _, signal_time, signal_evidence = _execution_signal(account, symbol)
        strategy_reversal = bool(
            signal_time is not None
            and _signal_is_fresh(
                account,
                signal_time,
                policy,
                signal_evidence,
            )
            and direction == -side
        )
        selected_exit = DEFAULT_EXIT_POLICY.select(
            price=position.get("mark_price") or position.get("entry_price"),
            observed_at=observed_at,
            profit_guard_exit=profit_guard_exit,
            strategy_reversal=strategy_reversal,
            holding_period_expired=holding_period_expired,
        )
        if selected_exit is not None:
            closed = _close_position(
                account,
                api_key,
                api_secret,
                position,
                selected_exit.reason,
            )
            positions_changed = closed or positions_changed
            if closed and selected_exit.reason == "profit_guard" and profit_guard_key:
                profit_guards.pop(profit_guard_key, None)
                profit_guards_changed = True

    if profit_guards_changed:
        _persist_live_profit_guards(account, profit_guards)
    if positions_changed:
        snapshot = _account_service.account(api_key, api_secret, force_refresh=True)
    positions = {_position_key(item): item for item in snapshot.positions}
    managed = _managed_positions(account)
    _cancel_orphan_protections(account, api_key, api_secret, managed, set(positions))
    position_count = len(positions)
    protection_counts = _protection_counts(account, managed)
    review_warnings = _risk_review_warnings(positions, managed, protection_counts)
    stop_prices = _current_stop_prices(account, managed)
    open_risk = _current_open_risk(
        snapshot,
        managed,
        stop_prices,
        exit_cost_bps=policy.round_trip_cost_bps,
    )
    allow_new_entries = _entry_loss_guard(
        account, snapshot, policy, review_warnings=review_warnings
    )
    allow_new_entries = allow_new_entries and not account.get("_local_audit_pending", False)
    symbol_set = set(symbols)
    prices = {
        row["symbol"]: row
        for row in store.query("SELECT symbol,price,ts FROM ticker WHERE price IS NOT NULL")
        if row["symbol"] in symbol_set
    }
    occupied_symbols = {key[0] for key in positions}
    if allow_new_entries and position_count < max_positions:
        for symbol in entry_symbols:
            if symbol in occupied_symbols:
                continue
            ticker = prices.get(symbol)
            if ticker is None:
                continue
            try:
                price = float(ticker["price"])
                ticker_fresh = market_data_freshness(
                    ticker["ts"],
                    max_age_seconds=policy.max_ticker_age_seconds,
                ).fresh
            except (KeyError, TypeError, ValueError):
                continue
            if not ticker_fresh or not math.isfinite(price) or price <= 0:
                continue
            admission = symbol_admission(
                symbol,
                sorted(occupied_symbols),
                policy=policy,
            )
            if not admission.allowed:
                continue
            direction, atr, basis, signal_time, signal_evidence = _execution_signal(
                account,
                symbol,
                price=price,
            )
            if (
                direction not in {-1, 1}
                or signal_time is None
                or not _signal_is_fresh(
                    account,
                    signal_time,
                    policy,
                    signal_evidence,
                )
            ):
                continue
            if not _automatic_directional_exposure_allowed(
                account,
                direction,
                positions,
            ):
                continue
            _open_position(
                account,
                api_key,
                api_secret,
                snapshot,
                symbol=symbol,
                direction=direction,
                price=price,
                atr=atr,
                signal_time=signal_time,
                current_open_risk=open_risk,
                basis=basis,
                signal_evidence=signal_evidence,
            )
            break
    store.execute(
        """UPDATE live_trading_accounts
           SET last_tick_at=UTC_TIMESTAMP(),last_error_code=?,updated_at=UTC_TIMESTAMP()
           WHERE id=? AND user_id=? AND status='active'""",
        (
            (
                "risk_review_required"
                if review_warnings
                else "filled_audit_pending"
                if account.get("_local_audit_pending")
                else None
            ),
            account["id"],
            account["user_id"],
        ),
    )


def _tick_account(account: dict[str, Any]) -> None:
    with _account_execution_lock(int(account["id"])):
        _tick_account_unlocked(account)


def _recover_account(account: dict[str, Any]) -> None:
    """Orchestrate application recovery actions for one physical account."""
    if _account_service is None:
        raise RuntimeError("live engine is not configured")
    api_key, api_secret = _credentials(account)
    snapshot = _account_service.account(api_key, api_secret, force_refresh=True)
    if snapshot.account_type != "UM_FUTURE":
        return
    market_state_changed = _reconcile_intents(account, api_key, api_secret)
    if market_state_changed:
        snapshot = _account_service.account(api_key, api_secret, force_refresh=True)
    positions = {_position_key(item): item for item in snapshot.positions}
    managed = _managed_positions(account)
    _cancel_orphan_protections(account, api_key, api_secret, managed, set(positions))
    protection_counts = _protection_counts(account, managed)
    failed_close_keys = _failed_close_keys(account, managed)
    recovery_actions = LiveAccountRecoveryService().plan(
        exchange_positions=positions,
        managed_positions=managed,
        protection_counts=protection_counts,
        failed_close_keys=failed_close_keys,
        grandfathered_keys={
            key for key, opened in managed.items() if _is_grandfathered_open(opened)
        },
    )
    for action in recovery_actions:
        key = action.key
        opened = managed[key]
        symbol, position_side = key
        position = positions.get(key)
        if action.kind == "record_close":
            _cancel_protection(account, api_key, api_secret, symbol, position_side)
            _record_reconciled_close(account, opened)
            continue
        if action.kind != "close_and_fail" or position is None:
            raise RuntimeError("live account recovery plan is invalid")
        _close_position(
            account,
            api_key,
            api_secret,
            position,
            action.reason,
        )
        _fail_account(account, action.reason)
    review_warnings = _risk_review_warnings(positions, managed, protection_counts)
    if review_warnings:
        _persist_risk_review(account, review_warnings)


def tick(account_id: int | None = None) -> None:
    if _settings is None:
        return
    with store.advisory_lock("quantdesk-binance-live-tick", 0) as acquired:
        if not acquired:
            return
        if account_id is None:
            # Resolve crash/timeout intents even though fail-closed accounts are
            # no longer eligible to evaluate strategy signals.
            for account in _recovery_accounts():
                account_id_value = int(account["id"])
                if _account_backoff_active(account_id_value):
                    continue
                try:
                    _recover_account(account)
                    _clear_account_backoff(account_id_value)
                except SecurityError:
                    continue
                except BinanceAccountClientError as exc:
                    _record_account_backoff(account_id_value, exc.category)
                    continue
                except Exception as exc:
                    print(f"[live] recovery reconciliation failed: {type(exc).__name__}")
        for account in _active_accounts(account_id):
            account_id_value = int(account["id"])
            if _account_backoff_active(account_id_value):
                continue
            try:
                _tick_account(account)
                _clear_account_backoff(account_id_value)
            except SecurityError:
                _fail_account(account, "credential_changed")
            except BinanceAccountClientError as exc:
                _record_account_backoff(account_id_value, exc.category)
                store.execute(
                    """UPDATE live_trading_accounts
                       SET last_error_code=?,updated_at=UTC_TIMESTAMP()
                       WHERE id=? AND user_id=?""",
                    (exc.category[:64], account["id"], account["user_id"]),
                )
            except Exception as exc:
                print(f"[live] account tick failed: {type(exc).__name__}")
                _fail_account(account, "internal_error")


def live_loop() -> None:
    settings = _settings
    if settings is None:
        raise RuntimeError("live engine is not configured")
    print("[live] Binance live executor started")
    while True:
        try:
            tick()
        except Exception as exc:
            print(f"[live] tick failed: {type(exc).__name__}")
        time.sleep(settings.binance_live_trading_interval_seconds)
