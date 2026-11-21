from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import threading
import time
from collections import defaultdict, deque
from typing import Any

from .db import Database


PBKDF2_ITERATIONS = 310_000


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS
    )
    return "pbkdf2_sha256${}${}${}".format(
        PBKDF2_ITERATIONS,
        base64.urlsafe_b64encode(salt).decode("ascii"),
        base64.urlsafe_b64encode(digest).decode("ascii"),
    )


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, iterations, salt_b64, digest_b64 = encoded.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        salt = base64.urlsafe_b64decode(salt_b64.encode("ascii"))
        expected = base64.urlsafe_b64decode(digest_b64.encode("ascii"))
        actual = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), salt, int(iterations)
        )
        return hmac.compare_digest(actual, expected)
    except (TypeError, ValueError):
        return False


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create_session(
    db: Database,
    user_id: int,
    remote_addr: str,
    user_agent: str,
    session_ttl_hours: int,
) -> tuple[str, dict[str, Any]]:
    token = secrets.token_urlsafe(32)
    csrf_token = secrets.token_urlsafe(24)
    expires_at = int(time.time()) + max(1, session_ttl_hours) * 3600
    db.create_session(
        token_hash(token),
        user_id,
        csrf_token,
        expires_at,
        remote_addr,
        user_agent,
    )
    return token, {
        "csrf_token": csrf_token,
        "expires_at": expires_at,
    }


class LoginThrottle:
    def __init__(self) -> None:
        self._attempts: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def allowed(
        self, remote_addr: str, max_attempts: int, window_seconds: int
    ) -> bool:
        now = time.monotonic()
        with self._lock:
            attempts = self._attempts[remote_addr]
            while attempts and now - attempts[0] > max(1, window_seconds):
                attempts.popleft()
            return len(attempts) < max(1, max_attempts)

    def record_failure(
        self, remote_addr: str, window_seconds: int = 600
    ) -> None:
        now = time.monotonic()
        with self._lock:
            attempts = self._attempts[remote_addr]
            while attempts and now - attempts[0] > max(1, window_seconds):
                attempts.popleft()
            attempts.append(now)

    def reset(self, remote_addr: str) -> None:
        with self._lock:
            self._attempts.pop(remote_addr, None)
