"""Add per-user encrypted AI model configurations.

Revision ID: 0016_ai_model_configs
Revises: 0015_admin_control_plane
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0016_ai_model_configs"
down_revision: str | None = "0015_admin_control_plane"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _require_mysql() -> None:
    if op.get_bind().dialect.name not in {"mysql", "mariadb"}:
        raise RuntimeError("QuantDesk migrations require MySQL or MariaDB")


def upgrade() -> None:
    _require_mysql()
    op.create_table(
        "ai_model_configs",
        sa.Column(
            "id",
            sa.BigInteger(),
            autoincrement=True,
            nullable=False,
            comment="AI 模型配置内部主键",
        ),
        sa.Column("public_id", sa.String(36), nullable=False, comment="供接口使用的公开 UUID"),
        sa.Column("user_id", sa.BigInteger(), nullable=False, comment="所属用户 ID，用于租户隔离"),
        sa.Column(
            "provider_code",
            sa.String(32),
            nullable=False,
            comment="服务端白名单中的 AI 服务商代码",
        ),
        sa.Column(
            "display_name",
            sa.String(80),
            nullable=False,
            comment="用户自定义配置名称，同一用户内唯一",
        ),
        sa.Column("model_name", sa.String(128), nullable=False, comment="服务商模型标识"),
        sa.Column(
            "api_key_encrypted",
            sa.Text(),
            nullable=False,
            comment="Fernet 加密后的 AI 服务商 API Key",
        ),
        sa.Column(
            "api_key_fingerprint",
            sa.String(16),
            nullable=False,
            comment="API Key 的 SHA-256 短指纹，仅用于识别",
        ),
        sa.Column(
            "api_key_version",
            sa.Integer(),
            server_default=sa.text("1"),
            nullable=False,
            comment="API Key 版本，每次替换时递增",
        ),
        sa.Column(
            "is_enabled",
            sa.Boolean(),
            server_default=sa.text("1"),
            nullable=False,
            comment="该模型配置是否允许被调用",
        ),
        sa.Column(
            "is_default",
            sa.Boolean(),
            server_default=sa.text("0"),
            nullable=False,
            comment="是否为当前用户默认 AI 模型",
        ),
        sa.Column(
            "default_user_id",
            sa.BigInteger(),
            sa.Computed(
                "CASE WHEN is_default = 1 THEN user_id ELSE NULL END",
                persisted=True,
            ),
            nullable=True,
            comment="默认配置唯一性生成列；非默认配置为空",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
            comment="配置创建时间（UTC）",
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
            comment="配置最后更新时间（UTC）",
        ),
        sa.CheckConstraint(
            "provider_code IN ('openai', 'deepseek', 'doubao', 'qwen', 'kimi', 'minimax')",
            name="valid_provider",
        ),
        sa.CheckConstraint(
            "is_default = 0 OR is_enabled = 1",
            name="default_enabled",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_ai_model_configs_user_id_users",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_ai_model_configs"),
        sa.UniqueConstraint("public_id", name="uq_ai_model_configs_public_id"),
        sa.UniqueConstraint(
            "user_id",
            "display_name",
            name="uq_ai_model_configs_user_display_name",
        ),
        sa.UniqueConstraint(
            "default_user_id",
            name="uq_ai_model_configs_default_user_id",
        ),
        comment="用户隔离并加密保存的 AI 模型调用配置",
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )
    op.create_index(
        "ix_ai_model_configs_user_updated",
        "ai_model_configs",
        ["user_id", "updated_at"],
        unique=False,
    )
    op.create_index(
        "ix_ai_model_configs_provider",
        "ai_model_configs",
        ["provider_code"],
        unique=False,
    )


def downgrade() -> None:
    _require_mysql()
    op.drop_index("ix_ai_model_configs_provider", table_name="ai_model_configs")
    op.drop_index("ix_ai_model_configs_user_updated", table_name="ai_model_configs")
    op.drop_table("ai_model_configs")
