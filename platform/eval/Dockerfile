FROM python:3.12-slim AS builder
WORKDIR /build
COPY platform/eval /build/platform/eval
RUN pip install --no-cache-dir --prefix=/install /build/platform/eval

FROM python:3.12-slim
RUN useradd --create-home --uid 10001 appuser
WORKDIR /app
COPY --from=builder /install /usr/local
COPY platform/eval/schema /app/schema
RUN mkdir -p /data && chown -R appuser:appuser /data
USER 10001
ENV EVAL_STORE_PATH=/data/runs.jsonl
ENV EVAL_SCHEMA_PATH=/app/schema/eval-result.v1.schema.json
EXPOSE 8096
CMD ["uvicorn", "platform_eval.store:app", "--host", "0.0.0.0", "--port", "8096"]
