"""Session-memory engine (SESSION-MEMORY.md).

Agent sessions are journaled through harness command hooks (cheap,
<100 ms: one raw_events row + a state bump) and rolled up into
hierarchical summary trees:

  level 1   one session window (N agent tool steps, default 6), addressed by
            a span into the harness's raw session log
  level 2+  summary-of-summaries via references (chain width 3-4,
            disjoint siblings, grouped by workspace — all sessions of one
            DSH workspace form one series — so trees of different
            workspaces never merge)
  temporal  lazy day->week->month rollups, materialized on query

Invariants: a summary node is *navigation, not truth* — every leaf has a
span (where the raw data lives), every higher node has references (which
nodes it folds). LLM failure degrades a window to a rule stub
(status="partial"); raw data never depends on the LLM.

Execution: "detached" (default) — each finished window spawns a
short-lived `memor window-flush` worker; "resident" — `memor session-watch`
picks windows up from a poll loop. Both share `flush_window`.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from .config import Config
from .models import now_utc, parse_ts
from .scope import machine_name, resolve_project
from .sessionlog import (UnknownHarnessError, dsh_session_file, events_in_range,
                        session_info, span_format, span_parse)
from .store import Store

STATE_KEY = "sessionmem.sessions"
FLUSH_LOCK_TTL = 900  # seconds; a crashed worker's lock expires after this

# LLM system prompt for ALL summary levels (window, chain fold, temporal).
SUMMARIZE_SYSTEM = """You are the session summarizer for "memor", a durable
project/environment memory. You receive a token-budgeted digest of agent
session activity (user prompts verbatim, budgeted assistant text, tool
calls and results) plus, for continuation or higher-level folds, the text
of the summaries you are building on.

Produce a summary that is NAVIGATION, NOT TRUTH: what was worked on, what
changed, decisions made (with reasons), problems hit, and what is still
open. Every statement must be grounded in the provided material; mark
uncertainty when the digest is thin; never fabricate values, commands, or
outcomes.

The "summary" field is a REQUIRED plain-English description — 150-400
words of flowing prose (short bullet lines are fine). It must NOT
duplicate the structured fields below and must NOT contain markdown
section headers, record lists, or "decisions/problems/open" bullet
material — that content belongs in its own fields.

Respond with ONLY the JSON object — no prose, no markdown code fences,
no text before or after it:
{"summary": "plain-English description of what the summary covers",
 "subjects": ["canonical subject strings, e.g. memor.session-memory, port:3080"],
 "project": "project name or null",
 "component": "component or null",
 "decisions": [{"subject": "...", "choice": "...", "reason": "..."}],
 "problems": ["one-line problems or errors"],
 "open": ["one-line open items / next steps"]}

When the payload carries `workspace_state` — the items of this
workspace ALREADY ON RECORD, as "(category) subject" lines — it is the
ongoing registry. One subject, one spelling: if a decision, problem,
or open item in this window is the SAME ongoing work as an item on
record, REUSE that item's subject verbatim; do not re-describe work
that is already on record. Only add subjects for genuinely new work."""


# ----------------------------------------------------------------- state

def _sessions(store: Store) -> dict:
    return store.snap_get(STATE_KEY) or {}


def _save_sessions(store: Store, sessions: dict) -> None:
    # prune entries untouched for 30 days (their windows are closed)
    cutoff = (datetime.utcnow() - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    stale = [s for s, st in sessions.items()
             if st.get("last_event_at", "") < cutoff
             and st.get("flushing") is None
             and st.get("prompts", 0) == 0 and not st.get("stopped")]
    for s in stale:
        del sessions[s]
    store.snap_set(STATE_KEY, sessions)


def _worker_cmd() -> str:
    """The memor executable: prefer this venv's console script so the hook
    path is stable regardless of how the hook itself was invoked."""
    p = Path(__file__).resolve().parent.parent / ".venv" / "bin" / "memor"
    return str(p) if p.exists() else (shutil.which("memor") or "memor")


def _worker_log_path(cfg: Config) -> Path:
    """Per-day stderr log for detached flush workers. The worker's output
    used to go to /dev/null, which kept dead workers invisible."""
    p = cfg.data_dir / "logs" / \
        f"window-flush-{now_utc()[:10].replace('-', '')}.log"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _spawn_worker(harness: str, session_id: str,
                 stderr_path: Path | None = None) -> None:
    """Detach a short-lived flush worker (detached execution mode). The
    worker is the flush-lock holder by contract — session_event reserved
    the lock right before spawning — so the entry runs with --force:
    a non-force entry would bail on its own fresh lock ('already-
    flushing') and exit without releasing it. Stderr goes to a per-day
    log (observability), stdout to devnull (the report only matters when
    the CLI is run by hand)."""
    with open(os.devnull, "w") as devnull:
        err = open(stderr_path, "a") if stderr_path else devnull
        try:
            subprocess.Popen(
                [_worker_cmd(), "window-flush", "--harness", harness,
                 "--session", session_id, "--force"],
                stdout=devnull, stderr=err, stdin=devnull,
                start_new_session=True,
            )
        finally:
            if err is not devnull:
                err.close()


# ------------------------------------------------- session meta & window width

def _session_meta(harness: str, session_id: str, dsh_root: str) -> dict | None:
    """The session record (first line of the log): origin, delegationDepth,
    parentSession. None when the log is not (yet) on disk or unreadable."""
    try:
        p = dsh_session_file(harness, session_id, dsh_root)
    except UnknownHarnessError:
        return None
    if not p:
        return None
    try:
        rec = session_info(p)
    except Exception:
        return None
    if not rec:
        return None
    return {"origin": rec.get("origin"),
            "depth": rec.get("delegationDepth") or 0,
            "parent_session": rec.get("parentSession")}


def window_steps_for(cfg: Config, st: dict) -> int | None:
    """Window width by delegation depth; None when that depth is
    WINDOW-EXEMPT — no windows, no summarisation: the parent that called
    the subagent covers it as part of its own tool calls.
    Depth 0 (main session, or unknown) always uses
    `session_window_steps`. Depth d>=1 uses
    `session_subagent_window_steps[d-1]`, the last entry capping deeper
    levels; an empty list means the uniform default for all depths.
    A -1 entry at index i disables windowing for depth i+1 and every
    deeper depth (ordered: a level only logs when every level above it
    logs); entries after the first -1 are ignored."""
    d = st.get("depth") or 0
    if d <= 0:
        return cfg.session_window_steps
    widths = list(cfg.session_subagent_window_steps or [])
    if not widths:
        return cfg.session_window_steps
    idx = min(d - 1, len(widths) - 1)
    cutoff = next((i for i, w in enumerate(widths) if w == -1), len(widths))
    if idx >= cutoff:
        return None
    return max(1, widths[idx])


# ---------------------------------------------------------------- journal

def session_event(cfg: Config, store: Store, harness: str, event: str,
                  payload: dict) -> dict:
    """One hook hit: journal the event (redaction chokepoint applies),
    bump window state, and — detached mode — spawn the flush worker when
    the window closes. Must stay well under the 100 ms hook SLA: this is
    one SQLite insert + one snapshot update, no I/O beyond that."""
    sid = payload.get("session_id")
    if not sid:
        return {"ok": False, "reason": "no session_id"}
    if not cfg.session_journal_enabled or harness not in cfg.session_harnesses:
        return {"ok": False, "reason": "not-wired"}
    ts = now_utc()
    cwd = payload.get("cwd") or None

    prompt = payload.get("prompt") if event == "prompt" else None
    tool_name = payload.get("tool_name") if event == "tool" else None
    args = payload.get("tool_input") if event == "tool" else None
    response = payload.get("tool_response") if event == "tool" else None
    args_s = (json.dumps(args, ensure_ascii=False, default=str)
              if isinstance(args, (dict, list)) else str(args or ""))
    out_s = _response_text(response)

    if event == "prompt":
        command = f"session prompt: {str(prompt)[:200]}"
        detail = {"prompt": str(prompt)[:1200]}
    elif event == "tool":
        command = f"session tool: {tool_name} {args_s[:150]}"
        detail = {"tool": tool_name, "args": args_s[:400],
                  "output": out_s[:1500]}
    else:  # start | stop
        command = f"session {event}"
        detail = {}
    if isinstance(detail, dict):
        detail["harness"] = harness
        detail["event"] = event

    sessions = _sessions(store)
    st = sessions.get(sid)
    if st is None:
        st = {"harness": harness, "machine": machine_name(),
              "project": resolve_project(cwd, cfg) if cwd else None,
              "cwd": cwd, "started_at": ts,
              "workspace": cwd,
              "prompts": 0, "steps": 0, "events_since_last": 0,
              "last_span_hi": None, "last_summary_id": None,
              "depth": None, "origin": None, "parent_session": None,
              "retries": 0, "stopped": False, "flushing": None}
    # Window-exemption by delegation depth: a session known to sit at a
    # disabled depth keeps no state and journals nothing — the parent
    # that called it summarises it as part of its own tool calls. (Rows
    # journaled before the exemption stay in the store: they may already
    # be folded into summaries whose spans must keep resolving.)
    if st.get("depth") and window_steps_for(cfg, st) is None:
        if sid in sessions:
            sessions.pop(sid, None)
            _save_sessions(store, sessions)
        return {"ok": True, "session": sid, "depth": st.get("depth"),
                "reason": "depth-exempt"}
    st["last_event_at"] = ts
    st["events_since_last"] = st.get("events_since_last", 0) + 1
    if event == "prompt":
        st["prompts"] = st.get("prompts", 0) + 1
    elif event == "tool":
        # steps drive windowing: one agent tool call = one step
        st["steps"] = st.get("steps", 0) + 1
    if event == "stop":
        st["stopped"] = True
    if cwd:
        st["workspace"] = cwd
    # delegation depth decides the window width AND the exemption: read
    # the session record at `start` (log on disk by then, or within ms)
    # and retry on the first tool event if not. Cheap: one first-line
    # read, at most twice.
    if event in ("start", "tool") and st.get("depth") is None:
        meta = _session_meta(harness, sid, cfg.session_dsh_root)
        if meta:
            st["depth"] = meta["depth"]
            st["origin"] = meta["origin"]
            st["parent_session"] = meta["parent_session"]
    if st.get("depth") and window_steps_for(cfg, st) is None:
        # depth just resolved as exempt: unwind whatever was journaled
        # before the depth was known (at most the start row), drop the
        # state, stop — a gated session must not log anything.
        deleted = store.delete_events_for_session(sid)
        sessions.pop(sid, None)
        _save_sessions(store, sessions)
        return {"ok": True, "session": sid, "depth": st.get("depth"),
                "reason": "depth-exempt", "rolled_back_events": deleted}
    project = st.get("project") if st else (
        resolve_project(cwd, cfg) if cwd else None)
    store.append_event(
        "session", event,
        machine=machine_name(),
        project=project,
        cwd=cwd,
        session=sid,
        command=command,
        payload=detail,
    )
    sessions[sid] = st

    n = window_steps_for(cfg, st)
    ready = (n is not None
             and (st.get("steps", 0) >= n
                  or (st["stopped"] and st["events_since_last"] >= 1)))
    spawn = False
    # A crashed worker's lock expires via TTL (same rule as flush_window's
    # own guard): without this, one stale lock would block every future
    # spawn forever.
    holding = (st.get("flushing") is not None
               and _lock_age(st["flushing"]) < FLUSH_LOCK_TTL)
    if ready and not holding:
        if cfg.session_execution_mode == "detached":
            st["flushing"] = ts
            _save_sessions(store, sessions)
            try:
                _spawn_worker(harness, sid, _worker_log_path(cfg))
                spawn = True
            except Exception as e:
                # spawn failed: release the lock so the next event retries
                print(f"memor: worker spawn failed: {e}", file=sys.stderr)
                st["flushing"] = None
                _save_sessions(store, sessions)
        else:  # resident: the watch loop owns flushing
            _save_sessions(store, sessions)
    else:
        _save_sessions(store, sessions)
    return {"ok": True, "session": sid, "window_ready": ready,
            "spawned": spawn}


def _response_text(response) -> str:
    """Claude-Code-shaped tool_response -> flat text (dicts carry
    stdout/stderr; the DSH bridge mirrors that shape)."""
    if response is None:
        return ""
    if isinstance(response, str):
        return response
    if isinstance(response, dict):
        parts = [str(response.get(k, ""))
                 for k in ("stdout", "stderr", "text", "output")]
        return "\n".join(p for p in parts if p)[:4000]
    return json.dumps(response, ensure_ascii=False, default=str)[:4000]


# ---------------------------------------------------------------- digest

def _text_blocks(content) -> str:
    """Concatenated *text* blocks only — reasoning blocks are excluded by
    construction (the digest never carries thinking traces; those live in
    the span drill-down)."""
    out = []
    for b in content or []:
        if isinstance(b, dict) and b.get("type") == "text":
            out.append(str(b.get("text", "")))
    return "\n".join(t for t in out if t)


def _snip(text: str, budget: int) -> str:
    """Head+tail snip: keep the beginning, elide the middle, keep the end
    (tool output: the interesting bits are the first line and the error)."""
    t = str(text or "")
    if len(t) <= budget:
        return t
    half = budget // 2
    return t[:half] + f" …[{len(t) - budget} chars elided]… " + t[-half:]


def build_digest(cfg: Config, events: list[dict]) -> tuple[str, int, int]:
    """Token-budgeted digest of one window. Never includes reasoning
    traces — only budgeted assistant *text*; full thinking is reachable
    via the span drill-down. Returns (digest, n_prompts, n_tool_calls)."""
    lines: list[str] = []
    n_prompts = n_tools = 0
    for ev in events:
        t = ev.get("type")
        d = ev.get("data") or {}
        seq = ev.get("seq", "?")
        if t == "user/message":
            if (d.get("source") or {}).get("kind") != "user":
                continue  # plugin splices (policy changes etc.) are not prompts
            text = _text_blocks(d.get("content"))
            if not text:
                continue
            n_prompts += 1
            lines.append(f"[{seq}] USER: {text[:cfg.session_prompt_chars]}")
        elif t == "assistant/message":
            text = _text_blocks((d.get("message") or {}).get("content"))
            if text:
                lines.append(f"[{seq}] ASSISTANT: {_snip(text, cfg.session_assistant_chars)}")
        elif t == "tool/call":
            n_tools += 1
            args = d.get("arguments")
            args_s = args if isinstance(args, str) else json.dumps(
                args, ensure_ascii=False, default=str)
            lines.append(f"[{seq}] TOOL {d.get('name', '?')}({_snip(args_s, cfg.session_tool_arg_chars)})")
        elif t == "tool/result":
            blocks = ((d.get("message") or {}).get("content")) or []
            err = any(b.get("isError") for b in blocks if isinstance(b, dict))
            text = ""
            for b in blocks:
                if isinstance(b, dict) and b.get("type") == "tool-result":
                    text = _text_blocks(b.get("content")) or text
            lines.append(f"[{seq}] TOOL-RESULT{' [ERROR]' if err else ''}: "
                         f"{_snip(text, cfg.session_tool_result_chars)}")
        elif t in ("compaction/start", "compaction/summary", "compaction/end"):
            lines.append(f"[{seq}] COMPACT marker (DSH-authored summary "
                         f"available via span; not inlined)")
        elif t in ("turn/start", "turn/end"):
            lines.append(f"[{seq}] turn {d.get('turn', '?')} {t.split('/')[1]}")
        elif t == "goal/change":
            lines.append(f"[{seq}] GOAL {_snip(json.dumps(d, ensure_ascii=False), 200)}")
        elif t == "command/run":
            lines.append(f"[{seq}] COMMAND {d.get('name', '')} {_snip(str(d.get('args', '')), 120)}")
        # step/start|end, agent/inbox/spliced, permission/*, request/*: noise, skip
    # overall digest cap: head + tail with a gap marker (the window may be
    # long between a failed retry and the next flush). Its own knob — not
    # derived from the assistant budget (a long assistant message is
    # content, not a reason to elide the rest of the window).
    joined = "\n".join(lines)
    cap = cfg.session_digest_max_chars
    if len(joined) > cap:
        joined = (joined[:cap // 2]
                  + f"\n…[{(len(joined) - cap) // 4} tokens elided in the middle of the window]…\n"
                  + joined[-cap // 4:])
    return joined, n_prompts, n_tools


# ----------------------------------------------------------------- flush

def flush_window(cfg: Config, store: Store, harness: str, session_id: str,
                 force: bool = False, max_windows: int | None = None,
                 budget: float | None = None) -> dict:
    """Flush worker: drain pending windows from the stored pointer to the
    log tail, in order (catch-up) — one window per LLM call, folds as due.
    No gap = exactly one window, i.e. the historic single-window behavior.
    `force` marks the caller as the flush-lock holder (the spawned worker
    or the backfill); a non-force call bails on a fresh lock and releases
    nothing. `budget` caps this run's wall time (default: config;
    `inf` = uncapped backfill).

    Returns a small report; never raises for LLM/log problems (the raw
    journal and harness log are independent of all of this)."""
    sessions = _sessions(store)
    st = sessions.get(session_id)
    if st is None:
        return {"ok": False, "reason": "no-session-state"}
    if st.get("depth") and window_steps_for(cfg, st) is None:
        # window-exempt by depth (config changed after spawn, or a stale
        # state row): this session produces no windows — clean up its
        # state so it stops being picked up by flush --all / the D tail.
        sessions.pop(session_id, None)
        _save_sessions(store, sessions)
        return {"ok": True, "session": session_id, "depth": st.get("depth"),
                "reason": "depth-exempt"}
    if st.get("flushing") and not force:
        if _lock_age(st["flushing"]) < FLUSH_LOCK_TTL:
            return {"ok": False, "reason": "already-flushing"}

    # (re)take the lock
    st["flushing"] = now_utc()
    _save_sessions(store, sessions)
    report: dict = {"ok": True, "session": session_id}
    eff_budget = (cfg.session_catchup_budget_seconds
                  if budget is None else budget)
    try:
        report.update(_flush_body(cfg, store, harness, session_id, st,
                                  sessions, max_windows=max_windows,
                                  budget=eff_budget))
    finally:
        # release on every path (a crashed worker's lock expires via TTL);
        # same `sessions` dict the body mutated, so its updates survive
        cur = sessions.get(session_id)
        if cur is not None:
            cur["flushing"] = None
            _save_sessions(store, sessions)
        if report.get("successor_due") \
                and cfg.session_execution_mode == "detached":
            # Pending work past this run's snapshot (budget stop, or the
            # tail grew while it ran): hand off to a successor worker.
            # Re-read the lock fresh — a hook event that arrived in the
            # release window may have taken it already (its run continues
            # the gap; skip the duplicate).
            fresh_map = _sessions(store)
            fst = fresh_map.get(session_id)
            if fst is not None and (
                    fst.get("flushing") is None
                    or _lock_age(fst["flushing"]) >= FLUSH_LOCK_TTL):
                try:
                    fst["flushing"] = now_utc()
                    _save_sessions(store, fresh_map)
                    _spawn_worker(harness, session_id, _worker_log_path(cfg))
                except Exception as e:  # noqa: BLE001 — spawn is best-effort
                    print(f"memor: successor spawn failed: {e}",
                          file=sys.stderr)
                    fst["flushing"] = None
                    _save_sessions(store, fresh_map)
    return report


def _lock_age(ts: str | None) -> float:
    if not ts:
        return 1e9
    try:
        # both sides timezone-aware: parse_ts returns aware, so use the
        # aware now (a naive utcnow() minus aware raised TypeError ->
        # every lock looked stale and the TTL guard never fired)
        return (datetime.now(timezone.utc) - parse_ts(ts)).total_seconds()
    except Exception:
        return 1e9


# MCP write tools whose results carry the ids of the records they created.
# A window's raw log is scanned for these calls so the summary can point at
# what the session itself recorded — records attach as refs (signposts),
# never copies: the fact/decision row stays the truth, the summary navigates.
_WRITE_TOOLS = ("mcp__memor__memory_add_fact",
                "mcp__memor__memory_add_decision",
                "mcp__memor__memory_add_note")
_RECORD_PREFIXES = ("fact_", "dec_")
_REF_CAP = 40       # record refs stored per summary node


def _extract_inserted(text: str) -> list[str]:
    """Record ids from an MCP write result ({'inserted': [...]}); tolerant
    of non-JSON or truncated output (regex fallback over the raw text)."""
    try:
        obj = json.loads(text)
        ins = obj.get("inserted") if isinstance(obj, dict) else None
        if isinstance(ins, list):
            return [x for x in ins
                    if isinstance(x, str) and x.startswith(_RECORD_PREFIXES)]
    except (json.JSONDecodeError, TypeError, AttributeError):
        pass
    return re.findall(r'"((?:fact|dec)_[0-9a-f]{6,})"', text)


def _record_refs_from_span(evs: list[dict]) -> list[str]:
    """Exact attachment: pair the session's memor write calls with their
    results inside the raw window and read the inserted ids back."""
    write_calls = {(e.get("data") or {}).get("callId")
                   for e in evs
                   if e.get("type") == "tool/call"
                   and (e.get("data") or {}).get("name") in _WRITE_TOOLS}
    refs: list[str] = []
    for e in evs:
        if e.get("type") != "tool/result":
            continue
        for block in (((e.get("data") or {}).get("message") or {})
                     .get("content") or []):
            if not isinstance(block, dict) or block.get("type") != "tool-result":
                continue
            if block.get("toolCallId") not in write_calls:
                continue
            text = "".join(c.get("text", "")
                           for c in (block.get("content") or [])
                           if isinstance(c, dict))
            for rid in _extract_inserted(text):
                if rid not in refs:
                    refs.append(rid)
    return refs


def _event_time_range(evs: list[dict]) -> tuple[str, str] | None:
    """ISO-Z [first, last] window from DSH event times (epoch ms)."""
    times = [e.get("time") for e in evs
             if isinstance(e.get("time"), (int, float))]
    if not times:
        return None
    fmt = lambda ms: datetime.fromtimestamp(ms / 1000, tz=timezone.utc) \
        .strftime("%Y-%m-%dT%H:%M:%SZ")
    return fmt(min(times)), fmt(max(times))


def _flush_body(cfg: Config, store: Store, harness: str, session_id: str,
                st: dict, sessions: dict,
                max_windows: int | None = None,
                budget: float | None = None) -> dict:
    """Drain pending windows from the stored pointer to the log tail.

    One window per LLM call, tiled with the same boundary rule as the
    backfill (_next_window_boundary: the Nth tool call, or the last event
    when fewer remain) — so a session with no gap runs exactly one window
    (the historic behavior) and a session with a backlog (dead hooks,
    idle stretches) catches up window by window instead of collapsing
    the whole gap into one elided digest. Folds (L2/L3/...) run after
    every window, so upper levels keep pace with the catch-up. `budget`
    caps this run's wall time. A run that ends with pending work past
    its snapshot tail — a budget stop, or the log grew while it ran
    (drained up-to-date) — asks for a successor (flush_window spawns
    it): the relay chases the tail and ends only when the session goes
    quiet. llm-unavailable and explicit max-windows never chain."""
    def done(report: dict) -> dict:
        _save_sessions(store, sessions)  # st is stored by reference
        return report

    path = dsh_session_file(harness, session_id, cfg.session_dsh_root)
    if path is None:
        return done({"ok": False, "reason": "no-session-log"})
    # whole log: the windowing below is what bounds each digest; reading
    # from 1 would drop a seq-0 event that could be content
    all_evs, first_seq, last_seq = events_in_range(path, 0, 10**9)
    if not all_evs:
        # log hasn't grown since the last window; keep the state, wait for
        # the next event to re-trigger (detached) or the poll (resident)
        return done({"ok": False, "reason": "no-progress"})

    t0 = time.monotonic()
    processed: list[dict] = []
    reason = "up-to-date"
    while True:
        pointer = st.get("last_span_hi")
        lo = (pointer + 1) if pointer is not None else first_seq
        if lo > last_seq:
            break  # pointer at (or past) the tail
        if max_windows is not None and len(processed) >= max_windows:
            reason = "max-windows"
            break
        if budget is not None and time.monotonic() - t0 > budget:
            reason = "budget"
            break
        pending = [e for e in all_evs if lo <= e.get("seq", 0) <= last_seq]
        if not pending:
            reason = "no-progress"
            break
        boundary = _next_window_boundary(cfg, pending, st)
        window_evs = [e for e in pending if e.get("seq", 0) <= boundary]
        r = _flush_one_window(cfg, store, harness, session_id, st,
                              sessions, window_evs, lo, boundary)
        processed.append(r)
        if not r.get("ok") and r.get("reason") == "llm-unavailable":
            # retryable LLM failure: the gap stays pending (no stub-spam,
            # no successor — a dead LLM shouldn't chain); the next
            # trigger resumes from the durable pointer
            reason = "llm-unavailable"
            break
        # ok (including noise-skipped): pointer advanced, keep going

    if not processed:
        return done({"ok": False, "reason": reason
                    if reason != "up-to-date" else "no-progress"})
    report: dict = {k: v for k, v in processed[-1].items()
                   if k in ("ok", "summary_id", "level", "span",
                            "prompts", "tools", "folded", "status",
                            "reason", "retry")}
    report["processed"] = len(processed)
    report["reports"] = [{k: r.get(k) for k in
                          ("ok", "reason", "summary_id", "span", "status")}
                         for r in processed]
    if reason != "up-to-date":
        report["reason"] = reason

    # Pending work past this run's snapshot? The log may have grown while
    # we worked (a live session), so re-read the tail fresh — a stale read
    # would under-count and strand the new tail until the next hook
    # trigger. A run that ends with pending (budget stop, or drained
    # up-to-date with a grown tail) asks for a successor, which re-reads
    # the fresh tail at its own start: further growth merges into it, so
    # at most one successor is ever in flight. llm-unavailable,
    # max-windows (an explicit operator cap) and no-progress never chain.
    try:
        fresh_evs, _, live_tail = events_in_range(path, 0, 10**9)
    except Exception:  # noqa: BLE001 — a lost log at exit must not kill
        # an already-persisted run's report; treat as nothing new
        fresh_evs, live_tail = [], last_seq
    pointer_now = st.get("last_span_hi")
    lo2 = (pointer_now + 1) if pointer_now is not None else first_seq
    pending_events = sum(
        1 for e in fresh_evs if lo2 <= e.get("seq", 0) <= live_tail)
    if reason in ("up-to-date", "budget") and pending_events:
        report["pending_events"] = pending_events
        report["successor_due"] = True
    return done(report)


def _flush_one_window(cfg: Config, store: Store, harness: str,
                      session_id: str, st: dict, sessions: dict,
                      evs: list[dict], lo: int, hi: int) -> dict:
    """One window [lo..hi]: digest, LLM-summarize (or stub), store an L1
    with its span, advance the pointer, fold chains as due."""
    def done(report: dict) -> dict:
        _save_sessions(store, sessions)  # st is stored by reference
        return report

    # Idempotency: a current (non-partial) L1 summary that already covers
    # this whole window means a duplicate trigger (crash-era retry with a
    # stale pointer, overlapping workers) — adopt the stored row and
    # advance the pointer instead of paying a second LLM call.
    for existing in store.stored_window(session_id, lo):
        m = re.search(r"seq\[\d+\.\.(\d+)\]$", existing.get("span") or "")
        if not m or existing.get("status") == "partial":
            continue
        if int(m.group(1)) >= hi:
            st["last_span_hi"] = int(m.group(1))
            st["retries"] = 0
            return done({"ok": True, "reason": "already-flushed",
                         "summary_id": existing["id"],
                         "span": existing["span"],
                         "status": existing.get("status")})

    digest, n_prompts, n_tools = build_digest(cfg, evs)
    if n_prompts == 0 and n_tools < 3:
        # noise-only tail (probe calls, empty turns): advance the pointer,
        # don't litter the tree
        st["last_span_hi"] = hi
        st["prompts"] = 0
        st["stopped"] = False
        st["events_since_last"] = 0
        return done({"ok": True, "reason": "noise-skipped", "span": None,
                     "prompts": n_prompts, "tools": n_tools})

    # attach the records this window produced (exact span scan first;
    # timestamp-window query when the results carry no parsable ids)
    recs = _record_refs_from_span(evs)
    if not recs:
        rng = _event_time_range(evs)
        if rng:
            recs = [r["id"] for r in store.records_for_range(*rng,
                                                             limit=_REF_CAP)]
    recs = recs[:_REF_CAP]

    llm = _extractor(cfg)
    text = subjects = None
    structured = {"decisions": [], "problems": [], "open_items": []}
    project = st.get("project")
    if llm is not None:
        try:
            data = _summarize_recorded(cfg, store, llm, mode="window",
                                       session=session_id,
                                       span=span_format(harness, session_id, lo, hi),
                                       digest=digest,
                                       prev_text=_prev_summary_text(store, st),
                                       project=project,
                                       workspace_state=_active_state_lines(
                                           store, st.get("workspace")))
            if data and data.get("summary"):
                # text is the plain-English summary only; the structured
                # fields (decisions/problems/open) are stored in their own
                # columns
                text = str(data["summary"]).strip()
                structured = _structured_fields(data)
                subjects = [str(s)[:120] for s in data.get("subjects", [])[:12]]
                project = data.get("project") or project
        except Exception as e:  # noqa: BLE001 — LLM path must never kill the worker
            print(f"memor: window summarize failed: {e}", file=sys.stderr)
    if text is None:
        # LLM down (or output unparseable): the window stays pending and
        # merges into the next; after N failed attempts it degrades to a
        # rule stub. Raw data is never at risk either way.
        st["retries"] = st.get("retries", 0) + 1
        if st["retries"] < cfg.session_max_flush_retries:
            return done({"ok": False, "reason": "llm-unavailable",
                         "retry": st["retries"]})
        text = (f"[stub] Session {session_id} window seq[{lo}..{hi}]: "
                f"{n_prompts} user prompts, {n_tools} tool calls "
                f"(LLM unavailable after {st['retries']} retries). "
                f"Raw detail: memor span {span_format(harness, session_id, lo, hi)}")
        subjects = []
        status = "partial"
    else:
        st["retries"] = 0
        status = "current"

    sid = store.insert_summary(
        ts=st["last_event_at"], machine=st.get("machine"),
        session=session_id, harness=harness, axis="project",
        project=project, workspace=st.get("workspace"),
        level=1, span=span_format(harness, session_id, lo, hi),
        references=recs, text=text, subjects=subjects,
        decisions=structured["decisions"], problems=structured["problems"],
        open_items=structured["open_items"],
        model=getattr(cfg, "llm_model", None),
        status=status,
    )
    store.append_event("memor", "summary_written",
                       machine=st.get("machine"), project=project,
                       session=session_id,
                       command="session summary",
                       payload={"summary_id": sid, "level": 1,
                                "span": span_format(harness, session_id, lo, hi)})
    # Autonomous promotion: this window's structured extraction becomes
    # workspace state (deduped on the key, idempotent per span). Self-
    # maintaining by default. The AGENTS.local.md file projection is
    # config-gated (off by default — that file is DSH's instruction
    # channel; the state rides the SessionStart envelope + on-demand
    # fetch instead). Neither may kill the worker.
    try:
        from .wstate import promote_from_summary, project_state_file
        ws = st.get("workspace")
        if ws:
            # P1 backstop: the dedup embedder is created lazily (only if a
            # candidate needs the comparison) so promotion-heavy windows
            # pay no model cost when everything matches exact keys.
            get_emb = None
            if cfg.state_promote_dedup:
                from .embed import get_embedder
                def _promote_embedder():
                    return get_embedder(cfg)
                get_emb = _promote_embedder
            promoted = promote_from_summary(
                store, {"workspace": ws, "session": session_id,
                        "span": span_format(harness, session_id, lo, hi),
                        "decisions": structured["decisions"],
                        "problems": structured["problems"],
                        "open_items": structured["open_items"]},
                get_emb=get_emb,
                threshold=cfg.state_promote_merge_threshold,
                threshold_ref=cfg.state_promote_merge_threshold_ref)
            if promoted and cfg.session_state_project_file:
                project_state_file(ws, store)
    except Exception as e:  # noqa: BLE001
        print(f"memor: workspace-state promotion failed: {e}",
              file=sys.stderr)
    st["last_span_hi"] = hi
    st["last_summary_id"] = sid
    st["prompts"] = 0
    st["steps"] = 0
    st["stopped"] = False
    st["events_since_last"] = 0
    st["retries"] = 0

    folded = fold_chains(cfg, store, project=project,
                         workspace=st.get("workspace"))
    return done({"ok": True, "summary_id": sid, "level": 1,
                 "span": span_format(harness, session_id, lo, hi),
                 "prompts": n_prompts, "tools": n_tools,
                 "folded": folded, "status": status})


def _prev_summary_text(store: Store, st: dict) -> str | None:
    sid = st.get("last_summary_id")
    if not sid:
        return None
    s = store.get_summary(sid)
    return s["text"][:1500] if s else None


def _structured_fields(data: dict) -> dict:
    """Sanitize the summarizer's structured fields for storage: decisions
    as {subject, choice, reason} objects, problems/open as one-liners.
    Caps: 8 items per field, 200 chars per item."""
    def line(x) -> str:
        return " ".join(str(x or "").split())[:200]
    decisions = []
    for r in (data.get("decisions") or [])[:8]:
        if isinstance(r, str):
            r = {"subject": r}
        elif not isinstance(r, dict):
            continue
        d = {"subject": line(r.get("subject")),
             "choice": line(r.get("choice")),
             "reason": line(r.get("reason"))}
        if any(d.values()):
            decisions.append(d)
    return {
        "decisions": decisions,
        "problems": [p for p in (line(x)
                                 for x in (data.get("problems") or [])[:8])
                     if p],
        "open_items": [o for o in (line(x)
                                   for x in (data.get("open") or [])[:8])
                       if o],
    }


# ---------------------------------------------------------------- chaining

def fold_chains(cfg: Config, store: Store,
                project: str | None = None,
                workspace: str | None = None) -> list[str]:
    """Fold chain_width unparented level-k summaries into one level-(k+1)
    parent, level by level until no group is due. Returns the new parent
    ids. Scope: one `workspace` (all its sessions — workspaces never mix)
    or, without one, one `project` (legacy axis). Siblings partition
    (parent_id set on the consumed children)."""
    made: list[str] = []
    k = 1
    while k < 8:
        unparented = store.unparented_summaries(
            workspace=workspace,
            project=None if workspace is not None else project, level=k)
        if len(unparented) < cfg.session_chain_width:
            break
        children = unparented[:cfg.session_chain_width]
        texts = [f"({c['ts']} {c['span'] or ''})\n{c['text']}" for c in children]
        # records accumulate up the tree: the fold carries the union of its
        # children's record refs (they partition the same covered range)
        recs: list[str] = []
        for c in children:
            for rid in (c.get("references") or []):
                if rid.startswith(_RECORD_PREFIXES) and rid not in recs:
                    recs.append(rid)
        llm = _extractor(cfg)
        text = subjects = None
        structured = {"decisions": [], "problems": [], "open_items": []}
        if llm is not None:
            try:
                data = _summarize_recorded(cfg, store, llm, mode="chain",
                                           child_texts=texts,
                                           project=project, level=k + 1,
                                           workspace_state=_active_state_lines(
                                               store, workspace))
                if data and data.get("summary"):
                    # plain-English summary in `text`; structured fields
                    # in their own columns
                    text = str(data["summary"]).strip()
                    structured = _structured_fields(data)
                    subjects = [str(s)[:120]
                                 for s in data.get("subjects", [])[:12]]
            except Exception as e:
                print(f"memor: chain summarize failed: {e}", file=sys.stderr)
        if text is None:
            text = ("(folded, LLM unavailable) children "
                    + ", ".join(c["id"] for c in children)
                    + ":\n" + "\n".join(f"- {c['text'][:600]}" for c in children))
        pid = store.insert_summary(
            ts=children[-1]["ts"],
            machine=children[0].get("machine"),
            # workspace-scoped folds cover many sessions: no single one
            session=None if workspace is not None else children[0].get("session"),
            project=None if workspace is not None else project,
            workspace=workspace, level=k + 1,
            references=[c["id"] for c in children] + recs, text=text,
            subjects=subjects or [],
            decisions=structured["decisions"], problems=structured["problems"],
            open_items=structured["open_items"],
            model=getattr(cfg, "llm_model", None), status="current",
        )
        store.set_summary_parents([c["id"] for c in children], pid)
        made.append(pid)
        k += 1
    return made


def _extractor(cfg: Config):
    if not getattr(cfg, "llm_enabled", True):
        return None
    try:
        from .consolidate.extract import extractor_for
        _, llm = extractor_for(cfg)
        return llm
    except Exception:
        return None


def _summarize_payload(mode: str, ctx: dict) -> str:
    """Exact user payload for one summarizer call (mode + version +
    non-None ctx keys). Stable shape: `prompt_version` in the prompt is
    what a future prompt change bumps."""
    merged = {k: v for k, v in ctx.items() if v is not None}
    return json.dumps({"mode": mode, "prompt_version": "session-v4",
                       **merged}, ensure_ascii=False, default=str)


def _active_state_lines(store: Store, ws: str | None) -> str | None:
    """The workspace's active state as a compact registry for the
    summarizer prompt: the dedup fix (the model reuses subjects for
    ongoing items instead of re-describing them, which is what created
    the duplicate-promotion noise in the live build). None (dropped
    from the payload) when there is no workspace or no recorded state.

    Ordering matters (live finding: creation-order + a hard cap 60
    truncated mostly open_items — the items most likely to be
    re-described): decaying items first, most-recently-handled first,
    then decisions, then everything else."""
    if not ws:
        return None
    rows = store.workspace_state_current(ws, 0)
    if not rows:
        return None

    def rank(r):
        if r["category"] in ("open_item", "problem"):
            band = 0
        elif r["category"] == "decision":
            band = 1
        else:
            band = 2
        return (band, -r["rev"])
    rows.sort(key=rank)
    return "\n".join(f"({r['category']}) {r['subject']}" for r in rows[:60])


def _parse_summary_response(raw: str | None) -> dict | None:
    """Tolerant parse of a summarizer response (code fences / leading
    prose around the JSON). None when unusable."""
    if not raw:
        return None
    t = raw.strip()
    t = re.sub(r"^```(?:json)?\s*", "", t)
    t = re.sub(r"\s*```$", "", t)
    s, e = t.find("{"), t.rfind("}")
    if s == -1 or e <= s:
        return None
    try:
        return json.loads(t[s:e + 1])
    except json.JSONDecodeError:
        return None


def _summarize_recorded(cfg: Config, store: Store, llm, *, mode: str,
                        session: str | None = None,
                        span: str | None = None, **ctx) -> dict | None:
    """One LLM call under SUMMARIZE_SYSTEM, recorded in the `llm_calls`
    audit table (the exact exchange — prompt sent, raw response, model,
    ok — for `memor window-review`). `session`/`span` join the payload
    when set (as before) and anchor the audit row. Returns the parsed
    JSON or None."""
    user = _summarize_payload(mode, {**ctx, "session": session, "span": span})
    raw: str | None = None
    if llm is not None:
        try:
            raw = llm._call(user, system=SUMMARIZE_SYSTEM)
        except Exception as e:  # noqa: BLE001 — audit the failure, don't die
            raw = f"<error: {type(e).__name__}: {e}>"
    parsed = _parse_summary_response(raw)
    try:
        store.record_llm_call(
            ts=now_utc(), mode=mode, prompt=user,
            raw_response=(raw if raw is not None else ""
                          if llm is not None else None),
            ok=bool(parsed and parsed.get("summary")),
            session=session, span=span,
            machine=machine_name(), model=getattr(cfg, "llm_model", None))
    except Exception:  # noqa: BLE001 — the audit must never kill a flush
        pass
    return parsed


# ------------------------------------------------------------------ watch

def watch_loop(cfg: Config, store: Store, poll: float = 2.0,
               stop: dict | None = None) -> None:
    """Resident execution mode: pick up ready windows from the state
    snapshot. (The default detached mode needs no resident process.)"""
    stop = stop or {"stop": False}
    while not stop["stop"]:
        try:
            for sid, st in list(_sessions(store).items()):
                n = window_steps_for(cfg, st)
                if n is None:
                    # depth-exempt (stale state row / config change):
                    # flush_window's gate cleans the state up
                    flush_window(cfg, store, st.get("harness", "dsh"), sid)
                    continue
                if st.get("flushing") is not None:
                    continue
                if not (st.get("steps", 0) >= n
                        or (st["stopped"] and st["events_since_last"] >= 1)):
                    continue
                if st["prompts"] == 0 and st["events_since_last"] == 0:
                    continue
                r = flush_window(cfg, store, st.get("harness", "dsh"), sid)
                print(f"memor session-watch: {sid} -> {r.get('reason') or r.get('summary_id')}",
                      flush=True)
        except Exception as e:  # keep the loop alive
            print(f"memor session-watch: {e}", file=sys.stderr)
        for _ in range(int(poll * 5)):
            if stop["stop"]:
                break
            time.sleep(0.2)


# ----------------------------------------------------------------- backfill

def _next_window_boundary(cfg: Config, evs_after: list[dict], st: dict):
    """Deterministic window boundary: the seq of the Nth tool/call event
    (N = window_steps_for), extended to include that call's tool/result so
    call/result pairs are never split across windows — record-ref pairing
    (write call + its inserted-ids result) and digests need both in the
    same window. Returns the last seq when fewer than N calls remain."""
    n = window_steps_for(cfg, st)
    seen = last = None
    closing = closing_seq = None
    for ev in evs_after:
        seq = ev.get("seq")
        if not isinstance(seq, int):
            continue
        last = seq
        if closing is not None:
            # closing stretch: stop when the Nth call's result lands
            data = ev.get("data") or {}
            src = (data.get("message") or {}).get("source") or {}
            if ev.get("type") == "tool/result" and \
                    src.get("callId") == closing:
                return seq
            continue
        if ev.get("type") == "tool/call":
            seen = (seen or 0) + 1
            if seen >= n:
                closing = (ev.get("data") or {}).get("callId")
                closing_seq = seq
    # result never seen (truncated log / call-less backfill fixture):
    # boundary stays at the Nth call itself — old behavior
    return closing_seq if closing_seq is not None else last


def _cascade_delete_series(store: Store, session_id: str) -> list[str]:
    """Delete a session's summary series plus every node that references
    it: fold ancestors up the chain and cached temporal rollups (all
    derived caches — lazily rebuilt on next query). Returns deleted ids."""
    doomed = {s["id"] for s in store.list_summaries(session=session_id,
                                                    limit=5000)}
    if not doomed:
        return []
    frontier = list(doomed)
    while frontier:
        new = [r["id"] for r in store.summaries_referencing(frontier)
               if r["id"] not in doomed]
        if not new:
            break
        doomed.update(new)
        frontier = new
    for sid in sorted(doomed):
        store.delete_summary(sid)
    return sorted(doomed)


def backfill_session(cfg: Config, store: Store, session_id: str,
                     harness: str = "dsh", dry_run: bool = False,
                     max_windows: int | None = None,
                     reprocess: bool = False) -> dict:
    """Process an existing (or live) session through the normal window
    pipeline, window by window, straight from its log. The input is
    deterministic — the log is immutable and each boundary is a pure
    function of it (the Nth tool call) — so backfill reproduces exactly
    what live journaling would have produced. Resumable: the pointer
    lives in the store; re-running continues where it stopped.
    `reprocess` first cascades away the session's existing series (folds
    and rollups that reference it included) and rebuilds it under the
    current config/params."""
    path = dsh_session_file(harness, session_id, cfg.session_dsh_root)
    if path is None:
        return {"ok": False, "reason": "no-session-log"}
    all_evs, first_seq, last_seq = events_in_range(path, 0, 10**9)
    if not all_evs:
        return {"ok": False, "reason": "empty-log"}

    rec = session_info(path) or {}
    sessions = _sessions(store)
    st = sessions.get(session_id)
    # never race a live flush worker on this session: both compute lo/hi
    # from last_span_hi and the last writer would win per key. A stale
    # lock (crashed worker) expires via TTL, like flush_window's own guard.
    if st is not None and st.get("flushing") \
            and _lock_age(st["flushing"]) < FLUSH_LOCK_TTL:
        return {"ok": False, "reason": "flush-in-progress",
                "session": session_id,
                "hint": "a flush worker is mid-flight on this session; "
                        "re-run in a minute"}
    if st is None:
        st = {"harness": harness, "machine": machine_name(),
              "project": resolve_project(rec.get("cwd"), cfg)
              if rec.get("cwd") else None,
              "cwd": rec.get("cwd"), "started_at": now_utc(),
              "workspace": rec.get("cwd"),
              "prompts": 0, "steps": 0, "events_since_last": 0,
              "last_span_hi": None, "last_summary_id": None,
              "depth": rec.get("delegationDepth") or 0,
              "origin": rec.get("origin"),
              "parent_session": rec.get("parentSession"),
              "retries": 0, "stopped": False, "flushing": None}
    else:
        st.setdefault("workspace", rec.get("cwd") or st.get("workspace"))
        if st.get("depth") is None:
            st["depth"] = rec.get("delegationDepth") or 0
            st["origin"] = rec.get("origin")
    # _save_sessions prunes entries without a recent last_event_at
    st["last_event_at"] = now_utc()
    if reprocess:
        deleted = _cascade_delete_series(store, session_id)
        st.update({"last_span_hi": None, "last_summary_id": None,
                   "steps": 0, "prompts": 0, "retries": 0, "stopped": False})
    sessions[session_id] = st
    _save_sessions(store, sessions)

    last_hi = st.get("last_span_hi")
    lo = first_seq if last_hi is None else last_hi + 1
    pending = [e for e in all_evs if e.get("seq", -1) >= lo]
    n_tools = sum(1 for e in pending if e.get("type") == "tool/call")
    n = window_steps_for(cfg, st)
    if n is None:
        # depth-exempt: the parent that called this subagent summarises it
        # as part of its own tool calls — there is nothing to backfill.
        # (With --reprocess, any existing series was just cascaded away:
        # gated means gated, whatever the session logged before.)
        sessions.pop(session_id, None)
        _save_sessions(store, sessions)
        return {"ok": True, "session": session_id,
                "depth": st.get("depth"), "reason": "depth-exempt",
                "processed": 0}
    n_windows = (n_tools + n - 1) // n if n_tools else (1 if pending else 0)

    plan: dict = {"ok": True, "session": session_id,
                 "workspace": st.get("workspace"), "depth": st.get("depth"),
                 "last_span_hi": last_hi, "last_seq": last_seq,
                 "pending_events": len(pending), "pending_tool_calls": n_tools,
                 "window_steps": n, "windows_planned": n_windows}
    if reprocess:
        plan["reprocess_deleted"] = len(deleted)
    if dry_run:
        return plan
    if last_hi is not None and last_hi >= last_seq and not pending:
        return {**plan, "reason": "up-to-date", "processed": 0}

    # The tiling loop lives in the worker body now — one shared boundary
    # rule (see _flush_body) for live catch-up and backfill alike, so the
    # backfill reproduces exactly what the worker would have produced.
    r = flush_window(cfg, store, harness, session_id, force=True,
                    max_windows=max_windows, budget=float("inf"))
    plan["processed"] = r.get("processed", 0)
    plan["reports"] = r.get("reports", [])
    if not r.get("ok"):
        plan["reason"] = r.get("reason")  # e.g. llm-unavailable: re-run resumes
    return plan


# ---------------------------------------------------------------- temporal

_UNIT_DAYS = {"day": 1, "week": 7, "month": 30}


def temporal_rollup(cfg: Config, store: Store, machine: str | None,
                    unit: str, start: date,
                    workspace: str | None = None) -> dict | None:
    """Lazy materialize-one: the `unit` rollup starting at `start`, for
    `machine` (optionally scoped to one workspace). Query-with-caching:
    returns the cached node when present. Children: day <- that day's
    level-1 project summaries; week <- day rollups; month <- week rollups
    (materialized first)."""
    if not cfg.temporal_rollups_enabled:
        return None
    days = _UNIT_DAYS.get(unit, 1)
    period = f"{unit}:{start.isoformat()}"
    cached = store.find_temporal(machine, period, workspace)
    if cached:
        return {"materialized": False, **cached}
    child_ids: list[str] = []
    texts: list[str] = []
    if unit == "day":
        lo = f"{start.isoformat()}T00:00:00Z"
        hi = f"{(start + timedelta(days=1)).isoformat()}T00:00:00Z"
        children = store.list_summaries(axis="project", level=1,
                                        workspace=workspace, limit=500)
        children = [c for c in children
                    if (c["machine"] or "") == (machine or "")
                    and lo <= (c["ts"] or "") < hi]
        if not children:
            return {"materialized": False, "period": period,
                    "reason": "no-summaries-that-day"}
        texts = [f"({c['ts']} {c['project'] or 'machine'})\n{c['text']}"
                 for c in children[:40]]
        child_ids = [c["id"] for c in children[:40]]
    else:
        child_unit = "week" if unit == "month" else "day"
        step = _UNIT_DAYS[child_unit]
        for i in range(days // step):
            c = temporal_rollup(cfg, store, machine, child_unit,
                                start + timedelta(days=step * i), workspace)
            if c and c.get("text"):
                texts.append(f"({c['period']})\n{c['text']}")
                if c.get("id"):
                    child_ids.append(c["id"])
        if not texts:
            return {"materialized": False, "period": period,
                    "reason": "no-children"}
    level = {"day": 1, "week": 2, "month": 3}[unit]
    # records accumulate up the temporal axis the same way: the rollup
    # carries the union of its children's record refs
    recs: list[str] = []
    for cid in child_ids:
        row = store.get_summary(cid)
        if not row:
            continue
        for rid in (row.get("references") or []):
            if rid.startswith(_RECORD_PREFIXES) and rid not in recs:
                recs.append(rid)
    llm = _extractor(cfg)
    text = None
    structured = {"decisions": [], "problems": [], "open_items": []}
    if llm is not None:
        try:
            data = _summarize_recorded(cfg, store, llm, mode="temporal",
                                       child_texts=texts,
                                       machine=machine, unit=unit, period=period)
            if data and data.get("summary"):
                text = str(data["summary"]).strip()
                structured = _structured_fields(data)
        except Exception as e:
            print(f"memor: temporal summarize failed: {e}", file=sys.stderr)
    if text is None:
        text = ("(folded, LLM unavailable) " + period + ":\n"
                + "\n".join(t[:800] for t in texts[:20]))
    sid = store.insert_summary(
        ts=now_utc(), machine=machine, axis="temporal", period=period,
        workspace=workspace,
        level=level, references=child_ids + recs,
        text=text,
        decisions=structured["decisions"], problems=structured["problems"],
        open_items=structured["open_items"],
        model=getattr(cfg, "llm_model", None), status="current",
    )
    return {"materialized": True, "id": sid, "period": period, "text": text}


# ------------------------------------------------------- injection context

# The injection is a one-time SessionStart cost and the scoping above is
# what keeps it honest — there are deliberately NO character budgets here:
# every listed summary carries its full summary prose (attached
# Decisions/Open/Problems/Records sections are store material, not summary
# text), and the raw tail is a representative line per event.
TREE_LAST_L1 = 6            # last-N L1 windows, always, regardless of coverage


def _tree_span_label(s: dict, byid: dict, refs_of) -> str:
    """An entry's drill-down label — the seq range the entry covers, so
    every level is addressed the same way: the window's own span for L1s,
    the union of its leaf windows for folds (folds carry no span of their
    own). Folds whose leaves span more than one session keep the time
    label (seq ranges are per-session and would be meaningless mixed)."""
    sp = s.get("span") or ""
    if "seq[" in sp:
        return sp.split("seq[")[1].rstrip("]")
    lo = hi = None
    sessions: set = set()
    seen: set = set()
    stack = [s["id"]]
    while stack:
        sid = stack.pop()
        if sid in seen:
            continue
        seen.add(sid)
        for r in refs_of(byid[sid]):
            if r not in byid:
                continue
            child = byid[r]
            m = re.search(r"(.+):seq\[(\d+)\.\.(\d+)\]$",
                          child.get("span") or "")
            if m:
                sessions.add(m.group(1))
                lo = int(m.group(2)) if lo is None else min(lo, int(m.group(2)))
                hi = int(m.group(3)) if hi is None else max(hi, int(m.group(3)))
            else:
                stack.append(r)
    if lo is not None and len(sessions) <= 1:
        return f"{lo}..{hi}"
    ts = s.get("created_at") or ""
    return f"L{s['level']} fold {ts[11:16] if len(ts) >= 16 else ts}"


def _summary_prose(text: str | None) -> str:
    """The summary body — everything before the first attached section
    (## Decisions / ## Open / ## Problems / ## Records). The injection lists
    the summary texts one after the other; the attached material stays in
    the store (drill-down: memory_get_span / memor span)."""
    t = (text or "").strip()
    m = re.search(r"^## ", t, re.M)
    if m:
        t = t[:m.start()].rstrip()
    return t


def _entry_prose(text: str | None) -> str:
    """A summary's body as one clean paragraph for the injection: legacy
    attached sections cut off, markdown decoration dropped (bold, code
    spans, list markers, stray headers), all whitespace runs collapsed to
    single spaces. The store keeps the original text; this is display
    form only — no content is elided."""
    t = _summary_prose(text)
    t = re.sub(r"(?m)^#{1,6}\s*", "", t)
    t = re.sub(r"(?m)^\s*[-*]\s+", " ", t)
    t = t.replace("**", "").replace("__", "").replace("`", "")
    return " ".join(t.split())


def _compact_args(a) -> str:
    """One representative fragment of a tool call's arguments: the field
    that says what the call did (file / command / pattern / url / selector),
    or a short generic form otherwise. Content payloads (text to write,
    expressions) are deliberately not the representative field."""
    if a is None:
        return ""
    if isinstance(a, str):
        # DSH events carry the arguments as a JSON string
        try:
            a = json.loads(a)
        except ValueError:
            pass
    if isinstance(a, dict):
        for key in ("command", "file_path", "path", "pattern", "url",
                    "selector", "query"):
            if key in a and a[key] is not None:
                v = " ".join(str(a[key]).split())
                return f"{key}={v[:200]}{'…' if len(v) > 200 else ''}"
        s = json.dumps(a, ensure_ascii=False)
        return s if len(s) <= 120 else s[:120] + "…"
    s = str(a)
    return s if len(s) <= 120 else s[:120] + "…"


def _reminder_note(txt: str) -> str | None:
    """Harness-injected reminders arrive as user-role events whose payload
    is a file (an AGENTS.md reload), not the user's words. Returns a
    one-line representative form — source, size, pointer to the raw span;
    None when the text is not a reminder."""
    t = txt.strip()
    if not t.startswith("<system-reminder>"):
        return None
    m = re.search(r"Updated instructions from:\s*(\S+)", t)
    what = f"{m.group(1)} reloaded" if m else "harness reminder"
    return (f"[system-reminder: {what} - {t.count(chr(10)) + 1} lines, "
            f"{len(t)} chars; full text in the raw span]")


def session_tree_context(cfg: Config, store: Store,
                         workspace: str | None) -> str:
    """The workspace's session summary tree as a self-explanatory injection
    section (the 'how did we get here' spine). Pure function of the store —
    nothing generated: every listed node is a row from the summary tree, and
    every scoping decision is computed from the reference graph.

    Structure:
      A  Apex       — every top-level node, i.e. the current tree's apex (the deepest fold that
                      exists so far; it grows as the tree folds upward), in
                      as few lines as possible.
      B  Mid series — ALL nodes at level max-2 (the 9-18 node band at chain
                      width 3; the deepest below-apex level when the tree is
                      too shallow). Whole project at mid grain; nodes already
                      folded into A are repeated here at finer grain.
      C  Newer than B — downward walk from level max-3: at each level, the
                      nodes NOT transitively covered by A/B (and by C nodes
                      included so far) — subsumed children are not re-listed.
                      At L1: the last TREE_LAST_L1 windows, always.
      D  Raw tail   — per session: events after the flushed pointer, not
                      yet summarized (a flush worker is digesting them),
                      as one representative line per event (harness reminders as a one-line
                      note — the payload is a file, and it stays verbatim
                      in the raw span).
     T  Traversing — how to go deeper than this document: the surfaces
                     that answer specific drill-down questions (a point in
                     time, the whole tree, a day/week, a topic), plus the
                     pointer to the full guide in AGENTS.md.
     No character budgets: every entry carries the full summary prose —
     only the attached sections (Decisions/Open/Problems/Records) stay in
     the store. The document's size is a property of what the tree
     actually contains, and the scoping above (not elision) is what keeps
     that scoped.
      Interpretation: every entry describes its own window, including what
      was open then — later entries record the resolution; the current
      state is the newest entries plus D, never an older entry's open list.
    """
    if not workspace:
        return ""
    rows = [s for s in store.list_summaries(limit=20000)
            if (s.get("workspace") or workspace) == workspace
            and s.get("level")]
    if not rows:
        return ""
    byid = {s["id"]: s for s in rows}

    def refs_of(s: dict) -> list:
        r = s.get("references")
        if isinstance(r, str):
            r = json.loads(r or "[]")
        return list(r or [])

    def covered_by(roots: list) -> set:
        seen: set = set()
        stack = list(roots)
        while stack:
            for r in refs_of(byid[stack.pop()]):
                if r in byid and r not in seen:
                    seen.add(r)
                    stack.append(r)
        return seen

    def time_key(s: dict):
        return (s.get("created_at") or s["id"], s["id"])

    def entry(s: dict) -> str:
        # one clean paragraph, prefixed by the seq range the entry covers
        # (its own window for L1s, the union of leaf windows for folds) —
        # attached material (Decisions/Open/Problems/Records) stays in the store
        return f"[{_tree_span_label(s, byid, refs_of)}] " \
               f"{_entry_prose(s.get('text'))}"

    def entries(nodes: list[dict]) -> list[str]:
        """Entries, each enclosed: a blank line after the section heading,
        and exactly one blank line between consecutive entries."""
        lines: list[str] = [""]
        for s in nodes:
            lines.append("")
            lines.append(entry(s))
        return lines

    def span_lo(s: dict) -> int:
        m = re.search(r"seq\[(\d+)\.", s.get("span") or "")
        return int(m.group(1)) if m else -1

    top_level = max(s["level"] for s in rows)
    apex = [s for s in rows if s["level"] == top_level]
    # the 9-18 band at chain width 3, once the tree is steady; the
    # deepest below-apex level while the tree is still shallow
    mid_level = (top_level - 2) if top_level >= 4 else (top_level - 1)
    mid = ([s for s in rows if s["level"] == mid_level]
           if top_level >= 2 else [])
    covered = covered_by([s["id"] for s in apex + mid])

    out: list[str] = [
        f"# Workspace session history — {workspace}",
        "",
        "How to read this document — a compression ladder over this "
        "workspace's raw session logs (each level folds the sibling "
        "summaries of the level below it):",
        "  L1 = one work window (≈N tool calls, one paragraph) → L2 → L3 → …",
        "  A  Apex: the current tree's apex, i.e. the deepest fold that "
        "exists so far (it grows as the tree folds upward), in as few "
        "lines as possible (orientation).",
        f"  B  Mid series: L{mid_level} nodes — the whole project at mid "
        "grain (that level is computed as L-Max − 2 from the live tree; "
        "it holds 9-18 nodes once the tree is steady); nodes already "
        "folded into A appear here again at finer grain, by design.",
        "  C  Newer than B: work not yet folded into A/B, plus always the "
        f"last {TREE_LAST_L1} L1 windows (the latest thing that was "
        "happening, at full detail).",
        "  D  Raw tail: events after the last flushed window, one "
        "representative line per event — user messages whole (harness "
        "reminders as a one-line note), calls with "
        "the field that says what they did, results by status and first "
        "line; not yet summarized (a flush worker is digesting them).",
        "  Coverage: a summary whose span falls inside a LISTED higher "
        "summary's fold is not re-listed — a detail missing at a lower level "
        "is already inside the higher entry. Nodes marked 'uncovered' are "
        "the opposite: newer than every higher entry shown.",
        "  Every entry describes its own window, including what was open "
        "then: later entries record the resolution. Current state = the "
        "newest entries + D.",
        "  Every entry is prefixed by the seq range it covers — its own "
        "window for L1s, the union of its leaf windows for folds (e.g. "
        "1234..1301) — and is one clean paragraph (summarizer formatting "
        "like bold/lists dropped; nothing elided).",
        "  Drill down: `memor span \"dsh:<session>:seq[1234..1301]\"` "
        f"shows the raw window; `memor summaries --tree --workspace "
        f"{workspace}` shows the whole tree; MCP: "
        "memory_get_span / memory_drill_down.",
        "",
        f"## A. Apex (L{top_level} — deepest fold so far)",
    ]
    out += entries(sorted(apex, key=time_key))

    c_lines = ["", "## C. Newer than B (not yet folded into A/B)"]
    included_c: list[dict] = []
    for lvl in range(mid_level - 1, 0, -1):
        nodes = [s for s in rows if s["level"] == lvl]
        if lvl == 1:
            unc = sorted(nodes, key=span_lo)[-TREE_LAST_L1:]
            heading = f"### L1 — last {len(unc)} windows (always, " \
                      "regardless of coverage)"
        else:
            unc = [s for s in nodes if s["id"] not in covered]
            unc.sort(key=time_key)
            if not unc:
                continue
            heading = f"### L{lvl} — uncovered ({len(unc)}; their lower " \
                      "children are subsumed)"
        c_lines.append("")
        c_lines.append(heading)
        c_lines += entries(unc)
        included_c += unc
    if not included_c:
        c_lines.append("(none — everything is already covered by A/B)")
    covered |= covered_by([s["id"] for s in included_c])

    # D: raw tails — per session, events past the flushed pointer
    d_lines = ["", "## D. Raw tail (unsummarized; the flush worker is "
               "digesting this)"]
    sessions = _sessions(store)
    pending_sessions = []
    for sid, st in sessions.items():
        if st.get("workspace") != workspace:
            continue
        pointer = st.get("last_span_hi") or 0
        p = dsh_session_file(st.get("harness") or "dsh", sid,
                             cfg.session_dsh_root)
        if p is None:
            continue
        try:
            evs, _, tail = events_in_range(p, 0, 10**9)
        except Exception:  # noqa: BLE001 — a missing log is not an error
            continue
        after = [e for e in evs if e.get("seq", -1) > pointer]
        if after:
            pending_sessions.append((st.get("last_event_at") or "", sid,
                                     pointer, tail, after))
    any_d = False
    for _, sid, pointer, tail, after in sorted(
            pending_sessions, reverse=True)[:2]:
        rendered = []
        for e in after:
            et = e.get("type")
            d = e.get("data") or {}
            seq = e.get("seq")
            if et == "user/message":
                # the user's words are representative — kept whole; a
                # harness reminder carries a file, not words — one line
                txt = " ".join(c.get("text", "") for c in d.get("content", [])
                               if isinstance(c, dict))
                rendered.append(f"#{seq} user: {_reminder_note(txt) or txt}")
            elif et == "tool/call":
                rendered.append(f"#{seq} call {d.get('name')}: "
                                f"{_compact_args(d.get('arguments'))}")
            elif et == "tool/result":
                blocks = ((d.get("message") or {}).get("content")) or []
                err = any(b.get("isError") for b in blocks
                          if isinstance(b, dict))
                text = ""
                for b in blocks:
                    if isinstance(b, dict) and b.get("type") == "tool-result":
                        text = _text_blocks(b.get("content")) or text
                first = (text.split("\n") or [""])[0]
                if len(first) > 160:
                    first = first[:160].rsplit(" ", 1)[0] + "…"
                rendered.append(
                    f"#{seq} result{' [ERROR]' if err else ' ok'}"
                    + (f": {first}" if first else ""))
            elif et in ("turn/start", "turn/end"):
                rendered.append(f"#{seq} {et.split('/')[-1]}")
            # hook/step/agent/assistant: plumbing — dropped from the tail
        if not rendered:
            continue
        any_d = True
        d_lines.append(f"{sid}: pointer {pointer} → tail {tail}\n```\n"
                       + "\n".join(rendered) + "\n```")
    if not any_d:
        d_lines.append("(none — the log is fully flushed)")

    # Traversing: how to go DEEPER than this document — the surfaces that
    # answer the specific drill-down questions, and where the full guide is
    t_lines = [
        "",
        "## Traversing (when the above isn't enough)",
        "Point in time → `memor span \"dsh:<session>:seq[lo..hi]\"` (the raw "
        "window, including thinking). Whole tree → `memor summaries --tree "
        f"--workspace {workspace}` (nodes show [d:n p:n o:n] markers). "
        f"A day or week → `memor temporal --workspace {workspace}` (lazy "
        "rollups). A topic or error → `memor search \"<q>\"` (hits carry "
        "their span — the landing spot). MCP equivalents: "
        "memory_get_span / memory_get_summaries / memory_drill_down / "
        "memory_get_temporal / memory_search. Full guide: AGENTS.md, "
        "memor section.",
    ]

    # Assemble: all of A, then ALL of B, then C, then D, then Traversing —
    # nothing elided; entries are separated by exactly one blank line
    b_lines: list[str] = []
    if mid:
        b_lines = ["",
                   f"## B. Mid series — all L{mid_level} ({len(mid)} "
                   "nodes; whole project at mid grain)"]
        b_lines += entries(sorted(mid, key=time_key))
    return "\n".join(out + b_lines + c_lines + d_lines + t_lines)


def _state_section(store: Store, workspace: str) -> str:
    """The workspace's active state as the envelope's state section
    (P3): actionable-first and bounded — Open / Recent failures /
    Recent decisions / Standing knowledge — with the standing close
    instruction. '' when nothing is active and nothing recently failed
    (the full document, incl. recently handled and stale, stays on the
    on-demand fetch: `memor state show` / `memory_get_workspace_state`)."""
    from .wstate import state_section
    return state_section(store, workspace)


def _review_section(reviews: list[dict]) -> str:
    """The envelope's only window into the review queue: unresolved
    contradictions / pending proposals. The queue is machine-global, so
    this is emitted even when the workspace's own state is quiet. The
    queue is human territory — the section points at `memor review`;
    it never resolves anything itself."""
    lines = [f"## Open review items ({len(reviews)})", "",
             "Contradictions and proposals waiting for a human decision "
             "— items here are NOT established facts; `memor review` "
             "lists and resolves them.", ""]
    for r in reviews[:3]:
        lines.append(f"- [{r['id']}] ({r['category']}) {r['summary']}")
    if len(reviews) > 3:
        lines.append(f"… +{len(reviews) - 3} more (`memor review`)")
    return "\n".join(lines)


def injection_context(cfg: Config, store: Store, project: str | None,
                      workspace: str | None = None) -> str:
    """SessionStart injection package (off-path retrieval is the MCP
    tools). With `workspace`: that workspace's session-tree section
    (session_tree_context: apex + mid series + newer-than-B + raw tail)
    followed by its workspace state (active rows plus recent failures —
    the structured 'what is true right now' surface; the full document
    with the recently-handled tail is the on-demand fetch), then — when
    any — the machine's open review items. Without a workspace: the
    project's latest top-of-tree session summaries.
    No environment/state remainder: the package IS the summaries,
    one after the other."""
    if not cfg.session_context_enabled:
        return ""
    if workspace:
        tree = session_tree_context(cfg, store, workspace)
        if tree:
            parts = [tree]
            state = _state_section(store, workspace)
            if state:
                parts.append(state)
            reviews = store.open_reviews()
            if reviews:
                parts.append(_review_section(reviews))
            return "\n".join(parts)
        # no tree yet (fresh workspace) — the historic fallback
        wroots = store.latest_root_summaries(workspace=workspace, limit=2)
        if wroots:
            return ("## This workspace (recent work)\n"
                    + "\n\n".join(_entry_prose(r["text"])
                                  for r in wroots))
        return ""
    roots = store.latest_root_summaries(project=project, limit=2)
    if roots:
        return ("## Latest session summaries\n"
                + "\n\n".join(_entry_prose(r["text"]) for r in roots))
    return ""
