"""Read local configuration without mutating process environment."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def read_environment(path=None):
    config = {}
    env_file = Path(path) if path else ROOT / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                config[key.strip()] = value.strip().strip("\"'")
    config.update(os.environ)
    return config
