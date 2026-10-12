# AGENT-GUIDE.md — how to use memor

This file is the complete interface contract for memor, the durable project/
environment memory of this machine and its projects. Any agent or harness
(Claude Code, Cursor, OpenCode, scripts, humans) can operate the memory by
following this document alone. It is kept in sync with the code; the footer
says which version it describes.

## 1. What memor is and what it promises

memor is a **journal of evidence plus knowledge derived from it**:

- **Raw evidence** (Level 4): every command run in a shell or by an agent
  (with output when the harness provides it), git commits/merges/checkouts,
  Docker lifecycle and config drift, systemd unit events, config-file changes
  (with diffs), and network/port changes — captured continuously by hooks and
  watchers, independent of any one agent.
- **Session summaries** (Level 1.5): the agent's own agent sessions (DSH)
  are journaled by command hooks and folded into a hierarchical summary tree —
  level 1 = work windows anchored to the raw log by a *span*, level 2+ =
  folds, plus lazy day/week/month rollups. Summaries are **navigation, not
  truth**: every leaf points back at raw events, so any summary path
  terminates in evidence.
- **Derived knowledge** (Levels 0–3): facts, state, decisions, configuration,
  lessons, problems, and relationships — consolidated from the evidence,
  each one **traceable back to the events that support it**.

Trust rules (these are enforced in code, not by convention):

1. **Every fact has a confidence** (`confirmed` > `probable` > `inferred` >
   `unknown`) **and a status** (`current`, `superseded`, `contradictory`,
   `unknown`, `retracted`).
2. **Nothing is ever overwritten.** When truth changes, the old row is closed
   (`superseded`, with `valid_until`) and a new row is inserted that points
   back at it (`supersedes_id`). A→B→A keeps all three rows.
3. **LLM-derived claims are gated on evidence.** Records the LLM proposes
   without citing existing evidence are never current (uncited ones are
   dropped, low-confidence ones are queued for review). Records asserted by
   a *trusted actor* — you (a human) or an agent/harness writing on purpose —
   are different: they become current with their provenance (`source_type`)
   and confidence still visible, without a review pass. This is what
   `trust_agent_writes` controls (default on); set it off to gate agent
   writes on evidence like the LLM's.
4. **Contradictions are surfaced, not resolved silently.** A new claim that
   conflicts with a current fact goes to the review queue; a human (or the
   owner) decides.
5. **You can always ask "why do you believe this?"** — `memory_get_evidence`
   returns the raw events (commands, diffs, observations) behind any fact.

## 2. The data model in one page

**Scopes** — every record is scoped:

| Scope | Meaning | Example |
|---|---|---|
| `project` | a body of work | `local-ai`, `auto-apply` |
| `component` | a part of a project | `qwen-server`, `web` |
| `machine` | a box (hostname) | `desktopU`, `laptop` |

Any combination is valid; omit what doesn't apply.

**Record kinds:** `fact`, `state` (time-bound truth: running/stopped/bound),
`configuration`, `preference`, `problem`, `lesson`, `todo`, `decision`,
`architecture`. Relations are separate: `runs_on`, `depends_on`, `uses`,
`part_of`, `conflicts_with`, `listens_on`, …

**Time:** a record's truth spans `valid_from` → `valid_until` (open when
current). "What is true now" = `status: current`. "What used to be true" =
the `superseded` chain. "What was true at a moment" = the point-in-time
read (`memor current <entity> --at <ts>` / `memory_get_state_as_of`) —
every fact whose validity window covers that instant, plus the decisions
in force then; retracted records never appear, contradictory ones do
(the conflict may have been real at that moment).

**Entities & subjects:** components/machines/projects are entities; facts
attach to a `subject` string (conventions: `service:qwen`, `port:8080`,
`container:web`, `config:/path`, `repo:<project>`). Refer to things
consistently — the system re-targets near-identical subject spellings onto
established ones, but don't rely on that.

## 3. How to READ

The memory is exposed as an **MCP server** (stdio, tool prefix `memory_*`);
every tool has a CLI twin for non-MCP harnesses.

### Recommended session pattern

1. **Start of a task** → `memory_get_project_context(project)` — one compact
   package: current state, relations, recent decisions, real recent changes
   (including what stopped being true), and open/uncertain items. Cap size
   with `max_chars` if needed. On a multi-box setup, start with
   `memory_get_global_context()`.
2. **Drill down** → `memory_get_current_state(entity)`,
   `memory_get_state_as_of(entity, at)` (point in time),
   `memory_get_history(entity)`,
   `memory_get_related(entity)`, `memory_get_decisions(project)`.
3. **Chronology / "how did we get here"** → `memory_get_timeline(entity,
   since_hours)`; "what did we work on" → `memory_get_recent_episodes()`.
4. **Free-form** → `memory_search(query, …)` — **scoped hybrid search**
   (full-text over raw events, facts, decisions, entities, and session
   summaries, **plus** semantic embeddings of summaries, active workspace
   state, and user prompts, rank-fused). Scope axes, ANDed: `workspace`
   (a DSH cwd — usually your own cwd), `scope_project`, `scope_component`,
   `machine`, `session`, `since`/`until` (ISO ts), `level`, `kinds`;
   `mode`: `hybrid` (default) / `fts` / `semantic`. Prefer a scope axis over
   stuffing qualifiers into the query string.
5. **Before acting on anything surprising** → `memory_get_evidence(fact_id)`
   to see the proof; if the record is `unknown`/`contradictory`, treat it as
   *not established*.

### Session-memory tools (what we worked on)

- **Session start** → `memory_get_injection(project, workspace?)` — the compact
  package a harness should inject: latest session-summary roots + open items
  + todos + problems + recent decisions. With `workspace` (a DSH cwd) that
  workspace's tree — all its sessions, other agents included — leads
  (CLI: `memor context <project> --injection --workspace <cwd>`).
  In DSH it arrives at your **first turn**, not at session load: the
  UserPromptSubmit hook runs `memor inject --envelope --point user-prompt`
  once per session lifetime (the same moment AGENTS.md is injected); the
  store's `envelope_delivered` marker makes later prompts a no-op.
- **What was a session like** → `memory_get_summaries(scope_project?, session?, workspace?)`
  lists the roots of the summary tree (one workspace's tree with `workspace`);
  `memory_drill_down(summary_id)` follows
  any edge of the tree — `sum_*` references down to child summaries,
  `fact_*`/`dec_*` references to the records that window wrote (the
  session's own memor writes — they accumulate up the folds), or the leaf
  `span` into the raw conversation events.
- **Raw conversation** → `memory_get_span(span)` reads a window straight from
  the harness log. Span syntax: `<harness>:<session>:seq[lo..hi]`, e.g.
  `dsh:abc123:seq[120..140]`. (CLI: `memor span "<span>"`.)
- **Find it / where do I stand?** → `memory_search(query, workspace=…, mode=…)`:
  hybrid = full-text ∪ semantic (embeddings of summaries + **active
  workspace state** + user prompts), fused. Scope by axis (`workspace`,
  `session`, `machine`, `since`/`until`, `level`, `kinds`) — e.g.
  "what decisions are open in this workspace" is
  `memory_search("…", workspace=<cwd>, kinds="state")`. A hit's
  `span`/`session` is the landing spot for `memory_get_span` /
  `memory_drill_down`; a `state` hit is a `wst_*` row for
  `memory_get_workspace_state`.
- **Workspace state — read it, write it, keep it true** →
  `memory_get_workspace_state(workspace)` renders the current state
  (ids on every line). Record what you LEARN as you go:
  `memory_workspace_state_add(category, subject, body, workspace)` —
  categories: open_item / problem / decision / convention / practice /
  command / workflow / checklist / note (any string). **Keep the list
  true**: when you finish an open item or a problem goes away, close it
  — `memory_workspace_state_set([ids], "resolved")` takes a LIST (bulk
  close in one call) — the state section in your opening context carries
  this instruction for a reason. CLI: `memor state show|add|set …`;
  curation triage: `memor state show --status active --age-days 14`
  (what's been sitting), `memor state sweep --days 14 --dry-run`
  (soft-close untouched rows — manual only, never automatic),
  `memor state dedupe` (list near-duplicate rows; `--apply` merges).
- **"What happened this week?"** → `memory_get_temporal(unit, start, scope_workspace?)`
  — day/week/month rollups folded from the session summaries, optionally
  scoped to one workspace (CLI: `memor temporal --unit week --start
  2026-09-08 [--workspace <cwd>]`). Tree view: `memor summaries --tree`.
- **Old sessions, on demand** → `memor sessions` (which DSH sessions are
  `unprocessed`/`behind`/`current`) and `memor backfill <id>
  [--dry-run|--reprocess]` — deterministic, resumable processing of existing
  logs (reprocess rebuilds one session's series under changed params).

Summaries are generated, so treat them as *signposts*: they point at the
raw evidence, never substitute for it.

**Entity syntax:** a plain name (`qwen-server`), or a specifier —
`project:local-ai`, `machine:desktopU`, `component:web`, or a raw subject
(`service:web`, `port:8080`).

### CLI equivalents (for harnesses without MCP)

```bash
memor context <project> [--evidence] [--max-chars N]   # catch me up
memor timeline <name> [--hours N]                      # chronology
memor current <entity> [--at ISO-ts] | memor history <entity>
                                                        # now / at T / past
memor related <entity> | memor decisions [--project P]
memor evidence <fact_id>                                # provenance
memor search "<query>" [--project P]
memor recent [--hours N] | memor episodes [--project P]
memor global                                            # cross-machine view
memor report <project>                                  # human catch-me-up
memor events [--source git]                             # raw Level-4
memor span "<harness>:<session>:seq[lo..hi]"            # raw conversation
memor summaries [--tree]                                # summary tree
memor temporal --unit day|week|month [--start YYYY-MM-DD]
```

All query commands print JSON (or Markdown for `context`/`timeline`/
`report`) and work against the same store the MCP server uses.

## 4. How to WRITE

Writes go through the same validated pipeline as consolidation — the rules of
§1 apply to you too.

### The hard rules

1. **Cite evidence when you have it.** If you observed a command, file
   change, or event, find its id (`memory_search` / `memor events`) and pass
   it in `evidence`. Your deliberate write is a *trusted-actor assertion* and
   becomes `current` with its provenance visible even without evidence — so
   cite evidence to make the record *verifiable* (a reader can pull the raw
   event), not to make it become true. (LLM-proposed records are different:
   they stay gated on evidence, per §1.3.)
2. **Only record what you observed or were told.** Never assert facts the
   evidence doesn't support; mark inference explicitly with
   `confidence: "inferred"`.
3. **Supersede, don't overwrite.** When a value changes, record the new
   state — memor closes the old record automatically (or flags a
   contradiction for review). To fix a wrong memory, use
   `memory_correct`; to retire one, `memory_retract` (both keep the audit
   trail).
4. **Decisions need their why.** Use `memory_add_decision` with `reason`
   and `alternatives` — a future agent must be able to tell whether a
   configuration is deliberate, accidental, or obsolete.
5. **One subject, one spelling.** Use the existing subject names you see in
   `memory_get_project_context` (e.g. `service:qwen`, `port:8080`).

### MCP write tools

| Tool | Use for |
|---|---|
| `memory_add_fact(subject, claim, scope_project?, scope_component?, kind?, confidence?, reason?, evidence?)` | any finding, state, configuration, preference, problem, lesson, todo |
| `memory_add_decision(subject, choice, scope_*, alternatives?, reason?, confidence?, evidence?)` | a deliberate choice (keep the why!) |
| `memory_add_relation(subject, relation, object, evidence)` | `runs_on`, `depends_on`, `uses`, … (evidence required) |
| `memory_add_note(text, scope_*, kind?, confidence?, reason?, evidence?)` | quick note/learning |
| `memory_correct(fact_id, claim, reason?)` | fix a wrong memory (supersedes it) |
| `memory_retract(fact_id, reason?)` | retire a memory (stays in history) |

Non-MCP harnesses: `memor remember "text" --project P --component C
--subject S --kind K --confidence K --reason R` (human-asserted path,
highest trust for uncited notes), and `memor correct` / `memor forget`.

## 5. How to LOG your work (recipes)

- **You made a change** (config, service, dependency): the capture layer
  already recorded the command/event. Add a `memory_add_decision` (or
  `state` fact) that says *what you did and why*, citing the event id.
- **You hit a failure:** `memory_add_fact(kind="problem", subject="error:<signature>",
  claim="…", evidence=[event])` so "have I seen this before?" is answerable.
- **You learned something generalizable:** `memory_add_fact(kind="lesson", …)`.
- **Something is open:** `memory_add_fact(kind="todo", …)` — it shows up in
  every context package's "Open items".
- **A hypothesis you're not sure about:** record with
  `confidence: "inferred"` (or `"unknown"`). As a trusted-actor write it
  becomes current but *labeled* inferred, so any reader (agent or human)
  sees it as a hypothesis, not established fact — treat `inferred`/`unknown`
  records that way too. If you'd rather it not be current until a human
  confirms it, that's what `trust_agent_writes: false` gives you.
- **End of a work session:** optionally `memor episode close --project P`
  (or let the daemon do it after an idle period) so "what did I work on?"
  has a one-line answer; then record anything only you know.

## 6. Worked examples

**Example A — you changed a port and want it remembered properly:**

```
memory_search("qwen port")                      → finds event evt_1a2b… (your `systemctl … edit`)
memory_add_fact(
    subject="port:8080",
    claim="Qwen inference endpoint listens on 8080/tcp",
    scope_project="local-ai", scope_component="qwen-server",
    kind="state", confidence="confirmed",
    reason="8000 conflicted with the monitoring stack",
    evidence=["evt_1a2b…"])                     → becomes current, old 8000 fact closed
```

**Example B — you're unsure why a service is stopped:**

```
memory_get_current_state("service:qwen")        → current: "is stopped (verified)" [fact_9f…], conflicting: []
memory_get_history("service:qwen")              → chain: running → stopped (valid_until 2026-08-24, reason: "I stopped it for the GPU migration")
memory_get_evidence("fact_9f…")                 → the exact `systemctl stop` command + session id
```

**Example C — you made a deliberate architectural choice:**

```
memory_add_decision(
    subject="inference-backend", choice="use vLLM",
    scope_project="local-ai", scope_component="qwen-server",
    alternatives=["llama.cpp", "sglang"],
    reason="llama.cpp throughput was 4× slower on this GPU; sglang CUDA mismatch (see evt_77…)",
    confidence="confirmed", evidence=["evt_77…"])
```

## 7. What memor will NOT do

- It doesn't remember what it never observed: no trace in a shell, git,
  service, file, or container → no memory (unless you record it via §4/§5).
- It doesn't delete anything: compaction (`memor compact`) retires old *raw
  events* that are already in memory and unreferenced; facts, decisions,
  evidence, and retracted records are permanent.
- It doesn't sync machines by itself: `memor export` / `memor import
  --as-machine` move a store between boxes.

---

*Interface contract, memor v0.1.0 — 23 MCP tools (17 read, 6 write) + CLI
surface as listed above. If a tool name or parameter here doesn't match
`memor --help` / the MCP tool list, the guide is stale: regenerate it from
the code.*