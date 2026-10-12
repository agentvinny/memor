"""Per-budget elision cost for one session's full log, under the current
live digest budgets. Companion to make_samples.py: it answers "how much
real content do the thresholds lose in THIS session?"

Usage:  elision_stats.py SESSION_ID
"""
import re
import sys

from memor.config import Config
from memor.sessionlog import dsh_session_file, events_in_range
from memor import sessionmem as sm


def main() -> None:
    sid = sys.argv[1]
    cfg = Config.load()
    p = dsh_session_file("dsh", sid, cfg.session_dsh_root)
    assert p, f"session log not found: {sid}"
    evs, _, _ = events_in_range(p, 0, 10**9)
    n_el = {"USER": 0, "ASSISTANT": 0, "TOOL args": 0, "TOOL-RESULT": 0}
    chars_el = {"USER": 0, "ASSISTANT": 0, "TOOL args": 0, "TOOL-RESULT": 0}
    lens = {"USER": [], "ASSISTANT": [], "TOOL args": [], "TOOL-RESULT": []}
    for ev in evs:
        t = ev.get("type")
        d = ev.get("data") or {}
        if t == "user/message":
            if (d.get("source") or {}).get("kind") != "user":
                continue
            text = sm._text_blocks(d.get("content"))
            if not text:
                continue
            lens["USER"].append(len(text))
            el = len(text) - cfg.session_prompt_chars
            if el > 0:
                n_el["USER"] += 1
                chars_el["USER"] += el
        elif t == "assistant/message":
            text = sm._text_blocks((d.get("message") or {}).get("content"))
            if not text:
                continue
            lens["ASSISTANT"].append(len(text))
            sn = sm._snip(text, cfg.session_assistant_chars)
            if "elided" in sn:
                n_el["ASSISTANT"] += 1
                m = re.search(r"\[(\d+) chars elided\]", sn)
                chars_el["ASSISTANT"] += int(m.group(1)) if m else 0
        elif t == "tool/call":
            args = d.get("arguments")
            if not isinstance(args, str):
                continue
            lens["TOOL args"].append(len(args))
            sn = sm._snip(args, cfg.session_tool_arg_chars)
            if "elided" in sn:
                n_el["TOOL args"] += 1
                m = re.search(r"\[(\d+) chars elided\]", sn)
                chars_el["TOOL args"] += int(m.group(1)) if m else 0
        elif t == "tool/result":
            blocks = ((d.get("message") or {}).get("content")) or []
            text = ""
            for b in blocks:
                if isinstance(b, dict) and b.get("type") == "tool-result":
                    text = sm._text_blocks(b.get("content")) or text
            if not text:
                continue
            lens["TOOL-RESULT"].append(len(text))
            sn = sm._snip(text, cfg.session_tool_result_chars)
            if "elided" in sn:
                n_el["TOOL-RESULT"] += 1
                m = re.search(r"\[(\d+) chars elided\]", sn)
                chars_el["TOOL-RESULT"] += int(m.group(1)) if m else 0

    def pctl(xs, q):
        xs = sorted(xs)
        return xs[int(len(xs) * q)] if xs else 0

    print(f"session: {sid}")
    for k in ("USER", "ASSISTANT", "TOOL args", "TOOL-RESULT"):
        n = len(lens[k])
        print(f"{k:12s} events={n:4d}  elided={n_el[k]:4d} "
              f"({100 * n_el[k] // max(n, 1):3d}%)  lost={chars_el[k]:>9,}  "
              f"len p50={pctl(lens[k], .5)} p90={pctl(lens[k], .9)} "
              f"max={max(lens[k]) if lens[k] else 0}")


if __name__ == "__main__":
    main()
