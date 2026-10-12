"""Filesystem watcher: config/unit-file changes as evidence. Uses watchdog
(inotify) on the configured watch list. F10: full content/diff is only kept
for config-like paths (the only things the rules parse) — other paths store
hash/size/action only, so watching big trees can't bloat the store."""
from __future__ import annotations

import difflib
import hashlib
import os
import re
import time
from pathlib import Path

from ..config import Config
from ..consolidate.rules import is_config_path
from ..scope import machine_name
from ..store import Store

MAX_CONTENT = 32 * 1024
_DEBOUNCE: dict[str, float] = {}


def _is_text(path: Path) -> bool:
    try:
        with open(path, "rb") as f:
            chunk = f.read(8192)
        return b"\x00" not in chunk
    except OSError:
        return False


def _hash(p: Path) -> str:
    h = hashlib.sha256()
    try:
        with open(p, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
    except OSError:
        return ""
    return h.hexdigest()[:16]


def _read_content(p: Path) -> str | None:
    try:
        if p.stat().st_size > MAX_CONTENT:
            return None
        return p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def make_handler(cfg: Config, store: Store, stop: "callable"):
    """Build the watchdog handler (module-level so tests can drive it with
    synthetic events)."""
    try:
        from watchdog.events import FileSystemEventHandler
    except ImportError:
        return None

    class Handler(FileSystemEventHandler):
        def on_any_event(self, ev):  # type: ignore[override]
            if stop():
                return
            if ev.is_directory:
                return
            path = Path(ev.dest_path if ev.event_type == "moved" else ev.src_path)
            try:
                if not path.exists() and ev.event_type != "deleted":
                    return
            except OSError:
                return
            now = time.time()
            if now - _DEBOUNCE.get(str(path), 0) < cfg.fs_interval:
                return
            _DEBOUNCE[str(path)] = now
            action = ev.event_type  # created | modified | deleted | moved
            key = f"fs:{path}"
            old = store.snap_get(key)
            keep_content = is_config_path(str(path))
            content = _read_content(path) if (path.exists() and keep_content) else None
            payload = {
                "path": str(path), "action": action,
                "machine": machine_name(),
                "old_hash": old.get("hash") if old else None,
                "new_hash": _hash(path) if path.exists() else None,
                "size": path.stat().st_size if path.exists() else None,
            }
            if content is not None and action != "deleted":
                payload["content"] = content
            if (old and old.get("content") and content is not None
                    and action in ("modified", "moved")):
                diff = list(difflib.unified_diff(
                    old["content"].splitlines(keepends=True),
                    content.splitlines(keepends=True),
                    fromfile="a/" + path.name, tofile="b/" + path.name, n=1))
                if diff:
                    payload["diff"] = "".join(diff[:120])
                payload["old_content"] = old.get("content")
            store.snap_set(key, {"hash": payload.get("new_hash"),
                                 "content": content, "seen_at": str(now)})
            store.append_event(
                "filesystem", "file_change",
                machine=machine_name(),
                command=f"fs {action} {path}",
                payload=payload,
            )

    return Handler()


def watch_filesystem(cfg: Config, store: Store, stop: "callable") -> None:
    try:
        from watchdog.observers import Observer
    except ImportError:
        return

    Handler = make_handler(cfg, store, stop)
    if Handler is None:
        return

    paths = [os.path.expanduser(p) for p in cfg.watch_paths]
    paths = [p for p in paths if os.path.exists(p)]
    if not paths:
        return

    observer = Observer(timeout=1.0)
    observer.schedule(Handler(), *paths, recursive=True)
    observer.start()
    # emit a bootstrap event with the current watch list
    store.append_event(
        "filesystem", "watch_started", machine=machine_name(),
        command="memor fs watcher started",
        payload={"paths": paths, "machine": machine_name()},
    )
    while not stop():
        time.sleep(0.5)
    observer.stop()
    observer.join(timeout=5)