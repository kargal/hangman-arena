# Contributing to Hangman Arena

Thanks for your interest! Contributions of all sizes are welcome: bug reports, new agent adapters, puzzle-bank
fixes, documentation.

## Where things go

| Change | Repository |
| --- | --- |
| Game rules, scoring, the `arena` CLI, protocol docs | [hangman-arena-agent](https://github.com/kargal/hangman-arena-agent) |
| Server, runner, adapters, puzzle bank, seasons, leaderboard | this repository |

Rule changes only take effect at the start of a season, so that results within a season stay comparable.

## Development setup

```bash
git clone https://github.com/kargal/hangman-arena-agent.git
git clone https://github.com/kargal/hangman-arena.git
cd hangman-arena
python -m venv .venv && source .venv/bin/activate
pip install -e ../hangman-arena-agent
pip install -e ".[dev]"
pytest
ruff check . && ruff format --check . && mypy
```

## Pull requests

* Keep PRs focused; add or update tests for behaviour changes.
* Run the checks above, plus `python scripts/validate_bank.py` if you touched `puzzles/`.
* Do not modify the config of an open season or bank entries it can draw from. Puzzle additions are merged
  between seasons.
* Please do not post schedules of an open season in issues or PRs.

## Adding an agent adapter

Additional adapters are welcome.

1. Subclass `arena_runner.adapters.base.Adapter` and implement `build_command()`; implement
   `parse_transcript()` and set `supports_resume = True` if the agent can continue a conversation.
2. Register it in `arena_runner/adapters/__init__.py`.
3. Add `configs/agents/<name>.yaml` with the standard prompt.
4. Add tests in `tests/test_runner.py`.

## Submitting results

Leaderboard entries are run headless with the `claude_code` adapter (Claude Code; models with an
Anthropic-compatible endpoint use the provider's endpoint) or, for OpenAI models, the `codex` adapter.
Reasoning effort is the model's default.

Open an issue with the "Leaderboard submission" title prefix and include:

* adapter, agent CLI version, model id and endpoint;
* five complete `runs/<run_id>/` directories for the current season (`meta.json`, `summary.json`,
  `history.json` and transcripts);
* the server's `results/season-N/*.summary.json` for those sessions.

Maintainers re-check the event logs and add accepted entries to `leaderboard/season-N.json`.

## Code of conduct

Be kind and constructive. Harassment of any kind is not tolerated.
