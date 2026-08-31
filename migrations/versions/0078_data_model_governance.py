"""Separate backtest facts from deployment state.

Revision ID: 0078_data_model_governance
Revises: 0077_live_canary_observations
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0078_data_model_governance"
down_revision: str | None = "0077_live_canary_observations"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _require_mysql() -> None:
    if op.get_bind().dialect.name not in {"mysql", "mariadb"}:
        raise RuntimeError("QuantDesk migrations require MySQL or MariaDB")


def _column(table_name: str, column_name: str) -> dict[str, object] | None:
    for item in sa.inspect(op.get_bind()).get_columns(table_name):
        if item["name"] == column_name:
            return item
    return None


def _constraint_names(table_name: str) -> set[str]:
    inspector = sa.inspect(op.get_bind())
    groups = (
        inspector.get_foreign_keys(table_name),
        inspector.get_unique_constraints(table_name),
        inspector.get_check_constraints(table_name),
    )
    return {
        str(item["name"])
        for group in groups
        for item in group
        if item.get("name")
    }


def _index_names(table_name: str) -> set[str]:
    return {
        str(item["name"])
        for item in sa.inspect(op.get_bind()).get_indexes(table_name)
        if item.get("name")
    }


def upgrade() -> None:
    _require_mysql()

    # MySQL DDL is not transactional.  Every schema operation is intentionally
    # resumable so an interrupted production migration can safely continue.
    if _column("backtest_runs", "public_id") is None:
        op.add_column(
            "backtest_runs", sa.Column("public_id", sa.String(36), nullable=True)
        )
    if _column("backtest_runs", "user_strategy_id") is None:
        op.add_column(
            "backtest_runs",
            sa.Column("user_strategy_id", sa.BigInteger(), nullable=True),
        )
    if _column("backtest_runs", "strategy_revision_id") is None:
        op.add_column(
            "backtest_runs",
            sa.Column("strategy_revision_id", sa.BigInteger(), nullable=True),
        )
    op.execute("UPDATE backtest_runs SET public_id = UUID() WHERE public_id IS NULL")
    op.execute(
        """
        UPDATE backtest_runs AS run_row
        JOIN (
            SELECT deployment_row.target_account_id AS run_id,
                   deployment_row.user_id,
                   MAX(deployment_row.id) AS deployment_id
            FROM strategy_deployments AS deployment_row
            WHERE deployment_row.mode = 'backtest'
              AND deployment_row.target_account_id IS NOT NULL
            GROUP BY deployment_row.target_account_id, deployment_row.user_id
        ) AS selected
          ON selected.run_id = run_row.id
         AND selected.user_id = run_row.user_id
        JOIN strategy_deployments AS deployment_row
          ON deployment_row.id = selected.deployment_id
        SET run_row.user_strategy_id = deployment_row.strategy_id,
            run_row.strategy_revision_id = deployment_row.strategy_revision_id
        """
    )
    public_id = _column("backtest_runs", "public_id")
    if public_id is not None and bool(public_id.get("nullable")):
        op.alter_column(
            "backtest_runs",
            "public_id",
            existing_type=sa.String(36),
            nullable=False,
        )
    run_constraints = _constraint_names("backtest_runs")
    if "uq_backtest_runs_public_id" not in run_constraints:
        op.create_unique_constraint(
            "uq_backtest_runs_public_id", "backtest_runs", ["public_id"]
        )
    if "uq_backtest_runs_id_user_id" not in run_constraints:
        op.create_unique_constraint(
            "uq_backtest_runs_id_user_id", "backtest_runs", ["id", "user_id"]
        )
    if "fk_backtest_runs_strategy_tenant" not in run_constraints:
        op.create_foreign_key(
            "fk_backtest_runs_strategy_tenant",
            "backtest_runs",
            "user_strategies",
            ["user_strategy_id", "user_id"],
            ["id", "user_id"],
            ondelete="RESTRICT",
        )
    if "fk_backtest_runs_revision_tenant" not in run_constraints:
        op.create_foreign_key(
            "fk_backtest_runs_revision_tenant",
            "backtest_runs",
            "strategy_revisions",
            ["strategy_revision_id", "user_id"],
            ["id", "user_id"],
            ondelete="RESTRICT",
        )
    if "ix_backtest_runs_revision_created" not in _index_names("backtest_runs"):
        op.create_index(
            "ix_backtest_runs_revision_created",
            "backtest_runs",
            ["strategy_revision_id", "created_at"],
        )

    if _column("strategy_run_manifests", "backtest_run_id") is None:
        op.add_column(
            "strategy_run_manifests",
            sa.Column("backtest_run_id", sa.BigInteger(), nullable=True),
        )
    manifest_constraints = _constraint_names("strategy_run_manifests")
    for name, constraint_type in (
        ("ck_strategy_run_manifests_valid_mode", "check"),
        ("fk_strategy_run_manifests_deployment_tenant", "foreignkey"),
        ("fk_strategy_run_manifests_revision_tenant", "foreignkey"),
        ("uq_strategy_run_manifests_deployment", "unique"),
    ):
        if name in manifest_constraints:
            op.drop_constraint(
                name,
                "strategy_run_manifests",
                type_=constraint_type,
            )
    deployment_id = _column("strategy_run_manifests", "deployment_id")
    if deployment_id is not None and not bool(deployment_id.get("nullable")):
        op.alter_column(
            "strategy_run_manifests",
            "deployment_id",
            existing_type=sa.BigInteger(),
            nullable=True,
        )
    revision_id = _column("strategy_run_manifests", "strategy_revision_id")
    if revision_id is not None and not bool(revision_id.get("nullable")):
        op.alter_column(
            "strategy_run_manifests",
            "strategy_revision_id",
            existing_type=sa.BigInteger(),
            nullable=True,
        )
    op.execute(
        """
        DELETE manifest_row
        FROM strategy_run_manifests AS manifest_row
        JOIN strategy_deployments AS deployment_row
          ON deployment_row.id = manifest_row.deployment_id
         AND deployment_row.mode = 'backtest'
        LEFT JOIN backtest_runs AS run_row
          ON run_row.id = deployment_row.target_account_id
         AND run_row.user_id = deployment_row.user_id
        WHERE run_row.id IS NULL
        """
    )
    op.execute(
        """
        DELETE manifest_row
        FROM strategy_run_manifests AS manifest_row
        JOIN strategy_deployments AS deployment_row
          ON deployment_row.id = manifest_row.deployment_id
         AND deployment_row.mode = 'backtest'
        JOIN (
            SELECT candidate.target_account_id AS run_id,
                   candidate.user_id,
                   MAX(candidate.id) AS deployment_id
            FROM strategy_deployments AS candidate
            WHERE candidate.mode = 'backtest'
              AND candidate.target_account_id IS NOT NULL
            GROUP BY candidate.target_account_id, candidate.user_id
        ) AS selected
          ON selected.run_id = deployment_row.target_account_id
         AND selected.user_id = deployment_row.user_id
        WHERE deployment_row.id <> selected.deployment_id
        """
    )
    op.execute(
        """
        UPDATE strategy_run_manifests AS manifest_row
        JOIN strategy_deployments AS deployment_row
          ON deployment_row.id = manifest_row.deployment_id
         AND deployment_row.mode = 'backtest'
        SET manifest_row.backtest_run_id = deployment_row.target_account_id,
            manifest_row.deployment_id = NULL,
            manifest_row.mode = 'backtest'
        """
    )
    op.execute(
        """
        INSERT INTO strategy_run_manifests (
            public_id, deployment_id, backtest_run_id, strategy_revision_id,
            user_id, mode, data_set_id, engine_version, cost_model_version,
            fill_model_version, risk_policy_version, manifest_json,
            manifest_hash, created_at
        )
        SELECT UUID(), NULL, run_row.id, run_row.strategy_revision_id,
               run_row.user_id, 'backtest',
               CONCAT('backtest:legacy:', CAST(run_row.id AS CHAR)),
               'legacy:unknown', 'legacy:unknown',
               'deterministic_bar_fill_v2', 'legacy:unknown',
               JSON_OBJECT(
                   'backfilled', TRUE,
                   'backtest_run_id', run_row.public_id,
                   'strategy_key', run_row.strategy_id,
                   'config', run_row.config_json
               ),
               SHA2(CONCAT('backtest-run:', CAST(run_row.id AS CHAR),
                           CHAR(58), 'legacy'), 256),
               run_row.created_at
        FROM backtest_runs AS run_row
        WHERE NOT EXISTS (
            SELECT 1
            FROM strategy_run_manifests AS existing_manifest
            WHERE existing_manifest.backtest_run_id = run_row.id
        )
        """
    )
    manifest_constraints = _constraint_names("strategy_run_manifests")
    if "fk_strategy_run_manifests_deployment_tenant" not in manifest_constraints:
        op.create_foreign_key(
            "fk_strategy_run_manifests_deployment_tenant",
            "strategy_run_manifests",
            "strategy_deployments",
            ["deployment_id", "user_id"],
            ["id", "user_id"],
            ondelete="CASCADE",
        )
    if "fk_strategy_run_manifests_revision_tenant" not in manifest_constraints:
        op.create_foreign_key(
            "fk_strategy_run_manifests_revision_tenant",
            "strategy_run_manifests",
            "strategy_revisions",
            ["strategy_revision_id", "user_id"],
            ["id", "user_id"],
            ondelete="RESTRICT",
        )
    if "fk_strategy_run_manifests_backtest_tenant" not in manifest_constraints:
        op.create_foreign_key(
            "fk_strategy_run_manifests_backtest_tenant",
            "strategy_run_manifests",
            "backtest_runs",
            ["backtest_run_id", "user_id"],
            ["id", "user_id"],
            ondelete="CASCADE",
        )
    if "uq_strategy_run_manifests_deployment" not in manifest_constraints:
        op.create_unique_constraint(
            "uq_strategy_run_manifests_deployment",
            "strategy_run_manifests",
            ["deployment_id"],
        )
    if "uq_strategy_run_manifests_backtest" not in manifest_constraints:
        op.create_unique_constraint(
            "uq_strategy_run_manifests_backtest",
            "strategy_run_manifests",
            ["backtest_run_id"],
        )
    if "ck_strategy_run_manifests_valid_mode" not in manifest_constraints:
        op.create_check_constraint(
            "ck_strategy_run_manifests_valid_mode",
            "strategy_run_manifests",
            "mode IN ('backtest', 'paper', 'shadow', 'live')",
        )
    if "ck_strategy_run_manifests_valid_owner" not in manifest_constraints:
        op.create_check_constraint(
            "ck_strategy_run_manifests_valid_owner",
            "strategy_run_manifests",
            "(mode = 'backtest' AND backtest_run_id IS NOT NULL AND deployment_id IS NULL) "
            "OR (mode IN ('paper', 'shadow', 'live') AND deployment_id IS NOT NULL "
            "AND backtest_run_id IS NULL)",
        )

    op.execute("DELETE FROM strategy_deployments WHERE mode = 'backtest'")
    deployment_constraints = _constraint_names("strategy_deployments")
    if "ck_strategy_deployments_valid_mode" in deployment_constraints:
        op.drop_constraint(
            "ck_strategy_deployments_valid_mode",
            "strategy_deployments",
            type_="check",
        )
    op.create_check_constraint(
        "ck_strategy_deployments_valid_mode",
        "strategy_deployments",
        "mode IN ('paper', 'shadow', 'live')",
    )


def downgrade() -> None:
    _require_mysql()

    op.drop_constraint(
        "ck_strategy_deployments_valid_mode",
        "strategy_deployments",
        type_="check",
    )
    op.create_check_constraint(
        "ck_strategy_deployments_valid_mode",
        "strategy_deployments",
        "mode IN ('backtest', 'paper', 'shadow', 'live')",
    )
    op.execute(
        """
        INSERT INTO strategy_deployments (
            public_id, user_id, strategy_id, strategy_revision_id, mode,
            target_account_id, name, status, runtime_state_json,
            started_at, created_at, updated_at
        )
        SELECT UUID(), run_row.user_id, run_row.user_strategy_id,
               run_row.strategy_revision_id, 'backtest', run_row.id,
               CONCAT('Backtest · ', run_row.strategy_name, ' · ', run_row.symbol),
               'stopped', JSON_OBJECT('backtest_run_id', run_row.id),
               run_row.created_at, run_row.created_at,
               COALESCE(run_row.completed_at, run_row.created_at)
        FROM backtest_runs AS run_row
        WHERE run_row.user_strategy_id IS NOT NULL
          AND run_row.strategy_revision_id IS NOT NULL
          AND NOT EXISTS (
              SELECT 1 FROM strategy_deployments AS existing_deployment
              WHERE existing_deployment.mode = 'backtest'
                AND existing_deployment.target_account_id = run_row.id
                AND existing_deployment.user_id = run_row.user_id
          )
        """
    )

    op.drop_constraint(
        "ck_strategy_run_manifests_valid_owner",
        "strategy_run_manifests",
        type_="check",
    )
    op.drop_constraint(
        "ck_strategy_run_manifests_valid_mode",
        "strategy_run_manifests",
        type_="check",
    )
    op.drop_constraint(
        "fk_strategy_run_manifests_backtest_tenant",
        "strategy_run_manifests",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_strategy_run_manifests_deployment_tenant",
        "strategy_run_manifests",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_strategy_run_manifests_revision_tenant",
        "strategy_run_manifests",
        type_="foreignkey",
    )
    op.drop_constraint(
        "uq_strategy_run_manifests_backtest",
        "strategy_run_manifests",
        type_="unique",
    )
    op.drop_constraint(
        "uq_strategy_run_manifests_deployment",
        "strategy_run_manifests",
        type_="unique",
    )
    op.execute(
        """
        UPDATE strategy_run_manifests AS manifest_row
        JOIN backtest_runs AS run_row
          ON run_row.id = manifest_row.backtest_run_id
         AND run_row.user_id = manifest_row.user_id
        JOIN strategy_deployments AS deployment_row
          ON deployment_row.mode = 'backtest'
         AND deployment_row.target_account_id = run_row.id
         AND deployment_row.user_id = run_row.user_id
        SET manifest_row.deployment_id = deployment_row.id,
            manifest_row.strategy_revision_id = deployment_row.strategy_revision_id
        """
    )
    op.execute(
        "DELETE FROM strategy_run_manifests "
        "WHERE mode = 'backtest' AND deployment_id IS NULL"
    )
    op.alter_column(
        "strategy_run_manifests",
        "deployment_id",
        existing_type=sa.BigInteger(),
        nullable=False,
    )
    op.alter_column(
        "strategy_run_manifests",
        "strategy_revision_id",
        existing_type=sa.BigInteger(),
        nullable=False,
    )
    op.create_foreign_key(
        "fk_strategy_run_manifests_deployment_tenant",
        "strategy_run_manifests",
        "strategy_deployments",
        ["deployment_id", "user_id"],
        ["id", "user_id"],
        ondelete="CASCADE",
    )
    op.create_foreign_key(
        "fk_strategy_run_manifests_revision_tenant",
        "strategy_run_manifests",
        "strategy_revisions",
        ["strategy_revision_id", "user_id"],
        ["id", "user_id"],
        ondelete="RESTRICT",
    )
    op.create_unique_constraint(
        "uq_strategy_run_manifests_deployment",
        "strategy_run_manifests",
        ["deployment_id"],
    )
    op.create_check_constraint(
        "ck_strategy_run_manifests_valid_mode",
        "strategy_run_manifests",
        "mode IN ('backtest', 'paper', 'shadow', 'live')",
    )
    op.drop_column("strategy_run_manifests", "backtest_run_id")

    op.drop_index("ix_backtest_runs_revision_created", table_name="backtest_runs")
    op.drop_constraint(
        "fk_backtest_runs_revision_tenant", "backtest_runs", type_="foreignkey"
    )
    op.drop_constraint(
        "fk_backtest_runs_strategy_tenant", "backtest_runs", type_="foreignkey"
    )
    op.drop_constraint("uq_backtest_runs_id_user_id", "backtest_runs", type_="unique")
    op.drop_constraint("uq_backtest_runs_public_id", "backtest_runs", type_="unique")
    op.drop_column("backtest_runs", "strategy_revision_id")
    op.drop_column("backtest_runs", "user_strategy_id")
    op.drop_column("backtest_runs", "public_id")
