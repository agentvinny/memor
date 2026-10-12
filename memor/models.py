"""Core types and small shared helpers for memor."""
from __future__ import annotations

import re
import unicodedata
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone


def new_id(prefix: str, n: int = 8) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:n]}"


def now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_ts(ts: str) -> datetime:
    return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def slugify(name: str) -> str:
    name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    name = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-.")
    return name.lower() or "x"


def normalize_claim(claim: str) -> str:
    c = " ".join(claim.lower().split())
    c = re.sub(r"[.!?;:]+$", "", c)
    return c


CONFIDENCES = ("confirmed", "probable", "inferred", "unknown")
FACT_STATUSES = ("current", "superseded", "contradictory", "unknown", "retracted")
FACT_KINDS = (
    "fact", "state", "configuration", "preference", "problem",
    "lesson", "todo", "decision", "architecture",
)


@dataclass
class Scope:
    project: str | None = None
    component: str | None = None
    machine: str | None = None

    def key(self) -> tuple:
        return (self.project or "", self.component or "", self.machine or "")

    def to_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if v}

    @classmethod
    def from_dict(cls, d: dict | None) -> "Scope":
        d = d or {}
        return cls(d.get("project"), d.get("component"), d.get("machine"))


@dataclass
class Candidate:
    """A proposed memory record from an extractor (rules or LLM).

    `evidence` must cite raw-event ids that actually exist; candidates whose
    evidence all fails validation are dropped (never trust the LLM as the
    source of truth).
    """

    kind: str  # "fact" | "decision" | "relation"
    scope: Scope = field(default_factory=Scope)
    subject: str = ""
    claim: str = ""
    fact_kind: str = "fact"
    confidence: str = "probable"
    status: str = "current"
    reason: str | None = None
    alternatives: list[str] = field(default_factory=list)
    valid_from: str | None = None
    evidence: list[str] = field(default_factory=list)
    supersedes: str | None = None
    contradiction: bool = False
    source_type: str = "consolidation"  # consolidation | human | import | agent
    origin: str = "rules"  # rules | llm | human | agent
    prompt_version: str | None = None  # set on LLM-derived records (C5 audit)

    def to_dict(self) -> dict:
        d = {
            "kind": self.kind,
            "scope": self.scope.to_dict(),
            "subject": self.subject,
            "claim": self.claim,
            "confidence": self.confidence,
            "evidence": list(self.evidence),
            "origin": self.origin,
        }
        if self.prompt_version:
            d["prompt_version"] = self.prompt_version
        if self.kind == "fact":
            d.update(
                fact_kind=self.fact_kind,
                status=self.status,
                reason=self.reason,
                valid_from=self.valid_from,
                supersedes=self.supersedes,
                contradiction=self.contradiction,
            )
        elif self.kind == "decision":
            d.update(
                reason=self.reason,
                alternatives=self.alternatives,
                decided_at=self.valid_from,
            )
        else:  # relation
            d.update(relation=self.claim)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Candidate":
        c = cls(
            kind=d.get("kind", "fact"),
            scope=Scope.from_dict(d.get("scope")),
            subject=d.get("subject", ""),
            claim=d.get("claim", ""),
            confidence=d.get("confidence", "probable"),
            source_type=d.get("source_type", "consolidation"),
            origin=d.get("origin", "llm"),
        )
        c.fact_kind = d.get("fact_kind", "fact")
        c.status = d.get("status", "current")
        c.reason = d.get("reason")
        c.alternatives = list(d.get("alternatives", []))
        c.valid_from = d.get("valid_from") or d.get("decided_at")
        c.evidence = list(d.get("evidence", []))
        c.supersedes = d.get("supersedes")
        c.contradiction = bool(d.get("contradiction", False))
        return c