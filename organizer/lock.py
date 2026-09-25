"""A single-holder run lock, so /run from Telegram and the nightly job never
organize the same folder at the same time."""
from __future__ import annotations

import json
import os
import time
from contextlib import contextmanager
from pathlib import Path

from . import host


class Busy(RuntimeError):
    def __init__(self, holder: dict):
        self.holder = holder
        super().__init__(f"already running (pid {holder.get('pid')}, "
                         f"started {holder.get('started_human')})")


def _alive(pid: int) -> bool:
    # Not os.kill(pid, 0): on Windows that terminates the process instead of
    # checking it, which would kill the very run this lock protects.
    return host.pid_alive(pid)


@contextmanager
def run_lock(state_dir: Path, owner: str = "cli"):
    path = state_dir / "run.lock"
    path.parent.mkdir(parents=True, exist_ok=True)

    if path.exists():
        try:
            holder = json.loads(path.read_text(encoding="utf-8"))
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
    }), encoding="utf-8")
    try:
        yield
    finally:
        path.unlink(missing_ok=True)
