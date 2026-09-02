"""Add Tiger reference data and Martingale TP4 basket foundations.

Revision ID: 0082_martingale_tp4_foundation
Revises: 0081_tradfi_security_mappings
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0082_martingale_tp4_foundation"
down_revision: str | None = "0081_tradfi_security_mappings"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE_OPTIONS = {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"}


def _require_mysql() -> None:
    if op.get_bind().dialect.name not in {"mysql", "mariadb"}:
        raise RuntimeError("QuantDesk migrations require MySQL or MariaDB")


def _replace_strategy_constraints(*, include_basket: bool) -> None:
    template_engines = (
        "'multi_factor', 'ma_cross', 'macd_momentum', 'rsi_reversal', "
        "'bollinger_reversion', 'strategy_dsl'"
    )
    user_engines = f"{template_engines}, 'python_source'"
    template_kinds = "'strategy', 'builtin_strategy'"
    user_kinds = "'full_strategy', 'source_strategy', 'builtin_strategy'"
    if include_basket:
        template_engines += ", 'martingale_tp4'"
        user_engines += ", 'martingale_tp4'"
        template_kinds += ", 'basket_strategy'"
        user_kinds += ", 'basket_strategy'"

    for table in ("strategy_templates", "user_strategies"):
        op.drop_constraint(f"ck_{table}_supported_engine", table, type_="check")
    op.create_check_constraint(
        "ck_strategy_templates_supported_engine",
        "strategy_templates",
        f"engine_key IN ({template_engines})",
    )
    op.create_check_constraint(
        "ck_user_strategies_supported_engine",
        "user_strategies",
        f"engine_key IN ({user_engines})",
    )
    for table in ("strategy_templates", "user_strategies"):
        op.drop_constraint(
            f"ck_{table}_valid_{'template' if table == 'strategy_templates' else 'strategy'}_kind",
            table,
            type_="check",
        )
    op.create_check_constraint(
        "ck_strategy_templates_valid_template_kind",
        "strategy_templates",
        f"template_kind IN ({template_kinds})",
    )
    op.create_check_constraint(
        "ck_user_strategies_valid_strategy_kind",
        "user_strategies",
        f"strategy_kind IN ({user_kinds})",
    )


def upgrade() -> None:
    _require_mysql()
    _replace_strategy_constraints(include_basket=True)

    op.create_table(
        "reference_market_bars",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("asset_class", sa.String(32), nullable=False),
        sa.Column("security_id", sa.BigInteger(), nullable=False),
        sa.Column("symbol", sa.String(32), nullable=False),
        sa.Column("timeframe", sa.String(8), nullable=False),
        sa.Column("trade_session", sa.String(24), nullable=False),
        sa.Column("adjustment", sa.String(16), nullable=False),
        sa.Column("open_time", sa.BigInteger(), nullable=False),
        sa.Column("close_time", sa.BigInteger(), nullable=False),
        sa.Column("open", sa.Numeric(30, 12), nullable=False),
        sa.Column("high", sa.Numeric(30, 12), nullable=False),
        sa.Column("low", sa.Numeric(30, 12), nullable=False),
        sa.Column("close", sa.Numeric(30, 12), nullable=False),
        sa.Column("volume", sa.Numeric(30, 8), nullable=False),
        sa.Column("amount", sa.Numeric(30, 8)),
        sa.Column("received_at", sa.DateTime(), nullable=False),
        sa.Column("source_version", sa.String(32), nullable=False),
        sa.ForeignKeyConstraint(["security_id"], ["securities.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id", name="pk_reference_market_bars"),
        sa.UniqueConstraint(
            "source",
            "asset_class",
            "symbol",
            "timeframe",
            "trade_session",
            "adjustment",
            "open_time",
            name="uq_reference_market_bars_identity",
        ),
        comment="Source-qualified reference-market OHLCV bars",
        **TABLE_OPTIONS,
    )
    op.create_index(
        "ix_reference_market_bars_lookup",
        "reference_market_bars",
        ["source", "symbol", "timeframe", "trade_session", "open_time"],
    )

    op.create_table(
        "reference_market_data_quality",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("symbol", sa.String(32), nullable=False),
        sa.Column("timeframe", sa.String(8), nullable=False),
        sa.Column("trade_session", sa.String(24), nullable=False),
        sa.Column("adjustment", sa.String(16), nullable=False),
        sa.Column("expected_bars", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("actual_bars", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("gap_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("duplicate_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("invalid_ohlc_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("newest_closed_time", sa.BigInteger()),
        sa.Column("age_seconds", sa.BigInteger()),
        sa.Column("completeness_ratio", sa.Numeric(12, 8), nullable=False, server_default="0"),
        sa.Column("status", sa.String(16), nullable=False, server_default="blocked"),
        sa.Column("reason_codes_json", sa.JSON(), nullable=False),
        sa.Column("evaluated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "status IN ('usable', 'blocked')",
            name="ck_reference_market_data_quality_valid_status",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_reference_market_data_quality"),
        sa.UniqueConstraint(
            "source",
            "symbol",
            "timeframe",
            "trade_session",
            "adjustment",
            name="uq_reference_market_data_quality_identity",
        ),
        comment="Auditable quality gate for reference market data",
        **TABLE_OPTIONS,
    )
    op.create_index(
        "ix_reference_market_data_quality_status",
        "reference_market_data_quality",
        ["source", "status", "evaluated_at"],
    )

    op.create_table(
        "strategy_market_data_manifests",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("public_id", sa.String(36), nullable=False),
        sa.Column("signal_source", sa.String(32), nullable=False),
        sa.Column("execution_source", sa.String(32), nullable=False),
        sa.Column("underlying_symbol", sa.String(32), nullable=False),
        sa.Column("contract_symbol", sa.String(32), nullable=False),
        sa.Column("timeframe", sa.String(8), nullable=False),
        sa.Column("trade_session", sa.String(24), nullable=False),
        sa.Column("adjustment", sa.String(16), nullable=False),
        sa.Column("begin_time", sa.BigInteger(), nullable=False),
        sa.Column("end_time", sa.BigInteger(), nullable=False),
        sa.Column("row_count", sa.BigInteger(), nullable=False),
        sa.Column("quality_json", sa.JSON(), nullable=False),
        sa.Column("storage_uri", sa.String(512)),
        sa.Column("content_sha256", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_strategy_market_data_manifests"),
        sa.UniqueConstraint("public_id", name="uq_strategy_market_data_manifests_public_id"),
        sa.UniqueConstraint("content_sha256", name="uq_strategy_market_data_manifests_hash"),
        comment="Immutable market-data lineage for basket strategy research",
        **TABLE_OPTIONS,
    )
    op.create_index(
        "ix_strategy_market_data_manifests_source_symbol",
        "strategy_market_data_manifests",
        ["signal_source", "underlying_symbol", "timeframe", "created_at"],
    )

    op.create_table(
        "strategy_basket_cycles",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("public_id", sa.String(36), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("deployment_id", sa.BigInteger(), nullable=False),
        sa.Column("strategy_revision_id", sa.BigInteger(), nullable=False),
        sa.Column("underlying_symbol", sa.String(32), nullable=False),
        sa.Column("contract_symbol", sa.String(32), nullable=False),
        sa.Column("mode", sa.String(16), nullable=False),
        sa.Column("cycle_seq", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(24), nullable=False),
        sa.Column("box_high", sa.Numeric(30, 12)),
        sa.Column("box_low", sa.Numeric(30, 12)),
        sa.Column("box_time", sa.BigInteger()),
        sa.Column("gross_quantity", sa.Numeric(30, 12), nullable=False, server_default="0"),
        sa.Column("net_quantity", sa.Numeric(30, 12), nullable=False, server_default="0"),
        sa.Column("weighted_cost", sa.Numeric(30, 12)),
        sa.Column("realized_pnl", sa.Numeric(30, 12), nullable=False, server_default="0"),
        sa.Column("unrealized_pnl", sa.Numeric(30, 12), nullable=False, server_default="0"),
        sa.Column("reserved_risk", sa.Numeric(30, 12), nullable=False, server_default="0"),
        sa.Column("max_risk", sa.Numeric(30, 12), nullable=False),
        sa.Column("active_key", sa.String(191)),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("opened_at", sa.DateTime()),
        sa.Column("closed_at", sa.DateTime()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "state IN ('arming', 'opening', 'open', 'adding', 'exiting', 'closed', "
            "'recovery_required', 'failed_closed')",
            name="ck_strategy_basket_cycles_valid_state",
        ),
        sa.CheckConstraint(
            "mode IN ('auto', 'recovery', 'grid')",
            name="ck_strategy_basket_cycles_valid_mode",
        ),
        sa.ForeignKeyConstraint(
            ["deployment_id", "user_id"],
            ["strategy_deployments.id", "strategy_deployments.user_id"],
            name="fk_strategy_basket_cycles_deployment_tenant",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["strategy_revision_id", "user_id"],
            ["strategy_revisions.id", "strategy_revisions.user_id"],
            name="fk_strategy_basket_cycles_revision_tenant",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_strategy_basket_cycles"),
        sa.UniqueConstraint("public_id", name="uq_strategy_basket_cycles_public_id"),
        sa.UniqueConstraint("active_key", name="uq_strategy_basket_cycles_active_key"),
        sa.UniqueConstraint("id", "user_id", name="uq_strategy_basket_cycles_id_user_id"),
        comment="Durable multi-leg basket strategy cycle",
        **TABLE_OPTIONS,
    )
    op.create_index(
        "ix_strategy_basket_cycles_deployment_state",
        "strategy_basket_cycles",
        ["deployment_id", "state", "updated_at"],
    )

    op.create_table(
        "strategy_basket_legs",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("cycle_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("leg_index", sa.Integer(), nullable=False),
        sa.Column("action", sa.String(24), nullable=False),
        sa.Column("direction", sa.String(8), nullable=False),
        sa.Column("position_side", sa.String(8), nullable=False),
        sa.Column("planned_quantity", sa.Numeric(30, 12), nullable=False),
        sa.Column("filled_quantity", sa.Numeric(30, 12), nullable=False, server_default="0"),
        sa.Column("trigger_price", sa.Numeric(30, 12)),
        sa.Column("planned_price", sa.Numeric(30, 12)),
        sa.Column("average_fill_price", sa.Numeric(30, 12)),
        sa.Column("intent_id", sa.String(64)),
        sa.Column("exchange_order_id", sa.String(64)),
        sa.Column("client_order_id", sa.String(64)),
        sa.Column("fee", sa.Numeric(30, 12), nullable=False, server_default="0"),
        sa.Column("funding", sa.Numeric(30, 12), nullable=False, server_default="0"),
        sa.Column("realized_pnl", sa.Numeric(30, 12), nullable=False, server_default="0"),
        sa.Column("state", sa.String(24), nullable=False),
        sa.Column("reason_code", sa.String(64)),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("submitted_at", sa.DateTime()),
        sa.Column("filled_at", sa.DateTime()),
        sa.CheckConstraint(
            "action IN ('open', 'add', 'hedge', 'overlap_close', 'reduce', 'exit')",
            name="ck_strategy_basket_legs_valid_action",
        ),
        sa.CheckConstraint(
            "direction IN ('long', 'short')",
            name="ck_strategy_basket_legs_valid_direction",
        ),
        sa.CheckConstraint(
            "state IN ('planned', 'approved', 'submitted', 'partially_filled', "
            "'filled', 'cancelled', 'rejected', 'failed')",
            name="ck_strategy_basket_legs_valid_state",
        ),
        sa.ForeignKeyConstraint(
            ["cycle_id", "user_id"],
            ["strategy_basket_cycles.id", "strategy_basket_cycles.user_id"],
            name="fk_strategy_basket_legs_cycle_tenant",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_strategy_basket_legs"),
        sa.UniqueConstraint(
            "cycle_id", "leg_index", "action", name="uq_strategy_basket_legs_action"
        ),
        sa.UniqueConstraint("client_order_id", name="uq_strategy_basket_legs_client_order"),
        comment="One planned or executed leg in a basket cycle",
        **TABLE_OPTIONS,
    )
    op.create_index(
        "ix_strategy_basket_legs_cycle_state",
        "strategy_basket_legs",
        ["cycle_id", "state", "created_at"],
    )

    op.create_table(
        "strategy_basket_events",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("public_id", sa.String(36), nullable=False),
        sa.Column("cycle_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("sequence_no", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("actor_type", sa.String(24), nullable=False),
        sa.Column("reason_code", sa.String(64)),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["cycle_id", "user_id"],
            ["strategy_basket_cycles.id", "strategy_basket_cycles.user_id"],
            name="fk_strategy_basket_events_cycle_tenant",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_strategy_basket_events"),
        sa.UniqueConstraint("public_id", name="uq_strategy_basket_events_public_id"),
        sa.UniqueConstraint("cycle_id", "sequence_no", name="uq_strategy_basket_events_sequence"),
        comment="Append-only basket strategy event ledger",
        **TABLE_OPTIONS,
    )
    op.create_index(
        "ix_strategy_basket_events_cycle_time",
        "strategy_basket_events",
        ["cycle_id", "occurred_at"],
    )

    # Use the existing security master as the identity bridge.  Mapping review
    # remains explicit; no unverified contract becomes live-tradable here.
    op.execute(
        """
        INSERT IGNORE INTO security_symbol_mappings (
            security_id, source, source_symbol, normalized_symbol,
            mapping_status, mapping_method, notes, source_status,
            contract_type, underlying_type, onboard_date_ms,
            monitor_enabled, strategy_enabled, live_trading_enabled,
            source_metadata_json, first_seen_at, last_seen_at, created_at, updated_at
        )
        SELECT security_row.id, 'tiger_openapi', security_row.symbol,
               security_row.symbol,
               CASE
                   WHEN security_row.verification_status='VERIFIED'
                    AND binance_mapping.mapping_status IN ('VERIFIED', 'MANUAL')
                   THEN 'VERIFIED' ELSE 'REVIEW_REQUIRED'
               END,
               'security_master_symbol', NULL, 'ACTIVE', NULL, 'US_EQUITY', NULL,
               TRUE,
               CASE
                   WHEN security_row.verification_status='VERIFIED'
                    AND binance_mapping.mapping_status IN ('VERIFIED', 'MANUAL')
                   THEN TRUE ELSE FALSE
               END,
               FALSE,
               JSON_OBJECT('provider', 'Tiger Open API', 'backfilled', TRUE),
               UTC_TIMESTAMP(), UTC_TIMESTAMP(), UTC_TIMESTAMP(), UTC_TIMESTAMP()
        FROM securities AS security_row
        JOIN security_symbol_mappings AS binance_mapping
          ON binance_mapping.security_id=security_row.id
         AND binance_mapping.source='binance_tradfi'
        WHERE security_row.exchange='US'
          AND binance_mapping.underlying_type='EQUITY'
        """
    )


def downgrade() -> None:
    _require_mysql()
    op.execute(
        "DELETE FROM security_symbol_mappings "
        "WHERE source='tiger_openapi' "
        "AND JSON_EXTRACT(source_metadata_json, '$.backfilled') = TRUE"
    )
    for index_name, table_name in (
        ("ix_strategy_basket_events_cycle_time", "strategy_basket_events"),
        ("ix_strategy_basket_legs_cycle_state", "strategy_basket_legs"),
        ("ix_strategy_basket_cycles_deployment_state", "strategy_basket_cycles"),
        (
            "ix_strategy_market_data_manifests_source_symbol",
            "strategy_market_data_manifests",
        ),
        ("ix_reference_market_data_quality_status", "reference_market_data_quality"),
        ("ix_reference_market_bars_lookup", "reference_market_bars"),
    ):
        op.drop_index(index_name, table_name=table_name)
    for table_name in (
        "strategy_basket_events",
        "strategy_basket_legs",
        "strategy_basket_cycles",
        "strategy_market_data_manifests",
        "reference_market_data_quality",
        "reference_market_bars",
    ):
        op.drop_table(table_name)
    _replace_strategy_constraints(include_basket=False)
