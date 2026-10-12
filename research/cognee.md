# Cognee — Research Notes

Repo: https://github.com/topoteretes/cognee | Site: https://cognee.ai | Docs: https://docs.cognee.ai

## Overview

Cognee (Topoteretes GmbH, Berlin) is an open-source (Apache-2.0) "AI memory platform" — a
self-hosted library/service that ingests unstructured and semi-structured content, builds a
combined **knowledge graph + vector store**, and exposes it for retrieval by LLM agents via
Python SDK, REST API, MCP server, or CLI. It raised a $7.5M seed (Pebblebed, 42CAP, Vermilion
Ventures, angels from DeepMind/n8n/Snowplow) and has a companion arXiv paper
(2505.24478, "Optimizing the Interface Between Knowledge Graphs and LLMs"). As of Aug 2026:
~29.8–30k GitHub stars (#~1156 global), ~3.0k forks, ~9,800 commits, 102 open issues, 253 PRs,
1.09M+ PyPI downloads (231k in last 30 days) — an actively developed, moderately mature OSS
project with commercial backing (Cognee Cloud is the paid hosted tier).

Sources:
[GitHub](https://github.com/topoteretes/cognee),
[star-history](https://www.star-history.com/topoteretes/cognee/),
[pepy.tech](https://pepy.tech/projects/cognee),
[Seed funding announcement](https://www.cognee.ai/blog/cognee-news/cognee-raises-seven-million-five-hundred-thousand-dollars-seed),
[arXiv paper](https://arxiv.org/pdf/2505.24478)

## How It Works

### ECL Pipeline (Extract → Cognify → Load)

Four operations sit on top of the ECL concept:

1. **Add** — ingests raw content (files, dirs, raw text, URLs, S3 URIs) across 30+ connectors
   (PDFs, Slack, Notion, Google Drive/SharePoint, databases, images via vision models, audio via
   transcription, code repos). Performs dedup and dataset organization.
2. **Cognify** — the core enrichment stage, a six-step pipeline: classify documents → check
   permissions → chunk → LLM entity/relationship extraction → summarize → embed. Optional
   ontology validation / RDF-OWL URI alignment ("ontology grounding") and cross-document
   coreference resolution.
3. **Memify** — post-hoc *enrichment* pass over an already-built graph (see below).
4. **Search** — 14 retrieval modes over the combined graph+vector store (chunk search, entity
   search, graph-completion / decomposition, hybrid 3-channel, "agentic" ReAct-loop retriever,
   temporal, code-graph, etc.).

Graph construction is LLM-driven (entity/relationship extraction quality is bounded by the
underlying LLM) and **incremental**: new data is deduplicated against existing entities (via a
`Dedup`-annotated field) and merged rather than triggering a full rebuild.

Sources:
[dlthub blog](https://dlthub.com/blog/cognee),
[Cognify docs](https://docs.cognee.ai/core-concepts/main-operations/legacy-operations/cognify),
[How Cognee Builds AI Memory](https://www.cognee.ai/blog/fundamentals/how-cognee-builds-ai-memory),
[guancyxx.cn deep dive](https://guancyxx.cn/en/blog/cognee-ai-memory-platform-deep-dive)

### Storage backends (the "graph+vector store")

Three separate stores, each pluggable, all file-based by default (zero external infra needed):

- **Graph store** — default **Kuzu** (embedded); also Neo4j, FalkorDB, Neptune, Memgraph,
  NetworkX (in-memory), Postgres+AGE.
- **Vector store** — default **LanceDB** (embedded); also Qdrant, pgvector, Redis, DuckDB,
  Pinecone, ChromaDB, Turso.
- **Relational store** — default **SQLite**; also PostgreSQL. Holds documents/chunks, dataset
  and user/permission registry, and pipeline run bookkeeping.

Default self-hosted footprint (SQLite + LanceDB + Kuzu, all embedded/file-based) is genuinely
light — `pip install cognee`, no separate DB servers required. A heavier deployment (Docker
Compose with REST API on :8000 and MCP on :8001, Neo4j/Qdrant swapped in) is available via
prebuilt Docker Hub images (`cognee/cognee`, `cognee/cognee-mcp`).

Sources: [How Cognee Builds AI Memory](https://www.cognee.ai/blog/fundamentals/how-cognee-builds-ai-memory), [GitHub README](https://github.com/topoteretes/cognee)

### Data model (DataPoint)

The base unit is `DataPoint`, a Pydantic model with: `id` (UUID), `created_at`/`updated_at`,
`version` (int, starts at 1), `topological_rank`, `metadata` (`index_fields`, `type`,
`identity_fields`), `type`, `ontology_uri`, `belongs_to_set`. Entities, chunks, summaries and
relationships are all `DataPoint`s.

**No confidence or evidence field exists in the base schema.** Versioning is a manual,
**destructive upsert**: you mutate fields, call `update_version()` (bumps `version` and
`updated_at`), then re-`add_data_points()` — this replaces/upserts the existing node rather than
preserving prior versions as distinct historical facts. Provenance (which user/dataset/data item
produced a node) is tracked separately in the relational store's pipeline-run bookkeeping, not as
a per-fact "evidence chain" attached to the graph node itself.

Source: [DataPoints docs](https://docs.cognee.ai/core-concepts/building-blocks/datapoints)

Caveat: several secondary sources (blog aggregator summaries, not primary docs) surfaced claims
of a `PROVENANCE_TRACKING=true` flag, `contradicts` edges with `confidence` scores, and a
`CONTRADICTION_CONFIDENCE_THRESHOLD` setting. These could not be confirmed against the primary
docs.cognee.ai pages or the DataPoint schema fetched directly, and the search results that
produced them were contaminated with unrelated papers (medical-imaging provenance standards,
USPTO patents). Treat these specific claims as **unverified/likely noise** rather than confirmed
Cognee features. What *is* independently corroborated (via the UWyO evidence-graph case study) is
that Cognee can preserve page/section-level citations on ingested documents and expose them at
query time for evidence-grounded answers — a document-citation feature, not a formal
confidence/provenance ontology on every derived fact.

Source: [Evidence graph case study](https://www.cognee.ai/blog/case-studies/evidence-graph-for-uwyo)

### Memify (consolidation)

`.memify` runs **enrichment pipelines on an already-built graph** (requires prior Add+Cognify —
it does not ingest raw data itself). Each pipeline = Extraction stage (pull triplets/chunks/cached
sessions from the graph) + Enrichment stage (LLM or heuristic processing, writes new/updated
nodes+edges back). Known built-in pipelines:

- **Triplet Datapoints / Triplet Embeddings** — index graph triplets for retrieval.
- **Coding Rules** — derives rule associations from code chunks via LLM (LLM-driven fact
  derivation).
- **Session Persistence** — turns cached Q&A sessions into graph nodes.
- **Entity Consolidation** (`consolidate_entity_descriptions.py`) — walks Entity nodes, gathers
  neighbors+edges, and **rewrites the node's `description` field via an LLM, in place**. This is
  the one clearly-documented "overwrite" behavior — the consolidated description replaces the
  prior one on the same node (destructive at the field level); it is not shown to fork off a
  separate "raw evidence" copy.
- **Cross-Connect Entities** — LLM proposes new relationships between previously unconnected
  entities, assigning a `feedback_weight` starting at 0.2 for inferred edges (this is the only
  documented edge-reweighting behavior found).
- **Frequency weighting** (`apply_frequency_weights.py`) — ages/weights knowledge by usage
  frequency (reweighting signal, not deletion).

**No documented automatic pruning of stale/superseded nodes was found** — despite Cognee's own
marketing copy ("prunes stale nodes... reweights edges... adds derived facts") describing Memify
in those terms, the concrete pipelines inspected show reweighting and LLM-driven
description-rewrite/derivation, but not an explicit stale-node garbage-collection pipeline in the
docs consulted. This is a marketing/implementation gap worth flagging: consolidation is real and
LLM-driven, but "pruning" appears aspirational/partial rather than a fully documented mechanism.

Sources: [Memify docs](https://docs.cognee.ai/core-concepts/main-operations/memify), [Commonplace summary](https://zby.github.io/commonplace/sources/cognee-knowledge-engine/), [GitHub memify module](https://github.com/topoteretes/cognee/tree/main/cognee/modules/memify)

### Temporal validity

Cognee has an **opt-in** "Temporal Cognification" feature (`temporal_cognify=True` on
ingestion). When enabled, it extracts events with timestamps/intervals from text and builds an
**event-based graph** where events link to entities via explicit temporal relations
(before/after/during), plus a `SearchType.TEMPORAL` retrieval mode that filters/ranks by time,
working across graph backends without requiring Neo4j. Marketing copy states "facts/relationships
carry a temporal validity window with `valid_from`/`valid_until` properties," but this specific
schema detail could not be independently confirmed in the primary time-awareness docs page fetched
(which describes behavior, not the underlying field schema). Net: temporal modeling exists and is
more than a toy feature, but it is **opt-in, not the default graph-mutation behavior** — outside
of temporal-cognify mode, updates behave as destructive upserts (see DataPoint versioning above),
i.e. **latest-only** by default.

Sources: [Temporal Cognification announcement](https://www.cognee.ai/blog/cognee-news/unlock-your-llm-s-time-awareness-introducing-temporal-cognification), [Time Awareness docs](https://docs.cognee.ai/guides/time-awareness)

### Hierarchical / multi-project scoping

Cognee organizes data into **datasets**, and all permissions (Read/Write/Delete/Share) are
defined at the dataset level (never per-document). Dataset owners can share permissions with
other tenants/roles/users. This gives a two-level scoping model — dataset ⊂ tenant/workspace —
suitable for multi-project or multi-tenant self-hosting (`ENABLE_BACKEND_ACCESS_CONTROL` on by
default), but it is **not a deep hierarchical scope model** (e.g., org → project → component →
subsystem) — it's flat dataset isolation plus RBAC, not a graph-native hierarchy of scopes baked
into the knowledge graph itself. Cognee Cloud adds isolated workspaces per tenant on top of this.

Sources: [Datasets docs](https://docs.cognee.ai/core-concepts/multi-user-mode/permissions-system/datasets), [Multi-tenant announcement](https://www.cognee.ai/blog/cognee-news/product-announcement-user-management)

### Integration surface

- **Python SDK** (primary), plus community/official **Rust** (`cognee-rs`) and **TypeScript**
  (`@cognee/cognee-ts`) clients.
- **REST API** — FastAPI server, default port 8000, Swagger/OpenAPI docs, dockerized.
- **MCP server** — `cognee-mcp`, supports stdio / SSE / Streamable HTTP transports, ships as its
  own Docker image (`cognee/cognee-mcp`, default port 8001). Exposes four tool-level capabilities:
  **Cognify** (text→graph), **Codify** (codebase→graph), **Search**, **Prune**.
- **CLI** — `cognee-cli`, including a local UI launcher.
- Deployable via Docker Compose, or to Modal/Railway/Fly.io/Render/Daytona sandboxes; managed
  Cognee Cloud alternative.

Sources: [GitHub README](https://github.com/topoteretes/cognee), [REST API docs](https://docs.cognee.ai/how-to-guides/cognee-sdk/rest-api-server), [cognee-mcp README](https://github.com/topoteretes/cognee/blob/main/cognee-mcp/README.md)

### Environmental/system evidence ingestion

Cognee's ~30+ connectors and "Add" ingestion targets are all **content sources**: documents
(PDF etc.), Slack/Notion/Drive/SharePoint, databases, images (vision models), audio
(transcription), and — via **Codify** — source code repositories (AST-level parse into
functions/classes/call-graphs/import-chains, hashed per file so only changed files are
re-processed on subsequent runs). There is **no found evidence of native connectors for git
commit/event history, Docker container logs, systemd/journald, shell/terminal session capture, or
other raw OS/service telemetry**. "Codify" ingests the *current state* of a code repo as a
structure graph, not git's event history as a time series, and nothing in docs/blog/GitHub
material surfaced covers docker/systemd/shell ingestion. Cognee is fundamentally a
document/content/code-structure memory system, not an environmental-observability agent.

Sources: [Codify / persistent codebase memory blog](https://www.cognee.ai/blog/guides/ai-coding-agent-persistent-codebase-memory), [GitHub README](https://github.com/topoteretes/cognee)

### Decision/rationale capture

Blog material describes **session traces** (end-to-end record of a multi-turn agent run: prompts,
responses, tools/data sources used, intermediate steps, results, manual interventions, plus the
slice of the graph the agent touched) and **decision traces** derived from them (the "structured
rationale... what the agent believed, what it optimised for, and why it chose an action"). A
`save_interaction`-style mechanism is referenced for explicitly logging significant architectural
decisions as first-class graph nodes, and ADR documents ingested alongside code can be linked to
the modules/functions they govern so a future agent can surface prior rationale before editing
code. This directly targets the brief's "decisions+rationale" requirement, but the primary docs
fetched did not spell out the exact API surface, storage schema, or confidence/provenance
treatment for these traces — this is best treated as a real but thinly-documented capability
(marketing/blog-level detail, not confirmed at the API-reference level in this research pass).

Sources: [Agent Memory: Decision Traces to World Models](https://www.cognee.ai/blog/deep-dives/context-graphs-world-models-and-behavioral-validation), [Persistent codebase memory guide](https://www.cognee.ai/blog/guides/ai-coding-agent-persistent-codebase-memory)

## Scoring vs the Brief's 6 Categories

**1. Capture — 2/5.** Rich ingestion breadth for *content* (30+ connectors: docs, Slack, Notion,
Drive, databases, media, code repos via Codify) and session/decision traces for agent
interactions. But no native capture of terminal/shell activity, git event history, Docker/systemd
state, or raw OS/service telemetry — everything must be pushed in as text/files/API calls. Not an
environmental-observability layer; someone else's shim would have to feed it.

**2. Representation — 3/5.** Strong on entities/relationships (LLM-extracted graph, ontology
grounding/RDF-OWL alignment) and has a real, if opt-in, temporal-event model (before/after/during
relations, `SearchType.TEMPORAL`). Weak on explicit confidence/provenance-as-first-class-fields
(base `DataPoint` schema has no confidence/evidence field; provenance lives only in
relational-store pipeline bookkeeping, not as an inspectable per-fact evidence chain). Hierarchy
is flat (dataset + RBAC), not a nested project/component scope model. Decision+rationale capture
exists (session/decision traces, ADR linking) but is thinly documented.

**3. Retrieval — 4/5.** 14 retrieval modes including hybrid, graph-completion/decomposition,
agentic ReAct-loop retrieval, temporal search, and a documented progressive-disclosure pattern
(skill catalog exposes names/descriptions only; bodies fetched on demand via `load_skill`). Dataset
scoping supports filtering by tenant/project. Multi-hop and semantic+graph hybrid retrieval are
core strengths.

**4. Consolidation — 3/5.** Memify is real and partly LLM-driven (entity-description
consolidation, coding-rule derivation, cross-entity linking, frequency-based edge reweighting).
However: entity-consolidation **overwrites the `description` field in place** rather than
demonstrably preserving raw evidence alongside the derived summary; no documented automatic
stale-node pruning was found despite Cognee's own copy claiming it; contradiction-handling claims
found in secondary sources could not be verified against primary docs.

**5. Integration — 4.5/5.** Excellent surface: Python/Rust/TS SDKs, REST API, dedicated MCP server
(stdio/SSE/HTTP, prebuilt Docker image), CLI, Docker Compose self-hosting, multiple deployment
targets. Genuinely tool/harness-agnostic per the brief's requirement — any MCP-speaking agent can
query it.

**6. Reliability — 2.5/5.** Human-inspectable to a degree (graph browsable via Neo4j/other backend
tooling, REST/Swagger, `Prune` MCP tool to reset). But no clear rollback/undo for consolidated
facts, versioning is a destructive upsert by default (no historical fact trail outside opt-in
temporal mode), and the "why do you believe this" evidence-trail requirement is only demonstrably
met for document-citation use cases (page/section citations, as in the UWyO case study) — not
as a general confidence+provenance layer over every derived graph fact.

## Maturity / License / Effort

- **License:** Apache-2.0 (permissive, self-hostable).
- **Activity:** ~29.8–30k GitHub stars, ~3.0k forks, ~9,800 commits, 102 open issues, 253 open
  PRs, active Docker image publishing on every push to main — healthy, active OSS project.
- **Funding:** $7.5M seed (2025/2026 era), backed VC + notable AI angels; commercial Cognee Cloud
  exists alongside the OSS core, so incentive alignment/maintenance risk is lower than a
  pure side-project, but also means some advanced features may be cloud-only over time.
- **Self-hosting effort:** Low for the default embedded stack — `pip install cognee`, no external
  DB servers needed (SQLite + LanceDB + Kuzu are all embedded/file-based). Moderate if you want
  REST+MCP servers running together (Docker Compose, two ports) or swap in Neo4j/Qdrant for scale.
  Overall: one of the lighter "knowledge graph memory" systems to stand up solo.

## Gaps vs the Brief's Vision

- **No environmental capture.** The brief wants git/docker/systemd/shell/multi-agent/manual-change
  capture; Cognee has none of this natively — it's a content/knowledge ingestion and reasoning
  layer, not a system-activity recorder. You'd need to build/bolt on your own capture agents that
  push structured events into Cognee's Add/Cognify pipeline.
- **Confidence and provenance are not first-class graph properties.** No `confidence` field on
  `DataPoint`; provenance is pipeline/dataset-level bookkeeping, not a queryable per-fact evidence
  chain by default. The brief's "ask why do you believe this and get an evidence trail" is only
  well-supported for document-citation-style use cases, not general derived facts.
- **Consolidation is destructive at the field level** (entity descriptions rewritten in place) and
  automatic stale-fact pruning is unconfirmed/likely partial, despite being advertised — this
  conflicts with the brief's requirement to preserve raw evidence alongside derived knowledge.
- **Temporal validity is opt-in, not default.** Outside temporal-cognify mode, updates are
  latest-only destructive upserts — not the "current vs historical state" model the brief wants
  as a baseline guarantee.
- **Hierarchical scoping is shallow** — dataset+RBAC, not nested project/component/subsystem
  scope trees baked into the graph model.
- **Decision+rationale capture is real but thin.** Session/decision traces and ADR-linking exist
  in blog material, but no primary-docs-level schema/API reference was found confirming how
  durable, structured, or confidence/provenance-tagged these decision records actually are.

## Verdict

Cognee is a strong, actively-maintained, easy-to-self-host **document/code knowledge-graph +
vector memory layer** with an unusually complete integration surface (SDKs, REST, MCP, CLI) and
genuine strengths in retrieval sophistication (14 modes, hybrid, temporal, progressive
disclosure) and graph-native representation. It is a good fit for the brief's Integration and
Retrieval requirements. It is a weaker fit for Capture (no environmental/system telemetry
ingestion — content only), for Representation's provenance/confidence/hierarchy asks (flat
dataset scoping, no first-class confidence field, provenance only at pipeline-bookkeeping level),
and for Reliability's evidence-trail and non-destructive-history requirements (default upserts are
destructive; temporal validity is opt-in; pruning/contradiction-handling claims are only
partially verifiable). Best used as one component — the "knowledge graph + retrieval engine"
layer — of a larger memory system, fed by purpose-built capture agents, rather than as a
turnkey solution to the full brief out of the box.
