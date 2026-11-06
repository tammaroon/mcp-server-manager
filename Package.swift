// swift-tools-version: 5.9
import PackageDescription

let package = Package(
    name: "MCPServerManager",
    platforms: [.macOS(.v14)],
    products: [.executable(name: "MCPServerManager", targets: ["MCPServerManager"])],
    targets: [.executableTarget(name: "MCPServerManager", path: "Sources/MCPServerManager")]
)
