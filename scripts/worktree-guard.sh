#!/usr/bin/env bash
# worktree-guard.sh — enforce no-main / dirty-main-aborts discipline.
# Usage:
#   worktree-guard.sh [--repo <path>] check-clean
#   worktree-guard.sh [--repo <path>] create <id> <slug>
set -u

REPO=""

while [ $# -gt 0 ]; do
  case "$1" in
    --repo)
      [ $# -lt 2 ] && { echo "missing value for --repo" >&2; exit 2; }
      REPO="$2"; shift 2;;
    --repo=*)
      REPO="${1#--repo=}"; shift;;
    --) shift; break;;
    -h|--help) usage 2>/dev/null || cat <<'EOF'
Usage: worktree-guard.sh [--repo <path>] check-clean | create <id> <slug>
EOF
      exit 0;;
    *) break;;
  esac
done

if [ -z "$REPO" ]; then
  REPO="$(pwd)"
fi

if [ ! -d "$REPO/.git" ] && [ ! -f "$REPO/.git" ]; then
  echo "not a git repo: $REPO" >&2
  exit 2
fi

SUBCMD="${1:-}"; shift 2>/dev/null || true

check_clean() {
  porcelain="$(git -C "$REPO" status --porcelain)"
  if [ -n "$porcelain" ]; then
    echo "main is dirty, address it first" >&2
    echo "$porcelain"
    return 1
  fi
  return 0
}

case "$SUBCMD" in
  check-clean)
    check_clean
    ;;
  create)
    ID="${1:-}"; SLUG="${2:-}"
    if [ -z "$ID" ] || [ -z "$SLUG" ]; then
      echo "usage: worktree-guard.sh [--repo <path>] create <id> <slug>" >&2
      exit 2
    fi
    if ! check_clean; then
      exit 1
    fi
    BRANCH="ticket/${ID}-${SLUG}"
    WT=".worktrees/${ID}/"
    ABS_WT="$REPO/$WT"
    if [ -e "$ABS_WT" ]; then
      echo "worktree path already exists: $WT" >&2
      exit 1
    fi
    git -C "$REPO" worktree add -b "$BRANCH" "$ABS_WT" || exit $?
    echo "worktree=$WT"
    echo "branch=$BRANCH"
    echo "record in item: worktree=$WT branch=$BRANCH"
    ;;
  *)
    echo "usage: worktree-guard.sh [--repo <path>] check-clean | create <id> <slug>" >&2
    exit 2
    ;;
esac
