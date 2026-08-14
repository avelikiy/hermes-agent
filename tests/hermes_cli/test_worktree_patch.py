"""Exporting an isolated worktree's work as a patch instead of applying it.

The worktree cleanup deletes uncommitted work by design, so these cover the
cases where a naive `git diff` would quietly lose most of a session: new files
the agent created, and work it never committed.
"""

from __future__ import annotations

import subprocess

import pytest

from hermes_cli.worktree_patch import PatchError, build_patch, write_patch


def _git(args, cwd):
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)


@pytest.fixture
def worktree(tmp_path):
    """A repo with an isolated worktree branched off a known base commit."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init", "-q"], repo)
    _git(["config", "user.email", "t@t"], repo)
    _git(["config", "user.name", "t"], repo)
    (repo / "a.txt").write_text("base line\n")
    _git(["add", "-A"], repo)
    _git(["commit", "-qm", "init"], repo)
    base = _git(["rev-parse", "HEAD"], repo).stdout.strip()
    wt = repo / ".worktrees" / "wt"
    _git(["worktree", "add", "-q", str(wt), "-b", "test/wt", "HEAD"], repo)
    return {"repo": repo, "wt": wt, "base": base}


def test_captures_edits_new_files_and_uncommitted_work(worktree):
    """The three ways a session's output shows up — all must survive."""
    wt = worktree["wt"]
    (wt / "a.txt").write_text("base line\nmodified\n")     # edit, uncommitted
    (wt / "new.py").write_text("brand new\n")              # new file, committed
    _git(["add", "new.py"], wt)
    _git(["commit", "-qm", "work"], wt)
    (wt / "extra.txt").write_text("uncommitted\n")         # new file, uncommitted

    patch = build_patch(str(wt), worktree["base"])
    for name in ("a.txt", "new.py", "extra.txt"):
        assert name in patch, f"{name} отсутствует в патче"


def test_patch_applies_to_a_clean_checkout(tmp_path, worktree):
    """The real contract: a human can apply it later, on a tree that never saw
    the agent."""
    wt = worktree["wt"]
    (wt / "a.txt").write_text("base line\nmodified\n")
    (wt / "new.py").write_text("brand new\n")
    out = tmp_path / "result.patch"
    wrote, _ = write_patch(str(wt), str(out), worktree["base"])
    assert wrote

    clean = tmp_path / "clean"
    subprocess.run(["git", "clone", "-q", str(worktree["repo"]), str(clean)],
                   capture_output=True)
    assert _git(["apply", "--check", str(out)], clean).returncode == 0
    assert _git(["apply", str(out)], clean).returncode == 0
    assert (clean / "new.py").read_text() == "brand new\n"
    assert "modified" in (clean / "a.txt").read_text()


def test_no_changes_writes_nothing(tmp_path, worktree):
    """An empty patch file is indistinguishable from a crashed export, and
    `git apply` on it succeeds silently — so don't create one."""
    out = tmp_path / "empty.patch"
    wrote, msg = write_patch(str(worktree["wt"]), str(out), worktree["base"])
    assert wrote is False
    assert not out.exists()
    assert "Изменений нет" in msg


def test_creates_missing_parent_directory(tmp_path, worktree):
    (worktree["wt"] / "a.txt").write_text("changed\n")
    out = tmp_path / "nested" / "dir" / "out.patch"
    wrote, _ = write_patch(str(worktree["wt"]), str(out), worktree["base"])
    assert wrote and out.exists()


def test_missing_worktree_is_reported_not_raised_raw(tmp_path):
    with pytest.raises(PatchError, match="worktree не найден"):
        build_patch(str(tmp_path / "nope"), None)


def test_works_without_an_explicit_base(worktree):
    """Falls back sensibly when the base commit wasn't recorded."""
    (worktree["wt"] / "a.txt").write_text("base line\nlocal edit\n")
    assert "a.txt" in build_patch(str(worktree["wt"]), None)
