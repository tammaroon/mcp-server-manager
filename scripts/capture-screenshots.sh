#!/bin/zsh
set -euo pipefail
cd "${0:A:h:h}"
mkdir -p .build
sources=(Sources/MCPServerManager/*.swift)
sources=(${sources:#Sources/MCPServerManager/App.swift})
swiftc -swift-version 5 -parse-as-library $sources scripts/capture-screenshots.swift -o .build/documentation-screenshots
.build/documentation-screenshots docs/screenshots
