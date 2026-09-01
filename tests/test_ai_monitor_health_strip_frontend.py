from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = (ROOT / "src/quantdesk_v2/static/ai-monitor.js").read_text(encoding="utf-8")


def test_data_health_strip_is_not_mounted_in_ai_monitor_shell() -> None:
    assert '<section id="signal-health-strip"' not in SCRIPT
    assert 'aria-label="实时数据健康与风险状态"' not in SCRIPT


def test_data_health_calculation_remains_available_without_a_mount_target() -> None:
    assert 'renderSignalHealth() {' in SCRIPT
    assert 'const target = this.q("#signal-health-strip");' in SCRIPT
    assert 'if (!target) return;' in SCRIPT
