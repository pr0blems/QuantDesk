"""Conservative, auditable multi-round news intelligence.

This module deliberately separates fact verification, symbol-specific impact,
market confirmation and trading eligibility.  It never places orders and its
features are shadow-only until forward outcomes are calibrated.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import time
import urllib.parse
import uuid
from dataclasses import dataclass
from typing import Any

from . import store
from .macro_events import extract_structured_event

ASSESSOR = "deterministic-evidence-gate"
ASSESSOR_VERSION = 1
SHADOW_FEATURE_VERSION = 1
RECHECK_SECONDS = 300
MAX_EVENT_AGE_SECONDS = 48 * 3600
OUTCOME_HORIZONS = (900, 3600, 14_400)
MIN_BASE_RATE_SAMPLES = 20

PRIMARY_DOMAINS = {
    "sec.gov",
    "www.sec.gov",
    "federalreserve.gov",
    "www.federalreserve.gov",
    "bls.gov",
    "www.bls.gov",
    "bea.gov",
    "www.bea.gov",
    "eia.gov",
    "www.eia.gov",
    "hkexnews.hk",
    "www.hkexnews.hk",
}
TIER_A_DOMAINS = {
    "reuters.com",
    "www.reuters.com",
    "bloomberg.com",
    "www.bloomberg.com",
    "ft.com",
    "www.ft.com",
    "wsj.com",
    "www.wsj.com",
    "cnbc.com",
    "www.cnbc.com",
}
TIER_C_DOMAINS = {
    "cointelegraph.com",
    "www.coindesk.com",
    "cryptobriefing.com",
    "cryptonews.com",
    "newsbtc.com",
}
RUMOR_WORDS = {
    "rumor",
    "reportedly",
    "considering",
    "may",
    "might",
    "could",
    "sources say",
    "传闻",
    "据悉",
    "考虑",
    "或将",
}
DENIAL_WORDS = {
    "denies",
    "denied",
    "false report",
    "not true",
    "correction",
    "withdraws",
    "cancels",
    "否认",
    "不实",
    "更正",
    "撤回",
    "取消",
}
POSITIVE_WORDS = {
    "beats",
    "beat estimates",
    "raises guidance",
    "record high",
    "approved",
    "approval",
    "wins contract",
    "win contract",
    "acquires",
    "surges",
    "rallies",
    "upgrade",
    "outperform",
    "buy rating",
    "增持",
    "获批",
    "上调指引",
    "超预期",
    "中标",
}
NEGATIVE_WORDS = {
    "misses",
    "miss estimates",
    "cuts guidance",
    "downgrade",
    "underperform",
    "sell rating",
    "lawsuit",
    "injunction",
    "investigation",
    "recall",
    "bankruptcy",
    "fraud",
    "fine",
    "sanctions",
    "data breach",
    "下调指引",
    "不及预期",
    "诉讼",
    "调查",
    "召回",
    "破产",
    "罚款",
}
EVENT_RULES = (
    ("earnings", ("earnings", "revenue", "profit", "eps", "财报", "营收", "利润")),
    ("guidance", ("guidance", "outlook", "指引", "展望")),
    ("regulatory", ("sec ", "regulator", "lawsuit", "investigation", "监管", "诉讼")),
    ("monetary_policy", ("fomc", "federal reserve", "interest rate", "美联储", "利率")),
    ("macro_release", ("cpi", "payroll", "inflation", "gdp", "非农", "通胀")),
    ("corporate_action", ("acquisition", "merger", "buyback", "dividend", "收购", "回购")),
    ("product", ("launch", "release", "approval", "推出", "发布", "获批")),
)
INVERSE_BASES = {"SQQQ", "SOXS", "TZA", "TBT"}
EXPOSURE_BASES = {
    "BITO",
    "EWJ",
    "EWY",
    "EWZ",
    "EWT",
    "IWM",
    "KORU",
    "SMH",
    "SOXL",
    "SOXS",
    "SPY",
    "SQQQ",
    "TBT",
    "TMF",
    "TQQQ",
    "TZA",
    "URNM",
    "UVXY",
    "XLE",
}
AMBIGUOUS_BASES = {
    "APP",
    "BE",
    "AI",
    "IT",
    "NOW",
    "ON",
    "OPEN",
    "PATH",
    "REAL",
    "ROOT",
}
ENTITY_ALIAS_OVERRIDES = {
    "AI": ["c3.ai", "c3 ai"],
    "APP": ["applovin"],
    "BE": ["bloom energy"],
    "NOW": ["servicenow"],
    "ON": ["on semiconductor", "onsemi"],
    "OPEN": ["opendoor technologies", "opendoor"],
    "PATH": ["uipath"],
    "REAL": ["the realreal"],
    "ROOT": ["root insurance"],
    "ZHIPU": ["zhipu", "z.ai", "z ai", "智谱"],
}
TOKEN_RE = re.compile(r"[a-z0-9]+|[\u3400-\u9fff]+", re.IGNORECASE)
NUMBER_RE = re.compile(
    r"(?P<value>[+-]?\d+(?:\.\d+)?)\s*(?P<unit>%|bp|bps|basis points?|million|billion|trillion|亿|万)?",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class EntityAssessment:
    symbol: str
    entity_name: str
    direction: str
    impact_score: float
    impact_confidence: float
    directness: float
    relationship_type: str
    horizons: tuple[str, ...]
    evidence: tuple[str, ...]


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _number(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if math.isfinite(result) else default


def _clip(value: float, lower: float = 0.0, upper: float = 1.0) -> float:
    return min(upper, max(lower, value))


def canonical_url(url: str | None) -> str:
    if not url:
        return ""
    try:
        parsed = urllib.parse.urlsplit(url)
    except ValueError:
        return ""
    query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=False)
    query = [(key, value) for key, value in query if not key.lower().startswith("utm_")]
    return urllib.parse.urlunsplit(
        (parsed.scheme.lower(), parsed.netloc.lower(), parsed.path.rstrip("/"), urllib.parse.urlencode(query), "")
    )


def source_domain(url: str | None) -> str:
    try:
        return (urllib.parse.urlsplit(url or "").hostname or "").lower()
    except ValueError:
        return ""


def source_tier(source: str | None, url: str | None) -> tuple[str, float]:
    domain = source_domain(url)
    source_name = (source or "").lower()
    if domain in PRIMARY_DOMAINS or any(
        marker in source_name for marker in ("sec filings", "federal reserve", "hkexnews", "bls")
    ):
        return "S", 0.99
    if domain in TIER_A_DOMAINS or any(
        marker in source_name for marker in ("reuters", "bloomberg", "financial times", "wall street journal")
    ):
        return "A", 0.88
    if domain in TIER_C_DOMAINS:
        return "C", 0.48
    return "B", 0.68


def normalize_title(title: str) -> str:
    tokens = TOKEN_RE.findall((title or "").casefold())
    noise = {"breaking", "update", "exclusive", "live", "最新", "快讯"}
    return " ".join(token for token in tokens if token not in noise)[:1200]


def title_fingerprint(title: str) -> str:
    return hashlib.sha256(normalize_title(title).encode("utf-8")).hexdigest()


def _token_set(text: str) -> set[str]:
    return {token for token in TOKEN_RE.findall(text.casefold()) if len(token) > 1}


def title_similarity(left: str, right: str) -> float:
    a, b = _token_set(left), _token_set(right)
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _contains(text: str, phrase: str) -> bool:
    phrase = phrase.strip().casefold()
    if not phrase:
        return False
    haystack = text.casefold()
    if re.fullmatch(r"[a-z0-9][a-z0-9 .&'/-]*", phrase):
        pattern = r"(?<![a-z0-9])" + re.escape(phrase) + r"(?![a-z0-9])"
        return re.search(pattern, haystack, re.IGNORECASE) is not None
    return phrase in haystack


def _valid_alias_context(text: str, alias: str) -> bool:
    """Reject ambiguous financial words without their required context."""

    value = alias.strip().casefold()
    context = text.casefold()
    if value in {"treasury", "美债", "国债"}:
        return any(
            _contains(context, marker)
            for marker in (
                "u.s. treasury",
                "us treasury",
                "treasury yield",
                "treasury bond",
                "government bond",
                "美债收益率",
                "美国国债",
            )
        )
    if value == "oil":
        return any(_contains(context, marker) for marker in ("crude oil", "wti", "brent"))
    if value == "arm":
        return _contains(context, "arm holdings") or re.search(r"\bArm\b", text) is not None
    return True


def _matched_aliases(text: str, aliases: list[str]) -> list[str]:
    return [alias for alias in aliases if _contains(text, alias) and _valid_alias_context(text, alias)]


def classify_event_type(text: str) -> str:
    for event_type, phrases in EVENT_RULES:
        if any(_contains(text, phrase) for phrase in phrases):
            return event_type
    return "other"


def claim_modality(text: str) -> str:
    if any(_contains(text, word) for word in DENIAL_WORDS):
        return "denied"
    if any(_contains(text, word) for word in RUMOR_WORDS):
        return "rumor"
    if any(_contains(text, word) for word in ("expects", "forecast", "will", "预计", "将")):
        return "forward_looking"
    return "actual"


def extract_claim(title: str, entities: list[str]) -> dict[str, Any]:
    match = NUMBER_RE.search(title)
    numeric_value = float(match.group("value")) if match else None
    unit = match.group("unit") if match and match.group("unit") else None
    event_type = classify_event_type(title)
    subject = ", ".join(entities[:3]) or title.split(":", 1)[0][:191]
    claim = {
        "subject": subject or "unknown",
        "predicate": event_type,
        "object": title[:2000],
        "numeric_value": numeric_value,
        "unit": unit,
        "modality": claim_modality(title),
        "evidence": title[:2000],
    }
    claim["hash"] = hashlib.sha256(_json(claim).encode("utf-8")).hexdigest()
    return claim


def _instrument_aliases() -> dict[str, list[str]]:
    from .report import KEYWORDS

    rows = store.query(
        "SELECT symbol FROM binance_contract_rules WHERE contract_type='TRADIFI_PERPETUAL' AND status='TRADING'"
    )
    aliases: dict[str, list[str]] = {}
    for row in rows:
        symbol = str(row["symbol"]).upper()
        base = symbol.removesuffix("USDT").removesuffix("USD1")
        configured = [*KEYWORDS.get(base, []), *ENTITY_ALIAS_OVERRIDES.get(base, [])]
        include_base = not configured and len(base) >= 4 and base not in AMBIGUOUS_BASES
        values = [*(configured or []), *([base] if include_base else [])]
        aliases[symbol] = list(dict.fromkeys(value.strip() for value in values if value.strip()))
    return aliases


def _direction_for_entity(title: str, aliases: list[str]) -> tuple[str, float, list[str]]:
    text = title.casefold()
    matched = _matched_aliases(title, aliases)
    if not matched:
        return "neutral", 0.0, []
    evidence: list[str] = [f"direct entity mention: {matched[0]}"]

    # Comparative statements need target-specific polarity rather than one
    # article-wide sentiment.  The entity before "over" is preferred.
    over_at = text.find(" over ")
    if over_at >= 0 and any(_contains(text, word) for word in ("favor", "favors", "prefer", "prefers")):
        alias_positions = [(text.find(alias.casefold()), alias) for alias in matched]
        if any(0 <= position < over_at for position, _ in alias_positions):
            evidence.append("preferred side of comparative statement")
            return "long", 0.72, evidence
        if any(position > over_at for position, _ in alias_positions):
            evidence.append("disfavored side of comparative statement")
            return "short", -0.72, evidence

    positive = [word for word in POSITIVE_WORDS if _contains(text, word)]
    negative = [word for word in NEGATIVE_WORDS if _contains(text, word)]
    denial = any(_contains(text, word) for word in DENIAL_WORDS)
    if denial:
        evidence.append("denial/correction language requires contradiction review")
        return "conflicted", 0.0, evidence
    against_at = text.find(" against ")
    if against_at >= 0 and any(
        _contains(text, word) for word in ("lawsuit", "injunction", "investigation")
    ):
        positions = [text.find(alias.strip().casefold()) for alias in matched]
        if any(position > against_at for position in positions):
            evidence.append("target of adverse legal action")
            return "short", -0.76, evidence
        if any(0 <= position < against_at for position in positions):
            evidence.append("initiator of legal action; no automatic positive inference")
            return "neutral", 0.0, evidence
    if positive and not negative:
        evidence.append(f"positive event phrase: {sorted(positive)[0]}")
        return "long", min(0.85, 0.55 + 0.08 * len(positive)), evidence
    if negative and not positive:
        evidence.append(f"negative event phrase: {sorted(negative)[0]}")
        return "short", -min(0.85, 0.55 + 0.08 * len(negative)), evidence
    if positive and negative:
        evidence.append("positive and negative evidence conflict")
        return "conflicted", 0.0, evidence
    return "neutral", 0.0, evidence


def assess_entities(title: str, event_type: str, aliases: dict[str, list[str]]) -> list[EntityAssessment]:
    results: list[EntityAssessment] = []
    horizons = {
        "earnings": ("15m", "1h", "4h"),
        "guidance": ("1h", "4h", "1d"),
        "regulatory": ("15m", "1h", "4h"),
        "monetary_policy": ("5m", "15m", "1h"),
        "macro_release": ("5m", "15m", "1h"),
        "corporate_action": ("1h", "4h", "1d"),
        "product": ("1h", "4h"),
    }.get(event_type, ("15m", "1h"))
    for symbol, symbol_aliases in aliases.items():
        matched = _matched_aliases(title, symbol_aliases)
        if not matched:
            continue
        direction, score, evidence = _direction_for_entity(title, symbol_aliases)
        base = symbol.removesuffix("USDT").removesuffix("USD1")
        relationship_type = "exposure" if base in EXPOSURE_BASES else "direct"
        directness = 0.62 if relationship_type == "exposure" else 0.98
        if base in INVERSE_BASES and direction in {"long", "short"}:
            direction = "short" if direction == "long" else "long"
            score *= -1
            evidence.append("direction inverted for inverse instrument")
        directional = direction in {"long", "short"}
        confidence = 0.74 if directional and event_type != "other" else 0.58 if directional else 0.30
        if relationship_type == "exposure":
            confidence = min(confidence, 0.58)
        if claim_modality(title) == "rumor":
            confidence = min(confidence, 0.42)
        results.append(
            EntityAssessment(
                symbol=symbol,
                entity_name=matched[0],
                direction=direction,
                impact_score=score,
                impact_confidence=confidence,
                directness=directness,
                relationship_type=relationship_type,
                horizons=horizons,
                evidence=tuple(evidence),
            )
        )
    return results


def _find_or_create_event(title: str, published_at: int) -> int:
    normalized = normalize_title(title)
    fingerprint = title_fingerprint(title)
    exact = store.query("SELECT id FROM news_event_clusters WHERE fingerprint=?", (fingerprint,))
    if exact:
        return int(exact[0]["id"])
    recent = store.query(
        "SELECT id,canonical_title FROM news_event_clusters WHERE last_seen_at>=? ORDER BY last_seen_at DESC LIMIT 200",
        (published_at - 12 * 3600,),
    )
    for row in recent:
        if title_similarity(normalized, str(row["canonical_title"])) >= 0.72:
            return int(row["id"])
    with store.transaction() as transaction:
        transaction.execute(
            """INSERT IGNORE INTO news_event_clusters(
                   public_id,fingerprint,canonical_title,event_type,state,revision,
                   first_seen_at,last_seen_at,independent_origins,contradiction_count,
                   quality_score,created_at,updated_at)
               VALUES(?,?,?,?, 'DETECTED',1,?,?,0,0,0,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)""",
            (
                str(uuid.uuid4()),
                fingerprint,
                normalized,
                classify_event_type(title),
                published_at,
                published_at,
            ),
        )
        created = transaction.query(
            "SELECT id FROM news_event_clusters WHERE fingerprint=?", (fingerprint,)
        )
    return int(created[0]["id"])


def _update_structured_event(
    event_id: int,
    event: Any,
    *,
    published_at: int,
    provenance: float,
    affected_symbols: list[str],
) -> None:
    """Persist structured fields without allowing weak headlines to overwrite facts.

    A field is only replaced by a non-null value.  This means a later generic
    syndicated headline cannot erase an official release's actual/consensus
    values, while a newer official release can update them naturally.
    """

    fields = event.as_dict()
    values = {
        "event_type": fields["event_type"],
        "scheduled_at": fields["scheduled_at"],
        "actual_at": fields["actual_at"],
        "actual_value": fields["actual_value"],
        "consensus_value": fields["consensus_value"],
        "surprise_value": fields["surprise_value"],
        "event_unit": fields["unit"],
        "affected_symbols_json": _json(sorted(set(affected_symbols))),
        "source_quality_score": round(max(0.0, min(1.0, provenance)), 8),
    }
    # MySQL's COALESCE keeps existing structured values when this document is
    # an ordinary follow-up article with no explicit release fields.
    store.execute(
        """UPDATE news_event_clusters SET
               event_type=CASE WHEN ? <> 'other' THEN ? ELSE event_type END,
               scheduled_at=COALESCE(?,scheduled_at), actual_at=COALESCE(?,actual_at),
               actual_value=COALESCE(?,actual_value), consensus_value=COALESCE(?,consensus_value),
               surprise_value=COALESCE(?,surprise_value), event_unit=COALESCE(?,event_unit),
               affected_symbols_json=CASE WHEN ? <> '[]' THEN ? ELSE affected_symbols_json END,
               source_quality_score=GREATEST(COALESCE(source_quality_score,0),?),
               updated_at=CURRENT_TIMESTAMP WHERE id=?""",
        (
            values["event_type"],
            values["event_type"],
            values["scheduled_at"],
            values["actual_at"],
            values["actual_value"],
            values["consensus_value"],
            values["surprise_value"],
            values["event_unit"],
            values["affected_symbols_json"],
            values["affected_symbols_json"],
            values["source_quality_score"],
            event_id,
        ),
    )


def _origin_key(title: str, domain: str, source: str) -> str:
    # An identical headline syndicated by many sites is one origin until its
    # lineage can prove otherwise. Different independently written headlines
    # retain their source-domain origin.
    normalized = normalize_title(title)
    headline_hash = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:32]
    if normalized:
        return f"headline:{headline_hash}"
    return f"source:{domain or source.casefold()}"[:191]


def process_document(row: dict[str, Any], aliases: dict[str, list[str]] | None = None) -> int:
    news_id = str(row["id"])
    existing = store.query("SELECT event_id FROM news_documents WHERE news_id=?", (news_id,))
    if existing:
        return int(existing[0]["event_id"])
    title = str(row.get("title") or "").strip()
    published_at = int(row.get("ts") or 0)
    event_id = _find_or_create_event(title, published_at)
    url = canonical_url(row.get("link"))
    domain = source_domain(url)
    tier, provenance = source_tier(row.get("source"), url)
    origin = _origin_key(title, domain, str(row.get("source") or ""))
    content_hash = hashlib.sha256(title.encode("utf-8")).hexdigest()
    now = int(time.time())
    store.execute(
        """INSERT IGNORE INTO news_documents(
               news_id,event_id,canonical_url,source_domain,origin_key,content_hash,
               source_tier,published_at,ingested_at,provenance_score,lineage_json)
           VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
        (
            news_id,
            event_id,
            url,
            domain,
            origin,
            content_hash,
            tier,
            published_at,
            now,
            provenance,
            _json(
                {
                    "declared_source": row.get("source"),
                    "canonical_domain": domain,
                    "source_tier": tier,
                    "provenance_score": provenance,
                }
            ),
        ),
    )
    aliases = aliases or _instrument_aliases()
    entity_results = assess_entities(title, classify_event_type(title), aliases)
    structured = extract_structured_event(
        title,
        published_at=published_at,
        source=str(row.get("source") or ""),
        affected_symbols=[item.symbol for item in entity_results],
    )
    _update_structured_event(
        event_id,
        structured,
        published_at=published_at,
        provenance=provenance,
        affected_symbols=[item.symbol for item in entity_results],
    )
    claim = extract_claim(title, [item.entity_name for item in entity_results])
    store.execute(
        """INSERT IGNORE INTO news_claims(
               event_id,news_id,claim_hash,subject_text,predicate_text,object_text,
               numeric_value,unit,modality,evidence_text,created_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)""",
        (
            event_id,
            news_id,
            claim["hash"],
            claim["subject"],
            claim["predicate"],
            claim["object"],
            claim["numeric_value"],
            claim["unit"],
            claim["modality"],
            claim["evidence"],
        ),
    )
    for item in entity_results:
        store.execute(
            """INSERT INTO news_event_entities(
                   event_id,symbol,entity_name,relationship_type,directness,direction,
                   impact_score,impact_confidence,horizons_json,evidence_json,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)
               ON DUPLICATE KEY UPDATE entity_name=VALUES(entity_name),
                   directness=GREATEST(directness,VALUES(directness)),direction=VALUES(direction),
                   impact_score=VALUES(impact_score),impact_confidence=VALUES(impact_confidence),
                   horizons_json=VALUES(horizons_json),evidence_json=VALUES(evidence_json),
                   updated_at=CURRENT_TIMESTAMP""",
            (
                event_id,
                item.symbol,
                item.entity_name,
                item.relationship_type,
                item.directness,
                item.direction,
                item.impact_score,
                item.impact_confidence,
                _json(item.horizons),
                _json(item.evidence),
            ),
        )
    store.execute(
        "UPDATE news_event_clusters SET last_seen_at=GREATEST(last_seen_at,?),updated_at=CURRENT_TIMESTAMP WHERE id=?",
        (published_at, event_id),
    )
    # Keep a cheap, queryable confirmation count alongside the detailed
    # evidence calculation.  The count is deliberately based on distinct
    # source domains, not article count, so syndicated copies do not inflate
    # confidence.
    store.execute(
        """UPDATE news_event_clusters c SET confirmation_count=(
               SELECT COUNT(DISTINCT COALESCE(source_domain,origin_key))
               FROM news_documents d WHERE d.event_id=c.id
           ) WHERE c.id=?""",
        (event_id,),
    )
    return event_id


def _event_evidence(event_id: int) -> dict[str, Any]:
    cluster_rows = store.query(
        """SELECT event_type,scheduled_at,actual_at,actual_value,consensus_value,
                  surprise_value,event_unit,affected_symbols_json,source_quality_score,
                  confirmation_count
           FROM news_event_clusters WHERE id=?""",
        (event_id,),
    )
    cluster = dict(cluster_rows[0]) if cluster_rows else {}
    documents = [
        dict(row)
        for row in store.query(
            "SELECT * FROM news_documents WHERE event_id=? ORDER BY published_at", (event_id,)
        )
    ]
    claims = [dict(row) for row in store.query("SELECT * FROM news_claims WHERE event_id=?", (event_id,))]
    entities = [
        dict(row) for row in store.query("SELECT * FROM news_event_entities WHERE event_id=?", (event_id,))
    ]
    by_hash: dict[str, list[dict[str, Any]]] = {}
    for row in documents:
        by_hash.setdefault(str(row["content_hash"]), []).append(row)
    origins: set[str] = set()
    for content_hash, group in by_hash.items():
        if len(group) > 1:
            origins.add(f"syndicated:{content_hash}")
        else:
            row = group[0]
            origins.add(f"source:{row.get('source_domain') or row.get('origin_key')}")
    primary = any(row.get("source_tier") == "S" for row in documents)
    best_provenance = max((_number(row.get("provenance_score")) for row in documents), default=0.0)
    rumor = any(row.get("modality") == "rumor" for row in claims)
    contradictions = sum(row.get("modality") == "denied" for row in claims)
    if primary:
        truth = 0.98
    elif len(origins) >= 3:
        truth = min(0.94, 0.76 + 0.06 * len(origins))
    elif len(origins) == 2:
        truth = 0.84
    else:
        truth = min(0.72, best_provenance * 0.82)
    if rumor:
        truth = min(truth, 0.45)
    if contradictions:
        truth *= 0.55
    return {
        "structured": cluster,
        "documents": documents,
        "claims": claims,
        "entities": entities,
        "origin_count": len(origins),
        "primary": primary,
        "truth": round(_clip(truth), 6),
        "rumor": rumor,
        "contradictions": contradictions,
    }


def _base_rate(event_id: int, symbol: str, event_type: str) -> dict[str, Any]:
    rows = store.query(
        """SELECT COUNT(*) sample_size,
                  AVG(o.directional_hit) hit_rate,AVG(o.signed_return_bps) mean_signed_return_bps
           FROM news_event_outcomes o
           JOIN news_event_clusters c ON c.id=o.event_id
           WHERE o.status='completed' AND o.event_id<>? AND o.horizon_seconds=3600
             AND (o.symbol=? OR c.event_type=?)""",
        (event_id, symbol, event_type),
    )
    row = dict(rows[0]) if rows else {}
    sample_size = int(row.get("sample_size") or 0)
    return {
        "sample_size": sample_size,
        "hit_rate": round(_number(row.get("hit_rate")), 6) if sample_size else None,
        "mean_signed_return_bps": (
            round(_number(row.get("mean_signed_return_bps")), 6) if sample_size else None
        ),
        "ready": sample_size >= MIN_BASE_RATE_SAMPLES,
    }


def _schedule_outcomes(
    decision_id: int, event_id: int, symbol: str, direction: str, now: int
) -> None:
    if direction not in {"long", "short"}:
        return
    baseline = store.query(
        """SELECT price FROM news_market_snapshots
           WHERE event_id=? AND symbol=? AND stage='T0'""",
        (event_id, symbol),
    )
    entry_price = _number(baseline[0].get("price")) if baseline else 0.0
    if entry_price <= 0:
        return
    for horizon in OUTCOME_HORIZONS:
        store.execute(
            """INSERT IGNORE INTO news_event_outcomes(
                   decision_id,event_id,symbol,horizon_seconds,status,predicted_direction,
                   entry_price,due_at,created_at,updated_at)
               VALUES(?,?,?,?,'pending',?,?,?,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)""",
            (decision_id, event_id, symbol, horizon, direction, entry_price, now + horizon),
        )


def update_outcomes(limit: int = 1000, now: int | None = None) -> dict[str, int]:
    now = int(now or time.time())
    rows = store.query(
        """SELECT id,symbol,predicted_direction,entry_price FROM news_event_outcomes
           WHERE status='pending' AND due_at<=? ORDER BY due_at LIMIT ?""",
        (now, limit),
    )
    completed = unavailable = 0
    for row in rows:
        ticker = store.query("SELECT price,ts FROM ticker WHERE symbol=?", (row["symbol"],))
        price = _number(ticker[0].get("price")) if ticker else 0.0
        fresh = bool(ticker and now - int(ticker[0].get("ts") or 0) <= 30)
        entry = _number(row.get("entry_price"))
        if not fresh or price <= 0 or entry <= 0:
            store.execute(
                """UPDATE news_event_outcomes SET status='unavailable',completed_at=?,
                       updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                (now, row["id"]),
            )
            unavailable += 1
            continue
        return_bps = (price / entry - 1.0) * 10_000
        sign = 1.0 if row["predicted_direction"] == "long" else -1.0
        signed_return = return_bps * sign
        store.execute(
            """UPDATE news_event_outcomes SET status='completed',exit_price=?,return_bps=?,
                   signed_return_bps=?,directional_hit=?,completed_at=?,updated_at=CURRENT_TIMESTAMP
               WHERE id=?""",
            (price, return_bps, signed_return, signed_return > 0, now, row["id"]),
        )
        completed += 1
    return {"completed": completed, "unavailable": unavailable}


def _market_confirmation(event_id: int, entity: dict[str, Any], now: int) -> tuple[str, dict[str, Any]]:
    symbol = str(entity["symbol"])
    baseline_rows = store.query(
        "SELECT * FROM news_market_snapshots WHERE event_id=? AND symbol=? AND stage='T0'",
        (event_id, symbol),
    )
    ticker_rows = store.query("SELECT price,ts FROM ticker WHERE symbol=?", (symbol,))
    micro_rows = store.query("SELECT spread_bps FROM market_microstructure WHERE symbol=?", (symbol,))
    positioning_rows = store.query(
        """SELECT open_interest,taker_buy_sell_ratio,snapshot_at_ms
           FROM market_positioning_snapshots WHERE symbol=?
           ORDER BY snapshot_at_ms DESC LIMIT 1""",
        (symbol,),
    )
    ticker = dict(ticker_rows[0]) if ticker_rows else {}
    positioning = dict(positioning_rows[0]) if positioning_rows else {}
    price = _number(ticker.get("price")) or None
    current = {
        "price": price,
        "open_interest": _number(positioning.get("open_interest")) or None,
        "taker_ratio": _number(positioning.get("taker_buy_sell_ratio")) or None,
        "spread_bps": _number((micro_rows[0] if micro_rows else {}).get("spread_bps")) or None,
        "fresh": bool(ticker and now - int(ticker.get("ts") or 0) <= 30),
    }
    if not baseline_rows:
        store.execute(
            """INSERT IGNORE INTO news_market_snapshots(
                   event_id,symbol,stage,as_of_ms,price,return_bps,open_interest,
                   taker_buy_sell_ratio,spread_bps,quality_json,created_at)
               VALUES(?,?,'T0',?,?,?,?,?,?,?,CURRENT_TIMESTAMP)""",
            (
                event_id,
                symbol,
                now * 1000,
                current["price"],
                None,
                current["open_interest"],
                current["taker_ratio"],
                current["spread_bps"],
                _json({"fresh": current["fresh"]}),
            ),
        )
        return "pending", {"reason": "baseline captured; waiting for delayed confirmation"}
    baseline = dict(baseline_rows[0])
    elapsed = now * 1000 - int(baseline["as_of_ms"])
    if elapsed < RECHECK_SECONDS * 1000 or not current["fresh"] or not current["price"]:
        return "pending", {"elapsed_seconds": max(0, elapsed // 1000), "fresh": current["fresh"]}
    base_price = _number(baseline.get("price"))
    return_bps = (current["price"] / base_price - 1.0) * 10_000 if base_price > 0 else 0.0
    store.execute(
        """INSERT IGNORE INTO news_market_snapshots(
               event_id,symbol,stage,as_of_ms,price,return_bps,open_interest,
               taker_buy_sell_ratio,spread_bps,quality_json,created_at)
           VALUES(?,?,'T5',?,?,?,?,?,?,?,CURRENT_TIMESTAMP)""",
        (
            event_id,
            symbol,
            now * 1000,
            current["price"],
            return_bps,
            current["open_interest"],
            current["taker_ratio"],
            current["spread_bps"],
            _json({"fresh": current["fresh"], "elapsed_seconds": elapsed // 1000}),
        ),
    )
    direction = str(entity.get("direction"))
    price_matches = (direction == "long" and return_bps >= 5.0) or (
        direction == "short" and return_bps <= -5.0
    )
    taker = _number(current["taker_ratio"], 1.0)
    flow_matches = (direction == "long" and taker >= 1.03) or (direction == "short" and taker <= 0.97)
    status = "confirmed" if price_matches and flow_matches else "partial" if price_matches else "contrary"
    return status, {
        "return_bps": round(return_bps, 4),
        "taker_buy_sell_ratio": current["taker_ratio"],
        "price_matches": price_matches,
        "flow_matches": flow_matches,
    }


def _record_rounds(event_id: int, rounds: list[dict[str, Any]], now: int) -> None:
    rows = store.query(
        "SELECT COALESCE(MAX(round_no),0) max_round FROM news_assessment_rounds WHERE event_id=?",
        (event_id,),
    )
    next_round = int(rows[0]["max_round"] or 0) + 1
    for item in rounds:
        store.execute(
            """INSERT INTO news_assessment_rounds(
                   event_id,round_no,stage,result_state,passed,score,reasons_json,
                   evidence_json,assessor,assessor_version,assessed_at,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)""",
            (
                event_id,
                next_round,
                item["stage"],
                item["state"],
                item["passed"],
                item["score"],
                _json(item.get("reasons", [])),
                _json(item.get("evidence", {})),
                ASSESSOR,
                ASSESSOR_VERSION,
                now,
            ),
        )
        next_round += 1


def assess_event(event_id: int, now: int | None = None) -> int:
    now = int(now or time.time())
    event_rows = store.query("SELECT * FROM news_event_clusters WHERE id=?", (event_id,))
    if not event_rows:
        return 0
    event = dict(event_rows[0])
    evidence = _event_evidence(event_id)
    provenance_ok = bool(evidence["documents"]) and all(
        row.get("canonical_url") and row.get("published_at") for row in evidence["documents"]
    )
    fact_verified = (evidence["primary"] or evidence["origin_count"] >= 2) and not evidence["rumor"]
    if evidence["contradictions"]:
        fact_verified = False
    entity_ready = bool(evidence["entities"])
    max_impact = max(
        (_number(row.get("impact_confidence")) for row in evidence["entities"]), default=0.0
    )
    impact_ready = entity_ready and max_impact >= 0.65
    structured = evidence.get("structured", {})
    has_numeric_claims = any(row.get("numeric_value") is not None for row in evidence["claims"])
    has_structured_release = (
        structured.get("actual_value") is not None or structured.get("consensus_value") is not None
    )
    numeric_validated = (
        not (has_numeric_claims or has_structured_release)
        or evidence["primary"]
        or evidence["origin_count"] >= 2
    )
    fresh = now - int(event["last_seen_at"]) <= MAX_EVENT_AGE_SECONDS

    rounds = [
        {
            "stage": "provenance",
            "state": "PROVENANCE_OK" if provenance_ok else "DATA_INSUFFICIENT",
            "passed": provenance_ok,
            "score": max((_number(r.get("provenance_score")) for r in evidence["documents"]), default=0),
            "reasons": [] if provenance_ok else ["missing canonical URL or publication time"],
            "evidence": {"documents": len(evidence["documents"])},
        },
        {
            "stage": "fact",
            "state": "FACT_VERIFIED" if fact_verified else "PROVENANCE_OK",
            "passed": fact_verified,
            "score": evidence["truth"],
            "reasons": [] if fact_verified else ["official source or two independent origins required"],
            "evidence": {
                "primary": evidence["primary"],
                "independent_origins": evidence["origin_count"],
                "rumor": evidence["rumor"],
            },
        },
        {
            "stage": "entity_impact",
            "state": "IMPACT_ASSESSED" if impact_ready else "DATA_INSUFFICIENT",
            "passed": impact_ready,
            "score": max_impact,
            "reasons": [] if impact_ready else ["no high-confidence symbol-specific direction"],
            "evidence": {"symbols": [row["symbol"] for row in evidence["entities"]]},
        },
        {
            "stage": "numeric_validation",
            "state": "CHALLENGED" if numeric_validated else "DATA_INSUFFICIENT",
            "passed": numeric_validated,
            "score": 1.0 if numeric_validated else 0.0,
            "reasons": (
                []
                if numeric_validated
                else ["numeric claim lacks official or independent corroboration"]
            ),
            "evidence": {"numeric_claims": has_numeric_claims},
        },
        {
            "stage": "counterevidence",
            "state": "CHALLENGED" if not evidence["contradictions"] else "DISPUTED",
            "passed": not evidence["contradictions"],
            "score": 1.0 if not evidence["contradictions"] else 0.0,
            "reasons": ["denial or correction detected"] if evidence["contradictions"] else [],
            "evidence": {"contradictions": evidence["contradictions"]},
        },
    ]
    decisions = 0
    best_state = "DETECTED"
    if provenance_ok:
        best_state = "PROVENANCE_OK"
    if fact_verified:
        best_state = "FACT_VERIFIED"
    if fact_verified and entity_ready:
        best_state = "IMPACT_ASSESSED"
    if evidence["contradictions"]:
        best_state = "DISPUTED"
    elif fact_verified and impact_ready and numeric_validated:
        best_state = "VALIDATED"
    if not fresh:
        best_state = "EXPIRED"

    for entity in evidence["entities"]:
        direction = str(entity["direction"])
        impact_confidence = _number(entity["impact_confidence"])
        market_status, market_evidence = _market_confirmation(event_id, entity, now)
        base_rate = _base_rate(event_id, str(entity["symbol"]), str(event["event_type"]))
        prior_rows = store.query(
            """SELECT * FROM news_decisions WHERE event_id=? AND symbol=? AND current_marker=1
               ORDER BY revision DESC LIMIT 1""",
            (event_id, entity["symbol"]),
        )
        prior = dict(prior_rows[0]) if prior_rows else None
        stable_direction = bool(prior and prior.get("direction") == direction)
        eligible = (
            fact_verified
            and impact_confidence >= 0.70
            and direction in {"long", "short"}
            and market_status == "confirmed"
            and stable_direction
            and base_rate["ready"]
            and _number(base_rate["mean_signed_return_bps"]) > 0
            and fresh
            and not evidence["contradictions"]
        )
        if eligible:
            state, reference = "REFERENCE_ELIGIBLE", "eligible"
        elif fact_verified and impact_ready and numeric_validated and market_status == "confirmed":
            state, reference = "MARKET_CONFIRMED", "risk_only"
        elif evidence["contradictions"]:
            state, reference = "DISPUTED", "blocked"
        elif fact_verified and impact_ready and numeric_validated:
            state, reference = "VALIDATED", "risk_only"
        elif direction in {"long", "short", "conflicted"}:
            state, reference = best_state, "observe"
        else:
            state, reference = best_state, "display_only"
        counterevidence: list[str] = ["only title evidence is available; full text not independently parsed"]
        if evidence["origin_count"] < 2 and not evidence["primary"]:
            counterevidence.append("independent corroboration is insufficient")
        if evidence["rumor"]:
            counterevidence.append("claim contains rumor or conditional language")
        if market_status in {"pending", "contrary"}:
            counterevidence.append(f"market confirmation is {market_status}")
        if not numeric_validated:
            counterevidence.append("numeric claims are not independently validated")
        if not base_rate["ready"]:
            counterevidence.append(
                f"historical base rate has {base_rate['sample_size']} of {MIN_BASE_RATE_SAMPLES} required samples"
            )
        invalidation = [
            "official denial, correction or cancellation",
            "entity mapping falls below required confidence",
            "market response reverses before valid_until",
            "event becomes stale or materially revised",
        ]
        revision = int(prior["revision"] if prior else 0) + 1
        changed = not prior or any(
            prior.get(key) != value
            for key, value in {
                "state": state,
                "direction": direction,
                "market_confirmation": market_status,
                "reference_status": reference,
            }.items()
        )
        if changed:
            if prior:
                store.execute("UPDATE news_decisions SET current_marker=NULL WHERE id=?", (prior["id"],))
            store.execute(
                """INSERT INTO news_decisions(
                       event_id,symbol,revision,current_marker,state,direction,truth_confidence,
                       impact_confidence,market_confirmation,reference_status,valid_until,
                       counterevidence_json,invalidation_json,rationale_json,created_at)
                   VALUES(?,?,?,1,?,?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)""",
                (
                    event_id,
                    entity["symbol"],
                    revision,
                    state,
                    direction,
                    evidence["truth"],
                    impact_confidence,
                    market_status,
                    reference,
                    min(int(event["last_seen_at"]) + MAX_EVENT_AGE_SECONDS, now + 4 * 3600),
                    _json(counterevidence),
                    _json(invalidation),
                    _json(
                        {
                            "independent_origins": evidence["origin_count"],
                            "primary_source": evidence["primary"],
                            "market": market_evidence,
                            "base_rate": base_rate,
                            "assessor": ASSESSOR,
                            "assessor_version": ASSESSOR_VERSION,
                        }
                    ),
                ),
            )
            decision_rows = store.query(
                "SELECT id FROM news_decisions WHERE event_id=? AND symbol=? AND revision=?",
                (event_id, entity["symbol"], revision),
            )
            if decision_rows:
                _schedule_outcomes(
                    int(decision_rows[0]["id"]), event_id, str(entity["symbol"]), direction, now
                )
            decisions += 1
        if state in {"REFERENCE_ELIGIBLE", "MARKET_CONFIRMED"}:
            best_state = state
        rounds.append(
            {
                "stage": "historical_base_rate",
                "state": "VALIDATED" if base_rate["ready"] else "DATA_INSUFFICIENT",
                "passed": base_rate["ready"],
                "score": _number(base_rate["hit_rate"]),
                "reasons": [] if base_rate["ready"] else ["insufficient leakage-safe forward outcomes"],
                "evidence": {"symbol": entity["symbol"], **base_rate},
            }
        )
        rounds.append(
            {
                "stage": f"market:{entity['symbol']}",
                "state": "MARKET_CONFIRMED" if market_status == "confirmed" else "VALIDATED",
                "passed": market_status == "confirmed",
                "score": 1.0 if market_status == "confirmed" else 0.5 if market_status == "partial" else 0.0,
                "reasons": [] if market_status == "confirmed" else [f"market status: {market_status}"],
                "evidence": market_evidence,
            }
        )

    _record_rounds(event_id, rounds, now)
    source_quality = _number(structured.get("source_quality_score"), 0.0)
    quality = (
        0.30 * evidence["truth"]
        + 0.25 * max_impact
        + 0.20 * min(1.0, evidence["origin_count"] / 2)
        + 0.10 * source_quality
        + 0.15 * (1.0 if fresh else 0.0)
    )
    store.execute(
        """UPDATE news_event_clusters SET state=?,revision=revision+1,
               independent_origins=?,contradiction_count=?,quality_score=?,updated_at=CURRENT_TIMESTAMP
           WHERE id=?""",
        (best_state, evidence["origin_count"], evidence["contradictions"], quality, event_id),
    )
    return decisions


def verify_once(limit: int = 100) -> dict[str, int]:
    outcomes = update_outcomes()
    aliases = _instrument_aliases()
    rows = [
        dict(row)
        for row in store.query(
            """SELECT n.* FROM news n LEFT JOIN news_documents d ON d.news_id=n.id
               WHERE d.news_id IS NULL ORDER BY n.ts DESC LIMIT ?""",
            (limit,),
        )
    ]
    events: set[int] = set()
    failures = 0
    for row in rows:
        try:
            events.add(process_document(row, aliases))
        except Exception as exc:
            failures += 1
            store.collector_report(
                "news_verification_item",
                success=False,
                error=f"{row.get('id')}: {type(exc).__name__}: {exc}",
            )
    pending = store.query(
        """SELECT DISTINCT event_id FROM news_decisions
           WHERE current_marker=1 AND market_confirmation IN ('pending','partial')
             AND valid_until>? ORDER BY event_id DESC LIMIT ?""",
        (int(time.time()), max(10, limit // 2)),
    )
    events.update(int(row["event_id"]) for row in pending)
    decisions = 0
    for event_id in events:
        try:
            decisions += assess_event(event_id)
        except Exception as exc:
            failures += 1
            store.collector_report(
                "news_verification_event",
                success=False,
                error=f"{event_id}: {type(exc).__name__}: {exc}",
            )
    if failures == 0:
        store.collector_report(
            "news_verification_item",
            success=True,
            items=len(rows),
            details={"batch_failures": 0},
        )
        store.collector_report(
            "news_verification_event",
            success=True,
            items=len(events),
            details={"batch_failures": 0},
        )
    return {
        "documents": len(rows),
        "events": len(events),
        "decisions": decisions,
        "outcomes_completed": outcomes["completed"],
        "outcomes_unavailable": outcomes["unavailable"],
        "failures": failures,
    }


def verification_loop(stop_event=None) -> None:
    while stop_event is None or not stop_event.is_set():
        try:
            result = verify_once()
            store.collector_report("news_verification", success=True, items=result["documents"], details=result)
        except Exception as exc:
            store.collector_report("news_verification", success=False, error=f"{type(exc).__name__}: {exc}")
        if stop_event is not None:
            stop_event.wait(60)
        else:
            time.sleep(60)


def features_for_symbol(symbol: str, now: int | None = None) -> dict[str, Any]:
    """Return shadow-only news features; they intentionally have zero model weight."""

    now = int(now or time.time())
    rows = store.query(
        """SELECT d.direction,d.impact_confidence,d.truth_confidence,d.reference_status,
                  d.valid_until,e.impact_score,e.horizons_json
           FROM news_decisions d JOIN news_event_entities e
             ON e.event_id=d.event_id AND e.symbol=d.symbol
           WHERE d.symbol=? AND d.current_marker=1 AND d.valid_until>?
           ORDER BY d.created_at DESC LIMIT 50""",
        (symbol, now),
    )
    verified_pressure = 0.0
    rumor_pressure = 0.0
    eligible_count = 0
    qualities: list[float] = []
    for row in rows:
        sign = 1.0 if row["direction"] == "long" else -1.0 if row["direction"] == "short" else 0.0
        strength = sign * _number(row["impact_score"]) * _number(row["impact_confidence"]) * _number(row["truth_confidence"])
        if row["reference_status"] == "eligible":
            verified_pressure += strength
            eligible_count += 1
        else:
            rumor_pressure += strength
        qualities.append(_number(row["truth_confidence"]) * _number(row["impact_confidence"]))
    return {
        "verified_event_pressure": max(-1.0, min(1.0, verified_pressure)),
        "rumor_pressure": max(-1.0, min(1.0, rumor_pressure * 0.25)),
        "event_risk_gate": eligible_count > 0,
        "news_data_quality": round(sum(qualities) / len(qualities), 6) if qualities else 0.0,
        "eligible_event_count": eligible_count,
        "feature_state": "shadow_only",
        "feature_version": SHADOW_FEATURE_VERSION,
    }
