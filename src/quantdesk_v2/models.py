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
    Date,
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
from sqlalchemy.dialects import mysql
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
MODEL_RAW_TEXT = Text().with_variant(mysql.LONGTEXT(), "mysql")

NEWS_DEDUP_EXPRESSION = (
    "CASE WHEN link IS NULL THEN NULL "
    "ELSE SHA2(CONCAT(COALESCE(source, ''), CHAR(0), link), 256) END"
)


class News(Base):
    __tablename__ = "news"
    __table_args__ = (
        Index("ix_news_ts", "ts"),
        Index("ix_news_ai_pending_ts", "ai_analyzed_at", "ts"),
        Index(
            "ix_news_ai_claim_pending",
            "ai_claim_batch_id",
            "ai_analyzed_at",
            "ts",
        ),
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
    rule_sentiment: Mapped[str | None] = mapped_column(
        String(32), comment="关键词规则产生的原始情绪分类"
    )
    summary: Mapped[str | None] = mapped_column(Text, comment="新闻摘要或深度舆情摘要")
    related_us_stocks: Mapped[list[dict[str, Any]] | None] = mapped_column(
        JSON, comment="AI 识别的关联美股、相关度与影响方向"
    )
    related_industries: Mapped[list[dict[str, Any]] | None] = mapped_column(
        JSON, comment="AI 识别的关联行业、相关度与影响方向"
    )
    ai_sentiment: Mapped[str | None] = mapped_column(
        String(32), comment="AI 语义研判情绪"
    )
    ai_confidence: Mapped[Decimal | None] = mapped_column(
        Numeric(5, 4), comment="AI 情绪置信度，范围 0 到 1"
    )
    ai_impact_strength: Mapped[str | None] = mapped_column(
        String(16), comment="AI 判断的影响强度"
    )
    ai_time_horizon: Mapped[str | None] = mapped_column(
        String(32), comment="AI 判断的影响周期"
    )
    ai_category: Mapped[str | None] = mapped_column(
        String(32), comment="AI 判断的新闻类别"
    )
    ai_reason: Mapped[str | None] = mapped_column(Text, comment="AI 情绪和关联标的判断依据")
    ai_model: Mapped[str | None] = mapped_column(String(128), comment="执行分析的模型标识")
    ai_batch_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("news_ai_batches.id", ondelete="SET NULL"), comment="AI 分析批次"
    )
    ai_claim_batch_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey(
            "news_ai_batches.id",
            ondelete="SET NULL",
            name="fk_news_ai_claim_batch_id_news_ai_batches",
        ),
        comment="当前原子领取该新闻的 AI 分析批次",
    )
    ai_claimed_at: Mapped[datetime | None] = mapped_column(
        DateTime, comment="AI 分析领取时间（UTC）"
    )
    ai_analyzed_at: Mapped[datetime | None] = mapped_column(
        DateTime, comment="AI 分析完成时间（UTC）"
    )
    source_link_hash: Mapped[str | None] = mapped_column(
        String(64),
        Computed(NEWS_DEDUP_EXPRESSION, persisted=True),
        comment="来源名称与原文链接生成的 SHA-256 去重键",
    )


class NewsAiBatch(Base):
    __tablename__ = "news_ai_batches"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'running', 'completed', 'partial', 'failed')",
            name="valid_status",
        ),
        CheckConstraint(
            "requested_count IN (10, 300, 500)",
            name="valid_requested_count_v2",
        ),
        CheckConstraint(
            "selected_count >= 0 AND processed_count >= 0 AND failed_count >= 0",
            name="nonnegative_counts",
        ),
        Index("ix_news_ai_batches_created", "created_at"),
        Index("ix_news_ai_batches_status", "status", "updated_at"),
        {
            "comment": "管理员发起的批量新闻 AI 研判任务与市场汇总结论",
            "mysql_engine": "InnoDB",
            "mysql_charset": "utf8mb4",
        },
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4()), comment="批次 UUID"
    )
    started_by: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("users.id", ondelete="SET NULL"),
        comment="发起分析的管理员用户 ID",
    )
    status: Mapped[str] = mapped_column(
        String(16), default="pending", nullable=False, comment="批次执行状态"
    )
    requested_count: Mapped[int] = mapped_column(
        Integer, nullable=False, comment="请求分析的最近新闻数量"
    )
    selected_count: Mapped[int] = mapped_column(
        Integer, default=0, nullable=False, comment="实际选中的新闻数量"
    )
    processed_count: Mapped[int] = mapped_column(
        Integer, default=0, nullable=False, comment="成功完成 AI 研判的新闻数量"
    )
    failed_count: Mapped[int] = mapped_column(
        Integer, default=0, nullable=False, comment="研判失败的新闻数量"
    )
    chunk_size: Mapped[int] = mapped_column(
        Integer, default=10, nullable=False, comment="单次模型调用包含的新闻数量"
    )
    provider_code: Mapped[str | None] = mapped_column(String(32), comment="AI 服务商代码")
    model_name: Mapped[str | None] = mapped_column(String(128), comment="模型标识")
    market_sentiment: Mapped[str | None] = mapped_column(
        String(32), comment="批次整体美股情绪结论"
    )
    market_confidence: Mapped[Decimal | None] = mapped_column(
        Numeric(5, 4), comment="批次整体结论置信度"
    )
    market_summary: Mapped[str | None] = mapped_column(Text, comment="批次整体市场结论")
    result_json: Mapped[dict[str, Any] | None] = mapped_column(
        JSON, comment="关键驱动、重点美股与模型汇总返回"
    )
    error_message: Mapped[str | None] = mapped_column(Text, comment="脱敏后的最近错误")
    started_at: Mapped[datetime | None] = mapped_column(DateTime, comment="开始时间（UTC）")
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, comment="完成时间（UTC）")
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, nullable=False, comment="创建时间（UTC）"
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=utcnow,
        onupdate=utcnow,
        nullable=False,
        comment="最后更新时间（UTC）",
    )


class NewsAiModelCall(Base):
    __tablename__ = "news_ai_model_calls"
    __table_args__ = (
        CheckConstraint("call_type IN ('analysis', 'summary')", name="valid_call_type"),
        CheckConstraint("status IN ('completed', 'failed')", name="valid_status"),
        Index("ix_news_ai_model_calls_batch", "batch_id", "created_at"),
        {
            "comment": "新闻 AI 模型调用的提示词、请求参数与原始响应审计记录",
            "mysql_engine": "InnoDB",
            "mysql_charset": "utf8mb4",
        },
    )

    id: Mapped[int] = mapped_column(
        BIGINT_PK, primary_key=True, autoincrement=True, comment="模型调用记录主键"
    )
    batch_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("news_ai_batches.id", ondelete="CASCADE"),
        nullable=False,
        comment="所属新闻 AI 批次",
    )
    call_type: Mapped[str] = mapped_column(
        String(16), nullable=False, default="analysis", comment="调用类型"
    )
    attempt_depth: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, comment="结构恢复拆分重试深度"
    )
    provider_code: Mapped[str] = mapped_column(
        String(32), nullable=False, comment="AI 服务商代码"
    )
    model_name: Mapped[str] = mapped_column(
        String(128), nullable=False, comment="模型标识"
    )
    news_ids_json: Mapped[list[str]] = mapped_column(
        JSON, nullable=False, comment="该次调用包含的新闻稳定 ID"
    )
    request_json: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, comment="实际发送给模型的请求体，不包含认证密钥"
    )
    response_text: Mapped[str | None] = mapped_column(
        MODEL_RAW_TEXT, comment="模型 message content 原始文本"
    )
    response_envelope: Mapped[str | None] = mapped_column(
        MODEL_RAW_TEXT, comment="AI 服务商返回的完整 HTTP 响应正文"
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="调用完成或失败状态"
    )
    error_category: Mapped[str | None] = mapped_column(
        String(32), comment="稳定且脱敏的错误类别"
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, comment="调用开始时间（UTC）"
    )
    completed_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, comment="调用结束时间（UTC）"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, nullable=False, comment="审计记录创建时间（UTC）"
    )


class NewsAiModelCallItem(Base):
    __tablename__ = "news_ai_model_call_items"
    __table_args__ = (
        Index("ix_news_ai_model_call_items_news", "news_id", "call_id"),
        {
            "comment": "模型调用与新闻记录的可查询关联",
            "mysql_engine": "InnoDB",
            "mysql_charset": "utf8mb4",
        },
    )

    call_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("news_ai_model_calls.id", ondelete="CASCADE"),
        primary_key=True,
        comment="模型调用记录主键",
    )
    news_id: Mapped[str] = mapped_column(
        String(255),
        ForeignKey("news.id", ondelete="CASCADE"),
        primary_key=True,
        comment="新闻稳定 ID",
    )


class NewsAiAnalysisRecord(Base):
    __tablename__ = "news_ai_analysis_records"
    __table_args__ = (
        CheckConstraint(
            "direction IN ('bull', 'neutral', 'bear')",
            name="valid_direction",
        ),
        CheckConstraint(
            "memory_effect IN ('initial', 'maintain', 'strengthen', 'weaken', 'reverse')",
            name="valid_memory_effect",
        ),
        CheckConstraint(
            "position_effect IS NULL OR position_effect IN "
            "('hold', 'strengthen', 'caution', 'exit', 'reverse')",
            name="valid_position_effect",
        ),
        UniqueConstraint(
            "batch_id",
            "news_id",
            "symbol",
            name="uq_news_ai_analysis_record_batch_news_symbol",
        ),
        Index(
            "ix_news_ai_analysis_records_user_symbol_time",
            "user_id",
            "symbol",
            "analyzed_at",
        ),
        Index(
            "ix_news_ai_analysis_records_news",
            "news_id",
            "analyzed_at",
        ),
        {
            "comment": "美股新闻 AI 一周滚动研判记忆与判断变化记录",
            "mysql_engine": "InnoDB",
            "mysql_charset": "utf8mb4",
        },
    )

    id: Mapped[int] = mapped_column(
        BIGINT_PK, primary_key=True, autoincrement=True, comment="新闻研判记忆主键"
    )
    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        comment="产生该研判记录的用户",
    )
    batch_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("news_ai_batches.id", ondelete="CASCADE"),
        nullable=False,
        comment="所属新闻 AI 分析批次",
    )
    news_id: Mapped[str] = mapped_column(
        String(255),
        ForeignKey("news.id", ondelete="CASCADE"),
        nullable=False,
        comment="本次研判对应的新闻",
    )
    symbol: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="关联美股代码"
    )
    direction: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="本次对股票的影响方向"
    )
    confidence: Mapped[Decimal] = mapped_column(
        Numeric(5, 4), nullable=False, comment="新闻研判置信度"
    )
    relevance: Mapped[Decimal] = mapped_column(
        Numeric(5, 4), nullable=False, comment="新闻与股票的关联度"
    )
    impact_strength: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="影响强度"
    )
    time_horizon: Mapped[str] = mapped_column(
        String(32), nullable=False, comment="影响周期"
    )
    category: Mapped[str] = mapped_column(
        String(32), nullable=False, comment="新闻类别"
    )
    analysis_reason: Mapped[str] = mapped_column(
        Text, nullable=False, comment="本次新闻研判依据"
    )
    memory_effect: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="相对历史判断的变化类型"
    )
    memory_reason: Mapped[str] = mapped_column(
        Text, nullable=False, comment="本次新闻如何影响历史判断"
    )
    judgment_basis_json: Mapped[dict[str, Any] | None] = mapped_column(
        JSON,
        comment="结构化研判依据：事实、影响传导、支持/反向证据及不确定性",
    )
    position_effect: Mapped[str | None] = mapped_column(
        String(16), comment="本次新闻对未结算研究持仓的建议影响"
    )
    position_reason: Mapped[str | None] = mapped_column(
        Text, comment="本次新闻影响研究持仓的判断依据"
    )
    previous_direction: Mapped[str | None] = mapped_column(
        String(16), comment="前序记忆记录的方向"
    )
    previous_confidence: Mapped[Decimal | None] = mapped_column(
        Numeric(5, 4), comment="前序记忆记录的置信度"
    )
    prior_record_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey(
            "news_ai_analysis_records.id",
            ondelete="SET NULL",
            name="fk_news_ai_memory_prior_record",
        ),
        comment="直接承接的前序新闻研判记录",
    )
    context_record_ids_json: Mapped[list[int]] = mapped_column(
        JSON, nullable=False, comment="本次模型实际收到的历史记忆记录 ID"
    )
    model_name: Mapped[str] = mapped_column(
        String(128), nullable=False, comment="执行研判的 AI 模型"
    )
    news_published_at: Mapped[int] = mapped_column(
        BigInteger, nullable=False, comment="新闻发布时间 Unix 时间戳"
    )
    analyzed_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, comment="本次研判完成时间（UTC）"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, nullable=False, comment="记录创建时间（UTC）"
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
    live_accounts: Mapped[list[LiveTradingAccount]] = relationship(
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
        UniqueConstraint(
            "user_id", "display_name", name="uq_ai_model_configs_user_display_name"
        ),
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
    model_name: Mapped[str] = mapped_column(
        String(128), nullable=False, comment="服务商模型标识"
    )
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


class AiMonitorConfig(Base):
    __tablename__ = "ai_monitor_configs"
    __table_args__ = (
        CheckConstraint(
            "news_interval_minutes BETWEEN 5 AND 1440",
            name="valid_news_interval",
        ),
        CheckConstraint(
            "opportunity_interval_minutes BETWEEN 5 AND 1440",
            name="valid_opportunity_interval",
        ),
        CheckConstraint(
            "news_lookback_hours BETWEEN 1 AND 168",
            name="valid_news_lookback",
        ),
        CheckConstraint("timeframe IN ('15m', '1h', '4h')", name="valid_timeframe"),
        CheckConstraint(
            "prediction_max_holding_bars BETWEEN 1 AND 24",
            name="valid_prediction_max_holding_bars",
        ),
        CheckConstraint(
            "minimum_news_confidence BETWEEN 0 AND 1",
            name="valid_news_confidence",
        ),
        CheckConstraint(
            "minimum_news_mentions BETWEEN 1 AND 20",
            name="valid_news_mentions",
        ),
        CheckConstraint(
            "minimum_indicator_score BETWEEN 0 AND 100",
            name="valid_minimum_indicator_score",
        ),
        CheckConstraint(
            "minimum_combined_score BETWEEN 0 AND 100",
            name="valid_minimum_combined_score",
        ),
        CheckConstraint(
            "maximum_market_age_seconds BETWEEN 5 AND 3600",
            name="valid_maximum_market_age",
        ),
        CheckConstraint(
            "minimum_feature_quality BETWEEN 0 AND 1",
            name="valid_minimum_feature_quality",
        ),
        CheckConstraint(
            "minimum_market_flow_quality BETWEEN 0 AND 1",
            name="valid_minimum_market_flow_quality",
        ),
        CheckConstraint(
            "minimum_calibration_samples BETWEEN 30 AND 5000",
            name="valid_minimum_calibration_samples",
        ),
        CheckConstraint(
            "live_safety_margin_bps BETWEEN 0 AND 500",
            name="valid_live_safety_margin",
        ),
        CheckConstraint(
            "news_score_weight BETWEEN 0 AND 100",
            name="valid_news_score_weight",
        ),
        CheckConstraint(
            "technical_score_weight BETWEEN 0 AND 100",
            name="valid_technical_score_weight",
        ),
        CheckConstraint(
            "market_flow_score_weight BETWEEN 0 AND 100",
            name="valid_market_flow_score_weight",
        ),
        CheckConstraint(
            "news_score_weight + technical_score_weight + market_flow_score_weight = 100",
            name="valid_score_weight_total",
        ),
        CheckConstraint(
            "prediction_fee_bps_per_side BETWEEN 0 AND 500",
            name="valid_prediction_fee_bps",
        ),
        CheckConstraint(
            "prediction_slippage_bps_per_side BETWEEN 0 AND 500",
            name="valid_prediction_slippage_bps",
        ),
        CheckConstraint(
            "prediction_funding_bps_per_8h BETWEEN 0 AND 500",
            name="valid_prediction_funding_bps",
        ),
        Index("ix_ai_monitor_configs_enabled", "enabled", "updated_at"),
        {
            "comment": "用户隔离的 AI 新闻与技术指标机会扫描配置",
            "mysql_engine": "InnoDB",
            "mysql_charset": "utf8mb4",
        },
    )

    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
        comment="所属用户 ID",
    )
    enabled: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False, comment="是否启用后台周期分析"
    )
    news_interval_minutes: Mapped[int] = mapped_column(
        Integer, default=15, nullable=False, comment="AI 分析最新 10 条新新闻的间隔分钟数"
    )
    opportunity_interval_minutes: Mapped[int] = mapped_column(
        Integer, default=15, nullable=False, comment="新闻与指标组合扫描间隔分钟数"
    )
    news_lookback_hours: Mapped[int] = mapped_column(
        Integer, default=168, nullable=False, comment="AI 新闻记忆采用的回看小时数"
    )
    timeframe: Mapped[str] = mapped_column(
        String(8), default="1h", nullable=False, comment="技术指标扫描周期"
    )
    prediction_max_holding_bars: Mapped[int] = mapped_column(
        Integer,
        default=4,
        nullable=False,
        comment="预测最大持有的技术周期K线根数",
    )
    indicator_keys_json: Mapped[list[str]] = mapped_column(
        JSON, nullable=False, comment="全部需要满足的技术指标稳定键"
    )
    monitor_symbols_json: Mapped[list[str]] = mapped_column(
        JSON, nullable=False, comment="机会扫描品种白名单；空数组表示全部可用品种"
    )
    minimum_news_confidence: Mapped[Decimal] = mapped_column(
        Numeric(5, 4), default=Decimal("0.6000"), nullable=False, comment="新闻最低置信度"
    )
    minimum_news_mentions: Mapped[int] = mapped_column(
        Integer, default=1, nullable=False, comment="候选美股至少关联新闻数"
    )
    minimum_indicator_score: Mapped[Decimal] = mapped_column(
        Numeric(5, 2), default=Decimal("65.00"), nullable=False, comment="影子准入最低技术强度"
    )
    minimum_combined_score: Mapped[Decimal] = mapped_column(
        Numeric(5, 2), default=Decimal("75.00"), nullable=False, comment="影子准入最低组合评分"
    )
    maximum_market_age_seconds: Mapped[int] = mapped_column(
        Integer, default=120, nullable=False, comment="影子准入允许的最大行情延迟秒数"
    )
    minimum_feature_quality: Mapped[Decimal] = mapped_column(
        Numeric(5, 4), default=Decimal("0.7000"), nullable=False, comment="预测因子最低数据质量"
    )
    minimum_market_flow_quality: Mapped[Decimal] = mapped_column(
        Numeric(5, 4), default=Decimal("0.5000"), nullable=False, comment="资金盘口最低数据质量"
    )
    minimum_calibration_samples: Mapped[int] = mapped_column(
        Integer, default=1000, nullable=False, comment="历史净优势校准的最低已结算样本数"
    )
    live_safety_margin_bps: Mapped[Decimal] = mapped_column(
        Numeric(8, 4), default=Decimal("10.0000"), nullable=False, comment="成本之外要求的安全边际基点"
    )
    news_score_weight: Mapped[Decimal] = mapped_column(
        Numeric(5, 2), default=Decimal("45.00"), nullable=False, comment="新闻评分组合权重百分比"
    )
    technical_score_weight: Mapped[Decimal] = mapped_column(
        Numeric(5, 2), default=Decimal("35.00"), nullable=False, comment="技术指标组合权重百分比"
    )
    market_flow_score_weight: Mapped[Decimal] = mapped_column(
        Numeric(5, 2), default=Decimal("20.00"), nullable=False, comment="资金盘口组合权重百分比"
    )
    news_system_prompt: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="User-configured system prompt for AI news analysis",
    )
    prediction_fee_enabled: Mapped[bool] = mapped_column(
        Boolean, default=True, nullable=False, comment="预测统计是否扣除手续费"
    )
    prediction_fee_bps_per_side: Mapped[Decimal] = mapped_column(
        Numeric(8, 4), default=Decimal("5.0000"), nullable=False, comment="预测统计单边手续费基点"
    )
    prediction_slippage_enabled: Mapped[bool] = mapped_column(
        Boolean, default=True, nullable=False, comment="预测统计是否扣除滑点"
    )
    prediction_slippage_bps_per_side: Mapped[Decimal] = mapped_column(
        Numeric(8, 4), default=Decimal("3.0000"), nullable=False, comment="预测统计单边滑点基点"
    )
    prediction_funding_enabled: Mapped[bool] = mapped_column(
        Boolean, default=True, nullable=False, comment="预测统计是否扣除持有期资金成本"
    )
    prediction_funding_bps_per_8h: Mapped[Decimal] = mapped_column(
        Numeric(8, 4), default=Decimal("1.0000"), nullable=False, comment="预测统计每八小时资金成本基点"
    )
    last_news_run_at: Mapped[datetime | None] = mapped_column(
        DateTime, comment="最近一次新闻分析启动时间（UTC）"
    )
    last_opportunity_run_at: Mapped[datetime | None] = mapped_column(
        DateTime, comment="最近一次机会扫描启动时间（UTC）"
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


class AiMonitorRun(Base):
    __tablename__ = "ai_monitor_runs"
    __table_args__ = (
        CheckConstraint("run_type IN ('news', 'opportunity')", name="valid_run_type"),
        CheckConstraint(
            "status IN ('pending', 'running', 'completed', 'partial', 'failed', 'skipped')",
            name="valid_status",
        ),
        UniqueConstraint("public_id", name="uq_ai_monitor_runs_public_id"),
        UniqueConstraint(
            "id",
            "user_id",
            name="uq_ai_monitor_runs_id_user_id",
        ),
        UniqueConstraint(
            "active_user_id",
            "run_type",
            name="uq_ai_monitor_runs_active_user_type",
        ),
        Index("ix_ai_monitor_runs_user_created", "user_id", "created_at"),
        Index("ix_ai_monitor_runs_user_status", "user_id", "status", "updated_at"),
        {
            "comment": "AI 监控新闻分析与机会发现的用户级执行记录",
            "mysql_engine": "InnoDB",
            "mysql_charset": "utf8mb4",
        },
    )

    id: Mapped[int] = mapped_column(
        BIGINT_PK, primary_key=True, autoincrement=True, comment="执行记录主键"
    )
    public_id: Mapped[str] = mapped_column(
        String(36), default=lambda: str(uuid.uuid4()), nullable=False, comment="执行记录公开 UUID"
    )
    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        comment="所属用户 ID",
    )
    run_type: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="news 新闻分析或 opportunity 机会扫描"
    )
    status: Mapped[str] = mapped_column(
        String(16), default="pending", nullable=False, comment="执行状态"
    )
    active_user_id: Mapped[int | None] = mapped_column(
        BigInteger,
        Computed(
            "CASE WHEN status IN ('pending', 'running') THEN user_id ELSE NULL END",
            persisted=True,
        ),
        comment="活动任务唯一性生成列；非活动任务为空",
    )
    news_batch_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("news_ai_batches.id", ondelete="SET NULL"),
        comment="关联的新闻 AI 批次 UUID",
    )
    input_count: Mapped[int] = mapped_column(
        Integer, default=0, nullable=False, comment="本轮输入新闻或候选数"
    )
    matched_count: Mapped[int] = mapped_column(
        Integer, default=0, nullable=False, comment="本轮成功分析或发现数量"
    )
    summary_json: Mapped[dict[str, Any] | None] = mapped_column(
        JSON, comment="执行摘要和脱敏统计"
    )
    error_message: Mapped[str | None] = mapped_column(
        Text, comment="面向用户的脱敏错误摘要"
    )
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime, comment="开始时间（UTC）"
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime, comment="完成时间（UTC）"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, nullable=False, comment="创建时间（UTC）"
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=utcnow,
        onupdate=utcnow,
        nullable=False,
        comment="最后更新时间（UTC）",
    )


class AiMonitorOpportunity(Base):
    __tablename__ = "ai_monitor_opportunities"
    __table_args__ = (
        CheckConstraint("direction IN ('long', 'short')", name="valid_direction"),
        CheckConstraint(
            "status IN ('candidate', 'discovered', 'expired', 'dismissed')",
            name="valid_status",
        ),
        UniqueConstraint("public_id", name="uq_ai_monitor_opportunities_public_id"),
        UniqueConstraint("dedup_key", name="uq_ai_monitor_opportunities_dedup_key"),
        UniqueConstraint(
            "id",
            "user_id",
            name="uq_ai_monitor_opportunities_id_user_id",
        ),
        ForeignKeyConstraint(
            ["analysis_run_id", "user_id"],
            ["ai_monitor_runs.id", "ai_monitor_runs.user_id"],
            name="fk_ai_monitor_opportunities_run_user",
            ondelete="CASCADE",
        ),
        Index(
            "ix_ai_monitor_opportunities_user_status_score",
            "user_id",
            "status",
            "combined_score",
        ),
        Index("ix_ai_monitor_opportunities_user_created", "user_id", "created_at"),
        {
            "comment": "由 AI 新闻与用户配置指标共同确认的美股机会",
            "mysql_engine": "InnoDB",
            "mysql_charset": "utf8mb4",
        },
    )

    id: Mapped[int] = mapped_column(
        BIGINT_PK, primary_key=True, autoincrement=True, comment="AI 机会主键"
    )
    public_id: Mapped[str] = mapped_column(
        String(36), default=lambda: str(uuid.uuid4()), nullable=False, comment="AI 机会公开 UUID"
    )
    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        comment="所属用户 ID",
    )
    analysis_run_id: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        comment="产生该机会的执行记录",
    )
    symbol: Mapped[str] = mapped_column(
        String(32), nullable=False, comment="标准美股代码，例如 AAPL"
    )
    contract_symbol: Mapped[str] = mapped_column(
        String(32), nullable=False, comment="对应的 Binance TradFi 合约代码"
    )
    direction: Mapped[str] = mapped_column(
        String(12), nullable=False, comment="机会方向"
    )
    status: Mapped[str] = mapped_column(
        String(16), default="discovered", nullable=False, comment="机会状态"
    )
    timeframe: Mapped[str] = mapped_column(
        String(8), nullable=False, comment="技术指标确认周期"
    )
    news_score: Mapped[Decimal] = mapped_column(
        Numeric(8, 4), nullable=False, comment="新闻侧置信评分（0 到 100）"
    )
    indicator_score: Mapped[Decimal] = mapped_column(
        Numeric(8, 4), nullable=False, comment="技术指标满足评分（0 到 100）"
    )
    combined_score: Mapped[Decimal] = mapped_column(
        Numeric(8, 4), nullable=False, comment="新闻与指标组合评分（0 到 100）"
    )
    matched_indicator_keys_json: Mapped[list[str]] = mapped_column(
        JSON, nullable=False, comment="本轮全部满足的指标键"
    )
    news_ids_json: Mapped[list[str]] = mapped_column(
        JSON, nullable=False, comment="本轮采用的新闻稳定 ID"
    )
    news_ai_batch_ids_json: Mapped[list[str] | None] = mapped_column(
        JSON, comment="机会首次生成时冻结的本租户新闻 AI 批次 ID"
    )
    news_ai_model_call_ids_json: Mapped[list[int] | None] = mapped_column(
        JSON, comment="机会首次生成时冻结的本租户模型调用审计 ID"
    )
    evidence_json: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, comment="新闻、技术指标与行情证据"
    )
    dedup_key: Mapped[str] = mapped_column(
        String(191), nullable=False, comment="相同新闻与行情输入的幂等去重键"
    )
    discovered_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, nullable=False, comment="发现时间（UTC）"
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, comment="机会失效时间（UTC）"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, nullable=False, comment="创建时间（UTC）"
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=utcnow,
        onupdate=utcnow,
        nullable=False,
        comment="最后更新时间（UTC）",
    )


class AiMonitorPrediction(Base):
    __tablename__ = "ai_monitor_predictions"
    __table_args__ = (
        CheckConstraint("direction IN ('long', 'short')", name="valid_direction"),
        CheckConstraint(
            "status IN ('pending', 'completed', 'unavailable')",
            name="valid_status",
        ),
        CheckConstraint(
            "result IS NULL OR result IN ('win', 'loss', 'flat')",
            name="valid_result",
        ),
        CheckConstraint(
            "net_result IS NULL OR net_result IN ('win', 'loss', 'flat')",
            name="valid_net_result",
        ),
        CheckConstraint(
            "readiness_status IN ('research_only', 'shadow_ready')",
            name="valid_readiness_status",
        ),
        CheckConstraint(
            "exit_reason IS NULL OR exit_reason IN "
            "('take_profit', 'stop_loss', 'score_breakdown', 'score_reversal', "
            "'max_holding_time', 'legacy_horizon_close')",
            name="valid_exit_reason",
        ),
        UniqueConstraint("public_id", name="uq_ai_monitor_predictions_public_id"),
        UniqueConstraint("opportunity_id", name="uq_ai_monitor_predictions_opportunity_id"),
        ForeignKeyConstraint(
            ["opportunity_id", "user_id"],
            ["ai_monitor_opportunities.id", "ai_monitor_opportunities.user_id"],
            name="fk_ai_monitor_predictions_opportunity_user",
            ondelete="CASCADE",
        ),
        Index(
            "ix_ai_monitor_predictions_user_status_due",
            "user_id",
            "status",
            "due_at",
        ),
        Index("ix_ai_monitor_predictions_user_predicted", "user_id", "predicted_at"),
        {
            "comment": "AI 监控机会生成的虚拟预测及到期结果，不产生任何交易订单",
            "mysql_engine": "InnoDB",
            "mysql_charset": "utf8mb4",
        },
    )

    id: Mapped[int] = mapped_column(
        BIGINT_PK, primary_key=True, autoincrement=True, comment="AI 预测主键"
    )
    public_id: Mapped[str] = mapped_column(
        String(36), default=lambda: str(uuid.uuid4()), nullable=False, comment="AI 预测公开 UUID"
    )
    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        comment="所属用户 ID",
    )
    opportunity_id: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        comment="触发该预测的 AI 机会",
    )
    symbol: Mapped[str] = mapped_column(String(32), nullable=False, comment="标准美股代码")
    contract_symbol: Mapped[str] = mapped_column(
        String(32), nullable=False, comment="行情对应的 TradFi 合约代码"
    )
    direction: Mapped[str] = mapped_column(
        String(12), nullable=False, comment="预测方向：long 或 short"
    )
    timeframe: Mapped[str] = mapped_column(String(8), nullable=False, comment="预测观察周期")
    status: Mapped[str] = mapped_column(
        String(16), default="pending", nullable=False, comment="预测结算状态"
    )
    result: Mapped[str | None] = mapped_column(
        String(16), comment="到期结果：win、loss 或 flat"
    )
    confidence_score: Mapped[Decimal] = mapped_column(
        Numeric(8, 4), nullable=False, comment="生成预测时的组合置信评分"
    )
    entry_price: Mapped[Decimal | None] = mapped_column(
        Numeric(30, 12), comment="生成预测时的参考入场价"
    )
    exit_price: Mapped[Decimal | None] = mapped_column(
        Numeric(30, 12), comment="预测到期时的参考价格"
    )
    exit_at: Mapped[datetime | None] = mapped_column(
        DateTime, comment="Virtual position exit time (UTC)"
    )
    exit_reason: Mapped[str | None] = mapped_column(
        String(32), comment="Virtual exit trigger"
    )
    raw_return_bps: Mapped[Decimal | None] = mapped_column(
        Numeric(20, 8), comment="到期价格相对入场价的原始涨跌基点"
    )
    directional_return_bps: Mapped[Decimal | None] = mapped_column(
        Numeric(20, 8), comment="按预测方向计算的收益基点"
    )
    signal_news_score: Mapped[Decimal | None] = mapped_column(
        Numeric(8, 4), comment="生成预测时不可变的新闻评分"
    )
    signal_indicator_score: Mapped[Decimal | None] = mapped_column(
        Numeric(8, 4), comment="生成预测时不可变的技术指标评分"
    )
    estimated_cost_bps: Mapped[Decimal] = mapped_column(
        Numeric(12, 4),
        default=Decimal("16.0000"),
        nullable=False,
        comment="预测持有期估算总成本基点",
    )
    net_directional_return_bps: Mapped[Decimal | None] = mapped_column(
        Numeric(20, 8), comment="扣除估算成本后的方向收益基点"
    )
    net_result: Mapped[str | None] = mapped_column(
        String(16), comment="成本后结果：win、loss 或 flat"
    )
    max_favorable_bps: Mapped[Decimal | None] = mapped_column(
        Numeric(20, 8), comment="预测持有期最大有利波动基点（MFE）"
    )
    max_adverse_bps: Mapped[Decimal | None] = mapped_column(
        Numeric(20, 8), comment="预测持有期最大不利波动基点（MAE）"
    )
    settlement_version: Mapped[str] = mapped_column(
        String(32),
        default="gross_v1",
        nullable=False,
        comment="预测结算与成本模型版本",
    )
    readiness_status: Mapped[str] = mapped_column(
        String(32), default="research_only", nullable=False, comment="研究信号或已达到影子运行准入"
    )
    calibration_sample_count: Mapped[int] = mapped_column(
        Integer, default=0, nullable=False, comment="生成信号时同方向历史校准样本数"
    )
    expected_gross_edge_bps: Mapped[Decimal | None] = mapped_column(
        Numeric(20, 8), comment="生成信号时历史样本估计的平均毛优势基点"
    )
    expected_edge_lower_bound_bps: Mapped[Decimal | None] = mapped_column(
        Numeric(20, 8), comment="生成信号时毛优势 95% 置信区间下限"
    )
    evidence_json: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, comment="预测生成时的新闻、指标与行情快照"
    )
    predicted_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, nullable=False, comment="预测生成时间（UTC）"
    )
    due_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, comment="预测到期结算时间（UTC）"
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime, comment="预测实际结算时间（UTC）"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, nullable=False, comment="创建时间（UTC）"
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=utcnow,
        onupdate=utcnow,
        nullable=False,
        comment="最后更新时间（UTC）",
    )


class MarketStreamEvent(Base):
    """Deduplicated provider events retained for replay and source auditing."""

    __tablename__ = "market_stream_events"
    __table_args__ = (
        CheckConstraint(
            "quality_status IN ('valid', 'delayed', 'stale', 'duplicate', 'invalid')",
            name="valid_quality_status",
        ),
        UniqueConstraint(
            "provider",
            "channel",
            "dedup_key",
            name="uq_market_stream_event_identity",
        ),
        Index("ix_market_stream_events_symbol_time", "symbol", "event_time"),
        Index("ix_market_stream_events_channel_time", "channel", "event_time"),
        {
            "comment": "Normalized market-data events used for deterministic replay",
            "mysql_engine": "InnoDB",
            "mysql_charset": "utf8mb4",
        },
    )

    id: Mapped[int] = mapped_column(BIGINT_PK, primary_key=True, autoincrement=True)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    channel: Mapped[str] = mapped_column(String(48), nullable=False)
    symbol: Mapped[str | None] = mapped_column(String(32))
    event_time: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    received_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, nullable=False
    )
    sequence_key: Mapped[str | None] = mapped_column(String(96))
    dedup_key: Mapped[str] = mapped_column(String(191), nullable=False)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    schema_version: Mapped[str] = mapped_column(
        String(32), default="uw_stream_v1", nullable=False
    )
    quality_status: Mapped[str] = mapped_column(
        String(16), default="valid", nullable=False
    )


class RealtimeMarketFeatureSnapshot(Base):
    """Minute-bucket quote, flow, GEX and venue features for AI opportunities."""

    __tablename__ = "realtime_market_feature_snapshots"
    __table_args__ = (
        CheckConstraint(
            "market_session IN ('premarket', 'regular', 'postmarket', 'closed', 'unknown')",
            name="valid_market_session",
        ),
        CheckConstraint(
            "halt_status IN ('clear', 'halted', 'cooldown', 'unknown')",
            name="valid_halt_status",
        ),
        CheckConstraint(
            "data_coverage BETWEEN 0 AND 1",
            name="valid_data_coverage",
        ),
        UniqueConstraint(
            "symbol",
            "bucket_at",
            "feature_version",
            name="uq_realtime_market_feature_identity",
        ),
        Index(
            "ix_realtime_market_features_symbol_time",
            "symbol",
            "bucket_at",
        ),
        {
            "comment": "Normalized real-time market features for AI signal gating",
            "mysql_engine": "InnoDB",
            "mysql_charset": "utf8mb4",
        },
    )

    id: Mapped[int] = mapped_column(BIGINT_PK, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    bucket_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    market_session: Mapped[str] = mapped_column(
        String(16), default="unknown", nullable=False
    )
    last_price: Mapped[Decimal | None] = mapped_column(Numeric(30, 12))
    bid: Mapped[Decimal | None] = mapped_column(Numeric(30, 12))
    ask: Mapped[Decimal | None] = mapped_column(Numeric(30, 12))
    spread_bps: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    quote_age_ms: Mapped[int | None] = mapped_column(BigInteger)
    size_imbalance: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    quote_snapshot_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    option_flow_snapshot_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    gex_snapshot_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    institutional_flow_snapshot_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    halt_status: Mapped[str] = mapped_column(
        String(16), default="unknown", nullable=False
    )
    data_coverage: Mapped[Decimal] = mapped_column(
        Numeric(5, 4), default=Decimal("0.0000"), nullable=False
    )
    stale_fields_json: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    quality_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    feature_version: Mapped[str] = mapped_column(
        String(32), default="uw_features_v2", nullable=False
    )
    captured_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, nullable=False
    )


class FinnhubQuoteSnapshot(Base):
    """Latest minute-bucket US cash-equity quote captured from Finnhub."""

    __tablename__ = "finnhub_quote_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "symbol",
            "bucket_at",
            name="uq_finnhub_quote_snapshots_symbol_bucket",
        ),
        Index(
            "ix_finnhub_quote_snapshots_symbol_source_time",
            "symbol",
            "source_timestamp",
        ),
        CheckConstraint("price > 0", name="positive_price"),
        {
            "comment": "Minute-bucket latest Finnhub US cash-equity quotes",
            "mysql_engine": "InnoDB",
            "mysql_charset": "utf8mb4",
        },
    )

    id: Mapped[int] = mapped_column(BIGINT_PK, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    bucket_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    price: Mapped[Decimal] = mapped_column(Numeric(30, 12), nullable=False)
    change: Mapped[Decimal | None] = mapped_column(Numeric(30, 12))
    change_percent: Mapped[Decimal | None] = mapped_column(Numeric(20, 8))
    day_high: Mapped[Decimal | None] = mapped_column(Numeric(30, 12))
    day_low: Mapped[Decimal | None] = mapped_column(Numeric(30, 12))
    day_open: Mapped[Decimal | None] = mapped_column(Numeric(30, 12))
    previous_close: Mapped[Decimal | None] = mapped_column(Numeric(30, 12))
    volume: Mapped[Decimal | None] = mapped_column(Numeric(30, 8))
    source_timestamp: Mapped[int] = mapped_column(BigInteger, nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    live: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    captured_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow, nullable=False
    )


class MarketRiskEvent(Base):
    """Scheduled or live risk events that may block a new virtual entry."""

    __tablename__ = "market_risk_events"
    __table_args__ = (
        CheckConstraint(
            "risk_level IN ('normal', 'medium', 'high', 'critical')",
            name="valid_risk_level",
        ),
        CheckConstraint(
            "status IN ('scheduled', 'active', 'completed', 'cancelled')",
            name="valid_event_status",
        ),
        UniqueConstraint(
            "provider", "dedup_key", name="uq_market_risk_event_identity"
        ),
        Index("ix_market_risk_events_schedule", "status", "scheduled_at"),
        Index("ix_market_risk_events_symbol_schedule", "symbol", "scheduled_at"),
        {
            "comment": "Macro, earnings and halt risk windows for AI entry gating",
            "mysql_engine": "InnoDB",
            "mysql_charset": "utf8mb4",
        },
    )

    id: Mapped[int] = mapped_column(BIGINT_PK, primary_key=True, autoincrement=True)
    public_id: Mapped[str] = mapped_column(
        String(36), default=lambda: str(uuid.uuid4()), nullable=False, unique=True
    )
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    provider_event_id: Mapped[str | None] = mapped_column(String(96))
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    event_name: Mapped[str] = mapped_column(String(191), nullable=False)
    symbol: Mapped[str | None] = mapped_column(String(32))
    scheduled_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    actual_at: Mapped[datetime | None] = mapped_column(DateTime)
    risk_level: Mapped[str] = mapped_column(
        String(16), default="medium", nullable=False
    )
    blocking_before_seconds: Mapped[int] = mapped_column(
        Integer, default=0, nullable=False
    )
    blocking_after_seconds: Mapped[int] = mapped_column(
        Integer, default=0, nullable=False
    )
    status: Mapped[str] = mapped_column(
        String(16), default="scheduled", nullable=False
    )
    dedup_key: Mapped[str] = mapped_column(String(191), nullable=False)
    source_payload_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    source_updated_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, nullable=False
    )


class OpportunityMarketSnapshot(Base):
    """Immutable market and decision inputs captured for one AI opportunity."""

    __tablename__ = "opportunity_market_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "opportunity_id", name="uq_opportunity_market_snapshot_opportunity"
        ),
        ForeignKeyConstraint(
            ["opportunity_id", "user_id"],
            ["ai_monitor_opportunities.id", "ai_monitor_opportunities.user_id"],
            name="fk_opportunity_market_snapshot_user",
            ondelete="CASCADE",
        ),
        Index("ix_opportunity_market_snapshots_user_time", "user_id", "captured_at"),
        {
            "comment": "Immutable signal-time evidence for opportunity history and replay",
            "mysql_engine": "InnoDB",
            "mysql_charset": "utf8mb4",
        },
    )

    id: Mapped[int] = mapped_column(BIGINT_PK, primary_key=True, autoincrement=True)
    opportunity_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    market_feature_snapshot_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("realtime_market_feature_snapshots.id", ondelete="SET NULL"),
    )
    captured_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, nullable=False
    )
    quote_snapshot_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    option_flow_snapshot_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    gex_snapshot_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    institutional_flow_snapshot_json: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False
    )
    macro_snapshot_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    risk_gate_snapshot_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    score_components_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    data_quality_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    weights_version: Mapped[str] = mapped_column(String(32), nullable=False)
    feature_version: Mapped[str] = mapped_column(String(32), nullable=False)
    decision_version: Mapped[str] = mapped_column(String(32), nullable=False)


class OpportunityGateDecision(Base):
    """Immutable gate decision for every opportunity scan candidate."""

    __tablename__ = "opportunity_gate_decisions"
    __table_args__ = (
        CheckConstraint(
            "gate_status IN ('passed', 'blocked', 'degraded', 'unavailable')",
            name="valid_gate_status",
        ),
        CheckConstraint("direction IN ('long', 'short')", name="valid_direction"),
        UniqueConstraint("public_id", name="uq_opportunity_gate_decisions_public_id"),
        UniqueConstraint("dedup_key", name="uq_opportunity_gate_decisions_dedup_key"),
        ForeignKeyConstraint(
            ["opportunity_id", "user_id"],
            ["ai_monitor_opportunities.id", "ai_monitor_opportunities.user_id"],
            name="fk_opportunity_gate_decisions_opportunity_user",
            ondelete="CASCADE",
        ),
        Index(
            "ix_opportunity_gate_decisions_user_time",
            "user_id",
            "decision_at",
        ),
        Index(
            "ix_opportunity_gate_decisions_opportunity_time",
            "opportunity_id",
            "decision_at",
        ),
        {
            "comment": "Immutable pass/reject evidence for every AI opportunity gate evaluation",
            "mysql_engine": "InnoDB",
            "mysql_charset": "utf8mb4",
        },
    )

    id: Mapped[int] = mapped_column(BIGINT_PK, primary_key=True, autoincrement=True)
    public_id: Mapped[str] = mapped_column(
        String(36), default=lambda: str(uuid.uuid4()), nullable=False
    )
    opportunity_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    analysis_run_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    market_feature_snapshot_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("realtime_market_feature_snapshots.id", ondelete="SET NULL"),
    )
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    contract_symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    direction: Mapped[str] = mapped_column(String(12), nullable=False)
    gate_status: Mapped[str] = mapped_column(String(16), nullable=False)
    selected: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    decision_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    feature_captured_at: Mapped[datetime | None] = mapped_column(DateTime)
    blocking_reasons_json: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    warnings_json: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    risk_gate_snapshot_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    quote_snapshot_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    market_flow_snapshot_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    score_components_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    data_quality_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    feature_version: Mapped[str] = mapped_column(String(32), nullable=False)
    weights_version: Mapped[str] = mapped_column(String(32), nullable=False)
    decision_version: Mapped[str] = mapped_column(String(32), nullable=False)
    dedup_key: Mapped[str] = mapped_column(String(191), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, nullable=False
    )


class AiMonitorReplayRun(Base):
    """An isolated, point-in-time historical replay execution."""

    __tablename__ = "ai_monitor_replay_runs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'running', 'completed', 'failed', 'cancelled')",
            name="valid_status",
        ),
        CheckConstraint("timeframe IN ('15m', '1h', '4h')", name="valid_timeframe"),
        UniqueConstraint("public_id", name="uq_ai_monitor_replay_runs_public_id"),
        UniqueConstraint(
            "active_user_id", name="uq_ai_monitor_replay_runs_active_user"
        ),
        UniqueConstraint("id", "user_id", name="uq_ai_monitor_replay_runs_id_user_id"),
        Index("ix_ai_monitor_replay_runs_user_created", "user_id", "created_at"),
        Index("ix_ai_monitor_replay_runs_user_status", "user_id", "status"),
        {
            "comment": "独立历史回放任务；数据和结果不得写入实时机会或实时预测表",
            "mysql_engine": "InnoDB",
            "mysql_charset": "utf8mb4",
        },
    )

    id: Mapped[int] = mapped_column(BIGINT_PK, primary_key=True, autoincrement=True)
    public_id: Mapped[str] = mapped_column(
        String(36), default=lambda: str(uuid.uuid4()), nullable=False
    )
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(16), default="pending", nullable=False)
    active_user_id: Mapped[int | None] = mapped_column(
        BigInteger,
        Computed(
            "CASE WHEN status IN ('pending', 'running') THEN user_id ELSE NULL END",
            persisted=True,
        ),
    )
    timeframe: Mapped[str] = mapped_column(String(8), nullable=False)
    start_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    end_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    out_of_sample_start_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    requested_symbols_json: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    config_snapshot_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    cost_model_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    provenance_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    dataset_hash: Mapped[str | None] = mapped_column(String(64))
    total_symbols: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    completed_symbols: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total_events: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    generated_signals: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    settled_signals: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    summary_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    error_message: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow, nullable=False
    )


class AiMonitorReplayDatasetManifest(Base):
    """Coverage and integrity record for every dataset consumed by a replay."""

    __tablename__ = "ai_monitor_replay_dataset_manifests"
    __table_args__ = (
        UniqueConstraint(
            "run_id", "source", "symbol", "data_type", name="uq_ai_replay_manifest_source"
        ),
        ForeignKeyConstraint(
            ["run_id", "user_id"],
            ["ai_monitor_replay_runs.id", "ai_monitor_replay_runs.user_id"],
            name="fk_ai_replay_manifest_run_user",
            ondelete="CASCADE",
        ),
        Index("ix_ai_replay_manifest_run", "run_id", "data_type"),
        {
            "comment": "历史回放数据来源、覆盖区间、校验和与降级说明",
            "mysql_engine": "InnoDB",
            "mysql_charset": "utf8mb4",
        },
    )

    id: Mapped[int] = mapped_column(BIGINT_PK, primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    symbol: Mapped[str] = mapped_column(String(32), default="*", nullable=False)
    data_type: Mapped[str] = mapped_column(String(32), nullable=False)
    coverage_start_at: Mapped[datetime | None] = mapped_column(DateTime)
    coverage_end_at: Mapped[datetime | None] = mapped_column(DateTime)
    row_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    sha256: Mapped[str | None] = mapped_column(String(64))
    exact_point_in_time: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    details_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)


class AiMonitorReplaySignal(Base):
    """Frozen signal generated only from information available at signal time."""

    __tablename__ = "ai_monitor_replay_signals"
    __table_args__ = (
        CheckConstraint("direction IN ('long', 'short')", name="valid_direction"),
        CheckConstraint("sample_split IN ('train', 'embargo', 'oos')", name="valid_split"),
        UniqueConstraint("dedup_key", name="uq_ai_monitor_replay_signals_dedup_key"),
        UniqueConstraint(
            "id", "run_id", "user_id", name="uq_ai_monitor_replay_signals_id_run_user"
        ),
        ForeignKeyConstraint(
            ["run_id", "user_id"],
            ["ai_monitor_replay_runs.id", "ai_monitor_replay_runs.user_id"],
            name="fk_ai_replay_signal_run_user",
            ondelete="CASCADE",
        ),
        Index("ix_ai_replay_signals_run_time", "run_id", "signal_at"),
        Index("ix_ai_replay_signals_user_split", "user_id", "sample_split"),
        {
            "comment": "独立历史回放的冻结信号，不参与当前机会与实时预测",
            "mysql_engine": "InnoDB",
            "mysql_charset": "utf8mb4",
        },
    )

    id: Mapped[int] = mapped_column(BIGINT_PK, primary_key=True, autoincrement=True)
    public_id: Mapped[str] = mapped_column(
        String(36), default=lambda: str(uuid.uuid4()), nullable=False, unique=True
    )
    run_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    contract_symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    direction: Mapped[str] = mapped_column(String(12), nullable=False)
    timeframe: Mapped[str] = mapped_column(String(8), nullable=False)
    sample_split: Mapped[str] = mapped_column(String(12), nullable=False)
    news_score: Mapped[Decimal] = mapped_column(Numeric(8, 4), nullable=False)
    indicator_score: Mapped[Decimal] = mapped_column(Numeric(8, 4), nullable=False)
    combined_score: Mapped[Decimal] = mapped_column(Numeric(8, 4), nullable=False)
    signal_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    entry_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    due_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    entry_price: Mapped[Decimal] = mapped_column(Numeric(30, 12), nullable=False)
    dedup_key: Mapped[str] = mapped_column(String(191), nullable=False)
    news_snapshot_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False)
    indicator_snapshot_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    evidence_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)


class AiMonitorReplayOutcome(Base):
    """Cost-adjusted settlement of one isolated historical replay signal."""

    __tablename__ = "ai_monitor_replay_outcomes"
    __table_args__ = (
        CheckConstraint("result IN ('win', 'loss', 'flat')", name="valid_result"),
        UniqueConstraint("signal_id", name="uq_ai_monitor_replay_outcomes_signal"),
        ForeignKeyConstraint(
            ["signal_id", "run_id", "user_id"],
            [
                "ai_monitor_replay_signals.id",
                "ai_monitor_replay_signals.run_id",
                "ai_monitor_replay_signals.user_id",
            ],
            name="fk_ai_replay_outcome_signal_run_user",
            ondelete="CASCADE",
        ),
        Index("ix_ai_replay_outcomes_run_split", "run_id", "sample_split"),
        {
            "comment": "历史回放信号的保守成本后结算结果",
            "mysql_engine": "InnoDB",
            "mysql_charset": "utf8mb4",
        },
    )

    id: Mapped[int] = mapped_column(BIGINT_PK, primary_key=True, autoincrement=True)
    signal_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    run_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sample_split: Mapped[str] = mapped_column(String(12), nullable=False)
    exit_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    exit_price: Mapped[Decimal] = mapped_column(Numeric(30, 12), nullable=False)
    gross_directional_return_bps: Mapped[Decimal] = mapped_column(
        Numeric(20, 8), nullable=False
    )
    estimated_cost_bps: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    net_directional_return_bps: Mapped[Decimal] = mapped_column(
        Numeric(20, 8), nullable=False
    )
    max_favorable_bps: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    max_adverse_bps: Mapped[Decimal] = mapped_column(Numeric(20, 8), nullable=False)
    result: Mapped[str] = mapped_column(String(16), nullable=False)
    settlement_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    settled_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)


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
    url: Mapped[str] = mapped_column(Text, nullable=False, comment="新闻来源 HTTPS 地址")
    feed_type: Mapped[str] = mapped_column(
        String(32), default="rss", nullable=False, comment="来源格式：rss 或 taoz_flash"
    )
    lang: Mapped[str] = mapped_column(String(16), default="en", nullable=False, comment="内容语言")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False, comment="是否启用")
    slow: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, comment="是否低频轮询")
    weight: Mapped[int] = mapped_column(Integer, default=100, nullable=False, comment="来源展示权重")
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
    heartbeat_at: Mapped[int] = mapped_column(BigInteger, nullable=False, comment="最近心跳 Unix 时间")
    last_success_at: Mapped[int | None] = mapped_column(BigInteger, comment="最近成功 Unix 时间")
    last_error_at: Mapped[int | None] = mapped_column(BigInteger, comment="最近失败 Unix 时间")
    last_error: Mapped[str | None] = mapped_column(Text, comment="最近错误摘要")
    cycles: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False, comment="累计周期数")
    items: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False, comment="累计处理条数")
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
        CheckConstraint(
            "direction IN ('long', 'short', 'neutral')", name="valid_direction"
        ),
        CheckConstraint(
            "status IN ('detected', 'watching', 'confirmed', 'expired', 'rejected', 'consumed')",
            name="valid_status",
        ),
        UniqueConstraint("public_id", name="uq_market_opportunities_public_id"),
        UniqueConstraint("dedup_key", name="uq_market_opportunities_dedup_key"),
        Index("ix_market_opportunities_status_quality", "status", "quality_score"),
        Index("ix_market_opportunities_symbol_time", "symbol", "detected_bar_time"),
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
    primary_timeframe: Mapped[str] = mapped_column(
        String(8), nullable=False, comment="机会主周期"
    )
    direction: Mapped[str] = mapped_column(
        String(12), nullable=False, comment="机会方向：long、short 或 neutral"
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="机会生命周期状态"
    )
    quality_score: Mapped[Decimal] = mapped_column(
        Numeric(8, 4), nullable=False, comment="机会质量分，仅用于排序和解释"
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
        CheckConstraint(
            "mode IN ('backtest', 'paper', 'shadow', 'live')", name="valid_mode"
        ),
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
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime, comment="策略部署启动时间（UTC）"
    )
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
    opportunity_id: Mapped[int | None] = mapped_column(
        BigInteger, comment="关联的公共市场机会 ID"
    )
    symbol: Mapped[str] = mapped_column(String(32), nullable=False, comment="合约代码")
    timeframe: Mapped[str] = mapped_column(String(8), nullable=False, comment="信号触发周期")
    signal_bar_time: Mapped[int] = mapped_column(
        BigInteger, nullable=False, comment="信号对应的已收盘 K 线时间"
    )
    decision: Mapped[str] = mapped_column(
        String(24), nullable=False, comment="结构化策略决策代码"
    )
    confidence: Mapped[Decimal | None] = mapped_column(
        Numeric(8, 4), comment="规则置信度，仅用于排序和解释"
    )
    status: Mapped[str] = mapped_column(
        String(24), default="proposed", nullable=False, comment="信号审批与执行状态"
    )
    valid_until: Mapped[datetime | None] = mapped_column(
        DateTime, comment="策略信号有效期（UTC）"
    )
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


class LiveTradingAccount(Base):
    __tablename__ = "live_trading_accounts"
    __table_args__ = (
        CheckConstraint(
            "status IN ('paused', 'active', 'archived', 'error')", name="valid_status"
        ),
        ForeignKeyConstraint(
            ["strategy_id", "user_id"],
            ["user_strategies.id", "user_strategies.user_id"],
            name="fk_live_accounts_strategy_tenant",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("public_id", name="uq_live_accounts_public_id"),
        UniqueConstraint("id", "user_id", name="uq_live_accounts_id_user_id"),
        UniqueConstraint("user_id", "name", name="uq_live_accounts_user_name"),
        Index("ix_live_accounts_user_status_updated", "user_id", "status", "updated_at"),
        {
            "comment": "用户隔离的 Binance 实盘策略部署；资金与仓位以交易所为准",
            "mysql_engine": "InnoDB",
            "mysql_charset": "utf8mb4",
        },
    )

    id: Mapped[int] = mapped_column(
        BIGINT_PK, primary_key=True, autoincrement=True, comment="实盘账户内部主键"
    )
    public_id: Mapped[str] = mapped_column(
        String(36), default=lambda: str(uuid.uuid4()), nullable=False, comment="实盘账户公开 UUID"
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
    name: Mapped[str] = mapped_column(String(100), nullable=False, comment="实盘部署显示名称")
    status: Mapped[str] = mapped_column(
        String(16), default="paused", nullable=False, comment="暂停、运行、归档或错误"
    )
    config_json: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, comment="实盘标的、杠杆、仓位和保护单风控配置"
    )
    strategy_snapshot_json: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, comment="创建实盘部署时冻结的完整策略快照"
    )
    credential_version: Mapped[int] = mapped_column(
        Integer, nullable=False, comment="启用时绑定的 Binance 凭据版本"
    )
    armed_at: Mapped[datetime | None] = mapped_column(
        DateTime, comment="用户明确确认启用实盘的时间（UTC）"
    )
    last_tick_at: Mapped[datetime | None] = mapped_column(
        DateTime, comment="最后一次实盘策略检查时间（UTC）"
    )
    last_error_code: Mapped[str | None] = mapped_column(
        String(64), comment="最后一次脱敏执行错误代码"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, nullable=False, comment="实盘部署创建时间（UTC）"
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=utcnow,
        onupdate=utcnow,
        nullable=False,
        comment="实盘部署最后更新时间（UTC）",
    )

    user: Mapped[User] = relationship(back_populates="live_accounts")


class LiveOrderIntent(Base):
    __tablename__ = "live_order_intents"
    __table_args__ = (
        CheckConstraint(
            "action IN ('open', 'close', 'stop', 'take_profit')", name="valid_action"
        ),
        CheckConstraint("side IN ('BUY', 'SELL')", name="valid_side"),
        CheckConstraint(
            "position_side IN ('BOTH', 'LONG', 'SHORT')", name="valid_position_side"
        ),
        CheckConstraint(
            "status IN ('created', 'submitted', 'filled', 'canceled', 'rejected', 'unknown')",
            name="valid_status",
        ),
        ForeignKeyConstraint(
            ["live_account_id", "user_id"],
            ["live_trading_accounts.id", "live_trading_accounts.user_id"],
            name="fk_live_order_intents_account_tenant",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["deployment_id", "user_id"],
            ["strategy_deployments.id", "strategy_deployments.user_id"],
            name="fk_live_order_intents_deployment_tenant",
            ondelete="CASCADE",
        ),
        UniqueConstraint("public_id", name="uq_live_order_intents_public_id"),
        UniqueConstraint("signal_key", name="uq_live_order_intents_signal_key"),
        UniqueConstraint("client_order_id", name="uq_live_order_intents_client_order_id"),
        Index("ix_live_order_intents_user_created", "user_id", "created_at"),
        Index("ix_live_order_intents_strategy_signal", "strategy_signal_id"),
        Index(
            "ix_live_order_intents_account_symbol",
            "live_account_id",
            "symbol",
            "position_side",
            "status",
        ),
        {
            "comment": "Binance 实盘订单的幂等意图、脱敏响应与审计状态",
            "mysql_engine": "InnoDB",
            "mysql_charset": "utf8mb4",
        },
    )

    id: Mapped[int] = mapped_column(
        BIGINT_PK, primary_key=True, autoincrement=True, comment="订单意图内部主键"
    )
    public_id: Mapped[str] = mapped_column(
        String(36), default=lambda: str(uuid.uuid4()), nullable=False, comment="订单意图公开 UUID"
    )
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False, comment="所属用户 ID")
    live_account_id: Mapped[int] = mapped_column(
        BigInteger, nullable=False, comment="所属实盘部署账户 ID"
    )
    deployment_id: Mapped[int] = mapped_column(
        BigInteger, nullable=False, comment="固定策略修订的部署 ID"
    )
    strategy_signal_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("strategy_signals.id", ondelete="SET NULL"),
        comment="Strategy signal that caused this order intent",
    )
    signal_key: Mapped[str] = mapped_column(
        String(191), nullable=False, comment="策略信号与订单动作的全局幂等键"
    )
    client_order_id: Mapped[str] = mapped_column(
        String(36), nullable=False, comment="发送给 Binance 的幂等客户端订单号"
    )
    binance_order_id: Mapped[str | None] = mapped_column(
        String(64), comment="Binance 订单 ID，按字符串保存避免精度丢失"
    )
    symbol: Mapped[str] = mapped_column(String(32), nullable=False, comment="USD-M 合约代码")
    action: Mapped[str] = mapped_column(String(16), nullable=False, comment="开仓、平仓或保护单")
    side: Mapped[str] = mapped_column(String(4), nullable=False, comment="BUY 或 SELL")
    position_side: Mapped[str] = mapped_column(
        String(8), default="BOTH", nullable=False, comment="BOTH、LONG 或 SHORT 持仓方向"
    )
    order_type: Mapped[str] = mapped_column(String(32), nullable=False, comment="Binance 订单类型")
    quantity: Mapped[Decimal | None] = mapped_column(
        Numeric(30, 18), comment="下单数量；closePosition 保护单可为空"
    )
    status: Mapped[str] = mapped_column(
        String(16), default="created", nullable=False, comment="订单意图生命周期状态"
    )
    request_json: Mapped[dict[str, Any]] = mapped_column(
        JSON, default=dict, nullable=False, comment="不含密钥与签名的订单参数"
    )
    entry_basis_json: Mapped[dict[str, Any] | None] = mapped_column(
        JSON, comment="Immutable strategy, signal, market and risk evidence captured at entry"
    )
    response_json: Mapped[dict[str, Any] | None] = mapped_column(
        JSON, comment="经过字段白名单裁剪的 Binance 响应"
    )
    error_code: Mapped[str | None] = mapped_column(
        String(64), comment="脱敏错误类别"
    )
    submitted_at: Mapped[datetime | None] = mapped_column(
        DateTime, comment="首次发往 Binance 的时间（UTC）"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, nullable=False, comment="订单意图创建时间（UTC）"
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=utcnow,
        onupdate=utcnow,
        nullable=False,
        comment="订单意图最后更新时间（UTC）",
    )


class Security(Base):
    __tablename__ = "securities"
    __table_args__ = (
        UniqueConstraint("exchange", "symbol", name="uq_securities_exchange_symbol"),
        Index("ix_securities_type_active", "security_type", "is_active"),
        {"comment": "美股、ADR 与 ETF 证券主数据", "mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"},
    )

    id: Mapped[int] = mapped_column(BIGINT_PK, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    exchange: Mapped[str] = mapped_column(String(32), default="US", nullable=False)
    security_type: Mapped[str] = mapped_column(String(32), default="UNKNOWN", nullable=False)
    company_name: Mapped[str | None] = mapped_column(String(255))
    company_name_zh: Mapped[str | None] = mapped_column(String(255))
    currency: Mapped[str] = mapped_column(String(8), default="USD", nullable=False)
    country: Mapped[str | None] = mapped_column(String(64))
    cik: Mapped[str | None] = mapped_column(String(16))
    isin: Mapped[str | None] = mapped_column(String(32))
    finnhub_symbol: Mapped[str | None] = mapped_column(String(32))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    verification_status: Mapped[str] = mapped_column(String(32), default="PENDING", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow, nullable=False)


class SecuritySymbolMapping(Base):
    __tablename__ = "security_symbol_mappings"
    __table_args__ = (UniqueConstraint("source", "source_symbol", name="uq_security_mapping_source_symbol"),)

    id: Mapped[int] = mapped_column(BIGINT_PK, primary_key=True, autoincrement=True)
    security_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("securities.id", ondelete="CASCADE"), nullable=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    source_symbol: Mapped[str] = mapped_column(String(64), nullable=False)
    normalized_symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    mapping_status: Mapped[str] = mapped_column(String(32), default="AUTO", nullable=False)
    mapping_method: Mapped[str | None] = mapped_column(String(64))
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)


class CompanyProfile(Base):
    __tablename__ = "company_profiles"

    security_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("securities.id", ondelete="CASCADE"), primary_key=True)
    legal_name: Mapped[str | None] = mapped_column(String(255))
    description: Mapped[str | None] = mapped_column(Text)
    industry: Mapped[str | None] = mapped_column(String(128))
    industry_zh: Mapped[str | None] = mapped_column(String(128))
    sector: Mapped[str | None] = mapped_column(String(128))
    sector_zh: Mapped[str | None] = mapped_column(String(128))
    website: Mapped[str | None] = mapped_column(Text)
    ipo_date: Mapped[Any | None] = mapped_column(Date)
    employee_count: Mapped[int | None] = mapped_column(BigInteger)
    market_cap: Mapped[Decimal | None] = mapped_column(Numeric(30, 4))
    shares_outstanding: Mapped[Decimal | None] = mapped_column(Numeric(30, 4))
    source: Mapped[str | None] = mapped_column(String(32))
    source_updated_at: Mapped[datetime | None] = mapped_column(DateTime)
    raw_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow, nullable=False)


class SecurityResearchSource(Base):
    __tablename__ = "security_research_sources"
    __table_args__ = (UniqueConstraint("security_id", "content_hash", name="uq_research_security_hash"),)

    id: Mapped[int] = mapped_column(BIGINT_PK, primary_key=True, autoincrement=True)
    security_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("securities.id", ondelete="CASCADE"), nullable=False)
    source_type: Mapped[str] = mapped_column(String(32), nullable=False)
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    url: Mapped[str | None] = mapped_column(Text)
    publisher: Mapped[str | None] = mapped_column(String(128))
    published_at: Mapped[datetime | None] = mapped_column(DateTime)
    retrieved_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    content_summary: Mapped[str | None] = mapped_column(Text)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    raw_metadata_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(32), default="ACTIVE", nullable=False)


class SecurityFundamentalAnalysis(Base):
    __tablename__ = "security_fundamental_analyses"
    __table_args__ = (UniqueConstraint("security_id", "analysis_version", "as_of_date", name="uq_analysis_security_version_date"),)

    id: Mapped[int] = mapped_column(BIGINT_PK, primary_key=True, autoincrement=True)
    security_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("securities.id", ondelete="CASCADE"), nullable=False)
    analysis_version: Mapped[str] = mapped_column(String(32), default="v1", nullable=False)
    as_of_date: Mapped[Any] = mapped_column(Date, nullable=False)
    business_summary: Mapped[str | None] = mapped_column(Text)
    growth_analysis: Mapped[str | None] = mapped_column(Text)
    profitability_analysis: Mapped[str | None] = mapped_column(Text)
    valuation_analysis: Mapped[str | None] = mapped_column(Text)
    risk_analysis: Mapped[str | None] = mapped_column(Text)
    catalysts_json: Mapped[list[Any] | None] = mapped_column(JSON)
    risk_factors_json: Mapped[list[Any] | None] = mapped_column(JSON)
    quality_score: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    growth_score: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    valuation_score: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    financial_health_score: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    overall_score: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    confidence_score: Mapped[Decimal | None] = mapped_column(Numeric(5, 4))
    evidence_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    generated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)


class SecurityFinancialSnapshot(Base):
    __tablename__ = "security_financial_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "security_id",
            "snapshot_date",
            name="uq_security_financial_snapshot_date",
        ),
        {
            "comment": "美股基本面财务、现金流、负债与估值快照",
            "mysql_engine": "InnoDB",
            "mysql_charset": "utf8mb4",
        },
    )

    id: Mapped[int] = mapped_column(BIGINT_PK, primary_key=True, autoincrement=True)
    security_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("securities.id", ondelete="CASCADE"),
        nullable=False,
    )
    snapshot_date: Mapped[Any] = mapped_column(Date, nullable=False)
    fiscal_period_end: Mapped[Any | None] = mapped_column(Date)
    period_type: Mapped[str] = mapped_column(String(16), default="TTM", nullable=False)
    currency: Mapped[str] = mapped_column(String(8), default="USD", nullable=False)
    data_status: Mapped[str] = mapped_column(String(32), nullable=False)
    coverage_pct: Mapped[Decimal | None] = mapped_column(Numeric(7, 4))

    revenue_ttm: Mapped[Decimal | None] = mapped_column(Numeric(30, 4))
    revenue_growth_yoy_pct: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    gross_profit_ttm: Mapped[Decimal | None] = mapped_column(Numeric(30, 4))
    gross_margin_pct: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    operating_income_ttm: Mapped[Decimal | None] = mapped_column(Numeric(30, 4))
    operating_margin_pct: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    net_income_ttm: Mapped[Decimal | None] = mapped_column(Numeric(30, 4))
    net_margin_pct: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    ebitda_ttm: Mapped[Decimal | None] = mapped_column(Numeric(30, 4))

    operating_cash_flow_ttm: Mapped[Decimal | None] = mapped_column(Numeric(30, 4))
    capital_expenditure_ttm: Mapped[Decimal | None] = mapped_column(Numeric(30, 4))
    free_cash_flow_ttm: Mapped[Decimal | None] = mapped_column(Numeric(30, 4))
    cash_and_equivalents: Mapped[Decimal | None] = mapped_column(Numeric(30, 4))
    total_debt: Mapped[Decimal | None] = mapped_column(Numeric(30, 4))
    total_assets: Mapped[Decimal | None] = mapped_column(Numeric(30, 4))
    total_liabilities: Mapped[Decimal | None] = mapped_column(Numeric(30, 4))
    stockholders_equity: Mapped[Decimal | None] = mapped_column(Numeric(30, 4))

    current_ratio: Mapped[Decimal | None] = mapped_column(Numeric(20, 6))
    debt_to_equity: Mapped[Decimal | None] = mapped_column(Numeric(20, 6))
    return_on_equity_pct: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    market_cap: Mapped[Decimal | None] = mapped_column(Numeric(30, 4))
    enterprise_value: Mapped[Decimal | None] = mapped_column(Numeric(30, 4))
    pe_ratio: Mapped[Decimal | None] = mapped_column(Numeric(20, 6))
    price_to_sales_ratio: Mapped[Decimal | None] = mapped_column(Numeric(20, 6))
    price_to_book_ratio: Mapped[Decimal | None] = mapped_column(Numeric(20, 6))
    ev_to_ebitda: Mapped[Decimal | None] = mapped_column(Numeric(20, 6))

    source: Mapped[str] = mapped_column(String(64), nullable=False)
    source_url: Mapped[str | None] = mapped_column(Text)
    filing_form: Mapped[str | None] = mapped_column(String(32))
    filing_accession: Mapped[str | None] = mapped_column(String(32))
    applicable_metrics_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    raw_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    retrieved_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=utcnow,
        onupdate=utcnow,
        nullable=False,
    )
