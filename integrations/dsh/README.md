# memor × DSH integration

Wires DSH agent sessions into the memor session-memory pipeline
(SESSION-MEMORY.md). **Zero DSH core changes** — the bridge package
`@deepseek-ai/dsh-hooks-claude-code` runs ordinary Claude-Code-style command
hooks, and DSH's own zstd session logs supply the full-fidelity data the
summary workers read.

## What gets captured

| Hook event     | memor command                          | Journaled as      |
|----------------|----------------------------------------|-------------------|
| SessionStart   | `session-event --event start`          | `session/start`   |
| UserPromptSubmit | `session-event --event prompt`        | `session/prompt`  |
| PostToolUse    | `session-event --event tool`           | `session/tool`    |
| Stop           | `session-event --event stop`           | `session/stop`    |

Each hook hit is one SQLite row + a window-state bump (<100 ms). The
*content* of the session (prompts, assistant text, tool args/results,
thinking traces) is read later by the detached `window-flush` worker from
DSH's session log (`~/.dsh/sessions/<cwd>/<id>/session.v2.jsonl.zstd`) —
the hooks carry only the cheap markers.

Windows close after `session_window_steps` (default 6) agent tool steps or at
Stop (subagent sessions get wider windows by delegation depth via
`session_subagent_window_steps`, default `[12, 24]` — read from the session
record, no extra hook payload); each produces a level-1 summary node with a
span into the raw log (`<harness>:<session>:seq[lo..hi]`). 3–4 level-1
summaries fold into a level-2 parent, and so on (chain width 3), grouped per
**workspace** (a DSH cwd) — all sessions of one workspace, main and
subagents, form one tree, and different workspaces never merge. Existing
logs can be processed on demand: `memor sessions` (status per session) and
`memor backfill <id>` (deterministic, resumable, `--reprocess` to rebuild
one session's series under changed params).

## Install (DSH side)

1. Mount the bridge package in the DSH agent preset/session config
   (dynamic package), pointing `configPath` at `integrations/dsh/hooks.json`:

```yaml
- name: '@deepseek-ai/dsh-hooks-claude-code'
  config:
    configPath: /home/adlokib/workspace/projects/memory/integrations/dsh/hooks.json
```

   (`pluginRoot`/`projectDir` are unused — the command strings are absolute.)

2. Done. No other DSH configuration is needed; the session log is already
   written by DSH itself.

Hook failures are non-fatal to the agent (bridge semantics); a dead memor
store only delays summaries — raw data never depends on memor.

## Starting a session with session memory off

The per-session lever is the preset: journaling is wired by the
`hooks-memor` preset row, so a session started under a preset **without**
that row never touches the pipeline (no rows, no state, no tree growth).

Live setup: `~/.dsh/.agent-presets/standard-ninfer-untracked/` — a copy of
`standard-ninfer` minus the `hooks-memor` block (the `mcp-memor` row stays,
so on-demand memory reads remain available in exempt sessions). Select it
when starting a session that should not be journaled. The preset is fixed
at session start — there is no mid-session toggle.

## Execution modes

- `session_execution_mode: "detached"` (default): every closed window
  spawns a short-lived `memor window-flush` worker (detached from the
  agent process, lives one LLM call, exits). Nothing resident.
- `session_execution_mode: "resident"`: run `memor session-watch` once
  (e.g. a systemd user service) and it picks windows up from its poll
  loop. Flip by editing `memor.json` and restarting the watcher.

## SessionStart injection

`session_context_enabled: true` (default): every session starts with the
workspace's session-tree document (apex + mid series + newer-than-B + raw
tail, see SESSION-MEMORY.md §7) delivered as a session-start user message.

Delivery is wired through the hooks.json `SessionStart` point, which runs
two independent commands:

1. `memor session-event --harness dsh --event start` — journals the start
   (10 s timeout; the <100 ms journaling SLA).
2. `memor inject --envelope` — renders the tree document for the session's
   cwd (the bridge runs hooks in the session workspace) and prints it on
   stdout wrapped in the Claude-Code hook envelope:
   `{"hookSpecificOutput": {"hookEventName": "SessionStart",
   "additionalContext": "<doc>"}}`. The `dsh-hooks-claude-code` bridge
   merges `additionalContext` and calls `agent.inject(...)` at session
   start. Status goes to stderr, so it can never contaminate the
   envelope. An empty document (no summaries yet, or injection disabled)
   prints nothing — the bridge then injects no context.

The two commands are separate processes on purpose: an injection render
failure (or a slow one) can never delay or break journaling. Known caveat
(bridge semantics): the SessionStart hook runs detached, so a *very* slow
hook can miss the agent's first request; the render is pure SQLite+string
(tens to low-hundreds of ms) and the hook timeout is 60 s, so in practice
the context is in place for the first turn.

`hooks.json` is read once at DSH host process start — a change to it (like
adding the inject command) takes effect on the next host start, not for
already-running sessions.

Off-path retrieval (drill-down) is the MCP tools: `memory_get_summaries`,
`memory_drill_down`, `memory_get_span`, `memory_get_temporal`,
`memory_get_injection`, plus `memory_search` / `memory_get_project_context`
/ `memory_get_timeline`.

## Switchover (one-time, per store)

`memor switchover` applies the session-memory regime: consolidation
interval 0, LLM autosync off, state polling off, and the pending LLM queue
lapses (cursor advanced; nothing is processed). Restart the capture daemon
afterwards. See SESSION-MEMORY.md §9.
