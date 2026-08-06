"""Add append-only 1m/5m archives and source quality telemetry.

Revision ID: 0031_data_quality_archives
Revises: 0030_news_structured_events
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0031_data_quality_archives"
down_revision: str | None = "0030_news_structured_events"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _require_mysql() -> None:
    if op.get_bind().dialect.name not in {"mysql", "mariadb"}:
        raise RuntimeError("QuantDesk migrations require MySQL or MariaDB")


def _common() -> tuple[sa.Column, ...]:
    return (
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("bucket_ts", sa.BigInteger(), nullable=False),
        sa.Column("bucket_size_seconds", sa.Integer(), nullable=False),
        sa.Column("source_age_ms", sa.BigInteger()),
        sa.Column("latency_ms", sa.BigInteger()),
        sa.Column("coverage_ratio", sa.Numeric(8, 6), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )


def upgrade() -> None:
    _require_mysql()
    op.create_table(
        "market_microstructure_archive",
        sa.Column("symbol", sa.String(32), nullable=False),
        sa.Column("event_time", sa.BigInteger(), nullable=False),
        sa.Column("received_at", sa.BigInteger(), nullable=False),
        sa.Column("mid_price", sa.Numeric(30, 12)),
        sa.Column("spread_bps", sa.Numeric(16, 6)),
        sa.Column("book_imbalance", sa.Numeric(16, 8)),
        sa.Column("aggressive_buy_ratio", sa.Numeric(16, 8)),
        sa.Column("trade_count_60s", sa.Integer()),
        sa.Column("quote_volume_60s", sa.Numeric(30, 8)),
        sa.Column("realized_volatility_60s", sa.Numeric(20, 10)),
        sa.Column("price_velocity_bps_60s", sa.Numeric(20, 8)),
        sa.Column("depth_levels", sa.Integer()),
        sa.Column("quality_json", sa.JSON(), nullable=False),
        *_common(),
        sa.UniqueConstraint("symbol", "bucket_ts", "bucket_size_seconds", name="uq_micro_archive_bucket"),
        mysql_engine="InnoDB", mysql_charset="utf8mb4",
    )
    op.create_index("ix_micro_archive_time", "market_microstructure_archive", ["bucket_ts"])
    op.create_table(
        "underlying_market_quotes_archive",
        sa.Column("contract_symbol", sa.String(32), nullable=False),
        sa.Column("quote_symbol", sa.String(32)),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("market_state", sa.String(24), nullable=False),
        sa.Column("market_time_ms", sa.BigInteger()),
        sa.Column("price", sa.Numeric(30, 12)),
        sa.Column("change_pct", sa.Numeric(20, 8)),
        sa.Column("volume", sa.Numeric(30, 4)),
        sa.Column("quality_json", sa.JSON(), nullable=False),
        *_common(),
        sa.UniqueConstraint("contract_symbol", "bucket_ts", "bucket_size_seconds", name="uq_underlying_archive_bucket"),
        mysql_engine="InnoDB", mysql_charset="utf8mb4",
    )
    op.create_index("ix_underlying_archive_time", "underlying_market_quotes_archive", ["bucket_ts"])
    op.create_table(
        "social_archive",
        sa.Column("symbol", sa.String(32), nullable=False),
        sa.Column("source_ts", sa.BigInteger(), nullable=False),
        sa.Column("st_bull", sa.Integer()),
        sa.Column("st_bear", sa.Integer()),
        sa.Column("st_msgs", sa.Integer()),
        sa.Column("ape_mentions", sa.Integer()),
        sa.Column("ape_upvotes", sa.Integer()),
        sa.Column("ape_rank", sa.Integer()),
        sa.Column("ape_rank_24h", sa.Integer()),
        sa.Column("quality_json", sa.JSON(), nullable=False),
        *_common(),
        sa.UniqueConstraint("symbol", "bucket_ts", "bucket_size_seconds", name="uq_social_archive_bucket"),
        mysql_engine="InnoDB", mysql_charset="utf8mb4",
    )
    op.create_index("ix_social_archive_time", "social_archive", ["bucket_ts"])


def downgrade() -> None:
    _require_mysql()
    for index, table in (
        ("ix_social_archive_time", "social_archive"),
        ("ix_underlying_archive_time", "underlying_market_quotes_archive"),
        ("ix_micro_archive_time", "market_microstructure_archive"),
    ):
        op.drop_index(index, table_name=table)
        op.drop_table(table)
