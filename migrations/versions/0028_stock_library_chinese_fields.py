"""Add Chinese display fields to the stock library.

Revision ID: 0028_stock_library_zh
Revises: 0027_merge_news_ai_stock_library
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0028_stock_library_zh"
down_revision: str | None = "0027_merge_news_ai_stock_library"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("securities", sa.Column("company_name_zh", sa.String(255)))
    op.add_column("company_profiles", sa.Column("industry_zh", sa.String(128)))
    op.add_column("company_profiles", sa.Column("sector_zh", sa.String(128)))


def downgrade() -> None:
    op.drop_column("company_profiles", "sector_zh")
    op.drop_column("company_profiles", "industry_zh")
    op.drop_column("securities", "company_name_zh")
