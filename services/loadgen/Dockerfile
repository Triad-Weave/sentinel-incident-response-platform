FROM python:3.12-slim
RUN useradd --create-home --uid 10001 appuser
WORKDIR /app
RUN pip install --no-cache-dir httpx==0.28.1
COPY services/loadgen/loadgen_app /app/loadgen_app
USER 10001
CMD ["python", "-m", "loadgen_app.main"]
