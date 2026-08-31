"""Stop production paper accounts from generating legacy score signals.

Revision ID: 0076_disable_legacy_paper
Revises: 0075_paper_reconciliation
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0076_disable_legacy_paper"
down_revision: str | None = "0075_paper_reconciliation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _require_mysql() -> None:
    if op.get_bind().dialect.name not in {"mysql", "mariadb"}:
        raise RuntimeError("QuantDesk migrations require MySQL or MariaDB")


def upgrade() -> None:
    _require_mysql()
    op.execute(
        """
        UPDATE paper_accounts
        SET config_json = JSON_SET(
            COALESCE(config_json, JSON_OBJECT()),
            '$.legacy_previous_signal_mode', 'legacy_score_v1',
            '$.legacy_signal_migrated_at',
                DATE_FORMAT(UTC_TIMESTAMP(6), '%Y-%m-%dT%H:%i:%s.%fZ'),
            '$.legacy_signal_cutoff_revision', '0076_disable_legacy_paper',
            '$.signal_mode', 'strategy_event_v2'
        )
        WHERE JSON_UNQUOTE(JSON_EXTRACT(config_json, '$.signal_mode')) =
              'legacy_score_v1'
        """
    )


def downgrade() -> None:
    _require_mysql()
    op.execute(
        """
        UPDATE paper_accounts
        SET config_json = JSON_REMOVE(
            JSON_SET(
                config_json,
                '$.signal_mode',
                COALESCE(
                    JSON_UNQUOTE(
                        JSON_EXTRACT(config_json, '$.legacy_previous_signal_mode')
                    ),
                    'legacy_score_v1'
                )
            ),
            '$.legacy_previous_signal_mode',
            '$.legacy_signal_migrated_at',
            '$.legacy_signal_cutoff_revision'
        )
        WHERE JSON_UNQUOTE(
            JSON_EXTRACT(config_json, '$.legacy_signal_cutoff_revision')
        ) = '0076_disable_legacy_paper'
        """
    )
