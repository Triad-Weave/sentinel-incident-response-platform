FROM python:3.12-slim AS builder
WORKDIR /build
COPY platform/telemetry /build/platform/telemetry
RUN pip install --no-cache-dir --prefix=/install \
    /build/platform/telemetry \
    fastapi==0.165.0 uvicorn==0.54.0 pydantic==2.14.4 pydantic-settings==2.16.3 \
    sqlalchemy==2.1.9 asyncpg==0.32.0 aio-pika==10.2.3 httpx==0.28.1 \
    argon2-cffi==25.1.0 itsdangerous==2.2.0 prometheus-client==0.26.0

FROM python:3.12-slim
RUN useradd --create-home --uid 10001 appuser
WORKDIR /app
COPY --from=builder /install /usr/local
COPY sentinel/common /app/sentinel_common
COPY sentinel/worker/sentinel_worker /app/sentinel_worker
COPY sentinel/api/sentinel_api /app/sentinel_api
USER 10001
EXPOSE 8000
CMD ["uvicorn", "sentinel_api.main:app", "--host", "0.0.0.0", "--port", "8000"]
