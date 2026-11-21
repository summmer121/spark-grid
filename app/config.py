from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parent.parent


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    root_dir: Path
    data_dir: Path
    static_dir: Path
    host: str
    port: int
    ssh_user: str
    ssh_port: int
    ssh_key_path: str
    ssh_timeout: float
    discovery_interval: int
    poll_interval: float
    node_timeout: float
    session_ttl_hours: int
    cookie_secure: bool
    history_points: int
    history_retention_hours: int


def load_settings() -> Settings:
    data_dir = Path(os.getenv("SPARK_MONITOR_DATA", str(ROOT_DIR / "data"))).resolve()
    data_dir.mkdir(parents=True, exist_ok=True)
    return Settings(
        root_dir=ROOT_DIR,
        data_dir=data_dir,
        static_dir=ROOT_DIR / "app" / "static",
        host=os.getenv("SPARK_MONITOR_HOST", "0.0.0.0"),
        port=_env_int("SPARK_MONITOR_PORT", 6301),
        ssh_user=os.getenv("SPARK_SSH_USER", "spark"),
        ssh_port=_env_int("SPARK_SSH_PORT", 22),
        ssh_key_path=os.getenv("SPARK_SSH_KEY_PATH", ""),
        ssh_timeout=max(2, _env_int("SPARK_SSH_TIMEOUT", 8)),
        discovery_interval=max(60, _env_int("SPARK_DISCOVERY_INTERVAL", 600)),
        poll_interval=max(2.0, _env_int("SPARK_POLL_INTERVAL", 3)),
        node_timeout=max(0.5, _env_int("SPARK_NODE_TIMEOUT", 2)),
        session_ttl_hours=max(1, _env_int("SPARK_SESSION_TTL_HOURS", 12)),
        cookie_secure=os.getenv("SPARK_COOKIE_SECURE", "0") == "1",
        history_points=max(30, _env_int("SPARK_HISTORY_POINTS", 120)),
        history_retention_hours=max(1, _env_int("SPARK_HISTORY_RETENTION_HOURS", 24)),
    )


settings = load_settings()
