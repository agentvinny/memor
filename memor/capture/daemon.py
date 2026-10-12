"""Capture daemon: supervises all enabled sources in one process.

Run:  memor capture run [--sources net,fs,docker,journal]
Stop: memor capture stop        (PID file based; or systemd user service)
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

from ..config import Config
from ..store import Store

SOURCES = ("net", "fs", "docker_events", "docker_snapshots", "journal")


def _stop_flag() -> dict:
    return {"stop": False}


def _handle_stop(flag: dict) -> None:
    flag["stop"] = True


def _maybe_llm(cfg: Config):
    """LLM extractor for daemon consolidation, if configured (C3: any
    provider; a downed server just degrades this pass to rules-only)."""
    from ..consolidate.extract import extractor_for
    name, llm = extractor_for(cfg)
    return llm


def consolidate_once(cfg: Config) -> dict:
    """One scheduled consolidation pass (rules + LLM if available),
    followed by Markdown projection regeneration."""
    from ..consolidate.apply import run_consolidation
    store = Store(cfg.data_dir)
    rep = run_consolidation(store, cfg, extractor="auto", llm=_maybe_llm(cfg))
    if rep.get("events"):
        try:
            from ..consolidate.markdown import regenerate
            regenerate(store, cfg)
        except Exception:
            pass
    return rep


def run(cfg: Config, sources: list[str], foreground: bool = True,
        consolidate_every: float | None = None) -> None:
    flag = _stop_flag()
    every = cfg.consolidate_interval if consolidate_every is None else consolidate_every
    verify_every = cfg.verify_interval
    episode_gap = cfg.episode_idle_gap
    digest_every = cfg.digest_interval

    def stop() -> bool:
        return flag["stop"]

    def run_source(name: str) -> None:
        store = Store(cfg.data_dir)
        try:
            if name == "net":
                from .netproc import poll_net
                poll_net(store, stop, interval=cfg.net_interval)
            elif name == "fs":
                from .fs_watch import watch_filesystem
                watch_filesystem(cfg, store, stop)
            elif name == "docker_events":
                from .docker_events import tail_docker_events
                tail_docker_events(store, stop)
            elif name == "docker_snapshots":
                from .docker_events import poll_docker_snapshots
                poll_docker_snapshots(store, stop, interval=cfg.docker_snapshot_interval)
            elif name == "journal":
                from .journal import tail_journal
                # F9: backfill from the last daemon run (reboot/downtime gap)
                since = store.meta_get("journal.since")
                if not since:
                    row = store._conn().execute(
                        "SELECT MAX(ts) FROM raw_events"
                        " WHERE source='memor' AND kind='daemon_started'"
                    ).fetchone()
                    since = row[0] if row and row[0] else None
                tail_journal(store, stop, backfill_since=since)
        except Exception as e:  # keep other sources alive
            try:
                store.append_event("memor", "source_error",
                                   command=f"capture source {name} crashed",
                                   payload={"source": name, "error": str(e)[:500]})
            finally:
                pass

    # atuin ingestion runs periodically alongside the sources
    def ingest_loop() -> None:
        from .terminal import ingest_atuin
        while not stop():
            try:
                store = Store(cfg.data_dir)
                ingest_atuin(cfg, store)
            except Exception:
                pass
            t_end = time.time() + 30
            while time.time() < t_end and not stop():
                time.sleep(1)

    # scheduled consolidation keeps derived memory + Markdown fresh
    def consolidate_loop() -> None:
        while not stop():
            t_end = time.time() + every
            while time.time() < t_end and not stop():
                time.sleep(1)
            if stop():
                break
            try:
                rep = consolidate_once(cfg)
                if rep.get("events"):
                    try:
                        store = Store(cfg.data_dir)
                        store.append_event("memor", "consolidation_run",
                                           command="memor daemon consolidation pass",
                                           payload={"report": rep.get("report", "")[:500]})
                    except Exception:
                        pass
            except Exception as e:
                try:
                    store = Store(cfg.data_dir)
                    store.append_event("memor", "consolidation_error",
                                       command="memor daemon consolidation failed",
                                       payload={"error": str(e)[:500]})
                except Exception:
                    pass

    threads = [threading.Thread(target=run_source, args=(s,), daemon=True)
               for s in sources]
    threads.append(threading.Thread(target=ingest_loop, daemon=True))
    if every and every > 0:
        threads.append(threading.Thread(target=consolidate_loop, daemon=True))
    if episode_gap and episode_gap > 0:
        def episode_loop() -> None:
            from ..models import parse_ts
            from ..consolidate.episodes import close_episode
            while not stop():
                t_end = time.time() + 120
                while time.time() < t_end and not stop():
                    time.sleep(1)
                if stop():
                    break
                try:
                    estore = Store(cfg.data_dir)
                    cursor = estore.meta_get("episode.cursor") or "1970-01-01T00:00:00Z"
                    row = estore._conn().execute(
                        "SELECT MAX(ts) FROM raw_events WHERE source != 'memor'"
                        " AND ts >= ?", (cursor,)).fetchone()
                    last = row[0] if row and row[0] else None
                    if last and (time.time() - parse_ts(last).timestamp()) >= episode_gap:
                        close_episode(estore, cfg)
                except Exception:
                    pass

        threads.append(threading.Thread(target=episode_loop, daemon=True))
    if cfg.llm_autosync:
        # C3 R5: only when the flag is on — check the queue, then the LLM's
        # availability at the configured endpoint, then sync
        def llm_autosync_loop() -> None:
            from ..consolidate.apply import run_llm_sync
            from ..consolidate.extract import extractor_for
            while not stop():
                t_end = time.time() + cfg.llm_sync_interval
                while time.time() < t_end and not stop():
                    time.sleep(1)
                if stop():
                    break
                try:
                    name, llm = extractor_for(cfg)
                    if llm is None or not llm.available():
                        continue
                    store = Store(cfg.data_dir)
                    run_llm_sync(store, cfg)
                except Exception as e:
                    try:
                        store = Store(cfg.data_dir)
                        store.append_event("memor", "llm_sync_error",
                                           command="memor daemon llm sync failed",
                                           payload={"error": str(e)[:500]})
                    except Exception:
                        pass

        threads.append(threading.Thread(target=llm_autosync_loop, daemon=True))
    # verify_enabled gates the whole state-polling pass (A1). Default True
    # for existing stores; `memor switchover` (session-memory regime) turns
    # it off — re-enable in memor.json or re-run `memor switchover --verify`.
    verify_gated = getattr(cfg, "verify_enabled", True)
    if verify_every and verify_every > 0 and verify_gated:
        def verify_loop() -> None:
            from .verify import run_verification
            while not stop():
                t_end = time.time() + verify_every
                while time.time() < t_end and not stop():
                    time.sleep(1)
                if stop():
                    break
                try:
                    vstore = Store(cfg.data_dir)
                    run_verification(vstore, cfg)
                except Exception as e:
                    try:
                        vstore = Store(cfg.data_dir)
                        vstore.append_event("memor", "verify_error",
                                            command="memor daemon verification failed",
                                            payload={"error": str(e)[:500]})
                    except Exception:
                        pass

        threads.append(threading.Thread(target=verify_loop, daemon=True))
    if digest_every and digest_every > 0:
        def digest_loop() -> None:
            from ..consolidate.digest import build_digest
            while not stop():
                t_end = time.time() + digest_every
                while time.time() < t_end and not stop():
                    time.sleep(2)
                if stop():
                    break
                try:
                    build_digest(Store(cfg.data_dir), cfg)
                except Exception:
                    pass

        threads.append(threading.Thread(target=digest_loop, daemon=True))
    for t in threads:
        t.start()

    pidfile = cfg.data_dir / "capture.pid"
    pidfile.write_text(str(os.getpid()))
    store = Store(cfg.data_dir)
    store.append_event("memor", "daemon_started",
                       command="memor capture daemon started",
                       payload={"sources": sources, "pid": os.getpid(),
                                "consolidate_every": every})

    if foreground:
        try:
            while True:
                time.sleep(0.5)
                if flag["stop"]:
                    break
        except KeyboardInterrupt:
            pass
    else:
        # daemonized mode: block on signal
        signal.signal(signal.SIGTERM, lambda *_: _handle_stop(flag))
        signal.signal(signal.SIGINT, lambda *_: _handle_stop(flag))
        while not flag["stop"]:
            time.sleep(1)

    store.append_event("memor", "daemon_stopped",
                       command="memor capture daemon stopped",
                       payload={"pid": os.getpid()})
    # F9: next start backfills the journal from here
    try:
        from ..models import now_utc
        store.meta_set("journal.since", now_utc())
    except Exception:
        pass
    try:
        pidfile.unlink()
    except OSError:
        pass
    for t in threads:
        t.join(timeout=5)
    sys.stdout.flush()


def daemonize(cfg: Config, sources: list[str],
              consolidate_every: float | None = None) -> int:
    """Double-fork into background, log to <data>/capture.log."""
    pidfile = cfg.data_dir / "capture.pid"
    if pidfile.exists():
        try:
            pid = int(pidfile.read_text().strip())
            os.kill(pid, 0)
            print(f"already running (pid {pid})")
            return pid
        except (ValueError, ProcessLookupError, PermissionError):
            pidfile.unlink(missing_ok=True)
    if os.fork() > 0:
        os._exit(0)
    os.setsid()
    if os.fork() > 0:
        os._exit(0)
    log = open(cfg.data_dir / "capture.log", "ab")
    os.dup2(log.fileno(), 1)
    os.dup2(log.fileno(), 2)
    sys.stdin = open(os.devnull)
    run(cfg, sources, foreground=True, consolidate_every=consolidate_every)
    os._exit(0)


def stop(cfg: Config) -> bool:
    pidfile = cfg.data_dir / "capture.pid"
    if not pidfile.exists():
        return False
    try:
        pid = int(pidfile.read_text().strip())
        os.kill(pid, signal.SIGTERM)
        for _ in range(20):
            try:
                os.kill(pid, 0)
                time.sleep(0.25)
            except ProcessLookupError:
                break
        pidfile.unlink(missing_ok=True)
        return True
    except (ValueError, ProcessLookupError):
        pidfile.unlink(missing_ok=True)
        return False


def status(cfg: Config) -> dict:
    pidfile = cfg.data_dir / "capture.pid"
    running, pid = False, None
    if pidfile.exists():
        try:
            pid = int(pidfile.read_text().strip())
            os.kill(pid, 0)
            running = True
        except (ValueError, ProcessLookupError, PermissionError):
            pass
    return {"running": running, "pid": pid, "pidfile": str(pidfile)}


SYSTEMD_UNIT = """[Unit]
Description=memor capture daemon
After=network.target docker.service

[Service]
Type=simple
# `capture run` is foreground by default (no --foreground flag exists);
# --daemonize is only for non-systemd use. An unknown flag here used to
# make the unit crash-loop with exit 2 (found 2026-08-25).
ExecStart={memor_bin} capture run --sources {sources}{consolidate_arg}
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
"""


def install_systemd_user(cfg: Config, sources: list[str]) -> Path | None:
    """Write a systemd *user* unit and enable it (needs a systemd user bus)."""
    r = subprocess.run(["systemctl", "--user", "is-system-running"],
                       capture_output=True, text=True)
    if r.returncode != 0 and "degraded" not in (r.stdout + r.stderr):
        return None
    memor_bin = str(Path(sys.executable).parent / "memor")
    consolidate_arg = (f" --consolidate-interval {cfg.consolidate_interval:g}"
                       if cfg.consolidate_interval > 0 else "")
    unit_dir = Path.home() / ".config/systemd/user"
    unit_dir.mkdir(parents=True, exist_ok=True)
    unit = unit_dir / "memor-capture.service"
    unit.write_text(SYSTEMD_UNIT.format(memor_bin=memor_bin,
                                        sources=",".join(sources),
                                        consolidate_arg=consolidate_arg))
    subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)
    subprocess.run(["systemctl", "--user", "enable", "--now", "memor-capture.service"],
                   capture_output=True)
    return unit