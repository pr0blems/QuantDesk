from pathlib import Path

from quantdesk import config_loader


def test_market_collector_uses_the_single_non_secret_config_directory() -> None:
    expected = (Path(__file__).resolve().parents[1] / "config").resolve()

    assert config_loader.CONFIG_DIR == expected
    assert "price_poll_seconds" in config_loader.settings
    symbols = config_loader.tradfi_symbols()
    assert len(symbols) == 151
    assert "GIGADEVUSDT" in symbols
    assert not hasattr(config_loader, "api_keys")
