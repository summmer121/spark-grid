from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator


SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'admin',
    created_at INTEGER NOT NULL,
    last_login INTEGER
);

CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    csrf_token TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL,
    remote_addr TEXT,
    user_agent TEXT
);

CREATE TABLE IF NOT EXISTS nodes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    host TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    ssh_port INTEGER NOT NULL DEFAULT 22,
    ssh_user TEXT NOT NULL DEFAULT 'spark',
    ssh_key_path TEXT NOT NULL DEFAULT '',
    group_name TEXT NOT NULL DEFAULT '未分组',
    tags TEXT NOT NULL DEFAULT '[]',
    enabled INTEGER NOT NULL DEFAULT 1,
    last_status TEXT NOT NULL DEFAULT 'unknown',
    last_seen INTEGER,
    last_latency_ms REAL,
    latest_metrics TEXT,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS metrics_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    node_id INTEGER NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
    ts INTEGER NOT NULL,
    cpu REAL,
    memory REAL,
    gpu REAL,
    temperature REAL,
    rx_rate REAL,
    tx_rate REAL
);
CREATE INDEX IF NOT EXISTS idx_metrics_node_ts
    ON metrics_history(node_id, ts DESC);

CREATE TABLE IF NOT EXISTS model_throughput_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL,
    process_started_at TEXT NOT NULL,
    engine TEXT NOT NULL,
    prefill_tokens_per_sec REAL,
    decode_tokens_per_sec REAL
);

CREATE INDEX IF NOT EXISTS idx_model_throughput_started_ts
    ON model_throughput_history(process_started_at, ts DESC);

CREATE TABLE IF NOT EXISTS discovered_nodes (
    host TEXT PRIMARY KEY,
    machine_id TEXT,
    name TEXT,
    ssh_port INTEGER NOT NULL DEFAULT 22,
    ssh_user TEXT NOT NULL DEFAULT 'spark',
    ssh_key_path TEXT NOT NULL DEFAULT '',
    info TEXT NOT NULL,
    first_seen INTEGER NOT NULL,
    last_seen INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL,
    user_id INTEGER,
    username TEXT,
    action TEXT NOT NULL,
    target TEXT,
    detail TEXT,
    remote_addr TEXT
);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_logs(ts DESC);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def _now() -> int:
    return int(time.time())


def _json_load(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


class Database:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def initialize(self) -> None:
        with self.connect() as connection:
            connection.executescript(SCHEMA)
            self._ensure_column(connection, "nodes", "ssh_user", "TEXT NOT NULL DEFAULT 'spark'")
            self._ensure_column(connection, "nodes", "ssh_key_path", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(
                connection, "discovered_nodes", "ssh_user", "TEXT NOT NULL DEFAULT 'spark'"
            )
            self._ensure_column(
                connection, "discovered_nodes", "ssh_key_path", "TEXT NOT NULL DEFAULT ''"
            )

    @staticmethod
    def _ensure_column(
        connection: sqlite3.Connection, table: str, column: str, definition: str
    ) -> None:
        columns = {
            row["name"] for row in connection.execute(f"PRAGMA table_info({table})")
        }
        if column not in columns:
            connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    def count_users(self) -> int:
        with self.connect() as connection:
            row = connection.execute("SELECT COUNT(*) AS count FROM users").fetchone()
        return int(row["count"])

    def get_user_by_username(self, username: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM users WHERE username = ?", (username,)
            ).fetchone()
        return dict(row) if row else None

    def create_user(self, username: str, password_hash: str) -> dict[str, Any]:
        timestamp = _now()
        with self.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO users(username, password_hash, role, created_at)
                VALUES (?, ?, 'admin', ?)
                """,
                (username, password_hash, timestamp),
            )
            user_id = int(cursor.lastrowid)
        return {
            "id": user_id,
            "username": username,
            "role": "admin",
            "created_at": timestamp,
        }

    def mark_login(self, user_id: int) -> None:
        with self.connect() as connection:
            connection.execute(
                "UPDATE users SET last_login = ? WHERE id = ?", (_now(), user_id)
            )

    def create_session(
        self,
        token_hash: str,
        user_id: int,
        csrf_token: str,
        expires_at: int,
        remote_addr: str,
        user_agent: str,
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO sessions(
                    token_hash, user_id, csrf_token, created_at, expires_at,
                    remote_addr, user_agent
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    token_hash,
                    user_id,
                    csrf_token,
                    _now(),
                    expires_at,
                    remote_addr,
                    user_agent[:300],
                ),
            )

    def get_session(self, token_hash: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT s.*, u.username, u.role
                FROM sessions s
                JOIN users u ON u.id = s.user_id
                WHERE s.token_hash = ? AND s.expires_at > ?
                """,
                (token_hash, _now()),
            ).fetchone()
        return dict(row) if row else None

    def delete_session(self, token_hash: str) -> None:
        with self.connect() as connection:
            connection.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash,))

    def purge_expired_sessions(self) -> None:
        with self.connect() as connection:
            connection.execute("DELETE FROM sessions WHERE expires_at <= ?", (_now(),))

    def list_nodes(self) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM nodes ORDER BY group_name, name, id"
            ).fetchall()
        return [self._node_row(row) for row in rows]

    def get_node(self, node_id: int) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM nodes WHERE id = ?", (node_id,)
            ).fetchone()
        return self._node_row(row) if row else None

    def get_node_by_host(self, host: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM nodes WHERE host = ?", (host,)
            ).fetchone()
        return self._node_row(row) if row else None

    def add_node(
        self,
        host: str,
        name: str,
        ssh_port: int = 22,
        ssh_user: str = "spark",
        ssh_key_path: str = "",
        group_name: str = "未分组",
        tags: Iterable[str] = (),
    ) -> dict[str, Any]:
        timestamp = _now()
        tag_json = json.dumps(list(dict.fromkeys(tags)), ensure_ascii=False)
        with self.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO nodes(
                    host, name, ssh_port, ssh_user, ssh_key_path, group_name, tags,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    host,
                    name,
                    ssh_port,
                    ssh_user,
                    ssh_key_path,
                    group_name,
                    tag_json,
                    timestamp,
                    timestamp,
                ),
            )
            node_id = int(cursor.lastrowid)
        return self.get_node(node_id) or {}

    def update_node(self, node_id: int, fields: dict[str, Any]) -> dict[str, Any] | None:
        allowed = {
            "name",
            "host",
            "ssh_port",
            "ssh_user",
            "ssh_key_path",
            "group_name",
            "tags",
            "enabled",
        }
        updates: list[str] = []
        values: list[Any] = []
        for key, value in fields.items():
            if key not in allowed:
                continue
            if key == "tags":
                value = json.dumps(value if isinstance(value, list) else [], ensure_ascii=False)
            if key == "enabled":
                value = 1 if value else 0
            updates.append(f"{key} = ?")
            values.append(value)
        if not updates:
            return self.get_node(node_id)
        updates.append("updated_at = ?")
        values.append(_now())
        values.append(node_id)
        with self.connect() as connection:
            connection.execute(
                f"UPDATE nodes SET {', '.join(updates)} WHERE id = ?", values
            )
        return self.get_node(node_id)

    def set_nodes_enabled(self, node_ids: list[int], enabled: bool) -> int:
        if not node_ids:
            return 0
        placeholders = ",".join("?" for _ in node_ids)
        with self.connect() as connection:
            cursor = connection.execute(
                f"UPDATE nodes SET enabled = ?, updated_at = ? WHERE id IN ({placeholders})",
                [1 if enabled else 0, _now(), *node_ids],
            )
        return int(cursor.rowcount)

    def delete_nodes(self, node_ids: list[int]) -> int:
        if not node_ids:
            return 0
        placeholders = ",".join("?" for _ in node_ids)
        with self.connect() as connection:
            cursor = connection.execute(
                f"DELETE FROM nodes WHERE id IN ({placeholders})", node_ids
            )
        return int(cursor.rowcount)

    def save_metrics(
        self,
        node_id: int,
        metrics: dict[str, Any],
        latency_ms: float,
    ) -> None:
        timestamp = _now()
        resources = metrics.get("resources", {})
        gpu_util = None
        gpus = metrics.get("gpus", [])
        if gpus:
            values = [gpu.get("utilization") for gpu in gpus]
            values = [value for value in values if isinstance(value, (int, float))]
            gpu_util = sum(values) / len(values) if values else None
        compact = metrics.get("compact", {})
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE nodes
                SET last_status = 'online', last_seen = ?, last_latency_ms = ?,
                    latest_metrics = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    timestamp,
                    latency_ms,
                    json.dumps(metrics, ensure_ascii=False, separators=(",", ":")),
                    timestamp,
                    node_id,
                ),
            )
            connection.execute(
                """
                INSERT INTO metrics_history(
                    node_id, ts, cpu, memory, gpu, temperature, rx_rate, tx_rate
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    node_id,
                    timestamp,
                    compact.get("cpu", resources.get("cpu_percent")),
                    compact.get("memory", resources.get("memory_percent")),
                    compact.get("gpu", gpu_util),
                    compact.get("temperature", resources.get("temperature_max")),
                    compact.get("rx_rate", resources.get("rx_rate")),
                    compact.get("tx_rate", resources.get("tx_rate")),
                ),
            )

    def record_model_throughput(
        self,
        process_started_at: str,
        engine: str,
        prefill_tokens_per_sec: float | None,
        decode_tokens_per_sec: float | None,
        timestamp: int | None = None,
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO model_throughput_history(
                    ts, process_started_at, engine,
                    prefill_tokens_per_sec, decode_tokens_per_sec
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    timestamp if timestamp is not None else _now(),
                    process_started_at,
                    engine,
                    prefill_tokens_per_sec,
                    decode_tokens_per_sec,
                ),
            )

    def load_model_throughput_history(
        self,
        process_started_at: str,
        minutes: int = 30,
        buckets: int = 30,
        now: int | None = None,
    ) -> dict[str, Any]:
        current = now if now is not None else _now()
        bucket_seconds = max(1, minutes * 60 // max(1, buckets))
        current_bucket = current - current % bucket_seconds
        start_bucket = current_bucket - (buckets - 1) * bucket_seconds
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT
                    CAST(ts / ? AS INTEGER) * ? AS bucket_ts,
                    AVG(prefill_tokens_per_sec) AS prefill_tokens_per_sec,
                    AVG(decode_tokens_per_sec) AS decode_tokens_per_sec
                FROM model_throughput_history
                WHERE process_started_at = ? AND ts >= ?
                GROUP BY CAST(ts / ? AS INTEGER)
                ORDER BY bucket_ts
                """,
                (
                    bucket_seconds,
                    bucket_seconds,
                    process_started_at,
                    start_bucket,
                    bucket_seconds,
                ),
            ).fetchall()
        by_bucket = {int(row["bucket_ts"]): dict(row) for row in rows}
        points = []
        for index in range(buckets):
            bucket_ts = start_bucket + index * bucket_seconds
            row = by_bucket.get(bucket_ts, {})
            points.append(
                {
                    "ts": bucket_ts,
                    "prefill_tokens_per_sec": (
                        round(float(row["prefill_tokens_per_sec"]), 2)
                        if row.get("prefill_tokens_per_sec") is not None
                        else None
                    ),
                    "decode_tokens_per_sec": (
                        round(float(row["decode_tokens_per_sec"]), 2)
                        if row.get("decode_tokens_per_sec") is not None
                        else None
                    ),
                }
            )
        return {
            "window_minutes": minutes,
            "bucket_seconds": bucket_seconds,
            "process_started_at": process_started_at,
            "points": points,
        }

    def mark_node_offline(self, node_id: int) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE nodes SET last_status = 'offline', updated_at = ?
                WHERE id = ? AND enabled = 1
                """,
                (_now(), node_id),
            )

    def mark_node_paused(self, node_id: int) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE nodes SET last_status = 'paused', updated_at = ?
                WHERE id = ? AND enabled = 0
                """,
                (_now(), node_id),
            )

    def load_history(self, node_id: int, limit: int = 120) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT ts, cpu, memory, gpu, temperature, rx_rate, tx_rate
                FROM metrics_history
                WHERE node_id = ?
                ORDER BY ts DESC
                LIMIT ?
                """,
                (node_id, limit),
            ).fetchall()
        return [dict(row) for row in reversed(rows)]

    def load_history_range(
        self, node_id: int, minutes: int, max_points: int = 300
    ) -> list[dict[str, Any]]:
        bucket_seconds = max(1, (minutes * 60 + max_points - 1) // max_points)
        since = _now() - minutes * 60
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT
                    CAST(ts / ? AS INTEGER) * ? AS bucket_ts,
                    AVG(cpu) AS cpu,
                    AVG(memory) AS memory,
                    AVG(gpu) AS gpu,
                    AVG(temperature) AS temperature,
                    AVG(rx_rate) AS rx_rate,
                    AVG(tx_rate) AS tx_rate
                FROM metrics_history
                WHERE node_id = ? AND ts >= ?
                GROUP BY CAST(ts / ? AS INTEGER)
                ORDER BY bucket_ts
                LIMIT ?
                """,
                (
                    bucket_seconds,
                    bucket_seconds,
                    node_id,
                    since,
                    bucket_seconds,
                    max_points,
                ),
            ).fetchall()
        return [
            {
                ("ts" if key == "bucket_ts" else key): (
                    round(value, 1) if isinstance(value, float) else value
                )
                for key, value in dict(row).items()
            }
            for row in rows
        ]

    def prune_history(self, retention_hours: int) -> int:
        cutoff = _now() - retention_hours * 3600
        with self.connect() as connection:
            cursor = connection.execute(
                "DELETE FROM metrics_history WHERE ts < ?", (cutoff,)
            )
            connection.execute(
                "DELETE FROM model_throughput_history WHERE ts < ?", (cutoff,)
            )
        return int(cursor.rowcount)

    def save_discovery(
        self,
        info: dict[str, Any],
        remote_addr: str,
        ssh_user: str = "spark",
        ssh_port: int = 22,
        ssh_key_path: str = "",
    ) -> None:
        host = str(info.get("host") or remote_addr)
        timestamp = _now()
        payload = json.dumps(info, ensure_ascii=False, separators=(",", ":"))
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO discovered_nodes(
                    host, machine_id, name, ssh_port, ssh_user, ssh_key_path,
                    info, first_seen, last_seen
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(host) DO UPDATE SET
                    machine_id = excluded.machine_id,
                    name = excluded.name,
                    ssh_port = excluded.ssh_port,
                    ssh_user = excluded.ssh_user,
                    ssh_key_path = excluded.ssh_key_path,
                    info = excluded.info,
                    last_seen = excluded.last_seen
                """,
                (
                    host,
                    info.get("machine_id"),
                    info.get("hostname") or host,
                    ssh_port,
                    ssh_user,
                    ssh_key_path,
                    payload,
                    timestamp,
                    timestamp,
                ),
            )

    def list_discovered(self, max_age_seconds: int = 900) -> list[dict[str, Any]]:
        cutoff = _now() - max_age_seconds
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT d.*, n.id AS managed_node_id
                FROM discovered_nodes d
                LEFT JOIN nodes n ON n.host = d.host
                WHERE d.last_seen >= ?
                ORDER BY d.name, d.host
                """,
                (cutoff,),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["info"] = _json_load(item.get("info"), {})
            item["managed"] = item.pop("managed_node_id") is not None
            result.append(item)
        return result

    def add_audit(
        self,
        user: dict[str, Any] | None,
        action: str,
        target: str,
        detail: str,
        remote_addr: str,
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO audit_logs(
                    ts, user_id, username, action, target, detail, remote_addr
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    _now(),
                    user.get("id") if user else None,
                    user.get("username") if user else None,
                    action,
                    target,
                    detail,
                    remote_addr,
                ),
            )

    def list_audit(self, limit: int = 100) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM audit_logs ORDER BY ts DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(row) for row in rows]

    def get_settings(self) -> dict[str, Any]:
        with self.connect() as connection:
            rows = connection.execute("SELECT key, value FROM settings").fetchall()
        return {row["key"]: _json_load(row["value"], row["value"]) for row in rows}

    def update_settings(self, values: dict[str, Any]) -> dict[str, Any]:
        with self.connect() as connection:
            for key, value in values.items():
                connection.execute(
                    """
                    INSERT INTO settings(key, value) VALUES (?, ?)
                    ON CONFLICT(key) DO UPDATE SET value = excluded.value
                    """,
                    (key, json.dumps(value, ensure_ascii=False)),
                )
        return self.get_settings()

    @staticmethod
    def _node_row(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["tags"] = _json_load(item.get("tags"), [])
        item["latest_metrics"] = _json_load(item.get("latest_metrics"), None)
        item["enabled"] = bool(item.get("enabled"))
        return item
