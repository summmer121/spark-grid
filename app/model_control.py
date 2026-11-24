"""Model service lifecycle control on the cluster head node.

Lists systemd units named ``model_*.service`` on the head node and supports
starting, stopping and switching them.  The model units are ``Type=oneshot``
with execution times from minutes to tens of minutes, so every mutating action
runs in a background thread and exposes a task record the UI can poll.
"""

from __future__ import annotations

import re
import threading
import time
import uuid
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
if [ -z "$units" ]; then echo "NO_UNITS"; exit 0; fi
for u in $units; do
  echo "UNIT:$u"
  systemctl show -p ActiveState -p SubState -p UnitFileState -p Description "$u" 2>/dev/null
  echo "END_UNIT"
done
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
LIST_TIMEOUT = 60.0


class ModelControlError(RuntimeError):
    """Raised when a model service action cannot be performed."""


def _tokens(name: str) -> set[str]:
    return {
        token
        for token in re.split(r"[^a-z0-9]+", name.lower())
        if len(token) >= 3 and token not in GUARD_STOPWORDS
    }


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

    def _parse_units(self, output: str) -> list[dict[str, Any]]:
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
                    current["name"] = unit[: -len(".service")] if unit.endswith(".service") else unit
                    current["active_state"] = current.get("active_state", "unknown")
                    current["sub_state"] = current.get("sub_state", "unknown")
                    current["unit_file_state"] = current.get("unit_file_state", "unknown")
                    current["description"] = current.get("description", "")
                    current["active"] = current["active_state"] in (
                        "active",
                        "activating",
                        "reloading",
                    )
                    services.append(current)
                current = None
                continue
            if current is None or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip()
            if key == "ActiveState":
                current["active_state"] = value
            elif key == "SubState":
                current["sub_state"] = value
            elif key == "UnitFileState":
                current["unit_file_state"] = value
            elif key == "Description":
                current["description"] = value
        services.sort(key=lambda item: item["unit"])
        return services

    def _list_units(self) -> list[dict[str, Any]]:
        output = self._run(["bash", "-lc", LIST_SCRIPT], timeout=LIST_TIMEOUT)
        if "NO_UNITS" in output and "UNIT:" not in output:
            return []
        return self._parse_units(output)

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
        try:
            services = self._list_units()
            guards = self._guards()
            error = ""
        except SSHError as exc:
            return {
                "available": False,
                "error": str(exc)[-300:],
                "head": {"name": head.get("name"), "host": head.get("host")},
                "services": [],
                "guards": [],
                "task": self._task_snapshot(),
            }
        return {
            "available": True,
            "error": error,
            "head": {"name": head.get("name"), "host": head.get("host")},
            "prefix": "model_",
            "services": services,
            "guards": guards,
            "task": self._task_snapshot(),
            "generated_at": int(time.time()),
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
        services = self._list_units()
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
