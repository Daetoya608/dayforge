from pathlib import Path

import pytest

from dayforge.vcs.git import AGENT_EMAIL, Git, GitError, MergeConflict


@pytest.fixture
async def repo(tmp_path: Path) -> Git:
    git = Git(tmp_path / "repo")
    git.cwd.mkdir()
    await git.init()
    (git.cwd / "README.md").write_text("hello\n")
    await git.commit_all("chore: init")
    await git.create_branch("dev")
    return git


async def commit_file(git: Git, name: str, text: str, message: str) -> str:
    (git.cwd / name).write_text(text)
    return await git.commit_all(message)


async def test_feature_branch_merges_into_dev_with_merge_commit(repo: Git) -> None:
    await repo.checkout_new("feature/T1-x", "dev")
    await commit_file(repo, "a.txt", "a\n", "feat(T1): a")
    await commit_file(repo, "b.txt", "b\n", "test(T1): b")

    commits = await repo.commits("dev..feature/T1-x")
    assert [c.message for c in commits] == ["feat(T1): a", "test(T1): b"]
    assert commits[0].author_email == AGENT_EMAIL
    assert commits[0].committed_at.tzinfo is not None

    await repo.checkout("dev")
    sha = await repo.merge("feature/T1-x", message="merge(T1): x")

    assert await repo.run("log", "-1", "--format=%s %P", "dev") == (
        f"merge(T1): x {await repo.rev_parse('main')} {await repo.rev_parse('feature/T1-x')}"
    )
    assert sha == await repo.rev_parse("dev")
    await repo.delete_branch("feature/T1-x")
    assert not await repo.ref_exists("feature/T1-x")


async def prepare_conflict(repo: Git) -> None:
    await repo.checkout_new("feature/T2-x", "dev")
    await commit_file(repo, "README.md", "feature\n", "feat(T2): readme")
    await repo.checkout("dev")
    await commit_file(repo, "README.md", "dev\n", "chore: readme")
    await repo.checkout("feature/T2-x")


async def test_conflict_is_aborted_by_default(repo: Git) -> None:
    await prepare_conflict(repo)

    with pytest.raises(MergeConflict) as exc:
        await repo.merge("dev", message="merge dev")

    assert exc.value.files == ["README.md"]
    assert not await repo.merge_in_progress()
    assert await repo.is_clean()


async def test_conflict_can_be_left_for_the_agent(repo: Git) -> None:
    await prepare_conflict(repo)

    with pytest.raises(MergeConflict):
        await repo.merge("dev", message="merge dev", abort_on_conflict=False)

    assert await repo.merge_in_progress()
    assert "<<<<<<<" in (repo.cwd / "README.md").read_text()


async def test_push_to_remote_with_tags(repo: Git, tmp_path: Path) -> None:
    remote = tmp_path / "remote.git"
    await Git(tmp_path).run("init", "-q", "--bare", str(remote))
    await repo.add_remote(str(remote))
    await repo.tag("v0.1.0", "first release")

    assert await repo.has_remote()
    await repo.push("main", "dev", env={}, tags=True)

    bare = Git(remote)
    assert await bare.rev_parse("main") == await repo.rev_parse("main")
    assert await bare.ref_exists("refs/tags/v0.1.0")

    await repo.delete_remote_branch("dev", env={})
    assert not await bare.ref_exists("dev")


async def test_errors_carry_stderr(repo: Git) -> None:
    with pytest.raises(GitError) as exc:
        await repo.checkout("no-such-branch")

    assert "no-such-branch" in exc.value.stderr
