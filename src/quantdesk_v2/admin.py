# ruff: noqa: S608 -- dynamic filters only combine fixed server-side SQL clauses.

from __future__ import annotations

import json
import threading
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request
from sqlalchemy import case, func, or_, select, text, update
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from . import market_config as config_loader
from . import news as market_news
from .ai_model_config import (
    GLOBAL_AI_MODEL_OWNER_USERNAME,
    get_global_ai_model_config,
    get_global_ai_model_owner,
)
from .ai_providers import get_ai_provider
from .database import get_db
from .dependencies import require_admin, require_admin_write
from .models import (
    AdminSetting,
    AiModelConfig,
    AiMonitorRun,
    AuditLog,
    CollectorStatus,
    CompanyProfile,
    NewsAiBatch,
    NewsSourceSetting,
    Security,
    SecurityFundamentalAnalysis,
    SecurityResearchSource,
    SecuritySymbolMapping,
    User,
    UserSession,
    utcnow,
)
from .news_ai import CHUNK_SIZE, run_news_ai_batch
from .schemas import (
    AdminAiModelConfigUpdate,
    AdminAlertRulesUpdate,
    AdminCleanupRequest,
    AdminNewsAiBatchCreate,
    AdminNewsSourceCreate,
    AdminNewsSourceUpdate,
    AdminUnusualWhalesConfigUpdate,
    AdminUserUpdate,
    MessageOut,
)
from .security import CredentialCipher, SecurityError, api_key_fingerprint
from .stock_library import security_out, sync_company_profile
from .strategy_ai import StrategyAiError, _chat_http_transport
from .tradfi_universe import (
    BINANCE_TRADFI_SOURCE,
    BINANCE_TRADFI_STATUS_KEY,
    sync_missing_company_profiles,
    sync_tradfi_universe,
)

router = APIRouter(prefix="/api/v2/admin", tags=["admin"])
COLLECTOR_NAMES = {"price", "ticker", "depth", "kline", "news", "social", "paper"}
FLASH_SOURCE_SEED_KEY = "news_source_seed:taoz_flash_v1"
UNUSUAL_WHALES_SOURCE_SEED_KEY = "news_source_seed:unusual_whales_v1"
SPECIAL_NEWS_SOURCE_SEEDS = {
    "taoz_flash": FLASH_SOURCE_SEED_KEY,
    "unusual_whales": UNUSUAL_WHALES_SOURCE_SEED_KEY,
}
UNUSUAL_WHALES_MARKET_DATA_KEY = "market_data:unusual_whales:v1"
COLLECTOR_STALE_SECONDS = {
    "price": 20,
    "ticker": 150,
    "depth": 30,
    "kline": 180,
    "news": 90,
    "social": 1200,
    "paper": 180,
}
_CLEANUP_COUNT_SQL = {
    "alerts": "SELECT COUNT(*) FROM `alerts` WHERE `ts` < :cutoff",
    "news": "SELECT COUNT(*) FROM `news` WHERE `ts` < :cutoff",
    "scores": "SELECT COUNT(*) FROM `scores` WHERE `open_time` < :cutoff",
}
_STORAGE_SQL = {
    "alerts": "SELECT COUNT(*) total, MIN(ts) oldest, MAX(ts) newest FROM alerts",
    "news": "SELECT COUNT(*) total, MIN(ts) oldest, MAX(ts) newest FROM news",
    "scores": "SELECT COUNT(*) total, MIN(open_time) oldest, MAX(open_time) newest FROM scores",
    "klines": "SELECT COUNT(*) total, MIN(open_time) oldest, MAX(open_time) newest FROM klines",
    "ticker": "SELECT COUNT(*) total, MIN(ts) oldest, MAX(ts) newest FROM ticker",
    "audit_logs": "SELECT COUNT(*) total FROM audit_logs",
}


def _global_ai_model_out(db: Session) -> dict[str, Any]:
    preset = get_ai_provider("deepseek")
    if preset is None:  # pragma: no cover - fixed server registry invariant
        raise HTTPException(status_code=500, detail="DeepSeek 服务端配置缺失")
    owner = get_global_ai_model_owner(db)
    config = get_global_ai_model_config(db, enabled_only=False)
    return {
        "scope": "global",
        "owner_username": GLOBAL_AI_MODEL_OWNER_USERNAME,
        "owner_exists": owner is not None,
        "provider_code": preset.code,
        "provider_name": preset.label,
        "base_url": preset.base_url,
        "models": list(preset.models),
        "default_model": preset.default_model,
        "configured": config is not None and bool(config.api_key_encrypted),
        "id": config.public_id if config is not None else None,
        "display_name": config.display_name if config is not None else "全局 DeepSeek",
        "model_name": config.model_name if config is not None else preset.default_model,
        "api_key_configured": bool(config and config.api_key_encrypted),
        "api_key_fingerprint": config.api_key_fingerprint if config is not None else None,
        "api_key_version": config.api_key_version if config is not None else 0,
        "is_enabled": bool(config and config.is_enabled),
        "updated_at": config.updated_at if config is not None else None,
    }


def _client_ip(request: Request) -> str | None:
    return request.client.host[:45] if request.client else None


def _audit(
    db: Session,
    request: Request,
    user_id: int,
    action: str,
    resource_type: str,
    resource_id: str,
    metadata: dict[str, Any] | None = None,
) -> None:
    db.add(
        AuditLog(
            user_id=user_id,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            ip_address=_client_ip(request),
            metadata_json=metadata,
        )
    )


def _iso_from_unix(value: int | None) -> str | None:
    if not value:
        return None
    return datetime.fromtimestamp(value, UTC).isoformat().replace("+00:00", "Z")


def _news_ai_batch_out(batch: NewsAiBatch) -> dict[str, Any]:
    selected = max(0, int(batch.selected_count or 0))
    finished = max(0, int(batch.processed_count or 0)) + max(0, int(batch.failed_count or 0))
    return {
        "id": batch.id,
        "status": batch.status,
        "requested_count": batch.requested_count,
        "selected_count": selected,
        "processed_count": batch.processed_count,
        "failed_count": batch.failed_count,
        "progress": round(min(1.0, finished / selected), 4) if selected else 0.0,
        "chunk_size": batch.chunk_size,
        "provider_code": batch.provider_code,
        "model_name": batch.model_name,
        "market_sentiment": batch.market_sentiment,
        "market_confidence": (
            float(batch.market_confidence) if batch.market_confidence is not None else None
        ),
        "market_summary": batch.market_summary,
        "result": batch.result_json,
        "error_message": batch.error_message,
        "started_at": batch.started_at,
        "completed_at": batch.completed_at,
        "created_at": batch.created_at,
        "updated_at": batch.updated_at,
    }


def _json_list(value: Any) -> list[Any] | None:
    if value is None or isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return None
        return parsed if isinstance(parsed, list) else None
    return None


def _default_alert_rules() -> dict[str, Any]:
    settings = config_loader.settings
    return {
        "score_alert_long": int(settings.get("score_alert_long", 60)),
        "score_alert_short": int(settings.get("score_alert_short", -60)),
        "score_alert_position": int(settings.get("score_alert_position", 40)),
        "spike_alert_pct_5m": float(settings.get("spike_alert_pct_5m", 2.0)),
        "watchlist_only": True,
        "enabled_timeframes": list(settings.get("timeframes", ["15m", "1h", "4h"])),
    }


def _setting_payload(db: Session, key: str, default: dict[str, Any]) -> tuple[dict[str, Any], int]:
    setting = db.get(AdminSetting, key)
    if setting is None:
        return default, 0
    value = setting.value_json if isinstance(setting.value_json, dict) else {}
    return {**default, **value}, setting.version


def _default_unusual_whales_config() -> dict[str, Any]:
    return AdminUnusualWhalesConfigUpdate().model_dump(exclude={"api_key"})


def _unusual_whales_config_out(
    db: Session,
    request: Request,
) -> dict[str, Any]:
    raw, version = _setting_payload(
        db,
        UNUSUAL_WHALES_MARKET_DATA_KEY,
        _default_unusual_whales_config(),
    )
    try:
        config = AdminUnusualWhalesConfigUpdate.model_validate(raw).model_dump(
            exclude={"api_key"}
        )
    except ValueError:
        config = _default_unusual_whales_config()

    credential = db.get(AdminSetting, market_news.UNUSUAL_WHALES_CREDENTIAL_KEY)
    credential_value = (
        credential.value_json
        if credential is not None and isinstance(credential.value_json, dict)
        else {}
    )
    fingerprint = credential_value.get("api_key_fingerprint")
    configured = bool(credential_value.get("api_key_encrypted"))
    if not configured:
        legacy_key = request.app.state.settings.unusual_whales_api_key.get_secret_value().strip()
        configured = bool(legacy_key)
        fingerprint = api_key_fingerprint(legacy_key) if legacy_key else None

    stream = getattr(request.app.state, "unusual_whales_stream", None)
    stream_health: dict[str, Any]
    if stream is not None and hasattr(stream, "health_snapshot"):
        try:
            snapshot = stream.health_snapshot()
            stream_health = snapshot if isinstance(snapshot, dict) else {}
        except Exception:
            stream_health = {"status": "error", "connected": False}
    else:
        stream_health = {"status": "disabled", "connected": False}

    channel_health = getattr(request.app.state, "unusual_whales_channel_health", {})
    if callable(channel_health):
        try:
            channel_health = channel_health()
        except (RuntimeError, ValueError):
            channel_health = {}
    if not isinstance(channel_health, dict):
        channel_health = {}
    runtime_rest_health = stream_health.get("rest")
    runtime_rest_health = (
        dict(runtime_rest_health) if isinstance(runtime_rest_health, dict) else {}
    )
    return {
        "scope": "platform",
        "configured": configured,
        "api_key_fingerprint": fingerprint,
        "credential_source": "database" if credential is not None else "environment",
        "version": version,
        "updated_at": credential.updated_at if credential is not None else None,
        "feature_version": "uw_features_v2",
        "weights_version": f"uw_weights_v{max(0, version)}",
        "decision_version": "hard_gate_v3_nbbo",
        "config": config,
        "health": {
            "rest": {
                **runtime_rest_health,
                "status": (
                    str(runtime_rest_health.get("status"))
                    if runtime_rest_health
                    else "ready"
                    if configured and config["rest_enabled"]
                    else "disabled"
                ),
                "configured": configured,
            },
            "websocket": stream_health,
            "channels": channel_health,
            "leadership": (
                dict(stream_health.get("leadership") or {})
                if isinstance(stream_health.get("leadership"), dict)
                else {"status": "unknown", "is_leader": False}
            ),
            "retention": (
                dict(stream_health.get("retention") or {})
                if isinstance(stream_health.get("retention"), dict)
                else {"status": "unknown"}
            ),
        },
    }


def _sync_news_sources(db: Session) -> None:
    # Once seeded, the database is authoritative. This prevents an intentionally
    # deleted file-defined source from being recreated on the next admin read.
    configured_sources = config_loader.settings.get("news_sources", [])
    if not db.scalar(select(func.count()).select_from(NewsSourceSetting)):
        sources_to_seed = configured_sources
    else:
        source_types_to_seed = {
            feed_type
            for feed_type, seed_key in SPECIAL_NEWS_SOURCE_SEEDS.items()
            if db.get(AdminSetting, seed_key) is None
        }
        sources_to_seed = [
            source
            for source in configured_sources
            if source.get("feed_type") in source_types_to_seed
        ]
    for source in sources_to_seed:
        name = str(source.get("name") or "").strip()
        url = str(source.get("url") or "").strip()
        if not name or not url or db.get(NewsSourceSetting, name) is not None:
            continue
        db.add(
            NewsSourceSetting(
                name=name,
                url=url,
                feed_type=str(source.get("feed_type") or "rss"),
                lang=str(source.get("lang") or "en"),
                slow=bool(source.get("slow")),
                enabled=bool(source.get("enabled", True)),
                weight=int(source.get("weight") or 100),
                hourly_limit=int(source.get("hourly_limit") or 600),
            )
        )
    configured_feed_types = {source.get("feed_type") for source in configured_sources}
    for feed_type, seed_key in SPECIAL_NEWS_SOURCE_SEEDS.items():
        if feed_type in configured_feed_types and db.get(AdminSetting, seed_key) is None:
            db.add(
                AdminSetting(
                    key=seed_key,
                    value_json={"seeded": True},
                    version=1,
                )
            )
    db.flush()


def _initialize_admin_runtime_job(engine: Engine) -> None:
    """Recover stale tasks without making API availability depend on a bulk update."""

    try:
        with Session(engine) as db:
            now = utcnow()
            stale_cutoff = now - timedelta(minutes=10)
            db.execute(
                update(NewsAiBatch)
                .where(
                    NewsAiBatch.status.in_(("pending", "running")),
                    NewsAiBatch.updated_at < stale_cutoff,
                )
                .values(
                    status="failed",
                    error_message="服务重启导致任务中断，请重新发起分析",
                    completed_at=now,
                )
            )
            db.execute(
                update(AiMonitorRun)
                .where(
                    AiMonitorRun.status.in_(("pending", "running")),
                    AiMonitorRun.updated_at < stale_cutoff,
                )
                .values(
                    status="failed",
                    error_message="服务重启或任务超时，请重新发起",
                    completed_at=now,
                )
            )
            _sync_news_sources(db)
            db.commit()
    except SQLAlchemyError as exc:
        # A degraded remote database must not keep the health endpoint offline.
        # Periodic workers have their own stale-run recovery and will retry later.
        print(f"[admin] deferred startup recovery failed: {type(exc).__name__}")


def initialize_admin_runtime(engine: Engine) -> None:
    """Run non-critical startup recovery asynchronously."""

    threading.Thread(
        target=_initialize_admin_runtime_job,
        args=(engine,),
        name="quantdesk-admin-startup-recovery",
        daemon=True,
    ).start()


def _collector_out(row: CollectorStatus, now: int, paused: bool) -> dict[str, Any]:
    lag = max(0, now - int(row.heartbeat_at or 0))
    if paused:
        health = "paused"
    elif lag > COLLECTOR_STALE_SECONDS.get(row.name, 180):
        health = "error"
    elif row.last_error_at and (not row.last_success_at or row.last_error_at > row.last_success_at):
        health = "warning"
    else:
        health = "ok"
    return {
        "name": row.name,
        "health": health,
        "paused": paused,
        "heartbeat_at": row.heartbeat_at,
        "heartbeat_iso": _iso_from_unix(row.heartbeat_at),
        "lag_seconds": lag,
        "last_success_at": row.last_success_at,
        "last_error_at": row.last_error_at,
        "last_error": row.last_error,
        "cycles": row.cycles,
        "items": row.items,
        "details": row.details_json or {},
    }


def _collectors(db: Session) -> list[dict[str, Any]]:
    now = int(time.time())
    pause_settings = {
        setting.key.removeprefix("collector_pause:"): bool(setting.value_json.get("paused"))
        for setting in db.scalars(
            select(AdminSetting).where(AdminSetting.key.like("collector_pause:%"))
        )
        if isinstance(setting.value_json, dict)
    }
    rows = {row.name: row for row in db.scalars(select(CollectorStatus))}
    output = []
    for name in sorted(COLLECTOR_NAMES | set(rows)):
        row = rows.get(name)
        if row is None:
            output.append(
                {
                    "name": name,
                    "health": "paused" if pause_settings.get(name) else "unknown",
                    "paused": pause_settings.get(name, False),
                    "heartbeat_at": None,
                    "heartbeat_iso": None,
                    "lag_seconds": None,
                    "last_success_at": None,
                    "last_error_at": None,
                    "last_error": None,
                    "cycles": 0,
                    "items": 0,
                    "details": {},
                }
            )
        else:
            output.append(_collector_out(row, now, pause_settings.get(name, False)))
    return output


@router.get("/ai-model")
def global_ai_model(
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
) -> dict[str, Any]:
    """Return the platform-wide DeepSeek setting without exposing its secret."""

    return _global_ai_model_out(db)


@router.put("/ai-model")
def update_global_ai_model(
    payload: AdminAiModelConfigUpdate,
    request: Request,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin_write),
) -> dict[str, Any]:
    """Create or update the DeepSeek credential shared by every user."""

    owner = get_global_ai_model_owner(db, for_update=True)
    if owner is None:
        raise HTTPException(
            status_code=409,
            detail=f"全局模型所属账号 {GLOBAL_AI_MODEL_OWNER_USERNAME} 不存在",
        )
    config = get_global_ai_model_config(db, enabled_only=False, for_update=True)
    raw_api_key = payload.api_key.get_secret_value() if payload.api_key is not None else None
    if config is None and raw_api_key is None:
        raise HTTPException(status_code=422, detail="首次配置必须填写 DeepSeek API Key")

    cipher = CredentialCipher(request.app.state.settings.credential_master_key.get_secret_value())
    previous_fingerprint = config.api_key_fingerprint if config is not None else None
    try:
        if config is None:
            config = AiModelConfig(
                public_id=str(uuid.uuid4()),
                user_id=owner.id,
                provider_code="deepseek",
                display_name="全局 DeepSeek",
                model_name=payload.model_name,
                api_key_encrypted=cipher.encrypt(raw_api_key or ""),
                api_key_fingerprint=api_key_fingerprint(raw_api_key or ""),
                api_key_version=1,
                is_enabled=False,
                is_default=False,
            )
            db.add(config)
            db.flush()
        else:
            config.model_name = payload.model_name
            if raw_api_key is not None:
                config.api_key_encrypted = cipher.encrypt(raw_api_key)
                config.api_key_fingerprint = api_key_fingerprint(raw_api_key)
                config.api_key_version += 1

        # The table enforces one default row per owner through a generated
        # unique column, so old defaults must be cleared before enabling this one.
        for item in db.scalars(
            select(AiModelConfig).where(
                AiModelConfig.user_id == owner.id,
                AiModelConfig.is_default.is_(True),
            )
        ):
            item.is_default = False
        config.is_default = False
        config.is_enabled = payload.is_enabled
        db.flush()
        config.is_default = payload.is_enabled
        _audit(
            db,
            request,
            admin.id,
            "admin.ai_model.update",
            "ai_model_config",
            config.public_id,
            {
                "owner_username": owner.username,
                "provider_code": "deepseek",
                "model_name": config.model_name,
                "enabled": config.is_enabled,
                "credential_replaced": raw_api_key is not None,
                "previous_fingerprint": previous_fingerprint,
                "fingerprint": config.api_key_fingerprint,
            },
        )
        db.commit()
    except (IntegrityError, SecurityError):
        db.rollback()
        raise HTTPException(status_code=409, detail="全局 DeepSeek 配置保存冲突") from None
    return _global_ai_model_out(db)


@router.post("/ai-model/test", response_model=MessageOut)
def test_global_ai_model(
    request: Request,
    db: Session = Depends(get_db),
    _: User = Depends(require_admin_write),
) -> MessageOut:
    """Test the saved shared credential against the allowlisted DeepSeek origin."""

    config = get_global_ai_model_config(db)
    preset = get_ai_provider("deepseek")
    if config is None or preset is None:
        raise HTTPException(status_code=422, detail="请先配置并启用全局 DeepSeek")
    try:
        api_key = CredentialCipher(
            request.app.state.settings.credential_master_key.get_secret_value()
        ).decrypt(config.api_key_encrypted)
        body = json.dumps(
            {
                "model": config.model_name,
                "messages": [{"role": "user", "content": "Reply with OK."}],
                "max_tokens": 1,
                "stream": False,
            },
            separators=(",", ":"),
        ).encode("utf-8")
        db.rollback()
        status_code, _ = _chat_http_transport(
            preset,
            body,
            {
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            15.0,
        )
    except SecurityError:
        raise HTTPException(status_code=503, detail="全局 DeepSeek 密钥无法解密") from None
    except TimeoutError:
        raise HTTPException(status_code=504, detail="连接 DeepSeek 超时") from None
    except (StrategyAiError, OSError):
        raise HTTPException(status_code=502, detail="无法连接 DeepSeek") from None
    if status_code in {401, 403}:
        raise HTTPException(status_code=422, detail="DeepSeek API Key 无效或无权访问该模型")
    if status_code in {408, 504}:
        raise HTTPException(status_code=504, detail="连接 DeepSeek 超时")
    if not 200 <= status_code < 300:
        raise HTTPException(status_code=502, detail=f"DeepSeek 返回错误（HTTP {status_code}）")
    return MessageOut(message="全局 DeepSeek API 测试成功")


@router.get("/market-data/unusual-whales")
def unusual_whales_market_data_config(
    request: Request,
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
) -> dict[str, Any]:
    """Return the platform market-data policy without exposing its credential."""

    return _unusual_whales_config_out(db, request)


@router.put("/market-data/unusual-whales")
def update_unusual_whales_market_data_config(
    payload: AdminUnusualWhalesConfigUpdate,
    request: Request,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin_write),
) -> dict[str, Any]:
    """Publish channel switches, safety limits, domain weights and an optional key."""

    values = payload.model_dump(exclude={"api_key"})
    setting = db.get(AdminSetting, UNUSUAL_WHALES_MARKET_DATA_KEY)
    if setting is None:
        setting = AdminSetting(
            key=UNUSUAL_WHALES_MARKET_DATA_KEY,
            value_json=values,
            version=1,
            updated_by=admin.id,
        )
        db.add(setting)
    else:
        setting.value_json = values
        setting.version += 1
        setting.updated_by = admin.id
        setting.updated_at = utcnow()

    raw_api_key = payload.api_key.get_secret_value().strip() if payload.api_key else None
    credential_replaced = raw_api_key is not None
    if raw_api_key is not None:
        cipher = CredentialCipher(
            request.app.state.settings.credential_master_key.get_secret_value()
        )
        credential = db.get(AdminSetting, market_news.UNUSUAL_WHALES_CREDENTIAL_KEY)
        credential_version = int(
            (credential.value_json or {}).get("api_key_version", 0)
        ) if credential is not None and isinstance(credential.value_json, dict) else 0
        credential_payload = {
            "api_key_encrypted": cipher.encrypt(raw_api_key),
            "api_key_fingerprint": api_key_fingerprint(raw_api_key),
            "api_key_version": credential_version + 1,
        }
        if credential is None:
            credential = AdminSetting(
                key=market_news.UNUSUAL_WHALES_CREDENTIAL_KEY,
                value_json=credential_payload,
                version=1,
                updated_by=admin.id,
            )
            db.add(credential)
        else:
            credential.value_json = credential_payload
            credential.version += 1
            credential.updated_by = admin.id
            credential.updated_at = utcnow()

    _audit(
        db,
        request,
        admin.id,
        "admin.market_data.unusual_whales.update",
        "admin_setting",
        UNUSUAL_WHALES_MARKET_DATA_KEY,
        {
            **values,
            "credential_replaced": credential_replaced,
            "published_version": setting.version,
        },
    )
    db.commit()

    subscription_errors: list[str] = []
    apply_runtime_config = getattr(
        request.app.state,
        "apply_unusual_whales_runtime_config",
        None,
    )
    if callable(apply_runtime_config):
        try:
            apply_runtime_config(values)
        except (RuntimeError, ValueError) as exc:
            # The saved configuration remains authoritative. A reconnect will
            # restore subscriptions even if this process is currently degraded.
            subscription_errors.append(type(exc).__name__)
    result = _unusual_whales_config_out(db, request)
    if subscription_errors:
        result["subscription_errors"] = subscription_errors
    return result


@router.post("/market-data/unusual-whales/test", response_model=MessageOut)
def test_unusual_whales_market_data(
    request: Request,
    db: Session = Depends(get_db),
    _: User = Depends(require_admin_write),
) -> MessageOut:
    """Perform a bounded authenticated REST probe without returning vendor payloads."""

    client = getattr(request.app.state, "unusual_whales_market_client", None)
    if client is None:
        raise HTTPException(status_code=503, detail="Unusual Whales 客户端尚未初始化")
    db.rollback()
    try:
        snapshot = client.market_tide()
    except (OSError, RuntimeError, TimeoutError, ValueError):
        raise HTTPException(status_code=502, detail="Unusual Whales 连接测试失败") from None
    if not isinstance(snapshot, dict) or not snapshot:
        raise HTTPException(status_code=502, detail="Unusual Whales 未返回有效市场数据")
    return MessageOut(message="Unusual Whales REST 连接正常")


@router.get("/overview")
def overview(
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
) -> dict[str, Any]:
    now = int(time.time())
    alerts = (
        db.execute(
            text(
                "SELECT COUNT(*) total, SUM(CASE WHEN `read`=0 THEN 1 ELSE 0 END) unread, "
                "MAX(ts) newest FROM alerts"
            )
        )
        .mappings()
        .one()
    )
    news = (
        db.execute(
            text("SELECT COUNT(*) total, COUNT(DISTINCT source) sources, MAX(ts) newest FROM news")
        )
        .mappings()
        .one()
    )
    ticker = db.execute(text("SELECT COUNT(*) total, MAX(ts) newest FROM ticker")).mappings().one()
    scores = (
        db.execute(text("SELECT COUNT(*) total, MAX(open_time) newest FROM scores"))
        .mappings()
        .one()
    )
    users = (
        db.execute(
            text(
                "SELECT COUNT(*) total, SUM(CASE WHEN is_active=1 THEN 1 ELSE 0 END) active FROM users"
            )
        )
        .mappings()
        .one()
    )
    collectors = _collectors(db)
    unhealthy = sum(1 for item in collectors if item["health"] in {"error", "warning", "unknown"})
    day_cutoff = now - 86400
    alert_kinds = {
        str(row["kind"]): int(row["total"])
        for row in db.execute(
            text("SELECT kind,COUNT(*) total FROM alerts WHERE ts>=:cutoff GROUP BY kind"),
            {"cutoff": day_cutoff},
        ).mappings()
    }
    alert_directions = {
        str(row["direction"]): int(row["total"])
        for row in db.execute(
            text(
                "SELECT direction,COUNT(*) total FROM alerts "
                "WHERE ts>=:cutoff GROUP BY direction"
            ),
            {"cutoff": day_cutoff},
        ).mappings()
    }
    news_sentiment = {
        str(row["sentiment"] or "unknown"): int(row["total"])
        for row in db.execute(
            text(
                "SELECT sentiment,COUNT(*) total FROM news "
                "WHERE ts>=:cutoff GROUP BY sentiment"
            ),
            {"cutoff": day_cutoff},
        ).mappings()
    }
    return {
        "generated_at": now,
        "health": "ok" if unhealthy == 0 else "warning",
        "unhealthy_collectors": unhealthy,
        "alerts": {
            "total": int(alerts["total"] or 0),
            "unread": int(alerts["unread"] or 0),
            "newest": alerts["newest"],
        },
        "news": {
            "total": int(news["total"] or 0),
            "sources": int(news["sources"] or 0),
            "newest": news["newest"],
        },
        "ticker": {
            "total": int(ticker["total"] or 0),
            "newest": ticker["newest"],
            "lag_seconds": max(0, now - int(ticker["newest"] or 0)) if ticker["newest"] else None,
        },
        "scores": {"total": int(scores["total"] or 0), "newest": scores["newest"]},
        "users": {"total": int(users["total"] or 0), "active": int(users["active"] or 0)},
        "last_24h": {
            "alerts": sum(alert_kinds.values()),
            "alert_kinds": alert_kinds,
            "alert_directions": alert_directions,
            "news": sum(news_sentiment.values()),
            "news_sentiment": news_sentiment,
        },
    }


@router.get("/alerts")
def alerts(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0, le=100000),
    user_id: int | None = Query(default=None, ge=1),
    kind: str | None = Query(default=None, max_length=32),
    direction: str | None = Query(default=None, max_length=16),
    symbol: str | None = Query(default=None, max_length=32),
    unread: bool | None = Query(default=None),
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
) -> dict[str, Any]:
    conditions = ["1=1"]
    params: dict[str, Any] = {"limit": limit, "offset": offset}
    for column, value in (("user_id", user_id), ("kind", kind), ("direction", direction)):
        if value is not None:
            conditions.append(f"a.{column}=:{column}")
            params[column] = value
    if symbol:
        conditions.append("a.symbol LIKE :symbol")
        params["symbol"] = f"%{symbol.strip().upper()}%"
    if unread is not None:
        conditions.append("a.`read`=:is_read")
        params["is_read"] = not unread
    where = " AND ".join(conditions)
    total = int(
        db.execute(
            text(f"SELECT COUNT(*) FROM alerts a WHERE {where}"),
            params,
        ).scalar_one()
    )
    rows = db.execute(
        text(
            "SELECT a.id,a.user_id,u.username,a.ts,a.symbol,a.kind,a.direction,a.score,"
            f"a.message,a.detail,a.`read` FROM alerts a LEFT JOIN users u ON u.id=a.user_id WHERE {where} "
            "ORDER BY a.ts DESC,a.id DESC LIMIT :limit OFFSET :offset"
        ),
        params,
    ).mappings()
    return {"total": total, "limit": limit, "offset": offset, "items": [dict(row) for row in rows]}


@router.get("/news")
def admin_news(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0, le=100000),
    source: str | None = Query(default=None, max_length=80),
    sentiment: str | None = Query(default=None, max_length=32),
    query: str | None = Query(default=None, max_length=120),
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
) -> dict[str, Any]:
    conditions = ["1=1"]
    params: dict[str, Any] = {"limit": limit, "offset": offset}
    if source:
        conditions.append("source=:source")
        params["source"] = source
    if sentiment:
        conditions.append("sentiment=:sentiment")
        params["sentiment"] = sentiment
    if query:
        conditions.append("(title LIKE :query OR title_zh LIKE :query OR summary LIKE :query)")
        params["query"] = f"%{query.strip()}%"
    where = " AND ".join(conditions)
    total = int(
        db.execute(
            text(f"SELECT COUNT(*) FROM news WHERE {where}"),
            params,
        ).scalar_one()
    )
    rows = db.execute(
        text(
            "SELECT id,ts,source,lang,title,title_zh,link,sentiment,rule_sentiment,summary,"
            "related_us_stocks,ai_sentiment,ai_confidence,ai_impact_strength,"
            "ai_time_horizon,ai_category,ai_reason,ai_model,ai_batch_id,ai_analyzed_at "
            "FROM news "
            f"WHERE {where} ORDER BY ts DESC LIMIT :limit OFFSET :offset"
        ),
        params,
    ).mappings()
    items: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        item["related_us_stocks"] = _json_list(item.get("related_us_stocks"))
        if item.get("ai_confidence") is not None:
            item["ai_confidence"] = float(item["ai_confidence"])
        items.append(item)
    return {"total": total, "limit": limit, "offset": offset, "items": items}


@router.get("/news-ai-batches")
def news_ai_batches(
    limit: int = Query(default=10, ge=1, le=50),
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
) -> dict[str, Any]:
    rows = db.scalars(
        select(NewsAiBatch).order_by(NewsAiBatch.created_at.desc()).limit(limit)
    ).all()
    return {"items": [_news_ai_batch_out(row) for row in rows]}


@router.get("/news-ai-batches/{batch_id}")
def news_ai_batch(
    batch_id: uuid.UUID,
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
) -> dict[str, Any]:
    batch = db.get(NewsAiBatch, str(batch_id))
    if batch is None:
        raise HTTPException(status_code=404, detail="news AI batch not found")
    return _news_ai_batch_out(batch)


@router.post("/news-ai-batches", status_code=202)
def create_news_ai_batch(
    payload: AdminNewsAiBatchCreate,
    background_tasks: BackgroundTasks,
    request: Request,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin_write),
) -> dict[str, Any]:
    active_batch = db.scalar(
        select(NewsAiBatch.id)
        .where(NewsAiBatch.status.in_(("pending", "running")))
        .order_by(NewsAiBatch.created_at.desc())
        .limit(1)
    )
    if active_batch is not None:
        raise HTTPException(status_code=409, detail="已有 AI 新闻分析批次正在运行")
    if get_global_ai_model_config(db, legacy_fallback_user_id=admin.id) is None:
        raise HTTPException(status_code=422, detail="请先在管理后台配置并启用全局 DeepSeek")
    batch = NewsAiBatch(
        id=str(uuid.uuid4()),
        started_by=admin.id,
        status="pending",
        requested_count=payload.count,
        chunk_size=CHUNK_SIZE,
    )
    db.add(batch)
    _audit(
        db,
        request,
        admin.id,
        "admin.news_ai_batch.create",
        "news_ai_batch",
        batch.id,
        {"requested_count": payload.count},
    )
    db.commit()
    db.refresh(batch)
    background_tasks.add_task(
        run_news_ai_batch,
        request.app.state.database_engine,
        batch.id,
        request.app.state.settings.credential_master_key.get_secret_value(),
    )
    return _news_ai_batch_out(batch)


@router.post("/news-ai-batches/{batch_id}/retry", status_code=202)
def retry_news_ai_batch(
    batch_id: uuid.UUID,
    background_tasks: BackgroundTasks,
    request: Request,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin_write),
) -> dict[str, Any]:
    batch = db.get(NewsAiBatch, str(batch_id))
    if batch is None:
        raise HTTPException(status_code=404, detail="news AI batch not found")
    if batch.status not in {"failed", "partial"}:
        raise HTTPException(status_code=409, detail="该批次当前不能续跑")
    active_batch = db.scalar(
        select(NewsAiBatch.id)
        .where(NewsAiBatch.status.in_(("pending", "running")))
        .limit(1)
    )
    if active_batch is not None:
        raise HTTPException(status_code=409, detail="已有 AI 新闻分析批次正在运行")
    batch.status = "pending"
    batch.failed_count = 0
    batch.error_message = None
    batch.completed_at = None
    batch.chunk_size = CHUNK_SIZE
    _audit(
        db,
        request,
        admin.id,
        "admin.news_ai_batch.retry",
        "news_ai_batch",
        batch.id,
        {"processed_count": batch.processed_count},
    )
    db.commit()
    db.refresh(batch)
    background_tasks.add_task(
        run_news_ai_batch,
        request.app.state.database_engine,
        batch.id,
        request.app.state.settings.credential_master_key.get_secret_value(),
    )
    return _news_ai_batch_out(batch)


@router.get("/stock-library")
def stock_library(
    query: str | None = Query(default=None, max_length=64),
    security_type: str | None = Query(default=None, max_length=32),
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
) -> dict[str, Any]:
    statement = select(Security).order_by(Security.symbol)
    if query:
        needle = f"%{query.strip()}%"
        statement = statement.where(
            or_(
                Security.symbol.like(needle),
                Security.company_name.like(needle),
                Security.company_name_zh.like(needle),
                Security.id.in_(
                    select(SecuritySymbolMapping.security_id).where(
                        SecuritySymbolMapping.source_symbol.like(needle)
                    )
                ),
            )
        )
    if security_type:
        statement = statement.where(Security.security_type == security_type)
    rows = list(db.scalars(statement.limit(500)).all())
    security_ids = [int(row.id) for row in rows]
    profiles = {
        int(row.security_id): row
        for row in db.scalars(
            select(CompanyProfile).where(CompanyProfile.security_id.in_(security_ids))
        ).all()
    } if security_ids else {}
    analyses: dict[int, SecurityFundamentalAnalysis] = {}
    if security_ids:
        for row in db.scalars(
            select(SecurityFundamentalAnalysis)
            .where(SecurityFundamentalAnalysis.security_id.in_(security_ids))
            .order_by(
                SecurityFundamentalAnalysis.security_id,
                SecurityFundamentalAnalysis.as_of_date.desc(),
            )
        ).all():
            analyses.setdefault(int(row.security_id), row)
    mappings: dict[int, list[SecuritySymbolMapping]] = {}
    if security_ids:
        for row in db.scalars(
            select(SecuritySymbolMapping)
            .where(SecuritySymbolMapping.security_id.in_(security_ids))
            .order_by(SecuritySymbolMapping.source, SecuritySymbolMapping.source_symbol)
        ).all():
            mappings.setdefault(int(row.security_id), []).append(row)
    items = [
        security_out(
            row,
            profiles.get(int(row.id)),
            analyses.get(int(row.id)),
            mappings.get(int(row.id), ()),
        )
        for row in rows
    ]
    binance_counts = db.execute(
        select(
            func.count(SecuritySymbolMapping.id),
            func.sum(
                case((SecuritySymbolMapping.source_status == "TRADING", 1), else_=0)
            ),
            func.sum(
                case((SecuritySymbolMapping.monitor_enabled.is_(True), 1), else_=0)
            ),
            func.sum(
                case(
                    (SecuritySymbolMapping.mapping_status == "REVIEW_REQUIRED", 1),
                    else_=0,
                )
            ),
        ).where(SecuritySymbolMapping.source == BINANCE_TRADFI_SOURCE)
    ).one()
    sync_state = db.get(AdminSetting, BINANCE_TRADFI_STATUS_KEY)
    verified_statuses = {"VERIFIED", "AUTO_VERIFIED"}
    return {
        "total": len(items),
        "verified": sum(
            item["verification_status"] in verified_statuses for item in items
        ),
        "review_required": sum(item["verification_status"] == "REVIEW_REQUIRED" for item in items),
        "pending": sum(item["verification_status"] not in verified_statuses | {"REVIEW_REQUIRED"} for item in items),
        "binance": {
            "total": int(binance_counts[0] or 0),
            "trading": int(binance_counts[1] or 0),
            "monitor_enabled": int(binance_counts[2] or 0),
            "review_required": int(binance_counts[3] or 0),
            "last_sync": sync_state.value_json if sync_state is not None else None,
        },
        "items": items,
    }


@router.get("/stock-library/{symbol}")
def stock_library_detail(
    symbol: str,
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
) -> dict[str, Any]:
    normalized = symbol.strip().upper()
    security = db.scalar(select(Security).where(Security.symbol == normalized))
    if security is None:
        mapping = db.scalar(
            select(SecuritySymbolMapping).where(
                SecuritySymbolMapping.source == BINANCE_TRADFI_SOURCE,
                SecuritySymbolMapping.source_symbol == normalized
            )
        )
        security = db.get(Security, mapping.security_id) if mapping is not None else None
    if security is None:
        raise HTTPException(status_code=404, detail="security not found")
    profile = db.get(CompanyProfile, security.id)
    analysis = db.scalar(select(SecurityFundamentalAnalysis).where(SecurityFundamentalAnalysis.security_id == security.id).order_by(SecurityFundamentalAnalysis.as_of_date.desc()).limit(1))
    symbol_mappings = list(
        db.scalars(
            select(SecuritySymbolMapping).where(
                SecuritySymbolMapping.security_id == security.id
            )
        ).all()
    )
    result = security_out(security, profile, analysis, symbol_mappings)
    result["research_sources"] = [
        {"source_type": row.source_type, "title": row.title, "url": row.url, "publisher": row.publisher, "published_at": row.published_at, "content_summary": row.content_summary}
        for row in db.scalars(select(SecurityResearchSource).where(SecurityResearchSource.security_id == security.id).order_by(SecurityResearchSource.retrieved_at.desc()).limit(100)).all()
    ]
    return result


@router.post("/stock-library/import", status_code=202)
def import_stock_library(
    request: Request,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin_write),
) -> dict[str, Any]:
    try:
        synced = sync_tradfi_universe(request.app.state.database_engine)
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Binance TradFi universe sync failed: {type(exc).__name__}",
        ) from exc
    result = synced.summary
    config_loader.refresh_symbols(force=True)
    if synced.profile_security_ids:
        background_tasks.add_task(
            sync_missing_company_profiles,
            request.app.state.database_engine,
            request.app.state.finnhub_client,
            synced.profile_security_ids,
        )
    _audit(db, request, admin.id, "admin.stock_library.import", "stock_library", "tradfi", result)
    db.commit()
    return result


@router.post("/stock-library/{symbol}/sync")
def sync_stock_library_profile(
    symbol: str,
    request: Request,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin_write),
) -> dict[str, Any]:
    security = db.scalar(select(Security).where(Security.symbol == symbol.strip().upper()))
    if security is None:
        raise HTTPException(status_code=404, detail="security not found")
    if security.exchange != "US" or not security.finnhub_symbol:
        raise HTTPException(
            status_code=409,
            detail="This security does not have a supported Finnhub US symbol mapping",
        )
    try:
        profile = sync_company_profile(db, request.app.state.finnhub_client, security)
    except Exception as exc:
        category = getattr(exc, "category", "upstream")
        raise HTTPException(status_code=502, detail=f"Finnhub profile sync failed: {category}") from exc
    _audit(db, request, admin.id, "admin.stock_library.sync", "security", security.symbol, {"source": "finnhub"})
    db.commit()
    mappings = list(
        db.scalars(
            select(SecuritySymbolMapping).where(
                SecuritySymbolMapping.security_id == security.id
            )
        ).all()
    )
    return security_out(security, profile, mappings=mappings)


@router.get("/symbols")
def symbols(
    query: str | None = Query(default=None, max_length=32),
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
) -> dict[str, Any]:
    now = int(time.time())
    ticker = {
        row["symbol"]: dict(row)
        for row in db.execute(text("SELECT symbol,price,pct_24h,quote_volume,ts FROM ticker")).mappings()
    }
    scores: dict[str, dict[str, Any]] = {}
    for row in db.execute(
        text(
            "SELECT s.symbol,s.tf,s.score,s.open_time FROM scores s JOIN "
            "(SELECT symbol,tf,MAX(open_time) newest FROM scores GROUP BY symbol,tf) latest "
            "ON latest.symbol=s.symbol AND latest.tf=s.tf AND latest.newest=s.open_time"
        )
    ).mappings():
        scores.setdefault(row["symbol"], {})[row["tf"]] = {
            "score": row["score"],
            "open_time": row["open_time"],
        }
    klines: dict[str, dict[str, int]] = {}
    for row in db.execute(
        text("SELECT symbol,tf,COUNT(*) bars,MAX(open_time) newest FROM klines GROUP BY symbol,tf")
    ).mappings():
        klines.setdefault(row["symbol"], {})[row["tf"]] = int(row["bars"])
    social = {
        row["symbol"]: dict(row)
        for row in db.execute(
            text(
                "SELECT symbol,st_bull,st_bear,st_msgs,ape_mentions,ape_upvotes,ape_rank,ts "
                "FROM social"
            )
        ).mappings()
    }
    needle = (query or "").strip().upper()
    items = []
    for metadata in config_loader.tradfi_metadata():
        symbol = str(metadata.get("symbol") or "")
        if not symbol or (needle and needle not in symbol):
            continue
        ticker_row = ticker.get(symbol, {})
        ticker_ts = ticker_row.get("ts")
        lag = max(0, now - int(ticker_ts)) if ticker_ts else None
        items.append(
            {
                "symbol": symbol,
                "underlying_type": metadata.get("underlyingType"),
                "underlying_sub_types": metadata.get("underlyingSubType") or [],
                "onboard_date": metadata.get("onboardDate"),
                "price": ticker_row.get("price"),
                "pct_24h": ticker_row.get("pct_24h"),
                "quote_volume": ticker_row.get("quote_volume"),
                "ticker_ts": ticker_ts,
                "ticker_lag_seconds": lag,
                "health": "missing" if lag is None else "stale" if lag > 180 else "ok",
                "scores": scores.get(symbol, {}),
                "kline_bars": klines.get(symbol, {}),
                "social": social.get(symbol),
            }
        )
    return {
        "total": len(items),
        "healthy": sum(1 for item in items if item["health"] == "ok"),
        "items": items,
    }


@router.get("/collectors")
def collectors(
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
) -> list[dict[str, Any]]:
    return _collectors(db)


@router.post("/collectors/{name}/{action}", response_model=MessageOut)
def set_collector_state(
    name: str,
    action: str,
    request: Request,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin_write),
) -> MessageOut:
    if name not in COLLECTOR_NAMES or action not in {"pause", "resume"}:
        raise HTTPException(status_code=404, detail="unknown collector action")
    key = f"collector_pause:{name}"
    setting = db.get(AdminSetting, key)
    if setting is None:
        setting = AdminSetting(
            key=key, value_json={"paused": action == "pause"}, updated_by=admin.id
        )
        db.add(setting)
    else:
        setting.value_json = {"paused": action == "pause"}
        setting.version += 1
        setting.updated_by = admin.id
    _audit(db, request, admin.id, f"admin.collector.{action}", "collector", name)
    db.commit()
    return MessageOut(message=f"collector {name} {action}d")


@router.get("/alert-rules")
def alert_rules(
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
) -> dict[str, Any]:
    rules, version = _setting_payload(db, "alert_rules", _default_alert_rules())
    return {"rules": rules, "version": version}


@router.put("/alert-rules")
def update_alert_rules(
    payload: AdminAlertRulesUpdate,
    request: Request,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin_write),
) -> dict[str, Any]:
    value = payload.model_dump()
    setting = db.get(AdminSetting, "alert_rules")
    if setting is None:
        setting = AdminSetting(key="alert_rules", value_json=value, version=1, updated_by=admin.id)
        db.add(setting)
    else:
        setting.value_json = value
        setting.version += 1
        setting.updated_by = admin.id
        setting.updated_at = utcnow()
    db.flush()
    _audit(
        db,
        request,
        admin.id,
        "admin.alert_rules.update",
        "admin_setting",
        "alert_rules",
        {"version": setting.version, "rules": value},
    )
    db.commit()
    return {"rules": value, "version": setting.version}


@router.get("/news-sources")
def news_sources(
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
) -> list[dict[str, Any]]:
    _sync_news_sources(db)
    db.commit()
    rows = db.scalars(select(NewsSourceSetting).order_by(NewsSourceSetting.name)).all()
    return [
        {
            "name": row.name,
            "url": row.url,
            "feed_type": row.feed_type,
            "lang": row.lang,
            "enabled": row.enabled,
            "slow": row.slow,
            "weight": row.weight,
            "hourly_limit": row.hourly_limit,
            "last_success_at": row.last_success_at,
            "last_error_at": row.last_error_at,
            "last_error": row.last_error,
            "fetched_items": row.fetched_items,
            "inserted_items": row.inserted_items,
        }
        for row in rows
    ]


@router.post("/news-sources", status_code=201)
def create_news_source(
    payload: AdminNewsSourceCreate,
    request: Request,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin_write),
) -> dict[str, Any]:
    if db.get(NewsSourceSetting, payload.name) is not None:
        raise HTTPException(status_code=409, detail="news source name already exists")
    values = payload.model_dump()
    source = NewsSourceSetting(**values, updated_by=admin.id)
    db.add(source)
    _audit(
        db,
        request,
        admin.id,
        "admin.news_source.create",
        "news_source",
        source.name,
        values,
    )
    db.commit()
    return {"name": source.name, **values}


@router.patch("/news-sources/{name}")
def update_news_source(
    name: str,
    payload: AdminNewsSourceUpdate,
    request: Request,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin_write),
) -> dict[str, Any]:
    _sync_news_sources(db)
    source = db.get(NewsSourceSetting, name)
    if source is None:
        raise HTTPException(status_code=404, detail="news source not found")
    changes = payload.model_dump(exclude_none=True)
    for field, value in changes.items():
        setattr(source, field, value)
    source.updated_by = admin.id
    source.updated_at = utcnow()
    _audit(db, request, admin.id, "admin.news_source.update", "news_source", name, changes)
    db.commit()
    return {
        "name": source.name,
        "url": source.url,
        "feed_type": source.feed_type,
        "lang": source.lang,
        "enabled": source.enabled,
        "slow": source.slow,
        "weight": source.weight,
        "hourly_limit": source.hourly_limit,
    }


@router.delete("/news-sources/{name}", response_model=MessageOut)
def delete_news_source(
    name: str,
    request: Request,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin_write),
) -> MessageOut:
    source = db.get(NewsSourceSetting, name)
    if source is None:
        raise HTTPException(status_code=404, detail="news source not found")
    credential_removed = False
    if source.feed_type == "unusual_whales":
        credential = db.get(AdminSetting, market_news.UNUSUAL_WHALES_CREDENTIAL_KEY)
        if credential is not None:
            db.delete(credential)
            credential_removed = True
    metadata = {
        "url": source.url,
        "lang": source.lang,
        "enabled": source.enabled,
        "credential_removed": credential_removed,
    }
    db.delete(source)
    _audit(
        db,
        request,
        admin.id,
        "admin.news_source.delete",
        "news_source",
        name,
        metadata,
    )
    db.commit()
    return MessageOut(message=f"news source {name} deleted")


@router.post("/news-sources/{name}/test")
def test_news_source(
    name: str,
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
) -> dict[str, Any]:
    _sync_news_sources(db)
    source = db.get(NewsSourceSetting, name)
    if source is None:
        raise HTTPException(status_code=404, detail="news source not found")
    try:
        items = market_news.fetch_source(
            {
                "name": source.name,
                "url": source.url,
                "feed_type": source.feed_type,
                "lang": source.lang,
            },
            timeout=10,
            retries=1,
        )[:5]
    except Exception as exc:
        raise HTTPException(
            status_code=502, detail=f"source test failed: {str(exc)[:160]}"
        ) from None
    return {
        "name": source.name,
        "items": [
            {
                "title": item["title"],
                "link": item["link"],
                "published": item["published"],
                "summary": item.get("summary"),
                "sentiment": market_news.sentiment_of(
                    f"{item['title']} {item.get('summary') or ''}"
                ),
            }
            for item in items
        ],
    }


@router.get("/users")
def users(
    limit: int = Query(default=100, ge=1, le=200),
    offset: int = Query(default=0, ge=0, le=100000),
    query: str | None = Query(default=None, max_length=80),
    status: str | None = Query(default=None, pattern="^(active|disabled|admin)$"),
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
) -> dict[str, Any]:
    conditions = ["1=1"]
    params: dict[str, Any] = {"limit": limit, "offset": offset}
    if query:
        conditions.append("(u.username LIKE :query OR u.email LIKE :query)")
        params["query"] = f"%{query.strip()}%"
    if status == "active":
        conditions.append("u.is_active=1")
    elif status == "disabled":
        conditions.append("u.is_active=0")
    elif status == "admin":
        conditions.append("u.is_admin=1")
    where = " AND ".join(conditions)
    total = int(
        db.execute(
            text(f"SELECT COUNT(*) FROM users u WHERE {where}"),
            params,
        ).scalar_one()
    )
    rows = db.execute(
        text(
            f"""
            SELECT u.id,u.username,u.email,u.is_active,u.is_admin,u.last_login_at,u.created_at,
                   u.binance_key_fingerprint,u.binance_key_updated_at,
                   (SELECT COUNT(*) FROM user_sessions s WHERE s.user_id=u.id AND s.revoked_at IS NULL AND s.expires_at>CURRENT_TIMESTAMP) active_sessions,
                   (SELECT COUNT(*) FROM alerts a WHERE a.user_id=u.id) alert_count,
                   (SELECT COUNT(*) FROM alerts a WHERE a.user_id=u.id AND a.`read`=0) unread_alerts
            FROM users u WHERE {where} ORDER BY u.id DESC LIMIT :limit OFFSET :offset
            """
        ),
        params,
    ).mappings()
    return {"total": total, "limit": limit, "offset": offset, "items": [dict(row) for row in rows]}


@router.patch("/users/{user_id}")
def update_user(
    user_id: int,
    payload: AdminUserUpdate,
    request: Request,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin_write),
) -> dict[str, Any]:
    target = db.get(User, user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="user not found")
    changes = payload.model_dump(exclude_none=True)
    if target.id == admin.id and (
        changes.get("is_active") is False or changes.get("is_admin") is False
    ):
        raise HTTPException(
            status_code=409, detail="administrator cannot disable or demote the current account"
        )
    if target.is_admin and changes.get("is_admin") is False:
        admin_count = (
            db.scalar(select(func.count()).select_from(User).where(User.is_admin.is_(True))) or 0
        )
        if admin_count <= 1:
            raise HTTPException(status_code=409, detail="at least one administrator is required")
    for field, value in changes.items():
        setattr(target, field, value)
    if changes.get("is_active") is False:
        db.execute(
            update(UserSession)
            .where(UserSession.user_id == target.id, UserSession.revoked_at.is_(None))
            .values(revoked_at=utcnow())
        )
    _audit(db, request, admin.id, "admin.user.update", "user", str(target.id), changes)
    db.commit()
    return {"id": target.id, "username": target.username, **changes}


@router.post("/users/{user_id}/revoke-sessions", response_model=MessageOut)
def revoke_user_sessions(
    user_id: int,
    request: Request,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin_write),
) -> MessageOut:
    if db.get(User, user_id) is None:
        raise HTTPException(status_code=404, detail="user not found")
    result = db.execute(
        update(UserSession)
        .where(UserSession.user_id == user_id, UserSession.revoked_at.is_(None))
        .values(revoked_at=utcnow())
    )
    _audit(
        db,
        request,
        admin.id,
        "admin.user.sessions.revoke",
        "user",
        str(user_id),
        {"count": result.rowcount},
    )
    db.commit()
    return MessageOut(message=f"revoked {result.rowcount} sessions")


def _cleanup_counts(db: Session, payload: AdminCleanupRequest) -> dict[str, int]:
    now = int(time.time())
    cutoffs = {
        "alerts": now - payload.alerts_days * 86400,
        "news": now - payload.news_days * 86400,
        "scores": (now - payload.scores_days * 86400) * 1000,
    }
    counts = {}
    for table_name, sql in _CLEANUP_COUNT_SQL.items():
        counts[table_name] = int(
            db.execute(
                text(sql),
                {"cutoff": cutoffs[table_name]},
            ).scalar_one()
        )
    return counts


@router.get("/storage")
def storage(
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
) -> list[dict[str, Any]]:
    output = []
    for table_name, sql in _STORAGE_SQL.items():
        row = db.execute(text(sql)).mappings().one()
        output.append({"table": table_name, **dict(row)})
    return output


@router.post("/maintenance/cleanup-preview")
def cleanup_preview(
    payload: AdminCleanupRequest,
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
) -> dict[str, Any]:
    return {
        "delete_counts": _cleanup_counts(db, payload),
        "retention": payload.model_dump(exclude={"confirm"}),
    }


@router.post("/maintenance/cleanup")
def cleanup(
    payload: AdminCleanupRequest,
    request: Request,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin_write),
) -> dict[str, Any]:
    if not payload.confirm:
        raise HTTPException(status_code=409, detail="cleanup confirmation is required")
    expected = _cleanup_counts(db, payload)
    now = int(time.time())
    results = {
        "alerts": db.execute(
            text("DELETE FROM alerts WHERE ts < :cutoff"),
            {"cutoff": now - payload.alerts_days * 86400},
        ).rowcount,
        "news": db.execute(
            text("DELETE FROM news WHERE ts < :cutoff"), {"cutoff": now - payload.news_days * 86400}
        ).rowcount,
        "scores": db.execute(
            text("DELETE FROM scores WHERE open_time < :cutoff"),
            {"cutoff": (now - payload.scores_days * 86400) * 1000},
        ).rowcount,
    }
    _audit(
        db,
        request,
        admin.id,
        "admin.maintenance.cleanup",
        "database",
        "retention",
        {"expected": expected, "deleted": results},
    )
    db.commit()
    return {"deleted": results}


@router.get("/audit")
def audit(
    limit: int = Query(default=100, ge=1, le=200),
    offset: int = Query(default=0, ge=0, le=100000),
    action: str | None = Query(default=None, max_length=80),
    user_id: int | None = Query(default=None, ge=1),
    resource_type: str | None = Query(default=None, max_length=80),
    query: str | None = Query(default=None, max_length=80),
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
) -> dict[str, Any]:
    statement = (
        select(AuditLog, User.username)
        .outerjoin(User, User.id == AuditLog.user_id)
        .order_by(AuditLog.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    count_statement = select(func.count()).select_from(AuditLog).outerjoin(
        User, User.id == AuditLog.user_id
    )
    if action:
        statement = statement.where(AuditLog.action.like(f"%{action}%"))
        count_statement = count_statement.where(AuditLog.action.like(f"%{action}%"))
    if user_id:
        statement = statement.where(AuditLog.user_id == user_id)
        count_statement = count_statement.where(AuditLog.user_id == user_id)
    if resource_type:
        statement = statement.where(AuditLog.resource_type == resource_type)
        count_statement = count_statement.where(AuditLog.resource_type == resource_type)
    if query:
        pattern = f"%{query}%"
        condition = (
            User.username.like(pattern)
            | AuditLog.resource_id.like(pattern)
            | AuditLog.ip_address.like(pattern)
        )
        statement = statement.where(condition)
        count_statement = count_statement.where(condition)
    items = [
        {
            "id": log.id,
            "username": username,
            "action": log.action,
            "resource_type": log.resource_type,
            "resource_id": log.resource_id,
            "ip_address": log.ip_address,
            "metadata": log.metadata_json,
            "created_at": log.created_at,
        }
        for log, username in db.execute(statement)
    ]
    return {
        "total": int(db.scalar(count_statement) or 0),
        "limit": limit,
        "offset": offset,
        "items": items,
    }
