import {
  apiRequest,
  clearSession,
  rememberUser,
  restoreAccess,
  setAccessToken,
} from "./client";
import type {
  AdminAlertRulesUpdate,
  AdminCleanupRequest,
  AdminNewsAiBatchCreate,
  AdminNewsSourceCreate,
  AdminNewsSourceUpdate,
  AdminUserUpdate,
  AiModelConfigCreate,
  AiModelConfigUpdate,
  ApiObject,
  BacktestRunRequest,
  BinanceCredentialUpdate,
  BinancePerformance,
  CurrentUser,
  DashboardPerformance,
  HealthStatus,
  LiveAccountArmRequest,
  LiveAccountCreateRequest,
  LiveAccountStatusUpdate,
  LiveAccountStrategyUpdate,
  LiveAccountsResponse,
  KillSwitchCommandRequest,
  LiveDashboardResponse,
  LoginInput,
  MonitorWatchlistUpdate,
  OpportunityPreferenceUpdate,
  PaperAccountCreateRequest,
  PaperAccountStatusUpdate,
  PaperAccountStrategyUpdate,
  PredictionAlgorithmOptimizationRequest,
  PredictionAlgorithmUpdate,
  RegisterInput,
  RuntimeIncidentListResponse,
  StrategyAiApplyRequest,
  StrategyAiPreviewRequest,
  StrategyCreateRequest,
  StrategyDeploymentsResponse,
  StrategyListResponse,
  StrategyPromotionRequest,
  StrategyPromotionReviewList,
  StrategyReadiness,
  StrategyValidationRunList,
  StrategyUpdateRequest,
  TokenPair,
  TradingControlLatch,
  TradingControlListResponse,
  TradingReadiness,
} from "./types";

type QueryValue = boolean | number | string | null | undefined;

function withQuery(path: string, values: Record<string, QueryValue>): `/${string}` {
  const query = new URLSearchParams();
  for (const [key, value] of Object.entries(values)) {
    if (value !== null && value !== undefined && value !== "") query.set(key, String(value));
  }
  const suffix = query.toString();
  return `${path}${suffix ? `?${suffix}` : ""}` as `/${string}`;
}

function jsonBody(value: unknown): string {
  return JSON.stringify(value);
}

export const systemApi = {
  health: () => apiRequest<HealthStatus>("/health", { useAuthentication: false }),
};

export const authApi = {
  async login(input: LoginInput): Promise<CurrentUser> {
    const pair = await apiRequest<TokenPair>("/auth/login", {
      method: "POST",
      body: JSON.stringify({ ...input, client_type: "web" }),
      retryAuthentication: false,
      useAuthentication: false,
    });
    setAccessToken(pair.access_token);
    const user = await apiRequest<CurrentUser>("/me", { retryAuthentication: false });
    rememberUser(user);
    return user;
  },

  async restore(): Promise<CurrentUser | null> {
    const restored = await restoreAccess();
    if (!restored) {
      clearSession();
      return null;
    }
    const user = await apiRequest<CurrentUser>("/me", { retryAuthentication: false });
    rememberUser(user);
    return user;
  },

  async logout(): Promise<void> {
    try {
      await apiRequest<{ message: string }>("/auth/logout", {
        method: "POST",
        body: "{}",
        retryAuthentication: false,
      });
    } finally {
      clearSession();
    }
  },

  register: (input: RegisterInput) =>
    apiRequest<ApiObject>("/auth/register", {
      method: "POST",
      body: jsonBody(input),
      retryAuthentication: false,
      useAuthentication: false,
    }),
};

export const strategyApi = {
  list: () => apiRequest<StrategyListResponse>("/strategies"),
  deployments: () =>
    apiRequest<StrategyDeploymentsResponse>("/strategies/deployments"),
  indicators: () => apiRequest<ApiObject>("/strategies/indicators/catalog"),
  signals: (limit = 100) => apiRequest<ApiObject>(withQuery("/strategies/signals", { limit })),
  detail: (publicId: string) => apiRequest<ApiObject>(`/strategies/${encodeURIComponent(publicId)}`),
  revisions: (publicId: string) => apiRequest<ApiObject>(`/strategies/${encodeURIComponent(publicId)}/revisions`),
  readiness: (publicId: string) =>
    apiRequest<StrategyReadiness>(`/strategies/${encodeURIComponent(publicId)}/readiness`),
  validationRuns: (publicId: string) =>
    apiRequest<StrategyValidationRunList>(`/strategies/${encodeURIComponent(publicId)}/validation-runs`),
  promotionRequests: (publicId: string) =>
    apiRequest<StrategyPromotionReviewList>(`/strategies/${encodeURIComponent(publicId)}/promotion-requests`),
  requestPromotion: (publicId: string, input: {
    request_id: string;
    expected_version: number;
    target_status: "validated" | "backtested" | "shadow" | "paper" | "micro_live" | "live";
    request_note: string;
    confirmed: true;
  }) => apiRequest<ApiObject>(`/strategies/${encodeURIComponent(publicId)}/promotion-requests`, { method: "POST", body: jsonBody(input) }),
  decidePromotion: (publicId: string, reviewId: string, input: {
    action: "approve" | "reject";
    expected_review_version: number;
    decision_note: string;
    confirmed: true;
  }) => apiRequest<ApiObject>(`/strategies/${encodeURIComponent(publicId)}/promotion-requests/${encodeURIComponent(reviewId)}/decision`, { method: "POST", body: jsonBody(input) }),
  rollback: (publicId: string, input: { expected_version: number; target_version: number; reason: string; confirmed: true }) =>
    apiRequest<ApiObject>(`/strategies/${encodeURIComponent(publicId)}/rollback`, { method: "POST", body: jsonBody(input) }),
  promote: (publicId: string, input: StrategyPromotionRequest) =>
    apiRequest<ApiObject>(`/strategies/${encodeURIComponent(publicId)}/promote`, { method: "POST", body: jsonBody(input) }),
  create: (input: StrategyCreateRequest) => apiRequest<ApiObject>("/strategies", { method: "POST", body: jsonBody(input) }),
  update: (publicId: string, input: StrategyUpdateRequest) =>
    apiRequest<ApiObject>(`/strategies/${encodeURIComponent(publicId)}`, { method: "PUT", body: jsonBody(input) }),
  archive: (publicId: string) =>
    apiRequest<ApiObject>(`/strategies/${encodeURIComponent(publicId)}`, { method: "DELETE" }),
  validate: (publicId: string) =>
    apiRequest<ApiObject>(`/strategies/${encodeURIComponent(publicId)}/validate`, { method: "POST" }),
  aiPreview: (publicId: string, input: StrategyAiPreviewRequest) =>
    apiRequest<ApiObject>(`/strategies/${encodeURIComponent(publicId)}/ai-preview`, { method: "POST", body: jsonBody(input) }),
  compositionPreview: (input: StrategyAiPreviewRequest) =>
    apiRequest<ApiObject>("/strategies/compose/ai-preview", { method: "POST", body: jsonBody(input) }),
  aiApply: (publicId: string, input: StrategyAiApplyRequest) =>
    apiRequest<ApiObject>(`/strategies/${encodeURIComponent(publicId)}/ai-apply`, { method: "POST", body: jsonBody(input) }),
};

export const liveApi = {
  accounts: () => apiRequest<LiveAccountsResponse>("/live/accounts"),
  dashboard: (accountId: string) =>
    apiRequest<LiveDashboardResponse>(
      `/live?${new URLSearchParams({ account_id: accountId }).toString()}`,
    ),
  create: (input: LiveAccountCreateRequest) =>
    apiRequest<ApiObject>("/live/accounts", { method: "POST", body: jsonBody(input) }),
  update: (accountId: string, input: LiveAccountStatusUpdate) =>
    apiRequest<ApiObject>(`/live/accounts/${encodeURIComponent(accountId)}`, { method: "PATCH", body: jsonBody(input) }),
  updateStrategy: (accountId: string, input: LiveAccountStrategyUpdate) =>
    apiRequest<ApiObject>(`/live/accounts/${encodeURIComponent(accountId)}/strategy`, { method: "PUT", body: jsonBody(input) }),
  arm: (accountId: string, input: LiveAccountArmRequest) =>
    apiRequest<ApiObject>(`/live/accounts/${encodeURIComponent(accountId)}/arm`, { method: "POST", body: jsonBody(input) }),
};

export const riskApi = {
  readiness: () => apiRequest<TradingReadiness>("/system/trading-readiness"),
  incidents: (status?: "open" | "acknowledged" | "resolved") =>
    apiRequest<RuntimeIncidentListResponse>(
      withQuery("/system/incidents", { incident_status: status }),
    ),
  acknowledgeIncident: (incidentId: string, note: string) =>
    apiRequest<ApiObject>(
      `/system/incidents/${encodeURIComponent(incidentId)}/acknowledge`,
      { method: "POST", body: jsonBody({ note, confirmed: true }) },
    ),
  resolveIncident: (incidentId: string, note: string) =>
    apiRequest<ApiObject>(
      `/system/incidents/${encodeURIComponent(incidentId)}/resolve`,
      { method: "POST", body: jsonBody({ note, confirmed: true }) },
    ),
  controls: (engagedOnly = false) =>
    apiRequest<TradingControlListResponse>(
      withQuery("/risk/kill-switches", { engaged_only: engagedOnly }),
    ),
  transition: (input: KillSwitchCommandRequest) =>
    apiRequest<TradingControlLatch>("/risk/kill-switch", {
      method: "POST",
      body: jsonBody(input),
    }),
};

export const dashboardApi = {
  performance: (month?: string, timezoneOffsetMinutes = new Date().getTimezoneOffset()) =>
    apiRequest<DashboardPerformance>(withQuery("/dashboard/performance", { month, timezone_offset_minutes: timezoneOffsetMinutes })),
  binancePerformance: (month?: string, timezoneOffsetMinutes = new Date().getTimezoneOffset()) =>
    apiRequest<BinancePerformance>(withQuery("/dashboard/binance-performance", { month, timezone_offset_minutes: timezoneOffsetMinutes })),
};

export const monitorApi = {
  overview: () => apiRequest<ApiObject>("/monitor/overview"),
  breadth: () => apiRequest<ApiObject>("/monitor/breadth"),
  intelligence: () => apiRequest<ApiObject>("/monitor/intelligence"),
  watchlist: () => apiRequest<ApiObject>("/monitor/watchlist"),
  saveWatchlist: (input: MonitorWatchlistUpdate) =>
    apiRequest<ApiObject>("/monitor/watchlist", { method: "PUT", body: jsonBody(input) }),
  alerts: (limit = 80) => apiRequest<ApiObject>(withQuery("/monitor/alerts", { limit })),
  markAlertsRead: () => apiRequest<ApiObject>("/monitor/alerts/read", { method: "POST" }),
  news: (limit = 60) => apiRequest<ApiObject>(withQuery("/monitor/news", { limit })),
  predictionAlgorithm: () => apiRequest<ApiObject>("/monitor/prediction-algorithm"),
  optimizePredictionAlgorithm: (input: PredictionAlgorithmOptimizationRequest) =>
    apiRequest<ApiObject>("/monitor/prediction-algorithm/optimize", {
      method: "POST",
      body: jsonBody(input),
    }),
  updatePredictionAlgorithm: (input: PredictionAlgorithmUpdate) =>
    apiRequest<ApiObject>("/monitor/prediction-algorithm", { method: "PUT", body: jsonBody(input) }),
  predictionHistory: (values: Record<string, QueryValue>) =>
    apiRequest<ApiObject>(withQuery("/monitor/prediction-history", values)),
  klines: (symbol: string, timeframe: string, limit = 120) =>
    apiRequest<ApiObject>(withQuery("/monitor/klines", { symbol, tf: timeframe, limit })),
  strategyIndicators: (symbol: string, timeframe: string) =>
    apiRequest<ApiObject>(withQuery("/monitor/strategy-indicators", { symbol, tf: timeframe })),
  score: (symbol: string) => apiRequest<ApiObject>(withQuery("/monitor/score", { symbol })),
  report: (symbol: string) => apiRequest<ApiObject>(withQuery("/monitor/report", { symbol })),
  opportunities: (symbol?: string, limit = 40, includeIgnored = false) =>
    apiRequest<ApiObject>(withQuery("/monitor/opportunities", { symbol, limit, include_ignored: includeIgnored })),
  preference: (publicId: string, input: OpportunityPreferenceUpdate) =>
    apiRequest<ApiObject>(`/monitor/opportunities/${encodeURIComponent(publicId)}/preference`, { method: "POST", body: jsonBody(input) }),
};

export const paperApi = {
  accounts: () => apiRequest<ApiObject>("/paper/accounts"),
  dashboard: (accountId: string) => apiRequest<ApiObject>(withQuery("/paper", { account_id: accountId })),
  create: (input: PaperAccountCreateRequest) =>
    apiRequest<ApiObject>("/paper/accounts", { method: "POST", body: jsonBody(input) }),
  update: (accountId: string, input: PaperAccountStatusUpdate) =>
    apiRequest<ApiObject>(`/paper/accounts/${encodeURIComponent(accountId)}`, { method: "PATCH", body: jsonBody(input) }),
  updateStrategy: (accountId: string, input: PaperAccountStrategyUpdate) =>
    apiRequest<ApiObject>(`/paper/accounts/${encodeURIComponent(accountId)}/strategy`, { method: "PUT", body: jsonBody(input) }),
  reset: (accountId: string) =>
    apiRequest<ApiObject>(withQuery("/paper/reset", { account_id: accountId }), { method: "POST" }),
};

export const backtestApi = {
  catalog: () => apiRequest<ApiObject>("/backtests/catalog"),
  history: (limit = 20) => apiRequest<ApiObject>(withQuery("/backtests", { limit })),
  detail: (runId: string | number) => apiRequest<ApiObject>(`/backtests/${encodeURIComponent(String(runId))}`),
  run: (input: BacktestRunRequest) =>
    apiRequest<ApiObject>("/backtests", { method: "POST", body: jsonBody(input) }),
};

export const settingsApi = {
  binanceAccount: () => apiRequest<ApiObject>("/me/binance-account"),
  saveBinanceCredentials: (input: BinanceCredentialUpdate) =>
    apiRequest<ApiObject>("/me/binance-credentials", { method: "PUT", body: jsonBody(input) }),
  deleteBinanceCredentials: () => apiRequest<ApiObject>("/me/binance-credentials", { method: "DELETE" }),
  aiProviders: () => apiRequest<ApiObject>("/me/ai-model-providers"),
  aiConfigs: () => apiRequest<ApiObject>("/me/ai-model-configs"),
  createAiConfig: (input: AiModelConfigCreate) =>
    apiRequest<ApiObject>("/me/ai-model-configs", { method: "POST", body: jsonBody(input) }),
  updateAiConfig: (configId: string | number, input: AiModelConfigUpdate) =>
    apiRequest<ApiObject>(`/me/ai-model-configs/${encodeURIComponent(String(configId))}`, { method: "PUT", body: jsonBody(input) }),
  deleteAiConfig: (configId: string | number) =>
    apiRequest<ApiObject>(`/me/ai-model-configs/${encodeURIComponent(String(configId))}`, { method: "DELETE" }),
  testAiConfig: (configId: string | number) =>
    apiRequest<ApiObject>(`/me/ai-model-configs/${encodeURIComponent(String(configId))}/test`, { method: "POST" }),
};

export const adminApi = {
  overview: () => apiRequest<ApiObject>("/admin/overview"),
  collectors: () => apiRequest<ApiObject>("/admin/collectors"),
  collectorAction: (name: string, action: string) =>
    apiRequest<ApiObject>(`/admin/collectors/${encodeURIComponent(name)}/${encodeURIComponent(action)}`, { method: "POST" }),
  alerts: (values: Record<string, QueryValue> = {}) => apiRequest<ApiObject>(withQuery("/admin/alerts", values)),
  alertRules: () => apiRequest<ApiObject>("/admin/alert-rules"),
  updateAlertRules: (input: AdminAlertRulesUpdate) =>
    apiRequest<ApiObject>("/admin/alert-rules", { method: "PUT", body: jsonBody(input) }),
  news: (values: Record<string, QueryValue> = {}) => apiRequest<ApiObject>(withQuery("/admin/news", values)),
  newsSources: () => apiRequest<ApiObject>("/admin/news-sources"),
  createNewsSource: (input: AdminNewsSourceCreate) =>
    apiRequest<ApiObject>("/admin/news-sources", { method: "POST", body: jsonBody(input) }),
  updateNewsSource: (name: string, input: AdminNewsSourceUpdate) =>
    apiRequest<ApiObject>(`/admin/news-sources/${encodeURIComponent(name)}`, { method: "PATCH", body: jsonBody(input) }),
  deleteNewsSource: (name: string) =>
    apiRequest<ApiObject>(`/admin/news-sources/${encodeURIComponent(name)}`, { method: "DELETE" }),
  testNewsSource: (name: string) =>
    apiRequest<ApiObject>(`/admin/news-sources/${encodeURIComponent(name)}/test`, { method: "POST" }),
  newsBatches: (limit = 20) => apiRequest<ApiObject>(withQuery("/admin/news-ai-batches", { limit })),
  createNewsBatch: (input: AdminNewsAiBatchCreate) =>
    apiRequest<ApiObject>("/admin/news-ai-batches", { method: "POST", body: jsonBody(input) }),
  retryNewsBatch: (batchId: string | number) =>
    apiRequest<ApiObject>(`/admin/news-ai-batches/${encodeURIComponent(String(batchId))}/retry`, { method: "POST" }),
  users: (values: Record<string, QueryValue> = {}) => apiRequest<ApiObject>(withQuery("/admin/users", values)),
  updateUser: (userId: string | number, input: AdminUserUpdate) =>
    apiRequest<ApiObject>(`/admin/users/${encodeURIComponent(String(userId))}`, { method: "PATCH", body: jsonBody(input) }),
  revokeSessions: (userId: string | number) =>
    apiRequest<ApiObject>(`/admin/users/${encodeURIComponent(String(userId))}/revoke-sessions`, { method: "POST" }),
  storage: () => apiRequest<ApiObject>("/admin/storage"),
  audit: (values: Record<string, QueryValue> = {}) => apiRequest<ApiObject>(withQuery("/admin/audit", values)),
  symbols: (values: Record<string, QueryValue> = {}) => apiRequest<ApiObject>(withQuery("/admin/symbols", values)),
  stockLibrary: (values: Record<string, QueryValue> = {}) => apiRequest<ApiObject>(withQuery("/admin/stock-library", values)),
  importStockLibrary: () => apiRequest<ApiObject>("/admin/stock-library/import", { method: "POST" }),
  syncStock: (symbol: string) =>
    apiRequest<ApiObject>(`/admin/stock-library/${encodeURIComponent(symbol)}/sync`, { method: "POST" }),
  cleanupPreview: (input: AdminCleanupRequest) =>
    apiRequest<ApiObject>("/admin/maintenance/cleanup-preview", { method: "POST", body: jsonBody(input) }),
  cleanup: (input: AdminCleanupRequest) =>
    apiRequest<ApiObject>("/admin/maintenance/cleanup", { method: "POST", body: jsonBody(input) }),
};
