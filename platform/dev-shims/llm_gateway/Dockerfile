FROM python:3.12-slim
RUN useradd --create-home --uid 10001 appuser
WORKDIR /app
RUN pip install --no-cache-dir fastapi uvicorn httpx prometheus-client
COPY platform/dev-shims/llm_gateway/app.py /app/app.py
USER 10001
EXPOSE 8090
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8090"]
