#!/bin/sh
# Bring VMware Fusion to the front and confirm it really is frontmost; exit 1 otherwise (never type then).
osascript -e 'tell application "VMware Fusion" to activate' >/dev/null 2>&1
sleep 0.8
front=$(osascript -e 'tell application "System Events" to get name of first application process whose frontmost is true' 2>/dev/null)
case "$front" in
  *VMware*) echo "front: $front"; exit 0;;
  *) echo "NOT FRONT: $front"; exit 1;;
esac
