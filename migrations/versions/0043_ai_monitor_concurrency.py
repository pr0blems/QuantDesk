"""Enforce AI monitor tenant integrity and atomic news claims.

Revision ID: 0043_ai_monitor_claims
Revises: 0042_security_financials
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0043_ai_monitor_claims"
down_revision: str | None = "0042_security_financials"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _require_mysql() -> None:
    if op.get_bind().dialect.name not in {"mysql", "mariadb"}:
        raise RuntimeError("QuantDesk migrations require MySQL or MariaDB")


def upgrade() -> None:
    _require_mysql()

    op.add_column(
        "news",
        sa.Column(
            "ai_claim_batch_id",
            sa.String(36),
            nullable=True,
            comment="当前原子领取该新闻的 AI 分析批次",
        ),
    )
    op.add_column(
        "news",
        sa.Column(
            "ai_claimed_at",
            sa.DateTime(),
            nullable=True,
            comment="AI 分析领取时间（UTC）",
        ),
    )
    op.create_foreign_key(
        "fk_news_ai_claim_batch_id_news_ai_batches",
        "news",
        "news_ai_batches",
        ["ai_claim_batch_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_news_ai_claim_pending",
        "news",
        ["ai_claim_batch_id", "ai_analyzed_at", "ts"],
        unique=False,
    )

    # Keep the newest active task for each tenant/type before enforcing the
    # generated-column uniqueness guard.
    op.execute(
        sa.text(
            "UPDATE ai_monitor_runs AS duplicate "
            "JOIN ai_monitor_runs AS keeper "
            "ON keeper.user_id=duplicate.user_id "
            "AND keeper.run_type=duplicate.run_type "
            "AND keeper.status IN ('pending','running') "
            "AND duplicate.status IN ('pending','running') "
            "AND keeper.id>duplicate.id "
            "SET duplicate.status='failed', "
            "duplicate.error_message='并发重复任务已在迁移时释放', "
            "duplicate.completed_at=COALESCE(duplicate.completed_at, UTC_TIMESTAMP()), "
            "duplicate.updated_at=UTC_TIMESTAMP()"
        )
    )
    op.execute(
        sa.text(
            "UPDATE news_ai_batches AS batch "
            "JOIN ai_monitor_runs AS run ON run.news_batch_id=batch.id "
            "SET batch.status='failed', "
            "batch.error_message='并发重复任务已在迁移时释放', "
            "batch.completed_at=COALESCE(batch.completed_at, UTC_TIMESTAMP()), "
            "batch.updated_at=UTC_TIMESTAMP() "
            "WHERE run.error_message='并发重复任务已在迁移时释放' "
            "AND batch.status IN ('pending','running')"
        )
    )
    op.add_column(
        "ai_monitor_runs",
        sa.Column(
            "active_user_id",
            sa.BigInteger(),
            sa.Computed(
                "CASE WHEN status IN ('pending', 'running') THEN user_id ELSE NULL END",
                persisted=True,
            ),
            nullable=True,
            comment="活动任务唯一性生成列；非活动任务为空",
        ),
    )
    op.create_unique_constraint(
        "uq_ai_monitor_runs_active_user_type",
        "ai_monitor_runs",
        ["active_user_id", "run_type"],
    )

    # Repair any historical mismatch before replacing independent parent FKs
    # with tenant-preserving composite keys.
    op.execute(
        sa.text(
            "UPDATE ai_monitor_opportunities AS opportunity "
            "JOIN ai_monitor_runs AS run ON run.id=opportunity.analysis_run_id "
            "SET opportunity.user_id=run.user_id "
            "WHERE opportunity.user_id<>run.user_id"
        )
    )
    op.execute(
        sa.text(
            "UPDATE ai_monitor_predictions AS prediction "
            "JOIN ai_monitor_opportunities AS opportunity "
            "ON opportunity.id=prediction.opportunity_id "
            "SET prediction.user_id=opportunity.user_id "
            "WHERE prediction.user_id<>opportunity.user_id"
        )
    )
    op.create_unique_constraint(
        "uq_ai_monitor_runs_id_user_id",
        "ai_monitor_runs",
        ["id", "user_id"],
    )
    op.drop_constraint(
        "fk_ai_monitor_opportunities_analysis_run_id_ai_monitor_runs",
        "ai_monitor_opportunities",
        type_="foreignkey",
    )
    op.create_foreign_key(
        "fk_ai_monitor_opportunities_run_user",
        "ai_monitor_opportunities",
        "ai_monitor_runs",
        ["analysis_run_id", "user_id"],
        ["id", "user_id"],
        ondelete="CASCADE",
    )
    op.create_unique_constraint(
        "uq_ai_monitor_opportunities_id_user_id",
        "ai_monitor_opportunities",
        ["id", "user_id"],
    )
    op.drop_constraint(
        "fk_ai_mon_pred_opportunity",
        "ai_monitor_predictions",
        type_="foreignkey",
    )
    op.create_foreign_key(
        "fk_ai_monitor_predictions_opportunity_user",
        "ai_monitor_predictions",
        "ai_monitor_opportunities",
        ["opportunity_id", "user_id"],
        ["id", "user_id"],
        ondelete="CASCADE",
    )


def downgrade() -> None:
    _require_mysql()

    op.drop_constraint(
        "fk_ai_monitor_predictions_opportunity_user",
        "ai_monitor_predictions",
        type_="foreignkey",
    )
    op.create_foreign_key(
        "fk_ai_mon_pred_opportunity",
        "ai_monitor_predictions",
        "ai_monitor_opportunities",
        ["opportunity_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.drop_constraint(
        "uq_ai_monitor_opportunities_id_user_id",
        "ai_monitor_opportunities",
        type_="unique",
    )
    op.drop_constraint(
        "fk_ai_monitor_opportunities_run_user",
        "ai_monitor_opportunities",
        type_="foreignkey",
    )
    op.create_foreign_key(
        "fk_ai_monitor_opportunities_analysis_run_id_ai_monitor_runs",
        "ai_monitor_opportunities",
        "ai_monitor_runs",
        ["analysis_run_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.drop_constraint(
        "uq_ai_monitor_runs_id_user_id",
        "ai_monitor_runs",
        type_="unique",
    )
    op.drop_constraint(
        "uq_ai_monitor_runs_active_user_type",
        "ai_monitor_runs",
        type_="unique",
    )
    op.drop_column("ai_monitor_runs", "active_user_id")

    op.drop_constraint(
        "fk_news_ai_claim_batch_id_news_ai_batches",
        "news",
        type_="foreignkey",
    )
    op.drop_index("ix_news_ai_claim_pending", table_name="news")
    op.drop_column("news", "ai_claimed_at")
    op.drop_column("news", "ai_claim_batch_id")
