"""Move journal (append-only JSONL) and the sha256 decision cache.

Every applied move is recorded before it is considered done, so an interrupted
run is still fully reversible from whatever reached the journal.
"""
from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path

from .config import Config


def new_run_id() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


@dataclass
class MoveEntry:
    src: str
    dst: str
    sha256: str
    size: int
    ts: float
    created_dirs: list[str]
    hash_method: str = "sha256"
    action: str = "move"        # "move" | "trash"
    category: str = ""
    confidence: float = 0.0
    provider: str = ""


class Journal:
    def __init__(self, cfg: Config, run_id: str):
        self.cfg = cfg
        self.run_id = run_id
        self.dir = cfg.state_dir / "journal"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path = self.dir / f"{run_id}.jsonl"

    def record(self, entry: MoveEntry) -> None:
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(asdict(entry), ensure_ascii=False) + "\n")
            fh.flush()

    def entries(self) -> list[MoveEntry]:
        return read_journal(self.path)

    @staticmethod
    def list_runs(cfg: Config) -> list[Path]:
        d = cfg.state_dir / "journal"
        if not d.is_dir():
            return []
        return sorted(d.glob("*.jsonl"), key=lambda p: p.name)

    @staticmethod
    def prune(cfg: Config) -> int:
        cutoff = datetime.now() - timedelta(days=cfg.behaviour.journal_retention_days)
        removed = 0
        for p in Journal.list_runs(cfg):
            try:
                stamp = datetime.strptime(p.stem, "%Y%m%d-%H%M%S")
            except ValueError:
                continue
            if stamp < cutoff:
                p.unlink()
                removed += 1
        return removed


def read_journal(path: Path) -> list[MoveEntry]:
    out: list[MoveEntry] = []
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        out.append(MoveEntry(**{k: d.get(k) for k in MoveEntry.__annotations__}))
    return out


SCHEMA = """
CREATE TABLE IF NOT EXISTS decisions (
    sha256      TEXT PRIMARY KEY,
    category    TEXT NOT NULL,
    folder      TEXT NOT NULL,
    project     TEXT,
    confidence  REAL NOT NULL,
    reason      TEXT,
    provider    TEXT,
    decided_at  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
    run_id      TEXT PRIMARY KEY,
    started_at  REAL,
    finished_at REAL,
    status      TEXT,
    scanned     INTEGER,
    moved       INTEGER,
    queued      INTEGER,
    detail      TEXT
);
"""


class Cache:
    """sha256 -> prior decision. Keeps steady-state runs nearly token-free."""

    def __init__(self, cfg: Config):
        cfg.state_dir.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(cfg.state_dir / "cache.db")
        self.db.executescript(SCHEMA)
        self.db.commit()

    def get(self, sha: str) -> dict | None:
        cur = self.db.execute(
            "SELECT category, folder, project, confidence, reason, provider "
            "FROM decisions WHERE sha256 = ?", (sha,))
        row = cur.fetchone()
        if not row:
            return None
        return dict(zip(
            ["category", "folder", "project", "confidence", "reason", "provider"], row))

    def put(self, sha: str, d: dict) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO decisions "
            "(sha256, category, folder, project, confidence, reason, provider, decided_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (sha, d.get("category", "unknown"), d.get("folder", ""), d.get("project"),
             float(d.get("confidence", 0.0)), d.get("reason"), d.get("provider"),
             time.time()),
        )
        self.db.commit()

    def start_run(self, run_id: str, scanned: int) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO runs (run_id, started_at, status, scanned) "
            "VALUES (?,?,?,?)", (run_id, time.time(), "running", scanned))
        self.db.commit()

    def finish_run(self, run_id: str, status: str, moved: int, queued: int,
                   detail: str = "") -> None:
        self.db.execute(
            "UPDATE runs SET finished_at=?, status=?, moved=?, queued=?, detail=? "
            "WHERE run_id=?", (time.time(), status, moved, queued, detail, run_id))
        self.db.commit()

    def recent_runs(self, limit: int = 10) -> list[dict]:
        cur = self.db.execute(
            "SELECT run_id, started_at, finished_at, status, scanned, moved, queued, detail "
            "FROM runs ORDER BY started_at DESC LIMIT ?", (limit,))
        cols = ["run_id", "started_at", "finished_at", "status", "scanned",
                "moved", "queued", "detail"]
        return [dict(zip(cols, r)) for r in cur.fetchall()]

    def close(self) -> None:
        self.db.close()
