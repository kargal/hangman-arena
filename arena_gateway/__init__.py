"""Arena gateway: serve the Anthropic Messages API on top of the OpenAI Responses API.

The arena evaluates every model with the same harness (Claude Code). Models that
are not reachable through an Anthropic-compatible endpoint are served through this
gateway, which translates requests, streaming events, tool calls and reasoning
state between the two APIs. See ``docs/gateway.md``.
"""

__version__ = "0.3.0"
