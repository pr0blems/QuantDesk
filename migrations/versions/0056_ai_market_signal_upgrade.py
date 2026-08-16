"""Add auditable market features, risk events, and immutable opportunity snapshots.

Revision ID: 0056_ai_market_signal_upgrade
Revises: 0055_news_ai_judgment_basis
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0056_ai_market_signal_upgrade"
down_revision: str | None = "0055_news_ai_judgment_basis"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _require_mysql() -> None:
    if op.get_bind().dialect.name not in {"mysql", "mariadb"}:
        raise RuntimeError("QuantDesk migrations require MySQL or MariaDB")


def upgrade() -> None:
    _require_mysql()
    op.create_table(
        "market_stream_events",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("channel", sa.String(length=48), nullable=False),
        sa.Column("symbol", sa.String(length=32), nullable=True),
        sa.Column("event_time", sa.DateTime(), nullable=False),
        sa.Column("received_at", sa.DateTime(), nullable=False),
        sa.Column("sequence_key", sa.String(length=96), nullable=True),
        sa.Column("dedup_key", sa.String(length=191), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("schema_version", sa.String(length=32), nullable=False),
        sa.Column("quality_status", sa.String(length=16), nullable=False),
        sa.CheckConstraint(
            "quality_status IN ('valid', 'delayed', 'stale', 'duplicate', 'invalid')",
            name="ck_market_stream_events_valid_quality_status",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_market_stream_events"),
        sa.UniqueConstraint(
            "provider",
            "channel",
            "dedup_key",
            name="uq_market_stream_event_identity",
        ),
        comment="Normalized market-data events used for deterministic replay",
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )
    op.create_index(
        "ix_market_stream_events_symbol_time",
        "market_stream_events",
        ["symbol", "event_time"],
    )
    op.create_index(
        "ix_market_stream_events_channel_time",
        "market_stream_events",
        ["channel", "event_time"],
    )

    op.create_table(
        "realtime_market_feature_snapshots",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("symbol", sa.String(length=32), nullable=False),
        sa.Column("bucket_at", sa.DateTime(), nullable=False),
        sa.Column("market_session", sa.String(length=16), nullable=False),
        sa.Column("last_price", sa.Numeric(30, 12), nullable=True),
        sa.Column("bid", sa.Numeric(30, 12), nullable=True),
        sa.Column("ask", sa.Numeric(30, 12), nullable=True),
        sa.Column("spread_bps", sa.Numeric(20, 8), nullable=True),
        sa.Column("quote_age_ms", sa.BigInteger(), nullable=True),
        sa.Column("size_imbalance", sa.Numeric(20, 8), nullable=True),
        sa.Column("quote_snapshot_json", sa.JSON(), nullable=True),
        sa.Column("option_flow_snapshot_json", sa.JSON(), nullable=True),
        sa.Column("gex_snapshot_json", sa.JSON(), nullable=True),
        sa.Column("institutional_flow_snapshot_json", sa.JSON(), nullable=True),
        sa.Column("halt_status", sa.String(length=16), nullable=False),
        sa.Column("data_coverage", sa.Numeric(5, 4), nullable=False),
        sa.Column("stale_fields_json", sa.JSON(), nullable=False),
        sa.Column("quality_json", sa.JSON(), nullable=False),
        sa.Column("feature_version", sa.String(length=32), nullable=False),
        sa.Column("captured_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "market_session IN ('premarket', 'regular', 'postmarket', 'closed', 'unknown')",
            name="ck_realtime_market_feature_snapshots_valid_market_session",
        ),
        sa.CheckConstraint(
            "halt_status IN ('clear', 'halted', 'cooldown', 'unknown')",
            name="ck_realtime_market_feature_snapshots_valid_halt_status",
        ),
        sa.CheckConstraint(
            "data_coverage BETWEEN 0 AND 1",
            name="ck_realtime_market_feature_snapshots_valid_data_coverage",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_realtime_market_feature_snapshots"),
        sa.UniqueConstraint(
            "symbol",
            "bucket_at",
            "feature_version",
            name="uq_realtime_market_feature_identity",
        ),
        comment="Normalized real-time market features for AI signal gating",
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )
    op.create_index(
        "ix_realtime_market_features_symbol_time",
        "realtime_market_feature_snapshots",
        ["symbol", "bucket_at"],
    )

    op.create_table(
        "market_risk_events",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("public_id", sa.String(length=36), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("provider_event_id", sa.String(length=96), nullable=True),
        sa.Column("event_type", sa.String(length=32), nullable=False),
        sa.Column("event_name", sa.String(length=191), nullable=False),
        sa.Column("symbol", sa.String(length=32), nullable=True),
        sa.Column("scheduled_at", sa.DateTime(), nullable=False),
        sa.Column("actual_at", sa.DateTime(), nullable=True),
        sa.Column("risk_level", sa.String(length=16), nullable=False),
        sa.Column("blocking_before_seconds", sa.Integer(), nullable=False),
        sa.Column("blocking_after_seconds", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("dedup_key", sa.String(length=191), nullable=False),
        sa.Column("source_payload_json", sa.JSON(), nullable=True),
        sa.Column("source_updated_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "risk_level IN ('normal', 'medium', 'high', 'critical')",
            name="ck_market_risk_events_valid_risk_level",
        ),
        sa.CheckConstraint(
            "status IN ('scheduled', 'active', 'completed', 'cancelled')",
            name="ck_market_risk_events_valid_event_status",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_market_risk_events"),
        sa.UniqueConstraint("public_id", name="uq_market_risk_events_public_id"),
        sa.UniqueConstraint(
            "provider", "dedup_key", name="uq_market_risk_event_identity"
        ),
        comment="Macro, earnings and halt risk windows for AI entry gating",
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )
    op.create_index(
        "ix_market_risk_events_schedule",
        "market_risk_events",
        ["status", "scheduled_at"],
    )
    op.create_index(
        "ix_market_risk_events_symbol_schedule",
        "market_risk_events",
        ["symbol", "scheduled_at"],
    )

    op.create_table(
        "opportunity_market_snapshots",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("opportunity_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("market_feature_snapshot_id", sa.BigInteger(), nullable=True),
        sa.Column("captured_at", sa.DateTime(), nullable=False),
        sa.Column("quote_snapshot_json", sa.JSON(), nullable=False),
        sa.Column("option_flow_snapshot_json", sa.JSON(), nullable=False),
        sa.Column("gex_snapshot_json", sa.JSON(), nullable=False),
        sa.Column("institutional_flow_snapshot_json", sa.JSON(), nullable=False),
        sa.Column("macro_snapshot_json", sa.JSON(), nullable=False),
        sa.Column("risk_gate_snapshot_json", sa.JSON(), nullable=False),
        sa.Column("score_components_json", sa.JSON(), nullable=False),
        sa.Column("data_quality_json", sa.JSON(), nullable=False),
        sa.Column("weights_version", sa.String(length=32), nullable=False),
        sa.Column("feature_version", sa.String(length=32), nullable=False),
        sa.Column("decision_version", sa.String(length=32), nullable=False),
        sa.ForeignKeyConstraint(
            ["market_feature_snapshot_id"],
            ["realtime_market_feature_snapshots.id"],
            name="fk_opportunity_market_snapshot_feature",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["opportunity_id", "user_id"],
            ["ai_monitor_opportunities.id", "ai_monitor_opportunities.user_id"],
            name="fk_opportunity_market_snapshot_user",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_opportunity_market_snapshots"),
        sa.UniqueConstraint(
            "opportunity_id", name="uq_opportunity_market_snapshot_opportunity"
        ),
        comment="Immutable signal-time evidence for opportunity history and replay",
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )
    op.create_index(
        "ix_opportunity_market_snapshots_user_time",
        "opportunity_market_snapshots",
        ["user_id", "captured_at"],
    )


def downgrade() -> None:
    _require_mysql()
    op.drop_index(
        "ix_opportunity_market_snapshots_user_time",
        table_name="opportunity_market_snapshots",
    )
    op.drop_table("opportunity_market_snapshots")
    op.drop_index(
        "ix_market_risk_events_symbol_schedule", table_name="market_risk_events"
    )
    op.drop_index("ix_market_risk_events_schedule", table_name="market_risk_events")
    op.drop_table("market_risk_events")
    op.drop_index(
        "ix_realtime_market_features_symbol_time",
        table_name="realtime_market_feature_snapshots",
    )
    op.drop_table("realtime_market_feature_snapshots")
    op.drop_index(
        "ix_market_stream_events_channel_time", table_name="market_stream_events"
    )
    op.drop_index(
        "ix_market_stream_events_symbol_time", table_name="market_stream_events"
    )
    op.drop_table("market_stream_events")
