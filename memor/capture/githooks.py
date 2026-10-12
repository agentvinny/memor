"""Git capture: JSON-emitting hooks installed via `core.hooksPath`.

`memor install-git-hooks [repo...]` points the given repos at the shared hook
directory in the data dir; `git_hook` is the handler the hook scripts call.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from ..config import Config
from ..scope import machine_name, resolve_project
from ..store import Store

HOOKS = ("post-commit", "post-merge", "post-checkout")


def _git(repo: str | None, *args: str) -> str:
    cmd = ["git", "-C", repo] if repo else ["git"]
    cmd += list(args)
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=30).stdout.strip()
    except Exception:
        return ""


def collect_hook_event(hook: str, hook_args: list[str] | None = None) -> dict | None:
    """Gather git state for the given hook type and build an event payload.

    `kind` is set on `base` BEFORE the payload is built, so the payload
    (which embeds `base`) always carries it. Git passes post-checkout its
    state positionally (<prev> <ref> <flag>) — `hook_args` carries those;
    env vars are the fallback for manual/test invocations.
    """
    repo = os.environ.get("GIT_WORK_TREE") or os.getcwd()
    if not _git(repo, "rev-parse", "--git-dir"):
        return None
    remote = _git(repo, "config", "--get", "remote.origin.url") or None
    branch = _git(repo, "rev-parse", "--abbrev-ref", "HEAD") or "HEAD"
    base: dict = {
        "repo": repo,
        "branch": branch,
        "remote": remote,
        "machine": machine_name(),
    }
    if hook == "post-checkout":
        if hook_args:
            prev, new_ref = (hook_args + ["", ""])[:2]
            is_branch = len(hook_args) > 2 and hook_args[2] == "1"
        else:
            new_ref = os.environ.get("ref", "")
            is_branch = os.environ.get("flag", "") == "1"
            prev = os.environ.get("prev", "")
        if not is_branch:
            return None
        # git passes $2 as a SHA for `checkout -b` / detached moves; the hook
        # runs after HEAD has moved, so resolve the name we actually landed on
        target = _git(repo, "rev-parse", "--abbrev-ref", "HEAD") or new_ref
        base["kind"] = "checkout"
        base["command"] = f"git checkout {target}"
        base["payload"] = {**base, "to": target, "from": prev, "ref": new_ref}
    elif hook == "post-commit":
        sha = _git(repo, "rev-parse", "HEAD")
        subj = _git(repo, "log", "-1", "--format=%s")
        author = _git(repo, "log", "-1", "--format=%an")
        ts = _git(repo, "log", "-1", "--format=%at")
        parents = _git(repo, "log", "-1", "--format=%P").split()
        diffstat = _git(repo, "diff", "--stat", "4b825dc642cb6eb9a060e54bf8d69288fbee4904", "HEAD") \
            if len(parents) == 0 else _git(repo, "diff", "--stat", *parents[:1], "HEAD")
        base["kind"] = "commit"
        base["command"] = f"git commit {subj}"
        base["payload"] = {**base, "sha": sha, "subject": subj, "author": author,
                           "commit_ts": ts, "parents": parents, "diffstat": diffstat[:2000]}
    elif hook == "post-merge":
        sha = _git(repo, "rev-parse", "HEAD")
        subj = _git(repo, "log", "-1", "--format=%s")
        base["kind"] = "merge"
        base["command"] = f"git merge ({subj})"
        base["payload"] = {**base, "sha": sha, "subject": subj}
    else:
        return None
    return base


def install_hook_scripts(cfg: Config) -> Path:
    """Write the shared hook scripts into the data dir's git-hooks dir.

    `"$@"` is forwarded so post-checkout's positional args
    (<prev> <ref> <flag>) reach the handler.
    """
    hooks_dir = cfg.hooks_dir
    hooks_dir.mkdir(parents=True, exist_ok=True)
    memor_bin = _memor_invocation()
    for hook in HOOKS:
        script = hooks_dir / hook
        script.write_text(
            "#!/usr/bin/env bash\n"
            "# memor git hook (generated) — captures repo state as raw evidence\n"
            f'exec "{memor_bin}" git-hook {hook} "$@" >/dev/null 2>&1 || true\n'
        )
        script.chmod(0o755)
    return hooks_dir


def _memor_invocation() -> str:
    """Absolute command that runs `memor ...` from any directory.

    Note: never fall back through an empty env var — `Path("")` normalizes
    to `Path(".")` which is truthy and "exists", and would generate hook
    scripts containing `exec "."`.
    """
    env_bin = os.environ.get("MEMOR_BIN", "").strip()
    if env_bin:
        p = Path(env_bin).expanduser()
        if p.exists():
            return str(p)
    venv = os.environ.get("MEMOR_PROJECT_VENV", "").strip()
    if venv:
        return str(Path(venv).expanduser() / "bin" / "memor")
    # this package's venv (editable install: <repo>/.venv/bin/memor)
    p = Path(__file__).resolve().parent.parent.parent / ".venv" / "bin" / "memor"
    if p.exists():
        return str(p)
    # wheel/venv install: the console script sits next to this interpreter
    p = Path(sys.executable).parent / "memor"
    if p.exists():
        return str(p)
    return shutil.which("memor") or "memor"


def install_hooks(cfg: Config, store: Store, repos: list[str]) -> list[str]:
    install_hook_scripts(cfg)
    done = []
    for repo in repos or [os.getcwd()]:
        repo = os.path.abspath(os.path.expanduser(repo))
        if not (Path(repo) / ".git").exists() and not _git(repo, "rev-parse", "--git-dir"):
            continue
        subprocess.run(
            ["git", "-C", repo, "config", "core.hooksPath", str(cfg.hooks_dir)],
            check=True, capture_output=True,
        )
        store.append_event(
            "git", "hook_installed",
            machine=machine_name(),
            command=f"memor: git hooks installed via core.hooksPath",
            payload={"repo": repo, "hooks_path": str(cfg.hooks_dir)},
        )
        done.append(repo)
    return done


def run_git_hook(cfg: Config, store: Store, hook: str,
                 hook_args: list[str] | None = None) -> None:
    ev = collect_hook_event(hook, hook_args)
    if ev is None:
        return
    repo = ev.get("repo") or ""
    # never let the memory store observe its own projection repo, even if a
    # user re-pointed its hooks here (it would be a self-amplifying loop)
    if repo and os.path.realpath(repo) == os.path.realpath(str(cfg.data_dir)):
        return
    payload = ev.pop("payload")
    repo = payload.get("repo") or ev.get("repo")
    project = None
    if repo:
        try:
            project = resolve_project(repo, cfg)
        except Exception:
            project = None
    payload.update({k: v for k, v in ev.items() if k not in ("kind",)})
    store.append_event("git", ev["kind"],
                       machine=payload.pop("machine", None),
                       project=project,
                       command=payload.pop("command", None),
                       payload=payload)