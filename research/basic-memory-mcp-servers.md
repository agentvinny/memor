# Basic Memory & the MCP Memory Server Ecosystem — Research Findings

Research date: 2026-08-23. Conducted for the design brief in `agent_memory_research_brief.md` (durable, hierarchical, temporal, evidence-backed, tool/harness-agnostic memory for developer projects and technical environments).

---

## Overview

Four things were investigated:

1. **Basic Memory** (`basicmachines-co/basic-memory`) — Markdown-native, local-first MCP server, Obsidian-friendly.
2. **Official reference "memory" server** (`modelcontextprotocol/servers`, `src/memory`) — JSON-file knowledge-graph demo server.
3. **`doobidoo/mcp-memory-service`** — REST + MCP server with SQLite/Milvus/Cloudflare backends and a "consolidation" subsystem.
4. **`mkreyman/mcp-memory-keeper`** — SQLite-backed session/context store for coding assistants with "session branching" and lightweight NLP-ish features.

Plus the **Model Context Protocol itself**, treated as a separate question from any specific server: is MCP the right *integration layer* regardless of which memory backend the brief eventually adopts?

All four sub-investigations were done via live WebSearch/WebFetch/GitHub API calls, not from training-data memory. Sources are inlined below each finding.

---

## Basic Memory findings

**On-disk format.** Plain Markdown files with YAML frontmatter (`title`, `type`, `tags`, `permalink`). One entity = one `.md` file. Inside the body, two special line-patterns encode the knowledge graph on top of ordinary Markdown:

- **Observations**: `- [category] content #tag`, e.g. `- [decision] Using JWT tokens for stateless authentication #security`. Categories include `[fact] [idea] [decision] [technique] [requirement] [problem] [solution] [insight]`. Each observation is indexed individually, not just the containing document.
- **Relations**: `- relation_type [[Target Note]]`, e.g. `- implements [[API Security Requirements]]`, `- depends_on [[User Database Schema]]`. Multi-word relation types are quoted. A bare `[[Note]]` defaults to `links_to`. Relations can forward-reference notes that don't exist yet; the edge resolves once the target file is created.
- **`memory://` URIs** are a *reference scheme* used by MCP tools (e.g. `build_context`) to address notes (`memory://title`, `memory://folder/title`, `memory://permalink`) — not something written into the files themselves.

Sources: [Knowledge Format](https://docs.basicmemory.com/concepts/knowledge-format), [Memory URLs](https://docs.basicmemory.com/concepts/memory-urls), [GitHub](https://github.com/basicmachines-co/basic-memory)

**Hierarchy/scoping.** "Projects" are the top-level scoping unit, each a separate folder (local or cloud instance) with its own independent notes, graph, and search index — explicitly siloed: "Notes in one project do not appear in another, and relations in one project cannot link to notes in a different project." Managed via CLI (`bm project add "personal" ~/Documents/personal-notes`) or the `switch_project`/`list_memory_projects` MCP tools. Below the project level, scoping is just filesystem sub-folders — there is no additional namespace/component concept, so a `global -> project -> component -> task` tree would have to be hand-built as folder conventions. Source: [Projects and folders](https://docs.basicmemory.com/concepts/projects-and-folders)

**Temporal modeling.** None in the open-source local server. The current file content is the only state; docs are explicit that "notes are plain files you can track with git" — git is a suggestion, not a built-in mechanism, and there is no valid-time/current-vs-superseded field anywhere in the schema. Full version history is reportedly a **paid cloud** feature (per a secondary source, not the primary docs — see caveat below), separate from the free/self-hosted core. Sources: [What is Basic Memory](https://docs.basicmemory.com/start-here/what-is-basic-memory); sync-internals detail from a third-party writeup ([akitaonrails/ai-memory research notes](https://github.com/akitaonrails/ai-memory/blob/main/docs/research-basic-memory.md)) could not be independently confirmed against primary docs.

**Tool-agnosticism.** Genuinely generic. It is a standalone Python package (`uv tool install basic-memory`) speaking MCP over stdio or HTTP(S), documented as working with Claude Desktop, Claude Code, Codex, Cursor, VS Code, ChatGPT (via custom actions), and Obsidian directly on the files. No Anthropic-specific code paths found. Sources: [GitHub README](https://github.com/basicmachines-co/basic-memory), [PulseMCP listing](https://www.pulsemcp.com/servers/basicmachines-memory)

**Search / progressive disclosure.** `search_notes` gives full-text search; `build_context` traverses the relation graph outward from a hit to assemble richer context on demand — functionally Basic Memory's version of progressive disclosure (index hit -> pull in linked notes as needed, rather than loading everything). No confirmed vector/embedding-based semantic search in the OSS core — "semantic" in their marketing appears to describe the typed-relation graph structure, not embedding similarity; this could not be fully verified against source code.

**Maturity (verified via GitHub API, 2026-08-23).** License **AGPL-3.0**. 3,725 stars, 261 forks, 77 open issues, 38 contributors. Created 2024-12-02, last push same day as this research (actively maintained). Frequent point releases (v0.21.4 → v0.22.1 within ~3 weeks in May–June 2026). **Pre-1.0** (currently v0.22.x) — API/format still subject to change. A commercial cloud-sync tier (~$15/mo) sits on top of the free core.

---

## MCP memory server ecosystem findings

### Official reference server (`modelcontextprotocol/servers/src/memory`)

- **Storage**: flat JSONL file (`memory.jsonl` by default, path via `MEMORY_FILE_PATH`), no database.
- **Data model**: Entities (name, entityType, observations[]), Relations (from -> to, relationType, active voice), Observations (atomic strings). Single flat graph per file; no multi-project scoping (would need separate server instances per file).
- **Temporal modeling**: none — flat mutable graph, no timestamps or history.
- **Retrieval**: 9 tools (`create_entities`, `create_relations`, `add_observations`, `delete_*`, `read_graph`, `search_nodes`, `open_nodes`). Exact/substring text match only, no vector search, no traversal query language.
- **Maturity/status**: explicitly a **reference/demo**, not production code — the repo states these servers "are intended as reference implementations to demonstrate MCP features and SDK usage." Parent repo is huge and active (~89.8k stars, 4,161+ commits), but this specific server is example code. Apache-2.0 (new contributions).
- **Tool-agnosticism**: confirmed generic — plain JSON-RPC/MCP over stdio or HTTP, no Claude-specific behavior.

Sources: [github.com/modelcontextprotocol/servers](https://github.com/modelcontextprotocol/servers/tree/main/src/memory), [npm package](https://www.npmjs.com/package/@modelcontextprotocol/server-memory)

### `doobidoo/mcp-memory-service`

- **"Autonomous consolidation" — real or marketing?** Real code, not just a slogan. `src/mcp_memory_service/consolidation/` contains `scheduler.py`, `consolidator.py`, `clustering.py`, `compression.py`, `decay.py`, `forgetting.py`, `contradictions.py`, `belief.py`, `associations.py`, `insights.py`, `quarantine.py`, `relationship_inference.py`. Named components (`DreamInspiredConsolidator`, `ExponentialDecayCalculator`, `SemanticClusteringEngine`, `SemanticCompressionEngine`, `ControlledForgettingEngine`) point to embedding-similarity clustering + exponential decay/forgetting-curve scoring + compression, run by an internal scheduler — not LLM-based summarization. Changelog churn (v11.5.0 "conditional temporal decay + functional belief derivation"; v11.8.1 fixing consolidation feedback loops) shows ongoing real engineering, not a one-off stub.
- **Capture surface**: explicit REST/MCP calls (`POST /api/memories`, `POST /api/memories/search`) is the primary path — same "agent must write it" model as everything else here. One partial exception: the Claude Code plugin auto-captures on the session **Stop event**, gated on "substantive content" — this is a Claude-Code-specific hook, not a generic git/shell/docker/systemd watcher. No evidence anywhere of automatic ingestion of git diffs, shell history, or system/service state.
- **Storage backends**: SQLite (default/local), Cloudflare Workers (cloud sync), hybrid (SQLite + background Cloudflare sync), Milvus (Lite/self-hosted/Zilliz Cloud). No first-class Postgres/Qdrant. Self-hosting via `pip install` or an interactive installer script, Docker images provided — not single-binary, but low-to-moderate effort.
- **Knowledge graph / time**: typed edges (`causes`, `fixes`, `contradicts`), auto-linking of @mentions/#tags/URLs/file-paths, pluggable NER extractors. Time is handled via decay/forgetting weighting, **not** a bitemporal/valid-time current-vs-superseded model.
- **Retrieval**: local ONNX embeddings (all-MiniLM-L6-v2, 384-dim) for semantic search; docs claim hybrid BM25+vector search; decay-weighted "temporal-aware" ranking; benchmarked against LongMemEval/DevBench/LoCoMo (72.0% R@5 cited, unverified independently).
- **Maturity**: Apache-2.0. ~1.9k stars, 289 forks, 8 open issues, latest release v11.8.1 (2026-08-22), created Dec 2024 — very active. **66 contributors per API but heavily maintainer-dominated** (doobidoo: 2,463 commits vs. next-highest 68) — effectively a solo/BDFL project.
- **Framework-compatibility claim check**: README advertises "Works with LangGraph · CrewAI · AutoGen · any HTTP client · Claude Desktop · OpenCode." No first-party LangChain/CrewAI/AutoGen adapter packages or classes were found — the "Agent Integration Guides" show generic HTTP-call pseudocode against the REST API. **This is the clearest gap between marketing language and actual code**: the claim is true only in the weak sense that "anything that can make an HTTP/MCP call can use it," not that dedicated framework-native integrations exist.

Sources: [GitHub](https://github.com/doobidoo/mcp-memory-service), [README](https://raw.githubusercontent.com/doobidoo/mcp-memory-service/main/README.md), [Dev Reference wiki](https://github.com/doobidoo/mcp-memory-service/wiki/06-Development-Reference), [Consolidation test results](https://glama.ai/mcp/servers/@doobidoo/mcp-memory-service/blob/f94a1f2c592bcd169cdb77e85f2948c9519f8ce6/CONSOLIDATION_TEST_RESULTS.md), GitHub REST API.

### `mkreyman/mcp-memory-keeper`

- **Storage**: SQLite (`better-sqlite3`, WAL mode), default `~/mcp-data/memory-keeper/context.db` — a real relational DB, technically inspectable with any SQLite client, though the project only documents access via its own MCP tools.
- **"Session branching"**: not git-DAG semantics. `mcp_context_branch_session` copies rows from one flat session into a new session (`shallow` = high-priority only, `deep` = everything); `mergeSessions` reconciles with `keep_newest`/`keep_current`/`keep_source` strategies. Copy-and-reconcile, not branch/commit/diff.
- **"Semantic search"**: not neural. `src/utils/vector-store.ts` implements 384-dim **hashed character-trigram/word-token vectors** (MD5-hashed buckets), explicitly commented as "a lightweight alternative to neural embeddings," with cosine similarity over these hash vectors. No embedding-model dependency in `package.json`.
- **"Entity/relationship extraction"**: automatic **regex pattern matching** (`src/utils/knowledge-graph.ts`), not NLP/LLM-based, triggered only when the agent explicitly calls `mcp_context_analyze()`. Stored in `entities`/`relations` (subject/predicate/object/confidence) tables.
- **Hierarchy/scoping**: only two flat layers — "sessions" (optionally tied to a project directory) and "channels" (topic tags auto-derived from git branch name). No global/project/component/task nesting.
- **Capture surface**: entirely tool-call-driven. Git status is captured only as a one-shot snapshot when a checkpoint is created with `includeGitStatus: true` — not continuous diff tracking. No shell/Docker/systemd capture found anywhere.
- **Temporal modeling**: no supersession/versioning/decay/TTL concept found in code or docs — only `created_at`/`updated_at` timestamps, a timeline visualization, and point-in-time checkpoint restore.
- **Maturity**: MIT license. 134 stars, 27 forks, 1 open issue, created 2025-06-18, last push 2026-06-06 (~2.5 months stale). package.json at v0.14.0, GitHub Releases lag at v0.12.1. Essentially single-maintainer (136 commits by mkreyman vs. 3 by a second contributor) — small, not team-backed, but shows real engineering discipline (CI, "adversarial-review" hardening commits, Codecov).
- **Honesty check**: README language ("automatically extract entities," "semantic search," "knowledge graph") reads more sophisticated than the implementation; the underlying mechanisms are legitimate but simpler/more mechanical (regex + hashed n-grams) than the marketing implies.

Sources: [GitHub](https://github.com/mkreyman/mcp-memory-keeper), raw README/source files, GitHub REST API.

---

## Scoring vs 6 Categories

Scale: **None / Weak / Partial / Strong**, followed by the evidence.

### 1. Capture (external activity/terminals/git/system/service events/multiple agents/manual changes)

| System | Score | Evidence |
|---|---|---|
| Basic Memory | **None (ambient)** / Strong (manual) | Purely what a human or agent writes to a `.md` file or via MCP write tools. No automatic environment capture of any kind. |
| Official reference server | **None** | Only `create_entities`/`add_observations` tool calls; nothing automatic. |
| mcp-memory-service | **Weak** | Explicit REST/MCP writes are the norm; one Claude-Code-specific Stop-event auto-capture hook exists. No generic git/shell/docker/systemd watcher. |
| mcp-memory-keeper | **Weak** | One-shot git-status snapshot only on manual checkpoint creation; no continuous capture of shell/docker/systemd. |

**Category verdict: the whole MCP ecosystem (and Basic Memory) scores essentially the same here — capture is 100% "what an LLM/agent/human explicitly writes via a tool call or file edit," never ambient environment observation.** This is a categorical gap against the brief's "capture layer separate from memory layer" vision (brief §10–12).

### 2. Representation (hierarchy, entities, relationships, time/current-vs-historical, provenance, confidence, decisions+rationale)

| System | Score | Evidence |
|---|---|---|
| Basic Memory | **Partial** | Real entities+typed relations in Markdown; observation *categories* (`[decision]`, `[fact]`, `[insight]`, `[problem]`) give a lightweight decisions/rationale/fact distinction. Projects give one level of hierarchy (no component/task nesting). No time/current-vs-superseded modeling in OSS core; no explicit confidence field; provenance = "whatever is written in the note," not a structured field. |
| Official reference server | **Weak** | Entities/relations/observations only; no hierarchy, no time, no provenance, no confidence. |
| mcp-memory-service | **Partial** | Typed edges (`causes`, `fixes`, `contradicts`) and NER-based entity extraction; decay/forgetting math is a proxy for "current-ness" but not an explicit valid-time/superseded model; no first-class confidence or decision/rationale schema found. |
| mcp-memory-keeper | **Weak-Partial** | Entities/relations via regex extraction with a confidence column; two flat scoping layers only; no time/versioning model. |

### 3. Retrieval (exact/semantic/hybrid/graph traversal/temporal/scope filtering/progressive disclosure/multi-hop)

| System | Score | Evidence |
|---|---|---|
| Basic Memory | **Partial** | Full-text search + graph-traversal-based `build_context` gives real progressive disclosure and multi-hop within a project; project-level scope filtering; no confirmed vector/semantic search; no temporal queries (nothing to query temporally). |
| Official reference server | **Weak** | Exact/substring text search and direct node lookup only; no semantic, no traversal query language, no scoping. |
| mcp-memory-service | **Partial-Strong** | Real vector embeddings (ONNX MiniLM) + claimed hybrid BM25/vector search + decay-weighted "temporal-aware" ranking + entity-graph traversal. Best retrieval sophistication of the three ecosystem servers, though some claims (72% R@5, "5ms retrieval") are self-reported and unverified. |
| mcp-memory-keeper | **Weak** | "Semantic search" is hashed-trigram cosine similarity, not true semantic search; regex-based graph exists but shallow; session/channel scope filtering only. |

### 4. Consolidation (extraction, duplicate/contradiction handling, stale-fact superseding, raw evidence retention)

| System | Score | Evidence |
|---|---|---|
| Basic Memory | **None** | No consolidation subsystem; the file is the fact, forever, until a human/agent edits it. |
| Official reference server | **None** | Explicitly a bare CRUD demo. |
| mcp-memory-service | **Strong (real, but decay-based not supersession-based)** | Verified `consolidation/` module (clustering, compression, decay, forgetting, contradiction-detection code files exist: `contradictions.py`, `belief.py`). This is the *only* system of the four with genuine automated consolidation machinery. However it models "staleness" via decay scoring, not the brief's explicit "supersedes / superseded-on / reason" record — and raw-evidence retention post-compression is unclear from docs. |
| mcp-memory-keeper | **None** | No dedup/contradiction/staleness logic found; only manual overwrite. |

### 5. Integration (API/MCP/CLI/SDK/filesystem/database/event stream, multi-agent consumption)

| System | Score | Evidence |
|---|---|---|
| Basic Memory | **Strong** | MCP (stdio/HTTP) + CLI + raw filesystem access (any editor, Obsidian, git) — genuinely multi-modal integration, and multi-client-verified (Claude Desktop/Code, Cursor, VS Code, Codex, ChatGPT, Obsidian). |
| Official reference server | **Partial** | MCP only, plus a raw JSONL file that's technically readable by anything, but no CLI/API. |
| mcp-memory-service | **Strong** | MCP + a 76-endpoint REST API explicitly pitched as framework-agnostic; Docker; multiple storage backends. Weakest point is the gap between "framework-agnostic API" and actual first-party framework adapters (see above). |
| mcp-memory-keeper | **Partial** | MCP tools + underlying SQLite DB that could be queried directly, but that path is undocumented/unsupported. |

### 6. Reliability (human-inspectable, auditable, correctable, rollback, provenance inspectable, survives agent replacement)

| System | Score | Evidence |
|---|---|---|
| Basic Memory | **Strong** | Plain Markdown+YAML files, human-readable/editable with any text editor, git-trackable by the user, Obsidian-viewable graph. Best-in-class here by design intent. Rollback = whatever git discipline the user applies themselves (not built-in to OSS core). |
| Official reference server | **Partial** | JSONL is human-readable-ish but not designed for editing; no rollback/versioning. |
| mcp-memory-service | **Partial** | SQLite/Milvus stores are not casually human-editable; some inspectability via REST API and a 3D graph visualizer; no explicit rollback beyond backend-level backups. |
| mcp-memory-keeper | **Weak-Partial** | SQLite DB, technically inspectable but not intended to be human-edited; checkpoint/restore gives a rollback primitive, which is actually a plus here. |

**Net picture**: Basic Memory wins categories 5 and 6 (integration, reliability/human-inspectability) by a wide margin because of the plain-Markdown design. mcp-memory-service wins categories 3 and 4 (retrieval sophistication, real consolidation machinery) because it's the only one with genuine ML-backed search and an actual consolidation engine. All four are roughly equally weak on category 1 (capture) and category 2 (representation) relative to the brief's ambitions — none has hierarchy deeper than "projects" or "sessions," none has the brief's explicit temporal-validity/decision-rationale/confidence schema, and none captures ambient environmental evidence automatically.

---

## Maturity / License / Effort summary

| System | License | Stars | Contributors | Status | Self-hosting effort |
|---|---|---|---|---|---|
| Basic Memory | AGPL-3.0 | 3,725 | 38 | Pre-1.0 (v0.22.x), very active | `uv tool install`; low |
| Official reference server | Apache-2.0 (new)/MIT (legacy) | ~89.8k (whole monorepo) | many (monorepo-wide) | Explicitly reference/demo code, not production | `npx`/single file; trivial |
| mcp-memory-service | Apache-2.0 | ~1.9k | 66 (maintainer-dominated: 2,463 vs 68 commits) | v11.8.1, very active, daily-scale cadence | pip/Docker; low-moderate; Milvus Lite explicitly discouraged for production |
| mcp-memory-keeper | MIT | 134 | 2 effective (136 vs 3 commits) | v0.14.0, last push ~2.5 months ago, small/solo | npm install; low |

---

## Gaps vs the brief's vision

Checked against the brief's core asks (hierarchy to component/task depth, temporal validity with supersession+reason, confidence/provenance fields, decisions+rationale as first-class objects, ambient capture layer separate from memory layer, multi-hop hierarchical+temporal retrieval):

- **No ambient capture layer exists in any of the four systems.** All of them are "write it via a tool call (or edit the file)" — none listens to shell history, git hooks, Docker events, systemd/service state, or filesystem watchers on their own. The brief's Level-4 "raw evidence" layer (terminal capture, diffs, process observation) has no counterpart here; it would have to be built as a separate capture pipeline that then *writes into* one of these stores via its API/MCP tools.
- **No system implements the brief's explicit temporal-validity record** (`Fact / Valid from / Supersedes / Superseded on / Reason / Evidence`). Basic Memory has zero temporal modeling in its OSS core (paid-cloud-only version history, per an unverified secondary source). mcp-memory-service has real decay/forgetting math, which is a different thing — it degrades relevance over time rather than recording *why* something changed and preserving the old fact as an explicit superseded record.
- **No system has a formal confidence field** (`confirmed/probable/inferred/unknown/contradictory`) as the brief specifies, though mcp-memory-keeper has a numeric confidence column on extracted relations, and Basic Memory's observation categories (`[decision]`, `[fact]`, `[insight]`, `[problem]`) are a partial, informal analog.
- **Hierarchy stops at "project" or "session" everywhere.** None natively models `global -> project -> component -> task`; Basic Memory gets closest (projects = folders, then arbitrary sub-folders/notes), but component/task granularity would be a convention the user imposes, not a modeled concept.
- **Decisions/rationale as structured objects**: Basic Memory's `[decision]` observation category is the only direct analog found; none of the three MCP ecosystem servers has a decision/alternatives-considered/rationale schema.
- **Basic Memory is the strongest fit for the brief's "human can read/edit it directly" requirement** by a wide margin (plain Markdown+YAML+wikilinks, Obsidian-compatible), but is the weakest on temporal/consolidation modeling. mcp-memory-service is the inverse: strongest on retrieval/consolidation machinery, weakest on human-readability (SQLite/vector-store internals).

---

## Verdict: is MCP itself the right integration layer?

**Yes, for the integration/transport layer — but MCP solves a narrower problem than the brief needs solved, and choosing MCP does not get you any of the memory-model properties the brief cares about for free.**

Evidence for "yes, adopt MCP as the interface":
- MCP is no longer an Anthropic-only technology. In December 2025, Anthropic donated MCP to a new Linux-Foundation-hosted **Agentic AI Foundation (AAIF)**, co-founded with **Block and OpenAI**, with **AWS, Google, Microsoft, Cloudflare, and Bloomberg** as platinum members — governance now modeled on the Kubernetes/CNCF pattern. OpenAI shipped MCP support in its Agents SDK and ChatGPT; Google confirmed Gemini tool-use support; the community server catalog exceeds 5,800 connectors; combined SDK downloads are roughly 97M/month. This is now reasonably described as a de facto cross-vendor standard, not vendor lock-in. ([pento.ai](https://www.pento.ai/blog/a-year-of-mcp-2025-review), [Wikipedia: MCP](https://en.wikipedia.org/wiki/Model_Context_Protocol), [chatforest.com](https://chatforest.com/guides/mcp-ecosystem-2026-state-of-the-standard/), [andrew.ooo](https://andrew.ooo/answers/mcp-model-context-protocol-enterprise-adoption-july-2026/))
- All three ecosystem servers plus Basic Memory confirm this in practice: every one of them is consumable by multiple harnesses (Claude Desktop/Code, Cursor, VS Code, OpenCode, generic HTTP clients), not just one vendor's product — directly satisfying the brief's "tool/harness-agnostic" requirement at the transport level.

Evidence for "but it doesn't solve the memory-model problem":
- MCP standardizes **only** the wire protocol (JSON-RPC 2.0), two transports (stdio, Streamable HTTP), and four primitives (Tools, Resources, Prompts, Sampling) with generic list/get/call semantics. It explicitly does **not** standardize any memory data model, entity/relation schema, hierarchy/namespace convention, temporal-validity semantics, consolidation behavior, or retrieval/ranking strategy — all of that is left to whichever server sits behind the protocol. This is exactly why the four systems researched here have four *completely different, mutually incompatible* on-disk representations (Markdown files, JSONL, SQLite+hashed-vectors, SQLite+ONNX-embeddings+decay) despite all speaking "the same" protocol.
- Known ecosystem-level limitations specific to memory use cases: no standard for progressive disclosure (all connected servers' tool definitions load into context up front by default, which can consume a large fraction of the context window before any user turn happens); no portability of memories between different memory servers; no native versioning/consolidation contract in the spec; and unresolved concurrency semantics for multiple agents writing to one shared memory server (locking is left to each server's own implementation). ([demiliani.com](https://demiliani.com/2025/09/04/model-context-protocol-and-the-too-many-tools-problem/), [mitek99.medium.com](https://mitek99.medium.com/mcps-overengineered-transport-and-protocol-design-f2e70bbbca62))

**Recommendation implied by this research**: treat MCP as the settled answer for "how do arbitrary harnesses talk to the memory system" — build/expose the eventual memory system as an MCP server (tools for write/query, resources for browsing) so it's usable from Claude Code, Cursor, other IDEs, and future harnesses without per-harness integration work. But do **not** adopt any of the four researched systems as the memory *model* wholesale: the official reference server is admittedly just a demo; mcp-memory-keeper and the official server have no consolidation or temporal semantics at all; mcp-memory-service has the most real consolidation/retrieval engineering but no explicit supersession-with-rationale model and a SQLite/vector-store substrate that fails the "human can read/edit it directly" requirement; Basic Memory is the closest prior art for the human-readable/git-friendly storage requirement (plain Markdown + YAML + wikilink relations + observation-category tagging) and could plausibly be forked or imitated for its file format, but its hierarchy tops out at flat "projects," it has no temporal-validity or confidence model in the open-source core, and it is pre-1.0 (v0.22.x, AGPL-3.0, so licensing terms matter if code is reused/embedded rather than just run as a subprocess). The brief's differentiating requirements — ambient environmental capture, explicit supersession-with-reason, confidence levels, decision/rationale objects, and true `global->project->component->task` hierarchy — are not solved by any existing MCP memory server and would need custom design regardless of which (if any) of these projects is used as a starting point.

---

## Source list

- https://github.com/basicmachines-co/basic-memory
- https://docs.basicmemory.com/concepts/knowledge-format
- https://docs.basicmemory.com/concepts/memory-urls
- https://docs.basicmemory.com/concepts/projects-and-folders
- https://docs.basicmemory.com/start-here/what-is-basic-memory
- https://www.pulsemcp.com/servers/basicmachines-memory
- https://mcpmarket.com/server/basic-memory
- https://github.com/akitaonrails/ai-memory/blob/main/docs/research-basic-memory.md (secondary/unverified detail on sync internals)
- https://github.com/modelcontextprotocol/servers/tree/main/src/memory
- https://www.npmjs.com/package/@modelcontextprotocol/server-memory
- https://github.com/doobidoo/mcp-memory-service
- https://raw.githubusercontent.com/doobidoo/mcp-memory-service/main/README.md
- https://github.com/doobidoo/mcp-memory-service/wiki/06-Development-Reference
- https://glama.ai/mcp/servers/@doobidoo/mcp-memory-service/blob/f94a1f2c592bcd169cdb77e85f2948c9519f8ce6/CONSOLIDATION_TEST_RESULTS.md
- https://codeberg.org/doobidoo/mcp-memory-service (mirror)
- https://github.com/mkreyman/mcp-memory-keeper
- https://www.pento.ai/blog/a-year-of-mcp-2025-review
- https://en.wikipedia.org/wiki/Model_Context_Protocol
- https://chatforest.com/guides/mcp-ecosystem-2026-state-of-the-standard/
- https://andrew.ooo/answers/mcp-model-context-protocol-enterprise-adoption-july-2026/
- https://modelcontextprotocol.io/specification/2025-06-18/basic/transports
- https://www.digitalapplied.com/blog/mcp-tool-use-vocabulary-reference-guide-2026
- https://demiliani.com/2025/09/04/model-context-protocol-and-the-too-many-tools-problem/
- https://medium.com/@ckekula/model-context-protocol-mcp-and-its-limitations-4d3c2561b206
- https://mitek99.medium.com/mcps-overengineered-transport-and-protocol-design-f2e70bbbca62
- GitHub REST API queries against `basicmachines-co/basic-memory`, `doobidoo/mcp-memory-service`, `mkreyman/mcp-memory-keeper`, `modelcontextprotocol/servers` (stars/forks/issues/contributors/releases), queried 2026-08-23.
