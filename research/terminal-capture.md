# Terminal / Shell Capture Layer — Research

Scope: how to reliably OBSERVE and CAPTURE terminal activity (commands, cwd, exit
code, duration, and enough context to infer "why") across many tools — Claude
Code, other agent CLIs, plain shells, SSH sessions — harness-agnostically, as raw
evidence for a downstream memory system. Not covering the memory/storage/reasoning
layer itself.

## Overview

There is no single existing tool that both (a) captures structured, queryable
per-command metadata across every shell/tool a developer might use, and (b)
captures full command output as evidence, in a harness-agnostic way. The
realistic answer is a **layered combination**:

1. **Atuin** — structured per-command metadata (command, cwd, exit code,
   duration, host, session), stored in a local SQLite DB, harness-agnostic
   (works in bash/zsh/fish/nu, independent of Claude Code or any other CLI),
   fully usable offline/self-hosted.
2. **Shell hooks (`preexec`/`precmd`, or Atuin's own hook points)** — cheap
   glue to push structured events to an external event store in near-real-time
   instead of (or in addition to) waiting to read Atuin's SQLite file.
3. **Harness-native hooks (e.g. Claude Code's `PreToolUse`/`PostToolUse`)** —
   the *only* way to reliably capture full command **output** (stdout) plus
   agent-level context ("why": the prompt/session that caused the command),
   but this is harness-specific and only exists where the tool vendor built it.
4. **Full-session recorders (`script`, `asciinema`, `ttyrec`, tmux
   `pipe-pane`)** — the only way to get a complete byte-for-byte transcript
   including all output, for cases that need full evidentiary fidelity, at the
   cost of being an unstructured blob that needs re-parsing.

No tool here solves "why did this change happen" — that requires correlating
command evidence with an agent's reasoning/prompt trace, which lives at the
harness layer (e.g., Claude Code's own transcript/session JSON), not the shell
layer.

## Atuin — findings

Atuin (atuin.sh, github.com/atuinsh/atuin) is a shell-history replacement
("magical shell history") for bash/zsh/fish/nushell/xonsh/PowerShell.

**What it captures per command** (confirmed via `history` table schema):
`id`, `timestamp`, `duration`, `exit` (exit code), `command`, `cwd`,
`session` (session id), `hostname`, `deleted_at`, `author`, `intent`, `shell`.
In practice: command text, working directory, exit code, duration, hostname,
session id, and shell type — i.e. exactly the metadata fields the design brief
asked about, natively, out of the box.

**What it does NOT capture — important for "evidence":**
- **No command output.** Atuin does not record stdout/stderr. This is
  confirmed by an open GitHub feature request (atuinsh/atuin issue #2179,
  "Store command's stdout/stderr") that has not been implemented — a
  deliberate design choice, partly for privacy/security (avoiding accidental
  capture of secrets printed to terminal). If output is needed as evidence,
  Atuin alone is insufficient.
- **No environment variables** captured by default (aside from what you
  explicitly template via `--format`). There is a secret-scrubbing feature
  (Atuin refuses to record commands that look like they contain credentials —
  AWS keys, GitHub/npm tokens, Slack webhooks, Stripe keys, etc. — enabled by
  default) and an explicit "excluding commands" mechanism
  (docs.atuin.sh/guide/excluding-commands) to blocklist directories/commands
  from being recorded at all.
- No built-in causal/"why" field beyond an optional free-text `intent` column
  most integrations don't populate.

**Storage format / self-hosting / offline use:**
- Local history lives in a **SQLite database** at
  `~/.local/share/atuin/history.db` (XDG-overridable), directly readable by
  any external program (no proprietary format) — this is exactly what a
  memory-processing pipeline would want: point a script at the sqlite file and
  query it with normal SQL.
- Sync is **fully optional**. Atuin's own docs state: "You may use either the
  server I host, or host your own! Or just don't use sync at all." Local
  recording/search works with zero network dependency.
- Self-hosting the sync server is supported via a docker/binary
  (`atuin-server`) backed by Postgres or SQLite (`ATUIN_DB_URI=sqlite:///...`);
  even self-hosted sync end-to-end encrypts history so the server never sees
  plaintext. For a fully offline single-machine capture layer, sync isn't
  needed at all — the local SQLite DB is sufficient and is the only piece that
  matters for this brief.

**Querying / exporting (non-interactive):**
- `atuin search` supports non-interactive, scriptable filtering:
  `--cwd`, `--exclude-cwd`, `--exit`, `--exclude-exit`, `--before`, `--after`,
  `--limit`, `--offset`, `--reverse`, and a `--format`/`-f` template with
  variables `{command} {directory} {duration} {user} {host} {time} {exit}
  {relativetime}` for structured, pipeable text output.
- No documented `--print0`/NUL-delimited flag; safe machine-parsing is best
  done either via a custom `--format` with an unlikely delimiter, or — more
  robustly — by **reading `history.db` directly with SQL**, which sidesteps
  CLI text-formatting entirely and is the recommended integration path for a
  memory pipeline.
- `atuin history list --format json` is referenced in community docs for JSON
  export, though the canonical/most robust integration remains direct SQLite
  access to `history.db` since the schema is stable and documented.

Sources:
- https://atuin.sh/
- https://docs.atuin.sh/ (redirects to /latest/)
- https://github.com/atuinsh/atuin
- https://github.com/atuinsh/atuin/issues/2179 (no stdout/stderr capture — open feature request)
- https://docs.atuin.sh/18.19/guide/excluding-commands/
- https://docs.atuin.sh/18.19/guide/shell-integration/
- https://docs.atuin.sh/18.18/reference/search/
- https://docs.atuin.sh/cli/self-hosting/server-setup/ and https://docs.atuin.sh/18.19/self-hosting/server-setup/
- https://www.sumarsono.com/blog/how-to-self-host-atuin-server/
- https://til.simonwillison.net/macos/atuin

## Alternatives / complements considered

### `preexec`/`precmd` shell hooks (zsh/bash-preexec)
- zsh natively supports `preexec` (fires before a command runs) and `precmd`
  (fires right before the next prompt is drawn, i.e. right after the previous
  command finished). Bash has an equivalent via the `bash-preexec` project
  (github.com/systemmonkey42/bash-preexec, and the more common
  rcaloras/bash-preexec).
- These are exactly the hooks Atuin itself is built on internally — Atuin's
  shell integration is essentially "preexec/precmd wired to write into
  SQLite." Using them directly (bypassing Atuin) is a small amount of glue:
  a `precmd`/`preexec` function that captures `$PWD`, `$?` (exit code),
  `$(date)`, and a session/host id, then curls/appends it to an external
  event store. This is genuinely simple (~20-40 lines of shell) and is the
  natural mechanism to get **real-time push** of structured events instead of
  Atuin's "recorded to local sqlite, read later" model.
- Caveats: shell-specific (zsh vs bash have different hook semantics),
  doesn't cover non-interactive/scripted shells or other agent CLIs unless
  they also source the same rc file, and pushing synchronously to a remote
  store from `precmd` can add latency to every prompt unless done
  asynchronously (backgrounded curl / local socket / append-only local queue
  drained by a daemon).
- Verdict: good real-time-forwarding complement to Atuin, not a replacement —
  Atuin already solves the "reliable structured local capture" problem; hooks
  add "push it out live" on top.

### `script(1)`
- POSIX-standard, ships on every Unix system, zero setup, records a full
  terminal transcript (typescript) including all output, optionally with
  `--timing` for a separate timing file that can reconstruct playback speed.
- Output is a raw byte stream with terminal control codes (ANSI escapes)
  intermixed — not structured, hard to parse programmatically for "what
  command produced what output" without additional heuristics (prompt
  detection). Good for archival/legal-style evidence, bad for direct
  structured querying.

### `ttyrec` / `ttyplay`
- Same idea as `script` but binary format with separate frame timestamps
  (like a proprietary predecessor to asciicast). Was popular in the 2000s;
  mostly superseded by asciinema today. Same structured-parsing difficulty as
  `script`.

### asciinema
- Modern, actively maintained, records to **asciicast v2**: a newline-
  delimited JSON format — a JSON header (terminal size, timestamp, env,
  command/title) followed by one JSON array per output event
  `[time, type, data]`. This is much easier to parse programmatically than
  raw `script` output (it's line-delimited JSON, not raw terminal bytes), and
  has a mature player/ecosystem plus self-hosted server option (no forced
  cloud upload).
- Still captures raw terminal bytes as the `data` payload (including ANSI
  escapes for interactive TUIs), so "what exact command ran" and "what exact
  output resulted" still require replay/ANSI-stripping to extract cleanly —
  it's a transcript, not a structured event log.
- Storage: proportional to terminal chattiness — verbose builds, log tailing,
  or TUI redraws (progress bars, htop-like tools) can bloat file size
  significantly compared to Atuin's few-hundred-bytes-per-command SQLite rows.
- Verdict: best complement for "capture everything" on specific
  high-stakes sessions (e.g., when there's ambiguity/incident investigation
  need), not viable as the default always-on capture mechanism because of
  volume and structured-parsing cost.

### tmux / screen pane logging (`pipe-pane`)
- `tmux pipe-pane [-o] 'cat >> file'` streams a pane's I/O to an external
  command/file live; combined with `ts` (moreutils) can timestamp each line.
  Plugins like `tmux-logging` and `tmux-logger` wrap this with
  auto-start-on-session and ANSI-stripping.
- Same fundamental tradeoff as `script`/asciinema: full transcript, weak
  structure, and only works for sessions actually run inside tmux/screen —
  doesn't cover bare SSH shells or non-multiplexed terminals.

### Harness-native hooks — Claude Code as the example
- Claude Code exposes a first-class hook system (`~/.claude/settings.json` or
  project `.claude/settings.json`) with lifecycle events including
  `PreToolUse`, `PostToolUse`, `SessionStart`, `SessionEnd`, `Stop`,
  `UserPromptSubmit`, etc. Hooks can be shell commands (JSON on stdin) or HTTP
  callbacks to an external service.
- For `Bash` tool calls specifically: `PreToolUse` gives `tool_input.command`,
  `cwd`, `session_id` before execution; `PostToolUse` additionally gives the
  tool result/output and exit code after execution. This means Claude Code's
  own hooks are the **only mechanism in this survey that reliably delivers
  command output alongside structured metadata**, plus the crucial extra
  context this brief wants — correlation to `session_id`/`prompt_id`, i.e. a
  link back to the user prompt or agent reasoning that caused the command
  (the "why").
- The catch: this is Claude-Code-specific configuration. Other agent CLIs
  (Codex CLI, Aider, etc.) each have their own hook surface (if any) with
  different schemas, so a harness-agnostic pipeline can't rely on this as the
  primary mechanism — it's a valuable *bonus* signal to fuse in for the
  specific harnesses that expose it, layered on top of the shell-level
  capture that works regardless of harness.

Sources:
- https://code.claude.com/docs/en/hooks (redirected from docs.claude.com/en/docs/claude-code/hooks)
- https://github.com/rcaloras/bash-preexec / https://github.com/systemmonkey42/bash-preexec
- https://github.com/rothgar/mastering-zsh/blob/master/docs/config/hooks.md
- https://github.com/asciinema/asciinema
- https://github.com/asciinema/asciinema/blob/v2.0.0/doc/asciicast-v2.md
- https://docs.asciinema.org/manual/asciicast/v2/
- https://suvadu.sh/blog/suvadu-vs-atuin-mcfly-hstr/
- https://github.com/tmux-plugins/tmux-logging
- https://github.com/dptsec/tmux-logger

## Recommended minimal-effort combination for this brief's capture layer

**Baseline (always on, harness-agnostic): Atuin.**
Install Atuin's shell integration in every interactive shell config
(`.zshrc`/`.bashrc`) on every machine, run with sync disabled or self-hosted
(no dependency on atuin.sh cloud). Point the memory pipeline's ingestion job
directly at `~/.local/share/atuin/history.db` (SQLite) and poll/tail it (or
watch the file for changes) to pull new rows: command, cwd, exit code,
duration, host, session, timestamp. This alone gives ~90% of the structured
"what happened" evidence with near-zero ongoing engineering effort since
Atuin already does the reliable capture, dedup, and storage.

**Add real-time push (optional, low effort): a thin `precmd` hook.**
If the pipeline wants events pushed live instead of polled, add a small
`precmd` (zsh) / `bash-preexec` (bash) function alongside Atuin that packages
the same fields (`$PWD`, `$?`, `$(date +%s)`, `$$`/session id, hostname) as
JSON and appends to a local append-only log or POSTs to a local ingestion
endpoint, backgrounded so it never blocks the prompt. This is genuinely
~30-50 lines of shell glue, well-understood, and is complementary to (not a
replacement for) Atuin — it just changes push-vs-pull timing.

**Add harness-native hooks where available: Claude Code `PostToolUse`.**
For Claude Code specifically, wire a `PostToolUse` hook (matcher `Bash`) that
posts `{command, cwd, exit_code, output, session_id, prompt_id}` to the same
event store. This is the only source in this stack for command **output**
and for a direct link to "why" (the driving prompt/session). Repeat the
equivalent for any other agent CLI that exposes similar hooks (check each
tool's docs individually — there's no cross-harness standard as of today).

**Add full-session recording selectively, not by default: asciinema.**
Don't record every session at asciicast fidelity (storage/parsing cost too
high for "ambient" capture). Instead, make it opt-in/triggerable — e.g. a
wrapper command (`ai-record <cmd>` or triggered automatically only around
higher-risk operations like `terraform apply`, prod deploys, migrations) that
wraps the shell in `asciinema rec` for full byte-level evidence when the
downstream memory system flags something as needing deep reconstruction.
`script` is an acceptable zero-dependency fallback on boxes where asciinema
isn't installed (e.g. random SSH hop to an unmanaged server), accepting its
worse parseability.

This gives three tiers of evidence: cheap structured metadata always-on
(Atuin), a little richer real-time+output context where the harness supports
it (shell hooks / Claude Code hooks), and full-fidelity transcripts on demand
(asciinema/script) — rather than trying to make one tool do everything.

## Effort estimate

- Atuin rollout (install + shell integration + disable/self-host sync +
  point pipeline at `history.db`): **hours**, not days. This is the highest
  leverage step and should be step one.
- `precmd`/`preexec` real-time-forwarding glue: **a few hours** to write,
  test across bash and zsh, and make async/non-blocking; ongoing maintenance
  is low (stable shell APIs, ~decade-old mechanism).
- Claude Code `PostToolUse`/`PreToolUse` hook wiring: **a few hours** per
  harness (settings.json + a small script); needs to be redone per agent CLI
  since there's no shared schema — budget similarly for each additional
  harness (Codex CLI, etc.) if/when their hook surfaces are investigated.
- asciinema opt-in wrapper for high-risk commands: **a few hours** for the
  wrapper/trigger logic; storage/retention policy for the recordings is a
  separate (memory-system-side) decision, not a capture-layer cost.
- Total for a working v1 capture layer covering the brief's requirements:
  **roughly 1-2 focused days**, dominated by integration/testing across
  multiple shells and machines rather than any single tool being hard to use.

## Gaps — what no tool here solves

- **Cross-harness "why" correlation.** Structured shell metadata (Atuin) and
  harness-native hooks (Claude Code) are captured through different, unrelated
  mechanisms with no shared identifier out of the box; the memory pipeline
  itself will need to invent a correlation key (e.g. a shared session id
  exported as an env var before either system starts recording) to stitch
  "this command" to "this agent turn/prompt" for harnesses without native
  hook support (plain shells, SSH sessions, other CLIs without a hook API).
- **No universal output capture.** Atuin deliberately doesn't store
  stdout/stderr; only harness-native hooks (where available) or full-session
  recorders provide output, and neither is harness-agnostic *and*
  structured at the same time — you always trade one for the other.
- **Non-interactive / scripted / cron shells.** preexec/precmd and Atuin's
  shell integration assume an interactive shell; commands run from scripts,
  CI, or other agent CLIs that don't source the user's rc files won't be
  captured by this stack at all unless those tools are separately
  instrumented.
- **Other agent CLIs' hook surfaces are unverified.** This research
  confirmed Claude Code's hook system in detail; it did not verify whether
  Codex CLI, Aider, Cursor CLI, etc. expose equivalent PreToolUse/PostToolUse
  style hooks — that needs a tool-by-tool follow-up before assuming the
  "harness-native output capture" tier generalizes beyond Claude Code.
- **Secret redaction is a policy tradeoff, not a solved problem.** Atuin's
  built-in secret-detection heuristics reduce but don't eliminate the risk of
  sensitive data landing in the evidence store (command-line args can contain
  many other kinds of sensitive data), and full-session recorders (asciinema/
  script) don't do any redaction at all — this needs to be handled by the
  memory pipeline's ingestion step, not the capture layer.
