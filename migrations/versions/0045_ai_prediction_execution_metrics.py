"""Add immutable AI prediction scores and cost-aware execution metrics.

Revision ID: 0045_ai_prediction_metrics
Revises: 0044_news_ai_call_audit
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0045_ai_prediction_metrics"
down_revision: str | None = "0044_news_ai_call_audit"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _require_mysql() -> None:
    if op.get_bind().dialect.name not in {"mysql", "mariadb"}:
        raise RuntimeError("QuantDesk migrations require MySQL or MariaDB")


def upgrade() -> None:
    _require_mysql()
    op.add_column(
        "ai_monitor_predictions",
        sa.Column(
            "signal_news_score",
            sa.Numeric(8, 4),
            nullable=True,
            comment="生成预测时不可变的新闻评分",
        ),
    )
    op.add_column(
        "ai_monitor_predictions",
        sa.Column(
            "signal_indicator_score",
            sa.Numeric(8, 4),
            nullable=True,
            comment="生成预测时不可变的技术指标评分",
        ),
    )
    op.add_column(
        "ai_monitor_predictions",
        sa.Column(
            "estimated_cost_bps",
            sa.Numeric(12, 4),
            nullable=False,
            server_default="16.0000",
            comment="预测持有期估算总成本基点",
        ),
    )
    op.add_column(
        "ai_monitor_predictions",
        sa.Column(
            "net_directional_return_bps",
            sa.Numeric(20, 8),
            nullable=True,
            comment="扣除估算成本后的方向收益基点",
        ),
    )
    op.add_column(
        "ai_monitor_predictions",
        sa.Column(
            "net_result",
            sa.String(16),
            nullable=True,
            comment="成本后结果：win、loss 或 flat",
        ),
    )
    op.add_column(
        "ai_monitor_predictions",
        sa.Column(
            "max_favorable_bps",
            sa.Numeric(20, 8),
            nullable=True,
            comment="预测持有期最大有利波动基点（MFE）",
        ),
    )
    op.add_column(
        "ai_monitor_predictions",
        sa.Column(
            "max_adverse_bps",
            sa.Numeric(20, 8),
            nullable=True,
            comment="预测持有期最大不利波动基点（MAE）",
        ),
    )
    op.add_column(
        "ai_monitor_predictions",
        sa.Column(
            "settlement_version",
            sa.String(32),
            nullable=False,
            server_default="gross_v1",
            comment="预测结算与成本模型版本",
        ),
    )
    op.create_check_constraint(
        "valid_net_result",
        "ai_monitor_predictions",
        "net_result IS NULL OR net_result IN ('win', 'loss', 'flat')",
    )

    op.execute(
        sa.text(
            """
            UPDATE ai_monitor_predictions p
            JOIN ai_monitor_opportunities o ON o.id=p.opportunity_id AND o.user_id=p.user_id
            SET p.signal_indicator_score=COALESCE(
                    100 * CAST(JSON_UNQUOTE(JSON_EXTRACT(p.evidence_json, '$.matched_indicator_count')) AS DECIMAL(12,4))
                        / NULLIF(CAST(JSON_UNQUOTE(JSON_EXTRACT(p.evidence_json, '$.required_indicator_count')) AS DECIMAL(12,4)), 0),
                    o.indicator_score
                )
            WHERE p.signal_indicator_score IS NULL
            """
        )
    )
    op.execute(
        sa.text(
            """
            UPDATE ai_monitor_predictions p
            JOIN ai_monitor_opportunities o ON o.id=p.opportunity_id AND o.user_id=p.user_id
            SET p.signal_news_score=COALESCE(
                    GREATEST(0, LEAST(100, (p.confidence_score - p.signal_indicator_score * 0.45) / 0.55)),
                    o.news_score
                )
            WHERE p.signal_news_score IS NULL
            """
        )
    )
    op.execute(
        sa.text(
            """
            UPDATE ai_monitor_predictions
            SET estimated_cost_bps=16.0 + GREATEST(
                    TIMESTAMPDIFF(SECOND, predicted_at, due_at), 0
                ) / 28800.0,
                net_directional_return_bps=directional_return_bps - (
                    16.0 + GREATEST(TIMESTAMPDIFF(SECOND, predicted_at, due_at), 0) / 28800.0
                ),
                net_result=CASE
                    WHEN directional_return_bps - (
                        16.0 + GREATEST(TIMESTAMPDIFF(SECOND, predicted_at, due_at), 0) / 28800.0
                    ) > 0 THEN 'win'
                    WHEN directional_return_bps - (
                        16.0 + GREATEST(TIMESTAMPDIFF(SECOND, predicted_at, due_at), 0) / 28800.0
                    ) < 0 THEN 'loss'
                    ELSE 'flat'
                END,
                settlement_version='cost_v2_backfill'
            WHERE status='completed' AND directional_return_bps IS NOT NULL
            """
        )
    )


def downgrade() -> None:
    _require_mysql()
    op.drop_constraint(
        "valid_net_result",
        "ai_monitor_predictions",
        type_="check",
    )
    for column in (
        "settlement_version",
        "max_adverse_bps",
        "max_favorable_bps",
        "net_result",
        "net_directional_return_bps",
        "estimated_cost_bps",
        "signal_indicator_score",
        "signal_news_score",
    ):
        op.drop_column("ai_monitor_predictions", column)
