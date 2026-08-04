"""Add explicit Binance position sides for Hedge Mode live trading.

Revision ID: 0019_hedge_mode_live
Revises: 0018_live_trading
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0019_hedge_mode_live"
down_revision: str | None = "0018_live_trading"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _require_mysql() -> None:
    if op.get_bind().dialect.name not in {"mysql", "mariadb"}:
        raise RuntimeError("QuantDesk migrations require MySQL or MariaDB")


def upgrade() -> None:
    _require_mysql()
    op.add_column(
        "positions",
        sa.Column(
            "position_side",
            sa.String(8),
            nullable=False,
            server_default="BOTH",
            comment="Binance 持仓方向：BOTH、LONG 或 SHORT",
        ),
    )
    op.execute(
        sa.text(
            "ALTER TABLE positions DROP PRIMARY KEY, "
            "ADD CONSTRAINT pk_positions PRIMARY KEY (user_id,symbol,position_side)"
        )
    )
    op.add_column(
        "live_order_intents",
        sa.Column(
            "position_side",
            sa.String(8),
            nullable=False,
            server_default="BOTH",
            comment="订单绑定的 Binance 持仓方向",
        ),
    )
    op.create_check_constraint(
        "ck_live_order_intents_valid_position_side",
        "live_order_intents",
        "position_side IN ('BOTH', 'LONG', 'SHORT')",
    )
    op.drop_index("ix_live_order_intents_account_symbol", table_name="live_order_intents")
    op.create_index(
        "ix_live_order_intents_account_symbol",
        "live_order_intents",
        ["live_account_id", "symbol", "position_side", "status"],
    )


def downgrade() -> None:
    _require_mysql()
    op.drop_index("ix_live_order_intents_account_symbol", table_name="live_order_intents")
    op.create_index(
        "ix_live_order_intents_account_symbol",
        "live_order_intents",
        ["live_account_id", "symbol", "status"],
    )
    op.drop_constraint(
        "ck_live_order_intents_valid_position_side",
        "live_order_intents",
        type_="check",
    )
    op.drop_column("live_order_intents", "position_side")
    op.execute(
        sa.text(
            "DELETE p1 FROM positions p1 JOIN positions p2 "
            "ON p1.user_id=p2.user_id AND p1.symbol=p2.symbol "
            "AND p1.position_side > p2.position_side"
        )
    )
    op.execute(
        sa.text(
            "ALTER TABLE positions DROP PRIMARY KEY, "
            "ADD CONSTRAINT pk_positions PRIMARY KEY (user_id,symbol)"
        )
    )
    op.drop_column("positions", "position_side")
