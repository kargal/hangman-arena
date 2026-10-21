# Hangman Arena

[![CI](https://github.com/kargal/hangman-arena/actions/workflows/ci.yml/badge.svg)](https://github.com/kargal/hangman-arena/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
![Season](https://img.shields.io/badge/season-3%20(open)-orange)

**Hangman Arena** is a small benchmark for LLM agents built around the oldest word game there is. It measures

1. **efficiency** — how many guesses an agent needs to solve a hangman puzzle, and
2. **in-context learning** — whether the agent gets better over consecutive puzzles within a single session.

Agents play through a command-line tool in a normal repository, the way a coding agent would use any other CLI.
Every agent in a season plays the same 20 puzzles in the same order — from easier to harder — and every
leaderboard entry runs in the same harness (Claude Code), so scores are directly comparable across models.

This repository contains the arena **server**, the **puzzle bank**, **season configs**, the **runner** that
launches agents, the **model gateway**, and the **leaderboard**. Agents do not work in this repository: they are launched in the separate
[**hangman-arena-agent**](https://github.com/kargal/hangman-arena-agent) workspace, which contains the
`arena` CLI client and the game rules.

* [Why hangman?](#why-hangman)
* [Metric](#metric)
* [Leaderboard](#leaderboard)
* [Architecture](#architecture)
* [Quickstart](#quickstart)
* [Running an agent](#running-an-agent)
* [One harness for all models](#one-harness-for-all-models)
* [Seasons](#seasons)
* [Citation](#citation)
* [Contributing](#contributing)

## Why hangman?

Hangman is a tiny sequential decision problem with a clean, objective cost: every guess counts. Doing well
requires calibrated reasoning about letter frequencies and word shapes, using the category hint, and knowing
when the evidence is strong enough to attempt a full solve instead of revealing more letters. It is cheap to run,
impossible to "half-solve", and needs no LLM judge.

Playing 20 puzzles back-to-back in one context also gives a clean view of in-context learning. The puzzles of a
session are served in order of **increasing difficulty**: ordinary words and phrases first, then increasingly
unusual wordplay (Before & After, Rhyme Time, Same Letter, …) where general knowledge helps less and an agent has
to rely on what it has picked up in earlier rounds — which letters paid off, how the puzzles are constructed, how
aggressive to be with solve attempts. Because later puzzles are harder, raw guess counts rise over a session;
in-context learning is therefore measured **relative to each puzzle's difficulty** (see [Metric](#metric)).

## Metric

| Metric | Definition | Better |
| --- | --- | --- |
| **Avg. guesses / puzzle** (primary) | mean over the session of guesses charged per puzzle | lower |
| **Difficulty-adjusted learning slope** | OLS slope of *excess guesses* (guesses − the puzzle's reference difficulty) vs. round index (1…20) | more negative |
| Solve rate | fraction of puzzles solved within the budget | higher |

Every scheduled puzzle has a **reference difficulty** (`ref_guesses` in the bank): the mean number of guesses a
fixed reference player — a smaller model playing a scripted hangman loop, two games per puzzle — needed to solve
it. Subtracting it removes the difficulty ramp from the learning curve: a slope of 0 means the agent kept the same
margin over the reference throughout the session, a negative slope means it did increasingly better than the
reference as the session went on. Session summaries also report the mean excess guesses and the raw slope of
guesses vs. round index (which mostly reflects the ramp).

Counting rules, implemented in
[`hangman_engine`](https://github.com/kargal/hangman-arena-agent/tree/main/hangman_engine):

* each letter guess = 1 guess; each full-solve attempt = 1 guess;
* repeated letters and malformed input are rejected and **not** counted;
* revealing every letter solves the puzzle;
* budget of 30 guesses per puzzle — an unsolved puzzle is forfeited and **scored as 30**.

Leaderboard entries report the mean over 5 independent sessions with a 95% confidence interval. All entries
use Claude Code as the agent harness, with each model's default reasoning settings.

## Leaderboard

<!-- leaderboard:start -->
### Season 3 (in progress, opened 2026-10-13)

Harness: **Claude Code** for every entry.

_Seasons 1 and 2 were internal pilot seasons and their results were not published. Season 3 is the first public season; results are published when it closes._
<!-- leaderboard:end -->

Want an entry on the board? See [Submitting results](CONTRIBUTING.md#submitting-results).

## Architecture

```
 ┌───────────────────────────── hangman-arena (this repo) ─────────────────────────────┐
 │                                                                                     │
 │  configs/season-3.yaml ─┐                                                           │
 │  puzzles/bank.jsonl ────┼─► arena_server.schedule ──► arena_server (FastAPI) ◄──┐   │
 │                         │      (20 puzzles, fixed order)     │ results/*.jsonl  │   │
 │                                                              │                  │   │
 │  arena_runner ── create session (admin) ─────────────────────┘                  │   │
 │       │                                                                         │   │
 │       │ clone + pip install, launch agent with ARENA_URL / ARENA_TOKEN /        │   │
 │       │ ARENA_SESSION, resume if it stops early, save runs/<run_id>/            │   │
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
* **`arena_gateway/`** — serves the Anthropic Messages API on top of the OpenAI Responses API, so OpenAI models
  run in the same Claude Code harness as everything else ([docs/gateway.md](docs/gateway.md)).
* **`puzzles/bank.jsonl`** — ~1,600 English words, common phrases and wordplay puzzles with categories, difficulty
  tiers and, for calibrated puzzles, reference difficulties.
* **`configs/`** — season definitions and per-agent configs (prompt, model, environment).
* **`leaderboard/`** — published results (from Season 3 on); rendered into this README by `scripts/render_leaderboard.py`.

## Quickstart

Requirements: Docker with Compose v2.

```bash
git clone https://github.com/kargal/hangman-arena.git
cd hangman-arena
export ARENA_ADMIN_TOKEN=$(openssl rand -hex 16)

# start the server for the current season
docker compose -f docker/docker-compose.yml up -d arena
curl -s localhost:8080/healthz
# {"status":"ok","season":3,"version":"0.3.2"}
```

### Local development (no Docker)

```bash
git clone https://github.com/kargal/hangman-arena-agent.git ../hangman-arena-agent
python -m venv .venv && source .venv/bin/activate
pip install -e ../hangman-arena-agent
pip install -e ".[dev]"
ARENA_ADMIN_TOKEN=dev python -m arena_server --port 8080
```

See [docs/maintainers.md](docs/maintainers.md) for playing a session by hand against a local server.

## Running an agent

```bash
export ANTHROPIC_API_KEY=...
docker compose -f docker/docker-compose.yml run --rm agent \
    python -m arena_runner run --agent claude_code --model claude-opus-5
```

The `agent` container mounts this checkout at `/home/agent/hangman-arena`, clones the agent workspace to
`/home/agent/hangman-arena-agent`, and launches the agent there. Results land in `runs/<run_id>/`:

```
runs/20261014-091244-claude_code-claude-opus-5/
├── meta.json                       # run metadata, workspace commit, attempts, final score
├── prompt.txt                      # the exact prompt given to the agent
├── attempts/attempt-01.stdout.jsonl  # agent transcript (stream-json for Claude Code)
├── attempts/attempt-01.stderr.log
├── summary.json                    # server-side session summary
└── history.json                    # per-puzzle answers, guesses, results
```

If an agent exits before finishing all puzzles, adapters that support it (currently `claude_code`) resume the
**same** conversation with a short "N puzzles remain" message, up to `max_resumes` times.

| Adapter | CLI | Resume | Status |
| --- | --- | --- | --- |
| `claude_code` | `claude -p … --output-format stream-json` | yes | used for all leaderboard entries |
| `codex` | `codex exec --json …` | no | experimental |
| `gemini_cli` | `gemini -p … --yolo` | no | experimental |

The prompt for each agent is in [`configs/agents/`](configs/agents/). It is intentionally short: it names the
season, the number of puzzles, the `arena` CLI and the scoring rule, and points to the workspace README.

## One harness for all models

Every leaderboard entry is run in **Claude Code**, headless, with the same prompt and the same settings
(`configs/agents/claude_code.yaml`). Only the model behind it changes:

* **Claude models** — the Anthropic API.
* **Models with an Anthropic-compatible endpoint** (Kimi, GLM, Qwen, DeepSeek, …) — the provider's endpoint, via
  `ANTHROPIC_BASE_URL` / `ANTHROPIC_AUTH_TOKEN` (both are passed through to the agent).
* **OpenAI models** — the [arena gateway](docs/gateway.md), our own small Anthropic-Messages → OpenAI-Responses
  translation server. It maps tools, streaming and stop reasons, and carries encrypted reasoning state across
  turns through thinking-block signatures, so the model keeps its reasoning between tool calls.

```bash
export OPENAI_API_KEY=sk-... GATEWAY_API_KEY=$(openssl rand -hex 16)
docker compose -f docker/docker-compose.yml --profile gateway up -d arena gateway
ANTHROPIC_BASE_URL=http://gateway:8090 ANTHROPIC_AUTH_TOKEN=$GATEWAY_API_KEY \
  docker compose -f docker/docker-compose.yml run --rm agent \
  python -m arena_runner run --agent claude_code --model gpt-5.6-sol
```

Reasoning effort is each model's default (set per model in `configs/gateway/models.yaml` for gateway models).

## Seasons

A season fixes the puzzle selection and rules for a period of time:

| Season | Dates | Puzzles | Notes |
| --- | --- | --- | --- |
| 1 (pilot) | Aug 4 – Aug 24, 2026 | 20 words | internal pilot, results not published; 26-guess budget |
| 2 (pilot) | Aug 31 – Oct 4, 2026 | 15 words + 5 phrases | internal pilot, results not published; phrases introduced; 30-guess budget |
| **3** | **from Oct 13, 2026** | **12 words & phrases, then 8 wordplay** | first public season; difficulty ramp (easier → harder); no puzzles reused from seasons 1–2 |

The schedule is a deterministic function of `configs/season-N.yaml` (schedule rule, seed and its settings) and the
puzzle bank — see [`arena_server/schedule.py`](arena_server/schedule.py). Season 3 uses the **difficulty ramp**
rule: the target reference difficulty rises linearly from the first to the last puzzle, each slot is filled with
the closest calibrated puzzle, and the last slots are reserved for wordplay categories. Every session in a season
gets the same puzzles in the same order. Season configs are frozen while a season is open.

## Citation

```bibtex
@misc{hangmanarena2026,
  title        = {Hangman Arena: Measuring Guess Efficiency and In-Context Learning of LLM Agents},
  author       = {{Hangman Arena maintainers}},
  year         = {2026},
  howpublished = {\url{https://github.com/kargal/hangman-arena}},
  note         = {Season 3}
}
```

## Contributing

Bug reports, new adapters and puzzle-bank fixes are welcome — see [CONTRIBUTING.md](CONTRIBUTING.md). Changes to
the game rules go to [hangman-arena-agent](https://github.com/kargal/hangman-arena-agent) and take effect at
the next season.

## License

[MIT](LICENSE)
