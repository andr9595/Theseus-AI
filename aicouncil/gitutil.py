"""Git helpers: repository inspection, diff capture, and run rollback.

The safety model matters more than the convenience here. Zero-Touch Mode hands
a coding agent a ``--dangerously-skip-permissions`` flag and lets it write to
disk unattended, so before Stage 2 runs we record a *snapshot*: the current
HEAD plus a dangling commit containing every tracked modification and every
untracked file. Rolling back restores that exact tree.

The snapshot is written with ``git stash create``-style plumbing rather than
``git stash push`` so the user's own stash stack is never touched.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

GIT_TIMEOUT = 60


class GitError(RuntimeError):
    """A git plumbing command failed."""


def _run(
    args: List[str],
    cwd: str | Path,
    check: bool = True,
    timeout: int = GIT_TIMEOUT,
    index_file: str | Path | None = None,
) -> subprocess.CompletedProcess:
    """Run a git command with a hardened, non-interactive environment.

    ``index_file`` points git at a scratch index so staging operations can be
    performed without disturbing the user's real, possibly carefully-staged,
    index.
    """
    env = dict(os.environ)
    # Never let git block the pipeline on a credential or editor prompt.
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_EDITOR"] = "true"
    env["GIT_PAGER"] = "cat"
    env.pop("GIT_DIR", None)
    env.pop("GIT_WORK_TREE", None)
    if index_file is not None:
        env["GIT_INDEX_FILE"] = str(index_file)

    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        # GitError is this module's failure abstraction. A raw TimeoutExpired
        # slips past every caller that catches GitError and surfaces as an
        # unhandled crash instead of a message about git.
        raise GitError(f"git {' '.join(args)} timed out after {timeout}s") from exc
    if check and proc.returncode != 0:
        raise GitError(
            f"git {' '.join(args)} failed ({proc.returncode}): "
            f"{proc.stderr.strip() or proc.stdout.strip()}"
        )
    return proc


# --------------------------------------------------------------------------
# Inspection
# --------------------------------------------------------------------------


@dataclass
class RepoStatus:
    """A point-in-time summary of a working tree."""

    path: str
    is_repo: bool = False
    branch: str = ""
    head: str = ""
    head_subject: str = ""
    remote: str = ""
    clean: bool = True
    staged: List[str] = field(default_factory=list)
    modified: List[str] = field(default_factory=list)
    untracked: List[str] = field(default_factory=list)
    error: str = ""

    def to_dict(self) -> Dict:
        return {
            "path": self.path,
            "is_repo": self.is_repo,
            "branch": self.branch,
            "head": self.head,
            "head_subject": self.head_subject,
            "remote": self.remote,
            "clean": self.clean,
            "staged": self.staged,
            "modified": self.modified,
            "untracked": self.untracked,
            "dirty_count": len(self.staged) + len(self.modified) + len(self.untracked),
            "error": self.error,
        }


def repo_root(path: str | Path) -> Optional[str]:
    """Return the top-level directory of the repo containing ``path``."""
    p = Path(path).expanduser()
    if not p.is_dir():
        return None
    try:
        proc = _run(["rev-parse", "--show-toplevel"], p, check=False)
    except (GitError, OSError):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip() or None


def status(path: str | Path) -> RepoStatus:
    """Describe the working tree at ``path``. Never raises."""
    p = str(Path(path).expanduser())
    st = RepoStatus(path=p)

    root = repo_root(p)
    if root is None:
        st.error = "Not a git repository"
        return st

    st.path = root
    st.is_repo = True
    try:
        st.branch = _run(["rev-parse", "--abbrev-ref", "HEAD"], root).stdout.strip()
        head = _run(["rev-parse", "HEAD"], root, check=False)
        if head.returncode == 0:
            st.head = head.stdout.strip()
            st.head_subject = _run(
                ["log", "-1", "--pretty=%s"], root, check=False
            ).stdout.strip()
        else:
            # A freshly `git init`ed repo has no commits yet.
            st.branch = st.branch or "(no commits)"

        remote = _run(["remote", "get-url", "origin"], root, check=False)
        if remote.returncode == 0:
            st.remote = remote.stdout.strip()

        # -z gives NUL-separated records, immune to spaces and quoting.
        porcelain = _run(["status", "--porcelain=v1", "-z"], root).stdout
        for record in _parse_porcelain_z(porcelain):
            code, name = record
            index_state, work_state = code[0], code[1]
            if code == "??":
                st.untracked.append(name)
                continue
            if index_state not in (" ", "?"):
                st.staged.append(name)
            if work_state not in (" ", "?"):
                st.modified.append(name)

        st.clean = not (st.staged or st.modified or st.untracked)
    except (GitError, OSError) as exc:
        st.error = str(exc)
    return st


def _parse_porcelain_z(blob: str) -> List[tuple]:
    """Parse ``git status --porcelain=v1 -z`` output into (code, path) pairs.

    Rename/copy records ("R " / "C ") are followed by a second NUL-terminated
    field holding the original path, which must be consumed so it is not
    mistaken for the next record.
    """
    fields = [f for f in blob.split("\0") if f != ""]
    out: List[tuple] = []
    i = 0
    while i < len(fields):
        entry = fields[i]
        if len(entry) < 4:
            i += 1
            continue
        code, name = entry[:2], entry[3:]
        out.append((code, name))
        i += 2 if code[0] in ("R", "C") else 1
    return out


def working_diff(path: str | Path, max_bytes: int = 400_000) -> str:
    """Unified diff of tracked changes, plus a synthetic diff for new files.

    Untracked files are rendered through ``git diff --no-index`` against
    /dev/null so the UI shows newly created files as additions rather than
    silently omitting them.
    """
    root = repo_root(path)
    if root is None:
        return ""

    chunks: List[str] = []
    tracked = _run(["diff", "HEAD", "--no-color"], root, check=False)
    if tracked.returncode == 0 and tracked.stdout.strip():
        chunks.append(tracked.stdout)
    elif tracked.returncode != 0:
        # No HEAD yet (empty repo): fall back to the index-less diff.
        fallback = _run(["diff", "--no-color"], root, check=False)
        if fallback.stdout.strip():
            chunks.append(fallback.stdout)

    for rel in _untracked_files(root):
        target = Path(root) / rel
        try:
            if target.stat().st_size > 200_000:
                chunks.append(
                    f"diff --git a/{rel} b/{rel}\n"
                    f"new file (skipped: larger than 200 KB)\n"
                )
                continue
        except OSError:
            continue
        # --no-index exits 1 when files differ, which is the expected case.
        proc = _run(
            ["diff", "--no-color", "--no-index", "--", "/dev/null", rel],
            root,
            check=False,
        )
        if proc.stdout:
            chunks.append(proc.stdout)

    # Each chunk already ends with a newline; joining on "\n" would insert a
    # blank line between files that no diff parser expects.
    diff = "".join(chunks)
    if len(diff) > max_bytes:
        diff = diff[:max_bytes] + "\n\n... diff truncated for display ...\n"
    return diff


def _untracked_files(root: str) -> List[str]:
    proc = _run(
        ["ls-files", "--others", "--exclude-standard", "-z"], root, check=False
    )
    return [f for f in proc.stdout.split("\0") if f]


def diff_stat(path: str | Path) -> Dict[str, int]:
    """Return ``{files, insertions, deletions}`` for the current changes."""
    root = repo_root(path)
    result = {"files": 0, "insertions": 0, "deletions": 0}
    if root is None:
        return result

    proc = _run(["diff", "HEAD", "--numstat"], root, check=False)
    lines = [ln for ln in proc.stdout.splitlines() if ln.strip()]
    for line in lines:
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        added, removed = parts[0], parts[1]
        result["files"] += 1
        # Binary files report "-" instead of a count.
        if added.isdigit():
            result["insertions"] += int(added)
        if removed.isdigit():
            result["deletions"] += int(removed)

    for rel in _untracked_files(root):
        result["files"] += 1
        path = Path(root) / rel
        # Counting "lines" in a binary file produces a meaningless number that
        # then inflates the +N shown in the UI. An agent that imports a module
        # to verify its work leaves .pyc files behind, so this is common in
        # practice, not a corner case. git itself reports "-" for binary in
        # --numstat; skip them the same way.
        if _is_binary(path):
            continue
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as fh:
                result["insertions"] += sum(1 for _ in fh)
        except OSError:
            pass
    return result


def _is_binary(path: Path, probe_bytes: int = 8000) -> bool:
    """Heuristic used by git itself: a NUL byte early in the file means binary."""
    try:
        with open(path, "rb") as fh:
            return b"\0" in fh.read(probe_bytes)
    except OSError:
        return False


# --------------------------------------------------------------------------
# Snapshot / rollback
# --------------------------------------------------------------------------


# One ref per snapshot, under a namespace of their own. A single shared ref
# stops protecting an earlier run's commit the moment a later run overwrites
# it, and two app instances pointed at one repository do exactly that.
SNAPSHOT_REF_DIR = "refs/ai-council/snapshots"
# Written by versions that kept exactly one snapshot ref. Nothing reads it any
# more, and left alone it pins one commit alive forever.
LEGACY_SNAPSHOT_REF = "refs/ai-council/snapshot"
# How many snapshot anchors to keep. Unbounded, they would accumulate for the
# life of the repository, each holding a whole worktree's objects alive.
SNAPSHOT_REF_LIMIT = 20


@dataclass
class Snapshot:
    """Everything needed to restore a working tree to a prior state."""

    root: str
    head: str
    commit: str = ""  # commit whose tree is the exact pre-run worktree
    had_changes: bool = False
    ref: str = ""  # anchor keeping `commit` reachable, so gc cannot reap it

    def to_dict(self) -> Dict:
        return {
            "root": self.root,
            "head": self.head,
            "commit": self.commit,
            "had_changes": self.had_changes,
            "ref": self.ref,
        }


def take_snapshot(path: str | Path) -> Optional[Snapshot]:
    """Capture the exact current worktree as a commit object.

    ``git stash create`` is the obvious tool here and it is the wrong one: it
    does not support ``--include-untracked``, so a rollback would then delete
    every file the user had created but not yet committed. For a feature whose
    entire purpose is preventing data loss, that is the one failure mode that
    must not exist.

    Instead this stages the whole worktree into a *scratch index* - leaving the
    user's real index untouched - and writes a tree from it:

        GIT_INDEX_FILE=tmp git add -A     # tracked edits + untracked files
        GIT_INDEX_FILE=tmp git write-tree # -> tree object
        git commit-tree <tree> -p HEAD    # -> commit object

    ``add -A`` honours .gitignore, which pairs correctly with the ``git clean``
    in ``restore_snapshot``: ignored files (build output, virtualenvs) are
    neither captured nor deleted.

    Returns None only when there is genuinely nothing to snapshot (no HEAD).
    Every other failure raises: a Snapshot without a commit records no tree,
    and handing one to ``restore_snapshot`` would reset the worktree to HEAD -
    destroying the very work the snapshot exists to protect.
    """
    root = repo_root(path)
    if root is None:
        return None

    head_proc = _run(["rev-parse", "HEAD"], root, check=False)
    if head_proc.returncode != 0:
        return None  # empty repo: no HEAD to anchor a snapshot to
    snap = Snapshot(root=root, head=head_proc.stdout.strip())

    # A scratch index per snapshot, not a fixed path: the path is shared state
    # between two app instances pointed at the same repository, and two
    # concurrent `add -A` runs writing one index file corrupt each other.
    git_dir = _run(["rev-parse", "--absolute-git-dir"], root).stdout.strip()
    handle, tmp_name = tempfile.mkstemp(
        prefix="ai-council-snapshot-", suffix=".index", dir=git_dir
    )
    os.close(handle)
    tmp_index = Path(tmp_name)
    # git wants the index file either absent or valid, never empty.
    tmp_index.unlink()
    try:
        # Seed the scratch index from HEAD so `add -A` records deletions too.
        _run(["read-tree", snap.head], root, index_file=tmp_index)
        _run(["add", "-A", "."], root, index_file=tmp_index)
        tree = _run(["write-tree"], root, index_file=tmp_index).stdout.strip()
        if not tree:
            raise GitError("git write-tree produced no tree object")
        snap.commit = _run(
            ["commit-tree", tree, "-p", snap.head, "-m", "ai-council snapshot"],
            root,
        ).stdout.strip()
        if not snap.commit:
            raise GitError("git commit-tree produced no commit object")
    finally:
        try:
            tmp_index.unlink()
        except OSError:
            pass

    snap.had_changes = tree != _run(
        ["rev-parse", f"{snap.head}^{{tree}}"], root, check=False
    ).stdout.strip()
    # Anchor the commit so `git gc` cannot reap it mid-run.
    snap.ref = f"{SNAPSHOT_REF_DIR}/{snap.commit[:12]}"
    _run(["update-ref", snap.ref, snap.commit], root, check=False)
    _prune_snapshot_refs(root)
    return snap


def _prune_snapshot_refs(root: str, keep: int = SNAPSHOT_REF_LIMIT) -> None:
    """Drop all but the newest ``keep`` snapshot anchors.

    Best-effort by design: failing to tidy up costs disk, not correctness, and
    must never lose the snapshot that was just taken.
    """
    try:
        _run(["update-ref", "-d", LEGACY_SNAPSHOT_REF], root, check=False)
        listed = _run(
            ["for-each-ref", "--sort=-creatordate", "--format=%(refname)",
             SNAPSHOT_REF_DIR],
            root,
            check=False,
        )
        for ref in listed.stdout.split()[keep:]:
            _run(["update-ref", "-d", ref], root, check=False)
    except (GitError, OSError):
        pass


def restore_snapshot(snap: Snapshot) -> str:
    """Restore the worktree captured by ``snap``. Returns a summary line.

    ``read-tree -u --reset`` makes the index and worktree match the snapshot
    tree exactly in one step - adding, overwriting and deleting as needed - so
    files the agent created are removed and files it deleted come back. The
    final ``reset --mixed`` puts the index back to HEAD, which restores the
    pre-run "these are your uncommitted changes" view.

    One caveat, stated plainly: the staged/unstaged split is not reproduced.
    Everything that was uncommitted before the run is uncommitted after it,
    but changes that were staged come back unstaged.
    """
    root = snap.root
    if not snap.commit:
        # No commit means no captured tree, so there is nothing to restore
        # *to*. Reading that as "the tree was clean" and resetting is how a
        # failed snapshot turns into the data loss it exists to prevent.
        raise GitError(
            "This snapshot is incomplete - it captured no tree. Refusing to "
            "reset the working tree."
        )

    try:
        # Order is load-bearing. Clean must run *before* the snapshot is laid
        # down, not after: once restored, the user's own untracked files look
        # exactly like the agent's, and a trailing `clean` would delete them.
        _run(["reset", "--hard", "-q", snap.head], root)
        _run(["clean", "-fdq"], root, check=False)
        _run(["read-tree", "-u", "--reset", snap.commit], root)
        # The snapshot's extra files are staged at this point; resetting the
        # index back to HEAD returns them to being untracked.
        _run(["reset", "--mixed", "-q", snap.head], root)
    except (GitError, OSError) as exc:
        raise GitError(f"Rollback failed: {exc}") from exc

    if snap.had_changes:
        return (
            f"Restored to {snap.head[:8]} with your pre-run uncommitted "
            f"changes reapplied (as unstaged)."
        )
    return f"Restored to clean {snap.head[:8]}."


def list_directory(path: str | Path) -> Dict:
    """List subdirectories of ``path`` for the GUI's directory picker.

    Returns only directories - the picker selects repositories, not files -
    and flags which entries are themselves git repos so the UI can badge them.
    """
    p = Path(path).expanduser()
    try:
        p = p.resolve(strict=True)
    except (OSError, RuntimeError):
        return {"path": str(path), "error": "No such directory", "entries": []}
    if not p.is_dir():
        return {"path": str(p), "error": "Not a directory", "entries": []}

    entries = []
    try:
        for child in sorted(p.iterdir(), key=lambda c: c.name.lower()):
            if not child.is_dir() or child.name.startswith("."):
                continue
            try:
                is_repo = (child / ".git").exists()
            except OSError:
                is_repo = False
            entries.append({"name": child.name, "path": str(child), "is_repo": is_repo})
    except PermissionError:
        return {"path": str(p), "error": "Permission denied", "entries": []}

    return {
        "path": str(p),
        "parent": str(p.parent) if p.parent != p else "",
        "is_repo": (p / ".git").exists(),
        "entries": entries,
        "error": "",
    }
