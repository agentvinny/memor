"""LLM extraction. The LLM only ever PROPOSES structured claims that cite
provided evidence ids; anything uncited is dropped downstream.

Two backends (C3):
- ClaudeExtractor: Anthropic API (ANTHROPIC_API_KEY)
- OpenAICompatibleExtractor: any OpenAI-compatible server
  (`llm_base_url`, e.g. http://127.0.0.1:8080/v1 or
  https://openrouter.ai/api/v1) — no SDK dependency, availability probed
  via GET {base}/models, optional `llm_api_key` auth for hosted endpoints.

Noise: shell/agent commands matching `Config.ignore_command_prefixes`
(pure navigation/usage — cd, ls, …) stay in the raw journal but are
withheld from the LLM's window, so routine churn doesn't have to be
filtered by the model's own judgment. A window that is entirely noise
skips the LLM call itself.
"""
from __future__ import annotations

import json
import os
import re
import urllib.request
from pathlib import Path

from ..config import Config
from ..models import Candidate, Scope, CONFIDENCES
from ..store import Store

PROMPT_VERSION = "v1"

SYSTEM_PROMPT = """You are the consolidation engine of "memor", a durable project/environment memory system.
You receive (1) recent raw evidence events and (2) the current memory state for one scope.
Produce NEW memory records that the evidence supports.

Hard rules:
1. Every record MUST cite at least one evidence id from the provided events in its "evidence" array. No citation -> do not emit the record.
2. Never invent values, reasons, or causation. If the evidence does not state WHY something changed, set reason to null.
3. confidence: "confirmed" = directly observed (a command ran, a file changed, a port bound); "probable" = strong inference; "inferred" = guess. Be honest.
4. If a record conflicts with a current fact and the evidence shows the old value is now obsolete, set "supersedes" to that fact's id. If you are unsure it truly replaces the old fact, set "contradiction": true (it will be queued for human review).
5. Prefer few high-signal records over many. Skip routine noise. Maximum 15 records total.
6. Emit a "decision" record when the evidence shows a deliberate choice was made (include reason and alternatives if visible).
7. Emit "relations" (subject, relation, object) for cross-component links visible in the evidence (runs_on, depends_on, part_of, uses, conflicts_with).
8. In "component_hints", map components to one-line descriptions so the memory hierarchy stays legible.

Respond with ONLY a JSON object, no prose:
{"facts":[{"subject","claim","kind":"fact|state|configuration|preference|problem|lesson|todo","confidence","reason":null,"evidence":[id],"supersedes":null,"contradiction":false}],
 "decisions":[{"subject","choice","reason":null,"alternatives":[],"confidence","evidence":[id]}],
 "relations":[{"subject","relation","object","evidence":[id]}],
 "component_hints":{"<component>":"<one-liner>"}}
"""


def llm_available() -> bool:
    try:
        import anthropic  # noqa: F401
        return True
    except ImportError:
        return False


class ClaudeExtractor:
    name = "anthropic"

    def __init__(self, cfg: Config):
        import anthropic
        self.client = anthropic.Anthropic()
        self.cfg = cfg

    def available(self) -> bool:
        return llm_available() and bool(os.environ.get("ANTHROPIC_API_KEY"))

    def _call(self, user: str, system: str | None = None) -> str:
        try:
            resp = self.client.messages.create(
                model=self.cfg.llm_model,
                max_tokens=self.cfg.llm_max_tokens,
                system=system or SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user}],
            )
        except Exception as e:
            print(f"memor: LLM extraction failed ({type(e).__name__}: {e}); rules-only for this group",
                  flush=True)
            return ""
        if getattr(resp, "stop_reason", None) == "max_tokens":
            return _truncated(self.cfg)
        return "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")

    def extract(self, scope: Scope, events: list[dict],
                current_facts: list[dict], store: Store | None = None) -> list[Candidate]:
        return _extract(self, scope, events, current_facts, store)


class OpenAICompatibleExtractor:
    """LLM served by any OpenAI-compatible HTTP endpoint (local server).
    `cfg.llm_base_url` is the full prefix including /v1."""
    name = "openai-compatible"

    def __init__(self, cfg: Config):
        self.cfg = cfg

    def available(self) -> bool:
        base = (self.cfg.llm_base_url or "").rstrip("/")
        if not base:
            return False
        try:
            with urllib.request.urlopen(base + "/models", timeout=3) as r:
                return getattr(r, "status", 200) == 200
        except Exception:
            return False

    def _call(self, user: str, system: str | None = None) -> str:
        base = self.cfg.llm_base_url.rstrip("/")
        payload = {
            "model": self.cfg.llm_model,
            "max_tokens": self.cfg.llm_max_tokens,
            "messages": [{"role": "system", "content": system or SYSTEM_PROMPT},
                         {"role": "user", "content": user}],
        }
        if getattr(self.cfg, "llm_provider_only", None):
            # OpenRouter provider routing: strict allowlist of upstream
            # slugs (e.g. ["xiaomi"]) — the request fails rather than
            # silently switching to another provider serving the model.
            payload["provider"] = {"only": list(self.cfg.llm_provider_only)}
        body = json.dumps(payload).encode()
        headers = {"Content-Type": "application/json"}
        key = _resolve_api_key(self.cfg)
        if key:
            headers["Authorization"] = f"Bearer {key}"
        req = urllib.request.Request(
            base + "/chat/completions", data=body,
            headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=300) as r:
                data = json.loads(r.read().decode())
            choice = (data.get("choices") or [{}])[0]
            if choice.get("finish_reason") == "length":
                return _truncated(self.cfg)
            return choice["message"]["content"]
        except Exception as e:
            print(f"memor: LLM extraction failed ({type(e).__name__}: {e}); rules-only for this group",
                  flush=True)
            return ""

    def extract(self, scope: Scope, events: list[dict],
                current_facts: list[dict], store: Store | None = None) -> list[Candidate]:
        return _extract(self, scope, events, current_facts, store)


def _resolve_api_key(cfg: Config) -> str:
    """Resolve the optional `llm_api_key`: a literal key, or a path to a file
    containing the key (e.g. the user's OpenRouter key file), read at call
    time so key rotation takes effect without a restart. Empty -> no
    Authorization header (local unauthenticated servers)."""
    v = (cfg.llm_api_key or "").strip()
    if not v:
        return ""
    p = Path(os.path.expanduser(v))
    if p.is_file():
        return p.read_text().strip()
    return v


def _truncated(cfg) -> str:
    """A completion that hit its max_tokens limit is unusable: the JSON is
    cut mid-document, and parsing a cut-off prefix can silently yield a
    partial-but-valid document. Warn loudly and yield no candidates — the
    raw events remain citable evidence, and the rules pass is unaffected.
    Fix: raise Config.llm_max_tokens, or shrink consolidate_batch so each
    window's output fits."""
    print(f"memor: LLM output was TRUNCATED at max_tokens={cfg.llm_max_tokens} — "
          f"this window yields no candidates. Raise llm_max_tokens in memor.json "
          f"or lower consolidate_batch so the output fits.", flush=True)
    return ""


def is_noise_command(command: str, prefixes: list[str]) -> bool:
    """True when the command is pure *usage* of the system (navigation,
    paging, help — see Config.DEFAULT_NOISE_COMMANDS) rather than a change.
    Matching is prefix-with-token-boundary: "ls" catches `ls -la` but not
    `lsblk`; multi-word prefixes ("docker compose") catch their invocations.
    """
    c = (command or "").strip()
    if not c:
        return False
    return any(c == p or c.startswith(p + " ") for p in prefixes if p)


def _withhold_noise(events: list[dict], llm) -> list[dict]:
    """Drop shell/agent/atuin events whose command is on the ignore list.
    They remain raw evidence (citable, searchable) — they just never enter
    the LLM's context. Rules-based extraction is unaffected (it never fires
    on these commands anyway)."""
    prefixes = tuple(getattr(getattr(llm, "cfg", None),
                             "ignore_command_prefixes", None) or ())
    if not prefixes:
        return events
    return [e for e in events
            if not (e.get("source") in ("shell", "agent")
                    and is_noise_command(e.get("command") or "", prefixes))]


def _extract(llm, scope: Scope, events: list[dict],
             current_facts: list[dict], store: Store | None = None) -> list[Candidate]:
    """Shared request/parsing path for both backends."""
    events = _withhold_noise(events, llm)
    if not events:
        return []
    trimmed = []
    for ev in events[:200]:
        p = ev.get("payload", {})
        summary = p.get("output") or p.get("diff") or ""
        if len(summary) > 500:
            summary = summary[:500]
        trimmed.append({
            "id": ev["id"], "ts": ev.get("ts"), "source": ev.get("source"),
            "kind": ev.get("kind"), "command": (ev.get("command") or "")[:300],
            "summary": summary,
        })
    cur = [{"id": f["id"], "subject": f["subject"], "claim": f["claim"][:200],
            "confidence": f["confidence"], "kind": f["kind"]}
           for f in current_facts[:80]]
    user = json.dumps({
        "prompt_version": PROMPT_VERSION,
        "machine": scope.machine, "project": scope.project,
        "component": scope.component,
        "events": trimmed, "current_facts": cur,
    }, ensure_ascii=False, default=str)
    text = llm._call(user)
    if not text:
        return []
    return _extract_from_text(text, scope, [e["id"] for e in trimmed], store)


def _extract_from_text(text: str, scope: Scope, valid_ids: set[str],
                       store: Store | None = None) -> list[Candidate]:
    data = _parse_json(text)
    if data is None:
        return []
    out = _candidates_from(data, scope, valid_ids)
    if store is not None:
        apply_component_hints(store, scope, data)
    return out


def extractor_for(cfg: Config) -> tuple[str, object | None]:
    """(provider name, extractor) for the configured LLM, or (name, None)."""
    if not cfg.llm_enabled:
        return "disabled", None
    if cfg.llm_base_url:
        return "openai-compatible", OpenAICompatibleExtractor(cfg)
    if llm_available() and os.environ.get("ANTHROPIC_API_KEY"):
        return "anthropic", ClaudeExtractor(cfg)
    return "none", None


def apply_component_hints(store: Store, scope: Scope, data: dict) -> None:
    """Register the LLM's component -> one-liner hints as entity descriptions
    so the Markdown hierarchy stays legible (the prompt promises these).
    Existing descriptions are never clobbered."""
    hints = data.get("component_hints") or {}
    if not isinstance(hints, dict):
        return
    for comp, desc in hints.items():
        comp = str(comp)[:80].strip()
        desc = str(desc)[:200].strip()
        if not comp or not desc:
            continue
        ent = store.find_entity(comp)
        if ent and ent.get("description"):
            continue
        store.upsert_entity(comp, kind="component", project=scope.project,
                            machine=scope.machine, description=desc)


def _parse_json(text: str):
    t = text.strip()
    t = re.sub(r"^```(?:json)?\s*", "", t)
    t = re.sub(r"\s*```$", "", t)
    start, end = t.find("{"), t.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        return json.loads(t[start:end + 1])
    except json.JSONDecodeError:
        return None


def _candidates_from(data: dict, scope: Scope, valid_ids: set[str]) -> list[Candidate]:
    out: list[Candidate] = []
    for f in data.get("facts", []):
        evs = [e for e in f.get("evidence", []) if e in valid_ids]
        if not evs:
            continue
        conf = f.get("confidence", "probable")
        out.append(Candidate(
            kind="fact", scope=scope,
            subject=str(f.get("subject", ""))[:120],
            claim=str(f.get("claim", ""))[:500],
            fact_kind=f.get("kind", "fact") if f.get("kind") in
            ("fact", "state", "configuration", "preference", "problem", "lesson", "todo") else "fact",
            confidence=conf if conf in CONFIDENCES else "inferred",
            reason=(str(f.get("reason"))[:500] if f.get("reason") else None),
            evidence=evs,
            supersedes=f.get("supersedes"),
            contradiction=bool(f.get("contradiction", False)),
            origin="llm", prompt_version=PROMPT_VERSION,
        ))
    for d in data.get("decisions", []):
        evs = [e for e in d.get("evidence", []) if e in valid_ids]
        if not evs:
            continue
        out.append(Candidate(
            kind="decision", scope=scope,
            subject=str(d.get("subject", ""))[:120],
            claim=str(d.get("choice", ""))[:500],
            reason=(str(d.get("reason"))[:500] if d.get("reason") else None),
            alternatives=[str(a)[:200] for a in d.get("alternatives", [])[:8]],
            confidence=d.get("confidence", "probable") if d.get("confidence") in CONFIDENCES else "probable",
            evidence=evs, origin="llm", prompt_version=PROMPT_VERSION,
        ))
    for r in data.get("relations", []):
        evs = [e for e in r.get("evidence", []) if e in valid_ids]
        if not evs:
            continue
        out.append(Candidate(
            kind="relation", scope=scope,
            subject=str(r.get("subject", ""))[:80],
            claim=str(r.get("relation", ""))[:40],
            evidence=evs, origin="llm", prompt_version=PROMPT_VERSION,
        ))
        # the object of a relation is carried via the claim tuple; repurpose:
        out[-1].claim = f"{out[-1].claim}|{str(r.get('object', ''))[:80]}"
    return out