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
-- Where Fily (or you, through review) put each file. A placed file is
-- never re-sorted: that is what keeps folders stable night after night.
CREATE TABLE IF NOT EXISTS placements (
    sha256      TEXT PRIMARY KEY,
    path        TEXT NOT NULL,
    placed_at   REAL NOT NULL
);
-- Folders Fily created. They are organizing folders, so it may look inside.
CREATE TABLE IF NOT EXISTS fily_dirs (
    path        TEXT PRIMARY KEY,
    created_at  REAL NOT NULL
);
-- Whole folders Fily moved as a set. They stay where they were put.
CREATE TABLE IF NOT EXISTS placed_dirs (
    path        TEXT PRIMARY KEY,
    placed_at   REAL NOT NULL
);
-- How each folder was judged ("set" or "open"), keyed by its contents, so
-- an unchanged folder is never asked about twice.
CREATE TABLE IF NOT EXISTS folder_decisions (
    fingerprint TEXT PRIMARY KEY,
    kind        TEXT NOT NULL,
    category    TEXT,
    folder      TEXT,
    confidence  REAL,
    reason      TEXT,
    decided_at  REAL NOT NULL
);
-- path + size + mtime -> sha256, so unchanged files aren't re-hashed nightly.
CREATE TABLE IF NOT EXISTS file_hashes (
    path        TEXT PRIMARY KEY,
    size        INTEGER NOT NULL,
    mtime       REAL NOT NULL,
    sha256      TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (
    key         TEXT PRIMARY KEY,
    value       TEXT
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

    # ------------------------------------------------------- placements

    def place(self, sha: str, path: str) -> None:
        self.db.execute("INSERT OR REPLACE INTO placements VALUES (?,?,?)",
                        (sha, path, time.time()))

    def unplace(self, sha: str) -> None:
        self.db.execute("DELETE FROM placements WHERE sha256 = ?", (sha,))

    def placed_shas(self) -> set[str]:
        return {r[0] for r in self.db.execute("SELECT sha256 FROM placements")}

    def placements(self) -> dict[str, str]:
        """sha256 -> where it was placed."""
        return dict(self.db.execute("SELECT sha256, path FROM placements"))

    def add_fily_dirs(self, paths) -> None:
        self.db.executemany("INSERT OR IGNORE INTO fily_dirs VALUES (?,?)",
                            [(p, time.time()) for p in paths])

    def fily_dirs(self) -> set[str]:
        return {r[0] for r in self.db.execute("SELECT path FROM fily_dirs")}

    def place_dir(self, path: str) -> None:
        self.db.execute("INSERT OR REPLACE INTO placed_dirs VALUES (?,?)",
                        (path, time.time()))

    def unplace_dir(self, path: str) -> None:
        self.db.execute("DELETE FROM placed_dirs WHERE path = ?", (path,))

    def placed_dirs(self) -> set[str]:
        return {r[0] for r in self.db.execute("SELECT path FROM placed_dirs")}

    def record_journal(self, path: Path) -> None:
        """Fold a journal's moves into what Fily remembers."""
        for e in read_journal(path):
            action = e.action or "move"
            if action == "move" and e.sha256:
                self.place(e.sha256, e.dst)
                self.add_fily_dirs(e.created_dirs or [])
            elif action == "move_dir":
                self.place_dir(e.dst)
                self.add_fily_dirs(e.created_dirs or [])
            elif action == "trash" and e.sha256:
                self.unplace(e.sha256)
        self.db.commit()

    def forget_journal(self, path: Path) -> None:
        """After an undo: those files and folders are unsorted again."""
        for e in read_journal(path):
            action = e.action or "move"
            if action == "move" and e.sha256:
                self.unplace(e.sha256)
            elif action == "move_dir":
                self.unplace_dir(e.dst)
        self.db.commit()

    def backfill(self, cfg: Config) -> int:
        """Learn placements from journals written before this table existed.
        Runs once; later journals are recorded as they are written."""
        done = self.db.execute(
            "SELECT value FROM meta WHERE key = 'placements_backfilled'").fetchone()
        if done:
            return 0
        n = 0
        for j in Journal.list_runs(cfg):           # *.jsonl only: undone runs
            self.record_journal(j)                 # are renamed .jsonl.undone
            n += 1
        self.db.execute("INSERT OR REPLACE INTO meta VALUES ('placements_backfilled', ?)",
                        (str(time.time()),))
        self.db.commit()
        return n

    # ------------------------------------------------------ folder decisions

    def folder_get(self, fingerprint: str) -> dict | None:
        row = self.db.execute(
            "SELECT kind, category, folder, confidence, reason FROM folder_decisions "
            "WHERE fingerprint = ?", (fingerprint,)).fetchone()
        if not row:
            return None
        return dict(zip(["kind", "category", "folder", "confidence", "reason"], row))

    def folder_put(self, fingerprint: str, d: dict) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO folder_decisions VALUES (?,?,?,?,?,?,?)",
            (fingerprint, d.get("kind", "set"), d.get("category"), d.get("folder"),
             float(d.get("confidence", 0.0)), d.get("reason"), time.time()))
        self.db.commit()

    # ------------------------------------------------------------ hash cache

    def cached_hash(self, path: str, size: int, mtime: float) -> str | None:
        row = self.db.execute(
            "SELECT sha256 FROM file_hashes WHERE path = ? AND size = ? AND mtime = ?",
            (path, size, mtime)).fetchone()
        return row[0] if row else None

    def remember_hash(self, path: str, size: int, mtime: float, sha: str) -> None:
        self.db.execute("INSERT OR REPLACE INTO file_hashes VALUES (?,?,?,?)",
                        (path, size, mtime, sha))

    def commit(self) -> None:
        self.db.commit()

    def close(self) -> None:
        self.db.commit()
        self.db.close()
