from __future__ import annotations

import json
import os
import shlex
import subprocess
import time
from pathlib import Path
from typing import Any

from .config import Settings


REMOTE_INFO_SCRIPT = r"""
import hashlib
import json
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
        output = subprocess.check_output(["ip", "-brief", "-4", "address"], text=True, timeout=3)
        for line in output.splitlines():
            fields = line.split()
            if len(fields) >= 3:
                grouped.setdefault(fields[0], []).append(fields[2].split("/")[0])
    except (OSError, subprocess.SubprocessError):
        pass
    return grouped

groups = addresses()
flat = [
    ip for name, values in groups.items() for ip in values
    if not name.startswith(("lo", "docker", "veth", "br-", "virbr"))
]
management = next((ip for ip in flat if ip.startswith("192.168.")), None)
if not management:
    management = next((ip for ip in flat if not ip.startswith("10.20.")), flat[0] if flat else "127.0.0.1")
try:
    gpu_output = subprocess.check_output(
        ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
        text=True,
        timeout=3,
    )
    gpu_names = [line.strip() for line in gpu_output.splitlines() if line.strip()]
except (OSError, subprocess.SubprocessError):
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
        output = subprocess.check_output(["ip", "-brief", "-4", "address"], text=True, timeout=3)
        for line in output.splitlines():
            fields = line.split()
            if len(fields) >= 3:
                grouped.setdefault(fields[0], []).append(fields[2].split("/")[0])
    except (OSError, subprocess.SubprocessError):
        pass
    return grouped

def physical_names(groups):
    return [
        name for name in groups
        if not name.startswith(("lo", "docker", "veth", "br-", "virbr"))
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

def rdma_entries(groups):
    entries = []
    for device_path in glob.glob("/sys/class/infiniband/*"):
        device = os.path.basename(device_path)
        counter_path = f"{device_path}/ports/1/counters"
        state_path = f"{device_path}/ports/1/state"
        try:
            state = open(state_path, encoding="ascii").read().strip()
            if not state.startswith("4"):
                continue
            rx_units = int(open(f"{counter_path}/port_rcv_data", encoding="ascii").read())
            tx_units = int(open(f"{counter_path}/port_xmit_data", encoding="ascii").read())
        except (OSError, ValueError):
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
            "speed_mbps": 200000,
            "rx_bytes": rx_units * 4,
            "tx_bytes": tx_units * 4,
        })
    return entries

def gpu_metrics():
    try:
        output = subprocess.check_output([
            "nvidia-smi",
            "--query-gpu=index,name,utilization.gpu,memory.used,memory.total,temperature.gpu,power.draw,power.limit,pstate,clocks.current.graphics,clocks.current.sm,clocks.max.graphics",
            "--format=csv,noheader,nounits",
        ], text=True, timeout=4, stderr=subprocess.DEVNULL)
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
        if "GB10" in name and power is not None:
            # GB10 can report 96% utilization while idle at 17 W. Power is a
            # more reliable load signal on this unified-memory platform.
            idle_power = 4.0
            rated_power = 140.0
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
cpu = psutil.cpu_percent(interval=0.5) if psutil else 0.0
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
flat = [
    ip for name, values in groups.items() for ip in values
    if not name.startswith(("lo", "docker", "veth", "br-", "virbr"))
]
management = next((ip for ip in flat if ip.startswith("192.168.")), None)
if not management:
    management = next((ip for ip in flat if not ip.startswith("10.20.")), flat[0] if flat else "127.0.0.1")
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


def _ssh_base(
    settings: Settings,
    host: str,
    port: int,
    user: str,
    key_path: str,
    connect_timeout: int = 4,
) -> list[str]:
    control_dir = settings.data_dir / "ssh"
    control_dir.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(control_dir, 0o700)
    except OSError:
        pass
    command = [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        f"ConnectTimeout={max(1, connect_timeout)}",
        "-o",
        "StrictHostKeyChecking=accept-new",
        "-o",
        "LogLevel=ERROR",
        "-o",
        "ControlMaster=auto",
        "-o",
        f"ControlPath={control_dir / '%C'}",
        "-o",
        "ControlPersist=90",
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
) -> dict[str, Any]:
    command = _ssh_base(settings, host, port, user, key_path) + ["python3", "-"]
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


def collect_via_ssh(node: dict[str, Any], settings: Settings) -> tuple[dict[str, Any], float]:
    started = time.perf_counter()
    payload = _run_script(
        settings,
        str(node["host"]),
        int(node.get("ssh_port") or settings.ssh_port),
        str(node.get("ssh_user") or settings.ssh_user),
        str(node.get("ssh_key_path") or settings.ssh_key_path),
        REMOTE_METRICS_SCRIPT,
        settings.ssh_timeout,
    )
    return payload, (time.perf_counter() - started) * 1000


def probe_via_ssh(
    host: str,
    settings: Settings,
    port: int | None = None,
    user: str | None = None,
    key_path: str | None = None,
) -> dict[str, Any]:
    return _run_script(
        settings,
        host,
        int(port or settings.ssh_port),
        str(user or settings.ssh_user),
        str(key_path if key_path is not None else settings.ssh_key_path),
        REMOTE_INFO_SCRIPT,
        max(4.0, settings.ssh_timeout),
    )


def run_remote_command(
    node: dict[str, Any],
    settings: Settings,
    remote_args: list[str],
    timeout: float = 8,
) -> str:
    remote_command = " ".join(shlex.quote(str(arg)) for arg in remote_args)
    command = _ssh_base(
        settings,
        str(node["host"]),
        int(node.get("ssh_port") or settings.ssh_port),
        str(node.get("ssh_user") or settings.ssh_user),
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
