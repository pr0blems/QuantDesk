"""Application services for the governed AI Monitor pipeline."""

from .ablation import classify_ablation_signal_state
from .contracts import AiMonitorAuthority, AiMonitorStageResult
from .event_gate import (
    EventGateService,
    market_risk_event_gate_snapshot,
    market_risk_event_snapshot,
    utc_event_iso,
)
from .macro_regime import MacroRegimeService
from .market_features import MarketFeatureService, realtime_feature_payload
from .news_scoring import (
    NewsScoringService,
    aggregate_news_candidates,
    configured_indicator_policy,
    indicator_group,
    match_configured_indicators,
    multi_timeframe_technical_snapshot,
    news_actionability_snapshot,
    news_event_bursts,
    news_event_cluster_id,
    resolved_news_event_cluster_id,
    select_directional_candidates_with_technical_context,
)
from .opportunity_generation import (
    OpportunityGenerationService,
    annotate_event_cluster_selection,
    filter_monitored_candidates,
    fresh_candidate_news_ids,
    prepare_candidate_news_triggers,
    strongest_candidate_per_symbol,
)
from .opportunity_projection import (
    OpportunityProjectionError,
    OpportunityProjectionLagging,
    OpportunityProjectionService,
    OpportunityProjectionUnavailable,
)
from .prediction_settlement import PredictionSettlementService

__all__ = [
    "AiMonitorAuthority",
    "AiMonitorStageResult",
    "classify_ablation_signal_state",
    "EventGateService",
    "market_risk_event_gate_snapshot",
    "market_risk_event_snapshot",
    "MacroRegimeService",
    "MarketFeatureService",
    "NewsScoringService",
    "aggregate_news_candidates",
    "configured_indicator_policy",
    "indicator_group",
    "OpportunityGenerationService",
    "annotate_event_cluster_selection",
    "filter_monitored_candidates",
    "fresh_candidate_news_ids",
    "news_event_bursts",
    "news_actionability_snapshot",
    "news_event_cluster_id",
    "match_configured_indicators",
    "multi_timeframe_technical_snapshot",
    "OpportunityProjectionError",
    "OpportunityProjectionLagging",
    "OpportunityProjectionService",
    "OpportunityProjectionUnavailable",
    "PredictionSettlementService",
    "prepare_candidate_news_triggers",
    "realtime_feature_payload",
    "resolved_news_event_cluster_id",
    "select_directional_candidates_with_technical_context",
    "strongest_candidate_per_symbol",
    "utc_event_iso",
]
