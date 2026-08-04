from __future__ import annotations

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.orm import Session

from .database import get_db
from .models import User, UserSession, utcnow
from .security import SecurityError, decode_access_token

bearer = HTTPBearer(auto_error=False)


def get_current_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    db: Session = Depends(get_db),
) -> User:
    unauthorized = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="invalid or expired credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise unauthorized
    settings = request.app.state.settings
    try:
        claims = decode_access_token(
            credentials.credentials, settings.jwt_secret.get_secret_value()
        )
        user_id = int(claims["sub"])
        session_id = str(claims["sid"])
    except (SecurityError, TypeError, ValueError):
        raise unauthorized from None

    session = db.scalar(
        select(UserSession).where(
            UserSession.id == session_id,
            UserSession.user_id == user_id,
            UserSession.revoked_at.is_(None),
            UserSession.expires_at > utcnow(),
        )
    )
    if session is None:
        raise unauthorized
    user = db.get(User, user_id)
    if user is None or not user.is_active:
        raise unauthorized
    return user


def require_admin(user: User = Depends(get_current_user)) -> User:
    """Require an active platform administrator for read-only admin APIs."""

    if not user.is_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="administrator access required")
    return user


def require_admin_write(
    request: Request,
    user: User = Depends(require_admin),
) -> User:
    """Protect admin mutations against silent cross-account browser changes."""

    expected = request.headers.get("X-QuantDesk-User-ID", "").strip()
    if not expected:
        raise HTTPException(status_code=428, detail="expected user identity is required")
    try:
        expected_user_id = int(expected)
    except ValueError:
        raise HTTPException(status_code=400, detail="expected user identity is invalid") from None
    if expected_user_id != user.id:
        raise HTTPException(status_code=409, detail="authenticated administrator changed")
    return user
