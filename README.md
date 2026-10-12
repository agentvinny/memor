# memor

Durable, tool-agnostic, temporal project/environment memory for agents and
humans. It journals what actually happens on your machines (terminals, agent
sessions, git, Docker, systemd, config files, network state), consolidates
that evidence into provenance-linked facts, decisions and relationships, and
exposes the result through an MCP server, a CLI, and a git-versioned
Markdown hierarchy.

The design goal (see the research below): a future agent — or you, back from
vacation — asks "what is going on here, why is it this way, and how did we
get here?", and the answer is reconstructable without the original agent,
harness, or human remembering it.

## The one-paragraph architecture

```
Terminal / agents / git / Docker / systemd / files / network
        │  (hooks, watchers, poll-diffs — all emit JSON)
        ▼
  raw evidence store (SQLite + monthly JSONL, redacted on ingest)
        │
        ▼
  consolidation: deterministic rules (+ optional LLM sync queue)
  → facts / decisions / relations with confidence, validity windows,
    supersedes chains, and evidence pointers; contradictions and
    low-confidence items go to a human review queue — never silently
    overwritten
        │
        ▼
  ┌───────────────────────────┬──────────────────────────────┐
  │ SQLite temporal store     │ Markdown projection          │
  │ (source of truth)         │ (system/ + reference/, git-  │
  │                           │  committed at most daily)    │
  └─────────────┬─────────────┴──────────────┬───────────────┘
                ▼                            ▼
        MCP server (25 tools)          human: read the files /
        + CLI                          git log -p in the store
```

## Start here

- **`SETUP.md`** — understand the project (code map, how the pieces fit,
  dev/agent guide) + full opt-in setup: every optional feature as a control
- **`getting-started.md`** — setup, everyday use, agent integration,
  operating procedures
- **`AGENT-GUIDE.md`** — the interface contract: point any agent/harness at
  this file and it can read and write the memory correctly
- **`GAPS.md`** — the gap checklist + build log for everything that has been
  and is being built
- **`FUTURE-WORK.md`** — sequenced follow-ups beyond the current build
  (calibration-driven improvements, then extension territory)

## Research trail

| Document | Contents |
|---|---|
| `agent_memory_research_brief.md` | the original requirements brief (problem, memory model, benchmarks, design principles) |
| `research/` | nine landscape deep-dives (Letta/MemFS, Mem0/LangMem, Zep/Graphiti, Cognee, Neo4j, Basic Memory, event-sourcing/temporal stores, terminal + infra capture) and `LANDSCAPE.md` |
| `FINDINGS.md` | why no existing product is adopted wholesale; what is reused vs. built |
| `PLAN.md` | the solution architecture, component decisions, build order, verification |

## Quick commands

```bash
uv sync && uv run memor init          # set up the store
uv run memor setup ~/work             # one-shot onboarding
uv run memor doctor                   # health of every layer
uv run memor context <project>        # "catch me up"
uv run memor timeline <project>       # how did we get here
uv run memor digest                   # the periodic human digest
```

Python ≥ 3.10; dependencies: `mcp`, `fastmcp`, `anthropic` (only used when
configured), `watchdog`. Tests: `uv run pytest` (244 tests, all offline).