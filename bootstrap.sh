#!/bin/sh
set -eu

# One-command bootstrap for a remote coding/agent host. The checkout lives in
# user cache, so this script never replaces a workspace or a knowledge repo.
BASE_URL="${REPOSITORY_MEMORY_BOOTSTRAP_URL:-https://github.com/LeslieWylie/repository-memory.git}"
CACHE_HOME="${XDG_CACHE_HOME:-${HOME:-.}/.cache}"
ROOT="${REPOSITORY_MEMORY_BOOTSTRAP_DIR:-$CACHE_HOME/repository-memory-installer}"

if [ ! -d "$ROOT/.git" ]; then
  mkdir -p "$(dirname "$ROOT")"
  git clone --depth 1 "$BASE_URL" "$ROOT"
else
  git -C "$ROOT" fetch --depth 1 origin main
  git -C "$ROOT" checkout --detach FETCH_HEAD
fi

exec python3 "$ROOT/install.py" "$@"
