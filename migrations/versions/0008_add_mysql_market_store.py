"""Add MySQL-backed market, monitor, and paper-trading storage.

Revision ID: 0008_mysql_market_store
Revises: 0007_paper_strategy_template
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008_mysql_market_store"
down_revision: str | None = "0007_paper_strategy_template"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE_OPTIONS = {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"}


def upgrade() -> None:
    op.create_table(
        "klines",
        sa.Column("symbol", sa.String(32), nullable=False),
        sa.Column("tf", sa.String(8), nullable=False),
        sa.Column("open_time", sa.BigInteger(), nullable=False),
        sa.Column("open", sa.Double(), nullable=False),
        sa.Column("high", sa.Double(), nullable=False),
        sa.Column("low", sa.Double(), nullable=False),
        sa.Column("close", sa.Double(), nullable=False),
        sa.Column("volume", sa.Double(), nullable=False),
        sa.PrimaryKeyConstraint("symbol", "tf", "open_time", name="pk_klines"),
        **TABLE_OPTIONS,
    )
    op.create_index("ix_klines_time", "klines", ["open_time"], unique=False)

    op.create_table(
        "ticker",
        sa.Column("symbol", sa.String(32), nullable=False),
        sa.Column("price", sa.Double(), nullable=True),
        sa.Column("pct_24h", sa.Double(), nullable=True),
        sa.Column("quote_volume", sa.Double(), nullable=True),
        sa.Column("ts", sa.BigInteger(), autoincrement=False, nullable=False),
        sa.PrimaryKeyConstraint("symbol", name="pk_ticker"),
        **TABLE_OPTIONS,
    )

    op.create_table(
        "positions",
        sa.Column("symbol", sa.String(32), nullable=False),
        sa.Column("amt", sa.Double(), nullable=True),
        sa.Column("side", sa.String(16), nullable=True),
        sa.Column("entry_price", sa.Double(), nullable=True),
        sa.Column("mark_price", sa.Double(), nullable=True),
        sa.Column("upnl", sa.Double(), nullable=True),
        sa.Column("leverage", sa.Integer(), nullable=True),
        sa.Column("ts", sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint("symbol", name="pk_positions"),
        **TABLE_OPTIONS,
    )

    op.create_table(
        "scores",
        sa.Column("symbol", sa.String(32), nullable=False),
        sa.Column("tf", sa.String(8), nullable=False),
        sa.Column("open_time", sa.BigInteger(), nullable=False),
        sa.Column("score", sa.Double(), nullable=False),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("symbol", "tf", "open_time", name="pk_scores"),
        **TABLE_OPTIONS,
    )

    op.create_table(
        "alerts",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("ts", sa.BigInteger(), nullable=False),
        sa.Column("symbol", sa.String(32), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("direction", sa.String(16), nullable=False),
        sa.Column("score", sa.Double(), nullable=True),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column("read", sa.Boolean(), server_default=sa.text("0"), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_alerts"),
        **TABLE_OPTIONS,
    )
    op.create_index("ix_alerts_ts", "alerts", ["ts"], unique=False)

    op.create_table(
        "news",
        sa.Column("id", sa.String(255), nullable=False),
        sa.Column("ts", sa.BigInteger(), nullable=False),
        sa.Column("source", sa.String(80), nullable=True),
        sa.Column("lang", sa.String(16), nullable=True),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("title_zh", sa.Text(), nullable=True),
        sa.Column("link", sa.Text(), nullable=True),
        sa.Column("sentiment", sa.String(32), nullable=True),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_news"),
        **TABLE_OPTIONS,
    )
    op.create_index("ix_news_ts", "news", ["ts"], unique=False)

    op.create_table(
        "social",
        sa.Column("symbol", sa.String(32), nullable=False),
        sa.Column("st_bull", sa.Integer(), nullable=True),
        sa.Column("st_bear", sa.Integer(), nullable=True),
        sa.Column("st_msgs", sa.Integer(), nullable=True),
        sa.Column("ape_mentions", sa.Integer(), nullable=True),
        sa.Column("ape_upvotes", sa.Integer(), nullable=True),
        sa.Column("ape_rank", sa.Integer(), nullable=True),
        sa.Column("ape_rank_24h", sa.Integer(), nullable=True),
        sa.Column("ts", sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint("symbol", name="pk_social"),
        **TABLE_OPTIONS,
    )

    op.create_table(
        "kv",
        sa.Column("k", sa.String(255), nullable=False),
        sa.Column("v", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("k", name="pk_kv"),
        **TABLE_OPTIONS,
    )

    op.create_table(
        "paper_positions",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("symbol", sa.String(32), nullable=False),
        sa.Column("side", sa.Integer(), nullable=False),
        sa.Column("qty", sa.Double(), nullable=False),
        sa.Column("avg_entry", sa.Double(), nullable=False),
        sa.Column("margin", sa.Double(), nullable=False),
        sa.Column("leverage", sa.Integer(), nullable=False),
        sa.Column("stop", sa.Double(), nullable=True),
        sa.Column("target", sa.Double(), nullable=True),
        sa.Column("adds", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("opened_ts", sa.BigInteger(), nullable=False),
        sa.Column("last_add_ts", sa.BigInteger(), nullable=True),
        sa.Column("open_score", sa.Integer(), nullable=True),
        sa.Column("basis", sa.Text(), nullable=True),
        sa.Column("funding_acc", sa.Double(), server_default=sa.text("0"), nullable=False),
        sa.Column("liq_price", sa.Double(), nullable=True),
        sa.Column("funding_ts", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("atr_entry", sa.Double(), nullable=True),
        sa.Column("peak_price", sa.Double(), nullable=True),
        sa.Column("tp_done", sa.Boolean(), server_default=sa.text("0"), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_paper_positions"),
        **TABLE_OPTIONS,
    )

    op.create_table(
        "paper_trades",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("symbol", sa.String(32), nullable=False),
        sa.Column("side", sa.Integer(), nullable=False),
        sa.Column("qty", sa.Double(), nullable=True),
        sa.Column("entry_price", sa.Double(), nullable=True),
        sa.Column("exit_price", sa.Double(), nullable=True),
        sa.Column("margin", sa.Double(), nullable=True),
        sa.Column("pnl", sa.Double(), nullable=True),
        sa.Column("fee", sa.Double(), nullable=True),
        sa.Column("funding", sa.Double(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("open_score", sa.Integer(), nullable=True),
        sa.Column("opened_ts", sa.BigInteger(), nullable=True),
        sa.Column("closed_ts", sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_paper_trades"),
        **TABLE_OPTIONS,
    )
    op.create_index(
        "ix_paper_trades_closed_ts", "paper_trades", ["closed_ts"], unique=False
    )

    op.create_table(
        "paper_equity",
        sa.Column("ts", sa.BigInteger(), autoincrement=False, nullable=False),
        sa.Column("equity", sa.Double(), nullable=False),
        sa.Column("balance", sa.Double(), nullable=False),
        sa.PrimaryKeyConstraint("ts", name="pk_paper_equity"),
        **TABLE_OPTIONS,
    )


def downgrade() -> None:
    op.drop_table("paper_equity")
    op.drop_index("ix_paper_trades_closed_ts", table_name="paper_trades")
    op.drop_table("paper_trades")
    op.drop_table("paper_positions")
    op.drop_table("kv")
    op.drop_table("social")
    op.drop_index("ix_news_ts", table_name="news")
    op.drop_table("news")
    op.drop_index("ix_alerts_ts", table_name="alerts")
    op.drop_table("alerts")
    op.drop_table("scores")
    op.drop_table("positions")
    op.drop_table("ticker")
    op.drop_index("ix_klines_time", table_name="klines")
    op.drop_table("klines")
