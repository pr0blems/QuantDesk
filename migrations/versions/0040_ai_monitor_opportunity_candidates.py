"""Persist news candidates before all technical indicators confirm.

Revision ID: 0040_ai_monitor_candidates
Revises: 0039_news_ai_industries
"""

import re
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0040_ai_monitor_candidates"
down_revision: str | None = "0039_news_ai_industries"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _require_mysql() -> None:
    if op.get_bind().dialect.name not in {"mysql", "mariadb"}:
        raise RuntimeError("QuantDesk migrations require MySQL or MariaDB")


def _status_constraint() -> str | None:
    inspector = sa.inspect(op.get_bind())
    for constraint in inspector.get_check_constraints("ai_monitor_opportunities"):
        sqltext = str(constraint.get("sqltext") or "").lower()
        if "status" in sqltext and "discovered" in sqltext:
            return str(constraint["name"])
    return None


def _drop_status_constraint(name: str) -> None:
    if not re.fullmatch(r"[A-Za-z0-9_]+", name):
        raise RuntimeError("unsafe AI monitor opportunity check constraint name")
    op.execute(
        sa.text(
            "ALTER TABLE ai_monitor_opportunities "
            f"DROP CONSTRAINT `{name}`"
        )
    )


def upgrade() -> None:
    _require_mysql()
    current = _status_constraint()
    if current:
        _drop_status_constraint(current)
    op.create_check_constraint(
        "valid_status",
        "ai_monitor_opportunities",
        "status IN ('candidate', 'discovered', 'expired', 'dismissed')",
    )


def downgrade() -> None:
    _require_mysql()
    op.execute(
        sa.text(
            "UPDATE ai_monitor_opportunities "
            "SET status='dismissed' WHERE status='candidate'"
        )
    )
    current = _status_constraint()
    if current:
        _drop_status_constraint(current)
    op.create_check_constraint(
        "valid_status",
        "ai_monitor_opportunities",
        "status IN ('discovered', 'expired', 'dismissed')",
    )
