"""Deterministic news scoring boundary; model text is evidence, not authority."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from ... import macro_market
from ...news_ai import news_stock_relation_supported
from .contracts import AiMonitorAuthority, AiMonitorStageResult

NEWS_EVENT_BURST_SECONDS = 2 * 60
NEWS_EVENT_BURST_MAX_SPAN_SECONDS = 10 * 60
INDICATOR_MATCH_POLICY = "grouped_weighted_v1"
MULTI_TIMEFRAME_TECHNICAL_VERSION = "directional_mtf_15m_1h_4h_v3"
MULTI_TIMEFRAME_TECHNICAL_WEIGHTS = {"15m": 0.25, "1h": 0.45, "4h": 0.30}
INDICATOR_GROUPS: dict[str, frozenset[str]] = {
    "trend": frozenset(
        {"moving_average_bull", "ma_golden_cross", "prediction_trend"}
    ),
    "breakout": frozenset(
        {
            "bollinger_breakout",
            "trend_breakout",
            "price_volume_rise",
            "strong_gap_open",
            "macd_golden_cross_volume",
        }
    ),
    "pullback": frozenset(
        {"moving_average_pullback_bounce", "low_volume_pullback"}
    ),
    "reversal": frozenset(
        {"new_low_reversal", "oversold_bounce", "oversold_reversal"}
    ),
    "market_flow": frozenset(
        {
            "prediction_aggressive_flow",
            "prediction_book_imbalance",
            "prediction_book_imbalance_5",
            "prediction_velocity",
            "prediction_flash_imbalance",
            "prediction_taker_flow",
            "prediction_price_oi_impulse",
        }
    ),
}
CORE_INDICATOR_GROUPS = frozenset({"trend", "breakout", "pullback", "reversal"})
NON_BLOCKING_INDICATOR_KEYS = frozenset(
    {"prediction_aggressive_flow", "prediction_velocity"}
)


def indicator_group(key: str) -> str:
    normalized = str(key).strip().lower()
    for group, keys in INDICATOR_GROUPS.items():
        if normalized in keys:
            return group
    return "other"


def configured_indicator_policy(
    evidence: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Evaluate selected indicators as alternative market-regime groups."""

    grouped: dict[str, list[Mapping[str, Any]]] = {}
    for item in evidence:
        grouped.setdefault(str(item.get("group") or "other"), []).append(item)

    group_results: list[dict[str, Any]] = []
    core_available_count = 0
    core_matched_count = 0
    for group, items in grouped.items():
        available = [item for item in items if bool(item.get("available"))]
        matched = [item for item in available if bool(item.get("matched"))]
        is_core = group in CORE_INDICATOR_GROUPS or group == "other"
        if is_core:
            core_available_count += len(available)
            core_matched_count += len(matched)
        required = min(2, len(available)) if is_core else 0
        strength_source = (
            matched
            if is_core and required and len(matched) >= required
            else available
        )
        ranked_strengths = sorted(
            (float(item.get("strength") or 0.0) for item in strength_source),
            reverse=True,
        )
        score_width = required or min(2, len(ranked_strengths))
        score = (
            round(sum(ranked_strengths[:score_width]) / score_width, 4)
            if score_width
            else None
        )
        group_results.append(
            {
                "key": group,
                "selected_count": len(items),
                "available_count": len(available),
                "matched_count": len(matched),
                "required_count": required,
                "passed": bool(is_core and required and len(matched) >= required),
                "score": score,
                "blocking": is_core,
            }
        )

    minimum_core_matches = min(2, core_available_count) if core_available_count else 0
    passed_groups = [
        item
        for item in group_results
        if bool(item["blocking"]) and bool(item["passed"])
    ]
    passed = bool(
        minimum_core_matches
        and core_matched_count >= minimum_core_matches
        and passed_groups
    )
    scored_groups = [
        item
        for item in (passed_groups or group_results)
        if bool(item["blocking"]) and item["score"] is not None
    ]
    technical_score = max(
        (float(item["score"]) for item in scored_groups),
        default=0.0,
    )
    return {
        "version": INDICATOR_MATCH_POLICY,
        "passed": passed,
        "technical_score": round(technical_score, 4),
        "minimum_core_matches": minimum_core_matches,
        "core_available_count": core_available_count,
        "core_matched_count": core_matched_count,
        "passed_groups": [str(item["key"]) for item in passed_groups],
        "groups": group_results,
        "non_blocking_keys": sorted(NON_BLOCKING_INDICATOR_KEYS),
    }


def match_configured_indicators(
    scan: Mapping[str, Any],
    indicator_keys: Sequence[str],
    direction: str = "long",
) -> tuple[bool, list[dict[str, Any]]]:
    """Apply grouped indicator policy using the requested signal direction."""

    expected_direction = "bearish" if direction == "short" else "bullish"
    items = [
        *list(scan.get("items") or []),
        *list((scan.get("prediction_features") or {}).get("items") or []),
    ]
    by_key = {
        str(item.get("key") or ""): item
        for item in items
        if isinstance(item, Mapping)
    }
    evidence: list[dict[str, Any]] = []
    for key in indicator_keys:
        item = by_key.get(key)
        status = str(item.get("status") or "") if item else "unavailable"
        available = bool(
            item
            and item.get("available", True) is not False
            and status not in {"insufficient", "unavailable"}
        )
        matched = bool(
            available
            and (
                item.get("direction") == expected_direction
                if key.startswith("prediction_")
                else (
                    item.get("bearish_triggered") is True
                    if direction == "short"
                    else item.get("bullish_triggered", item.get("triggered")) is True
                )
            )
        )
        bearish_strategy = bool(
            item and direction == "short" and not key.startswith("prediction_")
        )
        strength_key = "bearish_strength" if direction == "short" else "bullish_strength"
        raw_strength = item.get(strength_key) if item else None
        strength = (
            float(raw_strength)
            if raw_strength is not None
            else 100.0
            if matched
            else 0.0
        )
        evidence.append(
            {
                "key": key,
                "name": (
                    item.get("bearish_name") if bearish_strategy else item.get("name")
                )
                if item
                else key,
                "matched": matched,
                "available": available,
                "group": indicator_group(key),
                "blocking": indicator_group(key) in CORE_INDICATOR_GROUPS
                and key not in NON_BLOCKING_INDICATOR_KEYS,
                "strength": round(max(0.0, min(100.0, strength)), 4),
                "direction": direction,
                "status": (
                    item.get("bearish_status")
                    if bearish_strategy
                    else item.get("status")
                )
                if item
                else "unavailable",
                "summary": (
                    item.get("bearish_summary")
                    if bearish_strategy
                    else item.get("summary")
                )
                if item
                else "指标数据不可用",
                "metrics": list(item.get("metrics") or []) if item else [],
            }
        )
    policy = configured_indicator_policy(evidence)
    return bool(policy["passed"]), evidence


def _row_value(row: Any, name: str, default: Any = None) -> Any:
    if isinstance(row, Mapping):
        return row.get(name, default)
    return getattr(row, name, default)


def _json_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except (TypeError, ValueError, json.JSONDecodeError):
            return []
        return decoded if isinstance(decoded, list) else []
    return []


def news_event_bursts(
    news_items: Sequence[Mapping[str, Any]],
    *,
    maximum_gap_seconds: int = NEWS_EVENT_BURST_SECONDS,
    maximum_span_seconds: int = NEWS_EVENT_BURST_MAX_SPAN_SECONDS,
) -> list[list[Mapping[str, Any]]]:
    """Cluster same-source/category wire updates without mutating source records."""

    grouped: dict[str, list[Mapping[str, Any]]] = {}
    for index, item in enumerate(news_items):
        source = str(item.get("source") or "").strip().casefold()
        category = str(item.get("category") or "").strip().casefold()
        item_id = str(item.get("id") or index).strip()
        group_key = f"{source}|{category}" if source else f"id:{item_id}"
        grouped.setdefault(group_key, []).append(item)

    bursts: list[list[Mapping[str, Any]]] = []
    for items in grouped.values():
        ordered = sorted(
            items,
            key=lambda item: (int(item.get("ts") or 0), str(item.get("id") or "")),
        )
        current: list[Mapping[str, Any]] = []
        previous_ts = 0
        first_ts = 0
        for item in ordered:
            item_ts = int(item.get("ts") or 0)
            if current and (
                previous_ts <= 0
                or item_ts <= 0
                or item_ts - previous_ts > max(0, int(maximum_gap_seconds))
                or item_ts - first_ts > max(0, int(maximum_span_seconds))
            ):
                bursts.append(current)
                current = []
                first_ts = 0
            if not current:
                first_ts = item_ts
            current.append(item)
            previous_ts = item_ts
        if current:
            bursts.append(current)
    return bursts


def news_event_cluster_id(
    news_ids: Sequence[Any] | None,
    *,
    fallback: Any,
    direction: str = "",
) -> str:
    """Build a stable research-sample key from frozen trigger-news identities."""

    normalized = sorted(
        {str(item).strip() for item in (news_ids or []) if str(item).strip()}
    )
    if not normalized:
        return f"prediction:{str(fallback)}"
    seed = "\0".join(normalized)
    if str(direction).strip():
        seed = f"{str(direction).strip().lower()}\0{seed}"
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]


def resolved_news_event_cluster_id(
    stored_cluster_id: Any,
    news_ids: Sequence[Any] | None,
    *,
    fallback: Any,
    direction: str = "",
) -> str:
    """Prefer the immutable signal-time cluster id, with legacy-news fallback."""

    value = stored_cluster_id
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except (TypeError, ValueError, json.JSONDecodeError):
            decoded = value
        value = decoded if isinstance(decoded, (str, int)) else value
    normalized = str(value or "").strip().strip('"')
    if normalized:
        return normalized
    return news_event_cluster_id(news_ids, fallback=fallback, direction=direction)


def aggregate_news_candidates(
    news_rows: Sequence[Any],
    symbol_map: Mapping[str, str],
    *,
    minimum_confidence: float,
    minimum_mentions: int,
) -> list[dict[str, Any]]:
    """Build direction-aware US-stock candidates, including both news sides."""

    candidates: dict[tuple[str, str], dict[str, Any]] = {}
    for row in news_rows:
        confidence = float(_row_value(row, "ai_confidence", 0) or 0)
        if confidence <= 0:
            continue
        for related in _json_list(_row_value(row, "related_us_stocks")):
            if not isinstance(related, Mapping):
                continue
            raw_direction = str(related.get("direction") or "").strip().lower()
            if raw_direction in {"bull", "bullish", "long"}:
                direction = "long"
            elif raw_direction in {"bear", "bearish", "short"}:
                direction = "short"
            else:
                continue
            raw_symbol = str(related.get("symbol") or "").strip().upper()
            normalized = raw_symbol.removesuffix("USDT").removesuffix("USD1")
            if not normalized:
                continue
            contract = symbol_map.get(normalized, "")
            try:
                relevance = min(1.0, max(0.0, float(related.get("relevance") or 0)))
            except (TypeError, ValueError):
                continue
            if not news_stock_relation_supported(row, normalized, relevance):
                continue
            score = confidence * relevance
            if score < minimum_confidence:
                continue
            candidate = candidates.setdefault(
                (normalized, direction),
                {
                    "symbol": normalized,
                    "contract_symbol": contract,
                    "market_available": bool(contract),
                    "direction": direction,
                    "scores": [],
                    "news": [],
                },
            )
            candidate["scores"].append(score)
            candidate["news"].append(
                {
                    "id": str(_row_value(row, "id") or ""),
                    "ts": int(_row_value(row, "ts", 0) or 0),
                    "source": _row_value(row, "source"),
                    "title": _row_value(row, "title_zh")
                    or _row_value(row, "title")
                    or "",
                    "reason": _row_value(row, "ai_reason"),
                    "confidence": round(confidence, 4),
                    "relevance": round(relevance, 4),
                    "score": round(score, 4),
                    "direction": direction,
                    "memory_effect": related.get("memory_effect"),
                    "memory_reason": related.get("memory_reason"),
                    "position_effect": related.get("position_effect"),
                    "position_reason": related.get("position_reason"),
                    "category": _row_value(row, "ai_category"),
                }
            )
    result: list[dict[str, Any]] = []
    for candidate in candidates.values():
        unique_news = {item["id"] for item in candidate["news"] if item["id"]}
        if len(unique_news) < minimum_mentions:
            continue
        candidate.pop("scores")
        event_bursts = news_event_bursts(candidate["news"])
        event_scores: list[float] = []
        for burst in event_bursts:
            burst_scores = [float(item.get("score") or 0) for item in burst]
            if burst_scores:
                event_scores.append(sum(burst_scores) / len(burst_scores))
            cluster_id = news_event_cluster_id(
                [item.get("id") for item in burst],
                fallback=burst[0].get("id") if burst else candidate["symbol"],
                direction=str(candidate["direction"]),
            )
            for item in burst:
                item["event_cluster_id"] = cluster_id
        candidate["event_count"] = len(event_bursts)
        candidate["news_score"] = round(
            (sum(event_scores) / len(event_scores) if event_scores else 0) * 100,
            4,
        )
        candidate["news"].sort(
            key=lambda item: (item["score"], item["ts"]), reverse=True
        )
        result.append(candidate)
    return sorted(result, key=lambda item: item["news_score"], reverse=True)


_NON_ACTIONABLE_RECAP_PATTERNS = (
    re.compile(r"美股.{0,4}收盘|收盘综述|盘后复盘|收盘\s*[：:]"),
    re.compile(r"(?:收涨|收跌|尾盘)(?:[^。；;]{0,28})(?:%|％|点|美元)"),
    re.compile(
        r"\b(?:market close|closing bell|post[- ]market recap|stocks? close[sd]?|"
        r"closed (?:up|down)|ends? (?:up|down))\b",
        re.IGNORECASE,
    ),
)


def news_actionability_snapshot(news_item: Mapping[str, Any]) -> dict[str, Any]:
    """Classify whether a news item can initiate a new prediction."""

    title = str(news_item.get("title") or "").strip()
    reason = str(news_item.get("reason") or "").strip()
    text = " ".join(part for part in (title, reason) if part)
    matched_pattern = next(
        (
            pattern.pattern
            for pattern in _NON_ACTIONABLE_RECAP_PATTERNS
            if pattern.search(text)
        ),
        None,
    )
    return {
        "version": "news_actionability_v1",
        "actionable": matched_pattern is None,
        "reason_code": "CLOSING_RECAP_NOT_A_CATALYST" if matched_pattern else None,
        "matched_pattern": matched_pattern,
    }


def multi_timeframe_technical_snapshot(
    scans: Mapping[str, Mapping[str, Any]],
    indicator_keys: Sequence[str],
    *,
    direction: str,
) -> dict[str, Any]:
    """Evaluate one direction across 15m timing, 1h trend and 4h regime."""

    normalized_direction = "short" if direction == "short" else "long"
    frames: dict[str, dict[str, Any]] = {}
    for timeframe, weight in MULTI_TIMEFRAME_TECHNICAL_WEIGHTS.items():
        scan = dict(scans.get(timeframe) or {})
        matched, evidence = match_configured_indicators(
            scan,
            indicator_keys,
            normalized_direction,
        )
        policy = configured_indicator_policy(evidence)
        available = any(bool(item.get("available")) for item in evidence)
        frames[timeframe] = {
            "timeframe": timeframe,
            "role": {
                "15m": "entry_timing",
                "1h": "primary_trend",
                "4h": "regime_filter",
            }[timeframe],
            "weight": weight,
            "available": available,
            "passed": bool(matched),
            "technical_score": float(policy.get("technical_score") or 0.0),
            "matched_indicator_keys": [
                str(item.get("key")) for item in evidence if item.get("matched")
            ],
            "passed_groups": list(policy.get("passed_groups") or []),
            "evaluated_at": int(scan.get("evaluated_at") or 0),
            "policy": policy,
            "indicators": evidence,
        }

    passed_frames = [item for item in frames.values() if item["passed"]]
    score_source = passed_frames or [
        item for item in frames.values() if item["available"]
    ]
    score_weight = sum(float(item["weight"]) for item in score_source)
    technical_score = (
        sum(
            float(item["technical_score"]) * float(item["weight"])
            for item in score_source
        )
        / score_weight
        if score_weight
        else 0.0
    )
    structure_confirmed = bool(frames["4h"]["passed"])
    timing_confirmed = bool(frames["15m"]["passed"])
    trend_confirmed = bool(frames["1h"]["passed"])
    eligible = bool(structure_confirmed and timing_confirmed)
    return {
        "version": MULTI_TIMEFRAME_TECHNICAL_VERSION,
        "direction": normalized_direction,
        "technical_score": round(technical_score, 4),
        "eligible": eligible,
        "structure_confirmed": structure_confirmed,
        "timing_confirmed": timing_confirmed,
        "trend_confirmed": trend_confirmed,
        "confirmed_timeframes": [
            timeframe for timeframe, item in frames.items() if item["passed"]
        ],
        "available_timeframes": [
            timeframe for timeframe, item in frames.items() if item["available"]
        ],
        "frames": frames,
        "rule": "15m 确认入场时机，4h 确认大级别方向；1h 用于趋势加分与冲突校验。",
    }


def select_directional_candidates_with_technical_context(
    candidates: Sequence[Mapping[str, Any]],
    scans_by_contract: Mapping[str, Mapping[str, Mapping[str, Any]]],
    indicator_keys: Sequence[str],
    *,
    market_direction: str = "neutral",
    market_tide: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Choose one auditable side after evaluating both technical directions."""

    contexts: dict[tuple[str, str], dict[str, Any]] = {}

    def context(contract_symbol: str, direction: str) -> dict[str, Any]:
        key = (contract_symbol, direction)
        if key not in contexts:
            contexts[key] = multi_timeframe_technical_snapshot(
                scans_by_contract.get(contract_symbol, {}),
                indicator_keys,
                direction=direction,
            )
        return contexts[key]

    grouped: dict[str, list[dict[str, Any]]] = {}
    for raw in candidates:
        candidate = dict(raw)
        symbol_key = str(
            candidate.get("contract_symbol") or candidate.get("symbol") or ""
        ).upper()
        if symbol_key:
            grouped.setdefault(symbol_key, []).append(candidate)

    selected: list[dict[str, Any]] = []
    normalized_market_direction = str(market_direction or "neutral").lower()
    tide_signal = macro_market.market_tide_directional_signal(market_tide)
    tide_direction = str(tide_signal.get("direction") or "neutral")
    tide_strength = 10.0 if tide_signal.get("strong") else 6.0
    for symbol_key, alternatives in grouped.items():
        explicit_by_direction: dict[str, dict[str, Any]] = {}
        for candidate in alternatives:
            direction = "short" if candidate.get("direction") == "short" else "long"
            current = explicit_by_direction.get(direction)
            if current is None or float(candidate.get("news_score") or 0.0) > float(
                current.get("news_score") or 0.0
            ):
                explicit_by_direction[direction] = candidate
        strongest_explicit = max(
            explicit_by_direction.values(),
            key=lambda item: float(item.get("news_score") or 0.0),
        )
        directional_alternatives: list[dict[str, Any]] = []
        for direction in ("long", "short"):
            explicit = explicit_by_direction.get(direction)
            if explicit is not None:
                candidate = dict(explicit)
                candidate["direction_origin"] = "explicit_news"
                candidate["source_news_direction"] = direction
                candidate["synthetic_direction"] = False
            else:
                candidate = dict(strongest_explicit)
                candidate["direction"] = direction
                candidate["direction_origin"] = "technical_regime_override"
                candidate["source_news_direction"] = str(
                    strongest_explicit.get("direction") or "long"
                )
                candidate["synthetic_direction"] = True
            directional_alternatives.append(candidate)

        ranked: list[tuple[tuple[float, float, int], dict[str, Any]]] = []
        selection_rows: list[dict[str, Any]] = []
        for candidate in directional_alternatives:
            direction = "short" if candidate.get("direction") == "short" else "long"
            opposite_direction = "long" if direction == "short" else "short"
            technical = dict(context(symbol_key, direction))
            opposite_technical = context(symbol_key, opposite_direction)
            one_hour_conflict = bool(
                not dict(technical.get("frames") or {}).get("1h", {}).get("passed")
                and dict(opposite_technical.get("frames") or {})
                .get("1h", {})
                .get("passed")
            )
            technical["eligible_before_1h_conflict"] = bool(
                technical.get("eligible")
            )
            technical["one_hour_conflict"] = one_hour_conflict
            technical["eligible"] = bool(
                technical.get("eligible") and not one_hour_conflict
            )
            contexts[(symbol_key, direction)] = technical
            news_score = float(candidate.get("news_score") or 0.0)
            explicit_news_direction = not bool(candidate.get("synthetic_direction"))
            directional_news_score = (
                news_score
                if explicit_news_direction
                else max(0.0, 100.0 - news_score)
            )
            macro_bias = (
                4.0
                if (direction == "long" and normalized_market_direction == "bull")
                or (direction == "short" and normalized_market_direction == "bear")
                else -4.0
                if (direction == "long" and normalized_market_direction == "bear")
                or (direction == "short" and normalized_market_direction == "bull")
                else 0.0
            )
            tide_bias = (
                tide_strength
                if tide_signal.get("available")
                and (
                    (direction == "long" and tide_direction == "bull")
                    or (direction == "short" and tide_direction == "bear")
                )
                else -tide_strength
                if tide_signal.get("available")
                and (
                    (direction == "long" and tide_direction == "bear")
                    or (direction == "short" and tide_direction == "bull")
                )
                else 0.0
            )
            ranking_score = (
                directional_news_score * 0.40
                + float(technical["technical_score"]) * 0.55
                + (5.0 if technical["eligible"] else 0.0)
                + macro_bias
                + tide_bias
            )
            if not explicit_news_direction and not bool(technical["eligible"]):
                ranking_score = -1.0
            latest_news = max(
                (int(item.get("ts") or 0) for item in candidate.get("news") or []),
                default=0,
            )
            selection_rows.append(
                {
                    "direction": direction,
                    "news_score": round(news_score, 4),
                    "directional_news_score": round(directional_news_score, 4),
                    "news_direction_explicit": explicit_news_direction,
                    "direction_origin": candidate.get("direction_origin"),
                    "technical_score": technical["technical_score"],
                    "technical_eligible": technical["eligible"],
                    "one_hour_conflict": one_hour_conflict,
                    "confirmed_timeframes": list(technical["confirmed_timeframes"]),
                    "macro_bias": macro_bias,
                    "market_tide_bias": tide_bias,
                    "ranking_score": round(ranking_score, 4),
                }
            )
            ranked.append(((ranking_score, news_score, latest_news), candidate))
        _, winner = max(ranked, key=lambda item: item[0])
        winner_direction = "short" if winner.get("direction") == "short" else "long"
        opposite_direction = "long" if winner_direction == "short" else "short"
        winner["multi_timeframe_technical"] = context(symbol_key, winner_direction)
        winner["opposite_multi_timeframe_technical"] = context(
            symbol_key, opposite_direction
        )
        winner["direction_selection"] = {
            "version": MULTI_TIMEFRAME_TECHNICAL_VERSION,
            "selected_direction": winner_direction,
            "direction_origin": winner.get("direction_origin"),
            "source_news_direction": winner.get("source_news_direction"),
            "news_direction_aligned": not bool(winner.get("synthetic_direction")),
            "combined_score_adjustment": (
                -5.0 if bool(winner.get("synthetic_direction")) else 0.0
            ),
            "market_direction": normalized_market_direction,
            "market_tide": tide_signal,
            "alternatives": sorted(
                selection_rows,
                key=lambda item: float(item["ranking_score"]),
                reverse=True,
            ),
            "weights": {"news_direction": 0.40, "technical": 0.55},
            "technical_eligibility_bonus": 5.0,
            "macro_alignment_bonus": 4.0,
            "market_tide_alignment_bonus": tide_strength,
        }
        selected.append(winner)
    return sorted(
        selected,
        key=lambda item: max(
            (
                float(row.get("ranking_score") or 0.0)
                for row in dict(item.get("direction_selection") or {}).get(
                    "alternatives", []
                )
            ),
            default=0.0,
        ),
        reverse=True,
    )


class NewsScoringService:
    def __init__(
        self,
        *,
        aggregate: Callable[..., list[dict[str, Any]]],
        select_directional: Callable[..., list[dict[str, Any]]],
        version: str,
    ) -> None:
        self._aggregate = aggregate
        self._select_directional = select_directional
        self._version = version

    def aggregate(
        self,
        news_rows: Sequence[Any],
        symbol_map: Mapping[str, str],
        *,
        minimum_confidence: float,
        minimum_mentions: int,
    ) -> AiMonitorStageResult:
        candidates = self._aggregate(
            news_rows,
            symbol_map,
            minimum_confidence=minimum_confidence,
            minimum_mentions=minimum_mentions,
        )
        return AiMonitorStageResult(
            stage="news_scoring",
            authority=AiMonitorAuthority.DETERMINISTIC,
            version=self._version,
            payload={"candidates": candidates},
        )

    def select_directional(self, *args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        return self._select_directional(*args, **kwargs)
