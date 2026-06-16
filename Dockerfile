# Darwin Agentic Cloud — production server image.
#
# Builds a minimal Python image that exposes:
#   - The FastAPI HTTP server on port 8787 (default CMD)
#   - The CLI as `darwin`
#   - The MCP server via `darwin mcp serve`
#
# Usage:
#   docker run -p 8787:8787 darwin-agentic-cloud
#   docker run darwin-agentic-cloud darwin run --help
#
# The signing key persists in /data; mount a volume to keep it across
# restarts:
#   docker run -v darwin-data:/data -p 8787:8787 darwin-agentic-cloud

# ---- Stage 1: build dependencies and install darwin ----
FROM python:3.14-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build

# Install uv for fast resolution
RUN pip install uv==0.9.5

# Copy only what's needed for install
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY darwin ./darwin

# Install into a venv at /opt/darwin
RUN uv venv /opt/darwin --python 3.12 && \
    uv pip install --python /opt/darwin/bin/python .

# ---- Stage 2: runtime ----
FROM python:3.14-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/darwin/bin:${PATH}" \
    DARWIN_STATE_DIR=/data

# Bring the prebuilt venv across
COPY --from=builder /opt/darwin /opt/darwin

# State directory (signing key, attestation DB)
RUN mkdir -p /data && chmod 0700 /data

# Non-root user
RUN useradd -r -u 10001 -g 0 -d /home/darwin -m darwin && \
    chown -R darwin:0 /data /home/darwin

USER 10001:0
WORKDIR /home/darwin

EXPOSE 8787

# Bootstrap entrypoint: materializes class signing keys from env vars
# into /data/class-keys before the server starts. Owner-only readable.
USER 0:0
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh
USER 10001:0

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8787/healthz')" || exit 1

ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
CMD ["darwin", "serve", "--host", "0.0.0.0", "--port", "8787"]
