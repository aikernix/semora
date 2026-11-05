FROM python:3.12-slim
WORKDIR /app
COPY services/executor/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt
COPY services/executor/app /app/app
COPY packages/telemetry/python/semora_telemetry /app/semora_telemetry
RUN addgroup --system --gid 10001 semora \
    && adduser --system --uid 10001 --ingroup semora --no-create-home semora
USER 10001:10001
EXPOSE 8002
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8002"]

