"""Add isolated point-in-time historical replay storage.

Revision ID: 0050_ai_historical_replay
Revises: 0049_ai_score_weights
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0050_ai_historical_replay"
down_revision: str | None = "0049_ai_score_weights"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _require_mysql() -> None:
    if op.get_bind().dialect.name not in {"mysql", "mariadb"}:
        raise RuntimeError("QuantDesk migrations require MySQL or MariaDB")


def _table_options(comment: str) -> dict[str, str]:
    return {
        "comment": comment,
        "mysql_engine": "InnoDB",
        "mysql_charset": "utf8mb4",
    }


def upgrade() -> None:
    _require_mysql()
    op.create_table(
        "ai_monitor_replay_runs",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("public_id", sa.String(36), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column(
            "active_user_id",
            sa.BigInteger(),
            sa.Computed(
                "CASE WHEN status IN ('pending', 'running') THEN user_id ELSE NULL END",
                persisted=True,
            ),
            nullable=True,
        ),
        sa.Column("timeframe", sa.String(8), nullable=False),
        sa.Column("start_at", sa.DateTime(), nullable=False),
        sa.Column("end_at", sa.DateTime(), nullable=False),
        sa.Column("out_of_sample_start_at", sa.DateTime(), nullable=False),
        sa.Column("requested_symbols_json", sa.JSON(), nullable=False),
        sa.Column("config_snapshot_json", sa.JSON(), nullable=False),
        sa.Column("cost_model_json", sa.JSON(), nullable=False),
        sa.Column("provenance_json", sa.JSON(), nullable=False),
        sa.Column("dataset_hash", sa.String(64), nullable=True),
        sa.Column("total_symbols", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("completed_symbols", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("total_events", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("generated_signals", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("settled_signals", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("summary_json", sa.JSON(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "status IN ('pending', 'running', 'completed', 'failed', 'cancelled')",
            name=op.f("ck_ai_monitor_replay_runs_valid_status"),
        ),
        sa.CheckConstraint(
            "timeframe IN ('15m', '1h', '4h')",
            name=op.f("ck_ai_monitor_replay_runs_valid_timeframe"),
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_ai_monitor_replay_runs_user_id_users"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ai_monitor_replay_runs")),
        sa.UniqueConstraint("public_id", name="uq_ai_monitor_replay_runs_public_id"),
        sa.UniqueConstraint(
            "active_user_id", name="uq_ai_monitor_replay_runs_active_user"
        ),
        sa.UniqueConstraint("id", "user_id", name="uq_ai_monitor_replay_runs_id_user_id"),
        **_table_options("独立历史回放任务；不写入实时机会或实时预测表"),
    )
    op.create_index(
        "ix_ai_monitor_replay_runs_user_created",
        "ai_monitor_replay_runs",
        ["user_id", "created_at"],
    )
    op.create_index(
        "ix_ai_monitor_replay_runs_user_status",
        "ai_monitor_replay_runs",
        ["user_id", "status"],
    )

    op.create_table(
        "ai_monitor_replay_dataset_manifests",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("run_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("source", sa.String(64), nullable=False),
        sa.Column("symbol", sa.String(32), nullable=False, server_default="*"),
        sa.Column("data_type", sa.String(32), nullable=False),
        sa.Column("coverage_start_at", sa.DateTime(), nullable=True),
        sa.Column("coverage_end_at", sa.DateTime(), nullable=True),
        sa.Column("row_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("sha256", sa.String(64), nullable=True),
        sa.Column("exact_point_in_time", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("details_json", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["run_id", "user_id"],
            ["ai_monitor_replay_runs.id", "ai_monitor_replay_runs.user_id"],
            name="fk_ai_replay_manifest_run_user",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ai_monitor_replay_dataset_manifests")),
        sa.UniqueConstraint(
            "run_id", "source", "symbol", "data_type", name="uq_ai_replay_manifest_source"
        ),
        **_table_options("历史回放数据来源、覆盖区间、校验和与降级说明"),
    )
    op.create_index(
        "ix_ai_replay_manifest_run",
        "ai_monitor_replay_dataset_manifests",
        ["run_id", "data_type"],
    )

    op.create_table(
        "ai_monitor_replay_signals",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("public_id", sa.String(36), nullable=False),
        sa.Column("run_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("symbol", sa.String(32), nullable=False),
        sa.Column("contract_symbol", sa.String(32), nullable=False),
        sa.Column("direction", sa.String(12), nullable=False),
        sa.Column("timeframe", sa.String(8), nullable=False),
        sa.Column("sample_split", sa.String(12), nullable=False),
        sa.Column("news_score", sa.Numeric(8, 4), nullable=False),
        sa.Column("indicator_score", sa.Numeric(8, 4), nullable=False),
        sa.Column("combined_score", sa.Numeric(8, 4), nullable=False),
        sa.Column("signal_at", sa.DateTime(), nullable=False),
        sa.Column("entry_at", sa.DateTime(), nullable=False),
        sa.Column("due_at", sa.DateTime(), nullable=False),
        sa.Column("entry_price", sa.Numeric(30, 12), nullable=False),
        sa.Column("dedup_key", sa.String(191), nullable=False),
        sa.Column("news_snapshot_json", sa.JSON(), nullable=False),
        sa.Column("indicator_snapshot_json", sa.JSON(), nullable=False),
        sa.Column("evidence_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("direction IN ('long', 'short')", name=op.f("ck_ai_monitor_replay_signals_valid_direction")),
        sa.CheckConstraint("sample_split IN ('train', 'embargo', 'oos')", name=op.f("ck_ai_monitor_replay_signals_valid_split")),
        sa.ForeignKeyConstraint(
            ["run_id", "user_id"],
            ["ai_monitor_replay_runs.id", "ai_monitor_replay_runs.user_id"],
            name="fk_ai_replay_signal_run_user",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ai_monitor_replay_signals")),
        sa.UniqueConstraint("public_id", name="uq_ai_monitor_replay_signals_public_id"),
        sa.UniqueConstraint("dedup_key", name="uq_ai_monitor_replay_signals_dedup_key"),
        sa.UniqueConstraint(
            "id",
            "run_id",
            "user_id",
            name="uq_ai_monitor_replay_signals_id_run_user",
        ),
        **_table_options("独立历史回放的冻结信号"),
    )
    op.create_index("ix_ai_replay_signals_run_time", "ai_monitor_replay_signals", ["run_id", "signal_at"])
    op.create_index("ix_ai_replay_signals_user_split", "ai_monitor_replay_signals", ["user_id", "sample_split"])

    op.create_table(
        "ai_monitor_replay_outcomes",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("signal_id", sa.BigInteger(), nullable=False),
        sa.Column("run_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("sample_split", sa.String(12), nullable=False),
        sa.Column("exit_at", sa.DateTime(), nullable=False),
        sa.Column("exit_price", sa.Numeric(30, 12), nullable=False),
        sa.Column("gross_directional_return_bps", sa.Numeric(20, 8), nullable=False),
        sa.Column("estimated_cost_bps", sa.Numeric(20, 8), nullable=False),
        sa.Column("net_directional_return_bps", sa.Numeric(20, 8), nullable=False),
        sa.Column("max_favorable_bps", sa.Numeric(20, 8), nullable=False),
        sa.Column("max_adverse_bps", sa.Numeric(20, 8), nullable=False),
        sa.Column("result", sa.String(16), nullable=False),
        sa.Column("settlement_json", sa.JSON(), nullable=False),
        sa.Column("settled_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("result IN ('win', 'loss', 'flat')", name=op.f("ck_ai_monitor_replay_outcomes_valid_result")),
        sa.ForeignKeyConstraint(
            ["signal_id", "run_id", "user_id"],
            [
                "ai_monitor_replay_signals.id",
                "ai_monitor_replay_signals.run_id",
                "ai_monitor_replay_signals.user_id",
            ],
            name="fk_ai_replay_outcome_signal_run_user",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ai_monitor_replay_outcomes")),
        sa.UniqueConstraint("signal_id", name="uq_ai_monitor_replay_outcomes_signal"),
        **_table_options("历史回放信号的保守成本后结算结果"),
    )
    op.create_index(
        "ix_ai_replay_outcomes_run_split",
        "ai_monitor_replay_outcomes",
        ["run_id", "sample_split"],
    )


def downgrade() -> None:
    _require_mysql()
    op.drop_table("ai_monitor_replay_outcomes")
    op.drop_table("ai_monitor_replay_signals")
    op.drop_table("ai_monitor_replay_dataset_manifests")
    op.drop_table("ai_monitor_replay_runs")
