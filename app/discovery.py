from __future__ import annotations

import ipaddress
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Iterable

from .config import Settings
from .db import Database
from .ssh_runner import SSHError, probe_via_ssh


def _local_ipv4_networks() -> list[ipaddress.IPv4Network]:
    networks: list[ipaddress.IPv4Network] = []
    try:
        import psutil  # type: ignore

        for name, addresses in psutil.net_if_addrs().items():
            if name.startswith(("lo", "docker", "veth", "br-", "virbr")):
                continue
            for address in addresses:
                if address.family != socket.AF_INET or not address.netmask:
                    continue
                try:
                    network = ipaddress.ip_network(
                        f"{address.address}/{address.netmask}", strict=False
                    )
                except ValueError:
                    continue
                if network.is_loopback or network.is_link_local:
                    continue
                networks.append(network)
    except ImportError:
        pass
    unique = {str(network): network for network in networks}
    return list(unique.values())


def _port_open(host: str, port: int, timeout: float) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


class DiscoveryService:
    def __init__(self, db: Database, settings: Settings):
        self.db = db
        self.settings = settings
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._scan_state: dict[str, Any] = {
            "scanning": False,
            "scanned": 0,
            "total": 0,
            "found": 0,
            "subnets": [],
            "started_at": None,
            "finished_at": None,
            "error": None,
        }

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._scan_loop, name="spark-ssh-discovery", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _scan_loop(self) -> None:
        if self._stop.wait(3.0):
            return
        self.start_scan()
        while not self._stop.wait(self.settings.discovery_interval):
            self.start_scan()

    def start_scan(self, cidrs: Iterable[str] | None = None) -> dict[str, Any]:
        with self._lock:
            if self._scan_state["scanning"]:
                return dict(self._scan_state)
            networks = self._parse_networks(cidrs)
            hosts = self._expand_hosts(networks, max_hosts=2048)
            self._scan_state = {
                "scanning": True,
                "scanned": 0,
                "total": len(hosts),
                "found": 0,
                "subnets": [str(network) for network in networks],
                "started_at": int(time.time()),
                "finished_at": None,
                "error": None,
            }
        thread = threading.Thread(
            target=self._run_scan,
            args=(hosts,),
            name="spark-ssh-scan",
            daemon=True,
        )
        thread.start()
        return self.state()

    def _run_scan(self, hosts: list[str]) -> None:
        found_hosts: set[str] = set()
        scanned = 0
        try:
            with ThreadPoolExecutor(max_workers=64) as executor:
                futures = {
                    executor.submit(
                        _port_open,
                        host,
                        self.settings.ssh_port,
                        min(0.28, self.settings.node_timeout),
                    ): host
                    for host in hosts
                }
                ssh_hosts: list[str] = []
                for future in as_completed(futures):
                    host = futures[future]
                    try:
                        is_open = future.result()
                    except Exception:
                        is_open = False
                    scanned += 1
                    if is_open:
                        ssh_hosts.append(host)
                    if scanned % 20 == 0:
                        with self._lock:
                            self._scan_state["scanned"] = scanned
                            self._scan_state["found"] = len(found_hosts)

            with ThreadPoolExecutor(max_workers=min(8, max(1, len(ssh_hosts)))) as executor:
                probe_futures = {
                    executor.submit(probe_via_ssh, host, self.settings): host
                    for host in ssh_hosts
                }
                for future in as_completed(probe_futures):
                    try:
                        info = future.result()
                    except SSHError:
                        continue
                    host = str(info.get("host") or probe_futures[future])
                    info["host"] = host
                    self.db.save_discovery(
                        info,
                        host,
                        ssh_user=self.settings.ssh_user,
                        ssh_port=self.settings.ssh_port,
                        ssh_key_path=self.settings.ssh_key_path,
                    )
                    found_hosts.add(host)
                    with self._lock:
                        self._scan_state["found"] = len(found_hosts)
        except Exception as exc:
            with self._lock:
                self._scan_state["error"] = str(exc)
        finally:
            with self._lock:
                self._scan_state.update(
                    {
                        "scanning": False,
                        "scanned": scanned,
                        "found": len(found_hosts),
                        "finished_at": int(time.time()),
                    }
                )

    def _parse_networks(
        self, cidrs: Iterable[str] | None
    ) -> list[ipaddress.IPv4Network]:
        if not cidrs:
            networks = _local_ipv4_networks()
            management = [
                network
                for network in networks
                if str(network.network_address).startswith("192.168.")
            ]
            return management or networks
        networks: list[ipaddress.IPv4Network] = []
        for value in cidrs:
            value = value.strip()
            if not value:
                continue
            if "/" not in value:
                value += "/32"
            try:
                network = ipaddress.ip_network(value, strict=False)
            except ValueError:
                continue
            if network.version == 4:
                networks.append(network)
        return list({str(network): network for network in networks}.values())

    @staticmethod
    def _expand_hosts(
        networks: list[ipaddress.IPv4Network], max_hosts: int
    ) -> list[str]:
        result: set[str] = set()
        for network in networks:
            if network.num_addresses == 1:
                result.add(str(network.network_address))
            elif network.num_addresses == 2:
                result.add(str(network.network_address))
                result.add(str(network.broadcast_address))
            else:
                for address in network.hosts():
                    result.add(str(address))
                    if len(result) >= max_hosts:
                        return sorted(result)
        return sorted(result)

    def state(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._scan_state)
