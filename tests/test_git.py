import subprocess

import pytest
from conftest import LEGACY_REPO

from cloudy.executor import CommandBlocked, Executor
from cloudy.git import Git, commit_message
from cloudy.models import Edit
from cloudy.orchestrator import Orchestrator


def git(root, *args):
    return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, check=True).stdout


@pytest.mark.parametrize("command", [
    "git push", "git push --force origin main", "git reset --soft HEAD~1", "git reset --hard", "git clean -fdx",
    "git rebase -i main", "git checkout -- README.md", "git checkout .", "git restore README.md",
    "git stash drop", "git branch -D feature", "git commit --amend -m x", "git commit -m x --no-verify",
    "git rm README.md", "git filter-branch", "git reflog expire --all", "git gc --prune=now",
])
def test_destructive_git_is_refused(tmp_path, command):
    with pytest.raises(CommandBlocked):
        Executor(tmp_path).run(command)


@pytest.mark.parametrize("command", ["git status --porcelain", "git diff --stat", "git log -5", "git add -- a.txt",
                                     "git commit -m 'chore: x'", "git rev-parse HEAD"])
def test_read_only_and_local_commit_are_allowed(tmp_path, command):
    Executor(tmp_path).ensure_allowed(command)


def test_commit_message_is_deterministic():
    edits = [Edit("b.md", "", "x", "docs.clone-url", "r", applied=True),
             Edit("a.yml", "", "x", "ci.masked-failures", "r", applied=True),
             Edit("c.txt", "", "x", "docs.clone-url", "r", applied=False)]
    message = commit_message("Fix the  CI\nnow", edits, conventional=True)
    assert message == commit_message("Fix the  CI\nnow", list(reversed(edits)), conventional=True)
    assert message.splitlines()[0] == "chore: fix the CI now"
    assert "- ci.masked-failures: a.yml" in message and "- docs.clone-url: b.md" in message and "c.txt" not in message
    assert commit_message("x" * 100, edits, conventional=False).splitlines()[0].endswith("…")


def test_stage_refuses_files_with_prior_user_changes(make_repo):
    root = make_repo({"a.txt": "a\n", "b.txt": "b\n"})
    (root / "b.txt").write_text("user edit\n")
    dirty = git(root, "status", "--porcelain").splitlines()
    (root / "a.txt").write_text("cloudy edit\n")
    (root / "b.txt").write_text("user edit + cloudy edit\n")
    edits = [Edit("a.txt", "a\n", "cloudy edit\n", "r", "x", applied=True),
             Edit("b.txt", "user edit\n", "user edit + cloudy edit\n", "r", "x", applied=True)]
    outcome = Git(Executor(root)).stage(edits, dirty)
    assert outcome.staged == ["a.txt"] and "b.txt" in outcome.refused
    assert git(root, "diff", "--cached", "--name-only").split() == ["a.txt"]


def test_commit_refuses_when_other_files_are_staged(make_repo):
    root = make_repo({"a.txt": "a\n", "other.txt": "o\n"})
    (root / "other.txt").write_text("staged by the user\n")
    git(root, "add", "other.txt")
    (root / "a.txt").write_text("new\n")
    helper = Git(Executor(root))
    outcome = helper.commit(helper.stage([Edit("a.txt", "a\n", "new\n", "r", "x", applied=True)], []), "msg")
    assert outcome.commit is None and "other files are staged (other.txt)" in outcome.error


def test_apply_with_commit_commits_only_cloudy_files(make_repo, tmp_path):
    root = make_repo(LEGACY_REPO | {"notes.txt": "mine\n"})
    (root / "notes.txt").write_text("my unrelated work\n")  # user change that must stay uncommitted
    head = git(root, "rev-parse", "HEAD").strip()
    session = Orchestrator(root, apply=True, state_dir=tmp_path / "s", git_action="commit").run("fix the CI")
    data = session.to_dict()
    assert data["git"]["commit"] and not data["git"]["error"]
    assert git(root, "rev-parse", "HEAD~1").strip() == head  # exactly one new local commit
    committed = git(root, "show", "--name-only", "--format=", "HEAD").split()
    assert sorted(committed) == sorted({e["path"] for e in data["edits"] if e["applied"]})
    assert git(root, "log", "-1", "--format=%s").strip() == "chore: fix the CI"
    assert git(root, "status", "--porcelain").strip() == "M notes.txt"
    assert ".github/workflows/ci.yml" in data["git_diff_stat"]  # recorded before the commit: what was committed


def test_stage_and_commit_require_apply(make_repo, tmp_path):
    from cloudy.cli import main
    root = make_repo({"a.txt": "a\n"})
    assert main(["-C", str(root), "--commit", "--state-dir", str(tmp_path), "fix the CI"]) == 2
