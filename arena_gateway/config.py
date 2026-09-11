"""Gateway configuration (``configs/gateway/models.yaml``).

Values of the form ``${VAR}`` or ``${VAR:-default}`` are expanded from the
environment when the file is loaded.
"""

from __future__ import annotations

import fnmatch
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

DEFAULT_CONFIG = Path(__file__).resolve().parents[1] / "configs" / "gateway" / "models.yaml"
_ENV_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


class GatewayConfigError(ValueError):
    pass


def expand_env(value: Any) -> Any:
    if isinstance(value, str):
        return _ENV_RE.sub(lambda m: os.environ.get(m.group(1)) or (m.group(2) or ""), value)
    if isinstance(value, dict):
        return {k: expand_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [expand_env(v) for v in value]
    return value


@dataclass(frozen=True)
class Provider:
    name: str
    base_url: str
    api_key: str
    timeout_s: float = 900.0
    max_retries: int = 6
    prompt_cache_key: bool = True
    extra_headers: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ModelRoute:
    """How one arena model name is served."""

    name: str
    provider: Provider
    upstream_model: str
    reasoning: bool = True
    reasoning_effort: str | None = None
    reasoning_summary: str | None = "auto"
    max_output_tokens: int | None = None  # upstream cap
    context_window: int | None = None


@dataclass(frozen=True)
class GatewayConfig:
    default_model: str
    models: dict[str, ModelRoute]
    aliases: dict[str, str]
    max_output_tokens_floor: int = 64000
    log_file: Path | None = None
    log_bodies: bool = False
    api_key: str | None = None  # if set, clients must present it
    ping_interval_s: float = 10.0

    def resolve(self, requested: str | None) -> ModelRoute:
        """Map the model name sent by the client to a route; unknown names use the default."""
        name = (requested or "").strip()
        name = re.sub(r"\[[^\]]*\]$", "", name)  # e.g. "gpt-5.6-sol[1m]"
        if name in self.models:
            return self.models[name]
        for pattern, target in self.aliases.items():
            if fnmatch.fnmatchcase(name, pattern):
                return self.models[target]
        return self.models[self.default_model]


def _bool(value: Any, default: bool) -> bool:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def load_config(path: Path | None = None) -> GatewayConfig:
    path = path or Path(os.environ.get("GATEWAY_CONFIG", DEFAULT_CONFIG))
    try:
        raw = expand_env(yaml.safe_load(path.read_text(encoding="utf-8")))
    except OSError as exc:
        raise GatewayConfigError(f"cannot read gateway config {path}: {exc}") from exc

    try:
        providers = {
            name: Provider(
                name=name,
                base_url=str(p["base_url"]).rstrip("/"),
                api_key=str(p.get("api_key") or ""),
                timeout_s=float(p.get("timeout_s", 900)),
                max_retries=int(p.get("max_retries", 6)),
                prompt_cache_key=_bool(p.get("prompt_cache_key"), True),
                extra_headers={str(k): str(v) for k, v in (p.get("extra_headers") or {}).items()},
            )
            for name, p in raw["providers"].items()
        }
        models = {}
        for name, m in raw["models"].items():
            if m["provider"] not in providers:
                raise GatewayConfigError(f"model {name!r}: unknown provider {m['provider']!r}")
            models[name] = ModelRoute(
                name=name,
                provider=providers[m["provider"]],
                upstream_model=str(m.get("upstream_model", name)),
                reasoning=_bool(m.get("reasoning"), True),
                reasoning_effort=m.get("reasoning_effort"),
                reasoning_summary=m.get("reasoning_summary", "auto"),
                max_output_tokens=int(m["max_output_tokens"]) if m.get("max_output_tokens") else None,
                context_window=int(m["context_window"]) if m.get("context_window") else None,
            )
        aliases = {str(k): str(v) for k, v in (raw.get("aliases") or {}).items()}
        default_model = str(raw["default_model"])
    except (KeyError, TypeError, ValueError) as exc:
        raise GatewayConfigError(f"invalid gateway config {path}: {exc!r}") from exc

    for pattern, target in aliases.items():
        if target not in models:
            raise GatewayConfigError(f"alias {pattern!r} points to unknown model {target!r}")
    if default_model not in models:
        raise GatewayConfigError(f"default_model {default_model!r} is not defined")

    log_file = os.environ.get("GATEWAY_LOG_FILE") or raw.get("log_file")
    return GatewayConfig(
        default_model=default_model,
        models=models,
        aliases=aliases,
        max_output_tokens_floor=int(
            os.environ.get("GATEWAY_MAX_OUTPUT_TOKENS_FLOOR") or raw.get("max_output_tokens_floor", 64000)
        ),
        log_file=Path(log_file) if log_file else None,
        log_bodies=_bool(os.environ.get("GATEWAY_LOG_BODIES"), _bool(raw.get("log_bodies"), False)),
        api_key=os.environ.get("GATEWAY_API_KEY") or raw.get("api_key") or None,
        ping_interval_s=float(raw.get("ping_interval_s", 10.0)),
    )
