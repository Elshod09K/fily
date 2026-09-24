#!/usr/bin/env bash
# Fily installer.
#
#   ./install.sh           first time: install, then run setup
#                          afterwards: update dependencies and re-arm the schedule
#   ./install.sh --setup   run setup again (change keys, folders, time)
#
# Run it from the Terminal app. The schedule is registered with launchd for
# whoever runs this, and jobs registered from inside an IDE or sandbox can
# silently disappear when that process exits.
set -euo pipefail

cd "$(dirname "$0")"
MIN_MINOR=11

bold() { printf '\033[1m%s\033[0m\n' "$*"; }
fail() { printf '\033[31m%s\033[0m\n' "$*" >&2; exit 1; }

[[ "$(uname -s)" == "Darwin" ]] || fail "Fily runs on macOS only (it relies on launchd, Finder and the macOS Trash)."

bold "Installing Fily…"

if command -v uv >/dev/null 2>&1; then
    # uv fetches a suitable Python by itself if this Mac doesn't have one.
    uv venv --quiet --allow-existing --python ">=3.$MIN_MINOR" .venv
    uv pip install --quiet --python .venv/bin/python -e .
else
    PY=""
    for cand in python3.13 python3.12 python3.11 python3; do
        if command -v "$cand" >/dev/null 2>&1 && \
           "$cand" -c "import sys; sys.exit(sys.version_info < (3, $MIN_MINOR))" 2>/dev/null; then
            PY="$cand"; break
        fi
    done
    if [[ -z "$PY" ]]; then
        cat >&2 <<MSG
Fily needs Python 3.$MIN_MINOR or newer, and this Mac doesn't have it.
(The python3 that comes with macOS is too old.) Either:

  • install Python from https://www.python.org/downloads/macos/ , or
  • install uv, which sets Python up automatically:
        curl -LsSf https://astral.sh/uv/install.sh | sh

then run ./install.sh again.
MSG
        exit 1
    fi
    "$PY" -m venv .venv
    .venv/bin/python -m pip install --quiet --upgrade pip
    .venv/bin/python -m pip install --quiet -e .
fi

echo "✓ installed into $(pwd)/.venv"

if [[ "${1:-}" == "--setup" || ! -f config.yaml ]]; then
    exec .venv/bin/organize setup
fi

# Already set up: this was an update. Re-arm the jobs so they point at this
# copy's code, and let the bot restart onto it.
.venv/bin/organize install
echo
bold "Updated. Nothing else to do."
echo "Change keys, folders or time with:  ./install.sh --setup"
