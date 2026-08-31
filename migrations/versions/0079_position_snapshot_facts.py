"""Add append-only position facts sourced by durable execution outcomes.

Revision ID: 0079_position_snapshot_facts
Revises: 0078_data_model_governance
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision: str = "0079_position_snapshot_facts"
down_revision: str | None = "0078_data_model_governance"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _require_mysql() -> None:
    if op.get_bind().dialect.name not in {"mysql", "mariadb"}:
        raise RuntimeError("QuantDesk migrations require MySQL or MariaDB")


def upgrade() -> None:
    _require_mysql()
    op.create_table(
        "position_snapshots",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("public_id", sa.String(36), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("deployment_id", sa.BigInteger(), nullable=True),
        sa.Column("strategy_revision_id", sa.BigInteger(), nullable=True),
        sa.Column("mode", sa.String(16), nullable=False),
        sa.Column("account_scope", sa.String(191), nullable=False),
        sa.Column("symbol", sa.String(32), nullable=False),
        sa.Column("position_side", sa.String(8), nullable=False),
        sa.Column("position_state", sa.String(16), nullable=False),
        sa.Column("quantity", sa.Numeric(30, 18), nullable=False),
        sa.Column("average_entry_price", sa.Numeric(30, 18), nullable=True),
        sa.Column("mark_price", sa.Numeric(30, 18), nullable=True),
        sa.Column("source_type", sa.String(32), nullable=False),
        sa.Column("source_key", sa.String(191), nullable=False),
        sa.Column("snapshot_json", sa.JSON(), nullable=False),
        sa.Column("snapshot_hash", sa.String(64), nullable=False),
        sa.Column("observed_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.Column(
            "created_at",
            mysql.DATETIME(fsp=6),
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "mode IN ('paper', 'shadow', 'live')",
            name="ck_position_snapshots_valid_mode",
        ),
        sa.CheckConstraint(
            "position_state IN ('open', 'closed')",
            name="ck_position_snapshots_valid_state",
        ),
        sa.CheckConstraint(
            "position_side IN ('BOTH', 'LONG', 'SHORT')",
            name="ck_position_snapshots_valid_position_side",
        ),
        sa.CheckConstraint(
            "quantity >= 0",
            name="ck_position_snapshots_nonnegative_quantity",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_position_snapshots_user",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["deployment_id", "user_id"],
            ["strategy_deployments.id", "strategy_deployments.user_id"],
            name="fk_position_snapshots_deployment_tenant",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["strategy_revision_id", "user_id"],
            ["strategy_revisions.id", "strategy_revisions.user_id"],
            name="fk_position_snapshots_revision_tenant",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_position_snapshots"),
        sa.UniqueConstraint("public_id", name="uq_position_snapshots_public_id"),
        sa.UniqueConstraint(
            "mode",
            "account_scope",
            "source_type",
            "source_key",
            name="uq_position_snapshots_source",
        ),
        comment="Append-only position facts produced from durable execution outcomes",
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )
    op.create_index(
        "ix_position_snapshots_account_observed",
        "position_snapshots",
        ["user_id", "mode", "account_scope", "observed_at"],
    )
    op.create_index(
        "ix_position_snapshots_symbol_observed",
        "position_snapshots",
        ["symbol", "observed_at"],
    )
    op.execute(
        """
        INSERT IGNORE INTO position_snapshots (
            public_id,user_id,deployment_id,strategy_revision_id,mode,
            account_scope,symbol,position_side,position_state,quantity,
            average_entry_price,mark_price,source_type,source_key,
            snapshot_json,snapshot_hash,observed_at,created_at
        )
        SELECT UUID(), execution_row.user_id, execution_row.deployment_id,
               deployment_row.strategy_revision_id, 'paper',
               CONCAT('paper:', CAST(execution_row.paper_account_id AS CHAR)),
               execution_row.symbol, execution_row.position_side,
               IF(execution_row.action='open','open','closed'),
               IF(execution_row.action='open',execution_row.executed_quantity,0),
               IF(
                   execution_row.action='open', execution_row.average_price,
                   CAST(JSON_UNQUOTE(JSON_EXTRACT(
                       execution_row.projection_json,'$.trade.entry_price'
                   )) AS DECIMAL(30,18))
               ),
               execution_row.average_price,
               'paper_execution', execution_row.public_id,
               JSON_OBJECT(
                   'backfilled', TRUE,
                   'execution_public_id', execution_row.public_id,
                   'action', execution_row.action,
                   'state', IF(execution_row.action='open','open','closed')
               ),
               SHA2(CONCAT('paper-position:',execution_row.public_id),256),
               execution_row.updated_at, execution_row.updated_at
        FROM paper_order_executions AS execution_row
        JOIN strategy_deployments AS deployment_row
          ON deployment_row.id=execution_row.deployment_id
         AND deployment_row.user_id=execution_row.user_id
        WHERE execution_row.status='FILLED'
          AND execution_row.projection_status='applied'
        """
    )
    op.execute(
        """
        INSERT IGNORE INTO position_snapshots (
            public_id,user_id,deployment_id,strategy_revision_id,mode,
            account_scope,symbol,position_side,position_state,quantity,
            average_entry_price,mark_price,source_type,source_key,
            snapshot_json,snapshot_hash,observed_at,created_at
        )
        SELECT UUID(), intent_row.user_id, intent_row.deployment_id,
               deployment_row.strategy_revision_id, 'live',
               CONCAT('live:', CAST(intent_row.live_account_id AS CHAR)),
               intent_row.symbol, intent_row.position_side,
               IF(intent_row.action='open','open','closed'),
               IF(intent_row.action='open',COALESCE(intent_row.quantity,0),0),
               CAST(JSON_UNQUOTE(JSON_EXTRACT(
                   intent_row.entry_basis_json,'$.execution.entry_price'
               )) AS DECIMAL(30,18)),
               CAST(JSON_UNQUOTE(JSON_EXTRACT(
                   intent_row.response_json,'$.avgPrice'
               )) AS DECIMAL(30,18)),
               'live_execution', intent_row.public_id,
               JSON_OBJECT(
                   'backfilled', TRUE,
                   'intent_public_id', intent_row.public_id,
                   'action', intent_row.action,
                   'state', IF(intent_row.action='open','open','closed')
               ),
               SHA2(CONCAT('live-position:',intent_row.public_id),256),
               intent_row.updated_at, intent_row.updated_at
        FROM live_order_intents AS intent_row
        JOIN strategy_deployments AS deployment_row
          ON deployment_row.id=intent_row.deployment_id
         AND deployment_row.user_id=intent_row.user_id
        WHERE intent_row.status='filled'
          AND intent_row.action IN ('open','close')
        """
    )


def downgrade() -> None:
    _require_mysql()
    op.drop_index(
        "ix_position_snapshots_symbol_observed", table_name="position_snapshots"
    )
    op.drop_index(
        "ix_position_snapshots_account_observed", table_name="position_snapshots"
    )
    op.drop_table("position_snapshots")
