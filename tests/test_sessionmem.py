"""Session-memory (SESSION-MEMORY.md): session-log reader, window journaling,
summary trees, chaining, temporal rollups, retrieval, and the CLI surface.
All offline: synthetic DSH session logs, fake LLM, temp store."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from memor.config import Config
from memor.store import Store
from memor import sessionmem as sm
from memor import sessionlog as slog

MACH = "testbox"
ZSTD = "zstd" in (os.environ.get("PATH") or "")


def _has_bin(name: str) -> bool:
    from shutil import which
    return which(name) is not None


# ------------------------------------------------------------------ fixtures

@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMOR_DATA_DIR", str(tmp_path))
    cfg = Config.load()
    cfg.machine = MACH
    cfg.session_dsh_root = str(tmp_path / "dshroot")
    cfg.session_window_steps = 2
    cfg.session_chain_width = 2
    cfg.session_execution_mode = "resident"
    cfg.save()  # CLI commands reload config from the data dir
    store = Store(cfg.data_dir)
    return cfg, store


def make_session_log(tmp_path: Path, session_id: str, events: list[dict]) -> Path:
    """Write a synthetic DSH session log (zstd JSONL, like the real thing;
    plain JSONL when zstd is unavailable). Returns the log path."""
    d = tmp_path / "dshroot" / "sessions" / "--cwd--" / session_id
    d.mkdir(parents=True, exist_ok=True)
    plain = d / "session.v2.jsonl"
    plain.write_text("".join(json.dumps(e) + "\n" for e in events))
    zstd_path = d / "session.v2.jsonl.zstd"
    if _has_bin("zstd"):
        subprocess.run(["zstd", "-q", "-f", str(plain), "-o", str(zstd_path)],
                       check=True)
        os.remove(plain)
        return zstd_path
    plain.rename(zstd_path.with_suffix(".jsonl"))
    return zstd_path.with_suffix(".jsonl")


def base_events() -> list[dict]:
    return [
        {"type": "session", "version": 2, "id": "s1", "cwd": "/proj", "createdAt": 1},
        {"type": "turn/start", "seq": 0, "time": 2000, "data": {"turn": 1}},
        {"type": "user/message", "seq": 1, "time": 3000, "data": {
            "content": [{"type": "text", "text": "make it faster"}],
            "source": {"kind": "user"}, "role": "user"}},
        {"type": "assistant/message", "seq": 2, "time": 4000, "data": {
            "turn": 1, "message": {"role": "assistant", "content": [
                {"type": "reasoning", "text": "SECRET THINKING"},
                {"type": "text", "text": "I will profile it."}]}}},
        {"type": "tool/call", "seq": 3, "time": 5000, "data": {
            "turn": 1, "callId": "c1", "name": "bash",
            "arguments": json.dumps({"command": "perf run"})}},
        {"type": "tool/result", "seq": 4, "time": 6000, "data": {
            "turn": 1, "message": {"source": {"kind": "tool", "callId": "c1"},
                                   "content": [{"type": "tool-result",
                                                "toolCallId": "c1",
                                                "content": [{"type": "text",
                                                             "text": "hot: main()"}],
                                                "isError": False}]}}},
        {"type": "turn/end", "seq": 5, "time": 7000, "data": {"turn": 1}},
    ]


class FakeLLM:
    """Stands in for extractors: canned JSON summary, call counter."""
    def __init__(self):
        self.n = 0

    def available(self) -> bool:
        return True

    def _call(self, user: str, system: str | None = None) -> str:
        self.n += 1
        return json.dumps({
            "summary": f"summary-{self.n}", "subjects": ["memor.x"],
            "project": None, "component": None,
            "decisions": [{"subject": "s", "choice": "c", "reason": "r"}],
            "problems": [], "open": ["finish tests"]})


@pytest.fixture
def fake_llm(monkeypatch):
    llm = FakeLLM()
    monkeypatch.setattr(sm, "_extractor", lambda cfg: llm)
    return llm


def journal(cfg, store, session_id="s1", n_steps=2, cwd="/proj"):
    """Start a session, send one prompt, journal n_steps tool steps.
    (Steps drive windowing; prompts are content, never boundaries.)"""
    sm.session_event(cfg, store, "dsh", "start",
                     {"session_id": session_id, "cwd": cwd})
    sm.session_event(cfg, store, "dsh", "prompt",
                     {"session_id": session_id, "prompt": "work"})
    for i in range(n_steps):
        sm.session_event(cfg, store, "dsh", "tool",
                         {"session_id": session_id, "tool_name": "bash",
                          "tool_input": {}, "tool_response": "ok"})


# -------------------------------------------------------------------- spans

def test_span_parse_format():
    s = slog.span_format("dsh", "s1", 10, 20)
    assert s == "dsh:s1:seq[10..20]"
    assert slog.span_parse(s) == ("dsh", "s1", 10, 20)


def test_span_parse_rejects_garbage():
    for bad in ("", "dsh:s1", "dsh:s1:seq[20..10]", "dsh s1:seq[1..2]",
                "dsh:s1:seq[1]"):
        with pytest.raises(ValueError):
            slog.span_parse(bad)


# ------------------------------------------------------------ log reading

def test_find_session_file_and_range(tmp_path):
    p = make_session_log(tmp_path, "s1", base_events())
    found = slog.dsh_session_file("dsh", "s1", str(tmp_path / "dshroot"))
    assert found == p
    assert slog.dsh_session_file("dsh", "missing", str(tmp_path / "dshroot")) is None
    evs, first, last = slog.events_in_range(p, 1, 4)
    assert [e["seq"] for e in evs] == [1, 2, 3, 4]
    assert first == 0 and last == 5


def test_unknown_harness_rejected(tmp_path):
    with pytest.raises(slog.UnknownHarnessError):
        slog.dsh_session_file("claude", "s1", str(tmp_path / "dshroot"))


def test_read_span_roundtrip(tmp_path):
    p = make_session_log(tmp_path, "s1", base_events())
    evs = slog.read_span("dsh:s1:seq[2..4]", str(tmp_path / "dshroot"))
    assert [e["seq"] for e in evs] == [2, 3, 4]


def test_find_session_file_archive_fallback(tmp_path):
    """An archived session (moved out of sessions/ — any archive layout)
    is still drill-down-able; a live log always wins over an archived one."""
    live = make_session_log(tmp_path, "s1", base_events())
    a = tmp_path / "dshroot" / "archive" / "stale-project" / "s1"
    a.mkdir(parents=True)
    (a / "session.v2.jsonl").write_text(
        "".join(json.dumps(e) + "\n" for e in base_events()))
    assert slog.dsh_session_file("dsh", "s1", str(tmp_path / "dshroot")) == live
    # live log gone -> the archived copy is found
    shutil.rmtree(tmp_path / "dshroot" / "sessions" / "--cwd--" / "s1")
    assert slog.dsh_session_file(
        "dsh", "s1", str(tmp_path / "dshroot")) == a / "session.v2.jsonl"


def test_find_session_file_archive_only(tmp_path):
    """A session that exists ONLY in the archive (never live here): the
    span drill-down still resolves."""
    a = tmp_path / "dshroot" / "archive" / "2026" / "s9"
    a.mkdir(parents=True)
    evs = [dict(base_events()[0], id="s9")] + base_events()[1:]
    (a / "session.v2.jsonl").write_text(
        "".join(json.dumps(e) + "\n" for e in evs))
    found = slog.dsh_session_file("dsh", "s9", str(tmp_path / "dshroot"))
    assert found == a / "session.v2.jsonl"
    evs = slog.read_span("dsh:s9:seq[2..4]", str(tmp_path / "dshroot"))
    assert [e["seq"] for e in evs] == [2, 3, 4]


def test_torn_tail_line_never_kills(tmp_path):
    """A live log can end mid-line; the reader must skip the torn tail."""
    p = make_session_log(tmp_path, "s1", base_events())
    if p.suffix == ".zstd":
        # zstd files are self-delimiting; exercise the JSONL torn-tail path
        # directly with a plain file
        plain = tmp_path / "torn.jsonl"
        plain.write_text(p.read_text() if p.suffix != ".zstd" else "")
        if not plain.read_text():
            plain.write_text("".join(json.dumps(e) + "\n" for e in base_events()))
        plain.write_text(plain.read_text() + '{"type":"user/message","seq":6,"ti')
        evs = list(slog.iter_events(plain))
        assert [e.get("seq") for e in evs if e.get("seq") is not None] == \
            [0, 1, 2, 3, 4, 5]
    else:
        p.write_text(p.read_text() + '{"type":"user/message","seq":6,"ti')
        evs = list(slog.iter_events(p))
        assert [e.get("seq") for e in evs if e.get("seq") is not None] == \
            [0, 1, 2, 3, 4, 5]


# ----------------------------------------------------------------- digest

def test_digest_budgets_and_excludes_reasoning(env, tmp_path):
    cfg, store = env
    p = make_session_log(tmp_path, "s1", base_events())
    evs, _, _ = slog.events_in_range(p, 1, 10**9)
    digest, n_prompts, n_tools = sm.build_digest(cfg, evs)
    assert n_prompts == 1 and n_tools == 1
    assert "make it faster" in digest
    assert "I will profile it." in digest
    assert "SECRET THINKING" not in digest  # reasoning never in the digest
    assert "perf run" in digest and "hot: main()" in digest
    assert "COMPACT" not in digest


def test_digest_cap_elides_middle(env, tmp_path):
    cfg, store = env
    cfg.session_digest_max_chars = 1000  # tight window cap
    events = [{"type": "user/message", "seq": i, "time": i * 1000,
               "data": {"content": [{"type": "text", "text": "x" * 200}],
                        "source": {"kind": "user"}}}
              for i in range(50)]
    digest, n, _ = sm.build_digest(cfg, events)
    assert n == 50
    assert "elided" in digest
    assert len(digest) <= cfg.session_digest_max_chars + 200


def test_digest_keeps_long_prompt_and_prose_intact(env, tmp_path):
    """High-value signals survive: a 12k prompt and an 8k assistant message
    are kept whole under the default budgets (only paste-bombs truncate)."""
    cfg, store = env
    events = [
        {"type": "user/message", "seq": 0, "time": 1000,
         "data": {"content": [{"type": "text",
                               "text": "p" * 11000}],
                  "source": {"kind": "user"}}},
        {"type": "assistant/message", "seq": 1, "time": 2000,
         "data": {"message": {"role": "assistant", "content": [
             {"type": "text", "text": "a" * 7000}]}}},
    ]
    digest, n, _ = sm.build_digest(cfg, events)
    assert n == 1
    assert "p" * 10000 in digest and "a" * 6000 in digest
    assert "elided" not in digest


# ---------------------------------------------------------------- journal

def test_session_event_journals_and_bumps_state(env, monkeypatch):
    cfg, store = env
    cfg.session_window_steps = 2
    calls: list[tuple] = []
    monkeypatch.setattr(sm, "_spawn_worker",
                        lambda h, s, *a: calls.append((h, s)))
    cfg.session_execution_mode = "detached"
    r = sm.session_event(cfg, store, "dsh", "start",
                         {"session_id": "s1", "cwd": "/proj"})
    assert r["ok"] and not r["window_ready"]
    p1 = sm.session_event(cfg, store, "dsh", "prompt",
                          {"session_id": "s1", "prompt": "first", "cwd": "/proj"})
    assert not p1["spawned"]  # prompts are content, never window boundaries
    t1 = sm.session_event(cfg, store, "dsh", "tool",
                          {"session_id": "s1", "tool_name": "bash",
                           "tool_input": {}, "tool_response": "ok"})
    assert not t1["spawned"]
    t2 = sm.session_event(cfg, store, "dsh", "tool",
                          {"session_id": "s1", "tool_name": "bash",
                           "tool_input": {}, "tool_response": "ok"})
    assert t2["window_ready"] and t2["spawned"]
    assert calls == [("dsh", "s1")]

    rows = [e for e in store.recent_events(limit=50) if e["source"] == "session"]
    from collections import Counter
    assert Counter(e["kind"] for e in rows) == \
        Counter(["start", "prompt", "tool", "tool"])
    st = sm._sessions(store)["s1"]
    assert st["prompts"] == 1 and st["steps"] == 2 and st["flushing"]  # lock held until worker exits


def test_prompt_never_closes_window(env):
    # chosen semantics (pure-part): even a prompt right before the step
    # budget fills in cannot close the window — steps or Stop only
    cfg, store = env
    sm.session_event(cfg, store, "dsh", "start", {"session_id": "s1", "cwd": "/p"})
    sm.session_event(cfg, store, "dsh", "tool", {"session_id": "s1",
                                                 "tool_name": "bash",
                                                 "tool_input": {}})
    r = sm.session_event(cfg, store, "dsh", "prompt",
                         {"session_id": "s1", "prompt": "again"})
    assert not r["window_ready"] and not r["spawned"]


def test_not_wired_harness_not_journaled(env):
    cfg, store = env
    cfg.session_harnesses = []
    r = sm.session_event(cfg, store, "dsh", "prompt",
                         {"session_id": "s1", "prompt": "x"})
    assert not r["ok"] and r["reason"] == "not-wired"
    assert not [e for e in store.recent_events(limit=10) if e["source"] == "session"]


def test_stop_flushes_short_windows(env):
    cfg, store = env
    sm.session_event(cfg, store, "dsh", "start", {"session_id": "s1", "cwd": "/p"})
    sm.session_event(cfg, store, "dsh", "tool",
                     {"session_id": "s1", "tool_name": "bash",
                      "tool_input": {"command": "echo hi"}, "tool_response": "hi"})
    r = sm.session_event(cfg, store, "dsh", "stop", {"session_id": "s1"})
    assert r["window_ready"]  # stop counts even with 0 prompts


def test_empty_stdin_payload_ignored(env):
    cfg, store = env
    r = sm.session_event(cfg, store, "dsh", "prompt", {})
    assert not r["ok"] and r["reason"] == "no session_id"


# ------------------------------------------------------------------ flush

def test_flush_creates_l1_with_span(env, tmp_path, fake_llm):
    cfg, store = env
    make_session_log(tmp_path, "s1", base_events())
    journal(cfg, store, n_steps=2)
    r = sm.flush_window(cfg, store, "dsh", "s1")
    assert r["ok"] and r["status"] == "current"
    assert r["span"] == "dsh:s1:seq[0..5]"
    s = store.get_summary(r["summary_id"])
    assert s["level"] == 1 and s["span"] == r["span"] and s["subjects"] == ["memor.x"]
    # the text is plain-English summary prose only; structured material
    # lives in its own columns
    assert s["text"] == "summary-1"
    assert s["decisions"] == [{"subject": "s", "choice": "c", "reason": "r"}]
    assert s["open_items"] == ["finish tests"]
    assert s["problems"] == []
    # state advanced: next window starts after seq 5
    st = sm._sessions(store)["s1"]
    assert st["last_span_hi"] == 5 and st["prompts"] == 0
    # a memor event was journaled
    assert any(e["kind"] == "summary_written"
               for e in store.recent_events(limit=10))


def test_flush_promotes_workspace_state(env, tmp_path, fake_llm):
    """The flush worker promotes the window's structured extraction into
    workspace state. The AGENTS.local.md file is written only when
    session_state_project_file is on (R2b: the state rides the envelope +
    on-demand fetch, not the instruction file)."""
    cfg, store = env
    ws = str(tmp_path / "ws")
    make_session_log(tmp_path, "s1", base_events())
    journal(cfg, store, n_steps=2, cwd=ws)
    # default: no file
    r = sm.flush_window(cfg, store, "dsh", "s1")
    assert r["status"] == "current"
    d = store.workspace_state_active(ws, "decision", "s")
    assert d is not None and d["body"] == "c — r"
    assert d["source_span"] == "dsh:s1:seq[0..5]"
    assert store.workspace_state_active(
        ws, "open_item", "finish tests") is not None
    assert not (tmp_path / "ws" / "AGENTS.local.md").exists()
    # opt-in file projection: next flush (new session, same workspace)
    # re-promotes the same content — row refreshed in place (provenance
    # advances) and the file appears
    cfg.session_state_project_file = True
    cfg.save()
    make_session_log(tmp_path, "s2", base_events())
    journal(cfg, store, session_id="s2", n_steps=2, cwd=ws)
    r = sm.flush_window(cfg, store, "dsh", "s2")
    assert r["status"] == "current"
    d2 = store.workspace_state_active(ws, "decision", "s")
    assert d2["id"] == d["id"] and d2["source_span"] == "dsh:s2:seq[0..5]"
    f = tmp_path / "ws" / "AGENTS.local.md"
    assert f.exists() and "AGENTS.local.md" in str(f)
    assert (f.read_text().count(d["id"])) >= 1


def test_flush_no_progress_when_log_unchanged(env, tmp_path, fake_llm):
    cfg, store = env
    make_session_log(tmp_path, "s1", base_events())
    journal(cfg, store, n_steps=2)
    r1 = sm.flush_window(cfg, store, "dsh", "s1")
    assert r1["ok"]
    r2 = sm.flush_window(cfg, store, "dsh", "s1", force=True)
    assert not r2["ok"] and r2["reason"] == "no-progress"


def test_flush_missing_log_keeps_state(env, tmp_path, fake_llm):
    cfg, store = env
    journal(cfg, store, n_steps=2)  # no log file at all
    r = sm.flush_window(cfg, store, "dsh", "s1")
    assert not r["ok"] and r["reason"] == "no-session-log"
    # state intact, no lock leak
    st = sm._sessions(store)["s1"]
    assert st["prompts"] == 1 and st["steps"] == 2 and st["flushing"] is None


def test_llm_down_stays_pending_then_stub(env, tmp_path, monkeypatch):
    cfg, store = env
    cfg.session_max_flush_retries = 2
    make_session_log(tmp_path, "s1", base_events())
    journal(cfg, store, n_steps=2)
    monkeypatch.setattr(sm, "_extractor", lambda cfg: None)
    r1 = sm.flush_window(cfg, store, "dsh", "s1")
    assert not r1["ok"] and r1["reason"] == "llm-unavailable" and r1["retry"] == 1
    assert store.list_summaries() == []  # still pending
    r2 = sm.flush_window(cfg, store, "dsh", "s1", force=True)
    assert r2["ok"] and r2["status"] == "partial"
    s = store.get_summary(r2["summary_id"])
    assert s["status"] == "partial" and "[stub]" in s["text"]
    assert s["span"] == "dsh:s1:seq[0..5]"  # raw data still addressable


def test_noise_only_window_is_skipped(env, tmp_path, fake_llm):
    cfg, store = env
    evs = [base_events()[0]] + [
        {"type": "tool/call", "seq": i, "time": 9000 + i, "data": {
            "callId": f"c{i}", "name": "bash", "arguments": "{}"}}
        for i in (1, 2)
    ]
    make_session_log(tmp_path, "s1", evs)
    sm.session_event(cfg, store, "dsh", "start", {"session_id": "s1", "cwd": "/p"})
    sm.session_event(cfg, store, "dsh", "tool", {"session_id": "s1",
                                                 "tool_name": "bash",
                                                 "tool_input": {}})
    sm.session_event(cfg, store, "dsh", "stop", {"session_id": "s1"})
    r = sm.flush_window(cfg, store, "dsh", "s1")
    assert r["ok"] and r["reason"] == "noise-skipped"
    assert store.list_summaries() == []
    # pointer advanced, tree not littered
    assert sm._sessions(store)["s1"]["last_span_hi"] == 2


# --------------------------------------------------------------- chaining

def test_fold_chains_width_two(env, fake_llm):
    cfg, store = env
    a = store.insert_summary(session="s1", project="proj", level=1,
                             span="dsh:s1:seq[0..5]", text="win A", subjects=["a"])
    b = store.insert_summary(session="s1", project="proj", level=1,
                             span="dsh:s1:seq[6..9]", text="win B", subjects=["b"])
    other = store.insert_summary(session="s2", project="otherproj", level=1,
                                 span="dsh:s2:seq[0..3]", text="win C", subjects=[])
    made = sm.fold_chains(cfg, store, "proj")
    assert len(made) == 1
    parent = store.get_summary(made[0])
    assert parent["level"] == 2
    assert parent["references"] == [a, b]
    assert parent["text"].startswith("summary-")  # LLM fold
    assert store.get_summary(a)["parent_id"] == parent["id"]
    assert store.get_summary(b)["parent_id"] == parent["id"]
    assert store.get_summary(other)["parent_id"] is None  # never cross projects
    # frontier rebuilt: a fresh level-1 alone does not re-fold
    c = store.insert_summary(session="s1", project="proj", level=1,
                             span="dsh:s1:seq[10..12]", text="win D", subjects=[])
    assert sm.fold_chains(cfg, store, "proj") == []


def test_fold_without_llm_is_deterministic_join(env, monkeypatch):
    cfg, store = env
    monkeypatch.setattr(sm, "_extractor", lambda cfg: None)
    a = store.insert_summary(session="s1", project="p", level=1,
                             span="dsh:s1:seq[0..5]", text="win A", subjects=[])
    b = store.insert_summary(session="s1", project="p", level=1,
                             span="dsh:s1:seq[6..9]", text="win B", subjects=[])
    made = sm.fold_chains(cfg, store, "p")
    assert len(made) == 1
    assert "folded, LLM unavailable" in store.get_summary(made[0])["text"]
    assert "win A" in store.get_summary(made[0])["text"]


def test_latest_root_summaries_surface(env):
    cfg, store = env
    l1 = store.insert_summary(session="s1", project="p", level=1,
                              span="dsh:s1:seq[0..5]", text="leaf",
                              ts="2020-01-01T00:00:00Z")
    l2 = store.insert_summary(session="s1", project="p", level=2,
                              references=[l1], text="root",
                              ts="2020-01-02T00:00:00Z")
    # the newest parentless node per session is the surface: the L2 fold,
    # not the (older) L1 it covers
    roots = store.latest_root_summaries(project="p", limit=5)
    assert [r["id"] for r in roots] == [l2]
    # project filter includes box-wide (project NULL) summaries
    box = store.insert_summary(session="s2", level=1, span="dsh:s2:seq[0..1]",
                               text="box-wide window",
                               ts="2020-01-03T00:00:00Z")
    roots = store.latest_root_summaries(project="p", limit=5)
    assert {r["id"] for r in roots} == {l2, box}


# --------------------------------------------------------------- temporal

def test_temporal_day_rollup_lazy_and_cached(env, fake_llm):
    cfg, store = env
    store.insert_summary(machine=MACH, session="s1", project="p", level=1,
                         span="dsh:s1:seq[0..5]", text="day work",
                         ts="2026-09-10T10:00:00Z")
    day = date(2026, 9, 10)
    r1 = sm.temporal_rollup(cfg, store, MACH, "day", day)
    assert r1["materialized"] is True and r1["id"]
    assert r1["text"].startswith("summary-")
    cached = store.find_temporal(MACH, "day:2026-09-10")
    assert cached and cached["id"] == r1["id"]
    r2 = sm.temporal_rollup(cfg, store, MACH, "day", day)
    assert r2["materialized"] is False  # served from cache
    assert r2["id"] == r1["id"]
    # empty day: no rollup, nothing cached
    empty = sm.temporal_rollup(cfg, store, MACH, "day", date(2026, 1, 1))
    assert empty["materialized"] is False and empty["reason"] == "no-summaries-that-day"


def test_temporal_week_folds_days(env, fake_llm):
    cfg, store = env
    for i in range(3):
        store.insert_summary(machine=MACH, session="s1", project=f"p{i}",
                             level=1, span=f"dsh:s1:seq[{i}..{i+2}]",
                             text=f"day-{i} work",
                             ts=f"2026-09-{10+i}T10:00:00Z")
    w = sm.temporal_rollup(cfg, store, MACH, "week", date(2026, 9, 10))
    assert w["materialized"] is True
    assert w["text"].startswith("summary-")  # LLM fold over the day rollups


# ------------------------------------------------------------ injection

def test_injection_package(env):
    """Without a workspace the package is the project's top-of-tree
    summaries — nothing else (no state/todos/problems/decisions
    remainder)."""
    cfg, store = env
    root = store.insert_summary(session="s1", project="memor", level=2,
                                references=[], text="root summary body",
                                subjects=[])
    store.insert_fact(
        scope_project="memor", scope_component=None, scope_machine=None,
        subject="service:memor", claim="running on 3080",
        kind="state", confidence="confirmed", source_type="agent")
    inj = sm.injection_context(cfg, store, "memor")
    assert inj.startswith("## Latest session summaries")
    assert "root summary body" in inj
    assert "running on 3080" not in inj       # no environment remainder
    assert "## Current state" not in inj
    cfg.session_context_enabled = False
    assert sm.injection_context(cfg, store, "memor") == ""


def _build_tree(store, workspace: str, session_id: str, n_l1: int,
                two_child_l3: bool = False,
                attach_at: int | None = None) -> list:
    """Insert an L1→apex chain directly (no LLM): n_l1 L1 windows with
    spans, then 3:1 folds upward. `two_child_l3` leaves the newest L2
    unparented (its L3 takes only 2 children) to create a frontier.
    `attach_at` gives the nodes at that level an attached section
    (## Decisions) — the kind of material the injection must not list."""
    def ins(level, references, span=None, session=None):
        text = f"L{level} summary body " + "detail. " * 30
        if level == attach_at:
            text += ("\n\n## Decisions\n"
                     "- attached-choice — attached-reason\n"
                     "## Open\n- attached-todo")
        return store.insert_summary(
            session=session, project="memor", workspace=workspace,
            level=level, references=references, span=span,
            text=text, subjects=[])
    lvl = [ins(1, [], span=f"dsh:{session_id}:seq[{i * 5}..{i * 5 + 4}]",
               session=session_id) for i in range(n_l1)]
    level = 1
    while len(lvl) >= 3:
        keep = lvl[:-1] if (two_child_l3 and level == 2) else lvl
        lvl = [ins(level + 1, keep[k:k + 3])
               for k in range(0, len(keep), 3)]
        level += 1
    return lvl


def test_session_tree_context_structure(env, tmp_path):
    """A/B/C/D are constructed deterministically from the store; scoping is
    computed from the reference graph (covered children are never re-listed);
    every entry carries its FULL stored text (no budgets); the document
    explains its own structure."""
    cfg, store = env
    _build_tree(store, "/proj", "tt1", 81, two_child_l3=True, attach_at=2)
    make_session_log(tmp_path, "tt1", _gap_events("tt1", 1))
    sm.session_event(cfg, store, "dsh", "start",
                     {"session_id": "tt1", "cwd": "/proj"})
    sm.session_event(cfg, store, "dsh", "stop", {"session_id": "tt1"})
    sessions = sm._sessions(store)
    sessions["tt1"]["last_span_hi"] = -1     # nothing flushed yet
    sm._save_sessions(store, sessions)
    doc = sm.session_tree_context(cfg, store, "/proj")
    assert doc.startswith("# Workspace session history — /proj")
    assert "How to read this document" in doc     # self-explanatory
    assert "## A. Apex (L5" in doc
    assert "## B. Mid series — all L3 (9 nodes" in doc
    assert "### L2 — uncovered (1" in doc         # the frontier L2
    assert "### L1 — last 6 windows" in doc
    assert "## D. Raw tail" in doc and "user: gap 0" in doc
    # the B-level in 'how to read' is computed from the live tree
    assert "computed as L-Max − 2 from the live tree" in doc
    # the document ends by telling how to go deeper than it
    # entries are time-bounded: the doc says so (no stale-open misread)
    assert "Every entry describes its own window" in doc
    assert "Current state = the newest entries + D." in doc
    assert "## Traversing (when the above isn't enough)" in doc
    assert "memor span" in doc and "memory_drill_down" in doc
    assert doc.rstrip().endswith("memor section.")
    # full text: an L3 entry carries all 30 "detail." repetitions,
    # unelided (no ellipsis marker at the end)
    assert doc.count("detail. detail. detail. detail. detail.") >= 9
    assert "L3 summary body " + "detail. " * 29 + "detail." in doc
    # attached sections are store material, never injected: the L2
    # entries show their prose, not their ## Decisions / ## Open material
    assert "L2 summary body" in doc          # the uncovered frontier L2
    assert "attached-choice" not in doc
    assert "attached-todo" not in doc
    # the raw tail is a representative line per event
    for line in ("#0 user: gap 0", "#1 call bash: {}", "#2 result ok: ok",
                 "#3 call bash: {}", "#4 result ok: ok"):
        assert line in doc
    # scoping: covered L1s are never listed — only the last 6
    for i in list(range(24)) + [70]:
        assert f"[{i * 5}..{i * 5 + 4}]" not in doc
    for i in range(75, 81):
        assert f"[{i * 5}..{i * 5 + 4}]" in doc


def test_d_tail_collapses_harness_reminders(env, tmp_path):
    """Harness reminders arrive as user-role events whose payload is a
    FILE, not the user's words (an AGENTS.md reload re-injects the whole
    instruction file). D renders them as ONE representative line — source,
    size, pointer to the raw span — so a mid-session instruction reload
    never clutters the tail; real user messages stay whole."""
    cfg, store = env
    _build_tree(store, "/proj", "rem1", 4)
    reminder = ("<system-reminder>\n"
                 "Updated instructions from: ~/.dsh/AGENTS.md\n"
                 "\n"
                 "This file changed after it was loaded. Use the following "
                 "content instead.\n"
                 + "line of instructions\n" * 40
                 + "\n</system-reminder>")
    make_session_log(tmp_path, "rem1", [
        dict(base_events()[0], id="rem1"),
        {"type": "user/message", "seq": 0, "time": 1,
         "data": {"content": [{"type": "text", "text": "real user words"}],
                  "source": {"kind": "user"}, "role": "user"}},
        {"type": "user/message", "seq": 1, "time": 2,
         "data": {"content": [{"type": "text", "text": reminder}],
                  "source": {"kind": "user"}, "role": "user"}},
    ])
    sm.session_event(cfg, store, "dsh", "start",
                     {"session_id": "rem1", "cwd": "/proj"})
    sm.session_event(cfg, store, "dsh", "stop", {"session_id": "rem1"})
    sessions = sm._sessions(store)
    sessions["rem1"]["last_span_hi"] = -1
    sm._save_sessions(store, sessions)
    doc = sm.session_tree_context(cfg, store, "/proj")
    # real user messages stay whole
    assert "#0 user: real user words" in doc
    # the reminder is exactly ONE line: source + size + pointer to the raw
    # span
    assert doc.count("#1 user:") == 1
    line = [l for l in doc.split("\n") if l.startswith("#1 user:")][0]
    assert line.startswith("#1 user: [system-reminder: "
                           "~/.dsh/AGENTS.md reloaded -")
    assert "lines," in line and "chars; full text in the raw span" in line
    # the reminder body is not in the document at all
    assert "line of instructions" not in doc
    assert "This file changed" not in doc


def test_compact_args_representative():
    """The representative field wins; content payloads never do; JSON
    strings (how DSH carries arguments) are parsed, not dumped."""
    assert sm._compact_args('{"command": "ls -la /tmp"}') == \
        "command=ls -la /tmp"
    assert sm._compact_args({"file_path": "/a/b.py",
                             "new_string": "x" * 500}) == "file_path=/a/b.py"
    assert sm._compact_args("plain text args") == "plain text args"
    assert sm._compact_args(None) == ""
    long = sm._compact_args("z" * 300)
    assert long == "z" * 120 + "…"


def test_injection_leads_with_tree(env, tmp_path):
    """With a workspace, the injection package IS the tree section —
    nothing else (no environment remainder). Without one, the project's
    top-of-tree summaries."""
    cfg, store = env
    _build_tree(store, "/proj", "tt1", 81)
    store.insert_fact(
        scope_project="memor", scope_component=None, scope_machine=None,
        subject="svc:x", claim="up", kind="state",
        confidence="confirmed", source_type="agent")
    inj = sm.injection_context(cfg, store, "memor", workspace="/proj")
    assert inj.startswith("# Workspace session history — /proj")
    assert inj == sm.session_tree_context(cfg, store, "/proj")
    assert "## Current state" not in inj and "svc:x" not in inj
    inj2 = sm.injection_context(cfg, store, "memor")
    assert inj2.startswith("## Latest session summaries")


def test_injection_review_section(env, tmp_path):
    """Open review items (unresolved contradictions / pending proposals)
    appear in the workspace envelope after the tree — and only when any
    are open. The queue is human territory: the section points at
    `memor review`, it never resolves anything itself."""
    cfg, store = env
    _build_tree(store, "/proj", "tt-rvw", 4)
    assert "## Open review items" not in sm.injection_context(
        cfg, store, "memor", workspace="/proj")
    rid = store.queue_review("prop-1", "contradiction",
                             "port conflict: 8080 vs 8000")
    inj = sm.injection_context(cfg, store, "memor", workspace="/proj")
    assert "## Open review items (1)" in inj
    assert f"[{rid}] (contradiction) port conflict" in inj
    # the review section follows the tree
    assert inj.index("# Workspace session history") < inj.index(
        "## Open review items")
    # resolved queue → section disappears
    store.resolve_review(rid, "human: 8080 is right")
    inj2 = sm.injection_context(cfg, store, "memor", workspace="/proj")
    assert "## Open review items" not in inj2


# ------------------------------------------------------------------- cli

def _main(argv):
    # MEMOR_DATA_DIR (set by the env fixture) locates the store
    from memor import cli
    return cli.main(argv)


def test_cli_session_event_stdin(env, monkeypatch, capsys):
    cfg, store = env
    monkeypatch.setattr(sm, "_spawn_worker", lambda h, s, *a: None)
    import io, sys
    payload = json.dumps({"session_id": "s1", "prompt": "via stdin",
                          "cwd": "/proj"})
    monkeypatch.setattr(sys, "stdin", io.StringIO(payload))
    rc = _main(["session-event", "--harness", "dsh", "--event", "prompt"])
    assert rc == 0
    assert any(e["kind"] == "prompt" for e in store.recent_events(limit=10))


def test_cli_span_reads_log(env, tmp_path, capsys):
    cfg, store = env
    make_session_log(tmp_path, "s1", base_events())
    rc = _main(["span", "dsh:s1:seq[1..4]"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "make it faster" in out
    assert "user/message" in out
    assert "tool/call" in out and "bash" in out
    assert "SECRET THINKING" not in out  # preview shows text blocks only


def test_cli_switchover_applies_regime(env, tmp_path):
    cfg, store = env
    cfg.consolidate_interval = 3600.0
    cfg.llm_autosync = True
    cfg.save()
    store.meta_set("llm.cursor", "2026-01-01T00:00:00Z")
    rc = _main(["switchover"])
    assert rc == 0
    cfg2 = Config.load(cfg.data_dir)
    assert cfg2.consolidate_interval == 0.0
    assert cfg2.llm_autosync is False
    assert cfg2.verify_enabled is False
    assert cfg2.session_journal_enabled is True  # new regime on
    # pending queue lapsed: cursor advanced past 2026-01-01
    assert store.meta_get("llm.cursor") > "2026-06-01"
    # and it is journaled
    assert any(e["kind"] == "session_memory_switchover"
               for e in store.recent_events(limit=10))
    # --keep-verify preserves state polling
    rc = _main(["switchover", "--keep-verify"])
    assert rc == 0
    assert Config.load(cfg.data_dir).verify_enabled is True


def test_cli_window_flush_all(env, tmp_path, fake_llm):
    cfg, store = env
    make_session_log(tmp_path, "s1", base_events())
    journal(cfg, store, "s1", n_steps=2)
    rc = _main(["window-flush", "--all"])
    assert rc == 0
    assert store.list_summaries(session="s1")


def test_cli_inject_envelope(env, capsys):
    """inject --envelope: stdout is the hookSpecificOutput envelope the
    hooks-claude-code bridge merges into the session-start user message;
    status goes to stderr (never the envelope); empty doc = empty stdout
    (the bridge then injects nothing)."""
    cfg, store = env
    store.insert_summary(session="s1", workspace="/proj", level=1,
                         span="dsh:s1:seq[0..5]", text="first window")
    rc = _main(["inject", "--envelope", "--workspace", "/proj"])
    assert rc == 0
    out, err = capsys.readouterr()
    env_doc = json.loads(out)  # stdout must be exactly one JSON object
    hso = env_doc["hookSpecificOutput"]
    assert hso["hookEventName"] == "SessionStart"
    assert hso["additionalContext"].startswith("# Workspace session history")
    assert "## Current state" not in hso["additionalContext"]
    status = json.loads(err)
    assert status["injected"] is True and status["chars"] > 0

    # empty doc: no stdout at all, status says injected: false
    capsys.readouterr()
    rc = _main(["inject", "--envelope", "--workspace", "/empty"])
    assert rc == 0
    out, err = capsys.readouterr()
    assert out == ""
    assert json.loads(err)["injected"] is False


def test_cli_inject_raw(env, capsys):
    """inject without --envelope: the raw doc on stdout (manual/dev)."""
    cfg, store = env
    store.insert_summary(session="s1", workspace="/proj", level=1,
                         span="dsh:s1:seq[0..5]", text="first window")
    rc = _main(["inject", "--workspace", "/proj"])
    assert rc == 0
    out, _ = capsys.readouterr()
    assert out.startswith("# Workspace session history")


def test_cli_inject_user_prompt_once_per_session(env, monkeypatch, capsys):
    """R5: --point user-prompt is the guaranteed first-turn delivery —
    the envelope rides the turn's pre-step (hookEventName must match the
    firing point or the bridge discards it), and the store marker makes it
    run exactly once per session lifetime: second prompt emits nothing."""
    import io, sys
    cfg, store = env
    store.insert_summary(session="s1", workspace="/proj", level=1,
                         span="dsh:s1:seq[0..5]", text="first window")
    assert not store.envelope_delivered("s1")

    payload = json.dumps({"session_id": "s1", "prompt": "hi"})
    monkeypatch.setattr(sys, "stdin", io.StringIO(payload))
    rc = _main(["inject", "--envelope", "--point", "user-prompt",
                "--workspace", "/proj"])
    assert rc == 0
    out, err = capsys.readouterr()
    env_doc = json.loads(out)  # stdout is exactly one JSON object
    hso = env_doc["hookSpecificOutput"]
    assert hso["hookEventName"] == "UserPromptSubmit"
    assert hso["additionalContext"].startswith("# Workspace session history")
    assert json.loads(err)["injected"] is True
    assert store.envelope_delivered("s1")

    # second prompt of the same session: nothing emitted, marker intact
    capsys.readouterr()
    monkeypatch.setattr(sys, "stdin", io.StringIO(payload))
    rc = _main(["inject", "--envelope", "--point", "user-prompt",
                "--workspace", "/proj"])
    assert rc == 0
    out, err = capsys.readouterr()
    assert out == ""
    status = json.loads(err)
    assert status["injected"] is False
    assert status["already-delivered"] is True
    assert store.envelope_delivered("s1")

    # a different session is unaffected
    assert not store.envelope_delivered("s2")


def test_cli_inject_user_prompt_requires_session_id(env, monkeypatch,
                                                     capsys):
    """No session id on stdin (non-hook/manual misuse) = no delivery, no
    marker, clean exit (the hook must never fail the turn)."""
    import io, sys
    cfg, store = env
    store.insert_summary(session="s1", workspace="/proj", level=1,
                         span="dsh:s1:seq[0..5]", text="first window")
    for bad in ("", json.dumps({"prompt": "no id"}), "not json"):
        monkeypatch.setattr(sys, "stdin", io.StringIO(bad))
        rc = _main(["inject", "--envelope", "--point", "user-prompt",
                    "--workspace", "/proj"])
        assert rc == 0
        out, err = capsys.readouterr()
        assert out == ""
        assert json.loads(err)["ok"] is False
    assert not store.envelope_delivered("s1")


def test_cli_inject_user_prompt_empty_doc_sets_no_marker(env, monkeypatch,
                                                          capsys):
    """No history yet: emit nothing AND leave the marker unset, so the
    first prompt after the tree appears still delivers."""
    import io, sys
    cfg, store = env
    monkeypatch.setattr(sys, "stdin", io.StringIO(
        json.dumps({"session_id": "s9", "prompt": "hi"})))
    rc = _main(["inject", "--envelope", "--point", "user-prompt",
                "--workspace", "/nope"])
    assert rc == 0
    out, err = capsys.readouterr()
    assert out == ""
    assert json.loads(err)["injected"] is False
    assert not store.envelope_delivered("s9")


def test_envelope_marker_roundtrip(env):
    """Store side: the table is created on open; mark is idempotent."""
    cfg, store = env
    assert not store.envelope_delivered("s1")
    store.mark_envelope_delivered("s1")
    assert store.envelope_delivered("s1")
    store.mark_envelope_delivered("s1")  # no duplicate
    rows = store._conn().execute(
        "SELECT session FROM envelope_delivered").fetchall()
    assert [r[0] for r in rows] == ["s1"]


def test_injection_envelope_carries_workspace_state(env, capsys):
    """R2b: the state rides the SessionStart envelope (boot-time
    catch-up) — active rows only, as a trailing section. No state:
    no section at all."""
    cfg, store = env
    store.insert_summary(session="s1", workspace="/proj", level=1,
                         span="dsh:s1:seq[0..5]", text="first window")
    # no state yet: tree only
    rc = _main(["inject", "--workspace", "/proj"])
    assert rc == 0
    out, _ = capsys.readouterr()
    assert "## Workspace state" not in out
    # active state: trailing section, active rows only
    store.workspace_state_upsert("/proj", "open_item", "ship it", "soon")
    store.workspace_state_upsert("/proj", "decision", "storage", "sqlite")
    rc = _main(["inject", "--workspace", "/proj"])
    out, _ = capsys.readouterr()
    assert "## Workspace state — /proj" in out
    # R4/P3: actionable-first layout — Open / Recent decisions / Standing
    assert "### Open (1)" in out and "**ship it**" in out
    assert "### Recent decisions (1)" in out and "**storage**" in out
    assert out.index("### Open") < out.index("### Recent decisions")
    assert "### Recently handled" not in out  # active-only in the envelope
    assert "Keep it true" in out  # the standing close instruction
    # a resolved row must not appear in the envelope
    row = store.workspace_state_active("/proj", "open_item", "ship it")
    store.workspace_state_set(row["id"], "resolved")
    rc = _main(["inject", "--workspace", "/proj"])
    out, _ = capsys.readouterr()
    assert "## Workspace state — /proj" in out
    assert "### Recent decisions (1)" in out
    assert "### Open" not in out and "ship it" not in out


# ------------------------------------------------------------------- mcp

def test_mcp_span_and_drill(env, tmp_path, monkeypatch, fake_llm):
    cfg, store = env
    import memor.mcp_server as m
    monkeypatch.setattr(m, "_cfg", cfg)
    make_session_log(tmp_path, "s1", base_events())
    journal(cfg, store, n_steps=2)
    r = sm.flush_window(cfg, store, "dsh", "s1")
    out = json.loads(m.tool_get_span(span=r["span"]))
    assert out["count"] == 6
    assert [e["type"] for e in out["events"]][:2] == ["turn/start", "user/message"]
    leaf = json.loads(m.tool_drill_down(r["summary_id"]))
    assert len(leaf["raw_events"]) == 6
    # reasoning is NOT in the MCP preview either
    blob = json.dumps(leaf["raw_events"])
    assert "SECRET THINKING" not in blob

    # missing log -> structured error, no exception
    err = json.loads(m.tool_get_span(span="dsh:ghost:seq[1..2]"))
    assert "error" in err


def test_mcp_search_includes_summaries(env, tmp_path, monkeypatch, fake_llm):
    cfg, store = env
    import memor.mcp_server as m
    monkeypatch.setattr(m, "_cfg", cfg)
    make_session_log(tmp_path, "s1", base_events())
    journal(cfg, store, n_steps=2)
    r = sm.flush_window(cfg, store, "dsh", "s1")
    # R3: unified {hits} shape (was per-kind buckets); fts mode is
    # deterministic — the semantic leg would need an embedder
    hits = json.loads(m.tool_search("summary-1", mode="fts"))
    ids = [h["id"] for h in hits["hits"] if h["kind"] == "summary"]
    assert r["summary_id"] in ids


def test_mcp_context_shows_root_summaries(env, monkeypatch):
    cfg, store = env
    import memor.mcp_server as m
    monkeypatch.setattr(m, "_cfg", cfg)
    store.insert_summary(session="s1", project="memor", level=2,
                         references=[], text="recent work on retrieval",
                         subjects=["memor.retrieval"])
    out = m.tool_get_project_context("memor")
    assert "Recent session summaries" in out
    assert "recent work on retrieval" in out


def test_mcp_timeline_includes_summaries(env, monkeypatch):
    cfg, store = env
    import memor.mcp_server as m
    monkeypatch.setattr(m, "_cfg", cfg)
    sid = store.insert_summary(session="s1", project="memor", level=1,
                               span="dsh:s1:seq[0..5]", text="a window")
    out = m.tool_get_timeline("memor", 24 * 365 * 10)
    assert f"summary:{sid}" in out


# ------------------------------------------------------------- watch loop

def test_watch_loop_flushes_ready_windows(env, tmp_path, fake_llm, monkeypatch):
    cfg, store = env
    make_session_log(tmp_path, "s1", base_events())
    journal(cfg, store, n_steps=2)
    # bound the loop: stop after the first successful flush
    orig = sm.flush_window
    state = {"ran": 0}

    def once(*a, **k):
        state["ran"] += 1
        r = orig(*a, **k)
        if state["ran"] >= 1:
            stop_flag["stop"] = True
        return r

    stop_flag = {"stop": False}
    monkeypatch.setattr(sm, "flush_window", once)
    # the flush loop reads time.monotonic() for its wall budget: the stub
    # must carry it (a bare `sleep` stub makes every flush raise and the
    # no-op sleep turns the retry into a busy loop)
    monkeypatch.setattr(sm, "time", type("T", (), {
        "sleep": lambda s, x: None,
        "monotonic": lambda s: 0.0,
    })())
    sm.watch_loop(cfg, store, poll=0.1, stop=stop_flag)
    assert state["ran"] >= 1
    assert store.list_summaries(session="s1")


# ------------------------------------------------------------ record refs
# Summaries attach the records a session wrote (refs by prefix: sum_* are
# child summaries, fact_*/dec_* are records) so records accumulate up the
# tree and drill-down dispatches on the prefix.

def _memor_write_result(result_json: str) -> dict:
    return {"type": "tool-result", "toolCallId": "m1",
            "content": [{"type": "text", "text": result_json}],
            "isError": False}


def _memor_write_call(seq: int, t: int) -> dict:
    return {"type": "tool/call", "seq": seq, "time": t, "data": {
        "callId": "m1", "name": "mcp__memor__memory_add_fact",
        "arguments": "{}"}}


def _insert_fact(store, subject="port:9000", claim="port 9000 was freed"):
    return store.insert_fact(scope_project="memor", scope_component=None,
                             scope_machine=None, subject=subject,
                             claim=claim, kind="state", confidence="confirmed",
                             source_type="agent")


def _insert_decision(store, subject="backend", choice="use vLLM"):
    return store.insert_decision(scope_project="memor", scope_component=None,
                                 scope_machine=None, subject=subject,
                                 choice=choice, reason="faster")


def test_extract_inserted_parsing(env):
    cfg, store = env
    ok = sm._extract_inserted(json.dumps(
        {"inserted": ["fact_ab12cd34", "not-an-id"], "queued": ["f_x"]}))
    assert ok == ["fact_ab12cd34"]
    ragged = sm._extract_inserted(
        'no json here "fact_12345678" tail "dec_aabbccdd"')
    assert ragged == ["fact_12345678", "dec_aabbccdd"]
    assert sm._extract_inserted("{}") == []
    assert sm._extract_inserted("no ids here") == []


def test_flush_attaches_record_refs_from_span(env, tmp_path, fake_llm):
    cfg, store = env
    fid = _insert_fact(store)
    evs = base_events()[:6]
    evs.append(_memor_write_call(6, 9000))
    evs.append({"type": "tool/result", "seq": 7, "time": 9100, "data": {
        "message": {"source": {"kind": "tool", "callId": "m1"},
                    "content": [_memor_write_result(json.dumps(
                        {"inserted": [fid], "queued": []}))]}}})
    make_session_log(tmp_path, "s1", evs)
    journal(cfg, store, n_steps=2)
    r = sm.flush_window(cfg, store, "dsh", "s1")
    s = store.get_summary(r["summary_id"])
    assert s["references"] == [fid]
    assert "## Records" not in s["text"]  # refs live in the column, not text


def test_flush_record_fallback_timestamp(env, tmp_path, fake_llm):
    """When the tool result carries no parsable ids (e.g. queued for
    review), the window's records come from a created_at range query."""
    import time as _t
    cfg, store = env
    now_ms = int(_t.time() * 1000)
    evs = [{"type": "session", "version": 2, "id": "s1", "cwd": "/proj",
            "createdAt": 1},
           {"type": "user/message", "seq": 1, "time": now_ms - 6000,
            "data": {"content": [{"type": "text", "text": "check the port"}],
                     "source": {"kind": "user"}}},
           _memor_write_call(2, now_ms - 5000),
           {"type": "tool/result", "seq": 3, "time": now_ms - 4500,
            "data": {"message": {"content": [
                _memor_write_result(json.dumps(
                    {"inserted": [], "queued": ["review:port:9000"]}))]}}},
           {"type": "turn/end", "seq": 4, "time": now_ms + 10_000}]
    make_session_log(tmp_path, "s1", evs)
    fid = _insert_fact(store, subject="port:9001", claim="bound by new-app")
    journal(cfg, store, n_steps=2)
    r = sm.flush_window(cfg, store, "dsh", "s1")
    s = store.get_summary(r["summary_id"])
    assert s["references"] == [fid]
    assert "## Records" not in s["text"] and "bound by new-app" not in s["text"]


def test_fold_propagates_record_refs(env, fake_llm):
    cfg, store = env
    fid = _insert_fact(store)
    did = _insert_decision(store)
    a = store.insert_summary(session="s1", project="p", level=1,
                             span="dsh:s1:seq[0..5]", text="win A",
                             references=[fid])
    b = store.insert_summary(session="s1", project="p", level=1,
                             span="dsh:s1:seq[6..9]", text="win B",
                             references=[did])
    made = sm.fold_chains(cfg, store, "p")
    parent = store.get_summary(made[0])
    assert set(parent["references"]) == {a, b, fid, did}
    # record refs ride the references column up the tree; the parent's
    # text is plain summary prose with no Records section
    assert "## Records" not in parent["text"]
    assert "use vLLM" not in parent["text"]


def test_temporal_rollup_propagates_records(env, fake_llm):
    cfg, store = env
    fid = _insert_fact(store, subject="service:memor", claim="running")
    store.insert_summary(machine=MACH, session="s1", project="p", level=1,
                         span="dsh:s1:seq[0..5]", text="day work",
                         references=[fid], ts="2026-09-10T10:00:00Z")
    day = sm.temporal_rollup(cfg, store, MACH, "day", date(2026, 9, 10))
    drow = store.get_summary(day["id"])
    assert fid in drow["references"]
    week = sm.temporal_rollup(cfg, store, MACH, "week", date(2026, 9, 10))
    wrow = store.get_summary(week["id"])
    # record refs climb the temporal axis via the references column
    assert fid in wrow["references"]
    assert fid not in wrow["text"]


def test_drill_down_dispatches_records(env, monkeypatch):
    cfg, store = env
    import memor.mcp_server as m
    monkeypatch.setattr(m, "_cfg", cfg)
    fid = _insert_fact(store)
    did = _insert_decision(store)
    child = store.insert_summary(session="s1", project="p", level=1,
                                 span="dsh:s1:seq[0..5]", text="leaf")
    node = store.insert_summary(session="s1", project="p", level=2,
                                 references=[child, fid, did], text="fold")
    out = json.loads(m.tool_drill_down(node))
    assert [c["id"] for c in out["children"]] == [child]
    assert {r["id"] for r in out["records"]} == {fid, did}
    assert out["records"][0]["subject"] or out["records"][0]["subject"]
    # a leaf can carry both edges at once: records AND its raw span
    leaf = store.insert_summary(session="s2", project="p", level=1,
                                span="dsh:s2:seq[0..1]", text="leaf2",
                                references=[fid])
    out2 = json.loads(m.tool_drill_down(leaf))
    assert [r["id"] for r in out2["records"]] == [fid]
    assert "raw_events" in out2 or "raw_events_error" in out2


def test_old_store_migrates_workspace_column(tmp_path):
    """A store created before the workspace column (old summaries DDL)
    must open cleanly: executescript must not index the column before
    _migrate adds it."""
    c = tmp_path.joinpath("memor.db")
    import sqlite3
    con = sqlite3.connect(c)
    con.executescript("""
    CREATE TABLE summaries (
      id TEXT PRIMARY KEY, ts TEXT NOT NULL, machine TEXT, session TEXT,
      harness TEXT, axis TEXT NOT NULL DEFAULT 'project', project TEXT,
      component TEXT, level INTEGER NOT NULL DEFAULT 1, parent_id TEXT,
      period TEXT, span TEXT, refs TEXT, text TEXT NOT NULL, subjects TEXT,
      model TEXT, tokens_in INTEGER, tokens_out INTEGER,
      status TEXT NOT NULL DEFAULT 'current', created_at TEXT);
    """)
    con.commit()
    con.close()
    store = Store(tmp_path)
    cols = [r[1] for r in store._conn().execute("PRAGMA table_info(summaries)")]
    assert "workspace" in cols
    names = [dict(r)["name"] for r in
             store._conn().execute("PRAGMA index_list(summaries)")]
    assert "ix_summaries_workspace" in names
    # and it is usable
    store.insert_summary(ts="2026-01-01T00:00:00Z", workspace="/w", level=1,
                         span="dsh:s1:seq[0..1]", text="x")
    assert store.list_summaries(workspace="/w")


# ------------------------------- workspace axis, depth width, backfill

def test_workspace_fold_unifies_sessions(env, fake_llm):
    """One workspace's sessions fold together; workspaces never mix."""
    cfg, store = env
    a = store.insert_summary(session="s1", workspace="/proj", level=1,
                             span="dsh:s1:seq[0..5]", text="win A")
    b = store.insert_summary(session="s2", workspace="/proj", level=1,
                             span="dsh:s2:seq[0..5]", text="win B")
    c = store.insert_summary(session="s3", workspace="/other", level=1,
                             span="dsh:s3:seq[0..5]", text="win C")
    made = sm.fold_chains(cfg, store, workspace="/proj")
    assert len(made) == 1
    p = store.get_summary(made[0])
    # workspace-level fold: covers the whole workspace, no single session
    assert p["level"] == 2 and p["workspace"] == "/proj" and p["session"] is None
    assert set(p["references"]) == {a, b}
    # the other workspace's node was untouched (one unparented < width 2)
    assert store.get_summary(c)["parent_id"] is None
    assert sm.fold_chains(cfg, store, workspace="/other") == []


def test_depth_based_window_width(env):
    cfg, _ = env
    cfg.session_subagent_window_steps = [4]
    assert sm.window_steps_for(cfg, {}) == 2          # depth 0 -> default
    assert sm.window_steps_for(cfg, {"depth": 1}) == 4
    assert sm.window_steps_for(cfg, {"depth": 3}) == 4  # last entry caps
    cfg.session_subagent_window_steps = [4, 6]
    assert sm.window_steps_for(cfg, {"depth": 2}) == 6
    cfg.session_subagent_window_steps = []
    assert sm.window_steps_for(cfg, {"depth": 5}) == 2  # empty -> uniform


def test_session_event_reads_subagent_meta(env, tmp_path, fake_llm):
    """Subagent origin/depth come from the session record (first log line),
    read at hook time; depth sets the window width."""
    cfg, store = env
    evs = [dict(base_events()[0], id="sub1", origin="subagent",
                parentSession="main1", delegationDepth=2)]
    make_session_log(tmp_path, "sub1", evs)
    sm.session_event(cfg, store, "dsh", "start",
                     {"session_id": "sub1", "cwd": "/proj"})
    st = sm._sessions(store)["sub1"]
    assert st["depth"] == 2 and st["origin"] == "subagent"
    assert st["parent_session"] == "main1" and st["workspace"] == "/proj"
    # [12, 24] -> depth 2 gets the second entry
    assert sm.window_steps_for(cfg, st) == 24


def test_window_steps_for_minus_one(env):
    """-1 disables windowing at its depth and every deeper depth (ordered:
    a level only logs when every level above it logs); depth 0 is always
    on. Entries after the first -1 are ignored."""
    cfg, _ = env
    cfg.session_subagent_window_steps = [4, -1]
    assert sm.window_steps_for(cfg, {}) == 2            # depth 0 -> default
    assert sm.window_steps_for(cfg, {"depth": 1}) == 4
    assert sm.window_steps_for(cfg, {"depth": 2}) is None   # the -1 hits
    assert sm.window_steps_for(cfg, {"depth": 5}) is None   # capped deeper
    cfg.session_subagent_window_steps = [-1]
    assert sm.window_steps_for(cfg, {"depth": 1}) is None
    assert sm.window_steps_for(cfg, {"depth": 3}) is None
    cfg.session_subagent_window_steps = [4, 6, -1]
    assert sm.window_steps_for(cfg, {"depth": 2}) == 6
    assert sm.window_steps_for(cfg, {"depth": 3}) is None


def _event_rows(store, sid):
    cur = store._conn().execute(
        "SELECT COUNT(*) FROM raw_events WHERE session=?", (sid,))
    return cur.fetchone()[0]


def test_session_event_depth_exempt(env, tmp_path, fake_llm):
    """A session at a -1 depth journals nothing and keeps no state — the
    parent that called it summarises it as part of its own tool calls."""
    cfg, store = env
    cfg.session_subagent_window_steps = [4, -1]
    evs = [dict(base_events()[0], id="deep1", origin="subagent",
                parentSession="main1", delegationDepth=2)]
    make_session_log(tmp_path, "deep1", evs)
    r = sm.session_event(cfg, store, "dsh", "start",
                         {"session_id": "deep1", "cwd": "/proj"})
    assert r["ok"] and r["reason"] == "depth-exempt" and r["depth"] == 2
    assert sm._sessions(store).get("deep1") is None
    assert _event_rows(store, "deep1") == 0
    # follow-up events are exempt too (no state to carry between hits)
    r = sm.session_event(cfg, store, "dsh", "tool",
                         {"session_id": "deep1", "cwd": "/proj",
                          "tool_name": "bash", "tool_input": {"command": "x"}})
    assert r["reason"] == "depth-exempt"
    assert sm._sessions(store).get("deep1") is None
    assert _event_rows(store, "deep1") == 0
    # a depth-1 subagent under the same config still journals normally
    evs1 = [dict(base_events()[0], id="mid1", origin="subagent",
                 parentSession="main1", delegationDepth=1)]
    make_session_log(tmp_path, "mid1", evs1)
    sm.session_event(cfg, store, "dsh", "start",
                     {"session_id": "mid1", "cwd": "/proj"})
    assert sm._sessions(store).get("mid1", {}).get("depth") == 1
    assert _event_rows(store, "mid1") == 1


def test_session_event_depth_exempt_config_change(env, tmp_path, fake_llm):
    """A -1 arriving on a session that was tracked: no new rows, state
    unwound (the D tail stops showing a phantom 'digesting' worker); the
    journaled rows stay (their summaries' spans must keep resolving)."""
    cfg, store = env
    cfg.session_subagent_window_steps = [4, 6]
    evs = [dict(base_events()[0], id="deep2", origin="subagent",
                parentSession="main1", delegationDepth=2)]
    make_session_log(tmp_path, "deep2", evs)
    sm.session_event(cfg, store, "dsh", "start",
                      {"session_id": "deep2", "cwd": "/proj"})
    assert _event_rows(store, "deep2") == 1
    cfg.session_subagent_window_steps = [4, -1]
    r = sm.session_event(cfg, store, "dsh", "tool",
                         {"session_id": "deep2", "cwd": "/proj",
                          "tool_name": "bash", "tool_input": {}})
    assert r["reason"] == "depth-exempt"
    assert sm._sessions(store).get("deep2") is None
    assert _event_rows(store, "deep2") == 1


def test_flush_window_depth_exempt(env, fake_llm):
    """A worker landing on a gated session (stale state row / config
    change) cleans its state up and summarises nothing."""
    cfg, store = env
    cfg.session_subagent_window_steps = [4, -1]
    sessions = sm._sessions(store)
    sessions["deep3"] = {
        "harness": "dsh", "machine": MACH, "project": None, "cwd": "/proj",
        "started_at": "2026-01-01T00:00:00Z", "workspace": "/proj",
        "prompts": 1, "steps": 5, "events_since_last": 5,
        "last_span_hi": None, "last_summary_id": None, "depth": 2,
        "origin": "subagent", "parent_session": "main1", "retries": 0,
        "stopped": False, "flushing": None,
        "last_event_at": "2026-01-01T00:01:00Z"}
    sm._save_sessions(store, sessions)
    r = sm.flush_window(cfg, store, "dsh", "deep3", force=True)
    assert r["ok"] and r["reason"] == "depth-exempt"
    assert sm._sessions(store).get("deep3") is None


def test_backfill_depth_exempt(env, tmp_path, fake_llm):
    """Backfill refuses (and cleans state) for a gated depth: the parent's
    summary covers the session."""
    cfg, store = env
    cfg.session_subagent_window_steps = [4, -1]
    evs = [dict(backfill_events("deep4")[0], id="deep4", origin="subagent",
                parentSession="main1", delegationDepth=2)
           ] + backfill_events("deep4")[1:]
    make_session_log(tmp_path, "deep4", evs)
    plan = sm.backfill_session(cfg, store, "deep4", dry_run=True)
    assert plan["ok"] and plan["reason"] == "depth-exempt" \
        and plan["depth"] == 2
    assert sm._sessions(store).get("deep4") is None


def backfill_events(session_id="b1") -> list[dict]:
    """Record + 2 prompts + 6 tool calls. At width 2: window [0..2] and
    [3..5] carry a prompt (summarized); [6..7] is noise (pointer advances,
    no node). Boundaries = seq of the 2nd/4th/6th tool call: 2, 5, 7."""
    evs = [dict(base_events()[0], id=session_id)]
    def prompt(seq):
        return {"type": "user/message", "seq": seq, "time": 9000 + seq,
                "data": {"content": [{"type": "text",
                                       "text": f"step {seq}"}],
                         "source": {"kind": "user"}, "role": "user"}}
    def tool(seq):
        return {"type": "tool/call", "seq": seq, "time": 9000 + seq,
                "data": {"callId": f"c{seq}", "name": "bash",
                          "arguments": json.dumps({"command": "work"})}}
    evs.append(prompt(0))
    evs += [tool(1), tool(2), prompt(3), tool(4), tool(5), tool(6), tool(7)]
    return evs


def test_backfill_multi_window_resume(env, tmp_path, fake_llm):
    cfg, store = env
    make_session_log(tmp_path, "b1", backfill_events("b1"))
    # dry-run plan: deterministic, no writes
    plan = sm.backfill_session(cfg, store, "b1", dry_run=True)
    assert plan["ok"] and plan["pending_tool_calls"] == 6
    assert plan["windows_planned"] == 3 and plan["window_steps"] == 2
    assert plan["workspace"] == "/proj" and plan["depth"] == 0
    assert store.list_summaries() == []
    # first two windows only (resumable)
    r1 = sm.backfill_session(cfg, store, "b1", max_windows=2)
    assert r1["ok"] and r1["processed"] == 2
    l1 = store.list_summaries(session="b1", level=1)
    assert [x["span"] for x in l1] == ["dsh:b1:seq[0..2]", "dsh:b1:seq[3..5]"]
    # width 2 + 2 L1s -> one workspace-level fold parent
    l2 = [x for x in store.list_summaries(level=2)]
    assert len(l2) == 1 and l2[0]["workspace"] == "/proj" \
        and l2[0]["session"] is None
    assert {x["id"] for x in l1} <= set(l2[0]["references"])
    # resume: the noise window [6..7] advances the pointer, adds no node
    r2 = sm.backfill_session(cfg, store, "b1")
    assert r2["ok"] and r2["processed"] == 1
    assert r2["reports"][0]["reason"] == "noise-skipped"
    assert len(store.list_summaries(session="b1", level=1)) == 2
    # and it is up to date now
    r3 = sm.backfill_session(cfg, store, "b1")
    assert r3["ok"] and r3["reason"] == "up-to-date" and r3["processed"] == 0


def test_backfill_reprocess_cascades(env, tmp_path, fake_llm):
    cfg, store = env
    make_session_log(tmp_path, "b2", backfill_events("b2"))
    sm.backfill_session(cfg, store, "b2")
    l1_ids = [x["id"] for x in store.list_summaries(session="b2", level=1)]
    assert len(l1_ids) == 2
    # the fold parent references both L1s
    assert store.summaries_referencing(l1_ids)
    r = sm.backfill_session(cfg, store, "b2", reprocess=True)
    assert r["ok"] and r["reprocess_deleted"] >= 3
    # everything that referenced the old series is gone (lazily rebuildable)
    assert store.summaries_referencing(l1_ids) == []
    # rebuilt under the current params: same spans, fresh ids
    spans = [x["span"] for x in store.list_summaries(session="b2", level=1)]
    assert spans == ["dsh:b2:seq[0..2]", "dsh:b2:seq[3..5]"]
    assert not set(l1_ids) & {x["id"] for x in
                              store.list_summaries(session="b2", level=1)}


def test_lock_age_aware_comparison():
    """_lock_age must parse now_utc() timestamps: a fresh lock is young,
    not '1e9 seconds old' (naive utcnow() - aware ts raised TypeError and
    made every lock look stale, disabling the TTL guards)."""
    assert sm._lock_age(sm.now_utc()) < 5
    assert sm._lock_age("2020-01-01T00:00:00Z") > 365 * 24 * 3600
    assert sm._lock_age(None) > 1e8


def test_backfill_bails_on_live_flush_lock(env, tmp_path, fake_llm):
    """A mid-flight live flush must not race the backfill: it bails
    instead; a stale lock (dead worker) expires via TTL and does not."""
    cfg, store = env
    make_session_log(tmp_path, "b4", backfill_events("b4"))
    sessions = sm._sessions(store)
    sessions["b4"] = {"harness": "dsh", "machine": "test",
                      "project": None, "cwd": "/proj",
                      "started_at": sm.now_utc(), "workspace": "/proj",
                      "prompts": 0, "steps": 0, "events_since_last": 0,
                      "last_span_hi": 2, "last_summary_id": None,
                      "depth": 0, "origin": None, "parent_session": None,
                      "retries": 0, "stopped": False,
                      "flushing": sm.now_utc(), "last_event_at": sm.now_utc()}
    sm._save_sessions(store, sessions)
    r = sm.backfill_session(cfg, store, "b4")
    assert not r["ok"] and r["reason"] == "flush-in-progress"
    assert store.list_summaries(session="b4") == []
    # stale lock: a crashed worker's lock expires, backfill proceeds
    sessions = sm._sessions(store)
    sessions["b4"]["flushing"] = "2020-01-01T00:00:00Z"
    sm._save_sessions(store, sessions)
    r = sm.backfill_session(cfg, store, "b4")
    # resumes from last_span_hi=2: window [3..5] summarized, [6..7] noise
    assert r["ok"] and r["processed"] == 2
    assert [x["span"] for x in store.list_summaries(session="b4", level=1)] \
        == ["dsh:b4:seq[3..5]"]


def test_cli_sessions_and_backfill(env, tmp_path, fake_llm, capsys):
    cfg, store = env
    make_session_log(tmp_path, "b3", backfill_events("b3"))
    rc = _main(["sessions"])
    out = capsys.readouterr().out
    assert rc == 0 and "b3" in out and "unprocessed" in out
    rc = _main(["backfill", "b3", "--dry-run"])
    plan = json.loads(capsys.readouterr().out)
    assert rc == 0 and plan["ok"] and plan["windows_planned"] == 3
    assert store.list_summaries() == []
    rc = _main(["backfill", "b3"])
    rep = json.loads(capsys.readouterr().out)
    assert rc == 0 and rep["processed"] == 3
    assert len(store.list_summaries(session="b3", level=1)) == 2
    rc = _main(["sessions"])
    out = capsys.readouterr().out
    assert rc == 0 and "b3" in out and "current" in out


# ------------------------------------------------------------ llm audit


def test_llm_audit_records_window_and_chain(env, tmp_path, fake_llm):
    cfg, store = env
    make_session_log(tmp_path, "b1", backfill_events("b1"))
    sm.backfill_session(cfg, store, "b1")
    calls = store.list_llm_calls(session="b1", limit=50)
    win = [c for c in calls if c["mode"] == "window"]
    assert len(win) == 2
    assert {c["span"] for c in win} == {"dsh:b1:seq[0..2]", "dsh:b1:seq[3..5]"}
    assert all(c["ok"] for c in win)
    p = json.loads(win[0]["prompt"])
    assert p["mode"] == "window" and "digest" in p and "USER" in p["digest"]
    assert "summary-" in win[0]["raw_response"]
    # the fold is a chain call, not tied to one session/span
    chain = [c for c in store.list_llm_calls(limit=50)
             if c["mode"] == "chain"]
    assert len(chain) == 1 and chain[0]["session"] is None \
        and chain[0]["span"] is None and chain[0]["ok"]


def test_llm_audit_records_failed_exchange(env, tmp_path, monkeypatch):
    cfg, store = env
    make_session_log(tmp_path, "s1", base_events())
    journal(cfg, store, n_steps=2)

    class BoomLLM:
        def available(self):
            return True

        def _call(self, user, system=None):
            raise RuntimeError("model died")

    monkeypatch.setattr(sm, "_extractor", lambda cfg: BoomLLM())
    r = sm.flush_window(cfg, store, "dsh", "s1")
    assert not r["ok"]
    calls = store.list_llm_calls(session="s1")
    assert len(calls) == 1 and calls[0]["ok"] == 0
    assert "model died" in calls[0]["raw_response"]


def test_llm_audit_absent_without_exchange(env, tmp_path, monkeypatch):
    """No model, no exchange, no audit row — the pending state is visible
    through retries instead."""
    cfg, store = env
    make_session_log(tmp_path, "s1", base_events())
    journal(cfg, store, n_steps=2)
    monkeypatch.setattr(sm, "_extractor", lambda cfg: None)
    r = sm.flush_window(cfg, store, "dsh", "s1")
    assert not r["ok"] and r["reason"] == "llm-unavailable"
    assert store.list_llm_calls(session="s1") == []


def test_cli_window_review(env, tmp_path, fake_llm, capsys):
    cfg, store = env
    make_session_log(tmp_path, "s1", base_events())
    journal(cfg, store, n_steps=2)
    sm.flush_window(cfg, store, "dsh", "s1")
    rc = _main(["window-review", "s1"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "window" in out and "dsh:s1:seq[0..5]" in out
    assert "prompt (sent to the model)" in out
    assert "raw response" in out and "summary-1" in out
    assert "stored summary" in out
    # a summary without an audit row falls back to a reconstructed digest
    store.insert_summary(session="s1", level=1, span="dsh:s1:seq[1..2]",
                         text="orphan window", subjects=[])
    capsys.readouterr()
    rc = _main(["window-review", "s1"])
    out = capsys.readouterr().out
    assert rc == 0 and "reconstructed" in out


# ------------------------------------------------- autonomous catch-up

def _gap_events(session_id: str, n_windows: int) -> list[dict]:
    """n_windows of [prompt, (call, result) x2] — at width 2 each window is
    5 events, boundary on the 2nd call's result. Unique seqs throughout."""
    evs = [dict(base_events()[0], id=session_id)]
    seq = 0
    for w in range(n_windows):
        evs.append({"type": "user/message", "seq": seq, "time": 5000 + seq,
                    "data": {"content": [{"type": "text", "text": f"gap {w}"}],
                             "source": {"kind": "user"}, "role": "user"}})
        seq += 1
        for _i in range(2):
            cid = f"c{seq}"  # the result must carry the CALL's id
            evs.append({"type": "tool/call", "seq": seq, "time": 6000 + seq,
                        "data": {"callId": cid, "name": "bash",
                                  "arguments": "{}"}})
            seq += 1
            evs.append({"type": "tool/result", "seq": seq, "time": 6200 + seq,
                        "data": {"message": {"source": {"kind": "tool",
                                                          "callId": cid},
                                             "content": [{"type": "tool-result",
                                                          "toolCallId": cid,
                                                          "content": [
                                                              {"type": "text",
                                                               "text": "ok"}]}]}}})
            seq += 1
    return evs


def test_spawn_worker_forces_and_logs(env, tmp_path, monkeypatch):
    """The spawned worker holds the flush lock its spawner reserved, so
    the entry must run with --force — a non-force entry bails on its own
    fresh lock ('already-flushing') and exits without releasing it (a
    stale lock that blocks every future spawn). Stderr goes to the
    per-day log, not /dev/null: dead workers must not be invisible."""
    popen_calls: list[tuple] = []

    class FakePopen:
        def __init__(self, args, **kw):
            popen_calls.append((args, kw))

    monkeypatch.setattr(sm.subprocess, "Popen", FakePopen)
    log = tmp_path / "logs" / "w.log"
    log.parent.mkdir(parents=True)
    sm._spawn_worker("dsh", "s1", log)
    args, kw = popen_calls[0]
    assert args[-3:] == ["--session", "s1", "--force"]
    assert kw["stderr"].name == str(log)
    # without a log path, stderr is devnull as before
    sm._spawn_worker("dsh", "s1")
    assert popen_calls[1][1]["stderr"].name == "/dev/null"


def test_spawn_guard_retries_stale_lock(env, monkeypatch):
    """A crashed worker's lock expires via TTL (same rule as flush_window's
    guard) — without it, one stale lock blocks every future spawn forever."""
    cfg, store = env
    cfg.session_execution_mode = "detached"
    calls: list[tuple] = []
    monkeypatch.setattr(sm, "_spawn_worker",
                        lambda h, s, *a: calls.append((h, s)))
    sm.session_event(cfg, store, "dsh", "start", {"session_id": "s1", "cwd": "/proj"})
    sm.session_event(cfg, store, "dsh", "tool", {"session_id": "s1", "tool_name": "bash"})
    sm.session_event(cfg, store, "dsh", "tool", {"session_id": "s1", "tool_name": "bash"})
    assert calls == [("dsh", "s1")]  # baseline: window closed at step 2
    calls.clear()
    sessions = sm._sessions(store)
    sessions["s1"]["flushing"] = "2020-01-01T00:00:00Z"  # long-expired dead lock
    sm._save_sessions(store, sessions)
    r = sm.session_event(cfg, store, "dsh", "tool",
                         {"session_id": "s1", "tool_name": "bash"})
    assert r["spawned"] and calls == [("dsh", "s1")]
    assert sm._sessions(store)["s1"]["flushing"] != "2020-01-01T00:00:00Z"
    # a fresh lock still blocks (no double worker mid-flight)
    sm.session_event(cfg, store, "dsh", "tool",
                     {"session_id": "s1", "tool_name": "bash"})
    assert len(calls) == 1


def test_worker_runs_despite_own_fresh_lock(env, tmp_path, fake_llm):
    """session_event reserves the lock, THEN spawns: the worker (force=True)
    must process through its own fresh lock, not bail with already-flushing
    and leak it (that was the dead-on-arrival bug)."""
    cfg, store = env
    make_session_log(tmp_path, "s1", base_events())
    journal(cfg, store, n_steps=2)
    sessions = sm._sessions(store)
    sessions["s1"]["flushing"] = sm.now_utc()  # the spawner's reservation
    sm._save_sessions(store, sessions)
    r = sm.flush_window(cfg, store, "dsh", "s1", force=True)  # the worker's call
    assert r["ok"] and r["span"] == "dsh:s1:seq[0..5]"
    assert sm._sessions(store)["s1"]["flushing"] is None
    # non-force still honors a fresh lock (protects against other actors)
    sessions = sm._sessions(store)
    sessions["s1"]["flushing"] = sm.now_utc()
    sm._save_sessions(store, sessions)
    r2 = sm.flush_window(cfg, store, "dsh", "s1")
    assert not r2["ok"] and r2["reason"] == "already-flushing"
    assert sm._sessions(store)["s1"]["flushing"] is not None


def test_catchup_tiles_gap_and_folds(env, tmp_path, fake_llm):
    """A pointer->tail gap is tiled into windows (the backfill boundary
    rule), not collapsed into one elided mega-digest; folds cascade as
    the windows land."""
    cfg, store = env  # window_steps=2, chain_width=2
    make_session_log(tmp_path, "g1", _gap_events("g1", 4))
    sm.session_event(cfg, store, "dsh", "start", {"session_id": "g1", "cwd": "/proj"})
    r = sm.flush_window(cfg, store, "dsh", "g1", force=True)
    assert r["ok"] and r["processed"] == 4
    assert [s["span"] for s in store.list_summaries(session="g1", level=1)] == \
        ["dsh:g1:seq[0..4]", "dsh:g1:seq[5..9]",
         "dsh:g1:seq[10..14]", "dsh:g1:seq[15..19]"]
    assert sm._sessions(store)["g1"]["last_span_hi"] == 19
    # 4 L1s at width 2 -> 2 L2s -> 1 L3 root
    assert len(store.list_summaries(level=2)) == 2
    assert len(store.list_summaries(level=3)) == 1
    assert fake_llm.n == 7  # 4 windows + 2 L2 folds + 1 L3 fold


def test_catchup_budget_successor(env, tmp_path, fake_llm, monkeypatch):
    """A wall-budget stop with the gap still pending spawns a successor
    whether the session is idle or active (the relay chases the tail;
    the successor re-reads the fresh log, so further growth merges into
    its run instead of needing another hook trigger)."""
    cfg, store = env
    cfg.session_catchup_budget_seconds = 5
    cfg.session_execution_mode = "detached"  # successors only chain in detached mode
    make_session_log(tmp_path, "g2", _gap_events("g2", 3))
    sm.session_event(cfg, store, "dsh", "start", {"session_id": "g2", "cwd": "/proj"})
    sm.session_event(cfg, store, "dsh", "stop", {"session_id": "g2"})  # idle
    spawned: list[tuple] = []
    monkeypatch.setattr(sm, "_spawn_worker",
                       lambda h, s, *a: spawned.append((h, s)))
    def make_clock():
        # two cheap reads (t0, first budget check), then a big jump so the
        # second check expires the budget
        st = {"n": 0}

        def mono():
            st["n"] += 1
            return 0.0 if st["n"] <= 2 else 100.0
        return mono

    monkeypatch.setattr(sm.time, "monotonic", make_clock())
    r = sm.flush_window(cfg, store, "dsh", "g2", force=True)
    assert r["ok"] and r["reason"] == "budget" and r["successor_due"]
    assert r["processed"] == 1 and r["pending_events"] == 10
    assert spawned == [("dsh", "g2")]  # the successor, once
    # the successor now holds the lock (a fresh stamp, not the run's release)
    assert sm._sessions(store)["g2"]["flushing"]

    # active session: budget stop chains a successor too — the relay
    # chases the tail, so "I stop working mid-worker" converges without
    # a further trigger
    monkeypatch.setattr(sm.time, "monotonic", make_clock())
    make_session_log(tmp_path, "g3", _gap_events("g3", 3))
    sm.session_event(cfg, store, "dsh", "start", {"session_id": "g3", "cwd": "/proj"})
    r2 = sm.flush_window(cfg, store, "dsh", "g3", force=True)
    assert r2["ok"] and r2["reason"] == "budget" and r2["successor_due"]
    assert r2["pending_events"] == 10
    assert spawned == [("dsh", "g2"), ("dsh", "g3")]
    assert sm._sessions(store)["g3"]["flushing"]  # fresh successor stamp


def test_successor_when_tail_grows_midrun(env, tmp_path, fake_llm,
                                          monkeypatch):
    """The log grows while a worker runs (a live session): the run drains
    its snapshot, the fresh exit read sees the new tail, and a successor
    is spawned to chase it — no further hook trigger needed. A run whose
    snapshot IS the tail (stable log) spawns none: the relay ends when
    the session goes quiet."""
    cfg, store = env
    cfg.session_execution_mode = "detached"
    snapshot = _gap_events("g9", 2)     # on disk: two windows, seqs 0..9
    grown = _gap_events("g9", 3)        # the third window "arrived mid-run"
    make_session_log(tmp_path, "g9", snapshot)
    sm.session_event(cfg, store, "dsh", "start",
                     {"session_id": "g9", "cwd": "/proj"})
    real = sm.events_in_range
    grown_last = max(e["seq"] for e in grown if "seq" in e)
    calls = {"n": 0}

    def fake(path, lo, hi):
        calls["n"] += 1
        if calls["n"] == 1:
            return real(path, lo, hi)          # entry: the snapshot
        return (grown, None, grown_last)       # exit: the grown tail

    monkeypatch.setattr(sm, "events_in_range", fake)
    spawned: list[tuple] = []
    monkeypatch.setattr(sm, "_spawn_worker",
                       lambda h, s, *a: spawned.append((h, s)))
    r = sm.flush_window(cfg, store, "dsh", "g9", force=True)
    assert r["ok"] and r.get("reason") is None  # drained: no reason
    assert r["processed"] == 2                    # both snapshot windows
    assert r["pending_events"] == 5               # the third window
    assert r["successor_due"]
    assert spawned == [("dsh", "g9")]
    assert sm._sessions(store)["g9"]["flushing"]  # fresh successor stamp

    # stable tail: nothing new since the snapshot -> nothing to chase
    monkeypatch.setattr(sm, "events_in_range", real)
    make_session_log(tmp_path, "g9b", _gap_events("g9b", 2))
    sm.session_event(cfg, store, "dsh", "start",
                     {"session_id": "g9b", "cwd": "/proj"})
    r2 = sm.flush_window(cfg, store, "dsh", "g9b", force=True)
    assert r2["ok"] and r2.get("reason") is None
    assert r2["processed"] == 2
    assert not r2.get("successor_due")
    assert len(spawned) == 1
    assert sm._sessions(store)["g9b"]["flushing"] is None


def test_no_successor_on_max_windows(env, tmp_path, fake_llm, monkeypatch):
    """An explicit max-windows cap (an out-of-band backfill) stops without
    chaining: a capped run must not spawn an uncapped successor."""
    cfg, store = env
    cfg.session_execution_mode = "detached"
    make_session_log(tmp_path, "g7", _gap_events("g7", 3))
    sm.session_event(cfg, store, "dsh", "start",
                     {"session_id": "g7", "cwd": "/proj"})
    spawned: list[tuple] = []
    monkeypatch.setattr(sm, "_spawn_worker",
                       lambda h, s, *a: spawned.append((h, s)))
    r = sm.flush_window(cfg, store, "dsh", "g7", force=True,
                        max_windows=1, budget=float("inf"))
    assert r["ok"] and r["reason"] == "max-windows"
    assert r["processed"] == 1
    assert not r.get("successor_due")
    assert spawned == []
    assert sm._sessions(store)["g7"]["flushing"] is None


def test_catchup_llm_failure_stops_chain(env, tmp_path, monkeypatch):
    """A retryable LLM failure stops the catch-up chain (no stub-spam, no
    successor); the next trigger resumes from the durable pointer."""
    cfg, store = env
    make_session_log(tmp_path, "g4", _gap_events("g4", 2))
    sm.session_event(cfg, store, "dsh", "start", {"session_id": "g4", "cwd": "/proj"})
    sm.session_event(cfg, store, "dsh", "stop", {"session_id": "g4"})
    spawned: list[tuple] = []
    monkeypatch.setattr(sm, "_spawn_worker",
                       lambda h, s, *a: spawned.append((h, s)))
    calls = {"n": 0}

    class FailOnSecond:
        def available(self):
            return True

        def _call(self, user, system=None):
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("model died")
            return json.dumps({"summary": "ok", "subjects": [],
                               "decisions": [], "problems": [], "open": []})

    monkeypatch.setattr(sm, "_extractor", lambda cfg: FailOnSecond())
    r = sm.flush_window(cfg, store, "dsh", "g4", force=True)
    assert not r["ok"] and r["reason"] == "llm-unavailable" and r["retry"] == 1
    # 2 attempts: window 1 landed, window 2 failed (attempts parity with backfill)
    assert r["processed"] == 2
    assert len(store.list_summaries(session="g4", level=1)) == 1
    assert sm._sessions(store)["g4"]["last_span_hi"] == 4
    assert spawned == []  # a dead LLM must not chain successors
    # the next trigger resumes from the durable pointer
    r2 = sm.flush_window(cfg, store, "dsh", "g4", force=True)
    assert r2["ok"] and r2["processed"] == 1
    assert [s["span"] for s in store.list_summaries(session="g4", level=1)] == \
        ["dsh:g4:seq[0..4]", "dsh:g4:seq[5..9]"]


# --------------------------------------------- structured summary storage

def test_summary_structured_roundtrip(env):
    """insert_summary carries decisions/problems/open_items as their own
    columns; text stays plain prose."""
    cfg, store = env
    sid = store.insert_summary(
        session="s1", project="p", level=1, span="dsh:s1:seq[0..5]",
        text="plain prose",
        decisions=[{"subject": "a", "choice": "b", "reason": "c"}],
        problems=["p1"], open_items=["o1"])
    row = store.get_summary(sid)
    assert row["text"] == "plain prose"
    assert row["decisions"] == [{"subject": "a", "choice": "b",
                                  "reason": "c"}]
    assert row["problems"] == ["p1"]
    assert row["open_items"] == ["o1"]
    # defaults: empty lists, never None
    sid2 = store.insert_summary(session="s1", project="p", level=1,
                                span="dsh:s1:seq[6..9]", text="prose 2")
    row2 = store.get_summary(sid2)
    assert row2["decisions"] == [] and row2["problems"] == [] \
        and row2["open_items"] == []


def test_summary_structure_migration(env):
    """Legacy rows (attached sections in the text) are migrated into the
    structured columns at store open — text becomes plain prose, Records
    material is dropped (it already lives in refs); idempotent."""
    cfg, store = env
    sid = store.insert_summary(session="s1", project="p", level=1,
                               span="dsh:s1:seq[0..5]", text="legacy prose",
                               references=["fact_1234"])
    # simulate a legacy row: sections in the text (as the old writer made)
    store._conn().execute("UPDATE summaries SET text=? WHERE id=?", (
        "legacy prose\n\n## Decisions\n- subj — choice — reason\n"
        "## Problems\n- it broke\n## Open\n- next step\n"
        "## Records\n- fact_1234: port — bound [state]", sid))
    store._conn().commit()
    store2 = Store(cfg.data_dir)      # fresh open triggers the migration
    row = store2.get_summary(sid)
    assert row["text"] == "legacy prose"
    assert row["decisions"] == [{"subject": "subj", "choice": "choice",
                                  "reason": "reason"}]
    assert row["problems"] == ["it broke"]
    assert row["open_items"] == ["next step"]
    assert row["references"] == ["fact_1234"]   # refs column untouched
    # idempotent: a second open changes nothing
    row3 = Store(cfg.data_dir).get_summary(sid)
    assert row3["text"] == row["text"]
    assert row3["decisions"] == row["decisions"]
    # a row with an unknown section header is left untouched
    sid4 = store.insert_summary(session="s1", project="p", level=1,
                                 span="dsh:s1:seq[6..9]", text="keep me")
    store._conn().execute("UPDATE summaries SET text=? WHERE id=?", (
        "keep me\n\n## Mystery\n- odd line", sid4))
    store._conn().commit()
    Store(cfg.data_dir)
    assert store2.get_summary(sid4)["text"].endswith("- odd line")


# ------------------------------------------------- structured output prompt

def test_prompt_v4_contract():
    """The prompt asks for one bare JSON object (no fences, no prose) and
    the payload carries the bumped version. v4: the summarizer may
    receive the workspace's on-record state registry (dedup)."""
    p = json.loads(sm._summarize_payload("window", {"digest": "d"}))
    assert p["prompt_version"] == "session-v4"
    assert "code fences" in sm.SUMMARIZE_SYSTEM
    assert "plain-English" in sm.SUMMARIZE_SYSTEM
    assert "workspace_state" in sm.SUMMARIZE_SYSTEM


def test_active_state_lines(env):
    cfg, store = env
    assert sm._active_state_lines(store, None) is None
    assert sm._active_state_lines(store, "/nowhere") is None
    store.workspace_state_upsert("/proj", "decision", "storage", "sqlite")
    store.workspace_state_upsert("/proj", "open_item", "ship it", "soon")
    lines = sm._active_state_lines(store, "/proj")
    assert "(decision) storage" in lines and "(open_item) ship it" in lines
    # a resolved row is not part of the registry
    row = store.workspace_state_active("/proj", "open_item", "ship it")
    store.workspace_state_set(row["id"], "resolved")
    assert "ship it" not in sm._active_state_lines(store, "/proj")


def test_window_payload_carries_state_registry(env, tmp_path, fake_llm):
    """The dedup fix: a window's summarizer payload includes the
    workspace's on-record state (the model reuses subjects for ongoing
    items), and omits the key when there is no state."""
    cfg, store = env
    ws = str(tmp_path / "ws")
    make_session_log(tmp_path, "s1", base_events())
    journal(cfg, store, n_steps=2, cwd=ws)
    llm = fake_llm
    # capture the raw payload
    orig = llm._call
    seen = {}
    def spy(user, system=None):
        seen["payload"] = json.loads(user)
        return orig(user, system=system)
    llm._call = spy
    # no state yet: key absent
    sm.flush_window(cfg, store, "dsh", "s1")
    assert "workspace_state" not in seen["payload"]
    # state on record (s1's own promotion counts too): registry in the
    # payload, carrying the new item
    store.workspace_state_upsert(ws, "decision", "storage", "sqlite")
    make_session_log(tmp_path, "s2", base_events())
    journal(cfg, store, session_id="s2", n_steps=2, cwd=ws)
    sm.flush_window(cfg, store, "dsh", "s2")
    reg = seen["payload"]["workspace_state"]
    assert "(decision) storage" in reg
    assert "(decision) s" in reg and "(open_item) finish tests" in reg


# ------------------------------------------------- flush idempotency

def test_flush_skips_window_already_stored(env, tmp_path, fake_llm):
    """Idempotency: a current L1 summary that already covers the window is
    NOT re-summarized — a duplicate trigger (stale pointer after a crashed
    flush, overlapping workers) adopts the stored row instead of paying a
    second LLM call."""
    cfg, store = env
    make_session_log(tmp_path, "s1", base_events())
    journal(cfg, store, n_steps=2)
    r1 = sm.flush_window(cfg, store, "dsh", "s1")
    assert r1["ok"] and r1["status"] == "current" and fake_llm.n == 1
    # simulate the crash-era state: the row exists but the pointer never
    # advanced — the next trigger recomputes the same window from scratch
    sessions = sm._sessions(store)
    sessions["s1"]["last_span_hi"] = None
    sm._save_sessions(store, sessions)
    r2 = sm.flush_window(cfg, store, "dsh", "s1", force=True)
    assert r2["ok"] and r2["reason"] == "already-flushed"
    assert fake_llm.n == 1                          # no second LLM call
    assert r2["summary_id"] == r1["summary_id"]     # adopted, not replaced
    assert sm._sessions(store)["s1"]["last_span_hi"] == 5  # pointer healed


def test_flush_reflushes_partial_or_short_window(env, tmp_path, fake_llm):
    """The guard only skips on a CURRENT row that covers the whole window:
    a partial (stub) row, or a row whose span ends before the window's
    tail, still gets re-summarized."""
    cfg, store = env
    make_session_log(tmp_path, "s1", base_events())
    journal(cfg, store, n_steps=2)
    store.insert_summary(session="s1", project="memor", level=1,
                         span="dsh:s1:seq[0..5]", text="[stub] LLM down",
                         subjects=[], status="partial")
    r = sm.flush_window(cfg, store, "dsh", "s1", force=True)
    assert r["ok"] and r.get("reason") != "already-flushed"
    assert fake_llm.n == 1 and r["status"] == "current"


# ------------------------------------------------- injection entry form

def test_entry_prose_drops_formatting():
    """Entries are clean paragraphs: markdown decoration gone, legacy
    attached sections cut, whitespace collapsed — nothing elided."""
    text = ("Worked on **the engine** and `store.py`.\n"
            "- fixed a bug\n- added tests\n"
            "## Decisions\n- x — y — z")
    out = sm._entry_prose(text)
    assert out == "Worked on the engine and store.py. fixed a bug added tests"
    for marker in ("**", "`", "##", "__"):
        assert marker not in out
    assert sm._entry_prose(None) == ""


def test_tree_span_label_fold_range():
    """Every entry is addressed by a seq range: its own window for L1s,
    the union of leaf windows for folds; a fold whose leaves span more
    than one session falls back to the time label."""
    byid = {
        "l1a": {"id": "l1a", "level": 1, "span": "dsh:s1:seq[10..19]"},
        "l1b": {"id": "l1b", "level": 1, "span": "dsh:s1:seq[20..29]"},
        "l2": {"id": "l2", "level": 2, "span": None,
               "created_at": "2026-01-01T08:30:00Z"},
    }
    byid["l2"]["references"] = ["l1a", "l1b"]
    refs = lambda s: s.get("references") or []
    assert sm._tree_span_label(byid["l1a"], byid, refs) == "10..19"
    assert sm._tree_span_label(byid["l2"], byid, refs) == "10..29"
    # leaves from a second session: seq ranges are per-session — time label
    byid["l1c"] = {"id": "l1c", "level": 1, "span": "dsh:s2:seq[5..9]"}
    byid["l2"]["references"].append("l1c")
    assert sm._tree_span_label(byid["l2"], byid, refs) == "L2 fold 08:30"


def test_injection_entries_enclosed(env):
    """Entries are properly enclosed: each is one paragraph followed by a
    blank line; between two entries (same section) exactly one blank line;
    fold entries carry a seq range like the L1s do."""
    cfg, store = env
    _build_tree(store, "/proj", "tt1", 6)
    doc = sm.session_tree_context(cfg, store, "/proj")
    lines = doc.split("\n")
    idx = [i for i, l in enumerate(lines) if l.startswith("[")]
    assert idx, "expected summary entries"
    for i in idx:
        assert lines[i + 1] == "", "entry must be followed by a blank line"
    for a, b in zip(idx, idx[1:]):
        between = lines[a + 1:b]
        if any(l.startswith("#") for l in between):
            continue  # a subsection heading separates the two
        assert between == [""], "exactly one blank line between entries"
    # fold entries (A/B/C upper levels) carry a seq range, not a time label
    assert not re.search(r"\[L\d+ fold \d\d:\d\d\]", doc)
    # the apex L2 entries: ranges spanning their 3 L1 windows each
    assert "[0..14]" in doc and "[15..29]" in doc
