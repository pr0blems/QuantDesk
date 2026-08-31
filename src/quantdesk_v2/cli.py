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
            """SELECT id,user_id,name FROM paper_accounts
               WHERE status<>'archived' ORDER BY id"""
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


def audit_paper_history(
    account_id: int, *, rebuild: bool = False, confirmed: bool = False
) -> int:
    """Dry-run or rebuild derivable post-checkpoint paper balance ledgers."""

    if rebuild and not confirmed:
        print("rebuild-paper-history requires --confirm", file=sys.stderr)
        return 2
    from . import market_store
    from .infrastructure.persistence.paper_projections import (
        MySqlPaperProjectionStore,
        PaperProjectionError,
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
    store = MySqlPaperProjectionStore(market_store)
    try:
        if rebuild:
            report, rebuilt = store.rebuild_missing_history_ledger(
                user_id=int(account["user_id"]), paper_account_id=int(account["id"])
            )
        else:
            report = store.audit_applied_history(
                user_id=int(account["user_id"]), paper_account_id=int(account["id"])
            )
            rebuilt = 0
    except PaperProjectionError as exc:
        print(json.dumps({"paper_account_id": account_id, "error": str(exc)}))
        return 4
    print(
        json.dumps(
            {
                **report.snapshot(),
                "mode": "rebuild" if rebuild else "dry_run",
                "rebuilt_ledger_count": rebuilt,
            },
            ensure_ascii=False,
            default=str,
        )
    )
    return 0 if report.rebuild_safe else 4


def audit_live(account_id: int | None) -> int:
    """Read-only local live canary audit; never contacts Binance or writes state."""

    from . import market_store
    from .application.live_recovery import (
        LivePositionSyncService,
        ProtectionRecoveryService,
    )

    settings = get_settings()
    settings.validate_runtime()
    if account_id is None:
        accounts = market_store.query(
            """SELECT id,user_id,name,status,last_error_code
               FROM live_trading_accounts
               WHERE status<>'archived' ORDER BY id"""
        )
    else:
        accounts = market_store.query(
            """SELECT id,user_id,name,status,last_error_code
               FROM live_trading_accounts WHERE id=? ORDER BY id""",
            (account_id,),
        )
    results: list[dict[str, object]] = []
    blocked = False
    for account in accounts:
        user_id = int(account["user_id"])
        live_account_id = int(account["id"])
        market_rows = market_store.query(
            """SELECT id,symbol,position_side,action,status
               FROM live_order_intents
               WHERE user_id=? AND live_account_id=?
                 AND action IN ('open','close') AND status='filled'
               ORDER BY id DESC""",
            (user_id, live_account_id),
        )
        managed = LivePositionSyncService.managed_positions(market_rows)
        protection_rows = market_store.query(
            """SELECT id,symbol,position_side,action
               FROM live_order_intents
               WHERE user_id=? AND live_account_id=?
                 AND action IN ('stop','take_profit') AND status='submitted'
               ORDER BY id""",
            (user_id, live_account_id),
        )
        coverage = ProtectionRecoveryService.coverage_counts(
            protection_rows, managed
        )
        intent_counts = market_store.query(
            """SELECT
                   COALESCE(SUM(status='unknown'),0) AS unknown_count,
                   COALESCE(SUM(
                       status IN ('created','submitted','unknown')
                       AND updated_at<UTC_TIMESTAMP()-INTERVAL 5 MINUTE
                   ),0) AS stale_count
               FROM live_order_intents
               WHERE user_id=? AND live_account_id=?""",
            (user_id, live_account_id),
        )[0]
        journal_counts = market_store.query(
            """SELECT
                   COALESCE(SUM(claim_status='in_progress'),0) AS in_progress_count,
                   COALESCE(SUM(execution_state='unknown'),0) AS unknown_count
               FROM execution_idempotency_records
               WHERE user_scope=? AND account_scope=?""",
            (f"user:{user_id}", f"live-account:{live_account_id}"),
        )[0]
        unprotected = [
            f"{symbol}:{position_side}"
            for symbol, position_side in managed
            if int(coverage.get((symbol, position_side), 0)) != 2
        ]
        local_ready = (
            int(intent_counts["unknown_count"] or 0) == 0
            and int(intent_counts["stale_count"] or 0) == 0
            and int(journal_counts["in_progress_count"] or 0) == 0
            and int(journal_counts["unknown_count"] or 0) == 0
            and not unprotected
        )
        blocked = blocked or not local_ready
        results.append(
            {
                "live_account_id": live_account_id,
                "name": str(account["name"]),
                "status": str(account["status"]),
                "last_error_code": account.get("last_error_code"),
                "local_ready": local_ready,
                "managed_position_count": len(managed),
                "unprotected_managed_positions": unprotected,
                "unknown_intent_count": int(intent_counts["unknown_count"] or 0),
                "stale_intent_count": int(intent_counts["stale_count"] or 0),
                "in_progress_execution_count": int(
                    journal_counts["in_progress_count"] or 0
                ),
                "unknown_execution_count": int(journal_counts["unknown_count"] or 0),
            }
        )
    print(
        json.dumps(
            {
                "scope": "local_read_only",
                "account_count": len(results),
                "blocked": blocked,
                "accounts": results,
            },
            ensure_ascii=False,
            default=str,
        )
    )
    if account_id is not None and not results:
        return 3
    return 4 if blocked else 0


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
    paper_history = sub.add_parser("audit-paper-history")
    paper_history.add_argument("--account-id", type=int, required=True)
    paper_rebuild = sub.add_parser("rebuild-paper-history")
    paper_rebuild.add_argument("--account-id", type=int, required=True)
    paper_rebuild.add_argument("--confirm", action="store_true")
    live_audit = sub.add_parser("audit-live")
    live_audit.add_argument("--account-id", type=int)
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
    if args.command == "audit-paper-history":
        return audit_paper_history(args.account_id)
    if args.command == "rebuild-paper-history":
        return audit_paper_history(
            args.account_id, rebuild=True, confirmed=args.confirm
        )
    if args.command == "audit-live":
        return audit_live(args.account_id)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
