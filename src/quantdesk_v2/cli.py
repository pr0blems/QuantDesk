from __future__ import annotations

import argparse
import os
import secrets
import sys

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


def main() -> int:
    parser = argparse.ArgumentParser(prog="quantdesk-v2")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("generate-secrets")
    sub.add_parser("check-db")
    sub.add_parser("serve")
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
    if args.command == "create-admin":
        return create_admin(args.username, args.email)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
