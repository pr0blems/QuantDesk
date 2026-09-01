from quantdesk_v2.main import _admin_frontend_redirect


def test_legacy_admin_redirect_is_same_origin() -> None:
    response = _admin_frontend_redirect()

    assert response.status_code == 307
    assert response.headers["location"] == "/next/admin/#overview"
    assert response.headers["cache-control"] == "no-store"
    assert "127.0.0.1" not in response.headers["location"]
    assert "localhost" not in response.headers["location"]
