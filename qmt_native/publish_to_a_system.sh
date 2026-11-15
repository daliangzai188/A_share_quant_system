#!/bin/sh
# Publish the committed QMT-native code into the main repository and push it:
#   A_System/qmt_native/        <- this repository's HEAD (git archive)
# The VM scripts live only in the main repository's qmt_native_phase0/ (2026-11-15): they are developed in the main
# repository's dev copy and published with tools/publish_dev_copy.sh; this repository no longer has vm_scripts/.
# Usage: sh publish_to_a_system.sh "commit message"
# 2026-11-15: nothing new to commit is not an error, and a push that failed last time is pushed again on the next run
# (a failed push stops with an error instead of looking like success); every commit about to be pushed is checked for
# the broker account, not only the new one.
set -e
cd "$(dirname "$0")"
MSG=${1:?usage: sh publish_to_a_system.sh "commit message"}
REPO=${A_SYSTEM_REPO:-/Users/eass/Desktop/A_System}
[ -z "$(git status --porcelain)" ] || { echo "commit the QMT-native changes first"; git status --short; exit 1; }
rm -rf "$REPO/qmt_native"
mkdir -p "$REPO/qmt_native"
git archive --format=tar HEAD | tar -x -C "$REPO/qmt_native"
ACC=$(sed -n 's/^QMT_ACCOUNT_ID=//p' local.env 2>/dev/null)
cd "$REPO"
git add qmt_native .gitignore
if [ -n "$ACC" ] && [ -n "$(git diff --cached -G"$ACC" --name-only)" ]; then
  echo "the broker account appears in staged files: refusing"; git diff --cached -G"$ACC" --name-only; git reset -q -- qmt_native .gitignore; exit 1
fi
if git diff --cached --quiet; then
  echo "qmt_native unchanged: no new commit"
else
  git diff --cached --stat | tail -3
  git commit -q -m "$MSG"
fi
BRANCH=$(git branch --show-current)
git fetch -q origin "$BRANCH" 2>/dev/null || echo "fetch failed: comparing with the last known GitHub state"
BASE=$(git rev-parse --verify -q "origin/$BRANCH") || { echo "no origin/$BRANCH: cannot tell what GitHub has"; exit 1; }
RANGE=$BASE..HEAD
if [ -z "$(git rev-list -1 "$RANGE")" ]; then
  echo "GitHub is up to date"
else
  if [ -n "$ACC" ] && { git log --format=%B "$RANGE"; git log --no-renames -p --diff-filter=AMT --format= "$RANGE" | grep '^+'; } \
      | grep -q "$ACC"; then
    echo "the broker account appears in a commit about to be pushed: refusing"; exit 1
  fi
  git push origin "$BRANCH" || { echo "push failed: run this script again to push (the commit is already in $REPO)"; exit 1; }
fi
git log --oneline -1
