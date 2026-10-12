"""Docker evidence: lifecycle events (`docker events`) + container config
snapshot diffing (native replacement for Docker Drift: poll `docker inspect`
and emit an event on drift)."""
from __future__ import annotations

import json
import subprocess
import time

from ..scope import machine_name
from ..store import Store


def docker_available() -> bool:
    try:
        return subprocess.run(
            ["docker", "info", "--format", "{{.ServerVersion}}"],
            capture_output=True, timeout=10,
        ).returncode == 0
    except Exception:
        return False


def tail_docker_events(store: Store, stop: "callable") -> None:
    """Long-running: stream `docker events` into the store."""
    try:
        proc = subprocess.Popen(
            ["docker", "events", "--format", "{{json .}}"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
        )
    except FileNotFoundError:
        return
    assert proc.stdout is not None
    for line in proc.stdout:
        if stop():
            break
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        actor = ev.get("Actor", {})
        attrs = actor.get("Attributes", {})
        name = attrs.get("name") or attrs.get("Image") or actor.get("ID", "")[:12]
        etype = ev.get("Type", "")
        action = ev.get("Action", "")
        store.append_event(
            "docker", f"docker_{etype}_{action}",
            machine=machine_name(),
            command=f"docker {etype} {action} {name}",
            ts=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ev.get("time", time.time()))),
            payload={
                "type": etype, "action": action, "name": name,
                "id": actor.get("ID"), "attributes": attrs,
            },
        )
    proc.terminate()


def container_snapshot() -> list[dict]:
    """Current container state: name, image, status, published ports, env count."""
    try:
        out = subprocess.run(
            ["docker", "ps", "-a", "--format",
             "{{.Names}}\t{{.Image}}\t{{.Status}}\t{{.Ports}}"],
            capture_output=True, text=True, timeout=30,
        ).stdout
    except Exception:
        return []
    out_c = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) < 4:
            continue
        name, image, status, ports = parts[0], parts[1], parts[2], parts[3]
        port_map = {}
        for p in filter(None, ports.split(", ")):
            # e.g. "0.0.0.0:8080->80/tcp" or "127.0.0.1:5432->5432/tcp"
            if "->" in p:
                host_side, container_side = p.split("->", 1)
                hp = host_side.rsplit(":", 1)[-1]
                cp = container_side.split(":", 1)[0] if ":" in container_side else ""
                proto = container_side.rsplit("/", 1)[-1] if "/" in container_side else "tcp"
                addr = host_side.rsplit(":", 1)[0] if ":" in host_side else "0.0.0.0"
                port_map[f"{cp}/{proto}"] = f"{addr}:{hp}"
            else:
                port_map[p] = p
        out_c.append({"name": name, "image": image, "status": status, "ports": port_map})
    return out_c


def poll_docker_snapshots(store: Store, stop: "callable", interval: float = 900.0) -> None:
    """Emit a docker_snapshot on start, then drift events when config changes."""
    if not docker_available():
        return
    prev = store.snap_get("docker.snapshot") or {"containers": []}
    while not stop():
        cur = container_snapshot()
        cur_by = {c["name"]: c for c in cur}
        prev_by = {c["name"]: c for c in prev["containers"]}
        changes = []
        for name, c in cur_by.items():
            p = prev_by.get(name)
            if p is None:
                changes.append({"name": name, "change": "appeared", "now": c})
            else:
                for field in ("image", "status"):
                    if p.get(field) != c.get(field):
                        changes.append({"name": name, "change": field,
                                        "was": p.get(field), "now": c.get(field)})
                if p.get("ports") != c.get("ports"):
                    changes.append({"name": name, "change": "ports",
                                    "was": p.get("ports"), "now": c.get("ports")})
        for name, p in prev_by.items():
            if name not in cur_by:
                changes.append({"name": name, "change": "removed", "was": p})
        payload = {"machine": machine_name(), "containers": cur, "changes": changes}
        is_baseline = (not prev["containers"]) and prev.get("initial", True)
        kind = "docker_snapshot" if is_baseline else \
            ("docker_drift" if changes else "docker_snapshot")
        if is_baseline:
            command = (f"docker snapshot (baseline: {len(cur)} containers)")
        else:
            command = (f"docker drift: " + "; ".join(
                f"{c['name']} {c['change']}" for c in changes[:8])) if changes else \
                "docker snapshot"
        store.append_event(
            "docker", kind, machine=machine_name(),
            command=command,
            payload=payload,
        )
        prev = {"containers": cur, "initial": False}
        store.snap_set("docker.snapshot", prev)
        t_end = time.time() + interval
        while time.time() < t_end and not stop():
            time.sleep(min(2.0, t_end - time.time()))
    store.snap_set("docker.snapshot", prev)


def docker_snapshot_once(store: Store) -> None:
    if not docker_available():
        return
    cur = {"containers": container_snapshot(), "initial": False}
    store.append_event(
        "docker", "docker_snapshot", machine=machine_name(),
        command="docker snapshot (on-demand)",
        payload={"machine": machine_name(), **cur, "changes": []},
    )
    store.snap_set("docker.snapshot", cur)