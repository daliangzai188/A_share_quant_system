#!/bin/sh
# Prints the seconds since the last keyboard/mouse input on this Mac.
ioreg -c IOHIDSystem | awk '/HIDIdleTime/ {print int($NF/1000000000); exit}'
