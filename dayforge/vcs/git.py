"""Асинхронная обёртка над локальным git."""

import asyncio
import os
from pathlib import Path

AGENT_NAME = "Dayforge Agent"
AGENT_EMAIL = "agent@dayforge.local"


class GitError(Exception):
    def __init__(self, args: tuple[str, ...], returncode: int, stderr: str) -> None:
        super().__init__(f"git {' '.join(args)} failed ({returncode}): {stderr.strip()}")
        self.returncode = returncode
        self.stderr = stderr


class Git:
    def __init__(self, cwd: Path) -> None:
        self.cwd = cwd

    async def run(self, *args: str) -> str:
        process = await asyncio.create_subprocess_exec(
            "git",
            *args,
            cwd=self.cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            # Никаких интерактивных запросов и локализованных сообщений.
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C"},
        )
        stdout, stderr = await process.communicate()
        if process.returncode != 0:
            raise GitError(args, process.returncode or -1, stderr.decode(errors="replace"))
        return stdout.decode(errors="replace").strip()

    async def init(self, *, branch: str = "main") -> None:
        await self.run("init", "-q", "-b", branch)
        # Фиксированный автор отличает коммиты агента и системы от ручных.
        await self.run("config", "user.name", AGENT_NAME)
        await self.run("config", "user.email", AGENT_EMAIL)

    async def commit_all(self, message: str) -> str:
        await self.run("add", "-A")
        await self.run("commit", "-q", "-m", message)
        return await self.rev_parse("HEAD")

    async def rev_parse(self, ref: str) -> str:
        return await self.run("rev-parse", ref)

    async def current_branch(self) -> str:
        return await self.run("branch", "--show-current")

    async def create_branch(self, name: str, start: str = "HEAD") -> None:
        await self.run("branch", name, start)

    async def is_clean(self) -> bool:
        return await self.run("status", "--porcelain") == ""
