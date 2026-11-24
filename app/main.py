from __future__ import annotations

import argparse
import csv
import hmac
import io
import ipaddress
import json
import logging
import mimetypes
import os
import re
import sqlite3
import threading
import time
import urllib.parse
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from . import __version__
from .auth import (
    LoginThrottle,
    create_session,
    hash_password,
    token_hash,
    verify_password,
)
from .collector import MetricsCollector
from .config import Settings, load_settings
from .db import Database, sanitize_error_message
from .discovery import DiscoveryService
from .model_chat import ChatError, ModelChat
from .model_control import ModelControl, ModelControlError
from .model_monitor import ModelMonitor
from .runtime_config import RuntimeConfig
from .ssh_runner import SSHError, probe_via_ssh


LOGGER = logging.getLogger("spark-grid")
SESSION_COOKIE = "spark_grid_session"
HOST_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,252}$")
USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]{3,32}$")
# (setting key, alert metric type, severity, display name)
ALERT_RULES = (
    ("cpu_warning", "cpu", "warning", "CPU 使用率"),
    ("memory_warning", "memory", "warning", "内存使用率"),
    ("gpu_warning", "gpu", "warning", "GPU 负载"),
    ("temperature_warning", "temperature", "critical", "最高温度"),
    ("offline_seconds", "offline", "critical", "离线判定"),
)
SSH_USER_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


def _now() -> int:
    return int(time.time())


def _safe_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, parsed))


def _parse_ssh_port(value: Any, default: int) -> int:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        raise ValueError("SSH 端口格式无效")
    try:
        port = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("SSH 端口格式无效") from exc
    if not 1 <= port <= 65535:
        raise ValueError("SSH 端口必须在 1 到 65535 之间")
    return port


def _tags(value: Any, limit: int) -> list[str]:
    if isinstance(value, list):
        raw = value
    else:
        raw = re.split(r"[,;|]", str(value or ""))
    tags = list(
        dict.fromkeys(str(item).strip() for item in raw if str(item).strip())
    )
    if len(tags) > limit:
        raise ValueError(f"单个节点最多 {limit} 个标签")
    return tags


def _default_name(host: str, pattern: str) -> str:
    last_octet = host.rsplit(".", 1)[-1] if re.fullmatch(
        r"\d{1,3}(?:\.\d{1,3}){3}", host
    ) else host.split(".", 1)[0]
    try:
        return pattern.format(host=host, last_octet=last_octet)
    except (KeyError, IndexError, ValueError):
        return host.split(".", 1)[0]


def _expand_host_token(token: str, limit: int) -> list[str]:
    token = token.strip()
    if not token:
        return []
    if "/" in token:
        try:
            network = ipaddress.ip_network(token, strict=False)
            hosts = [str(address) for address in network.hosts()]
            if network.num_addresses == 1:
                hosts = [str(network.network_address)]
            return hosts[:limit]
        except ValueError:
            return []
    match = re.fullmatch(r"(\d{1,3}(?:\.\d{1,3}){3})\.\.(\d{1,3})", token)
    if not match:
        match = re.fullmatch(r"(\d{1,3}(?:\.\d{1,3}){3})-(\d{1,3})", token)
    if match:
        prefix, end_text = match.groups()
        base = prefix.rsplit(".", 1)[0]
        try:
            start = int(prefix.rsplit(".", 1)[-1])
            end = int(end_text)
            if end < start:
                return []
            return [f"{base}.{last}" for last in range(start, min(255, end) + 1)][:limit]
        except ValueError:
            return []
    match = re.fullmatch(
        r"(\d{1,3}(?:\.\d{1,3}){3})-(\d{1,3}(?:\.\d{1,3}){3})", token
    )
    if match:
        try:
            start_ip = ipaddress.ip_address(match.group(1))
            end_ip = ipaddress.ip_address(match.group(2))
            if start_ip.version != 4 or end_ip.version != 4 or int(end_ip) < int(start_ip):
                return []
            return [
                str(ipaddress.ip_address(value))
                for value in range(int(start_ip), min(int(end_ip) + 1, int(start_ip) + limit))
            ]
        except ValueError:
            return []
    return [token]


def _parse_bulk_lines(
    text: str,
    default_group: str,
    default_tags: list[str],
    ssh_port: int,
    ssh_user: str,
    ssh_key_path: str,
    name_pattern: str,
    limit: int,
    tag_limit: int,
) -> tuple[list[dict[str, Any]], list[str]]:
    parsed: list[dict[str, Any]] = []
    errors: list[str] = []
    seen: set[str] = set()
    for line_number, line in enumerate(text.splitlines(), start=1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            columns = next(csv.reader([line]))
        except csv.Error:
            columns = [line]
        host_token = columns[0].strip()
        if not host_token:
            continue
        for host in _expand_host_token(host_token, max(1, limit - len(parsed))):
            if not HOST_RE.fullmatch(host):
                errors.append(f"第 {line_number} 行：地址格式无效")
                break
            if host in seen:
                continue
            seen.add(host)
            parsed.append(
                {
                    "host": host,
                    "name": columns[1].strip()
                    if len(columns) > 1 and columns[1].strip()
                    else _default_name(host, name_pattern),
                    "group_name": columns[2].strip() if len(columns) > 2 and columns[2].strip() else default_group,
                    "tags": _tags(columns[3], tag_limit)
                    if len(columns) > 3 and columns[3].strip()
                    else default_tags,
                    "ssh_port": ssh_port,
                    "ssh_user": ssh_user,
                    "ssh_key_path": ssh_key_path,
                }
            )
            if len(parsed) >= limit:
                errors.append(f"单次最多处理 {limit} 个地址")
                return parsed, errors
    return parsed, errors


class SparkGridApp:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.db = Database(
            settings.database_path or settings.data_dir / "spark-grid.db",
            credential_key_path=(
                settings.credential_key_path
                or settings.data_dir / "ssh-secret.key"
            ),
            timeout_seconds=getattr(settings, "database_timeout_seconds", 15),
        )
        self.config = RuntimeConfig(self.db, settings)
        self.throttle = LoginThrottle()
        self.started_at = _now()
        self.collector = MetricsCollector(self.db, self.config)
        self.discovery = DiscoveryService(self.db, self.config)
        self.model_monitor = ModelMonitor(self.db, self.config)
        self.model_control = ModelControl(self.db, self.config)
        self.model_chat = ModelChat(self.db, self.config, self.model_control)

    def start(self) -> None:
        self.collector.start()
        self.discovery.start()
        self.model_monitor.start()

    def stop(self) -> None:
        self.model_monitor.stop()
        self.discovery.stop()
        self.collector.stop()

    def get_thresholds(self) -> dict[str, Any]:
        return self.config.thresholds()

    def get_ssh_defaults(self) -> dict[str, Any]:
        return self.config.ssh_defaults()

    def notify_config_changed(self) -> None:
        self.collector.notify_config_changed()
        self.discovery.notify_config_changed()
        self.model_monitor.notify_config_changed()

    def activate_discovered_hosts(self) -> None:
        nodes = self.db.list_nodes()
        for node in nodes:
            if node["enabled"]:
                self.collector.refresh_node(node)

    def dashboard(self) -> dict[str, Any]:
        config = self.config.current()
        thresholds = self.get_thresholds()
        raw_nodes = self.db.list_nodes()
        now = _now()
        offline_after = max(5, int(thresholds.get("offline_seconds", 20)))
        nodes: list[dict[str, Any]] = []
        for raw in raw_nodes:
            node = dict(raw)
            metrics = node.get("latest_metrics")
            last_seen = node.get("last_seen")
            if not node["enabled"]:
                status = "paused"
            elif not last_seen:
                persisted = str(node.get("last_status") or "unknown")
                status = persisted if persisted in {"online", "offline"} else "unknown"
            elif now - int(last_seen) <= offline_after:
                status = "online"
            else:
                status = "offline"
            node["status"] = status
            node["history"] = self.db.load_history(
                int(node["id"]), int(config.history_points)
            )
            nodes.append(node)

        online_nodes = [
            node
            for node in nodes
            if node["status"] == "online" and node.get("latest_metrics")
        ]

        def average(path: tuple[str, ...]) -> float:
            values: list[float] = []
            for node in online_nodes:
                current: Any = node.get("latest_metrics") or {}
                for key in path:
                    if not isinstance(current, dict):
                        current = None
                        break
                    current = current.get(key)
                if isinstance(current, (int, float)):
                    values.append(float(current))
            return round(sum(values) / len(values), 1) if values else 0.0

        def gpu_average() -> float:
            values: list[float] = []
            for node in online_nodes:
                gpus = (node.get("latest_metrics") or {}).get("gpus") or []
                for gpu in gpus:
                    utilization = gpu.get("load_percent", gpu.get("utilization"))
                    if isinstance(utilization, (int, float)):
                        values.append(float(utilization))
            return round(sum(values) / len(values), 1) if values else 0.0

        summary = {
            "total": len(nodes),
            "online": len(online_nodes),
            "offline": sum(node["status"] == "offline" for node in nodes),
            "paused": sum(node["status"] == "paused" for node in nodes),
            "load_1_avg": average(("resources", "load_1")),
            "cpu_count_avg": average(("resources", "cpu_count")),
            "cpu_avg": average(("resources", "cpu_percent")),
            "memory_avg": average(("resources", "memory_percent")),
            "memory_used_total": round(
                sum(
                    float(
                        (node.get("latest_metrics") or {})
                        .get("resources", {})
                        .get("memory_used", 0)
                    )
                    for node in online_nodes
                )
            ),
            "memory_total_total": round(
                sum(
                    float(
                        (node.get("latest_metrics") or {})
                        .get("resources", {})
                        .get("memory_total", 0)
                    )
                    for node in online_nodes
                )
            ),
            "memory_available_total": round(
                sum(
                    float(
                        (node.get("latest_metrics") or {})
                        .get("resources", {})
                        .get("memory_available", 0)
                    )
                    for node in online_nodes
                )
            ),
            "gpu_avg": gpu_average(),
            "temperature_max": max(
                [
                    float(node["latest_metrics"]["resources"].get("temperature_max") or 0)
                    for node in online_nodes
                ]
                or [0]
            ),
            "rx_rate": round(
                sum(
                    float(
                        (node.get("latest_metrics") or {})
                        .get("resources", {})
                        .get("rx_rate", 0)
                    )
                    for node in online_nodes
                ),
                1,
            ),
            "tx_rate": round(
                sum(
                    float(
                        (node.get("latest_metrics") or {})
                        .get("resources", {})
                        .get("tx_rate", 0)
                    )
                    for node in online_nodes
                ),
                1,
            ),
        }

        alerts = self._build_alerts(nodes, thresholds, now)
        groups: dict[str, list[dict[str, Any]]] = {}
        for node in nodes:
            groups.setdefault(
                node.get("group_name") or config.default_group_name, []
            ).append(node)
        group_list = [
            {"name": name, "nodes": group_nodes}
            for name, group_nodes in sorted(groups.items(), key=lambda item: item[0])
        ]
        return {
            "generated_at": now,
            "summary": summary,
            "groups": group_list,
            "nodes": nodes,
            "alerts": alerts,
            "thresholds": thresholds,
            "poll_interval": config.poll_interval,
            "dashboard_refresh_seconds": config.dashboard_refresh_seconds,
            "ui_history_minutes": config.ui_history_minutes,
            "ui_history_points": config.ui_history_points,
            "ui_history_ranges": config.ui_history_ranges,
            "model_history_minutes": config.model_history_minutes,
            "model_history_buckets": config.model_history_buckets,
            "default_group_name": config.default_group_name,
            "session_ttl_hours": config.session_ttl_hours,
        }

    def alert_rules(self) -> dict[str, Any]:
        payload = self.config.public_payload()
        fields = {field["key"]: field for field in payload["fields"]}
        dashboard = self.dashboard()
        counts: dict[str, int] = {}
        for alert in dashboard["alerts"]:
            metric = str(alert.get("type") or "")
            counts[metric] = counts.get(metric, 0) + 1
        rules = []
        for key, metric, severity, display in ALERT_RULES:
            field = fields.get(key)
            if not field:
                continue
            rules.append(
                {
                    "key": key,
                    "label": field["label"],
                    "display": display,
                    "description": field["description"],
                    "unit": field["unit"],
                    "value": field["value"],
                    "default": field["default"],
                    "min": field["min"],
                    "max": field["max"],
                    "severity": severity,
                    "metric": metric,
                    "triggered": counts.get(metric, 0),
                }
            )
        return {
            "rules": rules,
            "alerts": len(dashboard["alerts"]),
            "generated_at": _now(),
        }

    @staticmethod
    def _build_alerts(
        nodes: list[dict[str, Any]], thresholds: dict[str, Any], now: int
    ) -> list[dict[str, Any]]:
        alerts: list[dict[str, Any]] = []
        for node in nodes:
            metrics = node.get("latest_metrics") or {}
            resources = metrics.get("resources") or {}
            gpus = metrics.get("gpus") or []
            gpu_values = [
                float(gpu.get("load_percent", gpu.get("utilization")))
                for gpu in gpus
                if isinstance(gpu.get("load_percent", gpu.get("utilization")), (int, float))
            ]
            current = {
                "cpu": float(resources.get("cpu_percent") or 0),
                "memory": float(resources.get("memory_percent") or 0),
                "gpu": max(gpu_values) if gpu_values else 0,
                "temperature": float(resources.get("temperature_max") or 0),
            }
            checks = (
                ("cpu", "CPU", "cpu_warning", "warning"),
                ("memory", "内存", "memory_warning", "warning"),
                ("gpu", "GPU 负载", "gpu_warning", "warning"),
                ("temperature", "温度", "temperature_warning", "critical"),
            )
            for metric, label, setting_key, severity in checks:
                limit = float(thresholds.get(setting_key, 100))
                if current[metric] >= limit:
                    alerts.append(
                        {
                            "id": f"{node['id']}-{metric}",
                            "node_id": node["id"],
                            "node_name": node["name"],
                            "host": node["host"],
                            "type": metric,
                            "severity": severity,
                            "title": f"{label} 使用率偏高",
                            "message": f"{current[metric]:.1f}% 已达到阈值 {limit:.0f}%",
                            "value": current[metric],
                            "threshold": limit,
                            "ts": node.get("last_seen") or now,
                        }
                    )
            if node["status"] == "offline":
                alerts.append(
                    {
                        "id": f"{node['id']}-offline",
                        "node_id": node["id"],
                        "node_name": node["name"],
                        "host": node["host"],
                        "type": "offline",
                        "severity": "critical",
                        "title": "节点离线",
                        "message": f"最近采集于 {node.get('last_seen') or '未知'}",
                        "value": 0,
                        "threshold": int(thresholds.get("offline_seconds", 20)),
                        "ts": now,
                    }
                )
        severity_order = {"critical": 0, "warning": 1}
        return sorted(
            alerts,
            key=lambda item: (severity_order.get(item["severity"], 2), item["node_name"]),
        )


class SparkGridServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], application: SparkGridApp):
        super().__init__(address, SparkGridHandler)
        self.application = application


class SparkGridHandler(BaseHTTPRequestHandler):
    server: SparkGridServer
    protocol_version = "HTTP/1.1"
    server_version = f"SparkGrid/{__version__}"
    sys_version = ""

    @property
    def app(self) -> SparkGridApp:
        return self.server.application

    def log_message(self, fmt: str, *args: Any) -> None:
        LOGGER.info("%s - %s", self.client_address[0], fmt % args)

    def do_GET(self) -> None:
        try:
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path == "/api/health":
                self._json(
                    {
                        "ok": True,
                        "service": "spark-grid",
                        "version": __version__,
                        "uptime": _now() - self.app.started_at,
                    }
                )
                return
            if parsed.path == "/api/bootstrap":
                self._json(
                    {
                        "setup_required": self.app.db.count_users() == 0,
                        "version": __version__,
                        "service": "spark-grid",
                    }
                )
                return
            if parsed.path.startswith("/api/"):
                user, session, error = self._authenticate()
                if error:
                    return
                if parsed.path == "/api/session":
                    self._json(
                        {
                            "user": {
                                "id": user["id"],
                                "username": user["username"],
                                "role": user["role"],
                            },
                            "csrf_token": session["csrf_token"],
                            "expires_at": session["expires_at"],
                        }
                    )
                elif parsed.path == "/api/dashboard":
                    self._json(self.app.dashboard())
                elif parsed.path == "/api/model/overview":
                    self._json(self.app.model_monitor.overview())
                elif parsed.path == "/api/model/services":
                    self._json(self.app.model_control.list_services())
                elif parsed.path == "/api/model/chat/info":
                    self._json(self.app.model_chat.info())
                elif parsed.path == "/api/model/logs":
                    config = self.app.config.current()
                    query = urllib.parse.parse_qs(parsed.query)
                    rank = (query.get("rank") or ["0"])[0]
                    lines = _safe_int(
                        (query.get("lines") or [config.model_log_lines])[0],
                        int(config.model_log_lines),
                        20,
                        int(config.model_log_max_lines),
                    )
                    self._json(self.app.model_monitor.logs(rank, lines))
                elif parsed.path == "/api/history/cluster":
                    config = self.app.config.current()
                    query = urllib.parse.parse_qs(parsed.query)
                    history_min = min(int(value) for value in config.ui_history_ranges)
                    history_max = max(int(value) for value in config.ui_history_ranges)
                    minutes = _safe_int(
                        (query.get("minutes") or [config.ui_history_minutes])[0],
                        int(config.ui_history_minutes),
                        history_min,
                        history_max,
                    )
                    series = {
                        str(node["id"]): self.app.db.load_history_range(
                            int(node["id"]),
                            minutes,
                            max_points=int(config.ui_history_points),
                        )
                        for node in self.app.db.list_nodes()
                    }
                    self._json({"minutes": minutes, "series": series})
                elif re.fullmatch(r"/api/nodes/\d+/history", parsed.path):
                    config = self.app.config.current()
                    match = re.fullmatch(r"/api/nodes/(\d+)/history", parsed.path)
                    node = self.app.db.get_node(int(match.group(1)))
                    if not node:
                        self._error(404, "节点不存在")
                        return
                    query = urllib.parse.parse_qs(parsed.query)
                    history_min = min(int(value) for value in config.ui_history_ranges)
                    history_max = max(int(value) for value in config.ui_history_ranges)
                    minutes = _safe_int(
                        (query.get("minutes") or [config.ui_history_minutes])[0],
                        int(config.ui_history_minutes),
                        history_min,
                        history_max,
                    )
                    self._json(
                        {
                            "node": {
                                "id": node["id"],
                                "name": node["name"],
                                "host": node["host"],
                            },
                            "minutes": minutes,
                            "series": self.app.db.load_history_range(
                                int(node["id"]), minutes
                            ),
                        }
                    )
                elif parsed.path == "/api/nodes":
                    self._json({"nodes": self.app.db.list_nodes()})
                elif parsed.path == "/api/discovery":
                    self._json(
                        {
                            "discovered": self.app.db.list_discovered(),
                            "scan": self.app.discovery.state(),
                            "interfaces": self.app.discovery.interfaces(),
                        }
                    )
                elif parsed.path == "/api/discovery/scan":
                    self._json(self.app.discovery.state())
                elif parsed.path == "/api/alert-rules":
                    self._json(self.app.alert_rules())
                elif parsed.path == "/api/alerts":
                    dashboard = self.app.dashboard()
                    self._json(
                        {
                            "alerts": dashboard["alerts"],
                            "generated_at": dashboard["generated_at"],
                        }
                    )
                elif parsed.path == "/api/audit":
                    self._json({"logs": self.app.db.list_audit(150)})
                elif parsed.path == "/api/settings":
                    self._json(self.app.config.public_payload())
                elif parsed.path == "/api/export/nodes":
                    self._export_nodes_csv()
                else:
                    self._error(404, "接口不存在")
                return
            self._serve_static(parsed.path)
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception:
            LOGGER.exception("GET request failed")
            self._error(500, "服务器内部错误")

    def do_POST(self) -> None:
        try:
            path = urllib.parse.urlparse(self.path).path
            if path == "/api/login":
                self._login()
                return
            if path == "/api/setup":
                self._setup()
                return
            user, session, error = self._authenticate()
            if error:
                return
            if not self._check_csrf(session):
                return
            if path == "/api/logout":
                self.app.db.delete_session(session["token_hash"])
                self._json({"ok": True}, set_cookie=self._cleared_cookie())
                return
            if path == "/api/nodes":
                self._create_node(user)
                return
            if path == "/api/model/services/action":
                self._model_service_action(user)
                return
            if path == "/api/model/chat":
                self._model_chat()
                return
            if path == "/api/model/chat/stream":
                self._model_chat_stream()
                return
            if path == "/api/nodes/bulk":
                self._bulk_add_nodes(user)
                return
            if path == "/api/nodes/batch":
                self._batch_nodes(user)
                return
            if path == "/api/discovery/scan":
                config = self.app.config.current()
                payload = self._read_json(optional=True) or {}
                interface = payload.get("interface")
                if interface is not None and not isinstance(interface, str):
                    raise ValueError("网卡参数格式无效")
                cidrs = payload.get("cidrs")
                if cidrs is not None and not isinstance(cidrs, (str, list)):
                    raise ValueError("CIDR 参数格式无效")
                ssh_user = str(
                    payload.get("ssh_user") or config.ssh_user
                ).strip()
                if not SSH_USER_RE.fullmatch(ssh_user):
                    raise ValueError("SSH 用户名格式无效")
                ssh_port = _parse_ssh_port(
                    payload.get("ssh_port"), int(config.ssh_port)
                )
                ssh_key_path = str(
                    payload.get("ssh_key_path")
                    if payload.get("ssh_key_path") is not None
                    else config.ssh_key_path
                ).strip()[:500]
                ssh_password = str(payload.get("ssh_password") or "")
                if len(ssh_password) > 4096:
                    raise ValueError("SSH 密码过长")
                state = self.app.discovery.start_scan(
                    cidrs=cidrs,
                    interface=interface,
                    ssh_user=ssh_user,
                    ssh_port=ssh_port,
                    ssh_key_path=ssh_key_path,
                    ssh_password=ssh_password or None,
                )
                scope = interface or "自动网卡"
                ranges = ", ".join(state.get("subnets") or [])
                self.app.db.add_audit(
                    user,
                    "discovery.scan",
                    scope,
                    f"启动局域网扫描: {ranges}; SSH {ssh_user}@{ssh_port}",
                    self.client_address[0],
                )
                self._json({"scan": state})
                return
            if path == "/api/discovery/import":
                self._import_discovered(user)
                return
            match = re.fullmatch(r"/api/nodes/(\d+)/refresh", path)
            if match:
                node = self.app.db.get_node(int(match.group(1)))
                if not node:
                    self._error(404, "节点不存在")
                    return
                result = self.app.collector.refresh_node(node)
                if not result.get("ok"):
                    message = str(result.get("error") or "节点采集失败")
                    status = 409 if not node.get("enabled") else 502
                    self._error(status, message)
                    return
                metrics = result["metrics"]
                self.app.db.add_audit(
                    user,
                    "node.refresh",
                    node["host"],
                    "手动刷新节点指标",
                    self.client_address[0],
                )
                self._json({"ok": True, "metrics": metrics})
                return
            match = re.fullmatch(r"/api/nodes/(\d+)/test", path)
            if match:
                node = self.app.db.get_node(int(match.group(1)))
                if not node:
                    self._error(404, "节点不存在")
                    return
                started = time.perf_counter()
                try:
                    credentials = self.app.db.get_node_credentials(
                        int(match.group(1))
                    )
                    config = self.app.config.current()
                    if not credentials:
                        self._error(404, "节点不存在")
                        return
                    info = probe_via_ssh(
                        str(credentials["host"]),
                        config,
                        _parse_ssh_port(
                            credentials.get("ssh_port"),
                            int(config.ssh_port),
                        ),
                        str(
                            credentials.get("ssh_user")
                            or config.ssh_user
                        ),
                        str(
                            credentials.get("ssh_key_path")
                            if credentials.get("ssh_key_path") is not None
                            else config.ssh_key_path
                        ),
                        password=credentials.get("ssh_password") or None,
                    )
                except SSHError as exc:
                    self._error(
                        502,
                        sanitize_error_message(exc) or "SSH 连接测试失败",
                    )
                    return
                latency_ms = round((time.perf_counter() - started) * 1000, 1)
                self.app.db.add_audit(
                    user,
                    "node.test",
                    node["host"],
                    "SSH 连接测试成功",
                    self.client_address[0],
                )
                self._json(
                    {
                        "ok": True,
                        "latency_ms": latency_ms,
                        "host": info.get("host"),
                        "hostname": info.get("hostname"),
                        "os": info.get("os"),
                        "arch": info.get("arch"),
                        "addresses": info.get("addresses") or {},
                        "gpus": info.get("gpus") or [],
                    }
                )
                return
            self._error(404, "接口不存在")
        except ValueError as exc:
            self._error(400, str(exc) or "请求内容无效")
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception:
            LOGGER.exception("POST request failed")
            self._error(500, "服务器内部错误")

    def do_PATCH(self) -> None:
        try:
            user, session, error = self._authenticate()
            if error:
                return
            if not self._check_csrf(session):
                return
            path = urllib.parse.urlparse(self.path).path
            if path == "/api/alert-rules":
                payload = self._read_json()
                rules = payload.get("rules")
                if not isinstance(rules, dict) or not rules:
                    raise ValueError("没有可保存的告警规则")
                allowed = {key for key, *_ in ALERT_RULES}
                unknown = sorted(set(rules) - allowed)
                if unknown:
                    raise ValueError(f"未知告警规则: {', '.join(unknown)}")
                self.app.config.update(rules)
                self.app.notify_config_changed()
                self.app.db.add_audit(
                    user,
                    "alert-rules.update",
                    "runtime",
                    json.dumps(rules, ensure_ascii=False),
                    self.client_address[0],
                )
                self._json(self.app.alert_rules())
                return
            if path == "/api/settings":
                payload = self._read_json()
                updated = self.app.config.update(payload)
                self.app.notify_config_changed()
                self.app.db.add_audit(
                    user,
                    "settings.update",
                    "runtime",
                    json.dumps(
                        {key: value for key, value in updated.items() if key in payload},
                        ensure_ascii=False,
                    ),
                    self.client_address[0],
                )
                self._json(self.app.config.public_payload())
                return
            match = re.fullmatch(r"/api/nodes/(\d+)", path)
            if match:
                self._update_node(user, int(match.group(1)))
                return
            self._error(404, "接口不存在")
        except ValueError as exc:
            self._error(400, str(exc) or "请求内容无效")
        except Exception:
            LOGGER.exception("PATCH request failed")
            self._error(500, "服务器内部错误")

    def do_DELETE(self) -> None:
        try:
            user, session, error = self._authenticate()
            if error:
                return
            if not self._check_csrf(session):
                return
            match = re.fullmatch(
                r"/api/nodes/(\d+)", urllib.parse.urlparse(self.path).path
            )
            if not match:
                self._error(404, "接口不存在")
                return
            node = self.app.db.get_node(int(match.group(1)))
            if not node:
                self._error(404, "节点不存在")
                return
            self.app.db.delete_nodes([int(match.group(1))])
            self.app.db.add_audit(
                user, "node.delete", node["host"], node["name"], self.client_address[0]
            )
            self._json({"ok": True})
        except Exception:
            LOGGER.exception("DELETE request failed")
            self._error(500, "服务器内部错误")

    def _login(self) -> None:
        if self.app.db.count_users() == 0:
            self._error(409, "请先完成管理员初始化")
            return
        config = self.app.config.current()
        remote_addr = self.client_address[0]
        if not self.app.throttle.allowed(
            remote_addr,
            int(config.login_max_attempts),
            int(config.login_window_seconds),
        ):
            self._error(429, "登录尝试过多，请稍后再试")
            return
        payload = self._read_json()
        username = str(payload.get("username", "")).strip()
        password = str(payload.get("password", ""))
        user = self.app.db.get_user_by_username(username)
        if not user or not verify_password(password, user["password_hash"]):
            self.app.throttle.record_failure(
                remote_addr, int(config.login_window_seconds)
            )
            self._error(401, "用户名或密码错误")
            return
        self.app.throttle.reset(remote_addr)
        token, session = create_session(
            self.app.db,
            int(user["id"]),
            remote_addr,
            self.headers.get("User-Agent", ""),
            int(config.session_ttl_hours),
        )
        self.app.db.mark_login(int(user["id"]))
        self.app.db.add_audit(
            user, "auth.login", "session", "管理员登录", remote_addr
        )
        cookie = self._session_cookie(token, session["expires_at"])
        self._json(
            {
                "user": {
                    "id": user["id"],
                    "username": user["username"],
                    "role": user["role"],
                },
                "csrf_token": session["csrf_token"],
                "expires_at": session["expires_at"],
            },
            set_cookie=cookie,
        )

    def _setup(self) -> None:
        if self.app.db.count_users() > 0:
            self._error(409, "管理员已初始化")
            return
        payload = self._read_json()
        username = str(payload.get("username", "admin")).strip()
        password = str(payload.get("password", ""))
        if not USERNAME_RE.fullmatch(username):
            self._error(400, "用户名需为 3-32 位字母、数字、点、横线或下划线")
            return
        if len(password) < 10:
            self._error(400, "密码至少需要 10 个字符")
            return
        user = self.app.db.create_user(username, hash_password(password))
        config = self.app.config.current()
        token, session = create_session(
            self.app.db,
            int(user["id"]),
            self.client_address[0],
            self.headers.get("User-Agent", ""),
            int(config.session_ttl_hours),
        )
        self.app.db.add_audit(
            user, "auth.setup", "user", "初始化管理员账号", self.client_address[0]
        )
        cookie = self._session_cookie(token, session["expires_at"])
        self._json(
            {
                "user": user,
                "csrf_token": session["csrf_token"],
                "expires_at": session["expires_at"],
            },
            status=201,
            set_cookie=cookie,
        )

    def _authenticate(
        self,
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None, bool]:
        token = self._session_token()
        if not token:
            self._error(401, "请先登录")
            return None, None, True
        session = self.app.db.get_session(token_hash(token))
        if not session:
            self._error(401, "登录已过期", set_cookie=self._cleared_cookie())
            return None, None, True
        user = {
            "id": session["user_id"],
            "username": session["username"],
            "role": session["role"],
        }
        return user, session, False

    def _check_csrf(self, session: dict[str, Any]) -> bool:
        supplied = self.headers.get("X-CSRF-Token", "")
        valid = bool(supplied) and hmac.compare_digest(
            supplied, str(session.get("csrf_token", ""))
        )
        if not valid:
            self._error(403, "CSRF 校验失败")
        return valid

    def _model_chat_stream(self) -> None:
        payload = self._read_json(max_bytes=16_000_000)
        self.send_response(200)
        self._security_headers()
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

        stream_open = True

        def emit(event: dict[str, Any]) -> None:
            nonlocal stream_open
            if not stream_open:
                return
            data = (
                "data: " + json.dumps(event, ensure_ascii=False) + "\n\n"
            ).encode("utf-8")
            try:
                self.wfile.write(f"{len(data):X}\r\n".encode("ascii") + data + b"\r\n")
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                stream_open = False

        try:
            result = self.app.model_chat.chat(
                payload,
                on_delta=lambda text: emit({"type": "delta", "content": text}),
                on_reasoning=lambda text: emit({"type": "reasoning", "content": text}),
            )
            emit(
                {
                    "type": "done",
                    "usage": result.get("usage", {}),
                    "metrics": result.get("metrics", {}),
                    "reasoning": result.get("reasoning", ""),
                    "content": result.get("content", ""),
                }
            )
        except ChatError as exc:
            emit({"type": "error", "error": str(exc)})
        except Exception as exc:  # noqa: BLE001 - report, do not kill the stream
            LOGGER.exception("chat stream failed")
            emit({"type": "error", "error": f"服务器内部错误: {exc}"})
        finally:
            if stream_open:
                try:
                    self.wfile.write(b"0\r\n\r\n")
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    pass
            stream_open = False

    def _model_chat(self) -> None:
        payload = self._read_json(max_bytes=16_000_000)
        try:
            result = self.app.model_chat.chat(payload)
        except ChatError as exc:
            self._error(502, str(exc))
            return
        self._json(result)

    def _model_service_action(self, user: dict[str, Any] | None) -> None:
        payload = self._read_json()
        unit = str(payload.get("unit") or "").strip()
        action = str(payload.get("action") or "").strip().lower()
        if action not in ("start", "stop", "switch"):
            self._error(400, "未知操作")
            return
        try:
            result = self.app.model_control.submit(
                action, unit, user, self.client_address[0]
            )
        except ModelControlError as exc:
            message = str(exc)
            self._error(409 if "进行中" in message else 400, message)
            return
        self._json(result)

    def _create_node(self, user: dict[str, Any]) -> None:
        config = self.app.config.current()
        payload = self._read_json()
        host = str(payload.get("host", "")).strip()
        if not HOST_RE.fullmatch(host):
            self._error(400, "节点地址格式无效")
            return
        if self.app.db.get_node_by_host(host):
            self._error(409, "节点已存在")
            return
        ssh_user = str(payload.get("ssh_user") or config.ssh_user).strip()
        if not SSH_USER_RE.fullmatch(ssh_user):
            self._error(400, "SSH 用户名格式无效")
            return
        ssh_password = str(payload.get("ssh_password") or "")
        if len(ssh_password) > 4096:
            self._error(400, "SSH 密码过长")
            return
        node = self.app.db.add_node(
            host=host,
            name=str(
                payload.get("name")
                or _default_name(host, config.node_name_pattern)
            ).strip(),
            ssh_port=_parse_ssh_port(
                payload.get("ssh_port"), int(config.ssh_port)
            ),
            ssh_user=ssh_user,
            ssh_key_path=str(
                config.ssh_key_path
                if payload.get("ssh_key_path") is None
                else payload.get("ssh_key_path")
            ).strip()[:500],
            ssh_password=ssh_password or None,
            group_name=str(
                payload.get("group_name") or config.default_group_name
            ).strip()
            or config.default_group_name,
            tags=_tags(payload.get("tags"), int(config.max_tags_per_node))
            if "tags" in payload
            else list(config.default_tags),
        )
        self.app.db.add_audit(
            user, "node.create", host, node["name"], self.client_address[0]
        )
        self._json({"node": node}, status=201)

    def _bulk_add_nodes(self, user: dict[str, Any]) -> None:
        config = self.app.config.current()
        payload = self._read_json()
        default_group = str(
            payload.get("group_name") or config.default_group_name
        ).strip() or config.default_group_name
        default_tags = (
            _tags(payload.get("tags"), int(config.max_tags_per_node))
            if "tags" in payload
            else list(config.default_tags)
        )
        ssh_port = _parse_ssh_port(
            payload.get("ssh_port"), int(config.ssh_port)
        )
        ssh_user = str(payload.get("ssh_user") or config.ssh_user).strip()
        if not SSH_USER_RE.fullmatch(ssh_user):
            self._error(400, "SSH 用户名格式无效")
            return
        ssh_password = str(payload.get("ssh_password") or "")
        if len(ssh_password) > 4096:
            self._error(400, "SSH 密码过长")
            return
        ssh_key_path = str(
            config.ssh_key_path
            if payload.get("ssh_key_path") is None
            else payload.get("ssh_key_path")
        ).strip()[:500]
        items, errors = _parse_bulk_lines(
            str(payload.get("input", "")),
            default_group,
            default_tags,
            ssh_port,
            ssh_user,
            ssh_key_path,
            config.node_name_pattern,
            int(config.bulk_add_max_nodes),
            int(config.max_tags_per_node),
        )
        if not items:
            self._error(400, "没有可添加的节点地址")
            return
        added: list[dict[str, Any]] = []
        skipped: list[str] = []
        for item in items:
            if self.app.db.get_node_by_host(item["host"]):
                skipped.append(item["host"])
                continue
            try:
                added.append(
                    self.app.db.add_node(
                        host=item["host"],
                        name=item["name"],
                        ssh_port=item["ssh_port"],
                        ssh_user=item["ssh_user"],
                        ssh_key_path=item["ssh_key_path"],
                        ssh_password=ssh_password or None,
                        group_name=item["group_name"],
                        tags=item["tags"],
                    )
                )
            except sqlite3.IntegrityError:
                skipped.append(item["host"])
        self.app.db.add_audit(
            user,
            "node.bulk_add",
            "nodes",
            f"新增 {len(added)}，跳过 {len(skipped)}",
            self.client_address[0],
        )
        self._json(
            {
                "added": len(added),
                "skipped": skipped,
                "errors": errors,
                "nodes": added,
            },
            status=201 if added else 200,
        )

    def _batch_nodes(self, user: dict[str, Any]) -> None:
        config = self.app.config.current()
        payload = self._read_json()
        ids = [
            int(value)
            for value in payload.get("ids", [])
            if isinstance(value, int) or str(value).isdigit()
        ]
        if not ids:
            self._error(400, "请选择节点")
            return
        action = str(payload.get("action", ""))
        affected = 0
        if action == "enable":
            affected = self.app.db.set_nodes_enabled(ids, True)
        elif action == "disable":
            affected = self.app.db.set_nodes_enabled(ids, False)
        elif action == "delete":
            affected = self.app.db.delete_nodes(ids)
        elif action in {"group", "tags"}:
            for node_id in ids:
                fields: dict[str, Any] = {}
                if action == "group":
                    fields["group_name"] = str(
                        payload.get("group_name") or config.default_group_name
                    )
                else:
                    fields["tags"] = _tags(
                        payload.get("tags"), int(config.max_tags_per_node)
                    )
                if self.app.db.update_node(node_id, fields):
                    affected += 1
        else:
            self._error(400, "不支持的批量操作")
            return
        self.app.db.add_audit(
            user,
            f"node.batch_{action}",
            ",".join(str(value) for value in ids[:20]),
            f"影响 {affected} 个节点",
            self.client_address[0],
        )
        self._json({"ok": True, "affected": affected})

    def _update_node(self, user: dict[str, Any], node_id: int) -> None:
        config = self.app.config.current()
        node = self.app.db.get_node(node_id)
        if not node:
            self._error(404, "节点不存在")
            return
        payload = self._read_json()
        fields: dict[str, Any] = {}
        if "name" in payload:
            name = str(payload["name"]).strip()
            if not name:
                self._error(400, "节点名称不能为空")
                return
            fields["name"] = name[:80]
        if "host" in payload:
            host = str(payload["host"]).strip()
            if not HOST_RE.fullmatch(host):
                self._error(400, "节点地址格式无效")
                return
            fields["host"] = host
        if "ssh_port" in payload:
            fields["ssh_port"] = _parse_ssh_port(
                payload["ssh_port"], int(config.ssh_port)
            )
        if "ssh_user" in payload:
            user_value = str(payload["ssh_user"]).strip()
            if not SSH_USER_RE.fullmatch(user_value):
                self._error(400, "SSH 用户名格式无效")
                return
            fields["ssh_user"] = user_value
        if "ssh_key_path" in payload:
            fields["ssh_key_path"] = str(payload["ssh_key_path"]).strip()[:500]
        if "ssh_password" in payload:
            ssh_password = str(payload.get("ssh_password") or "")
            if len(ssh_password) > 4096:
                self._error(400, "SSH 密码过长")
                return
            if ssh_password:
                fields["ssh_password"] = ssh_password
        if payload.get("clear_ssh_password"):
            if fields.get("ssh_password"):
                self._error(400, "不能同时设置和清除 SSH 密码")
                return
            fields["clear_ssh_password"] = True
        if "group_name" in payload:
            fields["group_name"] = str(payload["group_name"]).strip()[
                :80
            ] or config.default_group_name
        if "tags" in payload:
            fields["tags"] = _tags(
                payload["tags"], int(config.max_tags_per_node)
            )
        if "enabled" in payload:
            fields["enabled"] = bool(payload["enabled"])
        try:
            updated = self.app.db.update_node(node_id, fields)
        except sqlite3.IntegrityError:
            self._error(409, "节点地址已存在")
            return
        audit_fields = dict(fields)
        if "ssh_password" in audit_fields:
            audit_fields["ssh_password"] = "configured"
        if "clear_ssh_password" in audit_fields:
            audit_fields["clear_ssh_password"] = "cleared"
        self.app.db.add_audit(
            user,
            "node.update",
            node["host"],
            json.dumps(audit_fields, ensure_ascii=False),
            self.client_address[0],
        )
        self._json({"node": updated})

    def _import_discovered(self, user: dict[str, Any]) -> None:
        config = self.app.config.current()
        payload = self._read_json()
        hosts = [str(value) for value in payload.get("hosts", [])]
        group_name = str(
            payload.get("group_name") or config.auto_import_group_name
        )
        discovered = {
            item["host"]: item
            for item in self.app.db.list_discovered(
                int(config.discovery_saved_lookback_seconds)
            )
        }
        added = 0
        skipped: list[str] = []
        for host in hosts:
            if self.app.db.get_node_by_host(host):
                skipped.append(host)
                continue
            info = discovered.get(host, {})
            info_details = info.get("info") or {}
            if info_details.get("authenticated") is not True:
                skipped.append(host)
                continue
            credentials = self.app.db.get_discovered_credentials(host) or {}
            try:
                self.app.db.add_node(
                    host=host,
                    name=info.get("name")
                    or _default_name(host, config.node_name_pattern),
                    ssh_port=_parse_ssh_port(
                        info.get("ssh_port"), int(config.ssh_port)
                    ),
                    ssh_user=str(info.get("ssh_user") or config.ssh_user),
                    ssh_key_path=str(
                        info.get("ssh_key_path") or config.ssh_key_path
                    ),
                    ssh_password=credentials.get("ssh_password") or None,
                    group_name=group_name,
                    tags=list(config.auto_import_tags),
                )
                added += 1
            except sqlite3.IntegrityError:
                skipped.append(host)
        self.app.db.add_audit(
            user,
            "discovery.import",
            ",".join(hosts[:20]),
            f"新增 {added}，跳过 {len(skipped)}",
            self.client_address[0],
        )
        self._json({"added": added, "skipped": skipped})

    def _export_nodes_csv(self) -> None:
        nodes = self.app.db.list_nodes()
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(
            [
                "name",
                "host",
                "ssh_port",
                "ssh_user",
                "ssh_key_path",
                "group_name",
                "tags",
                "enabled",
                "status",
            ]
        )
        for node in nodes:
            writer.writerow(
                [
                    node["name"],
                    node["host"],
                    node["ssh_port"],
                    node["ssh_user"],
                    node["ssh_key_path"],
                    node["group_name"],
                    "|".join(node["tags"]),
                    "1" if node["enabled"] else "0",
                    node["last_status"],
                ]
            )
        body = ("\ufeff" + output.getvalue()).encode("utf-8")
        self.send_response(200)
        self._security_headers()
        self.send_header("Content-Type", "text/csv; charset=utf-8")
        self.send_header(
            "Content-Disposition", 'attachment; filename="spark-nodes.csv"'
        )
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_static(self, path: str) -> None:
        static_dir = self.app.settings.static_dir.resolve()
        if path in {"", "/"}:
            target = static_dir / "index.html"
        else:
            relative = urllib.parse.unquote(path.lstrip("/"))
            target = (static_dir / relative).resolve()
            try:
                target.relative_to(static_dir)
            except ValueError:
                self._error(404, "页面不存在")
                return
        if not target.is_file():
            if "." not in path.rsplit("/", 1)[-1]:
                target = static_dir / "index.html"
            else:
                self._error(404, "文件不存在")
                return
        content = target.read_bytes()
        content_type = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        if content_type.startswith("text/") or content_type in {
            "application/javascript",
            "application/json",
            "image/svg+xml",
        }:
            content_type += "; charset=utf-8"
        self.send_response(200)
        self._security_headers()
        self.send_header("Content-Type", content_type)
        cache = (
            "no-cache"
            if target.name in {"index.html", "app.css", "app.js"}
            else "public, max-age=3600"
        )
        self.send_header("Cache-Control", cache)
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def _read_json(
        self, optional: bool = False, max_bytes: int = 2_000_000
    ) -> dict[str, Any]:
        length = _safe_int(
            self.headers.get("Content-Length"), 0, 0, 1 << 62
        )
        if length == 0:
            if optional:
                return {}
            raise ValueError("missing JSON body")
        if length > max_bytes:
            raise ValueError(f"请求体过大（上限 {max_bytes // 1024} KB）")
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise ValueError("invalid JSON") from exc
        if not isinstance(payload, dict):
            raise ValueError("JSON body must be an object")
        return payload

    def _json(
        self, payload: Any, status: int = 200, set_cookie: str | None = None
    ) -> None:
        body = json.dumps(
            payload, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
        self.send_response(status)
        self._security_headers()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        if set_cookie:
            self.send_header("Set-Cookie", set_cookie)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _error(
        self, status: int, message: str, set_cookie: str | None = None
    ) -> None:
        self._json({"error": message}, status=status, set_cookie=set_cookie)

    def _security_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; connect-src 'self'; font-src 'self'; "
            "frame-ancestors 'none'; base-uri 'self'; form-action 'self'",
        )
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")

    def _session_token(self) -> str:
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
        except Exception:
            return ""
        morsel = cookie.get(SESSION_COOKIE)
        return morsel.value if morsel else ""

    def _session_cookie(self, token: str, expires_at: int) -> str:
        max_age = max(1, expires_at - _now())
        parts = [
            f"{SESSION_COOKIE}={token}",
            "Path=/",
            f"Max-Age={max_age}",
            "HttpOnly",
            "SameSite=Strict",
        ]
        if self.app.config.current().cookie_secure:
            parts.append("Secure")
        return "; ".join(parts)

    @staticmethod
    def _cleared_cookie() -> str:
        return f"{SESSION_COOKIE}=; Path=/; Max-Age=0; HttpOnly; SameSite=Strict"


def run(settings: Settings) -> None:
    logging.basicConfig(
        level=os.getenv("SPARK_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    application = SparkGridApp(settings)
    application.start()
    server = SparkGridServer((settings.host, settings.port), application)
    LOGGER.info(
        "Spark Grid %s listening on http://%s:%s (SSH pull, user %s)",
        __version__,
        settings.host,
        settings.port,
        settings.ssh_user,
    )
    print(
        f"Spark Grid {__version__} running at http://{settings.host}:{settings.port}",
        flush=True,
    )
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        application.stop()
        server.server_close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Spark Grid monitoring platform")
    parser.add_argument("--host", default=None, help="listen address")
    parser.add_argument("--port", type=int, default=None, help="listen port")
    args = parser.parse_args()
    current = load_settings()
    if args.host or args.port:
        from dataclasses import replace

        current = replace(
            current,
            host=args.host or current.host,
            port=args.port or current.port,
        )
    run(current)


if __name__ == "__main__":
    main()
