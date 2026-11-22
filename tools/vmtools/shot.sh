#!/bin/sh
# Capture the VMware window (points 225,38 1256x839) at 1:1 point scale.
# Screenshots are temporary session data: they go outside the repo (default ~/A_System_vm_shots), never into git.
D=${VMTOOLS_SHOT_DIR:-$HOME/A_System_vm_shots}; N=${1:-shot}
mkdir -p "$D"
osascript -e 'tell application "VMware Fusion" to activate' >/dev/null 2>&1
sleep 0.6
screencapture -x -R225,38,1256,839 "$D/$N.png" && sips -Z 1256 "$D/$N.png" --out "$D/$N.jpg" -s format jpeg -s formatOptions 75 >/dev/null && rm -f "$D/$N.png" && echo "$D/$N.jpg"
