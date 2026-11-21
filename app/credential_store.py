from __future__ import annotations

import os
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken


class CredentialCipher:
    """Encrypt reusable SSH passwords with a local key file."""

    def __init__(self, key_path: Path):
        self.key_path = key_path
        self.key_path.parent.mkdir(parents=True, exist_ok=True)
        self._fernet = Fernet(self._load_or_create_key())

    def _load_or_create_key(self) -> bytes:
        try:
            value = self.key_path.read_bytes().strip()
        except FileNotFoundError:
            value = b""
        if value:
            self._restrict_permissions()
            return value

        generated = Fernet.generate_key()
        try:
            descriptor = os.open(
                self.key_path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
        except FileExistsError:
            return self._load_or_create_key()
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(generated + b"\n")
        finally:
            self._restrict_permissions()
        return generated

    def _restrict_permissions(self) -> None:
        try:
            os.chmod(self.key_path, 0o600)
        except OSError:
            pass

    def encrypt(self, value: str | None) -> str:
        if not value:
            return ""
        return self._fernet.encrypt(value.encode("utf-8")).decode("ascii")

    def decrypt(self, value: str | None) -> str:
        if not value:
            return ""
        try:
            return self._fernet.decrypt(value.encode("ascii")).decode("utf-8")
        except (InvalidToken, UnicodeError, ValueError) as exc:
            raise ValueError("SSH 密码凭据无法解密，请重新设置密码") from exc
