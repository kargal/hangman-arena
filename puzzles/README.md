# Puzzle bank

`bank.jsonl` holds every puzzle the arena can schedule, one JSON object per line:

| Field | Values |
| --- | --- |
| `id` | four-digit string, stable forever |
| `answer` | ASCII letters, single spaces, `-`, `'`, `,` |
| `type` | `word` or `phrase` |
| `category` | hint shown to the player |
| `difficulty` | `easy`, `medium`, `hard` — computed by `scripts/score_difficulty.py` |
| `ref_guesses` | optional: calibrated reference difficulty (mean guesses of a fixed reference player); only calibrated puzzles can be drawn by the `difficulty_ramp` schedule rule |

Seasons draw from this file deterministically (see `configs/season-N.yaml` and `arena_server/schedule.py`).
See [docs/maintainers.md](../docs/maintainers.md#adding-puzzles) before editing.
