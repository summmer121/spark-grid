"""Model service lifecycle control on the cluster head node.

Lists systemd units named ``model_*.service`` on the head node and supports
starting, stopping and switching them.  The model units are ``Type=oneshot``
with execution times from minutes to tens of minutes, so every mutating action
runs in a background thread and exposes a task record the UI can poll.

Service phases are derived from three layers instead of the bare systemd
ActiveState:

* systemd: ActiveState / SubState / Result / ExecMainStatus / timestamps
* container: whether any docker container is running on the head node
* health: ``GET /health`` on the configured model ports

That distinguishes "starting (with elapsed vs timeout)", "ready", "warming
(container up, engine still loading)", "completed (script finished but no
container)" and "failed (exit code / timeout / signal)".
"""

from __future__ import annotations

import re
import threading
import time
import uuid
from datetime import datetime
from typing import Any

from .ssh_runner import SSHError, run_remote_command

UNIT_RE = re.compile(r"^model_[A-Za-z0-9][A-Za-z0-9._-]*\.service$")
GUARD_UNITS = (
    "model-glm53-guard.service",
    "spark-ds41f-guard.service",
)
GUARD_STOPWORDS = {"model", "guard", "service", "spark"}

LIST_SCRIPT = r"""
set -u
units=$(systemctl list-unit-files 'model_*.service' --no-pager --plain 2>/dev/null | awk '$1 ~ /\.service$/ {print $1}')
echo "NOW:$(date +%%s)"
if [ -z "$units" ]; then echo "NO_UNITS"; exit 0; fi
for u in $units; do
  echo "UNIT:$u"
  systemctl show -p ActiveState -p SubState -p UnitFileState -p Description \
    -p Result -p ExecMainStatus -p ExecMainStartTimestamp -p ExecMainExitTimestamp \
    -p TimeoutStartUSec -p NRestarts -p StatusText "$u" 2>/dev/null
  echo "END_UNIT"
done
echo "CONTAINERS"
docker ps --format '{{.Names}}|{{.State}}' 2>/dev/null | head -80
echo "END_CONTAINERS"
echo "PORTS"
for p in %s; do
  code=$(curl -s -o /dev/null -m 2 -w '%%{http_code}' "http://127.0.0.1:$p/health" 2>/dev/null || true)
  echo "HEALTH:$p:$code"
done
echo "END_PORTS"
"""

GUARD_LIST_SCRIPT = r"""
for g in %s; do
  if systemctl list-unit-files "$g" --no-pager --plain 2>/dev/null | grep -q "^$g"; then
    echo "GUARD:$g"
    systemctl show -p ActiveState -p SubState "$g" 2>/dev/null
    echo "END_GUARD"
  fi
done
""" % " ".join(GUARD_UNITS)

START_TIMEOUT = 7200.0
STOP_TIMEOUT = 900.0
LIST_TIMEOUT = 90.0

PHASE_LABELS = {
    "starting": "启动中",
    "ready": "已就绪",
    "warming": "引擎加载中",
    "completed": "脚本已完成",
    "failed": "启动失败",
    "stopping": "停止中",
    "stopped": "已停止",
}

RESULT_HINTS = {
    "exit-code": "启动脚本非零退出",
    "timeout": "超过启动超时被终止",
    "signal": "被信号终止",
    "core-dump": "核心转储",
    "watchdog": "看门狗超时",
    "start-limit-hit": "触发启动频率限制",
}


class ModelControlError(RuntimeError):
    """Raised when a model service action cannot be performed."""


def _tokens(name: str) -> set[str]:
    return {
        token
        for token in re.split(r"[^a-z0-9]+", name.lower())
        if len(token) >= 3 and token not in GUARD_STOPWORDS
    }


def _human_seconds(value: float | int | None) -> str:
    if value is None:
        return "—"
    total = max(0, int(value))
    hours, rest = divmod(total, 3600)
    minutes, seconds = divmod(rest, 60)
    if hours:
        return f"{hours}h{minutes:02d}m"
    if minutes:
        return f"{minutes}m{seconds:02d}s"
    return f"{seconds}s"


def _parse_duration_usec(raw: str) -> float | None:
    """Parse systemd duration strings such as '3000s', '50min', '1h 30min'."""
    text = (raw or "").strip()
    if not text or text.lower() in ("infinity", "0"):
        return None
    total = 0.0
    matched = False
    for value, unit in re.findall(r"(\d+(?:\.\d+)?)\s*([a-z]+)", text):
        factor = {
            "us": 1e-6,
            "usec": 1e-6,
            "ms": 1e-3,
            "msec": 1e-3,
            "s": 1.0,
            "sec": 1.0,
            "second": 1.0,
            "seconds": 1.0,
            "min": 60.0,
            "minute": 60.0,
            "minutes": 60.0,
            "h": 3600.0,
            "hr": 3600.0,
            "hour": 3600.0,
            "hours": 3600.0,
            "d": 86400.0,
            "day": 86400.0,
            "days": 86400.0,
        }.get(unit)
        if factor is None:
            continue
        matched = True
        total += float(value) * factor
    return total if matched else None


def _parse_timestamp(raw: str) -> float | None:
    """Parse 'Tue 2026-11-24 16:01:23 CST' style timestamps."""
    text = (raw or "").strip()
    if not text or text.lower() in ("n/a", "none"):
        return None
    match = re.search(
        r"(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2}:\d{2})(?:\.\d+)?", text
    )
    if not match:
        return None
    try:
        return datetime.strptime(
            f"{match.group(1)} {match.group(2)}", "%Y-%m-%d %H:%M:%S"
        ).timestamp()
    except ValueError:
        return None


class ModelControl:
    def __init__(self, db: Any, config: Any):
        self.db = db
        self.config = config
        self._lock = threading.Lock()
        self._task: dict[str, Any] | None = None

    # ---------------------------------------------------------------- helpers

    def _current(self) -> Any:
        return self.config.current()

    def _head(self) -> dict[str, Any]:
        nodes = sorted(self.db.list_nodes(), key=lambda node: int(node["id"]))
        if not nodes:
            raise ModelControlError("没有可用节点，无法定位集群 HEAD")
        node = dict(nodes[0])
        credentials = self.db.get_node_credentials(int(node["id"]))
        if not credentials:
            raise ModelControlError("HEAD 节点 SSH 凭据缺失，请先在节点页面配置")
        node.update(credentials)
        return node

    def _run(self, args: list[str], timeout: float) -> str:
        return run_remote_command(self._head(), self._current(), args, timeout=timeout)

    def _validate_unit(self, unit: str) -> str:
        value = str(unit or "").strip()
        if not UNIT_RE.fullmatch(value):
            raise ModelControlError("非法的模型服务单元名")
        return value

    def _list_script(self) -> str:
        ports = " ".join(str(port) for port in self._current().model_ports) or "8888"
        return LIST_SCRIPT % ports

    # ------------------------------------------------------------- parsing

    def _parse_units(self, output: str) -> list[dict[str, Any]]:
        """Parse per-unit key=value blocks (kept for the plain listing path)."""
        services: list[dict[str, Any]] = []
        current: dict[str, Any] | None = None
        for raw in output.splitlines():
            line = raw.strip()
            if not line:
                continue
            if line.startswith("UNIT:"):
                current = {"unit": line[5:].strip()}
                continue
            if line == "END_UNIT":
                if current and current.get("unit"):
                    unit = current["unit"]
                    current["name"] = (
                        unit[: -len(".service")] if unit.endswith(".service") else unit
                    )
                    current["active_state"] = current.get("ActiveState", "unknown")
                    current["sub_state"] = current.get("SubState", "unknown")
                    current["unit_file_state"] = current.get("UnitFileState", "unknown")
                    current["description"] = current.get("Description", "")
                    current["active"] = current["active_state"] in (
                        "active",
                        "activating",
                        "reloading",
                        "deactivating",
                    )
                    services.append(current)
                current = None
                continue
            if current is None or "=" not in line:
                continue
            key, value = line.split("=", 1)
            current[key.strip()] = value.strip()
        services.sort(key=lambda item: item["unit"])
        return services

    def _collect(
        self, output: str
    ) -> tuple[list[dict[str, Any]], float, list[str], dict[int, int]]:
        services: list[dict[str, Any]] = []
        containers: list[str] = []
        health: dict[int, int] = {}
        server_now = time.time()
        current: dict[str, Any] | None = None
        section = "unit"
        for raw in output.splitlines():
            line = raw.strip()
            if not line:
                continue
            if line.startswith("NOW:"):
                try:
                    server_now = float(line[4:])
                except ValueError:
                    pass
                continue
            if line.startswith("UNIT:"):
                current = {"unit": line[5:].strip()}
                section = "unit"
                continue
            if line == "END_UNIT":
                if current and current.get("unit"):
                    services.append(current)
                current = None
                continue
            if line == "CONTAINERS":
                section = "containers"
                continue
            if line == "END_CONTAINERS":
                section = "unit"
                continue
            if line == "PORTS":
                section = "ports"
                continue
            if line == "END_PORTS":
                section = "unit"
                continue
            if section == "containers":
                if "|" in line:
                    containers.append(line)
                continue
            if section == "ports":
                if line.startswith("HEALTH:") and line.count(":") == 2:
                    _, port, code = line.split(":", 2)
                    if port.isdigit():
                        try:
                            health[int(port)] = int(code)
                        except ValueError:
                            pass
                continue
            if current is not None and "=" in line:
                key, value = line.split("=", 1)
                current[key.strip()] = value.strip()
        return services, server_now, containers, health

    def _classify(
        self,
        service: dict[str, Any],
        server_now: float,
        containers: list[str],
        health: dict[int, int],
        ports: list[int],
    ) -> dict[str, Any]:
        unit = str(service.get("unit") or "")
        active_state = str(service.get("ActiveState") or "unknown")
        sub_state = str(service.get("SubState") or "unknown")
        result = str(service.get("Result") or "")
        try:
            exec_status = int(service.get("ExecMainStatus") or 0)
        except ValueError:
            exec_status = 0
        started_at = _parse_timestamp(service.get("ExecMainStartTimestamp", ""))
        exited_at = _parse_timestamp(service.get("ExecMainExitTimestamp", ""))
        timeout_seconds = _parse_duration_usec(service.get("TimeoutStartUSec", ""))

        elapsed: float | None = None
        if started_at and active_state in ("activating", "active", "failed"):
            end = server_now if active_state in ("activating", "active") else (exited_at or server_now)
            elapsed = max(0.0, end - started_at)

        healthy_port = next((port for port in ports if health.get(port) == 200), None)

        phase = "stopped"
        detail = ""
        error_hint = ""
        progress: float | None = None

        if active_state == "activating":
            phase = "starting"
            if elapsed is not None:
                detail = f"脚本运行 {_human_seconds(elapsed)}"
                if timeout_seconds:
                    detail += f" / 上限 {_human_seconds(timeout_seconds)}"
                    progress = min(100.0, round(elapsed / timeout_seconds * 100, 1))
                    if elapsed >= timeout_seconds * 0.85:
                        error_hint = "已接近启动超时上限"
                else:
                    detail += " / 上限 未知"
            else:
                detail = "启动脚本运行中"
            if not containers:
                detail += " · 暂无容器"
        elif active_state == "active":
            if healthy_port:
                phase = "ready"
                detail = f"{healthy_port} /health 正常"
            elif containers:
                phase = "warming"
                detail = f"{len(containers)} 个容器运行中，等待 /health"
            else:
                phase = "completed"
                detail = "启动脚本已完成，未检测到运行中的容器"
                error_hint = "脚本成功但容器不在，可能已被手动停止"
        elif active_state == "failed":
            phase = "failed"
            base = RESULT_HINTS.get(result, "启动失败")
            if result == "exit-code":
                base = f"启动脚本退出码 {exec_status}"
            detail = base
            if elapsed is not None:
                detail += f" · 运行 {_human_seconds(elapsed)}"
            status_text = str(service.get("StatusText") or "").strip()
            if status_text:
                detail += f" · {status_text[:80]}"
            error_hint = detail
        elif active_state == "deactivating":
            phase = "stopping"
            detail = "正在停止"
        else:
            phase = "stopped"
            detail = "未运行"

        service.update(
            {
                "name": unit[: -len(".service")] if unit.endswith(".service") else unit,
                "description": str(service.get("Description") or ""),
                "active_state": active_state,
                "sub_state": sub_state,
                "unit_file_state": str(service.get("UnitFileState") or "unknown"),
                "active": active_state in ("active", "activating", "reloading", "deactivating"),
                "phase": phase,
                "phase_label": PHASE_LABELS.get(phase, phase),
                "phase_detail": detail,
                "error_hint": error_hint,
                "progress": progress,
                "elapsed_seconds": int(elapsed) if elapsed is not None else None,
                "timeout_seconds": int(timeout_seconds) if timeout_seconds else None,
                "result": result,
                "exec_main_status": exec_status,
                "container_count": len(containers),
                "healthy_port": healthy_port,
            }
        )
        return service

    def _guards(self) -> list[dict[str, Any]]:
        output = self._run(["bash", "-lc", GUARD_LIST_SCRIPT], timeout=30.0)
        return self._parse_guards(output)

    def _parse_guards(self, output: str) -> list[dict[str, Any]]:
        guards: list[dict[str, Any]] = []
        current: dict[str, Any] | None = None
        for raw in output.splitlines():
            line = raw.strip()
            if line.startswith("GUARD:"):
                if current is not None:
                    guards.append(current)
                current = {"unit": line[6:].strip()}
                continue
            if line == "END_GUARD":
                if current is not None:
                    guards.append(current)
                current = None
                continue
            if current is not None and "=" in line:
                key, value = line.split("=", 1)
                if key.strip() == "ActiveState":
                    current["active_state"] = value.strip()
                elif key.strip() == "SubState":
                    current["sub_state"] = value.strip()
        if current is not None:
            guards.append(current)
        return guards

    def _related_guards(
        self,
        target: str,
        guards: list[dict[str, Any]],
        dependencies: list[str] | None = None,
    ) -> list[str]:
        target_tokens = _tokens(target)
        deps = set(dependencies or [])
        related = []
        for guard in guards:
            unit = str(guard["unit"])
            if unit in deps or target_tokens & _tokens(unit):
                related.append(unit)
        return related

    def _target_guard_deps(self, target: str) -> list[str]:
        """Guards referenced by the target unit's ordering/requirement directives."""
        try:
            output = self._run(
                [
                    "bash",
                    "-lc",
                    f"systemctl show -p Before -p After -p Wants -p Requires {target} 2>/dev/null",
                ],
                timeout=15.0,
            )
        except SSHError:
            return []
        names: set[str] = set()
        for line in output.splitlines():
            if "=" not in line:
                continue
            _, value = line.split("=", 1)
            for token in value.split():
                if token.endswith("-guard.service"):
                    names.add(token)
        return sorted(names)

    def _task_snapshot(self) -> dict[str, Any] | None:
        with self._lock:
            return dict(self._task) if self._task else None

    def _step(self, task: dict[str, Any], message: str) -> None:
        entry = f"[{time.strftime('%H:%M:%S')}] {message}"
        with self._lock:
            task["message"] = message
            task.setdefault("steps", []).append(entry)
            if len(task["steps"]) > 60:
                del task["steps"][:-60]

    # ------------------------------------------------------------- public API

    def list_services(self) -> dict[str, Any]:
        try:
            head = self._head()
        except ModelControlError:
            return {
                "available": False,
                "error": "缺少 HEAD 节点或 SSH 凭据",
                "services": [],
                "guards": [],
                "task": self._task_snapshot(),
            }
        ports = list(self._current().model_ports)
        try:
            output = self._run(["bash", "-lc", self._list_script()], timeout=LIST_TIMEOUT)
            raw_services, server_now, containers, health = self._collect(output)
            guards = self._guards()
        except SSHError as exc:
            return {
                "available": False,
                "error": str(exc)[-300:],
                "head": {"name": head.get("name"), "host": head.get("host")},
                "services": [],
                "guards": [],
                "task": self._task_snapshot(),
            }
        services = [
            self._classify(service, server_now, containers, health, ports)
            for service in raw_services
        ]
        services.sort(key=lambda item: item["unit"])
        return {
            "available": True,
            "error": "",
            "head": {"name": head.get("name"), "host": head.get("host")},
            "prefix": "model_",
            "services": services,
            "guards": guards,
            "task": self._task_snapshot(),
            "containers": containers[:20],
            "generated_at": int(server_now),
        }

    def submit(
        self,
        action: str,
        unit: str,
        user: dict[str, Any] | None = None,
        remote_addr: str = "",
    ) -> dict[str, Any]:
        unit = self._validate_unit(unit)
        if action not in ("start", "stop", "switch"):
            raise ModelControlError("未知操作")
        if not self.db.list_nodes():
            raise ModelControlError("没有可用节点，无法操作模型服务")
        with self._lock:
            if self._task and self._task.get("status") == "running":
                raise ModelControlError(
                    f"已有任务进行中：{self._task.get('action')} {self._task.get('unit')}"
                )
            task: dict[str, Any] = {
                "id": uuid.uuid4().hex[:12],
                "action": action,
                "unit": unit,
                "status": "running",
                "started_at": int(time.time()),
                "finished_at": None,
                "message": "任务已提交",
                "steps": [],
                "user": user.get("username") if user else None,
            }
            self._task = task
        if user is not None:
            try:
                self.db.add_audit(
                    user,
                    f"model-{action}",
                    unit,
                    {
                        "start": "启动模型服务",
                        "stop": "停止模型服务",
                        "switch": "切换模型服务",
                    }[action],
                    remote_addr,
                )
            except Exception:  # noqa: BLE001 - audit must not block the action
                pass
        thread = threading.Thread(
            target=self._execute, args=(task,), name=f"model-{action}", daemon=True
        )
        thread.start()
        return {"ok": True, "task": dict(task)}

    def _execute(self, task: dict[str, Any]) -> None:
        unit = task["unit"]
        action = task["action"]
        try:
            if action == "start":
                self._step(task, f"启动 {unit} …")
                self._systemctl("start", unit, START_TIMEOUT)
                self._step(task, f"{unit} 启动完成")
            elif action == "stop":
                self._step(task, f"停止 {unit} …")
                self._systemctl("stop", unit, STOP_TIMEOUT)
                self._step(task, f"{unit} 已停止")
            else:
                self._switch(task, unit)
            with self._lock:
                task["status"] = "done"
                task["finished_at"] = int(time.time())
                task["message"] = task["message"] + "（完成）"
        except Exception as exc:  # noqa: BLE001 - surface the failure to the UI
            with self._lock:
                task["status"] = "failed"
                task["finished_at"] = int(time.time())
                task["message"] = str(exc)[-500:] or "操作失败"

    def _systemctl(self, verb: str, unit: str, timeout: float) -> str:
        return self._run(["sudo", "-n", "systemctl", verb, unit], timeout=timeout)

    def _switch(self, task: dict[str, Any], target: str) -> None:
        guards = self._guards()
        related = self._related_guards(
            target, guards, self._target_guard_deps(target)
        )

        # 1. Stop task guards so they do not fence/restart the model we replace.
        for guard in guards:
            if str(guard.get("active_state")) in ("active", "activating", "reloading"):
                self._step(task, f"停止守护单元 {guard['unit']}")
                try:
                    self._systemctl("stop", str(guard["unit"]), 120.0)
                except SSHError as exc:
                    self._step(task, f"守护单元停止失败（继续）：{str(exc)[-160:]}")

        # 2. Stop every other running model service.
        services = self.list_services().get("services", [])
        for service in services:
            unit = str(service["unit"])
            if unit == target:
                continue
            if str(service.get("active_state")) in (
                "active",
                "activating",
                "reloading",
                "deactivating",
            ):
                self._step(task, f"停止 {unit} …")
                self._systemctl("stop", unit, STOP_TIMEOUT)

        # 3. Start the target model service.
        self._step(task, f"启动 {target} …")
        self._systemctl("start", target, START_TIMEOUT)
        self._step(task, f"{target} 启动完成")

        # 4. Re-arm guards that belong to the newly started model.
        for guard in related:
            self._step(task, f"恢复守护单元 {guard}")
            try:
                self._systemctl("start", guard, 120.0)
            except SSHError as exc:
                self._step(task, f"守护单元启动失败：{str(exc)[-160:]}")
