import SwiftUI

struct ServerDetailView: View {
    @ObservedObject var model: ManagerModel
    var server: Server
    @State private var policyClient: Client?
    private var canSignIn: Bool {
        server.supportsBrowserSignIn
    }
    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 24) {
                HStack(alignment: .top, spacing: 13) {
                    ServerIcon(server: server, size: 45)
                    VStack(alignment: .leading, spacing: 6) {
                        Text(server.displayName).font(.system(size: 19, weight: .semibold)).tracking(-0.4)
                        HStack(spacing: 7) { TransportTag(text: server.transport); Text(server.transport == "HTTP" ? "Remote upstream" : "Local upstream process").font(.system(size: 11)).foregroundStyle(Palette.muted) }
                    }
                    Spacer()
                    Button { model.deletionCandidate = server } label: { Image(systemName: "trash").foregroundStyle(Palette.red) }
                        .buttonStyle(SoftButtonStyle()).disabled(model.busy).help("Delete server")
                        .accessibilityLabel("Delete \(server.displayName)")
                    Toggle("Enabled", isOn: Binding(get: { server.enabled }, set: { model.update(server, ["enabled": $0]) })).toggleStyle(.switch).labelsHidden().controlSize(.small).disabled(model.busy).help("Enable this upstream in the gateway")
                }
                VStack(alignment: .leading, spacing: 12) {
                    HStack { StatusPill(text: server.statusLabel, color: server.tint); Spacer(); Text(server.lastCheckText).font(.system(size: 10)).foregroundStyle(Palette.muted) }
                    Text(server.diagnostic).font(.system(size: 11)).foregroundStyle(.white.opacity(0.72)).lineSpacing(4).textSelection(.enabled)
                    HStack(spacing: 8) {
                        Button { model.check(server) } label: { Label("Check connection", systemImage: "arrow.clockwise") }.buttonStyle(SoftButtonStyle(primary: true)).disabled(model.busy || !server.enabled || !server.supported)
                        if canSignIn {
                            Button { model.signIn(server) } label: { Label(server.name.localizedCaseInsensitiveContains("webex") ? "Open login screen" : "Sign in", systemImage: "person.crop.circle.badge.checkmark") }.buttonStyle(SoftButtonStyle(primary: server.status == "auth_required")).disabled(model.busy || server.oauth_status == "pending" || !server.enabled)
                        }
                        if server.transport == "stdio" { Button { model.perform("restart", values: ["server_id": server.id], label: "Restarting upstream…") } label: { Image(systemName: "arrow.counterclockwise") }.buttonStyle(SoftButtonStyle()).help("Restart local connection").disabled(model.busy || !server.enabled || !server.supported) }
                    }
                    if server.oauth_status == "pending" {
                        HStack(spacing: 7) { ProgressView().controlSize(.mini); Text("Complete sign-in in your browser.").font(.system(size: 11)).foregroundStyle(Palette.muted) }
                    } else if server.oauth_status == "authorized" {
                        Label("Sign-in completed.", systemImage: "checkmark.circle").font(.system(size: 11)).foregroundStyle(Palette.green)
                    } else if server.oauth_status == "failed" {
                        Text(server.oauth_error.map { String($0.prefix(180)) } ?? "Sign-in failed. Try opening the login screen again.").font(.system(size: 11)).foregroundStyle(Palette.amber).lineLimit(3)
                    }
                }.padding(16).background(Palette.raised.opacity(0.5), in: RoundedRectangle(cornerRadius: 11)).overlay(RoundedRectangle(cornerRadius: 11).strokeBorder(Palette.stroke))
                VStack(alignment: .leading, spacing: 10) {
                    SectionTitle(title: "Connection")
                    InfoLine(label: server.transport == "HTTP" ? "Endpoint" : "Executable", value: server.endpoint)
                    InfoLine(label: "Authentication", value: server.auth)
                    if let identity = server.identity, let name = identity.name { InfoLine(label: "MCP identity", value: name + (identity.version.map { " · \($0)" } ?? "")) }
                    if let duration = server.duration_ms { InfoLine(label: "Last duration", value: "\(duration) ms") }
                }
                ServerConfigEditor(model: model, serverID: server.id)
                Rectangle().fill(Palette.stroke).frame(height: 1)
                VStack(alignment: .leading, spacing: 14) {
                    SectionTitle(title: "Client access", trailing: "\(server.clients.count) allowed")
                    Text("Allow clients to see and call this upstream’s tools through the gateway.").font(.system(size: 10)).foregroundStyle(Palette.muted).lineSpacing(3)
                    ForEach(model.snapshot?.clients ?? []) { client in
                        HStack(spacing: 9) {
                            Image(systemName: client.glyph).font(.system(size: 12)).foregroundStyle(Palette.muted).frame(width: 18)
                            VStack(alignment: .leading, spacing: 3) {
                                Text(client.name).font(.system(size: 11, weight: .medium))
                                Text(client.bridge_count > 0 ? "Bridge running" : client.configured ? "Bridge configured · not running" : "Bridge not configured").font(.system(size: 9)).foregroundStyle(Palette.muted)
                            }
                            Spacer()
                            if server.clients.contains(client.id), !server.tools.isEmpty {
                                Button(server.tool_allowlists[client.id].map { "\($0.count) tools" } ?? "All tools") { policyClient = client }.font(.system(size: 9)).foregroundStyle(Palette.blue).buttonStyle(.plain)
                            }
                            Toggle(client.name, isOn: Binding(get: { server.clients.contains(client.id) }, set: { model.toggleClient(server, client: client.id, enabled: $0) })).labelsHidden().toggleStyle(.switch).controlSize(.mini).disabled(model.busy)
                        }
                    }
                    Text("Reconnect the client to refresh its visible tool list. Revoked access takes effect immediately.").font(.system(size: 9)).foregroundStyle(Palette.muted).lineSpacing(3)
                }
                Rectangle().fill(Palette.stroke).frame(height: 1)
                VStack(alignment: .leading, spacing: 12) {
                    SectionTitle(title: "Discovered tools", trailing: "\(server.tools.count)")
                    if server.tools.isEmpty { Text(server.last_checked == nil ? "Check this server to discover its tools." : "No tools returned by the last successful check.").font(.system(size: 11)).foregroundStyle(Palette.muted) }
                    ForEach(server.tools) { tool in
                        VStack(alignment: .leading, spacing: 5) {
                            Label(tool.name, systemImage: "wrench.and.screwdriver").font(.system(size: 10, design: .monospaced)).foregroundStyle(.white.opacity(0.8))
                            if let desc = tool.description { Text(desc).font(.system(size: 10)).foregroundStyle(Palette.muted).lineLimit(3).lineSpacing(3) }
                        }.frame(maxWidth: .infinity, alignment: .leading).padding(10).background(Palette.raised.opacity(0.4), in: RoundedRectangle(cornerRadius: 7))
                    }
                }
            }.padding(25)
        }.frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
            .sheet(item: $policyClient) { client in ToolPolicyView(model: model, server: server, client: client) }
    }
}

struct ToolPolicyView: View {
    @ObservedObject var model: ManagerModel
    var server: Server
    var client: Client
    @Environment(\.dismiss) private var dismiss
    @State private var all = true
    @State private var selection: Set<String> = []
    var body: some View {
        VStack(alignment: .leading, spacing: 18) {
            Text("Tools for \(client.name)").font(.system(size: 19, weight: .semibold))
            Text(server.displayName).font(.system(size: 12)).foregroundStyle(Palette.muted)
            Toggle("Allow all current and future tools", isOn: $all)
            ScrollView { VStack(alignment: .leading, spacing: 12) { ForEach(server.tools) { tool in Toggle(tool.name, isOn: Binding(get: { all || selection.contains(tool.name) }, set: { if $0 { selection.insert(tool.name) } else { selection.remove(tool.name) } })).font(.system(size: 11, design: .monospaced)).disabled(all) } } }.frame(maxHeight: 300)
            HStack { Button("Cancel") { dismiss() }.buttonStyle(SoftButtonStyle()); Spacer(); Button("Save access") { var rules = server.tool_allowlists; if all { rules.removeValue(forKey: client.id) } else { rules[client.id] = selection.sorted() }; model.update(server, ["tool_allowlists": rules]); dismiss() }.buttonStyle(SoftButtonStyle(primary: true)) }
        }.padding(28).frame(width: 420).background(Palette.background).onAppear { if let tools = server.tool_allowlists[client.id] { all = false; selection = Set(tools) } }.preferredColorScheme(.dark)
    }
}
