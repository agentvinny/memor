# Landscape Index & Comparison

Synthesized 2026-08-23 from the nine deep-dive files in this folder. Each row's scores are qualitative (None / Weak / Partial / Strong), traceable to the cited file — this table is a navigation aid, not a new data source. Read the linked file before acting on any single cell.

## Index

| File | Covers |
|---|---|
| [everos.md](everos.md) | EverOS (EverMind) — Markdown+SQLite+LanceDB "memory OS" |
| [letta-memfs.md](letta-memfs.md) | Letta MemFS / Context Repositories / "dreaming" |
| [zep-graphiti.md](zep-graphiti.md) | Graphiti temporal knowledge graph (Zep's OSS engine) |
| [cognee.md](cognee.md) | Cognee graph+vector memory platform |
| [mem0-langmem.md](mem0-langmem.md) | Mem0 and LangMem conversational-memory libraries |
| [bitemporal-stores.md](bitemporal-stores.md) | HydraDB and XTDB as temporal storage backends |
| [basic-memory-mcp-servers.md](basic-memory-mcp-servers.md) | Basic Memory + the MCP memory-server ecosystem + MCP itself |
| [terminal-capture.md](terminal-capture.md) | Atuin, shell hooks, asciinema — terminal evidence capture |
| [infra-capture.md](infra-capture.md) | Git hooks, Docker events/Drift, journald, Watchman, process polling |

## Comparison — memory/representation candidates

| Candidate | Capture | Representation (hierarchy/time/provenance/confidence/decisions) | Retrieval | Consolidation | Integration | Reliability | License / Maturity |
|---|---|---|---|---|---|---|---|
| **EverOS** | Weak — conversation-only, no env capture | Partial — flat ID scoping, one temporal field (Foresight), no confidence/decision schema | Solid hybrid (BM25+vector+scalar), no graph | Real (Reflection), no contradiction algorithm | Multi-harness but *officially* cloud-first; self-host MCP is community-built | Good bones (Markdown+git-diffable), unproven at scale | Apache-2.0, ~4mo old, PR-marketing-driven growth |
| **Letta MemFS / Context Repos** | Weak — agent-mediated only, one-time `/init` scan | Partial — 2-tier dir hierarchy, git-log-as-history (not queryable temporal), no entities/confidence | Partial — file nav + vector archival, no graph/temporal query | Partial — copy-on-write "dreaming," reviewable, not contradiction-aware | Moderate, Letta-centric; storage is open (git+MD) but no supported foreign-write contract | Strong inspectability (git), weak proven multi-harness safety | Apache-2.0, 23K★, MemFS ~6mo old, high self-host ops cost |
| **Zep / Graphiti** | Weak — text/JSON ingestion only, no connectors | **Strong on time+provenance** (bi-temporal edges, episode provenance), flat `group_id` scoping, no confidence/decision types | **Strong** — hybrid BM25+vector+graph+rerank, temporal filtering | Strong on supersession (`invalid_at`), dedup unverified | Strong — Apache-2.0, official MCP server (HTTP) | Mixed — auditable via Cypher, but Zep Community Edition deprecated; you operate Neo4j yourself | Graphiti: Apache-2.0, 28.9K★, mature. Zep self-host: dead |
| **Cognee** | Weak — 30+ content connectors, no env/system telemetry | Partial — real entity graph, opt-in temporal (default destructive upsert), flat dataset scoping, no confidence field | **Strong** — 14 retrieval modes, progressive disclosure, temporal mode | Partial — real (Memify) but destructive-in-place, no confirmed pruning | **Strong** — SDKs+REST+MCP+CLI, easiest self-host (embedded SQLite/LanceDB/Kuzu) | Weak — destructive upserts by default, thin evidence-trail | Apache-2.0, ~30K★, $7.5M-funded, active |
| **Mem0** | Weak — conversation-turns only | Weak-Partial — 4 flat tags, co-occurrence "graph," no time-validity/provenance/confidence | Partial — vector+BM25+entity fusion, no graph traversal API | Partial — real ADD/UPDATE/DELETE/NOOP dedup, best features paywalled | Strong — REST/MCP/SDK, Apache-2.0 core | Weak — no audit/rollback/evidence API | Apache-2.0, ~60K★, mature but feature-gated |
| **LangMem** | None beyond conversation | Weak — 3 memory *types*, no hierarchy/graph/time/provenance | Weak — whatever backend store provides | Weak — no built-in dedup | Weak — LangGraph-locked, not harness-agnostic | Weak | MIT, LangChain-ecosystem |
| **HydraDB** | Weak — no connectors, write-only API | Strong on paper (typed bitemporal edges, claimed provenance), unconfirmed schema depth | Claimed hybrid, but shipped as one opaque `POST /query` endpoint | Documented LLM extraction/dedup, thin depth | Weak — no MCP/SDK/framework docs found | Marketing-heavy claims, unproven | AGPLv3, **~2 months old, pre-1.0, no releases**, self-host commercially gated at scale |
| **XTDB** | N/A — general DB, no agent features | **Best-in-class time axis** (true bitemporal, non-destructive), no entities/provenance/confidence/decisions (100% DIY) | Exact/temporal SQL only — no semantic/graph | N/A — mechanism (non-destructive UPDATE) is excellent; no extraction/dedup | Strong generic tooling (Postgres-wire) but zero agent/MCP integration | **Strong** — 7yr track record, audit-by-design | Open, 7 years mature, production-grade |
| **Basic Memory** | None (ambient) / Strong (manual writes) | Partial — real entities+typed relations+observation-categories in Markdown, one hierarchy level (projects), no time model | Partial — full-text + graph-traversal `build_context`, no vector search confirmed | None — file is the fact, forever | **Strong** — MCP+CLI+raw filesystem, most multi-client-verified | **Strong** — plain Markdown+YAML, best human-readability | AGPL-3.0, 3.7K★, pre-1.0, active |
| **mcp-memory-service** | Weak — one Claude-Code Stop-hook, no generic connectors | Partial — typed edges, decay-as-proxy-for-time, no confidence/decision schema | Partial-Strong — real ONNX embeddings + hybrid + decay ranking | **Strong (real code)** — clustering/decay/contradiction modules exist | Strong — MCP+76-endpoint REST, but framework-adapter claims overstated | Partial — SQLite/Milvus not human-editable | Apache-2.0, ~1.9K★, maintainer-dominated (solo) |
| **mcp-memory-keeper** | Weak — one-shot git-status snapshot only | Weak-Partial — regex entity extraction, 2 flat scope layers, no time model | Weak — hashed-trigram "semantic" search, not real embeddings | None | Partial — MCP+SQLite (undocumented direct access) | Weak-Partial — checkpoint/restore is a genuine plus | MIT, 134★, small/stale |
| **Official MCP memory server** | None | Weak — flat entity/relation graph, no time/hierarchy | Weak — exact/substring only | None | Partial — MCP + raw JSONL | Partial | Reference/demo code, not production |

## Comparison — capture-layer candidates

| Candidate | What it captures | Structured output? | Effort | Maturity/Risk |
|---|---|---|---|---|
| **Atuin** | Command, cwd, exit code, duration, host, session (NOT stdout/stderr) | Yes — local SQLite, directly queryable | Hours | Mature, self-hostable, offline-capable |
| **Shell hooks (preexec/precmd)** | Same fields as Atuin, pushed live | Yes, if you write the glue | ~30-50 lines, few hours | Decade-old stable shell mechanism |
| **Claude Code hooks (PreToolUse/PostToolUse)** | Command + full output + session/prompt "why" link | Yes | Few hours per harness | Harness-specific; only source for output+rationale |
| **asciinema** | Full byte-level transcript incl. output | Semi (JSON-wrapped raw bytes) | Few hours for opt-in wrapper | Mature; use selectively, not as default (storage/parse cost) |
| **Git hooks** | Commit/merge/checkout/rewrite events | Yes, you define the schema | 2-4 hours | Trivial, core git feature |
| **Docker events + Docker Drift** | Container lifecycle + config drift vs baseline | Yes, native JSON | 1-2 hrs (events) + 1-2 hrs (Drift) | Events: very mature. Drift: small single-maintainer project, no visible repo — treat as replaceable, not load-bearing |
| **journalctl (JSON mode)** | Service lifecycle/output; **not** unit-file edits (confirmed gap) | Yes | 1-2 hours | Very mature; gap must be covered by a filesystem watcher |
| **Watchman / fswatch** | Filesystem create/modify/delete/rename (incl. systemd unit files) | Watchman: yes (JSON). fswatch: plain text, easy to wrap | 2-4 hours | Very mature |
| **ss/proc poll-diff script** | Port/process bind changes | You build the JSON, logic is yours | 3-5 hours | Core Linux tools; sampled, not event-driven (accepted tradeoff) |
| **eBPF (Falco etc.)** | Kernel-level syscall attribution | Yes | High — privileged daemon, prod-security-oriented | **Ruled out as overkill** for a personal dev-memory system |
| **"Entire CLI"** | AI agent session transcripts on an orphan git branch | Yes | N/A — wrong layer | Real product (Nat Friedman, Thoughtworks Radar "Assess"), but answers "what did the agent think," not "what happened to the repo" — not a fit here |

## What the landscape actually shows

1. **No single project satisfies the brief.** Every memory framework researched is strong in at most 2-3 of the six categories (Capture / Representation / Retrieval / Consolidation / Integration / Reliability) and weak-to-absent in the rest. This is not a sampling gap — it recurs across all nine independent research passes.
2. **Capture is the most universally unsolved piece.** Every memory framework (EverOS, Letta, Zep/Graphiti, Cognee, Mem0, LangMem, Basic Memory, all three MCP servers) is "write it via an explicit tool call," never ambient environment observation. Environmental capture is solved instead by generic infra/ops tooling (Atuin, git hooks, Docker events, journald, Watchman) that has nothing to do with "AI agent memory" branding at all.
3. **Temporal correctness (current-vs-historical, non-destructive) is best solved by boring, mature technology (XTDB, or Graphiti's `valid_at`/`invalid_at`), not by the newest/most-hyped entrants.** HydraDB and EverOS make the most direct marketing claims about this exact requirement and are the least mature/most unproven implementations of it.
4. **MCP is a settled choice for the integration/transport layer** (now Linux-Foundation-governed, cross-vendor, ~97M SDK downloads/month) but standardizes nothing about the memory data model — every existing MCP memory server invented an incompatible schema, so adopting MCP doesn't shortcut building the representation layer the brief wants.
5. **The brief's specific representation requirements — explicit confidence taxonomy, first-class Decision/Rationale objects, `supersedes`/`superseded-on`/`reason` records, and true `global→project→component→task` hierarchy — were not found, fully implemented, in any single researched system.** Pieces exist scattered across projects (Graphiti's `invalid_at`, Basic Memory's `[decision]` observation category, mcp-memory-keeper's confidence column, Cognee's opt-in temporal-cognify), but nowhere combined.
