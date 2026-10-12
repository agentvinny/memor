# memor — a plain-language summary

*For readers who don't work with this tech day to day. Concepts that need a
definition are explained where they appear; this is a guided tour, not the
manual (that's `getting-started.md`).*

## The problem

People increasingly get work done with the help of **AI agents** — programs
that use a large language model (an AI trained to read and write text) to
actually *do* things: write code, run commands, fix errors. These agents work
in **sessions**, and a session has no memory of the one before it. When the
session ends, everything it learned is gone.

The same is true of people: *why is this port 8080 and not 8000? Did we ever
try that approach? Was that odd setting deliberate or a mistake?* The answers
usually live in someone's head, in a chat transcript that no one re-reads, or
nowhere. Two consequences:

1. **Everything gets re-discovered.** A new agent (or you, back from
   vacation) must re-explore the environment from scratch, re-make the same
   mistakes, or silently break something that was fixed deliberately.
2. **A lot of knowledge is never even written down.** Much of what a machine
   "knows" comes from *manual* human work — editing a config file, starting a
   service, moving a container — that no agent ever saw. If it wasn't
   recorded, it's lost.

The goal of this project: make the environment's knowledge **durable and
shared** — answerable as "what is going on here, why is it this way, and how
did we get here?" — independent of any particular agent, tool, or person.
The memory belongs to the *environment*, not to whoever was using it last.

## What memor is

A small program you install on your own machine (it is written in **Python**,
runs entirely locally, and sends nothing to the cloud) that does two jobs:

1. It **keeps a journal of everything that happens on the machine.**
2. It **organizes that journal into a knowledge base** anyone or any agent
   can query.

```
What happens on the machine
(terminals, code changes, containers, services, config files, network)
        │  small "sensors" record each occurrence as plain text
        ▼
Raw journal — an append-only log (records are only ever added,
never edited or deleted), kept in one compact database file
plus monthly text archives
        ▼
The organizer — a job that runs on a schedule (hourly by default)
and turns journal entries into structured, dated knowledge records
        ▼
Knowledge base — structured records + a human-readable tree of plain
Markdown files (the standard format for simple text documents)
        │
        ├── for you: a few command-line tools, or just reading the files
        └── for AI agents: a standardized plug-in interface called MCP
```

### Job 1: the journal ("capture")

Small **sensors** — pieces of code that hook into standard system
mechanisms — record events as they happen, automatically, no matter who
caused them:

| What it watches | In plain terms |
|---|---|
| Your terminal (command line) | every command you type, its result, and how long it took |
| AI agent sessions | the commands *and their output* for tools like Claude Code or Cursor |
| **Git** | a system that records every change to a set of project files, so history can be inspected or rolled back — here: every commit, merge, and branch switch |
| **Docker** | a way of packaging software into self-contained "boxes" (containers) so it runs the same anywhere — here: containers starting/stopping and their settings drifting |
| **systemd** | the mechanism Linux uses to keep background programs (services) running — here: services starting, stopping, failing |
| Config files | the system watches the folders where configuration lives and records real before/after differences (e.g. `PORT=8000 → PORT=8080`) |
| Network | a periodic check of which **ports** (numbered doors through which network traffic enters a program) are open on which process, noting only when something changes |

Everything lands in one place: an **append-only log** (think a ship's log —
you add a line for every occurrence, you never cross out old lines) stored in
**SQLite**, a database that fits in a single file. Copy the file, you have
the whole journal.

### Job 2: the organizer ("consolidation")

A raw journal answers *what happened*, but not *what it means*. A scheduled
job periodically reads new journal entries and distills them into structured
knowledge records:

- **facts** — "the Qwen server listens on port 8080"
- **state** — time-bound truths: "the service *is* running" (true until it stops)
- **decisions** — a deliberate choice, always stored *with its reason and the alternatives considered*, so a future reader can tell deliberate from accidental
- **problems** — failures worth remembering, so "have we seen this before?" is answerable
- **lessons** and **todos** — what was learned; what is still open
- **relations** — how things connect: "service X *runs on* machine Y", "project A *depends on* B"

What makes this a *trustworthy* memory — and the part that distinguishes it
from an AI that simply "remembers" — are four rules, enforced in code:

1. **Everything is traceable to proof.** Every record points back to the
   exact journal entries (commands, diffs, observations) that support it.
   You can always ask *"why do you believe this?"* and get the evidence.
2. **Truth is dated, and nothing is ever overwritten.** A record is true
   from one moment *until* another. When reality changes, the old record is
   *closed* (marked "superseded", i.e. replaced by the newer one, which
   points back at it) rather than deleted. If a value went A → B → A, all
   three steps are still there — which is exactly what "how did we get
   here?" needs.
3. **Machine guesses never silently become truth.** Every record carries a
   confidence level (`confirmed` / `probable` / `inferred` / `unknown`).
   Anything uncertain, and anything that **contradicts** what's already
   established, goes into a **human review queue** where a person decides.
   A wrong AI guess or a stale reading cannot quietly replace established
   fact.
4. **The AI is optional and leashed.** Optionally, a large language model
   helps the organizer extract additional facts it might miss — but it is
   only allowed to cite journal entries that actually exist, and anything it
   can't cite is dropped. Without any AI configured, the plain rules carry
   the whole system.

### How you and your agents use it

**You, in the terminal**, type a handful of short commands:
`memor context <project>` ("catch me up"), `memor timeline <thing>`
("how did we get here"), `memor search "…"` ("did we ever try…?"),
`memor review` (approve or reject the queued guesses).

**You, as a human reader**, can skip the tools entirely: the knowledge base
is mirrored into a tree of plain Markdown files that is automatically
committed to git after every organizing run — so `git log` on that folder
shows, as a diff-able history, exactly *what we believed and when*.

**Your AI agents** talk to it over **MCP** (Model Context Protocol) — an
industry-standard plug-in protocol that lets any MCP-compatible AI tool call
outside programs safely, like a universal adapter. Through it an agent can
ask for a project's current state, its history, its decisions, or the
evidence behind any claim. Crucially, **every agent tool on the machine uses
the same store**: what one agent learns is already visible to the next, so
handing a project from one tool to another (or from an agent to a human)
requires copying nothing.

## Why build this instead of buying it

The project's research (in `research/` and `FINDINGS.md`) studied the
existing "AI memory" products in detail (Letta, Zep/Graphiti, Cognee, Mem0,
EverOS, Basic Memory, and several others). The striking result: **every one
of them is "capture-blind"** — they only remember what a user or agent
explicitly hands them. None watches the real environment: no one else's
tool is listening to your shell, your git history, your containers, or your
services. Meanwhile the capture layer needed here turns out to be built from
mature, unglamorous, off-the-shelf pieces (shell hooks, git hooks, Docker's
own event stream, …).

So the design is: *assemble* the sensing from boring existing tools, and
*build* only the genuinely custom parts — the organizing rules, the dated
evidence-linked data model, and the query interface. That combination —
environmental capture + traceable, non-overwriting, dated knowledge + one
shared interface for all agents and humans — is what no product on the
market offered.

## Where it stands today

- Version 0.1.0: the journal, the organizer, the review queue, the
  human-readable file tree, and the agent interface are all built and tested
  (81 automated tests, all run offline).
- It is live on its author's machine, already connected to the AI tools used
  there, and recording real work.
- Deliberately **deferred** until real usage shows the need: search by
  *meaning* (rather than by keyword), automatic synchronization between
  several machines, and a graphical review interface. Each machine runs its
  own store; moving a store between machines is a manual export/import.
- Honest limitations: it cannot remember what left no trace at all (a change
  made with no command, no file edit, no service change), and it never
  deletes — history is permanent, by design.

*Deeper reading: `README.md` → `getting-started.md` (how to run it) →
`AGENT-GUIDE.md` (the agent contract) → `PLAN.md` / `FINDINGS.md` /
`research/` (the design rationale) → `GAPS.md` (the build log).*