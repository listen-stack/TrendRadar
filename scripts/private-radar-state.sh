#!/usr/bin/env bash
set -euo pipefail

repository="${GITHUB_REPOSITORY_OWNER}/trendradar-state"
if [[ -z "${GH_TOKEN:-}" ]]; then
  printf '%s\n' 'Configure RADAR_STATE_TOKEN before running send.' >&2
  exit 1
fi

metadata="$(gh api "repos/$repository" --jq \
  '[.full_name, (.private | tostring), (.permissions.push | tostring), .default_branch] | @tsv')"
IFS=$'\t' read -r name private can_push branch <<< "$metadata"
if [[ "${name,,}" != "${repository,,}" || "$private" != true || "$can_push" != true ]] ||
   ! git check-ref-format "refs/heads/$branch"; then
  printf '%s\n' 'The history target must be the configured private repository with write access.' >&2
  exit 1
fi

if [[ "$1" == verify ]]; then
  printf '%s\n' "$branch"
  exit 0
fi

if [[ "$1" != save || "$branch" != "${RADAR_STATE_BRANCH:-}" || ! -e radar-state/.git ]]; then
  printf '%s\n' 'The private history checkout or expected branch is missing.' >&2
  exit 1
fi

push_url="$(git -C radar-state remote get-url --push origin)"
expected="https://github.com/$repository"
if [[ "$push_url" != "$expected" && "$push_url" != "$expected.git" ]]; then
  printf '%s\n' 'The history checkout must push to the verified private repository.' >&2
  exit 1
fi

git -C radar-state add -- pending.json
if [[ -d radar-state/history ]]; then
  git -C radar-state add -- history/
fi
if ! git -C radar-state diff --cached --quiet; then
  git -C radar-state -c user.name='github-actions[bot]' \
    -c user.email='41898282+github-actions[bot]@users.noreply.github.com' commit -m "$2"
fi
git -C radar-state push origin "HEAD:refs/heads/$branch"
