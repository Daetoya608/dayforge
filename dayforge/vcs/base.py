"""API git-хостинга (docs/git-workflow.md#vcsprovider).

Сами git-операции выполняются локальным git и от провайдера не зависят.
"""

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, kw_only=True)
class RepoInfo:
    owner: str
    name: str


class VcsProvider(Protocol):
    name: str

    async def create_repo(self, name: str, *, private: bool, description: str) -> RepoInfo: ...

    def remote_url(self, repo: RepoInfo) -> str: ...

    def web_url(self, repo: RepoInfo) -> str: ...

    def commit_url(self, repo: RepoInfo, sha: str) -> str: ...

    def branch_url(self, repo: RepoInfo, branch: str) -> str: ...

    async def create_release(self, repo: RepoInfo, tag: str, notes: str) -> str: ...
