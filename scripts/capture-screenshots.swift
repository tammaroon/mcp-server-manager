import AppKit
import SwiftUI

/// Capture the real SwiftUI views with example data, without reading user configs.
@main enum DocumentationScreenshots {
    @MainActor static func main() throws {
        let app = NSApplication.shared
        app.setActivationPolicy(.prohibited)
        let output = URL(fileURLWithPath: CommandLine.arguments.dropFirst().first ?? "docs/screenshots", isDirectory: true)
        try FileManager.default.createDirectory(at: output, withIntermediateDirectories: true)
        let model = try exampleModel()
        try capture(ManagerView(model: model), size: NSSize(width: 1180, height: 900), to: output.appendingPathComponent("manager.png"))
        // The README menu image is a user-supplied capture; leave it intact.
        print("Saved example-data screenshots to \(output.path)")
    }

    @MainActor static func exampleModel() throws -> ManagerModel {
        let now = Date().timeIntervalSince1970
        func server(_ id: String, name: String, endpoint: String, local: Bool = false, login: Bool = false, tools: [String] = []) -> [String: Any] {
            ["id": id, "name": name, "transport": local ? "stdio" : "HTTP", "endpoint": endpoint,
             "enabled": true, "supported": true, "clients": ["codex", "claude", "cursor"], "tool_allowlists": [:],
             "auth": login ? "Authentication required" : local ? "Local process" : "Authenticated",
             "credential_set": false, "credential_header": "Authorization",
             "status": login ? "auth_required" : "reachable", "last_checked": now - 24, "duration_ms": 124,
             "tools": tools.map { ["name": $0, "description": $0.replacingOccurrences(of: "_", with: " ").capitalized] },
             "identity": ["name": name, "version": "1.0.0"],
             "diagnostic": login ? "Sign in to connect this server." : "MCP initialization and tool listing succeeded.",
             "can_sign_in": !local]
        }
        let servers = [
            server("github-demo", name: "GitHub", endpoint: "https://mcp.example.com/github", tools: ["search_repositories", "list_pull_requests", "get_issue"]),
            server("jira-demo", name: "jira", endpoint: "https://mcp.example.com/jira", tools: ["search_issues", "get_issue"]),
            server("confluence-demo", name: "confluence", endpoint: "https://mcp.example.com/docs", tools: ["search_pages", "get_page"]),
            server("filesystem-demo", name: "Filesystem", endpoint: "mcp-filesystem", local: true, tools: ["list_directory", "read_file"]),
            server("browser-demo", name: "Chrome DevTools", endpoint: "mcp-browser", local: true, tools: ["list_pages", "take_screenshot"]),
            server("team-demo", name: "Team workspace", endpoint: "https://mcp.example.com/team", login: true),
        ]
        let clients: [[String: Any]] = [
            ["id": "codex", "name": "Codex", "config_path": "~/.codex/config.toml", "installed": true, "configured": true, "bridge_count": 1],
            ["id": "claude", "name": "Claude Desktop", "config_path": "~/Library/Application Support/Claude/claude_desktop_config.json", "installed": true, "configured": true, "bridge_count": 1],
            ["id": "cursor", "name": "Cursor", "config_path": "~/.cursor/mcp.json", "installed": true, "configured": true, "bridge_count": 1],
            ["id": "vscode", "name": "VS Code", "config_path": "~/Library/Application Support/Code/User/mcp.json", "installed": true, "configured": false, "bridge_count": 0],
        ]
        let payload: [String: Any] = ["servers": servers, "clients": clients, "errors": [], "settings": ["check_interval": 300],
                                    "daemon": ["status": "reachable", "started": now - 3600, "last_checked": now - 24, "socket": "/tmp/mcp-manager-example/gateway.sock", "bridge_count": 3],
                                    "version": "0.1.0", "common_config_path": "~/Library/Application Support/MCP Server Manager/mcp.json"]
        let snapshot = try JSONDecoder().decode(Snapshot.self, from: JSONSerialization.data(withJSONObject: payload))
        let model = ManagerModel()
        model.accept(snapshot)
        model.starting = false
        model.loginEnabled = true
        let config = "{\n  \"url\": \"https://mcp.example.com/github\"\n}"
        // A local draft prevents the editor from making a backend request.
        model.configDrafts["github-demo"] = ServerConfigDraft(text: config, original: config + "\n", revision: "example")
        return model
    }

    @MainActor static func capture<V: View>(_ view: V, size: NSSize, to url: URL) throws {
        let hosting = NSHostingView(rootView: view.preferredColorScheme(.dark))
        let window = NSWindow(contentRect: NSRect(origin: .zero, size: size), styleMask: [.borderless], backing: .buffered, defer: false)
        window.appearance = NSAppearance(named: .darkAqua)
        window.contentView = hosting
        hosting.frame = NSRect(origin: .zero, size: size)
        hosting.layoutSubtreeIfNeeded()
        RunLoop.main.run(until: Date().addingTimeInterval(0.4))
        hosting.layoutSubtreeIfNeeded()
        let bitmap = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: Int(size.width * 2), pixelsHigh: Int(size.height * 2), bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true, isPlanar: false, colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0)!
        bitmap.size = size
        hosting.cacheDisplay(in: hosting.bounds, to: bitmap)
        guard let png = bitmap.representation(using: .png, properties: [:]) else { throw CocoaError(.fileWriteUnknown) }
        try png.write(to: url)
    }
}
