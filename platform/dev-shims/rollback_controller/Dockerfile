FROM python:3.12-slim
RUN useradd --create-home --uid 10001 appuser
WORKDIR /app
RUN pip install --no-cache-dir fastapi uvicorn pydantic pyyaml
COPY platform/dev-shims/rollback_controller/app.py /app/app.py
USER 10001
EXPOSE 8095
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8095"]
