#!/usr/bin/env bash
# Launch the Theseus AI dashboard.
#
# Deliberately dependency-free: the app is pure Python 3 standard library, so
# there is no virtualenv to create, no packages to install and nothing to
# build. This script only locates a usable interpreter and hands off.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

# Prefer an explicit override, then python3, then any 3.9+ interpreter found.
PY="${AI_COUNCIL_PYTHON:-}"
if [[ -z "$PY" ]]; then
  for candidate in python3 python3.13 python3.12 python3.11 python3.10 python; do
    if command -v "$candidate" >/dev/null 2>&1; then PY="$candidate"; break; fi
  done
fi

if [[ -z "$PY" ]]; then
  echo "error: no Python interpreter found. Install python3 and try again." >&2
  exit 1
fi

# The app uses match-free syntax but relies on 3.9+ typing and dict merging.
if ! "$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)'; then
  echo "error: Python 3.9 or newer is required (found: $("$PY" --version 2>&1))." >&2
  exit 1
fi

exec "$PY" -m aicouncil "$@"
