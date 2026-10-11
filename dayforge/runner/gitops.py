"""git-шаги раннера поверх рабочей копии проекта: ветки задач, WIP, PLAN.md, тесты шлюза."""

import asyncio
import contextlib
import os
import re
import signal
from collections import deque
from dataclasses import dataclass
from pathlib import Path

from dayforge.agents.process import launch_prefix
from dayforge.core.projects import suggest_slug
from dayforge.vcs.git import Git

TEST_OUTPUT_LINES = 200
_SLUG_RE = re.compile(r"[^a-z0-9]+")


def branch_name(key: str, title: str) -> str:
    slug = _SLUG_RE.sub("-", suggest_slug(title)).strip("-")[:40].strip("-") or "task"
    return f"feature/{key}-{slug}"


async def save_wip(git: Git, key: str, reason: str) -> bool:
    """Коммитит незакоммиченное в текущую ветку. True — коммит был."""
    if await git.is_clean():
        return False
    await git.commit_all(f"wip({key}): {reason}")
    return True


async def ensure_on_dev(git: Git) -> None:
    """Перед работой с проектом: всё закоммичено, выбрана dev."""
    if await git.merge_in_progress():
        await git.run("merge", "--abort")
    branch = await git.current_branch()
    if branch == "main":
        # В main напрямую не коммитим: изменения переезжают в dev вместе с переключением.
        await git.checkout("dev")
        branch = "dev"
    if not await git.is_clean():
        await git.commit_all(f"wip: незакоммиченные изменения в {branch or 'detached HEAD'}")
    if await git.current_branch() != "dev":
        await git.checkout("dev")


async def checkout_task_branch(git: Git, branch: str) -> None:
    await ensure_on_dev(git)
    if await git.ref_exists(f"refs/heads/{branch}"):
        await git.checkout(branch)
    else:
        await git.checkout_new(branch, "dev")


async def is_ancestor(git: Git, ancestor: str, ref: str) -> bool:
    try:
        await git.run("merge-base", "--is-ancestor", ancestor, ref)
    except Exception:
        return False
    return True


async def write_plan_file(git: Git, text: str) -> bool:
    """PLAN.md в dev; коммит, только если содержимое изменилось."""
    path = git.cwd / "docs" / "PLAN.md"
    if path.exists() and path.read_text() == text:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    await git.run("add", "docs/PLAN.md")
    await git.run("commit", "-q", "-m", "chore(plan): обновлён PLAN.md")
    return True


@dataclass(frozen=True)
class TestRun:
    passed: bool
    output: str


async def run_tests(
    cwd: Path, command: str, *, agent_unix_user: str | None, limit_seconds: float
) -> TestRun:
    """Тесты шлюза — от пользователя агента: код проекта недоверенный."""
    if agent_unix_user:
        argv = [*launch_prefix(agent_unix_user), "test", command]
    else:
        argv = ["sh", "-c", command]
    process = await asyncio.create_subprocess_exec(
        *argv,
        cwd=cwd,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        start_new_session=True,
    )
    lines: deque[str] = deque(maxlen=TEST_OUTPUT_LINES)

    async def read() -> None:
        assert process.stdout is not None
        async for raw in process.stdout:
            lines.append(raw.decode(errors="replace").rstrip("\n"))

    try:
        await asyncio.wait_for(asyncio.gather(read(), process.wait()), limit_seconds)
    except TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        await process.wait()
        lines.append(f"[dayforge] тесты прерваны по таймауту {int(limit_seconds)} с")
        return TestRun(False, "\n".join(lines))
    return TestRun(process.returncode == 0, "\n".join(lines))
