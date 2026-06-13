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
from pathlib import Path

CONFIG_PATH = Path.home() / ".config" / "goodreads-mcp" / "config.json"


def _load_config_file() -> dict:
    try:
        return json.loads(CONFIG_PATH.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def load_user_id() -> str | None:
    return os.environ.get("GOODREADS_USER_ID") or _load_config_file().get("user_id")
