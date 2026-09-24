"""Configuration loading and validation.

Anything in config.yaml is a *preference*. The hard safety rules in safety.py
are applied on top and cannot be relaxed from here.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
STATE_DIR = PROJECT_ROOT / "state"
DEFAULT_CONFIG = PROJECT_ROOT / "config.yaml"          # personal, gitignored
EXAMPLE_CONFIG = PROJECT_ROOT / "config.example.yaml"  # committed template
ENV_FILE = PROJECT_ROOT / ".env"


def load_env(path: Path = ENV_FILE) -> list[str]:
    """Read KEY=value lines from a chmod-600 .env into os.environ.

    This is what lets the launchd plists carry no secrets: the job inherits a
    bare environment and the keys come from one file only the owner can read.
    An existing environment variable always wins, so an interactive shell can
    still override.
    """
    loaded: list[str] = []
    if not path.exists():
        return loaded
    mode = path.stat().st_mode & 0o777
    if mode & 0o077:
        import warnings
        warnings.warn(
            f"{path} is mode {mode:o}; tighten it with: chmod 600 {path}",
            stacklevel=2,
        )
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        line = line.removeprefix("export ").strip()
        if "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = val
            loaded.append(key)
    return loaded


def expand(p: str | Path) -> Path:
    return Path(os.path.expandvars(str(p))).expanduser().resolve()


@dataclass(frozen=True)
class ProviderStep:
    provider: str
    model: str
    attempts: int = 3


@dataclass(frozen=True)
class Behaviour:
    scan_depth: int = 1
    quarantine_hours: int = 24
    max_moves_per_run: int = 150
    run_budget_seconds: int = 900
    auto_confidence: float = 0.85
    auto_confidence_any: float = 0.95
    min_files_for_new_folder: int = 2
    batch_size: int = 20
    snippet_chars: int = 800
    journal_retention_days: int = 90


@dataclass(frozen=True)
class Schedule:
    run_hour: int = 22
    run_minute: int = 0
    alert_hour: int = 8
    alert_minute: int = 0

    @property
    def run_at(self) -> str:
        return f"{self.run_hour:02d}:{self.run_minute:02d}"

    @property
    def alert_at(self) -> str:
        return f"{self.alert_hour:02d}:{self.alert_minute:02d}"


def parse_hhmm(value, field_name: str) -> tuple[int, int]:
    # YAML 1.1 reads an unquoted 22:00 as the base-60 integer 1320. Anyone
    # hand-editing config.yaml will write it unquoted, so accept that form.
    if isinstance(value, int) and not isinstance(value, bool):
        value = f"{value // 60}:{value % 60:02d}"
    try:
        hh, mm = str(value).strip().split(":")
        h, m = int(hh), int(mm)
    except (ValueError, AttributeError) as e:
        raise ConfigError(f"{field_name} must look like 22:00, not {value!r}") from e
    if not (0 <= h <= 23 and 0 <= m <= 59):
        raise ConfigError(f"{field_name} is not a valid time: {value!r}")
    return h, m


@dataclass(frozen=True)
class Config:
    scan_roots: tuple[Path, ...]
    media_destinations: dict[str, Path]
    deny_paths: tuple[Path, ...]
    behaviour: Behaviour
    auto_safe_categories: frozenset[str]
    chain: tuple[ProviderStep, ...]
    timeout_seconds: int = 120
    backoff_base: float = 2.0
    backoff_max: float = 30.0
    notify_enabled: bool = True
    duplicates_action: str = "trash"   # "trash" | "stage"
    schedule: Schedule = field(default_factory=Schedule)
    path: Path = field(default=DEFAULT_CONFIG)

    @property
    def state_dir(self) -> Path:
        return STATE_DIR

    def is_media_destination(self, p: Path) -> bool:
        return any(p == d or d in p.parents for d in self.media_destinations.values())


class ConfigError(RuntimeError):
    pass


def _dup_action(raw: dict) -> str:
    action = ((raw.get("duplicates") or {}).get("action") or "trash").lower()
    if action not in ("trash", "stage"):
        raise ConfigError(
            f"duplicates.action must be 'trash' or 'stage', not {action!r}")
    return action


def _schedule(raw: dict) -> Schedule:
    sched = raw.get("schedule") or {}
    rh, rm = parse_hhmm(sched.get("run", "22:00"), "schedule.run")
    ah, am = parse_hhmm(sched.get("alert", "08:00"), "schedule.alert")
    return Schedule(run_hour=rh, run_minute=rm, alert_hour=ah, alert_minute=am)


def deep_merge(base: dict, override: dict) -> dict:
    """Override wins; nested dicts merge, lists and scalars replace."""
    out = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load(path: Path | None = None) -> Config:
    load_env()
    path = path or DEFAULT_CONFIG
    if not path.exists():
        if path == DEFAULT_CONFIG:
            raise ConfigError(
                "not set up yet — run:  .venv/bin/organize setup")
        raise ConfigError(f"config not found: {path}")
    raw = yaml.safe_load(path.read_text()) or {}
    # The personal config only has to hold what differs from the defaults, so
    # upgrades that add settings work without anyone editing their file. An
    # explicit --config path is taken as complete (tests rely on this).
    if path == DEFAULT_CONFIG and EXAMPLE_CONFIG.exists():
        raw = deep_merge(yaml.safe_load(EXAMPLE_CONFIG.read_text()) or {}, raw)

    roots: list[Path] = []
    for r in raw.get("scan_roots") or []:
        p = expand(r)
        if not p.exists():
            continue  # a configured root that isn't there yet is not an error
        if not p.is_dir():
            raise ConfigError(f"scan root is not a directory: {p}")
        roots.append(p)
    if not roots:
        raise ConfigError("no usable scan_roots configured")

    media = {k: expand(v) for k, v in (raw.get("media_destinations") or {}).items()}
    deny = tuple(expand(d) for d in (raw.get("deny_paths") or []))

    b = raw.get("behaviour") or {}
    behaviour = Behaviour(**{k: v for k, v in b.items() if k in Behaviour.__annotations__})

    chain = tuple(
        ProviderStep(provider=s["provider"], model=s["model"], attempts=int(s.get("attempts", 3)))
        for s in ((raw.get("providers") or {}).get("chain") or [])
    )
    if not chain:
        raise ConfigError("providers.chain is empty; nothing can classify")

    prov = raw.get("providers") or {}
    cfg = Config(
        scan_roots=tuple(roots),
        media_destinations=media,
        deny_paths=deny,
        behaviour=behaviour,
        auto_safe_categories=frozenset(raw.get("auto_safe_categories") or []),
        chain=chain,
        timeout_seconds=int(prov.get("timeout_seconds", 120)),
        backoff_base=float(prov.get("backoff_base", 2.0)),
        backoff_max=float(prov.get("backoff_max", 30.0)),
        notify_enabled=bool((raw.get("notify") or {}).get("enabled", True)),
        duplicates_action=_dup_action(raw),
        schedule=_schedule(raw),
        path=path,
    )

    # Validate roots against the hard rules now, not at walk time.
    from . import safety

    for r in cfg.scan_roots:
        why = safety.root_rejection_reason(r, cfg)
        if why:
            raise ConfigError(f"refusing scan root {r}: {why}")

    # Media destinations become allowed write targets, so they get the same
    # scrutiny. They may sit inside Pictures/Movies/Music (which are barred as
    # scan roots) but never inside a system location or a package bundle.
    for kind, dest in cfg.media_destinations.items():
        if not dest.is_absolute():
            raise ConfigError(f"media destination for {kind} is not absolute: {dest}")
        if safety.under_any(dest, safety.HARD_DENY_ROOTS):
            raise ConfigError(f"media destination for {kind} is in a system "
                              f"location: {dest}")
        if any(safety.is_bundle(p) for p in [dest, *dest.parents]):
            raise ConfigError(f"media destination for {kind} is inside a "
                              f"package bundle: {dest}")
    return cfg
