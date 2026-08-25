"""Sandboxed runtime for user-authored strategy source code.

The source language is real Python, but intentionally exposes a small pure-
calculation surface.  Source is parsed before publication and parsed again in
an isolated worker before execution.  Imports, attributes, I/O and dynamic
code execution are unavailable.
"""

from __future__ import annotations

import ast
import hashlib
import json
import math
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

MAX_SOURCE_BYTES = 64 * 1024
MAX_AST_NODES = 4_000
MAX_BATCH_CONTEXTS = 20_000
MAX_INPUT_BYTES = 64 * 1024 * 1024
MAX_OUTPUT_BYTES = 16 * 1024 * 1024
SUPPORTED_TIMEFRAMES = frozenset({"1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "6h", "8h", "12h", "1d"})
DECISIONS = frozenset({"LONG_ENTRY", "SHORT_ENTRY", "HOLD", "SKIP"})
REQUIRED_CONSTANTS = frozenset({"TIMEFRAMES", "TRIGGER_TIMEFRAME", "LOOKBACK_BARS", "DIRECTIONS"})
OPTIONAL_CONSTANTS = frozenset({"VALID_FOR_BARS", "PARAMETERS"})
FORBIDDEN_NAMES = frozenset(
    {
        "__import__",
        "breakpoint",
        "compile",
        "delattr",
        "dir",
        "eval",
        "exec",
        "getattr",
        "globals",
        "help",
        "input",
        "locals",
        "memoryview",
        "open",
        "setattr",
        "vars",
    }
)
SAFE_CALLS = frozenset(
    {
        "abs",
        "all",
        "adx",
        "any",
        "atr",
        "bool",
        "dict",
        "ema",
        "enumerate",
        "exp",
        "float",
        "int",
        "len",
        "list",
        "log",
        "max",
        "min",
        "range",
        "round",
        "rsi",
        "sma",
        "sorted",
        "sqrt",
        "sum",
        "tuple",
        "zip",
    }
)
FORBIDDEN_NODES = (
    ast.AsyncFunctionDef,
    ast.Await,
    ast.ClassDef,
    ast.Delete,
    ast.Global,
    ast.Import,
    ast.ImportFrom,
    ast.Lambda,
    ast.Match,
    ast.NamedExpr,
    ast.Nonlocal,
    ast.Try,
    ast.With,
    ast.Yield,
    ast.YieldFrom,
)


class StrategySourceError(ValueError):
    """Source is invalid, unsafe, or returned an invalid decision."""


class StrategySourceExecutionError(RuntimeError):
    """The isolated source worker failed or exceeded its limits."""


@dataclass(frozen=True, slots=True)
class SourceMetadata:
    language: Literal["python"]
    runtime_version: str
    timeframes: tuple[str, ...]
    trigger_timeframe: str
    lookback_bars: int
    directions: tuple[str, ...]
    valid_for_bars: int
    parameter_keys: tuple[str, ...]
    parameter_schema: tuple[dict[str, Any], ...]
    source_hash: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "language": self.language,
            "runtime_version": self.runtime_version,
            "timeframes": list(self.timeframes),
            "trigger_timeframe": self.trigger_timeframe,
            "lookback_bars": self.lookback_bars,
            "directions": list(self.directions),
            "valid_for_bars": self.valid_for_bars,
            "parameter_keys": list(self.parameter_keys),
            "parameter_schema": [dict(item) for item in self.parameter_schema],
            "source_hash": self.source_hash,
        }


@dataclass(frozen=True, slots=True)
class SourceStrategyDecision:
    decision: Literal["LONG_ENTRY", "SHORT_ENTRY", "HOLD", "SKIP"]
    signal_time: int | None
    valid_until: int | None
    confidence: float | None
    reason_codes: tuple[str, ...]
    evidence: dict[str, Any]
    risk_proposal: dict[str, Any]


def source_hash(source_code: str) -> str:
    return hashlib.sha256(_normalized_source(source_code).encode("utf-8")).hexdigest()


def validate_source(source_code: str, language: str = "python") -> SourceMetadata:
    if language != "python":
        raise StrategySourceError("首个源码运行时仅支持 Python")
    normalized = _normalized_source(source_code)
    try:
        tree = ast.parse(normalized, filename="<strategy>", mode="exec")
    except SyntaxError as exc:
        raise StrategySourceError(f"Python 语法错误（第 {exc.lineno or 0} 行）") from None
    nodes = list(ast.walk(tree))
    if len(nodes) > MAX_AST_NODES:
        raise StrategySourceError("策略源码结构过于复杂")

    functions: set[str] = set()
    constants: dict[str, Any] = {}
    for statement in tree.body:
        if isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Constant) and isinstance(statement.value.value, str):
            continue
        if isinstance(statement, ast.Assign) and len(statement.targets) == 1 and isinstance(statement.targets[0], ast.Name):
            name = statement.targets[0].id
            if name not in REQUIRED_CONSTANTS | OPTIONAL_CONSTANTS:
                raise StrategySourceError(f"顶层只允许声明运行时常量，不能声明 {name}")
            if name in constants:
                raise StrategySourceError(f"运行时常量不能重复声明：{name}")
            try:
                constants[name] = ast.literal_eval(statement.value)
            except (TypeError, ValueError, SyntaxError):
                raise StrategySourceError(f"运行时常量 {name} 必须是字面量") from None
            continue
        if isinstance(statement, ast.FunctionDef):
            if statement.decorator_list:
                raise StrategySourceError("策略函数不能使用装饰器")
            if statement.name.startswith("_"):
                raise StrategySourceError("策略函数名称不能以下划线开头")
            if statement.name in functions:
                raise StrategySourceError(f"策略函数不能重复定义：{statement.name}")
            functions.add(statement.name)
            continue
        raise StrategySourceError("顶层只允许常量、函数和文档字符串")

    missing = sorted(REQUIRED_CONSTANTS - set(constants))
    if missing:
        raise StrategySourceError(f"缺少运行时常量：{', '.join(missing)}")
    if "evaluate" not in functions:
        raise StrategySourceError("必须定义 evaluate(context, params) 函数")

    for node in nodes:
        if isinstance(node, FORBIDDEN_NODES):
            raise StrategySourceError(f"不允许使用 Python 语法：{type(node).__name__}")
        if isinstance(node, ast.Attribute):
            raise StrategySourceError("不允许访问对象属性；请使用字典索引和内置指标函数")
        if isinstance(node, ast.Name) and (node.id.startswith("_") or node.id in FORBIDDEN_NAMES):
            raise StrategySourceError(f"不允许使用名称：{node.id}")
        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name):
                raise StrategySourceError("函数调用必须使用直接函数名")
            if node.func.id not in SAFE_CALLS | functions:
                raise StrategySourceError(f"不允许调用函数：{node.func.id}")

    evaluate = next(
        node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "evaluate"
    )
    positional = [*evaluate.args.posonlyargs, *evaluate.args.args]
    if (
        [item.arg for item in positional] != ["context", "params"]
        or evaluate.args.vararg
        or evaluate.args.kwarg
        or evaluate.args.kwonlyargs
        or evaluate.args.defaults
    ):
        raise StrategySourceError("evaluate 函数签名必须是 evaluate(context, params)")

    parameter_keys = tuple(
        sorted(
            {
                str(node.slice.value)
                for node in nodes
                if isinstance(node, ast.Subscript)
                and isinstance(node.value, ast.Name)
                and node.value.id == "params"
                and isinstance(node.slice, ast.Constant)
                and isinstance(node.slice.value, str)
            }
        )
    )
    parameter_schema = _parameter_schema_from_constant(
        constants.get("PARAMETERS"), parameter_keys
    )

    raw_timeframes = constants["TIMEFRAMES"]
    if not isinstance(raw_timeframes, (tuple, list)) or not 1 <= len(raw_timeframes) <= 6:
        raise StrategySourceError("TIMEFRAMES 必须包含 1 到 6 个周期")
    timeframes = tuple(dict.fromkeys(str(value) for value in raw_timeframes))
    if any(value not in SUPPORTED_TIMEFRAMES for value in timeframes):
        raise StrategySourceError("TIMEFRAMES 包含不支持的周期")
    trigger = constants["TRIGGER_TIMEFRAME"]
    if not isinstance(trigger, str) or trigger not in timeframes:
        raise StrategySourceError("TRIGGER_TIMEFRAME 必须属于 TIMEFRAMES")
    lookback = constants["LOOKBACK_BARS"]
    if isinstance(lookback, bool) or not isinstance(lookback, int) or not 20 <= lookback <= 2_000:
        raise StrategySourceError("LOOKBACK_BARS 必须是 20 到 2000 的整数")
    raw_directions = constants["DIRECTIONS"]
    if not isinstance(raw_directions, (tuple, list)) or not raw_directions:
        raise StrategySourceError("DIRECTIONS 必须声明 long 和/或 short")
    directions = tuple(dict.fromkeys(str(value) for value in raw_directions))
    if any(value not in {"long", "short"} for value in directions):
        raise StrategySourceError("DIRECTIONS 只支持 long、short")
    valid_for_bars = constants.get("VALID_FOR_BARS", 1)
    if isinstance(valid_for_bars, bool) or not isinstance(valid_for_bars, int) or not 1 <= valid_for_bars <= 10:
        raise StrategySourceError("VALID_FOR_BARS 必须是 1 到 10 的整数")
    return SourceMetadata(
        language="python",
        runtime_version="python_sandbox_v1",
        timeframes=timeframes,
        trigger_timeframe=trigger,
        lookback_bars=lookback,
        directions=directions,
        valid_for_bars=valid_for_bars,
        parameter_keys=parameter_keys,
        parameter_schema=parameter_schema,
        source_hash=source_hash(normalized),
    )


def _parameter_schema_from_constant(
    raw: Any, parameter_keys: tuple[str, ...]
) -> tuple[dict[str, Any], ...]:
    """Validate the source-owned, literal parameter contract.

    PARAMETERS is optional for legacy sources. New/AI-authored sources use it so
    the configuration UI can be derived from the executable source rather than a
    stale database-only schema.
    """

    if raw is None:
        return ()
    if not isinstance(raw, dict) or len(raw) > 32:
        raise StrategySourceError("PARAMETERS 必须是最多包含 32 项的字典")
    schema: list[dict[str, Any]] = []
    for raw_key, raw_definition in raw.items():
        if (
            not isinstance(raw_key, str)
            or not raw_key.isidentifier()
            or raw_key.startswith("_")
            or len(raw_key) > 64
        ):
            raise StrategySourceError("PARAMETERS 参数名必须是有效的 Python 标识符")
        if not isinstance(raw_definition, dict):
            raise StrategySourceError(f"PARAMETERS.{raw_key} 必须是参数定义字典")
        unknown = sorted(
            set(raw_definition) - {"label", "type", "default", "min", "max", "step", "help"}
        )
        if unknown:
            raise StrategySourceError(
                f"PARAMETERS.{raw_key} 包含未知字段：{', '.join(unknown)}"
            )
        parameter_type = raw_definition.get("type", "number")
        if parameter_type not in {"integer", "number"}:
            raise StrategySourceError(
                f"PARAMETERS.{raw_key}.type 只支持 integer 或 number"
            )
        label = raw_definition.get("label", raw_key.replace("_", " "))
        help_text = raw_definition.get("help")
        if not isinstance(label, str) or not label.strip() or len(label) > 80:
            raise StrategySourceError(f"PARAMETERS.{raw_key}.label 格式无效")
        if help_text is not None and (
            not isinstance(help_text, str) or len(help_text) > 200
        ):
            raise StrategySourceError(f"PARAMETERS.{raw_key}.help 格式无效")
        if "default" not in raw_definition:
            raise StrategySourceError(f"PARAMETERS.{raw_key} 缺少 default")
        default = _finite_parameter_number(
            raw_definition["default"], f"PARAMETERS.{raw_key}.default"
        )
        minimum = _finite_parameter_number(
            raw_definition.get("min", -1_000_000), f"PARAMETERS.{raw_key}.min"
        )
        maximum = _finite_parameter_number(
            raw_definition.get("max", 1_000_000), f"PARAMETERS.{raw_key}.max"
        )
        step = _finite_parameter_number(
            raw_definition.get("step", 1 if parameter_type == "integer" else 0.1),
            f"PARAMETERS.{raw_key}.step",
        )
        if minimum > maximum or not minimum <= default <= maximum or step <= 0:
            raise StrategySourceError(f"PARAMETERS.{raw_key} 的范围或默认值无效")
        if parameter_type == "integer" and any(
            not float(value).is_integer() for value in (minimum, maximum, default, step)
        ):
            raise StrategySourceError(f"PARAMETERS.{raw_key} 的整数配置必须使用整数")
        definition: dict[str, Any] = {
            "key": raw_key,
            "label": label.strip(),
            "type": parameter_type,
            "default": int(default) if parameter_type == "integer" else default,
            "min": int(minimum) if parameter_type == "integer" else minimum,
            "max": int(maximum) if parameter_type == "integer" else maximum,
            "step": int(step) if parameter_type == "integer" else step,
        }
        if help_text:
            definition["help"] = help_text.strip()
        schema.append(definition)
    declared_keys = {item["key"] for item in schema}
    referenced_keys = set(parameter_keys)
    missing = sorted(referenced_keys - declared_keys)
    unused = sorted(declared_keys - referenced_keys)
    if missing:
        raise StrategySourceError(
            "PARAMETERS 缺少源码引用的参数：" + ", ".join(missing)
        )
    if unused:
        raise StrategySourceError(
            "PARAMETERS 包含源码未使用的参数：" + ", ".join(unused)
        )
    return tuple(schema)


def _finite_parameter_number(value: Any, path: str) -> int | float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise StrategySourceError(f"{path} 必须是数字")
    numeric = float(value)
    if not math.isfinite(numeric) or abs(numeric) > 1_000_000:
        raise StrategySourceError(f"{path} 必须是有限且合理的数字")
    return int(numeric) if numeric.is_integer() else numeric


def evaluate_source(
    source_code: str,
    context: dict[str, Any],
    params: dict[str, Any],
    *,
    language: str = "python",
    timeout_seconds: float = 1.5,
) -> SourceStrategyDecision:
    return evaluate_source_many(
        source_code,
        [context],
        params,
        language=language,
        timeout_seconds=timeout_seconds,
    )[0]


def evaluate_source_many(
    source_code: str,
    contexts: list[dict[str, Any]],
    params: dict[str, Any],
    *,
    language: str = "python",
    timeout_seconds: float = 8.0,
) -> list[SourceStrategyDecision]:
    metadata = validate_source(source_code, language)
    if not contexts or len(contexts) > MAX_BATCH_CONTEXTS:
        raise StrategySourceError("策略求值批次数量无效")
    payload = json.dumps(
        {"source_code": _normalized_source(source_code), "contexts": contexts, "params": params},
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(payload) > MAX_INPUT_BYTES:
        raise StrategySourceError("策略求值输入超过隔离进程限制")
    command = [sys.executable, "-I", str(Path(__file__).resolve()), "--worker"]
    env = {"PYTHONIOENCODING": "utf-8", "PYTHONHASHSEED": "0"}
    try:
        completed = subprocess.run(  # noqa: S603 - executable is the current trusted Python
            command,
            input=payload,
            capture_output=True,
            timeout=max(0.1, timeout_seconds),
            check=False,
            env=env,
        )
    except subprocess.TimeoutExpired:
        raise StrategySourceExecutionError("策略执行超时") from None
    if completed.returncode != 0:
        category = completed.stderr.decode("utf-8", errors="replace").strip()[:300]
        raise StrategySourceExecutionError(category or "策略隔离进程执行失败")
    if len(completed.stdout) > MAX_OUTPUT_BYTES:
        raise StrategySourceExecutionError("策略输出超过限制")
    try:
        decoded = json.loads(completed.stdout)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise StrategySourceExecutionError("策略隔离进程返回了无效数据") from None
    if not isinstance(decoded, list) or len(decoded) != len(contexts):
        raise StrategySourceExecutionError("策略隔离进程返回数量不匹配")
    return [
        _decision_from_output(item, context, metadata, params)
        for item, context in zip(decoded, contexts, strict=True)
    ]


def default_python_source() -> str:
    return '''"""可编辑 Python 策略示例：EMA 趋势交叉。"""

TIMEFRAMES = ("1h",)
TRIGGER_TIMEFRAME = "1h"
LOOKBACK_BARS = 200
DIRECTIONS = ("long", "short")
VALID_FOR_BARS = 1
PARAMETERS = {
    "fast_period": {"label": "快速 EMA", "type": "integer", "default": 20, "min": 2, "max": 200, "step": 1},
    "slow_period": {"label": "慢速 EMA", "type": "integer", "default": 50, "min": 3, "max": 500, "step": 1},
    "atr_period": {"label": "ATR 周期", "type": "integer", "default": 14, "min": 2, "max": 100, "step": 1},
    "stop_atr": {"label": "止损 ATR 倍数", "type": "number", "default": 1.5, "min": 0.1, "max": 10, "step": 0.1},
    "take_profit_r": {"label": "止盈 R 倍数", "type": "number", "default": 2, "min": 0.1, "max": 20, "step": 0.1},
}

def evaluate(context, params):
    bars = context["bars"][TRIGGER_TIMEFRAME]
    closes = [bar["close"] for bar in bars]
    fast_period = int(params["fast_period"])
    slow_period = int(params["slow_period"])
    if len(closes) < slow_period + 1:
        return {"decision": "SKIP", "reason_codes": ["WARMUP"]}
    fast_now = ema(closes, fast_period)
    slow_now = ema(closes, slow_period)
    fast_prev = ema(closes[:-1], fast_period)
    slow_prev = ema(closes[:-1], slow_period)
    current_atr = atr(bars, int(params["atr_period"]))
    stop_distance = current_atr * float(params["stop_atr"])
    risk = {
        "stop_distance": stop_distance,
        "take_profit_distance": stop_distance * float(params["take_profit_r"]),
    }
    evidence = {"fast_ema": fast_now, "slow_ema": slow_now, "atr": current_atr}
    if fast_prev <= slow_prev and fast_now > slow_now:
        return {"decision": "LONG_ENTRY", "confidence": 0.7, "reason_codes": ["EMA_CROSS_UP"], "evidence": evidence, "risk_proposal": risk}
    if fast_prev >= slow_prev and fast_now < slow_now:
        return {"decision": "SHORT_ENTRY", "confidence": 0.7, "reason_codes": ["EMA_CROSS_DOWN"], "evidence": evidence, "risk_proposal": risk}
    return {"decision": "HOLD", "reason_codes": ["NO_CROSS"], "evidence": evidence}
'''


def conversion_python_source() -> str:
    return '''"""源码迁移起点：请实现入场逻辑后再保存和部署。"""

TIMEFRAMES = ("1h",)
TRIGGER_TIMEFRAME = "1h"
LOOKBACK_BARS = 200
DIRECTIONS = ("long", "short")
VALID_FOR_BARS = 1
PARAMETERS = {}

def evaluate(context, params):
    bars = context["bars"][TRIGGER_TIMEFRAME]
    if len(bars) < LOOKBACK_BARS:
        return {"decision": "SKIP", "reason_codes": ["WARMUP"]}
    return {
        "decision": "HOLD",
        "reason_codes": ["SOURCE_LOGIC_NOT_IMPLEMENTED"],
        "evidence": {"latest_close": bars[-1]["close"]},
    }
'''


def default_python_parameters() -> tuple[list[dict[str, Any]], dict[str, int | float]]:
    schema = [dict(item) for item in validate_source(default_python_source()).parameter_schema]
    return schema, {item["key"]: item["default"] for item in schema}


def _normalized_source(source_code: str) -> str:
    if not isinstance(source_code, str):
        raise StrategySourceError("策略源码必须是文本")
    normalized = source_code.replace("\r\n", "\n").replace("\r", "\n").strip() + "\n"
    if not normalized.strip() or len(normalized.encode("utf-8")) > MAX_SOURCE_BYTES:
        raise StrategySourceError("策略源码不能为空且不能超过 64 KiB")
    if "\x00" in normalized:
        raise StrategySourceError("策略源码包含无效字符")
    return normalized


def _decision_from_output(
    value: Any,
    context: dict[str, Any],
    metadata: SourceMetadata,
    params: dict[str, Any],
) -> SourceStrategyDecision:
    if not isinstance(value, dict):
        raise StrategySourceExecutionError("evaluate 必须返回字典")
    unknown = set(value) - {"decision", "confidence", "reason_codes", "evidence", "risk_proposal"}
    if unknown:
        raise StrategySourceExecutionError(f"策略返回了未知字段：{', '.join(sorted(unknown))}")
    decision = value.get("decision")
    if decision not in DECISIONS:
        raise StrategySourceExecutionError("decision 返回值无效")
    if decision == "LONG_ENTRY" and "long" not in metadata.directions:
        raise StrategySourceExecutionError("策略返回了未声明的 long 方向")
    if decision == "SHORT_ENTRY" and "short" not in metadata.directions:
        raise StrategySourceExecutionError("策略返回了未声明的 short 方向")
    confidence = value.get("confidence")
    if confidence is not None:
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
            raise StrategySourceExecutionError("confidence 必须是 0 到 1 的数字")
        confidence = float(confidence)
        if not math.isfinite(confidence) or not 0 <= confidence <= 1:
            raise StrategySourceExecutionError("confidence 必须是 0 到 1 的数字")
    reasons = value.get("reason_codes", [])
    if not isinstance(reasons, list) or len(reasons) > 20 or any(not isinstance(item, str) or len(item) > 80 for item in reasons):
        raise StrategySourceExecutionError("reason_codes 格式无效")
    evidence = value.get("evidence", {})
    risk = value.get("risk_proposal", {})
    if not isinstance(evidence, dict) or not isinstance(risk, dict):
        raise StrategySourceExecutionError("evidence 和 risk_proposal 必须是字典")
    _ensure_json_value(evidence)
    _ensure_json_value(risk)
    signal_time = _latest_signal_time(context, metadata.trigger_timeframe)
    interval = _timeframe_seconds(metadata.trigger_timeframe)
    scale = 1_000 if signal_time is not None and signal_time >= 100_000_000_000 else 1
    valid_for_bars = metadata.valid_for_bars
    if "signal_valid_bars" in metadata.parameter_keys:
        configured_validity = params.get("signal_valid_bars")
        if (
            isinstance(configured_validity, bool)
            or not isinstance(configured_validity, (int, float))
            or not float(configured_validity).is_integer()
            or not 1 <= int(configured_validity) <= 10
        ):
            raise StrategySourceExecutionError("signal_valid_bars 必须是 1 到 10 的整数")
        valid_for_bars = int(configured_validity)
    valid_until = (
        signal_time + interval * scale * valid_for_bars
        if signal_time is not None
        else None
    )
    return SourceStrategyDecision(
        decision=decision,
        signal_time=signal_time,
        valid_until=valid_until,
        confidence=confidence,
        reason_codes=tuple(reasons),
        evidence=evidence,
        risk_proposal=risk,
    )


def _latest_signal_time(context: dict[str, Any], timeframe: str) -> int | None:
    try:
        value = context["bars"][timeframe][-1]["open_time"]
    except (KeyError, IndexError, TypeError):
        return None
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _timeframe_seconds(value: str) -> int:
    unit = value[-1]
    amount = int(value[:-1])
    return amount * {"m": 60, "h": 3_600, "d": 86_400}[unit]


def _ensure_json_value(value: Any) -> None:
    try:
        encoded = json.dumps(value, allow_nan=False, separators=(",", ":"))
    except (TypeError, ValueError):
        raise StrategySourceExecutionError("策略输出必须是有限 JSON 数据") from None
    if len(encoded.encode("utf-8")) > 128 * 1024:
        raise StrategySourceExecutionError("单次策略输出超过限制")


def _sma(values: list[float], period: int) -> float:
    if period <= 0 or len(values) < period:
        return math.nan
    return sum(float(item) for item in values[-period:]) / period


def _ema(values: list[float], period: int) -> float:
    if period <= 0 or len(values) < period:
        return math.nan
    multiplier = 2.0 / (period + 1)
    result = sum(float(item) for item in values[:period]) / period
    for item in values[period:]:
        result = (float(item) - result) * multiplier + result
    return result


def _rsi(values: list[float], period: int) -> float:
    if period <= 0 or len(values) <= period:
        return math.nan
    changes = [float(values[index]) - float(values[index - 1]) for index in range(len(values) - period, len(values))]
    gains = sum(max(change, 0.0) for change in changes) / period
    losses = sum(max(-change, 0.0) for change in changes) / period
    return 100.0 if losses == 0 else 100.0 - 100.0 / (1.0 + gains / losses)


def _atr(bars: list[dict[str, Any]], period: int) -> float:
    if period <= 0 or len(bars) <= period:
        return math.nan
    relevant = bars[-(period + 1):]
    ranges = []
    for index in range(1, len(relevant)):
        current = relevant[index]
        previous = relevant[index - 1]
        high = float(current["high"])
        low = float(current["low"])
        previous_close = float(previous["close"])
        ranges.append(max(high - low, abs(high - previous_close), abs(low - previous_close)))
    return sum(ranges) / len(ranges)


def _adx(bars: list[dict[str, Any]], period: int) -> tuple[float, float, float]:
    """Return ADX, +DI and -DI using rolling Wilder-style directional ranges."""

    if period <= 0 or len(bars) <= period * 2:
        return math.nan, math.nan, math.nan
    dx_values: list[float] = []
    latest_plus_di = math.nan
    latest_minus_di = math.nan
    first_end = len(bars) - period + 1
    for end in range(first_end, len(bars) + 1):
        relevant = bars[end - period - 1 : end]
        true_range_total = 0.0
        plus_dm_total = 0.0
        minus_dm_total = 0.0
        for index in range(1, len(relevant)):
            current = relevant[index]
            previous = relevant[index - 1]
            high = float(current["high"])
            low = float(current["low"])
            previous_high = float(previous["high"])
            previous_low = float(previous["low"])
            previous_close = float(previous["close"])
            true_range_total += max(
                high - low,
                abs(high - previous_close),
                abs(low - previous_close),
            )
            upward = high - previous_high
            downward = previous_low - low
            plus_dm_total += upward if upward > downward and upward > 0 else 0.0
            minus_dm_total += downward if downward > upward and downward > 0 else 0.0
        if true_range_total <= 0:
            continue
        latest_plus_di = 100.0 * plus_dm_total / true_range_total
        latest_minus_di = 100.0 * minus_dm_total / true_range_total
        denominator = latest_plus_di + latest_minus_di
        dx_values.append(
            0.0
            if denominator <= 0
            else 100.0 * abs(latest_plus_di - latest_minus_di) / denominator
        )
    if not dx_values:
        return math.nan, math.nan, math.nan
    return sum(dx_values) / len(dx_values), latest_plus_di, latest_minus_di


def _safe_globals() -> dict[str, Any]:
    builtins = {
        "abs": abs,
        "all": all,
        "any": any,
        "bool": bool,
        "dict": dict,
        "enumerate": enumerate,
        "float": float,
        "int": int,
        "len": len,
        "list": list,
        "max": max,
        "min": min,
        "range": range,
        "round": round,
        "sorted": sorted,
        "sum": sum,
        "tuple": tuple,
        "zip": zip,
    }
    return {
        "__builtins__": builtins,
        "adx": _adx,
        "atr": _atr,
        "ema": _ema,
        "exp": math.exp,
        "log": math.log,
        "rsi": _rsi,
        "sma": _sma,
        "sqrt": math.sqrt,
    }


def _set_worker_limits() -> None:
    if os.name != "posix":
        return
    try:
        import resource

        resource.setrlimit(resource.RLIMIT_CPU, (10, 10))
        resource.setrlimit(resource.RLIMIT_AS, (512 * 1024 * 1024, 512 * 1024 * 1024))
        resource.setrlimit(resource.RLIMIT_FSIZE, (0, 0))
        resource.setrlimit(resource.RLIMIT_NOFILE, (16, 16))
    except (ImportError, OSError, ValueError):
        pass


def _worker_main() -> int:
    _set_worker_limits()
    try:
        payload = json.loads(sys.stdin.buffer.read(MAX_INPUT_BYTES))
        source = payload["source_code"]
        contexts = payload["contexts"]
        params = payload["params"]
        validate_source(source)
        namespace = _safe_globals()
        exec(compile(source, "<strategy>", "exec"), namespace, namespace)  # noqa: S102
        evaluate = namespace["evaluate"]
        output = [evaluate(context, params) for context in contexts]
        sys.stdout.write(json.dumps(output, ensure_ascii=False, allow_nan=False, separators=(",", ":")))
        return 0
    except BaseException as exc:  # Worker must convert every strategy failure to a redacted error.
        sys.stderr.write(f"策略执行失败：{type(exc).__name__}")
        return 1


if __name__ == "__main__" and "--worker" in sys.argv:
    raise SystemExit(_worker_main())
