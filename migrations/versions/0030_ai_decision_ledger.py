"""Add the append-only, hash-chained AI decision ledger.

Revision ID: 0030_ai_decision_ledger
Revises: 0029_market_microstructure
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision: str = "0030_ai_decision_ledger"
down_revision: str | None = "0029_market_microstructure"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE_OPTIONS = {
    "mysql_engine": "InnoDB",
    "mysql_charset": "utf8mb4",
    "mysql_collate": "utf8mb4_bin",
}
GENESIS_RECORD_HASH = "0" * 64


def _require_mysql() -> None:
    if op.get_bind().dialect.name not in {"mysql", "mariadb"}:
        raise RuntimeError("QuantDesk migrations require MySQL or MariaDB")


def upgrade() -> None:
    _require_mysql()
    op.create_table(
        "ai_decision_ledger_heads",
        sa.Column("actor_scope_id", sa.String(128), nullable=False),
        sa.Column("last_sequence", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "last_record_hash",
            sa.String(64),
            server_default=sa.text(f"'{GENESIS_RECORD_HASH}'"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            mysql.DATETIME(fsp=6),
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "last_sequence >= 0",
            name="ck_ai_decision_ledger_heads_sequence_nonnegative",
        ),
        sa.PrimaryKeyConstraint("actor_scope_id", name="pk_ai_decision_ledger_heads"),
        comment="AI decision hash-chain head per actor scope; internal append-only control table",
        **TABLE_OPTIONS,
    )
    op.create_table(
        "ai_decision_ledger_records",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("event_id", sa.String(32), nullable=False),
        sa.Column("actor_scope_id", sa.String(128), nullable=False),
        sa.Column("sequence", sa.BigInteger(), nullable=False),
        sa.Column("decision_run_id", sa.String(128), nullable=False),
        sa.Column("event_type", sa.String(32), nullable=False),
        sa.Column("occurred_at", mysql.DATETIME(fsp=6), nullable=False),
        sa.Column("prompt_hash", sa.String(64), nullable=False),
        sa.Column("model_hash", sa.String(64), nullable=False),
        sa.Column("input_hash", sa.String(64), nullable=False),
        sa.Column("output_hash", sa.String(64), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("previous_record_hash", sa.String(64), nullable=False),
        sa.Column("record_hash", sa.String(64), nullable=False),
        sa.Column(
            "created_at",
            mysql.DATETIME(fsp=6),
            server_default=sa.text("CURRENT_TIMESTAMP(6)"),
            nullable=False,
        ),
        sa.CheckConstraint("sequence > 0", name="ck_ai_decision_ledger_records_sequence_positive"),
        sa.CheckConstraint(
            "event_type IN ('proposal_gated', 'proposal_rejected')",
            name="ck_ai_decision_ledger_records_event_type",
        ),
        sa.ForeignKeyConstraint(
            ["actor_scope_id"],
            ["ai_decision_ledger_heads.actor_scope_id"],
            name="fk_ai_decision_ledger_records_actor_scope",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_ai_decision_ledger_records"),
        sa.UniqueConstraint("event_id", name="uq_ai_decision_ledger_records_event_id"),
        sa.UniqueConstraint(
            "actor_scope_id",
            "sequence",
            name="uq_ai_decision_ledger_records_scope_sequence",
        ),
        comment="Immutable AI proposal gate decisions with provenance hashes and a per-scope hash chain",
        **TABLE_OPTIONS,
    )
    op.create_index(
        "ix_ai_decision_ledger_records_run_time",
        "ai_decision_ledger_records",
        ["decision_run_id", "occurred_at"],
        unique=False,
    )
    op.create_index(
        "ix_ai_decision_ledger_records_scope_time",
        "ai_decision_ledger_records",
        ["actor_scope_id", "occurred_at"],
        unique=False,
    )


def downgrade() -> None:
    _require_mysql()
    op.drop_index(
        "ix_ai_decision_ledger_records_scope_time",
        table_name="ai_decision_ledger_records",
    )
    op.drop_index(
        "ix_ai_decision_ledger_records_run_time",
        table_name="ai_decision_ledger_records",
    )
    op.drop_table("ai_decision_ledger_records")
    op.drop_table("ai_decision_ledger_heads")
