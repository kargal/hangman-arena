.PHONY: dev test lint check serve schedule leaderboard

dev:
	pip install -e ../hangman-arena-agent
	pip install -e ".[dev]"

test:
	pytest

lint:
	ruff check .
	ruff format --check .
	mypy

check: lint test
	python scripts/validate_bank.py
	python scripts/render_leaderboard.py --check

serve:
	ARENA_ADMIN_TOKEN=$${ARENA_ADMIN_TOKEN:-dev} python -m arena_server --port 8080

schedule:
	python -m arena_server.schedule --season $${SEASON:-2}

leaderboard:
	python scripts/render_leaderboard.py
