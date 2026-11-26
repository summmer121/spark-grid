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
ENDPOINT_TTL_SECONDS = 10.0
CHAT_TIMEOUT_SECONDS = 300.0
MAX_MESSAGES = 60
MAX_CONTENT_CHARS = 32000
MAX_IMAGES = 4
MAX_IMAGE_CHARS = 4 * 1024 * 1024  # base64 payload cap (~3 MB image)
MAX_PARTS = 12


def _normalize_content(content: Any) -> Any:
    """Accept a plain string or an OpenAI multimodal content array."""
    if isinstance(content, str):
        return content[:MAX_CONTENT_CHARS]
    if not isinstance(content, list):
        return ""
    parts: list[dict[str, Any]] = []
    images = 0
    for item in content[:MAX_PARTS]:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("type") or "")
        if kind == "text":
            text = str(item.get("text") or "")[:MAX_CONTENT_CHARS]
            if text:
                parts.append({"type": "text", "text": text})
        elif kind == "image_url":
            image = item.get("image_url")
            if not isinstance(image, dict):
                continue
            url = str(image.get("url") or "")
            if not url.startswith("data:image/"):
                continue
            if len(url) > MAX_IMAGE_CHARS:
                continue
            if images >= MAX_IMAGES:
                continue
            images += 1
            parts.append({"type": "image_url", "image_url": {"url": url}})
    return parts


class ChatError(RuntimeError):
    """Raised when a chat test cannot be performed."""


class ModelChat:
    def __init__(self, db: Any, config: Any, control: Any):
        self.db = db
        self.config = config
        self.control = control
        self._lock = threading.Lock()
        self._endpoint_lock = threading.Lock()
        self._key_cache: tuple[float, str] = (0.0, "")
        self._endpoint_cache: tuple[float, str, tuple[int, ...], int] = (
            0.0,
            "",
            (),
            0,
        )

    # ---------------------------------------------------------------- helpers

    def _current(self) -> Any:
        return self.config.current()

    def _endpoint(self) -> tuple[str, int]:
        head = self.control._head()
        ports = [int(port) for port in self._current().model_ports] or [8888]
        host = str(head["host"])
        signature = tuple(ports)
        now = time.time()
        with self._endpoint_lock:
            expires_at, cached_host, cached_ports, cached_port = self._endpoint_cache
            if (
                cached_port
                and now < expires_at
                and cached_host == host
                and cached_ports == signature
            ):
                return cached_host, cached_port
        for port in ports:
            if self._endpoint_ready(host, port):
                with self._endpoint_lock:
                    self._endpoint_cache = (
                        now + ENDPOINT_TTL_SECONDS,
                        host,
                        signature,
                        port,
                    )
                return host, port
        raise ChatError(
            f"无法连接模型服务 {host}，已探测端口: {', '.join(map(str, ports))}"
        )

    def _model_scheme(self) -> str:
        scheme = str(getattr(self._current(), "model_scheme", "http") or "http").lower()
        return scheme if scheme in ("http", "https") else "http"

    def _endpoint_ready(self, host: str, port: int) -> bool:
        path = str(
            getattr(self._current(), "model_health_path", "/health") or "/health"
        )
        if not path.startswith("/"):
            path = "/" + path
        url = f"{self._model_scheme()}://{host}:{port}{path}"
        try:
            with urllib.request.urlopen(url, timeout=1.5) as response:
                return int(response.status) < 500
        except urllib.error.HTTPError as exc:
            return exc.code in (401, 403)
        except (urllib.error.URLError, TimeoutError, OSError):
            return False

    def _running_containers(self) -> list[str]:
        output = self.control._run(
            ["bash", "-lc", "docker ps --format '{{.Names}}' 2>/dev/null"], timeout=20.0
        )
        return [line.strip() for line in output.splitlines() if line.strip()]

    def _api_key(self, api_key: str | None = None) -> str:
        if api_key is not None:
            value = str(api_key).strip()
            if len(value) < 8:
                raise ChatError("手动 Key 至少需要 8 个字符")
            if len(value) > 256:
                raise ChatError("手动 Key 过长（最多 256 字符）")
            return value
        now = time.time()
        with self._lock:
            cached_at, cached_key = self._key_cache
            if cached_key and now - cached_at < KEY_TTL_SECONDS:
                return cached_key
        return self._read_container_key()

    def _read_container_key(self) -> str:
        containers = self._running_containers()
        if not containers:
            raise ChatError("HEAD 节点上没有运行中的模型容器")
        for name in containers:
            quoted = shlex.quote(name)
            # 1) Docker secret (docker compose secret file)
            try:
                output = self.control._run(
                    [
                        "bash",
                        "-lc",
                        f"docker exec {quoted} cat /run/secrets/model-api-key 2>/dev/null",
                    ],
                    timeout=15.0,
                )
            except SSHError:
                output = ""
            lines = (output or "").strip().splitlines()
            if lines and lines[-1].strip():
                return self._remember_key(lines[-1].strip())
            # 2) Container environment (docker run -e VLLM_API_KEY=...)
            try:
                env_output = self.control._run(
                    [
                        "bash",
                        "-lc",
                        f"docker inspect {quoted} --format "
                        "'{{range .Config.Env}}{{println .}}{{end}}'",
                    ],
                    timeout=15.0,
                )
            except SSHError:
                continue
            for raw in (env_output or "").splitlines():
                line = raw.strip()
                for prefix in ("VLLM_API_KEY=", "OPENAI_API_KEY=", "API_KEY="):
                    if line.startswith(prefix):
                        value = line[len(prefix):].strip()
                        if len(value) >= 8:
                            return self._remember_key(value)
        raise ChatError("未能读取模型 API Key（容器 secret 与环境变量中均未找到）")

    def _remember_key(self, value: str) -> str:
        with self._lock:
            self._key_cache = (time.time(), value)
        return value

    def _request(
        self,
        path: str,
        payload: dict[str, Any] | None = None,
        stream: bool = False,
        api_key: str | None = None,
    ):
        host, port = self._endpoint()
        if api_key is not None:
            key = self._api_key(api_key)
        else:
            try:
                key = self._api_key()
            except ChatError:
                key = ""
        url = f"{self._model_scheme()}://{host}:{port}{path}"
        headers = {"Accept": "application/json"}
        if key:
            headers["Authorization"] = f"Bearer {key}"
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
            lowered = detail.lower()
            if "multimodal" in lowered:
                raise ChatError(
                    "当前模型不支持图片输入（非多模态模型），请改用文本文件或切换到多模态模型。"
                ) from exc
            if "maximum context" in lowered or "too long" in lowered:
                raise ChatError("输入超出模型上下文长度，请缩短消息或减少附件。") from exc
            raise ChatError(f"模型服务返回 {exc.code}: {detail[:300]}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ChatError(f"无法连接模型服务 {host}:{port}（{exc}）") from exc

    # ------------------------------------------------------------- public API

    def key_info(self) -> dict[str, Any]:
        container_key = ""
        container_error = ""
        try:
            container_key = self._read_container_key()
        except Exception as exc:  # noqa: BLE001 - detection must not break manual entry
            container_key = ""
            container_error = str(exc) or exc.__class__.__name__
        effective = container_key
        host, port = "", 0
        models: list[str] = []
        endpoint_error = ""
        try:
            host, port = self._endpoint()
            models = self.info().get("models", [])
        except Exception as exc:  # noqa: BLE001 - detection must not break manual entry
            endpoint_error = str(exc) or exc.__class__.__name__
        model = models[0] if models else ""
        endpoint = f"{self._model_scheme()}://{host}:{port}" if host else ""
        curl = ""
        if endpoint:
            body = (
                '{"model": "%s", "messages": [{"role": "user", "content": "你好"}], '
                '"stream": true}' % model
            )
            curl = (
                f"curl -s {endpoint}/v1/chat/completions \\\n"
                f'  -H "Content-Type: application/json" \\\n'
                f'  -H "Authorization: Bearer {effective}" \\\n'
                f"  -d '{body}'"
            )
        return {
            "key": effective,
            "has_key": bool(effective),
            "source": "container" if container_key else "",
            "container_key": container_key,
            "endpoint": endpoint,
            "model": model,
            "models": models,
            "curl": curl,
            "error": container_error or endpoint_error,
        }

    def info(self, api_key: str | None = None) -> dict[str, Any]:
        host, port = "", 0
        try:
            host, port = self._endpoint()
            response = self._request("/v1/models", api_key=api_key)
            payload = json.loads(response.read().decode("utf-8", "replace") or "{}")
            # vLLM lists both --served-model-name and the model path basename for
            # one deployment; collapse entries that share the same root.
            models: list[str] = []
            aliases: dict[str, list[str]] = {}
            seen: dict[str, str] = {}
            for item in payload.get("data", []):
                if not isinstance(item, dict) or not item.get("id"):
                    continue
                model_id = str(item["id"])
                root = str(item.get("root") or model_id)
                if root in seen:
                    aliases.setdefault(seen[root], []).append(model_id)
                    continue
                seen[root] = model_id
                models.append(model_id)
            return {
                "available": True,
                "endpoint": f"{self._model_scheme()}://{host}:{port}",
                "models": models,
                "aliases": aliases,
                "error": "",
            }
        except ChatError as exc:
            return {
                "available": False,
                "endpoint": f"{self._model_scheme()}://{host}:{port}" if host else "",
                "models": [],
                "error": str(exc),
            }

    def chat(
        self,
        body: dict[str, Any],
        on_delta: Any = None,
        on_reasoning: Any = None,
        api_key: str | None = None,
    ) -> dict[str, Any]:
        raw_messages = body.get("messages")
        if not isinstance(raw_messages, list) or not raw_messages:
            raise ChatError("消息不能为空")
        messages = []
        for item in raw_messages[-MAX_MESSAGES:]:
            if not isinstance(item, dict):
                continue
            role = str(item.get("role") or "").strip()
            content = _normalize_content(item.get("content"))
            if role not in ("system", "user", "assistant") or not content:
                continue
            messages.append({"role": role, "content": content})
        if not messages:
            raise ChatError("没有有效的对话消息")

        model = str(body.get("model") or "").strip()
        payload: dict[str, Any] = {
            "messages": messages,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        # max_tokens is optional: omit it so the engine uses its own limit.
        raw_max = body.get("max_tokens")
        if raw_max not in (None, "", 0):
            try:
                max_tokens = int(raw_max)
            except (TypeError, ValueError):
                max_tokens = 0
            if max_tokens > 0:
                payload["max_tokens"] = max(1, min(131072, max_tokens))
        raw_temperature = body.get("temperature")
        if raw_temperature not in (None, ""):
            try:
                temperature = float(raw_temperature)
            except (TypeError, ValueError):
                temperature = None
            if temperature is not None:
                payload["temperature"] = max(0.0, min(2.0, temperature))
        if model:
            payload["model"] = model
        # Thinking is on unless explicitly disabled.
        enable_thinking = body.get("enable_thinking")
        if enable_thinking is None:
            enable_thinking = True
        # Explicit switch: some chat templates default thinking to off.
        payload["chat_template_kwargs"] = {"enable_thinking": bool(enable_thinking)}

        started = time.perf_counter()
        ttft_ms: float | None = None
        parts: list[str] = []
        reasoning: list[str] = []
        usage: dict[str, Any] = {}
        finish_reason = ""
        chunks = 0

        response = self._request(
            "/v1/chat/completions",
            payload,
            stream=True,
            api_key=api_key,
        )
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
                    if on_delta is not None:
                        on_delta(str(piece))
                if reasoning_piece:
                    reasoning.append(str(reasoning_piece))
                    if on_reasoning is not None:
                        on_reasoning(str(reasoning_piece))
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
