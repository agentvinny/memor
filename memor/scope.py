"""Scope resolution heuristics: cwd -> project, hostname -> machine."""
from __future__ import annotations

import socket
import subprocess
from pathlib import Path

from .config import Config
from .models import slugify

_machine_cache: str = ""
_remote_cache: dict[str, str | None] = {}


def machine_name() -> str:
    global _machine_cache
    if not _machine_cache:
        _machine_cache = socket.gethostname().lower()
    return _machine_cache


def nearest_git_root(path: str | Path) -> Path | None:
    p = Path(os_path := path)
    try:
        p = Path(os_path).expanduser().resolve()
    except Exception:
        return None
    for cand in [p, *p.parents]:
        if (cand / ".git").exists():
            return cand
    return None


def git_remote_url(root: Path | str) -> str | None:
    """origin URL of the repo at `root` (cached; None when absent)."""
    r = str(root)
    if r in _remote_cache:
        return _remote_cache[r]
    url = None
    try:
        out = subprocess.run(
            ["git", "-C", r, "config", "--get", "remote.origin.url"],
            capture_output=True, text=True, timeout=10,
        )
        if out.returncode == 0:
            url = out.stdout.strip() or None
    except Exception:
        url = None
    _remote_cache[r] = url
    return url


def resolve_project(path: str | Path, config: Config) -> str | None:
    """Longest-prefix match against the project registry, then git-remote
    matching (stable across clones/machines), else the git repo directory
    name as a fallback project id."""
    try:
        name = config.find_project_by_path(path)
        if name:
            return name
    except Exception:
        pass
    root = nearest_git_root(path)
    if root is not None:
        remote = git_remote_url(root)
        if remote:
            try:
                name = config.find_project_by_remote(remote)
                if name:
                    return name
            except Exception:
                pass
        return slugify(root.name)
    return None