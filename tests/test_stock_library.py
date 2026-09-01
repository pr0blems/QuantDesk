from sqlalchemy import BigInteger, create_engine, select
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session

from quantdesk_v2.models import Base, Security, SecuritySymbolMapping, utcnow
from quantdesk_v2.stock_library import normalize_contract_symbol
from quantdesk_v2.tradfi_universe import (
    parse_tradfi_contracts,
    sync_tradfi_contracts,
)


@compiles(BigInteger, "sqlite")
def _compile_big_integer_as_sqlite_integer(
    _type: BigInteger, _compiler: object, **_: object
) -> str:
    return "INTEGER"


def _stock_library_session() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    names = (
        "users",
        "admin_settings",
        "securities",
        "security_symbol_mappings",
        "company_profiles",
    )
    Base.metadata.create_all(
        engine,
        tables=[Base.metadata.tables[name] for name in names],
    )
    return Session(engine, expire_on_commit=False)


def _contract(
    symbol: str,
    underlying_type: str,
    *,
    contract_type: str = "TRADIFI_PERPETUAL",
) -> dict[str, object]:
    return {
        "symbol": symbol,
        "pair": symbol,
        "status": "TRADING",
        "contractType": contract_type,
        "underlyingType": underlying_type,
        "onboardDate": 1_700_000_000_000,
    }


def test_normalize_binance_tradfi_stock_symbols() -> None:
    assert normalize_contract_symbol("AAPLUSDT") == "AAPL"
    assert normalize_contract_symbol("BRKBUSDT") == "BRK.B"
    assert normalize_contract_symbol("SPCXUSD1") == "SPCX"


def test_parse_tradfi_contracts_uses_contract_type_not_crypto_rows() -> None:
    rows = parse_tradfi_contracts(
        {
            "symbols": [
                _contract("AAPLUSDT", "EQUITY"),
                _contract("0700USDT", "HK_EQUITY"),
                _contract("XAUUSDT", "COMMODITY"),
                _contract("BTCUSDT", "COIN", contract_type="PERPETUAL"),
            ]
        }
    )

    assert [row["symbol"] for row in rows] == [
        "0700USDT",
        "AAPLUSDT",
        "XAUUSDT",
    ]


def test_sync_links_contracts_to_security_master_with_safe_admission() -> None:
    db = _stock_library_session()
    try:
        result = sync_tradfi_contracts(
            db,
            {
                "symbols": [
                    _contract("AAPLUSDT", "EQUITY"),
                    _contract("0700USDT", "HK_EQUITY"),
                ]
            },
        )

        aapl = db.scalar(select(Security).where(Security.symbol == "AAPL"))
        assert aapl is not None
        assert aapl.exchange == "US"
        assert aapl.finnhub_symbol == "AAPL"
        assert result.profile_security_ids == (aapl.id,)

        mapping = db.scalar(
            select(SecuritySymbolMapping).where(
                SecuritySymbolMapping.source_symbol == "AAPLUSDT"
            )
        )
        assert mapping is not None
        assert mapping.security_id == aapl.id
        assert mapping.monitor_enabled is True
        assert mapping.strategy_enabled is False
        assert mapping.live_trading_enabled is False
        assert mapping.source_status == "TRADING"

        metadata = dict(mapping.source_metadata_json or {})
        metadata["_profile_sync"] = {
            "status": "no_data",
            "checked_at": utcnow().isoformat(),
        }
        mapping.source_metadata_json = metadata
        mapping.mapping_status = "REVIEW_REQUIRED"
        db.commit()
        repeated = sync_tradfi_contracts(
            db,
            {
                "symbols": [
                    _contract("AAPLUSDT", "EQUITY"),
                    _contract("0700USDT", "HK_EQUITY"),
                ]
            },
        )
        db.refresh(mapping)
        assert aapl.id not in repeated.profile_security_ids
        assert mapping.mapping_status == "REVIEW_REQUIRED"
        assert mapping.source_metadata_json["_profile_sync"]["status"] == "no_data"

        hk_mapping = db.scalar(
            select(SecuritySymbolMapping).where(
                SecuritySymbolMapping.source_symbol == "0700USDT"
            )
        )
        assert hk_mapping is not None
        assert hk_mapping.mapping_status == "REVIEW_REQUIRED"
        hk_security = db.get(Security, hk_mapping.security_id)
        assert hk_security is not None
        assert hk_security.security_type == "COMMON_STOCK"
    finally:
        db.close()


def test_packaged_contract_keeps_existing_strategy_and_live_admission() -> None:
    db = _stock_library_session()
    try:
        sync_tradfi_contracts(
            db,
            {"symbols": [_contract("XAUUSDT", "COMMODITY")]},
            preapproved_symbols=("XAUUSDT",),
        )

        mapping = db.scalar(select(SecuritySymbolMapping))
        assert mapping is not None
        assert mapping.monitor_enabled is True
        assert mapping.strategy_enabled is True
        assert mapping.live_trading_enabled is True
        assert (
            mapping.source_metadata_json["_admission_origin"]
            == "legacy_packaged_universe"
        )
    finally:
        db.close()


def test_missing_contract_requires_three_syncs_before_monitor_suspension() -> None:
    db = _stock_library_session()
    try:
        both = {
            "symbols": [
                _contract("AAPLUSDT", "EQUITY"),
                _contract("MSFTUSDT", "EQUITY"),
            ]
        }
        sync_tradfi_contracts(db, both)
        missing = db.scalar(
            select(SecuritySymbolMapping).where(
                SecuritySymbolMapping.source_symbol == "MSFTUSDT"
            )
        )
        assert missing is not None
        missing.strategy_enabled = True
        missing.live_trading_enabled = True
        db.commit()

        only_aapl = {"symbols": [_contract("AAPLUSDT", "EQUITY")]}
        sync_tradfi_contracts(db, only_aapl)
        sync_tradfi_contracts(db, only_aapl)
        db.refresh(missing)
        assert missing.source_status == "TRADING"
        assert missing.monitor_enabled is True

        sync_tradfi_contracts(db, only_aapl)
        db.refresh(missing)
        assert missing.source_status == "MISSING"
        assert missing.monitor_enabled is False
        assert missing.strategy_enabled is False
        assert missing.live_trading_enabled is True
    finally:
        db.close()
