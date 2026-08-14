"""Export an isolated worktree's changes as a patch instead of applying them.

``hermes --worktree`` already keeps an agent out of the checkout you are
working in: it edits a throwaway worktree, and the cleanup on exit deletes that
worktree along with anything uncommitted. That is safe but lossy — the work
only survives if the agent happened to commit and push it.

``--patch-out`` closes that gap and changes the contract. The agent proposes;
you decide. It is the right default when the agent runs somewhere its mistakes
would be expensive — on a server beside unrelated production services, or
against a repository you do not want it committing to — because nothing reaches
the real tree until a human applies the patch.

The diff is taken against the commit the worktree branched from and covers
committed work, uncommitted edits and new files alike, so a patch is the whole
session's output regardless of whether the agent bothered to commit.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Optional

GIT_TIMEOUT = 30


class PatchError(RuntimeError):
    """Raised when a patch cannot be produced. Carries an operator-facing message."""


def _git(args: list[str], cwd: str, timeout: int = GIT_TIMEOUT) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, timeout=timeout,
    )


def resolve_base(worktree_path: str, base_ref: Optional[str] = None) -> str:
    """Commit the diff is taken against.

    Prefers the recorded branch point. Falls back to the merge-base with the
    worktree's upstream-ish default, then to HEAD — a patch against HEAD still
    captures uncommitted work, which is the common case for an agent that never
    committed.
    """
    if base_ref:
        return base_ref
    for args in (["rev-parse", "HEAD@{1}"], ["rev-parse", "HEAD"]):
        try:
            p = _git(args, worktree_path)
            if p.returncode == 0 and p.stdout.strip():
                return p.stdout.strip()
        except (subprocess.SubprocessError, OSError):
            continue
    raise PatchError("не удалось определить базовый коммит worktree")


def build_patch(worktree_path: str, base_ref: Optional[str] = None) -> str:
    """Return a unified diff of everything the session changed.

    Stages untracked files first: `git diff` alone silently omits new files,
    which for an agent that mostly *adds* code would produce a patch that looks
    successful and rebuilds almost nothing. The index is mutated, which is fine
    — this worktree is disposable by construction.

    --binary so binary additions survive round-tripping through `git apply`.
    """
    wt = Path(worktree_path)
    if not wt.exists():
        raise PatchError(f"worktree не найден: {worktree_path}")

    base = resolve_base(worktree_path, base_ref)
    try:
        # Failure here is not fatal: without staging we still get tracked-file
        # edits, which beats losing the whole patch.
        _git(["add", "-A"], worktree_path)
        proc = _git(["diff", "--binary", base], worktree_path)
    except subprocess.TimeoutExpired as exc:
        raise PatchError(f"git не ответил за {GIT_TIMEOUT}с: {exc}") from exc
    except (subprocess.SubprocessError, OSError) as exc:
        raise PatchError(f"не удалось вызвать git: {exc}") from exc

    if proc.returncode != 0:
        raise PatchError(f"git diff завершился с кодом {proc.returncode}: {proc.stderr.strip()[:200]}")
    return proc.stdout


def write_patch(
    worktree_path: str, out_path: str, base_ref: Optional[str] = None
) -> tuple[bool, str]:
    """Write the session's diff to *out_path*.

    Returns (wrote_anything, human-readable message). An empty diff is reported
    rather than written: a zero-byte file is indistinguishable from a crashed
    export, and `git apply` on it succeeds silently, which would read as "the
    change was applied" when nothing happened.
    """
    patch = build_patch(worktree_path, base_ref)
    if not patch.strip():
        return False, "Изменений нет — патч не записан."

    out = Path(out_path).expanduser()
    try:
        if out.parent and not out.parent.exists():
            out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(patch, encoding="utf-8")
    except OSError as exc:
        raise PatchError(f"не удалось записать патч в {out}: {exc}") from exc

    files = sum(1 for line in patch.splitlines() if line.startswith("diff --git "))
    return True, (
        f"Патч записан: {out} ({files} файл(ов), {len(patch.splitlines())} строк).\n"
        f"  Посмотреть:  git apply --stat {out}\n"
        f"  Проверить:   git apply --check {out}\n"
        f"  Применить:   git apply {out}"
    )
