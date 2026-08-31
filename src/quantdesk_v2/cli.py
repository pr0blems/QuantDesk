from __future__ import annotations

import argparse
import json
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


def audit_paper(account_id: int | None, *, json_output: bool = False) -> int:
    """Run a read-only audit of durable paper fill projections."""

    from . import market_store
    from .infrastructure.persistence.paper_projections import (
        MySqlPaperProjectionStore,
    )

    settings = get_settings()
    settings.validate_runtime()
    if account_id is None:
        accounts = market_store.query(
            "SELECT id,user_id,name FROM paper_accounts ORDER BY id"
        )
    else:
        accounts = market_store.query(
            "SELECT id,user_id,name FROM paper_accounts WHERE id=? ORDER BY id",
            (account_id,),
        )
    store = MySqlPaperProjectionStore(market_store)
    results: list[dict[str, object]] = []
    blocked = False
    for account in accounts:
        pending, drift_codes, warning_codes = store.audit_account(
            user_id=int(account["user_id"]),
            paper_account_id=int(account["id"]),
        )
        ready = pending == 0 and not drift_codes
        blocked = blocked or not ready
        results.append(
            {
                "paper_account_id": int(account["id"]),
                "user_id": int(account["user_id"]),
                "name": str(account["name"]),
                "ready": ready,
                "pending_count": pending,
                "drift_codes": list(drift_codes),
                "warning_codes": list(warning_codes),
            }
        )
    payload = {"account_count": len(results), "blocked": blocked, "accounts": results}
    if json_output:
        print(json.dumps(payload, ensure_ascii=False, default=str))
    else:
        print(f"paper accounts: {len(results)}; blocked: {int(blocked)}")
        for result in results:
            state = "READY" if result["ready"] else "BLOCKED"
            print(
                f"[{state}] account={result['paper_account_id']} "
                f"pending={result['pending_count']} "
                f"drift={','.join(result['drift_codes']) or '-'} "
                f"warning={','.join(result['warning_codes']) or '-'}"
            )
    if account_id is not None and not results:
        print(f"paper account {account_id} was not found", file=sys.stderr)
        return 3
    return 4 if blocked else 0


def reconcile_paper(account_id: int, *, confirmed: bool = False) -> int:
    """Replay only pending/failed fill projections for one explicit account."""

    if not confirmed:
        print("reconcile-paper requires --confirm", file=sys.stderr)
        return 2
    from . import market_store
    from .application.paper_reconciliation import PaperExecutionReconciliationService
    from .infrastructure.persistence.paper_projections import (
        MySqlPaperProjectionStore,
    )

    settings = get_settings()
    settings.validate_runtime()
    accounts = market_store.query(
        "SELECT id,user_id,name FROM paper_accounts WHERE id=? ORDER BY id",
        (account_id,),
    )
    if not accounts:
        print(f"paper account {account_id} was not found", file=sys.stderr)
        return 3
    account = accounts[0]
    result = PaperExecutionReconciliationService(
        MySqlPaperProjectionStore(market_store)
    ).reconcile_account(
        user_id=int(account["user_id"]),
        paper_account_id=int(account["id"]),
    )
    print(
        json.dumps(
            {
                "paper_account_id": account_id,
                "ready": result.ready,
                "discovered": result.discovered,
                "applied": result.applied,
                "already_applied": result.already_applied,
                "failed": result.failed,
                "remaining": result.remaining,
                "drift_codes": list(result.drift_codes),
                "warning_codes": list(result.warning_codes),
                "errors": list(result.errors),
            },
            ensure_ascii=False,
        )
    )
    return 0 if result.ready else 4


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
    worker = sub.add_parser("worker")
    worker.add_argument(
        "kind",
        choices=("market", "shadow", "paper", "live", "ai", "ops"),
    )
    admin = sub.add_parser("create-admin")
    admin.add_argument("--username", required=True)
    admin.add_argument("--email")
    paper_audit = sub.add_parser("audit-paper")
    paper_audit.add_argument("--account-id", type=int)
    paper_audit.add_argument("--json", action="store_true", dest="json_output")
    paper_reconcile = sub.add_parser("reconcile-paper")
    paper_reconcile.add_argument("--account-id", type=int, required=True)
    paper_reconcile.add_argument("--confirm", action="store_true")
    args = parser.parse_args()

    if args.command == "generate-secrets":
        return generate_secrets()
    if args.command == "check-db":
        return check_db()
    if args.command == "serve":
        return serve()
    if args.command == "worker":
        from .worker_runtime import run_worker

        return run_worker(args.kind)
    if args.command == "create-admin":
        return create_admin(args.username, args.email)
    if args.command == "audit-paper":
        return audit_paper(args.account_id, json_output=args.json_output)
    if args.command == "reconcile-paper":
        return reconcile_paper(args.account_id, confirmed=args.confirm)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
