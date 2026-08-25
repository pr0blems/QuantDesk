import type { components } from "./schema";

export type JsonPrimitive = boolean | number | string | null;
export type JsonValue = JsonPrimitive | JsonValue[] | { [key: string]: JsonValue };
export type ApiObject = Record<string, unknown>;
export type ApiList = ApiObject[];

export type HealthStatus = components["schemas"]["HealthOut"];
export type DashboardPerformance = components["schemas"]["DashboardPerformanceOut"];
export type BinancePerformance = components["schemas"]["BinancePerformanceOut"];
export type TokenPair = components["schemas"]["TokenPair"];
export type CurrentUser = components["schemas"]["UserOut"];
export type LoginInput = Pick<
  components["schemas"]["LoginRequest"],
  "username" | "password"
>;
export type RegisterInput = components["schemas"]["RegisterRequest"];
export type BacktestRunRequest = components["schemas"]["BacktestRunRequest"];
export type PaperAccountCreateRequest = components["schemas"]["PaperAccountCreateRequest"];
export type PaperAccountStatusUpdate = components["schemas"]["PaperAccountStatusUpdate"];
export type PaperAccountStrategyUpdate = components["schemas"]["PaperAccountStrategyUpdate"];
export type StrategyCreateRequest = components["schemas"]["StrategyCreateRequest"];
export type StrategyUpdateRequest = components["schemas"]["StrategyUpdateRequest"];
export type StrategyPromotionRequest = components["schemas"]["StrategyPromotionRequest"];
export type StrategyAiPreviewRequest = components["schemas"]["StrategyAiPreviewRequest"];
export type StrategyAiApplyRequest = components["schemas"]["StrategyAiApplyRequest"];
export type LiveAccountCreateRequest = components["schemas"]["LiveAccountCreateRequest"];
export type LiveAccountStatusUpdate = components["schemas"]["LiveAccountStatusUpdate"];
export type LiveAccountArmRequest = components["schemas"]["LiveAccountArmRequest"];
export type LiveAccountStrategyUpdate = components["schemas"]["LiveAccountStrategyUpdate"];
export type BinanceCredentialUpdate = components["schemas"]["BinanceCredentialUpdate"];
export type AiModelConfigCreate = components["schemas"]["AiModelConfigCreate"];
export type AiModelConfigUpdate = components["schemas"]["AiModelConfigUpdate"];
export type MonitorWatchlistUpdate = components["schemas"]["MonitorWatchlistUpdate"];
export type OpportunityPreferenceUpdate = components["schemas"]["OpportunityPreferenceUpdate"];
export type PredictionAlgorithmOptimizationRequest =
  components["schemas"]["PredictionAlgorithmOptimizationRequest"];
export type PredictionAlgorithmUpdate = components["schemas"]["PredictionAlgorithmUpdate"];
export type AdminAlertRulesUpdate = components["schemas"]["AdminAlertRulesUpdate"];
export type AdminCleanupRequest = components["schemas"]["AdminCleanupRequest"];
export type AdminNewsAiBatchCreate = components["schemas"]["AdminNewsAiBatchCreate"];
export type AdminNewsSourceCreate = components["schemas"]["AdminNewsSourceCreate"];
export type AdminNewsSourceUpdate = components["schemas"]["AdminNewsSourceUpdate"];
export type AdminUserUpdate = components["schemas"]["AdminUserUpdate"];

export interface Strategy {
  id: string;
  public_id: string;
  name: string;
  category: string;
  description: string;
  status: string;
  version: number;
  engine_key: string;
  strategy_kind: string;
  lifecycle_status: string;
  spec_schema_version: number | null;
  spec: Record<string, JsonValue> | null;
  spec_hash: string | null;
  risk_level: string;
  parameter_schema: JsonValue[];
  parameters: Record<string, JsonValue>;
  risk_defaults: Record<string, JsonValue>;
  created_via: string;
  is_default: boolean;
  source_template_key: string | null;
  created_at: string | null;
  updated_at: string | null;
}

export interface StrategyTemplate {
  template_key: string;
  name: string;
  category: string;
  description: string;
  engine_key: string;
  template_kind: string;
  spec_schema_version: number | null;
  spec: Record<string, JsonValue> | null;
  implementation_version: string;
  parameter_schema: JsonValue[];
  parameters: Record<string, JsonValue>;
  risk_defaults: Record<string, JsonValue>;
  version: number;
}

export interface StrategyListResponse {
  items: Strategy[];
  templates: StrategyTemplate[];
  limits: {
    max_active_strategies: number;
  };
}

export interface StrategyDeployment {
  id: string;
  name: string;
  mode: string;
  status: string;
  strategy_id: number;
  strategy_revision_id: number;
  target_account_id: number | null;
  last_evaluated_bar_time: number | null;
  last_error_code: string | null;
  started_at: string | null;
  created_at: string;
  updated_at: string;
}

export interface StrategyDeploymentsResponse {
  items: StrategyDeployment[];
}

export interface StrategyReadinessCheck {
  code: string;
  label: string;
  passed: boolean;
  detail: string;
  evidence_id?: number | null;
}

export interface StrategyReadiness {
  strategy_id: string;
  strategy_version: number;
  revision_id: number | null;
  revision_status: string;
  next_status: string | null;
  can_promote: boolean;
  blockers: string[];
  checks: StrategyReadinessCheck[];
  promotion_checks: StrategyReadinessCheck[];
  eligibility: { backtest: boolean; paper: boolean; live: boolean };
}

export interface LiveAccount {
  id: string;
  name: string;
  status: string;
  strategy_id: string | null;
  strategy_name: string | null;
  engine_key: string | null;
  config: Record<string, JsonValue>;
  credential_version: number;
  armed_at: string | null;
  last_tick_at: string | null;
  last_error_code: string | null;
  system_enabled: boolean;
  created_at: string;
  updated_at: string;
}

export interface LiveAccountsResponse {
  items: LiveAccount[];
  system_enabled: boolean;
  credentials_configured: boolean;
  trade_permission_requested: boolean;
  universe: {
    key: string;
    count: number;
    label: string;
  };
}

export interface BinanceLiveStatus {
  configured: boolean;
  connected: boolean;
  account_type?: string;
  wallet_balance?: number;
  available_balance?: number;
  unrealized_pnl?: number;
  updated_at?: string;
  error_category: string | null;
  retry_at?: string | null;
  retry_after_seconds?: number;
  used_weight?: number;
  weight_limit?: number;
}

export interface LivePosition extends Record<string, unknown> {
  symbol?: string;
  position_side?: string;
  position_amt?: number | string;
  entry_price?: number | string;
  unrealized_pnl?: number | string;
  managed_by_strategy?: boolean;
}

export interface LiveOrder extends Record<string, unknown> {
  symbol?: string;
  side?: string;
  type?: string;
  status?: string;
  order_id?: string | number;
}

export interface LiveOrderIntent {
  id: string;
  client_order_id: string;
  binance_order_id: string | null;
  symbol: string;
  action: string;
  side: string;
  position_side: string | null;
  order_type: string;
  quantity: number | null;
  status: string;
  error_code: string | null;
  strategy_signal_id: number | null;
  entry_basis: Record<string, JsonValue>;
  request: Record<string, JsonValue>;
  response: Record<string, JsonValue>;
  submitted_at: string | null;
  created_at: string;
}

export interface LiveDashboardResponse {
  live_account: LiveAccount;
  binance: BinanceLiveStatus;
  positions: LivePosition[];
  open_orders: LiveOrder[];
  order_intents: LiveOrderIntent[];
}
