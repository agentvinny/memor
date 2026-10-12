"""Apply candidates: validate evidence, dedup against current state,
auto-apply high-confidence non-conflicting changes, queue the rest for
human review (copy-on-write pattern). Never mutates a fact in place.

Trust model: records from *trusted actors* — humans, and agents/harnesses
when `Config.trust_agent_writes` is on (default) — are deliberate
assertions and become current with provenance (source_type) and confidence
visible, without a human review pass. LLM-derived candidates stay
evidence-gated: uncited ones are dropped, low-confidence ones are queued.
The queue therefore receives only: LLM low-confidence claims,
contradictions (any source), and surprises/recurring conflicts.
"""
from __future__ import annotations

import fcntl
import json
import re
from pathlib import Path

from ..models import Candidate, Scope, normalize_claim, now_utc, slugify
from ..store import Store

AUTO_CONFIDENCES = ("confirmed", "probable")

# Sources that are pure system housekeeping, never real-world evidence:
# `memor` (daemon bookkeeping) and `verify` (A1 verification passes, one
# event every few minutes). They stay in the raw journal but are withheld
# from consolidation/LLM windows so they can't dilute the LLM's context or
# burn queue slots forever.
NON_EVIDENCE_SOURCES = ("memor", "verify")


def _is_trusted(c: Candidate, cfg) -> bool:
    """True for records asserted by a trusted actor: a human, or an
    agent/harness when `Config.trust_agent_writes` is enabled (default).
    Deliberate assertions by these actors become current (provenance and
    confidence remain visible) without a human review pass. LLM-derived
    candidates (source_type "consolidation", origin "llm") are never
    trusted: uncited ones are dropped, low-confidence ones are queued."""
    if c.source_type == "human":
        return True
    return c.source_type == "agent" and getattr(cfg, "trust_agent_writes", True)


def _tokens(s: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", s.lower())


def _similar_claims(a: str, b: str) -> bool:
    """F6: token-overlap test for near-duplicate state claims."""
    ta, tb = set(_tokens(a)), set(_tokens(b))
    if not ta or not tb:
        return False
    return len(ta & tb) / len(ta | tb) >= 0.8


def _tokens(s: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", s.lower())


_CONF_RANK = {"confirmed": 3, "probable": 2, "inferred": 1, "unknown": 0}


def _is_surprise(old: dict, new: dict) -> bool:
    """G3: a memory that was CONFIRMED gets overturned by weaker or
    re-observed evidence — exactly the 'things that may be surprising'
    the vacation test asks to surface."""
    if old.get("source_type") == "verify":
        return False
    if old.get("confidence") != "confirmed":
        return False
    return (new.get("source_type") == "verify"
            or _CONF_RANK.get(new.get("confidence", ""), 0)
            < _CONF_RANK.get(old.get("confidence", ""), 0))


def _queue_surprise(store: Store, subject: str, old: dict, new: dict) -> None:
    pid = store.add_proposal(
        "surprise",
        {"subject": subject, "was": old.get("claim"), "now": new.get("claim"),
         "old_confidence": old.get("confidence"),
         "new_source": new.get("source_type")},
        "auto_applied", note=f"surprise: {old.get('claim', '')[:60]} -> {new.get('claim', '')[:60]}",
    )
    store.queue_review(pid, "surprise",
                       f"{subject}: confirmed \"{(old.get('claim') or '')[:60]}\" "
                       f"-> \"{(new.get('claim') or '')[:60]}\" "
                       f"({new.get('source_type')})")


def _canonical_subject(store: Store, scope: Scope, subject: str) -> str:
    """F5: re-target a subject onto the vocabulary already in use for this
    scope, so `service:qwen` and `Service:Qwen` grow one chain, not two."""
    if not subject:
        return subject
    known = store.known_subjects(scope.project, scope.component)
    if not known:
        return subject
    for k in known:
        if k == subject:
            return k
    target = slugify(subject)
    for k in known:
        if slugify(k) == target:
            return k
    return subject


class ApplyResult:
    def __init__(self) -> None:
        self.inserted: list[str] = []
        self.superseded: list[tuple[str, str]] = []  # (old_id, new_id)
        self.noops: int = 0
        self.queued: list[str] = []
        self.dropped: list[str] = []
        self.decisions: list[str] = []
        self.relations: list[str] = []

    def summary(self) -> str:
        return (f"{len(self.inserted)} new facts, {len(self.superseded)} superseded, "
                f"{len(self.decisions)} decisions, {len(self.relations)} relations, "
                f"{self.noops} unchanged, {len(self.queued)} queued for review, "
                f"{len(self.dropped)} dropped")


def validate_evidence(store: Store, c: Candidate) -> bool:
    # Explicit assertions (human, or a deliberate agent write via the C1
    # tools) stand on their own and are recorded with a confidence ceiling:
    # without evidence they can never become current on their own.
    # LLM/rule candidates must cite evidence that actually exists — the §23
    # "don't trust the LLM as source of truth" rule.
    if c.origin in ("human", "agent"):
        c.evidence = [e for e in c.evidence if store.get_event(e)]
        return True
    ok = [e for e in c.evidence if store.get_event(e)]
    c.evidence = ok
    return bool(ok)


def apply_candidates(store: Store, cfg, candidates: list[Candidate]) -> ApplyResult:
    res = ApplyResult()
    for c in candidates:
        c.scope.machine = c.scope.machine or None
        if not validate_evidence(store, c):
            res.dropped.append(c.claim[:80])
            continue
        if c.kind == "decision":
            # B1: decisions get the same temporal treatment as facts — a
            # reversed choice supersedes the prior one instead of piling up
            project, component = c.scope.project, c.scope.component
            subject = _canonical_subject(store, c.scope, c.subject)
            current = store.current_decision(project, component, subject)
            norm = normalize_claim(c.claim)
            auto = (c.confidence in AUTO_CONFIDENCES and not c.contradiction
                    and c.source_type != "human" and c.evidence)
            trusted = _is_trusted(c, cfg)
            new = {
                "scope_project": project, "scope_component": component,
                "scope_machine": c.scope.machine, "subject": subject,
                "choice": c.claim, "alternatives": c.alternatives,
                "reason": c.reason, "confidence": c.confidence,
                "decided_at": c.valid_from or _evidence_ts(store, c.evidence) or now_utc(),
                "evidence_ids": c.evidence,
                "source_type": c.source_type,
            }
            if current and normalize_claim(current["choice"]) == norm:
                store.add_proposal("decision", c.to_dict(), "no_op",
                                   note=f"reinforces {current['id']}")
                res.noops += 1
                continue
            if current and (c.contradiction or not (auto or trusted)):
                did = store.insert_decision(status="contradictory",
                                            supersedes_id=current["id"], **new)
                pid = store.add_proposal("decision", c.to_dict(), "pending",
                                          note=f"contradicts {current['id']}")
                store.queue_review(pid, "decision_contradiction",
                                   f"{subject}: chose \"{current['choice'][:100]}\" "
                                   f"vs \"{c.claim[:100]}\"")
                res.queued.append(did)
                continue
            if current and (auto or trusted):
                did = store.supersede_decision(current, new)
                store.add_proposal("decision", c.to_dict(), "auto_applied",
                                    note=f"supersedes {current['id']}")
                res.superseded.append((current["id"], did))
                res.decisions.append(did)
                continue
            status = "current" if (auto or trusted) else "unknown"
            did = store.insert_decision(status=status, **new)
            if status == "unknown":
                pid = store.add_proposal("decision", c.to_dict(), "pending",
                                          note="low confidence, unconfirmed")
                store.queue_review(pid, "low_confidence",
                                   f"{subject}: {c.claim[:120]} (confidence {c.confidence})")
                res.queued.append(did)
            else:
                store.add_proposal("decision", c.to_dict(), "auto_applied")
            res.decisions.append(did)
            if c.supersedes:
                if store.get_fact(c.supersedes):
                    store.close_fact(c.supersedes, new["decided_at"] or now_utc())
                elif store.get_decision(c.supersedes):
                    store.close_decision(c.supersedes, new["decided_at"] or now_utc())
            continue
        if c.kind == "relation":
            parts = c.claim.split("|", 1)
            relation = parts[0]
            object_ = parts[1] if len(parts) > 1 else c.subject
            if not relation or not object_:
                continue
            # kind hint for the subject entity; upsert_relation resolves/creates
            # both endpoint entities (B2: relations are keyed on entity ids)
            store.upsert_entity(c.subject, kind="component",
                                project=c.scope.project, machine=c.scope.machine)
            rid = store.upsert_relation(c.subject, relation, object_,
                                        confidence=c.confidence)
            store.add_proposal("relation", c.to_dict(), "auto_applied", note=rid)
            res.relations.append(rid)
            continue

        # ---- facts ------------------------------------------------------
        project, component = c.scope.project, c.scope.component
        subject = _canonical_subject(store, c.scope, c.subject)
        current = store.current_fact(project, component, subject)
        norm = normalize_claim(c.claim)

        if current and normalize_claim(current["claim"]) == norm:
            for e in c.evidence:
                store.add_evidence(current["id"], _source_type(store, e), e)
            store.add_proposal("fact", c.to_dict(), "no_op",
                               note=f"reinforces {current['id']}")
            res.noops += 1
            continue

        # F6: near-duplicate state claims (differing only in incidental
        # detail) must not ping-pong the supersede chain
        if (current and not c.contradiction
                and c.fact_kind == (current.get("kind") or "fact")
                and _similar_claims(norm, normalize_claim(current["claim"]))):
            old_tokens, new_tokens = set(_tokens(normalize_claim(current["claim"]))), \
                set(_tokens(norm))
            if new_tokens > old_tokens:
                refined = {
                    "scope_project": project, "scope_component": component,
                    "scope_machine": c.scope.machine, "subject": subject,
                    "claim": c.claim, "kind": c.fact_kind,
                    "confidence": c.confidence,
                    "valid_from": c.valid_from or _evidence_ts(store, c.evidence) or now_utc(),
                    "reason": c.reason or f"refined by evidence {', '.join(c.evidence[:3])}",
                    "source_type": c.source_type,
                }
                fid = store.supersede(current, refined)
                for e in c.evidence:
                    store.add_evidence(fid, _source_type(store, e), e)
                store.add_proposal("fact", c.to_dict(), "auto_applied",
                                   note=f"refines {current['id']}")
                res.superseded.append((current["id"], fid))
                if _is_surprise(current, refined):
                    _queue_surprise(store, subject, current, refined)
            else:
                for e in c.evidence:
                    store.add_evidence(current["id"], _source_type(store, e), e)
                store.add_proposal("fact", c.to_dict(), "no_op",
                                   note=f"rephrasing of {current['id']}")
                res.noops += 1
            continue

        auto = (c.confidence in AUTO_CONFIDENCES and not c.contradiction
                and c.source_type != "human" and c.evidence)
        trusted = _is_trusted(c, cfg)
        new = {
            "scope_project": project, "scope_component": component,
            "scope_machine": c.scope.machine, "subject": subject,
            "claim": c.claim, "kind": c.fact_kind, "confidence": c.confidence,
            "valid_from": c.valid_from or _evidence_ts(store, c.evidence) or now_utc(),
            "reason": c.reason,
            "source_type": c.source_type,
        }

        if current and (c.contradiction or not (auto or trusted)):
            # record as contradictory; keep old as current; queue for review
            fid = store.insert_fact(status="contradictory",
                                    supersedes_id=current["id"], **new)
            for e in c.evidence:
                store.add_evidence(fid, _source_type(store, e), e)
            pid = store.add_proposal("fact", c.to_dict(), "pending",
                                       note=f"contradicts current {current['id']}")
            store.queue_review(pid, "contradiction",
                               f"{c.subject}: current \"{current['claim'][:100]}\" "
                               f"vs new \"{c.claim[:100]}\"")
            res.queued.append(fid)
            # G3: the same subject fighting itself repeatedly is a signal
            n = store._conn().execute(
                "SELECT COUNT(*) FROM review_queue"
                " WHERE summary LIKE ? AND category='contradiction'",
                (f"%{c.subject}:",)).fetchone()[0]
            if n >= 3:
                sp = store.add_proposal(
                    "surprise", {"subject": c.subject, "recurring": n},
                    "auto_applied", note=f"recurring conflicts on {c.subject}")
                store.queue_review(sp, "surprise",
                                   f"{c.subject}: recurring conflicts ({n} queued)"
                                   " — worth a manual look")
            continue

        if current and (auto or trusted):
            fid = store.supersede(current, new)
            for e in c.evidence:
                store.add_evidence(fid, _source_type(store, e), e)
            pid = store.add_proposal("fact", c.to_dict(), "auto_applied",
                                      note=f"supersedes {current['id']}")
            res.superseded.append((current["id"], fid))
            if _is_surprise(current, new):
                _queue_surprise(store, subject, current, new)
            continue

        # no current fact
        status = "current" if (auto or trusted) else "unknown"
        fid = store.insert_fact(status=status, **new)
        for e in c.evidence:
            store.add_evidence(fid, _source_type(store, e), e)
        if status == "unknown":
            pid = store.add_proposal("fact", c.to_dict(), "pending",
                                      note="low confidence, unconfirmed")
            store.queue_review(pid, "low_confidence",
                               f"{c.subject}: {c.claim[:120]} (confidence {c.confidence})")
            res.queued.append(fid)
        else:
            store.add_proposal("fact", c.to_dict(), "auto_applied")
            res.inserted.append(fid)

        # explicit supersedes hint from the LLM
        if c.supersedes and store.get_fact(c.supersedes):
            store.close_fact(c.supersedes, new["valid_from"] or now_utc())

    return res


def _source_type(store: Store, event_id: str) -> str:
    ev = store.get_event(event_id)
    return (ev or {}).get("source", "unknown")


def _evidence_ts(store: Store, evidence: list[str]) -> str | None:
    """Latest timestamp among cited events — the true 'valid from' moment
    for a derived record (never the time we happened to process it)."""
    best = None
    for eid in evidence:
        ev = store.get_event(eid)
        if not ev or not ev.get("ts"):
            continue
        if best is None or ev["ts"] > best:
            best = ev["ts"]
    return best


def review_accept(store: Store, review_id: str) -> str:
    """Human accepts a queued (contradictory/low-confidence) candidate:
    it becomes current, supersededing the fact it conflicted with."""
    rev = store.get_review(review_id)
    if not rev or rev["status"] != "open":
        return "not open"
    prop = store.get_proposal(rev["proposal_id"])
    if not prop:
        store.resolve_review(review_id, "accepted (proposal missing)")
        return "proposal missing"
    c = Candidate.from_dict(prop["payload"])
    # locate the candidate's row BEFORE creating the new fact, or the claim
    # lookup would match the new row and supersede the wrong one
    cand_id = _find_fact_for_proposal(store, prop)
    old = store.get_fact(c.supersedes or "") if c.supersedes else None
    if old is None:
        old = store.current_fact(c.scope.project, c.scope.component, c.subject)
    new = {
        "scope_project": c.scope.project, "scope_component": c.scope.component,
        "scope_machine": c.scope.machine, "subject": c.subject, "claim": c.claim,
        "kind": c.fact_kind, "confidence": c.confidence,
        "valid_from": now_utc(), "reason": c.reason, "source_type": c.source_type,
    }
    if old:
        fid = store.supersede(old, new)
    else:
        fid = store.insert_fact(status="current", **new)
    if cand_id and cand_id != fid:
        store.set_fact_status(cand_id, "superseded", valid_until=now_utc())
    for e in c.evidence:
        store.add_evidence(fid, _source_type(store, e), e)
    store.resolve_review(review_id, "accepted")
    return fid


def review_reject(store: Store, review_id: str, note: str | None = None) -> None:
    rev = store.get_review(review_id)
    if not rev or rev["status"] != "open":
        return
    prop = store.get_proposal(rev["proposal_id"])
    if prop:
        c = Candidate.from_dict(prop["payload"])
        # mark any contradictory fact row for this subject as unknown
        for f in store.list_facts(status="contradictory", subject=c.subject,
                                  project=c.scope.project, component=c.scope.component):
            store.set_fact_status(f["id"], "unknown")
    store.resolve_review(review_id, note or "rejected")


def _find_fact_for_proposal(store: Store, prop: dict) -> str | None:
    """Locate the fact row created for a proposal (by claim+subject)."""
    c = Candidate.from_dict(prop["payload"])
    for f in store.list_facts(subject=c.subject, project=c.scope.project,
                              component=c.scope.component):
        if normalize_claim(f["claim"]) == normalize_claim(c.claim):
            return f["id"]
    return None


def llm_pending_count(store: Store, since: str | None = None) -> int:
    """How many records still need LLM processing (C3 queue size)."""
    cursor = since or store.meta_get("llm.cursor") or "1970-01-01T00:00:00Z"
    seen = set((store.meta_get("llm.seen") or "").splitlines())
    return len([e for e in store.events_since(cursor, limit=5000)
                if e["id"] not in seen and e["source"] not in NON_EVIDENCE_SOURCES])


def run_llm_sync(store: Store, cfg, *, dry_run: bool = False,
                 since: str | None = None) -> dict:
    """C3: process the LLM-pending queue. The LLM may be down at any time;
    this command (or the daemon's autosync, when enabled) runs it whenever
    it's back. The LLM cursor is independent of the rules cursor, so a
    record is never skipped just because the LLM was offline.

    R1: everything else works with the LLM down
    R2: unprocessed records stay queued (durable llm.cursor/llm.seen)
    R3: explicit `memor llm sync` when the server is online
    R4: availability probed at the configured endpoint first
    R6: findings go through the same validated apply pipeline
    """
    from .extract import extractor_for

    name, llm = extractor_for(cfg)
    pending = llm_pending_count(store)
    if llm is None:
        return {"provider": name, "events": 0, "pending": pending,
                "report": f"LLM not configured ({name}) — {pending} records pending",
                "dry_run": dry_run}
    if not llm.available():
        return {"provider": name, "events": 0, "pending": pending,
                "report": f"LLM unavailable — {pending} records pending",
                "dry_run": dry_run}
    cursor = since or store.meta_get("llm.cursor") or "1970-01-01T00:00:00Z"
    seen = set((store.meta_get("llm.seen") or "").splitlines())
    events = [e for e in store.events_since(cursor, limit=5000)
              if e["id"] not in seen and e["source"] not in NON_EVIDENCE_SOURCES]
    if not events:
        return {"provider": name, "events": 0, "pending": 0,
                "report": "nothing pending", "dry_run": dry_run}

    # E2: bounded window, same semantics as the rules pass
    batch = max(1, int(getattr(cfg, "consolidate_batch", 500)))
    window = events[:batch]
    pending_after = len(events) - len(window)

    groups: dict[tuple, list[dict]] = {}
    for ev in window:
        groups.setdefault((ev.get("project") or None, ev.get("machine") or None), []).append(ev)
    all_cands: list[Candidate] = []
    for (project, machine), evs in sorted(groups.items(), key=lambda kv: str(kv[0])):
        scope = Scope(project=project, machine=machine)
        current = store.list_facts(project=project, machine=machine,
                                   status="current", limit=80)
        try:
            all_cands.extend(llm.extract(scope, evs, current, store))
        except Exception as e:
            print(f"memor: LLM sync error ({type(e).__name__}: {e})", flush=True)
            try:
                store.append_event("memor", "llm_extraction_error",
                                   command=f"llm sync failed ({name})",
                                   project=project, machine=machine,
                                   payload={"error": str(e)[:500]})
            except Exception:
                pass

    result = None if dry_run else apply_candidates(store, cfg, all_cands)
    if not dry_run:
        if pending_after == 0:
            store.meta_set("llm.cursor", max(e["ts"] for e in events))
        seen.update(e["id"] for e in window)
        store.meta_set("llm.seen", "\n".join(list(seen)[-3000:]))
        try:
            from .markdown import regenerate
            regenerate(store, cfg)
        except Exception:
            pass
    report = "dry run" if dry_run else (result.summary() if result else "")
    if pending_after:
        report += f" (+{pending_after} records left in queue)"
    return {"provider": name, "events": len(window),
            "pending": pending_after,
            "groups": {f"{p or '∅'}/{m or '∅'}": len(v) for (p, m), v in groups.items()},
            "candidates": len(all_cands), "dry_run": dry_run,
            "report": report,
            "result": result}


def run_consolidation(
    store: Store, cfg, *, since: str | None = None, extractor: str = "auto",
    scope_project: str | None = None, dry_run: bool = False,
    llm: "ClaudeExtractor | None" = None,
) -> dict:
    """Full pass: read new events, group by scope, extract, apply, return report.

    Serialized with a non-blocking file lock so a daemon run and a manual
    `memor consolidate` cannot race on the cursor / insert duplicate facts.
    A losing caller returns immediately with report='skipped: ...'.
    """
    lock_path = Path(cfg.data_dir) / "consolidate.lock"
    cursor_key = ("consolidate.cursor" if scope_project is None
                  else f"consolidate.cursor.{scope_project}")
    lock_fh = open(lock_path, "a+")
    try:
        fcntl.flock(lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (BlockingIOError, InterruptedError, OSError):
        lock_fh.close()
        return {"events": 0, "since": since or "", "groups": {}, "candidates": 0,
                "dry_run": dry_run, "report": "skipped: another consolidation is in progress",
                "result": None, "cursor": store.meta_get(cursor_key) or since or ""}
    try:
        return _run_consolidation_locked(
            store, cfg, since=since, extractor=extractor,
            scope_project=scope_project, dry_run=dry_run, llm=llm,
        )
    finally:
        fcntl.flock(lock_fh, fcntl.LOCK_UN)
        lock_fh.close()


def _run_consolidation_locked(
    store: Store, cfg, *, since: str | None, extractor: str,
    scope_project: str | None, dry_run: bool, llm,
) -> dict:
    from .rules import extract as rule_extract

    # One cursor per scope: a `--project X` run must not advance the global
    # cursor (that would permanently skip other projects' interleaved events)
    cursor_key = ("consolidate.cursor" if scope_project is None
                  else f"consolidate.cursor.{scope_project}")
    since = since or store.meta_get(cursor_key) or "1970-01-01T00:00:00Z"
    # events_since is inclusive (ts >= since); drop ids already consolidated so
    # same-second events after the cursor are not lost and re-runs are idempotent
    seen = set((store.meta_get("consolidate.seen") or "").splitlines())
    # source="memor" events are the system's own housekeeping (daemon runs,
    # store init, hook installs' audit trail) — never treat them as evidence
    events = [e for e in store.events_since(since, project=scope_project, limit=5000)
              if e["id"] not in seen and e["source"] not in NON_EVIDENCE_SOURCES]
    if not events:
        return {"events": 0, "since": since, "groups": {}, "candidates": 0,
                "dry_run": dry_run, "report": "nothing new", "result": None,
                "cursor": store.meta_get(cursor_key) or since, "pending": 0}

    # E2: bounded window — a busy day is processed in batches; the cursor
    # advances only when the backlog is fully consumed, so no event is ever
    # skipped (the seen-set keeps re-runs idempotent)
    batch = max(1, int(getattr(cfg, "consolidate_batch", 500)))
    window = events[:batch]
    pending_after = len(events) - len(window)

    # group by (project, machine); machine-only events keep project=None
    groups: dict[tuple, list[dict]] = {}
    for ev in window:
        key = (ev.get("project") or None, ev.get("machine") or None)
        if scope_project and ev.get("project") not in (None, scope_project):
            continue
        groups.setdefault(key, []).append(ev)

    all_candidates: list[Candidate] = []
    for (project, machine), evs in sorted(groups.items(), key=lambda kv: str(kv[0])):
        scope = Scope(project=project, machine=machine)
        cands = rule_extract(evs, shell_failures=getattr(cfg, "problem_capture_shell", False))
        use_llm = extractor in ("auto", "llm") and cfg.llm_enabled
        if extractor == "llm":
            use_llm = True
        if use_llm and llm is not None:
            current = store.list_facts(project=project, machine=machine,
                                       status="current", limit=80)
            try:
                cands.extend(llm.extract(scope, evs, current, store))
            except Exception as e:
                print(f"memor: LLM extraction error: {e}", flush=True)
                try:
                    store.append_event("memor", "llm_extraction_error",
                                       command=f"llm extraction failed ({getattr(llm, 'name', 'llm')})",
                                       project=project, machine=machine,
                                       payload={"error": str(e)[:500],
                                                "scope": {"project": project, "machine": machine}})
                except Exception:
                    pass
        all_candidates.extend(cands)

    # component hints: ensure entities exist
    for (project, machine) in groups:
        if project:
            store.upsert_entity(project, kind="project", project=project)
        if machine:
            store.upsert_entity(machine, kind="machine", machine=machine)

    result = (None if dry_run else apply_candidates(store, cfg, all_candidates))
    if not dry_run:
        if pending_after == 0:
            store.meta_set(cursor_key, max(e["ts"] for e in events))
        seen.update(e["id"] for e in window)
        store.meta_set("consolidate.seen", "\n".join(list(seen)[-3000:]))

    report = "dry run" if dry_run else (result.summary() if result else "")
    if pending_after:
        report += f" (+{pending_after} events left in queue)"
    return {
        "events": len(window),
        "pending": pending_after,
        "since": since,
        "groups": {f"{p or '∅'}/{m or '∅'}": len(v) for (p, m), v in groups.items()},
        "candidates": len(all_candidates),
        "dry_run": dry_run,
        "report": report,
        "result": result,
        "cursor": (max(e["ts"] for e in events) if pending_after == 0
                   else store.meta_get(cursor_key) or since),
    }