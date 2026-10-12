#!/usr/bin/env bash
# memor Claude Code hook — registered by `memor install --claude` as a
# PostToolUse (Bash) hook in ~/.claude/settings.json.
#
# Claude Code pipes the hook payload JSON on stdin. This wrapper forwards it
# to `memor agent-event`, which records the command AND its output (the only
# source of command output + session correlation for agent runs).
#
# Exit 0 always: a memor failure must never block or disturb the agent run.
set -u
if [ -n "${MEMOR_DISABLED:-}" ]; then exit 0; fi
MEMOR_BIN="${MEMOR_BIN:-$(command -v memor 2>/dev/null || true)}"
[ -x "$MEMOR_BIN" ] || MEMOR_BIN="$HOME/data/venvs/memory/bin/memor"
[ -x "$MEMOR_BIN" ] || MEMOR_BIN="$HOME/.local/bin/memor"
[ -x "$MEMOR_BIN" ] || exit 0
"$MEMOR_BIN" agent-event >/dev/null 2>&1 || true
exit 0