"""Agent configurations (``configs/agents/<name>.yaml``)."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

#: Root of the arena checkout (the directory containing ``configs/``).
ARENA_ROOT = Path(__file__).resolve().parents[1]


class AgentConfigError(ValueError):
    pass


@dataclass(frozen=True)
class AgentConfig:
    name: str
    adapter: str
    model: str | None
    prompt: str
    resume_prompt: str
    max_resumes: int = 5
    attempt_timeout_minutes: float = 90.0
    env_passthrough: tuple[str, ...] = ()
    env: dict[str, str] = field(default_factory=dict)
    extra_args: tuple[str, ...] = ()
    binary: str | None = None

    @classmethod
    def from_dict(cls, name: str, data: dict[str, Any]) -> AgentConfig:
        try:
            return cls(
                name=name,
                adapter=str(data.get("adapter", name)),
                model=str(data["model"]) if data.get("model") else None,
                prompt=str(data["prompt"]).strip(),
                resume_prompt=str(data["resume_prompt"]).strip(),
                max_resumes=int(data.get("max_resumes", 5)),
                attempt_timeout_minutes=float(data.get("attempt_timeout_minutes", 90)),
                env_passthrough=tuple(str(v) for v in data.get("env_passthrough", [])),
                env={str(k): str(v) for k, v in (data.get("env") or {}).items()},
                extra_args=tuple(str(v) for v in data.get("extra_args", [])),
                binary=str(data["binary"]) if data.get("binary") else None,
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise AgentConfigError(f"invalid agent config {name!r}: {exc!r}") from exc

    def render_prompt(self, **values: Any) -> str:
        return self.prompt.format(**values)

    def render_resume_prompt(self, **values: Any) -> str:
        return self.resume_prompt.format(**values)


def agents_dir(root: Path = ARENA_ROOT) -> Path:
    return root / "configs" / "agents"


def available_agents(root: Path = ARENA_ROOT) -> list[str]:
    return sorted(p.stem for p in agents_dir(root).glob("*.yaml"))


def load_agent_config(name: str, root: Path = ARENA_ROOT) -> AgentConfig:
    path = agents_dir(root) / f"{name}.yaml"
    if not path.exists():
        raise AgentConfigError(f"unknown agent {name!r}; available: {', '.join(available_agents(root))}")
    with path.open(encoding="utf-8") as fh:
        return AgentConfig.from_dict(name, yaml.safe_load(fh))
