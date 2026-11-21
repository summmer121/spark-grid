from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from .config import Settings
from .db import Database
from .ssh_runner import SSHError, collect_via_ssh


class MetricsCollector:
    def __init__(self, db: Database, settings: Settings):
        self.db = db
        self.settings = settings
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._failures: dict[int, int] = {}
        self._net_samples: dict[int, tuple[float, dict[str, tuple[int, int]]]] = {}
        self._last_prune = 0.0

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._run, name="spark-ssh-collector", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def refresh_node(self, node: dict[str, Any]) -> dict[str, Any] | None:
        if not node.get("enabled"):
            return None
        return self._collect_one(node)

    def _collect_one(self, node: dict[str, Any]) -> dict[str, Any] | None:
        node_id = int(node["id"])
        try:
            metrics, latency_ms = collect_via_ssh(node, self.settings)
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
            return metrics
        except (SSHError, OSError, ValueError, KeyError):
            failure_count = self._failures.get(node_id, 0) + 1
            self._failures[node_id] = failure_count
            if failure_count >= 2:
                self.db.mark_node_offline(node_id)
            return None

    def _run(self) -> None:
        while not self._stop.is_set():
            started = time.monotonic()
            nodes = [
                node for node in self.db.list_nodes() if node.get("enabled", False)
            ]
            if nodes:
                with ThreadPoolExecutor(max_workers=min(8, len(nodes))) as executor:
                    futures = {
                        executor.submit(self._collect_one, node): node for node in nodes
                    }
                    for future in as_completed(futures):
                        try:
                            future.result()
                        except Exception:
                            pass
            if time.monotonic() - self._last_prune > 600:
                self.db.prune_history(self.settings.history_retention_hours)
                self.db.purge_expired_sessions()
                self._last_prune = time.monotonic()
            elapsed = time.monotonic() - started
            self._stop.wait(max(0.5, self.settings.poll_interval - elapsed))
