from pathlib import Path

STATIC_DIR = Path(__file__).parents[1] / "web" / "admin-source"


def test_stock_library_names_open_accessible_detail_dialog() -> None:
    html = (STATIC_DIR / "admin.html").read_text(encoding="utf-8")
    script = (STATIC_DIR / "admin.js").read_text(encoding="utf-8")

    assert 'id="stock-detail-dialog"' in html
    assert 'aria-labelledby="stock-detail-title"' in html
    assert 'class="stock-detail-trigger"' in script
    assert 'data-stock-detail="${escapeHtml(item.symbol)}"' in script
    assert "openStockDetail(detailButton.dataset.stockDetail, detailButton)" in script
    assert "api(`/stock-library/${encodeURIComponent(symbol)}`)" in script


def test_stock_detail_dialog_supports_close_retry_and_focus_restore() -> None:
    script = (STATIC_DIR / "admin.js").read_text(encoding="utf-8")

    assert 'data-stock-detail-retry="${escapeHtml(symbol)}"' in script
    assert '$("#stock-detail-close").addEventListener("click"' in script
    assert '$("#stock-detail-dialog").addEventListener("close"' in script
    assert "stockDetailTrigger.focus()" in script
