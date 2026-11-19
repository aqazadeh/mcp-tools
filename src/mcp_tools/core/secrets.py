"""Provider credentials: from the environment, or from a private file in the data directory.

stdio hosts start the server with a minimal environment, so an API key exported in the
user's shell is often not there. `<data dir>/secrets.env` (0600, KEY=value lines) is the
alternative. Values are never written to the database, to results or to logs.
"""

import os
import stat
from pathlib import Path

from mcp_tools.core.logsafe import register_secret

FILE_NAME = "secrets.env"
KNOWN_KEYS = ("REPLICATE_API_TOKEN", "OPENAI_API_KEY", "OPENAI_ORG_ID", "OPENAI_PROJECT_ID")


class SecretsError(RuntimeError):
    pass


def _read_file(path: Path) -> dict[str, str]:
    try:
        info = path.stat()
    except FileNotFoundError:
        return {}
    if info.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise SecretsError(f"{path} is readable by others; run: chmod 600 {path}")
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        name = name.removeprefix("export ").strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if name in KNOWN_KEYS and value:
            values[name] = value
    return values


def load_secrets(data_dir: Path) -> dict[str, str]:
    """Known provider keys; the environment wins over the file."""
    values = _read_file(data_dir.expanduser() / FILE_NAME)
    for name in KNOWN_KEYS:
        if os.environ.get(name):
            values[name] = os.environ[name]
    for name, value in values.items():
        if name.endswith(("TOKEN", "KEY")):
            register_secret(value)
    return values
