"""Add persistent execution safety and atomic account-risk reservations.

Revision ID: 0032_execution_controls
Revises: 0031_execution_journal
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision: str = "0032_execution_controls"
down_revision: str | None = "0031_execution_journal"
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
    if not op.get_context().as_sql:
        existing_checkpoints = op.get_bind().execute(
            sa.text(
                "SELECT COUNT(*) FROM execution_idempotency_records "
                "WHERE checkpoint_json IS NOT NULL"
            )
        ).scalar_one()
        if int(existing_checkpoints) > 0:
            raise RuntimeError(
                "0032 requires a trusted physical_account_id migration for existing "
                "execution checkpoints before any DDL can run; do not infer it from "
                "account_scope or API-key metadata"
            )
    op.add_column(
        "execution_idempotency_records",
        sa.Column("physical_account_id", sa.String(191), nullable=True),
    )
    op.create_check_constraint(
        "ck_execution_idempotency_physical_account",
        "execution_idempotency_records",
        "(checkpoint_json IS NULL AND physical_account_id IS NULL) OR "
        "(checkpoint_json IS NOT NULL AND physical_account_id IS NOT NULL)",
    )
    op.create_table(
        "execution_account_controls",
        sa.Column("control_hash", sa.String(64), nullable=False),
        sa.Column("tenant_scope", sa.String(191), nullable=False),
        sa.Column("user_scope", sa.String(191), nullable=False),
        sa.Column("account_scope", sa.String(191), nullable=False),
        sa.Column("broker_name", sa.String(64), nullable=False),
        sa.Column("physical_account_id", sa.String(191), nullable=False),
        sa.Column("market", sa.String(32), nullable=False),
        sa.Column("execution_mode", sa.String(16), nullable=False),
        sa.Column("safe_reason", sa.String(64), nullable=True),
        sa.Column("kill_reason", sa.String(64), nullable=True),
        sa.Column(
            "consecutive_failures",
            sa.BigInteger(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "version",
            sa.BigInteger(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "changed_at",
            mysql.DATETIME(fsp=6),
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
            nullable=False,
        ),
        sa.Column("changed_by", sa.String(191), nullable=True),
        sa.Column("control_policy_hash", sa.String(64), nullable=True),
        sa.Column("failure_threshold", sa.Integer(), nullable=True),
        sa.Column("risk_snapshot_high_watermark_at", mysql.DATETIME(fsp=6), nullable=True),
        sa.Column(
            "risk_snapshot_source_updated_at",
            mysql.DATETIME(fsp=6),
            nullable=True,
        ),
        sa.Column(
            "risk_snapshot_high_watermark_recorded_at",
            mysql.DATETIME(fsp=6),
            nullable=True,
        ),
        sa.Column("risk_snapshot_high_watermark_hash", sa.String(64), nullable=True),
        sa.Column(
            "risk_snapshot_high_watermark_reference",
            sa.String(191),
            nullable=True,
        ),
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
            "execution_mode IN ('backtest', 'paper', 'shadow', 'live')",
            name="ck_execution_account_control_mode",
        ),
        sa.CheckConstraint(
            "consecutive_failures >= 0",
            name="ck_execution_account_control_failures",
        ),
        sa.CheckConstraint(
            "version >= 0",
            name="ck_execution_account_control_version",
        ),
        sa.CheckConstraint(
            "(control_policy_hash IS NULL AND failure_threshold IS NULL) OR "
            "(control_policy_hash IS NOT NULL AND failure_threshold >= 1)",
            name="ck_execution_account_control_policy",
        ),
        sa.CheckConstraint(
            "(risk_snapshot_high_watermark_at IS NULL "
            "AND risk_snapshot_source_updated_at IS NULL "
            "AND risk_snapshot_high_watermark_recorded_at IS NULL "
            "AND risk_snapshot_high_watermark_hash IS NULL "
            "AND risk_snapshot_high_watermark_reference IS NULL) OR "
            "(risk_snapshot_high_watermark_at IS NOT NULL "
            "AND risk_snapshot_source_updated_at IS NOT NULL "
            "AND risk_snapshot_high_watermark_recorded_at IS NOT NULL "
            "AND risk_snapshot_high_watermark_hash IS NOT NULL "
            "AND risk_snapshot_high_watermark_reference IS NOT NULL)",
            name="ck_execution_account_control_risk_watermark",
        ),
        sa.PrimaryKeyConstraint("control_hash", name="pk_execution_account_controls"),
        sa.UniqueConstraint(
            "broker_name",
            "market",
            "physical_account_id",
            "execution_mode",
            name="uq_execution_account_control_physical_scope",
        ),
        comment=(
            "Cross-process dual-latch safety state and serialization mutex per account"
        ),
        **TABLE_OPTIONS,
    )

    op.create_table(
        "execution_quote_watermarks",
        sa.Column("account_control_hash", sa.String(64), nullable=False),
        sa.Column("symbol", sa.String(32), nullable=False),
        sa.Column("observed_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.Column("quote_hash", sa.String(64), nullable=False),
        sa.Column("reference", sa.String(191), nullable=False),
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
        sa.ForeignKeyConstraint(
            ["account_control_hash"],
            ["execution_account_controls.control_hash"],
            name="fk_execution_quote_watermark_control",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint(
            "account_control_hash",
            "symbol",
            name="pk_execution_quote_watermarks",
        ),
        comment="Monotonic quote evidence per physical account and symbol",
        **TABLE_OPTIONS,
    )

    op.create_table(
        "execution_risk_reservations",
        sa.Column("reservation_id", sa.String(64), nullable=False),
        sa.Column("account_control_hash", sa.String(64), nullable=False),
        sa.Column("execution_scope_hash", sa.String(64), nullable=False),
        sa.Column("intent_id", sa.String(191), nullable=False),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column(
            "reserved_notional",
            sa.Numeric(precision=36, scale=18),
            nullable=False,
        ),
        sa.Column("reserved_open_slots", sa.Integer(), nullable=False),
        sa.Column("policy_hash", sa.String(64), nullable=False),
        sa.Column("risk_decision_hash", sa.String(64), nullable=False),
        sa.Column("snapshot_hash", sa.String(64), nullable=False),
        sa.Column("safety_version", sa.BigInteger(), nullable=False),
        sa.Column("client_order_id", sa.String(36), nullable=False),
        sa.Column("position_key_hash", sa.String(64), nullable=False),
        sa.Column("symbol", sa.String(32), nullable=False),
        sa.Column("position_mode", sa.String(16), nullable=False),
        sa.Column("target_direction", sa.String(8), nullable=False),
        sa.Column("target_position_side", sa.String(8), nullable=False),
        sa.Column("baseline_direction", sa.String(8), nullable=True),
        sa.Column("baseline_position_side", sa.String(8), nullable=True),
        sa.Column(
            "baseline_quantity",
            sa.Numeric(precision=36, scale=18),
            nullable=False,
        ),
        sa.Column(
            "authorized_quantity",
            sa.Numeric(precision=36, scale=18),
            nullable=False,
        ),
        sa.Column("risk_reducing", sa.Boolean(), nullable=False),
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
        sa.Column("settled_at", mysql.DATETIME(fsp=6), nullable=True),
        sa.Column("release_reason", sa.String(64), nullable=True),
        sa.Column("settlement_snapshot_hash", sa.String(64), nullable=True),
        sa.Column("settlement_observed_at", mysql.DATETIME(fsp=6), nullable=True),
        sa.Column("settlement_reference", sa.String(191), nullable=True),
        sa.CheckConstraint(
            "state IN ('held', 'committed_unreflected', 'released', 'settled')",
            name="ck_execution_risk_reservation_state",
        ),
        sa.CheckConstraint(
            "reserved_notional >= 0 AND reserved_open_slots >= 0",
            name="ck_execution_risk_reservation_budget",
        ),
        sa.CheckConstraint(
            "safety_version >= 0",
            name="ck_execution_risk_reservation_safety_version",
        ),
        sa.CheckConstraint(
            "position_mode IN ('one_way', 'hedge') "
            "AND target_direction IN ('long', 'short') "
            "AND target_position_side IN ('BOTH', 'LONG', 'SHORT') "
            "AND (baseline_direction IS NULL "
            "OR baseline_direction IN ('long', 'short')) "
            "AND (baseline_position_side IS NULL "
            "OR baseline_position_side IN ('BOTH', 'LONG', 'SHORT'))",
            name="ck_execution_risk_reservation_position_identity",
        ),
        sa.CheckConstraint(
            "baseline_quantity >= 0 AND authorized_quantity > 0 AND "
            "((baseline_quantity = 0 AND baseline_direction IS NULL "
            "AND baseline_position_side IS NULL) OR "
            "(baseline_quantity > 0 AND baseline_direction IS NOT NULL "
            "AND baseline_position_side IS NOT NULL))",
            name="ck_execution_risk_reservation_baseline",
        ),
        sa.CheckConstraint(
            "(position_mode = 'one_way' AND target_position_side = 'BOTH') OR "
            "(position_mode = 'hedge' AND target_position_side IN ('LONG', 'SHORT'))",
            name="ck_execution_risk_reservation_target_side",
        ),
        sa.CheckConstraint(
            "risk_reducing IN (0, 1)",
            name="ck_execution_risk_reservation_reducing",
        ),
        sa.CheckConstraint(
            "(state IN ('held', 'committed_unreflected') "
            "AND settled_at IS NULL AND release_reason IS NULL "
            "AND settlement_snapshot_hash IS NULL "
            "AND settlement_observed_at IS NULL "
            "AND settlement_reference IS NULL) OR "
            "(state = 'released' AND settled_at IS NOT NULL "
            "AND release_reason IS NOT NULL "
            "AND settlement_snapshot_hash IS NULL "
            "AND settlement_observed_at IS NULL "
            "AND settlement_reference IS NULL) OR "
            "(state = 'settled' AND settled_at IS NOT NULL "
            "AND release_reason IS NOT NULL "
            "AND settlement_snapshot_hash IS NOT NULL "
            "AND settlement_observed_at IS NOT NULL "
            "AND settlement_reference IS NOT NULL)",
            name="ck_execution_risk_reservation_lifecycle",
        ),
        sa.ForeignKeyConstraint(
            ["account_control_hash"],
            ["execution_account_controls.control_hash"],
            name="fk_execution_risk_reservation_control",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["execution_scope_hash"],
            ["execution_idempotency_records.scope_hash"],
            name="fk_execution_risk_reservation_execution",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint(
            "reservation_id",
            name="pk_execution_risk_reservations",
        ),
        sa.UniqueConstraint(
            "execution_scope_hash",
            name="uq_execution_risk_reservation_execution",
        ),
        comment=(
            "Pending and committed-but-unreflected account risk paired with a checkpoint"
        ),
        **TABLE_OPTIONS,
    )
    op.create_index(
        "ix_execution_risk_reservation_active",
        "execution_risk_reservations",
        ["account_control_hash", "position_key_hash", "state"],
        unique=False,
    )

    op.create_table(
        "execution_safety_events",
        sa.Column("event_id", sa.String(64), nullable=False),
        sa.Column("account_control_hash", sa.String(64), nullable=False),
        sa.Column("command_id", sa.String(64), nullable=False),
        sa.Column("command_hash", sa.String(64), nullable=False),
        sa.Column("action", sa.String(32), nullable=False),
        sa.Column("actor", sa.String(191), nullable=False),
        sa.Column("reason_code", sa.String(64), nullable=False),
        sa.Column("expected_version", sa.BigInteger(), nullable=False),
        sa.Column("resulting_version", sa.BigInteger(), nullable=False),
        sa.Column("result_safe_reason", sa.String(64), nullable=True),
        sa.Column("result_kill_reason", sa.String(64), nullable=True),
        sa.Column("result_consecutive_failures", sa.BigInteger(), nullable=False),
        sa.Column("result_control_policy_hash", sa.String(64), nullable=True),
        sa.Column("result_failure_threshold", sa.Integer(), nullable=True),
        sa.Column(
            "result_risk_snapshot_high_watermark_at",
            mysql.DATETIME(fsp=6),
            nullable=True,
        ),
        sa.Column(
            "result_risk_snapshot_high_watermark_hash",
            sa.String(64),
            nullable=True,
        ),
        sa.Column(
            "result_risk_snapshot_high_watermark_reference",
            sa.String(191),
            nullable=True,
        ),
        sa.Column("result_changed_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.Column(
            "created_at",
            mysql.DATETIME(fsp=6),
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "action IN ('engage_safe_mode', 'recover_safe_mode', "
            "'engage_kill_switch', 'release_kill_switch')",
            name="ck_execution_safety_event_action",
        ),
        sa.CheckConstraint(
            "expected_version >= 0 AND resulting_version = expected_version + 1",
            name="ck_execution_safety_event_version",
        ),
        sa.CheckConstraint(
            "result_consecutive_failures >= 0",
            name="ck_execution_safety_event_failures",
        ),
        sa.CheckConstraint(
            "(result_control_policy_hash IS NULL "
            "AND result_failure_threshold IS NULL) OR "
            "(result_control_policy_hash IS NOT NULL "
            "AND result_failure_threshold >= 1)",
            name="ck_execution_safety_event_policy",
        ),
        sa.CheckConstraint(
            "(result_risk_snapshot_high_watermark_at IS NULL "
            "AND result_risk_snapshot_high_watermark_hash IS NULL "
            "AND result_risk_snapshot_high_watermark_reference IS NULL) OR "
            "(result_risk_snapshot_high_watermark_at IS NOT NULL "
            "AND result_risk_snapshot_high_watermark_hash IS NOT NULL "
            "AND result_risk_snapshot_high_watermark_reference IS NOT NULL)",
            name="ck_execution_safety_event_risk_watermark",
        ),
        sa.ForeignKeyConstraint(
            ["account_control_hash"],
            ["execution_account_controls.control_hash"],
            name="fk_execution_safety_event_control",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("event_id", name="pk_execution_safety_events"),
        sa.UniqueConstraint(
            "account_control_hash",
            "command_id",
            name="uq_execution_safety_event_command",
        ),
        comment="Append-only idempotent operator safety transition audit",
        **TABLE_OPTIONS,
    )


def downgrade() -> None:
    _require_mysql()
    op.drop_table("execution_safety_events")
    op.drop_index(
        "ix_execution_risk_reservation_active",
        table_name="execution_risk_reservations",
    )
    op.drop_table("execution_risk_reservations")
    op.drop_table("execution_quote_watermarks")
    op.drop_table("execution_account_controls")
    op.drop_constraint(
        "ck_execution_idempotency_physical_account",
        "execution_idempotency_records",
        type_="check",
    )
    op.drop_column("execution_idempotency_records", "physical_account_id")
