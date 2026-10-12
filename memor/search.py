"""Scoped hybrid search (R3): composable scope axes over FTS and the
semantic index, fused with RRF.

Scope axes (all optional, all ANDed; `workspace` defaults to the
caller's cwd in the CLI): workspace, project, component, machine,
session, time window (since/until), summary levels, kinds. Every hit
carries its span/scope — the landing spot for drill-down.

Targets:
  FTS:       raw events, facts, decisions, summaries (+ entities by name)
  semantic:  summaries + active workspace state + user prompts
             (NOT raw tool output) — embedded lazily on first use and
             cached in the `embeddings` table.

`mode`: `fts` | `semantic` | `hybrid` (default — FTS ∪ semantic,
reciprocal-rank-fusioned; falls back to FTS with a note when no
embedder is available).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, fields
from typing import Any

from .embed import Embedder, get_embedder
from .store import Store


@dataclass
class Scope:
    workspace: str | None = None
    project: str | None = None
    component: str | None = None
    machine: str | None = None
    session: str | None = None
    since: str | None = None
    until: str | None = None
    levels: list[int] | None = None          # summaries only
    kinds: set[str] | None = None            # {"event","fact","decision",
                                            #  "summary","state","prompt","entity"}


def scope_from_args(args) -> Scope:
    """Build a Scope from CLI args (None/absent = axis not set)."""
    def opt(name, attr=None):
        v = getattr(args, attr or name, None)
        return v if v not in (None, "") else None
    levels = None
    lv = opt("level")
    if lv is not None:
        try:
            levels = [int(x) for x in str(lv).split(",") if x.strip()]
        except ValueError:
            levels = None
    kinds = None
    ks = opt("kinds")
    if ks:
        kinds = {k.strip() for k in str(ks).split(",") if k.strip()}
    return Scope(
        workspace=opt("workspace"), project=opt("project"),
        component=opt("component"), machine=opt("machine"),
        session=opt("session"), since=opt("since"), until=opt("until"),
        levels=levels, kinds=kinds)


def scope_fields() -> set[str]:
    return {f.name for f in fields(Scope)}


# ------------------------------------------------------------------ fts

def _fts_hits(store: Store, query: str, scope: Scope,
              limit: int) -> list[dict]:
    """The existing FTS surfaces, scope-filtered, one unified hit list."""
    kinds = scope.kinds
    hits: list[dict] = []

    def keep(k: str) -> bool:
        return not kinds or k in kinds

    if keep("event"):
        for e in store.search_events(query, limit=limit * 2):
            if scope.project and e.get("project") not in (None, scope.project):
                continue
            if scope.machine and e.get("machine") != scope.machine:
                continue
            if scope.session and e.get("session") != scope.session:
                continue
            if scope.workspace and e.get("cwd") != scope.workspace:
                continue
            if scope.since and e["ts"] < scope.since:
                continue
            if scope.until and e["ts"] > scope.until:
                continue
            hits.append(_hit("event", e["id"], e.get("command", "")[:300],
                             ts=e["ts"], session=e.get("session"),
                             machine=e.get("machine"),
                             workspace=e.get("cwd"), project=e.get("project")))
    if keep("fact"):
        for f in store.search_facts(query, limit=limit * 2):
            if scope.project and f.get("scope_project") not in (None, scope.project):
                continue
            if scope.component and f.get("scope_component") != scope.component:
                continue
            hits.append(_hit(
                "fact", f["id"],
                f"{f.get('subject', '')} — {f.get('claim', '')}"[:300],
                ts=f.get("created_at")))
    if keep("decision"):
        for d in store.search_decisions(query, limit=limit):
            hits.append(_hit(
                "decision", d["id"],
                f"{d.get('subject', '')} — {d.get('choice', '')}"[:300],
                ts=d.get("decided_at")))
    if keep("summary"):
        for s in store.search_summaries(query, limit=limit * 2):
            if not _summary_in_scope(s, scope):
                continue
            hits.append(_hit(
                "summary", s["id"], s["text"][:300],
                ts=s["ts"], span=s.get("span"), level=s.get("level"),
                session=s.get("session"), machine=s.get("machine"),
                workspace=s.get("workspace"), project=s.get("project")))
    if keep("entity"):
        for e in store.list_entities():
            if (query.lower() in e["name"].lower()
                    or query.lower() in (e.get("aliases") or "")):
                hits.append(_hit("entity", e["id"], e["name"]))
    return hits[:limit]


def _summary_in_scope(s: dict, scope: Scope) -> bool:
    if scope.workspace and s.get("workspace") != scope.workspace:
        return False
    if scope.project and s.get("project") not in (None, scope.project):
        return False
    if scope.session and s.get("session") != scope.session:
        return False
    if scope.machine and s.get("machine") != scope.machine:
        return False
    if scope.levels and s.get("level") not in scope.levels:
        return False
    if scope.since and s["ts"] < scope.since:
        return False
    if scope.until and s["ts"] > scope.until:
        return False
    return True


def _hit(kind: str, rid: str, text: str, **meta: Any) -> dict:
    return {"kind": kind, "id": rid, "text": text, "via": "fts",
            **{k: v for k, v in meta.items() if v is not None}}


# -------------------------------------------------------------- semantic

def _semantic_hits(store: Store, emb: Embedder, query: str, scope: Scope,
                   limit: int) -> list[dict]:
    import numpy as np
    kinds = scope.kinds
    qvec = _l2(np.array(emb.embed([query])[0], dtype=np.float32))
    hits: list[dict] = []

    def rank(kind: str, rows: list[dict], texts: dict, base: dict):
        if not rows:
            return
        have = ensure_cached_embeddings(store, emb, kind, texts)
        mat = np.vstack([np.frombuffer(have[r["id"]], dtype=np.float32)
                         for r in rows])
        cos = mat @ qvec
        order = np.argsort(-cos)[:limit]
        for o in order:
            r = rows[int(o)]
            h = _hit(kind, r["id"], texts[r["id"]][:300],
                     score=float(cos[int(o)]), via="semantic",
                     **base(r))
            hits.append(h)

    if not kinds or "summary" in kinds:
        rows = _summary_rows(store, scope, limit * 3)
        texts = {r["id"]: r["text"] for r in rows}
        rank("summary", rows, texts,
             lambda r: {"span": r.get("span"), "level": r.get("level"),
                       "session": r.get("session"),
                       "workspace": r.get("workspace"),
                       "project": r.get("project"), "ts": r["ts"]})
    if not kinds or "state" in kinds:
        rows = _state_rows(store, scope)
        texts = {r["id"]: f"{r['subject']} — {r['body']}" for r in rows}
        rank("state", rows, texts,
             lambda r: {"category": r.get("category"),
                        "subject": r.get("subject"),
                        "workspace": r.get("workspace"),
                        "ts": r.get("updated_at")})
    if not kinds or "prompt" in kinds:
        rows = _prompt_rows(store, scope, limit * 3)
        texts = {r["id"]: r["_text"] for r in rows}
        rank("prompt", rows, texts,
             lambda r: {"session": r.get("session"),
                        "workspace": r.get("cwd"),
                        "project": r.get("project"), "ts": r["ts"],
                        "command": r.get("command")})
    return hits[:limit]


def _summary_rows(store: Store, scope: Scope, limit: int) -> list[dict]:
    where, params = ["1=1"], []
    if scope.workspace:
        where.append("workspace=?"); params.append(scope.workspace)
    if scope.project:
        where.append("(project IS NULL OR project=?)"); params.append(scope.project)
    if scope.session:
        where.append("session=?"); params.append(scope.session)
    if scope.machine:
        where.append("machine=?"); params.append(scope.machine)
    if scope.levels:
        where.append("level IN (" + ",".join("?" * len(scope.levels)) + ")")
        params += scope.levels
    if scope.since:
        where.append("ts>=?"); params.append(scope.since)
    if scope.until:
        where.append("ts<=?"); params.append(scope.until)
    rows = store._conn().execute(
        f"SELECT id, ts, machine, session, axis, project, workspace, level,"
        f" span, text FROM summaries WHERE {' AND '.join(where)}"
        f" ORDER BY ts DESC LIMIT ?", [*params, limit]).fetchall()
    return [dict(r) for r in rows]


def _state_rows(store: Store, scope: Scope) -> list[dict]:
    where, params = ["status='active'"], []
    if scope.workspace:
        where.append("workspace=?"); params.append(scope.workspace)
    rows = store._conn().execute(
        f"SELECT id, workspace, category, subject, body, updated_at"
        f" FROM workspace_state WHERE {' AND '.join(where)}"
        f" LIMIT 200", params).fetchall()
    return [dict(r) for r in rows]


def _prompt_rows(store: Store, scope: Scope, limit: int) -> list[dict]:
    where, params = ["source='session'", "kind='prompt'"], []
    if scope.session:
        where.append("session=?"); params.append(scope.session)
    if scope.workspace:
        where.append("cwd=?"); params.append(scope.workspace)
    if scope.project:
        where.append("(project IS NULL OR project=?)"); params.append(scope.project)
    if scope.since:
        where.append("ts>=?"); params.append(scope.since)
    if scope.until:
        where.append("ts<=?"); params.append(scope.until)
    rows = store._conn().execute(
        f"SELECT id, ts, session, cwd, project, command, payload"
        f" FROM raw_events WHERE {' AND '.join(where)}"
        f" ORDER BY ts DESC LIMIT ?", [*params, limit]).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        text = ""
        try:
            text = str(json.loads(d["payload"]).get("prompt", ""))
        except Exception:  # noqa: BLE001
            text = d.get("command") or ""
        if text:
            d["_text"] = text
            out.append(d)
    return out


def ensure_cached_embeddings(store: Store, emb, kind: str,
                             texts: dict[str, str]) -> dict[str, bytes]:
    """Ensure every ref_id in `texts` (ref_id -> embeddable text) has a
    cached embedding; return {ref_id: packed float32 bytes}. Missing ones
    are embedded in one batch and stored. Shared by the search leg and the
    P1 promotion-merge backstop (same kind/text convention: state rows are
    always embedded as `subject — body`)."""
    if not texts:
        return {}
    import numpy as np
    have = store.get_embeddings(kind, list(texts))
    missing = [i for i in texts if i not in have]
    if missing:
        vecs = emb.embed([texts[m] for m in missing])
        for i, v in zip(missing, vecs):
            p = _pack(_l2(np.array(v, dtype=np.float32)))
            store.upsert_embedding(kind, i, p, emb.dim)
            have[i] = p
    return have


def _l2(v):
    """L2-normalize (zero vector survives as-is), so a matrix product is
    a cosine regardless of the provider's embedding scale."""
    import numpy as np
    n = float(np.linalg.norm(v))
    return v / n if n > 0 else v


def _pack(vec) -> bytes:
    import numpy as np
    return np.array(vec, dtype=np.float32).tobytes()


# ------------------------------------------------------------------ fuse

def _rrf(lists: list[list[dict]], k: int = 60) -> list[dict]:
    """Reciprocal rank fusion over ranked hit lists (each list is
    independently ranked; scores are rank-based, so FTS and cosine
    magnitudes never interfere)."""
    score: dict[tuple, float] = {}
    meta: dict[tuple, dict] = {}
    via: dict[tuple, set] = {}
    for lst in lists:
        for rank, h in enumerate(lst):
            key = (h["kind"], h["id"])
            score[key] = score.get(key, 0) + 1 / (k + rank + 1)
            via.setdefault(key, set()).add(h["via"])
            meta[key] = h
    out = []
    for key, s in sorted(score.items(), key=lambda kv: -kv[1]):
        h = dict(meta[key])
        h["score"] = round(s, 6)
        h["via"] = "+".join(sorted(via[key]))
        out.append(h)
    return out


# --------------------------------------------------------------- public

def run_search(store: Store, cfg, query: str, scope: Scope,
               mode: str = "hybrid", limit: int = 10) -> dict:
    """The unified search. Returns {"hits": [...ranked...], "note": ...}.
    `mode`: hybrid (default) | fts | semantic. Hybrid with no embedder
    degrades to FTS with an explanatory note (the FTS path never fails
    because of the semantic one)."""
    mode = (mode or "hybrid").lower()
    note = None
    if mode == "fts":
        hits = _fts_hits(store, query, scope, limit)
    elif mode == "semantic":
        emb = get_embedder(cfg)
        if emb is None:
            return {"hits": [], "note": "semantic mode needs an embedder "
                                        "(embed_provider=off or unavailable)"}
        hits = _semantic_hits(store, emb, query, scope, limit)
    else:  # hybrid
        fts = _fts_hits(store, query, scope, limit)
        emb = get_embedder(cfg)
        if emb is None:
            hits = fts
            note = ("hybrid degraded to FTS: no embedder available "
                    "(embed_provider=off or init failed)")
        else:
            sem = _semantic_hits(store, emb, query, scope, limit)
            hits = _rrf([fts, sem])[:limit] if (fts or sem) else []
    hits = hits[:limit]
    out = {"hits": hits}
    if note:
        out["note"] = note
    return out


def search_mcp(store: Store, cfg, query: str, *, project: str | None = None,
               component: str | None = None, machine: str | None = None,
               workspace: str | None = None, session: str | None = None,
               since: str | None = None, until: str | None = None,
               level: str | None = None, kinds: str | None = None,
               mode: str | None = None, limit: int = 10) -> dict:
    """Scope from MCP-style kwargs (strings, empty = unset)."""
    lv = None
    if level:
        try:
            lv = [int(x) for x in str(level).split(",") if x.strip()]
        except ValueError:
            lv = None
    ks = None
    if kinds:
        ks = {k.strip() for k in str(kinds).split(",") if k.strip()}
    scope = Scope(project=project, component=component, machine=machine,
                 workspace=workspace, session=session, since=since,
                 until=until, levels=lv, kinds=ks)
    return run_search(store, cfg, query, scope, mode or "hybrid", limit)
