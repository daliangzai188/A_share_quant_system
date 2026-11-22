#!/bin/sh
# Build the vmctl input helper from vmctl.swift (the binary is not kept in git; rebuild after cloning or editing).
cd "$(dirname "$0")" && swiftc -O vmctl.swift -o vmctl && echo "built $(pwd)/vmctl"
