# Changelog

All notable changes to the arena are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Minor versions track seasons.

## [Unreleased]

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

[Unreleased]: https://github.com/kargal/hangman-arena/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/kargal/hangman-arena/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/kargal/hangman-arena/releases/tag/v0.1.0
