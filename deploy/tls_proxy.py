#!/usr/bin/env python3
from __future__ import annotations

import argparse
import selectors
import socket
import socketserver
import ssl
import sys


TARGET = ("127.0.0.1", 16301)
SSL_CONTEXT: ssl.SSLContext | None = None


class TLSProxyHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        if SSL_CONTEXT is None:
            return
        try:
            client = SSL_CONTEXT.wrap_socket(self.request, server_side=True)
        except (ssl.SSLError, OSError):
            return
        try:
            upstream = socket.create_connection(TARGET, timeout=5)
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
                events = selector.select(timeout=30)
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


def main() -> None:
    parser = argparse.ArgumentParser(description="TLS reverse proxy for Spark Grid")
    parser.add_argument("--listen", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=30001)
    parser.add_argument("--target", default="127.0.0.1:16301")
    parser.add_argument("--cert", required=True)
    parser.add_argument("--key", required=True)
    args = parser.parse_args()
    global SSL_CONTEXT, TARGET
    target_host, target_port = args.target.rsplit(":", 1)
    TARGET = (target_host, int(target_port))
    SSL_CONTEXT = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    SSL_CONTEXT.minimum_version = ssl.TLSVersion.TLSv1_2
    SSL_CONTEXT.load_cert_chain(args.cert, args.key)
    server = ThreadedTLSServer((args.listen, args.port), TLSProxyHandler)
    print(f"Spark Grid TLS proxy listening on {args.listen}:{args.port}", flush=True)
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
