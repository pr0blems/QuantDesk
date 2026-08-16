"""Persist structured AI news judgment evidence.

Revision ID: 0055_news_ai_judgment_basis
Revises: 0054_ai_prediction_max_holding
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0055_news_ai_judgment_basis"
down_revision: str | None = "0054_ai_prediction_max_holding"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _require_mysql() -> None:
    if op.get_bind().dialect.name not in {"mysql", "mariadb"}:
        raise RuntimeError("QuantDesk migrations require MySQL or MariaDB")


def upgrade() -> None:
    _require_mysql()
    op.add_column(
        "news_ai_analysis_records",
        sa.Column(
            "judgment_basis_json",
            sa.JSON(),
            nullable=True,
            comment="Structured facts, impact path, counter evidence, and uncertainty",
        ),
    )
    op.add_column(
        "news_ai_analysis_records",
        sa.Column(
            "position_effect",
            sa.String(length=16),
            nullable=True,
            comment="Effect on an open research position",
        ),
    )
    op.add_column(
        "news_ai_analysis_records",
        sa.Column(
            "position_reason",
            sa.Text(),
            nullable=True,
            comment="Reason for the research-position effect",
        ),
    )
    op.create_check_constraint(
        "ck_news_ai_analysis_records_valid_position_effect",
        "news_ai_analysis_records",
        "position_effect IS NULL OR position_effect IN "
        "('hold', 'strengthen', 'caution', 'exit', 'reverse')",
    )


def downgrade() -> None:
    _require_mysql()
    op.drop_constraint(
        "ck_news_ai_analysis_records_valid_position_effect",
        "news_ai_analysis_records",
        type_="check",
    )
    op.drop_column("news_ai_analysis_records", "position_reason")
    op.drop_column("news_ai_analysis_records", "position_effect")
    op.drop_column("news_ai_analysis_records", "judgment_basis_json")
