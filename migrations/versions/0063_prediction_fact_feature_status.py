"""Project signal-time market feature availability into prediction facts.

Revision ID: 0063_prediction_fact_status
Revises: 0062_ai_projection_outbox
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0063_prediction_fact_status"
down_revision: str | None = "0062_ai_projection_outbox"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _require_mysql() -> None:
    if op.get_bind().dialect.name not in {"mysql", "mariadb"}:
        raise RuntimeError("QuantDesk migrations require MySQL or MariaDB")


def upgrade() -> None:
    _require_mysql()
    existing = {
        column["name"]
        for column in sa.inspect(op.get_bind()).get_columns("ai_monitor_prediction_facts")
    }
    if "quote_source" not in existing:
        op.add_column(
            "ai_monitor_prediction_facts",
            sa.Column(
                "quote_source",
                sa.String(length=32),
                nullable=False,
                server_default="unknown",
            ),
        )
    if "quote_age_ms" not in existing:
        op.add_column(
            "ai_monitor_prediction_facts",
            sa.Column("quote_age_ms", sa.BigInteger(), nullable=True),
        )
    if "quote_spread_bps" not in existing:
        op.add_column(
            "ai_monitor_prediction_facts",
            sa.Column("quote_spread_bps", sa.Numeric(20, 8), nullable=True),
        )
    for name in (
        "option_flow_status",
        "gex_status",
        "institutional_flow_status",
    ):
        if name not in existing:
            op.add_column(
                "ai_monitor_prediction_facts",
                sa.Column(
                    name,
                    sa.String(length=32),
                    nullable=False,
                    server_default="not_captured_at_signal",
                ),
            )
    if "projection_version" not in existing:
        op.add_column(
            "ai_monitor_prediction_facts",
            sa.Column(
                "projection_version",
                sa.String(length=32),
                nullable=False,
                server_default="legacy_v1",
            ),
        )


def downgrade() -> None:
    _require_mysql()
    existing = {
        column["name"]
        for column in sa.inspect(op.get_bind()).get_columns("ai_monitor_prediction_facts")
    }
    for name in (
        "projection_version",
        "institutional_flow_status",
        "gex_status",
        "option_flow_status",
        "quote_spread_bps",
        "quote_age_ms",
        "quote_source",
    ):
        if name in existing:
            op.drop_column("ai_monitor_prediction_facts", name)
