"""Merge news AI and US stock research library migration branches.

Revision ID: 0027_merge_news_ai_stock_library
Revises: 0026_news_ai_analysis, 0026_stock_research_library
"""

from collections.abc import Sequence

revision: str = "0027_merge_news_ai_stock_library"
down_revision: tuple[str, str] = (
    "0026_news_ai_analysis",
    "0026_stock_research_library",
)
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Join the two feature branches without changing the schema."""


def downgrade() -> None:
    """Split back to the two feature branch heads without changing the schema."""
