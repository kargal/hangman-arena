#!/usr/bin/env python3
"""Stand-in for the `claude` binary used by runner tests.

Plays up to FAKE_PUZZLES_PER_ATTEMPT puzzles through the arena API by guessing
letters in alphabetical order, prints stream-json records, then exits.
"""

import json
import os
import sys
import urllib.request

url, token, sid = os.environ["ARENA_URL"], os.environ["ARENA_TOKEN"], os.environ["ARENA_SESSION"]
per_attempt = int(os.environ.get("FAKE_PUZZLES_PER_ATTEMPT", "100"))


def call(method: str, path: str, body: dict | None = None) -> dict:
    req = urllib.request.Request(
        f"{url}/v1/sessions/{sid}{path}",
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())


args = sys.argv[1:]
resumed = args[args.index("--resume") + 1] if "--resume" in args else None
session_id = resumed or "fake-session-0001"
print(json.dumps({"type": "system", "subtype": "init", "session_id": session_id, "model": "fake"}))
print(json.dumps({"type": "user", "argv": args}))

played = 0
while played < per_attempt:
    status = call("GET", "")
    if status["session"]["finished"]:
        break
    puzzle = status["puzzle"]
    if puzzle is None or puzzle["status"] != "in_progress":
        puzzle = call("POST", "/next")["puzzle"]
    for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
        if letter in puzzle["guessed_letters"]:
            continue
        puzzle = call("POST", "/guess", {"letter": letter})["puzzle"]
        if puzzle["status"] != "in_progress":
            break
    played += 1

print(
    json.dumps(
        {"type": "result", "subtype": "success", "session_id": session_id, "num_turns": played, "is_error": False}
    )
)
