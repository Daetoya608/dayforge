import base64
import json

import httpx
import pytest

from dayforge.vcs.base import RepoInfo
from dayforge.vcs.github import API_URL, GitHubProvider, VcsError


def provider(handler, owner: str = "daetoya") -> GitHubProvider:  # type: ignore[no-untyped-def]
    client = httpx.AsyncClient(base_url=API_URL, transport=httpx.MockTransport(handler))
    return GitHubProvider("secret-token", owner, client=client)


async def test_create_repo_for_user() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/user":
            return httpx.Response(200, json={"login": "daetoya"})
        body = json.loads(request.content)
        return httpx.Response(201, json={"name": body["name"], "owner": {"login": "daetoya"}})

    repo = await provider(handler).create_repo("todo-bot", private=True, description="Бот")

    assert repo == RepoInfo(owner="daetoya", name="todo-bot")
    assert requests[1].url.path == "/user/repos"
    assert json.loads(requests[1].content) == {
        "name": "todo-bot",
        "private": True,
        "description": "Бот",
        "auto_init": False,
    }
    assert requests[1].headers["Authorization"] == "Bearer secret-token"


async def test_create_repo_for_organization() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/user":
            return httpx.Response(200, json={"login": "daetoya"})
        assert request.url.path == "/orgs/acme/repos"
        return httpx.Response(201, json={"name": "todo-bot", "owner": {"login": "acme"}})

    repo = await provider(handler, owner="acme").create_repo(
        "todo-bot", private=True, description=""
    )

    assert repo.owner == "acme"


async def test_api_errors_are_readable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/user":
            return httpx.Response(200, json={"login": "daetoya"})
        return httpx.Response(422, json={"message": "name already exists on this account"})

    with pytest.raises(VcsError, match="422: name already exists"):
        await provider(handler).create_repo("todo-bot", private=True, description="")


async def test_create_release_returns_url() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/repos/daetoya/todo-bot/releases"
        assert json.loads(request.content)["tag_name"] == "v0.1.0"
        return httpx.Response(201, json={"html_url": "https://github.com/x/releases/v0.1.0"})

    url = await provider(handler).create_release(
        RepoInfo(owner="daetoya", name="todo-bot"), "v0.1.0", "notes"
    )

    assert url == "https://github.com/x/releases/v0.1.0"


def test_urls_and_git_env() -> None:
    github = provider(lambda r: httpx.Response(500))
    repo = RepoInfo(owner="daetoya", name="todo-bot")

    assert github.remote_url(repo) == "https://github.com/daetoya/todo-bot.git"
    assert github.repo_from_remote(github.remote_url(repo)) == repo
    assert github.commit_url(repo, "abc") == "https://github.com/daetoya/todo-bot/commit/abc"
    assert github.branch_url(repo, "dev") == "https://github.com/daetoya/todo-bot/tree/dev"

    env = github.git_env()
    header = env["GIT_CONFIG_VALUE_0"].removeprefix("AUTHORIZATION: basic ")
    assert base64.b64decode(header).decode() == "x-access-token:secret-token"
    assert env["GIT_CONFIG_KEY_0"] == "http.https://github.com/.extraheader"


async def test_delete_repo_ignores_missing() -> None:
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(f"{request.method} {request.url.path}")
        return httpx.Response(404 if "gone" in request.url.path else 204)

    github = provider(handler)
    await github.delete_repo(RepoInfo(owner="daetoya", name="todo-bot"))
    await github.delete_repo(RepoInfo(owner="daetoya", name="gone"))

    assert paths == ["DELETE /repos/daetoya/todo-bot", "DELETE /repos/daetoya/gone"]
