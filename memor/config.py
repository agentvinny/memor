"""Configuration: data dir, project registry, watch list, LLM settings."""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

def default_data_dir() -> Path:
    env = os.environ.get("MEMOR_DATA_DIR")
    if env:
        return Path(env).expanduser()
    return Path("~/.local/share/memor").expanduser()
DEFAULT_LLM_MODEL = os.environ.get("MEMOR_LLM_MODEL", "claude-sonnet-5")

# Commands that are *using* the system (navigation, paging, help) rather than
# *changing* it. They stay in the raw journal but are withheld from the LLM's
# extraction window, so a local model's "skip routine noise" instruction isn't
# the only defense against ls/cd churn becoming "facts". Extend per-machine,
# e.g. the prefix of your LLM server's start command.
DEFAULT_NOISE_COMMANDS = [
    "cd", "ls", "pwd", "clear", "history",
    "man", "which", "type", "help", "file",
]

DEFAULT_WATCH_PATHS = [
    "/etc/systemd/system",
    "/etc/docker",
    "~/.config/systemd/user",
    "~/.config/memor",
]


def normalize_remote(url: str) -> str:
    """Canonical form of a git remote: `host/owner/repo` (scheme, user@,
    `.git` suffix, trailing slash all stripped) so `git@github.com:me/ai.git`,
    `https://github.com/me/ai` and `github.com/me/ai` all match."""
    u = url.strip().rstrip("/")
    u = re.sub(r"^https?://", "", u)
    u = re.sub(r"^git\+ssh://", "", u)
    u = re.sub(r"^ssh://", "", u)
    if "@" in u:
        u = u.split("@", 1)[1]
    u = re.sub(r"^([A-Za-z0-9.-]+):\d+/", r"\1/", u)  # drop ssh host:port
    u = u.replace(":", "/", 1) if ":" in u else u
    if u.endswith(".git"):
        u = u[: -len(".git")]
    return u.lower()


@dataclass
class ProjectRef:
    name: str
    paths: list[str] = field(default_factory=list)
    remotes: list[str] = field(default_factory=list)
    description: str = ""


@dataclass
class Config:
    data_dir: Path | None = None
    projects: list[ProjectRef] = field(default_factory=list)
    watch_paths: list[str] = field(default_factory=lambda: list(DEFAULT_WATCH_PATHS))
    fs_interval: float = 2.0
    net_interval: float = 20.0
    docker_snapshot_interval: float = 900.0
    consolidate_interval: float = 3600.0
    verify_interval: float = 300.0
    episode_idle_gap: float = 1800.0
    digest_interval: float = 86400.0
    # Throttle for git commits of the Markdown projection on *automatic*
    # (daemon) passes: files are always rewritten on disk, but a commit is
    # made at most once per interval (manual CLI commands always commit
    # immediately). 0 = commit on every pass (pre-2026-08 behavior).
    projection_commit_interval: float = 86400.0
    # Store-size alert threshold: when the store directory's on-disk
    # footprint exceeds this, `memor doctor`, the daily digest, and the
    # agent context packages all surface a warning (options: `memor
    # compact`, or raise the threshold). 0 disables the alert. No
    # automatic retention/compaction is run (deliberate: nothing falls out
    # of the journal; growth is surfaced, not silently pruned).
    max_store_size_bytes: int = 5 * 1024 ** 3
    retention_months: int = 12
    consolidate_batch: int = 500
    llm_model: str = DEFAULT_LLM_MODEL
    llm_enabled: bool = True
    llm_base_url: str = ""
    # Optional auth for `llm_base_url` (hosted endpoints such as OpenRouter
    # require it; leave empty for local unauthenticated servers). May be a
    # literal key or a path to a file containing the key, read at call time
    # so rotation takes effect without a restart.
    llm_api_key: str = ""
    # OpenRouter provider routing: strict allowlist of upstream provider
    # slugs (e.g. ["xiaomi"]). When non-empty, LLM requests are pinned to
    # those upstreams — a request fails rather than falling back to another
    # provider serving the same model. Ignored by non-OpenRouter endpoints.
    llm_provider_only: list[str] = field(default_factory=list)
    llm_autosync: bool = False
    llm_sync_interval: float = 600.0
    # Max completion tokens per LLM extraction call. The output is a JSON
    # document (up to 15 records with evidence/reasons); 4096 truncated
    # real windows on a 27B local model. Raise if a larger window is
    # configured (consolidate_batch), lower for endpoints with small
    # output caps.
    llm_max_tokens: int = 16384
    ignore_command_prefixes: list[str] = field(
        default_factory=lambda: list(DEFAULT_NOISE_COMMANDS))

    # --- session-memory (SESSION-MEMORY.md) -------------------------------
    # Agent-session journaling + hierarchical summary trees.
    session_journal_enabled: bool = True
    # harnesses whose command hooks are wired (v1: "dsh")
    session_harnesses: list[str] = field(default_factory=lambda: ["dsh"])
    # DSH session-log root (session v2 JSONL, zstd-compressed)
    session_dsh_root: str = "~/.dsh"
    # a window closes when this many agent tool steps have accumulated.
    # User prompts never close a window (they are content, not boundaries)
    # — only steps or Stop do.
    session_window_steps: int = 6
    # window width for subagent sessions, indexed by delegation depth
    # minus 1: [12, 24] = depth-1 subagents get 12-step windows, depth-2+
    # get 24 (the last entry caps deeper levels). Empty list = the uniform
    # session_window_steps for all depths.
    # -1 at index i DISABLES windowing for depth i+1 and every deeper
    # depth (ordered: a level only logs when every level above it logs —
    # an entry after the first -1 is ignored). Exempt sessions keep no
    # state and journal nothing: the parent that called them summarises
    # them as part of its own tool calls. [-1] = subagents off entirely.
    session_subagent_window_steps: list = field(
        default_factory=lambda: [12, 24])
    # N unparented summaries at level k trigger one level-(k+1) parent
    session_chain_width: int = 3
    # "detached" = each finished window spawns a short-lived
    # `memor window-flush` worker; "resident" = `memor session-watch`
    # picks windows up from its poll loop.
    session_execution_mode: str = "detached"
    # LLM failure retries before a window degrades to a rule stub
    # (status="partial"); raw data is never at risk either way.
    session_max_flush_retries: int = 3
    # A flush worker run processes pending windows (pointer -> tail, in
    # order) for at most this many wall-clock seconds; if the gap is not
    # drained and the session is idle (stopped) a successor worker
    # continues where it left off. Bounds any single run.
    session_catchup_budget_seconds: int = 900
    # digest budgets (chars; ~4 chars/token)
    # Digest budgets (build_digest). User prompts and assistant prose are the
    # high-value signals: almost never truncated, only paste-bombs (huge
    # pasted error dumps) and the largest documents. Tool fragments stay
    # small by design — detail is reachable via the span drill-down.
    session_prompt_chars: int = 12000    # per user prompt (head cut)
    session_assistant_chars: int = 8000  # per assistant message (head+tail)
    session_tool_arg_chars: int = 400    # per tool call (head+tail)
    session_tool_result_chars: int = 400  # per tool result (head+tail)
    # Whole-window digest cap: head + gap marker + tail. Separate from the
    # assistant budget on purpose (an 8k assistant message is legitimate
    # content, not a reason to elide the window around it).
    session_digest_max_chars: int = 24000
    # SessionStart context injection (off-path retrieval is the MCP tools)
    session_context_enabled: bool = True
    session_context_max_chars: int = 2000
    # Semantic search: configurable embedder. `local` runs the model on
    # this machine (device = GPU index, or "cpu"); `api` calls an
    # OpenAI-compatible /embeddings endpoint; `off` disables the semantic
    # axis (search stays FTS-only). Vectors are cached in the store's
    # `embeddings` table (lazy: targets are embedded on first use).
    embed_provider: str = "local"
    embed_model: str = "BAAI/bge-large-en-v1.5"
    embed_device: str = "1"
    embed_api_base: str = ""
    embed_api_key: str = ""
    embed_batch: int = 32
    # Workspace-state projection into <workspace>/AGENTS.local.md.
    # OFF by default: that file is DSH's instruction file, and DSH
    # re-projects any change into every running session as an "instruction
    # update" — a churning machine surface riding an instructions channel
    # is a per-window token tax. The state rides the SessionStart
    # injection envelope instead (boot-time catch-up) and is fetched
    # on demand mid-session (memory_get_workspace_state / `memor state
    # show`). Opt in here (or `memor state project`) for other tooling.
    session_state_project_file: bool = False
    # P1 dedup backstop: at flush promotion, a new-item candidate is
    # compared (cosine, same workspace+category) against the active rows;
    # at/above the threshold it updates the existing row in place instead
    # of creating a paraphrase-duplicate row. Off, or no embedder, falls
    # back to exact-key promotion only.
    state_promote_dedup: bool = True
    state_promote_merge_threshold: float = 0.80
    # Reference categories (decisions, conventions, ...) are records, not
    # tasks: related-but-distinct rows share vocabulary and reach ~0.87,
    # while true re-descriptions sit at 0.93+ (live-measured). Decaying
    # categories (open_item/problem) get the lower bar — same-item pairs
    # measured 0.95+ against 0.69 for distinct items.
    state_promote_merge_threshold_ref: float = 0.90
    # lazy temporal rollups (day->week->month), query-with-caching
    temporal_rollups_enabled: bool = True
    # state polling (A1): default ON for existing stores; `memor switchover`
    # flips it off as part of the session-memory regime.
    verify_enabled: bool = True

    # Trusted-actor semantics for deliberate writes: records asserted by an
    # agent/harness (source_type="agent") become current like human
    # assertions (provenance + confidence stay visible) instead of being
    # parked in the human review queue for lacking cited evidence. Only
    # LLM-derived candidates stay evidence-gated. Set False to revert agent
    # writes to the old evidence-gated queue behavior.
    trust_agent_writes: bool = True
    redact: bool = True
    redact_extra_patterns: list[str] = field(default_factory=list)
    problem_capture_shell: bool = False
    max_output_chars: int = 4096
    machine: str = ""

    def __post_init__(self) -> None:
        if self.data_dir is None:
            self.data_dir = default_data_dir()

    @property
    def db_path(self) -> Path:
        return self.data_dir / "memor.db"

    @property
    def events_dir(self) -> Path:
        return self.data_dir / "events"

    @property
    def system_dir(self) -> Path:
        return self.data_dir / "system"

    @property
    def reference_dir(self) -> Path:
        return self.data_dir / "reference"

    @property
    def hooks_dir(self) -> Path:
        return self.data_dir / "git-hooks"

    def expand(self, p: str) -> Path:
        return Path(os.path.expanduser(p))

    def project_paths(self) -> list[tuple[str, Path]]:
        out = []
        for p in self.projects:
            for path in p.paths:
                out.append((p.name, self.expand(path)))
        return out

    def find_project_by_path(self, path: str | Path) -> str | None:
        path = Path(os.path.expanduser(str(path)))
        best, best_len = None, -1
        for name, p in self.project_paths():
            try:
                if path == p or p in path.parents:
                    if len(str(p)) > best_len:
                        best, best_len = name, len(str(p))
            except Exception:
                continue
        return best

    def find_project_by_remote(self, remote_url: str) -> str | None:
        """Match a git remote URL against registered project remotes. This is
        what keeps project identity stable across clones and machines."""
        if not remote_url:
            return None
        target = normalize_remote(remote_url)
        for p in self.projects:
            for r in p.remotes:
                if normalize_remote(r) == target:
                    return p.name
        return None

    def save(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        data = {
            "data_dir": str(self.data_dir),
            "projects": [p.__dict__ for p in self.projects],
            "watch_paths": self.watch_paths,
            "fs_interval": self.fs_interval,
            "net_interval": self.net_interval,
            "docker_snapshot_interval": self.docker_snapshot_interval,
            "consolidate_interval": self.consolidate_interval,
            "verify_interval": self.verify_interval,
            "episode_idle_gap": self.episode_idle_gap,
            "digest_interval": self.digest_interval,
            "projection_commit_interval": self.projection_commit_interval,
            "max_store_size_bytes": self.max_store_size_bytes,
            "retention_months": self.retention_months,
            "consolidate_batch": self.consolidate_batch,
            "llm_model": self.llm_model,
            "llm_enabled": self.llm_enabled,
            "llm_base_url": self.llm_base_url,
            "llm_api_key": self.llm_api_key,
            "llm_provider_only": self.llm_provider_only,
            "llm_autosync": self.llm_autosync,
            "llm_sync_interval": self.llm_sync_interval,
            "llm_max_tokens": self.llm_max_tokens,
            "ignore_command_prefixes": self.ignore_command_prefixes,
            "session_journal_enabled": self.session_journal_enabled,
            "session_harnesses": self.session_harnesses,
            "session_dsh_root": self.session_dsh_root,
            "session_window_steps": self.session_window_steps,
            "session_subagent_window_steps": self.session_subagent_window_steps,
            "session_chain_width": self.session_chain_width,
            "session_execution_mode": self.session_execution_mode,
            "session_max_flush_retries": self.session_max_flush_retries,
            "session_catchup_budget_seconds": self.session_catchup_budget_seconds,
            "session_assistant_chars": self.session_assistant_chars,
            "session_tool_arg_chars": self.session_tool_arg_chars,
            "session_tool_result_chars": self.session_tool_result_chars,
            "session_prompt_chars": self.session_prompt_chars,
            "session_digest_max_chars": self.session_digest_max_chars,
            "session_context_enabled": self.session_context_enabled,
            "session_context_max_chars": self.session_context_max_chars,
            "session_state_project_file": self.session_state_project_file,
            "state_promote_dedup": self.state_promote_dedup,
            "state_promote_merge_threshold":
                self.state_promote_merge_threshold,
            "state_promote_merge_threshold_ref":
                self.state_promote_merge_threshold_ref,
            "embed_provider": self.embed_provider,
            "embed_model": self.embed_model,
            "embed_device": self.embed_device,
            "embed_api_base": self.embed_api_base,
            "embed_api_key": self.embed_api_key,
            "embed_batch": self.embed_batch,
            "temporal_rollups_enabled": self.temporal_rollups_enabled,
            "verify_enabled": self.verify_enabled,
            "trust_agent_writes": self.trust_agent_writes,
            "redact": self.redact,
            "redact_extra_patterns": self.redact_extra_patterns,
            "problem_capture_shell": self.problem_capture_shell,
            "max_output_chars": self.max_output_chars,
            "machine": self.machine,
        }
        (self.data_dir / "memor.json").write_text(json.dumps(data, indent=2) + "\n")

    @classmethod
    def load(cls, data_dir: Path | None = None) -> "Config":
        data_dir = (Path(data_dir).expanduser() if data_dir else default_data_dir())
        cfg = cls(data_dir=data_dir)
        f = data_dir / "memor.json"
        if f.exists():
            try:
                data = json.loads(f.read_text())
                cfg.projects = [ProjectRef(**p) for p in data.get("projects", [])]
                cfg.watch_paths = data.get("watch_paths", cfg.watch_paths)
                for k in ("fs_interval", "net_interval", "docker_snapshot_interval",
                          "consolidate_interval", "verify_interval",
                          "episode_idle_gap", "digest_interval",
                          "projection_commit_interval",
                          "max_store_size_bytes",
                          "retention_months", "consolidate_batch",
                          "llm_model", "llm_enabled", "llm_base_url", "llm_api_key",
                           "llm_provider_only",
                          "llm_autosync", "llm_sync_interval",
                          "llm_max_tokens", "redact",
                          "ignore_command_prefixes",
                          "session_journal_enabled", "session_harnesses",
                          "session_dsh_root", "session_window_steps",
                          "session_subagent_window_steps",
                          "session_chain_width", "session_execution_mode",
                          "session_max_flush_retries",
                          "session_catchup_budget_seconds",
                          "session_assistant_chars", "session_tool_arg_chars",
                          "session_tool_result_chars", "session_prompt_chars",
                          "session_digest_max_chars",
                          "session_context_enabled",
                          "session_context_max_chars",
                          "session_state_project_file",
                          "state_promote_dedup", "state_promote_merge_threshold",
                          "state_promote_merge_threshold_ref",
                          "embed_provider", "embed_model", "embed_device",
                          "embed_api_base", "embed_api_key", "embed_batch",
                          "temporal_rollups_enabled", "verify_enabled",
                          "trust_agent_writes",
                          "redact_extra_patterns", "problem_capture_shell",
                          "max_output_chars", "machine"):
                    if k in data:
                        setattr(cfg, k, data[k])
            except Exception:
                pass
        cfg.data_dir = data_dir
        return cfg