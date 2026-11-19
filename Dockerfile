FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN adduser --disabled-password --gecos "" appuser

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY --chown=appuser:appuser config.py models.py sonos_service.py workflows.py server.py agent.py scenes.json ./

USER appuser

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import sys, urllib.request; r = urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=4); sys.exit(0 if r.status == 200 else 1)"

CMD ["uvicorn", "agent:app", "--host", "0.0.0.0", "--port", "8080"]
