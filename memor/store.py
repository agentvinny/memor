"""SQLite store: raw events (Level 4), facts/decisions/evidence (Level 3),
entities/relations, proposals + review queue (copy-on-write consolidation).

Temporal discipline (mirrors Graphiti invalid_at / XTDB bitemporal idea with
plain columns): a row is never mutated in place to change truth. A new truth
closes the previous row (status -> superseded, valid_until set) and is inserted
as a new row whose supersedes_id points back. A→B→A preserves all three rows.
"""
from __future__ import annotations

import json
import re
import sqlite3
import threading
from datetime import datetime, timedelta
from pathlib import Path

from .models import new_id, now_utc

REDACT_PATTERNS = [
    ("pem_key", re.compile(
        r"-----BEGIN[A-Z ]*PRIVATE KEY-----[\s\S]*?-----END[A-Z ]*PRIVATE KEY-----")),
    ("aws_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("bearer", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9\-_\.=+/]{16,}")),
    ("secret_assignment", re.compile(
        r"(?i)\b(api[_\-]?key|secret|token|passw(or)?d|authorization)\b"
        r"\s*[:=]\s*['\"]?[A-Za-z0-9\-_\.=+/]{8,}")),
]


def _word_and(columns: tuple[str, ...], text: str) -> tuple[list[str], list[str]]:
    """Per-word AND over LIKE: a row matches when EVERY query word appears
    (as substring) in at least one of the columns. Strictly a superset of
    whole-phrase LIKE — same behavior for single-word queries."""
    words = [w for w in text.lower().split() if len(w) > 1][:8]
    where, params = [], []
    for w in words:
        p = f"%{w}%"
        where.append("(" + " OR ".join(f"{c} LIKE ?" for c in columns) + ")")
        params.extend([p] * len(columns))
    return where, params


def redact_text(text: str, extra: list[tuple[str, re.Pattern]] = ()) -> tuple[str, list[str]]:
    """Replace known secret shapes with `[REDACTED:<type>]`; returns the
    cleaned text and the list of types that fired (empty when clean)."""
    if not text:
        return text, []
    out = text
    fired: list[str] = []
    for name, pat in (*REDACT_PATTERNS, *extra):
        out, n = pat.subn(f"[REDACTED:{name}]", out)
        if n:
            fired.append(name)
    return out, fired


def _redact_deep(value, extra: list) -> tuple[object, list[str]]:
    """Redact every string in a (possibly nested) JSON-ish structure."""
    fired: list[str] = []
    if isinstance(value, str):
        value, t = redact_text(value, extra)
        fired.extend(t)
    elif isinstance(value, dict):
        for k in list(value.keys()):
            v, t = _redact_deep(value[k], extra)
            value[k] = v
            fired.extend(t)
    elif isinstance(value, (list, tuple)):
        out_list = []
        for v in value:
            nv, t = _redact_deep(v, extra)
            out_list.append(nv)
            fired.extend(t)
        value = out_list
    return value, fired

SCHEMA = """
CREATE TABLE IF NOT EXISTS raw_events (
  id TEXT PRIMARY KEY,
  ts TEXT NOT NULL,
  source TEXT NOT NULL,
  kind TEXT NOT NULL,
  machine TEXT,
  project TEXT,
  cwd TEXT,
  session TEXT,
  command TEXT,
  payload TEXT,
  captured_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_events_ts ON raw_events(ts);
CREATE INDEX IF NOT EXISTS ix_events_source ON raw_events(source, kind);
CREATE INDEX IF NOT EXISTS ix_events_project ON raw_events(project);
CREATE INDEX IF NOT EXISTS ix_events_session ON raw_events(session);

CREATE TABLE IF NOT EXISTS facts (
  id TEXT PRIMARY KEY,
  scope_project TEXT,
  scope_component TEXT,
  scope_machine TEXT,
  subject TEXT NOT NULL,
  claim TEXT NOT NULL,
  kind TEXT NOT NULL DEFAULT 'fact',
  status TEXT NOT NULL DEFAULT 'current',
  confidence TEXT NOT NULL DEFAULT 'probable',
  valid_from TEXT,
  valid_until TEXT,
  reason TEXT,
  supersedes_id TEXT REFERENCES facts(id),
  source_type TEXT DEFAULT 'consolidation',
  created_at TEXT,
  updated_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_facts_scope ON facts(scope_project, scope_component, subject, status);
CREATE INDEX IF NOT EXISTS ix_facts_sup ON facts(supersedes_id);

CREATE TABLE IF NOT EXISTS evidence (
  id TEXT PRIMARY KEY,
  fact_id TEXT REFERENCES facts(id),
  source_type TEXT,
  pointer TEXT,
  note TEXT,
  captured_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_evidence_fact ON evidence(fact_id);

CREATE TABLE IF NOT EXISTS decisions (
  id TEXT PRIMARY KEY,
  scope_project TEXT,
  scope_component TEXT,
  scope_machine TEXT,
  subject TEXT,
  choice TEXT,
  alternatives_considered TEXT,
  reason TEXT,
  confidence TEXT,
  decided_at TEXT,
  evidence_ids TEXT,
  status TEXT NOT NULL DEFAULT 'current',
  valid_until TEXT,
  supersedes_id TEXT REFERENCES decisions(id),
  source_type TEXT DEFAULT 'consolidation',
  created_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_decisions_scope ON decisions(scope_project, scope_component);

CREATE TABLE IF NOT EXISTS entities (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  name_key TEXT NOT NULL UNIQUE,
  kind TEXT,
  project TEXT,
  machine TEXT,
  aliases TEXT,
  description TEXT
);

CREATE TABLE IF NOT EXISTS relations (
  id TEXT PRIMARY KEY,
  subject TEXT NOT NULL,
  relation TEXT NOT NULL,
  object TEXT NOT NULL,
  subject_ent TEXT,
  object_ent TEXT,
  valid_from TEXT,
  valid_until TEXT,
  confidence TEXT DEFAULT 'probable',
  note TEXT
);
CREATE INDEX IF NOT EXISTS ix_relations_sub ON relations(subject);
CREATE INDEX IF NOT EXISTS ix_relations_obj ON relations(object);

CREATE TABLE IF NOT EXISTS proposals (
  id TEXT PRIMARY KEY,
  ts TEXT,
  kind TEXT,
  payload TEXT,
  status TEXT,
  decided_by TEXT,
  note TEXT
);

CREATE TABLE IF NOT EXISTS review_queue (
  id TEXT PRIMARY KEY,
  ts TEXT,
  proposal_id TEXT,
  category TEXT,
  summary TEXT,
  status TEXT DEFAULT 'open',
  resolution TEXT
);
CREATE INDEX IF NOT EXISTS ix_review_status ON review_queue(status);

CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY,
  value TEXT
);
CREATE TABLE IF NOT EXISTS snapshots (
  key TEXT PRIMARY KEY,
  payload TEXT,
  updated_at TEXT
);
CREATE TABLE IF NOT EXISTS episodes (
  id TEXT PRIMARY KEY,
  project TEXT,
  machine TEXT,
  session TEXT,
  started_at TEXT,
  ended_at TEXT,
  objective TEXT,
  summary TEXT,
  lessons TEXT,
  event_ids TEXT,
  created_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_episodes_proj ON episodes(project, started_at);

-- Session-memory (SESSION-MEMORY.md): hierarchical summary trees.
-- Level 1 = one session window, addressed by a span into the harness's
-- raw session log; level 2+ = summary-of-summaries via references.
-- axis="temporal" marks lazy day/week/month rollups (period set).
CREATE TABLE IF NOT EXISTS summaries (
  id TEXT PRIMARY KEY,
  ts TEXT NOT NULL,
  machine TEXT,
  session TEXT,
  harness TEXT,
  axis TEXT NOT NULL DEFAULT 'project',
  project TEXT,
  workspace TEXT,
  component TEXT,
  level INTEGER NOT NULL DEFAULT 1,
  parent_id TEXT REFERENCES summaries(id),
  period TEXT,
  span TEXT,
  refs TEXT,
  text TEXT NOT NULL,
  subjects TEXT,
  model TEXT,
  tokens_in INTEGER,
  tokens_out INTEGER,
  status TEXT NOT NULL DEFAULT 'current',
  created_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_summaries_project ON summaries(project, level, ts);
CREATE INDEX IF NOT EXISTS ix_summaries_session ON summaries(session, level);
CREATE INDEX IF NOT EXISTS ix_summaries_parent ON summaries(parent_id);
CREATE INDEX IF NOT EXISTS ix_summaries_axis ON summaries(axis, period, ts);

-- Workspace state (session-memory): the workspace-scoped, structured
-- "what is true right now" surface — open items, problems, decisions,
-- conventions, key commands, workflows, checklists, practices, notes.
-- The live view is `status='active'`; superseded/retired rows stay for
-- history (the file projection and `memor state show` render the current
-- set only). Written by the agent as-you-go (MCP/CLI), by the flush
-- worker (promotion of a window's decisions/problems/open_items), and by
-- hand via the CLI. One subject per category per workspace (the upsert
-- key); a re-promoted subject updates the active row in place.
CREATE TABLE IF NOT EXISTS workspace_state (
  id TEXT PRIMARY KEY,
  workspace TEXT NOT NULL,
  category TEXT NOT NULL,
  subject TEXT NOT NULL,
  body TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'active',
  source_session TEXT,
  source_span TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  superseded_by TEXT,
  rev INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS ix_wstate_current ON workspace_state(workspace, status, category);
CREATE INDEX IF NOT EXISTS ix_wstate_key ON workspace_state(workspace, category, subject);
CREATE TABLE IF NOT EXISTS embeddings (
  kind TEXT NOT NULL,
  ref_id TEXT NOT NULL,
  dim INTEGER NOT NULL,
  vector BLOB NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (kind, ref_id)
);

-- LLM exchange audit (session-memory): the exact prompt sent to the
-- summarizer and the raw response it returned, one row per call
-- (window / chain / temporal mode). Review surface: `memor window-review`.
CREATE TABLE IF NOT EXISTS llm_calls (
  id TEXT PRIMARY KEY,
  ts TEXT NOT NULL,
  machine TEXT,
  session TEXT,
  span TEXT,
  mode TEXT NOT NULL,
  prompt TEXT NOT NULL,
  raw_response TEXT,
  model TEXT,
  ok INTEGER NOT NULL DEFAULT 0,
  created_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_llm_calls_session ON llm_calls(session, ts);

-- One-shot injection marker: the session-tree envelope was delivered to
-- this session's conversation at its first turn (UserPromptSubmit hook).
-- The conversation history keeps it, so it is never re-injected — even
-- after a host restart.
CREATE TABLE IF NOT EXISTS envelope_delivered (
  session TEXT PRIMARY KEY,
  delivered_at TEXT NOT NULL
);
"""

FTS_TABLE = "raw_events_fts"
FTS_TRIGGER = """
CREATE TRIGGER IF NOT EXISTS raw_events_ai AFTER INSERT ON raw_events BEGIN
  INSERT INTO raw_events_fts(rowid, command, payload)
  VALUES (new.rowid, new.command, new.payload);
END;
"""


def data_dir_size(data_dir: Path) -> int:
    """Total bytes used by the store directory (SQLite + WAL, JSONL event
    mirror, Markdown projection, git repo). Vanished/unreadable files are
    skipped; the result is a best-effort footprint number for size alerts."""
    total = 0
    root = Path(data_dir)
    try:
        for f in root.rglob("*"):
            try:
                if f.is_file():
                    total += f.stat().st_size
            except OSError:
                continue
    except OSError:
        pass
    return total


class Store:
    def __init__(self, data_dir: str | Path):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        (self.data_dir / "events").mkdir(exist_ok=True)
        self.db_path = self.data_dir / "memor.db"
        self._local = threading.local()
        self.fts = True
        self.redact = True
        self.redact_extra: list[tuple[str, re.Pattern]] = []
        self._load_redact_cfg()
        self._init_db()

    def _load_redact_cfg(self) -> None:
        """Redaction is on by default; memor.json can disable it or add
        custom patterns (secret shapes vary per environment)."""
        f = self.data_dir / "memor.json"
        if not f.exists():
            return
        try:
            d = json.loads(f.read_text())
        except (OSError, json.JSONDecodeError):
            return
        self.redact = bool(d.get("redact", True))
        for pat in d.get("redact_extra_patterns", []) or []:
            try:
                self.redact_extra.append(("custom", re.compile(pat)))
            except re.error:
                continue

    def _conn(self) -> sqlite3.Connection:
        c = getattr(self._local, "conn", None)
        if c is None:
            c = sqlite3.connect(self.db_path, timeout=30, check_same_thread=False)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA foreign_keys=ON")
            self._local.conn = c
        return c

    def _init_db(self) -> None:
        c = self._conn()
        c.executescript(SCHEMA)
        self._migrate(c)
        self._backfill_relation_entities(c)
        if not self._has_fts():
            try:
                c.execute(
                    f"CREATE VIRTUAL TABLE {FTS_TABLE} USING fts5(command, payload, "
                    "content='raw_events', content_rowid='rowid')"
                )
                c.executescript(FTS_TRIGGER)
                self._rebuild_fts(c)
                c.commit()
            except sqlite3.OperationalError:
                self.fts = False

    def _has_fts(self) -> bool:
        row = self._conn().execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (FTS_TABLE,)
        ).fetchone()
        self.fts = bool(row)
        return bool(row)

    def _rebuild_fts(self, c: sqlite3.Connection) -> None:
        if self.fts:
            c.execute(f"INSERT INTO {FTS_TABLE}({FTS_TABLE}) VALUES('rebuild')")

    def _migrate(self, c: sqlite3.Connection) -> None:
        """Additive migrations for stores created before a column existed."""
        dcols = {r[1] for r in c.execute("PRAGMA table_info(decisions)")}
        if "status" not in dcols:
            c.execute("ALTER TABLE decisions ADD COLUMN status TEXT NOT NULL DEFAULT 'current'")
        if "valid_until" not in dcols:
            c.execute("ALTER TABLE decisions ADD COLUMN valid_until TEXT")
        if "supersedes_id" not in dcols:
            c.execute("ALTER TABLE decisions ADD COLUMN supersedes_id TEXT")
        if "source_type" not in dcols:
            c.execute("ALTER TABLE decisions ADD COLUMN source_type TEXT DEFAULT 'consolidation'")
        rcols = {r[1] for r in c.execute("PRAGMA table_info(relations)")}
        if "subject_ent" not in rcols:
            c.execute("ALTER TABLE relations ADD COLUMN subject_ent TEXT")
        if "object_ent" not in rcols:
            c.execute("ALTER TABLE relations ADD COLUMN object_ent TEXT")
        # indexes go here (not in SCHEMA) so old stores get their columns
        # before anything indexes them
        c.execute("CREATE INDEX IF NOT EXISTS ix_relations_sub_ent ON relations(subject_ent)")
        c.execute("CREATE INDEX IF NOT EXISTS ix_relations_obj_ent ON relations(object_ent)")
        c.execute("CREATE INDEX IF NOT EXISTS ix_decisions_sup ON decisions(supersedes_id)")
        scols = {r[1] for r in c.execute("PRAGMA table_info(summaries)")}
        if "workspace" not in scols:
            c.execute("ALTER TABLE summaries ADD COLUMN workspace TEXT")
        wcols = {r[1] for r in c.execute("PRAGMA table_info(workspace_state)")}
        if wcols and "rev" not in wcols:
            # pre-existing rows all get rev 1 (tied); every future write
            # bumps past them, which is all the ordering needs
            c.execute(
                "ALTER TABLE workspace_state ADD COLUMN rev INTEGER"
                " NOT NULL DEFAULT 1")
        for col in ("decisions", "problems", "open_items"):
            if col not in scols:
                c.execute(f"ALTER TABLE summaries ADD COLUMN {col} TEXT")
        # index here (not in SCHEMA) so old stores get the column before
        # anything indexes it
        c.execute("CREATE INDEX IF NOT EXISTS ix_summaries_workspace"
                  " ON summaries(workspace, level, ts)")
        self._migrate_summary_structure(c)
        c.commit()

    def _migrate_summary_structure(self, c: sqlite3.Connection) -> None:
        """One-time: pull the attached sections (## Decisions / ## Problems
        / ## Open / ## Records) out of legacy summary text into the
        structured columns; `text` becomes the plain summary prose.
        Idempotent — only rows whose text still carries a '## ' section
        and all of whose sections are known are rewritten; anything
        unrecognized is left untouched. ## Records is dropped entirely
        (the record refs already live in the `refs` column)."""
        rows = c.execute(
            "SELECT id, text FROM summaries WHERE text LIKE '%## %'").fetchall()
        if not rows:
            return
        import re as _re
        known = {"Decisions", "Problems", "Open", "Records"}
        for rid, text in rows:
            heads = list(_re.finditer(r"^## (\w+)\s*$", text, _re.M))
            # conservative: any unknown section header, or any non-item
            # line inside a section body, leaves the row untouched
            if not heads or any(h.group(1) not in known for h in heads):
                continue
            sections: dict | None = {}
            for i, h in enumerate(heads):
                end = heads[i + 1].start() if i + 1 < len(heads) else len(text)
                lines = [ln for ln in text[h.end():end].splitlines()
                          if ln.strip()]
                if any(not ln.startswith("- ") for ln in lines):
                    sections = None
                    break
                sections[h.group(1)] = [ln[2:] for ln in lines]
            if not sections:
                continue
            prose = text[:heads[0].start()].rstrip()
            decisions = []
            for line in sections.get("Decisions", []):
                parts = [p.strip() for p in line.split(" — ", 2)]
                decisions.append({
                    "subject": parts[0] if len(parts) > 0 else "",
                    "choice": parts[1] if len(parts) > 1 else "",
                    "reason": parts[2] if len(parts) > 2 else ""})
            c.execute(
                "UPDATE summaries SET text=?, decisions=?, problems=?, "
                "open_items=? WHERE id=?",
                (prose,
                 json.dumps(decisions, ensure_ascii=False)
                 if decisions else None,
                 json.dumps(sections["Problems"], ensure_ascii=False)
                 if sections.get("Problems") else None,
                 json.dumps(sections["Open"], ensure_ascii=False)
                 if sections.get("Open") else None,
                 rid))

    def _backfill_relation_entities(self, c: sqlite3.Connection) -> None:
        """One-time: give pre-existing name-only relations their entity ids,
        so old and new rows live in one identity system (B2)."""
        if self.meta_get("relations_migrated.v1"):
            return
        names = set()
        for col in ("subject", "object"):
            for r in c.execute(f"SELECT DISTINCT {col} FROM relations WHERE {col} IS NOT NULL"):
                names.add(r[0])
        from .models import slugify

        id_by_name: dict[str, str] = {}
        for name in names:
            key = slugify(name)
            row = c.execute("SELECT id FROM entities WHERE name_key=?", (key,)).fetchone()
            if row:
                eid = row[0]
            else:
                eid = new_id("ent")
                c.execute(
                    "INSERT OR IGNORE INTO entities (id, name, name_key, kind, project, machine, aliases, description)"
                    " VALUES (?,?,?,?,NULL,NULL,'[]',NULL)",
                    (eid, name, key, None),
                )
                eid = c.execute("SELECT id FROM entities WHERE name_key=?", (key,)).fetchone()[0]
            id_by_name.setdefault(name, eid)
        for name, eid in id_by_name.items():
            c.execute("UPDATE relations SET subject_ent=? WHERE lower(subject)=lower(?) AND subject_ent IS NULL",
                      (eid, name))
            c.execute("UPDATE relations SET object_ent=? WHERE lower(object)=lower(?) AND object_ent IS NULL",
                      (eid, name))
        c.commit()
        self.meta_set("relations_migrated.v1", "1")

    def close(self) -> None:
        c = getattr(self._local, "conn", None)
        if c is not None:
            c.close()
            self._local.conn = None

    # ------------------------------------------------------------- raw events

    def append_event(
        self,
        source: str,
        kind: str,
        *,
        ts: str | None = None,
        machine: str | None = None,
        project: str | None = None,
        cwd: str | None = None,
        session: str | None = None,
        command: str | None = None,
        payload: dict | None = None,
        event_id: str | None = None,
    ) -> dict:
        ts = ts or now_utc()
        eid = event_id or new_id("evt", 12)
        # single chokepoint: anything stored (and later sent to an LLM) is
        # redacted here — see F8. The redaction marker travels in the payload
        # so consumers know the text was scrubbed, never silently.
        command_r, red1 = (redact_text(command, self.redact_extra)
                           if (self.redact and command) else (command, []))
        payload_r, red2 = (_redact_deep(dict(payload or {}), self.redact_extra)
                           if self.redact else (dict(payload or {}), []))
        redacted = sorted(set(red1 + red2))
        if redacted:
            payload_r["_redacted"] = redacted
        payload_json = json.dumps(payload_r, ensure_ascii=False, default=str)
        c = self._conn()
        c.execute(
            "INSERT OR IGNORE INTO raw_events (id, ts, source, kind, machine, project,"
            " cwd, session, command, payload, captured_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (eid, ts, source, kind, machine, project, cwd, session,
             command_r, payload_json, now_utc()),
        )
        c.commit()
        self._mirror_jsonl(eid, ts, source, kind, machine, project, command_r, payload_r)
        return {
            "id": eid, "ts": ts, "source": source, "kind": kind, "machine": machine,
            "project": project, "cwd": cwd, "session": session, "command": command_r,
            "payload": payload_r,
        }

    def _mirror_jsonl(self, eid, ts, source, kind, machine, project, command, payload) -> None:
        import fcntl

        month = ts[:7]
        f = self.data_dir / "events" / f"{month}.jsonl"
        line = json.dumps(
            {"id": eid, "ts": ts, "source": source, "kind": kind, "machine": machine,
             "project": project, "command": command, "payload": payload or {}},
            ensure_ascii=False, default=str,
        ) + "\n"
        try:
            f.parent.mkdir(parents=True, exist_ok=True)
            with open(f, "a", encoding="utf-8") as fh:
                fcntl.flock(fh, fcntl.LOCK_EX)
                fh.write(line)
                fcntl.flock(fh, fcntl.LOCK_UN)
        except OSError:
            pass

    def get_event(self, event_id: str) -> dict | None:
        row = self._conn().execute(
            "SELECT * FROM raw_events WHERE id=?", (event_id,)
        ).fetchone()
        return self._event_row(row) if row else None

    @staticmethod
    def _event_row(row: sqlite3.Row) -> dict:
        d = dict(row)
        try:
            d["payload"] = json.loads(row["payload"] or "{}")
        except json.JSONDecodeError:
            d["payload"] = {}
        return d

    def events_since(
        self, ts: str, *, source: str | None = None, project: str | None = None,
        kinds: tuple[str, ...] | None = None, limit: int = 10000,
    ) -> list[dict]:
        q = "SELECT * FROM raw_events WHERE ts >= ?"
        args: list = [ts]
        if source:
            q += " AND source = ?"
            args.append(source)
        if project:
            q += " AND project = ?"
            args.append(project)
        if kinds:
            q += " AND kind IN (%s)" % ",".join("?" * len(kinds))
            args.extend(kinds)
        # ts is second-granular: rowid (insertion order) breaks ties so
        # same-second bursts consolidate in causal order
        q += " ORDER BY ts ASC, rowid ASC LIMIT ?"
        args.append(limit)
        return [self._event_row(r) for r in self._conn().execute(q, args)]

    def count_events(self) -> int:
        row = self._conn().execute("SELECT COUNT(*) FROM raw_events").fetchone()
        return row[0]

    def recent_events(self, limit: int = 50, *, source: str | None = None) -> list[dict]:
        q = "SELECT * FROM raw_events"
        args: list = []
        if source:
            q += " WHERE source = ?"
            args.append(source)
        q += " ORDER BY ts DESC, rowid DESC LIMIT ?"
        args.append(limit)
        return [self._event_row(r) for r in self._conn().execute(q, args)]

    def max_event_ts(self) -> str | None:
        row = self._conn().execute("SELECT MAX(ts) FROM raw_events").fetchone()
        return row[0] if row and row[0] else None

    def referenced_event_ids(self) -> set[str]:
        """Events that provenance points at — never compacted away
        ('why do you believe this?' must keep working forever)."""
        ids: set[str] = set()
        for r in self._conn().execute("SELECT pointer FROM evidence"):
            if r[0] and r[0].startswith("evt_"):
                ids.add(r[0])
        for r in self._conn().execute("SELECT evidence_ids FROM decisions"):
            try:
                ids.update(json.loads(r[0] or "[]"))
            except json.JSONDecodeError:
                pass
        return ids

    def eligible_for_compaction(self, cutoff_ts: str, seen: set[str],
                                excluded: set[str]) -> list[str]:
        """A2: old events that are (a) already consolidated into memory,
        (b) not referenced by any fact/decision evidence."""
        rows = self._conn().execute(
            "SELECT id FROM raw_events WHERE ts < ?", (cutoff_ts,)
        ).fetchall()
        return [r[0] for r in rows if r[0] in seen and r[0] not in excluded]

    def delete_events(self, ids: list[str]) -> int:
        if not ids:
            return 0
        c = self._conn()
        c.executemany("DELETE FROM raw_events WHERE id=?", [(i,) for i in ids])
        # external-content FTS index must be rebuilt after bulk deletes
        if self.fts:
            try:
                c.execute(f"INSERT INTO {FTS_TABLE}({FTS_TABLE}) VALUES('rebuild')")
            except sqlite3.OperationalError:
                self.fts = False
        c.commit()
        return len(ids)

    def delete_events_for_session(self, session: str) -> int:
        """All raw-event rows for one session (window-exemption rollback:
        a session whose depth resolves as gated must not keep logging).
        External-content FTS index must be rebuilt after bulk deletes."""
        c = self._conn()
        cur = c.execute("DELETE FROM raw_events WHERE session=?", (session,))
        n = cur.rowcount or 0
        if n and self.fts:
            try:
                c.execute(f"INSERT INTO {FTS_TABLE}({FTS_TABLE}) VALUES('rebuild')")
            except sqlite3.OperationalError:
                self.fts = False
        c.commit()
        return n

    def oldest_event_ts(self) -> str | None:
        row = self._conn().execute("SELECT MIN(ts) FROM raw_events").fetchone()
        return row[0] if row and row[0] else None

    def insert_episode(self, *, project: str | None, machine: str | None,
                       session: str | None, started_at: str, ended_at: str,
                       objective: str | None, summary: str,
                       lessons: list[str], event_ids: list[str]) -> str:
        eid = new_id("ep")
        self._conn().execute(
            "INSERT INTO episodes (id, project, machine, session, started_at,"
            " ended_at, objective, summary, lessons, event_ids, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (eid, project, machine, session, started_at, ended_at, objective,
             summary, json.dumps(lessons, ensure_ascii=False),
             json.dumps(event_ids), now_utc()),
        )
        self._conn().commit()
        return eid

    def list_episodes(self, *, project: str | None = None,
                      machine: str | None = None, since: str | None = None,
                      unassigned_only: bool = False,
                      limit: int = 50) -> list[dict]:
        q = "SELECT * FROM episodes WHERE 1=1"
        args: list = []
        if unassigned_only:
            q += " AND project IS NULL"
        elif project is not None:
            q += " AND project = ?"
            args.append(project)
        if machine is not None:
            q += " AND machine = ?"
            args.append(machine)
        if since is not None:
            q += " AND started_at >= ?"
            args.append(since)
        q += " ORDER BY started_at DESC LIMIT ?"
        args.append(limit)
        out = []
        for r in self._conn().execute(q, args):
            d = dict(r)
            d["lessons"] = json.loads(d["lessons"] or "[]")
            d["event_ids"] = json.loads(d["event_ids"] or "[]")
            out.append(d)
        return out

    # ----------------------------------------------------------------- facts

    def current_fact(self, project: str | None, component: str | None,
                     subject: str) -> dict | None:
        q = ("SELECT * FROM facts WHERE subject=? AND status='current'"
             " AND (scope_project IS ? OR scope_project IS NULL OR ? IS NULL)"
             " AND (scope_component IS ? OR scope_component IS NULL OR ? IS NULL)")
        rows = self._conn().execute(
            q, (subject, project, project, component, component)
        ).fetchall()
        if not rows:
            return None
        exact = [r for r in rows if r["scope_project"] == project
                 and r["scope_component"] == component]
        row = (exact or rows)[0]
        return dict(row)

    def list_facts(
        self, *, project: str | None = None, component: str | None = None,
        machine: str | None = None, subject: str | None = None,
        status: str | None = None, kind: str | None = None,
        limit: int = 500,
    ) -> list[dict]:
        q = "SELECT * FROM facts WHERE 1=1"
        args: list = []
        if project is not None:
            q += " AND (scope_project = ? OR scope_project IS NULL)"
            args.append(project)
        if component is not None:
            q += " AND scope_component = ?"
            args.append(component)
        if machine is not None:
            q += " AND scope_machine = ?"
            args.append(machine)
        if subject is not None:
            q += " AND subject = ?"
            args.append(subject)
        if status is not None:
            q += " AND status = ?"
            args.append(status)
        if kind is not None:
            q += " AND kind = ?"
            args.append(kind)
        q += " ORDER BY valid_from DESC, created_at DESC LIMIT ?"
        args.append(limit)
        return [dict(r) for r in self._conn().execute(q, args)]

    def fact_history(self, subject: str, *, project: str | None = None,
                     component: str | None = None) -> list[dict]:
        q = "SELECT * FROM facts WHERE subject=?"
        args: list = [subject]
        if project is not None:
            q += " AND (scope_project = ? OR scope_project IS NULL)"
            args.append(project)
        if component is not None:
            q += " AND (scope_component = ? OR scope_component IS NULL)"
            args.append(component)
        q += " ORDER BY COALESCE(valid_from, created_at) ASC"
        return [dict(r) for r in self._conn().execute(q, args)]

    def facts_valid_at(self, ts: str, subject: str | None = None, *,
                       project: str | None = None,
                       component: str | None = None,
                       machine: str | None = None, kind: str | None = None,
                       limit: int = 200) -> list[dict]:
        """Point-in-time read: facts whose validity window covers `ts`
        (`valid_from <= ts < valid_until`, open when valid_until is NULL).
        Timestamps are fixed-format UTC strings, so lexicographic
        comparison is chronological. Retracted rows are excluded — they
        were ruled wrong/obsolete and are 'not established' in any era;
        contradictory rows are INCLUDED: at that moment the conflict may
        have been real, and that is the answer the query deserves."""
        q = ("SELECT * FROM facts WHERE status != 'retracted'"
             " AND valid_from <= ? AND (valid_until IS NULL OR valid_until > ?)")
        args: list = [ts, ts]
        if subject is not None:
            q += " AND subject = ?"
            args.append(subject)
        if project is not None:
            q += " AND (scope_project = ? OR scope_project IS NULL)"
            args.append(project)
        if component is not None:
            q += " AND scope_component = ?"
            args.append(component)
        if machine is not None:
            q += " AND scope_machine = ?"
            args.append(machine)
        if kind is not None:
            q += " AND kind = ?"
            args.append(kind)
        q += " ORDER BY valid_from DESC, created_at DESC LIMIT ?"
        args.append(limit)
        return [dict(r) for r in self._conn().execute(q, args)]

    def decisions_valid_at(self, ts: str, *, project: str | None = None,
                           component: str | None = None,
                           limit: int = 50) -> list[dict]:
        """Decisions in force at `ts`: decided_at <= ts < valid_until
        (open when valid_until is NULL), same trust rules as facts_valid_at."""
        q = ("SELECT * FROM decisions WHERE status != 'retracted'"
             " AND decided_at <= ? AND (valid_until IS NULL OR valid_until > ?)")
        args: list = [ts, ts]
        if project is not None:
            q += " AND (scope_project = ? OR scope_project IS NULL)"
            args.append(project)
        if component is not None:
            q += " AND scope_component = ?"
            args.append(component)
        q += " ORDER BY decided_at DESC LIMIT ?"
        args.append(limit)
        return [dict(r) for r in self._conn().execute(q, args)]

    def insert_fact(
        self, *, scope_project: str | None, scope_component: str | None,
        scope_machine: str | None, subject: str, claim: str, kind: str = "fact",
        status: str = "current", confidence: str = "probable",
        valid_from: str | None = None, valid_until: str | None = None,
        reason: str | None = None, supersedes_id: str | None = None,
        source_type: str = "consolidation",
    ) -> str:
        fid = new_id("fact")
        ts = now_utc()
        self._conn().execute(
            "INSERT INTO facts (id, scope_project, scope_component, scope_machine,"
            " subject, claim, kind, status, confidence, valid_from, valid_until,"
            " reason, supersedes_id, source_type, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (fid, scope_project, scope_component, scope_machine, subject, claim,
             kind, status, confidence, valid_from or ts, valid_until, reason,
             supersedes_id, source_type, ts, ts),
        )
        self._conn().commit()
        return fid

    def close_fact(self, fact_id: str, valid_until: str) -> None:
        self._conn().execute(
            "UPDATE facts SET status='superseded', valid_until=?, updated_at=? WHERE id=?",
            (valid_until, now_utc(), fact_id),
        )
        self._conn().commit()

    def set_fact_status(self, fact_id: str, status: str, *, valid_until: str | None = None) -> None:
        if valid_until:
            self._conn().execute(
                "UPDATE facts SET status=?, valid_until=?, updated_at=? WHERE id=?",
                (status, valid_until, now_utc(), fact_id),
            )
        else:
            self._conn().execute(
                "UPDATE facts SET status=?, updated_at=? WHERE id=?",
                (status, now_utc(), fact_id),
            )
        self._conn().commit()

    def get_fact(self, fact_id: str) -> dict | None:
        row = self._conn().execute("SELECT * FROM facts WHERE id=?", (fact_id,)).fetchone()
        return dict(row) if row else None

    def supersede(self, old: dict, new: dict) -> str:
        """Close `old` and insert `new` with supersedes_id -> old.id.
        `new` keys: scope_project, scope_component, scope_machine, subject,
        claim, kind, confidence, valid_from, reason, source_type."""
        fid = self.insert_fact(
            supersedes_id=old["id"],
            status=new.get("status", "current"),
            **{k: new.get(k) for k in (
                "scope_project", "scope_component", "scope_machine", "subject",
                "claim", "kind", "confidence", "valid_from", "reason", "source_type",
            )},
        )
        self.close_fact(old["id"], new.get("valid_from") or now_utc())
        return fid

    def known_subjects(self, project: str | None, component: str | None,
                       limit: int = 500) -> list[str]:
        """Distinct subjects already used in this scope (F5 vocabulary),
        most-used first. Used to re-target new claims onto established
        subject names instead of fragmenting them."""
        q = ("SELECT subject, COUNT(*) n FROM facts"
             " WHERE (scope_project IS ? OR scope_project IS NULL OR ? IS NULL)"
             " AND (scope_component IS ? OR scope_component IS NULL OR ? IS NULL)"
             " AND subject IS NOT NULL"
             " GROUP BY subject ORDER BY n DESC, MIN(created_at) ASC LIMIT ?")
        rows = self._conn().execute(
            q, (project, project, component, component, limit)
        ).fetchall()
        return [r["subject"] for r in rows]

    def correct_fact(self, fact_id: str, claim: str, reason: str | None = None,
                     decided_by: str = "human") -> str:
        """Supersede a fact with a human-corrected claim (F4). The original
        row and its evidence are preserved."""
        old = self.get_fact(fact_id)
        if not old:
            return ""
        new = {
            "scope_project": old["scope_project"], "scope_component": old["scope_component"],
            "scope_machine": old["scope_machine"], "subject": old["subject"],
            "claim": claim, "kind": old["kind"], "confidence": "confirmed",
            "valid_from": now_utc(),
            "reason": reason or f"corrected by {decided_by}",
            "source_type": "human",
        }
        return self.supersede(old, new)

    def forget_fact(self, fact_id: str, reason: str | None = None) -> bool:
        """Retract a fact: it leaves every 'current' surface but stays in the
        history with the retraction reason (audit preserved)."""
        f = self.get_fact(fact_id)
        if not f:
            return False
        self._conn().execute(
            "UPDATE facts SET status='retracted', valid_until=?, reason=COALESCE(?, reason),"
            " updated_at=? WHERE id=?",
            (now_utc(), reason or "retracted", now_utc(), fact_id),
        )
        self._conn().commit()
        return True

    # --------------------------------------------------------------- evidence

    def add_evidence(self, fact_id: str, source_type: str, pointer: str,
                     note: str | None = None) -> None:
        self._conn().execute(
            "INSERT INTO evidence (id, fact_id, source_type, pointer, note, captured_at)"
            " VALUES (?,?,?,?,?,?)",
            (new_id("ev"), fact_id, source_type, pointer, note, now_utc()),
        )
        self._conn().commit()

    def evidence_for(self, fact_id: str) -> list[dict]:
        out = []
        for r in self._conn().execute(
            "SELECT * FROM evidence WHERE fact_id=? ORDER BY captured_at", (fact_id,)
        ):
            d = dict(r)
            ev = self.get_event(d["pointer"]) if d["pointer"].startswith("evt_") else None
            d["event"] = ev
            out.append(d)
        return out

    # --------------------------------------------------------------- decisions

    def insert_decision(
        self, *, scope_project: str | None, scope_component: str | None,
        scope_machine: str | None, subject: str, choice: str,
        alternatives: list[str] | None = None, reason: str | None = None,
        confidence: str = "probable", decided_at: str | None = None,
        evidence_ids: list[str] | None = None, status: str = "current",
        valid_until: str | None = None, supersedes_id: str | None = None,
        source_type: str = "consolidation",
    ) -> str:
        did = new_id("dec")
        ts = now_utc()
        self._conn().execute(
            "INSERT INTO decisions (id, scope_project, scope_component, scope_machine,"
            " subject, choice, alternatives_considered, reason, confidence, decided_at,"
            " evidence_ids, status, valid_until, supersedes_id, source_type, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (did, scope_project, scope_component, scope_machine, subject, choice,
             json.dumps(alternatives or []), reason, confidence,
             decided_at or ts, json.dumps(evidence_ids or []),
             status, valid_until, supersedes_id, source_type, ts),
        )
        self._conn().commit()
        return did

    def get_decision(self, did: str) -> dict | None:
        row = self._conn().execute("SELECT * FROM decisions WHERE id=?", (did,)).fetchone()
        if not row:
            return None
        d = dict(row)
        d["alternatives_considered"] = json.loads(d["alternatives_considered"] or "[]")
        d["evidence_ids"] = json.loads(d["evidence_ids"] or "[]")
        return d

    def records_for_range(self, start: str, end: str,
                          limit: int = 50) -> list[dict]:
        """Facts + decisions created in [start, end) (ISO-Z), created_at
        order. Session-memory fallback: attach the records a session wrote
        to its summary windows when the raw-span scan recovered no ids."""
        out: list[dict] = []
        for table in ("facts", "decisions"):
            rows = self._conn().execute(
                f"SELECT * FROM {table} WHERE created_at >= ?"
                " AND created_at < ? ORDER BY created_at LIMIT ?",
                (start, end, limit)).fetchall()
            out.extend(dict(r) for r in rows)
        out.sort(key=lambda r: r.get("created_at") or "")
        return out[:limit]

    def current_decision(self, project: str | None, component: str | None,
                         subject: str) -> dict | None:
        q = ("SELECT * FROM decisions WHERE subject=? AND status='current'"
             " AND (scope_project IS ? OR scope_project IS NULL OR ? IS NULL)"
             " AND (scope_component IS ? OR scope_component IS NULL OR ? IS NULL)")
        rows = self._conn().execute(
            q, (subject, project, project, component, component)
        ).fetchall()
        if not rows:
            return None
        exact = [r for r in rows if r["scope_project"] == project
                 and r["scope_component"] == component]
        return self._decision_row((exact or rows)[0])

    def decision_history(self, subject: str, *, project: str | None = None,
                         component: str | None = None) -> list[dict]:
        q = "SELECT * FROM decisions WHERE subject=?"
        args: list = [subject]
        if project is not None:
            q += " AND (scope_project = ? OR scope_project IS NULL)"
            args.append(project)
        if component is not None:
            q += " AND scope_component = ?"
            args.append(component)
        q += " ORDER BY COALESCE(decided_at, created_at) ASC"
        return [self._decision_row(r) for r in self._conn().execute(q, args)]

    @staticmethod
    def _decision_row(row: sqlite3.Row) -> dict:
        d = dict(row)
        d["alternatives_considered"] = json.loads(d["alternatives_considered"] or "[]")
        d["evidence_ids"] = json.loads(d["evidence_ids"] or "[]")
        return d

    def supersede_decision(self, old: dict, new: dict) -> str:
        """Close `old` and insert `new` with supersedes_id -> old.id."""
        did = self.insert_decision(
            supersedes_id=old["id"],
            status=new.get("status", "current"),
            **{k: new.get(k) for k in (
                "scope_project", "scope_component", "scope_machine", "subject",
                "choice", "alternatives", "reason", "confidence", "decided_at",
                "evidence_ids", "source_type",
            )},
        )
        self.close_decision(old["id"], new.get("decided_at") or now_utc())
        return did

    def close_decision(self, did: str, valid_until: str) -> None:
        self._conn().execute(
            "UPDATE decisions SET status='superseded', valid_until=? WHERE id=?",
            (valid_until, did),
        )
        self._conn().commit()

    def set_decision_status(self, did: str, status: str) -> None:
        self._conn().execute(
            "UPDATE decisions SET status=? WHERE id=?", (status, did))
        self._conn().commit()

    def list_decisions(self, *, project: str | None = None,
                       component: str | None = None, status: str | None = None,
                       limit: int = 100) -> list[dict]:
        q = "SELECT * FROM decisions WHERE 1=1"
        args: list = []
        if project is not None:
            q += " AND (scope_project = ? OR scope_project IS NULL)"
            args.append(project)
        if component is not None:
            q += " AND scope_component = ?"
            args.append(component)
        if status is not None:
            q += " AND status = ?"
            args.append(status)
        q += (" ORDER BY CASE WHEN status='current' THEN 0 ELSE 1 END,"
              " COALESCE(decided_at, created_at) DESC LIMIT ?")
        args.append(limit)
        out = []
        for r in self._conn().execute(q, args):
            d = dict(r)
            d["alternatives_considered"] = json.loads(d["alternatives_considered"] or "[]")
            d["evidence_ids"] = json.loads(d["evidence_ids"] or "[]")
            out.append(d)
        return out

    # ---------------------------------------------------------- entities/graph

    def upsert_entity(self, name: str, *, kind: str | None = None,
                      project: str | None = None, machine: str | None = None,
                      aliases: list[str] | None = None,
                      description: str | None = None) -> str:
        from .models import slugify

        key = slugify(name)
        row = self._conn().execute(
            "SELECT id FROM entities WHERE name_key=?", (key,)
        ).fetchone()
        if row:
            self._conn().execute(
                "UPDATE entities SET kind=COALESCE(?, kind), project=COALESCE(?, project),"
                " machine=COALESCE(?, machine), description=COALESCE(?, description)"
                " WHERE id=?",
                (kind, project, machine, description, row["id"]),
            )
            self._conn().commit()
            return row["id"]
        eid = new_id("ent")
        self._conn().execute(
            "INSERT INTO entities (id, name, name_key, kind, project, machine, aliases, description)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (eid, name, key, kind, project, machine,
             json.dumps(aliases or []), description),
        )
        self._conn().commit()
        return eid

    def find_entity(self, name: str) -> dict | None:
        from .models import slugify

        c = self._conn()
        row = c.execute("SELECT * FROM entities WHERE name_key=?", (slugify(name),)).fetchone()
        if not row:
            row = c.execute("SELECT * FROM entities WHERE name=?", (name,)).fetchone()
        if not row:
            row = c.execute(
                "SELECT * FROM entities WHERE aliases LIKE ?", (f"%{name.lower()}%",)
            ).fetchone()
        return dict(row) if row else None

    def list_entities(self, kind: str | None = None, project: str | None = None) -> list[dict]:
        q = "SELECT * FROM entities WHERE 1=1"
        args: list = []
        if kind:
            q += " AND kind=?"
            args.append(kind)
        if project:
            q += " AND project=?"
            args.append(project)
        return [dict(r) for r in self._conn().execute(q, args)]

    def upsert_relation(self, subject: str, relation: str, object_: str,
                         *, confidence: str = "probable", note: str | None = None) -> str:
        """Insert a relation keyed on entity ids (B2: one identity system).
        Both endpoint names are upserted as entities if needed."""
        subj_ent = self.upsert_entity(subject)
        obj_ent = self.upsert_entity(object_)
        row = self._conn().execute(
            "SELECT id FROM relations WHERE subject_ent=? AND relation=?"
            " AND object_ent=? AND valid_until IS NULL",
            (subj_ent, relation, obj_ent),
        ).fetchone()
        if row:
            return row["id"]
        rid = new_id("rel")
        self._conn().execute(
            "INSERT INTO relations (id, subject, relation, object, subject_ent, object_ent,"
            " valid_from, confidence, note) VALUES (?,?,?,?,?,?,?,?,?)",
            (rid, subject, relation, object_, subj_ent, obj_ent,
             now_utc(), confidence, note),
        )
        self._conn().commit()
        return rid

    def relations_for(self, name: str, depth: int = 1) -> list[dict]:
        """BFS over the relations graph, starting from the entity that `name`
        resolves to (falling back to name matching for unknowns)."""
        c = self._conn()
        ent = self.find_entity(name)
        out: list[dict] = []
        seen: set[str] = set()
        frontier: list[tuple[str | None, str]] = [
            (ent["id"] if ent else None, name),
        ]
        for _ in range(max(1, min(depth, 3))):
            nxt: list[tuple[str | None, str]] = []
            for node_id, node_name in frontier:
                if node_id is not None:
                    rows = c.execute(
                        "SELECT * FROM relations WHERE (subject_ent=? OR object_ent=?)"
                        " AND valid_until IS NULL", (node_id, node_id),
                    ).fetchall()
                else:
                    rows = c.execute(
                        "SELECT * FROM relations WHERE (lower(subject)=lower(?)"
                        " OR lower(object)=lower(?)) AND valid_until IS NULL",
                        (node_name, node_name),
                    ).fetchall()
                for row in rows:
                    d = dict(row)
                    d["subject"] = d.get("subject") or ""
                    d["object"] = d.get("object") or ""
                    out.append(d)
                    if d["subject"].lower() == node_name.lower():
                        other_id, other = d.get("object_ent"), d["object"]
                    elif d["object"].lower() == node_name.lower():
                        other_id, other = d.get("subject_ent"), d["subject"]
                    else:  # reached via entity id; figure out the far end
                        if d.get("subject_ent") == node_id:
                            other_id, other = d.get("object_ent"), d["object"]
                        else:
                            other_id, other = d.get("subject_ent"), d["subject"]
                    key = other_id or (other or "").lower()
                    if key and key not in seen:
                        seen.add(key)
                        nxt.append((other_id, other or ""))
            frontier = nxt
        # de-dup while preserving order
        uniq, seen_ids = [], set()
        for d in out:
            if d["id"] not in seen_ids:
                seen_ids.add(d["id"])
                uniq.append(d)
        return uniq

    # ---------------------------------------------------- proposals & review

    def add_proposal(self, kind: str, payload: dict, status: str,
                     note: str | None = None, decided_by: str = "auto") -> str:
        pid = new_id("prop")
        self._conn().execute(
            "INSERT INTO proposals (id, ts, kind, payload, status, decided_by, note)"
            " VALUES (?,?,?,?,?,?,?)",
            (pid, now_utc(), kind, json.dumps(payload, ensure_ascii=False, default=str),
             status, decided_by, note),
        )
        self._conn().commit()
        return pid

    def get_proposal(self, pid: str) -> dict | None:
        row = self._conn().execute("SELECT * FROM proposals WHERE id=?", (pid,)).fetchone()
        if not row:
            return None
        d = dict(row)
        d["payload"] = json.loads(d["payload"] or "{}")
        return d

    def queue_review(self, proposal_id: str, category: str, summary: str) -> str:
        rid = new_id("rev")
        self._conn().execute(
            "INSERT INTO review_queue (id, ts, proposal_id, category, summary, status)"
            " VALUES (?,?,?,?,?,'open')",
            (rid, now_utc(), proposal_id, category, summary),
        )
        self._conn().commit()
        return rid

    def open_reviews(self) -> list[dict]:
        return [dict(r) for r in self._conn().execute(
            "SELECT * FROM review_queue WHERE status='open' ORDER BY ts DESC")]

    def get_review(self, rid: str) -> dict | None:
        row = self._conn().execute("SELECT * FROM review_queue WHERE id=?", (rid,)).fetchone()
        return dict(row) if row else None

    def resolve_review(self, rid: str, resolution: str,
                       decided_by: str = "human") -> None:
        self._conn().execute(
            "UPDATE review_queue SET status='resolved', resolution=? WHERE id=?",
            (f"{resolution} (by {decided_by})", rid),
        )
        self._conn().commit()

    # ------------------------------------------------------------------- meta

    def meta_get(self, key: str) -> str | None:
        row = self._conn().execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def meta_set(self, key: str, value: str) -> None:
        self._conn().execute(
            "INSERT INTO meta (key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        self._conn().commit()

    def snap_get(self, key: str) -> dict | None:
        row = self._conn().execute("SELECT payload FROM snapshots WHERE key=?", (key,)).fetchone()
        if not row:
            return None
        try:
            return json.loads(row[0])
        except json.JSONDecodeError:
            return None

    def snap_set(self, key: str, payload: dict) -> None:
        self._conn().execute(
            "INSERT INTO snapshots (key, payload, updated_at) VALUES (?,?,?)"
            " ON CONFLICT(key) DO UPDATE SET payload=excluded.payload, updated_at=excluded.updated_at",
            (key, json.dumps(payload, default=str), now_utc()),
        )
        self._conn().commit()

    # ----------------------------------------------------------------- search

    def search_events(self, text: str, limit: int = 20) -> list[dict]:
        tokens = [t.strip('(),"\'') for t in text.split() if len(t.strip()) > 1]
        tokens = [t for t in tokens if not set(t) <= set('<>^&|()-*!@')]
        if not tokens:
            return []
        c = self._conn()
        if self.fts:
            pattern = " ".join(f'"{t.replace(chr(34), "")}"' for t in tokens[:8])
            try:
                rows = c.execute(
                    f"SELECT e.* FROM {FTS_TABLE} f JOIN raw_events e ON e.rowid=f.rowid"
                    f" WHERE {FTS_TABLE} MATCH ? ORDER BY f.rank LIMIT ?",
                    (pattern, limit),
                ).fetchall()
                return [self._event_row(r) for r in rows]
            except sqlite3.OperationalError:
                pass
        like = f"%{text.strip()}%"
        rows = c.execute(
            "SELECT * FROM raw_events WHERE command LIKE ? OR payload LIKE ? ORDER BY ts DESC LIMIT ?",
            (like, like, limit),
        ).fetchall()
        return [self._event_row(r) for r in rows]

    def search_facts(self, text: str, limit: int = 20) -> list[dict]:
        where, params = _word_and(("claim", "subject", "reason"), text)
        if not where:
            return []
        rows = self._conn().execute(
            f"SELECT * FROM facts WHERE {' AND '.join(where)}"
            " ORDER BY status='current' DESC, valid_from DESC LIMIT ?",
            (*params, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def search_decisions(self, text: str, limit: int = 20) -> list[dict]:
        where, params = _word_and(("choice", "reason", "subject"), text)
        if not where:
            return []
        rows = self._conn().execute(
            f"SELECT * FROM decisions WHERE {' AND '.join(where)}"
            " ORDER BY decided_at DESC LIMIT ?",
            (*params, limit),
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["alternatives_considered"] = json.loads(d["alternatives_considered"] or "[]")
            d["evidence_ids"] = json.loads(d["evidence_ids"] or "[]")
            out.append(d)
        return out

    # ------------------------------------------------------------- summaries

    @staticmethod
    def _summary_row(row: sqlite3.Row) -> dict:
        d = dict(row)
        d["references"] = json.loads(d["refs"] or "[]")
        d["subjects"] = json.loads(d["subjects"] or "[]")
        for col in ("decisions", "problems", "open_items"):
            d[col] = json.loads(d[col]) if d.get(col) else []
        return d

    def insert_summary(self, *, ts: str | None = None, machine: str | None = None,
                       session: str | None = None, harness: str | None = None,
                       axis: str = "project", project: str | None = None,
                       workspace: str | None = None,
                       component: str | None = None, level: int = 1,
                       parent_id: str | None = None, period: str | None = None,
                       span: str | None = None, references: list | None = None,
                       text: str, subjects: list[str] | None = None,
                       decisions: list | None = None,
                       problems: list | None = None,
                       open_items: list | None = None,
                       model: str | None = None, tokens_in: int | None = None,
                       tokens_out: int | None = None,
                       status: str = "current") -> str:
        """Insert a summary node. `text` is the plain summary prose —
        structured material (decisions/problems/open_items) lives in its own
        columns, never in the text. Invariants (SESSION-MEMORY.md §4):
        level-1 rows carry a `span` (window into the harness session log);
        level>1 and temporal rows carry `references` (child summary ids)."""
        sid = new_id("sum")
        self._conn().execute(
            "INSERT INTO summaries (id, ts, machine, session, harness, axis, project,"
            " workspace, component, level, parent_id, period, span, refs, text, subjects,"
            " decisions, problems, open_items, model, tokens_in, tokens_out, status,"
            " created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (sid, ts or now_utc(), machine, session, harness, axis, project,
             workspace, component, level, parent_id, period, span,
             json.dumps(references or [], ensure_ascii=False), text,
             json.dumps(subjects or [], ensure_ascii=False),
             json.dumps(decisions, ensure_ascii=False) if decisions else None,
             json.dumps(problems, ensure_ascii=False) if problems else None,
             json.dumps(open_items, ensure_ascii=False) if open_items else None,
             model, tokens_in, tokens_out, status, now_utc()),
        )
        self._conn().commit()
        return sid

    def get_summary(self, sid: str) -> dict | None:
        row = self._conn().execute("SELECT * FROM summaries WHERE id=?", (sid,)).fetchone()
        return self._summary_row(row) if row else None

    def list_summaries(self, *, project: str | None = None, session: str | None = None,
                       workspace: str | None = None,
                       axis: str | None = None, level: int | None = None,
                       parent_id: str | None = None, status: str | None = None,
                       order: str = "desc", limit: int = 100) -> list[dict]:
        q = "SELECT * FROM summaries WHERE 1=1"
        args: list = []
        if project is not None:
            q += " AND (project = ? OR project IS NULL)"
            args.append(project)
        if workspace is not None:
            q += " AND workspace = ?"
            args.append(workspace)
        if session is not None:
            q += " AND session = ?"
            args.append(session)
        if axis is not None:
            q += " AND axis = ?"
            args.append(axis)
        if level is not None:
            q += " AND level = ?"
            args.append(level)
        if parent_id is not None:
            q += " AND parent_id = ?"
            args.append(parent_id)
        if status is not None:
            q += " AND status = ?"
            args.append(status)
        q += f" ORDER BY ts {order.upper()} LIMIT ?"
        args.append(limit)
        return [self._summary_row(r) for r in self._conn().execute(q, args)]

    # ---------------------------------------------------- workspace state

    def _wstate_row(self, r) -> dict:
        return {
            "id": r[0], "workspace": r[1], "category": r[2], "subject": r[3],
            "body": r[4], "status": r[5], "source_session": r[6],
            "source_span": r[7], "created_at": r[8], "updated_at": r[9],
            "superseded_by": r[10], "rev": r[11],
        }

    _WSTATE_COLS = ("id, workspace, category, subject, body, status, "
                    "source_session, source_span, created_at, updated_at, "
                    "superseded_by, rev")

    _WS_REV = "(SELECT COALESCE(MAX(rev), 0) + 1 FROM workspace_state)"

    def workspace_state_get(self, wid: str) -> dict | None:
        r = self._conn().execute(
            f"SELECT {self._WSTATE_COLS} FROM workspace_state WHERE id=?",
            (wid,)).fetchone()
        return self._wstate_row(r) if r else None

    def workspace_state_active(self, workspace: str, category: str,
                               subject: str) -> dict | None:
        """The current (active) row for one workspace+category+subject key,
        or None. The upsert target."""
        r = self._conn().execute(
            f"SELECT {self._WSTATE_COLS} FROM workspace_state"
            " WHERE workspace=? AND category=? AND subject=? AND status='active'",
            (workspace, category, subject)).fetchone()
        return self._wstate_row(r) if r else None

    def workspace_state_upsert(self, workspace: str, category: str,
                              subject: str, body: str,
                              *, source_session: str | None = None,
                              source_span: str | None = None) -> dict:
        """Create or update the current row for the key. An active row with
        the same body is a no-op (idempotent re-promotion from a later
        window); otherwise the body/updated_at (and the provenance pointer)
        are refreshed in place. Historical (non-active) rows are never
        touched. Returns the row."""
        ts = now_utc()
        cur = self.workspace_state_active(workspace, category, subject)
        if cur is not None and cur["body"] == body \
                and cur["source_span"] == source_span:
            return cur
        if cur is not None:
            self._conn().execute(
                "UPDATE workspace_state SET body=?, source_session=?,"
                " source_span=?, updated_at=?, rev=" + self._WS_REV
                + " WHERE id=?",
                (body, source_session, source_span, ts, cur["id"]))
            self._conn().commit()
            return self.workspace_state_get(cur["id"])
        wid = new_id("wst")
        self._conn().execute(
            "INSERT INTO workspace_state "
            "(id, workspace, category, subject, body, status,"
            " source_session, source_span, created_at, updated_at,"
            " superseded_by, rev)"
            f" VALUES (?,?,?,?,?,'active',?,?,?,?,NULL,{self._WS_REV})",
            (wid, workspace, category, subject, body, source_session,
             source_span, ts, ts))
        self._conn().commit()
        return self.workspace_state_get(wid)

    def workspace_state_set(self, wid: str, status: str,
                            *, body: str | None = None) -> dict:
        """Transition a row: active -> resolved/retired, or supersede it
        (a new active row takes over the key; this row points at it via
        superseded_by). 'active' re-activates. Returns the updated row."""
        row = self.workspace_state_get(wid)
        if row is None:
            raise ValueError(f"unknown workspace_state id: {wid}")
        ts = now_utc()
        if status == "superseded":
            # a NEW active row takes over the key; this row (which must be
            # active) closes with a pointer. History is kept.
            if row["status"] != "active":
                raise ValueError(
                    f"only an active row can be superseded ({row['status']})")
            new_wid = new_id("wst")
            self._conn().execute(
                "INSERT INTO workspace_state (id, workspace, category, subject,"
                " body, status, source_session, source_span, created_at,"
                " updated_at, superseded_by, rev)"
                f" VALUES (?,?,?,?,?,'active',?,?,?,?,NULL,{self._WS_REV})",
                (new_wid, row["workspace"], row["category"], row["subject"],
                 body or row["body"], row["source_session"],
                 row["source_span"], ts, ts))
            self._conn().execute(
                "UPDATE workspace_state SET status='superseded',"
                " superseded_by=?, updated_at=?, rev=" + self._WS_REV
                + " WHERE id=?",
                (new_wid, ts, wid))
            self._conn().commit()
        elif status == "active":
            # re-activating a closed row: only if the key is free
            if self.workspace_state_active(row["workspace"],
                                           row["category"],
                                           row["subject"]) is not None:
                raise ValueError("another active row holds this key")
            self._conn().execute(
                "UPDATE workspace_state SET status='active',"
                " superseded_by=NULL, updated_at=?, rev=" + self._WS_REV
                + " WHERE id=?",
                (ts, wid))
            self._conn().commit()
        else:  # resolved | retired
            self._conn().execute(
                "UPDATE workspace_state SET status=?, updated_at=?, rev="
                + self._WS_REV + " WHERE id=?",
                (status, ts, wid))
            self._conn().commit()
        return self.workspace_state_get(wid)

    def workspace_state_current(self, workspace: str,
                                recently_resolved: int = 5) -> list[dict]:
        """The current view: every active row for the workspace (creation
        order, so the document reads chronologically) plus up to
        `recently_resolved` rows that closed most recently (context:
        "what just got handled"). Closing order is `rev` — a global
        monotonic counter bumped on every write — because timestamps are
        second-precision and several closes can land in the same second."""
        c = self._conn()
        rows = [self._wstate_row(r) for r in c.execute(
            f"SELECT {self._WSTATE_COLS} FROM workspace_state"
            " WHERE workspace=? AND status='active'"
            " ORDER BY created_at, id", (workspace,))]
        if recently_resolved:
            rows += [self._wstate_row(r) for r in c.execute(
                f"SELECT {self._WSTATE_COLS} FROM workspace_state"
                " WHERE workspace=? AND status IN ('resolved','retired')"
                " ORDER BY rev DESC LIMIT ?",
                (workspace, recently_resolved))]
        return rows

    def workspace_state_sweep(self, workspace: str, days: float,
                              dry_run: bool = False) -> list[dict]:
        """P2 triage: active rows untouched for `days` become `stale`
        (soft: dropped from the envelope's active section, still in the
        store, revivable via `set <id> active`). Never raises on
        per-row problems; returns the affected rows."""
        ts = (datetime.utcnow() - timedelta(days=days)).strftime(
            "%Y-%m-%dT%H:%M:%SZ")
        c = self._conn()
        rows = [self._wstate_row(r) for r in c.execute(
            f"SELECT {self._WSTATE_COLS} FROM workspace_state"
            " WHERE workspace=? AND status='active' AND updated_at<?"
            " ORDER BY updated_at", (workspace, ts)).fetchall()]
        if dry_run or not rows:
            return rows
        now = now_utc()
        for r in rows:
            c.execute("UPDATE workspace_state SET status='stale', updated_at="
                      "?, rev=" + self._WS_REV + " WHERE id=?",
                      (now, r["id"]))
        c.commit()
        return [self.workspace_state_get(r["id"]) for r in rows]

    def workspace_state_stale(self, workspace: str) -> list[dict]:
        """The workspace's soft-closed (`stale`) rows, oldest first."""
        return [self._wstate_row(r) for r in self._conn().execute(
            f"SELECT {self._WSTATE_COLS} FROM workspace_state"
            " WHERE workspace=? AND status='stale' ORDER BY updated_at",
            (workspace,)).fetchall()]

    def workspace_state_list(self, workspace: str, *, category: str | None = None,
                             status: str | None = None, limit: int | None = None,
                             age_days: float | None = None) -> list[dict]:
        """P2 filters: any combination of category / status / recency.
        Most-recently-handled first (rev)."""
        where, params = ["workspace=?"], [workspace]
        if category:
            where.append("category=?"); params.append(category)
        if status:
            where.append("status=?"); params.append(status)
        if age_days is not None:
            ts = (datetime.utcnow() - timedelta(days=age_days)).strftime(
                "%Y-%m-%dT%H:%M:%SZ")
            where.append("updated_at>=?"); params.append(ts)
        sql = (f"SELECT {self._WSTATE_COLS} FROM workspace_state"
               f" WHERE {' AND '.join(where)} ORDER BY rev DESC")
        if limit:
            sql += " LIMIT ?"; params.append(limit)
        return [self._wstate_row(r) for r in self._conn()
                .execute(sql, params).fetchall()]

    def workspace_state_absorb(self, wid: str, keeper_id: str) -> dict:
        """P4 dedupe: close `wid` as superseded by the EXISTING
        `keeper_id` (no new row is created — contrast with the
        key-taking `workspace_state_set(wid, 'superseded')`)."""
        if self.workspace_state_get(wid) is None:
            raise ValueError(f"unknown workspace_state id: {wid}")
        if self.workspace_state_get(keeper_id) is None:
            raise ValueError(f"unknown workspace_state id: {keeper_id}")
        ts = now_utc()
        self._conn().execute(
            "UPDATE workspace_state SET status='superseded',"
            " superseded_by=?, updated_at=?, rev=" + self._WS_REV
            + " WHERE id=?", (keeper_id, ts, wid))
        self._conn().commit()
        return self.workspace_state_get(wid)

    # ------------------------------------------------ embeddings (R3)

    def get_embedding(self, kind: str, ref_id: str) -> bytes | None:
        r = self._conn().execute(
            "SELECT vector FROM embeddings WHERE kind=? AND ref_id=?",
            (kind, ref_id)).fetchone()
        return r[0] if r else None

    def get_embeddings(self, kind: str, ref_ids: list[str]) -> dict[str, bytes]:
        """Bulk fetch; missing ids simply absent from the result."""
        if not ref_ids:
            return {}
        out: dict[str, bytes] = {}
        c = self._conn()
        for i in range(0, len(ref_ids), 500):
            chunk = ref_ids[i:i + 500]
            q = ",".join("?" * len(chunk))
            for row in c.execute(
                    f"SELECT ref_id, vector FROM embeddings"
                    f" WHERE kind=? AND ref_id IN ({q})",
                    [kind, *chunk]):
                out[row[0]] = row[1]
        return out

    def upsert_embedding(self, kind: str, ref_id: str, vector: bytes,
                         dim: int) -> None:
        self._conn().execute(
            "INSERT INTO embeddings(kind, ref_id, dim, vector, updated_at)"
            " VALUES (?,?,?,?,?) ON CONFLICT(kind, ref_id) DO UPDATE SET"
            " dim=excluded.dim, vector=excluded.vector,"
            " updated_at=excluded.updated_at",
            (kind, ref_id, dim, vector, now_utc()))
        self._conn().commit()

    def unparented_summaries(self, *, project: str | None = None,
                             workspace: str | None = None,
                             level: int = 1,
                             status: str | None = None) -> list[dict]:
        """Summaries at `level` not yet folded into a parent — the chain
        frontier. Oldest first: chaining consumes the oldest N. With
        `workspace`, the frontier is that workspace's (exact match);
        `project` only applies without a workspace."""
        q = ("SELECT * FROM summaries WHERE level=? AND parent_id IS NULL AND"
             " axis='project'"
             + (" AND status=?" if status else "")
             + (" AND workspace = ?" if workspace is not None else "")
             + (" AND (project = ? OR project IS NULL)"
                if (project is not None and workspace is None) else ""))
        args: list = [level]
        if status:
            args.append(status)
        if workspace is not None:
            args.append(workspace)
        if project is not None and workspace is None:
            args.append(project)
        q += " ORDER BY ts ASC"
        return [self._summary_row(r) for r in self._conn().execute(q, args)]

    def summaries_referencing(self, ids: list[str]) -> list[dict]:
        """Nodes whose refs list mentions any of `ids` (fold parents,
        temporal rollups). JSON array storage: ids appear as "id" tokens."""
        if not ids:
            return []
        clauses = " OR ".join("refs LIKE ?" for _ in ids)
        q = (f"SELECT * FROM summaries WHERE {clauses}")
        rows = self._conn().execute(q, [f'%"{i}"%' for i in ids]).fetchall()
        return [self._summary_row(r) for r in rows]

    # ------------------------------------------------------------- llm_calls
    # LLM exchange audit (session-memory): the exact prompt sent to the
    # summarizer and the raw response, one row per call. `memor
    # window-review` prints these alongside the stored summary.

    def record_llm_call(self, *, ts: str, mode: str, prompt: str,
                       raw_response: str | None = None, ok: bool = False,
                       session: str | None = None, span: str | None = None,
                       machine: str | None = None,
                       model: str | None = None) -> str:
        sid = new_id("llm")
        self._conn().execute(
            "INSERT INTO llm_calls (id, ts, machine, session, span, mode,"
            " prompt, raw_response, model, ok, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (sid, ts, machine, session, span, mode, prompt, raw_response,
             model, 1 if ok else 0, ts))
        return sid

    def list_llm_calls(self, session: str | None = None,
                       span: str | None = None, limit: int = 100) -> list[dict]:
        q = "SELECT * FROM llm_calls"
        clauses: list[str] = []
        args: list = []
        if session is not None:
            clauses.append("session = ?")
            args.append(session)
        if span is not None:
            clauses.append("span = ?")
            args.append(span)
        if clauses:
            q += " WHERE " + " AND ".join(clauses)
        q += " ORDER BY ts DESC LIMIT ?"
        args.append(limit)
        return [dict(r) for r in self._conn().execute(q, args).fetchall()]

    def envelope_delivered(self, session: str) -> bool:
        """Has the one-shot first-turn envelope already been delivered to
        this session?"""
        return self._conn().execute(
            "SELECT 1 FROM envelope_delivered WHERE session=?",
            (session,)).fetchone() is not None

    def mark_envelope_delivered(self, session: str) -> None:
        ts = now_utc()
        self._conn().execute(
            "INSERT OR IGNORE INTO envelope_delivered(session, delivered_at)"
            " VALUES(?, ?)", (session, ts))
        self._conn().commit()

    def get_summary_by_span(self, span: str) -> dict | None:
        row = self._conn().execute(
            "SELECT * FROM summaries WHERE span=? ORDER BY ts DESC LIMIT 1",
            (span,)).fetchone()
        return self._summary_row(row) if row else None

    def stored_window(self, session: str, lo: int) -> list[dict]:
        """All L1 summaries whose span STARTS at `seq[lo..` for this session
        (the window that opened at lo). Normally exactly one; crash-era
        re-flushes with a grown boundary may leave several."""
        rows = self._conn().execute(
            "SELECT * FROM summaries WHERE session=? AND axis='project' "
            "AND span LIKE ? ORDER BY ts DESC",
            (session, f"%seq[{lo}..%")).fetchall()
        return [self._summary_row(r) for r in rows]

    def search_summaries(self, text: str, limit: int = 20) -> list[dict]:
        where, params = _word_and(("text", "subjects", "span"), text)
        if not where:
            return []
        rows = self._conn().execute(
            f"SELECT * FROM summaries WHERE {' AND '.join(where)}"
            " ORDER BY ts DESC LIMIT ?",
            (*params, limit),
        ).fetchall()
        return [self._summary_row(r) for r in rows]

    def latest_root_summaries(self, *, project: str | None = None,
                              workspace: str | None = None,
                              limit: int = 3) -> list[dict]:
        """Top of the tree: the newest parentless (root) summary per
        session, newest sessions first. A chain root (level>1) covers its
        whole folded span; an unchained level-1 is its own surface. For a
        `workspace` query the roots are workspace-level (session NULL), so
        no per-session dedup — newest `limit` by ts."""
        q = ("SELECT * FROM summaries WHERE parent_id IS NULL AND axis='project'")
        args: list = []
        if project is not None:
            q += " AND (project = ? OR project IS NULL)"
            args.append(project)
        if workspace is not None:
            q += " AND workspace = ?"
            args.append(workspace)
        q += " ORDER BY ts DESC"
        seen: set[str | None] = set()
        out: list[dict] = []
        for r in self._conn().execute(q, args):
            d = self._summary_row(r)
            if workspace is None and d["session"] in seen:
                continue
            if workspace is None:
                seen.add(d["session"])
            out.append(d)
            if len(out) >= limit:
                break
        return out

    def find_temporal(self, machine: str | None, period: str,
                      workspace: str | None = None) -> dict | None:
        """Cached lazy rollup for (machine, period), e.g.
        ('box', 'day:2026-09-15'); optionally scoped to a workspace."""
        q = ("SELECT * FROM summaries WHERE axis='temporal' AND period=?"
             " AND (machine = ? OR (machine IS NULL AND ? IS NULL))"
             + (" AND workspace = ?" if workspace else " AND workspace IS NULL"))
        args = [period, machine, machine] + ([workspace] if workspace else [])
        row = self._conn().execute(q + " ORDER BY ts DESC LIMIT 1", args).fetchone()
        return self._summary_row(row) if row else None

    def delete_summary(self, sid: str) -> None:
        row = self._conn().execute("SELECT * FROM summaries WHERE id=?", (sid,)).fetchone()
        if row is None:
            return
        self._conn().execute(
            "UPDATE summaries SET parent_id=NULL WHERE parent_id=?", (sid,))
        self._conn().execute("DELETE FROM summaries WHERE id=?", (sid,))
        self._conn().commit()

    def set_summary_parents(self, child_ids: list[str], parent_id: str) -> None:
        """Fold a set of siblings under one parent (chain-width fold)."""
        if not child_ids:
            return
        q = ",".join("?" for _ in child_ids)
        self._conn().execute(
            f"UPDATE summaries SET parent_id=? WHERE id IN ({q})"
            " AND parent_id IS NULL", ([parent_id] + list(child_ids)),
        )
        self._conn().commit()