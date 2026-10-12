"""Workspace state: store semantics, document rendering, file projection,
worker promotion, CLI and MCP surfaces."""
import json
from pathlib import Path

import pytest

from memor import wstate
from memor.config import Config
from memor.store import Store


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMOR_DATA_DIR", str(tmp_path))
    cfg = Config.load()
    cfg.machine = "testbox"
    store = Store(cfg.data_dir)
    return cfg, store


# ------------------------------------------------------------- store api

def test_upsert_create_update_noop(env):
    cfg, store = env
    r1 = store.workspace_state_upsert(
        "/proj", "convention", "venv", "use /data venv",
        source_span="dsh:s1:seq[0..5]")
    assert r1["status"] == "active"
    # identical re-promotion: same row, not even touched
    r2 = store.workspace_state_upsert(
        "/proj", "convention", "venv", "use /data venv",
        source_span="dsh:s1:seq[0..5]")
    assert r2["id"] == r1["id"] and r2["created_at"] == r2["updated_at"]
    # new content: in-place update, same id
    r3 = store.workspace_state_upsert(
        "/proj", "convention", "venv", "use /data venv (x)",
        source_span="dsh:s1:seq[6..10]")
    assert r3["id"] == r1["id"] and r3["body"] == "use /data venv (x)"
    # one active row per (workspace, category, subject) key
    assert store.workspace_state_active(
        "/proj", "convention", "venv")["id"] == r1["id"]
    assert store.workspace_state_active("/proj", "convention", "other") is None
    # the key is per-workspace
    store.workspace_state_upsert("/other", "convention", "venv", "elsewhere")
    assert store.workspace_state_active(
        "/proj", "convention", "venv")["id"] == r1["id"]


def test_set_resolved_retired_active(env):
    cfg, store = env
    r = store.workspace_state_upsert("/proj", "open_item", "ship it", "soon")
    store.workspace_state_set(r["id"], "resolved")
    assert store.workspace_state_get(r["id"])["status"] == "resolved"
    # resolved rows leave the active set but appear in recently-handled
    cur = store.workspace_state_current("/proj")
    assert [x["id"] for x in cur if x["status"] == "active"] == []
    assert [x["id"] for x in cur if x["status"] == "resolved"] == [r["id"]]
    # re-opening: the key must be free
    store.workspace_state_set(r["id"], "active")
    assert store.workspace_state_get(r["id"])["status"] == "active"
    # retired is a second closed state
    store.workspace_state_set(r["id"], "retired")
    assert store.workspace_state_get(r["id"])["status"] == "retired"


def test_set_superseded_keeps_history(env):
    cfg, store = env
    r = store.workspace_state_upsert(
        "/proj", "decision", "storage", "sqlite (v1)")
    new = store.workspace_state_set(r["id"], "superseded", body="pglite (v2)")
    assert new["status"] == "superseded" and new["superseded_by"]
    repl = store.workspace_state_get(new["superseded_by"])
    assert repl["status"] == "active" and repl["body"] == "pglite (v2)"
    # the key now points at the replacement; the old row is history
    assert store.workspace_state_active(
        "/proj", "decision", "storage")["id"] == repl["id"]
    # superseding a closed row is refused
    with pytest.raises(ValueError):
        store.workspace_state_set(r["id"], "superseded", body="again")
    # re-opening a closed row while the key is occupied is refused
    with pytest.raises(ValueError):
        store.workspace_state_set(r["id"], "active")


# ------------------------------------------------------------- rendering

def test_state_document_order_ids_and_overflow(env):
    cfg, store = env
    store.workspace_state_upsert("/proj", "open_item", "ship it", "soon")
    store.workspace_state_upsert("/proj", "problem", "flaky hook", "details")
    store.workspace_state_upsert("/proj", "decision", "storage", "sqlite")
    store.workspace_state_upsert("/proj", "convention", "venv", "use /data")
    store.workspace_state_upsert("/proj", "weird", "custom cat", "body")
    doc = wstate.state_document(store, "/proj")
    assert doc.startswith("## Workspace state — /proj")
    # fixed category order, unknown categories after, alphabetical
    for a, b in [("### Open items", "### Problems (active)"),
                 ("### Problems (active)", "### Decisions"),
                 ("### Decisions", "### Conventions"),
                 ("### Conventions", "### Weird")]:
        assert doc.index(a) < doc.index(b)
    # every active row's id is present (the curation handle)
    for r in store.workspace_state_current("/proj", 0):
        assert r["id"] in doc


def test_state_document_empty(env):
    cfg, store = env
    doc = wstate.state_document(store, "/nowhere")
    assert "nothing recorded yet" in doc


# --------------------------------------------------------- file projection

def test_project_state_file_lifecycle(env, tmp_path):
    ws = str(tmp_path / "ws")
    store = env[1]
    p = wstate.project_state_file(ws, store)  # creates (empty state)
    assert p == Path(ws) / "AGENTS.local.md" and p.exists()
    text = p.read_text()
    assert wstate.BEGIN_MARKER in text and "memor:workspace-state:end" in text
    # no change -> no write (None)
    assert wstate.project_state_file(ws, store) is None
    # an item appears inside the markers, hand-written content outside
    # survives untouched
    p.write_text("my own notes\n" + text)
    store.workspace_state_upsert(ws, "command", "pytest", "the venv one")
    p2 = wstate.project_state_file(ws, store)
    assert p2 == p
    text2 = p.read_text()
    assert text2.startswith("my own notes\n")
    assert text2.count(wstate.BEGIN_MARKER) == 1
    assert "**pytest**" in text2
    # idempotent again
    assert wstate.project_state_file(ws, store) is None


def test_project_state_file_appends_to_markerless_file(env, tmp_path):
    ws = str(tmp_path / "ws2")
    Path(ws).mkdir()
    (Path(ws) / "AGENTS.local.md").write_text("handwritten\n")
    store = env[1]
    store.workspace_state_upsert(ws, "note", "hi", "there")
    p = wstate.project_state_file(ws, store)
    text = p.read_text()
    assert text.startswith("handwritten\n")
    assert wstate.BEGIN_MARKER in text


# ------------------------------------------------------------- promotion

def _summary(ws, span, **cols):
    return {"workspace": ws, "session": "s1", "span": span, **cols}


def test_promote_from_summary(env, tmp_path):
    ws = str(tmp_path / "ws")
    store = env[1]
    s = _summary(
        ws, "dsh:s1:seq[0..5]",
        decisions=json.dumps([{"subject": "use zstd", "choice": "yes",
                               "reason": "smaller"}]),
        problems=json.dumps(["flaky hook"]),
        open_items=json.dumps(["write tests"]))
    assert wstate.promote_from_summary(store, s) == 3
    assert store.workspace_state_active(
        ws, "decision", "use zstd")["body"] == "yes — smaller"
    assert store.workspace_state_active(
        ws, "problem", "flaky hook") is not None
    # idempotent: same content from the same span moves nothing
    assert wstate.promote_from_summary(store, s) == 0
    # same content from a later span: provenance advances, all 3 moved
    s2 = _summary(
        ws, "dsh:s1:seq[6..10]",
        decisions=json.dumps([{"subject": "use zstd", "choice": "yes",
                               "reason": "smaller"}]),
        problems=json.dumps(["flaky hook"]),
        open_items=json.dumps(["write tests"]))
    assert wstate.promote_from_summary(store, s2) == 3
    assert store.workspace_state_active(
        ws, "decision", "use zstd")["source_span"] == "dsh:s1:seq[6..10]"
    # windows without columns / without a workspace promote nothing
    assert wstate.promote_from_summary(store, {"workspace": ws}) == 0
    assert wstate.promote_from_summary(
        store, {"open_items": ["x"]}) == 0
    # corrupt columns never raise, never promote
    assert wstate.promote_from_summary(
        store, _summary(ws, "dsh:s1:seq[0..1]",
                        open_items="{corrupt")) == 0


def test_recently_handled_survives_same_second_closes(env):
    """Six closes inside one wall-clock second (second-precision
    timestamps tie) must still order deterministically: the last close
    written is the newest, always in the slice."""
    cfg, store = env
    ids = [store.workspace_state_upsert(
        "/proj", "open_item", f"t{i}", f"task {i}")["id"]
        for i in range(6)]
    for wid in ids:
        store.workspace_state_set(wid, "resolved")
    store.workspace_state_set(ids[2], "retired")  # the last write
    closed = [r for r in store.workspace_state_current("/proj")
              if r["status"] in ("resolved", "retired")]
    assert len(closed) == 5  # 6 closes, limit 5
    assert closed[0]["id"] == ids[2] and closed[0]["status"] == "retired"


# ------------------------------------------------------------- cli

def test_cli_state_add_set_show(env, tmp_path, monkeypatch, capsys):
    from memor import cli
    ws = str(tmp_path / "ws")
    rc = cli.main(["state", "add", "--category", "command",
                   "--subject", "pytest", "--body", "the venv one",
                   "--workspace", ws])
    assert rc == 0
    err = capsys.readouterr().err
    row = json.loads(err.splitlines()[0])
    # R2b: no file projection by default (the state rides the
    # injection envelope + on-demand fetch, not the instruction file)
    assert not (tmp_path / "ws" / "AGENTS.local.md").exists()
    # explicit projection
    capsys.readouterr()
    rc = cli.main(["state", "project", "--workspace", ws])
    assert rc == 0 and (tmp_path / "ws" / "AGENTS.local.md").exists()
    capsys.readouterr()
    rc = cli.main(["state", "show", "--workspace", ws])
    assert rc == 0
    out = capsys.readouterr().out
    assert "### Key commands (1)" in out and row["id"] in out
    # set resolved -> moves to Recently handled
    rc = cli.main(["state", "set", row["id"], "--status", "resolved",
                   "--workspace", ws])
    assert rc == 0
    capsys.readouterr()
    rc = cli.main(["state", "show", "--workspace", ws])
    out = capsys.readouterr().out
    assert "### Recently handled (1)" in out
    assert "### Key commands" not in out


def test_cli_state_add_projects_when_config_on(env, tmp_path, monkeypatch,
                                               capsys):
    from memor import cli
    cfg = env[0]
    cfg.session_state_project_file = True
    cfg.save()
    ws = str(tmp_path / "ws")
    rc = cli.main(["state", "add", "--category", "note", "--subject",
                   "hi", "--body", "there", "--workspace", ws])
    assert rc == 0
    assert (tmp_path / "ws" / "AGENTS.local.md").exists()


# ------------------------------------------------------------- mcp

def test_mcp_workspace_state_tools(env, tmp_path, monkeypatch):
    cfg, store = env
    import memor.mcp_server as m
    monkeypatch.setattr(m, "_cfg", cfg)
    ws = str(tmp_path / "ws")
    out = json.loads(m.tool_workspace_state_add(
        "convention", "venv", "use the /data one", ws))
    # R2b: no file projection by default
    assert out["ok"] and out["updated_file"] is None
    doc = m.tool_get_workspace_state(ws)
    assert "**venv**" in doc and out["id"] in doc
    # R4: bulk set — the result shape is {n, rows}
    out = json.loads(m.tool_workspace_state_set(out["id"], "retired"))
    assert out["n"] == 1 and out["rows"][0]["status"] == "retired"
    assert "### Recently handled" in m.tool_get_workspace_state(ws)
    # bulk: multiple ids in one call
    a = json.loads(m.tool_workspace_state_add(
        "open_item", "one", "a", ws))["id"]
    b = json.loads(m.tool_workspace_state_add(
        "open_item", "two", "b", ws))["id"]
    out = json.loads(m.tool_workspace_state_set([a, b], "resolved"))
    assert out["n"] == 2
    assert {r["id"] for r in out["rows"]} == {a, b}
    assert all(r["status"] == "resolved" for r in out["rows"])


# ------------------------------------------------- P1: dedup backstop

class FakeEmbedder:
    """Deterministic bag-of-words vectors (16 dim, L2-normalized):
    paraphrase overlap behaves like a real embedder for these purposes
    (same item ~0.9+, related-but-distinct ~0.5-0.8, distinct ~0)."""

    dim = 16

    def _vec(self, t: str) -> list:
        v = [0.0] * self.dim
        for w in t.lower().split():
            v[sum(ord(c) for c in w) % self.dim] += 1.0
        n = sum(x * x for x in v) ** 0.5 or 1.0
        return [x / n for x in v]

    def embed(self, texts):
        return [self._vec(t) for t in texts]


def _sum(ks: dict) -> dict:
    return {"workspace": "/proj", "session": "s1",
            "span": "dsh:s1:seq[0..5]", **ks}


def test_promote_merge_near_duplicate(env):
    cfg, store = env
    emb = FakeEmbedder()
    store.workspace_state_upsert(
        "/proj", "open_item", "Restart DSH host for R1 injection",
        "host restart needed for R1 injection", source_span="s1")
    n = wstate.promote_from_summary(
        store, _sum({"open_items": [
            "Restart DSH host for R1 SessionStart injection"]}),
        get_emb=lambda: emb)
    # the paraphrase merged into the existing row: no new row
    assert n == 1  # the row WAS updated (new body/provenance)
    rows = [r for r in store.workspace_state_list("/proj", status="active")
            if r["category"] == "open_item"]
    assert len(rows) == 1
    assert rows[0]["source_span"] == "dsh:s1:seq[0..5]"
    # identical re-promotion of the same span: idempotent no-op
    assert wstate.promote_from_summary(
        store, _sum({"open_items": [
            "Restart DSH host for R1 SessionStart injection"]}),
        get_emb=lambda: emb) == 0


def test_promote_distinct_creates(env):
    cfg, store = env
    store.workspace_state_upsert(
        "/proj", "open_item", "Restart DSH host for R1 injection",
        "host restart")
    n = wstate.promote_from_summary(
        store, _sum({"open_items": [
            "Fix the failing test suite in the harness"]}),
        get_emb=lambda: FakeEmbedder())
    assert n == 1
    assert len(store.workspace_state_list("/proj", status="active")) == 2


def test_promote_no_embedder_exact_key_only(env):
    cfg, store = env
    store.workspace_state_upsert(
        "/proj", "open_item", "Restart DSH host for R1 injection", "x")
    # get_emb resolves to None (no embedder available): exact-key only
    n = wstate.promote_from_summary(
        store, _sum({"open_items": [
            "Restart DSH host for R1 SessionStart injection"]}),
        get_emb=lambda: None)
    assert n == 1
    assert len(store.workspace_state_list("/proj", status="active")) == 2
    # and with dedup disabled entirely (get_emb=None) same behavior
    n = wstate.promote_from_summary(
        store, _sum({"open_items": ["yet another variant"]}))
    assert n == 1
    assert len(store.workspace_state_list("/proj", status="active")) == 3


def test_promote_ref_category_strict_threshold(env):
    """Decisions (reference records) need the stricter bar: a related-
    but-distinct decision does NOT merge at 0.90, while the same pair
    would merge as decaying items at 0.80."""
    cfg, store = env
    store.workspace_state_upsert(
        "/proj", "decision", "Decoupled state from file projection",
        "state decoupled from the file projection channel")
    cand = ("State decoupling from file projection channel",
            "state decoupling from the file projection channel")
    # decision: cosine ~0.82 < 0.90 ref threshold -> created
    n = wstate.promote_from_summary(
        store, _sum({"decisions": [{
            "subject": cand[0], "choice": cand[1], "reason": ""}]}),
        get_emb=lambda: FakeEmbedder())
    assert n == 1
    assert len(store.workspace_state_list("/proj", status="active")) == 2
    # same pair as an open item: cosine ~0.9+ > 0.80 -> merged
    store2 = Store(cfg.data_dir)
    store2.workspace_state_upsert(
        "/proj", "open_item", "Restart DSH host for R1 injection",
        "restart the dsh host for r1 injection")
    n = wstate.promote_from_summary(
        store2, _sum({"open_items": [
            "Restart DSH host for R1 SessionStart injection — "
            "restart the dsh host for r1 sessionstart injection"]}),
        get_emb=lambda: FakeEmbedder())
    assert n == 1  # merged, not created
    assert len([r for r in store2.workspace_state_list(
        "/proj", status="active") if r["category"] == "open_item"]) == 1


# ------------------------------------------------- P2: settling

def test_sweep_and_stale_lifecycle(env, tmp_path):
    cfg, store = env
    r = store.workspace_state_upsert("/proj", "open_item", "old", "x")
    # backdate the row 30 days
    from memor.models import now_utc
    old = "2026-08-01T00:00:00Z"
    store._conn().execute(
        "UPDATE workspace_state SET updated_at=? WHERE id=?",
        (old, r["id"]))
    store._conn().commit()
    # dry run: listed, untouched
    rows = store.workspace_state_sweep("/proj", 7, dry_run=True)
    assert [x["id"] for x in rows] == [r["id"]]
    assert store.workspace_state_get(r["id"])["status"] == "active"
    # real sweep: marked stale
    rows = store.workspace_state_sweep("/proj", 7)
    assert store.workspace_state_get(r["id"])["status"] == "stale"
    # stale is excluded from the envelope section...
    assert "old" not in wstate.state_section(store, "/proj")
    # ...but shown in the full document
    assert "### Stale (1)" in wstate.state_document(store, "/proj")
    # revivable
    store.workspace_state_set(r["id"], "active")
    assert store.workspace_state_get(r["id"])["status"] == "active"
    # a fresh row is not swept
    r2 = store.workspace_state_upsert("/proj", "open_item", "new", "y")
    assert store.workspace_state_sweep("/proj", 7) == []
    assert store.workspace_state_get(r2["id"])["status"] == "active"


def test_state_section_slim(env):
    cfg, store = env
    for i in range(3):
        store.workspace_state_upsert(
            "/proj", "open_item", f"open {i}", f"open body {i}")
    for i in range(3):
        store.workspace_state_upsert(
            "/proj", "decision", f"dec {i}", f"dec body {i}")
    for i in range(2):
        store.workspace_state_upsert(
            "/proj", "convention", f"conv {i}", f"conv body {i}")
    doc = wstate.state_section(store, "/proj")
    assert "### Open (3)" in doc
    assert "### Recent decisions (3)" in doc
    assert "### Standing knowledge (2)" in doc
    # actionable first: the Open section precedes decisions
    assert doc.index("### Open") < doc.index("### Recent decisions")
    assert doc.index("### Recent decisions") < doc.index(
        "### Standing knowledge")
    # most recently handled first within a section
    o = doc.index("**open 2**")
    assert o < doc.index("**open 1**") < doc.index("**open 0**")
    # the standing close instruction is present
    assert "Keep it true" in doc
    # empty workspace: ''
    assert wstate.state_section(store, "/nowhere") == ""


def test_registry_decaying_first(env):
    cfg, store = env
    from memor import sessionmem
    # decision created FIRST, open item last — the registry must still
    # lead with the open item (decaying first, rev-desc)
    store.workspace_state_upsert("/proj", "decision", "old decision", "x")
    store.workspace_state_upsert("/proj", "open_item", "hot item", "y")
    lines = sessionmem._active_state_lines(store, "/proj")
    assert lines.index("(open_item) hot item") < lines.index(
        "(decision) old decision")
    # newest open item above older ones
    store.workspace_state_upsert("/proj", "open_item", "hotter", "z")
    lines = sessionmem._active_state_lines(store, "/proj")
    assert lines.index("(open_item) hotter") < lines.index(
        "(open_item) hot item")


def test_show_filters_and_bulk_set_cli(env, tmp_path, monkeypatch, capsys):
    from memor import cli
    cfg = env[0]
    monkeypatch.setenv("MEMOR_DATA_DIR", str(cfg.data_dir))
    ws = str(tmp_path / "ws")
    ids = []
    for i in range(3):
        rc = cli.main(["state", "add", "--category", "open_item",
                       "--subject", f"item {i}", "--body", f"b{i}",
                       "--workspace", ws])
        assert rc == 0
        ids.append(json.loads(capsys.readouterr().err.splitlines()[0])["id"])
    rc = cli.main(["state", "show", "--category", "open_item",
                   "--workspace", ws])
    out = capsys.readouterr().out
    assert out.count(f"[{ids[0]}]") == 1 and "item 2" in out
    # --status filter
    capsys.readouterr()
    rc = cli.main(["state", "show", "--status", "resolved",
                   "--workspace", ws])
    assert "no matching state rows" in capsys.readouterr().out
    # bulk set: all three in one call
    capsys.readouterr()
    rc = cli.main(["state", "set", ids[0], ids[1], ids[2],
                   "--status", "resolved", "--workspace", ws])
    assert rc == 0
    out = json.loads(capsys.readouterr().err)
    assert out["n"] == 3 and all(r["status"] == "resolved"
                                  for r in out["rows"])
    # --limit
    capsys.readouterr()
    cli.main(["state", "show", "--status", "resolved", "--limit", "2",
              "--workspace", ws])
    assert capsys.readouterr().out.count("[wst_") == 2


# ------------------------------------------------- P4: dedupe

def test_dedupe_cli_dry_run_and_apply(env, tmp_path, monkeypatch, capsys):
    from memor import cli, embed
    cfg, store = env
    monkeypatch.setenv("MEMOR_DATA_DIR", str(cfg.data_dir))
    monkeypatch.setattr(embed, "get_embedder", lambda c: FakeEmbedder())
    ws = str(tmp_path / "ws")
    a = store.workspace_state_upsert(
        ws, "open_item", "Restart DSH host for R1 injection",
        "restart the dsh host for r1 injection")
    b = store.workspace_state_upsert(
        ws, "open_item", "Restart DSH host for R1 SessionStart injection",
        "restart the dsh host for r1 sessionstart injection")
    c = store.workspace_state_upsert(
        ws, "open_item", "Fix the failing test suite in the harness",
        "fix the failing test suite")
    # dry run: clusters listed, nothing changed
    rc = cli.main(["state", "dedupe", "--workspace", ws])
    assert rc == 0
    out = capsys.readouterr().out
    assert "KEEP" in out and f"[{b['id']}]" in out
    assert store.workspace_state_get(b["id"])["status"] == "active"
    # apply: the variant merges into the keeper (oldest)
    capsys.readouterr()
    rc = cli.main(["state", "dedupe", "--apply", "--workspace", ws])
    assert rc == 0
    assert f"[{b['id']}]" in capsys.readouterr().out
    assert store.workspace_state_get(a["id"])["status"] == "active"
    assert store.workspace_state_get(b["id"])["status"] == "superseded"
    assert store.workspace_state_get(b["id"])["superseded_by"] == a["id"]
    assert store.workspace_state_get(c["id"])["status"] == "active"
    # idempotent: nothing left to merge
    capsys.readouterr()
    rc = cli.main(["state", "dedupe", "--workspace", ws])
    assert "no near-duplicate clusters" in capsys.readouterr().out


def test_bulk_set_survives_bad_id(env, capsys):
    """R4: one bad id (typo, absorbed row) in a bulk close must not
    abort the rest — the good ids are applied and the bad one is
    reported per-id."""
    from memor import cli
    cfg, store = env
    ws = "/proj"
    good = [store.workspace_state_upsert(ws, "open_item", f"g{i}", "b")
            ["id"] for i in range(2)]
    rc = cli.main(["state", "set", good[0], "wst_deadbeef", good[1],
                   "--status", "resolved", "--workspace", ws])
    err = capsys.readouterr().err
    out = json.loads(err)
    assert rc == 0
    assert out["ok"] is False
    assert out["n"] == 2
    assert out["errors"] == [{"id": "wst_deadbeef",
                              "error":
                                  "unknown workspace_state id: wst_deadbeef"}]
    for wid in good:
        assert store.workspace_state_get(wid)["status"] == "resolved"


def test_state_section_recent_failures(env):
    """Closed problem rows inside the horizon surface as 'Recent
    failures' (negative knowledge: we tried this, it failed, here's how
    it was handled). Active problems stay in Open; rows older than the
    horizon drop out; a workspace with ONLY recent failures still
    renders (no active state needed)."""
    cfg, store = env
    ws = "/proj"
    r1 = store.workspace_state_upsert(ws, "problem", "p1", "b1")["id"]
    r2 = store.workspace_state_upsert(ws, "problem", "p2", "b2")["id"]
    store.workspace_state_set(r2, "resolved")
    doc = wstate.state_section(store, ws)
    assert "### Open (1)" in doc and "**p1**" in doc
    assert "### Recent failures (1)" in doc and "**p2**" in doc
    # the closed row carries its resolution stamp
    assert "— resolved " in doc
    # actionable-first: Open precedes the failures bucket
    assert doc.index("### Open") < doc.index("### Recent failures")
    # outside the horizon it drops out (backdate the close)
    store._conn().execute(
        "UPDATE workspace_state SET updated_at=? WHERE id=?",
        ("2020-01-01T00:00:00Z", r2))
    store._conn().commit()
    doc = wstate.state_section(store, ws)
    assert "Recent failures" not in doc and "**p1**" in doc
    # only recent failures, no active state: the section still renders
    store.workspace_state_set(r1, "resolved")
    doc = wstate.state_section(store, ws)
    assert doc and "### Recent failures (1)" in doc
    assert "**p1**" in doc and "### Open" not in doc
    # nothing active, nothing recent: ''
    store._conn().execute(
        "UPDATE workspace_state SET updated_at=? WHERE id=?",
        ("2020-01-01T00:00:00Z", r1))
    store._conn().commit()
    assert wstate.state_section(store, ws) == ""
