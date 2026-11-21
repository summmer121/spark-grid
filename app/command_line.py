from __future__ import annotations

import shlex
from typing import Any


def parse_command(
    value: Any,
    *,
    label: str = "命令",
    allow_empty: bool = False,
    max_length: int = 200,
) -> tuple[str, ...]:
    text = str(value or "").strip()
    if not text:
        if allow_empty:
            return ()
        raise ValueError(f"{label}不能为空")
    if len(text) > max_length:
        raise ValueError(f"{label}不能超过 {max_length} 个字符")
    if any(character in text for character in ("\x00", "\r", "\n")):
        raise ValueError(f"{label}不能包含控制字符")
    try:
        parts = tuple(shlex.split(text, posix=True))
    except ValueError as exc:
        raise ValueError(f"{label}格式无效: {exc}") from exc
    if not parts:
        if allow_empty:
            return ()
        raise ValueError(f"{label}不能为空")
    return parts


def command_text(
    value: Any,
    *,
    label: str = "命令",
    allow_empty: bool = False,
    max_length: int = 200,
) -> str:
    text = str(value or "").strip()
    parse_command(
        text,
        label=label,
        allow_empty=allow_empty,
        max_length=max_length,
    )
    return text
