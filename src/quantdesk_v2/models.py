from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    MetaData,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


BIGINT_PK = BigInteger()


class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        UniqueConstraint("username", name="uq_users_username"),
        UniqueConstraint("email", name="uq_users_email"),
        Index("ix_users_active", "is_active"),
        {"comment": "平台用户及其加密后的 Binance API 凭据"},
    )

    id: Mapped[int] = mapped_column(
        BIGINT_PK, primary_key=True, autoincrement=True, comment="用户主键"
    )
    username: Mapped[str] = mapped_column(
        String(64), nullable=False, comment="登录用户名，全局唯一"
    )
    email: Mapped[str | None] = mapped_column(String(254), comment="用户邮箱，可为空，全局唯一")
    password_hash: Mapped[str] = mapped_column(
        String(255), nullable=False, comment="Argon2id 登录密码哈希"
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, default=True, nullable=False, comment="账户是否启用"
    )
    is_admin: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False, comment="是否为平台管理员"
    )

    # 用户要求将币安凭据放在 users 表；这里只保存 Fernet 加密后的密文。
    binance_api_key_encrypted: Mapped[str | None] = mapped_column(
        Text, comment="Fernet 加密后的 Binance API Key"
    )
    binance_api_secret_encrypted: Mapped[str | None] = mapped_column(
        Text, comment="Fernet 加密后的 Binance API Secret"
    )
    binance_key_fingerprint: Mapped[str | None] = mapped_column(
        String(16), comment="API Key 的 SHA-256 短指纹，仅用于识别"
    )
    binance_key_version: Mapped[int] = mapped_column(
        Integer, default=1, nullable=False, comment="Binance 凭据版本号，每次更新或删除递增"
    )
    binance_permissions: Mapped[dict[str, Any] | None] = mapped_column(
        JSON, comment="Binance API 权限快照，只允许 READ 和 TRADE"
    )
    binance_key_updated_at: Mapped[datetime | None] = mapped_column(
        DateTime, comment="Binance 凭据最后更新时间（UTC）"
    )
    monitor_watchlist: Mapped[list[str] | None] = mapped_column(
        JSON, comment="当前用户的合约监控自选列表"
    )
    monitor_last_read_alert_id: Mapped[int] = mapped_column(
        BigInteger, default=0, nullable=False, comment="当前用户已读的最新监控信号 ID"
    )

    last_login_at: Mapped[datetime | None] = mapped_column(DateTime, comment="最后登录时间（UTC）")
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, nullable=False, comment="用户创建时间（UTC）"
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=utcnow,
        onupdate=utcnow,
        nullable=False,
        comment="用户记录最后更新时间（UTC）",
    )

    sessions: Mapped[list[UserSession]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    backtest_runs: Mapped[list[BacktestRun]] = relationship(
        back_populates="user", cascade="all, delete-orphan", passive_deletes=True
    )
    backtest_trades: Mapped[list[BacktestTrade]] = relationship(
        back_populates="user", cascade="all, delete-orphan", passive_deletes=True
    )
    user_strategies: Mapped[list[UserStrategy]] = relationship(
        back_populates="user", cascade="all, delete-orphan", passive_deletes=True
    )
    paper_accounts: Mapped[list[PaperAccount]] = relationship(
        back_populates="user", cascade="all, delete-orphan", passive_deletes=True
    )

    @property
    def binance_credentials_configured(self) -> bool:
        return bool(self.binance_api_key_encrypted and self.binance_api_secret_encrypted)


class UserSession(Base):
    __tablename__ = "user_sessions"
    __table_args__ = (
        UniqueConstraint("refresh_token_hash", name="uq_user_sessions_refresh_token_hash"),
        Index("ix_user_sessions_user_active", "user_id", "revoked_at", "expires_at"),
        {"comment": "用户登录会话与刷新令牌生命周期"},
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4()), comment="会话 UUID 主键"
    )
    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        comment="所属用户 ID",
    )
    refresh_token_hash: Mapped[str] = mapped_column(
        String(64), nullable=False, comment="刷新令牌 SHA-256 哈希，不保存明文令牌"
    )
    client_type: Mapped[str] = mapped_column(
        String(16), default="web", nullable=False, comment="客户端类型：web 或 native"
    )
    user_agent: Mapped[str | None] = mapped_column(String(512), comment="登录客户端 User-Agent")
    ip_address: Mapped[str | None] = mapped_column(String(45), comment="登录来源 IPv4 或 IPv6")
    expires_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, comment="刷新会话过期时间（UTC）"
    )
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime, comment="会话撤销时间（UTC），为空表示未撤销"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, nullable=False, comment="会话创建时间（UTC）"
    )

    user: Mapped[User] = relationship(back_populates="sessions")


class AuditLog(Base):
    __tablename__ = "audit_logs"
    __table_args__ = (
        Index("ix_audit_logs_user_created", "user_id", "created_at"),
        Index("ix_audit_logs_action_created", "action", "created_at"),
        {"comment": "安全敏感操作审计日志"},
    )

    id: Mapped[int] = mapped_column(
        BIGINT_PK, primary_key=True, autoincrement=True, comment="审计日志主键"
    )
    user_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("users.id", ondelete="SET NULL"),
        comment="操作用户 ID，用户删除后可为空",
    )
    action: Mapped[str] = mapped_column(String(80), nullable=False, comment="审计动作代码")
    resource_type: Mapped[str | None] = mapped_column(String(80), comment="被操作资源类型")
    resource_id: Mapped[str | None] = mapped_column(String(80), comment="被操作资源标识")
    ip_address: Mapped[str | None] = mapped_column(String(45), comment="操作来源 IPv4 或 IPv6")
    metadata_json: Mapped[dict[str, Any] | None] = mapped_column(
        JSON, comment="脱敏后的扩展审计信息，禁止存放密钥和令牌"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, nullable=False, comment="审计事件发生时间（UTC）"
    )


class StrategyTemplate(Base):
    __tablename__ = "strategy_templates"
    __table_args__ = (
        CheckConstraint("version > 0", name="positive_version"),
        CheckConstraint(
            "engine_key IN ('multi_factor', 'ma_cross', 'macd_momentum', "
            "'rsi_reversal', 'bollinger_reversion')",
            name="supported_engine",
        ),
        UniqueConstraint("template_key", name="uq_strategy_templates_template_key"),
        Index("ix_strategy_templates_active_sort", "is_active", "sort_order"),
        {
            "comment": "平台维护的系统默认策略模板",
            "mysql_engine": "InnoDB",
            "mysql_charset": "utf8mb4",
        },
    )

    id: Mapped[int] = mapped_column(
        BIGINT_PK, primary_key=True, autoincrement=True, comment="系统策略模板主键"
    )
    template_key: Mapped[str] = mapped_column(
        String(64), nullable=False, comment="系统策略模板稳定标识，全局唯一"
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False, comment="系统策略模板显示名称")
    category: Mapped[str] = mapped_column(
        String(32), nullable=False, comment="策略分类，例如趋势、动量或反转"
    )
    description: Mapped[str] = mapped_column(
        Text, nullable=False, comment="系统策略模板用途与信号逻辑说明"
    )
    engine_key: Mapped[str] = mapped_column(
        String(32), nullable=False, comment="受支持的安全回测引擎标识"
    )
    parameter_schema_json: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, nullable=False, comment="策略参数定义、类型、默认值及上下界"
    )
    parameters_json: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, comment="系统模板的默认策略参数"
    )
    risk_defaults_json: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, comment="系统模板的默认仓位、成本与风控参数"
    )
    version: Mapped[int] = mapped_column(
        Integer, default=1, nullable=False, comment="系统策略模板版本号"
    )
    sort_order: Mapped[int] = mapped_column(
        Integer, default=0, nullable=False, comment="策略中心的默认展示顺序"
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, default=True, nullable=False, comment="系统策略模板是否允许复制给用户"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, nullable=False, comment="系统策略模板创建时间（UTC）"
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=utcnow,
        onupdate=utcnow,
        nullable=False,
        comment="系统策略模板最后更新时间（UTC）",
    )

    user_strategies: Mapped[list[UserStrategy]] = relationship(
        back_populates="source_template", passive_deletes=True
    )


class UserStrategy(Base):
    __tablename__ = "user_strategies"
    __table_args__ = (
        CheckConstraint("version > 0", name="positive_version"),
        CheckConstraint("status IN ('active', 'archived')", name="valid_status"),
        CheckConstraint(
            "created_via IN ('system_default', 'manual', 'ai')", name="valid_created_via"
        ),
        CheckConstraint(
            "engine_key IN ('multi_factor', 'ma_cross', 'macd_momentum', "
            "'rsi_reversal', 'bollinger_reversion')",
            name="supported_engine",
        ),
        UniqueConstraint("public_id", name="uq_user_strategies_public_id"),
        UniqueConstraint(
            "user_id",
            "source_template_id",
            name="uq_user_strategies_user_source_template",
        ),
        # The composite key lets strategy_revisions enforce that a revision and its
        # strategy always belong to the same tenant at database level.
        UniqueConstraint("id", "user_id", name="uq_user_strategies_id_user_id"),
        Index("ix_user_strategies_user_status_updated", "user_id", "status", "updated_at"),
        Index("ix_user_strategies_user_name", "user_id", "name"),
        {
            "comment": "用户独立拥有并可编辑的策略配置",
            "mysql_engine": "InnoDB",
            "mysql_charset": "utf8mb4",
        },
    )

    id: Mapped[int] = mapped_column(
        BIGINT_PK, primary_key=True, autoincrement=True, comment="用户策略内部主键"
    )
    public_id: Mapped[str] = mapped_column(
        String(36),
        default=lambda: str(uuid.uuid4()),
        nullable=False,
        comment="对外使用的随机 UUID，避免暴露自增主键",
    )
    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        comment="所属用户 ID，用于租户数据隔离",
    )
    source_template_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("strategy_templates.id", ondelete="SET NULL"),
        comment="首次复制来源的系统策略模板 ID，自建策略可为空",
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False, comment="用户策略显示名称")
    category: Mapped[str] = mapped_column(
        String(32), nullable=False, comment="策略分类，例如趋势、动量或反转"
    )
    description: Mapped[str] = mapped_column(Text, nullable=False, comment="用户策略逻辑说明")
    status: Mapped[str] = mapped_column(
        String(16), default="active", nullable=False, comment="策略状态：启用或已归档"
    )
    version: Mapped[int] = mapped_column(
        Integer, default=1, nullable=False, comment="用户策略当前版本号"
    )
    engine_key: Mapped[str] = mapped_column(
        String(32), nullable=False, comment="受支持的安全回测引擎标识"
    )
    parameter_schema_json: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, nullable=False, comment="策略参数定义、类型、默认值及上下界快照"
    )
    parameters_json: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, comment="用户当前生效的策略参数"
    )
    risk_defaults_json: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, comment="用户策略默认仓位、成本与风控参数"
    )
    created_via: Mapped[str] = mapped_column(
        String(24),
        default="manual",
        nullable=False,
        comment="策略创建来源：系统默认复制、手工新建或 AI 新建",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, nullable=False, comment="用户策略创建时间（UTC）"
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=utcnow,
        onupdate=utcnow,
        nullable=False,
        comment="用户策略最后更新时间（UTC）",
    )

    user: Mapped[User] = relationship(back_populates="user_strategies")
    source_template: Mapped[StrategyTemplate | None] = relationship(
        back_populates="user_strategies"
    )
    revisions: Mapped[list[StrategyRevision]] = relationship(
        back_populates="strategy",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="StrategyRevision.version",
    )


class StrategyRevision(Base):
    __tablename__ = "strategy_revisions"
    __table_args__ = (
        CheckConstraint("version > 0", name="positive_version"),
        CheckConstraint(
            "change_source IN ('system_default', 'manual', 'ai')", name="valid_change_source"
        ),
        ForeignKeyConstraint(
            ["user_strategy_id", "user_id"],
            ["user_strategies.id", "user_strategies.user_id"],
            name="fk_strategy_revisions_strategy_tenant",
            ondelete="CASCADE",
        ),
        UniqueConstraint(
            "user_strategy_id", "version", name="uq_strategy_revisions_strategy_version"
        ),
        Index("ix_strategy_revisions_user_created", "user_id", "created_at"),
        {
            "comment": "用户策略每次修改后的不可变版本快照",
            "mysql_engine": "InnoDB",
            "mysql_charset": "utf8mb4",
        },
    )

    id: Mapped[int] = mapped_column(
        BIGINT_PK, primary_key=True, autoincrement=True, comment="策略修订记录主键"
    )
    user_strategy_id: Mapped[int] = mapped_column(
        BigInteger, nullable=False, comment="所属用户策略内部 ID"
    )
    user_id: Mapped[int] = mapped_column(
        BigInteger, nullable=False, comment="所属用户 ID，用于数据库级租户一致性校验"
    )
    version: Mapped[int] = mapped_column(
        Integer, nullable=False, comment="该用户策略内单调递增的版本号"
    )
    change_source: Mapped[str] = mapped_column(
        String(24), nullable=False, comment="修改来源：系统默认复制、手工修改或 AI 修改"
    )
    change_summary: Mapped[str] = mapped_column(
        String(500), nullable=False, comment="本次修改的简短说明"
    )
    snapshot_json: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, comment="该版本完整且可复现的策略配置快照"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, nullable=False, comment="策略修订创建时间（UTC）"
    )

    strategy: Mapped[UserStrategy] = relationship(back_populates="revisions")


class BacktestRun(Base):
    __tablename__ = "backtest_runs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('queued', 'running', 'completed', 'failed', 'cancelled')",
            name="valid_status",
        ),
        CheckConstraint("end_at >= start_at", name="valid_period"),
        CheckConstraint("initial_capital > 0", name="positive_initial_capital"),
        CheckConstraint("trade_count >= 0", name="nonnegative_trade_count"),
        Index("ix_backtest_runs_user_created", "user_id", "created_at"),
        Index("ix_backtest_runs_user_status_created", "user_id", "status", "created_at"),
        Index("ix_backtest_runs_user_strategy_created", "user_id", "strategy_id", "created_at"),
        Index("ix_backtest_runs_user_symbol_timeframe", "user_id", "symbol", "timeframe"),
        {
            "comment": "用户策略回测任务、配置与汇总指标",
            "mysql_engine": "InnoDB",
            "mysql_charset": "utf8mb4",
        },
    )

    id: Mapped[int] = mapped_column(
        BIGINT_PK, primary_key=True, autoincrement=True, comment="回测任务主键"
    )
    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        comment="所属用户 ID，用于租户数据隔离",
    )
    strategy_id: Mapped[str] = mapped_column(
        String(64), nullable=False, comment="策略目录中的稳定策略标识"
    )
    strategy_name: Mapped[str] = mapped_column(
        String(128), nullable=False, comment="执行回测时的策略名称快照"
    )
    symbol: Mapped[str] = mapped_column(String(32), nullable=False, comment="回测交易标的代码")
    timeframe: Mapped[str] = mapped_column(
        String(8), nullable=False, comment="回测行情周期，例如 15m、4h 或 1d"
    )
    status: Mapped[str] = mapped_column(
        String(16),
        default="queued",
        nullable=False,
        comment="任务状态：排队、运行、完成、失败或取消",
    )
    start_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, comment="回测数据起始时间（UTC）"
    )
    end_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, comment="回测数据结束时间（UTC）"
    )
    initial_capital: Mapped[Decimal] = mapped_column(
        Numeric(30, 8), nullable=False, comment="回测初始资金"
    )
    final_equity: Mapped[Decimal | None] = mapped_column(
        Numeric(30, 8), comment="回测结束时账户权益"
    )
    net_profit: Mapped[Decimal | None] = mapped_column(
        Numeric(30, 8), comment="扣除交易成本后的净利润"
    )
    total_return_pct: Mapped[Decimal | None] = mapped_column(
        Numeric(18, 8), comment="总收益率百分比"
    )
    max_drawdown_pct: Mapped[Decimal | None] = mapped_column(
        Numeric(18, 8), comment="最大回撤百分比"
    )
    sharpe_ratio: Mapped[Decimal | None] = mapped_column(Numeric(18, 8), comment="年化夏普比率")
    win_rate_pct: Mapped[Decimal | None] = mapped_column(
        Numeric(18, 8), comment="盈利成交占比百分比"
    )
    profit_factor: Mapped[Decimal | None] = mapped_column(
        Numeric(18, 8), comment="总盈利与总亏损绝对值之比"
    )
    trade_count: Mapped[int] = mapped_column(
        Integer, default=0, nullable=False, comment="已完成成交总笔数"
    )
    config_json: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, comment="本次回测的完整参数与成本配置快照"
    )
    metrics_json: Mapped[dict[str, Any] | None] = mapped_column(
        JSON, comment="可扩展的回测指标集合"
    )
    equity_curve_json: Mapped[list[dict[str, Any]] | None] = mapped_column(
        JSON, comment="按时间排序的账户权益曲线数据"
    )
    data_quality_json: Mapped[dict[str, Any] | None] = mapped_column(
        JSON, comment="有效行情柱数、实际数据区间、截断与回测假设说明"
    )
    metadata_json: Mapped[dict[str, Any] | None] = mapped_column(
        JSON, comment="数据源、引擎版本等扩展运行信息"
    )
    error: Mapped[str | None] = mapped_column(Text, comment="任务失败时的脱敏错误说明")
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, nullable=False, comment="回测任务创建时间（UTC）"
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime, comment="回测任务完成或失败时间（UTC）"
    )

    user: Mapped[User] = relationship(back_populates="backtest_runs")
    trades: Mapped[list[BacktestTrade]] = relationship(
        back_populates="run",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="BacktestTrade.entry_at",
    )


class BacktestTrade(Base):
    __tablename__ = "backtest_trades"
    __table_args__ = (
        CheckConstraint("side IN ('long', 'short')", name="valid_side"),
        CheckConstraint("quantity > 0", name="positive_quantity"),
        CheckConstraint("holding_bars >= 0", name="nonnegative_holding_bars"),
        Index("ix_backtest_trades_run_entry", "run_id", "entry_at"),
        Index("ix_backtest_trades_user_entry", "user_id", "entry_at"),
        {
            "comment": "回测任务产生的逐笔成交明细",
            "mysql_engine": "InnoDB",
            "mysql_charset": "utf8mb4",
        },
    )

    id: Mapped[int] = mapped_column(
        BIGINT_PK, primary_key=True, autoincrement=True, comment="回测成交主键"
    )
    run_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("backtest_runs.id", ondelete="CASCADE"),
        nullable=False,
        comment="所属回测任务 ID",
    )
    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        comment="所属用户 ID，用于租户数据隔离",
    )
    side: Mapped[str] = mapped_column(
        String(8), nullable=False, comment="持仓方向：long 多头或 short 空头"
    )
    entry_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, comment="开仓成交时间（UTC）"
    )
    exit_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, comment="平仓成交时间（UTC）"
    )
    entry_price: Mapped[Decimal] = mapped_column(
        Numeric(30, 12), nullable=False, comment="开仓成交价格"
    )
    exit_price: Mapped[Decimal] = mapped_column(
        Numeric(30, 12), nullable=False, comment="平仓成交价格"
    )
    quantity: Mapped[Decimal] = mapped_column(
        Numeric(48, 18), nullable=False, comment="成交标的数量，保留极小仓位精度"
    )
    gross_pnl: Mapped[Decimal] = mapped_column(
        Numeric(30, 8), nullable=False, comment="扣除费用前的成交盈亏"
    )
    fees: Mapped[Decimal] = mapped_column(
        Numeric(30, 8), nullable=False, comment="开仓与平仓交易费用合计"
    )
    net_pnl: Mapped[Decimal] = mapped_column(
        Numeric(30, 8), nullable=False, comment="扣除费用后的成交净盈亏"
    )
    return_pct: Mapped[Decimal] = mapped_column(
        Numeric(18, 8), nullable=False, comment="本笔成交收益率百分比"
    )
    holding_bars: Mapped[int] = mapped_column(
        Integer, nullable=False, comment="从开仓到平仓持有的行情柱数量"
    )
    exit_reason: Mapped[str | None] = mapped_column(
        String(64), comment="平仓原因代码，例如止损、止盈或超时"
    )
    metadata_json: Mapped[dict[str, Any] | None] = mapped_column(
        JSON, comment="信号、滑点与执行过程等扩展信息"
    )

    run: Mapped[BacktestRun] = relationship(back_populates="trades")
    user: Mapped[User] = relationship(back_populates="backtest_trades")


class PaperAccount(Base):
    __tablename__ = "paper_accounts"
    __table_args__ = (
        CheckConstraint("status IN ('active', 'paused', 'archived')", name="valid_status"),
        CheckConstraint("initial_balance > 0", name="positive_initial_balance"),
        CheckConstraint("balance >= 0", name="nonnegative_balance"),
        ForeignKeyConstraint(
            ["strategy_id", "user_id"],
            ["user_strategies.id", "user_strategies.user_id"],
            name="fk_paper_accounts_strategy_tenant",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("public_id", name="uq_paper_accounts_public_id"),
        UniqueConstraint("id", "user_id", name="uq_paper_accounts_id_user_id"),
        UniqueConstraint("user_id", "name", name="uq_paper_accounts_user_name"),
        Index("ix_paper_accounts_user_status_updated", "user_id", "status", "updated_at"),
        {
            "comment": "用户可独立运行的多策略模拟盘实例",
            "mysql_engine": "InnoDB",
            "mysql_charset": "utf8mb4",
        },
    )

    id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=True, comment="模拟盘内部主键"
    )
    public_id: Mapped[str] = mapped_column(
        String(36), default=lambda: str(uuid.uuid4()), nullable=False, comment="模拟盘公开 UUID"
    )
    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        comment="所属用户 ID",
    )
    strategy_id: Mapped[int] = mapped_column(
        BigInteger, nullable=False, comment="同一用户拥有的策略 ID"
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False, comment="用户自定义模拟盘名称")
    status: Mapped[str] = mapped_column(
        String(16), default="active", nullable=False, comment="运行、暂停或归档"
    )
    initial_balance: Mapped[Decimal] = mapped_column(
        Numeric(30, 8), nullable=False, comment="初始资金"
    )
    balance: Mapped[Decimal] = mapped_column(
        Numeric(30, 8), nullable=False, comment="当前可用余额"
    )
    config_json: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, comment="杠杆、仓位和风控配置"
    )
    strategy_snapshot_json: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, comment="创建或切换策略时的完整策略快照"
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, nullable=False, comment="本轮模拟盘启动时间"
    )
    last_tick_at: Mapped[datetime | None] = mapped_column(
        DateTime, comment="最后一次策略执行时间"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, nullable=False, comment="模拟盘创建时间"
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=utcnow,
        onupdate=utcnow,
        nullable=False,
        comment="模拟盘最后更新时间",
    )

    user: Mapped[User] = relationship(back_populates="paper_accounts")
