"""Harness session-log readers (SESSION-MEMORY.md §4/§5).

The contract is "harness fires command hooks + readable session storage":
the hooks only journal cheap markers, and the summary workers read the
harness's own raw session log for full-fidelity digests. Spans point into
that log:

    <harness>:<session-id>:seq[lo..hi]      e.g. dsh:session-abc:seq[1204..1417]

DSH keeps each session as zstd-compressed JSONL (one JSON event per line,
every event carries a monotonic `seq` and a millisecond `time`). Readers
are keyed by the harness prefix of the span, so other harnesses can
register their own without touching the core.
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Iterator

SPAN_RE = re.compile(r"^([a-z0-9][a-z0-9_-]*):(.+?):seq\[(\d+)\.\.(\d+)\]$")


class UnknownHarnessError(ValueError):
    pass


def span_format(harness: str, session: str, lo: int, hi: int) -> str:
    return f"{harness}:{session}:seq[{lo}..{hi}]"


def span_parse(span: str) -> tuple[str, str, int, int]:
    m = SPAN_RE.match(span.strip())
    if not m:
        raise ValueError(f"bad span: {span!r} "
                         "(expected <harness>:<session>:seq[lo..hi])")
    harness, session, lo, hi = m.group(1), m.group(2), int(m.group(3)), int(m.group(4))
    if lo > hi:
        raise ValueError(f"bad span range: {lo} > {hi}")
    return harness, session, lo, hi


# ------------------------------------------------------------------ DSH logs

def _log_files_for(sess_dir: Path) -> list[Path]:
    """The session log file(s) inside one session dir: prefers v2, plus any
    other `session*` JSONL (zstd CLI default `.zst`, plain `.jsonl` for
    older logs / other harnesses)."""
    cands: list[Path] = []
    v2 = sess_dir / "session.v2.jsonl.zstd"
    if v2.exists():
        cands.append(v2)
    for f in sess_dir.iterdir():
        if (f.name.startswith("session")
                and (f.name.endswith(".zstd") or f.name.endswith(".zst")
                     or f.name.endswith(".jsonl"))
                and f not in cands):
            cands.append(f)
    return cands


def dsh_session_file(harness: str, session_id: str, dsh_root: str) -> Path | None:
    """Locate the session log for `session_id` under `dsh_root` (~/.dsh).
    The log lives at sessions/<sanitized-cwd>/<session-id>/; the cwd
    component is opaque to us, so we glob it. Prefers the v2 format;
    returns None when the session (or root) doesn't exist yet.

    An archived session (moved out of sessions/ — by hand, or by a future
    DSH archive feature) is still drill-down-able: when the live root has
    nothing, the archive root (dsh_root/archive, any layout) is searched
    for a <session-id> dir. Live logs always win over archived ones."""
    if harness != "dsh":
        raise UnknownHarnessError(
            f"no session-log reader for harness {harness!r} (only 'dsh' for now)")
    root = Path(dsh_root).expanduser() / "sessions"
    cands: list[Path] = []
    if root.is_dir():
        for sess_dir in root.iterdir():
            if not sess_dir.is_dir():
                continue
            d = sess_dir / session_id
            if d.is_dir():
                cands.extend(_log_files_for(d))
    if not cands:
        # archived: layout-agnostic — match the session-id dir anywhere
        # under the archive root
        arch = Path(dsh_root).expanduser() / "archive"
        if arch.is_dir():
            for d in arch.rglob(session_id):
                if d.is_dir():
                    cands.extend(_log_files_for(d))
    if not cands:
        return None
    cands.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return cands[0]


def iter_lines(path: Path) -> Iterator[str]:
    """Stream JSONL lines from a (possibly zstd-compressed) session log.
    Shells out to zstdcat — no Python compression dependency (codebase
    style: verify.py & friends shell out the same way)."""
    if path.name.endswith(".zstd") or path.name.endswith(".zst"):
        proc = subprocess.Popen(["zstdcat", str(path)],
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        try:
            for line in proc.stdout:
                yield line.decode("utf-8", errors="replace")
        finally:
            proc.kill()
            proc.wait()
    else:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                yield line


def iter_events(path: Path) -> Iterator[dict]:
    """Parsed events, oldest first. Lines that don't parse are skipped —
    a torn tail line of a live log must never kill a flush."""
    for line in iter_lines(path):
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(ev, dict):
            yield ev


def events_in_range(path: Path, lo: int, hi: int) -> tuple[list[dict], int, int]:
    """Events with lo <= seq <= hi. Returns (events, first_seq, last_seq)
    where first/last are the seq of the first/last event in the file — the
    worker uses them to anchor the next window's span."""
    out: list[dict] = []
    first = last = -1
    for ev in iter_events(path):
        seq = ev.get("seq")
        if not isinstance(seq, int):
            continue
        first = first if first != -1 else seq
        last = seq
        if seq > hi:
            break  # monotonic
        if seq >= lo:
            out.append(ev)
    return out, first, last


def session_info(path: Path) -> dict:
    """The leading `session` event (id, cwd, createdAt) — no seq of its
    own; everything else follows with seq >= 0."""
    for ev in iter_events(path):
        if ev.get("type") == "session":
            return ev
        if ev.get("seq") is not None:
            break  # no leading session event; nothing to report
    return {}


def list_session_logs(dsh_root: str) -> list[dict]:
    """Every session log on disk under `dsh_root`, newest (mtime) first:
    {workspace_dir, session_id, path, mtime}. `workspace_dir` is the
    sanitized-cwd directory name (the DSH workspace); the unsanitized cwd
    comes from the session record (`session_info`)."""
    root = Path(dsh_root).expanduser() / "sessions"
    if not root.is_dir():
        return []
    out: list[dict] = []
    for ws_dir in sorted(root.iterdir()):
        if not ws_dir.is_dir():
            continue
        for sess_dir in ws_dir.iterdir():
            if not sess_dir.is_dir():
                continue
            cands = [f for f in sess_dir.iterdir()
                     if f.name.startswith("session")
                     and (f.name.endswith(".zstd") or f.name.endswith(".zst")
                          or f.name.endswith(".jsonl"))]
            if not cands:
                continue
            cands.sort(key=lambda p: p.stat().st_mtime, reverse=True)
            out.append({"workspace_dir": ws_dir.name,
                        "session_id": sess_dir.name,
                        "path": cands[0],
                        "mtime": cands[0].stat().st_mtime})
    out.sort(key=lambda d: d["mtime"], reverse=True)
    return out


def read_span(span: str, dsh_root: str) -> list[dict]:
    """Full raw events for a span (the drill-down path)."""
    harness, session, lo, hi = span_parse(span)
    path = dsh_session_file(harness, session, dsh_root)
    if path is None:
        raise FileNotFoundError(f"session log not found for span {span!r}")
    evs, _, _ = events_in_range(path, lo, hi)
    return evs
