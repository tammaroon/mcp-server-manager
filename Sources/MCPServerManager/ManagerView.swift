import SwiftUI

struct ManagerView: View {
    @ObservedObject var model: ManagerModel
    @State private var adding = false
    var body: some View {
        HStack(spacing: 0) {
            sidebar
            Rectangle().fill(Palette.stroke).frame(width: 1)
            VStack(spacing: 0) {
                header
                if let error = model.error { Banner(text: error, isError: true) { model.error = nil } }
                if let notice = model.notice { Banner(text: notice, isError: false) { model.notice = nil } }
                if model.starting {
                    VStack(spacing: 14) { ProgressView().controlSize(.small); Text("Starting your local gateway…").foregroundStyle(Palette.muted) }.frame(maxWidth: .infinity, maxHeight: .infinity)
                } else {
                    switch model.page {
                    case "clients": ClientsView(model: model)
                    case "settings": SettingsView(model: model)
                    default: serversPage
                    }
                }
            }
        }
        .frame(minWidth: 1060, minHeight: 700)
        .background(Palette.background).preferredColorScheme(.dark)
        .tint(Palette.blue)
        .sheet(item: $model.review) { change in ConfigReviewView(model: model, change: change) }
        .sheet(isPresented: $adding) { AddServerView(model: model) }
        .alert("Delete MCP server?", isPresented: Binding(get: { model.deletionCandidate != nil }, set: { if !$0 { model.deletionCandidate = nil } }), presenting: model.deletionCandidate) { server in
            Button("Delete", role: .destructive) { model.deleteServer(server) }
            Button("Cancel", role: .cancel) { model.deletionCandidate = nil }
        } message: { server in
            Text("Delete \(server.displayName) and its saved credentials from the common MCP configuration? It will no longer be available to connected clients or added back by discovery.")
        }
    }
    private var sidebar: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack(spacing: 10) { BrandMark(size: 32); VStack(alignment: .leading, spacing: 2) { Text("MCP Manager").font(.system(size: 14, weight: .semibold)); Text("YOUR LOCAL GATEWAY").font(.system(size: 8, weight: .medium)).tracking(1.2).foregroundStyle(Palette.muted) } }.padding(.top, 35).padding(.bottom, 35)
            SectionTitle(title: "Workspace").padding(.horizontal, 10).padding(.bottom, 12)
            nav("servers", title: "MCP servers", icon: "square.stack.3d.up", count: model.servers.count)
            nav("clients", title: "Agent clients", icon: "app.connected.to.app.below.fill")
            Spacer()
            VStack(alignment: .leading, spacing: 8) {
                HStack(spacing: 7) { StatusDot(color: !model.gatewayAvailable ? Palette.amber : Palette.green); Text(!model.gatewayAvailable ? "Gateway unavailable" : "Local gateway running").font(.system(size: 11, weight: .medium)) }
                Text("\(model.snapshot?.daemon.bridge_count ?? 0) active client bridges").font(.system(size: 10)).foregroundStyle(Palette.muted).padding(.leading, 13)
            }.padding(13).frame(maxWidth: .infinity, alignment: .leading).background(.white.opacity(0.025), in: RoundedRectangle(cornerRadius: 10)).padding(.bottom, 12)
            nav("settings", title: "Settings", icon: "gearshape")
            HStack { Text("MCP Server Manager"); Spacer(); Text("v0.1") }.font(.system(size: 9)).foregroundStyle(Palette.muted.opacity(0.65)).padding(.horizontal, 10).padding(.top, 20).padding(.bottom, 18)
        }.padding(.horizontal, 16).frame(width: 210).background(Palette.panel)
    }
    private func nav(_ id: String, title: String, icon: String, count: Int? = nil) -> some View {
        Button { model.page = id } label: {
            HStack(spacing: 10) {
                Image(systemName: icon).font(.system(size: 13)).frame(width: 18)
                Text(title).font(.system(size: 12, weight: model.page == id ? .medium : .regular)).lineLimit(1).minimumScaleFactor(0.9)
                Spacer()
                if let count { Text("\(count)").font(.system(size: 10, design: .monospaced)).foregroundStyle(model.page == id ? Palette.blue : Palette.muted) }
            }.foregroundStyle(model.page == id ? Color.white : Palette.muted).padding(.horizontal, 11).padding(.vertical, 11)
                .background(model.page == id ? Palette.blue.opacity(0.1) : Color.clear, in: RoundedRectangle(cornerRadius: 8))
        }.buttonStyle(.plain).padding(.bottom, 4)
    }
    private var header: some View {
        HStack(alignment: .center) {
            VStack(alignment: .leading, spacing: 5) {
                Text(["servers": "MCP servers", "clients": "Agent clients", "settings": "Settings"][model.page] ?? "MCP Manager").font(.system(size: 21, weight: .semibold)).tracking(-0.5)
                Text(["servers": "One place for every MCP connection.", "clients": "One bridge per app. One shared upstream pool.", "settings": "A small utility that stays out of your way."][model.page] ?? "").font(.system(size: 11)).foregroundStyle(Palette.muted)
            }
            Spacer()
            if model.busy { ProgressView().controlSize(.small).padding(.trailing, 6) }
            if model.page == "servers" {
                Button { model.openCommonConfig() } label: { Label("Open common config", systemImage: "doc.text") }.buttonStyle(SoftButtonStyle()).disabled(model.starting || model.busy)
                Button { adding = true } label: { Label("Add server", systemImage: "plus") }.buttonStyle(SoftButtonStyle()).disabled(model.busy || model.starting)
                Button { model.check() } label: { Label("Check all", systemImage: "arrow.clockwise") }.buttonStyle(SoftButtonStyle(primary: true)).disabled(model.busy || model.starting)
            }
        }.padding(.horizontal, 28).padding(.top, 32).padding(.bottom, 24)
            .background(Palette.background).overlay(alignment: .bottom) { Rectangle().fill(Palette.stroke).frame(height: 1) }
    }
    private var serversPage: some View {
        VStack(spacing: 0) {
            HStack(spacing: 0) {
                serverList.frame(width: 330)
                Rectangle().fill(Palette.stroke).frame(width: 1)
                if let server = model.selected { ServerDetailView(model: model, server: server).id(server.id) }
                else { EmptyState(symbol: "point.3.connected.trianglepath.dotted", title: "Your connections start here", message: "Discover existing MCP definitions or add an upstream server.") }
            }
            HStack(spacing: 7) {
                Image(systemName: "lock.shield").foregroundStyle(Palette.blue)
                Text(model.busy ? model.operation : "Health checks use initialization & tool listing only.")
                Spacer()
                Text("No public network listener").foregroundStyle(Palette.muted.opacity(0.7))
            }.font(.system(size: 10)).foregroundStyle(Palette.muted).padding(.horizontal, 22).padding(.vertical, 12).background(Palette.panel)
        }
    }
    private var serverList: some View {
        VStack(spacing: 0) {
            HStack(spacing: 8) {
                Image(systemName: "magnifyingglass").foregroundStyle(Palette.muted)
                TextField("Find a server…", text: $model.search).textFieldStyle(.plain).font(.system(size: 11))
                if !model.search.isEmpty { Button { model.search = "" } label: { Image(systemName: "xmark.circle.fill").foregroundStyle(Palette.muted) }.buttonStyle(.plain) }
            }.padding(10).background(Palette.raised.opacity(0.55), in: RoundedRectangle(cornerRadius: 7)).padding(16)
            HStack {
                Menu { ForEach(["All servers", "Needs attention", "Local stdio", "Remote HTTP"], id: \.self) { f in Button(f) { model.filter = f } } } label: { HStack(spacing: 5) { Text(model.filter); Image(systemName: "chevron.down").font(.system(size: 8)) } }.menuStyle(.borderlessButton).fixedSize()
                Spacer()
                Text("\(model.filtered.count) servers").foregroundStyle(Palette.muted)
            }.font(.system(size: 10)).padding(.horizontal, 20).padding(.bottom, 12)
            ScrollView {
                LazyVStack(spacing: 4) {
                    ForEach(model.filtered) { server in
                        Button { model.selectedID = server.id } label: {
                            HStack(spacing: 11) {
                                ServerIcon(server: server)
                                VStack(alignment: .leading, spacing: 5) {
                                    Text(server.displayName).font(.system(size: 12, weight: .medium)).lineLimit(1)
                                    TransportTag(text: server.transport)
                                }
                                Spacer(minLength: 3)
                                StatusDot(color: server.tint)
                            }.padding(12).background(model.selectedID == server.id ? Palette.blue.opacity(0.10) : Color.clear, in: RoundedRectangle(cornerRadius: 9))
                                .overlay(RoundedRectangle(cornerRadius: 9).strokeBorder(model.selectedID == server.id ? Palette.blue.opacity(0.22) : Color.clear))
                                .contentShape(Rectangle())
                        }.buttonStyle(.plain)
                        .contextMenu {
                            Button(role: .destructive) { model.deletionCandidate = server } label: { Label("Delete server…", systemImage: "trash") }
                                .disabled(model.busy)
                        }
                    }
                    if model.filtered.isEmpty { Text(model.servers.isEmpty ? "No servers discovered." : "No matching servers.").font(.system(size: 12)).foregroundStyle(Palette.muted).padding(25) }
                }.padding(.horizontal, 10).padding(.bottom, 12)
            }
            Button { model.perform("discover", label: "Discovering client configurations…") } label: { Label("Discover client configs", systemImage: "arrow.triangle.2.circlepath").frame(maxWidth: .infinity) }.buttonStyle(SoftButtonStyle()).padding(16).disabled(model.busy)
        }.background(Palette.panel.opacity(0.25))
    }
}
