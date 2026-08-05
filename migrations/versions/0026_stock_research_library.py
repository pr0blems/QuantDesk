"""Create the US stock research library.

Revision ID: 0026_stock_research_library
Revises: 0025_news_source_feed_type
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0026_stock_research_library"
down_revision: str | None = "0025_news_source_feed_type"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    common = {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"}
    op.create_table(
        "securities",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("symbol", sa.String(32), nullable=False),
        sa.Column("exchange", sa.String(32), nullable=False, server_default="US"),
        sa.Column("security_type", sa.String(32), nullable=False, server_default="UNKNOWN"),
        sa.Column("company_name", sa.String(255)), sa.Column("currency", sa.String(8), nullable=False, server_default="USD"),
        sa.Column("country", sa.String(64)), sa.Column("cik", sa.String(16)), sa.Column("isin", sa.String(32)),
        sa.Column("finnhub_symbol", sa.String(32)), sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("verification_status", sa.String(32), nullable=False, server_default="PENDING"),
        sa.Column("created_at", sa.DateTime(), nullable=False), sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"), sa.UniqueConstraint("exchange", "symbol", name="uq_securities_exchange_symbol"), **common,
    )
    op.create_index("ix_securities_type_active", "securities", ["security_type", "is_active"])
    op.create_table(
        "security_symbol_mappings", sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("security_id", sa.BigInteger(), nullable=False), sa.Column("source", sa.String(32), nullable=False),
        sa.Column("source_symbol", sa.String(64), nullable=False), sa.Column("normalized_symbol", sa.String(32), nullable=False),
        sa.Column("mapping_status", sa.String(32), nullable=False, server_default="AUTO"), sa.Column("mapping_method", sa.String(64)),
        sa.Column("notes", sa.Text()), sa.Column("created_at", sa.DateTime(), nullable=False), sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["security_id"], ["securities.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("source", "source_symbol", name="uq_security_mapping_source_symbol"), **common,
    )
    op.create_table(
        "company_profiles", sa.Column("security_id", sa.BigInteger(), nullable=False), sa.Column("legal_name", sa.String(255)),
        sa.Column("description", sa.Text()), sa.Column("industry", sa.String(128)), sa.Column("sector", sa.String(128)),
        sa.Column("website", sa.Text()), sa.Column("ipo_date", sa.Date()), sa.Column("employee_count", sa.BigInteger()),
        sa.Column("market_cap", sa.Numeric(30, 4)), sa.Column("shares_outstanding", sa.Numeric(30, 4)),
        sa.Column("source", sa.String(32)), sa.Column("source_updated_at", sa.DateTime()), sa.Column("raw_json", sa.JSON()),
        sa.Column("updated_at", sa.DateTime(), nullable=False), sa.PrimaryKeyConstraint("security_id"),
        sa.ForeignKeyConstraint(["security_id"], ["securities.id"], ondelete="CASCADE"), **common,
    )
    op.create_table(
        "security_research_sources", sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("security_id", sa.BigInteger(), nullable=False), sa.Column("source_type", sa.String(32), nullable=False),
        sa.Column("title", sa.String(512), nullable=False), sa.Column("url", sa.Text()), sa.Column("publisher", sa.String(128)),
        sa.Column("published_at", sa.DateTime()), sa.Column("retrieved_at", sa.DateTime(), nullable=False),
        sa.Column("content_summary", sa.Text()), sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("raw_metadata_json", sa.JSON()), sa.Column("status", sa.String(32), nullable=False, server_default="ACTIVE"),
        sa.PrimaryKeyConstraint("id"), sa.ForeignKeyConstraint(["security_id"], ["securities.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("security_id", "content_hash", name="uq_research_security_hash"), **common,
    )
    op.create_table(
        "security_fundamental_analyses", sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("security_id", sa.BigInteger(), nullable=False), sa.Column("analysis_version", sa.String(32), nullable=False, server_default="v1"),
        sa.Column("as_of_date", sa.Date(), nullable=False), sa.Column("business_summary", sa.Text()), sa.Column("growth_analysis", sa.Text()),
        sa.Column("profitability_analysis", sa.Text()), sa.Column("valuation_analysis", sa.Text()), sa.Column("risk_analysis", sa.Text()),
        sa.Column("catalysts_json", sa.JSON()), sa.Column("risk_factors_json", sa.JSON()), sa.Column("quality_score", sa.Numeric(5, 2)),
        sa.Column("growth_score", sa.Numeric(5, 2)), sa.Column("valuation_score", sa.Numeric(5, 2)),
        sa.Column("financial_health_score", sa.Numeric(5, 2)), sa.Column("overall_score", sa.Numeric(5, 2)),
        sa.Column("confidence_score", sa.Numeric(5, 4)), sa.Column("evidence_json", sa.JSON()), sa.Column("generated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"), sa.ForeignKeyConstraint(["security_id"], ["securities.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("security_id", "analysis_version", "as_of_date", name="uq_analysis_security_version_date"), **common,
    )


def downgrade() -> None:
    for table in ("security_fundamental_analyses", "security_research_sources", "company_profiles", "security_symbol_mappings", "securities"):
        op.drop_table(table)
