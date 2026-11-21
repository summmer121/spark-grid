from __future__ import annotations

import json
import os
import re
import shlex
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from .config import Settings
from .db import Database
from .ssh_runner import SSHError, run_remote_command


MODEL_PORTS = [
    int(value)
    for value in os.getenv("SPARK_MODEL_PORTS", "8888,8000,8001,30000").split(",")
    if value.strip().isdigit()
]
CONTAINER_HINTS = [
    value.strip()
    for value in os.getenv(
        "SPARK_MODEL_CONTAINERS", "dsv41-head,dsv41-worker,spark-ds41f"
    ).split(",")
    if value.strip()
]
LOCAL_TZ = ZoneInfo("Asia/Shanghai")

SGLANG_PREFILL_RE = re.compile(
    r"\[(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) TP\d+ EP\d+\] "
    r"Prefill batch, #new-seq: (?P<new_seq>\d+), #new-token: (?P<new_tokens>\d+), "
    r"#cached-token: (?P<cached_tokens>\d+), full token usage: (?P<full_usage>[\d.]+), "
    r"swa token usage: (?P<swa_usage>[\d.]+), #running-req: (?P<running>\d+), "
    r"#queue-req: (?P<queue>\d+), #pending-token: (?P<pending>\d+).*?"
    r"input throughput \(token/s\): (?P<throughput>[\d.]+)"
)
SGLANG_DECODE_RE = re.compile(
    r"\[(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) TP\d+ EP\d+\] "
    r"Decode batch, #running-req: (?P<running>\d+), #full token: \d+, "
    r"full token usage: (?P<full_usage>[\d.]+),.*?"
    r"accept rate: (?P<accept_rate>[\d.]+),.*?"
    r"gen throughput \(token/s\): (?P<throughput>[\d.]+), #queue-req: (?P<queue>\d+)"
)
VLLM_THROUGHPUT_RE = re.compile(
    r"Avg prompt throughput: (?P<prompt>[\d.]+) tokens/s, "
    r"Avg generation throughput: (?P<generation>[\d.]+) tokens/s, "
    r"Running: (?P<running>\d+) reqs, Waiting: (?P<waiting>\d+) reqs, "
    r"GPU KV cache usage: (?P<kv>[\d.]+)%, Prefix cache hit rate: (?P<prefix>[\d.]+)%"
)
VLLM_ACCEPT_RE = re.compile(r"Avg Draft acceptance rate: (?P<rate>[\d.]+)%")
VLLM_TIME_RE = re.compile(r"INFO (?P<ts>\d{2}-\d{2} \d{2}:\d{2}:\d{2})")
PROM_LINE_RE = re.compile(
    r"^(?P<name>[A-Za-z_:][A-Za-z0-9_:]*)"
    r"(?:\{(?P<labels>[^}]*)\})?\s+(?P<value>[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?)$"
)
PROM_LABEL_RE = re.compile(r'([A-Za-z_][A-Za-z0-9_]*)="((?:\\.|[^"])*)"')


def _http_get(url: str, timeout: float = 3) -> tuple[int, str]:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json,text/plain,*/*",
            "User-Agent": "SparkGrid-ModelMonitor/0.2",
        },
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=timeout) as response:
            return response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")
    except (OSError, urllib.error.URLError):
        return 0, ""


def _prometheus_samples(text: str) -> dict[str, list[dict[str, Any]]]:
    samples: dict[str, list[dict[str, Any]]] = {}
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        match = PROM_LINE_RE.match(line)
        if not match:
            continue
        try:
            value = float(match.group("value"))
        except ValueError:
            continue
        labels = {
            key: value.replace('\\"', '"')
            for key, value in PROM_LABEL_RE.findall(match.group("labels") or "")
        }
        samples.setdefault(match.group("name"), []).append(
            {"labels": labels, "value": value}
        )
    return samples


def _sum_metric(
    samples: dict[str, list[dict[str, Any]]],
    name: str,
    labels: dict[str, str] | None = None,
) -> float | None:
    values = []
    for sample in samples.get(name, []):
        if labels and any(sample["labels"].get(key) != value for key, value in labels.items()):
            continue
        values.append(float(sample["value"]))
    return sum(values) if values else None


def _display_sglang_time(value: str | None) -> str | None:
    if not value:
        return None
    try:
        parsed = datetime.strptime(value, "%Y-%m-%d %H:%M:%S").replace(
            tzinfo=timezone.utc
        )
        return parsed.astimezone(LOCAL_TZ).strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return value


def _display_vllm_time(value: str | None) -> str | None:
    if not value:
        return None
    try:
        parsed = datetime.strptime(
            f"{datetime.now().year}-{value}", "%Y-%m-%d %H:%M:%S"
        ).replace(tzinfo=timezone.utc)
        return parsed.astimezone(LOCAL_TZ).strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return value


def _parse_docker_timestamp(value: str) -> datetime:
    normalized = value.replace("Z", "+00:00")
    normalized = re.sub(r"(\.\d{6})\d+", r"\1", normalized)
    return datetime.fromisoformat(normalized)


def _docker_since(value: str | None) -> str | None:
    if not value:
        return None
    try:
        _parse_docker_timestamp(value)
    except ValueError:
        return None
    return value


def _parse_sglang_runtime(lines: list[str]) -> dict[str, Any]:
    prefill_events: list[dict[str, Any]] = []
    decode_events: list[dict[str, Any]] = []
    errors = 0
    for line in lines:
        if re.search(r"\bERROR\b|Traceback|CUDA error|Exception", line):
            errors += 1
        match = SGLANG_PREFILL_RE.search(line)
        if match:
            item = match.groupdict()
            item["kind"] = "prefill"
            item["ts_epoch"] = _parse_naive_time(item["ts"])
            item["display_ts"] = _display_sglang_time(item["ts"])
            prefill_events.append(item)
            continue
        match = SGLANG_DECODE_RE.search(line)
        if match:
            item = match.groupdict()
            item["kind"] = "decode"
            item["ts_epoch"] = _parse_naive_time(item["ts"])
            item["display_ts"] = _display_sglang_time(item["ts"])
            decode_events.append(item)
    latest_prefill = prefill_events[-1] if prefill_events else None
    latest_decode = decode_events[-1] if decode_events else None
    latest = max(
        [event for event in (latest_prefill, latest_decode) if event],
        key=lambda event: event.get("ts_epoch") or 0,
        default=None,
    )
    cached = int(latest_prefill.get("cached_tokens") or 0) if latest_prefill else 0
    new_tokens = int(latest_prefill.get("new_tokens") or 0) if latest_prefill else 0
    return {
        "running_requests": int(latest.get("running") or 0) if latest else None,
        "queued_requests": int(latest.get("queue") or 0) if latest else None,
        "pending_tokens": int(latest_prefill.get("pending") or 0) if latest_prefill else None,
        "prefill_tokens_per_sec": round(float(latest_prefill["throughput"]), 2) if latest_prefill else None,
        "decode_tokens_per_sec": round(float(latest_decode["throughput"]), 2) if latest_decode else None,
        "new_tokens_last_prefill": new_tokens if latest_prefill else None,
        "cached_tokens_last_prefill": cached if latest_prefill else None,
        "full_token_usage_pct": round(float(latest["full_usage"]) * 100, 1) if latest and latest.get("full_usage") else None,
        "accept_rate_pct": round(float(latest_decode["accept_rate"]) * 100, 2) if latest_decode else None,
        "prefix_cache_hit_rate_pct": round(cached / (cached + new_tokens) * 100, 1) if cached + new_tokens else None,
        "ttft_avg_ms": None,
        "e2e_avg_ms": None,
        "inter_token_avg_ms": None,
        "successful_requests": None,
        "failed_requests": None,
        "last_batch_type": latest.get("kind") if latest else None,
        "last_batch_at": latest.get("display_ts") if latest else None,
        "last_prefill_at": latest_prefill.get("display_ts") if latest_prefill else None,
        "last_decode_at": latest_decode.get("display_ts") if latest_decode else None,
        "log_error_count": errors,
        "metrics_source": "batch logs",
    }


def _parse_naive_time(value: str) -> int | None:
    try:
        return int(datetime.strptime(value, "%Y-%m-%d %H:%M:%S").timestamp())
    except ValueError:
        return None


def _parse_vllm_logs(lines: list[str]) -> dict[str, Any]:
    latest_throughput: dict[str, Any] | None = None
    accept_rate = None
    errors = 0
    for line in lines:
        lower = line.lower()
        if "error" in lower or "traceback" in lower or "exception" in lower:
            errors += 1
        match = VLLM_THROUGHPUT_RE.search(line)
        if match:
            values = match.groupdict()
            time_match = VLLM_TIME_RE.search(line)
            latest_throughput = {
                "prompt": float(values["prompt"]),
                "generation": float(values["generation"]),
                "running": int(values["running"]),
                "waiting": int(values["waiting"]),
                "kv": float(values["kv"]),
                "prefix": float(values["prefix"]),
                "display_ts": _display_vllm_time(
                    time_match.group("ts") if time_match else None
                ),
            }
        match = VLLM_ACCEPT_RE.search(line)
        if match:
            accept_rate = float(match.group("rate"))
    return {
        "log_throughput": latest_throughput,
        "accept_rate_pct": accept_rate,
        "log_error_count": errors,
    }


def _arg_value(args: list[str], *names: str) -> str | None:
    for index, value in enumerate(args):
        if value in names and index + 1 < len(args):
            return args[index + 1]
        for name in names:
            prefix = f"{name}="
            if value.startswith(prefix):
                return value[len(prefix) :]
    return None


class ModelMonitor:
    def __init__(self, db: Database, settings: Settings):
        self.db = db
        self.settings = settings
        self._lock = threading.Lock()
        self._overview_cache: tuple[float, dict[str, Any]] | None = None
        self._runtime_cache: tuple[float, dict[str, Any]] | None = None
        self._container_cache: dict[str, tuple[float, list[dict[str, str]]]] = {}
        self._inspect_cache: dict[tuple[str, str], tuple[float, dict[str, Any]]] = {}
        self._logs_cache: dict[
            tuple[str, int, str, str], tuple[float, list[str]]
        ] = {}
        self._counter_state: dict[str, tuple[float, dict[str, float]]] = {}
        self._stop_event = threading.Event()
        self._worker: threading.Thread | None = None

    def start(self) -> None:
        if self._worker and self._worker.is_alive():
            return
        self._stop_event.clear()
        self._worker = threading.Thread(
            target=self._sample_loop,
            name="model-throughput-monitor",
            daemon=True,
        )
        self._worker.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._worker:
            self._worker.join(timeout=2)

    def _sample_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self.overview()
            except Exception:
                pass
            self._stop_event.wait(15)

    def _nodes(self) -> list[dict[str, Any]]:
        return sorted(self.db.list_nodes(), key=lambda node: int(node["id"]))

    def _containers(self, node: dict[str, Any]) -> list[dict[str, str]]:
        host = str(node["host"])
        now = time.monotonic()
        with self._lock:
            cached = self._container_cache.get(host)
            if cached and now - cached[0] < 20:
                return cached[1]
        output = run_remote_command(
            node,
            self.settings,
            ["docker", "ps", "--format", "{{.Names}}|{{.Image}}"],
            timeout=8,
        )
        containers = []
        for line in output.splitlines():
            if "|" not in line:
                continue
            name, image = line.split("|", 1)
            containers.append({"name": name.strip(), "image": image.strip()})
        with self._lock:
            self._container_cache[host] = (now, containers)
        return containers

    def _choose_container(
        self, node: dict[str, Any], engine_hint: str | None = None
    ) -> dict[str, str] | None:
        containers = self._containers(node)
        if not containers:
            return None
        for hint in CONTAINER_HINTS:
            for container in containers:
                if container["name"] == hint:
                    return container
        scored: list[tuple[int, dict[str, str]]] = []
        for container in containers:
            searchable = f"{container['name']} {container['image']}".lower()
            score = 0
            if engine_hint and engine_hint.lower() in searchable:
                score += 30
            if any(term in searchable for term in ("vllm", "sglang", "spark", "dsv", "ds41")):
                score += 10
            scored.append((score, container))
        scored.sort(key=lambda item: item[0], reverse=True)
        return scored[0][1]

    def _container_process_args(
        self, node: dict[str, Any], container_name: str
    ) -> tuple[list[str], str]:
        output = run_remote_command(
            node,
            self.settings,
            ["docker", "top", container_name, "-eo", "pid,args"],
            timeout=8,
        )
        candidates: list[tuple[int, list[str], str]] = []
        for line in output.splitlines():
            if not re.match(r"^\s*\d+\s+", line):
                continue
            try:
                args = shlex.split(line.strip().split(None, 1)[1])
            except (IndexError, ValueError):
                continue
            lowered = " ".join(args).lower()
            score = 0
            if "vllm" in lowered:
                score += 20
            if "sglang" in lowered or "launch_server" in lowered:
                score += 20
            if "--model" in lowered or "--served-model-name" in lowered:
                score += 10
            if score:
                candidates.append((score, args, lowered))
        if not candidates:
            return [], ""
        candidates.sort(key=lambda item: item[0], reverse=True)
        return candidates[0][1], candidates[0][2]

    def _detect_runtime(self) -> dict[str, Any]:
        now = time.monotonic()
        with self._lock:
            if self._runtime_cache and now - self._runtime_cache[0] < 10:
                return self._runtime_cache[1]
        nodes = self._nodes()
        if not nodes:
            return {
                "nodes": [],
                "port": MODEL_PORTS[0] if MODEL_PORTS else 8888,
                "engine": "unknown",
                "container": None,
                "args": [],
                "metrics": "",
            }
        head = nodes[0]
        selected = None
        head_container = self._choose_container(head)
        args: list[str] = []
        process_text = ""
        if head_container:
            try:
                args, process_text = self._container_process_args(
                    head, head_container["name"]
                )
            except SSHError:
                pass
        for port in MODEL_PORTS:
            status, _ = _http_get(f"http://{head['host']}:{port}/health")
            if status != 200:
                continue
            _, metrics = _http_get(f"http://{head['host']}:{port}/metrics", 5)
            if "vllm:" in metrics:
                engine = "vllm"
            elif "sglang:" in metrics:
                engine = "sglang"
            elif "vllm" in process_text:
                engine = "vllm"
            elif "sglang" in process_text or "launch_server" in process_text:
                engine = "sglang"
            else:
                engine = "unknown"
            selected = {
                "nodes": nodes,
                "port": port,
                "engine": engine,
                "container": head_container,
                "args": args,
                "metrics": metrics,
            }
            break
        if selected is None:
            port = MODEL_PORTS[0] if MODEL_PORTS else 8888
            engine = (
                "vllm"
                if "vllm" in process_text
                else "sglang"
                if "sglang" in process_text or "launch_server" in process_text
                else "unknown"
            )
            selected = {
                "nodes": nodes,
                "port": port,
                "engine": engine,
                "container": head_container,
                "args": args,
                "metrics": "",
            }
        with self._lock:
            self._runtime_cache = (now, selected)
        return selected

    def _inspect_container(
        self, head: dict[str, Any], container: dict[str, str] | None
    ) -> dict[str, Any]:
        if not container:
            return {"status": "not_found", "running": False, "name": "unknown"}
        key = (str(head["host"]), container["name"])
        now = time.monotonic()
        with self._lock:
            cached = self._inspect_cache.get(key)
            if cached and now - cached[0] < 5:
                return cached[1]
        state_raw = run_remote_command(
            head,
            self.settings,
            ["docker", "inspect", "--format", "{{json .State}}", container["name"]],
            timeout=8,
        )
        try:
            state = json.loads(state_raw.strip().splitlines()[-1])
        except (ValueError, IndexError):
            state = {}
        started_at = state.get("StartedAt")
        uptime = None
        if started_at and state.get("Running"):
            try:
                uptime = int(
                    time.time()
                    - _parse_docker_timestamp(started_at).timestamp()
                )
            except ValueError:
                pass
        result = {
            "name": container["name"],
            "status": state.get("Status", "unknown"),
            "health": (state.get("Health") or {}).get("Status"),
            "running": bool(state.get("Running")),
            "started_at": started_at,
            "uptime": uptime,
            "image": container.get("image"),
        }
        with self._lock:
            self._inspect_cache[key] = (now, result)
        return result

    def _health_map(
        self, nodes: list[dict[str, Any]], port: int
    ) -> dict[int, bool]:
        result: dict[int, bool] = {}
        with ThreadPoolExecutor(max_workers=min(8, max(1, len(nodes)))) as executor:
            futures = {
                executor.submit(
                    _http_get, f"http://{node['host']}:{port}/health", 2
                ): int(node["id"])
                for node in nodes
            }
            for future in as_completed(futures):
                node_id = futures[future]
                try:
                    result[node_id] = future.result()[0] == 200
                except Exception:
                    result[node_id] = False
        return result

    def _config(self, args: list[str], metrics: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
        model_name = None
        for samples in metrics.values():
            for sample in samples:
                if sample["labels"].get("model_name"):
                    model_name = sample["labels"]["model_name"]
                    break
            if model_name:
                break
        spec = _arg_value(
            args, "--speculative-config", "--speculative-algorithm"
        )
        model_path = _arg_value(
            args, "--model-path", "--model", "--model-name"
        )
        if not model_path and "serve" in args:
            index = args.index("serve")
            if index + 1 < len(args) and not args[index + 1].startswith("-"):
                model_path = args[index + 1]
        return {
            "served_model_name": _arg_value(
                args, "--served-model-name", "--model"
            )
            or model_name
            or "unknown",
            "tensor_parallel_size": _arg_value(
                args, "--tensor-parallel-size", "--tp"
            ),
            "data_parallel_size": _arg_value(args, "--data-parallel-size", "--dp"),
            "expert_parallel_size": _arg_value(args, "--ep-size", "--expert-parallel-size"),
            "max_running_requests": _arg_value(
                args, "--max-num-seqs", "--max-running-requests"
            ),
            "max_num_seqs": _arg_value(args, "--max-num-seqs"),
            "kv_cache_memory_bytes": _arg_value(
                args, "--kv-cache-memory-bytes"
            ),
            "gpu_memory_utilization": _arg_value(
                args, "--gpu-memory-utilization", "--mem-fraction-static"
            ),
            "block_size": _arg_value(args, "--block-size"),
            "prefix_cache_retention_interval": _arg_value(
                args, "--prefix-cache-retention-interval"
            ),
            "long_prefill_token_threshold": _arg_value(
                args, "--long-prefill-token-threshold"
            ),
            "context_length": _arg_value(args, "--max-model-len", "--context-length"),
            "speculative_algorithm": spec
            or _arg_value(args, "--speculative-algorithm"),
            "speculative_block_size": _arg_value(
                args, "--speculative-dspark-block-size"
            ),
            "model_path": model_path,
        }

    def _vllm_inference(self, metrics_text: str, logs: list[str]) -> dict[str, Any]:
        samples = _prometheus_samples(metrics_text)
        prompt_total = _sum_metric(samples, "vllm:prompt_tokens_total") or 0.0
        generation_total = (
            _sum_metric(samples, "vllm:generation_tokens_total") or 0.0
        )
        now = time.monotonic()
        previous = self._counter_state.get("vllm")
        prefill_rate = decode_rate = None
        if previous:
            elapsed = max(0.1, now - previous[0])
            prefill_rate = max(0.0, (prompt_total - previous[1]["prompt"]) / elapsed)
            decode_rate = max(
                0.0, (generation_total - previous[1]["generation"]) / elapsed
            )
        self._counter_state["vllm"] = (
            now,
            {"prompt": prompt_total, "generation": generation_total},
        )
        log_data = _parse_vllm_logs(logs)
        log_throughput = log_data["log_throughput"] or {}
        if prefill_rate is None:
            prefill_rate = log_throughput.get("prompt")
        if decode_rate is None:
            decode_rate = log_throughput.get("generation")
        running = _sum_metric(samples, "vllm:num_requests_running")
        waiting = _sum_metric(samples, "vllm:num_requests_waiting")
        kv = _sum_metric(samples, "vllm:kv_cache_usage_perc")
        prefix_queries = _sum_metric(
            samples, "vllm:prefix_cache_queries_total"
        )
        prefix_hits = _sum_metric(samples, "vllm:prefix_cache_hits_total")
        success = _sum_metric(samples, "vllm:request_success_total")
        failed = _sum_metric(
            samples,
            "vllm:request_success_total",
            {"finished_reason": "error"},
        )
        pending = None
        for name in samples:
            if "pending" in name.lower() and "token" in name.lower():
                pending = _sum_metric(samples, name)
                break
        ttft_sum = _sum_metric(samples, "vllm:time_to_first_token_seconds_sum")
        ttft_count = _sum_metric(samples, "vllm:time_to_first_token_seconds_count")
        e2e_sum = _sum_metric(samples, "vllm:e2e_request_latency_seconds_sum")
        e2e_count = _sum_metric(samples, "vllm:e2e_request_latency_seconds_count")
        itl_sum = _sum_metric(samples, "vllm:inter_token_latency_seconds_sum")
        itl_count = _sum_metric(samples, "vllm:inter_token_latency_seconds_count")
        return {
            "running_requests": int(running) if running is not None else None,
            "queued_requests": int(waiting) if waiting is not None else None,
            "pending_tokens": int(pending) if pending is not None else None,
            "prefill_tokens_per_sec": round(prefill_rate, 2) if prefill_rate is not None else None,
            "decode_tokens_per_sec": round(decode_rate, 2) if decode_rate is not None else None,
            "new_tokens_last_prefill": None,
            "cached_tokens_last_prefill": None,
            "full_token_usage_pct": round(kv * 100, 1) if kv is not None else None,
            "accept_rate_pct": log_data["accept_rate_pct"],
            "prefix_cache_hit_rate_pct": round(prefix_hits / prefix_queries * 100, 1)
            if prefix_hits is not None and prefix_queries
            else None,
            "ttft_avg_ms": round(ttft_sum / ttft_count * 1000, 1)
            if ttft_sum is not None and ttft_count
            else None,
            "e2e_avg_ms": round(e2e_sum / e2e_count * 1000, 1)
            if e2e_sum is not None and e2e_count
            else None,
            "inter_token_avg_ms": round(itl_sum / itl_count * 1000, 1)
            if itl_sum is not None and itl_count
            else None,
            "successful_requests": int(success) if success is not None else None,
            "failed_requests": int(failed) if failed is not None else None,
            "request_count_total": int(success) if success is not None else None,
            "prompt_tokens_total": int(prompt_total) if prompt_total else None,
            "generation_tokens_total": int(generation_total)
            if generation_total
            else None,
            "total_tokens": int(prompt_total + generation_total)
            if prompt_total or generation_total
            else None,
            "last_batch_type": "decode" if (running or decode_rate) else "idle",
            "last_batch_at": log_throughput.get("display_ts"),
            "last_prefill_at": log_throughput.get("display_ts"),
            "last_decode_at": log_throughput.get("display_ts"),
            "log_error_count": log_data["log_error_count"],
            "metrics_source": "Prometheus /metrics",
        }

    def _sglang_inference(self, metrics_text: str, logs: list[str]) -> dict[str, Any]:
        log_data = _parse_sglang_runtime(logs)
        samples = _prometheus_samples(metrics_text)
        metric_map = {
            "running_requests": "sglang:num_running_reqs",
            "queued_requests": "sglang:num_queue_reqs",
            "prefill_tokens_per_sec": "sglang:prompt_throughput",
            "decode_tokens_per_sec": "sglang:generation_throughput",
            "full_token_usage_pct": "sglang:token_usage",
        }
        for field, name in metric_map.items():
            value = _sum_metric(samples, name)
            if value is not None:
                log_data[field] = round(value, 2)
        prompt_total = _sum_metric(samples, "sglang:prompt_tokens_total")
        generation_total = _sum_metric(
            samples, "sglang:generation_tokens_total"
        )
        log_data["prompt_tokens_total"] = (
            int(prompt_total) if prompt_total is not None else None
        )
        log_data["generation_tokens_total"] = (
            int(generation_total) if generation_total is not None else None
        )
        log_data["total_tokens"] = (
            int(prompt_total + generation_total)
            if prompt_total is not None and generation_total is not None
            else None
        )
        request_total = _sum_metric(samples, "sglang:request_success_total")
        if request_total is None:
            request_total = _sum_metric(samples, "sglang:num_requests_total")
        log_data["request_count_total"] = (
            int(request_total) if request_total is not None else None
        )
        log_data["metrics_source"] = (
            "Prometheus + logs" if samples else "batch logs"
        )
        return log_data

    def overview(self) -> dict[str, Any]:
        now = time.monotonic()
        with self._lock:
            if self._overview_cache and now - self._overview_cache[0] < 4:
                return self._overview_cache[1]
        runtime = self._detect_runtime()
        nodes = runtime["nodes"]
        if not nodes:
            return {"status": "unavailable", "generated_at": int(time.time()), "nodes": []}
        head = nodes[0]
        port = int(runtime["port"])
        engine = runtime.get("engine") or "unknown"
        health = self._health_map(nodes, port)
        if engine == "vllm":
            container_health: dict[int, bool] = {}
            with ThreadPoolExecutor(max_workers=min(8, len(nodes))) as executor:
                futures = {}
                for node in nodes:
                    selected = self._choose_container(node, "vllm")
                    if selected:
                        futures[
                            executor.submit(
                                self._inspect_container, node, selected
                            )
                        ] = int(node["id"])
                for future in as_completed(futures):
                    node_id = futures[future]
                    try:
                        container_health[node_id] = bool(
                            future.result().get("running")
                        )
                    except SSHError:
                        container_health[node_id] = False
            for node in nodes:
                node_id = int(node["id"])
                if not health.get(node_id) and container_health.get(node_id):
                    health[node_id] = True
        container_info = runtime.get("container")
        try:
            inspected = self._inspect_container(head, container_info)
        except SSHError as exc:
            inspected = {
                "name": container_info["name"] if container_info else "unknown",
                "status": "unreachable",
                "running": False,
                "error": str(exc),
            }
        try:
            head_logs = self._fetch_logs(head, 0, 800, container_info)
        except SSHError:
            head_logs = []
        metrics_text = runtime.get("metrics") or ""
        metrics = _prometheus_samples(metrics_text)
        if engine == "vllm":
            inference = self._vllm_inference(metrics_text, head_logs)
        else:
            inference = self._sglang_inference(metrics_text, head_logs)
        process_started_at = inspected.get("started_at")
        inference["data_since"] = process_started_at
        inference["data_scope"] = "current_model_process"
        throughput_history = {
            "window_minutes": 30,
            "bucket_seconds": 60,
            "process_started_at": process_started_at,
            "points": [],
        }
        if process_started_at:
            self.db.record_model_throughput(
                process_started_at,
                engine,
                inference.get("prefill_tokens_per_sec"),
                inference.get("decode_tokens_per_sec"),
            )
            throughput_history = self.db.load_model_throughput_history(
                process_started_at,
                minutes=30,
                buckets=30,
            )
        config = self._config(runtime.get("args") or [], metrics)
        healthy = sum(1 for value in health.values() if value)
        total = len(nodes)
        if healthy == total and inspected.get("running"):
            status = "ready"
        elif healthy > 0:
            status = "degraded"
        else:
            status = "offline"
        latest_metrics = [
            node["latest_metrics"]
            for node in nodes
            if node.get("latest_metrics") and node.get("last_status") == "online"
        ]
        gpu_loads = []
        memory_values = []
        temperatures = []
        rx = tx = 0.0
        for node_metrics in latest_metrics:
            resources = node_metrics.get("resources", {})
            for gpu in node_metrics.get("gpus") or []:
                if isinstance(gpu.get("load_percent"), (int, float)):
                    gpu_loads.append(float(gpu["load_percent"]))
            if isinstance(resources.get("memory_percent"), (int, float)):
                memory_values.append(float(resources["memory_percent"]))
            if isinstance(resources.get("temperature_max"), (int, float)):
                temperatures.append(float(resources["temperature_max"]))
            rx += float(resources.get("rx_rate") or 0)
            tx += float(resources.get("tx_rate") or 0)
        result = {
            "status": status,
            "generated_at": int(time.time()),
            "endpoint": f"http://{head['host']}:{port}",
            "engine": engine,
            "engine_label": {"vllm": "vLLM", "sglang": "SGLang"}.get(engine, "Unknown"),
            "model": {
                "name": config["served_model_name"],
                "path": config["model_path"],
                "health_nodes": healthy,
                "total_nodes": total,
                "nodes": [
                    {
                        "rank": index,
                        "name": node["name"],
                        "host": node["host"],
                        "healthy": health.get(int(node["id"]), False),
                    }
                    for index, node in enumerate(nodes)
                ],
            },
            "container": inspected,
            "config": config,
            "inference": inference,
            "throughput_history": throughput_history,
            "cluster": {
                "gpu_load_avg": round(sum(gpu_loads) / len(gpu_loads), 1) if gpu_loads else None,
                "memory_avg": round(sum(memory_values) / len(memory_values), 1) if memory_values else None,
                "temperature_max": max(temperatures) if temperatures else None,
                "network_rx_rate": round(rx, 1),
                "network_tx_rate": round(tx, 1),
            },
        }
        with self._lock:
            self._overview_cache = (now, result)
        return result

    def _fetch_logs(
        self,
        node: dict[str, Any],
        rank: int,
        lines: int,
        container: dict[str, str] | None = None,
        use_cache: bool = True,
    ) -> list[str]:
        selected = container or self._choose_container(node)
        if not selected:
            raise SSHError("no running model container found")
        started_at = None
        try:
            started_at = self._inspect_container(node, selected).get("started_at")
        except SSHError:
            pass
        key = (str(node["host"]), rank, selected["name"], started_at or "")
        now = time.monotonic()
        if use_cache:
            with self._lock:
                cached = self._logs_cache.get(key)
                if cached and now - cached[0] < 4:
                    return cached[1]
        command = [
            "docker",
            "logs",
            "--tail",
            str(max(20, min(lines, 1500))),
            "--timestamps",
        ]
        since = _docker_since(started_at)
        if since:
            command.extend(["--since", since])
        command.append(selected["name"])
        output = run_remote_command(
            node,
            self.settings,
            command,
            timeout=12,
        )
        result = output.splitlines()
        if use_cache:
            with self._lock:
                self._logs_cache[key] = (now, result)
        return result

    def logs(self, rank: str = "0", lines: int = 220) -> dict[str, Any]:
        nodes = self._nodes()
        if not nodes:
            return {"generated_at": int(time.time()), "lines": []}
        if rank == "all":
            output: list[str] = []
            with ThreadPoolExecutor(max_workers=min(8, len(nodes))) as executor:
                futures = {
                    executor.submit(self._fetch_logs, node, index, lines): (index, node)
                    for index, node in enumerate(nodes)
                }
                sections: dict[int, list[str]] = {}
                for future in as_completed(futures):
                    index, node = futures[future]
                    try:
                        sections[index] = [
                            f"[{node['name']}] {line}" for line in future.result()
                        ]
                    except SSHError as exc:
                        sections[index] = [f"[{node['name']}] ERROR {exc}"]
            for index in sorted(sections):
                output.extend(sections[index])
            return {
                "generated_at": int(time.time()),
                "rank": "all",
                "lines": output[-min(1200, len(output)):],
            }
        try:
            index = max(0, min(len(nodes) - 1, int(rank)))
        except ValueError:
            index = 0
        node = nodes[index]
        try:
            lines_out = self._fetch_logs(
                node, index, lines, use_cache=False
            )
        except SSHError as exc:
            lines_out = [f"ERROR {exc}"]
        return {
            "generated_at": int(time.time()),
            "rank": index,
            "node": node["name"],
            "lines": lines_out,
        }
