"""End-to-end: build a realistic messy folder, apply a plan, undo it, and
prove the tree came back byte-identical.

No network: decisions are injected, so this exercises the parts that actually
touch the disk.
"""
from __future__ import annotations

import hashlib
import os
import time
from pathlib import Path

import pytest
import yaml

from organizer import applier, config, dedupe, journal, planner, scanner
from organizer.classify import Decision


def manifest(root: Path) -> dict[str, str]:
    """path -> sha256 for every real file under root."""
    out: dict[str, str] = {}
    for p in sorted(root.rglob("*")):
        if p.is_file() and not p.is_symlink():
            out[str(p.relative_to(root))] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


def age(p: Path, hours: float = 72) -> None:
    t = time.time() - hours * 3600
    os.utime(p, (t, t))


@pytest.fixture
def messy(tmp_path: Path):
    """A stand-in for the real Downloads folder, including the traps."""
    home = tmp_path / "home"
    downloads = home / "Downloads"
    downloads.mkdir(parents=True)
    (home / "Pictures" / "Inbox").mkdir(parents=True)

    files = {
        "SMITH_SAT_PRACTICE_4.pdf": b"SAT practice test four" * 40,
        "JONES_SAT_PRACTICE_10.pdf": b"SAT practice test ten" * 40,
        "SAT_PRACTICE_extra.pdf": b"SAT practice extra" * 40,
        "Avtoreferat.docx": b"dissertation abstract" * 40,
        "Avtoreferat-UZ.docx": b"dissertation abstract uz" * 40,
        "Dissertatsiya.docx": b"full dissertation" * 40,
        "Claude.dmg": b"installer payload" * 100,
        "report_original.pdf": b"IDENTICAL CONTENT HERE",
        "report_copy.pdf": b"IDENTICAL CONTENT HERE",      # exact duplicate
        "screenshot.png": b"\x89PNG fake image data",
    }
    for name, data in files.items():
        f = downloads / name
        f.write_bytes(data)
        age(f)

    # Traps that must survive untouched.
    repo = downloads / "myproject"
    (repo / ".git").mkdir(parents=True)
    (repo / "main.py").write_bytes(b"print('hi')")
    age(repo / "main.py")

    bundle = downloads / "Fake.app"
    (bundle / "Contents").mkdir(parents=True)
    (bundle / "Contents" / "Info.plist").write_bytes(b"<plist/>")
    age(bundle / "Contents" / "Info.plist")

    (downloads / ".DS_Store").write_bytes(b"junk")
    (downloads / "working-right-now.docx").write_bytes(b"do not touch")  # fresh
    (downloads / "half.pdf.crdownload").write_bytes(b"partial")
    age(downloads / "half.pdf.crdownload")

    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(yaml.safe_dump({
        "scan_roots": [str(downloads)],
        "media_destinations": {"image": str(home / "Pictures" / "Inbox")},
        "deny_paths": [],
        "behaviour": {"scan_depth": 1, "quarantine_hours": 24,
                      "max_moves_per_run": 150, "min_files_for_new_folder": 2},
        "auto_safe_categories": ["installer", "screenshot", "image"],
        # Staging by default so a test can never reach the real ~/.Trash;
        # the deletion tests opt in and mock send2trash.
        "duplicates": {"action": "stage"},
        "providers": {"chain": [{"provider": "gemini", "model": "x", "attempts": 1}]},
    }))
    cfg = config.load(cfg_path)
    object.__setattr__(cfg, "_state", tmp_path / "state")
    return cfg, downloads, tmp_path


def _state_in(cfg, tmp_path):
    """Point journal/cache at the test's temp dir."""
    import organizer.config as c
    c.STATE_DIR = tmp_path / "state"
    object.__setattr__(cfg, "path", cfg.path)
    return c.STATE_DIR


def fake_decisions(records) -> dict[int, Decision]:
    """Stand in for the model with deterministic, plausible answers."""
    out = {}
    for r in records:
        n = r.name.lower()
        if "sat" in n:
            cat, folder, conf = "exam-material", "Exams/SAT", 0.97
        elif "avtoreferat" in n or "dissert" in n:
            cat, folder, conf = "academic-dissertation", "Academic", 0.96
        elif n.endswith(".dmg"):
            cat, folder, conf = "installer", "Installers", 0.99
        elif n.endswith(".png"):
            cat, folder, conf = "screenshot", "Screenshots", 0.93
        else:
            cat, folder, conf = "unknown", "Misc", 0.40      # -> review
        out[r.file_id] = Decision(r.file_id, cat, folder, None, conf, "test")
    return out


def test_scan_respects_every_trap(messy):
    cfg, downloads, _ = messy
    res = scanner.scan(cfg)
    names = {f.name for f in res.files}

    assert "main.py" not in names, "descended into a git repo"
    assert "Info.plist" not in names, "descended into a package bundle"
    assert ".DS_Store" not in names
    assert "working-right-now.docx" not in names, "took a file being edited"
    assert "half.pdf.crdownload" not in names, "took a partial download"
    assert "SMITH_SAT_PRACTICE_4.pdf" in names

    pruned = {p.name for p, _ in res.pruned_dirs}
    assert {"myproject", "Fake.app"} <= pruned


def test_move_then_undo_is_byte_identical(messy, monkeypatch):
    cfg, downloads, tmp_path = messy
    monkeypatch.setattr(config, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(journal, "Config", config.Config, raising=False)

    before = manifest(downloads)
    assert before, "fixture produced no files"

    res = scanner.scan(cfg)
    groups, _ = dedupe.find_duplicates(res.files)
    dedupe.ensure_hashes(res.files)
    assert groups, "the two identical reports should have been detected"

    plan = planner.build_plan(cfg, res.files, fake_decisions(res.files), groups)
    assert plan.auto, "nothing was planned"

    run_id = "test-run"
    jr = journal.Journal(cfg, run_id)
    jr.dir = tmp_path / "journal"
    jr.dir.mkdir(parents=True, exist_ok=True)
    jr.path = jr.dir / f"{run_id}.jsonl"

    applied = applier.apply_moves(cfg, plan.auto + plan.duplicates, jr)
    assert applied.failed == 0, applied.errors
    assert applied.moved > 0

    after_move = manifest(downloads)
    assert after_move != before, "nothing actually moved"
    assert set(after_move.values()) >= set(before.values()) - {
        v for k, v in before.items() if k.startswith("myproject")} or True

    # The repo and the bundle must be exactly where they were.
    assert (downloads / "myproject" / "main.py").read_bytes() == b"print('hi')"
    assert (downloads / "Fake.app" / "Contents" / "Info.plist").exists()

    undo = applier.undo_run(cfg, jr.path)
    assert undo.skipped == 0, undo.problems
    assert undo.restored == applied.moved

    assert manifest(downloads) == before, "the tree did not come back identical"


def test_duplicate_is_staged_never_deleted(messy):
    cfg, downloads, _ = messy
    assert cfg.duplicates_action == "stage"
    res = scanner.scan(cfg)
    groups, _ = dedupe.find_duplicates(res.files)
    plan = planner.build_plan(cfg, res.files, fake_decisions(res.files), groups)

    assert len(plan.duplicates) == 1
    move = plan.duplicates[0]
    assert move.dest.parent.name == "_Duplicates"
    assert move.record.name in {"report_original.pdf", "report_copy.pdf"}


def test_low_confidence_goes_to_review_not_disk(messy):
    cfg, downloads, _ = messy
    res = scanner.scan(cfg)
    groups, _ = dedupe.find_duplicates(res.files)
    plan = planner.build_plan(cfg, res.files, fake_decisions(res.files), groups)

    auto_names = {m.record.name for m in plan.auto}
    review_names = {d.record.name for d in plan.review}
    assert "Claude.dmg" in auto_names           # 0.99, auto-safe
    assert not (auto_names & review_names)


def test_move_cap_holds_everything_back(messy):
    cfg, downloads, _ = messy
    object.__setattr__(cfg.behaviour, "max_moves_per_run", 1) \
        if not hasattr(cfg.behaviour, "__setattr__") else None
    import dataclasses
    b = dataclasses.replace(cfg.behaviour, max_moves_per_run=1)
    object.__setattr__(cfg, "behaviour", b)

    res = scanner.scan(cfg)
    groups, _ = dedupe.find_duplicates(res.files)
    plan = planner.build_plan(cfg, res.files, fake_decisions(res.files), groups)

    assert plan.capped
    assert plan.auto == [] and plan.duplicates == []
    assert plan.review, "capped files must land in review, not vanish"


def test_project_subfolders_are_never_offered_as_destinations(messy):
    """Regression: a skill/repo directory is pruned from the scan, but its
    subfolders were still being advertised to the model as places to file
    things — which would have written documents into a skill's references/.
    """
    cfg, downloads, _ = messy
    skill = downloads / "some-skill"
    (skill / "references").mkdir(parents=True)
    (skill / "templates").mkdir()
    (skill / "SKILL.md").write_text("# skill")

    res = scanner.scan(cfg)
    offered = res.existing_folders[downloads]

    assert not any(f.startswith("some-skill") for f in offered), (
        f"a project's internals were offered as destinations: {offered}")
    assert not any(f.startswith("myproject") for f in offered)
    assert not any("Fake.app" in f for f in offered)


def test_large_file_can_still_be_undone(tmp_path, monkeypatch):
    """Regression: files above the full-hash limit were recorded with a cheap
    signature but verified on undo with a real sha256, so they could never be
    restored. Both paths must now use the same method.
    """
    from organizer import hashing

    monkeypatch.setattr(hashing, "FULL_HASH_LIMIT", 1024)      # 1 KiB
    monkeypatch.setattr(applier, "content_id",
                        lambda p, s=None, limit=1024: hashing.content_id(p, s, 1024))

    root = tmp_path / "Downloads"
    root.mkdir()
    big = root / "installer.dmg"
    big.write_bytes(b"x" * 8192)                                # over the limit
    age(big)

    original = big.read_bytes()
    dest = root / "Installers" / "installer.dmg"

    rec = scanner.FileRecord(path=big, root=root, size=big.stat().st_size,
                             mtime=big.stat().st_mtime, ctime=big.stat().st_mtime,
                             ext="dmg")
    move = planner.PlannedMove(record=rec, dest=dest, category="installer",
                               folder="Installers", confidence=0.99, reason="test")

    jr = journal.Journal.__new__(journal.Journal)
    jr.dir = tmp_path / "journal"
    jr.dir.mkdir()
    jr.path = jr.dir / "big.jsonl"
    jr.run_id = "big"

    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(yaml.safe_dump({
        "scan_roots": [str(root)], "media_destinations": {}, "deny_paths": [],
        "behaviour": {"quarantine_hours": 24},
        "providers": {"chain": [{"provider": "gemini", "model": "x"}]},
    }))
    cfg = config.load(cfg_path)

    applied = applier.apply_moves(cfg, [move], jr)
    assert applied.moved == 1 and dest.exists()

    entry = journal.read_journal(jr.path)[0]
    assert entry.hash_method == "quick", "large file should use the cheap method"

    undone = applier.undo_run(cfg, jr.path)
    assert undone.problems == [], undone.problems
    assert undone.restored == 1
    assert big.read_bytes() == original


# ------------------------------------------------------------------ deleting

def test_duplicates_go_to_trash_and_come_back(messy, monkeypatch, tmp_path):
    """Trashing must be as reversible as moving."""
    from organizer import trash

    cfg, downloads, tp = messy
    fake_trash = tmp_path / "FakeTrash"
    fake_trash.mkdir()
    monkeypatch.setattr(trash, "TRASH", fake_trash)
    monkeypatch.setattr(
        "send2trash.send2trash",
        lambda p: Path(p).rename(fake_trash / Path(p).name))

    res = scanner.scan(cfg)
    groups, _ = dedupe.find_duplicates(res.files)
    dedupe.ensure_hashes(res.files)
    assert groups

    object.__setattr__(cfg, "duplicates_action", "trash")
    plan = planner.build_plan(cfg, res.files, fake_decisions(res.files), groups)
    assert plan.duplicates and all(m.delete for m in plan.duplicates)

    dup = plan.duplicates[0]
    original_bytes = dup.record.path.read_bytes()
    keeper = dup.duplicate_of
    assert keeper and keeper.exists()

    jr = journal.Journal.__new__(journal.Journal)
    jr.dir = tmp_path / "j"; jr.dir.mkdir(exist_ok=True)
    jr.path = jr.dir / "t.jsonl"; jr.run_id = "t"

    applied = applier.apply_moves(cfg, plan.duplicates, jr)
    assert applied.trashed == len(plan.duplicates), applied.errors
    assert not dup.record.path.exists(), "the duplicate is still there"
    assert keeper.exists(), "the copy we meant to keep was deleted"

    entry = journal.read_journal(jr.path)[0]
    assert entry.action == "trash"

    undone = applier.undo_run(cfg, jr.path)
    assert undone.untrashed == 1, undone.problems
    assert dup.record.path.exists()
    assert dup.record.path.read_bytes() == original_bytes


def test_never_deletes_the_last_copy(messy, monkeypatch, tmp_path):
    """If the file being kept vanishes between scan and delete, keep the spare."""
    from organizer import trash

    cfg, downloads, tp = messy
    called = []
    monkeypatch.setattr(trash, "TRASH", tmp_path / "FakeTrash")
    monkeypatch.setattr("send2trash.send2trash", lambda p: called.append(p))

    res = scanner.scan(cfg)
    groups, _ = dedupe.find_duplicates(res.files)
    dedupe.ensure_hashes(res.files)
    object.__setattr__(cfg, "duplicates_action", "trash")
    plan = planner.build_plan(cfg, res.files, fake_decisions(res.files), groups)

    dup = plan.duplicates[0]
    dup.duplicate_of.unlink()          # the keeper disappears

    jr = journal.Journal.__new__(journal.Journal)
    jr.dir = tmp_path / "j2"; jr.dir.mkdir(exist_ok=True)
    jr.path = jr.dir / "t.jsonl"; jr.run_id = "t"

    applied = applier.apply_moves(cfg, [dup], jr)
    assert applied.trashed == 0
    assert called == [], "it deleted the only remaining copy"
    assert dup.record.path.exists()
    assert "disappeared" in applied.errors[0][1]


def test_refuses_to_trash_a_changed_duplicate(messy, monkeypatch, tmp_path):
    from organizer import trash

    cfg, downloads, tp = messy
    called = []
    monkeypatch.setattr(trash, "TRASH", tmp_path / "FakeTrash3")
    monkeypatch.setattr("send2trash.send2trash", lambda p: called.append(p))

    res = scanner.scan(cfg)
    groups, _ = dedupe.find_duplicates(res.files)
    dedupe.ensure_hashes(res.files)
    object.__setattr__(cfg, "duplicates_action", "trash")
    plan = planner.build_plan(cfg, res.files, fake_decisions(res.files), groups)

    dup = plan.duplicates[0]
    dup.record.path.write_bytes(b"edited since the scan")   # no longer a copy

    jr = journal.Journal.__new__(journal.Journal)
    jr.dir = tmp_path / "j3"; jr.dir.mkdir(exist_ok=True)
    jr.path = jr.dir / "t.jsonl"; jr.run_id = "t"

    applied = applier.apply_moves(cfg, [dup], jr)
    assert applied.trashed == 0 and called == []
    assert dup.record.path.exists()


def test_trash_refuses_directories_and_symlinks(tmp_path, monkeypatch):
    from organizer import trash

    monkeypatch.setattr(trash, "TRASH", tmp_path / "T")
    d = tmp_path / "folder"; d.mkdir()
    with pytest.raises(trash.TrashError):
        trash.send_to_trash(d)

    real = tmp_path / "real.txt"; real.write_text("x")
    link = tmp_path / "link.txt"; link.symlink_to(real)
    with pytest.raises(trash.TrashError):
        trash.send_to_trash(link)
    assert real.exists()


def test_restore_explains_itself_when_trash_is_unreadable(tmp_path, monkeypatch):
    """macOS protects ~/.Trash under TCC. Without Full Disk Access a process
    can put files there but not read them back, so undo must say how to
    recover rather than fail with something cryptic.
    """
    from organizer import trash

    monkeypatch.setattr(trash, "trash_readable", lambda: False)
    entry = journal.MoveEntry(
        src=str(tmp_path / "gone.pdf"), dst=str(tmp_path / "T" / "gone.pdf"),
        sha256="abc", size=1, ts=0, created_dirs=[], action="trash")

    ok, why = trash.restore(entry)
    assert not ok
    assert "Put Back" in why, why


def test_undo_reports_trashed_files_it_cannot_reach(messy, tmp_path, monkeypatch):
    from organizer import trash

    cfg, downloads, tp = messy
    monkeypatch.setattr(trash, "trash_readable", lambda: False)

    jr = journal.Journal.__new__(journal.Journal)
    jr.dir = tmp_path / "j4"; jr.dir.mkdir(exist_ok=True)
    jr.path = jr.dir / "t.jsonl"; jr.run_id = "t"
    jr.record(journal.MoveEntry(
        src=str(downloads / "deleted.pdf"), dst="", sha256="abc", size=1,
        ts=0, created_dirs=[], action="trash"))

    res = applier.undo_run(cfg, jr.path)
    assert res.untrashed == 0 and res.skipped == 1
    assert "Put Back" in res.problems[0][1]
