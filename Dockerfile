# Stage 1: Claude Code CLI (used when research runs on a Claude Pro/Max subscription).
FROM node:22-bookworm-slim AS claude
RUN npm install -g --prefix /opt/claude @anthropic-ai/claude-code@2.1.311 && npm cache clean --force

# Stage 2: the portal (web + worker share this image).
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 DATA_DIR=/data \
    HOME=/data/home PATH=/opt/claude/bin:$PATH DISABLE_AUTOUPDATER=1
WORKDIR /app

COPY --from=claude /usr/local/bin/node /usr/local/bin/node
COPY --from=claude /opt/claude /opt/claude

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app ./app
COPY MASTER_PROMPT.md README.md ./

RUN useradd --system --uid 10001 --home /data/home afportal && mkdir -p /data/home && chown -R afportal /data \
    && node --version && claude --version
USER afportal

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4).status == 200 else 1)" || exit 1

# The same image runs the web portal (default) or the worker (`python -m app.worker`).
CMD ["uvicorn", "app.web.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2", "--proxy-headers", "--forwarded-allow-ips", "*"]
