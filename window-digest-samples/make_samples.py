"""Reproduce, exactly, what the summarizer would see for every window of one
DSH session.

Same code paths as live/backfill processing: events_in_range -> window
boundary (_next_window_boundary: the Nth tool/call) -> build_digest ->
_summarize_payload (the exact user-prompt JSON sent to the model). No LLM
involved; fully deterministic; re-runnable after any param change.

Usage:  make_samples.py [SESSION_ID] [OUT_DIR]
(default: this session, next to this script)

Output: one pretty-printed JSON per window — the exact payload — plus
stats.csv with per-window metrics for evaluation.
"""
import csv
import json
import sys
from pathlib import Path

from memor.config import Config
from memor.sessionlog import dsh_session_file, events_in_range, span_format, session_info
from memor import sessionmem as sm

DEFAULT_SID = "session-d109891a-48da-4eff-9d43-5809dbd81892"


def main() -> None:
    sid = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_SID
    out = Path(sys.argv[2]) if len(sys.argv) > 2 else Path(__file__).parent
    cfg = Config.load()
    p = dsh_session_file("dsh", sid, cfg.session_dsh_root)
    assert p, f"session log not found: {sid}"
    evs, first_seq, last_seq = events_in_range(p, 0, 10**9)
    rec = session_info(p) or {}
    # depth from the session record: width depends on delegation depth
    st = {"depth": rec.get("delegationDepth") or 0}
    n = sm.window_steps_for(cfg, st)
    cap = cfg.session_digest_max_chars
    project = sm.resolve_project(rec.get("cwd"), cfg) if rec.get("cwd") else None

    out.mkdir(parents=True, exist_ok=True)
    for f in out.glob("w*.json"):
        f.unlink()

    rows: list[dict] = []
    i = 0
    pointer = first_seq - 1
    pending = evs
    while pending:
        b = sm._next_window_boundary(cfg, pending, st)
        if b is None:
            break
        i += 1
        lo, hi = pointer + 1, b
        window = [e for e in evs if lo <= e.get("seq", 0) <= hi]
        digest, n_prompts, n_tools = sm.build_digest(cfg, window)
        # The exact user payload _summarize_recorded sends (mode window):
        payload = sm._summarize_payload("window", {
            "digest": digest,
            # live runs pass the previous window's summary text here;
            # for samples we show the shape with none
            "prev_text": None,
            "project": project,
            "session": sid,
            "span": span_format("dsh", sid, lo, hi),
        })
        (out / f"w{i:03d}_seq{lo:04d}-{hi:04d}.json").write_text(
            json.dumps(json.loads(payload), indent=2, ensure_ascii=False) + "\n")
        rows.append({
            "i": i, "lo": lo, "hi": hi,
            "prompts": n_prompts, "tools": n_tools,
            "events": len(window),
            "digest_chars": len(digest),
            "cap_hit": int(len(digest) > cap),
            "noise": int(n_prompts == 0 and n_tools < 3),
            "payload_chars": len(payload),
        })
        pointer = b
        pending = [e for e in pending if e.get("seq", 0) > b]

    with open(out / "stats.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)

    dig = sorted(r["digest_chars"] for r in rows)
    med = dig[len(dig) // 2]
    print(f"session: {sid}  cwd={rec.get('cwd')}  project={project}  "
          f"depth={st['depth']}")
    print(f"windows: {len(rows)}  (width {n} tools; log {first_seq}..{last_seq})")
    print(f"noise-skipped windows (0 prompts, <3 tools): "
          f"{sum(r['noise'] for r in rows)}")
    print(f"windows with >=1 user prompt: "
          f"{sum(1 for r in rows if r['prompts'])}")
    print(f"cap-hit windows (digest > {cap} chars): "
          f"{sum(r['cap_hit'] for r in rows)}")
    print(f"digest chars: min {dig[0]} / median {med} / "
          f"p90 {dig[int(len(dig) * 0.9)]} / max {dig[-1]}")
    print(f"payload chars: max {max(r['payload_chars'] for r in rows)}")


if __name__ == "__main__":
    main()
