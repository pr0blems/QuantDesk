"""Deterministic broker adapter that simulates fills and performs no network writes."""

from __future__ import annotations

import re
import threading
from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal

from ..application.ports import MarketDataFeed
from ..domain.execution import ExecutionMode
from ..domain.trading import (
    AccountSnapshot,
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
    PositionMode,
    PositionSide,
    TriggerOrder,
)

_TEN_THOUSAND = Decimal("10000")
_DEFAULT_LEVERAGE = 10
_ZERO = Decimal(0)
_PHYSICAL_ACCOUNT_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@/-]{0,190}")


class ShadowBroker:
    """A broker-compatible dry-run boundary suitable for staged deployment.

    Available balance uses a conservative cash ledger: initial available balance,
    minus posted initial margin, plus released margin and realized PnL. Unrealized
    PnL is reported separately and is never credited as spendable balance.
    """

    __slots__ = (
        "_account",
        "_account_scope",
        "_clock",
        "_feed",
        "_leverage",
        "_lock",
        "_market",
        "_orders",
        "_physical_account_id",
        "_position_mode",
        "_positions",
        "_realized_pnl",
        "_rules",
        "_slippage_bps",
        "_available_balance",
        "_wallet_balance",
    )

    def __init__(
        self,
        *,
        account: AccountSnapshot,
        feed: MarketDataFeed,
        rules: Mapping[str, InstrumentRules],
        account_scope: str,
        physical_account_id: str,
        market: str,
        position_mode: PositionMode = PositionMode.ONE_WAY,
        slippage_bps: Decimal = Decimal("0"),
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not slippage_bps.is_finite() or slippage_bps < 0:
            raise ValueError("slippage_bps must be a non-negative finite Decimal")
        self._account = account
        self._account_scope = str(account_scope).strip()
        self._physical_account_id = str(physical_account_id).strip()
        self._market = str(market).strip().lower()
        if not self._account_scope or len(self._account_scope) > 191:
            raise ValueError("account_scope must contain 1-191 characters")
        if not _PHYSICAL_ACCOUNT_PATTERN.fullmatch(self._physical_account_id):
            raise ValueError("invalid physical_account_id")
        if not self._market or len(self._market) > 32:
            raise ValueError("market must contain 1-32 characters")
        self._feed = feed
        self._rules = {str(key).upper(): value for key, value in rules.items()}
        self._position_mode = position_mode
        self._slippage_bps = slippage_bps
        self._clock = clock or (lambda: datetime.now(UTC))
        self._orders: dict[str, BrokerOrder] = {}
        self._leverage: dict[str, int] = {}
        self._positions: dict[tuple[str, PositionSide], Position] = {}
        self._wallet_balance = account.wallet_balance
        self._available_balance = account.available_balance
        self._realized_pnl = _ZERO
        self._load_initial_positions(account.positions)
        self._account = replace(account, positions=self._position_tuple())
        self._lock = threading.RLock()

    @property
    def name(self) -> str:
        return "shadow"

    @property
    def execution_mode(self) -> ExecutionMode:
        return ExecutionMode.SHADOW

    @property
    def market(self) -> str:
        return self._market

    @property
    def account_scope(self) -> str:
        return self._account_scope

    @property
    def physical_account_id(self) -> str:
        return self._physical_account_id

    @property
    def realized_pnl(self) -> Decimal:
        """Cumulative realized PnL produced by this in-memory shadow ledger."""

        with self._lock:
            return self._realized_pnl

    def account_snapshot(self) -> AccountSnapshot:
        with self._lock:
            now = self._clock()
            quote_prices: dict[str, Decimal] = {}
            for position in self._positions.values():
                if position.symbol in quote_prices:
                    continue
                quote = self._feed.latest_quote(position.symbol)
                if quote is not None and quote.symbol == position.symbol:
                    quote_prices[position.symbol] = quote.price
            for key, position in tuple(self._positions.items()):
                mark_price = quote_prices.get(position.symbol)
                if mark_price is not None:
                    self._positions[key] = self._marked_position(
                        position,
                        mark_price,
                        now,
                    )
            self._sync_account(now)
            return self._account

    def open_orders(self) -> tuple[BrokerOrder, ...]:
        with self._lock:
            return tuple(
                order
                for order in self._orders.values()
                if order.status in {OrderStatus.NEW, OrderStatus.PARTIALLY_FILLED}
            )

    def instrument_rules(self, symbol: str) -> InstrumentRules:
        try:
            return self._rules[str(symbol).strip().upper()]
        except KeyError:
            raise BrokerError("unknown_instrument") from None

    def position_mode(self) -> PositionMode:
        return self._position_mode

    def configure_leverage(self, symbol: str, leverage: int) -> None:
        if isinstance(leverage, bool) or not isinstance(leverage, int) or leverage < 1:
            raise BrokerError("invalid_leverage")
        normalized_symbol = self.instrument_rules(symbol).symbol
        with self._lock:
            affected = {
                key: position
                for key, position in self._positions.items()
                if position.symbol == normalized_symbol
            }
            replacements: dict[tuple[str, PositionSide], Position] = {}
            margin_delta = _ZERO
            for key, position in affected.items():
                if position.entry_price is None:
                    raise BrokerError("position_cost_basis_unavailable")
                previous_margin = self._position_margin(position)
                next_margin = position.quantity * position.entry_price / Decimal(leverage)
                margin_delta += next_margin - previous_margin
                replacements[key] = replace(
                    position,
                    initial_margin=next_margin,
                    leverage=leverage,
                )
            if margin_delta > self._available_balance:
                raise BrokerError("insufficient_margin")
            self._available_balance -= margin_delta
            self._positions.update(replacements)
            self._leverage[normalized_symbol] = leverage
            self._sync_account(self._clock())

    def place_market_order(self, order: MarketOrder) -> BrokerOrder:
        with self._lock:
            existing = self._orders.get(order.client_order_id)
            if existing is not None:
                return existing
            rules = self.instrument_rules(order.symbol)
            try:
                normalized_quantity = rules.quantity(order.quantity)
            except ValueError:
                raise BrokerError("quantity_outside_instrument_rules") from None
            if normalized_quantity != order.quantity:
                raise BrokerError("quantity_step_mismatch")
            quote = self._feed.latest_quote(order.symbol)
            if quote is None:
                raise BrokerError("market_data")
            direction = Decimal(1) if order.side.value == "BUY" else Decimal(-1)
            price = quote.price * (
                Decimal(1) + direction * self._slippage_bps / _TEN_THOUSAND
            )
            result = BrokerOrder(
                reference=OrderReference(order.client_order_id, order.symbol),
                exchange_order_id=f"shadow-{len(self._orders) + 1}",
                symbol=order.symbol,
                side=order.side,
                position_side=order.position_side,
                order_type=OrderType.MARKET,
                status=OrderStatus.FILLED,
                exchange_status="SHADOW_FILLED",
                quantity=order.quantity,
                executed_quantity=order.quantity,
                average_price=price,
                reduce_only=order.reduce_only,
            )
            self._apply_market_fill(order, price)
            self._orders[order.client_order_id] = result
            return result

    def place_trigger_order(self, order: TriggerOrder) -> BrokerOrder:
        with self._lock:
            existing = self._orders.get(order.client_order_id)
            if existing is not None:
                return existing
            result = BrokerOrder(
                reference=OrderReference(
                    order.client_order_id, order.symbol, conditional=True
                ),
                exchange_order_id=f"shadow-trigger-{len(self._orders) + 1}",
                symbol=order.symbol,
                side=order.side,
                position_side=order.position_side,
                order_type=order.order_type,
                status=OrderStatus.NEW,
                exchange_status="SHADOW_NEW",
                quantity=order.quantity or Decimal(0),
                trigger_price=order.trigger_price,
            )
            self._orders[order.client_order_id] = result
            return result

    def get_order(self, reference: OrderReference) -> BrokerOrder:
        with self._lock:
            try:
                return self._orders[reference.client_order_id]
            except KeyError:
                raise BrokerError("order_not_found") from None

    def cancel_order(self, reference: OrderReference) -> None:
        with self._lock:
            order = self.get_order(reference)
            if order.status is OrderStatus.FILLED:
                raise BrokerError("order_already_filled")
            self._orders[reference.client_order_id] = replace(
                order,
                status=OrderStatus.CANCELED,
                exchange_status="SHADOW_CANCELED",
            )

    def _load_initial_positions(self, positions: tuple[Position, ...]) -> None:
        for position in positions:
            if self._position_mode is PositionMode.ONE_WAY:
                if position.position_side is not PositionSide.BOTH:
                    raise ValueError("one-way shadow positions must use BOTH")
            elif position.position_side is PositionSide.BOTH:
                raise ValueError("hedge-mode shadow positions must use LONG or SHORT")
            if (
                position.position_side is PositionSide.LONG
                and position.direction is not PositionDirection.LONG
            ) or (
                position.position_side is PositionSide.SHORT
                and position.direction is not PositionDirection.SHORT
            ):
                raise ValueError("position direction does not match position side")
            key = (position.symbol, position.position_side)
            if key in self._positions:
                raise ValueError("duplicate shadow position")
            leverage = position.leverage or self._leverage.get(
                position.symbol,
                _DEFAULT_LEVERAGE,
            )
            self._leverage.setdefault(position.symbol, leverage)
            margin = position.initial_margin
            if margin is None and position.entry_price is not None:
                margin = position.quantity * position.entry_price / Decimal(leverage)
            self._positions[key] = replace(
                position,
                initial_margin=margin,
                leverage=leverage,
            )

    def _apply_market_fill(self, order: MarketOrder, price: Decimal) -> None:
        if self._position_mode is PositionMode.ONE_WAY:
            self._apply_one_way_fill(order, price)
        else:
            self._apply_hedge_fill(order, price)

    def _apply_one_way_fill(self, order: MarketOrder, price: Decimal) -> None:
        if order.position_side is not PositionSide.BOTH:
            raise BrokerError("position_mode_mismatch")
        key = (order.symbol, PositionSide.BOTH)
        position = self._positions.get(key)
        order_direction = (
            PositionDirection.LONG
            if order.side is OrderSide.BUY
            else PositionDirection.SHORT
        )
        if order.reduce_only:
            self._require_reducible(position, order)
            self._reduce_position(key, position, order.quantity, price)
            return
        if position is None:
            self._open_position(
                key,
                order.symbol,
                order_direction,
                PositionSide.BOTH,
                order.quantity,
                price,
            )
            return
        if position.direction is order_direction:
            self._increase_position(key, position, order.quantity, price)
            return

        if order.quantity <= position.quantity:
            self._reduce_position(key, position, order.quantity, price)
            return

        closing_quantity = position.quantity
        opening_quantity = order.quantity - closing_quantity
        realized, released_margin, _ = self._closing_values(position, closing_quantity, price)
        opening_margin = self._required_margin(order.symbol, opening_quantity, price)
        available_after_close = self._available_balance + released_margin + realized
        if opening_margin > available_after_close:
            raise BrokerError("insufficient_margin")
        next_position = self._new_position(
            order.symbol,
            order_direction,
            PositionSide.BOTH,
            opening_quantity,
            price,
            opening_margin,
        )
        self._commit_fill(
            key,
            next_position,
            realized=realized,
            available_delta=released_margin + realized - opening_margin,
        )

    def _apply_hedge_fill(self, order: MarketOrder, price: Decimal) -> None:
        if order.position_side is PositionSide.BOTH:
            raise BrokerError("position_mode_mismatch")
        key = (order.symbol, order.position_side)
        position = self._positions.get(key)
        direction = (
            PositionDirection.LONG
            if order.position_side is PositionSide.LONG
            else PositionDirection.SHORT
        )
        opening_side = OrderSide.BUY if direction is PositionDirection.LONG else OrderSide.SELL
        if order.side is opening_side:
            if position is None:
                self._open_position(
                    key,
                    order.symbol,
                    direction,
                    order.position_side,
                    order.quantity,
                    price,
                )
            else:
                self._increase_position(key, position, order.quantity, price)
            return
        self._require_reducible(position, order)
        self._reduce_position(key, position, order.quantity, price)

    def _require_reducible(self, position: Position | None, order: MarketOrder) -> None:
        if position is None:
            raise BrokerError("position_to_reduce_not_found")
        expected_side = (
            OrderSide.SELL if position.direction is PositionDirection.LONG else OrderSide.BUY
        )
        if order.side is not expected_side:
            raise BrokerError("reduce_only_side_mismatch")
        if order.quantity > position.quantity:
            raise BrokerError("reduce_quantity_exceeds_position")

    def _open_position(
        self,
        key: tuple[str, PositionSide],
        symbol: str,
        direction: PositionDirection,
        position_side: PositionSide,
        quantity: Decimal,
        price: Decimal,
    ) -> None:
        margin = self._required_margin(symbol, quantity, price)
        if margin > self._available_balance:
            raise BrokerError("insufficient_margin")
        position = self._new_position(
            symbol,
            direction,
            position_side,
            quantity,
            price,
            margin,
        )
        self._commit_fill(key, position, available_delta=-margin)

    def _increase_position(
        self,
        key: tuple[str, PositionSide],
        position: Position,
        quantity: Decimal,
        price: Decimal,
    ) -> None:
        if position.entry_price is None:
            raise BrokerError("position_cost_basis_unavailable")
        added_margin = self._required_margin(position.symbol, quantity, price)
        if added_margin > self._available_balance:
            raise BrokerError("insufficient_margin")
        next_quantity = position.quantity + quantity
        next_entry = (
            position.entry_price * position.quantity + price * quantity
        ) / next_quantity
        previous_margin = self._position_margin(position)
        next_position = self._marked_position(
            replace(
                position,
                quantity=next_quantity,
                entry_price=next_entry,
                break_even_price=next_entry,
                initial_margin=previous_margin + added_margin,
                leverage=self._leverage_for(position.symbol),
            ),
            price,
            self._clock(),
        )
        self._commit_fill(key, next_position, available_delta=-added_margin)

    def _reduce_position(
        self,
        key: tuple[str, PositionSide],
        position: Position,
        quantity: Decimal,
        price: Decimal,
    ) -> None:
        realized, released_margin, remaining_quantity = self._closing_values(
            position, quantity, price
        )
        next_position = None
        if remaining_quantity > 0:
            remaining_margin = self._position_margin(position) - released_margin
            next_position = self._marked_position(
                replace(
                    position,
                    quantity=remaining_quantity,
                    initial_margin=remaining_margin,
                ),
                price,
                self._clock(),
            )
        self._commit_fill(
            key,
            next_position,
            realized=realized,
            available_delta=released_margin + realized,
        )

    def _closing_values(
        self,
        position: Position,
        quantity: Decimal,
        price: Decimal,
    ) -> tuple[Decimal, Decimal, Decimal]:
        if position.entry_price is None:
            raise BrokerError("position_cost_basis_unavailable")
        if quantity > position.quantity:
            raise BrokerError("reduce_quantity_exceeds_position")
        pnl_per_unit = (
            price - position.entry_price
            if position.direction is PositionDirection.LONG
            else position.entry_price - price
        )
        realized = pnl_per_unit * quantity
        released_margin = self._position_margin(position) * quantity / position.quantity
        return realized, released_margin, position.quantity - quantity

    def _required_margin(self, symbol: str, quantity: Decimal, price: Decimal) -> Decimal:
        return quantity * price / Decimal(self._leverage_for(symbol))

    def _position_margin(self, position: Position) -> Decimal:
        if position.initial_margin is not None:
            return position.initial_margin
        if position.entry_price is None:
            raise BrokerError("position_margin_unavailable")
        return self._required_margin(position.symbol, position.quantity, position.entry_price)

    def _leverage_for(self, symbol: str) -> int:
        return self._leverage.get(symbol, _DEFAULT_LEVERAGE)

    def _new_position(
        self,
        symbol: str,
        direction: PositionDirection,
        position_side: PositionSide,
        quantity: Decimal,
        price: Decimal,
        margin: Decimal,
    ) -> Position:
        leverage = self._leverage_for(symbol)
        self._leverage.setdefault(symbol, leverage)
        return Position(
            symbol=symbol,
            direction=direction,
            position_side=position_side,
            quantity=quantity,
            entry_price=price,
            mark_price=price,
            break_even_price=price,
            notional=quantity * price,
            initial_margin=margin,
            unrealized_pnl=_ZERO,
            leverage=leverage,
            updated_at_ms=self._timestamp_ms(self._clock()),
        )

    def _commit_fill(
        self,
        key: tuple[str, PositionSide],
        position: Position | None,
        *,
        realized: Decimal = _ZERO,
        available_delta: Decimal = _ZERO,
    ) -> None:
        if position is None:
            self._positions.pop(key, None)
        else:
            self._positions[key] = position
        self._wallet_balance += realized
        self._available_balance += available_delta
        self._realized_pnl += realized
        self._sync_account(self._clock())

    @staticmethod
    def _marked_position(
        position: Position,
        mark_price: Decimal,
        observed_at: datetime,
    ) -> Position:
        if position.entry_price is None:
            unrealized = position.unrealized_pnl
        elif position.direction is PositionDirection.LONG:
            unrealized = (mark_price - position.entry_price) * position.quantity
        else:
            unrealized = (position.entry_price - mark_price) * position.quantity
        return replace(
            position,
            mark_price=mark_price,
            notional=position.quantity * mark_price,
            unrealized_pnl=unrealized,
            updated_at_ms=ShadowBroker._timestamp_ms(observed_at),
        )

    def _sync_account(self, updated_at: datetime) -> None:
        positions = self._position_tuple()
        unrealized = sum(
            (position.unrealized_pnl or _ZERO for position in positions),
            start=_ZERO,
        )
        self._account = replace(
            self._account,
            wallet_balance=self._wallet_balance,
            available_balance=self._available_balance,
            unrealized_pnl=unrealized,
            positions=positions,
            updated_at=updated_at,
            observed_at=updated_at,
        )

    def _position_tuple(self) -> tuple[Position, ...]:
        keys = sorted(
            self._positions,
            key=lambda item: (item[0], item[1].value),
        )
        return tuple(self._positions[key] for key in keys)

    @staticmethod
    def _timestamp_ms(value: datetime) -> int:
        return int(value.timestamp() * 1000)
