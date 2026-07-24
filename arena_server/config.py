"""Server settings and season configuration."""

from __future__ import annotations

import logging
import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger(__name__)


class ConfigError(ValueError):
    """A season config file is missing or invalid."""


#: Schedule rules (see :mod:`arena_server.schedule`).
SCHEDULE_RULES = ("mix", "difficulty_ramp")


@dataclass(frozen=True)
class RampConfig:
    """Settings of the ``difficulty_ramp`` schedule rule."""

    start: float  # target reference difficulty (guesses) of the first puzzle
    end: float  # target reference difficulty of the last puzzle
    wordplay_slots: int = 0  # the last N slots are filled from ``wordplay_categories``
    wordplay_categories: tuple[str, ...] = ()

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RampConfig:
        return cls(
            start=float(data["start"]),
            end=float(data["end"]),
            wordplay_slots=int(data.get("wordplay_slots", 0)),
            wordplay_categories=tuple(str(c) for c in data.get("wordplay_categories", [])),
        )


@dataclass(frozen=True)
class SeasonConfig:
    season: int
    name: str
    status: str
    bank: Path
    seed: int
    puzzles_per_session: int
    max_guesses: int
    phrase_share: float
    difficulty: dict[str, float]
    max_per_category: int = 2
    rule: str = "mix"
    ramp: RampConfig | None = None
    exclude_seasons: tuple[int, ...] = ()
    bank_max_id: int | None = None  # draw only from bank ids <= this (the bank as of the season's opening)
    opens: str | None = None
    closes: str | None = None
    agent_workspace: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any], *, home: Path) -> SeasonConfig:
        try:
            rule = str(data.get("schedule", "mix"))
            mix = data.get("mix", {})
            ramp = RampConfig.from_dict(data["ramp"]) if rule == "difficulty_ramp" else None
            difficulty = {str(k): float(v) for k, v in mix["difficulty"].items()} if rule == "mix" else {}
            max_per_category = (data["ramp"] if ramp else mix).get("max_per_category", 2)
            config = cls(
                season=int(data["season"]),
                name=str(data.get("name", f"Season {data['season']}")),
                status=str(data.get("status", "active")),
                bank=(home / data["bank"]).resolve(),
                seed=int(data["seed"]),
                puzzles_per_session=int(data["puzzles_per_session"]),
                max_guesses=int(data.get("max_guesses", 30)),
                phrase_share=float(mix.get("phrase_share", 0.0)),
                difficulty=difficulty,
                max_per_category=int(max_per_category),
                rule=rule,
                ramp=ramp,
                exclude_seasons=tuple(int(s) for s in data.get("exclude", {}).get("seasons", [])),
                bank_max_id=int(data["bank_max_id"]) if data.get("bank_max_id") is not None else None,
                opens=str(data["opens"]) if data.get("opens") else None,
                closes=str(data["closes"]) if data.get("closes") else None,
                agent_workspace={str(k): str(v) for k, v in data.get("agent_workspace", {}).items()},
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ConfigError(f"invalid season config: {exc!r}") from exc
        if rule not in SCHEDULE_RULES:
            raise ConfigError(f"season {config.season}: unknown schedule rule {rule!r}")
        if ramp is not None and (ramp.start > ramp.end or not 0 <= ramp.wordplay_slots <= config.puzzles_per_session):
            raise ConfigError(f"season {config.season}: invalid ramp settings")
        if rule == "mix" and abs(sum(difficulty.values()) - 1.0) > 1e-6:
            raise ConfigError(f"season {config.season}: difficulty mix must sum to 1.0")
        if not 0.0 <= config.phrase_share <= 1.0:
            raise ConfigError(f"season {config.season}: phrase_share must be in [0, 1]")
        if config.season in config.exclude_seasons:
            raise ConfigError(f"season {config.season} cannot exclude itself")
        return config


def season_config_path(season: int, *, home: Path) -> Path:
    return home / "configs" / f"season-{season}.yaml"


def load_season(season: int, *, home: Path) -> SeasonConfig:
    path = season_config_path(season, home=home)
    if not path.exists():
        raise ConfigError(f"no config for season {season} at {path}")
    with path.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    return SeasonConfig.from_dict(data, home=home)


@dataclass(frozen=True)
class Settings:
    """Process-level settings, normally read from the environment."""

    home: Path
    season: int
    results_dir: Path
    admin_token: str

    @classmethod
    def from_env(cls) -> Settings:
        home = Path(os.environ.get("ARENA_HOME", Path.cwd())).resolve()
        admin_token = os.environ.get("ARENA_ADMIN_TOKEN", "")
        if not admin_token:
            admin_token = secrets.token_urlsafe(24)
            log.warning("ARENA_ADMIN_TOKEN is not set; generated a temporary admin token: %s", admin_token)
        return cls(
            home=home,
            season=int(os.environ.get("ARENA_SEASON", "3")),
            results_dir=Path(os.environ.get("ARENA_RESULTS_DIR", home / "results")).resolve(),
            admin_token=admin_token,
        )
