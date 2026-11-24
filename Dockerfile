# Fleet Copilot app image: Streamlit UI by default; the same image runs migrations,
# seeding and the nightly maintenance job (see docker-compose.yml).
FROM python:3.12-slim-bookworm

COPY --from=ghcr.io/astral-sh/uv:0.13.9 /uv /usr/local/bin/uv

ENV PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    UV_NO_CACHE=1

WORKDIR /app

# Dependencies first (cached layer), then the project itself.
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --frozen --no-dev --no-install-project

# The app reads config/, db/, data/, evals/ and docs/ relative to the source tree,
# so the whole repository (minus .dockerignore) lives at /app.
COPY . .
RUN uv sync --frozen --no-dev

RUN useradd --system --uid 10001 --no-create-home app
USER app
ENV PATH=/app/.venv/bin:$PATH \
    HOME=/tmp

EXPOSE 8501
HEALTHCHECK --interval=15s --timeout=5s --start-period=30s --retries=5 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8501/_stcore/health', timeout=4)" || exit 1

CMD ["streamlit", "run", "src/fleet_copilot/interfaces/ui/app.py", \
     "--server.address=0.0.0.0", "--server.port=8501", \
     "--server.headless=true", "--server.fileWatcherType=none"]
