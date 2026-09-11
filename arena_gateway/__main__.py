"""Run the gateway: ``python -m arena_gateway [--host H] [--port P] [--config models.yaml]``."""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

import uvicorn

from arena_gateway.app import create_app
from arena_gateway.config import load_config


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m arena_gateway", description=__doc__)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8090)
    parser.add_argument(
        "--config", type=Path, default=None, help="models.yaml (default: $GATEWAY_CONFIG or configs/gateway)"
    )
    parser.add_argument("--log-level", default="info")
    args = parser.parse_args()

    logging.basicConfig(level=args.log_level.upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    config = load_config(args.config)
    logging.getLogger(__name__).info(
        "arena gateway: %d model(s), default %s, log %s", len(config.models), config.default_model, config.log_file
    )
    uvicorn.run(create_app(config), host=args.host, port=args.port, log_level=args.log_level)


if __name__ == "__main__":
    main()
