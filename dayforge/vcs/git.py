"""Асинхронная обёртка над локальным git."""

import asyncio
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

AGENT_NAME = "Dayforge Agent"
AGENT_EMAIL = "agent@dayforge.local"


class GitError(Exception):
    def __init__(self, args: tuple[str, ...], returncode: int, stderr: str) -> None:
        super().__init__(f"git {' '.join(args)} failed ({returncode}): {stderr.strip()}")
        self.returncode = returncode
        self.stderr = stderr


class MergeConflict(Exception):
    def __init__(self, files: list[str]) -> None:
        super().__init__(f"merge conflict in: {', '.join(files)}")
        self.files = files


@dataclass(frozen=True)
class CommitInfo:
    sha: str
    message: str
    author_email: str
    committed_at: datetime


class Git:
    def __init__(self, cwd: Path) -> None:
        self.cwd = cwd

    async def run(self, *args: str, env: dict[str, str] | None = None) -> str:
        process = await asyncio.create_subprocess_exec(
            "git",
            *args,
            cwd=self.cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            # Никаких интерактивных запросов и локализованных сообщений.
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C", **(env or {})},
        )
        stdout, stderr = await process.communicate()
        if process.returncode != 0:
            raise GitError(args, process.returncode or -1, stderr.decode(errors="replace"))
        return stdout.decode(errors="replace").strip()

    async def init(self, *, branch: str = "main") -> None:
        await self.run("init", "-q", "-b", branch)
        # Рабочую копию пишут система и агент — разные пользователи одной группы.
        await self.run("config", "core.sharedRepository", "group")
        # Фиксированный автор отличает коммиты агента и системы от ручных.
        await self.run("config", "user.name", AGENT_NAME)
        await self.run("config", "user.email", AGENT_EMAIL)

    async def commit_all(self, message: str) -> str:
        await self.run("add", "-A")
        await self.run("commit", "-q", "-m", message)
        return await self.rev_parse("HEAD")

    async def rev_parse(self, ref: str) -> str:
        return await self.run("rev-parse", ref)

    async def ref_exists(self, ref: str) -> bool:
        try:
            await self.run("rev-parse", "--verify", "--quiet", ref)
        except GitError:
            return False
        return True

    async def current_branch(self) -> str:
        return await self.run("branch", "--show-current")

    async def create_branch(self, name: str, start: str = "HEAD") -> None:
        await self.run("branch", name, start)

    async def checkout(self, branch: str) -> None:
        await self.run("checkout", "-q", branch)

    async def checkout_new(self, branch: str, start: str) -> None:
        await self.run("checkout", "-q", "-b", branch, start)

    async def delete_branch(self, branch: str) -> None:
        await self.run("branch", "-q", "-D", branch)

    async def is_clean(self) -> bool:
        return await self.run("status", "--porcelain") == ""

    async def merge(self, branch: str, *, message: str, abort_on_conflict: bool = True) -> str:
        """merge --no-ff в текущую ветку. При конфликте — MergeConflict.

        abort_on_conflict=False оставляет конфликт в рабочей копии: его разрешает агент.
        """
        try:
            await self.run("merge", "-q", "--no-ff", "-m", message, branch)
        except GitError:
            files = (await self.run("diff", "--name-only", "--diff-filter=U")).splitlines()
            if not files:
                raise
            if abort_on_conflict:
                await self.run("merge", "--abort")
            raise MergeConflict(files) from None
        return await self.rev_parse("HEAD")

    async def merge_in_progress(self) -> bool:
        return await self.ref_exists("MERGE_HEAD")

    async def tag(self, name: str, message: str) -> None:
        await self.run("tag", "-a", name, "-m", message)

    async def commits(self, revision_range: str) -> list[CommitInfo]:
        """Коммиты диапазона (`dev..feature/T1`), от старых к новым."""
        out = await self.run(
            "log", "--reverse", "--format=%H%x1f%ae%x1f%ct%x1f%B%x1e", revision_range
        )
        result = []
        for record in out.split("\x1e"):
            record = record.strip()
            if not record:
                continue
            sha, email, timestamp, message = record.split("\x1f", 3)
            result.append(
                CommitInfo(
                    sha=sha,
                    message=message.strip(),
                    author_email=email,
                    committed_at=datetime.fromtimestamp(int(timestamp), UTC),
                )
            )
        return result

    async def has_remote(self, name: str = "origin") -> bool:
        return name in (await self.run("remote")).splitlines()

    async def add_remote(self, url: str, name: str = "origin") -> None:
        await self.run("remote", "add", name, url)

    async def push(
        self, *refspecs: str, env: dict[str, str], remote: str = "origin", tags: bool = False
    ) -> None:
        args = ["push", "-q", remote, *refspecs]
        if tags:
            args.append("--tags")
        await self.run(*args, env=env)

    async def delete_remote_branch(
        self, branch: str, *, env: dict[str, str], remote: str = "origin"
    ) -> None:
        await self.run("push", "-q", remote, "--delete", branch, env=env)
