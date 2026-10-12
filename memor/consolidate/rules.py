"""Deterministic evidence → candidate extraction.

Runs on every consolidation pass (with or without the LLM). Only emits
claims the events directly support; confidence is 'confirmed' because these
are observations, not inferences.
"""
from __future__ import annotations

import re

from ..models import Candidate, Scope

RE_SYSTEMCTL = re.compile(
    r"\bsystemctl\s+(?:--(?:user|global)\s+)?(start|stop|restart|enable|disable|reload|mask|unmask)\s+([A-Za-z0-9@._-]+)")
RE_DOCKER_CLI = re.compile(
    r"\bdocker\s+(?:compose\s+)?(up|down|start|stop|restart|rm)\s+([A-Za-z0-9._/-]+)?")
RE_PORT_PAIR = re.compile(r"(\d{1,5})\s*→\s*(\d{1,5})")

VERB_STATE = {
    "start": "is running", "stop": "is stopped", "restart": "was restarted",
    "enable": "is enabled (autostart)", "disable": "is disabled (autostart off)",
    "reload": "was reloaded", "mask": "is masked", "unmask": "is unmasked",
    "up": "is running (docker up)", "down": "was stopped (docker down)",
    "rm": "was removed (docker rm)",
}


def is_config_path(path: str) -> bool:
    """The config-file heuristic shared by rules (what to parse) and the fs
    watcher (what's worth storing full content for) — F10."""
    if not path:
        return False
    name = path.rsplit("/", 1)[-1]
    return (
        path.endswith((".service", ".conf", ".env", ".yml", ".yaml", ".toml", ".ini"))
        or "/systemd/" in path or name.startswith(".env")
        or path in ("/etc/hosts", "/etc/resolv.conf")
    )


def _port_change_candidates(ev: dict) -> list[Candidate]:
    out = []
    for ch in ev.get("payload", {}).get("changes", []):
        port = ch.get("port")
        if not port:
            continue
        addr = ch.get("addr", "")
        loc = f" on {addr}" if addr not in ("*", "0.0.0.0") else ""
        if ch["change"] == "bound":
            proc = ch.get("process") or "an unknown process"
            out.append(Candidate(
                kind="fact",
                scope=Scope(machine=ev.get("machine")),
                subject=f"port:{port}",
                claim=f"port {port}/{ch.get('proto','tcp')}{loc} is bound by {proc}",
                fact_kind="state", confidence="confirmed",
                valid_from=ev.get("ts"), evidence=[ev["id"]],
            ))
            if ch.get("process"):
                out.append(Candidate(
                    kind="relation",
                    scope=Scope(machine=ev.get("machine")),
                    subject=ch["process"],
                    claim=f"listens_on|port:{port}",
                    evidence=[ev["id"]], confidence="confirmed",
                ))
        elif ch["change"] == "freed":
            was = ch.get("was_process") or "a process"
            out.append(Candidate(
                kind="fact",
                scope=Scope(machine=ev.get("machine")),
                subject=f"port:{port}",
                claim=f"port {port}/{ch.get('proto','tcp')}{loc} is no longer bound (was {was})",
                fact_kind="state", confidence="confirmed",
                valid_from=ev.get("ts"), evidence=[ev["id"]],
            ))
    return out


def _file_change_candidates(ev: dict) -> list[Candidate]:
    p = ev.get("payload", {})
    path = p.get("path", "")
    if not path:
        return []
    name = path.rsplit("/", 1)[-1]
    if not is_config_path(path):
        return []
    scope = Scope(machine=ev.get("machine"), project=ev.get("project"))
    if p.get("action") == "deleted":
        return [Candidate(
            kind="fact", scope=scope, subject=f"config:{path}",
            claim=f"configuration file {path} was deleted",
            fact_kind="state", confidence="confirmed",
            valid_from=ev.get("ts"), evidence=[ev["id"]],
        )]
    cands = [Candidate(
        kind="fact", scope=scope, subject=f"config:{path}",
        claim=f"configuration file {path} was modified",
        fact_kind="configuration", confidence="confirmed",
        valid_from=ev.get("ts"), evidence=[ev["id"]],
    )]
    # key/value drift from the diff, e.g. PORT=8000 -> PORT=8080
    diff = p.get("diff", "")
    if diff:
        removed = {l[1:].strip() for l in diff.splitlines() if l.startswith("-") and "=" in l and "+++" not in l}
        added = {l[1:].strip() for l in diff.splitlines() if l.startswith("+") and "=" in l and "---" not in l}
        rm_keys = {r.split("=", 1)[0].strip(): r.split("=", 1)[1].strip() for r in removed if "+++" not in r}
        ad_keys = {a.split("=", 1)[0].strip(): a.split("=", 1)[1].strip() for a in added if "---" not in a}
        for k in set(rm_keys) & set(ad_keys):
            if rm_keys[k] != ad_keys[k]:
                cands.append(Candidate(
                    kind="fact", scope=scope, subject=f"config:{path}:{k}",
                    claim=f"{name}: {k} changed {rm_keys[k]} → {ad_keys[k]}",
                    fact_kind="configuration", confidence="confirmed",
                    reason=f"observed diff in {path}",
                    valid_from=ev.get("ts"), evidence=[ev["id"]],
                ))
        # "port X → Y" style mentions inside the diff
        for m in RE_PORT_PAIR.finditer(diff):
            cands.append(Candidate(
                kind="fact", scope=scope, subject=f"config:{path}",
                claim=f"{name}: port changed {m.group(1)} → {m.group(2)}",
                fact_kind="configuration", confidence="confirmed",
                valid_from=ev.get("ts"), evidence=[ev["id"]],
            ))
    return cands


def extract(events: list[dict], shell_failures: bool = False) -> list[Candidate]:
    cands: list[Candidate] = []
    for ev in events:
        source, kind, command = ev.get("source"), ev.get("kind"), ev.get("command") or ""
        machine = ev.get("machine")
        project = ev.get("project")

        if kind == "port_change":
            cands.extend(_port_change_candidates(ev))
            continue
        if kind == "file_change":
            cands.extend(_file_change_candidates(ev))
            continue

        m = RE_SYSTEMCTL.search(command)
        if m and source in ("shell", "agent"):
            verb, unit = m.group(1), m.group(2)
            # canonical subject is always `service:<stem>` — one vocabulary
            # shared with the A1 verification job
            stem = unit.removesuffix(".service")
            scope = Scope(machine=machine, project=project, component=stem)
            cands.append(Candidate(
                kind="fact", scope=scope,
                subject=f"service:{stem}",
                claim=f"systemd service {stem} {VERB_STATE.get(verb, verb + 'ed')}",
                fact_kind="state", confidence="confirmed",
                valid_from=ev.get("ts"), evidence=[ev["id"]],
                reason=f"command: {command[:120]}",
            ))

        m = RE_DOCKER_CLI.search(command)
        if m and source in ("shell", "agent"):
            verb, target = m.group(1), m.group(2)
            if target and not target.startswith("-"):
                cands.append(Candidate(
                    kind="fact",
                    scope=Scope(machine=machine, project=project, component=target),
                    subject=f"container:{target}",
                    claim=f"container {target} {VERB_STATE.get(verb, verb + 'ed')}",
                    fact_kind="state", confidence="confirmed",
                    valid_from=ev.get("ts"), evidence=[ev["id"]],
                ))

        if source == "docker" and kind.startswith("docker_container"):
            p = ev.get("payload", {})
            name = p.get("name")
            action = p.get("action")
            if name and action in ("start", "stop", "die", "destroy", "kill", "create"):
                state = {"start": "started", "stop": "stopped", "die": "died (exited)",
                         "destroy": "was removed", "kill": "was killed",
                         "create": "was created"}[action]
                cands.append(Candidate(
                    kind="fact",
                    scope=Scope(machine=machine, project=project, component=name),
                    subject=f"container:{name}",
                    claim=f"container {name} {state}",
                    fact_kind="state", confidence="confirmed",
                    valid_from=ev.get("ts"), evidence=[ev["id"]],
                ))

        if kind == "commit" and source == "git":
            p = ev.get("payload", {})
            repo = p.get("repo", "")
            if project:
                cands.append(Candidate(
                    kind="fact",
                    scope=Scope(project=project, machine=machine),
                    subject=f"repo:{project}",
                    claim=(f"repository {project}: commit {p.get('subject','')[:80]}"
                           f" on branch {p.get('branch','')}"),
                    fact_kind="fact", confidence="confirmed",
                    valid_from=ev.get("ts"), evidence=[ev["id"]],
                ))

        if source == "human" and kind == "note":
            p = ev.get("payload", {})
            cands.append(Candidate(
                kind="fact",
                scope=Scope(project=p.get("project"), component=p.get("component"),
                            machine=machine or p.get("machine")),
                subject=p.get("subject", "note"),
                claim=command,
                fact_kind=p.get("kind", "fact"),
                confidence=p.get("confidence", "confirmed"),
                reason=p.get("reason"),
                valid_from=ev.get("ts"), evidence=[ev["id"]],
                source_type="human", origin="human",
            ))

        # F7: failed commands are high-signal, low-noise memory —
        # "have I hit this error before?" must be answerable without an LLM.
        # Agent runs are deliberate actions (always captured); interactive
        # shell noise is opt-in via `problem_capture_shell`.
        pl = ev.get("payload") or {}
        exit_code = pl.get("exit_code", pl.get("exit"))
        if exit_code is not None and exit_code != 0 and command and \
                (source == "agent" or (source == "shell" and shell_failures)):
            out = " ".join(
                ((pl.get("output") or "") + " " + (pl.get("stderr") or "")).split())
            tail = out[-200:] if out else ""
            cands.append(Candidate(
                kind="fact", scope=Scope(project=project, machine=machine),
                subject=f"failure:{command[:60]}",
                claim=f"command failed (exit {exit_code}): {command[:120]}",
                fact_kind="problem", confidence="confirmed",
                valid_from=ev.get("ts"), evidence=[ev["id"]],
                reason=f"exit {exit_code}" + (f"; last output: {tail}" if tail else ""),
            ))

    return dedup_candidates(cands)


def dedup_candidates(cands: list[Candidate]) -> list[Candidate]:
    """Keep one candidate per (kind, scope, subject, normalized claim)."""
    from ..models import normalize_claim

    seen: dict[tuple, Candidate] = {}
    for c in cands:
        key = (c.kind, c.scope.key(), c.subject.lower(),
               normalize_claim(c.claim) if c.kind != "relation" else
               (c.claim.lower(),))
        if key not in seen:
            seen[key] = c
        else:
            # merge evidence
            for e in c.evidence:
                if e not in seen[key].evidence:
                    seen[key].evidence.append(e)
    return list(seen.values())