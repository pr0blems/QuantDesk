from pathlib import Path

from quantdesk import config_loader


def test_market_collector_uses_the_single_non_secret_config_directory() -> None:
    expected = (Path(__file__).resolve().parents[1] / "config").resolve()

    assert config_loader.CONFIG_DIR == expected
    assert "price_poll_seconds" in config_loader.settings
    assert len(config_loader.tradfi_symbols()) == 150
    assert not hasattr(config_loader, "api_keys")
