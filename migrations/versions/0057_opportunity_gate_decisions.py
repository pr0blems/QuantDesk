"""Add immutable opportunity gate-decision audit records.

Revision ID: 0057_opportunity_gate_decisions
Revises: 0056_ai_market_signal_upgrade
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0057_opportunity_gate_decisions"
down_revision: str | None = "0056_ai_market_signal_upgrade"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _require_mysql() -> None:
    if op.get_bind().dialect.name not in {"mysql", "mariadb"}:
        raise RuntimeError("QuantDesk migrations require MySQL or MariaDB")


def upgrade() -> None:
    _require_mysql()
    op.create_table(
        "opportunity_gate_decisions",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("public_id", sa.String(length=36), nullable=False),
        sa.Column("opportunity_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("analysis_run_id", sa.BigInteger(), nullable=False),
        sa.Column("market_feature_snapshot_id", sa.BigInteger(), nullable=True),
        sa.Column("symbol", sa.String(length=32), nullable=False),
        sa.Column("contract_symbol", sa.String(length=32), nullable=False),
        sa.Column("direction", sa.String(length=12), nullable=False),
        sa.Column("gate_status", sa.String(length=16), nullable=False),
        sa.Column("selected", sa.Boolean(), nullable=False),
        sa.Column("decision_at", sa.DateTime(), nullable=False),
        sa.Column("feature_captured_at", sa.DateTime(), nullable=True),
        sa.Column("blocking_reasons_json", sa.JSON(), nullable=False),
        sa.Column("warnings_json", sa.JSON(), nullable=False),
        sa.Column("risk_gate_snapshot_json", sa.JSON(), nullable=False),
        sa.Column("quote_snapshot_json", sa.JSON(), nullable=False),
        sa.Column("market_flow_snapshot_json", sa.JSON(), nullable=False),
        sa.Column("score_components_json", sa.JSON(), nullable=False),
        sa.Column("data_quality_json", sa.JSON(), nullable=False),
        sa.Column("feature_version", sa.String(length=32), nullable=False),
        sa.Column("weights_version", sa.String(length=32), nullable=False),
        sa.Column("decision_version", sa.String(length=32), nullable=False),
        sa.Column("dedup_key", sa.String(length=191), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "gate_status IN ('passed', 'blocked', 'degraded', 'unavailable')",
            name="ck_opportunity_gate_decisions_valid_gate_status",
        ),
        sa.CheckConstraint(
            "direction IN ('long', 'short')",
            name="ck_opportunity_gate_decisions_valid_direction",
        ),
        sa.ForeignKeyConstraint(
            ["market_feature_snapshot_id"],
            ["realtime_market_feature_snapshots.id"],
            name="fk_opportunity_gate_decisions_market_feature",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["opportunity_id", "user_id"],
            ["ai_monitor_opportunities.id", "ai_monitor_opportunities.user_id"],
            name="fk_opportunity_gate_decisions_opportunity_user",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_opportunity_gate_decisions"),
        sa.UniqueConstraint(
            "public_id", name="uq_opportunity_gate_decisions_public_id"
        ),
        sa.UniqueConstraint(
            "dedup_key", name="uq_opportunity_gate_decisions_dedup_key"
        ),
        comment="Immutable pass/reject evidence for every AI opportunity gate evaluation",
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )
    op.create_index(
        "ix_opportunity_gate_decisions_user_time",
        "opportunity_gate_decisions",
        ["user_id", "decision_at"],
    )
    op.create_index(
        "ix_opportunity_gate_decisions_opportunity_time",
        "opportunity_gate_decisions",
        ["opportunity_id", "decision_at"],
    )


def downgrade() -> None:
    _require_mysql()
    op.drop_index(
        "ix_opportunity_gate_decisions_opportunity_time",
        table_name="opportunity_gate_decisions",
    )
    op.drop_index(
        "ix_opportunity_gate_decisions_user_time",
        table_name="opportunity_gate_decisions",
    )
    op.drop_table("opportunity_gate_decisions")
