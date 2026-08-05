from quantdesk_v2.stock_library import normalize_contract_symbol


def test_normalize_binance_tradfi_stock_symbols() -> None:
    assert normalize_contract_symbol("AAPLUSDT") == "AAPL"
    assert normalize_contract_symbol("BRKBUSDT") == "BRK.B"
    assert normalize_contract_symbol("SPCXUSD1") == "SPCX"
