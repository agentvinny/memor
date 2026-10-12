"""memor CLI — control surface for capture, consolidation, review, query.

  memor init                     set up data dir, db, git repo, hooks
  memor doctor                   health/status of every layer
  memor add-project NAME --path
  memor remember TEXT ...        record a human-asserted fact/decision
  memor consolidate              evidence -> facts/decisions -> markdown+git
  memor capture run|stop|status  always-on capture daemon
  memor install-git-hooks [repo] point repos at shared JSON-emitting hooks
  memor install --claude         register PostToolUse hook + MCP server
  memor serve                    run the MCP server (stdio)
  memor search|current|history|related|decisions|evidence|context|recent
  memor review [accept|reject]   human pass over staged memory changes
  memor correct FACT_ID --claim  supersede a fact with a human correction
  memor forget FACT_ID           retract a fact (kept in history, audited)
  memor llm sync|status          process the LLM queue (works while LLM is down)
  memor verify                   re-observe reality, fix stale state facts
  memor run CMD...               run a command with full output captured
  memor setup [dirs]             one-shot onboarding
  memor compact|checkpoint       retire old evidence / WAL checkpoint
  memor export|import            portable store bundles (cross-machine)
  memor events                   browse raw Level-4 evidence
  memor report [project]         human 'catch me up'
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import __version__
from .config import Config
from .models import now_utc
from .store import Store, data_dir_size


def _cfg(args) -> Config:
    return Config.load(Path(args.data_dir).expanduser() if args.data_dir else None)


# ---------------------------------------------------------------- lifecycle

def cmd_init(args) -> int:
    cfg = _cfg(args)
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    cfg.events_dir.mkdir(exist_ok=True)
    store = Store(cfg.data_dir)
    if not cfg.machine:
        from .scope import machine_name
        cfg.machine = machine_name()
    cfg.save()
    store.append_event("memor", "store_initialized",
                       command="memor store initialized",
                       payload={"version": __version__, "data_dir": str(cfg.data_dir)})
    # git repo for the markdown hierarchy (Level 0-2 projection)
    if not (cfg.data_dir / ".git").exists():
        subprocess.run(["git", "init", "-b", "main"], cwd=cfg.data_dir, check=True,
                       capture_output=True)
    gi = cfg.data_dir / ".gitignore"
    gi.write_text("memor.db\nmemor.db-wal\nmemor.db-shm\nevents/\ngit-hooks/\n*.pid\ncapture.log\n")
    from .capture.githooks import install_hook_scripts
    install_hook_scripts(cfg)
    # NOTE: the store's own repo is deliberately NOT pointed at the hooks —
    # its git history IS the diff-able audit trail of the Markdown projection,
    # and feeding its commits back into the evidence stream would make the
    # store observe itself (each consolidation commit becomes "new" evidence).
    subprocess.run(["git", "-C", str(cfg.data_dir), "add", "-A"], capture_output=True)
    subprocess.run(["git", "-C", str(cfg.data_dir), "-c", "user.name=memor",
                    "-c", "user.email=memor@local", "commit", "-m",
                    "memor: initial memory store"], capture_output=True)
    print(f"initialized memory store at {cfg.data_dir}")
    print(f"  db:        {cfg.db_path}")
    print(f"  events:    {cfg.events_dir}/YYYY-MM.jsonl (Level 4 raw evidence)")
    print(f"  markdown:  {cfg.system_dir}/ (Level 0-1) + {cfg.reference_dir}/ (Level 2-3)")
    print("next: memor add-project / memor install / memor capture run")
    return 0


def cmd_doctor(args) -> int:
    cfg = _cfg(args)
    ok = lambda b: "yes" if b else "NO"
    store = None
    db_ok = cfg.db_path.exists()
    if db_ok:
        store = Store(cfg.data_dir)
    n_events = store.count_events() if store else 0
    n_facts = len(store.list_facts(limit=100000)) if store else 0
    n_open = len(store.open_reviews()) if store else 0
    cursor = store.meta_get("consolidate.cursor") if store else None
    print(f"version:     memor {__version__}")
    print(f"data dir:    {cfg.data_dir} ({'exists' if cfg.data_dir.exists() else 'MISSING — run: memor init'})")
    print(f"db:          {ok(db_ok)}" + (f"  events={n_events} facts={n_facts} open_reviews={n_open}" if db_ok else ""))
    print(f"cursor:      {cursor or 'never consolidated'}")
    print(f"consolidate: every {cfg.consolidate_interval:g}s via daemon (0=off)")
    print(f"git repo:    {ok((cfg.data_dir / '.git').exists() if cfg.data_dir.exists() else False)}")
    print(f"machine:     {cfg.machine or '(unset)'}")
    print(f"projects:    {', '.join(p.name for p in cfg.projects) or '(none registered)'}")
    print(f"watch paths: {', '.join(cfg.watch_paths[:4])}{'…' if len(cfg.watch_paths) > 4 else ''}")
    if store:
        integ = store._conn().execute("PRAGMA integrity_check").fetchone()[0]
        print(f"integrity:   {ok(integ == 'ok')}"
              + ("" if integ == "ok" else f"  ({integ[:120]})"))
        sizes = data_dir_size(cfg.data_dir) if cfg.data_dir.exists() else 0
        oldest, newest = store.oldest_event_ts(), store.max_event_ts()
        print(f"store:       {sizes / 1e6:.1f} MiB  "
              f"events {oldest or '—'} → {newest or '—'}  "
              f"fts={'on' if store.fts else 'OFF (LIKE fallback)'}")
        if cfg.max_store_size_bytes > 0 and sizes > cfg.max_store_size_bytes:
            print(f"store size:  WARNING {sizes / 1e9:.2f} GiB exceeds "
                  f"max_store_size_bytes ({cfg.max_store_size_bytes / 1e9:.0f} GiB) — "
                  f"consider `memor compact --dry-run`, or raise the threshold in memor.json")
        by_src = store._conn().execute(
            "SELECT source, COUNT(*) FROM raw_events GROUP BY source"
            " ORDER BY 2 DESC").fetchall()
        print("event mix:   " + "  ".join(f"{r[0]}={r[1]}" for r in by_src[:8]))
        dups = store._conn().execute(
            "SELECT COUNT(*) FROM (SELECT command, machine, ts FROM raw_events"
            " GROUP BY command, machine, ts HAVING COUNT(*) > 2 AND command <> '')").fetchone()[0]
        if dups:
            print(f"duplicates: {dups} identical (command,machine,second) groups — check double hooks")
        if cfg.retention_months > 0:
            eligible = store.eligible_for_compaction(
                _months_ago(cfg.retention_months),
                set((store.meta_get("consolidate.seen") or "").splitlines()),
                store.referenced_event_ids())
            print(f"retention:   {cfg.retention_months} months; {len(eligible)} events eligible "
                  f"for `memor compact`")
    from .capture import daemon as d
    st = d.status(cfg)
    print(f"daemon:      {'running pid ' + str(st['pid']) if st['running'] else 'stopped'}")
    from .capture.journal import journal_available
    jmode, _ = journal_available()
    from .capture.docker_events import docker_available
    from .capture.terminal import atuin_db_path
    import shutil as _sh
    print(f"sources:     journal={ok(bool(jmode))}"
          f"{'(' + jmode + ')' if jmode else ''}  docker={ok(docker_available())}"
          f"  ss={ok(bool(_sh.which('ss')))}  atuin={ok(atuin_db_path() is not None)}")
    from .consolidate.extract import extractor_for
    from .consolidate.apply import llm_pending_count
    name, llm = extractor_for(cfg)
    avail = bool(llm is not None and llm.available())
    pending = llm_pending_count(store) if store else 0
    base = cfg.llm_base_url or ("anthropic-api" if name == "anthropic" else "—")
    print(f"llm:         provider={name} available={ok(avail)} model={cfg.llm_model} "
          f"base={base} max_tokens={cfg.llm_max_tokens} "
          f"autosync={'on' if cfg.llm_autosync else 'off'} pending={pending}")
    claude_settings = Path.home() / ".claude" / "settings.json"
    hook_installed = False
    if claude_settings.exists():
        try:
            hook_installed = "memor" in claude_settings.read_text()
        except OSError:
            pass
    print(f"claude hook: {ok(hook_installed)}  (PostToolUse Bash → memor agent-event)")
    print(f"mcp:         see `claude mcp list` (register with: memor install --claude)")
    return 0


def cmd_add_project(args) -> int:
    cfg = _cfg(args)
    from .config import ProjectRef
    p = next((x for x in cfg.projects if x.name == args.name), None)
    if p is None:
        p = ProjectRef(name=args.name, description=args.description or "")
        cfg.projects.append(p)
    for path in args.path or []:
        if path not in p.paths:
            p.paths.append(path)
    for remote in args.remote or []:
        if remote not in p.remotes:
            p.remotes.append(remote)
    if args.description:
        p.description = args.description
    cfg.save()
    store = Store(cfg.data_dir)
    store.upsert_entity(args.name, kind="project", project=args.name,
                        description=p.description)
    ev = store.append_event("human", "project_registered", project=args.name,
                           command=f"registered project {args.name}: {args.description}",
                           payload={"project": args.name,
                                    "paths": p.paths,
                                    "description": args.description})
    store.insert_fact(scope_project=args.name, scope_component=None,
                      scope_machine=None, subject="purpose",
                      claim=f"Project {args.name}: {args.description or 'purpose not set'}",
                      kind="fact", status="current", confidence="confirmed",
                      source_type="human")
    store.add_evidence(_purpose_fid(store, args.name), "human", ev["id"])
    print(f"registered project '{args.name}' paths={p.paths or '(none)'}")
    return 0


def _purpose_fid(store: Store, project: str) -> str:
    f = store.list_facts(project=project, subject="purpose", limit=1)
    return f[0]["id"] if f else ""


def cmd_remember(args) -> int:
    cfg = _cfg(args)
    store = Store(cfg.data_dir)
    ev = store.append_event(
        "human", "note",
        project=args.project, machine=cfg.machine,
        command=args.text[:500],
        payload={"project": args.project, "component": args.component,
                 "kind": args.kind, "confidence": args.confidence,
                 "subject": args.subject or "note", "reason": args.reason},
    )
    fid = store.insert_fact(
        scope_project=args.project, scope_component=args.component,
        scope_machine=cfg.machine or None,
        subject=args.subject or "note", claim=args.text, kind=args.kind,
        status="current" if args.confidence in ("confirmed", "probable") else "unknown",
        confidence=args.confidence, reason=args.reason, source_type="human",
    )
    store.add_evidence(fid, "human", ev["id"])
    store.add_proposal("fact", {"kind": "fact", "scope": {"project": args.project},
                                "subject": args.subject or "note", "claim": args.text,
                                "confidence": args.confidence, "evidence": [ev["id"]],
                                "origin": "human"}, "auto_applied",
                       note=f"human remember {fid}")
    if args.markdown:
        from .consolidate.markdown import regenerate
        regenerate(store, cfg, immediate=True)
    print(f"remembered: {fid} (confidence {args.confidence})")
    return 0


def cmd_consolidate(args) -> int:
    cfg = _cfg(args)
    store = Store(cfg.data_dir)
    from .capture.terminal import ingest_atuin
    if not args.no_atuin:
        n = ingest_atuin(cfg, store)
        if n:
            print(f"ingested {n} atuin rows")
    llm = None
    if args.extractor in ("auto", "llm") and not args.no_llm:
        from .consolidate.extract import extractor_for
        name, llm = extractor_for(cfg)
        if llm is None and args.extractor == "auto":
            print(f"note: LLM unavailable ({name}) — rules only", file=sys.stderr)
        if llm is None and args.extractor == "llm":
            print(f"error: --extractor llm but no LLM configured ({name})", file=sys.stderr)
            return 1
    rep = _consolidate(store, cfg, since=args.since, extractor=args.extractor,
                       scope_project=args.project, dry_run=args.dry_run, llm=llm)
    print(json.dumps({k: rep.get(k) for k in
                      ("events", "since", "groups", "candidates", "dry_run", "report")},
                     indent=1, default=str))
    if not args.dry_run and not args.no_markdown:
        from .consolidate.markdown import regenerate
        changed = regenerate(store, cfg, commit=not args.no_commit, immediate=True)
        if changed:
            print(f"markdown: {len(changed)} files updated, git-committed in {cfg.data_dir}")
    return 0


def _consolidate(store, cfg, **kw):
    from .consolidate.apply import run_consolidation
    return run_consolidation(store, cfg, **kw)


def cmd_capture(args) -> int:
    cfg = _cfg(args)
    from .capture import daemon as d
    every = getattr(args, "consolidate_interval", None)
    if args.capture_cmd == "run":
        sources = args.sources.split(",") if args.sources else \
            ["net", "fs", "docker_events", "docker_snapshots", "journal"]
        sources = [s for s in sources if s in d.SOURCES]
        if args.daemonize:
            d.daemonize(cfg, sources, consolidate_every=every)
            print(f"capture daemon started (log: {cfg.data_dir / 'capture.log'})")
        else:
            d.run(cfg, sources, foreground=True, consolidate_every=every)
    elif args.capture_cmd == "stop":
        print("stopped" if d.stop(cfg) else "not running")
    elif args.capture_cmd == "status":
        st = d.status(cfg)
        print(json.dumps(st))
    elif args.capture_cmd == "install-systemd":
        if every is not None:
            cfg.consolidate_interval = every
        sources = args.sources.split(",") if args.sources else \
            ["net", "fs", "docker_events", "docker_snapshots", "journal"]
        unit = d.install_systemd_user(cfg, [s for s in sources if s in d.SOURCES])
        print(f"installed {unit}" if unit else "systemd user session not available")
    return 0


def cmd_install(args) -> int:
    cfg = _cfg(args)
    if getattr(args, "claude", False):
        _install_claude(cfg)
    return 0


def _venv_memor() -> str:
    p = Path(__file__).resolve().parent.parent / ".venv" / "bin" / "memor"
    return str(p) if p.exists() else shutil.which("memor") or "memor"


def _install_claude(cfg: Config) -> None:
    memor = _venv_memor()
    # 1) PostToolUse hook (Bash matcher) → command output + session correlation
    settings = Path.home() / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True, exist_ok=True)
    data = {}
    if settings.exists():
        try:
            data = json.loads(settings.read_text() or "{}")
        except json.JSONDecodeError:
            data = {}
    if not (settings.exists() and "memor" in settings.read_text()):
        backup = settings.with_suffix(".json.bak-memor")
        if settings.exists() and not backup.exists():
            shutil.copy2(settings, backup)
        hooks = data.setdefault("hooks", {})
        ptu = hooks.setdefault("PostToolUse", [])
        entry = {"matcher": "Bash",
                 "hooks": [{"type": "command",
                            "command": f'"{memor}" agent-event'}]}
        if not any("memor" in json.dumps(h.get("hooks", [])) for h in ptu):
            ptu.append(entry)
        settings.write_text(json.dumps(data, indent=2) + "\n")
        print(f"PostToolUse(Bash) hook registered in {settings}")
    else:
        print("PostToolUse hook already present")
    # 2) MCP server (user scope)
    r = subprocess.run(["claude", "mcp", "list"], capture_output=True, text=True)
    if r.returncode == 0 and "memor" in (r.stdout + r.stderr):
        print("MCP server 'memor' already registered")
    else:
        r = subprocess.run(
            ["claude", "mcp", "add", "-s", "user", "memor", "--", memor, "serve"],
            capture_output=True, text=True)
        print(("registered MCP server 'memor' (user scope)" if r.returncode == 0
               else f"mcp add failed: {r.stderr.strip()[:200]}"))
    # 3) shell hook snippet
    hint = cfg.data_dir / "shell-hook.txt"
    hint.write_text(
        "# memor: add this one line to your .bashrc (bash) — see hooks/ for zsh\n"
        f'eval "$("{_venv_memor()}" shell-hook bash)"\n')
    print(f"shell hook: add to .bashrc →  eval \"$({_venv_memor()} shell-hook bash)\"")


def cmd_install_git_hooks(args) -> int:
    cfg = _cfg(args)
    store = Store(cfg.data_dir)
    repos = args.repos or [os.getcwd()]
    if not repos:
        repos = [os.getcwd()]
    done = _install_hooks(cfg, store, repos)
    for r in done:
        print(f"git hooks → {r}")
    if not done:
        print("no valid repos given")
    return 0


def _install_hooks(cfg, store, repos):
    from .capture.githooks import install_hooks
    return install_hooks(cfg, store, repos)


def cmd_shell_hook(args) -> int:
    """Print shell snippet that, when eval'd, installs preexec/precmd capture."""
    memor = _venv_memor()
    if args.shell == "zsh":
        print(f"""
_memor_preexec() {{ _MEMOR_T0=$(date +%s%N); _MEMOR_EXIT=$?; }}
_memor_precmd() {{
  [ -n "${{MEMOR_DISABLED:-}}" ] && return
  local cmd="${{commands[$HISTCMD]:-}}"
  [ -z "$cmd" ] && return
  case "$cmd" in __memor_*|_memor_*|eval*"shell-hook"*) return;; esac
  local t1=$(date +%s%N)
  MEMOR_CMD="$cmd" MEMOR_EXIT="${{ _MEMOR_EXIT:-0 }}" MEMOR_T0="${{ _MEMOR_T0:-0 }}" MEMOR_T1="$t1" \\
    MEMOR_CWD="$PWD" MEMOR_SESSION="$$" MEMOR_SHELL=zsh \\
    nohup "{memor}" shell-event >/dev/null 2>&1 &!
  _MEMOR_EXIT=
}}
preexec_functions=(${{preexec_functions:-}} _memor_preexec)
precmd_functions=(${{precmd_functions:-}} _memor_precmd)
""")
    else:
        print(f"""
__memor_precmd() {{
  [ -z "${{MEMOR_DISABLED:-}}" ] || return 0
  local __memor_cmd
  __memor_cmd=$(HISTTIMEOUT=0 history 1 2>/dev/null | sed 's/^[ ]*[0-9]*[ ]*//')
  case "$__memor_cmd" in __memor_*|eval*__memor*) return 0;; esac
  local __memor_exit=$?
  MEMOR_CMD="$__memor_cmd" MEMOR_EXIT=$__memor_exit \\
    MEMOR_CWD="$PWD" MEMOR_SESSION="$$" MEMOR_SHELL=bash \\
    nohup "{memor}" shell-event >/dev/null 2>&1 &
  # disown so the (brief) capture job is never announced as a job
  # completion notification: '[1]+ Done ... shell-event'
  disown 2>/dev/null || true
  return 0
}}
case $PROMPT_COMMAND in *__memor_precmd*) ;; *)
  PROMPT_COMMAND="__memor_precmd${{PROMPT_COMMAND:+;$PROMPT_COMMAND}}" ;;
esac
""")
    return 0


def cmd_shell_event(args) -> int:
    cfg = _cfg(args)
    from .capture.terminal import shell_event
    shell_event(cfg, Store(cfg.data_dir))
    return 0


def cmd_agent_event(args) -> int:
    cfg = _cfg(args)
    from .capture.terminal import agent_event
    agent_event(cfg, Store(cfg.data_dir), sys.stdin.read())
    return 0


def cmd_git_hook(args) -> int:
    cfg = _cfg(args)
    from .capture.githooks import run_git_hook
    run_git_hook(cfg, Store(cfg.data_dir), args.hook, args.hook_args or None)
    return 0


# ------------------------------------------------------------------- query

def _print_json(s: str) -> None:
    print(s)


def cmd_search(args) -> int:
    from .search import run_search, scope_from_args
    cfg = _cfg(args)
    store = Store(cfg.data_dir)
    scope = scope_from_args(args)
    if not scope.workspace:
        scope.workspace = os.getcwd()  # the session's workspace by default
    out = run_search(store, cfg, args.query, scope, args.mode, args.limit)
    print(json.dumps(out))
    return 0


def cmd_current(args) -> int:
    from . import mcp_server as m
    m._cfg = _cfg(args)
    if args.at:
        _print_json(m.tool_get_state_as_of(args.entity, args.at))
    else:
        _print_json(m.tool_get_current_state(args.entity))
    return 0


def cmd_history(args) -> int:
    from . import mcp_server as m
    m._cfg = _cfg(args)
    _print_json(m.tool_get_history(args.entity, args.limit))
    return 0


def cmd_related(args) -> int:
    from . import mcp_server as m
    m._cfg = _cfg(args)
    _print_json(m.tool_get_related(args.entity, args.depth))
    return 0


def cmd_decisions(args) -> int:
    from . import mcp_server as m
    m._cfg = _cfg(args)
    _print_json(m.tool_get_decisions(args.project, args.component))
    return 0


def cmd_evidence(args) -> int:
    from . import mcp_server as m
    m._cfg = _cfg(args)
    _print_json(m.tool_get_evidence(args.fact_id))
    return 0


def cmd_context(args) -> int:
    from . import mcp_server as m
    m._cfg = _cfg(args)
    if getattr(args, "injection", False):
        # the SessionStart hook package (summary trees + state + todos)
        from .sessionmem import injection_context
        print(injection_context(_cfg(args), Store(_cfg(args).data_dir),
                                args.project, getattr(args, "workspace", None)))
        return 0
    _print_json(m.tool_get_project_context(args.project, args.evidence, args.max_chars))
    return 0


# ------------------------------------------------------------- session-memory

def cmd_session_event(args) -> int:
    """Harness command hook (JSON on stdin). Must stay <100 ms: one row +
    a state bump, plus (detached mode) spawning the flush worker."""
    cfg = _cfg(args)
    text = sys.stdin.read() if not sys.stdin.isatty() else ""
    if not text.strip():
        return 0
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return 0  # never fail the harness
    from .sessionmem import session_event
    store = Store(cfg.data_dir)
    r = session_event(cfg, store, args.harness, args.event, payload)
    print(json.dumps(r))
    return 0


def cmd_inject(args) -> int:
    """Session context delivery. Renders the workspace's session-tree
    document. Default: raw doc on stdout (manual/dev). With --envelope:
    stdout is the Claude-Code hookSpecificOutput envelope the DSH
    hooks-claude-code bridge merges into the model's context. The bridge
    discards hook output whose hookEventName names a different event than
    the one firing, so the emitted event must match the hook point.
    --point session-start (default): best-effort delivery when the session
    loads/creates. The bridge injects the result via agent.inject(), which
    is lost if no turn has started yet — so this alone does NOT guarantee
    first-turn delivery.
    --point user-prompt: the guaranteed first-turn delivery. The hook runs
    at the first pre-step (right after the user's message) and its context
    is merged into that turn's messages — the same moment the host injects
    its own AGENTS.md reminders. Runs once per session lifetime: a marker
    in the store suppresses re-delivery (the conversation history already
    holds the envelope, so it must not be duplicated). The session id is
    read from the hook's stdin payload; without one, nothing is emitted.
        {"hookSpecificOutput": {"hookEventName": "SessionStart",
                               "additionalContext": "<doc>"}}
    The hook runs in the session's workspace (the bridge sets the cwd), so
    --workspace is only needed for manual runs. Status always goes to
    stderr so it never contaminates the envelope.
    An empty doc (no summaries yet, or injection disabled) prints nothing:
    the bridge then injects no context, which is the correct answer."""
    cfg = _cfg(args)
    store = Store(cfg.data_dir)
    from .sessionmem import injection_context
    point = args.point
    session_id = None
    if point == "user-prompt":
        text = sys.stdin.read() if not sys.stdin.isatty() else ""
        if text.strip():
            try:
                session_id = json.loads(text).get("session_id")
            except json.JSONDecodeError:
                session_id = None
        if not session_id:
            print(json.dumps({"ok": False, "reason": "no session_id on "
                              "stdin (required for once-per-session "
                              "delivery)"}), file=sys.stderr)
            return 0
    workspace = args.workspace or os.getcwd()
    doc = injection_context(cfg, store, None, workspace)
    if doc:
        if point == "user-prompt" and store.envelope_delivered(session_id):
            print(json.dumps({"ok": True, "injected": False,
                              "already-delivered": True,
                              "session": session_id}), file=sys.stderr)
            return 0
        if args.envelope:
            event = {"user-prompt": "UserPromptSubmit"}.get(
                point, "SessionStart")
            print(json.dumps({"hookSpecificOutput": {
                "hookEventName": event,
                "additionalContext": doc}}))
        else:
            print(doc)
        if point == "user-prompt":
            store.mark_envelope_delivered(session_id)
    print(json.dumps({"ok": True, "injected": bool(doc),
                      "point": point, "workspace": workspace,
                      "chars": len(doc)}), file=sys.stderr)
    return 0


def cmd_state(args) -> int:
    """Workspace state: the structured 'what is true right now' surface
    (open items, problems, decisions, conventions, commands, workflows,
    checklists, ...). `show` renders the document (or a filtered
    listing with --category/--status/--limit/--age-days); add/set write
    (store is source of truth; `set` takes multiple ids); `sweep`
    soft-closes untouched rows; `dedupe` merges near-duplicates;
    `project` forces the AGENTS.local.md file projection (config-gated
    otherwise — session_state_project_file)."""
    from .wstate import state_document, state_listing
    cfg = _cfg(args)
    store = Store(cfg.data_dir)
    ws = args.workspace or os.getcwd()
    if args.state_cmd == "show":
        if args.category or args.status or args.limit or args.age_days:
            print(state_listing(store, ws, category=args.category,
                                status=args.status, limit=args.limit,
                                age_days=args.age_days))
        else:
            print(state_document(store, ws))
        return 0
    if args.state_cmd == "add":
        row = store.workspace_state_upsert(
            ws, args.category, args.subject, args.body,
            source_session=args.source_session,
            source_span=args.source_span)
        print(json.dumps({"ok": True, "id": row["id"],
                          "status": row["status"], "workspace": ws}),
              file=sys.stderr)
    elif args.state_cmd == "set":
        # fault-tolerant bulk: one bad id (typo, absorbed row) must not
        # abort the rest — report it and continue
        results, errors = [], []
        for wid in args.ids:
            try:
                row = store.workspace_state_set(wid, args.status,
                                                body=args.body)
                results.append({"id": row["id"],
                                "status": row["status"]})
            except ValueError as e:
                errors.append({"id": wid, "error": str(e)})
        out = {"ok": not errors, "n": len(results), "rows": results}
        if errors:
            out["errors"] = errors
        print(json.dumps(out), file=sys.stderr)
        if results:
            ws = store.workspace_state_get(results[0]["id"])["workspace"]
    elif args.state_cmd == "project":
        pass
    elif args.state_cmd == "sweep":
        rows = store.workspace_state_sweep(ws, args.days,
                                           dry_run=args.dry_run)
        print(json.dumps({
            "dry_run": args.dry_run, "days": args.days, "n": len(rows),
            "ids": [r["id"] for r in rows]}), file=sys.stderr)
    elif args.state_cmd == "dedupe":
        _state_dedupe(cfg, store, ws, args)
        return 0
    else:  # argparse should prevent this
        print("state needs show|add|set|project|sweep|dedupe",
              file=sys.stderr)
        return 2
    path = None
    from .wstate import project_state_file
    if args.state_cmd == "project":
        path = project_state_file(ws, store)
    elif args.state_cmd in ("add", "set") and cfg.session_state_project_file:
        path = project_state_file(ws, store)
    if path is not None:
        print(f"projected: {path}", file=sys.stderr)
    return 0


def _state_dedupe(cfg, store, ws: str, args) -> None:
    """P4: cluster the workspace's active rows (same category, cosine >=
    threshold) and, with --apply, close the absorbed rows as superseded
    by the cluster's keeper (oldest row). Dry run by default."""
    import numpy as np
    from .embed import get_embedder
    from .search import ensure_cached_embeddings
    rows = store.workspace_state_list(ws, status="active")
    if len(rows) < 2:
        print("(nothing to dedupe)")
        return
    emb = get_embedder(cfg)
    if emb is None:
        print("note: no embedder available — exact-key dedup is already "
              "handled at write time; nothing to merge", file=sys.stderr)
        return
    have = ensure_cached_embeddings(
        store, emb, "state",
        {r["id"]: f"{r['subject']} — {r['body']}" for r in rows})
    vecs = {r["id"]: np.frombuffer(have[r["id"]], dtype=np.float32)
            for r in rows}
    # union-find within each category (merges never cross categories);
    # decaying categories get the lower bar, reference categories the
    # stricter one (related-but-distinct decisions reach ~0.87)
    thr_decay = (args.threshold
                 if args.threshold is not None
                 else cfg.state_promote_merge_threshold)
    thr_ref = (args.threshold
               if args.threshold is not None
               else cfg.state_promote_merge_threshold_ref)
    parent = {r["id"]: r["id"] for r in rows}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    by_cat: dict[str, list] = {}
    for r in rows:
        by_cat.setdefault(r["category"], []).append(r)
    for cat, cat_rows in by_cat.items():
        t = (thr_decay
             if cat in ("open_item", "problem") else thr_ref)
        for i in range(len(cat_rows)):
            for j in range(i + 1, len(cat_rows)):
                a, b = cat_rows[i]["id"], cat_rows[j]["id"]
                if float(vecs[a] @ vecs[b]) >= t and find(a) != find(b):
                    parent[find(a)] = find(b)
    clusters: dict[str, list] = {}
    for r in rows:
        clusters.setdefault(find(r["id"]), []).append(r)
    merges = {k: v for k, v in clusters.items() if len(v) >= 2}
    if not merges:
        print("no near-duplicate clusters")
        return
    # insertion order (rowid) breaks same-second created_at ties — the
    # keeper is the row that has been on record the longest
    order = {rid: i for i, (rid,) in enumerate(store._conn().execute(
        "SELECT id FROM workspace_state WHERE workspace=? AND"
        " status='active' ORDER BY rowid", (ws,)))}
    for members in merges.values():
        keeper = min(members,
                     key=lambda r: (r["created_at"], order.get(r["id"], 0)))
        print(f"KEEP   [{keeper['id']}] ({keeper['category']}) "
              f"{keeper['subject']}")
        for m in members:
            if m["id"] == keeper["id"]:
                continue
            line = f"  merge [{m['id']}] {m['subject']}"
            if args.apply:
                store.workspace_state_absorb(m["id"], keeper["id"])
                line += "  -> superseded by keeper"
            print(line)
    print(f"{len(merges)} cluster(s) of >=2 — "
          + ("applied" if args.apply
             else "dry run (re-run with --apply to merge)"))


def cmd_window_flush(args) -> int:
    """One window worker (detached execution). Spawned by session-event;
    also runnable by hand for catch-up (`--all` scans every open session)."""
    cfg = _cfg(args)
    store = Store(cfg.data_dir)
    from .sessionmem import flush_window, _sessions, window_steps_for
    if args.all:
        reports = []
        for sid, st in sorted(_sessions(store).items()):
            n = window_steps_for(cfg, st)
            if n is None:
                # depth-exempt (stale state row / config change):
                # flush_window's gate cleans the state up
                reports.append(flush_window(
                    cfg, store, st.get("harness", "dsh"), sid))
                continue
            ready = (st.get("steps", 0) >= n
                     or (st["stopped"] and st["events_since_last"] >= 1))
            if not ready and not args.force:
                continue
            reports.append(flush_window(cfg, store, st.get("harness", "dsh"), sid))
        _print_json(json.dumps(reports, indent=2))
    else:
        if not (args.harness and args.session):
            print("window-flush needs --harness + --session (or --all)", file=sys.stderr)
            return 2
        _print_json(json.dumps(flush_window(cfg, store, args.harness, args.session,
                                            force=args.force), indent=2))
    return 0


def cmd_session_watch(args) -> int:
    """Resident execution mode (alternative to detached workers): poll the
    state snapshot and flush ready windows. Default is detached."""
    cfg = _cfg(args)
    store = Store(cfg.data_dir)
    from .sessionmem import watch_loop
    stop = {"stop": False}
    try:
        watch_loop(cfg, store, poll=args.poll, stop=stop)
    except KeyboardInterrupt:
        pass
    return 0


def cmd_span(args) -> int:
    """Read raw harness session-log events (the drill-down path).
    Accepts a span `<harness>:<session>:seq[lo..hi]` or explicit flags."""
    cfg = _cfg(args)
    from .sessionlog import dsh_session_file, events_in_range, span_format, span_parse
    from .sessionmem import _sessions
    if args.span:
        harness, session, lo, hi = span_parse(args.span)
    else:
        if not (args.harness and args.session):
            print("span needs a SPAN argument or --harness + --session",
                  file=sys.stderr)
            return 2
        lo = args.start or 1
        hi = args.end or 10**9
        # default: the journaled window of this session, if known
        if not (args.start or args.end):
            st = _sessions(Store(cfg.data_dir)).get(args.session)
            if st:
                lo = (st.get("last_span_hi") or 0) + 1
                hi = args.end or 10**9
        harness = args.harness
        session = args.session
    path = dsh_session_file(harness, session, cfg.session_dsh_root)
    if path is None:
        print(f"session log not found for {harness}:{session} "
              f"(root: {cfg.session_dsh_root})", file=sys.stderr)
        return 1
    evs, first_seq, last_seq = events_in_range(path, lo, hi)
    evs = evs[: args.limit]
    if args.json:
        _print_json(json.dumps(evs, indent=2, ensure_ascii=False, default=str))
        return 0
    for ev in evs:
        t = ev.get("type")
        d = ev.get("data") or {}
        seq = ev.get("seq", "?")
        if t == "user/message":
            body = _span_text(d.get("content"))[:200]
            print(f"[{seq}] {t}  {body}")
        elif t == "assistant/message":
            body = _span_text((d.get("message") or {}).get("content"))[:200]
            print(f"[{seq}] {t}  {body}")
        elif t == "tool/call":
            a = d.get("arguments")
            a = a if isinstance(a, str) else json.dumps(a, default=str)
            print(f"[{seq}] {t}  {d.get('name', '?')}({a[:200]})")
        elif t == "tool/result":
            blocks = ((d.get("message") or {}).get("content")) or []
            err = any(b.get("isError") for b in blocks if isinstance(b, dict))
            body = ""
            for b in blocks:
                if isinstance(b, dict) and b.get("type") == "tool-result":
                    body = _span_text(b.get("content"))
            print(f"[{seq}] {t}{' [ERROR]' if err else ''}  {body[:200]}")
        else:
            print(f"[{seq}] {t}  {json.dumps(d, ensure_ascii=False, default=str)[:200]}")
    return 0


def _span_text(content) -> str:
    out = []
    for b in content or []:
        if isinstance(b, dict) and b.get("type") == "text":
            out.append(str(b.get("text", "")))
    return "\n".join(t for t in out if t)


def cmd_summaries(args) -> int:
    """Browse the summary trees (project axis or one session)."""
    store = Store(_cfg(args).data_dir)
    rows = store.list_summaries(project=args.project, session=args.session,
                                workspace=getattr(args, "workspace", None),
                                order="asc", limit=args.limit)
    if not rows:
        print("(no summaries yet)")
        return 0
    if args.tree:
        by_parent: dict[str | None, list[dict]] = {}
        for r in rows:
            by_parent.setdefault(r.get("parent_id"), []).append(r)
        import sys as _s
        def walk(pid, depth):
            for r in sorted(by_parent.get(pid, []), key=lambda x: x["ts"]):
                addr = r.get("span") or (
                    f"refs:{len(r['references'])}" if r["references"] else "")
                mark = "" if r["status"] == "current" else f" ({r['status']})"
                # structured material lives in its own columns, not the
                # text — the marker says which fields are populated
                extra = (f" [d:{len(r['decisions'])}"
                         f" p:{len(r['problems'])}"
                         f" o:{len(r['open_items'])}]"
                         if (r["decisions"] or r["problems"]
                             or r["open_items"]) else "")
                print("  " * depth + f"L{r['level']} {r['ts'][:10]} {r['id']}"
                      f" {addr}{extra}{mark}")
                print("  " * depth + f"      {r['text'][:120].replace(chr(10), ' ')}")
                walk(r["id"], depth + 1)
        walk(None, 0)
    else:
        for r in rows:
            addr = r.get("span") or (f"refs:{len(r['references'])}" if r["references"] else "")
            print(f"{r['ts']} L{r['level']} [{r['axis']}] {r['project'] or '-'}"
                  f" {r['id']} {addr} :: {r['text'][:100].replace(chr(10), ' ')}")
    return 0


def cmd_temporal(args) -> int:
    """Materialize + show lazy temporal rollups (day -> week -> month)."""
    cfg = _cfg(args)
    store = Store(cfg.data_dir)
    from datetime import date as _date, timedelta as _td
    from .scope import machine_name
    from .sessionmem import temporal_rollup
    machine = args.machine or machine_name()
    start = _date.fromisoformat(args.start) if args.start else _date.today()
    step = {"day": 1, "week": 7, "month": 30}[args.unit]
    out = []
    for i in range(args.days):
        out.append(temporal_rollup(cfg, store, machine, args.unit,
                                   start + _td(days=step * i),
                                   getattr(args, "workspace", None)))
    _print_json(json.dumps(out, indent=2, ensure_ascii=False, default=str))
    return 0


def cmd_sessions(args) -> int:
    """List DSH sessions on this machine with their journaling status:
    unprocessed (nothing journaled yet), behind (pointer < log end),
    current (pointer at the log's last seq)."""
    cfg = _cfg(args)
    store = Store(cfg.data_dir)
    from .sessionlog import list_session_logs, session_info, events_in_range
    from .sessionmem import _sessions
    sess = _sessions(store)
    logs = list_session_logs(cfg.session_dsh_root)
    out = []
    for l in logs:
        rec = session_info(l["path"]) or {}
        ws = rec.get("cwd") or l["workspace_dir"]
        if args.workspace and ws != args.workspace:
            continue
        sid = l["session_id"]
        st = sess.get(sid)
        last_seq = None
        if st and st.get("last_span_hi") is not None:
            _, _, last_seq = events_in_range(l["path"], 1, 10**9)
            status = "current" if st["last_span_hi"] >= (last_seq or 0) else "behind"
        else:
            status = "unprocessed"
        out.append({
            "session": sid,
            "workspace": ws,
            "origin": rec.get("origin") or "top",
            "depth": rec.get("delegationDepth") or 0,
            "started": rec.get("createdAt"),
            "last_span_hi": (st or {}).get("last_span_hi"),
            "last_seq": last_seq,
            "windows": len(store.list_summaries(session=sid, level=1,
                                                limit=10000)),
            "status": status,
        })
    if args.json:
        _print_json(json.dumps(out, indent=2, ensure_ascii=False, default=str))
    else:
        for r in out:
            origin = (f"sub:d{r['depth']}" if r["origin"] != "top" else "-")
            print(f"{r['status']:<12} {r['session']} {origin:<8} win={r['windows']:<3}"
                  f" hi={str(r['last_span_hi'] or '-'):<8} last={str(r['last_seq'] or '-'):>6}"
                  f"  {r['workspace']}")
    return 0


def cmd_backfill(args) -> int:
    """Journal an existing DSH session's log through the window pipeline.
    Deterministic input (the log is immutable; boundaries = Nth tool call),
    resumable (pointer in the store), and `--reprocess` cascades away the
    session's existing series before rebuilding it under the current
    config/params."""
    cfg = _cfg(args)
    store = Store(cfg.data_dir)
    from .sessionmem import backfill_session
    r = backfill_session(cfg, store, args.session, harness=args.harness,
                         dry_run=args.dry_run, max_windows=args.max_windows,
                         reprocess=args.reprocess)
    _print_json(json.dumps(r, indent=2, ensure_ascii=False, default=str))
    return 0 if r.get("ok") else 1


def cmd_window_review(args) -> int:
    """Review what the summarizer saw and said. Per LLM call (audit rows
    in `llm_calls`): the exact prompt sent (digest / child texts), the raw
    model response, and the stored summary it produced. Windows processed
    before the audit existed fall back to a deterministic digest
    reconstruction (marked as such)."""
    cfg = _cfg(args)
    store = Store(cfg.data_dir)
    from .sessionlog import dsh_session_file, events_in_range, span_parse
    from .sessionmem import build_digest
    calls = store.list_llm_calls(session=args.session, limit=args.limit)
    reconstructed = []
    if args.session:
        audited = {c.get("span") for c in calls if c.get("span")}
        reconstructed = [s for s in
                          store.list_summaries(session=args.session, level=1,
                                               order="asc", limit=1000)
                          if s["span"] and s["span"] not in audited]
    if args.json:
        out = []
        for c in calls:
            e = {k: c.get(k) for k in
                 ("id", "ts", "session", "span", "mode", "model", "ok")}
            try:
                e["prompt"] = json.loads(c["prompt"])
            except (json.JSONDecodeError, TypeError):
                e["prompt"] = c.get("prompt")
            e["raw_response"] = c.get("raw_response")
            if c.get("span"):
                s = store.get_summary_by_span(c["span"])
                if s:
                    e["summary"] = {"id": s["id"], "status": s["status"],
                                    "text": s["text"]}
            out.append(e)
        for s in reconstructed:
            out.append({"reconstructed": True, "summary_id": s["id"],
                        "span": s["span"], "summary": {"text": s["text"]}})
        _print_json(json.dumps(out, indent=2, ensure_ascii=False, default=str))
        return 0
    for c in calls:
        span = c.get("span")
        print(f"== {c['ts']}  {c['mode']}  {span or '-'}  "
              f"model={c.get('model') or '?'}  {'OK' if c['ok'] else 'FAILED'}")
        try:
            prompt = json.loads(c["prompt"])
        except (json.JSONDecodeError, TypeError):
            prompt = c.get("prompt")
        print("  prompt (sent to the model):")
        print("  " + json.dumps(prompt, indent=2, ensure_ascii=False,
                                default=str).replace("\n", "\n  "))
        raw = c.get("raw_response")
        print("  raw response:")
        if raw is None:
            print("  (none - model was unavailable)")
        elif raw:
            print("  " + raw.replace("\n", "\n  "))
        else:
            print("  (empty)")
        if span:
            s = store.get_summary_by_span(span)
            if s:
                print(f"  stored summary [{s['status']}]: "
                      f"{s['text'][:400].strip()}")
            print(f"  raw events: memor span {span}")
        print()
    for s in reconstructed:
        span = s["span"]
        print(f"== {s['ts']}  window  {span}  (reconstructed - no audit row)")
        try:
            _, sess, lo, hi = span_parse(span)
            p = dsh_session_file("dsh", sess, cfg.session_dsh_root)
            evs, _, _ = events_in_range(p, lo, hi) if p else ([], 1, 0)
            digest, np_, nt = build_digest(cfg, evs)
            print(f"  digest: {np_} prompts, {nt} tool calls")
            print("  " + digest.replace("\n", "\n  "))
        except Exception as e:  # noqa: BLE001 — review must never crash
            print(f"  (reconstruction failed: {e})")
        print(f"  stored summary: {s['text'][:400].strip()}")
        print()
    if not calls and not reconstructed:
        print("(no LLM calls recorded"
              + (f" for session {args.session}" if args.session else "") + ")")
    return 0


def cmd_switchover(args) -> int:
    """Apply the session-memory regime to THIS store (SESSION-MEMORY.md §9):
    consolidation off, LLM autosync off, state polling off (default), and
    the pending LLM queue lapses (cursor advanced; nothing is processed).
    The capture daemon must be restarted for the interval changes to take
    effect. Run it yourself — it changes a running system."""
    cfg = _cfg(args)
    store = Store(cfg.data_dir)
    from .scope import machine_name
    before = {"consolidate_interval": cfg.consolidate_interval,
              "llm_autosync": cfg.llm_autosync,
              "verify_enabled": getattr(cfg, "verify_enabled", True)}
    cfg.consolidate_interval = 0.0
    cfg.llm_autosync = False
    cfg.verify_enabled = bool(args.keep_verify)
    # lapse the pending LLM windows: advance the LLM cursor to now so the
    # old queue is simply no longer pending (never processed)
    pending = store.meta_get("llm.cursor")
    store.meta_set("llm.cursor", now_utc())
    cfg.save()
    store.append_event("memor", "session_memory_switchover",
                       machine=machine_name(),
                       command="memor switchover",
                       payload={"before": before,
                                "pending_lapsed_from": pending})
    print(json.dumps({
        "consolidate_interval": cfg.consolidate_interval,
        "llm_autosync": cfg.llm_autosync,
        "verify_enabled": cfg.verify_enabled,
        "llm_queue": "lapsed (cursor advanced, nothing processed)",
        "note": "restart the capture daemon for the interval changes to "
                "take effect (systemctl --user restart memor-capture)",
    }, indent=2))
    return 0


def cmd_recent(args) -> int:
    from . import mcp_server as m
    m._cfg = _cfg(args)
    _print_json(m.tool_get_recent_changes(args.project, args.machine, args.hours))
    return 0


def cmd_report(args) -> int:
    from . import mcp_server as m
    m._cfg = _cfg(args)
    if args.project:
        _print_json(m.tool_get_project_context(args.project, args.evidence, args.max_chars))
    else:
        store = Store(_cfg(args).data_dir)
        print("projects:", ", ".join({f["scope_project"] for f in store.list_facts(limit=500)
                                      if f.get("scope_project")}) or "(none)")
        rec = m.tool_get_recent_changes(None, None, args.hours)
        _print_json(rec)
    return 0


def cmd_events(args) -> int:
    store = Store(_cfg(args).data_dir)
    if args.since:
        evs = store.events_since(args.since, source=args.source, limit=args.limit)
    else:
        evs = store.recent_events(limit=args.limit, source=args.source)
        evs.reverse()
    for e in evs:
        cmd = (e.get("command") or "")[:120]
        print(f"{e['ts']}  {e['source']:<10} {e['kind']:<16} {cmd}  [{e['id']}]")
    return 0


def cmd_review(args) -> int:
    cfg = _cfg(args)
    store = Store(cfg.data_dir)
    from .consolidate.apply import review_accept, review_reject
    if args.review_cmd == "accept":
        fid = review_accept(store, args.id)
        from .consolidate.markdown import regenerate
        regenerate(store, cfg, immediate=True)
        print(f"accepted → {fid}")
    elif args.review_cmd == "reject":
        review_reject(store, args.id, note=args.note)
        from .consolidate.markdown import regenerate
        regenerate(store, cfg, immediate=True)
        print("rejected")
    else:
        for r in store.open_reviews():
            prop = store.get_proposal(r["proposal_id"]) or {}
            print(f"{r['id']}  [{r['category']:<14}] {r['summary']}")
            p = prop.get("payload", {})
            if p.get("evidence"):
                print(f"            evidence: {', '.join(p['evidence'][:5])}")
        print("(accept: memor review accept <id> | reject: memor review reject <id>)")
    return 0


def cmd_correct(args) -> int:
    cfg = _cfg(args)
    store = Store(cfg.data_dir)
    fid = store.correct_fact(args.fact_id, args.claim, reason=args.reason)
    if not fid:
        print(f"unknown fact id: {args.fact_id}", file=sys.stderr)
        return 1
    from .consolidate.markdown import regenerate
    regenerate(store, cfg, immediate=True)
    print(f"corrected {args.fact_id} -> {fid}")
    return 0


def cmd_forget(args) -> int:
    cfg = _cfg(args)
    store = Store(cfg.data_dir)
    if not store.forget_fact(args.fact_id, reason=args.reason):
        print(f"unknown fact id: {args.fact_id}", file=sys.stderr)
        return 1
    from .consolidate.markdown import regenerate
    regenerate(store, cfg, immediate=True)
    print(f"retracted {args.fact_id}")
    return 0


def cmd_verify(args) -> int:
    cfg = _cfg(args)
    from .capture.verify import run_verification
    rep = run_verification(Store(cfg.data_dir), cfg, machine=args.machine or None)
    print(json.dumps(rep, indent=1, default=str))
    return 0


def cmd_llm(args) -> int:
    cfg = _cfg(args)
    store = Store(cfg.data_dir)
    from .consolidate.apply import run_llm_sync, llm_pending_count
    from .consolidate.extract import extractor_for
    if args.llm_cmd == "sync":
        rep = run_llm_sync(store, cfg, dry_run=args.dry_run, since=args.since)
        keep = ("provider", "events", "pending", "groups", "candidates", "report")
        print(json.dumps({k: rep[k] for k in keep if k in rep}, indent=1, default=str))
        return 0
    name, llm = extractor_for(cfg)
    avail = bool(llm is not None and llm.available())
    print(json.dumps({
        "provider": name,
        "model": cfg.llm_model,
        "base_url": cfg.llm_base_url or None,
        "max_tokens": cfg.llm_max_tokens,
        "available": avail,
        "enabled": cfg.llm_enabled,
        "autosync": cfg.llm_autosync,
        "pending": llm_pending_count(store),
    }, indent=1))
    return 0


def cmd_run(args) -> int:
    cfg = _cfg(args)
    from .capture.terminal import run_wrapped
    return run_wrapped(cfg, Store(cfg.data_dir), " ".join(args.command),
                       no_project=args.no_project)


def _months_ago(n: int) -> str:
    now = datetime.now(timezone.utc)
    month = now.month - n
    year = now.year + (month - 1) // 12
    month = (month - 1) % 12 + 1
    dim = [31, 29 if (year % 4 == 0 and (year % 100 != 0 or year % 400 == 0))
           else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][month - 1]
    day = min(now.day, dim)
    return datetime(year, month, day, now.hour, now.minute, now.second,
                    tzinfo=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def cmd_compact(args) -> int:
    """A2: retire old raw evidence that is already in memory and unreferenced.
    Level-3 knowledge (facts/decisions/evidence) is never touched."""
    import gzip
    cfg = _cfg(args)
    store = Store(cfg.data_dir)
    if cfg.retention_months <= 0:
        print("retention disabled (retention_months=0 in memor.json)")
        return 0
    cutoff = _months_ago(cfg.retention_months)
    seen = set((store.meta_get("consolidate.seen") or "").splitlines()) | \
        set((store.meta_get("llm.seen") or "").splitlines())
    eligible = store.eligible_for_compaction(cutoff, seen, store.referenced_event_ids())
    if args.dry_run:
        print(json.dumps({"eligible_events": len(eligible), "cutoff": cutoff,
                          "total_events": store.count_events()}, indent=1))
        return 0
    deleted = store.delete_events(eligible)
    archived = []
    cut_month = cutoff[:7]
    ev_dir = cfg.events_dir
    arch_dir = ev_dir / "archive"
    if ev_dir.exists():
        arch_dir.mkdir(exist_ok=True)
        for f in sorted(ev_dir.glob("*.jsonl")):
            if f.name[:7] < cut_month:
                with open(f, "rb") as fin, gzip.open(arch_dir / (f.name + ".gz"), "wb") as fout:
                    fout.write(fin.read())
                f.unlink()
                archived.append(f"archive/{f.name}.gz")
    store._conn().execute("PRAGMA wal_checkpoint(TRUNCATE)")
    print(json.dumps({"deleted_events": deleted, "archived_files": archived,
                      "remaining_events": store.count_events()}, indent=1))
    return 0


def cmd_checkpoint(args) -> int:
    store = Store(_cfg(args).data_dir)
    r = store._conn().execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
    print(f"checkpoint: busy={r[0]} log={r[1]} checkpointed={r[2]}")
    return 0


def cmd_export(args) -> int:
    """D1: portable bundle of the whole store (SQLite online backup + config
    + raw events + Markdown projection + manifest)."""
    import sqlite3
    import tarfile
    import tempfile
    cfg = _cfg(args)
    if not cfg.db_path.exists():
        print("no store to export (run: memor init)", file=sys.stderr)
        return 1
    out = args.out or (f"memor-export-{cfg.machine or 'machine'}-"
                       f"{now_utc().replace(':', '').replace('-', '')[:8]}.tar.gz")
    with tempfile.TemporaryDirectory() as tmp:
        stage = Path(tmp) / "store"
        stage.mkdir()
        src = sqlite3.connect(str(cfg.db_path))
        dst = sqlite3.connect(str(stage / "memor.db"))
        with dst:
            src.backup(dst)
        src.close(); dst.close()
        cfgj = cfg.data_dir / "memor.json"
        if cfgj.exists():
            (stage / "memor.json").write_text(cfgj.read_text())
        ev_dir = cfg.events_dir
        if ev_dir.exists():
            (stage / "events").mkdir()
            for f in ev_dir.glob("*.jsonl"):
                (stage / "events" / f.name).write_bytes(f.read_bytes())
        for sub in ("system", "reference"):
            src_sub = cfg.data_dir / sub
            if src_sub.exists():
                dst_sub = stage / sub
                dst_sub.mkdir()
                for f in src_sub.rglob("*.md"):
                    rel = f.relative_to(src_sub)
                    (dst_sub / rel).parent.mkdir(parents=True, exist_ok=True)
                    (dst_sub / rel).write_bytes(f.read_bytes())
        (stage / "manifest.json").write_text(json.dumps({
            "format": "memor-bundle", "version": __version__,
            "machine": cfg.machine, "exported_at": now_utc(),
            "source_data_dir": str(cfg.data_dir),
        }, indent=2))
        with tarfile.open(out, "w:gz") as tar:
            tar.add(stage, arcname="store")
    print(f"exported store ({cfg.data_dir}) -> {out}")
    return 0


def cmd_import(args) -> int:
    """D1: merge a bundle exported from (possibly another) machine into this
    store. Ids are uuids so re-import is additive; --as-machine reattributes
    the bundle's machine scope (cross-box continuity)."""
    import sqlite3
    import tarfile
    import tempfile
    cfg = _cfg(args)
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        with tarfile.open(args.bundle, "r:gz") as tar:
            tar.extractall(tmpdir, filter="data")
        stage = tmpdir / "store"
        if not (stage / "memor.db").exists():
            print("bundle does not contain a memor store", file=sys.stderr)
            return 1
        bundle_cfg = {}
        if (stage / "memor.json").exists():
            try:
                bundle_cfg = json.loads((stage / "memor.json").read_text() or "{}")
            except json.JSONDecodeError:
                pass
        old_machine = bundle_cfg.get("machine") or None
        new_machine = args.as_machine or old_machine
        store = Store(cfg.data_dir)
        c = store._conn()
        c.execute("ATTACH DATABASE ? AS bundle", (str(stage / "memor.db"),))
        counts = {}
        for table in ("raw_events", "facts", "decisions", "evidence",
                      "entities", "relations", "proposals", "review_queue"):
            try:
                cur = c.execute(f"INSERT OR IGNORE INTO main.{table} "
                                f"SELECT * FROM bundle.{table}")
                counts[table] = cur.rowcount
                cur.close()
            except sqlite3.OperationalError as e:
                counts[table] = f"skipped ({e})"
            if args.as_machine and old_machine and args.as_machine != old_machine:
                m = args.as_machine
                c.execute("UPDATE raw_events SET machine=? WHERE machine=?", (m, old_machine))
                c.execute("UPDATE facts SET scope_machine=? WHERE scope_machine=?", (m, old_machine))
                c.execute("UPDATE decisions SET scope_machine=? WHERE scope_machine=?", (m, old_machine))
                c.execute("UPDATE entities SET machine=? WHERE machine=?", (m, old_machine))
        c.commit()  # close the inserts' transaction before detaching
        c.execute("DETACH DATABASE bundle")
        c.commit()
        for p in bundle_cfg.get("projects", []):
            name = p.get("name")
            if not name:
                continue
            if next((x for x in cfg.projects if x.name == name), None) is None:
                from .config import ProjectRef
                cfg.projects.append(ProjectRef(
                    name=name, paths=p.get("paths", []),
                    remotes=p.get("remotes", []), description=p.get("description", "")))
                store.upsert_entity(name, kind="project", project=name,
                                    description=p.get("description"))
        cfg.save()
        if new_machine:
            store.upsert_entity(new_machine, kind="machine")
        try:
            from .consolidate.markdown import regenerate
            regenerate(store, cfg, immediate=True)
        except Exception:
            pass
    print(json.dumps({"imported": args.bundle, "new_rows": counts,
                      "machine": new_machine}, indent=1, default=str))
    return 0


def cmd_setup(args) -> int:
    """G4: one-shot onboarding — init (if needed), discover git repos under
    the given dirs and register them as projects (name from the remote when
    there is one, so identity survives re-clones), wire the Claude Code hook
    + MCP server when available, print the shell-hook line, and start the
    daemon (systemd user unit, or backgrounded)."""
    import re
    from .config import ProjectRef
    from .scope import git_remote_url
    from .store import Store as _S

    cfg = _cfg(args)
    data_dir = cfg.data_dir
    if not data_dir.exists() or not cfg.db_path.exists():
        print("initializing store...")
        init_args = argparse.Namespace(data_dir=None)
        cmd_init(init_args)
    store = _S(cfg.data_dir)
    if not cfg.machine:
        from .scope import machine_name
        cfg.machine = machine_name()

    dirs = args.dirs or []
    if not dirs:
        for d in ("~/work", "~/projects", "~/src"):
            d = os.path.expanduser(d)
            if os.path.isdir(d):
                dirs.append(d)
    if not dirs:
        dirs = [os.getcwd()]

    def discover(d: str, depth: int = 2):
        d = os.path.expanduser(d)
        if not os.path.isdir(d):
            print(f"note: {d} does not exist — skipped")
            return
        base = Path(d)
        seen = {os.path.realpath(base)}
        for root, names, _ in os.walk(base):
            root = Path(root)
            if os.path.realpath(root) in seen:
                continue
            if len(root.relative_to(base).parts) > depth:
                names[:] = []
                continue
            if (root / ".git").exists():
                seen.add(os.path.realpath(root))
                yield root

    found: dict[str, dict] = {}
    for d in dirs:
        for repo in discover(d):
            remote = git_remote_url(repo)
            name = None
            if remote:
                m = re.search(r"[:/]([^/:]+/[^/:]+?)(?:\.git)?$", remote)
                if m:
                    name = m.group(1).split("/")[-1].lower()
            name = name or repo.name.lower()
            entry = found.setdefault(
                name, {"paths": [], "remotes": [], "description": ""})
            entry["paths"].append(str(repo))
            if remote and remote not in entry["remotes"]:
                entry["remotes"].append(remote)

    registered = []
    for name, info in sorted(found.items()):
        existing = next((p for p in cfg.projects if p.name == name), None)
        if existing is None:
            existing = ProjectRef(name=name, paths=info["paths"],
                                  remotes=info["remotes"],
                                  description=f"auto-discovered under {dirs[0] if dirs else '.'}")
            cfg.projects.append(existing)
            store.upsert_entity(name, kind="project", project=name)
            store.append_event("human", "project_registered", project=name,
                               command=f"registered project {name} (memor setup)",
                               payload={"project": name, "paths": info["paths"],
                                        "remotes": info["remotes"], "auto": True})
        else:
            for p in info["paths"]:
                if p not in existing.paths:
                    existing.paths.append(p)
            for r in info["remotes"]:
                if r not in existing.remotes:
                    existing.remotes.append(r)
        registered.append(name)
    cfg.save()

    if not args.no_claude and shutil.which("claude"):
        print("wiring Claude Code hook + MCP server...")
        _install_claude(cfg)
    else:
        print("claude CLI not found — skipping Claude wiring "
              "(rerun: memor install --claude)")

    shell = os.environ.get("SHELL", "")
    if "zsh" in shell:
        hookfile = "memor-zsh-hook.zsh"
    else:
        hookfile = "memor-shell-hook.bash"
    repo_root = Path(__file__).resolve().parent.parent
    print(f"shell hook: add to your rc file ->  "
          f"source {repo_root / 'hooks' / hookfile}")

    if args.no_daemon:
        print("daemon not started (--no-daemon); start with: memor capture run --daemonize")
    else:
        from .capture import daemon as d
        unit = d.install_systemd_user(cfg, ["net", "fs", "docker_events",
                                            "docker_snapshots", "journal"])
        if unit:
            print(f"capture daemon installed as {unit}")
        else:
            d.daemonize(cfg, ["net", "fs", "docker_events", "docker_snapshots", "journal"])
            print(f"capture daemon started (log: {cfg.data_dir / 'capture.log'})")

    print("\nsetup complete. next:")
    print(f"  memor doctor                    health of every layer")
    print(f"  memor add-project NAME ...      refine auto-discovered projects")
    print(f"  memor context <project>         catch me up")
    return 0


def cmd_episodes(args) -> int:
    from .consolidate.episodes import close_episode
    cfg = _cfg(args)
    store = Store(cfg.data_dir)
    if getattr(args, "episode_cmd", None) == "close":
        rep = close_episode(store, cfg, project=args.project)
        print(rep["report"])
        if rep.get("episode"):
            from .consolidate.markdown import regenerate
            regenerate(store, cfg, immediate=True)
        return 0
    since = None
    if args.since_hours:
        from datetime import datetime, timedelta, timezone
        since = (datetime.now(timezone.utc)
                 - timedelta(hours=args.since_hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
    eps = store.list_episodes(project=args.project, since=since, limit=args.limit)
    for e in eps:
        print(f"{e['started_at'][:16]} → {e['ended_at'][:16]}  "
              f"[{e['project'] or '∅'}/{e['machine'] or '∅'}]  {e['summary'][:140]}  [{e['id']}]")
        for l in (e.get("lessons") or [])[:3]:
            print(f"    lesson: {l[:140]}")
    if not eps:
        print("(no episodes yet — run: memor episode close)")
    return 0


def cmd_global(args) -> int:
    from .mcp_server import tool_get_global_context
    from . import mcp_server as ms
    ms._cfg = _cfg(args)
    _print_json(tool_get_global_context())
    return 0


def cmd_digest(args) -> int:
    cfg = _cfg(args)
    store = Store(cfg.data_dir)
    from .consolidate.digest import build_digest
    path = build_digest(store, cfg, immediate=True)
    print(f"digest written: {path}")
    if args.show:
        print(Path(path).read_text())
    return 0


def cmd_timeline(args) -> int:
    from . import mcp_server as ms
    ms._cfg = _cfg(args)
    _print_json(ms.tool_get_timeline(args.name, args.hours, args.limit))
    return 0


def cmd_serve(args) -> int:
    from .mcp_server import main
    main()
    return 0


# --------------------------------------------------------------------- main

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="memor", description=__doc__)
    ap.add_argument("--version", action="version", version=__version__)
    ap.add_argument("--data-dir", default=None, help="override memory store location")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init")
    sub.add_parser("doctor")
    sub.add_parser("serve")
    sub.add_parser("shell-event")
    sub.add_parser("agent-event")

    p = sub.add_parser("llm", help="LLM sync queue (works when the LLM server is down: records stay queued)")
    lp = p.add_subparsers(dest="llm_cmd")
    s = lp.add_parser("sync")
    s.add_argument("--dry-run", action="store_true")
    s.add_argument("--since", default=None)
    lp.add_parser("status")

    p = sub.add_parser("episodes", help="list work episodes (objective/activity/lessons)")
    p.add_argument("--project", default=None)
    p.add_argument("--since-hours", type=int, default=24 * 7)
    p.add_argument("--limit", type=int, default=20)

    p = sub.add_parser("episode")
    ep = p.add_subparsers(dest="episode_cmd", required=True)
    c = ep.add_parser("close", help="condense the not-yet-episodes work window")
    c.add_argument("--project", default=None)

    sub.add_parser("global", help="D2: cross-machine Level-0 view")

    p = sub.add_parser("timeline", help="G1: chronological 'how did we get here' view")
    p.add_argument("name", help="project:x / machine:y / component:z / entity name")
    p.add_argument("--hours", type=int, default=72)
    p.add_argument("--limit", type=int, default=200)

    p = sub.add_parser("digest", help="G2: write the periodic digest (changes, open items, surprises)")
    p.add_argument("--show", action="store_true")

    p = sub.add_parser("setup", help="one-shot onboarding: init + discover projects + hooks + daemon")
    p.add_argument("dirs", nargs="*", help="dirs to scan for git repos (default: ~/work ~/projects ~/src)")
    p.add_argument("--no-claude", action="store_true")
    p.add_argument("--no-daemon", action="store_true")

    p = sub.add_parser("compact", help="A2: retire old consolidated+unreferenced raw evidence")
    p.add_argument("--dry-run", action="store_true")

    sub.add_parser("checkpoint", help="WAL checkpoint the store")

    p = sub.add_parser("export", help="D1: portable bundle of the whole store")
    p.add_argument("-o", "--out", default=None)

    p = sub.add_parser("import", help="D1: merge an exported bundle (optionally re-attributing the machine)")
    p.add_argument("bundle")
    p.add_argument("--as-machine", default=None)

    p = sub.add_parser("verify", help="re-observe services/ports/containers and "
                         "reconcile current state facts (A1)")
    p.add_argument("--machine", default=None)

    p = sub.add_parser("run", help="run a command with full output captured")
    p.add_argument("--no-project", action="store_true",
                   help="record the run box-wide (machine scope) instead of "
                        "under the project containing the cwd")
    p.add_argument("command", nargs="+")

    p = sub.add_parser("add-project")
    p.add_argument("name")
    p.add_argument("--path", action="append")
    p.add_argument("--remote", action="append",
                   help="git remote URL (repeatable); keeps project identity "
                        "stable across clones and machines")
    p.add_argument("--description", default="")

    p = sub.add_parser("remember")
    p.add_argument("text")
    p.add_argument("--project")
    p.add_argument("--component")
    p.add_argument("--subject", default=None)
    p.add_argument("--kind", default="fact",
                   choices=["fact", "state", "configuration", "preference",
                            "problem", "lesson", "todo", "decision"])
    p.add_argument("--confidence", default="confirmed",
                   choices=["confirmed", "probable", "inferred", "unknown"])
    p.add_argument("--reason", default=None)
    p.add_argument("--no-markdown", dest="markdown", action="store_false", default=True)

    p = sub.add_parser("consolidate")
    p.add_argument("--extractor", default="auto", choices=["auto", "rules", "llm"])
    p.add_argument("--since", default=None)
    p.add_argument("--project", default=None)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--no-commit", action="store_true")
    p.add_argument("--no-markdown", action="store_true")
    p.add_argument("--no-llm", action="store_true")
    p.add_argument("--no-atuin", action="store_true")

    p = sub.add_parser("capture")
    cap = p.add_subparsers(dest="capture_cmd", required=True)
    for name in ("run", "stop", "status", "install-systemd"):
        q = cap.add_parser(name)
        if name in ("run", "install-systemd"):
            q.add_argument("--sources", default=None,
                           help="comma list of: net,fs,docker_events,docker_snapshots,journal")
            q.add_argument("--consolidate-interval", type=float, default=None,
                           help="seconds between scheduled consolidation passes "
                                "(0 disables; default: config, 3600)")
        if name == "run":
            q.add_argument("--daemonize", action="store_true")

    p = sub.add_parser("install")
    p.add_argument("--claude", action="store_true",
                   help="register PostToolUse hook + MCP server with Claude Code")

    p = sub.add_parser("install-git-hooks")
    p.add_argument("repos", nargs="*")

    p = sub.add_parser("shell-hook")
    p.add_argument("shell", nargs="?", default="bash", choices=["bash", "zsh"])

    p = sub.add_parser("git-hook")
    p.add_argument("hook", choices=["post-commit", "post-merge", "post-checkout"])
    p.add_argument("hook_args", nargs="*",
                   help="hook positional args (post-checkout: <prev> <ref> <flag>)")

    p = sub.add_parser("search")
    p.add_argument("query")
    p.add_argument("--mode", default="hybrid",
                   choices=["hybrid", "fts", "semantic"],
                   help="hybrid (default): FTS ∪ semantic, RRF-fused; "
                        "degrades to FTS with a note when no embedder")
    p.add_argument("--project", default=None)
    p.add_argument("--component", default=None)
    p.add_argument("--workspace", default=None,
                   help="scope axis; default: the current working dir")
    p.add_argument("--session", default=None, help="scope: one session id")
    p.add_argument("--machine", default=None, help="scope: one machine")
    p.add_argument("--since", default=None, help="scope: ISO ts (inclusive)")
    p.add_argument("--until", default=None, help="scope: ISO ts (inclusive)")
    p.add_argument("--level", default=None,
                   help="scope: summary levels, e.g. '1' or '1,2'")
    p.add_argument("--kinds", default=None,
                   help="scope: event,fact,decision,summary,state,prompt,entity")
    p.add_argument("--limit", type=int, default=20)

    p = sub.add_parser("current")
    p.add_argument("entity")
    p.add_argument("--at", default=None,
                   help="ISO ts: answer 'what was true about the entity "
                        "THEN' (point-in-time read) instead of 'now'")

    p = sub.add_parser("history")
    p.add_argument("entity")
    p.add_argument("--limit", type=int, default=50)

    p = sub.add_parser("related")
    p.add_argument("entity")
    p.add_argument("--depth", type=int, default=1)

    p = sub.add_parser("decisions")
    p.add_argument("--project", default=None)
    p.add_argument("--component", default=None)

    p = sub.add_parser("evidence")
    p.add_argument("fact_id")

    p = sub.add_parser("context")
    p.add_argument("project")
    p.add_argument("--evidence", action="store_true")
    p.add_argument("--max-chars", type=int, default=0,
                   help="cap output size; low-priority sections drop first")
    p.add_argument("--injection", action="store_true",
                   help="print the SessionStart hook package instead "
                        "(summary trees + state + todos)")
    p.add_argument("--workspace", default=None,
                   help="with --injection: lead with this workspace's tree")

    p = sub.add_parser("inject",
                       help="session context delivery "
                            "(hook: --envelope [--point user-prompt])")
    p.add_argument("--envelope", action="store_true",
                   help="wrap the doc in the hookSpecificOutput envelope "
                        "(hook delivery)")
    p.add_argument("--point", choices=["session-start", "user-prompt"],
                   default="session-start",
                   help="hook point the envelope is for: session-start "
                        "(default; best-effort, can be lost if no turn "
                        "has started) or user-prompt (guaranteed first-"
                        "turn delivery, once per session; session id "
                        "from the stdin payload)")
    p.add_argument("--workspace", default=None,
                   help="workspace cwd (default: the current working dir — "
                        "the hook already runs there)")

    p = sub.add_parser("state",
                       help="workspace state (show|add|set|project|sweep|dedupe)")
    # --workspace rides each subparser (argparse parents): it must be
    # acceptable after the subcommand token, like the subcommand's own
    # flags
    common_ws = argparse.ArgumentParser(add_help=False)
    common_ws.add_argument("--workspace", default=None,
                           help="workspace cwd (default: the current "
                                "working dir)")
    sp = p.add_subparsers(dest="state_cmd", required=True)

    sh = sp.add_parser("show", parents=[common_ws],
                       help="render the state document")
    sh.add_argument("--category", default=None,
                    help="show: one category (default: the grouped document)")
    sh.add_argument("--status", default=None,
                    help="show: active|resolved|retired|stale|superseded "
                         "(default: the document)")
    sh.add_argument("--limit", type=int, default=None)
    sh.add_argument("--age-days", type=float, default=None,
                    help="show: only rows updated within N days")

    ad = sp.add_parser("add", parents=[common_ws], help="add/refresh one item")
    ad.add_argument("--category", required=True,
                    help="open_item|problem|decision|convention|practice|command|workflow|checklist|note (any string)")
    ad.add_argument("--subject", required=True,
                    help="the one-spelling subject key")
    ad.add_argument("--body", required=True, help="the item's content")
    ad.add_argument("--source-session", default=None,
                    help="provenance session id")
    ad.add_argument("--source-span", default=None,
                    help="provenance span")

    st = sp.add_parser("set", parents=[common_ws], help="transition item(s)")
    st.add_argument("ids", nargs="+",
                    help="one or more row ids (memor state show lists them)")
    st.add_argument("--status", required=True,
                    help="active|resolved|retired|stale|superseded")
    st.add_argument("--body", default=None,
                    help="replacement body (with --status superseded)")

    sp.add_parser("project", parents=[common_ws],
                  help="force the AGENTS.local.md projection now")

    sw = sp.add_parser("sweep", parents=[common_ws],
                       help="soft-close untouched active rows (stale)")
    sw.add_argument("--days", type=float, default=7.0,
                    help="mark active rows untouched for N days (default 7)")
    sw.add_argument("--dry-run", action="store_true",
                    help="list without marking")

    dd = sp.add_parser("dedupe", parents=[common_ws],
                       help="find/merge near-duplicate active rows (P1/P4)")
    dd.add_argument("--threshold", type=float, default=None,
                    help="cosine threshold (default: config "
                         "state_promote_merge_threshold)")
    dd.add_argument("--apply", action="store_true",
                    help="merge (default: dry run — list the clusters)")

    p = sub.add_parser("session-event", help="harness command hook (JSON on stdin); must stay <100ms")
    p.add_argument("--harness", required=True)
    p.add_argument("--event", required=True,
                   choices=["start", "prompt", "tool", "stop"])

    p = sub.add_parser("window-flush", help="flush one session window (worker / catch-up)")
    p.add_argument("--harness", default=None)
    p.add_argument("--session", default=None)
    p.add_argument("--all", action="store_true",
                   help="scan every open session for ready windows")
    p.add_argument("--force", action="store_true",
                   help="flush even if not ready / already locked")

    p = sub.add_parser("session-watch", help="resident window worker (alternative to detached)")
    p.add_argument("--poll", type=float, default=2.0)

    p = sub.add_parser("span", help="read raw harness session-log events (drill-down)")
    p.add_argument("span", nargs="?",
                   help="full span <harness>:<session>:seq[lo..hi]")
    p.add_argument("--harness", default=None)
    p.add_argument("--session", default=None)
    p.add_argument("--start", type=int, default=None, help="lo seq")
    p.add_argument("--end", type=int, default=None, help="hi seq")
    p.add_argument("--limit", type=int, default=200)
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("sessions",
                       help="list DSH sessions with journaling status "
                            "(unprocessed / behind / current)")
    p.add_argument("--workspace", default=None,
                   help="only sessions of this workspace (cwd)")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("backfill",
                       help="journal an existing session's log through the "
                            "window pipeline (deterministic, resumable)")
    p.add_argument("session")
    p.add_argument("--harness", default="dsh")
    p.add_argument("--dry-run", action="store_true",
                   help="plan only: pending events, windows, LLM calls")
    p.add_argument("--max-windows", type=int, default=None,
                   help="stop after this many windows (resumes on re-run)")
    p.add_argument("--reprocess", action="store_true",
                   help="cascade-delete the session's existing series "
                        "(folds + rollups referencing it), then rebuild")

    p = sub.add_parser("window-review",
                       help="review summarizer I/O per window (prompt sent, "
                            "raw response, stored summary)")
    p.add_argument("session", nargs="?", default=None,
                   help="one session (default: recent calls across sessions)")
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("summaries", help="browse the summary trees")
    p.add_argument("--project", default=None)
    p.add_argument("--session", default=None)
    p.add_argument("--workspace", default=None)
    p.add_argument("--tree", action="store_true")
    p.add_argument("--limit", type=int, default=200)

    p = sub.add_parser("temporal", help="lazy day/week/month rollups (query-with-caching)")
    p.add_argument("--machine", default=None)
    p.add_argument("--workspace", default=None,
                   help="rollup scoped to one workspace (cwd)")
    p.add_argument("--unit", default="day", choices=["day", "week", "month"])
    p.add_argument("--start", default=None, help="YYYY-MM-DD (default today)")
    p.add_argument("--days", type=int, default=1,
                   help="how many periods back from --start")

    p = sub.add_parser("switchover",
                       help="apply the session-memory regime to this store "
                            "(consolidation off, verify off, LLM queue lapses)")
    p.add_argument("--keep-verify", action="store_true",
                   help="keep state polling on (default: off)")

    p = sub.add_parser("recent")
    p.add_argument("--project", default=None)
    p.add_argument("--machine", default=None)
    p.add_argument("--hours", type=int, default=72)

    p = sub.add_parser("report")
    p.add_argument("project", nargs="?")
    p.add_argument("--evidence", action="store_true")
    p.add_argument("--hours", type=int, default=72)
    p.add_argument("--max-chars", type=int, default=0)

    p = sub.add_parser("correct")
    p.add_argument("fact_id")
    p.add_argument("--claim", required=True)
    p.add_argument("--reason", default=None)

    p = sub.add_parser("forget")
    p.add_argument("fact_id")
    p.add_argument("--reason", default=None)

    p = sub.add_parser("events")
    p.add_argument("--source", default=None)
    p.add_argument("--since", default=None)
    p.add_argument("--limit", type=int, default=50)

    p = sub.add_parser("review")
    rv = p.add_subparsers(dest="review_cmd")
    rv.add_parser("list")
    q = rv.add_parser("accept"); q.add_argument("id")
    q = rv.add_parser("reject"); q.add_argument("id"); q.add_argument("--note", default=None)

    args = ap.parse_args(argv)
    if getattr(args, "review_cmd", None) is None:
        args.review_cmd = "list"
    if getattr(args, "capture_cmd", None) is None:
        args.capture_cmd = "status"
    if getattr(args, "llm_cmd", None) is None:
        args.llm_cmd = "status"
    if getattr(args, "episode_cmd", None) is None:
        args.episode_cmd = "close"
    handlers = {
        "init": cmd_init, "doctor": cmd_doctor, "serve": cmd_serve,
        "run": cmd_run, "verify": cmd_verify, "llm": cmd_llm,
        "setup": cmd_setup, "compact": cmd_compact, "checkpoint": cmd_checkpoint,
        "export": cmd_export, "import": cmd_import,
        "episodes": cmd_episodes, "episode": cmd_episodes, "global": cmd_global,
        "timeline": cmd_timeline, "digest": cmd_digest,
        "shell-event": cmd_shell_event, "agent-event": cmd_agent_event,
        "add-project": cmd_add_project, "remember": cmd_remember,
        "consolidate": cmd_consolidate, "capture": cmd_capture,
        "install": cmd_install, "install-git-hooks": cmd_install_git_hooks,
        "shell-hook": cmd_shell_hook, "git-hook": cmd_git_hook,
"search": cmd_search, "current": cmd_current, "history": cmd_history,
    "related": cmd_related, "decisions": cmd_decisions, "evidence": cmd_evidence,
    "context": cmd_context, "recent": cmd_recent, "report": cmd_report,
    "correct": cmd_correct, "forget": cmd_forget,
    "events": cmd_events, "review": cmd_review,
    "session-event": cmd_session_event, "window-flush": cmd_window_flush,
    "inject": cmd_inject, "state": cmd_state,
    "session-watch": cmd_session_watch, "span": cmd_span,
    "sessions": cmd_sessions, "backfill": cmd_backfill,
    "window-review": cmd_window_review,
    "summaries": cmd_summaries, "temporal": cmd_temporal,
    "switchover": cmd_switchover,
    }
    return handlers[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())