#!/usr/bin/env python3
"""Fetch a remote repository into a disposable, read-only cache snapshot."""

from __future__ import annotations

import shutil
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows has no fcntl
    fcntl = None

try:
    import msvcrt
except ImportError:  # pragma: no cover - Unix has no msvcrt
    msvcrt = None

from discovery import (
    cache_root,
    content_revision,
    fingerprint,
    git,
    is_git_repo,
    redact_remote,
    remote_branch,
)

from models import SourceSpec, SourceView


def _run(root: Path, args: list[str], timeout: int = 180) -> tuple[bool, str]:
    try:
        result = subprocess.run(["git", "-C", str(root), *args], text=True, capture_output=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)
    if result.returncode:
        return False, (result.stderr or result.stdout or f"git exited {result.returncode}").strip()
    return True, result.stdout.strip()


def _safe_error(error: str, remote_url: str | None) -> str:
    return error.replace(remote_url, "[REDACTED_REMOTE]") if remote_url else error


@contextmanager
def _snapshot_lock(target: Path, timeout: float = 300.0):
    """Serialize mutations to one shared snapshot across bot processes.

    Multiple MCP clients can sync the same source at once. Without a process
    lock, one client can run ``remote set-url`` while another runs fetch or
    checkout, producing Git's ``.git/config.lock`` failure and a false local
    fallback. The lock is a small derived cache file and is intentionally
    retained so a waiter never races a lock-file unlink/recreate cycle.
    """

    if fcntl is None and msvcrt is None:
        yield
        return
    lock_path = target.with_name(f"{target.name}.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+b")
    deadline = time.monotonic() + timeout
    try:
        if msvcrt is not None and fcntl is None:
            # msvcrt.locking locks a byte range from the current file offset.
            # Keep one byte in the derived lock file so every waiter uses the
            # same range on Windows.
            handle.seek(0)
            handle.write(b"\0")
            handle.flush()
        while True:
            try:
                handle.seek(0)
                if fcntl is not None:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                else:
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                break
            except (BlockingIOError, OSError):
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"timed out waiting for snapshot lock: {lock_path}")
                time.sleep(0.1)
        yield
    finally:
        handle.seek(0)
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        elif msvcrt is not None:
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
        handle.close()


def snapshot_lock_backend() -> str:
    """Return the platform lock implementation used for shared snapshots."""

    if fcntl is not None:
        return "fcntl"
    if msvcrt is not None:
        return "msvcrt"
    return "unavailable"


def local_view(spec: SourceSpec, reason: str | None = None) -> SourceView:
    git_source = is_git_repo(spec.root)
    dirty = bool(git(spec.root, "status", "--porcelain")) if git_source else False
    return SourceView(
        spec=spec,
        path=spec.root,
        commit=git(spec.root, "rev-parse", "HEAD") if git_source else content_revision(spec.root),
        branch=git(spec.root, "branch", "--show-current") if git_source else None,
        commit_type="local_worktree" if git_source else "local_directory",
        dirty=dirty,
        remote_url=redact_remote(spec.remote or git(spec.root, "remote", "get-url", "origin")),
        remote_commit=None,
        fetch_ok=False if reason else None,
        fetch_error=reason,
        snapshot=False,
    )


def prepare_view(spec: SourceSpec, local: bool = False) -> SourceView:
    if local or spec.local_only:
        return local_view(spec)
    remote_url = spec.remote or git(spec.root, "remote", "get-url", "origin")
    branch = spec.branch or remote_branch(spec.root)
    if not remote_url or not branch:
        return local_view(spec, "no remote default branch available" if is_git_repo(spec.root) else None)

    target = cache_root() / "snapshots" / fingerprint(spec)
    try:
        with _snapshot_lock(target):
            target.parent.mkdir(parents=True, exist_ok=True)
            if not (target / ".git").exists():
                if target.exists():
                    shutil.rmtree(target)
                ok, error = _run(spec.root, ["clone", "--no-checkout", "--quiet", str(spec.root), str(target)], timeout=300)
                if not ok:
                    return local_view(spec, f"snapshot clone failed: {_safe_error(error, remote_url)}")
            # A local clone may inherit an authenticated remote URL. Use it only
            # for this fetch, then clear the cache copy so credentials do not stay
            # in the derived snapshot's .git/config.
            ok, error = _run(target, ["remote", "set-url", "origin", remote_url], timeout=30)
            if not ok:
                return local_view(spec, f"snapshot remote setup failed: {_safe_error(error, remote_url)}")
            try:
                ok, error = _run(target, ["fetch", "--prune", "origin"], timeout=300)
                if not ok:
                    return local_view(spec, f"remote fetch failed: {_safe_error(error, remote_url)}")
            finally:
                _run(target, ["remote", "set-url", "origin", ""], timeout=30)
            remote_ref = f"refs/remotes/origin/{branch}"
            commit = git(target, "rev-parse", remote_ref)
            if not commit:
                return local_view(spec, f"remote ref unavailable: {remote_ref}")
            ok, error = _run(target, ["checkout", "--detach", "--quiet", commit], timeout=120)
            if not ok:
                return local_view(spec, f"snapshot checkout failed: {_safe_error(error, remote_url)}")
            return SourceView(
                spec=spec,
                path=target,
                commit=commit,
                branch=branch,
                commit_type="remote_snapshot",
                dirty=False,
                remote_url=redact_remote(remote_url),
                remote_commit=commit,
                fetch_ok=True,
                snapshot=True,
                metadata={"snapshot_path": str(target)},
            )
    except (OSError, subprocess.SubprocessError) as exc:
        return local_view(spec, f"snapshot preparation failed: {_safe_error(str(exc), remote_url)}")
