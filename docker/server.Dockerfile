# Hangman Arena server.
#   docker build -f docker/server.Dockerfile -t hangman-arena-server .
FROM python:3.11-slim-bookworm

ARG AGENT_WORKSPACE_PKG="hangman-arena-agent @ git+https://github.com/kargal/hangman-arena-agent.git@v0.3.0"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    ARENA_HOME=/srv/arena \
    ARENA_RESULTS_DIR=/srv/arena/results \
    ARENA_SEASON=3

RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /srv/arena

# Game engine first (changes rarely), then the server package.
RUN pip install "${AGENT_WORKSPACE_PKG}"
COPY pyproject.toml README.md LICENSE ./
COPY arena_server ./arena_server
COPY arena_runner ./arena_runner
RUN pip install --no-deps . \
    && pip install "fastapi>=0.110,<1" "uvicorn>=0.29" "pydantic>=2.6,<3" "pyyaml>=6" "httpx>=0.27,<1"

COPY configs ./configs
COPY puzzles ./puzzles

RUN useradd --create-home --uid 1000 arena && mkdir -p results && chown arena:arena results
USER arena

EXPOSE 8080
HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=5 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=2)"

CMD ["python", "-m", "arena_server", "--host", "0.0.0.0", "--port", "8080"]
