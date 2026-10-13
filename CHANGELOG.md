# Changelog

All notable changes to the arena are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Minor versions track seasons.

## [Unreleased]

## [0.3.0] - 2026-10-13 — Season 3

### Added
- Season 3 config with the new `difficulty_ramp` schedule rule: the 20 puzzles of a session get harder and
  harder — ordinary words and phrases first, wordplay in the last 8 slots — and no puzzle is reused from
  seasons 1–2.
- Puzzle bank grown to ~1,600 entries; new categories (Song Titles, Famous Places, Math & Shapes, Actions) and
  118 wordplay puzzles (Before & After, Rhyme Time, Same Letter, Odd Couples, Odd Things).
- `ref_guesses` in the bank: calibrated reference difficulty (mean guesses of a fixed reference player) of the
  puzzles Season 3 draws from. Session summaries report the difficulty-adjusted learning slope
  (hangman-arena-agent 0.3.0).
- `exclude.seasons`, `max_per_category` and `bank_max_id` in season configs; archived seasons draw only from
  the bank as it was when they opened.
- Runner resumes Claude Code sessions (`--resume`) when the agent stops before the session is complete.
- `gemini_cli` adapter (experimental).
- `arena_gateway`: web search. Claude Code's `WebSearch` tool now works for gateway models: Anthropic web search
  server-tool requests are served by the provider-hosted `web_search` tool of the Responses API and returned as
  `server_tool_use` / `web_search_tool_result` blocks with the real result URLs, plus citations. A failed search is
  returned as a search error, never as model text. Search calls are counted in the request log.
- `gpt-6.1-sol` route in `configs/gateway/models.yaml`.
- Runner sets `ENABLE_TOOL_SEARCH=false` and `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1` for every model, so all
  models see the same tool list.
- Sessions are restored from the event log after a server restart.
- `python -m arena_server.schedule --hide-answers`.

### Changed
- Seasons 1 and 2 were internal pilot seasons; their results are not published. The public leaderboard starts
  with Season 3 and is published when the season closes.
- Difficulty tiers are recomputed per type (words and phrases ranked separately).
- Agent workspace pinned to `hangman-arena-agent` v0.3.0.

### Fixed
- Largest-remainder rounding in the schedule quota could favour easy puzzles because of floating-point ties.

## [0.2.1] - 2026-09-18

### Added
- `arena_gateway`: Anthropic Messages API on top of the OpenAI Responses API (streaming, tool calls, reasoning
  carried across turns via thinking-block signatures, retries, JSONL request log). OpenAI models now run in the
  same Claude Code harness as all other models. Optional `gateway` service in `docker/docker-compose.yml`.
- `configs/gateway/models.yaml` (model routing and per-model reasoning effort).
- `endpoint` field in leaderboard entries.

### Changed
- All leaderboard entries use the Claude Code harness; the `codex` adapter is now marked experimental.

## [0.2.0] - 2026-08-31 — Season 2

### Added
- Multi-word phrases in the bank and a `phrase_share` setting.
- Leaderboard rendering into the README (`scripts/render_leaderboard.py`) and a CI check.
- `codex` adapter; `ANTHROPIC_BASE_URL` / `ANTHROPIC_AUTH_TOKEN` passthrough for models with an
  Anthropic-compatible endpoint (Kimi, GLM, Qwen, DeepSeek) in Claude Code.
- Docker images for the server and the agent sandbox; `docker/docker-compose.yml`.

### Changed
- Guess budget 26 → 30; forfeits scored as 30.
- Event log moved to `results/season-N/<session>.jsonl`.

## [0.1.0] - 2026-08-04 — Season 1 (pilot)

### Added
- Arena server with per-session tokens, deterministic season schedule and JSONL event log.
- Runner with the `claude_code` adapter.
- Initial puzzle bank (single words).

[Unreleased]: https://github.com/kargal/hangman-arena/compare/v0.3.0...HEAD
[0.3.0]: https://github.com/kargal/hangman-arena/compare/v0.2.1...v0.3.0
[0.2.1]: https://github.com/kargal/hangman-arena/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/kargal/hangman-arena/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/kargal/hangman-arena/releases/tag/v0.1.0
