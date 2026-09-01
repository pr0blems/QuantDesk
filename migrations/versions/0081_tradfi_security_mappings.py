"""Persist Binance TradFi contract lifecycle on the existing security mapping.

Revision ID: 0081_tradfi_security_mappings
Revises: 0080_builtin_strategy_cutover
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0081_tradfi_security_mappings"
down_revision: str | None = "0080_builtin_strategy_cutover"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _require_mysql() -> None:
    if op.get_bind().dialect.name not in {"mysql", "mariadb"}:
        raise RuntimeError("QuantDesk migrations require MySQL or MariaDB")


def upgrade() -> None:
    _require_mysql()
    op.add_column(
        "security_symbol_mappings",
        sa.Column(
            "source_status", sa.String(32), nullable=False, server_default="UNKNOWN"
        ),
    )
    op.add_column(
        "security_symbol_mappings", sa.Column("contract_type", sa.String(32))
    )
    op.add_column(
        "security_symbol_mappings", sa.Column("underlying_type", sa.String(32))
    )
    op.add_column(
        "security_symbol_mappings", sa.Column("onboard_date_ms", sa.BigInteger())
    )
    op.add_column(
        "security_symbol_mappings",
        sa.Column(
            "monitor_enabled", sa.Boolean(), nullable=False, server_default=sa.true()
        ),
    )
    op.add_column(
        "security_symbol_mappings",
        sa.Column(
            "strategy_enabled", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
    )
    op.add_column(
        "security_symbol_mappings",
        sa.Column(
            "live_trading_enabled", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
    )
    op.add_column(
        "security_symbol_mappings", sa.Column("source_metadata_json", sa.JSON())
    )
    op.add_column(
        "security_symbol_mappings",
        sa.Column("first_seen_at", sa.DateTime(), nullable=True),
    )
    op.add_column(
        "security_symbol_mappings", sa.Column("last_seen_at", sa.DateTime())
    )
    op.add_column(
        "security_symbol_mappings",
        sa.Column("updated_at", sa.DateTime(), nullable=True),
    )
    op.execute(
        """
        UPDATE security_symbol_mappings
        SET source_status=IF(source='binance_tradfi', 'TRADING', 'ACTIVE'),
            contract_type=IF(source='binance_tradfi', 'TRADIFI_PERPETUAL', NULL),
            monitor_enabled=TRUE,
            strategy_enabled=IF(source='binance_tradfi', TRUE, FALSE),
            live_trading_enabled=IF(source='binance_tradfi', TRUE, FALSE),
            first_seen_at=created_at,
            last_seen_at=created_at,
            updated_at=created_at
        """
    )
    op.alter_column(
        "security_symbol_mappings",
        "first_seen_at",
        existing_type=sa.DateTime(),
        nullable=False,
    )
    op.alter_column(
        "security_symbol_mappings",
        "updated_at",
        existing_type=sa.DateTime(),
        nullable=False,
    )
    op.create_index(
        "ix_security_mappings_source_status",
        "security_symbol_mappings",
        ["source", "source_status"],
    )


def downgrade() -> None:
    _require_mysql()
    op.drop_index(
        "ix_security_mappings_source_status", table_name="security_symbol_mappings"
    )
    for column in (
        "updated_at",
        "last_seen_at",
        "first_seen_at",
        "source_metadata_json",
        "live_trading_enabled",
        "strategy_enabled",
        "monitor_enabled",
        "onboard_date_ms",
        "underlying_type",
        "contract_type",
        "source_status",
    ):
        op.drop_column("security_symbol_mappings", column)
