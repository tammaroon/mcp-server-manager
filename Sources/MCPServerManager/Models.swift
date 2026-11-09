import Foundation
import SwiftUI

struct Tool: Decodable, Identifiable {
    var name: String
    var description: String?
    var id: String { name }
}
struct Identity: Decodable {
    var name: String?
    var version: String?
}
struct Server: Decodable, Identifiable {
    var id: String
    var name: String
    var transport: String
    var endpoint: String
    var enabled: Bool
    var supported: Bool
    var clients: [String]
    var tool_allowlists: [String: [String]]
    var auth: String
    var status: String
    var last_checked: Double?
    var duration_ms: Int?
    var tools: [Tool]
    var identity: Identity?
    var diagnostic: String
    var can_sign_in: Bool?
    var oauth_status: String?
    var oauth_error: String?
    var supportsBrowserSignIn: Bool {
        supported && transport == "HTTP" && (can_sign_in ?? true)
    }
    var shouldOfferDropdownLogin: Bool {
        enabled && supportsBrowserSignIn && ["auth_required", "failed"].contains(status)
    }
    var glyph: String {
        let label = name.lowercased()
        if label.contains("confluence") { return "doc.text" }
        if label.contains("jira") { return "square.stack.3d.up" }
        if label.contains("logs") { return "text.alignleft" }
        if label.contains("webex") { return "video" }
        if label.contains("computer") || label.contains("chrome") { return "desktopcomputer" }
        return transport == "HTTP" ? "globe" : "terminal"
    }
    var displayName: String {
        name
    }
    var statusLabel: String {
        switch status {
        case "reachable": return "Reachable"
        case "disabled": return "Disabled"
        case "auth_required": return "Sign-in needed"
        case "failed": return "Check failed"
        case "unsupported": return "Unsupported"
        default: return "Not checked"
        }
    }
    var tint: Color {
        switch status {
        case "reachable": return Palette.green
        case "auth_required", "unsupported": return Palette.amber
        case "failed": return Palette.red
        default: return Palette.muted
        }
    }
    var lastCheckText: String {
        guard let value = last_checked else { return "Never checked" }
        return Date(timeIntervalSince1970: value).formatted(.relative(presentation: .named))
    }
}
struct Client: Decodable, Identifiable {
    var id: String
    var name: String
    var config_path: String
    var installed: Bool
    var configured: Bool
    var bridge_count: Int
    var error: String?
    var glyph: String {
        switch id {
        case "codex": return "chevron.left.forwardslash.chevron.right"
        case "claude": return "asterisk"
        case "cursor": return "cursorarrow"
        default: return "curlybraces"
        }
    }
}
struct DaemonState: Decodable {
    var last_checked: Double?
    var bridge_count: Int
}
struct ManagerSettings: Decodable { var check_interval: Int }
struct Snapshot: Decodable {
    var servers: [Server]
    var clients: [Client]
    var errors: [String]
    var settings: ManagerSettings
    var daemon: DaemonState
}
struct ConfigReview: Decodable, Identifiable {
    var client: String
    var path: String
    var digest: String
    var diff: String
    var remove: Bool
    var warning: String
    var id: String { digest }
}

enum Palette {
    static let background = Color(red: 0.065, green: 0.07, blue: 0.09)
    static let panel = Color(red: 0.095, green: 0.10, blue: 0.13)
    static let raised = Color(red: 0.13, green: 0.14, blue: 0.18)
    static let stroke = Color.white.opacity(0.08)
    static let blue = Color(red: 0.57, green: 0.65, blue: 1)
    static let muted = Color(red: 0.57, green: 0.60, blue: 0.67)
    static let green = Color(red: 0.48, green: 0.84, blue: 0.64)
    static let amber = Color(red: 1, green: 0.81, blue: 0.48)
    static let red = Color(red: 1, green: 0.55, blue: 0.60)
}

struct ServerConfigDocument: Decodable {
    var config_json: String
    var revision: String
}
struct ServerConfigDraft {
    var text: String
    var original: String
    var revision: String
}
