"""Load the project's local API key without overriding a shell-provided key."""

import os
from pathlib import Path


def load_local_api_key() -> None:
    if os.environ.get("OPENAI_API_KEY"):
        return
    env_path = Path(__file__).resolve().parents[1] / ".env"
    if not env_path.is_file():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        entry = line.strip()
        if entry.startswith("export "):
            entry = entry[7:].lstrip()
        name, separator, value = entry.partition("=")
        if not separator or name.strip() != "OPENAI_API_KEY":
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if value:
            os.environ["OPENAI_API_KEY"] = value
            return
