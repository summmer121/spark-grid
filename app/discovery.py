from __future__ import annotations

import ipaddress
import re
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .db import Database, sanitize_error_message
from .ssh_runner import SSHError, probe_via_ssh


@dataclass(frozen=True)
class LocalIPv4Interface:
    name: str
    address: str
    netmask: str
    network: ipaddress.IPv4Network
    physical: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "address": self.address,
            "netmask": self.netmask,
            "cidr": str(self.network),
            "physical": self.physical,
        }


def _is_physical_interface(name: str) -> bool:
    sysfs_root = Path("/sys/class/net")
    if not sysfs_root.is_dir():
        return True
    return (sysfs_root / name / "device").exists()


def _local_ipv4_interfaces() -> list[LocalIPv4Interface]:
    interfaces: list[LocalIPv4Interface] = []
    try:
        import psutil  # type: ignore

        try:
            stats = psutil.net_if_stats()
        except (AttributeError, OSError):
            stats = {}
        for name, addresses in psutil.net_if_addrs().items():
            interface_stats = stats.get(name)
            if interface_stats is not None and not interface_stats.isup:
                continue
            for address in addresses:
                if address.family != socket.AF_INET or not address.netmask:
                    continue
                try:
                    ipv4_address = ipaddress.IPv4Address(address.address)
                    network = ipaddress.ip_network(
                        f"{address.address}/{address.netmask}", strict=False
                    )
                except ValueError:
                    continue
                if (
                    ipv4_address.is_loopback
                    or ipv4_address.is_link_local
                    or ipv4_address.is_unspecified
                    or ipv4_address.is_multicast
                    or network.is_loopback
                    or network.is_link_local
                ):
                    continue
                interfaces.append(
                    LocalIPv4Interface(
                        name=name,
                        address=str(ipv4_address),
                        netmask=address.netmask,
                        network=network,
                        physical=_is_physical_interface(name),
                    )
                )
    except ImportError:
        pass
    unique = {
        (interface.name, interface.address, str(interface.network)): interface
        for interface in interfaces
    }
    return sorted(
        unique.values(),
        key=lambda interface: (
            not interface.physical,
            interface.name,
            int(interface.network.network_address),
            int(ipaddress.IPv4Address(interface.address)),
        ),
    )


def _port_open(
    host: str, port: int, timeout: float, source_address: str | None = None
) -> bool:
    try:
        with socket.create_connection(
            (host, port),
            timeout=timeout,
            source_address=(source_address, 0) if source_address else None,
        ):
            return True
    except OSError:
        return False


class DiscoveryService:
    def __init__(self, db: Database, config: Any):
        self.db = db
        self.config = config
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._scan_state: dict[str, Any] = {
            "scanning": False,
            "scanned": 0,
            "total": 0,
            "found": 0,
            "authenticated": 0,
            "subnets": [],
            "interface": None,
            "interfaces": [],
            "ssh_user": None,
            "ssh_port": None,
            "ssh_key_path": "",
            "probe_failures": 0,
            "last_probe_error": None,
            "started_at": None,
            "finished_at": None,
            "error": None,
        }

    def _current(self) -> Any:
        current = getattr(self.config, "current", None)
        return current() if callable(current) else self.config

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._scan_loop, name="spark-ssh-discovery", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def notify_config_changed(self) -> None:
        self._wake.set()

    def _scan_loop(self) -> None:
        initial_delay = True
        while not self._stop.is_set():
            config = self._current()
            if initial_delay:
                delay = max(0.0, float(config.discovery_initial_delay_seconds))
                initial_delay = False
                self._wake.wait(delay)
                self._wake.clear()
                if self._stop.is_set():
                    return
                config = self._current()
            if config.discovery_auto_enabled:
                self._start_automatic_scan()
                wait_for = max(10.0, float(config.discovery_interval_seconds))
            else:
                wait_for = 60.0
            self._wake.wait(wait_for)
            self._wake.clear()

    def _start_automatic_scan(self) -> None:
        try:
            self.start_scan()
        except Exception as exc:
            with self._lock:
                self._scan_state.update(
                    {
                        "scanning": False,
                        "finished_at": int(time.time()),
                        "error": sanitize_error_message(exc),
                    }
                )

    def interfaces(self) -> list[dict[str, Any]]:
        return [interface.as_dict() for interface in _local_ipv4_interfaces()]

    def start_scan(
        self,
        cidrs: Iterable[str] | str | None = None,
        interface: str | None = None,
        ssh_user: str | None = None,
        ssh_port: int | str | None = None,
        ssh_key_path: str | None = None,
        ssh_password: str | None = None,
    ) -> dict[str, Any]:
        config = self._current()
        with self._lock:
            if self._scan_state["scanning"]:
                raise ValueError("局域网扫描正在进行，请等待当前扫描结束")
            default_user = str(config.ssh_user)
            default_port = int(config.ssh_port)
            default_key_path = str(config.ssh_key_path or "")
            selected_user = str(ssh_user or default_user).strip()
            if not selected_user or len(selected_user) > 64:
                raise ValueError("SSH 用户名格式无效")
            try:
                selected_port = int(
                    default_port if ssh_port in {None, ""} else ssh_port
                )
            except (TypeError, ValueError) as exc:
                raise ValueError("SSH 端口格式无效") from exc
            if not 1 <= selected_port <= 65535:
                raise ValueError("SSH 端口必须在 1 到 65535 之间")
            selected_key_path = str(
                default_key_path if ssh_key_path is None else ssh_key_path
            ).strip()[:500]
            selected_password = str(ssh_password or "")
            if len(selected_password) > 4096:
                raise ValueError("SSH 密码过长")
            interface_name = str(interface or "").strip()
            local_interfaces = _local_ipv4_interfaces()
            if interface_name:
                selected_interfaces = [
                    item for item in local_interfaces if item.name == interface_name
                ]
                if not selected_interfaces:
                    raise ValueError(
                        f"网卡 {interface_name!r} 不存在或没有可用的 IPv4 地址"
                    )
            else:
                physical_interfaces = [
                    item for item in local_interfaces if item.physical
                ]
                selected_interfaces = physical_interfaces or local_interfaces

            networks = self._parse_networks(cidrs, selected_interfaces)
            if networks and interface_name:
                outside = [
                    network
                    for network in networks
                    if not any(
                        network.subnet_of(item.network)
                        for item in selected_interfaces
                    )
                ]
                if outside:
                    values = ", ".join(str(network) for network in outside)
                    raise ValueError(
                        f"扫描范围 {values} 不属于网卡 {interface_name!r} 的 IPv4 网段"
                    )
            if not networks:
                networks = list(
                    {
                        str(item.network): item.network
                        for item in selected_interfaces
                    }.values()
                )
            if not networks:
                raise ValueError("未找到可供扫描的 IPv4 网卡或网段")

            targets = self._build_targets(
                networks,
                selected_interfaces if interface_name else local_interfaces,
                max_hosts=max(1, int(config.discovery_max_hosts)),
            )
            if not targets:
                raise ValueError("所选网卡或网段没有可扫描的主机地址")
            self._scan_state = {
                "scanning": True,
                "scanned": 0,
                "total": len(targets),
                "found": 0,
                "authenticated": 0,
                "subnets": [str(network) for network in networks],
                "interface": interface_name or None,
                "interfaces": sorted({item.name for item in selected_interfaces}),
                "ssh_user": selected_user,
                "ssh_port": selected_port,
                "ssh_key_path": selected_key_path,
                "probe_failures": 0,
                "last_probe_error": None,
                "started_at": int(time.time()),
                "finished_at": None,
                "error": None,
            }
        thread = threading.Thread(
            target=self._run_scan,
            args=(
                targets,
                selected_user,
                selected_port,
                selected_key_path,
                selected_password,
                {
                    "port_concurrency": max(
                        1, int(config.discovery_port_concurrency)
                    ),
                    "port_timeout_seconds": max(
                        0.05, float(config.discovery_port_timeout_seconds)
                    ),
                    "ssh_concurrency": max(
                        1, int(config.discovery_ssh_concurrency)
                    ),
                    "saved_lookback_seconds": max(
                        60, int(config.discovery_saved_lookback_seconds)
                    ),
                    "reuse_saved_ports": bool(config.discovery_reuse_saved_ports),
                    "config": config,
                },
            ),
            name="spark-ssh-scan",
            daemon=True,
        )
        thread.start()
        return self.state()

    def _run_scan(
        self,
        targets: list[tuple[str, str | None]],
        ssh_user: str,
        ssh_port: int,
        ssh_key_path: str,
        ssh_password: str,
        options: dict[str, Any],
    ) -> None:
        found_hosts: set[str] = set()
        authenticated_hosts: set[str] = set()
        scanned = 0
        probe_failures = 0
        try:
            saved_records: dict[str, dict[str, Any]] = {}
            if not ssh_password:
                try:
                    saved_records = {
                        str(item["host"]): item
                        for item in self.db.list_discovered(
                            int(options["saved_lookback_seconds"])
                        )
                    }
                except Exception:
                    saved_records = {}

            scan_targets: list[tuple[str, str | None, int]] = []
            remaining_ports: dict[str, int] = {}
            for host, source_address in targets:
                ports = {ssh_port}
                saved = saved_records.get(host)
                if saved and options["reuse_saved_ports"]:
                    try:
                        ports.add(int(saved.get("ssh_port") or ssh_port))
                    except (TypeError, ValueError):
                        pass
                remaining_ports[host] = len(ports)
                scan_targets.extend(
                    (host, source_address, port) for port in sorted(ports)
                )

            with ThreadPoolExecutor(
                max_workers=int(options["port_concurrency"])
            ) as executor:
                futures = {
                    executor.submit(
                        _port_open,
                        host,
                        port,
                        float(options["port_timeout_seconds"]),
                        source_address,
                    ): (host, port)
                    for host, source_address, port in scan_targets
                }
                ssh_hosts: dict[str, set[int]] = {}
                for future in as_completed(futures):
                    host, port = futures[future]
                    try:
                        is_open = future.result()
                    except Exception:
                        is_open = False
                    if is_open:
                        ssh_hosts.setdefault(host, set()).add(port)
                        found_hosts.add(host)
                    remaining_ports[host] -= 1
                    if remaining_ports[host] == 0:
                        scanned += 1
                    if scanned % 20 == 0:
                        with self._lock:
                            self._scan_state["scanned"] = scanned
                            self._scan_state["found"] = len(found_hosts)

            resolved_credentials: dict[str, tuple[str, int, str, str]] = {}
            for host, open_ports in ssh_hosts.items():
                effective_user = ssh_user
                effective_port = (
                    ssh_port if ssh_port in open_ports else min(open_ports)
                )
                effective_key_path = ssh_key_path
                effective_password = ssh_password
                if not ssh_password:
                    try:
                        saved = self.db.get_discovered_credentials(host) or {}
                    except Exception:
                        saved = {}
                    saved_password = str(saved.get("ssh_password") or "")
                    if saved_password:
                        effective_password = saved_password
                        effective_user = str(saved.get("ssh_user") or ssh_user)
                        effective_key_path = str(
                            saved.get("ssh_key_path")
                            if saved.get("ssh_key_path") is not None
                            else ssh_key_path
                        )
                        try:
                            saved_port = int(saved.get("ssh_port") or ssh_port)
                        except (TypeError, ValueError):
                            saved_port = ssh_port
                        effective_port = (
                            saved_port
                            if saved_port in open_ports
                            else effective_port
                        )
                resolved_credentials[host] = (
                    effective_user,
                    effective_port,
                    effective_key_path,
                    effective_password,
                )

            for host in ssh_hosts:
                effective_user, effective_port, effective_key_path, _ = (
                    resolved_credentials[host]
                )
                self.db.save_discovery(
                    {"authenticated": False},
                    host,
                    ssh_user=effective_user,
                    ssh_port=effective_port,
                    ssh_key_path=effective_key_path,
                    authenticated=False,
                    preserve_password=True,
                )

            with ThreadPoolExecutor(
                max_workers=min(
                    int(options["ssh_concurrency"]), max(1, len(ssh_hosts))
                )
            ) as executor:
                probe_futures = {
                    executor.submit(
                        probe_via_ssh,
                        host,
                        options["config"],
                        resolved_credentials[host][1],
                        resolved_credentials[host][0],
                        resolved_credentials[host][2],
                        resolved_credentials[host][3] or None,
                    ): host
                    for host in ssh_hosts
                }
                for future in as_completed(probe_futures):
                    target_host = probe_futures[future]
                    try:
                        info = future.result()
                    except SSHError as exc:
                        probe_failures += 1
                        error_message = sanitize_error_message(exc)
                        with self._lock:
                            self._scan_state["probe_failures"] = probe_failures
                            self._scan_state["last_probe_error"] = error_message
                        self.db.save_discovery(
                            {
                                "authenticated": False,
                                "ssh_error": error_message,
                            },
                            target_host,
                            ssh_user=resolved_credentials[target_host][0],
                            ssh_port=resolved_credentials[target_host][1],
                            ssh_key_path=resolved_credentials[target_host][2],
                            authenticated=False,
                            preserve_password=True,
                        )
                        continue
                    self.db.save_discovery(
                        info,
                        target_host,
                        ssh_user=resolved_credentials[target_host][0],
                        ssh_port=resolved_credentials[target_host][1],
                        ssh_key_path=resolved_credentials[target_host][2],
                        ssh_password=resolved_credentials[target_host][3] or None,
                        authenticated=True,
                    )
                    authenticated_hosts.add(target_host)
                    with self._lock:
                        self._scan_state["authenticated"] = len(
                            authenticated_hosts
                        )
        except Exception as exc:
            with self._lock:
                self._scan_state["error"] = sanitize_error_message(exc)
        finally:
            with self._lock:
                self._scan_state.update(
                    {
                        "scanning": False,
                        "scanned": scanned,
                        "found": len(found_hosts),
                        "authenticated": len(authenticated_hosts),
                        "probe_failures": probe_failures,
                        "finished_at": int(time.time()),
                    }
                )

    @staticmethod
    def _parse_networks(
        cidrs: Iterable[str] | str | None,
        local_interfaces: Iterable[LocalIPv4Interface] = (),
    ) -> list[ipaddress.IPv4Network]:
        if isinstance(cidrs, str):
            values = re.split(r"[,;\s]+", cidrs)
        elif cidrs is None:
            values = []
        else:
            values = [str(value) for value in cidrs]
        local_networks = [interface.network for interface in local_interfaces]
        networks: list[ipaddress.IPv4Network] = []
        invalid: list[str] = []
        for value in values:
            value = value.strip()
            if not value:
                continue
            if "/" not in value:
                try:
                    address = ipaddress.IPv4Address(value)
                except ValueError:
                    invalid.append(value)
                    continue
                matching_network = next(
                    (
                        network
                        for network in local_networks
                        if address == network.network_address
                    ),
                    None,
                )
                if matching_network is not None:
                    networks.append(matching_network)
                    continue
                value = f"{address}/32"
            try:
                network = ipaddress.ip_network(value, strict=False)
            except ValueError:
                invalid.append(value)
                continue
            if network.version != 4:
                invalid.append(value)
                continue
            networks.append(network)
        if invalid:
            raise ValueError(f"CIDR 格式无效: {', '.join(invalid)}")
        return list({str(network): network for network in networks}.values())

    def _build_targets(
        self,
        networks: list[ipaddress.IPv4Network],
        source_interfaces: list[LocalIPv4Interface],
        max_hosts: int = 2048,
    ) -> list[tuple[str, str | None]]:
        hosts = self._expand_hosts(networks, max_hosts=max_hosts)
        targets: list[tuple[str, str | None]] = []
        for host in hosts:
            address = ipaddress.IPv4Address(host)
            source_address = next(
                (
                    item.address
                    for item in source_interfaces
                    if address in item.network
                ),
                None,
            )
            targets.append((host, source_address))
        return targets

    @staticmethod
    def _expand_hosts(
        networks: list[ipaddress.IPv4Network], max_hosts: int
    ) -> list[str]:
        result: set[str] = set()

        def network_hosts(network: ipaddress.IPv4Network) -> Iterable[str]:
            if network.num_addresses == 1:
                yield str(network.network_address)
            elif network.num_addresses == 2:
                yield str(network.network_address)
                yield str(network.broadcast_address)
            else:
                for address in network.hosts():
                    yield str(address)

        streams = [iter(network_hosts(network)) for network in networks]
        while streams and len(result) < max_hosts:
            active_streams: list[Iterable[str]] = []
            for stream in streams:
                if len(result) >= max_hosts:
                    break
                try:
                    result.add(next(stream))
                except StopIteration:
                    continue
                active_streams.append(stream)
            streams = active_streams
        return sorted(result)

    def state(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._scan_state)
