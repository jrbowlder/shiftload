FROM python:3.13-slim

# uv for the same dependency resolution we use locally
COPY --from=ghcr.io/astral-sh/uv:0.9.7 /uv /usr/local/bin/uv

ENV UV_PROJECT_ENVIRONMENT=/app/.venv \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1 \
    TZ=America/Denver

WORKDIR /app

# dependencies first, so a code change does not re-resolve them
COPY pyproject.toml uv.lock ./
# dependencies only: the package itself is not on disk yet
RUN uv sync --frozen --no-dev --no-install-project

COPY shiftload ./shiftload
# now the package exists, so this installs it and its console script
# --no-editable installs a real wheel: an editable .pth in this image is not honoured
RUN uv sync --frozen --no-dev --no-editable

# match the host uid so files written into the bind mount stay editable outside
RUN useradd --uid 1000 --create-home app && chown -R app:app /app
USER app

ENV PATH="/app/.venv/bin:$PATH"
EXPOSE 4534

HEALTHCHECK --interval=60s --timeout=10s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:4534/healthz',timeout=5)"

CMD ["uvicorn", "shiftload.web.app:app", "--host", "0.0.0.0", "--port", "4534", "--log-level", "warning"]
