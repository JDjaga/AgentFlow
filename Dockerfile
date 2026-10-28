# ─── AgentFlow — Production Docker image (VPS / cloud) ────────────────────────
# Build:  docker build -t agentflow .
# Run:    docker run -p 8001:8001 --env-file .env agentflow
#
# The LLM layer (llm_config.py) calls cloud providers over HTTPS, so this image
# needs NO local Ollama/GPU. Set LLM_PROVIDERS + API keys via --env-file / env vars.

FROM python:3.12-slim

# Environment: don't write .pyc, unbuffered logs (so uvicorn/loguru stream to docker logs)
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONIOENCODING=utf-8

WORKDIR /app

# System deps: build tools for psycopg2, curl for healthchecks.
# (tesseract-ocr is optional — only needed if the Analyst OCRs real images.)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8001

# Liveness probe hits the /status endpoint.
# ${PORT:-8001} — locally we use 8001; Render/cloud inject their own $PORT
# (e.g. 10000) and route traffic there, so we MUST honour it.
HEALTHCHECK --interval=30s --timeout=8s --start-period=20s --retries=3 \
    CMD curl -fsS http://localhost:${PORT:-8001}/status || exit 1

# JSON form (keeps Docker lint clean) wrapping sh -c so:
#   - ${PORT} expands at runtime (Render sets it; local docker doesn't)
#   - `exec` replaces the shell so uvicorn receives SIGTERM on shutdown
CMD ["sh", "-c", "exec uvicorn api.server:app --host 0.0.0.0 --port ${PORT:-8001}"]
