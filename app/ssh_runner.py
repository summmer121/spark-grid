from __future__ import annotations

import json
import os
import shlex
import socket
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from .command_line import parse_command
from .config import (
    NETWORK_INTERFACE_EXCLUDE_PREFIXES,
    SSH_HOST_KEY_POLICIES,
    Settings,
)


REMOTE_INFO_SCRIPT = r"""
import hashlib
import json
import os
import platform
import socket
import subprocess

def machine_id():
    for path in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
        try:
            value = open(path, encoding="ascii").read().strip()
            if value:
                return hashlib.sha256(value.encode()).hexdigest()[:16]
        except OSError:
            pass
    return hashlib.sha256(socket.gethostname().encode()).hexdigest()[:16]

def addresses():
    grouped = {}
    try:
        output = subprocess.check_output(
            ["ip", "-brief", "-4", "address"],
            text=True,
            timeout=__SPARK_INTERFACE_QUERY_TIMEOUT__,
        )
        for line in output.splitlines():
            fields = line.split()
            if len(fields) >= 3:
                grouped.setdefault(fields[0], []).append(fields[2].split("/")[0])
    except (OSError, subprocess.SubprocessError):
        pass
    return grouped

def preferred_address(groups):
    candidates = [
        ip for name, values in groups.items() for ip in values
        if not name.startswith(__SPARK_INTERFACE_EXCLUDE_PREFIXES__)
    ]
    probe_host = __SPARK_NETWORK_ROUTE_PROBE_HOST__
    if not probe_host:
        ssh_client = os.getenv("SSH_CLIENT", "").split()
        ssh_connection = os.getenv("SSH_CONNECTION", "").split()
        probe_host = (
            ssh_client[0]
            if ssh_client
            else (ssh_connection[0] if ssh_connection else "")
        )
    if not probe_host:
        return candidates[0] if candidates else "127.0.0.1"
    try:
        route_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            route_socket.connect((probe_host, 80))
            route_address = route_socket.getsockname()[0]
        finally:
            route_socket.close()
        if route_address in candidates:
            return route_address
    except OSError:
        pass
    return candidates[0] if candidates else "127.0.0.1"

groups = addresses()
management = preferred_address(groups)
gpu_command = __SPARK_GPU_QUERY_COMMAND__
if gpu_command:
    try:
        gpu_output = subprocess.check_output(
            [*gpu_command, "--query-gpu=name", "--format=csv,noheader"],
            text=True,
            timeout=__SPARK_GPU_INFO_TIMEOUT__,
        )
        gpu_names = [line.strip() for line in gpu_output.splitlines() if line.strip()]
    except (OSError, subprocess.SubprocessError):
        gpu_names = []
else:
    gpu_names = []
try:
    os_name = next(
        (
            line.split("=", 1)[1].strip().strip('"')
            for line in open("/etc/os-release", encoding="utf-8")
            if line.startswith("PRETTY_NAME=")
        ),
        platform.platform(),
    )
except OSError:
    os_name = platform.platform()
print(json.dumps({
    "service": "spark-ssh-probe",
    "version": "0.1.0",
    "machine_id": machine_id(),
    "hostname": socket.gethostname(),
    "host": management,
    "addresses": groups,
    "gpus": gpu_names,
    "os": os_name,
    "arch": platform.machine(),
    "timestamp": int(__import__("time").time()),
}, ensure_ascii=False, separators=(",", ":")))
"""


REMOTE_METRICS_SCRIPT = r"""
import glob
import hashlib
import json
import os
import platform
import re
import shutil
import socket
import subprocess
import time

try:
    import psutil
except ImportError:
    psutil = None

def number(value):
    value = value.strip()
    if value in ("", "N/A", "[N/A]", "Not Supported", "[Not Supported]"):
        return None
    try:
        return float(value)
    except ValueError:
        return None

def rounded(value, digits=1):
    return None if value is None else round(float(value), digits)

def addresses():
    grouped = {}
    try:
        output = subprocess.check_output(
            ["ip", "-brief", "-4", "address"],
            text=True,
            timeout=__SPARK_INTERFACE_QUERY_TIMEOUT__,
        )
        for line in output.splitlines():
            fields = line.split()
            if len(fields) >= 3:
                grouped.setdefault(fields[0], []).append(fields[2].split("/")[0])
    except (OSError, subprocess.SubprocessError):
        pass
    return grouped

def preferred_address(groups):
    candidates = [
        ip for name, values in groups.items() for ip in values
        if not name.startswith(__SPARK_INTERFACE_EXCLUDE_PREFIXES__)
    ]
    probe_host = __SPARK_NETWORK_ROUTE_PROBE_HOST__
    if not probe_host:
        ssh_client = os.getenv("SSH_CLIENT", "").split()
        ssh_connection = os.getenv("SSH_CONNECTION", "").split()
        probe_host = (
            ssh_client[0]
            if ssh_client
            else (ssh_connection[0] if ssh_connection else "")
        )
    if not probe_host:
        return candidates[0] if candidates else "127.0.0.1"
    try:
        route_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            route_socket.connect((probe_host, 80))
            route_address = route_socket.getsockname()[0]
        finally:
            route_socket.close()
        if route_address in candidates:
            return route_address
    except OSError:
        pass
    return candidates[0] if candidates else "127.0.0.1"

def physical_names(groups):
    return [
        name for name in groups
        if not name.startswith(__SPARK_INTERFACE_EXCLUDE_PREFIXES__)
    ]

def network_counters(groups):
    if not psutil:
        values = {}
        try:
            for line in open("/proc/net/dev", encoding="ascii").read().splitlines()[2:]:
                name, data = line.split(":", 1)
                fields = data.split()
                if name.strip() in physical_names(groups):
                    values[name.strip()] = {
                        "rx_bytes": int(fields[0]),
                        "tx_bytes": int(fields[8]),
                    }
        except (OSError, ValueError):
            pass
        return values
    counters = psutil.net_io_counters(pernic=True)
    names = physical_names(groups)
    return {
        name: {
            "rx_bytes": counters[name].bytes_recv,
            "tx_bytes": counters[name].bytes_sent,
        }
        for name in names
        if name in counters
    }

def rdma_rate_mbps(port_path):
    try:
        rate = open(f"{port_path}/rate", encoding="ascii").read().strip()
    except OSError:
        return None
    match = re.search(r"([0-9.]+)\s*([GMK])b(?:it)?/(?:s|sec)", rate, re.I)
    if not match:
        return None
    multiplier = {"K": 0.001, "M": 1, "G": 1000}[match.group(2).upper()]
    return round(float(match.group(1)) * multiplier)

def rdma_entries(groups):
    entries = []
    for device_path in glob.glob("/sys/class/infiniband/*"):
        device = os.path.basename(device_path)
        rx_units = 0
        tx_units = 0
        speeds = []
        active_ports = 0
        for port_path in glob.glob(f"{device_path}/ports/*"):
            try:
                state = open(f"{port_path}/state", encoding="ascii").read().strip()
                if not state.startswith("4"):
                    continue
                rx_units += int(
                    open(f"{port_path}/counters/port_rcv_data", encoding="ascii").read()
                )
                tx_units += int(
                    open(f"{port_path}/counters/port_xmit_data", encoding="ascii").read()
                )
            except (OSError, ValueError):
                continue
            active_ports += 1
            speed = rdma_rate_mbps(port_path)
            if speed is not None:
                speeds.append(speed)
        if not active_ports:
            continue
        addresses = []
        try:
            for net_name in os.listdir(f"{device_path}/device/net"):
                addresses.extend(groups.get(net_name, []))
        except OSError:
            pass
        entries.append({
            "name": device,
            "kind": "rdma",
            "address": addresses[0] if addresses else "",
            "speed_mbps": max(speeds) if speeds else None,
            "active_ports": active_ports,
            "rx_bytes": rx_units * 4,
            "tx_bytes": tx_units * 4,
        })
    return entries

def gpu_metrics():
    gpu_command = __SPARK_GPU_QUERY_COMMAND__
    if not gpu_command:
        return []
    try:
        output = subprocess.check_output([
            *gpu_command,
            "--query-gpu=index,name,utilization.gpu,memory.used,memory.total,temperature.gpu,power.draw,power.limit,pstate,clocks.current.graphics,clocks.current.sm,clocks.max.graphics",
            "--format=csv,noheader,nounits",
        ], text=True, timeout=__SPARK_GPU_METRICS_TIMEOUT__, stderr=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return []
    result = []
    for line in output.splitlines():
        fields = [item.strip() for item in line.split(",")]
        if len(fields) < 12:
            continue
        name = fields[1]
        raw_utilization = number(fields[2])
        power = number(fields[6])
        power_limit = number(fields[7])
        pstate = fields[8]
        utilization = raw_utilization
        load_percent = raw_utilization
        load_source = "driver utilization"
        matched_power_estimate_name = any(
            hint.lower() in name.lower()
            for hint in __SPARK_GPU_POWER_ESTIMATE_NAMES__
        )
        rated_power = (
            __SPARK_GPU_POWER_ESTIMATE_RATED_WATTS__
            if __SPARK_GPU_POWER_ESTIMATE_RATED_WATTS__ > 0
            else power_limit
        )
        if (
            __SPARK_GPU_POWER_ESTIMATE_ENABLED__
            and matched_power_estimate_name
            and power is not None
            and rated_power is not None
            and rated_power > __SPARK_GPU_POWER_ESTIMATE_IDLE_WATTS__
        ):
            idle_power = __SPARK_GPU_POWER_ESTIMATE_IDLE_WATTS__
            load_percent = round(
                max(0.0, min(100.0, (power - idle_power) / (rated_power - idle_power) * 100)),
                1,
            )
            load_source = "power estimate"
            utilization = None
        result.append({
            "index": int(fields[0]) if fields[0].isdigit() else len(result),
            "name": name,
            "utilization": utilization,
            "load_percent": load_percent,
            "load_source": load_source,
            "memory_used": number(fields[3]),
            "memory_total": number(fields[4]),
            "temperature": number(fields[5]),
            "power": power,
            "power_limit": power_limit,
            "pstate": pstate,
            "clock_graphics": number(fields[9]),
            "clock_sm": number(fields[10]),
            "clock_max_graphics": number(fields[11]),
            "memory_type": "unified",
        })
    return result

def temperatures():
    values = []
    if psutil and hasattr(psutil, "sensors_temperatures"):
        try:
            for name, entries in (psutil.sensors_temperatures() or {}).items():
                for entry in entries:
                    if entry.current is not None:
                        values.append({
                            "name": name,
                            "label": entry.label or name,
                            "current": rounded(entry.current),
                        })
        except (OSError, RuntimeError):
            pass
    maximum = max((item["current"] for item in values), default=None)
    return maximum, values[:12]

def machine_id():
    for path in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
        try:
            value = open(path, encoding="ascii").read().strip()
            if value:
                return hashlib.sha256(value.encode()).hexdigest()[:16]
        except OSError:
            pass
    return hashlib.sha256(socket.gethostname().encode()).hexdigest()[:16]

groups = addresses()
counters = network_counters(groups)
rdma = rdma_entries(groups)
rx = sum(item["rx_bytes"] for item in counters.values()) + sum(
    item["rx_bytes"] for item in rdma
)
tx = sum(item["tx_bytes"] for item in counters.values()) + sum(
    item["tx_bytes"] for item in rdma
)
cpu = psutil.cpu_percent(interval=__SPARK_CPU_SAMPLE_INTERVAL__) if psutil else 0.0
load = os.getloadavg() if hasattr(os, "getloadavg") else (0.0, 0.0, 0.0)
memory_percent = memory_used = memory_total = swap_percent = 0.0
memory_available = memory_cached = memory_buffers = 0.0
process_count = None
uptime = None
if psutil:
    memory = psutil.virtual_memory()
    swap = psutil.swap_memory()
    memory_percent, memory_used, memory_total = memory.percent, memory.used, memory.total
    memory_available = memory.available
    memory_cached = getattr(memory, "cached", 0)
    memory_buffers = getattr(memory, "buffers", 0)
    swap_percent = swap.percent
    process_count = len(psutil.pids())
    uptime = int(time.time() - psutil.boot_time())
else:
    try:
        info = {}
        for line in open("/proc/meminfo", encoding="ascii"):
            key, raw = line.split(":", 1)
            info[key] = int(raw.strip().split()[0]) * 1024
        memory_total = float(info.get("MemTotal", 0))
        available = float(info.get("MemAvailable", 0))
        memory_available = available
        memory_cached = float(info.get("Cached", 0))
        memory_buffers = float(info.get("Buffers", 0))
        memory_used = max(0.0, memory_total - available)
        memory_percent = memory_used / memory_total * 100 if memory_total else 0
    except (OSError, ValueError):
        pass
disk = shutil.disk_usage("/")
system_temperature_max, sensors = temperatures()
gpus = gpu_metrics()
gpu_temps = [
    gpu["temperature"] for gpu in gpus
    if isinstance(gpu.get("temperature"), (int, float))
]
gpu_temperature = max(gpu_temps) if gpu_temps else None
temperature_max = gpu_temperature if gpu_temperature is not None else system_temperature_max
disks = []
if psutil:
    try:
        for partition in psutil.disk_partitions(all=False):
            usage = psutil.disk_usage(partition.mountpoint)
            disks.append({
                "mount": partition.mountpoint,
                "device": partition.device,
                "percent": rounded(usage.percent),
                "used": usage.used,
                "total": usage.total,
            })
    except (OSError, PermissionError):
        pass
management = preferred_address(groups)
network = []
stats = psutil.net_if_stats() if psutil else {}
for name, values in groups.items():
    for address in values:
        counter = counters.get(name, {})
        network.append({
            "name": name,
            "kind": "netdev",
            "address": address,
            "speed_mbps": stats[name].speed if name in stats else None,
            "rx_bytes": counter.get("rx_bytes"),
            "tx_bytes": counter.get("tx_bytes"),
        })
network.extend(rdma)
resources = {
    "cpu_percent": rounded(cpu),
    "cpu_count": os.cpu_count(),
    "cpu_count_physical": psutil.cpu_count(logical=False) if psutil else None,
    "load_1": rounded(load[0], 2),
    "load_5": rounded(load[1], 2),
    "load_15": rounded(load[2], 2),
    "memory_percent": rounded(memory_percent),
    "memory_used": memory_used,
    "memory_total": memory_total,
    "memory_available": memory_available,
    "memory_cached": memory_cached,
    "memory_buffers": memory_buffers,
    "swap_percent": rounded(swap_percent),
    "process_count": process_count,
    "uptime": uptime,
    "temperature_max": rounded(temperature_max),
    "gpu_temperature": rounded(gpu_temperature),
    "system_temperature_max": rounded(system_temperature_max),
    "temperature_sensors": sensors,
    "rx_bytes": rx,
    "tx_bytes": tx,
    "rx_rate": 0,
    "tx_rate": 0,
    "disk_percent": rounded(disk.used / disk.total * 100 if disk.total else 0),
    "disk_used": disk.used,
    "disk_total": disk.total,
}
compact = {
    "cpu": resources["cpu_percent"],
    "memory": resources["memory_percent"],
    "gpu": max([
        float(gpu.get("load_percent", gpu.get("utilization"))) for gpu in gpus
        if isinstance(gpu.get("load_percent", gpu.get("utilization")), (int, float))
    ] or [0]),
    "temperature": resources["temperature_max"],
    "rx_rate": 0,
    "tx_rate": 0,
}
print(json.dumps({
    "service": "spark-ssh-probe",
    "metrics_schema": 5,
    "version": "0.1.0",
    "timestamp": round(time.time(), 3),
    "machine_id": machine_id(),
    "hostname": socket.gethostname(),
    "host": management,
    "os": platform.platform(),
    "arch": platform.machine(),
    "resources": resources,
    "gpus": gpus,
    "network": network,
    "disks": disks,
    "compact": compact,
}, ensure_ascii=False, separators=(",", ":")))
"""


class SSHError(RuntimeError):
    pass


_KNOWN_HOSTS_LOCK = threading.Lock()


def _known_hosts_path(settings: Settings) -> Path:
    configured = str(
        getattr(settings, "ssh_known_hosts_path", "") or ""
    ).strip()
    if configured:
        path = Path(configured).expanduser()
        if not path.is_absolute():
            base_dir = (
                getattr(settings, "root_dir", None)
                or getattr(settings, "data_dir", Path.cwd())
            )
            path = Path(base_dir) / path
    else:
        path = settings.data_dir / "ssh" / "known_hosts"
    ssh_dir = path.parent
    ssh_dir.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(ssh_dir, 0o700)
    except OSError:
        pass
    return path


def _ssh_host_key_policy(settings: Settings) -> str:
    value = str(
        getattr(settings, "ssh_strict_host_key_checking", "accept-new")
        or "accept-new"
    ).strip().lower()
    return value if value in SSH_HOST_KEY_POLICIES else "accept-new"


def _render_remote_script(settings: Settings, script: str) -> str:
    prefixes = getattr(
        settings,
        "network_interface_exclude_prefixes",
        NETWORK_INTERFACE_EXCLUDE_PREFIXES,
    )
    normalized = tuple(
        str(item).strip()
        for item in (prefixes or ())
        if str(item).strip()
    )
    estimate_names = tuple(
        str(item).strip()
        for item in (
            getattr(settings, "gpu_power_estimate_names", ())
            or ()
        )
        if str(item).strip()
    )
    estimate_enabled = bool(
        getattr(settings, "gpu_power_estimate_enabled", False)
    )
    try:
        gpu_query = parse_command(
            getattr(settings, "gpu_query_command", "nvidia-smi"),
            label="GPU 查询命令",
            allow_empty=True,
            max_length=500,
        )
    except ValueError as exc:
        raise SSHError(str(exc)) from exc
    try:
        idle_watts = max(
            0.0,
            float(
                getattr(settings, "gpu_power_estimate_idle_watts", 0.0)
            ),
        )
    except (TypeError, ValueError):
        idle_watts = 0.0
    try:
        rated_watts = max(
            0.0,
            float(
                getattr(settings, "gpu_power_estimate_rated_watts", 0.0)
            ),
        )
    except (TypeError, ValueError):
        rated_watts = 0.0

    def setting_float(
        name: str,
        default: float,
        minimum: float,
        maximum: float,
    ) -> float:
        try:
            value = float(getattr(settings, name, default))
        except (TypeError, ValueError):
            value = default
        return max(minimum, min(maximum, value))

    interface_query_timeout = setting_float(
        "ssh_interface_query_timeout_seconds", 3.0, 0.1, 60.0
    )
    gpu_info_timeout = setting_float(
        "ssh_gpu_info_timeout_seconds", 3.0, 0.1, 60.0
    )
    gpu_metrics_timeout = setting_float(
        "ssh_gpu_metrics_timeout_seconds", 4.0, 0.1, 120.0
    )
    cpu_sample_interval = setting_float(
        "ssh_cpu_sample_interval_seconds", 0.5, 0.1, 10.0
    )
    replacements = {
        "__SPARK_INTERFACE_EXCLUDE_PREFIXES__": repr(normalized),
        "__SPARK_NETWORK_ROUTE_PROBE_HOST__": repr(
            str(
                getattr(settings, "network_route_probe_host", "") or ""
            ).strip()
        ),
        "__SPARK_GPU_QUERY_COMMAND__": repr(gpu_query),
        "__SPARK_GPU_POWER_ESTIMATE_ENABLED__": repr(estimate_enabled),
        "__SPARK_GPU_POWER_ESTIMATE_NAMES__": repr(estimate_names),
        "__SPARK_GPU_POWER_ESTIMATE_IDLE_WATTS__": repr(idle_watts),
        "__SPARK_GPU_POWER_ESTIMATE_RATED_WATTS__": repr(rated_watts),
        "__SPARK_INTERFACE_QUERY_TIMEOUT__": repr(interface_query_timeout),
        "__SPARK_GPU_INFO_TIMEOUT__": repr(gpu_info_timeout),
        "__SPARK_GPU_METRICS_TIMEOUT__": repr(gpu_metrics_timeout),
        "__SPARK_CPU_SAMPLE_INTERVAL__": repr(cpu_sample_interval),
    }
    for marker, value in replacements.items():
        script = script.replace(marker, value)
    return script


def _host_key_name(host: str, port: int) -> str:
    return host if int(port) == 22 else f"[{host}]:{int(port)}"


def _persistent_host_key_policy(settings: Settings, host: str, port: int):
    try:
        import paramiko
    except ImportError as exc:
        raise SSHError("SSH 密码认证组件未安装，请在中心节点安装 requirements.txt") from exc

    class PersistentPolicy(paramiko.MissingHostKeyPolicy):
        def missing_host_key(
            self,
            client: Any,
            hostname: str,
            key: Any,
        ) -> None:
            path = _known_hosts_path(settings)
            key_name = _host_key_name(host, port)
            with _KNOWN_HOSTS_LOCK:
                known_hosts = paramiko.HostKeys()
                if path.is_file():
                    known_hosts.load(str(path))
                existing = known_hosts.lookup(key_name) or {}
                saved_key = existing.get(key.get_name())
                if saved_key is not None:
                    if saved_key == key:
                        return
                    raise paramiko.BadHostKeyException(
                        hostname, key, saved_key
                    )
                known_hosts.add(key_name, key.get_name(), key)
                ssh_dir = path.parent
                ssh_dir.mkdir(parents=True, exist_ok=True)
                descriptor, temporary_path = tempfile.mkstemp(
                    prefix=".known_hosts.",
                    dir=ssh_dir,
                    text=True,
                )
                os.close(descriptor)
                try:
                    known_hosts.save(temporary_path)
                    try:
                        os.chmod(temporary_path, 0o600)
                    except OSError:
                        pass
                    os.replace(temporary_path, path)
                finally:
                    try:
                        os.unlink(temporary_path)
                    except FileNotFoundError:
                        pass

    return PersistentPolicy()


def _password_ssh_client(
    settings: Settings,
    host: str,
    port: int,
    user: str,
    password: str,
    connect_timeout: float | None = None,
):
    if connect_timeout is None:
        connect_timeout = float(
            getattr(settings, "ssh_connect_timeout", 8)
        )
    try:
        import paramiko
    except ImportError as exc:
        raise SSHError("SSH 密码认证组件未安装，请在中心节点安装 requirements.txt") from exc

    client = paramiko.SSHClient()
    host_key_policy = _ssh_host_key_policy(settings)
    if host_key_policy == "yes":
        known_hosts_path = _known_hosts_path(settings)
        if known_hosts_path.is_file():
            client.load_host_keys(str(known_hosts_path))
        client.load_system_host_keys()
        client.set_missing_host_key_policy(paramiko.RejectPolicy())
    elif host_key_policy in {"no", "off"}:
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    else:
        client.set_missing_host_key_policy(
            _persistent_host_key_policy(settings, host, port)
        )
    try:
        client.connect(
            hostname=host,
            port=int(port),
            username=user,
            password=password,
            timeout=max(1.0, float(connect_timeout)),
            banner_timeout=max(2.0, float(connect_timeout)),
            auth_timeout=max(2.0, float(connect_timeout)),
            look_for_keys=False,
            allow_agent=False,
        )
    except paramiko.BadHostKeyException as exc:
        client.close()
        raise SSHError("SSH 主机密钥已变化，连接已拒绝") from exc
    except paramiko.AuthenticationException as exc:
        client.close()
        raise SSHError("SSH 认证失败，请检查用户名和密码") from exc
    except (paramiko.SSHException, OSError, socket.timeout) as exc:
        client.close()
        detail = str(exc).strip()
        if isinstance(exc, socket.timeout) or "timed out" in detail.lower():
            raise SSHError("SSH 连接超时") from exc
        raise SSHError(f"SSH 连接失败: {detail[:300] or '无法建立连接'}") from exc
    return client


def _run_password_command(
    client: Any,
    command: str,
    timeout: float,
    stdin_data: str | None = None,
) -> tuple[int, str, str]:
    channel = None
    try:
        transport = client.get_transport()
        if transport is None or not transport.is_active():
            raise SSHError("SSH 连接已断开")
        channel = transport.open_session(timeout=max(1.0, float(timeout)))
        channel.settimeout(max(1.0, float(timeout)))
        channel.exec_command(command)
        if stdin_data is not None:
            channel.sendall(stdin_data.encode("utf-8"))
        channel.shutdown_write()

        stdout_chunks: list[bytes] = []
        stderr_chunks: list[bytes] = []

        def read_stream(stream: Any, chunks: list[bytes]) -> None:
            try:
                while True:
                    value = stream.read(65536)
                    if not value:
                        return
                    chunks.append(value)
            except (OSError, EOFError):
                return

        stdout_thread = threading.Thread(
            target=read_stream,
            args=(channel.makefile("rb"), stdout_chunks),
            name="spark-ssh-stdout",
            daemon=True,
        )
        stderr_thread = threading.Thread(
            target=read_stream,
            args=(channel.makefile_stderr("rb"), stderr_chunks),
            name="spark-ssh-stderr",
            daemon=True,
        )
        stdout_thread.start()
        stderr_thread.start()
        deadline = time.monotonic() + max(1.0, float(timeout))
        while time.monotonic() < deadline:
            if (
                channel.exit_status_ready()
                and not stdout_thread.is_alive()
                and not stderr_thread.is_alive()
            ):
                break
            time.sleep(0.02)
        else:
            channel.close()
            raise SSHError("SSH 命令执行超时")
        stdout_thread.join(timeout=0.2)
        stderr_thread.join(timeout=0.2)
        exit_status = int(channel.recv_exit_status())
        stdout = b"".join(stdout_chunks).decode("utf-8", errors="replace")
        stderr = b"".join(stderr_chunks).decode("utf-8", errors="replace")
        return exit_status, stdout, stderr
    except SSHError:
        raise
    except (OSError, EOFError, socket.timeout) as exc:
        raise SSHError(f"SSH 命令执行失败: {str(exc)[:300]}") from exc
    except Exception as exc:
        try:
            import paramiko
        except ImportError:
            paramiko = None
        if paramiko is not None and isinstance(exc, paramiko.SSHException):
            raise SSHError(f"SSH 命令执行失败: {str(exc)[:300]}") from exc
        raise
    finally:
        if channel is not None:
            try:
                channel.close()
            except OSError:
                pass
        client.close()


def _ssh_base(
    settings: Settings,
    host: str,
    port: int,
    user: str,
    key_path: str,
    connect_timeout: float | int | None = None,
) -> list[str]:
    control_dir = settings.data_dir / "ssh"
    control_dir.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(control_dir, 0o700)
    except OSError:
        pass
    if connect_timeout is None:
        connect_timeout = float(
            getattr(settings, "ssh_connect_timeout", 8)
        )
    control_persist = max(
        0,
        int(getattr(settings, "ssh_control_persist", 90)),
    )
    try:
        ssh_command = parse_command(
            getattr(settings, "ssh_bin", "ssh"),
            label="SSH 命令",
            max_length=500,
        )
    except ValueError as exc:
        raise SSHError(str(exc)) from exc
    command = [
        *ssh_command,
        "-o",
        "BatchMode=yes",
        "-o",
        f"ConnectTimeout={max(1, int(round(float(connect_timeout))))}",
        "-o",
        f"StrictHostKeyChecking={_ssh_host_key_policy(settings)}",
        "-o",
        f"UserKnownHostsFile={_known_hosts_path(settings)}",
        "-o",
        "LogLevel=ERROR",
        "-o",
        "ControlMaster=auto",
        "-o",
        f"ControlPath={control_dir / '%C'}",
        "-o",
        f"ControlPersist={control_persist if control_persist > 0 else 'no'}",
        "-p",
        str(port),
    ]
    if key_path:
        command.extend(["-i", str(Path(key_path).expanduser()), "-o", "IdentitiesOnly=yes"])
    command.append(f"{user}@{host}")
    return command


def _run_script(
    settings: Settings,
    host: str,
    port: int,
    user: str,
    key_path: str,
    script: str,
    timeout: float,
    password: str | None = None,
) -> dict[str, Any]:
    script = _render_remote_script(settings, script)
    try:
        remote_python = parse_command(
            getattr(settings, "ssh_remote_python", "python3"),
            label="远端 Python 命令",
            max_length=500,
        )
    except ValueError as exc:
        raise SSHError(str(exc)) from exc
    if password:
        client = _password_ssh_client(
            settings,
            host,
            port,
            user,
            password,
            getattr(settings, "ssh_connect_timeout", 8),
        )
        returncode, stdout, stderr = _run_password_command(
            client,
            shlex.join([*remote_python, "-"]),
            timeout,
            stdin_data=script,
        )
        if returncode != 0:
            detail = (stderr or stdout or "SSH command failed").strip()
            raise SSHError(detail[-500:])
        for line in reversed(stdout.strip().splitlines()):
            try:
                payload = json.loads(line)
            except ValueError:
                continue
            if payload.get("service") != "spark-ssh-probe":
                raise SSHError("远端返回了非 Spark 数据")
            return payload
        raise SSHError("远端没有返回有效指标")

    command = _ssh_base(settings, host, port, user, key_path) + [
        *remote_python,
        "-",
    ]
    try:
        completed = subprocess.run(
            command,
            input=script,
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SSHError(f"SSH 连接失败: {exc}") from exc
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "SSH command failed").strip()
        raise SSHError(detail[-500:])
    for line in reversed(completed.stdout.strip().splitlines()):
        try:
            payload = json.loads(line)
        except ValueError:
            continue
        if payload.get("service") != "spark-ssh-probe":
            raise SSHError("远端返回了非 Spark 数据")
        return payload
    raise SSHError("远端没有返回有效指标")


def collect_via_ssh(
    node: dict[str, Any],
    settings: Settings,
    password: str | None = None,
) -> tuple[dict[str, Any], float]:
    started = time.perf_counter()
    selected_password = password
    if selected_password is None:
        selected_password = str(node.get("ssh_password") or "") or None
    payload = _run_script(
        settings,
        str(node["host"]),
        int(node.get("ssh_port") or settings.ssh_port),
        str(node.get("ssh_user") or settings.ssh_user),
        str(node.get("ssh_key_path") or settings.ssh_key_path),
        REMOTE_METRICS_SCRIPT,
        settings.ssh_timeout,
        password=selected_password,
    )
    return payload, (time.perf_counter() - started) * 1000


def probe_via_ssh(
    host: str,
    settings: Settings,
    port: int | None = None,
    user: str | None = None,
    key_path: str | None = None,
    password: str | None = None,
) -> dict[str, Any]:
    return _run_script(
        settings,
        host,
        int(port if port is not None else settings.ssh_port),
        str(user or settings.ssh_user),
        str(key_path if key_path is not None else settings.ssh_key_path),
        REMOTE_INFO_SCRIPT,
        settings.ssh_timeout,
        password=password,
    )


def run_remote_command(
    node: dict[str, Any],
    settings: Settings,
    remote_args: list[str],
    timeout: float = 8,
    password: str | None = None,
) -> str:
    remote_command = " ".join(shlex.quote(str(arg)) for arg in remote_args)
    selected_password = password
    if selected_password is None:
        selected_password = str(node.get("ssh_password") or "") or None
    host = str(node["host"])
    port = int(node.get("ssh_port") or settings.ssh_port)
    user = str(node.get("ssh_user") or settings.ssh_user)
    if selected_password:
        client = _password_ssh_client(
            settings,
            host,
            port,
            user,
            selected_password,
            getattr(settings, "ssh_connect_timeout", 8),
        )
        returncode, stdout, stderr = _run_password_command(
            client,
            remote_command,
            timeout,
        )
        output = "\n".join(
            part.strip()
            for part in (stdout, stderr)
            if part and part.strip()
        )
        if returncode != 0:
            raise SSHError(
                output[-1000:] or f"SSH command exited {returncode}"
            )
        return output

    command = _ssh_base(
        settings,
        host,
        port,
        user,
        str(node.get("ssh_key_path") or settings.ssh_key_path),
    ) + [remote_command]
    try:
        completed = subprocess.run(
            command,
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SSHError(f"SSH command failed: {exc}") from exc
    output = "\n".join(
        part.strip()
        for part in (completed.stdout, completed.stderr)
        if part and part.strip()
    )
    if completed.returncode != 0:
        raise SSHError(output[-1000:] or f"SSH command exited {completed.returncode}")
    return output
