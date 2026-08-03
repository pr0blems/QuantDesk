from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from . import __version__
from .database import get_db
from .dependencies import get_current_user
from .models import AuditLog, User, UserSession, utcnow
from .schemas import (
    BinanceCredentialStatus,
    BinanceCredentialUpdate,
    HealthOut,
    LoginRequest,
    LogoutRequest,
    MessageOut,
    RefreshRequest,
    RegisterRequest,
    TokenPair,
    UserOut,
)
from .security import (
    CredentialCipher,
    api_key_fingerprint,
    create_access_token,
    hash_password,
    hash_refresh_token,
    new_refresh_token,
    password_needs_rehash,
    verify_password,
)

router = APIRouter(prefix="/api/v2")


def _client_ip(request: Request) -> str | None:
    return request.client.host[:45] if request.client else None


def _user_out(user: User) -> UserOut:
    return UserOut(
        id=user.id,
        username=user.username,
        email=user.email,
        is_active=user.is_active,
        is_admin=user.is_admin,
        binance_credentials_configured=user.binance_credentials_configured,
        binance_key_fingerprint=user.binance_key_fingerprint,
        binance_key_updated_at=user.binance_key_updated_at,
        created_at=user.created_at,
    )


def _audit(
    db: Session,
    request: Request,
    action: str,
    user_id: int | None,
    resource_type: str | None = None,
    resource_id: str | None = None,
) -> None:
    db.add(
        AuditLog(
            user_id=user_id,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            ip_address=_client_ip(request),
        )
    )


def _issue_session(
    *, db: Session, request: Request, user: User, client_type: str
) -> tuple[UserSession, str, str, int]:
    settings = request.app.state.settings
    refresh = new_refresh_token()
    session = UserSession(
        user_id=user.id,
        refresh_token_hash=hash_refresh_token(refresh),
        client_type=client_type,
        user_agent=(request.headers.get("user-agent") or "")[:512] or None,
        ip_address=_client_ip(request),
        expires_at=utcnow() + timedelta(days=settings.refresh_token_days),
    )
    db.add(session)
    db.flush()
    access, expires_in = create_access_token(
        user_id=user.id,
        session_id=session.id,
        jwt_secret=settings.jwt_secret.get_secret_value(),
        expires_minutes=settings.access_token_minutes,
    )
    return session, refresh, access, expires_in


def _set_refresh_cookie(response: Response, request: Request, token: str) -> None:
    settings = request.app.state.settings
    response.set_cookie(
        settings.refresh_cookie_name,
        token,
        max_age=settings.refresh_token_days * 86400,
        httponly=True,
        secure=settings.app_cookie_secure,
        samesite="lax",
        path="/api/v2/auth",
    )


def _refresh_from_request(request: Request, body_token: str | None) -> str | None:
    return body_token or request.cookies.get(request.app.state.settings.refresh_cookie_name)


@router.get("/health", response_model=HealthOut)
def health(request: Request, db: Session = Depends(get_db)) -> HealthOut:
    try:
        db.execute(text("SELECT 1"))
    except SQLAlchemyError:
        raise HTTPException(status_code=503, detail="database unavailable") from None
    settings = request.app.state.settings
    return HealthOut(
        status="ok",
        database="ok",
        version=__version__,
        database_dialect=db.bind.dialect.name if db.bind else "unknown",
        tls_required=settings.db_ssl_required,
    )


@router.post("/auth/register", response_model=UserOut, status_code=status.HTTP_201_CREATED)
def register(payload: RegisterRequest, request: Request, db: Session = Depends(get_db)) -> UserOut:
    if not request.app.state.settings.allow_public_registration:
        raise HTTPException(status_code=403, detail="public registration is disabled")
    username = payload.username.strip().lower()
    if db.scalar(select(User.id).where(User.username == username)):
        raise HTTPException(status_code=409, detail="username already exists")
    if payload.email is not None and db.scalar(select(User.id).where(User.email == payload.email)):
        raise HTTPException(status_code=409, detail="email already exists")
    user = User(
        username=username,
        email=payload.email,
        password_hash=hash_password(payload.password.get_secret_value()),
    )
    db.add(user)
    try:
        db.flush()
        _audit(db, request, "user.register", user.id, "user", str(user.id))
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="username or email already exists") from None
    db.refresh(user)
    return _user_out(user)


@router.post("/auth/login", response_model=TokenPair)
def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
) -> TokenPair:
    username = payload.username.strip().lower()
    user = db.scalar(select(User).where(User.username == username))
    if (
        user is None
        or not user.is_active
        or not verify_password(payload.password.get_secret_value(), user.password_hash)
    ):
        raise HTTPException(status_code=401, detail="invalid username or password")
    if password_needs_rehash(user.password_hash):
        user.password_hash = hash_password(payload.password.get_secret_value())
    user.last_login_at = utcnow()
    _, refresh, access, expires_in = _issue_session(
        db=db, request=request, user=user, client_type=payload.client_type
    )
    _audit(db, request, "auth.login", user.id, "user", str(user.id))
    db.commit()
    if payload.client_type == "web":
        _set_refresh_cookie(response, request, refresh)
    return TokenPair(
        access_token=access,
        refresh_token=refresh if payload.client_type == "native" else None,
        expires_in=expires_in,
    )


@router.post("/auth/refresh", response_model=TokenPair)
def refresh(
    request: Request,
    response: Response,
    payload: RefreshRequest | None = None,
    db: Session = Depends(get_db),
) -> TokenPair:
    supplied = _refresh_from_request(
        request,
        payload.refresh_token.get_secret_value() if payload and payload.refresh_token else None,
    )
    if not supplied:
        raise HTTPException(status_code=401, detail="refresh token is required")
    old_session = db.scalar(
        select(UserSession).where(
            UserSession.refresh_token_hash == hash_refresh_token(supplied),
            UserSession.revoked_at.is_(None),
            UserSession.expires_at > utcnow(),
        )
    )
    if old_session is None:
        raise HTTPException(status_code=401, detail="invalid refresh token")
    user = db.get(User, old_session.user_id)
    if user is None or not user.is_active:
        raise HTTPException(status_code=401, detail="invalid refresh token")
    old_session.revoked_at = utcnow()
    _, new_refresh, access, expires_in = _issue_session(
        db=db, request=request, user=user, client_type=old_session.client_type
    )
    _audit(db, request, "auth.refresh", user.id, "session", old_session.id)
    db.commit()
    if old_session.client_type == "web":
        _set_refresh_cookie(response, request, new_refresh)
    return TokenPair(
        access_token=access,
        refresh_token=new_refresh if old_session.client_type == "native" else None,
        expires_in=expires_in,
    )


@router.post("/auth/logout", response_model=MessageOut)
def logout(
    request: Request,
    response: Response,
    payload: LogoutRequest | None = None,
    db: Session = Depends(get_db),
) -> MessageOut:
    supplied = _refresh_from_request(
        request,
        payload.refresh_token.get_secret_value() if payload and payload.refresh_token else None,
    )
    if supplied:
        session = db.scalar(
            select(UserSession).where(
                UserSession.refresh_token_hash == hash_refresh_token(supplied),
                UserSession.revoked_at.is_(None),
            )
        )
        if session:
            session.revoked_at = utcnow()
            _audit(db, request, "auth.logout", session.user_id, "session", session.id)
            db.commit()
    response.delete_cookie(
        request.app.state.settings.refresh_cookie_name,
        path="/api/v2/auth",
    )
    return MessageOut(message="logged out")


@router.get("/me", response_model=UserOut)
def me(user: User = Depends(get_current_user)) -> UserOut:
    return _user_out(user)


@router.put("/me/binance-credentials", response_model=BinanceCredentialStatus)
def update_binance_credentials(
    payload: BinanceCredentialUpdate,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> BinanceCredentialStatus:
    api_key = payload.api_key.get_secret_value().strip()
    api_secret = payload.api_secret.get_secret_value().strip()
    cipher = CredentialCipher(request.app.state.settings.credential_master_key.get_secret_value())
    user.binance_api_key_encrypted = cipher.encrypt(api_key)
    user.binance_api_secret_encrypted = cipher.encrypt(api_secret)
    user.binance_key_fingerprint = api_key_fingerprint(api_key)
    user.binance_permissions = {"requested": sorted(set(payload.permissions))}
    user.binance_key_updated_at = utcnow()
    user.binance_key_version += 1
    _audit(db, request, "binance.credentials.update", user.id, "user", str(user.id))
    db.commit()
    db.refresh(user)
    return BinanceCredentialStatus(
        configured=True,
        fingerprint=user.binance_key_fingerprint,
        updated_at=user.binance_key_updated_at,
    )


@router.delete("/me/binance-credentials", response_model=MessageOut)
def delete_binance_credentials(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> MessageOut:
    user.binance_api_key_encrypted = None
    user.binance_api_secret_encrypted = None
    user.binance_key_fingerprint = None
    user.binance_permissions = None
    user.binance_key_updated_at = None
    user.binance_key_version += 1
    _audit(db, request, "binance.credentials.delete", user.id, "user", str(user.id))
    db.commit()
    return MessageOut(message="Binance credentials removed")
