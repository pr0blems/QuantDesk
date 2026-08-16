from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import AiModelConfig, User

GLOBAL_AI_MODEL_OWNER_USERNAME = "y0ur"
GLOBAL_AI_MODEL_PROVIDER_CODE = "deepseek"


def get_global_ai_model_owner(db: Session, *, for_update: bool = False) -> User | None:
    """Return the single account that owns the platform-wide model credential."""

    statement = select(User).where(User.username == GLOBAL_AI_MODEL_OWNER_USERNAME).limit(1)
    if for_update:
        statement = statement.with_for_update()
    return db.scalar(statement)


def get_global_ai_model_config(
    db: Session,
    *,
    enabled_only: bool = True,
    for_update: bool = False,
    legacy_fallback_user_id: int | None = None,
) -> AiModelConfig | None:
    """Resolve the DeepSeek configuration shared by every application user.

    The owner is deliberately selected by the stable username instead of the
    current request user. This prevents any personal model row from affecting
    news analysis, strategy previews, monitoring, or algorithm optimization.
    """

    owner = get_global_ai_model_owner(db, for_update=for_update)
    if owner is None:
        # Compatibility for pre-migration/test databases only. Once the stable
        # y0ur account exists, personal rows can never influence model calls.
        if legacy_fallback_user_id is None:
            return None
        statement = (
            select(AiModelConfig)
            .where(
                AiModelConfig.user_id == legacy_fallback_user_id,
                AiModelConfig.provider_code == GLOBAL_AI_MODEL_PROVIDER_CODE,
            )
            .order_by(
                AiModelConfig.is_default.desc(),
                AiModelConfig.updated_at.desc(),
                AiModelConfig.id.desc(),
            )
            .limit(1)
        )
        if enabled_only:
            statement = statement.where(AiModelConfig.is_enabled.is_(True))
        if for_update:
            statement = statement.with_for_update()
        return db.scalar(statement)
    statement = (
        select(AiModelConfig)
        .where(
            AiModelConfig.user_id == owner.id,
            AiModelConfig.provider_code == GLOBAL_AI_MODEL_PROVIDER_CODE,
        )
        .order_by(
            AiModelConfig.is_default.desc(),
            AiModelConfig.updated_at.desc(),
            AiModelConfig.id.desc(),
        )
        .limit(1)
    )
    if enabled_only:
        statement = statement.where(AiModelConfig.is_enabled.is_(True))
    if for_update:
        statement = statement.with_for_update()
    return db.scalar(statement)


def global_ai_model_configured(
    db: Session, *, legacy_fallback_user_id: int | None = None
) -> bool:
    return (
        get_global_ai_model_config(db, legacy_fallback_user_id=legacy_fallback_user_id)
        is not None
    )
