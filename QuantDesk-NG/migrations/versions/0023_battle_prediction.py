"""Add explainable long-short battle predictions and forward labels.

Revision ID: 0023_battle_prediction
Revises: 0022_contract_price_move_counts
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0023_battle_prediction"
down_revision: str | None = "0022_contract_price_move_counts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _require_mysql() -> None:
    if op.get_bind().dialect.name not in {"mysql", "mariadb"}:
        raise RuntimeError("QuantDesk migrations require MySQL or MariaDB")


def upgrade() -> None:
    _require_mysql()

    op.create_table(
        "prediction_model_versions",
        sa.Column("model_key", sa.String(64), primary_key=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(24), nullable=False),
        sa.Column("feature_schema_version", sa.Integer(), nullable=False),
        sa.Column("training_window_json", sa.JSON(), nullable=False),
        sa.Column("metrics_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "state IN ('heuristic','shadow','calibrated','retired')",
            name="ck_prediction_model_versions_state",
        ),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )
    op.execute(
        """INSERT INTO prediction_model_versions(
               model_key,version,state,feature_schema_version,training_window_json,
               metrics_json,created_at,updated_at)
           VALUES('battle-ensemble',1,'heuristic',1,JSON_OBJECT(),JSON_OBJECT(),
                  CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"""
    )

    op.create_table(
        "market_positioning_snapshots",
        sa.Column("symbol", sa.String(32), nullable=False),
        sa.Column("snapshot_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("open_interest", sa.Numeric(30, 12)),
        sa.Column("mark_price", sa.Numeric(30, 12)),
        sa.Column("global_long_short_ratio", sa.Numeric(20, 10)),
        sa.Column("long_account_ratio", sa.Numeric(20, 10)),
        sa.Column("short_account_ratio", sa.Numeric(20, 10)),
        sa.Column("taker_buy_sell_ratio", sa.Numeric(20, 10)),
        sa.Column("taker_buy_volume", sa.Numeric(30, 12)),
        sa.Column("taker_sell_volume", sa.Numeric(30, 12)),
        sa.Column("source_timestamp_ms", sa.BigInteger()),
        sa.Column("quality_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("symbol", "snapshot_at_ms"),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )
    op.create_index(
        "ix_market_positioning_snapshots_time",
        "market_positioning_snapshots",
        ["snapshot_at_ms"],
    )

    op.create_table(
        "prediction_feature_snapshots",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("symbol", sa.String(32), nullable=False),
        sa.Column("as_of_ms", sa.BigInteger(), nullable=False),
        sa.Column("feature_schema_version", sa.Integer(), nullable=False),
        sa.Column("features_json", sa.JSON(), nullable=False),
        sa.Column("quality_score", sa.Numeric(10, 6), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("symbol", "as_of_ms", name="uq_prediction_feature_symbol_time"),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )
    op.create_index(
        "ix_prediction_feature_snapshots_time",
        "prediction_feature_snapshots",
        ["as_of_ms"],
    )

    op.create_table(
        "battle_predictions",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("public_id", sa.String(36), nullable=False),
        sa.Column("feature_snapshot_id", sa.BigInteger(), nullable=False),
        sa.Column("symbol", sa.String(32), nullable=False),
        sa.Column("horizon_seconds", sa.Integer(), nullable=False),
        sa.Column("current_marker", sa.SmallInteger()),
        sa.Column("prediction_state", sa.String(24), nullable=False),
        sa.Column("result", sa.String(16), nullable=False),
        sa.Column("battle_score", sa.Numeric(10, 6), nullable=False),
        sa.Column("long_probability", sa.Numeric(10, 8), nullable=False),
        sa.Column("short_probability", sa.Numeric(10, 8), nullable=False),
        sa.Column("neutral_probability", sa.Numeric(10, 8), nullable=False),
        sa.Column("confidence_score", sa.Numeric(10, 8), nullable=False),
        sa.Column("confidence_label", sa.String(16), nullable=False),
        sa.Column("gross_edge_bps", sa.Numeric(20, 8)),
        sa.Column("entry_price", sa.Numeric(30, 12), nullable=False),
        sa.Column("spread_bps", sa.Numeric(20, 8)),
        sa.Column("target_bps", sa.Numeric(20, 8), nullable=False),
        sa.Column("stop_bps", sa.Numeric(20, 8), nullable=False),
        sa.Column("reason_codes_json", sa.JSON(), nullable=False),
        sa.Column("components_json", sa.JSON(), nullable=False),
        sa.Column("model_key", sa.String(64), nullable=False),
        sa.Column("model_version", sa.Integer(), nullable=False),
        sa.Column("predicted_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("valid_until_ms", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["feature_snapshot_id"],
            ["prediction_feature_snapshots.id"],
            name="fk_battle_predictions_feature_snapshot",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["model_key"],
            ["prediction_model_versions.model_key"],
            name="fk_battle_predictions_model",
        ),
        sa.UniqueConstraint("public_id", name="uq_battle_predictions_public_id"),
        sa.UniqueConstraint(
            "symbol", "horizon_seconds", "current_marker",
            name="uq_battle_predictions_current",
        ),
        sa.CheckConstraint(
            "prediction_state IN ('heuristic','calibrated','data_insufficient')",
            name="ck_battle_predictions_state",
        ),
        sa.CheckConstraint(
            "result IN ('long','short','neutral')",
            name="ck_battle_predictions_result",
        ),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )
    op.create_index(
        "ix_battle_predictions_current_rank",
        "battle_predictions",
        ["current_marker", "horizon_seconds", "confidence_score"],
    )

    op.create_table(
        "prediction_outcomes",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("prediction_id", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("actual_result", sa.String(16)),
        sa.Column("exit_price", sa.Numeric(30, 12)),
        sa.Column("raw_return_bps", sa.Numeric(20, 8)),
        sa.Column("directional_return_bps", sa.Numeric(20, 8)),
        sa.Column("max_favorable_bps", sa.Numeric(20, 8)),
        sa.Column("max_adverse_bps", sa.Numeric(20, 8)),
        sa.Column("hit_result", sa.String(16)),
        sa.Column("cost_bps", sa.Numeric(20, 8), nullable=False, server_default="0"),
        sa.Column("due_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("completed_at_ms", sa.BigInteger()),
        sa.Column("label_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["prediction_id"],
            ["battle_predictions.id"],
            name="fk_prediction_outcomes_prediction",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("prediction_id", name="uq_prediction_outcomes_prediction"),
        sa.CheckConstraint(
            "status IN ('pending','completed','unavailable')",
            name="ck_prediction_outcomes_status",
        ),
        sa.CheckConstraint(
            "actual_result IS NULL OR actual_result IN ('long','short','neutral')",
            name="ck_prediction_outcomes_result",
        ),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )
    op.create_index(
        "ix_prediction_outcomes_status_due",
        "prediction_outcomes",
        ["status", "due_at_ms"],
    )


def downgrade() -> None:
    _require_mysql()
    op.drop_table("prediction_outcomes")
    op.drop_index("ix_battle_predictions_current_rank", table_name="battle_predictions")
    op.drop_table("battle_predictions")
    op.drop_index("ix_prediction_feature_snapshots_time", table_name="prediction_feature_snapshots")
    op.drop_table("prediction_feature_snapshots")
    op.drop_index("ix_market_positioning_snapshots_time", table_name="market_positioning_snapshots")
    op.drop_table("market_positioning_snapshots")
    op.drop_table("prediction_model_versions")
