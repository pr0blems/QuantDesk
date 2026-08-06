FROM node:22-alpine AS web-builder

WORKDIR /web

COPY web/package.json web/package-lock.json ./
RUN npm ci

COPY web ./
RUN npm run check:api && npm run build

FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN groupadd --system quantdesk \
    && useradd --system --gid quantdesk --home /app quantdesk

COPY pyproject.toml README.md alembic.ini ./
COPY migrations ./migrations
COPY config ./config
COPY src ./src
COPY --from=web-builder /web/dist ./src/quantdesk_v2/react_static

RUN python -m pip install --upgrade pip \
    && python -m pip install .

USER quantdesk
EXPOSE 8200

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8200/api/v2/health', timeout=3)"

CMD ["quantdesk-v2", "serve"]
