"""Deterministic live reconciliation and recovery orchestration.

The module contains no Binance client, SQL, credential, or worker-loop code.
Those details are injected by the composition root so live execution has one
testable recovery policy instead of strategy decisions hidden in adapters.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

PositionKey = tuple[str, str]


class LiveOrderStatePending(RuntimeError):
    """A market intent is still open and must fence further execution."""


class LiveOrderStateUnknown(RuntimeError):
    """The exchange returned a state that cannot be classified safely."""


@dataclass(frozen=True, slots=True)
class LiveOrderReconciliationOutcome:
    market_state_changed: bool
    reconciled_count: int


class LiveOrderReconciliationService:
    """Resolve durable non-terminal intents against authoritative orders."""

    def __init__(
        self,
        *,
        load_intents: Callable[[int, int], Sequence[Mapping[str, Any]]],
        load_open_orders: Callable[[str, str], Sequence[Mapping[str, Any]]],
        query_market_order: Callable[[str, str, str, str], Mapping[str, Any]],
        query_protection_order: Callable[[str, str, str], Mapping[str, Any]],
        update_intent: Callable[..., None],
        normalize_open_order: Callable[[Mapping[str, Any]], dict[str, Any]],
        classify_status: Callable[[Mapping[str, Any]], str],
        is_order_not_found: Callable[[Exception], bool],
    ) -> None:
        self._load_intents = load_intents
        self._load_open_orders = load_open_orders
        self._query_market_order = query_market_order
        self._query_protection_order = query_protection_order
        self._update_intent = update_intent
        self._normalize_open_order = normalize_open_order
        self._classify_status = classify_status
        self._is_order_not_found = is_order_not_found

    def reconcile(
        self,
        *,
        user_id: int,
        account_id: int,
        api_key: str,
        api_secret: str,
    ) -> LiveOrderReconciliationOutcome:
        rows = self._load_intents(user_id, account_id)
        if not rows:
            return LiveOrderReconciliationOutcome(False, 0)
        open_orders = self._load_open_orders(api_key, api_secret)
        current_by_client_id = {
            str(order.get("client_order_id") or ""): order
            for order in open_orders
            if str(order.get("client_order_id") or "")
        }
        changed = False
        pending_market = False
        for raw_row in rows:
            row = dict(raw_row)
            intent_id = int(row["id"])
            intent_user_id = int(row["user_id"])
            action = str(row["action"])
            client_order_id = str(row["client_order_id"])
            current = current_by_client_id.get(client_order_id)
            if current is not None:
                self._update_intent(
                    intent_id,
                    intent_user_id,
                    status="submitted",
                    response=self._normalize_open_order(current),
                )
                pending_market = pending_market or action in {"open", "close"}
                continue
            try:
                if action in {"stop", "take_profit"}:
                    response = self._query_protection_order(
                        api_key, api_secret, client_order_id
                    )
                else:
                    response = self._query_market_order(
                        api_key,
                        api_secret,
                        str(row["symbol"]),
                        client_order_id,
                    )
            except Exception as exc:
                if not self._is_order_not_found(exc):
                    raise
                self._update_intent(
                    intent_id,
                    intent_user_id,
                    status="canceled",
                    error_code="exchange_order_not_found",
                )
                changed = changed or action in {"open", "close"}
                continue
            status = self._classify_status(response)
            self._update_intent(
                intent_id,
                intent_user_id,
                status=status,
                response=dict(response),
                error_code=(
                    "unrecognized_exchange_status" if status == "unknown" else None
                ),
            )
            if action in {"open", "close"}:
                if status == "submitted":
                    pending_market = True
                else:
                    changed = True
            elif action in {"stop", "take_profit"} and status == "filled":
                changed = True
            if status == "unknown":
                raise LiveOrderStateUnknown("unrecognized exchange order status")
        if pending_market:
            raise LiveOrderStatePending("market order remains non-terminal")
        return LiveOrderReconciliationOutcome(changed, len(rows))


class LivePositionSyncService:
    """Build the current managed-position projection from immutable intents."""

    @staticmethod
    def managed_positions(
        rows: Sequence[Mapping[str, Any]],
    ) -> dict[PositionKey, dict[str, Any]]:
        latest: dict[PositionKey, dict[str, Any]] = {}
        for raw_row in rows:
            row = dict(raw_row)
            key = (
                str(row["symbol"]).upper(),
                str(row.get("position_side") or "BOTH").upper(),
            )
            if key not in latest:
                latest[key] = row
        return {key: row for key, row in latest.items() if row["action"] == "open"}


@dataclass(frozen=True, slots=True)
class LiveRecoveryAction:
    kind: str
    key: PositionKey
    reason: str


class ProtectionRecoveryService:
    """Choose fail-closed actions for missing exchange protection."""

    @staticmethod
    def coverage_counts(
        rows: Sequence[Mapping[str, Any]],
        managed_positions: Mapping[PositionKey, Mapping[str, Any]],
    ) -> dict[PositionKey, int]:
        actions: dict[PositionKey, set[str]] = {}
        for row in rows:
            key = (
                str(row["symbol"]).upper(),
                str(row.get("position_side") or "BOTH").upper(),
            )
            opened = managed_positions.get(key)
            if opened is None or int(row["id"]) <= int(opened["id"]):
                continue
            action = str(row.get("action") or "")
            if action in {"stop", "take_profit"}:
                actions.setdefault(key, set()).add(action)
        return {key: len(value) for key, value in actions.items()}

    @staticmethod
    def failed_close_keys(
        rows: Sequence[Mapping[str, Any]],
        managed_positions: Mapping[PositionKey, Mapping[str, Any]],
    ) -> set[PositionKey]:
        failed: set[PositionKey] = set()
        for row in rows:
            key = (
                str(row["symbol"]).upper(),
                str(row.get("position_side") or "BOTH").upper(),
            )
            opened = managed_positions.get(key)
            if opened is not None and int(row["id"]) > int(opened["id"]):
                failed.add(key)
        return failed

    @staticmethod
    def orphan_keys(
        rows: Sequence[Mapping[str, Any]],
        managed_positions: Mapping[PositionKey, Mapping[str, Any]],
        exchange_position_keys: set[PositionKey],
    ) -> tuple[PositionKey, ...]:
        keys = {
            (
                str(row["symbol"]).upper(),
                str(row.get("position_side") or "BOTH").upper(),
            )
            for row in rows
        }
        return tuple(
            sorted(keys - set(managed_positions) - exchange_position_keys)
        )

    @staticmethod
    def plan(
        *,
        exchange_positions: Mapping[PositionKey, Mapping[str, Any]],
        managed_positions: Mapping[PositionKey, Mapping[str, Any]],
        protection_counts: Mapping[PositionKey, int],
        failed_close_keys: set[PositionKey],
        grandfathered_keys: set[PositionKey],
    ) -> tuple[LiveRecoveryAction, ...]:
        actions: list[LiveRecoveryAction] = []
        for key in managed_positions:
            if key not in exchange_positions:
                actions.append(LiveRecoveryAction("record_close", key, "reconciled_close"))
            elif key in failed_close_keys:
                actions.append(
                    LiveRecoveryAction("close_and_fail", key, "recovery_close_retry")
                )
            elif key in grandfathered_keys:
                continue
            elif int(protection_counts.get(key, 0)) != 2:
                actions.append(
                    LiveRecoveryAction(
                        "close_and_fail", key, "recovery_protection_missing"
                    )
                )
        return tuple(actions)


class LiveAccountRecoveryService:
    """Coordinate position reconciliation and protection recovery planning."""

    def __init__(self, protection: ProtectionRecoveryService | None = None) -> None:
        self._protection = protection or ProtectionRecoveryService()

    def plan(
        self,
        *,
        exchange_positions: Mapping[PositionKey, Mapping[str, Any]],
        managed_positions: Mapping[PositionKey, Mapping[str, Any]],
        protection_counts: Mapping[PositionKey, int],
        failed_close_keys: set[PositionKey],
        grandfathered_keys: set[PositionKey],
    ) -> tuple[LiveRecoveryAction, ...]:
        return self._protection.plan(
            exchange_positions=exchange_positions,
            managed_positions=managed_positions,
            protection_counts=protection_counts,
            failed_close_keys=failed_close_keys,
            grandfathered_keys=grandfathered_keys,
        )
