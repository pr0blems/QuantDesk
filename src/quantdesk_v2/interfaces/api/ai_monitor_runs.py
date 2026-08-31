"""AI Monitor historical replay and explicit run command routes."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from ... import ai_monitor, historical_replay
from ...database import get_db
from ...dependencies import get_current_user
from ...models import AiMonitorReplayRun, News, User
from ...monitor import MonitorRepository
from ...schemas import AiMonitorNewsAnalyzeRequest, AiMonitorReplayRequest, AiMonitorRunRequest
from .ai_monitor_support import add_ai_monitor_audit, require_expected_user, run_out

router = APIRouter()
_audit = add_ai_monitor_audit
_require_expected_user = require_expected_user
_run_out = run_out

@router.get("/replays")
def list_historical_replays(
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
    limit: int = Query(default=20, ge=1, le=100),
) -> dict[str, Any]:
    runs = db.scalars(
        select(AiMonitorReplayRun)
        .where(AiMonitorReplayRun.user_id == user.id)
        .order_by(AiMonitorReplayRun.created_at.desc(), AiMonitorReplayRun.id.desc())
        .limit(limit)
    ).all()
    return {
        "items": [historical_replay.replay_run_out(item) for item in runs],
        "readiness": historical_replay.replay_readiness_report(db, user.id),
    }


@router.get("/replays/{replay_id}")
def historical_replay_detail(
    replay_id: str,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
) -> dict[str, Any]:
    run = db.scalar(
        select(AiMonitorReplayRun).where(
            AiMonitorReplayRun.public_id == replay_id,
            AiMonitorReplayRun.user_id == user.id,
        )
    )
    if run is None:
        raise HTTPException(status_code=404, detail="历史回放任务不存在")
    return {
        **historical_replay.replay_run_out(run),
        "readiness": historical_replay.replay_readiness_report(
            db, user.id, run_id=run.id
        ),
    }


@router.post("/replays", status_code=202)
def create_historical_replay(
    payload: AiMonitorReplayRequest,
    background_tasks: BackgroundTasks,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
) -> dict[str, Any]:
    _require_expected_user(request, user)
    active = db.scalar(
        select(AiMonitorReplayRun.id).where(
            AiMonitorReplayRun.user_id == user.id,
            AiMonitorReplayRun.status.in_(("pending", "running")),
        )
    )
    if active is not None:
        raise HTTPException(status_code=409, detail="已有历史回放正在执行")
    repository = MonitorRepository(
        request.app.state.database_engine,
        request.app.state.settings.monitor_symbols_config,
    )
    try:
        run = historical_replay.create_replay_run(
            db,
            repository,
            user.id,
            days=payload.days,
            timeframe=payload.timeframe,
            symbols=payload.symbols,
        )
        _audit(
            db,
            request,
            user.id,
            "ai_monitor.replay.create",
            run.public_id,
            {
                "days": payload.days,
                "timeframe": payload.timeframe,
                "symbol_count": run.total_symbols,
            },
        )
        db.commit()
    except historical_replay.HistoricalReplayError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from None
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="已有历史回放正在执行") from None
    except SQLAlchemyError:
        db.rollback()
        raise HTTPException(status_code=503, detail="历史回放任务暂时无法创建") from None
    background_tasks.add_task(
        historical_replay.execute_replay_run,
        request.app.state.database_engine,
        run.public_id,
        request.app.state.settings.monitor_symbols_config,
    )
    return historical_replay.replay_run_out(run)


@router.post("/news/analyze", status_code=202)
def analyze_single_news(
    payload: AiMonitorNewsAnalyzeRequest,
    background_tasks: BackgroundTasks,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
) -> dict[str, Any]:
    """Analyze one explicitly selected news record with the user's default AI model."""

    _require_expected_user(request, user)
    if db.get(News, payload.news_id) is None:
        raise HTTPException(status_code=404, detail="新闻不存在或已被删除")
    try:
        run = ai_monitor.create_single_news_run(db, user.id, payload.news_id)
        _audit(
            db,
            request,
            user.id,
            "ai_monitor.news.analyze",
            payload.news_id,
            {"run_id": run.public_id, "mode": "single"},
        )
        db.commit()
    except ai_monitor.AiMonitorError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from None
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=409, detail="新闻分析任务状态刚刚发生变化，请重试"
        ) from None
    except SQLAlchemyError:
        db.rollback()
        raise HTTPException(status_code=503, detail="AI 监控数据库暂时不可用，请稍后重试") from None
    background_tasks.add_task(
        ai_monitor.execute_news_run,
        request.app.state.database_engine,
        run.public_id,
        request.app.state.settings.credential_master_key.get_secret_value(),
        [payload.news_id],
        request.app.state.settings.monitor_symbols_config,
        True,
    )
    return _run_out(run)


@router.post("/runs", status_code=202)
def create_run(
    payload: AiMonitorRunRequest,
    background_tasks: BackgroundTasks,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
) -> dict[str, Any]:
    _require_expected_user(request, user)
    try:
        run = ai_monitor.create_run(db, user.id, payload.run_type)
        _audit(
            db,
            request,
            user.id,
            "ai_monitor.run.create",
            run.public_id,
            {"run_type": payload.run_type},
        )
        db.commit()
    except ai_monitor.AiMonitorError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from None
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail="任务状态刚刚发生变化，请刷新后重试",
        ) from None
    except SQLAlchemyError:
        db.rollback()
        raise HTTPException(
            status_code=503,
            detail="AI 监控数据库暂时不可用，请稍后重试",
        ) from None
    if payload.run_type == "news":
        background_tasks.add_task(
            ai_monitor.execute_news_run,
            request.app.state.database_engine,
            run.public_id,
            request.app.state.settings.credential_master_key.get_secret_value(),
            None,
            request.app.state.settings.monitor_symbols_config,
            True,
        )
    else:
        background_tasks.add_task(
            ai_monitor.execute_opportunity_run,
            request.app.state.database_engine,
            run.public_id,
            request.app.state.settings.monitor_symbols_config,
        )
    return _run_out(run)

