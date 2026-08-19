from __future__ import annotations

from collections.abc import Generator
from typing import Any

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from .config import Settings, get_settings


def build_engine(
    settings: Settings,
    poolclass: type | None = None,
    *,
    connect_timeout: int = 8,
    read_timeout: int = 20,
    write_timeout: int = 20,
) -> Engine:
    url = settings.database_url_value
    kwargs: dict[str, Any] = {
        "pool_pre_ping": True,
        "pool_recycle": 300,
        "pool_size": 10,
        "max_overflow": 20,
    }
    connect_args: dict[str, Any] = {
        "connect_timeout": max(1, int(connect_timeout)),
        "read_timeout": max(1, int(read_timeout)),
        "write_timeout": max(1, int(write_timeout)),
        "charset": "utf8mb4",
    }
    if settings.db_ssl_required:
        ssl_options: dict[str, Any] = {
            "check_hostname": settings.db_ssl_verify_identity,
        }
        if settings.db_ssl_ca:
            ssl_options["ca"] = settings.db_ssl_ca
        connect_args["ssl"] = ssl_options

    if poolclass is not None:
        kwargs["poolclass"] = poolclass
        kwargs.pop("pool_size", None)
        kwargs.pop("max_overflow", None)

    kwargs["connect_args"] = connect_args
    return create_engine(url, **kwargs)


engine = build_engine(get_settings())
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
