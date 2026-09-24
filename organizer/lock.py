"""A single-holder run lock, so /run from Telegram and the nightly job never
organize the same folder at the same time."""
from __future__ import annotations

import json
import os
import time
from contextlib import contextmanager
from pathlib import Path


class Busy(RuntimeError):
    def __init__(self, holder: dict):
        self.holder = holder
        super().__init__(f"already running (pid {holder.get('pid')}, "
                         f"started {holder.get('started_human')})")


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


@contextmanager
def run_lock(state_dir: Path, owner: str = "cli"):
    path = state_dir / "run.lock"
    path.parent.mkdir(parents=True, exist_ok=True)

    if path.exists():
        try:
            holder = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            holder = {}
        pid = holder.get("pid")
        # A stale lock from a killed process must not block every future run.
        if isinstance(pid, int) and _alive(pid) and pid != os.getpid():
            raise Busy(holder)
        path.unlink(missing_ok=True)

    path.write_text(json.dumps({
        "pid": os.getpid(), "owner": owner, "started": time.time(),
        "started_human": time.strftime("%H:%M:%S"),
    }))
    try:
        yield
    finally:
        path.unlink(missing_ok=True)
