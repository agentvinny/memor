# Mem0 and LangMem — Research Findings

Research date: 2026-08-23. All claims below are cited to source; where sources conflicted
(this happens a lot with Mem0, whose architecture has changed multiple times through 2025-2026),
both figures/claims are given rather than picking one.

---

## Overview

**Mem0** (mem0.ai, github.com/mem0ai/mem0, Apache-2.0, ~59-63K GitHub stars) is a
purpose-built "memory layer for AI agents": an SDK + optional managed backend that sits
between an LLM application and a vector store (plus, optionally, a graph store), extracting,
deduplicating and retrieving facts from **conversation turns the calling application feeds
it**. It ships as an open-source Python/TS package plus a paid managed Platform.
[github.com/mem0ai/mem0](https://github.com/mem0ai/mem0), [docs.mem0.ai/platform/platform-vs-oss](https://docs.mem0.ai/platform/platform-vs-oss)

**LangMem** (github.com/langchain-ai/langmem, MIT) is LangChain/LangGraph's memory *toolkit*,
not a standalone database. It is a set of extraction/consolidation functions and prompt
utilities ("memory managers", "prompt optimizers") plus tool wrappers that write into
whatever storage backend you plug in — most naturally LangGraph's `BaseStore`. The
core extraction logic is storage-agnostic, but the parts that make it a usable *system*
(cross-thread persistence, memory-as-tool for an agent, background/hot-path consolidation
loops) are built assuming a LangGraph runtime.
[langchain-ai.github.io/langmem/concepts/conceptual_guide](https://langchain-ai.github.io/langmem/concepts/conceptual_guide/), [github.com/langchain-ai/langmem](https://github.com/langchain-ai/langmem)

Both are fundamentally **conversational-memory** systems: something that talks to an LLM
feeds them message turns, and they extract "facts about the user/agent" from that stream.
Neither ingests git commits, shell history, docker/systemd events, or other non-conversational
environmental evidence natively — that would have to be turned into synthetic "conversation"
text by the calling application first (see Gaps section).

---

## How They Work

### Mem0

- **Scoping model**: memories are tagged at write time with up to four/five identifier
  dimensions — `user_id`, `agent_id`, `run_id` (session), and `app_id`/`org_id`. These are
  flat, independent tags rather than a true nested tree; "hierarchy" is simulated by how you
  choose to combine tags and by null-scoping rules (passing only `user_id` implicitly
  restricts to records where the other fields are null; `"*"` wildcards mean "any non-null
  value"). There is no first-class arbitrary "project → component → sub-component" nesting —
  you'd have to encode that yourself inside one of the four tag fields (e.g. jam
  `project:component` into `agent_id` or a custom metadata field) rather than getting native
  multi-level scope resolution. [docs.mem0.ai/platform/features/entity-scoped-memory](https://docs.mem0.ai/platform/features/entity-scoped-memory)
- **Pipeline**: incoming turns go through an LLM-based extraction step that pulls candidate
  facts, then a "self-editing" step that decides to ADD / UPDATE / DELETE / NOOP against
  existing memories to avoid duplication. Retrieval is multi-signal: semantic (vector),
  BM25 keyword, and (as of the 2026 rework) entity matching, fused into one ranked result set.
  [mem0.ai/blog/state-of-ai-agent-memory-2026](https://mem0.ai/blog/state-of-ai-agent-memory-2026)
- **Graph mode — evolving and inconsistent across sources**: The original Mem0 architecture
  (arXiv 2504.19413, "Mem0g") built an explicit directed labeled graph with typed
  relationship triplets (source, relation, destination) and could target Neo4j, Memgraph,
  Kuzu, Neptune, or Apache AGE. Sources indicate that in the **v3 pipeline (~April 2026) the
  standalone graph-memory module was removed from the open-source core** and replaced by
  lighter-weight "entity linking" (spaCy-based entity extraction into a parallel vector
  collection, with edges inferred purely from **co-occurrence, not declared/typed
  relationships**). Meanwhile, current **Platform** docs still market a "Graph Memory"
  feature ("native, always-on graph over extracted entities... connections feed directly
  into ranking") as a **Pro-tier ($249/mo), managed-only** capability. Net: what "graph
  memory" means in Mem0 depends on which tier and which release you're looking at, and even
  the more sophisticated variants extract entities (people/places/orgs/concepts) linked by
  co-occurrence — **no source describes typed relations like "manages" or "depends-on"**,
  and **no source describes the graph carrying time-validity or provenance/evidence
  metadata**. [docs.mem0.ai/platform/features/graph-memory](https://docs.mem0.ai/platform/features/graph-memory), [docs.mem0.ai/platform/platform-vs-oss](https://docs.mem0.ai/platform/platform-vs-oss), [arxiv.org/html/2504.19413v1](https://arxiv.org/html/2504.19413v1)
- **Temporal reasoning**: the 2026 blog claims a "+29.6 point" improvement on temporal
  reasoning benchmarks via "memory decay" (recent memories weighted up, stale ones down) and
  timestamp-matching at query time — but this is a *ranking heuristic*, not an explicit
  valid-from/valid-until field on facts, and Memory Decay / Temporal Reasoning boosting are
  listed as **Platform-only (v3)** features absent from OSS.
  [mem0.ai/blog/state-of-ai-agent-memory-2026](https://mem0.ai/blog/state-of-ai-agent-memory-2026), [docs.mem0.ai/platform/platform-vs-oss](https://docs.mem0.ai/platform/platform-vs-oss)
- **Provenance/attribution**: Mem0 tags messages with `name`/`user_id` vs `agent_id` so it
  can distinguish user-stated facts from agent-generated inferences in group-chat scenarios.
  That is actor-attribution, not evidentiary provenance (no link back to a source document,
  commit, log line, or "confidence" score is described anywhere in the docs or blog reviewed).
  [mem0.ai/blog/state-of-ai-agent-memory-2026](https://mem0.ai/blog/state-of-ai-agent-memory-2026)
- **Self-hosted vs managed**: Core `mem0ai` package is Apache-2.0 and free; self-hosting
  requires you to bring/operate your own vector store (25+ supported), LLM provider (18+),
  and embedder (11+); you pay only the LLM/embedding API costs (reported ~$5-7/mo infra on
  something like Railway, plus token costs). The **managed Platform** (free tier up to 10K
  memories/mo, then $19 Starter / $79 Growth / $249 Pro) is what unlocks Graph Memory, Memory
  Decay, Temporal Reasoning boosts, "Dream" background consolidation, webhooks, export, batch
  ops, and SOC 2 Type II / HIPAA(enterprise) compliance. OSS gives you vector+KV memory with
  the self-editing dedup pipeline, but not the graph/temporal/consolidation extras.
  [docs.mem0.ai/platform/platform-vs-oss](https://docs.mem0.ai/platform/platform-vs-oss), [vectorize.io/articles/mem0-alternatives](https://vectorize.io/articles/mem0-alternatives)
- **Integration**: REST API, Python/TS SDKs, and an official MCP server (`mem0-mcp`,
  also multiple community MCP wrappers) exposing tools like `add_memory`, `search_memories`,
  `update_memory`, `delete_memory` over stdio/SSE — usable from Claude Desktop, Cursor, etc.
  Multi-tenant isolation via the scoping tags is the multi-agent story.
  [docs.mem0.ai/platform/mem0-mcp](https://docs.mem0.ai/platform/mem0-mcp), [mem0.ai/blog/introducing-openmemory-mcp](https://mem0.ai/blog/introducing-openmemory-mcp)

### LangMem

- **Standalone-ness**: the extraction/consolidation *functions* (`create_memory_manager`,
  prompt optimizers) don't hard-depend on a specific store and can technically be called
  against any storage you wire up yourself. But the practically-usable form — memory tools an
  agent calls mid-conversation, background "reflection" jobs, cross-thread persistence — is
  built on and documented against **LangGraph's `BaseStore`/checkpointer** primitives. Third-
  party comparison (Atlan) calls it "tightly coupled to LangChain/LangGraph — standalone use
  is impractical" — i.e. LangMem is a memory *layer for LangGraph agents*, not a portable
  memory service you'd run independent of that ecosystem.
  [langchain-ai.github.io/langmem/concepts/conceptual_guide](https://langchain-ai.github.io/langmem/concepts/conceptual_guide/), [atlan.com/know/best-ai-agent-memory-frameworks-2026](https://atlan.com/know/best-ai-agent-memory-frameworks-2026/)
- **Memory types**: Semantic (facts, as free-form "collections" or strict-schema
  "profiles"), Episodic (successful past interactions stored as few-shot examples, including
  the reasoning that made them succeed), Procedural (behavioral rules that evolve — starts as
  a system prompt and gets refined from feedback). No dedicated "decision + rationale" object
  type, but episodic memory's "situation / thought-process / why it worked" triple is the
  closest analog and does capture rationale for a specific past action.
  [langchain-ai.github.io/langmem/concepts/conceptual_guide](https://langchain-ai.github.io/langmem/concepts/conceptual_guide/)
- **Temporal facts / provenance**: no built-in temporal-validity modeling (no valid-from/
  valid-until, no "supersedes" chain) and no provenance/evidence fields; comparison sources
  explicitly note "no temporal reasoning native to the architecture" and audit logging "depends
  on backend configuration" (i.e., you'd have to build it into whatever DB you plugged in).
  [atlan.com/know/best-ai-agent-memory-frameworks-2026](https://atlan.com/know/best-ai-agent-memory-frameworks-2026/)
- **Consolidation**: two extraction modes — "hot path" (updates memory synchronously during
  the conversation, adds latency, good for must-have-now context) and "background/
  subconscious" (post-hoc reflection, no latency cost, better for deep pattern extraction).
  Consolidation reconciles new info against existing memory via insert/update/delete, but
  unlike Mem0 there's no built-in dedup guarantee — "no built-in deduplication; custom
  implementation required" per third-party review.
  [langchain-ai.github.io/langmem/concepts/conceptual_guide](https://langchain-ai.github.io/langmem/concepts/conceptual_guide/), [atlan.com/know/best-ai-agent-memory-frameworks-2026](https://atlan.com/know/best-ai-agent-memory-frameworks-2026/)
- **Storage/entities/graph**: flat key-value/document items with vector search over whatever
  store you choose (FAISS/Chroma/Postgres+pgvector/MongoDB, etc.); no entity extraction, no
  relationship graph, no multi-hop traversal.
  [dev.to comparison, "Mem0 vs Zep vs LangMem vs MemoClaw"](https://dev.to/anajuliabit/mem0-vs-zep-vs-langmem-vs-memoclaw-ai-agent-memory-comparison-2026-1l1k)
- **License / cost**: MIT, fully open source, no managed-cloud tier to compare against — you
  run it entirely inside your own LangGraph deployment and pay only for your own storage/LLM
  usage. [github.com/langchain-ai/langmem](https://github.com/langchain-ai/langmem)
- **Integration**: `pip install langmem`, Python-only, designed to be called from LangGraph
  agent code as tools/functions. No official MCP server was found for LangMem itself in this
  research (searches turned up general MCP+LangChain tutorials, not a LangMem-specific MCP
  server) — MCP exposure would have to be hand-built by wrapping LangMem calls in your own
  MCP server. [github.com/langchain-ai/langmem](https://github.com/langchain-ai/langmem)

---

## Benchmark Data (LongMemEval / LoCoMo) — and whether it's the right benchmark

- **LongMemEval** (arXiv 2410.10813) evaluates chat assistants on long-term *interactive*
  memory across five abilities: information extraction, multi-session reasoning, temporal
  reasoning, knowledge updates, and abstention, via 500 questions in categories like
  single-session recall, preference recall, and multi-session recall. It is explicitly a
  **conversational QA benchmark**: given a long chat history, can the system answer questions
  a user might ask about earlier turns. [arxiv.org/abs/2410.10813](https://arxiv.org/abs/2410.10813)
- **LoCoMo** is a similar long-conversation-memory benchmark (very long simulated dialogues,
  QA over them).
- Mem0's own blog reports **92.5 on LoCoMo** and **94.4 on LongMemEval** for its 2026
  token-efficient retrieval algorithm, with "+29.6 temporal reasoning" and "+23.1 multi-hop"
  gains and ~4x lower token cost than full-context baselines.
  [mem0.ai/blog/state-of-ai-agent-memory-2026](https://mem0.ai/blog/state-of-ai-agent-memory-2026), [mem0.ai/research](https://mem0.ai/research)
- A **third-party** source (Vectorize.io) cites a very different number — **Mem0 at 49.0% on
  LongMemEval**, "substantially lower than alternatives like Hindsight at 94.6%" — i.e. an
  independent evaluation puts Mem0 roughly half of its own self-reported figure. This kind of
  large self-reported-vs-third-party gap is common in this space (different harness configs,
  different Mem0 version/tier, or cherry-picked settings) and should be treated as a red flag
  for taking vendor benchmark claims at face value.
  [vectorize.io/articles/mem0-alternatives](https://vectorize.io/articles/mem0-alternatives)
- **Is LongMemEval/LoCoMo the right benchmark for this brief?** No, not fully. Both
  benchmarks measure "can the assistant recall/reason over things a *user said in a chat*
  many turns ago" — a conversational-personalization task. The brief's use case (durable
  project/environmental memory: config drift, decisions+rationale, git/system state history,
  cross-agent/cross-session facts about *infrastructure*, not about a chat partner's stated
  preferences) is a different problem: the "facts" originate from tool output, logs, and
  system state, not dialogue, and the query patterns are things like "what changed and why,
  and is this still true" rather than "what did the user tell you three sessions ago." A high
  LongMemEval score says little about whether a system can, e.g., correctly track that a
  Postgres version was upgraded on 2026-06-01 and supersede the prior "running Postgres 14"
  fact with provenance pointing at the actual migration commit. No benchmark reviewed here
  measures that.

---

## Scoring vs the 6 Categories

### Mem0

| Category | Score (1-5) | Notes |
|---|---|---|
| 1. Capture | **1.5** | Only ingests what the calling app feeds it as "messages" (user_id/agent_id/run_id tagged text). No native git/shell/docker/systemd/log connectors found anywhere in docs, GitHub, or third-party reviews. Multi-agent capture is supported only in the sense that different `agent_id`/`app_id` tags can write to the same store. |
| 2. Representation | **2.5** | Flat 4-tag scoping (not a true arbitrary hierarchy); entity extraction exists but relationships are inferred co-occurrence, not typed/declared; no time-validity fields on facts (only ranking-time recency weighting, and that's Platform-only); actor-attribution (user vs agent) exists but no formal provenance/evidence links; no confidence scores documented; no decision+rationale object type. |
| 3. Retrieval | **3** | Real strength area: vector + BM25 + entity multi-signal fusion, scope filtering via the tag system, some temporal ranking boost (Platform only). No genuine multi-hop graph traversal query API surfaced in docs (the graph is used for ranking, not exposed as a traversable query interface); no progressive-disclosure retrieval pattern described. |
| 4. Consolidation | **3.5** | Best category: LLM-based extraction plus an explicit ADD/UPDATE/DELETE/NOOP self-editing step for dedup; "Dream" background consolidation for pattern-merging (Platform-only). Raw evidence/original-turn retention is not clearly documented as preserved once facts are extracted. |
| 5. Integration | **3** | REST API, Python/TS SDK, official + community MCP servers, multi-tenant scoping tags support multiple agents/apps writing to a shared store. No native filesystem/DB/event-stream ingestion adapters (CDC, systemd journal, docker events) found. |
| 6. Reliability | **2** | Managed Platform has SOC2/HIPAA compliance, but that's about data handling, not memory auditability. No documented human-readable audit trail of "why was this fact stored/changed," no rollback/versioning of superseded facts, no explicit provenance inspection UI/API found. Being Apache-2.0 + self-hostable is a plus for "survives agent replacement" (it's infrastructure, not tied to one agent framework). |

### LangMem

| Category | Score (1-5) | Notes |
|---|---|---|
| 1. Capture | **1** | Purely conversational; explicitly conversation-turn driven; no environmental/system connectors of any kind found; multi-agent capture would require you to build your own turn-attribution scheme. |
| 2. Representation | **2** | Three memory *types* (semantic/episodic/procedural) is a genuine, useful conceptual model, and episodic memory's situation/reasoning/outcome triple approximates decision+rationale for a single interaction — but no hierarchy/scoping primitive beyond whatever keys you invent in your own store, no entity/relationship modeling at all, no temporal-validity fields, no provenance, no confidence scores. |
| 3. Retrieval | **2** | Whatever the underlying vector/KV store supports (semantic search); no graph traversal (no graph exists), no native temporal queries, no scope-filtering primitive beyond your own store's query language, no progressive disclosure pattern documented. |
| 4. Consolidation | **2** | Hot-path vs background extraction is a nice design distinction, but "no built-in deduplication; custom implementation required" per independent review — meaning contradiction/stale-fact handling is left to the developer, not provided out of the box. |
| 5. Integration | **2** | Python SDK, tight LangGraph coupling, no REST API of its own, no official MCP server found (would need custom wrapping), not designed for consumption by "any future agent" — designed for consumption by *your LangGraph agent*. |
| 6. Reliability | **1.5** | No compliance certs, no documented audit/rollback/provenance-inspection tooling; auditability entirely depends on what backend store you chose and how you instrumented it; "survives agent replacement" is weak because the whole point of LangMem is that it's the memory layer *for* a LangGraph-style agent — swapping harness/orchestrator likely means re-plumbing the store integration. |

(Scores are relative, qualitative judgments assembled from the cited sources, not scores
either vendor publishes themselves.)

---

## Maturity / License / Effort

| | Mem0 | LangMem |
|---|---|---|
| License (core) | Apache 2.0 | MIT |
| GitHub stars | ~59-63K | not found in this research (smaller, LangChain-ecosystem project) |
| Managed offering | Yes — free tier (10K memories/mo) through $19/$79/$249+ tiers, SOC2/HIPAA | None — pure OSS, self-run inside your own LangGraph deployment |
| Self-host effort | Moderate: `pip install mem0ai`, but you must provision/operate a vector store, an LLM provider, and an embedder yourself (25+/18+/11+ options respectively); reported minimal infra cost ~$5-7/mo (e.g. Railway) plus LLM token costs | Low-to-moderate to install (`pip install langmem`), but real effort is standing up a LangGraph app with a `BaseStore` backend (Postgres/Mongo/vector DB) — effort scales with how much LangGraph plumbing you already have |
| Feature gating | Significant: Graph Memory, Memory Decay, Temporal Reasoning boost, "Dream" consolidation, webhooks, export, batch ops are Platform/Pro-only, not in OSS | None — everything is open source, but "everything" is a fairly thin extraction/consolidation toolkit, not a full memory database |

---

## Gaps vs the Brief's Vision

The brief wants: capture of external activity (terminals/git/system/services/multiple
agents/manual edits), rich representation (hierarchy, entities, relationships, time,
provenance, confidence, decisions+rationale), rich retrieval (hybrid/graph/temporal/scope/
progressive-disclosure/multi-hop), real consolidation (dedup, contradiction/staleness
handling, raw-evidence retention), broad integration (API/MCP/CLI/SDK/filesystem/DB/event
stream, multi-agent), and reliability (inspectable, auditable, correctable, rollback,
survives agent replacement).

Neither product was built for this. Concretely:

- **No environmental capture.** Neither Mem0 nor LangMem has any adapter for git, shell
  history, docker, systemd, or file-system state. Both assume a chat-shaped input stream;
  making them capture "the project's configuration state changed" requires the calling
  application to synthesize a fake conversational turn describing the change, which is a
  workaround, not native support.
- **No time-validity or provenance model.** Mem0's "temporal reasoning" is retrieval-time
  ranking (recency-weighted scoring), not a stored valid-from/valid-until/superseded-by
  structure on facts, and it's gated to the paid Platform tier anyway. LangMem has no
  temporal model at all. Neither system stores "this fact came from evidence X, with
  confidence Y" — Mem0 stores actor-attribution (user vs agent said it), which is a much
  weaker guarantee than evidentiary provenance.
  [docs.mem0.ai/platform/platform-vs-oss](https://docs.mem0.ai/platform/platform-vs-oss)
- **No genuine relationship graph.** Mem0's graph feature (where it still exists, Platform-
  only) links memories by entity co-occurrence, not typed/declared relationships — it cannot
  express "service A depends on service B" as a queryable typed edge. LangMem has no graph at
  all.
- **Hierarchy is shallow and fixed-shape.** Mem0's scoping is four flat tags
  (user/agent/session/app), not an arbitrary nestable tree — a "project → subsystem →
  component" hierarchy would have to be flattened/encoded into those tags by the caller.
  LangMem has no scoping primitive of its own.
- **No decision+rationale as a first-class object.** LangMem's episodic memory gets partway
  there (situation/reasoning/outcome for a single successful interaction) but it's designed
  as a few-shot example for prompting, not a durable "why we chose X over Y" record queryable
  independent of the agent's next response.
  [langchain-ai.github.io/langmem/concepts/conceptual_guide](https://langchain-ai.github.io/langmem/concepts/conceptual_guide/)
- **Weak auditability/reliability.** Neither system exposes a clear "show me the evidence
  and history behind this fact" API/UI; Mem0's compliance certs (SOC2/HIPAA) are about data
  handling, not about memory correctness/audit. Contradiction handling exists in Mem0 (ADD/
  UPDATE/DELETE/NOOP) but is opaque (LLM-decided) rather than inspectable/reversible with a
  clear rollback path.
- **Tool/harness independence is only partial.** Mem0 is genuinely tool-agnostic (REST API +
  MCP server, any LLM framework can call it) — this is its strongest alignment with the
  brief. LangMem is the opposite: multiple independent comparisons describe it as "tightly
  coupled to LangChain/LangGraph" such that "standalone use is impractical," which directly
  conflicts with the brief's "tool/harness-agnostic" requirement.
  [atlan.com/know/best-ai-agent-memory-frameworks-2026](https://atlan.com/know/best-ai-agent-memory-frameworks-2026/)

---

## Verdict

Both Mem0 and LangMem are **conversational-personalization memory tools**, not
**environmental/project memory systems**, and the mismatch with this brief is real, not
cosmetic. Their entire pipeline — capture, extraction, scoping, retrieval, benchmarks used to
validate them (LongMemEval/LoCoMo) — is built around "what did a human say to an assistant
across many chat sessions," not "what is true about a developer's systems, why did it become
true, and how confident/current is that belief." Neither ingests git/shell/docker/systemd
events natively; neither models time-validity or evidentiary provenance as first-class data;
neither supports a true arbitrary hierarchical scope; neither treats "decision + rationale"
as a durable, independently queryable record. Mem0 is the stronger of the two on integration
(REST/MCP/multi-tenant tags, Apache-2.0, real managed option) and has *some* entity/relation
and dedup machinery, but its most relevant capabilities (graph, temporal ranking,
consolidation/"Dream") are paywalled to its Platform tier, and even those don't reach
provenance or time-validity as the brief defines them. LangMem is architecturally the
weaker fit for this brief specifically because it is not tool/harness-agnostic — it is a
LangGraph-agent memory toolkit, and standalone/cross-harness use is explicitly called
impractical by independent reviewers. If the brief's system is meant to be a durable,
agent-agnostic ground truth about a developer's actual technical environment, neither product
should be adopted as-is; at most, Mem0's storage/retrieval/dedup primitives could be borrowed
or used as one ingestion backend behind a purpose-built capture/provenance/temporal layer that
neither project provides.

---

## Sources

- [github.com/mem0ai/mem0](https://github.com/mem0ai/mem0)
- [github.com/mem0ai/mem0/blob/main/README.md](https://github.com/mem0ai/mem0/blob/main/README.md)
- [github.com/mem0ai/mem0/blob/main/LICENSE](https://github.com/mem0ai/mem0/blob/main/LICENSE)
- [docs.mem0.ai/platform/platform-vs-oss](https://docs.mem0.ai/platform/platform-vs-oss)
- [docs.mem0.ai/platform/features/graph-memory](https://docs.mem0.ai/platform/features/graph-memory)
- [docs.mem0.ai/platform/features/entity-scoped-memory](https://docs.mem0.ai/platform/features/entity-scoped-memory)
- [docs.mem0.ai/platform/mem0-mcp](https://docs.mem0.ai/platform/mem0-mcp)
- [mem0.ai/blog/state-of-ai-agent-memory-2026](https://mem0.ai/blog/state-of-ai-agent-memory-2026)
- [mem0.ai/blog/ai-memory-benchmarks-in-2026](https://mem0.ai/blog/ai-memory-benchmarks-in-2026)
- [mem0.ai/blog/introducing-openmemory-mcp](https://mem0.ai/blog/introducing-openmemory-mcp)
- [mem0.ai/research](https://mem0.ai/research)
- [arxiv.org/html/2504.19413v1 — Mem0: Building Production-Ready AI Agents with Scalable Long-Term Memory](https://arxiv.org/html/2504.19413v1)
- [arxiv.org/abs/2410.10813 — LongMemEval](https://arxiv.org/abs/2410.10813)
- [vectorize.io/articles/mem0-alternatives](https://vectorize.io/articles/mem0-alternatives)
- [atlan.com/know/best-ai-agent-memory-frameworks-2026](https://atlan.com/know/best-ai-agent-memory-frameworks-2026/)
- [dev.to/anajuliabit — Mem0 vs Zep vs LangMem vs MemoClaw](https://dev.to/anajuliabit/mem0-vs-zep-vs-langmem-vs-memoclaw-ai-agent-memory-comparison-2026-1l1k)
- [langchain-ai.github.io/langmem/concepts/conceptual_guide](https://langchain-ai.github.io/langmem/concepts/conceptual_guide/)
- [github.com/langchain-ai/langmem](https://github.com/langchain-ai/langmem)
- [docs.langchain.com/oss/python/concepts/memory](https://docs.langchain.com/oss/python/concepts/memory)

Note: a targeted search for a "particula.tech" comparison of Mem0/LangMem did not surface a
matching page in this research session; the comparison sources actually used instead are
Atlan, Vectorize.io, and a 2026 DEV Community multi-tool comparison, as listed above.
