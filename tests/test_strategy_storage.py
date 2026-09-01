from __future__ import annotations

import importlib.util
import uuid
from pathlib import Path

import pytest
from sqlalchemy import JSON, ForeignKeyConstraint, func, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from quantdesk_v2.models import (
    AiMonitorConfig,
    StrategyRevision,
    StrategyTemplate,
    User,
    UserStrategy,
)
from quantdesk_v2.strategy_catalog import (
    AI_MONITOR_STRATEGY_TEMPLATE_KEY,
    SYSTEM_STRATEGY_DEFINITIONS,
    StrategyParameterError,
    ai_monitor_strategy_parameters,
    apply_ai_monitor_strategy_parameters,
    ensure_system_templates,
    ensure_user_default_strategies,
    get_user_strategy,
    is_ai_monitor_strategy,
    list_user_strategies,
    serialize_user_strategy,
    serialize_strategy_catalog,
    strategy_management_mode,
    strategy_snapshot,
    validate_ai_monitor_strategy_parameters,
    validate_strategy_parameters,
)

EXPECTED_NAMES = [
    "AI 机会决策策略",
    "多周期趋势回踩延续",
    "趋势突破",
    "MA 金叉",
    "MACD 金叉放量",
    "量价齐升",
    "低波动龙头",
    "断板反包",
    "超跌反弹",
    "布林突破",
    "均线多头",
    "连板股",
    "缩量回踩",
    "新低反转",
    "高换手拉升",
    "连板接力",
    "逼近涨停",
    "超跌反转",
    "均线回踩反弹",
    "强势高开",
    "AI 模拟盘 ATR 趋势",
]


@pytest.fixture
def session(mysql_test_engine: Engine) -> Session:
    with Session(mysql_test_engine) as db:
        yield db


def _user(db: Session, username: str) -> User:
    user = User(username=username, password_hash="test-only-hash")  # noqa: S106
    db.add(user)
    db.flush()
    return user


def test_strategy_models_are_commented_json_backed_and_tenant_scoped() -> None:
    for table in (
        StrategyTemplate.__table__,
        UserStrategy.__table__,
        StrategyRevision.__table__,
    ):
        assert table.comment
        assert all(column.comment for column in table.columns)

    assert isinstance(StrategyTemplate.__table__.c.parameter_schema_json.type, JSON)
    assert isinstance(UserStrategy.__table__.c.parameters_json.type, JSON)
    assert isinstance(StrategyRevision.__table__.c.snapshot_json.type, JSON)

    revision_fk = next(
        constraint
        for constraint in StrategyRevision.__table__.constraints
        if isinstance(constraint, ForeignKeyConstraint)
    )
    assert [element.parent.name for element in revision_fk.elements] == [
        "user_strategy_id",
        "user_id",
    ]
    assert [element.target_fullname for element in revision_fk.elements] == [
        "user_strategies.id",
        "user_strategies.user_id",
    ]
    assert revision_fk.ondelete == "CASCADE"
    assert UserStrategy.revisions.property.cascade.delete_orphan
    assert UserStrategy.revisions.property.passive_deletes


def test_system_catalog_has_full_strategy_and_all_builtin_defaults(session: Session) -> None:
    templates = ensure_system_templates(session)

    assert len(SYSTEM_STRATEGY_DEFINITIONS) == 21
    assert sum(item["template_kind"] == "strategy" for item in SYSTEM_STRATEGY_DEFINITIONS) == 1
    assert (
        sum(
            item["template_kind"] == "builtin_strategy"
            for item in SYSTEM_STRATEGY_DEFINITIONS
        )
        == 20
    )
    assert [template.name for template in templates] == EXPECTED_NAMES
    assert {template.engine_key for template in templates} == {
        "multi_factor",
        "ma_cross",
        "macd_momentum",
        "rsi_reversal",
        "bollinger_reversion",
    }
    for template in templates:
        if template.template_key == AI_MONITOR_STRATEGY_TEMPLATE_KEY:
            assert (
                validate_ai_monitor_strategy_parameters(template.parameters_json)
                == template.parameters_json
            )
        else:
            assert (
                validate_strategy_parameters(template.engine_key, template.parameters_json)
                == template.parameters_json
            )

    paper_template = next(
        template for template in templates if template.template_key == "paper_multifactor_atr_v1"
    )
    assert paper_template.risk_defaults_json == {
        "position_size_pct": 10,
        "leverage": 20,
        "fee_bps": 5,
        "slippage_bps": 3,
        "stop_loss_pct": 3,
        "take_profit_pct": 5,
        "max_holding_bars": 12,
    }
    assert "2.5×ATR 固定止盈" in paper_template.description

    assert len(ensure_system_templates(session)) == 21
    assert session.scalar(select(func.count()).select_from(StrategyTemplate)) == 21


def test_first_login_copy_is_idempotent_and_creates_initial_revisions(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = _user(session, "alice")

    def forbidden_commit() -> None:
        raise AssertionError("catalog helpers must not commit")

    monkeypatch.setattr(session, "commit", forbidden_commit)
    first = ensure_user_default_strategies(session, user.id)
    second = ensure_user_default_strategies(session, user.id)

    assert len(first) == len(second) == 21
    assert [strategy.name for strategy in first] == EXPECTED_NAMES
    assert len({strategy.source_template_id for strategy in first}) == 21
    assert all(uuid.UUID(strategy.public_id).version == 4 for strategy in first)
    assert all(strategy.created_via == "system_default" for strategy in first)
    serialized = [serialize_user_strategy(strategy) for strategy in first]
    assert all(item["complete_strategy"] is True for item in serialized)
    assert {item["management_mode"] for item in serialized} == {
        "managed_parameters",
        "parameterized_engine",
        "strategy_dsl",
    }
    assert all(strategy_management_mode(strategy) != "legacy" for strategy in first)
    assert (
        session.scalar(
            select(func.count()).select_from(UserStrategy).where(UserStrategy.user_id == user.id)
        )
        == 21
    )
    assert (
        session.scalar(
            select(func.count())
            .select_from(StrategyRevision)
            .where(StrategyRevision.user_id == user.id)
        )
        == 21
    )
    assert all(strategy.revisions[0].snapshot_json["name"] == strategy.name for strategy in first)


def test_ai_monitor_strategy_is_visible_versioned_and_uses_real_config(
    session: Session,
) -> None:
    user = _user(session, "ai-monitor-strategy-user")
    strategies = ensure_user_default_strategies(session, user.id)
    managed = next(item for item in strategies if is_ai_monitor_strategy(item))

    assert managed.status == "active"
    assert managed.lifecycle_status == "published"
    assert managed.spec_json["decision_version"] == "actionable_entry_v11"
    assert managed.parameters_json == ai_monitor_strategy_parameters(None)
    assert managed.revisions[0].lifecycle_status == "published"
    schema_by_key = {
        item["key"]: item for item in managed.parameter_schema_json
    }
    assert schema_by_key["monitor_enabled"]["group"] == "运行设置"
    assert schema_by_key["monitor_enabled"]["control"] == "switch"
    assert schema_by_key["news_score_weight"]["group"] == "评分权重"
    assert all(
        item["group"] == "技术指标开关" and item["control"] == "switch"
        for item in managed.parameter_schema_json
        if item["key"].startswith("indicator_")
    )

    edited = dict(managed.parameters_json)
    edited["monitor_enabled"] = 1
    edited["timeframe_minutes"] = 240
    edited["minimum_combined_score"] = 82
    for key in list(edited):
        if key.startswith("indicator_"):
            edited[key] = 0
    edited["indicator_prediction_trend"] = 1
    apply_ai_monitor_strategy_parameters(session, user.id, edited)

    config = session.get(AiMonitorConfig, user.id)
    assert config is not None
    assert config.enabled is True
    assert config.timeframe == "4h"
    assert float(config.minimum_combined_score) == pytest.approx(82)
    assert config.indicator_keys_json == ["prediction_trend"]

    refreshed = ensure_user_default_strategies(session, user.id)
    managed = next(item for item in refreshed if is_ai_monitor_strategy(item))
    assert managed.version == 2
    assert managed.parameters_json["timeframe_minutes"] == 240
    assert managed.parameters_json["minimum_combined_score"] == 82
    assert [revision.version for revision in managed.revisions] == [1, 2]


def test_ai_monitor_strategy_rejects_invalid_cross_field_parameters() -> None:
    parameters = ai_monitor_strategy_parameters(None)
    parameters["news_score_weight"] = 50
    with pytest.raises(StrategyParameterError, match="权重合计"):
        validate_ai_monitor_strategy_parameters(parameters)

    parameters = ai_monitor_strategy_parameters(None)
    for key in list(parameters):
        if key.startswith("indicator_"):
            parameters[key] = 0
    with pytest.raises(StrategyParameterError, match="至少启用一个"):
        validate_ai_monitor_strategy_parameters(parameters)


def test_user_strategy_queries_do_not_cross_tenants(session: Session) -> None:
    alice = _user(session, "alice")
    bob = _user(session, "bob")
    alice_strategies = ensure_user_default_strategies(session, alice.id)
    bob_strategies = ensure_user_default_strategies(session, bob.id)

    assert len(list_user_strategies(session, alice.id)) == 21
    assert len(list_user_strategies(session, bob.id)) == 21
    assert {item.public_id for item in alice_strategies}.isdisjoint(
        {item.public_id for item in bob_strategies}
    )
    assert get_user_strategy(session, bob.id, alice_strategies[0].public_id) is None

    alice_strategies[0].status = "archived"
    session.flush()
    assert len(list_user_strategies(session, alice.id)) == 20
    assert len(list_user_strategies(session, alice.id, include_archived=True)) == 21
    assert len(list_user_strategies(session, bob.id)) == 21


def test_revision_relationship_and_catalog_serialization(session: Session) -> None:
    user = _user(session, "revision-user")
    strategy = ensure_user_default_strategies(session, user.id)[0]
    strategy.version = 2
    strategy.parameters_json = {**strategy.parameters_json, "threshold": 2}
    revision = StrategyRevision(
        user_strategy_id=strategy.id,
        user_id=user.id,
        version=2,
        change_source="ai",
        change_summary="降低入场分数",
        snapshot_json=strategy_snapshot(strategy),
    )
    strategy.revisions.append(revision)
    session.flush()

    assert [item.version for item in strategy.revisions] == [1, 2]
    catalog = serialize_strategy_catalog([strategy])
    assert catalog[0]["id"] == strategy.public_id
    assert catalog[0]["engine_key"] == "multi_factor"
    assert next(item for item in catalog[0]["params"] if item["key"] == "threshold")["default"] == 2


@pytest.mark.parametrize(
    ("engine_key", "parameters"),
    [
        ("ma_cross", {"fast_period": 50, "slow_period": 20}),
        ("rsi_reversal", {"period": 14, "oversold": 80, "overbought": 70}),
        ("bollinger_reversion", {"period": 20, "stddev": 6}),
        ("multi_factor", {"fast_period": 20, "unknown": 1}),
    ],
)
def test_parameter_validation_is_strict(engine_key: str, parameters: dict) -> None:
    with pytest.raises(StrategyParameterError):
        validate_strategy_parameters(engine_key, parameters)


def test_strategy_migration_follows_quantity_precision_revision() -> None:
    migration_path = (
        Path(__file__).parents[1] / "migrations" / "versions" / "0006_add_strategy_tables.py"
    )
    spec = importlib.util.spec_from_file_location("strategy_migration_0006", migration_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    assert module.revision == "0006_strategy_tables"
    assert module.down_revision == "0005_backtest_qty_precision"
    assert len(module.SEED_TEMPLATES) == 18
    assert [item[1] for item in module.SEED_TEMPLATES] == EXPECTED_NAMES[2:-1]


def test_paper_strategy_migration_follows_strategy_tables_revision() -> None:
    migration_path = (
        Path(__file__).parents[1]
        / "migrations"
        / "versions"
        / "0007_add_paper_strategy_template.py"
    )
    spec = importlib.util.spec_from_file_location("paper_strategy_migration_0007", migration_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    assert module.revision == "0007_paper_strategy_template"
    assert module.down_revision == "0006_strategy_tables"
    assert module.TEMPLATE_KEY == "paper_multifactor_atr_v1"
    assert module.RISK_DEFAULTS["take_profit_pct"] == 5
    assert "2.5×ATR 固定止盈" in module.DESCRIPTION
