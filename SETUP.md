# SETUP — understanding memor, and running it on your machine

This document has two jobs:

1. **Understanding** — a map of the codebase and its moving parts, for any agent
   (or human) that wants to know what memor is, how it works, and where each
   behavior lives.
2. **Setup** — everything it takes to run memor on a fresh machine, with
   **every optional feature as an explicit opt-in control**: what you get,
   the command that enables it, the way to disable it, and what the default is.

It is *not* the design rationale (see `PLAN.md`, `research/`,
`SESSION-MEMORY.md`) and *not* the interface contract (see `AGENT-GUIDE.md` —
that file is the authoritative source for the exact tool/command surface).
Everyday operations once you're running: `getting-started.md`.

```
README.md            — what it is, architecture at a glance
SETUP.md (this file) — understand the project + opt-in setup matrix
AGENT-GUIDE.md       — the contract: every MCP tool, CLI command, hard rules
getting-started.md   — one-machine setup walkthrough + everyday use
SESSION-MEMORY.md    — design of the session-memory subsystem (tree, spans, injection)
GAPS.md / FUTURE-WORK.md — what's built, what's next
```

---

## 1. What memor is (30-second version)

memor is durable, tool-agnostic, **temporal** memory for a machine and its
projects. It has two independent subsystems that share one store:

- **Environment memory** — journals what *actually happens* on the box
  (shell commands, agent tool calls, git activity, Docker, systemd, config
  file edits, port/process changes), then consolidates that raw evidence
  into provenance-linked **facts, state, decisions, lessons, and relations**
  with confidence levels and validity windows. Contradictions and low-
  confidence guesses go to a human review queue; nothing is ever silently
  overwritten — truth changes are *supersedes*, and the old value stays
  readable in history.

- **Session memory** — journals *agent sessions* (via command hooks + the
  harness's own session logs), folds them into a hierarchical summary tree
  (work window → L1 → L2 → … per workspace), and injects a compact "where
  do I stand" document at session start, with drill-down to raw events at
  any point.

Memory belongs to the **environment**, not to any agent or session: an agent
dies, its harness dies, a person goes on vacation — the memory outlives all
of them, and a fresh agent can be brought up to speed from it.

```
sources (shell / agents / git / docker / systemd / files / network)
        │  hooks, watchers, poll-diffs → raw events (redacted on ingest)
        ▼
journal (SQLite + monthly JSONL mirror)  ◄──── session logs (harness)
        ▼
consolidation: deterministic rules + optional LLM queue
        ▼
facts / state / decisions / relations  (temporal, evidence-linked)
        ▼
┌──────────────────────┬──────────────────────┐
│ MCP server (25 tools)│ CLI (46 commands)    │ + Markdown projection
│ + stdio `memor serve`│ (humans, scripts)    │   (git-versioned, human view)
└──────────────────────┴──────────────────────┘
```

**The invariants** (memor enforces these; don't fight them):

- *Cite evidence or don't assert.* Records backed by journal event ids can
  become current; records without evidence are queued for human review.
- *Supersede, don't overwrite.* `correct` and `retract` preserve history.
- *Confidence is data.* `unknown` / `contradictory` / `inferred` items are
  hypotheses, not truth.
- *The store is append-only evidence.* Only the tools/CLI write to it;
  the Markdown tree is a projection.

---

## 2. Repo map

```
memory/
├── memor/                     # the package (all of it)
│   ├── cli.py                 # 46-command CLI (control surface for everything)
│   ├── store.py               # SQLite schema, migrations, temporal read/write
│   ├── config.py              # Config dataclass — every knob in the repo lives here
│   ├── models.py              # domain objects (facts, decisions, events)
│   ├── scope.py               # project/machine/component scoping + attribution
│   ├── search.py              # FTS + semantic hybrid search (rank-fused)
│   ├── embed.py               # embedder (local model / OpenAI-compatible API / off)
│   ├── sessionlog.py          # DSH session-log parsing (zstd JSONL)
│   ├── sessionmem.py          # session-memory engine: windows, folds, flush, injection
│   ├── sessionlog.py          # harness session-log reader
│   ├── wstate.py              # workspace state (structured current-state surface)
│   ├── mcp_server.py          # stdio MCP server (25 tools)
│   ├── consolidate/           # environment-memory consolidation
│   │   ├── rules.py           # deterministic rules (no LLM needed)
│   │   ├── extract.py        # LLM extraction (proposals, evidence-cited only)
│   │   ├── apply.py           # trust/review gating → current facts
│   │   ├── digest.py          # periodic human digest
│   │   ├── markdown.py        # Markdown projection + throttled git commits
│   │   └── episodes.py        # work-episode rollups
│   └── capture/               # the capture layer
│       ├── daemon.py          # always-on daemon (watchers + scheduled consolidation)
│       ├── terminal.py        # shell-hook event intake
│       ├── journal.py         # raw-event journaling (redaction)
│       ├── githooks.py        # shared JSON-emitting git hooks
│       ├── fs_watch.py        # inotify watcher with content diffs
│       ├── docker_events.py   # docker events stream + snapshot diffing
│       ├── netproc.py         # ss poll-diff (ports/processes)
│       └── verify.py          # re-observe reality, reconcile state facts
├── hooks/
│   ├── memor-shell-hook.bash  # sourced in .bashrc — journals every command
│   ├── memor-zsh-hook.zsh     # zsh twin
│   └── memor-claude-hook.sh   # agent PostToolUse hook (command + output)
├── integrations/dsh/
│   ├── README.md              # the DSH bridge: what, why, install, opt-out
│   └── hooks.json             # the actual hook config (⚠ machine-specific paths)
├── tests/                     # 244 hermetic tests (offline; no store, no LLM)
├── .github/workflows/test.yml # CI: 3.10/3.11/3.12, uv sync --frozen, pytest
├── uv.lock                    # pinned dependency graph
├── pyproject.toml             # Python ≥3.10; deps: mcp, anthropic, watchdog, fastmcp
└── (docs at the root — see the reading list above)
```

Document roles, so you know what to trust:

| Doc | Role |
|---|---|
| `AGENT-GUIDE.md` | **Contract.** Kept in sync with the code; if a tool/command in it doesn't match reality, the guide is stale. |
| `getting-started.md` | One-machine setup + everyday use (core system; predates session memory). |
| `SESSION-MEMORY.md` | Design spec for session memory: windows, folding, injection, spans. |
| `SESSION-MEMORY-GUIDE.md` | Plain-language companion to the above. |
| `PLAN.md`, `FINDINGS.md`, `agent_memory_research_brief.md`, `research/` | Why it's built this way; the landscape. |
| `GAPS.md`, `FUTURE-WORK.md` | Build log and sequenced follow-ups. |
| `chatgpt-review.md`, `summary-review/context-sample.md` | **Example data** — a real third-party review, and a real rendered session-injection document from the author's machine (useful as a concrete sample; not a spec). |
| `window-digest-samples/` | Generated digest samples (`make_samples.py` is the source). |

---

## 3. How the pieces fit

### Environment memory (the core loop)

```
capture layer ──► journal ──► consolidation ──► store ──► surfaces
```

1. **Capture** turns real activity into raw events. Each source is independent;
   enable any subset (Section 5). Shell commands via a sourced hook (command +
   exit code + cwd + duration + session); agent tool calls via the harness's
   command hooks (command **+ output** — the only source of agent output); git
   via shared `post-commit/post-merge/post-checkout` hooks (`core.hooksPath`);
   Docker via `docker events` + periodic snapshot diffing; systemd via
   `journalctl` tail; config files via inotify watcher with content kept (so
   edits produce real diffs, `PORT=8000 → PORT=8080`); network via `ss`
   poll-diff (event only on change). Everything is redacted on ingest.
2. **Journal** is append-only: SQLite (`memor.db`) + monthly JSONL mirrors
   under `events/`. The store is fenced against self-observation: the
   projection repo's own commits never feed back in as evidence.
3. **Consolidation** runs on a schedule (default hourly) inside the daemon, or
   manually. The deterministic rules path is always on and fully tested. An
   optional LLM path proposes extra facts/decisions/relations from batched
   event windows (up to 200 events/call); the LLM may only cite evidence ids
   it was given, and it keeps its own cursor — a downed LLM never loses work.
4. **Store** = temporal truth: every record has a validity window, confidence,
   evidence pointers, and supersedes chains. The review queue holds what needs
   a human call (LLM guesses, contradictions, surprises).
5. **Surfaces**: the MCP server (25 tools, stdio), the CLI (46 commands), and
   the Markdown projection (`system/` + `reference/`, git-committed at most
   daily on daemon passes — manual commands commit immediately).

Attribution is location-based: an event lands under the *registered* project
whose path contains the cwd; unregistered git repos get a pseudo-project;
anything else is box-wide (`machine:` scope). `MEMOR_NO_PROJECT=1` (or
`memor run --no-project`) forces box-wide attribution.

### Session memory (the agent-session pipeline)

```
DSH session ──hooks (cheap markers)──► journal (SQLite rows)
    │                                          │
    └── zstd session log (full fidelity) ─► window-flush worker
                                              │  (one LLM call per closed window)
                                              ▼
                                    L1 summaries (spans into raw log)
                                              │  3 siblings fold → L2 → L3 → …
                                              ▼
                                    workspace summary tree (per cwd)
                                              │
                 ┌────────────────────────────┼─────────────────────────┐
                 ▼                            ▼                         ▼
        SessionStart injection     drill-down: span /               workspace
        (tree doc as a user msg)    summaries --tree / temporal       state surface
```

- **Journaling** is lifecycle-only: four hooks (SessionStart,
  UserPromptSubmit, PostToolUse, Stop) each insert one row + bump window state
  (<100 ms SLA). The *content* is read later from the harness's own session
  log — the hooks carry only markers.
- **Windows** close after `session_window_steps` (default 6) agent tool steps
  or at Stop; subagents get wider windows by delegation depth
  (`[12, 24]`; a `-1` entry disables windowing for that depth and below).
  Each closed window becomes an L1 summary with a **span**
  (`<harness>:<session>:seq[lo..hi]`) pointing at the raw log.
- **Folding**: `session_chain_width` (default 3) unparented siblings at level k
  roll up into one level-k+1 parent. Grouped per **workspace** (a cwd) — all
  sessions of one workspace, main + subagents, form one tree; workspaces
  never merge.
- **Execution modes**: `detached` (default) — each closed window spawns a
  short-lived `memor window-flush` worker; `resident` — run
  `memor session-watch` once and it polls. LLM failure degrades a window to a
  rule stub (`status="partial"`) after retries; raw data is never at risk.
- **Injection**: with `session_context_enabled` (default on), every session
  starts with the workspace tree document (apex + mid series + newer + raw
  tail) delivered as a session-start user message via the hook envelope.
  Summaries are *navigation, not truth* — verify claims at the raw level
  (span/evidence) before acting on them.
- **One-time switchover**: `memor switchover` applies the session-memory
  regime to an existing store (consolidation interval → 0, LLM autosync off,
  state polling off, pending LLM queue lapses; `--keep-verify` keeps
  polling). **The user runs it, never an agent.** Restart the daemon after.

---

## 4. For agents (or humans) working on the code

**Environment.** Python ≥ 3.10. `uv` is the canonical manager (`uv.lock` is
committed); plain `pip install -e .` works.

```bash
uv sync              # deps incl. pytest (dev group)
uv run pytest        # 244 tests, hermetic: no real store, no LLM, no network
```

CI (`.github/workflows/test.yml`) runs the same suite on 3.10/3.11/3.12 with
`MEMOR_DATA_DIR` pointed at a tmp dir and `ANTHROPIC_API_KEY` emptied — that
env setup is the pattern tests themselves use, and the pattern you should use
when running the app in isolation.

**Where behavior lives** (quick index):

| Behavior | File(s) |
|---|---|
| Schema, migrations, temporal reads | `memor/store.py` |
| All config knobs + defaults | `memor/config.py` (one file — the whole control surface) |
| Consolidation rules (deterministic) | `memor/consolidate/rules.py` |
| LLM extraction + queue | `memor/consolidate/extract.py`, `memor/cli.py` (`llm`) |
| Trust/review gating | `memor/consolidate/apply.py` |
| Markdown projection + git commit throttle | `memor/consolidate/markdown.py` |
| Session windows/folds/flush/injection | `memor/sessionmem.py`, `memor/sessionlog.py` |
| Workspace state | `memor/wstate.py` |
| Hybrid search (FTS ∪ semantic) | `memor/search.py`, `memor/embed.py` |
| Daemon + capture sources | `memor/capture/` |
| MCP tools | `memor/mcp_server.py` (surface documented in `AGENT-GUIDE.md`) |

**Invariants to preserve when changing code:**

- The rules path must stay LLM-free and fully tested; the LLM path is *not*
  covered by the suite (by design — keep it that way or add the coverage).
- Tests are hermetic: they build temp stores under `tmp_path`, never touch
  `~/.local/share/memor`, never call a real LLM.
- Migrations go in `_migrate()`, not `SCHEMA` (schema changes that assume a
  fresh DB crash old stores — this bug happened and was fixed; don't
  reintroduce it).
- Evidence citations are mandatory at the store layer; don't add a write path
  that bypasses the review gate for ungated claims.
- The DSH hook scripts and `session-event` must stay <100 ms and non-fatal:
  they run inside the agent's turn.

**A real-world gotcha (learned the hard way):** if the package is installed
editable (`.venv` → shared venv, as on the author's box), **checking out an
older tree (e.g. an old `main`) temporarily removes newer modules from
`memor`** — command hooks like `memor session-event` start failing until a
newer tree is checked out or the merge lands. The hook failures are non-fatal
to any agent session, but if your shell/agent tooling suddenly starts spewing
`memor: error: invalid choice` after a git checkout, this is why.

---

## 5. Setting up on a machine — the opt-in matrix

Every row below is an independent control. "Default" is what you get if you
never touch it.

| # | Feature | Default | Enable | Disable |
|---|---|---|---|---|
| A | Store + CLI (the core) | — | always, first step | `memor` is useless without it |
| B | Project registration | none | `memor add-project NAME --path DIR` | `memor forget` / edit `memor.json`; capture keeps working (pseudo-projects) |
| C | Shell capture (bash/zsh) | off | source the hook (recipe below) | `export MEMOR_DISABLED=1` (per shell) |
| D | Agent-harness capture (Claude Code) | off | `memor install --claude` | `claude mcp remove memor`; remove the hook from `~/.claude/settings.json` |
| D' | DSH harness capture | off | mount the bridge package + `integrations/dsh/hooks.json` (see below) | start the session under a preset without the `hooks-memor` row (per-session opt-out) |
| E | Git capture | off | `memor install-git-hooks REPO…` | `git config --unset core.hooksPath` |
| F | Capture daemon (docker/systemd/net/fs watchers + scheduled consolidation) | off | `memor capture install-systemd` (user unit) **or** `memor capture run --daemonize` | `memor capture stop`; or run nothing and call `memor consolidate` by hand |
| G | LLM-assisted extraction | on, but inert without an endpoint | set `ANTHROPIC_API_KEY` **or** `llm_base_url` in `memor.json` | `llm_enabled: false` (or just no key/endpoint) |
| H | MCP server (any MCP client) | available, not registered | `memor serve` as a server entry (stdio) | remove the server entry |
| I | Semantic search (embedder) | **on, local model** | it is; `embed_provider: "local" \| "api" \| "off"` | `embed_provider: "off"` → FTS-only search |
| J | Session journaling (hooks + summary tree) | on, if hooks are wired | wire the DSH bridge (D') / Claude hooks (D) | preset opt-out (D'); `session_journal_enabled: false` |
| K | Session-memory regime on an existing store | not applied | `memor switchover` (**user**, one-time) + daemon restart | `memor switchover --verify` re-enables polling; restore intervals in `memor.json` |
| L | Workspace-state file projection (`AGENTS.local.md`) | **off** | `session_state_project_file: true` or `memor state project` | leave off — state rides the injection envelope + `memory_get_workspace_state` instead |
| M | Trust agent/harness writes | on | it is; `trust_agent_writes: true` | `trust_agent_writes: false` → agent writes go through the evidence-gated review queue like LLM guesses |

### Step 0 — install the package (required)

```bash
# needs Python ≥ 3.10; uv is canonical (uv.lock committed)
uv sync                    # or: python -m venv .venv && . .venv/bin/activate && pip install -e .
memor --version            # → 0.1.0
```

### Step 1 — create the store (required)

```bash
memor init                 # ~/.local/share/memor  (override: export MEMOR_DATA_DIR=…)
```

Creates `memor.db`, the `events/` JSONL mirror dir, the git-backed Markdown
projection (`system/`, `reference/`), and `memor.json` (your config file —
every knob in Section 6).

### Step 2 — pick your recipe

**Minimal** (read/query only, nothing resident):

```bash
memor init
memor add-project myproject --path ~/work/myproject --description "…"
memor remember "baseline: repo checked out 2026-…" --project myproject --kind state
memor context myproject
```

No daemon, no capture, no LLM. You can still feed it evidence manually
(`memor remember`, MCP tools) and consolidate by hand (`memor consolidate`).

**Standard** (what most people want — the box journals itself):

```bash
memor init
memor add-project myproject --path ~/work/myproject
# shell capture (bash; for zsh use the .zsh twin, or: memor shell-hook bash|zsh prints the snippet)
echo "source <repo>/hooks/memor-shell-hook.bash" >> ~/.bashrc && source ~/.bashrc
# git capture for your repos
memor install-git-hooks ~/work/myproject
# the daemon (systemd user unit — recommended; survives reboots, nohup-free):
memor capture install-systemd
memor capture status        # verify: active (running)
# optional but useful: point an LLM at it (see G)
export ANTHROPIC_API_KEY=…  # or set llm_base_url in memor.json
memor llm status            # provider + queue depth
```

**DSH-full** (everything, including session memory):

```bash
# Standard, plus:
# 1. The DSH bridge (no DSH core changes — the package @deepseek-ai/dsh-hooks-
#    claude-code runs Claude-Code-style command hooks; configPath → integrations/dsh/hooks.json)
#    ⚠ hooks.json contains the author's absolute paths — rewrite them to YOUR
#    venv's memor binary and YOUR repo path first (or generate an equivalent).
# 2. Start sessions under the preset that mounts the `hooks-memor` row
#    (see integrations/dsh/README.md for the exact preset shape).
# 3. One-time, the USER applies the session-memory regime and restarts the daemon:
memor switchover            # consolidation interval 0, autosync off, polling off,
                            # pending LLM queue lapses — then: memor capture stop && memor capture install-systemd
```

From then on, **you do nothing** in the standard/DSH recipes: activity flows
in, the daemon consolidates, `memor doctor` shows health of every layer, and
`memor context <project>` is the one command you'll type in a hurry.

### Per-feature details

**C — shell capture.** The sourced hook wraps command execution to record
command + exit code + cwd + duration + session id into the journal. Bash and
zsh twins in `hooks/`; `memor shell-hook bash` / `memor shell-hook zsh` print
the exact snippet to source. Silence one shell without touching rc files:
`export MEMOR_DISABLED=1`.

**D — Claude Code.** `memor install --claude` registers (a) the `PostToolUse`
command hook from `hooks/memor-claude-hook.sh` (records the command *and its
output* + session id — the only source of agent output) and (b) the MCP server
at user scope. Any harness with a similar hook API can adopt the same script
(`memor agent-event` is the intake).

**D' — DSH.** See `integrations/dsh/README.md`: four hook points (start /
prompt / tool / stop), each one SQLite row + window bump (<100 ms); content
comes from DSH's own zstd session log, so raw data never depends on memor.
**Per-session opt-out:** start the session under a preset *without* the
`hooks-memor` row — no rows, no state, no tree growth, but MCP memory reads
stay available. The preset is fixed at session start; there is no mid-session
toggle.

**E — git capture.** `memor install-git-hooks` points each repo's
`core.hooksPath` at the shared JSON-emitting hooks (`post-commit`,
`post-merge`, `post-checkout`): SHA, branch, author, diffstat.

**F — the daemon.** One process does everything resident: fs watcher,
network poll, Docker events, journald tail, atuin (if installed), plus
scheduled consolidation + digest + LLM autosync. `install-systemd` writes a
**user** unit (`memor-capture.service`); `--daemonize` double-forks with the
log in `<data>/capture.log` (no systemd, e.g. in a container). Interval knobs
are in `memor.json` (Section 6). Manual mode: no daemon at all — run
`memor consolidate`, `memor digest`, `memor llm sync` from cron or by hand.

**G — LLM extraction.** Works with the Anthropic API (`ANTHROPIC_API_KEY`,
model via `MEMOR_LLM_MODEL` / `llm_model`, default `claude-sonnet-5`) or any
OpenAI-compatible server (`llm_base_url`, e.g. a local vLLM; optional
`llm_api_key` — literal or path-to-file, re-read per call; `llm_provider_only`
pins OpenRouter upstreams). With nothing configured, the deterministic rules
run alone and everything still works — the LLM is strictly optional. The LLM
queue survives outages (`memor llm status` shows depth; `llm_autosync: true`
drains it whenever the endpoint is back).

**H — MCP.** `memor serve` is a stdio MCP server (25 tools; the full surface
is in `AGENT-GUIDE.md`). Any MCP client registers it the usual way; Claude
Code gets it for free from `memor install --claude`.

**I — semantic search.** Search is hybrid: FTS ∪ semantic, rank-fused
(`memor search` / `memory_search` get both axes without asking). The embedder
is configurable: `embed_provider: "local"` (default — runs
`BAAI/bge-large-en-v1.5` on this machine, GPU index via `embed_device` or
`"cpu"`), `"api"` (any OpenAI-compatible `/embeddings`, via
`embed_api_base`/`embed_api_key`), or `"off"` (FTS-only; everything else keeps
working). Vectors are cached in the store's `embeddings` table, embedded
lazily.

**K — switchover.** For stores that already ran the environment-memory
regime: `memor switchover` moves them onto the session-memory regime
(consolidation interval → 0, LLM autosync off, state polling off, pending LLM
queue lapses — cursors advance, nothing is processed). `--keep-verify` keeps
state polling on. It's a one-time, per-store, **user-run** operation; agents
never run it. Restart the daemon afterwards.

**L — workspace state.** A structured current-state surface per workspace
(decisions, conventions, open items) that agents maintain. Off by default
because `AGENTS.local.md` is DSH's instruction file and a churning one is a
token tax — state is delivered through the session-start injection envelope
and fetched on demand (`memory_get_workspace_state`, `memor state show`)
instead. Flip `session_state_project_file` on only for tooling that reads the
file.

---

## 6. Config reference (`~/.local/share/memor/memor.json`)

Everything in `memor/config.py` is overridable here; env vars win over the
file for a few (`MEMOR_DATA_DIR`, `ANTHROPIC_API_KEY`, `MEMOR_LLM_MODEL`,
`MEMOR_DISABLED`, `MEMOR_NO_PROJECT`).

**Core / store**

| Knob | Default | Controls |
|---|---|---|
| `projects` | `[]` | registered projects (name, paths, remotes, description) |
| `max_store_size_bytes` | 5 GiB | footprint alert threshold in `doctor`/digest/context; 0 disables |
| `retention_months` | 12 | evidence retention for `memor compact` |
| `trust_agent_writes` | `true` | feature M: agent/harness writes trusted vs. review-queued |

**Capture intervals (daemon)**

| Knob | Default | Controls |
|---|---|---|
| `watch_paths` | systemd/docker/user-config dirs | inotify watch list (config edits → real diffs) |
| `fs_interval` | 2 s | fs watcher poll granularity |
| `net_interval` | 20 s | port/process poll-diff interval |
| `docker_snapshot_interval` | 900 s | container snapshot diffing |
| `verify_interval` | 300 s | state re-observation (A1); off after `switchover` |
| `consolidate_interval` | 3600 s | scheduled consolidation; 0 = rules pass disabled (session regime) |
| `projection_commit_interval` | 86400 s | Markdown-projection git-commit cadence on daemon runs (manual commands always commit immediately) |

**LLM**

| Knob | Default | Controls |
|---|---|---|
| `llm_enabled` | `true` | master switch for the LLM path |
| `llm_model` | `claude-sonnet-5` | model for extraction/summaries |
| `llm_base_url` | `""` | OpenAI-compatible endpoint (local server); empty = Anthropic API |
| `llm_api_key` | `""` | for `llm_base_url`; literal or path-to-file (re-read per call) |
| `llm_provider_only` | `[]` | OpenRouter upstream pin (e.g. `["xiaomi"]`) |
| `llm_autosync` | `false` | daemon drains the LLM queue when the endpoint is back |
| `llm_sync_interval` | 600 s | autosync probe period |
| `llm_max_tokens` | 16384 | completion cap per extraction call |
| `consolidate_batch` | 500 | events per consolidation batch |
| `ignore_command_prefixes` | cd/ls/pwd/clear/history/man/which/type/help/file | usage-noise withheld from LLM windows (extend per machine) |

**Session memory**

| Knob | Default | Controls |
|---|---|---|
| `session_journal_enabled` | `true` | master switch for session journaling |
| `session_harnesses` | `["dsh"]` | harnesses whose hooks are wired |
| `session_dsh_root` | `~/.dsh` | DSH session-log root (zstd JSONL) |
| `session_window_steps` | 6 | tool steps per window (prompts never close a window) |
| `session_subagent_window_steps` | `[12, 24]` | per delegation depth; `-1` disables that depth and below |
| `session_chain_width` | 3 | siblings per fold level |
| `session_execution_mode` | `detached` | `detached` (per-window worker) vs `resident` (`memor session-watch`) |
| `session_max_flush_retries` | 3 | LLM retries before a window degrades to a rule stub |
| `session_catchup_budget_seconds` | 900 | wall-clock cap per catch-up worker run |
| `session_prompt_chars` | 12000 | digest budget per user prompt (head cut) |
| `session_assistant_chars` | 8000 | per assistant message (head+tail) |
| `session_tool_arg_chars` / `session_tool_result_chars` | 400 / 400 | per tool call / result |
| `session_digest_max_chars` | 24000 | whole-window digest cap |
| `session_context_enabled` | `true` | SessionStart injection of the tree document |
| `session_context_max_chars` | 2000 | injection document cap |

**Workspace state / temporal / search**

| Knob | Default | Controls |
|---|---|---|
| `session_state_project_file` | `false` | feature L: write `AGENTS.local.md` |
| `state_promote_dedup` | `true` | embedding-based dedup on state-row promotion |
| `state_promote_merge_threshold` | 0.80 | cosine threshold (decays to 0.90 for ref categories) |
| `temporal_rollups_enabled` | `true` | lazy day→week→month rollups (`memor temporal`) |
| `verify_enabled` | `true` | A1 state polling; `switchover` flips it off |
| `embed_provider` | `local` | `local` / `api` / `off` (feature I) |
| `embed_model` | `BAAI/bge-large-en-v1.5` | the local model |
| `embed_device` | `"1"` | GPU index or `"cpu"` |
| `embed_api_base` / `embed_api_key` | `""` | for `api` provider |
| `embed_batch` | 32 | embedding batch size |

---

## 7. Sharing this repo

**What you (the sender) do:**

1. **Decide branch state.** Share a branch whose tip *is* the current state.
   (On the author's box: `session-memory` was merged into `main` before
   sharing — `main` should never be a stale ancestor of the work.)
2. **Pick a vehicle:**
   - **Tarball (no remote needed):**
     ```bash
     git archive --format=tar.gz --prefix=memor/ main -o memor-share.tar.gz
     ```
     `git archive` includes *tracked files only* — untracked local artifacts
     (`chatgpt-review.md`, `summary-review/L*.json`,
     `window-digest-samples/proton/`), `.venv`, `__pycache__`, `.pytest_cache`
     are all excluded automatically. Verify with `tar -tzf memor-share.tar.gz | head`.
   - **Git remote (long-term / collaborative):** `git remote add origin …`
     (private GitHub/GitLab, or a bare repo), `git push -u origin main`.
     CI (`.github/workflows/test.yml`) runs on push.
3. **Know what travels** (the shared copy contains):
   - `integrations/dsh/hooks.json` — **machine-specific**: the author's
     absolute venv/repo paths. The recipient must rewrite the `memor` binary
     path (and the `configPath` in their DSH preset) to their own.
   - `summary-review/context-sample.md` — a *real* rendered session-injection
     document from the author's machine: useful as a concrete sample of what
     injection looks like, but it contains the author's paths and session ids
     (example data, not a spec).
   - `SESSION-MEMORY.md`, `SESSION-MEMORY-GUIDE.md`, `SUMMARY.md` — design
     docs. The author keeps them `600` on disk; git only tracks the exec bit,
     so they arrive as `644` in any shared copy. Their *content* is design,
     not secrets — but set their permissions to taste after unpacking.
4. **Know what never travels** (by design, these live outside the repo):
   the store (`~/.local/share/memor/`), `memor.json`, the venv, any API keys,
   the DSH presets. A fresh recipient starts with an empty memory — that's
   correct: memor is per-machine, per-environment.

**If you want to share the *memory contents* too** (not just the code):
`memor export` produces a portable bundle of the whole store; the recipient
`memor import`s it (optionally re-attributing the machine). That's the
supported path for moving memories between boxes.

**What the recipient does:** read `SETUP.md` §1–§3 to understand it, then
walk §5 top to bottom, enabling only the features they want (each row is an
independent control), then run §8 to verify.

---

## 8. Verifying the setup

```bash
memor doctor                 # store, daemon, each capture source, LLM, hooks — one screen
uv run pytest                # 244 hermetic tests, ~1 min, offline
```

**Smoke test** (proves the full loop without touching your real store):

```bash
export MEMOR_DATA_DIR=/tmp/memor-smoke
memor init
memor add-project smoke --path /tmp/smoke-project
mkdir -p /tmp/smoke-project && cd /tmp/smoke-project
memor remember "smoke baseline" --project smoke --kind state --confidence confirmed
memor consolidate
memor context smoke          # → the fact, with provenance
memor search smoke
unset MEMOR_DATA_DIR
```

Expected per enabled feature (what "it's working" looks like):

- **Shell hook** → `memor events --source terminal` grows as you type commands; `MEMOR_DISABLED=1` stops it.
- **Git hooks** → a commit in a hooked repo appears as a `git` event with SHA/branch/diffstat.
- **Daemon** → `memor capture status` shows active; `memor recent` shows the
  watcher events; `capture.log` (daemonize mode) or the journal (systemd) is quiet.
- **LLM** → `memor llm status` shows provider + endpoint + queue depth;
  `memor llm sync` drains (or reports empty).
- **MCP** → your client lists the 25 `memory_*` tools; `memory_get_project_context("smoke")` returns the fact.
- **DSH integration** → new sessions journal `session/*` rows (`memor sessions`
  shows the session as current); after ~6 tool steps an L1 summary appears
  (`memor summaries --tree --workspace <cwd>`); the next session starts with
  the tree document injected.
- **Switchover** → `memor doctor` shows consolidation interval 0 / autosync
  off / verify off; the pending LLM queue depth is 0 (lapsed).

---

*Describes the `main` branch as of the session-memory merge (2026-09: 46 CLI
commands, 25 MCP tools, 244 tests). If a knob or tool here doesn't match
`memor --help` / the MCP tool list, this doc is stale — regenerate from the
code, the way `AGENT-GUIDE.md` keeps itself current.*
