#!/bin/sh
# Publish the committed QMT-native code into the main repository and push it:
#   A_System/qmt_native/        <- exactly this repository's HEAD (git archive)
#   A_System/qmt_native_phase0/ <- the VM deployment folder (strategy files stay ignored)
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
git add qmt_native qmt_native_phase0 .gitignore
if [ -n "$ACC" ] && [ -n "$(git diff --cached -G"$ACC" --name-only)" ]; then
  echo "the broker account appears in staged files: refusing"; git diff --cached -G"$ACC" --name-only; git reset -q -- qmt_native qmt_native_phase0 .gitignore; exit 1
fi
git diff --cached --stat | tail -3
git commit -q -m "$MSG"
git push origin "$(git branch --show-current)"
git log --oneline -1
