"""Preflight, safe-mode and kill-switch controls without framework dependencies."""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from math import isfinite

from ..domain.execution import ExecutionContext, OrderIntent
from ..domain.safety import (
    PreflightCheck,
    PreflightReport,
    SafetyDecision,
    SafetyMode,
    SafetySnapshot,
)
from ..domain.trading import BrokerError
from .ports import Broker, MarketDataFeed


@dataclass(frozen=True, slots=True)
class PreflightPolicy:
    max_quote_age_seconds: float = 120.0
    max_account_age_seconds: float = 120.0
    max_future_skew_seconds: float = 5.0
    require_explicit_trade_permission: bool = True

    def __post_init__(self) -> None:
        if not all(
            isfinite(value)
            for value in (
                self.max_quote_age_seconds,
                self.max_account_age_seconds,
                self.max_future_skew_seconds,
            )
        ):
            raise ValueError("preflight time bounds must be finite")
        if self.max_quote_age_seconds <= 0 or self.max_account_age_seconds <= 0:
            raise ValueError("preflight maximum ages must be positive")
        if self.max_future_skew_seconds < 0:
            raise ValueError("max_future_skew_seconds must not be negative")


@dataclass(frozen=True, slots=True)
class PreflightOutcome:
    report: PreflightReport
    context: ExecutionContext | None

    def __post_init__(self) -> None:
        if self.report.ready != (self.context is not None):
            raise ValueError("ready preflight reports must include their execution context")


class PreflightService:
    """Collect a fresh, normalized account and market snapshot before risk evaluation."""

    __slots__ = ("_broker", "_clock", "_feed", "_policy")

    def __init__(
        self,
        broker: Broker,
        feed: MarketDataFeed,
        policy: PreflightPolicy | None = None,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._broker = broker
        self._feed = feed
        self._policy = policy or PreflightPolicy()
        self._clock = clock or (lambda: datetime.now(UTC))

    @property
    def policy(self) -> PreflightPolicy:
        """Expose the immutable policy pinned by durable LIVE authorization."""

        return self._policy

    def run(self, intent: OrderIntent) -> PreflightOutcome:
        now = self._clock()
        checks: list[PreflightCheck] = []
        account = None
        quote = None
        rules = None
        position_mode = None

        try:
            account = self._broker.account_snapshot()
            checks.append(PreflightCheck("account", True))
        except Exception as exc:
            checks.append(PreflightCheck("account", False, _dependency_code("account", exc)))

        if account is None:
            checks.append(PreflightCheck("account_freshness", False, "account_unavailable"))
            checks.append(PreflightCheck("trade_permission", False, "account_unavailable"))
        else:
            checks.append(
                _freshness_check(
                    "account_freshness",
                    account.observed_at,
                    now,
                    self._policy.max_account_age_seconds,
                    self._policy.max_future_skew_seconds,
                )
            )
            permission_ok = account.can_trade is True or (
                account.can_trade is None
                and not self._policy.require_explicit_trade_permission
            )
            checks.append(
                PreflightCheck(
                    "trade_permission",
                    permission_ok,
                    "ok" if permission_ok else "account_cannot_trade",
                )
            )

        try:
            quote = self._feed.latest_quote(intent.symbol)
            if quote is None:
                checks.append(PreflightCheck("quote", False, "quote_unavailable"))
                checks.append(
                    PreflightCheck("quote_symbol", False, "quote_unavailable")
                )
            else:
                checks.append(PreflightCheck("quote", True))
                if quote.symbol != intent.symbol:
                    checks.append(
                        PreflightCheck("quote_symbol", False, "quote_symbol_mismatch")
                    )
                else:
                    checks.append(PreflightCheck("quote_symbol", True))
        except Exception as exc:
            checks.append(PreflightCheck("quote", False, _dependency_code("quote", exc)))
            checks.append(PreflightCheck("quote_symbol", False, "quote_unavailable"))

        if quote is None:
            checks.append(PreflightCheck("quote_freshness", False, "quote_unavailable"))
        else:
            checks.append(
                _freshness_check(
                    "quote_freshness",
                    quote.observed_at,
                    now,
                    self._policy.max_quote_age_seconds,
                    self._policy.max_future_skew_seconds,
                )
            )

        try:
            rules = self._broker.instrument_rules(intent.symbol)
            if rules.symbol != intent.symbol:
                checks.append(
                    PreflightCheck(
                        "instrument_rules",
                        False,
                        "instrument_rules_symbol_mismatch",
                    )
                )
            else:
                checks.append(PreflightCheck("instrument_rules", True))
        except Exception as exc:
            checks.append(
                PreflightCheck(
                    "instrument_rules", False, _dependency_code("instrument_rules", exc)
                )
            )

        try:
            position_mode = self._broker.position_mode()
            checks.append(PreflightCheck("position_mode", True))
        except Exception as exc:
            checks.append(
                PreflightCheck("position_mode", False, _dependency_code("position_mode", exc))
            )

        report = PreflightReport(now, tuple(checks))
        context = None
        if report.ready:
            if account is None or quote is None or rules is None or position_mode is None:
                raise RuntimeError("passing preflight checks did not produce a complete context")
            context = ExecutionContext(account, quote, rules, position_mode)
        return PreflightOutcome(report, context)


def _dependency_code(component: str, exc: Exception) -> str:
    category = exc.category if isinstance(exc, BrokerError) else "unavailable"
    safe_category = "".join(character for character in category if character.isalnum() or character == "_")
    return f"{component}_{safe_category or 'unavailable'}"[:64]


def _freshness_check(
    name: str,
    observed_at: datetime,
    now: datetime,
    max_age_seconds: float,
    max_future_skew_seconds: float,
) -> PreflightCheck:
    if observed_at.tzinfo is None or observed_at.utcoffset() is None:
        return PreflightCheck(name, False, f"{name}_timezone_missing")
    age = (now - observed_at).total_seconds()
    if age < -max_future_skew_seconds:
        return PreflightCheck(name, False, f"{name}_in_future")
    if age > max_age_seconds:
        return PreflightCheck(name, False, f"{name}_stale")
    return PreflightCheck(name, True)


class ExecutionSafetyController:
    """A process-local, fail-closed gate; durable state can wrap the same API later."""

    __slots__ = (
        "_clock",
        "_consecutive_failures",
        "_failure_threshold",
        "_kill_reason",
        "_lock",
        "_safe_mode_reason",
    )

    def __init__(
        self,
        *,
        failure_threshold: int = 3,
        kill_switch_engaged: bool = False,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if failure_threshold < 1:
            raise ValueError("failure_threshold must be positive")
        self._failure_threshold = failure_threshold
        self._clock = clock or (lambda: datetime.now(UTC))
        self._lock = threading.RLock()
        self._consecutive_failures = 0
        self._safe_mode_reason: str | None = None
        self._kill_reason = "kill_switch_configured" if kill_switch_engaged else None

    def decision(self, intent: OrderIntent) -> SafetyDecision:
        with self._lock:
            if self._kill_reason is not None:
                return SafetyDecision(False, SafetyMode.KILL_SWITCH, self._kill_reason)
            if self._safe_mode_reason is not None and not intent.risk_reducing:
                return SafetyDecision(False, SafetyMode.SAFE_MODE, self._safe_mode_reason)
            mode = SafetyMode.SAFE_MODE if self._safe_mode_reason else SafetyMode.NORMAL
            return SafetyDecision(True, mode, "ok")

    def record_success(self) -> SafetySnapshot:
        with self._lock:
            if self._safe_mode_reason is None:
                self._consecutive_failures = 0
            return self._snapshot()

    def record_failure(self, reason_code: str) -> SafetySnapshot:
        reason = _reason(reason_code)
        with self._lock:
            self._consecutive_failures += 1
            if (
                self._safe_mode_reason is None
                and self._consecutive_failures >= self._failure_threshold
            ):
                self._safe_mode_reason = f"repeated_{reason}"[:64]
            return self._snapshot()

    def engage_safe_mode(self, reason_code: str) -> SafetySnapshot:
        with self._lock:
            self._safe_mode_reason = _reason(reason_code)
            return self._snapshot()

    def recover_safe_mode(self) -> SafetySnapshot:
        """Explicit operator recovery; successes never clear a latched safe mode."""

        with self._lock:
            self._safe_mode_reason = None
            self._consecutive_failures = 0
            return self._snapshot()

    def engage_kill_switch(self, reason_code: str = "kill_switch_engaged") -> SafetySnapshot:
        with self._lock:
            self._kill_reason = _reason(reason_code)
            return self._snapshot()

    def release_kill_switch(self) -> SafetySnapshot:
        with self._lock:
            self._kill_reason = None
            return self._snapshot()

    def snapshot(self) -> SafetySnapshot:
        with self._lock:
            return self._snapshot()

    def _snapshot(self) -> SafetySnapshot:
        if self._kill_reason is not None:
            mode = SafetyMode.KILL_SWITCH
            reasons = (self._kill_reason,)
        elif self._safe_mode_reason is not None:
            mode = SafetyMode.SAFE_MODE
            reasons = (self._safe_mode_reason,)
        else:
            mode = SafetyMode.NORMAL
            reasons = ()
        return SafetySnapshot(mode, self._consecutive_failures, reasons, self._clock())


def _reason(value: str) -> str:
    normalized = "".join(
        character for character in str(value).strip().lower() if character.isalnum() or character == "_"
    )[:64]
    if not normalized:
        raise ValueError("reason code cannot be empty")
    return normalized
