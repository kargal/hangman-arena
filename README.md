# Hangman Arena

[![CI](https://github.com/kargal/hangman-arena/actions/workflows/ci.yml/badge.svg)](https://github.com/kargal/hangman-arena/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
![Season](https://img.shields.io/badge/season-2-orange)

**Hangman Arena** is a small benchmark for LLM agents built around the oldest word game there is. It measures

1. **efficiency** — how many guesses an agent needs to solve a hangman puzzle, and
2. **in-context learning** — whether the agent gets better over consecutive puzzles within a single session.

Agents play through a command-line tool in a normal repository, the way a coding agent would use any other CLI.
Every agent in a season plays the same 20 puzzles in the same order, so scores are directly comparable across
agents and models.

This repository contains the arena **server**, the **puzzle bank**, **season configs**, the **runner** that
launches agents, and the **leaderboard**. Agents do not work in this repository: they are launched in the separate
[**hangman-arena-agent**](https://github.com/kargal/hangman-arena-agent) workspace, which contains the
`arena` CLI client and the game rules.

* [Why hangman?](#why-hangman)
* [Metric](#metric)
* [Leaderboard](#leaderboard)
* [Architecture](#architecture)
* [Quickstart](#quickstart)
* [Running an agent](#running-an-agent)
* [Seasons](#seasons)

## Why hangman?

Hangman is a tiny sequential decision problem with a clean, objective cost: every guess counts. Doing well
requires calibrated reasoning about letter frequencies and word shapes, using the category hint, and knowing
when the evidence is strong enough to attempt a full solve instead of revealing more letters. It is cheap to run,
impossible to "half-solve", and needs no LLM judge.

Playing 20 puzzles back-to-back in one context also gives a clean view of in-context learning. Puzzles within a
season share a style (difficulty mix, phrase share, kinds of categories), so an agent that notices patterns in
earlier rounds — which letters paid off, how phrases are constructed, how aggressive to be with solve attempts —
should need fewer guesses later on.

## Metric

| Metric | Definition | Better |
| --- | --- | --- |
| **Avg. guesses / puzzle** (primary) | mean over the session of guesses charged per puzzle | lower |
| **Learning-curve slope** | OLS slope of guesses vs. round index (1…20) | more negative |
| Solve rate | fraction of puzzles solved within the budget | higher |

Counting rules, implemented in
[`hangman_engine`](https://github.com/kargal/hangman-arena-agent/tree/main/hangman_engine):

* each letter guess = 1 guess; each full-solve attempt = 1 guess;
* repeated letters and malformed input are rejected and **not** counted;
* revealing every letter solves the puzzle;
* budget of 30 guesses per puzzle — an unsolved puzzle is forfeited and **scored as 30**.

Leaderboard entries report the mean over 5 independent sessions with a 95% confidence interval.
Models run with their default reasoning settings.

## Leaderboard

Seasons 1 and 2 are internal pilot seasons and their results are not published. Results appear here from the
first public season on.

<!-- leaderboard:start -->

<!-- leaderboard:end -->

## Architecture

```
 ┌───────────────────────────── hangman-arena (this repo) ─────────────────────────────┐
 │                                                                                     │
 │  configs/season-2.yaml ─┐                                                           │
 │  puzzles/bank.jsonl ────┼─► arena_server.schedule ──► arena_server (FastAPI) ◄──┐   │
 │                         │      (20 puzzles, fixed order)     │ results/*.jsonl  │   │
 │                                                              │                  │   │
 │  arena_runner ── create session (admin) ─────────────────────┘                  │   │
 │       │                                                                         │   │
 │       │ clone + pip install, launch agent with ARENA_URL / ARENA_TOKEN /        │   │
 │       │ ARENA_SESSION, save runs/<run_id>/                                      │   │
 └───────┼─────────────────────────────────────────────────────────────────────────┼───┘
         ▼                                                                         │
 ┌──── hangman-arena-agent (agent workspace) ────┐                                 │
 │  agent (e.g. Claude Code) runs here           │      HTTP (docs/protocol.md)    │
 │  `arena next / guess / solve / status` ───────┼─────────────────────────────────┘
 │  hangman_engine: rules shared with the server │
 └───────────────────────────────────────────────┘
```

* **`arena_server/`** — FastAPI service. Creates sessions (admin token), serves puzzles in season-schedule order,
  judges every move with `hangman_engine`, and appends every event to `results/season-N/<session>.jsonl`. Answers
  are never sent before a puzzle is finished.
* **`arena_runner/`** — creates a session, prepares a fresh clone of the agent workspace next to this checkout
  (`../hangman-arena-agent`), installs the `arena` CLI, launches the agent via an adapter, and stores transcripts
  under `runs/<run_id>/`.
* **`puzzles/bank.jsonl`** — ~1,200 English words and common phrases with categories and difficulty tiers.
* **`configs/`** — season definitions and per-agent configs (prompt, model, environment).
* **`leaderboard/`** — published results; rendered into this README by `scripts/render_leaderboard.py`.

## Quickstart

Requirements: Docker with Compose v2.

```bash
git clone https://github.com/kargal/hangman-arena.git
cd hangman-arena
export ARENA_ADMIN_TOKEN=$(openssl rand -hex 16)

# start the server for the current season
docker compose -f docker/docker-compose.yml up -d arena
curl -s localhost:8080/healthz
# {"status":"ok","season":2,"version":"0.2.0"}
```

### Local development (no Docker)

```bash
git clone https://github.com/kargal/hangman-arena-agent.git ../hangman-arena-agent
python -m venv .venv && source .venv/bin/activate
pip install -e ../hangman-arena-agent
pip install -e ".[dev]"
ARENA_ADMIN_TOKEN=dev python -m arena_server --port 8080
```

## Running an agent

```bash
export ANTHROPIC_API_KEY=...
docker compose -f docker/docker-compose.yml run --rm agent \
    python -m arena_runner run --agent claude_code --model claude-opus-5
```

The `agent` container mounts this checkout at `/home/agent/hangman-arena`, clones the agent workspace to
`/home/agent/hangman-arena-agent`, and launches the agent there. Results land in `runs/<run_id>/`:

```
runs/20260831-162254-claude_code-claude-opus-5/
├── meta.json                       # run metadata, workspace commit, attempts, final score
├── prompt.txt                      # the exact prompt given to the agent
├── attempts/attempt-01.stdout.jsonl  # agent transcript (stream-json for Claude Code)
├── attempts/attempt-01.stderr.log
├── summary.json                    # server-side session summary
└── history.json                    # per-puzzle answers, guesses, results
```

| Adapter | CLI | Status |
| --- | --- | --- |
| `claude_code` | `claude -p … --output-format stream-json` | default |
| `codex` | `codex exec --json …` | OpenAI models |

The prompt for each agent is in [`configs/agents/`](configs/agents/). It is intentionally short: it names the
season, the number of puzzles, the `arena` CLI and the scoring rule, and points to the workspace README.

## Seasons

A season fixes the puzzle selection and rules for a period of time:

| Season | Dates | Puzzles | Notes |
| --- | --- | --- | --- |
| 1 (pilot) | Aug 4 – Aug 24, 2026 | 20 words | internal pilot, results not published; 26-guess budget |
| **2 (pilot)** | **from Aug 31, 2026** | **15 words + 5 phrases** | internal pilot, results not published; phrases introduced; 30-guess budget |

The schedule is a deterministic function of `configs/season-N.yaml` (seed, phrase share, difficulty mix) and the
puzzle bank — see [`arena_server/schedule.py`](arena_server/schedule.py). Every session in a season gets the same
puzzles in the same order. Season configs are frozen while a season is open.

## License

[MIT](LICENSE)
