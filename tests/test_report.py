from __future__ import annotations

from quantdesk_v2 import report


def test_conclusion_labels_are_unchanged() -> None:
    assert report.conclusion_label(None) == ("数据不足", "#77808f")
    assert report.conclusion_label(75) == ("强烈看多", "#2ebd85")
    assert report.conclusion_label(40) == ("看多", "#7fc8a9")
    assert report.conclusion_label(-75) == ("强烈看空", "#f6465d")
    assert report.conclusion_label(-40) == ("看空", "#e98a97")
    assert report.conclusion_label(0) == ("中性观望", "#77808f")


def test_report_shape_and_weighting_are_unchanged(monkeypatch) -> None:
    monkeypatch.setattr(
        report,
        "_tf_scores",
        lambda symbol: {
            "15m": {"score": 60, "factors": []},
            "1h": {"score": 80, "factors": []},
            "4h": {"score": 40, "factors": []},
        },
    )

    def query(sql, params=()):
        if "price, pct_24h" in sql:
            return [{"price": 100.0, "pct_24h": 2.0}]
        if "FROM news" in sql or "FROM social" in sql:
            return []
        if "quote_volume" in sql:
            return [{"quote_volume": 5_000.0}]
        raise AssertionError(f"unexpected query: {sql!r} {params!r}")

    monkeypatch.setattr(report.store, "query", query)
    monkeypatch.setattr(report.store, "get_klines", lambda *args, **kwargs: [])
    monkeypatch.setattr(report.store, "system_state_get", lambda *args, **kwargs: {})

    result = report.build_report("TESTUSDT")

    assert result["combined"] == 62
    assert result["label"] == "看多"
    assert result["tf_scores"] == {"15m": 60, "1h": 80, "4h": 40}
    assert [item["score"] for item in result["horizons"]] == [70, 56, 50]
    assert result["stats"] == {"24h成交额": "5.0K USDT"}
    assert result["social"] == {}
    assert result["news_direct"] is False
