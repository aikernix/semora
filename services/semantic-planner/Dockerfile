FROM python:3.12-slim
WORKDIR /app
COPY services/semantic-planner/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt
COPY packages/contracts/python /app/packages/contracts/python
COPY packages/query-core/python/semora_query_core /app/semora_query_core
COPY services/api/app/planner.py /app/planner.py
COPY services/api/app/query_stages.py /app/query_stages.py
COPY packages/telemetry/python/semora_telemetry /app/semora_telemetry
COPY services/semantic-planner/app /app/app
ENV PYTHONPATH=/app:/app/packages/contracts/python
RUN addgroup --system --gid 10001 semora \
    && adduser --system --uid 10001 --ingroup semora --no-create-home semora
USER 10001:10001
EXPOSE 8001
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8001"]

