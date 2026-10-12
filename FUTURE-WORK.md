# FUTURE WORK

Sequenced follow-ups beyond the current build (see `GAPS.md` for what is
done, with its build log). Items in §1 are improvements expected to be
validated by the calibration pass; §2 is extension territory to touch only
after the core has proven itself in real service.

**Sequencing rule:** the next milestone after the current build is *not* a
feature — it is 2–4 weeks of real service (`memor setup` on a real machine,
real projects, local LLM server wired in) plus a calibration pass: audit
what the LLM extracted (right? noisy? missing what mattered?), which rules
fire on garbage, and what actually gets used vs. never opened. The items
below are ordered on that feedback; expect some to be confirmed, some
descoped.

## 1. Improvements the calibration pass is expected to demand

In priority order:

1. **LLM prompt iteration + a tiny eval harness.** The LLM's extraction
   quality is where the system's real value lives, and the shipped prompt is
   a v1 placeholder, not a finish line. Capture a few real event windows +
   good extractions so prompt changes become measurable instead of vibes.
   Natural extension: per-project extraction profiles (what's worth
   remembering differs per project).
2. **Self-observability — `memor stats`.** Facts/day, supersede rate,
   review-queue depth, LLM-pending trend, store growth. `doctor` is a
   point-in-time photo; the time series is what shows the system drifting.
3. **Noise tuning.** Partially done (2026-08-25): `ignore_command_prefixes`
   in `memor.json` withholds pure usage commands (default `cd ls pwd clear
   history man which type help file`) from the LLM window — a fully-noise
   window skips the LLM call itself; rules were already noise-immune.
   Remaining: raise the bar on what becomes a *fact* vs. stays raw evidence
   (pager churn, failed greps, edit noise) — calibrate against real shells.
4. **The 3000-entry seen-set cap.** Descoped by decision (2026-08-25): no
   retention window / automatic compaction will be added — nothing falls
   out of the journal; store growth is surfaced instead via a
   `max_store_size_bytes` alert (default 5 GiB) in `doctor`, the daily
   digest, and agent context packages. The cap itself remains a
   performance-only issue (idempotency bookkeeping); revisit only if
   volume makes re-scanning expensive.
5. **The agent-replacement test with a real second harness.** The brief's
   core requirement has only been exercised with our own MCP client. Point
   Cursor/OpenCode/a plain script at `AGENT-GUIDE.md` + the MCP server, hand
   it a project, and verify it works from the document alone — this either
   validates or rewrites the guide.

## 2. Extensions that would make it more than a personal tool

Only after §1 (and the calibration pass) proves the core:

- **Cross-machine topology.** The store is already portable
  (`export`/`import --as-machine`); a pull-based sync protocol (boxes
  exchanging new events/facts on a schedule) would make "global" genuinely
  global across a fleet.
- **A pleasant human review surface.** Agents are the primary consumers, but
  the human is the reviewer; even `memor review --interactive` (a small TUI)
  closes that loop.
- **PyPI packaging.** `uv tool install memor` on each box instead of cloning
  the repo — small lift, big effect on the cross-machine story.
- **Team scope.** The model has `project`/`machine`; adding `team` + a
  shared store turns it into small-team onboarding ("new teammate's harness
  points at the shared memory").
- **P4 — semantic/embedding search.** Still last, and only if zero-result
  `memory_search` queries start piling up; the gate is defined in
  `GAPS.md` (store the misses as events; revisit when the pile grows).

## Guardrail (why the ordering is the ordering)

Not to do, in any of the above: adopt a new database, add an embeddings
stack speculatively, add multi-agent orchestration, or add more scope types
beyond what a real need shows. The system is only as good as its trust in
real use, and none of those accelerate that.