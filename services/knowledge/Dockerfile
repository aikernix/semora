FROM python:3.12-slim
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends \
    tesseract-ocr tesseract-ocr-chi-sim tesseract-ocr-eng \
    && rm -rf /var/lib/apt/lists/*
COPY services/knowledge/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt
COPY services/knowledge/app /app/app
COPY packages/telemetry/python/semora_telemetry /app/semora_telemetry
COPY packages/s3-security/python/semora_s3_security /app/semora_s3_security
COPY services/knowledge/seed /app/seed
ENV PYTHONPATH=/app
RUN addgroup --system --gid 10001 semora \
    && adduser --system --uid 10001 --ingroup semora --no-create-home semora \
    && mkdir -p /data/uploads \
    && chown -R 10001:10001 /data /app
USER 10001:10001
EXPOSE 8003
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8003"]
