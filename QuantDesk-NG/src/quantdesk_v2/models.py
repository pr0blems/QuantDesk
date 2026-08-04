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
    Computed,
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

NEWS_DEDUP_EXPRESSION = (
    "CASE WHEN link IS NULL THEN NULL "
    "ELSE SHA2(CONCAT(COALESCE(source, ''), CHAR(0), link), 256) END"
)


class News(Base):
    __tablename__ = "news"
    __table_args__ = (
        Index("ix_news_ts", "ts"),
        Index("uq_news_source_link_hash", "source_link_hash", unique=True),
        {
            "comment": "系统共享的新闻、翻译、情绪和摘要数据",
            "mysql_engine": "InnoDB",
            "mysql_charset": "utf8mb4",
        },
    )

    id: Mapped[str] = mapped_column(String(255), primary_key=True, comment="新闻稳定标识主键")
    ts: Mapped[int] = mapped_column(
        BigInteger, nullable=False, comment="新闻发布时间的 Unix 时间戳"
    )
    source: Mapped[str | None] = mapped_column(String(80), comment="新闻来源名称")
    lang: Mapped[str | None] = mapped_column(String(16), comment="新闻原文语言代码")
    title: Mapped[str] = mapped_column(Text, nullable=False, comment="新闻原始标题")
    title_zh: Mapped[str | None] = mapped_column(Text, comment="新闻中文翻译标题")
    link: Mapped[str | None] = mapped_column(Text, comment="新闻原文链接")
    sentiment: Mapped[str | None] = mapped_column(String(32), comment="新闻情绪分类")
    summary: Mapped[str | None] = mapped_column(Text, comment="新闻摘要或深度舆情摘要")
    source_link_hash: Mapped[str | None] = mapped_column(
        String(64),
        Computed(NEWS_DEDUP_EXPRESSION, persisted=True),
        comment="来源名称与原文链接生成的 SHA-256 去重键",
    )


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
    ai_model_configs: Mapped[list[AiModelConfig]] = relationship(
        back_populates="user", cascade="all, delete-orphan", passive_deletes=True
    )

    @property
    def binance_credentials_configured(self) -> bool:
        return bool(self.binance_api_key_encrypted and self.binance_api_secret_encrypted)


class AiModelConfig(Base):
    __tablename__ = "ai_model_configs"
    __table_args__ = (
        CheckConstraint(
            "provider_code IN ('openai', 'deepseek', 'doubao', 'qwen', 'kimi', 'minimax')",
            name="valid_provider",
        ),
        CheckConstraint("is_default = 0 OR is_enabled = 1", name="default_enabled"),
        UniqueConstraint("public_id", name="uq_ai_model_configs_public_id"),
        UniqueConstraint("user_id", "display_name", name="uq_ai_model_configs_user_display_name"),
        UniqueConstraint("default_user_id", name="uq_ai_model_configs_default_user_id"),
        Index(
            "ix_ai_model_configs_user_updated",
            "user_id",
            "updated_at",
        ),
        Index("ix_ai_model_configs_provider", "provider_code"),
        {
            "comment": "用户隔离并加密保存的 AI 模型调用配置",
            "mysql_engine": "InnoDB",
            "mysql_charset": "utf8mb4",
        },
    )

    id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=True, comment="AI 模型配置内部主键"
    )
    public_id: Mapped[str] = mapped_column(
        String(36),
        default=lambda: str(uuid.uuid4()),
        nullable=False,
        comment="供接口使用的公开 UUID",
    )
    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        comment="所属用户 ID，用于租户隔离",
    )
    provider_code: Mapped[str] = mapped_column(
        String(32), nullable=False, comment="服务端白名单中的 AI 服务商代码"
    )
    display_name: Mapped[str] = mapped_column(
        String(80), nullable=False, comment="用户自定义配置名称，同一用户内唯一"
    )
    model_name: Mapped[str] = mapped_column(String(128), nullable=False, comment="服务商模型标识")
    api_key_encrypted: Mapped[str] = mapped_column(
        Text, nullable=False, comment="Fernet 加密后的 AI 服务商 API Key"
    )
    api_key_fingerprint: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="API Key 的 SHA-256 短指纹，仅用于识别"
    )
    api_key_version: Mapped[int] = mapped_column(
        Integer, default=1, nullable=False, comment="API Key 版本，每次替换时递增"
    )
    is_enabled: Mapped[bool] = mapped_column(
        Boolean, default=True, nullable=False, comment="该模型配置是否允许被调用"
    )
    is_default: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False, comment="是否为当前用户默认 AI 模型"
    )
    default_user_id: Mapped[int | None] = mapped_column(
        BigInteger,
        Computed("CASE WHEN is_default = 1 THEN user_id ELSE NULL END", persisted=True),
        comment="默认配置唯一性生成列；非默认配置为空",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, nullable=False, comment="配置创建时间（UTC）"
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=utcnow,
        onupdate=utcnow,
        nullable=False,
        comment="配置最后更新时间（UTC）",
    )

    user: Mapped[User] = relationship(back_populates="ai_model_configs")


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


class AdminSetting(Base):
    __tablename__ = "admin_settings"
    __table_args__ = ({"comment": "管理员发布的系统运行配置"},)

    key: Mapped[str] = mapped_column(String(64), primary_key=True, comment="配置稳定键")
    value_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, comment="配置 JSON")
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False, comment="配置版本")
    updated_by: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="SET NULL"), comment="最后更新管理员"
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow, nullable=False, comment="最后更新时间"
    )


class NewsSourceSetting(Base):
    __tablename__ = "news_source_settings"
    __table_args__ = (
        Index("ix_news_source_settings_enabled", "enabled"),
        {"comment": "可由管理员维护的新闻来源与健康状态"},
    )

    name: Mapped[str] = mapped_column(String(80), primary_key=True, comment="来源名称")
    url: Mapped[str] = mapped_column(Text, nullable=False, comment="RSS HTTPS 地址")
    lang: Mapped[str] = mapped_column(String(16), default="en", nullable=False, comment="内容语言")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False, comment="是否启用")
    slow: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False, comment="是否低频轮询"
    )
    weight: Mapped[int] = mapped_column(
        Integer, default=100, nullable=False, comment="来源展示权重"
    )
    hourly_limit: Mapped[int] = mapped_column(
        Integer, default=600, nullable=False, comment="每小时最大入库数量"
    )
    last_success_at: Mapped[int | None] = mapped_column(BigInteger, comment="最后成功 Unix 时间")
    last_error_at: Mapped[int | None] = mapped_column(BigInteger, comment="最后失败 Unix 时间")
    last_error: Mapped[str | None] = mapped_column(Text, comment="最近错误摘要")
    fetched_items: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    inserted_items: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    updated_by: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="SET NULL"), comment="最后更新管理员"
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow, nullable=False
    )


class CollectorStatus(Base):
    __tablename__ = "collector_status"
    __table_args__ = ({"comment": "后台采集器心跳与最近运行结果"},)

    name: Mapped[str] = mapped_column(String(32), primary_key=True, comment="采集器名称")
    heartbeat_at: Mapped[int] = mapped_column(
        BigInteger, nullable=False, comment="最近心跳 Unix 时间"
    )
    last_success_at: Mapped[int | None] = mapped_column(BigInteger, comment="最近成功 Unix 时间")
    last_error_at: Mapped[int | None] = mapped_column(BigInteger, comment="最近失败 Unix 时间")
    last_error: Mapped[str | None] = mapped_column(Text, comment="最近错误摘要")
    cycles: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False, comment="累计周期数")
    items: Mapped[int] = mapped_column(
        BigInteger, default=0, nullable=False, comment="累计处理条数"
    )
    details_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, comment="脱敏运行详情")


class StrategyTemplate(Base):
    __tablename__ = "strategy_templates"
    __table_args__ = (
        CheckConstraint("version > 0", name="positive_version"),
        CheckConstraint(
            "engine_key IN ('multi_factor', 'ma_cross', 'macd_momentum', "
            "'rsi_reversal', 'bollinger_reversion', 'strategy_dsl')",
            name="supported_engine",
        ),
        CheckConstraint(
            "template_kind IN ('strategy', 'legacy_signal')", name="valid_template_kind"
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
    template_kind: Mapped[str] = mapped_column(
        String(24),
        default="legacy_signal",
        nullable=False,
        comment="模板类型：完整策略 strategy 或旧版指标信号 legacy_signal",
    )
    spec_schema_version: Mapped[int | None] = mapped_column(
        Integer, comment="完整策略 DSL 结构版本；旧版指标信号为空"
    )
    spec_json: Mapped[dict[str, Any] | None] = mapped_column(
        JSON, comment="完整且受约束的系统策略 DSL 定义"
    )
    implementation_version: Mapped[str] = mapped_column(
        String(32), default="legacy_v1", nullable=False, comment="策略求值器实现版本"
    )
    deprecated_at: Mapped[datetime | None] = mapped_column(
        DateTime, comment="模板停止用于新策略的时间（UTC）"
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
            "'rsi_reversal', 'bollinger_reversion', 'strategy_dsl')",
            name="supported_engine",
        ),
        CheckConstraint(
            "strategy_kind IN ('full_strategy', 'legacy_signal')", name="valid_strategy_kind"
        ),
        CheckConstraint(
            "lifecycle_status IN ('draft', 'published', 'retired')",
            name="valid_lifecycle_status",
        ),
        CheckConstraint("risk_level IN ('low', 'medium', 'high')", name="valid_risk_level"),
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
    strategy_kind: Mapped[str] = mapped_column(
        String(24),
        default="legacy_signal",
        nullable=False,
        comment="策略类型：完整策略 full_strategy 或旧版指标信号 legacy_signal",
    )
    lifecycle_status: Mapped[str] = mapped_column(
        String(16),
        default="published",
        nullable=False,
        comment="策略生命周期：draft、published 或 retired",
    )
    spec_schema_version: Mapped[int | None] = mapped_column(
        Integer, comment="完整策略 DSL 结构版本"
    )
    spec_json: Mapped[dict[str, Any] | None] = mapped_column(
        JSON, comment="用户当前完整策略 DSL 定义"
    )
    spec_hash: Mapped[str | None] = mapped_column(
        String(64), comment="规范化策略 DSL 的 SHA-256 哈希"
    )
    risk_level: Mapped[str] = mapped_column(
        String(16), default="medium", nullable=False, comment="策略风险等级：low、medium 或 high"
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
        UniqueConstraint("id", "user_id", name="uq_strategy_revisions_id_user_id"),
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
    spec_schema_version: Mapped[int | None] = mapped_column(
        Integer, comment="该修订采用的策略 DSL 结构版本"
    )
    spec_json: Mapped[dict[str, Any] | None] = mapped_column(
        JSON, comment="该修订不可变的完整策略 DSL 定义"
    )
    spec_hash: Mapped[str | None] = mapped_column(
        String(64), comment="该修订规范化策略 DSL 的 SHA-256 哈希"
    )
    validation_json: Mapped[dict[str, Any] | None] = mapped_column(
        JSON, comment="发布时的静态校验、数据依赖和风险提示"
    )
    published_at: Mapped[datetime | None] = mapped_column(
        DateTime, comment="该修订正式发布时间（UTC）；草稿为空"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, nullable=False, comment="策略修订创建时间（UTC）"
    )

    strategy: Mapped[UserStrategy] = relationship(back_populates="revisions")


class MarketFeatureSnapshot(Base):
    __tablename__ = "market_feature_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "symbol",
            "timeframe",
            "bar_open_time",
            "feature_set_key",
            "feature_set_version",
            "params_hash",
            name="uq_market_feature_snapshots_identity",
        ),
        Index("ix_market_feature_snapshots_symbol_tf_time", "symbol", "timeframe", "bar_open_time"),
        {
            "comment": "系统共享的已收盘行情指标与数据质量快照",
            "mysql_engine": "InnoDB",
            "mysql_charset": "utf8mb4",
        },
    )

    id: Mapped[int] = mapped_column(
        BIGINT_PK, primary_key=True, autoincrement=True, comment="市场特征快照主键"
    )
    symbol: Mapped[str] = mapped_column(String(32), nullable=False, comment="合约代码")
    timeframe: Mapped[str] = mapped_column(String(8), nullable=False, comment="K 线周期")
    bar_open_time: Mapped[int] = mapped_column(
        BigInteger, nullable=False, comment="对应已收盘 K 线开盘时间戳"
    )
    feature_set_key: Mapped[str] = mapped_column(
        String(64), nullable=False, comment="标准特征集合稳定标识"
    )
    feature_set_version: Mapped[int] = mapped_column(
        Integer, nullable=False, comment="标准特征集合实现版本"
    )
    params_hash: Mapped[str] = mapped_column(
        String(64), nullable=False, comment="指标参数规范化 SHA-256 哈希"
    )
    values_json: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, comment="指标和派生市场特征值"
    )
    quality_json: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, comment="行情缺口、陈旧、异常和可用性信息"
    )
    computed_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, nullable=False, comment="特征计算完成时间（UTC）"
    )


class MarketOpportunity(Base):
    __tablename__ = "market_opportunities"
    __table_args__ = (
        CheckConstraint("direction IN ('long', 'short', 'neutral')", name="valid_direction"),
        CheckConstraint(
            "status IN ('detected', 'watching', 'confirmed', 'expired', 'rejected', 'consumed')",
            name="valid_status",
        ),
        UniqueConstraint("public_id", name="uq_market_opportunities_public_id"),
        UniqueConstraint("dedup_key", name="uq_market_opportunities_dedup_key"),
        UniqueConstraint(
            "scanner_key",
            "symbol",
            "direction",
            "current_marker",
            name="uq_market_opportunities_current_scanner_symbol_direction",
        ),
        Index("ix_market_opportunities_status_quality", "status", "quality_score"),
        Index("ix_market_opportunities_symbol_time", "symbol", "detected_bar_time"),
        Index(
            "ix_market_opportunities_current_rank",
            "current_marker",
            "expected_value_score",
            "quality_score",
        ),
        {
            "comment": "系统共享的可解释市场机会及生命周期",
            "mysql_engine": "InnoDB",
            "mysql_charset": "utf8mb4",
        },
    )

    id: Mapped[int] = mapped_column(
        BIGINT_PK, primary_key=True, autoincrement=True, comment="市场机会主键"
    )
    public_id: Mapped[str] = mapped_column(
        String(36), default=lambda: str(uuid.uuid4()), nullable=False, comment="市场机会公开 UUID"
    )
    scanner_key: Mapped[str] = mapped_column(
        String(64), nullable=False, comment="机会扫描器稳定标识"
    )
    scanner_version: Mapped[int] = mapped_column(
        Integer, nullable=False, comment="机会扫描器实现版本"
    )
    symbol: Mapped[str] = mapped_column(String(32), nullable=False, comment="合约代码")
    primary_timeframe: Mapped[str] = mapped_column(String(8), nullable=False, comment="机会主周期")
    direction: Mapped[str] = mapped_column(
        String(12), nullable=False, comment="机会方向：long、short 或 neutral"
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False, comment="机会生命周期状态")
    quality_score: Mapped[Decimal] = mapped_column(
        Numeric(8, 4), nullable=False, comment="机会质量分，仅用于排序和解释"
    )
    current_marker: Mapped[int | None] = mapped_column(
        Integer, comment="当前有效记录为 1；历史记录为空"
    )
    entry_price: Mapped[Decimal | None] = mapped_column(
        Numeric(30, 12), comment="机会发现时的可复现参考价格"
    )
    expected_value_score: Mapped[Decimal | None] = mapped_column(
        Numeric(10, 4), comment="扣除估算成本后的机会排序分"
    )
    detected_bar_time: Mapped[int] = mapped_column(
        BigInteger, nullable=False, comment="首次发现机会的已收盘 K 线时间"
    )
    expires_bar_time: Mapped[int] = mapped_column(
        BigInteger, nullable=False, comment="机会失效的 K 线时间"
    )
    evidence_json: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, comment="机会条件、指标值和解释证据"
    )
    dedup_key: Mapped[str] = mapped_column(
        String(191), nullable=False, comment="机会事件幂等去重键"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, nullable=False, comment="机会创建时间（UTC）"
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=utcnow,
        onupdate=utcnow,
        nullable=False,
        comment="机会最后状态更新时间（UTC）",
    )


class MarketMicrostructure(Base):
    __tablename__ = "market_microstructure"
    __table_args__ = (
        Index("ix_market_microstructure_received", "received_at"),
        {"comment": "Binance WebSocket 聚合后的标的级实时微观结构快照"},
    )

    symbol: Mapped[str] = mapped_column(String(32), primary_key=True)
    event_time: Mapped[int] = mapped_column(BigInteger, nullable=False)
    received_at: Mapped[int] = mapped_column(BigInteger, nullable=False)
    bid_price: Mapped[Decimal | None] = mapped_column(Numeric(30, 12))
    ask_price: Mapped[Decimal | None] = mapped_column(Numeric(30, 12))
    mid_price: Mapped[Decimal | None] = mapped_column(Numeric(30, 12))
    spread_bps: Mapped[Decimal | None] = mapped_column(Numeric(16, 6))
    book_imbalance: Mapped[Decimal | None] = mapped_column(Numeric(16, 8))
    aggressive_buy_ratio: Mapped[Decimal | None] = mapped_column(Numeric(16, 8))
    trade_count_60s: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    quote_volume_60s: Mapped[Decimal] = mapped_column(Numeric(30, 8), default=0, nullable=False)
    realized_volatility_60s: Mapped[Decimal | None] = mapped_column(Numeric(20, 10))
    price_velocity_bps_60s: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    window_low_price: Mapped[Decimal | None] = mapped_column(Numeric(30, 12))
    window_low_event_time: Mapped[int | None] = mapped_column(BigInteger)
    window_high_price: Mapped[Decimal | None] = mapped_column(Numeric(30, 12))
    window_high_event_time: Mapped[int | None] = mapped_column(BigInteger)
    quality_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)


class MarketDataQualityEvent(Base):
    __tablename__ = "market_data_quality_events"
    __table_args__ = (
        CheckConstraint("severity IN ('info','warning','critical')", name="valid_severity"),
        Index("ix_market_data_quality_events_time", "event_time", "severity"),
        {"comment": "断线、过期、丢包和异常行情等数据质量事件"},
    )

    id: Mapped[int] = mapped_column(BIGINT_PK, primary_key=True, autoincrement=True)
    stream_key: Mapped[str] = mapped_column(String(64), nullable=False)
    symbol: Mapped[str | None] = mapped_column(String(32))
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    severity: Mapped[str] = mapped_column(String(16), nullable=False)
    event_time: Mapped[int] = mapped_column(BigInteger, nullable=False)
    details_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)


class OpportunityEvent(Base):
    __tablename__ = "opportunity_events"
    __table_args__ = (
        UniqueConstraint("event_key", name="uq_opportunity_events_event_key"),
        Index("ix_opportunity_events_opportunity_time", "opportunity_id", "event_time"),
        {"comment": "机会状态变化的只追加审计事件"},
    )

    id: Mapped[int] = mapped_column(BIGINT_PK, primary_key=True, autoincrement=True)
    opportunity_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("market_opportunities.id", ondelete="CASCADE"), nullable=False
    )
    event_key: Mapped[str] = mapped_column(String(191), nullable=False)
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    previous_status: Mapped[str | None] = mapped_column(String(16))
    next_status: Mapped[str] = mapped_column(String(16), nullable=False)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    event_time: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)


class OpportunityOutcome(Base):
    __tablename__ = "opportunity_outcomes"
    __table_args__ = (
        CheckConstraint("status IN ('pending','completed','unavailable')", name="valid_status"),
        CheckConstraint("direction IN ('long','short','neutral')", name="valid_direction"),
        UniqueConstraint(
            "opportunity_id", "horizon_seconds", name="uq_opportunity_outcomes_horizon"
        ),
        Index("ix_opportunity_outcomes_status_due", "status", "due_at"),
        {"comment": "所有候选机会在多个未来周期上的收益、MFE/MAE与命中结果"},
    )

    id: Mapped[int] = mapped_column(BIGINT_PK, primary_key=True, autoincrement=True)
    opportunity_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("market_opportunities.id", ondelete="CASCADE"), nullable=False
    )
    horizon_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="pending", nullable=False)
    direction: Mapped[str] = mapped_column(String(12), nullable=False)
    entry_price: Mapped[Decimal] = mapped_column(Numeric(30, 12), nullable=False)
    exit_price: Mapped[Decimal | None] = mapped_column(Numeric(30, 12))
    raw_return_bps: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    directional_return_bps: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    max_favorable_bps: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    max_adverse_bps: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    target_bps: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    stop_bps: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    hit_result: Mapped[str | None] = mapped_column(String(16))
    due_at: Mapped[int] = mapped_column(BigInteger, nullable=False)
    completed_at: Mapped[int | None] = mapped_column(BigInteger)
    cost_bps: Mapped[Decimal] = mapped_column(Numeric(20, 8), default=0, nullable=False)
    context_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow, nullable=False
    )


class UserOpportunityState(Base):
    __tablename__ = "user_opportunity_states"
    __table_args__ = (
        CheckConstraint("state IN ('watching', 'ignored')", name="valid_state"),
        Index("ix_user_opportunity_states_user_state", "user_id", "state", "updated_at"),
        {
            "comment": "用户对公共市场机会的关注、忽略和提醒偏好",
            "mysql_engine": "InnoDB",
            "mysql_charset": "utf8mb4",
        },
    )

    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
        comment="所属用户 ID，用于租户隔离",
    )
    opportunity_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("market_opportunities.id", ondelete="CASCADE"),
        primary_key=True,
        comment="用户关注或忽略的公共市场机会 ID",
    )
    state: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="用户状态：watching 关注或 ignored 忽略"
    )
    notify_enabled: Mapped[bool] = mapped_column(
        Boolean, default=True, nullable=False, comment="该机会状态更新时是否生成用户提醒"
    )
    last_viewed_at: Mapped[datetime | None] = mapped_column(
        DateTime, comment="用户最后查看该机会证据的时间（UTC）"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, nullable=False, comment="用户机会状态创建时间（UTC）"
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=utcnow,
        onupdate=utcnow,
        nullable=False,
        comment="用户机会状态最后更新时间（UTC）",
    )


class StrategyDeployment(Base):
    __tablename__ = "strategy_deployments"
    __table_args__ = (
        CheckConstraint("mode IN ('backtest', 'paper', 'shadow', 'live')", name="valid_mode"),
        CheckConstraint(
            "status IN ('created', 'running', 'paused', 'stopped', 'error')",
            name="valid_status",
        ),
        ForeignKeyConstraint(
            ["strategy_id", "user_id"],
            ["user_strategies.id", "user_strategies.user_id"],
            name="fk_strategy_deployments_strategy_tenant",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["strategy_revision_id", "user_id"],
            ["strategy_revisions.id", "strategy_revisions.user_id"],
            name="fk_strategy_deployments_revision_tenant",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("public_id", name="uq_strategy_deployments_public_id"),
        UniqueConstraint("id", "user_id", name="uq_strategy_deployments_id_user_id"),
        Index("ix_strategy_deployments_user_status", "user_id", "status", "updated_at"),
        {
            "comment": "用户将固定策略修订绑定到回测、模拟、影子或实盘的部署实例",
            "mysql_engine": "InnoDB",
            "mysql_charset": "utf8mb4",
        },
    )

    id: Mapped[int] = mapped_column(
        BIGINT_PK, primary_key=True, autoincrement=True, comment="策略部署内部主键"
    )
    public_id: Mapped[str] = mapped_column(
        String(36), default=lambda: str(uuid.uuid4()), nullable=False, comment="策略部署公开 UUID"
    )
    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        comment="所属用户 ID",
    )
    strategy_id: Mapped[int] = mapped_column(
        BigInteger, nullable=False, comment="所属用户策略内部 ID"
    )
    strategy_revision_id: Mapped[int] = mapped_column(
        BigInteger, nullable=False, comment="部署固定的不可变策略修订 ID"
    )
    mode: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="部署模式：回测、模拟、影子或实盘"
    )
    target_account_id: Mapped[int | None] = mapped_column(
        BigInteger, comment="模拟盘或实盘目标账户内部 ID"
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False, comment="策略部署显示名称")
    status: Mapped[str] = mapped_column(
        String(16), default="created", nullable=False, comment="策略部署运行状态"
    )
    universe_override_json: Mapped[dict[str, Any] | None] = mapped_column(
        JSON, comment="部署级交易标的范围覆盖"
    )
    risk_override_json: Mapped[dict[str, Any] | None] = mapped_column(
        JSON, comment="部署级仅收紧风险参数覆盖"
    )
    runtime_state_json: Mapped[dict[str, Any]] = mapped_column(
        JSON, default=dict, nullable=False, comment="机会、冷却和幂等等策略运行状态"
    )
    last_evaluated_bar_time: Mapped[int | None] = mapped_column(
        BigInteger, comment="最后完成求值的 K 线时间"
    )
    last_error_code: Mapped[str | None] = mapped_column(
        String(64), comment="最后一次脱敏运行错误代码"
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime, comment="策略部署启动时间（UTC）")
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, nullable=False, comment="策略部署创建时间（UTC）"
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=utcnow,
        onupdate=utcnow,
        nullable=False,
        comment="策略部署最后更新时间（UTC）",
    )


class StrategySignal(Base):
    __tablename__ = "strategy_signals"
    __table_args__ = (
        CheckConstraint(
            "decision IN ('LONG_ENTRY', 'SHORT_ENTRY', 'EXIT', 'HOLD', 'SKIP')",
            name="valid_decision",
        ),
        CheckConstraint(
            "status IN ('proposed', 'risk_rejected', 'approved', 'expired', 'executed')",
            name="valid_status",
        ),
        ForeignKeyConstraint(
            ["deployment_id", "user_id"],
            ["strategy_deployments.id", "strategy_deployments.user_id"],
            name="fk_strategy_signals_deployment_tenant",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["strategy_revision_id", "user_id"],
            ["strategy_revisions.id", "strategy_revisions.user_id"],
            name="fk_strategy_signals_revision_tenant",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["opportunity_id"],
            ["market_opportunities.id"],
            name="fk_strategy_signals_opportunity",
            ondelete="SET NULL",
        ),
        UniqueConstraint("public_id", name="uq_strategy_signals_public_id"),
        UniqueConstraint("idempotency_key", name="uq_strategy_signals_idempotency_key"),
        Index("ix_strategy_signals_user_created", "user_id", "created_at"),
        Index("ix_strategy_signals_deployment_bar", "deployment_id", "signal_bar_time"),
        {
            "comment": "用户策略求值产生的可解释信号及风控审批结果",
            "mysql_engine": "InnoDB",
            "mysql_charset": "utf8mb4",
        },
    )

    id: Mapped[int] = mapped_column(
        BIGINT_PK, primary_key=True, autoincrement=True, comment="策略信号主键"
    )
    public_id: Mapped[str] = mapped_column(
        String(36), default=lambda: str(uuid.uuid4()), nullable=False, comment="策略信号公开 UUID"
    )
    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        comment="所属用户 ID",
    )
    deployment_id: Mapped[int] = mapped_column(
        BigInteger, nullable=False, comment="产生信号的策略部署 ID"
    )
    strategy_revision_id: Mapped[int] = mapped_column(
        BigInteger, nullable=False, comment="产生信号的不可变策略修订 ID"
    )
    opportunity_id: Mapped[int | None] = mapped_column(BigInteger, comment="关联的公共市场机会 ID")
    symbol: Mapped[str] = mapped_column(String(32), nullable=False, comment="合约代码")
    timeframe: Mapped[str] = mapped_column(String(8), nullable=False, comment="信号触发周期")
    signal_bar_time: Mapped[int] = mapped_column(
        BigInteger, nullable=False, comment="信号对应的已收盘 K 线时间"
    )
    decision: Mapped[str] = mapped_column(String(24), nullable=False, comment="结构化策略决策代码")
    confidence: Mapped[Decimal | None] = mapped_column(
        Numeric(8, 4), comment="规则置信度，仅用于排序和解释"
    )
    status: Mapped[str] = mapped_column(
        String(24), default="proposed", nullable=False, comment="信号审批与执行状态"
    )
    valid_until: Mapped[datetime | None] = mapped_column(DateTime, comment="策略信号有效期（UTC）")
    reason_codes_json: Mapped[list[str]] = mapped_column(
        JSON, nullable=False, comment="稳定且可检索的决策原因代码"
    )
    evidence_json: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, comment="参与策略决策的指标和条件证据"
    )
    risk_decision_json: Mapped[dict[str, Any] | None] = mapped_column(
        JSON, comment="账户与组合风控的批准或拒绝结果"
    )
    idempotency_key: Mapped[str] = mapped_column(
        String(191), nullable=False, comment="部署、标的、修订、K 线和决策组成的幂等键"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, nullable=False, comment="策略信号创建时间（UTC）"
    )


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
    balance: Mapped[Decimal] = mapped_column(Numeric(30, 8), nullable=False, comment="当前可用余额")
    config_json: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, comment="杠杆、仓位和风控配置"
    )
    strategy_snapshot_json: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, comment="创建或切换策略时的完整策略快照"
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, nullable=False, comment="本轮模拟盘启动时间"
    )
    last_tick_at: Mapped[datetime | None] = mapped_column(DateTime, comment="最后一次策略执行时间")
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


class WorkerLeaseRecord(Base):
    __tablename__ = "worker_leases"
    __table_args__ = (
        Index("ix_worker_leases_expires_at", "expires_at"),
        {"comment": "后台 Worker 角色的可续租单实例运行权"},
    )

    worker_key: Mapped[str] = mapped_column(String(100), primary_key=True, comment="Worker 角色键")
    owner_id: Mapped[str] = mapped_column(String(36), nullable=False, comment="本次进程租约 UUID")
    metadata_json: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, comment="主机名、进程号等非敏感运行信息"
    )
    acquired_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, comment="取得租约时间")
    heartbeat_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, comment="最后心跳时间")
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, comment="租约失效时间")


class ExchangeAccount(Base):
    __tablename__ = "exchange_accounts"
    __table_args__ = (
        CheckConstraint("environment IN ('demo', 'live')", name="valid_environment"),
        CheckConstraint(
            "status IN ('disabled', 'read_only', 'shadow', 'canary', 'enabled')",
            name="valid_status",
        ),
        UniqueConstraint("public_id", name="uq_exchange_accounts_public_id"),
        UniqueConstraint("id", "user_id", name="uq_exchange_accounts_id_user_id"),
        UniqueConstraint("user_id", "name", name="uq_exchange_accounts_user_name"),
        Index("ix_exchange_accounts_user_status", "user_id", "status"),
        {"comment": "用户隔离的交易所账户与分阶段执行门禁"},
    )

    id: Mapped[int] = mapped_column(BIGINT_PK, primary_key=True, autoincrement=True)
    public_id: Mapped[str] = mapped_column(
        String(36), default=lambda: str(uuid.uuid4()), nullable=False
    )
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    venue: Mapped[str] = mapped_column(String(32), default="binance", nullable=False)
    account_type: Mapped[str] = mapped_column(
        String(32), default="portfolio_margin", nullable=False
    )
    environment: Mapped[str] = mapped_column(String(16), default="demo", nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="disabled", nullable=False)
    credential_fingerprint: Mapped[str | None] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow, nullable=False
    )


class RiskDecisionRecord(Base):
    __tablename__ = "risk_decisions"
    __table_args__ = (
        CheckConstraint("decision IN ('approved', 'rejected')", name="valid_decision"),
        UniqueConstraint("public_id", name="uq_risk_decisions_public_id"),
        Index("ix_risk_decisions_user_created", "user_id", "created_at"),
        {"comment": "不可变的交易前风控审批证据"},
    )

    id: Mapped[int] = mapped_column(BIGINT_PK, primary_key=True, autoincrement=True)
    public_id: Mapped[str] = mapped_column(
        String(36), default=lambda: str(uuid.uuid4()), nullable=False
    )
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    exchange_account_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("exchange_accounts.id", ondelete="CASCADE"), nullable=False
    )
    subject_type: Mapped[str] = mapped_column(String(32), nullable=False)
    subject_id: Mapped[str] = mapped_column(String(64), nullable=False)
    decision: Mapped[str] = mapped_column(String(16), nullable=False)
    reason_codes_json: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    limits_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    exposure_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)


class OrderIntent(Base):
    __tablename__ = "order_intents"
    __table_args__ = (
        CheckConstraint("side IN ('buy', 'sell')", name="valid_side"),
        CheckConstraint("quantity > 0", name="positive_quantity"),
        UniqueConstraint("public_id", name="uq_order_intents_public_id"),
        UniqueConstraint(
            "exchange_account_id", "idempotency_key", name="uq_order_intents_account_idempotency"
        ),
        ForeignKeyConstraint(
            ["exchange_account_id", "user_id"],
            ["exchange_accounts.id", "exchange_accounts.user_id"],
            name="fk_order_intents_account_tenant",
            ondelete="CASCADE",
        ),
        Index("ix_order_intents_user_state_created", "user_id", "state", "created_at"),
        {"comment": "通过风控前后均可审计的幂等订单意图"},
    )

    id: Mapped[int] = mapped_column(BIGINT_PK, primary_key=True, autoincrement=True)
    public_id: Mapped[str] = mapped_column(
        String(36), default=lambda: str(uuid.uuid4()), nullable=False
    )
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    exchange_account_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    deployment_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("strategy_deployments.id", ondelete="SET NULL")
    )
    signal_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("strategy_signals.id", ondelete="SET NULL")
    )
    risk_decision_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("risk_decisions.id", ondelete="RESTRICT")
    )
    idempotency_key: Mapped[str] = mapped_column(String(191), nullable=False)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    side: Mapped[str] = mapped_column(String(8), nullable=False)
    position_side: Mapped[str] = mapped_column(String(8), default="BOTH", nullable=False)
    order_type: Mapped[str] = mapped_column(String(24), nullable=False)
    time_in_force: Mapped[str | None] = mapped_column(String(8))
    quantity: Mapped[Decimal] = mapped_column(Numeric(48, 18), nullable=False)
    price: Mapped[Decimal | None] = mapped_column(Numeric(30, 12))
    stop_price: Mapped[Decimal | None] = mapped_column(Numeric(30, 12))
    leverage: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    reduce_only: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    state: Mapped[str] = mapped_column(String(24), default="created", nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime)
    error_code: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow, nullable=False
    )


class ExchangeOrder(Base):
    __tablename__ = "exchange_orders"
    __table_args__ = (
        UniqueConstraint("intent_id", name="uq_exchange_orders_intent_id"),
        UniqueConstraint(
            "exchange_account_id", "exchange_order_id", name="uq_exchange_orders_account_order"
        ),
        UniqueConstraint(
            "exchange_account_id", "client_order_id", name="uq_exchange_orders_account_client"
        ),
        Index("ix_exchange_orders_account_state", "exchange_account_id", "state"),
        {"comment": "交易所确认后的订单当前状态快照"},
    )

    id: Mapped[int] = mapped_column(BIGINT_PK, primary_key=True, autoincrement=True)
    intent_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("order_intents.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    exchange_account_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("exchange_accounts.id", ondelete="CASCADE"), nullable=False
    )
    exchange_order_id: Mapped[str | None] = mapped_column(String(64))
    client_order_id: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(String(24), nullable=False)
    requested_quantity: Mapped[Decimal] = mapped_column(Numeric(48, 18), nullable=False)
    filled_quantity: Mapped[Decimal] = mapped_column(Numeric(48, 18), default=0, nullable=False)
    average_price: Mapped[Decimal | None] = mapped_column(Numeric(30, 12))
    raw_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)


class OrderEventRecord(Base):
    __tablename__ = "order_events"
    __table_args__ = (
        UniqueConstraint("event_key", name="uq_order_events_event_key"),
        Index("ix_order_events_order_exchange_ts", "exchange_order_id", "exchange_ts"),
        {"comment": "只追加的订单状态与交易所回报事件"},
    )

    id: Mapped[int] = mapped_column(BIGINT_PK, primary_key=True, autoincrement=True)
    exchange_order_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("exchange_orders.id", ondelete="CASCADE"), nullable=False
    )
    event_key: Mapped[str] = mapped_column(String(191), nullable=False)
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    previous_state: Mapped[str | None] = mapped_column(String(24))
    next_state: Mapped[str] = mapped_column(String(24), nullable=False)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    exchange_ts: Mapped[datetime | None] = mapped_column(DateTime)
    received_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)


class FillRecord(Base):
    __tablename__ = "fills"
    __table_args__ = (
        CheckConstraint("quantity > 0", name="positive_quantity"),
        CheckConstraint("price > 0", name="positive_price"),
        UniqueConstraint("exchange_account_id", "exchange_trade_id", name="uq_fills_account_trade"),
        Index("ix_fills_user_filled_at", "user_id", "filled_at"),
        {"comment": "交易所逐笔成交事实，不允许覆盖历史成交"},
    )

    id: Mapped[int] = mapped_column(BIGINT_PK, primary_key=True, autoincrement=True)
    exchange_order_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("exchange_orders.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    exchange_account_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("exchange_accounts.id", ondelete="CASCADE"), nullable=False
    )
    exchange_trade_id: Mapped[str] = mapped_column(String(64), nullable=False)
    price: Mapped[Decimal] = mapped_column(Numeric(30, 12), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Numeric(48, 18), nullable=False)
    commission: Mapped[Decimal] = mapped_column(Numeric(30, 12), default=0, nullable=False)
    commission_asset: Mapped[str | None] = mapped_column(String(16))
    realized_pnl: Mapped[Decimal | None] = mapped_column(Numeric(30, 12))
    filled_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    raw_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)


class OutboxEvent(Base):
    __tablename__ = "outbox_events"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'publishing', 'published', 'failed')", name="valid_status"
        ),
        UniqueConstraint("event_key", name="uq_outbox_events_event_key"),
        Index("ix_outbox_events_status_available", "status", "available_at"),
        {"comment": "业务事务与异步消息可靠衔接的 Outbox 事件"},
    )

    id: Mapped[int] = mapped_column(BIGINT_PK, primary_key=True, autoincrement=True)
    event_key: Mapped[str] = mapped_column(String(191), nullable=False)
    aggregate_type: Mapped[str] = mapped_column(String(64), nullable=False)
    aggregate_id: Mapped[str] = mapped_column(String(64), nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="pending", nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    available_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    published_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_error_code: Mapped[str | None] = mapped_column(String(64))
