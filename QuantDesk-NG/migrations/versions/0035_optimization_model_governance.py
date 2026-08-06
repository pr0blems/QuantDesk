"""Add auditable parameter optimization and model evidence governance.

Revision ID: 0035_optimization_governance
Revises: 0034_proxy_management
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0035_optimization_governance"
down_revision: str | None = "0034_proxy_management"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _require_mysql() -> None:
    if op.get_bind().dialect.name not in {"mysql", "mariadb"}:
        raise RuntimeError("QuantDesk migrations require MySQL or MariaDB")


def upgrade() -> None:
    _require_mysql()
    op.create_table(
        "optimization_runs",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("public_id", sa.String(36), nullable=False),
        sa.Column("user_id", sa.BigInteger(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("strategy_id", sa.String(36), nullable=False),
        sa.Column("baseline_backtest_run_id", sa.BigInteger(), sa.ForeignKey("backtest_runs.id", ondelete="SET NULL")),
        sa.Column("parameter_space_json", sa.JSON(), nullable=False),
        sa.Column("objective_json", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("proposal_json", sa.JSON()),
        sa.Column("decision_reason", sa.String(500)),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("decided_at", sa.DateTime()),
        sa.UniqueConstraint("public_id", name="uq_optimization_runs_public_id"),
        sa.CheckConstraint("status IN ('draft','evaluating','proposed','approved','rejected','rolled_back')", name="ck_optimization_runs_valid_status"),
        mysql_engine="InnoDB", mysql_charset="utf8mb4",
    )
    op.create_index("ix_optimization_runs_user_created", "optimization_runs", ["user_id", "created_at"])
    op.create_table(
        "optimization_candidates",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("optimization_run_id", sa.BigInteger(), sa.ForeignKey("optimization_runs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("backtest_run_id", sa.BigInteger(), sa.ForeignKey("backtest_runs.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("parameters_json", sa.JSON(), nullable=False),
        sa.Column("evaluation_json", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("rejection_reason", sa.String(500)),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("status IN ('accepted','rejected')", name="ck_optimization_candidates_valid_status"),
        sa.UniqueConstraint("optimization_run_id", "backtest_run_id", name="uq_optimization_candidate_run_backtest"),
        mysql_engine="InnoDB", mysql_charset="utf8mb4",
    )
    op.create_index("ix_optimization_candidates_run_status", "optimization_candidates", ["optimization_run_id", "status"])
    op.create_table(
        "model_evidence",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("evidence_key", sa.String(191), nullable=False),
        sa.Column("source_kind", sa.String(16), nullable=False),
        sa.Column("provider_key", sa.String(64), nullable=False),
        sa.Column("provider_version", sa.String(64), nullable=False),
        sa.Column("symbol", sa.String(32), nullable=False),
        sa.Column("horizon_seconds", sa.Integer(), nullable=False),
        sa.Column("direction", sa.String(16), nullable=False),
        sa.Column("probability", sa.Numeric(10, 8), nullable=False),
        sa.Column("quality_score", sa.Numeric(10, 8), nullable=False),
        sa.Column("as_of_ms", sa.BigInteger(), nullable=False),
        sa.Column("expires_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("evidence_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("evidence_key", name="uq_model_evidence_key"),
        sa.CheckConstraint("direction IN ('long','short','neutral')", name="ck_model_evidence_valid_direction"),
        sa.CheckConstraint("source_kind IN ('model','evidence')", name="ck_model_evidence_valid_source_kind"),
        sa.CheckConstraint("probability >= 0 AND probability <= 1", name="ck_model_evidence_valid_probability"),
        sa.CheckConstraint("quality_score >= 0 AND quality_score <= 1", name="ck_model_evidence_valid_quality"),
        mysql_engine="InnoDB", mysql_charset="utf8mb4",
    )
    op.create_index("ix_model_evidence_symbol_horizon_asof", "model_evidence", ["symbol", "horizon_seconds", "as_of_ms"])
    op.create_table(
        "model_releases",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("model_key", sa.String(64), nullable=False),
        sa.Column("model_version", sa.Integer(), nullable=False),
        sa.Column("horizon_seconds", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("validation_snapshot_json", sa.JSON(), nullable=False),
        sa.Column("gate_json", sa.JSON(), nullable=False),
        sa.Column("decision_reason", sa.String(500)),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("decided_at", sa.DateTime()),
        sa.UniqueConstraint("model_key", "model_version", "horizon_seconds", name="uq_model_releases_version_horizon"),
        sa.CheckConstraint("status IN ('shadow','approved','rejected','rolled_back')", name="ck_model_releases_valid_status"),
        mysql_engine="InnoDB", mysql_charset="utf8mb4",
    )
    op.create_index("ix_model_releases_status_horizon", "model_releases", ["status", "horizon_seconds"])


def downgrade() -> None:
    _require_mysql()
    op.drop_index("ix_model_releases_status_horizon", table_name="model_releases")
    op.drop_table("model_releases")
    op.drop_index("ix_model_evidence_symbol_horizon_asof", table_name="model_evidence")
    op.drop_table("model_evidence")
    op.drop_index("ix_optimization_candidates_run_status", table_name="optimization_candidates")
    op.drop_table("optimization_candidates")
    op.drop_index("ix_optimization_runs_user_created", table_name="optimization_runs")
    op.drop_table("optimization_runs")
