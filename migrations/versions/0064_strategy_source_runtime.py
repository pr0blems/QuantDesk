"""Add versioned Python source strategies.

Revision ID: 0064_strategy_source_runtime
Revises: 0063_prediction_fact_status
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0064_strategy_source_runtime"
down_revision: str | None = "0063_prediction_fact_status"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _require_mysql() -> None:
    if op.get_bind().dialect.name not in {"mysql", "mariadb"}:
        raise RuntimeError("QuantDesk migrations require MySQL or MariaDB")


def upgrade() -> None:
    _require_mysql()
    op.drop_constraint("ck_user_strategies_supported_engine", "user_strategies", type_="check")
    op.create_check_constraint(
        "ck_user_strategies_supported_engine",
        "user_strategies",
        "engine_key IN ('multi_factor', 'ma_cross', 'macd_momentum', "
        "'rsi_reversal', 'bollinger_reversion', 'strategy_dsl', 'python_source')",
    )
    op.drop_constraint(
        "ck_user_strategies_valid_strategy_kind", "user_strategies", type_="check"
    )
    op.create_check_constraint(
        "ck_user_strategies_valid_strategy_kind",
        "user_strategies",
        "strategy_kind IN ('full_strategy', 'source_strategy', 'legacy_signal')",
    )
    op.add_column("user_strategies", sa.Column("source_language", sa.String(24)))
    op.add_column("user_strategies", sa.Column("source_code", sa.Text()))
    op.add_column("user_strategies", sa.Column("source_hash", sa.String(64)))
    op.add_column("user_strategies", sa.Column("source_runtime_version", sa.String(32)))
    op.add_column("user_strategies", sa.Column("source_validation_json", sa.JSON()))
    op.create_index("ix_user_strategies_source_hash", "user_strategies", ["source_hash"])

    op.add_column("strategy_revisions", sa.Column("source_language", sa.String(24)))
    op.add_column("strategy_revisions", sa.Column("source_code", sa.Text()))
    op.add_column("strategy_revisions", sa.Column("source_hash", sa.String(64)))
    op.add_column("strategy_revisions", sa.Column("source_runtime_version", sa.String(32)))


def downgrade() -> None:
    _require_mysql()
    for name in (
        "source_runtime_version",
        "source_hash",
        "source_code",
        "source_language",
    ):
        op.drop_column("strategy_revisions", name)
    op.drop_index("ix_user_strategies_source_hash", table_name="user_strategies")
    for name in (
        "source_validation_json",
        "source_runtime_version",
        "source_hash",
        "source_code",
        "source_language",
    ):
        op.drop_column("user_strategies", name)
    op.drop_constraint(
        "ck_user_strategies_valid_strategy_kind", "user_strategies", type_="check"
    )
    op.create_check_constraint(
        "ck_user_strategies_valid_strategy_kind",
        "user_strategies",
        "strategy_kind IN ('full_strategy', 'legacy_signal')",
    )
    op.drop_constraint("ck_user_strategies_supported_engine", "user_strategies", type_="check")
    op.create_check_constraint(
        "ck_user_strategies_supported_engine",
        "user_strategies",
        "engine_key IN ('multi_factor', 'ma_cross', 'macd_momentum', "
        "'rsi_reversal', 'bollinger_reversion', 'strategy_dsl')",
    )
