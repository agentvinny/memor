# SESSION-MEMORY.md — agent-built memory, design v1 (for review)

*Status: **built** (branch `session-memory` — engine, store, CLI, MCP,
tests, dsh integration; workspace-as-tree-axis, depth-based subagent window
width, deterministic backfill/reprocess, and the LLM exchange audit
(`llm_calls` + `memor window-review`) all built; see
`SESSION-MEMORY-GUIDE.md` for the plain-language guide: capabilities, knobs,
usage, expected behaviour). Supersedes the
consolidation-daemon design in `PLAN.md` §organizer; capture layer, store,
evidence model, and the trusted-actor write path are kept as-is. Open items
are marked **[OPEN]** and are expected to iterate before/at build time.*

## 1. The one-paragraph change

Capture stays exactly what it is: hooks and watchers journal raw evidence
(time-ordered, machine/cwd/session scoped, redacted, append-only). Everything
*downstream* of the journal changes. The consolidation daemon (rules-to-facts
+ LLM extraction windows + autosync queue), the human review loop, and the
episode concept are retired. Knowledge is now built in two ways:

1. **Session summaries** — a mechanical, harness-triggered pipeline that turns
   each agent session's trajectory into a hierarchical tree of short
   summaries (**workspace axis**: all sessions of one DSH workspace = one
   series; project stays a filter/metadata tag), plus
   lazily-materialized temporal rollups (day → week → month axis,
   optionally workspace-scoped). This is the floor: it happens even if the
   agent never writes a single memory record.
2. **Agent knowledge writes** — the existing in-session trusted-actor writes
   (`memory_add_fact/decision/…`) are the ceiling: semantic records with
   scope, why, and evidence, as good as the agent's discipline.

Retrieval is rebuilt around the summary layer: search lands on a summary at
the right altitude, drills through children, and reaches raw evidence —
including the agent's own thinking traces in the harness session log — via
span pointers. Summaries are navigation, never truth: any claim is verified
at the raw level before an agent acts on it.

## 2. Delta against today

| Component | Today | Target |
|---|---|---|
| Capture (shell/agent/git/docker/systemd/fs/net) | live | **unchanged** |
| Raw journal (SQLite `raw_events` + monthly JSONL) | live | **unchanged**, + new `session` source |
| Consolidation daemon (rules + LLM windows + autosync) | live | **retired** (config toggle, default off) |
| State verification (`verify`) | live | **config toggle, default off** (flip back on demand) |
| Human review queue | live, 12 open | frozen; becomes an optional, agent-facing inbox (any agent may resolve with evidence) |
| Episodes | live (1 open, 1977 events) | replaced by summary trees; old records kept as history |
| LLM pending queue (55) | accumulating | lapses — no flush, autosync off |
| Markdown projection | live | kept, demoted to byproduct (git history of agent-written beliefs) |
| Session journaling (harness hook plugin) | — | **new** |
| Window workers (detached default / resident optional) | — | **new** |
| Summary trees (workspace axis) + temporal rollups | — | **new** |
| Span reader (DSH zstd session logs) | — | **new** |
| `SessionStart` context injection | — | **new, default on, configurable** |
| Retrieval API | facts + events FTS | rebuilt: summaries + rollups + spans |

## 3. Architecture

```
 capture (unchanged)
   shell hook · git hooks · docker events/snapshots · journal · fs watcher · ss poll
   harness hook plugin (DSH: dsh-hooks-claude-code mount + memor hooks.json)
        │  prompt / tool call+result / turn / compact / stop   (thin, <100 ms)
        ▼
 raw journal (SQLite, append-only)
   source='session': {prompt, tool_call, tool_result, turn, compact, stop}
   + existing sources: shell · agent(historical) · git · docker · systemd ·
     network · verify(off) · human · memor
        │
        ▼  window bookkeeping (per session, in-store)
 window full (N agent tool steps since last summary) | Stop
        │
        ▼  execution mode (configurable)
 detached (default): hook spawns `memor window-flush` — lives one LLM call, exits
 resident (optional):  `memor session-watch` loop polls for pending windows
        │
        ▼
 digest builder (token-budgeted) → LLM call (cheap model, current local backend)
        │
        ▼
 summary record (level, axis, span | references, subjects, scope)
   chain width 3–4 → parent summaries (deterministic)
   day → week → month temporal rollups (lazy, query-with-caching)
        │
        ▼
 query: MCP server (rebuilt) + CLI + span reader (zstdcat on harness session logs)
```

## 4. Data model

### 4.1 New record kind: `summary` (new table `summaries`)

| field | meaning |
|---|---|
| `id`, `ts`, `machine`, `session_id` | identity; `session_id` = harness session id |
| `axis` | `project` (primary tree) or `temporal` (rollup) |
| `project`, `component` | semantic scope (assigned by the summarizer, not cwd) — a filter/metadata tag |
| `workspace` | the session's cwd (its DSH workspace) — **the tree axis**: folds and temporal rollups are scoped to it (all sessions of one workspace = one series); NULL on nodes predating the column |
| `level` | 1 = raw window; 2+ = summary of summaries |
| `parent_id` | pointer to the containing summary (project axis) |
| `span` | **leaf summaries only**: raw data location, e.g. `dsh:session-<id>:seq[1204..1417]` |
| `references` | typed list of id prefixes: `sum_*` child summaries (chained/temporal nodes) and `fact_*`/`dec_*` records the window's session wrote (any node, incl. leaves) — drill-down dispatches on prefix |
| `text` | the plain-English summary prose — the `summary` field of the summarizer's structured output; never carries sections |
| `decisions[]` | `{subject, choice, reason}` objects from the summarizer (own column, not text) |
| `problems[]` | one-line problems/errors from the summarizer |
| `open_items[]` | one-line open items / next steps from the summarizer |
| `subjects[]` | canonical subject strings (`service:x`, `port:8080`, …) for search |
| `model`, `tokens_in`, `tokens_out` | provenance of the generation |
| `status` | `current` \| `partial` (LLM failed, rule-stub filled in) |

> Implementation note: in the SQLite table the column is named `refs` —
> `references` is a SQL reserved word and breaks the DDL. The dict API
> (and this contract) keep the name `references` throughout.

Invariants (enforced in code):

- A project-axis leaf summary **always has a span** (no raw, no summary).
- A chained/temporal summary **always has child-summary references**;
  drilling descends through children until a span is reached. Raw is
  always reachable.
- Record refs (phase-2 attachment): a summary's references may also carry
  the `fact_*`/`dec_*` ids of records its window wrote, read back from the
  raw log's MCP write results (exact); when results carry no parsable ids
  (e.g. queued for review) a `created_at` range query over the window is
  the fallback. Records accumulate up the tree (fold and temporal refs are
  the union of their children's record refs). Records are never copied into
  summaries — the fact/decision row stays the truth; the refs column is the
  single home for record attachment.
- Summaries are **structured output**: the summarizer returns JSON with a
  required `summary` (plain-English prose) plus `decisions` / `problems` /
  `open` fields, stored in their own columns (`decisions`, `problems`,
  `open_items`) — never composed into the text. Legacy rows that still
  carry `## Decisions` / `## Problems` / `## Open` / `## Records` sections
  are migrated at store open: sections parsed into the columns, text
  reduced to prose, `## Records` dropped as the refs column's duplicate.
  Caps at write: 8 items per field, 200 chars per item.
- Sibling windows **partition** the session (disjoint seq ranges) — no stored
  overlap. The writer receives one-neighbor context (previous summary + the
  next boundary marker) so coherent units straddling a cut aren't severed.

### 4.2 Spans

Format: `<harness>:<session-id>:seq[lo..hi]`.

DSH session logs (verified 2026-09-15) live at
`~/.dsh/sessions/--<sanitized-cwd>--/<session-id>/session.v2.jsonl.zstd`
(live) — a zstd-compressed JSONL where every event carries a monotonic `seq`
and a `type`: `session`, `user/message`, `assistant/message` (incl.
`reasoning` blocks), `tool/call` (`callId`, `name`, `arguments`),
`tool/result`, `turn/start|end`, `step/start|end`, `compaction/start|summary|end`,
`goal/change`, `agent/inbox/spliced`, …

Reader (`memor span` / MCP): stream the zstd file, filter by seq range.
That is the full implementation — no DSH-side exposure needed. The reader is
doubly useful: it serves drill-down **and** is the digest builder's source
for budgeted assistant text (outlines/truncations of `assistant/message`),
so the summarizer never sees raw thinking traces — only the span points at
them.

Span addressing is harness-generic (`harness:session:range`); DSH is the first
reader implementation. Other harnesses contribute their own reader for their
own storage.

### 4.3 Session journal source

New `source='session'` events in the existing `raw_events` table
(kind ∈ `prompt | tool_call | tool_result | turn | compact | stop`), carrying
`session_id`, `machine`, `cwd`, `harness`, and a `seq` mirror of the harness
log seq (so span ↔ journal cross-reference is exact). Existing `source='agent'`
records stay as history.

### 4.4 LLM exchange audit (`llm_calls`)

One row per summarizer call (window / chain / temporal): the exact user
prompt sent (`mode`, `prompt_version`, digest or child texts), the raw model
response (or the captured error), the model, and an `ok` flag. This is the
review surface for "what did the summarizer receive, and what did it
create" — `memor window-review <session>` prints each exchange next to the
stored summary it produced, falling back to a deterministic digest
reconstruction for windows processed before auditing existed. The audit is
append-only, a few KB per call, and never blocks a flush (write failure is
swallowed). No exchange happened → no row (a model-down window shows up
through retries/stub status instead).

## 5. Hook contract (harness-agnostic)

The only harness requirement is: **fire short-lived command hooks with a JSON
stdin payload at these moments.** DSH ships exactly this
(`@deepseek-ai/dsh-hooks-claude-code`, mounted via settings + `hooks.json`;
the same bridge covers Claude Code and Codex).

| Hook | Payload used | memor action |
|---|---|---|
| `SessionStart` | session_id, cwd | journal session start (`memor session-event`); **deliver the injection** via a second command `memor inject --envelope` (default on, §7) |
| `UserPromptSubmit` | session_id, prompt | journal `prompt` (window content, never a boundary); **deliver the injection once** via `memor inject --envelope --point user-prompt` — first-turn guarantee, §7 |
| `PostToolUse` | session_id, tool name, input, output (output truncated at capture, 4 KB, redacted) | journal `tool_call`+`tool_result`; **step++ → check window full** → flag |
| `Stop` | session_id | journal `stop`; flush tail window |

SLA: each hook command must return in <100 ms — it appends one row and sets a
flag. No LLM, no reads of session logs, nothing slow. Exact payload field names
are verified against the bridge source at build time (it mirrors Claude Code's
hook payloads).

Integration artifact (ships in the memor repo): `integrations/dsh/` with
`hooks.json`, the settings.yaml plugin entry, and a README. DSH core: **zero
changes.**

## 6. Windows, workers, and the tree

**Window unit** (per session): the stretch of events since the last summary,
sized by **N agent tool steps** (N configurable, `session_window_steps`,
default 6). A step is one `PostToolUse` hit — an agent tool call. User
prompts are *content*, never boundaries: they are journaled into the window
and shown verbatim (budgeted) in the digest, but cannot close it; only
N steps or Stop do. So one command that drives 100 steps becomes ~17 small
windows (fine-grained "what happened"), while two quick commands may share a
window. Cuts happen at `turn/end` boundaries from the session log
(nearest boundary at/after the count).

**Depth-based width**: subagents are long single-task runs, so their
windows are wider: `session_subagent_window_steps` (default `[12, 24]`) is
indexed by delegation depth − 1 (the last entry caps deeper levels; an
empty list means the uniform default for all depths). The session record
(first line of the harness log — `origin`, `parentSession`,
`delegationDepth`) is read at hook time (one record read per session,
cached in state); the bridge payload needs no changes.

**Execution** — two scripts, configurable (`session.execution_mode`):

- `memor window-flush` (**default, detached**): the hook that detects a full
  window spawns it as a short-lived process. The process lives exactly as long
  as one LLM call: build digest → call model → write summary → chain if
  needed → exit. An in-store lock serializes concurrent windows per session.
  No resident process exists at all.
- `memor session-watch` (optional, resident): a small loop polling the store
  for pending windows. Switch by config; built now, used only if requested.

**Idempotency**: before paying an LLM call, the flush checks whether a
current (non-partial) L1 summary already covers the window
(`store.stored_window(session, lo)` — span starts at lo, hi ≥ window
tail). Yes → the stored row is adopted, the pointer heals to the stored
boundary, no second call. Duplicate triggers (a crash-era retry with a
stale pointer, overlapping workers) must never re-summarize an
already-summarized window; partial/stub rows and rows that end before
the window's tail do not gate the flush.

**Prompt contract** (v3): the summarizer returns exactly one bare JSON
object — no prose, no markdown code fences (the parser still tolerates
fenced output, but the contract says it must not come). `summary` is the
required plain-English field; decisions/problems/open are structured
sisters, never composed into the text.

**Failure**: LLM down → window stays pending, merges into the next
(full-failure after N attempts → rule-stub summary with `status: partial`).
Raw data is never at risk: the journal and the harness log are independent of
the summarizer working. Every actual LLM exchange is audited per call in
`llm_calls` (§4.4) — prompt sent, raw response, ok — reviewable with
`memor window-review`.

**Catch-up and relay** (a worker run is a *drain*, not a single window):
every worker run tiles the whole pending range (stored pointer → log tail)
window by window — one window per LLM call, the same N-steps boundary rule
(so a no-gap run is exactly one window, the historic behavior) — with a wall
budget per run (`session_catchup_budget_seconds`). A run that ends with work
still pending past its snapshot tail — a budget stop, or the log grew while it
ran (drained up-to-date on a live session) — spawns a **successor** worker
(detached mode): the successor re-reads the fresh tail, so further growth
merges into its run and at most one successor is ever in flight. The relay
chases the tail and ends only when the session goes quiet — stopping work
mid-worker therefore converges without any further trigger. A retryable LLM
failure stops the chain (no stub-spam; the next hook trigger resumes from the
durable pointer), and an explicit `--max-windows` cap (out-of-band backfill)
never chains. An in-flight worker and a hook-triggered worker are
serialized by the flush lock; a crashed worker's lock expires via
`FLUSH_LOCK_TTL`.

**Chaining**: every 3–4 sibling level-k summaries **of the same workspace**
(configurable, default 3–4) trigger one level-k+1 summary of their
summaries, same writer. Workspaces never fold into each other (all sessions
of one DSH cwd — main + subagents — form one series). Depth is unbounded in
principle, shallow in practice (100 windows → ~30 → ~10).

**Session cap**: no "session ended" hook exists in DSH (Stop is per-run); the
root cap and any final partial window are computed lazily (next flush or next
query). The cap is bookkeeping — always derivable.

**Digest budget** (target 3–6k tokens/call, cheap-model sized):

| window content | budget |
|---|---|
| user prompts | verbatim (short, high signal) |
| assistant messages | first ~300–500 tokens of `assistant/message` text (from the zstd log, budgeted — never the full thinking trace) |
| each tool call | name + arg digest (~150 chars: command line / file path) |
| each tool result | head+tail ~200 chars, or exit/error only |
| `compaction/*` | one-line structural marker ("context compacted at turn 14") |
| + previous summary + project context | capped by `max_chars` |

**Noise skip**: a window whose events are all pure-noise tool calls (reuse the
existing `ignore_command_prefixes` machinery + a tool-level list) skips the LLM
and writes a rule-stub.

**DSH compaction summaries** (`compaction/summary` events — DSH's own
context-level summaries) are available as free upper-level material.
**[OPEN]** whether to feed them into level-2+ generation instead of
re-summarizing.

**Backfill / reprocess** (`memor sessions`, `memor backfill <id>`):
existing (or live) session logs can be processed on demand through the same
window pipeline. The input is **deterministic** — the log is immutable and a
window boundary is a pure function of it (the seq of the Nth `tool/call`
event after the pointer), so backfill reproduces exactly what live
journaling would have produced (live mode counts the same events via hooks;
backfill counts them by log scan — identical boundaries). Properties:

- **Resumable**: the pointer (`last_span_hi`) lives in the store; a run
  interrupted (LLM down, `--max-windows`, Ctrl-C) continues on re-run.
- **Dry-run plan**: `--dry-run` reports pending events/tool calls → windows
  → expected LLM calls, without writing anything.
- **Reprocess** (`--reprocess`): cascade-delete the session's existing
  series — its level-1 rows, then iteratively every summary whose `refs`
  references a doomed id (fold ancestors up the chain, cached temporal
  rollups) — then rebuild under the current config/params. Everything
  deleted is a derived cache (lazy-rebuildable on next query); raw logs,
  facts, and decisions are untouched. `store.summaries_referencing(ids)`
  (refs LIKE over the JSON array) is the cascade engine.
- **Continuable**: backfill and live journaling share the per-session
  state entry (pointer, counters, depth, workspace), so a session
  backfilled and then continued in DSH is journaled from exactly where
  the backfill stopped — the two eras fold into one tree. Guard: with a
  live flush worker mid-flight, backfill bails as `flush-in-progress`
  (stale locks expire via `FLUSH_LOCK_TTL`, same guard as `flush_window`).

## 7. `SessionStart` context injection

`session_context.enabled` — **default on**, configurable.

- **On**: the `SessionStart` hook point runs two independent commands.
  `memor session-event --event start` journals the start; `memor inject
  --envelope` renders the tree document for the session's cwd and prints
  it on stdout wrapped in the Claude-Code hook envelope
  (`hookSpecificOutput.additionalContext`), which the `dsh-hooks-claude-
  code` bridge merges and injects as a session-start user message
  (`agent.inject`). Status goes to stderr so it can never contaminate
  the envelope; an empty document prints nothing (the bridge then
  injects no context — the correct answer for a fresh workspace). The
  agent boots "catch me up" without being told to. The package is
  **exactly that workspace's session tree** — a self-explanatory document
  built by `session_tree_context` from the live store:
  - **A** apex: all top-level nodes, i.e. the current tree's apex (the
    deepest fold that exists so far; in as few lines as possible)
  - **B** mid series: ALL nodes at L-Max−2 (the 9–18 band at chain width 3)
  - **C** newer than B: downward walk of levels, only nodes NOT transitively
    covered by A/B (reference-graph BFS); L1 always its last 6 windows
  - **D** raw tail: events past the flushed pointer, one representative line
    per event (user messages whole; harness reminders collapsed to a one-line
    note — their payload is a file, kept verbatim in the raw span; calls by
    their key field; results by status + first line)
  Every entry is the summary's plain prose as ONE CLEAN PARAGRAPH
  (summarizer markdown decoration — bold, code spans, list markers —
  dropped; nothing elided), prefixed by the seq range it covers (its
  own window for L1s, the union of its leaf windows for folds — the
  same addressing at every level), and enclosed: exactly one blank line
  between entries. The structured columns (decisions/problems/
  open_items) are not injected; they show in the drill-down surfaces.
  The document ends with a **Traversing** section: the surfaces that
  answer specific drill-down questions (point in time → `memor span`,
  whole tree → `memor summaries --tree`, a day/week → `memor temporal`,
  a topic/error → `memor search`), plus the pointer to the full
  question→surface guide in AGENTS.md's memor section.
  No character budgets: the scoping above is what keeps it scoped.
  Every entry describes its own window, including what was open then:
  later entries record the resolution; current state = the newest entries + D.
  Without a workspace: the project's top-of-tree summaries.
  **First-turn guarantee (R5):** the SessionStart delivery above is
  best-effort — the bridge injects hook results via `agent.inject()` at
  session load/create time, which is *lost if no turn has started yet*
  (type minutes later and the context never reaches the model; observed
  live). So the `UserPromptSubmit` point also runs `memor inject
  --envelope --point user-prompt`: it executes at the first pre-step —
  the same moment the host injects its own AGENTS.md reminders — and its
  context is merged into the turn's messages, deterministically. The
  envelope rides exactly ONE turn per session lifetime via an
  `envelope_delivered` marker in the store: no duplicate context on later
  turns, no re-injection after a host restart (the conversation history
  already holds it). With no history yet it emits nothing and sets no
  marker, so the first prompt after the tree appears still delivers.
- **Off**: the agent retrieves on demand — the AGENTS.md instruction
  ("call `memory_get_project_context` at task start") + MCP tools, the pattern
  this session uses.

### 7.1 Workspace state — the self-maintaining current-state surface

The session tree (§6) answers "what happened". A workspace also needs
"what is true right now": open items, active problems, standing decisions,
conventions, key commands, workflows, checklists. That is the
**workspace state** — a structured, workspace-scoped table with two writer
classes and one curation loop.

- **Storage**: `workspace_state` table; one row per
  `(workspace, category, subject)` key. Category is an open string
  (known: open_item, problem, decision, convention, practice, command,
  workflow, checklist, note). Statuses: `active` / `resolved` / `retired` /
  `superseded`. A `superseded` close creates a NEW active row on the key
  and points the old row at it (`superseded_by`) — history is kept, never
  overwritten. A monotonic `rev` counter (bumped on every write) orders
  closes deterministically: timestamps are second-precision and several
  closes land in the same second, so `updated_at` alone ties.
- **Writer 1 — the agent**: MCP `memory_workspace_state_add(category,
  subject, body, workspace)` / `memory_workspace_state_set(ids, status)`
  (bulk: close everything that is done in one call), or the CLI
  `memor state add|set|show|sweep|dedupe`. The agent records what it
  LEARNS (a command that works, a convention it must follow, a decision
  with its why) and CURES what is stale (resolve/retire/supersede). The
  rendered id on each line is the curation handle; the envelope's state
  section carries the standing close instruction so every new session
  knows the list must stay true.
- **Writer 2 — auto-promotion**: the flush worker promotes each
  window's structured summary columns after writing the summary
  (decisions `{subject, choice, reason}` → decision rows,
  `choice — reason`; problems/open_items → one-liner rows). Idempotent:
  re-promoting identical content from the same span is a no-op; a later
  span advances provenance. Promotion never kills the worker (errors go to
  stderr). Default ON — the system self-populates; the agent cures.
  **Dedup, two legs**:
  1. *Prompt leg* (session-v4): the window/chain summarizer prompt
     carries the workspace's on-record state registry (`workspace_state`
     payload key) with the instruction to reuse subjects for ongoing work
     — without it each window re-described the same ongoing items with
     fresh subjects (observed live: one rev-counter bug recorded three
     times across four windows). The registry lists decaying categories
     first, most-recently-handled first (live finding: creation-order +
     the 60-line cap truncated mostly open_items — the items most likely
     to be re-described).
  2. *Deterministic backstop* (R4, `state_promote_dedup`, default on):
     prompt instructions do not bind a mid-tier model — the paraphrase
     noise continued with the registry in flight (live: one item
     re-created 3–5×). So at promotion, a candidate that misses the exact
     key is compared (cosine, same workspace+category) against the active
     rows — at/above threshold it updates the existing row in place
     instead of creating a duplicate. Category-dependent bars,
     live-measured: decaying categories (open_item/problem) 0.80 —
     same-item pairs sit at 0.95+ against 0.69 for distinct items;
     reference categories 0.90 — related-but-distinct *decisions* share
     vocabulary up to 0.87, so they need the stricter bar. The embedder
     is created lazily (only when a candidate needs the comparison) and
     vectors ride the R3 `embeddings` cache; no embedder → exact-key only.
- **Delivery channels** (decoupled — R2b): the state rides the
  **SessionStart injection envelope** at boot (`_state_section`: active
  rows only — the catch-up moment is where freshness matters), and is
  **fetched on demand** mid-session (`memor state show`, MCP
  `memory_get_workspace_state`). The `AGENTS.local.md` file projection
  (`project_state_file`, markers, created if absent, content outside the
  markers survives, no-op write when unchanged) is **opt-in**
  (`session_state_project_file`, default off, or explicit `memor state
  project`). Why the file is off by default: it is DSH's instruction
  channel — DSH injects it at session start and re-projects *any* change
  into every running session as an "instruction update", so a churning
  machine surface riding that channel is a per-window token tax with
  wrong framing. Lesson from the build: the first live run of this
  feature proved exactly that (a state write in every window flush
  re-injected the whole document as "Updated instructions"); the channel
  was decoupled in response.
- **Settling (R4)** — the lifecycle is no longer one-way:
  - *Closing*: the state document carries a standing instruction
    ("close items that are done"); `memor state set <id>... --status
    resolved` and `memory_workspace_state_set` take **multiple ids**
    (bulk close), and `state show` filters (`--category/--status/
    --limit/--age-days`) make triage a one-liner.
  - *Staleness triage*: `memor state sweep --days N [--dry-run]` marks
    untouched active rows `stale` (a soft status: dropped from the
    envelope, listed in a collapsed `### Stale` section, revivable via
    `set <id> active`). Manual only — the worker never auto-closes; an
    inferred close from the lossy window extraction would make
    destructive writes (wrong-close is worse than stale-keep).
  - *Dedupe*: `memor state dedupe [--apply]` clusters the active rows
    (same category, cosine ≥ the category bar) and — with `--apply` —
    closes the absorbed rows as superseded by the cluster keeper (the
    longest-on-record row, rowid-ordered). Dry run by default.
- **Render**: two projections. The **envelope section** (boot, P3) is
  actionable-first and bounded: `### Open` (open items + problems,
  most-recently-handled first, cap 15), `### Recent failures` (closed
  problem rows inside a 14-day horizon — 'we tried this, it failed,
  here's how it was handled'; active problems stay in Open, so there is
  no duplication, cap 5), `### Recent decisions` (cap 10),
  `### Standing knowledge` (conventions/commands/workflows/..., cap 10,
  only if present) — worst case ~40 lines, stale rows excluded; the
  section is omitted when there is neither active state nor recent
  failures. After the state section, the envelope appends a
  `## Open review items (N)` section when the review queue (unresolved
  contradictions / pending proposals — machine-global) is non-empty: up
  to 3 items + a pointer to `memor review`; the queue is human
  territory, so the envelope points at it and never resolves it.
  The **full document** (`memor state show`, on-demand) keeps the classic
  per-category sections (per-category cap 25, overflow pointer),
  `### Recently handled (N)` (last 5 closes, `rev` order) and
  `### Stale (N)`.
- **Verified live** (this branch's build window): six consecutive
  worker windows of the building session were flushed, promoted,
  projected, and re-projected into the very session that was building the
  feature; curation (resolve/retire/supersede) via the CLI
  re-projected each time, including regressions the live loop surfaced
  (ids missing from the render → added; same-second close ties →
  `rev`; per-window instruction re-injection → channel decoupled in
  R2b). R4 evidence from the same loop: the active set had grown to
  ~100 rows of ~20 real items (prompt-leg dedup was shipping but the
  model kept paraphrasing); the live `state dedupe` dry run clustered
  12 groups at the measured thresholds (all 6 variants of one item
  grouped; distinct decisions stayed separate at the 0.90 bar), and the
  apply pass closed 44 absorbed rows + 36 resolved items back to a
  sane hot set.

## 8. Retrieval (rebuilt)

| tool / command | semantics |
|---|---|
| `memory_context(project, workspace?)` | the SessionStart injection package (§7): with `workspace`, that workspace's session tree (apex + mid series + uncovered frontier + raw tail, full prose); without, the project's top-of-tree summaries |
| `memory_timeline(project)` | the summary tree, chronological — "how did we get here" at every altitude |
| `memory_temporal(machine|workspace, since, until)` | day/week/month rollups, **lazy**: computed on demand from existing summaries, cached as records (query-with-caching, not a scheduled job); with `workspace`, the rollups cover only that workspace's summaries (distinct cache entries, so machine-wide rollups are unaffected); `Stop` crossing a day boundary pre-warms that day as a piggyback |
| `memory_search(query, scope…, mode?)` | **Scoped hybrid search (R3):** FTS over raw events / facts / decisions / summaries / entities **plus** semantic search (embeddings of summaries, active workspace state, user prompts), rank-fused (RRF). Scope axes (ANDed, all optional): workspace (the session cwd — the usual default), project/component, machine, session, since/until, summary level, kinds. `mode`: `hybrid` (default) / `fts` / `semantic`; degrades to FTS with an explicit note when no embedder is available. A hit carries its span/scope — the landing surface for drill-down |
| `memory_drill(summary_id)` | children (chained/temporal) or the raw span (leaf) |
| `memory_span(session, lo, hi)` | raw harness session events from the zstd log — **the "read the thinking trace" path** |
| `memory_get_evidence(fact_id)` | unchanged — provenance to raw events |

Invariant (kept from today, enforced in the agent guide): **summaries are
navigation, not truth.** An agent that acts on a summary's claim verifies it
at the raw level first (span or evidence). This is what keeps a lossy
compression from becoming a lie.

### 8.1 Semantic index (R3)

FTS answers "where do these exact words appear"; embeddings answer "where
is the same thing discussed in other words." The legs complement each
other (verified live: paraphrased queries resolve only via the semantic
leg, exact phrases only via FTS; RRF then fuses both rankings).

- **Targets** (chosen to keep the index small and meaningful): session
  summaries (all levels), **active** workspace-state rows (the decisions /
  problems / open items "where things stand" surface), and user prompt
  events (what was asked). Raw tool output is deliberately NOT indexed —
  noise, and already FTS-searchable over events.
- **Lazy + cached**: vectors are produced on first need and cached in the
  `embeddings` table (SQLite BLOB float32, keyed `(kind, ref_id)`); each
  search embeds only the query plus any rows not yet cached. Cosine via
  numpy — no external service.
- **Model**: local `BAAI/bge-large-en-v1.5` (1024-dim) on GPU-1
  (`embed_device`); ~1.4 s load, ~0.1 s per small batch. `embed_provider:
  api` (+ OpenAI-compatible base/key) is the alternative. Any embedder
  failure degrades to FTS with a note — retrieval never goes down because
  of a model.
- **Fusion**: RRF (k=60) over [FTS ranking, semantic ranking]; a hit
  ranked by both legs is `via: fts+semantic` and outranks single-leg hits.
  CLI: `memor search "<q>" --mode hybrid|fts|semantic --workspace …
  --since … --kinds …` (workspace defaults to the caller's cwd).
- **Multi-word FTS**: `search_summaries` / `search_facts` /
  `search_decisions` were upgraded from whole-phrase substring LIKE to
  per-word AND (every query word must appear as a substring in at least
  one text column) — a strict superset; single-word behavior unchanged.

Measured on the live store (2026-09, dense build session): first hybrid
search embeds the whole corpus (~15 summaries + ~70 state rows + ~15
prompts) in ~4 s including model load; cached subsequent runs pay only
model load + one query embed; the `--mode fts` path is 0.04 s.

## 9. Store migration (switchover, non-destructive)

1. `llm_autosync: false`; the 55 pending windows are marked lapsed (no flush —
   they've been falling back rules-only during the 401 period anyway).
2. `verify.enabled: false` (toggle available); consolidation off.
3. Review queue: frozen, 12 items remain, resolvable by any future agent via
   `memory_review` (agent-facing accept/reject with reason). No human
   dependency.
4. Existing `raw_events` (2831), facts (1180), decisions, episodes: untouched
   history. Projection keeps generating (now from agent writes only).
5. New: `summaries` table + FTS index, `session` source, span readers, window
   bookkeeping.

The new regime starts at switchover. Backfill of past sessions is now a
**deliberate, per-session command** (`memor backfill <id>`, §6 — deterministic
input, resumable, `--reprocess` for re-building under changed params), so
"which old sessions are worth summarizing" stays a human decision; their
zstd logs remain readable via `memory_span` either way.

## 10. Configuration surface (new keys, `memor.json`)

```jsonc
{
  "session": {
    "journal_enabled": true,
    "harnesses": ["dsh"],
    "window_steps": 6,              // N agent tool steps per window (prompts never close)
    "subagent_window_steps": [12, 24],  // by delegation depth − 1; last caps; [] = uniform
    "chain_width": 3,               // siblings per parent, per workspace (3–4)
    "execution_mode": "detached",   // detached | resident
    "digest": { "assistant_tokens": 400, "tool_arg_chars": 150,
                "tool_result_chars": 200 },
    "noise_tool_prefixes": []
  },
  "session_context": { "enabled": true, "max_chars": 2000 },
  "temporal": { "rollups": ["day", "week", "month"], "lazy": true },
  "verify": { "enabled": false },
  "consolidate": { "enabled": false },   // the flip-back toggle
  "llm": { "autosync": false }
}
```

LLM backend stays as-is (local-first; current OpenRouter wiring kept as
config), used for window summaries with whatever model is configured —
expected to run on a cheap local model.

## 11. Build order (proposed, for approval)

1. Store: `summaries` table + span types + `session` source + FTS + window
   bookkeeping + in-store locking.
2. `memor session-event` (hook CLI) + digest builder (zstd reader included).
3. `memor window-flush` (detached worker, LLM call reusing extractor plumbing)
   + `memor session-watch` (resident variant).
4. DSH integration: `integrations/dsh/` (hooks.json + settings entry + README)
   + `SessionStart` injection.
5. Retrieval: MCP tools + CLI (`context/timeline/temporal/search/drill/span`)
   + lazy temporal rollups.
6. Switchover: §9 migration steps.
7. Docs: `AGENT-GUIDE.md` v2 (the new agent contract), `getting-started.md`
   updates, this file's **[OPEN]** items closed.

Estimate: steps 1–3 are the memor core (a few hundred lines each); step 4 is
config + a README (no DSH code); step 5 is mostly plumbing over step 1's
schema.

## 12. Open items (to iterate, in priority order)

1. Injection assembly strategy (§7) — what exactly goes into the
   `SessionStart` package, and what the off-path retrieval instruction says.
2. DSH compaction summaries as upper-level tree material (§6).
3. Defaults: `window_steps`, `subagent_window_steps`, `chain_width`, digest
   budgets — set by the numbers, tune after a few real sessions.
4. Tool-level noise list for the skip rule (§6).
5. ~~One-off backfill of recent sessions (§9)~~ — **resolved**: on-demand,
   per-session, deterministic (`memor backfill`), so no one-off decision.
6. Housekeeping: pointer from `FUTURE-WORK.md` to this doc; `GAPS.md` build
   log entry once built. *(Proposed, not done.)*
