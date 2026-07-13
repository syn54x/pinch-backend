"""Local CLI state: one JSON file in the platform config dir, mode 0600.

Holds the stored token and default server from ``pinch auth login``.
Written 0600 from birth (created via os.open with the mode, never chmod'd
after the secret is inside) because the token is a live credential.
Keyring/OS-credential storage is deliberately deferred (PRD M3).
"""

import json
import os
from pathlib import Path

import platformdirs


def config_path() -> Path:
    """``PINCH_CONFIG_DIR`` overrides the platform default — for tests and
    for self-hosters juggling several servers."""
    override = os.environ.get("PINCH_CONFIG_DIR")
    base = Path(override) if override else Path(platformdirs.user_config_dir("pinch"))
    return base / "config.json"


def load() -> dict:
    try:
        return json.loads(config_path().read_text())
    except FileNotFoundError:
        return {}


def save(config: dict) -> None:
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    # O_CREAT with 0600 so the file is never observable with wider
    # permissions; O_TRUNC because this is a whole-file rewrite.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write(json.dumps(config, indent=2) + "\n")
    path.chmod(0o600)  # heal a pre-existing looser file
