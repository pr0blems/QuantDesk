"""Persist immutable entry evidence for paper and live orders.

Revision ID: 0020_entry_basis
Revises: 0019_hedge_mode_live
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0020_entry_basis"
down_revision: str | None = "0019_hedge_mode_live"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _require_mysql() -> None:
    if op.get_bind().dialect.name not in {"mysql", "mariadb"}:
        raise RuntimeError("QuantDesk migrations require MySQL or MariaDB")


def upgrade() -> None:
    _require_mysql()
    op.add_column(
        "paper_trades",
        sa.Column(
            "entry_basis_json",
            sa.JSON(),
            nullable=True,
            comment="Immutable strategy, signal, market and risk evidence captured at entry",
        ),
    )
    op.add_column(
        "live_order_intents",
        sa.Column(
            "strategy_signal_id",
            sa.BigInteger(),
            nullable=True,
            comment="Strategy signal that caused this order intent",
        ),
    )
    op.add_column(
        "live_order_intents",
        sa.Column(
            "entry_basis_json",
            sa.JSON(),
            nullable=True,
            comment="Immutable entry evidence copied to open and close intents",
        ),
    )
    op.create_foreign_key(
        "fk_live_order_intents_strategy_signal",
        "live_order_intents",
        "strategy_signals",
        ["strategy_signal_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_live_order_intents_strategy_signal",
        "live_order_intents",
        ["strategy_signal_id"],
    )


def downgrade() -> None:
    _require_mysql()
    op.drop_index(
        "ix_live_order_intents_strategy_signal", table_name="live_order_intents"
    )
    op.drop_constraint(
        "fk_live_order_intents_strategy_signal",
        "live_order_intents",
        type_="foreignkey",
    )
    op.drop_column("live_order_intents", "entry_basis_json")
    op.drop_column("live_order_intents", "strategy_signal_id")
    op.drop_column("paper_trades", "entry_basis_json")
