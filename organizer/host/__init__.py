"""The operating system, behind one interface.

Everything that differs between macOS and Windows — scheduling, file
managers, notifications, file locking, process checks, protected folders —
is reached through here, so the rest of Fily doesn't branch on the OS.
"""
from __future__ import annotations

import sys

IS_WINDOWS = sys.platform == "win32"
IS_MAC = sys.platform == "darwin"

if IS_WINDOWS:
    from . import windows as _impl
else:
    from . import macos as _impl

NAME = _impl.NAME
FILE_MANAGER = _impl.FILE_MANAGER
TRASH_NAME = _impl.TRASH_NAME
RESTORE_HINT = _impl.RESTORE_HINT
CLI = _impl.CLI
TRASH_DIR = _impl.TRASH_DIR
ACCESS_PANE = _impl.ACCESS_PANE

protected_roots = _impl.protected_roots
library_roots = _impl.library_roots
known_folder = _impl.known_folder
map_known_folder = _impl.map_known_folder
hidden = _impl.hidden
cloud_only = _impl.cloud_only
is_link_dir = _impl.is_link_dir
is_file_open = _impl.is_file_open
folder_in_use = _impl.folder_in_use
pid_alive = _impl.pid_alive
run_with_timeout = _impl.run_with_timeout
restrict_to_owner = _impl.restrict_to_owner
owner_only = _impl.owner_only
notify_desktop = _impl.notify_desktop
reveal = _impl.reveal
open_url = _impl.open_url
copy_to_clipboard = _impl.copy_to_clipboard
no_window = _impl.no_window
stdin_is_interactive = _impl.stdin_is_interactive
sleep_risk = _impl.sleep_risk
wake_tip = _impl.wake_tip
access_fix = _impl.access_fix
access_fix_html = _impl.access_fix_html
interpreters_needing_access = _impl.interpreters_needing_access


def scheduler():
    """The launchd or Task Scheduler backend (imported lazily)."""
    if IS_WINDOWS:
        from .. import winsched
        return winsched
    from .. import launchd
    return launchd
