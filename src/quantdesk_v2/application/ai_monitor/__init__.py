"""Application services for the governed AI Monitor pipeline."""

from .ablation import classify_ablation_signal_state
from .contracts import AiMonitorAuthority, AiMonitorStageResult
from .event_gate import EventGateService
from .macro_regime import MacroRegimeService
from .market_features import MarketFeatureService, realtime_feature_payload
from .news_scoring import NewsScoringService
from .opportunity_generation import OpportunityGenerationService
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
    "OpportunityProjectionError",
    "OpportunityProjectionLagging",
    "OpportunityProjectionService",
    "OpportunityProjectionUnavailable",
    "PredictionSettlementService",
    "realtime_feature_payload",
]
