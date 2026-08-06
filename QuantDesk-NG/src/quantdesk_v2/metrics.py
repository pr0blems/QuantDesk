"""Small Prometheus-compatible process metrics without another runtime dependency."""

from __future__ import annotations

import hmac
from collections import Counter
from threading import Lock
from time import perf_counter

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import PlainTextResponse
from starlette.middleware.base import BaseHTTPMiddleware

router = APIRouter(tags=["operations"])
_BUCKETS = (0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)


class RuntimeMetrics:
    def __init__(self) -> None:
        self.lock = Lock()
        self.requests = Counter()
        self.latency_buckets = Counter()
        self.latency_count = Counter()
        self.latency_sum = Counter()
        self.inflight = 0

    def started(self) -> None:
        with self.lock:
            self.inflight += 1

    def finished(self, method: str, route: str, status_code: int, elapsed: float) -> None:
        key = (method, route, str(status_code))
        with self.lock:
            self.inflight = max(0, self.inflight - 1)
            self.requests[key] += 1
            self.latency_count[(method, route)] += 1
            self.latency_sum[(method, route)] += elapsed
            for bucket in _BUCKETS:
                if elapsed <= bucket:
                    self.latency_buckets[(method, route, bucket)] += 1

    def render(self, version: str) -> str:
        rows = [
            "# HELP quantdesk_build_info QuantDesk build information.",
            "# TYPE quantdesk_build_info gauge",
            f'quantdesk_build_info{{version="{version}"}} 1',
            "# HELP quantdesk_http_requests_in_flight Active HTTP requests.",
            "# TYPE quantdesk_http_requests_in_flight gauge",
            f"quantdesk_http_requests_in_flight {self.inflight}",
            "# HELP quantdesk_http_requests_total Completed HTTP requests.",
            "# TYPE quantdesk_http_requests_total counter",
        ]
        with self.lock:
            for (method, route, status_code), value in sorted(self.requests.items()):
                labels = f'method="{method}",route="{route}",status="{status_code}"'
                rows.append(f"quantdesk_http_requests_total{{{labels}}} {value}")
            rows.extend(
                [
                    "# HELP quantdesk_http_request_duration_seconds HTTP request latency.",
                    "# TYPE quantdesk_http_request_duration_seconds histogram",
                ]
            )
            for (method, route), count in sorted(self.latency_count.items()):
                labels = f'method="{method}",route="{route}"'
                for bucket in _BUCKETS:
                    value = self.latency_buckets[(method, route, bucket)]
                    rows.append(
                        f'quantdesk_http_request_duration_seconds_bucket{{{labels},le="{bucket}"}} {value}'
                    )
                rows.append(
                    f'quantdesk_http_request_duration_seconds_bucket{{{labels},le="+Inf"}} {count}'
                )
                rows.append(f"quantdesk_http_request_duration_seconds_count{{{labels}}} {count}")
                rows.append(
                    f"quantdesk_http_request_duration_seconds_sum{{{labels}}} {self.latency_sum[(method, route)]:.9f}"
                )
        return "\n".join(rows) + "\n"


def _route_label(path: str) -> str:
    if path.startswith("/api/v2/monitor/"):
        return "/api/v2/monitor/:resource"
    if path.startswith("/api/v2/backtests/"):
        return "/api/v2/backtests/:id"
    if path.startswith("/api/v2/paper/accounts/"):
        return "/api/v2/paper/accounts/:id"
    return path if len(path) <= 96 else "/unmatched"


class MetricsMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        metrics: RuntimeMetrics = request.app.state.metrics
        metrics.started()
        started = perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            metrics.finished(
                request.method, _route_label(request.url.path), 500, perf_counter() - started
            )
            raise
        metrics.finished(
            request.method,
            _route_label(request.url.path),
            response.status_code,
            perf_counter() - started,
        )
        return response


def _authorized(request: Request) -> bool:
    settings = request.app.state.settings
    token = settings.metrics_token.get_secret_value()
    if settings.app_env.lower() != "production":
        return True
    supplied = request.headers.get("Authorization", "").removeprefix("Bearer ")
    return bool(token) and hmac.compare_digest(supplied, token)


@router.get("/metrics", include_in_schema=False, response_class=PlainTextResponse)
def prometheus_metrics(request: Request) -> PlainTextResponse:
    if not request.app.state.settings.metrics_enabled:
        raise HTTPException(status_code=404, detail="metrics are disabled")
    if not _authorized(request):
        raise HTTPException(status_code=401, detail="metrics authorization required")
    return PlainTextResponse(
        request.app.state.metrics.render(request.app.state.settings.app_version),
        media_type="text/plain; version=0.0.4; charset=utf-8",
    )
