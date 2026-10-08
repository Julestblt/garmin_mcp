FROM python:3.12-slim
WORKDIR /app
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PATH="/app/.venv/bin:$PATH" \
    STRIDE_MODE=1 GARMIN_MCP_TRANSPORT=streamable-http GARMIN_MCP_HOST=0.0.0.0 GARMIN_MCP_PORT=8000
COPY pyproject.toml uv.lock README.md ./
COPY src/ ./src/
RUN uv sync --frozen --no-dev --no-editable && useradd --system --create-home --uid 10001 stride
USER stride
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3)"
ENTRYPOINT ["garmin-mcp"]
