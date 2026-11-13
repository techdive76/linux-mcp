FROM python:3.12-slim

# Keeps Python from generating .pyc files in the container
ENV PYTHONDONTWRITEBYTECODE=1
# Turns off buffering for easier container logging
ENV PYTHONUNBUFFERED=1

# Container-side locations of the mounted volumes (see docker-compose.yml)
ENV MCP_HOST=0.0.0.0 \
    MCP_PORT=8000 \
    SERVERS_CONFIG=/config/servers.yaml \
    CLIENTS_CONFIG=/config/clients.yaml \
    SSH_KEY_DIR=/ssh

WORKDIR /app

# Install pip requirements
COPY requirements.txt .
RUN python -m pip install --no-cache-dir -r requirements.txt

COPY pyproject.toml ./
COPY src/ ./src/
RUN python -m pip install --no-cache-dir --no-deps .

# Non-root user with an explicit UID — SSH keys in the /ssh volume must be readable by UID 5678
RUN adduser -u 5678 --disabled-password --gecos "" appuser && chown -R appuser /app
USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3).status == 200 else 1)"

# Single process: MCP sessions are held in memory.
# Proxy headers are handled by AuthMiddleware (TRUSTED_PROXIES), not by uvicorn.
CMD ["uvicorn", "linux_mcp.server:starlette_app", "--host", "0.0.0.0", "--port", "8000", "--no-proxy-headers"]
