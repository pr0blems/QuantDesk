"""Synchronize paper execution with the Binance TradFi perpetual environment.

Revision ID: 0021_binance_environment_parity
Revises: 0020_paper_protective_execution
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0021_binance_environment_parity"
down_revision: str | None = "0020_paper_protective_execution"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _require_mysql() -> None:
    if op.get_bind().dialect.name not in {"mysql", "mariadb"}:
        raise RuntimeError("QuantDesk migrations require MySQL or MariaDB")


def upgrade() -> None:
    _require_mysql()
    op.create_table(
        "binance_contract_rules",
        sa.Column("symbol", sa.String(32), primary_key=True),
        sa.Column("contract_type", sa.String(32), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("quote_asset", sa.String(16), nullable=False),
        sa.Column("margin_asset", sa.String(16), nullable=False),
        sa.Column("underlying_type", sa.String(32), nullable=True),
        sa.Column("tick_size", sa.Numeric(30, 12), nullable=False),
        sa.Column("lot_step_size", sa.Numeric(30, 12), nullable=False),
        sa.Column("min_qty", sa.Numeric(30, 12), nullable=False),
        sa.Column("market_step_size", sa.Numeric(30, 12), nullable=False),
        sa.Column("market_min_qty", sa.Numeric(30, 12), nullable=False),
        sa.Column("min_notional", sa.Numeric(30, 12), nullable=False),
        sa.Column("liquidation_fee_rate", sa.Numeric(18, 12), nullable=False),
        sa.Column("market_take_bound", sa.Numeric(18, 12), nullable=False),
        sa.Column("trigger_protect", sa.Numeric(18, 12), nullable=False),
        sa.Column("funding_interval_hours", sa.Integer(), nullable=True),
        sa.Column("funding_rate_cap", sa.Numeric(18, 12), nullable=True),
        sa.Column("funding_rate_floor", sa.Numeric(18, 12), nullable=True),
        sa.Column("mark_price", sa.Numeric(30, 12), nullable=True),
        sa.Column("index_price", sa.Numeric(30, 12), nullable=True),
        sa.Column("last_funding_rate", sa.Numeric(18, 12), nullable=True),
        sa.Column("next_funding_time", sa.BigInteger(), nullable=True),
        sa.Column("rule_updated_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("mark_updated_at_ms", sa.BigInteger(), nullable=True),
        sa.Column("raw_json", sa.JSON(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )
    op.create_index("ix_binance_rules_status", "binance_contract_rules", ["status"])

    op.create_table(
        "binance_funding_events",
        sa.Column("symbol", sa.String(32), nullable=False),
        sa.Column("funding_time", sa.BigInteger(), nullable=False),
        sa.Column("funding_rate", sa.Numeric(18, 12), nullable=False),
        sa.Column("mark_price", sa.Numeric(30, 12), nullable=True),
        sa.Column("received_at_ms", sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint("symbol", "funding_time"),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )

    op.create_table(
        "binance_user_commission_rates",
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("symbol", sa.String(32), nullable=False),
        sa.Column("maker_rate", sa.Numeric(18, 12), nullable=False),
        sa.Column("taker_rate", sa.Numeric(18, 12), nullable=False),
        sa.Column("rpi_rate", sa.Numeric(18, 12), nullable=True),
        sa.Column("credential_version", sa.Integer(), nullable=False),
        sa.Column("synced_at_ms", sa.BigInteger(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("user_id", "symbol"),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )

    op.create_table(
        "binance_user_leverage_brackets",
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("symbol", sa.String(32), nullable=False),
        sa.Column("bracket", sa.Integer(), nullable=False),
        sa.Column("initial_leverage", sa.Integer(), nullable=False),
        sa.Column("notional_floor", sa.Numeric(30, 8), nullable=False),
        sa.Column("notional_cap", sa.Numeric(30, 8), nullable=False),
        sa.Column("maint_margin_ratio", sa.Numeric(18, 12), nullable=False),
        sa.Column("cum", sa.Numeric(30, 8), nullable=False),
        sa.Column("credential_version", sa.Integer(), nullable=False),
        sa.Column("synced_at_ms", sa.BigInteger(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("user_id", "symbol", "bracket"),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )

    op.add_column("market_microstructure", sa.Column("bid_qty", sa.Numeric(30, 12)))
    op.add_column("market_microstructure", sa.Column("ask_qty", sa.Numeric(30, 12)))
    op.add_column(
        "paper_positions",
        sa.Column("execution_model", sa.String(32), server_default="legacy_fixed_v1", nullable=False),
    )
    op.add_column("paper_positions", sa.Column("open_fee", sa.Double(), server_default="0", nullable=False))
    op.add_column("paper_positions", sa.Column("fee_rate_open", sa.Numeric(18, 12)))
    op.add_column("paper_positions", sa.Column("rule_updated_at_ms", sa.BigInteger()))
    op.add_column("paper_trades", sa.Column("open_fee", sa.Double(), server_default="0", nullable=False))
    op.add_column("paper_trades", sa.Column("close_fee", sa.Double(), server_default="0", nullable=False))
    op.add_column("paper_trades", sa.Column("liquidation_fee", sa.Double(), server_default="0", nullable=False))
    op.add_column("paper_trades", sa.Column("execution_model", sa.String(32), server_default="legacy_fixed_v1", nullable=False))


def downgrade() -> None:
    _require_mysql()
    for column in ("execution_model", "liquidation_fee", "close_fee", "open_fee"):
        op.drop_column("paper_trades", column)
    for column in ("rule_updated_at_ms", "fee_rate_open", "open_fee", "execution_model"):
        op.drop_column("paper_positions", column)
    op.drop_column("market_microstructure", "ask_qty")
    op.drop_column("market_microstructure", "bid_qty")
    op.drop_table("binance_user_leverage_brackets")
    op.drop_table("binance_user_commission_rates")
    op.drop_table("binance_funding_events")
    op.drop_index("ix_binance_rules_status", table_name="binance_contract_rules")
    op.drop_table("binance_contract_rules")
