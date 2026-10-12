# Environmental & Project Memory for Agents

## Status

**Purpose:** Research/design brief for a durable, tool-agnostic memory system for development and operational work.

**Core requirement:** Preserve not only *what happened*, but *what was learned, decided, configured, changed, why it changed, what is currently true, what used to be true, and how all of this relates to projects and subprojects over time*.

---

# 1. The problem

When working on software, infrastructure, local AI systems, or other technical projects, work is rarely a single action. It is a long sequence of:

- investigations
- experiments
- commands
- configuration changes
- fixes
- decisions
- reversions
- migrations
- services being created or removed
- frameworks being adopted or abandoned
- ports and endpoints changing
- environment changes
- dependencies being installed or removed
- architectural choices
- temporary workarounds
- discoveries
- failed approaches
- successful approaches
- reasons for decisions
- relationships between components

Much of this information is not deliberately recorded.

An agent may know what happened during its current session, but months later the same environment can contain the accumulated result of many sessions, tools, agents, and manual interventions.

The desired system should allow a future agent — or the human — to ask:

> "What is going on here, why is it this way, and how did we get here?"

It should be able to reconstruct the answer without requiring the original agent, harness, conversation, or human to remember it.

---

# 2. The key distinction: logs are necessary but insufficient

A raw activity log is only the first layer.

For example:

```text
10:32  systemctl stop qwen.service
10:34  edited /etc/systemd/system/qwen.service
10:36  systemctl daemon-reload
10:36  systemctl start qwen.service
10:37  port 8080 became active
```

This is useful evidence, but it is not yet useful memory.

The memory system should derive higher-level knowledge:

```text
Project: Local AI Infrastructure
Component: Qwen inference server

Current state:
  Service: qwen.service
  Port: 8080

Historical state:
  Previously used port 8000

Change:
  Port changed 8000 → 8080

Reason:
  8000 conflicted with another service

Evidence:
  systemd unit diff
  shell commands
  process/socket observation
```

The system therefore needs to preserve both:

1. **Evidence / events**
2. **Derived knowledge**

The derived knowledge must remain traceable back to evidence.

---

# 3. Desired memory model

The system should be thought of as a **hierarchical, temporal, evidence-backed project memory**.

It should combine:

- timeline
- hierarchy
- entities
- relationships
- facts
- decisions
- state
- provenance
- validity periods
- semantic retrieval
- exact retrieval
- progressive disclosure

A useful conceptual model is:

```text
                           GLOBAL
                              │
          ┌───────────────────┼───────────────────┐
          │                   │                   │
       Projects             Systems            Personal
          │
     ┌────┴─────┐
     │          │
  Project A   Project B
     │
 ┌───┼───────────────┐
 │   │               │
Subsys Component   Workflow
 │
 ├── decisions
 ├── configuration
 ├── architecture
 ├── history
 ├── incidents
 ├── experiments
 └── procedures
```

The hierarchy should not be rigid. A fact may belong simultaneously to:

- a project
- a machine
- a service
- a workflow
- a person/team
- a time period

The system therefore needs both **hierarchical organization** and **cross-links/relationships**.

---

# 4. Memory should exist at different depths

Not everything deserves the same level of detail.

A future agent should be able to progressively drill down.

## Level 0 — Global index

Small, high-value orientation.

Example:

```text
Projects:
- Local AI Infrastructure
- Auto Apply
- OpenHuman setup

Machines:
- DesktopU
- Laptop

Important active services:
- Qwen inference server
- OpenHuman
- monitoring stack
```

## Level 1 — Project summary

```text
Local AI Infrastructure

Purpose:
Run local inference and supporting AI infrastructure.

Current architecture:
...

Current models:
...

Current services:
...

Major decisions:
...

Known problems:
...

Important historical changes:
...
```

## Level 2 — Component memory

```text
Qwen inference server

Current:
- model: Qwen ...
- server: ...
- port: 8080
- service: qwen.service
- GPU: ...

Previous:
- server: ...
- port: 8000

Decisions:
...

Known constraints:
...
```

## Level 3 — Detailed records

Specific decisions, experiments, incidents, configuration changes, etc.

```text
Decision:
Move inference endpoint from 8000 → 8080.

Date:
...

Reason:
...

Alternatives considered:
...

Result:
...

Evidence:
...
```

## Level 4 — Raw evidence

Conversation excerpts, shell commands, diffs, logs, tool calls, screenshots, files, etc.

A future agent should not need to load Level 4 unless Levels 0–3 are insufficient.

This is **progressive disclosure**.

---

# 5. Memory must be temporal

A major requirement is distinguishing:

- what is true now
- what was true previously
- when it became true
- when it stopped being true
- why it changed

Do not simply overwrite:

```text
port = 8080
```

Instead represent something closer to:

```text
Fact:
  Qwen inference endpoint uses port 8080.

Valid from:
  2026-08-23

Supersedes:
  Qwen inference endpoint used port 8000.

Superseded on:
  2026-08-23

Reason:
  Port conflict.

Evidence:
  ...
```

This allows historical questions:

- Why is this port used?
- What used to run here?
- When did this change?
- What did we have before?
- Why was the old solution abandoned?
- Was this setting intentional or accidental?

---

# 6. Memory types

The system should distinguish different kinds of durable information.

## Facts

Things believed to be true.

```text
The inference server listens on 8080.
```

## State

Facts tied explicitly to a point or interval in time.

```text
vLLM was the inference backend from X to Y.
```

## Decisions

Choices made intentionally.

```text
We selected vLLM over llama.cpp because ...
```

## Rationale

Why a decision was made.

## Configuration

Settings, flags, ports, environment variables, service definitions, dependencies, etc.

## Architecture

How components relate to each other.

## Experiments

Hypothesis → setup → result → conclusion.

## Incidents

Problem → diagnosis → actions → resolution.

## Procedures

How to accomplish something.

## Lessons

Generalizable knowledge learned from experience.

## Preferences / conventions

Stable project or user preferences.

## TODOs / unresolved questions

Things that still require action or investigation.

## Failed approaches

Important because repeating failed work is expensive.

---

# 7. Memory should preserve decisions, not just outcomes

This is critical.

Bad memory:

```text
Use Qwen server X.
```

Better:

```text
Decision:
Use X instead of Y.

Reason:
Y had issue Z under workload W.

Alternatives considered:
Y, Z.

Confidence:
High.

Date:
...

Evidence:
...
```

A future agent needs to know whether a configuration is:

- deliberate
- accidental
- temporary
- inherited
- experimental
- obsolete
- unknown

---

# 8. Memory should preserve uncertainty

The system should not turn guesses into facts.

A memory record should be able to distinguish:

```text
confirmed
probable
inferred
unknown
contradictory
superseded
```

Example:

```text
Fact:
The service currently uses CUDA version X.

Confidence:
Confirmed.

Evidence:
package state + process environment.
```

versus:

```text
Hypothesis:
The previous crash was caused by CUDA mismatch.

Confidence:
Medium.

Evidence:
logs suggest correlation, but no controlled confirmation.
```

---

# 9. Provenance is fundamental

Every important derived memory should ideally have provenance.

Possible provenance:

- shell command
- terminal output
- file diff
- Git commit
- conversation
- agent tool call
- browser action
- API response
- service state
- process observation
- Docker event
- human assertion
- imported document

The memory system should be able to answer:

> "Why do you believe this?"

and point back to the evidence.

This is especially important because an LLM-generated memory can be wrong.

---

# 10. The system should be tool/harness agnostic

The preferred design should not require every agent harness to implement its own memory integration.

Potential sources include:

```text
Claude Code
Qwen Code
OpenCode
normal shell
SSH sessions
GUI agents
browser agents
IDE
scripts
manual changes
system services
Docker
Git
```

The memory layer should ideally observe the environment rather than depend entirely on a specific agent.

Potential observation sources:

- terminal/session capture
- shell hooks
- process monitoring
- filesystem watchers
- Git hooks
- Docker events
- systemd/service state
- network/socket observations
- agent APIs
- MCP
- A2A
- application logs
- browser activity
- explicit human input

This suggests a **capture layer** separate from the memory/knowledge layer.

---

# 11. Proposed architecture

```text
                        ENVIRONMENT
                             │
          ┌──────────────────┼──────────────────┐
          │                  │                  │
       Terminal             Git             Services
          │                  │                  │
       Agents              Files            Docker
          │                  │                  │
          └──────────────────┼──────────────────┘
                             ▼
                    ┌─────────────────┐
                    │ EVENT CAPTURE   │
                    └────────┬────────┘
                             │
                             ▼
                    ┌─────────────────┐
                    │ RAW EVENT STORE │
                    └────────┬────────┘
                             │
                             ▼
                    ┌────────────────────┐
                    │ MEMORY PROCESSING  │
                    │                    │
                    │ extraction         │
                    │ entity resolution  │
                    │ deduplication      │
                    │ consolidation      │
                    │ contradiction      │
                    │ temporal updates   │
                    └─────────┬──────────┘
                              │
              ┌───────────────┼────────────────┐
              ▼               ▼                ▼
        Hierarchical       Temporal          Search/
        memory store       relationships     retrieval
              │               │                │
              └───────────────┼────────────────┘
                              ▼
                       MEMORY API / MCP
                              │
               ┌──────────────┼──────────────┐
               ▼              ▼              ▼
           Agent A         Agent B        Human UI
```

---

# 12. Capture, memory, and retrieval are separate problems

These should not be conflated.

## Capture

"What happened?"

## Interpretation

"What does it mean?"

## Consolidation

"What should be retained as durable knowledge?"

## Storage

"How should it be represented?"

## Retrieval

"What subset is relevant to this task?"

## Presentation

"How should the agent receive it?"

A good system can have excellent retrieval while having terrible capture, or excellent capture while having unusable retrieval.

The entire pipeline matters.

---

# 13. Retrieval should be hierarchical, not just vector search

A single embedding search over all memories is probably insufficient.

Suppose the agent asks:

> "Why does this server run on port 8080?"

The retrieval process might be:

```text
Query
  ↓
identify project
  ↓
identify component
  ↓
retrieve current state
  ↓
retrieve related historical state
  ↓
retrieve decisions affecting that state
  ↓
retrieve relevant evidence if needed
  ↓
answer
```

This is closer to **traversal + retrieval** than simple RAG.

Potential retrieval mechanisms:

- exact keyword search
- metadata filtering
- project scoping
- entity lookup
- temporal filtering
- graph traversal
- semantic/vector search
- hybrid search
- relationship expansion
- provenance traversal
- hierarchical summaries
- recency weighting

The system should use the cheapest/narrowest mechanism first and expand only when needed.

---

# 14. Memory should be scoped

A useful hierarchy could include:

```text
Global
├── user
├── machines
├── organizations
└── general knowledge

Project
├── overview
├── architecture
├── components
├── decisions
├── configuration
├── incidents
├── experiments
└── procedures

Component
├── current state
├── configuration
├── dependencies
├── history
└── decisions

Task / Episode
├── objective
├── actions
├── observations
├── decisions
├── result
└── lessons
```

A memory item should have explicit scope.

For example:

```text
scope:
  project: local-ai
  component: qwen-server
  machine: desktopU
```

This reduces irrelevant retrieval and helps prevent contamination between unrelated projects.

---

# 15. Memory should support cross-project relationships

Not everything belongs exclusively to one project.

Example:

```text
DesktopU
  ├── Local AI Infrastructure
  │      └── Qwen server
  ├── OpenHuman
  │      └── speech service
  └── Auto Apply
```

A CUDA configuration may affect several projects.

The system therefore needs relationships such as:

```text
Qwen server
  └── runs_on → DesktopU

Qwen server
  └── depends_on → CUDA

OpenHuman
  └── uses → speech service

speech service
  └── runs_on → DesktopU
```

This is one reason graph-like representations are worth investigating.

---

# 16. Memory should distinguish current state from history

A useful pattern:

```text
CURRENT
  ↓
historical lineage
  ↓
evidence
```

For example:

```text
Qwen server
Current:
  backend = X
  port = 8080
  model = Y

History:
  backend X replaced backend Z
  port 8000 → 8080
  model A → B

Why:
  backend Z had issue ...
  port 8000 conflicted with ...
  model B selected because ...
```

The current summary should be optimized for fast retrieval.

The historical records should be available for investigation.

---

# 17. Memory consolidation

Raw events should not all become permanent memories.

A consolidation process should periodically ask:

- What happened?
- What changed?
- What is still relevant?
- What became obsolete?
- What decision was made?
- What was learned?
- What should be summarized?
- What should remain raw evidence only?
- What contradictions exist?
- Which memories are duplicates?
- Which memories should be linked?

This can happen:

- after a session
- after a task
- periodically
- when context is compacted
- after a major project milestone
- on demand

Letta's current "dreaming" concept is worth studying as one implementation of background memory consolidation. citeturn0search2turn0search1

---

# 18. Progressive disclosure is a core design principle

Do not put the entire memory bank into the model context.

Instead:

```text
Level 0:
"What projects exist?"

Level 1:
"What is the state of Local AI Infrastructure?"

Level 2:
"What is the Qwen server configuration?"

Level 3:
"Why was port 8080 selected?"

Level 4:
"Show the evidence for that decision."
```

The agent should traverse the memory hierarchy as needed.

Letta's MemFS is an interesting concrete example of this idea: a small `system/` layer is always available while deeper Markdown memory is discovered and loaded only when relevant; it can also use keyword, semantic, or hybrid search. citeturn0search0

---

# 19. Candidate representation

A memory record might conceptually look like:

```yaml
id: memory-123

type: decision

scope:
  project: local-ai
  component: qwen-server
  machine: desktopU

subject: inference-endpoint

claim: "The Qwen inference service uses port 8080."

status: current

valid_from: 2026-08-23
valid_until: null

confidence: confirmed

reason:
  "Port 8000 conflicted with another local service."

supersedes:
  - memory-091

related:
  - service:qwen
  - project:local-ai
  - machine:desktopU

source:
  type: terminal
  session: abc123
  evidence:
    - /etc/systemd/system/qwen.service
    - shell command history
    - socket observation

created_at: 2026-08-23
updated_at: 2026-08-23
```

This is illustrative rather than a fixed schema.

---

# 20. Candidate storage approaches to investigate

Do not prematurely choose one.

## A. Markdown/filesystem + Git

Pros:

- human-readable
- portable
- simple
- easy for agents to manipulate
- naturally hierarchical
- excellent for project-local memory
- version history

Letta's MemFS is a strong example of this approach. It uses Markdown with YAML frontmatter, a directory hierarchy, Git versioning, and optional semantic/hybrid search. citeturn0search0

Cons:

- temporal semantics are not automatic
- contradiction handling is difficult
- large-scale retrieval can become awkward
- concurrent writes can be problematic
- files record edits but do not inherently model fact validity

A recent Zep analysis makes this exact criticism: Markdown/Git is useful, but evolving facts, provenance, contradictions, and concurrent agents push the problem toward more structured memory. citeturn0search4

## B. Relational database

Pros:

- strong metadata
- temporal state
- structured queries
- transactional updates
- easy event sourcing

Cons:

- less natural for free-form knowledge
- retrieval still needs additional mechanisms

## C. Vector database

Pros:

- semantic retrieval
- easy similarity search

Cons:

- weak representation of temporal relationships
- weak provenance by itself
- weak current-vs-historical state
- similarity does not equal relevance

Vector search should probably be one retrieval mechanism, not the whole memory architecture.

## D. Knowledge graph / temporal graph

Investigate systems such as:

- Zep
- Graphiti
- Neo4j-based agent memory
- Cognee

Zep explicitly frames agent memory as a temporal context graph where facts have provenance and validity windows. citeturn0search10

Neo4j's agent-memory project is another useful reference because it combines conversations, entities, facts, reasoning/tool traces, graph relationships, and vector/text search. citeturn0search9

## E. Hybrid

Potentially the most promising architecture:

```text
Event store
     +
Structured/temporal facts
     +
Knowledge graph
     +
Document/Markdown memory
     +
Vector/hybrid index
```

Different representations serve different retrieval jobs.

---

# 21. Projects worth researching

This list is intentionally broader than "best memory framework."

## Letta / MemFS

Study for:

- persistent agent state
- memory hierarchy
- filesystem-based memory
- progressive disclosure
- Git-backed memory
- memory consolidation
- shared memory

Official documentation indicates that MemFS is a git-backed memory filesystem, and Letta Code treats experience, memory, skills, and context as persistent state. citeturn0search0turn0search1

## Mem0

Study as:

- external memory service/library
- automatic memory extraction
- agent-agnostic memory integration

Useful comparison point, but do not assume "memory API" solves environmental history.

## Zep

Particularly relevant for:

- temporal memory
- evolving facts
- provenance
- knowledge graphs
- current-vs-historical reasoning

citeturn0search10

## Graphiti

Investigate for:

- temporal knowledge graphs
- entity relationships
- evolving world state
- retrieval over time

## Neo4j Agent Memory

Useful for studying:

- graph-native agent memory
- entity resolution
- reasoning/tool traces
- relationships between memories

citeturn0search9

## Cognee

Investigate as another graph/knowledge-oriented memory approach.

## LangMem

Investigate as a memory layer integrated with agent orchestration rather than as an entire agent runtime.

## Event sourcing / temporal databases

This is not an AI-agent product, but it may be one of the most important conceptual areas to understand.

The core idea:

> Store immutable events and derive current state from them.

This maps extremely well to the requirement that configuration changes and historical state remain reconstructible.

---

# 22. Environmental observation is its own research track

The memory system cannot remember what it never observes.

Investigate:

### Terminal

- shell history hooks
- PTY/session recording
- terminal multiplexers
- command/event capture
- stdout/stderr capture
- exit codes
- working directory
- environment changes

### Filesystem

- filesystem watchers
- Git diffs
- configuration file changes

### Processes

- process creation/termination
- service state
- ports
- sockets

### Docker

- container lifecycle
- image changes
- environment/configuration
- port mappings

### systemd

- unit changes
- service lifecycle
- journal events

### Git

- commits
- branches
- diffs
- merges
- tags

The goal is not necessarily to capture everything forever.

The goal is to make important changes **observable enough to become evidence**.

---

# 23. A critical design principle: don't trust the LLM as the source of truth

The LLM should interpret evidence.

It should not manufacture reality.

Prefer:

```text
Environment
    ↓
Evidence
    ↓
Memory extraction
    ↓
Derived knowledge
```

rather than:

```text
LLM
    ↓
"Remember this"
```

If an agent says:

> "I think we changed the server because of a port conflict."

that should remain a hypothesis unless evidence supports it.

---

# 24. Another critical principle: memory should be queryable by agents

A memory UI is useful for humans, but the primary consumer should be agents.

A future agent should have operations conceptually like:

```text
memory.search(query)
memory.get(entity)
memory.get_current_state(entity)
memory.get_history(entity)
memory.get_related(entity)
memory.get_decisions(entity)
memory.get_evidence(memory_id)
memory.get_project_context(project)
memory.get_recent_changes(scope)
```

This is more useful than a single:

```text
memory.search("...")
```

endpoint.

---

# 25. A possible retrieval algorithm

Given a new task:

```text
1. Identify project/workspace.
2. Identify machine/environment.
3. Identify entities mentioned or inferred.
4. Load compact project summary.
5. Load current state of relevant entities.
6. Traverse relationships.
7. Retrieve relevant decisions.
8. Retrieve relevant historical changes.
9. Search semantic memory for related episodes.
10. Retrieve raw evidence only if necessary.
11. Detect contradictions/stale facts.
12. Present the agent with a compact context package.
```

The retrieval system should optimize for:

- relevance
- correctness
- freshness
- provenance
- minimal context
- explainability

---

# 26. What "good memory" should allow

A good implementation should answer questions like:

### Current state

> What is running on this machine right now?

### Historical state

> What did this machine look like three months ago?

### Change history

> What changed since I last worked on this?

### Decision history

> Why did I choose this architecture?

### Troubleshooting

> Have I encountered this error before?

### Project recovery

> Catch me up on this project.

### Dependency reasoning

> What will break if I remove this service?

### Configuration archaeology

> Why is this weird environment variable set?

### Reversibility

> What did I change to make this work, and how can I undo it?

### Cross-project impact

> Which projects depend on this service?

### Forgotten work

> What did I configure manually that isn't documented in Git?

These are the real acceptance tests.

---

# 27. The "vacation test"

A particularly useful benchmark for the system:

> Stop working for 1–6 months.

Return to the machine.

Ask:

> "Catch me up."

The system should reconstruct:

1. What projects exist.
2. What changed since the last session.
3. Current state.
4. Major decisions.
5. Important historical decisions.
6. Current services and dependencies.
7. Known unresolved problems.
8. Things that may be surprising.
9. Things that are stale or uncertain.
10. Relevant evidence when requested.

If it can do this reliably, it is approaching the desired system.

---

# 28. The "agent replacement test"

Another benchmark:

> Use Agent A for six months.

Then replace it with Agent B.

Agent B should be able to continue working with minimal loss of institutional knowledge.

This is a core requirement.

Memory should belong to:

**the environment/project/user**

rather than:

**the specific agent harness.**

---

# 29. The "manual intervention test"

Another benchmark:

> Make a configuration change manually without telling the agent.

The memory system should ideally detect enough environmental evidence to notice:

```text
Something changed.
```

and eventually derive:

```text
Current state changed.
```

It may not know *why* without human input, but it should not falsely preserve the old state.

---

# 30. The "reversal test"

If something changes:

```text
A → B → A
```

the memory should preserve the full history.

It should not simply conclude:

```text
Current = A
```

and erase the fact that B existed.

This matters enormously for debugging and understanding why a configuration looks unusual.

---

# 31. Recommended exploration strategy

Do not start by building a huge agent-memory platform.

Start with a narrow but real environment:

```text
One machine
+
One project
+
One terminal
+
Git
+
systemd/Docker
```

Capture:

- commands
- Git changes
- important filesystem/config changes
- services
- selected process/network state

Store raw events.

Then build derived memories.

Then build retrieval.

Then add semantic search.

Then add graph relationships.

Then add additional agent integrations.

This gives you a reliable foundation instead of starting with an LLM and hoping it remembers correctly.

---

# 32. Potential MVP

A very practical first implementation:

```text
                  Terminal / Git / systemd
                           │
                           ▼
                     Event Collector
                           │
                           ▼
                         SQLite
                           │
                 ┌─────────┴─────────┐
                 │                   │
             Raw events          Snapshots
                 │                   │
                 └─────────┬─────────┘
                           ▼
                    Local LLM worker
                           │
                 ┌─────────┼─────────┐
                 ▼         ▼         ▼
              Facts    Decisions   Changes
                 │         │         │
                 └─────────┼─────────┘
                           ▼
                  Project memory tree
                           │
                           ▼
                    Search / MCP API
                           │
                           ▼
                      Any agent
```

SQLite is enough initially.

Do not introduce five databases before you know what retrieval queries you need.

---

# 33. Longer-term architecture

If the MVP proves useful, evolve toward:

```text
                 ┌────────────────────────┐
                 │     MEMORY CONTROL      │
                 │         PLANE           │
                 └───────────┬────────────┘
                             │
        ┌────────────────────┼────────────────────┐
        │                    │                    │
        ▼                    ▼                    ▼
  Event / Evidence      Knowledge Store       Retrieval
       Store             / Temporal Graph       Engine
        │                    │                    │
        │              ┌─────┴─────┐              │
        │              │           │              │
        │          Current State  History          │
        │                                          │
        └──────────────────┬───────────────────────┘
                           ▼
                     Memory API/MCP
                           │
              ┌────────────┼────────────┐
              ▼            ▼            ▼
          Coding        Research       Ops
           Agent         Agent         Agent
```

---

# 34. What not to optimize for initially

Do not start by optimizing:

- benchmark scores
- huge context windows
- perfect embeddings
- sophisticated multi-agent orchestration
- fancy UI
- maximum automatic memory extraction
- storing every byte of terminal output forever

First optimize for:

1. **Correctness**
2. **Traceability**
3. **Temporal accuracy**
4. **Project scoping**
5. **Useful retrieval**
6. **Low friction**
7. **Agent/tool independence**

---

# 35. Core design thesis

The desired system is not:

> "An AI that remembers things I told it."

It is:

> **A persistent, hierarchical, temporal, evidence-backed memory of my projects, environments, decisions, configurations, experiments, and work history, which any future agent can traverse and query to recover the context necessary to continue working effectively.**

The ideal system should remember:

```text
WHAT happened
WHEN it happened
WHERE it happened
TO WHAT
WHY it happened
WHO/WHAT caused it
WHAT it replaced
WHAT replaced it
WHAT evidence supports it
WHAT is currently true
WHAT is uncertain
WHAT is related
WHAT should be retrieved next
```

And it should preserve both:

```text
RAW REALITY
    +
DERIVED UNDERSTANDING
```

rather than replacing one with the other.

---

# 36. Research questions

When evaluating existing projects, explicitly ask:

### Capture
- Can it observe external activity?
- Can it observe arbitrary terminals?
- Can it ingest Git/system/service events?
- Can it ingest multiple agents?
- Can it ingest human/manual changes?

### Representation
- Does it support hierarchy?
- Does it support entities?
- Does it support relationships?
- Does it represent time?
- Does it represent current vs historical state?
- Does it represent provenance?
- Does it represent confidence?
- Does it preserve decisions and rationale?

### Retrieval
- Does it support exact search?
- Semantic search?
- Hybrid search?
- Graph traversal?
- Temporal queries?
- Scope filtering?
- Progressive disclosure?
- Multi-hop retrieval?

### Consolidation
- How are memories extracted?
- How are duplicates handled?
- How are contradictions handled?
- How are stale facts superseded?
- Can raw evidence be retained?

### Integration
- API?
- MCP?
- CLI?
- SDK?
- filesystem?
- database?
- event stream?
- A2A?
- Can multiple unrelated agents consume it?

### Reliability
- Can a human inspect it?
- Can history be audited?
- Can incorrect memories be corrected?
- Can changes be rolled back?
- Can provenance be inspected?
- Can the memory store survive replacement of the agent?

---

# 37. Current projects to compare

| Project | Primary idea | Particularly relevant to |
|---|---|---|
| **Letta / MemFS** | Persistent agent state + hierarchical filesystem memory | Hierarchy, progressive disclosure, persistence |
| **Mem0** | External memory extraction/retrieval layer | Tool-agnostic memory integration |
| **Zep** | Temporal context graph | Time, evolving facts, provenance |
| **Graphiti** | Temporal knowledge graph | Relationships + changing world state |
| **Neo4j Agent Memory** | Graph-native agent memory | Entities, reasoning traces, relationships |
| **Cognee** | Knowledge/graph-oriented memory | Knowledge extraction + retrieval |
| **LangMem** | Memory integrated with agent orchestration | Agent-controlled memory |
| **Event sourcing** | Immutable events → derived state | Environmental history and reconstruction |
| **Temporal databases** | Time-aware structured state | Current vs historical truth |

No single project should be assumed to solve the entire problem.

The goal of this research is to determine which combination of ideas produces the desired architecture.

---

# 38. Final mental model

Think of the system as a **memory operating system for your technical life**.

```text
                        YOUR MEMORY OS
                              │
       ┌──────────────────────┼──────────────────────┐
       │                      │                      │
   ENVIRONMENT              PROJECTS              AGENTS
       │                      │                      │
       │                 hierarchical              │
       │                 knowledge                  │
       │                      │                      │
       └───────────────► MEMORY ◄───────────────────┘
                            │
              ┌─────────────┼─────────────┐
              │             │             │
           Timeline       Graph         Search
              │             │             │
              └─────────────┼─────────────┘
                            │
                       Current State
                            +
                         History
                            +
                         Decisions
                            +
                          Evidence
```

The ultimate goal is **continuity across time and across tools**.

A future agent should not need to know whether yesterday's work was performed through Claude Code, Qwen Code, a normal terminal, an IDE, a browser agent, a GUI, or manually.

It should be able to enter the memory system, identify the relevant project and environment, traverse from current state → related components → decisions → history → evidence, and recover enough context to continue the work intelligently.

That is the standard against which any memory technology should be evaluated.
