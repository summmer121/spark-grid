from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from .db import Database
from .ssh_runner import collect_via_ssh


LOGGER = logging.getLogger("spark-grid.collector")


class MetricsCollector:
    def __init__(self, db: Database, config: Any):
        self.db = db
        self.config = config
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._failures: dict[int, int] = {}
        self._net_samples: dict[int, tuple[float, dict[str, tuple[int, int]]]] = {}
        self._last_prune = 0.0

    def _current(self) -> Any:
        current = getattr(self.config, "current", None)
        return current() if callable(current) else self.config

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._run, name="spark-ssh-collector", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def notify_config_changed(self) -> None:
        self._wake.set()

    def refresh_node(self, node: dict[str, Any]) -> dict[str, Any]:
        if not node.get("enabled"):
            return {"ok": False, "error": "节点已暂停监控"}
        return self._collect_one(node)

    def _collect_one(self, node: dict[str, Any]) -> dict[str, Any]:
        node_id = int(node["id"])
        try:
            credentials = self.db.get_node_credentials(node_id)
            if not credentials:
                raise ValueError("节点不存在或凭据已被移除")
            metrics, latency_ms = collect_via_ssh(
                credentials,
                self._current(),
                password=credentials.get("ssh_password") or None,
            )
            if int(metrics.get("metrics_schema") or 0) < 5:
                raise ValueError("unsupported metrics schema")
            resources = metrics.setdefault("resources", {})
            now = float(metrics.get("timestamp") or time.time())
            previous = self._net_samples.get(node_id)
            current: dict[str, tuple[int, int]] = {}
            for item in metrics.get("network") or []:
                name = str(item.get("name") or "")
                if not name or name in current:
                    continue
                rx = item.get("rx_bytes")
                tx = item.get("tx_bytes")
                if isinstance(rx, (int, float)) and isinstance(tx, (int, float)):
                    current[name] = (int(rx), int(tx))
            interface_rates: dict[str, tuple[float, float]] = {}
            if previous:
                previous_time, previous_counters = previous
                elapsed = max(0.1, now - previous_time)
                for name, (rx, tx) in current.items():
                    if name not in previous_counters:
                        interface_rates[name] = (0.0, 0.0)
                        continue
                    previous_rx, previous_tx = previous_counters[name]
                    interface_rates[name] = (
                        max(0, rx - previous_rx) / elapsed,
                        max(0, tx - previous_tx) / elapsed,
                    )
            for item in metrics.get("network") or []:
                name = str(item.get("name") or "")
                rx_rate, tx_rate = interface_rates.get(name, (0.0, 0.0))
                item["rx_rate"] = round(rx_rate, 1)
                item["tx_rate"] = round(tx_rate, 1)
            self._net_samples[node_id] = (now, current)
            rx_rate = sum(rate[0] for rate in interface_rates.values())
            tx_rate = sum(rate[1] for rate in interface_rates.values())
            resources["rx_rate"] = round(rx_rate, 1)
            resources["tx_rate"] = round(tx_rate, 1)
            compact = metrics.setdefault("compact", {})
            compact["rx_rate"] = resources["rx_rate"]
            compact["tx_rate"] = resources["tx_rate"]
            self.db.save_metrics(node_id, metrics, latency_ms)
            self._failures[node_id] = 0
            return {"ok": True, "metrics": metrics, "latency_ms": latency_ms}
        except Exception as exc:
            failure_count = self._failures.get(node_id, 0) + 1
            self._failures[node_id] = failure_count
            message = self.db.record_node_error(node_id, exc)
            if failure_count >= max(1, int(self._current().offline_failures)):
                self.db.mark_node_offline(node_id)
            return {
                "ok": False,
                "error": message or "SSH 采集失败",
                "failure_count": failure_count,
            }

    def _run(self) -> None:
        while not self._stop.is_set():
            config = self._current()
            started = time.monotonic()
            nodes = [
                node for node in self.db.list_nodes() if node.get("enabled", False)
            ]
            if nodes:
                workers = min(max(1, int(config.collector_concurrency)), len(nodes))
                with ThreadPoolExecutor(max_workers=workers) as executor:
                    futures = {
                        executor.submit(self._collect_one, node): node for node in nodes
                    }
                    for future in as_completed(futures):
                        node = futures[future]
                        try:
                            result = future.result()
                            if not result.get("ok"):
                                LOGGER.warning(
                                    "collector failed for %s: %s",
                                    node.get("host"),
                                    result.get("error"),
                                )
                        except Exception as exc:
                            message = self.db.record_node_error(int(node["id"]), exc)
                            LOGGER.exception(
                                "unexpected collector failure for %s: %s",
                                node.get("host"),
                                message,
                            )
            if (
                time.monotonic() - self._last_prune
                > max(30.0, float(config.prune_interval_seconds))
            ):
                self.db.prune_history(int(config.history_retention_hours))
                self.db.purge_expired_sessions()
                self._last_prune = time.monotonic()
            elapsed = time.monotonic() - started
            wait_for = max(0.5, float(config.poll_interval) - elapsed)
            self._wake.wait(wait_for)
            self._wake.clear()
