"""Add revision-bound strategy promotion reviews.

Revision ID: 0068_strategy_promotion_reviews
Revises: 0067_hierarchical_trading_controls
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision: str = "0068_strategy_promotion_reviews"
down_revision: str | None = "0067_hierarchical_trading_controls"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE_OPTIONS = {
    "mysql_engine": "InnoDB",
    "mysql_charset": "utf8mb4",
    "mysql_collate": "utf8mb4_bin",
}


def _require_mysql() -> None:
    if op.get_bind().dialect.name not in {"mysql", "mariadb"}:
        raise RuntimeError("QuantDesk migrations require MySQL or MariaDB")


def upgrade() -> None:
    _require_mysql()
    op.create_table(
        "strategy_promotion_reviews",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("public_id", sa.String(36), nullable=False),
        sa.Column("user_strategy_id", sa.BigInteger(), nullable=False),
        sa.Column("strategy_revision_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("strategy_version", sa.Integer(), nullable=False),
        sa.Column("from_stage", sa.String(16), nullable=False),
        sa.Column("to_stage", sa.String(16), nullable=False),
        sa.Column("gate_result_json", sa.JSON(), nullable=False),
        sa.Column("requested_by_user_id", sa.BigInteger(), nullable=False),
        sa.Column("approved_by_user_id", sa.BigInteger(), nullable=True),
        sa.Column("status", sa.String(16), server_default="pending", nullable=False),
        sa.Column("request_note", sa.String(500), nullable=False),
        sa.Column("decision_note", sa.String(500), nullable=True),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("decided_at", mysql.DATETIME(fsp=6), nullable=True),
        sa.Column("applied_at", mysql.DATETIME(fsp=6), nullable=True),
        sa.Column(
            "created_at",
            mysql.DATETIME(fsp=6),
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            mysql.DATETIME(fsp=6),
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'approved', 'rejected', 'cancelled', 'applied')",
            name="ck_strategy_promotion_reviews_valid_status",
        ),
        sa.CheckConstraint(
            "version > 0",
            name="ck_strategy_promotion_reviews_positive_version",
        ),
        sa.ForeignKeyConstraint(
            ["user_strategy_id", "user_id"],
            ["user_strategies.id", "user_strategies.user_id"],
            name="fk_strategy_promotion_reviews_strategy_tenant",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["strategy_revision_id", "user_id"],
            ["strategy_revisions.id", "strategy_revisions.user_id"],
            name="fk_strategy_promotion_reviews_revision_tenant",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["requested_by_user_id"],
            ["users.id"],
            name="fk_strategy_promotion_reviews_requested_by_users",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["approved_by_user_id"],
            ["users.id"],
            name="fk_strategy_promotion_reviews_approved_by_users",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_strategy_promotion_reviews"),
        sa.UniqueConstraint("public_id", name="uq_strategy_promotion_reviews_public_id"),
        comment="Revision-bound promotion request, approval and gate evidence",
        **TABLE_OPTIONS,
    )
    op.create_index(
        "ix_strategy_promotion_reviews_strategy_status",
        "strategy_promotion_reviews",
        ["user_strategy_id", "status", "created_at"],
    )
    op.create_index(
        "ix_strategy_promotion_reviews_revision_stage",
        "strategy_promotion_reviews",
        ["strategy_revision_id", "to_stage"],
    )


def downgrade() -> None:
    _require_mysql()
    op.drop_index(
        "ix_strategy_promotion_reviews_revision_stage",
        table_name="strategy_promotion_reviews",
    )
    op.drop_index(
        "ix_strategy_promotion_reviews_strategy_status",
        table_name="strategy_promotion_reviews",
    )
    op.drop_table("strategy_promotion_reviews")
