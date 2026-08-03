from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from cryptography.fernet import Fernet, InvalidToken

PASSWORD_HASHER = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=4)
JWT_ALGORITHM = "HS256"


class SecurityError(ValueError):
    pass


class CredentialCipher:
    def __init__(self, master_key: str):
        try:
            self._fernet = Fernet(master_key.encode("ascii"))
        except Exception as exc:
            raise SecurityError("invalid credential master key") from exc

    def encrypt(self, value: str) -> str:
        if not value:
            raise SecurityError("credential value cannot be empty")
        return self._fernet.encrypt(value.encode("utf-8")).decode("ascii")

    def decrypt(self, ciphertext: str) -> str:
        try:
            return self._fernet.decrypt(ciphertext.encode("ascii")).decode("utf-8")
        except (InvalidToken, UnicodeError, TypeError, ValueError) as exc:
            raise SecurityError("credential decryption failed") from exc


def hash_password(password: str) -> str:
    return PASSWORD_HASHER.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return PASSWORD_HASHER.verify(password_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def password_needs_rehash(password_hash: str) -> bool:
    try:
        return PASSWORD_HASHER.check_needs_rehash(password_hash)
    except InvalidHashError:
        return True


def create_access_token(
    *, user_id: int, session_id: str, jwt_secret: str, expires_minutes: int
) -> tuple[str, int]:
    now = datetime.now(UTC)
    expires = now + timedelta(minutes=expires_minutes)
    payload = {
        "sub": str(user_id),
        "sid": session_id,
        "type": "access",
        "jti": str(uuid.uuid4()),
        "iat": int(now.timestamp()),
        "exp": int(expires.timestamp()),
    }
    return jwt.encode(payload, jwt_secret, algorithm=JWT_ALGORITHM), int(
        (expires - now).total_seconds()
    )


def decode_access_token(token: str, jwt_secret: str) -> dict[str, Any]:
    try:
        payload = jwt.decode(token, jwt_secret, algorithms=[JWT_ALGORITHM])
    except jwt.PyJWTError as exc:
        raise SecurityError("invalid access token") from exc
    if payload.get("type") != "access" or not payload.get("sub") or not payload.get("sid"):
        raise SecurityError("invalid access token claims")
    return payload


def new_refresh_token() -> str:
    return secrets.token_urlsafe(48)


def hash_refresh_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def api_key_fingerprint(api_key: str) -> str:
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:16].upper()
