#!/usr/bin/env bash
set -Eeuo pipefail

REPO_DIR="${GPTCHECKPLUGIN_REPO_DIR:-/root/apps/GptCheckPlugin}"
BRANCH="${GPTCHECKPLUGIN_BRANCH:-main}"
REMOTE="${GPTCHECKPLUGIN_REMOTE:-origin}"
BACKEND_SERVICE="${GPTCHECKPLUGIN_BACKEND_SERVICE:-gptcheckplugin.service}"
FRONTEND_SERVICE="${GPTCHECKPLUGIN_FRONTEND_SERVICE:-gptcheckplugin-frontend.service}"

cd "$REPO_DIR"

if [[ -n "$(git status --porcelain)" ]]; then
  printf 'Deployment stopped: %s has uncommitted changes.\n' "$REPO_DIR" >&2
  exit 1
fi

current_branch="$(git branch --show-current)"
if [[ "$current_branch" != "$BRANCH" ]]; then
  printf 'Deployment stopped: expected branch %s, found %s.\n' "$BRANCH" "${current_branch:-detached HEAD}" >&2
  exit 1
fi

git fetch --prune "$REMOTE"

remote_ref="$REMOTE/$BRANCH"
current_head="$(git rev-parse HEAD)"
target_head="$(git rev-parse "$remote_ref")"
rollback_branch=""

if ! git merge-base --is-ancestor "$current_head" "$target_head"; then
  printf 'Deployment stopped: %s and %s have diverged.\n' "$BRANCH" "$remote_ref" >&2
  exit 1
fi

if [[ "$current_head" != "$target_head" ]]; then
  rollback_branch="rollback/pre-deploy-$(date -u +%Y%m%dT%H%M%SZ)"
  git branch "$rollback_branch" "$current_head"
  git merge --ff-only "$remote_ref"
fi

"$REPO_DIR/.venv/bin/python" -m pip install \
  --disable-pip-version-check \
  --requirement backend/requirements.txt
npm --prefix frontend ci
npm --prefix frontend run build

systemctl restart "$BACKEND_SERVICE" "$FRONTEND_SERVICE"

curl --fail --silent --show-error \
  --retry 10 --retry-delay 2 --retry-connrefused \
  http://127.0.0.1:8000/api/health >/dev/null
curl --fail --silent --show-error \
  --retry 10 --retry-delay 2 --retry-connrefused \
  http://127.0.0.1:5173/ >/dev/null

printf 'Deployed %s at %s.\n' "$BRANCH" "$(git rev-parse --short HEAD)"
if [[ -n "$rollback_branch" ]]; then
  printf 'Rollback branch: %s\n' "$rollback_branch"
fi
