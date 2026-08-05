from __future__ import annotations

import os
import re
import uuid
from base64 import urlsafe_b64encode
from dataclasses import dataclass
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine
from sqlalchemy.engine import URL, Engine, make_url
from sqlalchemy.pool import NullPool

TEST_DATABASE_PREFIX = "quantdesk_test_"
TEST_ADMIN_URL_ENV = "QUANTDESK_TEST_DATABASE_ADMIN_URL"
TEST_DATABASE_URL_ENV = "QUANTDESK_TEST_DATABASE_URL"
_SAFE_IDENTIFIER = re.compile(r"^[a-z0-9_]+$")
_PRESERVED_TABLES = {
    "alembic_version",
    "strategy_templates",
    "prediction_model_versions",
}
_ROOT = Path(__file__).resolve().parents[1]
_UNAVAILABLE_DATABASE_URL = "mysql+pymysql://test:test@127.0.0.1:3306/quantdesk_test_unavailable"


@dataclass(slots=True)
class _MySQLTestState:
    database_name: str
    database_url: URL
    test_engine: Engine
    admin_engine: Engine | None
    owns_database: bool


_state: _MySQLTestState | None = None
_original_environment: dict[str, str | None] = {}


def _assert_test_database_name(database_name: str) -> None:
    if not database_name.startswith(TEST_DATABASE_PREFIX):
        raise RuntimeError(
            f"refusing destructive test database operation outside {TEST_DATABASE_PREFIX}*"
        )
    if not _SAFE_IDENTIFIER.fullmatch(database_name):
        raise RuntimeError("test database name contains unsafe characters")


def _set_test_environment(database_url: URL | str) -> None:
    values = {
        "DATABASE_URL": (
            database_url.render_as_string(hide_password=False)
            if isinstance(database_url, URL)
            else database_url
        ),
        "DB_SSL_REQUIRED": os.environ.get("QUANTDESK_TEST_DB_SSL_REQUIRED", "false"),
        "DB_SSL_VERIFY_IDENTITY": os.environ.get("QUANTDESK_TEST_DB_SSL_VERIFY_IDENTITY", "false"),
        "DB_SSL_CA": os.environ.get("QUANTDESK_TEST_DB_SSL_CA", ""),
        "APP_ENV": "test",
        "APP_COOKIE_SECURE": "false",
        "JWT_SECRET": "quantdesk-test-jwt-secret-that-is-long-enough",
        "CREDENTIAL_MASTER_KEY": urlsafe_b64encode(b"q" * 32).decode("ascii"),
    }
    for key, value in values.items():
        _original_environment.setdefault(key, os.environ.get(key))
        os.environ[key] = value


def _test_connect_args() -> dict[str, object]:
    if os.environ.get("QUANTDESK_TEST_DB_SSL_REQUIRED", "false").lower() not in {
        "1",
        "true",
        "yes",
        "on",
    }:
        return {}
    verify_identity = os.environ.get("QUANTDESK_TEST_DB_SSL_VERIFY_IDENTITY", "false").lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    ssl_options: dict[str, object] = {"check_hostname": verify_identity}
    ca_path = os.environ.get("QUANTDESK_TEST_DB_SSL_CA", "").strip()
    if ca_path:
        ssl_options["ca"] = ca_path
    return {"ssl": ssl_options}


def _restore_environment() -> None:
    for key, value in _original_environment.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    _original_environment.clear()


def _drop_owned_test_database(state: _MySQLTestState) -> None:
    if not state.owns_database or state.admin_engine is None:
        return
    _assert_test_database_name(state.database_name)
    state.test_engine.dispose()
    try:
        with state.admin_engine.connect() as connection:
            connection.exec_driver_sql(f"DROP DATABASE IF EXISTS `{state.database_name}`")
    finally:
        state.admin_engine.dispose()


def _migrate(database_url: URL) -> None:
    _set_test_environment(database_url)
    alembic_config = Config(str(_ROOT / "alembic.ini"))
    alembic_config.set_main_option("script_location", str(_ROOT / "migrations"))
    command.upgrade(alembic_config, "head")


def pytest_configure(config: pytest.Config) -> None:
    """Provision MySQL only from an explicit, dedicated test database setting."""

    del config
    global _state
    if _state is not None:
        return

    admin_value = os.environ.get(TEST_ADMIN_URL_ENV, "").strip()
    database_value = os.environ.get(TEST_DATABASE_URL_ENV, "").strip()
    if admin_value and database_value:
        raise RuntimeError(f"set only one of {TEST_ADMIN_URL_ENV} or {TEST_DATABASE_URL_ENV}")
    if not admin_value and not database_value:
        # Application modules create their global engine during collection. Point
        # that lazy engine at a non-existent loopback test database so an
        # accidental connection can never reach the production .env target.
        _set_test_environment(_UNAVAILABLE_DATABASE_URL)
        return

    admin_engine: Engine | None = None
    owns_database = False
    if admin_value:
        admin_url = make_url(admin_value)
        if admin_url.get_backend_name() not in {"mysql", "mariadb"}:
            raise RuntimeError(f"{TEST_ADMIN_URL_ENV} must use MySQL/MariaDB")
        database_name = f"{TEST_DATABASE_PREFIX}{os.getpid()}_{uuid.uuid4().hex[:12]}"
        _assert_test_database_name(database_name)
        admin_engine = create_engine(
            admin_url.set(database=None),
            poolclass=NullPool,
            isolation_level="AUTOCOMMIT",
            connect_args=_test_connect_args(),
        )
        with admin_engine.connect() as connection:
            connection.exec_driver_sql(
                f"CREATE DATABASE `{database_name}` "
                "CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
            )
        database_url = admin_url.set(database=database_name)
        owns_database = True
    else:
        database_url = make_url(database_value)
        if database_url.get_backend_name() not in {"mysql", "mariadb"}:
            raise RuntimeError(f"{TEST_DATABASE_URL_ENV} must use MySQL/MariaDB")
        database_name = database_url.database or ""
        _assert_test_database_name(database_name)

    test_engine = create_engine(
        database_url,
        poolclass=NullPool,
        pool_pre_ping=True,
        connect_args=_test_connect_args(),
    )
    state = _MySQLTestState(
        database_name=database_name,
        database_url=database_url,
        test_engine=test_engine,
        admin_engine=admin_engine,
        owns_database=owns_database,
    )
    _state = state
    try:
        _migrate(database_url)
    except BaseException:
        _drop_owned_test_database(state)
        test_engine.dispose()
        _state = None
        _restore_environment()
        raise


def pytest_unconfigure(config: pytest.Config) -> None:
    del config
    global _state
    state = _state
    _state = None
    try:
        if state is not None:
            _drop_owned_test_database(state)
            state.test_engine.dispose()
    finally:
        _restore_environment()


def _require_state() -> _MySQLTestState:
    if _state is None:
        pytest.skip(
            "MySQL integration test requires explicit "
            f"{TEST_ADMIN_URL_ENV} or {TEST_DATABASE_URL_ENV}"
        )
    _assert_test_database_name(_state.database_name)
    return _state


def _truncate_business_tables(engine: Engine) -> None:
    state = _require_state()
    with engine.connect() as connection:
        selected_database = connection.exec_driver_sql("SELECT DATABASE()").scalar_one()
        if selected_database != state.database_name:
            raise RuntimeError("refusing to clean a database other than the isolated test database")
        tables = connection.exec_driver_sql(
            """
            SELECT table_name
            FROM information_schema.tables
            WHERE table_schema=%s AND table_type='BASE TABLE'
            """,
            (state.database_name,),
        ).scalars()
        cleanable_tables = sorted(set(tables) - _PRESERVED_TABLES)
        if any(not _SAFE_IDENTIFIER.fullmatch(table) for table in cleanable_tables):
            raise RuntimeError("test database contains an unsafe table identifier")
        connection.exec_driver_sql("SET FOREIGN_KEY_CHECKS=0")
        try:
            for table in cleanable_tables:
                connection.exec_driver_sql(f"TRUNCATE TABLE `{table}`")
        finally:
            connection.exec_driver_sql("SET FOREIGN_KEY_CHECKS=1")
        connection.commit()


@pytest.fixture
def mysql_test_engine() -> Engine:
    state = _require_state()
    _truncate_business_tables(state.test_engine)
    try:
        yield state.test_engine
    finally:
        _truncate_business_tables(state.test_engine)


@pytest.fixture
def mysql_test_database_url(mysql_test_engine: Engine) -> str:
    del mysql_test_engine
    return _require_state().database_url.render_as_string(hide_password=False)
