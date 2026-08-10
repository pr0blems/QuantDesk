"""Add AI monitor symbol selection and virtual prediction outcomes.

Revision ID: 0038_ai_monitor_predictions
Revises: 0037_ai_monitor_workspace
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0038_ai_monitor_predictions"
down_revision: str | None = "0037_ai_monitor_workspace"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

BIGINT = sa.BigInteger()


def _require_mysql() -> None:
    if op.get_bind().dialect.name not in {"mysql", "mariadb"}:
        raise RuntimeError("QuantDesk migrations require MySQL or MariaDB")


def upgrade() -> None:
    _require_mysql()
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    config_columns = {
        column["name"] for column in inspector.get_columns("ai_monitor_configs")
    }
    if "monitor_symbols_json" not in config_columns:
        op.add_column(
            "ai_monitor_configs",
            sa.Column(
                "monitor_symbols_json",
                sa.JSON(),
                nullable=True,
                comment="机会扫描品种白名单；空数组表示全部可用品种",
            ),
        )
    op.execute(
        sa.text(
            "UPDATE ai_monitor_configs SET monitor_symbols_json=JSON_ARRAY() "
            "WHERE monitor_symbols_json IS NULL"
        )
    )
    op.alter_column(
        "ai_monitor_configs",
        "monitor_symbols_json",
        existing_type=sa.JSON(),
        nullable=False,
        existing_comment="机会扫描品种白名单；空数组表示全部可用品种",
    )

    op.create_table(
        "ai_monitor_predictions",
        sa.Column("id", BIGINT, autoincrement=True, nullable=False, comment="AI 预测主键"),
        sa.Column("public_id", sa.String(36), nullable=False, comment="AI 预测公开 UUID"),
        sa.Column("user_id", BIGINT, nullable=False, comment="所属用户 ID"),
        sa.Column("opportunity_id", BIGINT, nullable=False, comment="触发该预测的 AI 机会"),
        sa.Column("symbol", sa.String(32), nullable=False, comment="标准美股代码"),
        sa.Column("contract_symbol", sa.String(32), nullable=False, comment="TradFi 合约代码"),
        sa.Column("direction", sa.String(12), nullable=False, comment="预测方向"),
        sa.Column("timeframe", sa.String(8), nullable=False, comment="预测观察周期"),
        sa.Column("status", sa.String(16), nullable=False, comment="预测结算状态"),
        sa.Column("result", sa.String(16), comment="到期结果"),
        sa.Column("confidence_score", sa.Numeric(8, 4), nullable=False, comment="组合置信评分"),
        sa.Column("entry_price", sa.Numeric(30, 12), comment="预测参考入场价"),
        sa.Column("exit_price", sa.Numeric(30, 12), comment="预测到期参考价"),
        sa.Column("raw_return_bps", sa.Numeric(20, 8), comment="原始涨跌基点"),
        sa.Column("directional_return_bps", sa.Numeric(20, 8), comment="方向收益基点"),
        sa.Column("evidence_json", sa.JSON(), nullable=False, comment="生成预测时的证据快照"),
        sa.Column("predicted_at", sa.DateTime(), nullable=False, comment="预测生成时间（UTC）"),
        sa.Column("due_at", sa.DateTime(), nullable=False, comment="预测到期时间（UTC）"),
        sa.Column("completed_at", sa.DateTime(), comment="预测结算时间（UTC）"),
        sa.Column("created_at", sa.DateTime(), nullable=False, comment="创建时间（UTC）"),
        sa.Column("updated_at", sa.DateTime(), nullable=False, comment="最后更新时间（UTC）"),
        sa.CheckConstraint(
            "direction IN ('long', 'short')",
            name="valid_direction",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'completed', 'unavailable')",
            name="valid_status",
        ),
        sa.CheckConstraint(
            "result IS NULL OR result IN ('win', 'loss', 'flat')",
            name="valid_result",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_ai_mon_pred_user",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["opportunity_id"],
            ["ai_monitor_opportunities.id"],
            name="fk_ai_mon_pred_opportunity",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_ai_monitor_predictions"),
        sa.UniqueConstraint("public_id", name="uq_ai_monitor_predictions_public_id"),
        sa.UniqueConstraint(
            "opportunity_id",
            name="uq_ai_monitor_predictions_opportunity_id",
        ),
        comment="AI 监控机会生成的虚拟预测及到期结果，不产生任何交易订单",
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )
    op.create_index(
        "ix_ai_monitor_predictions_user_status_due",
        "ai_monitor_predictions",
        ["user_id", "status", "due_at"],
    )
    op.create_index(
        "ix_ai_monitor_predictions_user_predicted",
        "ai_monitor_predictions",
        ["user_id", "predicted_at"],
    )


def downgrade() -> None:
    _require_mysql()
    op.drop_index(
        "ix_ai_monitor_predictions_user_predicted",
        table_name="ai_monitor_predictions",
    )
    op.drop_index(
        "ix_ai_monitor_predictions_user_status_due",
        table_name="ai_monitor_predictions",
    )
    op.drop_table("ai_monitor_predictions")
    op.drop_column("ai_monitor_configs", "monitor_symbols_json")
