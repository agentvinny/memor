"""A3: episode/task memory. A completed work window (objective → activity →
outcome → lessons) is condensed into one durable record so "what did I work
on?" is a query, not an archaeology dig. Summaries are rule-based and
deterministic (no provider dependency); the raw events stay in the store as
evidence for every claim in the sketch."""
from __future__ import annotations

from collections import Counter

from ..models import now_utc
from ..store import Store


def _sketch(events: list[dict], created: list[dict], changed: list[dict],
            decisions: list[dict]) -> str:
    parts = []
    by_source = Counter(e.get("source") or "?" for e in events)
    parts.append(f"{len(events)} events ({', '.join(f'{k}:{v}' for k, v in by_source.most_common(5))})")
    cmds = Counter((e.get("command") or "").strip()[:80]
                   for e in events if (e.get("command") or "").strip())
    top = [c for c, _ in cmds.most_common(3)]
    if top:
        parts.append("top activity: " + " | ".join(top))
    if created:
        parts.append(f"{len(created)} new standing facts (e.g. {created[0]['claim'][:120]})")
    if changed:
        parts.append(f"{len(changed)} state changes along the way")
    if decisions:
        parts.append(f"{len(decisions)} decisions (e.g. {decisions[0]['choice'][:120]})")
    return " · ".join(parts)


def close_episode(store: Store, cfg, project: str | None = None) -> dict:
    """Condense the not-yet-episodes work window (per `episode.cursor`)
    into an episode record and advance the cursor. Idempotent when there is
    nothing new."""
    cursor = store.meta_get("episode.cursor") or "1970-01-01T00:00:00Z"
    seen = set((store.meta_get("episode.seen") or "").splitlines())
    # seen-set backstop: events sharing the cursor's second must not be
    # re-episodesed on the next close
    events = [e for e in store.events_since(cursor, project=project, limit=2000)
              if e["id"] not in seen and e["source"] != "memor"]
    if not events:
        return {"report": "nothing to summarize", "episode": None}

    started, ended = events[0]["ts"], events[-1]["ts"]
    machine = next((e.get("machine") for e in events if e.get("machine")), None)
    proj = project or next((e.get("project") for e in events if e.get("project")), None)
    session = next((e.get("session") for e in events if e.get("session")), None)
    objective = next(((e.get("command") or "").strip()[:160]
                      for e in events if (e.get("command") or "").strip()), None)

    facts = store.list_facts(project=proj, limit=500)
    in_window = [f for f in facts
                 if (started <= (f.get("valid_from") or "") <= ended)
                 or (f.get("valid_until") and started <= f["valid_until"] <= ended)]
    created = [f for f in in_window if f["status"] == "current"]
    changed = [f for f in in_window if f["status"] in ("superseded", "retracted")]
    decs = [d for d in store.list_decisions(project=proj, limit=50)
            if started <= (d.get("decided_at") or "") <= ended]

    lessons = [f["claim"][:200] for f in in_window
               if f["kind"] == "problem" and f["status"] == "current"][:5]
    lessons += [r["summary"][:200] for r in store.open_reviews()
                if r["category"] in ("contradiction", "decision_contradiction")][:5]

    summary = _sketch(events, created, changed, decs)
    eid = store.insert_episode(
        project=proj, machine=machine, session=session,
        started_at=started, ended_at=ended, objective=objective,
        summary=summary, lessons=lessons,
        event_ids=[e["id"] for e in events[:500]],
    )
    store.meta_set("episode.cursor", ended)
    seen.update(e["id"] for e in events)
    store.meta_set("episode.seen", "\n".join(list(seen)[-3000:]))
    return {"report": (f"episode {eid}: {started} → {ended}, "
                       f"{len(events)} events, project={proj or '∅'}"),
            "episode": eid}