"""Restore the original paper score-entry mode for existing built-in strategies.

Revision ID: 0033_legacy_paper_mode
Revises: 0032_execution_controls
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0033_legacy_paper_mode"
down_revision: str | None = "0032_execution_controls"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _require_mysql() -> None:
    if op.get_bind().dialect.name not in {"mysql", "mariadb"}:
        raise RuntimeError("QuantDesk migrations require MySQL or MariaDB")


def upgrade() -> None:
    _require_mysql()
    op.execute(
        sa.text(
            """
            UPDATE paper_accounts
            SET config_json = JSON_SET(
                COALESCE(config_json, JSON_OBJECT()),
                '$.signal_mode',
                'legacy_score_v1'
            )
            WHERE COALESCE(
                JSON_UNQUOTE(JSON_EXTRACT(strategy_snapshot_json, '$.strategy_kind')),
                'legacy'
            ) <> 'full_strategy'
              AND JSON_EXTRACT(config_json, '$.signal_mode') IS NULL
            """
        )
    )


def downgrade() -> None:
    _require_mysql()
    op.execute(
        sa.text(
            """
            UPDATE paper_accounts
            SET config_json = JSON_REMOVE(config_json, '$.signal_mode')
            WHERE JSON_UNQUOTE(JSON_EXTRACT(config_json, '$.signal_mode'))
                  = 'legacy_score_v1'
            """
        )
    )
