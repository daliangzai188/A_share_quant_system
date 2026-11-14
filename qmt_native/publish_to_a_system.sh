#!/bin/sh
# Publish the committed QMT-native code into the main repository and push it:
#   A_System/qmt_native/        <- this repository's HEAD (git archive)
# The VM scripts live only in the main repository's qmt_native_phase0/ (2026-11-15): they are developed in the main
# repository's dev copy and published with tools/publish_dev_copy.sh; this repository no longer has vm_scripts/.
# Usage: sh publish_to_a_system.sh "commit message"
set -e
cd "$(dirname "$0")"
MSG=${1:?usage: sh publish_to_a_system.sh "commit message"}
REPO=/Users/eass/Desktop/A_System
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
git diff --cached --stat | tail -3
git commit -q -m "$MSG"
git push origin "$(git branch --show-current)"
git log --oneline -1
