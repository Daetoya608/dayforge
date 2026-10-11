"""Секреты проекта: значения — только в .env рабочей директории, в БД — только имена.

См. docs/telegram-bot.md#секреты и docs/operations.md#безопасность.
"""

import os
import re
from pathlib import Path

ENV_FILE_MODE = 0o660
SECRET_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{1,63}$")
_LINE_RE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=")


class EnvFile:
    def __init__(self, path: Path) -> None:
        self.path = path

    def names(self) -> set[str]:
        if not self.path.exists():
            return set()
        return {
            match.group(1)
            for line in self.path.read_text().splitlines()
            if (match := _LINE_RE.match(line))
        }

    def has(self, name: str) -> bool:
        return name in self.names()

    def values(self) -> list[str]:
        """Все значения — чтобы маскировать их в логах."""
        if not self.path.exists():
            return []
        result = []
        for line in self.path.read_text().splitlines():
            if _LINE_RE.match(line):
                value = line.split("=", 1)[1].strip()
                result.append(_unquote(value))
        return [v for v in result if v]

    def set(self, name: str, value: str) -> None:
        """Добавляет или заменяет переменную, не трогая остальные строки.

        Права 660: пишет система, читает агент — оба через группу рабочих копий
        (docs/operations.md#пользователи); остальным пользователям сервера доступа нет.
        """
        if not SECRET_NAME_RE.fullmatch(name):
            raise ValueError(f"недопустимое имя секрета: {name!r}")
        lines = self.path.read_text().splitlines() if self.path.exists() else []
        entry = f"{name}={_quote(value)}"
        for index, line in enumerate(lines):
            match = _LINE_RE.match(line)
            if match and match.group(1) == name:
                lines[index] = entry
                break
        else:
            lines.append(entry)

        tmp = self.path.with_name(self.path.name + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, ENV_FILE_MODE)
        with os.fdopen(fd, "w") as file:
            file.write("\n".join(lines) + "\n")
        os.replace(tmp, self.path)
        os.chmod(self.path, ENV_FILE_MODE)


def _quote(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    return f'"{escaped}"'


_ESCAPES = {"n": "\n", '"': '"', "\\": "\\"}


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] == "'":
        return value[1:-1]
    if not (len(value) >= 2 and value[0] == value[-1] == '"'):
        return value
    inner, result, index = value[1:-1], [], 0
    while index < len(inner):
        char = inner[index]
        if char == "\\" and index + 1 < len(inner):
            result.append(_ESCAPES.get(inner[index + 1], char + inner[index + 1]))
            index += 2
        else:
            result.append(char)
            index += 1
    return "".join(result)
