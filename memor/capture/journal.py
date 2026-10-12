"""systemd journal capture: tail `journalctl -o json`, keep lifecycle +
warning-or-higher entries. Unit *file* edits are NOT in the journal (known
journald gap) — the fs watcher covers those. On start, entries since the
last daemon run are backfilled first (F9: reboot/daemon-down gap), then the
stream is followed. Backfill and follow dedupe against each other."""
from __future__ import annotations

import json
import subprocess
import time

from ..scope import machine_name
from ..store import Store

LIFECYCLE_PREFIXES = (
    "Started", "Stopped", "Deactivated", "Failed", "Activating", "Reached",
    "Unit ", "Starting",
)
_THROTTLE: dict[str, float] = {}
_BACKFILL_LIMIT = 20000


def journal_available() -> tuple[str, list[str]]:
    """Return (mode, base_cmd) — 'system', 'user', or ('', [])."""
    for mode, extra in (("system", []), ("user", ["--user"])):
        try:
            r = subprocess.run(
                ["journalctl", "-n", "1", "-o", "json", "--no-pager", *extra],
                capture_output=True, timeout=10,
            )
            if r.returncode == 0 and r.stdout.strip():
                return mode, ["journalctl", "-o", "json", "-f", "--no-pager", *extra]
        except Exception:
            continue
    return "", []


def _interesting(entry: dict) -> bool:
    unit = entry.get("_SYSTEMD_UNIT", "")
    msg = entry.get("MESSAGE", "")
    prio = entry.get("PRIORITY", "6")
    if not unit:
        return False
    if msg[:10] in tuple(p[:10] for p in LIFECYCLE_PREFIXES) or any(
            msg.startswith(p) for p in LIFECYCLE_PREFIXES):
        return True
    try:
        return int(prio) <= 3  # err..emerg
    except ValueError:
        return False


def _emit(store: Store, entry: dict, mode: str, emitted: set[str]) -> None:
    """Filter, throttle, dedupe, and append one journal entry."""
    unit = entry["_SYSTEMD_UNIT"]
    rt = entry.get("__REALTIME_TIMESTAMP")
    key = f"{unit}:{rt}:{entry.get('MESSAGE', '')[:60]}"
    if key in emitted:
        return
    emitted.add(key)
    if len(emitted) > 20000:
        # keep the set bounded; dropping old keys only risks a rare
        # re-emit, which downstream dedupe absorbs
        for k in list(emitted)[:10000]:
            emitted.discard(k)
    now = time.time()
    if now - _THROTTLE.get(key, 0) < 60:
        return
    _THROTTLE[key] = now
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(int(rt) / 1e6)) if rt else None
    store.append_event(
        "systemd", "unit_event",
        machine=machine_name(), ts=ts,
        command=f"systemd [{mode}] {unit}: {entry.get('MESSAGE', '')[:200]}",
        payload={
            "mode": mode, "unit": unit,
            "message": entry.get("MESSAGE", "")[:1024],
            "priority": entry.get("PRIORITY"),
            "identifier": entry.get("SYSLOG_IDENTIFIER"),
            "pid": entry.get("_PID"),
        },
    )


def process_journal_lines(lines, store: Store, mode: str, emitted: set[str]) -> int:
    """Parse + emit journal JSON lines (used by backfill and tests)."""
    n = 0
    for line in lines:
        line = line.strip() if isinstance(line, str) \
            else line.decode(errors="ignore").strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not _interesting(entry):
            continue
        _emit(store, entry, mode, emitted)
        n += 1
    return n


def tail_journal(store: Store, stop: "callable", backfill_since: str | None = None) -> None:
    mode, cmd = journal_available()
    if not cmd:
        return
    emitted: set[str] = set()
    # F9: recover the gap (reboot, daemon downtime) before following
    if backfill_since:
        base = ["journalctl", "-o", "json", "--no-pager",
                "--since", backfill_since, "--lines", str(_BACKFILL_LIMIT)]
        if mode == "user":
            base.append("--user")
        try:
            r = subprocess.run(base, capture_output=True, text=True, timeout=120)
            if r.returncode == 0:
                process_journal_lines(r.stdout.splitlines(), store, mode, emitted)
        except Exception:
            pass
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, text=True)
    except FileNotFoundError:
        return
    assert proc.stdout is not None
    for line in proc.stdout:
        if stop():
            break
        process_journal_lines([line], store, mode, emitted)
    proc.terminate()