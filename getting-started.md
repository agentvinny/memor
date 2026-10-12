# Getting started with memor

memor is a durable, tool-agnostic, temporal project/environment memory system.
It journals what happens on your machine — terminals, agents, git, Docker,
systemd, config files — and consolidates it into provenance-linked facts,
decisions, and relationships that any MCP-capable agent (or a human) can
query. Memory belongs to the environment and the projects, not to any
particular agent harness.

This guide covers setup on one machine and everyday use. The design rationale
lives in `agent_memory_research_brief.md`, `FINDINGS.md`, `PLAN.md`, and
`research/`.

## 1. One-time setup (per machine)

```bash
# 0. Install the package so `memor` is on your PATH
uv sync                                  # or: pip install -e .

# 1. Create the store (~/.local/share/memor; override with $MEMOR_DATA_DIR)
memor init

# 2. Register the projects you work on (longest path prefix wins)
memor add-project local-ai --path ~/work/ai --description "Local inference infra"
memor add-project auto-apply --path ~/work/auto-apply

# 3. Wire up the capture sources
memor install --claude                   # Claude Code: PostToolUse hook + MCP server (user scope)
memor install-git-hooks ~/work/ai ~/work/auto-apply   # point repos at the shared JSON hooks

# 4. Your interactive shell
echo "source <repo>/hooks/memor-shell-hook.bash" >> ~/.bashrc   # or the .zsh one
# re-login (or eval the printed snippet: memor shell-hook bash)

# 5. The always-on daemon (capture + scheduled consolidation)
memor capture install-systemd            # systemd user unit, or:
memor capture run --daemonize            # double-fork background, log in <data>/capture.log
```

Check health any time with `memor doctor` — it reports the store, the daemon,
each source (journal/docker/ss/atuin), LLM availability, and the Claude hook.

From then on, **you do nothing**: every command in your shell, every Claude
Code tool call, every commit/merge/checkout, container change, service event,
and config-file edit flows into the store, and the daemon consolidates it
into facts on a schedule (default hourly; `memor capture run
--consolidate-interval N` to change, `0` to disable).

### What gets captured

| Source | How |
|---|---|
| Your shell (bash/zsh) | sourced hook records every command + exit code + cwd + duration + session |
| Claude Code (or any harness with a similar hook API) | `PostToolUse` hook records the command **and its output** + session id — the only source of agent command output |
| Atuin | if installed, its `history.db` is polled (harness-agnostic terminal history) |
| Git | shared `post-commit`/`post-merge`/`post-checkout` hooks via `core.hooksPath`: SHA, branch, author, diffstat, checkout target |
| Docker | `docker events` streaming + periodic container snapshot diffing (image/status/port drift) |
| systemd | `journalctl -o json` tail: service lifecycle + errors; unit-*file* edits are covered by the filesystem watcher (the known journald gap) |
| Filesystem | inotify watch on configured paths (`/etc/systemd/system`, `~/.config`, …) with content kept, so config edits produce real diffs (`PORT=8000 → PORT=8080`) |
| Network/processes | `ss -tulpn` poll-diff: port bound/freed/process-swapped; an event is emitted only on change |

Everything lands in one append-only SQLite store (mirrored to monthly JSONL
files) — the raw evidence layer. The store is fenced against self-observation:
its own projection-repo commits can never feed back in as evidence.

To silence capture in a shell: `export MEMOR_DISABLED=1`.

## 2. During normal work

The only thing you typically type is **`memor remember`** — for the things
the environment can't observe: reasons, preferences, open questions.

```bash
memor remember "we pin vLLM 0.4 because 0.5 crashes on this GPU" \
  --project local-ai --component qwen-server --subject vllm-version \
  --kind decision --reason "segfaults at batch>8" --confidence confirmed
```

Kinds: `fact | state | configuration | preference | problem | lesson | todo | decision`
Confidences: `confirmed | probable | inferred | unknown`
(Only `confirmed`/`probable` become current facts; the rest are queued for review.)

Everything else — commands run, ports bound, files changed, services started —
is captured automatically, including changes you make *without* telling any
agent: the watchers see the state change even when nobody reports it.

**Project vs. machine scoping.** Attribution is location-based: a command
lands under the *registered* project whose path contains your cwd; an
unregistered git repo becomes a pseudo-project named after its directory
(register it by path or git remote to give that identity a stable name); an
unregistered, non-git directory is already box-wide. So nothing needs
registering to be *captured* — registering is how a directory gets a
stable project identity. If you're doing **general/machine-wide work from
inside a project folder**, mark it so it isn't attributed to the project:

- one command: `MEMOR_NO_PROJECT=1 systemctl daemon-reload`
- a whole session: `export MEMOR_NO_PROJECT=1` (and `unset` it when done)
- the wrapper: `memor run --no-project <cmd>`

Such events are recorded box-wide (machine scope) and show up in machine
views/timelines, not the project's.

### Optional: LLM-assisted extraction

With `ANTHROPIC_API_KEY` set (model via `MEMOR_LLM_MODEL`, default
`claude-sonnet-5`), or a local OpenAI-compatible server via `llm_base_url`
(e.g. `http://localhost:8000/v1`), consolidation asks the LLM to propose
additional facts/decisions/relations. The LLM may only cite evidence ids it
was given; anything uncited is dropped. Without any LLM configured, the
deterministic rules run alone and everything still works.

Two noise controls keep local models from drowning in churn:

- **Batched windows, not one-by-one.** Each LLM call receives up to 200
  recent events in chronological order (per project/machine group), so
  sequences of related commands reach the model together, in context.
- **Usage commands are withheld.** Commands matching `ignore_command_prefixes`
  (defaults: `cd ls pwd clear history man which type help file` — pure
  navigation/usage, nothing happening) stay in the raw journal but are never
  offered to the LLM; a window that is entirely noise skips the LLM call
  itself. Extend the list in `memor.json` for machine-specific noise, e.g.
  your LLM server's start command (`"ignore_command_prefixes": ["ninfer-serve", …]`)
  — its port binding is still captured by the network poller, so no state is
  lost.

**The LLM queue (works while the LLM is down).** The LLM keeps its own
cursor, separate from the rules pass: events processed rules-only stay
*pending* for the LLM, nothing is skipped because the model was offline.
`memor llm sync` drains the queue on demand (R3); with `llm_autosync: true`
the daemon probes the endpoint every `llm_sync_interval` seconds (default
10 min) and drains the queue whenever the server is online (occasional
passes, zero work when it's down). Housekeeping events (daemon
bookkeeping, A1 verification passes) are withheld from the LLM window, so
the model only ever sees real-world activity. Check `memor llm status` /
`memor doctor` for provider, availability, and queue depth.

## 3. Asking questions (in a terminal)

```bash
memor context local-ai        # "catch me up" — state, relations, decisions, recent changes, open items
memor current qwen-server     # what's true now (also: project:local-ai, machine:desktopU, component:z)
memor history qwen-server     # every state ever, incl. superseded (A→B→A shows all three)
memor related qwen-server     # runs_on / depends_on graph, plus facts about the neighbors
memor decisions --project local-ai
memor evidence <fact_id>      # "why do you believe this?" → the raw commands/diffs behind it
memor recent --hours 72       # what changed since I was away
memor search "port 8080"      # full-text over events + facts + decisions
memor events --source git     # browse raw Level-4 evidence
memor report local-ai         # human-oriented catch-me-up
```

## 4. For agents

Any MCP-capable agent (Claude Code, Cursor, OpenCode, …) talks to
`memor serve` (stdio MCP). `memor install --claude` registers it with Claude
Code at user scope; other MCP clients add `memor serve` as a server. The
tool surface:

```
memory_search, memory_get_current_state, memory_get_history,
memory_get_related, memory_get_decisions, memory_get_evidence,
memory_get_project_context, memory_get_recent_changes
```

The intended pattern:

- **Start of a task** → `memory_get_project_context("local-ai")` — one compact
  package instead of re-exploring the environment
- **Need a detail** → `memory_get_current_state` / `memory_get_history` /
  `memory_get_related`
- **About to act on something weird** → `memory_get_decisions` +
  `memory_get_evidence` to learn whether it's deliberate, accidental,
  temporary, or obsolete
- **Only if still unsure** → pull raw evidence
  (`include_evidence=true` / `memory_get_evidence`)

Retrieval is cheapest-first: exact scope → entity → current state → relations
→ decisions → history; raw evidence only when asked. Keyword/full-text search,
no embeddings (a deliberate early-stage choice).

Because Claude Code's hook feeds every Bash call (command + output + session)
into the store, an agent's work lands in the same memory as your manual work.
That is what makes agent A → agent B handoff work: the knowledge lives in the
store, not in the harness.

## 5. The human review loop

Consolidation auto-applies high-confidence, non-conflicting claims. The
review queue receives only what a machine *inferred* and that needs a human
call: **LLM-derived low-confidence guesses, contradictions (any source), and
surprises/recurring conflicts.** Deliberate writes by trusted actors — you
via `memor remember`, or an agent/harness via the MCP tools — are **not**
queued; they become current with their provenance and confidence visible
(toggle with `trust_agent_writes`). So the queue stays small and meaningful:

```bash
memor review                          # list open items (LLM guesses, contradictions, surprises)
memor review accept rev_abc123         # it becomes current, supersedes the old fact
memor review reject rev_def456 --note "wrong, it's actually Y"
```

Contradictions are never silently overwritten — the old fact stays current
until you decide. This is what keeps a wrong LLM guess or a stale reading
from becoming "the truth".

## 6. The human-readable layer

- `<data>/system/` — small, always-useful: `INDEX.md` (projects, machines,
  active services, open review items) and one summary file per project
  (purpose, components, current state, major decisions, known problems,
  uncertain items, relations).
- `<data>/reference/` — per-component detail: current state, history with
  supersession dates and reasons, decisions, relations; plus per-machine pages.

These files are regenerated on every consolidation pass. Git commits are
throttled to **at most one per `projection_commit_interval` (default: daily)**
on daemon runs — the commit message summarizes the window since the last
commit (facts added/changed, decisions, top subjects, queue state) — while
manual commands (`consolidate`, `review`, `remember`, `digest`, `correct`)
commit immediately. No-op passes rewrite nothing (a changed `updated:`
stamp alone doesn't count), so a commit means something actually changed.
`git -C ~/.local/share/memor log -p` is therefore a compact, diff-able
history of "what we believed at each point in time". The SQL store is the
authoritative temporal record; the Markdown tree is its human-readable
projection.

## 7. Typical scenarios

| Situation | What you do |
|---|---|
| Back from a month away | `memor context <project>` — or tell your agent "catch me up on X" |
| "Why is this port 8080?" | `memor history <component>` → reason + `memor evidence <id>` |
| "Did we ever try Y?" | `memor search Y` → events/facts/decisions |
| "What will break if I remove Z?" | `memor related Z` |
| Undocumented manual change you forgot about | `memor recent` / `memor context` — the watchers saw it even though you told nobody |
| A new agent takes over the project | nothing to do — it reads the same MCP store |
| "Is this setting deliberate or accidental?" | `memor current <entity>` (shows confidence + uncertain/conflicting items) + `memor evidence` |

## 8. Operating it

```bash
memor capture status          # is the daemon alive?
memor capture stop
memor consolidate --dry-run   # preview what the next pass would do
memor doctor                  # full health check
```

Notes:

- **Per-machine.** Each box runs its own daemon and store. The machine is a
  first-class scope (`machine:desktopU`), so per-box state (services, ports)
  stays correctly attributed.
- **Data locations.** Store: `~/.local/share/memor` (override with
  `$MEMOR_DATA_DIR`): `memor.db` (SQLite), `events/YYYY-MM.jsonl` (raw
  evidence mirror), `system/` + `reference/` (Markdown, git repo),
  `memor.json` (config: projects, watch paths, intervals).
- **Config knobs** (edit `memor.json`): `watch_paths`, `fs_interval`,
  `net_interval`, `docker_snapshot_interval`, `consolidate_interval`,
  `projection_commit_interval` (git commit cadence of the Markdown
  projection on daemon runs; manual commands always commit immediately),
  `llm_model`, `llm_enabled`, `llm_base_url` (local OpenAI-compatible
  server, e.g. `http://localhost:8000/v1`), `llm_autosync` (daemon drains
  the LLM queue whenever the server is back online) + `llm_sync_interval`,
  `ignore_command_prefixes` (usage-noise commands withheld from the LLM),
  `trust_agent_writes` (agent/harness writes trusted like human ones; off
  reverts them to the evidence-gated review queue), `max_store_size_bytes`
  (store-footprint alert threshold, default 5 GiB, 0 disables: `doctor`,
  the digest, and agent context packages all warn when exceeded),
  `max_output_chars`.

## 9. Current limits (by design, not bugs)

- LLM extraction is implemented but not covered by the test suite (the rules
  path is fully tested); exercise it with an API key.
- No semantic/embedding search, no multi-machine federation, no
  browser/GUI observation — deferred until real usage shows the need.
- It remembers what it observed. A change with no observable trace (no
  command, no file edit, no service change) is only caught if some watched
  state actually changes.