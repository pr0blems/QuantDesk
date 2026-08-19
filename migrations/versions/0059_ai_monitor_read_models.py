"""Add compact AI monitor analytics read models.

Revision ID: 0059_ai_monitor_read_models
Revises: 0058_finnhub_quote_snapshots
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0059_ai_monitor_read_models"
down_revision: str | None = "0058_finnhub_quote_snapshots"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _require_mysql() -> None:
    if op.get_bind().dialect.name not in {"mysql", "mariadb"}:
        raise RuntimeError("QuantDesk migrations require MySQL or MariaDB")


def upgrade() -> None:
    _require_mysql()
    op.create_table(
        "ai_monitor_prediction_facts",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("prediction_id", sa.BigInteger(), nullable=False),
        sa.Column("opportunity_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("symbol", sa.String(length=32), nullable=False),
        sa.Column("contract_symbol", sa.String(length=32), nullable=False),
        sa.Column("direction", sa.String(length=12), nullable=False),
        sa.Column("timeframe", sa.String(length=8), nullable=False),
        sa.Column("opportunity_status", sa.String(length=16), nullable=False),
        sa.Column("prediction_status", sa.String(length=16), nullable=False),
        sa.Column("result", sa.String(length=16), nullable=True),
        sa.Column("net_result", sa.String(length=16), nullable=True),
        sa.Column("market_session", sa.String(length=16), nullable=False),
        sa.Column("quote_quality", sa.String(length=16), nullable=False),
        sa.Column("event_risk", sa.String(length=16), nullable=False),
        sa.Column("data_coverage", sa.Numeric(8, 4), nullable=True),
        sa.Column("news_score", sa.Numeric(8, 4), nullable=True),
        sa.Column("technical_score", sa.Numeric(8, 4), nullable=True),
        sa.Column("market_context_score", sa.Numeric(8, 4), nullable=True),
        sa.Column("option_flow_score", sa.Numeric(8, 4), nullable=True),
        sa.Column("gex_score", sa.Numeric(8, 4), nullable=True),
        sa.Column("institutional_flow_score", sa.Numeric(8, 4), nullable=True),
        sa.Column("market_flow_score", sa.Numeric(8, 4), nullable=True),
        sa.Column("combined_score", sa.Numeric(8, 4), nullable=True),
        sa.Column(
            "price_source",
            sa.String(length=16),
            server_default="binance",
            nullable=False,
        ),
        sa.Column("entry_price", sa.Numeric(30, 12), nullable=True),
        sa.Column("exit_price", sa.Numeric(30, 12), nullable=True),
        sa.Column("gross_return_bps", sa.Numeric(20, 8), nullable=True),
        sa.Column("net_return_bps", sa.Numeric(20, 8), nullable=True),
        sa.Column("mfe_bps", sa.Numeric(20, 8), nullable=True),
        sa.Column("mae_bps", sa.Numeric(20, 8), nullable=True),
        sa.Column("estimated_cost_bps", sa.Numeric(20, 8), nullable=True),
        sa.Column("exit_reason", sa.String(length=32), nullable=True),
        sa.Column("weights_version", sa.String(length=32), nullable=False),
        sa.Column("feature_version", sa.String(length=32), nullable=False),
        sa.Column("decision_version", sa.String(length=32), nullable=False),
        sa.Column("settlement_version", sa.String(length=32), nullable=False),
        sa.Column(
            "readiness_status",
            sa.String(length=24),
            server_default="research_only",
            nullable=False,
        ),
        sa.Column(
            "calibration_sample_count",
            sa.BigInteger(),
            server_default="0",
            nullable=False,
        ),
        sa.Column("expected_gross_edge_bps", sa.Numeric(20, 8), nullable=True),
        sa.Column(
            "expected_edge_lower_bound_bps", sa.Numeric(20, 8), nullable=True
        ),
        sa.Column("snapshot_complete", sa.Boolean(), nullable=False),
        sa.Column("invalid_reason", sa.String(length=96), nullable=True),
        sa.Column("signal_at", sa.DateTime(), nullable=False),
        sa.Column("due_at", sa.DateTime(), nullable=False),
        sa.Column("exit_at", sa.DateTime(), nullable=True),
        sa.Column("settled_at", sa.DateTime(), nullable=True),
        sa.Column("source_updated_at", sa.DateTime(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["prediction_id"],
            ["ai_monitor_predictions.id"],
            name="fk_ai_monitor_prediction_facts_prediction",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["opportunity_id"],
            ["ai_monitor_opportunities.id"],
            name="fk_ai_monitor_prediction_facts_opportunity",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_ai_monitor_prediction_facts_user",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_ai_monitor_prediction_facts"),
        sa.UniqueConstraint("prediction_id", name="uq_ai_monitor_prediction_facts_prediction"),
        comment="Flattened prediction facts for paged analytics and reconciliation",
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )
    op.create_index(
        "ix_ai_monitor_prediction_facts_user_signal",
        "ai_monitor_prediction_facts",
        ["user_id", "signal_at"],
    )
    op.create_index(
        "ix_ai_monitor_prediction_facts_user_status_signal",
        "ai_monitor_prediction_facts",
        ["user_id", "prediction_status", "signal_at"],
    )
    op.create_index(
        "ix_ai_monitor_prediction_facts_user_direction_result_signal",
        "ai_monitor_prediction_facts",
        ["user_id", "direction", "net_result", "signal_at"],
    )
    op.create_index(
        "ix_ai_monitor_prediction_facts_user_symbol_signal",
        "ai_monitor_prediction_facts",
        ["user_id", "symbol", "signal_at"],
    )

    op.create_table(
        "ai_monitor_opportunity_current",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("opportunity_id", sa.BigInteger(), nullable=False),
        sa.Column("prediction_id", sa.BigInteger(), nullable=True),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("symbol", sa.String(length=32), nullable=False),
        sa.Column("contract_symbol", sa.String(length=32), nullable=False),
        sa.Column("direction", sa.String(length=12), nullable=False),
        sa.Column("timeframe", sa.String(length=8), nullable=False),
        sa.Column("lifecycle_status", sa.String(length=24), nullable=False),
        sa.Column("opportunity_status", sa.String(length=16), nullable=False),
        sa.Column("prediction_status", sa.String(length=16), nullable=True),
        sa.Column("primary_blocker", sa.String(length=191), nullable=True),
        sa.Column("news_score", sa.Numeric(8, 4), nullable=True),
        sa.Column("technical_score", sa.Numeric(8, 4), nullable=True),
        sa.Column("market_flow_score", sa.Numeric(8, 4), nullable=True),
        sa.Column("combined_score", sa.Numeric(8, 4), nullable=True),
        sa.Column("data_coverage", sa.Numeric(8, 4), nullable=True),
        sa.Column(
            "price_source",
            sa.String(length=16),
            server_default="binance",
            nullable=False,
        ),
        sa.Column("entry_price", sa.Numeric(30, 12), nullable=True),
        sa.Column("current_price", sa.Numeric(30, 12), nullable=True),
        sa.Column("discovered_at", sa.DateTime(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("score_updated_at", sa.DateTime(), nullable=False),
        sa.Column("row_version", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["opportunity_id"],
            ["ai_monitor_opportunities.id"],
            name="fk_ai_monitor_opportunity_current_opportunity",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["prediction_id"],
            ["ai_monitor_predictions.id"],
            name="fk_ai_monitor_opportunity_current_prediction",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_ai_monitor_opportunity_current_user",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_ai_monitor_opportunity_current"),
        sa.UniqueConstraint("opportunity_id", name="uq_ai_monitor_opportunity_current_opportunity"),
        sa.UniqueConstraint(
            "user_id",
            "contract_symbol",
            name="uq_ai_monitor_opportunity_current_user_contract",
        ),
        comment="Latest compact state for active AI opportunities",
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )
    op.create_index(
        "ix_ai_monitor_opportunity_current_user_state_time",
        "ai_monitor_opportunity_current",
        ["user_id", "lifecycle_status", "discovered_at"],
    )

    op.create_table(
        "ai_monitor_score_history",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("opportunity_id", sa.BigInteger(), nullable=False),
        sa.Column("gate_decision_id", sa.BigInteger(), nullable=True),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("direction", sa.String(length=12), nullable=False),
        sa.Column("gate_status", sa.String(length=16), nullable=False),
        sa.Column("primary_blocker", sa.String(length=191), nullable=True),
        sa.Column("news_score", sa.Numeric(8, 4), nullable=True),
        sa.Column("technical_score", sa.Numeric(8, 4), nullable=True),
        sa.Column("market_context_score", sa.Numeric(8, 4), nullable=True),
        sa.Column("option_flow_score", sa.Numeric(8, 4), nullable=True),
        sa.Column("gex_score", sa.Numeric(8, 4), nullable=True),
        sa.Column("institutional_flow_score", sa.Numeric(8, 4), nullable=True),
        sa.Column("market_flow_score", sa.Numeric(8, 4), nullable=True),
        sa.Column("combined_score", sa.Numeric(8, 4), nullable=True),
        sa.Column("data_coverage", sa.Numeric(8, 4), nullable=True),
        sa.Column("feature_version", sa.String(length=32), nullable=False),
        sa.Column("weights_version", sa.String(length=32), nullable=False),
        sa.Column("decision_version", sa.String(length=32), nullable=False),
        sa.Column("sampled_at", sa.DateTime(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["opportunity_id"],
            ["ai_monitor_opportunities.id"],
            name="fk_ai_monitor_score_history_opportunity",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["gate_decision_id"],
            ["opportunity_gate_decisions.id"],
            name="fk_ai_monitor_score_history_gate_decision",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_ai_monitor_score_history_user", ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_ai_monitor_score_history"),
        sa.UniqueConstraint(
            "opportunity_id",
            "sampled_at",
            name="uq_ai_monitor_score_history_opportunity_sample",
        ),
        sa.UniqueConstraint("gate_decision_id", name="uq_ai_monitor_score_history_gate_decision"),
        comment="Five-minute opportunity score projection for charting",
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )
    op.create_index(
        "ix_ai_monitor_score_history_opportunity_time",
        "ai_monitor_score_history",
        ["opportunity_id", "sampled_at"],
    )


def downgrade() -> None:
    _require_mysql()
    op.drop_index(
        "ix_ai_monitor_score_history_opportunity_time",
        table_name="ai_monitor_score_history",
    )
    op.drop_table("ai_monitor_score_history")
    op.drop_index(
        "ix_ai_monitor_opportunity_current_user_state_time",
        table_name="ai_monitor_opportunity_current",
    )
    op.drop_table("ai_monitor_opportunity_current")
    op.drop_index(
        "ix_ai_monitor_prediction_facts_user_symbol_signal",
        table_name="ai_monitor_prediction_facts",
    )
    op.drop_index(
        "ix_ai_monitor_prediction_facts_user_direction_result_signal",
        table_name="ai_monitor_prediction_facts",
    )
    op.drop_index(
        "ix_ai_monitor_prediction_facts_user_status_signal",
        table_name="ai_monitor_prediction_facts",
    )
    op.drop_index(
        "ix_ai_monitor_prediction_facts_user_signal",
        table_name="ai_monitor_prediction_facts",
    )
    op.drop_table("ai_monitor_prediction_facts")
