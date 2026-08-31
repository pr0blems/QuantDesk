"""Application services for the governed AI Monitor pipeline."""

from .ablation import classify_ablation_signal_state
from .contracts import AiMonitorAuthority, AiMonitorStageResult
from .event_gate import EventGateService
from .macro_regime import MacroRegimeService
from .market_features import MarketFeatureService, realtime_feature_payload
from .news_scoring import NewsScoringService, news_event_bursts
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
    "MacroRegimeService",
    "MarketFeatureService",
    "NewsScoringService",
    "OpportunityGenerationService",
    "annotate_event_cluster_selection",
    "filter_monitored_candidates",
    "fresh_candidate_news_ids",
    "news_event_bursts",
    "OpportunityProjectionError",
    "OpportunityProjectionLagging",
    "OpportunityProjectionService",
    "OpportunityProjectionUnavailable",
    "PredictionSettlementService",
    "prepare_candidate_news_triggers",
    "realtime_feature_payload",
    "strongest_candidate_per_symbol",
]
