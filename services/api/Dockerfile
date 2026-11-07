FROM python:3.12-slim
WORKDIR /app
COPY services/api/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt
COPY packages/contracts/python /app/packages/contracts/python
COPY packages/query-core/python/semora_query_core /app/semora_query_core
COPY packages/telemetry/python/semora_telemetry /app/semora_telemetry
COPY services/agent/semora_agent /app/semora_agent
COPY services/api/app /app/app
ENV PYTHONPATH=/app:/app/packages/contracts/python
RUN addgroup --system --gid 10001 semora \
    && adduser --system --uid 10001 --ingroup semora --no-create-home semora
USER 10001:10001
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]

