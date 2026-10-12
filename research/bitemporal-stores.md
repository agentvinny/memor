# HydraDB & Bitemporal Databases (XTDB) — Research Findings

Researched: 2026-08-23. Sources cited inline; full list at bottom.

## Overview

This file evaluates two candidates for the **storage backend** layer of a durable, tool-agnostic
memory system that needs correct time representation (current vs historical, when/why things
changed) and strong provenance:

- **HydraDB** (github.com/hydra-db/hydradb, hydradb.com) — a young, venture-funded, purpose-built
  "AI agent memory" product that markets itself heavily around temporal graphs and decision
  traceability.
- **XTDB** (xtdb.com) — a mature, general-purpose **bitemporal** database (not agent-specific) that
  has spent years solving the "valid time vs transaction time" problem properly.

Also considered briefly: Postgres + valid_from/valid_to + audit table (roll-your-own), and
Zep/Graphiti's built-in temporal graph modeling (see companion file `zep-graphiti.md` in this
directory for the deep dive — referenced here only for the effort-comparison).

---

## HydraDB findings

**What it actually is.** Confirmed via GitHub API (api.github.com/repos/hydra-db/hydradb, fetched
directly, not scraped HTML): a real, AGPL-3.0-licensed repo, description "HydraDB - fast graph
database on object storage." `license.name` = "GNU Affero General Public License v3.0" — confirmed
by fetching the raw LICENSE file. So **yes, it is genuinely open source under AGPLv3**, not just
marketing.

**Self-hostable today?** Yes, mechanically: `docker pull ghcr.io/hydra-db/hydradb:latest` is a real
published image, and the README documents source-build requirements (Rust 1.91+, C/C++ toolchain,
libcypher-parser, SuiteSparse GraphBLAS) plus a Kubernetes/Helm chart. However, the commercial
pricing page (hydradb.com) tells a more nuanced story: self-hosting is explicitly monetized —
"Scale" tier ($399/mo) lists self-host as a *licensed add-on*, and full self-host without usage
caps is only clearly offered at the custom "Enterprise/BYOC" tier. The free "Ship" tier is API-call
based against their managed cloud, not self-host. So: **the OSS repo is real and buildable, but the
company's actual go-to-market is the managed cloud product; self-hosting is a secondary, partly
gated path** — closer to open-core than to "clone and run for free at any scale."

**Maturity.** Repo `created_at` = 2026-07-03 — **under two months old** as of this research
(2026-08-23). `pushed_at` = 2026-08-19 (active, recent commits). 1,157 stars / 592 forks / 62 open
issues per the GitHub API. **No GitHub Releases published** (checked releases page directly — "there
aren't any releases here"), so there is no v1.0, or even a tagged v0.x release yet — this is
pre-alpha/pre-1.0 by any normal definition, whatever a stray "0.1.0" reference in docs might imply.
The company raised $6.5M (per multiple press/social sources — Medium, X/Twitter, aibase.com) shortly
before/around open-sourcing, which is consistent with a young, well-funded startup still finding
product-market fit rather than a battle-tested system with a real production track record. No
independent case studies or named production users were found in this research; all "accuracy"
numbers (e.g., "90.79% on LongMemEval-s") are self-reported on HydraDB's own benchmarks subdomain,
not third-party verified.

**What "git-styled temporal graph + vector index + B-tree in one unified memory layer" means
concretely.** Per hydradb.com/blog/agent-memory-layer-vs-vector-db and
hydradb.com/blog/ai-agent-decision-traceability-auditability:
- **Temporal graph**: edges are immutable/append-only and each carries
  `(semantic_relation, t_commit, t_valid, C_meta)` — a typed relation (e.g. `WORKS_AT`,
  `CAUSED_BY`, `BLOCKED_BY`), an ingestion timestamp (`t_commit`), a "true in the world" timestamp
  (`t_valid`), and a metadata blob (`C_meta`) meant to hold reasoning/context. This is a genuine
  bitemporal-on-edges design in principle — but the blog posts describe the *shape* of the model in
  prose, not a schema spec, and no worked example of `C_meta`'s actual contents was found.
- **Vector index**: a native embedding index used for semantic-similarity retrieval, fused with the
  graph rather than a bolted-on external vector DB.
- **B-tree**: used for structured/ordered lookups and metadata filtering (exact-match / range
  queries), i.e., the "exact retrieval" leg of the hybrid pipeline.
- These three back three declared "memory primitives": semantic knowledge, user memories, episodic
  experiences.

**Query API/language — this is the weakest, most concrete finding.** The underlying repo
(hydra-db/hydradb, the OSS graph engine) is a genuine **OpenCypher** graph database — "a practical
OpenCypher subset for graph reads and mutations" (relationships, bounded variable-length paths,
property/label predicates, ordering, pagination, aggregation), reachable via Neo4j Bolt drivers or
an HTTP/JSON+NDJSON API, plus native path procedures (`algo.SPpaths`, `algo.SSpaths`, `algo.MSpaths`).
GraphBLAS is used for traversal; SlateDB for durable storage on S3-compatible object storage;
storage/compute are disaggregated (stateless `graph-node` query/mutation nodes + `graph-indexer`
nodes building async indexes). **But** the docs.hydradb.com "Core Concepts" page — the actual
product-level documentation for the memory product built on top of this engine — describes a
**single REST endpoint**, `POST /query`, taking `query_by` (hybrid/text), `mode` (thinking/fast),
and `graph_context` parameters, with no query language exposed to the caller and no code/SDK
examples found. That's a real gap: the raw graph DB has a real (if partial) query language, but the
packaged "memory layer" product wraps it behind an opaque, undocumented-in-depth single endpoint. No
MCP integration, agent-framework (LangChain/CrewAI) integration, or language-specific SDK
documentation was found on hydradb.com/use-cases; the use-cases page lists only problem domains
(customer support, coding assistants, IT ops, etc.), not integration mechanics.

**Extraction/consolidation.** hydradb.com/blog/knowledge-graphs-for-ai-agents-a-practical-guide
describes an LLM-based extraction pipeline (chunk → LLM extracts entities + SPO triples), duplicate
handling via entity-resolution prompting with an explicit warning about LLM-hallucinated
relationships, and stale-fact handling via the temporal validity windows described above. It claims
provenance is maintained end-to-end ("I found this because it came from a support ticket Sarah
submitted in 2024, not from speculation") — plausible given the graph-native design, but no schema
or query example demonstrating this was found; it is stated in blog prose, not documented API
behavior.

---

## XTDB findings

**Bitemporal model.** XTDB is a bitemporal database with two orthogonal time axes on every record
(v1-docs.xtdb.com/concepts/bitemporality/, docs.xtdb.com/concepts/key-concepts.html):
- **Valid time** (`_valid_from` / `_valid_to` in v2; user-assigned) — when the fact was/is true in
  the real world. Fully user-controlled: you can backfill, correct retroactively, or schedule future
  changes ("proactive" writes).
- **System/transaction time** (`_system_from` / `_system_to` in v2) — when the database itself
  learned the fact. Append-only and immutable: you cannot rewrite the past of this axis, which is
  exactly what gives you an audit trail ("what did we believe was true, and when did we start
  believing it").
- Old versions are never destructively overwritten: a `PUT`/update closes the valid-time (and/or
  system-time) interval of the prior version and opens a new one. Point-in-time queries
  (`FOR VALID_TIME AS OF ...`, `FOR SYSTEM_TIME AS OF ...`) read whichever version was true along
  both axes at the queried instant.

**Schema/record shape.** XTDB v1 was schema-free EDN documents, auto-indexed as
Entity-Attribute-Value triples for graph-style queries (Datalog). **XTDB v2** (the current,
production-focused line — v2.0.0 "we're live!", v2.1.0 Dec 2025, v2.2.0-beta1 in progress per
github.com/xtdb/xtdb/releases) is a rearchitected, Postgres-wire-protocol, Apache Arrow-based HTAP
system with a stable SQL API and storage format; docs.xtdb.com describes it as schema-flexible
(each row just needs `_id`) rather than fully schemaless, supporting nested/composite types (sets,
structs, maps, vectors) for graph-like data — but this is document flexibility, not a native
entity/relationship graph engine.

**Modeling "port changed 8000→8080 because of a conflict" in XTDB v2:**
```sql
-- Prior fact (implicit; valid_from set when first inserted, e.g. 2026-01-10)
-- INSERT INTO service_config (_id, service, port) VALUES ('svc:foo:port', 'foo', 8000)

-- The change — a normal UPDATE/PUT; XTDB automatically closes the old valid-time
-- interval and opens a new one, no explicit versioning code required:
UPDATE service_config
SET port = 8080
WHERE _id = 'svc:foo:port'
-- (optionally) FOR VALID_TIME FROM DATE '2026-08-20'  -- backdate/forward-date if needed

-- Query "what was the port before, and what changed":
SELECT * FROM service_config FOR VALID_TIME ALL WHERE _id = 'svc:foo:port';
-- returns both rows: (8000, valid 2026-01-10..2026-08-20), (8080, valid 2026-08-20..null)

SELECT * FROM service_config FOR SYSTEM_TIME AS OF TIMESTAMP '2026-08-15 00:00:00Z'
WHERE _id = 'svc:foo:port';
-- what did the DB believe as of Aug 15 (before the change was even known)?
```
The **"reason: conflict" and "evidence: systemd diff" parts are NOT native to XTDB** — there is no
built-in provenance/rationale/evidence field. You get this by modeling it yourself: either (a) add
plain columns to the row itself (`reason TEXT, evidence_ref TEXT`) that ride along with each
bitemporal version, or (b) maintain a separate `change_events` / `decisions` table with a foreign
key to the config row's `_id` and its `_valid_from`, storing `reason`, `evidence_uri` (or a blob/hash
of the systemd diff), `actor`, etc. — essentially recreating an event-sourcing log on top of XTDB's
built-in temporal versioning. XTDB gives you the "when did this change and what was true before"
axis for free; the "why" and "evidence" are pure application-level modeling, same as they would be
in Postgres.

**What's missing (confirmed by direct docs fetch).** docs.xtdb.com/concepts/key-concepts.html
explicitly has no mention of provenance metadata or confidence scores as first-class concepts; it
positions XTDB as an HTAP/columnar system "closer to data warehouse systems than graph databases."
Concretely absent, confirmed via search/fetch:
- No LLM-based fact extraction (it's a database, you write SQL/inserts yourself).
- No hierarchy/entity/relationship graph primitives beyond nested document columns — no native
  `MENTIONS`/`CAUSED_BY`-style typed graph edges (v1's EAV indexing gave you Datalog graph queries
  over triples; v2's positioning is SQL/HTAP, not graph-first).
- No MCP server, no agent-framework integration, no "memory" vocabulary (episodes, decisions,
  consolidation) anywhere in the docs — it is a **general bitemporal SQL database**, full stop.
- No built-in "supersede because of contradiction" semantics — a bitemporal UPDATE just records a
  new valid-time interval; recognizing that two facts *contradict* (vs. simply differ) is an
  application-level concern.

**Maturity.** Much more mature than HydraDB: v1 launched 2019 (JUXT), currently in
maintenance/legacy mode; **v2 is the current production line** with tagged releases going back
through 2025 and active beta development (v2.2.0-beta1) as of this research. Real users and a
years-long track record (JUXT is an established Clojure consultancy; XTDB has enterprise/compliance
positioning, e.g. "time-travel SQL database to simplify compliance"). This is an order of magnitude
more battle-tested than HydraDB.

---

## Other bitemporal/event-sourcing options (brief)

- **EventStoreDB** — open source, purpose-built for event sourcing (append-only event streams +
  projections). Not bitemporal in the valid-time/system-time sense XTDB or the brief wants — it
  gives you an immutable log of "what happened when it was recorded," but no native retroactive
  valid-time correction axis. Good fit if the design leans fully event-sourced rather than
  bitemporal-query-oriented; would need your own projection layer for "what do we believe is true
  now" views.
- **Datomic** — closest philosophical cousin to XTDB (same lineage/inspiration; immutable
  accumulate-only facts, point-in-time queries via `asOf`/`since`). Commercial license (not fully
  OSS), historically requires a supported storage backend (DynamoDB/SQL/etc.) and JVM. Only has one
  time axis natively exposed the way most people use it (transaction time), though valid-time can be
  modeled via extra attributes — genuinely bitemporal patterns are DIY on top of Datomic too, similar
  effort profile to Postgres. Worth a mention, not a deep dive given commercial licensing friction
  for a solo dev.

Neither is a better fit than XTDB for this brief's "current vs historical, non-destructive,
queryable both ways" requirement — XTDB is the more direct match because bitemporality (not just
event logging) is its core, native primitive.

---

## Scoring vs 6 categories

| Category | HydraDB | XTDB |
|---|---|---|
| **1. Capture** — external activity/terminals/git/system/service events/multi-agent/manual changes | Not a capture layer itself; it's a memory *store* that agents write to. Multi-tenant write support exists (dashboard shows "multi-tenancy"), but no built-in connectors for terminals/git/systemd/service logs — you'd still need your own capture pipeline feeding it via the `POST /query` (write path) or SDK. | N/A — general DB, no agent/capture features at all. You write rows via SQL/API from whatever capture pipeline you build; XTDB has zero opinion on sources. |
| **2. Representation** — hierarchy, entities, relationships, time (current vs historical), provenance, confidence, decisions+rationale | Strong on paper: typed graph relationships, native bitemporal edges (`t_commit`/`t_valid`), claimed provenance ("traced back to source ticket"). No confirmed confidence-score field. No explicit `Decision`/`Rationale` schema — reasoning goes into a generic `C_meta` blob of unspecified structure. Hierarchy: graph-native, so arbitrary hierarchies are representable, but no example was found of a built-in project/component/task tree. | Excellent, best-in-class on the time axis specifically: true bitemporal (valid-time + system-time) on every row, non-destructive by design, this is XTDB's whole reason for existing. Entities/relationships: v1 had EAV/Datalog graph queries; v2 is SQL/HTAP with nested docs, not a graph model. Provenance, confidence, decision+rationale: **not built in at all** — 100% DIY columns/tables. |
| **3. Retrieval** — exact/semantic/hybrid/graph traversal/temporal/scope filtering/progressive disclosure/multi-hop | Hybrid pipeline explicitly claimed: metadata filtering + semantic + keyword + graph-context traversal, temporal filtering (validity windows), `mode: thinking/fast` suggests some progressive-disclosure-like tiering. Underlying OSS engine supports real multi-hop Cypher path queries. But the packaged product API is a single opaque `POST /query` endpoint — much of the "how you construct precise queries" is hidden/undocumented. | Exact + temporal are excellent and SQL-native (`FOR VALID_TIME`, `FOR SYSTEM_TIME`, arbitrary WHERE predicates, joins). No semantic/vector search, no built-in graph traversal in v2, no progressive disclosure or agent-oriented retrieval concepts — it's a SQL database; you get exactly what SQL gives you. |
| **4. Consolidation** — extraction, duplicate/contradiction handling, stale-fact superseding, raw evidence retention | LLM-based extraction pipeline documented (chunk→extract entities/triples), explicit dedup-by-entity-resolution guidance, stale-fact handling via validity windows (superseding, not deletion), evidence retention claimed via source-document backlinks. Real design intent, thin documentation depth. | N/A for extraction/dedup/contradiction detection (general DB, no agent features) — but the underlying *mechanism* for non-destructive superseding is XTDB's strongest feature: an UPDATE never destroys the prior valid-time version, so "raw evidence retention" via an app-level audit table is easy and natural to build correctly here. |
| **5. Integration** — API/MCP/CLI/SDK/filesystem/database/event stream, multi-agent consumption | HTTP/JSON+NDJSON API and Bolt (Neo4j-driver-compatible) at the raw graph-DB layer; the memory-product layer offers one documented REST endpoint. No MCP server, no LangChain/CrewAI docs, no CLI found, no filesystem mode (object-storage-backed only). Multi-tenancy supported (dashboard feature). | PostgreSQL wire-protocol compatible in v2 (huge integration win — any Postgres client/driver/ORM/BI tool works out of the box), SQL API, JDBC-like ADBC/Flight SQL coming in v2.2.x. No MCP, no agent-specific SDK, no CLI beyond standard SQL clients — again, N/A for agent-specific integration, but broad *generic* tooling compatibility. |
| **6. Reliability** — human-inspectable, auditable, correctable, rollback, provenance inspectable, survives agent replacement | Auditability is a headline marketing claim (git-style versioning, "trace any decision back through its dependency lineage") but backed by thin public documentation/examples; AGPLv3 + Docker image means you *can* inspect/host it yourself, but pre-1.0 status (no GitHub releases yet, 2-month-old repo) is a real reliability risk — schema/API could change without a stable versioning contract. Rollback: append-only graph implies old states remain queryable, consistent with "correctable not destructive." | Very strong: system-time axis is precisely designed for "roll back and see exactly what the DB believed and when," which is inherently human-inspectable/auditable; corrections are just new valid-time versions, nothing is destroyed; years of production use back this up. Survives "agent replacement" trivially — it has no agent concept to begin with, so there's nothing agent-specific to break. |

---

## Maturity / License / Effort / Risk

| | HydraDB | XTDB | Postgres + valid_from/valid_to + audit table | Zep/Graphiti |
|---|---|---|---|---|
| License | AGPLv3 (confirmed) | Apache-2.0-style OSS (JUXT open-source; check exact SPDX at time of adoption) | Postgres license (permissive) | Apache-2.0 (Graphiti); Zep Cloud commercial |
| Age/maturity | ~2 months old, pre-1.0, no GitHub releases yet | 7 years (2019–2026), v2 stable production line, tagged releases | Postgres itself is decades-mature; the temporal pattern is well-known/DIY | ~2 years, 28.9K stars, but Zep Community Edition (self-host) is deprecated |
| Effort to adopt for this brief | Low effort for the "memory product" shape (schemas/primitives pre-built) if you accept the opaque `POST /query` API and pre-1.0 risk; **high effort/risk** if the product changes API before 1.0 | Medium effort: bitemporal time axis is free and correct out of the box, but provenance/decision/rationale schema, extraction pipeline, hierarchy, and retrieval logic are 100% yours to build | Medium-high effort: even the time-axis correctness (the hardest part to get right — e.g., overlapping interval bugs, correct AS-OF semantics) is yours to build and test, in addition to everything XTDB doesn't provide | Low-medium effort if self-hosting Graphiti directly (not deprecated Zep server): temporal graph + provenance + hybrid retrieval largely pre-built, but you still build capture/hierarchy/decision-schema layers yourself |
| Key risk | Vendor/product immaturity: could pivot, deprecate self-host tier, or break API pre-1.0; opaque query surface limits control | None storage-wise; the risk is entirely "you must build the agent-memory semantics yourself" | Same as XTDB but you also own getting bitemporal correctness right (a known hard problem — XTDB's own "DIY Bitemporality Challenge" post argues this exact point) | Depends on Neo4j/FalkorDB/Neptune backend choice; deprecation of the "batteries included" Zep server already happened once |

---

## Gaps vs the brief's vision

Neither HydraDB nor XTDB alone satisfies the full brief; both are **storage-backend** candidates,
not full solutions:

- **HydraDB** is the only one of the two that attempts agent-memory semantics (extraction,
  consolidation, provenance-as-a-feature) natively, but it is unproven, pre-1.0, and its
  documented query surface is thin/opaque relative to its marketing claims. Its "capture" story
  (terminals/git/systemd/service events) is entirely absent — it expects you to feed it structured
  writes.
- **XTDB** nails exactly one piece of the brief — non-destructive current-vs-historical state with
  a genuinely correct dual time axis — better than anything else evaluated, but has **zero** opinion
  on capture, extraction, hierarchy, decisions/rationale schema, retrieval ergonomics for agents, or
  integration with LLM tooling. Every other requirement in the brief (capture, consolidation,
  MCP/agent integration) is 100% your own build on top of it.
- Both leave "capture" (terminals, git hooks, systemd/service diffs, multi-agent write
  coordination) as an external, unaddressed pipeline that has to be built regardless of which
  storage backend is chosen.

---

## Verdict

For a **solo developer** building this system, **XTDB is the lower-risk choice specifically for
the storage backend layer that must represent time and non-destructive history correctly** — it is
mature (7 years, stable v2 production line, Postgres-wire compatible so tooling/ORM support is
broad), its bitemporal model does exactly what the brief's port-8000→8080 example needs (old value
never destroyed, both time axes queryable, "what did we know when" answerable natively), and the
license/deployment risk is minimal. The cost is that XTDB gives you **only** the time axis — you
still write the schema for provenance/evidence/reason (e.g., extra columns or a linked
`decisions`/`evidence` table referencing config rows by `_id` + `_valid_from`), and you get none of
HydraDB's agent-specific conveniences (extraction, hybrid retrieval, graph traversal) for free.

**HydraDB is higher-risk, not because its ideas are wrong** — the temporal-graph-edge design
(`t_commit`/`t_valid`/`C_meta`) is directionally exactly what this brief wants, more so than XTDB's
generic rows — **but because the product is two months old, pre-1.0 (no tagged release), its
self-host tier is commercially gated at scale, and its actual documented query API is a single
opaque REST endpoint** rather than the OpenCypher surface the underlying OSS engine technically has.
Adopting it now means betting a "durable, tool-agnostic" memory system's storage layer on a startup
that could pivot its API, deprecate free self-hosting, or simply not survive — precisely the kind
of fragility this project's "harness-agnostic, durable" requirement is trying to avoid.

**Recommendation**: use **XTDB (or plain Postgres with valid_from/valid_to + an audit/evidence
table, if you want to avoid a second database technology and JVM dependency and don't need XTDB's
free correctness guarantees around interval bugs) as the underlying temporal storage engine**, and
build the provenance/decision/rationale/hierarchy schema and the LLM-extraction/consolidation
pipeline yourself on top — the same layer HydraDB would give you out of the box, but without
betting on an unproven vendor. If a purpose-built agent-memory product is still wanted for its
higher-level conveniences (extraction, hybrid retrieval), **Graphiti** (self-hosted directly, not
via the deprecated Zep server) is a more defensible middle ground than HydraDB: same category of
built-in temporal-graph agent semantics, but 2+ years old with 28.9K stars and a real OSS track
record, versus HydraDB's 2-month-old pre-1.0 repo. HydraDB is worth re-evaluating in 6–12 months
once it has a tagged 1.0 release, a stable documented query language, and a self-host story that
isn't tier-gated.

---

## Sources

- https://github.com/hydra-db/hydradb (README, LICENSE, releases, commits — fetched directly)
- https://api.github.com/repos/hydra-db/hydradb (stars/forks/dates/license, fetched directly)
- https://hydradb.com/
- https://hydradb.com/use-cases
- https://hydradb.com/blog/ai-agent-context-database-problem ("AI Agent Memory Is a Database Problem" — via search snippet; direct fetch 404'd)
- https://hydradb.com/blog/agent-memory-layer-vs-vector-db ("HydraDB vs Traditional Vector Databases")
- https://hydradb.com/blog/ai-agent-decision-traceability-auditability ("AI Decision Traceability")
- https://hydradb.com/blog/knowledge-graphs-for-ai-agents-a-practical-guide
- https://docs.hydradb.com/concepts
- https://benchmarks.hydradb.com/hydradb
- https://v1-docs.xtdb.com/concepts/bitemporality/
- https://v1-docs.xtdb.com/concepts/what-is-xtdb/
- https://docs.xtdb.com/concepts/key-concepts.html
- https://docs.xtdb.com/about/time-in-xtdb.html
- https://xtdb.com/blog/launching-xtdb-v2
- https://xtdb.com/blog/diy-bitemporality-challenge
- https://github.com/xtdb/xtdb (releases, README)
- https://www.juxt.pro/blog/value-of-bitemporality/
- https://www.getzep.com/ai-agents/temporal-knowledge-graph/ (Graphiti bi-temporal model, for comparison)
- https://blog.getzep.com/beyond-static-knowledge-graphs/
- db-engines.com Datomic/EventStoreDB comparison pages (brief mentions only)
