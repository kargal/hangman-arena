"""HTTP error type with the protocol's ``{"error": {"code", "message"}}`` envelope."""

from __future__ import annotations


class ArenaHTTPError(Exception):
    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


def unauthorized(message: str = "missing or invalid token") -> ArenaHTTPError:
    return ArenaHTTPError(401, "unauthorized", message)


def not_found(session_id: str) -> ArenaHTTPError:
    return ArenaHTTPError(404, "session_not_found", f"unknown session {session_id!r}")


def conflict(code: str, message: str) -> ArenaHTTPError:
    return ArenaHTTPError(409, code, message)
