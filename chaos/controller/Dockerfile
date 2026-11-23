FROM python:3.12-slim AS builder
WORKDIR /build
COPY platform/telemetry /build/platform/telemetry
COPY chaos/controller /build/chaos/controller
RUN pip install --no-cache-dir --prefix=/install \
    /build/platform/telemetry \
    fastapi==0.165.0 uvicorn==0.54.0 pydantic==2.14.4 pydantic-settings==2.16.3 \
    sqlalchemy==2.1.9 asyncpg==0.32.0 httpx==0.28.1

FROM python:3.12-slim
RUN useradd --create-home --uid 10001 appuser
WORKDIR /app
COPY --from=builder /install /usr/local
COPY chaos/controller/chaos_controller /app/chaos_controller
USER 10001
EXPOSE 8010
CMD ["uvicorn", "chaos_controller.main:app", "--host", "0.0.0.0", "--port", "8010"]
