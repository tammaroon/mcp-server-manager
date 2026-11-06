import AppKit
import SwiftUI

struct SettingsView: View {
    @ObservedObject var model: ManagerModel
    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 26) {
                VStack(alignment: .leading, spacing: 20) {
                    SectionTitle(title: "General")
                    HStack {
                        VStack(alignment: .leading, spacing: 5) { Text("Launch at login").font(.system(size: 13, weight: .medium)); Text("Start the menu-bar manager and gateway when you sign in.").font(.system(size: 11)).foregroundStyle(Palette.muted) }
                        Spacer()
                        Toggle("Launch at login", isOn: Binding(get: { model.loginEnabled }, set: { model.setLogin($0) })).labelsHidden().toggleStyle(.switch).controlSize(.small)
                    }
                    Divider()
                    HStack {
                        VStack(alignment: .leading, spacing: 5) { Text("Automatic connection checks").font(.system(size: 13, weight: .medium)); Text("Initialization and tool listing. Never a production query.").font(.system(size: 11)).foregroundStyle(Palette.muted) }
                        Spacer()
                        Picker("Cadence", selection: Binding(get: { model.snapshot?.settings.check_interval ?? 0 }, set: { model.perform("settings", values: ["check_interval": $0]) })) {
                            Text("Manual only").tag(0); Text("Every 5 minutes").tag(300); Text("Every 15 minutes").tag(900); Text("Every 30 minutes").tag(1800); Text("Every hour").tag(3600)
                        }.labelsHidden().frame(width: 160).disabled(model.busy)
                    }
                }.padding(22).background(Palette.panel, in: RoundedRectangle(cornerRadius: 12))
                VStack(alignment: .leading, spacing: 14) {
                    SectionTitle(title: "Discovery")
                    Text("Codex · Claude Desktop · Cursor · VS Code").font(.system(size: 13, weight: .medium))
                    Text("Imports user-wide MCP configs into the common mcp.json file. Credentials and browser sign-in tokens are stored directly with each server’s settings, with owner-only file permissions. Workspace-specific configurations and interactive input variables are not imported.").font(.system(size: 11)).foregroundStyle(Palette.muted).lineSpacing(5)
                    Button("Discover client configs") { model.perform("discover", label: "Discovering client configurations…") }.buttonStyle(SoftButtonStyle()).disabled(model.busy)
                }.padding(22).background(Palette.panel, in: RoundedRectangle(cornerRadius: 12))
                VStack(alignment: .leading, spacing: 14) {
                    SectionTitle(title: "Local runtime")
                    InfoLine(label: "Application", value: "MCP Server Manager 0.1.0 · macOS 14+")
                    InfoLine(label: "Python", value: Backend.python ?? "Not found")
                    InfoLine(label: "Transport", value: "Permission-protected Unix socket · no public or LAN endpoint")
                    Text("This version routes tools over stdio and Streamable HTTP. Compatible remote servers support browser sign-in. Legacy SSE, resources, prompts, sampling and elicitation are not implemented. Session state is shared per upstream; grant access only to clients that can safely share that state.").font(.system(size: 11)).foregroundStyle(Palette.muted).lineSpacing(5)
                    HStack {
                        Button("Open Login Items") { SMSettings.openLoginItems() }.buttonStyle(SoftButtonStyle())
                        Button("Reconnect gateway") { model.start() }.buttonStyle(SoftButtonStyle()).disabled(model.busy)
                    }
                }.padding(22).background(Palette.panel, in: RoundedRectangle(cornerRadius: 12))
            }.padding(28)
        }
    }
}
enum SMSettings {
    static func openLoginItems() { if let url = URL(string: "x-apple.systempreferences:com.apple.LoginItems-Settings.extension") { NSWorkspace.shared.open(url) } }
}

struct MenuPopoverView: View {
    @ObservedObject var model: ManagerModel
    var size: NSSize = MenuBarPresentation.preferredSize
    var openManager: () -> Void
    var quit: () -> Void
    var body: some View {
        VStack(spacing: 0) {
            VStack(alignment: .leading, spacing: 17) {
                HStack(spacing: 10) {
                    BrandMark(size: 33)
                    VStack(alignment: .leading, spacing: 3) { Text("MCP Server Manager").font(.system(size: 16.5, weight: .semibold)); Text("Shared daemon · \(model.servers.count) upstream servers").font(.system(size: 11.0)).foregroundStyle(Palette.muted) }
                    Spacer()
                    Menu {
                        Button("Settings") { model.page = "settings"; openManager() }
                        Divider()
                        Button("Quit manager", action: quit)
                    } label: { Image(systemName: "ellipsis").font(.system(size: 18.7)) }.menuStyle(.borderlessButton).fixedSize().frame(width: 20)
                }
                HStack {
                    VStack(alignment: .leading, spacing: 5) {
                        Text(model.starting ? "Starting gateway…" : model.aggregate.0).font(.system(size: 18.7, weight: .semibold)).tracking(-0.4)
                        Text(model.busy ? model.operation : model.snapshot?.daemon.last_checked.map { "Last checked " + Date(timeIntervalSince1970: $0).formatted(.relative(presentation: .named)) } ?? "Upstream checks: never").font(.system(size: 11.0)).foregroundStyle(Palette.muted)
                    }
                    Spacer()
                    StatusDot(color: model.aggregate.1)
                }
                HStack(spacing: 8) {
                    Button { model.check() } label: { Label(model.busy ? "Checking…" : "Check daemon & servers", systemImage: "arrow.clockwise") }.buttonStyle(SoftButtonStyle(primary: true, fontSize: 13.2)).disabled(model.busy || model.starting)
                    Button("Open manager", action: openManager).buttonStyle(SoftButtonStyle(fontSize: 13.2))
                }
                if let error = model.error { Text(error).font(.system(size: 11.0)).foregroundStyle(Palette.amber).lineLimit(3) }
            }.padding(18).fixedSize(horizontal: false, vertical: true)
            Rectangle().fill(Palette.stroke).frame(height: 1)
            ScrollView {
                VStack(spacing: 0) {
                    ForEach(["stdio", "HTTP"], id: \.self) { transport in
                        let servers = model.servers.filter { $0.transport == transport }
                        if !servers.isEmpty {
                            SectionTitle(title: transport == "stdio" ? "Upstream · local stdio" : "Upstream · remote HTTP", trailing: "\(servers.count)", fontSize: 9.9).padding(.horizontal, 17).padding(.top, 14).padding(.bottom, 6)
                            ForEach(servers) { server in
                                HStack(spacing: 10) {
                                    Button { openServer(server) } label: {
                                        HStack(spacing: 10) {
                                            ServerIcon(server: server, size: 30)
                                            VStack(alignment: .leading, spacing: 4) {
                                                Text(server.displayName).font(.system(size: 12.1, weight: .medium)).lineLimit(1)
                                                HStack(spacing: 5) { TransportTag(text: server.transport, fontSize: 9.9); Text(server.clients.isEmpty ? "No clients allowed" : server.clients.map { $0.capitalized }.joined(separator: " · ")).font(.system(size: 9.9)).foregroundStyle(Palette.muted).lineLimit(1) }
                                            }
                                            Spacer(minLength: 0)
                                        }.contentShape(Rectangle())
                                    }.buttonStyle(.plain)
                                    VStack(alignment: .trailing, spacing: 6) {
                                        Button { openServer(server) } label: {
                                            HStack(spacing: 5) { StatusDot(color: server.tint); Text(server.statusLabel).font(.system(size: 9.9)).foregroundStyle(server.tint); Image(systemName: "chevron.right").font(.system(size: 8.8)).foregroundStyle(Palette.muted) }
                                        }.buttonStyle(.plain)
                                        if server.shouldOfferDropdownLogin {
                                            Button { model.signIn(server) } label: {
                                                Label(server.oauth_status == "pending" ? "Signing in…" : "Sign in", systemImage: "person.crop.circle.badge.checkmark")
                                                    .font(.system(size: 11.0, weight: .medium))
                                            }.buttonStyle(.borderedProminent).controlSize(.mini)
                                                .disabled(model.busy || server.oauth_status == "pending")
                                                .help("Open browser sign-in for \(server.displayName)")
                                                .accessibilityLabel("Sign in to \(server.displayName)")
                                        }
                                    }.fixedSize(horizontal: true, vertical: false)
                                }.padding(.horizontal, 17).padding(.vertical, 10)
                                Rectangle().fill(Palette.stroke.opacity(0.6)).frame(height: 1).padding(.leading, 57)
                            }
                        }
                    }
                    if model.servers.isEmpty { Text("Open manager to discover or add servers.").font(.system(size: 13.2)).foregroundStyle(Palette.muted).padding(25) }
                }.padding(.bottom, 10)
            }.frame(maxWidth: .infinity, maxHeight: .infinity).layoutPriority(1)
            HStack(spacing: 7) {
                StatusDot(color: !model.gatewayAvailable ? Palette.amber : Palette.green)
                Text(!model.gatewayAvailable ? "Gateway unavailable" : "Local gateway running").font(.system(size: 11.0))
                Spacer()
                Toggle("Launch at login", isOn: Binding(get: { model.loginEnabled }, set: { model.setLogin($0) })).font(.system(size: 11.0)).toggleStyle(.switch).controlSize(.mini)
            }.padding(.horizontal, 17).padding(.vertical, 13).background(Palette.panel).fixedSize(horizontal: false, vertical: true)
        }.frame(width: size.width, height: size.height).background(Palette.background).preferredColorScheme(.dark).tint(Palette.blue)
    }
    private func openServer(_ server: Server) {
        model.page = "servers"
        model.selectedID = server.id
        openManager()
    }

}
