"""Terminal/agent evidence capture.

Three entry points:
- `shell_event`: called by the sourced shell hook (bash/zsh), fields passed
  via environment variables (no shell-quoting hazards).
- `agent_event`: called by Claude Code's PostToolUse hook; reads the hook
  payload JSON on stdin. Only source of command *output* + session id.
- `ingest_atuin`: pulls new rows from Atuin's history.db (when Atuin is the
  user's shell history, on any machine).
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path

from ..config import Config
from ..scope import machine_name, resolve_project
from ..store import Store

OUTPUT_KEEP = 4096


def _session_bootstrap(store: Store, session: str, machine: str) -> None:
    """G5: once per session, snapshot the environment (redacted at append
    time) so env-driven behavior is later explainable ('why is FOO set?').
    `os.environ` is the right source: the hook process inherits the shell's
    environment, and /proc/self/environ would only show the frozen
    exec-time view."""
    if not session:
        return
    env = {k: v[:2048] for k, v in os.environ.items() if v}
    if not env:
        return
    seen = store.snap_get("env_sessions") or {"ids": []}
    if session in seen["ids"]:
        return
    store.append_event(
        "shell", "session_env",
        machine=machine, session=session,
        command=f"session {session} environment snapshot ({len(env)} vars)",
        payload={"vars": dict(list(env.items())[:300])},
    )
    ids = [i for i in seen["ids"] if i != session] + [session]
    store.snap_set("env_sessions", {"ids": ids[-500:]})


def shell_event(cfg: Config, store: Store) -> None:
    """Fields come via env: MEMOR_CMD, MEMOR_EXIT, MEMOR_T0, MEMOR_T1,
    MEMOR_SESSION, MEMOR_CWD, MEMOR_SHELL."""
    cmd = os.environ.get("MEMOR_CMD", "").strip()
    if not cmd or cmd.startswith("__memor") or cmd.startswith("_memor"):
        return
    if os.environ.get("MEMOR_DISABLED"):
        return
    cwd = os.environ.get("MEMOR_CWD") or os.getcwd()
    t0 = float(os.environ.get("MEMOR_T0", 0) or 0)
    t1 = float(os.environ.get("MEMOR_T1", 0) or 0)
    if t0 > 1e12 or t1 > 1e12:  # nanoseconds (date +%s%N); stay in int-ns to
        duration_ms = (int(t1) - int(t0)) // 1_000_000 or None  # avoid float loss
    else:
        duration_ms = int((t1 - t0) * 1000) if t0 and t1 else None
    if not (t0 and t1):
        duration_ms = None
    session = os.environ.get("MEMOR_SESSION") or None
    machine = machine_name()
    if session:
        # G5: env snapshot precedes the first command of the session
        _session_bootstrap(store, session, machine)
    # F13: carry every session identifier we can see, so events from one
    # visual session are joinable later (atuin/tmux/ppid)
    extra = {}
    for key in ("TMUX_PANE", "ATUIN_SESSION", "ATUIN_SESSION_ID", "TERM_PROGRAM"):
        v = os.environ.get(key)
        if v:
            extra[key.lower()] = v
    try:
        extra["ppid"] = os.getppid()
    except AttributeError:
        pass
    store.append_event(
        "shell", "command",
        machine=machine,
        # Box-wide override for general (non-project) work done from inside
        # a project/repo directory: `MEMOR_NO_PROJECT=1 <cmd>` per command,
        # or `export MEMOR_NO_PROJECT=1` for the whole session (unset it to
        # go back to project-scoped capture). The raw command, prefix
        # included, is recorded as evidence.
        project=None if (os.environ.get("MEMOR_NO_PROJECT") not in (None, "", "0")
                         or cmd.startswith("MEMOR_NO_PROJECT=1 "))
                else resolve_project(cwd, cfg),
        cwd=cwd,
        session=session,
        command=cmd[:1024],
        payload={
            "exit": int(os.environ.get("MEMOR_EXIT", "0") or 0),
            "duration_ms": duration_ms,
            "shell": os.environ.get("MEMOR_SHELL", "bash"),
            **extra,
        },
    )


def agent_event(cfg: Config, store: Store, stdin_text: str) -> dict | None:
    try:
        p = json.loads(stdin_text)
    except json.JSONDecodeError:
        return None
    tool = p.get("tool_name", "")
    t_in = p.get("tool_input") or {}
    t_out = p.get("tool_response")
    command = None
    payload: dict = {"tool": tool}
    if tool == "Bash":
        command = str(t_in.get("command", ""))[:1024]
    elif isinstance(t_in, dict):
        command = f"{tool}: " + json.dumps(t_in, ensure_ascii=False)[:300]
    else:
        command = f"{tool}"
    if isinstance(t_out, str):
        out_text = t_out
    elif isinstance(t_out, dict):
        out_text = "\n".join(str(t_out.get(k, "")) for k in ("stdout", "stderr"))
        if t_out.get("interrupted"):
            payload["interrupted"] = True
    else:
        out_text = json.dumps(t_out, ensure_ascii=False, default=str)[:200] if t_out else ""
    payload["output"] = out_text[: cfg.max_output_chars]
    if tool == "Bash" and isinstance(t_out, dict) and t_out.get("exit_code") is not None:
        payload["exit_code"] = t_out.get("exit_code")
    cwd = p.get("cwd") or os.getcwd()
    store.append_event(
        "agent", "tool_call",
        machine=machine_name(),
        project=resolve_project(cwd, cfg),
        cwd=cwd,
        session=p.get("session_id"),
        command=command,
        payload=payload,
    )
    return payload


def run_wrapped(cfg: Config, store: Store, command: str,
                no_project: bool = False) -> int:
    """C2: run a command with full output capture (the manual-shell twin of
    the Claude Code hook — only that hook captures output otherwise).
    Output passes through the store's redaction; the terminal still shows
    the raw output, so the wrapper is transparent to use. `no_project`
    records the run box-wide (machine scope) instead of under the project
    containing the cwd — same semantics as MEMOR_NO_PROJECT."""
    import subprocess as _sp
    import sys as _sys
    import time as _time

    t0 = _time.time()
    p = _sp.run(command, shell=True, capture_output=True, text=True)
    out, err = p.stdout or "", p.stderr or ""
    _sys.stdout.write(out)
    _sys.stderr.write(err)
    _sys.stdout.flush()
    _sys.stderr.flush()
    store.append_event(
        "shell", "command",
        machine=machine_name(),
        project=None if (no_project or os.environ.get("MEMOR_NO_PROJECT")
                         not in (None, "", "0"))
        else resolve_project(os.getcwd(), cfg),
        cwd=os.getcwd(),
        session=os.environ.get("MEMOR_SESSION") or f"memor-{os.getpid()}",
        command=command[:1024],
        payload={
            "exit": p.returncode,
            "output": out[: cfg.max_output_chars],
            "stderr": err[:2048],
            "duration_ms": int((_time.time() - t0) * 1000),
            "wrapped": True,
            "shell": "bash",
        },
    )
    return p.returncode


def atuin_db_path() -> Path | None:
    env = os.environ.get("ATUIN_DATA_DIR")
    if env:
        p = Path(env) / "history.db"
        return p if p.exists() else None
    p = Path.home() / ".local/share/atuin/history.db"
    return p if p.exists() else None


def ingest_atuin(cfg: Config, store: Store) -> int:
    """Ingest new rows from Atuin's SQLite history db (harness-agnostic
    terminal capture when the user runs Atuin)."""
    db = atuin_db_path()
    if db is None:
        return 0
    seen = store.snap_get("atuin.seen") or {"ids": []}
    seen_ids = set(seen["ids"])
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        rows = conn.execute(
            "SELECT id, timestamp, duration, exit, command, cwd, session, hostname"
            " FROM history WHERE deleted_at IS NULL ORDER BY timestamp ASC"
        ).fetchall()
        conn.close()
    except sqlite3.Error:
        return 0
    new = [r for r in rows if r[0] not in seen_ids]
    if not new:
        return 0
    for r in new:
        aid, ts, duration, exit_code, command, cwd, session, hostname = r
        if not command or command.startswith("atuin"):
            continue
        seen_ids.add(aid)
        store.append_event(
            "shell", "command",
            ts=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts)) if ts else None,
            machine=(hostname or machine_name()).lower(),
            project=resolve_project(cwd or "", cfg) if cwd else None,
            cwd=cwd,
            session=session,
            command=command[:1024],
            payload={"exit": exit_code, "duration_ms": duration, "atuin_id": aid},
        )
    store.snap_set("atuin.seen", {"ids": list(seen_ids)[-3000:]})
    return len(new)