"""End-to-end tests for memor (rules-only; no LLM, no network)."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from memor.cli import cmd_doctor, cmd_export, cmd_import, cmd_setup, cmd_digest
from memor.config import Config
from memor.models import Candidate, Scope
from memor.store import Store

MACH = "testmachine"


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMOR_DATA_DIR", str(tmp_path))
    cfg = Config.load()
    cfg.machine = MACH
    store = Store(cfg.data_dir)
    return cfg, store


def ev(store, **kw):
    base = dict(source="shell", kind="command", machine=MACH, command="true")
    base.update(kw)
    return store.append_event(**base)


# --------------------------------------------------------------------- store

class TestStore:
    def test_append_event_and_jsonl_mirror(self, env):
        cfg, st = env
        e = st.append_event("shell", "command", machine=MACH,
                            project="p1", cwd="/tmp", command="ls -la")
        assert st.get_event(e["id"])["command"] == "ls -la"
        jsonl = cfg.events_dir / (e["ts"][:7] + ".jsonl")
        assert jsonl.exists()
        lines = [json.loads(l) for l in jsonl.read_text().splitlines() if l.strip()]
        assert any(l["id"] == e["id"] and l["command"] == "ls -la" for l in lines)

    def test_events_since_inclusive_and_ordered(self, env):
        cfg, st = env
        a = st.append_event("shell", "command", machine=MACH, command="first")
        b = st.append_event("shell", "command", machine=MACH, command="second")
        got = st.events_since(a["ts"])
        assert [e["id"] for e in got] == [a["id"], b["id"]]  # inclusive >=

    def test_fts_search(self, env):
        cfg, st = env
        st.append_event("shell", "command", machine=MACH,
                        command="kubectl apply -f zebra-gateway.yaml")
        hits = st.search_events("zebra-gateway")
        assert any("zebra-gateway" in (h.get("command") or "") for h in hits)

    def test_supersede_chain_a_b_a(self, env):
        cfg, st = env
        kw = dict(scope_project="p", scope_component="web", scope_machine=None,
                  subject="service:web", kind="state", confidence="confirmed",
                  source_type="consolidation")
        a = st.insert_fact(claim="web is running", status="current",
                       valid_from="2026-08-23T00:00:00Z", **kw)
        b = st.supersede(st.get_fact(a), {**kw, "claim": "web is stopped",
                                          "valid_from": "2026-08-23T00:01:00Z"})
        a2 = st.supersede(st.get_fact(b), {**kw, "claim": "web is running",
                                           "valid_from": "2026-08-23T00:02:00Z"})
        fa, fb, fa2 = st.get_fact(a), st.get_fact(b), st.get_fact(a2)
        assert fa["status"] == "superseded" and fa["valid_until"]
        assert fb["status"] == "superseded" and fb["supersedes_id"] == a
        assert fa2["status"] == "current" and fa2["supersedes_id"] == b
        cur = st.current_fact("p", "web", "service:web")
        assert cur["id"] == a2
        hist = st.fact_history("service:web", project="p", component="web")
        assert [f["id"] for f in hist] == [a, b, a2]


# -------------------------------------------------------------------- rules

class TestRules:
    def test_systemctl_with_user_flag(self, env):
        cfg, st = env
        e = ev(st, command="systemctl --user start web", project="demo")
        from memor.consolidate.rules import extract
        cands = extract([st.get_event(e["id"])])
        assert len(cands) == 1
        c = cands[0]
        assert c.subject == "service:web" and c.confidence == "confirmed"
        assert "is running" in c.claim and c.evidence == [e["id"]]
        assert c.scope.component == "web" and c.scope.project == "demo"

    def test_docker_compose_up(self, env):
        cfg, st = env
        e = ev(st, source="agent", kind="tool_call", command="docker compose up web")
        from memor.consolidate.rules import extract
        cands = extract([st.get_event(e["id"])])
        assert cands and cands[0].subject == "container:web"
        assert "docker up" in cands[0].claim

    def test_port_change(self, env):
        cfg, st = env
        e = st.append_event("network", "port_change", machine=MACH,
                            command="ss -tulpn diff",
                            payload={"changes": [
                                {"change": "bound", "port": 8080, "addr": "0.0.0.0",
                                 "proto": "tcp", "process": "python"}]})
        from memor.consolidate.rules import extract
        cands = extract([st.get_event(e["id"])])
        kinds = {c.kind for c in cands}
        assert any(c.subject == "port:8080" and "8080" in c.claim for c in cands)
        assert "relation" in kinds  # python listens_on port:8080

    def test_file_change_kv_drift(self, env):
        cfg, st = env
        e = st.append_event("filesystem", "file_change", machine=MACH,
                            command="inotify ~/.config/memor/ports.conf",
                            payload={"path": "/home/u/.config/memor/ports.conf",
                                     "action": "modified",
                                     "diff": "--- a\n+++ b\n-PORT=8000\n+PORT=8080\n"})
        from memor.consolidate.rules import extract
        cands = extract([st.get_event(e["id"])])
        assert any("PORT" in c.claim and "8000" in c.claim and "8080" in c.claim
                   for c in cands)

    def test_git_commit(self, env):
        cfg, st = env
        e = st.append_event("git", "commit", machine=MACH, project="demo",
                            command="commit abc",
                            payload={"repo": "/repo", "sha": "abc123",
                                     "subject": "fix: login", "branch": "main"})
        from memor.consolidate.rules import extract
        cands = extract([st.get_event(e["id"])])
        assert any(c.subject == "repo:demo" and "fix: login" in c.claim
                   and "main" in c.claim for c in cands)


# -------------------------------------------------------------- consolidation

class TestConsolidate:

    def test_reversal_and_markdown(self, env):
        cfg, st = env
        from memor.consolidate.apply import run_consolidation
        from memor.consolidate.markdown import regenerate
        st.append_event("human", "project_registered", project="demo",
                        command="registered project demo")
        cwd = str(cfg.data_dir)
        for cmd in ("systemctl --user start web", "systemctl --user stop web",
                    "systemctl --user start web"):
            st.append_event("shell", "command", machine=MACH, project="demo",
                            cwd=cwd, command=cmd, payload={"exit": 0})
            run_consolidation(st, cfg, extractor="rules")
        chain = st.fact_history("service:web", project="demo", component="web")
        by_claim: dict[str, list] = {}
        for f in chain:
            by_claim.setdefault(f["claim"], []).append(f)
        running = by_claim["systemd service web is running"]
        stopped = by_claim["systemd service web is stopped"]
        assert len(running) == 2 and len(stopped) == 1
        stop = stopped[0]
        # identify the chain regardless of same-second valid_from ordering
        first_run = next(f for f in running if stop["supersedes_id"] == f["id"])
        last_run = next(f for f in running if f["id"] != first_run["id"])
        assert last_run["supersedes_id"] == stop["id"]
        assert first_run["status"] == stop["status"] == "superseded"
        assert last_run["status"] == "current"
        current = last_run

        # evidence trail: the current fact cites its raw event
        evs = st.evidence_for(current["id"])
        assert evs and evs[0]["event"]["command"] == "systemctl --user start web"

        changed = regenerate(st, cfg, commit=False)
        assert changed
        idx = (cfg.system_dir / "INDEX.md").read_text()
        assert "demo" in idx
        proj = (cfg.system_dir / "projects" / "demo.md").read_text()
        assert "is running" in proj
        comp = cfg.reference_dir / "projects" / "demo" / "components" / "web.md"
        assert comp.exists()
        hist = comp.read_text()
        assert "superseded" in hist and "is stopped" in hist

    def test_dry_run_makes_no_changes(self, env):
        cfg, st = env
        from memor.consolidate.apply import run_consolidation
        st.append_event("shell", "command", machine=MACH, project="demo",
                        command="systemctl --user start web")
        rep = run_consolidation(st, cfg, extractor="rules", dry_run=True)
        assert rep["dry_run"] and rep["candidates"] == 1
        assert st.list_facts(project="demo") == []
        assert st.meta_get("consolidate.cursor") is None

    def test_cursor_idempotent_rerun(self, env):
        cfg, st = env
        from memor.consolidate.apply import run_consolidation
        st.append_event("shell", "command", machine=MACH, project="demo",
                        command="systemctl --user start web")
        run_consolidation(st, cfg, extractor="rules")
        n1 = len(st.list_facts(project="demo"))
        rep = run_consolidation(st, cfg, extractor="rules")
        assert rep["report"] == "nothing new"
        assert len(st.list_facts(project="demo")) == n1  # no re-apply

    def test_contradiction_and_low_confidence_queued(self, env):
        cfg, st = env
        from memor.consolidate.apply import apply_candidates
        e1 = ev(st, command="systemctl --user start web", project="demo")
        e2 = ev(st, command="systemctl --user stop web", project="demo",
                source="agent", kind="tool_call")
        st.append_event("shell", "command", machine=MACH, project="demo",
                        command="systemctl --user start web",
                        payload={"exit": 0})
        apply_candidates(st, cfg, [
            Candidate(kind="fact", scope=Scope(project="demo", machine=MACH,
                                               component="web"),
                      subject="service:web", claim="systemd service web is running",
                      fact_kind="state", confidence="confirmed",
                      valid_from="2026-08-23T00:00:00Z",
                      evidence=[e1["id"]]),
        ])
        # (a) confident but flagged contradiction -> queued, old stays current
        apply_candidates(st, cfg, [
            Candidate(kind="fact", scope=Scope(project="demo", machine=MACH,
                                               component="web"),
                      subject="service:web", claim="systemd service web is stopped",
                      fact_kind="state", confidence="confirmed", contradiction=True,
                      valid_from="2026-08-23T00:01:00Z",
                      evidence=[e2["id"]]),
        ])
        cur = st.current_fact("demo", "web", "service:web")
        assert "is running" in cur["claim"]  # no silent overwrite
        reviews = st.open_reviews()
        assert any(r["category"] == "contradiction" for r in reviews)
        # (b) low confidence -> unknown status + review
        apply_candidates(st, cfg, [
            Candidate(kind="fact", scope=Scope(project="demo", machine=MACH),
                      subject="queue:web", claim="web uses a redis queue",
                      confidence="inferred", evidence=[e1["id"]]),
        ])
        assert any(f["status"] == "unknown" for f in st.list_facts(project="demo"))
        # accept the contradiction -> it becomes current, supersedes the old
        rev = next(r for r in st.open_reviews() if r["category"] == "contradiction")
        from memor.consolidate.apply import review_accept
        fid = review_accept(st, rev["id"])
        cur2 = st.get_fact(fid)
        assert cur2["status"] == "current" and "stopped" in cur2["claim"]
        assert st.get_review(rev["id"])["status"] == "resolved"

    def test_uncited_candidate_dropped(self, env):
        cfg, st = env
        from memor.consolidate.apply import apply_candidates
        res = apply_candidates(st, cfg, [
            Candidate(kind="fact", scope=Scope(project="demo", machine=MACH),
                      subject="x", claim="fabricated claim", confidence="confirmed",
                      evidence=["evt_doesnotexist"]),
        ])
        assert res.dropped and st.list_facts(project="demo") == []


# ---------------------------------------------------------------------- mcp

class TestMcp:
    @pytest.fixture
    def wired(self, env, monkeypatch):
        from memor import mcp_server as m
        monkeypatch.setattr(m, "_cfg", env[0])
        cfg, st = env
        st.append_event("human", "project_registered", project="demo",
                        command="registered project demo")
        st.append_event("shell", "command", machine=MACH, project="demo",
                        command="systemctl --user start web")
        from memor.consolidate.apply import run_consolidation
        run_consolidation(st, cfg, extractor="rules")
        return cfg, st

    def test_tool_functions(self, wired):
        from memor import mcp_server as m
        cur = json.loads(m.tool_get_current_state("service:web"))
        assert any("is running" in f["claim"] for f in cur["current"])
        hist = json.loads(m.tool_get_history("service:web"))
        subj = hist["subjects"]["service:web"]
        assert subj[0]["claim"].endswith("is running")
        ctx = m.tool_get_project_context("demo")
        assert "## Current state" in ctx and "demo" in ctx
        ev = json.loads(m.tool_get_evidence(subj[0]["id"]))
        assert ev["evidence"] and ev["evidence"][0]["event"]["command"].startswith("systemctl")
        rec = json.loads(m.tool_get_recent_changes("demo", MACH, 24 * 30))
        assert rec["event_counts_by_source"]
        # R3: unified {hits} shape; fts mode keeps this test embedder-free
        s = json.loads(m.tool_search("systemctl", mode="fts"))
        assert any(h["kind"] == "event" for h in s["hits"])

    def test_stdio_roundtrip(self, env, tmp_path):
        """Boot `memor serve` as a real MCP stdio client sees it."""
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
        import asyncio

        memor_bin = (Path(__file__).resolve().parent.parent
                     / ".venv" / "bin" / "memor")
        if not memor_bin.exists():  # non-venv install
            import shutil
            memor_bin = shutil.which("memor") or str(memor_bin)

        async def run():
            params = StdioServerParameters(
                command=str(memor_bin),
                args=["serve"], env={**os.environ})
            async with stdio_client(params) as (r, w):
                async with ClientSession(r, w) as session:
                    await session.initialize()
                    tools = await session.list_tools()
                    names = {t.name for t in tools.tools}
                    assert {"memory_search", "memory_get_current_state",
                            "memory_get_history", "memory_get_related",
                            "memory_get_decisions", "memory_get_evidence",
                            "memory_get_project_context",
                            "memory_get_recent_changes",
                            # write surface (C1)
                            "memory_add_fact", "memory_add_decision",
                            "memory_add_relation", "memory_add_note",
                            "memory_correct", "memory_retract",
                            # chunk 8/9
                            "memory_get_timeline", "memory_get_recent_episodes",
                            "memory_get_global_context"} <= names
                    res = await session.call_tool(
                        "memory_get_recent_changes", {"since_hours": 72})
                    assert isinstance(res.content[0].text, str)
                    return names

        asyncio.run(run())


# ------------------------------------------------------------------ capture

class TestCapture:
    def test_netproc_diff(self):
        from memor.capture.netproc import snapshot_listen, diff_snapshots
        prev = snapshot_listen(
            'tcp LISTEN 0 128 0.0.0.0:8080 0.0.0.0:* users:((\"python\",pid=1,fd=3))\n'
            'tcp LISTEN 0 128 127.0.0.1:3000 0.0.0.0:* users:((\"node\",pid=2,fd=4))\n')
        cur = snapshot_listen(
            'tcp LISTEN 0 128 0.0.0.0:8080 0.0.0.0:* users:((\"python\",pid=1,fd=3))\n'
            'tcp LISTEN 0 128 0.0.0.0:9090 0.0.0.0:* users:((\"java\",pid=3,fd=5))\n')
        changes = diff_snapshots(prev, cur)
        kinds = {(c["change"], c["port"]) for c in changes}
        assert ("bound", 9090) in kinds and ("freed", 3000) in kinds
        assert not any(c["change"] == "bound" and c["port"] == 8080 for c in changes)

    def test_agent_event(self, env, monkeypatch):
        cfg, st = env
        from memor.capture.terminal import agent_event
        payload = json.dumps({
            "tool_name": "Bash",
            "tool_input": {"command": "pytest -q"},
            "tool_response": {"stdout": "3 passed", "stderr": "", "exit_code": 0},
            "cwd": str(cfg.data_dir), "session_id": "sess1",
        })
        agent_event(cfg, st, payload)
        e = st.recent_events(limit=1, source="agent")[0]
        assert e["command"] == "pytest -q" and e["session"] == "sess1"
        assert e["payload"]["output"].startswith("3 passed")
        assert e["payload"]["exit_code"] == 0

    def test_shell_event_env_handoff(self, env, monkeypatch):
        cfg, st = env
        for k in ("MEMOR_CMD", "MEMOR_EXIT", "MEMOR_T0", "MEMOR_T1",
                  "MEMOR_SESSION", "MEMOR_CWD", "MEMOR_SHELL"):
            monkeypatch.delenv(k, raising=False)
        monkeypatch.setenv("MEMOR_CMD", "git status")
        monkeypatch.setenv("MEMOR_EXIT", "0")
        monkeypatch.setenv("MEMOR_T0", "1000000000000")
        monkeypatch.setenv("MEMOR_T1", "1003500000000")
        monkeypatch.setenv("MEMOR_SESSION", "s9")
        monkeypatch.setenv("MEMOR_CWD", str(cfg.data_dir))
        monkeypatch.setenv("MEMOR_SHELL", "bash")
        from memor.capture.terminal import shell_event
        shell_event(cfg, st)
        e = st.recent_events(limit=1, source="shell")[0]
        assert e["command"] == "git status"
        assert e["payload"]["exit"] == 0 and e["payload"]["duration_ms"] == 3500

    def test_git_hook_scripts(self, env):
        cfg, st = env
        from memor.capture.githooks import install_hook_scripts
        install_hook_scripts(cfg)
        for h in ("post-commit", "post-merge", "post-checkout"):
            p = cfg.hooks_dir / h
            assert p.exists() and os.access(p, os.X_OK)
            assert "memor" in p.read_text()

    def test_claude_hook_script_swallows_errors(self, tmp_path):
        hook = Path(__file__).resolve().parents[1] / "hooks" / "memor-claude-hook.sh"
        assert hook.exists(), f"missing hook script: {hook}"
        base_env = {**os.environ, "MEMOR_DATA_DIR": str(tmp_path)}
        # disabled → immediate 0, never touches a memor binary
        r = subprocess.run(["bash", str(hook)], input="not json",
                           capture_output=True, text=True, timeout=30,
                           env={**base_env, "MEMOR_DISABLED": "1"})
        assert r.returncode == 0
        # enabled + garbage JSON → still exit 0: agent_event swallows the
        # JSONDecodeError and the script must never block the agent run
        r = subprocess.run(["bash", str(hook)], input="not json",
                           capture_output=True, text=True, timeout=30,
                           env={**base_env, "MEMOR_BIN": _memor_bin()})
        assert r.returncode == 0, r.stderr


# ----------------------------------------------------------------- git hooks

def _memor_bin() -> str:
    """The memor console script next to the test interpreter (the venv)."""
    return str(Path(sys.executable).parent / "memor")


def _git(repo: str, *args: str) -> None:
    subprocess.run(["git", "-C", repo, *args], check=True, capture_output=True,
                   text=True, timeout=60)


class TestGitHooks:
    def test_memor_invocation_never_dot(self, monkeypatch):
        """Regression: empty MEMOR_BIN used to produce `exec "."` in hooks,
        because Path("") normalizes to Path(".") which is truthy+existing."""
        monkeypatch.delenv("MEMOR_BIN", raising=False)
        monkeypatch.delenv("MEMOR_PROJECT_VENV", raising=False)
        from memor.capture import githooks
        inv = githooks._memor_invocation()
        assert inv != "."
        assert inv != ""
        if inv != "memor":
            assert os.path.exists(inv)

    def test_memor_invocation_env_overrides(self, monkeypatch):
        monkeypatch.setenv("MEMOR_BIN", _memor_bin())
        from memor.capture import githooks
        assert githooks._memor_invocation() == _memor_bin()
        monkeypatch.setenv("MEMOR_PROJECT_VENV", str(Path(sys.executable).parent.parent))
        assert githooks._memor_invocation().endswith(os.path.join("bin", "memor"))

    def test_generated_hook_uses_real_invocation(self, env, monkeypatch):
        cfg, st = env
        monkeypatch.setenv("MEMOR_BIN", _memor_bin())
        from memor.capture.githooks import install_hook_scripts
        install_hook_scripts(cfg)
        script = (cfg.hooks_dir / "post-commit").read_text()
        assert 'exec "."' not in script
        assert _memor_bin() in script
        assert '"$@"' in script  # post-checkout positional args are forwarded

    def test_collect_hook_event_all_kinds(self, tmp_path, monkeypatch):
        from memor.capture.githooks import collect_hook_event
        repo = str(tmp_path / "repo")
        subprocess.run(["git", "init", "-q", "-b", "main", repo], check=True,
                       capture_output=True, text=True)
        _git(repo, "-c", "user.name=t", "-c", "user.email=t@t",
             "commit", "--allow-empty", "-m", "first")
        monkeypatch.setenv("GIT_WORK_TREE", repo)
        ev = collect_hook_event("post-commit")
        assert ev is not None and ev["kind"] == "commit"
        assert ev["payload"]["kind"] == "commit"
        assert ev["payload"]["subject"] == "first"
        assert ev["payload"]["sha"]
        # post-checkout via positional args, as real git passes them:
        # post-checkout <prev> <ref> <flag>
        co = collect_hook_event("post-checkout", ["deadbeef", "main", "1"])
        assert co is not None and co["kind"] == "checkout"
        assert co["payload"]["to"] == "main" and co["payload"]["from"] == "deadbeef"
        # non-branch checkouts are ignored
        assert collect_hook_event("post-checkout", ["a", "v1.0", "0"]) is None
        # post-checkout via env vars (manual/test invocation)
        monkeypatch.setenv("flag", "1")
        monkeypatch.setenv("ref", "main")
        monkeypatch.setenv("prev", "")
        co = collect_hook_event("post-checkout")
        assert co is not None and co["kind"] == "checkout" and co["payload"]["to"] == "main"
        monkeypatch.setenv("flag", "0")
        assert collect_hook_event("post-checkout") is None

    def test_git_hook_end_to_end(self, env, monkeypatch, tmp_path):
        """Real hooks installed on a real repo: commit/checkout/merge events
        land in the store (this path was silently dead before the fix)."""
        cfg, st = env
        monkeypatch.setenv("MEMOR_BIN", _memor_bin())
        repo = str(tmp_path / "repo")
        subprocess.run(["git", "init", "-q", "-b", "main", repo], check=True,
                       capture_output=True, text=True)
        from memor.capture.githooks import install_hooks
        assert install_hooks(cfg, st, [repo]) == [repo]

        (tmp_path / "repo" / "a.txt").write_text("x\n")
        _git(repo, "add", "-A")
        _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "add a")
        _git(repo, "checkout", "-q", "-b", "feat")
        (tmp_path / "repo" / "b.txt").write_text("y\n")
        _git(repo, "add", "-A")
        _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "add b")
        _git(repo, "checkout", "-q", "main")
        _git(repo, "-c", "user.name=t", "-c", "user.email=t@t",
             "merge", "--no-ff", "-q", "feat", "-m", "merge feat")

        git_events = [e for e in st.events_since("1970-01-01T00:00:00Z", source="git")]
        kinds = [e["kind"] for e in git_events]
        commits = [e for e in git_events if e["kind"] == "commit"]
        assert "commit" in kinds and "checkout" in kinds and "merge" in kinds
        # git fires post-merge (not post-commit) for a merge commit
        assert [e["payload"]["subject"] for e in commits] == ["add a", "add b"]
        merges = [e for e in git_events if e["kind"] == "merge"]
        assert merges and merges[-1]["payload"]["subject"] == "merge feat"
        checkouts = [e for e in git_events if e["kind"] == "checkout"]
        assert any(e["payload"]["to"] == "feat" for e in checkouts)
        assert any(e["payload"]["to"] == "main" for e in checkouts)

    def test_store_own_repo_is_self_excluded(self, env, monkeypatch):
        """Even if hooks are pointed at the store's own projection repo, its
        commits must not re-enter the evidence stream (self-observation loop)."""
        cfg, st = env
        monkeypatch.setenv("MEMOR_BIN", _memor_bin())
        from memor.capture.githooks import install_hook_scripts
        install_hook_scripts(cfg)
        subprocess.run(["git", "init", "-q", "-b", "main", str(cfg.data_dir)],
                       check=True, capture_output=True, text=True)
        subprocess.run(["git", "-C", str(cfg.data_dir),
                        "config", "core.hooksPath", str(cfg.hooks_dir)],
                       check=True, capture_output=True, text=True)
        (cfg.data_dir / "probe.md").write_text("x\n")
        subprocess.run(["git", "-C", str(cfg.data_dir), "add", "-A"],
                       check=True, capture_output=True, text=True)
        subprocess.run(["git", "-C", str(cfg.data_dir), "-c", "user.name=t",
                        "-c", "user.email=t@t", "commit", "-q", "-m", "self commit"],
                       check=True, capture_output=True, text=True)
        commits = [e for e in st.events_since("1970-01-01T00:00:00Z", source="git")
                   if e["kind"] == "commit"]
        assert commits == []

    def test_git_events_consolidate_to_facts(self, env, monkeypatch, tmp_path):
        cfg, st = env
        monkeypatch.setenv("MEMOR_BIN", _memor_bin())
        repo = str(tmp_path / "repo")
        subprocess.run(["git", "init", "-q", "-b", "main", repo], check=True,
                       capture_output=True, text=True)
        from memor.capture.githooks import install_hooks
        install_hooks(cfg, st, [repo])
        (tmp_path / "repo" / "a.txt").write_text("x\n")
        _git(repo, "add", "-A")
        _git(repo, "-c", "user.name=t", "-c", "user.email=t@t",
             "commit", "-q", "-m", "fix: login")
        from memor.consolidate.apply import run_consolidation
        run_consolidation(st, cfg, extractor="rules")
        facts = st.list_facts(project="repo")
        assert any("fix: login" in f["claim"] for f in facts)


# ------------------------------------------------- consolidation concurrency

class TestConsolidationLock:
    def test_concurrent_run_skips(self, env):
        import fcntl
        cfg, st = env
        st.append_event("shell", "command", machine=MACH, project="demo",
                       command="systemctl --user start web")
        from memor.consolidate.apply import run_consolidation
        lock = open(cfg.data_dir / "consolidate.lock", "a+")
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            rep = run_consolidation(st, cfg, extractor="rules")
            assert "skipped" in rep["report"]
            assert st.list_facts(project="demo") == []
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)
            lock.close()
        rep = run_consolidation(st, cfg, extractor="rules")
        assert "skipped" not in rep["report"]
        assert st.list_facts(project="demo")


# ------------------------------------------------- daemon consolidation loop

class TestDaemonConsolidation:
    def test_consolidate_once_end_to_end(self, env, monkeypatch):
        cfg, st = env
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        st.append_event("shell", "command", machine=MACH, project="demo",
                        command="systemctl --user start web")
        from memor.capture.daemon import consolidate_once
        rep = consolidate_once(cfg)
        assert rep.get("events", 0) >= 1
        assert st.list_facts(project="demo")
        assert (cfg.system_dir / "INDEX.md").exists()

    def test_consolidate_once_no_new_events(self, env, monkeypatch):
        cfg, st = env
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        from memor.capture.daemon import consolidate_once
        assert consolidate_once(cfg)["report"] == "nothing new"

# ------------------------------------------------- design-review fixes (F/B)

class TestDesignReview:

    def test_f1_same_second_ordering(self, env):
        """Same-second bursts must consolidate in causal (insertion) order."""
        cfg, st = env
        ts = "2026-08-24T00:00:00Z"
        a = st.append_event("shell", "command", machine=MACH, ts=ts,
                            command="first", project="p")
        b = st.append_event("shell", "command", machine=MACH, ts=ts,
                            command="second", project="p")
        c = st.append_event("shell", "command", machine=MACH, ts=ts,
                            command="third", project="p")
        got = st.events_since("2026-08-23T00:00:00Z", project="p")
        assert [e["id"] for e in got] == [a["id"], b["id"], c["id"]]
        got_desc = st.recent_events(limit=10, source="shell")
        assert [e["command"] for e in got_desc][:3] == ["third", "second", "first"]

    def test_f2_llm_valid_from_anchored_to_evidence(self, env):
        cfg, st = env
        old = st.append_event("shell", "command", machine=MACH, project="p",
                              ts="2026-08-01T10:00:00Z", command="old work")
        from memor.consolidate.apply import apply_candidates
        from memor.models import Candidate, Scope
        apply_candidates(st, cfg, [
            Candidate(kind="fact", scope=Scope(project="p", machine=MACH),
                      subject="s", claim="something happened back in august",
                      confidence="confirmed", valid_from=None,
                      evidence=[old["id"]], origin="llm"),
        ])
        f = st.current_fact("p", None, "s")
        assert f["valid_from"] == "2026-08-01T10:00:00Z"  # not sync time

    def test_b3_component_hints_registered(self, env):
        cfg, st = env
        from memor.consolidate.extract import apply_component_hints
        from memor.models import Scope
        apply_component_hints(st, Scope(project="p"), {
            "component_hints": {"qwen-server": "inference endpoint",
                                "cache": "", "": "ignored"}})
        ent = st.find_entity("qwen-server")
        assert ent and ent["description"] == "inference endpoint"
        # existing descriptions are never clobbered
        apply_component_hints(st, Scope(project="p"),
                              {"component_hints": {"qwen-server": "new name"}})
        assert st.find_entity("qwen-server")["description"] == "inference endpoint"

    def test_f3_scoped_run_does_not_skip_other_projects(self, env):
        cfg, st = env
        from memor.consolidate.apply import run_consolidation
        st.append_event("shell", "command", machine=MACH, project="demo",
                        ts="2026-08-01T00:00:01Z", command="systemctl --user start web")
        st.append_event("shell", "command", machine=MACH, project="other",
                        ts="2026-08-01T00:00:02Z", command="systemctl --user start api")
        st.append_event("shell", "command", machine=MACH, project="demo",
                        ts="2026-08-01T00:00:03Z", command="systemctl --user start web2")
        run_consolidation(st, cfg, extractor="rules", scope_project="demo")
        assert st.list_facts(project="demo")
        assert st.list_facts(project="other") == []  # not yet
        rep = run_consolidation(st, cfg, extractor="rules")  # full run
        assert "nothing new" not in rep["report"]
        assert st.list_facts(project="other")  # not permanently skipped

    def test_f11_remote_url_project_matching(self, env, tmp_path, monkeypatch):
        from memor.config import normalize_remote, ProjectRef
        assert normalize_remote("git@github.com:me/ai.git") == "github.com/me/ai"
        assert normalize_remote("https://github.com/me/ai") == "github.com/me/ai"
        assert normalize_remote("ssh://git@github.com:22/me/ai.git") == "github.com/me/ai"
        cfg, st = env
        cfg.projects.append(ProjectRef(name="local-ai",
                                       remotes=["git@github.com:me/ai.git"]))
        repo = tmp_path / "clone-anywhere"
        repo.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True,
                       capture_output=True, text=True)
        subprocess.run(["git", "-C", str(repo), "remote", "add", "origin",
                        "https://github.com/me/ai"], check=True, capture_output=True)
        from memor.scope import resolve_project
        assert resolve_project(str(repo / "src"), cfg) == "local-ai"
        # unregistered repo falls back to dir-name slug
        other = tmp_path / "stray"
        other.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "main", str(other)], check=True,
                       capture_output=True, text=True)
        assert resolve_project(str(other), cfg) == "stray"

    def test_f8_secrets_redacted_on_store(self, env):
        cfg, st = env
        e = st.append_event("shell", "command", machine=MACH,
                            command='curl -H "Authorization: Bearer sk_live_abcdef1234567890" https://x',
                            payload={"output": "password=supersecretpw token=ABCDEF12345678",
                                     "nested": {"lines": ["aws AKIA1234567890ABCDEF here"]}})
        stored = st.get_event(e["id"])
        assert "sk_live_abcdef1234567890" not in stored["command"]
        assert "[REDACTED:bearer]" in stored["command"]
        assert "supersecretpw" not in stored["payload"]["output"]
        assert "AKIA1234567890ABCDEF" not in stored["payload"]["nested"]["lines"][0]
        assert "bearer" in stored["payload"]["_redacted"]
        # JSONL mirror is redacted too
        from pathlib import Path
        jsonl = cfg.events_dir / (e["ts"][:7] + ".jsonl")
        assert "sk_live_abcdef1234567890" not in jsonl.read_text()

    def test_f8_clean_event_has_no_marker(self, env):
        cfg, st = env
        e = st.append_event("shell", "command", machine=MACH, command="ls -la")
        stored = st.get_event(e["id"])
        assert "_redacted" not in stored["payload"]


# ------------------------------------------------- chunk 2: model evolution

class TestModelEvolution:

    def test_b1_decision_reversal_chain(self, env):
        cfg, st = env
        from memor.consolidate.apply import apply_candidates
        from memor.models import Candidate, Scope
        e1 = st.append_event("shell", "command", machine=MACH, project="demo",
                             command="chose vllm", payload={"exit": 0})
        e2 = st.append_event("shell", "command", machine=MACH, project="demo",
                             command="chose llama.cpp", payload={"exit": 0})
        apply_candidates(st, cfg, [
            Candidate(kind="decision", scope=Scope(project="demo"),
                      subject="inference-backend", claim="use vLLM",
                      reason="throughput", confidence="confirmed",
                      evidence=[e1["id"]]),
        ])
        apply_candidates(st, cfg, [
            Candidate(kind="decision", scope=Scope(project="demo"),
                      subject="inference-backend", claim="use llama.cpp",
                      reason="vLLM crashed on this GPU", confidence="confirmed",
                      evidence=[e2["id"]]),
        ])
        decs = st.list_decisions(project="demo")
        current = [d for d in decs if d["status"] == "current"]
        superseded = [d for d in decs if d["status"] == "superseded"]
        assert len(current) == 1 and current[0]["choice"] == "use llama.cpp"
        assert len(superseded) == 1
        assert current[0]["supersedes_id"] == superseded[0]["id"]
        assert superseded[0]["valid_until"]
        # identical re-statement is a no-op
        n = len(st.list_decisions(project="demo"))
        apply_candidates(st, cfg, [
            Candidate(kind="decision", scope=Scope(project="demo"),
                      subject="inference-backend", claim="use llama.cpp",
                      confidence="confirmed", evidence=[e2["id"]]),
        ])
        assert len(st.list_decisions(project="demo")) == n
        # low-confidence contradiction is queued, not auto-applied
        e3 = st.append_event("shell", "command", machine=MACH, project="demo",
                             command="maybe sglang?", payload={"exit": 0})
        apply_candidates(st, cfg, [
            Candidate(kind="decision", scope=Scope(project="demo"),
                      subject="inference-backend", claim="use sglang",
                      confidence="inferred", evidence=[e3["id"]]),
        ])
        assert st.current_decision("demo", None, "inference-backend")["choice"] == "use llama.cpp"
        assert any(r["category"] == "decision_contradiction" for r in st.open_reviews())

    def test_b2_relations_keyed_on_entity_ids(self, env):
        cfg, st = env
        rid = st.upsert_relation("Qwen Server", "runs_on", "DesktopU")
        row = st._conn().execute("SELECT * FROM relations WHERE id=?", (rid,)).fetchone()
        assert row["subject_ent"] and row["object_ent"]
        # case-variant lookup resolves through the entity
        rels = st.relations_for("desktopu")
        assert any(r["relation"] == "runs_on" for r in rels)
        rels2 = st.relations_for("Qwen Server")
        assert any(r["object"] == "DesktopU" for r in rels2)
        # entities exist for both endpoints
        assert st.find_entity("qwen-server") is not None
        assert st.find_entity("desktopu") is not None

    def test_f5_subject_canonicalization(self, env):
        cfg, st = env
        from memor.consolidate.apply import apply_candidates
        from memor.models import Candidate, Scope
        e1 = st.append_event("shell", "command", machine=MACH, project="demo",
                             command="systemctl --user start web")
        e2 = st.append_event("shell", "command", machine=MACH, project="demo",
                             command="systemctl --user stop web")
        apply_candidates(st, cfg, [
            Candidate(kind="fact", scope=Scope(project="demo", machine=MACH, component="web"),
                      subject="service:web", claim="systemd service web is running",
                      fact_kind="state", confidence="confirmed", evidence=[e1["id"]]),
        ])
        # same thing, different phrasing of the subject -> re-targeted
        apply_candidates(st, cfg, [
            Candidate(kind="fact", scope=Scope(project="demo", machine=MACH, component="web"),
                      subject="Service:Web", claim="systemd service web is stopped",
                      fact_kind="state", confidence="confirmed", evidence=[e2["id"]]),
        ])
        hist = st.fact_history("service:web", project="demo", component="web")
        assert len(hist) == 2  # one chain, not two parallel ones
        cur = st.current_fact("demo", "web", "service:web")
        assert cur and "stopped" in cur["claim"]

    def test_f6_near_duplicate_merge(self, env):
        cfg, st = env
        from memor.consolidate.apply import apply_candidates
        from memor.models import Candidate, Scope
        e1 = st.append_event("network", "port_change", machine=MACH,
                             command="ss diff", payload={
                                 "changes": [{"change": "bound", "port": 8080,
                                             "addr": "0.0.0.0", "proto": "tcp",
                                             "process": "python"}]})
        apply_candidates(st, cfg, [
            Candidate(kind="fact", scope=Scope(machine=MACH), subject="port:8080",
                      claim="port 8080/tcp is bound by python",
                      fact_kind="state", confidence="confirmed", evidence=[e1["id"]]),
        ])
        # refinement (adds a token) -> supersede with the more specific claim
        e2 = st.append_event("network", "port_change", machine=MACH,
                             command="ss diff 2", payload={
                                 "changes": [{"change": "bound", "port": 8080,
                                             "addr": "0.0.0.0", "proto": "tcp",
                                             "process": "python worker"}]})
        apply_candidates(st, cfg, [
            Candidate(kind="fact", scope=Scope(machine=MACH), subject="port:8080",
                      claim="port 8080/tcp is bound by python worker",
                      fact_kind="state", confidence="confirmed", evidence=[e2["id"]]),
        ])
        cur = st.current_fact(None, None, "port:8080")
        assert "worker" in cur["claim"]
        # subset rephrasing (>=0.8 overlap, no new tokens) -> no-op
        e3 = st.append_event("network", "port_change", machine=MACH,
                             command="ss diff 3", payload={
                                 "changes": [{"change": "bound", "port": 8080,
                                             "addr": "0.0.0.0", "proto": "tcp",
                                             "process": "python"}]})
        n_before = len(st.fact_history("port:8080"))
        apply_candidates(st, cfg, [
            Candidate(kind="fact", scope=Scope(machine=MACH), subject="port:8080",
                      claim="port 8080/tcp is bound by python",
                      fact_kind="state", confidence="confirmed", evidence=[e3["id"]]),
        ])
        cur = st.current_fact(None, None, "port:8080")
        assert "worker" in cur["claim"]  # not replaced by the vaguer claim
        assert len(st.fact_history("port:8080")) == n_before

    def test_f4_correct_and_forget(self, env):
        cfg, st = env
        e = st.append_event("shell", "command", machine=MACH, project="demo",
                            command="x", payload={"exit": 0})
        fid = st.insert_fact(scope_project="demo", scope_component=None,
                             scope_machine=None, subject="s",
                             claim="the port is 8000", kind="configuration",
                             confidence="confirmed", source_type="human")
        st.add_evidence(fid, "shell", e["id"])
        new = st.correct_fact(fid, "the port is 8080", reason="it was always 8080")
        assert new and new != fid
        assert st.get_fact(new)["status"] == "current"
        assert st.get_fact(fid)["status"] == "superseded"
        assert st.current_fact("demo", None, "s")["claim"] == "the port is 8080"
        assert st.correct_fact("fact_nope", "x") == ""
        assert st.forget_fact(new, reason="obsolete")
        assert st.get_fact(new)["status"] == "retracted"
        assert st.current_fact("demo", None, "s") is None  # retracted = not current
        hist = st.fact_history("s", project="demo")
        assert any(f["status"] == "retracted" for f in hist)
        assert st.forget_fact("fact_nope") is False


# ------------------------------------------------- chunk 3: capture upgrades

class TestCaptureUpgrades:

    def test_f7_agent_failure_becomes_problem_fact(self, env):
        cfg, st = env
        e = st.append_event("agent", "tool_call", machine=MACH, project="demo",
                            command="pytest tests/", 
                            payload={"output": "3 failed", "stderr": "E  assert 1 == 2",
                                     "exit_code": 1})
        from memor.consolidate.rules import extract
        cands = extract([st.get_event(e["id"])])
        probs = [c for c in cands if c.fact_kind == "problem"]
        assert probs and probs[0].confidence == "confirmed"
        assert "exit 1" in probs[0].reason and "assert 1 == 2" in probs[0].reason
        assert probs[0].evidence == [e["id"]]

    def test_f7_shell_failures_opt_in(self, env):
        cfg, st = env
        e = st.append_event("shell", "command", machine=MACH, project="demo",
                            command="make build", payload={"exit": 2})
        from memor.consolidate.rules import extract
        ev = st.get_event(e["id"])
        assert all(c.fact_kind != "problem" for c in extract([ev]))
        assert any(c.fact_kind == "problem"
                   for c in extract([ev], shell_failures=True))

    def test_f9_journal_backfill_lines_and_dedupe(self, env, monkeypatch):
        cfg, st = env
        from memor.capture import journal as j
        j._THROTTLE.clear()
        lines = [
            json.dumps({"_SYSTEMD_UNIT": "qwen.service", "MESSAGE": "Started Qwen inference.",
                        "PRIORITY": "6", "__REALTIME_TIMESTAMP": "1750000000000000"}),
            json.dumps({"_SYSTEMD_UNIT": "qwen.service", "MESSAGE": "heartbeat tick",
                        "PRIORITY": "6", "__REALTIME_TIMESTAMP": "1750000001000000"}),
            json.dumps({"_SYSTEMD_UNIT": "qwen.service", "MESSAGE": "CUDA error",
                        "PRIORITY": "3", "__REALTIME_TIMESTAMP": "1750000002000000"}),
            json.dumps({"_SYSTEMD_UNIT": "qwen.service", "MESSAGE": "Started Qwen inference.",
                        "PRIORITY": "6", "__REALTIME_TIMESTAMP": "1750000003000000"}),
        ]
        emitted = set()
        n = j.process_journal_lines(lines, st, "system", emitted)
        assert n == 3  # heartbeat (info, no lifecycle prefix) filtered
        evs = [e for e in st.recent_events(limit=10, source="systemd")]
        assert len(evs) == 3  # duplicate 'Started' line deduped via emitted set
        # second pass over the same lines emits nothing new
        n2 = j.process_journal_lines(lines, st, "system", emitted)
        assert n2 == 3  # _interesting count, but _emit dedupes
        assert len([e for e in st.recent_events(limit=10, source="systemd")]) == 3

    def test_f10_content_only_for_config_paths(self, env, tmp_path):
        cfg, st = env
        from memor.capture.fs_watch import make_handler
        handler = make_handler(cfg, st, lambda: False)
        assert handler is not None

        class Ev:
            def __init__(self, path, event_type="modified"):
                self.src_path = self.dest_path = path
                self.event_type = event_type
                self.is_directory = False

        (tmp_path / "app.service").write_text("ExecStart=/x\n")
        handler.on_any_event(Ev(str(tmp_path / "app.service")))
        (tmp_path / "cache.bin").write_text("x" * 100)
        handler.on_any_event(Ev(str(tmp_path / "cache.bin")))
        evs = {e["payload"]["path"].rsplit("/", 1)[-1]: e
               for e in st.recent_events(limit=5, source="filesystem")
               if e["kind"] == "file_change"}
        assert "content" in evs["app.service"]["payload"]       # config: full content
        assert "content" not in evs["cache.bin"]["payload"]    # other: hash/size only
        assert evs["cache.bin"]["payload"]["new_hash"]

    def test_f13_session_identifiers_captured(self, env, monkeypatch):
        cfg, st = env
        for k in ("MEMOR_CMD", "MEMOR_EXIT", "MEMOR_T0", "MEMOR_T1",
                  "MEMOR_SESSION", "MEMOR_CWD", "MEMOR_SHELL",
                  "TMUX_PANE", "TERM_PROGRAM"):
            monkeypatch.delenv(k, raising=False)
        monkeypatch.setenv("MEMOR_CMD", "ls")
        monkeypatch.setenv("MEMOR_EXIT", "0")
        monkeypatch.setenv("MEMOR_SESSION", "s77")
        monkeypatch.setenv("MEMOR_CWD", str(cfg.data_dir))
        monkeypatch.setenv("MEMOR_SHELL", "bash")
        monkeypatch.setenv("TMUX_PANE", "%12")
        monkeypatch.setenv("TERM_PROGRAM", "vscode")
        from memor.capture.terminal import shell_event
        shell_event(cfg, st)
        e = st.recent_events(limit=1, source="shell")[0]
        assert e["kind"] == "command"
        assert e["payload"]["tmux_pane"] == "%12"
        assert e["payload"]["term_program"] == "vscode"
        assert "ppid" in e["payload"]

    def test_g5_session_env_snapshot_once(self, env, monkeypatch):
        cfg, st = env
        monkeypatch.setenv("MEMOR_DATA_DIR", str(cfg.data_dir))  # visible in /proc env
        for k in ("MEMOR_CMD", "MEMOR_EXIT", "MEMOR_T0", "MEMOR_T1",
                  "MEMOR_SESSION", "MEMOR_CWD", "MEMOR_SHELL"):
            monkeypatch.delenv(k, raising=False)
        monkeypatch.setenv("MEMOR_CMD", "ls")
        monkeypatch.setenv("MEMOR_EXIT", "0")
        monkeypatch.setenv("MEMOR_SESSION", "sess-env-1")
        monkeypatch.setenv("MEMOR_CWD", str(cfg.data_dir))
        monkeypatch.setenv("MEMOR_SHELL", "bash")
        from memor.capture.terminal import shell_event
        shell_event(cfg, st)
        envs = [e for e in st.recent_events(limit=20, source="shell")
                if e["kind"] == "session_env"]
        assert len(envs) == 1
        assert envs[0]["payload"]["vars"].get("MEMOR_DATA_DIR") == str(cfg.data_dir)
        # second command in the same session: no new snapshot
        monkeypatch.setenv("MEMOR_CMD", "ls again")
        shell_event(cfg, st)
        envs = [e for e in st.recent_events(limit=20, source="shell")
                if e["kind"] == "session_env"]
        assert len(envs) == 1

    def test_c2_run_wrapped(self, env, monkeypatch, capsys):
        cfg, st = env
        from memor.capture.terminal import run_wrapped
        rc = run_wrapped(cfg, st, "echo hello-memor")
        assert rc == 0
        assert "hello-memor" in capsys.readouterr().out
        e = st.recent_events(limit=1, source="shell")[0]
        assert e["payload"]["wrapped"] is True
        assert "hello-memor" in e["payload"]["output"]
        assert e["payload"]["exit"] == 0
        rc = run_wrapped(cfg, st, "exit 3")
        assert rc == 3
        e = st.recent_events(limit=1, source="shell")[0]
        assert e["payload"]["exit"] == 3


# ------------------------------------------------- chunk 4: A1 verification

class TestVerification:
    def _patch_probes(self, monkeypatch, services=None, listeners=None,
                      user_units=None, containers=None):
        import memor.capture.netproc as netproc
        import memor.capture.verify as verify
        monkeypatch.setattr(netproc, "snapshot_listen",
                           lambda text=None: listeners or [])
        monkeypatch.setattr(verify, "probe_systemd",
                           lambda units: {u: (services or {}).get(u, "inactive")
                                          for u in units})
        monkeypatch.setattr(verify, "running_user_units",
                           lambda: user_units or [])
        monkeypatch.setattr(verify, "_docker_statuses",
                           lambda: containers or {})

    def test_a1_stale_service_superseded_with_evidence(self, env, monkeypatch):
        cfg, st = env
        cfg.machine = MACH
        e = st.append_event("shell", "command", machine=MACH,
                            command="systemctl --user start websvc")
        from memor.consolidate.rules import extract
        from memor.consolidate.apply import apply_candidates
        apply_candidates(st, cfg, extract([st.get_event(e["id"])]))
        old = st.current_fact(None, "websvc", "service:websvc")
        assert old and "is running" in old["claim"]
        # reality diverges with no captured trace: the unit is inactive now
        self._patch_probes(monkeypatch, services={"websvc": "inactive"})
        from memor.capture.verify import run_verification
        rep = run_verification(st, cfg)
        cur = st.current_fact(None, "websvc", "service:websvc")
        assert cur and "stopped (verified)" in cur["claim"]
        assert st.get_fact(old["id"])["status"] == "superseded"
        evs = st.evidence_for(cur["id"])
        assert evs and evs[0]["source_type"] == "verify"
        assert evs[0]["pointer"]  # points at the verify_run event
        assert rep["changes"]

    def test_a1_bootstrap_and_port_freed(self, env, monkeypatch):
        cfg, st = env
        cfg.machine = MACH
        # a port fact that reality no longer supports
        st.insert_fact(scope_project=None, scope_component=None, scope_machine=MACH,
                       subject="port:9999", claim="port 9999/tcp is bound by foo",
                       kind="state", confidence="confirmed", source_type="consolidation")
        # a running unit with no fact yet (e.g. after a reboot)
        self._patch_probes(monkeypatch, services={"bgsvc": "active"},
                           user_units=["bgsvc"], listeners=[])
        from memor.capture.verify import run_verification
        rep = run_verification(st, cfg)
        port = st.current_fact(None, None, "port:9999")
        assert port and "no longer bound (verified)" in port["claim"]
        boot = st.current_fact(None, "bgsvc", "service:bgsvc")
        assert boot and "is running" in boot["claim"] and boot["source_type"] == "verify"
        assert rep["created"]

    def test_a1_consistent_state_is_untouched(self, env, monkeypatch):
        cfg, st = env
        cfg.machine = MACH
        st.insert_fact(scope_project=None, scope_component="websvc",
                       scope_machine=MACH, subject="service:websvc",
                       claim="systemd service websvc is running",
                       kind="state", confidence="confirmed", source_type="consolidation")
        st.insert_fact(scope_project="demo", scope_component=None, scope_machine=None,
                       subject="purpose", claim="we use postgres", kind="fact",
                       confidence="confirmed", source_type="human")
        self._patch_probes(monkeypatch, services={"websvc": "active"})
        from memor.capture.verify import run_verification
        rep = run_verification(st, cfg)
        assert rep["changes"] == [] and rep["created"] == []
        assert st.current_fact(None, "websvc", "service:websvc")["claim"].endswith("is running")
        # non-observable knowledge is never touched
        assert st.current_fact("demo", None, "purpose")["claim"] == "we use postgres"

    def test_a1_container_transitions(self, env, monkeypatch):
        cfg, st = env
        cfg.machine = MACH
        st.insert_fact(scope_project=None, scope_component="web", scope_machine=MACH,
                       subject="container:web", claim="container web is running (docker up)",
                       kind="state", confidence="confirmed", source_type="consolidation")
        self._patch_probes(monkeypatch, containers={"web": "Exited (1) 2 hours ago"})
        from memor.capture.verify import run_verification
        run_verification(st, cfg)
        cur = st.current_fact(None, "web", "container:web")
        assert cur and "stopped (verified)" in cur["claim"]


# ------------------------------------------------- chunk 5: C3 LLM sync

class _FakeLLMServer:
    """Minimal OpenAI-compatible server: /v1/models + /v1/chat/completions.
    The completion cites the first event id it was given, so the sync must
    produce a fact that passes evidence validation."""

    def __init__(self):
        import http.server

        srv = self
        self.requests = []  # parsed user-payload "events" lists, per LLM call
        self.last_req = None  # raw chat/completions body of the last call
        self.finish_reason = "stop"

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                if self.path.endswith("/models"):
                    body = json.dumps({"data": [{"id": "test-model"}]}).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(body)
                else:
                    self.send_response(404)
                    self.end_headers()

            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                req = json.loads(self.rfile.read(length) or b"{}")
                srv.last_req = req
                user = json.loads(req["messages"][-1]["content"])
                srv.requests.append(user.get("events", []))
                evs = user.get("events", [])
                eid = evs[0]["id"] if evs else "evt_nope"
                content = json.dumps({
                    "facts": [{"subject": "llm-observed",
                               "claim": "the llm noted something noteworthy",
                               "kind": "fact", "confidence": "confirmed",
                               "reason": "because the evidence says so",
                               "evidence": [eid]}],
                    "component_hints": {"llm-comp": "a component the llm named"},
                })
                body = json.dumps(
                    {"choices": [{"message": {"content": content},
                                 "finish_reason": srv.finish_reason}]}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body)

        import http.server as _hs
        self.server = _hs.ThreadingHTTPServer(("127.0.0.1", 0), H)
        import threading
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}/v1"

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def fake_llm():
    srv = _FakeLLMServer()
    yield srv.base
    srv.stop()


class TestLLMSync:

    def test_c3_sync_processes_queue_then_noop(self, env, fake_llm, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        cfg, st = env
        cfg.llm_base_url = fake_llm
        cfg.llm_model = "test-model"
        e = st.append_event("shell", "command", machine=MACH, project="demo",
                            command="systemctl --user start web")
        from memor.consolidate.apply import run_llm_sync
        rep = run_llm_sync(st, cfg)
        assert rep["provider"] == "openai-compatible"
        assert rep["events"] == 1
        assert rep["report"].startswith("1 new facts")
        f = st.current_fact("demo", None, "llm-observed")
        assert f and f["confidence"] == "confirmed"
        assert st.find_entity("llm-comp")  # B3 hints flowed through the sync
        # second run: queue drained
        rep2 = run_llm_sync(st, cfg)
        assert rep2["report"] == "nothing pending"
        # idempotent: no duplicate facts
        assert len([x for x in st.list_facts(project="demo")
                    if x["subject"] == "llm-observed"]) == 1

    def test_c3_unavailable_report(self, env, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        cfg, st = env
        # a port nothing listens on
        cfg.llm_base_url = "http://127.0.0.1:1/v1"
        e = st.append_event("shell", "command", machine=MACH, project="demo",
                            command="some work")
        from memor.consolidate.apply import run_llm_sync, llm_pending_count
        rep = run_llm_sync(st, cfg)
        assert "unavailable" in rep["report"]
        assert rep["pending"] == 1
        assert llm_pending_count(st) == 1  # still queued, nothing lost
        # queue survives: once the server is back, the same run processes it
        assert llm_pending_count(st) == 1

    def test_c3_not_configured(self, env, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        cfg, st = env
        cfg.llm_enabled = False
        st.append_event("shell", "command", machine=MACH, project="demo",
                        command="x")
        from memor.consolidate.apply import run_llm_sync
        rep = run_llm_sync(st, cfg)
        assert "not configured" in rep["report"]
        assert rep["pending"] == 1

    def test_c3_autosync_off_by_default(self, env):
        cfg, st = env
        assert cfg.llm_autosync is False
        assert cfg.llm_sync_interval == 600.0

    def test_c3_uncited_llm_claims_dropped(self, env, monkeypatch):
        """R6: a claim whose cited evidence doesn't exist is dropped at the
        apply layer (never trusted as source of truth)."""
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        cfg, st = env
        from memor.consolidate.apply import apply_candidates
        from memor.models import Candidate, Scope
        res = apply_candidates(st, cfg, [
            Candidate(kind="fact", scope=Scope(project="demo"), subject="x",
                      claim="fabricated", confidence="confirmed",
                      evidence=["evt_does_not_exist"], origin="llm"),
        ])
        assert res.dropped and st.list_facts(project="demo") == []

    def test_noise_commands_withheld_from_llm(self, env, monkeypatch):
        """cd/ls-class usage commands never enter the LLM window (they stay
        raw evidence and still count as processed, so the queue drains)."""
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        srv = _FakeLLMServer()
        try:
            cfg, st = env
            cfg.llm_base_url = srv.base
            cfg.llm_model = "test-model"
            st.append_event("shell", "command", machine=MACH, project="demo",
                            command="cd /tmp")
            st.append_event("shell", "command", machine=MACH, project="demo",
                            command="ls -la")
            e = st.append_event("shell", "command", machine=MACH, project="demo",
                                command="systemctl --user start web")
            from memor.consolidate.apply import run_llm_sync
            rep = run_llm_sync(st, cfg)
            assert rep["events"] == 3
            assert len(srv.requests) == 1
            offered = [x["id"] for x in srv.requests[0]]
            assert offered == [e["id"]]  # only the systemctl event reached it
            rep2 = run_llm_sync(st, cfg)
            assert rep2["report"] == "nothing pending"
        finally:
            srv.stop()

    def test_all_noise_window_skips_llm_call(self, env, monkeypatch):
        """A window of pure usage noise makes no LLM round-trip at all
        (matters for slow local models), but still drains the queue."""
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        srv = _FakeLLMServer()
        try:
            cfg, st = env
            cfg.llm_base_url = srv.base
            cfg.llm_model = "test-model"
            st.append_event("shell", "command", machine=MACH, project="demo",
                            command="ls -la")
            st.append_event("shell", "command", machine=MACH, project="demo",
                            command="cd /tmp")
            from memor.consolidate.apply import run_llm_sync
            rep = run_llm_sync(st, cfg)
            assert rep["events"] == 2
            assert srv.requests == []
            rep2 = run_llm_sync(st, cfg)
            assert rep2["report"] == "nothing pending"
        finally:
            srv.stop()

    def test_ignore_list_is_configurable(self, env, monkeypatch):
        """Machine-specific noise (e.g. one's LLM server start command) can
        be added to the ignore list; token boundaries are respected."""
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        srv = _FakeLLMServer()
        try:
            cfg, st = env
            cfg.llm_base_url = srv.base
            cfg.llm_model = "test-model"
            cfg.ignore_command_prefixes = ["ninfer-serve", "cd", "ls"]
            st.append_event("shell", "command", machine=MACH, project="demo",
                            command="ninfer-serve --model qwen3.8-27b -p 8000")
            st.append_event("shell", "command", machine=MACH, project="demo",
                            command="ninfer-cli complete 'hi'")  # not ignored
            from memor.consolidate.apply import run_llm_sync
            rep = run_llm_sync(st, cfg)
            assert len(srv.requests) == 1
            offered = [x["command"] for x in srv.requests[0]]
            assert offered == ["ninfer-cli complete 'hi'"]
        finally:
            srv.stop()

    def test_noise_prefixes_config_roundtrip(self, env):
        cfg, st = env
        assert cfg.ignore_command_prefixes  # sensible defaults ship enabled
        assert "cd" in cfg.ignore_command_prefixes
        cfg.ignore_command_prefixes = ["ninfer-serve"]
        cfg.save()
        loaded = Config.load()
        assert loaded.ignore_command_prefixes == ["ninfer-serve"]

    def test_llm_max_tokens_default(self, env):
        cfg, st = env
        assert cfg.llm_max_tokens == 16384

    def test_llm_max_tokens_roundtrip(self, env):
        cfg, st = env
        cfg.llm_max_tokens = 32768
        cfg.save()
        loaded = Config.load()
        assert loaded.llm_max_tokens == 32768

    def test_llm_max_tokens_sent_to_server(self, env, monkeypatch, tmp_path):
        """The configured value (not a hardcoded one) is what the endpoint
        sees; the default of 16384 applies when memor.json has no key."""
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        srv = _FakeLLMServer()
        try:
            cfg, st = env
            cfg.llm_base_url = srv.base
            cfg.llm_model = "test-model"
            cfg.llm_max_tokens = 8192
            st.append_event("shell", "command", machine=MACH, project="demo",
                            command="systemctl --user start web")
            from memor.consolidate.apply import run_llm_sync
            rep = run_llm_sync(st, cfg)
            assert rep["report"].startswith("1 new facts")
            assert srv.last_req["max_tokens"] == 8192
            # a memor.json without the key falls back to the 16384 default
            empty = tmp_path / "empty-config"
            empty.mkdir()
            assert Config.load(empty).llm_max_tokens == 16384
        finally:
            srv.stop()

    def test_truncated_llm_output_yields_no_candidates(self, env, monkeypatch,
                                                       capsys):
        """A completion that hit max_tokens (finish_reason=length) must not
        be parsed as a partial-but-valid JSON document: the window yields no
        candidates and warns loudly; raw events remain citable."""
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        srv = _FakeLLMServer()
        srv.finish_reason = "length"
        try:
            cfg, st = env
            cfg.llm_base_url = srv.base
            cfg.llm_model = "test-model"
            st.append_event("shell", "command", machine=MACH, project="demo",
                            command="systemctl --user start web")
            from memor.consolidate.apply import run_llm_sync
            rep = run_llm_sync(st, cfg)
            assert rep["candidates"] == 0
            assert st.list_facts(project="demo") == []
            assert "TRUNCATED" in capsys.readouterr().out
            # the window is consumed so a deterministic overflow can't loop
            # forever; the raw event is still citable evidence
            rep2 = run_llm_sync(st, cfg)
            assert rep2["report"] == "nothing pending"
        finally:
            srv.stop()


def test_is_noise_command_matching():
    from memor.consolidate.extract import is_noise_command
    p = ["cd", "ls", "docker compose"]
    assert is_noise_command("cd /x", p)
    assert is_noise_command("ls", p)
    assert is_noise_command("ls -la", p)
    assert not is_noise_command("lsblk", p)      # token boundary
    assert not is_noise_command("cdx", p)
    assert is_noise_command("docker compose up -d", p)  # multi-word prefix
    assert not is_noise_command("docker ps", p)
    assert not is_noise_command("", p)


def test_noise_filter_unit():
    from memor.consolidate.extract import _withhold_noise, OpenAICompatibleExtractor
    from memor.config import Config
    cfg = Config(data_dir=None)
    llm = OpenAICompatibleExtractor(cfg)  # .cfg carries the ignore list
    evs = [
        {"source": "shell", "command": "cd /tmp"},
        {"source": "shell", "command": "ls -la"},
        {"source": "shell", "command": "systemctl --user start web"},
        {"source": "agent", "command": "ls src/"},
        {"source": "git", "command": "git commit 'x'"},   # never filtered
    ]
    kept = _withhold_noise(evs, llm)
    assert [e["command"] for e in kept] == [
        "systemctl --user start web", "git commit 'x'"]


# ------------------------------------------------- chunk 6: write surface

class TestWriteSurface:
    @pytest.fixture
    def wired(self, env, monkeypatch):
        from memor import mcp_server as m
        monkeypatch.setattr(m, "_cfg", env[0])
        cfg, st = env
        st.append_event("shell", "command", machine=MACH, project="demo",
                        command="systemctl --user start web")
        yield cfg, st, m

    def test_add_fact_with_evidence_becomes_current(self, wired):
        cfg, st, m = wired
        e = st.recent_events(limit=1, source="shell")[0]
        out = json.loads(m.tool_add_fact(
            "web-server", "the web server was restarted today",
            "demo", "web", "state", "confirmed", "because it hung",
            [e["id"]]))
        assert "inserted" in out and out["inserted"]
        f = st.current_fact("demo", "web", "web-server")
        assert f and f["source_type"] == "agent"
        assert st.evidence_for(f["id"])

    def test_add_fact_agent_trusted_becomes_current(self, wired):
        """Agent/harness writes are trusted-actor assertions: even without
        cited evidence and even at low confidence they become current
        (provenance + confidence stay visible) and are NOT queued."""
        cfg, st, m = wired
        out = json.loads(m.tool_add_fact(
            "guess", "probably uses a message queue", "demo", None,
            "fact", "inferred"))
        f = st.current_fact("demo", None, "guess")
        assert f and f["source_type"] == "agent"
        assert f["confidence"] == "inferred"
        assert not any(r for r in st.open_reviews())

    def test_add_fact_agent_trust_disabled_queued(self, wired):
        """With trust_agent_writes off, agent writes revert to the old
        evidence-gated behavior: uncited/low-confidence -> not current, queued."""
        cfg, st, m = wired
        cfg.trust_agent_writes = False
        m.tool_add_fact(
            "guess2", "maybe uses a message queue", "demo", None,
            "fact", "inferred")
        assert st.current_fact("demo", None, "guess2") is None
        assert any(r["category"] == "low_confidence" for r in st.open_reviews())

    def test_add_decision_agent_trusted_becomes_current(self, wired):
        """Decisions share the trusted-actor semantics (no evidence needed)."""
        cfg, st, m = wired
        m.tool_add_decision(
            "queue-choice", "use a redis queue for fanout", "demo", None,
            ["process-local queue"], "simpler to operate", "inferred")
        d = st.current_decision("demo", None, "queue-choice")
        assert d and d["source_type"] == "agent"
        assert not any(r for r in st.open_reviews())

    def test_add_fact_long_claim_not_truncated(self, wired):
        """Regression: the MCP write layer used to silently slice trusted
        claims at 500 chars (long manifests/lessons lost their tails).
        Trusted writes store the claim in full — display-time truncation is
        the context-package's job, not the write path's."""
        cfg, st, m = wired
        long_claim = "the data consolidation moved the weights to the shared drive. " * 15
        assert len(long_claim) > 500
        m.tool_add_fact("big-record", long_claim, "demo", None, "state", "confirmed")
        f = st.current_fact("demo", None, "big-record")
        assert f and f["claim"] == long_claim

    def test_add_decision_long_choice_not_truncated(self, wired):
        cfg, st, m = wired
        long_choice = "keep large tool caches on the OS disk because exFAT cannot store them. " * 10
        assert len(long_choice) > 500
        m.tool_add_decision("cache-policy", long_choice, "demo", None,
                            None, None, "confirmed")
        d = st.current_decision("demo", None, "cache-policy")
        assert d and d["choice"] == long_choice

    def test_add_fact_bad_evidence_rejected(self, wired):
        cfg, st, m = wired
        out = json.loads(m.tool_add_fact(
            "x", "y", "demo", None, "fact", "confirmed", None,
            ["evt_does_not_exist"]))
        assert "error" in out

    def test_add_fact_contradiction_queued(self, wired):
        cfg, st, m = wired
        e = st.recent_events(limit=1, source="shell")[0]
        m.tool_add_fact("service:web", "systemd service web is running",
                        "demo", "web", "state", "confirmed", None, [e["id"]])
        m.tool_add_fact("service:web", "systemd service web is stopped",
                        "demo", "web", "state", "confirmed",
                        contradiction=None, evidence=[e["id"]]) \
            if False else None
        # contradiction-flagged write goes to review, old stays current
        from memor.models import Candidate, Scope
        from memor.consolidate.apply import apply_candidates
        apply_candidates(st, cfg, [
            Candidate(kind="fact", scope=Scope(project="demo", component="web"),
                      subject="service:web", claim="systemd service web is DOWN",
                      fact_kind="state", confidence="confirmed", contradiction=True,
                      evidence=[e["id"]], source_type="agent", origin="agent"),
        ])
        cur = st.current_fact("demo", "web", "service:web")
        assert "running" in cur["claim"]  # not silently overwritten
        assert any(r["category"] == "contradiction" for r in st.open_reviews())

    def test_add_decision_and_relation(self, wired):
        cfg, st, m = wired
        e = st.recent_events(limit=1, source="shell")[0]
        out = json.loads(m.tool_add_decision(
            "web-framework", "keep the current framework", "demo", "web",
            ["rewrite in rust"], "maintenance cost too high", "confirmed",
            [e["id"]]))
        d = st.current_decision("demo", "web", "web-framework")
        assert d and d["choice"] == "keep the current framework"
        out = json.loads(m.tool_add_relation("web", "depends_on", "postgres", [e["id"]]))
        rels = st.relations_for("web")
        assert any(r["relation"] == "depends_on" and r["object"] == "postgres"
                   for r in rels)
        # relation without evidence is refused
        out = json.loads(m.tool_add_relation("web", "uses", "redis"))
        assert "error" in out

    def test_correct_and_retract_tools(self, wired):
        cfg, st, m = wired
        e = st.recent_events(limit=1, source="shell")[0]
        m.tool_add_fact("port:8080", "port 8080/tcp is bound by python",
                        "demo", None, "state", "confirmed", None, [e["id"]])
        fid = st.current_fact("demo", None, "port:8080")["id"]
        out = json.loads(m.tool_correct(fid, "port 8080/tcp is bound by python3.12"))
        assert out["corrected"] == fid and out["now"] != fid
        assert st.current_fact("demo", None, "port:8080")["claim"].endswith("python3.12")
        fid2 = st.current_fact("demo", None, "port:8080")["id"]
        out = json.loads(m.tool_retract(fid2, "obsolete"))
        assert out.get("retracted") == fid2
        assert st.current_fact("demo", None, "port:8080") is None
        assert json.loads(m.tool_retract("fact_nope"))["error"]

    def test_f12_context_shows_transitions_and_truncates(self, env, monkeypatch):
        cfg, st = env
        from memor import mcp_server as m
        monkeypatch.setattr(m, "_cfg", cfg)
        cfg.projects.append(type(cfg.projects[0])(name="demo", paths=[],
                                                 description="demo project")
                            if cfg.projects else
                            __import__("memor.config", fromlist=["ProjectRef"]).ProjectRef(
                                name="demo", description="demo project"))
        # dates relative to now: the "→ superseded" marker renders inside the
        # "last 14 days" window, so hardcoded wall-clock dates rot (the
        # original 2026-08-15/20 dates aged out of the window in September)
        from datetime import datetime, timedelta, timezone
        now = datetime.now(timezone.utc)
        ts = lambda d: (now - timedelta(days=d)).strftime("%Y-%m-%dT%H:%M:%SZ")
        st.insert_fact(scope_project="demo", scope_component=None, scope_machine=None,
                       subject="port:9000", claim="port 9000 was bound by old-app",
                       kind="state", confidence="confirmed",
                       valid_from=ts(3), source_type="consolidation")
        old = st.current_fact("demo", None, "port:9000")
        st.insert_fact(scope_project="demo", scope_component=None, scope_machine=None,
                       subject="port:9000", claim="port 9000 was freed",
                       kind="state", confidence="confirmed",
                       supersedes_id=old["id"],
                       valid_from=ts(1), source_type="consolidation")
        st.close_fact(old["id"], ts(1))
        ctx = m.tool_get_project_context("demo")
        assert "→ superseded" in ctx  # the before-state is visible
        assert "port 9000 was bound by old-app" in ctx
        small = m.tool_get_project_context("demo", max_chars=120)
        assert len(small) < 400 and "truncated" in small


# ------------------------------------------------- chunk 7: durability

class TestDurability:

    def test_a2_compaction_rules(self, env):
        cfg, st = env
        old = st.append_event("shell", "command", machine=MACH,
                              ts="2024-01-02T03:04:05Z", command="old work",
                              project="demo")
        mid = st.append_event("shell", "command", machine=MACH,
                              ts="2024-01-02T03:04:06Z", command="referenced",
                              project="demo")
        st.add_evidence(st.insert_fact(scope_project="demo", scope_component=None,
                                       scope_machine=None, subject="s",
                                       claim="c", kind="fact",
                                       confidence="confirmed"), "shell", mid["id"])
        recent = st.append_event("shell", "command", machine=MACH, command="fresh")
        st.meta_set("consolidate.cursor", "2024-06-01T00:00:00Z")
        st.meta_set("consolidate.seen", "\n".join([old["id"], mid["id"]]))
        cutoff = "2025-01-01T00:00:00Z"
        eligible = st.eligible_for_compaction(cutoff,
                                              {old["id"], mid["id"]},
                                              st.referenced_event_ids())
        assert old["id"] in eligible
        assert mid["id"] not in eligible      # referenced by evidence
        assert recent["id"] not in eligible   # newer than cutoff
        n = st.delete_events(eligible)
        assert n == 1
        assert st.get_event(old["id"]) is None
        assert st.get_event(mid["id"]) is not None
        # FTS still consistent after bulk delete
        assert st.search_events("fresh")
        assert all("fresh" in (h.get("command") or "") for h in st.search_events("fresh"))

    def test_d1_export_import_roundtrip(self, env, tmp_path):
        import argparse
        from memor.config import Config
        cfg, st = env
        cfg.machine = "machineA"
        cfg.save()
        st.append_event("shell", "command", machine="machineA", project="demo",
                        command="systemctl --user start web")
        from memor.consolidate.apply import run_consolidation
        run_consolidation(st, cfg, extractor="rules")
        assert st.list_facts(project="demo")  # premise: there is knowledge to ship
        bundle = str(tmp_path / "a.bundle.tar.gz")
        rc = cmd_export(argparse.Namespace(out=bundle, data_dir=str(cfg.data_dir)))
        assert rc == 0 and os.path.exists(bundle)
        # import into a fresh store on "another machine"
        bdir = tmp_path / "store-b"
        cfg_b = Config(data_dir=bdir)
        rc = cmd_import(argparse.Namespace(bundle=bundle, as_machine="machineB",
                                           data_dir=str(bdir)))
        assert rc == 0
        st_b = Store(bdir)
        facts = st_b.list_facts(project="demo")
        assert facts  # A's knowledge is queryable on B
        assert any(f["scope_machine"] == "machineB" for f in facts)  # reattributed
        # re-import is additive/idempotent
        rc = cmd_import(argparse.Namespace(bundle=bundle, as_machine="machineB",
                                           data_dir=str(bdir)))
        assert rc == 0
        assert len(st_b.list_facts(project="demo")) == len(facts)

    def test_e1_doctor_depth(self, env, capsys):
        import argparse
        cmd_doctor(argparse.Namespace(data_dir=str(env[0].data_dir)))
        out = capsys.readouterr().out
        assert "integrity:" in out and "yes" in out.split("integrity:")[1][:6]
        assert "event mix:" in out
        assert "store:" in out
        assert "retention:" in out

    def test_g4_setup_discovers_projects(self, env, tmp_path, monkeypatch):
        import argparse
        cfg, st = env
        work = tmp_path / "work" / "myrepo"
        work.mkdir(parents=True)
        subprocess.run(["git", "init", "-q", "-b", "main", str(work)], check=True,
                       capture_output=True, text=True)
        subprocess.run(["git", "-C", str(work), "remote", "add", "origin",
                        "git@github.com:me/myrepo.git"], check=True, capture_output=True)
        monkeypatch.setenv("SHELL", "/bin/bash")
        rc = cmd_setup(argparse.Namespace(
            dirs=[str(tmp_path / "work")], no_claude=True, no_daemon=True,
            data_dir=str(cfg.data_dir)))
        assert rc == 0
        from memor.config import Config
        cfg2 = Config.load(cfg.data_dir)
        names = [p.name for p in cfg2.projects]
        assert "myrepo" in names  # from the remote slug, not the dir name
        proj = next(p for p in cfg2.projects if p.name == "myrepo")
        assert any("myrepo.git" in r for r in proj.remotes)


# ------------------------------------------------- chunk 8: E2/C5/A3/D2

class TestChunk8:

    def test_e2_bounded_window_processes_full_backlog(self, env):
        cfg, st = env
        cfg.consolidate_batch = 2
        for unit in ("a", "b", "c", "d", "e"):
            st.append_event("shell", "command", machine=MACH, project="demo",
                            command=f"systemctl --user start {unit}")
        from memor.consolidate.apply import run_consolidation
        r1 = run_consolidation(st, cfg, extractor="rules")
        assert r1["events"] == 2 and r1["pending"] == 3
        assert "left in queue" in r1["report"]
        r2 = run_consolidation(st, cfg, extractor="rules")
        assert r2["events"] == 2 and r2["pending"] == 1
        r3 = run_consolidation(st, cfg, extractor="rules")
        assert r3["events"] == 1 and r3["pending"] == 0
        r4 = run_consolidation(st, cfg, extractor="rules")
        assert r4["report"] == "nothing new"
        # nothing was skipped: every unit has a current fact
        for unit in ("a", "b", "c", "d", "e"):
            f = st.current_fact("demo", unit, f"service:{unit}")
            assert f and "is running" in f["claim"]

    def test_c5_parse_robustness(self):
        from memor.consolidate.extract import _parse_json, _candidates_from
        from memor.models import Scope
        ok = _parse_json('```json\n{"facts": []}\n```')
        assert ok == {"facts": []}
        assert _parse_json("the model rambled, no json here") is None
        assert _parse_json("{broken") is None
        scope = Scope(project="p")
        data = {"facts": [
            {"subject": "s", "claim": "x" * 900, "confidence": "bogus",
             "kind": "nonsense", "evidence": ["evt_1"], "reason": "r" * 900},
            {"subject": "t", "claim": "no evidence", "evidence": []},
        ]}
        cands = _candidates_from(data, scope, {"evt_1"})
        assert len(cands) == 1  # uncited one dropped
        c = cands[0]
        assert len(c.claim) == 500 and c.confidence == "inferred"
        assert c.fact_kind == "fact" and len(c.reason) == 500
        assert c.prompt_version  # C5 audit: prompt version travels with the record

    def test_a3_episode_close_and_idempotent(self, env, monkeypatch):
        cfg, st = env
        cfg.machine = MACH
        first = None
        for unit in ("web", "api"):
            e = st.append_event("shell", "command", machine=MACH, project="demo",
                                command=f"systemctl --user start {unit}")
            first = first or e
        st.append_event("agent", "tool_call", machine=MACH, project="demo",
                        command="pytest tests/",
                        payload={"output": "FAILED", "exit_code": 1})
        from memor.consolidate.apply import run_consolidation
        run_consolidation(st, cfg, extractor="rules")
        # a problem fact pinned inside the episode window (deterministic)
        st.insert_fact(scope_project="demo", scope_component=None, scope_machine=MACH,
                       subject="bug:web", claim="web crashes under load",
                       kind="problem", confidence="confirmed", source_type="human",
                       valid_from=first["ts"])
        from memor.consolidate.episodes import close_episode
        rep = close_episode(st, cfg)
        assert rep["episode"]
        ep = st.list_episodes(project="demo")[0]
        assert "3 events" in ep["summary"]
        assert "systemctl" in ep["summary"]
        assert any("crashes under load" in l for l in ep["lessons"])
        assert ep["project"] == "demo" and ep["machine"] == MACH
        # markdown projection
        from memor.consolidate.markdown import regenerate
        regenerate(st, cfg, commit=False)
        assert (cfg.reference_dir / "projects" / "demo" / "episodes.md").exists()
        # idempotent: no new events -> nothing to summarize, cursor stable
        rep2 = close_episode(st, cfg)
        assert rep2["report"] == "nothing to summarize"
        assert len(st.list_episodes(project="demo")) == 1
        # MCP surface
        from memor import mcp_server as m
        monkeypatch.setattr(m, "_cfg", cfg)
        eps = __import__("json").loads(m.tool_get_recent_episodes("demo", 168, 5))
        assert eps and eps[0]["id"] == ep["id"]

    def test_d2_global_context_lists_machines(self, env, monkeypatch):
        cfg, st = env
        cfg.machine = MACH
        st.insert_fact(scope_project="local-ai", scope_component=None,
                       scope_machine="boxA", subject="service:qwen",
                       claim="qwen is running", kind="state", confidence="confirmed")
        st.insert_fact(scope_project="auto-apply", scope_component=None,
                       scope_machine="boxB", subject="port:9000",
                       claim="port 9000 is bound by node", kind="state",
                       confidence="confirmed")
        st.upsert_entity("boxA", kind="machine")
        from memor import mcp_server as m
        monkeypatch.setattr(m, "_cfg", cfg)
        ctx = m.tool_get_global_context()
        assert "## boxA" in ctx and "## boxB" in ctx
        assert "local-ai" in ctx and "auto-apply" in ctx
        assert "qwen is running" in ctx and "port 9000" in ctx


# ------------------------------------------------- chunk 9: G1/G2/G3

class TestChunk9:

    def test_g1_timeline_merges_events_facts_decisions(self, env, monkeypatch):
        from datetime import datetime, timedelta, timezone
        cfg, st = env
        base = (datetime.now(timezone.utc) - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ")
        e = st.append_event("shell", "command", machine=MACH, project="demo",
                            command="systemctl --user start web", ts=base)
        from memor.consolidate.apply import run_consolidation
        run_consolidation(st, cfg, extractor="rules")
        st.insert_decision(scope_project="demo", scope_component="web",
                           scope_machine=None, subject="web-backend",
                           choice="use nginx", reason="speed",
                           confidence="confirmed", source_type="human")
        # a state change inside the window
        st.append_event("shell", "command", machine=MACH, project="demo",
                        command="systemctl --user stop web")
        run_consolidation(st, cfg, extractor="rules")
        from memor import mcp_server as m
        monkeypatch.setattr(m, "_cfg", cfg)
        tl = m.tool_get_timeline("project:demo", since_hours=48, limit=50)
        assert "event" in tl and "fact+" in tl and "decision" in tl
        assert "systemctl --user start web" in tl
        assert "use nginx" in tl
        # chronological order
        stamps = [l.split("  ")[0] for l in tl.splitlines() if l.startswith("20")]
        assert stamps == sorted(stamps)

    def test_g2_digest_writes_and_advances_window(self, env):
        import argparse
        cfg, st = env
        st.meta_set("digest.since", "2020-01-01T00:00:00Z")  # wide window
        e = st.append_event("shell", "command", machine=MACH, project="demo",
                            command="systemctl --user start web")
        from memor.consolidate.apply import run_consolidation
        run_consolidation(st, cfg, extractor="rules")
        cmd_digest(argparse.Namespace(show=False, data_dir=str(cfg.data_dir)))
        from pathlib import Path as P
        path = str(cfg.system_dir / "DIGEST.md")
        assert P(path).exists()
        text = P(path).read_text()
        assert "## Changes since last digest" in text
        assert "is running" in text
        assert "## Surprises & open items" in text
        assert "## Activity" in text
        assert st.meta_get("digest.since") > "2020-01-01T00:00:00Z"  # window advanced
        # second digest: nothing new in the (now tiny) window
        cmd_digest(argparse.Namespace(show=False, data_dir=str(cfg.data_dir)))
        text2 = P(path).read_text()
        assert "no state changes" in text2

    def test_g3_confirmed_overturned_by_verify_is_surprise(self, env, monkeypatch):
        cfg, st = env
        cfg.machine = MACH
        import memor.capture.netproc as netproc
        import memor.capture.verify as verify
        monkeypatch.setattr(netproc, "snapshot_listen", lambda text=None: [])
        monkeypatch.setattr(verify, "probe_systemd",
                            lambda units: {u: "inactive" for u in units})
        monkeypatch.setattr(verify, "running_user_units", lambda: [])
        monkeypatch.setattr(verify, "_docker_statuses", lambda: {})
        # a CONFIRMED fact that reality no longer supports
        st.insert_fact(scope_project=None, scope_component="websvc",
                       scope_machine=MACH, subject="service:websvc",
                       claim="systemd service websvc is running",
                       kind="state", confidence="confirmed", source_type="human")
        from memor.capture.verify import run_verification
        rep = run_verification(st, cfg)
        assert rep["changes"]
        surprises = [r for r in st.open_reviews() if r["category"] == "surprise"]
        assert surprises  # confirmed fact overturned by re-observation

    def test_g3_unexplained_change_flagged(self, env, monkeypatch):
        cfg, st = env
        cfg.machine = MACH
        import memor.capture.netproc as netproc
        import memor.capture.verify as verify
        monkeypatch.setattr(netproc, "snapshot_listen", lambda text=None: [])
        monkeypatch.setattr(verify, "probe_systemd",
                            lambda units: {u: "inactive" for u in units})
        monkeypatch.setattr(verify, "running_user_units", lambda: [])
        monkeypatch.setattr(verify, "_docker_statuses", lambda: {})
        # NO human/agent/shell events at all -> the change is unexplained
        st.insert_fact(scope_project=None, scope_component="websvc",
                       scope_machine=MACH, subject="service:websvc",
                       claim="systemd service websvc is running",
                       kind="state", confidence="confirmed", source_type="human")
        from memor.capture.verify import run_verification
        rep = run_verification(st, cfg)
        assert rep.get("unexplained"), "no nearby activity -> must be flagged"
        assert any("unexplained" in r["summary"]
                   for r in st.open_reviews() if r["category"] == "surprise")

    def test_g3_explained_change_not_flagged(self, env, monkeypatch):
        cfg, st = env
        cfg.machine = MACH
        import memor.capture.netproc as netproc
        import memor.capture.verify as verify
        monkeypatch.setattr(netproc, "snapshot_listen", lambda text=None: [])
        monkeypatch.setattr(verify, "probe_systemd",
                            lambda units: {u: "inactive" for u in units})
        monkeypatch.setattr(verify, "running_user_units", lambda: [])
        monkeypatch.setattr(verify, "_docker_statuses", lambda: {})
        st.insert_fact(scope_project=None, scope_component="websvc",
                       scope_machine=MACH, subject="service:websvc",
                       claim="systemd service websvc is running",
                       kind="state", confidence="confirmed", source_type="human")
        # a human command right before the verify pass explains the change
        st.append_event("human", "note", machine=MACH,
                        command="I stopped websvc manually", payload={})
        from memor.capture.verify import run_verification
        rep = run_verification(st, cfg)
        assert not rep.get("unexplained")


# ------------------------------------------------- chunk 10: E3 robustness

class TestCaptureModules:

    def test_docker_snapshot_diff(self, env, monkeypatch):
        cfg, st = env
        import memor.capture.docker_events as de
        snaps = [
            [{"name": "web", "image": "nginx:1", "status": "Up 1h",
              "ports": {"80/tcp": "0.0.0.0:8080"}}],
            [{"name": "web", "image": "nginx:2", "status": "Up 2h",
              "ports": {"80/tcp": "0.0.0.0:8081"}},
             {"name": "cache", "image": "redis:7", "status": "Up 5m",
              "ports": {}}],
        ]
        state = {"i": 0}

        def fake_snap():
            i = state["i"]
            state["i"] = min(i + 1, len(snaps) - 1)
            return snaps[i]

        monkeypatch.setattr(de, "container_snapshot", fake_snap)
        monkeypatch.setattr(de, "docker_available", lambda: True)
        stop = {"flag": False}
        import threading
        t = threading.Thread(
            target=lambda: de.poll_docker_snapshots(st, lambda: stop["flag"],
                                                    interval=0.05),
            daemon=True)
        t.start()
        time.sleep(0.6)  # several 0.05s iterations: baseline, drift, no-ops
        stop["flag"] = True
        t.join(timeout=5)
        evs = [e for e in st.events_since("1970-01-01T00:00:00Z", source="docker")]
        assert evs
        kinds = " | ".join(e["kind"] for e in evs)
        assert "docker_drift" in kinds  # image + port change detected
        assert any("cache" in (e.get("command") or "") for e in evs)

    def test_docker_events_tolerates_garbage(self, env, monkeypatch):
        cfg, st = env
        import memor.capture.docker_events as de

        class FakeProc:
            def __init__(self, lines):
                self.stdout = iter(lines)
            def terminate(self):
                pass

        lines = ['{"Type":"container","Action":"start","Actor":{"Attributes":{"name":"web"}},"time":1}',
                 "not json at all",
                 '{"Type":"container","Action":"stop","Actor":{"Attributes":{"name":"web"}},"time":2}']
        monkeypatch.setattr(de.subprocess, "Popen",
                            lambda *a, **k: FakeProc(lines))
        calls = {"n": 0}

        def stop():
            calls["n"] += 1
            return calls["n"] > len(lines) + 1

        de.tail_docker_events(st, stop)
        evs = [e for e in st.recent_events(limit=5, source="docker")]
        assert len(evs) == 2  # garbage line skipped, both events recorded


class TestContextSeparation:

    def test_project_context_keeps_machine_state_apart(self, env, monkeypatch):
        cfg, st = env
        st.insert_fact(scope_project="demo", scope_component="web", scope_machine=None,
                       subject="service:web", claim="web is running",
                       kind="state", confidence="confirmed")
        for i in range(15):
            st.insert_fact(scope_project=None, scope_component=f"svc{i}",
                           scope_machine="box", subject=f"service:svc{i}",
                           claim=f"svc{i} is running", kind="state",
                           confidence="confirmed")
        from memor import mcp_server as m
        monkeypatch.setattr(m, "_cfg", cfg)
        ctx = m.tool_get_project_context("demo")
        assert "web is running" in ctx
        assert "Machine state (shared" in ctx
        assert "svc0 is running" in ctx
        # the 15 machine facts are collapsed to 10 + a pointer
        assert "and 5 more machine facts" in ctx
        # project section comes before the machine section
        assert ctx.index("web is running") < ctx.index("Machine state (shared")


# ------------------------------------------- projection git commit policy

class TestProjectionGit:
    @pytest.fixture
    def repo(self, env):
        cfg, st = env
        subprocess.run(["git", "init", "-q", "-b", "main", str(cfg.data_dir)],
                       check=True, capture_output=True, text=True)
        return cfg, st

    @staticmethod
    def _commits(cfg):
        r = subprocess.run(["git", "-C", str(cfg.data_dir), "log", "--format=%s"],
                           capture_output=True, text=True)
        return r.stdout.strip().splitlines()

    def test_noop_regeneration_rewrites_nothing(self, repo):
        """The `updated:` frontmatter stamp must not count as a change: a
        pass with no new knowledge rewrites no files and commits nothing
        (previously every hourly pass committed the whole tree)."""
        cfg, st = repo
        from memor.consolidate.apply import run_consolidation
        from memor.consolidate.markdown import regenerate
        st.append_event("shell", "command", machine=MACH, project="demo",
                        command="systemctl --user start web")
        run_consolidation(st, cfg, extractor="rules")
        first = regenerate(st, cfg, immediate=True)
        assert first and len(self._commits(cfg)) == 1
        second = regenerate(st, cfg, immediate=False)
        assert second == []                        # nothing rewritten
        assert len(self._commits(cfg)) == 1        # nothing committed

    def test_daemon_commit_throttled_and_manual_immediate(self, repo):
        cfg, st = repo
        from memor.consolidate.apply import run_consolidation
        from memor.consolidate.markdown import regenerate
        cfg.projection_commit_interval = 10_000_000  # "never" for the test
        st.append_event("shell", "command", machine=MACH, project="demo",
                        command="systemctl --user start web")
        run_consolidation(st, cfg, extractor="rules")
        regenerate(st, cfg, immediate=True)        # manual-style: commits now
        assert len(self._commits(cfg)) == 1

        # next "day": new work, daemon pass -> files updated on disk only
        st.append_event("shell", "command", machine=MACH, project="demo",
                        command="systemctl --user stop web")
        run_consolidation(st, cfg, extractor="rules")
        changed = regenerate(st, cfg)              # throttled default path
        assert changed                              # rewritten on disk…
        assert len(self._commits(cfg)) == 1        # …but not yet committed

    def test_daily_commit_message_summarizes_window(self, repo):
        cfg, st = repo
        from memor.consolidate.apply import run_consolidation
        from memor.consolidate.markdown import regenerate
        st.append_event("shell", "command", machine=MACH, project="demo",
                        command="systemctl --user start web")
        run_consolidation(st, cfg, extractor="rules")
        regenerate(st, cfg, immediate=True)
        st.append_event("shell", "command", machine=MACH, project="demo",
                        command="systemctl --user stop web")
        run_consolidation(st, cfg, extractor="rules")
        # second-granularity clocks can't express "0.01s later" inside one
        # test run, so force the due-branch via interval=0 (commit every
        # pass); the not-due branch is covered by the throttle test above
        cfg.projection_commit_interval = 0
        regenerate(st, cfg)
        msgs = self._commits(cfg)
        assert len(msgs) == 2
        assert "facts" in msgs[0] and "superseded" in msgs[0]
        assert "since" in msgs[0]

    def test_projection_interval_config_roundtrip(self, repo):
        cfg, st = repo
        assert cfg.projection_commit_interval == 86400.0  # default: daily
        cfg.projection_commit_interval = 3600.0
        cfg.save()
        assert Config.load().projection_commit_interval == 3600.0


# ------------------------------------- housekeeping exclusion (verify etc.)

class TestHousekeepingExclusion:
    def test_verify_events_excluded_from_llm_queue_and_window(self, env, monkeypatch):
        """A1 verification passes are housekeeping: they must not enter the
        LLM window or count as LLM-pending (they'd dilute the model's
        context and burn queue slots forever)."""
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        srv = _FakeLLMServer()
        try:
            cfg, st = env
            cfg.llm_base_url = srv.base
            cfg.llm_model = "test-model"
            st.append_event("verify", "verify_run", machine=MACH,
                            command="verification pass: 3 units, 2 listeners")
            st.append_event("shell", "command", machine=MACH, project="demo",
                            command="systemctl --user start web")
            from memor.consolidate.apply import run_llm_sync, llm_pending_count
            assert llm_pending_count(st) == 1        # verify event not queued
            rep = run_llm_sync(st, cfg)
            assert rep["events"] == 1                # only the shell event
            assert len(srv.requests) == 1
            offered = [x["command"] for x in srv.requests[0]]
            assert offered == ["systemctl --user start web"]
        finally:
            srv.stop()

    def test_verify_events_excluded_from_consolidation_window(self, env):
        cfg, st = env
        st.append_event("verify", "verify_run", machine=MACH,
                        command="verification pass: 3 units")
        st.append_event("shell", "command", machine=MACH, project="demo",
                        command="systemctl --user start web")
        from memor.consolidate.apply import run_consolidation
        rep = run_consolidation(st, cfg, extractor="rules")
        assert rep["events"] == 1                    # verify event skipped
        cur = st.current_fact("demo", "web", "service:web")
        assert cur and "running" in cur["claim"]     # real event still processed


# ------------------------------------------------------- store size alerts

class TestStoreSizeAlert:
    def test_data_dir_size_helper(self, env):
        cfg, st = env
        from memor.store import data_dir_size
        base = data_dir_size(cfg.data_dir)
        (cfg.data_dir / "probe.bin").write_bytes(b"x" * 4096)
        assert data_dir_size(cfg.data_dir) >= base + 4096

    def test_doctor_warns_above_threshold(self, env, capsys):
        import argparse
        from memor.cli import cmd_doctor
        cfg, st = env
        cfg.max_store_size_bytes = 100               # store already exceeds
        cfg.save()
        cmd_doctor(argparse.Namespace(data_dir=None))
        out = capsys.readouterr().out
        assert "WARNING" in out and "exceeds" in out

    def test_doctor_quiet_below_threshold(self, env, capsys):
        import argparse
        from memor.cli import cmd_doctor
        cfg, st = env
        cfg.max_store_size_bytes = 5 * 1024 ** 3
        cfg.save()
        cmd_doctor(argparse.Namespace(data_dir=None))
        out = capsys.readouterr().out
        assert "exceeds" not in out

    def test_digest_store_size_section(self, env):
        cfg, st = env
        cfg.max_store_size_bytes = 100
        cfg.save()
        from memor.consolidate.digest import build_digest
        text = Path(build_digest(st, cfg)).read_text()
        assert "Store size" in text and "WARNING" in text

    def test_project_context_size_alert(self, env, monkeypatch):
        """Agents see the alert in every catch-me-up package."""
        from memor import mcp_server as m
        cfg, st = env
        cfg.max_store_size_bytes = 100
        cfg.save()
        monkeypatch.setattr(m, "_cfg", cfg)
        ctx = m.tool_get_project_context("demo")
        assert "Store size alert" in ctx

    def test_project_context_no_alert_below_threshold(self, env, monkeypatch):
        from memor import mcp_server as m
        cfg, st = env
        cfg.max_store_size_bytes = 5 * 1024 ** 3
        cfg.save()
        monkeypatch.setattr(m, "_cfg", cfg)
        assert "Store size alert" not in m.tool_get_project_context("demo")


# ------------------------------------------- systemd unit regression

class TestSystemdUnit:
    def test_execstart_uses_only_flags_the_cli_accepts(self):
        """The generated unit's ExecStart must only use flags `capture run`
        actually accepts: a made-up `--foreground` used to make the service
        crash-loop with exit 2 (found 2026-08-25 when the user installed
        the unit and the daemon never came up)."""
        from memor.capture.daemon import SYSTEMD_UNIT
        unit = SYSTEMD_UNIT.format(memor_bin="/bin/memor", sources="net,fs",
                                   consolidate_arg="")
        line = next(l for l in unit.splitlines() if l.startswith("ExecStart="))
        cmd = line[len("ExecStart="):].split()
        assert cmd[1:3] == ["capture", "run"]
        flags = [c for c in cmd[3:] if c.startswith("--")]
        assert "--foreground" not in flags  # run is foreground by default
        assert all(f in ("--sources", "--consolidate-interval", "--daemonize")
                   for f in flags)


# ----------------------------------- entity resolution / timeline scoping

class TestTimelineProjectScoping:
    def test_plain_project_name_scopes_timeline_not_store_wide(self, env, monkeypatch):
        """A plain project name must resolve to THAT project: project-kind
        entities used to resolve to project=None (no filter), so
        `timeline memory` returned the whole store's history (other
        projects' facts and events) — reported by the user 2026-08-25."""
        from memor import mcp_server as m
        cfg, st = env
        st.upsert_entity("memory", kind="project", project="memory")
        st.upsert_entity("opencode", kind="project", project="opencode")
        st.insert_fact(scope_project="memory", scope_component=None,
                       scope_machine=None, subject="work",
                       claim="memory project work A", kind="fact",
                       confidence="confirmed")
        st.insert_fact(scope_project="opencode", scope_component=None,
                       scope_machine=None, subject="work",
                       claim="opencode project work B", kind="fact",
                       confidence="confirmed")
        st.insert_fact(scope_project=None, scope_component=None,
                       scope_machine=None, subject="box",
                       claim="box-wide note", kind="fact",
                       confidence="confirmed")
        st.append_event("shell", "command", machine=MACH, project="memory",
                        command="work on memory")
        st.append_event("shell", "command", machine=MACH, project="opencode",
                        command="work on opencode")
        monkeypatch.setattr(m, "_cfg", cfg)
        out = m.tool_get_timeline("memory", since_hours=72)
        assert "memory project work A" in out
        assert "work on memory" in out
        assert "box-wide note" in out and "(box-wide)" in out
        # other projects' history must NOT leak in
        assert "opencode project work B" not in out
        assert "work on opencode" not in out
        # explicit specifier agrees with the (now fixed) plain name
        out2 = m.tool_get_timeline("project:memory", since_hours=72)
        assert "opencode project work B" not in out2
        assert "memory project work A" in out2


# ------------------------------------------------- box-wide scope override

class TestNoProjectOverride:
    """General (machine-wide) work done from inside a project directory can
    be marked box-wide instead of being attributed to the project."""

    @pytest.fixture
    def proj(self, env, tmp_path, monkeypatch):
        cfg, st = env
        proj = tmp_path / "proj"
        (proj / "sub").mkdir(parents=True)
        from memor.config import ProjectRef
        cfg.projects.append(ProjectRef(name="proj", paths=[str(proj)]))
        for k in ("MEMOR_CMD", "MEMOR_EXIT", "MEMOR_T0", "MEMOR_T1",
                  "MEMOR_SESSION", "MEMOR_CWD", "MEMOR_SHELL",
                  "MEMOR_NO_PROJECT"):
            monkeypatch.delenv(k, raising=False)
        monkeypatch.setenv("MEMOR_EXIT", "0")
        monkeypatch.setenv("MEMOR_T0", "1000000000000")
        monkeypatch.setenv("MEMOR_T1", "1003500000000")
        monkeypatch.setenv("MEMOR_SESSION", "s1")
        monkeypatch.setenv("MEMOR_SHELL", "bash")
        monkeypatch.setenv("MEMOR_CWD", str(proj / "sub"))
        return cfg, st, proj

    @staticmethod
    def _event(st):
        return st.recent_events(limit=1, source="shell")[0]

    def test_default_resolves_to_project(self, proj, monkeypatch):
        cfg, st, _ = proj
        monkeypatch.setenv("MEMOR_CMD", "make build")
        from memor.capture.terminal import shell_event
        shell_event(cfg, st)
        assert self._event(st)["project"] == "proj"

    def test_per_command_prefix_records_box_wide(self, proj, monkeypatch):
        """`MEMOR_NO_PROJECT=1 <cmd>` marks one command box-wide even from
        inside a registered project dir; the raw command (prefix included)
        stays in the evidence."""
        cfg, st, _ = proj
        monkeypatch.setenv("MEMOR_CMD",
                           "MEMOR_NO_PROJECT=1 systemctl daemon-reload")
        from memor.capture.terminal import shell_event
        shell_event(cfg, st)
        e = self._event(st)
        assert e["project"] is None
        assert e["command"].startswith("MEMOR_NO_PROJECT=1")

    def test_session_export_marks_box_wide(self, proj, monkeypatch):
        """export MEMOR_NO_PROJECT=1 → the whole session is machine-scoped
        regardless of cwd."""
        cfg, st, _ = proj
        monkeypatch.setenv("MEMOR_NO_PROJECT", "1")
        monkeypatch.setenv("MEMOR_CMD", "systemctl daemon-reload")
        from memor.capture.terminal import shell_event
        shell_event(cfg, st)
        assert self._event(st)["project"] is None

    def test_run_wrapped_no_project_flag(self, proj):
        import os as _os
        cfg, st, p = proj
        from memor.capture.terminal import run_wrapped
        old = _os.getcwd()
        try:
            _os.chdir(p / "sub")
            run_wrapped(cfg, st, "true", no_project=True)
            run_wrapped(cfg, st, "true")
        finally:
            _os.chdir(old)
        assert self._event(st)["project"] == "proj"      # control, last run
        earlier = st.recent_events(limit=2, source="shell")
        assert [e["project"] for e in earlier] == ["proj", None]  # newest first


# ------------------------------------------- shell hook job-notification

class TestShellHookSnippet:
    """The capture process must not surface as a shell job-completion
    notification ('[1]+ Done MEMOR_CMD=... nohup memor shell-event'):
    bash disowns the background job, zsh uses '&!' (bg + disown)."""

    def test_bash_snippet_disowns(self, capsys, monkeypatch):
        monkeypatch.delenv("MEMOR_BIN", raising=False)
        monkeypatch.delenv("MEMOR_PROJECT_VENV", raising=False)
        import argparse
        from memor.cli import cmd_shell_hook
        cmd_shell_hook(argparse.Namespace(shell="bash"))
        out = capsys.readouterr().out
        assert "nohup" in out and "shell-event" in out
        assert "disown" in out

    def test_zsh_snippet_disowns(self, capsys, monkeypatch):
        monkeypatch.delenv("MEMOR_BIN", raising=False)
        monkeypatch.delenv("MEMOR_PROJECT_VENV", raising=False)
        import argparse
        from memor.cli import cmd_shell_hook
        cmd_shell_hook(argparse.Namespace(shell="zsh"))
        out = capsys.readouterr().out
        assert "nohup" in out and "&!" in out


# ------------------------------------------------------------- state as-of

class TestStateAsOf:
    """Point-in-time reads: valid_from <= ts < valid_until (open when
    valid_until is NULL). A→B supersession flips exactly at the
    boundary; retracted rows never appear in any era; contradictory
    rows do (the conflict may have been real at that moment)."""

    def test_supersession_window(self, env):
        cfg, st = env
        a = st.insert_fact(scope_project="p", scope_component=None,
                           scope_machine=None, subject="port:srv",
                           claim="uses 8000", kind="state",
                           valid_from="2026-09-01T00:00:00Z")
        st.close_fact(a, "2026-09-10T00:00:00Z")
        b = st.insert_fact(scope_project="p", scope_component=None,
                           scope_machine=None, subject="port:srv",
                           claim="uses 8080", kind="state",
                           valid_from="2026-09-10T00:00:00Z",
                           supersedes_id=a)
        assert [f["id"] for f in
                st.facts_valid_at("2026-09-05T12:00:00Z", "port:srv")] == [a]
        assert [f["id"] for f in
                st.facts_valid_at("2026-09-15T00:00:00Z", "port:srv")] == [b]
        # the boundary belongs to the new record (valid_until is exclusive)
        assert [f["id"] for f in
                st.facts_valid_at("2026-09-10T00:00:00Z", "port:srv")] == [b]
        # before anything was recorded: nothing
        assert st.facts_valid_at("2026-08-15T00:00:00Z", "port:srv") == []
        # history still carries the full chain — as-of is a cut, not a
        # rewrite
        assert len(st.fact_history("port:srv")) == 2

    def test_retracted_never_appears(self, env):
        cfg, st = env
        a = st.insert_fact(scope_project="p", scope_component=None,
                           scope_machine=None, subject="port:old",
                           claim="was 9000", kind="state",
                           valid_from="2026-09-01T00:00:00Z")
        st.set_fact_status(a, "retracted")
        assert st.facts_valid_at("2026-09-05T00:00:00Z", "port:old") == []

    def test_contradictory_included(self, env):
        cfg, st = env
        for n in ("x", "y"):
            st.insert_fact(scope_project="p", scope_component=None,
                           scope_machine=None, subject="port:dup",
                           claim=f"uses 8{n}80", kind="state",
                           status="contradictory",
                           valid_from="2026-09-01T00:00:00Z")
        got = st.facts_valid_at("2026-09-05T00:00:00Z", "port:dup")
        assert len(got) == 2  # the conflict was real at that moment

    def test_decisions_valid_at(self, env):
        cfg, st = env
        d1 = st.insert_decision(scope_project="p", scope_component=None,
                                scope_machine=None, subject="backend",
                                choice="vllm",
                                decided_at="2026-09-01T00:00:00Z",
                                valid_until="2026-09-12T00:00:00Z")
        d2 = st.insert_decision(scope_project="p", scope_component=None,
                                scope_machine=None, subject="backend",
                                choice="sglang",
                                decided_at="2026-09-12T00:00:00Z")
        assert [d["id"] for d in
                st.decisions_valid_at("2026-09-05T00:00:00Z",
                                      project="p")] == [d1]
        assert [d["id"] for d in
                st.decisions_valid_at("2026-09-15T00:00:00Z",
                                      project="p")] == [d2]

    def test_tool_and_cli_state_as_of(self, env, capsys):
        import argparse
        from memor import mcp_server as m
        from memor.cli import cmd_current
        cfg, st = env
        st.insert_fact(scope_project="demo", scope_component=None,
                       scope_machine=None, subject="port:srv",
                       claim="uses 8000", kind="state",
                       valid_from="2026-09-01T00:00:00Z")
        st.close_fact(st.list_facts(subject="port:srv")[0]["id"],
                      "2026-09-10T00:00:00Z")
        st.insert_fact(scope_project="demo", scope_component=None,
                       scope_machine=None, subject="port:srv",
                       claim="uses 8080", kind="state",
                       valid_from="2026-09-10T00:00:00Z")
        st.insert_decision(scope_project="demo", scope_component=None,
                           scope_machine=None, subject="backend",
                           choice="vllm",
                           decided_at="2026-09-01T00:00:00Z")
        # MCP tool: project-scoped entity sweeps its facts' subjects
        m._cfg = cfg
        out = json.loads(m.tool_get_state_as_of(
            "project:demo", "2026-09-05T12:00:00Z"))
        assert [f["claim"] for f in out["subjects"]["port:srv"]] == ["uses 8000"]
        assert [d["choice"] for d in out["decisions"]] == ["vllm"]
        out = json.loads(m.tool_get_state_as_of(
            "project:demo", "2026-09-15T00:00:00Z"))
        assert [f["claim"] for f in out["subjects"]["port:srv"]] == ["uses 8080"]
        # empty moment: no subjects + explanatory note
        out = json.loads(m.tool_get_state_as_of(
            "project:demo", "2026-08-01T00:00:00Z"))
        assert out["subjects"] == {} and "note" in out
        # CLI: `memor current <entity> --at` is the same read
        args = argparse.Namespace(data_dir=str(cfg.data_dir),
                                  entity="port:srv",
                                  at="2026-09-05T12:00:00Z")
        assert cmd_current(args) == 0
        out = json.loads(capsys.readouterr().out)
        assert [f["claim"] for f in out["subjects"]["port:srv"]] == ["uses 8000"]
        # without --at the plain current read is untouched
        args = argparse.Namespace(data_dir=str(cfg.data_dir),
                                  entity="port:srv", at=None)
        assert cmd_current(args) == 0
        out = json.loads(capsys.readouterr().out)
        assert "current" in out  # the ordinary current-state shape
