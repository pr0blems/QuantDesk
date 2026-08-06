# syntax=docker/dockerfile:1.7
FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN groupadd --system quantdesk && useradd --system --gid quantdesk --home /app quantdesk

COPY pyproject.toml README.md alembic.ini ./
COPY migrations ./migrations
COPY config ./config
COPY src ./src

RUN --mount=type=cache,target=/root/.cache/pip \
    python -m pip install --retries 8 --timeout 60 .

USER quantdesk
EXPOSE 8200

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8200/api/v2/health', timeout=3)"

CMD ["quantdesk-ng", "serve"]
