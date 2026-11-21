from __future__ import annotations

import http.cookiejar
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from app.auth import hash_password, verify_password
from app.config import Settings
from app.db import Database
from app.main import SparkGridApp, SparkGridServer, _parse_bulk_lines
from app.model_monitor import (
    ModelMonitor,
    _arg_value,
    _docker_since,
    _parse_sglang_runtime,
    _parse_vllm_logs,
    _prometheus_samples,
    _sum_metric,
)
from app.ssh_runner import REMOTE_INFO_SCRIPT, REMOTE_METRICS_SCRIPT


class AuthTests(unittest.TestCase):
    def test_password_hash_round_trip(self) -> None:
        encoded = hash_password("a-long-test-password")
        self.assertTrue(verify_password("a-long-test-password", encoded))
        self.assertFalse(verify_password("wrong-password", encoded))
        self.assertNotIn("a-long-test-password", encoded)


class DatabaseTests(unittest.TestCase):
    def test_node_and_history(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = Database(Path(directory) / "test.db")
            node = db.add_node(
                host="192.0.2.11",
                name="spark1",
                ssh_port=22,
                ssh_user="spark",
                ssh_key_path="",
                group_name="G1",
                tags=["spark", "test"],
            )
            self.assertEqual(node["ssh_user"], "spark")
            self.assertEqual(node["tags"], ["spark", "test"])
            self.assertEqual(len(db.list_nodes()), 1)
            db.save_metrics(
                node["id"],
                {
                    "resources": {
                        "cpu_percent": 11.5,
                        "memory_percent": 22.5,
                        "temperature_max": 45,
                        "rx_rate": 100,
                        "tx_rate": 200,
                    },
                    "gpus": [{"utilization": 96.0, "load_percent": 2.0}],
                    "compact": {
                        "cpu": 11.5,
                        "memory": 22.5,
                        "gpu": 2.0,
                        "temperature": 45,
                        "rx_rate": 100,
                        "tx_rate": 200,
                    },
                },
                12.0,
            )
            history = db.load_history(node["id"])
            self.assertEqual(len(history), 1)
            self.assertEqual(history[0]["gpu"], 2.0)
            ranged = db.load_history_range(node["id"], 30)
            self.assertEqual(len(ranged), 1)
            self.assertEqual(ranged[0]["cpu"], 11.5)

    def test_model_throughput_history(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = Database(Path(directory) / "test.db")
            started_at = "2026-11-20T08:50:42Z"
            now = 1_800_000_000
            for offset, prefill, decode in [
                (125, 10.0, 1.0),
                (65, 20.0, 2.0),
                (5, 30.0, 3.0),
            ]:
                db.record_model_throughput(
                    started_at,
                    "vllm",
                    prefill,
                    decode,
                    timestamp=now - offset,
                )
            history = db.load_model_throughput_history(
                started_at, minutes=30, buckets=30, now=now
            )
            self.assertEqual(len(history["points"]), 30)
            values = [
                point
                for point in history["points"]
                if point["prefill_tokens_per_sec"] is not None
            ]
            self.assertEqual(
                [point["prefill_tokens_per_sec"] for point in values],
                [10.0, 20.0, 30.0],
            )
            self.assertEqual(
                [point["decode_tokens_per_sec"] for point in values],
                [1.0, 2.0, 3.0],
            )
            old_process = db.load_model_throughput_history(
                "2026-11-20T07:00:00Z", minutes=30, buckets=30, now=now
            )
            self.assertTrue(
                all(
                    point["prefill_tokens_per_sec"] is None
                    for point in old_process["points"]
                )
            )


class BulkParserTests(unittest.TestCase):
    def test_explicit_rows_and_range(self) -> None:
        rows, errors = _parse_bulk_lines(
            "192.0.2.12,spark2,G1,spark|prod\n192.0.2.13-15",
            "默认",
            ["lan"],
            22,
            "spark",
            "",
        )
        self.assertEqual(errors, [])
        self.assertEqual(len(rows), 4)
        self.assertEqual(rows[0]["name"], "spark2")
        self.assertEqual(rows[0]["group_name"], "G1")
        self.assertEqual(rows[0]["tags"], ["spark", "prod"])
        self.assertEqual(rows[-1]["host"], "192.0.2.15")


class RemoteScriptTests(unittest.TestCase):
    def test_remote_scripts_compile(self) -> None:
        compile(REMOTE_INFO_SCRIPT, "<remote-info>", "exec")
        compile(REMOTE_METRICS_SCRIPT, "<remote-metrics>", "exec")
        self.assertIn('"metrics_schema": 5', REMOTE_METRICS_SCRIPT)
        self.assertIn("power estimate", REMOTE_METRICS_SCRIPT)


class ModelMonitorParserTests(unittest.TestCase):
    def test_runtime_log_parser(self) -> None:
        lines = [
            "[2026-11-19 08:29:10 TP0 EP0] Prefill batch, #new-seq: 1, "
            "#new-token: 4096, #cached-token: 128, full token usage: 0.05, "
            "swa token usage: 0.00, #running-req: 2, #queue-req: 3, "
            "#pending-token: 4096, cuda graph: False, input throughput (token/s): 5640.13",
            "[2026-11-19 08:29:11 TP0 EP0] Decode batch, #running-req: 2, "
            "#full token: 0, full token usage: 0.06, #swa token: 0, "
            "swa token usage: 0.00, accept len: 1.62, accept rate: 0.12, "
            "cuda graph: True, gen throughput (token/s): 35.5, #queue-req: 1",
        ]
        parsed = _parse_sglang_runtime(lines)
        self.assertEqual(parsed["running_requests"], 2)
        self.assertEqual(parsed["queued_requests"], 1)
        self.assertEqual(parsed["pending_tokens"], 4096)
        self.assertEqual(parsed["prefill_tokens_per_sec"], 5640.13)
        self.assertEqual(parsed["decode_tokens_per_sec"], 35.5)
        self.assertEqual(parsed["full_token_usage_pct"], 6.0)
        self.assertEqual(parsed["accept_rate_pct"], 12.0)

    def test_arg_value(self) -> None:
        args = ["--served-model-name", "model-a", "--tp", "8"]
        self.assertEqual(_arg_value(args, "--served-model-name"), "model-a")
        self.assertEqual(_arg_value(args, "--tp"), "8")

    def test_docker_since(self) -> None:
        self.assertEqual(
            _docker_since("2026-11-20T08:50:42.123456789Z"),
            "2026-11-20T08:50:42.123456789Z",
        )
        self.assertIsNone(_docker_since("not-a-timestamp"))
        self.assertIsNone(_docker_since(None))

    def test_vllm_metrics_and_logs(self) -> None:
        text = """
# HELP vllm:num_requests_running Running requests.
vllm:num_requests_running{engine="0",model_name="model-a"} 3.0
vllm:num_requests_waiting{engine="0",model_name="model-a"} 2.0
vllm:kv_cache_usage_perc{engine="0",model_name="model-a"} 0.25
vllm:prompt_tokens_total{engine="0",model_name="model-a"} 1000.0
vllm:generation_tokens_total{engine="0",model_name="model-a"} 500.0
"""
        samples = _prometheus_samples(text)
        self.assertEqual(_sum_metric(samples, "vllm:num_requests_running"), 3.0)
        self.assertEqual(
            _sum_metric(samples, "vllm:num_requests_waiting"), 2.0
        )
        logs = [
            "(APIServer pid=1) INFO 11-20 04:07:32 [loggers.py:336] "
            "Engine 000: Avg prompt throughput: 176.2 tokens/s, "
            "Avg generation throughput: 281.4 tokens/s, Running: 1 reqs, "
            "Waiting: 0 reqs, GPU KV cache usage: 0.1%, "
            "Prefix cache hit rate: 70.9%",
            "Avg Draft acceptance rate: 56.0%",
        ]
        parsed = _parse_vllm_logs(logs)
        self.assertEqual(parsed["log_throughput"]["prompt"], 176.2)
        self.assertEqual(parsed["log_throughput"]["generation"], 281.4)
        self.assertEqual(parsed["accept_rate_pct"], 56.0)


class HttpIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(__file__).resolve().parent.parent
        self.settings = Settings(
            root_dir=root,
            data_dir=Path(self.temp.name),
            static_dir=root / "app" / "static",
            host="127.0.0.1",
            port=0,
            ssh_user="spark",
            ssh_port=22,
            ssh_key_path="",
            ssh_timeout=5,
            discovery_interval=600,
            poll_interval=3,
            node_timeout=2,
            session_ttl_hours=12,
            cookie_secure=False,
            history_points=120,
            history_retention_hours=24,
        )
        self.application = SparkGridApp(self.settings)
        self.server = SparkGridServer(("127.0.0.1", 0), self.application)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.cookies = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.cookies)
        )
        self.csrf = ""

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.temp.cleanup()

    def request(
        self, path: str, method: str = "GET", body: dict | None = None
    ) -> tuple[int, dict]:
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Accept": "application/json"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        if self.csrf:
            headers["X-CSRF-Token"] = self.csrf
        request = urllib.request.Request(
            self.base + path, data=data, headers=headers, method=method
        )
        try:
            with self.opener.open(request, timeout=5) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def test_setup_login_session_and_bulk_add(self) -> None:
        status, bootstrap = self.request("/api/bootstrap")
        self.assertEqual(status, 200)
        self.assertTrue(bootstrap["setup_required"])

        status, setup = self.request(
            "/api/setup",
            "POST",
            {"username": "admin", "password": "test-password-123"},
        )
        self.assertEqual(status, 201)
        self.csrf = setup["csrf_token"]

        status, session = self.request("/api/session")
        self.assertEqual(status, 200)
        self.assertEqual(session["user"]["username"], "admin")

        status, result = self.request(
            "/api/nodes/bulk",
            "POST",
            {
                "input": "192.0.2.11,spark1,G1,spark",
                "group_name": "自动发现",
                "tags": "lan",
                "ssh_user": "spark",
                "ssh_port": 22,
                "ssh_key_path": "",
            },
        )
        self.assertEqual(status, 201)
        self.assertEqual(result["added"], 1)

        status, dashboard = self.request("/api/dashboard")
        self.assertEqual(status, 200)
        self.assertEqual(dashboard["summary"]["total"], 1)
        node_id = dashboard["nodes"][0]["id"]
        status, history = self.request(f"/api/nodes/{node_id}/history?minutes=30")
        self.assertEqual(status, 200)
        self.assertEqual(history["minutes"], 30)
        self.assertEqual(history["series"], [])
        status, cluster_history = self.request("/api/history/cluster?minutes=30")
        self.assertEqual(status, 200)
        self.assertEqual(cluster_history["minutes"], 30)
        self.assertEqual(cluster_history["series"][str(node_id)], [])

        status, _ = self.request("/api/logout", "POST", {})
        self.assertEqual(status, 200)
        status, _ = self.request("/api/session")
        self.assertEqual(status, 401)


if __name__ == "__main__":
    unittest.main()
