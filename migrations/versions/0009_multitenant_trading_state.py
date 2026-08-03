"""Make positions, alerts, state, and paper trading tenant-aware.

Revision ID: 0009_multitenant_trading
Revises: 0008_mysql_market_store
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009_multitenant_trading"
down_revision: str | None = "0008_mysql_market_store"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE_OPTIONS = {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"}


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name not in {"mysql", "mariadb"}:
        raise RuntimeError("QuantDesk migrations require MySQL")

    _seed_paper_strategy_copies()

    op.create_table(
        "paper_accounts",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("public_id", sa.String(36), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("strategy_id", sa.BigInteger(), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("status", sa.String(16), server_default="active", nullable=False),
        sa.Column("initial_balance", sa.Numeric(30, 8), nullable=False),
        sa.Column("balance", sa.Numeric(30, 8), nullable=False),
        sa.Column("config_json", sa.JSON(), nullable=False),
        sa.Column("strategy_snapshot_json", sa.JSON(), nullable=False),
        sa.Column("started_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("last_tick_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.CheckConstraint(
            "status IN ('active', 'paused', 'archived')", name="ck_paper_accounts_valid_status"
        ),
        sa.CheckConstraint("initial_balance > 0", name="ck_paper_accounts_positive_initial_balance"),
        sa.CheckConstraint("balance >= 0", name="ck_paper_accounts_nonnegative_balance"),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_paper_accounts_user_id_users", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["strategy_id", "user_id"],
            ["user_strategies.id", "user_strategies.user_id"],
            name="fk_paper_accounts_strategy_tenant",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_paper_accounts"),
        sa.UniqueConstraint("public_id", name="uq_paper_accounts_public_id"),
        sa.UniqueConstraint("id", "user_id", name="uq_paper_accounts_id_user_id"),
        sa.UniqueConstraint("user_id", "name", name="uq_paper_accounts_user_name"),
        **TABLE_OPTIONS,
    )
    op.create_index(
        "ix_paper_accounts_user_status_updated",
        "paper_accounts",
        ["user_id", "status", "updated_at"],
        unique=False,
    )

    # Preserve the old shared paper account under the first existing user's first
    # active strategy. Fresh databases have no shared rows and therefore create no
    # implicit account.
    op.execute(
        sa.text(
            """
            INSERT INTO paper_accounts(
                public_id,user_id,strategy_id,name,status,initial_balance,balance,
                config_json,strategy_snapshot_json,started_at,created_at,updated_at
            )
            SELECT UUID(), u.id, us.id, '已迁移模拟盘', 'active', 10000,
                   COALESCE((SELECT CAST(v AS DECIMAL(30,8)) FROM kv WHERE k='paper_balance'), 10000),
                   JSON_OBJECT('leverage',20,'max_positions',15,'margin_cap',0.8),
                   JSON_OBJECT(
                       'strategy_id',us.id,'public_id',us.public_id,'name',us.name,
                       'engine_key',us.engine_key,'version',us.version,
                       'parameters',us.parameters_json,'risk_defaults',us.risk_defaults_json
                   ),
                   FROM_UNIXTIME(COALESCE(
                       (SELECT CAST(v AS UNSIGNED) FROM kv WHERE k='paper_start_ts'), UNIX_TIMESTAMP()
                   )), CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
            FROM users u
            JOIN user_strategies us ON us.user_id=u.id AND us.status='active'
            LEFT JOIN strategy_templates st ON st.id=us.source_template_id
            WHERE EXISTS (SELECT 1 FROM paper_positions LIMIT 1)
               OR EXISTS (SELECT 1 FROM paper_trades LIMIT 1)
               OR EXISTS (SELECT 1 FROM paper_equity LIMIT 1)
            ORDER BY u.is_admin DESC, u.id,
                     (st.template_key='paper_multifactor_atr_v1') DESC, us.id
            LIMIT 1
            """
        )
    )

    _replace_positions()
    _replace_alerts()
    op.rename_table("kv", "system_state")
    op.create_table(
        "user_states",
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("k", sa.String(255), nullable=False),
        sa.Column("v", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_user_states_user_id_users", ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("user_id", "k", name="pk_user_states"),
        **TABLE_OPTIONS,
    )
    _replace_paper_positions()
    _replace_paper_trades()
    _replace_paper_equity()

    # Paper balance and timestamps now live on paper_accounts. Only genuinely
    # system-wide collector state remains in system_state.
    op.execute(sa.text("DELETE FROM system_state WHERE k LIKE 'paper%'"))


def _seed_paper_strategy_copies() -> None:
    """Give every existing user the repaired paper strategy before account migration."""

    op.execute(
        sa.text(
            """
            INSERT IGNORE INTO user_strategies(
                public_id,user_id,source_template_id,name,category,description,status,version,
                engine_key,parameter_schema_json,parameters_json,risk_defaults_json,created_via,
                created_at,updated_at
            )
            SELECT UUID(),u.id,t.id,t.name,t.category,t.description,'active',t.version,
                   t.engine_key,t.parameter_schema_json,t.parameters_json,t.risk_defaults_json,
                   'system_default',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP
            FROM users u
            JOIN strategy_templates t ON t.template_key='paper_multifactor_atr_v1'
            LEFT JOIN user_strategies existing
              ON existing.user_id=u.id AND existing.source_template_id=t.id
            WHERE existing.id IS NULL
            """
        )
    )
    op.execute(
        sa.text(
            """
            INSERT IGNORE INTO strategy_revisions(
                user_strategy_id,user_id,version,change_source,change_summary,
                snapshot_json,created_at
            )
            SELECT us.id,us.user_id,us.version,'system_default',
                   '系统模拟盘默认策略初始化',
                   JSON_OBJECT(
                       'public_id',us.public_id,'name',us.name,'category',us.category,
                       'description',us.description,'status',us.status,'version',us.version,
                       'engine_key',us.engine_key,
                       'parameter_schema',us.parameter_schema_json,
                       'parameters',us.parameters_json,'risk_defaults',us.risk_defaults_json
                   ),CURRENT_TIMESTAMP
            FROM user_strategies us
            JOIN strategy_templates t ON t.id=us.source_template_id
            WHERE t.template_key='paper_multifactor_atr_v1'
              AND NOT EXISTS(
                  SELECT 1 FROM strategy_revisions sr
                  WHERE sr.user_strategy_id=us.id AND sr.version=us.version
              )
            """
        )
    )


def _replace_positions() -> None:
    op.create_table(
        "positions_v2",
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("symbol", sa.String(32), nullable=False),
        sa.Column("amt", sa.Double(), nullable=True),
        sa.Column("side", sa.String(16), nullable=True),
        sa.Column("entry_price", sa.Double(), nullable=True),
        sa.Column("mark_price", sa.Double(), nullable=True),
        sa.Column("upnl", sa.Double(), nullable=True),
        sa.Column("leverage", sa.Integer(), nullable=True),
        sa.Column("ts", sa.BigInteger(), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_positions_user_id_users", ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("user_id", "symbol", name="pk_positions"),
        **TABLE_OPTIONS,
    )
    op.execute(
        sa.text(
            """
            INSERT INTO positions_v2(user_id,symbol,amt,side,entry_price,mark_price,upnl,leverage,ts)
            SELECT u.id,p.symbol,p.amt,p.side,p.entry_price,p.mark_price,p.upnl,p.leverage,p.ts
            FROM positions p
            JOIN (SELECT id FROM users ORDER BY is_admin DESC,id LIMIT 1) u
            """
        )
    )
    op.drop_table("positions")
    op.rename_table("positions_v2", "positions")


def _replace_alerts() -> None:
    op.create_table(
        "alerts_v2",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("ts", sa.BigInteger(), nullable=False),
        sa.Column("symbol", sa.String(32), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("direction", sa.String(16), nullable=False),
        sa.Column("score", sa.Double(), nullable=True),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column("read", sa.Boolean(), server_default=sa.text("0"), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_alerts_user_id_users", ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_alerts"),
        **TABLE_OPTIONS,
    )
    op.create_index("ix_alerts_user_ts", "alerts_v2", ["user_id", "ts"], unique=False)
    op.execute(
        sa.text(
            """
            INSERT INTO alerts_v2(id,user_id,ts,symbol,kind,direction,score,message,detail,`read`)
            SELECT a.id,u.id,a.ts,a.symbol,a.kind,a.direction,a.score,a.message,a.detail,a.`read`
            FROM alerts a
            JOIN (SELECT id FROM users ORDER BY is_admin DESC,id LIMIT 1) u
            """
        )
    )
    op.drop_table("alerts")
    op.rename_table("alerts_v2", "alerts")


def _replace_paper_positions() -> None:
    op.create_table(
        "paper_positions_v2",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("paper_account_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("symbol", sa.String(32), nullable=False),
        sa.Column("side", sa.Integer(), nullable=False),
        sa.Column("qty", sa.Double(), nullable=False),
        sa.Column("avg_entry", sa.Double(), nullable=False),
        sa.Column("margin", sa.Double(), nullable=False),
        sa.Column("leverage", sa.Integer(), nullable=False),
        sa.Column("stop", sa.Double(), nullable=True),
        sa.Column("target", sa.Double(), nullable=True),
        sa.Column("adds", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("opened_ts", sa.BigInteger(), nullable=False),
        sa.Column("last_add_ts", sa.BigInteger(), nullable=True),
        sa.Column("open_score", sa.Integer(), nullable=True),
        sa.Column("basis", sa.Text(), nullable=True),
        sa.Column("funding_acc", sa.Double(), server_default=sa.text("0"), nullable=False),
        sa.Column("liq_price", sa.Double(), nullable=True),
        sa.Column("funding_ts", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("atr_entry", sa.Double(), nullable=True),
        sa.Column("peak_price", sa.Double(), nullable=True),
        sa.Column("tp_done", sa.Boolean(), server_default=sa.text("0"), nullable=False),
        sa.ForeignKeyConstraint(
            ["paper_account_id", "user_id"],
            ["paper_accounts.id", "paper_accounts.user_id"],
            name="fk_paper_positions_account_tenant",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_paper_positions"),
        sa.UniqueConstraint("paper_account_id", "symbol", name="uq_paper_positions_account_symbol"),
        **TABLE_OPTIONS,
    )
    op.execute(
        sa.text(
            """
            INSERT INTO paper_positions_v2(
                id,paper_account_id,user_id,symbol,side,qty,avg_entry,margin,leverage,stop,target,
                adds,opened_ts,last_add_ts,open_score,basis,funding_acc,liq_price,funding_ts,
                atr_entry,peak_price,tp_done
            )
            SELECT p.id,a.id,a.user_id,p.symbol,p.side,p.qty,p.avg_entry,p.margin,p.leverage,
                   p.stop,p.target,p.adds,p.opened_ts,p.last_add_ts,p.open_score,p.basis,
                   p.funding_acc,p.liq_price,p.funding_ts,p.atr_entry,p.peak_price,p.tp_done
            FROM paper_positions p JOIN paper_accounts a ON a.name='已迁移模拟盘'
            """
        )
    )
    op.drop_table("paper_positions")
    op.rename_table("paper_positions_v2", "paper_positions")


def _replace_paper_trades() -> None:
    op.create_table(
        "paper_trades_v2",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("paper_account_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("symbol", sa.String(32), nullable=False),
        sa.Column("side", sa.Integer(), nullable=False),
        sa.Column("qty", sa.Double(), nullable=True),
        sa.Column("entry_price", sa.Double(), nullable=True),
        sa.Column("exit_price", sa.Double(), nullable=True),
        sa.Column("margin", sa.Double(), nullable=True),
        sa.Column("pnl", sa.Double(), nullable=True),
        sa.Column("fee", sa.Double(), nullable=True),
        sa.Column("funding", sa.Double(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("open_score", sa.Integer(), nullable=True),
        sa.Column("opened_ts", sa.BigInteger(), nullable=True),
        sa.Column("closed_ts", sa.BigInteger(), nullable=False),
        sa.ForeignKeyConstraint(
            ["paper_account_id", "user_id"],
            ["paper_accounts.id", "paper_accounts.user_id"],
            name="fk_paper_trades_account_tenant",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_paper_trades"),
        **TABLE_OPTIONS,
    )
    op.create_index(
        "ix_paper_trades_account_closed",
        "paper_trades_v2",
        ["paper_account_id", "closed_ts"],
        unique=False,
    )
    op.execute(
        sa.text(
            """
            INSERT INTO paper_trades_v2(
                id,paper_account_id,user_id,symbol,side,qty,entry_price,exit_price,margin,pnl,
                fee,funding,reason,open_score,opened_ts,closed_ts
            )
            SELECT p.id,a.id,a.user_id,p.symbol,p.side,p.qty,p.entry_price,p.exit_price,p.margin,
                   p.pnl,p.fee,p.funding,p.reason,p.open_score,p.opened_ts,p.closed_ts
            FROM paper_trades p JOIN paper_accounts a ON a.name='已迁移模拟盘'
            """
        )
    )
    op.drop_table("paper_trades")
    op.rename_table("paper_trades_v2", "paper_trades")


def _replace_paper_equity() -> None:
    op.create_table(
        "paper_equity_v2",
        sa.Column("paper_account_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("ts", sa.BigInteger(), autoincrement=False, nullable=False),
        sa.Column("equity", sa.Double(), nullable=False),
        sa.Column("balance", sa.Double(), nullable=False),
        sa.ForeignKeyConstraint(
            ["paper_account_id", "user_id"],
            ["paper_accounts.id", "paper_accounts.user_id"],
            name="fk_paper_equity_account_tenant",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("paper_account_id", "ts", name="pk_paper_equity"),
        **TABLE_OPTIONS,
    )
    op.execute(
        sa.text(
            """
            INSERT INTO paper_equity_v2(paper_account_id,user_id,ts,equity,balance)
            SELECT a.id,a.user_id,p.ts,p.equity,p.balance
            FROM paper_equity p JOIN paper_accounts a ON a.name='已迁移模拟盘'
            """
        )
    )
    op.drop_table("paper_equity")
    op.rename_table("paper_equity_v2", "paper_equity")


def downgrade() -> None:
    raise RuntimeError("0009 cannot be safely downgraded after multiple users create trading data")
