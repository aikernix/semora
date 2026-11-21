FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY backend backend
COPY frontend frontend
RUN useradd --system --uid 10001 offline && mkdir /data && chown offline /data
USER offline
ENV OFFLINE_DATA_DIR=/data
EXPOSE 8015
CMD ["uvicorn", "backend.app:app", "--host", "0.0.0.0", "--port", "8015", "--workers", "1"]
