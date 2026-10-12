# Session Memory — plain-language guide

Companion to `SESSION-MEMORY.md` (the design). This is the "what is this,
what does it do, how do I use it" version.

---

## 1. The basic idea

memor already journals everything the environment does. What it was missing
was its own working life: *what the agent actually did inside a session*.

Session memory fixes that. DSH command hooks tell memor "a session
started / a prompt arrived / a tool ran / a session stopped" (one cheap
SQLite row each, <100 ms, no LLM in the hot path). A background worker then
reads DSH's own session log (the zstd JSONL files under `~/.dsh/sessions/`)
and writes **summary trees**:

```
DSH session log (raw, zstd)
   └─ window 1  (6 agent tool steps) ──→ summary ──┐  (span points back
   └─ window 2                              ──→ summary ──┤  into the log)
   └─ window 3  ────────────────────────→ summary ──┘
                                              │
         same workspace's other sessions fold in too (workspaces are
         the axis: all sessions of one DSH cwd = one series)
                                              │
                        N windows fold into one parent (level 2)
                                              │
                        day / week / month rollups (lazy, on query)
```

The one rule everything follows: **a summary is a signpost, not the
truth.** Every node points at the raw data — leaves carry a *span*
(`<harness>:<session>:seq[lo..hi]`) into the log; higher nodes carry
*references* to their children, plus the record ids the session wrote.
You can always drill all the way down to the raw events.

---

## 2. What you get

| Question | Answered by |
|---|---|
| "What did we work on in past sessions?" | `memory_get_summaries` → roots of the tree; `memory_drill_down` follows it |
| "What exactly happened around line 130 of that session?" | `memory_get_span("dsh:<id>:seq[125..140]")` — raw events back |
| "What did that session *record* in memor (facts, decisions)?" | the `## Records` section of its summary; `memory_drill_down` returns the records themselves |
| "What happened this week / this day?" | `memory_get_temporal` — rollups folded from the summaries (`scope_workspace=` for one workspace) |
| "What did we work on in this workspace (across all its sessions)?" | `memory_get_summaries(workspace="<cwd>")` — the workspace's tree, including other sessions' work |
| "Start of session: brief me" | `memory_get_injection(project, workspace)` — latest summaries + open items + todos + problems + recent decisions, size-capped; the workspace's tree leads |
| "Which sessions are journaled / caught up?" | `memor sessions` — every DSH session on the box with its status (`unprocessed` / `behind` / `current`) |
| "What did the summarizer actually see and say for this window?" | `memor window-review <session>` — per LLM call: the exact prompt (digest), the raw model response, the stored summary; windows from before the audit are shown as deterministic digest reconstructions |
| "Search my past work" | `memory_search` now also hits summaries |
| "Show me the whole tree" | `memor summaries --tree` |

What does **not** change: facts, decisions, and raw events remain the
source of truth; the old tools and CLI keep working; summaries never
overwrite or delete anything.

---

## 3. Control knobs

All in `~/.local/share/memor/config.json` (edit and it applies to the next
worker run; capture/serve restarts pick it up on restart).

| Key | Default | What it does |
|---|---|---|
| `session_journal_enabled` | `true` | Master switch for hook journaling |
| `session_harnesses` | `["dsh"]` | Which harnesses are journaled (the span reader is dsh-only today) |
| `session_dsh_root` | `~/.dsh` | Where DSH session logs live |
| `session_window_steps` | `6` | Agent tool steps per summary window (main sessions). User prompts never close a window — they're content, not boundaries; steps or Stop do |
| `session_subagent_window_steps` | `[12, 24]` | Wider windows for subagents, indexed by delegation depth − 1 (depth 1 → 12, depth 2+ → 24; the last entry caps). Subagents are long single-task runs, so coarser windows keep their series readable. Empty list = the uniform default |
| `session_chain_width` | `3` | How many sibling windows fold into one parent |
| `session_execution_mode` | `detached` | `detached` = a short-lived `memor window-flush` worker per window; `resident` = a `memor session-watch` loop polls |
| `session_max_flush_retries` | `3` | Failed LLM attempts before a window degrades to a rule stub |
| `session_prompt_chars` | `12000` | Budget per user prompt in the digest — high-value signal, almost never truncated (only paste-bombs) |
| `session_assistant_chars` | `8000` | Budget per assistant message (text only — **reasoning is never summarized**); head+tail snip, so long documents keep intro + ending |
| `session_tool_arg_chars` / `session_tool_result_chars` | `400` / `400` | Budgets per tool call / result — detail beyond these is reachable via the span drill-down |
| `session_digest_max_chars` | `24000` | Whole-window digest cap (head + gap marker + tail) — backstop for windows that carry big prompts *and* big prose |
| `session_context_enabled` | `true` | Whether injection context is generated at all |
| `session_context_max_chars` | `2000` | Cap on the injection package |
| `temporal_rollups_enabled` | `true` | Allow lazy day/week/month rollups |
| `verify_enabled` | `true` (code) / `false` (this box, post-switchover) | Old state-polling verify pass; off in the session-memory regime |

The regime switch itself is one command: `memor switchover` — it turns
consolidation and LLM autosync off and **lapses** the old pending LLM queue
(cursor advances, nothing is processed). It is meant to be run deliberately,
once, by a human.

---

## 4. Getting started

**On this machine** — already done:

1. ✅ Hooks mounted in the agent preset (`~/.dsh/.agent-presets/standard-ninfer/agent.cordis.yml` → `hooks-memor` row)
2. ✅ Switchover applied to the live store (consolidation/autosync/verify off, old queue lapsed)
3. ✅ Capture daemon restarted (regime in effect)
4. ✅ `memor serve` restarted (new tools registered)

**One thing left:** the preset rows mount when the DSH host process starts,
so journaling begins at the **next DSH host start** (or new session after a
restart). After that it's fully automatic — work, and summaries appear.

**On a fresh machine** (checklist):

1. Write the hooks row into your agent preset (see
   `integrations/dsh/README.md` — zero DSH core changes, it's just a
   plugin mount pointing at `integrations/dsh/hooks.json`).
2. Restart the host so hooks fire.
3. When you want the new regime (and to stop the old consolidation/verify
   loops): `memor switchover`, then restart the capture daemon.
4. Restart `memor serve` (or wait for DSH's auto-reconnect) so the new
   `memory_*` tools are available.

**Backfilling old sessions:** sessions that ran before journaling started
(or after a config change you want re-applied) can be processed from their
logs on demand — the input is deterministic (the log is immutable, and each
window boundary is a pure function of it: the Nth tool call), so backfill
reproduces exactly what live journaling would have produced:

```bash
memor sessions                          # what exists, and its status
memor backfill <session-id> --dry-run   # plan: N tool calls → W windows
memor backfill <session-id>             # run it (resumable; re-run continues)
memor backfill <session-id> --reprocess # cascade the old series away,
                                        # rebuild under current params
```

Resumable means exactly that: `--max-windows 3` stops partway, the pointer
sits in the store, the next run continues. If the LLM is down, windows stay
pending (`llm-unavailable`) and the next run picks them back up.

Backfill and live journaling share the same per-session state entry: if you
backfill a session and then continue it in DSH, the hooks pick up exactly
where the backfill's pointer stopped — backfilled and continued windows fold
into the same tree. (A backfill never races a live flush: with a flush
worker mid-flight it bails as `flush-in-progress`; a stale lock from a dead
worker expires via TTL.)

---

## 5. How to use it

### As an agent (the MCP pattern)

```
session start   →  memory_get_injection("<project>")        # brief me (capped)
                  →  memory_get_project_context("<project>") # as before
while working   →  normal memory_* writes (add_fact/decision/todo)
                  →  nothing else needed — hooks journal the rest
curious about a past session:
                  memory_get_summaries(scope_project=...)    # roots
                  memory_drill_down(summary_id)              # children / records / raw span
                  memory_get_span("dsh:<id>:seq[120..140]")  # raw conversation
"what was this week?":
                  memory_get_temporal(unit="week", start="2026-09-08")
```

### As a human (CLI, same store)

```bash
memor context <project> --injection      # the session-start brief
memor sessions                           # every DSH session + journaling status
memor backfill <id> --dry-run            # plan processing one old session
memor window-review <id>                 # what the summarizer saw/said per window
memor summaries --tree                   # the whole summary tree
memor summaries --workspace <cwd>        # one workspace's tree
memor span "dsh:<session>:seq[100..120]" # raw conversation window
memor temporal --unit week --start 2026-09-08
memor window-flush --all                 # manual catch-up flush (detached mode)
```

### What a summary looks like

```
## summary-2
Profiled the qwen server; found the prefill phase dominates on big contexts.

## Decisions
- inference-backend — use vLLM — llama.cpp 4x slower on this GPU

## Open
- finish the temporal rollup tests

## Records
- fact_a1b2c3d4: port:9000 — port 9000 was freed [state]
- dec_9f8e7d6c: backend — use vLLM [decision]
```

The last section is the session's own memor writes, attached as pointers
(ids only — the rows remain the truth). Records accumulate upward: a fold
carries the union of its children's records, and so do the day/week/month
rollups.

---

## 6. Expected behaviours (and surprises that aren't bugs)

- **Hooks are cheap on purpose.** Each hook = 1 row + a state bump
  (<100 ms). No LLM, no log reading, in the hot path. If a hook fails it's
  non-fatal — the session just continues un-journaled for that beat.
- **Windows are step-based, not prompt-based.** A window closes after
  `session_window_steps` agent tool steps (default 6 for main sessions;
  subagents get wider windows per `session_subagent_window_steps`, by
  delegation depth) or at Stop. One
  command that drives 100 steps becomes ~17 small windows — that's the
  point: fine-grained "what happened". Two quick commands may share a
  window (prompts are content, never barriers); the digest shows each
  prompt verbatim.
  In detached mode a worker is spawned for each ready window; if the LLM
  is busy or down, the window **stays pending and merges into the next** —
  nothing is lost. After `session_max_flush_retries` failures the window
  gets a rule stub (`status: partial`) that still carries the span, so the
  raw data stays reachable.
- **Every LLM exchange is audited.** Each window/fold call stores its exact
  prompt (the digest or child texts) and raw model response in a small
  `llm_calls` table — `memor window-review <session>` prints them next to
  the stored summary, so you can check the input/output of any
  summarization (including failed exchanges). Windows processed before
  auditing exists are shown as deterministic digest reconstructions.
- **Quiet sessions produce nothing.** A window with no prompts and <3 tool
  calls (probe traffic) is skipped — the pointer advances, no node is
  created.
- **Reasoning traces are never summarized.** The digest takes assistant
  *text* only; thinking is reachable only via the raw span.
- **The tree folds on its own, per workspace.** Every flush checks the
  frontier: `session_chain_width` unparented siblings of the same
  workspace get a parent — so a workspace's tree grows across all its
  sessions (main + subagents; a subagent's series folds into its
  workspace's tree, not into the parent session's). Workspaces never
  fold into each other.
- **Rollups are lazy.** A day/week/month node is materialized on first
  query and cached — querying again costs a read, not an LLM call.
- **After `memor switchover`:** no consolidation daemon work, no LLM
  autosync, no verify polling; old pending LLM windows are gone (lapsed,
  not processed). New memory comes from: capture (still running), agent
  writes (trusted-actor path, unchanged), and session summaries.
- **Nothing is deleted except on explicit reprocess.** Compaction, facts,
  and raw events keep their existing rules. The only deletion is
  `memor backfill <id> --reprocess`, which cascades away one session's
  derived series — its window summaries, the fold parents that reference
  them, and cached rollups (all lazily rebuildable; raw logs and
  facts/decisions are untouched) — and rebuilds it under the current
  params.

If something looks wrong, start here:
`memor summaries --tree` (is the tree there?) →
`memor span <span>` (does the raw log read?) →
`memor events` (are the `session/*` journal rows coming in?).
