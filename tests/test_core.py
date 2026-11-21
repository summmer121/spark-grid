from __future__ import annotations

import http.cookiejar
import importlib.util
import ipaddress
import json
import os
import sqlite3
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.auth import hash_password, verify_password
from app.config import Settings, load_settings
from app.db import Database
from app.discovery import DiscoveryService, LocalIPv4Interface
from app.main import SparkGridApp, SparkGridServer, _parse_bulk_lines
from app.model_monitor import (
    ModelMonitor,
    _arg_value,
    _container_command,
    _docker_since,
    _model_base_url,
    _parse_sglang_runtime,
    _parse_vllm_logs,
    _prometheus_samples,
    _sum_metric,
)
from app.runtime_config import RuntimeConfig
from app.ssh_runner import (
    REMOTE_INFO_SCRIPT,
    REMOTE_METRICS_SCRIPT,
    _render_remote_script,
    _run_script,
    _ssh_base,
    probe_via_ssh,
)


class AuthTests(unittest.TestCase):
    def test_password_hash_round_trip(self) -> None:
        encoded = hash_password("a-long-test-password")
        self.assertTrue(verify_password("a-long-test-password", encoded))
        self.assertFalse(verify_password("wrong-password", encoded))
        self.assertNotIn("a-long-test-password", encoded)


class DatabaseTests(unittest.TestCase):
    def test_new_database_does_not_assume_a_spark_ssh_user(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = Database(Path(directory) / "test.db")
            node = db.add_node(host="192.0.2.10", name="node-10")
            db.save_discovery(
                {"host": "192.0.2.10", "hostname": "node-10"},
                "192.0.2.10",
            )

            self.assertEqual(node["ssh_user"], "")
            self.assertEqual(
                db.get_discovered_credentials("192.0.2.10")["ssh_user"],
                "",
            )
            connection = sqlite3.connect(db.path)
            try:
                for table in ("nodes", "discovered_nodes"):
                    columns = {
                        row[1]: row[4]
                        for row in connection.execute(
                            f"PRAGMA table_info({table})"
                        )
                    }
                    self.assertEqual(columns["ssh_user"], "''")
            finally:
                connection.close()

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

    def test_ssh_password_is_encrypted_and_never_returned(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = Database(root / "test.db")
            password = "database-secret-42"
            node = db.add_node(
                host="192.0.2.21",
                name="spark-password",
                ssh_user="ops",
                ssh_password=password,
            )

            self.assertTrue(node["ssh_password_configured"])
            self.assertNotIn("ssh_password", node)
            self.assertEqual(
                db.get_node_credentials(node["id"])["ssh_password"],
                password,
            )
            for database_file in root.glob("test.db*"):
                self.assertNotIn(password.encode(), database_file.read_bytes())

            updated = db.update_node(node["id"], {"ssh_password": ""})
            self.assertTrue(updated["ssh_password_configured"])
            self.assertEqual(
                db.get_node_credentials(node["id"])["ssh_password"],
                password,
            )

            cleared = db.update_node(node["id"], {"clear_ssh_password": True})
            self.assertFalse(cleared["ssh_password_configured"])
            self.assertEqual(
                db.get_node_credentials(node["id"])["ssh_password"],
                "",
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
            "spark-{last_octet}",
            256,
            32,
        )
        self.assertEqual(errors, [])
        self.assertEqual(len(rows), 4)
        self.assertEqual(rows[0]["name"], "spark2")
        self.assertEqual(rows[0]["group_name"], "G1")
        self.assertEqual(rows[0]["tags"], ["spark", "prod"])
        self.assertEqual(rows[-1]["host"], "192.0.2.15")


class DeploymentConfigTests(unittest.TestCase):
    @staticmethod
    def _load_tls_proxy_module():
        path = Path(__file__).resolve().parent.parent / "deploy" / "tls_proxy.py"
        spec = importlib.util.spec_from_file_location("spark_grid_tls_proxy", path)
        if spec is None or spec.loader is None:
            raise RuntimeError("Could not load tls_proxy.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        return module

    def test_tls_proxy_uses_environment_and_cli_overrides(self) -> None:
        module = self._load_tls_proxy_module()
        environment = {
            "SPARK_TLS_LISTEN": "192.0.2.30",
            "SPARK_TLS_PORT": "3443",
            "SPARK_TLS_TARGET": "127.0.0.1:16301",
            "SPARK_TLS_CERT": "/config/site.crt",
            "SPARK_TLS_KEY": "/config/site.key",
            "SPARK_TLS_CONNECT_TIMEOUT": "7.5",
            "SPARK_TLS_POLL_TIMEOUT": "45",
        }
        parser = module._build_parser(environment)
        config = module.resolve_config(parser.parse_args([]), environment)
        self.assertEqual(config.listen, "192.0.2.30")
        self.assertEqual(config.port, 3443)
        self.assertEqual(config.target, ("127.0.0.1", 16301))
        self.assertEqual(config.cert, "/config/site.crt")
        self.assertEqual(config.key, "/config/site.key")
        self.assertEqual(config.connect_timeout, 7.5)
        self.assertEqual(config.poll_timeout, 45.0)

        overridden = module.resolve_config(
            parser.parse_args(
                [
                    "--listen",
                    "::1",
                    "--port",
                    "9443",
                    "--target",
                    "[::1]:26301",
                    "--cert",
                    "/override/site.crt",
                    "--key",
                    "/override/site.key",
                ]
            ),
            environment,
        )
        self.assertEqual(overridden.listen, "::1")
        self.assertEqual(overridden.port, 9443)
        self.assertEqual(overridden.target, ("::1", 26301))
        self.assertEqual(overridden.cert, "/override/site.crt")
        self.assertEqual(overridden.key, "/override/site.key")

        with self.assertRaises(ValueError):
            module.resolve_config(parser.parse_args(["--cert", ""]), {})

    def test_runtime_paths_and_float_intervals_are_configurable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            environment = {
                "SPARK_MONITOR_DATA": "runtime-data",
                "SPARK_DATABASE_PATH": "state/custom-grid.db",
                "SPARK_DATABASE_TIMEOUT": "7.5",
                "SPARK_CREDENTIAL_KEY_FILE": "secrets/custom-credential.key",
                "SPARK_SSH_KEY_PATH": "keys/cluster",
                "SPARK_SSH_BIN": "/opt/openssh/bin/ssh -F /etc/cluster/ssh_config",
                "SPARK_SSH_KNOWN_HOSTS_PATH": "state/known_hosts",
                "SPARK_SSH_CONTROL_PERSIST": "0",
                "SPARK_SSH_INTERFACE_QUERY_TIMEOUT": "1.25",
                "SPARK_SSH_GPU_INFO_TIMEOUT": "1.5",
                "SPARK_SSH_GPU_METRICS_TIMEOUT": "2.5",
                "SPARK_SSH_CPU_SAMPLE_INTERVAL": "0.75",
                "SPARK_GPU_QUERY_COMMAND": "/opt/gpu/bin/query --json",
                "SPARK_MODEL_SCHEME": "https",
                "SPARK_POLL_INTERVAL": "2.5",
                "SPARK_SSH_TIMEOUT": "4.5",
                "SPARK_NODE_TIMEOUT": "1.25",
            }
            with patch("app.config.ROOT_DIR", root), patch.dict(
                os.environ, environment, clear=True
            ):
                settings = load_settings()
                application = SparkGridApp(settings)
            self.assertEqual(settings.data_dir, (root / "runtime-data").resolve())
            self.assertEqual(settings.ssh_key_path, str((root / "keys" / "cluster").resolve()))
            self.assertEqual(
                settings.database_path,
                (root / "state" / "custom-grid.db").resolve(),
            )
            self.assertEqual(
                settings.credential_key_path,
                (root / "secrets" / "custom-credential.key").resolve(),
            )
            self.assertEqual(settings.ssh_control_persist, 0)
            self.assertEqual(settings.poll_interval, 2.5)
            self.assertEqual(settings.ssh_timeout, 4.5)
            self.assertEqual(settings.node_timeout, 1.25)
            self.assertEqual(settings.database_timeout_seconds, 7.5)
            self.assertEqual(application.db.path, settings.database_path)
            self.assertEqual(application.db.timeout_seconds, 7.5)
            values = application.config.values()
            self.assertEqual(
                values["ssh_bin"],
                "/opt/openssh/bin/ssh -F /etc/cluster/ssh_config",
            )
            self.assertEqual(values["ssh_known_hosts_path"], "state/known_hosts")
            self.assertEqual(values["ssh_interface_query_timeout_seconds"], 1.25)
            self.assertEqual(values["ssh_gpu_info_timeout_seconds"], 1.5)
            self.assertEqual(values["ssh_gpu_metrics_timeout_seconds"], 2.5)
            self.assertEqual(values["ssh_cpu_sample_interval_seconds"], 0.75)
            self.assertEqual(values["model_scheme"], "https")

    def test_ssh_user_defaults_to_the_running_service_account(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch("app.config.ROOT_DIR", root), patch(
                "app.config.getpass.getuser", return_value="cluster-runner"
            ), patch.dict(
                os.environ,
                {"SPARK_MONITOR_DATA": "data"},
                clear=True,
            ):
                settings = load_settings()
            self.assertEqual(settings.ssh_user, "cluster-runner")

            with patch("app.config.ROOT_DIR", root), patch(
                "app.config.getpass.getuser", return_value="ignored"
            ), patch.dict(
                os.environ,
                {
                    "SPARK_MONITOR_DATA": "data",
                    "SPARK_SSH_USER": "cluster-ops",
                },
                clear=True,
            ):
                overridden = load_settings()
            self.assertEqual(overridden.ssh_user, "cluster-ops")

    def test_timezone_defaults_follow_system_and_allow_explicit_override(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            environment = {
                "SPARK_MONITOR_DATA": "data",
                "SPARK_SSH_USER": "ops",
            }
            with patch("app.config.ROOT_DIR", root), patch.dict(
                os.environ, environment, clear=True
            ), patch(
                "app.runtime_config.system_timezone_name",
                return_value="Europe/Berlin",
            ):
                settings = load_settings()
                db = Database(root / "settings.db")
                runtime = RuntimeConfig(db, settings)
                values = runtime.values()
            self.assertEqual(values["ui_timezone"], "Europe/Berlin")
            self.assertEqual(values["model_timezone"], "Europe/Berlin")

            with patch("app.config.ROOT_DIR", root), patch.dict(
                os.environ,
                {
                    **environment,
                    "SPARK_UI_TIMEZONE": "UTC",
                    "SPARK_MODEL_TIMEZONE": "Europe/London",
                },
                clear=True,
            ), patch(
                "app.runtime_config.system_timezone_name",
                return_value="Europe/Berlin",
            ):
                settings = load_settings()
                db = Database(root / "override.db")
                runtime = RuntimeConfig(db, settings)
                values = runtime.values()
            self.assertEqual(values["ui_timezone"], "UTC")
            self.assertEqual(values["model_timezone"], "Europe/London")

    def test_deployment_assets_do_not_assume_one_environment(self) -> None:
        root = Path(__file__).resolve().parent.parent
        start = (root / "scripts" / "start.sh").read_text(encoding="utf-8")
        stop = (root / "scripts" / "stop.sh").read_text(encoding="utf-8")
        check_ssh = (root / "scripts" / "check_cluster_ssh.sh").read_text(
            encoding="utf-8"
        )
        system_unit = (root / "deploy" / "spark-grid.system.service").read_text(
            encoding="utf-8"
        )
        tls_unit = (root / "deploy" / "spark-grid-tls.service").read_text(
            encoding="utf-8"
        )
        system_installer = (
            root / "scripts" / "install_system_service.sh"
        ).read_text(encoding="utf-8")
        frontend = (root / "app" / "static" / "js" / "app.js").read_text(
            encoding="utf-8"
        )

        for variable in (
            "SPARK_PYTHON",
            "SPARK_PID_FILE",
            "SPARK_LOG_FILE",
            "SPARK_HEALTH_URL",
            "SPARK_HEALTH_TIMEOUT",
        ):
            self.assertIn(variable, start)
        self.assertNotIn("curl --fail", start)
        self.assertIn("SPARK_PID_FILE", stop)
        self.assertIn("SPARK_HOSTS", check_ssh)
        self.assertNotIn("DEFAULT_HOSTS=", check_ssh)
        self.assertNotIn("spark1 spark2", check_ssh)
        self.assertIn("@SPARK_ROOT@", system_unit)
        self.assertIn("@SPARK_PYTHON@", system_unit)
        self.assertIn("@SPARK_READ_WRITE_PATHS@", system_unit)
        self.assertIn("@SPARK_TLS_PROXY@", tls_unit)
        self.assertIn("SPARK_DATABASE_PATH", system_installer)
        self.assertIn("SPARK_CREDENTIAL_KEY_FILE", system_installer)
        self.assertIn("resolvedOptions().timeZone", frontend)
        self.assertNotIn('ui_timezone", "Asia/Shanghai"', frontend)
        self.assertNotIn("/home/spark/spark-monitor", system_unit)
        self.assertNotIn("/opt/spark-grid", tls_unit)

    def test_runtime_environment_variables_are_documented(self) -> None:
        root = Path(__file__).resolve().parent.parent
        documents = {
            path: (root / path).read_text(encoding="utf-8")
            for path in (".env.example", "deploy/example.env", "README.md")
        }
        for variable in (
            "SPARK_DATABASE_TIMEOUT",
            "SPARK_SSH_BIN",
            "SPARK_SSH_KNOWN_HOSTS_PATH",
            "SPARK_SSH_INTERFACE_QUERY_TIMEOUT",
            "SPARK_SSH_GPU_INFO_TIMEOUT",
            "SPARK_SSH_GPU_METRICS_TIMEOUT",
            "SPARK_SSH_CPU_SAMPLE_INTERVAL",
            "SPARK_MODEL_SCHEME",
        ):
            for path, content in documents.items():
                self.assertIn(variable, content, f"{variable} missing from {path}")


class DiscoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        settings = SimpleNamespace(
            ssh_user="spark",
            ssh_port=22,
            ssh_key_path="",
            node_timeout=2,
            discovery_max_hosts=2048,
            discovery_port_concurrency=8,
            discovery_port_timeout_seconds=0.1,
            discovery_ssh_concurrency=2,
            discovery_saved_lookback_seconds=2592000,
            discovery_reuse_saved_ports=True,
        )
        self.discovery = DiscoveryService(None, settings)  # type: ignore[arg-type]
        self.interfaces = [
            LocalIPv4Interface(
                name="lan1",
                address="192.0.2.10",
                netmask="255.255.255.0",
                network=ipaddress.ip_network("192.0.2.0/24"),
                physical=True,
            ),
            LocalIPv4Interface(
                name="lan2",
                address="198.51.100.20",
                netmask="255.255.255.0",
                network=ipaddress.ip_network("198.51.100.0/24"),
                physical=True,
            ),
        ]

    @patch("app.discovery._local_ipv4_interfaces")
    def test_interfaces_are_reported_from_runtime(self, local_interfaces) -> None:
        local_interfaces.return_value = self.interfaces

        result = self.discovery.interfaces()

        self.assertEqual([item["name"] for item in result], ["lan1", "lan2"])
        self.assertEqual(result[0]["cidr"], "192.0.2.0/24")

    @patch("app.discovery.threading.Thread")
    @patch("app.discovery._local_ipv4_interfaces")
    def test_selected_interface_uses_its_network(
        self, local_interfaces, thread_class
    ) -> None:
        local_interfaces.return_value = self.interfaces

        state = self.discovery.start_scan(interface="lan2")

        self.assertEqual(state["interface"], "lan2")
        self.assertEqual(state["subnets"], ["198.51.100.0/24"])
        self.assertEqual(state["total"], 254)
        thread_class.return_value.start.assert_called_once()

    @patch("app.discovery._local_ipv4_interfaces")
    def test_cidr_must_belong_to_selected_interface(self, local_interfaces) -> None:
        local_interfaces.return_value = self.interfaces

        with self.assertRaisesRegex(ValueError, "不属于网卡"):
            self.discovery.start_scan(
                cidrs=["203.0.113.0/24"],
                interface="lan1",
            )

    @patch("app.discovery.threading.Thread")
    @patch("app.discovery._local_ipv4_interfaces")
    def test_manual_cidr_does_not_require_an_interface(
        self, local_interfaces, thread_class
    ) -> None:
        local_interfaces.return_value = self.interfaces

        state = self.discovery.start_scan(cidrs=["203.0.113.0/30"])

        self.assertIsNone(state["interface"])
        self.assertEqual(state["subnets"], ["203.0.113.0/30"])
        self.assertEqual(state["total"], 2)
        thread_class.return_value.start.assert_called_once()

    @patch("app.discovery.threading.Thread")
    @patch("app.discovery._local_ipv4_interfaces")
    def test_network_address_without_prefix_uses_interface_cidr(
        self, local_interfaces, thread_class
    ) -> None:
        local_interfaces.return_value = self.interfaces

        state = self.discovery.start_scan(
            cidrs=["192.0.2.0"],
            interface="lan1",
        )

        self.assertEqual(state["subnets"], ["192.0.2.0/24"])
        self.assertEqual(state["total"], 254)
        thread_class.return_value.start.assert_called_once()

    @patch("app.discovery._local_ipv4_interfaces")
    def test_unknown_interface_is_rejected(self, local_interfaces) -> None:
        local_interfaces.return_value = self.interfaces

        with self.assertRaisesRegex(ValueError, "不存在"):
            self.discovery.start_scan(interface="missing0")

    @patch("app.discovery.probe_via_ssh")
    @patch("app.discovery._port_open")
    def test_rescan_reuses_saved_password(
        self, port_open, probe_via_ssh
    ) -> None:
        port_open.return_value = True
        probe_via_ssh.return_value = {
            "service": "spark-ssh-probe",
            "host": "192.0.2.31",
            "hostname": "spark-password",
            "machine_id": "machine-31",
        }
        with tempfile.TemporaryDirectory() as directory:
            db = Database(Path(directory) / "test.db")
            db.save_discovery(
                {
                    "service": "spark-ssh-probe",
                    "host": "192.0.2.31",
                    "hostname": "spark-password",
                    "machine_id": "machine-31",
                },
                "192.0.2.31",
                ssh_user="ops",
                ssh_port=2222,
                ssh_key_path="",
                ssh_password="scan-secret-31",
                authenticated=True,
            )
            discovery = DiscoveryService(
                db,
                SimpleNamespace(
                    ssh_user="spark",
                    ssh_port=22,
                    ssh_key_path="",
                    node_timeout=2,
                    discovery_max_hosts=2048,
                    discovery_port_concurrency=8,
                    discovery_port_timeout_seconds=0.1,
                    discovery_ssh_concurrency=2,
                    discovery_saved_lookback_seconds=2592000,
                    discovery_reuse_saved_ports=True,
                ),
            )

            discovery._run_scan(
                [("192.0.2.31", None)],
                "spark",
                22,
                "",
                "",
                {
                    "port_concurrency": 8,
                    "port_timeout_seconds": 0.1,
                    "ssh_concurrency": 2,
                    "saved_lookback_seconds": 2592000,
                    "reuse_saved_ports": True,
                    "config": discovery._current(),
                },
            )

        called = probe_via_ssh.call_args.args
        self.assertEqual(called[2:6], (2222, "ops", "", "scan-secret-31"))


class RemoteScriptTests(unittest.TestCase):
    def test_remote_scripts_compile(self) -> None:
        compile(REMOTE_INFO_SCRIPT, "<remote-info>", "exec")
        compile(REMOTE_METRICS_SCRIPT, "<remote-metrics>", "exec")
        self.assertIn('"metrics_schema": 5', REMOTE_METRICS_SCRIPT)
        self.assertIn("power estimate", REMOTE_METRICS_SCRIPT)
        self.assertNotIn("200000", REMOTE_METRICS_SCRIPT)
        self.assertNotIn("1.1.1.1", REMOTE_INFO_SCRIPT)
        self.assertNotIn("1.1.1.1", REMOTE_METRICS_SCRIPT)
        self.assertNotIn('ip.startswith("192.168.")', REMOTE_INFO_SCRIPT)
        self.assertNotIn('ip.startswith("192.168.")', REMOTE_METRICS_SCRIPT)

    def test_remote_scripts_use_configurable_gpu_command(self) -> None:
        settings = SimpleNamespace(
            network_interface_exclude_prefixes=("lo", "docker"),
            gpu_query_command="/opt/vendor/gpu-query --format csv",
            gpu_power_estimate_enabled=True,
            gpu_power_estimate_names=("Vendor GPU",),
            gpu_power_estimate_idle_watts=12,
            gpu_power_estimate_rated_watts=240,
            network_route_probe_host="192.0.2.1",
        )
        rendered = _render_remote_script(settings, REMOTE_INFO_SCRIPT)
        compile(rendered, "<remote-info-custom>", "exec")
        self.assertIn("('/opt/vendor/gpu-query', '--format', 'csv')", rendered)
        self.assertIn("'192.0.2.1'", rendered)
        self.assertNotIn("nvidia-smi", rendered)

        settings.gpu_query_command = ""
        disabled = _render_remote_script(settings, REMOTE_METRICS_SCRIPT)
        compile(disabled, "<remote-metrics-disabled>", "exec")
        self.assertIn("gpu_command = ()", disabled)
        self.assertNotIn("nvidia-smi", disabled)

    def test_ssh_control_persist_zero_and_remote_python_arguments(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            known_hosts = Path(directory) / "known-hosts" / "cluster"
            settings = SimpleNamespace(
                root_dir=Path(directory),
                data_dir=Path(directory),
                ssh_bin="/opt/openssh/bin/ssh -F /etc/cluster/ssh_config",
                ssh_known_hosts_path=str(known_hosts),
                ssh_connect_timeout=7.5,
                ssh_control_persist=0,
                ssh_strict_host_key_checking="accept-new",
                ssh_remote_python="/usr/bin/python3 -u",
                gpu_query_command="",
                gpu_power_estimate_enabled=False,
                gpu_power_estimate_names=(),
                gpu_power_estimate_idle_watts=0,
                gpu_power_estimate_rated_watts=0,
                network_interface_exclude_prefixes=("lo",),
            )
            command = _ssh_base(
                settings,
                "192.0.2.10",
                22,
                "ops",
                "",
            )
            self.assertEqual(
                command[:3],
                [
                    "/opt/openssh/bin/ssh",
                    "-F",
                    "/etc/cluster/ssh_config",
                ],
            )
            self.assertIn(f"UserKnownHostsFile={known_hosts}", command)
            self.assertIn("ConnectTimeout=8", command)
            self.assertIn("ControlPersist=no", command)

            response = json.dumps(
                {
                    "service": "spark-ssh-probe",
                    "host": "192.0.2.10",
                }
            )
            with patch(
                "app.ssh_runner._password_ssh_client",
                return_value=object(),
            ), patch(
                "app.ssh_runner._run_password_command",
                return_value=(0, response, ""),
            ) as password_command:
                payload = _run_script(
                    settings,
                    "192.0.2.10",
                    22,
                    "ops",
                    "",
                    REMOTE_INFO_SCRIPT,
                    5,
                    password="secret",
                )
            self.assertEqual(payload["service"], "spark-ssh-probe")
            self.assertEqual(
                password_command.call_args.args[1],
                "/usr/bin/python3 -u -",
            )

    def test_remote_script_timeouts_are_rendered(self) -> None:
        settings = SimpleNamespace(
            network_interface_exclude_prefixes=("lo",),
            gpu_query_command="",
            gpu_power_estimate_enabled=False,
            gpu_power_estimate_names=(),
            gpu_power_estimate_idle_watts=0,
            gpu_power_estimate_rated_watts=0,
            network_route_probe_host="",
            ssh_interface_query_timeout_seconds=1.25,
            ssh_gpu_info_timeout_seconds=1.5,
            ssh_gpu_metrics_timeout_seconds=2.5,
            ssh_cpu_sample_interval_seconds=0.75,
        )
        info_script = _render_remote_script(settings, REMOTE_INFO_SCRIPT)
        metrics_script = _render_remote_script(settings, REMOTE_METRICS_SCRIPT)
        compile(info_script, "<remote-info-timeouts>", "exec")
        compile(metrics_script, "<remote-metrics-timeouts>", "exec")
        self.assertIn("timeout=1.25", info_script)
        self.assertIn("timeout=1.5", info_script)
        self.assertIn("timeout=1.25", metrics_script)
        self.assertIn("timeout=2.5", metrics_script)
        self.assertIn("psutil.cpu_percent(interval=0.75)", metrics_script)

    def test_probe_uses_configured_ssh_timeout(self) -> None:
        settings = SimpleNamespace(
            ssh_port=2222,
            ssh_user="ops",
            ssh_key_path="",
            ssh_timeout=1.25,
        )
        with patch(
            "app.ssh_runner._run_script",
            return_value={"service": "spark-ssh-probe"},
        ) as run_script:
            payload = probe_via_ssh("192.0.2.50", settings)

        self.assertEqual(payload["service"], "spark-ssh-probe")
        called = run_script.call_args.args
        self.assertIs(called[0], settings)
        self.assertEqual(called[2], 2222)
        self.assertEqual(called[6], 1.25)


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

    def test_container_runtime_supports_arguments(self) -> None:
        config = SimpleNamespace(model_container_runtime="sudo -n docker")
        self.assertEqual(
            _container_command(config, "ps", "--format", "{{.Names}}"),
            ["sudo", "-n", "docker", "ps", "--format", "{{.Names}}"],
        )
        config.model_container_runtime = "docker 'unterminated"
        with self.assertRaisesRegex(ValueError, "格式无效"):
            _container_command(config, "ps")

    def test_model_url_supports_http_and_https(self) -> None:
        config = SimpleNamespace(model_scheme="http")
        self.assertEqual(
            _model_base_url(config, "192.0.2.60", 8000),
            "http://192.0.2.60:8000",
        )
        config.model_scheme = "https"
        self.assertEqual(
            _model_base_url(config, "192.0.2.60", 8443),
            "https://192.0.2.60:8443",
        )

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

    def login(self) -> None:
        status, setup = self.request(
            "/api/setup",
            "POST",
            {"username": "admin", "password": "test-password-123"},
        )
        self.assertEqual(status, 201)
        self.csrf = setup["csrf_token"]

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

        status, discovery = self.request("/api/discovery")
        self.assertEqual(status, 200)
        self.assertIsInstance(discovery["interfaces"], list)
        self.assertIsInstance(discovery["scan"], dict)

        status, _ = self.request(
            "/api/discovery/scan",
            "POST",
            {"interface": 123},
        )
        self.assertEqual(status, 400)

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

    def test_password_auth_for_scan_bulk_add_and_edit(self) -> None:
        self.login()
        password = "api-secret-pass-77"

        with patch.object(
            self.application.discovery,
            "start_scan",
            return_value={"scanning": True},
        ) as start_scan:
            status, scan = self.request(
                "/api/discovery/scan",
                "POST",
                {
                    "interface": "lan-test",
                    "cidrs": ["198.51.100.0/28"],
                    "ssh_user": "ops",
                    "ssh_port": 2222,
                    "ssh_key_path": "",
                    "ssh_password": password,
                },
            )
        self.assertEqual(status, 200)
        self.assertEqual(scan, {"scan": {"scanning": True}})
        start_scan.assert_called_once_with(
            cidrs=["198.51.100.0/28"],
            interface="lan-test",
            ssh_user="ops",
            ssh_port=2222,
            ssh_key_path="",
            ssh_password=password,
        )

        status, result = self.request(
            "/api/nodes/bulk",
            "POST",
            {
                "input": "192.0.2.41,spark-password",
                "ssh_user": "ops",
                "ssh_port": 2222,
                "ssh_password": password,
            },
        )
        self.assertEqual(status, 201)
        node = result["nodes"][0]
        self.assertTrue(node["ssh_password_configured"])
        self.assertNotIn("ssh_password", node)
        self.assertNotIn(password, json.dumps(result))

        status, dashboard = self.request("/api/dashboard")
        self.assertEqual(status, 200)
        self.assertNotIn(password, json.dumps(dashboard))
        node_id = dashboard["nodes"][0]["id"]

        status, unchanged = self.request(
            f"/api/nodes/{node_id}",
            "PATCH",
            {"ssh_password": ""},
        )
        self.assertEqual(status, 200)
        self.assertTrue(unchanged["node"]["ssh_password_configured"])

        status, cleared = self.request(
            f"/api/nodes/{node_id}",
            "PATCH",
            {"ssh_password": "", "clear_ssh_password": True},
        )
        self.assertEqual(status, 200)
        self.assertFalse(cleared["node"]["ssh_password_configured"])

        status, audit = self.request("/api/audit")
        self.assertEqual(status, 200)
        self.assertNotIn(password, json.dumps(audit))
        for database_file in Path(self.temp.name).glob("spark-grid.db*"):
            self.assertNotIn(password.encode(), database_file.read_bytes())

    def test_runtime_settings_are_typed_configurable_and_safe(self) -> None:
        self.login()

        status, payload = self.request("/api/settings")
        self.assertEqual(status, 200)
        for key in ("settings", "fields", "groups", "process", "ssh_defaults"):
            self.assertIn(key, payload)
        self.assertTrue(payload["ssh_defaults"]["password_supported"])

        status, updated = self.request(
            "/api/settings",
            "PATCH",
            {
                "cpu_warning": 91,
                "discovery_auto_enabled": False,
                "ssh_timeout": 6.5,
                "max_tags_per_node": 3,
                "default_tags": ["edge", "managed"],
                "ui_history_ranges": [5, 15, 30],
                "ui_timezone": "UTC",
                "model_default_port": 18080,
                "model_scheme": "https",
                "ssh_remote_python": "/usr/bin/python3 -u",
                "ssh_bin": "/opt/openssh/bin/ssh -F /etc/cluster/ssh_config",
                "ssh_known_hosts_path": "/etc/spark-grid/known_hosts",
                "ssh_control_persist": 0,
                "ssh_interface_query_timeout_seconds": 1.25,
                "ssh_gpu_info_timeout_seconds": 1.5,
                "ssh_gpu_metrics_timeout_seconds": 2.5,
                "ssh_cpu_sample_interval_seconds": 0.75,
                "gpu_query_command": "/opt/gpu/bin/query --json",
                "gpu_power_estimate_enabled": True,
                "gpu_power_estimate_names": ["Vendor GPU"],
                "gpu_power_estimate_idle_watts": 12.5,
                "gpu_power_estimate_rated_watts": 250,
                "model_container_runtime": "sudo -n docker",
                "database_timeout_seconds": 2.75,
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(updated["settings"]["cpu_warning"], 91)
        self.assertFalse(updated["settings"]["discovery_auto_enabled"])
        self.assertEqual(updated["settings"]["ssh_timeout"], 6.5)
        self.assertEqual(updated["settings"]["ui_history_ranges"], [5, 15, 30])
        self.assertEqual(updated["settings"]["ui_timezone"], "UTC")
        self.assertEqual(updated["settings"]["model_default_port"], 18080)
        self.assertEqual(updated["settings"]["model_scheme"], "https")
        self.assertEqual(
            updated["settings"]["ssh_remote_python"],
            "/usr/bin/python3 -u",
        )
        self.assertEqual(
            updated["settings"]["ssh_bin"],
            "/opt/openssh/bin/ssh -F /etc/cluster/ssh_config",
        )
        self.assertEqual(
            updated["settings"]["ssh_known_hosts_path"],
            "/etc/spark-grid/known_hosts",
        )
        self.assertEqual(updated["settings"]["ssh_control_persist"], 0)
        self.assertEqual(
            updated["settings"]["ssh_interface_query_timeout_seconds"],
            1.25,
        )
        self.assertEqual(
            updated["settings"]["ssh_gpu_info_timeout_seconds"],
            1.5,
        )
        self.assertEqual(
            updated["settings"]["ssh_gpu_metrics_timeout_seconds"],
            2.5,
        )
        self.assertEqual(
            updated["settings"]["ssh_cpu_sample_interval_seconds"],
            0.75,
        )
        self.assertEqual(
            updated["settings"]["gpu_query_command"],
            "/opt/gpu/bin/query --json",
        )
        self.assertTrue(updated["settings"]["gpu_power_estimate_enabled"])
        self.assertEqual(
            updated["settings"]["gpu_power_estimate_names"],
            ["Vendor GPU"],
        )
        self.assertEqual(
            updated["settings"]["model_container_runtime"],
            "sudo -n docker",
        )
        self.assertEqual(updated["settings"]["database_timeout_seconds"], 2.75)
        self.assertEqual(self.application.db.timeout_seconds, 2.75)

        status, history = self.request("/api/history/cluster?minutes=60")
        self.assertEqual(status, 200)
        self.assertEqual(history["minutes"], 30)

        invalid_payloads = (
            {"unknown_setting": True},
            {"cpu_warning": 0},
            {"ui_history_ranges": []},
            {"ui_timezone": "Invalid/Timezone"},
            {"model_scheme": "ftp"},
            {"ssh_bin": "ssh 'unterminated"},
            {"gpu_query_command": "nvidia-smi 'unterminated"},
            {"ssh_remote_python": "python3 'unterminated"},
            {"model_container_runtime": "docker 'unterminated"},
        )
        for invalid in invalid_payloads:
            status, error = self.request("/api/settings", "PATCH", invalid)
            self.assertEqual(status, 400)
            self.assertIn("error", error)

        status, disabled = self.request(
            "/api/settings",
            "PATCH",
            {"gpu_query_command": ""},
        )
        self.assertEqual(status, 200)
        self.assertEqual(disabled["settings"]["gpu_query_command"], "")

        status, audit = self.request("/api/audit")
        self.assertEqual(status, 200)
        serialized = json.dumps(updated, ensure_ascii=False) + json.dumps(
            audit, ensure_ascii=False
        )
        self.assertNotIn("ssh_password", serialized)


if __name__ == "__main__":
    unittest.main()
