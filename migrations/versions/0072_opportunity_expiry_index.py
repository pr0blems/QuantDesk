"""Add the opportunity expiry query-path index.

Revision ID: 0072_opportunity_expiry_index
Revises: 0071_runtime_incidents
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0072_opportunity_expiry_index"
down_revision: str | None = "0071_runtime_incidents"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _require_mysql() -> None:
    if op.get_bind().dialect.name not in {"mysql", "mariadb"}:
        raise RuntimeError("QuantDesk migrations require MySQL or MariaDB")


def upgrade() -> None:
    _require_mysql()
    op.create_index(
        "ix_ai_monitor_opportunities_user_status_expires",
        "ai_monitor_opportunities",
        ["user_id", "status", "expires_at"],
    )


def downgrade() -> None:
    _require_mysql()
    op.drop_index(
        "ix_ai_monitor_opportunities_user_status_expires",
        table_name="ai_monitor_opportunities",
    )
