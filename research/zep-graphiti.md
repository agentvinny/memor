# Zep / Graphiti — Research Findings

Researched: 2026-08-23. Sources cited inline; full list at bottom.

## Overview

**Graphiti** (github.com/getzep/graphiti, Apache-2.0, ~28.9K GitHub stars) is an open-source Python
framework for building **temporal knowledge graphs** for AI agents. It is the engine underneath
**Zep**, a commercial product that in 2026 rebranded from "agent memory platform" to a
**"Context Engineering Platform"** — a positioning shift (citing Karpathy/Shopify's Tobi Lütke)
toward "designing what information agents receive," not just storing it. Graphiti itself remains
a separate, independently self-hostable OSS project; Zep Cloud is the managed product built on it.

Critical fact for self-hosters: **Zep Community Edition (the self-hostable Zep server) has been
deprecated.** If you want to self-host today, you no longer stand up "Zep" — you stand up
**Graphiti directly** against your own graph database (Neo4j / FalkorDB / Amazon Neptune), and
lose Zep's higher-level conveniences (hosted APIs, dashboards, SOC2/HIPAA compliance, managed
scaling). (vectorize.io, tracxn.com, getzep.com)

## How It Works

**Data model** (DeepWiki, help.getzep.com):
- `EpisodicNode` — an episode is the raw ingestion unit (a node in the graph itself) storing the
  original input text/JSON/message. Episodes are the **ground-truth/provenance layer**.
- `EntityNode` — extracted entities (people, systems, concepts), with optional developer-defined
  Pydantic types ("prescribed ontology") or LLM-inferred types ("learned ontology").
- `EntityEdge` — a **fact** as a (subject, predicate, object) triplet, carrying the fact text and
  its own embedding, plus temporal metadata.
- `CommunityNode` — clusters of related entities (built via `build_communities`), giving a
  lightweight hierarchical/aggregation layer on top of the flat entity graph.
- `MENTIONS` edges link episodes → entities, giving direct **provenance**: every entity/fact can
  be traced back to the specific episode (message/document/JSON blob) that produced it, and
  `get_episode_entities` / episode UUIDs surface this lineage explicitly.

**Temporal ("bi-temporal") model** — every `EntityEdge` carries four timestamps:
- `created_at` — when Graphiti ingested the data
- `valid_at` — when the fact became true in the real world (event time)
- `invalid_at` — when the fact was superseded by contradicting new information
- `expired_at` — internal/system time for logical deletion/versioning

When a new episode contradicts an existing fact, Graphiti does **not delete** the old edge — it
sets `invalid_at` on it (temporal edge invalidation), preserving full history so the graph can
answer both "what's true now" and "what was true at time T." This is Graphiti's core "supersedes"
mechanism — it's implicit (an invalidated edge is superseded by whichever edge caused the
invalidation) rather than an explicit typed `SUPERSEDES` relationship, but the audit trail is
intact. (blog.getzep.com "Graphiti: Give Every Fact... an Expiration Date"; help.getzep.com core
concepts; FalkorDB blog)

**Scoping** — `group_id` is present on every node and edge and is the sole mechanism for
partitioning data within one graph/database instance (multi-tenant isolation: per-user, per-project,
per-org). This is **flat namespacing, not a true hierarchy** — there's no native
global→project→component→task tree; you'd have to encode that structure yourself (e.g.,
`group_id = "org/project/component"` string convention, or via community nodes as a soft
grouping layer). DeepWiki's "Multi-Database and Multi-Tenancy" page and help.getzep.com's "Graph
Namespacing" page confirm `group_id` is the only built-in partition primitive; if unset, the
Graphiti MCP server defaults to `group_id="main"`.

**Retrieval** — `Graphiti.search()` (and the lower-level `_search()` with a `SearchConfig`) runs a
genuinely **hybrid** pipeline, not a single strategy:
- Semantic search (vector similarity on entity/edge embeddings)
- Keyword/full-text search (BM25) on names and fact summaries
- Graph traversal (BFS from a center/focal node)
- Reranking: Reciprocal Rank Fusion (RRF), node-distance reranking (weights results by graph
  proximity to a focal node), episode-mentions reranking, maximal marginal relevance, and
  cross-encoder reranking
- Predefined "recipes" (e.g., `NODE_HYBRID_SEARCH_RRF`) bundle these into ready-made configs
- Facts can be filtered by date range (`search_memory_facts` supports temporal filtering) and by
  `group_id`

For a query like **"why does this run on port 8080"**: Graphiti would do semantic+BM25 retrieval
over fact/entity text to find edges mentioning "port 8080" or the relevant service, then graph-
traverse from that entity to connected facts/decisions (e.g., a config entity, a "decided because"
relationship, or the episode that introduced it), then use the episode/`MENTIONS` link to surface
the original source text (a Slack message, doc, or config file content) as evidence, and
`valid_at`/`invalid_at` to distinguish the current binding from a previous one (e.g., "was 3000
until 2026-05, changed to 8080"). This works **only if** the "why" (rationale) was itself ingested
as episode text — Graphiti has no dedicated "decision + rationale" schema; rationale would just be
whatever prose was in the source episode, extracted as a generic fact/entity if the LLM extractor
judged it salient. There's no first-class `Decision` or `Rationale` entity type, no confidence
score field on facts (searched explicitly — not found in any Graphiti-specific source), and no
explicit `SUPERSEDES` edge type (only implicit via `invalid_at`).

**Ingestion is text/JSON/message only, no native environmental connectors.** Graphiti supports
exactly three `episode_type`s: `text` (unstructured), `message` (`speaker: content` conversational
turns), and `json` (structured data). There is no built-in git/shell/docker/systemd/filesystem
watcher — an external application (or the MCP server's `add_memory`/`add_triplet` tools) must push
each event/fact in as text or JSON. It's a generic "episodic ground truth" ingestion API, not a
capture layer. Nothing in the docs, GitHub repo, or MCP server README suggests native OS/tooling
connectors; that plumbing is entirely the caller's responsibility. (help.getzep.com "Adding
Episodes"; mcp_server/README.md)

**MCP server**: getzep/graphiti ships an MCP server (`mcp_server/`) exposing `add_memory` (episode
ingestion with `reference_time`, `excluded_entity_types`, `custom_extraction_instructions`,
`previous_episode_uuids`), `add_triplet` (direct fact insertion without LLM extraction),
`search_nodes`, `search_memory_facts` (with date filters), `delete_episode`,
`delete_entity_edge`, `get_entity_edge`, `get_episode_entities`, `build_communities`, and
`summarize_saga`. It runs via Docker Compose in two flavors: FalkorDB-combined (single container,
default/simplest) or Neo4j-separate (Neo4j 5.26+ container + MCP server container), served over
HTTP at `/mcp/` (not stdio-only), so multiple agents/tools can consume the same graph concurrently.

**Self-hosting infra**: Neo4j 5.26+ (or FalkorDB, or Amazon Neptune + OpenSearch Serverless; Kuzu
support is deprecated), Python 3.10+, and an LLM API key (OpenAI by default, since entity/fact
extraction and embeddings both go through an LLM call on every episode). Neo4j **Community
Edition is sufficient** for a single-instance self-hosted deployment (scales to 512GB RAM per
Neo4j's own docs) — Enterprise Edition only adds clustering/HA/backup/faster Cypher, none of which
are required to run Graphiti solo. Minimal viable deployment: one Neo4j (or FalkorDB) container +
one Graphiti/MCP-server container + an LLM API key — genuinely lightweight to stand up, but every
ingested episode costs an LLM call for extraction, so cost/latency scale with ingestion volume,
not just query volume.

## Scoring vs the Brief's 6 Categories

**1. Capture — Weak (not its job).** No native connectors for terminals, git, shell, docker,
systemd, or filesystem events. Everything must be pushed in as text/JSON/message episodes by an
external agent or script. Multiple agents *can* write to the same graph (via MCP, group_id-scoped),
so it supports multi-agent *writes*, but it does not observe anything on its own. This is purely an
ingestion API sitting downstream of whatever capture layer you build.

**2. Representation — Strong on time/provenance, weak on hierarchy/decisions/confidence.**
Excellent bi-temporal fact model (`created_at`/`valid_at`/`invalid_at`/`expired_at`), first-class
provenance via episode nodes + `MENTIONS` edges, and a real entity-relationship graph (not just
flat vector chunks). Community nodes give a soft clustering layer. But: scoping is flat
(`group_id` namespace only — no native global→project→component→task tree), there's no dedicated
`Decision`/`Rationale` entity type (would ride on generic entity/fact extraction), and there is no
confidence-score field anywhere in the schema (confirmed absent across all sources checked).

**3. Retrieval — Strong.** Genuinely hybrid: BM25 keyword + vector semantic + graph traversal, with
multiple reranking strategies (RRF, node-distance, cross-encoder, episode-mentions, MMR) and
prebuilt recipes. Supports temporal filtering (date-ranged fact search) and scope filtering
(`group_id`). Multi-hop traversal works via node-distance reranking from a focal entity. No
explicit "progressive disclosure" feature (e.g., summary-then-drill-down API), though episode
provenance links let a caller manually drill from fact → source text.

**4. Consolidation — Strong on contradiction/supersession, unclear on dedup.** Fact invalidation
(`invalid_at`) is the headline feature: new contradicting episodes don't overwrite/delete old facts,
they mark them invalid while keeping full history — directly matches the brief's "supersedes" and
"current vs historical" requirements. Raw evidence (the episode) is retained indefinitely alongside
derived facts. Explicit dedicated deduplication logic for near-duplicate entities exists (entity
resolution during extraction) but was not deeply verified in this pass.

**5. Integration — Strong.** Python SDK, REST-ish API via the library, and an official MCP server
(HTTP, not stdio-locked) exposing add/search/delete tools — directly usable by any MCP-aware
agent/harness, which matches the brief's "queryable by ANY future agent via an API/MCP" requirement
well. Also has Go/TS SDKs on the Zep-cloud side (not confirmed for raw Graphiti).

**6. Reliability — Mixed.** Human-inspectable and auditable: it's a real graph database (Neo4j) you
can query directly with Cypher outside of Graphiti's own API, facts carry explicit timestamps, and
provenance is traceable to source episodes — all good for audit/inspection. Correctable: you can
manually add/delete edges and triplets. Rollback: point-in-time queries via `valid_at`/`invalid_at`
effectively give you "what did we believe at time T," which is a soft rollback for reasoning
purposes (though not a literal graph-state revert). Survives agent/harness replacement: yes, since
it's a standalone service any future agent can query — but only for whatever was actually ingested
as text/JSON episodes; nothing else. Operational reliability is weaker than the data-model story:
self-hosting requires you to run and maintain Neo4j/FalkorDB yourself since Zep Community Edition
was deprecated — this is real ongoing ops burden, not "batteries included."

## Maturity / License / Effort

- **License**: Graphiti = Apache-2.0, fully open, no usage restrictions. Zep (the commercial
  cloud/platform) is proprietary/closed-source.
- **Maturity**: ~28.9K GitHub stars, active development, backed by a funded company (Zep AI), used
  as reference architecture in several third-party comparison articles (vectorize.io, FalkorDB,
  Medium deep-dives) — reasonably mature for an "agent memory" library, though the ecosystem overall
  is young (2024–2026).
- **Effort to self-host**: Low to stand up (Docker Compose gives you FalkorDB+MCP-server in one
  container, or Neo4j+MCP-server in two), but ongoing operational effort is real: you own graph DB
  backups/upgrades, LLM API costs scale with every ingested episode (extraction + embeddings are
  LLM calls), and you lose Zep's managed conveniences (SOC2/HIPAA, hosted scaling, dashboards)
  since Zep Community Edition is deprecated.
- **Zep Cloud pricing** (context, not required for self-host path): free tier ~2,500
  messages/month + 2.5MB graph/month, then $125/mo (or $99–104/mo annual) with no mid-tier; credit
  metering is per-episode-size, and retrieval/storage themselves are free of charge.

## Gaps vs the Brief's Vision

- No native hierarchical scope model (global→project→component→task) — only flat `group_id`
  tenant partitioning; you'd have to layer a naming convention or your own entity types on top.
- No native capture of git/shell/docker/systemd/filesystem events — it is purely a downstream
  ingestion+reasoning engine; a separate capture layer is mandatory in any architecture using this.
- No first-class `Decision`/`Rationale` entity types or confidence scoring — decisions+rationale
  and "confidence levels" from the brief would have to be modeled as generic extracted
  facts/entities, with no guaranteed schema consistency.
- "Supersedes" relationship is implicit (via `invalid_at` timestamp), not an explicit typed edge
  you can query as "X supersedes Y" directly, though the practical effect (old-vs-new fact,
  auditable) is achieved.
- Progressive disclosure isn't a built-in retrieval mode; you'd build it yourself on top of the
  episode-provenance links and search recipes.
- Self-hosting maturity has regressed for people who want "Zep" specifically — the deprecation of
  Zep Community Edition means the self-host path is really "become a Graphiti+Neo4j operator,"
  which is a materially bigger commitment than a single `docker run`.

## Verdict

Graphiti is a **strong candidate for the temporal-graph representation and retrieval layer** of the
proposed memory system — specifically the parts of the brief calling out "temporal memory, evolving
facts, provenance, knowledge graphs, current-vs-historical reasoning." Its bi-temporal fact model
(`valid_at`/`invalid_at`), episode-based provenance, and hybrid (BM25 + vector + graph-traversal +
reranking) retrieval directly answer several of the brief's hardest requirements, and it's
genuinely open-source (Apache-2.0) and self-hostable independent of Zep's commercial platform.

It is **not** a capture layer, **not** a hierarchical-scope engine, and **not** a decision/rationale/
confidence framework out of the box — those would need to be built around it (a capture pipeline
feeding structured episodes in, plus an application-level schema/convention for scope hierarchy,
decisions, rationale, and confidence, since Graphiti's own schema is agnostic to all of that beyond
generic entities/facts). It's best understood as the "temporal fact store + retrieval engine" piece
of the puzzle, to be paired with a separate capture/ingestion layer and an application-level schema
that maps the brief's richer semantics (hierarchy, decisions, confidence) onto Graphiti's generic
entity/edge/episode primitives.

## Sources

- https://github.com/getzep/graphiti (repo, license, README)
- https://github.com/getzep/graphiti/blob/main/mcp_server/README.md (MCP server tools, docker compose setups)
- https://help.getzep.com/graphiti/graphiti/overview
- https://help.getzep.com/graphiti/core-concepts/adding-episodes
- https://help.getzep.com/graphiti/core-concepts/graph-namespacing
- https://help.getzep.com/graphiti/getting-started/mcp-server
- https://help.getzep.com/graphiti/working-with-data/searching
- https://blog.getzep.com/graphiti-knowledge-graphs-for-agents/
- https://blog.getzep.com/how-do-you-search-a-knowledge-graph/
- https://medium.com/@pankaj_pandey/graphiti-give-every-fact-in-our-knowledge-graph-an-expiration-date-090f0de93ff2
- https://www.falkordb.com/blog/building-temporal-knowledge-graphs-graphiti/
- https://deepwiki.com/getzep/graphiti (architecture, data model, multi-tenancy)
- https://deepwiki.com/getzep/graphiti/7.3-multi-database-and-multi-tenancy
- https://deepwiki.com/getzep/graphiti/6.2-embedding-and-reranking-services
- https://vectorize.io/articles/mem0-vs-zep (Zep Community Edition deprecation, self-host burden)
- https://www.getzep.com/platform/graphiti/
- https://www.getzep.com/ai-agents/temporal-knowledge-graph/
- https://www.getzep.com/solutions/context-engineering/ (2026 "Context Engineering Platform" rebrand)
- https://atlan.com/know/context-engineering-platforms-comparison/
- https://atlan.com/know/zep-vs-mem0/
- https://neo4j.com/product/community-edition/ (Community vs Enterprise capabilities)
