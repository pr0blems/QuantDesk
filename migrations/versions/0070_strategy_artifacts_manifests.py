"""Add immutable strategy artifacts, validation evidence and run manifests.

Revision ID: 0070_strategy_artifacts_manifests
Revises: 0069_worker_heartbeats
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision: str = "0070_strategy_artifacts_manifests"
down_revision: str | None = "0069_worker_heartbeats"
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
        "strategy_artifacts",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("public_id", sa.String(36), nullable=False),
        sa.Column("strategy_revision_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("source_hash", sa.String(64), nullable=True),
        sa.Column("runtime_image_digest", sa.String(191), nullable=False),
        sa.Column("parameter_hash", sa.String(64), nullable=False),
        sa.Column("dependency_hash", sa.String(64), nullable=False),
        sa.Column("artifact_manifest_json", sa.JSON(), nullable=False),
        sa.Column(
            "created_at",
            mysql.DATETIME(fsp=6),
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["strategy_revision_id", "user_id"],
            ["strategy_revisions.id", "strategy_revisions.user_id"],
            name="fk_strategy_artifacts_revision_tenant",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_strategy_artifacts"),
        sa.UniqueConstraint("public_id", name="uq_strategy_artifacts_public_id"),
        sa.UniqueConstraint("strategy_revision_id", name="uq_strategy_artifacts_revision"),
        comment="Immutable strategy revision build and dependency identity",
        **TABLE_OPTIONS,
    )
    op.create_index(
        "ix_strategy_artifacts_user_created",
        "strategy_artifacts",
        ["user_id", "created_at"],
    )

    op.create_table(
        "strategy_validation_runs",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("public_id", sa.String(36), nullable=False),
        sa.Column("strategy_revision_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("validation_type", sa.String(24), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("report_json", sa.JSON(), nullable=False),
        sa.Column("started_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.Column("completed_at", mysql.DATETIME(fsp=6), nullable=True),
        sa.Column(
            "created_at",
            mysql.DATETIME(fsp=6),
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "validation_type IN ('static', 'unit', 'backtest', 'oos', 'stress', "
            "'shadow', 'paper', 'micro_live', 'fault_drill')",
            name="ck_strategy_validation_runs_valid_validation_type",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'running', 'passed', 'failed', 'cancelled')",
            name="ck_strategy_validation_runs_valid_status",
        ),
        sa.ForeignKeyConstraint(
            ["strategy_revision_id", "user_id"],
            ["strategy_revisions.id", "strategy_revisions.user_id"],
            name="fk_strategy_validation_runs_revision_tenant",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_strategy_validation_runs"),
        sa.UniqueConstraint("public_id", name="uq_strategy_validation_runs_public_id"),
        comment="Append-only validation reports for immutable revisions",
        **TABLE_OPTIONS,
    )
    op.create_index(
        "ix_strategy_validation_runs_revision_type",
        "strategy_validation_runs",
        ["strategy_revision_id", "validation_type", "created_at"],
    )

    op.create_table(
        "strategy_run_manifests",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("public_id", sa.String(36), nullable=False),
        sa.Column("deployment_id", sa.BigInteger(), nullable=False),
        sa.Column("strategy_revision_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("mode", sa.String(16), nullable=False),
        sa.Column("data_set_id", sa.String(191), nullable=True),
        sa.Column("engine_version", sa.String(64), nullable=False),
        sa.Column("cost_model_version", sa.String(64), nullable=False),
        sa.Column("fill_model_version", sa.String(64), nullable=False),
        sa.Column("risk_policy_version", sa.String(64), nullable=False),
        sa.Column("manifest_json", sa.JSON(), nullable=False),
        sa.Column("manifest_hash", sa.String(64), nullable=False),
        sa.Column(
            "created_at",
            mysql.DATETIME(fsp=6),
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "mode IN ('backtest', 'paper', 'shadow', 'live')",
            name="ck_strategy_run_manifests_valid_mode",
        ),
        sa.ForeignKeyConstraint(
            ["deployment_id", "user_id"],
            ["strategy_deployments.id", "strategy_deployments.user_id"],
            name="fk_strategy_run_manifests_deployment_tenant",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["strategy_revision_id", "user_id"],
            ["strategy_revisions.id", "strategy_revisions.user_id"],
            name="fk_strategy_run_manifests_revision_tenant",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_strategy_run_manifests"),
        sa.UniqueConstraint("public_id", name="uq_strategy_run_manifests_public_id"),
        sa.UniqueConstraint("deployment_id", name="uq_strategy_run_manifests_deployment"),
        sa.UniqueConstraint("manifest_hash", name="uq_strategy_run_manifests_hash"),
        comment="Frozen reproducibility manifest for every strategy deployment",
        **TABLE_OPTIONS,
    )
    op.create_index(
        "ix_strategy_run_manifests_user_created",
        "strategy_run_manifests",
        ["user_id", "created_at"],
    )

    op.execute(
        """
        INSERT INTO strategy_artifacts (
            public_id, strategy_revision_id, user_id, source_hash,
            runtime_image_digest, parameter_hash, dependency_hash,
            artifact_manifest_json, created_at
        )
        SELECT UUID(), revision_row.id, revision_row.user_id,
               COALESCE(revision_row.source_hash, revision_row.spec_hash),
               'legacy:unknown',
               SHA2(COALESCE(CAST(JSON_EXTRACT(revision_row.snapshot_json, '$.parameters')
                    AS CHAR), '{}'), 256),
               SHA2(COALESCE(CAST(revision_row.validation_json AS CHAR), '{}'), 256),
               JSON_OBJECT(
                   'backfilled', TRUE,
                   'strategy_version', revision_row.version,
                   'source_runtime_version', revision_row.source_runtime_version
               ),
               revision_row.created_at
        FROM strategy_revisions AS revision_row
        """
    )
    op.execute(
        """
        INSERT INTO strategy_validation_runs (
            public_id, strategy_revision_id, user_id, validation_type, status,
            report_json, started_at, completed_at, created_at
        )
        SELECT UUID(), revision_row.id, revision_row.user_id, 'static',
               CASE
                   WHEN JSON_EXTRACT(revision_row.validation_json, '$.valid') = TRUE
                     OR revision_row.validation_json IS NULL
                   THEN 'passed' ELSE 'failed'
               END,
               COALESCE(revision_row.validation_json, JSON_OBJECT('backfilled', TRUE)),
               revision_row.created_at, revision_row.created_at, revision_row.created_at
        FROM strategy_revisions AS revision_row
        """
    )
    op.execute(
        """
        INSERT INTO strategy_run_manifests (
            public_id, deployment_id, strategy_revision_id, user_id, mode,
            data_set_id, engine_version, cost_model_version, fill_model_version,
            risk_policy_version, manifest_json, manifest_hash, created_at
        )
        SELECT UUID(), deployment_row.id, deployment_row.strategy_revision_id,
               deployment_row.user_id, deployment_row.mode,
               CONCAT(deployment_row.mode, ':legacy:',
                      COALESCE(CAST(deployment_row.target_account_id AS CHAR), 'none')),
               'legacy:unknown', 'legacy:unknown', 'legacy:unknown', 'legacy:unknown',
               JSON_OBJECT(
                   'backfilled', TRUE,
                   'deployment_id', deployment_row.public_id,
                   'strategy_revision_id', deployment_row.strategy_revision_id,
                   'runtime_state', deployment_row.runtime_state_json
               ),
               SHA2(CONCAT(deployment_row.public_id, ':legacy'), 256),
               deployment_row.created_at
        FROM strategy_deployments AS deployment_row
        """
    )


def downgrade() -> None:
    _require_mysql()
    op.drop_index(
        "ix_strategy_run_manifests_user_created",
        table_name="strategy_run_manifests",
    )
    op.drop_table("strategy_run_manifests")
    op.drop_index(
        "ix_strategy_validation_runs_revision_type",
        table_name="strategy_validation_runs",
    )
    op.drop_table("strategy_validation_runs")
    op.drop_index(
        "ix_strategy_artifacts_user_created",
        table_name="strategy_artifacts",
    )
    op.drop_table("strategy_artifacts")
