"""MCP server: the agent-facing query surface (brief §24).

Tools (stdio MCP, vendor-neutral):
  memory_search, memory_get_current_state, memory_get_history,
  memory_get_related, memory_get_decisions, memory_get_evidence,
  memory_get_project_context, memory_get_recent_changes

Retrieval order follows brief §25: narrowest/cheapest mechanism first
(exact scope → entity → current state → relations → decisions → history →
semantic keyword search; raw evidence only when asked).
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import Config
from .models import Candidate, Scope, CONFIDENCES, FACT_KINDS, normalize_claim
from .store import Store

_cfg: Config | None = None


def get_store() -> Store:
    global _cfg
    if _cfg is None:
        _cfg = Config.load()
    return Store(_cfg.data_dir)


def _j(*objs) -> str:
    return json.dumps(objs[0] if len(objs) == 1 else list(objs),
                      ensure_ascii=False, indent=1, default=str)


def _compact_fact(f: dict) -> dict:
    return {k: f.get(k) for k in
            ("id", "subject", "claim", "kind", "status", "confidence",
             "valid_from", "valid_until", "reason", "supersedes_id",
             "scope_project", "scope_component", "scope_machine") if f.get(k) is not None}


def _compact_event(e: dict) -> dict:
    return {k: e.get(k) for k in ("id", "ts", "source", "kind", "command", "project", "machine")
            if e.get(k)}


def _cfg_or_load() -> "Config":
    global _cfg
    if _cfg is None:
        _cfg = Config.load()
    return _cfg


def _validate_evidence(store: Store, evidence: list[str] | None) -> str | None:
    """C1: writes may only cite evidence that exists — otherwise the write
    is rejected (never stored unattributed)."""
    if not evidence:
        return None
    missing = [e for e in evidence if not store.get_event(e)]
    if missing:
        return f"evidence ids do not exist: {missing} (search for real events first)"
    return None


def _apply_and_report(store: Store, cfg, candidates) -> str:
    from .consolidate.apply import apply_candidates
    from .consolidate.markdown import regenerate
    res = apply_candidates(store, cfg, candidates)
    try:
        regenerate(store, cfg)
    except Exception:
        pass
    return _j({
        "result": res.summary(),
        "inserted": res.inserted,
        "superseded": [f"{a} -> {b}" for a, b in res.superseded],
        "queued": res.queued,
        "dropped": res.dropped,
        "noops": res.noops,
    })


def resolve_entity(store: Store, name: str):
    """Return (entity_row_or_None, hints). Accepts plain names and
    'project:x' / 'machine:y' / 'component:z' specifiers. A plain name that
    matches no entity is treated as a subject (e.g. 'service:web', 'port:8080')."""
    if ":" in name and name.split(":", 1)[0] in ("project", "machine", "component"):
        kind, val = name.split(":", 1)
        return None, {"project": val if kind == "project" else None,
                      "component": val if kind == "component" else None,
                      "machine": val if kind == "machine" else None,
                      "subject": None}
    ent = store.find_entity(name)
    if ent:
        ek = ent.get("kind")
        # A project-kind entity scopes the query to THAT project. (An earlier
        # version returned project=None here, which silently meant "no
        # filter": every entity query with a plain project name — timeline,
        # current state, history, related — degraded to store-wide.)
        return ent, {"project": ent["name"] if ek == "project" else ent.get("project"),
                     "machine": None if ek == "machine" else ent.get("machine"),
                     "component": None if ek in ("project", "machine") else ent["name"],
                     "subject": None}
    return None, {"project": None, "component": None, "machine": None,
                  "subject": name}


# --------------------------------------------------------------------- tools

def tool_search(query: str, scope_project: str | None = None,
                scope_component: str | None = None, limit: int = 20,
                workspace: str | None = None, machine: str | None = None,
                session: str | None = None, since: str | None = None,
                until: str | None = None, level: str | None = None,
                kinds: str | None = None,
                mode: str | None = None) -> str:
    """Scoped hybrid search (R3). Composable scope axes: workspace
    (the session cwd — the usual default), project, component, machine,
    session, time window (since/until), summary level, kinds
    (event|fact|decision|summary|state|prompt|entity). `mode`: hybrid
    (default — FTS ∪ semantic, rank-fused), fts, or semantic (embeddings
    of summaries + workspace state + user prompts; falls back to FTS
    with a note when no embedder is available). Every hit carries its
    span/scope — the landing spot for drill-down (memory_get_span,
    memory_get_summaries, memory_get_workspace_state)."""
    from .search import search_mcp
    store = get_store()
    out = search_mcp(store, _cfg_or_load(), query,
                     project=scope_project, component=scope_component,
                     machine=machine, workspace=workspace, session=session,
                     since=since, until=until, level=level, kinds=kinds,
                     mode=mode, limit=limit)
    if not out["hits"] and "note" not in out:
        out["note"] = "no matches"
    return _j(out)


def tool_get_current_state(entity: str) -> str:
    """Current (status=current) facts + open decisions for an entity.

    Entity may be a name, or 'project:x' / 'machine:y' / 'component:z'."""
    store = get_store()
    _, hints = resolve_entity(store, entity)
    facts = store.list_facts(
        project=hints.get("project"), component=hints.get("component"),
        machine=hints.get("machine"), status="current", limit=200)
    if not facts and hints.get("subject"):
        stem = hints["subject"].split(":", 1)[-1]
        for q in ({"subject": hints["subject"]}, {"component": stem},
                  {"subject": stem}):
            facts = store.list_facts(status="current", limit=200, **q)
            if facts:
                break
    unknown = store.list_facts(project=hints.get("project"), machine=hints.get("machine"),
                               status="unknown", limit=20)
    contrad = store.list_facts(project=hints.get("project"), machine=hints.get("machine"),
                               status="contradictory", limit=20)
    out = {
        "entity": entity,
        "current": [_compact_fact(f) for f in facts],
        "uncertain": [_compact_fact(f) for f in unknown],
        "conflicting": [_compact_fact(f) for f in contrad],
    }
    return _j(out)


def _entity_subjects(store, ent, hints: dict) -> list[str]:
    """The subjects an entity query stands for: the entity's own name,
    its scoped hints, plus the subjects of the entity's scoped facts
    (only when a scope actually exists — otherwise a plain subject name
    would sweep the whole store)."""
    subjects = set()
    if ent:
        subjects.add(ent["name"])
    if hints.get("subject"):
        subjects.add(hints["subject"])
    if hints.get("component"):
        subjects.add(hints["component"])
    scoped = (hints.get("project") or hints.get("component")
              or hints.get("machine") or ent)
    if scoped:
        for f in store.list_facts(project=hints.get("project"),
                                  component=hints.get("component"),
                                  machine=hints.get("machine"), limit=300):
            subjects.add(f["subject"])
    return sorted(subjects)[:20]


def tool_get_history(entity: str, limit: int = 50) -> str:
    """Temporal history: every fact ever recorded for the entity's subjects,
    current + superseded + contradictory, oldest first. A→B→A shows all three."""
    store = get_store()
    ent, hints = resolve_entity(store, entity)
    out: dict = {"entity": entity, "subjects": {}, "decisions": []}
    for s in _entity_subjects(store, ent, hints):
        hist = store.fact_history(s, project=hints.get("project"),
                                  component=hints.get("component"))
        if hist:
            out["subjects"][s] = [_compact_fact(f) for f in hist[:limit]]
    if hints.get("project") or hints.get("component"):
        decs = store.list_decisions(project=hints.get("project"),
                                    component=hints.get("component"), limit=20)
        out["decisions"] = [{k: d.get(k) for k in
                             ("id", "subject", "choice", "reason", "decided_at")} for d in decs]
    return _j(out)


def tool_get_state_as_of(entity: str, at: str) -> str:
    """Point-in-time query: 'what was true about the entity at `at`'
    (ISO ts). Returns every fact whose validity window covers that
    moment plus the decisions in force then — 'what port did the server
    use when we deployed v3?', not just 'what does it use now'.
    Retracted records never appear; contradictory ones do (the conflict
    may have been real at that moment)."""
    store = get_store()
    ent, hints = resolve_entity(store, entity)
    out: dict = {"entity": entity, "at": at, "subjects": {}}
    for s in _entity_subjects(store, ent, hints):
        rows = store.facts_valid_at(at, s, project=hints.get("project"),
                                    component=hints.get("component"))
        if rows:
            out["subjects"][s] = [_compact_fact(f) for f in rows]
    if hints.get("project") or hints.get("component"):
        decs = store.decisions_valid_at(at, project=hints.get("project"),
                                        component=hints.get("component"))
        out["decisions"] = [{k: d.get(k) for k in
                            ("id", "subject", "choice", "reason", "decided_at")}
                            for d in decs]
    if not any(out["subjects"].values()):
        out["note"] = ("no recorded state covers that moment (nothing was "
                       "written for it, or the record was retracted)")
    return _j(out)


def tool_get_related(entity: str, depth: int = 1) -> str:
    """Relationships (runs_on, depends_on, ...) reachable from the entity,
    plus facts about those related entities."""
    store = get_store()
    ent, _ = resolve_entity(store, entity)
    name = ent["name"] if ent else entity
    rels = store.relations_for(name, depth=max(1, min(depth, 3)))
    others = {r["object"] if r["subject"].lower() == name.lower() else r["subject"]
              for r in rels}
    facts = []
    for o in sorted(others)[:10]:
        facts.extend(store.list_facts(component=o, subject=o,
                                      project=o, status="current", limit=10))
    out = {
        "entity": name,
        "relations": [{k: r.get(k) for k in ("subject", "relation", "object",
                                             "valid_from", "confidence")} for r in rels],
        "related_facts": [_compact_fact(f) for f in facts[:40]],
    }
    return _j(out)


def tool_get_decisions(scope_project: str | None = None,
                       scope_component: str | None = None, limit: int = 25) -> str:
    """Decisions with rationale and alternatives, newest first."""
    store = get_store()
    decs = store.list_decisions(project=scope_project, component=scope_component,
                                limit=limit)
    return _j([{k: d.get(k) for k in
                ("id", "subject", "choice", "reason", "alternatives_considered",
                 "confidence", "decided_at", "evidence_ids", "status",
                 "valid_until", "supersedes_id")} for d in decs])


def tool_get_evidence(fact_id: str) -> str:
    """Provenance for a fact/decision: evidence rows + the raw events behind
    them. Answers 'why do you believe this?'"""
    store = get_store()
    fact = store.get_fact(fact_id)
    out: dict = {"fact_id": fact_id}
    if fact:
        out["fact"] = _compact_fact(fact)
        out["evidence"] = []
        for e in store.evidence_for(fact_id):
            ev = e.get("event")
            out["evidence"].append({
                "source_type": e.get("source_type"), "pointer": e.get("pointer"),
                "event": _compact_event(ev) if ev else None,
                "event_payload": ev.get("payload") if ev else None,
            })
    else:
        dec = None
        for d in store.list_decisions(limit=1000):
            if d["id"] == fact_id:
                dec = d
                break
        if dec:
            out["decision"] = {k: dec.get(k) for k in ("subject", "choice", "reason", "decided_at")}
            out["evidence"] = [{"event": _compact_event(e) if e else None,
                                "event_payload": (e or {}).get("payload")}
                               for e in (store.get_event(i) for i in dec["evidence_ids"])]
        else:
            out["note"] = "unknown id (search for facts/decisions first)"
    return _j(out)


def tool_get_project_context(project: str, include_evidence: bool = False,
                             max_chars: int = 0) -> str:
    """Compact 'catch me up' package for a project (brief §25 retrieval
    order): orientation → current state → relations → decisions → recent
    history → open/uncertain items. include_evidence pulls raw-event trails;
    max_chars caps the size (0 = uncapped) by dropping low-priority
    sections (decisions, relations, evidence) first."""
    store = get_store()
    cfg = _cfg_or_load()
    lines = [f"# Context: {project}", ""]
    desc = next((p.description for p in cfg.projects if p.name == project), None)
    if desc:
        lines += [f"> {desc}", ""]
    # session-memory: top-of-tree summaries land first (navigation surface;
    # drill with memory_drill_down / memory_get_span)
    sum_lines = []
    roots = store.latest_root_summaries(project=project, limit=2)
    if roots:
        sum_lines = ["## Recent session summaries"]
        for r in roots:
            addr = r.get("span") or f"refs:{len(r['references'])}"
            mark = "" if r["status"] == "current" else f" ({r['status']})"
            sum_lines.append(f"- {r['ts'][:16]} L{r['level']} {addr}{mark} `[{r['id']}]`")
            for ln in r["text"][:400].splitlines():
                sum_lines.append(f"  {ln}")
    facts = store.list_facts(project=project, status="current", limit=200)
    # project-scoped facts vs box-wide (project-NULL) facts: the latter are
    # shared machine state (verify-bootstrapped services/ports) and must not
    # drown the project catch-up
    proj_facts = [f for f in facts if f.get("scope_project") == project]
    mach_facts = [f for f in facts if f.get("scope_project") is None]
    comps = sorted({f["scope_component"] for f in proj_facts
                    if f.get("scope_component")})
    current_lines = ["## Current state"]
    if proj_facts:
        for c in ([None] + comps):
            sub = [f for f in proj_facts if (f.get("scope_component") or None) == c]
            if c:
                current_lines.append(f"### {c}")
            for f in sub:
                conf = "" if f["confidence"] == "confirmed" else f" ({f['confidence']})"
                current_lines.append(f"- {f['claim']}{conf} `[{f['id']}]`")
    else:
        current_lines.append("(no project-scoped facts yet)")
    if mach_facts:
        current_lines += ["", "### Machine state (shared — this box, not project-specific)",
                         "_(full view: memory_get_global_context)_"]
        for f in sorted(mach_facts, key=lambda x: x["subject"])[:10]:
            conf = "" if f["confidence"] == "confirmed" else f" ({f['confidence']})"
            current_lines.append(f"- {f['claim']}{conf} `[{f['id']}]`")
        if len(mach_facts) > 10:
            current_lines.append(f"- …and {len(mach_facts) - 10} more machine facts")

    rel_lines = ["## Relations"]
    any_rel = False
    for name in ([project] + comps)[:6]:
        for r in store.relations_for(name, depth=1):
            if r["subject"].lower() == name.lower():
                rel_lines.append(f"- {name} —{r['relation']}→ {r['object']}")
                any_rel = True
    if not any_rel:
        rel_lines.append("(none recorded)")

    dec_lines = ["## Recent decisions"]
    decs = store.list_decisions(project=project, limit=10)
    if decs:
        for d in decs:
            mark = "" if d.get("status") == "current" else f" ({d.get('status')})"
            reason = f" — {d['reason']}" if d.get("reason") else ""
            dec_lines.append(f"- {d['decided_at'][:10]}{mark}: {d['choice']}{reason} `[{d['id']}]`")
    else:
        dec_lines.append("(none)")

    # F12: "changes" = real transitions in the window — facts that became
    # current AND facts that stopped being true (with their before-state)
    since = _days_ago(14)
    all_facts = store.list_facts(project=project, limit=500)
    chg = []
    for f in facts:
        if (f.get("valid_from") or "") >= since:
            why = f" (reason: {f['reason']})" if f.get("reason") else ""
            chg.append(f"- {f['valid_from'][:10]}: {f['claim']}{why}")
    for f in all_facts:
        if f["status"] == "superseded" and (f.get("valid_until") or "") >= since:
            why = f" (reason: {f['reason']})" if f.get("reason") else ""
            chg.append(f"- {f['valid_until'][:10]}: {f['claim']} → superseded{why}")
    change_lines = [f"## Changes in the last 14 days ({len(chg)})"]
    change_lines += (chg[:30] if chg else ["(none)"])

    open_items = store.list_facts(project=project, status="unknown", limit=20) + \
        store.list_facts(project=project, status="contradictory", limit=20)
    reviews = store.open_reviews()
    open_lines = ["## Open items / uncertain"]
    if open_items or reviews:
        for f in open_items:
            open_lines.append(f"- [{f['status']}] {f['claim']} `[{f['id']}]`")
        for r in reviews[:10]:
            open_lines.append(f"- [review:{r['category']}] {r['summary']} `[{r['id']}]`")
    else:
        open_lines.append("(none)")

    ev_lines = []
    if include_evidence and proj_facts:
        ev_lines = ["## Evidence trails (most recent 10 facts)"]
        for f in proj_facts[:10]:
            evs = store.evidence_for(f["id"])
            if not evs:
                continue
            ev_lines.append(f"- {f['claim'][:80]} `[{f['id']}]`:")
            for e in evs[:4]:
                ev = e.get("event")
                ev_lines.append(f"    - {e['source_type']} {ev.get('ts','?')} "
                                f"{(ev.get('command') or '')[:100]} `[{e['pointer']}]`")

    size_lines = []
    if cfg.max_store_size_bytes > 0:
        from .store import data_dir_size
        size = data_dir_size(cfg.data_dir)
        if size > cfg.max_store_size_bytes:
            size_lines = ["## ⚠ Store size alert",
                          f"The store uses {size / 1e9:.2f} GiB — over the "
                          f"{cfg.max_store_size_bytes / 1e9:.0f} GiB threshold. "
                          f"Tell the user (options: `memor compact`, or raise "
                          f"max_store_size_bytes in memor.json)."]

    # priority order for truncation: identity > open items > summaries >
    # current > changes > decisions > relations > raw evidence
    sections = [lines, open_lines, size_lines, sum_lines, current_lines,
                change_lines, dec_lines, rel_lines, ev_lines]
    text = "\n".join("\n".join(s) for s in sections if s)
    if max_chars > 0 and len(text) > max_chars:
        kept = [lines]
        total = len("\n".join(lines))
        for s in sections[1:]:
            if not s:
                continue
            cost = len("\n".join(s)) + 1
            if total + cost > max_chars:
                continue  # drop this (and later, lower-priority) section
            kept.append(s)
            total += cost
        text = "\n".join("\n".join(s) for s in kept if s)
        text += ("\n…[truncated — lower-priority sections omitted; use "
                 "memory_search / memory_get_history / "
                 "memory_get_project_context(include_evidence=true) for detail]")
    return text


def _days_ago(n: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=n)).strftime("%Y-%m-%dT%H:%M:%SZ")


def tool_get_recent_changes(scope_project: str | None = None,
                            scope_machine: str | None = None,
                            since_hours: int = 72) -> str:
    """What changed recently: supersessions/new facts, raw events by source,
    open review items."""
    store = get_store()
    since = _days_ago(since_hours / 24)
    out: dict = {"since": since}
    facts = store.list_facts(project=scope_project, machine=scope_machine, limit=500)
    recent_facts = [f for f in facts if (f.get("valid_from") or "") >= since]
    out["facts"] = [_compact_fact(f) for f in recent_facts[:40]]
    events = store.events_since(since, project=scope_project, limit=500)
    if scope_machine:
        events = [e for e in events if e.get("machine") == scope_machine]
    by_src: dict[str, int] = {}
    for e in events:
        by_src[e["source"]] = by_src.get(e["source"], 0) + 1
    out["event_counts_by_source"] = by_src
    out["latest_events"] = [_compact_event(e) for e in events[-20:]]
    revs = store.open_reviews()
    if revs:
        out["open_reviews"] = revs[:10]
    return _j(out)


# ------------------------------------------------------------ write tools
# C1: agents/harnesses contribute memory through the same validated apply
# pipeline as consolidation. Cited evidence must exist; uncited claims land
# in the review queue (status=unknown) and never become current on their own.
# Supersede, never overwrite: conflicting writes are queued, not silent.

def tool_add_fact(subject: str, claim: str, scope_project: str | None = None,
                  scope_component: str | None = None, kind: str = "fact",
                  confidence: str = "probable", reason: str | None = None,
                  evidence: list[str] | None = None) -> str:
    """Record a fact/state/lesson/... in memory. Cite evidence ids (from
    memory_search / recent events) when you have them. Agent/harness writes
    are trusted-actor assertions: they become current with provenance and
    confidence visible (only LLM-derived claims are gated on evidence /
    queued for review). A write that contradicts a current fact is queued."""
    store = get_store()
    err = _validate_evidence(store, evidence)
    if err:
        return _j({"error": err})
    c = Candidate(
        kind="fact",
        scope=Scope(project=scope_project, component=scope_component),
        subject=subject[:120], claim=claim,
        fact_kind=kind if kind in FACT_KINDS else "fact",
        confidence=confidence if confidence in CONFIDENCES else "probable",
        reason=reason, evidence=list(evidence or []),
        source_type="agent", origin="agent",
    )
    return _apply_and_report(store, _cfg_or_load(), [c])


def tool_add_decision(subject: str, choice: str,
                      scope_project: str | None = None,
                      scope_component: str | None = None,
                      alternatives: list[str] | None = None,
                      reason: str | None = None, confidence: str = "probable",
                      evidence: list[str] | None = None) -> str:
    """Record a decision WITH its rationale and alternatives. A conflicting
    choice for the same subject supersedes the prior decision (temporal
    history is kept). Agent/harness writes are trusted-actor assertions;
    explicit contradictions are queued for review."""
    store = get_store()
    err = _validate_evidence(store, evidence)
    if err:
        return _j({"error": err})
    c = Candidate(
        kind="decision",
        scope=Scope(project=scope_project, component=scope_component),
        subject=subject[:120], claim=choice,
        alternatives=[str(a)[:200] for a in (alternatives or [])[:8]],
        confidence=confidence if confidence in CONFIDENCES else "probable",
        reason=reason, evidence=list(evidence or []),
        source_type="agent", origin="agent",
    )
    return _apply_and_report(store, _cfg_or_load(), [c])


def tool_add_relation(subject: str, relation: str, object: str,
                      evidence: list[str] | None = None) -> str:
    """Record a relationship (runs_on, depends_on, uses, part_of, ...).
    Evidence is required for relation writes."""
    store = get_store()
    if not evidence:
        return _j({"error": "relation writes require evidence ids"})
    err = _validate_evidence(store, evidence)
    if err:
        return _j({"error": err})
    c = Candidate(
        kind="relation", scope=Scope(),
        subject=subject[:80], claim=f"{relation[:40]}|{object[:80]}",
        evidence=list(evidence), source_type="agent", origin="agent",
    )
    return _apply_and_report(store, _cfg_or_load(), [c])


def tool_add_note(text: str, scope_project: str | None = None,
                  scope_component: str | None = None, kind: str = "fact",
                  confidence: str = "confirmed", reason: str | None = None,
                  evidence: list[str] | None = None) -> str:
    """Quick note/learning (human `memor remember` equivalent for agents)."""
    return tool_add_fact("note", text, scope_project, scope_component,
                         kind, confidence, reason, evidence)


def tool_correct(fact_id: str, claim: str, reason: str | None = None) -> str:
    """Correct a wrong memory: supersedes it with the corrected claim.
    The original row and its evidence are preserved (audit intact)."""
    store = get_store()
    fid = store.correct_fact(fact_id, claim, reason=reason, decided_by="agent")
    if not fid:
        return _j({"error": f"unknown fact id: {fact_id}"})
    from .consolidate.markdown import regenerate
    try:
        regenerate(store, _cfg_or_load())
    except Exception:
        pass
    return _j({"corrected": fact_id, "now": fid})


def tool_retract(fact_id: str, reason: str | None = None) -> str:
    """Retract a memory (e.g. it was wrong or is obsolete). It leaves every
    'current' surface but stays in history with the retraction reason."""
    store = get_store()
    if not store.forget_fact(fact_id, reason=reason):
        return _j({"error": f"unknown fact id: {fact_id}"})
    from .consolidate.markdown import regenerate
    try:
        regenerate(store, _cfg_or_load())
    except Exception:
        pass
    return _j({"retracted": fact_id})


def tool_get_timeline(entity: str, since_hours: int = 72, limit: int = 200) -> str:
    """G1: the literal 'how did we get here' view — raw events, fact
    transitions (became current / stopped being true) and decisions merged
    into one chronological line for a project/component/machine.
    Entry ids let you drill down with memory_get_evidence / memory_search."""
    from datetime import timedelta
    store = get_store()
    _, hints = resolve_entity(store, entity)
    since = (datetime.now(timezone.utc)
             - timedelta(hours=since_hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
    entries: list[tuple[str, str, str, str]] = []

    def _scope_tag(rec: dict) -> str:
        # In a project-scoped timeline, list_facts/list_decisions may also
        # return box-wide (scope_project NULL) records: label them so a
        # reader can tell the project's own history from machine-wide notes.
        if not hints.get("project"):
            return ""
        sp = rec.get("scope_project")
        if sp is None:
            return " (box-wide)"
        if sp != hints.get("project"):
            return f" [{sp}]"
        return ""

    evs = store.events_since(since, project=hints.get("project"), limit=2000)
    comp = hints.get("component")
    for e in evs:
        if e.get("source") == "memor":
            continue
        if comp and comp.lower() not in (e.get("command") or "").lower() \
                and e.get("project") != comp:
            continue
        entries.append((e["ts"], "event",
                        f"{e['source']}/{e['kind']}: "
                        f"{(e.get('command') or '')[:100]}", e["id"]))
    for f in store.list_facts(project=hints.get("project"),
                              component=comp, limit=500):
        if f.get("valid_from") and f["valid_from"] >= since:
            note = f" ({f['reason']})" if f.get("reason") else ""
            entries.append((f["valid_from"], "fact+",
                            (f"{f['claim']} [{f['confidence']}]{note}")[:120]
                            + _scope_tag(f), f["id"]))
        if f.get("valid_until") and f["valid_until"] >= since:
            entries.append((f["valid_until"], "fact-",
                            f"{f['claim']} → {f['status']}"[:120] + _scope_tag(f),
                            f["id"]))
    for d in store.list_decisions(project=hints.get("project"),
                                  component=comp, limit=100):
        if (d.get("decided_at") or "") >= since:
            note = f" (because {d['reason']})" if d.get("reason") else ""
            entries.append((d["decided_at"], "decision",
                            f"{d['choice']}{note}"[:120] + _scope_tag(d), d["id"]))
    for s in store.list_summaries(project=hints.get("project"),
                                  limit=100, order="asc"):
        if (s.get("ts") or "") >= since:
            addr = s.get("span") or f"refs:{len(s['references'])}"
            entries.append((s["ts"], "summary",
                            f"L{s['level']} {addr}: {s['text'][:80]}".replace("\n", " "),
                            f"summary:{s['id']}"))
    entries.sort(key=lambda x: x[0])
    lines = [f"# Timeline: {entity} (last {since_hours}h, up to {limit} entries)", ""]
    for ts, kind, text, eid in entries[-limit:]:
        lines.append(f"{ts}  {kind:<9} {text}  [{eid}]")
    if not entries:
        lines.append("(nothing in this window)")
    return "\n".join(lines)


def tool_get_recent_episodes(scope_project: str | None = None,
                             since_hours: int = 168, limit: int = 20) -> str:
    """Work episodes (objective, activity sketch, outcome, lessons) from
    recent work windows — 'what did we actually work on?'"""
    from datetime import timedelta
    since = (datetime.now(timezone.utc) - timedelta(hours=since_hours)
             ).strftime("%Y-%m-%dT%H:%M:%SZ")
    store = get_store()
    eps = store.list_episodes(project=scope_project, since=since, limit=limit)
    return _j([{k: e.get(k) for k in
                ("id", "project", "machine", "started_at", "ended_at",
                 "objective", "summary", "lessons")} for e in eps])


def tool_get_global_context() -> str:
    """D2: cross-machine Level-0 view — which machines exist, what projects
    and services each carries, and where things are uncertain. Use after
    importing stores from other boxes, or to orient on the whole fleet."""
    store = get_store()
    machines = sorted({f["scope_machine"] for f in store.list_facts(limit=2000)
                       if f.get("scope_machine")}
                      | {e["name"] for e in store.list_entities(kind="machine")})
    lines = ["# Global view (all machines in this store)", ""]
    if not machines:
        lines.append("(no machine-scoped memory yet)")
        return "\n".join(lines)
    for m in machines:
        facts = store.list_facts(machine=m, status="current", limit=300)
        projects = sorted({f["scope_project"] for f in facts if f.get("scope_project")})
        services = [f for f in facts
                    if f["subject"].startswith(("service:", "port:")) or
                    f["subject"].endswith(".service") or
                    f["subject"].startswith("container:")]
        unknown = store.list_facts(machine=m, status="unknown", limit=20) + \
            store.list_facts(machine=m, status="contradictory", limit=20)
        lines += [f"## {m}", ""]
        lines.append(f"- projects: {', '.join(projects) or '(none)'}"
                     f"   ({len(facts)} current facts)")
        if services:
            lines.append("- notable state:")
            for f in services[:12]:
                lines.append(f"  - {f['claim']} `[{f['id']}]`")
        if unknown:
            lines.append("- uncertain/conflicting:")
            for f in unknown[:8]:
                lines.append(f"  - [{f['status']}] {f['claim']} `[{f['id']}]`")
        lines.append("")
    cfg = _cfg_or_load()
    if cfg.max_store_size_bytes > 0:
        from .store import data_dir_size
        size = data_dir_size(cfg.data_dir)
        if size > cfg.max_store_size_bytes:
            lines += ["⚠ Store size alert",
                      f"Store uses {size / 1e9:.2f} GiB — over the "
                      f"{cfg.max_store_size_bytes / 1e9:.0f} GiB threshold "
                      f"(tell the user; options: `memor compact`, or raise "
                      f"max_store_size_bytes in memor.json)"]
    return "\n".join(lines)


# ----------------------------------------------------------- session-memory

def _span_event_compact(ev: dict) -> dict:
    """One harness-log event, budgeted for agent consumption (full fidelity
    stays in the harness log — this is a preview)."""
    from .sessionmem import _text_blocks
    t = ev.get("type")
    d = ev.get("data") or {}
    out: dict = {"seq": ev.get("seq"), "type": t, "time": ev.get("time")}
    if t == "user/message":
        out["role"] = (d.get("source") or {}).get("kind")
        out["text"] = _text_blocks(d.get("content"))[:800]
    elif t == "assistant/message":
        out["text"] = _text_blocks((d.get("message") or {}).get("content"))[:1500]
    elif t == "tool/call":
        out["name"] = d.get("name")
        out["arguments"] = (d.get("arguments") or "")[:800]
    elif t == "tool/result":
        blocks = ((d.get("message") or {}).get("content")) or []
        out["is_error"] = any(b.get("isError") for b in blocks
                              if isinstance(b, dict))
        text = ""
        for b in blocks:
            if isinstance(b, dict) and b.get("type") == "tool-result":
                text = _text_blocks(b.get("content")) or text
        out["text"] = text[:800]
    else:
        out["data"] = json.dumps(d, ensure_ascii=False, default=str)[:500]
    return out


def tool_get_span(span: str | None = None, harness: str | None = None,
                  session: str | None = None, start_seq: int | None = None,
                  end_seq: int | None = None, limit: int = 200) -> str:
    """Read the harness's RAW session log for a span — the drill-down path
    when a summary's digest is not enough. Pass `span`
    ('<harness>:<session>:seq[lo..hi]') or harness/session with optional
    seq bounds. Returns budgeted event previews; the complete trace stays
    in the harness log."""
    from .sessionlog import (dsh_session_file, events_in_range, span_parse)
    cfg = _cfg_or_load()
    store = get_store()
    from .sessionmem import _sessions
    if span:
        harness, session, lo, hi = span_parse(span)
    else:
        if not (harness and session):
            return _j({"error": "need span or harness+session"})
        st = _sessions(store).get(session)
        lo = start_seq or ((st or {}).get("last_span_hi") or 0) + 1
        hi = end_seq or 10 ** 9
    path = dsh_session_file(harness, session, cfg.session_dsh_root)
    if path is None:
        return _j({"error": f"session log not found for {harness}:{session}"
                            f" (root {cfg.session_dsh_root})"})
    evs, first_seq, last_seq = events_in_range(path, lo, hi)
    return _j({"harness": harness, "session": session,
               "log": str(path), "first_seq": first_seq, "last_seq": last_seq,
               "requested": [lo, hi],
               "count": len(evs), "events": [_span_event_compact(e) for e in evs[:limit]]})


def tool_get_summaries(scope_project: str | None = None,
                       session: str | None = None, limit: int = 50,
                       workspace: str | None = None) -> str:
    """Browse the summary trees (project axis, one session, or one
    workspace's tree), oldest first: each node's text, level, span (leaf
    -> raw data) or references (children), and status. Use
    memory_drill_down / memory_get_span to go deeper."""
    store = get_store()
    rows = store.list_summaries(project=scope_project, session=session,
                                workspace=workspace, order="asc", limit=limit)
    out = []
    for r in rows:
        out.append({k: r.get(k) for k in
                   ("id", "ts", "machine", "session", "harness", "axis",
                    "project", "component", "level", "parent_id", "period",
                    "span", "references", "status")} |
                   {"text": r["text"][:500], "subjects": r["subjects"][:8]})
    return _j({"count": len(out), "summaries": out})


def tool_drill_down(summary_id: str) -> str:
    """Go one level deeper from a summary. Refs are typed by prefix:
    sum_* -> child summaries (drill each further), fact_*/dec_* -> the
    records this window produced (read their proof with memory_get_evidence);
    a leaf's span -> budgeted raw harness-log events. A node can carry both
    edges at once (a leaf window lists its records AND its raw span)."""
    store = get_store()
    s = store.get_summary(summary_id)
    if s is None:
        return _j({"error": f"no such summary: {summary_id}"})
    out = {"summary": {k: s.get(k) for k in
                       ("id", "ts", "project", "session", "level", "axis",
                        "span", "references", "status")}}
    refs = s.get("references") or []
    children = [c for c in (store.get_summary(r) for r in refs
                            if r.startswith("sum_")) if c]
    if children:
        out["children"] = [{k: c.get(k) for k in
                            ("id", "ts", "level", "span", "status")} |
                           {"text": c["text"][:400]} for c in children]
    rec_ids = [r for r in refs if r.startswith(("fact_", "dec_"))]
    if rec_ids:
        out["records"] = []
        for rid in rec_ids:
            r = (store.get_fact(rid) if rid.startswith("fact_")
                 else store.get_decision(rid))
            if r:
                out["records"].append(
                    {k: r.get(k) for k in
                     ("id", "subject", "kind", "claim", "choice", "status")})
    if s.get("span"):
        try:
            detail = json.loads(tool_get_span(s["span"], limit=100))
            out["raw_events"] = detail.get("events", [])
        except Exception as e:
            out["raw_events_error"] = str(e)
    if children or rec_ids or s.get("span"):
        out["hint"] = ("drill into a child with memory_drill_down(child id); "
                       "leaves expose raw data via memory_get_span; records "
                       "are facts/decisions — pull their proof with "
                       "memory_get_evidence")
    else:
        out["note"] = "no span and no references — nothing deeper to drill"
    return _j(out)


def tool_get_temporal(scope_machine: str | None = None, unit: str = "day",
                      start: str | None = None, days: int = 1,
                      scope_workspace: str | None = None) -> str:
    """Lazy temporal rollups (query-with-caching): that machine's day of
    project summaries across projects, folded day->week->month; with
    `scope_workspace`, the rollups cover only that workspace's sessions.
    `unit` is day|week|month, `start` YYYY-MM-DD (default today), `days`
    how many periods (counting back from `start`). Missing periods
    materialize on demand; the LLM folds them (deterministic fold when the
    LLM is down)."""
    from datetime import date as _date, timedelta as _td
    from .scope import machine_name
    from .sessionmem import temporal_rollup
    cfg = _cfg_or_load()
    store = get_store()
    machine = scope_machine or machine_name()
    start_d = _date.fromisoformat(start) if start else _date.today()
    step = {"day": 1, "week": 7, "month": 30}.get(unit, 1)
    out = []
    for i in range(max(1, min(days, 40))):
        out.append(temporal_rollup(cfg, store, machine, unit,
                                   start_d + _td(days=step * i), scope_workspace))
    return _j({"machine": machine, "unit": unit, "workspace": scope_workspace,
               "periods": out})


def tool_get_injection(project: str, workspace: str | None = None) -> str:
    """The SessionStart hook package for injection at session start: with
    `workspace` (a DSH cwd), exactly that workspace's session tree —
    apex + mid series + uncovered frontier + raw tail, full summary prose
    at each level. Without a workspace: the project's latest top-of-tree
    session summaries."""
    from .sessionmem import injection_context
    cfg = _cfg_or_load()
    store = get_store()
    return injection_context(cfg, store, project, workspace) \
        or "(injection disabled or empty)"


# --------------------------------------------------------- workspace state

def tool_get_workspace_state(workspace: str) -> str:
    """The workspace's current structured state — open items, problems,
    decisions, conventions, key commands, workflows, checklists, practices
    — as a rendered document (active items by category + what just
    closed). Pass the workspace cwd (the session's working directory)."""
    from .wstate import state_document
    store = get_store()
    return state_document(store, workspace)


def tool_workspace_state_add(category: str, subject: str, body: str,
                             workspace: str,
                             source_session: str | None = None,
                             source_span: str | None = None) -> str:
    """Record (or refresh) one workspace state item. `category`: open_item
    / problem / decision / convention / practice / command / workflow /
    checklist / note (any string accepted). `subject`: the item's key —
    one subject, one spelling; an existing active item with the same
    key is updated in place (supersede-don't-duplicate). Use for things
    you LEARN while working: key commands, conventions, workflows,
    checklists, decisions you made, problems you're carrying. The
    AGENTS.local.md file projection happens only when
    session_state_project_file is on (default: off — the state is
    fetched on demand, not pushed through the instruction channel)."""
    from .wstate import project_state_file
    store = get_store()
    row = store.workspace_state_upsert(
        workspace, category, subject, body,
        source_session=source_session, source_span=source_span)
    path = (project_state_file(workspace, store)
            if _cfg_or_load().session_state_project_file else None)
    return json.dumps({"ok": True, "id": row["id"], "status": row["status"],
                       "updated_file": str(path) if path else None})


def tool_workspace_state_set(state_ids, status: str,
                             body: str | None = None) -> str:
    """Transition workspace state item(s) — bulk: close everything that
    is done in one call. status: resolved (done) / retired (no longer
    relevant) / superseded (replaced by `body` on the same key — single
    id only) / active (re-open) / stale (soft-close, single id only).
    Pass the item ids (memory_get_workspace_state / `memor state show`
    list them)."""
    from .wstate import project_state_file
    ids = (state_ids if isinstance(state_ids, (list, tuple))
           else [state_ids])
    store = get_store()
    rows, errors = [], []
    for i in ids:
        try:
            rows.append(store.workspace_state_set(i, status, body=body))
        except ValueError as e:
            errors.append({"id": i, "error": str(e)})
    path = (project_state_file(rows[0]["workspace"], store)
            if rows and _cfg_or_load().session_state_project_file
            else None)
    out = {"ok": not errors, "n": len(rows),
           "rows": [{"id": r["id"], "status": r["status"]} for r in rows],
           "updated_file": str(path) if path else None}
    if errors:
        out["errors"] = errors
    return json.dumps(out)


# ------------------------------------------------------------------ MCP wrap

def build_server():
    try:
        from fastmcp import FastMCP  # standalone package (fastmcp >= 2)
    except ImportError:
        from mcp.server.fastmcp import FastMCP  # mcp python-sdk <= 1.x

    mcp = FastMCP("memor")

    @mcp.tool()
    def memory_search(query: str, scope_project: str | None = None,
                      scope_component: str | None = None, limit: int = 20,
                      workspace: str | None = None,
                      machine: str | None = None,
                      session: str | None = None,
                      since: str | None = None,
                      until: str | None = None,
                      level: str | None = None,
                      kinds: str | None = None,
                      mode: str | None = None) -> str:
        """Scoped hybrid search over memory: full-text + semantic (embeddings
        of summaries, workspace state, user prompts), rank-fused. Scope axes
        (all optional, ANDed): workspace (a DSH cwd — the usual default),
        project/component (legacy names), machine, session, since/until (ISO
        ts), level ('1' or '1,2'), kinds. mode: hybrid (default) | fts |
        semantic. Hits carry span + scope — the landing spot for
        memory_get_span / memory_drill_down / memory_get_workspace_state."""
        return tool_search(query, scope_project, scope_component, limit,
                           workspace=workspace, machine=machine,
                           session=session, since=since, until=until,
                           level=level, kinds=kinds, mode=mode)

    @mcp.tool()
    def memory_get_current_state(entity: str) -> str:
        """Current facts + decisions for an entity (name, or project:x /
        machine:y / component:z). Includes uncertain and conflicting items."""
        return tool_get_current_state(entity)

    @mcp.tool()
    def memory_get_history(entity: str, limit: int = 50) -> str:
        """Full temporal history for an entity: every state ever recorded,
        current + superseded, with validity windows and reasons."""
        return tool_get_history(entity, limit)

    @mcp.tool()
    def memory_get_state_as_of(entity: str, at: str) -> str:
        """What was true about an entity at a past moment: facts whose
        validity window covers `at` (ISO ts) + the decisions in force
        then. 'What port did the server use when we deployed v3?'
        (memory_get_history is the full chain; this answers one moment.)"""
        return tool_get_state_as_of(entity, at)

    @mcp.tool()
    def memory_get_related(entity: str, depth: int = 1) -> str:
        """Cross-component relationships (runs_on, depends_on, uses...) and
        facts about the related entities."""
        return tool_get_related(entity, depth)

    @mcp.tool()
    def memory_get_decisions(scope_project: str | None = None,
                             scope_component: str | None = None, limit: int = 25) -> str:
        """Decisions with rationale and alternatives considered, newest first."""
        return tool_get_decisions(scope_project, scope_component, limit)

    @mcp.tool()
    def memory_get_evidence(fact_id: str) -> str:
        """Provenance for a fact or decision: the raw events (commands, file
        diffs, service changes) it was derived from. Answers 'why do you
        believe this?'"""
        return tool_get_evidence(fact_id)

    @mcp.tool()
    def memory_get_project_context(project: str, include_evidence: bool = False,
                                   max_chars: int = 0) -> str:
        """Compact catch-me-up package for a project: current state,
        relations, recent decisions, real recent changes (incl. what stopped
        being true), open/uncertain items. include_evidence=true pulls raw
        proof; max_chars caps the size (0 = uncapped)."""
        return tool_get_project_context(project, include_evidence, max_chars)

    @mcp.tool()
    def memory_get_recent_changes(scope_project: str | None = None,
                                  scope_machine: str | None = None,
                                  since_hours: int = 72) -> str:
        """What changed recently: new/superseded facts, event volume by
        source, latest raw events, open review items."""
        return tool_get_recent_changes(scope_project, scope_machine, since_hours)

    @mcp.tool()
    def memory_add_fact(subject: str, claim: str, scope_project: str | None = None,
                        scope_component: str | None = None, kind: str = "fact",
                        confidence: str = "probable", reason: str | None = None,
                        evidence: list[str] | None = None) -> str:
        """Record a fact in memory (kinds: fact|state|configuration|preference|
        problem|lesson|todo). Cite evidence ids from memory_search when you
        have them; without evidence the record is queued for review and
        does not become current. Conflicts with current state are queued,
        never silently overwritten."""
        return tool_add_fact(subject, claim, scope_project, scope_component,
                             kind, confidence, reason, evidence)

    @mcp.tool()
    def memory_add_decision(subject: str, choice: str,
                           scope_project: str | None = None,
                           scope_component: str | None = None,
                           alternatives: list[str] | None = None,
                           reason: str | None = None, confidence: str = "probable",
                           evidence: list[str] | None = None) -> str:
        """Record a decision WITH reason and alternatives. A conflicting
        choice supersedes the prior decision (history kept) or is queued
        when low-confidence."""
        return tool_add_decision(subject, choice, scope_project, scope_component,
                                 alternatives, reason, confidence, evidence)

    @mcp.tool()
    def memory_add_relation(subject: str, relation: str, object: str,
                            evidence: list[str] | None = None) -> str:
        """Record a relationship (runs_on, depends_on, uses, part_of,
        conflicts_with). Evidence ids are required."""
        return tool_add_relation(subject, relation, object, evidence)

    @mcp.tool()
    def memory_add_note(text: str, scope_project: str | None = None,
                        scope_component: str | None = None, kind: str = "fact",
                        confidence: str = "confirmed", reason: str | None = None,
                        evidence: list[str] | None = None) -> str:
        """Quick note or learning (agent equivalent of `memor remember`)."""
        return tool_add_note(text, scope_project, scope_component, kind,
                             confidence, reason, evidence)

    @mcp.tool()
    def memory_correct(fact_id: str, claim: str, reason: str | None = None) -> str:
        """Correct a wrong memory: supersedes it with the corrected claim
        (original row + evidence preserved for audit)."""
        return tool_correct(fact_id, claim, reason)

    @mcp.tool()
    def memory_retract(fact_id: str, reason: str | None = None) -> str:
        """Retract a memory (wrong or obsolete): leaves all 'current'
        surfaces, stays in history with the retraction reason."""
        return tool_retract(fact_id, reason)

    @mcp.tool()
    def memory_get_timeline(entity: str, since_hours: int = 72,
                           limit: int = 200) -> str:
        """Chronological 'how did we get here' view for a project/component/
        machine: raw events + fact transitions + decisions in one line.
        Every entry carries an id for drill-down."""
        return tool_get_timeline(entity, since_hours, limit)

    @mcp.tool()
    def memory_get_recent_episodes(scope_project: str | None = None,
                                   since_hours: int = 168, limit: int = 20) -> str:
        """Work episodes from recent windows: objective, activity sketch,
        outcome, lessons — 'what did we actually work on?'"""
        return tool_get_recent_episodes(scope_project, since_hours, limit)

    @mcp.tool()
    def memory_get_global_context() -> str:
        """Cross-machine Level-0 view: which machines exist, which projects
        and services each carries, where things are uncertain."""
        return tool_get_global_context()

    @mcp.tool()
    def memory_get_span(span: str | None = None, harness: str | None = None,
                        session: str | None = None, start_seq: int | None = None,
                        end_seq: int | None = None, limit: int = 200) -> str:
        """Read the harness's RAW session log for a span (drill-down when a
        summary's digest is not enough): pass `span`
        ('<harness>:<session>:seq[lo..hi]') or harness+session with optional
        seq bounds. Returns budgeted event previews; the complete thinking
        trace stays in the harness log."""
        return tool_get_span(span, harness, session, start_seq, end_seq, limit)

    @mcp.tool()
    def memory_get_summaries(scope_project: str | None = None,
                             session: str | None = None, limit: int = 50,
                             workspace: str | None = None) -> str:
        """Browse the summary trees (project axis, one session, or one
        workspace), oldest first: text, level, span (leaf -> raw data) or
        references (children), status. Drill deeper with memory_drill_down
        / memory_get_span."""
        return tool_get_summaries(scope_project, session, limit, workspace)

    @mcp.tool()
    def memory_drill_down(summary_id: str) -> str:
        """Go one level deeper from a summary: a leaf returns its raw span
        (budgeted harness-log events); a folded node returns its children
        (drill each further); a temporal node returns its day/week children."""
        return tool_drill_down(summary_id)

    @mcp.tool()
    def memory_get_temporal(scope_machine: str | None = None, unit: str = "day",
                            start: str | None = None, days: int = 1,
                            scope_workspace: str | None = None) -> str:
        """Lazy temporal rollups (query-with-caching): that machine's period
        of project summaries across projects (or only `scope_workspace`'s
        sessions), folded day->week->month. unit=day|week|month,
        start=YYYY-MM-DD (default today), days=how many periods. Missing
        periods materialize on demand."""
        return tool_get_temporal(scope_machine, unit, start, days,
                                 scope_workspace)

    @mcp.tool()
    def memory_get_injection(project: str,
                             workspace: str | None = None) -> str:
        """The SessionStart hook package: top-of-tree session summaries +
        current state + open todos/problems + recent decisions, sized for
        injection at session start. Pass `workspace` (a DSH cwd) to lead
        with that workspace's tree."""
        return tool_get_injection(project, workspace)

    @mcp.tool()
    def memory_get_workspace_state(workspace: str) -> str:
        """The workspace's current structured state: open items, problems,
        decisions, conventions, key commands, workflows, checklists,
        practices — active items by category plus what just closed.
        Pass the workspace cwd. Written via memory_workspace_state_add /
        memory_workspace_state_set (or autonomously by the summary
        worker's promotion of each window's decisions/problems/open
        items)."""
        return tool_get_workspace_state(workspace)

    @mcp.tool()
    def memory_workspace_state_add(category: str, subject: str, body: str,
                                   workspace: str,
                                   source_session: str | None = None,
                                   source_span: str | None = None) -> str:
        """Record (or refresh) one workspace state item. `category`:
        open_item / problem / decision / convention / practice / command /
        workflow / checklist / note (any string). `subject`: the item key
        — one subject, one spelling; an active item with the same key is
        updated in place. For things you LEARN while working: key
        commands, conventions, workflows, checklists, decisions, problems
        you're carrying."""
        return tool_workspace_state_add(category, subject, body, workspace,
                                        source_session, source_span)

    @mcp.tool()
    def memory_workspace_state_set(state_ids, status: str,
                                   body: str | None = None) -> str:
        """Transition workspace state item(s) — pass ONE id or a LIST of
        ids (bulk close: resolved / retired). superseded (replaced by
        `body`) and stale take a single id. Keep the state list true:
        close what is done."""
        return tool_workspace_state_set(state_ids, status, body)

    return mcp


def main() -> None:
    m = build_server()
    m.run()  # stdio


if __name__ == "__main__":
    main()