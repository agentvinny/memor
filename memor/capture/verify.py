"""A1: active verification of current state.

Re-observes ground truth (listening ports, systemd units, docker
containers) and reconciles it with current state facts:

- stale claims are superseded (`source_type="verify"`, evidence = the
  verification run) so the brief's "should not falsely preserve the old
  state" holds for changes that leave no captured trace (crash, reboot,
  off-band manual change);
- bootstrap: observed state with no fact yet becomes a fact (so right
  after a reboot, "what is running on this machine" is answerable).

Only machine-scoped, re-observable state is touched (`service:*`,
`port:*`, `container:*`). Knowledge that can't be re-observed
(decisions, lessons, human-asserted facts) is never modified.
"""
from __future__ import annotations

import subprocess

from ..models import now_utc
from ..scope import machine_name
from ..store import Store


def probe_systemd(units: list[str]) -> dict[str, str]:
    """unit -> 'active' | 'inactive' | 'unknown'. A unit counts as active
    if either the system or the user scope reports it as such."""
    out: dict[str, str] = {}
    for unit in units:
        for args in ((["systemctl", "is-active", unit],
                      ["systemctl", "--user", "is-active", unit])):
            try:
                r = subprocess.run(args, capture_output=True, text=True, timeout=15)
            except Exception:
                continue
            state = r.stdout.strip()
            if state == "active":
                out[unit] = "active"
                break
            if r.returncode == 0 and state and state != "inactive":
                # unknown state (e.g. 'activating') — don't assert anything
                out.setdefault(unit, "unknown")
        else:
            out[unit] = out.get(unit, "inactive")
    return out


def running_user_units() -> list[str]:
    """Service units currently running in the user scope (bootstrap)."""
    out: list[str] = []
    for scope_args in ((["--user"], [])):
        try:
            r = subprocess.run(
                ["systemctl", *scope_args, "list-units", "--type=service",
                 "--state=running", "--no-legend", "--plain"],
                capture_output=True, text=True, timeout=15,
            )
        except Exception:
            continue
        if r.returncode != 0:
            continue
        for line in r.stdout.splitlines():
            parts = line.split()
            if parts and parts[0].endswith(".service"):
                out.append(parts[0].removesuffix(".service"))
    return out


def _docker_statuses() -> dict[str, str]:
    try:
        from .docker_events import container_snapshot, docker_available
        if not docker_available():
            return {}
        return {c["name"]: c["status"] for c in container_snapshot()}
    except Exception:
        return {}


def run_verification(store: Store, cfg, *, machine: str | None = None) -> dict:
    """One verification pass. Returns a report (checked / changed / created)."""
    from .netproc import snapshot_listen

    machine = machine or cfg.machine or machine_name()
    listeners = snapshot_listen()
    containers = _docker_statuses()

    # ---- collect the currently-claimed state for this machine ----
    svc_facts: dict[str, list[dict]] = {}
    port_facts: dict[str, list[dict]] = {}
    cont_facts: dict[str, list[dict]] = {}
    for f in store.list_facts(kind="state", status="current", limit=2000):
        if f.get("scope_machine") not in (None, machine):
            continue
        subj = f.get("subject") or ""
        if subj.startswith("service:"):
            svc_facts.setdefault(subj[len("service:"):], []).append(f)
        elif subj.startswith("port:"):
            port_facts.setdefault(subj[len("port:"):], []).append(f)
        elif subj.startswith("container:"):
            cont_facts.setdefault(subj[len("container:"):], []).append(f)

    units = set(svc_facts) | set(running_user_units())
    services = probe_systemd(sorted(units)) if units else {}

    evidence = store.append_event(
        "verify", "verify_run", machine=machine,
        command=(f"verification pass: {len(units)} units,"
                 f" {len(listeners)} listeners, {len(containers)} containers"),
        payload={"machine": machine,
                 "units_checked": sorted(units),
                 "listeners": len(listeners),
                 "containers": len(containers)},
    )
    ev_id = evidence["id"]

    changes: list[dict] = []
    created: list[str] = []

    def supersede(fact: dict, claim: str, reason: str) -> None:
        new = {
            "scope_project": fact["scope_project"],
            "scope_component": fact["scope_component"],
            "scope_machine": machine, "subject": fact["subject"],
            "claim": claim, "kind": "state", "confidence": "confirmed",
            "valid_from": now_utc(), "reason": reason,
            "source_type": "verify",
        }
        fid = store.supersede(fact, new)
        store.add_evidence(fid, "verify", ev_id)
        changes.append({"subject": fact["subject"], "was": fact["claim"],
                       "now": claim})

    def create(subject: str, claim: str, component: str | None, reason: str) -> None:
        fid = store.insert_fact(
            scope_project=None, scope_component=component, scope_machine=machine,
            subject=subject, claim=claim, kind="state", confidence="confirmed",
            reason=reason, source_type="verify",
        )
        store.add_evidence(fid, "verify", ev_id)
        created.append(fid)

    # ---- services ----
    for unit in sorted(units):
        state = services.get(unit, "unknown")
        if state == "unknown":
            continue
        facts = svc_facts.get(unit, [])
        for f in facts:
            claim = f["claim"]
            if "is running" in claim and state == "inactive":
                supersede(f, f"systemd service {unit} is stopped (verified)",
                          f"verified state changed: unit {unit} is inactive")
            elif "is stopped" in claim and state == "active":
                supersede(f, f"systemd service {unit} is running (verified)",
                          f"verified state changed: unit {unit} is active")
        if state == "active" and not facts:
            create(f"service:{unit}", f"systemd service {unit} is running",
                   unit, "verified: unit is active (bootstrap)")

    # ---- ports ----
    bound = {str(l["port"]): l for l in listeners}
    for port, facts in port_facts.items():
        l = bound.get(port)
        for f in facts:
            claim = f["claim"]
            if "is bound" in claim and l is None:
                supersede(f, f"port {port} is no longer bound (verified)",
                          "verified state changed: port is not listening")
            elif "no longer bound" in claim and l is not None:
                proc = l.get("process") or "an unknown process"
                supersede(f, f"port {port} is bound by {proc} (verified)",
                          "verified state changed: port is listening again")
        if l is not None and not facts:
            proc = l.get("process") or "an unknown process"
            loc = f" on {l['addr']}" if l.get("addr") not in ("*", "0.0.0.0") else ""
            create(f"port:{port}",
                   f"port {port}/{l.get('proto', 'tcp')}{loc} is bound by {proc}",
                   None, "verified: port is listening (bootstrap)")

    # ---- containers ----
    for name, facts in cont_facts.items():
        status = containers.get(name)
        for f in facts:
            claim = f["claim"]
            if status is None:
                if "is running" in claim or "started" in claim:
                    supersede(f, f"container {name} was removed (verified)",
                              "verified state changed: container no longer exists")
            elif status.startswith("Up"):
                if "is running" not in claim:
                    supersede(f, f"container {name} is running (verified)",
                              "verified state changed: container is up")
            else:
                if "is running" in claim:
                    supersede(f, f"container {name} is stopped (verified)",
                              f"verified state changed: {status}")

    for name, status in containers.items():
        if status.startswith("Up") and not cont_facts.get(name):
            create(f"container:{name}", f"container {name} is running (docker up)",
                   name, "verified: container is up (bootstrap)")

    # G3: a change with no human/agent activity nearby is 'unexplained' —
    # exactly the case the brief says the system must flag, not paper over
    unexplained: list[dict] = []
    if changes:
        from datetime import datetime, timedelta, timezone
        window_start = (datetime.now(timezone.utc)
                        - timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
        nearby = [e for e in store.events_since(window_start, limit=500)
                  if e.get("source") in ("shell", "agent", "human", "git",
                                         "docker", "filesystem", "systemd",
                                         "network")]
        if not nearby:
            unexplained = list(changes)
            for ch in changes[:5]:
                pid = store.add_proposal(
                    "surprise", {"change": ch, "machine": machine},
                    "auto_applied", note="unexplained state change")
                store.queue_review(pid, "surprise",
                                   f"unexplained on {machine}: "
                                   f"{ch['was'][:60]} -> {ch['now'][:60]} "
                                   f"(no human/agent activity in the last 10 min)")

    return {"machine": machine,
            "checked": {"units": sorted(units),
                        "listeners": len(listeners),
                        "containers": len(containers)},
            "changes": changes,
            "created": created,
            "unexplained": unexplained}