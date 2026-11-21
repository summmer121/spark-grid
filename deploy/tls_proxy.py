#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import selectors
import socket
import socketserver
import ssl
import sys
from dataclasses import dataclass
from typing import Mapping, Sequence


@dataclass(frozen=True)
class ProxyConfig:
    listen: str
    port: int
    target_host: str
    target_port: int
    cert: str
    key: str
    connect_timeout: float
    poll_timeout: float

    @property
    def target(self) -> tuple[str, int]:
        return self.target_host, self.target_port


CONFIG: ProxyConfig | None = None
SSL_CONTEXT: ssl.SSLContext | None = None


class TLSProxyHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        if SSL_CONTEXT is None:
            return
        try:
            client = SSL_CONTEXT.wrap_socket(self.request, server_side=True)
        except (ssl.SSLError, OSError):
            return
        if CONFIG is None:
            client.close()
            return
        try:
            upstream = socket.create_connection(
                CONFIG.target, timeout=CONFIG.connect_timeout
            )
        except OSError:
            client.close()
            return
        client.settimeout(None)
        upstream.settimeout(None)
        selector = selectors.DefaultSelector()
        selector.register(client, selectors.EVENT_READ, upstream)
        selector.register(upstream, selectors.EVENT_READ, client)
        try:
            while True:
                events = selector.select(timeout=CONFIG.poll_timeout)
                if not events:
                    continue
                for key, _ in events:
                    source = key.fileobj
                    destination = key.data
                    data = source.recv(65536)
                    if not data:
                        return
                    destination.sendall(data)
        except (OSError, ssl.SSLError):
            return
        finally:
            selector.close()
            client.close()
            upstream.close()


class ThreadedTLSServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def _target_parts(value: str) -> tuple[str, int]:
    target = value.strip()
    if not target:
        raise ValueError("TLS target 不能为空")
    if target.startswith("["):
        closing = target.find("]")
        if closing < 0 or target[closing + 1 : closing + 2] != ":":
            raise ValueError("IPv6 目标格式应为 [地址]:端口")
        host = target[1:closing]
        port_text = target[closing + 2 :]
    else:
        host, separator, port_text = target.rpartition(":")
        if not separator:
            raise ValueError("TLS target 格式应为 主机:端口")
    if not host:
        raise ValueError("TLS target 主机不能为空")
    try:
        port = int(port_text)
    except ValueError as exc:
        raise ValueError("TLS target 端口必须是整数") from exc
    if not 1 <= port <= 65535:
        raise ValueError("TLS target 端口必须在 1 到 65535 之间")
    return host, port


def _positive_float(env: Mapping[str, str], name: str, default: str) -> float:
    raw = env.get(name, default).strip() or default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} 必须是数字") from exc
    if value <= 0:
        raise ValueError(f"{name} 必须大于 0")
    return value


def _build_parser(env: Mapping[str, str] | None = None) -> argparse.ArgumentParser:
    current_env = os.environ if env is None else env
    parser = argparse.ArgumentParser(description="TLS reverse proxy for Spark Grid")
    parser.add_argument(
        "--listen",
        default=None,
        help=f"监听地址，默认读取 SPARK_TLS_LISTEN 或 {current_env.get('SPARK_TLS_LISTEN', '0.0.0.0')}",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help="监听端口，默认读取 SPARK_TLS_PORT 或 30001",
    )
    parser.add_argument(
        "--target",
        default=None,
        help="上游 主机:端口，默认读取 SPARK_TLS_TARGET 或 127.0.0.1:16301",
    )
    parser.add_argument(
        "--cert",
        default=None,
        help="TLS 证书路径，默认读取 SPARK_TLS_CERT",
    )
    parser.add_argument(
        "--key",
        default=None,
        help="TLS 私钥路径，默认读取 SPARK_TLS_KEY",
    )
    parser.add_argument(
        "--connect-timeout",
        type=float,
        default=None,
        help="上游连接超时，默认读取 SPARK_TLS_CONNECT_TIMEOUT 或 5",
    )
    parser.add_argument(
        "--poll-timeout",
        type=float,
        default=None,
        help="双向转发轮询间隔，默认读取 SPARK_TLS_POLL_TIMEOUT 或 30",
    )
    return parser


def resolve_config(
    args: argparse.Namespace, env: Mapping[str, str] | None = None
) -> ProxyConfig:
    current_env = os.environ if env is None else env
    listen = (args.listen or current_env.get("SPARK_TLS_LISTEN") or "0.0.0.0").strip()
    if not listen:
        raise ValueError("TLS 监听地址不能为空")

    port_text = str(
        args.port
        if args.port is not None
        else current_env.get("SPARK_TLS_PORT", "30001")
    ).strip()
    try:
        port = int(port_text)
    except ValueError as exc:
        raise ValueError("TLS 监听端口必须是整数") from exc
    if not 1 <= port <= 65535:
        raise ValueError("TLS 监听端口必须在 1 到 65535 之间")

    target = args.target or current_env.get("SPARK_TLS_TARGET") or "127.0.0.1:16301"
    target_host, target_port = _target_parts(target)
    cert = (args.cert or current_env.get("SPARK_TLS_CERT") or "").strip()
    key = (args.key or current_env.get("SPARK_TLS_KEY") or "").strip()
    if not cert or not key:
        raise ValueError("请通过 SPARK_TLS_CERT/SPARK_TLS_KEY 或 --cert/--key 配置证书和私钥")

    connect_timeout = (
        args.connect_timeout
        if args.connect_timeout is not None
        else _positive_float(current_env, "SPARK_TLS_CONNECT_TIMEOUT", "5")
    )
    poll_timeout = (
        args.poll_timeout
        if args.poll_timeout is not None
        else _positive_float(current_env, "SPARK_TLS_POLL_TIMEOUT", "30")
    )
    if connect_timeout <= 0:
        raise ValueError("TLS 连接超时必须是正数")
    if poll_timeout <= 0:
        raise ValueError("TLS 轮询间隔必须是正数")

    return ProxyConfig(
        listen=listen,
        port=port,
        target_host=target_host,
        target_port=target_port,
        cert=cert,
        key=key,
        connect_timeout=connect_timeout,
        poll_timeout=poll_timeout,
    )


def main(argv: Sequence[str] | None = None) -> None:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        config = resolve_config(args)
    except ValueError as exc:
        parser.error(str(exc))

    global CONFIG, SSL_CONTEXT
    CONFIG = config
    SSL_CONTEXT = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    SSL_CONTEXT.minimum_version = ssl.TLSVersion.TLSv1_2
    SSL_CONTEXT.load_cert_chain(config.cert, config.key)
    if ":" in config.listen:
        ThreadedTLSServer.address_family = socket.AF_INET6
    server = ThreadedTLSServer((config.listen, config.port), TLSProxyHandler)
    print(
        "Spark Grid TLS proxy listening on "
        f"{config.listen}:{config.port} -> "
        f"{config.target_host}:{config.target_port}",
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"TLS proxy failed: {exc}", file=sys.stderr, flush=True)
        raise
