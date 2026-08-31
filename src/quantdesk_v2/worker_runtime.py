"""Independent process entry points for QuantDesk background runtimes."""

from __future__ import annotations

import os
import signal
import socket
import sys
import threading
from collections.abc import Callable
from datetime import datetime
from typing import Literal

from sqlalchemy import select, text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.orm import Session

from . import __version__
from .admin import initialize_admin_runtime
from .config import Settings, get_settings
from .main import (
    _finnhub_runtime_config,
    _unusual_whales_runtime_config,
    create_app,
)
from .models import WorkerHeartbeat, utcnow

WorkerType = Literal["market", "shadow", "paper", "live", "ai", "ops"]
_HEARTBEAT_SECONDS = 5.0


def _instance_key(worker_type: WorkerType) -> tuple[str, str]:
    host = socket.gethostname().strip()[:128] or "unknown-host"
    configured = os.environ.get("QUANTDESK_WORKER_INSTANCE", "").strip()
    instance = configured[:96] if configured else host
    return host, f"{instance}:{worker_type}"[:128]


def _write_heartbeat(
    engine: Engine,
    worker_type: WorkerType,
    *,
    status: str,
    started_at: datetime,
    details: dict[str, object] | None = None,
) -> None:
    host, instance = _instance_key(worker_type)
    now = utcnow()
    with Session(engine) as db:
        row = db.scalar(
            select(WorkerHeartbeat).where(
                WorkerHeartbeat.worker_type == worker_type,
                WorkerHeartbeat.instance_key == instance,
            )
        )
        if row is None:
            row = WorkerHeartbeat(
                worker_type=worker_type,
                instance_key=instance,
                status=status,
                pid=os.getpid(),
                host=host,
                release_version=__version__,
                details_json=details,
                started_at=started_at,
                last_seen_at=now,
                stopped_at=now if status in {"stopped", "error"} else None,
                updated_at=now,
            )
            db.add(row)
        else:
            row.status = status
            row.pid = os.getpid()
            row.host = host
            row.release_version = __version__
            row.details_json = details
            row.started_at = started_at
            row.last_seen_at = now
            row.stopped_at = now if status in {"stopped", "error"} else None
            row.updated_at = now
        db.commit()


def _heartbeat_loop(
    engine: Engine,
    worker_type: WorkerType,
    started_at: datetime,
    stop_event: threading.Event,
) -> None:
    while not stop_event.wait(_HEARTBEAT_SECONDS):
        try:
            _write_heartbeat(
                engine,
                worker_type,
                status="running",
                started_at=started_at,
            )
        except Exception as exc:
            print(
                f"[{worker_type}-worker] heartbeat failed: {type(exc).__name__}",
                file=sys.stderr,
            )


def _acquire_singleton(engine: Engine, worker_type: WorkerType) -> Connection:
    connection = engine.connect()
    acquired = connection.execute(
        text("SELECT GET_LOCK(:lock_name, 0)"),
        {"lock_name": f"quantdesk-worker-{worker_type}"},
    ).scalar_one()
    if int(acquired or 0) != 1:
        connection.close()
        raise RuntimeError(f"another {worker_type} worker already owns the lease")
    return connection


def _release_singleton(connection: Connection, worker_type: WorkerType) -> None:
    try:
        try:
            connection.execute(
                text("SELECT RELEASE_LOCK(:lock_name)"),
                {"lock_name": f"quantdesk-worker-{worker_type}"},
            )
        except Exception as exc:
            # MySQL releases named locks when their connection disappears. A
            # long-running worker may therefore find its lease connection was
            # already recycled during an otherwise normal systemd shutdown.
            # Cleanup must remain best-effort and must not turn a clean stop
            # into a failed service result.
            print(
                f"[{worker_type}-worker] lease release skipped: "
                f"{type(exc).__name__}",
                file=sys.stderr,
            )
    finally:
        try:
            connection.close()
        except Exception as exc:
            print(
                f"[{worker_type}-worker] lease connection close skipped: "
                f"{type(exc).__name__}",
                file=sys.stderr,
            )


def _start_market(app, settings: Settings) -> Callable[[], None]:
    from . import battle, market_engine, market_store, underlying_quotes

    engine = app.state.database_engine
    market_store.configure_engine(engine)
    initialize_admin_runtime(engine)
    uw_config = _unusual_whales_runtime_config(engine)
    uw_enabled = bool(uw_config.get("enabled", True))
    app.state.unusual_whales_runtime.apply_config(
        uw_config["channels"],
        websocket_enabled=uw_enabled and uw_config["websocket_enabled"],
        rest_enabled=uw_enabled and uw_config["rest_enabled"],
        thresholds=uw_config["thresholds"],
        retention=uw_config["retention"],
    )
    app.state.unusual_whales_runtime.start()
    app.state.finnhub_us_quote_service.set_enabled(
        bool(_finnhub_runtime_config(engine).get("enabled", True))
    )
    app.state.finnhub_us_quote_service.start()
    market_engine.start(include_paper=False)
    battle.start()
    underlying_quotes.start()

    def stop() -> None:
        app.state.unusual_whales_runtime.stop()
        app.state.finnhub_us_quote_service.stop()
        underlying_quotes.stop()

    return stop


def _start_paper(app, settings: Settings) -> Callable[[], None]:
    del settings
    from . import market_store, paper_engine

    market_store.configure_engine(app.state.database_engine)
    threading.Thread(
        target=paper_engine.paper_loop,
        daemon=True,
        name="paper-runtime",
    ).start()
    return lambda: None


def _start_shadow(app, settings: Settings) -> Callable[[], None]:
    del settings
    from . import market_store
    from .shadow_worker import shadow_loop

    market_store.configure_engine(app.state.database_engine)
    shadow_stop = threading.Event()
    threading.Thread(
        target=shadow_loop,
        args=(app.state.database_engine, shadow_stop),
        daemon=True,
        name="shadow-runtime",
    ).start()
    return shadow_stop.set


def _start_live(app, settings: Settings) -> Callable[[], None]:
    from . import live_engine, market_store

    market_store.configure_engine(app.state.database_engine)
    live_engine.configure(
        settings,
        app.state.binance_service,
        app.state.binance_trading_client,
    )
    live_engine.start()
    return lambda: None


def _start_ai(app, settings: Settings) -> Callable[[], None]:
    from . import ai_monitor, market_store

    market_store.configure_engine(app.state.database_engine)
    ai_monitor.start(
        app.state.database_engine,
        settings.credential_master_key.get_secret_value(),
        settings.monitor_symbols_config,
    )
    return lambda: None


def _start_ops(app, settings: Settings) -> Callable[[], None]:
    from .ops_monitor import ops_loop

    ops_stop = threading.Event()
    threading.Thread(
        target=ops_loop,
        args=(app.state.database_engine, ops_stop),
        kwargs={"live_enabled": settings.binance_live_trading_enabled},
        daemon=True,
        name="ops-runtime",
    ).start()
    return ops_stop.set


_STARTERS: dict[WorkerType, Callable[[object, Settings], Callable[[], None]]] = {
    "market": _start_market,
    "shadow": _start_shadow,
    "paper": _start_paper,
    "live": _start_live,
    "ai": _start_ai,
    "ops": _start_ops,
}


def run_worker(worker_type: WorkerType) -> int:
    settings = get_settings()
    settings.validate_runtime()
    app = create_app(settings)
    engine = app.state.database_engine
    stop_event = threading.Event()
    started_at = utcnow()
    lock_connection: Connection | None = None
    failed = False

    def stop_runtime() -> None:
        return None

    def request_stop(_signum: int, _frame: object) -> None:
        stop_event.set()

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)
    try:
        lock_connection = _acquire_singleton(engine, worker_type)
        _write_heartbeat(
            engine,
            worker_type,
            status="starting",
            started_at=started_at,
        )
        stop_runtime = _STARTERS[worker_type](app, settings)
        _write_heartbeat(
            engine,
            worker_type,
            status="running",
            started_at=started_at,
        )
        heartbeat = threading.Thread(
            target=_heartbeat_loop,
            args=(engine, worker_type, started_at, stop_event),
            daemon=True,
            name=f"{worker_type}-heartbeat",
        )
        heartbeat.start()
        print(f"[{worker_type}-worker] started release={__version__}")
        stop_event.wait()
        return 0
    except Exception as exc:
        failed = True
        try:
            _write_heartbeat(
                engine,
                worker_type,
                status="error",
                started_at=started_at,
                details={"error_type": type(exc).__name__},
            )
        except Exception as heartbeat_exc:
            print(
                f"[{worker_type}-worker] failed to persist error state: "
                f"{type(heartbeat_exc).__name__}",
                file=sys.stderr,
            )
        print(f"[{worker_type}-worker] failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    finally:
        stop_event.set()
        try:
            stop_runtime()
        finally:
            if lock_connection is not None:
                _release_singleton(lock_connection, worker_type)
        if not failed:
            try:
                _write_heartbeat(
                    engine,
                    worker_type,
                    status="stopped",
                    started_at=started_at,
                )
            except Exception as heartbeat_exc:
                print(
                    f"[{worker_type}-worker] failed to persist stopped state: "
                    f"{type(heartbeat_exc).__name__}",
                    file=sys.stderr,
                )
        engine.dispose()
