# Solution Architecture: A Durable, Tool-Agnostic Environmental/Project Memory System

See `FINDINGS.md` for why no existing product is adopted wholesale, and `research/LANDSCAPE.md` for the underlying comparison. This plan assembles the pieces the landscape research showed to be genuinely reusable, and scopes precisely what has to be custom-built — which, per the research, is smaller than it sounds: a schema, a consolidation job, and a thin MCP server. Everything else is configuration of existing, mature tools.

## Status

Phases 0–3 are implemented in the `memor/` package and verified (full pytest suite green, plus end-to-end runs of the brief's own benchmarks: reversal A→B→A history, "catch me up" project context, evidence trails back to raw events, cross-harness MCP queries). Deliberate deviations from the text above, all in the "assemble, don't build" spirit:

- **Watchman → `watchdog` (inotify) in-process**: same job (config/unit-file change evidence with content diffing), no extra daemon; covers the journald unit-file-edit gap.
- **Docker Drift sidecar → native `docker ps`/inspect poll-diff** (`docker_events.poll_docker_snapshots`): the fallback the plan explicitly allowed.
- **Scheduled consolidation**: the capture daemon runs a consolidation pass on `Config.consolidate_interval` (default 1h, `memor capture run --consolidate-interval N`, wired into the systemd user unit); manual `memor consolidate` remains. A non-blocking file lock (`consolidate.lock`) keeps daemon and manual runs from racing.
- **Store self-observation is fenced off**: the store's own projection repo is never pointed at the capture hooks, and git events from it are dropped even if re-pointed — otherwise each consolidation commit would become "new evidence" for the next pass. The store's git history remains the diff-able audit trail of the Markdown projection.

Phase 4 (self-hosted Graphiti as secondary semantic/graph index, embeddings, XTDB) stays deferred per §34 until real usage shows a retrieval gap.

## Architecture at a glance

```
                    ENVIRONMENT (per machine)
    Terminal      Git       Docker      systemd     Filesystem
       │           │          │           │             │
     Atuin      git hooks  docker      journalctl    Watchman
   + shell      (post-     events +    -o json       (configs +
     hooks      commit/    Drift                      unit files)
   + Claude       merge/
   Code hooks   checkout)
       └───────────┴──────────┴───────────┴─────────────┘
                             │
                  append-only JSONL / SQLite
                  "raw evidence" store (Level 4)
                             │
                             ▼
                 ┌───────────────────────┐
                 │  CONSOLIDATION JOB     │   <- the first genuinely custom component
                 │  (scheduled/triggered) │
                 │  LLM extracts facts/   │
                 │  decisions, checks     │
                 │  for contradiction,    │
                 │  never overwrites      │
                 └───────────┬───────────┘
                             │
              ┌──────────────┴───────────────┐
              ▼                               ▼
   SQLite/Postgres temporal tables     Markdown hierarchy (Level 0-2)
   (facts, decisions, evidence,        (git-committed projection of
    supersedes — Level 3 detail)        current state, human-readable)
              │                               │
              └───────────────┬───────────────┘
                               ▼
                  ┌─────────────────────────┐
                  │   MCP SERVER            │   <- the second custom component
                  │  memory.get_current_    │
                  │  state / .get_history / │
                  │  .get_decisions / etc.  │
                  └────────────┬────────────┘
                               │
                ┌──────────────┼──────────────┐
                ▼               ▼              ▼
          Claude Code      Other agent CLIs   Human (reads
          (MCP client)      (MCP clients)      Markdown/git directly)
```

## Component decisions and why

### 1. Capture layer — assemble, don't build

Per `research/terminal-capture.md` and `research/infra-capture.md`, every source of environmental evidence already has a weekend-viable, JSON-emitting tool:

- **Terminal**: [Atuin](https://atuin.sh) (self-hosted or sync-disabled) as the always-on baseline — point the consolidation job's ingestion step directly at `~/.local/share/atuin/history.db` (stable SQLite schema). Add a `precmd`/`preexec` hook only if real-time push (vs. periodic polling of the SQLite file) is needed. Wire Claude Code's `PostToolUse` hook (matcher `Bash`) as the *only* source of command output and a session/prompt correlation id — repeat per additional agent CLI as their hook surfaces are confirmed. Use `asciinema` only as an opt-in wrapper around explicitly flagged high-risk commands (deploys, migrations), not as ambient capture — the research is explicit that full-session recording doesn't scale as a default.
- **Git**: `post-commit`/`post-merge`/`post-checkout` hooks distributed via `core.hooksPath`, emitting JSON (SHA, branch, diffstat, parents) — you define the schema, ~20 lines each.
- **Docker**: `docker events --format '{{json .}}'` as a small systemd user service, plus [Docker Drift](https://hub.docker.com/r/zachbg/docker-drift) as a sidecar for config-drift detection (`OUTPUT_FORMAT=json`) — treat Drift as replaceable (it's a small, single-maintainer image) rather than load-bearing; a `docker inspect`-diff script is an easy fallback if it disappears.
- **systemd**: `journalctl -o json -f` tailed for service lifecycle. Confirmed gap: unit-file *edits* are not tracked by journald at all — cover this with the filesystem watcher below, pointed at `/etc/systemd/system/` and `~/.config/systemd/user/`.
- **Filesystem/config**: [Watchman](https://facebook.github.io/watchman/) watching a short explicit list of directories (`/etc`, `~/.config`, project dotfiles, systemd unit dirs), JSON trigger output.
- **Process/network**: a small `ss -tulpn` poll-diff script (10-30s interval or triggered off Docker/systemd events), emitting a structured event only on change. eBPF/Falco explicitly ruled out — that's production intrusion-detection tooling, not personal dev-box journaling, and adds a privileged daemon for no benefit here.

All sources land as JSON lines in one append-only local store — this **is** the brief's Level 4 raw evidence layer. Total estimated effort: **roughly one weekend** (both capture research files independently arrived at 1-2 days).

### 2. Temporal/representation storage — start with SQLite, not a new database

Per `research/bitemporal-stores.md`, the brief's core temporal requirement (non-destructive current-vs-historical state, `valid_from`/`valid_until`/`supersedes`/`reason`/`evidence`) is best served by a **plain schema with explicit temporal columns**, not by adopting a new database technology on day one:

```sql
CREATE TABLE facts (
  id TEXT PRIMARY KEY,
  scope_project TEXT, scope_component TEXT, scope_machine TEXT,
  subject TEXT, claim TEXT,
  status TEXT,                 -- current | superseded | contradictory | unknown
  confidence TEXT,              -- confirmed | probable | inferred | unknown
  valid_from TIMESTAMP, valid_until TIMESTAMP,
  reason TEXT, supersedes_id TEXT REFERENCES facts(id),
  created_at TIMESTAMP, updated_at TIMESTAMP
);
CREATE TABLE evidence (
  id TEXT PRIMARY KEY, fact_id TEXT REFERENCES facts(id),
  source_type TEXT,             -- shell | git | docker | systemd | filesystem | human
  pointer TEXT,                 -- path, commit SHA, or raw-event-store row id
  captured_at TIMESTAMP
);
CREATE TABLE decisions (
  id TEXT PRIMARY KEY, scope_project TEXT, scope_component TEXT,
  choice TEXT, alternatives_considered TEXT, reason TEXT,
  confidence TEXT, decided_at TIMESTAMP, evidence_ids TEXT
);
```
An `UPDATE` never mutates `claim`/`confidence` in place — it closes the prior row's `valid_until` and inserts a new row with `supersedes_id` pointing back, mirroring Graphiti's `invalid_at` pattern and XTDB's bitemporal model, without adopting either as infrastructure. This directly implements the brief's own §19 candidate schema — no researched product ships this schema, so this is unavoidably custom, but it's a few dozen lines of DDL plus write-path discipline in the consolidation job, not a new system.

**Upgrade path, not a starting point**: if multi-hop relationship queries or hybrid semantic search later prove necessary, add self-hosted **Graphiti** (Apache-2.0, `research/zep-graphiti.md`) as a secondary graph/semantic index fed by the same consolidation job — don't replace the SQL facts table, which stays the authoritative temporal/decision record. Reach for **XTDB** only if the hand-rolled bitemporal columns produce real correctness bugs at scale (interval-overlap edge cases) — per the research, this is unlikely to matter before then, and XTDB brings zero agent-memory features, only a better-audited time axis.

### 3. Human-readable hierarchy (Levels 0-2) — Markdown, git-committed

Borrow the two closest prior-art conventions rather than inventing a new one:
- **Directory shape** from Letta's MemFS (`research/letta-memfs.md`): a small `system/`-equivalent (always-loaded Level 0-1 summaries) vs. a larger `reference/`-equivalent (Level 2-3 detail, loaded on demand).
- **In-file format** from Basic Memory (`research/basic-memory-mcp-servers.md`): YAML frontmatter, `- [decision]/[fact]/[problem]` observation-category tagging, and `- relation_type [[Target]]` wikilinks for cross-entity relationships — plain enough for a human to read in any editor or Obsidian, structured enough for a script to parse.

These Markdown files are a **projection of the current facts/decisions tables, regenerated by the consolidation job and committed to git on every run** — git then gives a free, human-diffable secondary history of "what the current-state summary looked like over time," even though the SQL tables remain the authoritative source of temporal truth (Markdown alone, as the research repeatedly found, can't express `valid_from`/`supersedes` correctly — that's exactly why Letta's and Basic Memory's git-only history was flagged as a gap in both their write-ups).

### 4. Consolidation job — the main custom component

A scheduled or on-demand job (nightly, after a work session, or triggered by hook) that:
1. Reads new raw events since last run from the Level 4 store, grouped by scope (heuristics: cwd → project mapping, git remote → project, hostname → machine).
2. Calls an LLM (a Claude API call, or literally a Claude Code subagent invocation) with the grouped evidence, prompted to propose candidate facts/decisions in the schema above — explicitly required to cite which evidence row(s) support each claim (this operationalizes the brief's §23 "don't trust the LLM as source of truth" principle: the LLM only ever proposes structured claims *from* evidence, never asserts unattributed facts).
3. Diffs proposals against current facts for the same `(scope, subject)`: no conflict → insert as new current fact, closing any prior row's `valid_until`; conflict → insert as `status=contradictory` and queue for human review rather than silently overwriting (this is the direct implementation of the brief's "reversal test" and "manual intervention test").
4. Applies Letta's **copy-on-write review pattern** (`research/letta-memfs.md`'s "dreaming"): write proposed changes to a staging table/branch first; auto-apply high-confidence non-conflicting updates, surface everything else for a quick human accept/reject pass before it becomes "current."
5. Regenerates the affected Markdown files and commits with an informative message.

This is the one place the research found no shortcut — no product ships an installable "evidence → confident, provenance-linked fact" pipeline — but mcp-memory-service's real (if differently-purposed) `consolidation/` module and Letta's dreaming are concrete reference implementations to read before writing this from scratch.

### 5. Query surface — a small custom MCP server

MCP is the settled transport choice (`research/basic-memory-mcp-servers.md`: Linux-Foundation-governed as of Dec 2025, ~97M SDK downloads/month, already how Claude Code/Cursor/other harnesses consume tools) — but no existing MCP memory server implements the operation set the brief actually wants (§24), so build a small one (a few hundred lines, Python or TS) exposing exactly:

```
memory.search(query, scope?)
memory.get_current_state(entity)
memory.get_history(entity)
memory.get_related(entity)
memory.get_decisions(scope)
memory.get_evidence(fact_id)
memory.get_project_context(project)
memory.get_recent_changes(scope, since?)
```

Each is a thin, mostly-mechanical query against the SQLite tables (plus a keyword search over the Markdown files to start — add embeddings later only if keyword search proves insufficient, matching the brief's §13 "cheapest/narrowest mechanism first" principle and §34's explicit "don't start by optimizing embeddings"). Implement the retrieval order from the brief's §25 directly in `memory.get_project_context` (identify project → load summary → current state → relationships → decisions → history → evidence only if asked) so progressive disclosure is enforced by the server, not left to caller discipline. Register this server with Claude Code and any other MCP-capable harness — this is what makes the system pass the brief's "agent replacement test" for real, rather than weakly/theoretically as Letta's raw-git-files approach does.

## Build order

| Phase | Deliverable | Effort (per research) |
|---|---|---|
| 0 | Capture layer live on one machine, all 5 sources landing in one append-only JSONL/SQLite store | ~1 weekend |
| 1 | `facts`/`evidence`/`decisions` schema created; Markdown directory convention established; git repo initialized for the memory store | 1-2 days |
| 2 | Consolidation job v1: evidence → proposed facts → staged → auto-apply/queue-for-review → Markdown regeneration + git commit | The main build; budget 1-2 weeks for a working v1, iterate after |
| 3 | MCP server exposing the 8 operations above, registered with Claude Code | ~1 weekend |
| 4 (optional, later) | Add self-hosted Graphiti as a secondary semantic/graph index; add local embeddings for Markdown/evidence text search; migrate hand-rolled bitemporal columns to XTDB if correctness bugs surface at scale | Only if Phase 0-3 usage reveals a real gap — don't start here |

This mirrors the brief's own §31 recommended exploration strategy (narrow real environment → raw events → derived memories → retrieval → semantic search → graph relationships, in that order) and its §34 explicit list of what not to optimize first.

## Verification

Use the brief's own benchmarks as literal acceptance tests once Phase 3 is done:
- **Vacation test** (§27): after a multi-week gap, ask `memory.get_project_context` for a real project and confirm it surfaces current state, recent changes, and open decisions without the human re-explaining anything.
- **Agent replacement test** (§28): query the MCP server from a *different* agent harness than whichever one populated it, and confirm it gets equivalent context.
- **Manual intervention test** (§29): change a config file or port by hand, outside any agent, and confirm the next consolidation run surfaces "something changed" via the capture layer rather than silently keeping stale state.
- **Reversal test** (§30): flip a setting A→B→A and confirm `memory.get_history` shows all three states, not just "current = A."
- Additionally: confirm every fact returned by `memory.get_current_state` has a working `memory.get_evidence` trail back to a real captured event, and that no fact is ever silently overwritten (check the `facts` table for any row lacking a `supersedes_id` chain where one should exist).
