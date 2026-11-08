import AppKit
import Foundation
import ServiceManagement
import SwiftUI

struct BackendError: LocalizedError {
    var message: String
    var errorDescription: String? { message }
}

enum Backend {
    static var script: String {
        let bundled = Bundle.main.bundleURL.appendingPathComponent("Contents/Resources/backend/manager.py").path
        if FileManager.default.fileExists(atPath: bundled) { return bundled }
        return URL(fileURLWithPath: #filePath).deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent().appendingPathComponent("backend/manager.py").path
    }
    static var python: String? {
        let candidates = [Bundle.main.object(forInfoDictionaryKey: "MCPManagerPython") as? String, "/opt/homebrew/bin/python3.14", "/opt/homebrew/bin/python3.12", "/opt/homebrew/bin/python3", "/usr/local/bin/python3", "/usr/bin/python3"].compactMap { $0 }
        return candidates.first { FileManager.default.isExecutableFile(atPath: $0) }
    }
    static func run(_ mode: String, request: [String: Any]? = nil) async throws -> Data {
        try await withCheckedThrowingContinuation { continuation in
            DispatchQueue.global(qos: .userInitiated).async {
                do {
                    guard let python else { throw BackendError(message: "Python 3.11 or later is required. Install Python, then reopen the manager.") }
                    let process = Process()
                    process.executableURL = URL(fileURLWithPath: python)
                    process.arguments = [script, mode]
                    var env = ProcessInfo.processInfo.environment
                    env["PATH"] = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:" + (env["PATH"] ?? "")
                    env["PYTHONDONTWRITEBYTECODE"] = "1"
                    process.environment = env
                    let input = Pipe(), output = Pipe()
                    process.standardInput = input
                    process.standardOutput = output
                    process.standardError = FileHandle.nullDevice
                    try process.run()
                    if let request { try input.fileHandleForWriting.write(contentsOf: JSONSerialization.data(withJSONObject: request)) }
                    try input.fileHandleForWriting.close()
                    // Drain concurrently with process execution, avoiding a full-pipe wait deadlock.
                    let data = output.fileHandleForReading.readDataToEndOfFile()
                    process.waitUntilExit()
                    guard let envelope = try JSONSerialization.jsonObject(with: data) as? [String: Any] else { throw BackendError(message: "The gateway returned an invalid response.") }
                    guard envelope["ok"] as? Bool == true else { throw BackendError(message: envelope["error"] as? String ?? "The gateway request failed.") }
                    guard let value = envelope["result"] else { throw BackendError(message: "The gateway returned no result.") }
                    continuation.resume(returning: try JSONSerialization.data(withJSONObject: value))
                } catch { continuation.resume(throwing: error) }
            }
        }
    }
}

@MainActor final class ManagerModel: ObservableObject {
    @Published var snapshot: Snapshot?
    @Published var gatewayAvailable = false
    @Published var busy = false
    @Published var starting = true
    @Published var operation = "Starting local gateway…"
    @Published var error: String?
    @Published var notice: String?
    @Published var review: ConfigReview?
    @Published var selectedID: String?
    @Published var deletionCandidate: Server?
    @Published var page = "servers"
    @Published var search = ""
    @Published var filter = "All servers"
    @Published var loginEnabled = SMAppService.mainApp.status == .enabled
    var configDrafts: [String: ServerConfigDraft] = [:]
    private var lastConfigIssue: String?
    var timer: Timer?
    var statusChanged: (() -> Void)?
    var servers: [Server] { snapshot?.servers ?? [] }
    var selected: Server? { servers.first { $0.id == selectedID } }
    var filtered: [Server] {
        servers.filter { s in
            (search.isEmpty || "\(s.displayName) \(s.name) \(s.transport)".localizedCaseInsensitiveContains(search)) &&
            (filter == "All servers" || (filter == "Needs attention" && ["failed", "auth_required", "unsupported"].contains(s.status)) || (filter == "Local stdio" && s.transport == "stdio") || (filter == "Remote HTTP" && s.transport == "HTTP"))
        }
    }
    var aggregate: (String, Color) {
        if !gatewayAvailable { return ("Gateway unavailable", Palette.amber) }
        let enabled = servers.filter(\.enabled)
        if enabled.contains(where: { $0.status == "failed" }) { return ("A connection needs attention", Palette.red) }
        if enabled.contains(where: { ["auth_required", "unsupported"].contains($0.status) }) { return ("Setup needs attention", Palette.amber) }
        if enabled.isEmpty { return ("No enabled upstreams", Palette.muted) }
        if enabled.contains(where: { $0.status == "not_checked" }) { return ("Ready for a first check", Palette.muted) }
        return ("All upstreams reachable", Palette.green)
    }
    func start() {
        timer?.invalidate()
        starting = true
        Task {
            do {
                let data = try await Backend.run("ensure")
                accept(try JSONDecoder().decode(Snapshot.self, from: data))
            } catch { connectionFailed(error.localizedDescription) }
            starting = false
            timer = Timer.scheduledTimer(withTimeInterval: 5, repeats: true) { [weak self] _ in
                Task { @MainActor in await self?.refresh() }
            }
        }
    }
    func accept(_ value: Snapshot) {
        snapshot = value
        let activeIDs = Set(value.servers.map(\.id))
        configDrafts = configDrafts.filter { activeIDs.contains($0.key) }
        if error == lastConfigIssue { error = nil }
        lastConfigIssue = value.errors.first
        if let issue = lastConfigIssue { error = issue }
        gatewayAvailable = true
        if !value.servers.contains(where: { $0.id == selectedID }) { selectedID = value.servers.first?.id }
        statusChanged?()
    }
    func connectionFailed(_ message: String) {
        gatewayAvailable = false
        error = message
        statusChanged?()
    }
    func refresh() async {
        guard !busy, !starting else { return }
        do {
            let data = try await Backend.run("control", request: ["action": "snapshot"])
            accept(try JSONDecoder().decode(Snapshot.self, from: data))
        } catch {
            connectionFailed(error.localizedDescription)
            do {
                let data = try await Backend.run("ensure")
                accept(try JSONDecoder().decode(Snapshot.self, from: data))
                self.error = nil
            } catch { connectionFailed(error.localizedDescription) }
        }
    }
    func perform(_ action: String, values: [String: Any] = [:], label: String? = nil) {
        guard !busy else { return }
        busy = true
        operation = label ?? "Updating…"
        error = nil
        Task {
            do {
                var request = values
                request["action"] = action
                let data = try await Backend.run("control", request: request)
                accept(try JSONDecoder().decode(Snapshot.self, from: data))
            } catch { self.error = error.localizedDescription }
            busy = false
        }
    }
    func check(_ server: Server? = nil) {
        perform("check", values: server.map { ["server_id": $0.id] } ?? [:], label: server.map { "Checking \($0.displayName)…" } ?? "Checking daemon & upstreams…")
    }
    func update(_ server: Server, _ changes: [String: Any]) {
        perform("update", values: ["server_id": server.id, "changes": changes])
    }
    func deleteServer(_ server: Server) {
        deletionCandidate = nil
        perform("delete", values: ["server_id": server.id, "confirmed": true], label: "Deleting \(server.displayName)…")
    }
    func toggleClient(_ server: Server, client: String, enabled: Bool) {
        var clients = server.clients.filter { $0 != client }
        if enabled { clients.append(client) }
        update(server, ["clients": clients])
    }
    func preview(_ client: Client, remove: Bool = false) {
        guard !busy else { return }
        busy = true
        error = nil
        Task {
            do {
                let data = try await Backend.run("control", request: ["action": "preview", "client": client.id, "remove": remove])
                review = try JSONDecoder().decode(ConfigReview.self, from: data)
            } catch { self.error = error.localizedDescription }
            busy = false
        }
    }
    func apply(_ change: ConfigReview) {
        guard !busy else { return }
        busy = true
        Task {
            do {
                let data = try await Backend.run("control", request: ["action": "apply", "client": change.client, "digest": change.digest, "remove": change.remove])
                let result = try JSONSerialization.jsonObject(with: data) as? [String: Any]
                review = nil
                notice = "Configuration applied. Restart the client.\n" + ((result?["backup"] as? String).map { "Backup: \($0)" } ?? "A new config file was created.")
                busy = false
                await refresh()
            } catch { self.error = error.localizedDescription; busy = false }
        }
    }
    func setLogin(_ enabled: Bool) {
        do {
            if enabled { try SMAppService.mainApp.register() }
            else { try SMAppService.mainApp.unregister() }
            loginEnabled = SMAppService.mainApp.status == .enabled
            if enabled && !loginEnabled { notice = "Approve MCP Server Manager in System Settings → General → Login Items." }
        } catch { self.error = error.localizedDescription }
    }
    func openCommonConfig() {
        guard !busy else { return }
        busy = true; operation = "Opening common MCP configuration…"
        Task {
            do {
                let data = try await Backend.run("control", request: ["action": "common_config"])
                guard let result = try JSONSerialization.jsonObject(with: data) as? [String: Any], let path = result["path"] as? String else { throw BackendError(message: "The common configuration path is unavailable.") }
                openConfig(path)
            } catch { self.error = error.localizedDescription }
            busy = false
        }
    }
    func signIn(_ server: Server) {
        guard !busy else { return }
        busy = true; operation = "Opening sign-in for \(server.displayName)…"; error = nil
        Task {
            do {
                let data = try await Backend.run("control", request: ["action": "oauth_start", "server_id": server.id])
                guard let result = try JSONSerialization.jsonObject(with: data) as? [String: Any], let value = result["authorization_url"] as? String, let url = URL(string: value), ["https", "http"].contains(url.scheme?.lowercased() ?? "") else { throw BackendError(message: "The server did not provide a valid login screen.") }
                guard NSWorkspace.shared.open(url) else { throw BackendError(message: "The browser could not open the login screen.") }
                notice = "Finish signing in in your browser, then return to MCP Server Manager."
            } catch { self.error = error.localizedDescription }
            busy = false
            await refresh()
        }
    }
    func openConfig(_ path: String) {
        let url = URL(fileURLWithPath: path)
        guard FileManager.default.fileExists(atPath: path) else {
            error = "This config file has not been created. Use Review connection to create it."
            return
        }
        guard let editor = NSWorkspace.shared.urlForApplication(withBundleIdentifier: "com.microsoft.VSCode") else {
            error = "Visual Studio Code is not installed. Install it to open configuration files."
            return
        }
        NSWorkspace.shared.open([url], withApplicationAt: editor, configuration: NSWorkspace.OpenConfiguration()) { [weak self] _, failure in
            if let failure {
                Task { @MainActor in self?.error = "Could not open configuration in Visual Studio Code: " + failure.localizedDescription }
            }
        }
    }
}
