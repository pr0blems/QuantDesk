import pytest
from cryptography.fernet import Fernet
from pydantic import SecretStr

from quantdesk_v2.config import Settings
from quantdesk_v2.security import (
    CredentialCipher,
    create_access_token,
    decode_access_token,
    hash_password,
    verify_password,
)


def test_password_hash_and_verify() -> None:
    password = "correct horse battery staple"
    hashed = hash_password(password)
    assert password not in hashed
    assert verify_password(password, hashed)
    assert not verify_password("wrong password", hashed)


def test_credential_cipher_round_trip() -> None:
    cipher = CredentialCipher(Fernet.generate_key().decode("ascii"))
    encrypted = cipher.encrypt("binance-secret-value")
    assert "binance-secret-value" not in encrypted
    assert cipher.decrypt(encrypted) == "binance-secret-value"


def test_access_token_contains_user_and_session() -> None:
    secret = "j" * 48
    token, expires_in = create_access_token(
        user_id=7, session_id="session-1", jwt_secret=secret, expires_minutes=15
    )
    claims = decode_access_token(token, secret)
    assert claims["sub"] == "7"
    assert claims["sid"] == "session-1"
    assert expires_in == 900


def test_production_rejects_database_without_tls() -> None:
    settings = Settings(
        _env_file=None,
        app_env="production",
        database_url="sqlite+pysqlite:///:memory:",
        db_ssl_required=False,
        jwt_secret=SecretStr("j" * 48),
        credential_master_key=SecretStr(Fernet.generate_key().decode("ascii")),
        app_cookie_secure=True,
        app_allowed_origins="https://trade.example.com",
    )
    with pytest.raises(RuntimeError, match="require TLS"):
        settings.validate_runtime()
