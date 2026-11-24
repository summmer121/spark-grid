# Spark Grid

Spark Grid 是一个面向可 SSH 管理的 Linux GPU 集群的轻量监控平台，默认配置可用于 DGX Spark，也可用于其他厂商和型号的 GPU 节点。中心服务通过 SSH 主动拉取节点指标，不在节点安装 Agent，也不要求节点额外开放监控端口。

节点数量、SSH 账号、密码或私钥、扫描网段、GPU 查询命令和容器运行时均不写死，可在页面设置或环境变量中调整。

## 功能

- 节点总览、在线状态、CPU、GPU、统一内存、温度和网络吞吐
- 最近 24 小时节点指标历史与可悬停折线图
- 局域网 SSH 资产发现、批量纳管、SSH 私钥或密码认证、分组、标签和启停
- CPU、GPU、内存、温度、离线和心跳告警
- 登录鉴权、CSRF 防护、操作审计和 CSV 导出
- vLLM / SGLang 自动识别和模型运行态监控
- Prefill / Decode 最近 30 分钟吞吐折线图
- 模型日志查看、筛选和下载
- 模型服务生命周期管理：自动识别 HEAD 节点 `model_*.service`，支持单服务启停与模型切换（含 guard 守护单元协调）
- 会话测试：对话式验证推理服务，实时显示输入 / 输出 token、TTFT、总耗时与吞吐
- API Key 管理：查看服务端当前 Key、来源、Endpoint，一键复制 curl 示例
- 告警规则在线查看与修改，支持一键恢复默认
- 内置界面帮助：各页面和关键面板标题旁的 `?` 弹出机制说明
- 响应式布局，支持桌面、平板和手机

## 架构

- 中心端：Python 标准库 HTTP 服务、SQLite、鉴权、告警和资产管理
- 采集端：无常驻 Agent
- 采集方式：中心端按配置间隔通过 SSH 执行只读 Python 采样脚本
- 局域网发现：中心端扫描页面所选网卡的 IPv4 网段或手动 CIDR，再通过所选私钥或 SSH 密码探测主机身份
- 数据保留：节点历史默认保留 24 小时，模型吞吐历史按当前模型启动周期保存

平台默认直接使用 GPU 驱动利用率，不假设 GPU 型号，也不默认启用功耗估算。某些型号的驱动利用率不可信时，可在页面中启用功耗估算，并按 GPU 名称子串配置适用型号、空闲功率和额定功率。

## 快速开始

要求：

- Python 3.10+
- 中心节点可以使用私钥或已保存的 SSH 密码登录全部被监控节点
- 被监控节点提供 Python 3；默认使用 `python3`，也可通过页面或 `SPARK_REMOTE_PYTHON` 指定绝对路径和其他参数
- 默认使用 `nvidia-smi` 查询 GPU；不需要 GPU 指标时可将 GPU 查询命令留空

```bash
git clone https://github.com/<your-account>/spark-grid.git
cd spark-grid
cp .env.example .env
chmod +x scripts/*.sh
```

如需在启动前预置节点，可编辑 `.env` 中的 `SPARK_HOSTS`。SSH 默认账号留空时使用中心服务运行账号，也可以在首次登录后的页面设置中修改。然后检查 SSH：

```bash
./scripts/check_cluster_ssh.sh
```

启动中心服务：

```bash
./scripts/start.sh
```

浏览器打开：

```text
http://<center-host>:<SPARK_MONITOR_PORT>
```

首次访问会引导创建管理员账号。密码只保存为 PBKDF2-SHA256 加盐哈希。

停止或重启：

```bash
./scripts/stop.sh
./scripts/restart.sh
```

日志和 PID 路径分别由 `SPARK_LOG_FILE`、`SPARK_PID_FILE` 控制。相对路径以项目根目录解析，健康检查可用 `SPARK_HEALTH_URL` 完全覆盖。

## SSH 配置

中心节点先探测节点配置中的 SSH TCP 端口，再用私钥或密码完成认证。认证成功后，中心节点按 `SPARK_POLL_INTERVAL` 周期主动通过 SSH 执行只读采集脚本，不要求被监控节点安装 Agent 或向中心上报。端口开放只表示 TCP 可达，不代表认证成功。

推荐在中心节点的 `~/.ssh/config` 中使用自己的别名，例如：

```sshconfig
Host node-a node-b node-c
    User <ssh-user>
    Port <ssh-port>
    IdentityFile ~/.ssh/<private-key>
    IdentitiesOnly yes
    StrictHostKeyChecking accept-new
```

也可以在 DNS 或 `/etc/hosts` 中配置节点地址。下面是文档专用地址示例，必须替换：

```text
192.0.2.11  node-a
192.0.2.12  node-b
192.0.2.13  node-c
```

`192.0.2.0/24` 是 RFC 5737 文档专用网段，仅用于示例，请替换为实际管理地址。

检查脚本不再假设固定节点名，必须在 `.env` 中设置目标列表：

```bash
SPARK_HOSTS=node-a,node-b,node-c
SPARK_SSH_USER=<ssh-user>
SPARK_SSH_PORT=22
SPARK_SSH_KEY_PATH=~/.ssh/<private-key>
```

`check_cluster_ssh.sh` 使用非交互密钥或 SSH Agent 检查，并读取 `SPARK_GPU_QUERY_COMMAND`。页面中的扫描、批量添加、编辑节点和连接测试同时支持 SSH 私钥与 SSH 密码。密码不会通过 API 返回或写入前端存储；需要复用密码时，密码会加密后保存到 SQLite。

`SPARK_SSH_USER` 留空时自动使用运行中心服务的账号，不会假设固定用户名为 `spark`。

凭据密钥默认位于数据目录下的 `ssh-secret.key`，可通过 `SPARK_CREDENTIAL_KEY_FILE` 修改。它必须与数据库一起备份并严格限制访问权限。密钥丢失后，数据库中已有的 SSH 密码无法解密，需要在页面中重新设置。生产环境推荐使用专用最小权限账号，并配合防火墙限制。

### 运行参数

页面中的“系统设置”会持久化到 SQLite，后台采集和模型监控线程会读取最新值，不需要为常用参数反复改 `.env`。可调整项包括：

- SSH 默认用户、端口、私钥、连接超时和连接复用秒数
- SSH 可执行文件、known_hosts 路径、网卡查询/GPU 识别/GPU 指标超时和 CPU 采样窗口
- 远端 Python 命令，例如 `/usr/bin/python3 -u`
- GPU 查询命令；留空表示不采集 GPU
- 功耗估算开关、适用 GPU 名称、空闲功率和额定功率
- 模型服务协议、端口、容器名称提示、容器运行时命令，例如 `docker`、`podman` 或 `sudo -n docker`
- 扫描并发、数据库锁等待、告警阈值、页面刷新和时区等；页面和模型日志默认使用系统时区，也可分别设置 IANA 时区

命令类配置支持可执行文件绝对路径和独立参数，执行时不会拼接 shell 字符串。数据库位置、监听地址等启动期参数通过 `.env` 配置；`SPARK_DATABASE_PATH` 留空时使用数据目录中的 `spark-grid.db`。

常用环境变量如下，全部都有可运行的默认值，只有需要改变部署行为时才需要写入 `.env`：

```text
SPARK_DATABASE_PATH=
SPARK_DATABASE_TIMEOUT=15
SPARK_SSH_USER=
SPARK_SSH_PORT=22
SPARK_SSH_KEY_PATH=
SPARK_SSH_BIN=ssh
SPARK_SSH_KNOWN_HOSTS_PATH=
SPARK_SSH_TIMEOUT=8
SPARK_SSH_CONNECT_TIMEOUT=8
SPARK_SSH_INTERFACE_QUERY_TIMEOUT=3
SPARK_SSH_GPU_INFO_TIMEOUT=3
SPARK_SSH_GPU_METRICS_TIMEOUT=4
SPARK_SSH_CPU_SAMPLE_INTERVAL=0.5
SPARK_REMOTE_PYTHON=python3
SPARK_GPU_QUERY_COMMAND=nvidia-smi
SPARK_MODEL_SCHEME=http
SPARK_MODEL_PORTS=8888,8000,8001,30000
SPARK_MODEL_CONTAINER_RUNTIME=docker
```

`SPARK_SSH_KEY_PATH`、`SPARK_SSH_KNOWN_HOSTS_PATH`、`SPARK_DATABASE_PATH` 和 `SPARK_CREDENTIAL_KEY_FILE` 使用相对路径时，均以项目根目录为基准。`SPARK_MODEL_SCHEME` 仅接受 `http` 或 `https`。这些运行参数在页面保存后会写入 SQLite 并立即生效；监听地址、监听端口和数据库文件位置属于启动期配置，修改后需要重启服务。

## 局域网发现

中心端会扫描本机管理网段并尝试探测 SSH 主机。发现结果可以在页面中批量纳管。

扫描范围由运行中心服务的机器在页面中动态列出，可选择任一已有 IPv4 网卡并按该网卡网段扫描。未选择网卡时自动使用当前系统的物理网卡；页面也支持手动输入 CIDR 覆盖。

例如中心节点存在 `192.168.10.11/24` 网卡时，在“局域网发现”中选择该网卡，扫描目标会显示为 `192.168.10.0/24`。也可以保持网卡选择后手动确认 CIDR。只发现自己通常表示选到了其他网卡、容器网卡或使用了不包含目标节点的默认网段。

建议：

- 仅扫描受控管理网段
- 限制中心节点 SSH 可达范围
- 不要对公网开放节点 SSH
- 使用专用只读密钥，必要时在节点侧限制 `authorized_keys` 命令

## 模型监控

平台默认探测以下模型服务端口：

```text
8888,8000,8001,30000
```

可通过 `.env` 修改：

```bash
SPARK_MODEL_PORTS=8888,8000
SPARK_MODEL_CONTAINERS=my-vllm-container,my-sglang-container
```

vLLM 通过 Prometheus `/metrics` 获取：

- 并发和等待队列
- Prefill / Decode Token 吞吐
- KV Cache 和 Prefix Cache
- TTFT、E2E、ITL
- 成功和失败请求数
- 启动后累计 Token

SGLang 优先读取兼容指标，没有指标时回退到 Prefill / Decode 批次日志。

后台每 15 秒采样一次模型吞吐，按分钟聚合并保存到 SQLite。模型重启后，旧启动周期的数据不会混入新图。

## 模型服务控制

模型管理页的“模型服务控制”面板会自动扫描 HEAD 节点（节点列表中第一个节点）systemd 中名为 `model_*.service` 的服务单元，把它们识别为可管理的模型服务，无需在页面中逐条登记。

- **启动 / 停止**：通过 `systemctl start|stop` 执行，与在节点上手工操作完全一致。模型服务通常为 `Type=oneshot`，命令会等待启动脚本执行结束。
- **切换到此**：先停止其他正在运行的模型服务，以及可能把旧模型重新拉起的 `*-guard.service` 守护单元，再启动目标服务；启动完成后自动恢复与该模型对应的 guard（按单元名关键词或 `Before=` / `After=` 依赖关系识别）。
- **后台执行**：8 节点启动通常需要数分钟（受单元 `TimeoutStartSec` 约束，最长可达 70 分钟），任务在后台线程运行，面板每 4 秒刷新进度；同一时间只允许一个任务。
- **安全**：单元名按 `^model_[A-Za-z0-9][A-Za-z0-9._-]*\.service$` 校验，避免命令注入；所有操作写入操作审计。

前置条件：中心服务运行账号对 HEAD 节点具备免密 `sudo systemctl` 权限（例如 NOPASSWD sudoers 规则）。

## 会话测试

会话测试页提供对话式验证：选择模型后直接发消息，返回内容与生成指标。

- **模型选择**：从运行中服务的 `/v1/models` 读取可用模型；服务不可用时页面顶部提示原因。
- **指标**：输入 / 输出 token（引擎 usage）、TTFT（首 token 延迟）、总耗时、吞吐（输出 token ÷ 首 token 后的生成耗时）、结束原因。
- **会话累计**：多轮对话累计输入 / 输出 token 与平均生成速度；每条回复下方附本轮指标。
- **流式回复**：通过 SSE 逐 token 推送，首 token 到达即开始显示，无需等待整段生成完成。
- **思考模式**：默认开启；模型输出的 `reasoning` 直接展开显示在回复上方（超过 260px 内部滚动）。
- **附件输入**：点击回形针、拖入对话区或直接 `Ctrl+V` 粘贴截图；图片以 base64 多模态格式发送（单张 ≤3 MB、最多 4 张），文本文件（txt/md/json/csv/log/yaml/py/sh 等，≤200 KB）以 `<file>` 块并入消息。非多模态模型上传图片会提示不支持。
- **无额外限制**：不强制 `max_tokens` 与温度，由引擎按默认策略生成（可用上下文上限内）。
- **安全**：请求由中心服务转发到 HEAD 节点的模型端口，API Key 从容器 Docker secret（`/run/secrets/model-api-key`）读取并缓存，不写入数据库、不返回给前端。

## API Key 管理

模型菜单里的 API Key 页面用于对接客户端：

- **当前 Key**：默认遮罩显示，可一键展开或复制；来源标注为容器 secret 或容器环境变量。
- **Endpoint / 模型**：显示当前服务的访问地址与模型名。
- **curl 示例**：自动生成带 `Authorization` 头的完整命令（含 `stream: true`），一键复制。
- Key 仅在请求时读取并缓存于内存，不写入日志或审计明细。

要更换服务端 Key，需要修改容器启动参数（`--api-key` 或 `VLLM_API_KEY`）并重启模型服务。

## 告警规则

告警中心页面直接展示当前生效的全部阈值，可在线修改：

- CPU、内存、GPU 负载达到阈值产生“警告”级告警
- 温度达到阈值、节点离线产生“严重”级告警
- 保存后下一轮采集生效；与“系统设置 → 告警阈值”是同一份配置，两处修改互相同步
- 支持一键恢复默认值，修改记录写入操作审计

## 界面帮助

各页面标题与关键面板标题旁的 `?` 按钮会弹出对应功能的机制说明，包括采集方式与指标口径、模型服务识别与启停规则、告警规则含义等，便于新成员快速上手。

## systemd

当前用户服务：

```bash
./scripts/install_service.sh
```

安装器会使用实际项目目录、当前用户和 `SPARK_PYTHON` 动态生成 unit，不再依赖固定的 `~/spark-monitor` 或 `/usr/bin/python3`。服务名可通过 `SPARK_UNIT_NAME` 修改。

系统级服务：

```bash
sudo ./scripts/install_system_service.sh
```

调用前在 `.env` 中设置 `SPARK_SYSTEM_USER`，或用 `sudo SPARK_SYSTEM_USER=<user> ./scripts/install_system_service.sh` 指定运行账号。模板位于 `deploy/spark-grid.system.service`。

安装器会根据 `SPARK_MONITOR_DATA`、`SPARK_DATABASE_PATH` 和 `SPARK_CREDENTIAL_KEY_FILE` 的实际位置生成 systemd 可写目录，因此数据库或凭据密钥可以放在项目目录之外。安装时仍会拒绝把 `/` 暴露为可写路径。

公网或跨网访问建议通过 SSH 隧道和 TLS 反向代理，不要直接暴露模型、节点 SSH 或管理端口。安装网关 unit：

```bash
sudo ./scripts/install_gateway_units.sh
```

该命令默认只生成 `/etc/spark-grid/tls.env`、`/etc/spark-grid/tunnel.env` 和对应 unit，不自动启动。确认证书、密钥、目标地址和端口后，可设置 `SPARK_GATEWAY_ENABLE=1` 再执行，或直接使用 `systemctl` 启用。相关示例文件：

```text
deploy/spark-grid-tunnel.service
deploy/tunnel.env.example
deploy/spark-grid-tls.service
deploy/tls.env.example
deploy/tls_proxy.py
```

TLS 代理读取 `SPARK_TLS_LISTEN`、`SPARK_TLS_PORT`、`SPARK_TLS_TARGET`、`SPARK_TLS_CERT`、`SPARK_TLS_KEY` 等变量，也支持同名命令行参数覆盖。隧道读取 `SPARK_TUNNEL_BIND`、`SPARK_TUNNEL_LOCAL_PORT`、`SPARK_TUNNEL_TARGET_HOST`、`SPARK_TUNNEL_TARGET_PORT`、SSH 用户/端口/私钥等变量。配置目录可通过 `SPARK_GATEWAY_CONFIG_DIR` 修改。

## 测试

```bash
python3 -m unittest discover -s tests -v
node --check app/static/js/app.js
```

## 安全建议

- 通过 HTTPS 访问控制台，并设置 `SPARK_COOKIE_SECURE=1`
- 不要提交 `.env`、`data/`、SQLite 数据库、`data/ssh-secret.key`、日志或 SSH 私钥
- 使用独立管理员密码和专用 SSH 密钥
- 限制控制台和 SSH 隧道监听范围
- 定期轮换 SSH 密钥和管理员密码

安全问题请通过 GitHub Security Advisories 私下报告，不要在公开 Issue 中粘贴凭据或日志。

## 更新记录

| 日期 | 提交 | 新增 / 变更 |
|:---|:---|:---|
| 2026-11-24 | `6355107` | **会话测试增强**：支持 `Ctrl+V` 粘贴截图、拖入或选择图片与文本文件（多模态，图片 ≤3 MB / 4 张，文本 ≤200 KB）；思考默认开启并直接展开；模型别名去重；模型管理中切换为专属侧栏（总览 / 会话测试 / 服务控制 / 运行日志 / 返回集群） |
| 2026-11-24 | `d5015f4` | **流式回复**：会话测试改为 SSE 逐 token 推送，首 token 到达即开始显示 |
| 2026-11-24 | `be09160` | **修复**：模型服务 `/health` 端口探测——端口列表分隔符错误，导致运行中的服务被误判为"引擎加载中" |
| 2026-11-24 | `70aed9a` | **状态精细识别**：按 systemd / 容器 / `/health` 三层判定「启动中（含时长与超时进度）/ 已就绪 / 引擎加载中 / 脚本已完成 / 启动失败（退出码、超时、信号）」 |
| 2026-11-24 | `2543317` | **会话测试**：对话式验证推理服务，展示输入 / 输出 token、TTFT、总耗时、吞吐与会话累计 |
| 2026-11-24 | `9c07d54` | **修复**：确认弹窗在动作执行后不自动关闭 |
| 2026-11-24 | `8656e7d` | **模型服务生命周期管理**：自动识别 HEAD 节点 `model_*.service`，支持单服务启动 / 停止、一键切换（自动停止其他模型与 guard 守护单元）、后台任务与进度轮询、操作审计；**告警规则在线编辑**（5 项阈值、恢复默认）；**界面帮助**：各页面与关键面板 `?` 机制说明 |
| 2026-11-24 | `1b44afa` | 文档：README 增加更新记录时间轴 |
| 2026-11-21 | `ebc7885` | **可配置网络与 SSH 发现**：扫描网段、SSH 账号 / 端口 / 密钥、远程 Python、GPU 查询命令均可在页面配置；局域网发现支持自定义 CIDR 与多网卡；新增 SSH 密码认证（paramiko）、凭据加密存储、运行时配置分层与命令编排 |
| 2026-11-21 | `23b7b1a` | 兼容 Python 3.10 环境下的纳秒精度 Docker 时间戳 |
| 2026-11-21 | `2d81195` | **首个开源版本**：节点总览与 24 小时指标历史、SSH 资产发现与批量纳管、CPU / GPU / 内存 / 温度 / 离线告警、登录鉴权与操作审计、vLLM / SGLang 模型运行态与日志查看、CSV 导出 |

## 许可证

MIT License，详见 [LICENSE](LICENSE)。

本项目与 NVIDIA 无隶属或官方背书关系。
