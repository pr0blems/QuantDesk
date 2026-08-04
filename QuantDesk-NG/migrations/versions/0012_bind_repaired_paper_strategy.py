"""Bind migrated paper accounts to the repaired system-default strategy.

Revision ID: 0012_bind_paper_strategy
Revises: 0011_repair_paper_strategy
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0012_bind_paper_strategy"
down_revision: str | None = "0011_repair_paper_strategy"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    if op.get_bind().dialect.name not in {"mysql", "mariadb"}:
        raise RuntimeError("QuantDesk migrations require MySQL")
    op.execute(
        sa.text(
            """
            UPDATE paper_accounts pa
            JOIN user_strategies us
              ON us.user_id=pa.user_id AND us.status='active'
            JOIN strategy_templates st
              ON st.id=us.source_template_id
             AND st.template_key='paper_multifactor_atr_v1'
            SET pa.strategy_id=us.id,
                pa.strategy_snapshot_json=JSON_OBJECT(
                    'strategy_id',us.id,'public_id',us.public_id,'name',us.name,
                    'engine_key',us.engine_key,'version',us.version,
                    'parameters',us.parameters_json,'risk_defaults',us.risk_defaults_json
                ),
                pa.config_json=JSON_MERGE_PATCH(
                    COALESCE(pa.config_json,JSON_OBJECT()),
                    JSON_OBJECT(
                        'leverage',JSON_EXTRACT(us.risk_defaults_json,'$.leverage'),
                        'max_positions',15,
                        'position_size_pct',JSON_EXTRACT(
                            us.risk_defaults_json,'$.position_size_pct'
                        ),
                        'margin_cap',0.8,
                        'fee_bps',JSON_EXTRACT(us.risk_defaults_json,'$.fee_bps'),
                        'slippage_bps',JSON_EXTRACT(us.risk_defaults_json,'$.slippage_bps'),
                        'stop_loss_pct',JSON_EXTRACT(
                            us.risk_defaults_json,'$.stop_loss_pct'
                        ),
                        'take_profit_pct',JSON_EXTRACT(
                            us.risk_defaults_json,'$.take_profit_pct'
                        ),
                        'max_holding_bars',JSON_EXTRACT(
                            us.risk_defaults_json,'$.max_holding_bars'
                        )
                    )
                ),
                pa.updated_at=CURRENT_TIMESTAMP
            WHERE pa.name='已迁移模拟盘'
            """
        )
    )


def downgrade() -> None:
    # The prior strategy binding cannot be reconstructed reliably after migration.
    pass
