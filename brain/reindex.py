#!/usr/bin/env python3
"""
Hook Stop: keep the search index current without getting in the way.

Without this the index is a snapshot: new sessions only become searchable if
someone remembers to run --reindex by hand, and nobody does, so memory ages in
silence.

Three precautions, because Stop fires at the end of EVERY TURN, not session:

  1. THROTTLE by time. Change detection is no guard: the transcript is
     rewritten every turn, so "changed?" is always yes.
  2. LOCK. Fast turns could start two concurrent reindexes.
  3. SILENCE. Nothing here may pollute output or break the turn.
"""
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from brain.core import DATA, load_config                                 # noqa: E402

KSEARCH = Path(__file__).resolve().parent / "ksearch.py"
STAMP = DATA / ".last-reindex"
LOCK = DATA / ".reindex.lock"

MIN_INTERVAL = 15 * 60   # cheap enough not to weigh, short enough that today's
                         # work is searchable today
LOCK_STALE = 10 * 60     # a lock older than this belongs to a dead process


def stale_lock() -> bool:
    try:
        return time.time() - LOCK.stat().st_mtime > LOCK_STALE
    except OSError:
        return True


def start_distill_if_due():
    """Detached worker: the hook returns at once, the distilling runs on its
    own (minutes, one model call per session). A hook that waited for it
    would freeze the turn; SessionEnd would kill it halfway."""
    try:
        from brain import distill
        cfg = load_config()
        if not distill.should_run(cfg):
            return
        env = {k: v for k, v in os.environ.items() if not k.startswith("CLAUDE")}
        with open(distill.AUTO_LOG, "a") as log:
            subprocess.Popen([sys.executable, str(KSEARCH.parent / "distill.py"), "--auto"],
                             stdout=log, stderr=log, stdin=subprocess.DEVNULL,
                             start_new_session=True, env=env)
    except Exception:
        pass


def main() -> int:
    if not load_config()["enabled"]:
        return 0
    try:
        if time.time() - float(STAMP.read_text().strip()) < MIN_INTERVAL:
            return 0
    except (OSError, ValueError):
        pass  # no stamp yet: first run
    if LOCK.exists() and not stale_lock():
        return 0
    try:
        DATA.mkdir(parents=True, exist_ok=True)
        LOCK.write_text(str(os.getpid()))
    except OSError:
        return 0
    try:
        subprocess.run([sys.executable, str(KSEARCH), "--reindex"],
                       capture_output=True, timeout=120)
        # Memory strength rides along: what was injected and opened since
        # the last pass changes which pages recall should trust.
        subprocess.run([sys.executable, str(KSEARCH.parent / "hit_rate.py"), "--strength"],
                       capture_output=True, timeout=120)
        if (load_config().get("embeddings") or {}).get("enabled"):
            subprocess.run([sys.executable, str(KSEARCH.parent / "embed.py"), "--build"],
                           capture_output=True, timeout=900)
        if (load_config().get("catalog") or {}).get("enabled"):
            # Headers only: milliseconds. The catalog is a cache of the pages.
            subprocess.run([sys.executable, str(KSEARCH.parent / "catalog.py"), "--rebuild"],
                           capture_output=True, timeout=120)
        start_distill_if_due()
        # The stamp only advances after finishing: if the reindex fails or is
        # killed, the next stop tries again instead of waiting 15 minutes.
        STAMP.write_text(str(time.time()))
    except (subprocess.SubprocessError, OSError):
        pass
    finally:
        try:
            LOCK.unlink()
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    try:
        sys.stdin.read()          # drain the hook's stdin so the pipe does not hang
    except Exception:
        pass
    try:
        sys.exit(main())
    except Exception:
        sys.exit(0)
