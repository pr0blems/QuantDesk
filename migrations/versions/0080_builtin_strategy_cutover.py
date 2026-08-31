"""Replace the retired legacy strategy runtime marker with a first-class built-in kind.

Revision ID: 0080_builtin_strategy_cutover
Revises: 0079_position_snapshot_facts

The five deterministic indicator engines remain unchanged.  This migration is
an in-place metadata cutover: identifiers, revisions, deployments, executions,
backtests and position facts keep their existing primary/foreign keys.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0080_builtin_strategy_cutover"
down_revision: str | None = "0079_position_snapshot_facts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _require_mysql() -> None:
    if op.get_bind().dialect.name not in {"mysql", "mariadb"}:
        raise RuntimeError("QuantDesk migrations require MySQL or MariaDB")


def _replace_kind_constraints(*, include_legacy: bool) -> None:
    template_values = "'strategy', 'builtin_strategy'"
    strategy_values = "'full_strategy', 'source_strategy', 'builtin_strategy'"
    if include_legacy:
        template_values += ", 'legacy_signal'"
        strategy_values += ", 'legacy_signal'"

    op.drop_constraint(
        "ck_strategy_templates_valid_template_kind",
        "strategy_templates",
        type_="check",
    )
    op.create_check_constraint(
        "ck_strategy_templates_valid_template_kind",
        "strategy_templates",
        f"template_kind IN ({template_values})",
    )
    op.drop_constraint(
        "ck_user_strategies_valid_strategy_kind",
        "user_strategies",
        type_="check",
    )
    op.create_check_constraint(
        "ck_user_strategies_valid_strategy_kind",
        "user_strategies",
        f"strategy_kind IN ({strategy_values})",
    )


def upgrade() -> None:
    _require_mysql()
    _replace_kind_constraints(include_legacy=True)

    op.execute(
        """
        UPDATE strategy_templates
        SET template_kind='builtin_strategy',
            implementation_version=IF(
                implementation_version='legacy_v1',
                'builtin_v1',
                implementation_version
            )
        WHERE template_kind='legacy_signal'
        """
    )
    op.execute(
        """
        UPDATE user_strategies
        SET strategy_kind='builtin_strategy'
        WHERE strategy_kind='legacy_signal'
        """
    )
    op.execute(
        """
        UPDATE strategy_revisions
        SET snapshot_json=JSON_SET(
                snapshot_json,
                '$.strategy_kind',
                'builtin_strategy'
            )
        WHERE JSON_UNQUOTE(JSON_EXTRACT(snapshot_json, '$.strategy_kind')) =
              'legacy_signal'
        """
    )
    op.execute(
        """
        UPDATE strategy_revisions
        SET validation_json=JSON_SET(
            JSON_REMOVE(validation_json, '$.legacy'),
            '$.builtin',
            TRUE
        )
        WHERE JSON_EXTRACT(validation_json, '$.legacy') IS NOT NULL
        """
    )
    op.execute(
        """
        UPDATE paper_accounts
        SET strategy_snapshot_json=JSON_SET(
            strategy_snapshot_json,
            '$.strategy_kind',
            'builtin_strategy'
        )
        WHERE JSON_UNQUOTE(
            JSON_EXTRACT(strategy_snapshot_json, '$.strategy_kind')
        )='legacy_signal'
        """
    )
    op.execute(
        """
        UPDATE live_trading_accounts
        SET strategy_snapshot_json=JSON_SET(
            strategy_snapshot_json,
            '$.strategy_kind',
            'builtin_strategy'
        )
        WHERE JSON_UNQUOTE(
            JSON_EXTRACT(strategy_snapshot_json, '$.strategy_kind')
        )='legacy_signal'
        """
    )
    op.execute(
        """
        UPDATE strategy_validation_runs
        SET report_json=JSON_SET(
            JSON_REMOVE(report_json, '$.legacy'),
            '$.builtin',
            TRUE
        )
        WHERE JSON_EXTRACT(report_json, '$.legacy') IS NOT NULL
        """
    )
    op.execute(
        """
        UPDATE paper_accounts
        SET config_json=JSON_SET(
            config_json,
            '$.signal_mode',
            'strategy_event_v2'
        )
        WHERE JSON_UNQUOTE(JSON_EXTRACT(config_json, '$.signal_mode')) =
              'legacy_score_v1'
        """
    )
    op.execute(
        """
        UPDATE paper_accounts
        SET config_json=JSON_REMOVE(
            config_json,
            '$.legacy_previous_signal_mode',
            '$.legacy_signal_migrated_at',
            '$.legacy_signal_cutoff_revision'
        )
        WHERE JSON_EXTRACT(config_json, '$.legacy_previous_signal_mode') IS NOT NULL
           OR JSON_EXTRACT(config_json, '$.legacy_signal_migrated_at') IS NOT NULL
           OR JSON_EXTRACT(config_json, '$.legacy_signal_cutoff_revision') IS NOT NULL
        """
    )

    op.alter_column(
        "strategy_templates",
        "template_kind",
        existing_type=sa.String(24),
        existing_nullable=False,
        server_default="builtin_strategy",
    )
    op.alter_column(
        "user_strategies",
        "strategy_kind",
        existing_type=sa.String(24),
        existing_nullable=False,
        server_default="builtin_strategy",
    )
    _replace_kind_constraints(include_legacy=False)


def downgrade() -> None:
    raise RuntimeError(
        "0080 is an intentional physical compatibility cleanup and cannot be downgraded"
    )
