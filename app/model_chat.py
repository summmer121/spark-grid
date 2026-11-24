"""Model chat playground.

Proxies OpenAI-compatible chat requests to the running model service on the
cluster head node and reports generation metrics (prompt/completion tokens,
TTFT, wall time, throughput).  The API key is read from the container's
Docker secret (``/run/secrets/model-api-key``) over SSH and cached; it never
leaves the server.
"""

from __future__ import annotations

import json
import shlex
import threading
import time
import urllib.error
import urllib.request
from typing import Any

from .ssh_runner import SSHError

KEY_TTL_SECONDS = 600.0
CHAT_TIMEOUT_SECONDS = 300.0
MAX_MESSAGES = 60
MAX_CONTENT_CHARS = 32000


class ChatError(RuntimeError):
    """Raised when a chat test cannot be performed."""


class ModelChat:
    def __init__(self, db: Any, config: Any, control: Any):
        self.db = db
        self.config = config
        self.control = control
        self._lock = threading.Lock()
        self._key_cache: tuple[float, str] = (0.0, "")

    # ---------------------------------------------------------------- helpers

    def _current(self) -> Any:
        return self.config.current()

    def _endpoint(self) -> tuple[str, int]:
        head = self.control._head()
        ports = [int(port) for port in self._current().model_ports] or [8888]
        return str(head["host"]), ports[0]

    def _running_containers(self) -> list[str]:
        output = self.control._run(
            ["bash", "-lc", "docker ps --format '{{.Names}}' 2>/dev/null"], timeout=20.0
        )
        return [line.strip() for line in output.splitlines() if line.strip()]

    def _api_key(self) -> str:
        now = time.time()
        with self._lock:
            cached_at, cached_key = self._key_cache
            if cached_key and now - cached_at < KEY_TTL_SECONDS:
                return cached_key
        containers = self._running_containers()
        if not containers:
            raise ChatError("HEAD 节点上没有运行中的模型容器")
        for name in containers:
            try:
                output = self.control._run(
                    [
                        "bash",
                        "-lc",
                        f"docker exec {shlex.quote(name)} cat /run/secrets/model-api-key 2>/dev/null",
                    ],
                    timeout=15.0,
                )
            except SSHError:
                continue
            key = (output or "").strip().splitlines()
            if key and key[-1].strip():
                value = key[-1].strip()
                with self._lock:
                    self._key_cache = (now, value)
                return value
        raise ChatError("未能从容器 secret 读取模型 API Key")

    def _request(self, path: str, payload: dict[str, Any] | None = None, stream: bool = False):
        host, port = self._endpoint()
        key = self._api_key()
        url = f"http://{host}:{port}{path}"
        headers = {"Authorization": f"Bearer {key}", "Accept": "application/json"}
        data = None
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            url, data=data, headers=headers, method="POST" if data else "GET"
        )
        try:
            return urllib.request.urlopen(request, timeout=CHAT_TIMEOUT_SECONDS)
        except urllib.error.HTTPError as exc:
            detail = exc.read(600).decode("utf-8", "replace")
            raise ChatError(f"模型服务返回 {exc.code}: {detail[:300]}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ChatError(f"无法连接模型服务 {host}:{port}（{exc}）") from exc

    # ------------------------------------------------------------- public API

    def info(self) -> dict[str, Any]:
        host, port = "", 0
        try:
            host, port = self._endpoint()
            response = self._request("/v1/models")
            payload = json.loads(response.read().decode("utf-8", "replace") or "{}")
            models = [
                str(item.get("id"))
                for item in payload.get("data", [])
                if isinstance(item, dict) and item.get("id")
            ]
            return {
                "available": True,
                "endpoint": f"http://{host}:{port}",
                "models": models,
                "error": "",
            }
        except ChatError as exc:
            return {
                "available": False,
                "endpoint": f"http://{host}:{port}" if host else "",
                "models": [],
                "error": str(exc),
            }

    def chat(self, body: dict[str, Any]) -> dict[str, Any]:
        raw_messages = body.get("messages")
        if not isinstance(raw_messages, list) or not raw_messages:
            raise ChatError("消息不能为空")
        messages = []
        for item in raw_messages[-MAX_MESSAGES:]:
            if not isinstance(item, dict):
                continue
            role = str(item.get("role") or "").strip()
            content = str(item.get("content") or "")[:MAX_CONTENT_CHARS]
            if role not in ("system", "user", "assistant") or not content:
                continue
            messages.append({"role": role, "content": content})
        if not messages:
            raise ChatError("没有有效的对话消息")

        model = str(body.get("model") or "").strip()
        max_tokens = int(body.get("max_tokens") or 512)
        max_tokens = max(1, min(8192, max_tokens))
        try:
            temperature = float(body.get("temperature", 0.7))
        except (TypeError, ValueError):
            temperature = 0.7
        temperature = max(0.0, min(2.0, temperature))

        enable_thinking = bool(body.get("enable_thinking"))
        payload: dict[str, Any] = {
            "messages": messages,
            "stream": True,
            "stream_options": {"include_usage": True},
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if model:
            payload["model"] = model
        if not enable_thinking:
            # vLLM chat template switches; harmless for models without support.
            payload["chat_template_kwargs"] = {"enable_thinking": False}

        started = time.perf_counter()
        ttft_ms: float | None = None
        parts: list[str] = []
        reasoning: list[str] = []
        usage: dict[str, Any] = {}
        finish_reason = ""
        chunks = 0

        response = self._request("/v1/chat/completions", payload, stream=True)
        try:
            for raw_line in response:
                line = raw_line.decode("utf-8", "replace").strip()
                if not line or not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if not data or data == "[DONE]":
                    continue
                try:
                    chunk = json.loads(data)
                except ValueError:
                    continue
                chunks += 1
                choice = (chunk.get("choices") or [{}])[0]
                delta = choice.get("delta") or {}
                piece = delta.get("content")
                reasoning_piece = delta.get("reasoning_content") or delta.get("reasoning")
                if piece or reasoning_piece:
                    if ttft_ms is None:
                        ttft_ms = (time.perf_counter() - started) * 1000.0
                if piece:
                    parts.append(str(piece))
                if reasoning_piece:
                    reasoning.append(str(reasoning_piece))
                if choice.get("finish_reason"):
                    finish_reason = str(choice["finish_reason"])
                if isinstance(chunk.get("usage"), dict):
                    usage = chunk["usage"]
        finally:
            response.close()

        total_ms = (time.perf_counter() - started) * 1000.0
        content = "".join(parts)
        prompt_tokens = int(usage.get("prompt_tokens") or 0)
        completion_tokens = int(usage.get("completion_tokens") or 0)
        gen_ms = max(1.0, total_ms - (ttft_ms or 0.0))
        throughput = (completion_tokens / gen_ms * 1000.0) if completion_tokens else 0.0
        return {
            "ok": True,
            "content": content,
            "reasoning": "".join(reasoning),
            "model": model,
            "usage": {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": prompt_tokens + completion_tokens,
            },
            "metrics": {
                "ttft_ms": round(ttft_ms, 1) if ttft_ms is not None else None,
                "total_ms": round(total_ms, 1),
                "generation_ms": round(gen_ms, 1),
                "throughput": round(throughput, 2),
                "chunks": chunks,
                "finish_reason": finish_reason,
            },
        }
