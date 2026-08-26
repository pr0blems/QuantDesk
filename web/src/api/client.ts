import type { CurrentUser, TokenPair } from "./types";

const apiOrigin = (import.meta.env.VITE_API_BASE_URL ?? "").replace(/\/$/, "");
const apiRoot = `${apiOrigin}/api/v2`;
const identityStorageKey = "quantdesk.web.user-id";
const authenticationLostEvent = "quantdesk:authentication-lost";

let accessToken = "";
let authenticatedUserId = readStoredUserId();
let refreshPromise: Promise<boolean> | null = null;

type ErrorPayload = {
  detail?: unknown;
  message?: unknown;
};

export class ApiError extends Error {
  readonly status: number;
  readonly detail: unknown;

  constructor(message: string, status: number, detail: unknown = null) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
  }
}

function readStoredUserId(): string {
  try {
    return window.sessionStorage.getItem(identityStorageKey) ?? "";
  } catch {
    return "";
  }
}

function writeStoredUserId(userId: string): void {
  authenticatedUserId = userId;
  try {
    if (userId) window.sessionStorage.setItem(identityStorageKey, userId);
    else window.sessionStorage.removeItem(identityStorageKey);
  } catch {
    // The in-memory identity still protects this tab when storage is unavailable.
  }
}

function loseAuthentication(): void {
  clearSession();
  window.dispatchEvent(new Event(authenticationLostEvent));
}

function detailMessage(detail: unknown): string {
  if (typeof detail === "string" && detail.trim()) return detail;
  if (Array.isArray(detail)) {
    const messages = detail
      .map((item: unknown) => {
        if (!item || typeof item !== "object") return "";
        const candidate = item as { msg?: unknown; message?: unknown };
        if (typeof candidate.msg === "string") return candidate.msg;
        return typeof candidate.message === "string" ? candidate.message : "";
      })
      .filter(Boolean);
    if (messages.length > 0) return messages.join("；");
  }
  if (detail && typeof detail === "object") {
    const message = (detail as { message?: unknown }).message;
    if (typeof message === "string" && message.trim()) return message;
  }
  return "请求失败";
}

async function responsePayload(response: Response): Promise<unknown> {
  const contentType = response.headers.get("content-type") ?? "";
  if (!contentType.includes("application/json")) return null;
  return response.json().catch(() => null) as Promise<unknown>;
}

async function refreshAccessToken(): Promise<boolean> {
  if (refreshPromise) return refreshPromise;

  const pending = (async () => {
    let response: Response;
    try {
      response = await fetch(`${apiRoot}/auth/refresh`, {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: "{}",
      });
    } catch {
      return false;
    }
    if (!response.ok) return false;
    const pair = (await responsePayload(response)) as TokenPair | null;
    if (!pair?.access_token) return false;

    if (authenticatedUserId) {
      const identityResponse = await fetch(`${apiRoot}/me`, {
        credentials: "include",
        headers: { Authorization: `Bearer ${pair.access_token}` },
      });
      if (!identityResponse.ok) return false;
      const currentUser = (await responsePayload(identityResponse)) as CurrentUser | null;
      if (!currentUser || String(currentUser.id) !== authenticatedUserId) {
        loseAuthentication();
        throw new ApiError("检测到登录身份已变化，请重新登录。", 401);
      }
    }

    accessToken = pair.access_token;
    return true;
  })();

  refreshPromise = pending;
  try {
    return await pending;
  } finally {
    if (refreshPromise === pending) refreshPromise = null;
  }
}

export interface ApiRequestOptions extends RequestInit {
  retryAuthentication?: boolean;
  useAuthentication?: boolean;
}

export type ApiStreamOptions = ApiRequestOptions;

export async function openAiMonitorWebSocket(): Promise<WebSocket> {
  if (!accessToken) {
    const restored = await refreshAccessToken();
    if (!restored) {
      loseAuthentication();
      throw new ApiError("登录状态已失效，请重新登录", 401);
    }
  }
  const endpoint = new URL(`${apiRoot}/ai-monitor/ws`, window.location.origin);
  endpoint.protocol = endpoint.protocol === "https:" ? "wss:" : "ws:";
  return new WebSocket(endpoint, [
    "quantdesk.ai-monitor.v1",
    `quantdesk.auth.${accessToken}`,
  ]);
}

export async function openMonitorMarketWebSocket(symbol: string): Promise<WebSocket> {
  const normalized = symbol.trim().toUpperCase();
  if (!/^[A-Z0-9]{2,24}$/.test(normalized)) {
    throw new ApiError("行情品种代码无效", 422);
  }
  if (!accessToken) {
    const restored = await refreshAccessToken();
    if (!restored) {
      loseAuthentication();
      throw new ApiError("登录状态已失效，请重新登录", 401);
    }
  }
  const endpoint = new URL(`${apiRoot}/ai-monitor/market/ws`, window.location.origin);
  endpoint.protocol = endpoint.protocol === "https:" ? "wss:" : "ws:";
  endpoint.searchParams.set("symbol", normalized);
  return new WebSocket(endpoint, [
    "quantdesk.ai-monitor.v1",
    `quantdesk.auth.${accessToken}`,
  ]);
}

export async function apiRequest<T>(
  path: `/${string}`,
  options: ApiRequestOptions = {},
): Promise<T> {
  const {
    retryAuthentication = true,
    useAuthentication = true,
    ...requestOptions
  } = options;
  const headers = new Headers(requestOptions.headers);
  if (requestOptions.body && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  if (useAuthentication && accessToken) {
    headers.set("Authorization", `Bearer ${accessToken}`);
    if (authenticatedUserId) headers.set("X-QuantDesk-User-ID", authenticatedUserId);
  }

  const response = await fetch(`${apiRoot}${path}`, {
    ...requestOptions,
    headers,
    credentials: "include",
  });

  if (
    response.status === 401 &&
    retryAuthentication &&
    useAuthentication &&
    !path.startsWith("/auth/")
  ) {
    const refreshed = await refreshAccessToken();
    if (refreshed) {
      return apiRequest<T>(path, { ...options, retryAuthentication: false });
    }
    loseAuthentication();
  }

  const payload = await responsePayload(response);
  if (!response.ok) {
    const errorPayload = (payload ?? {}) as ErrorPayload;
    const detail = errorPayload.detail ?? errorPayload.message ?? null;
    throw new ApiError(detailMessage(detail), response.status, detail);
  }
  return payload as T;
}

export async function apiStream(
  path: `/${string}`,
  options: ApiStreamOptions = {},
): Promise<Response> {
  const {
    retryAuthentication = true,
    useAuthentication = true,
    ...requestOptions
  } = options;
  const headers = new Headers(requestOptions.headers);
  headers.set("Accept", "text/event-stream");
  if (useAuthentication && accessToken) {
    headers.set("Authorization", `Bearer ${accessToken}`);
    if (authenticatedUserId) headers.set("X-QuantDesk-User-ID", authenticatedUserId);
  }
  const response = await fetch(`${apiRoot}${path}`, {
    ...requestOptions,
    headers,
    credentials: "include",
  });
  if (
    response.status === 401
    && retryAuthentication
    && useAuthentication
    && !path.startsWith("/auth/")
  ) {
    const refreshed = await refreshAccessToken();
    if (refreshed) {
      return apiStream(path, { ...options, retryAuthentication: false });
    }
    loseAuthentication();
  }
  if (!response.ok) {
    const payload = await responsePayload(response);
    const errorPayload = (payload ?? {}) as ErrorPayload;
    const detail = errorPayload.detail ?? errorPayload.message ?? null;
    throw new ApiError(detailMessage(detail), response.status, detail);
  }
  return response;
}

export function setAccessToken(token: string): void {
  accessToken = token;
}

export function rememberUser(user: CurrentUser): void {
  writeStoredUserId(String(user.id));
}

export function clearSession(): void {
  accessToken = "";
  writeStoredUserId("");
}

export function onAuthenticationLost(listener: () => void): () => void {
  window.addEventListener(authenticationLostEvent, listener);
  return () => window.removeEventListener(authenticationLostEvent, listener);
}

export async function restoreAccess(): Promise<boolean> {
  return refreshAccessToken();
}
