"""Add complete security financial and valuation snapshots.

Revision ID: 0042_security_financials
Revises: 0041_ai_monitor_pipeline
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0042_security_financials"
down_revision: str | None = "0041_ai_monitor_pipeline"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _require_mysql() -> None:
    if op.get_bind().dialect.name not in {"mysql", "mariadb"}:
        raise RuntimeError("QuantDesk migrations require MySQL or MariaDB")


def upgrade() -> None:
    _require_mysql()
    op.create_table(
        "security_financial_snapshots",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("security_id", sa.BigInteger(), nullable=False),
        sa.Column("snapshot_date", sa.Date(), nullable=False),
        sa.Column("fiscal_period_end", sa.Date(), nullable=True),
        sa.Column("period_type", sa.String(length=16), nullable=False),
        sa.Column("currency", sa.String(length=8), nullable=False),
        sa.Column("data_status", sa.String(length=32), nullable=False),
        sa.Column("coverage_pct", sa.Numeric(7, 4), nullable=True),
        sa.Column("revenue_ttm", sa.Numeric(30, 4), nullable=True),
        sa.Column("revenue_growth_yoy_pct", sa.Numeric(12, 6), nullable=True),
        sa.Column("gross_profit_ttm", sa.Numeric(30, 4), nullable=True),
        sa.Column("gross_margin_pct", sa.Numeric(12, 6), nullable=True),
        sa.Column("operating_income_ttm", sa.Numeric(30, 4), nullable=True),
        sa.Column("operating_margin_pct", sa.Numeric(12, 6), nullable=True),
        sa.Column("net_income_ttm", sa.Numeric(30, 4), nullable=True),
        sa.Column("net_margin_pct", sa.Numeric(12, 6), nullable=True),
        sa.Column("ebitda_ttm", sa.Numeric(30, 4), nullable=True),
        sa.Column("operating_cash_flow_ttm", sa.Numeric(30, 4), nullable=True),
        sa.Column("capital_expenditure_ttm", sa.Numeric(30, 4), nullable=True),
        sa.Column("free_cash_flow_ttm", sa.Numeric(30, 4), nullable=True),
        sa.Column("cash_and_equivalents", sa.Numeric(30, 4), nullable=True),
        sa.Column("total_debt", sa.Numeric(30, 4), nullable=True),
        sa.Column("total_assets", sa.Numeric(30, 4), nullable=True),
        sa.Column("total_liabilities", sa.Numeric(30, 4), nullable=True),
        sa.Column("stockholders_equity", sa.Numeric(30, 4), nullable=True),
        sa.Column("current_ratio", sa.Numeric(20, 6), nullable=True),
        sa.Column("debt_to_equity", sa.Numeric(20, 6), nullable=True),
        sa.Column("return_on_equity_pct", sa.Numeric(12, 6), nullable=True),
        sa.Column("market_cap", sa.Numeric(30, 4), nullable=True),
        sa.Column("enterprise_value", sa.Numeric(30, 4), nullable=True),
        sa.Column("pe_ratio", sa.Numeric(20, 6), nullable=True),
        sa.Column("price_to_sales_ratio", sa.Numeric(20, 6), nullable=True),
        sa.Column("price_to_book_ratio", sa.Numeric(20, 6), nullable=True),
        sa.Column("ev_to_ebitda", sa.Numeric(20, 6), nullable=True),
        sa.Column("source", sa.String(length=64), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=True),
        sa.Column("filing_form", sa.String(length=32), nullable=True),
        sa.Column("filing_accession", sa.String(length=32), nullable=True),
        sa.Column("applicable_metrics_json", sa.JSON(), nullable=True),
        sa.Column("raw_json", sa.JSON(), nullable=True),
        sa.Column("retrieved_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["security_id"],
            ["securities.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "security_id",
            "snapshot_date",
            name="uq_security_financial_snapshot_date",
        ),
        comment="美股基本面财务、现金流、负债与估值快照",
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )
def downgrade() -> None:
    _require_mysql()
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "security_financial_snapshots" in tables:
        op.drop_table("security_financial_snapshots")
