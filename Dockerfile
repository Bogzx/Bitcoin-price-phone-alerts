FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DATABASE_URL=sqlite:////data/alerts.db

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py ./
COPY btc_alerts ./btc_alerts

RUN useradd --create-home --uid 10001 app && mkdir /data && chown app /data
USER app
VOLUME ["/data"]
EXPOSE 5000

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:5000/healthz', timeout=4).status == 200 else 1)"

# Exactly one worker: the price feed and Socket.IO state live in process memory.
# Threads serve concurrent requests and Socket.IO connections.
CMD ["gunicorn", "--workers", "1", "--threads", "50", "--bind", "0.0.0.0:5000", "app:app"]
