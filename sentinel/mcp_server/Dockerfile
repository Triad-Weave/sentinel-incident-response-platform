# Note: NEVER copies eval/, chaos/, or releases/ into sentinel-mcp image (FR-CH-03)!
FROM python:3.12-slim AS builder
WORKDIR /build
COPY platform/telemetry /build/platform/telemetry
RUN pip install --no-cache-dir --prefix=/install \
    /build/platform/telemetry \
    fastapi==0.165.0 uvicorn==0.54.0 pydantic==2.14.4 pydantic-settings==2.16.3 \
    httpx==0.28.1 mcp==2.3.0 kubernetes==37.0.3 prometheus-client==0.26.0

FROM python:3.12-slim
RUN useradd --create-home --uid 10001 appuser
WORKDIR /app
COPY --from=builder /install /usr/local
COPY sentinel/common /app/sentinel_common
COPY sentinel/mcp_server/sentinel_mcp /app/sentinel_mcp
USER 10001
EXPOSE 8003
CMD ["uvicorn", "sentinel_mcp.main:app", "--host", "0.0.0.0", "--port", "8003"]
