"""VcsProvider для GitHub: REST API через httpx (docs/git-workflow.md#vcsprovider)."""

import base64

import httpx

from dayforge.vcs.base import RepoInfo

API_URL = "https://api.github.com"


class VcsError(Exception):
    pass


class GitHubProvider:
    name = "github"

    def __init__(self, token: str, owner: str, *, client: httpx.AsyncClient | None = None) -> None:
        self._token = token
        self._owner = owner
        self._client = client or httpx.AsyncClient(base_url=API_URL, timeout=30)
        self._client.headers.update(
            {
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            }
        )
        self._login: str | None = None

    async def aclose(self) -> None:
        await self._client.aclose()

    async def create_repo(self, name: str, *, private: bool, description: str) -> RepoInfo:
        # Репозиторий пользователя и организации создаются разными эндпоинтами.
        path = (
            "/user/repos"
            if self._owner == await self._user_login()
            else f"/orgs/{self._owner}/repos"
        )
        response = await self._client.post(
            path,
            json={"name": name, "private": private, "description": description, "auto_init": False},
        )
        _raise_for_status(response, f"создание репозитория {self._owner}/{name}")
        return RepoInfo(owner=response.json()["owner"]["login"], name=response.json()["name"])

    def remote_url(self, repo: RepoInfo) -> str:
        return f"https://github.com/{repo.owner}/{repo.name}.git"

    def web_url(self, repo: RepoInfo) -> str:
        return f"https://github.com/{repo.owner}/{repo.name}"

    def commit_url(self, repo: RepoInfo, sha: str) -> str:
        return f"{self.web_url(repo)}/commit/{sha}"

    def branch_url(self, repo: RepoInfo, branch: str) -> str:
        return f"{self.web_url(repo)}/tree/{branch}"

    async def create_release(self, repo: RepoInfo, tag: str, notes: str) -> str:
        response = await self._client.post(
            f"/repos/{repo.owner}/{repo.name}/releases",
            json={"tag_name": tag, "name": tag, "body": notes},
        )
        _raise_for_status(response, f"релиз {tag} в {repo.owner}/{repo.name}")
        return str(response.json()["html_url"])

    async def delete_repo(self, repo: RepoInfo) -> None:
        response = await self._client.delete(f"/repos/{repo.owner}/{repo.name}")
        if response.status_code != 404:
            _raise_for_status(response, f"удаление репозитория {repo.owner}/{repo.name}")

    def repo_from_remote(self, remote_url: str) -> RepoInfo:
        prefix = "https://github.com/"
        if not remote_url.startswith(prefix):
            raise VcsError(f"не GitHub-репозиторий: {remote_url}")
        owner, _, name = remote_url.removeprefix(prefix).removesuffix(".git").partition("/")
        return RepoInfo(owner=owner, name=name)

    def git_env(self) -> dict[str, str]:
        """Авторизация для git push: заголовок через переменные окружения процесса git.

        Токен не попадает ни в конфиг репозитория, ни на диск, ни в окружение агентов.
        """
        basic = base64.b64encode(f"x-access-token:{self._token}".encode()).decode()
        return {
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
            "GIT_CONFIG_VALUE_0": f"AUTHORIZATION: basic {basic}",
        }

    async def _user_login(self) -> str:
        if self._login is None:
            response = await self._client.get("/user")
            _raise_for_status(response, "проверка токена GitHub")
            self._login = str(response.json()["login"])
        return self._login


def _raise_for_status(response: httpx.Response, action: str) -> None:
    if response.is_success:
        return
    try:
        message = response.json().get("message", response.text)
    except ValueError:
        message = response.text
    raise VcsError(f"{action}: GitHub ответил {response.status_code}: {message}")
