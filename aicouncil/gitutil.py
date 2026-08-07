"""Git helpers: repository inspection, diff capture, rollback, and delivery.

The safety model matters more than the convenience here. Zero-Touch Mode hands
a coding agent a ``--dangerously-skip-permissions`` flag and lets it write to
disk unattended, so before Stage 2 runs we record a *snapshot*: the current
HEAD plus a dangling commit containing every tracked modification and every
untracked file. Rolling back restores that exact tree.

The snapshot is written with ``git stash create``-style plumbing rather than
``git stash push`` so the user's own stash stack is never touched.

Pull-request mode is the other half of that safety model: instead of leaving
the senior stage's work uncommitted on whatever branch is checked out, it moves
to a branch of its own, commits, pushes and opens a pull request - so the base
branch only ever changes when a human merges it.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

GIT_TIMEOUT = 60
# Pushing and talking to GitHub crosses the network, so they get their own,
# longer budget than local plumbing.
REMOTE_TIMEOUT = 300
# How much of a capped command's output is read at a time. Big enough that a
# normal diff arrives in one or two reads, small enough that the timeout is
# checked often on a slow one.
READ_CHUNK = 64 * 1024


class GitError(RuntimeError):
    """A git plumbing command failed."""


def _child_env() -> Dict[str, str]:
    """The environment every subprocess here runs under.

    Never let a child block the pipeline on a credential, editor or pager
    prompt: nothing in this app can answer one, so it would hang until the
    timeout instead of failing with a message.
    """
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_EDITOR"] = "true"
    env["GIT_PAGER"] = "cat"
    env.pop("GIT_DIR", None)
    env.pop("GIT_WORK_TREE", None)
    return env


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
    env = _child_env()
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


def _run_capped(
    args: List[str],
    cwd: str | Path,
    max_bytes: int,
    timeout: int = GIT_TIMEOUT,
) -> subprocess.CompletedProcess:
    """Run a git command, reading at most ``max_bytes`` of its output.

    ``_run`` buffers a command's entire stdout before any caller can look at
    it. That is right for plumbing that answers in a line and wrong for
    ``diff``: one accidentally staged multi-gigabyte asset is that many bytes
    held in memory, only to be clipped to 400 KB a moment later. This stops one
    byte past the cap and kills git rather than reading the rest.

    A capped run reports ``returncode == 0``: *we* stopped it, git did not
    fail, and a caller that reads a non-zero status as "no HEAD yet" would
    otherwise re-run the same enormous diff. The extra byte is deliberate too -
    it is what makes ``_clip_diff`` add its truncation notice.
    """
    deadline = time.monotonic() + timeout
    proc = subprocess.Popen(
        ["git", *args],
        cwd=str(cwd),
        env=_child_env(),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        errors="replace",
    )

    out: List[str] = []
    size = 0
    with proc:
        while size <= max_bytes:
            if time.monotonic() > deadline:
                proc.kill()
                raise GitError(f"git {' '.join(args)} timed out after {timeout}s")
            chunk = proc.stdout.read(READ_CHUNK) if proc.stdout else ""
            if not chunk:
                break
            out.append(chunk)
            size += len(chunk)
        capped = size > max_bytes
        if capped:
            proc.kill()
        try:
            code = proc.wait(timeout=max(1, int(deadline - time.monotonic())))
        except subprocess.TimeoutExpired:
            proc.kill()
            raise GitError(f"git {' '.join(args)} timed out after {timeout}s") from None

    return subprocess.CompletedProcess(
        ["git", *args], 0 if capped else code, "".join(out), ""
    )


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
    """Describe the working tree at ``path``. Never raises.

    ``path`` on the result is canonical either way: the repository root when
    there is one, and the resolved directory when there is not. The app stores
    what this reports and later resolves it again when a run starts, so the two
    have to agree - otherwise a conversation held in a symlinked folder cannot
    be continued from the same folder it was held in.
    """
    p = str(Path(path).expanduser().resolve())
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
    tracked = _run_capped(["diff", "HEAD", "--no-color"], root, max_bytes)
    if tracked.returncode == 0 and tracked.stdout.strip():
        chunks.append(tracked.stdout)
    elif tracked.returncode != 0:
        # No HEAD yet (empty repo): fall back to the index-less diff.
        fallback = _run_capped(["diff", "--no-color"], root, max_bytes)
        if fallback.stdout.strip():
            chunks.append(fallback.stdout)
    collected = sum(len(c) for c in chunks)

    for rel in _untracked_files(root):
        # Each new file is capped on its own below, but a thousand of them are
        # not: stop once there is already more than will survive the clip,
        # rather than diffing files whose output is guaranteed to be cut.
        if collected > max_bytes:
            break
        target = Path(root) / rel
        try:
            if target.stat().st_size > 200_000:
                note = (
                    f"diff --git a/{rel} b/{rel}\n"
                    f"new file (skipped: larger than 200 KB)\n"
                )
                chunks.append(note)
                collected += len(note)
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
            collected += len(proc.stdout)

    # Each chunk already ends with a newline; joining on "\n" would insert a
    # blank line between files that no diff parser expects.
    return _clip_diff("".join(chunks), max_bytes)


def _clip_diff(diff: str, max_bytes: int) -> str:
    if len(diff) > max_bytes:
        return diff[:max_bytes] + "\n\n... diff truncated for display ...\n"
    return diff


def _untracked_files(root: str) -> List[str]:
    proc = _run(
        ["ls-files", "--others", "--exclude-standard", "-z"], root, check=False
    )
    return [f for f in proc.stdout.split("\0") if f]


def _numstat_totals(numstat: str) -> Dict[str, int]:
    """Fold ``git diff --numstat`` output into ``{files, insertions, deletions}``."""
    result = {"files": 0, "insertions": 0, "deletions": 0}
    for line in numstat.splitlines():
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
    return result


def diff_stat(path: str | Path) -> Dict[str, int]:
    """Return ``{files, insertions, deletions}`` for the current changes."""
    root = repo_root(path)
    if root is None:
        return {"files": 0, "insertions": 0, "deletions": 0}

    proc = _run(["diff", "HEAD", "--numstat"], root, check=False)
    result = _numstat_totals(proc.stdout)
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


# --------------------------------------------------------------------------
# Pull-request delivery
# --------------------------------------------------------------------------


# Every branch this app creates lives under one prefix, so a repository's
# branch list stays legible and `git branch -d ai-council/...` cleans up.
BRANCH_PREFIX = "ai-council"


@dataclass
class PullRequest:
    """The branch, commit and pull request one run was delivered as."""

    base: str
    branch: str
    commit: str = ""
    url: str = ""

    def to_dict(self) -> Dict[str, str]:
        return {
            "base": self.base,
            "branch": self.branch,
            "commit": self.commit,
            "url": self.url,
        }


def _gh(
    args: List[str], cwd: str | Path, timeout: int = REMOTE_TIMEOUT
) -> subprocess.CompletedProcess:
    """Run the GitHub CLI with the same guarantees ``_run`` gives git."""
    env = _child_env()
    # gh asks interactive questions when it is unsure - about the remote to
    # use, about pushing - and there is nobody here to answer them.
    env["GH_PROMPT_DISABLED"] = "1"
    env["GH_PAGER"] = "cat"
    env["NO_COLOR"] = "1"
    try:
        proc = subprocess.run(
            ["gh", *args],
            cwd=str(cwd),
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        raise GitError("The GitHub CLI (`gh`) is not installed.") from exc
    except subprocess.TimeoutExpired as exc:
        raise GitError(f"gh {' '.join(args)} timed out after {timeout}s") from exc
    if proc.returncode != 0:
        raise GitError(
            f"gh {' '.join(args)} failed ({proc.returncode}): "
            f"{proc.stderr.strip() or proc.stdout.strip()}"
        )
    return proc


def pull_request_blocker(path: str | Path) -> str:
    """Why ``path`` cannot start a pull-request run, or "" if it can.

    Everything checkable is checked *before* the agents run. The alternative is
    discovering a missing identity or an unauthenticated CLI after a senior
    stage has already spent its quota and written to disk, with the work
    stranded on a branch nobody asked for.
    """
    st = status(path)
    if not st.is_repo:
        return (
            "Pull-request mode commits, pushes and opens a PR, and this "
            "working folder is not a git repository. Pick one that is, or turn "
            "the toggle off."
        )
    if st.error:
        return st.error
    if not st.head:
        return (
            "Pull-request mode needs a commit to branch from, and this "
            "repository has none yet. Make an initial commit first."
        )
    if not st.branch or st.branch == "HEAD":
        return (
            "Pull-request mode branches from whatever is checked out, and this "
            "repository is on a detached HEAD. Check out the branch the pull "
            "request should target."
        )
    if not st.clean:
        return (
            f"Pull-request mode commits everything the run changes, so it needs "
            f"a clean tree to start from - and this one has "
            f"{len(st.staged) + len(st.modified) + len(st.untracked)} "
            f"uncommitted change(s). Commit or stash them first, or they will "
            f"end up in the pull request."
        )

    root = st.path
    # `git var` fails exactly when git cannot assemble an identity, which is
    # the same thing that would abort the commit at the end of the run.
    if _run(["var", "GIT_COMMITTER_IDENT"], root, check=False).returncode != 0:
        return (
            "Pull-request mode has to commit, and git has no identity to commit "
            "with. Set `git config user.name` and `git config user.email`."
        )
    if not st.remote:
        return (
            "Pull-request mode pushes to a remote named 'origin', and this "
            "repository has none."
        )
    if shutil.which("gh") is None:
        return (
            "Pull-request mode opens the pull request with the GitHub CLI "
            "(`gh`), which is not on PATH. Install it, or turn the toggle off."
        )
    # Authentication is the one precondition only GitHub can answer, and it is
    # the one that would otherwise surface at the very end of the run.
    try:
        _gh(["auth", "status"], root, timeout=GIT_TIMEOUT)
    except GitError as exc:
        return f"The GitHub CLI is not ready: {exc}"
    return ""


def branch_for(run_id: str, task: str) -> str:
    """Name the branch a run delivers on: ``ai-council/<task-slug>-<run-id>``.

    The slug is for the human reading a pull-request list; the run id is what
    makes the name unique and traceable back to a transcript.
    """
    slug = re.sub(r"[^a-z0-9]+", "-", task.lower()).strip("-")[:48].strip("-")
    return f"{BRANCH_PREFIX}/{slug}-{run_id}" if slug else f"{BRANCH_PREFIX}/{run_id}"


def create_branch(path: str | Path, branch: str) -> None:
    """Create ``branch`` at HEAD and check it out."""
    _run(["checkout", "-q", "-b", branch], path)


def checkout(path: str | Path, branch: str) -> None:
    """Check out an existing branch."""
    _run(["checkout", "-q", branch], path)


def branch_diff(
    path: str | Path, base: str, head: str = "HEAD", max_bytes: int = 400_000
) -> str:
    """The diff a pull request would show: ``base...head``.

    Three dots, so the diff is against the merge base rather than the tip of
    ``base`` - commits that landed on the base branch meanwhile are not part of
    what this run changed.
    """
    root = repo_root(path)
    if root is None:
        return ""
    proc = _run_capped(["diff", "--no-color", f"{base}...{head}"], root, max_bytes)
    return _clip_diff(proc.stdout, max_bytes)


def branch_diff_stat(path: str | Path, base: str, head: str = "HEAD") -> Dict[str, int]:
    """``{files, insertions, deletions}`` for ``base...head``."""
    root = repo_root(path)
    if root is None:
        return {"files": 0, "insertions": 0, "deletions": 0}
    return _numstat_totals(
        _run(["diff", "--numstat", f"{base}...{head}"], root, check=False).stdout
    )


def publish_pull_request(
    path: str | Path, base: str, branch: str, title: str, body: str
) -> PullRequest:
    """Commit the run's work, push ``branch`` and open a pull request.

    Deliberately tolerant about who made the commit. Some agents commit their
    own work and some leave it in the worktree; both are legitimate, so this
    commits whatever is outstanding and then judges success on whether the
    branch is actually ahead of ``base``.
    """
    root = repo_root(path)
    if root is None:
        raise GitError("Not a git repository.")

    st = status(root)
    if st.branch != branch:
        raise GitError(
            f"Expected to publish {branch!r}, but {st.branch or 'a detached HEAD'} "
            f"is checked out. Nothing was pushed."
        )
    if not st.clean:
        _run(["add", "-A"], root)
        _run(["commit", "-q", "-m", title, "-m", body], root)

    ahead = _run(["rev-list", "--count", f"{base}..{branch}"], root, check=False)
    if ahead.stdout.strip() in ("", "0"):
        raise GitError(
            "The senior stage changed nothing, so there is no pull request to "
            "open."
        )

    pr = PullRequest(
        base=base,
        branch=branch,
        commit=_run(["rev-parse", "HEAD"], root).stdout.strip(),
    )
    _run(["push", "--set-upstream", "origin", branch], root, timeout=REMOTE_TIMEOUT)
    result = _gh(
        ["pr", "create", "--base", base, "--head", branch,
         "--title", title, "--body", body],
        root,
    )
    # gh prints progress lines before the URL, so take the last thing that
    # looks like one rather than the last line.
    for line in reversed(result.stdout.splitlines()):
        if line.strip().startswith("http"):
            pr.url = line.strip()
            break
    if not pr.url:
        raise GitError(
            f"gh reported no pull-request URL. {branch} is pushed; open the "
            f"pull request by hand."
        )
    return pr


def list_directory(path: str | Path) -> Dict:
    """List subdirectories of ``path`` for the GUI's directory picker.

    Returns only directories - the picker selects a folder to work in, not a
    file - and flags which entries are git repositories so the UI can badge
    them. A badge, not a filter: any folder can be worked in, and being a
    repository only decides whether diff, snapshot and rollback are on offer.
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


def commit_all(path: str | Path, message: str) -> Dict[str, Any]:
    """Stage everything and commit it. Returns a summary of what landed.

    Deliberately ``add -A``: the diff the operator just reviewed in the app is
    the whole working tree, so committing a subset of it would not be the
    thing they approved. Ignored files stay ignored - `add -A` honours
    .gitignore, which is what keeps build output and virtualenvs out.

    Nothing is pushed. Publishing is a separate, outward-facing act and it has
    its own path through pull-request mode; a button labelled "commit" that
    also pushed would be a surprise the first time it mattered.
    """
    message = (message or "").strip()
    if not message:
        raise GitError("A commit message is required.")

    root = repo_root(path)
    if root is None:
        raise GitError(f"{path!r} is not a git repository.")

    st = status(root)
    if st.clean:
        raise GitError("Nothing to commit - the working tree is clean.")

    _run(["add", "-A"], root)

    # `add -A` can still leave nothing staged: every change may have been to an
    # ignored file. Committing then would create an empty commit.
    staged = _run(["diff", "--cached", "--name-only"], root).stdout.split()
    if not staged:
        raise GitError(
            "Nothing to commit after staging - the changes are all in ignored "
            "files."
        )

    stat = _numstat_totals(_run(["diff", "--cached", "--numstat"], root).stdout)

    proc = _run(["commit", "-m", message], root, check=False)
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        # The overwhelmingly common cause, and git's own message for it is a
        # wall of configuration advice.
        if "user.email" in detail or "user.name" in detail:
            raise GitError(
                "git has no identity configured for this repository. Set one "
                "with:  git config user.name '...'  and  git config "
                "user.email '...'"
            )
        raise GitError(detail.splitlines()[-1] if detail else "git commit failed.")

    head = _run(["rev-parse", "HEAD"], root, check=False).stdout.strip()
    return {
        "commit": head,
        "short": head[:8],
        "message": message,
        "files": len(staged),
        "insertions": stat.get("insertions", 0),
        "deletions": stat.get("deletions", 0),
        "branch": _run(["rev-parse", "--abbrev-ref", "HEAD"], root,
                       check=False).stdout.strip(),
    }
