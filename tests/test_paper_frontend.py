from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_paper_adjustment_edits_bound_strategy_parameters() -> None:
    script = (ROOT / "web/src/controllers/paper.js").read_text(encoding="utf-8")
    stylesheet = (ROOT / "web/public/assets/paper.css").read_text(encoding="utf-8")

    for contract in (
        'href="/next/assets/paper.css?v=20260905-strategy-parameters"',
        "调整策略参数",
        'id="paper-adjust-strategy"',
        'id="paper-adjust-parameters"',
        "strategy.parameter_schema",
        'data-paper-param-key="',
        "/api/v2/backtests/strategy-parameters/",
        'scope: "default"',
        "下一轮信号计算时使用",
    ):
        assert contract in script

    for removed_account_setting in (
        'id="paper-adjust-leverage"',
        'id="paper-adjust-max-positions"',
        'id="paper-adjust-position-size"',
        'id="paper-adjust-margin-cap"',
    ):
        assert removed_account_setting not in script

    assert ".strategy-parameter-groups" in stylesheet
    assert ".strategy-parameter-grid" in stylesheet
