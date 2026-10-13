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
│   ├── season-N.yaml    season definitions (rule, seed, size, mix/ramp, exclusions, workspace ref)
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
python -m arena_server --port 8080         # serves the season in $ARENA_SEASON (default 3)
```

Server environment:

| Variable | Default | Meaning |
| --- | --- | --- |
| `ARENA_SEASON` | `3` | season to serve (`configs/season-N.yaml`) |
| `ARENA_HOME` | current directory | arena checkout containing `configs/` and `puzzles/` |
| `ARENA_RESULTS_DIR` | `$ARENA_HOME/results` | where event logs and summaries are written |
| `ARENA_ADMIN_TOKEN` | random (logged at start-up) | bearer token for the admin endpoints |

To play a session by hand, create one with the admin API and point the CLI at it:

```bash
curl -s -X POST localhost:8080/v1/sessions \
     -H "Authorization: Bearer $ARENA_ADMIN_TOKEN" -H 'Content-Type: application/json' \
     -d '{"agent": "manual", "model": null}'
# {"session_id":"ses_…","token":"…","season":3,"total":20,"max_guesses":30}

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

The session token is returned once and only its SHA-256 is stored. Sessions survive a server restart: on
start-up the server replays every `results/season-N/ses_*.jsonl` log for the season it serves.

## Season schedule preview

The puzzles of a season are fixed by its config and the bank. Preview them before opening a season (and
whenever you touch the bank) with:

```bash
python -m arena_server.schedule --season 3                 # table with answers
python -m arena_server.schedule --season 3 --hide-answers  # safe to paste into issues
python -m arena_server.schedule --season 3 --json          # machine-readable
```

Example (answers hidden):

```
Season 3 (seed 730241) - 20 puzzles, max 30 guesses each
rule: difficulty_ramp, target reference difficulty 1 -> 15 guesses

#   puzzle   type    difficulty  ref   category             answer
1   s3-1152  word    easy        1.0   Sea Life             ***
2   s3-0507  phrase  medium      1.5   Song Titles          ****************
...
19  s3-1534  phrase  medium      14.5  Before & After       *************
20  s3-1594  phrase  hard        14.5  Same Letter          **********
mix: phrase/easy=3, phrase/hard=6, phrase/medium=4, word/easy=2, word/hard=4, word/medium=1
```

`ref` is the calibrated reference difficulty (`ref_guesses`); under the `difficulty_ramp` rule it never decreases
from one puzzle to the next. The server serves exactly this list, in this order, to every session. Schedules of
earlier seasons listed under `exclude.seasons` are recomputed and their puzzles removed from the pool, so no puzzle
is ever reused.

## Starting a new season

1. Tag the agent workspace release that defines the season's rules (e.g. `v0.4.0`).
2. Copy the previous `configs/season-N.yaml`, bump `season`, pick a fresh `seed`, adjust the mix or ramp, add the
   previous season to `exclude.seasons`, set `bank_max_id` to the last id in the bank, and set
   `agent_workspace.ref` to the new tag. A `difficulty_ramp` season needs calibrated `ref_guesses` for enough
   puzzles across the whole target range (see [Adding puzzles](#adding-puzzles)).
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
{"id": "1631", "answer": "Bubble wrap", "type": "phrase", "category": "Things", "difficulty": "medium"}
```

* `id` — next free four-digit id. Ids are never reused or renumbered.
* `answer` — ASCII letters, single spaces, hyphens, apostrophes and commas only. Use standard spelling; prefer
  well-known words and phrases over obscure ones. Proper nouns are fine if they are widely known.
* `type` — `word` (no spaces) or `phrase`.
* `category` — an existing category where possible (`python scripts/validate_bank.py` prints the list size).
* `difficulty` — assigned by `scripts/score_difficulty.py`, not by hand.
* `ref_guesses` (optional) — calibrated reference difficulty: the mean number of guesses a fixed reference player
  (a smaller model in a scripted hangman loop, two games per puzzle) needed. It is measured with the maintainers'
  calibration harness, never set by hand, and only calibrated puzzles can be drawn by the `difficulty_ramp` rule.
  Session summaries use it for the difficulty-adjusted learning slope.

Append new entries at the end, then:

```bash
python scripts/score_difficulty.py puzzles/bank.jsonl --write
python scripts/validate_bank.py
pytest tests/test_bank.py tests/test_schedule.py
```

Adding puzzles can change difficulty tiers of existing entries and therefore future schedules; it never
changes the schedule of a season that is already open as long as you add puzzles between seasons.

Difficulty is the number of misses a strict frequency-order guesser (`ETAOINSHRDLCUMWFGYPBVKJXQZ`) makes, with a
small penalty for very short answers, bucketed per type into easy (bottom 35%), medium (35%) and hard (top 30%).
Tiers are a cheap, reproducible heuristic used by the `mix` rule; the `difficulty_ramp` rule orders puzzles by the
measured `ref_guesses` instead.

## Results and the leaderboard

Every session writes `results/season-N/<session_id>.jsonl` (all events) and, when finished,
`<session_id>.summary.json`. The runner additionally stores the agent transcript in `runs/<run_id>/`.

Seasons 1 and 2 were internal pilot seasons; their results were not published and they have no leaderboard file.
From Season 3 on, results are published when the season closes.

A leaderboard entry is the mean of 5 complete sessions with the same agent and model. Before publishing,
review the transcripts for harness problems (crashes, rate limits, sessions that needed many resumes). Add the
entry to `leaderboard/season-N.json` and run:

```bash
python scripts/render_leaderboard.py
```

CI fails if the README table is out of date.
