"""Add a database-enforced SHA-256 deduplication key for news links.

Revision ID: 0013_news_dedup_hash
Revises: 0012_bind_paper_strategy
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0013_news_dedup_hash"
down_revision: str | None = "0012_bind_paper_strategy"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NEWS_DEDUP_EXPRESSION = (
    "CASE WHEN link IS NULL THEN NULL "
    "ELSE SHA2(CONCAT(COALESCE(source, ''), CHAR(0), link), 256) END"
)
NEWS_DEDUP_INDEX = "uq_news_source_link_hash"


def _require_mysql() -> None:
    if op.get_bind().dialect.name not in {"mysql", "mariadb"}:
        raise RuntimeError("QuantDesk migrations require MySQL or MariaDB")


def upgrade() -> None:
    _require_mysql()
    duplicate = (
        op.get_bind()
        .execute(
            sa.text(
                """
            SELECT COALESCE(source, '') AS normalized_source, link, COUNT(*) AS row_count
            FROM news
            WHERE link IS NOT NULL
            GROUP BY COALESCE(source, ''), link
            HAVING COUNT(*) > 1
            LIMIT 1
            """
            )
        )
        .mappings()
        .first()
    )
    if duplicate is not None:
        raise RuntimeError(
            "news contains duplicate source/link rows; deduplicate them before migration 0013"
        )

    op.add_column(
        "news",
        sa.Column(
            "source_link_hash",
            sa.String(64),
            sa.Computed(NEWS_DEDUP_EXPRESSION, persisted=True),
            nullable=True,
            comment="来源名称与原文链接生成的 SHA-256 去重键",
        ),
    )
    op.create_index(NEWS_DEDUP_INDEX, "news", ["source_link_hash"], unique=True)


def downgrade() -> None:
    _require_mysql()
    op.drop_index(NEWS_DEDUP_INDEX, table_name="news")
    op.drop_column("news", "source_link_hash")
