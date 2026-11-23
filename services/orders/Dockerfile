FROM python:3.12-slim AS builder
WORKDIR /build
COPY platform/telemetry /build/platform/telemetry
COPY services/faultkit /build/services/faultkit
RUN pip install --no-cache-dir --prefix=/install \
    /build/platform/telemetry \
    /build/services/faultkit \
    fastapi==0.165.0 uvicorn==0.54.0 pydantic==2.14.4 pydantic-settings==2.16.3 \
    sqlalchemy==2.1.9 asyncpg==0.32.0 httpx==0.28.1 prometheus-client==0.26.0

FROM python:3.12-slim
RUN useradd --create-home --uid 10001 appuser
WORKDIR /app
COPY --from=builder /install /usr/local
COPY services/orders/VERSION /app/VERSION
COPY services/orders/orders_app /app/orders_app
USER 10001
EXPOSE 8001
CMD ["uvicorn", "orders_app.main:app", "--host", "0.0.0.0", "--port", "8001"]
