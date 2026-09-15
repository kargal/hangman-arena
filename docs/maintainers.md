# Maintainer guide

This document is for people running the arena: operating the server, preparing seasons, curating the puzzle
bank and publishing results. Agent-facing documentation lives in the
[agent workspace](https://github.com/kargal/hangman-arena-agent).

* [Repository layout](#repository-layout)
* [Running locally](#running-locally)
* [Admin API](#admin-api)
* [Season schedule preview](#season-schedule-preview)
* [Starting a new season](#starting-a-new-season)
* [Adding puzzles](#adding-puzzles)
* [Results and the leaderboard](#results-and-the-leaderboard)

## Repository layout

```
hangman-arena/
├── arena_server/        FastAPI server, season schedule, session store, event log
├── arena_runner/        agent runner + adapters (claude_code; codex and gemini_cli are experimental)
├── arena_gateway/       Anthropic Messages -> OpenAI Responses gateway (docs/gateway.md)
├── configs/
│   ├── season-N.yaml    season definitions (seed, size, mix, exclusions, workspace ref)
│   ├── agents/          per-agent prompt, model and environment
│   └── gateway/         gateway model routing (models.yaml)
├── puzzles/bank.jsonl   puzzle bank
├── leaderboard/         published results (season-N.json)
├── scripts/             validate_bank.py, score_difficulty.py, render_leaderboard.py
├── docker/              server and agent images, docker-compose.yml
└── tests/
```

The game rules (`hangman_engine`) and the client (`arena` CLI) live in
[hangman-arena-agent](https://github.com/kargal/hangman-arena-agent) and are pinned per season through
`pyproject.toml` and `agent_workspace.ref` in the season config.

## Running locally

```bash
git clone https://github.com/kargal/hangman-arena.git
git clone https://github.com/kargal/hangman-arena-agent.git
cd hangman-arena
python -m venv .venv && source .venv/bin/activate
pip install -e ../hangman-arena-agent      # engine + arena CLI from the sibling checkout
pip install -e ".[dev]"

export ARENA_ADMIN_TOKEN=dev
python -m arena_server --port 8080         # serves the season in $ARENA_SEASON (default 2)
```

Server environment:

| Variable | Default | Meaning |
| --- | --- | --- |
| `ARENA_SEASON` | `2` | season to serve (`configs/season-N.yaml`) |
| `ARENA_HOME` | current directory | arena checkout containing `configs/` and `puzzles/` |
| `ARENA_RESULTS_DIR` | `$ARENA_HOME/results` | where event logs and summaries are written |
| `ARENA_ADMIN_TOKEN` | random (logged at start-up) | bearer token for the admin endpoints |

To play a session by hand, create one with the admin API and point the CLI at it:

```bash
curl -s -X POST localhost:8080/v1/sessions \
     -H "Authorization: Bearer $ARENA_ADMIN_TOKEN" -H 'Content-Type: application/json' \
     -d '{"agent": "manual", "model": null}'
# {"session_id":"ses_…","token":"…","season":2,"total":20,"max_guesses":30}

export ARENA_URL=http://localhost:8080 ARENA_SESSION=ses_… ARENA_TOKEN=…
arena next
```

To run an agent without Docker (the workspace is cloned to `../hangman-arena-agent` — use `--workspace` to put
it somewhere else if you have a development checkout there):

```bash
python -m arena_runner run --agent claude_code --server http://localhost:8080 \
    --workspace /tmp/arena-ws/hangman-arena-agent
```

## Admin API

All admin endpoints require `Authorization: Bearer $ARENA_ADMIN_TOKEN`.

| Method | Path | Body | Returns |
| --- | --- | --- | --- |
| `POST` | `/v1/sessions` | `{"agent", "model", "run_id", "season"}` (all optional) | `{"session_id", "token", "season", "total", "max_guesses"}` |
| `GET` | `/v1/sessions` | — | progress of every session on this server |

Public endpoints: `GET /healthz`, `GET /v1/season`. Session endpoints are documented in the agent workspace's
[`docs/protocol.md`](https://github.com/kargal/hangman-arena-agent/blob/main/docs/protocol.md).

The session token is returned once and only its SHA-256 is stored.

## Season schedule preview

The puzzles of a season are fixed by its config and the bank. Preview them before opening a season (and
whenever you touch the bank) with:

```bash
python -m arena_server.schedule --season 2          # table with answers
python -m arena_server.schedule --season 2 --json   # machine-readable
```

Example (Season 1, archived):

```
Season 1 (pilot) (seed 104729) - 20 puzzles, max 26 guesses each

#   puzzle   type  difficulty  ref  category           answer
1   s1-0885  word  easy        -    Sea Life           flounder
2   s1-0453  word  easy        -    Food               muffin
...
mix: word/easy=6, word/hard=6, word/medium=8
```

Do not post the schedule of an open season anywhere public.

The server serves exactly this list, in this order, to every session.

## Starting a new season

1. Tag the agent workspace release that defines the season's rules (e.g. `v0.3.0`).
2. Copy the previous `configs/season-N.yaml`, bump `season`, pick a fresh `seed`, adjust the mix, and set `agent_workspace.ref` to the new tag.
3. Run `python scripts/validate_bank.py` and preview the schedule. Check that categories are varied and that no
   answer is obscure or ambiguous.
4. Set the previous season's `status: archived` and `closes:` date, and freeze its leaderboard file.
5. Add `leaderboard/season-N.json` with `"status": "in_progress"` and re-render the README.
6. Deploy with `ARENA_SEASON=N`.

Do not edit a season config while the season is open — any change to it, or to bank entries it can draw from,
changes the schedule and makes results incomparable.

## Adding puzzles

The bank is JSON Lines, one puzzle per line:

```json
{"id": "1201", "answer": "Bubble wrap", "type": "phrase", "category": "Things", "difficulty": "medium"}
```

* `id` — next free four-digit id. Ids are never reused or renumbered.
* `answer` — ASCII letters, single spaces, hyphens, apostrophes and commas only. Use standard spelling; prefer
  well-known words and phrases over obscure ones. Proper nouns are fine if they are widely known.
* `type` — `word` (no spaces) or `phrase`.
* `category` — an existing category where possible (`python scripts/validate_bank.py` prints the list size).
* `difficulty` — assigned by `scripts/score_difficulty.py`, not by hand.

Append new entries at the end, then:

```bash
python scripts/score_difficulty.py puzzles/bank.jsonl --write
python scripts/validate_bank.py
pytest tests/test_bank.py tests/test_schedule.py
```

Adding puzzles can change difficulty tiers of existing entries and therefore future schedules; it never
changes the schedule of a season that is already open as long as you add puzzles between seasons.

Difficulty is the number of misses a strict frequency-order guesser (`ETAOINSHRDLCUMWFGYPBVKJXQZ`) makes, with a
small penalty for very short answers, bucketed into easy (bottom 35%), medium (35%) and hard (top 30%).

## Results and the leaderboard

Every session writes `results/season-N/<session_id>.jsonl` (all events) and, when finished,
`<session_id>.summary.json`. The runner additionally stores the agent transcript in `runs/<run_id>/`.

A leaderboard entry is the mean of 5 complete sessions with the same agent and model. Before publishing,
review the transcripts for harness problems (crashes, rate limits, sessions that needed many resumes). Add the
entry to `leaderboard/season-N.json` and run:

```bash
python scripts/render_leaderboard.py
```

CI fails if the README table is out of date.
