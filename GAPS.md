# GAPS.md — gap checklist and build log

Living checklist of everything `memor` is missing relative to the brief
(`agent_memory_research_brief.md`) and `PLAN.md`, plus operational
requirements. Each item has a design sketch and acceptance criteria; status
is tracked in the master list and updated as we build.

Status legend: `[ ]` pending · `[~]` in progress · `[x]` done (date + note in Build log)

## Design review verdict (2026-08-24)

Architecture confirmed as intended: environment → evidence → derived
knowledge → query, SQL as temporal authority, Markdown as projection, MCP as
the agent surface. No redesign warranted; everything below refines within the
existing architecture. Per-capability judgment: capture (right tool choices;
tweaks F9–F11), store (correct, but subject/entity identity is the weak
spot — F5/F6/B2), consolidation (right rules/LLM split with graceful
degradation; correctness flaws F1–F3), query (right 8-op surface; F12),
human layer (ideal at this scale).

## Master checklist

### Wave 1 — trust (stop the memory from lying or dead-ending)
- [x] **A1** Active verification of current state (detects silent drift; also bootstraps state after reboot)
- [x] **B1** Temporal model for decisions (supersession of reversed choices)
- [x] **B3** Use the LLM's `component_hints` (prompt promises them, code ignores them)
- [x] **C1** Write surface for agents/harnesses (MCP `memory_add_*` tools)
- [x] **C4** Agent/harness interface contract (`AGENT-GUIDE.md`)
- [x] **F1** Same-second event ordering (`ORDER BY ts, rowid`)
- [x] **F2** Anchor LLM-fact `valid_from` to cited-evidence time
- [x] **F3** Per-project consolidation cursors (scoped runs must not skip other projects' events)
- [x] **F4** Correction/forget operations (`memor correct`, `memor forget`)

### Wave 2 — durability & operations
- [x] **F8** Secrets redaction before store/LLM send
- [x] **C3** LLM sync: provider abstraction, endpoint probe, on-demand `memor llm sync`, optional daemon auto-sync
- [x] **A2** Retention/compaction for raw evidence
- [x] **D1** Export/import + machine registry (portable store)
- [x] **C2** `memor run <cmd>` — capture output for manual commands
- [x] **E1** `doctor` depth: integrity check, checkpoint, store health, LLM probe
- [x] **F7** Failed-command rule (exit ≠ 0 → `problem` fact, rules-only troubleshooting)
- [x] **F9** Journal backfill since last daemon start (reboot scenario)
- [x] **F10** FS watcher: content/diff only for config-like paths (store bloat)
- [x] **F11** Git remote URL → project mapping (identity across clones/machines)
- [x] **F12** `get_project_context` accuracy + size control
- [x] **G4** One-shot `memor setup` onboarding

### Wave 3 — intelligence & scale
- [x] **A3** Episode/task memory (session summaries, lessons)
- [x] **E2** Bounded evidence windows (no silent overflow past the 200/5000 caps)
- [x] **C5** LLM extraction hardening: tests, prompt versioning, failure accounting
- [x] **B2** Entity resolution — redesigned: relations keyed on entity ids (not name strings)
- [x] **F5** Subject canonicalization (vocabulary + re-targeting at apply time)
- [x] **F6** Near-duplicate state-claim merge (stop supersede ping-pong)
- [x] **E3** Robustness & packaging: capture-module tests, CI, README, timestamp/FTS polish
- [x] **D2** Cross-machine queries (federation over imported stores)
- [x] **F13** Session identifiers (atuin/tmux) in shell events
- [x] **G1** `memor timeline` — merged chronological events+facts+decisions view
- [x] **G2** Periodic human digest (changes, open reviews, stale items)
- [x] **G3** Surprise/anomaly surfacing (extends A1 verify pass)
- [x] **G5** Session bootstrap env snapshot (env-var archaeology)
- [ ] **P4** Semantic/embedding search — **gated: deferred by design.** Build only when real usage shows retrieval misses (store zero-result `memory_search` queries as events; revisit if the pile grows). Everything else in this file is expected to be built.

## Known limitations (accepted, mitigated in code)

- **Second-granularity timestamps.** The event format stays
  `%Y-%m-%dT%H:%M:%SZ` (stable for existing stores). All window/cursor
  logic uses seen-sets and/or strict comparisons so same-second bursts are
  ordered (rowid) and never skipped or double-counted (F1, F3, E2, A3, G2).
  Millisecond timestamps would be a schema break with no user-visible gain
  at this scale.
- **Episodes are rule-sketches** (deterministic, provider-free). LLM-narrated
  episode summaries are a natural C3 extension (feed the episode window
  through the sync queue) — not built; the rule sketch is complete and
  evidence-linked.
- **Cross-machine continuity is import-based** (D1/D2), not live federation:
  a store from box A is merged into box B's store via `memor import
  --as-machine`. A live multi-box topology (peers syncing) is out of scope
  until needed.
- **`timeline`/`current` component filtering is fuzzy** (substring of
  commands + project match) — good enough for navigation; exact
  component-scoped history comes from `memory_get_history` with
  `component:x` syntax.
- **LLM extraction quality depends on the model.** The guardrails
  (evidence-citation enforcement, confidence ceilings, review queues,
  redaction) bound the damage; a weak model yields fewer/weaker derived
  facts, never invented current facts.

## Build log

<!-- newest first: date — item — what changed — verification -->

- 2026-08-25 — **E3, C4 + final verification** (chunk 10) — E3: docker capture module now tested (snapshot-diff drift detection incl. appeared/removed/field changes via a threaded fake, garbage-tolerant event tailing); the stdio roundtrip test no longer hardcodes a machine path (repo-relative venv, `which` fallback for CI installs) and now asserts the full 17-tool surface; `README.md` added (architecture, doc map, quick commands); GitHub Actions workflow runs the suite on 3.10–3.12 via `uv sync --frozen`. C4: `AGENT-GUIDE.md` — the interface contract any harness can be pointed at: what memor promises (trust rules), the one-page data model, read patterns (session pattern, entity syntax, CLI twins), the write contract (cite evidence, confidence ceilings, supersede-don't-overwrite, decisions need their why), logging recipes (changes/failures/lessons/todos/hypotheses/session-end), three worked examples, and what memor won't do. G1's CLI half closed (`memor timeline <name> --hours N`). Added "Known limitations (accepted)" section: second-granular timestamps (mitigated), rule-sketch episodes, import-based cross-box continuity, fuzzy component filtering, model-dependent LLM quality. **Final verification: full pytest suite green (81 tests) + live end-to-end smoke on a clean store** (init → projects → shell/git capture → consolidation → verify → episode → digest → timeline → llm status → export/import roundtrip → compact → doctor all-pass; brief benchmarks: reversal A→B→A history, catch-me-up context, evidence trails, cross-harness MCP read/write all confirmed). **P4 stays deferred** per its own gate (no observed retrieval misses yet).
- 2026-08-25 — **G1, G2, G3** (chunk 9) — `memor timeline [name] --hours N` / MCP `memory_get_timeline`: the literal "how did we get here" view — raw events, fact transitions (became-current / stopped-being-true with before-state) and decisions merged into one chronological line, every entry carrying a drill-down id. `memor digest` / daemon `digest_interval` (default 24h): `system/DIGEST.md` (git-committed) pulls the human into the review loop — changes since last digest (strict window edges so boundary items never double-count), decisions, surprises & open items, activity by source, episodes, LLM-queue state. G3 surprise detection (three heuristics): a CONFIRMED fact overturned by weaker or re-observed (`verify`) evidence → `surprise` review item; a verification change with no human/agent activity in the preceding 10 minutes → "unexplained" flag + review item; the same subject in the contradiction queue ≥3 times → "recurring conflicts" escalation. Verified: 79/79 tests incl. `TestChunk9` (timeline ordering/merge, digest write+window advance, all three surprise heuristics incl. the explained-negative control).

- 2026-08-25 — **E2, C5, A3, D2** (chunk 8) — consolidation and LLM sync process bounded windows (`consolidate_batch`, default 500): the per-scope cursor advances only when the backlog is fully consumed and the report shows `+N left in queue`, so a busy day is drained over passes with nothing skipped (the seen-sets keep re-runs idempotent). LLM records carry `prompt_version` (audit: which prompt produced which memory); extraction failures already leave `llm_extraction_error` events; new tests cover fenced/malformed JSON, bad confidence kinds, over-long claim/reason truncation, uncited-claim drops. Episode memory (`episodes` table + `close_episode`): a work window is condensed into one record — objective (first command), deterministic activity sketch (event mix, top commands, facts born/changed, decisions), lessons (problem facts + open contradictions) — with `episode.cursor`/`episode.seen` (same-second-safe), Markdown projection (`reference/projects/<p>/episodes.md`), `memor episode close` / `memor episodes [--project --since-hours]`, MCP `memory_get_recent_episodes`, and a daemon loop that auto-closes when a session goes idle (`episode_idle_gap`, default 30 min). `memory_get_global_context` / `memor global` answers "what's on each machine?" across imported stores (D2). Verified: 74/74 tests incl. `TestChunk8`.

- 2026-08-25 — **A2, D1, E1, G4** (chunk 7) — `memor compact [--dry-run]`: retires raw events older than `retention_months` (default 12, 0=off) that are both already consolidated (`seen` sets) and unreferenced by any fact/decision evidence (provenance survives compaction forever); older monthly JSONL files are gzip-archived, FTS index rebuilt, WAL checkpointed. `memor checkpoint` = explicit WAL checkpoint. `memor export [-o FILE]`: portable bundle (SQLite online backup + memor.json + raw events + Markdown projection + manifest); `memor import BUNDLE [--as-machine M]`: merges into this store (uuid ids make re-import additive), reattributes the bundle's machine scope for cross-box continuity, merges the project registry, regenerates the Markdown projection. `doctor` now reports `PRAGMA integrity_check`, store size/age, FTS health, per-source event mix, duplicate-event heuristic, retention eligibility, and the full LLM status (provider/availability/pending/autosync). `memor setup [dirs]` is one-shot onboarding: init, git-repo discovery (project name from the remote slug so identity survives re-clones), Claude wiring when present, shell-hook instructions, daemon via systemd user unit or background. Verified: 70/70 tests incl. `TestDurability` (compaction rules incl. evidence protection + FTS rebuild, export→import→re-import roundtrip with machine reattribution, doctor depth, setup discovery).

- 2026-08-25 — **C1, F12** (chunk 6) — write surface: `memory_add_fact` / `memory_add_decision` / `memory_add_relation` / `memory_add_note` / `memory_correct` / `memory_retract` MCP tools (CLI parity: `memor correct`/`forget`, `remember`) — writes flow through the same validated apply pipeline: cited evidence must exist (else the write is rejected), uncited writes land `unknown` + review queue and can never become current on their own, conflicts queue instead of overwriting; explicit agent/human assertions stand on their own (confidence-ceilinged) while LLM/rule candidates still require real citations (§23 preserved). `get_project_context` now renders true transitions (what stopped being true shows its before-state) and accepts `max_chars` (CLI `--max-chars`), dropping low-priority sections (decisions → relations → evidence) first when capped. Verified: 66/66 tests incl. `TestWriteSurface` + stdio roundtrip.

- 2026-08-25 — **C3** (chunk 5) — LLM sync exactly per the user-specified R1–R7: provider abstraction (`extractor_for` → Anthropic API or any OpenAI-compatible local server via `llm_base_url`, urllib-only, no new dependency); availability probed at the configured endpoint (`GET {base}/models`) before any attempt; `memor llm sync [--dry-run|--since]` processes the pending queue on demand; the queue is durable (`llm.cursor`/`llm.seen`, independent of the rules cursor) so records are never skipped while the LLM is down — `memor llm status` shows provider/available/pending; daemon auto-sync only when `llm_autosync` is on (default off), checking queue-then-availability every `llm_sync_interval` (600s); findings flow through the same validated apply pipeline (R6); `cmd_consolidate` and the daemon now share the one `extractor_for` path. Verified: 59/59 tests incl. `TestLLMSync` against a live fake OpenAI-compatible server (sync→drain→noop, unavailable report with queue preserved, not-configured path, autosync default off, uncited-claim drop).

- 2026-08-25 — **A1** (chunk 4) — new `memor/capture/verify.py`: a verification pass re-observes ground truth (`ss` listeners, `systemctl is-active` in system+user scopes, docker container statuses) and reconciles current state facts — stale claims are superseded with `source_type="verify"` and evidence pointing at a `verify_run` event; observed state with no fact yet is bootstrapped (so "what is running" is answerable immediately after a reboot). Only re-observable machine-scoped state (`service:*`, `port:*`, `container:*`) is ever touched; decisions/lessons/human facts are untouched. Rules now emit one canonical service subject form (`service:<stem>`), shared with verification. `memor verify` runs a pass on demand; the daemon runs one every `verify_interval` (default 300s). Verified: 54/54 tests incl. `TestVerification` (stale supersession + evidence, bootstrap, no-op on consistent state, container transitions).

- 2026-08-25 — **F7, F9, F10, F13, G5, C2** (chunk 3) — `extract(events, shell_failures=)` emits a `problem` fact for any agent command with exit≠0 (reason carries the last 200 chars of output+stderr; interactive-shell failures opt-in via `problem_capture_shell`); journal source backfills `--since <last daemon run>` (meta `journal.since`, fallback: last `daemon_started` event) before following, with a shared `emitted` set deduping backfill-vs-follow; fs watcher stores full content/diff only for config-like paths (shared `is_config_path` predicate, `MAX_CONTENT` 256KB→32KB), hash/size for everything else; shell events carry `tmux_pane`/`atuin_session*`/`term_program`/`ppid`; G5: first command of a session emits a redacted `session_env` snapshot from `os.environ` (one per session, `env_sessions` tracking); `memor run <cmd>` wraps any command with full output capture (transparent to the terminal) and passes through the exit code. Verified: 50/50 tests incl. `TestCaptureUpgrades`.

- 2026-08-25 — **B1, B2, F5, F6, F4** (chunk 2) — decisions now carry `status`/`valid_until`/`supersedes_id`/`source_type` (additive migration for old stores): a reversed choice supersedes the prior decision instead of piling up; identical re-statements no-op; low-confidence conflicts queue for review. Relations are keyed on entity ids (`subject_ent`/`object_ent`, one-time backfill of legacy name-only rows) so "Qwen Server" and "qwen-server" are one node; `relations_for` BFSes over entities with name fallback. New candidates are re-targeted onto the scope's existing subject vocabulary (F5) so one thing grows one chain; near-duplicate state claims (token Jaccard ≥ 0.8, same kind) merge as no-op or refinement instead of ping-ponging the supersede chain (F6). `memor correct <fact> --claim` / `memor forget <fact> [--reason]` (+ store `correct_fact`/`forget_fact`, new `retracted` status kept out of all "current" surfaces but preserved in history/audit). MCP `get_decisions` returns status/valid_until/supersedes_id; Markdown marks superseded/retracted decisions. Verified: 43/43 tests incl. `TestModelEvolution`.
- 2026-08-25 — **F1, F2, B3, F3, F11, F8** (chunk 1) — `events_since`/`recent_events` order by `(ts, rowid)`; derived records anchor `valid_from` to the latest cited-evidence ts (`_evidence_ts`); `apply_component_hints` registers the LLM's component hints as entity descriptions (never clobbers); consolidation cursors are per-project (`consolidate.cursor.<project>`) so scoped runs can't skip other projects; `normalize_remote` + `find_project_by_remote` + `ProjectRef.remotes` + `add-project --remote` give repos stable project identity across clones/machines; `redact_text`/`_redact_deep` scrub bearer/secret/PEM/AWS shapes at the single `append_event` chokepoint (SQLite + JSONL), `redact`/`redact_extra_patterns` in config, `_redacted` marker in payload; LLM extraction failures now leave a `llm_extraction_error` audit event (first part of C5). Verified: 38/38 tests incl. new `TestDesignReview` suite.

---

# Wave 1 — trust

## A1 — Active verification of current state

**Problem.** A fact like "service X is running" or "port 8080 is bound by
python" stays `current` forever unless a *captured* event contradicts it. If
the service dies silently (crash, reboot, manual `kill`, container removed
off-band), the memory keeps asserting the old state — the brief's §29
("should not falsely preserve the old state") is only half-met today: it
holds for changes that leave a trace, not for ones that don't.

**Design.**
- A `verify` job (daemon thread on `verify_interval`, default 300s; also
  `memor verify` on demand) re-reads ground truth from the same sources
  capture uses: `ss -tulpn` (listeners), `systemctl is-active` /
  `--user` for known services, `docker ps -a` for known containers.
- It compares against current `state`-kind facts whose subjects match what
  was observed (`service:*`, `port:*`, `container:*`) — and **only** those;
  it never touches human-asserted or LLM-derived knowledge it cannot
  re-observe.
- Mismatch → `supersede` with `source_type="verify"`,
  `reason="verified state changed"`, evidence = the verification snapshot
  event. Agreement → no-op (optionally a low-frequency `verify_ok` audit
  event).
- Facts whose subject can't be re-observed (e.g. a service removed from
  systemd) are flagged `unknown` only after N consecutive failed lookups,
  never silently deleted.

**Acceptance.**
- Start a service (captured), then `kill -9` it outside any agent; next
  verify pass supersedes "is running" → "is stopped (verified)" with a
  working evidence trail.
- `memor verify` on demand does the same; daemon auto-verifies on interval.
- No fact outside the observable set is modified by verification.

## B1 — Temporal model for decisions

**Problem.** The `decisions` table is append-only: no `status`,
`valid_until`, or `supersedes_id`. If we chose vLLM, then reversed to
llama.cpp, both rows sit side by side forever; `memory_get_decisions` can't
say which choice is live, and the brief's reversal test (§30) only works
for facts.

**Design.**
- Add `status` (`current`/`superseded`), `valid_until`, `supersedes_id` to
  `decisions` (migration is an additive `ALTER TABLE` — SQLite).
- When consolidation/LLM/human records a new decision for the same
  `(scope_project, scope_component, subject)` with a conflicting `choice`,
  the prior current decision is closed (`status=superseded`,
  `valid_until` set) and linked via `supersedes_id`. Identical re-statements
  are no-ops (evidence merged). Low-confidence conflicts go to the review
  queue, same as facts.
- `memory_get_decisions` returns current-first with status; Markdown
  projection marks superseded decisions as history.

**Acceptance.** Two contradictory decisions on one subject → exactly one
current; history shows the reversal with dates; `memory_get_evidence` works
for both.

## B3 — Use the LLM's `component_hints`

**Problem.** The extraction prompt asks for `component_hints` (component →
one-line description) "so the memory hierarchy stays legible", but
`_candidates_from` (memor/consolidate/extract.py) never reads the key. The
hierarchy is silently less legible than the prompt promises.

**Design.** After candidate extraction, upsert each hint:
`store.upsert_entity(component, description=hint, project=scope.project)`
(Coalesce-style: only fill missing descriptions, or refresh on change).
Hints with a component that produced no candidates are still recorded —
they exist to name the hierarchy.

**Acceptance.** Unit test: canned LLM JSON with `component_hints` →
entities exist with descriptions; `get_related`/Markdown show them.

## C1 — Write surface for agents and harnesses

**Problem.** The MCP surface is read-only (`memory_get_*` / `memory_search`).
An agent that learns something mid-task can only hope consolidation picks it
up from raw output. The agent-replacement test needs *bidirectional*
memory: Agent B must be able to contribute in the same shape it reads.

**Design.** New MCP tools (and CLI parity), all flowing through the same
validated apply path as consolidation:
- `memory_add_fact(subject, claim, scope?, kind?, confidence?, reason?,
  evidence?)`
- `memory_add_decision(subject, choice, alternatives?, reason?, confidence?,
  evidence?)`
- `memory_add_relation(subject, relation, object, evidence?)`
- `memory_note(text, ...)` → thin wrapper over `memory_add_fact`
  (`kind` from caller), matching `memor remember`.
Rules:
- `source_type="agent"`, `origin` recorded from the caller if provided.
- Evidence ids, if given, must exist (else the write is rejected with an
  error — never stored unattributed); without evidence, the record lands
  `status=unknown` and enters the review queue (same discipline as
  low-confidence LLM output). Agent-asserted claims without evidence never
  become `current` on their own.
- Contradictions with current state → `status=contradictory` + review queue,
  never silent overwrite.
- Writes regenerate the Markdown projection.

**Acceptance.** MCP client test: add fact (with cited evidence) → current;
add contradictory fact → queued, old stays current; add uncited claim →
unknown + review row. `AGENT-GUIDE.md` documents the exact contract.

## C4 — Agent/harness interface contract (`AGENT-GUIDE.md`)

**Problem.** Today, teaching a new harness how to use memor requires
reading the code. The user should be able to point any tool/agent at one
file and have it operate the memory correctly — read, write, and log —
with no out-of-band instruction.

**Design.** One document, `AGENT-GUIDE.md`, covering:
1. **What memor is** — one paragraph: evidence journal → derived, temporal,
   evidence-backed project memory; trust rules (facts carry confidence and
   provenance; "why do you believe this?" is always answerable).
2. **Data model** — scopes (project/component/machine), fact kinds,
   confidence scale, statuses, supersedence, evidence pointers.
3. **How to read** — MCP tools with one-line semantics + a recommended
   session pattern (start with `memory_get_project_context`, drill with
   `get_current_state`/`get_history`/`get_related`, pull evidence last),
   plus CLI equivalents for non-MCP harnesses.
4. **How to write** — `memory_add_*` tools (from C1) + `memor remember`,
   with the evidence-citation rule stated as a hard requirement: *only
   record what you observed or were told; cite the event id when you have
   one; mark guesses; never rewrite history — supersede instead*.
5. **How to log work** — recipes: decisions (choice + reason +
   alternatives), failed approaches (`kind=problem`/`lesson`), open
   questions (`kind=todo`), session-end summary habits; what is captured
   automatically (so the agent doesn't double-record commands).
6. **Working examples** — 3–4 realistic request/response pairs.
7. **Drift guard** — a "verified against memor vX" footer; the file is
   regenerated/reviewed whenever the tool surface changes.

**Acceptance.** A harness given only this file can: catch itself up on a
project, answer a "why is X configured this way" question with provenance,
and record a new decision correctly — without reading source code.

## F1 — Same-second event ordering

**Problem.** Event timestamps are second-granular; `events_since` orders by
`ts` alone. Several events in the same second (e.g. `start; stop; start`
typed quickly, or a burst of hook events) consolidate in arbitrary order →
the wrong row can end up `current`.

**Design.** `ORDER BY ts, rowid` in `events_since`/`recent_events` (rowid =
insertion order = causal order for a single hook pipeline). Existing stores
are unaffected (rowid is already stable).

**Acceptance.** Three same-second start/stop/start events → history chain
matches command order; test with identical timestamps.

## F2 — Anchor LLM-derived `valid_from` to evidence time

**Problem.** `_candidates_from` never sets `valid_from` for LLM facts, so
they are stamped with *apply time*. A `memor llm sync` (C3) processing a
queue of events from days ago would date today's-yesterday's findings "now"
— breaking the brief's core "when did this become true" requirement.

**Design.** In `apply_candidates`, for candidates lacking `valid_from`, set
it to `max(ts of cited evidence)` (deterministic, no LLM involvement).
Optionally also ask the LLM for it in the prompt, but never trust it over
the evidence anchor.

**Acceptance.** LLM candidate citing events from T1 → fact's `valid_from`
== T1, not sync time.

## F3 — Per-project consolidation cursors

**Problem.** `consolidate.cursor` is one global timestamp, but scoped runs
(`--project X`) read only X's events. A scoped run advances the global
cursor to X's newest event, permanently skipping other projects' events
that fall between the old cursor and the new one.

**Design.** Cursor per scope: `consolidate.cursor` (global runs) plus
`consolidate.cursor.<project>` for scoped runs; `run_consolidation` reads
and advances the cursor matching its scope. The `seen` set stays global as
the idempotency backstop.

**Acceptance.** Interleave demo/web events in time; run `--project demo`;
next full run still picks up web's events; nothing double-applied.

## F4 — Correction and forget operations

**Problem.** Brief §36: "Can incorrect memories be corrected?" Today the
only remedy is remembering a replacement fact. No direct operation exists.

**Design.**
- `memor correct <fact_id> --claim … [--reason …]` → supersede the fact
  with a human-corrected claim (`source_type="human"`), preserving the
  original row and its evidence.
- `memor forget <fact_id> --reason …` → `status="retracted"` (row,
  evidence, and supersedes chain preserved for audit; excluded from
  `current`/`context`/Markdown). Retracted facts are visible in
  `memor history` and in `get_current_state`'s audit section.
- MCP parity: `memory_correct`, `memory_retract` (write-side, C1 family).

**Acceptance.** Correct a wrong fact → new current fact supersedes it,
history shows both; forget → fact disappears from all "current" surfaces,
still present in history with the retraction reason.

---

# Wave 2 — durability & operations

## C3 — LLM sync: on-demand, provider-agnostic, auto-optional

**Requirements (user-specified).**
- R1: The LLM (local inference server, or API) may be down at any time;
  capture, rules-based consolidation, and all read/write operations must
  keep working without it.
- R2: Records not yet processed by the LLM stay queued (durable), so
  nothing is ever skipped just because the LLM was offline.
- R3: An explicit command — `memor llm sync` — processes the pending queue
  when the user runs it with the LLM server online.
- R4: Before attempting, probe LLM availability at the configured
  endpoint (the "expected port"); report cleanly if it's down
  (`LLM unavailable — N records pending`).
- R5: Optional auto mode, **off by default**: when the config flag is on,
  the daemon periodically checks (a) are there unprocessed records,
  (b) is the LLM up — and if both, runs the sync. Never auto-sync when the
  flag is off.
- R6: Generated findings are recorded through the same validated apply
  pipeline as everything else (evidence citation enforced, contradictions
  queued, nothing silently overwritten).
- R7: Endpoint is configurable: local OpenAI-compatible server
  (`llm_base_url` incl. port + `llm_model`) and/or the Anthropic API.

**Design.**
- Provider abstraction: `BaseExtractor` with `available() -> bool`
  (probe: `GET {base}/v1/models` with short timeout for OpenAI-compatible;
  SDK/key check for Anthropic), `extract(scope, events, current) ->
  [Candidate]`. Existing `ClaudeExtractor` becomes one backend; new
  `OpenAICompatibleExtractor` (plain HTTP, no SDK dependency) covers local
  servers.
- **Separate LLM cursor**: `llm.cursor` + `llm.seen` in `meta`, tracked
  independently of the rules cursor. The rules pass (already) runs on
  every event; the LLM pass processes `[llm.cursor → consolidate.cursor]`
  — i.e. exactly the events the LLM hasn't seen. Re-runs are idempotent
  (apply dedupes by `(scope, subject, claim)`).
- `memor llm sync [--since] [--dry-run]`: probe → if down, print pending
  count and exit; if up, process the queue, apply, regenerate Markdown,
  advance the LLM cursor.
- Daemon: `llm_autosync` (bool, default `false`) + `llm_sync_interval`
  (default 600s) — a thread that, only when the flag is on, checks
  pending-count + availability each interval and syncs.
- `memor doctor` reports: provider, endpoint, availability, pending count.

**Acceptance.**
- LLM down: `memor llm sync` → clean "unavailable, N pending"; rules path
  and all queries unaffected.
- LLM up: sync processes the queue once; second run is a no-op
  ("nothing pending").
- Flag off: daemon never calls the LLM (asserted in test with a stub).
- Flag on + stub server: daemon syncs when the stub comes up; does nothing
  while it's down.
- Provider tests run against a fake HTTP server (no network, no key).

## A2 — Retention / compaction

**Problem.** Raw events and the JSONL mirror grow without bound; the brief
is explicit that we should not "capture everything forever".

**Design.**
- `retention_months` config (default 12) + `memor compact [--dry-run]`.
- Compaction deletes `raw_events` rows older than the cutoff **only if**
  (a) their id is in the consolidated `seen` set, and (b) they are not
  referenced by any `evidence` row — referenced events are preserved
  (provenance must survive "why do you believe this?" forever). Before
  deletion of a JSONL line, the old monthly file is gzip-archived
  (`events/archive/YYYY-MM.jsonl.gz`) so nothing is unrecoverable.
- Facts/decisions/evidence are never deleted by compaction (Level 3 is the
  durable layer by design).
- Doctor reports store size, oldest event, and "eligible for compaction".

**Acceptance.** Seed 14-month-old consolidated + unreferenced events →
`compact` removes them, archives JSONL, keeps referenced + recent events;
`get_evidence` still resolves every surviving fact.

## D1 — Export / import + machine registry

**Problem.** The store is one machine's SQLite + files. The brief's GLOBAL
level (user, machines, projects across boxes) and "what do I have on which
machine" need at minimum a portable store.

**Design.**
- `memor export [project] > bundle.tar.gz`: SQLite online backup,
  `memor.json`, `events/` (unarchived), `system/`+`reference/` trees,
  manifest (version, machine, exported_at).
- `memor import <bundle> [--as-machine NAME]`: restores into this store
  (new or existing), remapping `scope_machine` to `NAME` when given;
  id collisions are impossible (uuid ids) — re-import is additive.
- Machine registry: machines become first-class entities
  (`kind="machine"` — already done in consolidation); `system/INDEX.md`
  gains per-machine sections so the Level-0 view answers "which boxes
  exist and what's on them".

**Acceptance.** Export on box A → import on box B (different data dir) →
`memory_get_project_context` returns A's history with machine labels
intact; double-import doesn't duplicate facts.

## C2 — `memor run <cmd...>`: output capture for manual commands

**Problem.** The shell hook records command + exit + duration but not
output (only the Claude hook captures output). "What did that migration
print?" is unanswerable for manual work — and output is often where the
reason for a change lives.

**Design.** `memor run <command...>`: exec in the user's shell env,
capture stdout/stderr (truncated to `max_output_chars`), exit code, cwd,
project (via `resolve_project`), machine, and the current session id
(`$MEMOR_SESSION` if set, else a fresh one); append one event
(`source="shell"`, `kind="command"`, `payload.output` populated — the same
shape the Claude hook produces, so rules/LLM treat it identically);
echo the output to the terminal as usual (it's a wrapper, not a black box).

**Acceptance.** `memor run systemctl --user status qwen` → event carries
truncated output; consolidation/LLM can cite it; exit code is passed
through.

## E1 — `doctor` depth + store hygiene

**Problem.** `doctor` checks liveness only; nothing checks store health.

**Design.** Add to `memor doctor`: `PRAGMA integrity_check`, FTS status
(warn, not fail, if degraded), store size on disk, oldest/newest event ts,
event counts by source, duplicate-event heuristic (same
command+machine within 1s), open-review count, LLM provider probe +
pending count (C3). New `memor checkpoint` → `PRAGMA wal_checkpoint(TRUNCATE)`.

**Acceptance.** Corrupt a copy of the db → doctor reports it; healthy
store reports all-green with the new metrics.

## F7 — Failed-command rule (rules-only troubleshooting)

**Problem.** Rules ignore exit codes and output, so in rules-only mode a
failed command leaves no derived trace — the brief's "have I encountered
this error before?" (§26) is unanswerable without an LLM.

**Design.** Rule: event with `exit`/`exit_code` != 0 → `kind=problem`
candidate, `subject` from command heuristic (tool name), claim
"command `…` failed (exit N)", evidence = the event. Restrict to
`source="agent"` by default (agent runs are deliberate actions whose
failures matter); `problem_capture_shell: bool` config to extend to shell
events (noisier — every failed `grep`).

**Acceptance.** Agent Bash event with exit 1 + stderr → a `problem` fact
appears after consolidation; `memory_search "<error text>"` finds the event
and the fact.

## F8 — Secrets redaction

**Problem.** Command lines and captured output (tokens, `password=…`,
private keys, bearer headers) are stored verbatim and sent verbatim to the
LLM — which, for the Anthropic API default, leaves the machine.

**Design.** A redaction pass applied at event append (single chokepoint in
`Store.append_event`'s `command`/`payload` text fields) and before any LLM
send: regex set for bearer tokens, `password/secret/token/api_key=…`,
PEM blocks, AWS keys, high-entropy strings; replaced with
`[REDACTED:<type>]` and a `redacted: [types]` payload marker so consumers
know. Configurable (`redact: bool` default on; pattern overrides in
config). Redaction is logged (count by type) in the event payload, never
silently.

**Acceptance.** `curl -H "Authorization: Bearer sk-…"` command + output
containing a key → stored/LLM-bound text contains only `[REDACTED:token]`;
doctor reports redaction stats.

## F9 — Journal backfill on daemon start

**Problem.** `journalctl -f` only streams entries from daemon start; after a
reboot or daemon restart, boot-time service history is never captured.

**Design.** On daemon start (journal source), first run
`journalctl -o json --since <ts of last daemon_started event>` and ingest
matching entries, then follow with `-f`. Idempotent via the existing
throttle + seen semantics (duplicate suppression by unit:message key).

**Acceptance.** Stop daemon, reboot, start daemon → boot-time
"Started X.service" entries are in the store exactly once.

## F10 — FS watcher content policy

**Problem.** The watcher stores up to 256KB of file content (+ old content,
+ diff) in every change event under watched paths — including paths like
`~/.config` where rules only ever parse config-like files. Store bloat with
zero retrieval value.

**Design.** Capture `content`/`diff`/`old_content` only when the path
matches the rules' config heuristic (same `is_config` predicate, exported
from `rules.py`); all other paths get hash+size+action only. Lower
`MAX_CONTENT` to 32KB (config files are small; bigger "config" is a
different problem).

**Acceptance.** Change a `~/.config/…/cache.bin`-style file → event has
hash but no content; change a `.service` file → diff present; store size
growth proportional to config changes only.

## F11 — Git remote URL → project mapping

**Problem.** Project resolution is registered-path prefix or git-dir-name
slug. The same repo cloned to different paths (other machine, other
clone) resolves to a different project id — breaking cross-machine
continuity. The PLAN's own heuristic list includes "git remote → project"
and it was never built.

**Design.** `ProjectRef.remotes: list[str]` (e.g. `github.com:me/ai`);
`resolve_project` checks, for a git root, its `remote.origin.url` (read via
`git config` or `.git/config`) against registered remotes (normalize
`https://` vs `git@` vs `.git` suffix). Precedence: explicit path match →
remote match → dir-name slug fallback. `add-project --remote …` registers
them; doctor shows unmatched repos (repos seen in events whose remote
matches no project) as onboarding hints.

**Acceptance.** Same remote registered once → clones at two paths both
resolve to the registered project.

## F12 — `get_project_context` accuracy + size control

**Problem.** (a) "Changes in the last 14 days" lists only *current* facts
newly became current — a service that stopped shows the stop without the
before-state, mislabeling history as "changes". (b) No size knob: a large
project's context can blow an agent's context window.

**Design.** (a) Build the section from the window's *transitions*: current
facts with `valid_from` in window plus superseded facts in window, rendered
`"was X → now Y"`. (b) `max_chars` (or `verbosity: brief|standard|full`)
parameter on `memory_get_project_context`; truncate sections in priority
order (open items > current state > recent > rest), ending with a pointer
to `memory_search`/`memory_get_history` for the truncated remainder.

**Acceptance.** A→B→A in the window renders all three states; a 500-fact
project returns ≤ `max_chars` with a truncation notice.

## G4 — One-shot `memor setup` onboarding

**Problem.** Setup is four separate steps (init, add-project, hooks,
daemon); value is zero until all are done.

**Design.** `memor setup [dir …]`: init if needed; scan given dirs (default
`~/work`, `~/projects`) for git repos, propose projects (name from remote
slug), register; install Claude hook + MCP if `claude` is on PATH; print
the exact shell-hook line for the detected shell; offer to start the daemon
(and install the systemd unit). Every step idempotent, every step skippable
(`--no-daemon`, …), non-interactive with flags for scripting.

**Acceptance.** Fresh machine: `memor setup ~/work` → doctor all-green
(minus sources absent on that box) with projects registered.

---

# Wave 3 — intelligence & scale

## A3 — Episode / task memory

**Problem.** The brief's §14 Task/Episode scope (objective → actions →
observations → decisions → result → lessons) doesn't exist. "What did I
work on last week?" must be reconstructed from raw events.

**Design.** `episodes` table (`project, machine, session(s), started_at,
ended_at, objective, summary, lessons JSON, event range`). A session-end
trigger (daemon: new session start after idle gap, or explicit
`memor episode close`) selects the session's events + resulting facts and
produces an episode record — LLM-written when available (queues under C3
when not), rule-sketched otherwise (commands touched, facts changed,
decisions made). `reference/projects/<p>/episodes.md` projection; new MCP
tool `memory_get_recent_episodes(scope, since?)`.

**Acceptance.** A work session's events → one episode with a summary and
≥1 lesson that traces back to event ids; `get_recent_episodes` returns it.

## E2 — Bounded evidence windows

**Problem.** Consolidation reads up to 5000 events and the LLM sees at most
200 per group; on a busy day, evidence past the cap is silently never
processed (cursor jumps to the max ts of what *was* seen).

**Design.** Windowed processing: advance the cursor only to the end of the
window actually consumed (`batch` config, default 500); the remainder waits
for the next pass (rules and LLM cursors both become window-cursors). No
event is ever skipped, just deferred. Doctor surfaces "events pending
beyond last window".

**Acceptance.** Seed 2000 events, batch 500 → four passes consume all;
cursor == max ts only after the last; nothing lost.

## C5 — LLM extraction hardening

**Problem.** The LLM path is untested (rules path is fully tested),
single-provider, and prompt changes are invisible in stored data.

**Design.** Mock/fake-HTTP tests for both providers (JSON parse failures,
truncated output, non-JSON prose, over-long claims, bad confidence
values); store `prompt_version` on proposals; per-run failure accounting
(event → `llm_extraction_error` with truncated error) so silent
degradation to rules-only is visible in the store.

**Acceptance.** Provider tests pass offline; a broken endpoint leaves an
audit event, not a silent gap.

## B2 — Entity resolution (redesigned: one identity system)

**Problem.** Two identity systems that don't compose: entities are
slug-keyed rows, while `relations` are plain name strings and facts carry
scope strings with no entity link. `relations_for("Qwen")` never matches a
relation stored as `qwen-server`; `get_related` only connects names the
extractors happened to emit.

**Design.**
- Relations keyed on **entity ids** (with denormalized display names);
  writers resolve names → entities first (exact → alias → fuzzy).
- Fuzzy/alias matching at upsert (normalized comparison + stored aliases,
  plus LLM-suggested aliases from B3/C3 hints).
- Facts gain optional `entity_ids` linking to resolved entities;
  `get_related`/`get_current_state` expand through those links.
- Pairs with F5 (subject vocabulary) — together they make "what is this
  thing" a stable answer instead of a string accident.

**Acceptance.** "Qwen server" / "qwen-server" / "qwen" converge on one
entity; a fact about any alias is found via the canonical entity; a
relation added under any alias is found from the canonical name.

## E3 — Robustness & packaging

- Tests for the remaining capture modules (`fs_watch` with a fake
  watcher event, `docker_events` with fake subprocess output, `journal`
  with canned JSON lines).
- CI (pytest on push/PR) and a `README.md` (pointer: brief → findings →
  plan → getting-started → gaps → agent guide).
- Timestamps: keep the second-granularity format (stable for existing
  stores) but add a monotonic tie-break on `events_since` ordering
  (`ts, rowid`).
- FTS degradation: surfaced in `doctor` (see E1).

**Acceptance.** `pytest` covers every module in `memor/` except the
provider HTTP layer; CI green on a clean checkout.

## D2 — Cross-machine queries

**Problem/Design.** After D1, multiple imported stores coexist in one db.
Add a global level: `memory_get_context_global` (or `memor report
--global`) merges per-machine current state into the brief's Level-0 view;
`get_related` expands across machines for shared entities (a CUDA config
affecting several projects). Sketched here; built only after D1 proves the
import story in real use.

## F5 — Subject canonicalization

**Problem.** The deepest coherence risk. Facts are keyed on free-string
`subject`s chosen independently by rules and LLM; `current_fact` matches
exactly. If one pass emits `service:qwen` and the next `qwen.service`, two
parallel fact chains grow for one thing — and the contradiction detector
can't even see the conflict. Unchecked, this is the difference between
memory that compounds and memory that fragments.

**Design.**
- Maintain a per-scope subject vocabulary (derived from existing facts:
  `subject` + normalized `subject_key` = slug).
- At apply time, match each candidate's subject against the vocabulary
  (exact → normalized → alias/fuzzy, reusing the entity-resolution
  machinery from B2); on a confident match, **re-target** the candidate to
  the canonical subject (record the alias on the entity).
- Prompt instruction to the LLM: "prefer existing subjects from
  `current_facts`; coin new ones only for genuinely new things."
- Ambiguous matches → review queue, never auto-merge.

**Acceptance.** Two phrasings of the same thing converge on one subject
chain (either auto or via one review item); no silent mis-merges (exact
mismatches never auto-merge).

## F6 — Near-duplicate state-claim merge

**Problem.** State claims that differ only in incidental detail
("port 8080 bound by python" vs "…by an unknown process") are different
strings → supersede/contradict ping-pong between near-identical facts
every verification pass.

**Design.** Before treating a same-subject candidate as new, compare
against the current fact: if `kind` matches and token-overlap of the
normalized claims exceeds a threshold (≈0.8), treat as a refinement —
merge evidence, keep the more specific claim (or supersede with
`reason="refined by <evidence>"` if the new claim adds information), never
queue a contradiction. Threshold + behavior configurable.

**Acceptance.** A process-name-flapping port fact produces one stable
current fact across many verify passes, not a supersede chain.

## F13 — Session identifiers in shell events

**Problem.** Shell-hook events carry the shell PID as session; atuin
(when used) and tmux have their own session/pane ids. FINDINGS named
cross-harness correlation "the one custom piece" — none of the ids are
emitted side by side, so stitching is impossible later.

**Design.** Shell hook emits available identifiers into the payload:
`$ATUIN_SESSION`/`ATUIN_DATA_DIR` marker, `$TMUX_PANE`, `$PPID`,
`$TERM_PROGRAM` — whatever exists, no guessing. Purely additive.

**Acceptance.** In a tmux+atuin shell, shell events carry all three ids;
events from one visual session are joinable.

## G1 — `memor timeline` (the "how did we get here" view)

**Problem.** The brief's headline question wants one chronological story;
today facts and events are separate query results the consumer must merge.

**Design.** `memor timeline <project|machine|component> --since … --until …`
(plus MCP `memory_get_timeline`) merges, in time order: raw events
(command/config/port/service), fact transitions (became current /
superseded, with reasons), decisions, reviews. Compact one-line-per-entry
rendering with kind markers; `--evidence` expands a line to its raw events.

**Acceptance.** A week of mixed activity renders as a single readable
chronology; every entry carries an id for drill-down.

## G2 — Periodic human digest

**Problem.** The review loop only happens if the human thinks to run
`memor review`; nothing pulls them in.

**Design.** Digest job (in daemon, `digest_interval` default daily; also
`memor digest` on demand): writes `<data>/system/DIGEST.md` (git-committed
like the rest) — changes since last digest, open review items,
uncertain/contradictory facts, stale `confirmed` facts (A1), LLM pending
count (C3). Notification hook is out of scope (file + `doctor` mention).

**Acceptance.** After a busy day, DIGEST.md surfaces every open item and
the day's transitions without running any query.

## G3 — Surprise/anomaly surfacing

**Problem.** The vacation test asks for "things that may be surprising"
(§27.8): nothing today flags a change that contradicts a `confirmed` fact
or that happened with no recorded human/agent session around it.

**Design.** Heuristic layer on the A1 verify pass + consolidation: flag
(a) any supersession of a `confirmed` fact by lower-confidence or
`verify`-sourced evidence, (b) state changes with no nearby
shell/agent/human events (unexplained change), (c) repeated
contradiction-queue entries for the same subject. Surface as
`[surprise]` items in context/digest with the supporting evidence.

**Acceptance.** A manual port change made outside any session appears as a
surprise item on the next pass; agent-caused changes don't false-positive.

## G5 — Session bootstrap environment snapshot

**Problem.** "Why is this weird env var set?" (brief §26) is unanswerable —
environment state is never captured.

**Design.** Shell hook: on first command of a session (session id unseen),
capture `env -0` (truncated, redacted per F8) as a `session_env` event.
~1 event per login session; rules/LLM can then attribute env-driven
behavior. No per-command env diffing (too noisy).

**Acceptance.** New session → one `session_env` event; a later
"why is FOO set" search finds the value and its session.

## P4 — Semantic / embedding search

The old Phase 4. Still deferred: keyword/FTS + scope traversal covers the
acceptance tests, and embeddings add an index, a model dependency, and a
re-indexing story. Revisit only when real queries demonstrably miss
(retrieval failure log: store `memory_search` queries that returned
nothing as events, and revisit P4 if that pile grows).