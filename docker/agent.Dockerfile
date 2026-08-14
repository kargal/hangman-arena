# Agent sandbox: Python 3.11, Node.js, git and Claude Code.
# The arena checkout is mounted at /home/agent/hangman-arena at runtime and the runner
# clones the agent workspace to /home/agent/hangman-arena-agent.
#   docker build -f docker/agent.Dockerfile -t hangman-arena-agent-sandbox .
FROM python:3.11-slim-bookworm

ARG NODE_MAJOR=22
ARG CLAUDE_CODE_VERSION=latest
ARG UID=1000

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DEBIAN_FRONTEND=noninteractive

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl git gnupg less procps ripgrep jq \
    && curl -fsSL https://deb.nodesource.com/setup_${NODE_MAJOR}.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

RUN npm install -g "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" \
    && npm cache clean --force

# Claude Code refuses --dangerously-skip-permissions as root, so agents run as an unprivileged user.
RUN useradd --create-home --uid ${UID} --shell /bin/bash agent
USER agent
WORKDIR /home/agent

# Virtualenv owned by the agent user; the runner pip-installs the workspace (the `arena` CLI) into it.
ENV VIRTUAL_ENV=/home/agent/.venv \
    PATH=/home/agent/.venv/bin:$PATH
RUN python -m venv "$VIRTUAL_ENV" \
    && pip install --no-cache-dir "httpx>=0.27,<1" "pyyaml>=6"

RUN git config --global user.name "arena-agent" \
    && git config --global user.email "agent@hangman-arena.invalid" \
    && git config --global init.defaultBranch main

WORKDIR /home/agent/hangman-arena
CMD ["python", "-m", "arena_runner", "--help"]
