FROM python:3.12-slim
WORKDIR /app
COPY services/audit-exporter/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt
COPY packages/telemetry/python/semora_telemetry /app/semora_telemetry
COPY packages/s3-security/python/semora_s3_security /app/semora_s3_security
COPY services/audit-exporter/app /app/app
RUN addgroup --system --gid 10001 semora \
    && adduser --system --uid 10001 --ingroup semora --no-create-home semora
USER 10001:10001
EXPOSE 8004
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8004"]
