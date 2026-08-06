from __future__ import annotations

from quantdesk.data_quality import quality_gate, source_quality


def test_source_quality_reports_freshness_latency_and_coverage() -> None:
    result = source_quality(
        source="market_microstructure",
        age_ms=2_000,
        latency_ms=80,
        coverage_ratio=0.9,
    )
    assert result["fresh"] is True
    assert result["usable"] is True
    assert result["latency_ms"] == 80
    assert result["coverage_ratio"] == 0.9


def test_quality_gate_abstains_on_stale_or_low_coverage_inputs() -> None:
    assert quality_gate(0.95, age_ms=20_000) == (False, "stale")
    assert quality_gate(0.95, age_ms=1_000, coverage_ratio=0.4) == (False, "coverage_low")
    assert quality_gate(0.6, age_ms=1_000) == (False, "quality_low")
    assert quality_gate(0.9, age_ms=1_000) == (True, "ok")
