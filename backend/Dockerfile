FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY backend/pompa ./pompa
COPY docs/reference/heishamon/MQTT-Topics.md docs/reference/heishamon/realne_dane.md ./docs/reference/heishamon/
RUN useradd --system --uid 10001 pompa
USER pompa

EXPOSE 8001
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s \
  CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/health' % os.environ.get('API_PORT', '8001'), timeout=4)"

# One process, one worker: the app object is passed to uvicorn inside main().
CMD ["python", "-m", "pompa.main"]
