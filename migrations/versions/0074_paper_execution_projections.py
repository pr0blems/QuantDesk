"""Add restart-safe paper execution projections.

Revision ID: 0074_paper_projections
Revises: 0073_unified_execution
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision: str = "0074_paper_projections"
down_revision: str | None = "0073_unified_execution"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _require_mysql() -> None:
    if op.get_bind().dialect.name not in {"mysql", "mariadb"}:
        raise RuntimeError("QuantDesk migrations require MySQL or MariaDB")


def upgrade() -> None:
    _require_mysql()
    op.add_column(
        "paper_order_executions",
        sa.Column(
            "projection_status",
            sa.String(16),
            server_default="applied",
            nullable=False,
        ),
    )
    op.add_column(
        "paper_order_executions",
        sa.Column(
            "projection_version",
            sa.String(32),
            server_default="paper_projection_v1",
            nullable=False,
        ),
    )
    op.add_column(
        "paper_order_executions",
        sa.Column("projection_json", sa.JSON(), nullable=True),
    )
    op.add_column(
        "paper_order_executions",
        sa.Column("projection_error", sa.Text(), nullable=True),
    )
    op.add_column(
        "paper_order_executions",
        sa.Column(
            "projection_attempts", sa.Integer(), server_default="0", nullable=False
        ),
    )
    op.add_column(
        "paper_order_executions",
        sa.Column("projected_at", mysql.DATETIME(fsp=6), nullable=True),
    )
    op.execute(
        """
        UPDATE paper_order_executions
        SET projected_at = updated_at
        WHERE projection_status = 'applied' AND projected_at IS NULL
        """
    )
    op.create_check_constraint(
        "ck_paper_order_projection_status",
        "paper_order_executions",
        "projection_status IN ('pending', 'applied', 'failed')",
    )
    op.create_index(
        "ix_paper_order_projection_queue",
        "paper_order_executions",
        ["paper_account_id", "projection_status", "id"],
    )

    op.add_column(
        "paper_positions",
        sa.Column("source_execution_id", sa.BigInteger(), nullable=True),
    )
    op.create_unique_constraint(
        "uq_paper_positions_source_execution",
        "paper_positions",
        ["source_execution_id"],
    )
    op.add_column(
        "paper_trades",
        sa.Column("source_execution_id", sa.BigInteger(), nullable=True),
    )
    op.create_unique_constraint(
        "uq_paper_trades_source_execution",
        "paper_trades",
        ["source_execution_id"],
    )


def downgrade() -> None:
    _require_mysql()
    op.drop_constraint(
        "uq_paper_trades_source_execution", "paper_trades", type_="unique"
    )
    op.drop_column("paper_trades", "source_execution_id")
    op.drop_constraint(
        "uq_paper_positions_source_execution", "paper_positions", type_="unique"
    )
    op.drop_column("paper_positions", "source_execution_id")
    op.drop_index(
        "ix_paper_order_projection_queue", table_name="paper_order_executions"
    )
    op.drop_constraint(
        "ck_paper_order_projection_status",
        "paper_order_executions",
        type_="check",
    )
    op.drop_column("paper_order_executions", "projected_at")
    op.drop_column("paper_order_executions", "projection_attempts")
    op.drop_column("paper_order_executions", "projection_error")
    op.drop_column("paper_order_executions", "projection_json")
    op.drop_column("paper_order_executions", "projection_version")
    op.drop_column("paper_order_executions", "projection_status")
