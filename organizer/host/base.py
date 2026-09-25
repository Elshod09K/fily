"""Types shared by the per-OS scheduler implementations."""
from __future__ import annotations

from dataclasses import dataclass, field

JOBS = ("run", "alert", "bot")


@dataclass
class JobState:
    job: str
    label: str
    installed: bool          # plist written / task definition exists
    registered: bool         # the scheduler actually knows about it
    running: bool
    last_exit: int | None    # None = never ran, or still running
    schedule: str = ""
    where: str = ""          # human hint for where it lives

    @property
    def ok(self) -> bool:
        if not (self.installed and self.registered):
            return False
        if self.job == "bot":
            return self.running
        return self.last_exit in (None, 0)

    @property
    def problem(self) -> str:
        if not self.installed:
            return f"not installed{f' ({self.where})' if self.where else ''}"
        if not self.registered:
            return "not registered with the scheduler — it will never fire"
        if self.job == "bot" and not self.running:
            return "not running, so Telegram will not answer"
        if self.last_exit not in (None, 0):
            return f"last run exited {self.last_exit}"
        return ""


@dataclass
class InstallResult:
    installed: list[str] = field(default_factory=list)
    removed_legacy: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.failed and bool(self.installed)
