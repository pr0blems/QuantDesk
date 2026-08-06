"""Add real-time intelligence, opportunity lifecycle, and outcome feedback.

Revision ID: 0019_opportunity_intelligence
Revises: 0018_execution_foundation
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0019_opportunity_intelligence"
down_revision: str | None = "0018_execution_foundation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _require_mysql() -> None:
    if op.get_bind().dialect.name not in {"mysql", "mariadb"}:
        raise RuntimeError("QuantDesk migrations require MySQL or MariaDB")


def upgrade() -> None:
    _require_mysql()

    # Preserve history while making the currently actionable row explicit.
    op.add_column(
        "market_opportunities",
        sa.Column("current_marker", sa.SmallInteger(), nullable=True),
    )
    op.add_column(
        "market_opportunities",
        sa.Column("entry_price", sa.Numeric(30, 12), nullable=True),
    )
    op.add_column(
        "market_opportunities",
        sa.Column("expected_value_score", sa.Numeric(10, 4), nullable=True),
    )
    op.execute(
        "UPDATE market_opportunities SET status='expired', current_marker=NULL "
        "WHERE status IN ('detected','watching','confirmed')"
    )
    op.create_unique_constraint(
        "uq_market_opportunities_current_scanner_symbol_direction",
        "market_opportunities",
        ["scanner_key", "symbol", "direction", "current_marker"],
    )
    op.create_index(
        "ix_market_opportunities_current_rank",
        "market_opportunities",
        ["current_marker", "expected_value_score", "quality_score"],
    )

    op.create_table(
        "market_microstructure",
        sa.Column("symbol", sa.String(32), primary_key=True),
        sa.Column("event_time", sa.BigInteger(), nullable=False),
        sa.Column("received_at", sa.BigInteger(), nullable=False),
        sa.Column("bid_price", sa.Numeric(30, 12)),
        sa.Column("ask_price", sa.Numeric(30, 12)),
        sa.Column("mid_price", sa.Numeric(30, 12)),
        sa.Column("spread_bps", sa.Numeric(16, 6)),
        sa.Column("book_imbalance", sa.Numeric(16, 8)),
        sa.Column("aggressive_buy_ratio", sa.Numeric(16, 8)),
        sa.Column("trade_count_60s", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("quote_volume_60s", sa.Numeric(30, 8), nullable=False, server_default="0"),
        sa.Column("realized_volatility_60s", sa.Numeric(20, 10)),
        sa.Column("price_velocity_bps_60s", sa.Numeric(20, 8)),
        sa.Column("quality_json", sa.JSON(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        comment="Binance WebSocket 聚合后的标的级实时微观结构快照",
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )
    op.create_index(
        "ix_market_microstructure_received", "market_microstructure", ["received_at"]
    )

    op.create_table(
        "market_data_quality_events",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("stream_key", sa.String(64), nullable=False),
        sa.Column("symbol", sa.String(32)),
        sa.Column("event_type", sa.String(32), nullable=False),
        sa.Column("severity", sa.String(16), nullable=False),
        sa.Column("event_time", sa.BigInteger(), nullable=False),
        sa.Column("details_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "severity IN ('info','warning','critical')",
            name="ck_market_data_quality_events_valid_severity",
        ),
        comment="断线、过期、丢包和异常行情等数据质量事件",
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )
    op.create_index(
        "ix_market_data_quality_events_time",
        "market_data_quality_events",
        ["event_time", "severity"],
    )

    op.create_table(
        "opportunity_events",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("opportunity_id", sa.BigInteger(), nullable=False),
        sa.Column("event_key", sa.String(191), nullable=False),
        sa.Column("event_type", sa.String(32), nullable=False),
        sa.Column("previous_status", sa.String(16)),
        sa.Column("next_status", sa.String(16), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("event_time", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["opportunity_id"],
            ["market_opportunities.id"],
            name="fk_opportunity_events_opportunity",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("event_key", name="uq_opportunity_events_event_key"),
        comment="机会状态变化的只追加审计事件",
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )
    op.create_index(
        "ix_opportunity_events_opportunity_time",
        "opportunity_events",
        ["opportunity_id", "event_time"],
    )

    op.create_table(
        "opportunity_outcomes",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("opportunity_id", sa.BigInteger(), nullable=False),
        sa.Column("horizon_seconds", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("direction", sa.String(12), nullable=False),
        sa.Column("entry_price", sa.Numeric(30, 12), nullable=False),
        sa.Column("exit_price", sa.Numeric(30, 12)),
        sa.Column("raw_return_bps", sa.Numeric(20, 8)),
        sa.Column("directional_return_bps", sa.Numeric(20, 8)),
        sa.Column("max_favorable_bps", sa.Numeric(20, 8)),
        sa.Column("max_adverse_bps", sa.Numeric(20, 8)),
        sa.Column("target_bps", sa.Numeric(20, 8), nullable=False),
        sa.Column("stop_bps", sa.Numeric(20, 8), nullable=False),
        sa.Column("hit_result", sa.String(16)),
        sa.Column("due_at", sa.BigInteger(), nullable=False),
        sa.Column("completed_at", sa.BigInteger()),
        sa.Column("cost_bps", sa.Numeric(20, 8), nullable=False, server_default="0"),
        sa.Column("context_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "status IN ('pending','completed','unavailable')",
            name="ck_opportunity_outcomes_valid_status",
        ),
        sa.CheckConstraint(
            "direction IN ('long','short','neutral')",
            name="ck_opportunity_outcomes_valid_direction",
        ),
        sa.ForeignKeyConstraint(
            ["opportunity_id"],
            ["market_opportunities.id"],
            name="fk_opportunity_outcomes_opportunity",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "opportunity_id", "horizon_seconds", name="uq_opportunity_outcomes_horizon"
        ),
        comment="所有候选机会在多个未来周期上的收益、MFE/MAE与命中结果",
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )
    op.create_index(
        "ix_opportunity_outcomes_status_due",
        "opportunity_outcomes",
        ["status", "due_at"],
    )


def downgrade() -> None:
    _require_mysql()
    op.drop_table("opportunity_outcomes")
    op.drop_table("opportunity_events")
    op.drop_table("market_data_quality_events")
    op.drop_table("market_microstructure")
    op.drop_index("ix_market_opportunities_current_rank", table_name="market_opportunities")
    op.drop_constraint(
        "uq_market_opportunities_current_scanner_symbol_direction",
        "market_opportunities",
        type_="unique",
    )
    op.drop_column("market_opportunities", "expected_value_score")
    op.drop_column("market_opportunities", "entry_price")
    op.drop_column("market_opportunities", "current_marker")
