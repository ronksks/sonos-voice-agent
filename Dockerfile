FROM python:3.11-slim
WORKDIR /app
RUN pip install --no-cache-dir fastapi uvicorn requests soco
COPY agent.py .
CMD ["uvicorn", "agent:app", "--host", "0.0.0.0", "--port", "8080"]
