# Spark Grid

Spark Grid 是一个面向 NVIDIA DGX Spark 小规模集群的轻量监控平台。中心服务通过 SSH 主动拉取节点指标，不在节点安装 Agent，也不要求节点额外开放监控端口。

项目最初针对 8 台 DGX Spark 场景设计，也适用于其他可通过 SSH 管理的小型 GPU 集群。

## 功能

- 节点总览、在线状态、CPU、GPU、统一内存、温度和网络吞吐
- 最近 24 小时节点指标历史与可悬停折线图
- 局域网 SSH 资产发现、批量纳管、分组、标签和启停
- CPU、GPU、内存、温度、离线和心跳告警
- 登录鉴权、CSRF 防护、操作审计和 CSV 导出
- vLLM / SGLang 自动识别和模型运行态监控
- Prefill / Decode 最近 30 分钟吞吐折线图
- 模型日志查看、筛选和下载
- 响应式布局，支持桌面、平板和手机

## 架构

- 中心端：Python 标准库 HTTP 服务、SQLite、鉴权、告警和资产管理
- 采集端：无常驻 Agent
- 采集方式：中心端按配置间隔通过 SSH 执行只读 Python 采样脚本
- 局域网发现：中心端扫描管理网段，再通过 `BatchMode` SSH 探测主机身份
- 数据保留：节点历史默认保留 24 小时，模型吞吐历史按当前模型启动周期保存

GB10 的部分驱动版本会在空闲时把 `nvidia-smi utilization.gpu` 固定上报为 `96%`。平台不直接展示该原始值，而是优先使用功耗和 P-State 估算负载，并在详情中展示实际采样口径。

## 快速开始

要求：

- Python 3.10+
- 中心节点可以免交互 SSH 登录全部被监控节点
- 被监控节点提供 `python3` 和 `nvidia-smi`

```bash
git clone https://github.com/<your-account>/spark-grid.git
cd spark-grid
cp .env.example .env
chmod +x scripts/*.sh
```

编辑 `.env` 后检查 SSH：

```bash
./scripts/check_cluster_ssh.sh
```

启动中心服务：

```bash
./scripts/start.sh
```

浏览器打开：

```text
http://<center-host>:6301
```

首次访问会引导创建管理员账号。密码只保存为 PBKDF2-SHA256 加盐哈希。

停止或重启：

```bash
./scripts/stop.sh
./scripts/restart.sh
```

日志位于项目根目录的 `.server.log`。

## SSH 配置

推荐在中心节点的 `~/.ssh/config` 中使用别名：

```sshconfig
Host spark1 spark2 spark3 spark4 spark5 spark6 spark7 spark8
    User spark
    IdentityFile ~/.ssh/id_ed25519_cluster
    IdentitiesOnly yes
    StrictHostKeyChecking accept-new
```

也可以在主机的 DNS 或 `/etc/hosts` 中配置示例地址：

```text
192.0.2.11  spark1
192.0.2.12  spark2
192.0.2.13  spark3
192.0.2.14  spark4
192.0.2.15  spark5
192.0.2.16  spark6
192.0.2.17  spark7
192.0.2.18  spark8
```

`192.0.2.0/24` 是 RFC 5737 文档专用网段，仅用于示例，请替换为实际管理地址。

如果 SSH 别名不是 `spark1` 到 `spark8`，可以覆盖检查脚本的目标列表：

```bash
SPARK_HOSTS="node-a node-b node-c" ./scripts/check_cluster_ssh.sh
```

平台不会保存节点 SSH 密码。生产环境应使用专用密钥、最小权限账号和防火墙限制。

## 局域网发现

中心端会扫描本机管理网段并尝试探测 SSH 主机。发现结果可以在页面中批量纳管。

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

## systemd

当前用户服务：

```bash
./scripts/install_service.sh
```

系统级服务示例位于：

```text
deploy/spark-grid.system.service
```

公网或跨网访问建议通过 SSH 隧道和 TLS 反向代理，不要直接暴露模型、节点 SSH 或管理端口。示例文件：

```text
deploy/spark-grid-tunnel.service
deploy/tunnel.env.example
deploy/spark-grid-tls.service
deploy/tls_proxy.py
```

隧道参数通过 `/etc/spark-grid/tunnel.env` 配置。

## 测试

```bash
python3 -m unittest discover -s tests -v
node --check app/static/js/app.js
```

## 安全建议

- 通过 HTTPS 访问控制台，并设置 `SPARK_COOKIE_SECURE=1`
- 不要提交 `.env`、`data/`、SQLite 数据库、日志或 SSH 私钥
- 使用独立管理员密码和专用 SSH 密钥
- 限制控制台和 SSH 隧道监听范围
- 定期轮换 SSH 密钥和管理员密码

安全问题请通过 GitHub Security Advisories 私下报告，不要在公开 Issue 中粘贴凭据或日志。

## 许可证

MIT License，详见 [LICENSE](LICENSE)。

本项目与 NVIDIA 无隶属或官方背书关系。
