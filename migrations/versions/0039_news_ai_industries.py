"""Add news industries and ten-item rolling AI batches.

Revision ID: 0039_news_ai_industries
Revises: 0038_ai_monitor_predictions
"""

import re
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0039_news_ai_industries"
down_revision: str | None = "0038_ai_monitor_predictions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _require_mysql() -> None:
    if op.get_bind().dialect.name not in {"mysql", "mariadb"}:
        raise RuntimeError("QuantDesk migrations require MySQL or MariaDB")


def _requested_count_constraint() -> str | None:
    inspector = sa.inspect(op.get_bind())
    for constraint in inspector.get_check_constraints("news_ai_batches"):
        sqltext = str(constraint.get("sqltext") or "").lower()
        if "requested_count" in sqltext:
            return str(constraint["name"])
    return None


def _drop_check_constraint(name: str) -> None:
    if not re.fullmatch(r"[A-Za-z0-9_]+", name):
        raise RuntimeError("unsafe news AI check constraint name")
    op.execute(sa.text(f"ALTER TABLE news_ai_batches DROP CONSTRAINT `{name}`"))


def upgrade() -> None:
    _require_mysql()
    inspector = sa.inspect(op.get_bind())
    news_columns = {column["name"] for column in inspector.get_columns("news")}
    if "related_industries" not in news_columns:
        op.add_column(
            "news",
            sa.Column(
                "related_industries",
                sa.JSON(),
                nullable=True,
                comment="AI 识别的关联行业、相关度与影响方向",
            ),
        )

    old_constraint = _requested_count_constraint()
    if old_constraint:
        _drop_check_constraint(old_constraint)
    op.create_check_constraint(
        "valid_requested_count_v2",
        "news_ai_batches",
        "requested_count IN (10, 300, 500)",
    )
    op.alter_column(
        "ai_monitor_configs",
        "news_interval_minutes",
        existing_type=sa.Integer(),
        existing_nullable=False,
        server_default=sa.text("15"),
        comment="AI 分析最新 10 条新新闻的间隔分钟数",
    )
def downgrade() -> None:
    _require_mysql()
    op.execute(sa.text("DELETE FROM news_ai_batches WHERE requested_count=10"))
    current_constraint = _requested_count_constraint()
    if current_constraint:
        _drop_check_constraint(current_constraint)
    op.create_check_constraint(
        "valid_requested_count",
        "news_ai_batches",
        "requested_count IN (300, 500)",
    )
    op.alter_column(
        "ai_monitor_configs",
        "news_interval_minutes",
        existing_type=sa.Integer(),
        existing_nullable=False,
        server_default=sa.text("30"),
        comment="AI 分析新新闻的间隔分钟数",
    )
    op.drop_column("news", "related_industries")
