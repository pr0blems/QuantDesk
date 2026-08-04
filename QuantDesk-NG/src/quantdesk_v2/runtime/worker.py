from __future__ import annotations

import signal
import threading
from collections.abc import Callable

from quantdesk import engine as market_engine
from quantdesk import exchange_sync, news, outcomes, paper, realtime, social, store

from ..admin import initialize_admin_runtime
from ..config import get_settings
from ..database import engine
from ..execution.shadow import shadow_loop
from .leases import LeaseOwner, WorkerLease

WorkerTarget = Callable[[threading.Event], None]

WORKER_ROLES: dict[str, tuple[tuple[str, WorkerTarget], ...]] = {
    "market": (
        ("binance-environment", exchange_sync.public_sync_loop),
        ("market-stream", realtime.market_stream_loop),
        ("price", market_engine.price_loop),
        ("ticker", market_engine.ticker_loop),
        ("kline", market_engine.kline_loop),
    ),
    "news": (("news", news.news_loop), ("social", social.social_loop)),
    "paper": (("paper", paper.paper_loop),),
    "intelligence": (("outcome-labeler", outcomes.outcome_loop), ("shadow", shadow_loop)),
}


def _install_signal_handlers(stop_event: threading.Event) -> None:
    def stop(_: int, __: object) -> None:
        stop_event.set()

    for signal_name in ("SIGINT", "SIGTERM"):
        value = getattr(signal, signal_name, None)
        if value is not None:
            signal.signal(value, stop)


def run_worker(role: str) -> int:
    if role not in WORKER_ROLES:
        raise ValueError(f"unknown worker role: {role}")
    settings = get_settings()
    settings.validate_runtime()
    store.configure_engine(engine)
    initialize_admin_runtime(engine)

    owner = LeaseOwner.create(f"quantdesk-ng:{role}")
    lease = WorkerLease(engine, owner, settings.worker_lease_ttl_seconds)
    if not lease.acquire():
        print(f"[worker] active lease already exists for role={role}")
        return 3

    stop_event = threading.Event()
    lease_lost = threading.Event()
    _install_signal_handlers(stop_event)

    def heartbeat() -> None:
        while not stop_event.wait(settings.worker_heartbeat_seconds):
            try:
                if not lease.heartbeat():
                    print(f"[worker] lease lost for role={role}; stopping safely")
                    lease_lost.set()
                    stop_event.set()
            except Exception as exc:
                print(f"[worker] lease heartbeat failed: {type(exc).__name__}: {exc}")
                lease_lost.set()
                stop_event.set()

    threads = [
        threading.Thread(target=target, args=(stop_event,), name=name)
        for name, target in WORKER_ROLES[role]
    ]
    heartbeat_thread = threading.Thread(target=heartbeat, name="lease-heartbeat")
    print(f"[worker] starting role={role} owner={owner.owner_id}")
    heartbeat_thread.start()
    for thread in threads:
        thread.start()
    try:
        while not stop_event.wait(1):
            if any(not thread.is_alive() for thread in threads):
                print(f"[worker] child loop exited unexpectedly for role={role}")
                stop_event.set()
    finally:
        stop_event.set()
        for thread in threads:
            thread.join(timeout=15)
        heartbeat_thread.join(timeout=5)
        try:
            lease.release()
        except Exception as exc:
            print(f"[worker] lease release failed: {type(exc).__name__}: {exc}")
    return 4 if lease_lost.is_set() else 0
