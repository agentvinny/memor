"""R3: scoped hybrid search — scope axes, semantic index + cache, RRF
fusion, FTS fallback, CLI and MCP surfaces. The embedder is a
deterministic fake (bag-of-words, stable per-process) — no GPU in tests."""
import json

import pytest

from memor.config import Config
from memor.store import Store
from memor import search as srch
from memor.search import Scope, run_search


class FakeEmbedder:
    """Bag-of-words vectors in 16 dims (stable char-sum hash)."""

    dim = 16

    def __init__(self):
        self.n_calls = 0
        self.corpus_calls = 0

    def _vec(self, t: str) -> list[float]:
        v = [0.0] * self.dim
        for w in t.lower().split():
            v[sum(ord(c) for c in w) % self.dim] += 1.0
        n = sum(x * x for x in v) ** 0.5 or 1.0
        return [x / n for x in v]

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.n_calls += 1
        return [self._vec(t) for t in texts]


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMOR_DATA_DIR", str(tmp_path))
    cfg = Config.load()
    cfg.machine = "testbox"
    cfg.embed_provider = "off"  # tests inject the fake explicitly
    cfg.save()
    store = Store(cfg.data_dir)
    return cfg, store


@pytest.fixture
def fake(monkeypatch):
    emb = FakeEmbedder()
    monkeypatch.setattr(srch, "get_embedder", lambda cfg: emb)
    return emb


def populate(store, cfg):
    """Two workspaces with summaries, state, and prompts."""
    store.insert_summary(session="s1", workspace="/proj", project="p",
                         level=1, span="dsh:s1:seq[0..5]",
                         text="added a monotonic rev counter to order closes")
    store.insert_summary(session="s1", workspace="/proj", project="p",
                         level=2, span="dsh:s1:seq[6..9]",
                         text="sqlite storage for the embeddings table")
    store.insert_summary(session="s2", workspace="/other", project="p",
                         level=1, span="dsh:s2:seq[0..5]",
                         text="unrelated networking work on sockets")
    store.workspace_state_upsert("/proj", "decision", "storage", "sqlite")
    store.workspace_state_upsert("/proj", "open_item", "ship it", "soon")
    store.workspace_state_upsert("/other", "decision", "dns", "do53")
    store.append_event("session", "prompt", machine="testbox", project="p",
                       cwd="/proj", session="s1",
                       command="session prompt: tests",
                       payload={"prompt": "how do I run the test suite"})
    store.append_event("session", "prompt", machine="testbox", project="p",
                       cwd="/other", session="s2",
                       command="session prompt: net",
                       payload={"prompt": "bind the socket"})
    store.append_event("session", "tool", machine="testbox", project="p",
                       cwd="/proj", session="s1",
                       command="session tool: bash",
                       payload={"tool": "bash"})
    store.append_event("memor", "summary_written", machine="testbox",
                       project="p", session="s1",
                       command="session summary", payload={"x": 1})


# -------------------------------------------------------------- scoping

def test_scope_workspace(env):
    cfg, store = env
    populate(store, cfg)
    out = run_search(store, cfg, "storage sqlite",
                      Scope(workspace="/proj"), mode="fts", limit=10)
    kinds = {(h["kind"], h.get("workspace")) for h in out["hits"]}
    assert ("summary", "/proj") in kinds
    # /other summaries are out of scope
    assert not any(h.get("workspace") == "/other" for h in out["hits"])
    out = run_search(store, cfg, "sockets networking",
                     Scope(workspace="/other"), mode="fts", limit=10)
    assert any(h.get("workspace") == "/other" for h in out["hits"])


def test_scope_session_machine_time_level(env):
    cfg, store = env
    populate(store, cfg)
    # session
    out = run_search(store, cfg, "monotonic",
                     Scope(session="s2"), mode="fts", limit=10)
    assert not any(h.get("session") == "s1" for h in out["hits"])
    # machine
    out = run_search(store, cfg, "monotonic",
                     Scope(machine="otherbox"), mode="fts", limit=10)
    assert out["hits"] == []
    # time window: everything is now, so a past window is empty
    out = run_search(store, cfg, "monotonic",
                     Scope(since="2000-01-01T00:00:00Z",
                           until="2001-01-01T00:00:00Z"),
                     mode="fts", limit=10)
    assert out["hits"] == []
    # level: summaries only, and only the given levels
    out = run_search(store, cfg, "sqlite",
                     Scope(levels=[2]), mode="fts", limit=10)
    sums = [h for h in out["hits"] if h["kind"] == "summary"]
    assert sums and all(h["level"] == 2 for h in sums)


def test_scope_kinds(env):
    cfg, store = env
    populate(store, cfg)
    out = run_search(store, cfg, "sqlite storage",
                     Scope(kinds={"summary"}), mode="fts", limit=10)
    assert out["hits"] and all(h["kind"] == "summary" for h in out["hits"])
    out = run_search(store, cfg, "sqlite",
                     Scope(kinds={"state"}), mode="fts", limit=10)
    assert out["hits"] == []  # state is semantic-only


# --------------------------------------------------------------- semantic

def test_semantic_search_and_cache(env, fake):
    cfg, store = env
    populate(store, cfg)
    emb = fake
    out = run_search(store, cfg, "monotonic rev counter closes order",
                     Scope(workspace="/proj"), mode="semantic", limit=5)
    assert out["hits"]
    # the paraphrased query finds the rev-counter summary (its exact words
    # are not all in the query) and ranks the storage one lower
    top = out["hits"][0]
    assert top["kind"] == "summary" and "rev counter" in top["text"]
    assert all(h["via"] == "semantic" for h in out["hits"])
    # cached: second run embeds only the query (one call)
    n1 = emb.n_calls
    run_search(store, cfg, "monotonic rev counter closes order",
               Scope(workspace="/proj"), mode="semantic", limit=5)
    assert emb.n_calls == n1 + 1
    # cache rows exist
    for k in ("summary", "state", "prompt"):
        assert store._conn().execute(
            "SELECT COUNT(*) FROM embeddings WHERE kind=?", (k,)).fetchone()[0] > 0


def test_semantic_kinds_scope(env, fake):
    cfg, store = env
    populate(store, cfg)
    out = run_search(store, cfg, "sqlite storage",
                     Scope(workspace="/proj", kinds={"state"}),
                     mode="semantic", limit=5)
    assert out["hits"] and all(h["kind"] == "state" for h in out["hits"])
    assert out["hits"][0]["subject"] == "storage"
    # prompt target: the question text is a paraphrase away from "socket"
    out = run_search(store, cfg, "run the test suite",
                     Scope(kinds={"prompt"}), mode="semantic", limit=5)
    assert any(h["kind"] == "prompt" for h in out["hits"])


# ----------------------------------------------------------------- rrf

def test_hybrid_rrf_fusion(env, fake):
    cfg, store = env
    populate(store, cfg)
    out = run_search(store, cfg, "sqlite storage",
                     Scope(workspace="/proj"), mode="hybrid", limit=10)
    via = {h["id"]: h["via"] for h in out["hits"]}
    # the storage summary is in BOTH the FTS list and the semantic list
    assert any(v == "fts+semantic" for v in via.values())
    # both-ranked hits outrank single-list hits
    both = [h for h in out["hits"] if h["via"] == "fts+semantic"]
    single = [h for h in out["hits"] if h["via"] != "fts+semantic"]
    assert both and (both[0]["score"] >= single[0]["score"])


def test_no_embedder_fallback(env):
    cfg, store = env
    populate(store, cfg)
    # provider off: hybrid degrades to FTS with a note
    out = run_search(store, cfg, "monotonic rev",
                     Scope(workspace="/proj"), mode="hybrid", limit=10)
    assert out["hits"] and "degraded" in out.get("note", "")
    assert all(h["via"] == "fts" for h in out["hits"])
    # semantic mode alone: empty + explanatory note
    out = run_search(store, cfg, "x", Scope(), mode="semantic")
    assert out["hits"] == [] and "embedder" in out["note"]


# ----------------------------------------------------------------- cli

def test_cli_search(env, fake, monkeypatch, capsys):
    from memor import cli
    monkeypatch.setenv("MEMOR_DATA_DIR", str(env[0].data_dir))
    cfg, store = env
    populate(store, cfg)
    import os
    monkeypatch.chdir("/tmp")
    # fts mode, explicit workspace
    rc = cli.main(["search", "sqlite storage", "--mode", "fts",
                   "--workspace", "/proj", "--limit", "5"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["hits"] and all(h.get("workspace") in ("/proj", None)
                                for h in out["hits"])
    # kinds + fts: state is semantic-only, so an empty-but-valid answer
    capsys.readouterr()
    rc = cli.main(["search", "sqlite", "--mode", "fts",
                   "--workspace", "/proj", "--kinds", "state"])
    out = json.loads(capsys.readouterr().out)
    assert out["hits"] == []
    # hybrid with the fake embedder injected (default mode)
    capsys.readouterr()
    rc = cli.main(["search", "sqlite storage", "--workspace", "/proj",
                   "--limit", "5"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["hits"]


# ----------------------------------------------------------------- mcp

def test_mcp_tool_search(env, fake, monkeypatch):
    import memor.mcp_server as m
    cfg, store = env
    monkeypatch.setattr(m, "_cfg", cfg)
    populate(store, cfg)
    out = json.loads(m.tool_search(
        "sqlite storage", scope_project="p", workspace="/proj",
        mode="fts", limit=5))
    assert out["hits"]
    out = json.loads(m.tool_search(
        "monotonic rev counter", workspace="/proj", mode="hybrid",
        limit=5))
    assert out["hits"]
    assert all("via" in h and "kind" in h and "id" in h for h in out["hits"])
    # no matches
    out = json.loads(m.tool_search("zzzqqqxx", mode="fts"))
    assert out["hits"] == [] and "no matches" in out["note"]
