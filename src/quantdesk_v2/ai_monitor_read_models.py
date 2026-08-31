"""Compatibility exports for AI Monitor read-model projections.

Projection assembly is infrastructure-owned.  This module preserves the public
maintenance import path while production code imports the adapter directly.
"""

from .infrastructure.persistence.ai_monitor_read_models import (
    PREDICTION_FACT_PROJECTION_VERSION,
    PREDICTION_FACT_READ_BATCH_SIZE,
    PROJECTION_FLUSH_BATCH_SIZE,
    READ_MODEL_TABLES,
    SCORE_HISTORY_READ_BATCH_SIZE,
    read_models_available,
    reconcile_ai_monitor_read_models,
    refresh_ai_monitor_read_models,
    refresh_current_opportunities,
    refresh_prediction_facts,
    refresh_score_history,
)

__all__ = [
    "PREDICTION_FACT_PROJECTION_VERSION",
    "PREDICTION_FACT_READ_BATCH_SIZE",
    "PROJECTION_FLUSH_BATCH_SIZE",
    "READ_MODEL_TABLES",
    "SCORE_HISTORY_READ_BATCH_SIZE",
    "read_models_available",
    "reconcile_ai_monitor_read_models",
    "refresh_ai_monitor_read_models",
    "refresh_current_opportunities",
    "refresh_prediction_facts",
    "refresh_score_history",
]
