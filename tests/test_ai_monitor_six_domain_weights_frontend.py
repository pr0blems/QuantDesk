from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = (ROOT / "src/quantdesk_v2/static/ai-monitor.js").read_text(encoding="utf-8")
STYLES = (ROOT / "src/quantdesk_v2/static/ai-monitor.css").read_text(encoding="utf-8")


def test_weight_editor_exposes_all_six_scoring_domains() -> None:
    expected = {
        "news": ("config-news-weight", "新闻与历史研判"),
        "technical": ("config-technical-weight", "技术指标"),
        "options_flow": ("config-options-flow-weight", "个股期权资金流"),
        "market_context": ("config-market-context-weight", "宏观与板块环境"),
        "gex": ("config-gex-weight", "GEX 与波动率结构"),
        "institutional_flow": (
            "config-institutional-flow-weight",
            "Lit / Off-lit 机构确认",
        ),
    }
    for domain, (field_id, label) in expected.items():
        assert f'id="{field_id}"' in SCRIPT
        assert f'data-weight-domain="{domain}"' in SCRIPT
        assert label in SCRIPT

    assert 'aria-label="六域权重占比预览"' in SCRIPT
    assert "六类证据共同决定机会组合分" in SCRIPT
    assert "历史机会保留生成时的冻结权重与版本" in SCRIPT


def test_weight_editor_defaults_validate_and_save_versioned_six_domain_payload() -> None:
    for key, value in {
        "news": 20,
        "technical": 30,
        "options_flow": 20,
        "market_context": 10,
        "gex": 10,
        "institutional_flow": 10,
    }.items():
        assert f"{key}: {value}," in SCRIPT

    assert 'this.api("/score-policy")' in SCRIPT
    assert "config.news_score_weight" in SCRIPT
    assert 'this.api("/score-policy", {' in SCRIPT
    assert "weights: Object.fromEntries" in SCRIPT
    assert "value / 100" in SCRIPT
    assert 'const valid = Math.abs(total - 100) <= 0.01;' in SCRIPT
    assert 'modeState.textContent = ["score", "gate"].includes(mode) ? "参与组合评分" : "当前仅记录"' in SCRIPT
    assert "六域评分策略为平台级配置，仅管理员可以修改" in SCRIPT
    assert "this.state.weightDraftDirty = true" in SCRIPT
    assert "if (!this.state.weightDraftDirty)" in SCRIPT
    assert "this.state.weightDraftDirty = false" in SCRIPT
    for status in ("已保存", "待保存", "需调整为 100%"):
        assert status in SCRIPT


def test_ai_monitor_exposes_unusual_whales_platform_switch() -> None:
    assert 'id="uw-usage-toggle"' in SCRIPT
    assert 'this.api("/unusual-whales-enabled", {' in SCRIPT
    assert 'id="finnhub-usage-toggle"' in SCRIPT
    assert 'this.api("/finnhub-enabled", {' in SCRIPT
    assert "Finnhub 美股现货" in SCRIPT
    assert "盘中采集" in SCRIPT
    assert "provider-quote-badge" in SCRIPT
    assert "binanceQuote" in SCRIPT
    assert "finnhubSpot" in SCRIPT
    assert "unusualWhalesQuote" in SCRIPT
    assert "5分钟/次" in SCRIPT


def test_weight_editor_has_distinct_six_domain_visual_language() -> None:
    for selector in (
        ".weight-grid .news",
        ".weight-grid .technical",
        ".weight-grid .options",
        ".weight-grid .macro",
        ".weight-grid .gex",
        ".weight-grid .institutional",
        ".weight-preview .options-flow",
        ".weight-preview .market-context",
        ".weight-preview .institutional-flow",
    ):
        assert selector in STYLES
    assert ".weight-config > header > aside > span.dirty" in STYLES
    assert ".weight-grid > label::before" in STYLES
