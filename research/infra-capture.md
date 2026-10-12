# Capture Layer Research: Git / Docker / systemd / Filesystem / Process-Network

Research date: 2026-08-23. Scope: self-hostable, low-effort (weekend-wireable) tools that produce
**raw evidence** of what happened to a developer's technical environment, for a downstream
memory-processing step to turn into derived facts (e.g. "port changed 8000->8080 because of a
conflict"). Not evaluating anything that requires building a custom kernel module, a SaaS account,
or a multi-day integration.

---

## Overview

The five observation sources map to five very different capture mechanisms, and they differ a lot
in "does this already emit structured events I can just tail" vs. "I need to write the glue myself":

| Source | Structured-events out of the box? | Effort to wire up | Maturity |
|---|---|---|---|
| Git | No (need hooks), but trivial to make structured | Very low (hours) | N/A (hooks are core git) |
| Docker | Yes (`docker events --format '{{json .}}'`) | Very low (hours) | Very mature |
| systemd/journald | Partially (journal is structured; unit-file edits are not tracked) | Low-medium | Very mature, with a real gap |
| Filesystem | Yes (watchman) / semi (fswatch) | Low (hours) | Very mature |
| Process/network | No (need polling + diffing) | Low-medium | Mature but you're building the "events" yourself |

The overall shape of a realistic weekend build: a handful of long-running lightweight daemons/hooks
each emitting JSON lines to a local file or a tiny local HTTP/queue endpoint, with no eBPF, no
central agent framework, and no dependency on anything that isn't already installed on a typical
Linux dev box (or a single `apt`/`brew`/`go install` away).

---

## Git capture options

**Git hooks (`post-commit`, `post-merge`, `post-checkout`, `post-rewrite`, `reference-transaction`)**
- What they capture: exactly the events you listed — a commit happened (with SHA, message, diffstat,
  branch), a merge/pull happened, a checkout/branch-switch happened, history was rewritten (rebase/
  amend), and (via `reference-transaction`, a less common hook) any ref update including branch
  creation/deletion.
- Effort: trivial. A hook is just an executable script dropped in `.git/hooks/<name>` (or centrally
  distributed via `core.hooksPath` / `git config init.templateDir` so every repo gets it automatically).
  A ~20-line shell or Python script can shell out to `git log -1 --format=...`, `git diff --stat`,
  etc., build a JSON object, and append it to a local file or POST it to a local capture daemon.
- Structured output: entirely up to you, and that's the point — you control the schema from the
  start (commit SHA, author, timestamp, branch, files changed, insertions/deletions, parent SHAs).
  This is the cleanest, lowest-risk piece of the whole capture layer.
- Gaps: hooks are per-repo unless you configure a global `core.hooksPath`; they don't fire for
  changes made via GitHub/GitLab web UI or other clones unless you also capture server-side (not
  relevant for a solo local-dev memory system). `reference-transaction` is the only hook that sees
  branch create/delete and is the least commonly documented — worth testing before relying on it.
- Source: https://git-scm.com/book/en/v2/Customizing-Git-Git-Hooks

**"Entire CLI" — researched per your request, verdict: real product, wrong layer for this use case**
- It is **real, not vaporware/blogspam** (the blogspam is techedubyte.com's SEO content farm, which
  has published ~9 near-duplicate AI-generated articles about it, but the underlying tool is
  legitimate). GitHub repo: https://github.com/entireio/cli, site: https://entire.io/, and it has an
  independent Thoughtworks Technology Radar entry (rated **"Assess"**, described as "very new," with
  "the ecosystem for agent session traceability still forming"):
  https://www.thoughtworks.com/radar/tools/entire-cli
- Founder: Nat Friedman, ex-CEO of GitHub, per multiple secondary sources reporting a ~$60M seed
  round for "Entire" as "the world's next developer platform" (I could not independently verify the
  funding figure beyond aggregator/blog coverage, so treat that number as unconfirmed, but the
  founder identity and product existence are corroborated by the Thoughtworks Radar and the
  `entireio/cli` GitHub org).
- **What it actually captures: AI agent reasoning/session data, not general git-repo-state
  observability.** Per its own repo description: "Entire CLI hooks into your Git workflow to capture
  AI agent sessions as you work. Sessions are indexed alongside commits, creating a searchable
  record of how code was written in your repo." Concretely it captures, per "checkpoint" (created at
  commit time): the agent's full transcript, your prompts, token usage, tool calls, files touched,
  and line-level human-vs-agent attribution. This is all stored on a **separate orphan branch**
  (`entire/checkpoints/v1`) so it never pollutes your real commit history. It's agent-agnostic
  (Claude Code, Cursor, Gemini CLI, Copilot CLI, etc.).
- **Verdict for this design brief: not a fit.** It answers "what did the AI agent do/think while
  writing this commit," which is a different (and narrower) question than "what happened to the git
  repo state itself" (commits, branches, merges, rewrites) that a general environment-memory system
  needs. It's also file/branch-based rather than emitting a live event stream, and it only activates
  when an *agent* session is bracketing a commit — it captures nothing for a plain human `git commit`
  or `git checkout`. Don't build on it; plain hooks give you everything actually needed here, with
  far less surface area and no product-dependency risk on an early-stage funded startup's CLI. If the
  broader project *also* wants to remember AI-pairing sessions specifically (a legitimately different
  and interesting capability), Entire is worth a second look then, but it's orthogonal to the
  "reconstruct git repo state changes" requirement.
- Sources: https://github.com/entireio/cli, https://entire.io/, https://entire.io/blog/the-entire-cli-how-it-works-and-its-headed,
  https://www.thoughtworks.com/radar/tools/entire-cli, https://dev.to/stevengonsalvez/entire-cli-git-blame-for-the-ai-era-52gj

---

## Docker capture options

**`docker system events` / Docker Events API**
- What it captures: real-time stream of container lifecycle events — `create`, `start`, `stop`,
  `die`, `kill`, `pause`, `unpause`, `restart`, `destroy`, `rename`, `oom`, `exec_create/start`, plus
  image, network, volume, and daemon-level events. Each event carries an actor (container/image ID),
  attributes (e.g. exit code, image name), and a timestamp.
- Effort: trivially low — it's already running on any Docker install. `docker events --format
  '{{json .}}'` streams newline-delimited JSON directly to stdout; the same is available
  programmatically via `GET /events` on the Docker Engine API (also `decode=True` in the Python SDK
  auto-parses JSON). A one-line `docker events --format '{{json .}}' >> events.jsonl &` (or a tiny
  systemd unit around it) is enough to make this durable evidence.
- Structured output: yes, first-class JSON lines — the best-behaved source in this whole list.
- Maturity: extremely mature, part of the Docker Engine core API since early versions.
- Gap: this captures *lifecycle* transitions, not *config diffs* — e.g. it tells you a container was
  recreated but not what changed about its config between the old and new instantiation. That's what
  Docker Drift complements (below).
- Sources: https://docs.docker.com/reference/cli/docker/system/events/, https://docs.docker.com/engine/api/v1.43/#tag/System/operation/SystemEvents

**Docker Drift (`hub.docker.com/r/zachbg/docker-drift`)**
- What it captures: configuration drift of *running* containers against an automatically-recorded
  baseline — diffs in image tag/digest, env vars, volumes, port mappings, networks, labels, security
  settings (privileged mode, capabilities, read-only rootfs), and resource limits. This is exactly
  the kind of evidence that supports a derived fact like "port changed 8000->8080."
  https://hub.docker.com/r/zachbg/docker-drift
- Effort: low — it's a single small (41.2 MB) Docker image you run via Compose, mounted against
  `/var/run/docker.sock`, with baselines persisted to a volume. Config is via env vars:
  `CHECK_INTERVAL` (default 300s poll), `AUTO_BASELINE=true` (baseline is captured automatically on
  first sight of a container — no manual baseline step), `BASELINE_DIR`, `OUTPUT_FORMAT` (`text` or
  `json`), and `WEBHOOK_URL` for push-style alerting.
- Structured output: yes — set `OUTPUT_FORMAT=json` and you get machine-parseable drift records; it
  also supports webhook delivery for a push model instead of tailing logs.
- Maturity: **small/early, single-maintainer project** — last image update was ~5 months prior to
  this research (roughly Q1 2026) with only ~1.4K Docker Hub pulls. No GitHub repo link was
  discoverable from the Docker Hub page itself (only the Hub username `zachbg`), so there's no
  visible issue tracker/commit history to independently vet its robustness or how actively it's
  maintained going forward. Treat it as a nice-to-have accelerant rather than a load-bearing
  dependency: given how narrow its job is (diffing docker-inspect-shaped JSON against a stored
  baseline), it would also be one of the easier pieces of this whole system to reimplement yourself
  in an evening if it stalls or disappears (poll `docker inspect`, diff against last-seen JSON, done).
- Source: https://hub.docker.com/r/zachbg/docker-drift

---

## systemd capture options

**journald / journalctl for service lifecycle**
- What it captures: structured logs for every unit's stdout/stderr (systemd connects service output
  to the journal by default) plus systemd's own lifecycle messages (unit started/stopped/failed/
  restarted, with fields like `_SYSTEMD_UNIT`, `_PID`, `MESSAGE_ID`, `PRIORITY`). This is enough to
  reconstruct "service X was restarted at time T, N times, exit code Y."
- Effort: zero setup on any systemd-based distro (Ubuntu, Fedora, Debian, Arch, etc.) — it's already
  running. Structured extraction: `journalctl -o json` / `-o json-pretty` (or `--output=cat` for raw)
  emits one JSON object per log entry with full metadata; `journalctl -u <unit> -f -o json` gives you
  a live structured tail per service you care about, easy to pipe into a capture daemon.
- Structured output: yes, JSON export is a first-class `journalctl` output mode.
- Maturity: extremely mature, ships with systemd itself.
- Sources: https://www.freedesktop.org/software/systemd/man/latest/systemd-journald.service.html, https://www.dash0.com/guides/systemd-logs-linux-journalctl

**Gap confirmed: unit *file* changes are not tracked by systemd/journald by default.** journald
records what services *do* (start/stop/log output) but has no built-in mechanism that notices when
someone edits `/etc/systemd/system/foo.service` or drops an override in
`/etc/systemd/system/foo.service.d/`. Web/manpage research turned up nothing in journald itself for
this; the closest built-in primitives are `systemctl daemon-reload` (which reloads unit definitions
but emits no diff of what changed) and generic `auditd` watch rules (`-w /etc/systemd/system -p wa`)
which log *that* a file under that path was written but not a semantic diff of the unit's fields.
Practical implication for the capture layer: **unit file changes need their own capture path**,
realistically the same filesystem-watcher mechanism used for other config files (Watchman/fswatch
watching `/etc/systemd/system/` and `~/.config/systemd/user/`) rather than anything systemd-native.
This is a real, worth-flagging gap in the design brief's assumptions.
- Sources: https://linux-audit.com/systemd/faq/how-to-reload-the-systemd-configuration/, https://docs.redhat.com/en/documentation/red_hat_enterprise_linux/9/html/using_systemd_unit_files_to_customize_and_optimize_your_system/assembly_working-with-systemd-unit-files_working-with-systemd

---

## Filesystem capture options

**Watchman (Meta/Facebook)**
- What it captures: recursive file/directory create/modify/delete/rename events via a persistent
  watch, using inotify on Linux (FSEvents on macOS) under the hood, but abstracted behind a
  client-server daemon so you don't manage raw inotify watch descriptors yourself.
- Effort: low — install the daemon, define a watch + a JSON-configured "trigger" (a command run when
  matching files change) or subscribe over its socket API for a live event stream. Config and
  queries are JSON in/JSON out natively.
- Structured output: yes, first-class JSON — this is one of its main selling points over raw
  inotify/fswatch.
- Maturity: very mature, widely used (originally built for watching huge source trees at Facebook,
  also powers various JS toolchains like Jest/Metro).
- Source: https://www.tecmint.com/watchman-monitor-file-changes-in-linux/

**fswatch**
- What it captures: same category of filesystem events, using native OS backends (inotify/fanotify
  on Linux, FSEvents on macOS, kqueue on BSD), simpler and more portable than Watchman but no
  daemon/server model — it's a straightforward CLI you script around.
- Effort: very low — single binary, `fswatch -x /path | while read line; do ...; done` style usage;
  output is plain text (path + optional event flags) rather than JSON, so you'd wrap it yourself to
  produce structured events (trivial, a few lines of awk/python).
- Structured output: not natively JSON like Watchman, but flag/path output is regular enough to
  parse reliably.
- Maturity: very mature, cross-platform, actively maintained (`emcrisostomo/fswatch` on GitHub).
- Both tools inherit the same underlying Linux inotify limitations worth noting for the design brief:
  inotify doesn't identify *which process/user* triggered a change, doesn't see remote/network-FS
  events, can't watch pseudo-filesystems (`/proc`, `/sys`), and is subject to a system-wide watch
  count limit (`/proc/sys/fs/inotify/max_user_watches`) that matters if you point it at large
  directory trees.
- Sources: https://github.com/emcrisostomo/fswatch, https://anarc.at/blog/2019-11-20-file-monitoring-tools/

**Recommendation between the two:** Watchman if you want JSON events and a subscribe model with
minimal glue; fswatch if you want the absolute simplest single static binary and don't mind writing
a thin JSON-wrapping shim. For a solo weekend build watching a handful of config directories
(`~/.config`, `/etc`, `/etc/systemd/system`, project `.env`/dotfiles), either is comfortably enough —
this is the least risky/most solved part of the whole capture layer.

---

## Process/network capture options

**Lightweight polling: `ss`, `netstat`, `/proc`**
- What it captures: point-in-time snapshots of listening sockets/ports, established connections,
  process-to-port mapping (`ss -tulpn`, or walking `/proc/net/tcp[6]` + `/proc/<pid>/fd` to resolve
  socket inodes to owning processes), and process table state (`/proc/<pid>/{cmdline,status,stat}`).
- Effort: very low — no new software required (`ss`/`iproute2` and `/proc` are already present on
  any Linux box). The work is entirely in your own polling loop + diffing logic: snapshot state every
  N seconds (or on a trigger), diff against the previous snapshot, and emit a structured event only
  when something changed (port opened/closed, new process, PID reused on a known port, etc.).
- Structured output: none natively (`ss`/`netstat` are human-readable tables), but `ss -H` (no
  header) or parsing `/proc` directly gives clean enough fields to turn into JSON yourself in a small
  script; this is genuinely a same-day task.
- Maturity: the underlying tools are core Linux utilities, extremely mature and stable; the
  "product" here is just your own diffing script, which is exactly the right amount of ownership for
  something this simple.

**Heavier eBPF-based tools (Falco, Tetragon, etc.)**
- What they capture: kernel-level, per-syscall visibility into process execution, network
  connections, file access, and privilege changes, with rule-based anomaly/policy detection layered
  on top (Falco's rules engine, Tetragon's policy hooks). They give you *causal, real-time* attribution
  (which process/binary/container made which syscall) rather than point-in-time snapshots.
- Effort: meaningfully higher — Falco in particular is designed and documented around production
  Kubernetes/cloud-native security monitoring: it wants a rules configuration, typically runs as a
  privileged daemon/container with kernel driver or eBPF probe loading, and is built to integrate
  with SOC/alerting pipelines, not personal dev-box journaling. Getting it running standalone on a
  laptop for "did the process on port 8080 change" is real setup and a nontrivial daemon footprint
  for what you get.
- **Verdict: overkill for this use case, and this is a deliberate design call rather than a
  maturity/quality judgment.** Falco's own positioning and every source found treats it as a
  runtime *intrusion-detection* tool for production/Kubernetes environments — anomalous behavior
  detection against security policies — not as a general "what changed" event log for a personal
  dev-environment memory system. eBPF's actual advantage (kernel-verified, tamper-resistant,
  zero-syscall-overhead visibility, causal attribution of *which* process did *what* at the syscall
  level) matters for adversarial/production threat detection, where you can't trust userspace
  polling to not be evaded or to keep up with high event volume. None of that threat model applies
  to a solo developer's own machine capturing its own git/Docker/systemd/filesystem/process activity
  for a personal memory system — polling `ss`/`/proc` every few seconds and diffing is both
  sufficient and far cheaper to run and maintain, and it doesn't require a privileged
  kernel-probe-loading daemon babysitting your laptop just to notice a port moved from 8000 to 8080.
  Reach for eBPF only if the design brief later needs sub-second causal attribution ("which exact
  process/thread bound this port, in what call chain") — the stated goal ("port changed 8000->8080
  because of a conflict") doesn't need that resolution; a snapshot-diff every few seconds correlated
  with the Docker/systemd event streams already gives you "what" and, combined with timing, a good
  proxy for "because of what."
- Sources: https://falco.org/blog/tracing-syscalls-using-ebpf-part-1/, https://ebpf.io/applications/, https://morgannriu.fr/en/blog/ebpf-observabilite-linux-falco-cilium-2026

---

## Recommended minimal-effort combination

For a solo developer, weekend-scale build, all emitting JSON lines to either local files or a small
local capture endpoint (e.g. a single SQLite-backed local daemon or just append-only `.jsonl` files
per source that a later ingestion step tails):

1. **Git**: `post-commit` + `post-merge` + `post-checkout` (+ `reference-transaction` if you confirm
   it fires reliably in your git version) hooks, installed globally via `core.hooksPath`, each
   emitting a JSON event with SHA/branch/diffstat/parents.
2. **Docker**: `docker events --format '{{json .}}'` run as a small always-on systemd user service,
   appending to a log file, **plus** Docker Drift as a sidecar container for config-drift detection
   with `OUTPUT_FORMAT=json` and a local `WEBHOOK_URL` pointed at your capture endpoint (or just have
   it log JSON and tail that).
3. **systemd**: `journalctl -o json -f` (optionally scoped to units you care about) tailed into your
   capture pipeline for lifecycle events; **separately**, point a filesystem watcher (see #4) at
   `/etc/systemd/system/` and `~/.config/systemd/user/` to catch unit file edits, since journald
   won't.
4. **Filesystem**: Watchman watching a small explicit list of directories (`/etc`, `~/.config`,
   project `.env`/config files, `/etc/systemd/system`) with a trigger that emits JSON on change.
5. **Process/network**: a simple cron-or-systemd-timer script (every 10-30s, or on a Docker/systemd
   event as a trigger) that runs `ss -tulpn`, diffs against the last snapshot, and emits a structured
   "port X now bound by process Y" / "port X freed" event only on change.

None of this needs eBPF, a message broker, or a hosted service. All five sources land as JSON lines
that a downstream memory-processing step can correlate by timestamp (e.g. a Docker `die` event for a
container at T, followed 2 seconds later by a filesystem change to a `docker-compose.yml` port
mapping, followed by a new `ss` snapshot showing the new port bound — that's the raw evidence trail
behind "port changed 8000->8080 because of a conflict").

---

## Effort estimate

| Piece | Estimated effort |
|---|---|
| Git hooks + JSON emission | 2-4 hours |
| Docker events tailing | 1-2 hours |
| Docker Drift deployment | 1-2 hours (given it's a ready image; add buffer since it's a small/unverified single-maintainer project) |
| journald JSON tailing | 1-2 hours |
| Filesystem watcher (Watchman) for configs + unit files | 2-4 hours |
| Process/network poll-and-diff script | 3-5 hours (this is the one piece you're actually writing logic for, not just wiring) |
| Gluing all sources into one append-only local event store | 2-4 hours |
| **Total** | **roughly 1-2 focused weekend days**, consistent with the brief's "wire up in a weekend, not build from scratch" bar |

---

## Gaps

- **systemd unit file changes are not tracked by any built-in systemd/journald mechanism.** Must be
  covered by a filesystem watcher on unit-file directories, or `auditd` watch rules, layered on top —
  not a native capability. Flagged in detail above.
- **Docker Drift is a small, single-maintainer, unverified-provenance project** (~1.4K pulls, no
  discoverable public GitHub repo from its Docker Hub page, last updated ~5 months before this
  research). It's useful and low-effort to try, but shouldn't be treated as a long-term dependency
  without either finding/confirming its source repo or being ready to replace it with a homegrown
  `docker inspect` diff script (which is a small effort either way).
- **inotify-based watchers (Watchman/fswatch) cannot identify which process/user made a change**, and
  don't see remote/network-filesystem events or pseudo-filesystem (`/proc`,`/sys`) activity. If "who
  changed this config file" matters to the derived facts later, you'll need to correlate filesystem
  events with process-snapshot/shell-history evidence rather than get attribution from the watcher
  itself.
- **Process/network capture via polling is inherently sampled, not event-driven** — a port that opens
  and closes faster than your poll interval will be missed. This is an accepted tradeoff given eBPF is
  judged overkill for this use case; if that tradeoff later proves wrong (e.g. flaky short-lived
  processes matter to the memory system), the honest escalation path is a lightweight eBPF probe
  scoped only to socket bind/close events (not a full Falco deployment).
- **"Entire CLI" does not solve any part of the git-capture requirement as originally scoped** — it's
  a real, VC-funded, actively-developing product, but its captured payload (agent transcripts/tool
  calls/token usage) answers a different question than "what happened to the repo" and depends on an
  external, early-stage, subscription-oriented startup CLI rather than plain git hooks. Its funding
  claim (~$60M seed) could not be independently confirmed beyond aggregator/blog coverage.
- **Docker events capture lifecycle transitions but not the "why"** (e.g. it tells you a container
  was recreated, not that the recreation was because a Compose file's port mapping changed) — the
  "why" has to come from correlating it with the filesystem-watcher event on the compose file/env,
  which is exactly the kind of cross-source correlation the downstream memory-processing step is
  meant to do, not something any single capture source provides on its own.

## Sources (consolidated)

- Git hooks: https://git-scm.com/book/en/v2/Customizing-Git-Git-Hooks
- Entire CLI: https://github.com/entireio/cli, https://entire.io/, https://www.thoughtworks.com/radar/tools/entire-cli, https://dev.to/stevengonsalvez/entire-cli-git-blame-for-the-ai-era-52gj
- Docker events: https://docs.docker.com/reference/cli/docker/system/events/, https://docs.docker.com/engine/api/v1.43/#tag/System/operation/SystemEvents
- Docker Drift: https://hub.docker.com/r/zachbg/docker-drift
- journald/journalctl: https://www.freedesktop.org/software/systemd/man/latest/systemd-journald.service.html, https://www.dash0.com/guides/systemd-logs-linux-journalctl, https://linux-audit.com/systemd/faq/how-to-reload-the-systemd-configuration/
- Watchman: https://www.tecmint.com/watchman-monitor-file-changes-in-linux/
- fswatch: https://github.com/emcrisostomo/fswatch, https://anarc.at/blog/2019-11-20-file-monitoring-tools/
- Falco/eBPF: https://falco.org/blog/tracing-syscalls-using-ebpf-part-1/, https://ebpf.io/applications/, https://morgannriu.fr/en/blog/ebpf-observabilite-linux-falco-cilium-2026
