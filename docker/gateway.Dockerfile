# Arena gateway: Anthropic Messages API on top of the OpenAI Responses API.
#   docker build -f docker/gateway.Dockerfile -t hangman-arena-gateway .
FROM python:3.11-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    GATEWAY_CONFIG=/srv/gateway/configs/gateway/models.yaml \
    GATEWAY_LOG_FILE=/srv/gateway/logs/gateway.jsonl

WORKDIR /srv/gateway
RUN pip install "fastapi>=0.110,<1" "uvicorn>=0.29" "httpx>=0.27,<1" "pyyaml>=6" "pydantic>=2.6,<3"
COPY pyproject.toml README.md LICENSE ./
COPY arena_gateway ./arena_gateway
COPY arena_server ./arena_server
COPY arena_runner ./arena_runner
RUN pip install --no-deps .
COPY configs/gateway ./configs/gateway

RUN useradd --create-home --uid 1000 gateway && mkdir -p logs && chown gateway:gateway logs
USER gateway

EXPOSE 8090
HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=5 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8090/healthz', timeout=2)"

CMD ["python", "-m", "arena_gateway", "--host", "0.0.0.0", "--port", "8090"]
