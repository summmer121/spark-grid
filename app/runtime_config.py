from __future__ import annotations

import os
import re
import string
from dataclasses import dataclass
from typing import Any, Callable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .command_line import command_text
from .config import (
    NETWORK_INTERFACE_EXCLUDE_PREFIXES,
    SSH_HOST_KEY_POLICIES,
    Settings,
    system_timezone_name,
)
from .db import Database


TypedValue = int | float | bool | str | list[int] | list[str]
Validator = Callable[[Any], TypedValue]


CONFIG_GROUPS = [
    ("alerts", "告警阈值", "控制指标告警和离线判定。"),
    ("ssh", "SSH 默认值", "新建节点和扫描时使用的默认 SSH 参数，节点自身配置优先。"),
    ("nodes", "节点与分组", "节点命名、默认分组和自动导入规则。"),
    ("discovery", "局域网扫描", "扫描范围由页面选择的网卡或 CIDR 决定。"),
    ("collector", "指标采集", "中心节点主动通过 SSH 拉取指标，不依赖远端 Agent。"),
    ("model", "模型监控", "模型端口、容器匹配、超时、缓存和曲线窗口。"),
    ("interface", "界面刷新", "控制页面轮询间隔和历史曲线范围。"),
    ("storage", "存储与数据库", "控制中心数据库在并发写入时的锁等待时间。"),
    ("security", "安全与会话", "管理员会话和登录失败限制。"),
]


@dataclass(frozen=True)
class ConfigField:
    key: str
    label: str
    description: str
    group: str
    field_type: str
    default: TypedValue
    minimum: float | int | None = None
    maximum: float | int | None = None
    unit: str = ""
    placeholder: str = ""
    max_length: int = 200
    max_items: int = 128

    def metadata(self, value: TypedValue) -> dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "description": self.description,
            "group": self.group,
            "type": self.field_type,
            "value": value,
            "default": list(self.default)
            if isinstance(self.default, (list, tuple))
            else self.default,
            "min": self.minimum,
            "max": self.maximum,
            "unit": self.unit,
            "placeholder": self.placeholder,
            "max_length": self.max_length,
            "max_items": self.max_items,
        }


@dataclass(frozen=True)
class EffectiveConfig:
    root_dir: Any
    data_dir: Any
    static_dir: Any
    host: str
    port: int
    cookie_secure: bool
    database_timeout_seconds: float
    cpu_warning: int
    memory_warning: int
    gpu_warning: int
    temperature_warning: int
    offline_seconds: int
    ssh_user: str
    ssh_port: int
    ssh_key_path: str
    ssh_remote_python: str
    gpu_query_command: str
    ssh_timeout: float
    ssh_connect_timeout: float
    ssh_control_persist: int
    ssh_strict_host_key_checking: str
    ssh_bin: str
    ssh_known_hosts_path: str
    ssh_interface_query_timeout_seconds: float
    ssh_gpu_info_timeout_seconds: float
    ssh_gpu_metrics_timeout_seconds: float
    ssh_cpu_sample_interval_seconds: float
    node_name_pattern: str
    default_group_name: str
    default_tags: list[str]
    max_tags_per_node: int
    auto_import_group_name: str
    auto_import_tags: list[str]
    bulk_add_max_nodes: int
    discovery_auto_enabled: bool
    discovery_initial_delay_seconds: float
    discovery_interval_seconds: float
    discovery_port_concurrency: int
    discovery_port_timeout_seconds: float
    discovery_max_hosts: int
    discovery_ssh_concurrency: int
    discovery_result_retention_seconds: int
    discovery_saved_lookback_seconds: int
    discovery_reuse_saved_ports: bool
    poll_interval: float
    collector_concurrency: int
    network_interface_exclude_prefixes: list[str]
    network_route_probe_host: str
    offline_failures: int
    history_points: int
    history_retention_hours: int
    prune_interval_seconds: float
    session_ttl_hours: int
    login_max_attempts: int
    login_window_seconds: int
    dashboard_refresh_seconds: float
    model_scheme: str
    model_default_port: int
    model_ports: list[int]
    model_container_hints: list[str]
    model_engine_hints: list[str]
    model_container_runtime: str
    model_health_path: str
    model_metrics_path: str
    model_timezone: str
    model_sample_interval_seconds: float
    model_cluster_concurrency: int
    model_http_timeout_seconds: float
    model_metrics_timeout_seconds: float
    model_ssh_timeout_seconds: float
    model_log_timeout_seconds: float
    model_overview_log_lines: int
    model_log_lines: int
    model_log_max_lines: int
    model_log_all_limit: int
    model_cache_overview_seconds: float
    model_cache_runtime_seconds: float
    model_cache_container_seconds: float
    model_cache_inspect_seconds: float
    model_cache_logs_seconds: float
    model_history_minutes: int
    model_history_buckets: int
    model_health_concurrency: int
    gpu_power_estimate_enabled: bool
    gpu_power_estimate_names: list[str]
    gpu_power_estimate_idle_watts: float
    gpu_power_estimate_rated_watts: float
    ui_history_minutes: int
    ui_history_points: int
    ui_history_ranges: list[int]
    ui_cluster_history_refresh_seconds: float
    ui_timezone: str
    ui_model_log_refresh_seconds: float
    ui_model_log_lines: int
    ui_gpu_fallback_name: str


def _env_text(name: str, default: str) -> str:
    return os.getenv(name, default).strip()


def _env_text_or_default(name: str, default: str) -> str:
    return os.getenv(name, "").strip() or default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    try:
        return _bool_value(os.getenv(name, "1" if default else "0"))
    except ValueError:
        return default


def _env_list(name: str, default: str) -> list[str]:
    return [
        item.strip()
        for item in re.split(r"[,;\s]+", os.getenv(name, default))
        if item.strip()
    ]


def _bool_value(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in {0, 1}:
        return bool(value)
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "on", "enabled"}:
        return True
    if normalized in {"0", "false", "no", "off", "disabled"}:
        return False
    raise ValueError("必须是开或关")


def _int_value(value: Any) -> int:
    if isinstance(value, bool):
        raise ValueError("必须是整数")
    if isinstance(value, float) and not value.is_integer():
        raise ValueError("必须是整数")
    try:
        return int(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise ValueError("必须是整数") from exc


def _float_value(value: Any) -> float:
    if isinstance(value, bool):
        raise ValueError("必须是数字")
    try:
        return float(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise ValueError("必须是数字") from exc


def _list_values(value: Any) -> list[Any]:
    if isinstance(value, (list, tuple)):
        return list(value)
    normalized = str(value or "").strip()
    if not normalized:
        return []
    return [item.strip() for item in re.split(r"[,;\s]+", normalized) if item.strip()]


def _string_value(max_length: int) -> Validator:
    def validate(value: Any) -> str:
        result = str(value if value is not None else "").strip()
        if len(result) > max_length:
            raise ValueError(f"长度不能超过 {max_length} 个字符")
        return result

    return validate


def _list_str_value(value: Any, max_items: int = 128) -> list[str]:
    result = []
    for item in _list_values(value):
        text = str(item).strip()
        if not text:
            continue
        if len(text) > 160:
            raise ValueError("列表项长度不能超过 160 个字符")
        result.append(text)
    result = list(dict.fromkeys(result))
    if len(result) > max_items:
        raise ValueError(f"列表最多包含 {max_items} 项")
    return result


def _list_int_value(value: Any, max_items: int = 128) -> list[int]:
    result = []
    for item in _list_values(value):
        try:
            parsed = _int_value(item)
        except ValueError as exc:
            raise ValueError("列表只能包含整数") from exc
        if parsed < 1:
            raise ValueError("列表中的值必须大于 0")
        result.append(parsed)
    result = list(dict.fromkeys(result))
    if len(result) > max_items:
        raise ValueError(f"列表最多包含 {max_items} 项")
    return result


def _tz_value(value: Any) -> str:
    timezone_name = str(value or "").strip()
    if not timezone_name:
        raise ValueError("时区不能为空")
    try:
        ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError("时区名称无效，例如 Asia/Shanghai") from exc
    return timezone_name


def _node_name_pattern(value: Any) -> str:
    pattern = str(value or "").strip()
    if not pattern or len(pattern) > 120:
        raise ValueError("节点命名模板不能为空且不能超过 120 个字符")
    allowed = {"host", "last_octet"}
    try:
        parsed = list(string.Formatter().parse(pattern))
        for _, field_name, format_spec, conversion in parsed:
            if field_name is None:
                continue
            if field_name not in allowed or format_spec or conversion:
                raise ValueError(
                    "节点命名模板仅支持 {host} 和 {last_octet}，不支持格式说明"
                )
        pattern.format(host="192.0.2.1", last_octet="1")
    except (KeyError, IndexError, ValueError) as exc:
        if isinstance(exc, ValueError) and str(exc).startswith("节点命名模板"):
            raise
        raise ValueError("节点命名模板格式无效") from exc
    return pattern


def _ssh_user(value: Any) -> str:
    user = str(value or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", user):
        raise ValueError("SSH 用户名格式无效")
    return user


def _remote_python(value: Any) -> str:
    return command_text(value, label="远端 Python 命令", max_length=500)


def _command_name(value: Any) -> str:
    return command_text(value, label="容器运行时命令", max_length=500)


def _optional_command(value: Any) -> str:
    return command_text(
        value,
        label="GPU 查询命令",
        allow_empty=True,
        max_length=500,
    )


def _ssh_command(value: Any) -> str:
    return command_text(value, label="SSH 命令", max_length=500)


def _model_scheme(value: Any) -> str:
    scheme = str(value or "").strip().lower()
    if scheme not in {"http", "https"}:
        raise ValueError("仅支持 http 或 https")
    return scheme


def _url_path(value: Any) -> str:
    path = str(value or "").strip()
    if not path.startswith("/") or len(path) > 500 or re.search(r"\s", path):
        raise ValueError("必须以 / 开头、不能包含空白字符且不能超过 500 个字符")
    return path


def _ssh_host_key_policy_value(value: Any) -> str:
    policy = str(value or "").strip().lower()
    if policy not in SSH_HOST_KEY_POLICIES:
        allowed = "、".join(sorted(SSH_HOST_KEY_POLICIES))
        raise ValueError(f"仅支持 {allowed}")
    return policy


def _config_fields(settings: Settings) -> tuple[ConfigField, ...]:
    model_ports = [
        int(item)
        for item in _env_list("SPARK_MODEL_PORTS", "8888,8000,8001,30000")
        if item.isdigit()
    ]
    model_containers = _env_list("SPARK_MODEL_CONTAINERS", "")
    return (
        ConfigField(
            "cpu_warning", "CPU 告警", "CPU 使用率达到该值时产生告警。",
            "alerts", "int", 85, 1, 100, "%"
        ),
        ConfigField(
            "memory_warning", "内存告警", "内存使用率达到该值时产生告警。",
            "alerts", "int", 85, 1, 100, "%"
        ),
        ConfigField(
            "gpu_warning", "GPU 告警", "任一 GPU 负载达到该值时产生告警。",
            "alerts", "int", 90, 1, 100, "%"
        ),
        ConfigField(
            "temperature_warning", "温度告警", "节点最高温度达到该值时产生告警。",
            "alerts", "int", 80, 1, 150, "°C"
        ),
        ConfigField(
            "offline_seconds", "离线判定", "最后采集时间超过该时长后判定离线。",
            "alerts", "int", 20, 5, 86400, "秒"
        ),
        ConfigField(
            "ssh_user", "SSH 用户", "新建节点、批量添加和扫描时使用的默认用户。",
            "ssh", "string", settings.ssh_user, max_length=64
        ),
        ConfigField(
            "ssh_port", "SSH 端口", "未指定节点端口时使用的默认 SSH 端口。",
            "ssh", "int", settings.ssh_port, 1, 65535, ""
        ),
        ConfigField(
            "ssh_key_path", "SSH 私钥路径", "中心节点可访问的私钥文件路径；留空使用 SSH Agent 或密码。",
            "ssh", "string", settings.ssh_key_path, max_length=500
        ),
        ConfigField(
            "ssh_remote_python", "远端 Python",
            "被监控节点上执行采集脚本的 Python 命令及可选参数，例如 /usr/bin/python3 -u。",
            "ssh", "string", _env_text("SPARK_REMOTE_PYTHON", "python3"),
            max_length=500, placeholder="/usr/bin/python3 -u"
        ),
        ConfigField(
            "ssh_timeout", "SSH 超时", "探针和指标采集命令的最长等待时间。",
            "ssh", "float", settings.ssh_timeout, 1, 300, "秒"
        ),
        ConfigField(
            "ssh_connect_timeout", "SSH 连接超时",
            "建立 SSH TCP 连接和等待认证的最长秒数，应与节点网络延迟匹配。",
            "ssh", "float", settings.ssh_connect_timeout, 1, 300, "秒"
        ),
        ConfigField(
            "ssh_control_persist", "SSH 连接复用",
            "OpenSSH ControlMaster 空闲连接保留秒数，设为 0 表示禁用连接复用。",
            "ssh", "int", settings.ssh_control_persist, 0, 3600, "秒"
        ),
        ConfigField(
            "ssh_strict_host_key_checking", "主机密钥策略",
            "支持 accept-new、yes、no、off；yes 会拒绝未登记的新主机密钥。",
            "ssh", "string", settings.ssh_strict_host_key_checking,
            max_length=20, placeholder="accept-new"
        ),
        ConfigField(
            "ssh_bin", "SSH 命令",
            "中心节点执行 SSH 的命令及可选参数，可使用包装器或绝对路径。",
            "ssh", "string", _env_text("SPARK_SSH_BIN", "ssh"),
            max_length=500, placeholder="ssh"
        ),
        ConfigField(
            "ssh_known_hosts_path", "known_hosts 路径",
            "保存已确认主机密钥的路径；留空时使用数据目录下的 ssh/known_hosts。",
            "ssh", "string", _env_text("SPARK_SSH_KNOWN_HOSTS_PATH", ""),
            max_length=500, placeholder="留空使用数据目录"
        ),
        ConfigField(
            "ssh_interface_query_timeout_seconds", "网卡查询超时",
            "远端执行 ip 命令读取网卡地址的最长等待时间。",
            "ssh", "float",
            _env_float("SPARK_SSH_INTERFACE_QUERY_TIMEOUT", 3),
            0.1, 60, "秒"
        ),
        ConfigField(
            "ssh_gpu_info_timeout_seconds", "GPU 识别超时",
            "SSH 探针读取 GPU 型号的最长等待时间。",
            "ssh", "float",
            _env_float("SPARK_SSH_GPU_INFO_TIMEOUT", 3),
            0.1, 60, "秒"
        ),
        ConfigField(
            "ssh_gpu_metrics_timeout_seconds", "GPU 指标超时",
            "远端采集 GPU 指标的最长等待时间。",
            "ssh", "float",
            _env_float("SPARK_SSH_GPU_METRICS_TIMEOUT", 4),
            0.1, 120, "秒"
        ),
        ConfigField(
            "ssh_cpu_sample_interval_seconds", "CPU 采样间隔",
            "远端 psutil CPU 使用率采样窗口；未安装 psutil 时不影响回退逻辑。",
            "ssh", "float",
            _env_float("SPARK_SSH_CPU_SAMPLE_INTERVAL", 0.5),
            0.1, 10, "秒"
        ),
        ConfigField(
            "node_name_pattern", "节点命名模板", "支持 {host} 和 {last_octet}，例如 spark-{last_octet}。",
            "nodes", "string",
            _env_text("SPARK_NODE_NAME_PATTERN", "spark-{last_octet}"),
            max_length=120
        ),
        ConfigField(
            "default_group_name", "默认分组", "新建节点未指定分组时使用。",
            "nodes", "string", _env_text("SPARK_DEFAULT_GROUP", "未分组"),
            max_length=80
        ),
        ConfigField(
            "default_tags", "默认标签", "新建节点未指定标签时使用，多个标签用逗号分隔。",
            "nodes", "list_str", _env_list("SPARK_DEFAULT_TAGS", ""), max_items=32
        ),
        ConfigField(
            "max_tags_per_node", "单节点最大标签", "新增、编辑和批量操作时单个节点允许的标签数量。",
            "nodes", "int", _env_int("SPARK_MAX_TAGS_PER_NODE", 32), 1, 256
        ),
        ConfigField(
            "auto_import_group_name", "自动导入分组", "从扫描结果纳管节点时使用的分组。",
            "nodes", "string", _env_text("SPARK_AUTO_IMPORT_GROUP", "自动发现"),
            max_length=80
        ),
        ConfigField(
            "auto_import_tags", "自动导入标签", "从扫描结果纳管节点时附加的标签。",
            "nodes", "list_str", _env_list("SPARK_AUTO_IMPORT_TAGS", "auto-discovery"),
            max_items=32
        ),
        ConfigField(
            "bulk_add_max_nodes", "批量添加上限", "单次批量添加最多展开的节点数量。",
            "nodes", "int", _env_int("SPARK_BULK_ADD_MAX_NODES", 256), 1, 65536
        ),
        ConfigField(
            "discovery_auto_enabled", "启用自动扫描", "开启后中心节点按周期扫描所选网卡或全部物理网卡。",
            "discovery", "bool", _env_bool("SPARK_DISCOVERY_AUTO", True)
        ),
        ConfigField(
            "discovery_initial_delay_seconds", "首次扫描延迟", "服务启动后等待多久开始自动扫描。",
            "discovery", "float",
            _env_float("SPARK_DISCOVERY_INITIAL_DELAY", 3.0), 0, 3600, "秒"
        ),
        ConfigField(
            "discovery_interval_seconds", "自动扫描周期", "两次自动扫描之间的间隔。",
            "discovery", "float", settings.discovery_interval, 10, 86400, "秒"
        ),
        ConfigField(
            "discovery_port_concurrency", "端口并发", "同时探测的 TCP 端口数量。",
            "discovery", "int",
            _env_int("SPARK_DISCOVERY_PORT_CONCURRENCY", 64), 1, 512
        ),
        ConfigField(
            "discovery_port_timeout_seconds", "端口超时", "单个 SSH 端口探测的最长等待时间。",
            "discovery", "float",
            _env_float("SPARK_DISCOVERY_PORT_TIMEOUT", min(0.28, settings.node_timeout)),
            0.05, 30, "秒"
        ),
        ConfigField(
            "discovery_max_hosts", "最大主机数", "单次扫描最多展开的主机地址数量。",
            "discovery", "int",
            _env_int("SPARK_DISCOVERY_MAX_HOSTS", 2048), 1, 65536
        ),
        ConfigField(
            "discovery_ssh_concurrency", "认证并发", "同时执行 SSH 身份认证的节点数量。",
            "discovery", "int",
            _env_int("SPARK_DISCOVERY_SSH_CONCURRENCY", 8), 1, 64
        ),
        ConfigField(
            "discovery_result_retention_seconds", "结果保留", "扫描结果在局域网发现页面保留的时间。",
            "discovery", "int",
            _env_int("SPARK_DISCOVERY_RESULT_RETENTION", 900), 60, 2592000, "秒"
        ),
        ConfigField(
            "discovery_saved_lookback_seconds", "历史凭据回溯", "扫描时复用该时间窗口内保存过的 SSH 凭据和端口。",
            "discovery", "int",
            _env_int("SPARK_DISCOVERY_SAVED_LOOKBACK", 2592000),
            60, 31536000, "秒"
        ),
        ConfigField(
            "discovery_reuse_saved_ports", "复用历史端口", "尝试之前发现过的非默认 SSH 端口。",
            "discovery", "bool",
            _env_bool("SPARK_DISCOVERY_REUSE_PORTS", True)
        ),
        ConfigField(
            "poll_interval", "采集间隔", "节点指标轮询周期。",
            "collector", "float", settings.poll_interval, 1, 3600, "秒"
        ),
        ConfigField(
            "collector_concurrency", "采集并发", "同时通过 SSH 采集的节点数量。",
            "collector", "int", _env_int("SPARK_COLLECTOR_CONCURRENCY", 8), 1, 64
        ),
        ConfigField(
            "network_interface_exclude_prefixes", "网卡过滤前缀",
            "指标采集时忽略虚拟或隧道网卡，支持多个前缀；清空表示不按名称过滤。",
            "collector", "list_str", _env_list(
                "SPARK_NETWORK_INTERFACE_EXCLUDE_PREFIXES",
                ",".join(NETWORK_INTERFACE_EXCLUDE_PREFIXES),
            ), max_items=64
        ),
        ConfigField(
            "network_route_probe_host", "管理地址探测目标",
            "远端节点选择管理网卡 IPv4 地址时使用的可达目标；留空时优先使用当前 SSH 连接来源。",
            "collector", "string",
            _env_text("SPARK_NETWORK_ROUTE_PROBE_HOST", ""),
            max_length=253, placeholder="例如 192.0.2.1"
        ),
        ConfigField(
            "offline_failures", "离线失败次数", "连续采集失败多少次后标记节点离线。",
            "collector", "int", _env_int("SPARK_OFFLINE_FAILURES", 2), 1, 20
        ),
        ConfigField(
            "history_points", "实时历史点数", "总览节点卡片加载的最近采样点数量。",
            "collector", "int", settings.history_points, 10, 10000
        ),
        ConfigField(
            "history_retention_hours", "历史保留时长", "指标和模型吞吐历史超过该时长后清理。",
            "collector", "int", settings.history_retention_hours, 1, 8760, "小时"
        ),
        ConfigField(
            "prune_interval_seconds", "清理周期", "执行历史清理和过期会话清理的间隔。",
            "collector", "float", _env_float("SPARK_PRUNE_INTERVAL", 600), 30, 86400, "秒"
        ),
        ConfigField(
            "model_default_port", "默认模型端口", "节点未单独指定模型端口时使用的端口。",
            "model", "int",
            model_ports[0] if model_ports else 8888,
            1, 65535
        ),
        ConfigField(
            "model_scheme", "模型服务协议",
            "探测模型健康检查和指标时使用的协议。",
            "model", "string",
            _env_text_or_default("SPARK_MODEL_SCHEME", "http"),
            max_length=5, placeholder="http"
        ),
        ConfigField(
            "model_ports", "模型服务端口", "模型健康检查、指标和日志探测端口。",
            "model", "list_int", model_ports, 1, 65535
        ),
        ConfigField(
            "model_container_hints", "容器名称提示", "优先匹配的模型容器名称，支持多个。",
            "model", "list_str", model_containers
        ),
        ConfigField(
            "model_engine_hints", "容器引擎提示", "自动选择容器时的名称或镜像关键词。",
            "model", "list_str", _env_list(
                "SPARK_MODEL_ENGINE_HINTS", "vllm,sglang"
            )
        ),
        ConfigField(
            "model_container_runtime", "容器运行时",
            "远端查询模型容器的命令及可选参数，例如 docker、podman、sudo -n docker。",
            "model", "string", _env_text("SPARK_MODEL_CONTAINER_RUNTIME", "docker"),
            max_length=500, placeholder="sudo -n docker"
        ),
        ConfigField(
            "model_health_path", "健康检查路径",
            "模型服务的 HTTP 健康检查路径。",
            "model", "string", _env_text("SPARK_MODEL_HEALTH_PATH", "/health"),
            max_length=500, placeholder="/health"
        ),
        ConfigField(
            "model_metrics_path", "指标路径",
            "模型服务的 Prometheus 指标路径。",
            "model", "string", _env_text("SPARK_MODEL_METRICS_PATH", "/metrics"),
            max_length=500, placeholder="/metrics"
        ),
        ConfigField(
            "model_timezone", "日志显示时区", "解析 SGLang 和 vLLM 日志时间时使用的 IANA 时区。",
            "model", "timezone",
            _env_text_or_default("SPARK_MODEL_TIMEZONE", system_timezone_name())
        ),
        ConfigField(
            "model_sample_interval_seconds", "后台采样周期", "模型概览后台刷新周期。",
            "model", "float", _env_float("SPARK_MODEL_SAMPLE_INTERVAL", 15), 2, 3600, "秒"
        ),
        ConfigField(
            "model_cluster_concurrency", "集群探测并发", "模型健康和容器检查的并发数量。",
            "model", "int", _env_int("SPARK_MODEL_CLUSTER_CONCURRENCY", 8), 1, 64
        ),
        ConfigField(
            "model_http_timeout_seconds", "健康检查超时", "模型服务 HTTP 健康检查超时。",
            "model", "float", _env_float("SPARK_MODEL_HTTP_TIMEOUT", 2), 0.2, 60, "秒"
        ),
        ConfigField(
            "model_metrics_timeout_seconds", "指标请求超时", "拉取 Prometheus 指标的超时。",
            "model", "float", _env_float("SPARK_MODEL_METRICS_TIMEOUT", 5), 0.2, 120, "秒"
        ),
        ConfigField(
            "model_ssh_timeout_seconds", "容器命令超时", "Docker 容器查询和检查命令超时。",
            "model", "float", _env_float("SPARK_MODEL_SSH_TIMEOUT", 8), 1, 300, "秒"
        ),
        ConfigField(
            "model_log_timeout_seconds", "日志读取超时", "读取容器日志的超时。",
            "model", "float", _env_float("SPARK_MODEL_LOG_TIMEOUT", 12), 1, 300, "秒"
        ),
        ConfigField(
            "model_overview_log_lines", "概览日志行数", "计算吞吐时读取的最近日志行数。",
            "model", "int", _env_int("SPARK_MODEL_OVERVIEW_LOG_LINES", 800), 20, 1500
        ),
        ConfigField(
            "model_log_lines", "默认日志行数", "日志接口默认读取行数。",
            "model", "int", _env_int("SPARK_MODEL_LOG_LINES", 220), 20, 1500
        ),
        ConfigField(
            "model_log_max_lines", "单节点最大日志", "向远端请求的最大日志行数。",
            "model", "int", _env_int("SPARK_MODEL_LOG_MAX_LINES", 1500), 20, 5000
        ),
        ConfigField(
            "model_log_all_limit", "全部节点最大日志", "全部节点日志视图最多展示的行数。",
            "model", "int", _env_int("SPARK_MODEL_LOG_ALL_LIMIT", 1200), 20, 5000
        ),
        ConfigField(
            "model_cache_overview_seconds", "概览缓存", "模型概览结果缓存时间。",
            "model", "float", _env_float("SPARK_MODEL_OVERVIEW_CACHE", 4), 0, 300, "秒"
        ),
        ConfigField(
            "model_cache_runtime_seconds", "运行态缓存", "模型服务探测结果缓存时间。",
            "model", "float", _env_float("SPARK_MODEL_RUNTIME_CACHE", 10), 0, 300, "秒"
        ),
        ConfigField(
            "model_cache_container_seconds", "容器列表缓存", "Docker 容器列表缓存时间。",
            "model", "float", _env_float("SPARK_MODEL_CONTAINER_CACHE", 20), 0, 600, "秒"
        ),
        ConfigField(
            "model_cache_inspect_seconds", "容器状态缓存", "Docker inspect 状态缓存时间。",
            "model", "float", _env_float("SPARK_MODEL_INSPECT_CACHE", 5), 0, 300, "秒"
        ),
        ConfigField(
            "model_cache_logs_seconds", "日志缓存", "模型日志缓存时间。",
            "model", "float", _env_float("SPARK_MODEL_LOGS_CACHE", 4), 0, 300, "秒"
        ),
        ConfigField(
            "model_history_minutes", "吞吐曲线时长", "模型 P/D 吞吐历史窗口。",
            "model", "int", _env_int("SPARK_MODEL_HISTORY_MINUTES", 30), 1, 1440, "分钟"
        ),
        ConfigField(
            "model_history_buckets", "吞吐曲线点数", "模型 P/D 吞吐历史分桶数量。",
            "model", "int", _env_int("SPARK_MODEL_HISTORY_BUCKETS", 30), 2, 1440
        ),
        ConfigField(
            "model_health_concurrency", "健康检查并发", "模型节点健康检查并发数量。",
            "model", "int", _env_int("SPARK_MODEL_HEALTH_CONCURRENCY", 8), 1, 64
        ),
        ConfigField(
            "gpu_query_command", "GPU 查询命令",
            "节点上读取 GPU 指标的命令及可选参数；留空表示不采集 GPU。",
            "collector", "string",
            _env_text("SPARK_GPU_QUERY_COMMAND", "nvidia-smi"),
            max_length=500, placeholder="nvidia-smi"
        ),
        ConfigField(
            "gpu_power_estimate_enabled", "启用功耗估算",
            "仅对匹配型号按功耗估算 GPU 负载；默认关闭并直接使用驱动利用率。",
            "collector", "bool", _env_bool("SPARK_GPU_POWER_ESTIMATE", False)
        ),
        ConfigField(
            "gpu_power_estimate_names", "功耗估算 GPU 名称",
            "名称子串匹配，可用逗号分隔多个型号；仅对匹配的 GPU 启用功耗估算。",
            "collector", "list_str",
            _env_list("SPARK_GPU_POWER_ESTIMATE_NAMES", ""), max_items=32
        ),
        ConfigField(
            "gpu_power_estimate_idle_watts", "GPU 空闲功率",
            "功耗估算使用的空闲功耗基线。", "collector", "float",
            _env_float("SPARK_GPU_POWER_ESTIMATE_IDLE_WATTS", 0), 0, 100000, "W"
        ),
        ConfigField(
            "gpu_power_estimate_rated_watts", "GPU 额定功率",
            "功耗估算使用的额定功耗；设为 0 时采用 GPU 查询命令上报的 power.limit。",
            "collector", "float",
            _env_float("SPARK_GPU_POWER_ESTIMATE_RATED_WATTS", 0), 0, 100000, "W"
        ),
        ConfigField(
            "ui_history_minutes", "页面历史窗口", "总览和节点卡片默认展示的历史时长。",
            "interface", "int", _env_int("SPARK_UI_HISTORY_MINUTES", 30), 5, 1440, "分钟"
        ),
        ConfigField(
            "ui_history_points", "页面历史点数", "集群历史曲线最大返回点数。",
            "interface", "int", _env_int("SPARK_UI_HISTORY_POINTS", 120), 10, 2000
        ),
        ConfigField(
            "ui_history_ranges", "历史范围选项", "详情抽屉可选的历史分钟数，用逗号分隔。",
            "interface", "list_int",
            _env_list("SPARK_UI_HISTORY_RANGES", "5,30,60,360,1440"), 5, 1440, "分钟"
        ),
        ConfigField(
            "ui_cluster_history_refresh_seconds", "集群历史刷新", "总览集群历史曲线的刷新周期。",
            "interface", "float",
            _env_float("SPARK_UI_CLUSTER_HISTORY_REFRESH", 60), 10, 3600, "秒"
        ),
        ConfigField(
            "ui_timezone", "页面显示时区", "日志时间和 ISO 时间在前端展示时使用的 IANA 时区。",
            "interface", "timezone",
            _env_text_or_default("SPARK_UI_TIMEZONE", system_timezone_name())
        ),
        ConfigField(
            "ui_model_log_refresh_seconds", "模型日志刷新", "模型日志页面的自动刷新周期。",
            "interface", "float",
            _env_float("SPARK_UI_MODEL_LOG_REFRESH", 10), 2, 3600, "秒"
        ),
        ConfigField(
            "ui_model_log_lines", "模型日志前端行数", "模型日志页面每次请求的行数。",
            "interface", "int", _env_int("SPARK_UI_MODEL_LOG_LINES", 350), 20, 5000
        ),
        ConfigField(
            "ui_gpu_fallback_name", "GPU 缺省名称", "节点未上报 GPU 型号时卡片显示的通用名称。",
            "interface", "string", _env_text("SPARK_UI_GPU_FALLBACK", "GPU"),
            max_length=80
        ),
        ConfigField(
            "dashboard_refresh_seconds", "页面刷新间隔", "前端自动刷新总览和状态的时间间隔。",
            "interface", "float",
            _env_float("SPARK_DASHBOARD_REFRESH", 5), 2, 120, "秒"
        ),
        ConfigField(
            "session_ttl_hours", "会话有效期", "管理员登录会话的有效小时数。",
            "security", "int", settings.session_ttl_hours, 1, 720, "小时"
        ),
        ConfigField(
            "login_max_attempts", "登录失败上限", "窗口期内允许的失败登录次数。",
            "security", "int", _env_int("SPARK_LOGIN_MAX_ATTEMPTS", 8), 1, 100
        ),
        ConfigField(
            "login_window_seconds", "登录限制窗口", "失败登录计数的时间窗口。",
            "security", "int", _env_int("SPARK_LOGIN_WINDOW", 600), 30, 86400, "秒"
        ),
        ConfigField(
            "database_timeout_seconds", "数据库锁等待",
            "中心数据库写入遇到锁时的最长等待时间，适用于高并发采集环境。",
            "storage", "float",
            getattr(settings, "database_timeout_seconds", 15),
            0.1, 300, "秒"
        ),
    )


class RuntimeConfig:
    def __init__(self, db: Database, settings: Settings):
        self.db = db
        self.settings = settings
        self.fields = _config_fields(settings)
        self._by_key = {field.key: field for field in self.fields}
        self.db.timeout_seconds = float(
            self.values()["database_timeout_seconds"]
        )

    def _normalize(self, field: ConfigField, value: Any) -> TypedValue:
        if field.field_type == "int":
            parsed: TypedValue = _int_value(value)
        elif field.field_type == "float":
            parsed = _float_value(value)
        elif field.field_type == "bool":
            parsed = _bool_value(value)
        elif field.field_type == "list_int":
            parsed = _list_int_value(value, field.max_items)
            if not parsed:
                raise ValueError("列表不能为空")
        elif field.field_type == "list_str":
            parsed = _list_str_value(value, field.max_items)
        elif field.field_type == "timezone":
            parsed = _tz_value(value)
        elif field.key == "node_name_pattern":
            parsed = _node_name_pattern(value)
        elif field.key == "ssh_user":
            parsed = _ssh_user(value)
        elif field.key == "ssh_remote_python":
            parsed = _remote_python(value)
        elif field.key == "ssh_bin":
            parsed = _ssh_command(value)
        elif field.key == "gpu_query_command":
            parsed = _optional_command(value)
        elif field.key == "ssh_strict_host_key_checking":
            parsed = _ssh_host_key_policy_value(value)
        elif field.key == "model_scheme":
            parsed = _model_scheme(value)
        elif field.key in {
            "model_container_runtime",
        }:
            parsed = _command_name(value)
        elif field.key in {"model_health_path", "model_metrics_path"}:
            parsed = _url_path(value)
        else:
            parsed = _string_value(field.max_length)(value)
        if isinstance(parsed, list) and field.field_type == "list_int":
            for item in parsed:
                if field.minimum is not None and item < field.minimum:
                    raise ValueError(f"列表中的值不能小于 {field.minimum:g}")
                if field.maximum is not None and item > field.maximum:
                    raise ValueError(f"列表中的值不能大于 {field.maximum:g}")
        if isinstance(parsed, (int, float)) and not isinstance(parsed, bool):
            if field.minimum is not None and parsed < field.minimum:
                raise ValueError(f"不能小于 {field.minimum:g}")
            if field.maximum is not None and parsed > field.maximum:
                raise ValueError(f"不能大于 {field.maximum:g}")
        return parsed

    def values(self) -> dict[str, TypedValue]:
        stored = self.db.get_settings()
        result: dict[str, TypedValue] = {}
        for field in self.fields:
            if field.key not in stored:
                result[field.key] = field.default
                continue
            try:
                result[field.key] = self._normalize(field, stored[field.key])
            except ValueError:
                result[field.key] = field.default
        return result

    def _validate_related(self, values: dict[str, TypedValue]) -> None:
        max_tags = int(values["max_tags_per_node"])
        for key in ("default_tags", "auto_import_tags"):
            tags = values[key]
            if len(tags) > max_tags:
                label = self._by_key[key].label
                raise ValueError(
                    f"{label}数量不能超过单节点最大标签数 {max_tags}"
                )

    def current(self) -> EffectiveConfig:
        values = self.values()
        return EffectiveConfig(
            root_dir=self.settings.root_dir,
            data_dir=self.settings.data_dir,
            static_dir=self.settings.static_dir,
            host=self.settings.host,
            port=self.settings.port,
            cookie_secure=self.settings.cookie_secure,
            **values,
        )

    def update(self, values: dict[str, Any]) -> dict[str, TypedValue]:
        if not isinstance(values, dict) or not values:
            raise ValueError("没有可保存的配置项")
        unknown = sorted(set(values) - set(self._by_key))
        if unknown:
            raise ValueError(f"未知配置项: {', '.join(unknown)}")
        normalized: dict[str, TypedValue] = {}
        for key, raw_value in values.items():
            field = self._by_key[key]
            try:
                normalized[key] = self._normalize(field, raw_value)
            except ValueError as exc:
                raise ValueError(f"{field.label}: {exc}") from exc
        candidate = self.values()
        candidate.update(normalized)
        try:
            self._validate_related(candidate)
        except ValueError as exc:
            raise ValueError(str(exc)) from exc
        self.db.update_settings(normalized)
        if "database_timeout_seconds" in normalized:
            self.db.timeout_seconds = float(
                normalized["database_timeout_seconds"]
            )
        return self.values()

    def thresholds(self) -> dict[str, int]:
        values = self.values()
        return {
            "cpu_warning": int(values["cpu_warning"]),
            "memory_warning": int(values["memory_warning"]),
            "gpu_warning": int(values["gpu_warning"]),
            "temperature_warning": int(values["temperature_warning"]),
            "offline_seconds": int(values["offline_seconds"]),
        }

    def ssh_defaults(self) -> dict[str, Any]:
        values = self.values()
        return {
            "user": str(values["ssh_user"]),
            "port": int(values["ssh_port"]),
            "key_path": str(values["ssh_key_path"]),
            "key_path_configured": bool(values["ssh_key_path"]),
            "password_supported": True,
            "host_key_policy": str(values["ssh_strict_host_key_checking"]),
        }

    def public_payload(self) -> dict[str, Any]:
        values = self.values()
        groups = [
            {
                "key": key,
                "label": label,
                "description": description,
                "fields": [
                    field.metadata(values[field.key])
                    for field in self.fields
                    if field.group == key
                ],
            }
            for key, label, description in CONFIG_GROUPS
        ]
        return {
            "settings": values,
            "fields": [
                field.metadata(values[field.key]) for field in self.fields
            ],
            "groups": groups,
            "ssh_defaults": self.ssh_defaults(),
            "process": {
                "host": self.settings.host,
                "port": self.settings.port,
                "data_dir": str(self.settings.data_dir),
                "database_path": str(
                    getattr(self.settings, "database_path", None)
                    or self.settings.data_dir / "spark-grid.db"
                ),
                "cookie_secure": self.settings.cookie_secure,
            },
        }
