# ruff: noqa: S608 -- dynamic filters only combine fixed server-side SQL clauses.

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import func, select, text, update
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from quantdesk import config_loader
from quantdesk import news as market_news

from .database import get_db
from .dependencies import require_admin, require_admin_write
from .models import (
    AdminSetting,
    AuditLog,
    CollectorStatus,
    NewsSourceSetting,
    User,
    UserSession,
    utcnow,
)
from .schemas import (
    AdminAlertRulesUpdate,
    AdminCleanupRequest,
    AdminNewsSourceCreate,
    AdminNewsSourceUpdate,
    AdminUserUpdate,
    MessageOut,
)

router = APIRouter(prefix="/api/v2/admin", tags=["admin"])
COLLECTOR_NAMES = {"price", "ticker", "kline", "news", "social", "paper"}
COLLECTOR_STALE_SECONDS = {
    "price": 20,
    "ticker": 150,
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


def _sync_news_sources(db: Session) -> None:
    # Once seeded, the database is authoritative. This prevents an intentionally
    # deleted file-defined source from being recreated on the next admin read.
    if db.scalar(select(func.count()).select_from(NewsSourceSetting)):
        return
    for source in config_loader.settings.get("news_sources", []):
        name = str(source.get("name") or "").strip()
        url = str(source.get("url") or "").strip()
        if not name or not url or db.get(NewsSourceSetting, name) is not None:
            continue
        db.add(
            NewsSourceSetting(
                name=name,
                url=url,
                lang=str(source.get("lang") or "en"),
                slow=bool(source.get("slow")),
                enabled=True,
            )
        )
    db.flush()


def initialize_admin_runtime(engine: Engine) -> None:
    """Seed file-defined news sources before production workers start."""

    with Session(engine) as db:
        _sync_news_sources(db)
        db.commit()


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
                "SELECT direction,COUNT(*) total FROM alerts WHERE ts>=:cutoff GROUP BY direction"
            ),
            {"cutoff": day_cutoff},
        ).mappings()
    }
    news_sentiment = {
        str(row["sentiment"] or "unknown"): int(row["total"])
        for row in db.execute(
            text("SELECT sentiment,COUNT(*) total FROM news WHERE ts>=:cutoff GROUP BY sentiment"),
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
            "SELECT id,ts,source,lang,title,title_zh,link,sentiment,summary FROM news "
            f"WHERE {where} ORDER BY ts DESC LIMIT :limit OFFSET :offset"
        ),
        params,
    ).mappings()
    return {"total": total, "limit": limit, "offset": offset, "items": [dict(row) for row in rows]}


@router.get("/symbols")
def symbols(
    query: str | None = Query(default=None, max_length=32),
    db: Session = Depends(get_db),
    _: User = Depends(require_admin),
) -> dict[str, Any]:
    now = int(time.time())
    ticker = {
        row["symbol"]: dict(row)
        for row in db.execute(
            text("SELECT symbol,price,pct_24h,quote_volume,ts FROM ticker")
        ).mappings()
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
    for metadata in config_loader.symbols_meta.get("symbols", []):
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
    metadata = {"url": source.url, "lang": source.lang, "enabled": source.enabled}
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
        items = market_news.fetch_rss(source.url, timeout=10, retries=1)[:5]
    except Exception as exc:
        raise HTTPException(
            status_code=502, detail=f"source test failed: {str(exc)[:160]}"
        ) from None
    return {
        "name": source.name,
        "items": [
            {
                "title": title,
                "link": link,
                "published": published,
                "sentiment": market_news.sentiment_of(title),
            }
            for title, link, published in items
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
    count_statement = (
        select(func.count()).select_from(AuditLog).outerjoin(User, User.id == AuditLog.user_id)
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
