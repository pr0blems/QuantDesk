"""Add Chinese comments for every current table and column.

Revision ID: 0002_add_schema_comments
Revises: 0001_identity_foundation
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_add_schema_comments"
down_revision: str | None = "0001_identity_foundation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _comment_column(
    table: str,
    column: str,
    existing_type: sa.types.TypeEngine,
    comment: str,
    *,
    nullable: bool,
    server_default: sa.sql.elements.TextClause | None = None,
    autoincrement: bool | None = None,
) -> None:
    op.alter_column(
        table,
        column,
        existing_type=existing_type,
        existing_nullable=nullable,
        existing_server_default=server_default,
        existing_autoincrement=autoincrement,
        comment=comment,
    )


def upgrade() -> None:
    op.create_table_comment("users", "平台用户及其加密后的 Binance API 凭据")
    _comment_column("users", "id", sa.BigInteger(), "用户主键", nullable=False, autoincrement=True)
    _comment_column("users", "username", sa.String(64), "登录用户名，全局唯一", nullable=False)
    _comment_column("users", "email", sa.String(254), "用户邮箱，可为空，全局唯一", nullable=True)
    _comment_column(
        "users", "password_hash", sa.String(255), "Argon2id 登录密码哈希", nullable=False
    )
    _comment_column(
        "users",
        "is_active",
        sa.Boolean(),
        "账户是否启用",
        nullable=False,
        server_default=sa.text("1"),
    )
    _comment_column(
        "users",
        "is_admin",
        sa.Boolean(),
        "是否为平台管理员",
        nullable=False,
        server_default=sa.text("0"),
    )
    _comment_column(
        "users",
        "binance_api_key_encrypted",
        sa.Text(),
        "Fernet 加密后的 Binance API Key",
        nullable=True,
    )
    _comment_column(
        "users",
        "binance_api_secret_encrypted",
        sa.Text(),
        "Fernet 加密后的 Binance API Secret",
        nullable=True,
    )
    _comment_column(
        "users",
        "binance_key_fingerprint",
        sa.String(16),
        "API Key 的 SHA-256 短指纹，仅用于识别",
        nullable=True,
    )
    _comment_column(
        "users",
        "binance_key_version",
        sa.Integer(),
        "Binance 凭据版本号，每次更新或删除递增",
        nullable=False,
        server_default=sa.text("1"),
    )
    _comment_column(
        "users",
        "binance_permissions",
        sa.JSON(),
        "Binance API 权限快照，只允许 READ 和 TRADE",
        nullable=True,
    )
    _comment_column(
        "users",
        "binance_key_updated_at",
        sa.DateTime(),
        "Binance 凭据最后更新时间（UTC）",
        nullable=True,
    )
    _comment_column("users", "last_login_at", sa.DateTime(), "最后登录时间（UTC）", nullable=True)
    _comment_column(
        "users",
        "created_at",
        sa.DateTime(),
        "用户创建时间（UTC）",
        nullable=False,
        server_default=sa.text("CURRENT_TIMESTAMP"),
    )
    _comment_column(
        "users",
        "updated_at",
        sa.DateTime(),
        "用户记录最后更新时间（UTC）",
        nullable=False,
        server_default=sa.text("CURRENT_TIMESTAMP"),
    )

    op.create_table_comment("user_sessions", "用户登录会话与刷新令牌生命周期")
    _comment_column("user_sessions", "id", sa.String(36), "会话 UUID 主键", nullable=False)
    _comment_column("user_sessions", "user_id", sa.BigInteger(), "所属用户 ID", nullable=False)
    _comment_column(
        "user_sessions",
        "refresh_token_hash",
        sa.String(64),
        "刷新令牌 SHA-256 哈希，不保存明文令牌",
        nullable=False,
    )
    _comment_column(
        "user_sessions", "client_type", sa.String(16), "客户端类型：web 或 native", nullable=False
    )
    _comment_column(
        "user_sessions", "user_agent", sa.String(512), "登录客户端 User-Agent", nullable=True
    )
    _comment_column(
        "user_sessions", "ip_address", sa.String(45), "登录来源 IPv4 或 IPv6", nullable=True
    )
    _comment_column(
        "user_sessions", "expires_at", sa.DateTime(), "刷新会话过期时间（UTC）", nullable=False
    )
    _comment_column(
        "user_sessions",
        "revoked_at",
        sa.DateTime(),
        "会话撤销时间（UTC），为空表示未撤销",
        nullable=True,
    )
    _comment_column(
        "user_sessions",
        "created_at",
        sa.DateTime(),
        "会话创建时间（UTC）",
        nullable=False,
        server_default=sa.text("CURRENT_TIMESTAMP"),
    )

    op.create_table_comment("audit_logs", "安全敏感操作审计日志")
    _comment_column(
        "audit_logs", "id", sa.BigInteger(), "审计日志主键", nullable=False, autoincrement=True
    )
    _comment_column(
        "audit_logs", "user_id", sa.BigInteger(), "操作用户 ID，用户删除后可为空", nullable=True
    )
    _comment_column("audit_logs", "action", sa.String(80), "审计动作代码", nullable=False)
    _comment_column("audit_logs", "resource_type", sa.String(80), "被操作资源类型", nullable=True)
    _comment_column("audit_logs", "resource_id", sa.String(80), "被操作资源标识", nullable=True)
    _comment_column(
        "audit_logs", "ip_address", sa.String(45), "操作来源 IPv4 或 IPv6", nullable=True
    )
    _comment_column(
        "audit_logs",
        "metadata_json",
        sa.JSON(),
        "脱敏后的扩展审计信息，禁止存放密钥和令牌",
        nullable=True,
    )
    _comment_column(
        "audit_logs",
        "created_at",
        sa.DateTime(),
        "审计事件发生时间（UTC）",
        nullable=False,
        server_default=sa.text("CURRENT_TIMESTAMP"),
    )

    op.create_table_comment("alembic_version", "Alembic 数据库结构迁移版本")
    _comment_column(
        "alembic_version", "version_num", sa.String(32), "当前数据库结构迁移版本号", nullable=False
    )


def downgrade() -> None:
    for table in ("users", "user_sessions", "audit_logs", "alembic_version"):
        op.drop_table_comment(table, existing_comment=None)
