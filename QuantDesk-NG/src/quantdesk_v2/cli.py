from __future__ import annotations

import argparse
import os
import secrets
import sys
from datetime import UTC, datetime

import uvicorn
from cryptography.fernet import Fernet
from sqlalchemy import select, text

from .config import get_settings
from .database import SessionLocal
from .models import User
from .security import hash_password


def generate_secrets() -> int:
    print(f"JWT_SECRET={secrets.token_urlsafe(48)}")
    print(f"CREDENTIAL_MASTER_KEY={Fernet.generate_key().decode('ascii')}")
    return 0


def check_db() -> int:
    settings = get_settings()
    settings.validate_runtime()
    with SessionLocal() as db:
        version = db.execute(text("SELECT VERSION()"))
        print({"connected": True, "version": version.scalar_one(), "database": settings.db_name})
    return 0


def create_admin(username: str, email: str | None) -> int:
    password = os.environ.get("QUANTDESK_ADMIN_PASSWORD", "")
    if len(password) < 12:
        print("QUANTDESK_ADMIN_PASSWORD must contain at least 12 characters", file=sys.stderr)
        return 2
    with SessionLocal() as db:
        normalized = username.strip().lower()
        if db.scalar(select(User.id).where(User.username == normalized)):
            print("admin username already exists", file=sys.stderr)
            return 3
        db.add(
            User(
                username=normalized,
                email=email.strip().lower() if email else None,
                password_hash=hash_password(password),
                is_admin=True,
            )
        )
        db.commit()
    print("admin created")
    return 0


def serve() -> int:
    settings = get_settings()
    settings.validate_runtime()
    uvicorn.run(
        "quantdesk_v2.main:app",
        host=settings.app_host,
        port=settings.app_port,
        proxy_headers=True,
        forwarded_allow_ips="127.0.0.1",
    )
    return 0


def worker(role: str) -> int:
    from .runtime import run_worker

    return run_worker(role)


def worker_status() -> int:
    with SessionLocal() as db:
        rows = db.execute(
            text(
                """
                SELECT worker_key,owner_id,metadata_json,heartbeat_at,expires_at,
                       CASE WHEN expires_at > UTC_TIMESTAMP(6) THEN 'active' ELSE 'expired' END status
                  FROM worker_leases ORDER BY worker_key
                """
            )
        ).mappings()
        for row in rows:
            print(dict(row))
    return 0


def worker_health(role: str) -> int:
    """Exit non-zero unless the requested worker has a fresh, active lease."""

    settings = get_settings()
    settings.validate_runtime()
    worker_key = f"quantdesk-ng:{role}"
    maximum_lag = max(settings.worker_heartbeat_seconds * 2, 15)
    with SessionLocal() as db:
        row = db.execute(
            text(
                "SELECT heartbeat_at, expires_at FROM worker_leases WHERE worker_key=:worker_key"
            ),
            {"worker_key": worker_key},
        ).mappings().one_or_none()
    if row is None:
        print(f"worker is not registered: {role}", file=sys.stderr)
        return 2
    now = datetime.now(UTC).replace(tzinfo=None)
    heartbeat = row["heartbeat_at"]
    expires_at = row["expires_at"]
    if heartbeat.tzinfo is not None:
        heartbeat = heartbeat.astimezone(UTC).replace(tzinfo=None)
    if expires_at.tzinfo is not None:
        expires_at = expires_at.astimezone(UTC).replace(tzinfo=None)
    heartbeat_age = max(0, int((now - heartbeat).total_seconds()))
    if expires_at <= now or heartbeat_age > maximum_lag:
        print(f"worker lease is stale: {role} (heartbeat age {heartbeat_age}s)", file=sys.stderr)
        return 2
    print(f"worker is healthy: {role} (heartbeat age {heartbeat_age}s)")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="quantdesk-ng")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("generate-secrets")
    sub.add_parser("check-db")
    sub.add_parser("serve")
    worker_parser = sub.add_parser("worker")
    worker_parser.add_argument(
        "--role", choices=("market", "news", "paper", "intelligence"), required=True
    )
    sub.add_parser("worker-status")
    worker_health_parser = sub.add_parser("worker-health")
    worker_health_parser.add_argument(
        "--role", choices=("market", "news", "paper", "intelligence"), required=True
    )
    admin = sub.add_parser("create-admin")
    admin.add_argument("--username", required=True)
    admin.add_argument("--email")
    args = parser.parse_args()

    if args.command == "generate-secrets":
        return generate_secrets()
    if args.command == "check-db":
        return check_db()
    if args.command == "serve":
        return serve()
    if args.command == "worker":
        return worker(args.role)
    if args.command == "worker-status":
        return worker_status()
    if args.command == "worker-health":
        return worker_health(args.role)
    if args.command == "create-admin":
        return create_admin(args.username, args.email)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
