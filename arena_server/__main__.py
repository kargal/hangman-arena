"""Run the arena server: ``python -m arena_server [--host H] [--port P]``."""

from __future__ import annotations

import argparse
import logging

import uvicorn

from arena_server.app import create_app
from arena_server.config import Settings


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m arena_server", description="Run the Hangman Arena server.")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--log-level", default="info")
    args = parser.parse_args()

    logging.basicConfig(level=args.log_level.upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    app = create_app(Settings.from_env())
    uvicorn.run(app, host=args.host, port=args.port, log_level=args.log_level)


if __name__ == "__main__":
    main()
