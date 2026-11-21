from __future__ import annotations

import getpass
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


ROOT_DIR = Path(__file__).resolve().parent.parent
SSH_HOST_KEY_POLICIES = {"accept-new", "yes", "no", "off"}
NETWORK_INTERFACE_EXCLUDE_PREFIXES = ("lo", "docker", "veth", "br-", "virbr")


def valid_timezone_name(value: str | None) -> str | None:
    name = str(value or "").strip()
    if name.startswith(":"):
        name = name[1:].strip()
    if not name:
        return None
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return None
    return name


def system_timezone_name() -> str:
    for variable in ("SPARK_SYSTEM_TIMEZONE", "TZ"):
        detected = valid_timezone_name(os.getenv(variable))
        if detected:
            return detected

    try:
        configured = Path("/etc/timezone").read_text(
            encoding="utf-8", errors="replace"
        ).strip()
    except OSError:
        configured = ""
    detected = valid_timezone_name(configured)
    if detected:
        return detected

    try:
        localtime = Path("/etc/localtime").resolve()
        marker = "/zoneinfo/"
        resolved = str(localtime)
        if marker in resolved:
            detected = valid_timezone_name(resolved.split(marker, 1)[1])
            if detected:
                return detected
    except OSError:
        pass

    detected = valid_timezone_name(getattr(datetime.now().astimezone().tzinfo, "key", None))
    return detected or "UTC"


def current_ssh_user() -> str:
    try:
        return getpass.getuser().strip()
    except (KeyError, OSError):
        return ""


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on", "enabled"}:
        return True
    if normalized in {"0", "false", "no", "off", "disabled"}:
        return False
    return default


def _ssh_host_key_policy() -> str:
    value = os.getenv("SPARK_SSH_STRICT_HOST_KEY_CHECKING", "accept-new")
    normalized = value.strip().lower()
    return normalized if normalized in SSH_HOST_KEY_POLICIES else "accept-new"


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
    ssh_strict_host_key_checking: str = "accept-new"
    credential_key_path: Path | None = None
    ssh_connect_timeout: float = 8
    ssh_control_persist: int = 90
    database_path: Path | None = None
    database_timeout_seconds: float = 15


def load_settings() -> Settings:
    data_value = Path(
        os.getenv("SPARK_MONITOR_DATA", str(ROOT_DIR / "data"))
    ).expanduser()
    if not data_value.is_absolute():
        data_value = ROOT_DIR / data_value
    data_dir = data_value.resolve()
    data_dir.mkdir(parents=True, exist_ok=True)
    ssh_key_value = os.getenv("SPARK_SSH_KEY_PATH", "").strip()
    if ssh_key_value:
        ssh_key_path = Path(ssh_key_value).expanduser()
        if not ssh_key_path.is_absolute():
            ssh_key_path = ROOT_DIR / ssh_key_path
        ssh_key_value = str(ssh_key_path.resolve())
    credential_key_value = (
        os.getenv("SPARK_CREDENTIAL_KEY_FILE", "").strip()
        or os.getenv("SPARK_SSH_SECRET_KEY_FILE", "").strip()
    )
    if credential_key_value:
        credential_key_path = Path(credential_key_value).expanduser()
        if not credential_key_path.is_absolute():
            credential_key_path = ROOT_DIR / credential_key_path
        credential_key_path = credential_key_path.resolve()
    else:
        credential_key_path = data_dir / "ssh-secret.key"
    database_value = os.getenv("SPARK_DATABASE_PATH", "").strip()
    if database_value:
        database_path = Path(database_value).expanduser()
        if not database_path.is_absolute():
            database_path = ROOT_DIR / database_path
        database_path = database_path.resolve()
    else:
        database_path = None
    default_ssh_user = os.getenv("SPARK_SSH_USER", "").strip() or current_ssh_user()
    return Settings(
        root_dir=ROOT_DIR,
        data_dir=data_dir,
        static_dir=ROOT_DIR / "app" / "static",
        host=os.getenv("SPARK_MONITOR_HOST", "0.0.0.0").strip() or "0.0.0.0",
        port=_env_int("SPARK_MONITOR_PORT", 6301),
        ssh_user=default_ssh_user,
        ssh_port=_env_int("SPARK_SSH_PORT", 22),
        ssh_key_path=ssh_key_value,
        ssh_timeout=max(1.0, _env_float("SPARK_SSH_TIMEOUT", 8)),
        ssh_connect_timeout=max(
            1.0,
            _env_float(
                "SPARK_SSH_CONNECT_TIMEOUT",
                _env_float("SPARK_SSH_TIMEOUT", 8),
            ),
        ),
        ssh_control_persist=max(
            0, min(3600, _env_int("SPARK_SSH_CONTROL_PERSIST", 90))
        ),
        discovery_interval=max(60, _env_int("SPARK_DISCOVERY_INTERVAL", 600)),
        poll_interval=max(1.0, _env_float("SPARK_POLL_INTERVAL", 3)),
        node_timeout=max(0.5, _env_float("SPARK_NODE_TIMEOUT", 2)),
        session_ttl_hours=max(1, _env_int("SPARK_SESSION_TTL_HOURS", 12)),
        cookie_secure=_env_bool("SPARK_COOKIE_SECURE", False),
        history_points=max(30, _env_int("SPARK_HISTORY_POINTS", 120)),
        history_retention_hours=max(1, _env_int("SPARK_HISTORY_RETENTION_HOURS", 24)),
        ssh_strict_host_key_checking=_ssh_host_key_policy(),
        credential_key_path=credential_key_path,
        database_path=database_path,
        database_timeout_seconds=max(
            0.1,
            min(300.0, _env_float("SPARK_DATABASE_TIMEOUT", 15)),
        ),
    )


settings = load_settings()
