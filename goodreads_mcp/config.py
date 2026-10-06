"""Configuration.

This is a read-only server — no credentials, no cookies, no login. The only
setting is your numeric Goodreads profile id, used by the RSS shelf tools
(they're addressed by user id).

GOODREADS_USER_ID (or "user_id" in the config file) is the number in
goodreads.com/user/show/<ID>-yourname. It can also be passed per-call.

Config file (optional): ~/.config/goodreads-mcp/config.json
  {"user_id": "12345678"}
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

CONFIG_PATH = Path.home() / ".config" / "goodreads-mcp" / "config.json"

_EXAMPLE = '{"user_id": "12345678"}'


def _warn(problem: str) -> None:
    # stdout is the MCP transport; stderr is what the client shows as the
    # server log, so this is where a misconfiguration can actually be seen.
    print(f"goodreads-mcp: ignoring {CONFIG_PATH}: {problem}", file=sys.stderr)


def _load_config_file() -> dict:
    """Return the config file as a dict, or ``{}`` if it is absent or unusable.

    ``server.py`` calls this at import time, so nothing here may raise: a bad
    file has to degrade to "no user id configured" (the shelf tools then say
    so per call) rather than kill the process before the MCP handshake.
    """
    try:
        data = json.loads(CONFIG_PATH.read_text())
    except FileNotFoundError:
        return {}
    except OSError as e:  # unreadable: permissions, a directory in its place, ...
        _warn(e.strerror or str(e))
        return {}
    except ValueError as e:  # not JSON at all
        _warn(f"not valid JSON ({e})")
        return {}
    if not isinstance(data, dict):
        _warn(f"expected a JSON object like {_EXAMPLE}, got {type(data).__name__}")
        return {}
    return data


def load_user_id() -> str | None:
    env = os.environ.get("GOODREADS_USER_ID")
    if env:
        return env
    uid = _load_config_file().get("user_id")
    if uid is None or uid == "":
        return None
    # A bare number is the README example without its quotes; accept it. Any
    # other shape is a mistake, not an id.
    if isinstance(uid, bool) or not isinstance(uid, (str, int)):
        _warn(f'"user_id" must be a string like {_EXAMPLE}, got {type(uid).__name__}')
        return None
    return str(uid)
