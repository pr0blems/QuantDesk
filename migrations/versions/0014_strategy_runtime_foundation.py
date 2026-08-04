"""Add full strategy DSL, deployments, signals, features, and opportunities.

Revision ID: 0014_strategy_runtime
Revises: 0013_news_dedup_hash
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0014_strategy_runtime"
down_revision: str | None = "0013_news_dedup_hash"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

BIGINT = sa.BigInteger()


def _require_mysql() -> None:
    if op.get_bind().dialect.name not in {"mysql", "mariadb"}:
        raise RuntimeError("QuantDesk migrations require MySQL or MariaDB")


def upgrade() -> None:
    _require_mysql()
    _extend_strategy_templates()
    _extend_user_strategies()
    _extend_strategy_revisions()
    _create_market_feature_snapshots()
    _create_market_opportunities()
    _create_strategy_deployments()
    _backfill_paper_deployments()
    _create_strategy_signals()


def _extend_strategy_templates() -> None:
    op.drop_constraint(
        "ck_strategy_templates_supported_engine", "strategy_templates", type_="check"
    )
    op.create_check_constraint(
        "ck_strategy_templates_supported_engine",
        "strategy_templates",
        "engine_key IN ('multi_factor', 'ma_cross', 'macd_momentum', "
        "'rsi_reversal', 'bollinger_reversion', 'strategy_dsl')",
    )
    op.add_column(
        "strategy_templates",
        sa.Column(
            "template_kind",
            sa.String(24),
            nullable=False,
            server_default="legacy_signal",
            comment="模板类型：完整策略 strategy 或旧版指标信号 legacy_signal",
        ),
    )
    op.add_column(
        "strategy_templates",
        sa.Column("spec_schema_version", sa.Integer(), comment="完整策略 DSL 结构版本"),
    )
    op.add_column(
        "strategy_templates",
        sa.Column("spec_json", sa.JSON(), comment="完整且受约束的系统策略 DSL 定义"),
    )
    op.add_column(
        "strategy_templates",
        sa.Column(
            "implementation_version",
            sa.String(32),
            nullable=False,
            server_default="legacy_v1",
            comment="策略求值器实现版本",
        ),
    )
    op.add_column(
        "strategy_templates",
        sa.Column("deprecated_at", sa.DateTime(), comment="模板停止用于新策略的时间（UTC）"),
    )
    op.create_check_constraint(
        "ck_strategy_templates_valid_template_kind",
        "strategy_templates",
        "template_kind IN ('strategy', 'legacy_signal')",
    )


def _extend_user_strategies() -> None:
    op.drop_constraint("ck_user_strategies_supported_engine", "user_strategies", type_="check")
    op.create_check_constraint(
        "ck_user_strategies_supported_engine",
        "user_strategies",
        "engine_key IN ('multi_factor', 'ma_cross', 'macd_momentum', "
        "'rsi_reversal', 'bollinger_reversion', 'strategy_dsl')",
    )
    op.add_column(
        "user_strategies",
        sa.Column(
            "strategy_kind",
            sa.String(24),
            nullable=False,
            server_default="legacy_signal",
            comment="策略类型：完整策略 full_strategy 或旧版指标信号 legacy_signal",
        ),
    )
    op.add_column(
        "user_strategies",
        sa.Column(
            "lifecycle_status",
            sa.String(16),
            nullable=False,
            server_default="published",
            comment="策略生命周期：draft、published 或 retired",
        ),
    )
    op.add_column(
        "user_strategies",
        sa.Column("spec_schema_version", sa.Integer(), comment="完整策略 DSL 结构版本"),
    )
    op.add_column(
        "user_strategies",
        sa.Column("spec_json", sa.JSON(), comment="用户当前完整策略 DSL 定义"),
    )
    op.add_column(
        "user_strategies",
        sa.Column("spec_hash", sa.String(64), comment="规范化策略 DSL 的 SHA-256 哈希"),
    )
    op.add_column(
        "user_strategies",
        sa.Column(
            "risk_level",
            sa.String(16),
            nullable=False,
            server_default="medium",
            comment="策略风险等级：low、medium 或 high",
        ),
    )
    op.create_check_constraint(
        "ck_user_strategies_valid_strategy_kind",
        "user_strategies",
        "strategy_kind IN ('full_strategy', 'legacy_signal')",
    )
    op.create_check_constraint(
        "ck_user_strategies_valid_lifecycle_status",
        "user_strategies",
        "lifecycle_status IN ('draft', 'published', 'retired')",
    )
    op.create_check_constraint(
        "ck_user_strategies_valid_risk_level",
        "user_strategies",
        "risk_level IN ('low', 'medium', 'high')",
    )


def _extend_strategy_revisions() -> None:
    op.add_column(
        "strategy_revisions",
        sa.Column("spec_schema_version", sa.Integer(), comment="该修订采用的策略 DSL 结构版本"),
    )
    op.add_column(
        "strategy_revisions",
        sa.Column("spec_json", sa.JSON(), comment="该修订不可变的完整策略 DSL 定义"),
    )
    op.add_column(
        "strategy_revisions",
        sa.Column("spec_hash", sa.String(64), comment="该修订策略 DSL 的 SHA-256 哈希"),
    )
    op.add_column(
        "strategy_revisions",
        sa.Column(
            "validation_json", sa.JSON(), comment="发布时的静态校验、数据依赖和风险提示"
        ),
    )
    op.add_column(
        "strategy_revisions",
        sa.Column("published_at", sa.DateTime(), comment="该修订正式发布时间（UTC）"),
    )
    op.create_unique_constraint(
        "uq_strategy_revisions_id_user_id", "strategy_revisions", ["id", "user_id"]
    )


def _create_market_feature_snapshots() -> None:
    op.create_table(
        "market_feature_snapshots",
        sa.Column("id", BIGINT, autoincrement=True, nullable=False, comment="市场特征快照主键"),
        sa.Column("symbol", sa.String(32), nullable=False, comment="合约代码"),
        sa.Column("timeframe", sa.String(8), nullable=False, comment="K 线周期"),
        sa.Column(
            "bar_open_time", BIGINT, nullable=False, comment="对应已收盘 K 线开盘时间戳"
        ),
        sa.Column(
            "feature_set_key", sa.String(64), nullable=False, comment="标准特征集合稳定标识"
        ),
        sa.Column(
            "feature_set_version", sa.Integer(), nullable=False, comment="标准特征集合实现版本"
        ),
        sa.Column(
            "params_hash", sa.String(64), nullable=False, comment="指标参数规范化 SHA-256 哈希"
        ),
        sa.Column("values_json", sa.JSON(), nullable=False, comment="指标和派生市场特征值"),
        sa.Column(
            "quality_json", sa.JSON(), nullable=False, comment="行情缺口、陈旧、异常和可用性信息"
        ),
        sa.Column("computed_at", sa.DateTime(), nullable=False, comment="特征计算完成时间（UTC）"),
        sa.PrimaryKeyConstraint("id", name="pk_market_feature_snapshots"),
        sa.UniqueConstraint(
            "symbol",
            "timeframe",
            "bar_open_time",
            "feature_set_key",
            "feature_set_version",
            "params_hash",
            name="uq_market_feature_snapshots_identity",
        ),
        comment="系统共享的已收盘行情指标与数据质量快照",
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )
    op.create_index(
        "ix_market_feature_snapshots_symbol_tf_time",
        "market_feature_snapshots",
        ["symbol", "timeframe", "bar_open_time"],
    )


def _create_market_opportunities() -> None:
    op.create_table(
        "market_opportunities",
        sa.Column("id", BIGINT, autoincrement=True, nullable=False, comment="市场机会主键"),
        sa.Column("public_id", sa.String(36), nullable=False, comment="市场机会公开 UUID"),
        sa.Column("scanner_key", sa.String(64), nullable=False, comment="机会扫描器稳定标识"),
        sa.Column("scanner_version", sa.Integer(), nullable=False, comment="机会扫描器实现版本"),
        sa.Column("symbol", sa.String(32), nullable=False, comment="合约代码"),
        sa.Column("primary_timeframe", sa.String(8), nullable=False, comment="机会主周期"),
        sa.Column(
            "direction", sa.String(12), nullable=False, comment="机会方向：long、short 或 neutral"
        ),
        sa.Column("status", sa.String(16), nullable=False, comment="机会生命周期状态"),
        sa.Column(
            "quality_score", sa.Numeric(8, 4), nullable=False, comment="机会质量分，仅用于排序"
        ),
        sa.Column(
            "detected_bar_time", BIGINT, nullable=False, comment="首次发现机会的已收盘 K 线时间"
        ),
        sa.Column("expires_bar_time", BIGINT, nullable=False, comment="机会失效的 K 线时间"),
        sa.Column("evidence_json", sa.JSON(), nullable=False, comment="机会条件、指标值和解释证据"),
        sa.Column("dedup_key", sa.String(191), nullable=False, comment="机会事件幂等去重键"),
        sa.Column("created_at", sa.DateTime(), nullable=False, comment="机会创建时间（UTC）"),
        sa.Column("updated_at", sa.DateTime(), nullable=False, comment="机会最后更新时间（UTC）"),
        sa.CheckConstraint(
            "direction IN ('long', 'short', 'neutral')",
            name="ck_market_opportunities_valid_direction",
        ),
        sa.CheckConstraint(
            "status IN ('detected', 'watching', 'confirmed', 'expired', 'rejected', 'consumed')",
            name="ck_market_opportunities_valid_status",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_market_opportunities"),
        sa.UniqueConstraint("public_id", name="uq_market_opportunities_public_id"),
        sa.UniqueConstraint("dedup_key", name="uq_market_opportunities_dedup_key"),
        comment="系统共享的可解释市场机会及生命周期",
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )
    op.create_index(
        "ix_market_opportunities_status_quality",
        "market_opportunities",
        ["status", "quality_score"],
    )
    op.create_index(
        "ix_market_opportunities_symbol_time",
        "market_opportunities",
        ["symbol", "detected_bar_time"],
    )


def _create_strategy_deployments() -> None:
    op.create_table(
        "strategy_deployments",
        sa.Column("id", BIGINT, autoincrement=True, nullable=False, comment="策略部署内部主键"),
        sa.Column("public_id", sa.String(36), nullable=False, comment="策略部署公开 UUID"),
        sa.Column("user_id", BIGINT, nullable=False, comment="所属用户 ID"),
        sa.Column("strategy_id", BIGINT, nullable=False, comment="所属用户策略内部 ID"),
        sa.Column(
            "strategy_revision_id", BIGINT, nullable=False, comment="部署固定的不可变策略修订 ID"
        ),
        sa.Column(
            "mode", sa.String(16), nullable=False, comment="部署模式：回测、模拟、影子或实盘"
        ),
        sa.Column("target_account_id", BIGINT, comment="模拟盘或实盘目标账户内部 ID"),
        sa.Column("name", sa.String(100), nullable=False, comment="策略部署显示名称"),
        sa.Column("status", sa.String(16), nullable=False, comment="策略部署运行状态"),
        sa.Column("universe_override_json", sa.JSON(), comment="部署级交易标的范围覆盖"),
        sa.Column("risk_override_json", sa.JSON(), comment="部署级仅收紧风险参数覆盖"),
        sa.Column(
            "runtime_state_json", sa.JSON(), nullable=False, comment="机会、冷却和幂等等运行状态"
        ),
        sa.Column("last_evaluated_bar_time", BIGINT, comment="最后完成求值的 K 线时间"),
        sa.Column("last_error_code", sa.String(64), comment="最后一次脱敏运行错误代码"),
        sa.Column("started_at", sa.DateTime(), comment="策略部署启动时间（UTC）"),
        sa.Column("created_at", sa.DateTime(), nullable=False, comment="策略部署创建时间（UTC）"),
        sa.Column("updated_at", sa.DateTime(), nullable=False, comment="策略部署最后更新时间（UTC）"),
        sa.CheckConstraint(
            "mode IN ('backtest', 'paper', 'shadow', 'live')",
            name="ck_strategy_deployments_valid_mode",
        ),
        sa.CheckConstraint(
            "status IN ('created', 'running', 'paused', 'stopped', 'error')",
            name="ck_strategy_deployments_valid_status",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_strategy_deployments_user", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["strategy_id", "user_id"],
            ["user_strategies.id", "user_strategies.user_id"],
            name="fk_strategy_deployments_strategy_tenant",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["strategy_revision_id", "user_id"],
            ["strategy_revisions.id", "strategy_revisions.user_id"],
            name="fk_strategy_deployments_revision_tenant",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_strategy_deployments"),
        sa.UniqueConstraint("public_id", name="uq_strategy_deployments_public_id"),
        sa.UniqueConstraint("id", "user_id", name="uq_strategy_deployments_id_user_id"),
        comment="用户将固定策略修订绑定到回测、模拟、影子或实盘的部署实例",
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )
    op.create_index(
        "ix_strategy_deployments_user_status",
        "strategy_deployments",
        ["user_id", "status", "updated_at"],
    )


def _backfill_paper_deployments() -> None:
    """Represent every existing paper account as one tenant-owned deployment."""

    op.execute(
        sa.text(
            """
            INSERT INTO strategy_deployments(
                public_id,user_id,strategy_id,strategy_revision_id,mode,
                target_account_id,name,status,runtime_state_json,started_at,
                created_at,updated_at
            )
            SELECT UUID(),pa.user_id,pa.strategy_id,sr.id,'paper',pa.id,pa.name,
                   CASE pa.status
                       WHEN 'active' THEN 'running'
                       WHEN 'paused' THEN 'paused'
                       ELSE 'stopped'
                   END,
                   JSON_OBJECT(),pa.started_at,pa.created_at,pa.updated_at
            FROM paper_accounts pa
            JOIN strategy_revisions sr
              ON sr.user_strategy_id=pa.strategy_id
             AND sr.user_id=pa.user_id
             AND sr.version=COALESCE(
                 CAST(JSON_UNQUOTE(JSON_EXTRACT(pa.strategy_snapshot_json,'$.version')) AS UNSIGNED),
                 (
                     SELECT MAX(sr_latest.version)
                     FROM strategy_revisions sr_latest
                     WHERE sr_latest.user_strategy_id=pa.strategy_id
                       AND sr_latest.user_id=pa.user_id
                 )
             )
            """
        )
    )


def _create_strategy_signals() -> None:
    op.create_table(
        "strategy_signals",
        sa.Column("id", BIGINT, autoincrement=True, nullable=False, comment="策略信号主键"),
        sa.Column("public_id", sa.String(36), nullable=False, comment="策略信号公开 UUID"),
        sa.Column("user_id", BIGINT, nullable=False, comment="所属用户 ID"),
        sa.Column("deployment_id", BIGINT, nullable=False, comment="产生信号的策略部署 ID"),
        sa.Column(
            "strategy_revision_id", BIGINT, nullable=False, comment="产生信号的不可变策略修订 ID"
        ),
        sa.Column("opportunity_id", BIGINT, comment="关联的公共市场机会 ID"),
        sa.Column("symbol", sa.String(32), nullable=False, comment="合约代码"),
        sa.Column("timeframe", sa.String(8), nullable=False, comment="信号触发周期"),
        sa.Column("signal_bar_time", BIGINT, nullable=False, comment="信号对应的已收盘 K 线时间"),
        sa.Column("decision", sa.String(24), nullable=False, comment="结构化策略决策代码"),
        sa.Column("confidence", sa.Numeric(8, 4), comment="规则置信度，仅用于排序和解释"),
        sa.Column("status", sa.String(24), nullable=False, comment="信号审批与执行状态"),
        sa.Column("valid_until", sa.DateTime(), comment="策略信号有效期（UTC）"),
        sa.Column(
            "reason_codes_json", sa.JSON(), nullable=False, comment="稳定且可检索的决策原因代码"
        ),
        sa.Column("evidence_json", sa.JSON(), nullable=False, comment="参与策略决策的指标和条件证据"),
        sa.Column("risk_decision_json", sa.JSON(), comment="账户与组合风控审批结果"),
        sa.Column(
            "idempotency_key",
            sa.String(191),
            nullable=False,
            comment="部署、标的、修订、K 线和决策组成的幂等键",
        ),
        sa.Column("created_at", sa.DateTime(), nullable=False, comment="策略信号创建时间（UTC）"),
        sa.CheckConstraint(
            "decision IN ('LONG_ENTRY', 'SHORT_ENTRY', 'EXIT', 'HOLD', 'SKIP')",
            name="ck_strategy_signals_valid_decision",
        ),
        sa.CheckConstraint(
            "status IN ('proposed', 'risk_rejected', 'approved', 'expired', 'executed')",
            name="ck_strategy_signals_valid_status",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_strategy_signals_user", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["deployment_id", "user_id"],
            ["strategy_deployments.id", "strategy_deployments.user_id"],
            name="fk_strategy_signals_deployment_tenant",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["strategy_revision_id", "user_id"],
            ["strategy_revisions.id", "strategy_revisions.user_id"],
            name="fk_strategy_signals_revision_tenant",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["opportunity_id"],
            ["market_opportunities.id"],
            name="fk_strategy_signals_opportunity",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_strategy_signals"),
        sa.UniqueConstraint("public_id", name="uq_strategy_signals_public_id"),
        sa.UniqueConstraint("idempotency_key", name="uq_strategy_signals_idempotency_key"),
        comment="用户策略求值产生的可解释信号及风控审批结果",
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )
    op.create_index(
        "ix_strategy_signals_user_created", "strategy_signals", ["user_id", "created_at"]
    )
    op.create_index(
        "ix_strategy_signals_deployment_bar",
        "strategy_signals",
        ["deployment_id", "signal_bar_time"],
    )


def downgrade() -> None:
    _require_mysql()
    op.drop_index("ix_strategy_signals_deployment_bar", table_name="strategy_signals")
    op.drop_index("ix_strategy_signals_user_created", table_name="strategy_signals")
    op.drop_table("strategy_signals")
    op.drop_index("ix_strategy_deployments_user_status", table_name="strategy_deployments")
    op.drop_table("strategy_deployments")
    op.drop_index("ix_market_opportunities_symbol_time", table_name="market_opportunities")
    op.drop_index("ix_market_opportunities_status_quality", table_name="market_opportunities")
    op.drop_table("market_opportunities")
    op.drop_index(
        "ix_market_feature_snapshots_symbol_tf_time", table_name="market_feature_snapshots"
    )
    op.drop_table("market_feature_snapshots")

    op.drop_constraint(
        "uq_strategy_revisions_id_user_id", "strategy_revisions", type_="unique"
    )
    for column in (
        "published_at",
        "validation_json",
        "spec_hash",
        "spec_json",
        "spec_schema_version",
    ):
        op.drop_column("strategy_revisions", column)

    for constraint in (
        "ck_user_strategies_valid_risk_level",
        "ck_user_strategies_valid_lifecycle_status",
        "ck_user_strategies_valid_strategy_kind",
    ):
        op.drop_constraint(constraint, "user_strategies", type_="check")
    for column in (
        "risk_level",
        "spec_hash",
        "spec_json",
        "spec_schema_version",
        "lifecycle_status",
        "strategy_kind",
    ):
        op.drop_column("user_strategies", column)
    op.drop_constraint("ck_user_strategies_supported_engine", "user_strategies", type_="check")
    op.create_check_constraint(
        "ck_user_strategies_supported_engine",
        "user_strategies",
        "engine_key IN ('multi_factor', 'ma_cross', 'macd_momentum', "
        "'rsi_reversal', 'bollinger_reversion')",
    )

    op.drop_constraint(
        "ck_strategy_templates_valid_template_kind", "strategy_templates", type_="check"
    )
    for column in (
        "deprecated_at",
        "implementation_version",
        "spec_json",
        "spec_schema_version",
        "template_kind",
    ):
        op.drop_column("strategy_templates", column)
    op.drop_constraint(
        "ck_strategy_templates_supported_engine", "strategy_templates", type_="check"
    )
    op.create_check_constraint(
        "ck_strategy_templates_supported_engine",
        "strategy_templates",
        "engine_key IN ('multi_factor', 'ma_cross', 'macd_momentum', "
        "'rsi_reversal', 'bollinger_reversion')",
    )
