from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    MetaData,
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


BIGINT_PK = BigInteger().with_variant(Integer, "sqlite")


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
