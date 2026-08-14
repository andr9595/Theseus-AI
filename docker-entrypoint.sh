#!/usr/bin/env bash
# Reconciles the image's built-in user with the PUID/PGID the operator wants
# files owned by - the linuxserver.io convention most NAS Docker UIs (unraid,
# Synology, TrueNAS) expose, so a mounted volume is written by the uid the
# rest of the host expects, not by whatever uid this image happened to pick.
#
# This has to run as root (the image's default user) and then hand off,
# because changing another user's uid/gid is not a thing a non-root process
# can do to itself.
set -euo pipefail

PUID="${PUID:-1000}"
PGID="${PGID:-1000}"

CURRENT_UID="$(id -u aicouncil)"
CURRENT_GID="$(id -g aicouncil)"

if [ "$PGID" != "$CURRENT_GID" ]; then
  groupmod -o -g "$PGID" aicouncil
fi
if [ "$PUID" != "$CURRENT_UID" ]; then
  usermod -o -u "$PUID" aicouncil
fi

# Only the home directory, not /app - the app image is read-only in spirit
# even though nothing enforces that, and re-chowning it on every start would
# cost time for no benefit. The home volume is what agent installs, git
# credentials and the app's own config write into, and it starts out
# root-owned the first time a fresh volume is mounted.
chown -R aicouncil:aicouncil /home/aicouncil

exec gosu aicouncil "$@"
