from __future__ import annotations

from pathlib import Path

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from pydantic import SecretStr

from quantdesk_v2.config import Settings
from quantdesk_v2.main import create_app


def _settings() -> Settings:
    return Settings(
        _env_file=None,
        app_env="test",
        database_url="mysql+pymysql://test:test@127.0.0.1/quantdesk_test_unavailable",
        jwt_secret=SecretStr("test-jwt-secret-that-is-long-enough-123456"),
        credential_master_key=SecretStr(Fernet.generate_key().decode("ascii")),
        app_cookie_secure=False,
        app_allowed_hosts="testserver",
        app_allowed_origins="http://testserver",
    )


def test_react_build_is_exposed_only_under_the_canary_mount(
    monkeypatch,
    tmp_path: Path,
) -> None:
    build_dir = tmp_path / "react_static"
    assets_dir = build_dir / "assets"
    assets_dir.mkdir(parents=True)
    (build_dir / "index.html").write_text(
        '<main id="root"></main><script src="/next/assets/app.js"></script>',
        encoding="utf-8",
    )
    (assets_dir / "app.js").write_text("window.quantdeskCanary=true;", encoding="utf-8")
    monkeypatch.setattr(
        Settings,
        "react_static_dir",
        property(lambda _: build_dir),
    )

    with TestClient(create_app(_settings())) as client:
        index = client.get("/next/")
        asset = client.get("/next/assets/app.js")

    assert index.status_code == 200
    assert 'id="root"' in index.text
    assert asset.status_code == 200
    assert asset.text == "window.quantdeskCanary=true;"
    assert index.headers["x-content-type-options"] == "nosniff"
