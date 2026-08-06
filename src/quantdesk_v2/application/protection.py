"""Idempotent installation and verification of position protection orders."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from decimal import Decimal

from ..domain.protection import ProtectionAction, ProtectionPlan
from ..domain.trading import (
    BrokerOrder,
    OrderReference,
    OrderStatus,
    TriggerOrder,
)
from .ports import Broker

_OPEN_ORDER_STATUSES = frozenset({OrderStatus.NEW, OrderStatus.PARTIALLY_FILLED})


class ProtectionInstallationError(RuntimeError):
    """Redacted failure that requires reconciliation before another attempt."""

    def __init__(self, code: str, *, rollback_complete: bool) -> None:
        super().__init__("protection installation failed")
        self.code = code
        self.rollback_complete = rollback_complete


@dataclass(frozen=True, slots=True)
class ProtectionStatus:
    expected_client_order_ids: tuple[str, str]
    present_actions: frozenset[ProtectionAction]
    missing_actions: frozenset[ProtectionAction]
    invalid_actions: frozenset[ProtectionAction]

    @property
    def complete(self) -> bool:
        return not self.missing_actions and not self.invalid_actions


class ProtectionService:
    """Install a stop/target pair through the normalized broker boundary.

    Exact deterministic client ids make retries reconcilable. A broker error is
    treated as an unknown outcome: callers must run ``verify`` before retrying.
    """

    def __init__(self, broker: Broker) -> None:
        self._broker = broker

    def verify(self, plan: ProtectionPlan, *, execution_scope: str) -> ProtectionStatus:
        desired = _desired_orders(plan, execution_scope)
        open_by_client_id = {
            order.reference.client_order_id: order
            for order in self._broker.open_orders()
            if order.reference.client_order_id in desired
        }
        present: set[ProtectionAction] = set()
        missing: set[ProtectionAction] = set()
        invalid: set[ProtectionAction] = set()
        for client_order_id, (action, expected) in desired.items():
            actual = open_by_client_id.get(client_order_id)
            if actual is None:
                missing.add(action)
            elif _matches(plan, expected, actual):
                present.add(action)
            else:
                invalid.add(action)
        return ProtectionStatus(
            expected_client_order_ids=tuple(desired),
            present_actions=frozenset(present),
            missing_actions=frozenset(missing),
            invalid_actions=frozenset(invalid),
        )

    def ensure(
        self,
        plan: ProtectionPlan,
        *,
        execution_scope: str,
    ) -> tuple[BrokerOrder, ...]:
        """Return the complete pair, placing only deterministically missing orders."""

        desired = _desired_orders(plan, execution_scope)
        open_by_client_id = {
            order.reference.client_order_id: order
            for order in self._broker.open_orders()
            if order.reference.client_order_id in desired
        }
        resolved: list[BrokerOrder] = []
        placed: list[BrokerOrder] = []
        for client_order_id, (action, expected) in desired.items():
            existing = open_by_client_id.get(client_order_id)
            if existing is not None:
                if not _matches(plan, expected, existing):
                    raise ProtectionInstallationError(
                        f"invalid_existing_{action.value}",
                        rollback_complete=True,
                    )
                resolved.append(existing)
                continue
            try:
                created = self._broker.place_trigger_order(expected)
            except Exception:
                _rollback(self._broker, placed)
                raise ProtectionInstallationError(
                    f"unknown_{action.value}_outcome",
                    # The failed call may have reached the broker. Even when all
                    # earlier known orders were canceled, only reconciliation by
                    # its deterministic id can prove that rollback is complete.
                    rollback_complete=False,
                ) from None
            if not _matches(plan, expected, created):
                rollback_complete = _rollback(self._broker, [*placed, created])
                raise ProtectionInstallationError(
                    f"invalid_{action.value}_response",
                    rollback_complete=rollback_complete,
                )
            placed.append(created)
            resolved.append(created)
        return tuple(resolved)

    def repair(
        self,
        plan: ProtectionPlan,
        *,
        execution_scope: str,
    ) -> tuple[BrokerOrder, ...]:
        """Reconcile the pair and install only missing orders.

        Invalid existing orders are never canceled automatically because a
        mismatched protection order can represent unknown external state.
        """

        return self.ensure(plan, execution_scope=execution_scope)


def protection_client_order_id(
    plan: ProtectionPlan,
    execution_scope: str,
    action: ProtectionAction,
) -> str:
    normalized_scope = str(execution_scope).strip()
    if not normalized_scope or len(normalized_scope) > 512:
        raise ValueError("invalid protection execution scope")
    material = f"{normalized_scope}\x1f{plan.signal_key(normalized_scope, action)}"
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()[:30]
    return f"qp-{digest}"


def _desired_orders(
    plan: ProtectionPlan,
    execution_scope: str,
) -> dict[str, tuple[ProtectionAction, TriggerOrder]]:
    desired: dict[str, tuple[ProtectionAction, TriggerOrder]] = {}
    for specification in plan.orders:
        client_order_id = protection_client_order_id(
            plan,
            execution_scope,
            specification.action,
        )
        desired[client_order_id] = (
            specification.action,
            TriggerOrder(
                symbol=plan.symbol,
                side=plan.close_side,
                order_type=specification.order_type,
                trigger_price=specification.trigger_price,
                client_order_id=client_order_id,
                position_side=plan.position_side,
                quantity=plan.quantity,
            ),
        )
    if len(desired) != 2:
        raise ValueError("protection plan must create two distinct orders")
    return desired


def _matches(plan: ProtectionPlan, expected: TriggerOrder, actual: BrokerOrder) -> bool:
    expected_quantity = expected.quantity if expected.quantity is not None else Decimal(0)
    return (
        actual.reference.client_order_id == expected.client_order_id
        and actual.reference.conditional
        and actual.symbol == plan.symbol
        and actual.side is plan.close_side
        and actual.position_side is plan.position_side
        and actual.order_type is expected.order_type
        and actual.trigger_price == expected.trigger_price
        and actual.quantity == expected_quantity
        and actual.status in _OPEN_ORDER_STATUSES
    )


def _rollback(broker: Broker, orders: list[BrokerOrder]) -> bool:
    complete = True
    for order in reversed(orders):
        try:
            broker.cancel_order(
                OrderReference(
                    client_order_id=order.reference.client_order_id,
                    symbol=order.symbol,
                    conditional=True,
                )
            )
        except Exception:
            complete = False
    return complete
