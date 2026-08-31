"""Configuration from the environment (optionally via a .env file).

Loopback bind, gitignored data dir, dotenv that never overrides a real env var.
SIMPLEFIN_ACCESS_URL is the live bank source.
Never log Access URLs or setup tokens.
"""

from __future__ import annotations

import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def load_dotenv(path: Path | None = None) -> None:
    """Populate os.environ from a .env file. Existing env vars always win."""
    env_file = path or PROJECT_ROOT / ".env"
    if not env_file.is_file():
        return
    for raw in env_file.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def upsert_env_var(env_file: Path, key: str, value: str) -> None:
    """Set key=value in .env, preserving other lines. Mode 0600. Never log value."""
    env_file.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    if env_file.is_file():
        lines = env_file.read_text(encoding="utf-8").splitlines()
    found = False
    out: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in line:
            out.append(line)
            continue
        k, _, _ = line.partition("=")
        if k.strip() == key:
            out.append(f"{key}={value}")
            found = True
        else:
            out.append(line)
    if not found:
        if out and out[-1] != "":
            out.append("")
        out.append(f"{key}={value}")
    env_file.write_text("\n".join(out) + "\n", encoding="utf-8")
    try:
        os.chmod(env_file, 0o600)
    except OSError:
        pass


class Config:
    """Resolved settings. Instantiate after load_dotenv()."""

    def __init__(self) -> None:
        self.data_dir = Path(
            os.environ.get("WALNUT_DATA_DIR", PROJECT_ROOT / "data")
        ).expanduser()
        self.db_path = Path(
            os.environ.get("WALNUT_DB", self.data_dir / "walnut.db")
        ).expanduser()
        self.web_host = os.environ.get("WALNUT_WEB_HOST", "127.0.0.1")
        self.web_port = int(os.environ.get("WALNUT_WEB_PORT", "8766"))
        self.env_path = PROJECT_ROOT / ".env"
        self.simplefin_access_url = os.environ.get("SIMPLEFIN_ACCESS_URL", "").strip()

    @property
    def simplefin_ready(self) -> bool:
        return bool(self.simplefin_access_url)

    @property
    def live_source(self) -> str:
        return "simplefin"

    @property
    def live_ready(self) -> bool:
        return self.simplefin_ready

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
