"""Process/network poll-diff: snapshot listening ports via `ss -tulpn`,
emit an event only when the state changes (port bound/freed, process swap)."""
from __future__ import annotations

import re
import subprocess
import time

from ..scope import machine_name
from ..store import Store

SS_RE = re.compile(
    r"^\s*(tcp|udp)\d?\s+LISTEN\s+\d+\s+\d+\s+(\S+?)(?::(\d+))?\s+(.*)$")
PROC_RE = re.compile(r'users:\(\("([^"]*)",pid=(\d+)')


def snapshot_listen(text: str | None = None) -> list[dict]:
    if text is None:
        try:
            text = subprocess.run(["ss", "-tulpnH"], capture_output=True, text=True,
                                  timeout=15).stdout
        except Exception:
            return []
    return _parse_lines(text)


def _parse_lines(out: str) -> list[dict]:
    rows = []
    for line in out.splitlines():
        if "LISTEN" not in line:
            continue
        m = SS_RE.match(line)
        if not m:
            continue
        proto, addr, port, rest = m.groups()
        pm = PROC_RE.search(rest or "")
        rows.append({"proto": proto, "addr": addr or "0.0.0.0",
                     "port": int(port or 0),
                     "process": pm.group(1) if pm else None,
                     "pid": int(pm.group(2)) if pm else None})
    return rows


def _key(r: dict) -> str:
    return f"{r['proto']}:{r['addr']}:{r['port']}"


def diff_snapshots(prev: list[dict], cur: list[dict]) -> list[dict]:
    prev_by = {_key(r): r for r in prev}
    cur_by = {_key(r): r for r in cur}
    events = []
    for k, r in cur_by.items():
        p = prev_by.get(k)
        if p is None:
            events.append({"change": "bound", "proto": r["proto"], "addr": r["addr"],
                           "port": r["port"], "process": r["process"], "pid": r["pid"]})
        elif (p["process"], p["pid"]) != (r["process"], r["pid"]):
            events.append({"change": "process_changed", "proto": r["proto"],
                           "addr": r["addr"], "port": r["port"],
                           "was_process": p["process"], "process": r["process"]})
    for k, p in prev_by.items():
        if k not in cur_by:
            events.append({"change": "freed", "proto": p["proto"], "addr": p["addr"],
                           "port": p["port"], "was_process": p["process"]})
    return events


def poll_net(store: Store, stop: "callable", interval: float = 20.0) -> None:
    prev = None
    snap = store.snap_get("net.snapshot")
    if snap:
        prev = snap
    else:
        prev = snapshot_listen()
        store.append_event(
            "network", "net_snapshot", machine=machine_name(),
            command=f"network: {len(prev)} listening sockets at startup",
            payload={"machine": machine_name(), "listeners": prev, "initial": True},
        )
        store.snap_set("net.snapshot", prev)
    while not stop():
        t_end = time.time() + interval
        while time.time() < t_end and not stop():
            time.sleep(min(1.0, max(0.1, t_end - time.time())))
        if stop():
            break
        cur = snapshot_listen()
        changes = diff_snapshots(prev, cur)
        if changes:
            store.append_event(
                "network", "port_change", machine=machine_name(),
                command="; ".join(
                    (f"port {c['port']}/{c['proto']} on {c['addr']} {c['change']}"
                     + (f" by {c['process']}" if c.get("process") else ""))
                    for c in changes[:8]),
                payload={"machine": machine_name(), "changes": changes,
                         "listeners": cur},
            )
        prev = cur
        store.snap_set("net.snapshot", prev)