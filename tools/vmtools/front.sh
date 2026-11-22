#!/bin/sh
# Succeeds only when the screen is unlocked AND VMware Fusion is the frontmost app, so no
# click or keystroke can land in another application the user is working in.
S=$(dirname "$0")
"$S/locked.sh" >/dev/null || { echo LOCKED; exit 1; }
APP=$(osascript -e 'tell application "System Events" to get name of first application process whose frontmost is true' 2>/dev/null)
[ "$APP" = "VMware Fusion" ] || { echo "NOT_FRONT:$APP"; exit 1; }
