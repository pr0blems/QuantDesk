"""Add auditable multi-round news intelligence verification.

Revision ID: 0024_news_intelligence
Revises: 0023_battle_prediction
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0024_news_intelligence"
down_revision: str | None = "0023_battle_prediction"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


TABLE_OPTIONS = {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"}


def _require_mysql() -> None:
    if op.get_bind().dialect.name not in {"mysql", "mariadb"}:
        raise RuntimeError("QuantDesk migrations require MySQL or MariaDB")


def upgrade() -> None:
    _require_mysql()

    op.execute(
        "UPDATE prediction_model_versions SET feature_schema_version=2,updated_at=CURRENT_TIMESTAMP "
        "WHERE model_key='battle-ensemble'"
    )

    op.create_table(
        "news_event_clusters",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("public_id", sa.String(36), nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("canonical_title", sa.Text(), nullable=False),
        sa.Column("event_type", sa.String(32), nullable=False),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("first_seen_at", sa.BigInteger(), nullable=False),
        sa.Column("last_seen_at", sa.BigInteger(), nullable=False),
        sa.Column("independent_origins", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("contradiction_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("quality_score", sa.Numeric(10, 8), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("public_id", name="uq_news_event_clusters_public_id"),
        sa.UniqueConstraint("fingerprint", name="uq_news_event_clusters_fingerprint"),
        sa.CheckConstraint(
            "state IN ('DETECTED','PROVENANCE_OK','FACT_VERIFIED','IMPACT_ASSESSED',"
            "'CHALLENGED','VALIDATED','MARKET_CONFIRMED','REFERENCE_ELIGIBLE',"
            "'DISPUTED','REFUTED','REVISED','DATA_INSUFFICIENT','EXPIRED')",
            name="ck_news_event_clusters_state",
        ),
        comment="去重后的新闻事件及其当前验证状态",
        **TABLE_OPTIONS,
    )
    op.create_index(
        "ix_news_event_clusters_state_time",
        "news_event_clusters",
        ["state", "last_seen_at"],
    )

    op.create_table(
        "news_documents",
        sa.Column("news_id", sa.String(255), primary_key=True),
        sa.Column("event_id", sa.BigInteger(), nullable=False),
        sa.Column("canonical_url", sa.Text()),
        sa.Column("source_domain", sa.String(191)),
        sa.Column("origin_key", sa.String(191), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("source_tier", sa.String(1), nullable=False),
        sa.Column("published_at", sa.BigInteger(), nullable=False),
        sa.Column("ingested_at", sa.BigInteger(), nullable=False),
        sa.Column("provenance_score", sa.Numeric(10, 8), nullable=False),
        sa.Column("lineage_json", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(
            ["news_id"], ["news.id"], name="fk_news_documents_news", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["event_id"],
            ["news_event_clusters.id"],
            name="fk_news_documents_event",
            ondelete="CASCADE",
        ),
        comment="新闻原始文档、来源血缘和事件归属",
        **TABLE_OPTIONS,
    )
    op.create_index("ix_news_documents_event_origin", "news_documents", ["event_id", "origin_key"])

    op.create_table(
        "news_claims",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("event_id", sa.BigInteger(), nullable=False),
        sa.Column("news_id", sa.String(255), nullable=False),
        sa.Column("claim_hash", sa.String(64), nullable=False),
        sa.Column("subject_text", sa.Text(), nullable=False),
        sa.Column("predicate_text", sa.String(191), nullable=False),
        sa.Column("object_text", sa.Text()),
        sa.Column("numeric_value", sa.Numeric(30, 10)),
        sa.Column("unit", sa.String(32)),
        sa.Column("modality", sa.String(24), nullable=False),
        sa.Column("evidence_text", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["event_id"],
            ["news_event_clusters.id"],
            name="fk_news_claims_event",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["news_id"], ["news.id"], name="fk_news_claims_news", ondelete="CASCADE"
        ),
        sa.UniqueConstraint("news_id", "claim_hash", name="uq_news_claims_document_hash"),
        comment="从原文证据中提取的可验证原子事实",
        **TABLE_OPTIONS,
    )
    op.create_index("ix_news_claims_event", "news_claims", ["event_id"])

    op.create_table(
        "news_event_entities",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("event_id", sa.BigInteger(), nullable=False),
        sa.Column("symbol", sa.String(32), nullable=False),
        sa.Column("entity_name", sa.String(191), nullable=False),
        sa.Column("relationship_type", sa.String(32), nullable=False),
        sa.Column("directness", sa.Numeric(10, 8), nullable=False),
        sa.Column("direction", sa.String(16), nullable=False),
        sa.Column("impact_score", sa.Numeric(10, 8), nullable=False),
        sa.Column("impact_confidence", sa.Numeric(10, 8), nullable=False),
        sa.Column("horizons_json", sa.JSON(), nullable=False),
        sa.Column("evidence_json", sa.JSON(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["event_id"],
            ["news_event_clusters.id"],
            name="fk_news_event_entities_event",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("event_id", "symbol", name="uq_news_event_entities_symbol"),
        sa.CheckConstraint(
            "direction IN ('long','short','neutral','conflicted')",
            name="ck_news_event_entities_direction",
        ),
        comment="事件对具体合约的独立方向、强度和周期判断",
        **TABLE_OPTIONS,
    )
    op.create_index(
        "ix_news_event_entities_symbol", "news_event_entities", ["symbol", "updated_at"]
    )

    op.create_table(
        "news_assessment_rounds",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("event_id", sa.BigInteger(), nullable=False),
        sa.Column("round_no", sa.Integer(), nullable=False),
        sa.Column("stage", sa.String(32), nullable=False),
        sa.Column("result_state", sa.String(32), nullable=False),
        sa.Column("passed", sa.Boolean(), nullable=False),
        sa.Column("score", sa.Numeric(10, 8), nullable=False),
        sa.Column("reasons_json", sa.JSON(), nullable=False),
        sa.Column("evidence_json", sa.JSON(), nullable=False),
        sa.Column("assessor", sa.String(64), nullable=False),
        sa.Column("assessor_version", sa.Integer(), nullable=False),
        sa.Column("assessed_at", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["event_id"],
            ["news_event_clusters.id"],
            name="fk_news_assessment_rounds_event",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("event_id", "round_no", name="uq_news_assessment_rounds_number"),
        comment="每轮验证的输入证据、分数和状态转移审计",
        **TABLE_OPTIONS,
    )
    op.create_index(
        "ix_news_assessment_rounds_event_time",
        "news_assessment_rounds",
        ["event_id", "assessed_at"],
    )

    op.create_table(
        "news_market_snapshots",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("event_id", sa.BigInteger(), nullable=False),
        sa.Column("symbol", sa.String(32), nullable=False),
        sa.Column("stage", sa.String(16), nullable=False),
        sa.Column("as_of_ms", sa.BigInteger(), nullable=False),
        sa.Column("price", sa.Numeric(30, 12)),
        sa.Column("return_bps", sa.Numeric(20, 8)),
        sa.Column("open_interest", sa.Numeric(30, 12)),
        sa.Column("taker_buy_sell_ratio", sa.Numeric(20, 10)),
        sa.Column("spread_bps", sa.Numeric(20, 8)),
        sa.Column("quality_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["event_id"],
            ["news_event_clusters.id"],
            name="fk_news_market_snapshots_event",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "event_id", "symbol", "stage", name="uq_news_market_snapshots_stage"
        ),
        comment="事件发生后冻结的币安市场确认快照",
        **TABLE_OPTIONS,
    )
    op.create_index(
        "ix_news_market_snapshots_symbol_time",
        "news_market_snapshots",
        ["symbol", "as_of_ms"],
    )

    op.create_table(
        "news_decisions",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("event_id", sa.BigInteger(), nullable=False),
        sa.Column("symbol", sa.String(32), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("current_marker", sa.SmallInteger()),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("direction", sa.String(16), nullable=False),
        sa.Column("truth_confidence", sa.Numeric(10, 8), nullable=False),
        sa.Column("impact_confidence", sa.Numeric(10, 8), nullable=False),
        sa.Column("market_confirmation", sa.String(16), nullable=False),
        sa.Column("reference_status", sa.String(16), nullable=False),
        sa.Column("valid_until", sa.BigInteger(), nullable=False),
        sa.Column("counterevidence_json", sa.JSON(), nullable=False),
        sa.Column("invalidation_json", sa.JSON(), nullable=False),
        sa.Column("rationale_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["event_id"],
            ["news_event_clusters.id"],
            name="fk_news_decisions_event",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("event_id", "symbol", "revision", name="uq_news_decisions_revision"),
        sa.UniqueConstraint(
            "event_id", "symbol", "current_marker", name="uq_news_decisions_current"
        ),
        sa.CheckConstraint(
            "reference_status IN ('display_only','observe','risk_only','eligible','blocked')",
            name="ck_news_decisions_reference_status",
        ),
        comment="按标的版本化保存的最终新闻裁决，默认不参与交易",
        **TABLE_OPTIONS,
    )
    op.create_index(
        "ix_news_decisions_symbol_current",
        "news_decisions",
        ["symbol", "current_marker", "valid_until"],
    )


def downgrade() -> None:
    _require_mysql()
    op.drop_index("ix_news_decisions_symbol_current", table_name="news_decisions")
    op.drop_table("news_decisions")
    op.drop_index("ix_news_market_snapshots_symbol_time", table_name="news_market_snapshots")
    op.drop_table("news_market_snapshots")
    op.drop_index("ix_news_assessment_rounds_event_time", table_name="news_assessment_rounds")
    op.drop_table("news_assessment_rounds")
    op.drop_index("ix_news_event_entities_symbol", table_name="news_event_entities")
    op.drop_table("news_event_entities")
    op.drop_index("ix_news_claims_event", table_name="news_claims")
    op.drop_table("news_claims")
    op.drop_index("ix_news_documents_event_origin", table_name="news_documents")
    op.drop_table("news_documents")
    op.drop_index("ix_news_event_clusters_state_time", table_name="news_event_clusters")
    op.drop_table("news_event_clusters")
    op.execute(
        "UPDATE prediction_model_versions SET feature_schema_version=1,updated_at=CURRENT_TIMESTAMP "
        "WHERE model_key='battle-ensemble'"
    )
