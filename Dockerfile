FROM python:3.12-slim
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg tzdata \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY app.py index.html ./
ENV PYTHONUNBUFFERED=1 CAMREC_PORT=8090 CAMREC_CONFIG=/config RECORD_ROOTS=/media,/mnt,/recordings
EXPOSE 8090
STOPSIGNAL SIGTERM
CMD ["python3", "/app/app.py"]
