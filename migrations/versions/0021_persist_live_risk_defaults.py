"""Persist effective live and paper risk defaults for existing accounts.

Revision ID: 0021_live_risk_defaults
Revises: 0020_entry_basis
"""

# ruff: noqa: S608 -- all interpolated identifiers are fixed migration constants.

from collections.abc import Sequence

from alembic import op

revision: str = "0021_live_risk_defaults"
down_revision: str | None = "0020_entry_basis"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_COMMON_DEFAULTS: tuple[tuple[str, str], ...] = (
    ("risk_per_trade_pct", "0.5"),
    ("max_total_risk_pct", "4"),
    ("max_cluster_positions", "2"),
    ("risk_max_leverage", "10"),
    ("liquidation_buffer_pct", "1.5"),
    ("daily_loss_limit_pct", "2"),
    ("max_drawdown_pct", "6"),
    ("short_risk_multiplier", "0.5"),
    ("max_ticker_age_seconds", "120"),
    ("max_signal_age_seconds", "18000"),
    ("block_high_risk_products", "true"),
    ("max_high_risk_positions", "1"),
    ("high_risk_multiplier", "0.5"),
    ("signal_valid_bars", "1"),
)
_LIVE_DEFAULTS = (*_COMMON_DEFAULTS, ("round_trip_cost_bps", "16"))


def _require_mysql() -> None:
    if op.get_bind().dialect.name not in {"mysql", "mariadb"}:
        raise RuntimeError("QuantDesk migrations require MySQL or MariaDB")


def _account_pairs(
    column: str, defaults: tuple[tuple[str, str], ...]
) -> str:
    return ", ".join(
        f"'$.{key}', IFNULL(JSON_EXTRACT({column}, '$.{key}'), "
        f"JSON_EXTRACT('{default_json}', '$'))"
        for key, default_json in defaults
    )


def _deployment_pairs(
    destination: str,
    source: str,
    defaults: tuple[tuple[str, str], ...],
) -> str:
    return ", ".join(
        f"'$.{key}', IFNULL(JSON_EXTRACT({destination}, '$.{key}'), "
        f"JSON_EXTRACT({source}, '$.{key}'))"
        for key, _default_json in defaults
    )


def _persist_account_defaults(
    table: str,
    defaults: tuple[tuple[str, str], ...],
) -> None:
    pairs = _account_pairs("config_json", defaults)
    op.execute(
        f"UPDATE {table} SET config_json="
        f"JSON_SET(COALESCE(config_json, JSON_OBJECT()), {pairs})"
    )


def _persist_deployment_defaults(
    account_table: str,
    mode: str,
    defaults: tuple[tuple[str, str], ...],
) -> None:
    pairs = _deployment_pairs("d.risk_override_json", "a.config_json", defaults)
    op.execute(
        "UPDATE strategy_deployments AS d "
        f"JOIN {account_table} AS a "
        "ON a.id=d.target_account_id AND a.user_id=d.user_id "
        "SET d.risk_override_json="
        f"JSON_SET(COALESCE(d.risk_override_json, JSON_OBJECT()), {pairs}) "
        f"WHERE d.mode='{mode}'"
    )


def upgrade() -> None:
    _require_mysql()
    _persist_account_defaults("live_trading_accounts", _LIVE_DEFAULTS)
    # Paper fees are account-specific; the engine derives round-trip cost from
    # each row's fee/slippage settings instead of forcing a live-market value.
    _persist_account_defaults("paper_accounts", _COMMON_DEFAULTS)
    # Deployments inherit the effective values from their target account. This
    # preserves pre-existing account customization while keeping explicit
    # deployment overrides authoritative.
    _persist_deployment_defaults("live_trading_accounts", "live", _LIVE_DEFAULTS)
    _persist_deployment_defaults("paper_accounts", "paper", _COMMON_DEFAULTS)


def downgrade() -> None:
    _require_mysql()
    # These keys may have been explicitly edited after this migration. Keeping
    # them is safer than deleting user risk configuration during a rollback.
