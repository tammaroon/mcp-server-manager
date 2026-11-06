#!/bin/zsh
set -euo pipefail
cd "${0:A:h:h}"
mkdir -p build
swiftc -swift-version 5 -parse-as-library Sources/MCPServerManager/Models.swift Sources/MCPServerManager/Backend.swift Tests/MCPServerManagerTests/ManagerStateTests.swift -o build/manager-state-check
./build/manager-state-check

swiftc -swift-version 5 -parse-as-library Sources/MCPServerManager/Models.swift Sources/MCPServerManager/Backend.swift Sources/MCPServerManager/Components.swift Sources/MCPServerManager/UtilityViews.swift Sources/MCPServerManager/MenuBarPresentation.swift tests/MCPServerManagerTests/PopoverLayoutCheck.swift -o build/popover-layout-check
./build/popover-layout-check
