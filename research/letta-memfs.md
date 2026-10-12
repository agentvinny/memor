# Letta (MemGPT) — MemFS, Context Repositories, and "Dreaming"

Research date: 2026-08-23

## Overview

Letta (formerly MemGPT) is an open-source (Apache-2.0) platform for building "stateful agents" — agents with persistent, self-editing memory. The core runtime is `letta-ai/letta` (server + SDKs), with a coding-agent product called `letta-ai/letta-code`. In 2026 Letta unified its memory model around **MemFS**, a "memory filesystem," and in February 2026 shipped **Context Repositories**, which make that filesystem explicitly git-backed and central to how Letta Code (their coding agent) manages memory. A background consolidation process called **"dreaming"** (evolution of MemGPT's original "sleep-time compute") periodically reviews and rewrites memory.

Sources:
- https://docs.letta.com/concepts/memfs
- https://docs.letta.com/letta-code/memfs
- https://docs.letta.com/letta-agent/memory (redirects/aliases to https://docs.letta.com/configuration/memory)
- https://docs.letta.com/v1-sdk/memory/context-hierarchy
- https://www.letta.com/blog/context-repositories/
- https://www.letta.com/blog/letta-code/
- https://www.letta.com/blog/agent-memory/
- https://github.com/letta-ai/letta
- https://github.com/letta-ai/letta-code
- https://aireviewzones.com/letta-review/
- https://www.leoniemonigatti.com/blog/memgpt.html

## How It Works

**Storage model.** Each Letta agent owns a git repository that holds its memory ("The memory itself is part of the agent's state, held in a git repository that belongs to the agent" — docs.letta.com/concepts/memfs). MemFS projects this repo onto a real filesystem checkout that the agent (or, per the Context Repositories blog, Letta Code specifically) reads and edits with ordinary file tools (cat/grep/edit-style operations), not a bespoke memory API. Cloud-hosted agents push/pull to a Letta-hosted remote; local/self-hosted agents keep the repo on the local machine.

**Directory structure.** A consistent convention appears across docs and the blog:
```
$MEMORY_DIR/
├── system/          # always loaded into the system prompt every turn
│   ├── persona.md
│   └── human.md
├── reference/        # out-of-context until the agent chooses to read it
│   └── project-notes.md
└── skills/           # agent-owned, versioned alongside memory
    └── my-skill/SKILL.md
```
Files carry YAML frontmatter (explicitly compared to Anthropic's `SKILL.md` convention) describing their contents. The blog post cites a target of "15–25 focused files" after a "defragmentation" pass.

**Progressive disclosure.** Implemented structurally, not semantically: `system/` files are unconditionally injected into the system prompt every turn; everything else stays out of context. Critically, "the file tree itself is always in the system prompt," so directory/file names act as signposts the agent uses to decide what to read next. This is a two-tier (in-context vs. on-demand-via-tool-call) scheme, not a multi-level summary→detail hierarchy.

**Git's role — versioning, not an explicit temporal model.** Every memory edit is committed, with auto-generated, "informative" commit messages, giving a changelog of what the agent learned and when. Git also underwrites *concurrency*: memory subagents (e.g., "dreaming," "memory doctor," `/init` history-mining subagents) each get their own **git worktree** so they can edit memory in parallel without blocking the main agent or each other, and changes are merged back via ordinary git merge/conflict resolution. However, nothing in the docs or blog describes an explicit valid-from/valid-until or current-vs-historical data model — there's no first-class "this fact was true until commit X, superseded at commit Y" object. The system distinguishes *saved* (committed) memory from *uncommitted* working changes, but querying "what did I believe about X two weeks ago" would require manually walking `git log`/`git blame` on the memory repo yourself; the agent's normal operation reads only the current checkout, i.e. it behaves like **latest-commit-wins** for retrieval purposes, with full history recoverable but not natively surfaced or queried by the agent's tools.

**Agent-agnosticism — real but unconfirmed at the edges.** Because MemFS is literally Markdown files in a git repo on disk, in principle any tool (a plain script, `grep`, another coding agent, Claude Code) could read or even write it directly — the docs describe "Unix philosophy" access ("agents can chain standard tools... use bash for batch operations, or write scripts to process memory programmatically"). This is a meaningfully more open design than a proprietary vector-DB memory store. That said:
- Direct third-party read/write is **not explicitly documented, tested, or supported** — every source I found (docs.letta.com/concepts/memfs, docs.letta.com/letta-code/memfs) stops short of confirming interoperability guarantees, locking semantics, or what happens if a non-Letta process edits the repo while a Letta subagent has a worktree open.
- The *initialization*, commit-message generation, worktree orchestration, defragmentation, and merge-conflict handling are done by Letta's own subagents (dreaming, memory doctor, `/init`) — i.e., the intelligence that curates the repository is Letta-specific even though the storage substrate (git + Markdown) is generic.
- Practical agent-replacement test: a different harness *could* clone the repo and read `system/*.md` for the always-loaded summary, and grep `reference/`/`skills/` for details — this would satisfy a weak form of the brief's "agent replacement test." But there is no documented API/MCP contract independent of the Letta server for a foreign agent to *safely* write back into the repo (respecting worktree/merge conventions), and no evidence that Letta ships a standalone spec for third parties to implement against.

**External evidence ingestion.** Populated by (a) the agent's own conversational/task activity, (b) `/init`, which inspects the target project's codebase and can optionally mine *prior* Claude Code / Codex session histories (fanned out across concurrent subagents, one worktree per subagent, merged back into "main" memory), and (c) explicit user teaching via `/remember`. I found **no evidence** of live ingestion of arbitrary environmental telemetry — real project git commit streams (beyond the one-time `/init` scan), shell/terminal activity logs, Docker/systemd state, or other system events — as an ongoing capture pipeline. Memory is fundamentally agent-populated (what the agent reads/does/is told), not an environment-observability layer.

**Dreaming (consolidation).** "Dreaming" is a background subagent process that reviews recent conversations/sessions and updates memory without blocking the active session. Triggers: after a configurable number of completed agent steps, or when the context window is compacted; also user-invocable via `/sleeptime` (CLI) or a "Dreams" setting (app). Mechanically it is described as **copy-on-write**: a dream reads the existing memory store plus recent session history and produces a *separate candidate memory store*; the original remains intact, so the user/agent can review, accept, discard, or roll back rather than have memory silently mutated. An optional "agent reviews before applying" mode adds a secondary review pass before a dream's edits are merged into main memory. This is the closest Letta gets to a "consolidation" step (deduplication, distillation into `system/` vs `reference/` placement, "defragmentation" of files) — but it is not described as detecting/resolving logical contradictions between facts, only as summarizing/reorganizing.

## Scoring vs. the 6 Categories

**1. Capture — Weak/Partial.**
Captures: agent conversation turns, task/tool-call history, one-time `/init` codebase + prior-session scans, explicit user `/remember` statements. Does **not** capture (per available docs): live shell/terminal activity, ongoing real-repo git commit streams, Docker/systemd/service state, or other agents' independent actions unless funneled through a Letta agent's own turns. Multi-agent capture exists only in the narrow sense of multiple Letta subagents writing to the same repo via worktrees.

**2. Representation — Partial.**
Has: hierarchy of a sort (system/ vs reference/ vs skills/, plus a separate 4-layer "context hierarchy" of memory blocks → files → archival vector DB → external RAG/MCP). Has: file-level provenance in the loose sense that git commit messages narrate what changed and why. Missing/unconfirmed: no explicit entity-relationship graph, no confidence scoring, no first-class current-vs-historical/valid-time model (git log is a de facto audit trail but isn't surfaced as queryable temporal state), and no dedicated "decision + rationale" schema distinct from free-text Markdown. Everything is unstructured Markdown, not typed facts/entities.

**3. Retrieval — Partial.**
Exact retrieval: file reads, grep, tool-based file/archival search. Semantic: archival memory is an explicit vector DB with similarity search. Progressive disclosure: yes, structurally (system/ auto-loaded, rest via file tree navigation). Missing: no documented graph traversal / multi-hop relationship queries, no temporal query support ("what was true as of date X"), no explicit scope-filtered queries (e.g., "give me only project-level facts") beyond directory conventions the agent must interpret itself.

**4. Consolidation — Partial.**
"Dreaming" performs extraction/summarization/reorganization of session history into memory files, copy-on-write with reviewable candidate stores, and "defragmentation" toward a smaller focused file set. Raw evidence (original session transcripts) is presumably retained separately from the distilled memory (the dream leaves the "input... intact"). Not confirmed: explicit duplicate/contradiction detection logic, or a formal "supersede stale fact" operation — reorganization appears to be file/structure-level rather than fact-level reconciliation.

**5. Integration — Moderate, but Letta-centric.**
Has a server, REST-style Agent API/SDKs (Python/TypeScript), and third-party MCP servers exist (e.g., `oculairmedia/Letta-MCP-server`) exposing agent/memory-block CRUD over MCP. The underlying storage (git + Markdown files) is filesystem-native and tool-agnostic in principle. However, the *authoritative* way to create/curate/merge memory is through Letta's own agent loop and subagents; there's no documented standalone spec/protocol for arbitrary external harnesses to safely co-author the repository, and no evidence of an event-stream/webhook mechanism for external systems to push updates in.

**6. Reliability — Good on inspectability, unclear on correction workflow.**
Strong point: because memory is literally Markdown in git, it is fully human-inspectable, diffable, and revertible with standard git tooling (`git log`, `git diff`, `git revert`), and "dreaming" is explicitly designed as reviewable/rollback-safe (copy-on-write, optional human/agent review before merge). Weaker point: "survives agent replacement" is only structurally possible (any git-aware tool can read/write the files) but not validated/supported by Letta as a first-class use case — no documented conformance suite or guarantee that a foreign harness editing the repo won't conflict with Letta's own worktree/merge conventions.

## Maturity / License / Effort

- **License:** `letta-ai/letta` is Apache-2.0, confirmed via GitHub. Self-hosted Letta is described (aireviewzones.com, evermind.ai) as free with "all features" available outside the hosted product.
- **Maturity:** 23K+ GitHub stars as of mid-2026 (aireviewzones.com); Context Repositories shipped February 2026, so the git-backed MemFS model specifically is quite new (roughly 6 months old at time of writing).
- **Self-hosting effort:** Reviewed as high operational complexity for production — aireviewzones.com's 2026 review notes self-hosted deployments need tuned PostgreSQL connection pools ("80+ concurrent connections"), long provider timeouts ("600+ seconds" to survive context compaction), and that multi-agent coordination ("Memory Swarms") can consume "up to 15× more tokens than standard single-agent chats." It also warns of "extreme model dependency" — smaller open models like Qwen/Llama see frequent tool-call/JSON-parsing failures, i.e. Letta's memory-editing loop assumes strong function-calling models.
- **Cloud vs. self-hosted split:** Free tier allows a few managed agents (BYOK, no login for local use); paid tiers (~$20/mo Pro, custom Enterprise) add hosted "Letta Auto" model quota and scaling. The core memory mechanics (MemFS, dreaming, context repos) are present in the open-source/self-hosted path, not gated to cloud only, based on available sources — but this wasn't independently verified against source code, only docs/marketing.

## Gaps vs. the Brief's Vision

- **No explicit temporal/valid-time model.** Git history exists but isn't exposed as a first-class "current vs. previously true, when it changed, why" query surface — you'd have to script `git log`/`git blame` yourself; Letta's own agent tooling reads only the live checkout.
- **No confidence levels or structured provenance beyond commit messages.** Everything is free-text Markdown; there's no schema for fact/decision/evidence/confidence as distinct typed objects.
- **No entity/relationship graph or multi-hop query capability** — retrieval is file-navigation + vector search, not graph traversal.
- **Capture is agent-mediated, not environment-observing.** It does not ingest live shell activity, docker/systemd state, or continuous external git-commit streams from the real project; `/init` does a one-time historical scan. This is a significant gap against the brief's "capture external activity/system/service events" requirement.
- **Agent-agnosticism is structural/theoretical, not a supported contract.** The storage format (git + Markdown) is refreshingly open compared to vector-DB-locked competitors, and passes a weak version of the "agent replacement test" (any git-aware tool can read the current memory state). But Letta does not publish a spec, conformance guarantees, or safe-write protocol for foreign harnesses; the curation intelligence (dreaming, defragmentation, conflict merge) is Letta's own subagents, so quality of a foreign agent's direct edits (commit hygiene, worktree discipline, avoiding clobbering a live dream) is unverified.
- **Consolidation is structural/summarization-focused**, not explicit contradiction detection or fact-supersession logic.
- **High operational cost and model-quality dependency** for self-hosting at any real scale, per independent review.

## Verdict

Letta's MemFS/Context Repositories is a genuinely interesting and *directionally* aligned piece of prior art — git-backed, human-readable/diffable/revertible Markdown memory with structural progressive disclosure and a reviewable, non-destructive consolidation process ("dreaming") — but it is **not a full solution** to the brief. It is best read as a strong **storage/versioning-layer reference design** (git+Markdown-as-memory-substrate, worktree-based concurrent editing, copy-on-write consolidation) rather than a complete durable-knowledge system. It falls short on: explicit temporal/valid-time modeling, structured entities/relationships/confidence/provenance, environmental-evidence capture (git/shell/service state beyond one-time scans), and a documented, supported contract for non-Letta agents to safely read/write the store (the "agent replacement test" passes weakly and only at the raw-filesystem level, not as an intentional, guaranteed integration point). A system built for this brief could reasonably borrow Letta's git-as-memory-substrate and copy-on-write dreaming pattern for the representation/consolidation layers, while building its own temporal/entity/provenance schema and environment-capture pipeline on top, and exposing retrieval via a real API/MCP contract rather than relying on ad hoc file-tree navigation.
